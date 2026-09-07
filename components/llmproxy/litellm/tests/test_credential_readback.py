"""Masked credential readback must not weaken identity or prune checks."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..src import config_sync as sync


class CredentialReadbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {
            "LITELLM_API_KEY": "synthetic-management-key",
            "LITELLM_BASE_URL": "http://litellm.test",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.desired = {
            "credential_name": "credential",
            "credential_values": {
                "api_key": "synthetic-secret-value",
                "api_base": "https://provider.test/v1",
            },
            "credential_info": {"description": "desired"},
        }
        self.actual = sync._credential_payload_with_fingerprint(copy.deepcopy(self.desired))
        self.actual["credential_values"]["api_key"] = "sy****ue"
        self.config = {"credentials": [self.desired]}

    def test_missing_or_changed_evidence_is_rejected(self):
        cases = {
            "missing key": lambda c: c["credential_values"].pop("api_key"),
            "missing base": lambda c: c["credential_values"].pop("api_base"),
            "wrong base": lambda c: c["credential_values"].update(api_base="https://other.test"),
            "masked base": lambda c: c["credential_values"].update(api_base="ht****v1"),
            "wrong plaintext": lambda c: c["credential_values"].update(api_key="wrong"),
            "wrong prefix": lambda c: c["credential_values"].update(api_key="xx****ue"),
            "wrong suffix": lambda c: c["credential_values"].update(api_key="sy****xx"),
            "wrong mask length": lambda c: c["credential_values"].update(api_key="sy*****ue"),
            "opaque redaction": lambda c: c["credential_values"].update(api_key="[REDACTED]"),
            "all stars for long key": lambda c: c["credential_values"].update(api_key="*****"),
            "missing marker": lambda c: c["credential_info"].pop(sync._CREDENTIAL_FINGERPRINT_KEY),
            "stale marker": lambda c: c["credential_info"].update({sync._CREDENTIAL_FINGERPRINT_KEY: "wrong"}),
            "wrong metadata": lambda c: c["credential_info"].update(description="wrong"),
            "wrong identity": lambda c: c.update(credential_name="other"),
        }
        for label, change in cases.items():
            with self.subTest(case=label):
                actual = copy.deepcopy(self.actual)
                change(actual)
                self.assertFalse(sync.verify_credentials(self.config, [actual]))

    def test_plaintext_and_short_string_masks_are_supported(self):
        for value, masked in (
            ("", "*****"), ("x", "*****"), ("abcd", "*****"),
            ("abcde", "ab****de"), ("synthetic-secret-value", "sy****ue"),
            ("abcdefghi", "ab****hi"),
        ):
            for readable in (True, False):
                with self.subTest(length=len(value), readable=readable):
                    desired = copy.deepcopy(self.desired)
                    desired["credential_values"]["api_key"] = value
                    actual = sync._credential_payload_with_fingerprint(copy.deepcopy(desired))
                    actual["credential_values"]["api_key"] = value if readable else masked
                    self.assertTrue(sync.verify_credentials({"credentials": [desired]}, [actual]))

    def test_nested_masking_matches_litellm_field_selection(self):
        for field in ("authorization", "TOKEN", "api_key", "client_secret",
                      "vertex_credentials", "credentials", "password", "passwd"):
            with self.subTest(field=field):
                desired = {"credentials": {field: "synthetic-secret-value", "api_base": "base"}}
                actual = {"credentials": {field: "sy****ue", "api_base": "base"}}
                self.assertTrue(sync._credential_values_match(actual, desired))
        self.assertFalse(sync._credential_values_match(
            {"settings": {"api_key": "sy****ue"}},
            {"settings": {"api_key": "synthetic-secret-value"}},
        ))
        self.assertFalse(sync._credential_values_match(
            {"credentials": {"api_key": "sy****ue"}},
            {"credentials": {"api_key": "synthetic-secret-value"}}, depth=20,
        ))
        self.assertFalse(sync._credential_values_match({"token": "*****"}, {"token": 123}))
        self.assertFalse(sync._credential_values_match(
            {"tokens": ["sy****ue"]}, {"tokens": ["synthetic-secret-value"]},
        ))

    def test_masked_readback_cannot_authorize_credential_prune(self):
        self.assertTrue(sync.verify_credentials(self.config, [self.actual]))
        self.assertFalse(sync._credential_inventory_entry_is_verifiable(self.actual))
        with (
            patch.object(sync, "get_credential_inventory", return_value=[self.actual]),
            patch.object(sync, "get_all_models") as models,
            patch.object(sync, "delete_credential") as delete,
        ):
            self.assertFalse(sync.prune_credentials(self.config))
        models.assert_not_called()
        delete.assert_not_called()

    async def test_real_sync_orchestration_handles_skipped_created_and_forced_masks(self):
        for mode in ("skip", "create", "update"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmpdir:
                events = []
                inventory = [] if mode == "create" else [copy.deepcopy(self.actual)]

                def get(endpoint):
                    self.assertEqual(endpoint, "credentials")
                    events.append("GET")
                    return True, {"credentials": copy.deepcopy(inventory)}

                def write(endpoint, payload):
                    self.assertEqual(endpoint, "credentials" if mode == "create" else "credentials/credential")
                    self.assertEqual(payload["credential_values"], self.desired["credential_values"])
                    self.assertEqual(payload["credential_info"][sync._CREDENTIAL_FINGERPRINT_KEY],
                                     sync._credential_fingerprint(self.desired))
                    events.append("POST" if mode == "create" else "PATCH")
                    inventory[:] = [copy.deepcopy(self.actual)]
                    return True, {"success": True}

                with (
                    patch.object(sync, "load_dotenv"),
                    patch.object(sync, "generate_config_for_preset", return_value=self.config),
                    patch.object(sync, "get_request", side_effect=get),
                    patch.object(sync, "post_request", side_effect=write) as post,
                    patch.object(sync, "patch_request", side_effect=write) as update,
                    patch.object(sync.logger, "warning") as warning,
                ):
                    self.assertEqual(await sync.sync_config(
                        preset="default", only="credentials", force=mode == "update", root=Path(tmpdir),
                    ), 0)
                expected = ["GET", "GET", "GET"] if mode == "skip" else [
                    "GET", "POST" if mode == "create" else "PATCH", "GET", "GET",
                ]
                self.assertEqual(events, expected)
                self.assertEqual(post.call_count, int(mode == "create"))
                self.assertEqual(update.call_count, int(mode == "update"))
                warning.assert_not_called()

    async def test_models_only_accepts_verified_masked_dependency(self):
        model = {
            "model_name": "openai/test",
            "litellm_params": {"model": "openai/test", "litellm_credential_name": "credential"},
            "model_info": {"id": "test-id"},
        }
        config = {**self.config, "models": [model]}
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(sync, "load_dotenv"),
            patch.object(sync, "generate_config_for_preset", return_value=config),
            patch.object(sync, "get_credential_inventory", return_value=[self.actual]),
            patch.object(sync, "get_all_models", return_value=[model]),
            patch.object(sync, "get_actor_from_key", return_value="tester"),
            patch.object(sync, "post_request") as post,
            patch.object(sync, "patch_request") as update,
        ):
            self.assertEqual(await sync.sync_config(
                preset="default", only="models", root=Path(tmpdir),
            ), 0)
        post.assert_not_called()
        update.assert_not_called()
