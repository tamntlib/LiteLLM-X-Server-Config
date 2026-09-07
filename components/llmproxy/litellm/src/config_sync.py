#!/usr/bin/env python3
"""
Unified LiteLLM management script for credentials, models, aliases, fallbacks,
public model hub, router settings, and guardrails.

Configuration:
    - config.json: Base configuration (providers, models, aliases, fallbacks,
      public_model_hub, router_settings, guardrails)
    - config.local.json: Local overrides (extends/overrides config.json)
      Include api_key in provider config for credentials:
      {
        "providers": {
          "my-provider": {
            "api_key": "sk-..."
          }
        }
      }

"""

import urllib.request
import urllib.error
import urllib.parse
import hashlib
import hmac
import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llmproxy.core.http import DEFAULT_HTTP_TIMEOUT, build_request, format_http_error, request_json
from llmproxy.core.env import load_dotenv
from llmproxy.core.command import CommandError
from .config_generate import (
    ModelDiscoveryError,
    generate_config,
    generate_config_for_preset,
    router_fallback_references,
    validate_aliases,
    validate_fallbacks,
    validate_public_model_hub,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

def _get_api_key() -> str:
    return os.environ.get("LITELLM_API_KEY", "")

def _get_base_url() -> str:
    return os.environ.get("LITELLM_BASE_URL", "").rstrip("/")

LITELLM_API_KEY = _get_api_key()
LITELLM_BASE_URL = _get_base_url()


# ============================================================================
# Utility Functions
# ============================================================================


def _encode_endpoint_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CommandError(f"Invalid {label}")
    # urllib deliberately leaves RFC-unreserved dots unchanged. Encode them too
    # so '.' and '..' can never be interpreted as path traversal segments.
    return urllib.parse.quote(value, safe="").replace(".", "%2E")


def get_actor_from_key():
    url = f"{_get_base_url()}/key/info"
    headers = {"Authorization": "Bearer " + _get_api_key()}
    try:
        data = request_json(url, headers=headers)
        return (
            data.get("info", {}).get("user_id")
            or data.get("info", {}).get("team_id")
            or "unknown"
        )
    except urllib.error.HTTPError as e:
        logger.warning(f"Failed to get actor from key: {format_http_error(e)}")
        return "unknown"
    except Exception as e:
        logger.warning(f"Failed to get actor from key: {e}")
        return "unknown"


# ============================================================================
# HTTP Request Functions
# ============================================================================


def get_request(endpoint):
    url = f"{_get_base_url()}/{endpoint}"
    headers = {"Authorization": "Bearer " + _get_api_key()}
    try:
        return True, request_json(url, headers=headers)
    except urllib.error.HTTPError as e:
        return False, format_http_error(e)
    except Exception as e:
        return False, str(e)


def post_request(endpoint, data):
    url = f"{_get_base_url()}/{endpoint}"
    headers = {
        "Authorization": "Bearer " + _get_api_key(),
        "Content-Type": "application/json",
    }
    req = build_request(url, data=json.dumps(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT) as res:
            return True, res.read().decode()
    except urllib.error.HTTPError as e:
        return False, format_http_error(e)
    except Exception as e:
        return False, str(e)


def put_request(endpoint, data):
    url = f"{_get_base_url()}/{endpoint}"
    headers = {
        "Authorization": "Bearer " + _get_api_key(),
        "Content-Type": "application/json",
    }
    req = build_request(
        url,
        data=json.dumps(data).encode(),
        headers=headers,
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT) as res:
            return True, res.read().decode()
    except urllib.error.HTTPError as e:
        return False, format_http_error(e)
    except Exception as e:
        return False, str(e)


def delete_request(endpoint):
    url = f"{_get_base_url()}/{endpoint}"
    headers = {"Authorization": "Bearer " + _get_api_key()}
    req = build_request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT) as res:
            return True, res.read().decode()
    except urllib.error.HTTPError as e:
        return False, format_http_error(e)
    except Exception as e:
        return False, str(e)


# ============================================================================
# Credential Management
# ============================================================================


def get_credential_inventory():
    success, result = get_request("credentials")
    if not success:
        raise CommandError(f"Failed to read credential inventory: {result}")
    # Handle both dict with "credentials" key and list response formats
    creds = result.get("credentials", result) if isinstance(result, dict) else result
    if not isinstance(creds, list):
        raise CommandError("Credential inventory returned an unexpected response")
    if any(
        not isinstance(cred, dict)
        or not isinstance(cred.get("credential_name"), str)
        or not cred["credential_name"]
        or (
            "credential_values" in cred
            and not isinstance(cred["credential_values"], dict)
        )
        or (
            "credential_info" in cred
            and not isinstance(cred["credential_info"], dict)
        )
        for cred in creds
    ):
        raise CommandError("Credential inventory contains an invalid entry")
    names = [cred["credential_name"] for cred in creds]
    if len(names) != len(set(names)):
        raise CommandError("Credential inventory contains a duplicate credential name")
    return creds


def get_all_credentials():
    return [cred["credential_name"] for cred in get_credential_inventory()]


def credential_exists(credential_name):
    return credential_name in get_all_credentials()


def delete_credential(credential_name):
    return delete_request(
        f"credentials/{_encode_endpoint_segment(credential_name, 'credential name')}"
    )


def patch_request(endpoint, data):
    url = f"{_get_base_url()}/{endpoint}"
    headers = {
        "Authorization": "Bearer " + _get_api_key(),
        "Content-Type": "application/json",
    }
    req = build_request(
        url,
        data=json.dumps(data).encode(),
        headers=headers,
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT) as res:
            return True, res.read().decode()
    except urllib.error.HTTPError as e:
        return False, format_http_error(e)
    except Exception as e:
        return False, str(e)


def update_credential(credential_name, request_body):
    return patch_request(
        f"credentials/{_encode_endpoint_segment(credential_name, 'credential name')}",
        request_body,
    )


_CREDENTIAL_FINGERPRINT_KEY = "_llmproxy_config_fingerprint"


def _credential_fingerprint(request_body: dict) -> str:
    material = json.dumps(
        {
            "credential_name": request_body["credential_name"],
            "credential_values": request_body["credential_values"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(
        _get_api_key().encode(),
        material,
        hashlib.sha256,
    ).hexdigest()


def _credential_payload_with_fingerprint(request_body: dict) -> dict:
    payload = dict(request_body)
    credential_info = dict(payload.get("credential_info", {}))
    credential_info[_CREDENTIAL_FINGERPRINT_KEY] = _credential_fingerprint(payload)
    payload["credential_info"] = credential_info
    return payload


def _credential_inventory_entry_is_verifiable(credential: dict) -> bool:
    """Require readable values plus the keyed marker derived from those values."""
    name = credential.get("credential_name")
    values = credential.get("credential_values")
    info = credential.get("credential_info")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(values, dict)
        or not isinstance(info, dict)
    ):
        return False
    fingerprint = info.get(_CREDENTIAL_FINGERPRINT_KEY)
    if not isinstance(fingerprint, str) or not fingerprint:
        return False
    return hmac.compare_digest(
        fingerprint,
        _credential_fingerprint(
            {
                "credential_name": name,
                "credential_values": values,
            }
        ),
    )


def create_credential(request_body, force=False, *, exists: bool | None = None):
    credential_name = request_body["credential_name"]
    request_body = _credential_payload_with_fingerprint(request_body)

    exists_now = credential_exists(credential_name) if exists is None else exists
    if exists_now:
        if force:
            success, result = update_credential(credential_name, request_body)
            return success, result, "updated"
        logger.info(f"Skipped credential: {credential_name}")
        return True, "skipped", "skipped"

    success, result = post_request("credentials", request_body)
    return success, result, "created"


# ============================================================================
# Model Management
# ============================================================================


def get_all_models():
    """Read every model page; reject incomplete or inconsistent inventories."""
    endpoint = "v2/model/info?include_team_models=true"
    models = []
    page = 1
    pagination = None
    metadata_keys = {"total_count", "current_page", "total_pages", "size"}
    while True:
        success, result = get_request(endpoint)
        if not success:
            raise CommandError(f"Failed to read model inventory: {result}")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise CommandError("Model inventory returned an unexpected response")
        present = metadata_keys & result.keys()
        if not present and page == 1:
            # Older LiteLLM versions returned a single unpaginated data envelope.
            models = result["data"]
            break
        if present != metadata_keys or any(type(result[key]) is not int for key in metadata_keys):
            raise CommandError("Model inventory returned invalid pagination metadata")
        total, pages, size = result["total_count"], result["total_pages"], result["size"]
        if (
            total < 0 or size < 1 or result["current_page"] != page
            or pages != (total + size - 1) // size
            or (pagination is not None and pagination != (total, pages, size))
        ):
            raise CommandError("Model inventory pagination changed or is inconsistent")
        pagination = (total, pages, size)
        if len(result["data"]) != min(size, max(0, total - (page - 1) * size)):
            raise CommandError("Model inventory page is incomplete")
        models.extend(result["data"])
        if page >= pages:
            if len(models) != total:
                raise CommandError("Model inventory count does not match pagination total")
            break
        page += 1
        endpoint = f"v2/model/info?include_team_models=true&page={page}&size={size}"
    if any(
        not isinstance(model, dict)
        or not isinstance(model.get("model_name"), str)
        or not model.get("model_name")
        or not isinstance(model.get("litellm_params", {}), dict)
        or not isinstance(model.get("model_info"), dict)
        or not isinstance(model["model_info"].get("id"), str)
        or not model["model_info"].get("id")
        or (
            "litellm_credential_name" in model.get("litellm_params", {})
            and not isinstance(
                model["litellm_params"]["litellm_credential_name"], str
            )
        )
        for model in models
    ):
        raise CommandError("Model inventory contains an invalid entry")
    model_ids = [model["model_info"]["id"] for model in models]
    if len(model_ids) != len(set(model_ids)):
        raise CommandError("Model inventory contains a duplicate model ID")
    return models


def delete_model_by_id(model_id):
    """Delete a model by its ID directly."""
    return post_request("model/delete", {"id": model_id})


def _model_credential_name(model: dict) -> str:
    return model.get("litellm_params", {}).get("litellm_credential_name", "")


def _models_by_id(models: list[dict]) -> dict[str, dict]:
    return {model["model_info"]["id"]: model for model in models}


def _model_inventory_matches_projection(
    projected: dict[str, dict],
    actual: list[dict],
) -> bool:
    actual_by_id = _models_by_id(actual)
    return actual_by_id.keys() == projected.keys() and all(
        actual_by_id[model_id] == model
        for model_id, model in projected.items()
    )


def _create_model(
    payload,
    force,
    actor,
    existing_models_cache,
    *,
    pending_replacements: list[dict] | None = None,
    required_inventory_ids: set[str] | None = None,
    required_inventory_by_id: dict[str, dict] | None = None,
):
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
    credential_name = _model_credential_name(payload)

    # Check if model exists using cached models (could have multiple duplicates)
    model_key = (full_model_name, credential_name)
    existing_models = existing_models_cache.get(model_key, [])
    duplicates_deleted = 0

    if existing_models:
        if force:
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

    if not success:
        logger.error(
            f"Failed to create model: {full_model_name} ({credential_name}) - {result}"
        )
        return False, None, 0

    if action == "replaced":
        old_ids = {model["model_info"]["id"] for model in existing_models}
        initial_inventory_ids = (
            set(required_inventory_ids)
            if required_inventory_ids is not None
            else {
                model["model_info"]["id"]
                for models in existing_models_cache.values()
                for model in models
            }
        )
        initial_inventory_by_id = (
            dict(required_inventory_by_id)
            if required_inventory_by_id is not None
            else {
                model["model_info"]["id"]: model
                for models in existing_models_cache.values()
                for model in models
            }
        )
        expected_replacement = {
            "model_name": full_model_name,
            "litellm_params": payload["litellm_params"],
            "model_info": payload.get("model_info", {}),
        }
        post_create_inventory = get_all_models()
        post_create_ids = {
            model["model_info"]["id"] for model in post_create_inventory
        }
        if not initial_inventory_ids <= post_create_ids:
            logger.error(
                "Replacement creation changed unrelated model inventory: %s (%s)",
                full_model_name,
                credential_name,
            )
            return False, None, 0
        post_create_by_id = _models_by_id(post_create_inventory)
        if any(
            post_create_by_id.get(model_id) != model
            for model_id, model in initial_inventory_by_id.items()
        ):
            logger.error(
                "Replacement creation changed existing model configuration: %s (%s)",
                full_model_name,
                credential_name,
            )
            return False, None, 0
        new_same_identity = [
            model
            for model in post_create_inventory
            if model["model_info"]["id"] not in old_ids
            and (
                model["model_name"],
                _model_credential_name(model),
            )
            == model_key
        ]
        replacement_candidates = [
            model
            for model in new_same_identity
            if _config_contains(model, expected_replacement)
        ]
        if len(new_same_identity) != 1 or len(replacement_candidates) != 1:
            logger.error(
                f"Replacement model did not converge to exactly one new ID: "
                f"{full_model_name} ({credential_name})"
            )
            return False, None, 0

        new_id = new_same_identity[0]["model_info"]["id"]
        if post_create_ids != initial_inventory_ids | {new_id}:
            logger.error(
                "Replacement creation readback contains an unexpected inventory change: %s (%s)",
                full_model_name,
                credential_name,
            )
            return False, None, 0
        if required_inventory_ids is not None:
            required_inventory_ids.add(new_id)
        if required_inventory_by_id is not None:
            required_inventory_by_id[new_id] = post_create_by_id[new_id]

        if pending_replacements is not None:
            required_pre_delete_ids = initial_inventory_ids | {new_id}
            pending_replacements.append(
                {
                    "key": model_key,
                    "payload": payload,
                    "old_ids": set(old_ids),
                    "new_id": new_id,
                    "required_pre_delete_ids": required_pre_delete_ids,
                    "required_pre_delete_inventory": {
                        **initial_inventory_by_id,
                        new_id: post_create_by_id[new_id],
                    },
                }
            )
            logger.info(
                "Prepared replacement model pending destructive-plan validation: %s (%s)",
                full_model_name,
                credential_name,
            )
            return True, action, 0

        deleted = 0
        post_delete_inventory = []
        deleted_ids: set[str] = set()
        for model in existing_models:
            model_id = model["model_info"]["id"]
            deleted_ok, delete_result = delete_model_by_id(model_id)
            if not deleted_ok:
                logger.error(
                    f"Failed to delete replaced model {full_model_name} "
                    f"({credential_name}): {delete_result}"
                )
                return False, None, max(deleted - 1, 0)
            deleted += 1
            deleted_ids.add(model_id)
            post_delete_inventory = get_all_models()
            inventory_ids = {
                item["model_info"]["id"] for item in post_delete_inventory
            }
            expected_inventory_ids = (
                initial_inventory_ids
                | {new_same_identity[0]["model_info"]["id"]}
            ) - deleted_ids
            expected_inventory_by_id = {
                **initial_inventory_by_id,
                new_id: post_create_by_id[new_id],
            }
            for deleted_id in deleted_ids:
                expected_inventory_by_id.pop(deleted_id, None)
            surviving_new_identity = [
                item
                for item in post_delete_inventory
                if item["model_info"]["id"] not in old_ids
                and (item["model_name"], _model_credential_name(item)) == model_key
            ]
            if (
                inventory_ids != expected_inventory_ids
                or not _model_inventory_matches_projection(
                    expected_inventory_by_id,
                    post_delete_inventory,
                )
                or len(surviving_new_identity) != 1
                or not _config_contains(
                    surviving_new_identity[0], expected_replacement
                )
            ):
                logger.error(
                    "Replacement deletion readback did not converge: %s (%s)",
                    full_model_name,
                    credential_name,
                )
                return False, None, 0
        remaining_old_ids = {
            model["model_info"]["id"]
            for model in post_delete_inventory
            if model["model_info"]["id"] in old_ids
        }
        if remaining_old_ids:
            logger.error(
                "Replacement deletion readback did not converge: %s (%s)",
                full_model_name,
                credential_name,
            )
            return False, None, 0
        if required_inventory_ids is not None:
            required_inventory_ids.difference_update(old_ids)
        if required_inventory_by_id is not None:
            for old_id in old_ids:
                required_inventory_by_id.pop(old_id, None)
        duplicates_deleted = max(deleted - 1, 0)
        logger.info(f"Replaced model: {full_model_name} ({credential_name})")
    else:
        logger.info(f"Created model: {full_model_name} ({credential_name})")

    return True, action, duplicates_deleted


# ============================================================================
# Router Settings Management
# ============================================================================


def get_router_settings():
    """Get current router settings from /router/settings endpoint."""
    success, result = get_request("router/settings")
    if not success:
        raise CommandError(f"Failed to read router settings: {result}")
    if not isinstance(result, dict) or not isinstance(result.get("current_values"), dict):
        raise CommandError("Router settings inventory returned an unexpected response")
    settings = result["current_values"]
    aliases = settings.get("model_group_alias", {})
    if not isinstance(aliases, dict) or any(
        not isinstance(source, str)
        or not source
        or not isinstance(target, str)
        or not target
        for source, target in aliases.items()
    ):
        raise CommandError("Router settings inventory contains invalid aliases")
    try:
        router_fallback_references(settings)
    except ModelDiscoveryError as exc:
        raise CommandError(str(exc)) from exc
    if "routing_strategy" in settings and not isinstance(
        settings["routing_strategy"], str
    ):
        raise CommandError("Router settings inventory contains invalid routing_strategy")
    for field in ("cooldown_time", "timeout"):
        if field in settings and (
            isinstance(settings[field], bool)
            or not isinstance(settings[field], (int, float))
        ):
            raise CommandError(f"Router settings inventory contains invalid {field}")
    if "num_retries" in settings and (
        isinstance(settings["num_retries"], bool)
        or not isinstance(settings["num_retries"], int)
    ):
        raise CommandError("Router settings inventory contains invalid num_retries")
    return settings


def update_router_settings(updates: dict, *, current: dict | None = None):
    """Merge router settings, update them, and verify convergence."""
    current = get_router_settings() if current is None else current
    router_settings = dict(current)
    router_settings.update(updates)

    success, result = post_request(
        "config/update", {"router_settings": router_settings}
    )
    if not success:
        return False, result
    readback = get_router_settings()
    if any(
        key not in readback or readback[key] != value
        for key, value in router_settings.items()
    ):
        return False, "Router settings readback did not converge"
    return True, result


# ============================================================================
# Aliases Management
# ============================================================================


def get_current_aliases():
    settings = get_router_settings()
    return settings.get("model_group_alias", {})


def update_aliases(
    aliases: dict | None,
    force=False,
    *,
    current_settings: dict | None = None,
    existing_models: set[str] | None = None,
):
    if aliases is None:
        logger.info("No aliases to update")
        return True, "no aliases"

    current_settings = (
        get_router_settings() if current_settings is None else current_settings
    )
    current_aliases = current_settings.get("model_group_alias", {})

    if not force and current_aliases == aliases:
        logger.info("Aliases already up-to-date, skipping")
        return True, "skipped"

    if aliases:
        existing_models = (
            {model["model_name"] for model in get_all_models()}
            if existing_models is None
            else existing_models
        )
        if not validate_aliases(aliases, existing_models):
            message = "Aliases contain unresolved model references"
            logger.error(message)
            return False, message

    success, result = update_router_settings(
        {"model_group_alias": aliases},
        current=current_settings,
    )

    if success:
        logger.info(f"✅ Updated {len(aliases)} model group aliases")
    else:
        logger.error(f"❌ Failed to update aliases: {result}")

    return success, result


# ============================================================================
# Fallbacks Management
# ============================================================================


def get_current_fallbacks():
    settings = get_router_settings()
    return settings.get("fallbacks", [])


def update_fallbacks(
    fallbacks: list | None,
    force=False,
    *,
    current_settings: dict | None = None,
    existing_models: set[str] | None = None,
):
    if fallbacks is None:
        logger.info("No fallbacks to update")
        return True, "no fallbacks"
    current_settings = (
        get_router_settings() if current_settings is None else current_settings
    )
    current_fallbacks = current_settings.get("fallbacks", [])

    if not force and current_fallbacks == fallbacks:
        logger.info("Fallbacks already up-to-date, skipping")
        return True, "skipped"

    if fallbacks:
        existing_models = (
            {model["model_name"] for model in get_all_models()}
            if existing_models is None
            else existing_models
        )
        current_aliases = current_settings.get("model_group_alias", {})
        if not validate_fallbacks(fallbacks, existing_models, current_aliases):
            message = "Fallbacks contain unresolved model or alias references"
            logger.error(message)
            return False, message

    success, result = update_router_settings(
        {"fallbacks": fallbacks},
        current=current_settings,
    )

    if success:
        logger.info(f"✅ Updated {len(fallbacks)} fallback rules")
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
    if not success:
        raise CommandError(f"Failed to read public model hub: {result}")
    if not isinstance(result, list):
        raise CommandError("Public model hub inventory returned an unexpected response")

    model_groups = []
    for item in result:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("model_group"), str)
            or not item["model_group"]
        ):
            raise CommandError("Public model hub inventory contains an invalid entry")
        model_groups.append(item["model_group"])
    return _normalize_public_model_hub(model_groups)


def update_public_model_hub(
    model_groups: list | None,
    force=False,
    *,
    current_model_groups: list[str] | None = None,
    existing_models: set[str] | None = None,
    current_aliases: dict[str, str] | None = None,
):
    if model_groups is None:
        logger.info("No public model hub entries to update")
        return True, "no public model hub"
    desired_model_groups = _normalize_public_model_hub(model_groups)
    current_model_groups = (
        get_current_public_model_hub()
        if current_model_groups is None
        else current_model_groups
    )

    if not force and current_model_groups == desired_model_groups:
        logger.info("Public model hub already up-to-date, skipping")
        return True, "skipped"

    if desired_model_groups:
        existing_models = (
            {model["model_name"] for model in get_all_models()}
            if existing_models is None
            else existing_models
        )
        current_aliases = (
            get_current_aliases() if current_aliases is None else current_aliases
        )
        if not validate_public_model_hub(
            desired_model_groups, existing_models, current_aliases
        ):
            message = "Public model hub contains unresolved model or alias references"
            logger.error(message)
            return False, message

    success, result = post_request(
        "model_group/make_public", {"model_groups": desired_model_groups}
    )

    if success and get_current_public_model_hub() != desired_model_groups:
        success = False
        result = "Public model hub readback did not converge"

    if success:
        logger.info(
            f"✅ Updated public model hub with {len(desired_model_groups)} entries"
        )
    else:
        logger.error(f"❌ Failed to update public model hub: {result}")

    return success, result


# ============================================================================
# Guardrail Management
# ============================================================================


def get_all_guardrails():
    """Return a trustworthy guardrail inventory from LiteLLM."""
    success, result = get_request("v2/guardrails/list")
    if not success:
        return False, result

    guardrails = (
        result.get("guardrails", result) if isinstance(result, dict) else result
    )
    if not isinstance(guardrails, list):
        return False, "Unexpected response from /v2/guardrails/list"

    seen_names: set[str] = set()
    for guardrail in guardrails:
        if (
            not isinstance(guardrail, dict)
            or not isinstance(guardrail.get("guardrail_name"), str)
            or not guardrail["guardrail_name"]
        ):
            return False, "Guardrail inventory contains an invalid entry"
        name = guardrail["guardrail_name"]
        if name in seen_names:
            return False, f"Guardrail inventory contains duplicate name: {name}"
        seen_names.add(name)
    return True, guardrails


def _config_contains(actual, desired):
    """Compare desired config while ignoring server-generated fields/defaults."""
    if isinstance(desired, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _config_contains(actual[key], value)
            for key, value in desired.items()
        )
    if isinstance(desired, list):
        return isinstance(actual, list) and actual == desired
    return actual == desired


def create_guardrail(guardrail: dict):
    return post_request("guardrails", {"guardrail": guardrail})


def update_guardrail(guardrail_id: str, guardrail: dict):
    encoded_id = _encode_endpoint_segment(guardrail_id, "guardrail ID")
    return put_request(f"guardrails/{encoded_id}", {"guardrail": guardrail})


def _expected_guardrails(config: dict) -> dict[str, dict]:
    expected: dict[str, dict] = {}
    for desired in config.get("guardrails") or []:
        params = desired.get("litellm_params") if isinstance(desired, dict) else None
        mode = params.get("mode") if isinstance(params, dict) else None
        valid_mode = (
            isinstance(mode, str) and bool(mode)
        ) or (
            isinstance(mode, list)
            and bool(mode)
            and all(isinstance(item, str) and item for item in mode)
        )
        if (
            not isinstance(desired, dict)
            or not isinstance(desired.get("guardrail_name"), str)
            or not desired["guardrail_name"]
            or not isinstance(params, dict)
            or not isinstance(params.get("guardrail"), str)
            or not params["guardrail"]
            or not valid_mode
        ):
            raise CommandError("Guardrail configuration contains an invalid entry")
        name = desired["guardrail_name"]
        if name in expected:
            raise CommandError(
                f"Guardrail configuration contains duplicate guardrail name: {name}"
            )
        expected[name] = desired
    return expected


def sync_guardrails(config: dict, *, initial_inventory=None):
    logger.info("=" * 60)
    logger.info("Syncing guardrails...")
    logger.info("=" * 60)

    desired_guardrails = config.get("guardrails", [])
    if not desired_guardrails:
        logger.info("No guardrails in config, skipping")
        return True
    try:
        _expected_guardrails(config)
    except CommandError as exc:
        logger.error(str(exc))
        return False

    if initial_inventory is None:
        success, existing_guardrails = get_all_guardrails()
        if not success:
            logger.error(f"Failed to list guardrails: {existing_guardrails}")
            return False
    else:
        existing_guardrails = initial_inventory
    if not isinstance(existing_guardrails, list):
        logger.error("Guardrail inventory returned an unexpected response")
        return False
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("guardrail_name"), str)
        or not item["guardrail_name"]
        for item in existing_guardrails
    ):
        logger.error("Guardrail inventory contains an invalid entry")
        return False
    existing_by_name = {}
    for guardrail in existing_guardrails:
        name = guardrail["guardrail_name"]
        if name in existing_by_name:
            logger.error("Guardrail inventory contains duplicate name: %s", name)
            return False
        existing_by_name[name] = guardrail
    projected_by_name = dict(existing_by_name)

    action_plan = []
    for desired in desired_guardrails:
        name = desired["guardrail_name"]
        existing = existing_by_name.get(name)
        if existing is None:
            action_plan.append(("create", desired, None))
        elif _config_contains(existing, desired):
            action_plan.append(("skip", desired, None))
        else:
            guardrail_id = existing.get("guardrail_id") or existing.get("id")
            if not isinstance(guardrail_id, str) or not guardrail_id:
                logger.error(f"Cannot update guardrail {name}: missing guardrail_id")
                return False
            action_plan.append(("update", desired, guardrail_id))

    for action, desired, guardrail_id in action_plan:
        name = desired["guardrail_name"]
        if action == "create":
            success, result = create_guardrail(desired)
            if not success:
                logger.error(f"Failed to create guardrail {name}: {result}")
                return False
            logger.info(f"Created guardrail: {name}")
        elif action == "skip":
            logger.info(f"Guardrail already up-to-date, skipping: {name}")
        else:
            assert guardrail_id is not None
            success, result = update_guardrail(guardrail_id, desired)
            if not success:
                logger.error(f"Failed to update guardrail {name}: {result}")
                return False
            logger.info(f"Updated guardrail: {name}")

        success, readback = get_all_guardrails()
        if not success or not isinstance(readback, list):
            logger.error(f"Failed to read back guardrails: {readback}")
            return False
        readback_by_name = {}
        for item in readback:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("guardrail_name"), str)
                or not item["guardrail_name"]
                or item["guardrail_name"] in readback_by_name
            ):
                logger.error("Guardrail readback contains an invalid or duplicate entry")
                return False
            readback_by_name[item["guardrail_name"]] = item
        expected_names = set(projected_by_name)
        if action == "create":
            expected_names.add(name)
        if set(readback_by_name) != expected_names:
            logger.error("Guardrail readback changed projected inventory")
            return False
        if action == "skip" and readback_by_name[name] != projected_by_name[name]:
            logger.error("Skipped guardrail changed before readback: %s", name)
            return False
        if any(
            readback_by_name[other_name] != other
            for other_name, other in projected_by_name.items()
            if other_name != name
        ):
            logger.error("Guardrail readback changed preserved state")
            return False
        actual = readback_by_name.get(name)
        if actual is None or not _config_contains(actual, desired):
            logger.error("Guardrail readback did not converge: %s", name)
            return False
        projected_by_name = readback_by_name
        existing_by_name = readback_by_name

    return True


# ============================================================================
# Main Sync Functions
# ============================================================================


async def sync_credentials(config: dict, force=False, *, initial_inventory=None):
    logger.info("=" * 60)
    logger.info("Syncing credentials...")
    logger.info("=" * 60)

    desired_credentials = config.get("credentials")
    if desired_credentials is None:
        logger.info("No credential section in config, skipping")
        return True
    _expected_credentials(config)
    current_inventory = initial_inventory
    current_names = (
        None
        if current_inventory is None
        else {item["credential_name"] for item in current_inventory}
    )
    for credential in desired_credentials:
        if not (
            isinstance(credential, dict)
            and isinstance(credential.get("credential_name"), str)
            and credential["credential_name"]
            and isinstance(credential.get("credential_values"), dict)
        ):
            logger.error(f"Invalid credential config: {credential}")
            return False

        if current_names is None:
            result = create_credential(credential, force)
        else:
            result = create_credential(
                credential,
                force,
                exists=credential["credential_name"] in current_names,
            )
        success, message = result[:2]
        action = result[2] if len(result) > 2 else None
        name = credential["credential_name"]
        if not success:
            logger.error(f"Failed to sync credential: {name} - {message}")
            return False
        if action == "updated":
            logger.info(f"Updated credential: {name}")
        elif action == "created":
            logger.info(f"Created credential: {name}")
        current_inventory = get_credential_inventory()
        current_names = {item["credential_name"] for item in current_inventory}
        if not verify_credentials(
            {"credentials": [credential]},
            current_inventory,
        ):
            return False

    return True


async def sync_models(
    config: dict,
    force=False,
    *,
    initial_inventory=None,
    pending_replacements: list[dict] | None = None,
):
    logger.info("=" * 60)
    logger.info("Syncing models...")
    logger.info("=" * 60)

    if config.get("models") is None:
        logger.info("No model section in config, skipping")
        return True
    _expected_models(config)
    actor = get_actor_from_key()
    logger.info(f"Actor: {actor}")

    model_payloads = config.get("models", [])

    created_count = 0
    replaced_count = 0
    deleted_count = 0
    failed_count = 0

    # Cache existing models once before processing (store matching raw model objects)
    existing_models_cache = {}
    all_models = get_all_models() if initial_inventory is None else initial_inventory
    required_inventory_ids = {
        model["model_info"]["id"] for model in all_models
    }
    required_inventory_by_id = _models_by_id(all_models)
    for model in all_models:
        key = (
            model["model_name"],
            _model_credential_name(model),
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

    for payload in model_payloads:
        if not (
            isinstance(payload, dict)
            and isinstance(payload.get("model_name"), str)
            and payload["model_name"]
            and isinstance(payload.get("litellm_params"), dict)
            and isinstance(payload.get("model_info", {}), dict)
        ):
            logger.error(f"Invalid model config: {payload}")
            return False
        success, action, duplicates_deleted = _create_model(
            payload,
            force,
            actor,
            existing_models_cache,
            pending_replacements=pending_replacements,
            required_inventory_ids=required_inventory_ids,
            required_inventory_by_id=required_inventory_by_id,
        )
        if not success:
            failed_count += 1
            break
        if action == "created":
            created_count += 1
        elif action == "replaced":
            replaced_count += 1
            deleted_count += duplicates_deleted
        if not (action == "replaced" and pending_replacements is not None):
            readback = get_all_models()
            readback_ids = {
                model["model_info"]["id"] for model in readback
            }
            if action == "created":
                new_ids = readback_ids - required_inventory_ids
                projected = dict(required_inventory_by_id)
                if len(new_ids) == 1:
                    new_id = next(iter(new_ids))
                    projected[new_id] = _models_by_id(readback)[new_id]
                if (
                    len(new_ids) != 1
                    or not _model_inventory_matches_projection(projected, readback)
                ):
                    logger.error("Model creation changed unrelated model inventory")
                    failed_count += 1
                    break
                required_inventory_ids.update(new_ids)
                required_inventory_by_id = projected
            elif action is None and not _model_inventory_matches_projection(
                required_inventory_by_id,
                readback,
            ):
                logger.error("Skipped model readback changed unrelated model inventory")
                failed_count += 1
                break
            if not verify_models({"models": [payload]}, readback):
                failed_count += 1
                break

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
    return failed_count == 0


def _expected_credentials(config: dict) -> set[str]:
    expected: set[str] = set()
    for credential in config.get("credentials") or []:
        if not (
            isinstance(credential, dict)
            and isinstance(credential.get("credential_name"), str)
            and credential["credential_name"]
        ):
            continue
        name = credential["credential_name"]
        if name in expected:
            raise CommandError(
                f"Configuration contains duplicate credential definitions for {name}"
            )
        expected.add(name)
    return expected


def _credential_values_match(actual: object, desired: dict, *, depth: int = 0) -> bool:
    """Match plaintext or LiteLLM's default credential readback mask.

    Masked matching requires the separate desired-payload HMAC and metadata checks
    in verify_credentials. It is not proof of the hidden secret bytes and must not
    replace the readable-value evidence required by credential prune.
    """
    if not isinstance(actual, dict):
        return False
    sensitive_keywords = (
        "authorization", "token", "key", "secret", "vertex_credentials",
        "credentials", "password", "passwd",
    )
    for key, value in desired.items():
        if key not in actual:
            return False
        if _config_contains(actual[key], value):
            continue
        if not isinstance(key, str) or not any(word in key.lower() for word in sensitive_keywords):
            return False
        if isinstance(value, str):
            # _get_masked_values uses four visible characters and four asterisks;
            # short strings have no visible characters and exactly five asterisks.
            masked = "*****" if len(value) <= 4 else value[:2] + "****" + value[-2:]
            if actual[key] != masked:
                return False
        elif isinstance(value, dict) and depth < 20:
            # LiteLLM descends only through sensitive-named dictionary fields.
            if not _credential_values_match(actual[key], value, depth=depth + 1):
                return False
        else:
            return False
    return True


def verify_credentials(config: dict, existing: list | None = None) -> bool:
    if config.get("credentials") is None:
        return True
    _expected_credentials(config)
    inventory = get_credential_inventory() if existing is None else existing
    if inventory and not all(isinstance(item, dict) for item in inventory):
        logger.error(
            "Credential readback contains names without verifiable configuration"
        )
        return False
    if inventory:
        by_name = {item["credential_name"]: item for item in inventory}
        for desired in config.get("credentials", []):
            actual = by_name.get(desired["credential_name"])
            desired_info = desired.get("credential_info", {})
            actual_info = actual.get("credential_info", {}) if actual else {}
            values_match = bool(
                actual
                and isinstance(actual.get("credential_values"), dict)
                and _credential_values_match(
                    actual["credential_values"], desired["credential_values"]
                )
                and hmac.compare_digest(
                    str(actual_info.get(_CREDENTIAL_FINGERPRINT_KEY, "")),
                    _credential_fingerprint(desired),
                )
            )
            if (
                actual is None
                or not _config_contains(actual_info, desired_info)
                or not values_match
            ):
                logger.error(
                    "Credential readback is missing expected configuration: %s",
                    desired["credential_name"],
                )
                return False
        return True
    missing = _expected_credentials(config)
    if missing:
        logger.error(
            "Credential readback is missing expected entries: %s",
            ", ".join(sorted(missing)),
        )
        return False
    return True


def _expected_models(config: dict) -> dict[tuple[str, str | None], dict]:
    expected: dict[tuple[str, str | None], dict] = {}
    for payload in (config.get("models") or []):
        key = (payload["model_name"], _model_credential_name(payload))
        if key in expected:
            raise CommandError(
                f"Configuration contains duplicate model definitions for {key[0]} ({key[1]})"
            )
        expected[key] = payload
    return expected


def _model_matches_payload(model: dict, payload: dict) -> bool:
    desired = {
        "model_name": payload["model_name"],
        "litellm_params": payload.get("litellm_params", {}),
        "model_info": payload.get("model_info", {}),
    }
    return _config_contains(model, desired)


def verify_models(config: dict, existing: list[dict] | None = None) -> bool:
    if config.get("models") is None:
        return True
    inventory = get_all_models() if existing is None else existing
    expected = _expected_models(config)
    for key, payload in expected.items():
        candidates = [
            model
            for model in inventory
            if (model["model_name"], _model_credential_name(model)) == key
            and _model_matches_payload(model, payload)
        ]
        same_key = [
            model
            for model in inventory
            if (model["model_name"], _model_credential_name(model)) == key
        ]
        if len(same_key) != 1 or len(candidates) != 1:
            logger.error(
                "Model readback did not converge to exactly one expected configuration: %s (%s)",
                key[0],
                key[1],
            )
            return False
    return True


def _plan_model_prune(
    config: dict,
    *,
    inventory: list[dict] | None = None,
    router_settings: dict | None = None,
    public_model_hub: list[str] | None = None,
    pending_replacements: list[dict] | None = None,
    prune_stale: bool = True,
):
    pending_replacements = pending_replacements or []
    if config.get("models") is None and not pending_replacements:
        return ([], {}, [])
    inventory = get_all_models() if inventory is None else inventory
    inventory_ids = {model["model_info"]["id"] for model in inventory}
    inventory_by_id = _models_by_id(inventory)
    for replacement in pending_replacements:
        required_ids = replacement.get("required_pre_delete_ids", set())
        if not required_ids <= inventory_ids:
            logger.error("Prepared replacement lost required pre-delete model state")
            return None
        required_inventory = replacement.get("required_pre_delete_inventory", {})
        if any(
            inventory_by_id.get(model_id) != model
            for model_id, model in required_inventory.items()
        ):
            logger.error("Prepared replacement changed required pre-delete model state")
            return None
    expected = _expected_models(config)
    replacement_old_ids = {
        model_id
        for replacement in pending_replacements
        for model_id in replacement["old_ids"]
    }
    retained_inventory = [
        model
        for model in inventory
        if model["model_info"]["id"] not in replacement_old_ids
    ]
    if not verify_models(config, retained_inventory):
        return None

    keep_ids: set[str] = set()
    for key, payload in expected.items():
        matching = [
            model
            for model in retained_inventory
            if (model["model_name"], _model_credential_name(model)) == key
            and _model_matches_payload(model, payload)
        ]
        keep_ids.add(matching[0]["model_info"]["id"])

    for replacement in pending_replacements:
        if replacement["new_id"] not in keep_ids:
            logger.error("Prepared replacement no longer owns the retained model ID")
            return None

    router_settings = (
        get_router_settings() if router_settings is None else router_settings
    )
    public_model_hub = (
        get_current_public_model_hub()
        if public_model_hub is None
        else public_model_hub
    )
    deletion_candidates = [
        model
        for model in inventory
        if model["model_info"]["id"] not in keep_ids
        and (
            prune_stale
            or model["model_info"]["id"] in replacement_old_ids
        )
    ]
    delete_ids = {model["model_info"]["id"] for model in deletion_candidates}
    surviving_names = {
        model["model_name"]
        for model in inventory
        if model["model_info"]["id"] not in delete_ids
    }
    try:
        # Resolve again against the *post-delete* namespace. Literal alias
        # strings cannot protect their concrete backend or detect dangling chains.
        _routing_model_references(
            router_settings, surviving_names, public_model_hub
        )
    except CommandError as exc:
        logger.error("Refusing model prune: %s", exc)
        return None
    return deletion_candidates, expected, inventory


def prune_models(config: dict, plan=None) -> bool:
    if config.get("models") is None:
        return True
    plan = _plan_model_prune(config) if plan is None else plan
    if plan is None:
        return False
    deletion_candidates, expected, current_inventory = plan

    planned_delete_ids = {
        model["model_info"]["id"] for model in deletion_candidates
    }
    expected_retained_ids = {
        model["model_info"]["id"] for model in current_inventory
    } - planned_delete_ids
    projected_by_id = _models_by_id(current_inventory)
    deleted_ids: set[str] = set()
    for model in deletion_candidates:
        model_id = model["model_info"]["id"]
        logger.info(
            "Pruning model: %s (%s)",
            model["model_name"],
            _model_credential_name(model),
        )
        success, result = delete_model_by_id(model_id)
        if not success:
            logger.error("Failed to prune model %s: %s", model_id, result)
            return False
        current_inventory = get_all_models()
        deleted_ids.add(model_id)
        projected_by_id.pop(model_id, None)
        if any(
            item["model_info"]["id"] == model_id for item in current_inventory
        ):
            logger.error("Model prune readback did not remove model %s", model_id)
            return False
        if not _model_inventory_matches_projection(
            projected_by_id,
            current_inventory,
        ):
            logger.error("Model prune changed surviving model configuration")
            return False
        retained = [
            item
            for item in current_inventory
            if item["model_info"]["id"] in expected_retained_ids
        ]
        if not verify_models(config, retained):
            logger.error("Model prune changed retained desired state")
            return False
        allowed_ids = expected_retained_ids | (planned_delete_ids - deleted_ids)
        if {item["model_info"]["id"] for item in current_inventory} != allowed_ids:
            logger.error("Model prune readback contains an unexpected inventory change")
            return False

    remaining = current_inventory
    if {
        model["model_info"]["id"] for model in remaining
    } != expected_retained_ids:
        logger.error("Model prune readback did not converge to the expected inventory")
        return False
    return verify_models(config, remaining)


def _plan_credential_prune(
    config: dict,
    live_models: list[dict] | None = None,
    *,
    existing_credentials: list[dict] | list[str] | None = None,
):
    if config.get("credentials") is None:
        return ([], set(), [])
    existing_inventory = (
        get_credential_inventory()
        if existing_credentials is None
        else existing_credentials
    )
    if any(
        not isinstance(item, dict)
        or not _credential_inventory_entry_is_verifiable(item)
        for item in existing_inventory
    ):
        logger.error(
            "Credential prune requires inventory with verifiable configuration"
        )
        return None
    existing = [
        item["credential_name"] if isinstance(item, dict) else item
        for item in existing_inventory
    ]
    if not verify_credentials(config, existing_inventory):
        return None

    expected = _expected_credentials(config)
    stale = set(existing) - expected
    if not stale:
        return ([], expected, existing_inventory)

    if live_models is None:
        live_models = get_all_models()
    assert live_models is not None
    live_model_credentials = {
        credential_name
        for model in live_models
        if (credential_name := _model_credential_name(model))
    }
    in_use = stale & live_model_credentials
    if in_use:
        logger.error(
            "Refusing to prune credentials still referenced by live models: %s",
            ", ".join(sorted(in_use)),
        )
        return None
    return sorted(stale), expected, existing_inventory


def prune_credentials(config: dict, plan=None) -> bool:
    if config.get("credentials") is None:
        return True
    plan = _plan_credential_prune(config) if plan is None else plan
    if plan is None:
        return False
    stale, expected, current_inventory = plan
    initial_by_name = {
        item["credential_name"]: item for item in current_inventory
    }
    initial_names = {
        item["credential_name"] if isinstance(item, dict) else item
        for item in current_inventory
    }
    deleted_names: set[str] = set()

    for credential_name in stale:
        logger.info("Pruning credential: %s", credential_name)
        success, result = delete_credential(credential_name)
        if not success:
            logger.error(
                "Failed to prune credential %s: %s", credential_name, result
            )
            return False
        current_inventory = get_credential_inventory()
        if any(
            not _credential_inventory_entry_is_verifiable(item)
            for item in current_inventory
        ):
            logger.error(
                "Credential prune readback contains unverifiable inventory evidence"
            )
            return False
        current_names = {
            item["credential_name"] if isinstance(item, dict) else item
            for item in current_inventory
        }
        if credential_name in current_names:
            logger.error(
                "Credential prune readback did not remove credential %s",
                credential_name,
            )
            return False
        deleted_names.add(credential_name)
        if current_names != initial_names - deleted_names:
            logger.error("Credential prune readback contains an unexpected inventory change")
            return False
        for item in current_inventory:
            name = item["credential_name"]
            initial = initial_by_name.get(name)
            if initial is None or not hmac.compare_digest(
                item["credential_info"][_CREDENTIAL_FINGERPRINT_KEY],
                initial["credential_info"][_CREDENTIAL_FINGERPRINT_KEY],
            ):
                logger.error(
                    "Credential prune readback changed planned credential evidence: %s",
                    name,
                )
                return False
        if not verify_credentials(config, current_inventory):
            logger.error("Credential prune changed retained desired state")
            return False

    remaining = {
        item["credential_name"] if isinstance(item, dict) else item
        for item in current_inventory
    }
    if remaining != expected:
        logger.error("Credential prune readback did not converge to the expected inventory")
        return False
    return verify_credentials(config, current_inventory)


def sync_aliases(
    config: dict,
    force=False,
    *,
    current_settings=None,
    existing_models=None,
):
    logger.info("=" * 60)
    logger.info("Syncing aliases...")
    logger.info("=" * 60)

    aliases = config.get("aliases") if "aliases" in config else None
    success, _ = update_aliases(
        aliases,
        force,
        current_settings=current_settings,
        existing_models=existing_models,
    )
    return success


def sync_fallbacks(
    config: dict,
    force=False,
    *,
    current_settings=None,
    existing_models=None,
):
    logger.info("=" * 60)
    logger.info("Syncing fallbacks...")
    logger.info("=" * 60)

    fallbacks = config.get("fallbacks")
    success, _ = update_fallbacks(
        fallbacks,
        force,
        current_settings=current_settings,
        existing_models=existing_models,
    )
    return success


def sync_public_model_hub(
    config: dict,
    force=False,
    *,
    current_model_groups=None,
    existing_models=None,
    current_aliases=None,
):
    logger.info("=" * 60)
    logger.info("Syncing public model hub...")
    logger.info("=" * 60)

    public_model_hub = config.get("public_model_hub")
    success, _ = update_public_model_hub(
        public_model_hub,
        force,
        current_model_groups=current_model_groups,
        existing_models=existing_models,
        current_aliases=current_aliases,
    )
    return success


def sync_router_settings(config: dict, force=False):
    logger.info("=" * 60)
    logger.info("Syncing router settings...")
    logger.info("=" * 60)

    router_settings = config.get("router_settings", {})
    if not router_settings:
        logger.info("No router_settings in config, skipping")
        return True
    reserved = {"model_group_alias", "fallbacks"} & set(router_settings)
    if reserved:
        raise CommandError(
            "router_settings contains reserved routing sections: "
            + ", ".join(sorted(reserved))
        )

    success, _ = update_router_settings(router_settings)
    return success


# ============================================================================
# Main Entry Point
# ============================================================================


def _routing_model_references(
    settings: dict, model_names: set[str], public_model_hub: list[str]
) -> set[str]:
    """Resolve the effective routing graph to concrete model dependencies.

    A concrete target terminates alias traversal (including same-name aliases),
    matching desired alias validation. Only fallback *sources* may use '*'.
    """
    try:
        router_fallback_references(settings)
    except ModelDiscoveryError as exc:
        raise CommandError(str(exc)) from exc
    aliases = settings.get("model_group_alias", {})

    def resolve(reference: str) -> str:
        visited: set[str] = set()
        target = reference
        while target != "*":
            if target in model_names:
                return target
            if target in visited:
                raise CommandError(f"Projected routing contains an alias cycle: {reference}")
            visited.add(target)
            if target not in aliases:
                break
            target = aliases[target]
        raise CommandError(f"Projected routing contains an unresolved reference: {reference}")

    references = {resolve(target) for target in aliases.values()}
    for field in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
        for rule in settings.get(field) or []:
            for source, targets in rule.items():
                if source != "*":
                    references.add(resolve(source))
                references.update(resolve(target) for target in targets)
    references.update(resolve(target) for target in settings.get("default_fallbacks") or [])
    references.update(resolve(target) for target in public_model_hub)
    return references


def _preflight_routing_graph(
    config: dict, components: list[str], snapshot: dict[str, Any], *, prune: bool
) -> None:
    """Project the actual selected updates, without managing omitted sections."""
    if snapshot["router_settings"] is None:
        return
    # update_router_settings shallow-merges fields: aliases and each fallback
    # list replace their whole field; omitted fields retain their live values.
    projected = dict(snapshot["router_settings"])
    for section, field in (("aliases", "model_group_alias"), ("fallbacks", "fallbacks")):
        if section in components and config.get(section) is not None:
            projected[field] = config[section]
    if "router_settings" in components:
        projected.update(config.get("router_settings", {}))
    public_hub = snapshot["public_model_hub"] or []
    if "public_model_hub" in components and config.get("public_model_hub") is not None:
        public_hub = config["public_model_hub"]

    names = {model["model_name"] for model in snapshot["models"] or []}
    if "models" in components and config.get("models") is not None:
        desired_names = {model["model_name"] for model in config["models"]}
        # Force replaces IDs, not model names. Non-pruning sync retains extras.
        names = desired_names if prune else names | desired_names
    try:
        _routing_model_references(projected, names, public_hub)
    except CommandError as exc:
        phase = "model prune preflight" if prune and "models" in components else "routing preflight"
        raise CommandError(f"{phase}: {exc}") from exc


def _preflight_inventories(
    config: dict,
    components: list[str],
    *,
    prune: bool,
) -> dict[str, Any]:
    """Read and validate every selected initial inventory before mutation."""
    selected = set(components)
    aliases = config.get("aliases")
    fallbacks = config.get("fallbacks")
    public_model_hub = config.get("public_model_hub")
    router_updates = config.get("router_settings", {})
    guardrails = config.get("guardrails", [])
    referenced_credentials = {
        credential_name
        for model in (config.get("models") or [])
        if (credential_name := _model_credential_name(model))
    }

    need_credentials = (
        ("credentials" in selected and config.get("credentials") is not None)
        or ("models" in selected and bool(referenced_credentials))
    )
    need_models = (
        ("models" in selected and config.get("models") is not None)
        or ("aliases" in selected and bool(aliases))
        or ("fallbacks" in selected and bool(fallbacks))
        or ("public_model_hub" in selected and bool(public_model_hub))
        or (
            prune
            and "credentials" in selected
            and config.get("credentials") is not None
        )
    )
    need_router = (
        ("aliases" in selected and aliases is not None)
        or ("fallbacks" in selected and fallbacks is not None)
        or ("router_settings" in selected and bool(router_updates))
        or ("public_model_hub" in selected and bool(public_model_hub))
        or (
            prune
            and "models" in selected
            and config.get("models") is not None
        )
    )
    need_public_hub = (
        ("public_model_hub" in selected and public_model_hub is not None)
        or ("aliases" in selected and aliases is not None)
        or (
            prune
            and "models" in selected
            and config.get("models") is not None
        )
    )
    need_guardrails = "guardrails" in selected and bool(guardrails)

    # Even a clear can invalidate preserved references; raw routing-only updates
    # must resolve against live models rather than treating omission as empty.
    need_models = need_models or need_router

    snapshot: dict[str, Any] = {
        "credentials": None,
        "models": None,
        "router_settings": None,
        "public_model_hub": None,
        "guardrails": None,
    }
    if need_credentials:
        snapshot["credentials"] = get_credential_inventory()
    if need_models:
        snapshot["models"] = get_all_models()
    if need_router:
        snapshot["router_settings"] = get_router_settings()
    if need_public_hub:
        snapshot["public_model_hub"] = get_current_public_model_hub()
    if need_guardrails:
        success, inventory = get_all_guardrails()
        if not success:
            raise CommandError(f"Failed to read guardrail inventory: {inventory}")
        snapshot["guardrails"] = inventory
    return snapshot


async def sync_config(
    *,
    config_path: Path | None = None,
    preset: str | None = None,
    only: str = "credentials,models,aliases,fallbacks,public_model_hub,router_settings,guardrails",
    force: bool = False,
    prune: bool = False,
    dry_run: bool = False,
    root: Path = REPO_ROOT,
) -> int:
    load_dotenv(root / ".env")

    components = [c.strip() for c in only.split(",")]
    valid_components = {
        "credentials",
        "models",
        "aliases",
        "fallbacks",
        "public_model_hub",
        "router_settings",
        "guardrails",
    }
    invalid = set(components) - valid_components
    if invalid:
        raise CommandError(f"Invalid components: {invalid}. Valid: {valid_components}")

    logger.info(f"Components to sync: {components}")
    logger.info(f"Force: {force}, Prune: {prune}")

    if not dry_run:
        missing_env = [
            name
            for name, value in (
                ("LITELLM_API_KEY", _get_api_key()),
                ("LITELLM_BASE_URL", _get_base_url()),
            )
            if not value
        ]
        if missing_env:
            raise CommandError(f"Missing management environment: {', '.join(missing_env)}")

    if config_path:
        config_file = config_path
        logger.info(f"Using custom config file: {config_file}")
        if not config_file.is_file():
            raise CommandError(f"Config file not found: {config_file}")
        config = generate_config(config_file, require_complete=True)
    else:
        logger.info(f"Using deployment preset: {preset}")
        config = generate_config_for_preset(
            preset,
            require_complete=True,
            root=root,
        )

    # Resolve duplicate/conflicting desired identities before any API mutation.
    if "credentials" in components and config.get("credentials") is not None:
        _expected_credentials(config)
    if "models" in components and config.get("models") is not None:
        _expected_models(config)
    if "guardrails" in components and config.get("guardrails"):
        _expected_guardrails(config)

    if dry_run:
        logger.info("DRY RUN: Configuration resolved; no API changes will be made")
        return 0

    # Forced model replacement also deletes old IDs: validate its routing
    # inventories before creating replacements, not only at the delete phase.
    snapshot = _preflight_inventories(
        config,
        components,
        prune=prune or (force and "models" in components and config.get("models") is not None),
    )
    _preflight_routing_graph(config, components, snapshot, prune=prune)
    model_inventory = snapshot["models"]
    live_model_names = {
        model["model_name"] for model in (model_inventory or [])
    }
    if "models" in components and config.get("models") is not None:
        available_model_names = {
            model["model_name"] for model in config.get("models", [])
        }
    else:
        available_model_names = live_model_names
    initial_router = snapshot["router_settings"]

    referenced_credentials = {
        credential_name
        for model in (config.get("models") or [])
        if (credential_name := _model_credential_name(model))
    }
    if "models" in components and referenced_credentials:
        desired_credentials = {
            credential["credential_name"]: credential
            for credential in (config.get("credentials") or [])
        }
        missing_definitions = referenced_credentials - set(desired_credentials)
        if missing_definitions:
            raise CommandError(
                "Models reference credentials without desired definitions: "
                + ", ".join(sorted(missing_definitions))
            )
        if "credentials" not in components and not verify_credentials(
            {
                "credentials": [
                    desired_credentials[name]
                    for name in sorted(referenced_credentials)
                ]
            },
            snapshot["credentials"],
        ):
            raise CommandError(
                "Model credential dependencies did not converge before model synchronization"
            )

    if "credentials" in components:
        if not await sync_credentials(
            config,
            force,
            initial_inventory=snapshot["credentials"],
        ):
            raise CommandError(
                "One or more LiteLLM synchronization operations failed: credentials"
            )
        if not verify_credentials(config):
            raise CommandError(
                "One or more LiteLLM synchronization operations failed: "
                "credential readback did not converge"
            )

    pending_replacements: list[dict] = []
    model_readback: list[dict] | None = None
    if "models" in components:
        if not await sync_models(
            config,
            force,
            initial_inventory=model_inventory,
            pending_replacements=pending_replacements,
        ):
            raise CommandError(
                "One or more LiteLLM synchronization operations failed: models"
            )
        if pending_replacements:
            model_readback = get_all_models()
            assert model_readback is not None
            pending_old_ids = {
                model_id
                for replacement in pending_replacements
                for model_id in replacement["old_ids"]
            }
            retained_readback = [
                model
                for model in model_readback
                if model["model_info"]["id"] not in pending_old_ids
            ]
            models_converged = verify_models(config, retained_readback)
        else:
            models_converged = verify_models(config)
        if not models_converged:
            raise CommandError(
                "One or more LiteLLM synchronization operations failed: "
                "model readback did not converge"
            )

    if "aliases" in components and not sync_aliases(
        config,
        force,
        current_settings=initial_router,
        existing_models=available_model_names,
    ):
        raise CommandError(
            "One or more LiteLLM synchronization operations failed: aliases"
        )

    fallback_router = dict(initial_router or {})
    if "aliases" in components and config.get("aliases") is not None:
        fallback_router["model_group_alias"] = config["aliases"]
    if "fallbacks" in components and not sync_fallbacks(
        config,
        force,
        current_settings=fallback_router,
        existing_models=available_model_names,
    ):
        raise CommandError(
            "One or more LiteLLM synchronization operations failed: fallbacks"
        )

    if "public_model_hub" in components and not sync_public_model_hub(
        config,
        force,
        current_model_groups=snapshot["public_model_hub"],
        existing_models=available_model_names,
        current_aliases=fallback_router.get("model_group_alias", {}),
    ):
        raise CommandError(
            "One or more LiteLLM synchronization operations failed: public model hub"
        )

    if "router_settings" in components and not sync_router_settings(
        config, force
    ):
        raise CommandError(
            "One or more LiteLLM synchronization operations failed: router settings"
        )

    if "guardrails" in components and not sync_guardrails(
        config,
        initial_inventory=snapshot["guardrails"],
    ):
        raise CommandError(
            "One or more LiteLLM synchronization operations failed: guardrails"
        )

    # Validate the complete destructive plan before the first delete. Forced
    # replacement removals are part of the same model plan as ordinary prune.
    model_prune_plan = None
    credential_prune_plan = None
    if prune or pending_replacements:
        remaining_models = None
        if "models" in components:
            model_prune_plan = _plan_model_prune(
                config,
                inventory=model_readback if pending_replacements else None,
                pending_replacements=pending_replacements,
                prune_stale=prune,
            )
            if model_prune_plan is None:
                raise CommandError(
                    "One or more LiteLLM synchronization operations failed: model prune preflight"
                )
            if config.get("models") is not None:
                candidates, _, inventory = model_prune_plan
                delete_ids = {model["model_info"]["id"] for model in candidates}
                remaining_models = [
                    model
                    for model in inventory
                    if model["model_info"]["id"] not in delete_ids
                ]
        if prune and "credentials" in components:
            credential_prune_plan = _plan_credential_prune(
                config,
                live_models=remaining_models,
            )
            if credential_prune_plan is None:
                raise CommandError(
                    "One or more LiteLLM synchronization operations failed: credential prune preflight"
                )

    if (prune or pending_replacements) and "models" in components and not prune_models(
        config, plan=model_prune_plan
    ):
        raise CommandError(
            "One or more LiteLLM synchronization operations failed: model prune"
        )
    if prune and "credentials" in components and not prune_credentials(
        config, plan=credential_prune_plan
    ):
        raise CommandError(
            "One or more LiteLLM synchronization operations failed: credential prune"
        )

    logger.info("=" * 60)
    logger.info("✅ Sync complete!")
    logger.info("=" * 60)
    return 0
