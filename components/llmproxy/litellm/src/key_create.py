"""LiteLLM user and API-key creation with explicit management credentials."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from collections.abc import Callable, Sequence

from llmproxy.core.command import CommandError
from llmproxy.core.http import format_http_error, request_json

DEFAULT_KEY_RPM_LIMIT = 100
DEFAULT_KEY_MAX_BUDGET = 700
DEFAULT_KEY_BUDGET_DURATION = "7d"
DEFAULT_USER_MODELS = ("all-proxy-models",)
DEFAULT_ACCESS_GROUP = "General"
NO_DEFAULT_MODELS = "no-default-models"


def resolve_access_group_ids(
    access_groups: Sequence[str],
    *,
    base_url: str,
    api_key: str,
) -> list[str]:
    """Resolve Unified Access Group names (or exact IDs) via /v1/access_group."""
    requested = [item.strip() for item in access_groups if item.strip()]
    if not requested:
        return []
    try:
        data = request_json(
            f"{base_url}/v1/access_group",
            method="GET",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
    except urllib.error.HTTPError as exc:
        raise CommandError(
            f"Failed to list access groups: {format_http_error(exc)}"
        ) from exc
    except Exception as exc:
        raise CommandError(f"Failed to list access groups: {exc}") from exc

    if not isinstance(data, list):
        raise CommandError("Access group lookup returned an unexpected response")

    by_name: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for item in data:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("access_group_id"), str)
            or not item["access_group_id"]
            or not isinstance(item.get("access_group_name"), str)
            or not item["access_group_name"]
        ):
            raise CommandError("Access group lookup returned an invalid entry")
        name = item["access_group_name"]
        group_id = item["access_group_id"]
        if name in by_name:
            raise CommandError(f"Access group lookup returned duplicate name: {name}")
        if group_id in by_id:
            raise CommandError(f"Access group lookup returned duplicate ID: {group_id}")
        by_name[name] = group_id
        by_id[group_id] = group_id

    resolved: list[str] = []
    seen: set[str] = set()
    missing: list[str] = []
    for ref in requested:
        group_id = by_name.get(ref) or by_id.get(ref)
        if group_id is None:
            missing.append(ref)
            continue
        if group_id not in seen:
            resolved.append(group_id)
            seen.add(group_id)
    if missing:
        raise CommandError(
            "Unified Access Group not found: " + ", ".join(missing)
        )
    return resolved


def get_user_by_email(email, *, base_url: str, api_key: str):
    url = f"{base_url}/user/list?user_email={urllib.parse.quote(email)}&page=1&page_size=100"
    try:
        data = request_json(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
    except urllib.error.HTTPError as exc:
        raise CommandError(f"Failed to check user: {format_http_error(exc)}") from exc
    except Exception as exc:
        raise CommandError(f"Failed to look up user: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise CommandError("User lookup returned an unexpected response")
    matches = [
        user
        for user in data["users"]
        if isinstance(user, dict) and user.get("user_email") == email
    ]
    if len(matches) > 1:
        raise CommandError(f"User lookup returned multiple exact users for {email}")
    if not matches:
        return None
    if not matches[0].get("user_id"):
        raise CommandError("LiteLLM user lookup returned no user_id")
    return matches[0]


def create_user(email, models=None, *, base_url: str, api_key: str):
    payload = {
        "user_id": None,
        "user_email": email,
        "user_role": "internal_user_viewer",
        "models": list(models) if models else list(DEFAULT_USER_MODELS),
        "auto_create_key": False,
    }
    try:
        result = request_json(
            f"{base_url}/user/new",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "*/*",
            },
        )
    except urllib.error.HTTPError as exc:
        raise CommandError(f"Failed to create user: {format_http_error(exc)}") from exc
    except Exception as exc:
        raise CommandError(f"Failed to create user: {exc}") from exc
    if not isinstance(result, dict):
        raise CommandError("User creation returned an unexpected response")
    return result


def create_api_key(
    user_id,
    key_alias,
    key_value=None,
    rpm_limit=DEFAULT_KEY_RPM_LIMIT,
    max_budget=DEFAULT_KEY_MAX_BUDGET,
    budget_duration=DEFAULT_KEY_BUDGET_DURATION,
    team_id=None,
    models=None,
    access_group_ids=None,
    *,
    base_url: str,
    api_key: str,
):
    payload = {
        "user_id": user_id,
        "team_id": team_id,
        "key_alias": key_alias,
        "models": list(models) if models else [],
        "key_type": "llm_api",
        "rpm_limit": rpm_limit,
        "max_budget": max_budget,
        "budget_duration": budget_duration,
        "metadata": {},
    }
    if access_group_ids:
        payload["access_group_ids"] = list(access_group_ids)
    if key_value:
        payload["key"] = key_value
    try:
        result = request_json(
            f"{base_url}/key/generate",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "*/*",
            },
        )
    except urllib.error.HTTPError as exc:
        raise CommandError(f"Failed to create API key: {format_http_error(exc)}") from exc
    except Exception as exc:
        raise CommandError(f"Failed to create API key: {exc}") from exc
    if not isinstance(result, dict):
        raise CommandError("API-key creation returned an unexpected response")
    return result


def create_key_for_user(
    email: str,
    *,
    base_url: str,
    api_key: str,
    alias: str | None = None,
    key_value: str | None = None,
    rpm_limit=DEFAULT_KEY_RPM_LIMIT,
    max_budget=DEFAULT_KEY_MAX_BUDGET,
    budget_duration=DEFAULT_KEY_BUDGET_DURATION,
    team_id: str | None = None,
    models: Sequence[str] | None = None,
    access_groups: Sequence[str] | None = None,
    report: Callable[[str], None] = lambda message: None,
) -> str:
    """Look up or create the user, then create and return their API key.

    Progress is reported at the original operation boundaries. Credentials,
    model/access-group selections and reporting are supplied by the caller,
    not the process.
    """
    explicit_models = [item.strip() for item in (models or []) if item.strip()]
    explicit_groups = [item.strip() for item in (access_groups or []) if item.strip()]
    use_default_access_group = not explicit_models and not explicit_groups and team_id is None
    requested_groups = [DEFAULT_ACCESS_GROUP] if use_default_access_group else explicit_groups

    key_alias = alias or email.split("@")[0]
    report(f"Processing user: {email}")
    user = get_user_by_email(email, base_url=base_url, api_key=api_key)
    if user is not None and not user.get("user_id"):
        raise CommandError("LiteLLM user lookup returned no user_id")

    resolved_access_group_ids = (
        resolve_access_group_ids(requested_groups, base_url=base_url, api_key=api_key)
        if requested_groups
        else None
    )

    if resolved_access_group_ids:
        user_models: list[str] | None = list(DEFAULT_USER_MODELS)
        key_models = explicit_models or [NO_DEFAULT_MODELS]
    else:
        user_models = explicit_models or None
        key_models = explicit_models

    if user:
        user_id = user["user_id"]
        report(f"User already exists: {user_id}")
    else:
        result = create_user(email, models=user_models, base_url=base_url, api_key=api_key)
        user_id = result.get("user_id")
        if not user_id:
            raise CommandError("LiteLLM user creation returned no user_id")
        report(f"User created: {user_id}")
    result = create_api_key(
        user_id,
        key_alias,
        key_value,
        rpm_limit,
        max_budget,
        budget_duration,
        team_id=team_id,
        models=key_models,
        access_group_ids=resolved_access_group_ids,
        base_url=base_url,
        api_key=api_key,
    )
    created_key = result.get("key")
    if not created_key:
        raise CommandError("LiteLLM API-key creation returned no key")
    report("API key created")
    return created_key
