"""Client-visible prefixes are opt-in and independent of backend adapters."""

import unittest
from unittest.mock import patch

from ..src import config_generate as generator


class ModelNamePrefixTest(unittest.TestCase):
    def test_repository_presets_resolve_unprefixed_routes(self):
        for preset in ("llmproxy", "default"):
            with self.subTest(preset=preset):
                source, _ = generator.load_config_for_preset(preset, include_local=False)
                for provider in source["providers"].values():
                    provider["api_key"] = "synthetic-key"
                with patch.object(generator, "validate_prices"):
                    generated = generator.generate_config(source, require_complete=True)
                self.assertIn("a-0", {model["model_name"] for model in generated["models"]})
                self.assertIn("o-0", {model["model_name"] for model in generated["models"]})
                for model in generated["models"]:
                    self.assertNotIn("/", model["model_name"])
                    self.assertIn("/", model["litellm_params"]["model"])

    def test_interface_prefix_is_opt_in_during_complete_generation(self):
        for interface in ("openai", "anthropic", "gemini"):
            for prefix_config, prefix in (
                ({}, ""),
                ({"model_name_prefix": None}, ""),
                ({"model_name_prefix": ""}, ""),
                ({"model_name_prefix": "custom/"}, "custom/"),
            ):
                for model_config, names in (
                    ({}, ["backend"]),
                    ({"model_names": ",primary"}, ["backend", "primary"]),
                    ({"model_names": {"$self": {}, "primary": {}}}, ["backend", "primary"]),
                ):
                    with self.subTest(interface=interface, prefix=prefix_config, model=model_config):
                        source = {"providers": {"service": {
                            "api_key": "synthetic-key",
                            "models_autofill_disabled": True,
                            "is_public_model_hub": True,
                            "interfaces": {interface: {
                                **prefix_config,
                                "models": {"backend": model_config},
                            }},
                        }}}
                        with patch.object(generator, "validate_prices"):
                            generated = generator.generate_config(source, require_complete=True)
                        expected_names = {prefix + name for name in names}
                        self.assertEqual(
                            {model["model_name"] for model in generated["models"]},
                            expected_names,
                        )
                        self.assertEqual(set(generated["public_model_hub"]), expected_names)
                        for model in generated["models"]:
                            self.assertEqual(model["litellm_params"]["model"], f"{interface}/backend")
                            self.assertEqual(
                                model["litellm_params"]["litellm_credential_name"],
                                f"service-{interface}",
                            )
