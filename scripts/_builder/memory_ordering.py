"""Memory-ordering and happens-before analysis.

Extends concurrency_analysis beyond lock-based protection to recognize:
- RCU read-side critical sections (rcu_read_lock / rcu_read_unlock)
- Memory barriers (smp_mb / smp_rmb / smp_wmb / smp_store_release /
  smp_load_acquire, plus C11 atomic_thread_fence)
- Atomic operations (atomic_read / atomic_set / atomic_cmpxchg /
  atomic_inc / atomic_dec / READ_ONCE / WRITE_ONCE / smp_cond_load_acquire)
- Acquire/release semantics from these primitives

Used by the `happens-before` command to answer "is there a happens-before
edge between writer W and reader R of a shared variable?" without requiring
a profile-configured lock pattern. This covers the Linux kernel concurrency
model where many races are prevented by memory ordering rather than locks.

The analysis is conservative: when in doubt, it reports "no happens-before
edge found" rather than falsely claiming protection.
"""
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from _builder.utils import _get_body_text
from _builder.graph_build import _load_full_graph
import logging


# ---------------------------------------------------------------------------
# Memory-ordering primitive recognition
# ---------------------------------------------------------------------------
# These patterns are matched against function body text. They cover the
# common Linux kernel and C11 atomic APIs. Profile-specific primitives can
# extend this via the `memory_ordering_patterns` profile section.

# RCU critical section markers (no argument needed)
_RCU_READ_LOCK_RE = re.compile(r'\brcu_read_lock\s*\(', re.IGNORECASE)
_RCU_READ_UNLOCK_RE = re.compile(r'\brcu_read_unlock\s*\(', re.IGNORECASE)
_RCU_READ_LOCK_BH_RE = re.compile(r'\brcu_read_lock_bh\s*\(', re.IGNORECASE)
_RCU_READ_UNLOCK_BH_RE = re.compile(r'\brcu_read_unlock_bh\s*\(', re.IGNORECASE)
_RCU_DEREERENCE_RE = re.compile(r'\brcu_dereference\s*\(\s*([^)]+?)\s*\)', re.IGNORECASE)

# Memory barriers (no argument)
_SMP_MB_RE = re.compile(r'\bsmp_mb\s*\(', re.IGNORECASE)
_SMP_RMB_RE = re.compile(r'\bsmp_rmb\s*\(', re.IGNORECASE)
_SMP_WMB_RE = re.compile(r'\bsmp_wmb\s*\(', re.IGNORECASE)
_SMP_STORE_RELEASE_RE = re.compile(r'\bsmp_store_release\s*\(\s*([^,]+?)\s*,', re.IGNORECASE)
_SMP_LOAD_ACQUIRE_RE = re.compile(r'\bsmp_load_acquire\s*\(\s*([^)]+?)\s*\)', re.IGNORECASE)
_SMP_STORE_MB_RE = re.compile(r'\bsmp_store_mb\s*\(\s*([^,]+?)\s*,', re.IGNORECASE)
_SYNC_RCU_RE = re.compile(r'\bsynchronize_rcu\s*\(', re.IGNORECASE)
# Pre-compiled prefix patterns for WRITE_ONCE/READ_ONCE var matching.
# The full pattern is dynamic (includes the var name), but we can
# use these to quickly check if the macro is even present in the body
# before doing the expensive full regex search.
_WRITE_ONCE_PREFIX_RE = re.compile(r'\bWRITE_ONCE\s*\(', re.IGNORECASE)
_READ_ONCE_PREFIX_RE = re.compile(r'\bREAD_ONCE\s*\(', re.IGNORECASE)

# C11 / atomic_thread_fence
_ATOMIC_THREAD_FENCE_RE = re.compile(r'\batomic_thread_fence\s*\(\s*(\w+)\s*\)', re.IGNORECASE)

# READ_ONCE / WRITE_ONCE — Linux kernel macros for atomic-ish access
_READ_ONCE_RE = re.compile(r'\bREAD_ONCE\s*\(\s*([^)]+?)\s*\)', re.IGNORECASE)
_WRITE_ONCE_RE = re.compile(r'\bWRITE_ONCE\s*\(\s*([^,]+?)\s*,', re.IGNORECASE)

# Atomic operations — capture the variable name being operated on
_ATOMIC_RE = re.compile(
    r'\b(?:atomic_read|atomic_set|atomic_inc|atomic_dec|'
    r'atomic_add|atomic_sub|atomic_cmpxchg|atomic_xchg|'
    r'atomic_fetch_add|atomic_fetch_sub|atomic_fetch_or|atomic_fetch_and)'
    r'\s*\(\s*([^,)]+)', re.IGNORECASE)

# __atomic_load_n / __atomic_store_n — GCC builtins with memory order arg
_GCC_ATOMIC_LOAD_RE = re.compile(
    r'\b__atomic_load_n\s*\(\s*([^,]+?)\s*,\s*(\w+)\s*\)', re.IGNORECASE)
_GCC_ATOMIC_STORE_RE = re.compile(
    r'\b__atomic_store_n\s*\(\s*([^,]+?)\s*,\s*[^,]+\s*,\s*(\w+)\s*\)', re.IGNORECASE)


# Combined alternation regex with named groups. A single finditer() per line
# replaces the 11+ separate re.search() calls that the original loop ran on
# every line of every function body. The named group that matched is read
# via m.lastgroup; for primitives that carry a capture group (variable name
# or memory order), the value is extracted by re-searching the matched
# substring with the original per-primitive pattern (cheap, since the
# matched substring is short).
_MEM_ORDERING_RE = re.compile(
    r'(?P<rcu_lock_bh>\brcu_read_lock_bh\s*\()'
    r'|(?P<rcu_unlock_bh>\brcu_read_unlock_bh\s*\()'
    r'|(?P<rcu_lock>\brcu_read_lock\s*\()'
    r'|(?P<rcu_unlock>\brcu_read_unlock\s*\()'
    r'|(?P<smp_store_mb>\bsmp_store_mb\s*\(\s*([^,]+?)\s*,)'
    r'|(?P<smp_mb>\bsmp_mb\s*\()'
    r'|(?P<smp_rmb>\bsmp_rmb\s*\()'
    r'|(?P<smp_wmb>\bsmp_wmb\s*\()'
    r'|(?P<smp_store_release>\bsmp_store_release\s*\(\s*([^,]+?)\s*,)'
    r'|(?P<smp_load_acquire>\bsmp_load_acquire\s*\(\s*([^)]+?)\s*\))'
    r'|(?P<read_once>\bREAD_ONCE\s*\(\s*([^)]+?)\s*\))'
    r'|(?P<write_once>\bWRITE_ONCE\s*\(\s*([^,]+?)\s*,)'
    r'|(?P<atomic>\b(?:atomic_read|atomic_set|atomic_inc|atomic_dec|'
    r'atomic_add|atomic_sub|atomic_cmpxchg|atomic_xchg|'
    r'atomic_fetch_add|atomic_fetch_sub|atomic_fetch_or|atomic_fetch_and)'
    r'\s*\(\s*([^,)]+))'
    r'|(?P<atomic_thread_fence>\batomic_thread_fence\s*\(\s*(\w+)\s*\))',
    re.IGNORECASE
)


@dataclass
class MemoryOrderingInfo:
    """Memory-ordering primitives found in a function body."""
    rcu_read_locks: List[int] = field(default_factory=list)   # line numbers
    rcu_read_unlocks: List[int] = field(default_factory=list)
    smp_mb_lines: List[int] = field(default_factory=list)
    smp_rmb_lines: List[int] = field(default_factory=list)
    smp_wmb_lines: List[int] = field(default_factory=list)
    smp_store_release: List[Tuple[str, int]] = field(default_factory=list)  # (var, line)
    smp_load_acquire: List[Tuple[str, int]] = field(default_factory=list)
    read_once: List[Tuple[str, int]] = field(default_factory=list)
    write_once: List[Tuple[str, int]] = field(default_factory=list)
    atomic_ops: List[Tuple[str, int]] = field(default_factory=list)
    atomic_thread_fences: List[Tuple[str, int]] = field(default_factory=list)  # (order, line)

    def has_any(self) -> bool:
        return bool(
            self.rcu_read_locks or self.rcu_read_unlocks or
            self.smp_mb_lines or self.smp_rmb_lines or self.smp_wmb_lines or
            self.smp_store_release or self.smp_load_acquire or
            self.read_once or self.write_once or
            self.atomic_ops or self.atomic_thread_fences
        )

    def has_acquire_semantics(self) -> bool:
        """True if the function contains any acquire-order primitive."""
        return bool(
            self.smp_load_acquire or
            self.smp_mb_lines or  # full barrier implies acquire
            any(o in ("memory_order_acquire", "memory_order_acq_rel",
                      "memory_order_seq_cst")
                for o, _ in self.atomic_thread_fences) or
            self.rcu_read_locks  # RCU read-lock has acquire semantics
        )

    def has_release_semantics(self) -> bool:
        """True if the function contains any release-order primitive."""
        return bool(
            self.smp_store_release or
            self.smp_mb_lines or  # full barrier implies release
            self.smp_wmb_lines or  # wmb is release for prior writes
            any(o in ("memory_order_release", "memory_order_acq_rel",
                      "memory_order_seq_cst")
                for o, _ in self.atomic_thread_fences) or
            self.rcu_read_unlocks  # RCU read-unlock has release semantics
        )

    def in_rcu_critical_section(self, line: int) -> bool:
        """True if `line` is between a rcu_read_lock and matching unlock."""
        # For each lock, find the FIRST unlock at or after the lock line,
        # then check if line is within [lock, unlock]. This correctly
        # excludes lines between an unlock and the next lock.
        # The previous implementation checked ANY unlock >= lock and
        # line <= unlock, which returned True for lines outside any
        # RCU section (e.g., between unlock@20 and lock@30 with
        # unlocks=[20,40], line 25 was True because 25 <= 40).
        sorted_unlocks = sorted(self.rcu_read_unlocks)
        for lock_line in self.rcu_read_locks:
            if lock_line > line:
                continue
            # Find the first unlock at or after this lock
            matching_unlock = None
            for u in sorted_unlocks:
                if u >= lock_line:
                    matching_unlock = u
                    break
            if matching_unlock is not None and lock_line <= line <= matching_unlock:
                return True
        return False


def analyze_memory_ordering(ndata: Dict, G=None, nid: Optional[str] = None) -> MemoryOrderingInfo:
    """Extract memory-ordering primitive usage from a function body.

    Args:
        ndata: Node attrs dict.
        G, nid: Optional graph + node id for lazy body_text fetch.

    Returns:
        MemoryOrderingInfo with line numbers and variable names of each
        recognized primitive.
    """
    info = MemoryOrderingInfo()

    if G is not None and nid is not None:
        body = _get_body_text(G, nid)
    else:
        body = ndata.get("body_text", "")
    if not body:
        return info

    lines = body.split("\n")
    for line_no, line in enumerate(lines, 1):
        # Single pass over the line via the combined alternation regex.
        # `seen` records which primitive has already fired on this line so
        # we preserve the original "one entry per primitive per line" shape
        # (re.search returned only the first match).
        seen = set()
        for m in _MEM_ORDERING_RE.finditer(line):
            name = m.lastgroup
            if name is None or name in seen:
                continue
            seen.add(name)
            if name == 'rcu_lock' or name == 'rcu_lock_bh':
                info.rcu_read_locks.append(line_no)
            elif name == 'rcu_unlock' or name == 'rcu_unlock_bh':
                info.rcu_read_unlocks.append(line_no)
            elif name == 'smp_mb':
                info.smp_mb_lines.append(line_no)
            elif name == 'smp_rmb':
                info.smp_rmb_lines.append(line_no)
            elif name == 'smp_wmb':
                info.smp_wmb_lines.append(line_no)
            elif name == 'smp_store_mb':
                sub_m = _SMP_STORE_MB_RE.search(m.group(0))
                if sub_m:
                    info.smp_store_release.append((sub_m.group(1).strip(), line_no))
            elif name == 'smp_store_release':
                sub_m = _SMP_STORE_RELEASE_RE.search(m.group(0))
                if sub_m:
                    info.smp_store_release.append((sub_m.group(1).strip(), line_no))
            elif name == 'smp_load_acquire':
                sub_m = _SMP_LOAD_ACQUIRE_RE.search(m.group(0))
                if sub_m:
                    info.smp_load_acquire.append((sub_m.group(1).strip(), line_no))
            elif name == 'read_once':
                sub_m = _READ_ONCE_RE.search(m.group(0))
                if sub_m:
                    info.read_once.append((sub_m.group(1).strip(), line_no))
            elif name == 'write_once':
                sub_m = _WRITE_ONCE_RE.search(m.group(0))
                if sub_m:
                    info.write_once.append((sub_m.group(1).strip(), line_no))
            elif name == 'atomic':
                sub_m = _ATOMIC_RE.search(m.group(0))
                if sub_m:
                    info.atomic_ops.append((sub_m.group(1).strip(), line_no))
            elif name == 'atomic_thread_fence':
                sub_m = _ATOMIC_THREAD_FENCE_RE.search(m.group(0))
                if sub_m:
                    info.atomic_thread_fences.append((sub_m.group(1).strip(), line_no))

    return info


# ---------------------------------------------------------------------------
# Happens-before analysis
# ---------------------------------------------------------------------------

@dataclass
class HappensBeforeResult:
    """Result of happens-before analysis between a writer and a reader."""
    writer_id: str
    reader_id: str
    variable: str
    has_happens_before: bool
    mechanisms: List[str] = field(default_factory=list)
    explanation: str = ""
    confidence: str = "INFERRED"  # EXTRACTED if direct call, INFERRED if path-based

    def to_dict(self) -> Dict:
        return {
            "writer": self.writer_id,
            "reader": self.reader_id,
            "variable": self.variable,
            "has_happens_before": self.has_happens_before,
            "mechanisms": self.mechanisms,
            "explanation": self.explanation,
            "confidence": self.confidence,
        }


def _get_function_name(G, nid: str) -> str:
    nd = G.nodes[nid] if nid in G.nodes else {}
    return nd.get("name", nid)


def _writer_writes_var(G, writer_id: str, var: str) -> bool:
    """True if writer_id writes to `var` (field or global)."""
    if writer_id not in G.nodes:
        return False
    nd = G.nodes[writer_id]
    var_l = var.lower()
    for fw in nd.get("fields_written", []) or []:
        if fw.get("field_name", "").lower() == var_l:
            return True
        chain = fw.get("struct_chain", "")
        if chain and (chain + "." + fw.get("field_name", "")).lower().endswith(var_l):
            return True
    for gw in nd.get("globals_written", []) or []:
        if gw.get("name", "").lower() == var_l:
            return True
    # Check WRITE_ONCE on the var
    body = nd.get("body_text", "")
    if body and _WRITE_ONCE_PREFIX_RE.search(body) and re.search(r'\bWRITE_ONCE\s*\(\s*' + re.escape(var) + r'\b', body):
        return True
    return False


def _reader_reads_var(G, reader_id: str, var: str) -> bool:
    """True if reader_id reads from `var` (field or global)."""
    if reader_id not in G.nodes:
        return False
    nd = G.nodes[reader_id]
    var_l = var.lower()
    for fr in nd.get("fields_read", []) or []:
        if fr.get("field_name", "").lower() == var_l:
            return True
        chain = fr.get("struct_chain", "")
        if chain and (chain + "." + fr.get("field_name", "")).lower().endswith(var_l):
            return True
    for gr in nd.get("globals_read", []) or []:
        if gr.get("name", "").lower() == var_l:
            return True
    body = nd.get("body_text", "")
    if body and _READ_ONCE_PREFIX_RE.search(body) and re.search(r'\bREAD_ONCE\s*\(\s*' + re.escape(var) + r'\b', body):
        return True
    return False


def _share_lockset(G, a_id: str, b_id: str, profile: Optional[Dict]) -> bool:
    """True if both functions acquire at least one common lock.

    Conservative: if either function's lock patterns can't be analyzed
    (no profile), returns False.
    """
    if not profile:
        return False
    try:
        from _builder.lock_coverage import analyze_lock_coverage
        cov_a = analyze_lock_coverage(G.nodes[a_id], profile, G=G, nid=a_id)
        cov_b = analyze_lock_coverage(G.nodes[b_id], profile, G=G, nid=b_id)
        locks_a = {acq for acq, _ in cov_a.lock_acquire_lines}
        locks_b = {acq for acq, _ in cov_b.lock_acquire_lines}
        return bool(locks_a & locks_b)
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return False


def happens_before_analysis(G, writer_id: str, reader_id: str,
                            variable: str, profile: Optional[Dict] = None,
                            max_depth: int = 5) -> HappensBeforeResult:
    """Determine if there is a happens-before edge from writer to reader.

    A happens-before edge exists if ANY of the following holds:
    1. **Lock-based**: both writer and reader hold the same lock around the
       access (analyzed via lock_coverage). [Strongest guarantee]
    2. **RCU-based**: writer does a write+memory-barrier (or
       synchronize_rcu), reader is in an RCU critical section.
    3. **Release-acquire**: writer uses a release-store on a related atomic
       (smp_store_release / WRITE_ONCE followed by smp_wmb), reader uses an
       acquire-load (smp_load_acquire / READ_ONCE preceded by smp_rmb).
    4. **Full barrier**: writer has smp_wmb or smp_mb after the write, reader
       has smp_rmb or smp_mb before the read.
    5. **Direct call**: writer directly calls reader (single INVOKES edge) —
       then there is a program-order happens-before from writer to reader.

    Args:
        G: networkx DiGraph.
        writer_id: node id of the writer function.
        reader_id: node id of the reader function.
        variable: name of the shared variable (field or global).
        profile: optional profile dict for lock pattern compilation.
        max_depth: max path depth when searching for a call-chain relationship.

    Returns:
        HappensBeforeResult with has_happens_before=True if any mechanism holds.
    """
    result = HappensBeforeResult(
        writer_id=writer_id, reader_id=reader_id, variable=variable,
        has_happens_before=False)

    if writer_id not in G.nodes or reader_id not in G.nodes:
        result.explanation = "writer or reader not found in graph"
        return result

    writer_nd = G.nodes[writer_id]
    reader_nd = G.nodes[reader_id]

    # Mechanism 5: direct call writer -> reader (program order)
    if G.has_edge(writer_id, reader_id):
        ed = G.get_edge_data(writer_id, reader_id) or {}
        if ed.get("relation") in (None, "INVOKES", "callback_dispatch"):
            result.has_happens_before = True
            result.mechanisms.append("program-order (direct call)")
            result.confidence = "EXTRACTED"

    # Mechanism 1: shared lock
    if _share_lockset(G, writer_id, reader_id, profile):
        result.has_happens_before = True
        result.mechanisms.append("shared lock")

    # Mechanisms 2/3/4: memory-ordering primitives
    writer_mo = analyze_memory_ordering(writer_nd, G=G, nid=writer_id)
    reader_mo = analyze_memory_ordering(reader_nd, G=G, nid=reader_id)

    # Mechanism 2: RCU — reader in rcu_read_lock, writer has release semantics
    # or invoked synchronize_rcu (which is the canonical RCU writer primitive)
    if reader_mo.rcu_read_locks:
        # Use the lazy fetch (analyze_memory_ordering does this two lines
        # above): on LazySQLiteGraph/StreamingGraph backends body_text is
        # compressed/not in cached attrs, and the raw .get() read was always
        # empty there — silently missing synchronize_rcu() and reporting a
        # false 'possible data race' on exactly the kernel-scale graphs
        # this analysis targets.
        if G is not None and writer_id is not None:
            writer_body = _get_body_text(G, writer_id)
        else:
            writer_body = writer_nd.get("body_text", "")
        has_sync_rcu = bool(_SYNC_RCU_RE.search(writer_body))
        if has_sync_rcu:
            result.has_happens_before = True
            result.mechanisms.append("RCU: synchronize_rcu + reader in rcu_read_lock")
        elif writer_mo.has_release_semantics():
            result.has_happens_before = True
            result.mechanisms.append("RCU: wmb/mb in writer + rcu_read_lock in reader")

    # Mechanism 3: release-store in writer, acquire-load in reader on related var
    if writer_mo.smp_store_release and reader_mo.smp_load_acquire:
        # Check if any released var matches any acquired var (heuristic)
        released_vars = {v for v, _ in writer_mo.smp_store_release}
        acquired_vars = {v for v, _ in reader_mo.smp_load_acquire}
        common = released_vars & acquired_vars
        if common:
            result.has_happens_before = True
            result.mechanisms.append(
                f"release/acquire on shared atomic: {sorted(common)}")

    # Mechanism 4: full or write/read barrier pair
    if (writer_mo.smp_wmb_lines or writer_mo.smp_mb_lines) and \
       (reader_mo.smp_rmb_lines or reader_mo.smp_mb_lines):
        result.has_happens_before = True
        result.mechanisms.append("smp_wmb/mb in writer + smp_rmb/mb in reader")

    # Mechanism 4 variant: WRITE_ONCE in writer + READ_ONCE in reader with
    # matching variable, plus a barrier pair
    if writer_mo.write_once and reader_mo.read_once:
        w_vars = {v for v, _ in writer_mo.write_once}
        r_vars = {v for v, _ in reader_mo.read_once}
        common = w_vars & r_vars
        if common and (writer_mo.smp_wmb_lines or writer_mo.smp_mb_lines) and \
           (reader_mo.smp_rmb_lines or reader_mo.smp_mb_lines):
            result.has_happens_before = True
            result.mechanisms.append(
                f"WRITE_ONCE/READ_ONCE on {sorted(common)} with barrier pair")

    if not result.explanation:
        if result.has_happens_before:
            result.explanation = (
                f"happens-before established via: {', '.join(result.mechanisms)}")
        else:
            result.explanation = (
                "no happens-before edge found — possible data race")

    return result


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def cmd_happens_before(args):
    """Check happens-before relationship between a writer and a reader.

    Examples:
      happens-before --graph out/ --write writer_fn --read reader_fn --var pid
    """
    graph_dir = args.graph
    writer_hint = args.write
    reader_hint = args.read
    variable = args.var
    max_depth = getattr(args, "max_depth", 5)

    G = _load_full_graph(graph_dir)

    # Resolve writer/reader node ids
    from _builder.utils import _find_node_id
    writer_id = _find_node_id(G, writer_hint)
    reader_id = _find_node_id(G, reader_hint)
    if not writer_id:
        print(f"Error: writer node matching {writer_hint!r} not found",
              file=sys.stderr)
        sys.exit(1)
    if not reader_id:
        print(f"Error: reader node matching {reader_hint!r} not found",
              file=sys.stderr)
        sys.exit(1)

    # Load profile for lock pattern compilation
    profile = None
    profile_path = os.path.join(graph_dir, ".code2database_profile.json")
    if os.path.isfile(profile_path):
        try:
            import json
            with open(profile_path, "r", encoding="utf-8") as f:
                profile = json.load(f)
        except (IOError, OSError, ValueError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    result = happens_before_analysis(
        G, writer_id, reader_id, variable, profile=profile, max_depth=max_depth)

    # Verify writer actually writes the var and reader reads it (warning only,
    # suppressed when memory-ordering primitives suggest the access exists but
    # is hidden behind a pointer dereference the scanner didn't track)
    writer_mo = analyze_memory_ordering(G.nodes[writer_id], G=G, nid=writer_id)
    reader_mo = analyze_memory_ordering(G.nodes[reader_id], G=G, nid=reader_id)
    if not _writer_writes_var(G, writer_id, variable) and not writer_mo.has_any():
        print(f"Warning: writer {_get_function_name(G, writer_id)} "
              f"does not appear to write {variable!r}", file=sys.stderr)
    if not _reader_reads_var(G, reader_id, variable) and not reader_mo.has_any():
        print(f"Warning: reader {_get_function_name(G, reader_id)} "
              f"does not appear to read {variable!r}", file=sys.stderr)

    import json
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))


def cmd_memory_ordering(args):
    """Show memory-ordering primitives found in a function body.

    Examples:
      memory-ordering --graph out/ --node my_func
    """
    graph_dir = args.graph
    node_hint = args.node

    G = _load_full_graph(graph_dir)
    from _builder.utils import _find_node_id
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        print(f"Error: node matching {node_hint!r} not found", file=sys.stderr)
        sys.exit(1)

    nd = G.nodes[node_id]
    info = analyze_memory_ordering(nd, G=G, nid=node_id)

    import json
    output = {
        "node": node_id,
        "name": nd.get("name", ""),
        "rcu_read_locks": info.rcu_read_locks,
        "rcu_read_unlocks": info.rcu_read_unlocks,
        "smp_mb": info.smp_mb_lines,
        "smp_rmb": info.smp_rmb_lines,
        "smp_wmb": info.smp_wmb_lines,
        "smp_store_release": [{"var": v, "line": l}
                              for v, l in info.smp_store_release],
        "smp_load_acquire": [{"var": v, "line": l}
                             for v, l in info.smp_load_acquire],
        "read_once": [{"var": v, "line": l} for v, l in info.read_once],
        "write_once": [{"var": v, "line": l} for v, l in info.write_once],
        "atomic_ops": [{"var": v, "line": l} for v, l in info.atomic_ops],
        "atomic_thread_fences": [{"order": o, "line": l}
                                 for o, l in info.atomic_thread_fences],
        "has_acquire_semantics": info.has_acquire_semantics(),
        "has_release_semantics": info.has_release_semantics(),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
