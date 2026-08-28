"""DualBackendScanner — runs both tree-sitter and clang, merges results.

Per cgdb-architecture-and-poc-report.md Phase 1:
- Tree-sitter (CTreeSitterScanner) provides canonical functions/edges/
  vtable_registrations/fn_ptr_calls/macro_registrations — backward compat
  with the legacy Code2Database build pipeline.
- Clang (ClangScanner) provides cgdb_nodes/cgdb_types/cgdb_edges/cgdb_invoke_sites
  for the new 13-layer cgdb tables — multi-kind nodes, USR-stable IDs,
  clang type system.

If clang fails (libclang missing, parse error), fall back to tree-sitter-only
with a warning — the build still produces a valid (legacy-only) graph.

If tree-sitter fails, fall back to clang-only (rare) — produces cgdb-only
output with empty legacy functions/edges.

Coexists with the existing get_scanner() factory in code2database_scanner.py,
which now returns DualBackendScanner when extraction_backend='auto' or 'clang'
and lang is c/cpp.
"""

from _scanner.base import BaseScanner


class DualBackendScanner(BaseScanner):
    """Wraps tree-sitter + clang scanners; merges output into one result dict.

    The tree-sitter scanner is the primary source for legacy shape
    (functions/edges/...). The clang scanner is the primary source for
    cgdb_* lists. Both write into the same result dict on scan_file().
    """

    def __init__(self, ts_scanner: BaseScanner, clang_scanner: BaseScanner):
        self.ts_scanner = ts_scanner
        self.clang_scanner = clang_scanner
        # Inherit shared attributes from the tree-sitter scanner so the
        # existing factory configuration (api_prefixes, callback_patterns,
        # struct_op_types, macro_dispatch_patterns, etc.) flows through.
        for attr in ('_api_prefixes', '_export_macros', '_public_header_paths',
                     '_non_api_paths', '_callback_patterns', '_struct_op_types',
                     '_macro_dispatch_patterns', '_fn_ptr_call_require_evidence',
                     '_CALLBACK_SUFFIXES'):
            if hasattr(ts_scanner, attr):
                setattr(self, attr, getattr(ts_scanner, attr))
        # Propagate configuration to the clang scanner too
        for attr in ('_api_prefixes', '_export_macros', '_callback_patterns',
                     '_struct_op_types', '_macro_dispatch_patterns',
                     '_non_api_paths', '_public_header_paths'):
            if hasattr(clang_scanner, attr) and hasattr(self, attr):
                setattr(clang_scanner, attr, getattr(self, attr))

    def _parse(self, source_bytes: bytes):
        """Delegate to tree-sitter scanner's _parse (used by BaseScanner.scan_file
        if anyone calls it directly — but our scan_file overrides)."""
        return self.ts_scanner._parse(source_bytes)

    def _extract(self, tree, source_bytes: bytes, filepath: str,
                 source_root: str, domain: str):
        """Delegate to tree-sitter scanner's _extract."""
        return self.ts_scanner._extract(tree, source_bytes, filepath, source_root, domain)

    def scan_file(self, filepath: str, source_root: str,
                  macro_bindings: dict = None) -> dict:
        """Run both scanners, merge results. Tree-sitter is canonical for legacy;
        clang is canonical for cgdb_*. Falls back gracefully."""
        # Tree-sitter pass (canonical for functions/edges/legacy)
        ts_result = self.ts_scanner.scan_file(filepath, source_root, macro_bindings)
        # Clang pass (canonical for cgdb_*)
        clang_result = self.clang_scanner.scan_file(filepath, source_root, macro_bindings)

        # Merge: start from tree-sitter, add cgdb_* from clang.
        # If tree-sitter failed entirely, use clang_result as the base.
        if ts_result.get('error') and not clang_result.get('error'):
            base = clang_result
        else:
            base = ts_result

        # Carry over cgdb_* lists from clang when clang produced output;
        # otherwise keep the tree-sitter scanner's heuristic cgdb_* lists
        # (sync_primitives, includes, conditions, data_flow, etc.). Clang
        # is canonical when available, but tree-sitter provides useful
        # fallbacks when libclang is missing.
        clang_has_cgdb = bool(clang_result.get('cgdb_nodes'))
        for cgdb_key in ('cgdb_nodes', 'cgdb_types', 'cgdb_edges',
                         'cgdb_invoke_sites', 'cgdb_predicates',
                         'cgdb_ops_bindings', 'cgdb_basic_blocks',
                         'cgdb_cfg_edges', 'cgdb_data_flow',
                         'cgdb_sync_primitives', 'cgdb_happens_before',
                         'cgdb_alias_sets', 'cgdb_doc_comments',
                         'cgdb_metadata', 'cgdb_includes',
                         'cgdb_conditions'):
            clang_list = clang_result.get(cgdb_key, [])
            if clang_has_cgdb and clang_list:
                base[cgdb_key] = clang_list
            elif cgdb_key not in base:
                base[cgdb_key] = clang_list
            # else: keep base (tree-sitter) value
        if clang_has_cgdb:
            base['conditions'] = clang_result.get('conditions', [])
        elif 'conditions' not in base:
            base['conditions'] = []

        # Record warnings from either scanner
        warnings = []
        if ts_result.get('error'):
            warnings.append(f"tree-sitter: {ts_result['error']}")
        if clang_result.get('error'):
            warnings.append(f"clang: {clang_result['error']}")
        if warnings:
            base['cgdb_warnings'] = warnings
        return base
