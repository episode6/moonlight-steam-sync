"""Write the grid files for one shortcut, and the loop over all of them.

This is where the artwork phase meets the Steam shortcut layer, and it meets
it through **one narrow seam** so the two can be built independently:

.. code-block:: text

    ArtTarget(name, appid, grid_dir)                what the art phase needs
    TargetProvider.targets()                        where those come from
    TargetProvider.set_icon(appid, icon_path)       the one thing art writes back
    TargetProvider.commit()                         flush those icon patches

``appid`` is the shortcut's own 32-bit id (``crc32(Exe + AppName) |
0x80000000``, spec 2.1), which is knowable *before* the shortcut exists, and
``grid_dir`` is ``userdata/<steamid3>/config/grid``. Nothing in this package
itself resolves artwork through that seam: :class:`SteamShortcutProvider`
(backed by the PR-2 ``steam``/``shortcuts`` modules) supplies it.

Implementations of ``set_icon`` must **not** write ``shortcuts.vdf`` on the
spot: spec 3.6 wants exactly one atomic vdf write per run, after the whole
art phase, which is what ``commit()`` is for -- and :func:`run_art` does not
call it either. The *caller* commits once the summary is out, because a
commit can be refused (Steam is running and may not be restarted, exit 2)
and that refusal must not swallow the progress report.

Per-title flow (spec 3.5 and 3.9):

1. ``[overrides] art = false`` -> the title is skipped entirely.
2. Resolve the title (the match cache is flushed by the resolver).
3. For each slot: an existing file wins unless ``--force``; otherwise try the
   sources in order; a slot that finds nothing is cached as missing.
4. If an ``_icon`` file exists afterwards, hand its absolute path to the
   provider.
5. Flush the cache and print the one-line progress report for the title.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO

from moonlight_steam_sync import steam
from moonlight_steam_sync.art.http import HardStop
from moonlight_steam_sync.art.resolve import Match, Resolver
from moonlight_steam_sync.art.select import ICON, SLOTS, Selector, Slot, existing_slot_file
from moonlight_steam_sync.config import Config, default_exe
from moonlight_steam_sync.shortcuts import (
    CLIENT_APP_NAME,
    Shortcut,
    ShortcutsError,
    ShortcutsFile,
    unquote,
)

#: Slot outcomes, as they appear in the progress lines.
SOURCE_KEPT = "kept"  # a file was already there (spec 6.9)
SOURCE_MISSING = "missing"  # nothing found; the user can fill it in by hand
SOURCE_SKIPPED = "skipped"  # [overrides] art = false, or a cached slot miss
SOURCE_CACHED_MISS = "cached-miss"


@dataclass(frozen=True)
class ArtTarget:
    """One shortcut to dress.

    Attributes:
        name: the Moonlight app name, *without* ``name_suffix`` -- that is
            what gets searched for (spec 3.5).
        appid: the shortcut's 32-bit appid; the grid filenames are built from
            this, never from the Steam store appid the art came from.
        grid_dir: ``userdata/<steamid3>/config/grid``.
    """

    name: str
    appid: int
    grid_dir: Path
    #: ``Shortcut.is_hidden`` / ``.app_name`` -- filled in by
    #: :class:`SteamShortcutProvider` for ``status --json`` (spec 3.4.6);
    #: unused by the art phase itself, so every existing caller's default
    #: (``False`` / ``""``) is fine.
    hidden: bool = False
    app_name: str = ""
    #: The title's kind (decky spec 3.2) for ``sync``'s ``title`` event;
    #: ``sync.art_targets`` fills it from the plan, ``art`` derives its own
    #: from ``hidden`` and never reads this.
    kind: str = "shortcut"
    #: The Moonlight client shortcut (decky spec 3.4.5): ``status`` reports
    #: it, ``art`` skips it (no lookups for "Moonlight").
    client: bool = False


class TargetProvider(Protocol):
    """The seam onto the shortcut layer (implemented in PR-2/PR-5)."""

    def targets(self) -> Sequence[ArtTarget]:
        """Owned shortcuts (``Exe`` == the configured exe, spec 3.3)."""
        ...

    def set_icon(self, appid: int, icon_path: Path) -> None:
        """Record that this shortcut's ``icon`` field should point at ``icon_path``."""
        ...

    def commit(self) -> None:
        """Write any recorded icon patches: one atomic vdf write (spec 3.6)."""
        ...


class TargetsUnavailable(RuntimeError):
    """The shortcut layer could not produce the owned shortcuts."""


@dataclass
class CommitResult:
    """What a :data:`ShortcutsWriter` did to ``shortcuts.vdf`` (spec 3.4.6).

    Mirrors :class:`moonlight_steam_sync.sync.Commit` (the shape the
    ``commit`` JSON event needs -- ``written``/``restarted``/``backup``/
    ``relaunch_error``) without importing ``sync``, which itself imports
    this module.
    """

    written: bool = False
    restarted: bool = False
    backup: Path | None = None
    relaunch_error: str = ""


#: How a provider gets its patched ``ShortcutsFile`` onto disk. Returns a
#: :class:`CommitResult` describing what happened, so the caller knows
#: whether the new artwork is already showing or still needs a restart, and
#: can report the same detail ``sync``'s own ``commit`` event does.
ShortcutsWriter = Callable[[ShortcutsFile], CommitResult]


def patch_icon(entry: Shortcut, icon_path: Path) -> bool:
    """Point ``entry.icon`` at ``icon_path`` unless it already points at art that exists.

    Spec 3.6 patches ``icon`` on adopted entries *whose icon file is new*.
    An entry whose ``icon`` already names a file on disk -- the same one, or
    one the user picked by hand in the Steam UI -- is left alone; only an
    empty or dangling value is (re)pointed. The path is stored bare: that is
    how Steam's own UI and every tool in this space write it, and how the
    PR-2 fixture models a device file. Returns whether anything changed.
    """
    wanted = str(icon_path)
    current = unquote(entry.icon)
    if current == wanted:
        return False
    if current and Path(current).is_file():
        return False
    entry.icon = wanted
    return True


def write_when_steam_is_down(shortcuts_file: ShortcutsFile) -> CommitResult:
    """The default :data:`ShortcutsWriter`: write, or refuse if Steam is up.

    A bare write under a live Steam is silently lost when Steam rewrites the
    file on exit (spec 2.1), so this never does one. The ``sync`` module
    supplies the writer that shuts Steam down, writes and relaunches it
    (spec 3.6); this default is the safety net for any other caller. Never
    restarts Steam, so ``restarted`` is always ``False``.
    """
    if steam.is_running():
        raise steam.SteamRunningError(
            "Steam is running, so shortcuts.vdf was not written (Steam would overwrite "
            "it on exit). Quit Steam and rerun, or let `sync` restart it for you."
        )
    written = shortcuts_file.write()
    return CommitResult(written=written)


class SteamShortcutProvider:
    """The real :class:`TargetProvider`, backed by ``steam``/``shortcuts``.

    Reads the owned entries out of the picked Steam user's ``shortcuts.vdf``
    (ownership is ``Exe`` == the configured ``exe``, spec 3.3), hands the art
    phase each one's stored appid and that user's grid directory, and holds
    icon patches until :meth:`commit` hands the file to ``writer`` for the
    run's single atomic write. The default writer refuses to write while
    Steam is running; ``sync`` injects the one that restarts Steam.
    """

    def __init__(self, config: Config, *, writer: ShortcutsWriter | None = None) -> None:
        self._config = config
        self._writer: ShortcutsWriter = writer or write_when_steam_is_down
        self._file: ShortcutsFile | None = None
        self._grid_dir: Path | None = None
        #: Set by :meth:`commit`: the writer's full result, for the
        #: ``commit`` JSON event (spec 3.4.6).
        self.last_commit = CommitResult()
        #: Kept for callers that only care whether Steam was bounced;
        #: mirrors ``last_commit.restarted``.
        self.restarted_steam = False

    def _load(self) -> tuple[ShortcutsFile, Path]:
        if self._file is None or self._grid_dir is None:
            try:
                user = steam.pick_user(steam.find_steam_root())
            except steam.SteamError as exc:
                raise TargetsUnavailable(str(exc)) from exc
            try:
                self._file = ShortcutsFile.read(user.shortcuts_path)
            except ShortcutsError as exc:
                raise TargetsUnavailable(f"could not read {user.shortcuts_path}: {exc}") from exc
            self._grid_dir = user.grid_dir
        return self._file, self._grid_dir

    def targets(self) -> Sequence[ArtTarget]:
        shortcuts_file, grid_dir = self._load()
        tool_exe = default_exe()
        targets: list[ArtTarget] = []
        for entry in shortcuts_file.owned(self._config.exe):
            client = entry.is_client_entry(tool_exe)
            targets.append(
                ArtTarget(
                    name=(
                        CLIENT_APP_NAME
                        if client
                        else entry.moonlight_name(
                            self._config.launch_options, self._config.name_suffix
                        )
                    ),
                    appid=entry.appid,
                    grid_dir=grid_dir,
                    hidden=bool(entry.is_hidden),
                    app_name=entry.app_name,
                    kind="stream" if entry.is_hidden else "shortcut",
                    client=client,
                )
            )
        return targets

    def set_icon(self, appid: int, icon_path: Path) -> None:
        shortcuts_file, _ = self._load()
        entry = shortcuts_file.by_appid(appid)
        if entry is not None:
            patch_icon(entry, icon_path)

    def commit(self) -> None:
        """Write the icon patches -- a no-op on disk when nothing changed."""
        if self._file is not None and self._file.changed:
            self.last_commit = self._writer(self._file)
            self.restarted_steam = self.last_commit.restarted


def default_target_provider(config: Config) -> TargetProvider:
    """The provider used when nothing else is injected."""
    return SteamShortcutProvider(config)


@dataclass
class SlotOutcome:
    """What happened to one slot for one title."""

    slot: str
    source: str
    path: Path | None = None
    url: str | None = None

    @property
    def filled(self) -> bool:
        return self.source not in (SOURCE_MISSING, SOURCE_SKIPPED, SOURCE_CACHED_MISS)


@dataclass
class TitleResult:
    """One line of the progress stream."""

    target: ArtTarget
    match: Match
    slots: dict[str, SlotOutcome] = field(default_factory=dict)
    skipped: bool = False
    explain: list[str] = field(default_factory=list)

    def progress_line(self, index: int, total: int) -> str:
        if self.skipped:
            return f"[{index}/{total}] {self.target.name}: skipped (overrides)"
        parts = " ".join(f"{slot}={self.slots[slot].source}" for slot in self.slots)
        return f"[{index}/{total}] {self.target.name}: {parts}"

    @property
    def missing_slots(self) -> list[str]:
        return [key for key, outcome in self.slots.items() if not outcome.filled]


@dataclass
class RunSummary:
    """Totals for the final summary line."""

    results: list[TitleResult] = field(default_factory=list)
    stopped_early: bool = False
    stop_reason: str = ""

    @property
    def titles(self) -> int:
        return len(self.results)

    @property
    def unmatched(self) -> list[str]:
        return [r.target.name for r in self.results if not r.skipped and not r.match.found]

    @property
    def filled(self) -> int:
        return sum(1 for r in self.results for outcome in r.slots.values() if outcome.filled)

    @property
    def missing(self) -> int:
        return sum(1 for r in self.results for outcome in r.slots.values() if not outcome.filled)

    @property
    def written(self) -> int:
        """Slots this run actually put a file in (``kept`` does not count).

        ``sync`` uses it to decide whether Steam needs a restart at all: a
        run that changed nothing on disk must not bounce the client.
        """
        return sum(
            1
            for r in self.results
            for outcome in r.slots.values()
            if outcome.filled and outcome.source != SOURCE_KEPT
        )

    def lines(self) -> list[str]:
        lines = [
            f"{self.titles} title(s), {self.filled} art slot(s) filled, "
            f"{self.missing} still missing"
        ]
        unmatched = self.unmatched
        if unmatched:
            lines.append(f"no match for {len(unmatched)} title(s): " + ", ".join(sorted(unmatched)))
            lines.append(
                "that is an accepted outcome -- set those by hand in the Steam UI and "
                "this tool will never overwrite them"
            )
        if self.stopped_early:
            lines.append(self.stop_reason)
            lines.append("everything written so far is kept; rerun the same command to continue")
        return lines


def apply_title(
    target: ArtTarget,
    resolver: Resolver,
    selector: Selector,
    *,
    force: bool = False,
    explain: bool = False,
    slots: Sequence[Slot] = SLOTS,
) -> TitleResult:
    """Resolve one title and fill whichever of its slots still need filling."""
    chain: list[str] = []
    match = resolver.resolve(target.name)
    chain.extend(match.explain)
    result = TitleResult(target=target, match=match, explain=chain)

    if match.skipped:
        result.skipped = True
        return result

    cache = resolver.cache
    for slot in slots:
        existing = existing_slot_file(target.grid_dir, target.appid, slot)
        if existing is not None and not force:
            result.slots[slot.key] = SlotOutcome(slot.key, SOURCE_KEPT, path=existing)
            chain.append(f"{slot.key}: kept {existing.name} (already on disk)")
            continue

        if (
            not force
            and not resolver.retry_missing
            and cache.slot_is_known_missing(target.name, slot.key)
        ):
            result.slots[slot.key] = SlotOutcome(slot.key, SOURCE_CACHED_MISS)
            chain.append(f"{slot.key}: cached miss, not re-queried (--retry-missing to retry)")
            continue

        fill = selector.fill(
            slot,
            match,
            appid=target.appid,
            grid_dir=target.grid_dir,
            explain=chain if explain else None,
        )
        if fill is None:
            result.slots[slot.key] = SlotOutcome(slot.key, SOURCE_MISSING)
            if match.transient or selector.last_attempt_failed:
                # A lookup failed rather than "no source has this image": do
                # not start a 7-day negative window on a transient error.
                # ``match.transient`` covers the case where it was the *title*
                # search that failed -- the slot loop then finds nothing to
                # try, so no source reports a failure of its own, and the
                # cache must still be left alone (spec 6.10).
                chain.append(f"{slot.key}: lookup failed, not cached as missing")
            else:
                cache.record_missing_slot(target.name, slot.key)
                if not explain:
                    chain.append(f"{slot.key}: nothing found")
        else:
            result.slots[slot.key] = SlotOutcome(
                slot.key, fill.source, path=fill.path, url=fill.url
            )
            cache.clear_missing_slot(target.name, slot.key)

    # Spec 3.9 item 2: the cache is on disk before the next title starts.
    cache.flush()
    return result


def icon_path_for(target: ArtTarget, result: TitleResult) -> Path | None:
    """The absolute ``_icon`` path to write into the shortcut, if one exists."""
    outcome = result.slots.get("icon")
    if outcome is not None and outcome.path is not None:
        return outcome.path.resolve()
    existing = existing_slot_file(target.grid_dir, target.appid, ICON)
    return existing.resolve() if existing is not None else None


def run_art(
    targets: Sequence[ArtTarget],
    resolver: Resolver,
    selector: Selector,
    *,
    force: bool = False,
    explain: bool = False,
    provider: TargetProvider | None = None,
    out: TextIO | None = None,
    on_title: Callable[[TitleResult], None] | None = None,
) -> RunSummary:
    """Dress every target, streaming one line per title (spec 3.9 item 5).

    Never raises for a title that found nothing -- that is an accepted
    outcome (spec 7). A :class:`~moonlight_steam_sync.art.http.HardStop` or a
    ``KeyboardInterrupt`` ends the run early with everything so far kept, and
    is reported in the summary; callers turn that into exit code 4 or 130.

    Icon patches are *recorded* through ``provider.set_icon`` as each title
    completes; ``provider.commit()`` is the caller's call, after the summary
    and only when the run was not stopped early (spec 3.6 / 3.9 item 4).
    """
    summary = RunSummary()
    total = len(targets)
    for index, target in enumerate(targets, start=1):
        try:
            result = apply_title(target, resolver, selector, force=force, explain=explain)
        except HardStop as exc:
            summary.stopped_early = True
            summary.stop_reason = str(exc)
            break
        except KeyboardInterrupt:
            resolver.cache.flush()
            summary.stopped_early = True
            summary.stop_reason = "interrupted"
            break

        summary.results.append(result)
        if provider is not None and not result.skipped:
            icon = icon_path_for(target, result)
            if icon is not None:
                provider.set_icon(target.appid, icon)
        if out is not None:
            print(result.progress_line(index, total), file=out)
            if explain:
                for line in result.explain:
                    print(f"    {line}", file=out)
        if on_title is not None:
            on_title(result)

    resolver.cache.flush()
    return summary


def slot_report(grid_dir: Path, appid: int) -> dict[str, str]:
    """For ``status``: slot key -> the extension on disk, or ``-``."""
    report: dict[str, str] = {}
    for slot in SLOTS:
        existing = existing_slot_file(grid_dir, appid, slot)
        report[slot.key] = existing.suffix.lstrip(".") if existing is not None else "-"
    return report


__all__ = [
    "ArtTarget",
    "CommitResult",
    "RunSummary",
    "ShortcutsWriter",
    "SlotOutcome",
    "TargetProvider",
    "SteamShortcutProvider",
    "TargetsUnavailable",
    "TitleResult",
    "apply_title",
    "default_target_provider",
    "icon_path_for",
    "patch_icon",
    "run_art",
    "slot_report",
    "write_when_steam_is_down",
]
