"""Generic component-first deployment engine."""

from llmproxy.core.env import load_env_file, load_env_files
from llmproxy.deployment.component import Component, DockerConfigSpec
from llmproxy.deployment.compose import (
    ROOT,
    DeploymentError,
    compose_files_for_preset,
    docker_stack_config,
    local_override_paths_for_preset,
    render_preset,
    resolve_components,
    selected_stack_names,
    validate_preset,
)
from llmproxy.deployment.deploy import (
    _assert_no_unapproved_service_removals,
    _docker_command,
    _expected_stack_services,
    deploy_preset,
    content_addressed_config_name,
    preflight_preset,
)
from llmproxy.deployment.discovery import ComponentError, discover_components, load_component
from llmproxy.deployment.preset import Preset, PresetError, list_presets, load_preset, resolve_preset

__all__ = [
    "ROOT",
    "Component",
    "DockerConfigSpec",
    "ComponentError",
    "DeploymentError",
    "Preset",
    "PresetError",
    "_assert_no_unapproved_service_removals",
    "_docker_command",
    "_expected_stack_services",
    "compose_files_for_preset",
    "content_addressed_config_name",
    "deploy_preset",
    "discover_components",
    "docker_stack_config",
    "local_override_paths_for_preset",
    "list_presets",
    "load_component",
    "load_env_file",
    "load_env_files",
    "load_preset",
    "preflight_preset",
    "render_preset",
    "resolve_components",
    "resolve_preset",
    "selected_stack_names",
    "validate_preset",
]
