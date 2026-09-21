"""moonlight-steam-sync: sync a Moonlight host's game list into Steam shortcuts."""

from importlib import metadata

__version__ = "0.4.0"


def version() -> str:
    """The version string ``--version`` and every ``--json`` ``start`` event print.

    See ``__main__._version()`` (a thin wrapper kept for the existing tests
    that patch ``__main__.metadata``) for why this prefers installed package
    metadata over the literal :data:`__version__`.
    """
    try:
        return metadata.version("moonlight-steam-sync")
    except metadata.PackageNotFoundError:
        return __version__
