# Omni — Unity Editor Automation Agent

Omni is an interactive CLI that lets you automate Unity Editor workflows in plain English. It pairs the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) with the Unity MCP relay to give Claude live read/write access to your Unity project, while an explicit approval gate keeps you in control of every action.



## How it works

1. You type a task (e.g. "Enable HDR on the Main Camera and set bloom intensity to 0.8").
2. Claude plans the steps and issues tool calls.
3. Read-only tools (`Read`, `Glob`, `Grep`) are approved automatically.
4. Everything else pauses for your approval — you can approve, deny, edit the payload, or stop the task entirely.
5. The relay translates approved calls into Unity Editor actions and returns results.

## Requirements

- Python ≥ 3.10
- Unity Editor with the [Unity MCP bridge](https://docs.unity3d.com/Packages/com.unity.ai.assistant@2.0/manual/unity-mcp-overview.html) installed and approved in **Project Settings ▸ AI ▸ Unity MCP**
- The Unity MCP relay binary (`relay_linux`, `relay_mac_arm64`, `relay_mac_x64`, or `relay_win.exe`)


## Installation

```bash
cd Python/Package_project
pip install -e .          # installs the `omni` console script
pip install -e .[dev]     # also installs pytest for running tests
```

## Configuration

### Relay binary

Point Omni at the relay binary (or the directory that contains platform binaries):

```bash
export UNITY_RELAY_PATH=/path/to/RelayApp~          # directory form
```
or
```bash
export UNITY_RELAY_PATH=/path/to/RelayApp~/relay_linux   # file form
```

Omni picks the right binary for the current OS automatically when you point it at a directory.

### Unity project

Omni finds your project in priority order:

| Priority | Source |
|---|---|
| 1 | `--project DIR` CLI flag |
| 2 | Saved config (`~/.omni/config.json`) via `--set-project` |
| 3 | `UNITY_PROJECT_PATH` environment variable |
| 4 | Walk up from `UNITY_RELAY_PATH` until a `ProjectSettings/` folder is found |

Save a project permanently so you don't need to pass `--project` every time:

```bash
omni --set-project /path/to/MyGame
```

## Usage

```
omni [--project DIR] [--set-project DIR] [--add-dir DIR]
```

| Flag | Description |
|---|---|
| `--project DIR` | Use this Unity project for the session (does not save). |
| `--set-project DIR` | Save a project root to `~/.omni/config.json` and exit. |
| `--add-dir DIR` | Grant the agent access to an extra directory. Repeatable. |

### Example

```bash
# Point at the relay, then start the agent inside your Unity project
export UNITY_RELAY_PATH=~/Downloads/RelayApp~
omni --project ~/dev/MyGame
```


## Slash commands

Type any of these at the `›` prompt:

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/test` | Probe the Unity MCP connection |
| `/tools` | List the tools the Unity relay currently exposes |
| `/model <name>` | Switch the Claude model mid-session (e.g. `/model claude-opus-4-8`) |
| `/mode <mode>` | Change permission mode (`default` / `acceptEdits` / `plan` / …) |
| `/reconnect` | Reconnect the relay after an Editor restart |
| `/restart` | Alias for `/reconnect` |
| `/clear` | Clear the transcript pane (conversation is preserved) |
| `/exit`, `/quit` | Leave Omni |

## Approval gate

Whenever Claude wants to call a non-read-only tool, Omni shows a preview and waits:

| Key | Action |
|---|---|
| `a` | **Approve** — let the tool run |
| `d` | **Deny** — block this call (you can give a reason) |
| `e` | **Edit** — paste replacement JSON to change the tool's input before it runs |
| `f` | **Full payload** — show the complete input before deciding |
| `q` | **Deny + stop** — block this call and interrupt the whole task |

Read-only tools (`Read`, `Glob`, `Grep`, `TodoWrite`) are approved silently without a prompt.

## Debugging

Set `OMNI_DEBUG=1` to print a full Python traceback on fatal errors instead of the terse one-liner:

```bash
OMNI_DEBUG=1 omni --project ~/dev/MyGame
```

## Project structure

```
Python/Package_project/
├── pyproject.toml              # package metadata, console script entry point
├── dev-requirements.txt        # development dependencies (pytest, …)
└── project_omni/
    ├── __init__.py
    ├── __main__.py             # python -m project_omni entry point
    ├── agent.py                # REPL loop, approval gate, slash commands, SDK wiring
    ├── config.py               # ~/.omni/config.json read/write helpers
    └── ui.py                   # full-screen prompt_toolkit + rich terminal UI
tests/
└── test_Package.py             # unit tests (config, discovery, approval gate, UI helpers)
```

## Running tests

```bash
cd Python/Package_project
pip install -e .[dev]
pytest
```

Tests cover the config store, project/relay discovery, relay-state mapping, UI name parsing, and the full approval-gate state machine. Live Unity Editor integration is out of scope for unit tests.


## License

See [LICENSE](LICENSE).