import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from gen_config import deep_merge, generate_config, resolve_provider_models, _resolve_alias_group, expand_interface_vars


class RepositoryConfigTest(unittest.TestCase):
    def test_tier_deployments_do_not_override_max_input_tokens(self):
        config_path = Path(__file__).with_name("config.json")
        config = json.loads(config_path.read_text())
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
        config_path = Path(__file__).with_name("config.json")
        config = json.loads(config_path.read_text())
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

        with patch("gen_config.fetch_models_from_api", return_value=["discovered"]):
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
            with patch("gen_config.validate_prices"):
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

            with patch("gen_config.validate_prices"):
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

    def test_generate_config_accepts_wildcard_model_alias_in_fallbacks(self):
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

            with patch("gen_config.validate_prices"), patch("gen_config.logger.warning") as warn:
                generate_config(config_path)

        warning_messages = [call.args[0] for call in warn.call_args_list]
        self.assertFalse(
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
            with patch("gen_config.validate_prices"):
                generated = generate_config(config_path)

        self.assertIsNone(generated["aliases"])

    def test_generate_config_null_aliases_returns_none(self):
        config = self._base_config()
        config["aliases"] = None
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch("gen_config.validate_prices"):
                generated = generate_config(config_path)

        self.assertIsNone(generated["aliases"])

    def test_generate_config_empty_aliases_returns_empty_dict(self):
        config = self._base_config()
        config["aliases"] = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config))
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
        with patch("gen_config.logger.warning") as warn:
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
        with patch("gen_config.logger.warning") as warn:
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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
        from gen_config import resolve_fallback_base_refs

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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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
            with patch("gen_config.validate_prices"):
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


if __name__ == "__main__":
    unittest.main()
