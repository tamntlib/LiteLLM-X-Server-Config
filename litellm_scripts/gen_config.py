#!/usr/bin/env python3
"""
Generate a resolved config file (config.gen.json) from config.json and config.local.json.

This script handles:
    - Loading and deep-merging base config with local overrides
    - Resolving $extend directives in provider configs

Usage:
    python3 gen_config.py
    python3 gen_config.py --config config.json --output config.gen.json
"""

import json
import logging
import urllib.request
import urllib.error
import argparse
import re
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_OUTPUT_FILE = "config.gen.json"


# ============================================================================
# Utility Functions
# ============================================================================


def load_json(file_path):
    with open(file_path) as f:
        return json.load(f)


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries. Override values take precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
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


def resolve_fallback_base_refs(fallbacks: list, base_fallbacks: list) -> list:
    """Resolve $base references in fallback lists.

    In local config, a fallback entry can use "$base" to reference the base
    config's fallback values for the same model key:

        {"claude-opus-4-6": ["$base", "anthropic/glm-4.7"]}

    If the base config has:
        {"claude-opus-4-6": ["anthropic/gemini-3.1-pro-preview"]}

    The result will be:
        {"claude-opus-4-6": ["anthropic/gemini-3.1-pro-preview", "anthropic/glm-4.7"]}
    """
    # Build a lookup from base fallbacks: model_key -> fallback list
    base_lookup = {}
    for entry in base_fallbacks:
        for key, values in entry.items():
            base_lookup[key] = values

    resolved = []
    for entry in fallbacks:
        resolved_entry = {}
        for key, values in entry.items():
            if "$base" in values:
                base_values = base_lookup.get(key, [])
                resolved_entry[key] = [
                    item
                    for v in values
                    for item in (base_values if v == "$base" else [v])
                ]
            else:
                resolved_entry[key] = values
        resolved.append(resolved_entry)
    return resolved


def resolve_provider_extensions(providers: dict) -> dict:
    """Resolve $extend directives in provider configs.

    Example:
        "provider-2": {
            "$extend": "provider-1",
            "access_groups": [],
            "api_base": "http://different:8045"
        }

    This will copy all config from provider-1 and override with provider-2's values.
    Set "$extend": null in config.local.json to remove inheritance.
    """
    resolved = {}

    # First pass: add providers without $extend
    for name, config in providers.items():
        if not config.get("$extend"):
            resolved[name] = {k: v for k, v in config.items() if k != "$extend"}

    # Second pass: resolve providers with $extend
    for name, config in providers.items():
        if config.get("$extend"):
            base_name = config["$extend"]
            if base_name not in resolved:
                logger.error(
                    f"Provider '{name}' extends non-existent provider '{base_name}'"
                )
                continue
            base_config = resolved[base_name]
            merged = deep_merge(base_config, config)
            resolved[name] = {k: v for k, v in merged.items() if k != "$extend"}

    return resolved


def _fetch_openai_models(api_base: str, api_key: str) -> list[str]:
    """Fetch models using OpenAI-compatible /v1/models endpoint (Bearer auth)."""
    url = f"{api_base}/v1/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode())
    except Exception as e:
        logger.warning(f"Failed to fetch models from {url}: {e}")
        return []

    if isinstance(data, dict) and "data" in data:
        return [m["id"] for m in data["data"] if isinstance(m, dict) and m.get("id")]

    return []


def _fetch_gemini_models(api_base: str, api_key: str) -> list[str]:
    """Fetch models using Gemini /v1beta/models endpoint (Bearer auth)."""
    url = f"{api_base}/v1beta/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode())
    except Exception as e:
        logger.warning(f"Failed to fetch models from {url}: {e}")
        return []

    if isinstance(data, dict) and "models" in data:
        model_ids = []
        for m in data["models"]:
            if isinstance(m, dict) and m.get("name"):
                name = m["name"]
                if name.startswith("models/"):
                    name = name[len("models/") :]
                model_ids.append(name)
        return model_ids

    return []


def fetch_models_from_api(api_base: str, api_key: str, provider: str) -> list[str]:
    """Fetch available model IDs from a provider's /models endpoint.

    Tries the provider-specific endpoint first, then falls back to
    OpenAI-compatible /v1/models for anthropic and gemini interfaces.

    Returns a list of model ID strings.
    """
    if provider == "openai":
        return _fetch_openai_models(api_base, api_key)

    if provider == "anthropic":
        models = _fetch_anthropic_models(api_base, api_key)
        if not models:
            logger.info(f"Falling back to OpenAI-compatible endpoint for {api_base}")
            models = _fetch_openai_models(api_base, api_key)
        return models

    if provider == "gemini":
        models = _fetch_gemini_models(api_base, api_key)
        if not models:
            logger.info(f"Falling back to OpenAI-compatible endpoint for {api_base}")
            models = _fetch_openai_models(api_base, api_key)
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
    base_url = f"{api_base}/v1/models"
    url = base_url

    while True:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                data = json.loads(res.read().decode())
        except Exception as e:
            logger.warning(f"Failed to fetch models from {url}: {e}")
            break

        if isinstance(data, dict) and "data" in data:
            for m in data["data"]:
                if isinstance(m, dict) and m.get("id"):
                    model_ids.append(m["id"])

        # Handle pagination
        if data.get("has_more") and data.get("last_id"):
            separator = "&" if "?" in base_url else "?"
            url = f"{base_url}{separator}after_id={data['last_id']}"
        else:
            break

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
            natural_sort_key(payload["litellm_params"]["litellm_credential_name"]),
            natural_sort_key(payload["model_name"]),
        ),
        reverse=True,
    )


def resolve_provider_models(providers: dict, base_model_map: dict = None) -> list:
    """Resolve providers into a flat list of LiteLLM model request bodies.

    For each provider, for each interface, for each model:
    1. Use interface-specific `models` from providers.<provider>.interfaces.<interface>.models
    2. If `models` is empty, auto-fetch from the provider's /models API endpoint
    3. Resolve base_model: explicit > model_name_base_model_map > model_name
    4. Resolve access_groups (model-level overrides provider-level)
    5. Build the full model_name, litellm_params, and model_info

    Args:
        providers: Resolved provider configurations
        base_model_map: Global model_name -> base_model mapping (fallback)

    Returns a list of dicts ready to POST to LiteLLM's /model/new endpoint.
    """
    models = []
    base_model_map = base_model_map or {}

    for service_name, provider_config in providers.items():
        provider_access_groups = provider_config.get("access_groups")
        api_key = provider_config.get("api_key")
        api_base = provider_config.get("api_base", "")
        models_api_base = provider_config.get("models_api_base") or api_base

        if not api_key:
            continue

        interfaces = provider_config.get("interfaces", {})

        for provider, iface_config in interfaces.items():
            iface = iface_config if iface_config else {}
            iface_models = iface.get("models", {})
            autofill_disabled = iface.get("models_autofill_disabled", False)

            # Auto-discover models from API unless autofill is disabled
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
                    logger.warning(
                        f"No models discovered for {service_name}/{provider}"
                    )

            credential_name = f"{service_name}-{provider}"

            for litellm_model_name, model_cfg in iface_models.items():
                if isinstance(model_cfg, dict):
                    if model_cfg.get("ignored"):
                        continue
                    model_group_name = model_cfg.get("model_name")
                    model_info_cfg = model_cfg.get("model_info", {})
                    base_model = model_info_cfg.get("base_model")
                    litellm_params_cfg = model_cfg.get("litellm_params", {})
                    access_groups = model_cfg.get("access_groups")
                else:
                    model_group_name = None
                    model_info_cfg = {}
                    base_model = None
                    litellm_params_cfg = {}
                    access_groups = None

                model_name = model_group_name or litellm_model_name
                # Resolve base_model: explicit > map lookup > model_name
                base_model = base_model or base_model_map.get(model_name) or model_name

                # Resolve access_groups: model-level > model_info-level > provider-level
                resolved_access_groups = (
                    access_groups
                    if access_groups is not None
                    else model_info_cfg.get("access_groups", provider_access_groups)
                )

                # Build model_info
                model_info = dict(model_info_cfg)
                if base_model:
                    model_info["base_model"] = base_model
                if resolved_access_groups:
                    model_info["access_groups"] = resolved_access_groups

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

    return models


def generate_config(config_path: Path) -> dict:
    """Load config, merge local overrides, and resolve into deployment-ready format.

    Resolves:
    1. Local config overrides (config.local.json)
    2. Provider $extend directives
    3. Providers into a flat `models` array of LiteLLM request bodies

    Returns dict with:
    - models: list of LiteLLM /model/new request bodies
    - credentials: list of credential definitions
    - aliases: model alias mappings
    - fallbacks: fallback rules
    """
    config, base_config = load_config_with_local(config_path)

    providers = resolve_provider_extensions(config.get("providers", {}))

    # Build credentials list
    credentials = []
    for service_name, provider_config in providers.items():
        api_base = provider_config.get("api_base", "")
        api_key = provider_config.get("api_key")
        if not api_key:
            continue
        for provider in provider_config.get("interfaces", {}).keys():
            credentials.append(
                {
                    "service_name": service_name,
                    "provider": provider,
                    "api_key": api_key,
                    "api_base": api_base,
                }
            )

    # Build flat models array
    base_model_map = config.get("model_name_base_model_map", {})
    models = sort_model_payloads(resolve_provider_models(providers, base_model_map))

    # Resolve $base references in fallbacks
    fallbacks = resolve_fallback_base_refs(
        config.get("fallbacks", []),
        base_config.get("fallbacks", []),
    )

    aliases = config.get("aliases", {})

    # Validate aliases and fallbacks against known models
    model_names = {m["model_name"] for m in models}
    validate_aliases(aliases, model_names)
    validate_fallbacks(fallbacks, model_names, aliases)
    validate_prices(models)

    return {
        "credentials": credentials,
        "models": models,
        "aliases": aliases,
        "fallbacks": fallbacks,
    }


def validate_aliases(aliases: dict, model_names: set):
    """Validate that alias targets point to existing models or other aliases."""
    valid_targets = model_names | set(aliases.keys())
    for alias_name, target in aliases.items():
        if target not in valid_targets:
            logger.warning(
                f"⚠️ Alias '{alias_name}' points to non-existent model: {target}"
            )


def validate_fallbacks(fallbacks: list, model_names: set, aliases: dict):
    """Validate that fallback sources and targets reference existing models or aliases."""
    valid_targets = model_names | set(aliases.keys())
    for fallback_rule in fallbacks:
        for source, targets in fallback_rule.items():
            if source not in valid_targets:
                logger.warning(
                    f"⚠️ Fallback source '{source}' is not a known model or alias"
                )
            for target in targets:
                if target not in valid_targets:
                    logger.warning(
                        f"⚠️ Fallback target '{target}' for '{source}' "
                        f"is not a known model or alias"
                    )


_litellm_prices_cache = None

LITELLM_PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/refs/heads/main/model_prices_and_context_window.json"


def _get_litellm_prices() -> dict:
    """Fetch and cache LiteLLM model pricing data from GitHub."""
    global _litellm_prices_cache
    if _litellm_prices_cache is not None:
        return _litellm_prices_cache

    try:
        req = urllib.request.Request(LITELLM_PRICES_URL)
        with urllib.request.urlopen(req, timeout=30) as res:
            _litellm_prices_cache = json.loads(res.read().decode())
    except Exception as e:
        logger.warning(f"Failed to fetch LiteLLM pricing data: {e}")
        _litellm_prices_cache = {}

    return _litellm_prices_cache


def validate_prices(models: list):
    """Validate that each model's base_model exists in LiteLLM pricing data."""
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


# ============================================================================
# Main Entry Point
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Generate resolved config from config.json and config.local.json"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help="Path to the base config file (default: config.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help="Path to the output file (default: config.gen.json)",
    )

    args = parser.parse_args()

    if not args.config.exists():
        logger.error(f"Config file not found: {args.config}")
        return

    logger.info(f"Generating config from: {args.config}")
    config = generate_config(args.config)

    with open(args.output, "w") as f:
        json.dump(config, f, indent=4)
        f.write("\n")

    logger.info(f"Generated config written to: {args.output}")


if __name__ == "__main__":
    main()
