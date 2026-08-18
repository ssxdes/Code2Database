"""Tests for semantic edges: ALLOCATES / FREES / LOCKS / UNLOCKS."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder.semantic_edges import (
    detect_semantic_edges, add_semantic_edges_to_graph,
    who_allocates, who_frees, unbalanced_alloc_free, who_locks,
)


class TestDetectSemanticEdges(unittest.TestCase):
    """Test the detect_semantic_edges function."""

    def test_detects_kmalloc_call(self):
        """A function body with kmalloc() yields an ALLOCATES edge."""
        node = {
            "id": "func1",
            "name": "alloc_buf",
            "body_text": "void *p = kmalloc(1024, GFP_KERNEL); return p;",
        }
        edges = detect_semantic_edges(node)
        alloc_edges = [e for e in edges if e["relation"] == "ALLOCATES"]
        self.assertTrue(any(e["target"] == "kmalloc" for e in alloc_edges))

    def test_detects_kfree_call(self):
        """A function body with kfree() yields a FREES edge."""
        node = {
            "id": "func1",
            "name": "free_buf",
            "body_text": "kfree(p); p = NULL;",
        }
        edges = detect_semantic_edges(node)
        free_edges = [e for e in edges if e["relation"] == "FREES"]
        self.assertTrue(any(e["target"] == "kfree" for e in free_edges))

    def test_detects_mutex_lock_with_var(self):
        """A function body with mutex_lock(&my_lock) yields a LOCKS edge with var name."""
        node = {
            "id": "func1",
            "name": "critical",
            "body_text": "mutex_lock(&my_lock); do_work(); mutex_unlock(&my_lock);",
        }
        edges = detect_semantic_edges(node)
        lock_edges = [e for e in edges if e["relation"] == "LOCKS"]
        unlock_edges = [e for e in edges if e["relation"] == "UNLOCKS"]
        self.assertTrue(lock_edges)
        self.assertTrue(unlock_edges)
        # Should have extracted the lock variable name
        self.assertTrue(any(e["target"] == "my_lock" for e in lock_edges))
        self.assertTrue(any(e["target"] == "my_lock" for e in unlock_edges))

    def test_no_body_no_edges(self):
        """A node with no body_text produces no edges."""
        node = {"id": "func1", "name": "stub"}
        edges = detect_semantic_edges(node)
        self.assertEqual(edges, [])

    def test_multiple_allocs_deduplicated(self):
        """Multiple kmalloc calls produce a single ALLOCATES edge for kmalloc."""
        node = {
            "id": "func1",
            "name": "multi_alloc",
            "body_text": "void *a = kmalloc(10); void *b = kmalloc(20);",
        }
        edges = detect_semantic_edges(node)
        alloc_edges = [e for e in edges if e["relation"] == "ALLOCATES"
                       and e["target"] == "kmalloc"]
        self.assertEqual(len(alloc_edges), 1)


class TestAddSemanticEdgesToGraph(unittest.TestCase):
    """Test adding semantic edges to a graph."""

    def test_adds_virtual_resource_nodes(self):
        """Adding semantic edges creates resource:: virtual nodes."""
        G = nx.DiGraph()
        G.add_node("func1", name="alloc_buf",
                   body_text="void *p = kmalloc(1024);")
        added = add_semantic_edges_to_graph(G)
        self.assertEqual(added, 1)
        self.assertTrue(G.has_node("resource::kmalloc"))
        nd = G.nodes["resource::kmalloc"]
        self.assertEqual(nd.get("node_type"), "resource")

    def test_adds_locks_and_unlocks(self):
        """Both LOCKS and UNLOCKS edges are added for mutex_lock/unlock."""
        G = nx.DiGraph()
        G.add_node("func1", name="critical",
                   body_text="mutex_lock(&lk); x++; mutex_unlock(&lk);")
        added = add_semantic_edges_to_graph(G)
        self.assertEqual(added, 2)
        # Both lock and unlock virtual nodes should exist as lock nodes
        self.assertTrue(G.has_node("resource::locks::lk"))
        self.assertTrue(G.has_node("resource::unlocks::lk"))
        self.assertEqual(G.nodes["resource::locks::lk"].get("node_type"), "lock")
        self.assertEqual(G.nodes["resource::unlocks::lk"].get("node_type"), "lock")


class TestWhoAllocates(unittest.TestCase):
    """Test the who_allocates query."""

    def test_finds_all_allocators(self):
        """who_allocates with no resource filter returns all allocators."""
        G = nx.DiGraph()
        G.add_node("f1", name="alloc1", source_file="a.c", line=10)
        G.add_node("f2", name="alloc2", source_file="b.c", line=20)
        G.add_node("r1", name="kmalloc", node_type="resource")
        G.add_node("r2", name="vmalloc", node_type="resource")
        G.add_edge("f1", "r1", relation="ALLOCATES")
        G.add_edge("f2", "r2", relation="ALLOCATES")
        results = who_allocates(G)
        self.assertEqual(len(results), 2)
        names = {r["function_name"] for r in results}
        self.assertEqual(names, {"alloc1", "alloc2"})

    def test_finds_specific_resource_allocators(self):
        """who_allocates with resource filter returns only matching functions."""
        G = nx.DiGraph()
        G.add_node("f1", name="kmalloc_user", source_file="a.c", line=10)
        G.add_node("f2", name="vmalloc_user", source_file="b.c", line=20)
        G.add_node("r1", name="kmalloc", node_type="resource")
        G.add_node("r2", name="vmalloc", node_type="resource")
        G.add_edge("f1", "r1", relation="ALLOCATES")
        G.add_edge("f2", "r2", relation="ALLOCATES")
        results = who_allocates(G, resource="kmalloc")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["function_name"], "kmalloc_user")
        self.assertEqual(results[0]["resource"], "kmalloc")


class TestWhoFrees(unittest.TestCase):
    """Test the who_frees query."""

    def test_finds_all_freers(self):
        """who_frees with no resource filter returns all freers."""
        G = nx.DiGraph()
        G.add_node("f1", name="freer1", source_file="a.c", line=10)
        G.add_node("r1", name="kfree", node_type="resource")
        G.add_edge("f1", "r1", relation="FREES")
        results = who_frees(G)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["function_name"], "freer1")


class TestUnbalancedAllocFree(unittest.TestCase):
    """Test the unbalanced_alloc_free query."""

    def test_allocates_without_free(self):
        """A function with ALLOCATES but no FREES is flagged."""
        G = nx.DiGraph()
        G.add_node("f1", name="leaky", body_text="kmalloc(100);")
        G.add_node("f2", name="balanced",
                   body_text="void *p = kmalloc(100); kfree(p);")
        G.add_node("f3", name="just_free", body_text="kfree(p);")
        add_semantic_edges_to_graph(G)
        result = unbalanced_alloc_free(G)
        # leaky: allocates without free
        leaky_flagged = any(r["function_name"] == "leaky"
                            for r in result["allocates_without_free"])
        self.assertTrue(leaky_flagged)
        # just_free: frees without alloc
        free_only_flagged = any(r["function_name"] == "just_free"
                                for r in result["frees_without_alloc"])
        self.assertTrue(free_only_flagged)
        # balanced: should not be in any flagged list
        balanced_flagged = (
            any(r["function_name"] == "balanced" for r in result["allocates_without_free"])
            or any(r["function_name"] == "balanced" for r in result["frees_without_alloc"])
            or any(r["function_name"] == "balanced" for r in result["count_imbalance"])
        )
        self.assertFalse(balanced_flagged)

    def test_count_imbalance(self):
        """A function with 2 distinct allocs and 1 free is flagged as count_imbalance."""
        G = nx.DiGraph()
        G.add_node("f1", name="uneven",
                   body_text="void *a = kmalloc(10); void *b = vmalloc(20); kfree(a);")
        add_semantic_edges_to_graph(G)
        result = unbalanced_alloc_free(G)
        imbalanced = [r for r in result["count_imbalance"]
                      if r["function_name"] == "uneven"]
        self.assertTrue(imbalanced)
        self.assertEqual(imbalanced[0]["alloc_count"], 2)  # kmalloc + vmalloc
        self.assertEqual(imbalanced[0]["free_count"], 1)  # kfree


class TestWhoLocks(unittest.TestCase):
    """Test the who_locks query."""

    def test_finds_all_lockers(self):
        """who_locks with no filter returns all lockers."""
        G = nx.DiGraph()
        G.add_node("f1", name="locker1", source_file="a.c")
        G.add_node("r1", name="my_lock", node_type="lock")
        G.add_edge("f1", "r1", relation="LOCKS", lock_function="mutex_lock")
        results = who_locks(G)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["function_name"], "locker1")
        self.assertEqual(results[0]["lock"], "my_lock")

    def test_finds_specific_lock(self):
        """who_locks with lock filter returns only matching functions."""
        G = nx.DiGraph()
        G.add_node("f1", name="locker_a", source_file="a.c")
        G.add_node("f2", name="locker_b", source_file="b.c")
        G.add_node("r1", name="lock_a", node_type="lock")
        G.add_node("r2", name="lock_b", node_type="lock")
        G.add_edge("f1", "r1", relation="LOCKS", lock_function="mutex_lock")
        G.add_edge("f2", "r2", relation="LOCKS", lock_function="mutex_lock")
        results = who_locks(G, lock_name="lock_a")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["function_name"], "locker_a")


if __name__ == "__main__":
    unittest.main()
