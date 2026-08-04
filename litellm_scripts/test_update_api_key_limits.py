import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))


with patch.dict(
    os.environ,
    {
        "LITELLM_API_KEY": "dummy",
        "LITELLM_BASE_URL": "http://litellm.test",
    },
):
    update_api_key_limits = importlib.import_module("update_api_key_limits")


class KeyLimitOverrideTest(unittest.TestCase):
    def test_matching_override_replaces_rpm_and_max_budget(self):
        key_record = {
            "key_alias": "key-a",
            "token": "sk-a",
        }

        limits = update_api_key_limits.resolve_key_limits(
            key_record,
            100,
            3000,
            {
                "key-a": {
                    "rpm_limit": 4000,
                    "max_budget": 5000,
                }
            },
        )

        self.assertEqual(limits, (4000, 5000))

    def test_unmatched_key_uses_supplied_defaults(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-b"},
            150,
            3500,
            {"key-a": {"rpm_limit": 4000, "max_budget": 5000}},
        )

        self.assertEqual(limits, (150, 3500))

    def test_partial_override_keeps_other_default(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-a"},
            100,
            3000,
            {"key-a": {"max_budget": 5000}},
        )

        self.assertEqual(limits, (100, 5000))

    def test_multipliers_are_applied_to_supplied_defaults(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-a"},
            100,
            3000,
            {
                "key-a": {
                    "rpm_limit_multiplier": 2.5,
                    "max_budget_multiplier": 1.5,
                }
            },
        )

        self.assertEqual(limits, (250, 4500))

    def test_explicit_limits_take_precedence_over_multipliers(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-a"},
            100,
            3000,
            {
                "key-a": {
                    "rpm_limit": 400,
                    "rpm_limit_multiplier": 2,
                    "max_budget": 5000,
                    "max_budget_multiplier": 3,
                }
            },
        )

        self.assertEqual(limits, (400, 5000))

    def test_explicit_unlimited_limits_take_precedence_over_multipliers(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-a"},
            100,
            3000,
            {
                "key-a": {
                    "rpm_limit": None,
                    "rpm_limit_multiplier": 2,
                    "max_budget": None,
                    "max_budget_multiplier": 3,
                }
            },
        )

        self.assertEqual(limits, (None, None))

    def test_rpm_multiplier_must_produce_an_integer_limit(self):
        with self.assertRaisesRegex(ValueError, "must produce an integer"):
            update_api_key_limits.resolve_key_limits(
                {"key_alias": "key-a"},
                50,
                3000,
                {"key-a": {"rpm_limit_multiplier": 0.333}},
            )

    def test_multiplier_cannot_be_applied_to_an_unlimited_default(self):
        with self.assertRaisesRegex(ValueError, "default max_budget is unlimited"):
            update_api_key_limits.resolve_key_limits(
                {"key_alias": "key-a"},
                50,
                None,
                {"key-a": {"max_budget_multiplier": 2}},
            )

    def test_override_matches_all_supported_key_fields(self):
        fields = (
            "key",
            "token",
            "key_name",
            "key_alias",
            "api_key",
            "user_id",
            "user_email",
        )

        for field in fields:
            with self.subTest(field=field):
                limits = update_api_key_limits.resolve_key_limits(
                    {field: "matching-value"},
                    100,
                    3000,
                    {"matching-value": {"rpm_limit": 4000}},
                )
                self.assertEqual(limits, (4000, 3000))

    def test_first_matching_override_wins(self):
        key_record = {
            "key_alias": "key-a",
            "user_id": "user-a",
        }

        limits = update_api_key_limits.resolve_key_limits(
            key_record,
            100,
            3000,
            {
                "key-a": {"rpm_limit": 4000, "max_budget": 5000},
                "user-a": {"rpm_limit": 6000, "max_budget": 7000},
            },
        )

        self.assertEqual(limits, (4000, 5000))

    def test_validation_rejects_invalid_override_configuration(self):
        invalid_configs = (
            [],
            {"": {"rpm_limit": 4000}},
            {"key-a": {}},
            {"key-a": 4000},
            {"key-a": {"rpm_limit": 0}},
            {"key-a": {"rpm_limit": 1.5}},
            {"key-a": {"rpm_limit": True}},
            {"key-a": {"max_budget": 0}},
            {"key-a": {"max_budget": "5000"}},
            {"key-a": {"rpm_limit_multiplier": 0}},
            {"key-a": {"rpm_limit_multiplier": True}},
            {"key-a": {"rpm_limit_multiplier": "2"}},
            {"key-a": {"max_budget_multiplier": -1}},
            {"key-a": {"max_budget_multiplier": None}},
            {"key-a": {"unknown": 1}},
        )

        for overrides in invalid_configs:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    update_api_key_limits.validate_key_limit_overrides(overrides)

    def test_load_overrides_without_local_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "api_key_limits.json"
            expected = {"key-a": {"rpm_limit": 4000}}
            config_path.write_text(json.dumps(expected))

            overrides = update_api_key_limits.load_key_limit_overrides(config_path)

        self.assertEqual(overrides, expected)

    def test_schema_metadata_is_ignored_when_loading_overrides(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "api_key_limits.json"
            config_path.write_text(
                json.dumps(
                    {
                        "$schema": "./api_key_limits.schema.json",
                        "key-a": {"rpm_limit_multiplier": 2},
                    }
                )
            )

            overrides = update_api_key_limits.load_key_limit_overrides(config_path)

        self.assertEqual(overrides, {"key-a": {"rpm_limit_multiplier": 2}})

    def test_local_overrides_are_deep_merged(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "api_key_limits.json"
            local_path = Path(tmp_dir) / "api_key_limits.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "key-a": {"rpm_limit": 4000, "max_budget": 5000},
                        "key-b": {"rpm_limit": 200},
                    }
                )
            )
            local_path.write_text(
                json.dumps(
                    {
                        "key-a": {"max_budget": 6000},
                        "key-c": {"rpm_limit": 300},
                    }
                )
            )

            overrides = update_api_key_limits.load_key_limit_overrides(config_path)

        self.assertEqual(
            overrides,
            {
                "key-a": {"rpm_limit": 4000, "max_budget": 6000},
                "key-b": {"rpm_limit": 200},
                "key-c": {"rpm_limit": 300},
            },
        )

    def test_load_overrides_rejects_malformed_or_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "api_key_limits.json"
            config_path.write_text("{")
            with self.assertRaises(json.JSONDecodeError):
                update_api_key_limits.load_key_limit_overrides(config_path)

            config_path.write_text(json.dumps({"key-a": {"rpm_limit": 0}}))
            with self.assertRaises(ValueError):
                update_api_key_limits.load_key_limit_overrides(config_path)

    def test_config_failure_stops_before_listing_keys(self):
        stderr = io.StringIO()

        with (
            patch.object(
                update_api_key_limits,
                "load_key_limit_overrides",
                side_effect=ValueError("invalid limits"),
            ),
            patch.object(update_api_key_limits, "resolve_target_keys") as resolve,
            patch.object(sys, "argv", ["update_api_key_limits.py"]),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_error,
        ):
            update_api_key_limits.main()

        self.assertEqual(exit_error.exception.code, 1)
        self.assertIn("Invalid API key limits configuration", stderr.getvalue())
        resolve.assert_not_called()

    def test_dry_run_prints_effective_limits_per_key(self):
        keys = [
            {"key_alias": "key-a", "token": "sk-a"},
            {"key_alias": "key-b", "token": "sk-b"},
        ]
        stderr = io.StringIO()

        with (
            patch.object(
                update_api_key_limits,
                "load_key_limit_overrides",
                return_value={
                    "key-a": {"rpm_limit": 4000, "max_budget": 5000}
                },
            ),
            patch.object(
                update_api_key_limits,
                "resolve_target_keys",
                return_value=keys,
            ),
            patch.object(update_api_key_limits, "update_api_key") as update,
            patch.object(
                sys,
                "argv",
                [
                    "update_api_key_limits.py",
                    "--rpm",
                    "150",
                    "--max-budget",
                    "3500",
                ],
            ),
            redirect_stderr(stderr),
        ):
            update_api_key_limits.main()

        output = stderr.getvalue()
        self.assertIn(
            "Would update key-a (sk-a): rpm_limit=4000, max_budget=5000",
            output,
        )
        self.assertIn(
            "Would update key-b (sk-b): rpm_limit=150, max_budget=3500",
            output,
        )
        update.assert_not_called()

    def test_apply_passes_effective_limits_per_key(self):
        keys = [
            {"key_alias": "key-a", "token": "sk-a"},
            {"key_alias": "key-b", "token": "sk-b"},
        ]

        with (
            patch.object(
                update_api_key_limits,
                "load_key_limit_overrides",
                return_value={
                    "key-a": {"rpm_limit": 4000, "max_budget": 5000}
                },
            ),
            patch.object(
                update_api_key_limits,
                "resolve_target_keys",
                return_value=keys,
            ),
            patch.object(update_api_key_limits, "update_api_key") as update,
            patch.object(
                sys,
                "argv",
                [
                    "update_api_key_limits.py",
                    "--rpm",
                    "150",
                    "--max-budget",
                    "3500",
                    "--yes",
                ],
            ),
            redirect_stderr(io.StringIO()),
        ):
            update_api_key_limits.main()

        self.assertEqual(update.call_count, 2)
        self.assertEqual(update.call_args_list[0].args[1:3], (4000, 5000))
        self.assertEqual(update.call_args_list[1].args[1:3], (150, 3500.0))


if __name__ == "__main__":
    unittest.main()
