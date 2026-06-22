import argparse
import asyncio
import json
import os
import platform
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from . import config, ui

# Read-only tools we approve without prompting.
AUTO_APPROVE = {"Read", "Glob", "Grep", "TodoWrite"}


async def approval_gate(
    tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    """Permission prompt for any tool call that isn't already allowed.

    Tools permitted elsewhere (AUTO_APPROVE, permission_mode, settings rules)
    never reach this callback.
    """
    if tool_name in AUTO_APPROVE:
        return PermissionResultAllow()

    ui.approval(tool_name, input_data, context.title)

    while True:
        try:
            choice = (await ui.ask_choice()).strip().lower()
        except EOFError:
            return PermissionResultDeny(message="Input stream closed; action denied.")

        if choice == "f":
            ui.full_payload(input_data)
        elif choice == "a":
            return PermissionResultAllow()
        elif choice == "d":
            try:
                reason = (await ui.ask_line("reason › ")).strip()
            except EOFError:
                reason = ""
            return PermissionResultDeny(message=reason or "User denied this action.")
        elif choice == "e":
            ui.note("Paste replacement JSON object for the input (single line):")
            try:
                raw = (await ui.ask_line("json › ")).strip()
            except EOFError:
                return PermissionResultDeny(
                    message="Input stream closed; action denied."
                )
            try:
                new_input = json.loads(raw)
            except json.JSONDecodeError as exc:
                ui.error(f"Invalid JSON ({exc}); try again.")
                continue
            if not isinstance(new_input, dict):
                ui.error(
                    f"Tool input must be a JSON object, got "
                    f"{type(new_input).__name__}; try again."
                )
                continue
            return PermissionResultAllow(updated_input=new_input)
        elif choice == "q":
            return PermissionResultDeny(
                message="User denied and stopped the task.", interrupt=True
            )
        else:
            ui.error("Unrecognized choice; pick a / d / e / f / q.")


def find_project(override: str | None = None) -> Path | None:
    """Locate the Unity project root.

    Priority:
      1. --project CLI arg
      2. ~/.omni/config.json  (written by --set-project)
      3. UNITY_PROJECT_PATH env var
      4. Walk up from UNITY_RELAY_PATH until a ProjectSettings/ folder is found
    """
    for raw in (override, config.get("project"), os.environ.get("UNITY_PROJECT_PATH")):
        if raw:
            p = Path(raw)
            if p.is_dir():
                return p.resolve()

    relay_env = os.environ.get("UNITY_RELAY_PATH", "")
    if relay_env:
        candidate = Path(relay_env)
        for p in [candidate, *candidate.parents]:
            if (p / "ProjectSettings").is_dir():
                return p.resolve()

    return None


def is_unity_project(path: Path) -> bool:
    """True when `path` has a .csproj, Assembly-CSharp*, or Assets/ entry."""
    return any(path.glob("*.csproj")) or any(path.glob("Assembly-CSharp*")) or any(path.glob("Assets"))


def find_relay() -> str | None:
    """Path to the Unity MCP relay binary, or None when it isn't available.

    UNITY_RELAY_PATH may point either directly to the binary or to the
    RelayApp~ directory that contains platform-specific binaries.
    """
    relay = os.environ.get("UNITY_RELAY_PATH")
    if not relay:
        return None
    if os.path.isfile(relay):
        return relay
    # Directory case: pick the right binary for the current platform.
    if sys.platform == "win32":
        candidate = os.path.join(relay, "relay_win.exe")
    elif sys.platform == "darwin":
        candidate = os.path.join(
            relay,
            "relay_mac_arm64" if platform.machine() == "arm64" else "relay_mac_x64",
        )
    else:
        candidate = os.path.join(relay, "relay_linux")
    return candidate if os.path.isfile(candidate) else None


def relay_state(servers: Iterable[Mapping[str, Any]]) -> str:
    """Map the SDK's MCP server status to the user-facing relay state."""
    for s in servers:
        if s["name"] == "unity":
            return (
                ui.STATE_CONNECTED
                if s["status"] == "connected"
                else ui.STATE_DISCONNECTED
            )
    return ui.STATE_NO_RELAY


def build_options(
    relay: str | None,
    project: Path | None = None,
    add_dirs: list[str | Path] | None = None,
) -> ClaudeAgentOptions:
    # No relay is not fatal: the REPL still starts and the header explains how
    # to connect.
    mcp_servers: dict[str, Any] = {}
    if relay:
        mcp_servers["unity"] = {
            "type": "stdio",
            "command": relay,
            "args": ["--mcp"],
        }

    return ClaudeAgentOptions(
        # preset+append keeps Claude Code's default system prompt and adds our
        # domain instructions on top; a plain string would replace it entirely.
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "You are a Unity Editor automation agent. You operate on a live Unity "
                "project via Unity MCP tools. Before acting: state your plan briefly. "
                "Prefer small, verifiable steps. Domain focus: Editor configuration, "
                "graphics pipeline and Project Settings. "
                "Never modify files under Library/ or ProjectSettings/ directly with "
                "file tools when an MCP tool exists for the same change."
            ),
        },
        mcp_servers=mcp_servers,
        cwd=project,
        # Extra roots the agent may read/write beyond the project cwd.
        add_dirs=add_dirs or [],
        # Pinned so host settings can't silently widen permissions under the gate.
        permission_mode="default",
        can_use_tool=approval_gate,
        # Track file changes so we can rewind if an approved action goes wrong.
        enable_file_checkpointing=True,
        # Optional hard ceilings while developing:
        max_turns=50,
    )

# Slash command handlers run locally instead of going to the model. Signature
# is (client, relay, arg) -> None; register new ones in COMMANDS below.

# Accepted values for /mode (matches the SDK's PermissionMode).
_PERMISSION_MODES = (
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "dontAsk",
    "auto",
)


async def cmd_reconnect(client: ClaudeSDKClient, relay: str | None, arg: str) -> None:
    """Restart the Unity MCP relay link (use after an Editor restart)."""
    if relay is None:
        ui.relay_update(ui.STATE_NO_RELAY)
        return
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            ui.note(f"Reconnecting… (attempt {attempt}/3)")
            await client.reconnect_mcp_server("unity")
            # Give the relay subprocess a moment to finish its handshake with
            # Unity before we read status.
            await asyncio.sleep(1.0)
            status = await client.get_mcp_status()
            ui.relay_update(relay_state(status["mcpServers"]))
            return
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                await asyncio.sleep(1.5 * attempt)
    ui.error(f"(reconnect failed: {last_exc})")
    # A flat reconnect failure means we never (re-)connected.
    ui.relay_update(ui.STATE_DISCONNECTED)


async def cmd_test(client: ClaudeSDKClient, relay: str | None, arg: str) -> None:
    """Probe the Unity MCP connection without reconnecting."""
    if relay is None:
        ui.relay_update(ui.STATE_NO_RELAY)
        return
    try:
        status = await client.get_mcp_status()
        ui.relay_update(relay_state(status["mcpServers"]))
    except Exception as exc:
        ui.error(f"(status check failed: {exc})")


def _tool_names(server: Mapping[str, Any]) -> list[str]:
    """Pull tool names out of an mcpServers entry; tools may be dicts or strs."""
    names = []
    for tool in server.get("tools", []) or []:
        if isinstance(tool, Mapping):
            names.append(str(tool.get("name", tool)))
        else:
            names.append(str(tool))
    return names


async def cmd_tools(client: ClaudeSDKClient, relay: str | None, arg: str) -> None:
    """List the tools the Unity relay currently exposes."""
    if relay is None:
        ui.relay_update(ui.STATE_NO_RELAY)
        return
    try:
        status = await client.get_mcp_status()
    except Exception as exc:
        ui.error(f"(could not read tools: {exc})")
        return
    for server in status["mcpServers"]:
        if server["name"] == "unity":
            if server["status"] != "connected":
                ui.note(f"unity relay is {server['status']}; no tools to list.")
                return
            ui.tool_list("unity", _tool_names(server))
            return
    ui.note("No Unity relay is configured.")


async def cmd_clear(client: ClaudeSDKClient, relay: str | None, arg: str) -> None:
    """Clear the transcript pane (display only; conversation is untouched)."""
    ui.clear()


async def cmd_model(client: ClaudeSDKClient, relay: str | None, arg: str) -> None:
    """Switch the model mid-session, e.g. `/model claude-opus-4-8`."""
    model = arg.strip()
    if not model:
        ui.error("Usage: /model <name> (e.g. /model claude-sonnet-4-6).")
        return
    try:
        await client.set_model(model)
        ui.note(f"Model set to {model}.")
    except Exception as exc:
        ui.error(f"(could not set model: {exc})")


async def cmd_mode(client: ClaudeSDKClient, relay: str | None, arg: str) -> None:
    """Change the permission mode, e.g. `/mode acceptEdits`.

    build_options pins this to "default" so host settings can't silently widen
    the approval gate; this command lets *you* change it deliberately.
    """
    mode = arg.strip()
    if not mode:
        ui.error("Usage: /mode <" + " | ".join(_PERMISSION_MODES) + ">.")
        return
    if mode not in _PERMISSION_MODES:
        ui.error(f"Unknown mode {mode!r}. Choose: {', '.join(_PERMISSION_MODES)}.")
        return
    try:
        await client.set_permission_mode(mode)
    except Exception as exc:
        ui.error(f"(could not set mode: {exc})")
        return
    if mode == "default":
        ui.note("Permission mode set to default (approval gate active).")
    else:
        ui.note(f"Permission mode set to {mode} — this can bypass the approval gate.")


async def cmd_help(client: ClaudeSDKClient, relay: str | None, arg: str) -> None:
    """List the available slash commands."""
    ui.command_help(COMMANDS_HELP)


# name (without leading slash) -> handler.
COMMANDS: dict[str, Any] = {
    "help": cmd_help,
    "test": cmd_test,
    "tools": cmd_tools,
    "clear": cmd_clear,
    "model": cmd_model,
    "mode": cmd_mode,
    "reconnect": cmd_reconnect,
    "restart": cmd_reconnect,  # alias for reconnect
}

# (label, description) rows shown by /help; keep in sync with COMMANDS.
COMMANDS_HELP = [
    ("/help", "list these commands"),
    ("/test", "probe the Unity MCP connection"),
    ("/tools", "list the tools the Unity relay exposes"),
    ("/clear", "clear the transcript pane (display only)"),
    ("/model <name>", "switch the model mid-session"),
    ("/mode <mode>", "change permission mode (default/acceptEdits/plan/…)"),
    ("/reconnect", "reconnect the relay after an Editor restart"),
    ("/restart", "alias for /reconnect"),
    ("/exit, /quit", "leave Omni"),
]

# REPL

async def drain(client: ClaudeSDKClient) -> None:
    """Print one full response (until ResultMessage)."""
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    ui.assistant(block.text)
                elif isinstance(block, ToolUseBlock):
                    ui.tool_request(block.name, block.input)
        elif isinstance(message, ResultMessage):
            ui.turn_done(
                message.num_turns,
                message.total_cost_usd,
                message.subtype if message.is_error else None,
            )


async def prompt_unity_access() -> Path | None:
    """Offer to make the current directory omni's saved Unity project.

    Called only when no project was configured by other means. The cwd must
    look like a Unity project; answering yes saves it to ~/.omni/config.json so
    future launches skip this prompt. No (or ctrl-d) starts with no project.
    """
    cwd = Path.cwd()
    if not is_unity_project(cwd):
        ui.error("omni only runs inside a Unity project.")
        ui.note(f"  {cwd}")
        ui.note("  (no .csproj or Assembly-CSharp found here)")
        return None

    ui.note("Grant omni persistent access to this Unity project?")
    ui.note(f"  {cwd}")
    try:
        answer = (await ui.ask_line("grant? › ")).strip().lower()
    except EOFError:
        return None
    if answer not in {"y", "yes"}:
        return None

    resolved = cwd.resolve()
    config.set("project", str(resolved))  # persist for next launch
    ui.note(f"Saved — omni will reuse this project ({config.path()}).")
    return resolved


async def main(project: Path | None = None, add_dirs: list[str | Path] | None = None) -> None:
    relay = find_relay()
    if project is None:
        project = await prompt_unity_access()
    options = build_options(relay, project, add_dirs)

    async with ClaudeSDKClient(options=options) as client:
        # ctrl-c inside the UI reaches the running task through this.
        ui.set_interrupt_handler(client.interrupt)
        if relay is None:
            relay_st = ui.STATE_NO_RELAY
        else:
            status = await client.get_mcp_status()
            relay_st = relay_state(status["mcpServers"])
        ui.header(relay_st, project)

        while True:
            try:
                prompt = (await ui.ask_task()).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not prompt:
                continue
            ui.user_prompt(prompt)

            low = prompt.lower()
            if low in {"/exit", "/quit"}:
                break
            # Slash commands run locally; first word is the command, the rest
            # its argument. Bare words go to the model.
            if prompt.startswith("/"):
                name, _, arg = prompt.partition(" ")
                handler = COMMANDS.get(name.lower().lstrip("/"))
                if handler is None:
                    ui.error(f"Unknown command {prompt!r} — type /help.")
                else:
                    await handler(client, relay, arg.strip())
                continue

            await client.query(prompt)
            await drain(client)


async def _amain(project: Path | None, add_dirs: list[str | Path] | None = None) -> None:
    """Run the full-screen UI and the agent loop concurrently."""
    ui.start()
    app_task = asyncio.create_task(ui.run_async())
    try:
        await main(project, add_dirs)
    finally:
        ui.stop()
        await app_task


def run() -> None:
    """Sync entry point for ``python -m project_omni`` and the console script."""
    parser = argparse.ArgumentParser(
        prog="omni",
        description="Unity Editor automation agent",
    )
    parser.add_argument(
        "--project",
        metavar="DIR",
        default=None,
        help="Unity project root for this session (does not save).",
    )
    parser.add_argument(
        "--set-project",
        metavar="DIR",
        default=None,
        help="Save a Unity project root to ~/.omni/config.json and exit.",
    )
    parser.add_argument(
        "--add-dir",
        metavar="DIR",
        action="append",
        default=None,
        help="Grant the agent access to an extra directory beyond the project "
        "root. Repeatable.",
    )
    args = parser.parse_args()

    if args.set_project:
        p = Path(args.set_project).resolve()
        if not p.is_dir():
            print(f"error: '{p}' is not a directory")
            sys.exit(1)
        if not is_unity_project(p):
            print(f"error: '{p}' is not a Unity project (no .csproj or Assembly-CSharp)")
            sys.exit(1)
        config.set("project", str(p))
        print(f"Saved project root: {p}  ({config.path()})")
        return

    if args.project and not Path(args.project).is_dir():
        print(f"warning: --project '{args.project}' is not a directory; ignoring")

    add_dirs: list[str | Path] = []
    for raw in args.add_dir or []:
        p = Path(raw)
        if p.is_dir():
            add_dirs.append(p.resolve())
        else:
            print(f"warning: --add-dir '{raw}' is not a directory; ignoring")

    project = find_project(args.project)
    try:
        asyncio.run(_amain(project, add_dirs))
    except KeyboardInterrupt:
        pass