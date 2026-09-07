"""Model inventory must be complete before reconciliation can write or delete."""
import copy
import unittest
from unittest.mock import patch

from llmproxy.core.command import CommandError
from ..src import config_sync as sync


class ModelInventoryPaginationTest(unittest.TestCase):
    def model(self, identifier):
        return {"model_name": "model", "litellm_params": {}, "model_info": {"id": identifier}}

    def pages(self):
        return [
            {"data": [self.model("first"), self.model("second")],
             "total_count": 3, "current_page": 1, "total_pages": 2, "size": 2},
            {"data": [self.model("last")],
             "total_count": 3, "current_page": 2, "total_pages": 2, "size": 2},
        ]

    def test_reads_all_pages_including_new_replacement_on_last_page(self):
        with patch.object(sync, "get_request", side_effect=[(True, p) for p in self.pages()]) as get:
            models = sync.get_all_models()
        self.assertEqual([m["model_info"]["id"] for m in models], ["first", "second", "last"])
        self.assertEqual([call.args[0] for call in get.call_args_list], [
            "v2/model/info?include_team_models=true",
            "v2/model/info?include_team_models=true&page=2&size=2",
        ])

    def test_pagination_failure_or_inconsistent_metadata_is_rejected(self):
        cases = []
        for field, value in (("total_count", 4), ("total_pages", 3),
                             ("current_page", 1), ("size", 3), ("size", True)):
            pages = self.pages()
            pages[1][field] = value
            cases.append([(True, p) for p in pages])
        pages = self.pages()
        pages[1].pop("total_count")
        cases.append([(True, p) for p in pages])
        pages = self.pages()
        pages[1]["data"] = []
        cases.append([(True, p) for p in pages])
        cases.append([(True, self.pages()[0]), (False, "unavailable")])
        for index, responses in enumerate(cases):
            with self.subTest(case=index), patch.object(sync, "get_request", side_effect=responses):
                with self.assertRaises(CommandError):
                    sync.get_all_models()

    def test_duplicate_ids_across_pages_are_rejected(self):
        pages = self.pages()
        pages[1]["data"] = [copy.deepcopy(pages[0]["data"][0])]
        with patch.object(sync, "get_request", side_effect=[(True, p) for p in pages]):
            with self.assertRaisesRegex(CommandError, "duplicate model ID"):
                sync.get_all_models()

    def test_legacy_and_empty_inventories_remain_supported(self):
        for response in (
            {"data": []},
            {"data": [], "total_count": 0, "current_page": 1, "total_pages": 0, "size": 50},
        ):
            with self.subTest(response=response), patch.object(sync, "get_request", return_value=(True, response)):
                self.assertEqual(sync.get_all_models(), [])

    def test_partial_first_page_blocks_model_creation(self):
        page = self.pages()[0]
        page["data"] = page["data"][:1]
        with patch.object(sync, "get_request", return_value=(True, page)):
            with self.assertRaises(CommandError):
                sync.get_all_models()
