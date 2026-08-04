import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))


class ConfigModuleTestMixin:
    @classmethod
    def setUpClass(cls):
        env = {
            **os.environ,
            "LITELLM_API_KEY": "dummy",
            "LITELLM_BASE_URL": "http://litellm.test",
        }
        with patch.dict(os.environ, env):
            cls.config_module = importlib.import_module("config")


class AliasSyncTest(ConfigModuleTestMixin, unittest.TestCase):

    def test_empty_aliases_clears_router_aliases(self):
        with (
            patch.object(self.config_module, "get_router_settings", return_value={"model_group_alias": {"old": "target"}}),
            patch.object(self.config_module, "post_request", return_value=(True, "ok")) as post,
            patch.object(self.config_module, "get_all_models", return_value=[]),
        ):
            success, result = self.config_module.update_aliases({})

        self.assertTrue(success)
        self.assertEqual(result, "ok")
        post.assert_called_once_with(
            "config/update",
            {"router_settings": {"model_group_alias": {}}},
        )

    def test_none_aliases_leaves_router_aliases_unchanged(self):
        with (
            patch.object(self.config_module, "get_router_settings") as get_router_settings,
            patch.object(self.config_module, "post_request") as post,
        ):
            success, result = self.config_module.update_aliases(None)

        self.assertTrue(success)
        self.assertEqual(result, "no aliases")
        get_router_settings.assert_not_called()
        post.assert_not_called()


class ModelSyncTest(ConfigModuleTestMixin, unittest.IsolatedAsyncioTestCase):
    async def test_sync_models_accepts_manual_model_without_credential_name(self):
        config = {
            "models": [
                {
                    "model_name": "auto",
                    "litellm_params": {
                        "model": "auto_router/complexity_router",
                    },
                    "model_info": {
                        "access_groups": ["General"],
                    },
                }
            ]
        }

        with (
            patch.object(self.config_module, "get_actor_from_key", return_value="tester"),
            patch.object(self.config_module, "get_all_models", return_value=[]),
            patch.object(self.config_module, "post_request", return_value=(True, "ok")) as post,
        ):
            await self.config_module.sync_models(config)

        post.assert_called_once()
        endpoint, payload = post.call_args.args
        self.assertEqual(endpoint, "model/new")
        self.assertEqual(payload["model_name"], "auto")
        self.assertEqual(
            payload["litellm_params"],
            {"model": "auto_router/complexity_router"},
        )
        self.assertEqual(payload["model_info"]["access_groups"], ["General"])


class GuardrailSyncTest(ConfigModuleTestMixin, unittest.TestCase):
    def setUp(self):
        self.headroom = {
            "guardrail_name": "headroom-compression",
            "litellm_params": {
                "guardrail": "headroom",
                "mode": "pre_call",
                "default_on": True,
            },
            "guardrail_info": {
                "description": "Global Headroom prompt compression",
            },
        }

    def test_creates_missing_guardrail(self):
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, []),
            ),
            patch.object(
                self.config_module,
                "create_guardrail",
                return_value=(True, "ok"),
            ) as create,
            patch.object(self.config_module, "update_guardrail") as update,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom]}
            )

        self.assertTrue(success)
        create.assert_called_once_with(self.headroom)
        update.assert_not_called()

    def test_skips_matching_guardrail_with_server_fields(self):
        existing = {
            **self.headroom,
            "guardrail_id": "guardrail-1",
            "created_at": "2026-08-03T00:00:00Z",
        }
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, [existing]),
            ),
            patch.object(self.config_module, "create_guardrail") as create,
            patch.object(self.config_module, "update_guardrail") as update,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom]}
            )

        self.assertTrue(success)
        create.assert_not_called()
        update.assert_not_called()

    def test_updates_changed_guardrail_by_id(self):
        existing = {
            **self.headroom,
            "guardrail_id": "guardrail-1",
            "litellm_params": {
                **self.headroom["litellm_params"],
                "default_on": False,
            },
        }
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, [existing]),
            ),
            patch.object(self.config_module, "create_guardrail") as create,
            patch.object(
                self.config_module,
                "update_guardrail",
                return_value=(True, "ok"),
            ) as update,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom]}
            )

        self.assertTrue(success)
        create.assert_not_called()
        update.assert_called_once_with("guardrail-1", self.headroom)

    def test_does_not_delete_unmanaged_guardrails(self):
        existing = {
            "guardrail_id": "other-1",
            "guardrail_name": "other-guardrail",
            "litellm_params": {
                "guardrail": "generic",
                "mode": "pre_call",
            },
        }
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, [existing]),
            ),
            patch.object(
                self.config_module,
                "create_guardrail",
                return_value=(True, "ok"),
            ) as create,
            patch.object(self.config_module, "update_guardrail") as update,
            patch.object(self.config_module, "delete_request") as delete,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom]}
            )

        self.assertTrue(success)
        create.assert_called_once_with(self.headroom)
        update.assert_not_called()
        delete.assert_not_called()

    def test_does_not_create_when_guardrail_listing_fails(self):
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(False, "service unavailable"),
            ),
            patch.object(self.config_module, "create_guardrail") as create,
            patch.object(self.config_module, "update_guardrail") as update,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom]}
            )

        self.assertFalse(success)
        create.assert_not_called()
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
