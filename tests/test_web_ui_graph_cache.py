"""Smoke tests for GraphCache's 8 public methods.

Builds a tiny graph_dir with code2database_master.json + a domain JSON
containing two nodes and one edge, then exercises the 8 most-used
GraphCache methods (summary, get_node, neighbors, shortest_path,
list_communities, community_nodes, search, impact_analysis) as well as
reload / list_domains / get_code_snippet.

Smoke-test level — verify each method returns a sensible type and does
not crash on representative inputs.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _write_graph_dir(graph_dir: str):
    """Create a minimal graph_dir with master.json + a domain file."""
    master = {
        'version': 'test-1.0',
        'stats': {'total_functions': 2},
        'total_nodes': 2,
        'domains': {'test': 'code2database_test.json'},
    }
    with open(os.path.join(graph_dir, 'code2database_master.json'), 'w') as f:
        json.dump(master, f)
    domain = {
        'domain': 'test',
        'nodes': [
            {
                'id': 'n1', 'name': 'foo', 'source_file': '/tmp/test.c',
                'line': 10, 'domain': 'test', 'labels': ['API_entry'],
                'signature': 'int foo()', 'body_text': 'return 0;',
            },
            {
                'id': 'n2', 'name': 'bar', 'source_file': '/tmp/test.c',
                'line': 20, 'domain': 'test', 'labels': [],
                'signature': 'int bar()',
            },
        ],
        'edges': [
            {'source': 'n1', 'target': 'n2', 'relation': 'INVOKES',
             'call_order': 1, 'confidence': 'EXTRACTED',
             'confidence_score': 1.0, 'call_condition': '', 'concurrency': ''},
        ],
    }
    with open(os.path.join(graph_dir, 'code2database_test.json'), 'w') as f:
        json.dump(domain, f)


class TestGraphCacheImport(unittest.TestCase):
    """Verify the web_ui module imports cleanly."""

    def test_module_imports_cleanly(self):
        from _builder.web_ui import GraphCache
        self.assertTrue(callable(GraphCache))
        # Verify the 8+ public methods exist
        for name in ('reload', 'summary', 'get_node', 'neighbors',
                     'shortest_path', 'list_communities',
                     'community_nodes', 'search', 'get_code_snippet',
                     'list_domains', 'impact_analysis'):
            self.assertTrue(hasattr(GraphCache, name),
                            f'GraphCache missing method: {name}')


class TestGraphCacheSmoke(unittest.TestCase):
    """Smoke test 8 GraphCache public methods."""

    def setUp(self):
        from _builder.web_ui import GraphCache
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        _write_graph_dir(self.tmpdir)
        self.cache = GraphCache(self.tmpdir)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- The 8 methods requested by the spec ---

    def test_summary_returns_dict(self):
        s = self.cache.summary()
        self.assertIsInstance(s, dict)
        self.assertIn('node_count', s)
        self.assertIn('edge_count', s)
        self.assertIn('community_count', s)
        self.assertEqual(s['node_count'], 2)
        self.assertEqual(s['edge_count'], 1)

    def test_get_node_returns_dict(self):
        n = self.cache.get_node('n1')
        self.assertIsNotNone(n)
        self.assertEqual(n['name'], 'foo')
        self.assertEqual(n['id'], 'n1')

    def test_get_node_missing_returns_none(self):
        self.assertIsNone(self.cache.get_node('nonexistent'))

    def test_neighbors_returns_dict(self):
        # depth=2 captures the focused node + immediate successors
        nb = self.cache.neighbors('n1', depth=2)
        self.assertIsInstance(nb, dict)
        self.assertIn('nodes', nb)
        self.assertIn('edges', nb)
        # n2 should be in nodes (BFS depth 2 processes n2 at d=1 < depth=2)
        node_ids = {n.get('id') for n in nb['nodes']}
        self.assertIn('n2', node_ids)
        # Edge (n1 → n2) should be in edges
        edge_pairs = {(e.get('source'), e.get('target')) for e in nb['edges']}
        self.assertIn(('n1', 'n2'), edge_pairs)

    def test_neighbors_depth1_includes_edge_even_if_node_skipped(self):
        # depth=1: only the focused node is in `nodes` but its edges
        # are still surfaced in `edges`.
        nb = self.cache.neighbors('n1', depth=1)
        edge_pairs = {(e.get('source'), e.get('target')) for e in nb['edges']}
        self.assertIn(('n1', 'n2'), edge_pairs)

    def test_shortest_path_returns_dict(self):
        r = self.cache.shortest_path('n1', 'n2')
        self.assertIsInstance(r, dict)
        self.assertEqual(r.get('length'), 2)
        self.assertEqual(r['path'], ['n1', 'n2'])

    def test_shortest_path_same_node(self):
        r = self.cache.shortest_path('n1', 'n1')
        self.assertEqual(r['path'], ['n1'])
        self.assertEqual(r['length'], 1)

    def test_shortest_path_no_path(self):
        # n2 -> n1 has no edge
        r = self.cache.shortest_path('n2', 'n1')
        self.assertIn('error', r)

    def test_list_communities_returns_list(self):
        cs = self.cache.list_communities()
        self.assertIsInstance(cs, list)
        self.assertGreater(len(cs), 0)
        # Each community should have id + node_count
        for c in cs:
            self.assertIn('id', c)
            self.assertIn('node_count', c)

    def test_community_nodes_returns_dict(self):
        r = self.cache.community_nodes('test')
        self.assertIsInstance(r, dict)
        self.assertEqual(r['community'], 'test')
        self.assertEqual(r['node_count'], 2)

    def test_search_returns_list_with_results(self):
        r = self.cache.search('foo')
        self.assertIsInstance(r, list)
        self.assertTrue(any(res['name'] == 'foo' for res in r))

    def test_search_case_insensitive(self):
        r = self.cache.search('FOO')
        self.assertTrue(any(res['name'] == 'foo' for res in r))

    def test_search_no_match_returns_empty(self):
        r = self.cache.search('zzz')
        self.assertEqual(r, [])

    def test_impact_analysis_returns_dict(self):
        # Reverse reachability for n2 (who calls n2? -> n1)
        r = self.cache.impact_analysis('n2', max_depth=3)
        self.assertIsInstance(r, dict)
        self.assertEqual(r['node_id'], 'n2')
        self.assertGreaterEqual(r['affected_count'], 1)
        affected_ids = {a['id'] for a in r['affected']}
        self.assertIn('n1', affected_ids)


class TestGraphCacheExtended(unittest.TestCase):
    """Reload, list_domains, get_code_snippet — supporting methods."""

    def setUp(self):
        from _builder.web_ui import GraphCache
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        _write_graph_dir(self.tmpdir)
        self.cache = GraphCache(self.tmpdir)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reload_after_change_does_not_crash(self):
        # Mutate the domain file and reload
        domain_path = os.path.join(self.tmpdir, 'code2database_test.json')
        with open(domain_path) as f:
            data = json.load(f)
        data['nodes'].append({
            'id': 'n3', 'name': 'baz', 'source_file': '/tmp/test.c',
            'line': 30, 'domain': 'test', 'labels': [],
            'signature': 'int baz()',
        })
        with open(domain_path, 'w') as f:
            json.dump(data, f)
        self.cache.reload()
        s = self.cache.summary()
        self.assertEqual(s['node_count'], 3)

    def test_list_domains_returns_list(self):
        domains = self.cache.list_domains()
        self.assertIsInstance(domains, list)
        self.assertTrue(any(d['domain'] == 'test' for d in domains))

    def test_get_code_snippet_returns_string(self):
        # The test node's source_file points to /tmp/test.c which doesn't exist
        # — should fall back to body_text
        snip = self.cache.get_code_snippet('n1', context_lines=3)
        self.assertIsInstance(snip, str)


class TestGraphCacheDegrees(unittest.TestCase):
    """Degrees are precomputed once per reload and served from cache.

    The old /api/degrees recomputed every node's degree on every
    request — O(N+E) on the eager backend, but ~3N+2E indexed SQL
    queries on the lazy SQLite backend (one query per node-attr fetch,
    predecessor/successor scan and edge fetch), all while holding the
    global GraphCache lock. Browsing k nodes froze every other
    endpoint k times over.
    """

    def setUp(self):
        from _builder.web_ui import GraphCache
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.GraphCache = GraphCache
        self._write_dir(self._nodes(), self._edges())

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _nodes(self):
        return [
            {'id': 'n1', 'name': 'foo', 'source_file': '/tmp/t.c', 'line': 1,
             'domain': 'test', 'labels': [], 'signature': 'int foo()'},
            {'id': 'n2', 'name': 'bar', 'source_file': '/tmp/t.c', 'line': 2,
             'domain': 'test', 'labels': [], 'signature': 'int bar()'},
            {'id': 'n3', 'name': 'f3', 'source_file': '/tmp/t.c', 'line': 3,
             'domain': 'test', 'labels': [], 'is_empty': True},
            {'id': 'n4', 'name': 'f4', 'source_file': '/tmp/t.c', 'line': 4,
             'domain': 'test', 'labels': [], 'signature': 'int f4()'},
        ]

    def _edges(self):
        return [
            {'source': 'n1', 'target': 'n2', 'relation': 'INVOKES',
             'call_order': 1, 'confidence': 'EXTRACTED'},
            {'source': 'n1', 'target': 'n3', 'relation': 'CONTAINS'},
            {'source': 'n3', 'target': 'n1', 'relation': 'IMPORTS'},
            {'source': 'n4', 'target': 'n4', 'relation': 'INVOKES',
             'call_order': 1, 'confidence': 'EXTRACTED'},
        ]

    def _write_dir(self, nodes, edges):
        master = {'version': 't', 'stats': {'total_functions': len(nodes)},
                  'total_nodes': len(nodes),
                  'domains': {'test': 'code2database_test.json'}}
        with open(os.path.join(self.tmpdir, 'code2database_master.json'), 'w') as f:
            json.dump(master, f)
        domain = {'domain': 'test', 'nodes': nodes, 'edges': edges}
        with open(os.path.join(self.tmpdir, 'code2database_test.json'), 'w') as f:
            json.dump(domain, f)

    def test_degrees_semantics_preserved(self):
        cache = self.GraphCache(self.tmpdir)
        deg = cache.get_all_degrees()
        # CONTAINS/IMPORTS excluded; is_empty node excluded from the
        # all-degrees view; self-loop counts in both directions.
        self.assertEqual(deg, {'n1': 1, 'n2': 1, 'n4': 2})
        d = cache.get_node_degree('n4')
        self.assertEqual(d, {'in_degree': 1, 'out_degree': 1, 'total': 2})

    def test_degrees_served_from_cache_not_recomputed(self):
        cache = self.GraphCache(self.tmpdir)
        first = cache.get_all_degrees()
        # Any attempt to walk the graph after reload must not happen.
        def boom(*a, **k):
            raise AssertionError('degrees recomputed per request')
        cache.G.edges = boom
        cache.G.predecessors = boom
        cache.G.successors = boom
        second = cache.get_all_degrees()
        self.assertEqual(first, second)

    def test_reload_recomputes_degrees(self):
        cache = self.GraphCache(self.tmpdir)
        self.assertEqual(cache.get_all_degrees()['n2'], 1)
        domain_path = os.path.join(self.tmpdir, 'code2database_test.json')
        with open(domain_path) as f:
            data = json.load(f)
        data['edges'].append({'source': 'n2', 'target': 'n4',
                              'relation': 'INVOKES'})
        with open(domain_path, 'w') as f:
            json.dump(data, f)
        cache.reload()
        self.assertEqual(cache.get_all_degrees()['n2'], 2)

    def test_lazy_sqlite_backend_precomputes_via_sql(self):
        from _builder.sqlite_store import SQLiteStore
        from _builder.streaming_graph import LazySQLiteGraph
        db = os.path.join(self.tmpdir, 'code2database.db')
        store = SQLiteStore(db)
        store.connect()
        store.store_functions(self._nodes())
        store.store_edges(self._edges())
        store.close()
        cache = self.GraphCache(self.tmpdir)
        cache.G = LazySQLiteGraph(db)
        cache._compute_degrees()
        self.assertEqual(cache.get_all_degrees(), {'n1': 1, 'n2': 1, 'n4': 2})
        self.assertEqual(cache.get_node_degree('n1'),
                         {'in_degree': 0, 'out_degree': 1, 'total': 1})


if __name__ == '__main__':
    unittest.main()
