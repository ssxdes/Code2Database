"""Regression tests for thread-context partitioning and groupless locks.

Pins two false-negative/false-suppression sources:
  1. Callees of DIFFERENT thread entries with the same model all landed
     in one (model, None) context bucket — races between two thread
     families were missed. Thread contexts are now keyed by the entry's
     NODE ID (thread_entry_id / thread_entry_inherited from build-time
     propagation), which also distinguishes same-named static thread
     routines in different files.
  2. All groupless acquire patterns shared the single sentinel
     '__rcu_read_lock__' — two DIFFERENT primitives (rcu_read_lock vs
     preempt_disable) were 'the same lock' and real races between them
     were suppressed. Sentinels are now per-pattern (stable md5 prefix).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder.concurrency_analysis import (
    detect_data_races, _detect_locks_held, _get_thread_context)


class TestThreadContextPartitioning(unittest.TestCase):
    def test_callees_of_different_entries_are_distinct_contexts(self):
        G = nx.DiGraph()
        # two pthread entries (same fn NAME, different node ids)
        G.add_node("a.c:worker", name="worker", thread_model="pthread",
                   thread_entry=True, thread_entry_id="a.c:worker")
        G.add_node("b.c:worker", name="worker", thread_model="pthread",
                   thread_entry=True, thread_entry_id="b.c:worker")
        # each entry's callee writes the same global
        G.add_node("a.c:helper", name="helper_a",
                   thread_model_inherited="pthread",
                   thread_entry_inherited="a.c:worker",
                   globals_written=[{"name": "shared_ctr"}])
        G.add_node("b.c:helper", name="helper_b",
                   thread_model_inherited="pthread",
                   thread_entry_inherited="b.c:worker",
                   globals_written=[{"name": "shared_ctr"}])
        races = detect_data_races(G)
        self.assertTrue(
            any("shared_ctr" in str(r) for r in races),
            "race between callees of two different thread entries missed")

    def test_same_entry_callees_are_one_context(self):
        G = nx.DiGraph()
        G.add_node("a.c:worker", name="worker", thread_model="pthread",
                   thread_entry=True, thread_entry_id="a.c:worker")
        G.add_node("a.c:helper", name="helper_a",
                   thread_model_inherited="pthread",
                   thread_entry_inherited="a.c:worker",
                   globals_written=[{"name": "x"}])
        G.add_node("a.c:helper2", name="helper_b",
                   thread_model_inherited="pthread",
                   thread_entry_inherited="a.c:worker",
                   globals_written=[{"name": "x"}])
        races = detect_data_races(G)
        self.assertEqual(
            [r for r in races if "globals_var" in str(r)
             or '"x"' in str(r) or "'x'" in str(r) or "x" in str(r.get("resource", ""))],
            [], "same-thread callees must not race")

    def test_get_thread_context_prefers_node_identity(self):
        ctx = _get_thread_context({
            "thread_model": "pthread", "thread_entry": True,
            "thread_entry_id": "a.c:worker", "name": "worker"})
        self.assertEqual(ctx, ("pthread", "a.c:worker"))
        ctx = _get_thread_context({
            "thread_model_inherited": "pthread",
            "thread_entry_inherited": "a.c:worker"})
        self.assertEqual(ctx, ("pthread", "a.c:worker"))

    def test_legacy_graphs_fall_back_to_name(self):
        ctx = _get_thread_context({
            "thread_model": "pthread", "thread_entry": True,
            "name": "worker"})
        self.assertEqual(ctx, ("pthread", "worker"))


class TestGrouplessLockSentinels(unittest.TestCase):
    PROFILE = {"concurrency_patterns": {
        "lock_acquire_patterns": [r"rcu_read_lock\(\)",
                                  r"preempt_disable\(\)"],
    }}

    def test_different_primitives_are_distinct_locks(self):
        a = _detect_locks_held(
            {"body_text": "rcu_read_lock(); x = g->state;"},
            profile=self.PROFILE)
        b = _detect_locks_held(
            {"body_text": "preempt_disable(); y = g->state;"},
            profile=self.PROFILE)
        self.assertNotEqual(a, b, "distinct primitives collapsed to one "
                                  "sentinel — races would be suppressed")
        self.assertEqual(a & b, set())

    def test_same_primitive_across_functions_is_common_lock(self):
        a = _detect_locks_held(
            {"body_text": "rcu_read_lock(); x = g->state;"},
            profile=self.PROFILE)
        b = _detect_locks_held(
            {"body_text": "rcu_read_lock(); y = g->state;"},
            profile=self.PROFILE)
        self.assertTrue(a & b, "same primitive must map to the same "
                               "sentinel across functions")
        # and races between them are suppressed
        G = nx.DiGraph()
        G.add_node("f1", name="f1", body_text="rcu_read_lock(); x=g->s;",
                   fields_written=[{"struct_chain": "g",
                                    "field_name": "s"}],
                   thread_model="pthread", thread_entry=True,
                   thread_entry_id="f1")
        G.add_node("f2", name="f2", body_text="rcu_read_lock(); y=g->s;",
                   fields_read=[{"struct_chain": "g", "field_name": "s"}],
                   thread_model="pthread", thread_entry=True,
                   thread_entry_id="f2")
        races = detect_data_races(G, profile=self.PROFILE)
        protected = [r for r in races if "g.s" in str(r)]
        # detect_data_races does not suppress protected races — it
        # annotates them with the common lock. The old shared sentinel
        # would have made protection 'none' for DIFFERENT primitives;
        # here the SAME primitive must yield the groupless sentinel as
        # the protection annotation.
        self.assertEqual(len(protected), 1)
        self.assertNotEqual(protected[0]["protection"], "none")
        self.assertIn("__groupless_", protected[0]["protection"])


if __name__ == "__main__":
    unittest.main()
