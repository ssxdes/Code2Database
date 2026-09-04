"""Federated queries across registered C2D graphs.

Multiple graphs (per-project code2db-out dirs) registered in a
pluggable registry; fed-search / fed-neighbors / fed-path run across
ALL of them and annotate every result with its source graph, so an
agent can answer "where does X live and who calls it" across project
boundaries without a joint build-multi rebuild.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.federated import (
    federate_register, federate_list, federate_remove,
    fed_search, fed_neighbors, fed_path,
)


def _write_graph(graph_dir, project, nodes, edges):
    os.makedirs(graph_dir, exist_ok=True)
    master = {
        'version': 't', 'stats': {'total_functions': len(nodes)},
        'total_nodes': len(nodes),
        'domains': {project: f'code2database_{project}.json'},
    }
    with open(os.path.join(graph_dir, 'code2database_master.json'), 'w') as f:
        json.dump(master, f)
    with open(os.path.join(graph_dir, f'code2database_{project}.json'),
              'w') as f:
        json.dump({'domain': project, 'nodes': nodes, 'edges': edges}, f)


def _node(pid, name, domain):
    return {'id': pid, 'name': name, 'source_file': f'/src/{name}.c',
            'line': 1, 'domain': domain, 'labels': [], 'signature': ''}


class TestFederatedQueries(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.registry = os.path.join(root, 'registry.json')

        self.graph_a = os.path.join(root, 'projA')
        self.graph_b = os.path.join(root, 'projB')
        _write_graph(self.graph_a, 'proja', [
            _node('a_send', 'send_packet', 'net'),
            _node('a_recv', 'recv_packet', 'net'),
        ], [
            {'source': 'a_send', 'target': 'a_recv', 'relation': 'INVOKES'},
        ])
        _write_graph(self.graph_b, 'projb', [
            _node('b_send', 'send_packet', 'transport'),
            _node('b_encode', 'encode_frame', 'transport'),
        ], [
            {'source': 'b_send', 'target': 'b_encode', 'relation': 'INVOKES'},
        ])

        federate_register('alpha', self.graph_a, registry=self.registry)
        federate_register('beta', self.graph_b, registry=self.registry)

    def test_register_and_list(self):
        listed = federate_list(registry=self.registry)
        self.assertEqual(sorted(listed.keys()), ['alpha', 'beta'])
        self.assertEqual(listed['alpha']['graph_dir'], self.graph_a)

    def test_register_duplicate_name_replaces(self):
        federate_register('alpha', self.graph_b, registry=self.registry)
        listed = federate_list(registry=self.registry)
        self.assertEqual(listed['alpha']['graph_dir'], self.graph_b)

    def test_search_across_graphs_with_annotation(self):
        results = fed_search('send_packet', registry=self.registry)
        # same-named function in both projects — both reported, each
        # tagged with its source graph (the disambiguation the
        # single-graph UI couldn't give)
        self.assertEqual({r['graph'] for r in results}, {'alpha', 'beta'})
        for r in results:
            self.assertIn('id', r)
            self.assertIn('name', r)

    def test_neighbors_union(self):
        nb = fed_neighbors('send_packet', registry=self.registry,
                           resolve_by_name=True)
        by_graph = {n['graph']: {x['id'] for x in n['nodes']}
                    for n in nb}
        self.assertIn('a_recv', by_graph.get('alpha', set()))
        self.assertIn('b_encode', by_graph.get('beta', set()))

    def test_path_within_each_graph(self):
        paths = fed_path('send_packet', 'recv_packet',
                         registry=self.registry, resolve_by_name=True)
        # alpha has the route; beta doesn't (no recv_packet there)
        self.assertEqual([p['graph'] for p in paths], ['alpha'])
        self.assertEqual(paths[0]['path'],
                         ['a_send', 'a_recv'])

    def test_remove(self):
        federate_remove('alpha', registry=self.registry)
        listed = federate_list(registry=self.registry)
        self.assertEqual(sorted(listed.keys()), ['beta'])

    def test_remove_unknown_raises(self):
        with self.assertRaises(KeyError):
            federate_remove('nope', registry=self.registry)

    def test_missing_registry_returns_empty(self):
        listed = federate_list(
            registry=os.path.join(self.tmp.name, 'absent.json'))
        self.assertEqual(listed, {})

    def test_search_empty_registry(self):
        results = fed_search(
            'x', registry=os.path.join(self.tmp.name, 'absent.json'))
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
