from __future__ import annotations

from pathlib import Path


def unsafe_destination_reason(workspace: Path) -> str | None:
    absolute = workspace.absolute()
    if absolute == Path(absolute.anchor) or absolute == Path.home():
        return "destination is a protected directory"

    system_aliases = {Path("/etc"), Path("/tmp"), Path("/var")}
    for ancestor in reversed(absolute.parents):
        if ancestor.is_symlink() and ancestor not in system_aliases:
            return "destination has a symbolic-link ancestor"

    current = absolute
    while not current.exists() and current != current.parent:
        current = current.parent
    if current.is_symlink():
        return "destination has a symbolic-link ancestor"
    if not current.is_dir():
        return "destination parent is not a directory"
    if workspace.is_symlink():
        return "destination is a symbolic link"
    if workspace.exists() and not workspace.is_dir():
        return "destination is not a directory"
    if workspace.exists() and any(workspace.iterdir()):
        return "destination is not empty"
    return None
