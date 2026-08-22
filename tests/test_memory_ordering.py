"""Tests for memory-ordering and happens-before analysis."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.memory_ordering import (  # noqa: E402
    analyze_memory_ordering,
    happens_before_analysis,
    MemoryOrderingInfo,
)


def _nd(body_text, **extra):
    """Build a fake node attrs dict with the given body text."""
    nd = {"body_text": body_text, "name": "test"}
    nd.update(extra)
    return nd


def test_rcu_lock_unlock_detected():
    body = """
    void reader() {
        rcu_read_lock();
        p = rcu_dereference(g);
        rcu_read_unlock();
    }
    """
    info = analyze_memory_ordering(_nd(body))
    assert info.rcu_read_locks == [3]
    assert info.rcu_read_unlocks == [5]
    assert info.has_acquire_semantics() is True


def test_smp_barriers_detected():
    body = """
    void f() {
        WRITE_ONCE(x, 1);
        smp_wmb();
        smp_mb();
        smp_rmb();
    }
    """
    info = analyze_memory_ordering(_nd(body))
    assert info.smp_wmb_lines == [4]
    assert info.smp_mb_lines == [5]
    assert info.smp_rmb_lines == [6]
    assert info.write_once == [('x', 3)]
    assert info.has_release_semantics() is True
    assert info.has_acquire_semantics() is True


def test_smp_store_release_and_load_acquire():
    body = """
    void f() {
        smp_store_release(&flag, 1);
        v = smp_load_acquire(&flag);
    }
    """
    info = analyze_memory_ordering(_nd(body))
    assert info.smp_store_release == [('&flag', 3)]
    assert info.smp_load_acquire == [('&flag', 4)]
    assert info.has_release_semantics() is True
    assert info.has_acquire_semantics() is True


def test_read_once_write_once():
    body = """
    void f() {
        WRITE_ONCE(x, 42);
        v = READ_ONCE(x);
    }
    """
    info = analyze_memory_ordering(_nd(body))
    assert info.write_once == [('x', 3)]
    assert info.read_once == [('x', 4)]


def test_atomic_ops_detected():
    body = """
    void f() {
        atomic_set(&a, 1);
        v = atomic_read(&a);
        atomic_inc(&b);
        atomic_cmpxchg(&c, 0, 1);
    }
    """
    info = analyze_memory_ordering(_nd(body))
    assert len(info.atomic_ops) == 4
    vars_operated = {v for v, _ in info.atomic_ops}
    assert '&a' in vars_operated
    assert '&b' in vars_operated
    assert '&c' in vars_operated


def test_atomic_thread_fence():
    body = """
    void f() {
        atomic_thread_fence(memory_order_release);
        atomic_thread_fence(memory_order_acquire);
        atomic_thread_fence(memory_order_seq_cst);
    }
    """
    info = analyze_memory_ordering(_nd(body))
    orders = {o for o, _ in info.atomic_thread_fences}
    assert 'memory_order_release' in orders
    assert 'memory_order_acquire' in orders
    assert 'memory_order_seq_cst' in orders
    assert info.has_release_semantics() is True
    assert info.has_acquire_semantics() is True


def test_rcu_critical_section_check():
    info = MemoryOrderingInfo()
    info.rcu_read_locks = [10]
    info.rcu_read_unlocks = [20]
    assert info.in_rcu_critical_section(15) is True
    assert info.in_rcu_critical_section(5) is False
    assert info.in_rcu_critical_section(25) is False


def test_empty_body_returns_empty_info():
    info = analyze_memory_ordering(_nd(""))
    assert not info.has_any()
    assert not info.has_acquire_semantics()
    assert not info.has_release_semantics()


def test_happens_before_via_release_acquire():
    """writer does smp_store_release on flag, reader does smp_load_acquire."""
    import networkx as nx
    G = nx.DiGraph()
    G.add_node("W", body_text="""
    void W() {
        data = 42;
        smp_store_release(&flag, 1);
    }
    """, name="W")
    G.add_node("R", body_text="""
    void R() {
        v = smp_load_acquire(&flag);
        x = data;
    }
    """, name="R")
    result = happens_before_analysis(G, "W", "R", "flag")
    assert result.has_happens_before is True
    assert any("release/acquire" in m for m in result.mechanisms)


def test_happens_before_via_wmb_rmb():
    """writer does WRITE_ONCE + smp_wmb, reader does smp_rmb + READ_ONCE."""
    import networkx as nx
    G = nx.DiGraph()
    G.add_node("W", body_text="""
    void W() {
        WRITE_ONCE(x, 1);
        smp_wmb();
    }
    """, name="W")
    G.add_node("R", body_text="""
    void R() {
        smp_rmb();
        v = READ_ONCE(x);
    }
    """, name="R")
    result = happens_before_analysis(G, "W", "R", "x")
    assert result.has_happens_before is True


def test_happens_before_via_full_barrier():
    """writer does smp_mb, reader does smp_mb."""
    import networkx as nx
    G = nx.DiGraph()
    G.add_node("W", body_text="void W() { x = 1; smp_mb(); }", name="W")
    G.add_node("R", body_text="void R() { smp_mb(); v = x; }", name="R")
    result = happens_before_analysis(G, "W", "R", "x")
    assert result.has_happens_before is True


def test_happens_before_via_rcu():
    """writer does wmb + sync_rcu, reader in rcu_read_lock."""
    import networkx as nx
    G = nx.DiGraph()
    G.add_node("W", body_text="""
    void W() {
        x = 1;
        synchronize_rcu();
    }
    """, name="W")
    G.add_node("R", body_text="""
    void R() {
        rcu_read_lock();
        v = x;
        rcu_read_unlock();
    }
    """, name="R")
    result = happens_before_analysis(G, "W", "R", "x")
    assert result.has_happens_before is True
    assert any("RCU" in m for m in result.mechanisms)


def test_happens_before_via_direct_call():
    """writer directly calls reader — program order."""
    import networkx as nx
    G = nx.DiGraph()
    G.add_node("W", body_text="void W() { x = 1; R(); }", name="W")
    G.add_node("R", body_text="void R() { v = x; }", name="R")
    G.add_edge("W", "R", relation="INVOKES")
    result = happens_before_analysis(G, "W", "R", "x")
    assert result.has_happens_before is True
    assert any("program-order" in m for m in result.mechanisms)
    assert result.confidence == "EXTRACTED"


def test_no_happens_before_when_no_mechanism():
    """No locks, no barriers, no call edge — no happens-before."""
    import networkx as nx
    G = nx.DiGraph()
    G.add_node("W", body_text="void W() { x = 1; }", name="W")
    G.add_node("R", body_text="void R() { v = x; }", name="R")
    result = happens_before_analysis(G, "W", "R", "x")
    assert result.has_happens_before is False
    assert "no happens-before" in result.explanation


def test_missing_node_returns_false():
    """Missing writer or reader returns False with explanation."""
    import networkx as nx
    G = nx.DiGraph()
    G.add_node("W", body_text="void W() { x = 1; }", name="W")
    result = happens_before_analysis(G, "W", "missing_R", "x")
    assert result.has_happens_before is False
    assert "not found" in result.explanation
