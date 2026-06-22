"""Tests for project_omni.

Scope: the package's pure, deterministic logic — the config store, project /
relay discovery, status mapping, and the human-in-the-loop approval gate.
Anything that needs a live Unity Editor, a connected ClaudeSDKClient, or the
full-screen prompt_toolkit app is intentionally out of scope (those are
integration concerns, not unit-testable without heavy fakes).

Run:  pytest        (install dev deps first: pip install -e .[dev])
"""
import asyncio
import json

import pytest

from project_omni import agent, config, ui
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)


# config store

@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Point the config module at a throwaway directory so tests never touch
    the real ~/.omni/config.json."""
    cfg_dir = tmp_path / ".omni"
    monkeypatch.setattr(config, "_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "_CONFIG_FILE", cfg_dir / "config.json")
    return cfg_dir


def test_get_returns_default_when_missing(tmp_config):
    assert config.get("project") is None
    assert config.get("project", "fallback") == "fallback"


def test_set_then_get_round_trips(tmp_config):
    config.set("project", "/some/unity/project")
    assert config.get("project") == "/some/unity/project"


def test_set_creates_dir_and_writes_valid_json(tmp_config):
    config.set("model", "claude-opus-4-8")
    assert config._CONFIG_FILE.is_file()
    data = json.loads(config._CONFIG_FILE.read_text(encoding="utf-8"))
    assert data == {"model": "claude-opus-4-8"}


def test_set_preserves_other_keys(tmp_config):
    config.set("project", "/p")
    config.set("model", "m")
    assert config.get("project") == "/p"
    assert config.get("model") == "m"


def test_load_tolerates_corrupt_json(tmp_config):
    tmp_config.mkdir(parents=True)
    (tmp_config / "config.json").write_text("{not valid json", encoding="utf-8")
    # A corrupt file must degrade to "no config", not raise.
    assert config.get("project") is None


# is_unity_project

def test_is_unity_project_detects_csproj(tmp_path):
    (tmp_path / "Game.csproj").write_text("", encoding="utf-8")
    assert agent.is_unity_project(tmp_path)


def test_is_unity_project_detects_assembly_csharp(tmp_path):
    (tmp_path / "Assembly-CSharp.csproj").write_text("", encoding="utf-8")
    assert agent.is_unity_project(tmp_path)


def test_is_unity_project_detects_assets_dir(tmp_path):
    (tmp_path / "Assets").mkdir()
    assert agent.is_unity_project(tmp_path)


def test_is_unity_project_false_for_plain_dir(tmp_path):
    assert not agent.is_unity_project(tmp_path)


# find_project: priority order

def test_find_project_prefers_override(tmp_path, monkeypatch):
    monkeypatch.delenv("UNITY_PROJECT_PATH", raising=False)
    monkeypatch.setattr(config, "get", lambda *a, **k: None)
    result = agent.find_project(str(tmp_path))
    assert result == tmp_path.resolve()


def test_find_project_falls_back_to_saved_config(tmp_path, monkeypatch):
    monkeypatch.delenv("UNITY_PROJECT_PATH", raising=False)
    monkeypatch.setattr(config, "get", lambda *a, **k: str(tmp_path))
    assert agent.find_project(None) == tmp_path.resolve()


def test_find_project_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get", lambda *a, **k: None)
    monkeypatch.setenv("UNITY_PROJECT_PATH", str(tmp_path))
    assert agent.find_project(None) == tmp_path.resolve()


def test_find_project_ignores_nonexistent_paths(monkeypatch):
    monkeypatch.delenv("UNITY_PROJECT_PATH", raising=False)
    monkeypatch.delenv("UNITY_RELAY_PATH", raising=False)
    monkeypatch.setattr(config, "get", lambda *a, **k: None)
    assert agent.find_project("/no/such/dir/anywhere") is None


def test_find_project_walks_up_from_relay_to_project_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("UNITY_PROJECT_PATH", raising=False)
    monkeypatch.setattr(config, "get", lambda *a, **k: None)
    root = tmp_path / "UnityProj"
    (root / "ProjectSettings").mkdir(parents=True)
    relay = root / "RelayApp~" / "relay.exe"
    relay.parent.mkdir(parents=True)
    relay.write_text("", encoding="utf-8")
    monkeypatch.setenv("UNITY_RELAY_PATH", str(relay))
    assert agent.find_project(None) == root.resolve()


# find_relay

def test_find_relay_none_without_env(monkeypatch):
    monkeypatch.delenv("UNITY_RELAY_PATH", raising=False)
    assert agent.find_relay() is None


def test_find_relay_returns_direct_file(tmp_path, monkeypatch):
    binary = tmp_path / "relay"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setenv("UNITY_RELAY_PATH", str(binary))
    assert agent.find_relay() == str(binary)


def test_find_relay_picks_platform_binary_from_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITY_RELAY_PATH", str(tmp_path))
    monkeypatch.setattr(agent.sys, "platform", "win32")
    win_binary = tmp_path / "relay_win.exe"
    win_binary.write_text("", encoding="utf-8")
    assert agent.find_relay() == str(win_binary)


def test_find_relay_dir_without_matching_binary_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITY_RELAY_PATH", str(tmp_path))
    monkeypatch.setattr(agent.sys, "platform", "win32")
    # Directory exists but holds no relay_win.exe.
    assert agent.find_relay() is None


# relay_state

def test_relay_state_connected():
    servers = [{"name": "unity", "status": "connected"}]
    assert agent.relay_state(servers) == ui.STATE_CONNECTED


def test_relay_state_disconnected():
    servers = [{"name": "unity", "status": "failed"}]
    assert agent.relay_state(servers) == ui.STATE_DISCONNECTED


def test_relay_state_no_unity_server():
    servers = [{"name": "other", "status": "connected"}]
    assert agent.relay_state(servers) == ui.STATE_NO_RELAY


# _tool_names

def test_tool_names_handles_dicts_and_strings():
    server = {"tools": [{"name": "set_graphics"}, "raw_tool"]}
    assert agent._tool_names(server) == ["set_graphics", "raw_tool"]


def test_tool_names_empty_when_missing():
    assert agent._tool_names({}) == []
    assert agent._tool_names({"tools": None}) == []


# ui name parsing

def test_tool_plain_unpacks_mcp_name():
    assert ui._tool_plain("mcp__unity__set_graphics") == "set_graphics (unity)"


def test_tool_plain_passthrough_for_plain_name():
    assert ui._tool_plain("Read") == "Read"


# approval_gate
class FakeContext(ToolPermissionContext):
    """Minimal ToolPermissionContext stand-in for the gate."""
    def __init__(self, title="do the thing"):
        self.title = title
        self.suggestions = []


@pytest.fixture
def stub_ui(monkeypatch):
    """Replace the UI's I/O with scripted answers and capture what was shown.

    Set ``ui_state['choices']`` / ``ui_state['lines']`` to queues of strings
    the gate will receive from ask_choice()/ask_line(). Raise EOFError by
    queueing an EOFError instance.
    """
    state = {"choices": [], "lines": [], "shown": []}

    async def fake_ask_choice():
        item = state["choices"].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def fake_ask_line(label=""):
        item = state["lines"].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(ui, "ask_choice", fake_ask_choice)
    monkeypatch.setattr(ui, "ask_line", fake_ask_line)
    monkeypatch.setattr(ui, "approval", lambda *a, **k: state["shown"].append(a))
    monkeypatch.setattr(ui, "full_payload", lambda *a, **k: state["shown"].append("full"))
    monkeypatch.setattr(ui, "note", lambda *a, **k: None)
    monkeypatch.setattr(ui, "error", lambda *a, **k: state["shown"].append("error"))
    return state


def run(coro):
    return asyncio.run(coro)


def test_gate_auto_approves_read_only_tools(stub_ui):
    result = run(agent.approval_gate("Read", {"file_path": "x"}, FakeContext()))
    assert isinstance(result, PermissionResultAllow)
    # Auto-approved tools never reach the prompt.
    assert stub_ui["shown"] == []


def test_gate_approve_choice(stub_ui):
    stub_ui["choices"] = ["a"]
    result = run(agent.approval_gate("Write", {"file_path": "x"}, FakeContext()))
    assert isinstance(result, PermissionResultAllow)


def test_gate_deny_with_reason(stub_ui):
    stub_ui["choices"] = ["d"]
    stub_ui["lines"] = ["touches ProjectSettings"]
    result = run(agent.approval_gate("Bash", {"command": "rm -rf /"}, FakeContext()))
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "touches ProjectSettings"


def test_gate_deny_empty_reason_gets_default(stub_ui):
    stub_ui["choices"] = ["d"]
    stub_ui["lines"] = ["   "]
    result = run(agent.approval_gate("Bash", {"command": "ls"}, FakeContext()))
    assert isinstance(result, PermissionResultDeny)
    assert result.message == "User denied this action."


def test_gate_quit_denies_and_interrupts(stub_ui):
    stub_ui["choices"] = ["q"]
    result = run(agent.approval_gate("Write", {}, FakeContext()))
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True


def test_gate_edit_replaces_input(stub_ui):
    stub_ui["choices"] = ["e"]
    stub_ui["lines"] = ['{"file_path": "safe.txt", "content": "hi"}']
    result = run(agent.approval_gate("Write", {"file_path": "x"}, FakeContext()))
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"file_path": "safe.txt", "content": "hi"}


def test_gate_edit_rejects_invalid_json_then_recovers(stub_ui):
    # First reply is malformed JSON -> error + reprompt; then approve.
    stub_ui["choices"] = ["e", "a"]
    stub_ui["lines"] = ["{not json"]
    result = run(agent.approval_gate("Write", {}, FakeContext()))
    assert isinstance(result, PermissionResultAllow)
    assert "error" in stub_ui["shown"]


def test_gate_edit_rejects_non_object_json(stub_ui):
    # A JSON array is valid JSON but not a tool-input object -> reprompt.
    stub_ui["choices"] = ["e", "d"]
    stub_ui["lines"] = ["[1, 2, 3]", "no"]
    result = run(agent.approval_gate("Write", {}, FakeContext()))
    assert isinstance(result, PermissionResultDeny)
    assert "error" in stub_ui["shown"]


def test_gate_unknown_choice_reprompts(stub_ui):
    stub_ui["choices"] = ["x", "a"]
    result = run(agent.approval_gate("Write", {}, FakeContext()))
    assert isinstance(result, PermissionResultAllow)
    assert "error" in stub_ui["shown"]


def test_gate_eof_on_choice_denies(stub_ui):
    stub_ui["choices"] = [EOFError()]
    result = run(agent.approval_gate("Write", {}, FakeContext()))
    assert isinstance(result, PermissionResultDeny)


def test_gate_full_payload_then_approve(stub_ui):
    # [f] shows the full payload and loops back for another choice.
    stub_ui["choices"] = ["f", "a"]
    result = run(agent.approval_gate("Write", {"file_path": "x"}, FakeContext()))
    assert isinstance(result, PermissionResultAllow)
    assert "full" in stub_ui["shown"]
