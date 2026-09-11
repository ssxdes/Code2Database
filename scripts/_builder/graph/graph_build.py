"""callgraph builder module: graph_build."""

import logging
import os
import json
import sys
import sqlite3
from _builder.graph.streaming_graph import StreamingGraph
import re
import time
from pathlib import Path
from collections import defaultdict, Counter
import networkx as nx
from _detector.build_detector import BuildDetector
from _detector.community_detector import detect_communities, CommunityResult
from _builder.utils import _resolve_invoked_id
import _builder.utils as _utils

# ---------------------------------------------------------------------------
# Module-level compiled regexes — avoid recompiling on every call to
# _extract_state_access (called 1.5M+ times on kernel-scale projects).
# ---------------------------------------------------------------------------

# Match: identifier->identifier or identifier.identifier
# Exclude: function calls (identifier(...)), common C keywords
_FIELD_ACCESS_RE = re.compile(
    r'(\b[A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*)\b'
)

# Write pattern: the field access is followed by assignment operator.
# Capture the assigned RHS expression (truncated to ~60 chars) so callers
# can distinguish "bh->b_bdev = NULL" from "bh->b_bdev = bdev" — this is
# critical for null-pointer-deref analysis where only the NULL writers
# are suspects.
_FIELD_WRITE_RE = re.compile(
    r'(\b[A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*)\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*([^;,\n]{1,80})'
)

# Static regex: matches callback/function-pointer parameter names.
# Used in build_graph's edge processing loop (5.4M edges on kernel).
# Was recompiled per-edge before this fix — ~27 seconds wasted on kernel.
_FN_PTR_PARAM_RE = re.compile(
    r'^(cb_fn|cpl_cb|cb|fn|func|handler|callback|op|action|proc|'
    r'routine|build_io_fn|disconnected_qpair_cb)$|'
    r'_cb$|_fn$|_handler$|_callback$|_routine$'
)
from _builder.graph.sqlite_postprocess import (
    _build_indexes_from_sqlite,
    _build_callgraph_summary_md_from_sqlite,
    _build_domain_readmes_from_sqlite,
    _build_scenarios_file_from_sqlite,
    _build_architecture_flows_from_sqlite,
    _build_context_pack_from_sqlite,
    _validate_stats_consistency_sqlite,
)


_CGDB_WIPE_TABLES = (
    "cgdb_nodes",
    "cgdb_edges",
    "cgdb_types",
    "cgdb_files",
    "cgdb_includes",
    "invoke_sites",
    # NOTE: "predicates" was listed here since the init commit but no such
    # table has ever existed in the schema (the real table is
    # config_predicates, listed below) — every build paid a spurious
    # "no such table: predicates" warning/error for it.
    "ops_bindings",
    "basic_blocks",
    "cfg_edges",
    "data_flow",
    "sync_primitives",
    "happens_before",
    "alias_sets",
    "doc_comments",
    "conditions",
    "config_predicates",
    "node_metadata",
    "edge_metadata",
    # L1 tables (tokens, literals, string_literals, comments_freeform,
    # macros, macro_invocations, pp_directives, pragmas, attributes,
    # pp_branches, source_files_meta) are re-populated by the L1 ingest
    # phase of the same build. Wiping them here keeps full rebuilds
    # complete — previously rows of files REMOVED from the project
    # lingered forever. comments_fts is trigger-maintained over
    # comments_freeform/doc_comments and follows their DELETEs.
    "tokens",
    "literals",
    "string_literals",
    "comments_freeform",
    "macros",
    "macro_invocations",
    "pp_directives",
    "pragmas",
    "attributes",
    "pp_branches",
    "source_files_meta",
)



from _builder.graph.domain_split import split_by_domain, _build_domain_readmes, _domain_subdir

from _builder.graph.extraction_io import _load_extraction_chunked, _read_file_bytes, _load_json_file, _load_split_extraction

from _builder.graph.graph_loader import _load_full_graph, _load_full_graph_from_sqlite, _load_node_from_sqlite, _load_neighbors_from_sqlite, _disambiguate_struct_chain

from _builder.graph.state_access import _extract_module_hint, _trace_object_origin, _find_enclosing_guard, _extract_state_access, _extract_state_access_all, _proc_state_access, _pre_strip_worker_init, _proc_pre_strip_state_access

from _builder.graph.thread_models import _detect_thread_models, _propagate_thread_models, _validate_stats_consistency, _compile_dispatch_patterns

# Concurrency values that mark registration / scheduling / structural
# semantics rather than an invocation (see _edge_is_call).
_NON_CALL_CONCURRENCY = frozenset({
    "callback", "CALLBACK_ARG", "callback_register", "async_spawn",
    "thread_spawn", "spawn_target", "poller", "interrupt",
    "contains", "imports",
})

def _wipe_cgdb_data(conn) -> None:
    """Delete all rows from cgdb tables except graph_versions (preserved for
    time-travel). Called at the start of each build so rebuilds don't
    accumulate duplicates. Safe to call on a fresh DB — missing tables are
    skipped silently.
    """
    # Commit any pending transaction so write_batch can BEGIN cleanly.
    try:
        conn.commit()
    except Exception:
        logging.getLogger(__name__).warning(
            "_wipe_cgdb_data: pre-wipe commit failed", exc_info=True)
    for tbl in _CGDB_WIPE_TABLES:
        try:
            # Skip tables that don't exist yet (fresh or incomplete DB).
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,)
            ).fetchone()
            if not exists:
                logging.getLogger(__name__).info(
                    "_wipe_cgdb_data: table %s not found — skipping", tbl)
                continue
            conn.execute(f"DELETE FROM {tbl}")
        except Exception:
            logging.getLogger(__name__).warning(
                "_wipe_cgdb_data: DELETE FROM %s failed", tbl, exc_info=True)
    try:
        conn.commit()
    except Exception:
        logging.getLogger(__name__).warning(
            "_wipe_cgdb_data: post-wipe commit failed", exc_info=True)
        pass
# Marker file recording that a build's cgdb export failed — the graph
# is then missing (parts of) its semantic layer (L1 tokens, types,
# vtables, invoke_sites, ...) while the legacy tables look fine.
_CGDB_EXPORT_FAILED_MARKER = ".code2database_cgdb_export_failed.json"



def _mark_cgdb_export_failed(outdir, error, context=None):
    """Persist the cgdb-export-failure marker in outdir.

    The cgdb export failure used to be logged as a mere WARNING on
    stderr: the build reported success while cgdb semantic tables were
    silently missing/partial (e.g. pool workers killed by the OOM
    killer). The marker makes the degraded state durable and
    machine-detectable; the build summary also surfaces it on stdout.
    Returns the marker path.
    """
    import time as _time
    marker = os.path.join(outdir, _CGDB_EXPORT_FAILED_MARKER)
    payload = {
        "error": str(error),
        "failed_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "failed_at_ts": _time.time(),
    }
    if context:
        payload.update(context)
    tmp = marker + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, marker)
    return marker



def _clear_cgdb_export_failed(outdir):
    """Remove a stale failure marker left by a previous build.

    Called when this build's cgdb export completed, so the marker only
    ever reflects the LATEST build. Missing marker is a no-op.
    """
    marker = os.path.join(outdir, _CGDB_EXPORT_FAILED_MARKER)
    try:
        os.unlink(marker)
    except FileNotFoundError:
        pass



def _detect_commit_hash(source_root: str) -> str:
    """Detect the current commit hash of a source root.

    Tries git first (`git rev-parse HEAD`), then svn (`svn info`), then
    falls back to a synthetic hash based on the build timestamp. Returns
    an empty string if source_root is empty.
    """
    import subprocess
    if not source_root or not os.path.isdir(source_root):
        return ''
    # Try git
    try:
        result = subprocess.run(
            ['git', '-C', source_root, 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    try:
        result = subprocess.run(
            ['svn', 'info', '--show-item', 'revision', source_root],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return f'svn:r{result.stdout.strip()}'
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    import time
    return f'build:{int(time.time())}'



def _detect_commit_subject(source_root: str) -> str:
    """Detect the commit subject (first line of commit message) for the
    current HEAD. Returns empty string if not available.
    """
    import subprocess
    if not source_root or not os.path.isdir(source_root):
        return ''
    try:
        result = subprocess.run(
            ['git', '-C', source_root, 'log', '-1', '--format=%s', 'HEAD'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    return ''



def _is_test_domain(domain: str, profile: dict = None) -> bool:
    """Check if a domain is a test/unit domain (not production code).

    Uses profile.project_boundaries.test_domain_segments when available
    (project-agnostic: the tool default covers generic cross-project
    conventions; built-in reference profiles may add project-specific
    segments). Falls back to generic defaults when profile is None.
    """
    if not domain:
        return False
    # Profile-driven test domain segments (e.g., ["ut", "unit", "test", "fuzz"])
    pb = (profile or {}).get("project_boundaries", {}) if isinstance(profile, dict) else {}
    segments = pb.get("test_domain_segments")
    if not segments:
        # Generic cross-project defaults (kept here for the profile=None path;
        # _DEFAULT_PROFILE also declares these)
        segments = ("ut", "ut_mock", "unit", "test", "fuzz")
    # A domain is "test" if any of its dot-separated segments matches a test indicator
    parts = domain.split(".")
    for seg in parts:
        if seg in segments:
            return True
    return False



def _is_external_domain(domain: str, profile: dict = None) -> bool:
    """Check if a domain is an external/third-party code domain.

    Detects vendor-specific directories and common third-party directory names
    that should not be treated as first-class project domains.

    Profile-driven (project-agnostic):
    - profile.project_boundaries.vendor_domain_prefixes: project-specific vendor
      prefixes (e.g., ["huawei"]). EMPTY by default — auto-profile detects.
    - profile.project_boundaries.external_dir_prefixes: generic external dir
      names (vendor, third_party, etc.).

    Patterns detected:
    - Vendor-specific prefixes from profile (e.g., 'huawei.*')
    - Common external directories from profile (vendor/, third_party/, etc.)
    - Domains from profile's external_lib_prefixes categories (prefixed with 'external_')
    """
    if not domain:
        return False
    pb = (profile or {}).get("project_boundaries", {}) if isinstance(profile, dict) else {}
    # Vendor-specific directory patterns from profile (e.g., huawei.*, huawei)
    for vendor_prefix in pb.get("vendor_domain_prefixes", []):
        if domain == vendor_prefix or domain.startswith(vendor_prefix + "."):
            return True
    # Common external directory patterns from profile
    external_dir_prefixes = pb.get("external_dir_prefixes")
    if not external_dir_prefixes:
        # Generic cross-project defaults (profile=None path)
        external_dir_prefixes = ("vendor", "third_party", "thirdparty",
                                 "external", "3rdparty", "contrib")
    for prefix in external_dir_prefixes:
        if domain == prefix or domain.startswith(prefix + "."):
            return True
    # Domains starting with 'external_' come from profile external_lib_prefixes
    # classification and are always external
    if domain.startswith("external_"):
        return True
    if domain == "external":
        return True
    return False



def _is_test_source(source_file: str, profile: dict = None) -> bool:
    """Check if a source file is a test/unit file (not production code).

    Uses profile.project_boundaries.test_path_patterns and test_file_suffixes
    when available. Falls back to generic cross-project defaults when
    profile is None.
    """
    if not source_file:
        return False
    lower = source_file.lower()
    pb = (profile or {}).get("project_boundaries", {}) if isinstance(profile, dict) else {}
    # Test directory patterns from profile (or generic defaults)
    patterns = pb.get("test_path_patterns")
    if not patterns:
        patterns = ("/test/", "/tests/", "/unit/", "/ut/", "/unittest/",
                    "/fuzz/", "\\test\\", "\\tests\\", "\\unit\\")
    for pattern in patterns:
        if pattern in lower:
            return True
    # Test file suffixes from profile (or generic defaults)
    suffixes = pb.get("test_file_suffixes")
    if not suffixes:
        suffixes = ("_test.c", "_test.cpp", "_ut.c", "_ut.cpp",
                    "_unittest.c", "_unittest.cpp", "test_.c", "test_.cpp")
    for suffix in suffixes:
        if lower.endswith(suffix):
            return True
    return False



def _domain_hier_match(caller_dom: str, target_dom: str) -> bool:
    """Check if caller domain hierarchically matches target domain.

    Matches when:
    - Exact match: 'nvmf' == 'nvmf'
    - Parent→child: 'bdev' matches 'bdev.lvol'
    - Child→parent: 'bdev.lvol' matches 'bdev'
    - Shared last component: 'lvol' matches 'bdev.lvol' (min 3 chars)
    """
    if caller_dom == target_dom:
        return True
    if target_dom.startswith(caller_dom + "."):
        return True
    if caller_dom.startswith(target_dom + "."):
        return True
    caller_last = caller_dom.rsplit(".", 1)[-1]
    target_last = target_dom.rsplit(".", 1)[-1]
    if caller_last == target_last and len(caller_last) >= 3:
        return True
    return False


def _edge_is_call(edata: dict) -> bool:
    """True when an in-memory build-graph edge represents an invocation.

    Used by dispatch flattening to decide which predecessors of a node
    are callers. Call edges carry a concurrency value (direct_call,
    fn_ptr, vtable_dispatch, ...) and no relation attribute; structural
    and annotation edges carry a relation (CONTAINS, IMPORTS, DISPATCH)
    or a registration-style concurrency (callback, thread_spawn, ...).
    On persist, relation-less edges default to relation='INVOKES'
    (sqlite_store.store_edges), so a graph reloaded from JSON with
    explicit relation='INVOKES' also counts.
    """
    rel = edata.get("relation")
    if rel:
        return rel == "INVOKES"
    conc = edata.get("concurrency") or ""
    if conc in _NON_CALL_CONCURRENCY:
        return False
    return True


def _detect_build_system(source_root: str, build_config_arg: str,
                         macros_arg: str) -> tuple:
    """Detect build system and resolve macro bindings.

    Returns (build_info_dict, macro_bindings_dict).
    build_info_dict is suitable for inclusion in context_pack and summary.
    macro_bindings_dict is passed to scanners.
    """
    macro_bindings = {}

    # Parse user-supplied macros first
    if macros_arg:
        for token in macros_arg.split():
            token = token.strip()
            if token.startswith('-D'):
                token = token[2:]
            if not token:
                continue
            if '=' in token:
                k, v = token.split('=', 1)
                macro_bindings[k] = v
            else:
                macro_bindings[token] = ""

    # Auto-detect build system if requested
    build_info = None
    if build_config_arg and build_config_arg != "none":
        detector = BuildDetector()
        info = detector.detect(source_root)

        if build_config_arg == "auto" or build_config_arg is None:
            # Auto mode: use detected macros if any, prompt if ambiguous
            if info.build_system:
                print(f"Detected build system: {info.build_system} "
                      f"({', '.join(os.path.basename(f) for f in info.config_files)})")
                if info.macros:
                    # If build_types available, select Release by default
                    if info.build_types:
                        config_name = "Release" if "Release" in info.build_types else list(info.build_types.keys())[0]
                        selected = dict(info.build_types[config_name])
                        for k, v in info.macros.items():
                            if k.startswith(('HAVE_', 'WITH_', 'ENABLE_')) and k not in selected:
                                selected[k] = v
                        selected["_selected_config"] = config_name
                        # Merge with user macros (user overrides)
                        for k, v in macro_bindings.items():
                            selected[k] = v
                        macro_bindings = selected
                    else:
                        # No build types — use detected macros
                        for k, v in info.macros.items():
                            if k not in macro_bindings:
                                macro_bindings[k] = v
                elif not macro_bindings:
                    print("No macros found in build system. Parsing all #ifdef branches.")
            else:
                print("No build system detected. Parsing all #ifdef branches.")
        else:
            # Specific config name (e.g., "Release", "Debug")
            if info.build_types and build_config_arg in info.build_types:
                selected = dict(info.build_types[build_config_arg])
                for k, v in info.macros.items():
                    if k.startswith(('HAVE_', 'WITH_', 'ENABLE_')) and k not in selected:
                        selected[k] = v
                selected["_selected_config"] = build_config_arg
                for k, v in macro_bindings.items():
                    selected[k] = v
                macro_bindings = selected
            else:
                # Treat as build config name not found — just use detected macros
                if info.macros:
                    for k, v in info.macros.items():
                        if k not in macro_bindings:
                            macro_bindings[k] = v

        # Build the build_info dict for output files
        build_info = {
            "build_system": info.build_system,
            "config_files": [os.path.relpath(f, source_root) for f in info.config_files],
            "defined_macros": {k: v for k, v in macro_bindings.items()
                              if not k.startswith('_')},
            "targets": info.targets[:20],  # Limit to first 20
            "include_dirs": info.include_dirs[:20],
        }
        if "_selected_config" in macro_bindings:
            build_info["selected_config"] = macro_bindings["_selected_config"]

    # Clean internal keys from macro_bindings for scanner use
    scanner_bindings = {k: v for k, v in macro_bindings.items()
                        if not k.startswith('_')}

    # Count dead-code functions that will be affected
    if scanner_bindings:
        print(f"Macro bindings: {len(scanner_bindings)} macro(s) defined")

    return build_info, scanner_bindings







def build_graph(extraction: dict, profile: dict = None,
                graph=None) -> nx.DiGraph:
    """Build a networkx DiGraph from extraction data.

    Handles:
    - Function nodes with labels, domain, and FQN
    - Empty nodes for conditional grouping
    - Edges with call_order and call_condition
    - Cross-domain edges

    Args:
        extraction: Extraction data dict from scanner.
        profile: Builder config dict from ProfileSchema.to_builder_config().
                 When provided, uses profile's public_prefixes to protect
                 project API functions from noise removal.
        graph: Optional pre-created graph object. If None, creates nx.DiGraph().
               Can be a StreamingGraph instance for --storage sqlite builds.
    """
    from _builder.build.import_resolve import _compute_fqn

    # Callback field regex — used in both FN_PTR field_dispatch and
    # passthrough_bridged field_assignments fallback paths to skip
    # callback fields (cb_fn, cb_func, etc.) that are handled by
    # caller_bridged and param_bridged resolution.
    _CB_FIELD_RE = re.compile(
        r'^(cb_fn|cb_func|cb|callback|completion_cb|done_cb|cpl_cb)$'
        r'|(?:_cb|_fn|_func|_cb_fn|_cb_func|cb_fn|cb_func)$')

    # Wire external lib prefixes into callee resolution so that calls to
    # profiled external lib prefixes create ext.* endpoint nodes rather than
    # being silently dropped. This must happen before any _resolve_invoked_id
    # calls. When building via CLI, this is done in main(); when building via
    # API, we do it here.
    if profile:
        from _builder.utils import set_external_lib_prefixes
        # Profile comes from to_builder_config(), which uses flat keys:
        #   lib_prefix_map: {prefix: category, ...}
        #   skip_names_add: [...]
        #   visible_external_prefixes / silent_skip_prefixes come from to_scanner_config()
        lib_prefix_map = profile.get("lib_prefix_map", {})
        all_ext_prefixes = [k.lower() for k in lib_prefix_map]
        if all_ext_prefixes:
            set_external_lib_prefixes(all_ext_prefixes)

    # Project-specific prefixes from profile (e.g., 'proj_', 'rpc_', 'api_').
    # Functions starting with these prefixes are kept even if they look like
    # C++ noise (no body, underscore-prefixed), because they are project API.
    _keep_prefixes = tuple(profile.get("public_prefixes", [])) if profile else ()

    # Dispatch tuning from profile (O5/O6/O7/O8): allows projects to override
    # the inline-wrapper regex, macro-bridge patterns, vtable dispatch cap,
    # and fn_ptr_call heuristic. Defaults match the pre-tuning behavior so
    # existing profiles continue to work.
    _dispatch_tuning = (profile or {}).get("dispatch_tuning", {}) or {}
    _MAX_VTABLE_DISPATCH_PER_CALL = int(_dispatch_tuning.get(
        "max_vtable_dispatch_per_call", 50))
    _MAX_VTABLE_DISPATCH_PER_FIELD = dict(_dispatch_tuning.get(
        "max_vtable_dispatch_per_field", {}) or {})
    _inline_wrapper_patterns_cfg = list(_dispatch_tuning.get(
        "inline_wrapper_patterns", [r"^(?:__)?(?:call|invoke)_(\w+)$"]) or [])
    _macro_bridge_patterns_cfg = list(_dispatch_tuning.get(
        "macro_bridge_patterns", [{"pattern": r"^(\w+)$", "impl": "__{1}"}]) or [])
    _macro_bridge_require_same_domain = bool(_dispatch_tuning.get(
        "macro_bridge_require_same_domain", True))
    _fn_ptr_call_require_evidence = bool(_dispatch_tuning.get(
        "fn_ptr_call_require_evidence", False))

    # Pre-compile inline wrapper patterns (O5) and macro bridge patterns (O6).
    # Extraction into a named helper so the compilation logic is testable
    # without driving a full build_graph() invocation.
    _inline_wrapper_regexes, _macro_bridge_compiled = _compile_dispatch_patterns(
        _inline_wrapper_patterns_cfg, _macro_bridge_patterns_cfg)

    def _effective_vtable_cap(field_name: str) -> int:
        """Return the dispatch cap for a given vtable field.

        Per-field overrides in profile.dispatch_tuning.max_vtable_dispatch_per_field
        take precedence over the global cap. Field name match is case-insensitive.
        """
        if not field_name:
            return _MAX_VTABLE_DISPATCH_PER_CALL
        # Try exact, then case-insensitive.
        cap = _MAX_VTABLE_DISPATCH_PER_FIELD.get(field_name)
        if cap is not None:
            return int(cap)
        for k, v in _MAX_VTABLE_DISPATCH_PER_FIELD.items():
            if k.lower() == field_name.lower():
                return int(v)
        return _MAX_VTABLE_DISPATCH_PER_CALL

    G = graph if graph is not None else nx.DiGraph()
    functions = extraction.get("functions", [])
    raw_edges = extraction.get("edges", [])
    _build_start = time.time()
    print(f"[build] Building graph... ({len(functions)} nodes, {len(raw_edges)} edges)",
          file=sys.stderr)

    # Phase 1: derive project name from extraction or first source file
    from _builder.build.build_phases import (
        _derive_project_name, _build_id_registry, _filter_noise_nodes,
        _enable_streaming_deferred,
    )
    project_name = _derive_project_name(extraction, functions)

    # Phase 2: build id_registry and add function nodes to the graph
    id_registry = _build_id_registry(functions, G, project_name)

    # Build suffix index for O(1) callee resolution (vs O(N) scan)
    from _builder.utils import _build_suffix_index
    from _builder.build.import_resolve import _multi_strategy_resolve, _build_resolve_lookups
    suffix_index = _build_suffix_index(id_registry)

    # Phase 3: filter noise nodes (param-name-only + C++ artifacts)
    _filter_noise_nodes(G, id_registry, _keep_prefixes)
    # Rebuild suffix index after node removal (filter may have popped entries)
    suffix_index = _build_suffix_index(id_registry)

    # Pre-build lookup structures for multi-strategy resolve (avoid O(N) rebuild per edge)
    # Always build lookups — _build_resolve_lookups() is O(N) once, then each
    # _multi_strategy_resolve() call is O(1) amortized via pre-built indices.
    _ms_lookups = _build_resolve_lookups(G)

    # Phase 4: enable deferred mode for StreamingGraph during edge processing
    _using_streaming = _enable_streaming_deferred(G)

    # Phase 5: process edges — create empty conditional placeholder nodes
    # and build the target→first-edge index used by downstream condition lookup
    from _builder.build.build_phases import _create_empty_conditional_nodes
    _create_empty_conditional_nodes(G, raw_edges, id_registry)

    # Phase 6: build vtable field-name set + struct_type→{field→[func]} index
    from _builder.build.build_phases import (
        _build_vtable_field_index, _build_name_domain_index,
        _process_asm_aliases, _label_export_symbol_functions,
        _label_struct_op_types, _emit_ops_bind_edges,
        _build_field_dispatch_map,
    )
    _vtable_field_names, _vtable_type_fields = _build_vtable_field_index(
        extraction, profile)

    # Phase 7: build name→domain + name→nid lookup indexes
    _name_to_domain, _name_to_nid = _build_name_domain_index(G, extraction)

    # Phase 8: process ASM aliases (SYM_FUNC_ALIAS alias→original edges)
    _asm_aliases = extraction.get("asm_aliases", [])
    _asm_alias_map = _process_asm_aliases(extraction, G, _name_to_nid)

    # Phase 9: label EXPORT_SYMBOL functions as API_entry
    # (project_boundaries.non_api_paths filters out tools/scripts/selftests)
    _NON_API_PATHS = tuple(
        (profile or {}).get("project_boundaries", {}).get("non_api_paths", [])
        if isinstance(profile, dict) else []
    )
    _label_export_symbol_functions(
        G, extraction, _name_to_nid, _NON_API_PATHS)

    # Phase 10: label functions registered in struct_op_types (VFS ops) as API_entry
    _label_struct_op_types(
        G, extraction, profile, _name_to_nid, _NON_API_PATHS)

    # Phase 11: emit explicit OPS_BIND edges for vtable registrations
    _emit_ops_bind_edges(G, extraction, profile, _name_to_nid)

    # Phase 12: build field dispatch map (4 indexes + param-bridged FA dict)
    (_field_dispatch_map, _field_dispatch_flat,
     _field_dispatch_by_domain, _field_dispatch_by_target_domain,
     _param_bridged_fa) = _build_field_dispatch_map(
        extraction, _name_to_domain)

    # Phase 13: build caller→struct_chain lookup from fn_ptr_calls
    from _builder.build.build_phases import (
        _build_fn_ptr_struct_lookup, _build_struct_embedding_index,
        _identify_polymorphic_callback_fields,
    )
    _fn_ptr_struct_lookup = _build_fn_ptr_struct_lookup(extraction)

    # Phase 14: build struct embedding index (inner_type → embedding entries)
    _embedding_index = _build_struct_embedding_index(
        extraction, profile)

    # Phase 15: identify polymorphic callback fields (cb_fn, cb_func, etc.)
    _polymorphic_fields = _identify_polymorphic_callback_fields(
        _field_dispatch_map, _param_bridged_fa)

    # Pre-compute an index of field_assignments by field_name (and a
    # separate index by (struct_chain, field_name)) so that the per-edge
    # loops below don't do an O(FAs) linear scan for every edge.
    _fa_by_field_name: dict = {}
    _fa_by_sc_field: dict = {}
    for _fa in extraction.get("field_assignments", []):
        _fa_field = _fa.get("field_name", "")
        _fa_chain = _fa.get("struct_chain", "")
        _fa_target = _fa.get("target_func", "")
        if not _fa_field:
            continue
        _fa_by_field_name.setdefault(_fa_field, []).append(_fa)
        if _fa_chain:
            _fa_by_sc_field.setdefault((_fa_chain, _fa_field), []).append(_fa)

    # Add edges with resolved IDs
    _edge_progress_milestone = 0
    _total_edges = len(raw_edges)
    # Generic struct_chain names that are used across many modules and cannot
    # disambiguate dispatch targets on their own. Hoisted outside the loop
    # to avoid recreating the frozenset on every FN_PTR edge.
    _GENERIC_STRUCT_CHAINS = frozenset({
        'ctx', 'req', 'base', 'op', 'args', 'data', 'entry',
        'obj', 'handle', 'ptr', 'buf', 'result', 'ret',
    })
    for _edge_idx, edge in enumerate(raw_edges):
        source_id = edge.get("source", "")
        target_name = edge.get("target", "")
        # Pre-compute source_func (used by FN_PTR dispatch) once per edge
        # instead of recomputing inside multiple FN_PTR branches.
        _source_func = source_id.rsplit(".", 1)[-1] if "." in source_id else source_id
        # Progress reporting every 500K edges
        _next_milestone = (_edge_progress_milestone + 1) * 500000
        if _edge_idx >= _next_milestone:
            _edge_progress_milestone += 1
            _elapsed = time.time() - _build_start
            _rate = _edge_idx / _elapsed if _elapsed > 0 else 0
            _eta = (_total_edges - _edge_idx) / _rate if _rate > 0 else 0
            print(f"[build] Processing edges: {_edge_idx}/{_total_edges} "
                  f"({_rate:.0f} edges/s, ETA {_eta:.0f}s)", file=sys.stderr)

        # Skip edges from nodes that were removed (C++ artifacts, param-only names)
        if source_id not in G:
            continue

        # FN_PTR edges represent function pointer calls (e.g., table->field(args)).
        # Three categories:
        #   1) Vtable dispatch: targets that are vtable field names handled by
        #      vtable_analysis phase → skip (INFERRED edges already created)
        #   2) Field dispatch: targets that match field_assignments → create
        #      INFERRED edges to each possible target function
        #   3) Non-vtable, non-field function pointer calls: targets are actual
        #      function names → process like EXTRACTED edges with FN_PTR confidence
        if edge.get("concurrency") == "fn_ptr":
            target_name = edge.get("target", "")
            # Check if this target matches a field in the dispatch map FIRST.
            # Field dispatch has concrete field→target assignments, which is more
            # specific than the vtable skip. A field like cb_fn may appear in
            # _vtable_field_names due to a false vtable registration, but if we
            # have concrete assignments we should use them.
            if target_name in _field_dispatch_map:
                # Context-aware dispatch: use struct_chain and domain to narrow targets.
                source_func = _source_func
                struct_chain = _fn_ptr_struct_lookup.get((source_func, target_name), "")
                struct_map = _field_dispatch_map[target_name]
                source_domain = "root"
                if source_id in id_registry:
                    source_domain = id_registry[source_id].get("domain", "root")

                # Determine dispatch targets with decreasing specificity:
                # 1. Precise struct_chain match (non-generic name)
                # 2. struct_chain + domain match (generic name, domain narrows)
                # 3. domain-scoped match (no struct_chain, but domain matches)
                # 4. single struct_chain (unambiguous)
                # 5. Skip — field is too generic for meaningful dispatch
                dispatch_targets = None
                is_precise = False
                evidence_ctx = ""
                is_polymorphic = target_name in _polymorphic_fields

                # Normalize struct_chain: treat -> and . as equivalent
                # (e.g., "req->payload" and "req.payload" map to same key)
                sc_norm = struct_chain.replace("->", ".") if struct_chain else ""

                # For compound struct_chains (e.g., "transport->ops"), extract
                # the last segment for suffix/chain-tail matching. This connects
                # fn_ptr_call "transport->ops.field()" to FA entries like
                # "pcie_ops", "tcp_ops" via suffix "_ops".
                sc_last_seg = ""
                if sc_norm and "." in sc_norm:
                    sc_last_seg = sc_norm.split(".")[-1]

                if struct_chain and (struct_chain in struct_map or sc_norm in struct_map):
                    # Use normalized key if original doesn't match
                    lookup_key = struct_chain if struct_chain in struct_map else sc_norm
                    if struct_chain not in _GENERIC_STRUCT_CHAINS:
                        # Specific struct name — trust the match, but also
                        # include chain-tail and suffix matches for compound
                        # struct_chains (e.g., "b->bs_dev" tail-matches "bs_dev")
                        dispatch_targets = set(struct_map[lookup_key])
                        match_type = "struct"
                        # Add chain-tail matches: compare last segments
                        # e.g., FA "b.bs_dev" last_seg matches fn_ptr "bs_dev"
                        for sc, targets in struct_map.items():
                            segments = sc.replace("->", ".").split(".")
                            if len(segments) >= 2:
                                if segments[-1] == struct_chain or segments[-1] == sc_last_seg:
                                    dispatch_targets.update(targets)
                                    match_type = "struct+chain_tail"
                        # Add suffix matches: "vbdev_gpt_fn_table" matches "fn_table"
                        # For compound chains, use last segment: "transport->ops" → "_ops"
                        suffix_key = "_" + (sc_last_seg if sc_last_seg else struct_chain)
                        for sc, targets in struct_map.items():
                            if sc.endswith(suffix_key):
                                dispatch_targets.update(targets)
                                match_type = "struct+suffix"
                        if len(dispatch_targets) <= 20:
                            is_precise = len(dispatch_targets) <= 5
                            evidence_ctx = f" ({match_type}={struct_chain}, {len(dispatch_targets)} targets)"
                        else:
                            # Too many targets — only use exact match
                            dispatch_targets = set(struct_map[lookup_key])
                            is_precise = True
                            evidence_ctx = f" (struct={struct_chain})"
                    elif (source_domain != "root"
                          and target_name in _field_dispatch_by_domain
                          and source_domain in _field_dispatch_by_domain[target_name]):
                        # Generic struct name — combine with domain for precision
                        domain_targets = _field_dispatch_by_domain[target_name][source_domain]
                        if len(domain_targets) <= 5:
                            dispatch_targets = domain_targets
                            is_precise = True
                            evidence_ctx = f" (struct={struct_chain}, domain={source_domain})"
                elif struct_chain and struct_chain not in _GENERIC_STRUCT_CHAINS and not is_polymorphic:
                    # Suffix match: fn_ptr_call's struct_chain is a suffix of
                    # field_assignment struct_chains. E.g., fn_ptr_call has
                    # struct_chain="fn_table" and field_assignments have
                    # "vbdev_gpt_fn_table", "malloc_fn_table", etc.
                    # For compound chains ("transport->ops"), use last segment ("_ops").
                    suffix_key = "_" + (sc_last_seg if sc_last_seg else struct_chain)
                    suffix_targets = set()
                    for sc, targets in struct_map.items():
                        if sc.endswith(suffix_key):
                            suffix_targets.update(targets)
                    # Also add chain-tail matches (normalize -> to .)
                    for sc, targets in struct_map.items():
                        segments = sc.replace("->", ".").split(".")
                        if len(segments) >= 2:
                            if segments[-1] == struct_chain or segments[-1] == sc_last_seg:
                                suffix_targets.update(targets)
                    # Apply domain proximity filter when suffix is broad
                    # (many targets across different domains). Generic suffixes
                    # like "_module", "_ops", "_if" match struct variables from
                    # unrelated subsystems — filter by shared domain components.
                    if suffix_targets and len(suffix_targets) > 5 and source_domain != "root":
                        proximal = set()
                        src_comps = set(source_domain.split("."))
                        for tgt in suffix_targets:
                            tgt_dom = _name_to_domain.get(tgt, "")
                            tgt_comps = set(tgt_dom.split(".")) if tgt_dom else set()
                            if src_comps & tgt_comps - {"root"}:
                                proximal.add(tgt)
                        if proximal:
                            suffix_targets = proximal
                    # Segment-suffix match: for compound chains where the full
                    # last segment doesn't match (e.g., "endpoint->virtio_ops"
                    # → "_virtio_ops" doesn't match "virtio_blk_ops"), try the
                    # last underscore-split part (e.g., "_ops" matches both
                    # "virtio_blk_ops" and "pcie_ops"). Requires domain filter
                    # to avoid false positives from overly broad matches.
                    if not suffix_targets and sc_last_seg and "_" in sc_last_seg:
                        seg_suffix = "_" + sc_last_seg.rsplit("_", 1)[-1]
                        if seg_suffix != suffix_key and len(seg_suffix) > 2:
                            seg_suffix_targets = set()
                            for sc, targets in struct_map.items():
                                if sc.endswith(seg_suffix):
                                    seg_suffix_targets.update(targets)
                            # Apply domain proximity filter for broad matches
                            if seg_suffix_targets and len(seg_suffix_targets) <= 20:
                                if source_domain != "root":
                                    proximal = set()
                                    src_comps = set(source_domain.split("."))
                                    for tgt in seg_suffix_targets:
                                        tgt_dom = _name_to_domain.get(tgt, "")
                                        tgt_comps = set(tgt_dom.split(".")) if tgt_dom else set()
                                        if src_comps & tgt_comps - {"root"}:
                                            proximal.add(tgt)
                                    if proximal:
                                        suffix_targets = proximal
                                else:
                                    suffix_targets = seg_suffix_targets
                    if suffix_targets and len(suffix_targets) <= 20:
                        dispatch_targets = suffix_targets
                        is_precise = len(suffix_targets) <= 5
                        evidence_ctx = f" (struct_suffix={struct_chain}, {len(suffix_targets)} targets)"
                    elif not suffix_targets:
                        # Prefix-chain match: fn_ptr_call's struct_chain is a
                        # prefix of field_assignment struct_chains. E.g.,
                        # fn_ptr_call has struct_chain="api_data" and FAs have
                        # "api_data->args.cb_info". The fn_ptr_call sees the
                        # top-level struct variable but the FA sees the full
                        # path to the callback field.
                        prefix_targets = set()
                        for sc, targets in struct_map.items():
                            sc_n = sc.replace("->", ".")
                            # Check if fn_ptr_call struct_chain is a prefix of
                            # FA chain (e.g., "api_data" prefix of "api_data.args.cb_info")
                            if sc_n.startswith(sc_norm + ".") or sc_n == sc_norm:
                                prefix_targets.update(targets)
                            # Also check if FA chain's first segment matches
                            # fn_ptr_call struct_chain (handles "api_data" matching
                            # "api_data->args.cb_info")
                            if not prefix_targets and sc_norm:
                                fa_first_seg = sc_n.split(".")[0]
                                if fa_first_seg == struct_chain:
                                    prefix_targets.update(targets)
                        if prefix_targets and len(prefix_targets) <= 10:
                            dispatch_targets = prefix_targets
                            is_precise = len(prefix_targets) <= 3
                            evidence_ctx = f" (struct_chain_prefix={struct_chain}, {len(prefix_targets)} targets)"
                elif (source_domain != "root"
                      and target_name in _field_dispatch_by_domain
                      and source_domain in _field_dispatch_by_domain[target_name]
                      and not is_polymorphic):
                    # No struct_chain match — use domain only
                    # Skip for polymorphic fields (e.g., cb_fn) — domain match
                    # would over-dispatch to unrelated callbacks in same domain
                    domain_targets = _field_dispatch_by_domain[target_name][source_domain]
                    if len(domain_targets) <= 5:
                        dispatch_targets = domain_targets
                        is_precise = True
                        evidence_ctx = f" (domain={source_domain})"
                elif len(struct_map) == 1 and not is_polymorphic:
                    dispatch_targets = next(iter(struct_map.values()))
                    is_precise = True
                    evidence_ctx = f" (single_struct)"

                # If no precise match found, try fallback strategies:
                # - Domain-scoped field match (all targets in caller's domain)
                # - Field-only match with small target set
                # Polymorphic fields (cb_fn, cb_func, etc.) with many different
                # struct_chains are excluded from domain_fallback and
                # target_domain_hier — these produce massive over-dispatch and
                # are better handled by CALLBACK_ARG edges which track actual
                # data flow.
                is_polymorphic = target_name in _polymorphic_fields
                if dispatch_targets is None:
                    # Fallback 1: domain-scoped match across ALL struct variables
                    # Skip for polymorphic fields — too imprecise
                    if (not is_polymorphic
                            and source_domain != "root"
                            and target_name in _field_dispatch_by_domain
                            and source_domain in _field_dispatch_by_domain[target_name]):
                        domain_targets = _field_dispatch_by_domain[target_name][source_domain]
                        if len(domain_targets) <= 10:
                            dispatch_targets = domain_targets
                            is_precise = True
                            evidence_ctx = f" (domain_fallback={source_domain})"
                    # Fallback 1.5: target-domain match — find targets whose
                    # own domain hierarchically matches the caller's domain.
                    # Skip for polymorphic fields — too imprecise
                    if (dispatch_targets is None and not is_polymorphic
                            and source_domain != "root"):
                        if target_name in _field_dispatch_by_target_domain:
                            td_map = _field_dispatch_by_target_domain[target_name]
                            hier_targets = set()
                            for td, targets in td_map.items():
                                if _domain_hier_match(source_domain, td):
                                    hier_targets.update(targets)
                            if hier_targets and len(hier_targets) <= 10:
                                dispatch_targets = hier_targets
                                is_precise = True
                                evidence_ctx = f" (target_domain_hier={source_domain})"
                    # Fallback 2: field-only match with small target set
                    # Require domain proximity: at least one domain component
                    # must be shared between source and target to avoid
                    # coincidental name matches across unrelated modules.
                    # Skip callback fields (cb_fn, cb_func, etc.) — these are
                    # handled by caller_bridged and param_bridged resolution
                    # which track actual data flow and are more precise than
                    # domain-proximity heuristics.
                    if dispatch_targets is None and not _CB_FIELD_RE.search(target_name):
                        all_targets = _field_dispatch_flat.get(target_name, set())
                        if 0 < len(all_targets) <= 5:
                            # Filter targets by domain proximity
                            proximal_targets = set()
                            source_components = set(source_domain.split(".")) if source_domain else set()
                            for tgt in all_targets:
                                tgt_domain = _name_to_domain.get(tgt, "")
                                tgt_components = set(tgt_domain.split(".")) if tgt_domain else set()
                                # Require at least one shared non-root component
                                if source_components and tgt_components:
                                    shared = source_components & tgt_components - {"root"}
                                    if shared:
                                        proximal_targets.add(tgt)
                                else:
                                    # No domain info — include but mark as low confidence
                                    proximal_targets.add(tgt)
                            if proximal_targets:
                                dispatch_targets = proximal_targets
                                is_precise = len(proximal_targets) == 1
                                evidence_ctx = f" (field_fallback, {len(proximal_targets)} targets)"
                if dispatch_targets is None:
                    # No dispatch targets found — still add the edge as unresolved
                    # fn_ptr_call so the indirect call is visible in the graph.
                    target_id = _resolve_invoked_id(target_name, source_domain,
                                                    id_registry, suffix_index=suffix_index)
                    if target_id and target_id in G and not G.has_edge(source_id, target_id):
                        G.add_edge(source_id, target_id,
                                   call_order=edge.get("call_order"),
                                   call_condition=edge.get("call_condition", ""),
                                   concurrency="fn_ptr",
                                   confidence="AMBIGUOUS",
                                   source_tag="fn_ptr_unresolved",
                                   confidence_score=0.3,
                                   evidence=f"unresolved fn_ptr_call: {target_name}")
                    elif not target_id or target_id not in G:
                        # Target doesn't exist in graph — create an auto node
                        # so the fn_ptr_call edge is preserved
                        auto_id = f"{source_id}__fnptr_{target_name}"
                        if auto_id not in G:
                            G.add_node(auto_id,
                                       name=target_name,
                                       source_file="",
                                       line=0,
                                       domain=source_domain,
                                       labels=["fn_ptr_target"],
                                       is_empty=False,
                                       auto_created=True)
                            id_registry[auto_id] = {"id": auto_id, "domain": source_domain, "name": target_name}
                        if not G.has_edge(source_id, auto_id):
                            G.add_edge(source_id, auto_id,
                                       call_order=edge.get("call_order"),
                                       call_condition=edge.get("call_condition", ""),
                                       concurrency="fn_ptr",
                                       confidence="AMBIGUOUS",
                                       source_tag="fn_ptr_unresolved",
                                       confidence_score=0.3,
                                       evidence=f"unresolved fn_ptr_call: {target_name}")
                    continue

                conf_score = 0.8 if is_precise else 0.5

                for dispatch_target in dispatch_targets:
                    dispatch_id = _resolve_invoked_id(dispatch_target, source_domain,
                                                      id_registry, suffix_index=suffix_index)
                    if dispatch_id and dispatch_id in G and not G.has_edge(source_id, dispatch_id):
                        # Skip prod→test field_dispatch edges — these are false
                        # positives from test struct assignments
                        if source_domain not in ("root", ""):
                            target_domain = id_registry.get(dispatch_id, {}).get("domain", "")
                            if _is_test_domain(target_domain, profile) and not _is_test_domain(source_domain, profile):
                                continue
                        # Skip dispatch self-loops: a function dispatching
                        # through a field named after itself is a false edge.
                        if source_id == dispatch_id:
                            continue
                        # Classify as vtable_dispatch if field is a known vtable field
                        is_vtable_field = target_name in _vtable_field_names
                        edge_concurrency = "vtable_dispatch" if is_vtable_field else "field_dispatch"
                        edge_source = "vtable_dispatch" if is_vtable_field else "field_dispatch"
                        edge_conf = 0.8 if is_vtable_field else conf_score
                        edge_evidence = (f"vtable_dispatch: {target_name} -> {dispatch_target}{evidence_ctx}"
                                         if is_vtable_field
                                         else f"field_dispatch: {target_name} -> {dispatch_target}{evidence_ctx}")
                        # Derive call_condition from struct_chain for vtable_dispatch
                        dispatch_condition = ""
                        if is_vtable_field:
                            module_hint = ""
                            if struct_chain:
                                # For compound chains (e.g., "transport->ops"),
                                # try each segment from left to right for a hint.
                                chain_segments = struct_chain.split("->") if "->" in struct_chain else [struct_chain]
                                for seg in chain_segments:
                                    if seg not in _GENERIC_STRUCT_CHAINS:
                                        module_hint = _extract_module_hint(seg)
                                        if module_hint:
                                            break
                            # Fallback: derive module hint from target's source_file
                            if not module_hint and dispatch_id in id_registry:
                                tgt_sf = id_registry[dispatch_id].get("source_file", "")
                                if tgt_sf:
                                    module_hint = _extract_module_hint("", source_file=tgt_sf)
                            if module_hint:
                                dispatch_condition = f"#vtable_module={module_hint}"
                        # vtable_type comes from the field's registered struct_type
                        # (looked up via _vtable_type_fields); fall back to
                        # target_name's registered struct if compound chain.
                        edge_vtable_type = ""
                        edge_vtable_module = ""
                        if is_vtable_field:
                            edge_vtable_module = module_hint
                            # Find which struct_type has this field registered
                            for _stype, _fields in _vtable_type_fields.items():
                                if target_name in _fields:
                                    edge_vtable_type = _stype
                                    break
                        G.add_edge(source_id, dispatch_id,
                                   call_order=edge.get("call_order", 0),
                                   call_condition=dispatch_condition,
                                   concurrency=edge_concurrency,
                                   confidence="INFERRED",
                                   source=edge_source,
                                   confidence_score=edge_conf,
                                   vtable_type=edge_vtable_type,
                                   vtable_bound_module=edge_vtable_module,
                                   evidence=edge_evidence)
                continue  # Original FN_PTR edge replaced by INFERRED edges
            # Check if this target is a vtable field name
            if target_name in _vtable_field_names:
                continue  # Handled by vtable dispatch INFERRED edges

            # Fallback: try resolving FN_PTR via fn_ptr_calls struct_chain
            # + field_assignments for fields not in _field_dispatch_map.
            # The scanner captures fn_ptr_calls with (field_name, struct_chain)
            # and field_assignments with (field_name, struct_chain, target_func).
            # If target_name is a field name with a known struct_chain,
            # look up field_assignments for matching target functions.
            if target_name not in _field_dispatch_map:
                source_func = _source_func
                struct_chain = _fn_ptr_struct_lookup.get((source_func, target_name), "")
                fa_targets = set()
                fa_match_type = ""  # Track how we matched for evidence
                if struct_chain:
                    # Find field_assignments matching (struct_chain, field_name).
                    # Use pre-computed _fa_by_sc_field index instead of scanning
                    # all FAs per edge.
                    for fa in _fa_by_sc_field.get((struct_chain, target_name), []):
                        if fa.get("target_func"):
                            fa_targets.add(fa["target_func"])
                    fa_match_type = "direct" if fa_targets else ""

                # Chain-tail vtable type matching: for compound struct_chains
                # like "pg->accel_fn_table", the tail segment "accel_fn_table"
                # is a struct field holding a vtable. Match it against vtable
                # struct_type names that end with the tail and have this field.
                if not fa_targets and struct_chain and _vtable_type_fields:
                    chain_parts = [p.strip() for p in struct_chain.split("->")]
                    tail = chain_parts[-1] if len(chain_parts) > 1 else ""
                    if tail and len(tail) >= 4:
                        tail_norm = tail.lower().replace("_", "")
                        for stype, fields in _vtable_type_fields.items():
                            if target_name not in fields:
                                continue
                            stype_norm = stype.lower().replace("_", "")
                            # Tail is a suffix of struct_type name
                            if stype_norm.endswith(tail_norm) or tail_norm == stype_norm:
                                for fn in fields[target_name]:
                                    if fn:
                                        fa_targets.add(fn)
                        if fa_targets:
                            fa_match_type = "chain_tail_vtable"

                # If struct_chain didn't match, try field-name-only fallback
                # with domain or small-target-set constraints
                if not fa_targets and target_name:
                    source_domain_fa = "root"
                    if source_id in id_registry:
                        source_domain_fa = id_registry[source_id].get("domain", "root")
                    # Domain-scoped fallback
                    if (source_domain_fa != "root"
                            and target_name in _field_dispatch_by_domain
                            and source_domain_fa in _field_dispatch_by_domain[target_name]):
                        domain_targets = _field_dispatch_by_domain[target_name][source_domain_fa]
                        if len(domain_targets) <= 10:
                            fa_targets = domain_targets
                            struct_chain = struct_chain or target_name  # For evidence
                    # Target-domain fallback: find targets whose domain
                    # hierarchically matches the caller's domain
                    if (not fa_targets and source_domain_fa != "root"
                            and target_name in _field_dispatch_by_target_domain):
                        td_map = _field_dispatch_by_target_domain[target_name]
                        hier_targets = set()
                        for td, targets in td_map.items():
                            if _domain_hier_match(source_domain_fa, td):
                                hier_targets.update(targets)
                        if hier_targets and len(hier_targets) <= 10:
                            fa_targets = hier_targets
                            struct_chain = struct_chain or target_name
                            fa_match_type = "target_domain_hier"
                    # Field-only fallback with small target set
                    # Skip callback fields — handled by caller_bridged/param_bridged
                    if not fa_targets and not _CB_FIELD_RE.search(target_name):
                        all_targets = _field_dispatch_flat.get(target_name, set())
                        if 0 < len(all_targets) <= 5:
                            fa_targets = all_targets
                            struct_chain = struct_chain or target_name  # For evidence
                if fa_targets:
                    source_domain = "root"
                    if source_id in id_registry:
                        source_domain = id_registry[source_id].get("domain", "root")
                    is_precise_fa = len(fa_targets) <= 3
                    conf_score_fa = 0.75 if is_precise_fa else 0.55
                    for fa_target in fa_targets:
                        dispatch_id = _resolve_invoked_id(fa_target, source_domain,
                                                          id_registry, suffix_index=suffix_index)
                        if dispatch_id and dispatch_id in G and not G.has_edge(source_id, dispatch_id):
                            # Skip prod→test
                            if source_domain not in ("root", ""):
                                td = id_registry.get(dispatch_id, {}).get("domain", "")
                                if _is_test_domain(td, profile) and not _is_test_domain(source_domain, profile):
                                    continue
                            # Skip dispatch self-loops
                            if source_id == dispatch_id:
                                continue
                            # Classify as vtable_dispatch if field is a known vtable field
                            is_vtable_fa = target_name in _vtable_field_names
                            fa_concurrency = "vtable_dispatch" if is_vtable_fa else "field_dispatch"
                            fa_source = "vtable_dispatch" if is_vtable_fa else "field_dispatch"
                            fa_conf = 0.8 if is_vtable_fa else conf_score_fa
                            fa_evidence = (f"vtable_dispatch: {struct_chain}.{target_name} -> {fa_target}"
                                           if is_vtable_fa
                                           else f"fn_ptr_dispatch: {struct_chain}.{target_name} -> {fa_target}"
                                                + (f" (chain_tail_vtable)" if fa_match_type == "chain_tail_vtable" else ""))
                            # Derive call_condition from struct_chain for vtable_dispatch
                            fa_condition = ""
                            if is_vtable_fa:
                                fa_hint = ""
                                if struct_chain:
                                    # For compound chains, try each segment
                                    chain_segments = struct_chain.split("->") if "->" in struct_chain else [struct_chain]
                                    for seg in chain_segments:
                                        if seg not in _GENERIC_STRUCT_CHAINS:
                                            fa_hint = _extract_module_hint(seg)
                                            if fa_hint:
                                                break
                                # Fallback: derive module hint from target's source_file
                                if not fa_hint and dispatch_id in id_registry:
                                    tgt_sf = id_registry[dispatch_id].get("source_file", "")
                                    if tgt_sf:
                                        fa_hint = _extract_module_hint("", source_file=tgt_sf)
                                if fa_hint:
                                    fa_condition = f"#vtable_module={fa_hint}"
                            G.add_edge(source_id, dispatch_id,
                                       call_order=edge.get("call_order", 0),
                                       call_condition=fa_condition,
                                       concurrency=fa_concurrency,
                                       confidence="INFERRED",
                                       source=fa_source,
                                       confidence_score=fa_conf,
                                       evidence=fa_evidence)
                    continue  # Original FN_PTR edge replaced

            # If the target was not in _field_dispatch_map (no field assignments),
            # still preserve the fn_ptr_call edge as AMBIGUOUS/unresolved so
            # indirect calls are visible in the graph even without vtable info.
            if target_name not in _field_dispatch_map:
                _src_domain = id_registry.get(source_id, {}).get("domain", "root") if source_id in id_registry else "root"
                target_id = _resolve_invoked_id(target_name, _src_domain,
                                                id_registry, suffix_index=suffix_index)
                if target_id and target_id in G and not G.has_edge(source_id, target_id):
                    G.add_edge(source_id, target_id,
                               call_order=edge.get("call_order"),
                               call_condition=edge.get("call_condition", ""),
                               concurrency="fn_ptr",
                               confidence="AMBIGUOUS",
                               source_tag="fn_ptr_unresolved",
                               confidence_score=0.3,
                               evidence=f"unresolved fn_ptr_call: {target_name}")
                elif not target_id or target_id not in G:
                    # Target doesn't exist in graph — create an auto node
                    auto_id = f"{source_id}__fnptr_{target_name}"
                    if auto_id not in G:
                        G.add_node(auto_id,
                                   name=target_name,
                                   source_file="",
                                   line=0,
                                   domain=_src_domain,
                                   labels=["fn_ptr_target"],
                                   is_empty=False,
                                   auto_created=True)
                        id_registry[auto_id] = {"id": auto_id, "domain": _src_domain, "name": target_name}
                    if not G.has_edge(source_id, auto_id):
                        G.add_edge(source_id, auto_id,
                                   call_order=edge.get("call_order"),
                                   call_condition=edge.get("call_condition", ""),
                                   concurrency="fn_ptr",
                                   confidence="AMBIGUOUS",
                                   source_tag="fn_ptr_unresolved",
                                   confidence_score=0.3,
                                   evidence=f"unresolved fn_ptr_call: {target_name}")
                continue

            # Otherwise, process as a regular edge with FN_PTR confidence

        # Conditional node dispatch: when the source node is a conditional
        # like <conditional:if(dev->ops->callback)>, extract the
        # field name from the condition and resolve via vtable/field dispatch.
        # This bridges the gap between conditional nodes and actual targets.
        if (source_id in id_registry
                and id_registry[source_id].get("name", "").startswith("<conditional:")
                and "->" in id_registry[source_id].get("name", "")):
            _cond_name = id_registry[source_id]["name"]
            # Extract field name from condition: "dev->ops->callback" → "callback"
            _cond_arrow_m = re.search(r'(\w+)\s*->\s*(\w+)\s*$', _cond_name)
            if _cond_arrow_m:
                _cond_field = _cond_arrow_m.group(2).lower()
                if _cond_field in _field_dispatch_flat:
                    source_domain = "root"
                    if source_id in id_registry:
                        source_domain = id_registry[source_id].get("domain", "root")
                    # Use domain-scoped dispatch
                    _cond_targets = set()
                    if _cond_field in _field_dispatch_by_domain and source_domain in _field_dispatch_by_domain[_cond_field]:
                        _cond_targets = _field_dispatch_by_domain[_cond_field][source_domain]
                    if not _cond_targets:
                        _cond_targets = _field_dispatch_flat.get(_cond_field, set())
                    # Limit to reasonable number of targets (truncate, not skip)
                    if _cond_targets:
                        if len(_cond_targets) > _MAX_VTABLE_DISPATCH_PER_CALL:
                            _cond_targets = sorted(_cond_targets)[:_MAX_VTABLE_DISPATCH_PER_CALL]
                        is_vtable_cond = _cond_field in _vtable_field_names
                        for ct in _cond_targets:
                            dispatch_id = _resolve_invoked_id(ct, source_domain,
                                                            id_registry, suffix_index=suffix_index)
                            if dispatch_id and dispatch_id in G and not G.has_edge(source_id, dispatch_id):
                                if source_id == dispatch_id:
                                    continue
                                cond_concurrency = "vtable_dispatch" if is_vtable_cond else "field_dispatch"
                                cond_conf = 0.75 if is_vtable_cond else 0.6
                                cond_evidence = (f"vtable_dispatch: {_cond_field} -> {ct}"
                                                 if is_vtable_cond
                                                 else f"cond_dispatch: {_cond_field} -> {ct}")
                                G.add_edge(source_id, dispatch_id,
                                           call_order=edge.get("call_order", 0),
                                           call_condition=edge.get("call_condition", ""),
                                           concurrency=cond_concurrency,
                                           confidence="INFERRED",
                                           source=cond_concurrency,
                                           confidence_score=cond_conf,
                                           evidence=cond_evidence)

        # Resolve target
        source_domain = "root"
        if source_id in id_registry:
            source_domain = id_registry[source_id].get("domain", "root")

        # Apply macro aliases: if target_name is a known #define macro that
        # expands to another function, resolve through the alias instead.
        # e.g., nvme_ctrlr_get_cc_async -> nvme_ctrlr_get_reg_async
        _macro_aliases = (profile.get("macro_dispatch", {}).get("macro_aliases", {})
                          if profile else {})
        if target_name in _macro_aliases:
            target_name = _macro_aliases[target_name]

        # Handle inline_asm edges: target is a bare function name from C inline asm.
        # Try to resolve via _name_to_nid first (may be an ASM function).
        if edge.get("source") == "inline_asm":
            # First try ASM alias map
            if target_name in _asm_alias_map:
                target_name = _asm_alias_map[target_name]
            # Resolve via _name_to_nid (prefers ASM definitions)
            if target_name in _name_to_nid:
                target_id = _name_to_nid[target_name]
            else:
                target_id = _resolve_invoked_id(target_name, source_domain, id_registry,
                                               suffix_index=suffix_index)
        else:
            target_id = _resolve_invoked_id(target_name, source_domain, id_registry,
                                       suffix_index=suffix_index)

        # If unresolved or resolved to non-existent node, try multi-strategy resolution
        # (same_file, import_map, same_domain, suffix_match, unique_name, fuzzy)
        # Pre-built _ms_lookups makes each call O(1) amortized, so always use it.
        ms_source = ""
        ms_confidence = ""
        ms_conf_score = None
        need_multi = (not target_id) or (target_id not in G and target_id not in id_registry)
        if need_multi:
            ms_id, ms_strategy, ms_conf = _multi_strategy_resolve(
                G, target_name, source_id, id_registry=id_registry,
                _lookups=_ms_lookups, suffix_index=suffix_index)
            if ms_id and ms_id in G and ms_conf >= 0.7:
                target_id = ms_id
                ms_source = f"multi_strategy:{ms_strategy}"
                ms_confidence = "INFERRED"
                ms_conf_score = ms_conf

        # Skip edges to parser artifacts (empty target_id means artifact detected)
        if not target_id:
            continue

        # Handle external library calls: _resolve_invoked_id returns "ext:lib:name"
        # for calls matching profile external_lib_prefixes. Create a lightweight
        # endpoint node instead of an unresolved internal node.
        if target_id.startswith("ext:"):
            parts = target_id.split(":", 2)
            ext_lib = parts[1] if len(parts) > 1 else "unknown"
            ext_func = parts[2] if len(parts) > 2 else target_name
            ext_id = f"ext.{ext_lib}.{ext_func}"
            if ext_id not in G:
                G.add_node(ext_id,
                           name=ext_func,
                           domain=f"external_{ext_lib}",
                           labels=["out_end"],
                           is_empty=False,
                           source_file="",
                           node_type="external_endpoint",
                           external_desc=f"External {ext_lib} library function")
                id_registry[ext_id] = {
                    "id": ext_id, "name": ext_func,
                    "domain": f"external_{ext_lib}", "labels": ["out_end"],
                    "is_empty": False, "source_file": "",
                    "node_type": "external_endpoint"}
            G.add_edge(source_id, ext_id,
                       call_order=edge.get("call_order"),
                       call_condition=edge.get("call_condition", ""),
                       concurrency=edge.get("concurrency", "") or "direct_call",
                       confidence="EXTRACTED",
                       source="ast",
                       confidence_score=1.0,
                       preproc_condition=edge.get("preproc_condition", ""),
                       preproc_alive=edge.get("preproc_alive", True),
                       evidence=f"external_call: {source_id.rsplit('.',1)[-1]} -> {ext_func} ({ext_lib})")
            continue

        # Skip edges to function pointer parameter names: these are local
        # variable/parameter names (like cb_fn, cpl_cb) that the scanner
        # extracted as callees but are not real functions. They appear when
        # code calls through a function pointer parameter, e.g., cb_fn(arg).
        # Detect by: unresolved name (no domain prefix) that matches common
        # fn_ptr parameter patterns. Regex is module-level (_FN_PTR_PARAM_RE)
        # to avoid recompiling on every edge (5.4M edges on kernel).
        if target_id == target_name and '.' not in target_id:
            if _FN_PTR_PARAM_RE.match(target_id):
                continue

        # Redirect production→test edges.
        # Two categories:
        #   A) External library function names (profiled ext prefixes) → create ext.* endpoint
        #   B) Non-external names resolved to test mocks → check for production
        #      alternative, or skip the edge if only test definitions exist.
        #
        # Test domain detection checks ALL domain segments, not just the first,
        # because domains like "org.test.unit" or "org.unit.slice" have
        # an organizational prefix as the first segment but are still test domains.
        if ('.' in target_id
                and target_id != source_id):
            tgt_func = target_id.rsplit('.', 1)[-1] if '.' in target_id else target_id
            tgt_func_lower = tgt_func.lower()
            tgt_domain_full = target_id.rsplit('.', 1)[0] if '.' in target_id else ''
            # A domain is "test" if any segment matches test indicators,
            # via the shared profile-driven helper (generic defaults cover
            # unit/ut/test/fuzz; reference profiles may add segments).
            tgt_is_test = _is_test_domain(tgt_domain_full, profile)
            src_is_prod = not _is_test_domain(source_domain, profile)

            if src_is_prod and tgt_is_test:
                # Category A: external lib prefix → ext.* endpoint
                redirected = False
                if _utils._EXTERNAL_LIB_PREFIXES:
                    for prefix in _utils._EXTERNAL_LIB_PREFIXES:
                        if tgt_func_lower.startswith(prefix):
                            # Check if a production definition exists — if so,
                            # this is a project-internal function, not external
                            norm_key = re.sub(r'[^a-z0-9_]', '_', tgt_func_lower)
                            prod_matches = [c for c in suffix_index.get(norm_key, [])
                                            if not _is_test_domain(
                                                c.rsplit('.', 1)[0] if '.' in c else '',
                                                profile)]
                            if prod_matches:
                                # Production definition exists — redirect to it
                                target_id = prod_matches[0]
                                redirected = True
                                break
                            ext_id = f"ext.{prefix.rstrip('_')}:{tgt_func_lower}"
                            if ext_id not in G:
                                G.add_node(ext_id,
                                           name=tgt_func,
                                           domain=f"external_{prefix.rstrip('_')}",
                                           labels=["out_end"],
                                           is_empty=False,
                                           source_file="",
                                           node_type="external_endpoint",
                                           external_desc=f"External {prefix.rstrip('_')} library function")
                                id_registry[ext_id] = {
                                    "id": ext_id, "name": tgt_func,
                                    "domain": f"external_{prefix.rstrip('_')}",
                                    "labels": ["out_end"], "is_empty": False,
                                    "source_file": "", "node_type": "external_endpoint"}
                            G.add_edge(source_id, ext_id,
                                       call_order=edge.get("call_order"),
                                       call_condition=edge.get("call_condition", ""),
                                       concurrency=edge.get("concurrency", "") or "direct_call",
                                       confidence="EXTRACTED",
                                       source="ast",
                                       confidence_score=1.0,
                                       preproc_condition=edge.get("preproc_condition", ""),
                                       preproc_alive=edge.get("preproc_alive", True),
                                       evidence=f"external_call: {source_id.rsplit('.',1)[-1]} -> {tgt_func} ({prefix.rstrip('_')})")
                            target_id = None  # Skip original edge
                            redirected = True
                            break

                # Category B: non-external name resolved to test mock
                if not redirected:
                    norm_key = re.sub(r'[^a-z0-9_]', '_', tgt_func_lower)
                    prod_matches = [c for c in suffix_index.get(norm_key, [])
                                    if not _is_test_domain(
                                        c.rsplit('.', 1)[0] if '.' in c else '',
                                        profile)]
                    if prod_matches:
                        # Production definition exists — use it instead
                        target_id = prod_matches[0]
                    else:
                        # No production definition — production code can't call
                        # a test-only function. Skip this edge.
                        target_id = None
        if target_id is None:
            continue

        # Skip CALLBACK_ARG edges from production callers to test targets.
        # The scanner's callback detection matches bare identifier arguments
        # against all known function names, including test helpers. When
        # production code passes a variable named "channel" or "iobuf" as
        # an argument, it shouldn't create an edge to unit.thread.channel.
        if edge.get("confidence") == "CALLBACK_ARG":
            caller_src = id_registry.get(source_id, {}).get("source_file", "")
            caller_is_test = ('/test/' in f'/{caller_src}/'
                              or caller_src.startswith('test/'))
            if not caller_is_test and target_id in id_registry:
                target_src = id_registry[target_id].get("source_file", "")
                target_is_test = ('/test/' in f'/{target_src}/'
                                  or target_src.startswith('test/'))
                if target_is_test:
                    continue

        call_order = edge.get("call_order")
        call_condition = edge.get("call_condition", "")

        # For FN_PTR edges, only create edges to existing function nodes.
        # FN_PTR targets are often generic names (cb_fn, handler) or
        # unresolvable field names — creating synthetic nodes for them
        # adds noise. Skip if the target doesn't already exist.
        # Also skip name collisions: FN_PTR call ->field_name() resolves
        # to a function that happens to be named "field_name" but isn't
        # actually assigned to that struct field. Require field_assignment
        # evidence linking the field to the target function.
        if edge.get("concurrency") == "fn_ptr":
            if target_id not in G:
                continue
            # Name collision check: verify the target function is actually
            # assigned to this field via a field_assignment record.
            source_func = _source_func
            struct_chain = _fn_ptr_struct_lookup.get((source_func, target_name), "")
            has_fa = False
            # Use pre-computed _fa_by_field_name index instead of scanning
            # all FAs per fn_ptr edge.
            for fa in _fa_by_field_name.get(target_name, []):
                fa_chain = fa.get("struct_chain", "")
                fa_target = fa.get("target_func", "")
                if (fa_target == target_name
                        and (fa_chain == struct_chain or not struct_chain)):
                    has_fa = True
                    break
            if not has_fa:
                continue  # Name collision — skip

        # Ensure target node exists with proper attributes.
        # When the target couldn't be resolved to any scanned function
        # definition (target_id is the bare callee name with no domain
        # prefix), the call is to an external library, system builtin, or
        # language builtin. Assign to the "external" domain rather than
        # the caller's domain, so project domains stay clean of phantom
        # leaf nodes. The caller's domain is only inherited when the
        # target_id already carries a domain prefix (cross-domain call
        # to a real scanned function in another domain).
        if target_id not in G:
            if '.' in target_id:
                _auto_domain = source_domain
            else:
                _auto_domain = "external"
            G.add_node(target_id,
                       name=target_name,
                       domain=_auto_domain,
                       labels=[],
                       is_empty=False,
                       source_file="",
                       auto_created=True)
            id_registry[target_id] = {
                "id": target_id, "domain": _auto_domain,
                "name": target_name, "labels": [], "is_empty": False}

        # Default concurrency for edges without explicit type:
        # CALLBACK_ARG → callback (generic callback dispatch)
        # EXTRACTED → direct_call (directly observed call)
        # INFERRED → direct_call (cross-file resolved or dispatch
        #   inference — all INFERRED edges set concurrency at creation
        #   time; this is a structural catch-all for any that slip
        #   through with empty concurrency)
        # Note: scanner may already set concurrency to "poller" or "interrupt"
        # for registration functions that create periodic pollers or interrupt
        # handlers — preserve those rather than overriding to "callback".
        edge_concurrency = edge.get("concurrency", "")
        if not edge_concurrency and edge.get("confidence") == "CALLBACK_ARG":
            edge_concurrency = "callback"
        elif not edge_concurrency and edge.get("confidence") == "EXTRACTED":
            edge_concurrency = "direct_call"
        elif not edge_concurrency and edge.get("confidence") == "INFERRED":
            edge_concurrency = "direct_call"

        # Guard against self-loops: a self-loop is almost always a resolution
        # bug (e.g., qualified target ID normalized to match the caller's name).
        # Exception: callback self-loops where a function schedules itself
        # (e.g., timer re-arming) are legitimate.
        if source_id == target_id:
            edge_conf = ms_confidence or edge.get("confidence", "EXTRACTED")
            edge_conc = edge_concurrency
            # Allow legitimate callback self-loops (function scheduling itself)
            if edge_conf == "CALLBACK_ARG" or edge_conc == "callback":
                pass  # legitimate self-loop
            else:
                continue  # skip resolution-bug self-loop

        # Build evidence string if not already present
        edge_evidence = edge.get("evidence", "")
        if not edge_evidence and edge_concurrency == "direct_call":
            src_func = source_id.rsplit(".", 1)[-1] if "." in source_id else source_id
            tgt_func = target_id.rsplit(".", 1)[-1] if "." in target_id else target_id
            edge_evidence = f"direct_call: {src_func} -> {tgt_func}"
        elif not edge_evidence and edge_concurrency == "contains":
            edge_evidence = "contains: file contains function definition"
        elif not edge_evidence and edge_concurrency == "callback":
            src_func = source_id.rsplit(".", 1)[-1] if "." in source_id else source_id
            tgt_func = target_id.rsplit(".", 1)[-1] if "." in target_id else target_id
            edge_conf = ms_confidence or edge.get("confidence", "EXTRACTED")
            if edge_conf == "CALLBACK_ARG":
                edge_evidence = f"callback_arg: {src_func} registers {tgt_func} as callback"
            else:
                edge_evidence = f"callback: {src_func} -> {tgt_func}"
        elif not edge_evidence and edge_concurrency == "spawn_target":
            src_func = source_id.rsplit(".", 1)[-1] if "." in source_id else source_id
            tgt_func = target_id.rsplit(".", 1)[-1] if "." in target_id else target_id
            edge_evidence = f"spawn_target: {src_func} spawns {tgt_func}"
        elif not edge_evidence and edge_concurrency == "fn_ptr":
            src_func = source_id.rsplit(".", 1)[-1] if "." in source_id else source_id
            tgt_func = target_id.rsplit(".", 1)[-1] if "." in target_id else target_id
            edge_evidence = f"fn_ptr: {src_func} -> {tgt_func}"
        elif not edge_evidence and edge_concurrency == "poller":
            src_func = source_id.rsplit(".", 1)[-1] if "." in source_id else source_id
            tgt_func = target_id.rsplit(".", 1)[-1] if "." in target_id else target_id
            edge_evidence = f"poller: {src_func} registers {tgt_func} as poller"
        elif not edge_evidence and edge_concurrency == "interrupt":
            src_func = source_id.rsplit(".", 1)[-1] if "." in source_id else source_id
            tgt_func = target_id.rsplit(".", 1)[-1] if "." in target_id else target_id
            edge_evidence = f"interrupt: {src_func} registers {tgt_func} as interrupt handler"

        # Validate callback signature compatibility — reduce false positives
        # where a function with many parameters is incorrectly tagged as a callback.
        _cb_confidence = ms_confidence or edge.get("confidence", "EXTRACTED")
        if _cb_confidence == "CALLBACK_ARG" or edge_concurrency == "callback":
            # Check if callback parameter count matches registration function expectation
            caller_node = G.nodes[source_id] if source_id in G else id_registry.get(source_id, {})
            callee_node = G.nodes[target_id] if target_id in G else id_registry.get(target_id, {})
            caller_sig = caller_node.get("signature", "")
            callee_sig = callee_node.get("signature", "")
            if caller_sig and callee_sig:
                # Simple heuristic: count parameter count mismatch
                caller_params = caller_sig.count(",") + 1 if "(" in caller_sig else 0
                callee_params = callee_sig.count(",") + 1 if "(" in callee_sig else 0
                # Callbacks typically have fewer params than their registration function.
                # If callee has more params than expected, likely a false positive.
                if callee_params > caller_params and callee_params > 3:
                    _cb_confidence = "AMBIGUOUS"
        confidence = _cb_confidence

        G.add_edge(source_id, target_id,
                   call_order=call_order,
                   call_condition=call_condition,
                   concurrency=edge_concurrency,
                   confidence=confidence,
                   source=ms_source or edge.get("source_tag") or edge.get("source") or "ast",
                   confidence_score=ms_conf_score if ms_conf_score is not None else edge.get("confidence_score", 1.0),
                   preproc_condition=edge.get("preproc_condition", ""),
                   preproc_alive=edge.get("preproc_alive", True),
                   evidence=edge_evidence)

    # Disable deferred mode: subsequent phases (goto annotation, vtable dispatch,
    # domain splitting) need edge attribute access via G.edges(data=True), G[u][v].
    if _using_streaming:
        G.set_deferred(False)

    # Annotate call edges with goto control flow information.
    # For functions with goto_jumps, we add a call_condition annotation to
    # edges whose call_order falls within a goto's control flow range:
    #   - backward goto (loop): calls between the label and goto may repeat
    #   - forward goto (skip): calls between the goto and target may be skipped
    # Uses callee_args line info for precise line-based range matching.
    from _builder.build.build_phases import _annotate_call_edges_with_goto
    _annotate_call_edges_with_goto(G)

    # Process vtable registrations: create vtable_dispatch edges
    # For each fn_table struct type, build a map: struct_type → {field_name → [func_ids]}
    # Then for each fn_ptr_call (caller->table->field()), create INFERRED edges
    # from the caller to ALL registered implementations of that field.
    # At query time, bindings (e.g., module=nvme) determine which one to follow.
    vtable_regs = extraction.get("vtable_registrations", [])
    fn_ptr_calls = extraction.get("fn_ptr_calls", {})

    # Cap vtable_dispatch edges per fn_ptr_call to avoid combinatorial
    # explosion on large codebases (e.g., large C projects have 37K fn_ptr_call
    # sites × 125K registrations). The default of 50 is overridable via
    # profile.dispatch_tuning.max_vtable_dispatch_per_call (set near the top
    # of build_graph). Per-field overrides in
    # profile.dispatch_tuning.max_vtable_dispatch_per_field take precedence
    # for the named field.
    # _MAX_VTABLE_DISPATCH_PER_CALL is defined above (profile-driven).

    # Pre-build reverse index: callee_name → set of caller node IDs
    # Used by inline fn_ptr_call flattening to find callers of inline wrappers
    _invoked_to_invoker_ids = {}
    for u, v, edata in G.edges(data=True):
        if not _edge_is_call(edata):
            continue
        callee_name = id_registry.get(v, {}).get("name", "")
        if not callee_name:
            # Auto-created nodes may be in G but not id_registry
            callee_name = G.nodes.get(v, {}).get("name", "")
        if callee_name:
            _invoked_to_invoker_ids.setdefault(callee_name, set()).add(u)

    # Callback field patterns (used by P2A, P2B, and main vtable dispatch)
    _CALLBACK_FIELD_PATTERNS = (
        'cb_fn', 'cb_func', 'cb', 'callback',
        'completion_cb', 'done_cb', 'cpl_cb',
    )

    # Skip vtable dispatch entirely when there are no fn_ptr_calls
    # (no dispatch targets to create edges for)
    vtable_index = None
    _field_to_structs = None
    if vtable_regs and fn_ptr_calls:
        # Build struct_type → {field → [func_name]} index
        vtable_index = defaultdict(lambda: defaultdict(list))
        # Also build var_name → struct_type lookup for disambiguation
        var_to_struct = {}
        for vtable in vtable_regs:
            struct_type = vtable.get("struct_type", "")
            if not struct_type:
                continue
            vn = vtable.get("var_name", "")
            if vn:
                var_to_struct[vn.lower()] = struct_type
            for reg in vtable.get("registrations", []):
                field = reg["field"]
                func_name = reg["func_name"]
                condition = reg.get("condition", "")
                vtable_index[struct_type][field].append({
                    "func_name": func_name,
                    "condition": condition,
                    "source_file": vtable.get("source_file", ""),
                    "var_name": vtable.get("var_name", vn),
                })
        # Build reverse index: field_name → [struct_types that have this field].
        # Without this, each fn_ptr_call scans all struct types via
        # [st for st, fields in vtable_index.items() if field_name in fields]
        # — O(calls × struct_types) on large vtable-heavy codebases.
        _field_to_structs = defaultdict(list)
        for st, fields in vtable_index.items():
            for fname in fields:
                _field_to_structs[fname].append(st)
        # For each fn_ptr_call, find matching vtable registrations and create edges.
        # When the caller is a static inline (no node ID in the graph), we
        # "flatten" the dispatch: create edges from the callers OF the inline
        # function instead. This handles patterns like inline_accessor() in
        # headers which does obj->ops->method(args, ...).
        for caller_name, calls in fn_ptr_calls.items():
            # Resolve caller to node ID using pre-built name→nid index
            invoker_id = _name_to_nid.get(caller_name)
            if not invoker_id or invoker_id not in G:
                # Static inline fn_ptr_call: find callers of this inline function
                # and create vtable_dispatch edges from them instead
                inline_caller_ids = _invoked_to_invoker_ids.get(caller_name, set())
                if not inline_caller_ids:
                    continue
                # Create vtable_dispatch edges from each inline caller
                for inline_caller_id in inline_caller_ids:
                    if inline_caller_id not in G:
                        continue
                    inline_domain = id_registry.get(inline_caller_id, {}).get("domain", "")
                    for call_info in calls:
                        field_name = call_info["field_name"]
                        struct_chain = call_info["struct_chain"]
                        candidate_structs = list(_field_to_structs.get(field_name, []))
                        matched_structs = _disambiguate_struct_chain(
                            struct_chain, candidate_structs, var_to_struct,
                            caller_domain=inline_domain,
                            embedding_index=_embedding_index)
                        if not matched_structs and candidate_structs:
                            matched_structs = candidate_structs
                        for struct_type in matched_structs:
                            regs = vtable_index[struct_type].get(field_name, [])
                            if len(regs) > _MAX_VTABLE_DISPATCH_PER_CALL:
                                regs = sorted(regs, key=lambda r: (
                                    -(1 if set(re.split(r'[._/]', r.get("source_file", "").lower()))
                                      & set(re.split(r'[._]', inline_domain.lower())) else 0),
                                    r.get("source_file", "")))[:_MAX_VTABLE_DISPATCH_PER_CALL]
                            for reg in regs:
                                func_name = reg["func_name"]
                                target_id = _resolve_invoked_id(
                                    func_name, inline_domain, id_registry,
                                    suffix_index=suffix_index)
                                if not target_id or target_id not in G:
                                    continue
                                if inline_caller_id == target_id:
                                    continue
                                if G.has_edge(inline_caller_id, target_id):
                                    continue
                                is_cb = (field_name in _CALLBACK_FIELD_PATTERNS
                                         or field_name.endswith('_cb')
                                         or field_name.endswith('_callback')
                                         or field_name.endswith('_fn'))
                                G.add_edge(inline_caller_id, target_id,
                                           call_order=None,
                                           call_condition=f"#inlined_fn_ptr_call={caller_name}",
                                           concurrency="callback" if is_cb else "vtable_dispatch",
                                           confidence="INFERRED",
                                           source="vtable_analysis_inline",
                                           confidence_score=0.70 if is_cb else 0.75,
                                           preproc_condition="",
                                           preproc_alive=True,
                                           evidence=f"vtable_dispatch(inlined {caller_name}): {struct_type}.{field_name}={func_name}")
                continue  # Skip normal processing for this caller_name

            for call_info in calls:
                field_name = call_info["field_name"]
                struct_chain = call_info["struct_chain"]

                # Find which struct types have this field
                candidate_structs = list(_field_to_structs.get(field_name, []))

                # Disambiguate using struct_chain: if the chain hints at a
                # specific struct type, only match that one (or those).
                # This prevents over-dispatching where a call through
                # bdev->fn_table->get_io_channel() incorrectly matches
                # accel_module_if.get_io_channel implementations.
                matched_structs = _disambiguate_struct_chain(
                    struct_chain, candidate_structs, var_to_struct,
                    caller_domain=id_registry.get(invoker_id, {}).get("domain", ""),
                    embedding_index=_embedding_index)

                # If the struct_chain strongly hints at a specific struct type
                # (via var_name match) but that struct type doesn't have the
                # requested field, skip this edge entirely rather than falling
                # through to unrelated struct types.
                # E.g., module_if->get_memory_domains() where module_if suggests
                # accel_module_if but that struct doesn't have
                # get_memory_domains — don't dispatch to dev_fn_table.
                #
                # EXCEPTION: generic chain names like "ops", "fn_table", etc.
                # are shared across many struct types. var_to_struct maps these
                # with last-write-wins semantics, so the mapping is unreliable.
                # For generic names, fall through to domain-based disambiguation
                # instead of skipping.
                _GENERIC_CHAIN_NAMES = {'fn_table', 'ops', 'module', 'dev', 'impl',
                                        'ctx', 'ch', 'cb_args', 'req', 'desc', 'entry'}
                if (struct_chain.lower() in var_to_struct
                        and struct_chain.lower() not in _GENERIC_CHAIN_NAMES):
                    hinted_struct = var_to_struct[struct_chain.lower()]
                    if hinted_struct not in candidate_structs:
                        # struct_chain is a known var_name pointing to a struct
                        # that doesn't have this field — skip
                        continue

                # Also check keyword hints: if the struct_chain suggests a specific
                # struct type that doesn't have the requested field, skip the dispatch
                # rather than falling through to unrelated struct types.
                # E.g., module_if->get_memory_domains() where module_if suggests
                # accel_module_if but that struct doesn't have get_memory_domains.
                #
                # Heuristic: if the normalized struct_chain is a substring of a
                # non-candidate struct type name (but NOT a substring of any candidate),
                # the dispatch is likely wrong. This avoids false positives from
                # generic keywords like "impl" which appear in both candidates and
                # non-candidates.
                if (matched_structs == candidate_structs and len(candidate_structs) > 0
                        and struct_chain.lower() not in _GENERIC_CHAIN_NAMES):
                    chain_norm = struct_chain.lower().replace('_', '')
                    # Check if chain is a substring of any candidate
                    chain_in_candidate = any(chain_norm in st.lower().replace('_', '')
                                             for st in candidate_structs)
                    if not chain_in_candidate:
                        # Chain is NOT a substring of any candidate — check non-candidates
                        for st in vtable_index:
                            if st in candidate_structs:
                                continue
                            if chain_norm in st.lower().replace('_', ''):
                                # struct_chain is a substring of a non-candidate struct
                                # that doesn't have this field — wrong dispatch
                                matched_structs = []
                                break

                for struct_type in matched_structs:
                    fields = vtable_index[struct_type]

                    # Determine if caller is from test directory
                    caller_src = id_registry.get(invoker_id, {}).get("source_file", "")
                    caller_is_test = ('/test/' in f'/{caller_src}/'
                                      or caller_src.startswith('test/'))

                    # Var-name filtering: when struct_chain is a g_-prefixed
                    # global variable name (conventional C pattern for static
                    # module instances), only dispatch to registrations from
                    # that specific var_name. E.g., g_sw_module->submit_tasks()
                    # should only dispatch to g_sw_module's registrations, not
                    # all accel_module_if registrations.
                    # Do NOT filter for non-g_ chains (fn_table, ops, req) as
                    # those are typically local field accesses that dispatch
                    # through dynamically-set pointers.
                    filter_var_name = ""
                    if struct_chain.startswith("g_") and struct_chain.lower() in var_to_struct:
                        filter_var_name = struct_chain.lower()

                    registrations = fields[field_name]
                    if filter_var_name:
                        registrations = [r for r in registrations
                                         if r.get("var_name", "").lower() == filter_var_name]
                        # If filtering eliminates all registrations, fall back
                        # to unfiltered (the g_ name might be a local variable)
                        if not registrations:
                            registrations = fields[field_name]

                    # Cap vtable_dispatch edges per fn_ptr_call
                    # (see _MAX_VTABLE_DISPATCH_PER_CALL definition above).
                    # Per-field override via profile.dispatch_tuning.max_vtable_dispatch_per_field.
                    _field_cap = _effective_vtable_cap(field_name)
                    if len(registrations) > _field_cap:
                        caller_domain = id_registry.get(invoker_id, {}).get("domain", "root")
                        # Sort: same-domain first, then by source_file proximity
                        def _reg_priority(reg):
                            reg_src = reg.get("source_file", "")
                            reg_domain_parts = set(re.split(r'[._/]', reg_src.lower()))
                            caller_parts = set(re.split(r'[._]', caller_domain.lower()))
                            same_domain = 1 if (caller_parts & reg_domain_parts) else 0
                            return (-same_domain, reg_src)
                        registrations = sorted(registrations, key=_reg_priority)[:_field_cap]

                    for reg in registrations:
                        func_name = reg["func_name"]
                        reg_condition = reg.get("condition", "")
                        var_name = reg.get("var_name", "")

                        # Skip test-directory vtable registrations when caller is
                        # production code. Test mocks register their own implementations
                        # of vtable fields (e.g., dev_ut_if.get_io_channel), but these
                        # should not create dispatch edges from production callers.
                        reg_src = reg.get("source_file", "")
                        reg_is_test = ('/test/' in f'/{reg_src}/'
                                       or reg_src.startswith('test/'))
                        if not caller_is_test and reg_is_test:
                            continue

                        # Resolve the registered function to a node ID
                        target_id = _resolve_invoked_id(func_name,
                                                       id_registry.get(invoker_id, {}).get("domain", "root"),
                                                       id_registry,
                                                       suffix_index=suffix_index)
                        if not target_id:
                            # Try multi-strategy resolve for the registered function
                            ms_id, ms_strategy, ms_conf = _multi_strategy_resolve(
                                G, func_name, invoker_id, id_registry=id_registry,
                                _lookups=_ms_lookups, suffix_index=suffix_index)
                            if ms_id and ms_id in G and ms_conf >= 0.7:
                                target_id = ms_id

                        if not target_id or target_id not in G:
                            continue

                        # Create vtable_dispatch edge from caller to registered impl
                        if not G.has_edge(invoker_id, target_id):
                            # Determine the dispatch condition:
                            # Extract module hint from var_name (and optionally
                            # source_file as fallback) for dispatch conditioning.
                            module_hint = _extract_module_hint(
                                var_name, struct_type=struct_type,
                                source_file=reg.get("source_file", ""))

                            # Improve module_hint using struct embedding index:
                            # If the vtable struct_type is an inner_type in an
                            # embedding entry with a domain_hint, and the
                            # registration's source_file domain matches the
                            # embedding's domain_hint, use the embedding hint.
                            if _embedding_index and not module_hint:
                                embeddings = _embedding_index.get(struct_type, [])
                                reg_sf = reg.get("source_file", "")
                                reg_domain_parts = set(re.split(r'[._/]',
                                    re.sub(r'^lib/', '', reg_sf).split('/')[0].lower()))
                                for emb in embeddings:
                                    hint = emb.get("domain_hint", "")
                                    if hint:
                                        hint_parts = set(re.split(r'[._]', hint.lower()))
                                        if reg_domain_parts & hint_parts:
                                            module_hint = hint
                                            break

                            dispatch_condition = ""
                            if reg_condition:
                                dispatch_condition = reg_condition
                            elif module_hint:
                                dispatch_condition = f"#vtable_module={module_hint}"

                            # Callback fields (cb_fn, cb_func, etc.) are completion
                            # callbacks, not vtable dispatch — classify as callback.
                            is_callback_field = (
                                field_name in _CALLBACK_FIELD_PATTERNS
                                or field_name.endswith('_cb')
                                or field_name.endswith('_callback')
                                or field_name.endswith('_fn')
                            )
                            edge_concurrency = "callback" if is_callback_field else "vtable_dispatch"
                            edge_confidence_score = 0.75 if is_callback_field else 0.8

                            # Skip dispatch self-loops (resolution bug)
                            if invoker_id == target_id:
                                continue

                            # Structured vtable metadata for SQL-native filtering.
                            # vtable_type = the struct holding the dispatch field
                            # (e.g., super_operations, address_space_operations).
                            # vtable_bound_module = the module hint derived from
                            # the vtable variable name (e.g., ext4_sops → ext4).
                            edge_vtable_type = struct_type if edge_concurrency == "vtable_dispatch" else ""
                            edge_vtable_module = module_hint if edge_concurrency == "vtable_dispatch" else ""

                            G.add_edge(invoker_id, target_id,
                                       call_order=None,
                                       call_condition=dispatch_condition,
                                       concurrency=edge_concurrency,
                                       confidence="INFERRED",
                                       source="vtable_analysis",
                                       confidence_score=edge_confidence_score,
                                       preproc_condition="",
                                       preproc_alive=True,
                                       vtable_type=edge_vtable_type,
                                       vtable_bound_module=edge_vtable_module,
                                       evidence=f"vtable_dispatch: {struct_type}.{field_name}={func_name} (var={var_name}, module={module_hint})" if edge_concurrency == "vtable_dispatch" else f"callback_field: {struct_type}.{field_name}={func_name} (var={var_name}, module={module_hint})")

    # ------------------------------------------------------------------
    # Macro dispatch: constructor→global list→iterative call chain
    # ------------------------------------------------------------------
    # Handles registration macros like PROJ_SUBSYSTEM_REGISTER(name) that
    # expand to __attribute__((constructor)) adding a struct to a global list,
    # which an iterator function later walks calling a dispatch field.
    macro_regs = extraction.get("macro_registrations", [])
    if macro_regs and profile:
        macro_dispatch = profile.get("macro_dispatch", {})
        reg_macros = macro_dispatch.get("registration_macros", [])

        # Group macro_registrations by macro_name
        regs_by_macro = defaultdict(list)
        for mr in macro_regs:
            regs_by_macro[mr["macro_name"]].append(mr)

        for reg_entry in reg_macros:
            macro_name = reg_entry.get("macro_name", "")
            iterator_func = reg_entry.get("iterator_func", "")
            dispatch_field = reg_entry.get("dispatch_field", "")
            handler_arg_index = reg_entry.get("handler_arg_index")

            if not macro_name:
                continue

            registrations = regs_by_macro.get(macro_name, [])
            if not registrations:
                continue

            # Category 1: iterator_func + dispatch_field
            # The iterator walks a global list and calls struct->dispatch_field()
            # on each registered struct. We create INFERRED edges from the
            # iterator function to each dispatch field implementation.
            if iterator_func and dispatch_field:
                # Find iterator function node in graph using pre-built index
                iterator_id = _name_to_nid.get(iterator_func)

                if not iterator_id:
                    # Try partial match (iterator might have domain prefix)
                    for fname, fid in _name_to_nid.items():
                        if fname.endswith(iterator_func):
                            iterator_id = fid
                            break

                if iterator_id:
                    # For each registration, find the dispatch_field implementation
                    # via cross-reference with vtable_registrations
                    _macro_iter_count = 0
                    for mr in registrations:
                        if _macro_iter_count >= _MAX_VTABLE_DISPATCH_PER_CALL:
                            break
                        struct_var = mr.get("struct_var", "")
                        if not struct_var:
                            continue

                        # Find vtable registrations for this struct variable
                        dispatch_target = None
                        for vt in vtable_regs:
                            vt_var = vt.get("var_name", "")
                            # Match struct variable name (case-insensitive)
                            if vt_var and vt_var.lower() == struct_var.lower():
                                for reg in vt.get("registrations", []):
                                    if reg.get("field") == dispatch_field:
                                        dispatch_target = reg.get("func_name")
                                        break
                                if dispatch_target:
                                    break

                        if not dispatch_target:
                            # Fallback: try to find a function named
                            # <struct_prefix>_<dispatch_field>
                            # E.g., g_bdev_subsys.init → bdev_init
                            continue

                        # Find target function node using pre-built index
                        target_id = _name_to_nid.get(dispatch_target)

                        if target_id and not G.has_edge(iterator_id, target_id):
                            # Skip dispatch self-loops
                            if iterator_id == target_id:
                                continue
                            # Skip prod→test macro_dispatch edges: test/fuzz
                            # handlers should not appear as dispatch targets
                            # from production iterator functions.
                            tgt_domain = id_registry.get(target_id, {}).get("domain", "")
                            if _is_test_domain(tgt_domain, profile):
                                continue

                            G.add_edge(iterator_id, target_id,
                                       call_order=None,
                                       call_condition=f"#macro_dispatch={macro_name}",
                                       concurrency="macro_dispatch",
                                       confidence="INFERRED",
                                       source="macro_dispatch",
                                       confidence_score=0.85,
                                       preproc_condition="",
                                       preproc_alive=True,
                                       evidence=f"macro_dispatch: {macro_name} {struct_var}.{dispatch_field}={dispatch_target} (iterator={iterator_func})")
                            _macro_iter_count += 1

            # Category 3: dispatch_caller + handler_arg_index (register-then-dispatch)
            # Pattern: a registration macro stores a handler function pointer via
            # register_func, and a dispatch_caller function later invokes it.
            # E.g., PROJECT_REGISTER(method, func, mask) →
            #   project_register_method stores func → dispatch_handler calls func
            dispatch_caller = reg_entry.get("dispatch_caller", "")
            if handler_arg_index is not None and dispatch_caller and not iterator_func:
                # Find dispatch_caller node in graph using pre-built index
                invoker_id = _name_to_nid.get(dispatch_caller)
                if not invoker_id:
                    # Try partial match
                    for fname, fid in _name_to_nid.items():
                        if fname.endswith(dispatch_caller):
                            invoker_id = fid
                            break

                if invoker_id:
                    _macro_cat3_count = 0
                    for mr in registrations:
                        if _macro_cat3_count >= _MAX_VTABLE_DISPATCH_PER_CALL:
                            break
                        # Extract handler function name from struct_var
                        # For registration macros, struct_var is the handler name
                        # when handler_arg_index is set (it's the arg at that index)
                        handler_name = mr.get("struct_var", "")
                        if not handler_name:
                            continue

                        # Find handler function node using pre-built index
                        target_id = _name_to_nid.get(handler_name)

                        if target_id and not G.has_edge(invoker_id, target_id):
                            # Skip dispatch self-loops
                            if invoker_id == target_id:
                                continue
                            # Skip prod→test macro_dispatch edges (Category 3)
                            tgt_domain = id_registry.get(target_id, {}).get("domain", "")
                            if _is_test_domain(tgt_domain, profile):
                                continue

                            G.add_edge(invoker_id, target_id,
                                       call_order=None,
                                       call_condition=f"#macro_dispatch={macro_name}",
                                       concurrency="macro_dispatch",
                                       confidence="INFERRED",
                                       source="macro_dispatch",
                                       confidence_score=0.80,
                                       preproc_condition="",
                                       preproc_alive=True,
                                       evidence=f"macro_dispatch: {macro_name} handler={handler_name} (caller={dispatch_caller}, reg={reg_entry.get('register_func', '')})")
                            _macro_cat3_count += 1

    # ------------------------------------------------------------------
    # Inline fn_ptr wrapper dispatch
    # ------------------------------------------------------------------
    # When a function name matches known inline wrapper patterns (call_*, __call_*)
    # and exists as an auto-created node (no source_file, no body), it likely
    # wraps a fn_ptr_call like obj->ops->method(args). The scanner doesn't
    # extract fn_ptr_calls from header-defined inline functions, so we detect
    # these wrapper nodes by name pattern and create vtable_dispatch edges
    # from their callers to the vtable registrations for matching field names.
    # Match inline fn_ptr wrapper patterns (O5): pattern list is profile-driven
    # via dispatch_tuning.inline_wrapper_patterns. Defaults match the historical
    # behavior (call_X / __call_X / invoke_X / __invoke_X). Each pattern must
    # capture the vtable field name in group 1 so we can dispatch to the right
    # vtable registrations.
    _inline_dispatch_count = 0
    if vtable_regs:
        # Build vtable_index if not already built (may already exist from
        # the fn_ptr_calls processing block above)
        if vtable_index is None:
            vtable_index = defaultdict(lambda: defaultdict(list))
            var_to_struct = {}
            for vtable in vtable_regs:
                struct_type = vtable.get("struct_type", "")
                if not struct_type:
                    continue
                vn = vtable.get("var_name", "")
                if vn:
                    var_to_struct[vn.lower()] = struct_type
                for reg in vtable.get("registrations", []):
                    field = reg["field"]
                    func_name = reg["func_name"]
                    condition = reg.get("condition", "")
                    vtable_index[struct_type][field].append({
                        "func_name": func_name,
                        "condition": condition,
                        "source_file": vtable.get("source_file", ""),
                        "var_name": vtable.get("var_name", vn),
                    })
        if _field_to_structs is None:
            _field_to_structs = defaultdict(list)
            for st, fields in vtable_index.items():
                for fname in fields:
                    _field_to_structs[fname].append(st)
        for nid, ndata in G.nodes(data=True):
            node_name = ndata.get("name", "")
            if not node_name:
                continue
            # Must be an auto-created node (no source_file) or a header-defined
            # inline (source_file ends with .h). Inline fn_ptr wrappers in
            # headers are exactly the targets we want to dispatch from.
            sf = ndata.get("source_file", "")
            if sf and not sf.endswith('.h'):
                continue  # Has a .c source file — not an inline wrapper
            # Must match an inline wrapper pattern from the profile-driven list.
            # Try each pattern in order; the first match wins. Each pattern
            # captures the vtable field name in group 1.
            m = None
            for _iw_re in _inline_wrapper_regexes:
                m = _iw_re.match(node_name)
                if m:
                    break
            if not m:
                continue
            # Extract the likely field name from the wrapper name
            # call_write_iter → write_iter, call_read_iter → read_iter
            field_name = m.group(1) if m.lastindex and m.lastindex >= 1 else ""
            if not field_name:
                continue  # Pattern matched but no capture group — cannot dispatch
            # Find callers of this wrapper node (call edges only)
            wrapper_callers = []
            if nid in G:
                for pred in G.predecessors(nid):
                    # Use get_edge_data (always a method) instead of G.edges[pred, nid]
                    # (which can be a cached_property descriptor in some networkx
                    # versions, leading to TypeError: 'method' object is not
                    # subscriptable). get_edge_data returns None if the edge
                    # doesn't exist, or a dict of attributes if it does.
                    _edge_attrs = G.get_edge_data(pred, nid)
                    if _edge_attrs is not None and _edge_is_call(_edge_attrs):
                        wrapper_callers.append(pred)
            if not wrapper_callers:
                continue
            # Find vtable registrations for this field (case-insensitive match
            # since vtable field names may have different casing)
            field_name_lower = field_name.lower()
            candidate_structs = [st for st, fields in vtable_index.items()
                                 if field_name_lower in fields
                                 or any(f.lower() == field_name_lower for f in fields)]
            if not candidate_structs:
                continue
            for invoker_id in wrapper_callers:
                if invoker_id not in G:
                    continue
                caller_domain = id_registry.get(invoker_id, G.nodes.get(invoker_id, {})).get("domain", "")
                for struct_type in candidate_structs:
                    # Try exact match first, then case-insensitive fallback
                    regs = vtable_index[struct_type].get(field_name, [])
                    if not regs:
                        for fn_key, fn_regs in vtable_index[struct_type].items():
                            if fn_key.lower() == field_name_lower:
                                regs = fn_regs
                                break
                    _iw_field_cap = _effective_vtable_cap(field_name)
                    if len(regs) > _iw_field_cap:
                        regs = sorted(regs, key=lambda r: (
                            -(1 if set(re.split(r'[._/]', r.get("source_file", "").lower()))
                              & set(re.split(r'[._]', caller_domain.lower())) else 0),
                            r.get("source_file", "")))[:_iw_field_cap]
                    for reg in regs:
                        func_name = reg["func_name"]
                        target_id = _resolve_invoked_id(
                            func_name, caller_domain, id_registry,
                            suffix_index=suffix_index)
                        if not target_id or target_id not in G:
                            continue
                        if invoker_id == target_id:
                            continue
                        if G.has_edge(invoker_id, target_id):
                            continue
                        is_cb = (field_name in _CALLBACK_FIELD_PATTERNS
                                 or field_name.endswith('_cb')
                                 or field_name.endswith('_callback')
                                 or field_name.endswith('_fn'))
                        G.add_edge(invoker_id, target_id,
                                   call_order=None,
                                   call_condition=f"#inline_wrapper={node_name}",
                                   concurrency="callback" if is_cb else "vtable_dispatch",
                                   confidence="INFERRED",
                                   source="inline_wrapper_dispatch",
                                   confidence_score=0.70 if is_cb else 0.75,
                                   preproc_condition="",
                                   preproc_alive=True,
                                   evidence=f"vtable_dispatch(inline {node_name}): {struct_type}.{field_name}={func_name}")
                        _inline_dispatch_count += 1
    if _inline_dispatch_count:
        print(f"  Inline wrapper vtable_dispatch: {_inline_dispatch_count} edges")

    # ------------------------------------------------------------------
    # Caller-bridged cb_fn resolution
    # ------------------------------------------------------------------
    # When a function F contains an fn_ptr_call with field_name=cb_fn
    # (e.g., ctx->cb_fn(status)), the cb_fn target can be resolved by
    # looking at callers of F that also have CALLBACK_ARG edges.
    # Pattern: caller C calls F(cb_fn_arg), scanner creates CALLBACK_ARG
    # edge C -> cb_fn_arg. Inside F, the cb_fn_arg is stored in a struct
    # and later invoked via ctx->cb_fn(). So cb_fn_arg IS the dispatch target.
    #
    # This pass resolves fn_ptr cb_fn calls by bridging through the caller's
    # CALLBACK_ARG edges. It only applies to callback field patterns (cb_fn,
    # cb_func, etc.) which are generic completion callbacks that can't be
    # resolved through field_assignments alone (different local variable names).
    # Callback field detection: exact matches + suffix patterns.
    # Exact names cover the most common C callback parameter conventions.
    # Suffix patterns (_cb, _fn, _func, _cb_fn, _cb_func) catch variant
    # names like remove_cb, unregister_cb, start_cb_fn, decode_func, etc.
    _CALLBACK_FIELD_EXACT = frozenset({
        'cb_fn', 'cb_func', 'cb', 'callback',
        'completion_cb', 'done_cb', 'cpl_cb',
    })
    _CALLBACK_FIELD_SUFFIX_RE = re.compile(
        r'(?:_cb|_fn|_func|_cb_fn|_cb_func|cb_fn|cb_func)$')

    def _is_callback_field(name: str) -> bool:
        return name in _CALLBACK_FIELD_EXACT or bool(_CALLBACK_FIELD_SUFFIX_RE.search(name))

    # Build: fn_ptr_call source_func -> set of callback field_names
    _fn_ptr_cb_fields = defaultdict(set)
    for func_name, calls in extraction.get("fn_ptr_calls", {}).items():
        for c in (calls if isinstance(calls, list) else []):
            field = c.get("field_name", "")
            if _is_callback_field(field):
                _fn_ptr_cb_fields[func_name].add(field)

    # Single merged pass over G.edges to build three indexes consumed by
    # the caller-bridged, passthrough, and param-bridged dispatch blocks:
    #   _cbarg_from      -> caller_node -> {target_node} for CALLBACK_ARG edges
    #   _callers_by_name -> target_func_name -> {caller_node_ids}
    #   _cbarg_by_callee -> callee_name -> [(invoker_id, target_id, arg_idx)]
    _cbarg_from = defaultdict(set)
    _callers_by_name = defaultdict(set)
    _cbarg_by_callee = {}
    _need_cbarg_by_callee = bool(_param_bridged_fa)
    for u, v, d in G.edges(data=True):
        conf = d.get("confidence")
        if conf == "CALLBACK_ARG":
            _cbarg_from[u].add(v)
            if _need_cbarg_by_callee:
                ev = d.get("evidence", "")
                cc = d.get("call_condition", "")
                callee_name = ""
                if cc.startswith("#callback="):
                    callee_name = cc[len("#callback="):]
                if not callee_name and "callback_arg:" in ev:
                    parts = ev.split("callback_arg:")[1].strip()
                    callee_name = parts.split("(")[0].strip() if "(" in parts else ""
                arg_idx = -1
                if "arg#" in ev:
                    arg_str = ev.split("arg#")[1].split("=")[0]
                    try:
                        arg_idx = int(arg_str)
                    except ValueError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                if callee_name:
                    _cbarg_by_callee.setdefault(callee_name, []).append(
                        (u, v, arg_idx))
        v_name = G.nodes[v].get("name", "") if v in G else ""
        if v_name:
            _callers_by_name[v_name].add(u)

    if _fn_ptr_cb_fields:
        # For each function with cb_fn fn_ptr_calls, bridge through callers
        _bridged_count = 0
        _MAX_CALLERS_FOR_BRIDGED = 15  # Too many callers = imprecise bridging
        _MAX_BRIDGED_TARGETS = 20      # Too many targets = over-dispatch
        for func_name, cb_fields in _fn_ptr_cb_fields.items():
            # Find the function node in the graph using pre-built name index
            # (O(1) instead of O(N) linear scan of all nodes)
            func_nid = _name_to_nid.get(func_name)
            if not func_nid or func_nid not in G:
                continue
            func_nodes = [func_nid]

            # Find callers of this function
            callers = _callers_by_name.get(func_name, set())
            if not callers:
                continue

            # Skip functions with too many callers — the bridging becomes
            # too imprecise (cross-product of all callers' callbacks)
            if len(callers) > _MAX_CALLERS_FOR_BRIDGED:
                continue

            # Collect CALLBACK_ARG targets from callers
            bridged_targets = set()
            for caller in callers:
                bridged_targets.update(_cbarg_from.get(caller, set()))

            if not bridged_targets:
                continue

            # Skip if too many bridged targets — indicates a generic
            # callback dispatch function (like bdev_io_complete)
            if len(bridged_targets) > _MAX_BRIDGED_TARGETS:
                continue

            # Add INFERRED callback edges from this function to bridged targets
            for func_id in func_nodes:
                func_domain = G.nodes[func_id].get("domain", "root")
                # Domain-scoped filtering: prefer targets in same domain
                # to avoid over-dispatch across unrelated modules
                same_domain_targets = set()
                cross_domain_targets = set()
                for target_id in bridged_targets:
                    if target_id == func_id:
                        continue  # Skip self-loops
                    tgt_domain = G.nodes[target_id].get("domain", "") if target_id in G else ""
                    # Skip prod→test
                    if _is_test_domain(tgt_domain, profile) and not _is_test_domain(func_domain, profile):
                        continue
                    if G.has_edge(func_id, target_id):
                        continue  # Already have an edge
                    # Check domain relationship: same domain or parent/child
                    if (tgt_domain == func_domain
                            or tgt_domain.startswith(func_domain + ".")
                            or func_domain.startswith(tgt_domain + ".")):
                        same_domain_targets.add(target_id)
                    else:
                        cross_domain_targets.add(target_id)

                # Apply same-domain targets (high confidence)
                # Limit cross-domain targets to prevent over-dispatch
                _MAX_CROSS_DOMAIN_BRIDGED = 5
                selected = same_domain_targets
                if len(cross_domain_targets) <= _MAX_CROSS_DOMAIN_BRIDGED:
                    selected = selected | cross_domain_targets

                for target_id in selected:
                    tgt_name = G.nodes[target_id].get("name", target_id) if target_id in G else target_id
                    G.add_edge(func_id, target_id,
                               call_order=None,
                               call_condition="",
                               concurrency="callback",
                               confidence="INFERRED",
                               source="caller_bridged",
                               confidence_score=0.70,
                               evidence=f"caller_bridged: {func_name} cb_fn -> {tgt_name} (via CALLBACK_ARG from caller)")
                    _bridged_count += 1

        if _bridged_count:
            print(f"  Caller-bridged cb_fn resolution: {_bridged_count} edges")

    # ------------------------------------------------------------------
    # 2-hop caller-bridged resolution
    # ------------------------------------------------------------------
    # For functions F that ARE callbacks (called via CALLBACK_ARG) and
    # internally call cb_fn(), the 1-hop caller_bridged doesn't help
    # because the caller's CALLBACK_ARG target IS F itself.
    # 2-hop: F <-cbarg- C <-direct_call- G, where G has CALLBACK_ARG -> T.
    # Then F's cb_fn likely calls T (the grandcaller's callback target).
    # This handles the pattern:
    #   G calls C(direct) and also passes T as callback
    #   C calls F as a callback (F IS the callback passed to C)
    #   F internally calls cb_fn() which should resolve to T
    if _fn_ptr_cb_fields:
        _2hop_count = 0
        _MAX_2HOP_GRANDCALLERS = 10  # Limit grandcaller fanout
        for func_name, cb_fields in _fn_ptr_cb_fields.items():
            # Use pre-built name index for O(1) lookup instead of O(N) scan
            func_nid = _name_to_nid.get(func_name)
            if not func_nid or func_nid not in G:
                continue
            func_nodes = [func_nid]

            # Check if this function has CALLBACK_ARG incoming
            # (meaning it IS a callback)
            has_cbarg_in = False
            cbarg_callers = set()
            for fid in func_nodes:
                for u, _, d in G.in_edges(fid, data=True):
                    if d.get("confidence") == "CALLBACK_ARG":
                        has_cbarg_in = True
                        cbarg_callers.add(u)
            if not has_cbarg_in:
                continue  # 1-hop caller_bridged already handles this

            # For each CALLBACK_ARG caller C, find direct_call callers G
            # who have CALLBACK_ARG targets
            bridged_targets = set()
            grandcaller_count = 0
            for invoker_id in cbarg_callers:
                for gc_id, _, d in G.in_edges(invoker_id, data=True):
                    if d.get("concurrency") != "direct_call":
                        continue
                    grandcaller_count += 1
                    if grandcaller_count > _MAX_2HOP_GRANDCALLERS:
                        break
                    # Get grandcaller's CALLBACK_ARG targets
                    for _, gc_target, gc_d in G.out_edges(gc_id, data=True):
                        if gc_d.get("confidence") == "CALLBACK_ARG":
                            # Skip if target is the function itself or its caller
                            if gc_target in func_nodes or gc_target == invoker_id:
                                continue
                            bridged_targets.add(gc_target)
                if grandcaller_count > _MAX_2HOP_GRANDCALLERS:
                    break

            if not bridged_targets:
                continue

            # Skip if too many targets — indicates imprecise bridging
            if len(bridged_targets) > _MAX_BRIDGED_TARGETS:
                continue

            # Add INFERRED callback edges with domain filtering
            for func_id in func_nodes:
                func_domain = G.nodes[func_id].get("domain", "root")
                same_domain_targets = set()
                for target_id in bridged_targets:
                    if target_id == func_id:
                        continue
                    if G.has_edge(func_id, target_id):
                        continue
                    tgt_domain = G.nodes[target_id].get("domain", "") if target_id in G else ""
                    if _is_test_domain(tgt_domain, profile) and not _is_test_domain(func_domain, profile):
                        continue
                    # Require same domain or parent/child relationship
                    if (tgt_domain == func_domain
                            or tgt_domain.startswith(func_domain + ".")
                            or func_domain.startswith(tgt_domain + ".")):
                        same_domain_targets.add(target_id)

                for target_id in same_domain_targets:
                    tgt_name = G.nodes[target_id].get("name", target_id) if target_id in G else target_id
                    # Derive call_condition from target function name
                    cc = _extract_module_hint(tgt_name) or "#callback"
                    G.add_edge(func_id, target_id,
                               call_order=None,
                               call_condition=cc,
                               concurrency="callback",
                               confidence="INFERRED",
                               source="caller_bridged_2hop",
                               confidence_score=0.60,
                               evidence=f"caller_bridged_2hop: {func_name} cb_fn -> {tgt_name} (via CALLBACK_ARG from grandcaller)")
                    _2hop_count += 1

        if _2hop_count:
            print(f"  2-hop caller-bridged cb_fn resolution: {_2hop_count} edges")

    # ------------------------------------------------------------------
    # Passthrough caller-bridged resolution
    # ------------------------------------------------------------------
    # When function F is a passthrough registration function (it receives a
    # function-pointer parameter and forwards it to a real registration function),
    # callers of F that pass a known callback function as that parameter should
    # get callback edges. The scanner detects F as passthrough, but the
    # cross-file callback detection can't resolve parameter names like "cb_fn"
    # to actual function names. This builder pass resolves the gap by looking
    # at callers of F that have CALLBACK_ARG edges — those edges point to the
    # actual callback functions that F's callers pass through.
    _passthrough_funcs = extraction.get("passthrough_reg_funcs", {})
    if _passthrough_funcs:
        # _cbarg_from and _callers_by_name were built in the merged edge pass above.

        # Transitive passthrough resolution: for each passthrough function,
        # walk the caller chain upward to find ancestors with CALLBACK_ARG edges.
        # Then create INFERRED callback edges from EACH passthrough function
        # in the chain to the discovered callback targets.
        #
        # Chain example: A --calls--> B(cb_fn) --calls--> C(cb_fn) --calls--> reg(cb_fn)
        # If A has CALLBACK_ARG edge A -> target_fn, then:
        #   - B should get callback edge B -> target_fn
        #   - C should get callback edge C -> target_fn
        # This shows the callback dispatch path through the passthrough chain.
        _MAX_PT_DEPTH = 5
        _pt_bridged = 0

        for pt_name, pt_info in _passthrough_funcs.items():
            pt_concurrency = pt_info.get("concurrency_type", "callback")
            # Use pre-built name index for O(1) lookup instead of O(N) scan
            pt_nid = _name_to_nid.get(pt_name)
            if not pt_nid or pt_nid not in G:
                continue
            pt_nodes = [pt_nid]

            # Find direct callers of this passthrough function
            direct_callers = _callers_by_name.get(pt_name, set())
            if not direct_callers:
                continue

            # Walk upward from each direct caller to find ancestors with
            # CALLBACK_ARG edges. Stop walking up from a caller once we
            # find its CALLBACK_ARG targets — going further up the chain
            # picks up unrelated callbacks from different dispatch paths.
            visited = set()
            queue = list(direct_callers)
            discovered_targets = set()  # All callback targets found up the chain

            for _ in range(_MAX_PT_DEPTH):
                next_queue = []
                for invoker_id in queue:
                    if invoker_id in visited:
                        continue
                    visited.add(invoker_id)

                    # Check if this caller has CALLBACK_ARG edges
                    cb_targets = _cbarg_from.get(invoker_id, set())
                    if cb_targets:
                        discovered_targets.update(cb_targets)
                        # Stop walking further up from this caller — we
                        # found its dispatch targets. Going further up
                        # would pick up unrelated callbacks.
                        continue

                    # No CALLBACK_ARG at this level — walk further up
                    caller_name = G.nodes[invoker_id].get("name", "") if invoker_id in G else ""
                    if caller_name:
                        parent_callers = _callers_by_name.get(caller_name, set())
                        for pc in parent_callers:
                            if pc not in visited:
                                next_queue.append(pc)

                if not next_queue:
                    break
                queue = next_queue

            if not discovered_targets:
                continue

            # Create INFERRED callback edges from the passthrough function
            # to the discovered callback targets. Apply same domain-scoping
            # as caller_bridged: prefer same-domain targets, limit cross-domain
            # to prevent over-dispatch.
            _MAX_CROSS_DOMAIN_PT = 5
            for pt_id in pt_nodes:
                pt_domain = G.nodes[pt_id].get("domain", "root") if pt_id in G else "root"
                same_domain_targets = set()
                cross_domain_targets = set()
                for target_id in discovered_targets:
                    if target_id == pt_id:
                        continue  # Skip self-loops
                    tgt_domain = G.nodes[target_id].get("domain", "") if target_id in G else ""
                    # Skip cross-domain targets with no domain relationship.
                    # Passthrough bridging can create false edges when a generic
                    # registration function (like rpc_register_method) is
                    # the intermediate — it routes callbacks from many modules
                    # to many targets, and the bridging incorrectly connects
                    # unrelated source→target pairs.
                    if tgt_domain and pt_domain and tgt_domain != pt_domain:
                        is_related = (
                            tgt_domain.startswith(pt_domain + ".")
                            or pt_domain.startswith(tgt_domain + "."))
                        if not is_related:
                            # Check shared domain component (e.g., bdev.raid and bdev.nvme share 'bdev')
                            pt_parts = set(pt_domain.split("."))
                            tgt_parts = set(tgt_domain.split("."))
                            if not pt_parts.intersection(tgt_parts):
                                continue  # No shared component — skip
                    # Skip prod→test
                    if _is_test_domain(tgt_domain, profile) and not _is_test_domain(pt_domain, profile):
                        continue
                    if G.has_edge(pt_id, target_id):
                        continue  # Already have an edge
                    # Check domain relationship: same domain or parent/child
                    if (tgt_domain == pt_domain
                            or tgt_domain.startswith(pt_domain + ".")
                            or pt_domain.startswith(tgt_domain + ".")):
                        same_domain_targets.add(target_id)
                    else:
                        cross_domain_targets.add(target_id)

                # Apply same-domain targets (high confidence) with limit.
                # Passthrough functions that wrap generic dispatchers (like
                # thread_send_msg) can accumulate many same-domain targets
                # from unrelated callers — cap to keep signal-to-noise high.
                _MAX_SAME_DOMAIN_PT = 10
                selected = set()
                if len(same_domain_targets) <= _MAX_SAME_DOMAIN_PT:
                    selected = same_domain_targets
                if len(cross_domain_targets) <= _MAX_CROSS_DOMAIN_PT:
                    selected = selected | cross_domain_targets

                for target_id in selected:
                    tgt_name = G.nodes[target_id].get("name", target_id) if target_id in G else target_id
                    G.add_edge(pt_id, target_id,
                               call_order=None,
                               call_condition="",
                               concurrency="callback",
                               confidence="INFERRED",
                               source="passthrough_bridged",
                               confidence_score=0.70,
                               evidence=f"passthrough_bridged: {pt_name} -> {tgt_name} (via transitive CALLBACK_ARG from callers)")
                    _pt_bridged += 1

        if _pt_bridged:
            print(f"  Passthrough caller-bridged resolution: {_pt_bridged} edges")

    # Generic domain hints — used by param-bridged dispatch and vtable fallback
    _GENERIC_DOMAIN_HINTS = frozenset({
        'ops', 'module', 'ctx', 'req', 'data', 'entry', 'obj',
        'handle', 'ptr', 'buf', 'result', 'ret', 'base', 'dev',
        'impl', 'desc', 'table', 'fn', 'cb', 'config', 'state',
        'info', 'param', 'params', 'opts', 'cfg', 'core', 'main',
    })

    # ------------------------------------------------------------------
    # Param-bridged field_dispatch resolution
    # ------------------------------------------------------------------
    # When a struct field is assigned a function-pointer parameter
    # (e.g., ctx->data_transfer_cpl = cb_fn where cb_fn is a parameter),
    # we can't resolve the target directly. Instead, we find callers of
    # the assigning function and use their CALLBACK_ARG edges to determine
    # what actual callback each caller passes.
    #
    # Chain: caller calls assign_func(callback_func, ...)
    #        → assign_func sets struct.field = cb_fn (param_index=N)
    #        → fn_ptr_call site calls struct.field()
    #        → we create INFERRED edge from fn_ptr_call site to callback_func
    _param_bridged_count = 0
    if _param_bridged_fa:
        # _cbarg_by_callee was built in the merged edge pass above.

        # For each param-bridged FA, resolve the dispatch targets
        # Pre-compute: for polymorphic fields, count assign_funcs per struct_chain
        # to detect generic chains (like "ctx") with many unrelated assign_funcs.
        _poly_sc_assign_count = {}  # (field_name, sc_norm) -> count of assign_funcs
        if _polymorphic_fields:
            for (fn, sc), ents in _param_bridged_fa.items():
                if fn in _polymorphic_fields:
                    _poly_sc_assign_count[(fn, sc)] = len(set(e[0] for e in ents))

        # Pre-compute index of fn_ptr_call sources keyed by (field_name,
        # normalized struct_chain) so the per-FA dispatch below uses an O(1)
        # lookup instead of scanning every fn_ptr_call for every outer entry.
        _fn_ptr_calls_by_field_sc = defaultdict(list)  # (field_name, sc_norm) -> [caller_name]
        for caller_name, calls in extraction.get("fn_ptr_calls", {}).items():
            for call in (calls if isinstance(calls, list) else []):
                fn = call.get("field_name", "")
                if not fn:
                    continue
                call_sc = call.get("struct_chain", "").replace("->", ".")
                _fn_ptr_calls_by_field_sc[(fn, call_sc)].append(caller_name)

        for (field_name, sc_norm), entries in _param_bridged_fa.items():
            # Polymorphic fields (cb_fn, cb_func, etc.) require stricter
            # domain matching: the fn_ptr_call source must be in the SAME
            # domain as the assign_func (not just sharing a component).
            # Generic struct_chains like "ctx" have many unrelated assign_funcs
            # and callers, so shared-component matching creates massive over-dispatch.
            _is_poly = field_name in _polymorphic_fields
            # For polymorphic fields: skip generic struct_chains that have
            # many assign_funcs (>=3) AND are single-component (no sub-path).
            # "ctx" has 31 assign_funcs but "raid_io.waitq_entry" has 1 —
            # the former is too imprecise, the latter is fine.
            if _is_poly:
                sc_assign_count = _poly_sc_assign_count.get((field_name, sc_norm), 0)
                is_generic_sc = ('.' not in sc_norm and sc_assign_count >= 3)
                if is_generic_sc:
                    continue
            for assign_func, param_name, param_index in entries:
                if param_index < 0:
                    continue  # Can't resolve without param index
                # Find CALLBACK_ARG edges for calls TO assign_func
                cbarg_entries = _cbarg_by_callee.get(assign_func, [])
                if not cbarg_entries:
                    continue
                # Filter by matching param_index
                resolved_targets = set()
                for invoker_id, target_id, arg_idx in cbarg_entries:
                    if arg_idx == param_index and target_id in G:
                        resolved_targets.add(target_id)
                if not resolved_targets:
                    continue
                # Limit resolved targets to prevent over-dispatch
                if len(resolved_targets) > 10:
                    continue
                # Find fn_ptr_call sources: functions that have fn_ptr_calls
                # with this field_name and struct_chain matching sc_norm EXACTLY.
                # The fn_ptr_call source needs to be in the same domain as assign_func.
                assign_func_id = _name_to_nid.get(assign_func)
                if not assign_func_id or assign_func_id not in G:
                    continue
                assign_func_domain = G.nodes[assign_func_id].get("domain", "")
                fn_ptr_sources = set()
                for caller_name in _fn_ptr_calls_by_field_sc.get((field_name, sc_norm), ()):
                    caller_nid = _name_to_nid.get(caller_name)
                    if caller_nid and caller_nid in G:
                        fn_ptr_sources.add(caller_nid)
                if not fn_ptr_sources:
                    continue
                # Create INFERRED edges from fn_ptr_call sources to resolved targets
                for src_id in fn_ptr_sources:
                    src_domain = G.nodes[src_id].get("domain", "") if src_id in G else ""
                    # For polymorphic fields: require fn_ptr_call source in
                    # SAME domain as assign_func (not just shared component).
                    # Generic struct_chains like "ctx" have assign_funcs from
                    # many different domains, so shared-component matching
                    # creates massive over-dispatch (31 funcs × 64 callers).
                    if _is_poly and src_domain != assign_func_domain:
                        continue
                    for tgt_id in resolved_targets:
                        if tgt_id == src_id:
                            continue  # Skip self-loops
                        if G.has_edge(src_id, tgt_id):
                            continue  # Already have an edge
                        tgt_domain = G.nodes[tgt_id].get("domain", "") if tgt_id in G else ""
                        # Skip prod→test
                        if _is_test_domain(tgt_domain, profile) and not _is_test_domain(src_domain, profile):
                            continue
                        # Require domain proximity: at least one shared
                        # domain component between source and target
                        src_components = set(src_domain.split(".")) if src_domain else set()
                        tgt_components = set(tgt_domain.split(".")) if tgt_domain else set()
                        if src_components and tgt_components:
                            shared = src_components & tgt_components - {"root"}
                            if not shared:
                                continue
                        tgt_name = G.nodes[tgt_id].get("name", "") if tgt_id in G else ""
                        # Derive call_condition from assigning function's domain
                        pb_condition = ""
                        if assign_func_domain and assign_func_domain != "root":
                            parts = assign_func_domain.split(".")
                            hint = parts[-1] if len(parts) > 1 else assign_func_domain
                            if hint and hint not in _GENERIC_DOMAIN_HINTS:
                                pb_condition = f"#param_bridged={hint}"
                        G.add_edge(src_id, tgt_id,
                                   call_order=None,
                                   call_condition=pb_condition,
                                   concurrency="field_dispatch",
                                   confidence="INFERRED",
                                   source="param_bridged_dispatch",
                                   confidence_score=0.80,
                                   evidence=f"param_bridged_dispatch: {field_name} -> {tgt_name} (via {assign_func} param#{param_index}={param_name})")
                        _param_bridged_count += 1

    if _param_bridged_count:
        print(f"  Param-bridged dispatch resolution: {_param_bridged_count} edges")

    # ------------------------------------------------------------------
    # Merged edge annotation pass: single traversal for all post-processing
    # annotations (dispatch call_condition, crossfile cleanup, field_dispatch
    # call_condition, callback call_condition, spawn_target call_condition).
    # This replaces 5 separate list(G.edges(data=True)) traversals to avoid
    # creating 5 copies of 3.7M edges in memory (~18.5M tuple creations).
    # ------------------------------------------------------------------
    _domain_filled = 0
    _csf_removed = 0
    _fd_cc_filled = 0
    _cb_cc_filled = 0
    _sp_cc_filled = 0

    # Crossfile suffix struct field removal requires deferred deletion
    _edges_to_remove = []

    # Callback patterns (compile once)
    _CB_CC_PATTERNS = [
        (r'callback_arg:\s*(\w+)\s*\(\)', '#callback={}'),
        (r'concurrency_callback:\s*(\w+)\s*\(\)', '#callback={}'),
        (r'crossfile_suffix_callback:\s*\w+\s*->\s*(\w+)\s*\(\)', '#callback={}'),
        (r'crossfile_suffix_struct_field:\s*\w+\s*->\s*(\w+)\s*\(\)', '#callback={}'),
        (r'callback_field:\s*\w+\.\w+=\w+', '#callback=field_dispatch'),
        (r'caller_bridged:\s*\w+\s+(\w+)\s*->', '#callback=caller_bridged_{}'),
        (r'passthrough_bridged:', '#callback=passthrough_bridged'),
        (r'crossfile_struct_field:\s*\w+\s*->\s*(\w+)\s*\(\)', '#callback={}'),
        (r'callback_alias:\s*(\w+)\s*\(\)', '#callback={}'),
        (r'crossfile_reg_callback:\s*\w+\s*->\s*(\w+)\s*\(\)', '#callback={}'),
        (r'poller:\s*(\w+)\s+registers', '#poller={}'),
        (r'interrupt:\s*(\w+)\s+registers', '#interrupt={}'),
    ]
    import re as _cb_re
    _cb_cc_compiled = [(_cb_re.compile(p), t) for p, t in _CB_CC_PATTERNS]

    # Crossfile suffix struct field configuration
    _CSF_GENERIC_FIELDS = frozenset({
        'queue', 'event', 'data', 'ctx', 'handle', 'io', 'req',
        'buf', 'channel', 'poller', 'desc', 'ctrlr', 'qpair',
        'send_request', 'seek', 'entry', 'result', 'dev', 'ptr',
    })
    _CSF_ALWAYS_REMOVE_FIELDS = frozenset({
        'func',
    })

    for u, v, edata in G.edges(data=True):
        conc = edata.get("concurrency", "")

        # 1) Dispatch call_condition fallback: domain-derived hint
        if conc in ("vtable_dispatch", "field_dispatch") and not edata.get("call_condition", ""):
            vdom = G.nodes.get(v, {}).get("domain", "") if v in G else ""
            if vdom:
                parts = vdom.split(".")
                hint = parts[-1] if len(parts) > 1 else vdom
                if hint and hint not in _GENERIC_DOMAIN_HINTS:
                    tag = "vtable_module" if conc == "vtable_dispatch" else "field_module"
                    G[u][v]["call_condition"] = f"#{tag}={hint}"
                    _domain_filled += 1

        # 2) Cross-domain crossfile_suffix_struct_field false positives
        ev = edata.get("evidence", "")
        if isinstance(ev, str) and "crossfile_suffix_struct_field" in ev:
            field = ""
            if "->" in ev:
                parts = ev.split("->")
                if len(parts) >= 2:
                    field = parts[-1].split("=")[0]
            if field in _CSF_ALWAYS_REMOVE_FIELDS:
                _edges_to_remove.append((u, v))
                _csf_removed += 1
                continue  # Skip further processing for this edge
            if field in _CSF_GENERIC_FIELDS:
                u_dom = G.nodes.get(u, {}).get("domain", "") if u in G else ""
                v_dom = G.nodes.get(v, {}).get("domain", "") if v in G else ""
                u_top = u_dom.split(".")[0] if u_dom else ""
                v_top = v_dom.split(".")[0] if v_dom else ""
                if u_top and v_top and u_top != v_top:
                    _edges_to_remove.append((u, v))
                    _csf_removed += 1
                    continue

        # 3) field_dispatch call_condition from evidence
        if conc == "field_dispatch" and not edata.get("call_condition"):
            if isinstance(ev, str) and "field_dispatch:" in ev:
                m = _cb_re.match(r'field_dispatch:\s*(\S+)\s*->', ev)
                if m:
                    field_name = m.group(1)
                    struct_m = _cb_re.search(r'\(struct=(\w+)\)', ev)
                    if struct_m:
                        struct_name = struct_m.group(1)
                        G[u][v]["call_condition"] = f"#field_dispatch={struct_name}.{field_name}"
                    else:
                        G[u][v]["call_condition"] = f"#field_dispatch={field_name}"
                    _fd_cc_filled += 1

        # 4) Callback call_condition from evidence
        if conc in ("callback", "poller", "interrupt") and not edata.get("call_condition"):
            if isinstance(ev, str):
                conc_type = conc
                for pat, template in _cb_cc_compiled:
                    m = pat.match(ev)
                    if m:
                        if '{}' in template and m.lastindex and m.lastindex >= 1:
                            cc = template.format(m.group(1))
                        else:
                            cc = template
                        if conc_type == "poller" and cc.startswith("#callback="):
                            cc = "#poller=" + cc[len("#callback="):]
                        elif conc_type == "interrupt" and cc.startswith("#callback="):
                            cc = "#interrupt=" + cc[len("#callback="):]
                        try:
                            G[u][v]["call_condition"] = cc
                        except (KeyError, TypeError) as exc:
                            print(f"  Warning: failed to set call_condition on edge "
                                  f"{u!r}→{v!r}: {exc}", file=sys.stderr)
                        _cb_cc_filled += 1
                        break

        # 5) spawn_target call_condition from evidence
        if conc == "spawn_target" and not edata.get("call_condition"):
            if isinstance(ev, str):
                if "pthread_create" in ev:
                    G[u][v]["call_condition"] = "#spawn=pthread_create"
                    _sp_cc_filled += 1
                elif "fork" in ev:
                    G[u][v]["call_condition"] = "#spawn=fork"
                    _sp_cc_filled += 1

    # Remove crossfile false positive edges (deferred from pass above)
    for u, v in _edges_to_remove:
        if G.has_edge(u, v):
            G.remove_edge(u, v)

    if _domain_filled:
        print(f"  Dispatch call_condition (domain fallback): {_domain_filled} edges")
    if _csf_removed:
        print(f"  Removed crossfile_suffix_struct_field false positives: {_csf_removed} edges")
    if _fd_cc_filled:
        print(f"  field_dispatch call_condition (from evidence): {_fd_cc_filled} edges")
    if _cb_cc_filled:
        print(f"  callback call_condition (from evidence): {_cb_cc_filled} edges")
    if _sp_cc_filled:
        print(f"  spawn_target call_condition (from evidence): {_sp_cc_filled} edges")

    # ------------------------------------------------------------------
    # Macro-to-function bridging
    # ------------------------------------------------------------------
    print("  Building macro-to-function bridge index...", file=sys.stderr)
    # Kernel code frequently uses macros that forward to double-underscore
    # implementations, injecting __func__/__LINE__ for tracing:
    #   #define module_func(h, i) __module_func(h, i, __func__, __LINE__)
    # The scanner sees the macro name as a call target, creating a node for
    # "module_func" but no edge to "__module_func".
    # This pass detects such macro-wrapper/function-implementation pairs and
    # creates INFERRED bridge edges.
    _macro_bridge_count = 0
    # Build name → node_id index for fast lookup from both id_registry and G.nodes
    # (some auto-created nodes exist in G but not in id_registry)
    _name_to_nid_lower = {}
    for nid, ndata in id_registry.items():
        n = ndata.get("name", "")
        if n:
            _name_to_nid_lower[n.lower()] = nid
    # Also index nodes from G that may not be in id_registry
    # Use G.nodes(data=True) to iterate and access in one pass
    for nid, ndata in G.nodes(data=True):
        if not isinstance(nid, str):
            continue  # Skip non-string node IDs
        if nid in id_registry:
            continue  # Already indexed above
        n = ndata.get("name", "")
        if n and n.lower() not in _name_to_nid_lower:
            _name_to_nid_lower[n.lower()] = nid
    for nid, ndata in G.nodes(data=True):
        if not isinstance(nid, str):
            continue  # Skip non-string node IDs
        node_name = ndata.get("name", "")
        if not node_name:
            continue
        # Only bridge FROM nodes that look like macro wrappers:
        # - no source_file (macro invocation, not a real function definition)
        # - OR source_file ends with .h (header-only inline/macro)
        sf = ndata.get("source_file", "")
        if sf and not sf.endswith('.h'):
            continue
        # Skip double-underscore-prefixed names: they ARE the impl side.
        if node_name.startswith('__'):
            continue
        # Iterate over profile-driven macro_bridge_patterns (O6).
        # Each pattern is a (regex, impl_template) pair. The regex matches the
        # macro/wrapper name; the impl template formats the match to produce
        # the implementation name. Default: (r"^(\w+)$", "__{1}") — i.e., the
        # historical behavior of looking up __<name>. Template uses {N} to
        # reference the Nth capture group (1-indexed, matching regex syntax).
        node_domain = ndata.get("domain", "")
        for _mb_re, _mb_impl in _macro_bridge_compiled:
            _mb_m = _mb_re.match(node_name)
            if not _mb_m:
                continue
            # Format the implementation name template. {1} → group 1, {2} →
            # group 2, etc. str.format() with positional args is 0-indexed, so
            # we prepend a placeholder at index 0 to make {1} map to group 1.
            _groups = [''] + [_mb_m.group(i) for i in range(1, (_mb_m.lastindex or 0) + 1)]
            try:
                du_name = _mb_impl.format(*_groups)
            except (IndexError, KeyError) as _mb_err:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
            if not du_name or du_name == node_name:
                continue
            du_nid = _name_to_nid_lower.get(du_name.lower())
            if not du_nid or du_nid not in G:
                continue
            du_data = id_registry.get(du_nid, G.nodes.get(du_nid, {}))
            du_domain = du_data.get("domain", "")
            # Same-domain check (configurable via macro_bridge_require_same_domain).
            # When enabled, the macro and its implementation must share at
            # least the first domain component, and the second if both have
            # one — this prevents cross-subsystem false-positive bridges.
            if _macro_bridge_require_same_domain:
                if not node_domain or not du_domain:
                    continue
                node_parts = node_domain.split('.')
                du_parts = du_domain.split('.')
                if not node_parts or not du_parts or node_parts[0] != du_parts[0]:
                    continue
                if len(node_parts) > 1 and len(du_parts) > 1 and node_parts[1] != du_parts[1]:
                    continue
            # Don't bridge if edge already exists
            if G.has_edge(nid, du_nid):
                continue
            # Skip self-loops
            if nid == du_nid:
                continue
            # Defensive: stale id_registry entries may reference removed nodes
            if nid not in G or du_nid not in G:
                continue
            G.add_edge(nid, du_nid,
                       call_order=None,
                       call_condition="",
                       concurrency="macro_bridge",
                       confidence="INFERRED",
                       source="macro_bridge",
                       confidence_score=0.95,
                       preproc_condition="",
                       preproc_alive=True,
                       evidence=f"macro_bridge: {node_name} -> {du_name} (same domain: {node_domain})")
            _macro_bridge_count += 1
            break  # Only apply the first matching pattern per node
    if _macro_bridge_count:
        print(f"  Macro-to-function bridging: {_macro_bridge_count} edges")

    # ------------------------------------------------------------------
    # Conditional node parent edges
    # ------------------------------------------------------------------
    # Conditional sub-nodes (e.g., func__cond_0, func__cond_0_else) are
    # created by the scanner for if/else branches, but no edge connects
    # the parent function to its conditional sub-nodes. This pass adds
    # edges from parent functions to their conditional children.
    _cond_parent_count = 0
    _COND_SUFFIX_RE = re.compile(r'^(.+)__cond_\d+(_else)?$')
    # O17: Pre-build a parent → [cond_nid, ...] index to avoid the O(N²)
    # nested loop in Strategy 2 below. The index maps every parent_id
    # (the part before __cond_N) to the list of conditional child IDs.
    # This is built once before the main loop and reused for every named
    # conditional node that falls back to source_file matching.
    _cond_children_by_parent: dict = defaultdict(list)
    for _nid in id_registry:
        _m = _COND_SUFFIX_RE.match(_nid)
        if _m:
            _cond_children_by_parent[_m.group(1)].append(_nid)
    for nid, ndata in list(id_registry.items()):
        node_name = ndata.get("name", "")
        # Handle named conditional nodes: <conditional:if(...)>
        if node_name.startswith("<conditional:"):
            # These are standalone conditional nodes. Find the parent by
            # looking at which function contains them.
            # Check if any edge already points to this conditional node
            has_parent_edge = any(True for _ in G.predecessors(nid)) if nid in G else False
            if has_parent_edge:
                continue
            # Strategy 1: Use the __cond_N suffix in the node ID to extract
            # parent (same as non-named conditionals). This works even when
            # source_file is empty (common for scanner-created nodes).
            m = _COND_SUFFIX_RE.match(nid)
            if m:
                parent_id = m.group(1)
                if parent_id in G and not G.has_edge(parent_id, nid):
                    parent_name = id_registry.get(parent_id, {}).get("name", parent_id)
                    G.add_edge(parent_id, nid,
                               call_order=None,
                               call_condition="",
                               concurrency="conditional_entry",
                               confidence="EXTRACTED",
                               source="conditional_parent",
                               confidence_score=0.95,
                               preproc_condition="",
                               preproc_alive=True,
                               evidence=f"conditional_parent: {parent_name} -> {node_name}")
                    _cond_parent_count += 1
                    continue
            # Strategy 2: Fallback — try source_file matching for nodes
            # that have a source_file set. Uses the pre-built
            # _cond_children_by_parent index instead of a nested O(N) scan.
            sf = ndata.get("source_file", "")
            if not sf:
                continue
            # Find the function node whose source_file matches and whose
            # name appears in the conditional expression
            for other_nid, other_data in id_registry.items():
                if other_nid == nid:
                    continue
                other_sf = other_data.get("source_file", "")
                if other_sf != sf:
                    continue
                other_name = other_data.get("name", "")
                if other_name.startswith("<conditional:"):
                    continue
                # Check if this function has a conditional sub-node that
                # matches our conditional expression — use the index.
                for cond_nid in _cond_children_by_parent.get(other_nid, []):
                    cond_name = id_registry[cond_nid].get("name", "")
                    if cond_name == node_name and not G.has_edge(other_nid, nid):
                        G.add_edge(other_nid, nid,
                                   call_order=None,
                                   call_condition="",
                                   concurrency="conditional_entry",
                                   confidence="EXTRACTED",
                                   source="conditional_parent",
                                   confidence_score=0.95,
                                   preproc_condition="",
                                   preproc_alive=True,
                                   evidence=f"conditional_parent: {other_name} -> {node_name}")
                        _cond_parent_count += 1
                        break
            continue
        # Handle __cond_N suffixed nodes: parent is the base function ID
        m = _COND_SUFFIX_RE.match(nid)
        if not m:
            continue
        parent_id = m.group(1)
        if parent_id not in G:
            continue
        # Don't add if edge already exists
        if G.has_edge(parent_id, nid):
            continue
        parent_name = id_registry.get(parent_id, {}).get("name", nid)
        G.add_edge(parent_id, nid,
                   call_order=None,
                   call_condition="",
                   concurrency="conditional_entry",
                   confidence="EXTRACTED",
                   source="conditional_parent",
                   confidence_score=0.95,
                   preproc_condition="",
                   preproc_alive=True,
                   evidence=f"conditional_parent: {parent_name} -> {ndata.get('name', nid)}")
        _cond_parent_count += 1
    if _cond_parent_count:
        print(f"  Conditional node parent edges: {_cond_parent_count} edges")

    # Phase 23: add CONTAINS edges (file → function containment) + build
    # file_nodes index for downstream IMPORTS / prune phases
    from _builder.build.build_phases import (
        _add_contains_edges, _add_imports_edges,
        _label_callback_functions, _label_entry_exit_points,
        _refine_domains, _reclassify_external_domains,
        _prune_isolated_file_nodes,
    )
    file_nodes = _add_contains_edges(G, project_name)

    # Phase 24: add IMPORTS edges for #include relationships between file nodes
    _add_imports_edges(G, file_nodes)

    # Phase 25: label callback functions (CALLBACK_ARG edge targets)
    _label_callback_functions(G)

    # Phase 26: label entry/exit points (in_end / out_end / leaf_func)
    _label_entry_exit_points(G)

    # Phase 27: refine domains via profile domain_rules
    _refine_domains(G, profile)

    # Phase 28: reclassify external / third-party domains
    _reclassify_external_domains(G, profile)

    # Phase 29: prune isolated file nodes (file containers with no edges)
    _prune_isolated_file_nodes(G, file_nodes)

    # Phase 30: Go interface dynamic dispatch (INFERRED). The Go
    # scanner records interface_calls — calls through receivers whose
    # STATIC type is known (params / `var x T`) — and interface nodes
    # with their method sets. Resolve against the whole graph: any
    # type whose method set satisfies the interface's (Go structural
    # satisfaction) is a dispatch target.
    try:
        _dispatch_added = _add_go_interface_dispatch(G)
        if _dispatch_added:
            print(f"[build] Go interface dispatch: {_dispatch_added} "
                  f"INFERRED edge(s)", file=sys.stderr)
    except Exception as _dispatch_err:
        print(f"[build] Go interface dispatch skipped: {_dispatch_err}",
              file=sys.stderr)

    _build_elapsed = time.time() - _build_start
    print(f"[build] Graph built in {_build_elapsed:.0f}s ({G.number_of_nodes()} nodes, {G.number_of_edges()} edges)",
          file=sys.stderr)

    return G, file_nodes



def _add_go_interface_dispatch(G) -> int:
    """INFERRED DISPATCH edges for Go interface-typed call sites.

    Interface nodes carry node_type='interface' + methods=[...]; the
    scanner's interface_calls records {iface, method} from receivers
    with a statically-known type. An edge is emitted to EVERY type
    implementing the method (structural satisfaction — Go interfaces
    are implicit), annotated INFERRED with the interface in
    call_condition so consumers can tell dispatch from direct calls.
    Returns the number of edges added.
    """
    interface_methods = {}   # iface name -> set(methods)
    methods_by_type = {}     # Type name -> {method: node_id}
    for nid, nd in G.nodes(data=True):
        name = nd.get("name", "")
        if not name:
            continue
        if nd.get("node_type") == "interface":
            methods = set(nd.get("methods") or [])
            if methods:
                interface_methods[name] = methods
        elif "." in name and nd.get("node_type") != "file":
            tname, mname = name.rsplit(".", 1)
            methods_by_type.setdefault(tname, {})[mname] = nid
    if not interface_methods or not methods_by_type:
        return 0
    added = 0
    for nid, nd in G.nodes(data=True):
        for call in nd.get("interface_calls") or []:
            iface = call.get("iface", "")
            method = call.get("method", "")
            if iface not in interface_methods:
                continue
            if method not in interface_methods[iface]:
                continue
            # M5: Go structural satisfaction — a type must implement ALL
            # of the interface's methods, not just the one being called.
            required = interface_methods[iface]
            for tname, meths in methods_by_type.items():
                if not required.issubset(set(meths.keys())):
                    continue
                target = meths.get(method)
                if (target and target != nid
                        and not G.has_edge(nid, target)):
                    G.add_edge(
                        nid, target,
                        relation="DISPATCH",
                        call_order=None,
                        call_condition=f"via {iface} (dynamic dispatch)",
                        confidence="INFERRED",
                        confidence_score=0.5,
                        concurrency="dispatch",
                        evidence=f"go interface dispatch: {iface}.{method} -> {tname}.{method}",
                    )
                    added += 1
    return added





def _post_build_auto_enhance(args, outdir: str) -> None:
    """After a successful build, fill empty semantic fields on high-value nodes.

    Always runs the heuristic generator (no LLM required) so the graph
    carries a baseline description for every hub function. If an Anthropic
    API key or Claude Code env var is set, the LLM path is also attempted
    for richer supplements.
    """
    # Warn about low-memory + auto-enhance interaction
    if getattr(args, 'low_memory', False):
        print("[auto-enhance] WARNING: --low-memory strips body_text during "
              "load. Auto-enhance heuristic reads body_text for signal "
              "extraction — on SQLite/LazySQLiteGraph, body_text is "
              "lazy-loaded per node. Enhancement will be slower and "
              "body-derived signals may be incomplete for some nodes.",
              file=sys.stderr)
    import os
    try:
        from _builder.build.auto_enhance import (
            apply_heuristic_enhancement_batch,
        )
        from _builder.graph.graph_build import _load_full_graph
    except ImportError:
        print("[auto-enhance] auto_enhance module unavailable — skipping.",
              file=sys.stderr)
        return

    db_path = os.path.join(outdir, "code2database.db")
    if not os.path.exists(db_path):
        print(f"[auto-enhance] {db_path} not found — skipping.", file=sys.stderr)
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE")
    mode_label = "heuristic" + ("+llm" if api_key else "")

    # Use the efficient batch function — it loads the graph once and writes
    # directly to both JSON-side supplement store and SQLite cgdb_nodes.
    # Heuristic mode is free (no API calls), so always cover the full graph.
    # The LLM path is gated separately and only triggered by an explicit
    # `auto-enhance` CLI invocation with --apply, not by the build hook.
    limit = 100000
    print(f"[auto-enhance] {mode_label}: batch-enhancing up to {limit} node(s)...",
          file=sys.stderr)
    try:
        summary = apply_heuristic_enhancement_batch(outdir, limit=limit)
        enhanced = summary.get("applied", 0)
        print(f"[auto-enhance] heuristic filled fields on {enhanced} node(s) "
              f"(processed={summary.get('processed', 0)}, "
              f"no_signal={summary.get('skipped_no_signal', 0)}, "
              f"builtin={summary.get('skipped_builtin', 0)}).",
              file=sys.stderr)
    except Exception as exc:
        print(f"[auto-enhance] batch enhance failed: {exc}", file=sys.stderr)
    if not api_key:
        print("[auto-enhance] set ANTHROPIC_API_KEY or run `heuristic-enhance --all` "
              "for broader coverage.", file=sys.stderr)

    # Record a "post-enhance" version so time-travel can distinguish
    # pre-enhance vs post-enhance state. The version_id is used as
    # last_seen_version for any nodes whose descriptions were updated.
    # Use the real commit hash (or 'unknown') for commit_hash; the label
    # goes into commit_subject so schema semantics stay correct.
    try:
        from _builder.cgdb.cgdb_versions import VersionController
        vc = VersionController(db_path)
        # source_root is not in scope here; read it from the master JSON
        # written by _build_indexes (the same pattern used in the
        # CODE2DATABASE_SUMMARY refresh block below).
        _post_source_root = ""
        _master_path = os.path.join(outdir, "code2database_master.json")
        if os.path.exists(_master_path):
            import json as _json
            try:
                with open(_master_path, "r", encoding="utf-8") as _mf:
                    _master = _json.load(_mf)
                    _post_source_root = _master.get("source_root", "") or ""
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        post_enhance_hash = _detect_commit_hash(_post_source_root) or "unknown"
        vid = vc.record_version(
            commit_hash=post_enhance_hash,
            commit_subject="post-auto-enhance snapshot",
            force_insert=True,
        )
        vc.close()
        print(f"[auto-enhance] recorded post-enhance version_id={vid}",
              file=sys.stderr)
    except Exception as ve:
        print(f"[auto-enhance] record post-enhance version failed: {ve}",
              file=sys.stderr)

    # Regenerate REVIEW_CHECKLIST.md and CODE2DATABASE_SUMMARY.md so the
    # heuristic-filled nodes appear in the review report and the cgdb layer
    # coverage stats reflect post-enhance state. _build_indexes ran BEFORE
    # auto-enhance, so the initial reports showed 0 heuristic items / 0
    # descriptions. Re-writing them here picks up the supplements we just
    # applied.
    try:
        from _builder.export.index_pack import _write_review_checklist
        from _builder.graph.sqlite_postprocess import (
            _build_callgraph_summary_md_from_sqlite,
        )
        G_refresh = _load_full_graph(outdir)
        _write_review_checklist(outdir, G_refresh)
        print("[auto-enhance] regenerated REVIEW_CHECKLIST.md",
              file=sys.stderr)
        # Regenerate CODE2DATABASE_SUMMARY.md from the SQLite DB so the
        # cgdb layer coverage stats reflect post-enhance state.
        try:
            master_path = os.path.join(outdir, "code2database_master.json")
            source_root_for_summary = ""
            if os.path.exists(master_path):
                import json as _json
                with open(master_path, "r", encoding="utf-8") as _mf:
                    _master = _json.load(_mf)
                    source_root_for_summary = _master.get("source_root", "")
            _build_callgraph_summary_md_from_sqlite(
                db_path, outdir,
                source_root=source_root_for_summary,
                build_info=None,
            )
            print("[auto-enhance] regenerated CODE2DATABASE_SUMMARY.md",
                  file=sys.stderr)
        except Exception as sum_exc:
            print(f"[auto-enhance] CODE2DATABASE_SUMMARY refresh failed: "
                  f"{sum_exc}", file=sys.stderr)
    except Exception as re_exc:
        print(f"[auto-enhance] REVIEW_CHECKLIST refresh failed: {re_exc}",
              file=sys.stderr)



def cmd_build(args):
    """Build invocation graph from extraction JSON."""
    import subprocess
    import tempfile
    import glob
    import gc

    from _builder.profile.entry_scoring import _score_entry_points
    from _builder.build.import_resolve import _resolve_imports
    from _builder.export.index_pack import (
        _mark_endpoint_nodes, _build_indexes, _build_callgraph_summary_md,
        _build_scenarios_file, _build_context_pack,
    )
    from _builder.ops.plugins import _load_plugins, _discover_plugins, _run_plugins
    from _builder.profile.entry_scoring import _detect_processes
    from _builder.token_budget import PipelineTracker, estimate_tokens

    # Initialize memory guard if available
    memory_guard = None
    try:
        from _builder.memory.memory_guard import MemoryGuard, set_global_guard
        warn_thresh = getattr(args, 'memory_warn_threshold', 0.75)
        crit_thresh = getattr(args, 'memory_crit_threshold', 0.85)
        # Forward the absolute-MB caps and dynamic toggle the CLI exposes.
        # Without these, --memory-warn-mb / --memory-crit-mb / --no-memory-dynamic
        # were accepted and forwarded (make_cmd) but silently ignored by the
        # build (the phase with the largest memory footprint).
        warn_mb = getattr(args, 'memory_warn_mb', None)
        crit_mb = getattr(args, 'memory_crit_mb', None)
        dynamic = getattr(args, 'memory_dynamic', True)
        memory_guard = MemoryGuard(
            warn_threshold=warn_thresh,
            crit_threshold=crit_thresh,
            warn_threshold_mb=warn_mb,
            crit_threshold_mb=crit_mb,
            dynamic=dynamic,
            stats_file=getattr(args, 'memory_stats', None),
        )

        # More aggressive memory management for large projects
        if getattr(args, 'large_project', False) or getattr(args, 'low_memory', False):
            memory_guard.batch_reduction_factor = 0.3
            memory_guard.gc_interval = 1.0

        set_global_guard(memory_guard)
        memory_guard.start_monitoring(interval=10.0)
        _cap_desc = ""
        if warn_mb is not None or crit_mb is not None:
            _cap_desc = f", caps={warn_mb}MB/{crit_mb}MB"
        if not dynamic:
            _cap_desc += ", dynamic=off"
        print(f"[MemoryGuard] Started monitoring (warn={warn_thresh*100:.0f}%, "
              f"crit={crit_thresh*100:.0f}%{_cap_desc})", file=sys.stderr)
    except ImportError:
        print("Warning: memory_guard module not available, memory management disabled", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Failed to initialize memory guard: {e}", file=sys.stderr)

    tracker = PipelineTracker()
    extraction_path = args.extraction
    outdir = args.outdir

    # Determine whether to strip body_text during loading
    _low_memory = getattr(args, 'low_memory', False)
    _strip_body_on_load = _low_memory  # Strip during load to reduce peak memory

    # Stage: load extraction
    # Check for split extraction directory first (extraction.json.d/)
    _split_dir = extraction_path + ".d"
    tracker.begin("load_extraction")
    if os.path.isdir(_split_dir):
        # Split extraction format: load per-domain files incrementally
        print(f"[build] Loading split extraction from {_split_dir}"
              f"{' (stripping body_text)' if _strip_body_on_load else ''}", file=sys.stderr)
        data = _load_split_extraction(_split_dir, strip_body_text=_strip_body_on_load)
        source_root = data.get("source_root", "") or getattr(args, "source", "") or ""
        extraction_tokens = 0  # Skip token estimation for large files
    elif os.path.isdir(extraction_path):
        data = _load_split_extraction(extraction_path, strip_body_text=_strip_body_on_load)
        source_root = data.get("source_root", "") or getattr(args, "source", "") or ""
        extraction_tokens = 0
    else:
        data, extraction_tokens = _load_extraction_chunked(extraction_path, memory_guard)
        source_root = data.get("source_root", "") or getattr(args, "source", "") or ""
    tracker.end(output_tokens=extraction_tokens,
                extra={"functions": len(data.get("functions", [])),
                       "edges": len(data.get("edges", []))})

    # Memory info after loading extraction
    if memory_guard:
        memory_guard.check_and_adapt()
        info = memory_guard.get_memory_info()
        print(f"[MemoryGuard] After loading extraction: {info.get('usage_percent', 0)*100:.1f}% memory used, "
              f"{info.get('used_mb', 0):.0f}MB", file=sys.stderr)

    # Pre-strip state_access extraction: derive fields_read/fields_written/
    # globals_read/globals_written from body_text BEFORE any proactive
    # body_text stripping happens below. Without this, large projects
    # (kernel, SPDK, etc.) end up with empty field_access and global_access
    # tables because body_text is dropped for memory savings before
    # _extract_state_access_all runs (later, at line ~6538, after the graph
    # is built — at which point body_text is gone from nodes).
    # The per-domain path in _load_split_extraction handles the
    # strip_body_text=True case, but the proactive stripping paths here
    # (memory >70% or functions >100K) bypass that, so we must extract
    # state_access explicitly here.
    _globals_data_sa = data.get("globals", {})
    _field_assignments_sa = data.get("field_assignments", [])
    _global_var_names_sa = {}
    for _gv in _globals_data_sa.get("global_vars", []):
        _gname = _gv.get("name", "")
        if _gname:
            _global_var_names_sa[_gname] = _gv
    _cached_globals_sa = None
    if _global_var_names_sa:
        _ASSIGN_OPS_sa = re.compile(
            r'\b(' + '|'.join(re.escape(gn) for gn in
                              sorted(_global_var_names_sa.keys(),
                                     key=len, reverse=True))
            + r')\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*[^=]'
        )
        _cached_globals_sa = {
            "var_names": _global_var_names_sa,
            "var_names_keys": set(_global_var_names_sa.keys()),
            "assign_ops_re": _ASSIGN_OPS_sa,
        }
    _sa_extracted_count = 0
    _sa_candidate_count = 0
    # Build the list of candidates (functions with body_text) once.
    _sa_candidates = [(i, _func) for i, _func in enumerate(data.get("functions", []))
                       if _func.get("body_text", "")]

    # Decide sequential vs parallel for pre-strip state_access extraction.
    # This loop runs on ALL functions (1.5M on kernel) BEFORE graph
    # construction. It's a serial for-loop bottleneck when --jobs > 1.
    # When parallel_mode='process', use ProcessPoolExecutor with spawn +
    # initializer-passed context (fork would COW-map the whole extraction
    # payload into every worker). Otherwise, fall back to ThreadPoolExecutor
    # (re.finditer releases GIL during matching, giving modest speedup) or
    # sequential.
    _sa_parallel_mode = getattr(args, 'parallel_mode', None) or 'thread'
    try:
        from _builder.build.parallel import resolve_jobs
        _sa_workers = resolve_jobs(getattr(args, 'jobs', 0),
                                   max_workers_cap=getattr(args, 'max_workers', 0))
    except ImportError:
        _sa_workers = 1

    _use_sa_process = (_sa_parallel_mode == "process" and
                        _sa_workers > 1 and len(_sa_candidates) > 1000)

    if _use_sa_process:
        # ProcessPoolExecutor path: module-level _proc_pre_strip_state_access
        # worker + _pre_strip_worker_init initializer. The pool uses the
        # spawn start method: the parent process holds the extraction
        # payload (data["functions"] etc. — tens of GB on kernel-scale
        # projects) and fork() would map all of it copy-on-write into
        # every worker; the first write in each worker then triggers page
        # copies multiplied by N workers (OOM). Spawn children receive
        # the globals/field_assignments/cached-regex context once each
        # via initargs instead.
        try:
            from concurrent.futures import ProcessPoolExecutor
            import multiprocessing as _mp
            _ctx = _mp.get_context("spawn")
            with ProcessPoolExecutor(
                    max_workers=_sa_workers, mp_context=_ctx,
                    initializer=_pre_strip_worker_init,
                    initargs=(_globals_data_sa, _field_assignments_sa,
                              _cached_globals_sa)) as _pool:
                _sa_results = list(_pool.map(
                    _proc_pre_strip_state_access,
                    _sa_candidates,
                    chunksize=max(1, len(_sa_candidates) // (_sa_workers * 4)),
                ))
            # Merge results back into data["functions"]
            for _idx, _access_info in _sa_results:
                if _access_info is None:
                    continue
                _func = data["functions"][_idx]
                _had_any = False
                for _ak in ("globals_read", "globals_written",
                            "fields_read", "fields_written"):
                    _av = _access_info.get(_ak, [])
                    if _av:
                        _func[_ak] = _av
                        _had_any = True
                if _had_any:
                    _sa_extracted_count += 1
            _sa_candidate_count = len(_sa_candidates)
        except (ImportError, OSError, BrokenPipeError):
            _use_sa_process = False  # fall through to sequential

    if not _use_sa_process:
        # Sequential path (or ThreadPool fallback)
        for _idx, _func in _sa_candidates:
            _body = _func.get("body_text", "")
            if not _body:
                continue
            _sa_candidate_count += 1
            _access_info = _extract_state_access(
                _body,
                _func.get("local_vars", []),
                _func.get("params", []),
                _globals_data_sa,
                _field_assignments_sa,
                _func.get("name", ""),
                _cached_globals=_cached_globals_sa)
            _had_any = False
            for _ak in ("globals_read", "globals_written",
                        "fields_read", "fields_written"):
                _av = _access_info.get(_ak, [])
                if _av:
                    _func[_ak] = _av
                    _had_any = True
            if _had_any:
                _sa_extracted_count += 1
    if _sa_candidate_count:
        print(f"[build] Pre-strip state_access: extracted from "
              f"{_sa_extracted_count}/{_sa_candidate_count} function(s) "
              f"with body_text", file=sys.stderr)
    # Release the per-build cache before stripping body_text
    del _cached_globals_sa, _global_var_names_sa

    # Proactively strip body_text when memory is tight
    # body_text is the largest field and is only needed at query time
    # (where it can be re-read from source files). State_access has been
    # extracted above, so stripping body_text no longer loses that info.
    if memory_guard:
        if info["usage_percent"] > 0.70 and not _strip_body_on_load:
            dropped = memory_guard.drop_body_text(data.get("functions", []))
            if dropped:
                print(f"[MemoryGuard] Stripped body_text from {dropped} functions "
                      f"to save memory", file=sys.stderr)

    # EARLY body_text stripping for large graphs:
    # For graphs with >100K functions, strip body_text before graph construction
    # to save ~4-8GB of memory. body_text is only needed at query time
    # (can be re-read from source or from SQLite after export).
    # With --low-memory, body_text was already stripped during loading.
    _func_count = len(data.get("functions", []))
    if _func_count > 100000 and not _strip_body_on_load:
        dropped = 0
        for func in data.get("functions", []):
            if "body_text" in func:
                del func["body_text"]
                dropped += 1
        gc.collect()
        if dropped:
            print(f"[MemoryGuard] Pre-build: stripped body_text from {dropped} functions "
                  f"({_func_count} total, >100K threshold)", file=sys.stderr)
    elif _strip_body_on_load:
        print(f"[MemoryGuard] body_text already stripped during load "
              f"({_func_count} functions)", file=sys.stderr)

    # Detect build system and resolve macros
    tracker.begin("detect_build_system")
    build_config = getattr(args, "build_config", None)
    macros_arg = getattr(args, "macros", None)
    build_info, macro_bindings = _detect_build_system(source_root, build_config, macros_arg)
    tracker.end()

    # If macro_bindings found, skip re-scan for large extractions.
    # Re-scanning is too expensive for large projects (>50K functions).
    # Instead, macro bindings are stored for use during build (dead-code marking).
    # Also skip re-scan when the extraction already carries cgdb semantic records
    # (clang backend) — re-scanning with tree-sitter would discard CFG/data-flow
    # layers and lose compile_commands context.
    _has_cgdb = bool(data.get("cgdb_nodes")) or bool(data.get("cgdb_edges"))
    if macro_bindings and _has_cgdb:
        print(f"[build] Skipping macro rescan (extraction carries cgdb semantic records). "
              f"Macro bindings saved for dead-code marking.", file=sys.stderr)
    elif macro_bindings and not _has_cgdb:
        n_funcs = len(data.get("functions", [])) if isinstance(data, dict) else 0
        if data.get("_split_dir") and os.path.isdir(data["_split_dir"]):
            n_funcs = data.get("_function_count", 0)
        # Only re-scan for small projects (<50K functions)
        if n_funcs > 0 and n_funcs < 50000:
            tracker.begin("macro_rescan")
            scanner_script = os.path.join(os.path.dirname(__file__), "..", "..", "code2database_scanner.py")
            if not os.path.exists(scanner_script):
                scanner_script = os.path.join(os.path.dirname(__file__), "..", "code2database_scanner.py")
            # Build macros_str from macro_bindings. When the macro set is
            # large (kernel-scale: thousands of CONFIG_*), the joined string
            # can exceed Linux ARG_MAX (~128KB) and crash subprocess.run.
            # Threshold: if joined length > 8KB, write to a tempfile and use
            # --macros-from instead.
            macros_str = " ".join(f"-D{k}={v}" if v else f"-D{k}" for k, v in macro_bindings.items())
            macros_from_path = None
            macros_from_fd = None
            scan_cmd_macros = []
            if len(macros_str) > 8192:
                import tempfile
                macros_from_fd, macros_from_path = tempfile.mkstemp(
                    prefix="code2db_macros_", suffix=".txt")
                try:
                    with os.fdopen(macros_from_fd, "w", encoding="utf-8") as f:
                        for k, v in macro_bindings.items():
                            if v:
                                f.write(f"{k}={v}\n")
                            else:
                                f.write(f"{k}\n")
                    macros_from_fd = -1  # ownership transferred to file
                    scan_cmd_macros = ["--macros-from", macros_from_path]
                except OSError:
                    # Fallback to inline --macros (risky but better than nothing)
                    if macros_from_path and os.path.exists(macros_from_path):
                        try:
                            os.remove(macros_from_path)
                        except OSError:
                            logging.getLogger(__name__).debug("silent exception", exc_info=True)
                            pass
                    macros_from_path = None
                    scan_cmd_macros = ["--macros", macros_str]
            else:
                scan_cmd_macros = ["--macros", macros_str]
            re_scan = os.path.join(outdir, ".code2database_rescan.json")
            os.makedirs(outdir, exist_ok=True)
            try:
                scan_result = subprocess.run(
                    [sys.executable, scanner_script, "scan",
                     "--source", source_root, "--output", re_scan]
                    + scan_cmd_macros
                    + ["--no-interactive"],
                    capture_output=True, text=True,
                    stdin=subprocess.DEVNULL
                )
            finally:
                # Cleanup tempfile
                if macros_from_path and os.path.exists(macros_from_path):
                    try:
                        os.remove(macros_from_path)
                    except OSError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                if macros_from_fd and macros_from_fd != -1:
                    try:
                        os.close(macros_from_fd)
                    except OSError:
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
            if scan_result.returncode == 0:
                data = json.loads(Path(re_scan).read_text(encoding="utf-8"))
                print("Re-scanned with macro bindings")
            else:
                print(f"Warning: re-scan with macros failed, using original extraction: "
                      f"{scan_result.stderr.strip()}", file=sys.stderr)
            tracker.end_with_files([re_scan])
        else:
            print(f"[build] Skipping macro rescan ({n_funcs} functions is too large). "
                  f"Macro bindings saved for dead-code marking.", file=sys.stderr)

    # Load profile BEFORE build_graph so that external lib prefix filtering
    # takes effect during callee resolution inside build_graph().
    tracker.begin("load_profile")
    builder_profile = None
    profile_path = getattr(args, 'profile', None)
    if profile_path:
        from _profile import ProfileSchema
        p = ProfileSchema.load(profile_path)
        builder_profile = p.to_builder_config()

        # Wire external lib prefixes into callee resolution so that
        # calls to profiled external lib prefixes are skipped rather than
        # creating unresolved external edges.
        # Use builder_profile (flat dict from to_builder_config()) which
        # consolidates all prefixes in lib_prefix_map.
        from _builder.utils import set_external_lib_prefixes
        lib_prefix_map = builder_profile.get("lib_prefix_map", {})
        all_ext_prefixes = [k.lower() for k in lib_prefix_map]
        if all_ext_prefixes:
            set_external_lib_prefixes(all_ext_prefixes)
            print(f"External lib prefixes loaded: {len(all_ext_prefixes)} (from lib_prefix_map)")

        # Populate the module-level _ALLOCATION_SITES_MAP
        # from the profile's `allocation_sites` list. This lets
        # _trace_object_origin (called by _extract_state_access for each
        # field write/read) annotate origins with object_type without
        # threading the profile through every call site.
        global _ALLOCATION_SITES_MAP
        _ALLOCATION_SITES_MAP = {}
        for entry in builder_profile.get("allocation_sites", []):
            fn = entry.get("function", "")
            ot = entry.get("object_type", "")
            if fn and ot:
                _ALLOCATION_SITES_MAP[fn] = ot
        if _ALLOCATION_SITES_MAP:
            print(f"Allocation sites loaded: {len(_ALLOCATION_SITES_MAP)} (from allocation_sites)")
    tracker.end()

    # Load and run plugins
    plugin_paths = list(getattr(args, "plugin", []) or [])
    auto_plugins = _discover_plugins(source_root)
    plugin_paths.extend(auto_plugins)
    plugins = _load_plugins(plugin_paths, source_root)

    # Determine storage type early — if sqlite+low_memory, use StreamingGraph
    # to avoid holding the entire NetworkX graph in RAM.
    _use_streaming = (getattr(args, 'storage', 'auto') in ('sqlite', 'auto')
                      and getattr(args, 'low_memory', False))
    _streaming_graph = None

    # Graph construction
    tracker.begin("build_graph", metadata={
        "source_files": len(data.get("functions", [])),
        "source_bytes": extraction_tokens * 4,  # reverse estimate
    })

    if _use_streaming:
        # Streaming path: create SQLite DB early and stream nodes/edges to it
        from _builder.graph.streaming_graph import StreamingGraph
        db_path = os.path.join(outdir, "code2database.db")
        os.makedirs(outdir, exist_ok=True)
        _streaming_graph = StreamingGraph(db_path)
        print(f"[build] Using StreamingGraph → {db_path} (low-memory mode)",
              file=sys.stderr)
        if plugins:
            print(f"Loaded {len(plugins)} plugin(s): {', '.join(n for n, _ in plugins)}")
            data = dict(data)
            G, file_nodes = build_graph(data, profile=builder_profile,
                                        graph=_streaming_graph)
            # Note: plugins may need nx.DiGraph — skip for streaming mode
            if plugins:
                print("[build] Warning: plugins not supported in streaming mode, skipping",
                      file=sys.stderr)
        else:
            G, file_nodes = build_graph(data, profile=builder_profile,
                                        graph=_streaming_graph)

        # Flush remaining buffered data to SQLite
        _streaming_graph.flush_all()
        print(f"[build] StreamingGraph: {_streaming_graph.number_of_nodes()} nodes, "
              f"{_streaming_graph.number_of_edges()} edges written to SQLite",
              file=sys.stderr)

        # Free the extraction data dict — it's no longer needed since
        # all data is now in StreamingGraph's id_registry + SQLite.
        # This reclaims ~10-15GB for large codebases.
        del data
        gc.collect()
        if memory_guard:
            info = memory_guard.get_memory_info()
            print(f"[MemoryGuard] After freeing extraction data: "
                  f"{info.get('usage_percent', 0)*100:.1f}% memory, "
                  f"{info.get('used_mb', 0):.0f}MB", file=sys.stderr)
    elif plugins:
        print(f"Loaded {len(plugins)} plugin(s): {', '.join(n for n, _ in plugins)}")
        data = dict(data)
        G, file_nodes = build_graph(data, profile=builder_profile)
        G = _run_plugins(plugins, G, data)
    else:
        G, file_nodes = build_graph(data, profile=builder_profile)
    tracker.end(extra={"nodes": G.number_of_nodes(), "edges": G.number_of_edges()})

    # Persist the builder profile to the output directory so that downstream
    # query commands (detect-races, concurrency-analyze, io-path, etc.) can
    # load project-specific patterns (lock APIs, IO keywords, test segments,
    # non-API paths, vendor prefixes) without requiring --profile to be
    # re-specified. This is essential for the "code database" goal: the LLM
    # should be able to query the graph and get accurate, profile-driven
    # answers from the persisted database alone.
    if builder_profile:
        try:
            _persisted_profile_path = os.path.join(outdir, ".code2database_profile.json")
            with open(_persisted_profile_path, "w", encoding="utf-8") as _pf:
                json.dump(builder_profile, _pf, ensure_ascii=False, indent=2)
            print(f"[build] Profile persisted to {_persisted_profile_path}",
                  file=sys.stderr)
        except (IOError, OSError) as _e:
            print(f"[build] Warning: failed to persist profile: {_e}",
                  file=sys.stderr)

    # ── Streaming SQLite early-exit path ──
    # When using StreamingGraph, all data is already in SQLite.
    # Skip NetworkX-heavy post-processing and go straight to
    # SQLite-based index/doc generation.
    if _use_streaming and _streaming_graph is not None:
        try:
            _node_count = _streaming_graph.number_of_nodes()
            _edge_count = _streaming_graph.number_of_edges()

            # Memory check after streaming build
            if memory_guard:
                memory_guard.check_and_adapt()
                info = memory_guard.get_memory_info()
                print(f"[MemoryGuard] After streaming build: {info.get('usage_percent', 0)*100:.1f}% memory, "
                      f"{info.get('used_mb', 0):.0f}MB", file=sys.stderr)

            # Write communities as domain-based (no Leiden for streaming mode)
            comm_path = os.path.join(outdir, ".code2database_communities.json")
            _domain_communities = {}
            _node_community = {}
            for nid, ndata in _streaming_graph.nodes(data=True):
                domain = ndata.get("domain", "root")
                if domain not in _domain_communities:
                    _domain_communities[domain] = {"id": domain, "label": domain,
                                                    "node_ids": []}
                _domain_communities[domain]["node_ids"].append(nid)
                _node_community[nid] = domain
            with open(comm_path, "w", encoding="utf-8") as _cf:
                json.dump({"total_communities": len(_domain_communities),
                           "communities": list(_domain_communities.values()),
                           "node_community": _node_community},
                          _cf, ensure_ascii=False, separators=(',', ':'))
            # Write communities to SQLite
            _streaming_graph._store.store_communities(list(_domain_communities.values()))
            del _domain_communities, _node_community
            gc.collect()

            # Store entry scores (lightweight — from id_registry only)
            from _builder.profile.entry_scoring import _score_entry_points_lightweight
            entry_scores = _score_entry_points_lightweight(
                _streaming_graph.id_registry, profile=builder_profile)
            if entry_scores:
                score_list = [{"id": nid, "name": ndata.get("name", ""),
                               "score": score, "domain": ndata.get("domain", "")}
                              for nid, (ndata, score) in entry_scores.items()]
                _streaming_graph._store.store_entry_scores(score_list)
                ep_count = len(score_list)
            else:
                ep_count = 0

            # Write domain stats
            _domain_groups = {}
            for nid, ndata in _streaming_graph.nodes(data=True):
                dom = ndata.get("domain", "root")
                _domain_groups.setdefault(dom, 0)
                _domain_groups[dom] += 1
            for domain, count in _domain_groups.items():
                _streaming_graph._store.store_domain_stats(domain, {"funcs": count, "domain": domain})
            del _domain_groups

            # Store endpoints
            ep_path = os.path.join(outdir, ".code2database_endpoints.json")
            _endpoints = []
            for nid, ndata in _streaming_graph.nodes(data=True):
                if ndata.get("labels") and "out_end" in ndata.get("labels", []):
                    _endpoints.append({"id": nid, "name": ndata.get("name", ""),
                                       "domain": ndata.get("domain", "")})
            Path(ep_path).write_text(
                json.dumps(_endpoints, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        finally:
            # Always flush and close StreamingGraph — even on exception.
            # Without this, an exception in any of the above steps (build
            # failure, store_communities error, disk full, etc.) would
            # leak the underlying SQLiteStore connection for the lifetime
            # of the process and block subsequent writers.
            _streaming_graph.close()
            # Free id_registry — biggest remaining in-memory structure
            del _streaming_graph
            gc.collect()

        if memory_guard:
            info = memory_guard.get_memory_info()
            print(f"[MemoryGuard] After freeing streaming structures: "
                  f"{info.get('usage_percent', 0)*100:.1f}% memory, "
                  f"{info.get('used_mb', 0):.0f}MB", file=sys.stderr)

        # Build indexes from SQLite
        tracker.begin("build_indexes")
        print(f"[build] Building indexes from SQLite... ({_node_count} nodes)",
              file=sys.stderr)
        _indexes_start = time.time()
        _build_indexes_from_sqlite(db_path, outdir)
        index_files = glob.glob(os.path.join(outdir, ".code2database_*.json"))
        tracker.end_with_files(index_files)
        print(f"[build] Indexes built in {time.time() - _indexes_start:.0f}s",
              file=sys.stderr)

        # Generate docs from SQLite — parallelize 5 independent generators.
        # Each opens its own SQLite connection (WAL mode supports concurrent
        # reads). Previously sequential, costing ~409s on kernel; parallel
        # should cut wall-clock to the slowest generator (~150-200s).
        tracker.begin("generate_docs")
        print(f"[build] Generating docs (parallel)...", file=sys.stderr)
        _docs_start = time.time()
        from concurrent.futures import ThreadPoolExecutor, as_completed
        _doc_futures = {}
        with ThreadPoolExecutor(max_workers=5) as _doc_pool:
            _doc_futures[_doc_pool.submit(
                _build_callgraph_summary_md_from_sqlite,
                db_path, outdir, source_root, build_info=build_info)] = "summary"
            _doc_futures[_doc_pool.submit(
                _build_domain_readmes_from_sqlite,
                db_path, outdir)] = "readmes"
            _doc_futures[_doc_pool.submit(
                _build_scenarios_file_from_sqlite,
                db_path, outdir, build_info=build_info,
                builder_profile=builder_profile)] = "scenarios"
            _doc_futures[_doc_pool.submit(
                _build_architecture_flows_from_sqlite,
                db_path, outdir, source_root, build_info=build_info)] = "arch_flows"
            _doc_futures[_doc_pool.submit(
                _build_context_pack_from_sqlite,
                db_path, outdir, source_root, build_info=build_info)] = "context_pack"
            # Collect results (log failures but don't abort other generators)
            summary_path = None
            pack_path = None
            for _fut in as_completed(_doc_futures):
                _label = _doc_futures[_fut]
                try:
                    _result = _fut.result()
                    if _label == "summary":
                        summary_path = _result
                    elif _label == "context_pack":
                        pack_path = _result
                except Exception as _exc:
                    logging.getLogger(__name__).error(
                        "Doc generator '%s' failed: %s", _label, _exc,
                        exc_info=True)
        doc_files = [os.path.join(outdir, "CODE2DATABASE_SUMMARY.md")]
        doc_files += glob.glob(os.path.join(outdir, "domains/*/README.md"), recursive=True)
        tracker.end_with_files(doc_files)
        print(f"[build] Docs generated in {time.time() - _docs_start:.0f}s",
              file=sys.stderr)

        # Write build config
        if build_info:
            bc_path = os.path.join(outdir, ".code2database_build_config.json")
            Path(bc_path).write_text(
                json.dumps(build_info, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

        print(f"Built invocation graph: {_node_count} nodes, {_edge_count} edges")
        print(f"Domain files written to {outdir}/")
        print(f"Summary: {summary_path}")
        print(f"Context pack: {pack_path}")
        if ep_count > 0:
            print(f"Endpoints: {ep_count} external endpoint(s) marked as out_end")

        # Pipeline stats
        report = tracker.write_report(outdir)
        stats_path = os.path.join(outdir, ".code2database_pipeline_stats.json")
        Path(stats_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        total = report["summary"]
        comp = report.get("comparison", {})
        print(f"Pipeline stats: {total['total_stages']} stages, "
              f"{total['total_elapsed_sec']:.1f}s elapsed, "
              f"{total['total_output_tokens']} output tokens "
              f"(raw source ~{comp.get('raw_source_tokens_estimate', 0)} tokens, "
              f"savings {comp.get('savings_ratio', 'N/A')})",
              file=sys.stderr)

        # Stop memory guard
        if memory_guard:
            memory_guard.stop_monitoring()
            stats = memory_guard.get_stats()
            if stats.get('peak_memory_mb', 0) > 0:
                print(f"[MemoryGuard] Stats: peak={stats['peak_memory_mb']:.0f}MB, "
                      f"gc={stats['gc_count']}, criticals={stats['criticals']}, "
                      f"warnings={stats['warnings']}", file=sys.stderr)

        # Optional post-build auto-enhancement (streaming SQLite path).
        if getattr(args, 'auto_enhance', False):
            _post_build_auto_enhance(args, outdir)

        return  # Early return — all work done via streaming SQLite path

    # Post-build_graph memory check: strip body_text if memory is tight
    if memory_guard:
        memory_guard.check_and_adapt()
        info = memory_guard.get_memory_info()
        print(f"[MemoryGuard] After build_graph: {info.get('usage_percent', 0)*100:.1f}% memory, "
              f"{info.get('used_mb', 0):.0f}MB", file=sys.stderr)
        if info["usage_percent"] > 0.65:
            dropped = 0
            for nid in G.nodes():
                ndata = G.nodes[nid]
                if ndata.get("body_text"):
                    del ndata["body_text"]
                    dropped += 1
            if dropped:
                gc.collect()
                print(f"[MemoryGuard] Stripped body_text from {dropped} graph nodes", file=sys.stderr)

    # Thread model detection and propagation
    tracker.begin("detect_thread_models")
    thread_models = _detect_thread_models(G, builder_profile=builder_profile,
                                            jobs=getattr(args, 'jobs', 0),
                                            max_workers=getattr(args, 'max_workers', 0),
                                            parallel_mode=getattr(args, 'parallel_mode', None) or 'thread',
                                            explicit_parallel_mode=getattr(args, 'parallel_mode', None) is not None)
    if thread_models:
        inherited_count = _propagate_thread_models(G, thread_models)
        model_counts = {}
        for model in thread_models.values():
            model_counts[model] = model_counts.get(model, 0) + 1
        model_str = ', '.join(f'{k}={v}' for k, v in sorted(model_counts.items()))
        print(f"Thread models: {len(thread_models)} entry point(s) "
              f"({model_str}), {inherited_count} inherited")
    tracker.end(extra={"thread_entries": len(thread_models)})

    # Shared state access extraction
    tracker.begin("extract_state_access")
    _extract_state_access_all(G, data, jobs=getattr(args, 'jobs', 0) or 0,
                              max_workers=getattr(args, 'max_workers', 0) or 0,
                              parallel_mode=getattr(args, 'parallel_mode', None) or 'thread',
                              explicit_parallel_mode=getattr(args, 'parallel_mode', None) is not None)
    state_count = sum(1 for _, nd in G.nodes(data=True)
                      if nd.get("globals_read") or nd.get("globals_written")
                      or nd.get("fields_read") or nd.get("fields_written"))
    if state_count:
        print(f"State access: {state_count} function(s) with shared state access")
    tracker.end(extra={"state_access_nodes": state_count})

    # Extract needed references before freeing extraction data
    # After graph construction, only import_edges, globals, and
    # vtable_registrations are needed from the extraction dict.
    scanner_import_edges = data.get("import_edges", [])
    globals_data = data.get("globals", {})
    vtable_regs_data = data.get("vtable_registrations", [])
    macro_regs_data = data.get("macro_registrations", [])
    token_paste_data = data.get("token_paste_functions", [])
    container_of_data = data.get("container_of_usages", [])
    conversion_data = data.get("conversion_funcs", [])
    struct_defs_data = data.get("struct_defs", [])
    fn_ptr_calls_data = data.get("fn_ptr_calls", {})
    passthrough_data = data.get("passthrough_reg_funcs", {})
    field_assignments_data = data.get("field_assignments", [])
    # cgdb 13-layer records — preserve references before freeing extraction dict
    # so the SQLite export below can write cgdb_nodes/cgdb_types/cgdb_edges/
    # cgdb_invoke_sites to the cgdb schema tables.
    cgdb_nodes_data = data.get("cgdb_nodes", [])
    cgdb_types_data = data.get("cgdb_types", [])
    cgdb_edges_data = data.get("cgdb_edges", [])
    cgdb_invoke_sites_data = data.get("cgdb_invoke_sites", [])
    cgdb_predicates_data = data.get("cgdb_predicates", [])
    cgdb_ops_bindings_data = data.get("cgdb_ops_bindings", [])
    cgdb_basic_blocks_data = data.get("cgdb_basic_blocks", [])
    cgdb_cfg_edges_data = data.get("cgdb_cfg_edges", [])
    cgdb_data_flow_data = data.get("cgdb_data_flow", [])
    cgdb_sync_primitives_data = data.get("cgdb_sync_primitives", [])
    cgdb_happens_before_data = data.get("cgdb_happens_before", [])
    cgdb_alias_sets_data = data.get("cgdb_alias_sets", [])
    cgdb_doc_comments_data = data.get("cgdb_doc_comments", [])
    cgdb_metadata_data = data.get("cgdb_metadata", [])
    cgdb_includes_data = data.get("cgdb_includes", [])
    conditions_data = data.get("conditions", [])
    # Free the massive extraction dict
    del data
    gc.collect()

    os.makedirs(outdir, exist_ok=True)

    # Endpoint marking
    tracker.begin("mark_endpoints")
    # Pass the CURRENT build's vtable registrations: the file at
    # .code2database_vtables.json is only written ~270 lines below, so on a
    # first build _mark_endpoint_nodes previously found no file (all vtable
    # callback endpoints skipped) and on rebuilds it read the PREVIOUS
    # build's stale file.
    ep_count = _mark_endpoint_nodes(G, outdir, profile=builder_profile,
                                    vtable_regs=vtable_regs_data)
    tracker.end_with_files([os.path.join(outdir, ".code2database_endpoints.json")],
                           extra={"endpoints": ep_count})

    # Community detection (Leiden algorithm)
    # Pre-check memory: community detection can be very memory-intensive for large graphs
    if memory_guard:
        memory_guard.check_and_adapt()
        info = memory_guard.get_memory_info()
        print(f"[MemoryGuard] Before community detection: {info.get('usage_percent', 0)*100:.1f}% memory, "
              f"{info.get('used_mb', 0):.0f}MB", file=sys.stderr)
        if info["usage_percent"] > 0.80:
            print("[MemoryGuard] WARNING: Memory too high for community detection, "
                  "using lightweight domain-based fallback", file=sys.stderr)
    tracker.begin("detect_communities")
    skip_community = getattr(args, 'skip_community', False) or getattr(args, 'low_memory', False)
    if skip_community:
        print("[build] Skipping community detection (--skip-community/--low-memory)", file=sys.stderr)
        comm_result = CommunityResult()
        # Build minimal node_community from domains for downstream compatibility
        for nid, ndata in G.nodes(data=True):
            if not ndata.get("is_empty", False):
                domain = ndata.get("domain", "root")
                comm_result.node_community[nid] = domain
    else:
        comm_result = detect_communities(G, source_root)
    for nid, comm_id in comm_result.node_community.items():
        if nid in G:
            G.nodes[nid]["community_id"] = comm_id
    if comm_result.communities:
        comm_path = os.path.join(outdir, ".code2database_communities.json")
        # For large graphs, write node_community as a compact list instead of
        # a dict to reduce JSON size and serialization memory.
        _nc = comm_result.node_community
        if len(_nc) > 100000:
            # Stream write: communities + domain_overlap (small), then node_community as compact array
            with open(comm_path, "w", encoding="utf-8") as _cf:
                _cf.write('{')
                _cf.write(f'"total_communities": {len(comm_result.communities)}, ')
                _cf.write('"communities": ')
                json.dump(comm_result.communities, _cf, ensure_ascii=False, separators=(',', ':'))
                _cf.write(', "domain_overlap": ')
                json.dump(comm_result.domain_overlap, _cf, ensure_ascii=False, separators=(',', ':'))
                # Write node_community as sorted array of [nid, comm_id] pairs
                _cf.write(', "node_community_list": [')
                _first = True
                for nid in sorted(_nc.keys()):
                    if not _first:
                        _cf.write(',')
                    _first = False
                    json.dump([nid, _nc[nid]], _cf, ensure_ascii=False, separators=(',', ':'))
                _cf.write(']}')
            del _nc
            gc.collect()
        else:
            Path(comm_path).write_text(
                json.dumps({
                    "total_communities": len(comm_result.communities),
                    "communities": comm_result.communities,
                    "node_community": comm_result.node_community,
                    "domain_overlap": comm_result.domain_overlap,
                }, ensure_ascii=False, separators=(',', ':')) + "\n",
                encoding="utf-8")
        print(f"Communities: {len(comm_result.communities)} detected (Leiden)")
    tracker.end_with_files([os.path.join(outdir, ".code2database_communities.json")],
                           extra={"communities": len(comm_result.communities)})

    # Import resolution for C/C++ projects
    tracker.begin("resolve_imports")
    import_edges = _resolve_imports(G, source_root)
    # Also add IMPORTS edges from scanner's #include detection (regex fallback)
    scanner_imports = scanner_import_edges
    if scanner_imports:
        def _domain_for_file(fpath: str) -> str:
            """Derive domain for a file path, handling include/ prefix and bare filenames."""
            from _builder.utils import _derive_domain
            # Try _derive_domain first (same logic as scanner)
            d = _derive_domain(fpath)
            if d and d != 'root':
                return d
            # Fallback: use parent directory name if present
            parts = fpath.replace(os.sep, '/').split('/')
            if len(parts) > 1:
                return parts[0]
            return "root"

        for ie in scanner_imports:
            src = ie.get("source", "")
            tgt = ie.get("target", "")
            if not src or not tgt:
                continue
            # Find file node IDs
            src_id = file_nodes.get(src)
            if not src_id:
                src_id = f"file:{src}"
                if src_id not in G:
                    # Only create source file nodes for files that have extracted
                    # functions (present in file_nodes mapping). Files not in
                    # the mapping have no functions and would become isolated nodes.
                    continue
            tgt_id = None
            # Prefer exact path match, then directory-aware match,
            # then basename-only match as last resort (avoid false positives)
            # Only use endswith for partial paths (containing '/'), not bare basenames
            if '/' in tgt:
                for existing_sf, existing_id in file_nodes.items():
                    if existing_sf.endswith(tgt):
                        tgt_id = existing_id
                        break
            if not tgt_id:
                # Try to find include path in the existing file's directory tree
                # e.g., include "lib/bdev.h" should match "path/to/lib/bdev.h"
                for existing_sf, existing_id in file_nodes.items():
                    # Check if existing_sf contains the include path
                    if '/' + tgt in existing_sf:
                        tgt_id = existing_id
                        break
            if not tgt_id:
                # Basename-only fallback: only match if unique across the whole project
                # to avoid false positives (two dirs with same header name)
                candidates = [(sf, sid) for sf, sid in file_nodes.items()
                              if os.path.basename(sf) == os.path.basename(tgt)]
                if len(candidates) == 1:
                    tgt_id = candidates[0][1]
            if not tgt_id:
                tgt_id = f"file:{tgt}"
                if tgt_id not in G:
                    # External header not in scanned sources: derive domain
                    # from the header path itself (e.g., 'include' for
                    # 'spdk/bdev.h'). Previously inherited source domain which
                    # caused same-domain skip to filter all IMPORTS edges.
                    tgt_domain = _domain_for_file(tgt)
                    # System headers (<...>) get 'external' domain
                    if tgt.startswith('<') or tgt in ('stdio.h', 'stdlib.h',
                            'string.h', 'stdint.h', 'stdbool.h', 'errno.h',
                            'unistd.h', 'fcntl.h') \
               or tgt.startswith(('sys/', 'arpa/')):
                        tgt_domain = "external"
                    G.add_node(tgt_id, name=os.path.basename(tgt), domain=tgt_domain,
                               node_type="file", source_file=tgt, labels=["file"],
                               is_empty=False, auto_created=True)
            if src_id != tgt_id and not G.has_edge(src_id, tgt_id):
                # Skip same-domain IMPORTS only when both files are from
                # scanned sources (not auto-created header placeholders).
                # Auto-created headers may share domain by derivation but
                # still represent meaningful cross-file dependencies.
                src_dom = G.nodes[src_id].get("domain", "") if src_id in G else ""
                tgt_dom = G.nodes[tgt_id].get("domain", "") if tgt_id in G else ""
                tgt_auto = G.nodes[tgt_id].get("auto_created", False) if tgt_id in G else False
                src_auto = G.nodes[src_id].get("auto_created", False) if src_id in G else False
                if (src_dom and tgt_dom and src_dom == tgt_dom
                        and not src_auto and not tgt_auto):
                    continue
                src_name = os.path.basename(src) if src else src_id
                tgt_name = os.path.basename(tgt) if tgt else tgt_id
                G.add_edge(src_id, tgt_id, relation="IMPORTS",
                           concurrency="imports", confidence="EXTRACTED",
                           source="ast", confidence_score=1.0,
                           evidence=f"imports: {src_name} includes {tgt_name}")
                import_edges += 1
    if import_edges > 0:
        print(f"Import resolution: {import_edges} new edge(s) resolved through #include chains")
    tracker.end(extra={"import_edges": import_edges})

    # Entry-point scoring
    tracker.begin("score_entry_points")
    entry_scores = _score_entry_points(G, source_root)
    if entry_scores:
        top_entries = sorted(entry_scores.items(), key=lambda x: x[1], reverse=True)[:10]
        top_names = [(G.nodes[nid].get("name", nid), score) for nid, score in top_entries]
        scores_path = os.path.join(outdir, ".code2database_entry_scores.json")
        # For large graphs, use streaming write to avoid OOM from json.dumps
        if len(entry_scores) > 100000:
            with open(scores_path, "w", encoding="utf-8") as _sf:
                _sf.write('{\n  "total_scored": ' + str(len(entry_scores)) + ',\n')
                _sf.write('  "top_entries": ')
                json.dump([{"id": nid, "name": G.nodes[nid].get("name", ""),
                            "score": score, "domain": G.nodes[nid].get("domain", "")}
                           for nid, score in top_entries],
                          _sf, ensure_ascii=False, indent=2)
                _sf.write(',\n  "entry_points": [')
                _first = True
                for nid, score in entry_scores.items():
                    if not _first:
                        _sf.write(',')
                    _first = False
                    json.dump({"id": nid, "name": G.nodes[nid].get("name", ""),
                               "score": score, "domain": G.nodes[nid].get("domain", "")},
                              _sf, ensure_ascii=False, separators=(',', ':'))
                _sf.write(']\n}\n')
        else:
            Path(scores_path).write_text(
                json.dumps({"total_scored": len(entry_scores),
                            "top_entries": [{"id": nid, "name": G.nodes[nid].get("name", ""),
                                             "score": score, "domain": G.nodes[nid].get("domain", "")}
                                            for nid, score in top_entries],
                            "entry_points": [{"id": nid, "name": G.nodes[nid].get("name", ""),
                                              "score": score, "domain": G.nodes[nid].get("domain", "")}
                                             for nid, score in entry_scores.items()]},
                           ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
    tracker.end_with_files([os.path.join(outdir, ".code2database_entry_scores.json")],
                           extra={"entry_points": len(entry_scores)})

    # Process detection
    tracker.begin("detect_processes")
    processes = _detect_processes(G, entry_scores, comm_result)
    if processes:
        process_path = os.path.join(outdir, ".code2database_processes.json")
        Path(process_path).write_text(
            json.dumps({"total_processes": len(processes), "processes": processes},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"Processes: {len(processes)} execution flow(s) detected")
    tracker.end_with_files([os.path.join(outdir, ".code2database_processes.json")],
                           extra={"processes": len(processes) if processes else 0})

    # Prune isolated file nodes created by import resolution or scanner imports.
    # These are file container nodes with no edges (no CONTAINS, no IMPORTS).
    _pruned_files = 0
    # Collect only file-type node IDs — a small subset of the full graph.
    # list(G.nodes()) would materialize all 700K IDs unnecessarily.
    _file_node_ids = [nid for nid in G.nodes()
                      if G.nodes[nid].get("node_type") == "file"]
    for nid in _file_node_ids:
        if G.degree(nid) == 0:
            G.remove_node(nid)
            _pruned_files += 1
    if _pruned_files:
        print(f"  Pruned isolated file nodes: {_pruned_files}")

    max_per_dir = getattr(args, "max_domain_files", 50) or 0
    # Pre-split memory check
    if memory_guard:
        memory_guard.check_and_adapt()
        info = memory_guard.get_memory_info()
        print(f"[MemoryGuard] Before split_by_domain: {info.get('usage_percent', 0)*100:.1f}% memory, "
              f"{info.get('used_mb', 0):.0f}MB", file=sys.stderr)
    tracker.begin("split_domain")
    master_path = split_by_domain(G, outdir, source_root,
                                  max_per_dir=max_per_dir if max_per_dir > 0 else 999999,
                                  build_info=build_info,
                                  profile=builder_profile)
    domain_files = glob.glob(os.path.join(outdir, "domains/**/*.json"), recursive=True)
    tracker.end_with_files([master_path] + domain_files,
                           extra={"domains": len(comm_result.communities)})

    # Write globals if present in extraction
    # Use streaming write for large globals data to avoid OOM
    if any(globals_data.get(k) for k in ("enums", "constants", "typedefs", "global_vars")):
        globals_path = os.path.join(outdir, ".code2database_globals.json")
        _globals_size = sum(len(v) if isinstance(v, list) else 0
                           for v in globals_data.values())
        if _globals_size > 100000:
            # Streaming write for large globals
            with open(globals_path, "w", encoding="utf-8") as _gf:
                json.dump(globals_data, _gf, ensure_ascii=False, indent=2)
                _gf.write("\n")
        else:
            Path(globals_path).write_text(
                json.dumps(globals_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
    # Free globals data after writing
    del globals_data

    # Write vtable registration index for query-time dispatch resolution
    vtable_regs = vtable_regs_data
    if vtable_regs:
        vtable_path = os.path.join(outdir, ".code2database_vtables.json")
        # Build a structured index: struct_type → {field → [{func_name, var_name, source_file, condition}]}
        vtable_index = defaultdict(lambda: defaultdict(list))
        for vtable in vtable_regs:
            struct_type = vtable.get("struct_type", "")
            if not struct_type:
                continue
            for reg in vtable.get("registrations", []):
                vtable_index[struct_type][reg["field"]].append({
                    "func_name": reg["func_name"],
                    "var_name": vtable.get("var_name", ""),
                    "source_file": vtable.get("source_file", ""),
                    "condition": reg.get("condition", ""),
                })
        Path(vtable_path).write_text(
            json.dumps({
                "struct_types": dict(vtable_index),
                "total_registrations": sum(len(r) for v in vtable_index.values() for r in v.values()),
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"Vtable registrations: {len(vtable_regs)} table(s), "
              f"{sum(len(r) for v in vtable_index.values() for r in v.values())} field(s) indexed")
    # Preserve fn_ptr_calls for the cgdb dispatch_candidates population block
    # below (the cgdb block runs after the del below). vtable_regs is already
    # preserved above and survives the del.
    fn_ptr_calls = fn_ptr_calls_data
    # Free vtable and other extraction-derived data
    del vtable_regs_data, macro_regs_data, token_paste_data
    del container_of_data, conversion_data, struct_defs_data
    del fn_ptr_calls_data, passthrough_data, field_assignments_data
    gc.collect()

    # Determine storage type EARLY: the G-based index/doc generation
    # below is skipped entirely for the sqlite path (the SQLite branch
    # rebuilds them from the database after freeing G).
    storage_type = getattr(args, 'storage', 'json')
    if storage_type == 'auto':
        # Auto-select sqlite for large graphs (>100K nodes), json for smaller
        storage_type = 'sqlite' if G.number_of_nodes() > 100000 else 'json'
        print(f"[build] Auto-selected storage: {storage_type} (nodes={G.number_of_nodes()})", file=sys.stderr)

    # storage=sqlite skips this entire G-based pass: the SQLite
    # branch below rebuilds indexes/summary/scenarios/pack from the
    # database after freeing G — running both made the first pass
    # pure wasted wall-clock (hours on kernel-sized graphs).
    if storage_type != 'sqlite':
        # Pre-compute indexes
        tracker.begin("build_indexes")
        print(f"[build] Building indexes... ({G.number_of_nodes()} nodes)",
              file=sys.stderr)
        _indexes_start = time.time()
        _build_indexes(G, outdir)
        index_files = glob.glob(os.path.join(outdir, ".code2database_*.json"))
        tracker.end_with_files(index_files)
        print(f"[build] Indexes built in {time.time() - _indexes_start:.0f}s",
              file=sys.stderr)

        # Generate human-readable summary
        tracker.begin("generate_docs")
        print(f"[build] Generating docs...", file=sys.stderr)
        _docs_start = time.time()
        # Scenarios file FIRST: the summary generator reads
        # .code2database_scenarios.json for SCENARIOS_SUMMARY.md — with the
        # old order (summary, then scenarios) the first build into a clean
        # dir never produced SCENARIOS_SUMMARY.md, and rebuilds summarized
        # the PREVIOUS build's scenarios.
        _build_scenarios_file(G, outdir, build_info=build_info)
        summary_path = _build_callgraph_summary_md(G, outdir, source_root, build_info=build_info)
        _build_domain_readmes(G, outdir)

        # Generate LLM context pack
        pack_path = _build_context_pack(G, outdir, source_root, build_info=build_info)
        doc_files = [os.path.join(outdir, "CODE2DATABASE_SUMMARY.md")]
        doc_files += glob.glob(os.path.join(outdir, "domains/*/README.md"), recursive=True)
        tracker.end_with_files(doc_files)
        print(f"[build] Docs generated in {time.time() - _docs_start:.0f}s",
              file=sys.stderr)

        # Write build config file for future reference
        if build_info:
            bc_path = os.path.join(outdir, ".code2database_build_config.json")
            Path(bc_path).write_text(
                json.dumps(build_info, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

        print(f"Built invocation graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
        print(f"Domain files written to {outdir}/")
        print(f"Master: {master_path}")
        print(f"Summary: {summary_path}")
        print(f"Context pack: {pack_path}")
        if ep_count > 0:
            print(f"Endpoints: {ep_count} external endpoint(s) marked as out_end")
            print(f"Endpoint list exported to: {outdir}/.code2database_endpoints.json")
            print("Run 'classify-endpoints' after LLM analysis to finalize labels.")
        if build_info:
            dead_count = sum(1 for _, nd in G.nodes(data=True)
                            if "dead_code" in nd.get("labels", []))
            if dead_count:
                print(f"Dead code (excluded by macros): {dead_count} function(s)")

        # Auto-consolidate memory after build
        try:
            from _builder.memory.memory_manager import MemoryManager
            mgr = MemoryManager(outdir)
            mgr.consolidate()
        except Exception as e:
            print(f"Warning: memory consolidation failed ({e})", file=sys.stderr)

        # Post-build validation: check all output files for correctness
        tracker.begin("validate")
        val_result = None
        try:
            from _builder.ops.validate import validate_all
            val_result = validate_all(outdir, profile=builder_profile)
            print(val_result.summary())
            if not val_result.ok:
                print("WARNING: Post-build validation found errors — see above", file=sys.stderr)
        except Exception as e:
            print(f"Warning: post-build validation failed ({e})", file=sys.stderr)
        tracker.end()
        # Release validation caches before SQLite export to reduce peak memory
        if val_result is not None:
            del val_result
        gc.collect()

    if storage_type == 'sqlite':
        from _builder.graph.sqlite_store import SQLiteStore
        db_path = os.path.join(outdir, "code2database.db")
        print(f"[SQLite] Exporting to {db_path}...", file=sys.stderr)
        _sqlite_start = time.time()
        with SQLiteStore(db_path) as store:
            # Wipe legacy graph tables before the export so a rebuild into
            # an existing DB does not accumulate duplicate edges / access
            # rows (store_edges and the access batches are plain INSERT,
            # and store_functions is INSERT OR REPLACE — so without a wipe
            # every rebuild doubled the edges/access tables while stale
            # deleted-file nodes persisted forever). Order respects the
            # edges/entry_scores/field_access/global_access → functions FK.
            _wipe_conn = store._conn
            for _tbl in ("edges", "entry_scores", "field_access",
                         "global_access", "communities", "domain_stats"):
                try:
                    _wipe_conn.execute(f"DELETE FROM {_tbl}")
                except sqlite3.Error:
                    pass
            _wipe_conn.execute("DELETE FROM functions")
            _wipe_conn.commit()
            # Store functions AND populate field_access/global_access in a
            # single pass over G.nodes(data=True). Previously these were two
            # separate full-graph traversals (lines 7228 and 7242), costing
            # an extra ~30-60s on a 1.6M-node kernel graph.
            _BATCH_SIZE = 5000
            _func_batch = []
            _access_batch = []  # accumulate nodes for batch field/global access
            # Finding 9: Accumulate domain counts during the functions pass
            # to avoid a separate full-graph traversal for domain_stats.
            _domain_counter: Counter = Counter()
            print(f"[SQLite] Exporting functions + field_access/global_access...", file=sys.stderr)
            _sqlite_func_start = time.time()
            for nid, nd in G.nodes(data=True):
                # Batch-accumulate for functions table
                node_dict = dict(nd, id=nid)
                _func_batch.append(node_dict)
                if len(_func_batch) >= _BATCH_SIZE:
                    store.store_functions(_func_batch)
                    _func_batch.clear()
                # Accumulate non-empty, non-file nodes for batch field/global
                # access (same filter as the old second pass). Previously
                # called store_field_access/store_global_access per-function,
                # which issued ~835K individual executemany calls. Now we
                # batch them and use store_field_access_batch /
                # store_global_access_batch for ~167 bulk executemanys.
                if not nd.get("is_empty", False) and nd.get("node_type") != "file":
                    _access_batch.append(node_dict)
                    if len(_access_batch) >= _BATCH_SIZE:
                        store.store_field_access_batch(_access_batch, autocommit=False)
                        store.store_global_access_batch(_access_batch, autocommit=False)
                        _access_batch.clear()
                # Count domains for domain_stats (merged from separate pass)
                _domain_counter[nd.get("domain", "root")] += 1
            # Flush remaining function batch
            if _func_batch:
                store.store_functions(_func_batch)
            del _func_batch
            # Flush remaining access batch
            if _access_batch:
                store.store_field_access_batch(_access_batch, autocommit=False)
                store.store_global_access_batch(_access_batch, autocommit=False)
                del _access_batch
            # Flush accumulated field/global access rows
            store._conn.commit()
            print(f"[SQLite] Functions + field/global access exported in "
                  f"{time.time() - _sqlite_func_start:.1f}s", file=sys.stderr)

            # Store edges — stream in batches to avoid building a full list
            _edge_batch = []
            for u, v, ed in G.edges(data=True):
                _edge_batch.append(dict(ed, caller=u, callee=v))
                if len(_edge_batch) >= _BATCH_SIZE:
                    store.store_edges(_edge_batch)
                    _edge_batch.clear()
            if _edge_batch:
                store.store_edges(_edge_batch)
            del _edge_batch

            # Store communities
            if comm_result and comm_result.communities:
                store.store_communities(comm_result.communities)

            # Store entry scores
            if entry_scores:
                score_list = [{"id": nid, "name": G.nodes[nid].get("name", ""),
                               "score": score, "domain": G.nodes[nid].get("domain", "")}
                              for nid, score in entry_scores.items()]
                store.store_entry_scores(score_list)

            # Store domain stats — uses counts accumulated during the
            # functions pass above (Finding 9: merged from separate pass).
            for domain, func_count in _domain_counter.items():
                stats = {
                    "funcs": func_count,
                    "domain": domain,
                }
                store.store_domain_stats(domain, stats)
            del _domain_counter

            # cgdb (code graph database) 13-layer export — writes cgdb_nodes/
            # cgdb_types/cgdb_edges/cgdb_invoke_sites to the new schema tables
            # alongside the legacy functions/edges.
            _cgdb_export_error = ""  # set if the export below fails
            if cgdb_nodes_data or cgdb_types_data or cgdb_edges_data:
                print(f"[cgdb] Writing cgdb 13-layer records...", file=sys.stderr)
                _cgdb_start = time.time()
                # Detect commit_hash ONCE so every batch row carries the
                # same provenance. Falls back to a sentinel so the NOT NULL
                # constraint is never violated.
                _build_commit_hash = _detect_commit_hash(source_root) or "unknown"
                # Record an "initial-empty" version BEFORE writing any nodes
                # so time-travel can distinguish: v_initial = empty graph,
                # v_build = post-ingest, v_enhance = post-auto-enhance.
                # Use the real commit hash (or 'unknown') for commit_hash;
                # the label goes into commit_subject so the schema semantics
                # stay correct (commit_hash is a git/svn hash, not a label).
                try:
                    from _builder.cgdb.cgdb_versions import VersionController
                    vc_pre = VersionController(db_path, conn=store._conn)
                    _initial_hash = _detect_commit_hash(source_root) or "unknown"
                    vc_pre.record_version(
                        commit_hash=_initial_hash,
                        commit_subject="initial empty graph (pre-build)",
                        force_insert=True,
                    )
                    # Don't close vc_pre — share conn with the post-build VC.
                except Exception as _ve:
                    print(f"[cgdb] WARNING: initial record_version failed: {_ve}",
                          file=sys.stderr)
                try:
                    from _builder.cgdb.cgdb_ingest import extract_cgdb_batch
                    from _builder.cgdb.cgdb_store import SQLiteCGDBStore
                    # Schema already applied by SQLiteStore._create_tables.
                    # Share the connection so writes are in the same transaction.
                    cgdb_store = SQLiteCGDBStore(db_path, conn=store._conn)
                    # Wipe all cgdb tables (preserving graph_versions) so
                    # rebuilds don't accumulate duplicates. Without this,
                    # every rebuild doubles row counts in non-deduped tables
                    # (doc_comments, sync_primitives, data_flow, etc.).
                    _wipe_cgdb_data(store._conn)
                    # Group cgdb records by source file (one batch per file).
                    # Each cgdb_node carries its own file_path; we partition by it.
                    _nodes_by_file = {}
                    for n in cgdb_nodes_data:
                        fp = n.get("file_path", "")
                        _nodes_by_file.setdefault(fp, []).append(n)
                    _edges_by_file = {}
                    for e in cgdb_edges_data:
                        fp = e.get("file_path", "")
                        _edges_by_file.setdefault(fp, []).append(e)
                    # Partition per-file records that carry file_path so each
                    # write_batch only sees its own file's rows. Records
                    # without file_path fall back to the empty-string bucket
                    # which is written once (when fp == "" is skipped) — but
                    # to avoid losing them we emit them on the first batch.
                    _docs_by_file = {}
                    for d in cgdb_doc_comments_data:
                        fp = d.get("file_path", "")
                        _docs_by_file.setdefault(fp, []).append(d)
                    _includes_by_file = {}
                    for inc in cgdb_includes_data:
                        fp = inc.get("file_path", "")
                        _includes_by_file.setdefault(fp, []).append(inc)
                    _sync_by_file = {}
                    for sp in cgdb_sync_primitives_data:
                        fp = sp.get("file_path", "")
                        _sync_by_file.setdefault(fp, []).append(sp)
                    _dataflow_by_file = {}
                    for df in cgdb_data_flow_data:
                        fp = df.get("file_path", "")
                        _dataflow_by_file.setdefault(fp, []).append(df)
                    _alias_by_file = {}
                    for a in cgdb_alias_sets_data:
                        fp = a.get("file_path", "")
                        _alias_by_file.setdefault(fp, []).append(a)
                    _invoke_sites_by_file = {}
                    for cs in cgdb_invoke_sites_data:
                        fp = cs.get("file_path", "")
                        _invoke_sites_by_file.setdefault(fp, []).append(cs)
                    _conditions_by_file = {}
                    for c in conditions_data:
                        fp = c.get("file_path", "")
                        _conditions_by_file.setdefault(fp, []).append(c)
                    # Records with no file_path go into a single "global"
                    # bucket written only once on the first batch.
                    _global_docs = _docs_by_file.pop("", [])
                    _global_includes = _includes_by_file.pop("", [])
                    _global_sync = _sync_by_file.pop("", [])
                    _global_dataflow = _dataflow_by_file.pop("", [])
                    _global_alias = _alias_by_file.pop("", [])
                    _global_invoke_sites = _invoke_sites_by_file.pop("", [])
                    _global_conditions = _conditions_by_file.pop("", [])
                    _cgdb_node_count = 0
                    _cgdb_edge_count = 0
                    _global_emitted = False
                    # L1 ingest was previously interleaved with cgdb_store.write_batch
                    # in this loop, making it a pure-serial bottleneck. Split into
                    # two phases:
                    #   Phase 1 (serial): write all cgdb batches via the shared
                    #     connection (must be serial — single conn).
                    #   Phase 2 (parallel): run ingest_l1 across N worker processes,
                    #     each with its own WAL connection. The libclang parse is
                    #     CPU-bound and stateless across files.
                    _l1_tasks = []
                    # Begin a single bulk-load transaction for the entire
                    # per-file write loop. Without this, each write_batch()
                    # issues its own BEGIN+COMMIT, causing 40K+ fdatasync
                    # calls (one per file) on large projects like the Linux
                    # kernel — the dominant bottleneck observed in
                    # production (12h+ stall at this point).
                    # See cgdb_store.begin_bulk_load() for PRAGMA tuning.
                    _BULK_CHECKPOINT_INTERVAL = 1000
                    _bulk_file_count = 0
                    cgdb_store.begin_bulk_load()
                    _bulk_ok = False
                    try:
                        for fp, nodes in _nodes_by_file.items():
                            if not fp:
                                continue
                            # Records without file_path are emitted only on the
                            # first batch to avoid N× duplication.
                            is_first = not _global_emitted
                            sub_result = {
                                "file": fp,
                                "cgdb_nodes": nodes,
                                "cgdb_types": (
                                    cgdb_types_data if is_first else []
                                ),  # written once globally; deduped by id
                                "cgdb_edges": _edges_by_file.get(fp, []),
                                "cgdb_invoke_sites": (
                                    _invoke_sites_by_file.get(fp, []) +
                                    (_global_invoke_sites if is_first else [])
                                ),
                                "cgdb_predicates": (
                                    cgdb_predicates_data if is_first else []
                                ),  # written once globally; deduped by id
                                "cgdb_ops_bindings": (
                                    cgdb_ops_bindings_data if is_first else []
                                ),  # written once globally; deduped by edge_id
                                "cgdb_basic_blocks": (
                                    cgdb_basic_blocks_data if is_first else []
                                ),  # written once globally; deduped by id
                                "cgdb_cfg_edges": (
                                    cgdb_cfg_edges_data if is_first else []
                                ),  # written once globally; deduped by (src,dst,kind)
                                "cgdb_data_flow": (
                                    _dataflow_by_file.get(fp, []) +
                                    (_global_dataflow if is_first else [])
                                ),
                                "cgdb_sync_primitives": (
                                    _sync_by_file.get(fp, []) +
                                    (_global_sync if is_first else [])
                                ),
                                "cgdb_happens_before": (
                                    cgdb_happens_before_data if is_first else []
                                ),  # written once globally; deduped by (w,r,reason)
                                "cgdb_alias_sets": (
                                    _alias_by_file.get(fp, []) +
                                    (_global_alias if is_first else [])
                                ),
                                "cgdb_doc_comments": (
                                    _docs_by_file.get(fp, []) +
                                    (_global_docs if is_first else [])
                                ),
                                "cgdb_metadata": (
                                    cgdb_metadata_data if is_first else []
                                ),  # written once globally; deduped by (target_id,target_kind,key)
                                "cgdb_includes": (
                                    _includes_by_file.get(fp, []) +
                                    (_global_includes if is_first else [])
                                ),
                                "conditions": (
                                    _conditions_by_file.get(fp, []) +
                                    (_global_conditions if is_first else [])
                                ),
                            }
                            batch = extract_cgdb_batch(
                                sub_result,
                                commit_hash=_build_commit_hash,
                                version_id=1,
                            )
                            # NOTE: do NOT wipe batch.types here for files
                            # after the first. sub_result['cgdb_types'] is
                            # already gated to the first file (is_first) —
                            # for every later file batch.types contains ONLY
                            # the per-file lazy TypeRecords that backfill
                            # cgdb_nodes.type_id for nodes whose type came
                            # from a signature fallback. The old
                            # `if _files_written: batch.types = []` wiped
                            # exactly those rows, leaving dangling type_id
                            # FKs on every file but the first (silently —
                            # FK enforcement is off on this connection).
                            cgdb_store.write_batch(batch)
                            _global_emitted = True
                            _cgdb_node_count += len(nodes)
                            _cgdb_edge_count += len(_edges_by_file.get(fp, []))

                            # Collect L1 ingest tasks for Phase 2 (parallel).
                            # l1_ingest gracefully falls back to a sha256-only
                            # record when libclang is unavailable.
                            if fp.endswith(('.c', '.cc', '.cpp', '.cxx',
                                            '.h', '.hh', '.hpp', '.hxx',
                                            '.m', '.mm')):
                                from _builder.cgdb.cgdb_ingest import file_id_for
                                _l1_fid = file_id_for(fp)
                                _l1_tasks.append((fp, _l1_fid))

                            # Periodic WAL checkpoint to bound WAL growth.
                            # Without this, a single multi-GB transaction
                            # lets the WAL grow unbounded, degrading index
                            # lookups for every INSERT OR REPLACE inside
                            # the same transaction.
                            _bulk_file_count += 1
                            if (_bulk_file_count %
                                    _BULK_CHECKPOINT_INTERVAL == 0):
                                cgdb_store.commit_bulk_checkpoint()
                        _bulk_ok = True
                    finally:
                        if _bulk_ok:
                            cgdb_store.finalize()
                        else:
                            cgdb_store.abort_bulk_load()
                    # Flush any pending write on the shared conn before L1
                    # workers start (they need to read cgdb_nodes via their
                    # own connections in WAL mode).
                    try:
                        store._conn.commit()
                    except Exception:
                        pass

                    # Phase 2: L1 ingest (parallel when --parallel-mode process)
                    if _l1_tasks:
                        try:
                            from _builder.scanner_bridge.l1_ingest import run_l1_ingest
                            try:
                                from _builder.build.parallel import resolve_jobs
                                _l1_parallel_mode = getattr(args, 'parallel_mode', None) or 'thread'
                                _l1_workers = resolve_jobs(
                                    getattr(args, 'jobs', 0),
                                    max_workers_cap=getattr(args, 'max_workers', 0))
                            except ImportError:
                                _l1_workers = 1
                                _l1_parallel_mode = getattr(args, 'parallel_mode', None) or 'thread'
                            # When the user runs the default 'thread' mode on a
                            # large project, L1 ingest takes ~7s/file × 60K = 77h
                            # serially. Auto-promote to 'process' mode when:
                            # - >1000 C/C++ files need ingest
                            # - machine has >= 4 cores
                            # - user didn't explicitly pass --parallel-mode
                            # (if they explicitly chose 'thread', respect it;
                            # but the default is thread and most users don't
                            # realize they need process for CPU-bound libclang)
                            _user_set_pm = getattr(args, 'parallel_mode', None) is not None
                            if (_l1_parallel_mode == "thread"
                                    and not _user_set_pm
                                    and len(_l1_tasks) > 1000
                                    and _l1_workers > 1
                                    and (os.cpu_count() or 1) >= 4):
                                _l1_parallel_mode = "process"
                                print(
                                    f"[l1] Auto-promoting L1 ingest to process mode: "
                                    f"{len(_l1_tasks)} C/C++ files with "
                                    f"{_l1_workers} workers "
                                    f"(thread mode would take ~{len(_l1_tasks)*7//3600}h+)",
                                    file=sys.stderr)
                            _l1_results = run_l1_ingest(
                                tasks=_l1_tasks,
                                db_path=db_path,
                                source_root=source_root,
                                commit_hash=_build_commit_hash,
                                workers=_l1_workers,
                                parallel_mode=_l1_parallel_mode,
                                serial_conn=store._conn,
                            )
                        except ImportError:
                            _l1_results = []
                        # Print per-file summary (preserves prior log format).
                        for _l1_stats in _l1_results:
                            _fp = _l1_stats.get("file_path", "")
                            if _l1_stats.get("consistency_ok"):
                                _l1_msg = (
                                    f"[l1] {os.path.basename(_fp)}: "
                                    f"{_l1_stats.get('tokens', 0)} tokens, "
                                    f"{_l1_stats.get('macros', 0)} macros, "
                                    f"{_l1_stats.get('pp_branches', 0)} pp_branches, "
                                    f"{_l1_stats.get('string_literals', 0)} str_literals "
                                    f"(sha256 ok)"
                                )
                            elif _l1_stats.get("error"):
                                # external dependency files (not in this source
                                # tree — expected for subset builds like
                                # libstorage) are flagged file_not_found
                                # by l1_ingest and logged at INFO, not
                                # WARNING.  Other errors (permission
                                # denied, I/O error) stay at WARNING.
                                if _l1_stats.get("file_not_found"):
                                    _l1_msg = (
                                        f"[l1] INFO: {os.path.basename(_fp)}: "
                                        f"not in this source tree — "
                                        f"skipped (external dependency)"
                                    )
                                else:
                                    _l1_msg = (
                                        f"[l1] WARNING: {os.path.basename(_fp)}: "
                                        f"{_l1_stats['error']}"
                                    )
                            else:
                                _l1_msg = (
                                    f"[l1] {os.path.basename(_fp)}: "
                                    f"consistency_ok=False "
                                    f"(disk={_l1_stats.get('disk_sha256','')[:8]}, "
                                    f"rendered={_l1_stats.get('rendered_sha256','')[:8]})"
                                )
                            print(_l1_msg, file=sys.stderr)
                    cgdb_store.close()
                    print(f"[cgdb] Wrote {_cgdb_node_count} nodes, "
                          f"{_cgdb_edge_count} edges, {len(cgdb_types_data)} types, "
                          f"{len(cgdb_predicates_data)} predicates, "
                          f"{len(cgdb_ops_bindings_data)} ops_bindings, "
                          f"{len(cgdb_basic_blocks_data)} basic_blocks, "
                          f"{len(cgdb_cfg_edges_data)} cfg_edges, "
                          f"{len(cgdb_data_flow_data)} data_flow, "
                          f"{len(cgdb_sync_primitives_data)} sync_primitives, "
                          f"{len(cgdb_happens_before_data)} happens_before "
                          f"({time.time()-_cgdb_start:.1f}s)", file=sys.stderr)

                    # Populate invoke_sites.dispatch_candidates by joining
                    # vtable_registrations (ops_table → impl functions) with
                    # fn_ptr_calls (call sites). Each function-pointer call
                    # with field_name matching a registered vtable field gets
                    # the impl function node IDs as dispatch_candidates.
                    try:
                        from _scanner.unified_id import unified_node_id
                        # Build (struct_type, field_name) → [impl_nid, ...] map
                        # from vtable_registrations (aggregated across all files).
                        # Use vtable_regs (preserved before `del vtable_regs_data`).
                        ops_field_to_impls = {}
                        for vt in (vtable_regs or []):
                            struct_type = vt.get('struct_type', '') or ''
                            for reg in vt.get('registrations', []) or []:
                                fname = reg.get('field', '') or ''
                                func_name = reg.get('func_name', '') or ''
                                if not fname or not func_name:
                                    continue
                                # Detect language from the source file
                                _src = vt.get('source_file', '') or ''
                                _lang = 'c'
                                if _src.endswith('.go'):
                                    _lang = 'go'
                                elif _src.endswith('.rs'):
                                    _lang = 'rust'
                                elif _src.endswith('.py'):
                                    _lang = 'python'
                                elif _src.endswith('.java'):
                                    _lang = 'java'
                                elif _src.endswith(('.cc', '.cpp', '.cxx', '.hpp', '.hxx')):
                                    _lang = 'cpp'
                                impl_nid = unified_node_id(_lang, func_name)
                                key = (struct_type, fname)
                                ops_field_to_impls.setdefault(key, []).append(impl_nid)
                                # Also key by field_name only (fallback)
                                ops_field_to_impls.setdefault(('', fname), []).append(impl_nid)
                        # Walk fn_ptr_calls (dict keyed by invoker_name)
                        # and update invoke_sites in SQLite. Use fn_ptr_calls
                        # (preserved before `del fn_ptr_calls_data`).
                        conn = store._conn
                        # Build fn_name → unified node id from cgdb_nodes
                        fn_name_to_nid = {}
                        for row in conn.execute(
                            "SELECT name, id FROM cgdb_nodes WHERE kind='function'"
                        ):
                            fn_name_to_nid[row[0]] = row[1]
                        # Build invoke_site index by (invoker_id, invoked_id).
                        # invoke_sites has no `line` column, so we update all
                        # invoke_sites matching the (invoker, invoked) pair —
                        # for function_pointer dispatch this is correct since
                        # all calls from invoker→invoked share candidates.
                        invoke_site_index = {}
                        for row in conn.execute(
                            "SELECT id, invoker_id, invoked_id FROM invoke_sites "
                            "WHERE invoke_kind='function_pointer'"
                        ):
                            key = (row[1], row[2])
                            invoke_site_index.setdefault(key, []).append(row[0])
                        # Precompute index: field_name -> list of (struct_type, impls)
                        # so the per-call lookup is O(small list) instead of scanning
                        # all ops_field_to_impls entries for each fn_ptr_call.
                        _impls_by_field_name = defaultdict(list)
                        for (st, fn), impls in ops_field_to_impls.items():
                            _impls_by_field_name[fn].append((st, impls))
                        # Match each fn_ptr_call to candidates
                        import json as _json
                        update_batch = []  # list of (cand_json, rowid) for executemany
                        for invoker_name, calls in (fn_ptr_calls or {}).items():
                            invoker_nid = fn_name_to_nid.get(invoker_name)
                            if invoker_nid is None:
                                continue
                            for call in calls or []:
                                field_name = call.get('field_name', '') or ''
                                callee_name = call.get('callee_name', '') or ''
                                if not field_name:
                                    continue
                                # Find candidates: first try (struct_type, field_name)
                                # matching the struct_chain, then fall back to ('', field_name).
                                sc = call.get('struct_chain', '') or ''
                                candidates = []
                                for st, impls in _impls_by_field_name.get(field_name, ()):
                                    if st and st not in sc:
                                        continue
                                    candidates.extend(impls)
                                if not candidates:
                                    # Fall back to all impls with this field_name
                                    for st, impls in _impls_by_field_name.get(field_name, ()):
                                        if not st:
                                            candidates.extend(impls)
                                if not candidates:
                                    continue
                                # De-dup candidates
                                seen = set()
                                candidates = [c for c in candidates if not (c in seen or seen.add(c))]
                                # Find the invoke_site row to update
                                invoked_nid = fn_name_to_nid.get(callee_name, 0)
                                if not invoked_nid:
                                    # Try unified_node_id directly
                                    _lang = 'c'
                                    _src = call.get('file_path', '') or ''
                                    if _src.endswith('.go'):
                                        _lang = 'go'
                                    elif _src.endswith('.rs'):
                                        _lang = 'rust'
                                    elif _src.endswith('.py'):
                                        _lang = 'python'
                                    elif _src.endswith('.java'):
                                        _lang = 'java'
                                    elif _src.endswith(('.cc', '.cpp', '.cxx', '.hpp', '.hxx')):
                                        _lang = 'cpp'
                                    invoked_nid = unified_node_id(_lang, callee_name) if callee_name else 0
                                rowids = invoke_site_index.get((invoker_nid, invoked_nid), [])
                                if not rowids:
                                    continue
                                _cand_json = _json.dumps(candidates)
                                for rowid in rowids:
                                    update_batch.append((_cand_json, rowid))
                        if update_batch:
                            conn.executemany(
                                "UPDATE invoke_sites SET dispatch_candidates=? "
                                "WHERE id=?",
                                update_batch
                            )
                            updated = len(update_batch)
                        else:
                            updated = 0
                        conn.commit()
                        if updated:
                            print(f"[cgdb] Populated dispatch_candidates for "
                                  f"{updated} invoke_sites", file=sys.stderr)
                    except Exception as _de:
                        print(f"[cgdb] WARNING: dispatch_candidates population failed: {_de}",
                              file=sys.stderr)

                    # Populate invoke_sites.arg_bindings from callee_args
                    # stored on function nodes in the graph. Each function
                    # node's `callee_args` attribute is a list of per-call
                    # records: {call_order, callee, args: [{pos, value}, ...],
                    # line, column, start_byte, end_byte}. We join on
                    # (invoker_id, invoked_id) and prefer the call whose
                    # `line` matches the invoke_site's line (when available
                    # via cgdb_edges).
                    try:
                        import json as _json
                        # Build fqn → cgdb_nodes.id map (G node IDs are FQN
                        # strings; cgdb_nodes.id is numeric hash). Also keep
                        # the name → id map already built (fn_name_to_nid).
                        fqn_to_nid = {}
                        for row in conn.execute(
                            "SELECT id, name, fqn FROM cgdb_nodes WHERE kind='function'"
                        ):
                            if row[2]:
                                fqn_to_nid[row[2]] = row[0]
                        # Build (invoker_id, invoked_id) → list of callee_arg records.
                        # invoker_id is resolved via fqn_to_nid (G node ID is FQN).
                        # invoked_id is resolved via fn_name_to_nid (callee is a
                        # simple name in callee_args).
                        invoker_to_arg_calls = {}
                        for fn_fqn, nd in G.nodes(data=True):
                            ca_list = nd.get('callee_args') or []
                            if not ca_list:
                                continue
                            invoker_nid = fqn_to_nid.get(fn_fqn)
                            if invoker_nid is None:
                                # Fall back to fn_name_to_nid using the node's name
                                invoker_nid = fn_name_to_nid.get(nd.get('name', ''))
                            if invoker_nid is None:
                                continue
                            for ca in ca_list:
                                callee_name = ca.get('callee', '') or ''
                                if not callee_name:
                                    continue
                                callee_nid = fn_name_to_nid.get(callee_name, 0)
                                if not callee_nid:
                                    continue
                                invoker_to_arg_calls.setdefault(
                                    (invoker_nid, callee_nid), []).append(ca)
                        # Walk invoke_sites and populate arg_bindings.
                        # Batch all updates into a list and issue a single
                        # executemany — the per-row conn.execute("UPDATE")
                        # inside the SELECT loop was the same class of
                        # bottleneck as the old per-file COMMIT bug.
                        updated_args = 0
                        _arg_update_batch = []
                        for row in conn.execute(
                            "SELECT id, invoker_id, invoked_id FROM invoke_sites"
                        ):
                            isite_id = row[0]
                            invoker_id = row[1]
                            invoked_id = row[2]
                            arg_calls = invoker_to_arg_calls.get((invoker_id, invoked_id))
                            if not arg_calls:
                                continue
                            # If only one call record, use it directly.
                            if len(arg_calls) == 1:
                                arg_bindings = arg_calls[0].get('args', []) or []
                            else:
                                # Multiple calls: pick the first one (can't
                                # disambiguate without line in invoke_sites).
                                # Future: add line column to invoke_sites.
                                arg_bindings = arg_calls[0].get('args', []) or []
                            if not arg_bindings:
                                continue
                            _arg_update_batch.append(
                                (_json.dumps(arg_bindings), isite_id)
                            )
                        if _arg_update_batch:
                            conn.executemany(
                                "UPDATE invoke_sites SET arg_bindings=? "
                                "WHERE id=?",
                                _arg_update_batch
                            )
                            updated_args = len(_arg_update_batch)
                        conn.commit()
                        if updated_args:
                            print(f"[cgdb] Populated arg_bindings for "
                                  f"{updated_args} invoke_sites",
                                  file=sys.stderr)
                    except Exception as _ae:
                        print(f"[cgdb] WARNING: arg_bindings population failed: {_ae}",
                              file=sys.stderr)

                    # Auto record_version so all nodes carry a real
                    # version_id. Detect commit hash from the source root
                    # via git rev-parse HEAD (fallback to svn info, then
                    # to a synthetic timestamp-based hash).
                    try:
                        from _builder.cgdb.cgdb_versions import VersionController
                        commit_hash = _detect_commit_hash(source_root)
                        commit_subject = _detect_commit_subject(source_root)
                        vc = VersionController(db_path, conn=store._conn)
                        version_id = vc.record_version(
                            commit_hash=commit_hash,
                            commit_subject=commit_subject,
                        )
                        # Update all cgdb_nodes/edges first_seen_version
                        # to this version_id (they were just written with
                        # default version_id=1).
                        conn = store._conn
                        conn.execute(
                            "UPDATE cgdb_nodes SET first_seen_version = ? "
                            "WHERE first_seen_version = 1",
                            (version_id,)
                        )
                        conn.execute(
                            "UPDATE cgdb_nodes SET last_seen_version = ? "
                            "WHERE last_seen_version = 1",
                            (version_id,)
                        )
                        conn.execute(
                            "UPDATE cgdb_edges SET first_seen_version = ? "
                            "WHERE first_seen_version = 1",
                            (version_id,)
                        )
                        conn.execute(
                            "UPDATE cgdb_edges SET last_seen_version = ? "
                            "WHERE last_seen_version = 1",
                            (version_id,)
                        )
                        conn.commit()
                        print(f"[cgdb] Recorded version_id={version_id} "
                              f"for commit {commit_hash[:12]}",
                              file=sys.stderr)
                    except Exception as ve:
                        print(f"[cgdb] WARNING: record_version failed: {ve}",
                              file=sys.stderr)
                except Exception as e:
                    # cgdb semantic data is missing/partial after this —
                    # escalate beyond a stderr WARNING (which scrolled by
                    # unnoticed while builds reported success) and make
                    # the degraded state durable + machine-detectable.
                    _cgdb_export_error = str(e)
                    _marker = _mark_cgdb_export_failed(outdir, str(e), {
                        "stage": "cgdb_export",
                        "cgdb_nodes_input": len(cgdb_nodes_data),
                        "cgdb_edges_input": len(cgdb_edges_data),
                        "cgdb_types_input": len(cgdb_types_data),
                    })
                    print(f"[cgdb] ERROR: cgdb export failed: {e}",
                          file=sys.stderr)
                    print(f"[cgdb] ERROR: the graph is missing cgdb "
                          f"semantic data (L1 tokens, types, vtables, "
                          f"invoke_sites, ...); marker: {_marker} — "
                          f"re-run the build", file=sys.stderr)
                else:
                    # This build's cgdb export completed: drop any stale
                    # failure marker from a previous build so the marker
                    # always reflects the latest build.
                    _cgdb_export_error = ""
                    _clear_cgdb_export_failed(outdir)

        print(f"[SQLite] Export complete: {db_path} ({time.time()-_sqlite_start:.0f}s)", file=sys.stderr)

        # CRITICAL: Free NetworkX graph after SQLite export to reclaim ~16GB+
        # All subsequent operations (index building, doc generation) will use
        # SQLite queries or the already-written JSON files instead of G.
        _node_count = G.number_of_nodes()
        _edge_count = G.number_of_edges()
        del G
        gc.collect()
        if memory_guard:
            info = memory_guard.get_memory_info()
            print(f"[MemoryGuard] After freeing graph: {info.get('usage_percent', 0)*100:.1f}% memory, "
                  f"{info.get('used_mb', 0):.0f}MB (saved ~{16 if _node_count > 500000 else 4}GB)",
                  file=sys.stderr)

        # Build indexes from SQLite instead of NetworkX — much lower memory
        tracker.begin("build_indexes")
        print(f"[build] Building indexes from SQLite... ({_node_count} nodes)",
              file=sys.stderr)
        _indexes_start = time.time()
        _build_indexes_from_sqlite(db_path, outdir)
        index_files = glob.glob(os.path.join(outdir, ".code2database_*.json"))
        tracker.end_with_files(index_files)
        print(f"[build] Indexes built in {time.time() - _indexes_start:.0f}s",
              file=sys.stderr)

        # Generate docs from SQLite + existing JSON files.
        # Parallelize the 5 independent generators (same as the
        # low-memory streaming path above): each opens its own SQLite
        # connection (WAL supports concurrent reads). Sequential cost
        # ~409s on kernel-scale graphs; parallel cuts wall-clock to the
        # slowest generator.
        tracker.begin("generate_docs")
        print(f"[build] Generating docs (parallel)...", file=sys.stderr)
        _docs_start = time.time()
        from concurrent.futures import ThreadPoolExecutor, as_completed
        _doc_futures = {}
        with ThreadPoolExecutor(max_workers=5) as _doc_pool:
            _doc_futures[_doc_pool.submit(
                _build_callgraph_summary_md_from_sqlite,
                db_path, outdir, source_root, build_info=build_info)] = "summary"
            _doc_futures[_doc_pool.submit(
                _build_domain_readmes_from_sqlite,
                db_path, outdir)] = "readmes"
            _doc_futures[_doc_pool.submit(
                _build_scenarios_file_from_sqlite,
                db_path, outdir, build_info=build_info,
                builder_profile=builder_profile)] = "scenarios"
            _doc_futures[_doc_pool.submit(
                _build_architecture_flows_from_sqlite,
                db_path, outdir, source_root, build_info=build_info)] = "arch_flows"
            _doc_futures[_doc_pool.submit(
                _build_context_pack_from_sqlite,
                db_path, outdir, source_root, build_info=build_info)] = "context_pack"
            # Collect results (log failures but don't abort others)
            summary_path = None
            pack_path = None
            for _fut in as_completed(_doc_futures):
                _label = _doc_futures[_fut]
                try:
                    _result = _fut.result()
                    if _label == "summary":
                        summary_path = _result
                    elif _label == "context_pack":
                        pack_path = _result
                except Exception as _exc:
                    logging.getLogger(__name__).error(
                        "Doc generator '%s' failed: %s", _label, _exc,
                        exc_info=True)
        doc_files = [os.path.join(outdir, "CODE2DATABASE_SUMMARY.md")]
        doc_files += glob.glob(os.path.join(outdir, "domains/*/README.md"), recursive=True)
        tracker.end_with_files(doc_files)
        print(f"[build] Docs generated in {time.time() - _docs_start:.0f}s",
              file=sys.stderr)

        # Write build config file for future reference
        if build_info:
            bc_path = os.path.join(outdir, ".code2database_build_config.json")
            Path(bc_path).write_text(
                json.dumps(build_info, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

        print(f"Built invocation graph: {_node_count} nodes, {_edge_count} edges")
        if _cgdb_export_error:
            # stdout (not stderr): the failure must be visible in the
            # command's primary output, not just in log scroll.
            print("WARNING: cgdb export FAILED during this build — "
                  "cgdb semantic tables are missing/partial: "
                  f"{_cgdb_export_error}")
            print("WARNING: see "
                  f"{os.path.join(outdir, _CGDB_EXPORT_FAILED_MARKER)}; "
                  "re-run the build to restore cgdb data")
        print(f"Domain files written to {outdir}/")
        print(f"Summary: {summary_path}")
        print(f"Context pack: {pack_path}")
        if ep_count > 0:
            print(f"Endpoints: {ep_count} external endpoint(s) marked as out_end")
            print(f"Endpoint list exported to: {outdir}/.code2database_endpoints.json")
            print("Run 'classify-endpoints' after LLM analysis to finalize labels.")
        if build_info:
            dead_count = 0  # Already counted, approximate
            if dead_count:
                print(f"Dead code (excluded by macros): {dead_count} function(s)")

        # Auto-consolidate memory after build
        try:
            from _builder.memory.memory_manager import MemoryManager
            mgr = MemoryManager(outdir)
            mgr.consolidate()
        except Exception as e:
            print(f"Warning: memory consolidation failed ({e})", file=sys.stderr)

        # Validate using SQLite
        tracker.begin("validate")
        try:
            from _builder.ops.validate import validate_all
            val_result2 = validate_all(outdir, profile=builder_profile)
            print(val_result2.summary())
            if not val_result2.ok:
                print("WARNING: Post-build validation found errors — see above", file=sys.stderr)
        except Exception as e:
            print(f"Warning: post-build validation failed ({e})", file=sys.stderr)
        tracker.end()

        # Stats reconciliation using SQLite counts
        reconciliation = _validate_stats_consistency_sqlite(db_path, _node_count)

        # Write pipeline stats report
        report = tracker.write_report(outdir)
        report["stats_reconciliation"] = reconciliation
        stats_path = os.path.join(outdir, ".code2database_pipeline_stats.json")
        if not reconciliation.get("matches"):
            # This used to be written into .code2database_pipeline_stats.json
            # and nothing else — a build that silently dropped nodes in the
            # SQLite export still printed a healthy-looking summary.
            print(f"WARNING: stats mismatch between pipeline and SQLite: "
                  f"pipeline={reconciliation['pipeline_count']} nodes, "
                  f"sqlite={reconciliation['sqlite_count']} nodes "
                  f"(delta={reconciliation['delta']}, "
                  f"{reconciliation['delta_pct']}%). "
                  f"Details: {stats_path}", file=sys.stderr)
        Path(stats_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        total = report["summary"]
        comp = report.get("comparison", {})
        print(f"Pipeline stats: {total['total_stages']} stages, "
              f"{total['total_elapsed_sec']:.1f}s elapsed, "
              f"{total['total_output_tokens']} output tokens "
              f"(raw source ~{comp.get('raw_source_tokens_estimate', 0)} tokens, "
              f"savings {comp.get('savings_ratio', 'N/A')})",
              file=sys.stderr)

        # Cleanup: stop memory guard monitoring and show stats
        if memory_guard:
            memory_guard.stop_monitoring()
            stats = memory_guard.get_stats()
            if stats.get('peak_memory_mb', 0) > 0:
                print(f"[MemoryGuard] Stats: peak={stats['peak_memory_mb']:.0f}MB, "
                      f"gc={stats['gc_count']}, criticals={stats['criticals']}, "
                      f"warnings={stats['warnings']}", file=sys.stderr)
            memory_guard.maybe_gc(force=True)

        # O28: per-stage timing summary (SQLite path)
        if getattr(args, 'profile_timing', True):
            print(tracker.format_stage_summary(), file=sys.stderr)

        # Optional post-build auto-enhancement (SQLite path).
        if getattr(args, 'auto_enhance', False):
            _post_build_auto_enhance(args, outdir)

        # /D15: write coverage reports (SQLite path).
        try:
            from _builder.misc.coverage_report import (
                write_coverage_report, write_file_coverage,
            )
            _cov = write_coverage_report(outdir)
            if _cov:
                print(f"[coverage] Wrote {_cov}", file=sys.stderr)
            _fcov = write_file_coverage(outdir)
            if _fcov:
                print(f"[coverage] Wrote {_fcov}", file=sys.stderr)
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

        return  # Early return — all work done via SQLite path
    # Re-write with the reconciliation section included (JSON path)
    report = tracker.write_report(outdir)
    stats_path = os.path.join(outdir, ".code2database_pipeline_stats.json")
    Path(stats_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    total = report["summary"]
    comp = report.get("comparison", {})
    print(f"Pipeline stats: {total['total_stages']} stages, "
          f"{total['total_elapsed_sec']:.1f}s elapsed, "
          f"{total['total_output_tokens']} output tokens "
          f"(raw source ~{comp.get('raw_source_tokens_estimate', 0)} tokens, "
          f"savings {comp.get('savings_ratio', 'N/A')})",
          file=sys.stderr)

    # O28: per-stage timing summary (JSON path)
    if getattr(args, 'profile_timing', True):
        print(tracker.format_stage_summary(), file=sys.stderr)

    # Cleanup: stop memory guard monitoring and show stats
    if memory_guard:
        memory_guard.stop_monitoring()
        stats = memory_guard.get_stats()
        if stats.get('peak_memory_mb', 0) > 0:
            print(f"[MemoryGuard] Stats: peak={stats['peak_memory_mb']:.0f}MB, "
                  f"gc={stats['gc_count']}, criticals={stats['criticals']}, "
                  f"warnings={stats['warnings']}", file=sys.stderr)
        # Final garbage collection
        memory_guard.maybe_gc(force=True)

    # Optional post-build LLM auto-enhancement. Runs only when --auto-enhance
    # was passed AND an API key is available. Targets high-value nodes
    # (API entries, thread processors, hub functions) so the most-queried
    # parts of the graph get semantic descriptions first.
    if getattr(args, 'auto_enhance', False):
        _post_build_auto_enhance(args, outdir)

    # /D15: write coverage reports (JSON path).
    try:
        from _builder.misc.coverage_report import (
            write_coverage_report, write_file_coverage,
        )
        _cov = write_coverage_report(outdir)
        if _cov:
            print(f"[coverage] Wrote {_cov}", file=sys.stderr)
        _fcov = write_file_coverage(outdir)
        if _fcov:
            print(f"[coverage] Wrote {_fcov}", file=sys.stderr)
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass



