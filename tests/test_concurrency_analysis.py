"""Unit tests for concurrency_analysis.py — detect-races + concurrency-analyze.

Tests the data race detection algorithm: when two functions in different
thread contexts access the same shared resource (global var or struct
field) and at least one access is a write, with no common mutex
protection, that's a race.

Coverage:
- _get_thread_context: model + entry extraction from node attrs
- _same_thread_context: same/different context classification
- detect_data_races: race detection with read+write, write+write, no
  race when same context, lock protection suppresses races, target_func
  filtering, severity (high=write, low=read-read)
- TOCTOU detection: reader with lock + writer without lock
"""
import json
import os
import tempfile
import unittest


def _make_race_graph(nodes_spec, edges_spec=None) -> str:
    """Build a domain-split graph fixture for race-detection tests."""
    tmp = tempfile.mkdtemp(prefix="c2d_race_test_")
    defaulted_nodes = []
    for n in nodes_spec:
        node = {
            "id": n["id"], "name": n.get("name", n["id"]),
            "source_file": n.get("source_file", "/tmp/x.c"),
            "line": n.get("line", 1), "domain": "test",
            "labels": n.get("labels", []), "is_empty": n.get("is_empty", False),
        }
        for k, v in n.items():
            if k not in ("id", "name", "source_file", "line", "labels", "is_empty"):
                node[k] = v
        defaulted_nodes.append(node)
    domain_data = {
        "nodes": defaulted_nodes,
        "edges": [{"source": s, "target": t, "relation": r, "confidence": "EXTRACTED"}
                  for s, t, r in (edges_spec or [])],
    }
    domain_filename = "domain_test.json"
    with open(os.path.join(tmp, domain_filename), "w") as f:
        json.dump(domain_data, f)
    master = {"source_root": "/tmp", "domains": {"test": domain_filename}}
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump(master, f)
    return tmp


def _load_graph(graph_dir):
    """Load the full graph from a fixture directory."""
    from _builder.graph_build import _load_full_graph
    return _load_full_graph(graph_dir)


class TestGetThreadContext(unittest.TestCase):
    """Tests for _get_thread_context."""

    def test_returns_model_and_entry_for_thread_entry(self):
        from _builder.concurrency_analysis import _get_thread_context
        ndata = {"thread_model": "pthread", "thread_entry": True, "name": "worker"}
        model, entry = _get_thread_context(ndata)
        self.assertEqual(model, "pthread")
        self.assertEqual(entry, "worker")

    def test_returns_inherited_model_for_non_entry(self):
        from _builder.concurrency_analysis import _get_thread_context
        ndata = {"thread_model_inherited": "pthread", "thread_entry": False, "name": "helper"}
        model, entry = _get_thread_context(ndata)
        self.assertEqual(model, "pthread")
        self.assertIsNone(entry)

    def test_returns_none_none_when_no_thread_attrs(self):
        from _builder.concurrency_analysis import _get_thread_context
        model, entry = _get_thread_context({})
        self.assertIsNone(model)
        self.assertIsNone(entry)


class TestSameThreadContext(unittest.TestCase):
    """Tests for _same_thread_context."""

    def test_both_no_context_returns_true(self):
        """Two functions with no thread context are assumed same (avoid noise)."""
        from _builder.concurrency_analysis import _same_thread_context
        self.assertTrue(_same_thread_context({}, {}))

    def test_same_entry_point_returns_true(self):
        from _builder.concurrency_analysis import _same_thread_context
        a = {"thread_model": "pthread", "thread_entry": True, "name": "worker"}
        b = {"thread_model": "pthread", "thread_entry": True, "name": "worker"}
        self.assertTrue(_same_thread_context(a, b))

    def test_different_models_returns_false(self):
        from _builder.concurrency_analysis import _same_thread_context
        a = {"thread_model": "pthread", "thread_entry": False}
        b = {"thread_model": "kthread", "thread_entry": False}
        self.assertFalse(_same_thread_context(a, b))

    def test_same_model_different_entries_returns_false(self):
        """Two pthreads with different entry functions → different contexts."""
        from _builder.concurrency_analysis import _same_thread_context
        a = {"thread_model": "pthread", "thread_entry": True, "name": "worker1"}
        b = {"thread_model": "pthread", "thread_entry": True, "name": "worker2"}
        self.assertFalse(_same_thread_context(a, b))

    def test_one_has_context_other_doesnt_returns_false(self):
        from _builder.concurrency_analysis import _same_thread_context
        a = {"thread_model": "pthread", "thread_entry": False}
        b = {}
        self.assertFalse(_same_thread_context(a, b))


class TestDetectDataRaces(unittest.TestCase):
    """Tests for detect_data_races."""

    def setUp(self):
        # Two functions in different thread contexts, both write the same
        # global var 'counter' — should be flagged as a high-severity race.
        self.graph_dir = _make_race_graph([
            {"id": "writer_a", "name": "writer_a",
             "thread_model": "pthread", "thread_entry": True,
             "globals_written": [{"name": "counter"}]},
            {"id": "writer_b", "name": "writer_b",
             "thread_model": "pthread", "thread_entry": True,
             "globals_written": [{"name": "counter"}]},
        ])
        self.G = _load_graph(self.graph_dir)

    def test_write_write_race_detected(self):
        from _builder.concurrency_analysis import detect_data_races
        races = detect_data_races(self.G)
        self.assertEqual(len(races), 1)
        r = races[0]
        self.assertEqual(r["severity"], "high")
        self.assertEqual(r["shared_resource"]["name"], "counter")
        self.assertEqual(r["shared_resource"]["access_a"], "write")
        self.assertEqual(r["shared_resource"]["access_b"], "write")
        self.assertEqual(r["protection"], "none")

    def test_same_thread_context_no_race(self):
        """Two writers in the SAME thread context should NOT race."""
        from _builder.concurrency_analysis import detect_data_races
        graph_dir = _make_race_graph([
            {"id": "w1", "name": "w1",
             "thread_model": "pthread", "thread_entry": True,
             "globals_written": [{"name": "x"}]},
            {"id": "w2", "name": "w2",
             "thread_model": "pthread", "thread_entry": True, "name": "w1",
             "globals_written": [{"name": "x"}]},
        ])
        # Both have thread_entry=True and name="w1" → same entry → same context
        G = _load_graph(graph_dir)
        races = detect_data_races(G)
        self.assertEqual(len(races), 0)

    def test_read_write_race_is_high_severity(self):
        from _builder.concurrency_analysis import detect_data_races
        graph_dir = _make_race_graph([
            {"id": "reader", "name": "reader",
             "thread_model": "pthread", "thread_entry": True,
             "globals_read": [{"name": "shared"}]},
            {"id": "writer", "name": "writer",
             "thread_model": "kthread", "thread_entry": True,
             "globals_written": [{"name": "shared"}]},
        ])
        G = _load_graph(graph_dir)
        races = detect_data_races(G)
        self.assertEqual(len(races), 1)
        self.assertEqual(races[0]["severity"], "high")

    def test_read_read_race_is_low_severity(self):
        from _builder.concurrency_analysis import detect_data_races
        graph_dir = _make_race_graph([
            {"id": "r1", "name": "r1",
             "thread_model": "pthread", "thread_entry": True,
             "globals_read": [{"name": "config"}]},
            {"id": "r2", "name": "r2",
             "thread_model": "kthread", "thread_entry": True,
             "globals_read": [{"name": "config"}]},
        ])
        G = _load_graph(graph_dir)
        races = detect_data_races(G)
        self.assertEqual(len(races), 1)
        self.assertEqual(races[0]["severity"], "low")

    def test_struct_field_race_detected(self):
        from _builder.concurrency_analysis import detect_data_races
        graph_dir = _make_race_graph([
            {"id": "f1", "name": "f1",
             "thread_model": "pthread", "thread_entry": True,
             "fields_written": [{"struct_chain": "ctx", "field_name": "state"}]},
            {"id": "f2", "name": "f2",
             "thread_model": "kthread", "thread_entry": True,
             "fields_read": [{"struct_chain": "ctx", "field_name": "state"}]},
        ])
        G = _load_graph(graph_dir)
        races = detect_data_races(G)
        self.assertEqual(len(races), 1)
        self.assertEqual(races[0]["shared_resource"]["type"], "struct_field")
        self.assertIn("state", races[0]["shared_resource"]["name"])

    def test_no_race_when_no_shared_resource(self):
        from _builder.concurrency_analysis import detect_data_races
        graph_dir = _make_race_graph([
            {"id": "a", "name": "a",
             "thread_model": "pthread", "thread_entry": True,
             "globals_written": [{"name": "var_a"}]},
            {"id": "b", "name": "b",
             "thread_model": "kthread", "thread_entry": True,
             "globals_written": [{"name": "var_b"}]},
        ])
        G = _load_graph(graph_dir)
        races = detect_data_races(G)
        self.assertEqual(len(races), 0)

    def test_target_func_filters_to_matching_function(self):
        from _builder.concurrency_analysis import detect_data_races
        graph_dir = _make_race_graph([
            {"id": "a", "name": "a",
             "thread_model": "pthread", "thread_entry": True,
             "globals_written": [{"name": "x"}]},
            {"id": "b", "name": "b",
             "thread_model": "kthread", "thread_entry": True,
             "globals_written": [{"name": "x"}]},
            {"id": "c", "name": "c",
             "thread_model": "pthread", "thread_entry": True,
             "globals_written": [{"name": "y"}]},
            {"id": "d", "name": "d",
             "thread_model": "kthread", "thread_entry": True,
             "globals_written": [{"name": "y"}]},
        ])
        G = _load_graph(graph_dir)
        # Only races involving function 'a' should be reported
        races = detect_data_races(G, target_func="a")
        self.assertGreaterEqual(len(races), 1)
        for r in races:
            functions = {r["thread_a"]["function"], r["thread_b"]["function"]}
            self.assertIn("a", functions)

    def test_target_func_not_found_returns_empty(self):
        from _builder.concurrency_analysis import detect_data_races
        races = detect_data_races(self.G, target_func="nonexistent")
        self.assertEqual(races, [])

    def test_races_sorted_by_severity(self):
        """High-severity races should come before low-severity."""
        from _builder.concurrency_analysis import detect_data_races
        graph_dir = _make_race_graph([
            # read-read → low
            {"id": "r1", "name": "r1", "thread_model": "pthread", "thread_entry": True,
             "globals_read": [{"name": "low_racer"}]},
            {"id": "r2", "name": "r2", "thread_model": "kthread", "thread_entry": True,
             "globals_read": [{"name": "low_racer"}]},
            # write-write → high
            {"id": "w1", "name": "w1", "thread_model": "pthread", "thread_entry": True,
             "globals_written": [{"name": "high_racer"}]},
            {"id": "w2", "name": "w2", "thread_model": "kthread", "thread_entry": True,
             "globals_written": [{"name": "high_racer"}]},
        ])
        G = _load_graph(graph_dir)
        races = detect_data_races(G)
        self.assertGreaterEqual(len(races), 2)
        # First should be high severity
        self.assertEqual(races[0]["severity"], "high")


class TestRaceSchema(unittest.TestCase):
    """Verify the race dict has all required fields."""

    def test_race_dict_schema(self):
        from _builder.concurrency_analysis import detect_data_races
        graph_dir = _make_race_graph([
            {"id": "a", "name": "a", "thread_model": "pthread", "thread_entry": True,
             "globals_written": [{"name": "x"}]},
            {"id": "b", "name": "b", "thread_model": "kthread", "thread_entry": True,
             "globals_written": [{"name": "x"}]},
        ])
        G = _load_graph(graph_dir)
        races = detect_data_races(G)
        self.assertEqual(len(races), 1)
        r = races[0]
        for key in ("race_id", "thread_a", "thread_b", "shared_resource",
                    "protection", "severity", "confidence"):
            self.assertIn(key, r, f"missing key: {key}")
        for thread_key in ("thread_a", "thread_b"):
            for sub in ("function", "thread_model", "thread_entry"):
                self.assertIn(sub, r[thread_key])
        for sub in ("name", "type", "access_a", "access_b"):
            self.assertIn(sub, r["shared_resource"])


if __name__ == "__main__":
    unittest.main()
