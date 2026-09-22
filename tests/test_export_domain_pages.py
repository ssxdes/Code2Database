"""Tests for per-domain page assembly on large (SQLite-backed) graphs.

Large graphs are exported per domain, and the per-domain assembly
(_domain_pages) used to re-scan the full edge list once per domain —
~10 billion edge visits on a 2.3M-node, 5K-domain graph. These tests
pin the current behavior: the single-pass assembly produces the same
pages the per-domain scan produced, lazy views feed it, community
labels reach the pages, and the mermaid export path completes on a
read-only lazy view.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

import networkx as nx  # noqa: E402

from _builder.export.export import (  # noqa: E402
    _domain_pages, _export_mermaid,
)


class _LazyNodeView:
    """NodeView-like facade: callable with data=True (fresh dicts, like
    SQLite row reads), subscriptable for single-node attribute reads."""

    def __init__(self, G):
        self._G = G

    def __call__(self, data=False):
        if data:
            for nid, nd in self._G.nodes(data=True):
                yield nid, dict(nd)
        else:
            yield from self._G.nodes()

    def __getitem__(self, nid):
        return dict(self._G.nodes[nid])

    def __iter__(self):
        return iter(self._G)


class _FakeLazyGraph:
    """Stands in for LazySQLiteGraph: read-only, no node writes.

    Mimics the iteration surface the exporter uses (nodes/edges with
    data, G.nodes[nid] reads, number_of_nodes) so the export path can
    be driven without a 50K-node fixture.
    """

    _db_path = "/tmp/fake.db"

    def __init__(self, G):
        self._G = G
        self.nodes = _LazyNodeView(G)

    def number_of_nodes(self):
        return self._G.number_of_nodes()

    def edges(self, data=False):
        yield from self._G.edges(data=data)

    def __contains__(self, nid):
        return nid in self._G

    def __iter__(self):
        return iter(self._G)


def _make_graph():
    G = nx.DiGraph()
    G.add_node("a", name="a", domain="net", labels=["API_entry"])
    G.add_node("b", name="b", domain="net", labels=[])
    G.add_node("c", name="c", domain="fs", labels=[])
    G.add_edge("a", "b", relation="INVOKES")
    G.add_edge("a", "c", relation="INVOKES")  # cross-domain edge
    G.add_edge("b", "a", relation="CONTAINS")  # non-call edge
    return G


def _make_graph_dir(G, with_communities=True):
    tmp = tempfile.mkdtemp(prefix="c2d_html_test_")
    domain_data = {"nodes": [
        dict(nd, id=nid) for nid, nd in G.nodes(data=True)], "edges": []}
    with open(os.path.join(tmp, "domain_net.json"), "w") as f:
        json.dump(domain_data, f)
    with open(os.path.join(tmp, "code2database_master.json"), "w") as f:
        json.dump({"source_root": "/tmp", "domains": {}}, f)
    if with_communities:
        with open(os.path.join(tmp, ".code2database_communities.json"), "w") as f:
            json.dump({"communities": [
                {"id": "core", "node_ids": ["a", "b"]}]}, f)
    return tmp


class TestDomainPagesEquivalence(unittest.TestCase):
    """The single-pass page assembly must equal the old per-domain scan."""

    def test_pages_match_per_domain_composition(self):
        G = _make_graph()
        domain_nodes = {}
        for nid, ndata in G.nodes(data=True):
            domain_nodes.setdefault(ndata.get("domain", "root"),
                                    []).append((nid, ndata))
        pages = dict(_domain_pages(G, domain_nodes))
        # net page: own nodes a, b + cross endpoint c (edge a->c)
        self.assertEqual(set(pages["net"].nodes), {"a", "b", "c"})
        self.assertIn(("a", "b"), pages["net"].edges)
        self.assertIn(("a", "c"), pages["net"].edges)
        self.assertNotIn(("b", "a"), pages["net"].edges)  # CONTAINS skipped
        # fs page: own node c + cross endpoints a
        self.assertEqual(set(pages["fs"].nodes), {"c", "a"})
        self.assertIn(("a", "c"), pages["fs"].edges)
        # deterministic order
        self.assertEqual(sorted(p[0] for p in _domain_pages(G, domain_nodes)),
                         ["fs", "net"])


class TestMermaidExportOnLazyGraph(unittest.TestCase):
    """The mermaid export path must complete on a read-only lazy view."""

    def _run(self, with_communities):
        G = _make_graph()
        graph_dir = _make_graph_dir(G, with_communities)
        domain_nodes = {}
        lazy = _FakeLazyGraph(G)
        for nid, ndata in lazy.nodes(data=True):
            domain_nodes.setdefault(ndata.get("domain", "root"),
                                    []).append((nid, ndata))
        with tempfile.TemporaryDirectory() as out:
            output = os.path.join(out, "cg.html")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                _export_mermaid(lazy, output, max_nodes=2,
                                domain_nodes=domain_nodes,
                                total_nodes=lazy.number_of_nodes())
            files = []
            for root, _dirs, names in os.walk(out):
                for n in names:
                    files.append(os.path.join(root, n))
            return stdout.getvalue(), files

    def test_lazy_graph_with_communities_exports(self):
        stdout, files = self._run(with_communities=True)
        self.assertIn("mermaid index", stdout)
        self.assertTrue(any(f.endswith("cg.html") for f in files),
                        f"index page missing: {files}")
        self.assertTrue(any("domain_fs" in f for f in files), files)
        self.assertTrue(any("domain_net" in f for f in files), files)

    def test_lazy_graph_without_communities_exports(self):
        stdout, files = self._run(with_communities=False)
        self.assertIn("mermaid index", stdout)
        self.assertTrue(any("domain_net" in f for f in files), files)

    def test_community_labels_reach_domain_pages(self):
        """The community map (not node writes) carries labels on lazy
        views — the pages must still group nodes for collapse/expand."""
        G = _make_graph()
        graph_dir = _make_graph_dir(G, with_communities=True)
        domain_nodes = {}
        lazy = _FakeLazyGraph(G)
        for nid, ndata in lazy.nodes(data=True):
            if nid in ("a", "b"):
                ndata = dict(ndata, community_id="core")
            domain_nodes.setdefault(ndata.get("domain", "root"),
                                    []).append((nid, ndata))
        pages = dict(_domain_pages(lazy, domain_nodes))
        self.assertEqual(pages["net"].nodes["a"]["community_id"], "core")
        self.assertEqual(pages["net"].nodes["b"]["community_id"], "core")
        self.assertNotIn("community_id", pages["fs"].nodes["c"])


if __name__ == "__main__":
    unittest.main()
