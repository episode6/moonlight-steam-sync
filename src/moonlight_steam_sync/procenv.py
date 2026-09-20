"""Undo a frozen parent's ``LD_LIBRARY_PATH`` before it breaks this process.

A PyInstaller-frozen program (Decky Loader's ``PluginLoader`` is one) exports
``LD_LIBRARY_PATH=<its unpack dir, /tmp/_MEIxxxxxx>`` so *its own* bundled
libraries load, and every child inherits it. Those bundled libraries (an
older ``libssl``/``libcrypto`` among them) then shadow the system's for the
child too: ``flatpak`` dies in the dynamic linker ("version `OPENSSL_3.4.0'
not found"), which used to read as "moonlight CLI not found", and this
process's own ``import ssl`` fails the same way, which used to read as five
consecutive network failures. Found on the first device run of the Decky
plugin (2026-09-20).

The dynamic linker reads ``LD_LIBRARY_PATH`` once, at process start, so
fixing ``os.environ`` is only enough for children; for this process the one
cure is to ``exec`` itself again under the repaired environment
(:func:`reexec_if_needed`, called first thing by ``__main__.main``). With no
PyInstaller trace in the environment nothing here does anything.

Imports nothing from the package, so anything can import it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

#: PyInstaller's bootloader saves the value it replaced here -- but only when
#: there was one; under a systemd service there is not, hence the second rule.
ORIG_VAR = "LD_LIBRARY_PATH_ORIG"
#: PyInstaller's one-file unpack directory is always named ``_MEI<random>``.
MEI_PREFIX = "_MEI"


def _is_bundle_dir(entry: str) -> bool:
    return os.path.basename(entry.rstrip(os.sep)).startswith(MEI_PREFIX)


def repaired_env(environ: Mapping[str, str]) -> dict[str, str] | None:
    """``environ`` with a frozen parent's library path undone, or ``None``
    when there is nothing to undo (the overwhelmingly common case).

    ``LD_LIBRARY_PATH_ORIG`` set: put that value back. Otherwise drop every
    ``_MEI*`` entry from ``LD_LIBRARY_PATH`` and keep the rest; an empty
    result removes the variable.
    """
    current = environ.get("LD_LIBRARY_PATH")
    if ORIG_VAR in environ:
        env = dict(environ)
        restored = env.pop(ORIG_VAR)
        if restored:
            env["LD_LIBRARY_PATH"] = restored
        else:
            env.pop("LD_LIBRARY_PATH", None)
        return env
    if not current:
        return None
    entries = current.split(os.pathsep)
    kept = [entry for entry in entries if not _is_bundle_dir(entry)]
    if len(kept) == len(entries):
        return None
    env = dict(environ)
    if any(kept):
        env["LD_LIBRARY_PATH"] = os.pathsep.join(kept)
    else:
        del env["LD_LIBRARY_PATH"]
    return env


def reexec_if_needed() -> None:
    """Replace this process with itself under :func:`repaired_env`.

    Same pid, same argv (``sys.orig_argv``, so ``-m`` and the zipapp path
    both survive), so a caller's signals and pipes are unaffected. The
    repaired environment no longer triggers the rule, so this runs at most
    once. If the exec itself fails, carry on: children still get the
    repaired ``os.environ``, which is the part a running process can fix.
    """
    env = repaired_env(os.environ)
    if env is None:
        return
    os.environ.clear()
    os.environ.update(env)
    orig_argv = getattr(sys, "orig_argv", None)
    if not sys.executable or not orig_argv:
        return
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(sys.executable, [sys.executable, *orig_argv[1:]], env)
    except OSError:
        return
