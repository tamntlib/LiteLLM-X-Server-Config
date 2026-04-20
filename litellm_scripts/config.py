#!/usr/bin/env python3
"""
Unified LiteLLM management script for credentials, models, aliases, fallbacks, and public model hub.

Configuration:
    - config.json: Base configuration (providers, models, aliases, fallbacks, public_model_hub)
    - config.local.json: Local overrides (extends/overrides config.json)
      Include api_key in provider config for credentials:
      {
        "providers": {
          "my-provider": {
            "api_key": "sk-..."
          }
        }
      }

Usage:
    python3 config.py --only credentials,models,aliases,fallbacks,public_model_hub --force --prune
"""

import asyncio
import urllib.request
import urllib.error
import json
import os
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from load_dotenv import load_dotenv

from gen_config import (
    generate_config,
    validate_aliases,
    validate_fallbacks,
    validate_public_model_hub,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

load_dotenv()

LITELLM_API_KEY = os.environ["LITELLM_API_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]

DEFAULT_CONFIG_FILE = "config.json"

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


def get_actor_from_key():
    url = f"{LITELLM_BASE_URL}/key/info"
    headers = {"Authorization": "Bearer " + LITELLM_API_KEY}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode())
            return (
                data.get("info", {}).get("user_id")
                or data.get("info", {}).get("team_id")
                or LITELLM_API_KEY[:20]
            )
    except Exception as e:
        logger.warning(f"Failed to get actor from key: {e}")
        return LITELLM_API_KEY[:20]


# ============================================================================
# HTTP Request Functions
# ============================================================================


def get_request(endpoint):
    url = f"{LITELLM_BASE_URL}/{endpoint}"
    headers = {"Authorization": "Bearer " + LITELLM_API_KEY}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            return True, json.loads(res.read().decode())
    except Exception as e:
        return False, str(e)


def post_request(endpoint, data):
    url = f"{LITELLM_BASE_URL}/{endpoint}"
    headers = {
        "Authorization": "Bearer " + LITELLM_API_KEY,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            return True, res.read().decode()
    except Exception as e:
        return False, str(e)


def delete_request(endpoint):
    url = f"{LITELLM_BASE_URL}/{endpoint}"
    headers = {"Authorization": "Bearer " + LITELLM_API_KEY}
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req) as res:
            return True, res.read().decode()
    except Exception as e:
        return False, str(e)


# ============================================================================
# Credential Management
# ============================================================================


def get_all_credentials():
    success, result = get_request("credentials")
    if not success:
        return []
    # Handle both dict with "credentials" key and list response formats
    creds = result.get("credentials", result) if isinstance(result, dict) else result
    if not isinstance(creds, list):
        return []
    return [
        cred.get("credential_name")
        for cred in creds
        if isinstance(cred, dict) and cred.get("credential_name")
    ]


def credential_exists(credential_name):
    success, result = get_request("credentials")
    if not success:
        return False
    # Handle both dict with "credentials" key and list response formats
    creds = result.get("credentials", result) if isinstance(result, dict) else result
    if not isinstance(creds, list):
        return False
    for cred in creds:
        if isinstance(cred, dict) and cred.get("credential_name") == credential_name:
            return True
    return False


def delete_credential(credential_name):
    return delete_request(f"credentials/{credential_name}")


def create_credential(service_name, provider, api_key, api_base, force=False):
    provider_cfg = PROVIDER_CONFIG[provider]
    path_suffix = provider_cfg["path_suffix"]
    credential_name = f"{service_name}-{provider}"

    if credential_exists(credential_name):
        if force:
            delete_credential(credential_name)
            action = "replaced"
        else:
            logger.info(f"Skipped credential: {credential_name}")
            return True, "skipped", "skipped"
    else:
        action = "created"

    success, result = post_request(
        "credentials",
        {
            "credential_name": credential_name,
            "credential_values": {
                "api_key": api_key,
                "api_base": f"{api_base}{path_suffix}",
            },
            "credential_info": {
                "custom_llm_provider": provider_cfg["custom_llm_provider"]
            },
        },
    )
    return success, result, action


async def create_credential_async(
    executor, service_name, provider, api_key, api_base, force=False
):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        executor, create_credential, service_name, provider, api_key, api_base, force
    )
    credential_name = f"{service_name}-{provider}"
    if len(result) == 2:
        success, msg = result
        return success, msg
    success, msg, action = result
    if success:
        if action == "replaced":
            logger.info(f"Replaced credential: {credential_name}")
        elif action == "created":
            logger.info(f"Created credential: {credential_name}")
    else:
        logger.error(f"Failed to create credential: {credential_name} - {msg}")
    return success, msg


# ============================================================================
# Model Management
# ============================================================================


def get_all_models():
    """Get all models from /v2/model/info endpoint with full details."""
    success, result = get_request("v2/model/info?include_team_models=true")
    if not success:
        return []
    return result.get("data", [])


def delete_model_by_id(model_id):
    """Delete a model by its ID directly."""
    return post_request("model/delete", {"id": model_id})


def _create_model(payload, force, actor, existing_models_cache):
    """Create or replace a single model from a pre-built payload.

    Args:
        payload: Dict with model_name, litellm_params, model_info (from gen_config)
        force: Whether to replace existing models
        actor: Actor identifier for audit fields
        existing_models_cache: Dict of (model_name, credential_name) -> [raw model objects]

    Returns:
        (success, action, duplicates_deleted)
    """
    full_model_name = payload["model_name"]
    credential_name = payload["litellm_params"]["litellm_credential_name"]

    # Check if model exists using cached models (could have multiple duplicates)
    model_key = (full_model_name, credential_name)
    existing_models = existing_models_cache.get(model_key, [])
    duplicates_deleted = 0

    if existing_models:
        if force:
            for model in existing_models:
                delete_model_by_id(model["model_info"]["id"])
            duplicates_deleted = len(existing_models) - 1
            action = "replaced"
        else:
            logger.info(f"Skipped model: {full_model_name} ({credential_name})")
            return True, None, 0
    else:
        action = "created"

    now_iso_string = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    # Add audit fields to model_info
    model_info = dict(payload.get("model_info", {}))
    if existing_models:
        existing_model_info = existing_models[0]["model_info"]
        if existing_model_info.get("created_at"):
            model_info["created_at"] = existing_model_info["created_at"]
        if existing_model_info.get("created_by"):
            model_info["created_by"] = existing_model_info["created_by"]
    model_info.update(
        {
            "updated_at": now_iso_string,
            "updated_by": actor,
        }
    )
    model_info.setdefault("created_at", now_iso_string)
    model_info.setdefault("created_by", actor)

    request_body = {
        "model_name": full_model_name,
        "litellm_params": payload["litellm_params"],
        "model_info": model_info,
    }

    success, result = post_request("model/new", request_body)

    if success:
        if action == "replaced":
            logger.info(f"Replaced model: {full_model_name} ({credential_name})")
        else:
            logger.info(f"Created model: {full_model_name} ({credential_name})")
    else:
        logger.error(
            f"Failed to create model: {full_model_name} ({credential_name}) - {result}"
        )

    return success, action, duplicates_deleted


async def _sync_single_model(executor, payload, force, actor, existing_models_cache):
    """Async wrapper for _create_model."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        executor,
        lambda: _create_model(payload, force, actor, existing_models_cache),
    )


# ============================================================================
# Router Settings Management
# ============================================================================


def get_router_settings():
    """Get current router settings from /router/settings endpoint."""
    success, result = get_request("router/settings")
    if not success:
        return {}
    return result.get("current_values", {})


def update_router_settings(updates: dict):
    """
    Update router settings while preserving existing values.
    Fetches current settings, merges with updates, and posts to config/update.
    """
    current = get_router_settings()

    # Start with current settings, then apply updates
    router_settings = dict(current)
    router_settings.update(updates)

    payload = {"router_settings": router_settings}
    return post_request("config/update", payload)


# ============================================================================
# Aliases Management
# ============================================================================


def get_current_aliases():
    settings = get_router_settings()
    return settings.get("model_group_alias", {})


def update_aliases(aliases: dict, force=False):
    if not aliases:
        logger.info("No aliases to update")
        return True, "no aliases"

    current_aliases = get_current_aliases()

    if not force and current_aliases == aliases:
        logger.info("Aliases already up-to-date, skipping")
        return True, "skipped"

    success, result = update_router_settings({"model_group_alias": aliases})

    if success:
        logger.info(f"✅ Updated {len(aliases)} model group aliases")
        # Validate aliases point to existing models or other aliases
        existing_models = {model["model_name"] for model in get_all_models()}
        validate_aliases(aliases, existing_models)
    else:
        logger.error(f"❌ Failed to update aliases: {result}")

    return success, result


# ============================================================================
# Fallbacks Management
# ============================================================================


def get_current_fallbacks():
    settings = get_router_settings()
    return settings.get("fallbacks", [])


def update_fallbacks(fallbacks: list, force=False):
    if not fallbacks:
        logger.info("No fallbacks to update")
        return True, "no fallbacks"

    current_fallbacks = get_current_fallbacks()

    if not force and current_fallbacks == fallbacks:
        logger.info("Fallbacks already up-to-date, skipping")
        return True, "skipped"

    success, result = update_router_settings({"fallbacks": fallbacks})

    if success:
        logger.info(f"✅ Updated {len(fallbacks)} fallback rules")
        # Validate fallbacks reference existing models or aliases
        existing_models = {model["model_name"] for model in get_all_models()}
        current_aliases = get_current_aliases()
        validate_fallbacks(fallbacks, existing_models, current_aliases)
    else:
        logger.error(f"❌ Failed to update fallbacks: {result}")

    return success, result


# ============================================================================
# Public Model Hub Management
# ============================================================================


def _normalize_public_model_hub(model_groups: list) -> list:
    normalized = []
    seen = set()
    for model_group in model_groups:
        if model_group not in seen:
            normalized.append(model_group)
            seen.add(model_group)
    return normalized


def get_current_public_model_hub():
    success, result = get_request("public/model_hub")
    if not success or not isinstance(result, list):
        return []

    model_groups = []
    for item in result:
        if isinstance(item, dict) and item.get("model_group"):
            model_groups.append(item["model_group"])
    return _normalize_public_model_hub(model_groups)


def update_public_model_hub(model_groups: list, force=False):
    desired_model_groups = _normalize_public_model_hub(model_groups)
    current_model_groups = get_current_public_model_hub()

    if not force and current_model_groups == desired_model_groups:
        logger.info("Public model hub already up-to-date, skipping")
        return True, "skipped"

    success, result = post_request(
        "model_group/make_public", {"model_groups": desired_model_groups}
    )

    if success:
        logger.info(
            f"✅ Updated public model hub with {len(desired_model_groups)} entries"
        )
        existing_models = {model["model_name"] for model in get_all_models()}
        current_aliases = get_current_aliases()
        validate_public_model_hub(
            desired_model_groups, existing_models, current_aliases
        )
    else:
        logger.error(f"❌ Failed to update public model hub: {result}")

    return success, result


# ============================================================================
# Main Sync Functions
# ============================================================================


async def sync_credentials(config: dict, force=False, prune=False):
    logger.info("=" * 60)
    logger.info("Syncing credentials...")
    logger.info("=" * 60)

    expected_credentials = set()
    credentials = config.get("credentials", [])

    with ThreadPoolExecutor(max_workers=10) as executor:
        tasks = []

        for cred in credentials:
            service_name = cred["service_name"]
            provider = cred["provider"]
            api_key = cred["api_key"]
            api_base = cred["api_base"]

            if provider not in PROVIDER_CONFIG:
                logger.warning(f"Unknown provider: {provider}, skipping")
                continue

            expected_credentials.add(f"{service_name}-{provider}")
            tasks.append(
                create_credential_async(
                    executor, service_name, provider, api_key, api_base, force
                )
            )

        await asyncio.gather(*tasks)

    if prune:
        logger.info("Pruning unused credentials...")
        existing_credentials = get_all_credentials()
        for cred_name in existing_credentials:
            if cred_name not in expected_credentials:
                logger.info(f"Pruning credential: {cred_name}")
                success, result = delete_credential(cred_name)
                if success:
                    logger.info(f"Deleted credential: {cred_name}")
                else:
                    logger.error(f"Failed to delete credential: {cred_name} - {result}")


async def sync_models(config: dict, force=False, prune=False):
    logger.info("=" * 60)
    logger.info("Syncing models...")
    logger.info("=" * 60)

    actor = get_actor_from_key()
    logger.info(f"Actor: {actor}")

    expected_models = set()
    model_payloads = config.get("models", [])

    created_count = 0
    replaced_count = 0
    deleted_count = 0
    failed_count = 0

    # Cache existing models once before processing (store matching raw model objects)
    existing_models_cache = {}
    all_models = get_all_models()
    for model in all_models:
        key = (
            model["model_name"],
            model["litellm_params"]["litellm_credential_name"],
        )
        if key not in existing_models_cache:
            existing_models_cache[key] = []
        existing_models_cache[key].append(model)

    # Count total unique model groups and warn about duplicates
    total_models = sum(len(ids) for ids in existing_models_cache.values())
    duplicates = sum(1 for ids in existing_models_cache.values() if len(ids) > 1)
    if duplicates > 0:
        logger.warning(
            f"Found {duplicates} duplicate model groups (will be cleaned up)"
        )
    logger.info(
        f"Found {total_models} existing models ({len(existing_models_cache)} unique)"
    )

    with ThreadPoolExecutor(max_workers=10) as executor:
        tasks = []

        for payload in model_payloads:
            full_model_name = payload["model_name"]
            credential_name = payload["litellm_params"]["litellm_credential_name"]
            expected_models.add((full_model_name, credential_name))

            tasks.append(
                _sync_single_model(
                    executor, payload, force, actor, existing_models_cache
                )
            )

        results = await asyncio.gather(*tasks)
        for success, action, duplicates_deleted in results:
            if success and action == "created":
                created_count += 1
            elif success and action == "replaced":
                replaced_count += 1
                deleted_count += duplicates_deleted
            elif not success:
                failed_count += 1

    if prune:
        logger.info("Pruning unused models...")
        existing_models = get_all_models()
        for model in existing_models:
            model_name = model["model_name"]
            credential_name = model["litellm_params"]["litellm_credential_name"]
            model_id = model["model_info"]["id"]
            if (model_name, credential_name) not in expected_models:
                logger.info(f"Pruning model: {model_name} ({credential_name})")
                success, result = post_request("model/delete", {"id": model_id})
                if success:
                    logger.info(f"Deleted model: {model_name} ({credential_name})")
                    deleted_count += 1
                else:
                    logger.error(
                        f"Failed to delete model: {model_name} ({credential_name}) - {result}"
                    )
                    failed_count += 1

    total_ops = created_count + replaced_count + deleted_count + failed_count
    if failed_count == 0:
        icon = "✅"
    elif failed_count == total_ops:
        icon = "❌"
    else:
        icon = "⚠️"
    logger.info(
        f"{icon} Models: Created {created_count}, Replaced {replaced_count}, Deleted {deleted_count}, Failed {failed_count}"
    )


def sync_aliases(config: dict, force=False):
    logger.info("=" * 60)
    logger.info("Syncing aliases...")
    logger.info("=" * 60)

    aliases = config.get("aliases", {})
    update_aliases(aliases, force)


def sync_fallbacks(config: dict, force=False):
    logger.info("=" * 60)
    logger.info("Syncing fallbacks...")
    logger.info("=" * 60)

    fallbacks = config.get("fallbacks", [])
    update_fallbacks(fallbacks, force)


def sync_public_model_hub(config: dict, force=False):
    logger.info("=" * 60)
    logger.info("Syncing public model hub...")
    logger.info("=" * 60)

    public_model_hub = config.get("public_model_hub", [])
    update_public_model_hub(public_model_hub, force)


# ============================================================================
# Main Entry Point
# ============================================================================


async def main():
    parser = argparse.ArgumentParser(description="Unified LiteLLM management script")
    parser.add_argument(
        "--only",
        type=str,
        default="credentials,models,aliases,fallbacks,public_model_hub",
        help="Comma-separated list of components to sync (credentials,models,aliases,fallbacks,public_model_hub)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force update existing resources",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete resources not in config (only for credentials and models)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help="Path to the config file (default: config.json in script dir)",
    )

    args = parser.parse_args()

    components = [c.strip() for c in args.only.split(",")]
    valid_components = {
        "credentials",
        "models",
        "aliases",
        "fallbacks",
        "public_model_hub",
    }
    invalid = set(components) - valid_components
    if invalid:
        logger.error(f"Invalid components: {invalid}. Valid: {valid_components}")
        return

    logger.info(f"Components to sync: {components}")
    logger.info(f"Force: {args.force}, Prune: {args.prune}")

    if args.dry_run:
        logger.info("DRY RUN: No changes will be made")
        return

    config_file = args.config

    logger.info(f"Using config file: {config_file}")

    if not config_file.exists():
        logger.error(f"Config file not found: {config_file}")
        return

    config = generate_config(config_file)

    if "credentials" in components:
        await sync_credentials(config, args.force, args.prune)

    if "models" in components:
        await sync_models(config, args.force, args.prune)

    if "aliases" in components:
        sync_aliases(config, args.force)

    if "fallbacks" in components:
        sync_fallbacks(config, args.force)

    if "public_model_hub" in components:
        sync_public_model_hub(config, args.force)

    logger.info("=" * 60)
    logger.info("✅ Sync complete!")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
