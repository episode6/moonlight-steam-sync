"""``--commit {restart,await-exit,refuse}`` (decky spec 3.5, 3.13 A1/A2, PR-4).

The one ``shortcuts.vdf`` writer, :func:`sync.commit_shortcuts`, in its
three modes, driven through ``sync``, ``remove``, ``match`` and ``art``
against the e2e world from ``tests/test_sync_e2e.py`` (that file is frozen;
this one imports its fixtures). Steam is :class:`FakeRunner`, so "Steam
exits on its own" is a scripted poll count and the 100 ms polls advance a
fake clock rather than sleeping.

The ``restart`` and ``refuse`` modes are v0.2.0's ``restart_steam`` true /
false, already covered by the existing commit tests (which stay untouched);
here they are only checked to still be reachable through the flag.
"""

from __future__ import annotations

import io
import os
import signal

import pytest

from moonlight_steam_sync import sync
from moonlight_steam_sync.__main__ import build_parser, main
from moonlight_steam_sync.art.cli import build_services, cmd_art
from moonlight_steam_sync.shortcuts import ShortcutsFile
from moonlight_steam_sync.steam import SteamRunningError
from tests.art_fixtures import FakeTransport, make_fetcher
from tests.fakes import FakeRunner
from tests.test_match import (  # noqa: F401 - pytest fixtures
    HOLLOW_STEAM,
    grid_files_of,
    json_lines,
    run_match,
    shortcut,
    synced,
)
from tests.test_sync_e2e import (  # noqa: F401 - pytest fixtures
    Result,
    World,
    fresh_world,
    host_publishes,
    isolated_config,
    make_config,
    world,
)

TIMED_OUT = "steam did not exit; shortcuts.vdf not written"
WAITING = "waiting for Steam to exit (up to 60 s) before writing shortcuts.vdf"


class InterruptedRunner(FakeRunner):
    """A Steam that stays up, with the Ctrl-C landing in the wait's N-th
    sleep. Records the ``SIGINT`` disposition at every sleep so a test can
    see that the wait is interruptible (decky spec 3.13 A2)."""

    def __init__(self, *, interrupt_on_sleep: int = 1) -> None:
        super().__init__(running=True)
        self.interrupt_on_sleep = interrupt_on_sleep
        self.sleeps = 0
        self.handlers_seen: list[object] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.handlers_seen.append(signal.getsignal(signal.SIGINT))
        if self.sleeps == self.interrupt_on_sleep:
            raise KeyboardInterrupt
        super().sleep(seconds)


def run_art(world: World, argv: list[str], *, runner: FakeRunner, config=None) -> Result:
    """``art`` through the real Steam-aware provider, the way ``main()``
    builds it: the ``--commit`` flag reaches ``steam_aware_provider``."""
    transport = FakeTransport()
    config = config or world.config
    services = build_services(config, cache_path=world.cache_path, fetcher=make_fetcher(transport))
    args = build_parser().parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    code = cmd_art(
        args,
        config,
        provider_factory=lambda cfg: sync.steam_aware_provider(
            cfg, runner=runner, mode=getattr(args, "commit", None)
        ),
        services=services,
        out=out,
        err=err,
    )
    return Result(code, out.getvalue(), err.getvalue(), transport, runner)


def events_of(result: Result) -> list[dict]:
    return json_lines(result.out)


def event_names(result: Result) -> list[str]:
    return [e["event"] for e in events_of(result)]


def one(result: Result, name: str) -> dict:
    found = [e for e in events_of(result) if e["event"] == name]
    assert len(found) == 1, (name, event_names(result))
    return found[0]


# ---------------------------------------------------------------------------
# resolving the mode
# ---------------------------------------------------------------------------


def test_the_flag_wins_and_unset_follows_restart_steam(tmp_path):
    """Decky spec 3.4.8: unset reproduces today's behaviour, `restart` forces
    a restart over `restart_steam = false`, `await-exit` ignores the file."""
    yes, no = make_config(tmp_path), make_config(tmp_path, restart_steam=False)
    assert sync.resolve_commit_mode(None, yes) == "restart"
    assert sync.resolve_commit_mode(None, no) == "refuse"
    assert sync.resolve_commit_mode("restart", no) == "restart"
    assert sync.resolve_commit_mode("refuse", yes) == "refuse"
    assert sync.resolve_commit_mode("await-exit", no) == "await-exit"
    with pytest.raises(ValueError):
        sync.resolve_commit_mode("later", yes)


def test_the_flag_is_on_every_writing_command_and_excludes_no_restart_steam(capsys):
    parser = build_parser()
    for command, tail in (
        ("sync", []),
        ("remove", ["--all"]),
        ("art", []),
        ("match", ["X", "--none"]),
    ):
        args = parser.parse_args([command, *tail, "--commit", "await-exit"])
        assert args.commit == "await-exit"
        assert parser.parse_args([command, *tail]).commit is None
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["sync", "--commit", "restart", "--no-restart-steam"])
    assert excinfo.value.code == 2
    assert "not allowed with argument --commit" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        parser.parse_args(["sync", "--commit", "later"])


def test_commit_restart_forces_a_restart_over_restart_steam_false(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    config = make_config(tmp_path, restart_steam=False)
    result = world.run(["sync", "--commit", "restart"], config=config)
    assert result.code == 0, result.err
    assert result.runner.shutdowns == 1 and result.runner.relaunches == 1
    assert "Hades II™" in world.owned_names()


def test_commit_refuse_is_no_restart_steam(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    before = world.shortcuts_path.read_bytes()
    result = world.run(["sync", "--commit", "refuse"])
    assert result.code == 2
    assert result.err.startswith("sync: Steam is running and --commit refuse was given")
    assert "--commit await-exit" in result.err
    assert world.shortcuts_path.read_bytes() == before
    assert result.runner.shutdowns == 0 and result.runner.spawned == []


# ---------------------------------------------------------------------------
# await-exit: Steam running and something to write
# ---------------------------------------------------------------------------


def test_await_exit_writes_the_poll_after_steam_is_gone_and_never_touches_steam(
    world, tmp_path, monkeypatch
):
    """Decky spec 3.5 steps 2 and 4: emit `awaiting-steam-exit`, poll every
    100 ms, write the instant Steam is gone, never `-shutdown` or spawn."""
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    runner = FakeRunner(running=True, exits_after_polls=3)
    result = world.run(["--json", "sync", "--commit", "await-exit"], runner=runner)
    assert result.code == 0, result.err

    assert one(result, "awaiting-steam-exit") == {"event": "awaiting-steam-exit", "timeout_s": 60}
    names = event_names(result)
    assert names.index("awaiting-steam-exit") < names.index("commit") < names.index("summary")
    commit = one(result, "commit")
    assert commit["written"] is True and commit["restarted"] is False
    assert commit["backup"] == world.backups()[-1].name
    assert "Hades II™" in world.owned_names()

    # One check on entry, then the wait: two polls up, the third gone, then
    # the write -- and not a single sleep more than the polls needed.
    assert runner.polls == 4
    assert runner.slept == pytest.approx(0.2)
    assert runner.shutdowns == 0 and runner.spawned == []
    assert "Steam restarted" not in result.err
    assert WAITING in result.err.splitlines()  # the human line moved to stderr
    assert result.err.count("shutting down Steam") == 0


def test_await_exit_human_output_and_commit_awaited_exit(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    runner = FakeRunner(running=True, exits_after_polls=2)
    result = world.run(["sync", "--commit", "await-exit"], runner=runner)
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    assert WAITING in lines
    assert lines.index(WAITING) < lines.index(
        f"wrote {world.shortcuts_path}; backup {world.backups()[-1].name}"
    )
    assert "Steam restarted" not in result.out
    assert result.out.count("{") == 0


def test_commit_shortcuts_reports_awaited_exit(world, tmp_path):
    """`Commit.awaited_exit` is the mode's own mark: set only when the file
    was written after a wait, never on the immediate or no-op paths."""
    library = ShortcutsFile.read(world.shortcuts_path)
    library.append(sync.new_shortcut(world.config, "Hades II™"))
    runner = FakeRunner(running=True, exits_after_polls=1)
    seen: list[float] = []
    commit = sync.commit_shortcuts(
        library,
        config=world.config,
        runner=runner,
        out=io.StringIO(),
        mode="await-exit",
        on_awaiting=seen.append,
    )
    assert commit.awaited_exit is True and commit.written is True and commit.restarted is False
    assert seen == [60.0]
    assert runner.shutdowns == 0 and runner.spawned == []

    # Nothing to write: no wait, no hook, no mark.
    again = sync.commit_shortcuts(
        library, config=world.config, runner=runner, mode="await-exit", on_awaiting=seen.append
    )
    assert again.awaited_exit is False and again.unchanged is True
    assert seen == [60.0]


def test_the_wait_is_interruptible_but_the_write_is_not(world, tmp_path, monkeypatch):
    """Decky spec 3.13 A2: SIGINT is deferred only across the write itself."""
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    runner = FakeRunner(running=True, exits_after_polls=2)
    during_wait: list[object] = []
    during_write: list[object] = []
    original_sleep = FakeRunner.sleep
    real_write = ShortcutsFile.write

    def watched_sleep(self, seconds):
        during_wait.append(signal.getsignal(signal.SIGINT))
        original_sleep(self, seconds)

    def write_under_fire(self):
        during_write.append(signal.getsignal(signal.SIGINT))
        # Only safe because the handler is SIG_IGN, which the assertion
        # checks first: with it the kernel discards the signal outright.
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        os.kill(os.getpid(), signal.SIGINT)
        return real_write(self)

    monkeypatch.setattr(FakeRunner, "sleep", watched_sleep)
    monkeypatch.setattr(ShortcutsFile, "write", write_under_fire)
    previous = signal.getsignal(signal.SIGINT)
    result = world.run(["sync", "--commit", "await-exit"], runner=runner)
    assert result.code == 0, result.err
    assert during_wait and all(h is previous for h in during_wait)
    assert during_write == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGINT) is previous
    assert "Hades II™" in world.owned_names()
    assert ShortcutsFile.read(world.shortcuts_path)  # a complete, parseable file


# ---------------------------------------------------------------------------
# await-exit: Steam never exits, or the wait is interrupted
# ---------------------------------------------------------------------------


def test_steam_that_never_exits_is_exit_2_after_the_timeout_with_the_file_untouched(
    world, tmp_path, monkeypatch
):
    """Decky spec 3.5 step 5: exit 2, the §3.5 error text, art kept."""
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    monkeypatch.setattr(sync, "AWAIT_EXIT_TIMEOUT_S", 1.0)
    before = world.shortcuts_path.read_bytes()
    runner = FakeRunner(running=True)
    result = world.run(["--json", "sync", "--commit", "await-exit"], runner=runner)
    assert result.code == 2

    names = event_names(result)
    assert names[-2:] == ["summary", "error"]
    assert "commit" not in names
    assert one(result, "awaiting-steam-exit")["timeout_s"] == 1
    assert one(result, "error") == {"event": "error", "exit": 2, "message": f"sync: {TIMED_OUT}"}
    assert one(result, "summary")["exit"] == 2
    assert f"sync: {TIMED_OUT}" in result.err.splitlines()

    assert world.shortcuts_path.read_bytes() == before
    assert world.backups() == []
    assert runner.slept >= 1.0 and runner.slept < 1.2
    assert runner.shutdowns == 0 and runner.spawned == []
    hades = world.appid_for("Hades II™")
    assert len(grid_files_of(world, hades)) == 5

    # Nothing is lost: the next run only writes (decky spec 3.5 step 5).
    again = world.run(["sync", "--commit", "await-exit"], runner=FakeRunner(running=False))
    assert again.code == 0, again.err
    assert again.calls == []
    assert "Hades II™" in world.owned_names()


def test_sigint_during_the_wait_is_exit_130_at_once_with_the_file_untouched(
    world, tmp_path, monkeypatch
):
    """Decky spec 3.13 A2: `summary` with stop_reason interrupted and exit
    130, then `error`; no `commit`; nothing written; the rerun finishes."""
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    before = world.shortcuts_path.read_bytes()
    runner = InterruptedRunner(interrupt_on_sleep=2)
    result = world.run(["--json", "sync", "--commit", "await-exit"], runner=runner)
    assert result.code == 130

    names = event_names(result)
    assert names[-2:] == ["summary", "error"]
    assert "commit" not in names
    assert "awaiting-steam-exit" in names
    summary = one(result, "summary")
    assert summary["stop_reason"] == "interrupted" and summary["stopped_early"] is True
    assert summary["exit"] == 130
    assert summary["filled"] > 0  # the art phase had finished
    assert one(result, "error") == {"event": "error", "exit": 130, "message": sync.RESUME_HINT}

    assert world.shortcuts_path.read_bytes() == before
    assert world.backups() == []
    assert runner.sleeps == 2  # the second sleep was the Ctrl-C: nothing after it
    assert runner.shutdowns == 0 and runner.spawned == []
    assert all(h is not signal.SIG_IGN for h in runner.handlers_seen)

    again = world.run(["sync", "--commit", "await-exit"], runner=FakeRunner(running=False))
    assert again.code == 0, again.err
    assert again.calls == []
    assert "Hades II™" in world.owned_names()


# ---------------------------------------------------------------------------
# await-exit: nothing to wait for
# ---------------------------------------------------------------------------


def test_steam_not_running_writes_at_once_without_waiting(world, tmp_path, monkeypatch):
    """Decky spec 3.5 step 6."""
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    runner = FakeRunner(running=False)
    result = world.run(["--json", "sync", "--commit", "await-exit"], runner=runner)
    assert result.code == 0, result.err
    assert "awaiting-steam-exit" not in event_names(result)
    commit = one(result, "commit")
    assert commit["written"] is True and commit["restarted"] is False
    assert runner.slept == 0 and runner.polls == 1
    assert runner.spawned == []
    assert "Hades II™" in world.owned_names()


def test_an_art_only_run_emits_commit_written_false_and_does_not_wait(world, tmp_path, monkeypatch):
    """Decky spec 3.5 step 7: new grid files, no file change -> no wait, and
    the plugin decides whether that is worth a restart."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync"]).code == 0
    elden = shortcut(world, "Elden Ring")
    (world.grid_dir / f"{elden.appid}_hero.jpg").unlink()
    data = world.shortcuts_path.read_bytes()

    runner = FakeRunner(running=True)
    result = world.run(["--json", "sync", "--commit", "await-exit"], runner=runner)
    assert result.code == 0, result.err
    names = event_names(result)
    assert "awaiting-steam-exit" not in names
    assert one(result, "commit") == {
        "event": "commit",
        "written": False,
        "restarted": False,
        "backup": None,
        "relaunch_error": "",
    }
    assert result.calls == [
        "https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/library_hero.jpg"
    ]
    assert one(result, "note")["message"] == "restart Steam to see the new artwork"
    assert world.shortcuts_path.read_bytes() == data
    assert runner.slept == 0 and runner.polls == 1
    assert runner.shutdowns == 0 and runner.spawned == []


def test_a_finished_library_never_looks_at_steam(world, tmp_path, monkeypatch):
    """Nothing changed and no new art: `commit` says written:false and Steam
    is not even polled (the same as every other mode)."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync"]).code == 0
    runner = FakeRunner(running=True)
    result = world.run(["--json", "sync", "--commit", "await-exit"], runner=runner)
    assert result.code == 0, result.err
    assert result.calls == []
    assert "awaiting-steam-exit" not in event_names(result)
    assert one(result, "commit")["written"] is False
    assert runner.polls == 0 and runner.slept == 0


# ---------------------------------------------------------------------------
# remove and match
# ---------------------------------------------------------------------------


def test_remove_await_exit_waits_then_writes_and_deletes_the_art(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring", "Hollow Knight"])
    elden = shortcut(world, "Elden Ring")
    assert len(grid_files_of(world, elden.appid)) == 5
    runner = FakeRunner(running=True, exits_after_polls=2)

    result = world.run(["--json", "remove", "Elden Ring", "--commit", "await-exit"], runner=runner)
    assert result.code == 0, result.err
    assert event_names(result) == ["start", "awaiting-steam-exit", "commit", "summary"]
    assert one(result, "commit")["written"] is True
    assert one(result, "summary")["removed"] == 1
    assert "Elden Ring" not in world.owned_names()
    assert grid_files_of(world, elden.appid) == []
    assert runner.shutdowns == 0 and runner.spawned == []
    assert runner.slept == pytest.approx(0.1)


def test_remove_await_exit_timeout_leaves_the_entry_and_its_art(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    monkeypatch.setattr(sync, "AWAIT_EXIT_TIMEOUT_S", 0.5)
    elden = shortcut(world, "Elden Ring")
    before = world.shortcuts_path.read_bytes()
    result = world.run(["--json", "remove", "--all", "--commit", "await-exit"])
    assert result.code == 2
    assert event_names(result) == ["start", "awaiting-steam-exit", "error"]
    assert one(result, "error")["message"] == f"remove: {TIMED_OUT}"
    assert world.shortcuts_path.read_bytes() == before
    assert len(grid_files_of(world, elden.appid)) == 5


def test_remove_await_exit_interrupted_is_130_with_nothing_removed(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    elden = shortcut(world, "Elden Ring")
    before = world.shortcuts_path.read_bytes()
    result = world.run(
        ["--json", "remove", "--all", "--commit", "await-exit"], runner=InterruptedRunner()
    )
    assert result.code == 130
    assert event_names(result) == ["start", "awaiting-steam-exit", "summary", "error"]
    summary = one(result, "summary")
    assert summary["removed"] == 0
    # The general Ctrl-C rule (spec 3.4.6, decky spec 3.13 A2) holds for the
    # await-exit wait too: an interrupted run says so in its summary.
    assert summary["stopped_early"] is True
    assert summary["stop_reason"] == "interrupted"
    assert summary["exit"] == 130
    assert one(result, "error")["exit"] == 130
    assert world.shortcuts_path.read_bytes() == before
    assert len(grid_files_of(world, elden.appid)) == 5


def test_match_immediate_path_await_exit_waits_then_resets_the_art(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    elden = shortcut(world, "Elden Ring")
    assert elden.icon
    runner = FakeRunner(running=True, exits_after_polls=2)

    result = run_match(
        world,
        ["--json", "match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--commit", "await-exit"],
        runner=runner,
    )
    assert result.code == 0, result.err
    assert event_names(result) == ["start", "awaiting-steam-exit", "commit", "pinned"]
    commit = one(result, "commit")
    assert commit["written"] is True and commit["restarted"] is False
    assert shortcut(world, "Elden Ring").icon == ""
    assert grid_files_of(world, elden.appid) == []
    assert runner.shutdowns == 0 and runner.spawned == []
    assert WAITING in result.err.splitlines()


def test_match_immediate_path_await_exit_interrupted_pins_nothing(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    elden = shortcut(world, "Elden Ring")
    before = (world.shortcuts_path.read_bytes(), world.cache_path.read_bytes())
    result = run_match(
        world,
        ["--json", "match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--commit", "await-exit"],
        runner=InterruptedRunner(),
    )
    assert result.code == 130
    assert event_names(result) == ["start", "awaiting-steam-exit", "error"]
    assert one(result, "error")["message"] == f"match: {sync.RESUME_HINT}"
    assert (world.shortcuts_path.read_bytes(), world.cache_path.read_bytes()) == before
    assert len(grid_files_of(world, elden.appid)) == 5


# ---------------------------------------------------------------------------
# art --commit (decky spec 3.13 A1)
# ---------------------------------------------------------------------------


def test_art_await_exit_waits_only_for_an_icon_patch_that_changes_the_file(world):
    """First run: Elden Ring's icon field is empty, the patch changes the
    file, so `art` waits and writes. Second run with one slot refilled:
    art only, no wait, `commit` written:false."""
    runner = FakeRunner(running=True, exits_after_polls=2)
    result = run_art(
        world, ["--json", "art", "--only", "Elden Ring", "--commit", "await-exit"], runner=runner
    )
    assert result.code == 0, result.err
    names = event_names(result)
    assert names.index("awaiting-steam-exit") < names.index("commit")
    commit = one(result, "commit")
    assert commit["written"] is True and commit["restarted"] is False
    assert commit["backup"] == world.backups()[-1].name
    elden = shortcut(world, "Elden Ring")
    assert elden.icon.endswith(f"{elden.appid}_icon.jpg")
    assert runner.shutdowns == 0 and runner.spawned == []
    assert runner.slept == pytest.approx(0.1)
    assert WAITING in result.err.splitlines()

    (world.grid_dir / f"{elden.appid}_hero.jpg").unlink()
    data = world.shortcuts_path.read_bytes()
    runner = FakeRunner(running=True)
    again = run_art(
        world, ["--json", "art", "--only", "Elden Ring", "--commit", "await-exit"], runner=runner
    )
    assert again.code == 0, again.err
    assert "awaiting-steam-exit" not in event_names(again)
    assert one(again, "commit")["written"] is False
    assert again.calls == [
        "https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/library_hero.jpg"
    ]
    assert one(again, "note")["message"] == "restart Steam to see the new artwork"
    assert world.shortcuts_path.read_bytes() == data
    assert runner.polls == 0 and runner.slept == 0


def test_art_await_exit_timeout_and_interrupt_leave_the_file_untouched(world, monkeypatch):
    monkeypatch.setattr(sync, "AWAIT_EXIT_TIMEOUT_S", 0.3)
    before = world.shortcuts_path.read_bytes()

    result = run_art(
        world,
        ["--json", "art", "--only", "Elden Ring", "--commit", "await-exit"],
        runner=FakeRunner(running=True),
    )
    assert result.code == 2
    assert event_names(result)[-2:] == ["summary", "error"]
    assert one(result, "error") == {"event": "error", "exit": 2, "message": f"art: {TIMED_OUT}"}
    assert world.shortcuts_path.read_bytes() == before
    assert world.grid_files()  # the art is on disk regardless

    result = run_art(
        world,
        ["--json", "art", "--only", "Elden Ring", "--commit", "await-exit"],
        runner=InterruptedRunner(),
    )
    assert result.code == 130
    assert event_names(result) == ["start", "title", "awaiting-steam-exit", "summary", "error"]
    summary = one(result, "summary")
    # The art phase completed (all five slots filled) but the run was
    # interrupted during the wait, and the summary says so (spec 3.4.6,
    # decky spec 3.13 A2) while keeping the real art counts.
    assert summary["exit"] == 130
    assert summary["stopped_early"] is True
    assert summary["stop_reason"] == "interrupted"
    assert summary["filled"] == 5
    # Ctrl-C lands during the wait, never during the write: the error text
    # says what was interrupted and matches the other commands' hint.
    assert one(result, "error")["message"] == "interrupted; resume with the same command"
    assert world.shortcuts_path.read_bytes() == before


def test_art_commit_restart_and_refuse_through_the_flag(world, tmp_path):
    no_restart = make_config(tmp_path, restart_steam=False)
    before = world.shortcuts_path.read_bytes()
    runner = FakeRunner(running=True)
    refused = run_art(world, ["art", "--only", "Elden Ring"], runner=runner, config=no_restart)
    assert refused.code == 2 and "restart_steam is false" in refused.err
    assert world.shortcuts_path.read_bytes() == before

    runner = FakeRunner(running=True)
    forced = run_art(
        world,
        ["art", "--only", "Elden Ring", "--commit", "restart"],
        runner=runner,
        config=no_restart,
    )
    assert forced.code == 0, forced.err
    assert runner.shutdowns == 1 and runner.relaunches == 1
    assert shortcut(world, "Elden Ring").icon

    # Hollow Knight's fixture icon dangles, so the patch changes the file
    # and an explicit `refuse` refuses it (with its own wording: the user
    # asked for this, not restart_steam).
    data = world.shortcuts_path.read_bytes()
    runner = FakeRunner(running=True)
    refused = run_art(
        world, ["art", "--only", "Hollow Knight", "--commit", "refuse"], runner=runner
    )
    assert refused.code == 2
    assert refused.err.startswith("art: Steam is running and --commit refuse was given")
    assert world.shortcuts_path.read_bytes() == data
    assert runner.shutdowns == 0 and runner.spawned == []


def test_art_json_keeps_the_writers_human_lines_off_stdout(world):
    """The restart writer's "shutting down Steam" line goes where the art
    progress goes (stderr under --json), never onto the JSON stream."""
    result = run_art(
        world, ["--json", "art", "--only", "Elden Ring"], runner=FakeRunner(running=True)
    )
    assert result.code == 0, result.err
    assert "shutting down Steam (Ctrl-C is deferred until it is back up)" in result.err.splitlines()
    for line in result.out.splitlines():
        assert line.startswith("{"), line
    assert one(result, "commit")["restarted"] is True


def test_main_passes_art_commit_to_the_real_provider(world, isolated_config, capsys):
    runner = FakeRunner(running=True, exits_after_polls=2)
    code = main(
        ["art", "--only", "Elden Ring", "--commit", "await-exit"], deps=sync.Deps(runner=runner)
    )
    assert code == 0, capsys.readouterr().err
    assert runner.shutdowns == 0 and runner.spawned == []
    assert shortcut(world, "Elden Ring").icon
    assert WAITING in capsys.readouterr().out.splitlines()


def test_the_sync_options_carry_the_mode():
    args = build_parser().parse_args(["sync", "--commit", "await-exit"])
    assert sync.SyncOptions.from_args(args).commit == "await-exit"
    assert sync.SyncOptions.from_args(build_parser().parse_args(["sync"])).commit is None


def test_wait_for_exit_polls_at_the_interval_until_gone_or_the_deadline():
    from moonlight_steam_sync import steam

    runner = FakeRunner(running=True, exits_after_polls=3)
    assert steam.wait_for_exit(runner, timeout=60, poll_interval=0.1) is True
    assert runner.polls == 4 and runner.slept == pytest.approx(0.3)

    stubborn = FakeRunner(running=True)
    assert steam.wait_for_exit(stubborn, timeout=1.0, poll_interval=0.1) is False
    assert stubborn.slept == pytest.approx(1.0, abs=0.11)  # the fake clock's float drift
    assert stubborn.shutdowns == 0

    assert steam.wait_for_exit(FakeRunner(running=False), timeout=60, poll_interval=0.1) is True


def test_a_direct_timeout_raises_the_spec_text(world, monkeypatch):
    monkeypatch.setattr(sync, "AWAIT_EXIT_TIMEOUT_S", 0.2)
    before = world.shortcuts_path.read_bytes()
    library = ShortcutsFile.read(world.shortcuts_path)
    library.append(sync.new_shortcut(world.config, "Hades II™"))
    with pytest.raises(SteamRunningError, match=f"^{TIMED_OUT}$"):
        sync.commit_shortcuts(
            library,
            config=world.config,
            runner=FakeRunner(running=True),
            out=io.StringIO(),
            mode="await-exit",
        )
    assert world.shortcuts_path.read_bytes() == before
