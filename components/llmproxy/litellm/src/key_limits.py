"""LiteLLM API-key limit rules, inventory and plan/apply operations."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from llmproxy.core.command import CommandError
from llmproxy.core.json_merge import load_with_local
from llmproxy.core.http import format_http_error, request_json

DEFAULT_KEY_RPM_LIMIT = 100
DEFAULT_KEY_MAX_BUDGET = 700
DEFAULT_KEY_BUDGET_DURATION = "7d"
RESET_KEY_SPEND_VALUE = 0
MAX_PAGE_SIZE = 100
# Put key aliases, key names, hashes, tokens, user IDs, or emails here.
# Empty means update all keys.
TARGET_KEYS = []
# Put key aliases, key names, hashes, tokens, user IDs, or emails here.
# These keys are skipped.
EXCLUDED_KEYS = ["default_user_id"]


def build_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def format_amount(value):
    return f"{value:g}"


def key_update_fields(rpm_limit, max_budget, budget_duration, reset_spend=False):
    updates = {
        "rpm_limit": rpm_limit,
        "max_budget": max_budget,
        "budget_duration": budget_duration,
    }
    if reset_spend:
        updates["spend"] = RESET_KEY_SPEND_VALUE
    return updates


def key_values(key_record):
    if isinstance(key_record, dict):
        values = []
        for field in (
            "key",
            "token",
            "key_name",
            "key_alias",
            "api_key",
            "user_id",
            "user_email",
        ):
            value = key_record.get(field)
            if value:
                values.append(str(value).strip())
        return {value for value in values if value}

    key_record = str(key_record).strip()
    if not key_record:
        raise ValueError("target key cannot be empty")
    return {key_record}


def key_identifier(key_record):
    if not isinstance(key_record, dict):
        return str(key_record).strip()

    for field in ("key", "token", "key_name", "api_key"):
        value = key_record.get(field)
        if value:
            return str(value).strip()
    return ""


def key_label(key_record):
    if not isinstance(key_record, dict):
        return mask_value(str(key_record).strip())

    alias = key_record.get("key_alias")
    identifier = key_identifier(key_record)
    if alias:
        return f"{alias} ({mask_value(identifier)})"
    return mask_value(identifier)


def mask_value(value):
    if not value:
        return "<missing>"
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


def matches_any_configured_key(key_record, configured_keys):
    record_values = key_values(key_record)
    for configured_key in configured_keys:
        if record_values.intersection(key_values(configured_key)):
            return True
    return False


def validate_key_limit_overrides(overrides):
    if not isinstance(overrides, dict):
        raise ValueError("API key limits configuration must be a dictionary")

    allowed_fields = {
        "rpm_limit",
        "rpm_limit_multiplier",
        "max_budget",
        "max_budget_multiplier",
    }
    for selector, limits in overrides.items():
        key_values(selector)
        if not isinstance(limits, dict) or not limits:
            raise ValueError(f"Custom limits for {selector!r} must be a dictionary")

        unknown_fields = set(limits) - allowed_fields
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise ValueError(
                f"Unknown custom limit field(s) for {selector!r}: {fields}"
            )

        rpm_limit = limits.get("rpm_limit")
        if rpm_limit is not None and (
            isinstance(rpm_limit, bool)
            or not isinstance(rpm_limit, int)
            or rpm_limit <= 0
        ):
            raise ValueError(
                f"rpm_limit for {selector!r} must be a positive integer"
            )

        max_budget = limits.get("max_budget")
        if max_budget is not None and (
            isinstance(max_budget, bool)
            or not isinstance(max_budget, (int, float))
            or max_budget <= 0
        ):
            raise ValueError(
                f"max_budget for {selector!r} must be a positive number"
            )

        for field in ("rpm_limit_multiplier", "max_budget_multiplier"):
            multiplier = limits.get(field)
            if field in limits and (
                isinstance(multiplier, bool)
                or not isinstance(multiplier, (int, float))
                or multiplier <= 0
            ):
                raise ValueError(
                    f"{field} for {selector!r} must be a positive number"
                )


def load_key_limit_overrides(config_path):
    overrides, _ = load_with_local(Path(config_path))
    overrides.pop("$schema", None)
    validate_key_limit_overrides(overrides)
    return overrides


def multiply_limit(default_value, multiplier, field, selector):
    if default_value is None:
        raise ValueError(
            f"{field}_multiplier for {selector!r} cannot be used when the "
            f"default {field} is unlimited"
        )

    value = default_value * multiplier
    if field == "rpm_limit":
        if not float(value).is_integer():
            raise ValueError(
                f"rpm_limit_multiplier for {selector!r} must produce an "
                "integer rpm_limit"
            )
        return int(value)
    return value


def resolve_limit(limits, field, default_value, selector):
    if field in limits:
        return limits[field]

    multiplier_field = f"{field}_multiplier"
    if multiplier_field in limits:
        return multiply_limit(
            default_value,
            limits[multiplier_field],
            field,
            selector,
        )

    return default_value


def resolve_key_limits(key_record, default_rpm_limit, default_max_budget, overrides):
    record_values = key_values(key_record)
    for selector, limits in overrides.items():
        if record_values.intersection(key_values(selector)):
            return (
                resolve_limit(limits, "rpm_limit", default_rpm_limit, selector),
                resolve_limit(limits, "max_budget", default_max_budget, selector),
            )
    return default_rpm_limit, default_max_budget


def filter_target_keys(keys, target_keys):
    if not target_keys:
        return list(keys)

    return [
        key_record
        for key_record in keys
        if matches_any_configured_key(key_record, target_keys)
    ]


def filter_excluded_keys(keys, excluded_keys):
    if not excluded_keys:
        return list(keys)

    return [
        key_record
        for key_record in keys
        if not matches_any_configured_key(key_record, excluded_keys)
    ]


def list_api_keys(page_size, *, base_url: str, api_key: str):
    keys = []
    page = 1
    total_pages = None

    while total_pages is None or page <= total_pages:
        query = {
            "page": page,
            "size": page_size,
            "return_full_object": "true",
        }
        url = f"{base_url}/key/list?{urllib.parse.urlencode(query)}"
        data = request_json(
            url,
            method="GET",
            headers=build_headers(api_key),
        )
        page_keys = data.get("keys", [])
        if not isinstance(page_keys, list):
            raise ValueError("/key/list response did not include a keys list")

        keys.extend(page_keys)
        total_pages = data.get("total_pages") or 1
        page += 1

    return keys


def build_update_payload(key_record, rpm_limit, max_budget, budget_duration, reset_spend=False):
    identifier = key_identifier(key_record)
    if not identifier:
        raise ValueError(f"key has no usable identifier: {key_label(key_record)}")

    return {
        "key": identifier,
        **key_update_fields(rpm_limit, max_budget, budget_duration, reset_spend),
    }


def update_api_key(
    key_record,
    rpm_limit,
    max_budget,
    budget_duration,
    reset_spend=False,
    *,
    base_url: str,
    api_key: str,
):
    payload = build_update_payload(
        key_record,
        rpm_limit,
        max_budget,
        budget_duration,
        reset_spend,
    )
    return request_json(
        f"{base_url}/key/update",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=build_headers(api_key),
    )


def resolve_target_keys(
    page_size, *, base_url: str, api_key: str, target_keys=None, excluded_keys=None
):
    if target_keys is None:
        target_keys = TARGET_KEYS
    if excluded_keys is None:
        excluded_keys = EXCLUDED_KEYS
    keys = list_api_keys(page_size, base_url=base_url, api_key=api_key)
    target_keys = filter_target_keys(keys, target_keys)
    target_keys = filter_excluded_keys(target_keys, excluded_keys)
    return target_keys


def set_key_limits(
    *,
    base_url: str,
    api_key: str,
    config_path: Path,
    rpm_limit=DEFAULT_KEY_RPM_LIMIT,
    max_budget=DEFAULT_KEY_MAX_BUDGET,
    budget_duration=DEFAULT_KEY_BUDGET_DURATION,
    page_size=MAX_PAGE_SIZE,
    reset_spend=False,
    apply=False,
    target_keys=None,
    excluded_keys=None,
    report: Callable[[str], None] = lambda message: None,
) -> int:
    """Load rules, resolve all targets and plan before optionally applying.

    No process environment or component context is consulted. The caller owns
    configuration selection and presentation through the progress callback.
    Omitted selectors use TARGET_KEYS/EXCLUDED_KEYS at invocation; explicit
    empty sequences disable the corresponding target or exclusion filter.
    """
    if target_keys is None:
        target_keys = TARGET_KEYS
    if excluded_keys is None:
        excluded_keys = EXCLUDED_KEYS
    try:
        key_limit_overrides = load_key_limit_overrides(config_path)
    except Exception as exc:
        raise CommandError(f"Invalid API key limits configuration: {exc}") from exc

    page_size = min(page_size, MAX_PAGE_SIZE)
    try:
        selected_keys = resolve_target_keys(
            page_size,
            base_url=base_url,
            api_key=api_key,
            target_keys=target_keys,
            excluded_keys=excluded_keys,
        )
    except urllib.error.HTTPError as exc:
        raise CommandError(f"Failed to list API keys: {format_http_error(exc)}") from exc
    except Exception as exc:
        raise CommandError(f"Invalid target/excluded keys: {exc}") from exc

    planned_updates = [
        (
            key_record,
            *resolve_key_limits(
                key_record,
                rpm_limit,
                max_budget,
                key_limit_overrides,
            ),
        )
        for key_record in selected_keys
    ]
    summary = (
        f"default_rpm_limit={rpm_limit}, "
        f"default_max_budget={format_amount(max_budget)}, "
        f"key_limit_overrides={len(key_limit_overrides)}, "
        f"budget_duration={budget_duration}"
    )
    if reset_spend:
        summary = f"{summary}, spend={RESET_KEY_SPEND_VALUE:g}"
    configured_scope = "configured target key(s)" if target_keys else "all key(s)"
    if excluded_keys:
        configured_scope = f"{configured_scope} minus exclusions"
    report(
        f"{'APPLY' if apply else 'DRY RUN'}: set {summary} for "
        f"{len(selected_keys)} {configured_scope}.",
    )
    if not selected_keys:
        report("No API keys to update after applying targets/exclusions.")
        return 0
    if not apply:
        for key_record, rpm_limit, max_budget in planned_updates:
            report(
                f"Would update {key_label(key_record)}: "
                f"rpm_limit={rpm_limit}, max_budget={format_amount(max_budget)}",
            )
        report("Add --apply to apply these changes.")
        return 0

    failed = 0
    for key_record, rpm_limit, max_budget in planned_updates:
        try:
            update_api_key(
                key_record,
                rpm_limit,
                max_budget,
                budget_duration,
                reset_spend,
                base_url=base_url,
                api_key=api_key,
            )
            report(
                f"Updated {key_label(key_record)}: "
                f"rpm_limit={rpm_limit}, max_budget={format_amount(max_budget)}",
            )
        except urllib.error.HTTPError as exc:
            failed += 1
            report(
                f"Failed to update {key_label(key_record)}: {format_http_error(exc)}",
            )
        except Exception as exc:
            failed += 1
            report(f"Failed to update {key_label(key_record)}: {exc}")
    if failed:
        raise CommandError(f"Completed with {failed} failed update(s)")
    report(f"Updated {len(selected_keys)} API key(s).")
    return 0
