"""Subprocess execution and command display masking utilities."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


class ProcessError(RuntimeError):
    """Raised when an external subprocess execution fails."""


def command_display_text(command: list[str]) -> str:
    display = [
        f"<generated-env:{item.split(':', 1)[1]}>"
        if item.startswith("__PTCTOOLS_ENV__:")
        else item
        for item in command
    ]
    return shlex.join(display)


def run_process(
    command: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    text: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=text,
        capture_output=capture_output,
        check=False,
    )
    if check and result.returncode:
        msg = (
            (result.stderr or "").strip()
            or (result.stdout or "").strip()
            or f"Command failed with code {result.returncode}: {command_display_text(command)}"
        )
        raise ProcessError(msg)
    return result
