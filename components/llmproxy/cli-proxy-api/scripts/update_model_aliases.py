#!/usr/bin/env python3
"""Refresh Claude/GPT aliases and base_model mappings without changing routing."""

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


GPT_MODEL = re.compile(r"gpt-(?P<version>\d+(?:\.\d+)*)-(?P<line>astra|sol|terra|luna)")
GPT_ALIAS = re.compile(r"(?P<prefix>(?:[^/]+/)*)" + GPT_MODEL.pattern)


def fetch_latest_openai(api_key, lines, *, open_url=None):
    """Select the highest numeric canonical GPT version per configured line.

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
    latest = {}
    versions = {}
    seen = set()
    today = datetime.now(timezone.utc).date()
    for model in page["data"]:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            raise UpdateError("Invalid OpenAI Models API model entry")
        model_id = model["id"]
        match = GPT_MODEL.fullmatch(model_id)
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
        line = match["line"]
        version = tuple(int(part) for part in match["version"].split('.'))
        if line not in versions or version > versions[line]:
            versions[line] = version
            latest[line] = model_id
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


def fetch_latest_public(provider, lines, *, open_url=None):
    """Read only the current-lineup section of official public Markdown docs."""
    open_url = open_url or build_opener(_NoRedirect()).open
    request = Request(
        PUBLIC_CATALOGUES[provider],
        headers={"Accept": "text/markdown", "User-Agent": "model-alias-updater/1.0"},
    )
    with open_url(request, timeout=30) as response:
        content = response.read(1_048_577)
    if len(content) > 1_048_576:
        raise UpdateError("Public catalogue exceeds the size limit")
    text = content.decode("utf-8")
    heading = "Featured models" if provider == "openai" else "Latest models"
    sections = re.findall(r"(?ms)^## " + heading + r"[ \t]*\r?\n(.*?)(?=^## |\Z)", text)
    if len(sections) != 1:
        raise UpdateError("Public catalogue must contain one " + heading + " section")
    if provider == "openai":
        candidates = re.findall(
            r"(?m)^- \[[^\]\n]+\]\(/api/docs/models/([^/()]+)\.md\):", sections[0]
        )
    else:
        rows = re.findall(r"(?m)^\|[ \t]*Claude API ID[ \t]*\|(.*)\|[ \t]*\r?$", sections[0])
        if len(rows) != 1:
            raise UpdateError("Public catalogue must contain one Claude API ID row")
        candidates = re.findall(r"`([^`]+)`", rows[0])
    latest = {}
    for model_id in candidates:
        match = (GPT_MODEL if provider == "openai" else MODEL_ALIAS).fullmatch(model_id)
        if match is None or match.groupdict().get("prefix") or match["line"] not in lines:
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
    base_models = config.get("model_name_base_model_map", {})
    if not isinstance(base_models, dict) or any(not isinstance(v, str) for v in base_models.values()):
        raise UpdateError("model_name_base_model_map must be an object of strings")
    return config


def _model_pattern(provider):
    return GPT_ALIAS if provider == "openai" else MODEL_ALIAS


def _updated_model(match, latest, pattern):
    line = match["line"]
    model_id = latest.get(line)
    candidate = pattern.fullmatch(model_id) if isinstance(model_id, str) else None
    if candidate is None or candidate["line"] != line or candidate["prefix"]:
        raise UpdateError(f"Missing or invalid latest model for line: {line}")
    return match["prefix"] + model_id


def required_lines(text, provider="anthropic"):
    config = _load_config(text)
    pattern = _model_pattern(provider)
    return {
        match["line"]
        for name in (*config["aliases"], *config.get("model_name_base_model_map", {}).values())
        if (match := pattern.fullmatch(name))
    }


def _section_members(text, section):
    root_start = len(text) - len(text.lstrip())
    for key, _, _, value_start, _ in _members(text, root_start):
        if key == section:
            yield from _members(text, value_start)
            return


def _replace_tokens(text, replacements):
    for start, end, replacement in reversed(replacements):
        text = text[:start] + json.dumps(replacement) + text[end:]
    return text


def update_aliases(text, latest, provider="anthropic"):
    aliases = _load_config(text)["aliases"]
    pattern = _model_pattern(provider)
    renames = {}
    for name in aliases:
        if (match := pattern.fullmatch(name)) is not None:
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


def update_base_models(text, latest, provider="anthropic"):
    section = "model_name_base_model_map"
    base_models = _load_config(text).get(section, {})
    pattern = _model_pattern(provider)
    changes = {}
    for name, model_id in base_models.items():
        if (match := pattern.fullmatch(model_id)) is not None:
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
            updated, provider_changes = update_aliases(updated, latest, provider)
            changes.extend(provider_changes)
            updated, provider_base_changes = update_base_models(updated, latest, provider)
            base_changes.extend(provider_base_changes)
        if not (changes or base_changes):
            print("No changes needed for selected model aliases or base_model mappings.")
            return 0
        for old, new in changes:
            print(f"{old} -> {new}")
        for key, old, new in base_changes:
            print(f"base_model[{key}]: {old} -> {new}")
        summary = f"{len(changes)} aliases"
        if base_changes:
            summary += f" and {len(base_changes)} base_model mappings"
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
