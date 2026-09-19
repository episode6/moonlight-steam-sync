"""The ``--json`` event stream: :class:`Reporter` and its shared helpers
(spec 3.4.6).

Split out of :mod:`moonlight_steam_sync.sync` so that both ``sync`` and
:mod:`moonlight_steam_sync.art.cli` (which ``sync`` itself imports, so it
cannot import ``sync`` back) can share one implementation instead of two
copies drifting apart.
"""

from __future__ import annotations

import json
from typing import Any, TextIO


class Reporter:
    """Routes every human/machine line for one command (spec 3.4.6).

    ``json=False`` (the default, and every call site before this PR):
    :meth:`line` prints to ``out``, human formatting byte-identical to
    today, and :meth:`event` does nothing -- existing tests never see a
    change. ``json=True``: ``out`` carries one JSON object per line and
    nothing else, :meth:`line` moves the human text to ``err``, and
    :meth:`event` prints the JSON. :meth:`start` and :meth:`error` are
    unconditional (they also print/emit when ``json`` is false).
    """

    def __init__(
        self,
        out: TextIO,
        err: TextIO,
        *,
        json: bool = False,
        command: str = "",
        version: str = "",
    ) -> None:
        self.out = out
        self.err = err
        self.json = json
        self.command = command
        self.version = version

    def start(self) -> None:
        self.event("start", schema=1, version=self.version, command=self.command)

    def line(self, text: str) -> None:
        """A human progress line: ``out`` normally, ``err`` under ``--json``."""
        print(text, file=self.err if self.json else self.out)

    def note(self, message: str, *, only_json: bool = False) -> None:
        """A ``note:`` diagnostic -- on ``err``, plus a ``note`` event under
        ``--json``. These were already ``err``-only before this PR, *except*
        a note whose message is new in this PR (nothing in v0.2.0 could ever
        print it): pass ``only_json=True`` for one of those, so it appears
        only under ``--json`` and the human/non-json byte-identity invariant
        (spec 3.11) holds."""
        if only_json and not self.json:
            return
        print(f"note: {message}", file=self.err)
        self.event("note", message=message)

    def error(self, message: str, exit_code: int) -> None:
        """The human error text -- always on ``err`` -- plus an ``error`` event."""
        print(message, file=self.err)
        self.event("error", exit=exit_code, message=message)

    def event(self, event_name: str, **fields: Any) -> None:
        if not self.json:
            return
        print(json.dumps({"event": event_name, **fields}, sort_keys=True), file=self.out)

    def plan(self, **fields: Any) -> None:
        """The ``plan`` event (spec 3.4.6). A thin alias for
        ``event("plan", **fields)``, kept as its own method because the
        spec names ``sync.Reporter``'s methods as ``plan()``/``title()``/
        ``line()``/``event()``; the field-building itself stays in the
        caller (``sync._plan_event_fields``), since :class:`Reporter` has
        no access to ``Plan``/``RunSummary`` without importing back into
        ``sync`` (see the module docstring)."""
        self.event("plan", **fields)

    def title(self, **fields: Any) -> None:
        """The ``title`` event (spec 3.4.6), emitted per title by ``sync``
        and ``art``. See :meth:`plan` for why the field builders stay in
        the caller."""
        self.event("title", **fields)


def match_json(match: Any) -> dict[str, Any] | None:
    """``{steam_appid, sgdb_id, matched_name, how}``, or ``None`` (spec 3.4.6).

    ``match`` is a :class:`~moonlight_steam_sync.art.resolve.Match`, or
    ``None`` when the title has no cache entry. The ``unpinned``
    placeholder ``match --unpin --defer-art`` leaves (decky spec 3.4.4) is
    reported as ``None`` too: it carries ``stale_art`` and nothing else, and
    on the wire "not in ``matches.json``" is exactly what ``--unpin``
    promises until the next run re-resolves the title.
    """
    if match is None or getattr(match, "unpinned", False):
        return None
    return {
        "steam_appid": match.steam_appid,
        "sgdb_id": match.sgdb_id,
        "matched_name": match.matched_name,
        "how": match.how,
    }


#: ``title.slots`` / ``entry.slots`` vocabulary (spec 3.4.6): the human
#: progress line keeps printing ``official``/``community``, JSON maps those
#: two source names onto ``steam``/``sgdb``.
_SLOT_JSON_SOURCE = {
    "official": "steam",
    "community": "sgdb",
}


def slot_json_value(source: str) -> str:
    return _SLOT_JSON_SOURCE.get(source, source)


__all__ = ["Reporter", "match_json", "slot_json_value"]
