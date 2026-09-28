"""Regression coverage for non-force membership and early prune checks."""
import unittest
from unittest.mock import patch

from ..src import config_sync as sync


class SyncCompatibilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_nonforce_orchestration_preserves_existing_configuration(self):
        live = {"model_name": "model", "litellm_params": {"model": "openai/old"},
                "model_info": {"id": "existing"}}
        desired = {"models": [{"model_name": "model", "litellm_params": {"model": "openai/new"}}]}
        with (
            patch.object(sync, "load_dotenv"),
            patch.object(sync, "_get_api_key", return_value="test"),
            patch.object(sync, "_get_base_url", return_value="http://test.invalid"),
            patch.object(sync, "generate_config_for_preset", return_value=desired),
            patch.object(sync, "get_all_models", return_value=[live]),
            patch.object(sync, "get_actor_from_key", return_value="test"),
            patch.object(sync, "post_request") as post,
        ):
            self.assertEqual(await sync.sync_config(preset="test", only="models"), 0)
        post.assert_not_called()
        self.assertEqual(desired["models"][0]["litellm_params"]["model"], "openai/new")

    async def test_unverifiable_stale_credential_blocks_before_any_write(self):
        desired = {"credentials": [{"credential_name": "keep", "credential_values": {}}], "models": []}
        with (
            patch.object(sync, "load_dotenv"),
            patch.object(sync, "_get_api_key", return_value="test"),
            patch.object(sync, "_get_base_url", return_value="http://test.invalid"),
            patch.object(sync, "generate_config_for_preset", return_value=desired),
            patch.object(sync, "get_credential_inventory", return_value=[{"credential_name": "stale"}]),
            patch.object(sync, "get_all_models", return_value=[]),
            patch.object(sync, "get_router_settings", return_value={}),
            patch.object(sync, "get_current_public_model_hub", return_value=[]),
            patch.object(sync, "post_request") as post,
            patch.object(sync, "patch_request") as update,
            patch.object(sync, "delete_request") as delete,
        ):
            with self.assertRaisesRegex(sync.CommandError, "before writes"):
                await sync.sync_config(preset="test", only="credentials,models", force=True, prune=True)
        post.assert_not_called()
        update.assert_not_called()
        delete.assert_not_called()

    def test_masked_stale_credential_still_cannot_be_deleted(self):
        with (
            patch.object(sync, "get_credential_inventory", return_value=[{"credential_name": "stale"}]),
            patch.object(sync, "delete_credential") as delete,
        ):
            self.assertFalse(sync.prune_credentials({"credentials": []}))
        delete.assert_not_called()
