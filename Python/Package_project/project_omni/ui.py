"""Terminal UI for the Unity automation agent.

A single full-screen ``prompt_toolkit`` application: model output and tool
activity scroll inside the bordered pane up top, while the input box stays
pinned at the bottom. Because the app owns the screen, typed commands and
approval keypresses leave no scrollback residue.

Presentation only. ``agent.py`` owns all control flow and permission
decisions; it calls into here for every line of input and output. ``rich``
renders each message to ANSI, which we splice into the output pane.
"""
from __future__ import annotations

import difflib
import io
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import ANSI, HTML, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.data_structures import Point
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame
from rich.console import Console
from rich.json import JSON
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

# Product name shown in the UI (frame title, command help). One source so the
# agent and UI can't display different names.
APP_NAME = "Omni"

# Claude-ish terracotta accent.
ACCENT = "#d97757"
PREVIEW_LIMIT = 2000

# Bottom key-hint bars, swapped per input mode.
_TASK_BAR = HTML(
    " <b>enter</b> send  ·  <b>/help</b> commands  ·  <b>/reconnect</b> after editor restart"
    "  ·  <b>/exit</b> quit  ·  <b>PgUp/PgDn</b> scroll "
)
_APPROVE_BAR = HTML(
    " <b>a</b> approve  ·  <b>d</b> deny  ·  <b>e</b> edit  ·  <b>f</b> full  ·  <b>q</b> deny+stop "
)
_LINE_BAR = HTML(" <b>enter</b> submit  ·  <b>ctrl-d</b> cancel ")

_style = Style.from_dict(
    {
        "prompt": "bold #d97757",
        "toolbar": "#888888",
        "frame.border": ACCENT,
        "frame.label": f"bold {ACCENT}",
    }
)


# --- state ------------------------------------------------------------------

# Accumulated output: each block is a list of (style, text) fragments.
_blocks: list[Any] = []
# Current input prompt label and bottom bar (mutated per ask_*).
_prompt_label = "› "
_toolbar: Any = _TASK_BAR
# Set while an ask_* call is waiting for the user to submit.
_pending: Any = None  # asyncio.Future[str] | None
# agent.py registers client.interrupt() here so ctrl-c can reach a running task.
_interrupt_handler: Callable[[], Awaitable[None]] | None = None

_app: Application[Any] | None = None

# Scrolling is driven by the output window's ``vertical_scroll``: the wheel and
# Pg keys move it, and ``_emit`` snaps to the bottom on new output only when the
# view was already there. See ``_cursor_point`` for why the cursor sits on top.


def _invalidate() -> None:
    if _app is not None:
        try:
            _app.invalidate()
        except Exception:
            pass


def _term_width() -> int:
    """Inner width available to rich, accounting for the frame border."""
    if _app is not None:
        try:
            return max(40, _app.output.get_size().columns - 4)
        except Exception:
            pass
    return 96


def _emit(renderable: Any) -> None:
    """Render a rich renderable to ANSI and append it to the output pane."""
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor", width=_term_width()
    ).print(renderable)
    # Decide *before* appending whether the user was parked at the bottom; only
    # then do we ride the new output down (otherwise we'd yank them away from
    # whatever they scrolled up to read).
    stick = _at_bottom()
    _blocks.append(to_formatted_text(ANSI(buf.getvalue())))
    if stick:
        # Overshoot; the render clamps it to the real bottom.
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
    """Park the invisible cursor on the current top line.

    prompt_toolkit scrolls to keep the cursor visible; pinning it to the top of
    the viewport means our ``vertical_scroll`` is what wins, so the wheel and Pg
    keys can move freely instead of snapping back to a fixed cursor line.
    """
    return Point(x=0, y=min(_output_window.vertical_scroll, _content_lines()))


_output_window = Window(
    FormattedTextControl(
        text=_combined,
        focusable=False,
        show_cursor=False,
        get_cursor_position=_cursor_point,
    ),
    wrap_lines=True,
    right_margins=[ScrollbarMargin(display_arrows=True)],
)


def _at_bottom() -> bool:
    """True when the last content line is currently visible (or nothing rendered
    yet, so the first output sticks)."""
    info = _output_window.render_info
    if info is None:
        return True
    return info.last_visible_line() >= info.ui_content.line_count - 1


def _accept(buff: Buffer) -> bool:
    if _pending is not None and not _pending.done():
        _pending.set_result(buff.text)
    return False  # clear the input box; no residue


_input_buffer = Buffer(accept_handler=_accept, multiline=False)
_input_window = Window(
    BufferControl(
        buffer=_input_buffer,
        input_processors=[BeforeInput(lambda: [("class:prompt", _prompt_label)])],
    ),
    height=1,
)

_root = HSplit(
    [
        Frame(_output_window, title=APP_NAME),
        _input_window,
        Window(FormattedTextControl(lambda: _toolbar), height=1, style="class:toolbar"),
    ]
)


# --- key bindings -----------------------------------------------------------

_kb = KeyBindings()


@_kb.add("c-c")
def _(event: Any) -> None:
    """Interrupt a running task; harmless when idle at the prompt."""
    if _interrupt_handler is not None:
        event.app.create_background_task(_run_interrupt())


@_kb.add("c-d")
def _(event: Any) -> None:
    """EOF: end whatever ask_* is waiting (the loop reads this as quit/deny)."""
    if _pending is not None and not _pending.done():
        _pending.set_exception(EOFError())


def _visible_span(info: Any) -> int:
    """Content lines on screen, minus one for continuity overlap. Counting
    *visible* lines (not window rows) makes a page step account for wrapping, so
    PgUp/PgDn never skip wrapped content."""
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
    # Overshoot is clamped on render; landing at the bottom re-arms auto-scroll
    # via _at_bottom().
    _output_window.vertical_scroll = info.vertical_scroll + _visible_span(info)
    _invalidate()


async def _run_interrupt() -> None:
    note("[interrupting current task…]")
    try:
        if _interrupt_handler is not None:
            await _interrupt_handler()
    except Exception as exc:
        error(f"(interrupt failed: {exc})")


# --- lifecycle --------------------------------------------------------------

def set_interrupt_handler(handler: Callable[[], Awaitable[None]]) -> None:
    global _interrupt_handler
    _interrupt_handler = handler


def start() -> Application[Any]:
    global _app
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
    """``mcp__unity__set_graphics`` -> ``set_graphics  unity`` (styled)."""
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


# Canonical relay-connection states. agent.py produces these; the maps below
# turn each into its dot colour, displayed label, and connect hints. Use the
# constants everywhere instead of bare strings so the producer and consumer
# can't drift apart.
STATE_CONNECTED = "connected"
STATE_INTERRUPTED = "interrupted"
STATE_DISCONNECTED = "disconnected"
STATE_NO_RELAY = "no_relay_found"

# state -> dot colour.
_STATE_STYLES = {
    STATE_CONNECTED: "green",
    STATE_INTERRUPTED: "yellow",
    STATE_DISCONNECTED: "red",
    STATE_NO_RELAY: "red",
}

# state -> human label shown after "unity relay" (the key itself reads poorly).
_STATE_LABELS = {
    STATE_CONNECTED: "connected",
    STATE_INTERRUPTED: "interrupted",
    STATE_DISCONNECTED: "disconnected",
    STATE_NO_RELAY: "no relay found",
}

# How-to-connect instructions, shown whenever the relay isn't connected.
_STATE_HINTS = {
    STATE_DISCONNECTED: (
        "→ Is the Unity Editor open with the MCP bridge running?",
        "→ Approve this client in Project Settings ▸ AI ▸ Unity MCP.",
    ),
    STATE_INTERRUPTED: (
        "→ The relay connection dropped — check the Editor, then type /reconnect.",
    ),
    STATE_NO_RELAY: (
        "→ No relay binary found. Set UNITY_RELAY_PATH to the Unity MCP relay",
        "  and restart. Tasks can't reach Unity until then.",
    ),
}


def _relay_line(state: str) -> Text:
    style = _STATE_STYLES.get(state, "yellow")
    line = Text()
    line.append("● ", style=style)
    line.append("unity relay ", style="bold")
    line.append(_STATE_LABELS.get(state, state), style=style)
    return line


def _relay_hints(state: str) -> None:
    for hint in _STATE_HINTS.get(state, ()):
        _emit(Text(f"  {hint}", style="yellow"))


# --- output -----------------------------------------------------------------

def header(state: str, project: Any = None) -> None:
    _emit(_relay_line(state))
    _relay_hints(state)
    if project is not None:
        _emit(Text(f"  project  {project}", style="dim"))
    else:
        _emit(Text("  project  (none — set UNITY_PROJECT_PATH or pass --project)", style="dim yellow"))
    _emit(
        Text.from_markup(
            "Type a task, [bold]/help[/bold] for commands, "
            "or [bold]/exit[/bold] to quit.",
            style="dim",
        )
    )


def relay_update(state: str) -> None:
    """Re-print the relay state line (after a reconnect attempt)."""
    _emit(_relay_line(state))
    _relay_hints(state)


def user_prompt(text: str) -> None:
    """Echo submitted input into the scroll pane.

    The input box clears on submit (``_accept`` returns False), so without this
    the transcript would show only Claude's side.
    """
    _emit(Text(""))
    line = Text("› ", style=f"bold {ACCENT}")
    line.append(text)
    _emit(line)


def assistant(text: str) -> None:
    _emit(Text(""))
    _emit(Text("● Claude", style=f"bold {ACCENT}"))
    _emit(Markdown(text))


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
    # Show at most 4 key=value pairs; skip large blobs.
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


def turn_done(num_turns: int, cost: float | None, error_msg: str | None) -> None:
    price = f"${cost:.4f}" if cost is not None else "n/a"
    line = Text("⎿ done", style="dim")
    line.append(f" · {num_turns} turns · {price}", style="dim")
    if error_msg:
        line.append(f" · ERROR: {error_msg}", style="bold red")
    _emit(Text(""))
    _emit(line)


def command_help(items: list[tuple[str, str]]) -> None:
    """Render the slash-command list (label + description per row)."""
    _emit(Text(""))
    _emit(Text("commands", style=f"bold {ACCENT}"))
    for name, desc in items:
        line = Text(f"  {name}", style=ACCENT)
        line.append(f"   {desc}", style="dim")
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

async def _ask(label: str, toolbar: Any) -> str:
    import asyncio

    global _prompt_label, _toolbar, _pending
    _prompt_label = label
    _toolbar = toolbar
    if _app is not None:
        _app.layout.focus(_input_window)
    _invalidate()
    _pending = asyncio.get_running_loop().create_future()
    try:
        return await _pending
    finally:
        _pending = None


async def ask_task() -> str:
    return await _ask("› ", _TASK_BAR)


async def ask_choice() -> str:
    return await _ask("approve? › ", _APPROVE_BAR)


async def ask_line(label: str) -> str:
    return await _ask(label, _LINE_BAR)
