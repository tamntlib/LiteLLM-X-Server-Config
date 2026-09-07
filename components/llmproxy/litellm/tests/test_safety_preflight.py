"""Local-only regressions for complete intent and reference-safe deletion."""

import asyncio
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llmproxy.core.command import CommandError

from ..src import config_generate as generator
from ..src import config_sync as syncer


def model(name, ident):
    return {
        "model_name": name,
        "litellm_params": {"model": "openai/" + name},
        "model_info": {"id": ident},
    }


class ManagementAPI:
    def __init__(self, models=(), router=None):
        self.models = copy.deepcopy(list(models))
        self.router = copy.deepcopy(router or {})
        self.calls = []

    def get(self, endpoint):
        self.calls.append(("GET", endpoint))
        if endpoint.startswith("v2/model/info"):
            return True, {"data": copy.deepcopy(self.models)}
        if endpoint == "router/settings":
            return True, {"current_values": copy.deepcopy(self.router)}
        if endpoint in ("credentials", "public/model_hub"):
            return True, []
        raise AssertionError(("unexpected GET", endpoint))

    def post(self, endpoint, payload):
        self.calls.append(("POST", endpoint))
        if endpoint == "model/new":
            item = copy.deepcopy(payload)
            item.setdefault("model_info", {})["id"] = "new-id"
            self.models.append(item)
        elif endpoint == "model/delete":
            self.models = [m for m in self.models if m["model_info"]["id"] != payload["id"]]
        elif endpoint == "config/update":
            self.router = copy.deepcopy(payload["router_settings"])
        else:
            raise AssertionError(("unexpected POST", endpoint))
        return True, "{}"


def run_sync(source, api, **options):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "config.json"
        path.write_text(json.dumps(source))
        with (
            patch.dict(os.environ, {
                "LITELLM_API_KEY": "synthetic-master",
                "LITELLM_BASE_URL": "http://invalid.test",
            }),
            patch.object(syncer, "load_dotenv"),
            patch.object(generator, "validate_prices"),
            patch.object(generator, "fetch_models_from_api", side_effect=AssertionError("unexpected discovery")),
            patch.object(syncer, "get_request", side_effect=api.get),
            patch.object(syncer, "post_request", side_effect=api.post),
            patch.object(syncer, "put_request", side_effect=AssertionError("unexpected PUT")),
            patch.object(syncer, "patch_request", side_effect=AssertionError("unexpected PATCH")),
            patch.object(syncer, "delete_request", side_effect=AssertionError("unexpected DELETE")),
            patch.object(syncer, "get_actor_from_key", return_value="synthetic-reviewer"),
        ):
            return asyncio.run(syncer.sync_config(config_path=path, root=root, **options))


class ProviderInterfaceIntentTest(unittest.TestCase):
    def test_missing_interfaces_refuses_actual_sync_before_transport(self):
        incomplete = {
            "api_key": "synthetic-provider-key",
            "api_base": "http://invalid.test",
            "models": {"wanted-model": None},
        }
        for providers in (
            {"service": incomplete},
            {"child": {"$extend": "base"}, "base": incomplete},
            {"service": {}},
            {
                "child": {"$extend": "base", "$delete": ["interfaces"]},
                "base": {**incomplete, "interfaces": {"openai": {}}},
            },
        ):
            for only in ("models", "credentials", "models,credentials"):
                with self.subTest(providers=providers, only=only):
                    api = ManagementAPI([model("wanted-model", "live-id")])
                    with self.assertRaisesRegex(generator.ModelDiscoveryError, "interfaces"):
                        run_sync({"providers": providers}, api, only=only, prune=True)
                    self.assertEqual(api.calls, [])

    def test_complete_generation_accepts_inherited_interfaces(self):
        source = {"providers": {
            "child": {"$extend": "base"},
            "base": {
                "api_key": "synthetic-key",
                "models_autofill_disabled": True,
                "models": {"wanted": None},
                "interfaces": {"openai": {}},
            },
        }}
        for child in ({"$extend": "base"}, {"$extend": "base", "interfaces": {}}):
            with self.subTest(child=child), patch.object(generator, "validate_prices"):
                source["providers"]["child"] = child
                generated = generator.generate_config(source, require_complete=True)
                self.assertEqual({c["credential_name"] for c in generated["credentials"]}, {"base-openai", "child-openai"})
                self.assertEqual(len(generated["models"]), 2)

    def test_explicit_empty_intent_and_partial_sections_are_preserved(self):
        for source, models, credentials in (
            ({}, None, None),
            ({"providers": {}}, None, None),
            ({"providers": {"disabled": {"interfaces": {}}}}, [], []),
            ({"providers": {"child": {"$extend": "base"}, "base": {"interfaces": {}}}}, [], []),
            ({"models": []}, [], None),
            ({"credentials": []}, None, []),
            ({"models": [], "credentials": []}, [], []),
        ):
            with self.subTest(source=source), patch.object(generator, "validate_prices"):
                generated = generator.generate_config(source, require_complete=True)
                self.assertEqual(generated["models"], models)
                self.assertEqual(generated["credentials"], credentials)
                self.assertEqual(generator.generate_config(generated, require_complete=True), generated)

    def test_incomplete_public_template_still_supports_non_strict_generation(self):
        with patch.object(generator, "validate_prices"):
            generated = generator.generate_config({"providers": {"template": {"models": {"wanted": None}}}})
        self.assertEqual(generated["models"], [])


class FallbackReferenceSafetyTest(unittest.TestCase):
    def test_malformed_live_fallbacks_block_first_post_in_prune_and_force(self):
        malformed = {
            field: [{}, "backup", [None], [{}], [{"primary": "backup"}], [{"": ["backup"]}], [{"primary": [None]}], [{"primary": [""]}]]
            for field in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks")
        }
        malformed["default_fallbacks"] = [{}, "backup", [None], [""], [{"primary": ["backup"]}]]
        for field, values in malformed.items():
            for value in values:
                for options in ({"prune": True}, {"force": True}):
                    with self.subTest(field=field, value=value, options=options):
                        api = ManagementAPI([model("primary", "primary-id")], {field: value})
                        with self.assertRaisesRegex(CommandError, field):
                            run_sync({"models": [model("primary", "primary-id"), model("new", "new-id")]}, api, only="models", **options)
                        self.assertFalse([call for call in api.calls if call[0] != "GET"])

    def test_malformed_desired_raw_fallbacks_block_before_transport(self):
        for field in ("context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
            for value in ({}, "backup", [None], [""], [{"primary": "backup"}]):
                with self.subTest(field=field, value=value):
                    api = ManagementAPI()
                    with self.assertRaisesRegex(generator.ModelDiscoveryError, field):
                        run_sync({"models": [model("new", "new-id")], "router_settings": {field: value}}, api, only="models,router_settings")
                    self.assertEqual(api.calls, [])

    def test_all_live_fallback_classes_protect_sources_and_targets_before_delete(self):
        desired = {"models": [model("primary", "primary-id")]}
        for field in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
            values = [["backup"]] if field == "default_fallbacks" else [
                [{"primary": ["backup"]}],
                [{"backup": ["primary"]}],
                [{"*": ["backup"]}],
                [{"primary": [], "backup": []}],
            ]
            for value in values:
                with self.subTest(field=field, value=value):
                    api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")], {field: value})
                    with self.assertRaisesRegex(CommandError, "model prune preflight"):
                        run_sync(desired, api, only="models", prune=True)
                    self.assertFalse([call for call in api.calls if call[0] != "GET"])
                    self.assertEqual(len(api.models), 2)

    def test_valid_nullable_and_empty_live_fallbacks_allow_unreferenced_prune(self):
        for field in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
            for value in (None, [], ["primary"] if field == "default_fallbacks" else [{"*": ["primary"]}]):
                with self.subTest(field=field, value=value):
                    api = ManagementAPI([model("primary", "primary-id"), model("stale", "stale-id")], {field: value})
                    self.assertEqual(run_sync({"models": [model("primary", "primary-id")]}, api, only="models", prune=True), 0)
                    self.assertEqual(api.models, [model("primary", "primary-id")])
                    self.assertEqual(api.router, {field: value})

    def test_selected_raw_clear_is_applied_before_prune_but_unselected_clear_is_not(self):
        for field in ("context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
            for only in ("models", "models,router_settings"):
                with self.subTest(field=field, only=only):
                    value = ["backup"] if field == "default_fallbacks" else [{"primary": ["backup"]}]
                    api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")], {field: value})
                    source = {"models": [model("primary", "primary-id")], "router_settings": {field: []}}
                    if only == "models":
                        with self.assertRaisesRegex(CommandError, "model prune preflight"):
                            run_sync(source, api, only=only, prune=True)
                        self.assertFalse([call for call in api.calls if call[0] != "GET"])
                    else:
                        self.assertEqual(run_sync(source, api, only=only, prune=True), 0)
                        self.assertEqual(api.router[field], [])
                        self.assertEqual(api.models, [model("primary", "primary-id")])
                        self.assertLess(api.calls.index(("POST", "config/update")), api.calls.index(("POST", "model/delete")))

    def test_omitted_models_stay_unmanaged_with_valid_raw_fallback_update(self):
        for field in ("context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
            with self.subTest(field=field):
                value = ["primary"] if field == "default_fallbacks" else [{"*": ["primary"]}]
                api = ManagementAPI([model("primary", "primary-id")])
                self.assertEqual(run_sync({"router_settings": {field: value}}, api, only="models,router_settings", prune=True), 0)
                self.assertEqual(api.models, [model("primary", "primary-id")])
                self.assertEqual(api.router[field], value)
                self.assertNotIn(("POST", "model/delete"), api.calls)

    def test_valid_references_allow_forced_same_name_replacement(self):
        for field in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
            with self.subTest(field=field):
                value = ["primary"] if field == "default_fallbacks" else [{"*": ["primary"]}]
                api = ManagementAPI([model("primary", "primary-id")], {field: value})
                desired = {"model_name": "primary", "litellm_params": {"model": "openai/primary"}}
                self.assertEqual(run_sync({"models": [desired]}, api, only="models", force=True), 0)
                self.assertEqual([m["model_info"]["id"] for m in api.models], ["new-id"])
                self.assertLess(api.calls.index(("GET", "router/settings")), api.calls.index(("POST", "model/new")))
                self.assertEqual(api.router[field], value)


class ProjectedRoutingGraphTest(unittest.TestCase):
    fields = ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks")

    @staticmethod
    def rules(field, target, source="primary"):
        return [target] if field == "default_fallbacks" else [{source: [target]}]

    def test_source_sentinel_is_supported_for_every_rule_class(self):
        for field in self.fields:
            with self.subTest(field=field):
                api = ManagementAPI([model("primary", "primary-id")])
                source: dict = {"models": [model("primary", "primary-id")]}
                rules = self.rules(field, "primary", "*")
                if field == "fallbacks":
                    source["fallbacks"] = rules
                else:
                    source["router_settings"] = {field: rules}
                self.assertEqual(run_sync(source, api, only="models,fallbacks,router_settings", prune=True), 0)
                self.assertEqual(api.router[field], rules)
                self.assertLess(next(i for i, (_, endpoint) in enumerate(api.calls) if endpoint.startswith("v2/model/info")), api.calls.index(("POST", "config/update")))

    def test_destructive_planning_rechecks_fresh_graph_before_delete(self):
        for field in self.fields:
            for options in ({"prune": True}, {"force": True}):
                for aliases in ({}, {"hop": "next", "next": "hop"}, {"hop": "missing"}):
                    with self.subTest(field=field, options=options, aliases=aliases):
                        class DriftingAPI(ManagementAPI):
                            router_reads = 0

                            def get(inner, endpoint):
                                if endpoint == "router/settings":
                                    inner.router_reads += 1
                                    if inner.router_reads == 2:
                                        inner.router = {"model_group_alias": aliases, field: self.rules(field, "hop")}
                                return super().get(endpoint)

                        api = DriftingAPI([model("primary", "primary-id"), model("stale", "stale-id")])
                        desired = {"models": [{"model_name": "primary", "litellm_params": {"model": "openai/primary"}}]}
                        with self.assertRaisesRegex(CommandError, "model prune preflight"):
                            run_sync(desired, api, only="models", **options)
                        self.assertNotIn(("POST", "model/delete"), api.calls)
                        mutations = [call for call in api.calls if call[0] != "GET"]
                        self.assertEqual(mutations, [("POST", "model/new")] if options.get("force") else [])

    def test_unknown_raw_references_refuse_before_first_mutation(self):
        for field in self.fields[1:]:
            for options in ({"prune": True}, {"force": True}):
                for mode in ("managed", "omitted", "unselected"):
                    with self.subTest(field=field, options=options, mode=mode):
                        api = ManagementAPI([model("primary", "primary-id")])
                        source: dict = {"router_settings": {field: self.rules(field, "missing-target")}}
                        if mode != "omitted":
                            source["models"] = [{"model_name": "primary", "litellm_params": {"model": "openai/primary"}}]
                        only = "router_settings" if mode == "unselected" else "models,router_settings"
                        with self.assertRaisesRegex(CommandError, "routing.*missing-target"):
                            run_sync(source, api, only=only, **options)
                        self.assertTrue(any(endpoint.startswith("v2/model/info") for _, endpoint in api.calls))
                        self.assertFalse([call for call in api.calls if call[0] != "GET"])

    def test_alias_clear_cannot_orphan_any_preserved_fallback(self):
        for field in self.fields:
            for options in ({"prune": True}, {"force": True}):
                for only in ("models,aliases", "aliases"):
                    with self.subTest(field=field, options=options, only=only):
                        router = {"model_group_alias": {"backup-alias": "backup"}, field: self.rules(field, "backup-alias")}
                        api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")], router)
                        source = {"models": [{"model_name": "primary", "litellm_params": {"model": "openai/primary"}}], "aliases": {}}
                        with self.assertRaisesRegex(CommandError, "routing.*backup-alias"):
                            run_sync(source, api, only=only, **options)
                        self.assertFalse([call for call in api.calls if call[0] != "GET"])
                        self.assertEqual(api.router, router)
                        self.assertEqual(len(api.models), 2)

    def test_unknown_sources_and_target_sentinels_block_all_mutations(self):
        for field in self.fields:
            invalid = [("primary", "*"), ("primary", "missing-target")]
            if field != "default_fallbacks":
                invalid += [("missing-source", "primary"), ("primary*", "primary")]
            for source_name, target in invalid:
                for options in ({"prune": True}, {"force": True}):
                    with self.subTest(field=field, source=source_name, target=target, options=options):
                        api = ManagementAPI([model("primary", "primary-id")])
                        source: dict = {"models": [{"model_name": "primary", "litellm_params": {"model": "openai/primary"}}]}
                        rules = self.rules(field, target, source_name)
                        source.update({"fallbacks": rules} if field == "fallbacks" else {"router_settings": {field: rules}})
                        with self.assertRaises((CommandError, generator.ModelDiscoveryError)):
                            run_sync(source, api, only="models,fallbacks,router_settings", **options)
                        self.assertFalse([call for call in api.calls if call[0] != "GET"])

    def test_live_alias_cycles_and_unresolved_chains_block_raw_updates(self):
        for field in self.fields[1:]:
            for aliases in ({"hop": "next", "next": "hop"}, {"hop": "next", "next": "missing"}):
                for options in ({"prune": True}, {"force": True}):
                    with self.subTest(field=field, aliases=aliases, options=options):
                        api = ManagementAPI([model("primary", "primary-id")], {"model_group_alias": aliases})
                        source = {"router_settings": {field: self.rules(field, "hop")}}
                        with self.assertRaisesRegex(CommandError, "routing.*(cycle|unresolved)"):
                            run_sync(source, api, only="router_settings", **options)
                        self.assertFalse([call for call in api.calls if call[0] != "GET"])

    def test_untrustworthy_unmanaged_model_inventory_blocks_routing_updates(self):
        for section in ("aliases", "router_settings"):
            for response in ((False, "unavailable"), (True, {}), (True, {"data": [None]})):
                with self.subTest(section=section, response=response):
                    class InvalidInventoryAPI(ManagementAPI):
                        def get(inner, endpoint):
                            if endpoint.startswith("v2/model/info"):
                                inner.calls.append(("GET", endpoint))
                                return response
                            return super().get(endpoint)

                    api = InvalidInventoryAPI()
                    source = {"aliases": {}, "router_settings": {"default_fallbacks": []}}
                    with self.assertRaises(CommandError):
                        run_sync(source, api, only=section)
                    self.assertFalse([call for call in api.calls if call[0] != "GET"])

    def test_coordinated_selected_clears_preserve_other_fields_then_prune(self):
        for field in self.fields:
            for clear in ([[]] if field == "fallbacks" else [[], None]):
                with self.subTest(field=field, clear=clear):
                    router = {"model_group_alias": {"hop": "next", "next": "backup"}, field: self.rules(field, "hop"), "timeout": 42}
                    api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")], router)
                    source: dict = {"models": [model("primary", "primary-id")], "aliases": {}}
                    source.update({"fallbacks": clear} if field == "fallbacks" else {"router_settings": {field: clear}})
                    self.assertEqual(run_sync(source, api, only="models,aliases,fallbacks,router_settings", prune=True), 0)
                    self.assertEqual(api.router, {"model_group_alias": {}, field: clear, "timeout": 42})
                    self.assertEqual(api.models, [model("primary", "primary-id")])
                    mutations = [call for call in api.calls if call[0] != "GET"]
                    self.assertEqual(mutations, [("POST", "config/update"), ("POST", "config/update"), ("POST", "model/delete")])
                    for index, call in enumerate(api.calls):
                        if call == ("POST", "config/update"):
                            self.assertEqual(api.calls[index + 1], ("GET", "router/settings"))

    def test_unselected_or_nullable_dedicated_clear_cannot_authorize_alias_removal(self):
        for field in self.fields:
            cases = [("models,aliases", [])]
            if field == "fallbacks":
                cases.append(("models,aliases,fallbacks", None))
            for only, clear in cases:
                with self.subTest(field=field, only=only, clear=clear):
                    api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")], {"model_group_alias": {"hop": "backup"}, field: self.rules(field, "hop")})
                    source: dict = {"models": [model("primary", "primary-id")], "aliases": {}}
                    source.update({"fallbacks": clear} if field == "fallbacks" else {"router_settings": {field: clear}})
                    with self.assertRaisesRegex(CommandError, "routing.*hop"):
                        run_sync(source, api, only=only, prune=True)
                    self.assertFalse([call for call in api.calls if call[0] != "GET"])

    def test_alias_chain_retarget_preserves_fallbacks_and_allows_backend_prune(self):
        for field in self.fields:
            with self.subTest(field=field):
                rules = self.rules(field, "hop")
                api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")], {"model_group_alias": {"hop": "old-hop", "old-hop": "backup"}, field: rules})
                desired = {"models": [model("primary", "primary-id")], "aliases": {"hop": "new-hop", "new-hop": "primary"}}
                self.assertEqual(run_sync(desired, api, only="models,aliases", prune=True), 0)
                self.assertEqual(api.router, {"model_group_alias": desired["aliases"], field: rules})
                self.assertEqual(api.models, desired["models"])
                self.assertLess(api.calls.index(("POST", "config/update")), api.calls.index(("POST", "model/delete")))

    def test_alias_chain_removal_or_dropped_source_fails_before_writes(self):
        for field in self.fields:
            for rules in ([self.rules(field, "hop")] if field == "default_fallbacks" else [self.rules(field, "hop"), self.rules(field, "primary", "hop")]):
                for options in ({"prune": True}, {"force": True}):
                    with self.subTest(field=field, rules=rules, options=options):
                        api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")], {"model_group_alias": {"hop": "next", "next": "backup"}, field: rules})
                        desired = {"models": [{"model_name": "primary", "litellm_params": {"model": "openai/primary"}}], "aliases": {"next": "primary"}}
                        with self.assertRaisesRegex(CommandError, "routing.*hop"):
                            run_sync(desired, api, only="models,aliases", **options)
                        self.assertFalse([call for call in api.calls if call[0] != "GET"])

    def test_valid_unmanaged_chain_and_same_name_force_replacement_survive(self):
        for field in self.fields:
            for options in ({}, {"force": True}, {"prune": True}):
                with self.subTest(field=field, options=options):
                    router = {"model_group_alias": {"hop": "next", "next": "primary"}, field: self.rules(field, "hop")}
                    api = ManagementAPI([model("primary", "primary-id"), model("stale", "stale-id")], router)
                    source = {"models": [{"model_name": "primary", "litellm_params": {"model": "openai/primary"}}], "aliases": None, "fallbacks": None}
                    self.assertEqual(run_sync(source, api, only="models,aliases,fallbacks", **options), 0)
                    self.assertEqual(api.router, router)
                    self.assertEqual(sum(m["model_name"] == "primary" for m in api.models), 1)
                    if options.get("force"):
                        self.assertEqual(next(m["model_info"]["id"] for m in api.models if m["model_name"] == "primary"), "new-id")
                    self.assertEqual(any(m["model_name"] == "stale" for m in api.models), not options.get("prune", False))

    def test_valid_raw_update_uses_live_not_unselected_desired_models(self):
        for field in self.fields[1:]:
            for only in ("router_settings", "models,router_settings"):
                with self.subTest(field=field, only=only):
                    api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")], {"model_group_alias": {"hop": "next", "next": "backup"}})
                    source: dict = {"router_settings": {field: self.rules(field, "hop")}}
                    if only == "router_settings":
                        source["models"] = []
                    self.assertEqual(run_sync(source, api, only=only, prune=True), 0)
                    self.assertEqual(len(api.models), 2)
                    self.assertEqual(api.router[field], self.rules(field, "hop"))
                    self.assertEqual([call for call in api.calls if call[0] != "GET"], [("POST", "config/update")])

    def test_dedicated_desired_references_remain_strict_despite_live_targets(self):
        for section, value in (("aliases", {"hop": "backup"}), ("fallbacks", [{"primary": ["backup"]}]), ("public_model_hub", ["backup"])):
            with self.subTest(section=section):
                api = ManagementAPI([model("primary", "primary-id"), model("backup", "backup-id")])
                source = {"models": [model("primary", "primary-id")], section: value}
                with self.assertRaisesRegex(generator.ModelDiscoveryError, "unresolved"):
                    run_sync(source, api, only=section)
                self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()