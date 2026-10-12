import asyncio
from pathlib import Path

import pytest

from project_omni import agent, config, ui, workspace_manage
from project_omni.workspace_manage import Workspace, WorkspaceError
from claude_agent_sdk.types import PermissionResultDeny, ToolPermissionContext


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".omni"
    monkeypatch.setattr(config, "_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "_CONFIG_FILE", cfg_dir / "config.json")
    return cfg_dir


@pytest.fixture
def dirs(tmp_path):
    """Three real directories to build workspaces from, already resolved."""
    made = {}
    for name in ("game", "shared", "other"):
        path = tmp_path / name
        path.mkdir()
        made[name] = path.resolve()
    return made


@pytest.fixture
def shown(monkeypatch):
    """Capture ui.note / ui.error / ui.workspaces instead of rendering them."""
    out = []
    monkeypatch.setattr(ui, "note", lambda msg: out.append(("note", msg)))
    monkeypatch.setattr(ui, "error", lambda msg: out.append(("error", msg)))
    monkeypatch.setattr(ui, "workspaces", lambda *a: out.append(("list", a)))
    return out


@pytest.fixture
def trusted(tmp_path):
    """Trust everything under tmp_path, so nothing asks the trust question."""
    workspace_manage.trust(tmp_path)


@pytest.fixture
def answers(monkeypatch):
    """Queue replies for ui.ask_line; queue an EOFError to simulate ctrl-d."""
    queue = []

    async def fake_ask_line(label=""):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(ui, "ask_line", fake_ask_line)
    monkeypatch.setattr(ui, "trust_prompt", lambda root: None)
    return queue

# saved workspaces

def test_save_then_load_round_trips(dirs):
    ws = Workspace(dirs["game"], (dirs["shared"],))
    named = workspace_manage.save("game", ws)
    assert named.name == "game"
    assert workspace_manage.load("game") == named


def test_load_unknown_name_lists_saved_ones(dirs):
    workspace_manage.save("game", Workspace(dirs["game"]))
    with pytest.raises(WorkspaceError, match="saved: game"):
        workspace_manage.load("nope")


def test_load_rejects_a_root_that_is_gone(tmp_path):
    workspace_manage.save("gone", Workspace(tmp_path / "deleted"))
    with pytest.raises(WorkspaceError, match="not a directory"):
        workspace_manage.load("gone")


def test_load_drops_extra_dirs_that_are_gone_or_too_broad(dirs, tmp_path):
    missing = tmp_path / "deleted"
    workspace_manage.save(
        "game", Workspace(dirs["game"], (missing, Path.home(), dirs["shared"]))
    )
    assert workspace_manage.load("game").add_dirs == (dirs["shared"],)


@pytest.mark.parametrize("name", ["", "..", ".hidden", "a/b", "a\\b", "my game"])
def test_save_rejects_names_that_could_read_as_paths(dirs, name):
    with pytest.raises(WorkspaceError, match="not a usable name"):
        workspace_manage.save(name, Workspace(dirs["game"]))


def test_save_and_remove_leave_other_entries_alone(dirs):
    config.set("workspaces", {"hand-edited": {"odd": True}})
    workspace_manage.save("game", Workspace(dirs["game"]))
    assert workspace_manage.remove("game") is True
    assert workspace_manage.remove("game") is False
    assert config.get("workspaces") == {"hand-edited": {"odd": True}}


def test_saved_skips_malformed_entries(dirs):
    config.set(
        "workspaces",
        {
            "bad": 3,
            "no-root": {"add_dirs": []},
            "ok": {"root": str(dirs["game"]), "add_dirs": "not a list"},
        },
    )
    assert list(workspace_manage.saved()) == ["ok"]
    assert workspace_manage.saved()["ok"].add_dirs == ()


def test_saved_tolerates_a_non_object_value():
    config.set("workspaces", ["not", "an", "object"])
    assert workspace_manage.saved() == {}

# resolve

def test_resolve_prefers_a_saved_name_over_a_directory(dirs, tmp_path):
    (tmp_path / "shared").mkdir(exist_ok=True)
    workspace_manage.save("shared", Workspace(dirs["game"]))
    assert workspace_manage.resolve("shared", base=tmp_path).root == dirs["game"]


def test_resolve_anchors_relative_paths_at_base(dirs):
    ws = workspace_manage.resolve("../other", base=dirs["game"])
    assert ws == Workspace(dirs["other"])


def test_resolve_unknown_word_says_neither(dirs):
    with pytest.raises(WorkspaceError, match="neither a saved workspace"):
        workspace_manage.resolve("nothing", base=dirs["game"])

# recent + helpers

def test_remember_puts_newest_first_without_repeats(dirs, monkeypatch):
    monkeypatch.setattr(workspace_manage, "RECENT_LIMIT", 2)
    for name in ("game", "shared", "game", "other"):
        workspace_manage.remember(Workspace(dirs[name]))
    assert workspace_manage.recent() == [dirs["other"], dirs["game"]]


def test_remember_survives_an_unwritable_config(dirs, monkeypatch):
    def fail(*_):
        raise OSError("read-only")

    monkeypatch.setattr(config, "set", fail)
    workspace_manage.remember(Workspace(dirs["game"]))  # must not raise


def test_granting_drops_repeats_and_the_root(dirs):
    ws = Workspace(dirs["game"], (dirs["shared"],))
    grown = ws.granting([dirs["shared"], dirs["game"], dirs["other"]])
    assert grown.add_dirs == (dirs["shared"], dirs["other"])


def test_is_too_broad(tmp_path):
    assert workspace_manage.is_too_broad(Path.home())
    assert workspace_manage.is_too_broad(Path(tmp_path.anchor))
    assert not workspace_manage.is_too_broad(tmp_path)

# approval gate anchoring

class FakeContext(ToolPermissionContext):
    def __init__(self):
        self.title = "list files"
        self.suggestions = []


def test_gate_anchors_relative_paths_at_the_workspace_not_process_cwd(
    tmp_path, monkeypatch
):
    root = tmp_path / "ws"
    extra = tmp_path / "extra" / "deep"
    root.mkdir()
    extra.mkdir(parents=True)
    # Omni itself runs inside the extra dir, so anchoring "../outside" at the
    # process cwd would land back inside the roots and skip the prompt.
    monkeypatch.chdir(extra)
    prompted = []

    async def deny():
        return "d"

    async def no_reason(label=""):
        return ""

    monkeypatch.setattr(ui, "approval", lambda *a: prompted.append(a))
    monkeypatch.setattr(ui, "ask_choice", deny)
    monkeypatch.setattr(ui, "ask_line", no_reason)
    gate = agent.make_approval_gate([root, tmp_path / "extra"])
    result = asyncio.run(
        gate("Glob", {"pattern": "*", "path": "../outside"}, FakeContext())
    )
    assert prompted
    assert isinstance(result, PermissionResultDeny)

# /workspace command

def run_cmd(arg):
    asyncio.run(agent.cmd_workspace(None, {}, arg))  # type: ignore[arg-type]


def test_workspace_open_raises_a_switch_to_the_chosen_workspace(
    dirs, monkeypatch, shown, trusted
):
    monkeypatch.setattr(agent, "_workspace", Workspace(dirs["game"]))
    with pytest.raises(agent._SwitchWorkspace) as switch:
        run_cmd("open ../other")
    assert switch.value.target == Workspace(dirs["other"])


def test_workspace_open_accepts_quoted_dirs_with_spaces(tmp_path, monkeypatch, shown, trusted):
    spaced = tmp_path / "My Projects"
    spaced.mkdir()
    monkeypatch.setattr(agent, "_workspace", Workspace(tmp_path.resolve()))
    with pytest.raises(agent._SwitchWorkspace) as switch:
        run_cmd(f'open "{spaced}"')
    assert switch.value.target.root == spaced.resolve()


def test_workspace_open_same_roots_stays_put(dirs, monkeypatch, shown):
    monkeypatch.setattr(agent, "_workspace", Workspace(dirs["game"]))
    run_cmd(f"open {dirs['game']}")
    assert shown == [("note", "Already in that workspace.")]


def test_workspace_open_bad_target_reports_instead_of_switching(
    dirs, monkeypatch, shown
):
    monkeypatch.setattr(agent, "_workspace", Workspace(dirs["game"]))
    run_cmd("open nothing")
    assert shown[0][0] == "error"


def test_workspace_save_names_the_current_session(dirs, monkeypatch, shown):
    monkeypatch.setattr(agent, "_workspace", Workspace(dirs["game"], (dirs["shared"],)))
    run_cmd("save game")
    assert agent._workspace is not None and agent._workspace.name == "game"
    assert workspace_manage.load("game").add_dirs == (dirs["shared"],)


def test_workspace_without_subcommand_lists(dirs, monkeypatch, shown):
    monkeypatch.setattr(agent, "_workspace", Workspace(dirs["game"]))
    run_cmd("")
    assert shown[0][0] == "list"


def test_workspace_unknown_subcommand_shows_usage(dirs, monkeypatch, shown):
    monkeypatch.setattr(agent, "_workspace", Workspace(dirs["game"]))
    run_cmd("frobnicate")
    assert shown == [("error", agent._WORKSPACE_USAGE)]

def test_workspace_open_untrusted_declined_stays_put(dirs, monkeypatch, shown, answers):
    monkeypatch.setattr(agent, "_workspace", Workspace(dirs["game"]))
    answers.append("n")
    run_cmd("open ../other")  # no _SwitchWorkspace raised
    assert shown[-1] == ("note", f"Not trusted; staying in {dirs['game']}.")
    assert not workspace_manage.is_trusted(dirs["other"])

# trust

def test_trust_covers_subfolders_but_not_siblings_or_parents(dirs, tmp_path):
    assert workspace_manage.trust(dirs["game"]) is True
    inner = dirs["game"] / "Packages"
    inner.mkdir()
    assert workspace_manage.is_trusted(dirs["game"])
    assert workspace_manage.is_trusted(inner)
    assert not workspace_manage.is_trusted(dirs["other"])
    assert not workspace_manage.is_trusted(tmp_path)


def test_trust_is_recorded_once(dirs):
    workspace_manage.trust(dirs["game"])
    workspace_manage.trust(dirs["game"])
    assert config.get("trusted_workspaces") == [str(dirs["game"])]


def test_home_is_never_remembered_even_when_hand_edited(tmp_path):
    assert workspace_manage.trust(Path.home()) is False
    assert config.get("trusted_workspaces") is None
    config.set("trusted_workspaces", [str(Path.home())])
    assert not workspace_manage.is_trusted(Path.home())


def test_confirm_trust_yes_remembers(dirs, answers):
    answers.append("y")
    assert asyncio.run(agent.confirm_trust(Workspace(dirs["game"]))) is True
    assert workspace_manage.is_trusted(dirs["game"])


@pytest.mark.parametrize("reply", ["", "n", "no", EOFError()])
def test_confirm_trust_anything_but_yes_is_no(dirs, answers, reply):
    answers.append(reply)
    assert asyncio.run(agent.confirm_trust(Workspace(dirs["game"]))) is False
    assert not workspace_manage.is_trusted(dirs["game"])


def test_confirm_trust_does_not_ask_twice(dirs, answers):
    workspace_manage.trust(dirs["game"])
    # answers is empty, so asking would fail with IndexError.
    assert asyncio.run(agent.confirm_trust(Workspace(dirs["game"]))) is True

# main loop

def test_main_exits_without_a_session_when_trust_is_declined(dirs, monkeypatch, answers):
    started = []

    async def fake_session(*a):
        started.append(a)

    monkeypatch.setattr(agent, "_session", fake_session)
    monkeypatch.setattr(agent, "load_mcp_servers", lambda: {})
    answers.append("n")
    asyncio.run(agent.main(Workspace(dirs["game"])))
    assert started == []


def test_main_runs_a_new_session_per_switch(dirs, monkeypatch, trusted):
    first, second = Workspace(dirs["game"]), Workspace(dirs["other"])
    calls = []

    async def fake_session(ws, servers, max_turns, is_first):
        calls.append((ws, is_first))
        return second if ws == first else None

    monkeypatch.setattr(agent, "_session", fake_session)
    monkeypatch.setattr(agent, "load_mcp_servers", lambda: {})
    asyncio.run(agent.main(first))
    assert calls == [(first, True), (second, False)]

# rendering

def test_workspaces_renders_saved_missing_and_recent(dirs, tmp_path):
    current = Workspace(dirs["game"], (dirs["shared"],), "game")
    saved = {
        "game": current,
        "gone": Workspace(tmp_path / "deleted"),
    }
    before = len(ui._blocks)
    try:
        ui.workspaces(current, saved, [dirs["game"], dirs["other"]])
        text = "".join(frag[1] for block in ui._blocks[before:] for frag in block)
        text = text.replace("\n", "")  # long temp paths wrap at the pane width
    finally:
        del ui._blocks[before:]
    assert "missing" in text
    assert str(dirs["other"]) in text
    assert "+1 dir" in text
