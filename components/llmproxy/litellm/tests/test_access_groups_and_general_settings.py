"""Tests for general_settings and Unified Access Groups generation, sync, and prune safety."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from llmproxy.core.command import CommandError
from llmproxy.core.component_modules import load_component_module

from ..commands import config_sync as sync_command
from ..src import config_generate as generator
from ..src import config_sync as syncer


def _http_error(code: int, reason: str, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://invalid.test/config/field/info",
        code=code,
        msg=reason,
        hdrs={},
        fp=io.BytesIO(body.encode("utf-8")),
    )


def _live_group(
    name: str,
    group_id: str,
    *,
    description: str | None = None,
    models: list[str] | None = None,
    assigned_key_ids: list[str] | None = None,
    assigned_team_ids: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    item: dict = {
        "access_group_id": group_id,
        "access_group_name": name,
        "description": description,
        "access_model_names": list(models or []),
        "access_mcp_server_ids": [],
        "mcp_tool_permissions": {},
        "access_agent_ids": [],
        "access_skill_names": [],
        "assigned_key_ids": list(assigned_key_ids or []),
        "assigned_team_ids": list(assigned_team_ids or []),
    }
    if extra:
        item.update(extra)
    return item


class FakeLiteLLMAPI:
    def __init__(
        self,
        *,
        models=(),
        router=None,
        general_settings=None,
        guardrails=(),
        access_groups=(),
    ):
        self.models = copy.deepcopy(list(models))
        self.router = copy.deepcopy(router or {})
        self.general_settings = copy.deepcopy(general_settings or {})
        self.guardrails = copy.deepcopy(list(guardrails))
        self.access_groups = copy.deepcopy(list(access_groups))
        self.calls: list[tuple[str, str, object]] = []
        self._next_group_num = 1
        self._next_guardrail_num = 1

    def get(self, endpoint: str):
        self.calls.append(("GET", endpoint, None))
        if endpoint.startswith("v2/model/info"):
            return True, {"data": copy.deepcopy(self.models)}
        if endpoint == "router/settings":
            return True, {"current_values": copy.deepcopy(self.router)}
        if endpoint in ("credentials", "public/model_hub"):
            return True, []
        if endpoint == "v2/guardrails/list":
            return True, {"guardrails": copy.deepcopy(self.guardrails)}
        if endpoint == "v1/access_group":
            return True, copy.deepcopy(self.access_groups)
        raise AssertionError(("unexpected GET", endpoint))

    def get_general_setting(self, field_name: str):
        self.calls.append(("GET_FIELD", field_name, None))
        if field_name not in self.general_settings:
            return {
                "set": False,
                "field_name": field_name,
                "field_value": None,
                "source": None,
                "editable": True,
            }
        entry = self.general_settings[field_name]
        return {
            "set": True,
            "field_name": field_name,
            "field_value": copy.deepcopy(entry["field_value"]),
            "source": entry.get("source", "db"),
            "editable": entry.get("editable", True),
        }

    def post(self, endpoint: str, payload: dict):
        self.calls.append(("POST", endpoint, copy.deepcopy(payload)))
        if endpoint == "config/field/update":
            if payload.get("config_type") != "general_settings":
                return False, "HTTP Error 422: Unprocessable Entity"
            field_name = payload["field_name"]
            self.general_settings[field_name] = {
                "field_value": copy.deepcopy(payload["field_value"]),
                "source": "db",
                "editable": True,
            }
            return True, "{}"
        if endpoint == "v1/access_group":
            group_id = f"ag-new-{self._next_group_num}"
            self._next_group_num += 1
            created = _live_group(
                payload["access_group_name"],
                group_id,
                description=payload.get("description"),
                models=payload.get("access_model_names"),
                extra={
                    k: copy.deepcopy(v)
                    for k, v in payload.items()
                    if k not in ("access_group_name", "description", "access_model_names")
                },
            )
            self.access_groups.append(created)
            return True, json.dumps(created)
        if endpoint == "guardrails":
            guardrail_id = f"gr-new-{self._next_guardrail_num}"
            self._next_guardrail_num += 1
            created = {**copy.deepcopy(payload["guardrail"]), "guardrail_id": guardrail_id}
            self.guardrails.append(created)
            return True, json.dumps(created)
        if endpoint == "config/update":
            self.router = copy.deepcopy(payload["router_settings"])
            return True, "{}"
        raise AssertionError(("unexpected POST", endpoint))

    def put(self, endpoint: str, payload: dict):
        self.calls.append(("PUT", endpoint, copy.deepcopy(payload)))
        if endpoint.startswith("guardrails/"):
            guardrail_id = endpoint.split("/", 1)[1]
            for item in self.guardrails:
                if (item.get("guardrail_id") or item.get("id")) == guardrail_id:
                    item.update(copy.deepcopy(payload["guardrail"]))
                    return True, json.dumps(item)
            return False, "HTTP Error 404: Not Found"
        if endpoint.startswith("v1/access_group/"):
            group_id = endpoint.split("/", 2)[2]
            for item in self.access_groups:
                if item["access_group_id"] == group_id:
                    for key, value in payload.items():
                        item[key] = copy.deepcopy(value)
                    return True, json.dumps(item)
            return False, "HTTP Error 404: Not Found"
        raise AssertionError(("unexpected PUT", endpoint))

    def delete(self, endpoint: str):
        self.calls.append(("DELETE", endpoint, None))
        if endpoint.startswith("guardrails/"):
            guardrail_id = endpoint.split("/", 1)[1]
            self.guardrails = [
                item
                for item in self.guardrails
                if (item.get("guardrail_id") or item.get("id")) != guardrail_id
            ]
            return True, ""
        if endpoint.startswith("v1/access_group/"):
            group_id = endpoint.split("/", 2)[2]
            self.access_groups = [
                item for item in self.access_groups if item["access_group_id"] != group_id
            ]
            return True, ""
        raise AssertionError(("unexpected DELETE", endpoint))


def _run_sync(source: dict, api: FakeLiteLLMAPI, **options) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "config.json"
        path.write_text(json.dumps(source))
        with (
            patch.dict(
                os.environ,
                {
                    "LITELLM_API_KEY": "synthetic-master",
                    "LITELLM_BASE_URL": "http://invalid.test",
                },
            ),
            patch.object(syncer, "load_dotenv"),
            patch.object(generator, "validate_prices"),
            patch.object(syncer, "get_request", side_effect=api.get),
            patch.object(syncer, "get_general_setting", side_effect=api.get_general_setting),
            patch.object(syncer, "post_request", side_effect=api.post),
            patch.object(syncer, "put_request", side_effect=api.put),
            patch.object(syncer, "delete_request", side_effect=api.delete),
            patch.object(syncer, "get_actor_from_key", return_value="synthetic-actor"),
        ):
            return asyncio.run(syncer.sync_config(config_path=path, root=root, **options))


class TestAccessGroupsAndGeneralSettingsGeneration(unittest.TestCase):
    def test_three_state_matrix_and_round_trip_for_access_groups_and_general_settings(self):
        base_models = [
            {
                "model_name": "a-1",
                "litellm_params": {"model": "anthropic/a-1"},
                "model_info": {},
            }
        ]
        cases = (
            ({}, None, {}),
            ({"access_groups": None, "general_settings": {}}, None, {}),
            ({"access_groups": {}}, [], {}),
            ({"access_groups": []}, [], {}),
            (
                {
                    "models": base_models,
                    "aliases": {
                        "claude-*": "a-1",
                        "claude-opus-5-5": "a-1",
                    },
                    "general_settings": {"enforce_fallback_model_access": False},
                    "access_groups": {
                        "General": {
                            "description": "Default users",
                            "access_model_names": ["claude-*", "$aliases:claude-*"],
                        }
                    },
                },
                [
                    {
                        "access_group_name": "General",
                        "description": "Default users",
                        "access_model_names": ["claude-*", "claude-opus-5-5"],
                    }
                ],
                {"enforce_fallback_model_access": False},
            ),
        )
        for source, expected_groups, expected_gs in cases:
            with self.subTest(source=source), patch.object(generator, "validate_prices"):
                generated = generator.generate_config(source, require_complete=True)
                self.assertEqual(generated["access_groups"], expected_groups)
                self.assertEqual(generated["general_settings"], expected_gs)
                round_tripped = generator.generate_config(generated, require_complete=True)
                self.assertEqual(round_tripped, generated)

    def test_aliases_macro_excludes_hidden_and_cross_prefix_and_deduplicates(self):
        source = {
            "models": [
                {"model_name": name, "litellm_params": {"model": f"openai/{name}"}, "model_info": {}}
                for name in ("a-0", "a-1", "o-1", "gpt-image-2.6-flare", "codex-auto-review")
            ],
            "aliases": {
                "claude-*": "a-1",
                "claude-fable-5-2": "a-0",
                "claude-opus-5-5": "a-1",
                "claude-secret": {"model": "a-1", "hidden": True},
                "openai/claude-opus-5-5": "o-1",
                "gpt-*": "o-1",
                "gpt-6.2-sol": "o-1",
                "gpt-image-*": "gpt-image-2.6-flare",
                "gpt-image-2.6-flare": "gpt-image-2.6-flare",
                "anthropic/gpt-6.2-sol": "a-1",
                "codex-auto-review": "codex-auto-review",
            },
            "access_groups": {
                "General": {
                    "access_model_names": [
                        "claude-*",
                        "claude-opus-5-5",
                        "$aliases:claude-*",
                        "gpt-*",
                        "$aliases:gpt-*",
                        "codex-auto-review",
                    ]
                }
            },
        }
        with patch.object(generator, "validate_prices"):
            generated = generator.generate_config(source, require_complete=True)
        self.assertEqual(
            generated["access_groups"],
            [
                {
                    "access_group_name": "General",
                    "access_model_names": [
                        "claude-*",
                        "claude-opus-5-5",
                        "claude-fable-5-2",
                        "gpt-*",
                        "gpt-6.2-sol",
                        "gpt-image-2.6-flare",
                        "codex-auto-review",
                    ],
                }
            ],
        )

    def test_unmatched_or_empty_aliases_macro_fails_strict_generation(self):
        source_base = {
            "models": [
                {"model_name": "a-1", "litellm_params": {"model": "anthropic/a-1"}, "model_info": {}}
            ],
            "aliases": {"claude-*": "a-1"},
        }
        for expr, regex in (
            ("$aliases:", "empty \\$aliases:"),
            ("$aliases:claude-*", "matched no visible aliases"),
            ("$aliases:gemini-*", "matched no visible aliases"),
        ):
            with self.subTest(expr=expr), patch.object(generator, "validate_prices"):
                source = {
                    **source_base,
                    "access_groups": {"General": {"access_model_names": [expr]}},
                }
                with self.assertRaisesRegex(generator.ModelDiscoveryError, regex):
                    generator.generate_config(source, require_complete=True)

    def test_malformed_access_groups_and_general_settings_fail_closed(self):
        invalid_configs = (
            ({"general_settings": []}, "general_settings.*object"),
            ({"general_settings": {"": True}}, "general_settings.*invalid field"),
            ({"general_settings": {"enforce_fallback_model_access": "false"}}, "must be a boolean"),
            ({"access_groups": "bad"}, "access_groups.*object, list, or null"),
            ({"access_groups": {"General": "bad"}}, "must be an object"),
            (
                {"access_groups": {"General": {"assigned_key_ids": ["key-1"]}}},
                "unsupported field.*assigned_key_ids",
            ),
            (
                {"access_groups": {"General": {"assigned_team_ids": ["team-1"]}}},
                "unsupported field.*assigned_team_ids",
            ),
            (
                {"access_groups": [{"access_group_name": "General", "assigned_key_ids": []}]},
                "unsupported field.*assigned_key_ids",
            ),
            (
                {"access_groups": [{"access_group_name": "Dup"}, {"access_group_name": "Dup"}]},
                "duplicate access group name",
            ),
            (
                {"access_groups": {"General": {"access_model_names": [""]}}},
                "list of non-empty strings",
            ),
            (
                {"access_groups": {"General": {"mcp_tool_permissions": {"srv": [""]}}}},
                "mcp_tool_permissions",
            ),
        )
        for cfg, regex in invalid_configs:
            with self.subTest(cfg=cfg), patch.object(generator, "validate_prices"):
                with self.assertRaisesRegex(generator.ModelDiscoveryError, regex):
                    generator.generate_config(cfg, require_complete=True)

    def test_unresolved_access_group_model_reference_fails_strict_generation(self):
        base = {
            "models": [
                {"model_name": "a-1", "litellm_params": {"model": "anthropic/a-1"}, "model_info": {}}
            ],
            "aliases": {"claude-opus-5-5": "a-1"},
        }
        for bad_entry in ("missing-model", "gemini-*"):
            with self.subTest(bad_entry=bad_entry), patch.object(generator, "validate_prices"):
                source = {
                    **base,
                    "access_groups": {"General": {"access_model_names": [bad_entry]}},
                }
                with self.assertRaisesRegex(
                    generator.ModelDiscoveryError, "unresolved access group model references"
                ):
                    generator.generate_config(source, require_complete=True)

    def test_default_preset_generates_unified_general_group_and_general_settings(self):
        with patch.object(generator, "validate_prices"):
            cfg, base = generator.load_config_for_preset("default", include_local=False)
            cfg["providers"]["cli-proxy-api"]["api_key"] = "dummy"
            generated = generator.generate_config(cfg, base_config=base, require_complete=True)
            round_tripped = generator.generate_config(generated, require_complete=True)

        self.assertEqual(round_tripped, generated)
        self.assertEqual(
            generated["general_settings"],
            {"enforce_fallback_model_access": False},
        )
        self.assertEqual(
            generated["access_groups"],
            [
                {
                    "access_group_name": "General",
                    "description": "Default user access group",
                    "access_model_names": [
                        "claude-*",
                        "claude-fable-5-2",
                        "claude-opus-5-5",
                        "claude-sonnet-5-5",
                        "claude-haiku-5-5",
                        "gpt-*",
                        "gpt-6.2-astra",
                        "gpt-6.2-sol",
                        "gpt-6.1-terra",
                        "gpt-6-luna",
                        "gpt-image-2.6-flare",
                        "gpt-image-2.6-sunburst",
                        "codex-auto-review",
                    ],
                }
            ],
        )
        self.assertEqual(
            [m["model_name"] for m in generated["models"] if "access_groups" in m.get("model_info", {})],
            [],
        )


class TestGeneralSettingsSync(unittest.TestCase):
    def test_get_general_setting_recognizes_exact_unset_and_rejects_other_errors(self):
        with (
            patch.object(syncer, "_get_base_url", return_value="http://invalid.test"),
            patch.object(syncer, "_get_api_key", return_value="secret"),
        ):
            unset_err = _http_error(
                400,
                "Bad Request",
                json.dumps({"detail": {"error": "Field name=enforce_fallback_model_access is not set"}}),
            )
            with patch.object(syncer, "request_json", side_effect=unset_err):
                info = syncer.get_general_setting("enforce_fallback_model_access")
            self.assertEqual(info["set"], False)
            self.assertEqual(info["editable"], True)

            for code, body in (
                (400, json.dumps({"detail": {"error": "Invalid field=enforce_fallback_model_access passed in."}})),
                (400, json.dumps({"detail": {"error": "You are not allowed to access this route"}})),
                (400, "Field name=enforce_fallback_model_access is not set (secret-leak-sentinel)"),
                (401, json.dumps({"detail": {"error": "Field name=enforce_fallback_model_access is not set"}})),
                (500, json.dumps({"detail": {"error": "Field name=enforce_fallback_model_access is not set"}})),
            ):
                with self.subTest(code=code, body=body):
                    err = _http_error(code, "Error", body)
                    with patch.object(syncer, "request_json", side_effect=err):
                        with self.assertRaises(CommandError) as ctx:
                            syncer.get_general_setting("enforce_fallback_model_access")
                    self.assertNotIn("secret-leak-sentinel", str(ctx.exception))

    def test_sync_general_settings_updates_unset_and_skips_matching_and_enforces_bool_type(self):
        api = FakeLiteLLMAPI()
        source = {"general_settings": {"enforce_fallback_model_access": False}}
        self.assertEqual(_run_sync(source, api, only="general_settings"), 0)
        self.assertEqual(
            [c for c in api.calls if c[0] == "POST"],
            [
                (
                    "POST",
                    "config/field/update",
                    {
                        "config_type": "general_settings",
                        "field_name": "enforce_fallback_model_access",
                        "field_value": False,
                    },
                )
            ],
        )

        # Already matching -> skip without POST
        api.calls.clear()
        self.assertEqual(_run_sync(source, api, only="general_settings"), 0)
        self.assertEqual([c for c in api.calls if c[0] == "POST"], [])

        # Live integer 0 != boolean False -> must update, not skip!
        api_int = FakeLiteLLMAPI(
            general_settings={
                "enforce_fallback_model_access": {"field_value": 0, "source": "db", "editable": True}
            }
        )
        self.assertEqual(_run_sync(source, api_int, only="general_settings"), 0)
        self.assertEqual(len([c for c in api_int.calls if c[0] == "POST"]), 1)

    def test_config_owned_general_setting_blocks_before_any_mutation(self):
        api = FakeLiteLLMAPI(
            general_settings={
                "enforce_fallback_model_access": {
                    "field_value": True,
                    "source": "config",
                    "editable": False,
                }
            }
        )
        source = {
            "general_settings": {"enforce_fallback_model_access": False},
            "access_groups": {"General": {"description": "should not be created"}},
        }
        with self.assertRaisesRegex(CommandError, "owned by config file"):
            _run_sync(source, api, only="general_settings,access_groups")
        self.assertEqual([c for c in api.calls if c[0] != "GET" and c[0] != "GET_FIELD"], [])

    def test_general_settings_readback_failure_stops_before_second_field(self):
        calls = []

        def fake_post(endpoint, payload):
            calls.append((endpoint, payload))
            return True, "{}"

        def fake_get(field_name):
            # First preflight reads say unset; post-update readback returns wrong value (True != False)
            if not calls:
                return {"set": False, "field_name": field_name, "field_value": None, "source": None, "editable": True}
            return {"set": True, "field_name": field_name, "field_value": True, "source": "db", "editable": True}

        with (
            patch.object(syncer, "post_request", side_effect=fake_post),
            patch.object(syncer, "get_general_setting", side_effect=fake_get),
        ):
            ok = syncer.sync_general_settings(
                {
                    "general_settings": {
                        "enforce_fallback_model_access": False,
                        "return_wildcard_routes": False,
                    }
                }
            )
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["field_name"], "enforce_fallback_model_access")


class TestAccessGroupsSyncAndPrune(unittest.TestCase):
    def _base_api(self, *, access_groups=()):
        return FakeLiteLLMAPI(
            models=[
                {"model_name": "a-1", "litellm_params": {"model": "anthropic/a-1"}, "model_info": {"id": "m-1"}},
                {"model_name": "codex-auto-review", "litellm_params": {"model": "openai/codex-auto-review"}, "model_info": {"id": "m-2"}},
            ],
            router={
                "model_group_alias": {
                    "claude-*": {"model": "a-1", "hidden": True},
                    "claude-opus-5-5": "a-1",
                }
            },
            access_groups=access_groups,
        )

    def test_omitted_access_groups_never_reads_or_prunes_live_groups(self):
        live = [_live_group("General", "ag-1", models=["claude-*"], assigned_key_ids=["k-1"])]
        api = self._base_api(access_groups=live)
        self.assertEqual(_run_sync({}, api, only="access_groups", prune=True), 0)
        self.assertEqual(api.calls, [])
        self.assertEqual(api.access_groups, live)

    def test_create_update_skip_preserves_live_key_and_team_assignments(self):
        existing = _live_group(
            "General",
            "ag-1",
            description="Old",
            models=["claude-*"],
            assigned_key_ids=["key-hash-1", "key-hash-2"],
            assigned_team_ids=["team-1"],
        )
        api = self._base_api(access_groups=[existing])
        source = {
            "access_groups": {
                "General": {
                    "description": "Default user access group",
                    "access_model_names": ["claude-*", "claude-opus-5-5", "codex-auto-review"],
                },
                "ReviewOnly": {
                    "description": "Review only",
                    "access_model_names": ["codex-auto-review"],
                },
            }
        }
        self.assertEqual(_run_sync(source, api, only="access_groups"), 0)

        put_calls = [c for c in api.calls if c[0] == "PUT"]
        post_calls = [c for c in api.calls if c[0] == "POST"]
        self.assertEqual(len(put_calls), 1)
        self.assertEqual(put_calls[0][1], "v1/access_group/ag-1")
        self.assertNotIn("assigned_key_ids", put_calls[0][2])
        self.assertNotIn("assigned_team_ids", put_calls[0][2])
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0][1], "v1/access_group")

        by_name = {g["access_group_name"]: g for g in api.access_groups}
        self.assertEqual(by_name["General"]["assigned_key_ids"], ["key-hash-1", "key-hash-2"])
        self.assertEqual(by_name["General"]["assigned_team_ids"], ["team-1"])
        self.assertEqual(
            by_name["General"]["access_model_names"],
            ["claude-*", "claude-opus-5-5", "codex-auto-review"],
        )

        # Second run without --force skips all mutations
        api.calls.clear()
        self.assertEqual(_run_sync(source, api, only="access_groups"), 0)
        self.assertEqual([c for c in api.calls if c[0] in ("POST", "PUT", "DELETE")], [])

    def test_update_readback_fails_closed_if_live_assignments_change(self):
        before = _live_group("General", "ag-1", models=["claude-*"], assigned_key_ids=["key-1"])
        wiped = _live_group("General", "ag-1", models=["claude-*", "codex-auto-review"], assigned_key_ids=[])
        second = {"access_group_name": "Second", "access_model_names": ["codex-auto-review"]}
        writes = []

        def fake_put(endpoint, payload):
            writes.append(("PUT", endpoint))
            return True, "{}"

        def fake_post(endpoint, payload):
            writes.append(("POST", endpoint))
            return True, "{}"

        with (
            patch.object(syncer, "get_all_access_groups", side_effect=[(True, [before]), (True, [wiped])]),
            patch.object(syncer, "put_request", side_effect=fake_put),
            patch.object(syncer, "post_request", side_effect=fake_post),
        ):
            ok = syncer.sync_access_groups(
                {
                    "access_groups": [
                        {"access_group_name": "General", "access_model_names": ["claude-*", "codex-auto-review"]},
                        second,
                    ]
                },
                existing_models={"a-1", "codex-auto-review"},
                current_aliases={"claude-*": {"model": "a-1", "hidden": True}},
            )
        self.assertFalse(ok)
        self.assertEqual(writes, [("PUT", "v1/access_group/ag-1")])

    def test_unresolved_model_reference_blocks_before_first_write(self):
        api = self._base_api()
        source = {"access_groups": {"General": {"access_model_names": ["nonexistent-model"]}}}
        with self.assertRaisesRegex(CommandError, "access_groups preflight"):
            _run_sync(source, api, only="access_groups")
        self.assertEqual([c for c in api.calls if c[0] != "GET"], [])

    def test_prune_refuses_entire_plan_if_any_stale_group_has_assignments(self):
        # Deletion-plan adversary: unassigned stale group appears FIRST, assigned stale group SECOND.
        # Must NOT delete the first stale group before rejecting the second.
        unassigned = _live_group("StaleEmpty", "ag-empty", models=["codex-auto-review"])
        assigned_key = _live_group(
            "StaleWithKey",
            "ag-key",
            models=["codex-auto-review"],
            assigned_key_ids=["key-hash-1"],
        )
        assigned_team = _live_group(
            "StaleWithTeam",
            "ag-team",
            models=["codex-auto-review"],
            assigned_team_ids=["team-1"],
        )
        for bad_group in (assigned_key, assigned_team):
            with self.subTest(bad_group=bad_group["access_group_name"]):
                api = self._base_api(access_groups=[unassigned, bad_group])
                with self.assertRaisesRegex(CommandError, "live assignments"):
                    _run_sync({"access_groups": {}}, api, only="access_groups", prune=True)
                self.assertEqual([c for c in api.calls if c[0] == "DELETE"], [])
                self.assertEqual(len(api.access_groups), 2)

    def test_prune_deletes_only_unassigned_stale_groups_with_per_delete_readback(self):
        keep = _live_group("General", "ag-keep", models=["codex-auto-review"], assigned_key_ids=["key-1"])
        stale1 = _live_group("Stale1", "ag-stale-1", models=["codex-auto-review"])
        stale2 = _live_group("Stale2", "ag-stale-2", models=["codex-auto-review"])
        api = self._base_api(access_groups=[keep, stale1, stale2])

        source = {
            "access_groups": {
                "General": {"access_model_names": ["codex-auto-review"]}
            }
        }
        self.assertEqual(_run_sync(source, api, only="access_groups", prune=True), 0)
        self.assertEqual(
            [c[1] for c in api.calls if c[0] == "DELETE"],
            ["v1/access_group/ag-stale-1", "v1/access_group/ag-stale-2"],
        )
        self.assertEqual([g["access_group_name"] for g in api.access_groups], ["General"])
        self.assertEqual(api.access_groups[0]["assigned_key_ids"], ["key-1"])

    def test_prune_stops_on_first_failed_delete_readback(self):
        keep = _live_group("General", "ag-keep", models=["codex-auto-review"])
        stale1 = _live_group("Stale1", "ag-stale-1", models=["codex-auto-review"])
        stale2 = _live_group("Stale2", "ag-stale-2", models=["codex-auto-review"])
        plan = ([stale1, stale2], {"General": {"access_group_name": "General", "access_model_names": ["codex-auto-review"]}}, [keep, stale1, stale2])

        with (
            patch.object(syncer, "delete_access_group", return_value=(True, "")) as delete,
            patch.object(syncer, "get_all_access_groups", return_value=(True, [keep, stale1, stale2])),
        ):
            ok = syncer.prune_access_groups(
                {"access_groups": [{"access_group_name": "General", "access_model_names": ["codex-auto-review"]}]},
                plan=plan,
            )
        self.assertFalse(ok)
        delete.assert_called_once_with("ag-stale-1")

    def test_malformed_access_group_inventory_blocks_before_first_mutation(self):
        for bad_inventory in (
            {"not": "a-list"},
            [{"access_group_name": "MissingId"}],
            [_live_group("Dup", "ag-1"), _live_group("Dup", "ag-2")],
            [_live_group("A", "ag-same"), _live_group("B", "ag-same")],
            [_live_group("BadList", "ag-1", extra={"assigned_key_ids": [123]})],
        ):
            with self.subTest(bad_inventory=bad_inventory):
                with (
                    patch.object(syncer, "get_request", return_value=(True, bad_inventory)),
                ):
                    ok, err = syncer.get_all_access_groups()
                self.assertFalse(ok)
                self.assertIsInstance(err, str)

    def test_cli_default_only_includes_general_settings_and_access_groups(self):
        import argparse

        parser = argparse.ArgumentParser()
        sync_command.configure(parser)
        args = parser.parse_args(["--preset", "default"])
        sections = set(args.only.split(","))
        self.assertIn("general_settings", sections)
        self.assertIn("access_groups", sections)

    def test_models_and_access_groups_sync_respects_prune_projected_surviving_models(self):
        desired = {
            "credentials": None,
            "models": [
                {
                    "model_name": "a-1",
                    "litellm_params": {"model": "anthropic/a-1"},
                    "model_info": {},
                }
            ],
            "aliases": None,
            "fallbacks": None,
            "public_model_hub": None,
            "router_settings": {},
            "general_settings": {},
            "guardrails": None,
            "access_groups": [
                {
                    "access_group_name": "General",
                    "access_model_names": ["a-1", "codex-auto-review"],
                }
            ],
        }
        # Non-pruning sync retains live codex-auto-review -> allowed
        api_keep = self._base_api()
        with (
            patch.dict(os.environ, {"LITELLM_API_KEY": "k", "LITELLM_BASE_URL": "http://invalid.test"}),
            patch.object(syncer, "load_dotenv"),
            patch.object(syncer, "generate_config_for_preset", return_value=copy.deepcopy(desired)),
            patch.object(syncer, "get_request", side_effect=api_keep.get),
            patch.object(syncer, "post_request", side_effect=api_keep.post),
            patch.object(syncer, "put_request", side_effect=api_keep.put),
            patch.object(syncer, "delete_request", side_effect=api_keep.delete),
            patch.object(syncer, "get_actor_from_key", return_value="actor"),
        ):
            self.assertEqual(
                asyncio.run(syncer.sync_config(preset="test", only="models,access_groups", prune=False)),
                0,
            )
        self.assertEqual([g["access_group_name"] for g in api_keep.access_groups], ["General"])

        # Pruning sync drops codex-auto-review -> blocked in access_groups preflight before any write
        api_prune = self._base_api()
        with (
            patch.dict(os.environ, {"LITELLM_API_KEY": "k", "LITELLM_BASE_URL": "http://invalid.test"}),
            patch.object(syncer, "load_dotenv"),
            patch.object(syncer, "generate_config_for_preset", return_value=copy.deepcopy(desired)),
            patch.object(syncer, "get_request", side_effect=api_prune.get),
            patch.object(syncer, "post_request", side_effect=api_prune.post),
            patch.object(syncer, "put_request", side_effect=api_prune.put),
            patch.object(syncer, "delete_request", side_effect=api_prune.delete),
            patch.object(syncer, "get_actor_from_key", return_value="actor"),
            self.assertRaisesRegex(CommandError, "access_groups preflight"),
        ):
            asyncio.run(syncer.sync_config(preset="test", only="models,access_groups", prune=True))
        self.assertEqual([c for c in api_prune.calls if c[0] != "GET"], [])


class TestGuardrailsThreeStateAndPrune(unittest.TestCase):
    @staticmethod
    def _live_guardrail(name: str, gid: str | None = None) -> dict:
        item = {
            "guardrail_name": name,
            "litellm_params": {"guardrail": "custom", "mode": "pre_call"},
        }
        if gid is not None:
            item["guardrail_id"] = gid
        return item

    def test_three_state_generation_and_round_trip_for_guardrails(self):
        cases = (
            ({}, None),
            ({"guardrails": None}, None),
            ({"guardrails": {}}, []),
            ({"guardrails": []}, []),
            (
                {
                    "guardrails": {
                        "keep": {"litellm_params": {"guardrail": "custom", "mode": "pre_call"}}
                    }
                },
                [
                    {
                        "guardrail_name": "keep",
                        "litellm_params": {"guardrail": "custom", "mode": "pre_call"},
                    }
                ],
            ),
        )
        for source, expected in cases:
            with self.subTest(source=source), patch.object(generator, "validate_prices"):
                generated = generator.generate_config(source, require_complete=True)
                self.assertEqual(generated["guardrails"], expected)
                self.assertEqual(generator.generate_config(generated, require_complete=True), generated)

    def test_omitted_guardrails_never_reads_or_prunes_live_guardrails(self):
        live = [self._live_guardrail("live-gr", "gr-1")]
        api = FakeLiteLLMAPI(guardrails=live)
        self.assertEqual(_run_sync({}, api, only="guardrails", prune=True), 0)
        self.assertEqual(api.calls, [])
        self.assertEqual(api.guardrails, live)

    def test_explicit_empty_guardrails_prunes_all_with_prune_and_noop_without_prune(self):
        live = [
            self._live_guardrail("stale-1", "gr-1"),
            self._live_guardrail("stale-2", "gr-2"),
        ]
        api_no_prune = FakeLiteLLMAPI(guardrails=live)
        self.assertEqual(_run_sync({"guardrails": {}}, api_no_prune, only="guardrails", prune=False), 0)
        self.assertEqual([c for c in api_no_prune.calls if c[0] == "DELETE"], [])
        self.assertEqual(len(api_no_prune.guardrails), 2)

        api_prune = FakeLiteLLMAPI(guardrails=live)
        self.assertEqual(_run_sync({"guardrails": {}}, api_prune, only="guardrails", prune=True), 0)
        self.assertEqual(
            [c[1] for c in api_prune.calls if c[0] == "DELETE"],
            ["guardrails/gr-1", "guardrails/gr-2"],
        )
        self.assertEqual(api_prune.guardrails, [])

    def test_guardrail_prune_retains_desired_and_encodes_delete_id(self):
        keep = self._live_guardrail("keep", "gr-keep")
        stale = self._live_guardrail("stale", "gr-stale")
        api = FakeLiteLLMAPI(guardrails=[keep, stale])
        source = {
            "guardrails": {
                "keep": {"litellm_params": {"guardrail": "custom", "mode": "pre_call"}}
            }
        }
        self.assertEqual(_run_sync(source, api, only="guardrails", prune=True), 0)
        self.assertEqual([c[1] for c in api.calls if c[0] == "DELETE"], ["guardrails/gr-stale"])
        self.assertEqual([g["guardrail_name"] for g in api.guardrails], ["keep"])

        with patch.object(syncer, "delete_request", return_value=(True, "")) as delete:
            syncer.delete_guardrail("id/../x?y#z")
        delete.assert_called_once_with("guardrails/id%2F%2E%2E%2Fx%3Fy%23z")

    def test_guardrail_prune_refuses_entire_plan_when_second_stale_missing_or_duplicate_id(self):
        # Deletion-plan adversary: valid stale item FIRST, broken stale item SECOND.
        valid_stale = self._live_guardrail("stale-valid", "gr-1")
        for second in (
            self._live_guardrail("stale-missing-id", None),
            self._live_guardrail("stale-dup-id", "gr-1"),
        ):
            with self.subTest(second=second["guardrail_name"]):
                api = FakeLiteLLMAPI(guardrails=[valid_stale, second])
                with self.assertRaisesRegex(CommandError, "guardrails prune preflight"):
                    _run_sync(
                        {
                            "general_settings": {"enforce_fallback_model_access": False},
                            "guardrails": {},
                        },
                        api,
                        only="general_settings,guardrails",
                        prune=True,
                    )
                self.assertEqual([c for c in api.calls if c[0] in ("POST", "PUT", "DELETE")], [])
                self.assertEqual(len(api.guardrails), 2)

    def test_guardrail_prune_stops_on_first_failed_delete_readback(self):
        keep = self._live_guardrail("keep", "gr-keep")
        stale1 = self._live_guardrail("stale-1", "gr-1")
        stale2 = self._live_guardrail("stale-2", "gr-2")
        desired_keep = {"guardrail_name": "keep", "litellm_params": {"guardrail": "custom", "mode": "pre_call"}}
        plan = ([stale1, stale2], {"keep": desired_keep}, [keep, stale1, stale2])

        with (
            patch.object(syncer, "delete_guardrail", return_value=(True, "")) as delete,
            patch.object(syncer, "get_all_guardrails", return_value=(True, [keep, stale1, stale2])),
        ):
            ok = syncer.prune_guardrails({"guardrails": [desired_keep]}, plan=plan)
        self.assertFalse(ok)
        delete.assert_called_once_with("gr-1")


if __name__ == "__main__":
    unittest.main()
