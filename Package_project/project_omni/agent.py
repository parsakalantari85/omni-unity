import argparse
import asyncio
import json
import os
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
from . import config, log, ui, workspace_manage
from .workspace_manage import Workspace, WorkspaceError

logger = log.get()

# --- approval gate ----------------------------------------------------------
AUTO_APPROVE_READONLY = {"Read", "Glob", "Grep"}
AUTO_APPROVE_ALWAYS = {"TodoWrite"}

def make_approval_gate(roots: Iterable[Path]):
    """Build the can_use_tool callback, closing over the allowed roots.

    Read-only tools skip the prompt only for paths inside those roots. Paths
    are resolved before the check, which neutralizes ``../`` and symlink
    escapes. Relative paths are anchored at the first root, the session's
    cwd, which is also where a tool that passes no path looks; Omni's own
    process cwd differs from it after --project or /workspace open.
    """
    resolved_roots = [Path(r).resolve() for r in roots]
    session_cwd = resolved_roots[0] if resolved_roots else Path.cwd()

    def _in_roots(raw: Any, base: Any = None) -> bool:
        if not raw:
            return True
        try:
            anchor = Path(session_cwd, str(base)) if base else session_cwd
            p = Path(anchor, str(raw)).resolve()
        except (OSError, ValueError):
            return False
        return any(p == r or p.is_relative_to(r) for r in resolved_roots)

    async def approval_gate(
        tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Prompt the user for any tool call not already allowed elsewhere."""
        if tool_name in AUTO_APPROVE_ALWAYS:
            return PermissionResultAllow()
        if tool_name == "Glob":
            if _in_roots(input_data.get("pattern"), base=input_data.get("path")):
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

# --- workspace and mcp configuration ----------------------------------------
def find_workspace(override: str | None = None) -> Path:
    """Directory the agent operates in: --project when given, else the cwd."""
    if override:
        return Path(override).resolve()
    return Path.cwd().resolve()


def load_mcp_servers() -> dict[str, Any]:
    """Named MCP servers for this session.

    Source order: the OMNI_MCP_SERVERS environment variable (a JSON object),
    then the "mcp_servers" key in ~/.omni/config.json. Each value is handed to
    the SDK unchanged, so any server shape the SDK accepts works here.
    """
    raw = os.environ.get("OMNI_MCP_SERVERS")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("OMNI_MCP_SERVERS is not valid JSON; ignoring it")
        else:
            if isinstance(parsed, dict):
                return parsed
            logger.warning("OMNI_MCP_SERVERS must be a JSON object; ignoring it")
    servers = config.get("mcp_servers", {})
    return servers if isinstance(servers, dict) else {}


def server_states(servers: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Map the SDK's MCP server status onto per-server UI states."""
    return {
        str(s.get("name")): (
            ui.STATE_CONNECTED
            if s.get("status") == "connected"
            else ui.STATE_DISCONNECTED
        )
        for s in servers
    }

# --- discovery --------------------------------------------------------------
_SETTING_SOURCES: tuple[str, ...] = ("user", "project", "local")

_DEFAULT_SOURCES = ["user", "project"]


def discovery_settings() -> dict[str, Any]:
    """How much of the host Claude Code configuration a session picks up.

    Omni shares Claude Code's MCP servers rather than hiding them. The servers
    in ~/.claude.json, a project .mcp.json and the settings files all load, so
    a relay already configured for Claude Code works here without being
    declared a second time. /tools reports whatever the session ends up with
    and marks anything that did not come from Omni's own config, which is what
    keeps the shared setup legible instead of surprising.

    Skills stay off. They were noise in the transcript rather than something
    Omni drives, and the model announcing skills it had inherited is what made
    the shared configuration confusing in the first place.

    Each key is overridable in ~/.omni/config.json:

    - ``setting_sources`` (default user + project) chooses which settings
      files load. ``[]`` loads none, which also drops CLAUDE.md.
    - ``strict_mcp_config`` (default false) set true restricts the session to
      the servers Omni passes in itself.
    - ``skills`` (default ``[]``) takes ``"all"`` for every discovered skill,
      or a list of names. The filter hides skills from the model but leaves
      their files readable through Read and Bash; it is not a sandbox.

    One consequence worth knowing: settings files carry permission rules as
    well as servers, and a host rule that allows a tool outright can satisfy
    the CLI without the call ever reaching Omni's approval gate. Set
    ``setting_sources`` to ``[]`` when the gate must see everything.
    """
    sources = config.get("setting_sources", _DEFAULT_SOURCES)
    if not isinstance(sources, list):
        logger.warning("config setting_sources must be a list; ignoring it")
        sources = _DEFAULT_SOURCES

    skills = config.get("skills", [])
    if not (skills == "all" or isinstance(skills, list)):
        logger.warning('config skills must be a list or "all"; ignoring it')
        skills = []

    return {
        "setting_sources": [s for s in sources if s in _SETTING_SOURCES],
        "strict_mcp_config": bool(config.get("strict_mcp_config", False)),
        "skills": skills,
    }


# --- session options --------------------------------------------------------
def build_options(
    mcp_servers: dict[str, Any] | None = None,
    workspace: Path | None = None,
    add_dirs: list[str | Path] | None = None,
    max_turns: int | None = None,
) -> ClaudeAgentOptions:
    """Assemble the SDK options for a session.

    The system prompt is preset+append so the default prompt survives rather
    than being replaced. The permission mode is pinned to "default" so host
    settings cannot widen permissions underneath the approval gate.
    Checkpointing plus replayed UserMessage uuids are what /rewind runs on.

    Omni shares the host Claude Code MCP configuration; see
    :func:`discovery_settings` for how much is inherited and how to narrow it.
    """
    return ClaudeAgentOptions(
        **discovery_settings(),
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                "You are running inside Omni, a general-purpose automation "
                "harness, working in the user's chosen directory. Omni is the "
                "application, not your identity: you are still the underlying "
                "model, so answer as that model and never claim to be Omni. "
                "Before acting: state your plan briefly. "
                "Prefer small, verifiable steps, and check your own work as you go. "
                "When an MCP tool exists for a change, prefer it over editing the "
                "underlying files by hand."
            ),
        },
        mcp_servers=mcp_servers or {},
        cwd=workspace,
        add_dirs=add_dirs or [],
        permission_mode="default",
        can_use_tool=make_approval_gate(
            [Path(p) for p in [workspace, *(add_dirs or [])] if p]
        ),
        enable_file_checkpointing=True,
        extra_args={"replay-user-messages": None},
        max_turns=max_turns,
    )

# --- mcp commands -----------------------------------------------------------
_PERMISSION_MODES: tuple[str, ...] = get_args(PermissionMode)
_GATE_BYPASSING_MODES = {"bypassPermissions", "auto"}

def _targets(servers: Mapping[str, Any], arg: str) -> list[str]:
    """Server names to act on, chosen from what Omni configured.

    Used by /reconnect, which has to be able to name a server that Omni
    declared but the session never brought up; such a server is missing from
    the SDK's list precisely because it is the one that needs reconnecting.
    """
    if not servers:
        ui.note("No MCP servers configured.")
        ui.note(f'  (set OMNI_MCP_SERVERS, or add "mcp_servers" to {config.path()})')
        return []
    name = arg.strip()
    if not name:
        return list(servers)
    if name not in servers:
        ui.error(f"Unknown MCP server {name!r} — configured: {', '.join(servers)}.")
        return []
    return [name]

async def _reported_servers(client: ClaudeSDKClient) -> dict[str, Any] | None:
    """The MCP servers the session actually has, by name; None if the probe fails.

    This is the SDK's own view rather than Omni's config. The two can disagree
    whenever the isolation settings are relaxed, and a command that reports the
    config instead of this is how inherited servers stayed invisible.
    """
    try:
        status = await client.get_mcp_status()
    except Exception as exc:
        logger.exception("MCP status check failed")
        ui.error(f"(status check failed: {exc})")
        return None
    return {str(s.get("name")): s for s in status["mcpServers"]}

def _live_targets(reported: Mapping[str, Any], arg: str) -> list[str]:
    """Server names to act on, chosen from what the session reports."""
    name = arg.strip()
    if not name:
        return list(reported)
    if name not in reported:
        known = ", ".join(reported) or "none"
        ui.error(f"Unknown MCP server {name!r} — this session has: {known}.")
        return []
    return [name]

async def _refresh(client: ClaudeSDKClient) -> None:
    """Re-read MCP status and repaint the status lines."""
    reported = await _reported_servers(client)
    if reported is None:
        return
    ui.mcp_update(server_states(reported.values()))

async def cmd_reconnect(
    client: ClaudeSDKClient, servers: Mapping[str, Any], arg: str
) -> None:
    """Reconnect an MCP server: /reconnect [name] (default: every server)."""
    targets = _targets(servers, arg)
    if not targets:
        return
    for name in targets:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                ui.note(f"Reconnecting {name}… (attempt {attempt}/3)")
                await client.reconnect_mcp_server(name)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "reconnect %s attempt %d/3 failed", name, attempt, exc_info=True
                )
                if attempt < 3:
                    await asyncio.sleep(1.5 * attempt)
        if last_exc is not None:
            logger.error(
                "reconnect %s failed after 3 attempts", name, exc_info=last_exc
            )
            ui.error(f"({name}: reconnect failed: {last_exc})")
    await asyncio.sleep(1.0)
    await _refresh(client)

async def cmd_test(
    client: ClaudeSDKClient, servers: Mapping[str, Any], arg: str
) -> None:
    """Probe the MCP connections without reconnecting.

    Reports whatever the session has, not what Omni configured, so a server
    inherited from elsewhere shows up here instead of hiding behind an empty
    config. ``ui.mcp_update`` prints its own line when there are none.
    """
    await _refresh(client)

def _tool_names(server: Mapping[str, Any]) -> list[str]:
    """Pull tool names out of an mcpServers entry; tools may be dicts or strs."""
    names = []
    for tool in server.get("tools", []) or []:
        if isinstance(tool, Mapping):
            names.append(str(tool.get("name", tool)))
        else:
            names.append(str(tool))
    return names

async def cmd_tools(
    client: ClaudeSDKClient, servers: Mapping[str, Any], arg: str
) -> None:
    """List the tools an MCP server exposes: /tools [name].

    Lists every server the session has, whether or not Omni configured it, so
    the model and this command can never describe different sets of tools.
    """
    reported = await _reported_servers(client)
    if reported is None:
        return
    if not reported:
        ui.note("The session has no MCP servers.")
        ui.note(f'  (set OMNI_MCP_SERVERS, or add "mcp_servers" to {config.path()})')
        return
    unconfigured = [n for n in reported if n not in servers]
    if unconfigured:
        ui.note(f"  inherited, not from Omni's config: {', '.join(unconfigured)}")
    for name in _live_targets(reported, arg):
        server = reported[name]
        if server.get("status") != "connected":
            ui.note(f"{name} is {server.get('status')}; no tools to list.")
        else:
            ui.tool_list(name, _tool_names(server))

# --- session commands -------------------------------------------------------
async def cmd_clear(client: ClaudeSDKClient, servers: Mapping[str, Any], arg: str) -> None:
    """Clear the transcript pane (display only; conversation is untouched)."""
    ui.clear()

async def cmd_model(client: ClaudeSDKClient, servers: Mapping[str, Any], arg: str) -> None:
    """Switch the model mid-session, e.g. `/model claude-opus-4-8`."""
    model = arg.strip()
    if not model:
        ui.error("Usage: /model <name> (e.g. /model claude-sonnet-x-x).")
        return
    try:
        await client.set_model(model)
        ui.set_model_label(model)
        ui.note(f"Model set to {model}.")
    except Exception as exc:
        logger.exception("set_model(%r) failed", model)
        ui.error(f"(could not set model: {exc})")

async def cmd_mode(client: ClaudeSDKClient, servers: Mapping[str, Any], arg: str) -> None:
    """Change the permission mode, e.g. `/mode acceptEdits`."""
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

async def cmd_rewind(client: ClaudeSDKClient, servers: Mapping[str, Any], arg: str) -> None:
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

# --- workspace commands -----------------------------------------------------
_workspace: Workspace | None = None

class _SwitchWorkspace(Exception):
    """Raised by /workspace open to end this session and start one in ``target``."""

    def __init__(self, target: Workspace) -> None:
        super().__init__(target.label())
        self.target = target

async def confirm_trust(workspace: Workspace) -> bool:
    """Ask before a session starts in a folder the user has not trusted yet.

    This has to happen before the client connects, because connecting is what
    loads the folder's own settings: a project .mcp.json or
    .claude/settings.json can start MCP servers and hooks, and both run code.
    The SDK drives the CLI headless, which skips Claude Code's own trust
    dialog, so Omni asks instead. Anything but yes, including ctrl-d, is no.
    """
    if workspace_manage.is_trusted(workspace.root):
        return True
    ui.trust_prompt(workspace.root)
    try:
        answer = (await ui.ask_line("trust this folder? (y/N) › ")).strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes"}:
        return False
    try:
        remembered = workspace_manage.trust(workspace.root)
    except OSError as exc:
        logger.warning("could not record trust for %s", workspace.root, exc_info=True)
        ui.error(f"(could not remember this answer: {exc})")
        return True
    if not remembered:
        ui.note("Trusted for this session only: a drive root or home is never remembered.")
    return True

_WORKSPACE_USAGE = (
    "Usage: /workspace [list | open <name|dir> | save <name> | remove <name>]."
)

async def cmd_workspace(
    client: ClaudeSDKClient, servers: Mapping[str, Any], arg: str
) -> None:
    """List, open, save or remove workspaces: /workspace [sub] [target].

    Everything after the subcommand is one target, so a directory containing
    spaces works unquoted; surrounding quotes are stripped for those who add
    them anyway.
    """
    global _workspace
    current = _workspace
    if current is None:
        ui.error("No workspace is active.")
        return
    sub, _, rest = arg.partition(" ")
    sub = sub.lower()
    target = rest.strip().strip("\"'")

    if sub in {"", "list"}:
        ui.workspaces(current, workspace_manage.saved(), workspace_manage.recent())
    elif sub == "open" and target:
        try:
            chosen = workspace_manage.resolve(target, base=current.root)
        except WorkspaceError as exc:
            ui.error(str(exc))
            return
        if chosen.roots == current.roots:
            ui.note("Already in that workspace.")
            return
        if not await confirm_trust(chosen):
            ui.note(f"Not trusted; staying in {current.label()}.")
            return
        ui.note(f"Opening {chosen.label()} — this starts a new conversation…")
        raise _SwitchWorkspace(chosen)
    elif sub == "save" and target:
        try:
            _workspace = workspace_manage.save(target, current)
        except (WorkspaceError, OSError) as exc:
            ui.error(f"(could not save workspace: {exc})")
            return
        ui.note(
            f"Saved {current.root} as {target!r}; reopen it with "
            f"/workspace open {target} or omni -w {target}."
        )
    elif sub == "remove" and target:
        try:
            removed = workspace_manage.remove(target)
        except OSError as exc:
            ui.error(f"(could not remove workspace: {exc})")
            return
        if removed:
            ui.note(f"Removed saved workspace {target!r}.")
        else:
            ui.error(f"No saved workspace {target!r}.")
    else:
        ui.error(_WORKSPACE_USAGE)

# --- command registry -------------------------------------------------------
COMMANDS: dict[str, Any] = {
    "test": cmd_test,
    "tools": cmd_tools,
    "clear": cmd_clear,
    "model": cmd_model,
    "mode": cmd_mode,
    "rewind": cmd_rewind,
    "workspace": cmd_workspace,
    "reconnect": cmd_reconnect,
    "restart": cmd_reconnect,
}

COMMANDS_HELP = [
    ("/test", "probe the configured MCP connections"),
    ("/tools [name]", "list the tools an MCP server exposes"),
    ("/clear", "clear the transcript pane (display only)"),
    ("/model <name>", "switch the model mid-session"),
    ("/mode <mode>", "change permission mode (default/acceptEdits/plan/…)"),
    ("/rewind", "restore files to their state at your last message"),
    ("/workspace", "show the current, saved and recent workspaces"),
    ("/workspace open <name|dir>", "switch workspace (starts a new conversation)"),
    ("/workspace save <name>", "save this workspace under a name"),
    ("/workspace remove <name>", "forget a saved workspace"),
    ("/reconnect [name]", "reconnect an MCP server"),
    ("/restart", "alias for /reconnect"),
    ("/exit, /quit", "leave Omni"),
]

# --- repl -------------------------------------------------------------------
_checkpoints: list[str] = []

def _is_prompt_replay(message: UserMessage) -> bool:
    """True for a replayed user prompt; tool results also arrive as UserMessages."""
    if not message.uuid or message.parent_tool_use_id or message.tool_use_result:
        return False
    if isinstance(message.content, str):
        return True
    return not any(isinstance(b, ToolResultBlock) for b in message.content)

async def drain(client: ClaudeSDKClient) -> None:
    """Print one full response (until ResultMessage)."""
    async for message in client.receive_response():
        if isinstance(message, UserMessage):
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

async def main(
    workspace: Workspace | None = None, max_turns: int | None = None
) -> None:
    """Run sessions until the user quits; /workspace open starts the next one.

    Declining to trust the starting folder ends Omni before anything loads;
    /workspace open asks the same question before it hands a switch back here.
    """
    servers = load_mcp_servers()
    current = workspace or Workspace(find_workspace())
    if not await confirm_trust(current):
        logger.info("session declined: %s is not trusted", current.root)
        return
    first = True
    while current is not None:
        current = await _session(current, servers, max_turns, first)
        first = False

async def _session(
    workspace: Workspace,
    servers: dict[str, Any],
    max_turns: int | None,
    first: bool,
) -> Workspace | None:
    """Run one SDK session in ``workspace``.

    Returns the workspace /workspace open chose, or None when the user quits.
    The SDK fixes ``cwd`` and ``add_dirs`` when the client connects, and the
    approval gate closes over its roots, so a switch cannot be applied in
    place: this client is torn down and main() connects a fresh one, which
    also means a fresh conversation. Checkpoints belong to the old client,
    so they go with it.
    """
    global _workspace
    _workspace = workspace
    _checkpoints.clear()
    workspace_manage.remember(workspace)
    options = build_options(
        servers, workspace.root, [*workspace.add_dirs], max_turns
    )
    logger.info(
        "session start: workspace=%s add_dirs=%s mcp_servers=%s",
        workspace.root,
        [str(d) for d in workspace.add_dirs],
        list(servers),
    )

    async with ClaudeSDKClient(options=options) as client:
        ui.set_interrupt_handler(client.interrupt)
        ui.set_commands(COMMANDS_HELP)
        ui.set_model_label(options.model)
        states: dict[str, str] = {}
        if servers:
            try:
                status = await client.get_mcp_status()
                states = server_states(status["mcpServers"])
            except Exception as exc:
                logger.exception("startup MCP status check failed")
                ui.error(f"(status check failed: {exc})")
                states = {name: ui.STATE_DISCONNECTED for name in servers}
        if first:
            ui.welcome(options.model or "default model", workspace.label())
        ui.header(states, workspace.label(), workspace.add_dirs)

        while True:
            try:
                prompt = (await ui.ask_task()).strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not prompt:
                continue
            ui.user_prompt(prompt)

            if prompt.lower().split(maxsplit=1)[0] in {"/exit", "/quit"}:
                return None
            if prompt.startswith("/"):
                name, _, arg = prompt.partition(" ")
                handler = COMMANDS.get(name.lower().lstrip("/"))
                if handler is None:
                    ui.error(f"Unknown command {prompt!r} — press ? for the list.")
                else:
                    ui.busy_start()
                    try:
                        await handler(client, servers, arg.strip())
                    except _SwitchWorkspace as switch:
                        return switch.target
                    finally:
                        ui.busy_stop()
                continue

            ui.busy_start()
            try:
                await client.query(prompt)
                await drain(client)
            except Exception as exc:
                logger.exception("task failed: %r", prompt)
                ui.error(f"(task failed: {exc})")
            finally:
                ui.busy_stop()

# --- entry point ------------------------------------------------------------
async def _amain(workspace: Workspace | None, max_turns: int | None = None) -> None:
    """Run the full-screen UI and the agent loop concurrently."""
    ui.start()
    app_task = asyncio.create_task(ui.run_async())
    try:
        await main(workspace, max_turns)
    finally:
        ui.stop()
        await app_task

def run() -> None:
    """Sync entry point for ``python -m project_omni`` and the console script."""
    parser = argparse.ArgumentParser(
        prog="omni",
        description="General-purpose automation agent with approval gating",
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument(
        "--project",
        metavar="DIR",
        default=None,
        help="Working directory for this session (default: the current directory).",
    )
    where.add_argument(
        "-w",
        "--workspace",
        metavar="NAME",
        default=None,
        help="Open a saved workspace (save one with /workspace save <name>).",
    )
    parser.add_argument(
        "--add-dir",
        metavar="DIR",
        action="append",
        default=None,
        help="Grant the agent access to an extra directory beyond the working "
        "directory. Repeatable.",
    )
    parser.add_argument(
        "--max-turns",
        metavar="N",
        type=int,
        default=None,
        help="Stop a task after N agent turns (default: no limit).",
    )

    args = parser.parse_args()

    if args.project:
        p = Path(args.project)
        if not p.is_dir():
            print(f"error: --project '{args.project}' is not a directory")
            sys.exit(1)

    add_dirs: list[Path] = []
    for raw in args.add_dir or []:
        p = Path(raw)
        if not p.is_dir():
            print(f"warning: --add-dir '{raw}' is not a directory; ignoring")
            continue
        resolved = p.resolve()
        if workspace_manage.is_too_broad(resolved):
            print(f"error: --add-dir '{resolved}' is too broad (drive root or home)")
            sys.exit(1)
        add_dirs.append(resolved)

    log_file = log.setup()
    if args.workspace:
        try:
            workspace = workspace_manage.load(args.workspace)
        except WorkspaceError as exc:
            print(f"error: {exc}")
            sys.exit(1)
    else:
        workspace = Workspace(find_workspace(args.project))
    workspace = workspace.granting(add_dirs)
    try:
        asyncio.run(_amain(workspace, args.max_turns))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.exception("fatal error")
        print(f"fatal: {exc}  (traceback in {log_file})")
        sys.exit(1)
