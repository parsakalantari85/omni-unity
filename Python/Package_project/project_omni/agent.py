import argparse
import asyncio
import json
import os
import platform
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast, get_args

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from . import config, log, ui

logger = log.get()

# Read-only tools we approve without prompting — but only for paths inside the
# allowed roots (project + --add-dir). Reads elsewhere hit the prompt.
AUTO_APPROVE_READONLY = {"Read", "Glob", "Grep"}
# Tools with no filesystem surface at all.
AUTO_APPROVE_ALWAYS = {"TodoWrite"}


def make_approval_gate(roots: Iterable[Path]):
    """Build the can_use_tool callback, closing over the allowed roots."""
    resolved_roots = [Path(r).resolve() for r in roots]

    def _in_roots(raw: Any) -> bool:
        if not raw:
            return True  # tool defaults to cwd, which is the project root
        try:
            p = Path(str(raw)).resolve()  # neutralizes ../ and symlink escapes
        except (OSError, ValueError):
            return False
        return any(p == r or p.is_relative_to(r) for r in resolved_roots)

    async def approval_gate(
        tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Permission prompt for any tool call that isn't already allowed.

        Tools permitted elsewhere (permission_mode, settings rules) never
        reach this callback.
        """
        if tool_name in AUTO_APPROVE_ALWAYS:
            return PermissionResultAllow()
        if tool_name in AUTO_APPROVE_READONLY and _in_roots(
            input_data.get("file_path") or input_data.get("path")
        ):
            return PermissionResultAllow()

        ui.approval(tool_name, input_data, context.title)

        while True:
            try:
                choice = (await ui.ask_choice()).strip().lower()
            except EOFError:
                return PermissionResultDeny(
                    message="Input stream closed; action denied."
                )

            if choice == "f":
                ui.full_payload(input_data)
            elif choice == "a":
                return PermissionResultAllow()
            elif choice == "d":
                try:
                    reason = (await ui.ask_line("reason › ")).strip()
                except EOFError:
                    reason = ""
                return PermissionResultDeny(
                    message=reason or "User denied this action."
                )
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

    return approval_gate


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
            # Same bar as --set-project: a directory that also looks like a
            # Unity project. Skipping non-matches falls through to the next
            # source rather than handing the agent an arbitrary directory.
            if p.is_dir() and is_unity_project(p):
                return p.resolve()

    relay_env = os.environ.get("UNITY_RELAY_PATH", "")
    if relay_env:
        candidate = Path(relay_env)
        for p in [candidate, *candidate.parents]:
            if (p / "ProjectSettings").is_dir():
                return p.resolve()

    return None


def is_unity_project(path: Path) -> bool:
    """True when `path` has an Assets/ directory, a .csproj, or Assembly-CSharp*."""
    return (
        (path / "Assets").is_dir()
        or any(path.glob("*.csproj"))
        or any(path.glob("Assembly-CSharp*"))
    )


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
        if s.get("name") == "unity":
            return (
                ui.STATE_CONNECTED
                if s.get("status") == "connected"
                else ui.STATE_DISCONNECTED
            )
    return ui.STATE_NO_RELAY


def build_options(
    relay: str | None,
    project: Path | None = None,
    add_dirs: list[str | Path] | None = None,
    max_turns: int | None = None,
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
        can_use_tool=make_approval_gate(
            [Path(p) for p in [project, *(add_dirs or [])] if p]
        ),
        # Track file changes so /rewind can restore them if an approved action
        # goes wrong. replay-user-messages makes UserMessages (with uuid) flow
        # back through the stream; those uuids are the rewind checkpoints.
        enable_file_checkpointing=True,
        extra_args={"replay-user-messages": None},
        max_turns=max_turns,
    )

# Slash command handlers run locally instead of going to the model. Signature
# is (client, relay, arg) -> None; register new ones in COMMANDS below.

# Accepted values for /mode, taken from the SDK so the list can't drift.
_PERMISSION_MODES: tuple[str, ...] = get_args(PermissionMode)
# Modes that widen access beyond the approval gate. "dontAsk" is not here on
# purpose: it denies anything not pre-approved. "auto" is undocumented in the
# SDK, so treat it as widening until proven otherwise.
_GATE_BYPASSING_MODES = {"bypassPermissions", "auto"}


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
            logger.warning("reconnect attempt %d/3 failed", attempt, exc_info=True)
            if attempt < 3:
                await asyncio.sleep(1.5 * attempt)
    logger.error("reconnect failed after 3 attempts", exc_info=last_exc)
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
        logger.exception("MCP status check failed")
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
        logger.exception("could not read MCP tools")
        ui.error(f"(could not read tools: {exc})")
        return
    for server in status["mcpServers"]:
        if server.get("name") == "unity":
            if server.get("status") != "connected":
                ui.note(f"unity relay is {server.get('status')}; no tools to list.")
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
        logger.exception("set_model(%r) failed", model)
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
    if mode in _GATE_BYPASSING_MODES:
        try:
            answer = (
                await ui.ask_line(
                    f"'{mode}' disables the approval gate — continue? (y/N) › "
                )
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            ui.note("Mode unchanged.")
            return
    try:
        # Membership in _PERMISSION_MODES is checked above; the cast just
        # tells the type checker mode is a valid PermissionMode literal.
        await client.set_permission_mode(cast(PermissionMode, mode))
    except Exception as exc:
        logger.exception("set_permission_mode(%r) failed", mode)
        ui.error(f"(could not set mode: {exc})")
        return
    if mode == "default":
        ui.note("Permission mode set to default (approval gate active).")
    elif mode in _GATE_BYPASSING_MODES:
        ui.note(f"Permission mode set to {mode} — the approval gate is bypassed.")
    else:
        ui.note(f"Permission mode set to {mode}.")


async def cmd_rewind(client: ClaudeSDKClient, relay: str | None, arg: str) -> None:
    """Restore tracked files to their state as of your last message."""
    if not _checkpoints:
        ui.error("No checkpoints yet — send at least one task first.")
        return
    try:
        await client.rewind_files(_checkpoints[-1])
    except Exception as exc:
        logger.exception("rewind_files(%r) failed", _checkpoints[-1])
        ui.error(f"(rewind failed: {exc})")
        return
    ui.note("Files restored to their state at your last message.")


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
    "rewind": cmd_rewind,
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
    ("/rewind", "restore files to their state at your last message"),
    ("/reconnect", "reconnect the relay after an Editor restart"),
    ("/restart", "alias for /reconnect"),
    ("/exit, /quit", "leave Omni"),
]

# REPL

# uuids of replayed user prompts, oldest first; /rewind targets the newest.
_checkpoints: list[str] = []


def _is_prompt_replay(message: UserMessage) -> bool:
    """True for a replayed user *prompt* (usable as a rewind checkpoint).

    Tool results also arrive as UserMessages; those don't mark a state we
    would want to rewind to.
    """
    if not message.uuid or message.parent_tool_use_id or message.tool_use_result:
        return False
    if isinstance(message.content, str):
        return True
    return not any(isinstance(b, ToolResultBlock) for b in message.content)


async def drain(client: ClaudeSDKClient) -> None:
    """Print one full response (until ResultMessage)."""
    async for message in client.receive_response():
        if isinstance(message, UserMessage):
            # The explicit uuid check (redundant with _is_prompt_replay) is
            # what narrows str | None to str for the type checker.
            if message.uuid and _is_prompt_replay(message):
                _checkpoints.append(message.uuid)
        elif isinstance(message, AssistantMessage):
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
    future launches skip this prompt. No (or ctrl-d) exits — the SDK would
    otherwise fall back to the process cwd, granting access nothing consented to.
    """
    cwd = Path.cwd()
    if not is_unity_project(cwd):
        ui.error("omni only runs inside a Unity project.")
        ui.note(f"  {cwd}")
        ui.note("  (no Assets/, .csproj, or Assembly-CSharp found here)")
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


async def main(
    project: Path | None = None,
    add_dirs: list[str | Path] | None = None,
    max_turns: int | None = None,
) -> None:
    relay = find_relay()
    if project is None:
        project = await prompt_unity_access()
        if project is None:
            # No project means no session: cwd=None would silently default to
            # the process cwd, making the declined grant meaningless.
            return
    options = build_options(relay, project, add_dirs, max_turns)
    logger.info("session start: project=%s relay=%s", project, relay)

    async with ClaudeSDKClient(options=options) as client:
        # ctrl-c inside the UI reaches the running task through this.
        ui.set_interrupt_handler(client.interrupt)
        if relay is None:
            relay_st = ui.STATE_NO_RELAY
        else:
            try:
                status = await client.get_mcp_status()
                relay_st = relay_state(status["mcpServers"])
            except Exception as exc:
                logger.exception("startup MCP status check failed")
                ui.error(f"(status check failed: {exc})")
                relay_st = ui.STATE_DISCONNECTED
        ui.header(relay_st, project)

        while True:
            try:
                prompt = (await ui.ask_task()).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not prompt:
                continue
            ui.user_prompt(prompt)

            if prompt.lower().split(maxsplit=1)[0] in {"/exit", "/quit"}:
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

            # One failed turn shouldn't tear down the whole session.
            try:
                await client.query(prompt)
                await drain(client)
            except Exception as exc:
                logger.exception("task failed: %r", prompt)
                ui.error(f"(task failed: {exc})")


async def _amain(
    project: Path | None,
    add_dirs: list[str | Path] | None = None,
    max_turns: int | None = None,
) -> None:
    """Run the full-screen UI and the agent loop concurrently."""
    ui.start()
    app_task = asyncio.create_task(ui.run_async())
    try:
        await main(project, add_dirs, max_turns)
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
    parser.add_argument(
        "--max-turns",
        metavar="N",
        type=int,
        default=None,
        help="Stop a task after N agent turns (default: no limit).",
    )
    args = parser.parse_args()

    if args.set_project:
        p = Path(args.set_project).resolve()
        if not p.is_dir():
            print(f"error: '{p}' is not a directory")
            sys.exit(1)
        if not is_unity_project(p):
            print(f"error: '{p}' is not a Unity project (no Assets/, .csproj, or Assembly-CSharp)")
            sys.exit(1)
        config.set("project", str(p))
        print(f"Saved project root: {p}  ({config.path()})")
        return

    # An explicit --project that doesn't hold up is a hard error: falling
    # through to the saved config would silently run against a different
    # project than the one the user named.
    if args.project:
        p = Path(args.project)
        if not p.is_dir():
            print(f"error: --project '{args.project}' is not a directory")
            sys.exit(1)
        if not is_unity_project(p):
            print(f"error: '{p}' is not a Unity project (no Assets/, .csproj, or Assembly-CSharp)")
            sys.exit(1)

    add_dirs: list[str | Path] = []
    for raw in args.add_dir or []:
        p = Path(raw)
        if p.is_dir():
            add_dirs.append(p.resolve())
        else:
            print(f"warning: --add-dir '{raw}' is not a directory; ignoring")

    log_file = log.setup()
    project = find_project(args.project)
    try:
        asyncio.run(_amain(project, add_dirs, args.max_turns))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # The UI is torn down by now, so plain print is safe again.
        logger.exception("fatal error")
        print(f"fatal: {exc}  (traceback in {log_file})")
        sys.exit(1)