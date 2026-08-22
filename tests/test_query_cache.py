"""Tests for query result cache."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.query_cache import (  # noqa: E402
    cached_query,
    invalidate_node,
    invalidate_all,
    cache_stats,
)


class _Args:
    def __init__(self, graph, **kw):
        self.graph = graph
        for k, v in kw.items():
            setattr(self, k, v)


def test_cache_hit_skips_recomputation():
    calls = []

    @cached_query('test_cmd', ttl=60)
    def query(args):
        calls.append(args.node)
        return {'node': args.node}

    a1 = _Args('/tmp/cgtest1', node='foo')
    a2 = _Args('/tmp/cgtest1', node='foo')

    r1 = query(a1)
    r2 = query(a2)
    assert r1 == r2 == {'node': 'foo'}
    assert len(calls) == 1  # second call hit cache


def test_cache_miss_on_different_args():
    calls = []

    @cached_query('test_cmd', ttl=60)
    def query(args):
        calls.append(args.node)
        return {'node': args.node}

    a1 = _Args('/tmp/cgtest2', node='foo')
    a2 = _Args('/tmp/cgtest2', node='bar')

    query(a1)
    query(a2)
    assert calls == ['foo', 'bar']


def test_invalidate_node_evicts_entry():
    calls = []

    def touched(args):
        return frozenset({args.node})

    @cached_query('test_cmd', ttl=60, touched_nodes_fn=touched)
    def query(args):
        calls.append(args.node)
        return {'node': args.node}

    a = _Args('/tmp/cgtest3', node='foo')
    query(a)
    assert len(calls) == 1

    # Cache hit
    query(a)
    assert len(calls) == 1

    # Invalidate
    invalidate_node('/tmp/cgtest3', 'foo')
    query(a)
    assert len(calls) == 2  # recomputed


def test_invalidate_all_clears_cache():
    calls = []

    @cached_query('test_cmd', ttl=60)
    def query(args):
        calls.append(args.node)
        return {'node': args.node}

    a = _Args('/tmp/cgtest4', node='foo')
    query(a)
    query(a)
    assert len(calls) == 1

    invalidate_all('/tmp/cgtest4')
    query(a)
    assert len(calls) == 2


def test_cache_stats():
    @cached_query('test_cmd', ttl=60)
    def query(args):
        return {'node': args.node}

    a = _Args('/tmp/cgtest5', node='foo')
    query(a)
    stats = cache_stats('/tmp/cgtest5')
    assert stats['entries'] == 1
    assert stats['max_entries'] > 0


def test_no_graph_dir_skips_cache():
    calls = []

    @cached_query('test_cmd', ttl=60)
    def query(args):
        calls.append(args.node)
        return {'node': args.node}

    a = _Args('', node='foo')
    r1 = query(a)
    r2 = query(a)
    assert len(calls) == 2  # no caching without graph_dir


def test_capture_stdout():
    """capture_stdout=True caches stdout output and replays on hit."""
    call_count = []

    @cached_query('test_print', ttl=60, capture_stdout=True)
    def query(args):
        call_count.append(1)
        import sys
        sys.stdout.write(f'hello {args.node}\n')

    a = _Args('/tmp/cgtest6', node='world')

    # First call — wrapped fn runs, prints 'hello world' (captured by decorator)
    result1 = query(a)
    assert 'hello world' in result1

    # Second call — should hit cache, replays captured stdout
    result2 = query(a)
    assert result1 == result2
    assert len(call_count) == 1  # fn body only ran once
