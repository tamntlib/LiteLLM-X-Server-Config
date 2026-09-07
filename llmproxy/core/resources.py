"""Locate deployment/config resources in a checkout or installed wheel."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


_REQUIRED_DIRECTORIES = ("components", "presets")
_PACKAGE_DIR = Path(__file__).resolve().parents[1]
_CHECKOUT_ROOT = _PACKAGE_DIR.parent
_PACKAGED_ROOT = _PACKAGE_DIR / "resources"


class ResourceError(ValueError):
    """Raised when deployment/config resources cannot be located safely."""


def is_public_resource(path: Path) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and "__pycache__" not in path.parts
        and not any(".local" in part for part in path.parts[:-1])
        and (".local" not in path.name or path.name == "compose.local.example.yaml")
        and not any(part.startswith(".env") for part in path.parts)
        and path.name not in {"config.gen.json", "openapi.json"}
        and path.suffix not in {".pyc", ".pyo"}
    )


def _is_resource_root(path: Path) -> bool:
    return all(
        (path / name).is_dir() and not (path / name).is_symlink()
        for name in _REQUIRED_DIRECTORIES
    )


def resource_root() -> Path:
    explicit = os.environ.get("LLMPROXY_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()

    invocation_root = Path.cwd()
    if _is_resource_root(invocation_root):
        return invocation_root.resolve()
    if _is_resource_root(_CHECKOUT_ROOT):
        return _CHECKOUT_ROOT
    if _is_resource_root(_PACKAGED_ROOT):
        return _PACKAGED_ROOT
    raise ResourceError(
        "Cannot locate llmproxy resources; set LLMPROXY_ROOT to a repository checkout"
    )


def validate_resource_root(root: Path) -> None:
    if not root.is_dir() or root.is_symlink() or not _is_resource_root(root):
        raise ResourceError(f"LLMPROXY_ROOT is not a valid resource root: {root}")


def _reject_symlink_path(path: Path) -> None:
    """Reject existing symlinks early; descriptor traversal enforces this again."""
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ResourceError(f"Output path contains a symlink: {current}")


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_absolute_directory(path: Path) -> int:
    """Open an existing absolute directory without following path symlinks."""
    path = Path(os.path.normpath(path.expanduser().absolute()))
    descriptor = os.open(path.anchor, _directory_open_flags())
    try:
        for part in path.parts[1:]:
            child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_or_create_child_directory(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        return os.open(name, _directory_open_flags(), dir_fd=parent_fd)


def _write_private_text(
    path: Path,
    content: str,
    *,
    confinement_root: Path | None = None,
) -> Path:
    """Atomically write a private file using symlink-safe directory descriptors."""
    path = Path(os.path.normpath(path.expanduser().absolute()))
    if confinement_root is not None:
        root = Path(os.path.normpath(confinement_root.expanduser().absolute()))
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ResourceError(f"Output path escapes resource root: {path}") from exc
        _reject_symlink_path(root)
        boundary = root
    else:
        boundary = Path(path.anchor)
        relative = path.relative_to(boundary)

    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ResourceError(f"Invalid output path: {path}")
    _reject_symlink_path(path.parent)

    parent_fd = _open_absolute_directory(boundary)
    temp_name: str | None = None
    try:
        for part in relative.parts[:-1]:
            child_fd = _open_or_create_child_directory(parent_fd, part)
            os.close(parent_fd)
            parent_fd = child_fd

        target_name = relative.parts[-1]
        try:
            target_stat = os.stat(
                target_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None and stat.S_ISLNK(target_stat.st_mode):
            raise ResourceError(f"Output file is a symlink: {path}")
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            raise ResourceError(f"Output target is not a regular file: {path}")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        temp_name = f".llmproxy-{secrets.token_hex(12)}.tmp"
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
        try:
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "w")
            descriptor = -1
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        os.replace(
            temp_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_name = None
        os.fsync(parent_fd)
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
    return path


def write_private_text(
    path: Path,
    content: str,
    *,
    confinement_root: Path | None = None,
) -> Path:
    """Write a private regular file and convert filesystem failures to domain errors."""
    try:
        return _write_private_text(
            path,
            content,
            confinement_root=confinement_root,
        )
    except ResourceError:
        raise
    except OSError as exc:
        raise ResourceError(f"Cannot safely write output file: {path}: {exc}") from exc
