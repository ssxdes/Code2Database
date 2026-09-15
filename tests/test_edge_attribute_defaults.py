"""Build-stage edge-attribute defaults.

Scanners emit direct call edges without evidence (Go) and spawn_target
edges without call_condition (the spawn call itself is the trigger).
The build fills both once, before any consumer — graph construction,
master export, post-build validation — reads them, so downstream code
can trust the fields to exist.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.build.build_phases import _apply_edge_attribute_defaults


class TestEvidenceDefault(unittest.TestCase):

    def test_call_edge_without_evidence_gets_direct_call(self):
        edges = [{"source": "a", "target": "b", "call_order": 1}]
        _apply_edge_attribute_defaults(edges)
        self.assertEqual(edges[0]["evidence"], "direct_call")

    def test_existing_evidence_untouched(self):
        edges = [{"source": "a", "target": "b",
                  "evidence": [{"kind": "ast_call"}]}]
        _apply_edge_attribute_defaults(edges)
        self.assertEqual(edges[0]["evidence"], [{"kind": "ast_call"}])

    def test_relation_aware_marker(self):
        edges = [{"source": "a", "target": "b", "relation": "IMPORTS"},
                 {"source": "a", "target": "c", "relation": "CONTAINS"}]
        _apply_edge_attribute_defaults(edges)
        self.assertEqual(edges[0]["evidence"], "imports")
        self.assertEqual(edges[1]["evidence"], "contains")

    def test_cond_placeholder_parent_edges_skipped(self):
        # invoker -> func__cond_0 is branch scaffolding, not a call.
        edges = [{"source": "a", "target": "a__cond_0",
                  "call_order": None, "call_condition": "if(x)"}]
        _apply_edge_attribute_defaults(edges)
        self.assertNotIn("evidence", edges[0])


class TestSpawnCallConditionDefault(unittest.TestCase):

    def test_spawn_target_gets_spawn_condition(self):
        edges = [{"source": "a", "target": "worker",
                  "concurrency": "spawn_target"}]
        _apply_edge_attribute_defaults(edges)
        self.assertEqual(edges[0]["call_condition"], "spawn")

    def test_branch_condition_on_spawn_preserved(self):
        edges = [{"source": "a__cond_0", "target": "worker",
                  "concurrency": "spawn_target",
                  "call_condition": "if(ready)"}]
        _apply_edge_attribute_defaults(edges)
        self.assertEqual(edges[0]["call_condition"], "if(ready)")

    def test_other_concurrency_untouched(self):
        edges = [{"source": "a", "target": "b",
                  "concurrency": "callback"}]
        _apply_edge_attribute_defaults(edges)
        self.assertNotIn("call_condition", edges[0])


if __name__ == "__main__":
    unittest.main()
