"""Environment loading, parsing, and process isolation utilities."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


class EnvError(RuntimeError):
    """Raised for invalid environment configuration or parsing errors."""


COMMON_OPERATIONAL_ENV_NAMES = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "SSH_AUTH_SOCK",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}

DOCKER_OPERATIONAL_ENV_NAMES = {
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_API_VERSION",
    "DOCKER_AUTH_CONFIG",
    "DOCKER_CONTENT_TRUST",
    "DOCKER_CONTENT_TRUST_SERVER",
    "DOCKER_DEFAULT_PLATFORM",
}

PTCTOOLS_OPERATIONAL_ENV_NAMES = {
    "PORTAINER_URL",
    "PORTAINER_ACCESS_TOKEN",
    "UV_CACHE_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON",
    "UV_PYTHON_PREFERENCE",
    "UV_DEFAULT_INDEX",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_INDEX",
    "UV_FIND_LINKS",
    "UV_NO_INDEX",
    "UV_NATIVE_TLS",
    "UV_INSECURE_HOST",
    "UV_KEYRING_PROVIDER",
    "UV_HTTP_TIMEOUT",
    "UV_HTTP_RETRIES",
    "UV_CONCURRENT_DOWNLOADS",
    "UV_NO_CACHE",
    "UV_NO_PROGRESS",
    "UV_OFFLINE",
}


def read_env_values(path: Path) -> dict[str, str]:
    file_values: dict[str, str] = {}
    if path.is_file():
        for line_number, raw_line in enumerate(path.read_text().splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise EnvError(f"Invalid env line {line_number} in {path}")
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value and value[0] in {'"', "'"}:
                try:
                    parsed = shlex.split(value, posix=True)
                except ValueError as exc:
                    raise EnvError(
                        f"Invalid quoted value on line {line_number} in {path}"
                    ) from exc
                value = parsed[0] if parsed else ""
            file_values[key] = value
    return file_values


def load_env_files(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        values.update(read_env_values(path))
    return {**values, **os.environ}


def load_env_file(path: Path) -> dict[str, str]:
    return load_env_files([path])


def load_dotenv(file_path: Path | str = ".env") -> None:
    path = Path(file_path)
    if not path.is_file():
        return
    for k, v in read_env_values(path).items():
        os.environ.setdefault(k, v)


def operational_environment(
    source: dict[str, str], driver: str = "docker"
) -> dict[str, str]:
    names = set(COMMON_OPERATIONAL_ENV_NAMES)
    if driver == "docker":
        names.update(DOCKER_OPERATIONAL_ENV_NAMES)
    elif driver == "ptctools":
        names.update(PTCTOOLS_OPERATIONAL_ENV_NAMES)
    else:
        raise EnvError(f"Unknown deploy driver: {driver}")
    environment = {name: source[name] for name in names if name in source}
    environment.setdefault("PATH", os.environ.get("PATH", os.defpath))
    return environment


def referenced_env_names(stack_content: str) -> set[str]:
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", stack_content))


def command_environment(
    outputs: dict[str, Path], source: dict[str, str], driver: str
) -> dict[str, str]:
    environment = operational_environment(source, driver)
    referenced = set()
    for output in outputs.values():
        referenced.update(referenced_env_names(output.read_text()))
    for name in referenced:
        if name in source:
            environment[name] = source[name]
    return environment


def write_ptctools_env_file(
    path: Path, stack_content: str, env: dict[str, str]
) -> None:
    lines = []
    for name in sorted(referenced_env_names(stack_content)):
        if name not in env:
            continue
        value = str(env[name])
        if "\n" in value or "\r" in value:
            raise EnvError(
                f"Environment variable {name} contains a newline and cannot be sent to ptctools"
            )
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    path.chmod(0o600)
