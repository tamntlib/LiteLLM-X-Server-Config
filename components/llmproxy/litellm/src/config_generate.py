"""Resolve shared and component-owned LiteLLM configuration layers."""

import json
import logging
import urllib.error
import urllib.parse
import re
from pathlib import Path
from llmproxy.core.http import format_http_error, request_json
from llmproxy.core.integrations import resolve_integration_file
from llmproxy.deployment.discovery import discover_components
from llmproxy.deployment.preset import resolve_preset

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class ModelDiscoveryError(ValueError):
    """Raised when a provider model inventory cannot be trusted."""


class DuplicateModelInventoryError(ModelDiscoveryError):
    """Raised when a discovery response repeats a model identifier."""

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPONENT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = COMPONENT_DIR / "configs"
DEFAULT_CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_OUTPUT_FILE = Path("build/llmproxy/litellm/config.gen.json")

PROVIDER_CONFIG = {
    "openai": {
        "path_suffix": "/v1",
        "custom_llm_provider": "OpenAI_Compatible",
    },
    "gemini": {
        "path_suffix": "/v1beta",
        "custom_llm_provider": "Google_AI_Studio",
    },
    "anthropic": {
        "path_suffix": "",
        "custom_llm_provider": "Anthropic",
    },
}


# ============================================================================
# Utility Functions
# ============================================================================


def load_json(file_path):
    with open(file_path) as f:
        return json.load(f)


_DELETE_KEYS = "$delete"


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge dictionaries, with $delete listing inherited keys to remove."""
    result = base.copy()
    for key, value in override.items():
        if key == _DELETE_KEYS:
            continue
        if isinstance(value, dict):
            nested_base = result.get(key, {})
            result[key] = deep_merge(
                nested_base if isinstance(nested_base, dict) else {},
                value,
            )
        else:
            result[key] = value

    delete_keys = override.get(_DELETE_KEYS, [])
    if isinstance(delete_keys, list):
        for key in delete_keys:
            if isinstance(key, str):
                result.pop(key, None)

    return result


def load_config_with_local(config_path: Path) -> tuple[dict, dict]:
    """Load config.json and merge with config.local.json if it exists.

    The local config file is expected to be in the same directory as the main config.
    Values in local config will override/extend values in the base config.

    Returns a tuple of (merged_config, base_config) so that $base refs can be resolved.
    """
    config = load_json(config_path)
    base_config = config

    # Determine the local config path (same directory, with .local suffix)
    local_config_path = config_path.parent / config_path.name.replace(
        ".json", ".local.json"
    )

    if local_config_path.exists():
        logger.info(f"Found local config: {local_config_path}")
        local_config = load_json(local_config_path)
        config = deep_merge(config, local_config)
        logger.info("Merged local config with base config")

    return config, base_config


def load_config_for_preset(
    preset_name: str,
    *,
    include_local: bool = True,
    config_path: Path | None = None,
    root: Path | None = None,
) -> tuple[dict, dict]:
    """Load the base config and component-owned layers selected by a preset."""
    deployment_root = root or REPO_ROOT
    preset = resolve_preset(preset_name, deployment_root)
    components = discover_components(deployment_root)
    config_path = config_path or (
        deployment_root / "components" / "llmproxy" / "litellm" / "configs" / "config.json"
    )
    if not config_path.is_file() or config_path.is_symlink():
        raise ModelDiscoveryError(f"LiteLLM config file not found: {config_path}")
    selected_public_config = load_json(config_path)
    for identifier in preset.components:
        component = components.get(identifier)
        if component is None:
            raise ValueError(f"Unknown deployment component: {identifier}")
        try:
            config_layer = resolve_integration_file(
                component.directory, "llmproxy/litellm", "config.json"
            )
        except (ValueError, OSError) as exc:
            raise ModelDiscoveryError(str(exc)) from exc
        if config_layer is None:
            continue
        selected_public_config = deep_merge(
            selected_public_config,
            load_json(config_layer),
        )

    config = selected_public_config

    if include_local:
        for identifier in preset.components:
            component = components[identifier]
            try:
                local_config_layer = resolve_integration_file(
                    component.directory, "llmproxy/litellm", "config.local.json"
                )
            except (ValueError, OSError) as exc:
                raise ModelDiscoveryError(str(exc)) from exc
            if local_config_layer is None:
                continue
            logger.info(f"Found component local config: {local_config_layer}")
            config = deep_merge(config, load_json(local_config_layer))
            logger.info(f"Merged local config for component: {identifier}")

    return config, selected_public_config


def generate_config_for_preset(
    preset_name: str,
    *,
    include_local: bool = True,
    config_path: Path | None = None,
    require_complete: bool = False,
    root: Path | None = None,
) -> dict:
    config, base_config = load_config_for_preset(
        preset_name,
        include_local=include_local,
        config_path=config_path,
        root=root,
    )
    return generate_config(
        config,
        base_config=base_config,
        require_complete=require_complete,
    )


def resolve_fallback_base_refs(
    fallbacks: list,
    base_fallbacks: list,
    *,
    require_complete: bool = False,
) -> list:
    """Resolve $base references in fallback lists."""

    def expand_keys(rules: list, label: str) -> list:
        if not isinstance(rules, list):
            if require_complete:
                raise ModelDiscoveryError(f"{label} must be a list")
            return []
        expanded = []
        for entry in rules:
            if not isinstance(entry, dict):
                if require_complete:
                    raise ModelDiscoveryError(f"{label} contains a non-object rule")
                continue
            for key, values in entry.items():
                if not isinstance(key, str) or not isinstance(values, list):
                    if require_complete:
                        raise ModelDiscoveryError(
                            f"{label} rule keys must be strings and targets must be lists"
                        )
                    continue
                for subkey in key.split(","):
                    subkey = subkey.strip()
                    if subkey:
                        expanded.append({subkey: values})
        return expanded

    expanded_base = expand_keys(base_fallbacks, "base fallbacks")
    expanded_fallbacks = expand_keys(fallbacks, "fallbacks")

    base_lookup = {}
    for entry in expanded_base:
        for key, values in entry.items():
            base_lookup[key] = values

    resolved = []
    for entry in expanded_fallbacks:
        resolved_entry = {}
        for key, values in entry.items():
            if "$base" in values:
                if key not in base_lookup and require_complete:
                    raise ModelDiscoveryError(
                        f"Fallback '{key}' uses $base but has no matching base rule"
                    )
                base_values = base_lookup.get(key, [])
                resolved_entry[key] = [
                    item
                    for value in values
                    for item in (base_values if value == "$base" else [value])
                ]
            else:
                resolved_entry[key] = values
        resolved.append(resolved_entry)
    return resolved


def resolve_provider_extensions(providers: dict) -> dict:
    """Resolve provider inheritance independent of declaration order."""
    if not isinstance(providers, dict):
        raise ModelDiscoveryError("providers must be an object")

    resolved: dict[str, dict] = {}
    visiting: list[str] = []

    def resolve(name: str) -> dict:
        if name in resolved:
            return resolved[name]
        if name in visiting:
            cycle = " -> ".join((*visiting, name))
            raise ModelDiscoveryError(f"Cyclic provider inheritance: {cycle}")
        if name not in providers:
            raise ModelDiscoveryError(f"Provider extends non-existent provider '{name}'")
        config = providers[name]
        if not isinstance(config, dict):
            raise ModelDiscoveryError(f"Provider '{name}' must be an object")

        visiting.append(name)
        base_name = config.get("$extend")
        if base_name is None:
            merged = dict(config)
        else:
            if not isinstance(base_name, str) or not base_name:
                raise ModelDiscoveryError(
                    f"Provider '{name}' has an invalid $extend target"
                )
            if base_name not in providers:
                raise ModelDiscoveryError(
                    f"Provider '{name}' extends non-existent provider '{base_name}'"
                )
            merged = deep_merge(resolve(base_name), config)
        visiting.pop()
        resolved[name] = {key: value for key, value in merged.items() if key != "$extend"}
        return resolved[name]

    for provider_name in providers:
        resolve(provider_name)
    return resolved


def _join_api_base(api_base: str, path_suffix: str) -> str:
    """Append a path suffix once, tolerating already-suffixed base URLs."""
    normalized_base = api_base.rstrip("/")
    normalized_suffix = path_suffix.strip("/")

    if not normalized_suffix:
        return normalized_base or api_base

    if not normalized_base:
        return f"/{normalized_suffix}"

    if normalized_base.endswith(f"/{normalized_suffix}"):
        return normalized_base

    return f"{normalized_base}/{normalized_suffix}"


def _get_interface_api_base(provider_config: dict, iface: dict) -> str:
    """Resolve the API base for a specific interface."""
    if "api_base" in iface:
        return iface.get("api_base", "")
    return provider_config.get("api_base", "")


def _get_interface_models_api_base(provider_config: dict, iface: dict) -> str:
    """Resolve the /models API base for a specific interface."""
    if "models_api_base" in iface:
        return iface.get("models_api_base", "")
    if "api_base" in iface:
        return iface.get("api_base", "")
    if "models_api_base" in provider_config:
        return provider_config.get("models_api_base", "")
    return provider_config.get("api_base", "")


def build_credential_payload(
    service_name: str, provider: str, api_key: str, api_base: str
) -> dict:
    """Build a LiteLLM credential create request body."""
    provider_cfg = PROVIDER_CONFIG[provider]
    credential_name = f"{service_name}-{provider}"
    path_suffix = provider_cfg["path_suffix"]

    return {
        "credential_name": credential_name,
        "credential_values": {
            "api_key": api_key,
            "api_base": _join_api_base(api_base, path_suffix),
        },
        "credential_info": {
            "custom_llm_provider": provider_cfg["custom_llm_provider"]
        },
    }


def _fetch_openai_models(api_base: str, api_key: str) -> list[str]:
    """Fetch models using OpenAI-compatible /v1/models endpoint (Bearer auth)."""
    url = f"{_join_api_base(api_base, '/v1')}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        data = request_json(url, headers=headers, timeout=30)
    except urllib.error.HTTPError as e:
        raise ModelDiscoveryError(
            f"Failed to fetch models from {url}: {format_http_error(e)}"
        ) from e
    except Exception as e:
        raise ModelDiscoveryError(f"Failed to fetch models from {url}: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ModelDiscoveryError(f"Malformed model inventory from {url}")
    if any(
        not isinstance(model, dict)
        or not isinstance(model.get("id"), str)
        or not model["id"]
        for model in data["data"]
    ):
        raise ModelDiscoveryError(f"Malformed model inventory entry from {url}")
    model_ids = [model["id"] for model in data["data"]]
    _validate_unique_discovered_model_ids(model_ids, url)
    return model_ids


def _fetch_gemini_models(api_base: str, api_key: str) -> list[str]:
    """Fetch models using Gemini /v1beta/models endpoint (Bearer auth)."""
    url = f"{_join_api_base(api_base, '/v1beta')}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        data = request_json(url, headers=headers, timeout=30)
    except urllib.error.HTTPError as e:
        raise ModelDiscoveryError(
            f"Failed to fetch models from {url}: {format_http_error(e)}"
        ) from e
    except Exception as e:
        raise ModelDiscoveryError(f"Failed to fetch models from {url}: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ModelDiscoveryError(f"Malformed model inventory from {url}")
    model_ids = []
    for model in data["models"]:
        if (
            not isinstance(model, dict)
            or not isinstance(model.get("name"), str)
            or not model["name"]
        ):
            raise ModelDiscoveryError(f"Malformed model inventory entry from {url}")
        name = model["name"]
        if name.startswith("models/"):
            name = name[len("models/") :]
        if not name:
            raise ModelDiscoveryError(f"Malformed model inventory entry from {url}")
        model_ids.append(name)
    _validate_unique_discovered_model_ids(model_ids, url)
    return model_ids


def _validate_unique_discovered_model_ids(model_ids: list[str], source: str) -> None:
    if len(model_ids) != len(set(model_ids)):
        raise DuplicateModelInventoryError(
            f"Model inventory from {source} contains duplicate IDs"
        )


def fetch_models_from_api(api_base: str, api_key: str, provider: str) -> list[str]:
    """Fetch available model IDs from a provider's /models endpoint.

    Tries the provider-specific endpoint first, then falls back to
    OpenAI-compatible /v1/models for anthropic and gemini interfaces.

    Returns a list of model ID strings.
    """
    if provider == "openai":
        return _fetch_openai_models(api_base, api_key)

    if provider == "anthropic":
        primary_error = None
        try:
            models = _fetch_anthropic_models(api_base, api_key)
        except DuplicateModelInventoryError:
            raise
        except ModelDiscoveryError as exc:
            primary_error = exc
            models = []
        if not models:
            logger.info(f"Falling back to OpenAI-compatible endpoint for {api_base}")
            try:
                models = _fetch_openai_models(api_base, api_key)
            except ModelDiscoveryError as exc:
                if primary_error is not None:
                    raise ModelDiscoveryError(
                        f"Provider model discovery failed for both Anthropic and OpenAI-compatible endpoints: {primary_error}; {exc}"
                    ) from exc
                raise
        return models

    if provider == "gemini":
        primary_error = None
        try:
            models = _fetch_gemini_models(api_base, api_key)
        except DuplicateModelInventoryError:
            raise
        except ModelDiscoveryError as exc:
            primary_error = exc
            models = []
        if not models:
            logger.info(f"Falling back to OpenAI-compatible endpoint for {api_base}")
            try:
                models = _fetch_openai_models(api_base, api_key)
            except ModelDiscoveryError as exc:
                if primary_error is not None:
                    raise ModelDiscoveryError(
                        f"Provider model discovery failed for both Gemini and OpenAI-compatible endpoints: {primary_error}; {exc}"
                    ) from exc
                raise
        return models

    # Unknown provider: try OpenAI-compatible
    return _fetch_openai_models(api_base, api_key)


def _fetch_anthropic_models(api_base: str, api_key: str) -> list[str]:
    """Fetch models from Anthropic API with pagination support.

    Anthropic uses x-api-key auth and paginates via has_more / after_id.
    Response: {"data": [{"id": "..."}], "has_more": bool, "last_id": "..."}
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    model_ids = []
    seen_cursors: set[str] = set()
    base_url = f"{_join_api_base(api_base, '/v1')}/models"
    url = base_url

    while True:
        try:
            data = request_json(url, headers=headers, timeout=30)
        except urllib.error.HTTPError as e:
            raise ModelDiscoveryError(
                f"Failed to fetch models from {url}: {format_http_error(e)}"
            ) from e
        except Exception as e:
            raise ModelDiscoveryError(f"Failed to fetch models from {url}: {e}") from e

        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ModelDiscoveryError(f"Malformed model inventory from {url}")
        if any(
            not isinstance(model, dict)
            or not isinstance(model.get("id"), str)
            or not model["id"]
            for model in data["data"]
        ):
            raise ModelDiscoveryError(f"Malformed model inventory entry from {url}")
        model_ids.extend(model["id"] for model in data["data"])

        # Handle pagination
        has_more = data.get("has_more", False)
        if not isinstance(has_more, bool):
            raise ModelDiscoveryError(f"Malformed pagination metadata from {url}")
        if has_more:
            last_id = data.get("last_id")
            if not isinstance(last_id, str) or not last_id:
                raise ModelDiscoveryError(
                    f"Malformed model inventory from {url}: has_more missing last_id"
                )
            if last_id in seen_cursors:
                raise ModelDiscoveryError(
                    f"Repeated Anthropic pagination cursor from {url}: {last_id}"
                )
            seen_cursors.add(last_id)
            separator = "&" if "?" in base_url else "?"
            encoded_cursor = urllib.parse.quote(last_id, safe="")
            url = f"{base_url}{separator}after_id={encoded_cursor}"
        else:
            break

    _validate_unique_discovered_model_ids(model_ids, base_url)
    return model_ids


def natural_sort_key(value: str):
    parts = re.split(r"(\d+(?:[.-]\d+)*)", value)
    key = []
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"\d+(?:[.-]\d+)*", part):
            key.append((0, tuple(int(token) for token in re.split(r"[.-]", part))))
        else:
            key.append((1, part))
    return key


def sort_model_payloads(model_payloads: list[dict]) -> list[dict]:
    return sorted(
        model_payloads,
        key=lambda payload: (
            natural_sort_key(
                payload.get("litellm_params", {}).get("litellm_credential_name", "")
            ),
            natural_sort_key(payload["model_name"]),
        ),
        reverse=True,
    )


def build_model_info(
    model_cfg: dict,
    provider_access_groups: list | None,
    base_model_map: dict,
    litellm_model_name: str,
    model_name: str,
    default_access_groups: list | None = None,
) -> dict:
    model_info_cfg = model_cfg.get("model_info", {})
    base_model = model_info_cfg.get("base_model")
    access_groups = model_cfg.get("access_groups")

    resolved_base_model = (
        base_model
        or base_model_map.get(litellm_model_name)
        or base_model_map.get(model_name)
        or litellm_model_name
    )
    resolved_access_groups = (
        access_groups
        if access_groups is not None
        else model_info_cfg.get("access_groups", provider_access_groups)
    )

    model_info = dict(model_info_cfg)
    if resolved_base_model:
        model_info["base_model"] = resolved_base_model
    if resolved_access_groups is not None:
        model_info["access_groups"] = resolved_access_groups
    elif default_access_groups is not None:
        model_info["access_groups"] = default_access_groups
    return model_info


def resolve_provider_models_with_alias_targets(
    providers: dict,
    base_model_map: dict = None,
    require_complete: bool = False,
) -> tuple[list, list, dict]:
    """Resolve providers into model payloads and derived public model hub entries.

    For each provider, for each interface, for each model:
    1. Use interface-specific `models` from providers.<provider>.interfaces.<interface>.models
    2. If `models` is empty, auto-fetch from the interface/provider /models API endpoint
    3. Resolve base_model: explicit > model_name_base_model_map > model_name
    4. Resolve access_groups (model-level overrides provider-level)
    5. Resolve public model hub visibility (model-level overrides provider-level)
    6. Build the full model_name, litellm_params, and model_info

    Args:
        providers: Resolved provider configurations
        base_model_map: Global model_name -> base_model mapping (fallback)

    Returns a tuple of (model payloads, derived public model hub entries).
    """
    models = []
    public_model_hub = []
    model_alias_targets = {}
    base_model_map = base_model_map or {}

    for service_name, provider_config in providers.items():
        provider_access_groups = provider_config.get("access_groups")
        provider_is_public_model_hub = provider_config.get("is_public_model_hub", False)
        api_key = provider_config.get("api_key")

        if not api_key:
            if require_complete and provider_config.get("interfaces"):
                raise ModelDiscoveryError(
                    f"Provider '{service_name}' is missing api_key; refusing prune-safe generation"
                )
            continue

        interfaces = provider_config.get("interfaces", {})
        if not isinstance(interfaces, dict):
            raise ModelDiscoveryError(
                f"Provider '{service_name}' interfaces must be an object"
            )

        provider_default_model = provider_config.get("default_model") or {}
        provider_default_models = provider_config.get("models", {})
        provider_autofill_disabled = provider_config.get("models_autofill_disabled", False)

        for provider, iface_config in interfaces.items():
            iface = iface_config if iface_config else {}
            interface_models = iface.get("models", {})
            iface_models = {**provider_default_models, **interface_models}
            # Interface-level setting overrides provider-level default
            autofill_disabled = iface.get("models_autofill_disabled", provider_autofill_disabled)
            model_name_prefix = iface.get("model_name_prefix") if iface.get("model_name_prefix") is not None else f"{provider}/"
            models_api_base = _get_interface_models_api_base(provider_config, iface)

            # Auto-discover models from API unless autofill is disabled
            if require_complete and not autofill_disabled and not models_api_base:
                raise ModelDiscoveryError(
                    f"Model discovery for {service_name}/{provider} has no models API endpoint; "
                    "set api_base or explicitly disable autofill"
                )
            if not autofill_disabled and models_api_base:
                logger.info(
                    f"Autofilling {service_name}/{provider}, fetching from API..."
                )
                fetched_ids = fetch_models_from_api(models_api_base, api_key, provider)
                if fetched_ids:
                    # Only add models not already explicitly defined
                    new_ids = [m for m in fetched_ids if m not in iface_models]
                    if new_ids:
                        logger.info(
                            f"Discovered {len(new_ids)} new models for "
                            f"{service_name}/{provider}: {new_ids}"
                        )
                        fetched_models = {model_id: None for model_id in new_ids}
                        # Merge: explicit definitions take precedence
                        iface_models = {**fetched_models, **iface_models}
                    else:
                        logger.info(
                            f"All {len(fetched_ids)} fetched models already defined "
                            f"for {service_name}/{provider}"
                        )
                else:
                    if require_complete:
                        raise ModelDiscoveryError(
                            f"Model discovery for {service_name}/{provider} returned no models; "
                            "refusing prune-safe generation"
                        )
                    logger.warning(
                        f"No models discovered for {service_name}/{provider}"
                    )

            credential_name = f"{service_name}-{provider}"

            for litellm_model_name, discovered_model_cfg in iface_models.items():
                provider_model_cfg = provider_default_models.get(
                    litellm_model_name,
                    {},
                )
                if litellm_model_name in interface_models:
                    interface_model_cfg = interface_models[litellm_model_name]
                elif litellm_model_name not in provider_default_models:
                    interface_model_cfg = discovered_model_cfg
                else:
                    interface_model_cfg = {}

                model_cfg = deep_merge(
                    provider_default_model,
                    provider_model_cfg if isinstance(provider_model_cfg, dict) else {},
                )
                model_cfg = deep_merge(
                    model_cfg,
                    interface_model_cfg if isinstance(interface_model_cfg, dict) else {},
                )
                if model_cfg.get("ignored"):
                    continue
                model_names_cfg = model_cfg.get("model_names")

                target_model = f"{provider}/{litellm_model_name}"
                target_litellm_params = dict(model_cfg.get("litellm_params", {}))
                target_litellm_params.update(
                    {
                        "model": target_model,
                        "litellm_credential_name": credential_name,
                    }
                )
                target_model_info = build_model_info(
                    model_cfg,
                    provider_access_groups,
                    base_model_map,
                    litellm_model_name,
                    target_model,
                    default_access_groups=["General"],
                )
                model_alias_targets.setdefault(target_model, []).append(
                    {
                        "litellm_params": target_litellm_params,
                        "model_info": target_model_info,
                        "alias_target_cfg": model_cfg,
                    }
                )

                derived_model_name = f"{model_name_prefix}{litellm_model_name}"

                # Resolve model-level model_name_prefix override (non-object form).
                # Object form entries get per-entry prefix (step 2 below).
                model_prefix = model_cfg.get("model_name_prefix", model_name_prefix)

                # Parse model_names: comma-separated list or per-name overrides.
                # Leading comma means "include the derived name too":
                #   ",claude-opus" -> [("<derived>", {}), ("<prefix>claude-opus", {})]
                #   "claude-opus"  -> [("<prefix>claude-opus", {})]
                #   {"$self": {}, "primary": {...}} -> [("<derived>", {}), ("<prefix>primary", {...})]
                #   ""  or None    -> [("<derived>", {})]
                if isinstance(model_names_cfg, str) and model_names_cfg:
                    model_name_entries = []
                    for i, part in enumerate(model_names_cfg.split(",")):
                        stripped = part.strip()
                        if stripped:
                            model_name_entries.append((f"{model_prefix}{stripped}", {}))
                        elif i == 0:
                            model_name_entries.append((derived_model_name, {}))
                    if not model_name_entries:
                        model_name_entries = [(derived_model_name, {})]
                elif isinstance(model_names_cfg, dict):
                    model_name_entries = []
                    for name, override_cfg in model_names_cfg.items():
                        override_cfg = override_cfg or {}
                        # Per-entry prefix overrides model-level, which overrides interface-level.
                        # Use pop() to strip model_name_prefix from overrides to avoid leaking into model_info.
                        prefix = override_cfg.pop("model_name_prefix", model_prefix)
                        if name == "$self":
                            model_name = f"{prefix}{litellm_model_name}"
                        else:
                            model_name = f"{prefix}{name}"
                        model_name_entries.append((model_name, override_cfg))
                    if not model_name_entries:
                        model_name_entries = [(derived_model_name, {})]
                else:
                    model_name_entries = [(f"{model_prefix}{litellm_model_name}", {})]

                # Strip model_name_prefix from model_cfg to avoid leaking into model_info/litellm_params
                model_cfg.pop("model_name_prefix", None)

                for model_name, model_name_override in model_name_entries:
                    effective_model_cfg = deep_merge(model_cfg, model_name_override)
                    model_info = build_model_info(
                        effective_model_cfg,
                        provider_access_groups,
                        base_model_map,
                        litellm_model_name,
                        model_name,
                    )
                    litellm_params_cfg = effective_model_cfg.get("litellm_params", {})
                    is_public_model_hub = effective_model_cfg.get("is_public_model_hub")

                    resolved_is_public_model_hub = (
                        is_public_model_hub
                        if is_public_model_hub is not None
                        else provider_is_public_model_hub
                    )

                    # Build litellm_params
                    litellm_params = dict(litellm_params_cfg)
                    litellm_params.update(
                        {
                            "model": f"{provider}/{litellm_model_name}",
                            "litellm_credential_name": credential_name,
                        }
                    )

                    models.append(
                        {
                            "model_name": model_name,
                            "litellm_params": litellm_params,
                            "model_info": model_info,
                        }
                    )

                    if resolved_is_public_model_hub:
                        public_model_hub.append(model_name)

    return models, public_model_hub, model_alias_targets


def resolve_provider_models(providers: dict, base_model_map: dict = None) -> tuple[list, list]:
    models, public_model_hub, _ = resolve_provider_models_with_alias_targets(
        providers,
        base_model_map,
    )
    return models, public_model_hub


def _replace_interface_in_targets(target_map: dict, iface: str) -> dict:
    """Replace $interface/ in target keys and $extends values with a concrete interface."""
    result = {}
    for tk, tv in target_map.items():
        if tk == "$extends":
            if isinstance(tv, str):
                result[tk] = tv.replace("$interface/", f"{iface}/")
            elif isinstance(tv, list):
                result[tk] = [
                    item.replace("$interface/", f"{iface}/")
                    if isinstance(item, str) else item
                    for item in tv
                ]
            else:
                result[tk] = tv
        elif isinstance(tk, str) and tk.startswith("$interface/"):
            result[f"{iface}/{tk[len('$interface/'):]}"] = tv
        else:
            result[tk] = tv
    return result


def _has_interface_vars(target_map: dict) -> bool:
    return any(
        isinstance(k, str) and k.startswith("$interface/")
        for k in target_map
    )


def expand_interface_vars(model_aliases: dict, interfaces: set[str]) -> dict:
    """Expand $interface/ prefixes in model_aliases keys and target keys.

    - "$interface/secondary" as a key expands into one entry per known interface
      (e.g. "anthropic/secondary", "openai/secondary").
    - "$interface/model" inside targets resolves from the outer key's interface,
      whether the outer key is "$interface/..." or "anthropic/...".
    """
    if not interfaces:
        return model_aliases

    expanded = {}
    sorted_interfaces = sorted(interfaces)

    for key, target_map in model_aliases.items():
        if key.startswith("$interface/"):
            suffix = key[len("$interface/"):]
            for iface in sorted_interfaces:
                expanded_key = f"{iface}/{suffix}"
                expanded_targets = _replace_interface_in_targets(target_map, iface)
                if expanded_key in expanded:
                    expanded[expanded_key] = {**expanded[expanded_key], **expanded_targets}
                else:
                    expanded[expanded_key] = expanded_targets
        else:
            detected_iface = next(
                (iface for iface in interfaces if key.startswith(f"{iface}/")),
                None,
            )
            if detected_iface and _has_interface_vars(target_map):
                expanded[key] = _replace_interface_in_targets(target_map, detected_iface)
            else:
                expanded[key] = target_map

    return expanded


def _replace_interface_in_fallback_values(values: list, iface: str) -> list:
    return [
        value.replace("$interface/", f"{iface}/")
        if isinstance(value, str) and value.startswith("$interface/")
        else value
        for value in values
    ]


def expand_interface_fallbacks(fallbacks: list, interfaces: set[str]) -> list:
    """Expand $interface/ prefixes in fallback keys and target lists.

    - "$interface/primary" as a key expands into one entry per known interface.
    - "$interface/secondary" inside targets resolves from the fallback key's interface.
    """
    if not interfaces:
        return fallbacks

    expanded = []
    sorted_interfaces = sorted(interfaces)

    for entry in fallbacks:
        for key, values in entry.items():
            keys = [subkey.strip() for subkey in key.split(",") if subkey.strip()]
            for subkey in keys:
                if subkey.startswith("$interface/"):
                    suffix = subkey[len("$interface/"):]
                    for iface in sorted_interfaces:
                        expanded.append(
                            {
                                f"{iface}/{suffix}": _replace_interface_in_fallback_values(
                                    values,
                                    iface,
                                )
                            }
                        )
                else:
                    detected_iface = next(
                        (
                            iface
                            for iface in interfaces
                            if subkey.startswith(f"{iface}/")
                        ),
                        None,
                    )
                    expanded_values = values
                    if detected_iface:
                        expanded_values = _replace_interface_in_fallback_values(
                            values,
                            detected_iface,
                        )
                    expanded.append({subkey: expanded_values})

    return expanded


def _resolve_alias_group(
    model_aliases: dict,
    alias_name: str,
    resolving: set | None = None,
    *,
    require_complete: bool = False,
) -> dict:
    """Resolve a single model_aliases entry, expanding $extends references recursively."""
    if resolving is None:
        resolving = set()
    if alias_name in resolving:
        if require_complete:
            raise ModelDiscoveryError(f"Circular model_aliases $extends: {alias_name}")
        logger.warning(f"⚠️ Circular $extends detected: {alias_name}")
        return {}
    resolving = resolving | {alias_name}

    target_map = model_aliases.get(alias_name, {})
    extends = target_map.get("$extends")

    if extends is None:
        return {k: v for k, v in target_map.items() if k != "$extends"}

    if isinstance(extends, str):
        extends = [extends]

    merged = {}
    for ref in extends:
        if ref not in model_aliases:
            if require_complete:
                raise ModelDiscoveryError(
                    f"model_aliases '{alias_name}' extends unknown alias group: {ref}"
                )
            logger.warning(
                f"⚠️ model_aliases '{alias_name}' extends unknown alias group: {ref}"
            )
            continue
        merged.update(
            _resolve_alias_group(
                model_aliases,
                ref,
                resolving,
                require_complete=require_complete,
            )
        )

    for k, v in target_map.items():
        if k != "$extends":
            merged[k] = v

    return merged


def expand_model_aliases(
    model_aliases: dict,
    model_alias_targets: dict,
    *,
    require_complete: bool = False,
) -> list[dict]:
    expanded = []

    for model_name, raw_target_map in model_aliases.items():
        target_map = _resolve_alias_group(
            model_aliases,
            model_name,
            require_complete=require_complete,
        )
        for target_model, override_cfg in target_map.items():
            target_payloads = model_alias_targets.get(target_model)
            if not target_payloads:
                if require_complete:
                    raise ModelDiscoveryError(
                        f"model_aliases '{model_name}' points to unknown model: {target_model}"
                    )
                logger.warning(
                    f"⚠️ model_aliases '{model_name}' points to unknown model: "
                    f"{target_model}"
                )
                continue
            if len(target_payloads) > 1:
                if require_complete:
                    raise ModelDiscoveryError(
                        f"model_aliases '{model_name}' target '{target_model}' "
                        "matched multiple providers"
                    )
                logger.warning(
                    f"⚠️ model_aliases '{model_name}' target '{target_model}' "
                    f"matched multiple providers; using the first match"
                )

            target_payload = target_payloads[0]
            alias_cfg = deep_merge(
                target_payload.get("alias_target_cfg", {}),
                override_cfg or {},
            )
            model_info = deep_merge(
                dict(target_payload["model_info"]),
                alias_cfg.get("model_info", {}),
            )
            if "access_groups" in alias_cfg:
                model_info["access_groups"] = alias_cfg["access_groups"]
            expanded.append(
                {
                    "model_name": model_name,
                    "litellm_params": {
                        **target_payload["litellm_params"],
                        **alias_cfg.get("litellm_params", {}),
                    },
                    "model_info": model_info,
                }
            )

    return expanded


_ALIAS_REF_PATTERN = re.compile(r"^\$models:(.+?)/(.+?)$")


def expand_alias_refs(
    aliases: dict,
    models: list,
    *,
    require_complete: bool = False,
) -> dict:
    """Expand $models:<service>/<interface> references in alias keys.

    When an alias key matches $models:<service>/<interface>,
    it expands into one alias per model in that provider/interface:
        raw_model_name -> prefixed_model_name

    If the alias value is non-empty, it is used as the target for all expanded
    aliases instead of the model_name.
    """
    expanded = {}

    for key, value in aliases.items():
        match = _ALIAS_REF_PATTERN.match(key)
        if not match:
            expanded[key] = value
            continue

        service_name = match.group(1)
        interface = match.group(2)
        credential_name = f"{service_name}-{interface}"

        found = False
        for model in models:
            if model["litellm_params"].get("litellm_credential_name") != credential_name:
                continue
            found = True

            # Extract raw model name by stripping the "provider/" prefix
            litellm_model = model["litellm_params"]["model"]
            raw_name = (
                litellm_model.split("/", 1)[1]
                if "/" in litellm_model
                else litellm_model
            )
            model_name = model["model_name"]

            # Only alias when raw_name differs from model_name (i.e. a prefix exists)
            if raw_name != model_name:
                expanded[raw_name] = value if value else model_name

        if not found:
            if require_complete:
                raise ModelDiscoveryError(
                    f"Alias ref '{key}' matched no models "
                    f"(service={service_name}, interface={interface})"
                )
            logger.warning(
                f"⚠️ Alias ref '{key}' matched no models "
                f"(service={service_name}, interface={interface})"
            )

    return expanded


def expand_guardrails(
    guardrails: dict | list,
    *,
    require_complete: bool = False,
) -> list[dict]:
    """Convert guardrails keyed by name into LiteLLM's API list format."""
    if isinstance(guardrails, list):
        return [dict(guardrail) for guardrail in guardrails]
    if not isinstance(guardrails, dict):
        if require_complete:
            raise ModelDiscoveryError("Top-level 'guardrails' must be an object")
        logger.warning("⚠️ Top-level 'guardrails' must be an object; ignoring guardrails")
        return []

    expanded = []
    for guardrail_name, guardrail_config in guardrails.items():
        if not isinstance(guardrail_config, dict):
            if require_complete:
                raise ModelDiscoveryError(
                    f"Guardrail '{guardrail_name}' must be an object"
                )
            logger.warning(
                f"⚠️ Guardrail '{guardrail_name}' must be an object; skipping"
            )
            continue
        expanded.append({**guardrail_config, "guardrail_name": guardrail_name})
    return expanded


def router_fallback_references(settings: dict) -> set[str]:
    """Validate LiteLLM's nullable fallback lists and collect model references.

    Router.default_fallbacks is list[str] | None, unlike the three lists of
    source-to-target rules. LiteLLM also projects defaults into fallbacks as '*'.
    """
    references: set[str] = set()
    for field in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
        rules = settings.get(field)
        if rules is None:
            continue
        if not isinstance(rules, list):
            raise ModelDiscoveryError(f"Router settings contains invalid {field}")
        for rule in rules:
            if not isinstance(rule, dict) or not rule:
                raise ModelDiscoveryError(f"Router settings contains invalid {field}")
            for source, targets in rule.items():
                if (
                    not isinstance(source, str)
                    or not source
                    or not isinstance(targets, list)
                    or any(not isinstance(target, str) or not target for target in targets)
                ):
                    raise ModelDiscoveryError(f"Router settings contains invalid {field}")
                references.add(source)
                references.update(targets)
    defaults = settings.get("default_fallbacks")
    if defaults is not None:
        if not isinstance(defaults, list) or any(
            not isinstance(target, str) or not target for target in defaults
        ):
            raise ModelDiscoveryError("Router settings contains invalid default_fallbacks")
        references.update(defaults)
    return references


def _validate_complete_config_shape(config: dict, base_config: dict) -> None:
    """Validate every section that synchronization may materialize or replace."""
    if not isinstance(config, dict) or not isinstance(base_config, dict):
        raise ModelDiscoveryError("Configuration root must be an object")

    object_sections = (
        "providers",
        "model_name_base_model_map",
        "model_aliases",
        "router_settings",
    )
    for section in object_sections:
        value = config.get(section, {})
        if not isinstance(value, dict):
            raise ModelDiscoveryError(f"Top-level '{section}' must be an object")

    for field in (
        "public_model_hub_autofill_disabled",
        "public_model_hub_aliases_autofill_enabled",
    ):
        if field in config and not isinstance(config[field], bool):
            raise ModelDiscoveryError(f"Top-level '{field}' must be a boolean")

    router_settings = config.get("router_settings", {})
    if "routing_strategy" in router_settings and not isinstance(
        router_settings["routing_strategy"], str
    ):
        raise ModelDiscoveryError("router_settings routing_strategy must be a string")
    for field in ("cooldown_time", "timeout"):
        if field in router_settings and (
            isinstance(router_settings[field], bool)
            or not isinstance(router_settings[field], (int, float))
        ):
            raise ModelDiscoveryError(f"router_settings {field} must be a number")
    if "num_retries" in router_settings and (
        isinstance(router_settings["num_retries"], bool)
        or not isinstance(router_settings["num_retries"], int)
    ):
        raise ModelDiscoveryError("router_settings num_retries must be an integer")

    guardrails = config.get("guardrails", {})
    if not isinstance(guardrails, (dict, list)):
        raise ModelDiscoveryError("Top-level 'guardrails' must be an object or list")

    def validate_guardrail(name: object, guardrail: object) -> None:
        if not isinstance(name, str) or not name:
            raise ModelDiscoveryError("Top-level 'guardrails' contains an invalid entry")
        if not isinstance(guardrail, dict):
            raise ModelDiscoveryError(f"Guardrail '{name}' must be an object")
        params = guardrail.get("litellm_params")
        if not isinstance(params, dict):
            raise ModelDiscoveryError(
                f"Guardrail '{name}' litellm_params must be an object"
            )
        mode = params.get("mode")
        valid_mode = (
            isinstance(mode, str) and bool(mode)
        ) or (
            isinstance(mode, list)
            and bool(mode)
            and all(isinstance(item, str) and item for item in mode)
        )
        if (
            not isinstance(params.get("guardrail"), str)
            or not params["guardrail"]
            or not valid_mode
        ):
            raise ModelDiscoveryError(
                f"Guardrail '{name}' requires non-empty guardrail and mode values"
            )

    guardrail_names: set[str] = set()
    if isinstance(guardrails, list):
        for guardrail in guardrails:
            if (
                not isinstance(guardrail, dict)
                or not isinstance(guardrail.get("guardrail_name"), str)
                or not guardrail["guardrail_name"]
            ):
                raise ModelDiscoveryError(
                    "Top-level 'guardrails' list contains an invalid entry"
                )
            name = guardrail["guardrail_name"]
            if name in guardrail_names:
                raise ModelDiscoveryError(
                    f"Top-level 'guardrails' contains duplicate guardrail name: {name}"
                )
            guardrail_names.add(name)
            validate_guardrail(name, guardrail)
    else:
        for name, guardrail in guardrails.items():
            validate_guardrail(name, guardrail)

    def validate_optional_strings(value: dict, label: str) -> None:
        for field in (
            "api_base",
            "models_api_base",
            "api_key",
            "models_api_key",
            "model_name_prefix",
        ):
            if (
                field in value
                and not isinstance(value[field], str)
            ):
                raise ModelDiscoveryError(f"{label} {field} must be a string")

    def validate_optional_booleans(value: dict, label: str) -> None:
        for field in (
            "models_autofill_disabled",
            "ignored",
            "is_public_model_hub",
        ):
            if field in value and not isinstance(value[field], bool):
                raise ModelDiscoveryError(f"{label} {field} must be a boolean")

    def validate_model_config(value: dict, label: str) -> None:
        validate_optional_booleans(value, label)
        if "model_name_prefix" in value and not isinstance(
            value["model_name_prefix"], str
        ):
            raise ModelDiscoveryError(f"{label} model_name_prefix must be a string")
        for section in ("model_info", "litellm_params"):
            if section in value and not isinstance(value[section], dict):
                raise ModelDiscoveryError(f"{label} {section} must be an object")
        model_names = value.get("model_names")
        if model_names is not None and not isinstance(model_names, (str, dict)):
            raise ModelDiscoveryError(
                f"{label} model_names must be a string or object"
            )
        if isinstance(model_names, dict) and any(
            not isinstance(name, str)
            or not name
            or (override is not None and not isinstance(override, dict))
            for name, override in model_names.items()
        ):
            raise ModelDiscoveryError(
                f"{label} model_names contains an invalid entry"
            )
        if isinstance(model_names, dict):
            for name, override in model_names.items():
                if isinstance(override, dict):
                    validate_model_config(
                        override,
                        f"{label} model_names '{name}'",
                    )

    def validate_model_map(value: object, label: str) -> None:
        if not isinstance(value, dict):
            raise ModelDiscoveryError(f"{label} must be an object")
        for model_name, model_config in value.items():
            if (
                not isinstance(model_name, str)
                or not model_name
                or (model_config is not None and not isinstance(model_config, dict))
            ):
                raise ModelDiscoveryError(f"{label} contains an invalid model entry")
            if isinstance(model_config, dict):
                validate_model_config(model_config, f"{label} '{model_name}'")

    for service_name, provider in config.get("providers", {}).items():
        if not isinstance(service_name, str) or not service_name or not isinstance(provider, dict):
            raise ModelDiscoveryError(f"Provider '{service_name}' must be an object")
        validate_optional_strings(provider, f"Provider '{service_name}'")
        validate_optional_booleans(provider, f"Provider '{service_name}'")
        interfaces = provider.get("interfaces", {})
        if not isinstance(interfaces, dict):
            raise ModelDiscoveryError(f"Provider '{service_name}' interfaces must be an object")
        validate_model_map(provider.get("models", {}), f"Provider '{service_name}' models")
        default_model = provider.get("default_model", {})
        if not isinstance(default_model, dict):
            raise ModelDiscoveryError(
                f"Provider '{service_name}' default_model must be an object"
            )
        validate_model_config(default_model, f"Provider '{service_name}' default_model")
        for interface_name, interface in interfaces.items():
            if (
                not isinstance(interface_name, str)
                or not interface_name
                or (interface is not None and not isinstance(interface, dict))
            ):
                raise ModelDiscoveryError(
                    f"Provider '{service_name}' interface '{interface_name}' must be an object"
                )
            interface = interface or {}
            validate_optional_strings(
                interface,
                f"Provider '{service_name}' interface '{interface_name}'",
            )
            validate_optional_booleans(
                interface,
                f"Provider '{service_name}' interface '{interface_name}'",
            )
            validate_model_map(
                interface.get("models", {}),
                f"Provider '{service_name}' interface '{interface_name}' models",
            )

    for alias_name, target_map in config.get("model_aliases", {}).items():
        if not isinstance(alias_name, str) or not alias_name or not isinstance(target_map, dict):
            raise ModelDiscoveryError(f"model_aliases '{alias_name}' must be an object")
        extends = target_map.get("$extends")
        if extends is not None and not (
            isinstance(extends, str)
            or (
                isinstance(extends, list)
                and all(isinstance(item, str) and item for item in extends)
            )
        ):
            raise ModelDiscoveryError(
                f"model_aliases '{alias_name}' has an invalid $extends value"
            )
        for target_name, target_config in target_map.items():
            if target_name == "$extends":
                continue
            if (
                not isinstance(target_name, str)
                or not target_name
                or (target_config is not None and not isinstance(target_config, dict))
            ):
                raise ModelDiscoveryError(
                    f"model_aliases '{alias_name}' contains an invalid target"
                )
            if isinstance(target_config, dict):
                validate_model_config(
                    target_config,
                    f"model_aliases '{alias_name}' target '{target_name}'",
                )

    aliases = config.get("aliases")
    if aliases is not None and (
        not isinstance(aliases, dict)
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(target, str)
            or (not target and not name.startswith("$models:"))
            for name, target in aliases.items()
        )
    ):
        raise ModelDiscoveryError(
            "Top-level 'aliases' must be an object of non-empty string identifiers and targets or null"
        )

    for section in ("credentials", "models", "fallbacks", "public_model_hub"):
        value = config.get(section, [])
        if value is not None and not isinstance(value, list):
            raise ModelDiscoveryError(f"Top-level '{section}' must be a list or null")

    for fallback in config.get("fallbacks") or []:
        if not isinstance(fallback, dict) or not fallback:
            raise ModelDiscoveryError("Top-level 'fallbacks' contains an invalid rule")
        for source, targets in fallback.items():
            if (
                not isinstance(source, str)
                or not source
                or not isinstance(targets, list)
                or any(not isinstance(target, str) or not target for target in targets)
            ):
                raise ModelDiscoveryError(
                    "Top-level 'fallbacks' contains an invalid rule"
                )

    reserved_router_sections = {"model_group_alias", "fallbacks"}
    conflicts = reserved_router_sections & set(config.get("router_settings", {}))
    if conflicts:
        raise ModelDiscoveryError(
            "router_settings contains reserved routing sections: "
            + ", ".join(sorted(conflicts))
        )
    router_fallback_references(config.get("router_settings", {}))

    model_identities: set[tuple[str, str | None]] = set()
    for model in config.get("models") or []:
        if (
            not isinstance(model, dict)
            or not isinstance(model.get("model_name"), str)
            or not model["model_name"]
            or not isinstance(model.get("litellm_params"), dict)
            or not isinstance(model.get("model_info", {}), dict)
        ):
            raise ModelDiscoveryError("Top-level 'models' contains an invalid entry")
        credential_name = model["litellm_params"].get("litellm_credential_name")
        model_target = model["litellm_params"].get("model")
        if not isinstance(model_target, str) or not model_target:
            raise ModelDiscoveryError(
                "Top-level 'models' contains an invalid litellm_params.model"
            )
        if credential_name is not None and not isinstance(credential_name, str):
            raise ModelDiscoveryError(
                "Top-level 'models' contains an invalid litellm_credential_name"
            )
        identity = (model["model_name"], credential_name)
        if identity in model_identities:
            raise ModelDiscoveryError(
                "Top-level 'models' contains duplicate model identities"
            )
        model_identities.add(identity)

    credential_names: set[str] = set()
    for credential in config.get("credentials") or []:
        if (
            not isinstance(credential, dict)
            or not isinstance(credential.get("credential_name"), str)
            or not credential["credential_name"]
            or not isinstance(credential.get("credential_values"), dict)
            or not isinstance(credential.get("credential_info", {}), dict)
        ):
            raise ModelDiscoveryError(
                "Top-level 'credentials' contains an invalid entry"
            )
        if credential["credential_name"] in credential_names:
            raise ModelDiscoveryError(
                "Top-level 'credentials' contains duplicate credential names"
            )
        credential_names.add(credential["credential_name"])

    if any(
        not isinstance(entry, str) or not entry
        for entry in config.get("public_model_hub") or []
    ):
        raise ModelDiscoveryError("Top-level 'public_model_hub' contains an invalid entry")

    if isinstance(guardrails, dict):
        for name, guardrail in guardrails.items():
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(guardrail, dict)
                or not isinstance(guardrail.get("litellm_params"), dict)
            ):
                raise ModelDiscoveryError(f"Guardrail '{name}' must be an object")


def generate_config(
    config_source: Path | dict,
    base_config: dict | None = None,
    *,
    require_complete: bool = False,
) -> dict:
    """Resolve a path-based or already-layered config into deployment-ready format.

    Resolves:
    1. Local config overrides for path-based input
    2. Provider $extend directives
    3. Providers into a flat `models` array of LiteLLM request bodies

    Returns dict with:
    - models: list of LiteLLM /model/new request bodies
    - credentials: list of LiteLLM /credentials request bodies
    - aliases: model alias mappings
    - fallbacks: fallback rules
    - public_model_hub: derived model groups plus explicit aliases to expose in the public model hub
    - router_settings: raw router settings to merge into LiteLLM config
    - guardrails: guardrail definitions expanded to LiteLLM's list format
    """
    if isinstance(config_source, dict):
        config = config_source
        base_config = base_config or config_source
    else:
        config, loaded_base_config = load_config_with_local(config_source)
        base_config = base_config or loaded_base_config

    if require_complete:
        _validate_complete_config_shape(config, base_config)

    providers = resolve_provider_extensions(config.get("providers", {}))

    if require_complete:
        for service_name, provider_config in providers.items():
            # Check resolved intent, not raw layers: interfaces may be inherited.
            # An explicit empty object disables interfaces; omission is incomplete.
            if "interfaces" not in provider_config:
                raise ModelDiscoveryError(
                    f"Provider '{service_name}' is missing interfaces; "
                    "declare or inherit them, or use {} to explicitly disable them"
                )

    # Build credentials list
    credentials_requested = config.get("credentials") is not None or bool(providers)
    credentials = list(config.get("credentials") or [])
    for service_name, provider_config in providers.items():
        api_key = provider_config.get("api_key")
        if not api_key:
            continue
        interfaces = provider_config.get("interfaces", {})
        if not isinstance(interfaces, dict):
            raise ModelDiscoveryError(
                f"Provider '{service_name}' interfaces must be an object"
            )
        for provider, iface_config in interfaces.items():
            if provider not in PROVIDER_CONFIG:
                if require_complete:
                    raise ModelDiscoveryError(
                        f"Unknown provider interface '{provider}' for '{service_name}'"
                    )
                logger.warning(
                    f"Unknown provider '{provider}' for credentials, skipping"
                )
                continue
            iface = iface_config if iface_config else {}
            credentials.append(
                build_credential_payload(
                    service_name,
                    provider,
                    api_key,
                    _get_interface_api_base(provider_config, iface),
                )
            )
    credential_names: set[str] = set()
    for credential in credentials:
        name = credential["credential_name"]
        if name in credential_names:
            raise ModelDiscoveryError(
                f"Configuration contains duplicate credential definitions: {name}"
            )
        credential_names.add(name)
    resolved_credentials = credentials if credentials_requested else None

    # Build flat models array and derive public model hub entries from provider/model defaults
    base_model_map = config.get("model_name_base_model_map", {})
    models, derived_public_model_hub, model_alias_targets = (
        resolve_provider_models_with_alias_targets(
            providers,
            base_model_map,
            require_complete=require_complete,
        )
    )

    # Collect all interface names for $interface expansion
    all_interfaces = set()
    for provider_config in providers.values():
        all_interfaces.update(provider_config.get("interfaces", {}).keys())

    raw_model_aliases = config.get("model_aliases", {})
    resolved_model_aliases = expand_interface_vars(raw_model_aliases, all_interfaces)

    models.extend(
        expand_model_aliases(
            resolved_model_aliases,
            model_alias_targets,
            require_complete=require_complete,
        )
    )
    manual_models = config.get("models")
    if manual_models is None:
        pass
    elif isinstance(manual_models, list):
        models.extend(manual_models)
    else:
        if require_complete:
            raise ModelDiscoveryError("Top-level 'models' must be a list")
        logger.warning("⚠️ Top-level 'models' must be a list; ignoring manual models")
    models = sort_model_payloads(models)
    models_requested = (
        config.get("models") is not None
        or bool(config.get("model_aliases"))
        or bool(providers)
    )
    resolved_models = models if models_requested else None

    # Resolve $interface prefixes before $base refs so base lookups use concrete keys.
    expanded_fallbacks = expand_interface_fallbacks(
        config.get("fallbacks") or [],
        all_interfaces,
    )
    expanded_base_fallbacks = expand_interface_fallbacks(
        base_config.get("fallbacks") or [],
        all_interfaces,
    )

    # Resolve $base references in fallbacks
    fallbacks = resolve_fallback_base_refs(
        expanded_fallbacks,
        expanded_base_fallbacks,
        require_complete=require_complete,
    )
    resolved_fallbacks = fallbacks if config.get("fallbacks") is not None else None

    raw_aliases = config.get("aliases")
    if raw_aliases is not None and not isinstance(raw_aliases, dict):
        if require_complete:
            raise ModelDiscoveryError("Top-level 'aliases' must be an object or null")
        aliases = raw_aliases
    else:
        aliases = (
            expand_alias_refs(
                raw_aliases,
                models,
                require_complete=require_complete,
            )
            if isinstance(raw_aliases, dict)
            else raw_aliases
        )
    explicit_public_model_hub = config.get("public_model_hub")
    if explicit_public_model_hub is not None:
        public_model_hub = list(explicit_public_model_hub)
        resolved_public_model_hub = public_model_hub
    else:
        public_model_hub = []
        if not config.get("public_model_hub_autofill_disabled", False):
            public_model_hub.extend(derived_public_model_hub)
        if config.get("public_model_hub_aliases_autofill_enabled", False) and isinstance(
            aliases, dict
        ):
            public_model_hub.extend(aliases.keys())
        resolved_public_model_hub = public_model_hub or None

    # Validate aliases, fallbacks, and public model hub entries against known models
    model_names = {m["model_name"] for m in models}
    references_valid = all(
        (
            validate_aliases(aliases or {}, model_names),
            validate_fallbacks(fallbacks, model_names, aliases or {}),
            validate_public_model_hub(public_model_hub, model_names, aliases or {}),
        )
    )
    if require_complete and not references_valid:
        raise ModelDiscoveryError(
            "Configuration contains unresolved alias, fallback, or public model hub references"
        )
    if require_complete:
        validate_alias_cycles(aliases or {}, model_names)
    validate_prices(models)

    return {
        "credentials": resolved_credentials,
        "models": resolved_models,
        "aliases": aliases,
        "fallbacks": resolved_fallbacks,
        "public_model_hub": resolved_public_model_hub,
        "router_settings": config.get("router_settings", {}),
        "guardrails": expand_guardrails(
            config.get("guardrails", {}),
            require_complete=require_complete,
        ),
    }


def _matches_known_model_aliases(value: str, model_names: set[str], alias_keys: set[str]) -> bool:
    return value in model_names | alias_keys


def validate_aliases(aliases: dict, model_names: set):
    """Validate that alias targets point to existing models or other aliases."""
    valid_targets = model_names | set(aliases.keys())
    alias_keys = set(aliases.keys())
    valid = True
    for alias_name, target in aliases.items():
        if target not in valid_targets and not _matches_known_model_aliases(target, model_names, alias_keys):
            valid = False
            logger.warning(
                f"⚠️ Alias '{alias_name}' points to non-existent model: {target}"
            )
    return valid


def validate_alias_cycles(aliases: dict, model_names: set[str]) -> None:
    """Reject exact alias-only cycles that never resolve to a concrete model."""
    visiting: list[str] = []
    resolved: set[str] = set()

    def visit(alias: str) -> None:
        if alias in resolved:
            return
        if alias in visiting:
            cycle_start = visiting.index(alias)
            cycle = " -> ".join((*visiting[cycle_start:], alias))
            raise ModelDiscoveryError(f"Configuration contains an alias cycle: {cycle}")
        visiting.append(alias)
        target = aliases.get(alias)
        if isinstance(target, str) and target in aliases and target not in model_names:
            visit(target)
        visiting.pop()
        resolved.add(alias)

    for alias_name in aliases:
        visit(alias_name)


def validate_fallbacks(fallbacks: list, model_names: set, aliases: dict):
    """Validate that fallback sources and targets reference existing models or aliases."""
    valid_targets = model_names | set(aliases.keys())
    alias_keys = set(aliases.keys())
    valid = True
    for fallback_rule in fallbacks:
        for source, targets in fallback_rule.items():
            if source != "*" and source not in valid_targets and not _matches_known_model_aliases(source, model_names, alias_keys):
                valid = False
                logger.warning(
                    f"⚠️ Fallback source '{source}' is not a known model or alias"
                )
            for target in targets:
                if target not in valid_targets and not _matches_known_model_aliases(target, model_names, alias_keys):
                    valid = False
                    logger.warning(
                        f"⚠️ Fallback target '{target}' for '{source}' "
                        f"is not a known model or alias"
                    )
    return valid


def validate_public_model_hub(public_model_hub: list, model_names: set, aliases: dict):
    """Validate that public model hub entries reference existing models or aliases.

    Removes duplicates in-place.
    """
    valid_targets = model_names | set(aliases.keys())
    alias_keys = set(aliases.keys())
    seen = set()
    i = 0
    valid = True

    while i < len(public_model_hub):
        entry = public_model_hub[i]
        if entry in seen:
            public_model_hub.pop(i)
            continue
        seen.add(entry)

        if entry not in valid_targets and not _matches_known_model_aliases(entry, model_names, alias_keys):
            valid = False
            logger.warning(
                f"⚠️ Public model hub entry '{entry}' is not a known model or alias"
            )
        i += 1
    return valid



_litellm_prices_cache = None

LITELLM_PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/refs/heads/main/model_prices_and_context_window.json"


def _get_litellm_prices() -> dict:
    """Fetch and cache LiteLLM model pricing data from GitHub."""
    global _litellm_prices_cache
    if _litellm_prices_cache is not None:
        return _litellm_prices_cache

    try:
        _litellm_prices_cache = request_json(LITELLM_PRICES_URL, timeout=30)
    except urllib.error.HTTPError as e:
        logger.warning(
            f"Failed to fetch LiteLLM pricing data from {LITELLM_PRICES_URL}: "
            f"{format_http_error(e)}"
        )
        _litellm_prices_cache = {}
    except Exception as e:
        logger.warning(f"Failed to fetch LiteLLM pricing data: {e}")
        _litellm_prices_cache = {}

    return _litellm_prices_cache


def validate_prices(models: list):
    """Validate that each model's base_model exists in LiteLLM pricing data."""
    if not models:
        return
    prices = _get_litellm_prices()
    if not prices:
        logger.warning("⚠️ Skipping price validation (no pricing data available)")
        return

    missing = []
    for model in models:
        base_model = model.get("model_info", {}).get("base_model", "")
        if base_model and base_model not in prices:
            missing.append(base_model)

    if missing:
        unique_missing = sorted(set(missing))
        logger.warning(
            f"⚠️ {len(unique_missing)} base_model(s) not found in LiteLLM pricing: "
            f"{unique_missing}"
        )
