"""callgraph builder module: graph_loader — split from graph_build.py."""

import logging
import os
import json
import sys
import re
import time
from pathlib import Path
from collections import defaultdict, Counter
import networkx as nx
from _builder.graph.streaming_graph import StreamingGraph
from _builder.utils import _resolve_invoked_id
import _builder.utils as _utils


from _builder.graph.sqlite_postprocess import (
    _build_indexes_from_sqlite,
    _build_callgraph_summary_md_from_sqlite,
    _build_domain_readmes_from_sqlite,
    _build_scenarios_file_from_sqlite,
    _build_architecture_flows_from_sqlite,
    _build_context_pack_from_sqlite,
    _validate_stats_consistency_sqlite,
)

def _load_full_graph(graph_dir: str) -> nx.DiGraph:
    """Load the full invocation graph from domain-split JSON files.

    When code2database_master.json is absent (SQLite/deferred build mode,
    required for projects >100K functions), fall back to loading from
    code2database.db. Without this, ALL query commands (search, describe-node,
    explore-flow, neighbors, path, impact, serve/MCP, etc.) fail for large
    projects — the skill becomes "build-only" which violates the generality
    requirement.

    OPT-LG-1: When master.json exists but the graph is large (function_count
    >50K), prefer LazySQLiteGraph over eager full-load. Eager loading 100K+
    nodes from 150MB+ of domain JSON takes 25+ seconds for kernel-fs and
    would take 40+ minutes for a full 14GB kernel graph. LazySQLiteGraph
    queries SQLite on-demand, reducing load time to ~0s and per-query
    overhead to <100ms (predecessors/successors use indexed lookups).
    """
    master_path = os.path.join(graph_dir, "code2database_master.json")
    db_path = os.path.join(graph_dir, "code2database.db")

    # OPT-LG-1: For large graphs, prefer LazySQLiteGraph when SQLite db exists.
    # Threshold: 50K functions. Beyond this, eager load time exceeds 10s and
    # memory consumption exceeds 4GB, both unacceptable for interactive queries.
    _LARGE_GRAPH_THRESHOLD = 50000
    if os.path.exists(master_path) and os.path.exists(db_path):
        try:
            _master_head = json.loads(Path(master_path).read_text(encoding="utf-8"))
            _func_count = (
                _master_head.get("stats", {}).get("total_functions", 0)
                or _master_head.get("total_nodes", 0)
                or _master_head.get("total_functions", 0)
            )
            if _func_count >= _LARGE_GRAPH_THRESHOLD:
                print(f"[load] Large graph ({_func_count} nodes) — using "
                      f"LazySQLiteGraph for fast on-demand queries: {db_path}",
                      file=sys.stderr)
                try:
                    from scripts._builder.graph.streaming_graph import LazySQLiteGraph
                except ImportError:
                    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
                    from _builder.graph.streaming_graph import LazySQLiteGraph
                return LazySQLiteGraph(db_path)
        except Exception as _e:
            print(f"[load] Failed to peek master.json for size check: {_e}, "
                  f"falling back to eager load", file=sys.stderr)

    if not os.path.exists(master_path):
        # SQLite fallback for large-project query support.
        # Use LazySQLiteGraph (on-demand loading) instead of eager full-load
        # — eager load of 1.5M nodes times out interactive queries.
        if os.path.exists(db_path):
            print(f"[load] master not found, using LazySQLiteGraph: {db_path}",
                  file=sys.stderr)
            try:
                from scripts._builder.graph.streaming_graph import LazySQLiteGraph
            except ImportError:
                # Adjust for being called from within scripts/_builder/
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
                from _builder.graph.streaming_graph import LazySQLiteGraph
            return LazySQLiteGraph(db_path)
        print(f"Error: {master_path} not found and no code2database.db fallback",
              file=sys.stderr)
        raise FileNotFoundError(
            f"{master_path} not found and no code2database.db fallback"
        )

    master = json.loads(Path(master_path).read_text(encoding="utf-8"))
    G = nx.DiGraph()

    for domain, filename in master.get("domains", {}).items():
        domain_path = os.path.join(graph_dir, filename)
        if not os.path.exists(domain_path):
            continue
        domain_data = json.loads(Path(domain_path).read_text(encoding="utf-8"))

        if "nodes" in domain_data:
            # Legacy format: full node objects
            for node in domain_data.get("nodes", []):
                G.add_node(node["id"],
                           name=node.get("name", ""),
                           source_file=node.get("source_file", ""),
                           line=node.get("line", 0),
                           domain=node.get("domain", "root"),
                           labels=node.get("labels", []),
                           labels_source=node.get("labels_source", {}),
                           is_empty=node.get("is_empty", False),
                           condition=node.get("condition", ""),
                           api_constraints=node.get("api_constraints", ""),
                           external_desc=node.get("external_desc", ""),
                           semantic_desc=node.get("semantic_desc", ""),
                           body_text=node.get("body_text", ""),
                           signature=node.get("signature", ""),
                           params=node.get("params", []),
                           local_vars=node.get("local_vars", []),
                           callee_args=node.get("callee_args", []),
                           condition_vars=node.get("condition_vars", []),
                           preproc_alive=node.get("preproc_alive", True),
                           node_type=node.get("node_type", ""),
                           thread_model=node.get("thread_model"),
                           thread_entry=node.get("thread_entry", False),
                           thread_model_inherited=node.get("thread_model_inherited"),
                           globals_read=node.get("globals_read", []),
                           globals_written=node.get("globals_written", []),
                           fields_read=node.get("fields_read", []),
                           fields_written=node.get("fields_written", []),
                           language=node.get("language", ""),
                           reg_transfers=node.get("reg_transfers", []),
                           reg_state_final=node.get("reg_state_final", {}),
                           goto_jumps=node.get("goto_jumps", []),
                           goto_labels=node.get("goto_labels", []),
                           kind=node.get("kind", "function"),
                           fqn=node.get("fqn", node.get("id", "")),
                           file_path=node.get("file_path", node.get("source_file", "")),
                           byte_start=node.get("byte_start", 0),
                           byte_end=node.get("byte_end", 0),
                           commit_hash=node.get("commit_hash", ""),
                           description=node.get("description", ""))
        else:
            # Compact format: functions[] + function_details{} + empty_nodes[]
            ddomain = domain_data.get("domain", "root")
            for row in domain_data.get("functions", []):
                nid, name, source_file, line, labels_json, signature = row[0], row[1], row[2], row[3], row[4], row[5]
                labels = json.loads(labels_json) if labels_json else []
                details = domain_data.get("function_details", {}).get(nid, {})
                params = details.get("params", [])
                body_vars = details.get("local_vars", [])
                local_vars = [{"name": p["name"], "type": p.get("type", ""),
                               "value_snippet": "<param>", "line": 0, "is_param": True}
                              for p in params] + body_vars
                compact_args = details.get("callee_args", [])
                callee_args = []
                for ca in compact_args:
                    full_ca = {"call_order": ca.get("call_order"),
                               "callee": ca.get("callee", ""),
                               "args_snippet": ca.get("args_snippet", ""),
                               "args": ca.get("args", []),
                               "concurrency_info": ca.get("concurrency_info", {"is_spawn": False, "spawn_target": "", "spawn_arg": "", "concurrency_type": ""})}
                    if ca.get("callback_target"):
                        full_ca["callback_target"] = ca["callback_target"]
                    callee_args.append(full_ca)

                G.add_node(nid,
                           name=name,
                           source_file=source_file,
                           line=line,
                           domain=ddomain,
                           labels=labels,
                           labels_source=details.get("labels_source", {l: "ast" for l in labels}),
                           is_empty=False,
                           condition="",
                           api_constraints=details.get("api_constraints", ""),
                           external_desc=details.get("external_desc", ""),
                           semantic_desc=details.get("semantic_desc", ""),
                           body_text=details.get("body_text", ""),
                           signature=signature,
                           params=params,
                           local_vars=local_vars,
                           callee_args=callee_args,
                           condition_vars=details.get("condition_vars", []),
                           preproc_alive=details.get("preproc_alive", True),
                           node_type=details.get("node_type", ""),
                           thread_model=details.get("thread_model"),
                           thread_entry=details.get("thread_entry", False),
                           thread_model_inherited=details.get("thread_model_inherited"),
                           globals_read=details.get("globals_read", []),
                           globals_written=details.get("globals_written", []),
                           fields_read=details.get("fields_read", []),
                           fields_written=details.get("fields_written", []),
                           language=details.get("language", ""),
                           reg_transfers=details.get("reg_transfers", []),
                           reg_state_final=details.get("reg_state_final", {}),
                           goto_jumps=details.get("goto_jumps", []),
                           goto_labels=details.get("goto_labels", []),
                           kind=details.get("kind", "function"),
                           fqn=details.get("fqn", nid),
                           file_path=details.get("file_path", source_file),
                           byte_start=details.get("byte_start", 0),
                           byte_end=details.get("byte_end", 0),
                           commit_hash=details.get("commit_hash", ""),
                           description=details.get("description", ""))
                # Restore LLM supplement fields (from update-node command).
                # These are arbitrary keys with `_supplemented` suffix plus
                # the `_supplement_meta` provenance dict. Also fold the
                # supplemented value into the canonical attribute so
                # describe-node and other consumers see the supplemented
                # value rather than the empty original.
                for k, v in details.items():
                    if k.endswith("_supplemented") and v:
                        G.nodes[nid][k] = v
                        base_key = k[:-len("_supplemented")]
                        if not G.nodes[nid].get(base_key):
                            G.nodes[nid][base_key] = v
                if details.get("_supplement_meta"):
                    G.nodes[nid]["_supplement_meta"] = details["_supplement_meta"]

            for row in domain_data.get("empty_nodes", []):
                nid, cond, parent_id = row[0], row[1], row[2]
                G.add_node(nid,
                           name=f"<conditional:{cond}>",
                           source_file="",
                           line=0,
                           domain=ddomain,
                           labels=[],
                           labels_source={},
                           is_empty=True,
                           condition=cond)

        for edge in domain_data.get("edges", []):
            if isinstance(edge, list):
                # Compact format (v3): position-based array
                fields = domain_data.get("edge_fields",
                    ["source", "target", "call_order", "call_condition",
                     "concurrency", "confidence", "source_tag", "confidence_score"])
                ed = {fields[i]: v for i, v in enumerate(edge) if i < len(fields)}
                # Handle extras dict at end
                if len(edge) > len(fields) and isinstance(edge[-1], dict):
                    extras = edge[-1]
                    if "pc" in extras:
                        ed["preproc_condition"] = extras["pc"]
                    if "pa" in extras:
                        ed["preproc_alive"] = extras["pa"]
                    if "ev" in extras:
                        ed["evidence"] = extras["ev"]
                    if "rel" in extras:
                        ed["relation"] = extras["rel"]
                    if "ip" in extras:
                        ed["import_path"] = extras["ip"]
                # source = caller node ID, source_tag = provenance tag
                # NOTE: ed["source"] is the node ID — do NOT use it as
                # fallback for the provenance tag.  When source_tag is
                # absent, default to "ast".
                _source_tag = ed.get("source_tag") or "ast"
                _compact_edge_attrs = dict(
                           call_order=ed.get("call_order"),
                           call_condition=ed.get("call_condition", ""),
                           concurrency=ed.get("concurrency", ""),
                           confidence=ed.get("confidence", "EXTRACTED"),
                           source=_source_tag,
                           confidence_score=ed.get("confidence_score", 1.0),
                           preproc_condition=ed.get("preproc_condition", ""),
                           preproc_alive=ed.get("preproc_alive", True),
                           evidence=ed.get("evidence", ""))
                if ed.get("relation"):
                    _compact_edge_attrs["relation"] = ed["relation"]
                if ed.get("import_path"):
                    _compact_edge_attrs["import_path"] = ed["import_path"]
                G.add_edge(ed.get("source", ""), ed.get("target", ""),
                           **_compact_edge_attrs)
            else:
                # Legacy format (v1/v2): dict-based
                _legacy_source_tag = edge.get("source_tag") or "ast"
                _legacy_edge_attrs = dict(
                           call_order=edge.get("call_order"),
                           call_condition=edge.get("call_condition", ""),
                           concurrency=edge.get("concurrency", ""),
                           confidence=edge.get("confidence", "EXTRACTED"),
                           source=_legacy_source_tag,
                           confidence_score=edge.get("confidence_score", 1.0),
                           preproc_condition=edge.get("preproc_condition", ""),
                           preproc_alive=edge.get("preproc_alive", True),
                           evidence=edge.get("evidence", ""))
                if edge.get("relation"):
                    _legacy_edge_attrs["relation"] = edge["relation"]
                if edge.get("import_path"):
                    _legacy_edge_attrs["import_path"] = edge["import_path"]
                G.add_edge(edge["source"], edge["target"],
                           **_legacy_edge_attrs)

    # Add cross-domain edges
    for edge in master.get("cross_domain_edges", []):
        _xd_source_tag = edge.get("source_tag") or "ast"
        G.add_edge(edge["source"], edge["target"],
                   call_order=edge.get("call_order"),
                   call_condition=edge.get("call_condition", ""),
                   concurrency=edge.get("concurrency", ""),
                   confidence=edge.get("confidence", "EXTRACTED"),
                   source=_xd_source_tag,
                   confidence_score=edge.get("confidence_score", 1.0),
                   preproc_condition=edge.get("preproc_condition", ""),
                   preproc_alive=edge.get("preproc_alive", True),
                   evidence=edge.get("evidence", []),
                   relation=edge.get("relation", "INVOKES"))

    # Add structural edges (CONTAINS, IMPORTS) from master file
    # These are cross-domain structural edges that were not included in
    # per-domain edge lists. Without loading them, file nodes whose
    # CONTAINS targets ended up in a different domain become isolated.
    for edge in master.get("structural_edges", []):
        _se_source_tag = edge.get("source_tag") or "ast"
        _se_attrs = dict(
                   call_order=edge.get("call_order"),
                   call_condition=edge.get("call_condition", ""),
                   concurrency=edge.get("concurrency", ""),
                   confidence=edge.get("confidence", "EXTRACTED"),
                   source=_se_source_tag,
                   confidence_score=edge.get("confidence_score", 1.0),
                   preproc_condition=edge.get("preproc_condition", ""),
                   preproc_alive=edge.get("preproc_alive", True),
                   evidence=edge.get("evidence", []))
        if edge.get("relation"):
            _se_attrs["relation"] = edge["relation"]
        if edge.get("import_path"):
            _se_attrs["import_path"] = edge["import_path"]
        G.add_edge(edge["source"], edge["target"], **_se_attrs)

    return G



def _load_full_graph_from_sqlite(db_path: str) -> nx.DiGraph:
    """Load the full invocation graph from SQLite (code2database.db).

    Used when code2database_master.json is absent — i.e., builds that used
    --storage sqlite --low-memory (required for projects >100K functions
    where JSON master would be too large).

    Loads nodes from `functions` table and edges from `edges` table,
    deserializing JSON fields (labels, extra_json) on the fly. Decompresses
    body_text_compressed via zlib if needed.

    Memory note: For 1.5M nodes this consumes ~8-12 GB RAM. Callers that
    need lower memory should use targeted queries (describe-node, neighbors)
    that can load a single node via _load_node_from_sqlite instead of the
    full graph.
    """
    import sqlite3
    import zlib

    G = nx.DiGraph()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Load functions as nodes
        cur = conn.execute("SELECT * FROM functions")
        node_count = 0
        for row in cur:
            row_dict = dict(row)
            nid = row_dict.get("id")
            if not nid:
                continue
            labels_raw = row_dict.get("labels", "[]")
            try:
                labels = json.loads(labels_raw) if labels_raw else []
            except (json.JSONDecodeError, TypeError):
                labels = []
            # Parse extra_json for additional fields
            extra = {}
            extra_raw = row_dict.get("extra_json")
            if extra_raw:
                try:
                    extra = json.loads(extra_raw)
                except (json.JSONDecodeError, TypeError):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            # so describe-node and other consumers see the supplemented
            # value rather than the empty original.
            for supp_key, supp_val in list(extra.items()):
                if supp_key.endswith("_supplemented") and supp_val:
                    base_key = supp_key[:-len("_supplemented")]
                    if not extra.get(base_key):
                        extra[base_key] = supp_val
            # Decompress body_text if present
            body_text = ""
            body_blob = row_dict.get("body_text_compressed")
            if body_blob:
                try:
                    body_text = zlib.decompress(body_blob).decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""

            G.add_node(nid,
                       name=row_dict.get("name", ""),
                       source_file=row_dict.get("source_file", ""),
                       line=row_dict.get("line_number", 0) or 0,
                       domain=row_dict.get("domain", "root"),
                       labels=labels,
                       labels_source=extra.get("labels_source", {l: "ast" for l in labels}),
                       is_empty=extra.get("is_empty", False),
                       condition=extra.get("condition", ""),
                       api_constraints=extra.get("api_constraints", ""),
                       external_desc=extra.get("external_desc", ""),
                       semantic_desc=extra.get("semantic_desc", ""),
                       body_text=body_text,
                       signature=row_dict.get("signature", ""),
                       params=extra.get("params", []),
                       local_vars=extra.get("local_vars", []),
                       callee_args=extra.get("callee_args", []),
                       condition_vars=extra.get("condition_vars", []),
                       preproc_alive=extra.get("preproc_alive", True),
                       node_type=extra.get("node_type", ""),
                       thread_model=extra.get("thread_model"),
                       thread_entry=extra.get("thread_entry", False),
                       thread_model_inherited=extra.get("thread_model_inherited"),
                       globals_read=extra.get("globals_read", []),
                       globals_written=extra.get("globals_written", []),
                       fields_read=extra.get("fields_read", []),
                       fields_written=extra.get("fields_written", []),
                       language=extra.get("language", ""),
                       reg_transfers=extra.get("reg_transfers", []),
                       reg_state_final=extra.get("reg_state_final", {}),
                       goto_jumps=extra.get("goto_jumps", []),
                       goto_labels=extra.get("goto_labels", []),
                       stale=extra.get("stale", False),
                       # invariants
                       preconditions=extra.get("preconditions", []),
                       postconditions=extra.get("postconditions", []),
                       loop_invariants=extra.get("loop_invariants", []),
                       state_machine=extra.get("state_machine"),
                       _invariant_meta=extra.get("_invariant_meta"))
            node_count += 1
        print(f"[load] Loaded {node_count} nodes from SQLite", file=sys.stderr)

        # Load edges
        edge_count = 0
        try:
            cur = conn.execute("SELECT * FROM edges")
            for row in cur:
                row_dict = dict(row)
                caller = row_dict.get("invoker_id")
                callee = row_dict.get("invoked_id")
                if not caller or not callee:
                    continue
                # Parse callee_arg_json and reg_args_json if present
                callee_args = None
                ca_raw = row_dict.get("callee_arg_json")
                if ca_raw:
                    try:
                        callee_args = json.loads(ca_raw)
                    except (json.JSONDecodeError, TypeError):
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                reg_args = None
                ra_raw = row_dict.get("reg_args_json")
                if ra_raw:
                    try:
                        reg_args = json.loads(ra_raw)
                    except (json.JSONDecodeError, TypeError):
                        logging.getLogger(__name__).debug("silent exception", exc_info=True)
                        pass
                evidence = []
                ev_raw = row_dict.get("evidence")
                if ev_raw:
                    try:
                        evidence = json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                    except (json.JSONDecodeError, TypeError):
                        evidence = []

                attrs = dict(
                    call_order=row_dict.get("call_order"),
                    call_condition=row_dict.get("call_condition", "") or "",
                    concurrency=row_dict.get("concurrency", "") or "",
                    confidence=row_dict.get("confidence", "EXTRACTED") or "EXTRACTED",
                    confidence_score=row_dict.get("confidence_score", 1.0) or 1.0,
                    source=row_dict.get("source", "ast") or "ast",
                    evidence=evidence,
                    relation=row_dict.get("relation", "INVOKES") or "INVOKES")
                if callee_args is not None:
                    attrs["callee_args"] = callee_args
                if reg_args is not None:
                    attrs["reg_args"] = reg_args
                G.add_edge(caller, callee, **attrs)
                edge_count += 1
        except sqlite3.OperationalError as _e:
            print(f"[load] No edges table in SQLite: {_e}", file=sys.stderr)
        print(f"[load] Loaded {edge_count} edges from SQLite", file=sys.stderr)
    finally:
        conn.close()
    return G



def _load_node_from_sqlite(db_path: str, node_id: str) -> dict:
    """Load a single node from SQLite (memory-efficient for targeted queries).

    Returns a dict of node attributes, or empty dict if not found.
    Used by query commands that only need one node (describe-node, neighbors)
    to avoid loading the full 1.5M-node graph.
    """
    import sqlite3
    import zlib

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM functions WHERE id=?", (node_id,))
        row = cur.fetchone()
        if not row:
            return {}
        row_dict = dict(row)
        labels_raw = row_dict.get("labels", "[]")
        try:
            labels = json.loads(labels_raw) if labels_raw else []
        except (json.JSONDecodeError, TypeError):
            labels = []
        extra = {}
        extra_raw = row_dict.get("extra_json")
        if extra_raw:
            try:
                extra = json.loads(extra_raw)
            except (json.JSONDecodeError, TypeError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        body_text = ""
        body_blob = row_dict.get("body_text_compressed")
        if body_blob:
            try:
                body_text = zlib.decompress(body_blob).decode("utf-8", errors="replace")
            except Exception:
                body_text = ""
        return dict(
            name=row_dict.get("name", ""),
            source_file=row_dict.get("source_file", ""),
            line=row_dict.get("line_number", 0) or 0,
            domain=row_dict.get("domain", "root"),
            labels=labels,
            labels_source=extra.get("labels_source", {l: "ast" for l in labels}),
            is_empty=extra.get("is_empty", False),
            condition=extra.get("condition", ""),
            api_constraints=extra.get("api_constraints", ""),
            external_desc=extra.get("external_desc", ""),
            semantic_desc=extra.get("semantic_desc", ""),
            body_text=body_text,
            signature=row_dict.get("signature", ""),
            params=extra.get("params", []),
            local_vars=extra.get("local_vars", []),
            callee_args=extra.get("callee_args", []),
            condition_vars=extra.get("condition_vars", []),
            preproc_alive=extra.get("preproc_alive", True),
            node_type=extra.get("node_type", ""),
            thread_model=extra.get("thread_model"),
            thread_entry=extra.get("thread_entry", False),
            thread_model_inherited=extra.get("thread_model_inherited"),
            globals_read=extra.get("globals_read", []),
            globals_written=extra.get("globals_written", []),
            fields_read=extra.get("fields_read", []),
            fields_written=extra.get("fields_written", []),
            language=extra.get("language", ""),
            reg_transfers=extra.get("reg_transfers", []),
            reg_state_final=extra.get("reg_state_final", {}),
            goto_jumps=extra.get("goto_jumps", []),
            goto_labels=extra.get("goto_labels", []),
            stale=extra.get("stale", False),
        )
    finally:
        conn.close()



def _load_neighbors_from_sqlite(db_path: str, node_id: str):
    """Load a node's predecessors and successors from SQLite.

    Returns (predecessors, successors) where each is a list of
    (neighbor_id, edge_attrs) tuples. Used by neighbors/describe-node
    commands for memory-efficient single-node queries.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    predecessors = []
    successors = []
    try:
        # Successors (out-edges)
        cur = conn.execute("SELECT * FROM edges WHERE invoker_id=?", (node_id,))
        for row in cur:
            row_dict = dict(row)
            callee = row_dict.get("invoked_id")
            if not callee:
                continue
            evidence = []
            ev_raw = row_dict.get("evidence")
            if ev_raw:
                try:
                    evidence = json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                except (json.JSONDecodeError, TypeError):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            attrs = dict(
                call_order=row_dict.get("call_order"),
                call_condition=row_dict.get("call_condition", "") or "",
                concurrency=row_dict.get("concurrency", "") or "",
                confidence=row_dict.get("confidence", "EXTRACTED") or "EXTRACTED",
                confidence_score=row_dict.get("confidence_score", 1.0) or 1.0,
                source=row_dict.get("source", "ast") or "ast",
                evidence=evidence,
                relation=row_dict.get("relation", "INVOKES") or "INVOKES")
            successors.append((callee, attrs))

        # Predecessors (in-edges)
        cur = conn.execute("SELECT * FROM edges WHERE invoked_id=?", (node_id,))
        for row in cur:
            row_dict = dict(row)
            caller = row_dict.get("invoker_id")
            if not caller:
                continue
            evidence = []
            ev_raw = row_dict.get("evidence")
            if ev_raw:
                try:
                    evidence = json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                except (json.JSONDecodeError, TypeError):
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            attrs = dict(
                call_order=row_dict.get("call_order"),
                call_condition=row_dict.get("call_condition", "") or "",
                concurrency=row_dict.get("concurrency", "") or "",
                confidence=row_dict.get("confidence", "EXTRACTED") or "EXTRACTED",
                confidence_score=row_dict.get("confidence_score", 1.0) or 1.0,
                source=row_dict.get("source", "ast") or "ast",
                evidence=evidence,
                relation=row_dict.get("relation", "INVOKES") or "INVOKES")
            predecessors.append((caller, attrs))
    finally:
        conn.close()
    return predecessors, successors



def _disambiguate_struct_chain(struct_chain: str, candidate_structs: list,
                                var_to_struct: dict,
                                caller_domain: str = "",
                                embedding_index: dict = None) -> list:
    """Use struct_chain hint to narrow which struct types match a fn_ptr_call.

    Without disambiguation, a call like dev->ops->get_io_channel() would
    match ALL struct types that have a get_io_channel field (dev_fn_table,
    accel_module_if, raid_module, etc.), creating false INFERRED edges.

    Strategies (in order):
    1. Exact var_name match: struct_chain matches a known var_name
    2. Suffix match: last part of chain (after ->) matches struct_type suffix
    3. Keyword match: parts of chain appear in struct_type name
    4. Domain match: caller's domain appears in struct_type name
    5. Fallback: return all candidates (current behavior)

    Returns: list of struct_type strings that match.
    """
    if len(candidate_structs) <= 1:
        return candidate_structs

    chain_lower = struct_chain.lower()
    chain_parts = [p.strip() for p in chain_lower.split('->')]
    last_part = chain_parts[-1] if chain_parts else chain_lower

    # Strategy 1: exact var_name match
    # struct_chain might be a global variable name like g_accel_driver
    if chain_lower in var_to_struct:
        matched = var_to_struct[chain_lower]
        if matched in candidate_structs:
            return [matched]
    if last_part in var_to_struct:
        matched = var_to_struct[last_part]
        if matched in candidate_structs:
            return [matched]

    # Strategy 2: suffix match
    # e.g., 'fn_table' matches 'dev_fn_table'
    matched_by_suffix = []
    for st in candidate_structs:
        st_lower = st.lower()
        if st_lower.endswith('_' + last_part) or st_lower == last_part:
            matched_by_suffix.append(st)
    if len(matched_by_suffix) == 1:
        return matched_by_suffix

    # Strategy 3: keyword match
    # e.g., 'bdev->fn_table' matches 'dev_fn_table' because 'bdev' is in the name
    # Also split chain parts by underscore: 'accel_module' → ['accel', 'module']
    chain_keywords = set()
    for part in chain_parts:
        for sub in re.split(r'[_]', part):
            if len(sub) >= 3:
                chain_keywords.add(sub)
    matched_by_keyword = []
    for st in candidate_structs:
        st_lower = st.lower()
        st_parts = set(re.split(r'[_]', st_lower))
        if chain_keywords & st_parts:
            matched_by_keyword.append(st)
    if len(matched_by_keyword) == 1:
        return matched_by_keyword
    if matched_by_keyword:
        # If keyword matched multiple, try domain-based narrowing
        if caller_domain:
            domain_parts = set(re.split(r'[._]', caller_domain.lower()))
            domain_subset = []
            for st in matched_by_keyword:
                st_lower = st.lower()
                st_parts = set(re.split(r'[_]', st_lower))
                for dp in domain_parts:
                    if len(dp) >= 3 and dp in st_parts:
                        domain_subset.append(st)
                        break
            if len(domain_subset) == 1:
                return domain_subset
            if domain_subset and len(domain_subset) < len(matched_by_keyword):
                return domain_subset
        return matched_by_keyword

    # Strategy 4: domain-based disambiguation
    # When struct_chain is generic (e.g., 'module', 'ops', 'dev'), use the
    # caller's domain to narrow candidates. E.g., caller in 'bdev' domain
    # with struct_chain='module' should prefer 'dev_module' over
    # 'accel_module_if'.
    if caller_domain and len(chain_parts) == 1 and len(last_part) <= 6:
        domain_parts = set(re.split(r'[._]', caller_domain.lower()))
        domain_matched = []
        for st in candidate_structs:
            st_lower = st.lower()
            st_parts = set(re.split(r'[_]', st_lower))
            # Check if any domain part appears in struct_type name
            for dp in domain_parts:
                if len(dp) >= 3 and dp in st_parts:
                    domain_matched.append(st)
                    break
        if len(domain_matched) == 1:
            return domain_matched
        if domain_matched and len(domain_matched) < len(candidate_structs):
            return domain_matched

    # Strategy 4.5: Embedding-based disambiguation.
    # If the caller's domain contains a struct that embeds a candidate struct's
    # type, prefer that candidate. E.g., caller in 'nvme_pcie' domain, candidate
    # is a fn_table struct which is embedded by 'nvme_pcie_ctrlr' →
    # the embedding's domain_hint matches the caller's domain.
    if embedding_index and caller_domain:
        domain_parts = set(re.split(r'[._]', caller_domain.lower()))
        embedding_matched = []
        for st in candidate_structs:
            # Check if this struct_type is an inner_type in the embedding index
            embeddings = embedding_index.get(st, [])
            for emb in embeddings:
                hint = emb.get("domain_hint", "")
                if hint:
                    hint_parts = set(re.split(r'[._]', hint.lower()))
                    if domain_parts & hint_parts:
                        embedding_matched.append(st)
                        break
                # Also check if outer_type's name contains domain parts
                outer = emb.get("outer_type", "")
                if outer:
                    outer_parts = set(re.split(r'[_]', outer.lower()))
                    if domain_parts & outer_parts:
                        embedding_matched.append(st)
                        break
        if len(embedding_matched) == 1:
            return embedding_matched
        if embedding_matched and len(embedding_matched) < len(candidate_structs):
            return embedding_matched

    # If suffix matched multiple, return those
    if matched_by_suffix:
        return matched_by_suffix

    # Strategy 5: strip trailing digits from last_part and retry suffix/keyword
    # C code often uses numeric suffixes for disambiguation (e.g., bs_dev2, req3).
    # The numeric suffix prevents suffix/keyword match, causing fallback to
    # return ALL candidates (including unrelated structs). Stripping the digits
    # allows 'bs_dev2' to match 'bs_dev' via suffix '_bs_dev'.
    base_part = re.sub(r'\d+$', '', last_part)
    if base_part and base_part != last_part and len(base_part) >= 3:
        # Retry suffix match with base name
        base_suffix_matches = [st for st in candidate_structs
                               if st.lower().endswith('_' + base_part)
                               or st.lower() == base_part]
        if len(base_suffix_matches) == 1:
            return base_suffix_matches
        if base_suffix_matches:
            # Retry keyword match with base name parts
            base_keywords = set(re.split(r'[_]', base_part))
            base_keywords = {k for k in base_keywords if len(k) >= 3}
            if base_keywords:
                base_keyword_matches = [st for st in base_suffix_matches
                                        if base_keywords & set(re.split(r'[_]', st.lower()))]
                if len(base_keyword_matches) == 1:
                    return base_keyword_matches
                if base_keyword_matches:
                    # Domain narrowing on base matches
                    if caller_domain:
                        domain_parts = set(re.split(r'[._]', caller_domain.lower()))
                        domain_subset = [st for st in base_keyword_matches
                                         if any(len(dp) >= 3 and dp in set(re.split(r'[_]', st.lower()))
                                                for dp in domain_parts)]
                        if len(domain_subset) == 1:
                            return domain_subset
                        if domain_subset:
                            return domain_subset
                    return base_keyword_matches
            return base_suffix_matches

    # Fallback: return all candidates (preserves current behavior)
    return candidate_structs


