"""Build pipeline phase helpers — extracted from build_graph() for testability.

Each phase is a pure-ish function: it takes explicit inputs (the graph,
extraction data, profile-derived config) and mutates the graph in place
or returns a derived data structure. Phases are called in order by
build_graph() in graph_build.py; this module holds them so they can be
unit-tested in isolation without driving a full build.

Phase order (matches build_graph call sites):

1. _derive_project_name(extraction, functions) -> str
2. _build_id_registry(functions, G, project_name) -> dict
3. _filter_noise_nodes(G, id_registry, keep_prefixes) -> None (mutates G + id_registry)
4. _enable_streaming_deferred(G) -> bool
5. _build_edge_target_index(raw_edges) -> dict
6. _add_empty_nodes(G, raw_edges, empty_node_attrs) -> None
7. _build_vtable_field_index(extraction, struct_defs) -> (set, dict)
8. _build_name_domain_index(G) -> (dict, dict)
9. _process_asm_aliases(extraction, G) -> None
10. _label_export_symbol_functions(G, profile) -> None
11. _label_struct_op_types(G, profile) -> None
12. _emit_ops_bind_edges(G, extraction) -> int
13. _build_field_dispatch_map(extraction) -> (dict, dict, dict)
14. _build_struct_embedding_index(extraction, profile) -> (dict, dict)
15. _identify_polymorphic_callbacks(G, ...) -> set

... (additional phases documented inline as they are extracted)

Convention: each phase function takes the graph G (or extraction data)
as its first argument and either mutates G in place or returns a small
derived structure. Phases do NOT call each other; build_graph()
orchestrates the sequence. This keeps each phase independently testable.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Set, Tuple
import logging

# Constant: parameter-only names that are almost certainly callback
# parameter names extracted by the scanner as fake functions.
_PARAM_ONLY_NAMES = frozenset({
    'cb', 'fn', 'func', 'handler', 'callback', 'op', 'action', 'proc',
    'routine', 'init', 'fini', 'usage', 'done', 'cpl', 'arg', 'ctx',
    'data', 'result', 'rc', 'err', 'len', 'size', 'count', 'buf',
})

# Constant: C++ source file extensions whose names should be checked
# against struct-member / operator-overload patterns.
_CPP_SOURCE_EXTS = frozenset({'.cpp', '.cc', '.cxx', '.hpp'})

# Callback field regex — used in both FN_PTR field_dispatch and
# passthrough_bridged field_assignments fallback paths to skip
# callback fields (cb_fn, cb_func, etc.) that are handled by
# caller_bridged and param_bridged resolution.
_CB_FIELD_RE = re.compile(
    r'^(cb_fn|cb_func|cb|callback|completion_cb|done_cb|cpl_cb)$'
    r'|(?:_cb|_fn|_func|_cb_fn|_cb_func|cb_fn|cb_func)$')


# ---------------------------------------------------------------------------
# Phase 1: derive project name
# ---------------------------------------------------------------------------

def _derive_project_name(extraction: dict, functions: list) -> str:
    """Derive the project name from extraction metadata or first source file.

    The project name is used as the FQN prefix (e.g., ``linux_kernel.foo``).
    Falls back to 'project' when no source file is available.

    Args:
        extraction: Extraction data dict from the scanner.
        functions: List of function dicts (extraction['functions']).

    Returns:
        Project name string. Empty string only when extraction has no
        'project' key and no function exposes a source_file.
    """
    project_name = extraction.get("project", "")
    if not project_name and functions:
        first_file = functions[0].get("source_file", "")
        if first_file:
            project_name = os.path.basename(os.path.dirname(first_file)) or "project"
    return project_name


# ---------------------------------------------------------------------------
# Phase 2: build ID registry + add function nodes to graph
# ---------------------------------------------------------------------------

def _build_id_registry(functions: list, G, project_name: str) -> dict:
    """Build id_registry and add each function as a graph node.

    The id_registry maps function id -> raw function dict, used downstream
    by suffix_index / multi-strategy resolve / id-based lookups. The same
    loop adds each function as a graph node with all relevant attributes
    copied verbatim from the extraction record (so attribute drift between
    extraction JSON and graph nodes is impossible).

    Args:
        functions: List of function dicts (extraction['functions']).
        G: NetworkX DiGraph (or StreamingGraph). Mutated in place.
        project_name: Project name for FQN computation.

    Returns:
        id_registry: dict mapping function id -> raw function dict.
    """
    from _builder.import_resolve import _compute_fqn
    id_registry: Dict[str, dict] = {}
    for func in functions:
        fid = func["id"]
        id_registry[fid] = func
        fqn = _compute_fqn(func, project_name)
        G.add_node(
            fid,
            name=func.get("name", ""),
            fqn=fqn,
            source_file=func.get("source_file", ""),
            line=func.get("line", 0),
            domain=func.get("domain", "root"),
            labels=func.get("labels", []),
            labels_source=func.get("labels_source", {}),
            is_empty=func.get("is_empty", False),
            condition=func.get("condition", ""),
            api_constraints=func.get("api_constraints", ""),
            external_desc=func.get("external_desc", ""),
            semantic_desc=func.get("semantic_desc", ""),
            body_text=func.get("body_text", ""),
            signature=func.get("signature", ""),
            params=func.get("params", []),
            local_vars=func.get("local_vars", []),
            callee_args=func.get("callee_args", []),
            condition_vars=func.get("condition_vars", []),
            preproc_alive=func.get("preproc_alive", True),
            language=func.get("language", ""),
            reg_transfers=func.get("reg_transfers", []),
            reg_state_final=func.get("reg_state_final", {}),
            goto_jumps=func.get("goto_jumps", []),
            goto_labels=func.get("goto_labels", []),
            globals_read=func.get("globals_read", []),
            globals_written=func.get("globals_written", []),
            fields_read=func.get("fields_read", []),
            fields_written=func.get("fields_written", []),
            thread_model=func.get("thread_model"),
            thread_entry=func.get("thread_entry", False),
            thread_model_inherited=func.get("thread_model_inherited"),
            node_type=func.get("node_type", ""),
        )
    return id_registry


# ---------------------------------------------------------------------------
# Phase 3: filter noise nodes (param-name-only + C++ artifacts)
# ---------------------------------------------------------------------------

def _filter_noise_nodes(G, id_registry: dict, keep_prefixes: tuple) -> None:
    """Remove nodes that are clearly function-pointer parameter names or
    C++ struct member variables / operator overloads, not real functions.

    Mutates G (removes nodes) and id_registry (pops entries) in place.

    Args:
        G: NetworkX DiGraph (or StreamingGraph).
        id_registry: dict mapping function id -> raw function dict. Mutated.
        keep_prefixes: tuple of project-API prefixes (e.g., ``('proj_', 'rpc_')``).
            Functions starting with these prefixes are kept even when they
            look like C++ noise (no body, underscore-prefixed), because they
            are project API surface.
    """
    from _builder.utils import _build_suffix_index
    nodes_to_remove: List[str] = []
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        name = ndata.get("name", "")
        # Remove parameter-name-only nodes (no body, no source file)
        if (name in _PARAM_ONLY_NAMES
                and not ndata.get("body_text", "")
                and not ndata.get("source_file", "")):
            nodes_to_remove.append(nid)
            continue
        # Remove C++ struct member variables / operator overloads.
        # Only when: empty body_text AND C++ source file AND name is:
        # - starts with _ (private member convention), OR
        # - is exactly "operator" or "operator<something>" (C++ overload)
        src_file = ndata.get("source_file", "")
        src_ext = ""
        if "." in src_file:
            src_ext = "." + src_file.rsplit(".", 1)[-1]
        if (not ndata.get("body_text", "")
                and src_ext in _CPP_SOURCE_EXTS
                and (name.startswith("_") or name.startswith("operator"))
                and not any(name.startswith(p) for p in keep_prefixes)):
            nodes_to_remove.append(nid)

    if not nodes_to_remove:
        return

    # Capture names BEFORE popping registry entries — the previous diag
    # loop read id_registry AFTER the pop, so name was always the nid and
    # every removed node (param-only ones included) was misreported as a
    # "C++ artifact".
    _removed_names = {nid: id_registry.get(nid, {}).get("name", nid)
                      if isinstance(id_registry.get(nid), dict) else nid
                      for nid in nodes_to_remove}
    for nid in nodes_to_remove:
        G.remove_node(nid)
        id_registry.pop(nid, None)
    # Rebuild suffix index after removal — caller does this by re-calling
    # _build_suffix_index(id_registry). We only delete nodes here so the
    # caller can decide whether to rebuild (depending on phase ordering).
    print(f"Removed {len(nodes_to_remove)} noise nodes (param-name-only + C++ artifacts)",
          file=sys.stderr)
    # Diag: list removed C++ artifacts for verification
    for nid in nodes_to_remove:
        name = _removed_names[nid]
        if name not in _PARAM_ONLY_NAMES:
            print(f"  C++ artifact removed: {nid}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Phase 4: enable StreamingGraph deferred mode
# ---------------------------------------------------------------------------

def _enable_streaming_deferred(G) -> bool:
    """Enable deferred edge-attribute mode when G is a StreamingGraph.

    Deferred mode keeps only lightweight edge keys in _edge_set during
    edge processing (saves ~590MB for 5.4M edges). Edge attributes are
    stored in _edge_batch and written directly at close() time.

    Returns:
        True if G is a StreamingGraph and deferred mode was enabled,
        False otherwise. Callers use this to decide whether to call
        G.set_deferred(False) at the appropriate phase boundary.
    """
    from _builder.streaming_graph import StreamingGraph
    if isinstance(G, StreamingGraph):
        G.set_deferred(True)
        return True
    return False


# ---------------------------------------------------------------------------
# Phase 5: create empty conditional nodes + build edge target index
# ---------------------------------------------------------------------------

def _create_empty_conditional_nodes(G, raw_edges: list, id_registry: dict) -> None:
    """Create empty placeholder nodes for conditional sub-expressions.

    Conditional sub-nodes (e.g., ``func__cond_0``, ``func__cond_0_else``)
    are referenced by edges as their source but are never in the
    extraction['functions'] list — they're synthetic placeholders for
    conditional dispatch (if/while/for/switch with &&/|| compound
    conditions). This phase discovers them via is_cond_child edges and
    creates placeholder nodes in G with derived domain + condition text.

    Args:
        G: NetworkX DiGraph. Mutated in place — empty nodes added.
        raw_edges: List of edge dicts (extraction['edges']).
        id_registry: dict mapping function id -> raw function dict. Read
            to derive parent_domain from the parent caller id. Mutated to
            add a synthesized record for each new empty node.
    """
    empty_nodes_needed: Set[str] = set()
    # First-edge-wins: matches the original build_graph semantics where
    # the first edge targeting a given target provides the call_condition.
    # Used internally for O(1) condition text lookup.
    edge_target_index: Dict[str, dict] = {}
    for edge in raw_edges:
        tgt = edge.get("target", "")
        if tgt and tgt not in edge_target_index:
            edge_target_index[tgt] = edge
        if edge.get("is_cond_child") and edge.get("source", "") not in G:
            empty_nodes_needed.add(edge["source"])

    for enid in empty_nodes_needed:
        # Derive domain from the parent caller (extract from empty node id pattern)
        # Pattern: invoker_id__cond_N
        parts = enid.rsplit("__cond_", 1)
        parent_id = parts[0] if len(parts) > 1 else ""
        parent_domain = "root"
        if parent_id in id_registry:
            parent_domain = id_registry[parent_id].get("domain", "root")

        cond_text = ""
        # O(1) lookup via pre-built index instead of O(n) scan of raw_edges
        cond_edge = edge_target_index.get(enid)
        if cond_edge and cond_edge.get("call_condition"):
            cond_text = cond_edge["call_condition"]

        G.add_node(
            enid,
            name=f"<conditional:{cond_text}>",
            source_file="",
            line=0,
            domain=parent_domain,
            labels=[],
            is_empty=True,
            condition=cond_text,
        )
        id_registry[enid] = {
            "id": enid,
            "domain": parent_domain,
            "name": f"<cond:{cond_text}>",
        }


__all__ = [
    "_derive_project_name",
    "_build_id_registry",
    "_filter_noise_nodes",
    "_enable_streaming_deferred",
    "_create_empty_conditional_nodes",
    "_build_vtable_field_index",
    "_build_name_domain_index",
    "_process_asm_aliases",
    "_label_export_symbol_functions",
    "_label_struct_op_types",
    "_emit_ops_bind_edges",
    "_build_field_dispatch_map",
    "_build_fn_ptr_struct_lookup",
    "_build_struct_embedding_index",
    "_identify_polymorphic_callback_fields",
]


# ---------------------------------------------------------------------------
# Phase 6: build vtable field index
# ---------------------------------------------------------------------------

def _build_vtable_field_index(extraction: dict, profile) -> tuple:
    """Build the set of vtable field names + struct_type→{field→[func_name]} map.

    FN_PTR edges whose targets match vtable field names are handled by
    vtable dispatch (INFERRED edges) and should not be resolved
    individually. The struct_type→field→func_name map is used downstream
    for chain-tail vtable matching (when a fn_ptr_call doesn't carry
    struct context, fall back to the struct_type's registrations).

    Skips vtable_registrations from test source files (they pollute the
    field-name set with non-vtable fields — e.g., test code setting
    req.cb_fn = test_cb would cause all cb_fn fn_ptr_calls to resolve
    as vtable_dispatch instead of field_dispatch).

    Args:
        extraction: Extraction data dict from the scanner.
        profile: Builder config dict (used for _is_test_source check).

    Returns:
        Tuple ``(vtable_field_names, vtable_type_fields)`` where:
        - vtable_field_names: set of field-name strings
        - vtable_type_fields: dict[struct_type, dict[field_name, list[func_name]]]
    """
    # Lazy import: _is_test_source lives in graph_build to avoid an
    # import cycle (graph_build imports build_phases, build_phases must
    # not import graph_build at module load).
    from _builder.graph_build import _is_test_source
    from collections import defaultdict

    vtable_field_names: Set[str] = set()
    vtable_type_fields = defaultdict(lambda: defaultdict(list))
    for vtable in extraction.get("vtable_registrations", []):
        # Skip malformed vtable entries (e.g., Rust IMPLEMENTS without struct_type)
        if "struct_type" not in vtable:
            continue
        # Skip vtable_registrations from test source files (see docstring)
        vtable_src = vtable.get("source_file", "")
        if _is_test_source(vtable_src, profile):
            continue
        stype = vtable.get("struct_type", "")
        for reg in vtable.get("registrations", []):
            vtable_field_names.add(reg["field"])
            if stype:
                vtable_type_fields[stype][reg["field"]].append(
                    reg.get("func_name", ""))
    return vtable_field_names, vtable_type_fields


# ---------------------------------------------------------------------------
# Phase 7: build name→domain and name→nid indexes
# ---------------------------------------------------------------------------

def _build_name_domain_index(G, extraction: dict) -> tuple:
    """Build name→domain and name→nid lookup indexes from extraction data.

    Used by:
    - caller domain lookup in field_dispatch map building
    - API_entry labeling for EXPORT_SYMBOL and struct_op_types
    - ASM alias resolution

    The name→nid index prefers non-extern (complete definition) over
    extern declaration when the same name appears in multiple sources
    (e.g., C + ASM versions of the same function).

    Args:
        G: NetworkX DiGraph. Read-only here; lookup uses G.nodes.get to
            check is_empty on existing entries.
        extraction: Extraction data dict from the scanner.

    Returns:
        Tuple ``(name_to_domain, name_to_nid)``:
        - name_to_domain: dict[function_name, domain_string]
        - name_to_nid: dict[function_name, node_id_string]
    """
    name_to_domain: Dict[str, str] = {}
    name_to_nid: Dict[str, str] = {}
    for func in extraction.get("functions", []):
        fname = func.get("name", "")
        fdomain = func.get("domain", "")
        fnid = func.get("id", "")
        if fname and fdomain:
            name_to_domain[fname] = fdomain
        if fname and fnid:
            # When same name exists from multiple sources (C + ASM),
            # prefer non-extern (complete definition) over extern declaration
            if fname in name_to_nid:
                existing_nid = name_to_nid[fname]
                existing_node = G.nodes.get(existing_nid, {})
                new_node = G.nodes.get(fnid, {})
                # Prefer: has body (not extern) > no body (extern)
                if (not existing_node.get("is_empty", True) and
                        new_node.get("is_empty", True)):
                    continue  # Keep existing (has body)
            name_to_nid[fname] = fnid
    return name_to_domain, name_to_nid


# ---------------------------------------------------------------------------
# Phase 8: process ASM aliases (SYM_FUNC_ALIAS)
# ---------------------------------------------------------------------------

def _process_asm_aliases(extraction: dict, G, name_to_nid: dict) -> dict:
    """Create alias→original mapping edges for ASM SYM_FUNC_ALIAS entries.

    SYM_FUNC_ALIAS(alias, original) tells the linker that 'alias' is an
    alternate entry point for 'original'. The scanner captures this in
    extraction['asm_aliases'] as a list of {alias, original} dicts.

    This phase adds EXTRACTED edges from alias→original so queries that
    reach the alias continue to the original implementation.

    Args:
        extraction: Extraction data dict from the scanner.
        G: NetworkX DiGraph. Mutated in place — alias→original edges added.
        name_to_nid: dict[function_name, node_id] for resolving names to node ids.

    Returns:
        asm_alias_map: dict[alias_name, original_name] for downstream
        use (e.g., resolving call targets that hit alias names).
    """
    asm_aliases = extraction.get("asm_aliases", [])
    asm_alias_map: Dict[str, str] = {}
    for alias_info in asm_aliases:
        alias_name = alias_info.get("alias", "")
        original_name = alias_info.get("original", "")
        if alias_name and original_name:
            asm_alias_map[alias_name] = original_name
            alias_nid = name_to_nid.get(alias_name)
            original_nid = name_to_nid.get(original_name)
            if alias_nid and original_nid and alias_nid != original_nid:
                if not G.has_edge(alias_nid, original_nid):
                    G.add_edge(
                        alias_nid, original_nid,
                        call_order=0,
                        call_condition="",
                        confidence="EXTRACTED",
                        source="asm_alias",
                        confidence_score=1.0,
                    )
    return asm_alias_map


# ---------------------------------------------------------------------------
# Phase 9: label EXPORT_SYMBOL functions as API_entry
# ---------------------------------------------------------------------------

def _label_export_symbol_functions(G, extraction: dict, name_to_nid: dict,
                                     non_api_paths: tuple) -> int:
    """Label EXPORT_SYMBOL functions as API_entry.

    EXPORT_SYMBOL is the Linux kernel mechanism for making a function
    callable from other modules. Such functions are entry points into
    the module. Skips functions in non-API paths (e.g., tools/, scripts/,
    selftests/) — these are project-internal and shouldn't be tagged
    API_entry even if they appear in EXPORT_SYMBOL.

    Args:
        G: NetworkX DiGraph. Mutated in place — labels may be appended.
        extraction: Extraction data dict from the scanner.
        name_to_nid: dict[function_name, node_id] for resolving names.
        non_api_paths: tuple of path substrings to skip (e.g.,
            ``('tools/', 'scripts/')``). Empty tuple = don't skip any.

    Returns:
        Number of functions newly labeled as API_entry.
    """
    labeled = 0
    for exp in extraction.get("export_symbols", []):
        exp_name = exp.get("name", "")
        if not exp_name or exp_name not in name_to_nid:
            continue
        exp_nid = name_to_nid[exp_name]
        if exp_nid not in G.nodes:
            continue
        # Check source_file for non-API paths
        src = G.nodes[exp_nid].get("source_file", "").replace(os.sep, '/')
        if any(p in src for p in non_api_paths):
            continue
        labels = list(G.nodes[exp_nid].get("labels", []))
        if "API_entry" not in labels:
            labels.append("API_entry")
            G.nodes[exp_nid]["labels"] = labels
            labeled += 1
    return labeled


# ---------------------------------------------------------------------------
# Phase 10: label struct_op_types functions as API_entry
# ---------------------------------------------------------------------------

def _label_struct_op_types(G, extraction: dict, profile,
                            name_to_nid: dict, non_api_paths: tuple) -> int:
    """Label functions registered in struct_op_types (VFS ops) as API_entry.

    Functions registered in profile-declared struct_op_types (e.g.,
    file_operations, inode_operations) are kernel/framework API surface —
    called through function pointer dispatch by the framework, making
    them entry points into the module.

    Skips functions in non-API paths from profile.project_boundaries.non_api_paths.

    Args:
        G: NetworkX DiGraph. Mutated in place — labels may be appended.
        extraction: Extraction data dict from the scanner.
        profile: Builder config dict. Reads struct_op_types list.
        name_to_nid: dict[function_name, node_id].
        non_api_paths: tuple of path substrings to skip.

    Returns:
        Number of functions newly labeled as API_entry.
    """
    struct_op_types = set(profile.get("struct_op_types", [])) if profile else set()
    if not struct_op_types:
        return 0
    labeled = 0
    for vtable in extraction.get("vtable_registrations", []):
        stype = vtable.get("struct_type", "")
        if stype not in struct_op_types:
            continue
        for reg in vtable.get("registrations", []):
            fn_name = reg.get("func_name", "")
            if not fn_name or fn_name not in name_to_nid:
                continue
            fn_nid = name_to_nid[fn_name]
            if fn_nid not in G.nodes:
                continue
            src = G.nodes[fn_nid].get("source_file", "").replace(os.sep, '/')
            if any(p in src for p in non_api_paths):
                continue
            labels = list(G.nodes[fn_nid].get("labels", []))
            if "API_entry" not in labels:
                labels.append("API_entry")
                G.nodes[fn_nid]["labels"] = labels
                labeled += 1
    return labeled


# ---------------------------------------------------------------------------
# Phase 11: emit OPS_BIND edges for vtable registrations
# ---------------------------------------------------------------------------

def _emit_ops_bind_edges(G, extraction: dict, profile,
                          name_to_nid: dict) -> int:
    """Emit explicit OPS_BIND edges for each vtable registration.

    Unlike the in-memory vtable_index used for fn_ptr dispatch
    resolution, these edges are PERSISTED in the graph so queries can
    find "all functions bound to file_operations.read_iter" without
    recomputing the index at query time.

    Synthetic vtable nodes are added under id 'vtable::<var_name>' (or
    'vtable::<struct_type>' when var_name is empty). Each vtable node
    gets an OPS_BIND edge to the registered function node, carrying
    field_name + struct_type + condition metadata for downstream
    filtering at query time.

    Args:
        G: NetworkX DiGraph. Mutated in place — vtable nodes + OPS_BIND edges.
        extraction: Extraction data dict.
        profile: Builder config dict (used for _is_test_source check).
        name_to_nid: dict[function_name, node_id].

    Returns:
        Number of OPS_BIND edges emitted.
    """
    # Lazy import to avoid cycle (see _build_vtable_field_index docstring)
    from _builder.graph_build import _is_test_source

    edges_emitted = 0
    for vtable in extraction.get("vtable_registrations", []):
        vtable_src = vtable.get("source_file", "")
        if _is_test_source(vtable_src, profile):
            continue
        stype = vtable.get("struct_type", "")
        var_name = vtable.get("var_name", "")
        vtable_cond = vtable.get("condition", "")
        for reg in vtable.get("registrations", []):
            fn_name = reg.get("func_name", "")
            field_name = reg.get("field", "")
            if not fn_name or fn_name not in name_to_nid:
                continue
            fn_nid = name_to_nid[fn_name]
            if fn_nid not in G.nodes:
                continue
            # Use the vtable var_name as the source node id (synthetic).
            vtable_nid = f"vtable::{var_name}" if var_name else f"vtable::{stype}"
            if vtable_nid not in G.nodes:
                G.add_node(
                    vtable_nid,
                    name=var_name or stype,
                    kind="vtable",
                    struct_type=stype,
                    source_file=vtable_src,
                    labels=[],
                    domain="",
                )
            G.add_edge(
                vtable_nid, fn_nid,
                relation="OPS_BIND",
                field_name=field_name,
                struct_type=stype,
                call_condition=vtable_cond,
                confidence="EXTRACTED",
                source_tag="vtable_registration",
                confidence_score=1.0,
                preproc_condition=vtable_cond,
                preproc_alive=True,
                evidence=f"vtable_registration: {stype}.{field_name} = "
                         f"{fn_name} (var={var_name}, condition={vtable_cond or 'none'})",
            )
            edges_emitted += 1
    return edges_emitted


# ---------------------------------------------------------------------------
# Phase 12: build field dispatch map from field_assignments
# ---------------------------------------------------------------------------

def _build_field_dispatch_map(extraction: dict,
                                name_to_domain: dict) -> tuple:
    """Build the field dispatch map from struct field assignments.

    The field dispatch map resolves FN_PTR calls like ``ctx->cb_fn()``
    to actual callback targets, using the assignments captured by the
    scanner (e.g., ``ctx->cb_fn = my_callback`` in some init function).

    Four indexes are built from extraction['field_assignments']:
    - ``field_dispatch_map``: ``field_name → {struct_chain → {target_func, ...}}``
      Two-level; struct_chain is the path to the field (e.g., "req->payload").
      -> and . are normalized to . to merge equivalent access paths.
    - ``field_dispatch_flat``: ``field_name → {target_func, ...}`` (all
      targets, no struct context — used as fallback when struct_chain
      is unknown).
    - ``field_dispatch_by_domain``: ``field_name → caller_domain → {target_func}```
      for domain-scoped dispatch (resolves ambiguous callbacks within a module).
    - ``field_dispatch_by_target_domain``: ``field_name → target_domain → {target_func}```
      for cross-module dispatch by target function's domain.

    Param-bridged field_assignments (e.g., ``ctx->cb_fn = cb_fn`` where
    ``cb_fn`` is a parameter of the caller, not a concrete function)
    are recorded separately in ``param_bridged_fa`` for later resolution
    via the caller's incoming CALLBACK_ARG edges.

    Args:
        extraction: Extraction data dict.
        name_to_domain: dict[function_name, domain_string] used to derive
            caller_domain / target_domain for the domain-scoped indexes.

    Returns:
        Tuple ``(field_dispatch_map, field_dispatch_flat,
                 field_dispatch_by_domain, field_dispatch_by_target_domain,
                 param_bridged_fa)`` — all 5 dicts as documented above.
    """
    field_dispatch_map: Dict[str, Dict[str, set]] = {}
    field_dispatch_flat: Dict[str, set] = {}
    field_dispatch_by_domain: Dict[str, Dict[str, set]] = {}
    field_dispatch_by_target_domain: Dict[str, Dict[str, set]] = {}
    # (field_name, struct_chain_norm) → [(caller_name, param_name, param_index)]
    param_bridged_fa: Dict[Tuple[str, str], list] = {}

    for fa in extraction.get("field_assignments", []):
        field_name = fa.get("field_name", "")
        target_func = fa.get("target_func", "")
        struct_chain = fa.get("struct_chain", "")
        caller_name = fa.get("caller", "")
        is_param = fa.get("is_param", False)
        param_index = fa.get("param_index", -1)

        if not field_name or not target_func:
            continue

        if is_param:
            # Record param-bridged FA for later resolution
            sc_norm = struct_chain.replace("->", ".")
            key = (field_name, sc_norm)
            param_bridged_fa.setdefault(key, []).append(
                (caller_name, target_func, param_index))
            # Also index under original struct_chain
            if sc_norm != struct_chain:
                key2 = (field_name, struct_chain)
                param_bridged_fa.setdefault(key2, []).append(
                    (caller_name, target_func, param_index))
            continue

        # Normal (non-param) field_assignment
        # Normalize struct_chain: treat -> and . as equivalent
        sc_norm = struct_chain.replace("->", ".")
        field_dispatch_map.setdefault(field_name, {}).setdefault(
            sc_norm, set()).add(target_func)
        # Also index under original key for backward compatibility
        if sc_norm != struct_chain:
            field_dispatch_map.setdefault(field_name, {}).setdefault(
                struct_chain, set()).add(target_func)
        field_dispatch_flat.setdefault(field_name, set()).add(target_func)
        # Derive domain from caller function
        caller_domain = name_to_domain.get(caller_name, "")
        if caller_domain:
            field_dispatch_by_domain.setdefault(field_name, {}).setdefault(
                caller_domain, set()).add(target_func)
        # Also index by target function's domain for cross-module dispatch
        target_domain = name_to_domain.get(target_func, "")
        if target_domain:
            field_dispatch_by_target_domain.setdefault(field_name, {}).setdefault(
                target_domain, set()).add(target_func)
            field_dispatch_by_domain.setdefault(field_name, {}).setdefault(
                caller_domain, set()).add(target_func)

    return (field_dispatch_map, field_dispatch_flat,
            field_dispatch_by_domain, field_dispatch_by_target_domain,
            param_bridged_fa)


# ---------------------------------------------------------------------------
# Phase 13: build fn_ptr_struct_lookup (caller, field) → struct_chain
# ---------------------------------------------------------------------------

def _build_fn_ptr_struct_lookup(extraction: dict) -> dict:
    """Build (caller_name, field_name) → struct_chain index from fn_ptr_calls.

    Used by context-aware dispatch when resolving an FN_PTR edge — the
    fn_ptr_call already knows which struct field is being called, and
    the caller's enclosing function may know the struct_chain from a
    preceding assignment. This lookup lets the dispatch resolve to
    the right callback set without re-parsing the caller's body.

    Args:
        extraction: Extraction data dict from the scanner.

    Returns:
        dict[(caller_name, field_name), struct_chain]
    """
    fn_ptr_struct_lookup: Dict[Tuple[str, str], str] = {}
    for caller_name, calls in extraction.get("fn_ptr_calls", {}).items():
        for call in calls:
            fn_field = call.get("field_name", "")
            fn_struct = call.get("struct_chain", "")
            if fn_field and fn_struct:
                fn_ptr_struct_lookup[(caller_name, fn_field)] = fn_struct
    return fn_ptr_struct_lookup


# ---------------------------------------------------------------------------
# Phase 14: build struct embedding index
# ---------------------------------------------------------------------------

def _build_struct_embedding_index(extraction: dict, profile) -> tuple:
    """Build the struct embedding index from extraction data + profile.

    The embedding index maps inner_type → list of
    {outer_type, member, domain_hint} entries. It is used downstream
    for improved vtable dispatch disambiguation and module_hint
    inference.

    Sources (in priority order — later sources can add but not
    overwrite earlier ones for the same (outer_type, member) pair,
    since the deduplication at the end uses the first occurrence):
    1. struct_defs: field_type/field_name pairs where field_type is a
       known struct type (i.e., struct A embeds struct B as a member
       → B → [{outer: A, member: <field-name>, ...}]).
    2. container_of_usages: outer_type/member pairs from macro-based
       container_of patterns (inner_type may be unknown, fall back to
       the member name as the lookup key).
    3. conversion_funcs: inner/outer type pairs from accessor
       functions (e.g., ext4_inode_info → inode).
    4. profile.struct_embeddings.manual_entries: explicit overrides
       supplied by the project profile (highest priority — wins on
       conflict because deduplication keeps the first occurrence).

    The final index is deduplicated by (outer_type, member) per inner
    type so downstream dispatch doesn't double-count the same embedding.

    Args:
        extraction: Extraction data dict from the scanner.
        profile: Builder config dict. Reads profile.struct_embeddings.

    Returns:
        embedding_index: dict[inner_type, list[{outer_type, member, domain_hint}]]
    """
    # 1. From struct_defs: field_type/field_name pairs where field_type is a known struct
    embedding_index: Dict[str, list] = {}
    known_struct_types: Set[str] = set()
    for sd in extraction.get("struct_defs", []):
        stype = sd.get("struct_type", "")
        if stype:
            known_struct_types.add(stype)
    for sd in extraction.get("struct_defs", []):
        stype = sd.get("struct_type", "")
        if not stype:
            continue
        for field in sd.get("fields", []):
            ftype = field.get("field_type", "")
            fname = field.get("field_name", "")
            if ftype and fname and ftype in known_struct_types:
                embedding_index.setdefault(ftype, []).append({
                    "outer_type": stype,
                    "member": fname,
                    "domain_hint": "",
                })
    # 2. From container_of_usages: add inner_type→outer_type (inner_type may be unknown)
    for co in extraction.get("container_of_usages", []):
        outer = co.get("outer_type", "")
        member = co.get("member", "")
        inner = co.get("inner_type", "")
        if outer and member:
            key = inner if inner else member  # fallback to member if inner unknown
            embedding_index.setdefault(key, []).append({
                "outer_type": outer,
                "member": member,
                "domain_hint": "",
            })
    # 3. From conversion_funcs: add inner_type→outer_type with member
    for cf in extraction.get("conversion_funcs", []):
        outer = cf.get("outer_type", "")
        inner = cf.get("inner_type", "")
        member = cf.get("member", "")
        if outer and inner:
            embedding_index.setdefault(inner, []).append({
                "outer_type": outer,
                "member": member,
                "domain_hint": "",
            })
    # 4. From profile manual_entries (highest priority — explicit overrides)
    se_config = profile.get("struct_embeddings", {}) if profile else {}
    for entry in se_config.get("manual_entries", []):
        inner = entry.get("inner_type", "")
        outer = entry.get("outer_type", "")
        member = entry.get("member", "")
        hint = entry.get("domain_hint", "")
        if inner and outer and member:
            embedding_index.setdefault(inner, []).append({
                "outer_type": outer,
                "member": member,
                "domain_hint": hint,
            })
    # Deduplicate embedding entries
    for key in embedding_index:
        seen = set()
        unique = []
        for entry in embedding_index[key]:
            ekey = (entry["outer_type"], entry["member"])
            if ekey not in seen:
                seen.add(ekey)
                unique.append(entry)
        embedding_index[key] = unique
    return embedding_index


# ---------------------------------------------------------------------------
# Phase 15: identify polymorphic callback fields
# ---------------------------------------------------------------------------

# Fields with >= this many distinct struct_chains in field_assignments
# are considered polymorphic callback fields (e.g., cb_fn, cb_func that
# appear across unrelated contexts). For these, domain_fallback and
# target_domain_hier strategies produce massive over-dispatch and
# should be skipped.
_POLYMORPHIC_FIELD_THRESHOLD = 5


def _identify_polymorphic_callback_fields(field_dispatch_map: dict,
                                            param_bridged_fa: dict) -> set:
    """Identify polymorphic callback fields (cb_fn, cb_func, etc.).

    Polymorphic fields are fields assigned in many different struct
    contexts (>= _POLYMORPHIC_FIELD_THRESHOLD distinct struct_chains).
    For such fields, the domain_fallback and target_domain_hier
    strategies over-dispatch massively (one cb_fn call would resolve
    to N unrelated callbacks across the project), so they must be
    detected and excluded from those strategies.

    Args:
        field_dispatch_map: dict[field_name, dict[struct_chain, set[func]]]
            from _build_field_dispatch_map.
        param_bridged_fa: dict[(field_name, struct_chain), list[(caller, param, idx)]]
            from _build_field_dispatch_map. Counted toward polymorphic
            detection so that fields only seen via param-bridged FAs
            (no concrete assignments) are still classified.

    Returns:
        Set of field-name strings classified as polymorphic.
    """
    polymorphic_fields: Set[str] = set()
    for field_name, chain_map in field_dispatch_map.items():
        if len(chain_map) >= _POLYMORPHIC_FIELD_THRESHOLD:
            polymorphic_fields.add(field_name)
    # Also count param-bridged FA struct_chains toward polymorphic detection
    param_bridged_struct_chains: Dict[str, set] = {}
    for (field_name, sc_norm), _entries in param_bridged_fa.items():
        param_bridged_struct_chains.setdefault(field_name, set()).add(sc_norm)
    for field_name, chains in param_bridged_struct_chains.items():
        existing = len(field_dispatch_map.get(field_name, {}))
        total = existing + len(chains)
        if total >= _POLYMORPHIC_FIELD_THRESHOLD:
            polymorphic_fields.add(field_name)
    return polymorphic_fields


# ---------------------------------------------------------------------------
# Phase 23: add CONTAINS edges (file → function containment)
# ---------------------------------------------------------------------------

def _add_contains_edges(G, project_name: str) -> dict:
    """Add CONTAINS edges from synthetic file nodes to function nodes.

    Group graph nodes by source_file, create a synthetic
    ``file:<source_file>`` node for each file (if not already present),
    and emit an EXTRACTED-CONFIDENCE CONTAINS edge from the file node
    to each function defined in that file. This lets queries traverse
    file→function containment (e.g., "what functions does foo.c define?").

    Args:
        G: NetworkX DiGraph. Mutated in place — file nodes + CONTAINS
            edges added.
        project_name: Project name string used to derive file FQN
            (e.g., ``linux_kernel.src.foo``).

    Returns:
        file_nodes: dict[source_file, file_node_id] — used downstream
            by the IMPORTS edges phase (which adds IMPORTS edges
            between file nodes) and the prune phase (which removes
            isolated file nodes that have no edges).
    """
    file_nodes: Dict[str, str] = {}
    for nid, ndata in list(G.nodes(data=True)):
        sf = ndata.get("source_file", "")
        if not sf:
            continue
        if sf not in file_nodes:
            # Create a synthetic file node if not already present
            file_id = f"file:{sf}"
            if file_id not in G:
                G.add_node(
                    file_id,
                    name=os.path.basename(sf),
                    fqn=f"{project_name}.{sf.replace(os.sep, '.').replace('/', '.')}",
                    source_file=sf,
                    domain=ndata.get("domain", "root"),
                    labels=["file"],
                    is_empty=False,
                    node_type="file",
                )
            file_nodes[sf] = file_id

        # Add CONTAINS edge from file node to this function
        fid = file_nodes[sf]
        if fid != nid and not G.has_edge(fid, nid):
            G.add_edge(
                fid, nid,
                relation="CONTAINS",
                concurrency="contains",
                confidence="EXTRACTED",
                source="ast",
                evidence="contains: file contains function definition",
            )
    return file_nodes


# ---------------------------------------------------------------------------
# Phase 24: add IMPORTS edges (#include relationships between file nodes)
# ---------------------------------------------------------------------------

# Regex for #include directives — module-level so it's compiled once
# per Python process, not once per call.
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]', re.MULTILINE)


def _add_imports_edges(G, file_nodes: dict) -> None:
    """Add IMPORTS edges between file nodes for #include relationships.

    For each non-file, non-empty function node that has body_text,
    parse ``#include <header>`` directives from its body and add
    IMPORTS edges from the function's file node to the included
    header's file node. Cross-domain imports only — same-domain
    imports are skipped (CONTAINS already covers traversal within a
    domain).

    Header resolution: tries the literal ``file:<header>`` id first,
    then falls back to suffix / basename match against existing file
    nodes (so ``#include <linux/foo.h>`` resolves to ``file:foo.h``
    when only that short form is present).

    Args:
        G: NetworkX DiGraph. Mutated in place — IMPORTS edges added.
        file_nodes: dict[source_file, file_node_id] from
            _add_contains_edges.
    """
    # Pre-compute basename → list of source_files index once before the
    # per-node loop. Previously the inner loop did an O(file_nodes) linear
    # scan per #include directive per node.
    _file_by_basename: dict = {}
    for _sf, _fid in file_nodes.items():
        _base = os.path.basename(_sf)
        _file_by_basename.setdefault(_base, []).append((_sf, _fid))

    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False) or ndata.get("node_type") == "file":
            continue
        body = ndata.get("body_text", "")
        if not body:
            continue
        sf = ndata.get("source_file", "")
        if sf not in file_nodes:
            continue
        fid = file_nodes[sf]

        for m in _INCLUDE_RE.finditer(body):
            header = m.group(1)
            # Find the file node for the included header
            header_id = f"file:{header}"
            # Also try to find by matching existing file nodes
            if header_id not in G:
                # O(matches) lookup via pre-computed basename index
                header_base = os.path.basename(header)
                for existing_sf, existing_id in _file_by_basename.get(header_base, []):
                    if existing_sf.endswith(header) or os.path.basename(existing_sf) == header_base:
                        header_id = existing_id
                        break
            if header_id in G and header_id != fid and not G.has_edge(fid, header_id):
                # Skip same-domain IMPORTS — they add no traversal value
                # beyond what CONTAINS already provides
                src_domain = G.nodes[fid].get("domain", "") if fid in G else ""
                tgt_domain = G.nodes[header_id].get("domain", "") if header_id in G else ""
                if src_domain and tgt_domain and src_domain == tgt_domain:
                    continue
                G.add_edge(
                    fid, header_id,
                    relation="IMPORTS",
                    concurrency="imports",
                    confidence="EXTRACTED",
                    source="ast",
                    import_path=header,
                )


# ---------------------------------------------------------------------------
# Phase 25: label callback functions (CALLBACK_ARG targets)
# ---------------------------------------------------------------------------

def _label_callback_functions(G) -> int:
    """Label callback functions (CALLBACK_ARG edge targets) as callback_func.

    A function that is the target of a CALLBACK_ARG edge (passed as a
    callback to a registration function like pthread_create) gets the
    ``callback_func`` label. Without this, worker_thread-style functions
    that don't match naming heuristics would only get leaf_func and
    their callback role would be invisible to queries that filter by
    label.

    Skips:
    - file nodes and external_endpoint nodes
    - auto-created bare-name nodes in the external domain (placeholder
      callees that the scanner couldn't attribute to a real source
      definition — marking them callback_func would trigger a
      validation warning "callback_func in external domain").

    Args:
        G: NetworkX DiGraph. Mutated in place — labels may be appended.

    Returns:
        Number of nodes newly labeled callback_func.
    """
    labeled = 0
    for nid in G.nodes():
        ndata = G.nodes[nid]
        if ndata.get("node_type") in ("file", "external_endpoint"):
            continue
        if ndata.get("auto_created") and ndata.get("domain") == "external":
            continue
        for _, _, d in G.in_edges(nid, data=True):
            if d.get("confidence") == "CALLBACK_ARG":
                labels = list(ndata.get("labels", []))
                if "callback_func" not in labels:
                    labels.append("callback_func")
                    ndata["labels"] = labels
                    labeled += 1
                break  # one CALLBACK_ARG edge is enough
    return labeled


# ---------------------------------------------------------------------------
# Phase 26: label entry/exit points (in_end / out_end / leaf_func)
# ---------------------------------------------------------------------------

def _label_entry_exit_points(G) -> None:
    """Label entry/exit point functions: in_end, out_end, leaf_func.

    Classification rules:
    - ``in_end``: function with no non-contains/non-imports in-edges
      (entry point — called from external code, dispatch, or not at all).
    - ``out_end``: function with no non-contains/non-imports out-edges
      AND that is an external endpoint (in external domain or has no
      source_file). These represent calls into external code.
    - ``leaf_func``: function with no non-contains/non-imports out-edges
      AND that is internal (has source_file, non-external domain).
      These are simply leaves in the invocation graph, not external
      endpoints.

    Skips file nodes and external_endpoint nodes (these have their own
    node_type and shouldn't receive function labels).

    Args:
        G: NetworkX DiGraph. Mutated in place — labels may be appended.
    """
    for nid in G.nodes():
        ndata = G.nodes[nid]
        if ndata.get("node_type") == "file" or ndata.get("node_type") == "external_endpoint":
            continue
        has_non_contains_in = any(
            d.get("relation") not in ("CONTAINS", "IMPORTS")
            for _, _, d in G.in_edges(nid, data=True))
        has_non_contains_out = any(
            d.get("relation") not in ("CONTAINS", "IMPORTS")
            for _, _, d in G.out_edges(nid, data=True))
        labels = list(ndata.get("labels", []))
        if not has_non_contains_in and "in_end" not in labels:
            labels.append("in_end")
        if not has_non_contains_out:
            dom = ndata.get("domain", "")
            has_src = bool(ndata.get("source_file", ""))
            is_external = dom == "external" or dom.startswith("external_")
            if is_external or not has_src:
                if "out_end" not in labels:
                    labels.append("out_end")
            else:
                # Internal leaf function — not an external endpoint
                if "leaf_func" not in labels:
                    labels.append("leaf_func")
        if labels != ndata.get("labels", []):
            ndata["labels"] = labels


# ---------------------------------------------------------------------------
# Phase 27: refine domains via profile domain_rules
# ---------------------------------------------------------------------------

def _refine_domains(G, profile) -> int:
    """Refine node domains via profile.domain_rules pattern→suffix rules.

    When all files are in the same directory (e.g., Linux kernel
    ``fs/ext4/``), path-based domain classification puts everything in
    "root". Domain rules refine domains by function name prefix
    patterns. E.g., ``{"pattern": "ext4_mb_.*", "domain_suffix":
    "mballoc"}`` turns domain ``root`` into ``root.mballoc`` for
    matching functions.

    The pattern is matched against the function's name (not its id).
    The suffix is appended to the existing domain with a dot
    separator. The first matching rule wins (per-function).

    Args:
        G: NetworkX DiGraph. Mutated in place — domain attribute may
            be updated on matching nodes.
        profile: Builder config dict. Reads profile.domain_rules
            (list of dicts with "pattern" and "domain_suffix" keys).

    Returns:
        Number of nodes whose domain was refined.
    """
    domain_rules = profile.get("domain_rules", []) if profile else []
    if not domain_rules:
        return 0
    refined = 0
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        name = ndata.get("name", "")
        domain = ndata.get("domain", "")
        if not name or not domain:
            continue
        for rule in domain_rules:
            pattern = rule.get("pattern", "")
            suffix = rule.get("domain_suffix", "")
            if pattern and suffix and re.match(pattern, name):
                ndata["domain"] = f"{domain}.{suffix}" if domain else suffix
                refined += 1
                break
    if refined:
        print(f"  Domain rules refined {refined} nodes", file=sys.stderr)
    return refined


# ---------------------------------------------------------------------------
# Phase 28: reclassify external / third-party domains
# ---------------------------------------------------------------------------

def _reclassify_external_domains(G, profile) -> int:
    """Mark external/third-party domains and prefix them with "external_".

    External domains (vendor/, third_party/, huawei.*, external_*, etc.)
    are tagged with ``is_external=True`` and their domain names are
    prefixed with "external_" so they are separated from project
    domains in queries and graph summaries.

    The check is performed by the module-level helper
    ``_is_external_domain(domain, profile)`` which considers both
    built-in patterns and profile-supplied external_domain_patterns.

    Args:
        G: NetworkX DiGraph. Mutated in place — is_external flag set
            and domain attribute may be renamed.
        profile: Builder config dict. Reads
            profile.external_domain_patterns (list of substrings).

    Returns:
        Number of nodes reclassified into external domains.
    """
    # Lazy import to avoid cycle
    from _builder.graph_build import _is_external_domain
    reclassed = 0
    for nid, ndata in G.nodes(data=True):
        if ndata.get("is_empty", False):
            continue
        domain = ndata.get("domain", "")
        if _is_external_domain(domain, profile):
            ndata["is_external"] = True
            # Prefix domain with "external_" unless already prefixed
            if not domain.startswith("external_") and domain != "external":
                new_domain = f"external_{domain}"
                ndata["domain"] = new_domain
                reclassed += 1
    if reclassed:
        print(f"  Reclassified {reclassed} nodes into external domains",
              file=sys.stderr)
    return reclassed


# ---------------------------------------------------------------------------
# Phase 29: prune isolated file nodes
# ---------------------------------------------------------------------------

def _prune_isolated_file_nodes(G, file_nodes: dict) -> int:
    """Remove file nodes that have no edges (degree 0).

    These are created for #include targets that have no extracted
    functions, or for headers referenced by IMPORTS but not containing
    any graph nodes. Removing them keeps the graph clean for query
    traversal (no orphan file nodes that contribute nothing).

    Mutates both G (removes nodes) and file_nodes (pops entries that
    pointed to pruned nodes) in place.

    Args:
        G: NetworkX DiGraph. Mutated in place — isolated file nodes
            removed.
        file_nodes: dict[source_file, file_node_id]. Mutated — entries
            pointing to pruned nodes are popped.

    Returns:
        Number of file nodes pruned.
    """
    pruned = 0
    for nid in list(G.nodes()):
        ndata = G.nodes[nid]
        if ndata.get("node_type") != "file":
            continue
        if G.degree(nid) == 0:
            G.remove_node(nid)
            # Remove from file_nodes mapping
            sf = ndata.get("source_file", "")
            if sf in file_nodes and file_nodes[sf] == nid:
                del file_nodes[sf]
            pruned += 1
    if pruned:
        print(f"  Pruned isolated file nodes: {pruned}", file=sys.stderr)
    return pruned


__all__ += [
    "_add_contains_edges",
    "_add_imports_edges",
    "_label_callback_functions",
    "_label_entry_exit_points",
    "_refine_domains",
    "_reclassify_external_domains",
    "_prune_isolated_file_nodes",
    "_annotate_call_edges_with_goto",
]


# ---------------------------------------------------------------------------
# Phase 30: annotate call edges with goto control flow information
# ---------------------------------------------------------------------------

def _annotate_call_edges_with_goto(G) -> int:
    """Annotate call edges with goto control flow information.

    For functions with goto_jumps, this phase adds a call_condition
    annotation to edges whose call_order falls within a goto's control
    flow range:

    - **Backward goto (loop)**: calls between the label (earlier line)
      and the goto (later line) may repeat. Annotated with
      ``goto_loop:<label_name>``.
    - **Forward goto (skip)**: calls between the goto (earlier line)
      and the target label (later line) may be skipped. Annotated with
      ``goto_skip:<label_name>``.

    Uses callee_args line info for precise line-based range matching.
    When a callee_arg has no explicit line, the function's start line
    is used as an approximation: ``func_line + call_order``.

    Args:
        G: NetworkX DiGraph. Mutated in place — call_condition attribute
            may be appended on edges from goto-bearing caller nodes.

    Returns:
        Number of call edges annotated with a goto control flow tag.
    """
    goto_annotated = 0
    for nid, ndata in G.nodes(data=True):
        goto_jumps = ndata.get("goto_jumps", [])
        goto_labels = ndata.get("goto_labels", [])
        if not goto_jumps:
            continue
        # Build label_name → line_number map
        label_line_map = {lbl["label"]: lbl["line"] for lbl in goto_labels}
        # Build call_order → line map from callee_args
        callee_args = ndata.get("callee_args", [])
        if not callee_args:
            continue
        # Collect call_order values with their line numbers.
        # callee_args may have "line" from scanner, otherwise approximate from order
        func_line = ndata.get("line", 0)
        co_lines: Dict[int, int] = {}
        for ca in callee_args:
            _co = ca.get("call_order")
            _ca_line = ca.get("line", 0)
            if _co is not None:
                co_lines[_co] = _ca_line if _ca_line else (func_line + _co)
        # For each goto jump, determine affected call_orders and annotate edges
        for gj in goto_jumps:
            label_name = gj["label"]
            goto_line = gj["line"]
            direction = gj.get("direction", "unknown")
            target_line = label_line_map.get(label_name)
            if target_line is None or direction == "unknown":
                continue
            # Determine the affected line range
            if direction == "backward":
                # Loop: calls between label (earlier line) and goto (later line)
                range_start = target_line
                range_end = goto_line
            else:  # forward
                # Skip: calls between goto (earlier line) and target label (later line)
                range_start = goto_line
                range_end = target_line
            # Find call edges from this function whose line falls in range
            for succ in G.successors(nid):
                try:
                    edge_data = G[nid][succ]
                except KeyError:
                    logging.getLogger(__name__).warning(
                        "edge %s->%s vanished during goto annotation", nid, succ)
                    continue
                co = edge_data.get("call_order")
                if co is None or co not in co_lines:
                    continue
                call_line = co_lines[co]
                if range_start <= call_line <= range_end:
                    existing_cond = edge_data.get("call_condition", "")
                    goto_tag = (f"goto_loop:{label_name}" if direction == "backward"
                                else f"goto_skip:{label_name}")
                    if existing_cond:
                        new_cond = f"{existing_cond} && {goto_tag}"
                    else:
                        new_cond = goto_tag
                    edge_data["call_condition"] = new_cond
                    goto_annotated += 1
    if goto_annotated > 0:
        print(f"[build] Annotated {goto_annotated} call edges with goto "
              f"control flow", file=sys.stderr)
    return goto_annotated
