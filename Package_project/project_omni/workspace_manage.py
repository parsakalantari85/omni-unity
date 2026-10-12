"""Workspace manager: named and recent workspaces for Omni sessions."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import config, log

logger = log.get("workspace")

SAVED_KEY = "workspaces"
RECENT_KEY = "recent_workspaces"
TRUSTED_KEY = "trusted_workspaces"
RECENT_LIMIT = 10

# Names are typed after /workspace open, where anything else is read as a
# path, so a name is one word that can never be mistaken for one.
_NAME = re.compile(r"[A-Za-z0-9_][\w.-]*")


class WorkspaceError(ValueError):
    """A name or directory that cannot be used as a workspace."""


@dataclass(frozen=True)
class Workspace:
    """Where a session runs (``root``) and what else it may reach (``add_dirs``)."""

    root: Path
    add_dirs: tuple[Path, ...] = ()
    name: str | None = None

    @property
    def roots(self) -> list[Path]:
        """Every directory the approval gate treats as inside the workspace."""
        return [self.root, *self.add_dirs]

    def label(self) -> str:
        return f"{self.name} ({self.root})" if self.name else str(self.root)

    def granting(self, extra: Iterable[Path]) -> Workspace:
        """This workspace with ``extra`` directories added, repeats dropped."""
        dirs = dict.fromkeys(d for d in [*self.add_dirs, *extra] if d != self.root)
        return replace(self, add_dirs=tuple(dirs))


def is_too_broad(path: Path) -> bool:
    """True for a drive root or the home directory, which grant too much at once."""
    resolved = path.resolve()
    return resolved.parent == resolved or resolved == Path.home().resolve()


def _directory(raw: str, base: Path | None) -> Path:
    """``raw`` resolved against ``base`` (default: the process cwd)."""
    path = Path(base or Path.cwd(), Path(raw).expanduser())
    if not path.is_dir():
        raise WorkspaceError(f"'{raw}' is not a directory")
    return path.resolve()


# --- saved workspaces -------------------------------------------------------
def _stored() -> dict[str, Any]:
    """The raw ``workspaces`` object, or an empty one if it is unusable."""
    raw = config.get(SAVED_KEY, {})
    if not isinstance(raw, dict):
        logger.warning("config %s must be an object; ignoring it", SAVED_KEY)
        return {}
    return raw


def _parse(name: str, entry: Any) -> Workspace | None:
    """One saved entry as a Workspace; None when it is malformed."""
    if not isinstance(entry, dict) or not isinstance(entry.get("root"), str):
        logger.warning("saved workspace %r has no root; ignoring it", name)
        return None
    dirs = entry.get("add_dirs", [])
    if not isinstance(dirs, list):
        logger.warning("saved workspace %r: add_dirs must be a list", name)
        dirs = []
    return Workspace(
        root=Path(entry["root"]),
        add_dirs=tuple(Path(d) for d in dirs if isinstance(d, str)),
        name=name,
    )


def saved() -> dict[str, Workspace]:
    """Every saved workspace by name, as stored; directories are not checked."""
    out: dict[str, Workspace] = {}
    for name, entry in _stored().items():
        workspace = _parse(name, entry)
        if workspace is not None:
            out[name] = workspace
    return out


def load(name: str) -> Workspace:
    """The saved workspace ``name``, checked and ready to open.

    Raises WorkspaceError when there is no such workspace or its root is gone.
    Extra directories that have since disappeared, or that a hand edit made
    too broad, are dropped rather than fatal: the session only loses access,
    and the header lists exactly which directories it kept.
    """
    stored = saved()
    workspace = stored.get(name)
    if workspace is None:
        known = ", ".join(stored) or "none"
        raise WorkspaceError(f"no saved workspace {name!r} (saved: {known})")
    if not workspace.root.is_dir():
        raise WorkspaceError(
            f"workspace {name!r} points at {workspace.root}, which is not a directory"
        )
    kept: list[Path] = []
    for extra in workspace.add_dirs:
        if extra.is_dir() and not is_too_broad(extra):
            kept.append(extra.resolve())
        else:
            logger.warning("workspace %r: skipping extra directory %s", name, extra)
    return replace(workspace, root=workspace.root.resolve(), add_dirs=tuple(kept))


def resolve(target: str, base: Path | None = None) -> Workspace:
    """A saved workspace by name, else the directory ``target`` names.

    A saved name wins over a relative directory of the same name. ``base``
    anchors relative paths; /workspace passes the current root, so ``../x``
    means what it looks like from inside the session.
    """
    if target in _stored():
        return load(target)
    try:
        return Workspace(_directory(target, base))
    except WorkspaceError:
        if _NAME.fullmatch(target):
            raise WorkspaceError(
                f"{target!r} is neither a saved workspace nor a directory"
            ) from None
        raise


def save(name: str, workspace: Workspace) -> Workspace:
    """Store ``workspace`` under ``name``, replacing any entry already there.

    Returns the workspace carrying its new name. Other entries, even ones this
    module cannot parse, are written back untouched.
    """
    if not _NAME.fullmatch(name):
        raise WorkspaceError(
            f"{name!r} is not a usable name: use letters, digits, '_', '-' or "
            "'.', starting with a letter, digit or '_'"
        )
    stored = _stored()
    stored[name] = {
        "root": str(workspace.root),
        "add_dirs": [str(d) for d in workspace.add_dirs],
    }
    config.set(SAVED_KEY, stored)
    return replace(workspace, name=name)


def remove(name: str) -> bool:
    """Forget the saved workspace ``name``; False if there was none."""
    stored = _stored()
    if name not in stored:
        return False
    del stored[name]
    config.set(SAVED_KEY, stored)
    return True


# --- recent workspaces ------------------------------------------------------
def recent() -> list[Path]:
    """Roots of the latest sessions, newest first."""
    raw = config.get(RECENT_KEY, [])
    if not isinstance(raw, list):
        return []
    return [Path(p) for p in raw if isinstance(p, str)]


def remember(workspace: Workspace) -> None:
    """Move ``workspace``'s root to the front of the recent list.

    Runs at every session start, so a config file that cannot be written is
    logged rather than raised: failing to record history must not stop a
    session.
    """
    roots = [workspace.root, *(p for p in recent() if p != workspace.root)]
    try:
        config.set(RECENT_KEY, [str(p) for p in roots[:RECENT_LIMIT]])
    except OSError:
        logger.warning(
            "could not record recent workspace %s", workspace.root, exc_info=True
        )


# --- trust ------------------------------------------------------------------
def _trusted() -> list[Path]:
    """Trusted folders from config; a drive root or home never counts."""
    raw = config.get(TRUSTED_KEY, [])
    if not isinstance(raw, list):
        return []
    return [Path(p) for p in raw if isinstance(p, str) and not is_too_broad(Path(p))]


def is_trusted(root: Path) -> bool:
    """True when ``root`` or a folder above it has been trusted.

    Trust covers a folder's subfolders, as it does in Claude Code, so trusting
    a repository also trusts the packages inside it.
    """
    resolved = root.resolve()
    return any(resolved.is_relative_to(t) for t in _trusted())


def trust(root: Path) -> bool:
    """Remember ``root`` as trusted; False when it is too broad to remember.

    Trusting a drive root or the home folder would trust everything beneath
    it, so those are trusted for one session only and asked about again.
    """
    resolved = root.resolve()
    if is_too_broad(resolved):
        return False
    raw = config.get(TRUSTED_KEY, [])
    stored = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
    if str(resolved) not in stored:
        config.set(TRUSTED_KEY, [*stored, str(resolved)])
    return True
