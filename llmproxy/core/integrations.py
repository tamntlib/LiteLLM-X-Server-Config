"""Resolve source-owned files for an explicitly named integration destination."""

from pathlib import Path
import re
import stat


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def resolve_integration_file(
    component_directory: Path, target: str, filename: str
) -> Path | None:
    """Return ``source/integrations/<stack>/<component>/<filename>`` or None.

    ``target`` uses two component identifiers (ASCII alphanumerics followed by
    alphanumerics, underscores or hyphens); ``filename`` is a nonempty leaf with
    no slash, backslash, NUL, or ``..``. Contents and suffixes are opaque here.
    Missing paths return None. Invalid arguments, symlinks anywhere along the
    absolute path, and existing entries of the wrong type raise ValueError.
    Other filesystem errors propagate. No other destination is inspected.
    """
    parts = target.split("/") if isinstance(target, str) else []
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError(f"Invalid integration target: {target!r}")
    if (
        not isinstance(filename, str)
        or not filename
        or filename == "."
        or any(part in filename for part in ("/", "\\", "..", "\x00"))
    ):
        raise ValueError(f"Invalid integration filename: {filename!r}")
    path = component_directory.absolute() / "integrations" / target / filename
    for entry in (*reversed(path.parents), path):
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode):
            raise ValueError(f"Integration path contains a symlink: {entry}")
        if entry == path:
            if not stat.S_ISREG(mode):
                raise ValueError(f"Integration file must be a regular file: {entry}")
        elif not stat.S_ISDIR(mode):
            raise ValueError(f"Integration path must be a directory: {entry}")
    return path
