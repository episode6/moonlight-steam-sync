"""Artwork resolution, selection and application (spec 3.5).

The package is deliberately layered so every step is testable without a
network and every durable step is visible on disk (spec 3.9):

``http``
    urllib transport seam, pacing, backoff, the 429 hard stop.
``sgdb`` / ``steamstore``
    thin API clients over that transport.
``resolve``
    Moonlight title -> :class:`~moonlight_steam_sync.art.resolve.Match`, plus
    the on-disk match cache that is flushed after every title.
``select``
    per-slot source order, download to ``<file>.part``, magic-byte sniff,
    rename into place.
``apply``
    the per-shortcut loop: skip existing slots, write grid files, hand the
    ``_icon`` path back to the shortcut layer.
``cli``
    the ``art`` and ``status`` subcommands.
"""
