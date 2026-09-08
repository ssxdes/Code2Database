"""callgraph builder implementation modules.

This package contains the implementation split from the original
code2database_builder.py monolith. The entry script code2database_builder.py
imports from here and routes commands to the appropriate module.

Lazy import: modules are only loaded when accessed, reducing startup time.
"""

import importlib

_LAZY_IMPORTS = {
    # utils
    "_normalize_id": ("_builder.utils", "_normalize_id"),
    "_resolve_invoked_id": ("_builder.utils", "_resolve_invoked_id"),
    "_find_node_id": ("_builder.utils", "_find_node_id"),
    "_parse_bindings": ("_builder.utils", "_parse_bindings"),
    "_load_globals": ("_builder.utils", "_load_globals"),
    "_is_condition_alive": ("_builder.utils", "_is_condition_alive"),
    "_output_result": ("_builder.utils", "_output_result"),
    "_print_structured": ("_builder.utils", "_print_structured"),
    "_simple_tokenize": ("_builder.utils", "_simple_tokenize"),
    "_similarity_score": ("_builder.utils", "_similarity_score"),
    "_extract_chain_node_ids": ("_builder.utils", "_extract_chain_node_ids"),
    "_memory_dir": ("_builder.utils", "_memory_dir"),
    "_experience_dir": ("_builder.utils", "_experience_dir"),
    "_is_parser_artifact": ("_builder.utils", "_is_parser_artifact"),
    "_build_suffix_index": ("_builder.utils", "_build_suffix_index"),
    # graph_build
    "build_graph": ("_builder.graph.graph_build", "build_graph"),
    "split_by_domain": ("_builder.graph.graph_build", "split_by_domain"),
    "_load_full_graph": ("_builder.graph.graph_build", "_load_full_graph"),
    "_domain_subdir": ("_builder.graph.graph_build", "_domain_subdir"),
    "_detect_build_system": ("_builder.graph.graph_build", "_detect_build_system"),
    "cmd_build": ("_builder.graph.graph_build", "cmd_build"),
    "_build_domain_readmes": ("_builder.graph.graph_build", "_build_domain_readmes"),
    # entry_scoring
    "_calculate_entry_point_score": ("_builder.profile.entry_scoring", "_calculate_entry_point_score"),
    "_score_entry_points": ("_builder.profile.entry_scoring", "_score_entry_points"),
    "_detect_processes": ("_builder.profile.entry_scoring", "_detect_processes"),
    "_generate_process_label": ("_builder.profile.entry_scoring", "_generate_process_label"),
    # import_resolve
    "_resolve_imports": ("_builder.build.import_resolve", "_resolve_imports"),
    "_compute_fqn": ("_builder.build.import_resolve", "_compute_fqn"),
    "_multi_strategy_resolve": ("_builder.build.import_resolve", "_multi_strategy_resolve"),
    "_build_resolve_lookups": ("_builder.build.import_resolve", "_build_resolve_lookups"),
    # index_pack
    "_mark_endpoint_nodes": ("_builder.export.index_pack", "_mark_endpoint_nodes"),
    "_build_indexes": ("_builder.export.index_pack", "_build_indexes"),
    "_build_callgraph_summary_md": ("_builder.export.index_pack", "_build_callgraph_summary_md"),
    "_build_scenarios_file": ("_builder.export.index_pack", "_build_scenarios_file"),
    "_build_context_pack": ("_builder.export.index_pack", "_build_context_pack"),
    "_compute_hub_functions": ("_builder.export.index_pack", "_compute_hub_functions"),
    "_compute_cross_domain_hotspots": ("_builder.export.index_pack", "_compute_cross_domain_hotspots"),
    "_compute_scenarios": ("_builder.export.index_pack", "_compute_scenarios"),
    "_compute_data_flow": ("_builder.export.index_pack", "_compute_data_flow"),
    "_build_scenarios_summary_md": ("_builder.export.index_pack", "_build_scenarios_summary_md"),
    "_generate_mermaid_path_diagram": ("_builder.export.index_pack", "_generate_mermaid_path_diagram"),
    # plugins
    "_load_plugins": ("_builder.ops.plugins", "_load_plugins"),
    "_discover_plugins": ("_builder.ops.plugins", "_discover_plugins"),
    "_run_plugins": ("_builder.ops.plugins", "_run_plugins"),
    "cmd_plugins": ("_builder.ops.plugins", "cmd_plugins"),
    "cmd_validate_plugin": ("_builder.ops.plugins", "cmd_validate_plugin"),
    # query
    "cmd_describe_node": ("_builder.query.query", "cmd_describe_node"),
    "cmd_resolve_chain": ("_builder.query.query", "cmd_resolve_chain"),
    "cmd_trace_chain": ("_builder.query.query", "cmd_trace_chain"),
    "cmd_diff_chains": ("_builder.query.query", "cmd_diff_chains"),
    "_resolve_detailed_chain": ("_builder.query.query", "_resolve_detailed_chain"),
    "_trace_simple_chain": ("_builder.query.query", "_trace_simple_chain"),
    "_resolve_simple_chain": ("_builder.query.query", "_resolve_simple_chain"),
    "cmd_get_code_snippet": ("_builder.query.query", "cmd_get_code_snippet"),
    "cmd_blast_radius": ("_builder.query.query", "cmd_blast_radius"),
    "cmd_reverse_trace": ("_builder.query.query", "cmd_reverse_trace"),
    "cmd_field_access": ("_builder.query.query", "cmd_field_access"),
    "_get_code_snippet": ("_builder.query.query", "_get_code_snippet"),
    # search_cmd
    "cmd_load": ("_builder.query.search_cmd", "cmd_load"),
    "cmd_search": ("_builder.query.search_cmd", "cmd_search"),
    "cmd_path": ("_builder.query.search_cmd", "cmd_path"),
    "cmd_neighbors": ("_builder.query.search_cmd", "cmd_neighbors"),
    "cmd_impact": ("_builder.query.search_cmd", "cmd_impact"),
    "cmd_domain": ("_builder.query.search_cmd", "cmd_domain"),
    # export
    "cmd_export_html": ("_builder.export.export", "cmd_export_html"),
    "cmd_export_obsidian": ("_builder.export.export", "cmd_export_obsidian"),
    # memory_cmd
    "cmd_save_memory": ("_builder.memory.memory_cmd", "cmd_save_memory"),
    "cmd_search_memory": ("_builder.memory.memory_cmd", "cmd_search_memory"),
    "cmd_validate_memory": ("_builder.memory.memory_cmd", "cmd_validate_memory"),
    "_auto_validate_memory": ("_builder.memory.memory_cmd", "_auto_validate_memory"),
    # update_sync
    "cmd_merge": ("_builder.build.update_sync", "cmd_merge"),
    "cmd_update": ("_builder.build.update_sync", "cmd_update"),
    "cmd_sync": ("_builder.build.update_sync", "cmd_sync"),
    "_prune_nodes_by_source": ("_builder.build.update_sync", "_prune_nodes_by_source"),
    # semantics
    "cmd_extract_semantics": ("_builder.graph.semantics", "cmd_extract_semantics"),
    "cmd_apply_semantics": ("_builder.graph.semantics", "cmd_apply_semantics"),
    "cmd_think_chain": ("_builder.graph.semantics", "cmd_think_chain"),
    "cmd_classify_endpoints": ("_builder.graph.semantics", "cmd_classify_endpoints"),
    "cmd_extract_signals": ("_builder.graph.semantics", "cmd_extract_signals"),
    # concurrency
    "cmd_concurrency_risks": ("_builder.analysis.concurrency", "cmd_concurrency_risks"),
    "cmd_data_lifecycle": ("_builder.analysis.concurrency", "cmd_data_lifecycle"),
    # concurrency_analysis
    "cmd_concurrency_analyze": ("_builder.analysis.concurrency_analysis", "cmd_concurrency_analyze"),
    # explore
    "cmd_explore_flow": ("_builder.query.explore", "cmd_explore_flow"),
    # key_paths
    "cmd_key_paths": ("_builder.profile.key_paths", "cmd_key_paths"),
    # token_budget
    "estimate_tokens": ("_builder.token_budget", "estimate_tokens"),
    "truncate_to_tokens": ("_builder.token_budget", "truncate_to_tokens"),
    "budget_pack": ("_builder.token_budget", "budget_pack"),
    "budget_describe": ("_builder.token_budget", "budget_describe"),
    # memory_manager
    "MemoryManager": ("_builder.memory.memory_manager", "MemoryManager"),
    "cmd_manage_memory": ("_builder.memory.memory_manager", "cmd_manage_memory"),
    "cmd_memory_health": ("_builder.memory.memory_manager", "cmd_memory_health"),
    # memory_store
    "MemoryStore": ("_builder.memory.memory_store", "MemoryStore"),
    "cmd_memory_store_categories": ("_builder.memory.memory_store", "cmd_memory_store_categories"),
    "cmd_memory_store_split": ("_builder.memory.memory_store", "cmd_memory_store_split"),
    "cmd_memory_store_merge": ("_builder.memory.memory_store", "cmd_memory_store_merge"),
    "cmd_memory_store_move": ("_builder.memory.memory_store", "cmd_memory_store_move"),
    # session_init
    "cmd_session_init": ("_builder.mcp.session_init", "cmd_session_init"),
    "build_session_context": ("_builder.mcp.session_init", "build_session_context"),
    "render_session_context": ("_builder.mcp.session_init", "render_session_context"),
    # brief
    "cmd_knowledge_brief": ("_builder.kb.brief", "cmd_knowledge_brief"),
    "cmd_brief_update": ("_builder.kb.brief", "cmd_brief_update"),
    "cmd_brief_extract": ("_builder.kb.brief", "cmd_brief_extract"),
    "cmd_brief_validate": ("_builder.kb.brief", "cmd_brief_validate"),
    "render_brief_prompt": ("_builder.kb.brief", "render_brief_prompt"),
    "load_brief": ("_builder.kb.brief", "load_brief"),
    "validate_brief": ("_builder.kb.brief", "validate_brief"),
    # patcher
    "patch_from_diff": ("_builder.ops.patcher", "patch_from_diff"),
    "patch_from_git": ("_builder.ops.patcher", "patch_from_git"),
    "light_scan": ("_builder.ops.patcher", "light_scan"),
    "cmd_patch_from_diff": ("_builder.ops.patcher", "cmd_patch_from_diff"),
    "cmd_patch_from_git": ("_builder.ops.patcher", "cmd_patch_from_git"),
    "cmd_light_scan": ("_builder.ops.patcher", "cmd_light_scan"),
    "check_update_threshold": ("_builder.ops.patcher", "check_update_threshold"),
    "lazy_fill_node": ("_builder.ops.patcher", "lazy_fill_node"),
    # changelog_update
    "cmd_quick_update": ("_builder.build.changelog_update", "cmd_quick_update"),
    "cmd_export_changes": ("_builder.build.changelog_update", "cmd_export_changes"),
    "cmd_merge_changes": ("_builder.build.changelog_update", "cmd_merge_changes"),
    "cmd_semantic_status": ("_builder.build.changelog_update", "cmd_semantic_status"),
    "quick_update": ("_builder.build.changelog_update", "quick_update"),
    "export_change_graph": ("_builder.build.changelog_update", "export_change_graph"),
    "merge_change_graph": ("_builder.build.changelog_update", "merge_change_graph"),
    "get_semantic_update_status": ("_builder.build.changelog_update", "get_semantic_update_status"),
    # mcp_server
    "cmd_serve": ("_builder.mcp.mcp_server", "cmd_serve"),
    # lsp_server
    "cmd_lsp_server": ("_builder.misc.lsp_server", "cmd_lsp_server"),
    "LSPServer": ("_builder.misc.lsp_server", "LSPServer"),
}

_CACHE = {}


def __getattr__(name):
    if name in _LAZY_IMPORTS:
        if name not in _CACHE:
            module_path, attr_name = _LAZY_IMPORTS[name]
            mod = importlib.import_module(module_path)
            _CACHE[name] = getattr(mod, attr_name)
        return _CACHE[name]
    raise AttributeError(f"module '_builder' has no attribute {name!r}")
