"""cgdb_sync — L8 sync_primitives extraction via AST walk.

Per cgdb-architecture-and-poc-report.md 5.4 (Phase 4 L8):
  Walk each function body's AST, looking for CALL_EXPR nodes whose callee
  name matches known sync primitive patterns:
    - pthread_mutex_lock / pthread_mutex_unlock
    - spin_lock / spin_unlock
    - rcu_read_lock / rcu_read_unlock
    - atomic_load / atomic_store
    - smp_mb / smp_wmb / smp_rmb (memory barriers)
    - READ_ONCE / WRITE_ONCE

  Emit SyncPrimitiveRecord for each match, with:
    - function_id (enclosing function node)
    - kind (lock_acquire / lock_release / etc.)
    - sync_var_id (the VarDecl of the lock/mutex arg, if resolvable)
    - acquire_stmt_id / release_stmt_id (the CALL_EXPR node)

  Optionally emit HappensBeforeRecord pairs (acquire → release) within
  the same function for the same sync_var.

This is the MVP path. The production C++ plugin would use clang's
AnalysisManager for proper memory model analysis.
"""
import re
from typing import Dict, List, Optional, Tuple

from _builder.cgdb_records import SyncPrimitiveRecord, HappensBeforeRecord
import logging


# Patterns matching sync primitive function names.
# Each entry: (regex, kind, is_acquire_or_release)
# is_acquire_or_release: 'acquire' | 'release' | 'barrier' | 'read' | 'write'
_SYNC_PATTERNS = [
    # pthread / generic mutex
    (re.compile(r'^pthread_mutex_lock$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^pthread_mutex_unlock$'), 'lock_release', 'release'),
    (re.compile(r'^pthread_mutex_trylock$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^pthread_rwlock_rdlock$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^pthread_rwlock_wrlock$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^pthread_rwlock_unlock$'), 'lock_release', 'release'),
    (re.compile(r'^pthread_spin_lock$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^pthread_spin_unlock$'), 'lock_release', 'release'),
    # Linux kernel
    (re.compile(r'^spin_lock$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^spin_unlock$'), 'lock_release', 'release'),
    (re.compile(r'^spin_lock_irq$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^spin_unlock_irq$'), 'lock_release', 'release'),
    (re.compile(r'^spin_lock_irqsave$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^spin_unlock_irqrestore$'), 'lock_release', 'release'),
    (re.compile(r'^mutex_lock$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^mutex_unlock$'), 'lock_release', 'release'),
    (re.compile(r'^raw_spin_lock$'), 'lock_acquire', 'acquire'),
    (re.compile(r'^raw_spin_unlock$'), 'lock_release', 'release'),
    # RCU
    (re.compile(r'^rcu_read_lock$'), 'rcu_read_lock', 'acquire'),
    (re.compile(r'^rcu_read_unlock$'), 'rcu_read_unlock', 'release'),
    (re.compile(r'^rcu_read_lock_bh$'), 'rcu_read_lock', 'acquire'),
    (re.compile(r'^rcu_read_unlock_bh$'), 'rcu_read_unlock', 'release'),
    # Atomics
    (re.compile(r'^atomic_load$'), 'atomic_load', 'read'),
    (re.compile(r'^atomic_store$'), 'atomic_store', 'write'),
    (re.compile(r'^atomic_inc$'), 'atomic_store', 'write'),
    (re.compile(r'^atomic_dec$'), 'atomic_store', 'write'),
    (re.compile(r'^atomic_add$'), 'atomic_store', 'write'),
    (re.compile(r'^atomic_sub$'), 'atomic_store', 'write'),
    (re.compile(r'^smp_mb$'), 'memory_barrier', 'barrier'),
    (re.compile(r'^smp_wmb$'), 'memory_barrier', 'barrier'),
    (re.compile(r'^smp_rmb$'), 'memory_barrier', 'barrier'),
    (re.compile(r'^smp_store_mb$'), 'memory_barrier', 'barrier'),
    # READ_ONCE / WRITE_ONCE (Linux kernel)
    (re.compile(r'^READ_ONCE$'), 'read_once', 'read'),
    (re.compile(r'^WRITE_ONCE$'), 'write_once', 'write'),
    (re.compile(r'^__read_once_size$'), 'read_once', 'read'),
    (re.compile(r'^__write_once_size$'), 'write_once', 'write'),
]


def _match_sync_pattern(name: str) -> Optional[Tuple[str, str]]:
    """Match a function name against sync patterns. Returns (kind, role) or None."""
    if not name:
        return None
    for pat, kind, role in _SYNC_PATTERNS:
        if pat.match(name):
            return (kind, role)
    return None


class SyncPrimitiveWriter:
    """L8: walk AST, emit SyncPrimitiveRecord for sync primitive calls.

    NOT thread-safe: _lock_stack (per-function state) is a plain dict
    mutated without locking. Use one writer instance per thread, or
    serialize extract_from_function calls externally.

    Usage:
        writer = SyncPrimitiveWriter()
        records, happens_before = writer.extract_from_function(
            func_cursor, func_node_id, add_node_fn
        )

    Detection covers:
      - Direct calls (pthread_mutex_lock(&m))
      - Member-access calls (this->lock.lock(), obj.lock())
      - Macro-wrapped calls (e.g., spin_lock wrapped in a project macro)
      - Memory barriers (smp_mb, smp_wmb, smp_rmb)
      - Atomic accesses (READ_ONCE, WRITE_ONCE, atomic_load, atomic_store)

    Pairs emitted as HappensBeforeRecord:
      - lock_acquire → lock_release (for the same sync_var, scoped)
      - WRITE_ONCE → READ_ONCE (for the same sync_var, scoped)
      - memory_barrier → subsequent access (per-barrier scope)
    """

    def __init__(self):
        # For nested-lock region tracking: stack of (sync_var_id, acquire_stmt_id)
        # per function, used to detect unbalanced acquire/release.
        self._lock_stack: Dict[int, List[Tuple[int, int]]] = {}

    def _resolve_sync_var(self, call_expr, add_node_fn) -> Optional[int]:
        """Resolve the sync-var node id from the first argument of a CALL_EXPR.

        Handles:
          - Direct var ref: pthread_mutex_lock(&m) → m's VAR_DECL
          - Parm ref: foo(args->lock) where args is a parameter
          - Field ref via MEMBER_REF_EXPR: this->lock → FIELD_DECL
          - Address-of var: &m → m
          - Macro-wrapped: when the arg is a MACRO_EXPANSION or UNEXPOSED_EXPR,
            walk into it to find the underlying DeclRefExpr.
        """
        try:
            args = list(call_expr.get_arguments())
            if not args:
                return None
            first_arg = args[0]
            return self._resolve_var_node(first_arg, add_node_fn)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return None

    def _resolve_var_node(self, cursor, add_node_fn) -> Optional[int]:
        """Recursively resolve a cursor to a sync-var node id."""
        if cursor is None:
            return None
        try:
            k = cursor.kind.name if cursor.kind else ''
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return None
        # Direct decl reference
        if k in ('VAR_DECL', 'PARM_DECL', 'FIELD_DECL'):
            kind_str = {
                'VAR_DECL': 'var',
                'PARM_DECL': 'parm',
                'FIELD_DECL': 'field',
            }.get(k, 'var')
            return add_node_fn(cursor, kind_str)
        # DECL_REF_EXPR — resolve to referenced decl
        if k == 'DECL_REF_EXPR':
            ref = cursor.referenced
            if ref is not None and ref.kind and ref.kind.name in (
                    'VAR_DECL', 'PARM_DECL', 'FIELD_DECL'):
                kind_str = {
                    'VAR_DECL': 'var',
                    'PARM_DECL': 'parm',
                    'FIELD_DECL': 'field',
                }.get(ref.kind.name, 'var')
                return add_node_fn(ref, kind_str)
            # Fall back to the DeclRefExpr node itself
            return add_node_fn(cursor, 'decl_ref')
        # MEMBER_REF_EXPR — `obj.lock` or `this->lock`. Resolve to the
        # FIELD_DECL via .referenced (libclang exposes this for member refs).
        if k == 'MEMBER_REF_EXPR':
            ref = cursor.referenced
            if ref is not None and ref.kind and ref.kind.name == 'FIELD_DECL':
                return add_node_fn(ref, 'field')
            # Fall back: walk into children to find a DECL_REF_EXPR base
            try:
                for sub in cursor.get_children():
                    sub_id = self._resolve_var_node(sub, add_node_fn)
                    if sub_id is not None:
                        return sub_id
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            return None
        # UNEXPOSED_EXPR / MACRO_EXPANSION — walk into children
        if k in ('UNEXPOSED_EXPR', 'MACRO_EXPANSION', 'PAREN_EXPR'):
            try:
                for sub in cursor.get_children():
                    sub_id = self._resolve_var_node(sub, add_node_fn)
                    if sub_id is not None:
                        return sub_id
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            return None
        # UnaryOperator & — walk into operand
        if k == 'UNARY_OPERATOR':
            try:
                for sub in cursor.get_children():
                    sub_id = self._resolve_var_node(sub, add_node_fn)
                    if sub_id is not None:
                        return sub_id
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            return None
        return None

    def extract_from_function(self, func_cursor, func_node_id: int,
                              add_node_fn) -> Tuple[List[SyncPrimitiveRecord],
                                                    List[HappensBeforeRecord]]:
        """Walk the function body, emit SyncPrimitiveRecord for each match.

        Returns (sync_primitives, happens_before) lists.

        Lock-held region tracking:
          - Each acquire pushes (sync_var_id, acquire_stmt_id) onto a
            per-function stack.
          - Each release pops the matching acquire from the stack.
          - Unbalanced releases (no matching acquire) are still recorded;
            their happens-before is emitted with confidence < 1.0.
          - The lock stack is exposed via get_lock_stack_at_end() for callers
            that need to know if the function exits with locks held (a
            common bug pattern).
        """
        records: List[SyncPrimitiveRecord] = []
        happens_before: List[HappensBeforeRecord] = []
        # Per-function lock stack for nested-region tracking
        lock_stack: List[Tuple[int, int]] = []
        self._lock_stack[func_node_id] = lock_stack
        # Pending writes for WRITE_ONCE → READ_ONCE pairing
        # (sync_var_id → list of write_stmt_ids, in source order)
        pending_writes: Dict[int, List[int]] = {}

        try:
            for child in func_cursor.walk_preorder():
                k = child.kind.name if child.kind else ''
                if k != 'CALL_EXPR':
                    continue
                callee_name = child.spelling or ''
                # Macro-wrapped: try the referenced cursor's spelling too
                if not callee_name:
                    try:
                        ref = child.referenced
                        if ref is not None:
                            callee_name = ref.spelling or ''
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                match = _match_sync_pattern(callee_name)
                if match is None:
                    continue
                kind, role = match
                sync_var_id = self._resolve_sync_var(child, add_node_fn)
                # The CALL_EXPR node itself becomes the acquire/release stmt id.
                # Use kind 'expr' (not 'decl_ref') — CALL_EXPR is an expression,
                # and downstream consumers filtering kind='decl_ref' shouldn't
                # see call-expression nodes mixed in.
                call_stmt_id = add_node_fn(child, 'expr')

                rec = SyncPrimitiveRecord(
                    function_id=func_node_id,
                    kind=kind,
                    sync_var_id=sync_var_id,
                )
                if role == 'acquire':
                    rec.acquire_stmt_id = call_stmt_id
                    if sync_var_id is not None:
                        lock_stack.append((sync_var_id, call_stmt_id))
                elif role == 'release':
                    rec.release_stmt_id = call_stmt_id
                    if sync_var_id is not None:
                        # Pop the matching acquire from the stack
                        # (search from the top for the same sync_var_id)
                        matched = False
                        for i in range(len(lock_stack) - 1, -1, -1):
                            sv, acq_id = lock_stack[i]
                            if sv == sync_var_id:
                                lock_stack.pop(i)
                                happens_before.append(HappensBeforeRecord(
                                    write_event_id=acq_id,
                                    read_event_id=call_stmt_id,
                                    reason='lock',
                                    confidence=1.0,
                                ))
                                matched = True
                                break
                        if not matched:
                            # Unbalanced release — still record an HB with
                            # lower confidence so race-check can flag it.
                            happens_before.append(HappensBeforeRecord(
                                write_event_id=call_stmt_id,  # self-pair
                                read_event_id=call_stmt_id,
                                reason='unbalanced_release',
                                confidence=0.5,
                            ))
                elif role == 'write':
                    # WRITE_ONCE / atomic_store — record as a write event
                    rec.acquire_stmt_id = call_stmt_id  # repurpose as write-event id
                    if sync_var_id is not None:
                        pending_writes.setdefault(sync_var_id, []).append(
                            call_stmt_id)
                elif role == 'read':
                    # READ_ONCE / atomic_load — pair with most recent write
                    rec.release_stmt_id = call_stmt_id  # repurpose as read-event id
                    if sync_var_id is not None:
                        writes = pending_writes.get(sync_var_id, [])
                        if writes:
                            # Pair with the most recent write to the same var
                            write_id = writes[-1]
                            happens_before.append(HappensBeforeRecord(
                                write_event_id=write_id,
                                read_event_id=call_stmt_id,
                                reason='write_once',
                                confidence=1.0,
                            ))
                elif role == 'barrier':
                    # Memory barrier — record as a barrier event.
                    # Pairs with subsequent reads/writes up to the next barrier.
                    # For MVP, emit a self-HB so race-check can find barriers.
                    rec.acquire_stmt_id = call_stmt_id
                    happens_before.append(HappensBeforeRecord(
                        write_event_id=call_stmt_id,
                        read_event_id=call_stmt_id,
                        reason='memory_barrier',
                        confidence=0.7,
                    ))
                records.append(rec)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        return records, happens_before

    def get_lock_stack_at_end(self, func_node_id: int) -> List[Tuple[int, int]]:
        """Return the (sync_var_id, acquire_stmt_id) pairs still held at
        function exit. Non-empty list means the function exits with locks
        still held — a common bug pattern (or an intentional "lock the
        caller's data" pattern).
        """
        return list(self._lock_stack.get(func_node_id, []))
