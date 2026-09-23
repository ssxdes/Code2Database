"""Cross-file resolution strategies (import_map / suffix / fuzzy).

Strategy 2 (import_map), 4 (suffix_match) and 6 (fuzzy) resolve the
majority of cross-file call edges but had no direct tests — only
strategies 1/3/5 were pinned. Also pins the include-scan cache stamp:
a long-lived process must see a file's NEW include set after the file
changes, not the one from its first read.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx


def _resolve(G, callee, invoker_id, source_root="", suffix_index=None):
    from _builder.build.import_resolve import _multi_strategy_resolve
    return _multi_strategy_resolve(G, callee, invoker_id,
                                   source_root=source_root,
                                   suffix_index=suffix_index)


def _graph(nodes):
    G = nx.DiGraph()
    for nid, nd in nodes.items():
        G.add_node(nid, **nd)
    return G


class TestImportMapStrategy(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, "a.c"), "w") as f:
            f.write('#include "b.h"\nvoid caller(void) { target(); }\n')
        self.G = _graph({
            "root_caller": {"name": "caller", "source_file": "a.c",
                            "domain": "root"},
            "net_target": {"name": "target", "source_file": "b.h",
                           "domain": "net"},
        })

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_include_reaches_callee(self):
        nid, strategy, conf = _resolve(self.G, "target", "root_caller",
                                       source_root=self.root)
        self.assertEqual(nid, "net_target")
        self.assertEqual(strategy, "import_map")
        self.assertEqual(conf, 0.85)

    def test_same_file_beats_import_map(self):
        # A same-file definition of `target` must win over the header one.
        self.G.add_node("root_target",
                        name="target", source_file="a.c", domain="root")
        nid, strategy, _ = _resolve(self.G, "target", "root_caller",
                                    source_root=self.root)
        self.assertEqual(nid, "root_target")
        self.assertEqual(strategy, "same_file")

    def test_basename_include_match(self):
        # include "lib/ops.h" resolves a callee whose source_file is the
        # bare basename "ops.h"
        with open(os.path.join(self.root, "a.c"), "w") as f:
            f.write('#include "lib/ops.h"\n')
        self.G = _graph({
            "root_caller": {"name": "caller", "source_file": "a.c",
                            "domain": "root"},
            "blk_do_io": {"name": "do_io", "source_file": "ops.h",
                          "domain": "blk"},
        })
        nid, strategy, _ = _resolve(self.G, "do_io", "root_caller",
                                    source_root=self.root)
        self.assertEqual((nid, strategy), ("blk_do_io", "import_map"))


class TestIncludeCacheStamp(unittest.TestCase):

    def setUp(self):
        from _builder.build import import_resolve
        self.mod = import_resolve
        self.mod._file_includes_cache.clear()
        self.root = tempfile.mkdtemp()
        self.src = os.path.join(self.root, "a.c")
        with open(self.src, "w") as f:
            f.write('#include "b.h"\n')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        self.mod._file_includes_cache.clear()

    def test_reread_after_file_change(self):
        got = self.mod._get_file_includes("a.c", self.root)
        self.assertEqual(got, {"b.h"})
        # different length so the size part of the stamp changes even on
        # filesystems with coarse mtime granularity
        with open(self.src, "w") as f:
            f.write('#include "cccc.h"\n')
        got = self.mod._get_file_includes("a.c", self.root)
        self.assertEqual(got, {"cccc.h"})

    def test_missing_file_caches_empty(self):
        got = self.mod._get_file_includes("missing.c", self.root)
        self.assertEqual(got, set())
        got2 = self.mod._get_file_includes("missing.c", self.root)
        self.assertEqual(got2, set())


class TestSuffixMatchStrategy(unittest.TestCase):

    def test_suffix_index_unique_match(self):
        G = _graph({
            "root_caller": {"name": "caller", "source_file": "a.c",
                            "domain": "root"},
            "net_rx_packet": {"name": "rx_packet", "source_file": "net.c",
                              "domain": "net"},
        })
        idx = {"rx_packet": ["net_rx_packet"]}
        nid, strategy, conf = _resolve(G, "rx_packet", "root_caller",
                                       suffix_index=idx)
        self.assertEqual((nid, strategy, conf),
                         ("net_rx_packet", "suffix_match", 0.60))

    def test_suffix_prefers_caller_domain(self):
        # callee name != node name so the same_domain strategy cannot
        # fire first; suffix matching keys on the id suffix
        G = _graph({
            "root_caller": {"name": "caller", "source_file": "a.c",
                            "domain": "net"},
            "net_packet_handler": {"name": "handler", "source_file": "n.c",
                                   "domain": "net"},
            "blk_packet_handler": {"name": "handler", "source_file": "b.c",
                                   "domain": "blk"},
        })
        idx = {"packet_handler": ["net_packet_handler", "blk_packet_handler"]}
        nid, strategy, _ = _resolve(G, "packet_handler", "root_caller",
                                    suffix_index=idx)
        self.assertEqual(nid, "net_packet_handler")
        self.assertEqual(strategy, "suffix_match")


class TestFuzzyStrategy(unittest.TestCase):

    def test_same_domain_strategy_fires_for_shared_domain_names(self):
        G = _graph({
            "root_caller": {"name": "caller", "source_file": "a.c",
                            "domain": "net"},
            "net_helper": {"name": "helper", "source_file": "n.c",
                           "domain": "net"},
            "blk_helper": {"name": "helper", "source_file": "b.c",
                           "domain": "blk"},
        })
        nid, strategy, conf = _resolve(G, "helper", "root_caller")
        self.assertEqual(nid, "net_helper")
        self.assertEqual(strategy, "same_domain")
        self.assertEqual(conf, 0.75)

    def test_fuzzy_without_domain_match(self):
        G = _graph({
            "root_caller": {"name": "caller", "source_file": "a.c",
                            "domain": "fs"},
            "net_helper": {"name": "helper", "source_file": "n.c",
                           "domain": "net"},
            "blk_helper": {"name": "helper", "source_file": "b.c",
                           "domain": "blk"},
        })
        nid, strategy, conf = _resolve(G, "helper", "root_caller")
        self.assertIn(nid, ("net_helper", "blk_helper"))
        self.assertEqual(strategy, "fuzzy")
        self.assertEqual(conf, 0.30)


if __name__ == "__main__":
    unittest.main()
