import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llmproxy.core.command import CommandError


class SyncBackendContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_options_resolve_custom_config_without_api_calls(self):
        backend_path = Path(__file__).resolve().parents[1] / "src" / "config_sync.py"
        self.assertTrue(backend_path.is_file(), "Sync backend must live under src")
        backend = importlib.import_module("..src.config_sync", __package__)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.json"
            config_path.write_text('{"providers": {}}\n')
            with (
                patch.object(backend, "_preflight_inventories") as inventory,
                patch.object(backend, "load_dotenv") as dotenv,
            ):
                result = await backend.sync_config(
                    config_path=config_path,
                    only="models",
                    dry_run=True,
                    root=root,
                )
            self.assertEqual(result, 0)
            dotenv.assert_called_once_with(root / ".env")
            inventory.assert_not_called()


class ConfigModuleTestMixin:
    @classmethod
    def setUpClass(cls):
        env = {
            **os.environ,
            "LITELLM_API_KEY": "dummy",
            "LITELLM_BASE_URL": "http://litellm.test",
        }
        with patch.dict(os.environ, env):
            cls.config_module = importlib.import_module("..src.config_sync", __package__)


class AliasSyncTest(ConfigModuleTestMixin, unittest.TestCase):
    def test_router_update_rejects_missing_preserved_nullable_key(self):
        current = {"custom_nullable": None, "timeout": 30}
        readback = {"timeout": 60}
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                side_effect=[current, readback],
            ),
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "accepted"),
            ),
        ):
            success, _ = self.config_module.update_router_settings({"timeout": 60})
        self.assertFalse(success)

    def test_router_update_verifies_preserved_sections(self):
        current = {
            "model_group_alias": {"alias": "model"},
            "fallbacks": [],
            "timeout": 30,
        }
        drifted = {**current, "model_group_alias": {"alias": "wrong"}}
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                side_effect=[current, drifted],
            ),
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "accepted"),
            ),
        ):
            success, _ = self.config_module.update_router_settings({"timeout": 60})
        self.assertFalse(success)

    def test_alias_update_requires_converged_readback(self):
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                side_effect=[{"model_group_alias": {}}, {"model_group_alias": {}}],
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[
                    {
                        "model_name": "model",
                        "litellm_params": {},
                        "model_info": {"id": "id"},
                    }
                ],
            ),
            patch.object(
                self.config_module, "post_request", return_value=(True, "ok")
            ),
        ):
            success, _ = self.config_module.update_aliases({"alias": "model"})
        self.assertFalse(success)

    def test_unresolved_alias_is_rejected_before_update(self):
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                return_value={"model_group_alias": {}},
            ),
            patch.object(self.config_module, "get_all_models", return_value=[]),
            patch.object(self.config_module, "post_request") as post,
        ):
            success, _ = self.config_module.update_aliases(
                {"broken": "missing-model"}
            )
        self.assertFalse(success)
        post.assert_not_called()

    def test_empty_aliases_clears_router_aliases(self):
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                side_effect=[
                    {"model_group_alias": {"old": "target"}},
                    {"model_group_alias": {}},
                ],
            ),
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


class FallbackSyncTest(ConfigModuleTestMixin, unittest.TestCase):
    def test_none_fallbacks_leave_router_unchanged(self):
        with (
            patch.object(self.config_module, "get_router_settings") as inventory,
            patch.object(self.config_module, "post_request") as post,
        ):
            success, _ = self.config_module.update_fallbacks(None)
        self.assertTrue(success)
        inventory.assert_not_called()
        post.assert_not_called()

    def test_fallback_update_requires_converged_readback(self):
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                side_effect=[{"fallbacks": []}, {"fallbacks": []}],
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[
                    {
                        "model_name": name,
                        "litellm_params": {},
                        "model_info": {"id": name},
                    }
                    for name in ("source", "target")
                ],
            ),
            patch.object(self.config_module, "get_current_aliases", return_value={}),
            patch.object(
                self.config_module, "post_request", return_value=(True, "ok")
            ),
        ):
            success, _ = self.config_module.update_fallbacks(
                [{"source": ["target"]}]
            )
        self.assertFalse(success)
    def test_unresolved_fallback_is_rejected_before_update(self):
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                return_value={"fallbacks": []},
            ),
            patch.object(self.config_module, "get_all_models", return_value=[]),
            patch.object(self.config_module, "get_current_aliases", return_value={}),
            patch.object(self.config_module, "post_request") as post,
        ):
            success, _ = self.config_module.update_fallbacks(
                [{"missing-source": ["missing-target"]}]
            )
        self.assertFalse(success)
        post.assert_not_called()

    def test_empty_fallbacks_clear_router_fallbacks(self):
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                side_effect=[
                    {"fallbacks": [{"old": ["backup"]}]},
                    {"fallbacks": []},
                ],
            ),
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "ok"),
            ) as post,
            patch.object(self.config_module, "get_all_models") as get_all_models,
            patch.object(self.config_module, "get_current_aliases") as get_aliases,
        ):
            success, result = self.config_module.update_fallbacks([])

        self.assertTrue(success)
        self.assertEqual(result, "ok")
        post.assert_called_once_with(
            "config/update",
            {"router_settings": {"fallbacks": []}},
        )
        get_all_models.assert_not_called()
        get_aliases.assert_not_called()

    def test_empty_fallbacks_skip_when_already_empty(self):
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                return_value={"fallbacks": []},
            ),
            patch.object(self.config_module, "post_request") as post,
        ):
            success, result = self.config_module.update_fallbacks([])

        self.assertTrue(success)
        self.assertEqual(result, "skipped")
        post.assert_not_called()


class ModelSyncTest(ConfigModuleTestMixin, unittest.IsolatedAsyncioTestCase):
    async def test_model_create_rejects_same_id_unmanaged_configuration_drift(self):
        desired = {
            "model_name": "created",
            "litellm_params": {"model": "provider/created"},
            "model_info": {},
        }
        created = {**desired, "model_info": {"id": "created-id"}}
        unmanaged = {
            "model_name": "unmanaged",
            "litellm_params": {"model": "provider/original"},
            "model_info": {"id": "unmanaged-id"},
        }
        drifted = {
            **unmanaged,
            "litellm_params": {"model": "provider/drifted"},
        }
        with (
            patch.object(self.config_module, "get_actor_from_key", return_value="actor"),
            patch.object(
                self.config_module,
                "_create_model",
                return_value=(True, "created", 0),
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[created, drifted],
            ),
        ):
            self.assertFalse(
                await self.config_module.sync_models(
                    {"models": [desired]},
                    initial_inventory=[unmanaged],
                )
            )

    def test_replacement_rejects_same_id_unmanaged_configuration_drift(self):
        old = {
            "model_name": "replace",
            "litellm_params": {"model": "provider/old"},
            "model_info": {"id": "old-id"},
        }
        unmanaged = {
            "model_name": "unmanaged",
            "litellm_params": {"model": "provider/original"},
            "model_info": {"id": "unmanaged-id"},
        }
        desired = {
            "model_name": "replace",
            "litellm_params": {"model": "provider/new"},
            "model_info": {},
        }
        new = {**desired, "model_info": {"id": "new-id"}}
        drifted = {
            **unmanaged,
            "litellm_params": {"model": "provider/drifted"},
        }
        pending = []
        cache = {
            ("replace", ""): [old],
            ("unmanaged", ""): [unmanaged],
        }
        with (
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "accepted"),
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[old, new, drifted],
            ),
        ):
            success, _, _ = self.config_module._create_model(
                desired,
                True,
                "tester",
                cache,
                pending_replacements=pending,
            )
        self.assertFalse(success)
        self.assertEqual(pending, [])

    def test_model_delete_rejects_same_id_unmanaged_configuration_drift(self):
        desired = {
            "model_name": "replace",
            "litellm_params": {"model": "provider/new"},
            "model_info": {},
        }
        old = {
            "model_name": "replace",
            "litellm_params": {"model": "provider/old"},
            "model_info": {"id": "old-id"},
        }
        new = {**desired, "model_info": {"id": "new-id"}}
        unmanaged = {
            "model_name": "unmanaged",
            "litellm_params": {"model": "provider/original"},
            "model_info": {"id": "unmanaged-id"},
        }
        drifted = {
            **unmanaged,
            "litellm_params": {"model": "provider/drifted"},
        }
        plan = ([old], {("replace", ""): desired}, [old, new, unmanaged])
        with (
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(True, "accepted"),
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[new, drifted],
            ),
        ):
            self.assertFalse(
                self.config_module.prune_models(
                    {"models": [desired]},
                    plan=plan,
                )
            )

    def test_replacement_post_must_preserve_every_initial_model_id(self):
        old = {
            "model_name": "replace",
            "litellm_params": {"model": "provider/old"},
            "model_info": {"id": "old-id"},
        }
        unrelated = {
            "model_name": "unrelated",
            "litellm_params": {"model": "provider/unrelated"},
            "model_info": {"id": "unrelated-id"},
        }
        desired = {
            "model_name": "replace",
            "litellm_params": {"model": "provider/new"},
            "model_info": {},
        }
        new = {**desired, "model_info": {"id": "new-id"}}
        pending = []
        cache = {
            ("replace", ""): [old],
            ("unrelated", ""): [unrelated],
        }
        with (
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "accepted"),
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[old, new],
            ),
        ):
            success, _, _ = self.config_module._create_model(
                desired,
                True,
                "tester",
                cache,
                pending_replacements=pending,
            )
        self.assertFalse(success)
        self.assertEqual(pending, [])

    def test_all_name_only_dictionary_credentials_cannot_authorize_prune(self):
        plan = self.config_module._plan_credential_prune(
            {"credentials": []},
            live_models=[],
            existing_credentials=[{"credential_name": "stale"}],
        )
        self.assertIsNone(plan)

    def test_mixed_evidence_dictionary_credentials_cannot_authorize_prune(self):
        desired = {
            "credential_name": "keep",
            "credential_values": {"api_key": "secret"},
        }
        with patch.object(self.config_module, "_get_api_key", return_value="test-key"):
            retained = self.config_module._credential_payload_with_fingerprint(desired)
            plan = self.config_module._plan_credential_prune(
                {"credentials": [desired]},
                live_models=[],
                existing_credentials=[retained, {"credential_name": "stale"}],
            )
        self.assertIsNone(plan)

    def test_replacement_without_prune_preserves_unrelated_models(self):
        desired = {
            "model_name": "replace",
            "litellm_params": {"model": "provider/new"},
            "model_info": {},
        }
        old = {
            "model_name": "replace",
            "litellm_params": {"model": "provider/old"},
            "model_info": {"id": "old-id"},
        }
        new = {**desired, "model_info": {"id": "new-id"}}
        unrelated = {
            "model_name": "unrelated",
            "litellm_params": {"model": "provider/unrelated"},
            "model_info": {"id": "unrelated-id"},
        }
        plan = (
            [old],
            {("replace", None): desired},
            [old, new, unrelated],
        )
        with (
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(True, "accepted"),
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[new, unrelated],
            ),
        ):
            self.assertTrue(
                self.config_module.prune_models(
                    {"models": [desired]},
                    plan=plan,
                )
            )

    def test_name_only_credential_inventory_cannot_verify_secrets(self):
        config = {
            "credentials": [
                {
                    "credential_name": "keep",
                    "credential_values": {"api_key": "secret"},
                }
            ]
        }
        self.assertFalse(self.config_module.verify_credentials(config, ["keep"]))

    def test_name_only_credential_inventory_cannot_authorize_prune(self):
        config = {
            "credentials": [
                {
                    "credential_name": "keep",
                    "credential_values": {"api_key": "secret"},
                }
            ]
        }
        plan = self.config_module._plan_credential_prune(
            config,
            live_models=[],
            existing_credentials=["keep", "stale"],
        )
        self.assertIsNone(plan)

    def test_omitted_models_skip_prune_without_inventory_reads(self):
        with patch.object(self.config_module, "get_all_models") as inventory:
            self.assertTrue(self.config_module.prune_models({"models": None}))
        inventory.assert_not_called()

    async def test_duplicate_desired_models_are_rejected_before_inventory_or_write(self):
        model = {
            "model_name": "duplicate",
            "litellm_params": {"model": "openai/model"},
            "model_info": {},
        }
        with (
            patch.object(self.config_module, "get_all_models") as inventory,
            patch.object(self.config_module, "post_request") as post,
            self.assertRaisesRegex(CommandError, "duplicate model definitions"),
        ):
            await self.config_module.sync_models({"models": [model, dict(model)]})
        inventory.assert_not_called()
        post.assert_not_called()

    async def test_created_model_is_read_back_before_next_write(self):
        models = [
            {"model_name": name, "litellm_params": {}, "model_info": {}}
            for name in ("first", "second")
        ]
        with (
            patch.object(self.config_module, "get_actor_from_key", return_value="actor"),
            patch.object(self.config_module, "get_all_models", side_effect=[[], []]),
            patch.object(
                self.config_module,
                "_create_model",
                side_effect=[(True, "created", 0), (True, "created", 0)],
            ) as create,
        ):
            self.assertFalse(await self.config_module.sync_models({"models": models}))
        self.assertEqual(create.call_count, 1)

    async def test_skipped_stale_model_blocks_next_write(self):
        first = {
            "model_name": "first",
            "litellm_params": {"model": "openai/desired"},
            "model_info": {},
        }
        second = {
            "model_name": "second",
            "litellm_params": {"model": "openai/second"},
            "model_info": {},
        }
        stale = {
            "model_name": "first",
            "litellm_params": {"model": "openai/stale"},
            "model_info": {"id": "stale-id"},
        }
        with (
            patch.object(self.config_module, "get_actor_from_key", return_value="actor"),
            patch.object(
                self.config_module,
                "get_all_models",
                side_effect=[[stale], [stale]],
            ),
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "accepted"),
            ) as post,
        ):
            self.assertFalse(
                await self.config_module.sync_models({"models": [first, second]})
            )
        post.assert_not_called()

    def test_model_prune_stops_after_first_delete_failure(self):
        inventory = [
            {
                "model_name": name,
                "litellm_params": {},
                "model_info": {"id": f"{name}-id"},
            }
            for name in ("stale-one", "stale-two")
        ]
        with (
            patch.object(self.config_module, "get_all_models", return_value=inventory),
            patch.object(self.config_module, "get_router_settings", return_value={}),
            patch.object(self.config_module, "get_current_public_model_hub", return_value=[]),
            patch.object(
                self.config_module,
                "delete_model_by_id",
                side_effect=[(False, "failed"), (True, "ok")],
            ) as delete,
        ):
            self.assertFalse(self.config_module.prune_models({"models": []}))
        self.assertEqual(delete.call_count, 1)

    def test_model_prune_reads_back_each_delete_before_next_delete(self):
        inventory = [
            {
                "model_name": name,
                "litellm_params": {},
                "model_info": {"id": f"{name}-id"},
            }
            for name in ("stale-one", "stale-two")
        ]
        with (
            patch.object(
                self.config_module,
                "get_all_models",
                side_effect=[inventory, inventory],
            ),
            patch.object(self.config_module, "get_router_settings", return_value={}),
            patch.object(self.config_module, "get_current_public_model_hub", return_value=[]),
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(True, "accepted"),
            ) as delete,
        ):
            self.assertFalse(self.config_module.prune_models({"models": []}))
        delete.assert_called_once_with("stale-one-id")

    async def test_model_writes_stop_after_first_failure(self):
        payloads = [
            {"model_name": name, "litellm_params": {}, "model_info": {}}
            for name in ("first", "second")
        ]
        with (
            patch.object(self.config_module, "get_actor_from_key", return_value="actor"),
            patch.object(self.config_module, "get_all_models", return_value=[]),
            patch.object(
                self.config_module,
                "_create_model",
                side_effect=[(False, None, 0), (True, "created", 0)],
            ) as create,
        ):
            success = await self.config_module.sync_models({"models": payloads})
        self.assertFalse(success)
        self.assertEqual(create.call_count, 1)

    def test_model_readback_requires_desired_configuration(self):
        config = {
            "models": [
                {
                    "model_name": "provider/model",
                    "litellm_params": {
                        "model": "openai/new-model",
                        "litellm_credential_name": "credential",
                    },
                    "model_info": {},
                }
            ]
        }
        existing = [
            {
                "model_name": "provider/model",
                "litellm_params": {
                    "model": "openai/old-model",
                    "litellm_credential_name": "credential",
                },
                "model_info": {"id": "old-id"},
            }
        ]
        self.assertFalse(self.config_module.verify_models(config, existing))

    def test_model_prune_rejects_duplicate_inventory_before_delete(self):
        desired = {
            "model_name": "provider/model",
            "litellm_params": {
                "model": "openai/new-model",
                "litellm_credential_name": "credential",
            },
            "model_info": {},
        }
        inventory = [
            {**desired, "model_info": {"id": "keep-id"}},
            {**desired, "model_info": {"id": "duplicate-id"}},
            {
                "model_name": "stale/model",
                "litellm_params": {},
                "model_info": {"id": "stale-id"},
            },
        ]
        with (
            patch.object(
                self.config_module,
                "get_all_models",
                side_effect=[inventory, [inventory[0]]],
            ),
            patch.object(self.config_module, "get_router_settings", return_value={}),
            patch.object(self.config_module, "get_current_public_model_hub", return_value=[]),
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(True, "deleted"),
            ) as delete,
        ):
            self.assertFalse(self.config_module.prune_models({"models": [desired]}))

        delete.assert_not_called()

    def test_model_prune_requires_post_delete_readback(self):
        desired = {
            "model_name": "provider/model",
            "litellm_params": {},
            "model_info": {},
        }
        inventory = [
            {**desired, "model_info": {"id": "keep-id"}},
            {
                "model_name": "stale/model",
                "litellm_params": {},
                "model_info": {"id": "stale-id"},
            },
        ]
        with (
            patch.object(
                self.config_module,
                "get_all_models",
                side_effect=[inventory, inventory],
            ),
            patch.object(self.config_module, "get_router_settings", return_value={}),
            patch.object(self.config_module, "get_current_public_model_hub", return_value=[]),
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(True, "accepted"),
            ),
        ):
            self.assertFalse(
                self.config_module.prune_models({"models": [desired]})
            )
    def test_forced_replacement_requires_distinct_readback_before_delete(self):
        existing = {
            "model_name": "provider/model",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {"id": "old-model-id"},
        }
        payload = {
            "model_name": "provider/model",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {},
        }
        with (
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "accepted"),
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[existing],
            ),
            patch.object(self.config_module, "delete_model_by_id") as delete,
        ):
            success, action, deleted = self.config_module._create_model(
                payload,
                True,
                "tester",
                {("provider/model", "credential"): [existing]},
            )

        self.assertFalse(success)
        self.assertIsNone(action)
        self.assertEqual(deleted, 0)
        delete.assert_not_called()
    def test_forced_replacement_rejects_any_extra_same_identity_candidate(self):
        existing = {
            "model_name": "provider/model",
            "litellm_params": {
                "model": "openai/old-model",
                "litellm_credential_name": "credential",
            },
            "model_info": {"id": "old-model-id"},
        }
        payload = {
            "model_name": "provider/model",
            "litellm_params": {
                "model": "openai/new-model",
                "litellm_credential_name": "credential",
            },
            "model_info": {},
        }
        correct = {**payload, "model_info": {"id": "correct-new-id"}}
        wrong = {
            **payload,
            "litellm_params": {
                "model": "openai/wrong-model",
                "litellm_credential_name": "credential",
            },
            "model_info": {"id": "wrong-new-id"},
        }
        with (
            patch.object(self.config_module, "post_request", return_value=(True, "accepted")),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[existing, correct, wrong],
            ),
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(True, "accepted"),
            ) as delete,
        ):
            success, action, deleted = self.config_module._create_model(
                payload,
                True,
                "tester",
                {("provider/model", "credential"): [existing]},
            )

        self.assertFalse(success)
        self.assertIsNone(action)
        self.assertEqual(deleted, 0)
        delete.assert_not_called()

    async def test_model_sync_reports_desired_model_failure(self):
        config = {
            "models": [
                {
                    "model_name": "desired",
                    "litellm_params": {},
                    "model_info": {},
                }
            ]
        }
        with (
            patch.object(self.config_module, "get_actor_from_key", return_value="tester"),
            patch.object(self.config_module, "get_all_models", return_value=[]) as inventory,
            patch.object(
                self.config_module,
                "_create_model",
                return_value=(False, None, 0),
            ),
            patch.object(self.config_module, "post_request") as delete,
        ):
            succeeded = await self.config_module.sync_models(config)
        self.assertFalse(succeeded)
        self.assertEqual(inventory.call_count, 1)
        delete.assert_not_called()

    def test_model_inventory_failure_is_not_treated_as_empty(self):
        with patch.object(
            self.config_module,
            "get_request",
            return_value=(False, "service unavailable"),
        ):
            with self.assertRaisesRegex(CommandError, "model inventory"):
                self.config_module.get_all_models()

    def test_forced_replacement_reports_failure_when_old_model_delete_fails(self):
        existing = {
            "model_name": "provider/model",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {"id": "model-id"},
        }
        payload = {
            "model_name": "provider/model",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {},
        }
        replacement = {
            **payload,
            "model_info": {"id": "new-model-id"},
        }
        with (
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(False, "delete failed"),
            ),
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "created"),
            ) as create,
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[existing, replacement],
            ),
        ):
            success, action, deleted = self.config_module._create_model(
                payload,
                True,
                "tester",
                {("provider/model", "credential"): [existing]},
            )

        self.assertFalse(success)
        self.assertIsNone(action)
        self.assertEqual(deleted, 0)
        create.assert_called_once()

    def test_replacement_reads_back_each_old_id_before_next_delete(self):
        old_one = {
            "model_name": "provider/model",
            "litellm_params": {
                "model": "openai/old",
                "litellm_credential_name": "credential",
            },
            "model_info": {"id": "old-one"},
        }
        old_two = {**old_one, "model_info": {"id": "old-two"}}
        payload = {
            "model_name": "provider/model",
            "litellm_params": {
                "model": "openai/new",
                "litellm_credential_name": "credential",
            },
            "model_info": {},
        }
        replacement = {**payload, "model_info": {"id": "new-id"}}
        unchanged = [old_one, old_two, replacement]
        with (
            patch.object(self.config_module, "post_request", return_value=(True, "accepted")),
            patch.object(
                self.config_module,
                "get_all_models",
                side_effect=[unchanged, unchanged],
            ),
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(True, "accepted"),
            ) as delete,
        ):
            success, action, deleted = self.config_module._create_model(
                payload,
                True,
                "tester",
                {("provider/model", "credential"): [old_one, old_two]},
            )

        self.assertFalse(success)
        self.assertIsNone(action)
        self.assertEqual(deleted, 0)
        delete.assert_called_once_with("old-one")

    def test_forced_replacement_keeps_old_model_when_create_fails(self):
        existing = {
            "model_name": "provider/model",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {"id": "model-id"},
        }
        payload = {
            "model_name": "provider/model",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {},
        }
        with (
            patch.object(
                self.config_module,
                "post_request",
                return_value=(False, "create failed"),
            ),
            patch.object(self.config_module, "delete_model_by_id") as delete,
        ):
            success, _, _ = self.config_module._create_model(
                payload,
                True,
                "tester",
                {("provider/model", "credential"): [existing]},
            )
        self.assertFalse(success)
        delete.assert_not_called()

    def test_actor_lookup_failure_does_not_expose_api_key_prefix(self):
        with (
            patch.object(self.config_module, "_get_api_key", return_value="super-secret-api-key"),
            patch.object(
                self.config_module,
                "request_json",
                side_effect=OSError("offline"),
            ),
        ):
            self.assertEqual(self.config_module.get_actor_from_key(), "unknown")

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


    def test_forced_replacement_can_defer_old_id_deletion(self):
        existing = {
            "model_name": "provider/model",
            "litellm_params": {
                "model": "openai/old-model",
                "litellm_credential_name": "credential",
            },
            "model_info": {"id": "old-id"},
        }
        desired = {
            "model_name": "provider/model",
            "litellm_params": {
                "model": "openai/new-model",
                "litellm_credential_name": "credential",
            },
            "model_info": {},
        }
        created = {**desired, "model_info": {"id": "new-id"}}
        pending = []
        with (
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "accepted"),
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[existing, created],
            ),
            patch.object(self.config_module, "delete_model_by_id") as delete,
        ):
            success, action, deleted = self.config_module._create_model(
                desired,
                True,
                "actor",
                {("provider/model", "credential"): [existing]},
                pending_replacements=pending,
            )
        self.assertTrue(success)
        self.assertEqual(action, "replaced")
        self.assertEqual(deleted, 0)
        self.assertEqual(pending[0]["old_ids"], {"old-id"})
        self.assertEqual(pending[0]["new_id"], "new-id")
        delete.assert_not_called()


class GuardrailSyncTest(ConfigModuleTestMixin, unittest.TestCase):
    def test_guardrail_readback_preserves_cumulative_desired_state(self):
        second = {**self.headroom, "guardrail_name": "second"}
        with (
            patch.object(
                self.config_module,
                "create_guardrail",
                return_value=(True, "accepted"),
            ),
            patch.object(
                self.config_module,
                "get_all_guardrails",
                side_effect=[(True, [self.headroom]), (True, [second])],
            ),
        ):
            self.assertFalse(
                self.config_module.sync_guardrails(
                    {"guardrails": [self.headroom, second]},
                    initial_inventory=[],
                )
            )

    def test_guardrail_readback_preserves_unmanaged_state(self):
        unmanaged = {
            "guardrail_name": "unmanaged",
            "guardrail_id": "unmanaged-id",
            "litellm_params": {"guardrail": "custom", "mode": "pre_call"},
        }
        with (
            patch.object(
                self.config_module,
                "create_guardrail",
                return_value=(True, "accepted"),
            ),
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, [self.headroom]),
            ),
        ):
            self.assertFalse(
                self.config_module.sync_guardrails(
                    {"guardrails": [self.headroom]},
                    initial_inventory=[unmanaged],
                )
            )

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

    def test_rejects_duplicate_guardrail_names_before_inventory_or_write(self):
        duplicate = {
            **self.headroom,
            "litellm_params": {
                **self.headroom["litellm_params"],
                "guardrail": "other",
            },
        }
        with (
            patch.object(self.config_module, "get_all_guardrails") as inventory,
            patch.object(self.config_module, "create_guardrail") as create,
            patch.object(self.config_module, "update_guardrail") as update,
        ):
            self.assertFalse(
                self.config_module.sync_guardrails(
                    {"guardrails": [self.headroom, duplicate]}
                )
            )
        inventory.assert_not_called()
        create.assert_not_called()
        update.assert_not_called()

    def test_creates_missing_guardrail(self):
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                side_effect=[
                    (True, []),
                    (True, [{**self.headroom, "guardrail_id": "guardrail-1"}]),
                ],
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

    def test_skipped_guardrail_rejects_full_object_drift(self):
        existing = {
            **self.headroom,
            "guardrail_id": "guardrail-1",
            "server_metadata": {"revision": 1},
        }
        drifted = {
            **existing,
            "server_metadata": {"revision": 2},
        }
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, [drifted]),
            ),
            patch.object(self.config_module, "create_guardrail") as create,
            patch.object(self.config_module, "update_guardrail") as update,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom]},
                initial_inventory=[existing],
            )

        self.assertFalse(success)
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
                side_effect=[
                    (True, [existing]),
                    (True, [{**self.headroom, "guardrail_id": "guardrail-1"}]),
                ],
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
                side_effect=[
                    (True, [existing]),
                    (
                        True,
                        [
                            existing,
                            {**self.headroom, "guardrail_id": "guardrail-1"},
                        ],
                    ),
                ],
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

    def test_malformed_guardrail_inventory_blocks_mutation(self):
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, [{"unexpected": "shape"}]),
            ),
            patch.object(self.config_module, "create_guardrail") as create,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom]}
            )
        self.assertFalse(success)
        create.assert_not_called()

    def test_guardrail_write_failure_stops_later_writes(self):
        other = {**self.headroom, "guardrail_name": "second"}
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, []),
            ),
            patch.object(
                self.config_module,
                "create_guardrail",
                side_effect=[(False, "failed"), (True, "ok")],
            ) as create,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom, other]}
            )
        self.assertFalse(success)
        self.assertEqual(create.call_count, 1)

    def test_guardrail_write_requires_converged_readback(self):
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                side_effect=[(True, []), (True, [])],
            ),
            patch.object(
                self.config_module,
                "create_guardrail",
                return_value=(True, "ok"),
            ),
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom]}
            )
        self.assertFalse(success)

    def test_guardrail_failed_readback_blocks_later_writes(self):
        other = {**self.headroom, "guardrail_name": "second"}
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                side_effect=[(True, []), (True, [])],
            ),
            patch.object(
                self.config_module,
                "create_guardrail",
                return_value=(True, "ok"),
            ) as create,
        ):
            success = self.config_module.sync_guardrails(
                {"guardrails": [self.headroom, other]}
            )
        self.assertFalse(success)
        self.assertEqual(create.call_count, 1)


    def test_guardrail_plan_validates_all_update_ids_before_first_write(self):
        second = {**self.headroom, "guardrail_name": "second"}
        existing = [
            {
                "guardrail_name": self.headroom["guardrail_name"],
                "guardrail_id": "first-id",
                "litellm_params": {"guardrail": "old", "mode": "pre_call"},
            },
            {
                "guardrail_name": "second",
                "litellm_params": {"guardrail": "old", "mode": "pre_call"},
            },
        ]
        with patch.object(self.config_module, "update_guardrail") as update:
            self.assertFalse(
                self.config_module.sync_guardrails(
                    {"guardrails": [self.headroom, second]},
                    initial_inventory=existing,
                )
            )
        update.assert_not_called()

    def test_skipped_guardrail_is_freshly_verified_before_next_write(self):
        second = {**self.headroom, "guardrail_name": "second"}
        with (
            patch.object(
                self.config_module,
                "get_all_guardrails",
                return_value=(True, []),
            ),
            patch.object(self.config_module, "create_guardrail") as create,
        ):
            self.assertFalse(
                self.config_module.sync_guardrails(
                    {"guardrails": [self.headroom, second]},
                    initial_inventory=[self.headroom],
                )
            )
        create.assert_not_called()


class ConfigSyncCommandTest(ConfigModuleTestMixin, unittest.IsolatedAsyncioTestCase):
    async def test_models_only_requires_live_credential_dependencies_before_write(self):
        credential = {
            "credential_name": "missing-live",
            "credential_values": {"api_key": "secret"},
        }
        model = {
            "model_name": "model",
            "litellm_params": {
                "model": "provider/model",
                "litellm_credential_name": "missing-live",
            },
            "model_info": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value={"credentials": [credential], "models": [model]},
                ),
                patch.object(
                    self.config_module,
                    "get_credential_inventory",
                    return_value=[],
                ) as credential_inventory,
                patch.object(self.config_module, "get_all_models", return_value=[]),
                patch.object(self.config_module, "sync_models", return_value=True) as sync_models,
                patch.object(self.config_module, "verify_models", return_value=True),
                self.assertRaisesRegex(CommandError, "credential"),
            ):
                await self.config_module.sync_config(
                    **self.options(only="models"),
                    root=Path(tmpdir),
                )
        credential_inventory.assert_called_once_with()
        sync_models.assert_not_called()

    async def test_replacement_deletes_wait_for_complete_credential_prune_plan(self):
        desired = {
            "model_name": "model",
            "litellm_params": {"model": "provider/new"},
            "model_info": {},
        }
        old = {
            "model_name": "model",
            "litellm_params": {"model": "provider/old"},
            "model_info": {"id": "old-id"},
        }
        new = {**desired, "model_info": {"id": "new-id"}}

        async def prepare_models(*args, pending_replacements, **kwargs):
            pending_replacements.append(
                {
                    "key": ("model", ""),
                    "payload": desired,
                    "old_ids": {"old-id"},
                    "new_id": "new-id",
                }
            )
            return True

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value={"credentials": [], "models": [desired]},
                ),
                patch.object(
                    self.config_module,
                    "_preflight_inventories",
                    return_value={
                        "credentials": [],
                        "models": [old],
                        "router_settings": {},
                        "public_model_hub": [],
                        "guardrails": None,
                    },
                ),
                patch.object(self.config_module, "sync_credentials", return_value=True),
                patch.object(self.config_module, "verify_credentials", return_value=True),
                patch.object(self.config_module, "sync_models", side_effect=prepare_models),
                patch.object(self.config_module, "get_all_models", return_value=[old, new]),
                patch.object(self.config_module, "verify_models", return_value=True),
                patch.object(
                    self.config_module,
                    "_plan_model_prune",
                    return_value=([old], {("model", ""): desired}, [old, new]),
                ),
                patch.object(
                    self.config_module,
                    "_plan_credential_prune",
                    side_effect=CommandError("credential prune plan failed"),
                ),
                patch.object(self.config_module, "prune_models") as prune_models,
                self.assertRaisesRegex(CommandError, "credential prune plan failed"),
            ):
                await self.config_module.sync_config(
                    **self.options(
                        only="credentials,models",
                        force=True,
                        prune=True,
                    ),
                    root=Path(tmpdir),
                )
        prune_models.assert_not_called()

    def test_model_prune_verifies_retained_state_after_each_delete(self):
        desired = {
            "model_name": "keep",
            "litellm_params": {"model": "provider/desired"},
            "model_info": {},
        }
        kept = {**desired, "model_info": {"id": "keep-id"}}
        drifted = {
            **kept,
            "litellm_params": {"model": "provider/drifted"},
        }
        stale = [
            {
                "model_name": name,
                "litellm_params": {},
                "model_info": {"id": f"{name}-id"},
            }
            for name in ("stale-one", "stale-two")
        ]
        plan = (stale, {("keep", ""): desired}, [kept, *stale])
        with (
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[drifted, stale[1]],
            ),
            patch.object(
                self.config_module,
                "delete_model_by_id",
                return_value=(True, "accepted"),
            ) as delete,
        ):
            self.assertFalse(
                self.config_module.prune_models({"models": [desired]}, plan=plan)
            )
        delete.assert_called_once_with("stale-one-id")

    def test_credential_prune_rejects_hidden_retained_values_before_next_delete(self):
        desired = {
            "credential_name": "keep",
            "credential_values": {"api_key": "secret"},
        }
        hidden = {"credential_name": "keep", "credential_info": {}}
        plan = (["stale-one", "stale-two"], {"keep"}, [desired])
        with (
            patch.object(
                self.config_module,
                "get_credential_inventory",
                return_value=[hidden, {"credential_name": "stale-two"}],
            ),
            patch.object(
                self.config_module,
                "delete_credential",
                return_value=(True, "accepted"),
            ) as delete,
        ):
            self.assertFalse(
                self.config_module.prune_credentials(
                    {"credentials": [desired]},
                    plan=plan,
                )
            )
        delete.assert_called_once_with("stale-one")

    async def test_duplicate_desired_models_fail_before_credential_mutation(self):
        model = {
            "model_name": "duplicate",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value={"credentials": [], "models": [model, dict(model)]},
                ),
                patch.object(self.config_module, "sync_credentials") as credentials,
                self.assertRaisesRegex(CommandError, "duplicate model definitions"),
            ):
                await self.config_module.sync_config(
                    **self.options(only="credentials,models"),
                    root=Path(tmpdir),
                )
        credentials.assert_not_called()

    async def test_duplicate_guardrails_fail_before_credential_mutation(self):
        first = {
            "guardrail_name": "duplicate",
            "litellm_params": {"guardrail": "first", "mode": "pre_call"},
        }
        second = {
            "guardrail_name": "duplicate",
            "litellm_params": {"guardrail": "second", "mode": "pre_call"},
        }
        config = {
            "credentials": [],
            "guardrails": [first, second],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value=config,
                ),
                patch.object(self.config_module, "get_credential_inventory") as inventory,
                patch.object(self.config_module, "sync_credentials") as credentials,
                self.assertRaisesRegex(CommandError, "duplicate guardrail"),
            ):
                await self.config_module.sync_config(
                    **self.options(only="credentials,guardrails"),
                    root=Path(tmpdir),
                )
        inventory.assert_not_called()
        credentials.assert_not_called()

    async def test_late_model_inventory_failure_blocks_credential_mutation(self):
        config = {
            "credentials": [
                {"credential_name": "credential", "credential_values": {"api_key": "key"}}
            ],
            "models": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value=config,
                ),
                patch.object(
                    self.config_module,
                    "sync_credentials",
                    return_value=True,
                ) as credentials,
                patch.object(self.config_module, "verify_credentials", return_value=True),
                patch.object(
                    self.config_module,
                    "get_credential_inventory",
                    return_value=[],
                ),
                patch.object(self.config_module, "get_actor_from_key", return_value="actor"),
                patch.object(
                    self.config_module,
                    "get_all_models",
                    side_effect=CommandError("model inventory failed"),
                ),
                self.assertRaisesRegex(CommandError, "model inventory failed"),
            ):
                await self.config_module.sync_config(
                    **self.options(only="credentials,models"),
                    root=Path(tmpdir),
                )
        credentials.assert_not_called()

    async def test_last_selected_inventory_failure_blocks_first_mutation(self):
        guardrail = {
            "guardrail_name": "guardrail",
            "litellm_params": {"guardrail": "provider", "mode": "pre_call"},
        }
        config = {
            "credentials": [
                {"credential_name": "credential", "credential_values": {"api_key": "key"}}
            ],
            "models": [],
            "aliases": {},
            "fallbacks": [],
            "public_model_hub": [],
            "router_settings": {"timeout": 30},
            "guardrails": [guardrail],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value=config,
                ),
                patch.object(self.config_module, "get_credential_inventory", return_value=[]),
                patch.object(self.config_module, "get_all_models", return_value=[]),
                patch.object(self.config_module, "get_router_settings", return_value={}),
                patch.object(self.config_module, "get_current_public_model_hub", return_value=[]),
                patch.object(
                    self.config_module,
                    "get_all_guardrails",
                    return_value=(False, "malformed guardrail inventory"),
                ),
                patch.object(self.config_module, "sync_credentials") as credentials,
                self.assertRaisesRegex(CommandError, "guardrail inventory"),
            ):
                await self.config_module.sync_config(
                    **self.options(
                        only="credentials,models,aliases,fallbacks,public_model_hub,router_settings,guardrails"
                    ),
                    root=Path(tmpdir),
                )
        credentials.assert_not_called()

    async def test_credential_prune_preflight_blocks_first_model_delete(self):
        stale = {
            "model_name": "stale",
            "litellm_params": {},
            "model_info": {"id": "stale-id"},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value={"credentials": [], "models": []},
                ),
                patch.object(
                    self.config_module,
                    "_preflight_inventories",
                    return_value={
                        "credentials": [],
                        "models": [],
                        "router_settings": {},
                        "public_model_hub": [],
                        "guardrails": None,
                    },
                ),
                patch.object(self.config_module, "sync_credentials", return_value=True),
                patch.object(self.config_module, "verify_credentials", return_value=True),
                patch.object(self.config_module, "sync_models", return_value=True),
                patch.object(self.config_module, "verify_models", return_value=True),
                patch.object(
                    self.config_module,
                    "_plan_model_prune",
                    return_value=([stale], {}, [stale]),
                ),
                patch.object(
                    self.config_module,
                    "_plan_credential_prune",
                    side_effect=CommandError("credential inventory failed"),
                ),
                patch.object(self.config_module, "prune_models") as prune_models,
                self.assertRaisesRegex(CommandError, "credential inventory failed"),
            ):
                await self.config_module.sync_config(
                    **self.options(only="credentials,models", prune=True),
                    root=Path(tmpdir),
                )
        prune_models.assert_not_called()

    async def test_omitted_models_do_not_hide_live_credential_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value={"credentials": [], "models": None},
                ),
                patch.object(
                    self.config_module,
                    "_preflight_inventories",
                    return_value={
                        "credentials": [],
                        "models": [],
                        "router_settings": {},
                        "public_model_hub": [],
                        "guardrails": None,
                    },
                ),
                patch.object(self.config_module, "sync_credentials", return_value=True),
                patch.object(self.config_module, "verify_credentials", return_value=True),
                patch.object(self.config_module, "sync_models", return_value=True),
                patch.object(self.config_module, "verify_models", return_value=True),
                patch.object(
                    self.config_module,
                    "_plan_model_prune",
                    return_value=([], {}, []),
                ),
                patch.object(
                    self.config_module,
                    "_plan_credential_prune",
                    return_value=([], set(), []),
                ) as credential_plan,
                patch.object(self.config_module, "prune_models", return_value=True),
                patch.object(self.config_module, "prune_credentials", return_value=True),
            ):
                await self.config_module.sync_config(
                    **self.options(only="credentials,models", prune=True),
                    root=Path(tmpdir),
                )
        self.assertIsNone(credential_plan.call_args.kwargs["live_models"])

    def test_omitted_credentials_skip_prune_without_inventory_reads(self):
        with patch.object(self.config_module, "get_credential_inventory") as inventory:
            self.assertTrue(
                self.config_module.prune_credentials({"credentials": None})
            )
        inventory.assert_not_called()

    async def test_sync_threads_alternate_root_to_preset_generation(self):
        custom_root = Path("/tmp/custom-llmproxy-root")
        options = dict(
            only="aliases",
            force=False,
            prune=False,
            dry_run=True,
            config_path=None,
            preset="all",
        )
        with patch.object(
            self.config_module,
            "generate_config_for_preset",
            return_value={},
        ) as generate:
            self.assertEqual(
                await self.config_module.sync_config(**options, root=custom_root),
                0,
            )
        generate.assert_called_once_with(
            "all",
            require_complete=True,
            root=custom_root,
        )

    def test_model_inventory_rejects_untrusted_nested_identifiers(self):
        bad_entries = (
            {"model_name": "", "litellm_params": {}, "model_info": {"id": "id"}},
            {
                "model_name": "model",
                "litellm_params": {"litellm_credential_name": []},
                "model_info": {"id": "id"},
            },
            {"model_name": "model", "litellm_params": {}, "model_info": {"id": []}},
        )
        for entry in bad_entries:
            with self.subTest(entry=entry), patch.object(
                self.config_module,
                "get_request",
                return_value=(True, {"data": [entry]}),
            ), self.assertRaisesRegex(CommandError, "invalid entry"):
                self.config_module.get_all_models()

    def test_router_inventory_rejects_invalid_reserved_shapes(self):
        for current_values in (
            {"model_group_alias": []},
            {"model_group_alias": {"alias": []}},
            {"fallbacks": {}},
            {"fallbacks": [{"source": "target"}]},
        ):
            with self.subTest(current_values=current_values), patch.object(
                self.config_module,
                "get_request",
                return_value=(True, {"current_values": current_values}),
            ), self.assertRaisesRegex(CommandError, "Router settings"):
                self.config_module.get_router_settings()

    def test_model_inventory_rejects_non_string_ids_and_credential_names(self):
        invalid_models = [
            {
                "model_name": "model",
                "litellm_params": {},
                "model_info": {"id": ["unhashable"]},
            },
            {
                "model_name": "model",
                "litellm_params": {"litellm_credential_name": ["unhashable"]},
                "model_info": {"id": "id"},
            },
        ]
        for model in invalid_models:
            with self.subTest(model=model), patch.object(
                self.config_module,
                "get_request",
                return_value=(True, {"data": [model]}),
            ), self.assertRaisesRegex(CommandError, "Model inventory"):
                self.config_module.get_all_models()

    def test_credential_inventory_rejects_non_string_name(self):
        with patch.object(
            self.config_module,
            "get_request",
            return_value=(True, {"credentials": [{"credential_name": []}]}),
        ), self.assertRaisesRegex(CommandError, "Credential inventory"):
            self.config_module.get_credential_inventory()

    async def test_credential_is_read_back_before_next_write(self):
        credentials = [
            {"credential_name": name, "credential_values": {"api_key": name}}
            for name in ("first", "second")
        ]
        with (
            patch.object(
                self.config_module,
                "create_credential",
                side_effect=[(True, "ok", "created"), (True, "ok", "created")],
            ) as create,
            patch.object(
                self.config_module,
                "get_credential_inventory",
                return_value=[],
            ),
        ):
            self.assertFalse(
                await self.config_module.sync_credentials({"credentials": credentials})
            )
        self.assertEqual(create.call_count, 1)

    async def test_skipped_stale_credential_blocks_next_write(self):
        first = {
            "credential_name": "first",
            "credential_values": {"api_key": "desired"},
        }
        second = {
            "credential_name": "second",
            "credential_values": {"api_key": "second"},
        }
        stale = {
            "credential_name": "first",
            "credential_values": {"api_key": "stale"},
            "credential_info": {"_llmproxy_config_fingerprint": "wrong"},
        }
        with (
            patch.object(
                self.config_module,
                "get_credential_inventory",
                return_value=[stale],
            ),
            patch.object(
                self.config_module,
                "post_request",
                return_value=(True, "accepted"),
            ) as post,
        ):
            self.assertFalse(
                await self.config_module.sync_credentials(
                    {"credentials": [first, second]}
                )
            )
        post.assert_not_called()

    def test_credential_verification_accepts_litellm_mask_with_matching_fingerprint(self):
        desired = {
            "credential_name": "credential",
            "credential_values": {
                "api_key": "synthetic-secret-value",
                "api_base": "https://provider.test/v1",
            },
            "credential_info": {"description": "desired"},
        }
        actual = self.config_module._credential_payload_with_fingerprint(desired)
        actual["credential_values"] = {
            "api_key": "sy****ue",
            "api_base": "https://provider.test/v1",
        }
        self.assertTrue(
            self.config_module.verify_credentials({"credentials": [desired]}, [actual])
        )

    def test_credential_verification_checks_values_and_info(self):
        config = {
            "credentials": [
                {
                    "credential_name": "credential",
                    "credential_values": {"api_key": "desired"},
                    "credential_info": {"description": "desired"},
                }
            ]
        }
        actual = [
            {
                "credential_name": "credential",
                "credential_values": {"api_key": "wrong"},
                "credential_info": {"description": "wrong"},
            }
        ]
        self.assertFalse(self.config_module.verify_credentials(config, actual))

    def test_credential_verification_rejects_opaque_fingerprint_without_values(self):
        desired = {
            "credential_name": "credential",
            "credential_values": {"api_key": "desired"},
            "credential_info": {"description": "desired"},
        }
        fingerprint = self.config_module._credential_fingerprint(desired)
        actual = [
            {
                "credential_name": "credential",
                "credential_info": {
                    "description": "desired",
                    "_llmproxy_config_fingerprint": fingerprint,
                },
            }
        ]
        self.assertFalse(
            self.config_module.verify_credentials(
                {"credentials": [desired]}, actual
            )
        )

    def test_model_prune_preflights_all_references_before_delete(self):
        unreferenced = {
            "model_name": "delete-first",
            "litellm_params": {},
            "model_info": {"id": "delete-first-id"},
        }
        referenced = {
            "model_name": "referenced-second",
            "litellm_params": {},
            "model_info": {"id": "referenced-second-id"},
        }
        with (
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[unreferenced, referenced],
            ),
            patch.object(
                self.config_module,
                "get_router_settings",
                return_value={
                    "model_group_alias": {"alias": "referenced-second"},
                    "fallbacks": [],
                },
            ),
            patch.object(
                self.config_module, "get_current_public_model_hub", return_value=[]
            ),
            patch.object(self.config_module, "delete_model_by_id") as delete,
        ):
            self.assertFalse(self.config_module.prune_models({"models": []}))
        delete.assert_not_called()

    def test_credential_write_adds_opaque_fingerprint(self):
        desired = {
            "credential_name": "credential",
            "credential_values": {"api_key": "desired"},
        }
        with (
            patch.object(self.config_module, "credential_exists", return_value=False),
            patch.object(
                self.config_module, "post_request", return_value=(True, "ok")
            ) as post,
        ):
            self.config_module.create_credential(desired)
        payload = post.call_args.args[1]
        self.assertEqual(payload["credential_name"], "credential")
        self.assertNotEqual(
            payload["credential_info"]["_llmproxy_config_fingerprint"],
            desired["credential_values"]["api_key"],
        )

    def test_model_prune_refuses_live_routing_references(self):
        stale = {
            "model_name": "live-target",
            "litellm_params": {},
            "model_info": {"id": "stale-id"},
        }
        with (
            patch.object(self.config_module, "get_all_models", return_value=[stale]),
            patch.object(
                self.config_module,
                "get_router_settings",
                return_value={
                    "model_group_alias": {"alias": "live-target"},
                    "fallbacks": [],
                },
            ),
            patch.object(
                self.config_module, "get_current_public_model_hub", return_value=[]
            ),
            patch.object(self.config_module, "delete_model_by_id") as delete,
        ):
            self.assertFalse(self.config_module.prune_models({"models": []}))
        delete.assert_not_called()

    def test_credential_prune_stops_after_first_delete_failure(self):
        with patch.object(self.config_module, "_get_api_key", return_value="test-key"):
            inventory = [
                self.config_module._credential_payload_with_fingerprint(
                    {
                        "credential_name": name,
                        "credential_values": {"api_key": name},
                    }
                )
                for name in ("stale-one", "stale-two")
            ]
        with (
            patch.object(
                self.config_module,
                "get_credential_inventory",
                return_value=inventory,
            ),
            patch.object(self.config_module, "_get_api_key", return_value="test-key"),
            patch.object(self.config_module, "get_all_models", return_value=[]),
            patch.object(
                self.config_module,
                "delete_credential",
                side_effect=[(False, "failed"), (True, "ok")],
            ) as delete,
        ):
            self.assertFalse(
                self.config_module.prune_credentials({"credentials": []})
            )
        self.assertEqual(delete.call_count, 1)

    async def test_credential_writes_stop_after_first_failure(self):
        credentials = [
            {"credential_name": name, "credential_values": {}}
            for name in ("first", "second")
        ]
        with patch.object(
            self.config_module,
            "create_credential",
            side_effect=[(False, "failed", "created"), (True, "ok", "created")],
        ) as create:
            success = await self.config_module.sync_credentials(
                {"credentials": credentials}
            )
        self.assertFalse(success)
        self.assertEqual(create.call_count, 1)

    def test_endpoint_segments_are_percent_encoded(self):
        with (
            patch.object(
                self.config_module, "delete_request", return_value=(True, "ok")
            ) as delete,
            patch.object(
                self.config_module, "put_request", return_value=(True, "ok")
            ) as put,
        ):
            self.config_module.delete_credential("name/../x?y#z")
            self.config_module.update_guardrail("id/../x?y#z", {})

        delete.assert_called_once_with(
            "credentials/name%2F%2E%2E%2Fx%3Fy%23z"
        )
        put.assert_called_once_with(
            "guardrails/id%2F%2E%2E%2Fx%3Fy%23z", {"guardrail": {}}
        )

    def test_none_public_model_hub_leaves_state_unchanged(self):
        with (
            patch.object(self.config_module, "get_current_public_model_hub") as inventory,
            patch.object(self.config_module, "post_request") as post,
        ):
            success, _ = self.config_module.update_public_model_hub(None)
        self.assertTrue(success)
        inventory.assert_not_called()
        post.assert_not_called()

    def test_public_hub_write_requires_converged_readback(self):
        with (
            patch.object(
                self.config_module,
                "get_current_public_model_hub",
                side_effect=[[], []],
            ),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[
                    {
                        "model_name": "model",
                        "litellm_params": {},
                        "model_info": {"id": "id"},
                    }
                ],
            ),
            patch.object(self.config_module, "get_current_aliases", return_value={}),
            patch.object(
                self.config_module, "post_request", return_value=(True, "ok")
            ),
        ):
            success, _ = self.config_module.update_public_model_hub(["model"])
        self.assertFalse(success)

    def test_router_write_requires_converged_readback(self):
        with (
            patch.object(
                self.config_module,
                "get_router_settings",
                side_effect=[{}, {}],
            ),
            patch.object(
                self.config_module, "post_request", return_value=(True, "ok")
            ),
        ):
            success, _ = self.config_module.update_router_settings(
                {"routing_strategy": "usage-based-routing"}
            )
        self.assertFalse(success)

    def test_credential_prune_refuses_credentials_used_by_live_models(self):
        with (
            patch.object(
                self.config_module,
                "get_credential_inventory",
                return_value=[
                    {"credential_name": "desired"},
                    {"credential_name": "in-use-stale"},
                ],
            ),
            patch.object(self.config_module, "verify_credentials", return_value=True),
            patch.object(
                self.config_module,
                "get_all_models",
                return_value=[
                    {
                        "model_name": "live/model",
                        "litellm_params": {
                            "litellm_credential_name": "in-use-stale"
                        },
                        "model_info": {"id": "live-id"},
                    }
                ],
            ),
            patch.object(self.config_module, "delete_credential") as delete,
        ):
            success = self.config_module.prune_credentials(
                {
                    "credentials": [
                        {
                            "credential_name": "desired",
                            "credential_values": {},
                        }
                    ]
                }
            )

        self.assertFalse(success)
        delete.assert_not_called()

    def test_credential_prune_requires_post_delete_readback(self):
        config = {
            "credentials": [
                {"credential_name": "desired", "credential_values": {}}
            ]
        }
        with (
            patch.object(
                self.config_module,
                "get_credential_inventory",
                side_effect=[
                    [
                        {"credential_name": "desired"},
                        {"credential_name": "stale"},
                    ],
                    [
                        {"credential_name": "desired"},
                        {"credential_name": "stale"},
                    ],
                ],
            ),
            patch.object(self.config_module, "verify_credentials", return_value=True),
            patch.object(self.config_module, "get_all_models", return_value=[]),
            patch.object(
                self.config_module,
                "delete_credential",
                return_value=(True, "accepted"),
            ),
        ):
            self.assertFalse(self.config_module.prune_credentials(config))

    def test_credential_prune_reads_back_each_delete_before_next_delete(self):
        config = {
            "credentials": [
                {"credential_name": "desired", "credential_values": {}}
            ]
        }
        with patch.object(self.config_module, "_get_api_key", return_value="test-key"):
            desired = self.config_module._credential_payload_with_fingerprint(
                config["credentials"][0]
            )
            stale_one = self.config_module._credential_payload_with_fingerprint(
                {
                    "credential_name": "stale-one",
                    "credential_values": {"api_key": "one"},
                }
            )
            stale_two = self.config_module._credential_payload_with_fingerprint(
                {
                    "credential_name": "stale-two",
                    "credential_values": {"api_key": "two"},
                }
            )
        inventory = [desired, stale_one, stale_two]
        with (
            patch.object(
                self.config_module,
                "get_credential_inventory",
                side_effect=[inventory, inventory],
            ),
            patch.object(self.config_module, "_get_api_key", return_value="test-key"),
            patch.object(self.config_module, "verify_credentials", return_value=True),
            patch.object(self.config_module, "get_all_models", return_value=[]),
            patch.object(
                self.config_module,
                "delete_credential",
                return_value=(True, "accepted"),
            ) as delete,
        ):
            self.assertFalse(self.config_module.prune_credentials(config))
        delete.assert_called_once_with("stale-one")

    def test_credential_prune_revalidates_pending_evidence_after_each_delete(self):
        config = {
            "credentials": [
                {
                    "credential_name": "keep",
                    "credential_values": {"api_key": "keep-secret"},
                }
            ]
        }
        with patch.object(self.config_module, "_get_api_key", return_value="test-key"):
            initial = [
                self.config_module._credential_payload_with_fingerprint(item)
                for item in [
                    config["credentials"][0],
                    {
                        "credential_name": "stale-one",
                        "credential_values": {"api_key": "one"},
                    },
                    {
                        "credential_name": "stale-two",
                        "credential_values": {"api_key": "two"},
                    },
                ]
            ]
        incomplete_readback = [initial[0], {"credential_name": "stale-two"}]
        plan = (["stale-one", "stale-two"], {"keep"}, initial)
        with (
            patch.object(self.config_module, "_get_api_key", return_value="test-key"),
            patch.object(
                self.config_module,
                "get_credential_inventory",
                return_value=incomplete_readback,
            ),
            patch.object(
                self.config_module,
                "delete_credential",
                return_value=(True, "accepted"),
            ) as delete,
        ):
            self.assertFalse(
                self.config_module.prune_credentials(config, plan=plan)
            )
        delete.assert_called_once_with("stale-one")

    async def test_prune_runs_after_convergence_and_models_before_credentials(self):
        events = []

        async def sync_credentials(*_args, **_kwargs):
            events.append("sync-credentials")
            return True

        async def sync_models(*_args, **_kwargs):
            events.append("sync-models")
            return True

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value={"credentials": [], "models": []},
                ),
                patch.object(
                    self.config_module,
                    "_preflight_inventories",
                    return_value={
                        "credentials": [],
                        "models": [],
                        "router_settings": {},
                        "public_model_hub": [],
                        "guardrails": None,
                    },
                ),
                patch.object(
                    self.config_module,
                    "sync_credentials",
                    side_effect=sync_credentials,
                ),
                patch.object(
                    self.config_module,
                    "sync_models",
                    side_effect=sync_models,
                ),
                patch.object(
                    self.config_module,
                    "verify_credentials",
                    side_effect=lambda *_: events.append("verify-credentials") or True,
                ),
                patch.object(
                    self.config_module,
                    "verify_models",
                    side_effect=lambda *_: events.append("verify-models") or True,
                ),
                patch.object(
                    self.config_module,
                    "prune_models",
                    side_effect=lambda *_args, **_kwargs: events.append("prune-models") or True,
                ),
                patch.object(
                    self.config_module,
                    "prune_credentials",
                    side_effect=lambda *_args, **_kwargs: events.append("prune-credentials") or True,
                ),
                patch.object(
                    self.config_module,
                    "_plan_model_prune",
                    return_value=([], {}, []),
                ),
                patch.object(
                    self.config_module,
                    "_plan_credential_prune",
                    return_value=([], set(), []),
                ),
            ):
                await self.config_module.sync_config(
                    **self.options(
                        only="credentials,models",
                        prune=True,
                    ),
                    root=Path(tmpdir),
                )

        self.assertEqual(
            events,
            [
                "sync-credentials",
                "verify-credentials",
                "sync-models",
                "verify-models",
                "prune-models",
                "prune-credentials",
            ],
        )

    async def test_failed_readback_blocks_later_mutations_and_all_prune(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(
                    self.config_module,
                    "generate_config_for_preset",
                    return_value={"credentials": [], "models": [], "aliases": {}},
                ),
                patch.object(
                    self.config_module,
                    "_preflight_inventories",
                    return_value={
                        "credentials": [],
                        "models": [],
                        "router_settings": {},
                        "public_model_hub": [],
                        "guardrails": None,
                    },
                ),
                patch.object(self.config_module, "sync_credentials", return_value=True),
                patch.object(self.config_module, "verify_credentials", return_value=True),
                patch.object(self.config_module, "sync_models", return_value=True),
                patch.object(self.config_module, "verify_models", return_value=False),
                patch.object(self.config_module, "sync_aliases") as aliases,
                patch.object(self.config_module, "prune_models") as prune_models,
                patch.object(self.config_module, "prune_credentials") as prune_credentials,
                self.assertRaisesRegex(CommandError, "model readback"),
            ):
                await self.config_module.sync_config(
                    **self.options(
                        only="credentials,models,aliases",
                        prune=True,
                    ),
                    root=Path(tmpdir),
                )

        aliases.assert_not_called()
        prune_models.assert_not_called()
        prune_credentials.assert_not_called()
    def test_unresolved_public_hub_entry_is_rejected_before_update(self):
        with (
            patch.object(
                self.config_module,
                "get_current_public_model_hub",
                return_value=[],
            ),
            patch.object(self.config_module, "get_all_models", return_value=[]),
            patch.object(self.config_module, "get_current_aliases", return_value={}),
            patch.object(self.config_module, "post_request") as post,
        ):
            success, _ = self.config_module.update_public_model_hub(
                ["missing-model"]
            )
        self.assertFalse(success)
        post.assert_not_called()
    def test_malformed_inventories_are_rejected(self):
        cases = (
            ("get_all_credentials", {"credentials": [{}]}, "(?i)credential inventory"),
            ("get_all_models", {"data": [{}]}, "(?i)model inventory"),
            ("get_current_public_model_hub", [{}], "(?i)public model hub"),
        )
        for function_name, response, message in cases:
            with self.subTest(function=function_name):
                with patch.object(
                    self.config_module,
                    "get_request",
                    return_value=(True, response),
                ):
                    with self.assertRaisesRegex(CommandError, message):
                        getattr(self.config_module, function_name)()

    async def test_credential_sync_reports_desired_credential_failure(self):
        config = {
            "credentials": [
                {"credential_name": "desired", "credential_values": {}}
            ]
        }
        with (
            patch.object(
                self.config_module,
                "create_credential",
                return_value=(False, "failed", "created"),
            ),
            patch.object(self.config_module, "get_all_credentials") as inventory,
            patch.object(self.config_module, "delete_credential") as delete,
        ):
            succeeded = await self.config_module.sync_credentials(config)
        self.assertFalse(succeeded)
        inventory.assert_not_called()
        delete.assert_not_called()

    def test_credential_inventory_failure_blocks_creation(self):
        with (
            patch.object(
                self.config_module,
                "get_request",
                return_value=(False, "service unavailable"),
            ),
            patch.object(self.config_module, "post_request") as create,
        ):
            with self.assertRaisesRegex(CommandError, "credential inventory"):
                self.config_module.create_credential(
                    {"credential_name": "new", "credential_values": {}}
                )
        create.assert_not_called()

    def test_router_inventory_failure_blocks_update(self):
        with (
            patch.object(
                self.config_module,
                "get_request",
                return_value=(False, "service unavailable"),
            ),
            patch.object(self.config_module, "post_request") as update,
        ):
            with self.assertRaisesRegex(CommandError, "router settings"):
                self.config_module.update_router_settings({"fallbacks": []})
        update.assert_not_called()

    def test_public_hub_inventory_failure_blocks_update(self):
        with (
            patch.object(
                self.config_module,
                "get_request",
                return_value=(False, "service unavailable"),
            ),
            patch.object(self.config_module, "post_request") as update,
        ):
            with self.assertRaisesRegex(CommandError, "public model hub"):
                self.config_module.update_public_model_hub([])
        update.assert_not_called()

    @staticmethod
    def options(**overrides):
        values = {
            "only": "aliases",
            "force": False,
            "prune": False,
            "dry_run": False,
            "config_path": None,
            "preset": "all",
        }
        values.update(overrides)
        return values

    async def test_invalid_component_fails_before_reporting_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(CommandError, "Invalid components"):
                await self.config_module.sync_config(
                    **self.options(only="invalid_component"),
                    root=Path(tmpdir),
                )

    async def test_missing_config_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.json"
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                self.assertRaisesRegex(CommandError, "Config file not found"),
            ):
                await self.config_module.sync_config(
                    **self.options(config_path=missing, preset=None),
                    root=Path(tmpdir),
                )

    async def test_dry_run_still_validates_config_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.json"
            with self.assertRaisesRegex(CommandError, "Config file not found"):
                await self.config_module.sync_config(
                    **self.options(
                        config_path=missing,
                        preset=None,
                        dry_run=True,
                    ),
                    root=Path(tmpdir),
                )

    async def test_failed_alias_sync_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(self.config_module, "_get_api_key", return_value="key"),
                patch.object(self.config_module, "_get_base_url", return_value="http://litellm"),
                patch.object(self.config_module, "generate_config_for_preset", return_value={"aliases": {}}),
                patch.object(
                    self.config_module,
                    "_preflight_inventories",
                    return_value={
                        "credentials": None,
                        "models": None,
                        "router_settings": {},
                        "public_model_hub": None,
                        "guardrails": None,
                    },
                ),
                patch.object(self.config_module, "sync_aliases", return_value=False),
                self.assertRaisesRegex(CommandError, "synchronization operations failed"),
            ):
                await self.config_module.sync_config(
                    **self.options(),
                    root=Path(tmpdir),
                )


class FinalInventorySafetyTest(ConfigModuleTestMixin, unittest.TestCase):
    def test_duplicate_desired_credentials_are_rejected_before_inventory_or_write(self):
        credential = {
            "credential_name": "duplicate",
            "credential_values": {"api_key": "secret"},
            "credential_info": {},
        }
        with (
            patch.object(self.config_module, "get_credential_inventory") as inventory,
            patch.object(self.config_module, "post_request") as post,
            self.assertRaisesRegex(CommandError, "duplicate credential definitions"),
        ):
            asyncio.run(
                self.config_module.sync_credentials(
                    {"credentials": [credential, dict(credential)]}
                )
            )
        inventory.assert_not_called()
        post.assert_not_called()

    def test_replacement_requires_old_ids_absent_after_delete(self):
        existing = {
            "model_name": "provider/model",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {"id": "old-id"},
        }
        payload = {
            "model_name": "provider/model",
            "litellm_params": {"litellm_credential_name": "credential"},
            "model_info": {},
        }
        replacement = {**payload, "model_info": {"id": "new-id"}}
        with (
            patch.object(self.config_module, "post_request", return_value=(True, "created")),
            patch.object(self.config_module, "delete_model_by_id", return_value=(True, "accepted")),
            patch.object(
                self.config_module,
                "get_all_models",
                side_effect=[[existing, replacement], [existing, replacement]],
            ),
        ):
            success, _, _ = self.config_module._create_model(
                payload,
                True,
                "tester",
                {("provider/model", "credential"): [existing]},
            )
        self.assertFalse(success)

    def test_exact_routing_reference_with_glob_syntax_protects_model(self):
        live = {
            "model_name": "model[ab]",
            "litellm_params": {},
            "model_info": {"id": "bracket-id"},
        }
        with (
            patch.object(self.config_module, "get_all_models", return_value=[live]),
            patch.object(
                self.config_module,
                "get_router_settings",
                return_value={"model_group_alias": {"alias": "model[ab]"}},
            ),
            patch.object(self.config_module, "get_current_public_model_hub", return_value=[]),
            patch.object(self.config_module, "delete_model_by_id") as delete,
        ):
            self.assertFalse(self.config_module.prune_models({"models": []}))
        delete.assert_not_called()

    def test_duplicate_credential_names_are_rejected(self):
        result = {
            "credentials": [
                {"credential_name": "shared", "credential_values": {"api_key": "wrong"}},
                {"credential_name": "shared", "credential_values": {"api_key": "right"}},
            ]
        }
        with patch.object(self.config_module, "get_request", return_value=(True, result)):
            with self.assertRaisesRegex(CommandError, "duplicate credential"):
                self.config_module.get_credential_inventory()

    def test_duplicate_model_ids_are_rejected(self):
        result = {
            "data": [
                {"model_name": "one", "litellm_params": {}, "model_info": {"id": "same"}},
                {"model_name": "two", "litellm_params": {}, "model_info": {"id": "same"}},
            ]
        }
        with patch.object(self.config_module, "get_request", return_value=(True, result)):
            with self.assertRaisesRegex(CommandError, "duplicate model ID"):
                self.config_module.get_all_models()

    def test_malformed_known_router_scalar_is_rejected(self):
        result = {
            "current_values": {
                "model_group_alias": {},
                "fallbacks": [],
                "routing_strategy": [],
            }
        }
        with patch.object(self.config_module, "get_request", return_value=(True, result)):
            with self.assertRaisesRegex(CommandError, "routing_strategy"):
                self.config_module.get_router_settings()

    def test_all_preserved_known_router_scalars_are_validated(self):
        for field, value in (
            ("num_retries", []),
            ("timeout", []),
            ("cooldown_time", []),
        ):
            result = {
                "current_values": {
                    "model_group_alias": {},
                    "fallbacks": [],
                    field: value,
                }
            }
            with self.subTest(field=field):
                with patch.object(
                    self.config_module, "get_request", return_value=(True, result)
                ):
                    with self.assertRaisesRegex(CommandError, field):
                        self.config_module.get_router_settings()


if __name__ == "__main__":
    unittest.main()
