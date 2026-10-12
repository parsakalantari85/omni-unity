from __future__ import annotations

import asyncio
import difflib
import io
import json
import textwrap
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Awaitable, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.data_structures import Point
from prompt_toolkit.shortcuts import set_title
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.json import JSON
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.table import Table

from . import config, log
from .workspace_manage import Workspace

_logger = log.get("ui")


# --- configuration ----------------------------------------------------------

APP_NAME = "OMNI"

ACCENT = "#57a9d9"
PREVIEW_LIMIT = 2000


def _tunable(key, default, cast):
    """Read a UI knob from ~/.omni/config.json, ignoring unusable values."""
    try:
        return cast(config.get(key, default))
    except (TypeError, ValueError):
        return default


BUSY_INTERVAL = _tunable("busy_interval", 1.0, float)
BUSY_DOTS = max(1, _tunable("busy_dots", 1, int))

_TASK_HINTS = (
    ("enter", "send the task"),
    ("PgUp/PgDn", "scroll the transcript"),
    ("ctrl-c", "interrupt a running task"),
)
_APPROVE_HINTS = (
    ("a", "approve"),
    ("d", "deny"),
    ("e", "edit the input first"),
    ("f", "show the full payload"),
    ("q", "deny and stop the task"),
)
_LINE_HINTS = (
    ("enter", "submit"),
    ("ctrl-d", "cancel"),
)

_style = Style.from_dict(
    {
        "prompt": f"bold {ACCENT}",
        "box.border": "#3a3a3a",
        "overlay.title": f"bold {ACCENT}",
        "overlay.key": ACCENT,
        "overlay.desc": "#888888",
        "overlay.foot": "#666666",
        "busy.on": f"bold {ACCENT}",
        "busy.label": "#888888",
    }
)


# --- state ------------------------------------------------------------------

_blocks: list[Any] = []
_prompt_label = "❯ "
_mode_hints: tuple[tuple[str, str], ...] = _TASK_HINTS
_model_label = "Claude"

_commands: list[tuple[str, str]] = []
_overlay_visible = False
_pending: Any = None
_interrupt_handler: Callable[[], Awaitable[None]] | None = None

_busy = False
_busy_frame = 0
_busy_since = 0.0
_busy_task: Any = None

_app: Application[Any] | None = None


# --- rendering --------------------------------------------------------------

def _invalidate() -> None:
    if _app is not None:
        try:
            _app.invalidate()
        except Exception:
            pass


def _term_width() -> int:
    """Inner width available to rich.

    Asked of prompt_toolkit rather than left to rich's own detection, which
    falls back to 79 columns whenever it can't inspect the terminal.
    """
    if _app is not None:
        try:
            return max(40, _app.output.get_size().columns)
        except Exception:
            pass
    return 96


def _emit(renderable: Any) -> None:
    """Render a rich renderable to ANSI and append it to the output pane."""
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor", width=_term_width()
    ).print(renderable)
    stick = _at_bottom()
    _blocks.append(to_formatted_text(ANSI(buf.getvalue())))
    if stick:
        _output_window.vertical_scroll = _content_lines()
    _invalidate()


def _combined() -> list[Any]:
    out: list[Any] = []
    for block in _blocks:
        out.extend(block)
    return out


# --- layout -----------------------------------------------------------------

def _content_lines() -> int:
    """Index of the last content line (== total newlines across all blocks)."""
    return sum(frag[1].count("\n") for frag in _combined())


def _cursor_point() -> Point:
    """Pin the invisible cursor to the top line so our ``vertical_scroll``
    wins over prompt_toolkit's keep-cursor-visible scrolling."""
    return Point(x=0, y=min(_output_window.vertical_scroll, _content_lines()))


_output_window = Window(
    FormattedTextControl(
        text=_combined,
        focusable=False,
        show_cursor=False,
        get_cursor_position=_cursor_point,
    ),
    wrap_lines=True,
)


def _at_bottom() -> bool:
    """True when the last content line is visible (or nothing rendered yet)."""
    info = _output_window.render_info
    if info is None:
        return True
    return info.last_visible_line() >= info.ui_content.line_count - 1


def _accept(buff: Buffer) -> bool:
    if _pending is not None and not _pending.done():
        _pending.set_result(buff.text)
    return False


def _close_overlay(_: Buffer | None = None) -> None:
    """Typing anything dismisses the overlay."""
    global _overlay_visible
    if _overlay_visible:
        _overlay_visible = False
        _invalidate()


def _overlay_text() -> list[Any]:
    """Rows for the `?` overlay: slash commands (task mode) plus mode keys."""
    show_commands = _mode_hints is _TASK_HINTS and _commands
    pad = max(
        [len(k) for k, _ in _mode_hints]
        + ([len(n) for n, _ in _commands] if show_commands else [])
    )
    out: list[Any] = []

    def row(left: str, right: str) -> None:
        out.append(("class:overlay.key", f"  {left.ljust(pad)}"))
        out.append(("class:overlay.desc", f"   {right}\n"))

    if show_commands:
        out.append(("class:overlay.title", "  commands\n"))
        for name, desc in _commands:
            row(name, desc)
        out.append(("", "\n"))
    out.append(("class:overlay.title", "  keys\n"))
    for key, desc in _mode_hints:
        row(key, desc)
    out.append(("class:overlay.foot", "\n  ? or esc to close\n"))
    return out


_overlay_window = ConditionalContainer(
    Window(
        FormattedTextControl(_overlay_text, focusable=False, show_cursor=False),
        dont_extend_height=True,
    ),
    filter=Condition(lambda: _overlay_visible),
)


def _busy_text() -> list[Any]:
    """One line: the lit dots, cycling, plus how long we have waited.

    The cycle runs over ``BUSY_DOTS + 1`` steps so it includes an all-dark
    beat. That is what lets a single dot blink instead of sitting lit.
    """
    lit = (_busy_frame + 1) % (BUSY_DOTS + 1)
    out: list[Any] = [("", "  ")]
    for i in range(BUSY_DOTS):
        out.append(("class:busy.on", "● " if i < lit else "  "))
    out.append(("class:busy.label", "working"))
    secs = int(time.monotonic() - _busy_since) if _busy_since else 0
    if secs:
        out.append(("class:busy.label", f"  {secs}s"))
    out.append(("class:busy.label", "   ctrl-c to interrupt"))
    return out


_busy_window = ConditionalContainer(
    Window(
        FormattedTextControl(_busy_text, focusable=False, show_cursor=False),
        height=1,
    ),
    filter=Condition(lambda: _busy and _pending is None),
)


_input_buffer = Buffer(
    accept_handler=_accept, multiline=False, on_text_changed=_close_overlay
)
_input_window = Window(
    BufferControl(
        buffer=_input_buffer,
        input_processors=[BeforeInput(lambda: [("class:prompt", _prompt_label)])],
    ),
    height=1,
)

# The input sits inside a closed box rather than under a bare divider, so the
# framing is the same on all four sides. ``BOX_PAD`` is the gutter between the
# border and the text, applied left and right so the prompt is not flush
# against the edge.
BOX_PAD = 1

_BORDER = "class:box.border"


def _box_edge(left: str, right: str) -> VSplit:
    """A horizontal edge of the input box: corner, flexible fill, corner.

    Built from three windows rather than one width-aware string so
    prompt_toolkit sizes the fill itself; a string we measured ourselves
    would wrap and shift the layout whenever our width was off by one.
    """
    return VSplit(
        [
            Window(width=1, height=1, char=left, style=_BORDER),
            Window(height=1, char="─", style=_BORDER),
            Window(width=1, height=1, char=right, style=_BORDER),
        ]
    )


_input_box = HSplit(
    [
        _box_edge("╭", "╮"),
        VSplit(
            [
                Window(width=1, char="│", style=_BORDER),
                Window(width=BOX_PAD),
                _input_window,
                Window(width=BOX_PAD),
                Window(width=1, char="│", style=_BORDER),
            ],
            height=1,
        ),
        _box_edge("╰", "╯"),
    ]
)

_root = HSplit(
    [
        _output_window,
        _overlay_window,
        _busy_window,
        _input_box,
    ]
)


# --- key bindings -----------------------------------------------------------

_kb = KeyBindings()


@_kb.add("c-c")
def _(event: Any) -> None:
    """Interrupt a running task."""
    if _interrupt_handler is not None:
        event.app.create_background_task(_run_interrupt())


@_kb.add("?", filter=Condition(lambda: not _input_buffer.text))
def _(event: Any) -> None:
    """Toggle the command overlay — only when the input line is empty, so a
    literal ``?`` can still be typed mid-sentence."""
    global _overlay_visible
    _overlay_visible = not _overlay_visible
    _invalidate()


@_kb.add("escape", filter=Condition(lambda: _overlay_visible), eager=True)
def _(event: Any) -> None:
    """Dismiss the overlay."""
    _close_overlay()


@_kb.add("c-d")
def _(event: Any) -> None:
    """EOF: fail the pending ask_* (read as quit/deny)."""
    if _pending is not None and not _pending.done():
        _pending.set_exception(EOFError())


def _visible_span(info: Any) -> int:
    """Visible content lines (not window rows), so paging accounts for wrapping."""
    return max(1, info.last_visible_line() - info.first_visible_line())


@_kb.add("pageup")
def _(event: Any) -> None:
    info = _output_window.render_info
    if info is None:
        return
    _output_window.vertical_scroll = max(0, info.vertical_scroll - _visible_span(info))
    _invalidate()


@_kb.add("pagedown")
def _(event: Any) -> None:
    info = _output_window.render_info
    if info is None:
        return
    _output_window.vertical_scroll = info.vertical_scroll + _visible_span(info)
    _invalidate()


async def _run_interrupt() -> None:
    note("[interrupting current task…]")
    try:
        if _interrupt_handler is not None:
            await _interrupt_handler()
    except Exception as exc:
        _logger.exception("interrupt failed")
        error(f"(interrupt failed: {exc})")


# --- lifecycle --------------------------------------------------------------

def set_interrupt_handler(handler: Callable[[], Awaitable[None]]) -> None:
    global _interrupt_handler
    _interrupt_handler = handler


def set_commands(items: list[tuple[str, str]]) -> None:
    """Register the slash-command list shown in the `?` overlay."""
    global _commands
    _commands = list(items)


def _friendly_model(model: str | None) -> str:
    """Vendor-family name for a model id; the raw id if it is unrecognised."""
    if not model:
        return "Claude"
    head = model.split("/")[-1].lower()
    for prefix, label in (("claude", "Claude"), ("gpt", "GPT"), ("o1", "GPT"), ("o3", "GPT")):
        if head.startswith(prefix):
            return label
    return model


async def _busy_loop() -> None:
    """Advance the indicator until busy_stop() flips _busy or cancels us."""
    global _busy_frame
    try:
        while _busy:
            await asyncio.sleep(BUSY_INTERVAL)
            _busy_frame += 1
            _invalidate()
    except asyncio.CancelledError:
        pass


def busy_start() -> None:
    """Show the working indicator until busy_stop(). Safe to call twice."""
    global _busy, _busy_frame, _busy_since, _busy_task
    if _busy:
        return
    _busy, _busy_frame, _busy_since = True, 0, time.monotonic()
    try:
        _busy_task = asyncio.get_running_loop().create_task(_busy_loop())
    except RuntimeError:
        _busy_task = None
    _invalidate()


def busy_stop() -> None:
    """Hide the working indicator."""
    global _busy, _busy_task
    _busy = False
    if _busy_task is not None:
        _busy_task.cancel()
        _busy_task = None
    _invalidate()


def set_model_label(model: str | None) -> None:
    """Set the name the agent's turns are labelled with."""
    global _model_label
    _model_label = _friendly_model(model)


def start() -> Application[Any]:
    global _app
    set_title(APP_NAME)
    if _app is None:
        _app = Application(
            layout=Layout(_root, focused_element=_input_window),
            key_bindings=_kb,
            style=_style,
            full_screen=True,
            mouse_support=True,
        )
    return _app


async def run_async() -> None:
    await start().run_async()


def stop() -> None:
    if _app is not None and _app.is_running:
        _app.exit()


# --- helpers ----------------------------------------------------------------

def _tool_text(name: str) -> Text:
    """``mcp__github__create_issue`` -> ``create_issue  github`` (styled)."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            _, server, tool = parts
            t = Text(tool)
            t.append(f"  {server}", style="dim")
            return t
    return Text(name)


def _tool_plain(name: str) -> str:
    """Bracket-safe label for panel titles."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[2]} ({parts[1]})"
    return name


# --- mcp status -------------------------------------------------------------

STATE_CONNECTED = "connected"
STATE_INTERRUPTED = "interrupted"
STATE_DISCONNECTED = "disconnected"
STATE_NONE = "no_servers"

_STATE_STYLES = {
    STATE_CONNECTED: "green",
    STATE_INTERRUPTED: "yellow",
    STATE_DISCONNECTED: "red",
    STATE_NONE: "white",
}

_STATE_LABELS = {
    STATE_CONNECTED: "connected",
    STATE_INTERRUPTED: "interrupted",
    STATE_DISCONNECTED: "disconnected",
    STATE_NONE: "not configured",
}

_STATE_HINTS = {
    STATE_DISCONNECTED: (
        "→ The server isn't responding — check that it's running,",
        "  then type /reconnect <name>.",
    ),
    STATE_INTERRUPTED: (
        "→ The connection dropped — check the server, then /reconnect <name>.",
    ),
}


def _server_line(name: str, state: str) -> Text:
    style = _STATE_STYLES.get(state, "yellow")
    line = Text()
    line.append("● ", style=style)
    line.append(f"{name} ", style="bold")
    line.append(_STATE_LABELS.get(state, state), style=style)
    return line


def _state_hints(state: str) -> None:
    for hint in _STATE_HINTS.get(state, ()):
        _emit(Text(f"  {hint}", style="yellow"))


# --- output -----------------------------------------------------------------

def welcome(model: str, project: Any) -> None:
    """One-shot startup banner; scrolls away as the transcript grows."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="center")
    grid.add_column()
    left = Text.from_markup("[bold]Welcome back![/bold]\n\n", justify="center")
    left.append(f"{model}\n")
    left.append(str(project or "(no project)"), style="dim")
    right = Text.from_markup(
        "[bold]Getting started[/bold]\n"
        "Type a task, or press [bold]?[/bold] for commands.\n\n"
    )
    grid.add_row(left, right)
    _emit(Panel(grid, title=APP_NAME, title_align="left", border_style=ACCENT, padding=(0, 1)))


def trust_prompt(root: Any) -> None:
    """The folder-trust question, shown before a session starts somewhere new."""
    body = Text()
    body.append(f"{root}\n\n", style="bold")
    body.append(
        "Omni will read, edit and run files in this folder, and load the "
        "folder's own Claude settings: its .mcp.json and .claude/settings.json "
        "can start MCP servers and hooks, which run code.\n\n"
        "Only continue if you trust what is in it."
    )
    _emit(Text(""))
    _emit(
        Panel(
            body,
            title="Do you trust the files in this folder?",
            title_align="left",
            border_style="yellow",
            padding=(0, 1),
        )
    )


def header(
    states: dict[str, str] | None = None,
    workspace: Any = None,
    extra_dirs: Iterable[Any] = (),
) -> None:
    for name, state in (states or {}).items():
        _emit(_server_line(name, state))
        _state_hints(state)
    _emit(Text(f"  workspace  {workspace or Path.cwd()}", style="dim"))
    for extra in extra_dirs:
        _emit(Text(f"  also       {extra}", style="dim"))
    _emit(
        Text.from_markup(
            "Type a task, press [bold]?[/bold] for commands, "
            "or [bold]/exit[/bold] to quit.",
            style="dim",
        )
    )


def mcp_update(states: dict[str, str]) -> None:
    """Re-print the MCP status lines (after a reconnect or a probe)."""
    if not states:
        _emit(Text("  no MCP servers configured", style="dim"))
        return
    for name, state in states.items():
        _emit(_server_line(name, state))
        _state_hints(state)


def user_prompt(text: str) -> None:
    """Echo submitted input into the scroll pane (the input box clears on submit).

    A labelled block rather than a bare "❯ text" line: the label keeps the turn
    boundary legible in a long transcript, and indenting the body keeps a
    multi-line prompt aligned instead of falling back to column zero. Not the
    accent colour on purpose — that reads as Omni's own voice.
    """
    _emit(Text(""))
    _emit(Text("❯ You", style="bold"))
    _emit(Text(textwrap.indent(text, "  ")))


def assistant(text: str) -> None:
    _emit(Text(""))
    _emit(Text(f"● {_model_label}", style=f"bold {ACCENT}"))
    _emit(Markdown(text))


# --- tool previews ----------------------------------------------------------

_DIFF_LINE_LIMIT = 80


def _preview_edit(d: dict[str, Any]) -> None:
    fpath = d.get("file_path", "?")
    old = d.get("old_string", "")
    new = d.get("new_string", "")
    _emit(Text(f"  {fpath}", style="dim"))
    diff = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=Path(fpath).name,
            tofile=Path(fpath).name,
            n=2,
        )
    )
    if not diff:
        return
    shown = diff[:_DIFF_LINE_LIMIT]
    snippet = "".join(shown)
    if len(diff) > _DIFF_LINE_LIMIT:
        snippet += f"\n… {len(diff) - _DIFF_LINE_LIMIT} more lines hidden"
    _emit(Syntax(snippet, "diff", theme="ansi_dark", line_numbers=False, padding=(0, 1)))


def _preview_write(d: dict[str, Any]) -> None:
    fpath = d.get("file_path", "?")
    content = d.get("content", "")
    _emit(Text(f"  {fpath}", style="dim"))
    lines = content.splitlines(keepends=True)
    shown = lines[:30]
    snippet = "".join(shown)
    if len(lines) > 30:
        snippet += f"\n… {len(lines) - 30} more lines"
    ext = Path(fpath).suffix.lstrip(".") or "text"
    _emit(Syntax(snippet, ext, theme="ansi_dark", line_numbers=True, padding=(0, 1)))


def _preview_bash(d: dict[str, Any]) -> None:
    cmd = d.get("command") or d.get("cmd") or ""
    if cmd:
        _emit(Syntax(cmd, "bash", theme="ansi_dark", padding=(0, 1)))


def _preview_mcp(name: str, d: dict[str, Any]) -> None:
    pairs = [
        f"{k}={json.dumps(v) if not isinstance(v, str) else v}"
        for k, v in list(d.items())[:4]
        if not isinstance(v, (dict, list)) or len(json.dumps(v)) < 120
    ]
    if pairs:
        _emit(Text("  " + "  ·  ".join(pairs), style="dim"))


def _tool_preview(name: str, d: dict[str, Any]) -> None:
    if name == "Edit":
        _preview_edit(d)
    elif name == "Write":
        _preview_write(d)
    elif name in ("Bash",):
        _preview_bash(d)
    elif name in ("Read", "Glob", "Grep"):
        path = d.get("file_path") or d.get("pattern") or d.get("path") or ""
        if path:
            _emit(Text(f"  {path}", style="dim"))
    elif name.startswith("mcp__"):
        _preview_mcp(name, d)


def tool_request(name: str, input_data: dict[str, Any] | None = None) -> None:
    line = Text("● ", style=f"bold {ACCENT}")
    line.append(_tool_text(name))
    _emit(line)
    if input_data:
        _tool_preview(name, input_data)


# --- approvals --------------------------------------------------------------

def approval(tool_name: str, input_data: dict[str, Any], title: str | None) -> None:
    _emit(Text(""))
    if title:
        _emit(Text(title, style="dim"))
    payload = json.dumps(input_data, indent=2)
    if len(payload) > PREVIEW_LIMIT:
        hidden = len(payload) - PREVIEW_LIMIT
        body: Any = Text(
            payload[:PREVIEW_LIMIT]
            + f"\n… {hidden} more chars hidden — press [f] for the full payload"
        )
    else:
        body = JSON(payload)
    _emit(
        Panel(
            body,
            title=f"approve · {_tool_plain(tool_name)}",
            border_style="yellow",
            expand=False,
        )
    )


def full_payload(input_data: dict[str, Any]) -> None:
    _emit(JSON(json.dumps(input_data, indent=2)))


# --- transcript -------------------------------------------------------------

def turn_done(num_turns: int, cost: float | None, error_msg: str | None) -> None:
    price = f"${cost:.4f}" if cost is not None else "n/a"
    line = Text("⎿ done", style="dim")
    line.append(f" · {num_turns} turns · {price}", style="dim")
    if error_msg:
        line.append(f" · ERROR: {error_msg}", style="bold red")
    _emit(Text(""))
    _emit(line)


def tool_list(server: str, names: list[str]) -> None:
    """Render the tools an MCP server exposes."""
    _emit(Text(""))
    line = Text(f"{server} tools", style=f"bold {ACCENT}")
    line.append(f"  ({len(names)})", style="dim")
    _emit(line)
    if not names:
        _emit(Text("  (none reported)", style="dim"))
        return
    for name in names:
        _emit(Text(f"  • {name}", style="dim"))


def workspaces(
    current: Workspace, saved: Mapping[str, Workspace], recent: list[Path]
) -> None:
    """Render /workspace: this session's workspace, the saved ones, recent roots."""
    _emit(Text(""))
    line = Text("workspace  ", style=f"bold {ACCENT}")
    line.append(current.label())
    _emit(line)
    for extra in current.add_dirs:
        _emit(Text(f"  also  {extra}", style="dim"))

    _emit(Text(""))
    _emit(Text("saved", style=f"bold {ACCENT}"))
    if not saved:
        _emit(Text("  (none yet — /workspace save <name> saves this one)", style="dim"))
    pad = max((len(name) for name in saved), default=0)
    for name, ws in saved.items():
        here = ws.root == current.root
        line = Text("  ● " if here else "  • ", style=ACCENT if here else "dim")
        line.append(name.ljust(pad), style="bold")
        line.append(f"  {ws.root}", style="dim")
        if ws.add_dirs:
            count = len(ws.add_dirs)
            line.append(f"  +{count} dir{'' if count == 1 else 's'}", style="dim")
        if not ws.root.is_dir():
            line.append("  missing", style="red")
        _emit(line)

    others = [p for p in recent if p != current.root]
    if others:
        _emit(Text(""))
        _emit(Text("recent", style=f"bold {ACCENT}"))
        for root in others:
            _emit(Text(f"  {root}", style="dim"))


def clear() -> None:
    """Wipe the transcript pane and reset the scroll position."""
    _blocks.clear()
    _output_window.vertical_scroll = 0
    _invalidate()


def note(msg: str) -> None:
    _emit(Text(msg, style="dim"))


def error(msg: str) -> None:
    _emit(Text(msg, style="bold red"))


# --- input ------------------------------------------------------------------

async def _ask(label: str, hints: tuple[tuple[str, str], ...]) -> str:
    global _prompt_label, _mode_hints, _pending, _overlay_visible
    _prompt_label = label
    _mode_hints = hints
    _overlay_visible = False
    if _app is not None:
        _app.layout.focus(_input_window)
    _invalidate()
    _pending = asyncio.get_running_loop().create_future()
    try:
        return await _pending
    finally:
        _pending = None


async def ask_task() -> str:
    return await _ask("❯ ", _TASK_HINTS)


async def ask_choice() -> str:
    return await _ask("approve? › ", _APPROVE_HINTS)


async def ask_line(label: str) -> str:
    return await _ask(label, _LINE_HINTS)
