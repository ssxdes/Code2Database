"""Regression tests for TOCTOU detection pairing semantics.

Pins two false-positive sources fixed in this round:
  1. Writers/readers were paired by bare field_name — same-name fields
     of DIFFERENT structs (dev->state vs conn->state, ubiquitous in C)
     cross-paired into phantom TOCTOU races. Pairing key is now
     (struct_chain, field_name), matching detect_data_races.
  2. The reader/writer pair loop never checked thread context —
     single-threaded check-then-act patterns were reported as
     high-severity TOCTOU races.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder.concurrency_analysis import _detect_toctou_patterns

PROFILE = {"concurrency_patterns": {
    "lock_acquire_patterns": [r"mutex_lock\(&?(\w+)\)"],
}}


def _graph():
    return nx.DiGraph()


def _add(G, nid, name, writes=(), reads=(), model=None, entry=False,
         body=""):
    G.add_node(nid, name=name, is_empty=False,
               fields_written=[{"struct_chain": sc, "field_name": fn}
                               for sc, fn in writes],
               fields_read=[{"struct_chain": sc, "field_name": fn}
                            for sc, fn in reads],
               thread_model=model, thread_entry=entry, body_text=body)


class TestToctouStructPairing(unittest.TestCase):
    def test_same_field_name_different_structs_not_paired(self):
        G = _graph()
        _add(G, "w", "dev_writer",
             writes=[("dev", "state")], model="pthread", entry=True)
        _add(G, "r", "conn_reader",
             reads=[("conn", "state")],
             body="mutex_lock(&a); x = c->state;")
        self.assertEqual(_detect_toctou_patterns(G, profile=PROFILE), [])

    def test_same_struct_cross_thread_race_detected(self):
        G = _graph()
        _add(G, "w", "dev_writer",
             writes=[("dev", "state")], model="pthread", entry=True)
        _add(G, "r", "dev_reader",
             reads=[("dev", "state")],
             body="mutex_lock(&m); v = d->state;")
        res = _detect_toctou_patterns(G, profile=PROFILE)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["shared_resource"]["name"], "dev.state")
        self.assertEqual(res[0]["severity"], "high")


class TestToctouThreadContext(unittest.TestCase):
    def test_same_thread_check_then_act_not_reported(self):
        # Both functions in the default (main) context: a read under a
        # lock with an unlocked writer is a plain data-flow pattern,
        # not a cross-thread TOCTOU race.
        G = _graph()
        _add(G, "w", "solo_writer", writes=[("cfg", "val")])
        _add(G, "r", "solo_reader", reads=[("cfg", "val")],
             body="mutex_lock(&m); v = c->val;")
        self.assertEqual(_detect_toctou_patterns(G, profile=PROFILE), [])

    def test_cross_thread_still_reported(self):
        G = _graph()
        _add(G, "w", "bg_writer", writes=[("cfg", "val")],
             model="pthread", entry=True)
        _add(G, "r", "fg_reader", reads=[("cfg", "val")],
             body="mutex_lock(&m); v = c->val;")
        res = _detect_toctou_patterns(G, profile=PROFILE)
        self.assertEqual(len(res), 1)


class TestToctouLockProtection(unittest.TestCase):
    def test_common_lock_not_reported(self):
        G = _graph()
        _add(G, "w", "locked_writer", writes=[("dev", "state")],
             model="pthread", entry=True,
             body="mutex_lock(&m); d->state = 1; mutex_unlock(&m);")
        _add(G, "r", "locked_reader", reads=[("dev", "state")],
             body="mutex_lock(&m); v = d->state;")
        self.assertEqual(_detect_toctou_patterns(G, profile=PROFILE), [])


if __name__ == "__main__":
    unittest.main()
