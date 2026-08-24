#!/usr/bin/env python3
"""Call graph builder and query tool — CLI entry point.

All implementation lives in the _builder package. This file only
handles argparse configuration and command routing.
"""

import argparse
import json
import os
import sys

# Ensure _vendor/networkx shim is found before the real networkx
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_vendor"))

# Import logging utilities first so other modules can use get_logger()
from _builder.logging_utils import configure_logging, get_logger, parse_log_level

# Core command handlers — these are lightweight to import and cover the
# most common operations (build, search, describe, trace, query).
from _builder.graph_build import cmd_build, _detect_build_system
from _builder.search_cmd import cmd_load, cmd_search, cmd_path, cmd_neighbors, cmd_impact, cmd_domain
from _builder.query import cmd_describe_node, cmd_resolve_chain, cmd_trace_chain, cmd_diff_chains, cmd_get_code_snippet, cmd_blast_radius, cmd_io_path, cmd_reverse_trace, cmd_field_access, cmd_field_flow, cmd_param_flow, cmd_describe_commit, cmd_node_history, cmd_graph_provenance, cmd_blame_node, cmd_find_commits
from _builder.query_lang import cmd_query
from _builder.value_flow import cmd_value_flow
from _builder.lock_coverage import cmd_lock_coverage
from _builder.data_dep import cmd_data_dep
from _builder.invariants import cmd_extract_invariants, cmd_find_invariants, cmd_apply_invariants
from _builder.auto_enhance import cmd_auto_enhance, cmd_batch_confirm, cmd_rollback, cmd_fill_request, cmd_heuristic_enhance
from _builder.transactions import (
    cmd_tx_begin, cmd_tx_commit, cmd_tx_rollback, cmd_tx_status,
    cmd_tx_snapshot, cmd_tx_restore, cmd_tx_list_snapshots, cmd_tx_replay_wal,
)
from _builder.ffi_bridge import cmd_ffi_detect, cmd_ffi_list, cmd_ffi_trace, cmd_ffi_types
from _builder.intent_router import cmd_intent_query
from _builder.explore import cmd_explore_flow
from _builder.key_paths import cmd_key_paths
from _builder.update_sync import cmd_merge, cmd_update, cmd_sync
from _builder.semantics import cmd_classify_endpoints, cmd_extract_semantics, cmd_apply_semantics, cmd_think_chain, cmd_extract_signals
from _builder.memory_cmd import cmd_save_memory, cmd_search_memory, cmd_validate_memory
from _builder.export import cmd_export_obsidian
from _builder.visualizer import cmd_export_html
from _builder.watcher import cmd_watch
from _builder.plugins import cmd_plugins, cmd_validate_plugin
from _builder.concurrency import cmd_concurrency_risks, cmd_data_lifecycle
from _builder.concurrency_analysis import cmd_detect_races, cmd_concurrency_analyze
from _builder.memory_ordering import cmd_happens_before, cmd_memory_ordering
from _builder.explain import cmd_explain_label, cmd_why_ambiguous
from _builder.audit_log import cmd_audit_log
from _builder.semantic_edges import (
    cmd_who_allocates, cmd_who_frees, cmd_unbalanced_alloc_free,
    cmd_who_locks, cmd_add_semantic_edges,
)
from _builder.llm_invariants import cmd_extract_invariants_llm
from _builder.graph_history import (
    cmd_graph_history, cmd_graph_diff, cmd_graph_record_version,
)
from _builder.memory_manager import cmd_manage_memory, cmd_memory_health
from _builder.knowledge_manager import cmd_extract_knowledge, cmd_apply_knowledge, cmd_knowledge_query, cmd_knowledge_validate
from _builder.kb_index import rebuild_kb_index as cmd_kb_rebuild_index_impl
from _builder.patcher import cmd_patch_from_diff, cmd_patch_from_git, cmd_light_scan
from _builder.changelog_update import cmd_quick_update, cmd_export_changes, cmd_merge_changes, cmd_semantic_status
from _builder.update_cmd import cmd_update_node, cmd_update_edge, cmd_patch_profile
from _builder.cgdb_commands import (
    cmd_cgdb_query, cmd_cgdb_time_travel, cmd_cgdb_configs_for,
    cmd_cgdb_ops_impls, cmd_cgdb_cfg_paths, cmd_cgdb_data_flow,
    cmd_cgdb_race_check, cmd_cgdb_index_status, cmd_cgdb_versions,
    cmd_cgdb_find_invokers, cmd_cgdb_find_invoked, cmd_cgdb_path,
    cmd_cgdb_definition, cmd_cgdb_function_body, cmd_cgdb_struct_layout,
    cmd_cgdb_type_definition, cmd_cgdb_nodes_under_config,
    cmd_cgdb_path_feasible, cmd_cgdb_schema_version, cmd_cgdb_sql,
    cmd_cgdb_views, cmd_cgdb_get_source, cmd_cgdb_layer_summary,
    cmd_cgdb_coverage, cmd_cgdb_write_coverage,
)
from _builder.cmd_report_tools import (
    cmd_render_source, cmd_verify_consistency, cmd_edit_token,
    cmd_insert_token, cmd_delete_token, cmd_find_macros,
    cmd_get_pp_branches, cmd_get_string_literals,
    cmd_commit_db_transaction, cmd_rollback_db_transaction,
    cmd_insert_node_after, cmd_delete_node, cmd_add_function,
)
from _builder.runtime_guards import cmd_runtime_guards
from _builder.path_feasibility import cmd_path_feasible, cmd_path_guards
from _builder.doc_code_align import (
    cmd_doc_code_check, cmd_doc_mark_stale, cmd_doc_alignment_report,
    cmd_doc_signature_diff,
)
from _builder.profile_health import (
    cmd_profile_health, cmd_profile_evolve, cmd_profile_bind_version,
)


def _lazy(module_path: str, func_name: str):
    """Create a lazy-import wrapper for a command handler.

    The heavy module is imported only when the command is actually
    invoked, not at CLI startup time. This saves ~200-400ms of import
    overhead for common commands (search, describe, trace) that don't
    need daemon/web_ui/mcp_server/embeddings/bug_benchmark.
    """
    def wrapper(args):
        mod = __import__(module_path, fromlist=[func_name])
        fn = getattr(mod, func_name)
        return fn(args)
    wrapper.__name__ = func_name
    return wrapper


def cmd_kb_rebuild_index(args):
    """Rebuild the unified kb_paragraphs FTS5 index from filesystem sources."""
    summary = cmd_kb_rebuild_index_impl(args.graph, verbose=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_kb_query(args):
    """Unified FTS5+BM25 query across memory + knowledge."""
    from _builder.kb_index import query_kb
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()] if args.kinds else None
    results = query_kb(
        graph_dir=args.graph,
        query=args.query,
        top_n=args.top,
        kinds=kinds,
        min_weight=args.min_weight,
        max_tokens=args.max_tokens,
        semantic=getattr(args, 'semantic', False),
    )
    # Phase 8: fall back to global KB if no project matches
    if not results and getattr(args, 'global', False):
        from _builder.kb_global import global_search
        results = global_search(args.query, top_n=args.top)
    if not results:
        print("No matches found.")
        return
    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_kb_cluster(args):
    """Phase 4: cluster kb_paragraphs by FTS5 similarity."""
    from _builder.kb_cluster import cluster_kb
    summary = cluster_kb(args.graph, threshold=args.threshold, verbose=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_kb_migrate(args):
    """Phase 6: migrate kb_paragraphs → kb_items (fact-level)."""
    from _builder.kb_index import _kb_connect
    conn = _kb_connect(args.graph)
    if conn is None:
        print("No code2database.db found")
        sys.exit(1)
    try:
        # Copy rows; kb_items gets the same id, title, body, tags etc.
        # plus defaults for new columns (decay_class, provenance_*)
        rows = conn.execute(
            "SELECT id, source_kind AS kind_proxy, source_file, "
            "       para_index, title, body, tags, node_ids, weight, "
            "       confidence, kind, graph_version, created_at, "
            "       accessed_at, access_count, scope_id, canonical_id, "
            "       principle_ref, embedding FROM kb_paragraphs"
        ).fetchall()
        # Clear kb_items first
        conn.execute("DELETE FROM kb_items")
        migrated = 0
        for r in rows:
            # Map source_kind → decay_class
            sk = r["kind_proxy"]
            if sk == "knowledge":
                decay_class = "none"
            elif sk == "memory":
                decay_class = "soft"
            else:
                decay_class = "soft"
            try:
                conn.execute(
                    "INSERT INTO kb_items (id, kind, scope_id, canonical_id, "
                    "  principle_ref, title, body, tags, node_ids, "
                    "  weight, confidence, decay_class, graph_version, "
                    "  embedding, created_at, accessed_at, access_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["id"], r["kind"], r["scope_id"], r["canonical_id"],
                     r["principle_ref"], r["title"], r["body"], r["tags"],
                     r["node_ids"], r["weight"], r["confidence"],
                     decay_class, r["graph_version"], r["embedding"],
                     r["created_at"], r["accessed_at"],
                     r["access_count"] or 0)
                )
                migrated += 1
            except Exception:
                pass
        conn.commit()
        print(json.dumps({"migrated": migrated}, ensure_ascii=False, indent=2))
    finally:
        conn.close()


def cmd_kb_known_unknowns(args):
    """Phase 9: list queries that returned no matches."""
    from _builder.kb_index import get_known_unknowns
    results = get_known_unknowns(args.graph, top_n=args.top,
                                 min_occurrences=args.min_occurrences)
    if not results:
        print("No known unknowns (all queries matched or no queries logged).")
        return
    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_kb_audit(args):
    """Phase 10: audit KB."""
    from _builder.kb_audit import audit_kb
    result = audit_kb(args.graph, topic=args.topic)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_kb_conflict(args):
    """Phase 11: detect contradictory items."""
    from _builder.kb_conflict import detect_conflicts
    conflicts = detect_conflicts(args.graph)
    if not conflicts:
        print("No conflicts detected.")
        return
    print(json.dumps({"conflict_count": len(conflicts), "conflicts": conflicts},
                     ensure_ascii=False, indent=2))


def cmd_kb_rollback(args):
    """Phase 11: restore a kb_item to a prior version."""
    from _builder.kb_conflict import rollback_kb_item
    result = rollback_kb_item(args.graph, args.id, args.to_version)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_kb_forget(args):
    """Phase 11: immediately delete a kb_paragraph."""
    from _builder.kb_conflict import forget_kb_paragraph
    result = forget_kb_paragraph(args.graph, args.id, reason=args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_kb_global_add(args):
    """Phase 8: add to global KB."""
    from _builder.kb_global import global_add
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    entry_id = global_add(
        title=args.title, body=args.body, tags=tags, kind=args.kind,
        source_project=args.source_project, source_file=args.source_file,
    )
    print(json.dumps({"added": True, "id": entry_id}, ensure_ascii=False, indent=2))


def cmd_kb_global_search(args):
    """Phase 8: search global KB."""
    from _builder.kb_global import global_search
    results = global_search(args.query, top_n=args.top)
    if not results:
        print("No matches in global KB.")
        return
    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_kb_global_share(args):
    """Phase 8: export global KB."""
    from _builder.kb_global import global_share
    output = global_share(args.output)
    print(json.dumps({"exported": True, "path": output}, ensure_ascii=False, indent=2))


def cmd_kb_global_import(args):
    """Phase 8: import shared global KB JSON."""
    from _builder.kb_global import global_import
    imported = global_import(args.input)
    print(json.dumps({"imported": imported}, ensure_ascii=False, indent=2))


def cmd_build_multi(args):
    """Phase 1: build unified C2D from multi-project manifest."""
    from _builder.build_multi import build_multi
    summary = build_multi(
        manifest_path=args.manifest,
        outdir=args.outdir,
        jobs=getattr(args, "jobs", 0),
        force_rescan=[s.strip() for s in (args.force_rescan or "").split(",")
                      if s.strip()] or None,
        no_clang=getattr(args, "no_clang", False),
        verbose=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_c2d_add_foreign(args):
    """Phase 1: register foreign C2D + resolve refs."""
    from _builder.c2d_foreign import add_foreign
    summary = add_foreign(
        graph_dir=args.graph,
        foreign_c2d_path=args.foreign_c2d,
        project_name=getattr(args, "project_name", "") or "",
        rescan_unresolved=getattr(args, "rescan_unresolved", False),
        verbose=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_c2d_sync_foreign(args):
    """Phase 1: sync foreign_refs with updated foreign C2Ds."""
    from _builder.c2d_foreign import sync_foreign
    summary = sync_foreign(
        graph_dir=args.graph,
        foreign_c2d_path=getattr(args, "foreign_c2d", "") or "",
        verbose=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_c2d_list_foreign(args):
    """Phase 1: list watched foreign C2Ds."""
    from _builder.c2d_foreign import list_foreign
    result = list_foreign(args.graph)
    if not result:
        print("No watched foreign C2Ds.")
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_c2d_remove_foreign(args):
    """Phase 1: unregister a foreign C2D."""
    from _builder.c2d_foreign import remove_foreign
    summary = remove_foreign(args.graph, args.foreign_c2d)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_composite_query(args):
    """Phase 2: cross-C2D query via ATTACH."""
    from _builder.c2d_phase2 import composite_query
    foreign_c2ds = []
    if args.foreign_c2d:
        foreign_c2ds = [s.strip() for s in args.foreign_c2d.split(",")
                        if s.strip()]
    result = composite_query(
        graph_dir=args.graph,
        query=args.query,
        foreign_c2ds=foreign_c2ds,
        top_n=args.top,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_c2d_check_compat(args):
    """Phase 2: check B's foreign_refs against new A version."""
    from _builder.c2d_phase2 import check_compat
    result = check_compat(
        graph_dir=args.graph,
        against_c2d=args.against_c2d,
        verbose=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_coverage_cross_c2d(args):
    """Phase 2: test coverage across C2Ds."""
    from _builder.c2d_phase2 import coverage_cross_c2d
    result = coverage_cross_c2d(
        test_c2d=args.test_c2d,
        target_c2d=args.target_c2d,
        verbose=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_c2d_add_foreign_stub(args):
    """Phase 3: register vendor stub C2D."""
    from _builder.c2d_phase3 import add_foreign_stub
    summary = add_foreign_stub(
        graph_dir=args.graph,
        stub_c2d_path=args.stub_c2d,
        project_name=getattr(args, "project_name", "") or "",
        verbose=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_ffi_auto_link(args):
    """Phase 3: auto-link FFI bindings to foreign C2Ds."""
    from _builder.c2d_phase3 import auto_link_ffi_to_foreign
    summary = auto_link_ffi_to_foreign(args.graph, verbose=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_scan_rpc(args):
    """Phase 3: scan source for RPC client calls."""
    from _builder.c2d_phase3 import scan_rpc_edges
    summary = scan_rpc_edges(args.graph, verbose=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_import_foreign_knowledge(args):
    """Phase 3: copy foreign knowledge/*.md into local."""
    from _builder.c2d_phase3 import import_foreign_knowledge
    summary = import_foreign_knowledge(
        graph_dir=args.graph,
        foreign_c2d_path=args.foreign_c2d,
        project_name=getattr(args, "project_name", "") or "",
        verbose=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_install_hook(args):
    """Install git post-commit hook for auto quick-update after commits."""
    import shutil
    source_root = args.source
    graph_dir_name = args.graph_dir

    hooks_dir = os.path.join(source_root, ".git", "hooks")
    if not os.path.isdir(hooks_dir):
        print(f"Error: {hooks_dir} not found. Is this a git repo?", file=sys.stderr)
        sys.exit(1)

    # Source hook template
    hook_src = os.path.join(os.path.dirname(__file__), "hooks", "post-commit")
    if not os.path.exists(hook_src):
        print(f"Error: Hook template not found at {hook_src}", file=sys.stderr)
        sys.exit(1)

    hook_dst = os.path.join(hooks_dir, "post-commit")

    # Check if hook already exists
    if os.path.exists(hook_dst):
        with open(hook_dst, "r") as f:
            existing = f.read()
        if "Code2Database" in existing or "code2database_builder" in existing:
            print(f"Hook already installed at {hook_dst}")
            return
        # Append to existing hook
        with open(hook_dst, "a") as f:
            f.write("\n# Code2Database auto quick-update\n")
            with open(hook_src, "r") as src:
                f.write(src.read())
        print(f"Appended Code2Database hook to existing {hook_dst}")
    else:
        shutil.copy2(hook_src, hook_dst)
        os.chmod(hook_dst, 0o755)
        print(f"Installed post-commit hook at {hook_dst}")

    print(f"Hook will run: quick-update --source {source_root} --graph {source_root}/{graph_dir_name}")
    print("Set CALLGRAPH_DIR env var to change the graph directory name (default: .callgraph)")


def main():
    parser = argparse.ArgumentParser(description="Call graph builder and query tool")
    # Global logging flags — accepted before the subcommand.
    parser.add_argument("--log-level", default="INFO",
                        help="Log level: DEBUG|INFO|WARNING|ERROR|CRITICAL (default: INFO)")
    parser.add_argument("--log-file", default=None,
                        help="Optional path to write structured logs (in addition to stderr)")
    parser.add_argument("--log-json", action="store_true",
                        help="Emit JSON-line logs (machine-parseable)")
    sub = parser.add_subparsers(dest="command")

    # build
    p_build = sub.add_parser("build", help="Build invocation graph from extraction JSON")
    p_build.add_argument("--extraction", required=True, help="Extraction JSON from code2database_scanner.py")
    p_build.add_argument("--outdir", required=True, help="Output directory for domain-split JSON files")
    p_build.add_argument("--max-domain-files", type=int, default=50,
                          help="Max domain JSON files per subdirectory (default: 50, 0=flat)")
    p_build.add_argument("--plugin", action="append", default=[],
                          help="Python plugin file to load (can specify multiple). "
                               "Also auto-discovers .code2database_plugins/*.py")
    p_build.add_argument("--build-config", default=None,
                          help="Build config: 'auto' for auto-detection, path to config file, "
                               "or build type name (e.g. 'Release', 'Debug')")
    p_build.add_argument("--macros", default=None,
                          help="Space-separated macro bindings for #ifdef resolution "
                               "(e.g., 'NDEBUG FEATURE_X=1 -DFOO'). Merged with build-config.")
    p_build.add_argument("--plugin-config", default=None,
                         help='Plugin config JSON, e.g. \'{"threshold":0.8}\'')
    p_build.add_argument("--profile", default=None,
                         help="Project profile JSON file for endpoint classification "
                              "(e.g., scripts/config/profiles/spdk.json)")
    p_build.add_argument("--memory-warn-threshold", type=float, default=0.75,
                         help="Memory usage threshold (0.0-1.0) to trigger warnings (default: 0.75)")
    p_build.add_argument("--memory-crit-threshold", type=float, default=0.85,
                         help="Memory usage threshold (0.0-1.0) to trigger critical actions (default: 0.85)")
    p_build.add_argument("--memory-warn-mb", type=float, default=None,
                         help="Absolute warn cap in MB (overrides --memory-warn-threshold when set). "
                              "Recommended for systems where a fixed fraction would either OOM too "
                              "eagerly or never trigger (e.g., 16GB system with 0.85 = 13.6GB).")
    p_build.add_argument("--memory-crit-mb", type=float, default=None,
                         help="Absolute critical cap in MB (overrides --memory-crit-threshold when set). "
                              "Auto-clamped to <= total RAM * 0.9 when --memory-dynamic is on (default).")
    p_build.add_argument("--memory-dynamic", dest="memory_dynamic",
                         action="store_true", default=True,
                         help="Auto-clamp critical threshold to a safe absolute ceiling based on "
                              "available RAM (default: on). Use --no-memory-dynamic to disable.")
    p_build.add_argument("--no-memory-dynamic", dest="memory_dynamic",
                         action="store_false",
                         help="Disable dynamic clamping; use pure fractional thresholds.")
    p_build.add_argument("--memory-stats", default=None,
                         help="Optional path to write memory stats JSON during build.")
    p_build.add_argument("--large-project", action="store_true",
                         help="Optimize for very large projects (Linux kernel scale). "
                              "More aggressive memory management.")
    p_build.add_argument("--storage", choices=["json", "sqlite", "auto"], default="auto",
                         help="Storage backend: auto (sqlite for >100K nodes, else json), json, or sqlite")
    p_build.add_argument("--skip-community", action="store_true",
                         help="Skip Leiden community detection (faster for large projects)")
    p_build.add_argument("--low-memory", action="store_true",
                         help="Maximize memory savings: strip body_text early, "
                              "skip community detection, use streaming SQLite export, "
                              "and build indexes from SQLite instead of NetworkX")
    p_build.add_argument("--profile-timing", action="store_true", default=True,
                         help="O28: Print per-stage timing breakdown to stderr at end of build "
                              "(default: on). Use --no-profile-timing to disable.")
    p_build.add_argument("--no-profile-timing", dest="profile_timing",
                         action="store_false",
                         help="Disable per-stage timing output.")
    p_build.add_argument("--auto-enhance", dest="auto_enhance",
                         action="store_true", default=True,
                         help="After build, fill empty semantic fields on high-value nodes "
                              "(API-entry, thread-processors, hub functions). Always runs "
                              "the heuristic generator; also runs LLM if ANTHROPIC_API_KEY "
                              "or CLAUDE_CODE env var is set. (default: enabled)")
    p_build.add_argument("--no-auto-enhance", dest="auto_enhance",
                         action="store_false",
                         help="Disable post-build auto-enhancement.")
    p_build.add_argument("-j", "--jobs", type=int, default=0,
                         help="Parallel build workers (0=auto, 1=sequential, "
                              "N=N threads). Speeds up per-node extraction "
                              "loops (state_access, auto-enhance). Auto-capped "
                              "on very large graphs to bound memory.")

    # load
    p_load = sub.add_parser("load", help="Load and summarize the invocation graph")
    p_load.add_argument("--graph", required=True, help="Call graph output directory")
    p_load.add_argument("--summary", action="store_true", help="Show detailed summary")

    # search
    p_search = sub.add_parser("search", help="Search nodes by keywords")
    p_search.add_argument("--graph", required=True)
    p_search.add_argument("--keywords", required=True, help="Space-separated keywords")
    p_search.add_argument("--top", type=int, default=20)
    p_search.add_argument("--max-tokens", type=int, default=500, help="Max output tokens (0=unlimited)")
    p_search.add_argument("--json", action="store_true", help="Output as JSON")

    # describe-node
    p_desc = sub.add_parser("describe-node",
                             help="Get info about a node. Use --detail brief|standard|full to control output size")
    p_desc.add_argument("--graph", required=True, help="Call graph output directory")
    p_desc.add_argument("--node", required=True, help="Node ID (or partial match)")
    p_desc.add_argument("--detail", choices=["brief", "standard", "full"], default="full",
                         help="Output detail level: brief(~200t), standard(~500t), full(~900t)")
    p_desc.add_argument("--context", action="store_true",
                         help="Include hub role, reachable APIs, and reached endpoints")
    p_desc.add_argument("--include-body", action="store_true",
                         help="Include function body text (full mode only; omitted by default to save tokens)")
    p_desc.add_argument("--max-tokens", type=int, default=800,
                         help="Max output tokens (0=unlimited). Automatically drops low-priority fields.")
    p_desc.add_argument("--json", action="store_true", help="Output as JSON (default for describe-node)")
    p_desc.add_argument("--fields", default=None,
                        help="Comma-separated fields to include (e.g. 'signature,params,callers')")

    # resolve-chain
    p_resolve = sub.add_parser("resolve-chain",
                               help="Trace call chain from a node with variable bindings to prune dead branches")
    p_resolve.add_argument("--graph", required=True, help="Call graph output directory")
    p_resolve.add_argument("--node", required=True, help="Start node ID")
    p_resolve.add_argument("--bindings", default="", help='Variable bindings, e.g. "mode=1,flag=true"')
    p_resolve.add_argument("--json", action="store_true", help="Output as JSON")

    # io-path: interactive IO path tracing with vtable dispatch resolution
    p_io_path = sub.add_parser("io-path",
                               help="Trace IO path from a function, auto-detecting vtable dispatch options")
    p_io_path.add_argument("--graph", required=True, help="Call graph output directory")
    p_io_path.add_argument("--from", dest="from_node", required=True,
                           help="Start function name or ID")
    p_io_path.add_argument("--to", dest="to_node", default="",
                           help="Target function name or ID (optional)")
    p_io_path.add_argument("--bindings", default="",
                           help='Variable bindings, e.g. "FEATURE_X=1,module=storage"')
    p_io_path.add_argument("--max-nodes", dest="max_nodes", type=int, default=100,
                           help="Max nodes to explore in IO path (default: 100)")
    p_io_path.add_argument("--json", action="store_true", help="Output as JSON")

    # param-flow: trace how a parameter flows through the call chain
    p_pf = sub.add_parser("param-flow",
                          help="Trace parameter flow through the call chain (cross-function)")
    p_pf.add_argument("--graph", required=True, help="Call graph output directory")
    p_pf.add_argument("--from", dest="from_node", required=True,
                      help="Start function name or ID")
    p_pf.add_argument("--param", required=True,
                      help="Parameter name to track")
    p_pf.add_argument("--max-depth", type=int, default=10,
                      help="Max trace depth (default: 10)")
    p_pf.add_argument("--json", action="store_true", help="Output as JSON")

    # neighbors
    p_nb = sub.add_parser("neighbors", help="Get neighbors of a node")
    p_nb.add_argument("--graph", required=True)
    p_nb.add_argument("--node", required=True, help="Node ID to explore")
    p_nb.add_argument("--depth", type=int, default=2)
    p_nb.add_argument("--max-results", type=int, default=200,
                     help="Cap on total neighbors returned (prevents runaway BFS on high-degree nodes)")
    p_nb.add_argument("--json", action="store_true", help="Output as JSON")

    # path
    p_path = sub.add_parser("path", help="Find shortest call path between two nodes")
    p_path.add_argument("--graph", required=True)
    p_path.add_argument("--from", dest="from_node", required=True)
    p_path.add_argument("--to", dest="to_node", required=True)
    p_path.add_argument("--bindings", default="",
                         help="Condition bindings for path filtering (e.g., 'SPDK_CONFIG_APP_RW=1')")
    p_path.add_argument("--no-condition-filter", action="store_true",
                         help="Disable condition/concurrency filtering (show all paths)")
    p_path.add_argument("--no-prefer-same-domain", dest="prefer_same_domain",
                         action="store_false", default=True,
                         help="Disable same-domain preference for vtable_dispatch edges (may produce cross-filesystem false-positive paths)")
    p_path.add_argument("--strict-vtable-domain", dest="strict_vtable_domain",
                         action="store_true", default=False,
                         help="Completely exclude cross-domain vtable_dispatch edges (no fallback). Use to verify whether a path exists within a single subsystem.")
    p_path.add_argument("--vtable-bind", dest="vtable_bind", default="",
                         help="Bind specific vtable type to a single implementation (e.g., 'super_operations=ext4_evict_inode,address_space_operations=ext4_write_end'). Only dispatches to the bound impl are followed.")
    p_path.add_argument("--json", action="store_true", help="Output as JSON")

    # impact
    p_impact = sub.add_parser("impact", help="Impact analysis for a node")
    p_impact.add_argument("--graph", required=True)
    p_impact.add_argument("--node", required=True)
    p_impact.add_argument("--direction", choices=["reverse", "forward"], default="reverse",
                           help="reverse=who calls this, forward=what this calls")
    p_impact.add_argument("--lite", action="store_true",
                           help="Lite mode: only return counts and domains, no node lists")
    p_impact.add_argument("--json", action="store_true", help="Output as JSON")

    # domain
    p_domain = sub.add_parser("domain", help="List all nodes/edges in a domain")
    p_domain.add_argument("--graph", required=True)
    p_domain.add_argument("--name", required=True, help="Domain name (e.g. lib.bdev)")

    # merge
    p_merge = sub.add_parser("merge", help="Merge new extraction into existing graph")
    p_merge.add_argument("--graph", required=True, help="Existing invocation graph directory")
    p_merge.add_argument("--extraction", required=True, help="New extraction JSON to merge")

    # classify-endpoints
    p_classify = sub.add_parser("classify-endpoints",
                                help="Apply LLM endpoint classification to the graph")
    p_classify.add_argument("--graph", required=True, help="Call graph output directory")

    # update
    p_update = sub.add_parser("update", help="Incremental update: re-scan changed files and merge")
    p_update.add_argument("--source", required=True, help="Source directory")
    p_update.add_argument("--graph", required=True, help="Call graph output directory")
    p_update.add_argument("--extraction", help="Pre-scanned extraction JSON (skip auto-scan if given)")

    # sync
    p_sync = sub.add_parser("sync", help="Sync local code2db-out with git-tracked version (local wins)")
    p_sync.add_argument("--graph", required=True, help="Local code2db-out directory")
    p_sync.add_argument("--remote", default="origin", help="Git remote name (default: origin)")
    p_sync.add_argument("--branch", default="", help="Git branch (default: auto-detect current upstream)")
    p_sync.add_argument("--git-path", default="", help="Path to git-tracked code2db-out (bypass git fetch)")
    p_sync.add_argument("--dry-run", action="store_true", help="Show what would change without writing")

    # extract-semantics
    p_sem = sub.add_parser("extract-semantics", help="Export nodes for LLM semantic description")
    p_sem.add_argument("--graph", required=True, help="Call graph output directory")
    p_sem.add_argument("--docs", default="", help="Documentation directory for semantic extraction")

    # apply-semantics
    p_apply_sem = sub.add_parser("apply-semantics", help="Apply LLM semantic descriptions to graph")
    p_apply_sem.add_argument("--graph", required=True, help="Call graph output directory")

    # update-node (LLM-driven incremental node attribute supplement)
    p_un = sub.add_parser("update-node",
                          help="LLM-driven incremental supplement of node attributes (non-destructive, requires user confirmation)")
    p_un.add_argument("--graph", required=True, help="Call graph output directory")
    p_un.add_argument("--node", required=True,
                      help="Node ID or name (partial match supported)")
    p_un.add_argument("--attr", action="append", default=[],
                      help="key=value to supplement (value may be JSON). Repeatable. "
                           "Stored as {key}_supplemented to preserve original scan data.")
    p_un.add_argument("--source", default="llm_supplement",
                      choices=["llm_supplement", "manual", "auto_detect"],
                      help="Provenance label for the supplement (default: llm_supplement)")
    p_un.add_argument("--confidence", default="EXTRACTED",
                      choices=["EXTRACTED", "INFERRED", "AMBIGUOUS"],
                      help="Confidence level (default: EXTRACTED — use when LLM read source and confirmed)")
    p_un.add_argument("--yes", "-y", action="store_true",
                      help="Bypass confirmation prompt (for automation; LLM should NOT use this without explicit user permission)")

    # update-edge (LLM-driven incremental edge attribute supplement)
    p_ue = sub.add_parser("update-edge",
                          help="LLM-driven incremental supplement of edge attributes (non-destructive, requires user confirmation)")
    p_ue.add_argument("--graph", required=True, help="Call graph output directory")
    p_ue.add_argument("--from", dest="from_node", required=True,
                      help="Caller node ID or name")
    p_ue.add_argument("--to", dest="to_node", required=True,
                      help="Callee node ID or name")
    p_ue.add_argument("--attr", action="append", default=[],
                      help="key=value to supplement (value may be JSON). Repeatable. "
                           "Supported keys: call_condition, concurrency, confidence, callee_args, etc.")
    p_ue.add_argument("--source", default="llm_supplement",
                      choices=["llm_supplement", "manual", "auto_detect"],
                      help="Provenance label for the supplement (default: llm_supplement)")
    p_ue.add_argument("--confidence", default="EXTRACTED",
                      choices=["EXTRACTED", "INFERRED", "AMBIGUOUS"],
                      help="Confidence level (default: EXTRACTED)")
    p_ue.add_argument("--yes", "-y", action="store_true",
                      help="Bypass confirmation prompt (for automation; LLM should NOT use this without explicit user permission)")

    # patch-profile (LLM-driven incremental auto-profile calibration)
    p_pp = sub.add_parser("patch-profile",
                          help="LLM-driven incremental calibration of auto-profile (non-destructive, requires user confirmation)")
    p_pp.add_argument("--graph", required=True, help="Call graph output directory")
    p_pp.add_argument("--source", default="llm_calibrate",
                      choices=["llm_calibrate", "manual", "auto_detect"],
                      help="Provenance label for the calibration (default: llm_calibrate)")
    # project_boundaries
    p_pp.add_argument("--add-non-api-path", action="append", default=[],
                      help="Add a non-API path substring (e.g., 'samples/'). Repeatable.")
    p_pp.add_argument("--remove-non-api-path", action="append", default=[],
                      help="Remove a non-API path. Repeatable.")
    p_pp.add_argument("--add-test-path-pattern", action="append", default=[],
                      help="Add a test path pattern. Repeatable.")
    p_pp.add_argument("--remove-test-path-pattern", action="append", default=[],
                      help="Remove a test path pattern. Repeatable.")
    p_pp.add_argument("--add-test-file-suffix", action="append", default=[],
                      help="Add a test file suffix. Repeatable.")
    p_pp.add_argument("--remove-test-file-suffix", action="append", default=[],
                      help="Remove a test file suffix. Repeatable.")
    p_pp.add_argument("--add-test-domain-segment", action="append", default=[],
                      help="Add a test domain segment. Repeatable.")
    p_pp.add_argument("--remove-test-domain-segment", action="append", default=[],
                      help="Remove a test domain segment. Repeatable.")
    p_pp.add_argument("--add-vendor-prefix", action="append", default=[],
                      help="Add a vendor domain prefix. Repeatable.")
    p_pp.add_argument("--remove-vendor-prefix", action="append", default=[],
                      help="Remove a vendor domain prefix. Repeatable.")
    p_pp.add_argument("--add-external-dir-prefix", action="append", default=[],
                      help="Add an external dir prefix. Repeatable.")
    p_pp.add_argument("--remove-external-dir-prefix", action="append", default=[],
                      help="Remove an external dir prefix. Repeatable.")
    # concurrency_patterns
    p_pp.add_argument("--add-lock-acquire-pattern", action="append", default=[],
                      help="Add a lock acquire regex pattern. Repeatable.")
    p_pp.add_argument("--remove-lock-acquire-pattern", action="append", default=[],
                      help="Remove a lock acquire pattern. Repeatable.")
    p_pp.add_argument("--add-lock-release-pattern", action="append", default=[],
                      help="Add a lock release regex pattern. Repeatable.")
    p_pp.add_argument("--remove-lock-release-pattern", action="append", default=[],
                      help="Remove a lock release pattern. Repeatable.")
    # io_classification
    p_pp.add_argument("--add-io-main-keyword", action="append", default=[],
                      help="Add an io_main keyword. Repeatable.")
    p_pp.add_argument("--remove-io-main-keyword", action="append", default=[],
                      help="Remove an io_main keyword. Repeatable.")
    p_pp.add_argument("--add-io-side-keyword", action="append", default=[],
                      help="Add an io_side keyword. Repeatable.")
    p_pp.add_argument("--remove-io-side-keyword", action="append", default=[],
                      help="Remove an io_side keyword. Repeatable.")
    p_pp.add_argument("--yes", "-y", action="store_true",
                      help="Bypass confirmation prompt (for automation; LLM should NOT use this without explicit user permission)")

    # think-chain
    p_think = sub.add_parser("think-chain", help="Generate complete call chains for structured analysis")
    p_think.add_argument("--graph", required=True, help="Call graph output directory")
    p_think.add_argument("--output", help="Output file (default: graph-dir/.code2database_think_chain.json)")
    p_think.add_argument("--max-depth", type=int, default=10, help="Max path depth (default: 10)")

    # extract-signals
    p_sig = sub.add_parser("extract-signals", help="Extract #ifdef condition→affected edges map")
    p_sig.add_argument("--graph", required=True, help="Call graph output directory")
    p_sig.add_argument("--output", help="Output file (default: graph-dir/.code2database_signal_map.json)")

    # save-memory
    p_save_mem = sub.add_parser("save-memory", help="Save Q&A memory with call chains")
    p_save_mem.add_argument("--graph", required=True, help="Call graph output directory")
    p_save_mem.add_argument("--question", required=True, help="The question asked")
    p_save_mem.add_argument("--answer", default="", help="The answer given")
    p_save_mem.add_argument("--chains", default="", help="JSON string of call chain data")
    p_save_mem.add_argument("--tags", default="", help="Comma-separated tags")
    p_save_mem.add_argument("--node-ids", default="", help="Comma-separated node IDs this memory depends on")
    p_save_mem.add_argument("--no-merge", action="store_true", help="Don't merge with similar existing entry")

    # search-memory
    p_search_mem = sub.add_parser("search-memory", help="Search memory for similar questions")
    p_search_mem.add_argument("--graph", required=True, help="Call graph output directory")
    p_search_mem.add_argument("--query", required=True, help="Search query")
    p_search_mem.add_argument("--top", type=int, default=5, help="Max results")

    # validate-memory
    p_val_mem = sub.add_parser("validate-memory",
                               help="Validate memory against current graph; invalidate stale → experience")
    p_val_mem.add_argument("--graph", required=True, help="Call graph output directory")

    # export-html
    p_html = sub.add_parser("export-html", help="Export invocation graph as interactive HTML")
    p_html.add_argument("--graph", required=True, help="Call graph output directory")
    p_html.add_argument("--output", help="Output HTML file path")
    p_html.add_argument("--max-nodes", type=int, default=500, help="Max nodes per HTML file before splitting by domain")
    p_html.add_argument("--format", choices=["vis-network", "mermaid"],
                        help="HTML format (if omitted, prompts with pros/cons): "
                             "vis-network=interactive drag/zoom, handles 500+ nodes, needs internet; "
                             "mermaid=static, self-contained, Git-friendly, best for <200 nodes")

    # plugins
    p_plugins = sub.add_parser("plugins", help="List available callgraph plugins")
    p_plugins.add_argument("--source", default=".", help="Source root to search for .code2database_plugins/")
    p_plugins.add_argument("--plugin", action="append", default=[], help="Additional plugin path to check")

    # manage-memory
    p_mgmt = sub.add_parser("manage-memory", help="Manage persistent memory (add/correct/reshape/decay/promote/refine/query/pack/consolidate/export/import/scratch-*)")
    p_mgmt.add_argument("--graph", required=True, help="Call graph output directory")
    p_mgmt.add_argument("--action", required=True,
                         choices=["add", "correct", "reshape", "decay", "promote", "refine", "query", "pack",
                                  "consolidate", "export", "import", "scratch-save", "scratch-restore",
                                  "scratch-list", "scratch-cleanup"],
                         help="Action to perform")
    p_mgmt.add_argument("--question", default="", help="Question (for add/refine/query)")
    p_mgmt.add_argument("--answer", default="", help="Answer (for add/correct/reshape/refine)")
    p_mgmt.add_argument("--tags", default="", help="Comma-separated tags (for add/refine)")
    p_mgmt.add_argument("--node-ids", default="", help="Comma-separated node IDs (for add)")
    p_mgmt.add_argument("--id", default="0", help="Memory ID (for correct/promote)")
    p_mgmt.add_argument("--field", default="", help="Field name (for correct)")
    p_mgmt.add_argument("--value", default="", help="Field value (for correct)")
    p_mgmt.add_argument("--root-id", default="0", help="Root memory ID (for reshape)")
    p_mgmt.add_argument("--scratch-id", default="", help="Scratch session ID (for refine)")
    p_mgmt.add_argument("--boost", default="1.0", help="Weight boost (for promote)")
    p_mgmt.add_argument("--top", default="5", help="Max results (for query)")
    p_mgmt.add_argument("--min-weight", default="0.3", help="Min weight filter (for query)")
    p_mgmt.add_argument("--tier", default="lite", choices=["lite", "standard", "deep", "full"], help="Pack tier (for pack)")
    p_mgmt.add_argument("--query", default="", help="Query text (for query)")
    p_mgmt.add_argument("--output", default="", help="Output path (for export)")
    p_mgmt.add_argument("--input", default="", help="Input JSON path (for import)")
    p_mgmt.add_argument("--session-id", default="", help="Session ID (for scratch-*)")
    p_mgmt.add_argument("--chains", default="", help="JSON chains (for scratch-save)")
    p_mgmt.add_argument("--params", default="", help="JSON param bindings (for scratch-save)")
    p_mgmt.add_argument("--react", default="", help="JSON ReAct state (for scratch-save)")
    p_mgmt.add_argument("--ttl", default="24", help="TTL in hours (for scratch-save)")
    p_mgmt.add_argument("--merge", default=True, action=argparse.BooleanOptionalAction,
                         help="Merge on import (for import)")

    # memory-health
    p_mh = sub.add_parser("memory-health", help="Report memory system health statistics")
    p_mh.add_argument("--graph", required=True, help="Call graph output directory")

    # extract-knowledge
    p_ek = sub.add_parser("extract-knowledge", help="Extract knowledge from docs and graph")
    p_ek.add_argument("--graph", required=True, help="Call graph output directory")
    p_ek.add_argument("--source", default="", help="Source root directory")
    p_ek.add_argument("--docs", default="", help="Documentation directory or file")

    # apply-knowledge
    p_ak = sub.add_parser("apply-knowledge", help="Apply LLM knowledge to knowledge directory")
    p_ak.add_argument("--graph", required=True, help="Call graph output directory")

    # knowledge-query
    p_kq = sub.add_parser("knowledge-query", help="Query knowledge by topic")
    p_kq.add_argument("--graph", required=True, help="Call graph output directory")
    p_kq.add_argument("--topic", required=True, help="Topic to search")
    p_kq.add_argument("--max-tokens", type=int, default=500, help="Max output tokens")

    # knowledge-validate
    p_kv = sub.add_parser("knowledge-validate", help="Validate knowledge against current graph")
    p_kv.add_argument("--graph", required=True, help="Call graph output directory")

    # kb-rebuild-index (Phase 1: unified FTS5 index rebuild)
    p_kri = sub.add_parser("kb-rebuild-index",
                           help="Rebuild the unified kb_paragraphs FTS5 index "
                                "from memory/*.json and knowledge/*.md")
    p_kri.add_argument("--graph", required=True, help="Call graph output directory")

    # kb-query (Phase 3: unified FTS5+BM25 query across memory + knowledge)
    p_kq2 = sub.add_parser("kb-query",
                           help="Unified FTS5+BM25 query across memory and knowledge")
    p_kq2.add_argument("--graph", required=True, help="Call graph output directory")
    p_kq2.add_argument("--query", required=True, help="Free-form text query")
    p_kq2.add_argument("--top", type=int, default=10, help="Max results (default 10)")
    p_kq2.add_argument("--kinds", default="",
                       help="Comma-separated kind filter (e.g., 'memory_qa,knowledge_principle')")
    p_kq2.add_argument("--min-weight", type=float, default=0.0,
                       help="Skip rows with weight below this (default 0.0 = no filter)")
    p_kq2.add_argument("--max-tokens", type=int, default=4000,
                       help="Approximate character cap on returned bodies")
    p_kq2.add_argument("--semantic", action="store_true",
                       help="Phase 5: enable semantic search (requires embeddings)")
    p_kq2.add_argument("--global", action="store_true",
                       help="Phase 8: fall back to global KB if project KB has no match")

    # kb-cluster (Phase 4: union-find clustering + principle_ref)
    p_kc = sub.add_parser("kb-cluster",
                          help="Cluster kb_paragraphs by FTS5 similarity + link principles")
    p_kc.add_argument("--graph", required=True, help="Call graph output directory")
    p_kc.add_argument("--threshold", type=float, default=0.5,
                      help="BM25 similarity threshold for clustering (default 0.5)")

    # kb-migrate (Phase 6: migrate kb_paragraphs → kb_items fact-level)
    p_km = sub.add_parser("kb-migrate",
                          help="Migrate kb_paragraphs rows into kb_items (fact-level)")
    p_km.add_argument("--graph", required=True, help="Call graph output directory")

    # kb-known-unknowns (Phase 9: aggregate unmatched queries)
    p_kku = sub.add_parser("kb-known-unknowns",
                           help="List queries that returned no matches (Phase 9)")
    p_kku.add_argument("--graph", required=True, help="Call graph output directory")
    p_kku.add_argument("--top", type=int, default=20, help="Max results")
    p_kku.add_argument("--min-occurrences", type=int, default=2,
                       help="Only show queries asked at least this many times")

    # kb-audit (Phase 10: knowledge audit)
    p_ka = sub.add_parser("kb-audit",
                          help="Audit KB: counts, stale, low-confidence, citations")
    p_ka.add_argument("--graph", required=True, help="Call graph output directory")
    p_ka.add_argument("--topic", default="", help="Optional: 'what do we know about X'")

    # kb-conflict (Phase 11: detect contradictions)
    p_kcf = sub.add_parser("kb-conflict",
                           help="Detect contradictory items in the same cluster")
    p_kcf.add_argument("--graph", required=True, help="Call graph output directory")

    # kb-rollback (Phase 11: restore kb_item to prior version)
    p_kr = sub.add_parser("kb-rollback",
                          help="Restore a kb_item to a prior version")
    p_kr.add_argument("--graph", required=True, help="Call graph output directory")
    p_kr.add_argument("--id", required=True, type=int, help="kb_item id to rollback")
    p_kr.add_argument("--to-version", type=int, default=None,
                       help="Version to restore (default: latest)")

    # kb-forget (Phase 11: immediate delete)
    p_kf = sub.add_parser("kb-forget",
                          help="Immediately delete a kb_paragraph (no decay)")
    p_kf.add_argument("--graph", required=True, help="Call graph output directory")
    p_kf.add_argument("--id", required=True, type=int, help="kb_paragraph id to forget")
    p_kf.add_argument("--reason", default="", help="Reason for forgetting (audit log)")

    # kb-global-* (Phase 8: cross-project global KB)
    p_kga = sub.add_parser("kb-global-add",
                           help="Add an entry to the cross-project global KB")
    p_kga.add_argument("--title", required=True)
    p_kga.add_argument("--body", required=True)
    p_kga.add_argument("--tags", default="")
    p_kga.add_argument("--kind", default="principle")
    p_kga.add_argument("--source-project", default="")
    p_kga.add_argument("--source-file", default="")

    p_kgs = sub.add_parser("kb-global-search",
                           help="Search the cross-project global KB")
    p_kgs.add_argument("--query", required=True)
    p_kgs.add_argument("--top", type=int, default=10)

    p_kgsh = sub.add_parser("kb-global-share",
                            help="Export global KB to a portable JSON file")
    p_kgsh.add_argument("--output", required=True, help="Output JSON path")

    p_kgi = sub.add_parser("kb-global-import",
                           help="Import a shared global KB JSON file")
    p_kgi.add_argument("--input", required=True, help="Input JSON path")

    # build-multi (Phase 1: multi-project aggregate build)
    p_bm = sub.add_parser("build-multi",
                          help="Build a unified C2D from a multi-project manifest")
    p_bm.add_argument("--manifest", required=True, help="Path to manifest JSON")
    p_bm.add_argument("--outdir", required=True, help="Output directory for joint C2D")
    p_bm.add_argument("-j", "--jobs", type=int, default=0,
                      help="Parallel workers (0=auto)")
    p_bm.add_argument("--force-rescan", default="",
                      help="Comma-separated project names to force re-scan")
    p_bm.add_argument("--no-clang", action="store_true",
                      help="Force tree-sitter (no libclang)")

    # c2d-add-foreign (Phase 1: register external C2D + resolve refs)
    p_caf = sub.add_parser("c2d-add-foreign",
                           help="Register a foreign C2D and resolve cross-project refs")
    p_caf.add_argument("--graph", required=True, help="Local (B) C2D directory")
    p_caf.add_argument("--foreign-c2d", required=True,
                       help="Foreign (A) C2D directory to register")
    p_caf.add_argument("--project-name", default="",
                       help="Project name of foreign C2D (e.g., 'A')")
    p_caf.add_argument("--rescan-unresolved", action="store_true",
                       help="Re-attempt resolution of all unresolved refs")

    # c2d-sync-foreign (Phase 1: detect foreign changes + re-resolve)
    p_csf = sub.add_parser("c2d-sync-foreign",
                           help="Sync foreign_refs with updated foreign C2Ds")
    p_csf.add_argument("--graph", required=True, help="Local (B) C2D directory")
    p_csf.add_argument("--foreign-c2d", default="",
                       help="Specific foreign C2D to sync (default: all watched)")

    # c2d-list-foreign (Phase 1: list watched C2Ds)
    p_clf = sub.add_parser("c2d-list-foreign",
                           help="List watched foreign C2Ds with sync status")
    p_clf.add_argument("--graph", required=True, help="Local C2D directory")

    # c2d-remove-foreign (Phase 1: unregister foreign C2D)
    p_crf = sub.add_parser("c2d-remove-foreign",
                           help="Unregister a foreign C2D")
    p_crf.add_argument("--graph", required=True, help="Local C2D directory")
    p_crf.add_argument("--foreign-c2d", required=True,
                       help="Foreign C2D path to remove")

    # composite-query (Phase 2: cross-C2D JOIN via ATTACH)
    p_cq = sub.add_parser("composite-query",
                          help="Query across local + foreign C2Ds via SQLite ATTACH")
    p_cq.add_argument("--graph", required=True, help="Local C2D directory")
    p_cq.add_argument("--query", required=True,
                      help="Query: 'CALLERS_OF name' / 'CALLEES_OF name' / free-text")
    p_cq.add_argument("--foreign-c2d", default="",
                      help="Comma-separated foreign C2D paths to attach")
    p_cq.add_argument("--top", type=int, default=50)

    # c2d-check-compat (Phase 2: verify B's foreign_refs against A_v2)
    p_ccc = sub.add_parser("c2d-check-compat",
                           help="Check if B's foreign_refs still valid against new A version")
    p_ccc.add_argument("--graph", required=True, help="Local (B) C2D directory")
    p_ccc.add_argument("--against-c2d", required=True,
                       help="New version of foreign (A) C2D to check against")

    # coverage-cross-c2d (Phase 2: test coverage across C2Ds)
    p_ccc2 = sub.add_parser("coverage-cross-c2d",
                            help="Compute which functions in target_c2d are called by test_c2d")
    p_ccc2.add_argument("--test-c2d", required=True, help="Test code C2D directory")
    p_ccc2.add_argument("--target-c2d", required=True, help="Target (tested) C2D directory")

    # Phase 3 commands
    p_afs = sub.add_parser("c2d-add-foreign-stub",
                           help="Register a vendor SDK stub C2D (signatures only)")
    p_afs.add_argument("--graph", required=True, help="Local C2D directory")
    p_afs.add_argument("--stub-c2d", required=True, help="Stub C2D directory")
    p_afs.add_argument("--project-name", default="", help="Stub project name (e.g., 'glibc')")

    p_fal = sub.add_parser("ffi-auto-link",
                           help="Auto-link FFI bindings to watched foreign C2Ds")
    p_fal.add_argument("--graph", required=True, help="Local C2D directory")

    p_sre = sub.add_parser("scan-rpc",
                           help="Scan source for RPC client calls (HTTP/gRPC) + create stub edges")
    p_sre.add_argument("--graph", required=True, help="Local C2D directory")

    p_ifk = sub.add_parser("import-foreign-knowledge",
                           help="Copy foreign C2D's knowledge/*.md into local knowledge/")
    p_ifk.add_argument("--graph", required=True, help="Local C2D directory")
    p_ifk.add_argument("--foreign-c2d", required=True, help="Foreign C2D directory")
    p_ifk.add_argument("--project-name", default="", help="Foreign project name")

    # patch-from-diff
    p_pdiff = sub.add_parser("patch-from-diff", help="Patch graph from unified diff text")
    p_pdiff.add_argument("--graph", required=True, help="Call graph output directory")
    p_pdiff.add_argument("--source", default="", help="Source root directory")
    p_pdiff.add_argument("--diff-file", default="", help="Diff file path (or pipe via stdin)")
    p_pdiff.add_argument("--no-transaction", action="store_true",
                         help="Skip the transaction wrapper (no snapshot/WAL/rollback)")

    # patch-from-git
    p_pgit = sub.add_parser("patch-from-git", help="Patch graph from git diff")
    p_pgit.add_argument("--graph", required=True, help="Call graph output directory")
    p_pgit.add_argument("--source", required=True, help="Source root directory (git repo)")
    p_pgit.add_argument("--commit-range", default=None, help="Git commit range (e.g. HEAD~3, abc..def)")
    p_pgit.add_argument("--no-transaction", action="store_true",
                        help="Skip the transaction wrapper (no snapshot/WAL/rollback)")

    # light-scan
    p_lscan = sub.add_parser("light-scan", help="Lightweight scan of changed files (no LLM)")
    p_lscan.add_argument("--source", required=True, help="Source root directory")
    p_lscan.add_argument("--graph", required=True, help="Call graph output directory")
    p_lscan.add_argument("--files", default="", help="Comma-separated files to scan (auto-detect from git if omitted)")

    # explore-flow
    p_explore = sub.add_parser("explore-flow",
                                help="One-shot context retrieval: query → nodes + paths + conditions")
    p_explore.add_argument("--graph", required=True, help="Call graph output directory")
    p_explore.add_argument("--query", required=True,
                            help="Natural language query or symbol names (e.g. 'module initialization', 'api_connect')")
    p_explore.add_argument("--max-nodes", type=int, default=15,
                            help="Max nodes in result subgraph (default: 15)")
    p_explore.add_argument("--max-tokens", type=int, default=2000,
                            help="Max output tokens (default: 2000)")
    p_explore.add_argument("--focus-domain", dest="focus_domain", default=None,
                            help="Restrict search to a specific architecture domain (e.g., 'lib.bdev')")

    # key-paths
    p_kp = sub.add_parser("key-paths",
                            help="Extract key execution paths from entry points automatically")
    p_kp.add_argument("--graph", required=True, help="Call graph output directory")
    p_kp.add_argument("--top", type=int, default=5,
                       help="Number of top paths to return (default: 5)")
    p_kp.add_argument("--from", dest="from_entry", default=None,
                       help="Specific entry point name or ID (default: auto-detect)")
    p_kp.add_argument("--max-tokens", type=int, default=0,
                       help="Max output tokens (0=unlimited)")

    # trace-chain
    p_trace = sub.add_parser("trace-chain",
                             help="One-shot trace from --from to --to with full annotation")
    p_trace.add_argument("--graph", required=True, help="Call graph output directory")
    p_trace.add_argument("--from", dest="from_node", required=True, help="Start node ID or name")
    p_trace.add_argument("--to", dest="to_node", default=None, help="Target node ID or name")
    p_trace.add_argument("--bindings", default="", help='Variable bindings, e.g. "mode=1,flag=true"')
    p_trace.add_argument("--macros", default="",
                         help="Only return paths where these macro conditions are active (comma-separated, e.g., 'SPDK_CONFIG_APP_RW')")
    p_trace.add_argument("--annotate", action="store_true", help="Include signature/condition/concurrency per step")
    p_trace.add_argument("--json", action="store_true", help="Output as JSON")

    # reverse-trace
    p_rtrace = sub.add_parser("reverse-trace",
                              help="Reverse trace from crash point through callers with condition/concurrency annotation")
    p_rtrace.add_argument("--crash-point", required=True, help="Crash point function name or ID")
    p_rtrace.add_argument("--max-depth", type=int, default=10, help="Max BFS depth (default: 10)")
    p_rtrace.add_argument("--max-paths", type=int, default=20, help="Max number of paths to return (default: 20)")
    p_rtrace.add_argument("--graph", required=True, help="Call graph output directory")
    p_rtrace.add_argument("--macros", default="",
                          help="Only return paths where these macro conditions are active (comma-separated, e.g., 'CONFIG_X')")
    p_rtrace.add_argument("--json", action="store_true", help="Output as JSON")

    # diff-chains
    p_diff = sub.add_parser("diff-chains",
                            help="Compare execution paths under two different bindings")
    p_diff.add_argument("--graph", required=True, help="Call graph output directory")
    p_diff.add_argument("--node", required=True, help="Start node ID")
    p_diff.add_argument("--bindings-a", required=True, help='First binding set, e.g. "mode=0"')
    p_diff.add_argument("--bindings-b", required=True, help='Second binding set, e.g. "mode=1"')
    p_diff.add_argument("--json", action="store_true", help="Output as JSON")

    # concurrency-risks
    p_crisk = sub.add_parser("concurrency-risks",
                             help="List all concurrency risk points sorted by risk level")
    p_crisk.add_argument("--graph", required=True, help="Call graph output directory")
    p_crisk.add_argument("--json", action="store_true", help="Output as JSON")

    # data-lifecycle
    p_dl = sub.add_parser("data-lifecycle",
                          help="Trace resource allocation→usage→release paths")
    p_dl.add_argument("--graph", required=True, help="Call graph output directory")
    p_dl.add_argument("--resource", required=True, help="Resource keyword (e.g. 'buffer', 'conn')")
    p_dl.add_argument("--json", action="store_true", help="Output as JSON")

    # detect-races
    p_dr = sub.add_parser("detect-races",
                          help="Detect data races between different thread contexts")
    p_dr.add_argument("--graph", required=True, help="Call graph output directory")
    p_dr.add_argument("--func", default=None,
                       help="Specific function to check (default: scan all)")
    p_dr.add_argument("--json", action="store_true", help="Output as JSON")
    p_dr.add_argument("--min-severity", default="low",
                       choices=["low", "medium", "high"],
                       help="Minimum severity to report (default: low)")
    p_dr.add_argument("--profile", default=None,
                       help="Profile JSON path (overrides .code2database_profile.json in graph dir; provides lock APIs for race protection detection)")

    # concurrency-analyze
    p_ca = sub.add_parser("concurrency-analyze",
                          help="Analyze concurrency safety between two call chains or a function and its concurrent peers")
    p_ca.add_argument("--graph", required=True, help="Call graph output directory")
    p_ca.add_argument("--func", default=None,
                       help="Function name to analyze (finds concurrent peers automatically)")
    p_ca.add_argument("--chain1", default=None,
                       help="First function name (chain1)")
    p_ca.add_argument("--chain2", default=None,
                       help="Second function name (chain2, optional)")
    p_ca.add_argument("--json", action="store_true", help="Output as JSON")
    p_ca.add_argument("--profile", default=None,
                       help="Profile JSON path (overrides .code2database_profile.json in graph dir; provides lock APIs for protection detection)")

    # happens-before — memory-ordering-based happens-before analysis
    p_hb = sub.add_parser("happens-before",
                          help="Check happens-before between a writer and reader via locks, RCU, or memory barriers")
    p_hb.add_argument("--graph", required=True, help="Call graph output directory")
    p_hb.add_argument("--write", required=True, help="Writer function name or id")
    p_hb.add_argument("--read", required=True, help="Reader function name or id")
    p_hb.add_argument("--var", required=True,
                      help="Shared variable name (field or global)")
    p_hb.add_argument("--max-depth", type=int, default=5,
                      help="Max path depth when checking call-chain relationship")

    # memory-ordering — show memory primitives used by a function
    p_mo = sub.add_parser("memory-ordering",
                          help="Show RCU/memory-barrier/atomic primitives used by a function")
    p_mo.add_argument("--graph", required=True, help="Call graph output directory")
    p_mo.add_argument("--node", required=True, help="Function name or id")

    # explain-label — explain why a node has a given label
    p_el = sub.add_parser("explain-label",
                          help="Explain why a node has a given label (dead_code, API_entry, race_risk, etc.)")
    p_el.add_argument("--graph", required=True, help="Call graph output directory")
    p_el.add_argument("--node", required=True, help="Function name or id")
    p_el.add_argument("--label", required=True,
                      help="Label to explain (e.g., dead_code, API_entry, race_risk, AMBIGUOUS)")

    # why-ambiguous — explain why an edge is marked AMBIGUOUS
    p_wa = sub.add_parser("why-ambiguous",
                          help="Explain why an edge is marked AMBIGUOUS (fn_ptr dispatch, dead #ifdef, etc.)")
    p_wa.add_argument("--graph", required=True, help="Call graph output directory")
    p_wa.add_argument("--from", dest="from_node", required=True,
                      help="Caller function name or id")
    p_wa.add_argument("--to", dest="to_node", required=True,
                      help="Callee function name or id")

    # audit-log — query the audit log for traceability of graph edits
    p_al = sub.add_parser("audit-log",
                          help="Query the audit log (who edited what, when, why)")
    p_al.add_argument("--graph", required=True, help="Call graph output directory")
    p_al.add_argument("--node", default=None, help="Filter by target node id")
    p_al.add_argument("--command", default=None,
                      help="Filter by command name (update-node, auto-enhance, etc.)")
    p_al.add_argument("--target-kind", default=None,
                      help="Filter by target kind (node/edge/profile/graph)")
    p_al.add_argument("--action", default=None,
                      help="Filter by action (update/insert/delete/apply/invalidate)")
    p_al.add_argument("--tx", default=None, help="Filter by transaction id")
    p_al.add_argument("--since", default=None,
                      help="Filter entries since timestamp (ISO-8601)")
    p_al.add_argument("--until", default=None,
                      help="Filter entries until timestamp (ISO-8601)")
    p_al.add_argument("--limit", type=int, default=100,
                      help="Max entries to return (default 100)")
    p_al.add_argument("--offset", type=int, default=0,
                      help="Pagination offset (default 0)")

    # who-allocates — find functions that allocate a resource
    p_wa = sub.add_parser("who-allocates",
                          help="Find functions that allocate a resource (ALLOCATES edges)")
    p_wa.add_argument("--graph", required=True, help="Call graph output directory")
    p_wa.add_argument("--resource", default=None,
                      help="Filter by resource name (e.g., kmalloc)")

    # who-frees — find functions that free a resource
    p_wf = sub.add_parser("who-frees",
                          help="Find functions that free a resource (FREES edges)")
    p_wf.add_argument("--graph", required=True, help="Call graph output directory")
    p_wf.add_argument("--resource", default=None,
                      help="Filter by resource name (e.g., kfree)")

    # unbalanced-alloc-free — find functions with alloc/free imbalance
    p_uaf = sub.add_parser("unbalanced-alloc-free",
                           help="Find functions that alloc without free (or vice versa)")
    p_uaf.add_argument("--graph", required=True, help="Call graph output directory")

    # who-locks — find functions that acquire a lock
    p_wl = sub.add_parser("who-locks",
                          help="Find functions that acquire a lock (LOCKS edges)")
    p_wl.add_argument("--graph", required=True, help="Call graph output directory")
    p_wl.add_argument("--lock", default=None, help="Filter by lock variable name")

    # add-semantic-edges — scan graph and add ALLOCATES/FREES/LOCKS/UNLOCKS edges
    p_ase = sub.add_parser("add-semantic-edges",
                           help="Walk graph and add ALLOCATES/FREES/LOCKS/UNLOCKS edges from body text")
    p_ase.add_argument("--graph", required=True, help="Call graph output directory")

    # extract-invariants-llm — LLM-assisted invariant extraction with consensus
    p_eil = sub.add_parser("extract-invariants-llm",
                           help="Extract invariants with LLM consensus and continuous confidence")
    p_eil.add_argument("--graph", required=True, help="Call graph output directory")
    p_eil.add_argument("--node", required=True, help="Function name or id")
    p_eil.add_argument("--num-calls", type=int, default=3,
                       help="Number of LLM calls for consensus (default 3)")

    # graph-history — list graph versions or show history of a node (D9)
    p_gh = sub.add_parser("graph-history",
                          help="List graph versions or show history of a specific node")
    p_gh.add_argument("--graph", required=True, help="Call graph output directory")
    p_gh.add_argument("--node", default=None, help="Node id to show history for")
    p_gh.add_argument("--limit", type=int, default=50, help="Max versions to show")

    # graph-diff — diff two graph versions (D9)
    p_gd = sub.add_parser("graph-diff", help="Diff two graph versions")
    p_gd.add_argument("--graph", required=True, help="Call graph output directory")
    p_gd.add_argument("--from-version", type=int, default=None,
                      help="Source version id (default: current state)")
    p_gd.add_argument("--to-version", type=int, default=None,
                      help="Target version id (default: current state)")
    p_gd.add_argument("--from-path", default=None,
                      help="Explicit source path (overrides --from-version)")
    p_gd.add_argument("--to-path", default=None,
                      help="Explicit target path (overrides --to-version)")
    p_gd.add_argument("--summary-only", action="store_true",
                      help="Only print summary counts")

    # graph-record-version — manually record a graph version (D9)
    p_grv = sub.add_parser("graph-record-version",
                           help="Manually record a graph version")
    p_grv.add_argument("--graph", required=True, help="Call graph output directory")
    p_grv.add_argument("--description", default="", help="Description of this version")
    p_grv.add_argument("--commit-hash", default=None, help="Commit hash")
    p_grv.add_argument("--commit-short", default=None, help="Short commit hash")
    p_grv.add_argument("--operator", default=None, help="Operator name")

    # export-obsidian
    p_obs = sub.add_parser("export-obsidian",
                           help="Export invocation graph as Obsidian vault with [[links]] = calls")
    p_obs.add_argument("--graph", required=True, help="Call graph output directory")
    p_obs.add_argument("--output", help="Output directory (default: graph-dir/obsidian-vault)")

    # validate-plugin
    p_vplug = sub.add_parser("validate-plugin",
                             help="Validate a plugin file for interface compliance")
    p_vplug.add_argument("--plugin", required=True, help="Path to plugin .py file")

    # quick-update (Improvement 5: one-click patch + light-scan, no LLM)
    p_qu = sub.add_parser("quick-update", help="One-click: patch + light-scan, no LLM needed")
    p_qu.add_argument("--source", required=True, help="Source root directory")
    p_qu.add_argument("--graph", required=True, help="Call graph output directory")
    p_qu.add_argument("--commit-range", default=None, help="Git commit range (e.g. HEAD~3)")
    p_qu.add_argument("--auto-threshold", default=None,
                      help="Auto-trigger semantic update when stale ratio >= threshold (e.g. 0.15)")

    # install-hook (Improvement 5: install post-commit hook for auto quick-update)
    p_ih = sub.add_parser("install-hook", help="Install git post-commit hook for auto quick-update")
    p_ih.add_argument("--source", required=True, help="Git repository root")
    p_ih.add_argument("--graph-dir", default=".callgraph",
                      help="Call graph directory relative to source root (default: .callgraph)")

    # export-changes (Improvement 5: changelog-based change graph)
    p_ec = sub.add_parser("export-changes", help="Export change graph from git/svn changelog")
    p_ec.add_argument("--source", required=True, help="Source root directory")
    p_ec.add_argument("--graph", required=True, help="Call graph output directory")
    p_ec.add_argument("--commit-range", default=None, help="Git commit range (e.g. HEAD~3, abc..def)")
    p_ec.add_argument("--output", default="", help="Output JSON path (default: graph-dir/.code2database_changes.json)")

    # merge-changes (Improvement 5: apply change graph to existing graph)
    p_mc = sub.add_parser("merge-changes", help="Merge change graph JSON into existing graph")
    p_mc.add_argument("--graph", required=True, help="Call graph output directory")
    p_mc.add_argument("--changes", required=True, help="Change graph JSON path")
    p_mc.add_argument("--source", default="", help="Source root directory")

    # semantic-status (Improvement 5: check if semantic update recommended)
    p_ss = sub.add_parser("semantic-status", help="Check if semantic update is recommended")
    p_ss.add_argument("--graph", required=True, help="Call graph output directory")

    # serve (MCP server mode)
    p_serve = sub.add_parser("serve", help="Start MCP server for LLM agent queries (stdio transport)")
    p_serve.add_argument("--graph", required=True, help="Call graph output directory")

    # get-code-snippet
    p_snippet = sub.add_parser("get-code-snippet",
                               help="Extract source code snippet around a node")
    p_snippet.add_argument("--graph", required=True, help="Call graph output directory")
    p_snippet.add_argument("--node", required=True, help="Node ID or name")
    p_snippet.add_argument("--source", default="", help="Source root directory")
    p_snippet.add_argument("--context", type=int, default=10,
                            help="Lines of context (default 10)")
    p_snippet.add_argument("--persist", action="store_true",
                            help="Write snippet back to node's body_text field "
                                 "(non-destructive, stored as body_text_supplemented). "
                                 "Requires user confirmation by default.")
    p_snippet.add_argument("--yes", "-y", action="store_true",
                            help="Bypass confirmation prompt for --persist "
                                 "(for automation; LLM should NOT use this without explicit user permission)")

    # blast-radius
    p_blast = sub.add_parser("blast-radius",
                              help="Show blast radius: affected tests/APIs for a function change")
    p_blast.add_argument("--graph", required=True, help="Call graph output directory")
    p_blast.add_argument("--node", required=True, help="Node ID or name of changed function")
    p_blast.add_argument("--depth", type=int, default=3, help="Traversal depth (default 3)")
    p_blast.add_argument("--json", action="store_true", help="Output as JSON")

    # field-access
    p_fa = sub.add_parser("field-access",
                          help="Find which functions read/write a struct field or global variable")
    p_fa.add_argument("--graph", required=True, help="Call graph output directory")
    p_fa.add_argument("--struct", default="", help="Struct name (e.g., file_operations)")
    p_fa.add_argument("--field", required=True, help="Field or global variable name (e.g., init, g_counter)")
    p_fa.add_argument("--value", default="",
                      help="Filter writes by assigned RHS value (e.g., 'NULL' for null-pointer-deref analysis)")
    p_fa.add_argument("--json", action="store_true", help="Output as JSON")

    # field-flow — combine field-access writers with reverse-trace call chains
    # for null-pointer-deref / use-after-free / race-condition root-cause analysis.
    # Answers "who set field X to value Y, and how is that writer reached from entry points?"
    p_ff = sub.add_parser("field-flow",
                          help="Trace field writes + their call chains (combines field-access + reverse-trace)")
    p_ff.add_argument("--graph", required=True, help="Call graph output directory")
    p_ff.add_argument("--struct", default="", help="Struct name (e.g., buffer_head, block_device)")
    p_ff.add_argument("--field", required=True,
                      help="Field name (e.g., b_bdev, bd_inode) — the field written to NULL/special value")
    p_ff.add_argument("--value", default="",
                      help="Filter writes by assigned RHS value (e.g., 'NULL' for NULL-deref analysis)")
    p_ff.add_argument("--max-depth", type=int, default=8,
                      help="Max reverse-trace depth from each writer (default: 8)")
    p_ff.add_argument("--max-paths-per-writer", type=int, default=5,
                      help="Max call chains per writer to return (default: 5)")
    p_ff.add_argument("--json", action="store_true", help="Output as JSON")

    # watch
    p_watch = sub.add_parser("watch", help="Auto-sync: watch source directory and update incrementally")
    p_watch.add_argument("--source", required=True, help="Source directory to watch")
    p_watch.add_argument("--output", help="Build output directory (default: <source>/code2db-out)")
    p_watch.add_argument("--debounce", type=float, default=2.0,
                          help="Debounce interval in seconds (default: 2.0)")

    # Deficiency 2: commit-aware provenance commands
    p_dc = sub.add_parser("describe-commit",
                          help="Show which nodes/edges a commit affected")
    p_dc.add_argument("--graph", required=True, help="Call graph output directory")
    p_dc.add_argument("--commit", required=True, help="Commit hash (full or short)")
    p_dc.add_argument("--json", action="store_true", help="Output as JSON")

    p_nh = sub.add_parser("node-history",
                          help="Show commit history for a node")
    p_nh.add_argument("--graph", required=True, help="Call graph output directory")
    p_nh.add_argument("--node", required=True, help="Node ID")
    p_nh.add_argument("--json", action="store_true", help="Output as JSON")

    p_gp = sub.add_parser("graph-provenance",
                          help="Show which commit the current graph corresponds to")
    p_gp.add_argument("--graph", required=True, help="Call graph output directory")
    p_gp.add_argument("--json", action="store_true", help="Output as JSON")

    p_bn = sub.add_parser("blame-node",
                          help="Attribute a node to its introducing/last-modifying commit")
    p_bn.add_argument("--graph", required=True, help="Call graph output directory")
    p_bn.add_argument("--node", required=True, help="Node ID")
    p_bn.add_argument("--json", action="store_true", help="Output as JSON")

    p_fc = sub.add_parser("find-commits",
                          help="Find commits that recently modified a function")
    p_fc.add_argument("--graph", required=True, help="Call graph output directory")
    p_fc.add_argument("--function", required=True, help="Function name or node ID")
    p_fc.add_argument("--since", default="", help="Only commits since (date/rel)")
    p_fc.add_argument("--limit", type=int, default=20, help="Max commits to return")
    p_fc.add_argument("--json", action="store_true", help="Output as JSON")

    # Deficiency 3: unified Cypher-subset query language
    p_q = sub.add_parser("query",
                         help="Run a Cypher-subset query against the graph (unified query language)")
    p_q.add_argument("--graph", required=True, help="Call graph output directory")
    p_q.add_argument("--query", required=True,
                     help="Query string, e.g. \"MATCH (n:Function) WHERE n.name='foo' RETURN n.id, n.name\"")
    p_q.add_argument("--format", choices=["json", "md"], default="json",
                     help="Output format (json or markdown table)")
    p_q.add_argument("--with-hints", action="store_true",
                     help="Wrap output as {\"rows\": [...], \"_hints\": [...]} where _hints "
                          "contains top-3 kb_paragraphs hits matching the query string "
                          "(Phase 3 priority chain). Default off for backward compat — "
                          "stdout stays a flat rows list.")

    # Deficiency 4 (P0): value flow / taint tracking
    p_vf = sub.add_parser("value-flow",
                          help="Build and query value-flow edges (where does this value come from / go to?)")
    p_vf.add_argument("--graph", required=True, help="Call graph output directory")
    p_vf.add_argument("--build", action="store_true",
                      help="Build DATA_FLOW/RETURN_FLOW edges and save to .code2database_data_flow.json")
    p_vf.add_argument("--reverse", action="store_true",
                      help="Reverse trace: where does a value originate?")
    p_vf.add_argument("--taint", action="store_true",
                      help="Forward taint trace: where can a value flow to?")
    p_vf.add_argument("--interprocedural", action="store_true",
                      help="Multi-hop interprocedural trace with alias propagation (D19)")
    p_vf.add_argument("--node", default="", help="Node ID or function name (for --reverse/--taint)")
    p_vf.add_argument("--pattern", default="",
                      help="Value pattern to track (e.g., 'NULL', 'user_input')")
    p_vf.add_argument("--max-depth", type=int, default=10, help="Max trace depth")
    p_vf.add_argument("--json", action="store_true", help="Output as JSON")

    # Deficiency 5 (P0): precise lock-coverage analysis
    p_lc = sub.add_parser("lock-coverage",
                          help="Analyze lock-held regions and per-access locksets (replaces over-approximation)")
    p_lc.add_argument("--graph", required=True, help="Call graph output directory")
    p_lc.add_argument("--node", default="",
                      help="Node ID or function name (single-function analysis)")
    p_lc.add_argument("--detect-races", action="store_true",
                      help="Whole-graph race detection using precise locksets")

    # Deficiency 6: path feasibility auto-solving (Z3 if available, heuristic fallback)
    p_pf = sub.add_parser("path-feasible",
                          help="Auto-solve path feasibility (no manual bindings needed)")
    p_pf.add_argument("--graph", required=True, help="Call graph output directory")
    p_pf.add_argument("--conditions", default="",
                      help="Comma-separated condition list (e.g., 'mode==1,flag,x>0')")
    p_pf.add_argument("--node", default="",
                      help="Walk paths from this node and check feasibility")
    p_pf.add_argument("--max-depth", type=int, default=5, help="Max path depth")
    p_pf.add_argument("--with-configs", default="",
                      help="Macro bindings for config-predicate feasibility "
                           "(e.g., 'CONFIG_X=true,CONFIG_Y=false')")

    # IMPROVE-OPT4: path-guards — prove writer reachability from entry point
    p_pg = sub.add_parser("path-guards",
                          help="Prove writer reachability from entry using guard conditions")
    p_pg.add_argument("--graph", required=True, help="Call graph output directory")
    p_pg.add_argument("--from", dest="from_node", required=True,
                      help="Entry point function name or node ID")
    p_pg.add_argument("--to", dest="to_node", required=True,
                      help="Writer function name or node ID")
    p_pg.add_argument("--field", default="",
                      help="Target field name (e.g., 'b_bdev') — used to look up writer's guard_condition")
    p_pg.add_argument("--value", default="",
                      help="Filter writer by assigned value (e.g., 'NULL')")
    p_pg.add_argument("--max-depth", type=int, default=8, help="Max path depth for forward BFS")
    p_pg.add_argument("--with-configs", default="",
                      help="Macro bindings for config-predicate feasibility "
                           "(e.g., 'CONFIG_X=true,CONFIG_Y=false')")

    # Deficiency 7: cross-function data dependency
    p_dd = sub.add_parser("data-dep",
                          help="Cross-function data dependencies (globals/fields as nodes, mod-read chains, dead writers)")
    p_dd.add_argument("--graph", required=True, help="Call graph output directory")
    p_dd.add_argument("--build", action="store_true",
                      help="Build DATA_DEP index and save to .code2database_data_dep.json")
    p_dd.add_argument("--forward", action="store_true",
                      help="Forward data-dep: who reads what this function writes?")
    p_dd.add_argument("--reverse", action="store_true",
                      help="Reverse data-dep: who writes what this function reads?")
    p_dd.add_argument("--blast", action="store_true",
                      help="Combined blast radius (call chain + data dep)")
    p_dd.add_argument("--dead-writers", action="store_true",
                      help="Find functions that write globals/fields that no one reads (dead code)")
    p_dd.add_argument("--node", default="", help="Node ID or function name")
    p_dd.add_argument("--max-depth", type=int, default=5, help="Max trace depth")

    # Deficiency 8: invariant extraction
    p_ei = sub.add_parser("extract-invariants",
                          help="Extract preconditions/postconditions/loop_invariants + state machines from function bodies")
    p_ei.add_argument("--graph", required=True, help="Call graph output directory")
    p_ei.add_argument("--apply", action="store_true",
                      help="Attach extracted invariants to graph nodes (writes back)")
    p_ei.add_argument("--node", default="",
                      help="Extract invariants for a single node (by ID or name)")

    p_fi = sub.add_parser("find-invariants",
                          help="Find functions guaranteeing a given invariant (e.g., 'ctx->state == READY' after return)")
    p_fi.add_argument("--graph", required=True, help="Call graph output directory")
    p_fi.add_argument("--var", default="",
                      help="Variable pattern to match (e.g., 'ctx->state', 'config')")
    p_fi.add_argument("--value", default="",
                      help="Value pattern to match (e.g., 'READY', '!= NULL')")
    p_fi.add_argument("--kind", default="",
                      choices=["", "precondition", "postcondition", "loop_invariant"],
                      help="Restrict to one invariant kind")
    p_fi.add_argument("--state-machine", action="store_true",
                      help="Return the merged state machine for --var instead of invariant matches")

    p_ai = sub.add_parser("apply-invariants",
                          help="Apply LLM-enhanced invariants from .code2database_invariants.json back to the graph")
    p_ai.add_argument("--graph", required=True, help="Call graph output directory")
    p_ai.add_argument("--input", default="",
                      help="Path to invariants JSON (default: <graph>/.code2database_invariants.json)")

    # Deficiency 9: auto semantic enhancement
    p_ae = sub.add_parser("auto-enhance",
                          help="Auto-enhance a node with LLM-supplied attributes (auto-writes EXTRACTED, prompts INFERRED)")
    p_ae.add_argument("--graph", required=True, help="Call graph output directory")
    p_ae.add_argument("--node", default="", help="Node ID or function name")
    p_ae.add_argument("--attr", action="append", default=[],
                      help="key=value (JSON auto-parsed). Repeatable.")
    p_ae.add_argument("--confidence", default="INFERRED",
                      choices=["EXTRACTED", "INFERRED", "AMBIGUOUS"])
    p_ae.add_argument("--evidence", default="",
                      help="Evidence string for the supplement (enables auto-write for EXTRACTED)")
    p_ae.add_argument("--threshold", default="EXTRACTED",
                      choices=["EXTRACTED", "INFERRED"],
                      help="Auto-write threshold (EXTRACTED = only EXTRACTED auto-writes)")
    p_ae.add_argument("--allow-ambiguous", action="store_true",
                      help="Allow AMBIGUOUS supplements (rejected by default)")
    p_ae.add_argument("--batch", action="store_true",
                      help="Process the pending batch session (auto-write EXTRACTED items)")

    p_bc = sub.add_parser("batch-confirm",
                          help="Batch-confirm pending supplements (accept-all / reject-all / per-item / apply)")
    p_bc.add_argument("--graph", required=True, help="Call graph output directory")
    p_bc.add_argument("--list", action="store_true", help="List pending items")
    p_bc.add_argument("--accept-all", action="store_true")
    p_bc.add_argument("--reject-all", action="store_true")
    p_bc.add_argument("--accept", default="", help="Comma-separated item IDs to accept")
    p_bc.add_argument("--reject", default="", help="Comma-separated item IDs to reject")
    p_bc.add_argument("--apply", action="store_true",
                      help="Apply all accepted items to the graph")

    p_rb = sub.add_parser("rollback",
                          help="Rollback supplement writes (revert to previous value)")
    p_rb.add_argument("--graph", required=True, help="Call graph output directory")
    p_rb.add_argument("--list", action="store_true", help="List recent writes")
    p_rb.add_argument("--to", type=int, default=0,
                      help="Revert to before entry ID (use --list to find IDs)")
    p_rb.add_argument("--last", action="store_true", help="Revert the most recent write")
    p_rb.add_argument("--limit", type=int, default=50, help="Max entries to list")

    p_fr = sub.add_parser("fill-request",
                          help="List empty fields on a node that the LLM should fill (auto-fill request)")
    p_fr.add_argument("--graph", required=True, help="Call graph output directory")
    p_fr.add_argument("--node", default="", help="Node ID or function name")
    p_fr.add_argument("--all", action="store_true",
                      help="Find all nodes with empty fillable fields")
    p_fr.add_argument("--limit", type=int, default=100, help="Max nodes with --all")

    p_he = sub.add_parser("heuristic-enhance",
                          help="Generate heuristic supplements for empty fields — no LLM required (always-works fallback)")
    p_he.add_argument("--graph", required=True, help="Call graph output directory")
    p_he.add_argument("--node", default="", help="Node ID or function name")
    p_he.add_argument("--all", action="store_true",
                      help="Generate for all nodes with empty fillable fields")
    p_he.add_argument("--limit", type=int, default=100, help="Max nodes with --all")
    p_he.add_argument("--fields", default="",
                      help="Comma-separated subset of fields to fill (default: all)")
    p_he.add_argument("--dry-run", action="store_true",
                      help="Preview generated supplements without writing")

    # Deficiency 10: transactional updates (WAL + snapshots + rollback)
    p_txb = sub.add_parser("tx-begin", help="Begin a graph transaction (snapshot + WAL + write lock)")
    p_txb.add_argument("--graph", required=True, help="Call graph output directory")
    p_txb.add_argument("--description", default="", help="Description of the transaction")

    p_txc = sub.add_parser("tx-commit", help="Commit the current transaction (clears WAL)")
    p_txc.add_argument("--graph", required=True, help="Call graph output directory")

    p_txr = sub.add_parser("tx-rollback", help="Rollback the current transaction (restores snapshot)")
    p_txr.add_argument("--graph", required=True, help="Call graph output directory")

    p_txs = sub.add_parser("tx-status", help="Show current transaction state and WAL status")
    p_txs.add_argument("--graph", required=True, help="Call graph output directory")

    p_txsn = sub.add_parser("tx-snapshot", help="Take a manual snapshot (without starting a transaction)")
    p_txsn.add_argument("--graph", required=True, help="Call graph output directory")
    p_txsn.add_argument("--description", default="manual snapshot")

    p_txrs = sub.add_parser("tx-restore", help="Restore graph state from a specific snapshot")
    p_txrs.add_argument("--graph", required=True, help="Call graph output directory")
    p_txrs.add_argument("--id", required=True, help="Snapshot ID (from tx-list-snapshots)")

    p_txls = sub.add_parser("tx-list-snapshots", help="List all available snapshots")
    p_txls.add_argument("--graph", required=True, help="Call graph output directory")
    p_txls.add_argument("--limit", type=int, default=50, help="Max snapshots to list")

    p_txrw = sub.add_parser("tx-replay-wal", help="Replay or rollback an unfinished WAL (crash recovery)")
    p_txrw.add_argument("--graph", required=True, help="Call graph output directory")

    # Deficiency 11: cross-language FFI
    p_ffid = sub.add_parser("ffi-detect",
                            help="Detect FFI boundaries (Python ctypes, Go cgo, Rust extern \"C\") and add FFI edges")
    p_ffid.add_argument("--graph", required=True, help="Call graph output directory")
    p_ffid.add_argument("--source", required=True, help="Source root directory to scan")
    p_ffid.add_argument("--apply", action="store_true",
                        help="Attach detected FFI edges to the graph (otherwise just write JSON)")

    p_ffil = sub.add_parser("ffi-list", help="List all FFI edges in the graph")
    p_ffil.add_argument("--graph", required=True, help="Call graph output directory")
    p_ffil.add_argument("--mechanism", default="",
                        choices=["", "ctypes", "cgo", "extern_c", "cffi", "pybind11"],
                        help="Filter by FFI mechanism")

    p_ffit = sub.add_parser("ffi-trace", help="Trace the FFI call chain from a node")
    p_ffit.add_argument("--graph", required=True, help="Call graph output directory")
    p_ffit.add_argument("--node", required=True, help="Node ID or name")
    p_ffit.add_argument("--max-depth", type=int, default=10, help="Max trace depth")

    p_ffiTy = sub.add_parser("ffi-types", help="Find FFI type mappings matching patterns")
    p_ffiTy.add_argument("--graph", required=True, help="Call graph output directory")
    p_ffiTy.add_argument("--from", dest="from_type", default="", help="Source type pattern (e.g., 'int')")
    p_ffiTy.add_argument("--to", dest="to_type", default="", help="Target type pattern (e.g., 'long')")

    # D45+D16 optimization: intent-query — route a natural-language question
    # to the most appropriate CLI command automatically.
    p_iq = sub.add_parser("intent-query",
                           help="Classify a natural-language question and route to a CLI command")
    p_iq.add_argument("--question", required=True, help="Natural-language question to route")
    p_iq.add_argument("--graph", default="", help="Call graph output directory (optional)")

    # D24 enhancement: TF-IDF char n-gram embeddings for semantic search
    p_eb = sub.add_parser("embeddings-build",
                           help="Build TF-IDF char n-gram embeddings for semantic search")
    p_eb.add_argument("--graph", required=True, help="Call graph output directory")
    p_es = sub.add_parser("embeddings-search",
                           help="Cosine-similarity search over node embeddings")
    p_es.add_argument("--graph", required=True, help="Call graph output directory")
    p_es.add_argument("--query", required=True, help="Search query text")
    p_es.add_argument("--top-k", type=int, default=10, help="Max results to return")

    # Deficiency 12: interactive Web UI
    p_wui = sub.add_parser("web-ui",
                           help="Start interactive Web UI server for graph browsing, path highlighting, LOD rendering")
    p_wui.add_argument("--graph", required=True, help="Call graph output directory")
    p_wui.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    p_wui.add_argument("--open", action="store_true",
                       help="Open the browser automatically when the server starts")

    # Deficiency 13: BUG benchmark
    p_bb = sub.add_parser("bug-benchmark",
                          help="Run BUG benchmark (graph vs grep) and report recall/precision/tool-call/token efficiency")
    p_bb.add_argument("--graph", default="", help="Call graph output directory")
    p_bb.add_argument("--benchmark", default="", help="Benchmark JSON file path")
    p_bb.add_argument("--source", default="", help="Source root directory (for grep mode)")
    p_bb.add_argument("--create-template", default="",
                      help="Write a benchmark template to the given path and exit")

    # Deficiency 14: profile health + auto-evolution
    p_ph = sub.add_parser("profile-health",
                          help="Compute 0-100 health score for a project profile (callback patterns, skip_names, vtable_types, etc.)")
    p_ph.add_argument("--graph", required=True, help="Call graph output directory")
    p_ph.add_argument("--source", required=True, help="Source root directory")
    p_ph.add_argument("--profile", default="",
                      help="Profile JSON path (default: <graph>/.code2database_profile.json or <source>/.code2database_profile.json)")

    p_pe = sub.add_parser("profile-evolve",
                          help="Detect new callback patterns in source and suggest profile additions; optionally apply EXTRACTED-confidence suggestions")
    p_pe.add_argument("--graph", required=True, help="Call graph output directory")
    p_pe.add_argument("--source", required=True, help="Source root directory")
    p_pe.add_argument("--profile", default="", help="Profile JSON path")
    p_pe.add_argument("--apply", action="store_true",
                      help="Apply EXTRACTED-confidence suggestions (INFERRED require manual review)")

    p_pb = sub.add_parser("profile-bind-version",
                          help="Bind profile to current git/svn HEAD commit so stale profiles can be detected")
    p_pb.add_argument("--graph", required=True, help="Call graph output directory")
    p_pb.add_argument("--source", required=True, help="Source root directory")
    p_pb.add_argument("--profile", default="", help="Profile JSON path")

    # Deficiency 15: doc-code dual source truth alignment
    p_dcc = sub.add_parser("doc-code-check",
                           help="Check doc-code alignment: detect mismatches between semantic_desc (from docs) and body_text (from code)")
    p_dcc.add_argument("--graph", required=True, help="Call graph output directory")
    p_dcc.add_argument("--node", action="append", default=None,
                       help="Filter to specific node IDs (can repeat); default: all nodes")
    p_dcc.add_argument("--json", action="store_true", help="Output full JSON result")

    p_dms = sub.add_parser("doc-mark-stale",
                           help="Mark a node's doc as stale (e.g., after code change detected by daemon)")
    p_dms.add_argument("--graph", required=True, help="Call graph output directory")
    p_dms.add_argument("--node", required=True, help="Node ID to mark stale")
    p_dms.add_argument("--reason", required=True, help="Reason for staleness (e.g., 'signature changed in commit abc123')")

    p_dar = sub.add_parser("doc-alignment-report",
                           help="Generate full Markdown report of doc-code alignment issues")
    p_dar.add_argument("--graph", required=True, help="Call graph output directory")
    p_dar.add_argument("--source", default="", help="Source root directory")
    p_dar.add_argument("-o", "--output", default="",
                       help="Write report to file (default: stdout)")

    p_dsd = sub.add_parser("doc-signature-diff",
                           help="Detect signature changes between two graph versions (old vs new)")
    p_dsd.add_argument("--old-graph", required=True, help="Old invocation graph directory")
    p_dsd.add_argument("--new-graph", required=True, help="New invocation graph directory")

    # Deficiency 16 (P0): background daemon for real-time file monitoring
    p_ds = sub.add_parser("daemon-start",
                          help="Start long-running daemon (foreground; blocks). Monitors source files and auto-updates graph.")
    p_ds.add_argument("--graph", required=True, help="Call graph output directory")
    p_ds.add_argument("--source", required=True, help="Source root directory to monitor")
    p_ds.add_argument("--profile", default="", help="Profile JSON path (for daemon config)")

    p_dp = sub.add_parser("daemon-stop",
                          help="Stop a running daemon (sends SIGTERM)")
    p_dp.add_argument("--graph", required=True, help="Call graph output directory")

    p_dst = sub.add_parser("daemon-status",
                           help="Get daemon status: pid, last_sync, pending events, stale nodes")
    p_dst.add_argument("--graph", required=True, help="Call graph output directory")

    p_dfr = sub.add_parser("daemon-force-refresh",
                           help="Force daemon to re-scan a specific file immediately")
    p_dfr.add_argument("--graph", required=True, help="Call graph output directory")
    p_dfr.add_argument("--path", required=True, help="File path to refresh")

    p_dpa = sub.add_parser("daemon-pause",
                           help="Pause daemon (e.g., before manual updates to avoid conflicts)")
    p_dpa.add_argument("--graph", required=True, help="Call graph output directory")
    p_dpa.add_argument("--reason", default="manual", help="Reason for pausing")

    p_dr = sub.add_parser("daemon-resume",
                          help="Resume daemon after pause")
    p_dr.add_argument("--graph", required=True, help="Call graph output directory")

    p_dws = sub.add_parser("daemon-wait-sync",
                           help="Block until daemon finishes current sync (LLM agents call before important queries)")
    p_dws.add_argument("--graph", required=True, help="Call graph output directory")
    p_dws.add_argument("--timeout", type=float, default=30.0, help="Max seconds to wait (default 30)")

    p_dl = sub.add_parser("daemon-logs",
                          help="Show daemon log file (last N lines, or --follow for streaming)")
    p_dl.add_argument("--graph", required=True, help="Call graph output directory")
    p_dl.add_argument("--follow", action="store_true", help="Stream new log lines (tail -f)")
    p_dl.add_argument("-n", type=int, default=50, help="Number of lines to show (default 50)")

    p_dre = sub.add_parser("daemon-reload",
                           help="Reload daemon config (sends SIGHUP; daemon re-reads profile)")
    p_dre.add_argument("--graph", required=True, help="Call graph output directory")

    p_dlp = sub.add_parser("daemon-list-projects",
                           help="List all projects with daemon state/log files on this machine")
    p_dlp.add_argument("--graph", default=".", help="Current graph dir (for state lookup)")

    # ---- cgdb commands — direct cgdb table queries via SQLite ----
    p_cgq = sub.add_parser("cgdb-query",
                            help="Generic cgdb query: FTS5 symbol search or get_node by id")
    p_cgq.add_argument("--graph", required=True, help="Call graph output directory")
    p_cgq.add_argument("--query", default="", help="FTS5 search query (e.g., 'foo*')")
    p_cgq.add_argument("--node-id", default=None, help="If set, fetch node by numeric id")
    p_cgq.add_argument("--kind", default=None, help="Filter by node kind (function/var/struct...)")
    p_cgq.add_argument("--limit", type=int, default=50)

    p_cgtt = sub.add_parser("cgdb-time-travel",
                             help="Query node state at a past version (by commit_hash or version_id)")
    p_cgtt.add_argument("--graph", required=True)
    p_cgtt.add_argument("--node", required=True, help="Node id or symbol name")
    p_cgtt.add_argument("--version-id", default=None, help="Numeric version_id")
    p_cgtt.add_argument("--commit-hash", default=None, help="Git/svn commit hash")

    p_cgcf = sub.add_parser("cgdb-configs-for",
                             help="List config_predicate text_form(s) that gate a given node")
    p_cgcf.add_argument("--graph", required=True)
    p_cgcf.add_argument("--node", required=True, help="Node id or symbol name")

    p_cgoi = sub.add_parser("cgdb-ops-impls",
                             help="Find ops_bind implementations for a given field name")
    p_cgoi.add_argument("--graph", required=True)
    p_cgoi.add_argument("--field", required=True, help="Field name (e.g., 'open', 'read')")
    p_cgoi.add_argument("--struct", default=None, help="Optional struct type filter")

    p_cgcp = sub.add_parser("cgdb-cfg-paths",
                             help="Enumerate CFG paths through a function (entry → exit blocks)")
    p_cgcp.add_argument("--graph", required=True)
    p_cgcp.add_argument("--function", required=True, help="Function id or name")
    p_cgcp.add_argument("--max-len", type=int, default=10, help="Max path length")

    p_cgdfl = sub.add_parser("cgdb-data-flow",
                              help="Show data_flow entries (def-use chains) for a variable node")
    p_cgdfl.add_argument("--graph", required=True)
    p_cgdfl.add_argument("--var", required=True, help="Variable id or name")

    p_cgpr = sub.add_parser("cgdb-race-check",
                             help="Heuristic race-condition check for a function")
    p_cgpr.add_argument("--graph", required=True)
    p_cgpr.add_argument("--function", required=True, help="Function id or name")

    p_cgis = sub.add_parser("cgdb-index-status",
                             help="Overall cgdb index statistics: node/edge counts by kind, file count")
    p_cgis.add_argument("--graph", required=True)

    p_cgsq = sub.add_parser("cgdb-sql",
                            help="Run arbitrary read-only SQL against the cgdb database (cross-table joins, ad-hoc analysis)")
    p_cgsq.add_argument("--graph", required=True)
    p_cgsq.add_argument("--sql", required=True, help="SQL query (SELECT/WITH/EXPLAIN/PRAGMA only)")
    p_cgsq.add_argument("--format", choices=("json", "md"), default="json")

    p_cgvw = sub.add_parser("cgdb-views",
                            help="List/run predefined analysis views (hub functions, sync hotspots, doc coverage, etc.)")
    p_cgvw.add_argument("--graph", required=True)
    p_cgvw.add_argument("name", nargs="?", default=None,
                        help="View name to run (omit to list available views)")
    p_cgvw.add_argument("--format", choices=("json", "md"), default="json")

    p_cgsv = sub.add_parser("cgdb-schema-version",
                             help="Report current cgdb schema version and available migrations")
    p_cgsv.add_argument("--graph", required=True)

    p_cgv = sub.add_parser("cgdb-versions",
                            help="List graph_versions rows (newest first), or diff two versions")
    p_cgv.add_argument("--graph", required=True)
    p_cgv.add_argument("--limit", type=int, default=50)
    p_cgv.add_argument("--diff", nargs=2, metavar=("V1", "V2"),
                        help="Diff two version_ids instead of listing")
    p_cgv.add_argument("--diff-names", action="store_true",
                        help="With --diff, include name/fqn/kind for each changed node/edge")

    p_cgfc = sub.add_parser("cgdb-find-invokers",
                             help="Find callers (reverse closure) of a node via recursive CTE")
    p_cgfc.add_argument("--graph", required=True)
    p_cgfc.add_argument("--node", required=True, help="Node id or symbol name")
    p_cgfc.add_argument("--depth", type=int, default=1)
    p_cgfc.add_argument("--edge-types", default=None,
                        help="Comma-separated edge kinds (default: INVOKES)")
    p_cgfc.add_argument("--limit", type=int, default=200)
    p_cgfc.add_argument("--include-vtable-dispatch", action="store_true",
                        help="Also follow indirect dispatch via ops_bindings "
                             "and invoke_sites (finds vtable callers even "
                             "when no pre-computed INVOKES edge exists)")

    p_cgce = sub.add_parser("cgdb-find-invoked",
                             help="Find callees (forward closure) of a node via recursive CTE")
    p_cgce.add_argument("--graph", required=True)
    p_cgce.add_argument("--node", required=True, help="Node id or symbol name")
    p_cgce.add_argument("--depth", type=int, default=1)
    p_cgce.add_argument("--edge-types", default=None,
                        help="Comma-separated edge kinds (default: INVOKES)")
    p_cgce.add_argument("--limit", type=int, default=500)
    p_cgce.add_argument("--include-vtable-dispatch", action="store_true",
                        help="Also resolve vtable dispatch via ops_bindings")

    p_cgp = sub.add_parser("cgdb-path",
                            help="Find a call path from src to dst via recursive CTE")
    p_cgp.add_argument("--graph", required=True)
    p_cgp.add_argument("--src", required=True, help="Source node id or name")
    p_cgp.add_argument("--dst", required=True, help="Destination node id or name")
    p_cgp.add_argument("--max-len", type=int, default=10)

    p_cgd = sub.add_parser("cgdb-definition",
                            help="Find definition nodes (function/var/field/typedef) by name")
    p_cgd.add_argument("--graph", required=True)
    p_cgd.add_argument("--symbol", required=True)
    p_cgd.add_argument("--limit", type=int, default=10)

    p_cgfb = sub.add_parser("cgdb-function-body",
                             help="Return a function's body source text")
    p_cgfb.add_argument("--graph", required=True)
    p_cgfb.add_argument("--function", required=True, help="Function id or name")

    p_cgsl = sub.add_parser("cgdb-struct-layout",
                             help="Return a struct/union's field layout")
    p_cgsl.add_argument("--graph", required=True)
    p_cgsl.add_argument("--struct", required=True, help="Struct id or name")

    p_cgtd = sub.add_parser("cgdb-type-definition",
                             help="Find type definitions (struct/union/enum/typedef/class) by name")
    p_cgtd.add_argument("--graph", required=True)
    p_cgtd.add_argument("--type-name", required=True)
    p_cgtd.add_argument("--limit", type=int, default=10)

    p_cgnuc = sub.add_parser("cgdb-nodes-under-config",
                              help="Find all nodes gated by a given config predicate")
    p_cgnuc.add_argument("--graph", required=True)
    p_cgnuc.add_argument("--config", required=True,
                         help="Config predicate text (e.g., 'CONFIG_X86_64')")
    p_cgnuc.add_argument("--limit", type=int, default=500)

    p_cgpf = sub.add_parser("cgdb-path-feasible",
                             help="Check feasibility of a CFG path through blocks (uses Z3 if available)")
    p_cgpf.add_argument("--graph", required=True)
    p_cgpf.add_argument("--path", required=True,
                        help="Comma-separated block ids (e.g., '1,5,8,12')")
    p_cgpf.add_argument("--with-configs", default="",
                        help="Macro bindings for config-predicate feasibility "
                             "(e.g., 'CONFIG_X=true,CONFIG_Y=false')")

    p_cggsrc = sub.add_parser("cgdb-get-source",
                              help="Get source text for a node with byte-precise attribution")
    p_cggsrc.add_argument("--graph", required=True)
    p_cggsrc.add_argument("--node", required=True,
                          help="Node id or symbol name")
    p_cggsrc.add_argument("--snippet-only", action="store_true",
                          help="Only return source_snippet (no file read)")
    p_cggsrc.add_argument("--with-context", type=int, default=0,
                          help="Include N bytes of surrounding context")

    p_cgls = sub.add_parser("cgdb-layer-summary",
                            help="Generate cgdb_layer_summary.md report for all 13 cgdb tables")
    p_cgls.add_argument("--graph", required=True)

    # --- Cross-graph merge / suggest / tour ---
    p_cgm = sub.add_parser("cgdb-merge-knowledge",
                           help="Merge knowledge/memory from another branch's graph "
                                "into this one (graph structure stays based on target)")
    p_cgm.add_argument("--graph", required=True, help="Target graph directory")
    p_cgm.add_argument("--source-graph", required=True,
                        help="Source graph directory (another branch)")
    p_cgm.add_argument("--dry-run", action="store_true",
                       help="Only show what would be merged, don't write")
    p_cgm.add_argument("--no-knowledge", action="store_true",
                       help="Skip knowledge entries")
    p_cgm.add_argument("--no-memory", action="store_true",
                       help="Skip memory entries")

    p_cgs = sub.add_parser("cgdb-suggest",
                           help="Analyze the graph and suggest improvements "
                                "(missing invariants, duplicates, stale knowledge, etc.)")
    p_cgs.add_argument("--graph", required=True)
    p_cgs.add_argument("--top", type=int, default=20,
                       help="Max number of suggestions (default 20)")

    p_cgt = sub.add_parser("cgdb-tour",
                           help="Generate a guided codebase tour markdown "
                                "for new team member onboarding")
    p_cgt.add_argument("--graph", required=True)
    p_cgt.add_argument("--output", default=None,
                       help="Output path (default: <graph_dir>/CODEBASE_TOUR.md)")

    p_cgf = sub.add_parser("cgdb-freshness",
                            help="Check if the code graph is stale "
                                 "(source files changed since last scan)")
    p_cgf.add_argument("--graph", required=True)
    p_cgf.add_argument("--source", default=None,
                       help="Source root (default: parent of graph dir)")

    p_cgc = sub.add_parser("cgdb-compare",
                            help="Compare two graph directories (e.g., main vs feature branch)")
    p_cgc.add_argument("--graph", required=True, help="Target graph (e.g., main branch)")
    p_cgc.add_argument("--source-graph", required=True, help="Source graph (e.g., feature branch)")
    p_cgc.add_argument("--json", action="store_true", help="Write full diff to JSON file")

    p_em = sub.add_parser("export-mermaid",
                           help="Export call chains as Mermaid flowchart diagrams")
    p_em.add_argument("--graph", required=True)
    p_em.add_argument("--mode", choices=["chain", "domain", "paths", "function"],
                      default="chain", help="Export mode")
    p_em.add_argument("--node", default=None, help="Function name/ID")
    p_em.add_argument("--from", dest="from", default=None, help="Start function (chain mode)")
    p_em.add_argument("--to", dest="to", default=None, help="End function (chain mode)")
    p_em.add_argument("--depth", type=int, default=5)
    p_em.add_argument("--top", type=int, default=10, help="Top N paths (paths mode)")
    p_em.add_argument("--output", default=None, help="Output file path")
    p_em.add_argument("--multi", action="store_true",
                      help="Multi-project mode: render A -> B -> C dependency graph "
                           "with project-level nodes (boxes) and edge counts")

    # --- Report-layer CLI commands (13 new) ---
    p_rs = sub.add_parser("render-source", help="Render source from DB tokens")
    p_rs.add_argument("--graph", required=True)
    p_rs.add_argument("--file-id", type=int, default=None)
    p_rs.add_argument("--name", default=None, help="File name to resolve file_id")

    p_vc = sub.add_parser("verify-consistency", help="Verify DB render matches disk sha256")
    p_vc.add_argument("--graph", required=True)
    p_vc.add_argument("--file-id", type=int, default=None)
    p_vc.add_argument("--name", default=None)

    p_et = sub.add_parser("edit-token", help="Edit a token's spelling by token_id")
    p_et.add_argument("--graph", required=True)
    p_et.add_argument("--token-id", type=int, required=True)
    p_et.add_argument("--new-text", required=True)

    p_it = sub.add_parser("insert-token", help="Insert tokens after a given anchor token_id")
    p_it.add_argument("--graph", required=True)
    p_it.add_argument("--after-token-id", type=int, required=True)
    p_it.add_argument("--tokens-json", default=None, help="JSON array of token dicts")

    p_dt = sub.add_parser("delete-token", help="Delete a token by token_id")
    p_dt.add_argument("--graph", required=True)
    p_dt.add_argument("--token-id", type=int, required=True)

    p_fm = sub.add_parser("find-macros", help="Find macro definitions and invocations")
    p_fm.add_argument("--graph", required=True)
    p_fm.add_argument("--name", default=None)

    p_gpb = sub.add_parser("get-pp-branches", help="Get #ifdef branch tree for a file")
    p_gpb.add_argument("--graph", required=True)
    p_gpb.add_argument("--file-id", type=int, default=None)
    p_gpb.add_argument("--name", default=None)

    p_gsl = sub.add_parser("get-string-literals", help="Find string literals with optional pattern")
    p_gsl.add_argument("--graph", required=True)
    p_gsl.add_argument("--pattern", default=None)

    p_cdbtx = sub.add_parser("commit-db-transaction", help="Commit a write-back transaction (render+compile+lint+sha256+git)")
    p_cdbtx.add_argument("--graph", required=True)
    p_cdbtx.add_argument("--transaction-id", required=True)
    p_cdbtx.add_argument("--no-compile", action="store_true")
    p_cdbtx.add_argument("--run-lint", action="store_true")
    p_cdbtx.add_argument("--run-clang-format", action="store_true")
    p_cdbtx.add_argument("--git-commit", action="store_true")
    p_cdbtx.add_argument("--commit-message", default=None)

    p_rdbtx = sub.add_parser("rollback-db-transaction", help="Roll back a write-back transaction")
    p_rdbtx.add_argument("--graph", required=True)
    p_rdbtx.add_argument("--transaction-id", required=True)

    p_ina = sub.add_parser("insert-node-after", help="Insert a new AST node after an anchor")
    p_ina.add_argument("--graph", required=True)
    p_ina.add_argument("--ast-node-id", type=int, required=True)
    p_ina.add_argument("--node-spec-json", default=None)

    p_dn = sub.add_parser("delete-node", help="Soft-delete an AST node by ID")
    p_dn.add_argument("--graph", required=True)
    p_dn.add_argument("--ast-node-id", type=int, required=True)

    p_af = sub.add_parser("add-function", help="Add a new function to the graph")
    p_af.add_argument("--graph", required=True)
    p_af.add_argument("--signature", required=True)
    p_af.add_argument("--body-tokens-json", default=None)

    # --- Runtime guards + coverage ---
    p_rg = sub.add_parser("runtime-guards", help="Detect runtime guard patterns in path conditions")
    p_rg.add_argument("--graph", required=True)
    p_rg.add_argument("--conditions", required=True, help="Conditions separated by |||")

    p_cgcov = sub.add_parser("cgdb-coverage", help="Query graph coverage: --function NAME | --file PATH")
    p_cgcov.add_argument("--graph", required=True)
    p_cgcov.add_argument("--function", default=None)
    p_cgcov.add_argument("--file", default=None)

    p_cgwcov = sub.add_parser("cgdb-write-coverage", help="Rewrite coverage reports")
    p_cgwcov.add_argument("--graph", required=True)

    # --- ffi-persist ---
    p_ffiP = sub.add_parser("ffi-persist", help="Persist FFI edges into SQLite bridge tables")
    p_ffiP.add_argument("--graph", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Configure logging once, before dispatching to the subcommand handler.
    # All cmd_* modules call get_logger(__name__) at import time; the root
    # logger configured here will route their records.
    configure_logging(
        level=parse_log_level(getattr(args, "log_level", "INFO")),
        json_format=getattr(args, "log_json", False),
        log_file=getattr(args, "log_file", None),
    )

    commands = {
        "build": cmd_build,
        "load": cmd_load,
        "search": cmd_search,
        "describe-node": cmd_describe_node,
        "resolve-chain": cmd_resolve_chain,
        "neighbors": cmd_neighbors,
        "path": cmd_path,
        "impact": cmd_impact,
        "domain": cmd_domain,
        "merge": cmd_merge,
        "classify-endpoints": cmd_classify_endpoints,
        "update": cmd_update,
        "sync": cmd_sync,
        "extract-semantics": cmd_extract_semantics,
        "apply-semantics": cmd_apply_semantics,
        "update-node": cmd_update_node,
        "update-edge": cmd_update_edge,
        "patch-profile": cmd_patch_profile,
        "think-chain": cmd_think_chain,
        "save-memory": cmd_save_memory,
        "save": cmd_save_memory,  # SKILL.md alias
        "search-memory": cmd_search_memory,
        "recall": cmd_search_memory,  # SKILL.md alias
        "validate-memory": cmd_validate_memory,
        "export-html": cmd_export_html,
        "export-obsidian": cmd_export_obsidian,
        "plugins": cmd_plugins,
        "trace-chain": cmd_trace_chain,
        "reverse-trace": cmd_reverse_trace,
        "diff-chains": cmd_diff_chains,
        "concurrency-risks": cmd_concurrency_risks,
        "data-lifecycle": cmd_data_lifecycle,
        "detect-races": cmd_detect_races,
        "concurrency-analyze": cmd_concurrency_analyze,
        "happens-before": cmd_happens_before,
        "memory-ordering": cmd_memory_ordering,
        "explain-label": cmd_explain_label,
        "why-ambiguous": cmd_why_ambiguous,
        "audit-log": cmd_audit_log,
        "who-allocates": cmd_who_allocates,
        "who-frees": cmd_who_frees,
        "unbalanced-alloc-free": cmd_unbalanced_alloc_free,
        "who-locks": cmd_who_locks,
        "add-semantic-edges": cmd_add_semantic_edges,
        "extract-invariants-llm": cmd_extract_invariants_llm,
        "graph-history": cmd_graph_history,
        "graph-diff": cmd_graph_diff,
        "graph-record-version": cmd_graph_record_version,
        "validate-plugin": cmd_validate_plugin,
        "manage-memory": cmd_manage_memory,
        "memory-health": cmd_memory_health,
        "extract-knowledge": cmd_extract_knowledge,
        "apply-knowledge": cmd_apply_knowledge,
        "knowledge-query": cmd_knowledge_query,
        "know": cmd_knowledge_query,  # SKILL.md alias
        "knowledge-validate": cmd_knowledge_validate,
        "kb-rebuild-index": cmd_kb_rebuild_index,
        "kb-query": cmd_kb_query,
        "kb-cluster": cmd_kb_cluster,
        "kb-migrate": cmd_kb_migrate,
        "kb-known-unknowns": cmd_kb_known_unknowns,
        "kb-audit": cmd_kb_audit,
        "kb-conflict": cmd_kb_conflict,
        "kb-rollback": cmd_kb_rollback,
        "kb-forget": cmd_kb_forget,
        "kb-global-add": cmd_kb_global_add,
        "kb-global-search": cmd_kb_global_search,
        "kb-global-share": cmd_kb_global_share,
        "kb-global-import": cmd_kb_global_import,
        "build-multi": cmd_build_multi,
        "c2d-add-foreign": cmd_c2d_add_foreign,
        "c2d-sync-foreign": cmd_c2d_sync_foreign,
        "c2d-list-foreign": cmd_c2d_list_foreign,
        "c2d-remove-foreign": cmd_c2d_remove_foreign,
        "composite-query": cmd_composite_query,
        "c2d-check-compat": cmd_c2d_check_compat,
        "coverage-cross-c2d": cmd_coverage_cross_c2d,
        "c2d-add-foreign-stub": cmd_c2d_add_foreign_stub,
        "ffi-auto-link": cmd_ffi_auto_link,
        "scan-rpc": cmd_scan_rpc,
        "import-foreign-knowledge": cmd_import_foreign_knowledge,
        "patch-from-diff": cmd_patch_from_diff,
        "patch-from-git": cmd_patch_from_git,
        "light-scan": cmd_light_scan,
        "explore-flow": cmd_explore_flow,
        "key-paths": cmd_key_paths,
        "quick-update": cmd_quick_update,
        "export-changes": cmd_export_changes,
        "merge-changes": cmd_merge_changes,
        "semantic-status": cmd_semantic_status,
        "install-hook": cmd_install_hook,
        "serve": _lazy("_builder.mcp_server", "cmd_serve"),
        "get-code-snippet": cmd_get_code_snippet,
        "blast-radius": cmd_blast_radius,
        "field-access": cmd_field_access,
        "field-flow": cmd_field_flow,
        "io-path": cmd_io_path,
        "param-flow": cmd_param_flow,
        "extract-signals": cmd_extract_signals,
        "watch": cmd_watch,
        # Deficiency 2: commit-aware provenance commands
        "describe-commit": cmd_describe_commit,
        "node-history": cmd_node_history,
        "graph-provenance": cmd_graph_provenance,
        "blame-node": cmd_blame_node,
        "find-commits": cmd_find_commits,
        # Deficiency 3: unified query language
        "query": cmd_query,
        # Deficiency 4 (P0): value flow
        "value-flow": cmd_value_flow,
        # Deficiency 5 (P0): lock coverage
        "lock-coverage": cmd_lock_coverage,
        # Deficiency 6: path feasibility
        "path-feasible": cmd_path_feasible,
        "path-guards": cmd_path_guards,
        # Deficiency 7: data dependency
        "data-dep": cmd_data_dep,
        # Deficiency 8: invariants
        "extract-invariants": cmd_extract_invariants,
        "find-invariants": cmd_find_invariants,
        "apply-invariants": cmd_apply_invariants,
        # Deficiency 9: auto semantic enhancement
        "auto-enhance": cmd_auto_enhance,
        "batch-confirm": cmd_batch_confirm,
        "rollback": cmd_rollback,
        "fill-request": cmd_fill_request,
        "heuristic-enhance": cmd_heuristic_enhance,
        # Deficiency 10: transactional updates
        "tx-begin": cmd_tx_begin,
        "tx-commit": cmd_tx_commit,
        "tx-rollback": cmd_tx_rollback,
        "tx-status": cmd_tx_status,
        "tx-snapshot": cmd_tx_snapshot,
        "tx-restore": cmd_tx_restore,
        "tx-list-snapshots": cmd_tx_list_snapshots,
        "tx-replay-wal": cmd_tx_replay_wal,
        # Deficiency 11: cross-language FFI
        "ffi-detect": cmd_ffi_detect,
        "ffi-list": cmd_ffi_list,
        "ffi-trace": cmd_ffi_trace,
        "ffi-types": cmd_ffi_types,
        # D45+D16 optimization: intent router
        "intent-query": cmd_intent_query,
        # D24 enhancement: TF-IDF char n-gram embeddings
        "embeddings-build": _lazy("_builder.embeddings", "cmd_embeddings_build"),
        "embeddings-search": _lazy("_builder.embeddings", "cmd_embeddings_search"),
        # Deficiency 12: interactive Web UI
        "web-ui": _lazy("_builder.web_ui", "cmd_web_ui"),
        # Deficiency 13: BUG benchmark
        "bug-benchmark": _lazy("_builder.bug_benchmark", "cmd_bug_benchmark"),
        # Deficiency 14: profile health + auto-evolution
        "profile-health": cmd_profile_health,
        "profile-evolve": cmd_profile_evolve,
        "profile-bind-version": cmd_profile_bind_version,
        # Deficiency 15: doc-code dual source truth alignment
        "doc-code-check": cmd_doc_code_check,
        "doc-mark-stale": cmd_doc_mark_stale,
        "doc-alignment-report": cmd_doc_alignment_report,
        "doc-signature-diff": cmd_doc_signature_diff,
        # Deficiency 16 (P0): background daemon
        "daemon-start": _lazy("_builder.daemon", "cmd_daemon_start"),
        "daemon-stop": _lazy("_builder.daemon", "cmd_daemon_stop"),
        "daemon-status": _lazy("_builder.daemon", "cmd_daemon_status"),
        "daemon-force-refresh": _lazy("_builder.daemon", "cmd_daemon_force_refresh"),
        "daemon-pause": _lazy("_builder.daemon", "cmd_daemon_pause"),
        "daemon-resume": _lazy("_builder.daemon", "cmd_daemon_resume"),
        "daemon-wait-sync": _lazy("_builder.daemon", "cmd_daemon_wait_sync"),
        "daemon-logs": _lazy("_builder.daemon", "cmd_daemon_logs"),
        "daemon-reload": _lazy("_builder.daemon", "cmd_daemon_reload"),
        "daemon-list-projects": _lazy("_builder.daemon", "cmd_daemon_list_projects"),
        # cgdb direct queries
        "cgdb-query": cmd_cgdb_query,
        "cgdb-time-travel": cmd_cgdb_time_travel,
        "cgdb-configs-for": cmd_cgdb_configs_for,
        "cgdb-ops-impls": cmd_cgdb_ops_impls,
        "cgdb-cfg-paths": cmd_cgdb_cfg_paths,
        "cgdb-data-flow": cmd_cgdb_data_flow,
        "cgdb-race-check": cmd_cgdb_race_check,
        "cgdb-index-status": cmd_cgdb_index_status,
        "cgdb-sql": cmd_cgdb_sql,
        "cgdb-views": cmd_cgdb_views,
        "cgdb-schema-version": cmd_cgdb_schema_version,
        "cgdb-versions": cmd_cgdb_versions,
        "cgdb-find-invokers": cmd_cgdb_find_invokers,
        "cgdb-find-invoked": cmd_cgdb_find_invoked,
        "cgdb-path": cmd_cgdb_path,
        "cgdb-definition": cmd_cgdb_definition,
        "cgdb-function-body": cmd_cgdb_function_body,
        "cgdb-struct-layout": cmd_cgdb_struct_layout,
        "cgdb-type-definition": cmd_cgdb_type_definition,
        "cgdb-nodes-under-config": cmd_cgdb_nodes_under_config,
        "cgdb-path-feasible": cmd_cgdb_path_feasible,
        "cgdb-get-source": cmd_cgdb_get_source,
        "cgdb-layer-summary": cmd_cgdb_layer_summary,
        "cgdb-merge-knowledge": _lazy("_builder.cgdb_merge", "cmd_cgdb_merge_knowledge"),
        "cgdb-suggest": _lazy("_builder.cgdb_suggest", "cmd_cgdb_suggest"),
        "cgdb-tour": _lazy("_builder.cgdb_tour", "cmd_cgdb_tour"),
        "cgdb-freshness": _lazy("_builder.cgdb_freshness", "cmd_cgdb_freshness"),
        "cgdb-compare": _lazy("_builder.cgdb_compare", "cmd_cgdb_compare"),
        "export-mermaid": _lazy("_builder.export_mermaid", "cmd_export_mermaid"),
        "render-source": cmd_render_source,
        "verify-consistency": cmd_verify_consistency,
        "edit-token": cmd_edit_token,
        "insert-token": cmd_insert_token,
        "delete-token": cmd_delete_token,
        "find-macros": cmd_find_macros,
        "get-pp-branches": cmd_get_pp_branches,
        "get-string-literals": cmd_get_string_literals,
        "commit-db-transaction": cmd_commit_db_transaction,
        "rollback-db-transaction": cmd_rollback_db_transaction,
        "insert-node-after": cmd_insert_node_after,
        "delete-node": cmd_delete_node,
        "add-function": cmd_add_function,
        "runtime-guards": cmd_runtime_guards,
        "cgdb-coverage": _lazy("_builder.cgdb_commands", "cmd_cgdb_coverage"),
        "cgdb-write-coverage": _lazy("_builder.cgdb_commands", "cmd_cgdb_write_coverage"),
        "ffi-persist": _lazy("_builder.ffi_bridge", "cmd_ffi_persist"),
    }
    handler = commands.get(args.command)
    if handler is None:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(2)

    logger = get_logger("cli")
    try:
        result = handler(args)
        if isinstance(result, str) and result:
            sys.stdout.write(result)
            if not result.endswith("\n"):
                sys.stdout.write("\n")
    except KeyboardInterrupt:
        logger.warning("interrupted_by_user")
        sys.exit(130)
    except SystemExit:
        raise  # propagate explicit sys.exit() calls
    except Exception as exc:
        logger.error("command_failed", exc_info=True,
                     extra={"command": args.command, "error": str(exc)})
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
