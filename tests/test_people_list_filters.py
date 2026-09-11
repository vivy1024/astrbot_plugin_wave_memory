import ast
import json
import math
import unittest
from pathlib import Path


def _load_filter():
    source_path = Path(__file__).resolve().parents[1] / "webui" / "blueprints" / "people.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    wanted = {
        "_optional_float",
        "_optional_int",
        "_normalized_sort_order",
        "_person_from_relationship",
        "_relationship_affinity",
        "_interaction_count",
        "_alias_count",
        "_filter_people_rows",
    }
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    module = ast.Module(body=body, type_ignores=[])
    namespace: dict[str, object] = {"Any": object, "Mapping": dict, "json": json, "math": math}
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["_filter_people_rows"]


def _person(user_id: str, name: str, *, affinity=None, interactions=0, aliases=()):
    return {
        "user_id": user_id,
        "display_name": name,
        "interaction_count": interactions,
        "aliases": list(aliases),
        "affinity": affinity,
    }


class PeopleListFilterTest(unittest.TestCase):
    def test_filters_and_sorts_full_set_before_pagination(self):
        filter_rows = _load_filter()
        rows = [
            _person("u1", "甲", affinity=20, interactions=3),
            _person("u2", "乙", affinity=None, interactions=40, aliases=["小乙"]),
            _person("u3", "丙", affinity=8, interactions=12),
        ]
        known = filter_rows(rows, query={"relationship_state": "known", "sort_by": "affinity", "sort_order": "desc"})
        self.assertEqual([item["user_id"] for item in known], ["u1", "u3"])
        high = filter_rows(rows, query={"min_affinity": 15})
        self.assertEqual([item["user_id"] for item in high], ["u1"])
        aliased = filter_rows(rows, query={"alias_filter": "has"})
        self.assertEqual([item["user_id"] for item in aliased], ["u2"])
        active = filter_rows(rows, query={"min_interactions": 10, "sort_by": "interactions", "sort_order": "desc"})
        self.assertEqual([item["user_id"] for item in active], ["u2", "u3"])

    def test_affinity_sort_puts_unrecorded_last_and_defaults_to_high_first(self):
        filter_rows = _load_filter()
        rows = [
            _person("u1", "甲", affinity=20, interactions=3),
            _person("u2", "乙", affinity=None, interactions=40),
            _person("u3", "丙", affinity=8, interactions=12),
            _person("u4", "丁", affinity=-3, interactions=1),
        ]
        high_first = filter_rows(rows, query={"sort_by": "affinity"})
        self.assertEqual([item["user_id"] for item in high_first], ["u1", "u3", "u4", "u2"])
        low_first = filter_rows(rows, query={"sort_by": "affinity", "sort_order": "asc"})
        self.assertEqual([item["user_id"] for item in low_first], ["u4", "u3", "u1", "u2"])

    def test_search_matches_display_fields_not_internal_payload(self):
        filter_rows = _load_filter()
        rows = [
            {**_person("u1", "甲", aliases=["小甲"]), "nickname": "甲昵称", "scope_key": "u1|g1|b1"},
            {
                **_person("u2", "乙", affinity=8),
                "metadata": {"affinity_status": "available", "impression": "内部字段"},
            },
        ]
        self.assertEqual([item["user_id"] for item in filter_rows(rows, query={"search": "甲"})], ["u1"])
        self.assertEqual([item["user_id"] for item in filter_rows(rows, query={"search": "小甲"})], ["u1"])
        self.assertEqual([item["user_id"] for item in filter_rows(rows, query={"search": "u1|g1|b1"})], ["u1"])
        self.assertEqual(filter_rows(rows, query={"search": "affinity_status"}), [])
        self.assertEqual(filter_rows(rows, query={"search": "内部字段"}), [])
