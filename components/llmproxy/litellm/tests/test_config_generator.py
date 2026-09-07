import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ..src import config_generate as generator
from ..src.config_generate import (
    _resolve_alias_group,
    _fetch_anthropic_models,
    _fetch_gemini_models,
    _fetch_openai_models,
    ModelDiscoveryError,
    deep_merge,
    expand_interface_vars,
    generate_config,
    generate_config_for_preset,
    load_config_for_preset,
    resolve_provider_extensions,
    resolve_provider_models,
    validate_prices,
)


class RepositoryConfigTest(unittest.TestCase):
    def test_default_config_and_output_are_component_owned(self):
        owner = generator.REPO_ROOT / "components" / "llmproxy" / "litellm"
        self.assertEqual(generator.DEFAULT_CONFIG_FILE, owner / "configs" / "config.json")
        self.assertEqual(Path(generator.__file__), owner / "src" / "config_generate.py")
        self.assertEqual(
            generator.DEFAULT_OUTPUT_FILE,
            Path("build/llmproxy/litellm/config.gen.json"),
        )

    def test_explicit_root_loads_its_component_owned_base_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base = root / "components" / "llmproxy" / "litellm" / "configs" / "config.json"
            base.parent.mkdir(parents=True)
            base.write_text('{"providers": {}, "router_settings": {"timeout": 42}}\n')
            (root / "presets").mkdir()
            (root / "presets" / "empty.toml").write_text("components = []\n")
            config, public = load_config_for_preset("empty", root=root, include_local=False)

        self.assertEqual(config, {"providers": {}, "router_settings": {"timeout": 42}})
        self.assertEqual(public, config)

    def test_tier_deployments_do_not_override_max_input_tokens(self):
        config, _ = load_config_for_preset("llmproxy", include_local=False)
        config["providers"]["cli-proxy-api"]["api_key"] = "dummy"

        models, _ = resolve_provider_models(
            config["providers"],
            config["model_name_base_model_map"],
        )

        models_with_max_input_tokens = {
            model["model_name"]: model["model_info"]["max_input_tokens"]
            for model in models
            if "max_input_tokens" in model["model_info"]
        }
        self.assertEqual(models_with_max_input_tokens, {})

    def test_legacy_direct_gpt_models_and_mini_alias_are_removed(self):
        config, _ = load_config_for_preset("llmproxy", include_local=False)
        config["providers"]["cli-proxy-api"]["api_key"] = "dummy"

        models, _ = resolve_provider_models(
            config["providers"],
            config["model_name_base_model_map"],
        )
        model_names = {model["model_name"] for model in models}

        for interface in ("anthropic", "openai"):
            self.assertNotIn(f"{interface}/gpt-5.5", model_names)
            self.assertNotIn(f"{interface}/gpt-5.4", model_names)
            self.assertNotIn(f"{interface}/gpt-5.4-mini", model_names)
        self.assertNotIn("gpt-*-mini", config["aliases"])


class PresetConfigTest(unittest.TestCase):
    def test_selected_layer_confinement_never_consults_excluded_or_disabled_layers(self):
        cases = (
            ("config.json", True, True, True),
            ("config.local.json", True, True, True),
            ("config.local.json", False, True, False),
            ("config.json", True, False, False),
            ("config.local.json", True, False, False),
        )
        for filename, include_local, selected, reject in cases:
            with self.subTest(filename=filename, include_local=include_local, selected=selected):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    base = root / "config.json"
                    base.write_text('{"providers": {}}\n')
                    outside = root / "outside.json"
                    outside.write_text('{"router_settings": {"timeout": 999}}\n')
                    for name in ("selected", "excluded"):
                        directory = root / "components" / "app" / name
                        directory.mkdir(parents=True)
                        (directory / "compose.yaml").write_text(
                            f"services:\n  {name}:\n    image: example/app\n"
                        )
                    target = root / "components" / "app" / ("selected" if selected else "excluded")
                    (target / "integrations" / "llmproxy" / "litellm").mkdir(parents=True)
                    (target / "integrations" / "llmproxy" / "litellm" / filename).symlink_to(outside)
                    (root / "presets").mkdir()
                    (root / "presets" / "selected.toml").write_text('components = ["app/selected"]\n')
                    with patch.object(generator, "load_json", wraps=generator.load_json) as read_json:
                        if reject:
                            with self.assertRaisesRegex(ModelDiscoveryError, "symlink"):
                                load_config_for_preset("selected", config_path=base, root=root, include_local=include_local)
                        else:
                            config, public = load_config_for_preset("selected", config_path=base, root=root, include_local=include_local)
                            self.assertEqual(config, {"providers": {}})
                            self.assertEqual(public, config)
                    self.assertNotIn(outside, [call.args[0] for call in read_json.call_args_list])

    def test_layer_conventions_need_only_generic_component_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base = root / "config.json"
            base.write_text('{"providers": {}}\n')
            component = root / "selected"
            component.mkdir()
            (component / "integrations" / "llmproxy" / "litellm").mkdir(parents=True)
            (component / "integrations" / "llmproxy" / "litellm" / "config.json").write_text(
                '{"providers": {"api": {"api_base": "http://public"}}}\n'
            )
            (component / "integrations" / "llmproxy" / "litellm" / "config.local.json").write_text(
                '{"providers": {"api": {"api_key": "local"}}}\n'
            )
            with (
                patch.object(generator, "resolve_preset", return_value=SimpleNamespace(components=("app/api",))),
                patch.object(generator, "discover_components", return_value={"app/api": SimpleNamespace(directory=component)}),
            ):
                config, public = load_config_for_preset("selected", config_path=base, root=root)
        self.assertEqual(public, {"providers": {"api": {"api_base": "http://public"}}})
        self.assertEqual(config, {"providers": {"api": {"api_base": "http://public", "api_key": "local"}}})

    def test_default_excludes_the_headroom_config_layer(self):
        all_config, _ = load_config_for_preset("all", include_local=False)
        without_headroom, _ = load_config_for_preset(
            "default", include_local=False
        )

        self.assertIn("headroom-compression", all_config["guardrails"])
        self.assertNotIn("headroom-compression", without_headroom.get("guardrails", {}))
        expected = dict(all_config)
        expected.pop("guardrails")
        actual = dict(without_headroom)
        actual.pop("guardrails", None)
        self.assertEqual(actual, expected)

    def test_component_local_layer_is_merged_only_when_component_is_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base = root / "components" / "llmproxy" / "litellm" / "configs" / "config.json"
            base.parent.mkdir(parents=True)
            base.write_text('{"providers": {}}\n')
            component = root / "components" / "app" / "api"
            component.mkdir(parents=True)
            (component / "compose.yaml").write_text(
                "services:\n  api:\n    image: example/api\n"
            )
            (component / "integrations" / "llmproxy" / "litellm").mkdir(parents=True)
            (component / "integrations" / "llmproxy" / "litellm" / "config.json").write_text(
                '{"providers": {"api": {"api_base": "http://public"}}}\n'
            )
            (component / "integrations" / "llmproxy" / "litellm" / "config.local.json").write_text(
                '{"providers": {"api": {"api_key": "local"}}}\n'
            )
            (root / "presets").mkdir()
            (root / "presets" / "empty.toml").write_text("components = []\n")
            (root / "presets" / "selected.toml").write_text(
                'components = ["app/api"]\n'
            )
            (root / "presets" / "excluded.toml").write_text(
                'extends = "selected"\nexclude_components = ["app/api"]\n'
            )

            with patch(
                f"{generator.__name__}.REPO_ROOT",
                root,
            ):
                empty, _ = load_config_for_preset("empty", config_path=base)
                selected, _ = load_config_for_preset("selected", config_path=base)
                excluded, _ = load_config_for_preset("excluded", config_path=base)

        self.assertEqual(empty, {"providers": {}})
        self.assertEqual(excluded, {"providers": {}})
        self.assertEqual(
            selected["providers"]["api"],
            {"api_base": "http://public", "api_key": "local"},
        )


class IntegrationConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "custom.json"
        self.base.write_text('{"router_settings": {"timeout": 1}}\n')
        self.sources = {}
        for name in ("first", "second", "excluded"):
            directory = self.root / "components" / "app" / name
            directory.mkdir(parents=True)
            (directory / "compose.yaml").write_text(f"services:\n  {name}:\n    image: example\n")
            self.sources[name] = directory
        (self.root / "presets").mkdir()
        (self.root / "presets" / "selected.toml").write_text(
            'components = ["app/first", "app/second"]\n'
        )

    def layer(self, source, filename, content, target="llmproxy/litellm"):
        path = self.sources[source] / "integrations" / target / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content))
        return path

    def load(self, include_local=True):
        return load_config_for_preset(
            "selected", config_path=self.base, root=self.root, include_local=include_local,
        )

    def test_all_public_layers_precede_all_local_layers_in_preset_order(self):
        first_public = self.layer("first", "config.json", {"router_settings": {"timeout": 2, "public": "first"}})
        second_public = self.layer("second", "config.json", {"router_settings": {"timeout": 3, "public": "second"}})
        first_local = self.layer("first", "config.local.json", {"router_settings": {"timeout": 4, "local": "first"}})
        second_local = self.layer("second", "config.local.json", {"router_settings": {"local": "second"}})
        with patch.object(generator, "load_json", wraps=generator.load_json) as read_json:
            config, public = self.load()
        self.assertEqual(public, {"router_settings": {"timeout": 3, "public": "second"}})
        self.assertEqual(config, {"router_settings": {"timeout": 4, "public": "second", "local": "second"}})
        self.assertEqual([call.args[0] for call in read_json.call_args_list],
                         [self.base, first_public, second_public, first_local, second_local])
        self.assertEqual(self.load(include_local=False), (public, public))

    def test_destination_is_not_required_in_preset_or_discovered_components(self):
        self.layer("first", "config.json", {"router_settings": {"timeout": 42}})
        config, public = self.load()
        self.assertEqual(config, {"router_settings": {"timeout": 42}})
        self.assertEqual(config, public)
        self.assertFalse((self.root / "components" / "llmproxy" / "litellm").exists())

    def test_other_destination_namespaces_are_not_inspected(self):
        expected = {"router_settings": {"timeout": 42}}
        self.layer("first", "config.json", expected)
        for target in ("other/litellm", "llmproxy/other"):
            for filename in ("config.json", "config.local.json"):
                other = self.layer("first", filename, {"router_settings": {"timeout": 999}}, target)
                self.assertEqual(self.load(), (expected, expected))
                other.write_text("not valid JSON")
                self.assertEqual(self.load(), (expected, expected))
                other.unlink()
                other.symlink_to(self.root / "absent")
                self.assertEqual(self.load(), (expected, expected))

    def test_malformed_unselected_and_disabled_layers_are_not_inspected(self):
        selected = self.layer("first", "config.json", {"router_settings": {"timeout": 42}})
        for filename in ("config.json", "config.local.json"):
            self.layer("excluded", filename, {}).write_text("malformed excluded JSON")
        self.layer("first", "config.local.json", {}).write_text("malformed disabled JSON")
        with (
            patch.object(generator, "resolve_integration_file", wraps=generator.resolve_integration_file) as lookup,
            patch.object(generator, "load_json", wraps=generator.load_json) as read_json,
        ):
            config, public = self.load(include_local=False)
        self.assertEqual(config, {"router_settings": {"timeout": 42}})
        self.assertEqual(config, public)
        self.assertEqual([call.args for call in lookup.call_args_list], [
            (self.sources["first"], "llmproxy/litellm", "config.json"),
            (self.sources["second"], "llmproxy/litellm", "config.json"),
        ])
        self.assertEqual([call.args[0] for call in read_json.call_args_list], [self.base, selected])
        integration_root = self.sources["excluded"] / "integrations"
        integration_root.rename(self.root / "excluded-integrations")
        integration_root.symlink_to(self.root / "absent")
        self.assertEqual(self.load(include_local=False), (config, public))

    def test_custom_base_does_not_gain_an_implicit_preset_local_layer(self):
        self.base.with_name("custom.local.json").write_text("not a preset contribution")
        self.assertEqual(self.load(), ({"router_settings": {"timeout": 1}},) * 2)

    def test_direct_custom_config_keeps_its_sibling_local_behavior(self):
        self.base.with_name("custom.local.json").write_text('{"router_settings": {"timeout": 77}}')
        config, public = generator.load_config_with_local(self.base)
        self.assertEqual(public, {"router_settings": {"timeout": 1}})
        self.assertEqual(config, {"router_settings": {"timeout": 77}})

    def test_integration_filesystem_errors_are_wrapped_with_their_cause(self):
        lookup = generator.resolve_integration_file
        for filename in ("config.json", "config.local.json"):
            for error in (PermissionError("access denied"), ValueError("invalid integration")):
                with self.subTest(filename=filename, error=type(error).__name__):
                    def fail(directory, target, leaf):
                        if leaf == filename:
                            raise error
                        return lookup(directory, target, leaf)
                    with patch.object(generator, "resolve_integration_file", side_effect=fail):
                        with self.assertRaises(ModelDiscoveryError) as caught:
                            self.load()
                    self.assertIs(caught.exception.__cause__, error)
                    self.assertEqual(str(caught.exception), str(error))

    def test_legacy_filenames_are_ignored_without_compatibility_fallback(self):
        for filename in ("litellm-config.json", "litellm-config.local.json"):
            (self.sources["first"] / filename).write_text('{"router_settings": {"timeout": 999}}')
        self.assertEqual(self.load(), ({"router_settings": {"timeout": 1}},) * 2)
        for filename in ("litellm-config.json", "litellm-config.local.json"):
            (self.sources["first"] / filename).write_text("invalid legacy JSON")
        self.assertEqual(self.load(), ({"router_settings": {"timeout": 1}},) * 2)


class ProviderDiscoverySafetyTest(unittest.TestCase):
    def test_complete_generation_rejects_reserved_router_sections(self):
        for reserved, value in (("model_group_alias", {}), ("fallbacks", [])):
            with self.subTest(reserved=reserved):
                with patch(f"{generator.__name__}.validate_prices"):
                    with self.assertRaisesRegex(ModelDiscoveryError, "reserved"):
                        generate_config(
                            {
                                "providers": {},
                                "router_settings": {reserved: value},
                            },
                            require_complete=True,
                        )

    def test_missing_replaceable_sections_remain_unspecified(self):
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config({}, require_complete=True)
        self.assertIsNone(generated["credentials"])
        self.assertIsNone(generated["models"])
        self.assertIsNone(generated["aliases"])
        self.assertIsNone(generated["fallbacks"])
        self.assertIsNone(generated["public_model_hub"])

    def test_resolved_credentials_round_trip_through_generated_config(self):
        credential = {
            "credential_name": "credential",
            "credential_values": {"api_key": "secret"},
            "credential_info": {"description": "round-trip"},
        }
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config(
                {"credentials": [credential], "models": []},
                require_complete=True,
            )
        self.assertEqual(generated["credentials"], [credential])
        self.assertEqual(generated["models"], [])

    def test_complete_generation_rejects_unmatched_models_alias_ref(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "matched no models"):
                generate_config(
                    {
                        "providers": {},
                        "aliases": {"$models:missing/openai": ""},
                    },
                    require_complete=True,
                )

    def test_complete_generation_rejects_missing_base_fallback_rule(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "no matching base rule"):
                generate_config(
                    {
                        "providers": {},
                        "fallbacks": [{"source": ["$base"]}],
                    },
                    base_config={"providers": {}, "fallbacks": []},
                    require_complete=True,
                )

    def test_complete_generation_rejects_alias_cycle(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "alias cycle"):
                generate_config(
                    {
                        "providers": {},
                        "aliases": {"first": "second", "second": "first"},
                    },
                    require_complete=True,
                )

    def test_complete_generation_rejects_non_list_manual_models(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "models.*list"):
                generate_config(
                    {"providers": {}, "models": {"bad": "shape"}},
                    require_complete=True,
                )

    def test_complete_generation_rejects_malformed_guardrails(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "guardrails.*invalid entry"):
                generate_config(
                    {"providers": {}, "guardrails": ["invalid"]},
                    require_complete=True,
                )

        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "Guardrail 'bad'.*object"):
                generate_config(
                    {"providers": {}, "guardrails": {"bad": "shape"}},
                    require_complete=True,
                )

    def test_complete_generation_rejects_malformed_nested_provider_maps(self):
        malformed = (
            {"svc": "bad"},
            {"svc": {"api_key": "key", "interfaces": {"openai": "bad"}}},
            {"svc": {"api_key": "key", "interfaces": {"openai": {}}, "models": []}},
            {
                "svc": {
                    "api_key": "key",
                    "interfaces": {"openai": {"models": {"model": "bad"}}},
                }
            },
            {
                "svc": {
                    "api_key": "key",
                    "models_autofill_disabled": True,
                    "interfaces": {
                        "openai": {
                            "models": {"model": {"model_names": []}}
                        }
                    },
                }
            },
            {
                "svc": {
                    "api_key": "key",
                    "models_autofill_disabled": True,
                    "interfaces": {
                        "openai": {
                            "models": {"model": {"model_info": []}}
                        }
                    },
                }
            },
            {
                "svc": {
                    "api_key": "key",
                    "models_autofill_disabled": True,
                    "interfaces": {
                        "openai": {
                            "models": {
                                "model": {
                                    "model_names": {
                                        "alias": {"model_info": []}
                                    }
                                }
                            }
                        }
                    },
                }
            },
        )
        for providers in malformed:
            with self.subTest(providers=providers):
                with patch(f"{generator.__name__}.validate_prices"):
                    with self.assertRaises(ModelDiscoveryError):
                        generate_config(
                            {"providers": providers},
                            require_complete=True,
                        )

    def test_complete_generation_rejects_malformed_model_alias_groups(self):
        malformed = (
            {"alias": "bad"},
            {"alias": {"$extends": ["base", 1]}},
            {"alias": {"target": "bad"}},
            {"alias": {"target": {"litellm_params": []}}},
        )
        for model_aliases in malformed:
            with self.subTest(model_aliases=model_aliases):
                with patch(f"{generator.__name__}.validate_prices"):
                    with self.assertRaises(ModelDiscoveryError):
                        generate_config(
                            {"providers": {}, "model_aliases": model_aliases},
                            require_complete=True,
                        )

    def test_empty_model_set_does_not_fetch_remote_pricing(self):
        with patch(f"{generator.__name__}.request_json") as request:
            validate_prices([])
        request.assert_not_called()
    def test_complete_generation_rejects_unresolved_routing_references(self):
        config = {
            "aliases": {"broken": "missing-model"},
            "fallbacks": [{"missing-source": ["missing-target"]}],
            "public_model_hub": ["missing-public-model"],
        }

        with self.assertRaisesRegex(
            ModelDiscoveryError,
            "unresolved alias, fallback, or public model hub",
        ):
            generate_config(config, require_complete=True)
    def test_complete_generation_requires_discovery_endpoint_when_autofill_enabled(self):
        config = {
            "providers": {
                "svc": {
                    "api_key": "key",
                    "interfaces": {"openai": {"models": {"explicit": None}}},
                }
            }
        }
        with self.assertRaisesRegex(ModelDiscoveryError, "no models API endpoint"):
            generate_config(config, require_complete=True)

    def test_complete_generation_rejects_empty_discovery_even_with_explicit_models(self):
        config = {
            "providers": {
                "svc": {
                    "api_key": "key",
                    "api_base": "http://provider.test",
                    "interfaces": {"openai": {"models": {"explicit": None}}},
                }
            }
        }
        with (
            patch(f"{generator.__name__}.fetch_models_from_api", return_value=[]),
            self.assertRaisesRegex(ModelDiscoveryError, "returned no models"),
        ):
            generate_config(config, require_complete=True)

    def test_provider_inventory_rejects_malformed_entries(self):
        cases = (
            (_fetch_openai_models, {"data": [{}]}),
            (_fetch_gemini_models, {"models": [{}]}),
            (_fetch_anthropic_models, {"data": [{}], "has_more": False}),
        )
        for fetcher, response in cases:
            with self.subTest(fetcher=fetcher.__name__):
                with patch(f"{generator.__name__}.request_json", return_value=response):
                    with self.assertRaisesRegex(ModelDiscoveryError, "Malformed model inventory"):
                        fetcher("http://provider.test", "key")

    def test_provider_inventory_rejects_duplicate_model_ids(self):
        cases = (
            (_fetch_openai_models, {"data": [{"id": "same"}, {"id": "same"}]}),
            (_fetch_gemini_models, {"models": [{"name": "same"}, {"name": "same"}]}),
            (
                _fetch_anthropic_models,
                {"data": [{"id": "same"}, {"id": "same"}], "has_more": False},
            ),
        )
        for fetcher, response in cases:
            with self.subTest(fetcher=fetcher.__name__):
                with patch(f"{generator.__name__}.request_json", return_value=response):
                    with self.assertRaisesRegex(ModelDiscoveryError, "duplicate IDs"):
                        fetcher("http://provider.test", "key")

    def test_complete_generation_rejects_null_provider_api_base(self):
        config = {
            "providers": {
                "svc": {
                    "api_base": None,
                    "api_key": "key",
                    "interfaces": {"openai": {"models": {"model": None}}},
                }
            }
        }
        with self.assertRaisesRegex(ModelDiscoveryError, "api_base must be a string"):
            generate_config(config, require_complete=True)

    def test_complete_generation_rejects_duplicate_model_identities(self):
        model = {
            "model_name": "duplicate",
            "litellm_params": {
                "model": "openai/model",
                "litellm_credential_name": "credential",
            },
            "model_info": {},
        }
        with self.assertRaisesRegex(ModelDiscoveryError, "duplicate model identities"):
            generate_config(
                {"providers": {}, "models": [model, dict(model)]},
                require_complete=True,
            )

    def test_complete_generation_requires_manual_model_target(self):
        model = {
            "model_name": "manual",
            "litellm_params": {},
            "model_info": {},
        }
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "litellm_params.model"):
                generate_config(
                    {"providers": {}, "models": [model]},
                    require_complete=True,
                )

    def test_complete_generation_requires_guardrail_and_mode(self):
        invalid = (
            {
                "guardrail_name": "missing-guardrail",
                "litellm_params": {"mode": "pre_call"},
            },
            {
                "guardrail_name": "missing-mode",
                "litellm_params": {"guardrail": "headroom"},
            },
        )
        for guardrail in invalid:
            with self.subTest(guardrail=guardrail):
                with patch(f"{generator.__name__}.validate_prices"):
                    with self.assertRaisesRegex(ModelDiscoveryError, "guardrail.*mode"):
                        generate_config(
                            {"providers": {}, "guardrails": [guardrail]},
                            require_complete=True,
                        )

    def test_complete_generation_accepts_nonempty_guardrail_mode_list(self):
        guardrail = {
            "guardrail_name": "multi-mode",
            "litellm_params": {
                "guardrail": "provider",
                "mode": ["pre_call", "post_call"],
            },
        }
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config(
                {"providers": {}, "guardrails": [guardrail]},
                require_complete=True,
            )
        self.assertEqual(generated["guardrails"], [guardrail])

    def test_complete_generation_rejects_empty_alias_identifier(self):
        model = {
            "model_name": "manual",
            "litellm_params": {"model": "openai/manual"},
            "model_info": {},
        }
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "aliases.*non-empty"):
                generate_config(
                    {
                        "providers": {},
                        "models": [model],
                        "aliases": {"": "manual"},
                    },
                    require_complete=True,
                )

    def test_complete_generation_rejects_duplicate_guardrail_names(self):
        first = {
            "guardrail_name": "duplicate",
            "litellm_params": {"guardrail": "first", "mode": "pre_call"},
        }
        second = {
            "guardrail_name": "duplicate",
            "litellm_params": {"guardrail": "second", "mode": "pre_call"},
        }
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "duplicate guardrail"):
                generate_config(
                    {"providers": {}, "guardrails": [first, second]},
                    require_complete=True,
                )

    def test_anthropic_inventory_requires_cursor_when_has_more(self):
        with patch(
            f"{generator.__name__}.request_json",
            return_value={"data": [{"id": "model"}], "has_more": True},
        ):
            with self.assertRaisesRegex(ModelDiscoveryError, "missing last_id"):
                _fetch_anthropic_models("http://provider.test", "key")

    def test_anthropic_pagination_cursor_is_query_encoded(self):
        with patch(
            f"{generator.__name__}.request_json",
            side_effect=[
                {
                    "data": [{"id": "first"}],
                    "has_more": True,
                    "last_id": "cursor&limit=999#frag",
                },
                {"data": [{"id": "second"}], "has_more": False},
            ],
        ) as request:
            self.assertEqual(
                _fetch_anthropic_models("http://provider.test", "key"),
                ["first", "second"],
            )
        self.assertIn(
            "after_id=cursor%26limit%3D999%23frag",
            request.call_args_list[1].args[0],
        )

    def test_complete_generation_rejects_unknown_provider_interface(self):
        config = {
            "providers": {
                "svc": {
                    "api_key": "key",
                    "interfaces": {
                        "unknown": {"models": {"model": None}},
                    },
                }
            }
        }
        with self.assertRaisesRegex(ModelDiscoveryError, "Unknown provider interface"):
            generate_config(config, require_complete=True)

    def test_complete_generation_rejects_broken_model_alias_extension(self):
        config = {
            "providers": {},
            "model_aliases": {
                "alias": {
                    "$extends": "missing",
                }
            },
        }
        with self.assertRaisesRegex(ModelDiscoveryError, "unknown alias group"):
            generate_config(config, require_complete=True)

    def test_provider_extension_chain_is_order_independent(self):
        providers = {
            "leaf": {"$extend": "middle", "access_groups": ["leaf"]},
            "middle": {"$extend": "base", "api_base": "http://middle"},
            "base": {"api_key": "key", "interfaces": {"openai": {}}},
        }
        resolved = resolve_provider_extensions(providers)
        self.assertEqual(resolved["leaf"]["api_key"], "key")
        self.assertEqual(resolved["leaf"]["api_base"], "http://middle")
        self.assertEqual(resolved["leaf"]["access_groups"], ["leaf"])

    def test_missing_provider_extension_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "non-existent provider"):
            resolve_provider_extensions({"leaf": {"$extend": "missing"}})

    def test_provider_extension_cycle_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Cyclic provider inheritance"):
            resolve_provider_extensions(
                {"a": {"$extend": "b"}, "b": {"$extend": "a"}}
            )

    def test_prune_safe_generation_rejects_missing_provider_api_key(self):
        config = {
            "providers": {
                "svc": {
                    "interfaces": {
                        "openai": {
                            "models": {"model": None},
                        }
                    }
                }
            }
        }
        with self.assertRaisesRegex(ModelDiscoveryError, "missing api_key"):
            generate_config(config, require_complete=True)

    def test_prune_safe_generation_rejects_empty_autodiscovery(self):
        config = {
            "providers": {
                "svc": {
                    "api_key": "test-key",
                    "api_base": "http://provider.test",
                    "interfaces": {"openai": {}},
                }
            }
        }
        with (
            patch(
                f"{generator.__name__}.fetch_models_from_api",
                return_value=[],
            ),
            self.assertRaisesRegex(ModelDiscoveryError, "returned no models"),
        ):
            generate_config(config, require_complete=True)

    def test_network_failure_is_not_converted_to_empty_model_inventory(self):
        with patch(
            f"{generator.__name__}.request_json",
            side_effect=urllib.error.URLError("unavailable"),
        ):
            with self.assertRaisesRegex(ModelDiscoveryError, "Failed to fetch models"):
                _fetch_openai_models("http://provider", "key")

    def test_malformed_response_is_not_converted_to_empty_model_inventory(self):
        with patch(
            f"{generator.__name__}.request_json",
            return_value={"unexpected": []},
        ):
            with self.assertRaisesRegex(ModelDiscoveryError, "Malformed model inventory"):
                _fetch_openai_models("http://provider", "key")

    def test_litellm_only_has_no_optional_provider_or_guardrail(self):
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config_for_preset("litellm-only", include_local=False)

        self.assertIsNone(generated["credentials"])
        self.assertIsNone(generated["models"])
        self.assertEqual(generated["guardrails"], [])

    def test_llmproxy_preserves_cli_proxy_models_and_headroom_guardrail(self):
        with patch(f"{generator.__name__}.validate_prices"):
            config, base_config = load_config_for_preset("llmproxy", include_local=False)
            config["providers"]["cli-proxy-api"]["api_key"] = "dummy"
            generated = generate_config(config, base_config=base_config)

        self.assertTrue(
            any(
                item["credential_name"].startswith("cli-proxy-api-")
                for item in generated["credentials"]
            )
        )
        self.assertEqual(
            {item["guardrail_name"] for item in generated["guardrails"]},
            {"headroom-compression"},
        )

    def test_all_uses_same_litellm_layers_as_llmproxy(self):
        llmproxy, _ = load_config_for_preset("llmproxy", include_local=False)
        all_config, _ = load_config_for_preset("all", include_local=False)
        self.assertEqual(llmproxy, all_config)


class DeepMergeTest(unittest.TestCase):
    def test_delete_keyword_removes_keys_recursively_and_never_leaks(self):
        merged = deep_merge(
            {
                "keep": 1,
                "remove_top": 2,
                "nested": {
                    "keep": 3,
                    "remove_nested": 4,
                },
            },
            {
                "$delete": ["remove_top", "missing", "same_merge", 123],
                "nested": {
                    "$delete": ["remove_nested"],
                },
                "new_nested": {
                    "$delete": ["never_existed"],
                },
                "same_merge": "re-added before deletion pass",
            },
        )

        self.assertEqual(
            merged,
            {"keep": 1, "nested": {"keep": 3}, "new_nested": {}},
        )


class ResolveProviderModelsTest(unittest.TestCase):
    def test_provider_default_model_is_deep_merged_before_model_overrides(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "models_autofill_disabled": True,
                "default_model": {
                    "model_info": {
                        "max_input_tokens": 272000,
                        "supports_vision": True,
                    },
                    "litellm_params": {
                        "timeout": 600,
                    },
                },
                "models": {
                    "gpt-5.5": {
                        "model_info": {
                            "max_output_tokens": 32000,
                        },
                        "model_names": {
                            "$self": {},
                            "primary": {
                                "model_info": {
                                    "max_input_tokens": 128000,
                                },
                            },
                        },
                    }
                },
                "interfaces": {
                    "openai": {},
                },
            }
        }

        models, _ = resolve_provider_models(providers, {})

        by_name = {model["model_name"]: model for model in models}
        self.assertEqual(
            by_name["openai/gpt-5.5"]["model_info"]["max_input_tokens"],
            272000,
        )
        self.assertEqual(
            by_name["openai/gpt-5.5"]["model_info"]["max_output_tokens"],
            32000,
        )
        self.assertTrue(
            by_name["openai/gpt-5.5"]["model_info"]["supports_vision"]
        )
        self.assertEqual(
            by_name["openai/gpt-5.5"]["litellm_params"]["timeout"],
            600,
        )
        self.assertEqual(
            by_name["openai/primary"]["model_info"]["max_input_tokens"],
            128000,
        )
        self.assertEqual(
            by_name["openai/primary"]["model_info"]["max_output_tokens"],
            32000,
        )

    def test_delete_keyword_removes_inherited_default(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "models_autofill_disabled": True,
                "default_model": {
                    "model_info": {
                        "max_input_tokens": 272000,
                        "supports_vision": True,
                    }
                },
                "models": {
                    "gpt-image-2": {
                        "model_info": {
                            "max_output_tokens": 32000,
                        }
                    }
                },
                "interfaces": {
                    "openai": {
                        "models": {
                            "gpt-image-2": {
                                "model_info": {
                                    "$delete": ["max_input_tokens"],
                                }
                            }
                        }
                    },
                },
            }
        }

        models, _ = resolve_provider_models(providers, {})

        self.assertEqual(len(models), 1)
        self.assertNotIn("max_input_tokens", models[0]["model_info"])
        self.assertEqual(models[0]["model_info"]["max_output_tokens"], 32000)
        self.assertTrue(models[0]["model_info"]["supports_vision"])

    def test_interface_model_overrides_provider_model_and_default_model(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "models_autofill_disabled": True,
                "default_model": {
                    "model_info": {
                        "max_input_tokens": 272000,
                        "supports_vision": True,
                    }
                },
                "models": {
                    "gpt-5.5": {
                        "model_info": {
                            "max_output_tokens": 32000,
                        }
                    }
                },
                "interfaces": {
                    "anthropic": {},
                    "openai": {
                        "models": {
                            "gpt-5.5": {
                                "model_info": {
                                    "max_input_tokens": 128000,
                                }
                            }
                        }
                    },
                },
            }
        }

        models, _ = resolve_provider_models(providers, {})

        by_name = {model["model_name"]: model for model in models}
        self.assertEqual(
            by_name["anthropic/gpt-5.5"]["model_info"]["max_input_tokens"],
            272000,
        )
        self.assertEqual(
            by_name["openai/gpt-5.5"]["model_info"]["max_input_tokens"],
            128000,
        )
        self.assertEqual(
            by_name["openai/gpt-5.5"]["model_info"]["max_output_tokens"],
            32000,
        )
        self.assertTrue(
            by_name["openai/gpt-5.5"]["model_info"]["supports_vision"]
        )

    def test_auto_discovered_models_inherit_provider_default_model(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "api_base": "https://example.test",
                "default_model": {
                    "model_info": {
                        "max_input_tokens": 272000,
                    }
                },
                "interfaces": {
                    "openai": {},
                },
            }
        }

        with patch(f"{generator.__name__}.fetch_models_from_api", return_value=["discovered"]):
            models, _ = resolve_provider_models(providers, {})

        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["model_name"], "openai/discovered")
        self.assertEqual(models[0]["model_info"]["max_input_tokens"], 272000)

    def test_generated_model_alias_inherits_provider_default_model(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "default_model": {
                        "model_info": {
                            "max_input_tokens": 272000,
                        }
                    },
                    "models": {
                        "gpt-5.5": {},
                    },
                    "interfaces": {
                        "openai": {},
                    },
                }
            },
            "model_aliases": {
                "primary": {
                    "openai/gpt-5.5": {},
                }
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        alias_model = next(
            model for model in generated["models"]
            if model["model_name"] == "primary"
        )
        self.assertEqual(alias_model["model_info"]["max_input_tokens"], 272000)

    def test_model_names_object_uses_self_and_per_name_access_groups(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "access_groups": ["Provider"],
                "models_autofill_disabled": True,
                "models": {
                    "gpt-5.5": {
                        "access_groups": ["General1"],
                        "model_names": {
                            "$self": {},
                            "primary": {
                                "access_groups": ["General"],
                            },
                        },
                    }
                },
                "interfaces": {
                    "openai": {},
                },
            }
        }

        models, _ = resolve_provider_models(providers, {})

        by_name = {model["model_name"]: model for model in models}
        self.assertEqual(
            by_name["openai/gpt-5.5"]["model_info"]["access_groups"],
            ["General1"],
        )
        self.assertEqual(
            by_name["openai/primary"]["model_info"]["access_groups"],
            ["General"],
        )

    def test_model_names_string_keeps_existing_comma_behavior(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "access_groups": ["General"],
                "models_autofill_disabled": True,
                "models": {
                    "gpt-5.5": {
                        "model_names": ",primary",
                    }
                },
                "interfaces": {
                    "openai": {},
                },
            }
        }

        models, _ = resolve_provider_models(providers, {})

        by_name = {model["model_name"]: model for model in models}
        self.assertEqual(
            by_name["openai/gpt-5.5"]["model_info"]["access_groups"],
            ["General"],
        )
        self.assertEqual(
            by_name["openai/primary"]["model_info"]["access_groups"],
            ["General"],
        )

    def test_model_name_prefix_override_on_model_level(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "models_autofill_disabled": True,
                "models": {
                    "gpt-5.5": {
                        "model_name_prefix": "",
                        "model_names": {
                            "$self": {},
                            "primary": {},
                        },
                    }
                },
                "interfaces": {
                    "openai": {},
                },
            }
        }

        models, _ = resolve_provider_models(providers, {})

        by_name = {model["model_name"]: model for model in models}
        self.assertIn("gpt-5.5", by_name)
        self.assertIn("primary", by_name)
        # Model-level prefix "" should override interface default "openai/"
        self.assertNotIn("openai/gpt-5.5", by_name)
        self.assertNotIn("openai/primary", by_name)

    def test_per_entry_model_name_prefix_override(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "models_autofill_disabled": True,
                "models": {
                    "gpt-5.5": {
                        "model_names": {
                            "$self": {},
                            "claude-opus-*": {
                                "model_name_prefix": "",
                            },
                            "primary": {},
                        },
                    }
                },
                "interfaces": {
                    "openai": {},
                },
            }
        }

        models, _ = resolve_provider_models(providers, {})

        by_name = {model["model_name"]: model for model in models}
        # $self uses interface default prefix
        self.assertIn("openai/gpt-5.5", by_name)
        # per-entry prefix "" overrides interface default
        self.assertIn("claude-opus-*", by_name)
        self.assertNotIn("openai/claude-opus-*", by_name)
        # primary uses interface default prefix
        self.assertIn("openai/primary", by_name)

    def test_model_name_prefix_not_leaked_into_model_info(self):
        providers = {
            "svc": {
                "api_key": "dummy",
                "models_autofill_disabled": True,
                "models": {
                    "gpt-5.5": {
                        "model_name_prefix": "",
                        "model_names": {
                            "primary": {
                                "model_name_prefix": "custom/",
                                "access_groups": ["A"],
                            },
                        },
                    }
                },
                "interfaces": {
                    "openai": {},
                },
            }
        }

        models, _ = resolve_provider_models(providers, {})

        by_name = {model["model_name"]: model for model in models}
        # "custom/" prefix used
        self.assertIn("custom/primary", by_name)
        # model_name_prefix should not leak into model_info or litellm_params
        model = by_name["custom/primary"]
        self.assertNotIn("model_name_prefix", model.get("model_info", {}))
        self.assertNotIn("model_name_prefix", model.get("litellm_params", {}))

    def test_generate_config_expands_root_model_aliases(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "cli-proxy-api": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {
                        "gpt-5.5": {
                            "access_groups": ["General"],
                            "model_names": {
                                "$self": {
                                    "access_groups": []
                                }
                            },
                        }
                    },
                    "interfaces": {
                        "anthropic": {},
                    },
                },
                "example-provider": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "interfaces": {
                        "anthropic": {
                            "models": {
                                "example-model-pro": {
                                    "access_groups": ["General"],
                                    "model_names": {
                                        "$self": {
                                            "access_groups": []
                                        }
                                    },
                                }
                            }
                        }
                    },
                },
            },
            "model_name_base_model_map": {
                "example-model-pro": "openrouter/example/example-model-pro"
            },
            "model_aliases": {
                "claude-opus-*": {
                    "anthropic/gpt-5.5": {"access_groups": []},
                    "anthropic/example-model-pro": {"access_groups": []},
                }
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))

            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        alias_models = [
            model
            for model in generated["models"]
            if model["model_name"] == "claude-opus-*"
        ]
        by_target = {
            model["litellm_params"]["model"]: model
            for model in alias_models
        }

        self.assertEqual(
            set(by_target),
            {
                "anthropic/gpt-5.5",
                "anthropic/example-model-pro",
            },
        )
        self.assertEqual(
            by_target["anthropic/gpt-5.5"]["litellm_params"]["litellm_credential_name"],
            "cli-proxy-api-anthropic",
        )
        self.assertEqual(
            by_target["anthropic/example-model-pro"]["litellm_params"]["litellm_credential_name"],
            "example-provider-anthropic",
        )
        self.assertEqual(
            by_target["anthropic/gpt-5.5"]["model_info"],
            {
                "base_model": "gpt-5.5",
                "access_groups": [],
            },
        )
        self.assertEqual(
            by_target["anthropic/example-model-pro"]["model_info"],
            {
                "base_model": "openrouter/example/example-model-pro",
                "access_groups": [],
            },
        )

    def test_generate_config_does_not_treat_glob_alias_as_any_matching_model(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {
                        "gpt-5.5": {},
                    },
                    "interfaces": {
                        "openai": {},
                    },
                },
            },
            "model_aliases": {
                "gpt-*": {
                    "openai/gpt-5.5": {
                        "access_groups": ["General"],
                    }
                },
            },
            "fallbacks": [
                {
                    "gpt-5.5": [
                        "gpt-4o",
                    ]
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))

            with patch(f"{generator.__name__}.validate_prices"), patch(f"{generator.__name__}.logger.warning") as warn:
                generate_config(config_path)

        warning_messages = [call.args[0] for call in warn.call_args_list]
        self.assertTrue(
            any("is not a known model or alias" in message for message in warning_messages),
            warning_messages,
        )

    def _base_config(self):
        return {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {"gpt-5.5": {}},
                    "interfaces": {"openai": {}},
                }
            },
        }

    def test_generate_config_missing_aliases_returns_none(self):
        config = self._base_config()
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertIsNone(generated["aliases"])

    def test_generate_config_null_aliases_returns_none(self):
        config = self._base_config()
        config["aliases"] = None
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertIsNone(generated["aliases"])

    def test_generate_config_empty_aliases_returns_empty_dict(self):
        config = self._base_config()
        config["aliases"] = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertEqual(generated["aliases"], {})

    def test_generate_config_preserves_top_level_manual_models(self):
        config = self._base_config()
        config["models"] = [
            {
                "model_name": "manual-router",
                "litellm_params": {
                    "model": "auto_router/complexity_router",
                    "complexity_router_default_model": "gpt-*",
                },
                "model_info": {
                    "access_groups": ["General"],
                },
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        manual_models = [
            model for model in generated["models"]
            if model["model_name"] == "manual-router"
        ]
        self.assertEqual(
            manual_models,
            [
                {
                    "model_name": "manual-router",
                    "litellm_params": {
                        "model": "auto_router/complexity_router",
                        "complexity_router_default_model": "gpt-*",
                    },
                    "model_info": {
                        "access_groups": ["General"],
                    },
                }
            ],
        )

    def test_generate_config_preserves_auto_router_with_claude_wildcard_tiers(self):
        config = self._base_config()
        config["model_aliases"] = {
            "claude-opus-*": {
                "openai/gpt-5.5": {"access_groups": ["General"]},
            },
            "claude-sonnet-*": {
                "openai/gpt-5.5": {"access_groups": ["General"]},
            },
        }
        config["models"] = [
            {
                "model_name": "auto",
                "litellm_params": {
                    "model": "auto_router/complexity_router",
                    "complexity_router_config": {
                        "tiers": {
                            "SIMPLE": "claude-sonnet-*",
                            "MEDIUM": "claude-sonnet-*",
                            "COMPLEX": "claude-opus-*",
                            "REASONING": "claude-opus-*",
                        }
                    },
                    "complexity_router_default_model": "claude-sonnet-*",
                },
                "model_info": {
                    "access_groups": ["General"],
                },
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        auto_model = next(
            model for model in generated["models"]
            if model["model_name"] == "auto"
        )
        self.assertEqual(
            auto_model["litellm_params"],
            {
                "model": "auto_router/complexity_router",
                "complexity_router_config": {
                    "tiers": {
                        "SIMPLE": "claude-sonnet-*",
                        "MEDIUM": "claude-sonnet-*",
                        "COMPLEX": "claude-opus-*",
                        "REASONING": "claude-opus-*",
                    }
                },
                "complexity_router_default_model": "claude-sonnet-*",
            },
        )
        self.assertEqual(
            auto_model["model_info"],
            {"access_groups": ["General"]},
        )
        self.assertNotIn("litellm_credential_name", auto_model["litellm_params"])


class ResolveAliasGroupTest(unittest.TestCase):
    def test_extends_string_inherits_targets(self):
        model_aliases = {
            "anthropic/primary": {
                "anthropic/gpt-5.5": {"access_groups": ["General"]},
            },
            "claude-opus-*": {
                "$extends": "anthropic/primary",
                "anthropic/example-model-pro": {"access_groups": ["General"]},
            },
        }
        resolved = _resolve_alias_group(model_aliases, "claude-opus-*")
        self.assertEqual(
            set(resolved),
            {"anthropic/gpt-5.5", "anthropic/example-model-pro"},
        )

    def test_extends_array_inherits_from_multiple(self):
        model_aliases = {
            "anthropic/primary": {
                "anthropic/gpt-5.5": {"access_groups": ["General"]},
            },
            "anthropic/vision": {
                "anthropic/example-model": {"access_groups": ["Vision"]},
            },
            "claude-opus-*": {
                "$extends": ["anthropic/primary", "anthropic/vision"],
            },
        }
        resolved = _resolve_alias_group(model_aliases, "claude-opus-*")
        self.assertEqual(
            set(resolved),
            {"anthropic/gpt-5.5", "anthropic/example-model"},
        )

    def test_extends_local_overrides_inherited(self):
        model_aliases = {
            "anthropic/primary": {
                "anthropic/gpt-5.5": {"access_groups": ["General"]},
            },
            "claude-opus-*": {
                "$extends": "anthropic/primary",
                "anthropic/gpt-5.5": {"access_groups": ["VIP"]},
            },
        }
        resolved = _resolve_alias_group(model_aliases, "claude-opus-*")
        self.assertEqual(
            resolved["anthropic/gpt-5.5"],
            {"access_groups": ["VIP"]},
        )

    def test_extends_recursive(self):
        model_aliases = {
            "base": {
                "anthropic/model-a": {},
            },
            "mid": {
                "$extends": "base",
                "anthropic/model-b": {},
            },
            "top": {
                "$extends": "mid",
                "anthropic/model-c": {},
            },
        }
        resolved = _resolve_alias_group(model_aliases, "top")
        self.assertEqual(
            set(resolved),
            {"anthropic/model-a", "anthropic/model-b", "anthropic/model-c"},
        )

    def test_extends_circular_does_not_loop(self):
        model_aliases = {
            "a": {
                "$extends": "b",
                "anthropic/model-a": {},
            },
            "b": {
                "$extends": "a",
                "anthropic/model-b": {},
            },
        }
        with patch(f"{generator.__name__}.logger.warning") as warn:
            resolved = _resolve_alias_group(model_aliases, "a")
        self.assertIn("anthropic/model-a", resolved)
        warning_messages = [call.args[0] for call in warn.call_args_list]
        self.assertTrue(any("Circular" in msg for msg in warning_messages))

    def test_extends_unknown_ref_warns(self):
        model_aliases = {
            "claude-opus-*": {
                "$extends": "nonexistent",
                "anthropic/gpt-5.5": {},
            },
        }
        with patch(f"{generator.__name__}.logger.warning") as warn:
            resolved = _resolve_alias_group(model_aliases, "claude-opus-*")
        self.assertEqual(set(resolved), {"anthropic/gpt-5.5"})
        warning_messages = [call.args[0] for call in warn.call_args_list]
        self.assertTrue(any("unknown alias group" in msg for msg in warning_messages))

    def test_no_extends_returns_targets_as_is(self):
        model_aliases = {
            "anthropic/primary": {
                "anthropic/gpt-5.5": {"access_groups": ["General"]},
            },
        }
        resolved = _resolve_alias_group(model_aliases, "anthropic/primary")
        self.assertEqual(resolved, {"anthropic/gpt-5.5": {"access_groups": ["General"]}})


class ExtendsIntegrationTest(unittest.TestCase):
    def test_generate_config_extends_string(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {
                        "gpt-5.5": {"access_groups": ["General"]},
                        "example-model-pro": {"access_groups": ["General"]},
                    },
                    "interfaces": {"anthropic": {}},
                },
            },
            "model_aliases": {
                "anthropic/primary": {
                    "anthropic/gpt-5.5": {"access_groups": ["General"]},
                },
                "claude-opus-*": {
                    "$extends": "anthropic/primary",
                    "anthropic/example-model-pro": {"access_groups": ["General"]},
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        alias_models = [
            m for m in generated["models"] if m["model_name"] == "claude-opus-*"
        ]
        targets = {m["litellm_params"]["model"] for m in alias_models}
        self.assertEqual(targets, {"anthropic/gpt-5.5", "anthropic/example-model-pro"})

    def test_generate_config_extends_array(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {
                        "gpt-5.5": {},
                        "example-model": {},
                        "example-model-pro": {},
                    },
                    "interfaces": {"anthropic": {}},
                },
            },
            "model_aliases": {
                "anthropic/primary": {
                    "anthropic/gpt-5.5": {},
                },
                "anthropic/vision": {
                    "anthropic/example-model": {},
                },
                "claude-opus-*": {
                    "$extends": ["anthropic/primary", "anthropic/vision"],
                    "anthropic/example-model-pro": {},
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        alias_models = [
            m for m in generated["models"] if m["model_name"] == "claude-opus-*"
        ]
        targets = {m["litellm_params"]["model"] for m in alias_models}
        self.assertEqual(
            targets,
            {"anthropic/gpt-5.5", "anthropic/example-model", "anthropic/example-model-pro"},
        )


class ExpandInterfaceVarsTest(unittest.TestCase):
    def test_expands_key_across_interfaces(self):
        model_aliases = {
            "$interface/secondary": {
                "$interface/qwen3.6-plus": {"access_groups": ["General"]},
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic", "openai"})
        self.assertIn("anthropic/secondary", result)
        self.assertIn("openai/secondary", result)
        self.assertNotIn("$interface/secondary", result)

    def test_replaces_interface_in_target_keys(self):
        model_aliases = {
            "$interface/secondary": {
                "$interface/qwen3.6-plus": {"access_groups": ["General"]},
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic", "openai"})
        self.assertEqual(
            set(result["anthropic/secondary"]),
            {"anthropic/qwen3.6-plus"},
        )
        self.assertEqual(
            set(result["openai/secondary"]),
            {"openai/qwen3.6-plus"},
        )

    def test_preserves_non_interface_targets(self):
        model_aliases = {
            "$interface/secondary": {
                "$interface/qwen3.6-plus": {},
                "anthropic/special": {"access_groups": ["VIP"]},
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic", "openai"})
        self.assertIn("anthropic/special", result["anthropic/secondary"])
        self.assertIn("anthropic/special", result["openai/secondary"])

    def test_preserves_non_interface_keys(self):
        model_aliases = {
            "claude-opus-*": {
                "anthropic/gpt-5.5": {},
            },
            "$interface/secondary": {
                "$interface/qwen3.6-plus": {},
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic"})
        self.assertIn("claude-opus-*", result)
        self.assertIn("anthropic/secondary", result)

    def test_resolves_interface_from_outer_key_prefix(self):
        model_aliases = {
            "anthropic/secondary": {
                "$interface/qwen3.6-plus": {"access_groups": ["General"]},
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic", "openai"})
        self.assertEqual(
            set(result["anthropic/secondary"]),
            {"anthropic/qwen3.6-plus"},
        )
        self.assertEqual(
            result["anthropic/secondary"]["anthropic/qwen3.6-plus"],
            {"access_groups": ["General"]},
        )

    def test_resolves_interface_from_outer_key_with_extends(self):
        model_aliases = {
            "openai/secondary": {
                "$extends": "$interface/primary",
                "$interface/qwen3.6-plus": {},
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic", "openai"})
        self.assertEqual(result["openai/secondary"]["$extends"], "openai/primary")
        self.assertIn("openai/qwen3.6-plus", result["openai/secondary"])

    def test_no_interface_prefix_in_key_leaves_targets_unchanged(self):
        model_aliases = {
            "claude-opus-*": {
                "$interface/qwen3.6-plus": {},
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic", "openai"})
        self.assertIn("$interface/qwen3.6-plus", result["claude-opus-*"])

    def test_replaces_interface_in_extends_string(self):
        model_aliases = {
            "$interface/primary": {
                "$interface/gpt-5.5": {},
            },
            "$interface/secondary": {
                "$extends": "$interface/primary",
                "$interface/qwen3.6-plus": {},
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic", "openai"})
        self.assertEqual(
            result["anthropic/secondary"]["$extends"], "anthropic/primary"
        )
        self.assertEqual(
            result["openai/secondary"]["$extends"], "openai/primary"
        )

    def test_replaces_interface_in_extends_array(self):
        model_aliases = {
            "$interface/base": {
                "$interface/model-a": {},
            },
            "$interface/vision": {
                "$interface/model-b": {},
            },
            "$interface/primary": {
                "$extends": ["$interface/base", "$interface/vision"],
            },
        }
        result = expand_interface_vars(model_aliases, {"anthropic"})
        self.assertEqual(
            result["anthropic/primary"]["$extends"],
            ["anthropic/base", "anthropic/vision"],
        )

    def test_empty_interfaces_returns_unchanged(self):
        model_aliases = {
            "$interface/secondary": {"$interface/qwen3.6-plus": {}},
        }
        result = expand_interface_vars(model_aliases, set())
        self.assertEqual(result, model_aliases)

    def test_generate_config_interface_expansion_end_to_end(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {"qwen3.6-plus": {}},
                    "interfaces": {
                        "anthropic": {},
                        "openai": {},
                    },
                },
            },
            "model_aliases": {
                "$interface/secondary": {
                    "$interface/qwen3.6-plus": {"access_groups": ["General"]},
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        anthropic_aliases = [
            m for m in generated["models"]
            if m["model_name"] == "anthropic/secondary"
        ]
        openai_aliases = [
            m for m in generated["models"]
            if m["model_name"] == "openai/secondary"
        ]
        self.assertEqual(len(anthropic_aliases), 1)
        self.assertEqual(
            anthropic_aliases[0]["litellm_params"]["model"],
            "anthropic/qwen3.6-plus",
        )
        self.assertEqual(len(openai_aliases), 1)
        self.assertEqual(
            openai_aliases[0]["litellm_params"]["model"],
            "openai/qwen3.6-plus",
        )

    def test_interface_with_extends_end_to_end(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {
                        "qwen3.6-plus": {},
                        "example-model-pro": {},
                    },
                    "interfaces": {
                        "anthropic": {},
                        "openai": {},
                    },
                },
            },
            "model_aliases": {
                "$interface/primary": {
                    "$interface/qwen3.6-plus": {"access_groups": ["General"]},
                },
                "$interface/secondary": {
                    "$extends": "$interface/primary",
                    "$interface/example-model-pro": {"access_groups": ["General"]},
                },
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        anthropic_secondary = [
            m for m in generated["models"]
            if m["model_name"] == "anthropic/secondary"
        ]
        targets = {m["litellm_params"]["model"] for m in anthropic_secondary}
        self.assertEqual(
            targets,
            {"anthropic/qwen3.6-plus", "anthropic/example-model-pro"},
        )

        openai_secondary = [
            m for m in generated["models"]
            if m["model_name"] == "openai/secondary"
        ]
        targets = {m["litellm_params"]["model"] for m in openai_secondary}
        self.assertEqual(
            targets,
            {"openai/qwen3.6-plus", "openai/example-model-pro"},
        )


class ResolveFallbackBaseRefsTest(unittest.TestCase):
    def test_comma_separated_keys_are_expanded(self):
        from ..src.config_generate import resolve_fallback_base_refs

        base_fallbacks = [
            {
                "anthropic/primary,claude-opus-4-8": [
                    "anthropic/secondary"
                ]
            }
        ]
        fallbacks = [
            {
                "anthropic/primary,gpt-4": [
                    "$base",
                    "openai/secondary"
                ]
            }
        ]

        resolved = resolve_fallback_base_refs(fallbacks, base_fallbacks)

        expected = [
            {
                "anthropic/primary": [
                    "anthropic/secondary",
                    "openai/secondary"
                ]
            },
            {
                "gpt-4": [
                    "openai/secondary"
                ]
            }
        ]
        self.assertEqual(resolved, expected)

    def test_generate_config_expands_interface_fallback_keys_and_values(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {"gpt-5.5": {}},
                    "interfaces": {
                        "anthropic": {},
                        "openai": {},
                    },
                },
            },
            "fallbacks": [
                {
                    "$interface/primary": [
                        "$interface/secondary",
                        "$interface/tertiary",
                    ]
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertEqual(
            generated["fallbacks"],
            [
                {
                    "anthropic/primary": [
                        "anthropic/secondary",
                        "anthropic/tertiary",
                    ]
                },
                {
                    "openai/primary": [
                        "openai/secondary",
                        "openai/tertiary",
                    ]
                },
            ],
        )

    def test_generate_config_expands_interface_fallback_values_from_concrete_key(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {"gpt-5.5": {}},
                    "interfaces": {
                        "anthropic": {},
                        "openai": {},
                    },
                },
            },
            "fallbacks": [
                {
                    "anthropic/primary": [
                        "$interface/secondary",
                        "openai/backup",
                    ]
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertEqual(
            generated["fallbacks"],
            [
                {
                    "anthropic/primary": [
                        "anthropic/secondary",
                        "openai/backup",
                    ]
                }
            ],
        )

    def test_generate_config_expands_interface_values_per_comma_separated_key(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {"gpt-5.5": {}},
                    "interfaces": {
                        "anthropic": {},
                        "openai": {},
                    },
                },
            },
            "fallbacks": [
                {
                    "anthropic/primary,openai/primary": [
                        "$interface/secondary",
                    ]
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertEqual(
            generated["fallbacks"],
            [
                {
                    "anthropic/primary": [
                        "anthropic/secondary",
                    ]
                },
                {
                    "openai/primary": [
                        "openai/secondary",
                    ]
                },
            ],
        )

    def test_generate_config_resolves_base_after_interface_fallback_expansion(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {
                "svc": {
                    "api_key": "dummy",
                    "models_autofill_disabled": True,
                    "models": {"gpt-5.5": {}},
                    "interfaces": {
                        "anthropic": {},
                        "openai": {},
                    },
                },
            },
            "fallbacks": [
                {
                    "$interface/primary": [
                        "$interface/secondary",
                    ]
                }
            ],
        }
        local_config = {
            "fallbacks": [
                {
                    "$interface/primary": [
                        "$base",
                        "$interface/tertiary",
                    ]
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            local_config_path = Path(tmpdir) / "config.local.json"
            config_path.write_text(json.dumps(config))
            local_config_path.write_text(json.dumps(local_config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertEqual(
            generated["fallbacks"],
            [
                {
                    "anthropic/primary": [
                        "anthropic/secondary",
                        "anthropic/tertiary",
                    ]
                },
                {
                    "openai/primary": [
                        "openai/secondary",
                        "openai/tertiary",
                    ]
                },
            ],
        )

    def test_generate_config_preserves_guardrails(self):
        guardrails = {
            "headroom-compression": {
                "litellm_params": {
                    "guardrail": "headroom",
                    "mode": "pre_call",
                    "default_on": True,
                },
            }
        }
        config = {
            "$schema": "./config.schema.json",
            "providers": {},
            "guardrails": guardrails,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertEqual(
            generated["guardrails"],
            [
                {
                    "guardrail_name": "headroom-compression",
                    **guardrails["headroom-compression"],
                }
            ],
        )

    def test_local_config_overrides_guardrail_api_key_by_name(self):
        config = {
            "$schema": "./config.schema.json",
            "providers": {},
            "guardrails": {
                "headroom-compression": {
                    "litellm_params": {
                        "guardrail": "headroom",
                        "mode": "pre_call",
                        "default_on": True,
                        "api_base": "http://headroom:8787",
                    },
                }
            },
        }
        local_config = {
            "guardrails": {
                "headroom-compression": {
                    "litellm_params": {
                        "api_key": "raw-headroom-token",
                    }
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            config_path.with_name("config.local.json").write_text(
                json.dumps(local_config)
            )
            with patch(f"{generator.__name__}.validate_prices"):
                generated = generate_config(config_path)

        self.assertEqual(
            generated["guardrails"],
            [
                {
                    "guardrail_name": "headroom-compression",
                    "litellm_params": {
                        "guardrail": "headroom",
                        "mode": "pre_call",
                        "default_on": True,
                        "api_base": "http://headroom:8787",
                        "api_key": "raw-headroom-token",
                    },
                }
            ],
        )


class GeneratedConfigContractTest(unittest.TestCase):
    def test_generated_output_is_valid_complete_input(self):
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config({}, require_complete=True)
            round_tripped = generate_config(generated, require_complete=True)

        self.assertEqual(round_tripped, generated)

    def test_generated_explicit_sections_are_valid_complete_input(self):
        source = {
            "credentials": [
                {
                    "credential_name": "credential",
                    "credential_values": {"api_key": "secret"},
                    "credential_info": {},
                }
            ],
            "models": [
                {
                    "model_name": "model",
                    "litellm_params": {"model": "openai/model"},
                    "model_info": {},
                }
            ],
            "guardrails": {
                "guardrail": {
                    "litellm_params": {
                        "guardrail": "custom",
                        "mode": "pre_call",
                    }
                }
            },
        }
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config(source, require_complete=True)
            round_tripped = generate_config(generated, require_complete=True)
        self.assertEqual(round_tripped, generated)

    def test_generated_guardrail_list_rejects_malformed_nested_payload(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "litellm_params.*object"):
                generate_config(
                    {
                        "guardrails": [
                            {
                                "guardrail_name": "bad",
                                "litellm_params": [],
                            }
                        ]
                    },
                    require_complete=True,
                )

    def test_alias_public_hub_autofill_requires_explicit_opt_in(self):
        source = {
            "models": [
                {
                    "model_name": "target",
                    "litellm_params": {"model": "openai/target"},
                    "model_info": {},
                }
            ],
            "aliases": {"friendly": "target"},
            "public_model_hub_aliases_autofill_enabled": True,
        }
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config(source, require_complete=True)
        self.assertEqual(generated["public_model_hub"], ["friendly"])

    def test_public_hub_flags_must_be_boolean(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "must be a boolean"):
                generate_config(
                    {"public_model_hub_aliases_autofill_enabled": "false"},
                    require_complete=True,
                )

    def test_known_router_scalar_types_are_validated(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "routing_strategy.*string"):
                generate_config(
                    {"router_settings": {"routing_strategy": []}},
                    require_complete=True,
                )

    def test_provider_public_hub_flag_must_be_boolean(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "is_public_model_hub.*boolean"):
                generate_config(
                    {
                        "providers": {
                            "broken": {
                                "api_key": "dummy",
                                "is_public_model_hub": "yes",
                                "interfaces": {},
                            }
                        }
                    },
                    require_complete=True,
                )

    def test_models_autofill_disabled_must_be_boolean(self):
        for location in ("provider", "interface"):
            provider = {
                "api_key": "dummy",
                "interfaces": {"openai": {"models": {"model": {}}}},
            }
            if location == "provider":
                provider["models_autofill_disabled"] = "false"
            else:
                provider["interfaces"]["openai"]["models_autofill_disabled"] = "false"
            with self.subTest(location=location):
                with patch(f"{generator.__name__}.validate_prices"):
                    with self.assertRaisesRegex(
                        ModelDiscoveryError, "models_autofill_disabled.*boolean"
                    ):
                        generate_config(
                            {"providers": {"broken": provider}},
                            require_complete=True,
                        )

    def test_model_exposure_controls_are_typed_recursively(self):
        malformed_models = (
            {"model": {"ignored": "false"}},
            {"model": {"is_public_model_hub": "false"}},
            {"model": {"model_name_prefix": []}},
            {"model": {"model_names": {"alias": {"ignored": "false"}}}},
        )
        for models in malformed_models:
            provider = {
                "api_key": "dummy",
                "models_autofill_disabled": True,
                "interfaces": {"openai": {"models": models}},
            }
            with self.subTest(models=models):
                with patch(f"{generator.__name__}.validate_prices"):
                    with self.assertRaises(ModelDiscoveryError):
                        generate_config(
                            {"providers": {"broken": provider}},
                            require_complete=True,
                        )

    def test_empty_model_aliases_do_not_authorize_model_replacement(self):
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config(
                {"model_aliases": {}},
                require_complete=True,
            )
        self.assertIsNone(generated["models"])

    def test_omitted_public_hub_is_not_inferred_from_aliases(self):
        source = {
            "models": [
                {
                    "model_name": "target",
                    "litellm_params": {"model": "openai/target"},
                    "model_info": {},
                }
            ],
            "aliases": {"friendly": "target"},
        }
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config(source, require_complete=True)
        self.assertIsNone(generated["public_model_hub"])

    def test_explicit_empty_public_hub_disables_alias_autofill(self):
        source = {
            "models": [
                {
                    "model_name": "target",
                    "litellm_params": {"model": "openai/target"},
                    "model_info": {},
                }
            ],
            "aliases": {"friendly": "target"},
            "public_model_hub": [],
        }
        with patch(f"{generator.__name__}.validate_prices"):
            generated = generate_config(source, require_complete=True)
        self.assertEqual(generated["public_model_hub"], [])

    def test_provider_api_base_must_be_a_string(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "api_base.*string"):
                generate_config(
                    {
                        "providers": {
                            "broken": {
                                "api_base": [],
                                "interfaces": {},
                            }
                        }
                    },
                    require_complete=True,
                )

    def test_interface_api_base_must_be_a_string(self):
        with patch(f"{generator.__name__}.validate_prices"):
            with self.assertRaisesRegex(ModelDiscoveryError, "api_base.*string"):
                generate_config(
                    {
                        "providers": {
                            "broken": {
                                "interfaces": {
                                    "openai": {"api_base": 123},
                                }
                            }
                        }
                    },
                    require_complete=True,
                )


if __name__ == "__main__":
    unittest.main()
