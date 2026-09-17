"""Write the grid files for one shortcut, and the loop over all of them.

This is where the artwork phase meets the Steam shortcut layer, and it meets
it through **one narrow seam** so the two can be built independently:

.. code-block:: text

    ArtTarget(name, appid, grid_dir, boxart_path)   what the art phase needs
    TargetProvider.targets()                        where those come from
    TargetProvider.set_icon(appid, icon_path)       the one thing art writes back
    TargetProvider.commit()                         flush those icon patches

``appid`` is the shortcut's own 32-bit id (``crc32(Exe + AppName) |
0x80000000``, spec 2.1), which is knowable *before* the shortcut exists, and
``grid_dir`` is ``userdata/<steamid3>/config/grid``. Nothing in this package
parses ``shortcuts.vdf`` or discovers the Steam root: the shortcuts/steam
modules (PR-2) supply the provider, and the ``sync`` orchestration (PR-5)
supplies the Moonlight box art paths.

Implementations of ``set_icon`` must **not** write ``shortcuts.vdf`` on the
spot: spec 3.6 wants exactly one atomic vdf write per run, after the whole
art phase, which is what ``commit()`` is for.

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

from moonlight_steam_sync.art.http import HardStop
from moonlight_steam_sync.art.resolve import Match, Resolver
from moonlight_steam_sync.art.select import ICON, SLOTS, Selector, Slot, existing_slot_file

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
        boxart_path: the Moonlight box art PNG from ``list --csv``, when the
            host cached one; the last-resort source for the portrait slot.
    """

    name: str
    appid: int
    grid_dir: Path
    boxart_path: Path | None = None


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
    """No shortcut layer is wired up yet (see :func:`default_target_provider`)."""


def default_target_provider() -> TargetProvider:
    """The provider used when nothing else is injected.

    PR-4 ships the artwork phase on its own; reading ``shortcuts.vdf`` to
    discover owned shortcuts is PR-2's module, and assembling the two is
    PR-5's ``sync``. Until then ``art`` and ``status`` fail with a clear
    message rather than pretending they have a library.
    """
    raise TargetsUnavailable(
        "the shortcut lookup (shortcuts.vdf -> appid + grid dir) is not wired up yet; "
        "it lands with the Steam-side modules"
    )


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
        return sum(
            1 for r in self.results for outcome in r.slots.values() if outcome.filled
        )

    @property
    def missing(self) -> int:
        return sum(
            1 for r in self.results for outcome in r.slots.values() if not outcome.filled
        )

    def lines(self) -> list[str]:
        lines = [
            f"{self.titles} title(s), {self.filled} art slot(s) filled, "
            f"{self.missing} still missing"
        ]
        unmatched = self.unmatched
        if unmatched:
            lines.append(
                f"no match for {len(unmatched)} title(s): " + ", ".join(sorted(unmatched))
            )
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
            boxart_path=target.boxart_path,
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
    """
    summary = RunSummary()
    total = len(targets)
    for index, target in enumerate(targets, start=1):
        try:
            result = apply_title(
                target, resolver, selector, force=force, explain=explain
            )
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
    if provider is not None:
        provider.commit()
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
    "RunSummary",
    "SlotOutcome",
    "TargetProvider",
    "TargetsUnavailable",
    "TitleResult",
    "apply_title",
    "default_target_provider",
    "icon_path_for",
    "run_art",
    "slot_report",
]
