# Repository code review — suggested improvements

Scope: all sources under `Python/Package_project/` (`agent.py`, `ui.py`, `config.py`,
`__main__.py`, tests), packaging (`pyproject.toml`, `dev-requirements.txt`), CI, and docs.

Overall the codebase is in good shape: small, focused modules with a clear
separation between control flow (`agent.py`) and presentation (`ui.py`), an
unusually well-tested approval gate, and comments that explain *why* rather
than *what*. The suggestions below are ordered by impact.

---

## 1. Correctness / reliability

### 1.1 Pin a minimum version of `claude-agent-sdk`
`pyproject.toml` declares `claude-agent-sdk` with no version constraint, but the
code depends on newer SDK surface: `enable_file_checkpointing`
(`agent.py:192`), `reconnect_mcp_server` (`agent.py:220`), `get_mcp_status`,
`set_permission_mode`, and `PermissionResultDeny(interrupt=True)`. A user who
resolves an older SDK gets a `TypeError` at startup instead of a clear message.

**Suggestion:** pin the minimum version you actually developed against, e.g.
`claude-agent-sdk>=X.Y`.

### 1.2 One transient error kills the whole session
The REPL body (`agent.py:438-439`) calls `client.query(prompt)` and
`drain(client)` unguarded. Any exception from the SDK mid-task (relay process
dies, transport hiccup, malformed message) propagates out of `main()`, tears
down the UI, and exits via the fatal handler in `__main__.py`. The same applies
to the startup `get_mcp_status()` call (`agent.py:411`): a relay binary that
exists but fails to start crashes the app instead of showing the
"disconnected" header with hints.

**Suggestion:** wrap the query/drain step in `try/except`, report via
`ui.error(...)`, and continue the loop; wrap the startup status probe and fall
back to `STATE_DISCONNECTED`.

### 1.3 Two conflicting definitions of "a Unity project"
- `find_project()` walk-up requires a `ProjectSettings/` folder (`agent.py:107`).
- `is_unity_project()` accepts `.csproj` / `Assembly-CSharp*` / `Assets` (`agent.py:113-115`).
- Paths from saved config or `UNITY_PROJECT_PATH` are accepted if they merely
  `is_dir()` (`agent.py:100`) — no Unity check at all, while `--set-project`
  *does* validate.

**Suggestion:** define Unity-project detection once (e.g. `ProjectSettings/`
**or** `Assets/` directory present) and apply it consistently across
`find_project`, `--set-project`, and the cwd prompt.

Related nits:
- `any(path.glob("Assets"))` (`agent.py:115`) is true for a plain *file* named
  `Assets`; `(path / "Assets").is_dir()` is both clearer and stricter.
- The error text at `agent.py:381` says "(no .csproj or Assembly-CSharp found
  here)" but omits the `Assets` criterion the check actually uses.

### 1.4 Defensive access to SDK status payloads
`relay_state()` (`agent.py:145-149`) and `cmd_tools()` (`agent.py:269-271`)
index `s["name"]` / `s["status"]` directly. If the SDK ever renames or omits a
key, a `KeyError` propagates and (per 1.2) ends the session.

**Suggestion:** use `.get()` with a safe default; the existing
`STATE_DISCONNECTED` fallback path already handles unknown states well.

### 1.5 Validate `_PERMISSION_MODES` against the SDK, not a copy
`agent.py:201-208` hardcodes the accepted `/mode` values including `dontAsk`
and `auto`. Whether these are valid depends on the installed SDK version, and
the list will drift silently.

**Suggestion:** derive it from the SDK type at import time:
`typing.get_args(PermissionMode)`. Then `/mode` can never accept a value the
SDK rejects (or reject one it accepts).

### 1.6 Dead state: `STATE_INTERRUPTED`
`ui.py:302` defines `STATE_INTERRUPTED` with a style, label, and hint text, but
nothing in `agent.py` ever produces it.

**Suggestion:** either emit it (e.g. when ctrl-c interrupts a running task) or
delete it — right now it's untested, unreachable UI.

---

## 2. Robustness / UX

### 2.1 Contradictory message when cwd isn't a Unity project
`prompt_unity_access()` prints "omni only runs inside a Unity project"
(`agent.py:379`) and then… continues running with `project=None`. Either the
message should say the session starts without a project (matching the header's
"(none — set UNITY_PROJECT_PATH…)" hint), or the app should actually exit.

### 2.2 Hardcoded `max_turns=50`
`agent.py:194` sets `max_turns=50` with a comment "Optional hard ceilings while
developing" — but it ships enabled and will silently truncate long tasks.

**Suggestion:** make it configurable (env var or `~/.omni/config.json` key) or
remove it for release builds.

### 2.3 Atomic config writes
`config.set()` (`config.py:28`) writes with `write_text`; a crash mid-write
leaves corrupt JSON. `_load()` degrades gracefully so the failure mode is only
"config silently reset", but atomicity is one line:

```python
tmp = _CONFIG_FILE.with_suffix(".tmp")
tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
tmp.replace(_CONFIG_FILE)
```

(Also consider renaming `config.set` — shadowing the builtin makes call sites
like `config.set(...)` fine but the module internals slightly awkward.)

### 2.4 Guard previews against non-string inputs
`_preview_edit` / `_preview_write` (`ui.py:397-430`) call `.splitlines()` on
`old_string` / `new_string` / `content` taken straight from model-provided tool
input. A non-string value (malformed tool call) raises inside the UI layer and
takes the app down. Coerce with `str(...)` or type-check before slicing.

---

## 3. Packaging & CI

### 3.1 Fill in project metadata
`pyproject.toml` is missing `license` (there's an MIT `LICENSE` at repo root),
`readme`, `classifiers`, and `[project.urls]`. Note the README lives two
directories above the package, which leads to…

### 3.2 Flatten the repo layout
`Python/Package_project/` is an unusual nesting for a repo that contains
exactly one Python package. It complicates installation instructions
(`cd Python/Package_project` before `pip install -e .`), prevents referencing
the top-level README in package metadata, and adds a `working-directory` knob
to CI.

**Suggestion:** move `pyproject.toml`, `project_omni/`, and `tests/` to the
repo root (optionally as a `src/` layout). This is the single change that most
simplifies everything else.

### 3.3 CI hardening
`ci.yml` is minimal and correct. Worth adding:
- a lint/format job (ruff is a one-liner and `.ruff_cache/` is already in `.gitignore`),
- Python 3.13 in the matrix,
- `cache: pip` on `setup-python`,
- a `concurrency` block to cancel superseded runs on the same ref.

### 3.4 Clean up `dev-requirements.txt`
It still contains a TODO placeholder, and `setuptools`/`wheel` don't belong in
dev requirements (they're build-time, already declared in
`[build-system].requires`). `pytest` (plus `ruff` per 3.3) is all it needs.

---

## 4. Tests

Current coverage of the pure logic is genuinely good (the approval-gate state
machine is fully exercised). Gaps worth closing:

- `find_relay()` darwin/linux branches — only the win32 path is tested
  (`test_Package.py:142-154`).
- `build_options()` — assert the relay wiring, pinned `permission_mode`, and
  that no `mcp_servers` entry exists when relay is `None`.
- Slash-command parsing in `main()`'s loop (name/arg splitting, unknown
  command) — currently only reachable through the full REPL. Extracting a
  small `dispatch(prompt) -> handler, arg` function would make it unit-testable.
- `relay_state()` with a missing `status` key (per 1.4).
- File naming: `tests/test_Package.py` → `tests/test_agent.py` (or split per
  module) for conventional discovery and readability.

---

## 5. Documentation nits

- README's `/model` example uses `claude-opus-4-8` while `cmd_model`'s
  usage string suggests `claude-sonnet-4-6` — pick one.
- "How it works" step 3 lists the auto-approved tools as `Read`, `Glob`,
  `Grep`, but the Approval-gate section (and `AUTO_APPROVE` in code) also
  includes `TodoWrite`.
- The install snippet contains a stray comment ("pytest installation guide at
  the very end of this README") that reads oddly inside a code block.

---

## Suggested priority

1. **1.2** (error handling in the REPL loop) — biggest reliability win.
2. **1.1** (SDK version pin) — prevents confusing install-time breakage.
3. **1.3** (unify Unity-project detection) — user-facing consistency.
4. **3.2** (flatten layout) — unlocks the packaging/CI cleanups.
5. Everything else as convenient.
