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
import argparse
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


def resolve_provider_models(providers: dict) -> list:
    """Resolve providers into a flat list of LiteLLM model request bodies.

    For each provider, for each interface, for each model:
    1. Use interface-specific `models` from providers.<provider>.interfaces.<interface>.models
    2. Resolve access_groups (model-level overrides provider-level)
    3. Build the full model_name, litellm_params, and model_info

    Returns a list of dicts ready to POST to LiteLLM's /model/new endpoint.
    """
    models = []

    for service_name, provider_config in providers.items():
        provider_access_groups = provider_config.get("access_groups")
        api_key = provider_config.get("api_key")

        if not api_key:
            continue

        interfaces = provider_config.get("interfaces", {})

        for provider, iface_config in interfaces.items():
            iface = iface_config if iface_config else {}
            iface_models = iface.get("models", {})

            credential_name = f"{service_name}-{provider}"

            for litellm_model_name, model_cfg in iface_models.items():
                if isinstance(model_cfg, dict):
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
                base_model = base_model or model_name

                # Resolve access_groups: model-level > provider-level
                resolved_access_groups = (
                    access_groups
                    if access_groups is not None
                    else provider_access_groups
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
    models = resolve_provider_models(providers)

    # Resolve $base references in fallbacks
    fallbacks = resolve_fallback_base_refs(
        config.get("fallbacks", []),
        base_config.get("fallbacks", []),
    )

    return {
        "credentials": credentials,
        "models": models,
        "aliases": config.get("aliases", {}),
        "fallbacks": fallbacks,
    }


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
