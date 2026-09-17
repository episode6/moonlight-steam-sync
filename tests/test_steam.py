"""Steam install discovery, user pick and process control (spec 3.4 / 3.6).

Nothing here touches a real Steam install: directory trees are built under
``tmp_path`` and every subprocess call goes through a fake
:class:`~moonlight_steam_sync.steam.ProcessRunner`.
"""

from __future__ import annotations

import subprocess

import pytest

from moonlight_steam_sync import steam
from moonlight_steam_sync.steam import (
    STEAM64_OFFSET,
    ProcessRunner,
    SteamNotFoundError,
    SteamUser,
    SteamUserNotFoundError,
    find_grid_file,
    find_steam_root,
    grid_stem,
    is_running,
    parse_text_vdf,
    pick_user,
    relaunch,
    shutdown,
    steamid3_from_steam64,
)
from tests.fakes import FakeRunner

MOST_RECENT_STEAM64 = 76561197960500000
OTHER_STEAM64 = 76561197960400000


def make_steam_tree(tmp_path, *, users=(), loginusers: str | None = None):
    root = tmp_path / ".local" / "share" / "Steam"
    (root / "config").mkdir(parents=True)
    for steamid3 in users:
        (root / "userdata" / str(steamid3) / "config" / "grid").mkdir(parents=True)
    if loginusers is not None:
        (root / "config" / "loginusers.vdf").write_text(loginusers, encoding="utf-8")
    return root


def test_parse_loginusers_fixture(loginusers_text):
    parsed = parse_text_vdf(loginusers_text)
    users = parsed["users"]
    assert set(users) == {str(OTHER_STEAM64), str(MOST_RECENT_STEAM64)}
    assert users[str(MOST_RECENT_STEAM64)]["MostRecent"] == "1"
    assert users[str(OTHER_STEAM64)]["MostRecent"] == "0"
    # Escapes inside a quoted token survive.
    assert users[str(MOST_RECENT_STEAM64)]["PersonaName"] == 'Deck "Main" Account'


def test_parse_text_vdf_handles_comments_and_bare_tokens():
    parsed = parse_text_vdf('// leading comment\nroot\n{\n\t"a" "1" // trailing\n\tb c\n}\n')
    assert parsed == {"root": {"a": "1", "b": "c"}}


@pytest.mark.parametrize(
    "text",
    ['"a" {', '"a" "b" }', '{ "a" "b" }', '"unterminated', '"a"'],
)
def test_malformed_text_vdf_raises(text):
    with pytest.raises(ValueError):
        parse_text_vdf(text)


# --- steamid arithmetic ----------------------------------------------------


def test_steamid3_from_steam64():
    assert steamid3_from_steam64(MOST_RECENT_STEAM64) == MOST_RECENT_STEAM64 - STEAM64_OFFSET
    assert steamid3_from_steam64(str(MOST_RECENT_STEAM64)) == 234272
    with pytest.raises(ValueError):
        steamid3_from_steam64(42)


def test_steam_user_paths(tmp_path):
    user = SteamUser(steamid3=234272, path=tmp_path / "userdata" / "234272")
    assert user.steam64 == 234272 + STEAM64_OFFSET
    assert user.shortcuts_path.parts[-3:] == ("234272", "config", "shortcuts.vdf")
    assert user.grid_dir.parts[-3:] == ("234272", "config", "grid")


# --- root discovery --------------------------------------------------------


def test_find_steam_root_prefers_the_canonical_location(tmp_path):
    root = make_steam_tree(tmp_path)
    (tmp_path / ".steam").mkdir()
    (tmp_path / ".steam" / "steam").symlink_to(root)
    assert find_steam_root(env={}, home=tmp_path) == root


def test_find_steam_root_falls_back_to_dot_steam(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".steam").mkdir()
    (tmp_path / ".steam" / "steam").symlink_to(elsewhere)
    assert find_steam_root(env={}, home=tmp_path) == tmp_path / ".steam" / "steam"


def test_steam_root_env_override_wins(tmp_path):
    make_steam_tree(tmp_path)
    override = tmp_path / "override"
    override.mkdir()
    assert find_steam_root(env={"STEAM_ROOT": str(override)}, home=tmp_path) == override


def test_steam_root_env_override_must_exist(tmp_path):
    with pytest.raises(SteamNotFoundError, match="not a directory"):
        find_steam_root(env={"STEAM_ROOT": str(tmp_path / "nope")}, home=tmp_path)


def test_find_steam_root_reports_where_it_looked(tmp_path):
    with pytest.raises(SteamNotFoundError, match="STEAM_ROOT"):
        find_steam_root(env={}, home=tmp_path)


def test_find_steam_root_uses_home_from_the_environment(tmp_path):
    root = make_steam_tree(tmp_path)
    assert find_steam_root(env={"HOME": str(tmp_path)}) == root


# --- user pick -------------------------------------------------------------


def test_single_user_needs_no_loginusers(tmp_path):
    root = make_steam_tree(tmp_path, users=[234272])
    user = pick_user(root)
    assert user.steamid3 == 234272
    assert user.shortcuts_path == root / "userdata" / "234272" / "config" / "shortcuts.vdf"


def test_most_recent_decides_between_several_users(tmp_path, loginusers_text):
    root = make_steam_tree(
        tmp_path,
        users=[
            steamid3_from_steam64(OTHER_STEAM64),
            steamid3_from_steam64(MOST_RECENT_STEAM64),
        ],
        loginusers=loginusers_text,
    )
    user = pick_user(root)
    assert user.steamid3 == steamid3_from_steam64(MOST_RECENT_STEAM64)
    assert user.account_name == "deck"
    assert user.most_recent is True


def test_several_users_and_no_loginusers_refuses_to_guess(tmp_path):
    root = make_steam_tree(tmp_path, users=[111, 222])
    with pytest.raises(SteamUserNotFoundError, match="111, 222"):
        pick_user(root)


def test_several_most_recent_entries_refuses_to_guess(tmp_path):
    both = (
        f'"users" {{ "{OTHER_STEAM64}" {{ "MostRecent" "1" }} '
        f'"{MOST_RECENT_STEAM64}" {{ "MostRecent" "1" }} }}'
    )
    root = make_steam_tree(
        tmp_path,
        users=[
            steamid3_from_steam64(OTHER_STEAM64),
            steamid3_from_steam64(MOST_RECENT_STEAM64),
        ],
        loginusers=both,
    )
    with pytest.raises(SteamUserNotFoundError, match="more than one"):
        pick_user(root)


def test_no_users_at_all_raises(tmp_path):
    root = make_steam_tree(tmp_path)
    with pytest.raises(SteamUserNotFoundError, match="log in to Steam"):
        pick_user(root)


def test_placeholder_userdata_directories_are_ignored(tmp_path):
    root = make_steam_tree(tmp_path, users=[0, 234272])
    (root / "userdata" / "anonymous").mkdir()
    assert pick_user(root).steamid3 == 234272


def test_an_unparseable_loginusers_is_treated_as_no_information(tmp_path):
    root = make_steam_tree(tmp_path, users=[234272], loginusers='"users" { oops')
    assert pick_user(root).steamid3 == 234272
    assert steam.read_login_users(root) == {}


# --- grid paths ------------------------------------------------------------


def test_grid_stems_match_the_artwork_table():
    assert grid_stem(123, "portrait") == "123p"
    assert grid_stem(123, "landscape") == "123"
    assert grid_stem(123, "hero") == "123_hero"
    assert grid_stem(123, "logo") == "123_logo"
    assert grid_stem(123, "icon") == "123_icon"
    with pytest.raises(ValueError):
        grid_stem(123, "banner")


def test_find_grid_file_is_extension_agnostic_and_exact(tmp_path):
    grid = tmp_path / "grid"
    grid.mkdir()
    (grid / "123p.jpg").write_bytes(b"")
    (grid / "123.png").write_bytes(b"")
    (grid / "123_hero.jpg").write_bytes(b"")

    assert find_grid_file(grid, 123, "portrait") == grid / "123p.jpg"
    assert find_grid_file(grid, 123, "landscape") == grid / "123.png"
    assert find_grid_file(grid, 123, "hero") == grid / "123_hero.jpg"
    assert find_grid_file(grid, 123, "logo") is None
    assert find_grid_file(tmp_path / "missing", 123, "logo") is None


def test_find_grid_file_ignores_a_partial_download(tmp_path):
    """``.part`` files are in-flight downloads, never a finished slot (spec 3.9)."""
    grid = tmp_path / "grid"
    grid.mkdir()
    (grid / "123_logo.png.part").write_bytes(b"")
    assert find_grid_file(grid, 123, "logo") is None


def test_find_grid_file_ignores_the_logo_position_sidecar(tmp_path):
    """``<appid>.json`` is the logo *position* file, not landscape art (spec 2.1).

    It shares the landscape slot's stem, so treating it as a filled slot
    would make the skip-existing rule (spec 3.5 / 3.9 item 1) skip the
    landscape art forever for anyone who nudged a logo in the Steam UI.
    """
    grid = tmp_path / "grid"
    grid.mkdir()
    (grid / "123.json").write_text('{"nVersion": 1}')
    assert find_grid_file(grid, 123, "landscape") is None

    (grid / "123.jpg").write_bytes(b"")
    assert find_grid_file(grid, 123, "landscape") == grid / "123.jpg"


def test_find_grid_file_accepts_every_extension_steam_honours(tmp_path):
    """PNG/JPG everywhere, plus ``.ico`` for the icon slot (spec 2.1)."""
    grid = tmp_path / "grid"
    grid.mkdir()
    (grid / "1p.jpeg").write_bytes(b"")
    (grid / "2_icon.ico").write_bytes(b"")
    (grid / "3_hero.PNG").write_bytes(b"")

    assert find_grid_file(grid, 1, "portrait") == grid / "1p.jpeg"
    assert find_grid_file(grid, 2, "icon") == grid / "2_icon.ico"
    assert find_grid_file(grid, 3, "hero") == grid / "3_hero.PNG"


# --- process control -------------------------------------------------------


def test_is_running_uses_pgrep():
    runner = FakeRunner(running=True)
    assert is_running(runner) is True
    assert runner.calls == [["pgrep", "-x", "steam"]]
    assert is_running(FakeRunner(running=False)) is False


def test_is_running_falls_back_to_proc_when_pgrep_is_missing(tmp_path):
    proc = tmp_path / "proc"
    (proc / "1234").mkdir(parents=True)
    (proc / "1234" / "comm").write_text("steam\n")
    (proc / "self").mkdir()
    runner = FakeRunner(pgrep_missing=True)
    assert is_running(runner, proc_dir=proc) is True

    (proc / "1234" / "comm").write_text("something-else\n")
    assert is_running(runner, proc_dir=proc) is False


def test_is_running_without_pgrep_or_proc_says_no(tmp_path):
    assert is_running(FakeRunner(pgrep_missing=True), proc_dir=tmp_path / "nope") is False


def test_shutdown_when_steam_is_not_running_does_nothing():
    runner = FakeRunner(running=False)
    assert shutdown(runner) is True
    assert ["steam", "-shutdown"] not in runner.calls


def test_shutdown_asks_steam_to_quit_and_waits_for_it():
    runner = FakeRunner(running=True)
    assert shutdown(runner, poll_interval=0.5) is True
    assert ["steam", "-shutdown"] in runner.calls
    assert runner.slept > 0


def test_shutdown_gives_up_after_the_timeout():
    runner = FakeRunner(running=True)
    runner.shutdown_after_polls = None

    def run(cmd, *, timeout: float = 10.0):
        runner.calls.append(list(cmd))
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    runner.run = run  # type: ignore[method-assign]
    assert shutdown(runner, timeout=3.0, poll_interval=0.5) is False
    assert runner.clock >= 3.0


def test_a_hung_shutdown_command_still_polls_for_the_process():
    """``steam -shutdown`` timing out does not mean the request was not delivered."""
    runner = FakeRunner(running=True)
    inner = runner.run

    def run(cmd, *, timeout: float = 10.0):
        if cmd[:2] == ["steam", "-shutdown"]:
            runner.calls.append(list(cmd))
            runner.shutdown_after_polls = runner._polls + 2
            raise subprocess.TimeoutExpired(cmd, timeout)
        return inner(cmd, timeout=timeout)

    runner.run = run  # type: ignore[method-assign]
    assert shutdown(runner, poll_interval=0.5) is True
    assert ["steam", "-shutdown"] in runner.calls


def test_relaunch_spawns_steam_detached():
    runner = FakeRunner()
    relaunch(runner)
    assert runner.spawned == [["steam", "-silent"]]


def test_process_control_errors_are_steam_errors():
    class Broken(ProcessRunner):
        def run(self, cmd, *, timeout: float = 10.0):
            if cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise OSError("boom")

        def spawn(self, cmd):
            raise OSError("boom")

    with pytest.raises(steam.SteamError):
        shutdown(Broken())
    with pytest.raises(steam.SteamError):
        relaunch(Broken())
