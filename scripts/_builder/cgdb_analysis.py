"""cgdb_analysis — L4-L6 + L8 extraction via clang static analyzer CLI.

Per cgdb-architecture-and-poc-report.md 5.4 (Phase 4):
  L4 (CFG):    `clang -cc1 -analyze -analyzer-checker=debug.DumpCFG`
  L5 (data flow): `clang -cc1 -analyze -analyzer-checker=debug.DumpLiveVars`
                  + AST-level def-use via DeclRefExpr tracking
  L6 (alias):  `clang -cc1 -analyze -analyzer-checker=debug.DumpDominators`
                + heuristics on pointer assignments (MVP — clang's full
                alias analysis is in AnalysisManager, not exposed via CLI)
  L8 (concurrency): reuse `_builder/lock_coverage.py` for sync primitive
                     detection, write to sync_primitives table

The clang static analyzer dumps text output that we parse with regex.
This is the MVP approach; production would use the C++ clang plugin
with AnalysisManager access for in-process CFG/data-flow.

Each extractor takes the source file path + function name (for per-function
extraction) and returns a list of records (BasicBlockRecord, CFGEdgeRecord,
DataFlowRecord) for the IngestBatch.
"""
import logging
import hashlib
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from _builder.cgdb_records import (
    BasicBlockRecord, CFGEdgeRecord, DataFlowRecord, ConditionRecord,
)


# Regex for parsing DumpCFG output
_BLOCK_HEADER_RE = re.compile(r'^\s*\[B(\d+)\s*(\((ENTRY|EXIT)\))?\]\s*$')
_BLOCK_SUCCS_RE = re.compile(r'^\s*Suces\s*\(\d+\):\s*(B\d+(?:\s+B\d+)*)\s*$')
_BLOCK_PREDS_RE = re.compile(r'^\s*Preds\s*\(\d+\):\s*(B\d+(?:\s+B\d+)*)\s*$')
_BLOCK_SUCCS_RE2 = re.compile(r'^\s*Succs\s*\(\d+\):\s*(B\d+(?:\s+B\d+)*)\s*$')
_FUNC_HEADER_RE = re.compile(r'^\s*(int|void|char|float|double|long|short|unsigned|signed|struct|enum|union|static|inline|const|volatile|[A-Za-z_][A-Za-z0-9_]*)\s+\*?(\w+)\s*\([^)]*\)\s*$')

# Regex for parsing DumpLiveVars (live variables per block)
_LIVE_VAR_RE = re.compile(r'^\[\s*B(\d+)\s+\(live variables at block exit\)\s*\]\s*$')
_LIVE_VAR_NAME_RE = re.compile(r'^\s+(\w+)\s+<([^>]+)>')

# Regex for parsing DumpDominators
_DOM_HEADER_RE = re.compile(r'Immediate dominance tree \(Node#,IDom#\):')
_DOM_PAIR_RE = re.compile(r'^\((\d+),(\d+)\)')


def _block_id_for(func_id: int, block_index: int) -> int:
    """Stable 60-bit ID for a basic block (function_id, block_index)."""
    h = hashlib.sha256(f"bb|{func_id}|{block_index}".encode('utf-8')).hexdigest()[:15]
    return int(h, 16) & 0x0FFF_FFFF_FFFF_FFFF


class CFGExtractor:
    """L4: extract basic blocks + CFG edges from clang's DumpCFG output.

    Usage:
        extractor = CFGExtractor()
        blocks, edges = extractor.extract(source_path, function_name, func_node_id)

    Caches the DumpCFG output per source_path so multiple functions in the
    same file don't each trigger a separate clang subprocess.
    """

    def __init__(self, clang_bin: str = 'clang'):
        self.clang_bin = clang_bin
        self._dump_cache: Dict[str, str] = {}  # source_path → dump text

    def _run_dump_cfg(self, source_path: str) -> str:
        """Run clang -cc1 -analyze -analyzer-checker=debug.DumpCFG. Returns stdout.
        Note: clang's debug.DumpCFG checker writes its output to stderr, not stdout.
        """
        if source_path in self._dump_cache:
            return self._dump_cache[source_path]
        try:
            result = subprocess.run(
                [self.clang_bin, '-cc1', '-analyze',
                 '-analyzer-checker=debug.DumpCFG', source_path],
                capture_output=True, text=True, timeout=30,
            )
            # DumpCFG writes to stderr (clang's debug checkers use llvm::errs())
            dump = result.stderr or result.stdout or ''
            self._dump_cache[source_path] = dump
            return dump
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            self._dump_cache[source_path] = ''
            return ''

    def extract(self, source_path: str, function_name: str,
                func_node_id: int,
                tu_cursor=None, func_cursor=None) -> Tuple[List[BasicBlockRecord], List[CFGEdgeRecord]]:
        """Extract basic blocks + CFG edges for a function.

        Returns (basic_blocks, cfg_edges) lists. Empty if both clang -cc1
        and the libclang fallback fail, or the function isn't found.

        If func_cursor is provided (a clang FUNCTION_DECL cursor for the
        target function), the libclang fallback uses it directly without
        walking the entire TU AST. This is the recommended path when the
        caller already has the cursor — it avoids O(N×M) re-traversal of
        the TU AST for N functions × M AST nodes per TU.

        If tu_cursor is provided (a clang TranslationUnit root cursor)
        and func_cursor is None, the libclang fallback walks the TU AST
        to find the function (slower; kept for backward compatibility).

        If neither is provided, clang -cc1 is attempted; if that fails,
        the source is parsed via libclang as a last resort.
        """
        if func_cursor is not None:
            blocks, edges = self._extract_via_libclang(
                tu_cursor, function_name, func_node_id, source_path,
                func_cursor=func_cursor)
            if blocks:
                return blocks, edges
            # Fall through to clang -cc1 if libclang path returned nothing
        elif tu_cursor is not None:
            blocks, edges = self._extract_via_libclang(
                tu_cursor, function_name, func_node_id, source_path)
            if blocks:
                return blocks, edges
            # Fall through to clang -cc1 if libclang path returned nothing
        dump = self._run_dump_cfg(source_path)
        if not dump:
            # Fallback to libclang AST-based CFG if no cursor was passed
            if tu_cursor is None:
                return self._extract_via_libclang(
                    None, function_name, func_node_id, source_path)
            return [], []
        return self._parse_dump(dump, function_name, func_node_id, source_path)

    def _extract_via_libclang(self, tu_cursor, function_name: str,
                               func_node_id: int,
                               source_path: str,
                               func_cursor=None
                               ) -> Tuple[List[BasicBlockRecord], List[CFGEdgeRecord]]:
        """Libclang AST-based CFG extraction fallback.

        Walks the function body's CompoundStmt and approximates basic blocks
        using control-flow statements (IfStmt, ForStmt, WhileStmt, DoStmt,
        SwitchStmt, ConditionalOperator). Each control-flow statement introduces
        a branch; sequential statements are grouped into a single block.

        Less precise than clang's real CFG (no proper phi-nodes, no expression-
        level blocks), but covers the common case when clang -cc1 is unavailable
        or fails (e.g., missing system headers).

        If func_cursor is provided, it is used directly — skipping the
        O(M) walk of the entire TU AST to find the function. This is the
        fast path. If func_cursor is None, the TU AST is walked to find
        the FUNCTION_DECL with the matching name (slow path).
        """
        blocks: List[BasicBlockRecord] = []
        edges: List[CFGEdgeRecord] = []

        if func_cursor is None:
            if tu_cursor is None:
                # Try to parse the source file with libclang
                try:
                    import clang.clang as _clang_mod  # noqa: F401
                except ImportError:
                    return [], []
                try:
                    from clang.cindex import Index, TranslationUnit
                    index = Index.create()
                    tu = index.parse(source_path)
                    tu_cursor = tu.cursor
                except Exception:
                    return [], []

            try:
                from clang.cindex import CursorKind
            except ImportError:
                return [], []

            # Find the function definition (slow path: walks the entire TU AST)
            try:
                for top in tu_cursor.walk_preorder():
                    if (top.kind == CursorKind.FUNCTION_DECL
                            and top.spelling == function_name
                            and top.is_definition()):
                        func_cursor = top
                        break
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            if func_cursor is None:
                return [], []
        else:
            try:
                from clang.cindex import CursorKind
            except ImportError:
                return [], []

        # Find the body (CompoundStmt)
        body = None
        try:
            for child in func_cursor.get_children():
                if child.kind == CursorKind.COMPOUND_STMT:
                    body = child
                    break
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        if body is None:
            return [], []

        # Walk the body, assigning block indices
        # Block 0 = entry, Block N+1 = exit
        entry_id = _block_id_for(func_node_id, 0)
        blocks.append(BasicBlockRecord(
            id=entry_id,
            function_id=func_node_id,
            block_index=0,
            is_entry=True,
            is_exit=False,
        ))

        # Each direct child of body becomes its own block; control-flow
        # statements add branch blocks.
        next_block_idx = 1
        block_succs: Dict[int, List[Tuple[int, str]]] = {0: []}
        block_id_for_idx: Dict[int, int] = {0: entry_id}

        def new_block() -> int:
            nonlocal next_block_idx
            idx = next_block_idx
            next_block_idx += 1
            bid = _block_id_for(func_node_id, idx)
            block_id_for_idx[idx] = bid
            block_succs[idx] = []
            blocks.append(BasicBlockRecord(
                id=bid,
                function_id=func_node_id,
                block_index=idx,
                is_entry=False,
                is_exit=False,
            ))
            return idx

        # Sequential walk: each top-level stmt gets its own block, fallthrough
        # to the next.
        prev_idx = 0
        sequential_blocks: List[int] = []
        try:
            children = list(body.get_children())
        except Exception:
            children = []
        for stmt in children:
            idx = new_block()
            sequential_blocks.append(idx)
            if prev_idx is not None:
                block_succs[prev_idx].append((idx, 'fallthrough'))
            # Detect control-flow statements and add branch successors
            try:
                kind_name = stmt.kind.name if stmt.kind else ''
            except Exception:
                kind_name = ''
            if kind_name == 'IF_STMT':
                # then-branch and else-branch as new blocks
                then_idx = new_block()
                else_idx = new_block()
                block_succs[idx].append((then_idx, 'true_branch'))
                block_succs[idx].append((else_idx, 'false_branch'))
                # Both branches fall through to whatever comes next
                # (we'll fix this in the post-pass below)
                sequential_blocks.extend([then_idx, else_idx])
                prev_idx = None  # branch, no direct fallthrough from idx
                # Then/else need to fall through to the next top-level stmt
                # Stash for post-pass
                block_succs[then_idx].append((-1, 'fallthrough'))
                block_succs[else_idx].append((-1, 'fallthrough'))
            elif kind_name in ('FOR_STMT', 'WHILE_STMT', 'DO_STMT'):
                # Loop body block + back-edge + exit branch
                body_idx = new_block()
                block_succs[idx].append((body_idx, 'true_branch'))
                block_succs[idx].append((-1, 'false_branch'))  # to next stmt
                block_succs[body_idx].append((idx, 'fallthrough'))  # back-edge
                sequential_blocks.append(body_idx)
                prev_idx = None
            elif kind_name == 'SWITCH_STMT':
                # Approximate: one block per case, all fall through to next
                # stmt at the end.
                case_blocks: List[int] = []
                try:
                    for sub in stmt.get_children():
                        if sub.kind and sub.kind.name == 'CASE_STMT':
                            cidx = new_block()
                            case_blocks.append(cidx)
                            block_succs[idx].append((cidx, 'true_branch'))
                            block_succs[cidx].append((-1, 'fallthrough'))
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
                sequential_blocks.extend(case_blocks)
                prev_idx = None
            else:
                prev_idx = idx
        # Add exit block
        exit_idx = new_block()
        exit_id = block_id_for_idx[exit_idx]
        # Mark exit
        for b in blocks:
            if b.id == exit_id:
                b.is_exit = True
        # Resolve -1 (next-stmt) placeholders
        # sequential_blocks[i] should fall through to sequential_blocks[i+1],
        # or to exit_idx if last.
        for i, idx in enumerate(sequential_blocks):
            next_idx = sequential_blocks[i + 1] if i + 1 < len(sequential_blocks) else exit_idx
            new_succs = []
            for (s, k) in block_succs.get(idx, []):
                if s == -1:
                    new_succs.append((next_idx, k))
                else:
                    new_succs.append((s, k))
            block_succs[idx] = new_succs
        # If prev_idx was set (last stmt was sequential), link to exit
        if prev_idx is not None and prev_idx != exit_idx:
            block_succs[prev_idx].append((exit_idx, 'fallthrough'))
        # Also link entry to first block
        if sequential_blocks:
            first = sequential_blocks[0]
            if (first, 'fallthrough') not in block_succs[0]:
                block_succs[0].append((first, 'fallthrough'))
        else:
            # Empty body: entry → exit
            block_succs[0].append((exit_idx, 'fallthrough'))
        # Emit edges
        for src_idx, succs in block_succs.items():
            src_id = block_id_for_idx[src_idx]
            for (dst_idx, kind) in succs:
                if dst_idx < 0:
                    continue
                dst_id = block_id_for_idx.get(dst_idx)
                if dst_id is None:
                    continue
                edges.append(CFGEdgeRecord(
                    src_block_id=src_id,
                    dst_block_id=dst_id,
                    kind=kind,
                    function_id=func_node_id,
                ))
        return blocks, edges

    def _parse_dump(self, dump: str, function_name: str,
                    func_node_id: int, source_path: str
                    ) -> Tuple[List[BasicBlockRecord], List[CFGEdgeRecord]]:
        """Parse the DumpCFG text for the named function.

        The dump contains multiple functions separated by blank lines.
        Each function starts with a signature line, then basic blocks.
        """
        blocks: List[BasicBlockRecord] = []
        edges: List[CFGEdgeRecord] = []

        # Find the function's CFG section
        lines = dump.splitlines()
        in_func = False
        cur_block_index = None
        cur_block_succs: List[int] = []
        cur_block_preds: List[int] = []
        cur_block_stmts: List[str] = []
        is_entry = False
        is_exit = False
        cur_block_first_line = 0

        # Helper to flush the current block
        def flush_block():
            nonlocal cur_block_index, cur_block_succs, cur_block_preds
            nonlocal cur_block_stmts, is_entry, is_exit, cur_block_first_line
            if cur_block_index is None:
                return
            block_id = _block_id_for(func_node_id, cur_block_index)
            blocks.append(BasicBlockRecord(
                id=block_id,
                function_id=func_node_id,
                block_index=cur_block_index,
                is_entry=is_entry,
                is_exit=is_exit,
            ))
            # Emit edges to each successor
            for succ_idx, succ in enumerate(cur_block_succs):
                succ_id = _block_id_for(func_node_id, succ)
                # Edge kind: if the block has multiple successors, it's a
                # branch (true_branch / false_branch). Otherwise fallthrough.
                if len(cur_block_succs) == 1:
                    edge_kind = 'fallthrough'
                else:
                    # First successor = true_branch, second = false_branch
                    # (clang's CFG convention)
                    edge_kind = 'true_branch' if succ_idx == 0 else 'false_branch'
                edges.append(CFGEdgeRecord(
                    src_block_id=block_id,
                    dst_block_id=succ_id,
                    kind=edge_kind,
                    function_id=func_node_id,
                ))
            # Reset
            cur_block_index = None
            cur_block_succs = []
            cur_block_preds = []
            cur_block_stmts = []
            is_entry = False
            is_exit = False
            cur_block_first_line = 0

        # Walk lines, find function start, then parse blocks
        i = 0
        while i < len(lines):
            line = lines[i]
            # Check for function signature
            if not in_func:
                # Look for "<func_name>(" pattern with a return type prefix
                m = _FUNC_HEADER_RE.match(line)
                if m and m.group(2) == function_name:
                    in_func = True
                    i += 1
                    continue
                # Not the function we're looking for — advance to avoid
                # an infinite loop on lines that match the function-header
                # regex but belong to a different function.
                i += 1
                continue
            else:
                # Check for block header
                m = _BLOCK_HEADER_RE.match(line)
                if m:
                    # Flush previous block
                    flush_block()
                    cur_block_index = int(m.group(1))
                    label = m.group(3)
                    is_entry = (label == 'ENTRY')
                    is_exit = (label == 'EXIT')
                    i += 1
                    continue
                # Check for Succs line
                m = _BLOCK_SUCCS_RE2.match(line) or _BLOCK_SUCCS_RE.match(line)
                if m and cur_block_index is not None:
                    succs_str = m.group(1)
                    for s in succs_str.split():
                        if s.startswith('B'):
                            try:
                                cur_block_succs.append(int(s[1:]))
                            except ValueError:
                                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                                pass
                    i += 1
                    continue
                # Check for Preds line
                m = _BLOCK_PREDS_RE.match(line)
                if m and cur_block_index is not None:
                    preds_str = m.group(1)
                    for p in preds_str.split():
                        if p.startswith('B'):
                            try:
                                cur_block_preds.append(int(p[1:]))
                            except ValueError:
                                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                                pass
                    i += 1
                    continue
                # Check for end of function (blank line followed by next function
                # signature or EOF)
                if line.strip() == '' and cur_block_index is not None:
                    # Could be end of function or end of block. Look ahead.
                    # If the next non-blank line is another block header or
                    # function signature, continue. Otherwise, end of function.
                    j = i + 1
                    while j < len(lines) and lines[j].strip() == '':
                        j += 1
                    if j >= len(lines):
                        flush_block()
                        in_func = False
                        i += 1
                        continue
                    next_line = lines[j]
                    next_m = _BLOCK_HEADER_RE.match(next_line)
                    next_func = _FUNC_HEADER_RE.match(next_line)
                    if next_m:
                        # Continue (just a blank line in the middle)
                        i += 1
                        continue
                    elif next_func:
                        # End of current function
                        flush_block()
                        in_func = False
                        i += 1
                        continue
                    else:
                        # Statement line — append to current block
                        if cur_block_index is not None:
                            cur_block_stmts.append(line)
                        i += 1
                        continue
                # Statement line within a block
                if cur_block_index is not None:
                    cur_block_stmts.append(line)
                i += 1
        # Flush final block
        if in_func:
            flush_block()
        return blocks, edges


def _cfg_edge_id(src_id: int, dst_id: int, kind: str) -> int:
    """Stable 60-bit CFG edge ID."""
    h = hashlib.sha256(f"cfg|{src_id}|{dst_id}|{kind}".encode('utf-8')).hexdigest()[:15]
    return int(h, 16) & 0x0FFF_FFFF_FFFF_FFFF


def _condition_id(func_id: int, text_form: str) -> int:
    """Stable 60-bit ID for a condition expression within a function."""
    h = hashlib.sha256(
        f"cond|{func_id}|{text_form}".encode('utf-8')
    ).hexdigest()[:15]
    return int(h, 16) & 0x0FFF_FFFF_FFFF_FFFF


_COMPARISON_OPS = {
    '==': '==', '!=': '!=', '<': '<', '<=': '<=', '>': '>', '>=': '>=',
}
_LOGICAL_OPS = {'&&': '&&', '||': '||', '!': '!'}


def _cursor_text(cursor) -> str:
    """Best-effort textual reconstruction of a clang cursor expression."""
    try:
        tokens = list(cursor.get_tokens())
    except Exception:
        return ''
    if not tokens:
        return ''
    try:
        spans = []
        for tok in tokens:
            try:
                spans.append(tok.spelling)
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
        return ' '.join(spans).strip()
    except Exception:
        return ''


def _text_to_z3(text: str) -> str:
    """Lightweight text → Z3 form conversion for common comparison atoms.

    Returns an empty string for atoms we can't model (e.g., calls, macros).
    The intent is to give path-feasibility a starting point; complex
    expressions fall back to heuristic mode.
    """
    if not text:
        return ''
    expr = text.strip()
    for op in ('==', '!=', '<=', '>=', '<', '>'):
        if op in expr:
            parts = expr.split(op, 1)
            if len(parts) == 2:
                lhs = parts[0].strip()
                rhs = parts[1].strip()
                if lhs and rhs:
                    return f"({op} {lhs} {rhs})"
    return ''


class ConditionExtractor:
    """L3: walk a function AST and emit ConditionRecord atoms for branches.

    For each IfStmt / WhileStmt / ForStmt / DoStmt / SwitchStmt / ConditionalOperator,
    emit one ConditionRecord with text_form (the source text of the condition)
    and z3_form (a lightweight SMT string for simple comparisons).
    """

    def __init__(self):
        pass

    def extract_from_ast(self, func_cursor, func_node_id: int
                         ) -> List[ConditionRecord]:
        """Walk the function body and emit ConditionRecords."""
        try:
            from clang.cindex import CursorKind
        except ImportError:
            return []
        if func_cursor is None:
            return []
        records: List[ConditionRecord] = []
        seen_text: set = set()

        def _emit(condition_cursor, kind: str = 'comparison') -> None:
            text = _cursor_text(condition_cursor)
            if not text or text in seen_text:
                return
            seen_text.add(text)
            cid = _condition_id(func_node_id, text)
            records.append(ConditionRecord(
                id=cid,
                root_expr_id=cid,
                kind=kind,
                operator='',
                left_expr_id=None,
                right_expr_id=None,
                text_form=text,
                z3_form=_text_to_z3(text),
                attrs={'function_id': func_node_id},
            ))

        try:
            for stmt in func_cursor.walk_preorder():
                kind_name = stmt.kind.name if stmt.kind else ''
                if kind_name == 'IF_STMT':
                    try:
                        cond = next(stmt.get_children())
                        _emit(cond, 'comparison')
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                elif kind_name in ('WHILE_STMT', 'FOR_STMT', 'DO_STMT'):
                    try:
                        cond = next(stmt.get_children())
                        _emit(cond, 'comparison')
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                elif kind_name == 'SWITCH_STMT':
                    try:
                        cond = next(stmt.get_children())
                        _emit(cond, 'atom')
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                elif kind_name == 'CONDITIONAL_OPERATOR':
                    try:
                        children = list(stmt.get_children())
                        if children:
                            _emit(children[0], 'comparison')
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        return records


class DataFlowExtractor:
    """L5: extract def-use chains from clang's DumpLiveVars + AST analysis.

    Combines two layers:
      1. clang -cc1 -analyzer-checker=debug.DumpLiveVars output (when available)
         — provides block-level live-in/live-out sets that we use to mark
         each DeclRefExpr as a "def" or "use" with precise liveness info.
      2. libclang AST walk — emits DataFlowRecord entries linking each
         def-statement to subsequent uses of the same variable within the
         same function. Falls back to this when DumpLiveVars is unavailable.

    Def/use classification:
      - VAR_DECL with initializer → def
      - DeclRefExpr on LHS of BinaryOperator '=' → def
      - DeclRefExpr on RHS of BinaryOperator '=' or in other expression
        contexts → use
      - DeclRefExpr passed by reference to a call (UnaryOperator &) →
        may-def (conservatively treated as def for AI to chase)
      - DeclRefExpr inside a call argument (pass-by-pointer-to-struct)
        → may-use
    """

    def __init__(self, clang_bin: str = 'clang'):
        self.clang_bin = clang_bin
        self._live_vars_cache: Dict[str, str] = {}  # source_path → dump

    def _run_dump_live_vars(self, source_path: str) -> str:
        """Run clang -cc1 -analyze -analyzer-checker=debug.DumpLiveVars.

        Returns the dump text (empty on failure). Cached per source path.
        """
        if source_path in self._live_vars_cache:
            return self._live_vars_cache[source_path]
        try:
            result = subprocess.run(
                [self.clang_bin, '-cc1', '-analyze',
                 '-analyzer-checker=debug.DumpLiveVars', source_path],
                capture_output=True, text=True, timeout=30,
            )
            dump = result.stderr or result.stdout or ''
            self._live_vars_cache[source_path] = dump
            return dump
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            self._live_vars_cache[source_path] = ''
            return ''

    def _parse_live_vars(self, dump: str, function_name: str
                          ) -> Dict[int, Dict[str, List[str]]]:
        """Parse DumpLiveVars output for a function.

        Returns dict mapping block_index → {'live_in': [...], 'live_out': [...]}.

        DumpLiveVars output format (per block):
            [B3]
            live-in:
                x
                y
            live-out:
                z
        """
        out: Dict[int, Dict[str, List[str]]] = {}
        if not dump:
            return out
        lines = dump.splitlines()
        in_func = False
        cur_block = None
        cur_section = None  # 'live_in' or 'live_out'
        try:
            i = 0
            while i < len(lines):
                line = lines[i]
                if not in_func:
                    if _FUNC_HEADER_RE.match(line) and function_name in line:
                        in_func = True
                    i += 1
                    continue
                m = _BLOCK_HEADER_RE.match(line)
                if m:
                    cur_block = int(m.group(1))
                    out[cur_block] = {'live_in': [], 'live_out': []}
                    cur_section = None
                    i += 1
                    continue
                if cur_block is not None:
                    stripped = line.strip()
                    if stripped == 'live-in:':
                        cur_section = 'live_in'
                        i += 1
                        continue
                    if stripped == 'live-out:':
                        cur_section = 'live_out'
                        i += 1
                        continue
                    if stripped == '' or stripped.startswith('['):
                        cur_section = None
                        i += 1
                        continue
                    if cur_section and stripped:
                        out[cur_block][cur_section].append(stripped)
                i += 1
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        return out

    def extract_from_ast(self, func_cursor, func_node_id: int,
                         add_node_fn, filepath: str,
                         live_vars: Optional[Dict[int, Dict[str, List[str]]]] = None
                         ) -> List[DataFlowRecord]:
        """Walk the function body's AST, emit DataFlowRecord for each
        def/use of a local variable.

        Args:
          func_cursor: the FunctionDecl cursor (must be a definition)
          func_node_id: cgdb node id of the function
          add_node_fn: callable(cursor, kind_override) → node_id (de-dups)
          filepath: source file path (used for DumpLiveVars lookup if
            live_vars is None)
          live_vars: optional pre-parsed live-vars dict (block →
            {live_in, live_out}). If provided, used to attach liveness
            metadata. If None and filepath is provided, attempts to run
            DumpLiveVars; if that fails, falls back to AST-only analysis.

        Returns list of DataFlowRecord entries.
        """
        records: List[DataFlowRecord] = []
        try:
            # Try to get live-vars if not provided
            if live_vars is None and filepath:
                try:
                    dump = self._run_dump_live_vars(filepath)
                    func_name = func_cursor.spelling or ''
                    if dump and func_name:
                        live_vars = self._parse_live_vars(dump, func_name)
                except Exception:
                    live_vars = None

            # First pass: collect VarDecls (initial defs)
            var_decls = {}  # var_name → (var_node_id, def_stmt_node_id, def_order)
            decl_order = 0
            for child in func_cursor.walk_preorder():
                k = child.kind.name if child.kind else ''
                if k == 'VAR_DECL' and child.spelling:
                    var_node_id = add_node_fn(child, 'var')
                    var_decls[child.spelling] = (
                        var_node_id, var_node_id, decl_order)
                    # Initial def (declaration with optional initializer)
                    records.append(DataFlowRecord(
                        var_id=var_node_id,
                        def_stmt_id=var_node_id,
                        use_stmt_id=var_node_id,
                        function_id=func_node_id,
                        kind='def',
                    ))
                    decl_order += 1

            # Second pass: walk function body in source order, classify each
            # DeclRefExpr as def (LHS of `=`), may-def (address-of in call),
            # or use (everything else).
            try:
                from clang.cindex import CursorKind
            except ImportError:
                CursorKind = None

            def _classify_decl_ref(decl_ref, parent_cursor):
                """Classify a DeclRefExpr by its syntactic context.

                Returns one of: 'def', 'may_def', 'use', 'may_use'.
                """
                if parent_cursor is None:
                    return 'use'
                pk = parent_cursor.kind.name if parent_cursor.kind else ''
                # LHS of BinaryOperator '=' (writing to the variable)
                if pk == 'BINARY_OPERATOR' and parent_cursor.spelling == '=':
                    try:
                        children = list(parent_cursor.get_children())
                        if children and children[0].kind == CursorKind.DECL_REF_EXPR:
                            if children[0].referenced and \
                               children[0].referenced.spelling == decl_ref.spelling:
                                return 'def'
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                if pk == 'COMPOUND_ASSIGNMENT_OPERATOR':
                    try:
                        children = list(parent_cursor.get_children())
                        if children and children[0].kind == CursorKind.DECL_REF_EXPR:
                            if children[0].referenced and \
                               children[0].referenced.spelling == decl_ref.spelling:
                                return 'may_def'  # reads-then-writes
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                if pk == 'UNARY_OPERATOR' and parent_cursor.spelling == '&':
                    return 'may_def'
                # Inside a CALL_EXPR argument — may-use (callee may read it,
                # and if it's a pointer to a struct, may-def too). For AI's
                # purposes, mark as 'may_use' so the call chain is queryable.
                if pk == 'CALL_EXPR':
                    return 'may_use'
                # UnaryOperator * (dereference on LHS would be def, but
                # for pointer dereferences we conservatively mark as may_use)
                if pk == 'UNARY_OPERATOR' and parent_cursor.spelling == '*':
                    return 'may_use'
                # Member access (decl_ref.x or decl_ref->x) — use
                if pk == 'MEMBER_REF_EXPR':
                    return 'use'
                return 'use'

            # Walk the AST building (decl_ref, parent) pairs
            # We track parent via a stack
            def _walk_with_parent(cursor, parent):
                k = cursor.kind.name if cursor.kind else ''
                if k == 'DECL_REF_EXPR' and cursor.spelling:
                    if cursor.spelling in var_decls:
                        var_node_id, def_stmt_id, _ = var_decls[cursor.spelling]
                        use_stmt_id = add_node_fn(cursor, 'decl_ref')
                        kind = _classify_decl_ref(cursor, parent)
                        records.append(DataFlowRecord(
                            var_id=var_node_id,
                            def_stmt_id=def_stmt_id,
                            use_stmt_id=use_stmt_id,
                            function_id=func_node_id,
                            kind=kind,
                        ))
                try:
                    for child in cursor.get_children():
                        _walk_with_parent(child, cursor)
                except Exception:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            _walk_with_parent(func_cursor, None)

            # If we have live-vars data, attach metadata to records.
            # For MVP, we don't filter records based on liveness (we'd need
            # block-id mapping per statement, which requires deeper AST
            # integration). But we record the live-in/live-out sets as
            # auxiliary DataFlowRecord entries with kind='live_in'/'live_out'
            # so they're queryable.
            if live_vars:
                for block_idx, liveness in live_vars.items():
                    for var_name in liveness.get('live_in', []):
                        if var_name in var_decls:
                            var_node_id, _, _ = var_decls[var_name]
                            records.append(DataFlowRecord(
                                var_id=var_node_id,
                                def_stmt_id=var_node_id,
                                use_stmt_id=var_node_id,
                                function_id=func_node_id,
                                kind='live_in',
                            ))
                    for var_name in liveness.get('live_out', []):
                        if var_name in var_decls:
                            var_node_id, _, _ = var_decls[var_name]
                            records.append(DataFlowRecord(
                                var_id=var_node_id,
                                def_stmt_id=var_node_id,
                                use_stmt_id=var_node_id,
                                function_id=func_node_id,
                                kind='live_out',
                            ))
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        return records


class AliasExtractor:
    """L6: extract alias sets — pointers that may point to the same memory.

    Per cgdb-architecture-and-poc-report.md 5.5.6, this is a heuristic
    pointer-alias analysis. clang's full alias analysis lives in
    AnalysisManager (C++ only, not exposed via CLI), so we use a
    syntactic heuristic:

    1. **Direct assignment**  `T *p = q;`  →  p and q must_alias
    2. **Common source**      `T *p = q; T *r = q;`  →  p and r must_alias
    3. **Address-of same var** `T *p = &x; T *r = &x;`  →  p and r must_alias
    4. **Offset assignment**  `T *p = q + N;`  →  p and q may_alias
    5. **Parameter-to-local** `T *p = param;`  →  p and param must_alias

    Each VarDecl of pointer type is added as a node; assignments are
    detected via VAR_DECL's initializer or via BinaryOperator `=` with
    a DECL_REF_EXPR LHS.
    """

    def __init__(self):
        pass

    def extract_from_ast(self, func_cursor, func_node_id: int,
                         add_node_fn) -> List:
        """Walk the function body, emit AliasSetRecord entries.

        Returns list of AliasSetRecord entries.
        """
        try:
            from _builder.cgdb_records import AliasSetRecord
        except ImportError:
            return []
        records: List[AliasSetRecord] = []
        try:
            # Collect pointer-typed VarDecls with their initializers.
            # ptr_var_map: var_name → (var_node_id, source_kind, source_name)
            # where source_kind is one of: 'var', 'addr_of', 'offset', 'unknown'
            ptr_var_map: Dict[str, Tuple[int, str, str]] = {}
            # Collect ParmDecls of pointer type (function parameters)
            param_ptrs: Dict[str, int] = {}

            # First pass: collect pointer-typed parameters and locals
            for child in func_cursor.walk_preorder():
                k = child.kind.name if child.kind else ''
                if k == 'PARM_DECL' and child.spelling:
                    type_spelling = child.type.spelling if child.type else ''
                    if '*' in type_spelling:
                        # Parameter pointer — add as var node
                        try:
                            param_node_id = add_node_fn(child, 'var')
                            param_ptrs[child.spelling] = param_node_id
                        except Exception:
                            logging.getLogger(__name__).debug("silent exception", exc_info=True)
                            pass
                elif k == 'VAR_DECL' and child.spelling:
                    type_spelling = child.type.spelling if child.type else ''
                    if '*' not in type_spelling:
                        continue  # skip non-pointer vars
                    try:
                        var_node_id = add_node_fn(child, 'var')
                    except Exception:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        continue
                    init_kind, init_name = self._classify_initializer(child)
                    ptr_var_map[child.spelling] = (var_node_id, init_kind, init_name)

            # Second pass: collect assignments `p = q;` (BinaryOperator =)
            for child in func_cursor.walk_preorder():
                k = child.kind.name if child.kind else ''
                if k != 'BINARY_OPERATOR':
                    continue
                # Check if this is `=` assignment
                opcode = child.get_tokens() if hasattr(child, 'get_tokens') else None
                tokens_text = ' '.join(t.spelling for t in opcode) if opcode else ''
                if '=' not in tokens_text:
                    continue
                # Walk children to find LHS (DECL_REF_EXPR) and RHS
                children = list(child.get_children())
                if len(children) < 2:
                    continue
                lhs, rhs = children[0], children[1]
                lhs_name = lhs.spelling if hasattr(lhs, 'spelling') and lhs.spelling else ''
                if not lhs_name or lhs_name not in ptr_var_map:
                    continue
                rhs_kind_name = rhs.kind.name if rhs.kind else ''
                rhs_name = rhs.spelling if hasattr(rhs, 'spelling') and rhs.spelling else ''
                if rhs_kind_name == 'DECL_REF_EXPR' and rhs_name:
                    # Direct assignment: p = q
                    if rhs_name in ptr_var_map:
                        rhs_node_id, _, _ = ptr_var_map[rhs_name]
                        lhs_node_id, _, _ = ptr_var_map[lhs_name]
                        records.append(AliasSetRecord(
                            ptr1_node_id=lhs_node_id,
                            ptr2_node_id=rhs_node_id,
                            kind='must_alias',
                            confidence=0.9,
                        ))
                    elif rhs_name in param_ptrs:
                        # p = param (parameter pointer)
                        records.append(AliasSetRecord(
                            ptr1_node_id=ptr_var_map[lhs_name][0],
                            ptr2_node_id=param_ptrs[rhs_name],
                            kind='must_alias',
                            confidence=0.9,
                        ))
                elif rhs_kind_name == 'UNARY_OPERATOR':
                    # p = &x or p = *q
                    rhs_op_text = ' '.join(t.spelling for t in rhs.get_tokens()) if hasattr(rhs, 'get_tokens') else ''
                    if '&' in rhs_op_text:
                        # p = &x — find inner DeclRefExpr
                        for sub in rhs.get_children():
                            sub_name = sub.spelling if hasattr(sub, 'spelling') and sub.spelling else ''
                            if sub_name:
                                # Record source as 'addr_of:<name>'
                                ptr_var_map[lhs_name] = (ptr_var_map[lhs_name][0],
                                                          'addr_of', sub_name)

            # Third pass: emit alias sets from common sources
            # Group by source: var_name → list of (ptr_name, ptr_node_id)
            source_groups: Dict[str, List[Tuple[str, int]]] = {}
            for ptr_name, (node_id, src_kind, src_name) in ptr_var_map.items():
                if src_kind in ('var', 'addr_of') and src_name:
                    key = f'{src_kind}:{src_name}'
                    source_groups.setdefault(key, []).append((ptr_name, node_id))
            # Emit must_alias for pointers sharing a common source
            for key, group in source_groups.items():
                if len(group) < 2:
                    continue
                # All pairs in the group must_alias
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        records.append(AliasSetRecord(
                            ptr1_node_id=group[i][1],
                            ptr2_node_id=group[j][1],
                            kind='must_alias',
                            confidence=0.85,
                        ))
            # Emit may_alias for offset assignments
            for ptr_name, (node_id, src_kind, src_name) in ptr_var_map.items():
                if src_kind == 'offset' and src_name and src_name in ptr_var_map:
                    records.append(AliasSetRecord(
                        ptr1_node_id=node_id,
                        ptr2_node_id=ptr_var_map[src_name][0],
                        kind='may_alias',
                        confidence=0.5,
                    ))
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        return records

    def _classify_initializer(self, var_decl) -> Tuple[str, str]:
        """Classify a VAR_DECL's initializer into (kind, source_name).

        Kinds:
          - 'var':      `T *p = q;` (RHS is a DeclRefExpr)
          - 'addr_of':  `T *p = &x;` (RHS is UnaryOperator &)
          - 'offset':   `T *p = q + N;` or `T *p = q++;` (RHS involves arithmetic)
          - 'unknown':  no initializer or unrecognized
        """
        try:
            children = list(var_decl.get_children())
            # The initializer is the last child after the type/declarator
            init = None
            for c in reversed(children):
                k = c.kind.name if c.kind else ''
                if k not in ('TYPE_REF', 'PARM_DECL', 'INTEGER_LITERAL',
                             'ANNOT_ATTR', 'ALIGNOF_EXPR'):
                    init = c
                    break
            if init is None:
                return ('unknown', '')
            ik = init.kind.name if init.kind else ''
            if ik == 'DECL_REF_EXPR':
                name = init.spelling if hasattr(init, 'spelling') and init.spelling else ''
                return ('var', name)
            if ik == 'UNARY_OPERATOR':
                op_text = ' '.join(t.spelling for t in init.get_tokens()) if hasattr(init, 'get_tokens') else ''
                if '&' in op_text:
                    for sub in init.get_children():
                        sub_name = sub.spelling if hasattr(sub, 'spelling') and sub.spelling else ''
                        if sub_name:
                            return ('addr_of', sub_name)
            if ik == 'BINARY_OPERATOR':
                # p = q + 1 → offset
                for sub in init.get_children():
                    if sub.kind and sub.kind.name == 'DECL_REF_EXPR':
                        sub_name = sub.spelling if hasattr(sub, 'spelling') and sub.spelling else ''
                        if sub_name:
                            return ('offset', sub_name)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        return ('unknown', '')
