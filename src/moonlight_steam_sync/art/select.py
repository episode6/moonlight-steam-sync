"""Step B of spec 3.5: per slot, the first source that yields an image wins.

Sources per slot, in order (1. official, 2. community, 3. host):

* portrait ``<id>p`` -- CDN ``library_600x900_2x.jpg`` then
  ``library_600x900.jpg``; SGDB ``grids`` 600x900 ``alternate``; the
  Moonlight box art from ``list --csv``.
* landscape ``<id>`` -- CDN ``header.jpg``; SGDB ``grids`` 920x430 then
  460x215.
* hero ``<id>_hero`` -- CDN ``library_hero.jpg``; SGDB ``heroes`` 1920x620
  then 3840x1240.
* logo ``<id>_logo`` -- CDN ``logo.png``; SGDB ``logos`` ``official`` then
  ``white``.
* icon ``<id>_icon`` -- the ``GetApps`` hash then the community CDN; SGDB
  ``icons`` ``official`` then ``custom``.

Sources are evaluated lazily, so a slot that is filled by the CDN never costs
a SteamGridDB call.

Two rules from spec 3.9 live here:

* **Downloads are atomic.** Bytes go to ``<appid><suffix>.part``; only after
  the magic bytes say PNG or JPEG is the file renamed to its real extension.
  An exception mid-download leaves the ``.part`` and nothing else, so a
  killed run never leaves a truncated image that would count as done.
* **A slot whose file already exists is skipped** (spec 6.9). That is both
  the resume mechanism and the "never clobber hand-picked art" rule, since
  the Steam UI writes these very filenames. When ``--force`` does refill a
  slot, whatever was there is removed even if the new image has a different
  extension -- a slot holds one file, never a ``.png`` and a ``.jpg`` racing
  each other.

WebP is never requested (:mod:`moonlight_steam_sync.art.sgdb` enforces the
query filters) and never written (the sniffer only accepts PNG and JPEG).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from moonlight_steam_sync.art import steamstore
from moonlight_steam_sync.art.http import Fetcher, HardStop, HttpError
from moonlight_steam_sync.art.resolve import Match
from moonlight_steam_sync.art.sgdb import PNG_ONLY_MIMES, SgdbClient
from moonlight_steam_sync.art.steamstore import SteamStoreClient


@dataclass(frozen=True)
class Slot:
    """One artwork slot: its key and the filename suffix Steam expects."""

    key: str
    suffix: str


PORTRAIT = Slot("portrait", "p")
LANDSCAPE = Slot("landscape", "")
HERO = Slot("hero", "_hero")
LOGO = Slot("logo", "_logo")
ICON = Slot("icon", "_icon")

#: Every slot, in the order they are filled and reported.
SLOTS: tuple[Slot, ...] = (PORTRAIT, LANDSCAPE, HERO, LOGO, ICON)
SLOTS_BY_KEY = {slot.key: slot for slot in SLOTS}

#: Extensions that count as "this slot already has a file" (spec 6.9). No
#: ``.webp``: Steam cannot read it, so one would not count as art anyway.
ART_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".ico")

#: ``kind`` + query params per community query, in the order spec 3.5 tries them.
COMMUNITY_QUERIES: dict[str, tuple[tuple[str, dict[str, str]], ...]] = {
    "portrait": (("grids", {"dimensions": "600x900", "styles": "alternate"}),),
    "landscape": (
        ("grids", {"dimensions": "920x430", "styles": "alternate"}),
        ("grids", {"dimensions": "460x215", "styles": "alternate"}),
    ),
    "hero": (
        ("heroes", {"dimensions": "1920x620", "styles": "alternate"}),
        ("heroes", {"dimensions": "3840x1240", "styles": "alternate"}),
    ),
    "logo": (("logos", {"styles": "official"}), ("logos", {"styles": "white"})),
    "icon": (
        ("icons", {"styles": "official", "mimes": PNG_ONLY_MIMES}),
        ("icons", {"styles": "custom", "mimes": PNG_ONLY_MIMES}),
    ),
}

_OFFICIAL_URLS = {
    "portrait": steamstore.portrait_urls,
    "landscape": steamstore.landscape_urls,
    "hero": steamstore.hero_urls,
    "logo": steamstore.logo_urls,
}

_SNIFF_BYTES = 16
_COPY_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Candidate:
    """One thing worth trying for a slot."""

    source: str  # "official" | "community" | "host"
    url: str | None = None
    path: Path | None = None
    detail: str = ""

    def describe(self) -> str:
        where = self.url or (str(self.path) if self.path else "?")
        return f"{self.source}: {where}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class Fill:
    """A slot that got filled: where the bytes came from and where they landed."""

    slot: str
    source: str
    path: Path
    url: str | None = None


def slot_basename(appid: int, slot: Slot) -> str:
    """``<appid><suffix>``: the grid filename without its extension."""
    return f"{appid}{slot.suffix}"


def existing_slot_file(grid_dir: Path, appid: int, slot: Slot) -> Path | None:
    """The file already filling this slot, if any (spec 6.9)."""
    base = grid_dir / slot_basename(appid, slot)
    for extension in ART_EXTENSIONS:
        candidate = base.with_name(base.name + extension)
        if candidate.is_file():
            return candidate
    return None


def part_path(grid_dir: Path, appid: int, slot: Slot) -> Path:
    """The ``.part`` a download for this slot writes to."""
    return grid_dir / f"{slot_basename(appid, slot)}.part"


def sniff_extension(header: bytes) -> str | None:
    """PNG or JPEG magic bytes -> the extension to use; anything else -> ``None``.

    This is the gate that keeps WebP (``RIFF....WEBP``) and HTML error pages
    out of ``grid/`` even if a source hands us one.
    """
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8"):
        return ".jpg"
    return None


@dataclass
class Selector:
    """Builds the candidate list for a slot and downloads the winner."""

    fetcher: Fetcher
    sgdb: SgdbClient | None = None
    store: SteamStoreClient | None = None
    #: ``[steamgriddb].community_fallback``; False = official art only.
    community_fallback: bool = True
    #: URLs whose download was attempted, in order (tests and ``--explain``).
    attempted: list[str] = field(default_factory=list, repr=False)
    #: Set by :meth:`fill`: True when the slot came up empty *because a
    #: lookup failed*, rather than because no source has that image. Callers
    #: use it to avoid caching a negative result they are not sure about.
    last_attempt_failed: bool = field(default=False, repr=False)

    # -- candidates ------------------------------------------------------

    def candidates(
        self, slot: Slot, match: Match, boxart_path: Path | None = None
    ) -> Iterator[Candidate]:
        """Yield sources for ``slot`` in spec 3.5 order, lazily."""
        yield from self._official(slot, match)
        yield from self._community(slot, match)
        if slot.key == "portrait" and boxart_path is not None and Path(boxart_path).is_file():
            yield Candidate("host", path=Path(boxart_path), detail="moonlight box art")

    def _official(self, slot: Slot, match: Match) -> Iterator[Candidate]:
        appid = match.steam_appid
        if appid is None:
            return
        if slot.key == "icon":
            if self.store is None:
                return
            try:
                icon_hash = self.store.app_icon_hash(appid)
            except HardStop:
                raise
            except HttpError:
                self.last_attempt_failed = True
                return
            if icon_hash:
                yield Candidate(
                    "official",
                    url=steamstore.icon_url(appid, icon_hash),
                    detail=f"GetApps hash {icon_hash}",
                )
            return
        builder = _OFFICIAL_URLS.get(slot.key)
        if builder is None:
            return
        for url in builder(appid):
            yield Candidate("official", url=url, detail=f"steam appid {appid}")

    def _community(self, slot: Slot, match: Match) -> Iterator[Candidate]:
        if not self.community_fallback:
            return
        sgdb_id = match.sgdb_id
        if sgdb_id is None or self.sgdb is None or not self.sgdb.enabled:
            return
        for kind, params in COMMUNITY_QUERIES.get(slot.key, ()):
            try:
                assets = self.sgdb.assets(kind, sgdb_id, **params)
            except HardStop:
                raise
            except HttpError:
                self.last_attempt_failed = True
                continue
            # Spec 3.5: sort by score descending (the client does that), then
            # take the first.
            best = _first_usable_url(assets)
            if best is None:
                continue
            url, detail = best
            yield Candidate("community", url=url, detail=f"{kind} {_params_label(params)} {detail}")

    # -- fetching --------------------------------------------------------

    def fill(
        self,
        slot: Slot,
        match: Match,
        *,
        appid: int,
        grid_dir: Path,
        boxart_path: Path | None = None,
        explain: list[str] | None = None,
    ) -> Fill | None:
        """Fill ``slot`` for the shortcut ``appid`` under ``grid_dir``.

        ``appid`` is the *shortcut's* 32-bit id (spec 2.1), which is what the
        grid filenames use -- never the Steam store appid the art came from.
        """
        dest_base = grid_dir / slot_basename(appid, slot)
        self.last_attempt_failed = False
        for candidate in self.candidates(slot, match, boxart_path):
            if explain is not None:
                explain.append(f"{slot.key}: try {candidate.describe()}")
            try:
                path = self._materialise(candidate, dest_base)
            except HardStop:
                raise
            except HttpError as exc:
                self.last_attempt_failed = True
                if explain is not None:
                    explain.append(f"{slot.key}: {exc}")
                continue
            if path is not None:
                if explain is not None:
                    explain.append(f"{slot.key}: wrote {path.name} from {candidate.source}")
                return Fill(slot=slot.key, source=candidate.source, path=path, url=candidate.url)
        return None

    def _materialise(self, candidate: Candidate, dest_base: Path) -> Path | None:
        if candidate.path is not None:
            return copy_local(candidate.path, dest_base)
        if candidate.url is None:
            return None
        self.attempted.append(candidate.url)
        return self.download(candidate.url, dest_base)

    def download(self, url: str, dest_base: Path) -> Path | None:
        """Download ``url`` to ``<dest_base>.part``, sniff, rename into place.

        Returns the final path, or ``None`` when the source had nothing (a
        404, an empty body, or bytes that are not PNG/JPEG). A connection
        that dies mid-body raises
        :class:`~moonlight_steam_sync.art.http.NetworkError` with the
        ``.part`` left behind; :meth:`fill` treats that like any other failed
        attempt and moves to the next source.
        """
        response = self.fetcher.open(url, accept="image/png,image/jpeg")
        if response.status != 200:
            response.close()
            return None
        part = Path(str(dest_base) + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        header = bytearray()
        with part.open("wb") as handle:
            for chunk in response.chunks:
                if len(header) < _SNIFF_BYTES:
                    header.extend(chunk[: _SNIFF_BYTES - len(header)])
                handle.write(chunk)
        return _finish(part, dest_base, bytes(header))


def copy_local(source: Path, dest_base: Path) -> Path | None:
    """Same ``.part``-then-rename dance for a local file (host box art)."""
    part = Path(str(dest_base) + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray()
    try:
        with Path(source).open("rb") as reader, part.open("wb") as handle:
            while True:
                chunk = reader.read(_COPY_CHUNK)
                if not chunk:
                    break
                if len(header) < _SNIFF_BYTES:
                    header.extend(chunk[: _SNIFF_BYTES - len(header)])
                handle.write(chunk)
    except OSError:
        return None
    return _finish(part, dest_base, bytes(header))


def _finish(part: Path, dest_base: Path, header: bytes) -> Path | None:
    extension = sniff_extension(header)
    if extension is None:
        # Not an image we can hand Steam (WebP, HTML error page, empty body):
        # drop the .part rather than leave litter that looks resumable.
        part.unlink(missing_ok=True)
        return None
    final = Path(str(dest_base) + extension)
    os.replace(part, final)
    _drop_other_extensions(dest_base, final)
    return final


def _drop_other_extensions(dest_base: Path, final: Path) -> None:
    """A slot holds exactly one file, whatever extension it arrives with.

    Only ``--force`` ever reaches here with something already in the slot (a
    filled slot is skipped on every other run), and leaving the old file
    behind would undo the ``--force``: Steam's choice between
    ``<base>.png`` and ``<base>.jpg`` is undefined, ``status`` reports
    whichever :data:`ART_EXTENSIONS` lists first, and the next non-force run
    would call the stale file "kept" and patch the shortcut's ``icon`` field
    back to it (spec 6.9, 3.5, 3.6).
    """
    for extension in ART_EXTENSIONS:
        stale = Path(str(dest_base) + extension)
        if stale != final:
            stale.unlink(missing_ok=True)


def _first_usable_url(assets: Sequence[dict]) -> tuple[str, str] | None:
    for asset in assets:
        url = asset.get("url")
        if isinstance(url, str) and url and not url.lower().endswith(".webp"):
            style = asset.get("style", "?")
            score = asset.get("score", "?")
            return url, f"style={style} score={score}"
    return None


def _params_label(params: dict[str, str]) -> str:
    return " ".join(f"{k}={v}" for k, v in params.items())
