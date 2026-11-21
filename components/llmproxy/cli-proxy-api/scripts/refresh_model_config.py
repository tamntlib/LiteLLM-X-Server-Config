#!/usr/bin/env python3
"""Refresh Claude/GPT aliases, base_model mappings, and GPT-Image model references."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class UpdateError(ValueError):
    """Safe, actionable error text that never includes response/config values."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirect target.


def fetch_latest(api_key, lines, *, open_url=None):
    """The Anthropic catalogue is newest-first; select by line, never ID parsing."""
    if not api_key or not api_key.strip():
        raise UpdateError("ANTHROPIC_API_KEY is required")
    open_url = open_url or build_opener(_NoRedirect()).open
    latest = {}
    cursor = None
    seen = set()
    for _ in range(100):
        query = {"limit": 1000, "lifecycle[]": "active"}
        if cursor is not None:
            query["after_id"] = cursor
        request = Request(
            "https://api.anthropic.com/v1/models?" + urlencode(query),
            headers={
                "Authorization": "Bearer " + api_key,
                "anthropic-version": "2026-10-21",
                "Accept": "application/json",
            },
        )
        with open_url(request, timeout=30) as response:
            page = json.load(response)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise UpdateError("Invalid Models API response")
        for model in page["data"]:
            if not isinstance(model, dict):
                raise UpdateError("Invalid Models API model entry")
            line = model.get("line")
            if not isinstance(line, str) or line not in lines or line in latest:
                continue
            if model.get("lifecycle") != "active":
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                raise UpdateError("Invalid Models API model ID")
            latest[line] = model_id
        if lines <= latest.keys():
            return latest
        if page.get("has_more") is False:
            break
        cursor = page.get("last_id")
        if page.get("has_more") is not True or not isinstance(cursor, str) or not cursor or cursor in seen:
            raise UpdateError("Invalid or repeated Models API pagination cursor")
        seen.add(cursor)
    missing = ", ".join(sorted(lines - latest.keys()))
    raise UpdateError(f"No active model found for required lines: {missing}")


GPT_TEXT_LINES = frozenset({"astra", "sol", "terra", "luna"})
GPT_IMAGE_LINES = frozenset({"flare", "sunburst"})
GPT_TEXT_MODEL = re.compile(r"gpt-(?P<version>\d+(?:\.\d+)*)-(?P<line>astra|sol|terra|luna)")
GPT_IMAGE_MODEL = re.compile(r"gpt-image-(?P<version>\d+(?:\.\d+)*)-(?P<line>flare|sunburst)")
GPT_MODEL = GPT_TEXT_MODEL
GPT_TEXT_ALIAS = re.compile(r"(?P<prefix>(?:[^/]+/)*)" + GPT_TEXT_MODEL.pattern)
GPT_IMAGE_ALIAS = re.compile(r"(?P<prefix>(?:[^/]+/)*)" + GPT_IMAGE_MODEL.pattern)
GPT_ALIAS = GPT_TEXT_ALIAS


def _match_openai_model(model_id):
    return GPT_TEXT_MODEL.fullmatch(model_id) or GPT_IMAGE_MODEL.fullmatch(model_id)


def _select_highest_version(candidates, lines, pattern_fn):
    latest = {}
    versions = {}
    for model_id in candidates:
        match = pattern_fn(model_id)
        if match is None or match["line"] not in lines:
            continue
        line = match["line"]
        version = tuple(int(part) for part in match["version"].split("."))
        if line in versions and version == versions[line] and latest[line] != model_id:
            raise UpdateError("Ambiguous OpenAI model versions for a required line")
        if line not in versions or version > versions[line]:
            versions[line] = version
            latest[line] = model_id
    return latest


def fetch_latest_openai(api_key, lines, *, open_url=None):
    """Select the highest numeric canonical GPT / GPT-Image version per configured line.

    OpenAI provides no line field or release ordering. Do not use `created`
    as a release date or mix dated snapshots/previews into canonical aliases.
    """
    if not api_key or not api_key.strip():
        raise UpdateError("OPENAI_API_KEY is required")
    open_url = open_url or build_opener(_NoRedirect()).open
    request = Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
    )
    with open_url(request, timeout=30) as response:
        page = json.load(response)
    if not isinstance(page, dict) or page.get("object") != "list" or not isinstance(page.get("data"), list):
        raise UpdateError("Invalid OpenAI Models API response")
    candidates = []
    seen = set()
    today = datetime.now(timezone.utc).date()
    for model in page["data"]:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            raise UpdateError("Invalid OpenAI Models API model entry")
        model_id = model["id"]
        match = _match_openai_model(model_id)
        if match is None or match["line"] not in lines:
            continue
        if model_id in seen:
            raise UpdateError("Duplicate OpenAI model ID")
        seen.add(model_id)
        shutdown = model.get("shutdown_date")
        if shutdown is not None:
            if not isinstance(shutdown, str):
                raise UpdateError("Invalid OpenAI model shutdown date")
            try:
                shutdown_date = date.fromisoformat(shutdown)
            except ValueError as exc:
                raise UpdateError("Invalid OpenAI model shutdown date") from exc
            if shutdown_date <= today:
                continue
        candidates.append(model_id)
    latest = _select_highest_version(candidates, lines, _match_openai_model)
    missing = lines - latest.keys()
    if missing:
        raise UpdateError("No available canonical GPT model for required lines: " + ", ".join(sorted(missing)))
    return latest


MODEL_ALIAS = re.compile(
    r"(?P<prefix>(?:[^/]+/)*)claude-(?P<line>fable|opus|sonnet|haiku)-\d+(?:-\d+)*"
)


PUBLIC_CATALOGUES = {
    "anthropic": "https://platform.claude.com/docs/en/models/overview.md",
    "openai": "https://developers.openai.com/api/docs/models.md",
}
_OPENAI_MODEL_LINK = re.compile(r"(?m)^- \[[^\]\n]+\]\(/api/docs/models/([^/()]+)\.md\):")


def _markdown_section(text, heading):
    sections = re.findall(r"(?ms)^## " + re.escape(heading) + r"[ \t]*\r?\n(.*?)(?=^## |\Z)", text)
    if len(sections) != 1:
        raise UpdateError("Public catalogue must contain one " + heading + " section")
    return sections[0]


def fetch_latest_public(provider, lines, *, open_url=None):
    """Read official public Markdown docs for required model lines."""
    open_url = open_url or build_opener(_NoRedirect()).open
    request = Request(
        PUBLIC_CATALOGUES[provider],
        headers={"Accept": "text/markdown", "User-Agent": "model-config-refresher/1.0"},
    )
    with open_url(request, timeout=30) as response:
        content = response.read(1_048_577)
    if len(content) > 1_048_576:
        raise UpdateError("Public catalogue exceeds the size limit")
    text = content.decode("utf-8")
    latest = {}
    if provider == "openai":
        unknown = lines - (GPT_TEXT_LINES | GPT_IMAGE_LINES)
        if unknown:
            raise UpdateError("Unsupported OpenAI model lines: " + ", ".join(sorted(unknown)))
        text_lines = lines & GPT_TEXT_LINES
        image_lines = lines & GPT_IMAGE_LINES
        if text_lines:
            featured = _markdown_section(text, "Featured models")
            for model_id in _OPENAI_MODEL_LINK.findall(featured):
                match = GPT_TEXT_MODEL.fullmatch(model_id)
                if match is None or match["line"] not in text_lines:
                    continue
                line = match["line"]
                if line in latest and latest[line] != model_id:
                    raise UpdateError("Public catalogue has ambiguous model IDs for a required line")
                latest[line] = model_id
        if image_lines:
            catalog = _markdown_section(text, "Browse our full catalog of models")
            latest.update(
                _select_highest_version(_OPENAI_MODEL_LINK.findall(catalog), image_lines, GPT_IMAGE_MODEL.fullmatch)
            )
    else:
        section = _markdown_section(text, "Latest models")
        rows = re.findall(r"(?m)^\|[ \t]*Claude API ID[ \t]*\|(.*)\|[ \t]*\r?$", section)
        if len(rows) != 1:
            raise UpdateError("Public catalogue must contain one Claude API ID row")
        for model_id in re.findall(r"`([^`]+)`", rows[0]):
            match = MODEL_ALIAS.fullmatch(model_id)
            if match is None or match["prefix"] or match["line"] not in lines:
                continue
            line = match["line"]
            if line in latest and latest[line] != model_id:
                raise UpdateError("Public catalogue has ambiguous model IDs for a required line")
            latest[line] = model_id
    if lines - latest.keys():
        raise UpdateError("Public catalogue is missing required model lines")
    return latest


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("Duplicate JSON keys are not supported")
        result[key] = value
    return result


def _members(text, start):
    """Yield object keys and their token/value offsets in validated JSON."""
    decoder = json.JSONDecoder()
    position = start + 1
    while True:
        while text[position].isspace() or text[position] == ',':
            position += 1
        if text[position] == '}':
            return
        key_start = position
        key, key_end = decoder.raw_decode(text, position)
        position = key_end
        while text[position].isspace() or text[position] == ':':
            position += 1
        value_start = position
        _, position = decoder.raw_decode(text, position)
        yield key, key_start, key_end, value_start, position


def _load_config(text):
    config = json.loads(text, object_pairs_hook=_unique_object)
    if not isinstance(config, dict) or not isinstance(config.get("aliases"), dict):
        raise UpdateError("Config must contain an aliases object")
    for alias_target in config["aliases"].values():
        if isinstance(alias_target, dict) and "model" in alias_target and not isinstance(alias_target["model"], str):
            raise UpdateError("Alias target model must be a string")
    base_models = config.get("model_name_base_model_map", {})
    if not isinstance(base_models, dict) or any(not isinstance(v, str) for v in base_models.values()):
        raise UpdateError("model_name_base_model_map must be an object of strings")
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise UpdateError("providers must be an object")
    for provider_cfg in providers.values():
        if not isinstance(provider_cfg, dict):
            raise UpdateError("Each provider configuration must be an object")
        interfaces = provider_cfg.get("interfaces", {})
        if not isinstance(interfaces, dict):
            raise UpdateError("Provider interfaces must be an object")
        for interface_cfg in interfaces.values():
            if not isinstance(interface_cfg, dict):
                raise UpdateError("Each provider interface must be an object")
            models = interface_cfg.get("models", {})
            if not isinstance(models, dict):
                raise UpdateError("Provider interface models must be an object")
    return config


def _match_model(name, provider):
    if provider == "openai":
        for pattern in (GPT_TEXT_ALIAS, GPT_IMAGE_ALIAS):
            if (match := pattern.fullmatch(name)) is not None:
                return match, pattern
        return None
    if (match := MODEL_ALIAS.fullmatch(name)) is not None:
        return match, MODEL_ALIAS
    return None


def _alias_target_model(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("model"), str):
        return value["model"]
    return None


def _iter_provider_models(config):
    for provider_name, provider_cfg in config.get("providers", {}).items():
        for interface_name, interface_cfg in provider_cfg.get("interfaces", {}).items():
            yield provider_name, interface_name, interface_cfg.get("models", {})


def _updated_model(match, latest, pattern):
    line = match["line"]
    model_id = latest.get(line)
    candidate = pattern.fullmatch(model_id) if isinstance(model_id, str) else None
    if candidate is None or candidate["line"] != line or candidate["prefix"]:
        raise UpdateError(f"Missing or invalid latest model for line: {line}")
    return match["prefix"] + model_id


def required_lines(text, provider="anthropic"):
    config = _load_config(text)
    lines = set()
    for name in (*config["aliases"], *config.get("model_name_base_model_map", {}).values()):
        if (found := _match_model(name, provider)) is not None:
            lines.add(found[0]["line"])
    if provider == "openai":
        for value in config["aliases"].values():
            target = _alias_target_model(value)
            if target is not None and (match := GPT_IMAGE_ALIAS.fullmatch(target)) is not None:
                lines.add(match["line"])
        for _, _, models in _iter_provider_models(config):
            for name in models:
                if (match := GPT_IMAGE_ALIAS.fullmatch(name)) is not None:
                    lines.add(match["line"])
    return lines


def _section_members(text, section):
    root_start = len(text) - len(text.lstrip())
    for key, _, _, value_start, _ in _members(text, root_start):
        if key == section:
            yield from _members(text, value_start)
            return


def _replace_tokens(text, replacements):
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        text = text[:start] + json.dumps(replacement) + text[end:]
    return text


def update_aliases(text, latest, provider="anthropic"):
    aliases = _load_config(text)["aliases"]
    renames = {}
    for name in aliases:
        if (found := _match_model(name, provider)) is not None:
            match, pattern = found
            renamed = _updated_model(match, latest, pattern)
            if renamed != name:
                renames[name] = renamed
    destinations = [renames.get(name, name) for name in aliases]
    if len(destinations) != len(set(destinations)):
        raise UpdateError("Alias collision after update; resolve duplicate line aliases first")

    if not renames:
        return text, []
    replacements = [
        (start, end, renames[key])
        for key, start, end, _, _ in _section_members(text, "aliases")
        if key in renames
    ]
    return _replace_tokens(text, replacements), list(renames.items())


def update_alias_targets(text, latest, provider="openai"):
    if provider != "openai":
        return text, []
    aliases = _load_config(text)["aliases"]
    changes = {}
    for key, value in aliases.items():
        target = _alias_target_model(value)
        if target is not None and (match := GPT_IMAGE_ALIAS.fullmatch(target)) is not None:
            updated = _updated_model(match, latest, GPT_IMAGE_ALIAS)
            if updated != target:
                changes[key] = (target, updated)
    if not changes:
        return text, []
    replacements = []
    for key, _, _, value_start, value_end in _section_members(text, "aliases"):
        if key not in changes:
            continue
        if text[value_start] == "{":
            for field, _, _, field_start, field_end in _members(text, value_start):
                if field == "model":
                    replacements.append((field_start, field_end, changes[key][1]))
                    break
        else:
            replacements.append((value_start, value_end, changes[key][1]))
    return _replace_tokens(text, replacements), [(key, *pair) for key, pair in changes.items()]


def update_provider_models(text, latest, provider="openai"):
    if provider != "openai":
        return text, []
    config = _load_config(text)
    renames_by_scope = {}
    change_list = []
    for provider_name, interface_name, models in _iter_provider_models(config):
        renames = {}
        for name in models:
            if (match := GPT_IMAGE_ALIAS.fullmatch(name)) is not None:
                renamed = _updated_model(match, latest, GPT_IMAGE_ALIAS)
                if renamed != name:
                    renames[name] = renamed
        destinations = [renames.get(name, name) for name in models]
        if len(destinations) != len(set(destinations)):
            raise UpdateError("Provider model collision after update; resolve duplicate line models first")
        if renames:
            renames_by_scope[(provider_name, interface_name)] = renames
            scope = f"{provider_name}/{interface_name}"
            for old, new in renames.items():
                change_list.append((scope, old, new))
    if not renames_by_scope:
        return text, []
    replacements = []
    for provider_name, _, _, provider_start, _ in _section_members(text, "providers"):
        for field, _, _, field_start, _ in _members(text, provider_start):
            if field != "interfaces":
                continue
            for interface_name, _, _, interface_start, _ in _members(text, field_start):
                renames = renames_by_scope.get((provider_name, interface_name))
                if not renames:
                    continue
                for iface_field, _, _, models_start, _ in _members(text, interface_start):
                    if iface_field != "models":
                        continue
                    for model_key, key_start, key_end, _, _ in _members(text, models_start):
                        if model_key in renames:
                            replacements.append((key_start, key_end, renames[model_key]))
    return _replace_tokens(text, replacements), change_list


def update_base_models(text, latest, provider="anthropic"):
    section = "model_name_base_model_map"
    base_models = _load_config(text).get(section, {})
    changes = {}
    for name, model_id in base_models.items():
        if (found := _match_model(model_id, provider)) is not None:
            match, pattern = found
            updated = _updated_model(match, latest, pattern)
            if updated != model_id:
                changes[name] = (model_id, updated)
    if not changes:
        return text, []
    replacements = [
        (start, end, changes[key][1])
        for key, _, _, start, end in _section_members(text, section)
        if key in changes
    ]
    return _replace_tokens(text, replacements), [(key, *pair) for key, pair in changes.items()]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=Path(__file__).resolve().parents[1] / "integrations/llmproxy/litellm/config.json",
        help="Source config.json (default: this component's LiteLLM integration)",
    )
    parser.add_argument(
        "--provider", choices=("anthropic", "openai", "all"), default="anthropic",
        help="Catalogue to query (default: anthropic; all updates both in one write)",
    )
    parser.add_argument(
        "--source", choices=("public", "api"), default="public",
        help="Discovery source (default: public official docs, no API key required)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Apply changes locally; never sync or deploy")
    mode.add_argument("--dry-run", action="store_true", help="Preview only (the default)")
    args = parser.parse_args(argv)
    # Reuse the repository's symlink-safe atomic writer; no third-party packages.
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from llmproxy.core.resources import write_private_text

    try:
        path = args.config.expanduser().absolute()
        if path.is_symlink() or not path.is_file():
            raise UpdateError("Config must be a regular file, not a symlink")
        original = path.read_bytes()
        text = original.decode("utf-8")
        providers = ("anthropic", "openai") if args.provider == "all" else (args.provider,)
        updated = text
        changes = []
        target_changes = []
        provider_changes = []
        base_changes = []
        for provider in providers:
            lines = required_lines(text, provider)
            if not lines:
                continue
            if args.source == "public":
                latest = fetch_latest_public(provider, lines)
            else:
                fetch = fetch_latest_openai if provider == "openai" else fetch_latest
                key_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
                latest = fetch(os.environ.get(key_name, ""), lines)
            updated, provider_alias_changes = update_aliases(updated, latest, provider)
            changes.extend(provider_alias_changes)
            updated, provider_target_changes = update_alias_targets(updated, latest, provider)
            target_changes.extend(provider_target_changes)
            updated, provider_model_changes = update_provider_models(updated, latest, provider)
            provider_changes.extend(provider_model_changes)
            updated, provider_base_changes = update_base_models(updated, latest, provider)
            base_changes.extend(provider_base_changes)
        if not (changes or target_changes or provider_changes or base_changes):
            print("No changes needed for selected model aliases, provider models, or base_model mappings.")
            return 0
        for old, new in changes:
            print(f"{old} -> {new}")
        for key, old, new in target_changes:
            print(f"alias_target[{key}]: {old} -> {new}")
        for scope, old, new in provider_changes:
            print(f"provider_model[{scope}]: {old} -> {new}")
        for key, old, new in base_changes:
            print(f"base_model[{key}]: {old} -> {new}")
        parts = [f"{len(changes)} aliases"]
        if target_changes:
            parts.append(f"{len(target_changes)} alias targets")
        if provider_changes:
            parts.append(f"{len(provider_changes)} provider models")
        if base_changes:
            parts.append(f"{len(base_changes)} base_model mappings")
        summary = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]
        if args.write:
            if path.read_bytes() != original:
                raise UpdateError("Config changed during discovery; rerun before writing")
            write_private_text(path, updated)
            print(f"Updated {summary} in {path}; no sync or deployment performed.")
        else:
            print(f"Dry run: {summary} would change; use --write to apply.")
        return 0
    except HTTPError as exc:
        print(f"Model discovery failed: HTTP {exc.code}", file=sys.stderr)
    except (URLError, TimeoutError):
        print("Model discovery network request failed", file=sys.stderr)
    except UpdateError as exc:
        print(f"Update aborted: {exc}", file=sys.stderr)
    except (OSError, ValueError):
        # No exception payloads: a malformed response/config may contain secrets.
        print("Update aborted: invalid JSON/encoding or unsafe/unwritable config file.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
