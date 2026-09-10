"""MCP server mode for Code2Database.

Exposes core query commands as MCP tools over stdio transport,
enabling LLM agents to query the invocation graph in real-time without
CLI subprocess calls.

Usage:
    python code2database_builder.py serve --graph code2db-out/

MCP Tools exposed (83 total):
    - 36 code2database_* tools (load, search, describe, explore, trace,
      impact, key_paths, concurrency, data_lifecycle, domain, knowledge_query,
      memory_search, semantic_status, blast_radius, field_access,
      session_init, etc.)
    - 19 cgdb_* tools (search_symbols, get_definition, get_function_body,
      get_struct_layout, find_type_definition, find_invokers, find_invoked,
      find_ops_impls, find_cfg_paths, find_data_flow, find_aliases,
      find_lock_held_calls, check_race_condition, find_configs_for,
      find_nodes_under_config, index_status, time_travel_query,
      list_versions, get_source)
    - 28 design-report tools (render_source, verify_consistency, edit_token,
      insert_token, delete_token, find_macros, get_pp_branches,
      get_string_literals, commit/rollback_db_transaction, insert_node_after,
      delete_node, add_function, find_symbol, callers_of, callees_of,
      who_writes, who_reads, get_context, impact_analysis, get_module_view,
      indirect_targets, alias_set, trace_data_flow, cfg_of,
      path_sensitive_states, precise_write_set, dead_code_in)
"""

import json
import sys
import os
import atexit
from pathlib import Path

from _builder.token_budget import estimate_tokens
import logging

from _builder.mcp.mcp_cache import (
    _GRAPH_CACHE, _GRAPH_CACHE_LOCK,
    _CGDB_STORE_CACHE, _CGDB_STORE_CACHE_LOCK,
    _get_graph, _close_cached_graphs,
    _drop_cgdb_store, _close_cached_cgdb_stores,
    _cgdb_store, _mcp_coerce_str, _mcp_coerce_int,
)


from _builder.mcp.mcp_c2d_tools import _tool_load, _tool_search, _tool_describe, _tool_explore, _tool_trace, _tool_impact, _tool_key_paths, _tool_concurrency, _tool_data_lifecycle, _tool_domain, _tool_knowledge_query, _tool_memory_search, _tool_kb_query, _tool_save_memory, _tool_session_init, _tool_semantic_status, _tool_foreign_refs, _tool_sync_foreign, _tool_composite_query, _tool_get_code_snippet, _tool_blast_radius, _tool_extract_signals, _tool_path_feasible, _tool_find_invariants, _tool_ffi_trace, _tool_doc_code_check, _tool_daemon_status, _tool_who_allocates, _tool_who_frees, _tool_who_locks, _tool_explain_label, _tool_why_ambiguous, _tool_audit_log, _tool_happens_before, _tool_memory_ordering, _tool_unbalanced_alloc_free

from _builder.mcp.mcp_cgdb_tools import _tool_cgdb_search_symbols, _tool_cgdb_get_definition, _tool_cgdb_get_function_body, _tool_cgdb_get_source, _tool_cgdb_find_invokers, _tool_cgdb_find_invoked, _tool_cgdb_get_struct_layout, _tool_cgdb_find_type_definition, _tool_cgdb_find_ops_impls, _tool_cgdb_find_cfg_paths, _tool_cgdb_find_data_flow, _tool_cgdb_find_aliases, _tool_cgdb_find_lock_held_calls, _tool_cgdb_check_race_condition, _tool_cgdb_find_configs_for, _tool_cgdb_find_nodes_under_config, _tool_cgdb_index_status, _tool_cgdb_time_travel_query, _tool_cgdb_list_versions


# ---------------------------------------------------------------------------
# MCP stdio transport
# ---------------------------------------------------------------------------

# Sentinel for EOF detection — distinct from None (which means "parse error,
# skip and continue"). Using a sentinel object lets the main loop distinguish
# "stdin closed, should exit" from "malformed JSON, skip this message".
_EOF_SENTINEL = object()



def _read_message():
    """Read a JSON-RPC message from stdin (MCP stdio transport).

    Returns:
        dict: parsed JSON-RPC message.
        _EOF_SENTINEL: stdin reached EOF (client disconnected).
        None: malformed/unparseable message (skip, continue loop).

    Implements Content-Length header parsing per MCP spec:
    https://spec.modelcontextprotocol.io/specification/basic/transports/#stdio

    Fallback: when no Content-Length header is present (simple line-based
    clients), the first non-empty line is treated as the JSON body. This
    handles both framed and unframed inputs.
    """
    # Read headers until empty line. A line that doesn't look like a header
    # (no "key: value" pattern) is treated as the body for the fallback path.
    content_length = None
    fallback_body = None
    while True:
        line = sys.stdin.readline()
        if not line:
            return _EOF_SENTINEL  # EOF — client disconnected
        line = line.strip()
        if not line:
            break  # End of headers
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        elif ":" in line and not line.startswith("{"):
            # Looks like another header (e.g., "Content-Type: ...") — ignore.
            continue
        else:
            # Doesn't look like a header — treat as the body for fallback.
            fallback_body = line
            break

    if content_length is not None:
        # Read exactly content_length bytes. Content-Length is a byte count
        # per the MCP/LSP framing spec, but sys.stdin is a text stream whose
        # .read(n) counts CHARACTERS — non-ASCII (e.g. CJK query text)
        # desynchronizes the stream. Read bytes from the underlying buffer
        # and decode, so multi-byte UTF-8 bodies are framed correctly.
        # Fallback to the text stream when .buffer is unavailable (e.g.
        # StringIO-backed tests), where the byte/char divergence does not
        # arise for ASCII content.
        stdin_buffer = getattr(sys.stdin, "buffer", None)
        if stdin_buffer is not None:
            raw = stdin_buffer.read(content_length)
            if not raw:
                return _EOF_SENTINEL  # EOF
            data = raw.decode("utf-8", errors="replace")
        else:
            data = sys.stdin.read(content_length)
            if not data:
                return _EOF_SENTINEL  # EOF
    elif fallback_body is not None:
        # Fallback: the line we already read IS the JSON body.
        data = fallback_body
    else:
        # Fallback: try reading a single JSON line (for simple clients).
        line = sys.stdin.readline()
        if not line:
            return _EOF_SENTINEL  # EOF
        data = line.strip()

    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None



def _write_message(msg: dict):
    """Write a JSON-RPC message to stdout (MCP stdio transport).

    Includes Content-Length header per MCP spec.
    """
    body = json.dumps(msg, ensure_ascii=False)
    header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
    sys.stdout.write(header + body)
    sys.stdout.flush()



# ---------------------------------------------------------------------------
# Tool registry & dispatch
# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {
    "code2database_load": {
        "description": "Load and summarize the invocation graph. Returns node/edge/domain counts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_load,
    },
    "code2database_search": {
        "description": "Search nodes by keywords. Returns matching nodes with scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "Space-separated keywords"},
                "top": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": ["keywords"],
        },
        "handler": _tool_search,
    },
    "code2database_describe": {
        "description": "Describe a node by ID or name. Returns function details at brief/standard/full level.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID or name"},
                "detail": {"type": "string", "enum": ["brief", "standard", "full"],
                          "description": "Detail level (default brief)"},
            },
            "required": ["node"],
        },
        "handler": _tool_describe,
    },
    "code2database_explore": {
        "description": "One-shot context retrieval by natural language query. Returns relevant nodes, paths, and conditions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language or symbol query"},
                "max_nodes": {"type": "integer", "description": "Max nodes (default 15)"},
                "max_tokens": {"type": "integer", "description": "Max tokens (default 2000)"},
            },
            "required": ["query"],
        },
        "handler": _tool_explore,
    },
    "code2database_trace": {
        "description": "Trace call chain from one function to another. Returns annotated path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Source node ID or name"},
                "to": {"type": "string", "description": "Target node ID or name"},
            },
            "required": ["from"],
        },
        "handler": _tool_trace,
    },
    "code2database_impact": {
        "description": "Impact analysis for a node. Returns upstream (reverse) or downstream (forward) affected nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID or name"},
                "direction": {"type": "string", "enum": ["reverse", "forward"],
                             "description": "Direction (default reverse)"},
                "depth": {"type": "integer", "description": "Traverse depth (default 3)"},
            },
            "required": ["node"],
        },
        "handler": _tool_impact,
    },
    "code2database_key_paths": {
        "description": "Extract key execution paths from entry points.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "Number of paths (default 5)"},
                "from_entry": {"type": "string", "description": "Specific entry point (optional)"},
            },
            "required": [],
        },
        "handler": _tool_key_paths,
    },
    "code2database_concurrency": {
        "description": "List concurrency risk points sorted by risk level.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": [],
        },
        "handler": _tool_concurrency,
    },
    "code2database_data_lifecycle": {
        "description": "Trace resource allocation-usage-release paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Resource keyword (e.g. 'buffer')"},
            },
            "required": ["resource"],
        },
        "handler": _tool_data_lifecycle,
    },
    "code2database_domain": {
        "description": "List nodes/edges in a domain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Domain name (e.g. lib.bdev)"},
            },
            "required": ["name"],
        },
        "handler": _tool_domain,
    },
    "code2database_knowledge_query": {
        "description": "Query knowledge by topic. Returns relevant knowledge entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to search"},
                "max_tokens": {"type": "integer", "description": "Max output tokens (default 500)"},
            },
            "required": ["topic"],
        },
        "handler": _tool_knowledge_query,
    },
    "code2database_memory_search": {
        "description": "Search memory for similar questions. Returns Q&A pairs with relevance scores. Optional symbol filter restricts to memories grounded to a specific graph symbol (function/type).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "top": {"type": "integer", "description": "Max results (default 5)"},
                "symbol": {"type": "string", "description": "Filter by grounded symbol name (exact, case-insensitive)"},
            },
            "required": ["query"],
        },
        "handler": _tool_memory_search,
    },
    "code2database_kb_query": {
        "description": "Unified FTS5+BM25 query across memory + knowledge. Searches both stores via the shared kb_paragraphs_fts index. Returns ranked results with source kind (memory_qa / knowledge_principle / etc.), score, and body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-form text query (tokenized and AND-joined for FTS5 MATCH)"},
                "top": {"type": "integer", "description": "Max results (default 10)"},
                "kinds": {"type": "string", "description": "Comma-separated kind filter (e.g. 'memory_qa,knowledge_principle')"},
                "min_weight": {"type": "number", "description": "Skip rows with weight below this (default 0.0 = no filter)"},
                "max_tokens": {"type": "integer", "description": "Approximate char cap on returned bodies (default 4000)"},
            },
            "required": ["query"],
        },
        "handler": _tool_kb_query,
    },
    "code2database_save_memory": {
        "description": "Save a Q&A to the project memory store (veteran experience accumulation). Use after answering a project question worth remembering, or with correct=true when a previously stored answer was WRONG (reshapes the most similar entry in place — no duplicate variant). symbol (comma-separated) grounds the memory to graph symbols.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question asked"},
                "answer": {"type": "string", "description": "The answer given"},
                "category": {"type": "string", "description": "Category path (e.g. bdev/nvme/pcie; auto-created)"},
                "author": {"type": "string", "description": "Author attribution"},
                "tags": {"type": "string", "description": "Comma-separated tags"},
                "symbol": {"type": "string", "description": "Comma-separated graph symbol names this memory is about"},
                "correct": {"type": "boolean", "description": "Correction path: reshape the most similar existing entry instead of adding a variant (default false)"},
            },
            "required": ["question"],
        },
        "handler": _tool_save_memory,
    },
    "code2database_session_init": {
        "description": "One-shot session context: project brief (mandatory rules/modes/pitfalls), memory digest (veteran Q&A ranked by weight), graph state with brief-drift + source-freshness (stale graph) warnings, and known-unknowns (repeatedly unanswered queries worth capturing into memory). Call this FIRST at session start, before search/describe/trace. Every layer degrades to a hint when absent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "Max memory digest entries (default 10)"},
            },
            "required": [],
        },
        "handler": _tool_session_init,
    },
    "code2database_foreign_refs": {
        "description": "List cross-C2D foreign references for a node. Shows which functions in external C2Ds (project A) are called by this node (project B). Returns foreign_node_id, name, domain, source_file, signature, status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Local node ID to check foreign refs for"},
            },
            "required": ["node"],
        },
        "handler": _tool_foreign_refs,
    },
    "code2database_sync_foreign": {
        "description": "Trigger sync of foreign_refs with updated foreign C2Ds. Detects when external C2D (A) has changed (mtime/size/count diff) and re-resolves B's foreign_refs. Returns sync summary with newly_resolved/deleted/stale counts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "foreign_c2d": {"type": "string", "description": "Specific foreign C2D path to sync (default: all watched)"},
            },
            "required": [],
        },
        "handler": _tool_sync_foreign,
    },
    "code2database_composite_query": {
        "description": "Cross-C2D query via SQLite ATTACH. Finds callers/callees across local + foreign C2Ds. Supports 'CALLERS_OF name' and 'CALLEES_OF name' query language. Returns results tagged with source_db.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "CALLERS_OF <name> or CALLEES_OF <name> or free-text"},
                "foreign_c2ds": {"type": "string", "description": "Comma-separated foreign C2D paths to attach"},
                "top": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": ["query"],
        },
        "handler": _tool_composite_query,
    },
    "code2database_semantic_status": {
        "description": "Check if semantic update is recommended based on stale node accumulation.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_semantic_status,
    },
    "code2database_get_code_snippet": {
        "description": "Get source code snippet for a node. Returns lines around the function definition.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID or name"},
                "context": {"type": "integer", "description": "Lines of context (default 10)"},
            },
            "required": ["node"],
        },
        "handler": _tool_get_code_snippet,
    },
    "code2database_blast_radius": {
        "description": "Blast radius analysis: find all functions, APIs, and tests affected by a change to a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID or name of changed function"},
                "depth": {"type": "integer", "description": "Reverse BFS depth (default 3)"},
            },
            "required": ["node"],
        },
        "handler": _tool_blast_radius,
    },
    "code2database_extract_signals": {
        "description": "Extract #ifdef condition signals and their affected edges/functions. Shows how preprocessor conditions control call paths.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_extract_signals,
    },
    # ---- D37: New tools for expanded MCP coverage (16 -> 34) ----
    "code2database_path_feasible": {
        "description": "Check feasibility of a path under #ifdef conditions using Z3 or heuristics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
                "config": {"type": "object", "description": "Build config (e.g., {\"CONFIG_X\": true})"},
            },
            "required": ["node"],
        },
        "handler": _tool_path_feasible,
    },
    "code2database_find_invariants": {
        "description": "Extract preconditions, postconditions, loop invariants for a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
            },
            "required": ["node"],
        },
        "handler": _tool_find_invariants,
    },
    "code2database_ffi_trace": {
        "description": "Trace FFI boundaries from a function (ctypes, cgo, extern C).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
            },
            "required": ["node"],
        },
        "handler": _tool_ffi_trace,
    },
    "code2database_doc_code_check": {
        "description": "Check doc-code alignment (return values, params, signature mismatches).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Optional: limit check to one function"},
            },
            "required": [],
        },
        "handler": _tool_doc_code_check,
    },
    "code2database_daemon_status": {
        "description": "Check daemon status (running state, pending events, last sync).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_daemon_status,
    },
    "code2database_who_allocates": {
        "description": "Find functions that allocate a resource (kmalloc, malloc, new, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Optional: filter by resource name"},
            },
            "required": [],
        },
        "handler": _tool_who_allocates,
    },
    "code2database_who_frees": {
        "description": "Find functions that free a resource (kfree, free, delete, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Optional: filter by resource name"},
            },
            "required": [],
        },
        "handler": _tool_who_frees,
    },
    "code2database_who_locks": {
        "description": "Find functions that acquire a lock (mutex_lock, spin_lock, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lock": {"type": "string", "description": "Optional: filter by lock variable name"},
            },
            "required": [],
        },
        "handler": _tool_who_locks,
    },
    "code2database_explain_label": {
        "description": "Explain why a node has a given label (dead_code, API_entry, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
                "label": {"type": "string", "description": "Label to explain"},
            },
            "required": ["node", "label"],
        },
        "handler": _tool_explain_label,
    },
    "code2database_why_ambiguous": {
        "description": "Explain why an edge is marked AMBIGUOUS (fn_ptr, dead #ifdef, vtable).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "Caller function name or id"},
                "to": {"type": "string", "description": "Callee function name or id"},
            },
            "required": ["from", "to"],
        },
        "handler": _tool_why_ambiguous,
    },
    "code2database_audit_log": {
        "description": "Query the audit log (who edited what, when, why).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Filter by target node id"},
                "command": {"type": "string", "description": "Filter by command name"},
                "tx": {"type": "string", "description": "Filter by transaction id"},
                "limit": {"type": "integer", "description": "Max entries (default 100)"},
            },
            "required": [],
        },
        "handler": _tool_audit_log,
    },
    "code2database_happens_before": {
        "description": "Check happens-before between a writer and reader via locks, RCU, or memory barriers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "writer": {"type": "string", "description": "Writer function name or id"},
                "reader": {"type": "string", "description": "Reader function name or id"},
                "var": {"type": "string", "description": "Variable name"},
            },
            "required": ["writer", "reader"],
        },
        "handler": _tool_happens_before,
    },
    "code2database_memory_ordering": {
        "description": "Show memory-ordering primitives (RCU, barriers, atomics) used by a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or id"},
            },
            "required": ["node"],
        },
        "handler": _tool_memory_ordering,
    },
    "code2database_unbalanced_alloc_free": {
        "description": "Find functions that allocate without freeing (or vice versa).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_unbalanced_alloc_free,
    },
    # ---- cgdb: clang-based code graph database tools ----
    "cgdb_search_symbols": {
        "description": "Full-text search over cgdb_nodes via FTS5 (clang backend).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (name or fqn fragment)"},
                "kind": {"type": "string", "description": "Optional node kind filter (function/var/field/typedef/struct/union/enum/parm/decl_ref/call_expr/member_ref)"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": ["query"],
        },
        "handler": _tool_cgdb_search_symbols,
    },
    "cgdb_get_definition": {
        "description": "Find definition nodes by name (function/var/field/typedef). Returns id, fqn, file, line, type_spelling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to find"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["name"],
        },
        "handler": _tool_cgdb_get_definition,
    },
    "cgdb_get_function_body": {
        "description": "Return the function body source text for a function (name or id).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function name or node id"},
            },
            "required": ["node"],
        },
        "handler": _tool_cgdb_get_function_body,
    },
    "cgdb_get_source": {
        "description": "Get source text for a node with byte-precise attribution. Resolution: source_snippet column → file read via byte_start..byte_end.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Function/symbol name or node id"},
                "snippet_only": {"type": "boolean", "description": "Only return source_snippet (skip file read)"},
                "context_bytes": {"type": "integer", "description": "Include N bytes of surrounding context"},
            },
            "required": ["node"],
        },
        "handler": _tool_cgdb_get_source,
    },
    "cgdb_find_invokers": {
        "description": "Find callers of a node via recursive CTE with cycle protection. Set include_vtable_dispatch=true to also follow indirect dispatch via ops_bindings + invoke_sites (finds vtable callers even when no pre-computed INVOKES edge exists).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Source node id (cgdb_nodes.id)"},
                "depth": {"type": "integer", "description": "Recursive depth (default 1)"},
                "edge_types": {"type": "array", "items": {"type": "string"}, "description": "Edge kinds to traverse (default [\"INVOKES\"])"},
                "limit": {"type": "integer", "description": "Max results (default 200)"},
                "include_vtable_dispatch": {"type": "boolean", "description": "Also follow indirect dispatch via ops_bindings + invoke_sites (default false)"},
            },
            "required": ["node_id"],
        },
        "handler": _tool_cgdb_find_invokers,
    },
    "cgdb_find_invoked": {
        "description": "Find callees of a node via recursive CTE with cycle protection. Set include_vtable_dispatch=true to also resolve vtable dispatch via ops_bindings (finds impl functions invoked via function pointer calls).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Source node id"},
                "depth": {"type": "integer", "description": "Recursive depth (default 1)"},
                "edge_types": {"type": "array", "items": {"type": "string"}, "description": "Edge kinds (default [\"INVOKES\"])"},
                "limit": {"type": "integer", "description": "Max results (default 500)"},
                "include_vtable_dispatch": {"type": "boolean", "description": "Also resolve vtable dispatch via ops_bindings (default false)"},
            },
            "required": ["node_id"],
        },
        "handler": _tool_cgdb_find_invoked,
    },
    "cgdb_get_struct_layout": {
        "description": "Return a struct/union's field layout with types.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Struct name (or use 'struct' alias)"},
                "struct": {"type": "string", "description": "Alias for name"},
            },
            "required": [],
        },
        "handler": _tool_cgdb_get_struct_layout,
    },
    "cgdb_find_type_definition": {
        "description": "Find type definitions (struct/union/enum/typedef) by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Type name"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": ["name"],
        },
        "handler": _tool_cgdb_find_type_definition,
    },
    "cgdb_find_ops_impls": {
        "description": "Find functions bound to a vtable field (e.g., file_operations.read_iter). Uses ops_bindings table.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "field_name": {"type": "string", "description": "Ops field name (e.g., 'read_iter')"},
                "struct_type": {"type": "string", "description": "Optional: limit to a specific struct type (e.g., 'file_operations')"},
            },
            "required": ["field_name"],
        },
        "handler": _tool_cgdb_find_ops_impls,
    },
    "cgdb_find_cfg_paths": {
        "description": "Find CFG paths from entry to exit in a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "integer", "description": "Function node id"},
                "max_len": {"type": "integer", "description": "Max path length (default 10)"},
            },
            "required": ["function_id"],
        },
        "handler": _tool_cgdb_find_cfg_paths,
    },
    "cgdb_find_data_flow": {
        "description": "Find def-use chain entries for a variable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "var_id": {"type": "integer", "description": "Variable node id"},
            },
            "required": ["var_id"],
        },
        "handler": _tool_cgdb_find_data_flow,
    },
    "cgdb_find_aliases": {
        "description": "Find aliases of a pointer (may_alias / must_alias / no_alias).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ptr_id": {"type": "integer", "description": "Pointer node id"},
            },
            "required": ["ptr_id"],
        },
        "handler": _tool_cgdb_find_aliases,
    },
    "cgdb_find_lock_held_calls": {
        "description": "Find calls made while a lock is held in a function.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "integer", "description": "Function node id"},
            },
            "required": ["function_id"],
        },
        "handler": _tool_cgdb_find_lock_held_calls,
    },
    "cgdb_check_race_condition": {
        "description": "Heuristic race-condition check for a function (looks for unprotected var accesses).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_id": {"type": "integer", "description": "Function node id"},
            },
            "required": ["function_id"],
        },
        "handler": _tool_cgdb_check_race_condition,
    },
    "cgdb_find_configs_for": {
        "description": "Return the config predicate(s) attached to a node (text_form + config_macros).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Node id"},
            },
            "required": ["node_id"],
        },
        "handler": _tool_cgdb_find_configs_for,
    },
    "cgdb_find_nodes_under_config": {
        "description": "Find nodes whose config_predicate matches the given predicate text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "config": {"type": "string", "description": "Predicate text (e.g., 'CONFIG_X' or 'NOT CONFIG_X')"},
                "limit": {"type": "integer", "description": "Max results (default 500)"},
            },
            "required": ["config"],
        },
        "handler": _tool_cgdb_find_nodes_under_config,
    },
    "cgdb_index_status": {
        "description": "Return overall cgdb index statistics (node/edge/type/predicate counts, version count).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": _tool_cgdb_index_status,
    },
    "cgdb_time_travel_query": {
        "description": "Return the state of a node at a specific version_id (first_seen/last_seen soft-delete).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer", "description": "Node id"},
                "version_id": {"type": "integer", "description": "Target version_id"},
            },
            "required": ["node_id", "version_id"],
        },
        "handler": _tool_cgdb_time_travel_query,
    },
    "cgdb_list_versions": {
        "description": "List recent graph_versions rows (newest first).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": [],
        },
        "handler": _tool_cgdb_list_versions,
    },
}

# ============================================================================
# Merge in 28 design-report MCP tools (render_source / verify_consistency /
# edit_token / ... / commit_db_transaction / rollback_db_transaction /
# insert_node_after / delete_node / add_function).
# These implement design-report appendix B (28 tools: 8 L1 + 8 L2 + 7 L3 +
# 2 writeback + 3 advanced-edit). They are imported from mcp_report_tools
# so mcp_server.py stays under 2000 lines. Total tool count: 55 + 28 = 83.
# ============================================================================
try:
    from _builder.mcp.mcp_report_tools import TOOLS_REPORT
    TOOLS.update(TOOLS_REPORT)
except ImportError:
    # mcp_report_tools not available — log loudly so the user knows
    # the server is starting with 55 tools instead of the documented 83.
    logging.getLogger(__name__).error(
        "mcp_report_tools import failed — MCP server starting with "
        "%d tools (expected 83). Design-report tools unavailable.",
        len(TOOLS))


# Tools that modify state (graph DB, memory DB, tokens).  When the server
# is started with --read-only, these are hidden from tools/list and
# rejected on tools/call.  This protects production knowledge bases from
# accidental writes by remote clients.
WRITE_TOOLS = frozenset({
    "code2database_save_memory",
    "code2database_sync_foreign",
    "commit_db_transaction",
    "rollback_db_transaction",
    "insert_node_after",
    "delete_node",
    "add_function",
    "edit_token",
    "insert_token",
    "delete_token",
    # verify_consistency UPDATEs source_files_meta on success and INSERTs
    # into alignment_errors on mismatch, so it mutates the DB even though
    # its name sounds read-only. Hide it in --read-only mode.
    "verify_consistency",
})


# ---------------------------------------------------------------------------
# Shared JSON-RPC dispatch — used by both stdio and HTTP transports
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------


def _build_tools_list(read_only: bool = False) -> list:
    """Build the tools/list response array, optionally filtering write tools."""
    tools_list = []
    for name, tool_def in TOOLS.items():
        if read_only and name in WRITE_TOOLS:
            continue
        tools_list.append({
            "name": name,
            "description": tool_def["description"],
            "inputSchema": tool_def["inputSchema"],
        })
    return tools_list



def _handle_initialize(msg_id) -> dict:
    """Build the initialize response."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "Code2Database", "version": "2.1.0"},
    }}



def _handle_tools_call(msg_id, params, graph_dir, mcp_stats,
                       read_only: bool = False) -> dict:
    """Dispatch a tools/call request. Returns a complete JSON-RPC response.

    On success the result content is a JSON-serialised string (matching
    the stdio transport's behaviour).  On error the isError flag is set.
    Token usage is tracked in *mcp_stats* (mutated in place).
    """
    if not isinstance(params, dict):
        params = {}
    tool_name = params.get("name", "")
    tool_args = params.get("arguments", {})
    if not isinstance(tool_args, dict):
        tool_args = {}

    if tool_name not in TOOLS:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601,
                          "message": f"Unknown tool: {tool_name}"}}

    if read_only and tool_name in WRITE_TOOLS:
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text",
                     "text": json.dumps({"error":
                     f"Tool '{tool_name}' is disabled in read-only mode"})}],
                     "isError": True}}

    try:
        handler = TOOLS[tool_name]["handler"]
        # per-tool execution timeout. Without
        # this, a handler that loops indefinitely (e.g. _tool_impact
        # with depth=1000000 on a 1.5M-node graph before issue 27's
        # cap was added) blocks the dispatch thread — in stdio mode the
        # entire server hangs. signal.alarm only works in the main
        # thread (stdio mode); HTTP worker threads fall back to no
        # timeout (the --max-clients concurrency limit bounds resource
        # use there). Default 60s; tools can override via the
        # 'timeout' field in their TOOLS entry.
        tool_timeout = TOOLS[tool_name].get("timeout", 60)
        _alarm_installed = False
        try:
            import signal as _signal
            def _timeout_handler(signum, frame):
                raise TimeoutError(
                    f"tool '{tool_name}' exceeded {tool_timeout}s timeout")
            _old_handler = _signal.signal(_signal.SIGALRM, _timeout_handler)
            _signal.alarm(tool_timeout)
            _alarm_installed = True
        except (ValueError, OSError):
            # ValueError: not in main thread (HTTP worker) — skip.
            # OSError: signal not available (Windows) — skip.
            pass
        try:
            result = handler(tool_args, graph_dir)
        finally:
            if _alarm_installed:
                _signal.alarm(0)
                _signal.signal(_signal.SIGALRM, _old_handler)
        result_json = json.dumps(result, ensure_ascii=False, indent=2)
        tokens = estimate_tokens(result_json)
        mcp_stats["total_calls"] += 1
        mcp_stats["total_output_tokens"] += tokens
        mcp_stats["by_tool"].setdefault(tool_name, {"calls": 0, "tokens": 0})
        mcp_stats["by_tool"][tool_name]["calls"] += 1
        mcp_stats["by_tool"][tool_name]["tokens"] += tokens
        if isinstance(result, dict):
            result["_token_count"] = tokens
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text",
                    "text": json.dumps(result, ensure_ascii=False, indent=2)}]}}
    except Exception as e:
        # return the actual exception message
        # instead of opaque "internal error". The previous behavior made
        # debugging core tools nearly impossible — the 28 design-report
        # handlers all returned str(exc), but the 36 code2database_* +
        # 19 cgdb_* handlers cascaded through here and lost the message.
        # The exception message is safe to expose: it's generated by
        # Python/SQLite/library code, not user secrets. (Path-traversal
        # protection is handled separately in resolve_source_file.)
        logging.getLogger(__name__).warning(
            "tool '%s' raised: %s", tool_name, e, exc_info=True)
        err_payload = {"error": str(e) or type(e).__name__,
                       "tool": tool_name,
                       "exception_type": type(e).__name__}
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"content": [{"type": "text",
                    "text": json.dumps(err_payload, ensure_ascii=False)}],
                    "isError": True}}



def dispatch_mcp_request(method, msg_id, params, graph_dir, mcp_stats,
                         read_only: bool = False):
    """Handle a single JSON-RPC request.

    Returns:
        dict — a complete JSON-RPC response (to be sent as HTTP body or
               written to stdout).
        None  — the method is a notification (no response expected, e.g.
                ``notifications/initialized``).
    """
    try:
        if method == "initialize":
            return _handle_initialize(msg_id)

        if method == "notifications/initialized":
            return None

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": {"tools": _build_tools_list(read_only)}}

        if method == "tools/call":
            return _handle_tools_call(msg_id, params, graph_dir,
                                      mcp_stats, read_only)

        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        if msg_id is not None:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601,
                              "message": f"Method not found: {method}"}}
        return None
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "dispatch error for method '%s': %s", method, exc, exc_info=True)
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32603,
                          "message": "Internal error"}}


# ---------------------------------------------------------------------------
# MCP server main loop (stdio transport)
# ---------------------------------------------------------------------------


def run_mcp_server(graph_dir: str, read_only: bool = False):
    """Run MCP server over stdio transport."""
    # Propagate read-only mode to the tool handlers (kb-query tools skip
    # their access_count / query-log writes so a read-only server is
    # genuinely write-free, not just write-tool-filtered).
    from _builder.mcp.mcp_cache import set_mcp_read_only
    set_mcp_read_only(read_only)
    # Token tracking
    mcp_stats = {"total_calls": 0, "total_output_tokens": 0, "by_tool": {}}

    def _write_mcp_stats():
        stats_path = os.path.join(graph_dir, ".code2database_mcp_stats.json")
        try:
            Path(stats_path).write_text(
                json.dumps(mcp_stats, indent=2) + "\n", encoding="utf-8")
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    atexit.register(_write_mcp_stats)

    # Initialization
    initialized = False

    while True:
        msg = _read_message()
        if msg is _EOF_SENTINEL:
            break  # stdin closed — clean exit
        if msg is None:
            continue  # malformed message — skip

        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})
        if not isinstance(params, dict):
            params = {}

        try:
            response = dispatch_mcp_request(
                method, msg_id, params, graph_dir, mcp_stats, read_only)
        except Exception:
            logging.getLogger(__name__).warning(
                "uncaught error in dispatch_mcp_request", exc_info=True)
            response = {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32603,
                                  "message": "Internal error"}}
        if response is not None:
            _write_message(response)



def cmd_serve(args):
    """Handle serve command — start MCP server.

    Supports SQLite-only builds (code2database.db without
    code2database_master.json). Large projects (>100K functions) must use
    --storage sqlite, and without this fallback the MCP server cannot
    start for them — breaking LLM agent integration.

    Transport selection:
      - ``stdio`` (default): JSON-RPC over stdin/stdout — for local
        single-client usage (Claude Desktop, Cursor local).
      - ``http``: Streamable HTTP transport — for remote multi-client
        access over the network.  See ``--host``, ``--port``,
        ``--token``, ``--read-only`` flags.
    """
    graph_dir = args.graph
    has_master = os.path.exists(os.path.join(graph_dir, "code2database_master.json"))
    has_sqlite = os.path.exists(os.path.join(graph_dir, "code2database.db"))
    if not has_master and not has_sqlite:
        print(f"Error: No invocation graph found at {graph_dir} "
              f"(need code2database_master.json or code2database.db)", file=sys.stderr)
        sys.exit(1)

    transport = getattr(args, "transport", "stdio")
    read_only = getattr(args, "read_only", False)

    if transport == "http":
        from _builder.mcp.mcp_http_server import run_mcp_server_http
        host = getattr(args, "host", "0.0.0.0")
        port = getattr(args, "port", 8765)
        token = getattr(args, "token", None)
        if not token:
            token = os.environ.get("C2D_MCP_TOKEN")
        # Fail-fast: refuse to start on a public interface without auth
        # unless --allow-no-auth is explicitly passed
        allow_no_auth = getattr(args, "allow_no_auth", False)
        if not token and host not in ("127.0.0.1", "localhost", "::1") \
                and not allow_no_auth:
            print(
                "ERROR: Starting MCP HTTP server on a public interface "
                f"({host}) without --token is unsafe — anyone who can reach "
                f"this port can query your code graph AND write memories.\n"
                "  Fix: add --token <secret> (or set C2D_MCP_TOKEN env var).\n"
                "  Or: use --host 127.0.0.1 for localhost-only.\n"
                "  Or: pass --allow-no-auth to suppress this check "
                "(NOT recommended).", file=sys.stderr, flush=True)
            sys.exit(1)
        tls_cert = getattr(args, "tls_cert", None)
        tls_key = getattr(args, "tls_key", None)
        max_clients = getattr(args, "max_clients", 32)
        run_mcp_server_http(
            graph_dir=graph_dir, host=host, port=port, token=token,
            read_only=read_only, tls_cert=tls_cert, tls_key=tls_key,
            max_clients=max_clients)
    else:
        run_mcp_server(graph_dir, read_only=read_only)

