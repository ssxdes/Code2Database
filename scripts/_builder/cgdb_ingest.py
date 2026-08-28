"""cgdb_ingest — convert scanner output into an IngestBatch for SQLiteCGDBStore.

- Takes the scan_file() result dict from ClangScanner/DualBackendScanner
- Produces an IngestBatch with NodeRecord / EdgeRecord / TypeRecord /
  FileRecord / InvokeSiteRecord entries for write_batch()
- Computes file_id via SHA-256 of the absolute path (stable across runs)
- Maps scan-result node IDs (already USR-hashed by ClangScanner) to NodeRecord.id
"""
import logging
import hashlib
import os

from _builder.cgdb_records import (
    IngestBatch, NodeRecord, EdgeRecord, TypeRecord, FileRecord,
    InvokeSiteRecord, IncludeRecord, ConfigPredicateRecord,
    OpsBindingRecord, BasicBlockRecord, CFGEdgeRecord,
    DataFlowRecord, SyncPrimitiveRecord, HappensBeforeRecord,
    AliasSetRecord, DocCommentRecord, MetadataRecord, ConditionRecord,
)


def file_id_for(path: str) -> int:
    """Compute a stable file_id from a path.

    Uses SHA-256 truncated to 60 bits so it fits in SQLite's signed 64-bit
    INTEGER (max 0x7FFF_FFFF_FFFF_FFFF). File IDs collide with node IDs in
    numerical space, but they live in different tables so there's no actual
    collision — cgdb_files.id is its own primary key.
    """
    h = hashlib.sha256(path.encode('utf-8')).hexdigest()[:15]
    return int(h, 16) & 0x0FFF_FFFF_FFFF_FFFF


def extract_cgdb_batch(scan_result: dict, commit_hash: str = "",
                        version_id: int = 1) -> IngestBatch:
    """Convert a scan_file() result dict into an IngestBatch.

    Reads cgdb_nodes/cgdb_types/cgdb_edges/cgdb_invoke_sites from scan_result.
    Falls back to synthesizing NodeRecords from legacy functions/edges if
    cgdb_* lists are empty (e.g., when only tree-sitter ran).
    """
    filepath = scan_result.get('file', '')
    if not filepath:
        return IngestBatch()

    # Build FileRecord
    try:
        with open(filepath, 'rb') as f:
            content = f.read()
        content_hash = hashlib.sha256(content).hexdigest()
        line_count = content.count(b'\n') + (1 if content and not content.endswith(b'\n') else 0)
        byte_count = len(content)
    except (IOError, OSError):
        content_hash = ''
        line_count = None
        byte_count = None

    fid = file_id_for(filepath)
    language = _language_for(filepath)
    file_record = FileRecord(
        id=fid,
        path=filepath,
        language=language,
        sha256=content_hash or '',
        line_count=line_count,
        byte_count=byte_count,
        commit_hash=commit_hash or None,
        content_hash=content_hash or '',
    )

    batch = IngestBatch(file=file_record, tu_id=0)

    # Track which node IDs were emitted from cgdb_nodes so we don't double-add
    # when synthesizing from legacy functions.
    seen_node_ids = set()

    # type_registry: canonical_spelling → type_id. Pre-populated from
    # cgdb_types below, and lazily extended for nodes whose type_id is
    # missing but type_spelling is present.
    type_by_spelling: dict = {}
    # Pre-populate from cgdb_types so the node loop can look up existing
    # types without waiting for section 2.
    for _t in scan_result.get('cgdb_types', []):
        _canon = _t.get('canonical_spelling') or _t.get('spelling') or ''
        if _canon:
            type_by_spelling[_canon] = int(_t['id'])

    # 1. Convert cgdb_nodes → NodeRecord
    # Map legacy/invalid kind values to the cgdb_nodes.kind CHECK constraint
    # whitelist. Values not in the map are passed through; if they violate
    # the CHECK constraint the write_batch will fail and the offending row
    # is dropped (logged via the store's exception handler).
    _KIND_NORMALIZE = {
        'type': 'typedef',     # legacy scanner emit for enum/typedef nodes
        'enum_type': 'enum',
        'typedef_type': 'typedef',
        'struct_type': 'struct',
        'union_type': 'union',
        'class_type': 'class',
        'global_var': 'var',
        'global': 'var',
        'local_var': 'var',
        'param': 'parm',
    }
    _ALLOWED_KINDS = frozenset({
        'function', 'method', 'constructor', 'destructor',
        'var', 'parm', 'field', 'enum_constant', 'typedef',
        'struct', 'class', 'union', 'enum',
        'stmt', 'expr', 'decl_ref', 'member_ref',
        'label', 'namespace', 'template', 'concept',
        'file', 'macro', 'include',
        'vtable', 'ops_table',
    })
    for n in scan_result.get('cgdb_nodes', []):
        nid = int(n['id'])
        seen_node_ids.add(nid)
        # signature/body_text go into attrs as well as denormalized columns
        attrs = dict(n.get('attrs') or {})
        if n.get('signature'):
            attrs['signature'] = n['signature']
        if n.get('body_text'):
            attrs['body_text'] = n['body_text']
        # L3.5: propagate config_predicate_id from the scan-result node dict
        # (set by ClangScanner via ConfigPredicateExtractor.pass3_predicate_for_range).
        config_predicate_id = n.get('config_predicate_id')
        if config_predicate_id is not None:
            config_predicate_id = int(config_predicate_id)
        # L1+5.4.2: propagate enclosing_symbol_id (set by ClangScanner via
        # cursor.semantic_parent walk). 0/None = file/TU scope.
        enclosing_symbol_id = n.get('enclosing_symbol_id') or 0
        if enclosing_symbol_id:
            enclosing_symbol_id = int(enclosing_symbol_id)
        else:
            enclosing_symbol_id = None
        # Normalize kind to satisfy the cgdb_nodes.kind CHECK constraint.
        raw_kind = n.get('kind', '') or ''
        kind = _KIND_NORMALIZE.get(raw_kind, raw_kind)
        if kind not in _ALLOWED_KINDS:
            # Unknown kind — default to 'var' (least surprising neutral kind).
            # The original kind is preserved in attrs for debugging.
            if raw_kind and raw_kind != 'var':
                attrs['_original_kind'] = raw_kind
            kind = 'var'
        # Backfill type_id when the node has type_spelling but no type_id.
        # This commonly happens when the scanner falls back to the full
        # signature for type_spelling (e.g. Python functions without return
        # annotations). Register the type on demand so the node carries a
        # proper FK into cgdb_types.
        node_type_spelling = n.get('type_spelling', '') or ''
        node_type_id = n.get('type_id')
        if not node_type_id and node_type_spelling:
            canon = " ".join(str(node_type_spelling).split())
            if canon in type_by_spelling:
                node_type_id = type_by_spelling[canon]
            else:
                # Lazily register a new type. Categorize lexically — the
                # scanner's _register_type uses the same logic.
                stripped = canon
                is_const = 'const ' in canon + ' '
                is_volatile = 'volatile ' in canon + ' '
                tk = 'builtin'
                if '*' in stripped:
                    tk = 'pointer'
                elif stripped.startswith('enum '):
                    tk = 'enum'
                elif stripped.startswith('struct ') or stripped.startswith('union '):
                    tk = 'record'
                elif stripped.startswith('typedef '):
                    tk = 'typedef'
                elif '[' in stripped and stripped.endswith(']'):
                    tk = 'array'
                elif '(' in stripped and ')' in stripped:
                    tk = 'function'
                new_type_id = int(
                    hashlib.sha256(f'type:{canon}'.encode('utf-8')).hexdigest()[:15],
                    16,
                ) & 0x0FFF_FFFF_FFFF_FFFF
                type_by_spelling[canon] = new_type_id
                batch.types.append(TypeRecord(
                    id=new_type_id,
                    spelling=canon,
                    canonical_spelling=canon,
                    kind=tk,
                    size_bytes=None,
                    is_const=is_const,
                    is_volatile=is_volatile,
                    attrs={},
                ))
                node_type_id = new_type_id
        node = NodeRecord(
            id=nid,
            kind=kind,
            name=n['name'],
            fqn=n['fqn'],
            file_id=fid,
            line=int(n.get('line', 0)),
            col=int(n.get('col', 0)),
            byte_start=int(n.get('byte_start', 0)),
            byte_end=int(n.get('byte_end', 0)),
            type_spelling=node_type_spelling,
            type_id=node_type_id,
            config_predicate_id=config_predicate_id,
            enclosing_symbol_id=enclosing_symbol_id,
            attrs=attrs,
            commit_hash=commit_hash or None,
            first_seen_version=version_id,
            last_seen_version=version_id,
            source_snippet=n.get('source_snippet', '') or '',
        )
        # signature/body_text are denormalized columns on cgdb_nodes,
        # but they're inside attrs here — the store extracts them in _write_nodes.
        batch.nodes.append(node)

    # 2. Convert cgdb_types → TypeRecord
    # Populate the type_by_spelling map so we can backfill type_id on nodes
    # that have type_spelling but no type_id (e.g. function nodes whose
    # type_spelling is the full signature).
    for t in scan_result.get('cgdb_types', []):
        type_id = int(t['id'])
        canon = t.get('canonical_spelling') or t.get('spelling') or ''
        if canon:
            type_by_spelling[canon] = type_id
        batch.types.append(TypeRecord(
            id=type_id,
            spelling=t['spelling'],
            canonical_spelling=t['canonical_spelling'],
            kind=t['kind'],
            size_bytes=t.get('size_bytes'),
            is_const=bool(t.get('is_const', False)),
            is_volatile=bool(t.get('is_volatile', False)),
            attrs=dict(t.get('attrs') or {}),
        ))

    # 3. Convert cgdb_edges → EdgeRecord
    #    Backwards-compat: translate old 'CALLS' kind to 'INVOKES' (post-rename
    #    enum value). The CHECK constraint on cgdb_edges.kind rejects 'CALLS'.
    _EDGE_KIND_LEGACY_MAP = {'CALLS': 'INVOKES'}
    for e in scan_result.get('cgdb_edges', []):
        # L3.5: propagate config_predicate_id from edge dict (if set by scanner)
        edge_pred_id = e.get('config_predicate_id')
        if edge_pred_id is not None:
            edge_pred_id = int(edge_pred_id)
        # L7: propagate edge_id (deterministic ID for OPS_BIND edges so that
        # ops_bindings.edge_id FK is stable across runs).
        edge_id = e.get('edge_id')
        if edge_id is not None:
            edge_id = int(edge_id)
        # L1+5.4.2: propagate enclosing_symbol_id (set by ClangScanner).
        edge_enclosing = e.get('enclosing_symbol_id') or 0
        if edge_enclosing:
            edge_enclosing = int(edge_enclosing)
        else:
            edge_enclosing = None
        edge_kind = e['kind']
        edge_kind = _EDGE_KIND_LEGACY_MAP.get(edge_kind, edge_kind)
        # Read byte_start/byte_end from edge dict if present (set by
        # _emit_cgdb_records via line-offset lookup).
        edge_bs = e.get('byte_start')
        edge_be = e.get('byte_end')
        if edge_bs is not None:
            try:
                edge_bs = int(edge_bs)
            except (ValueError, TypeError):
                edge_bs = None
        if edge_be is not None:
            try:
                edge_be = int(edge_be)
            except (ValueError, TypeError):
                edge_be = None
        batch.edges.append(EdgeRecord(
            src_id=int(e['src_id']),
            dst_id=int(e['dst_id']),
            kind=edge_kind,
            file_id=fid,
            line=int(e.get('line', 0)) if e.get('line') else None,
            col=int(e.get('col', 0)) if e.get('col') else None,
            byte_start=edge_bs,
            byte_end=edge_be,
            config_predicate_id=edge_pred_id,
            enclosing_symbol_id=edge_enclosing,
            attrs=dict(e.get('attrs') or {}),
            commit_hash=commit_hash or None,
            first_seen_version=version_id,
            last_seen_version=version_id,
            edge_id=edge_id,
        ))

    # 4. Convert cgdb_invoke_sites → InvokeSiteRecord
    #    Backwards-compat: also read the old 'cgdb_call_sites' key with old
    #    field names (caller_id/callee_id/call_kind).
    invoke_sites_raw = list(scan_result.get('cgdb_invoke_sites', []) or [])
    if not invoke_sites_raw:
        for cs in scan_result.get('cgdb_call_sites', []) or []:
            cs = dict(cs)  # shallow copy so we can rewrite keys
            if 'invoker_id' not in cs and 'caller_id' in cs:
                cs['invoker_id'] = cs['caller_id']
            if 'invoked_id' not in cs and 'callee_id' in cs:
                cs['invoked_id'] = cs['callee_id']
            if 'invoke_kind' not in cs and 'call_kind' in cs:
                cs['invoke_kind'] = cs['call_kind']
            invoke_sites_raw.append(cs)
    for cs in invoke_sites_raw:
        try:
            invoker_id = int(cs.get('invoker_id') or cs.get('caller_id') or 0)
            invoked_id = int(cs.get('invoked_id') or cs.get('callee_id') or 0)
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        if not invoker_id or not invoked_id:
            continue
        # Read dispatch_candidates and arg_bindings if present (set by
        # _emit_cgdb_records when fn_ptr_calls + vtable_registrations match).
        dispatch_candidates = cs.get('dispatch_candidates') or []
        if dispatch_candidates and not isinstance(dispatch_candidates, list):
            try:
                import json as _json
                dispatch_candidates = _json.loads(dispatch_candidates) if isinstance(dispatch_candidates, str) else list(dispatch_candidates)
            except Exception:
                dispatch_candidates = []
        arg_bindings = cs.get('arg_bindings') or []
        if arg_bindings and not isinstance(arg_bindings, list):
            try:
                import json as _json
                arg_bindings = _json.loads(arg_bindings) if isinstance(arg_bindings, str) else list(arg_bindings)
            except Exception:
                arg_bindings = []
        batch.invoke_sites.append(InvokeSiteRecord(
            invoker_id=invoker_id,
            invoked_id=invoked_id,
            invoke_kind=cs.get('invoke_kind') or cs.get('call_kind') or 'direct',
            dispatch_candidates=[int(c) for c in dispatch_candidates if c],
            arg_bindings=[dict(a) if isinstance(a, dict) else {'value': str(a)} for a in arg_bindings] if arg_bindings else [],
        ))

    # 5. Convert cgdb_predicates → ConfigPredicateRecord (L3.5)
    #    These are deduplicated by id (stable hash of text_form), so the same
    #    predicate in different files resolves to the same row.
    seen_pred_ids = {p.id for p in batch.config_predicates}
    for p in scan_result.get('cgdb_predicates', []):
        try:
            pred_id = int(p['id'])
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        if pred_id in seen_pred_ids:
            continue
        seen_pred_ids.add(pred_id)
        batch.config_predicates.append(ConfigPredicateRecord(
            id=pred_id,
            root_expr_id=None,
            text_form=p.get('text_form', '') or '',
            z3_form=p.get('z3_form', '') or '',
            bdd_serialized=p.get('bdd_serialized', '') or '',
            config_macros=list(p.get('config_macros', []) or []),
            is_unconditional=bool(p.get('is_unconditional', False)),
            is_contradictory=bool(p.get('is_contradictory', False)),
        ))

    # 5b. Convert conditions → ConditionRecord (L3)
    #     One record per branch condition emitted by the scanner. De-dup by id
    #     (stable hash of function_id + text_form) so the same condition
    #     appearing in multiple per-file sub-results resolves to one row.
    #     Scanners emit under either 'conditions' (legacy clang path) or
    #     'cgdb_conditions' (tree-sitter heuristic path).
    seen_cond_ids = {c.id for c in batch.conditions}
    cond_sources = list(scan_result.get('conditions', [])) + \
                   list(scan_result.get('cgdb_conditions', []))
    for c in cond_sources:
        try:
            cid = int(c['id'])
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        if cid in seen_cond_ids:
            continue
        seen_cond_ids.add(cid)
        try:
            attrs = c.get('attrs', {}) or {}
        except Exception:
            attrs = {}
        kind = c.get('kind', 'atom') or 'atom'
        if kind not in ('comparison', 'logical', 'unary', 'atom', 'macro_call'):
            kind = 'atom'
        text_form = c.get('text_form', '') or ''
        if not text_form:
            continue
        batch.conditions.append(ConditionRecord(
            id=cid,
            root_expr_id=c.get('root_expr_id'),
            kind=kind,
            operator=c.get('operator', '') or '',
            left_expr_id=c.get('left_expr_id'),
            right_expr_id=c.get('right_expr_id'),
            text_form=text_form,
            z3_form=c.get('z3_form', '') or '',
            attrs=attrs,
        ))

    # 6. Convert cgdb_ops_bindings → OpsBindingRecord (L7)
    #    Each ops_binding references a cgdb_edges row via edge_id. The
    #    corresponding EdgeRecord was already added in step 3 (carrying the
    #    same edge_id). De-dup by edge_id in case the same binding appears
    #    in multiple per-file sub-results.
    seen_opbind_edge_ids = {b.edge_id for b in batch.ops_bindings}
    for b in scan_result.get('cgdb_ops_bindings', []):
        try:
            edge_id = int(b['edge_id'])
            ops_table_id = int(b['ops_table_id'])
            field_node_id = int(b['field_node_id'])
            impl_function_id = int(b['impl_function_id'])
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        if edge_id in seen_opbind_edge_ids:
            continue
        seen_opbind_edge_ids.add(edge_id)
        batch.ops_bindings.append(OpsBindingRecord(
            edge_id=edge_id,
            ops_table_id=ops_table_id,
            field_node_id=field_node_id,
            impl_function_id=impl_function_id,
            signature_match=bool(b.get('signature_match', True)),
        ))

    # 7. Convert cgdb_basic_blocks → BasicBlockRecord (L4)
    #    De-dup by id (stable hash of function_id|block_index).
    seen_bb_ids = {b.id for b in batch.basic_blocks}
    for b in scan_result.get('cgdb_basic_blocks', []):
        try:
            bb_id = int(b['id'])
            func_id = int(b['function_id'])
            block_index = int(b['block_index'])
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        if bb_id in seen_bb_ids:
            continue
        seen_bb_ids.add(bb_id)
        batch.basic_blocks.append(BasicBlockRecord(
            id=bb_id,
            function_id=func_id,
            block_index=block_index,
            is_entry=bool(b.get('is_entry', False)),
            is_exit=bool(b.get('is_exit', False)),
        ))

    # 8. Convert cgdb_cfg_edges → CFGEdgeRecord (L4)
    #    De-dup by (src_block_id, dst_block_id, kind).
    seen_cfg_edges = {
        (e.src_block_id, e.dst_block_id, e.kind) for e in batch.cfg_edges
    }
    for e in scan_result.get('cgdb_cfg_edges', []):
        try:
            src_block_id = int(e['src_block_id'])
            dst_block_id = int(e['dst_block_id'])
            kind = e['kind']
            func_id = int(e['function_id'])
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        key = (src_block_id, dst_block_id, kind)
        if key in seen_cfg_edges:
            continue
        seen_cfg_edges.add(key)
        batch.cfg_edges.append(CFGEdgeRecord(
            src_block_id=src_block_id,
            dst_block_id=dst_block_id,
            kind=kind,
            function_id=func_id,
        ))

    # 9. Convert cgdb_data_flow → DataFlowRecord (L5)
    #    De-dup by (var_id, def_stmt_id, use_stmt_id, kind).
    seen_df_keys = {
        (d.var_id, d.def_stmt_id, d.use_stmt_id, d.kind)
        for d in batch.data_flow
    }
    for d in scan_result.get('cgdb_data_flow', []):
        try:
            var_id = int(d['var_id'])
            def_stmt_id = int(d.get('def_stmt_id') or 0)
            use_stmt_id = int(d.get('use_stmt_id') or 0)
            func_id = int(d['function_id'])
            kind = d.get('kind', 'use')
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        key = (var_id, def_stmt_id, use_stmt_id, kind)
        if key in seen_df_keys:
            continue
        seen_df_keys.add(key)
        batch.data_flow.append(DataFlowRecord(
            var_id=var_id,
            def_stmt_id=def_stmt_id if def_stmt_id else None,
            use_stmt_id=use_stmt_id if use_stmt_id else None,
            function_id=func_id,
            kind=kind,
        ))

    # 10. Convert cgdb_sync_primitives → SyncPrimitiveRecord (L8)
    #     De-dup by (function_id, kind, sync_var_id, acquire_stmt_id, release_stmt_id).
    seen_sync_keys = {
        (s.function_id, s.kind, s.sync_var_id, s.acquire_stmt_id, s.release_stmt_id)
        for s in batch.sync_primitives
    }
    for s in scan_result.get('cgdb_sync_primitives', []):
        try:
            func_id = int(s['function_id'])
            kind = s['kind']
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        sync_var_id = s.get('sync_var_id')
        sync_var_id = int(sync_var_id) if sync_var_id is not None else None
        acquire_stmt_id = s.get('acquire_stmt_id')
        acquire_stmt_id = int(acquire_stmt_id) if acquire_stmt_id is not None else None
        release_stmt_id = s.get('release_stmt_id')
        release_stmt_id = int(release_stmt_id) if release_stmt_id is not None else None
        key = (func_id, kind, sync_var_id, acquire_stmt_id, release_stmt_id)
        if key in seen_sync_keys:
            continue
        seen_sync_keys.add(key)
        batch.sync_primitives.append(SyncPrimitiveRecord(
            function_id=func_id,
            kind=kind,
            sync_var_id=sync_var_id,
            acquire_stmt_id=acquire_stmt_id,
            release_stmt_id=release_stmt_id,
        ))

    # 11. Convert cgdb_happens_before → HappensBeforeRecord (L8)
    #     De-dup by (write_event_id, read_event_id, reason).
    seen_hb_keys = {
        (h.write_event_id, h.read_event_id, h.reason)
        for h in batch.happens_before
    }
    for h in scan_result.get('cgdb_happens_before', []):
        try:
            write_event_id = int(h['write_event_id'])
            read_event_id = int(h['read_event_id'])
            reason = h.get('reason', 'program_order')
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        key = (write_event_id, read_event_id, reason)
        if key in seen_hb_keys:
            continue
        seen_hb_keys.add(key)
        confidence = h.get('confidence', 1.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 1.0
        batch.happens_before.append(HappensBeforeRecord(
            write_event_id=write_event_id,
            read_event_id=read_event_id,
            reason=reason,
            confidence=confidence,
        ))

    # 12. Convert cgdb_alias_sets → AliasSetRecord (L6 alias sets)
    seen_alias_keys = {
        (a.ptr1_node_id, a.ptr2_node_id, a.kind)
        for a in batch.alias_sets
    }
    for a in scan_result.get('cgdb_alias_sets', []):
        try:
            ptr1 = int(a['ptr1_node_id'])
            ptr2 = int(a['ptr2_node_id'])
            kind = a.get('kind', 'may_alias')
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        key = (ptr1, ptr2, kind)
        if key in seen_alias_keys:
            continue
        seen_alias_keys.add(key)
        confidence = a.get('confidence', 1.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 1.0
        batch.alias_sets.append(AliasSetRecord(
            ptr1_node_id=ptr1,
            ptr2_node_id=ptr2,
            kind=kind,
            confidence=confidence,
        ))

    # 13. Convert cgdb_doc_comments → DocCommentRecord (L10 doc comments)
    for d in scan_result.get('cgdb_doc_comments', []):
        try:
            node_id = int(d['node_id'])
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        try:
            tags = d.get('tags', {})
            if not isinstance(tags, dict):
                tags = {}
        except Exception:
            tags = {}
        batch.doc_comments.append(DocCommentRecord(
            node_id=node_id,
            file_id=fid,
            line=int(d.get('line', 0) or 0),
            col=int(d.get('col', 0) or 0),
            comment_kind=str(d.get('comment_kind', '') or ''),
            raw_text=str(d.get('raw_text', '') or ''),
            cleaned_text=str(d.get('cleaned_text', '') or ''),
            tags=tags,
            byte_start=int(d.get('byte_start', 0) or 0),
            byte_end=int(d.get('byte_end', 0) or 0),
        ))

    # 14. Convert cgdb_metadata → MetadataRecord (L11 typed metadata)
    seen_meta_keys = {
        (m.target_id, m.target_kind, m.key)
        for m in batch.metadata
    }
    for m in scan_result.get('cgdb_metadata', []):
        try:
            target_id = int(m['target_id'])
            target_kind = str(m.get('target_kind', 'node') or 'node')
            key = str(m.get('key', '') or '')
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        if not key:
            continue
        mkey = (target_id, target_kind, key)
        if mkey in seen_meta_keys:
            continue
        seen_meta_keys.add(mkey)
        batch.metadata.append(MetadataRecord(
            target_id=target_id,
            target_kind=target_kind,
            key=key,
            value=str(m.get('value', '') or ''),
            value_type=str(m.get('value_type', 'str') or 'str'),
            source=str(m.get('source', 'scanner') or 'scanner'),
        ))

    # 15. Convert cgdb_includes → IncludeRecord (L9 #include graph)
    seen_inc_keys = {
        (i.source_file_id, i.included_path)
        for i in batch.includes
    }
    for inc in scan_result.get('cgdb_includes', []):
        try:
            source_file_id = int(inc['source_file_id'])
            included_path = str(inc.get('included_path', '') or '')
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        if not included_path:
            continue
        included_file_id = inc.get('included_file_id')
        try:
            included_file_id = int(included_file_id) if included_file_id is not None else None
        except (ValueError, TypeError):
            included_file_id = None
        is_system = bool(inc.get('is_system', False))
        ikey = (source_file_id, included_path)
        if ikey in seen_inc_keys:
            continue
        seen_inc_keys.add(ikey)
        batch.includes.append(IncludeRecord(
            source_file_id=source_file_id,
            included_file_id=included_file_id,
            included_path=included_path,
            is_system=is_system,
        ))

    # 5. Synthesize from legacy functions/edges if cgdb_* lists were empty
    #    (e.g., tree-sitter-only fallback path).
    if not scan_result.get('cgdb_nodes'):
        _synthesize_from_legacy(scan_result, batch, fid, commit_hash, version_id)

    return batch


def _language_for(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    if ext in ('.c',):
        return 'c'
    if ext in ('.cpp', '.cc', '.cxx', '.c++'):
        return 'cpp'
    if ext in ('.h',):
        return 'c'
    if ext in ('.hpp', '.hh', '.hxx'):
        return 'cpp'
    if ext in ('.go',):
        return 'go'
    if ext in ('.rs',):
        return 'rust'
    if ext in ('.py',):
        return 'python'
    if ext in ('.java',):
        return 'java'
    if ext in ('.s', '.S', '.asm'):
        return 'asm'
    return 'c'


def _language_for_filename(filepath: str) -> str:
    """Alias for _language_for; kept for clarity at call sites."""
    return _language_for(filepath)


def _synthesize_from_legacy(scan_result: dict, batch: IngestBatch,
                             file_id: int, commit_hash: str, version_id: int):
    """When clang didn't run, synthesize NodeRecord/EdgeRecord from the
    tree-sitter legacy functions/edges so the cgdb tables still get populated.

    Node IDs use the cross-language unified_node_id scheme (language prefix
    + FQN + signature hash), so they're stable across runs and don't
    collide with clang's USR-based IDs (different language prefix).
    """
    try:
        from _scanner.unified_id import unified_node_id, unified_edge_id
    except ImportError:
        # Fallback: simple md5 hash (legacy behavior)
        def unified_node_id(language, fqn, signature="", byte_offset=None):
            return int(hashlib.md5(
                f"{language}|{fqn}|{signature}".encode()
            ).hexdigest()[:15], 16) & 0x0FFF_FFFF_FFFF_FFFF
        def unified_edge_id(src_id, dst_id, kind, line=None):
            return int(hashlib.md5(
                f"{src_id}|{dst_id}|{kind}|{line}".encode()
            ).hexdigest()[:15], 16) & 0x0FFF_FFFF_FFFF_FFFF

    filepath = scan_result.get('file', '') or ''
    language = _language_for(filepath)
    existing_node_ids = {n.id for n in batch.nodes}

    for fn in scan_result.get('functions', []):
        fn_id_str = fn.get('id', '') or fn.get('name', '')
        if not fn_id_str:
            continue
        signature = fn.get('signature', '') or ''
        nid = unified_node_id(language, fn_id_str)
        if nid in existing_node_ids:
            continue
        existing_node_ids.add(nid)
        body_text = fn.get('body_text', '') or ''
        attrs = {}
        if signature:
            attrs['signature'] = signature
        if body_text:
            attrs['body_text'] = body_text
        labels = fn.get('labels', []) or []
        if labels:
            attrs['labels'] = labels
        batch.nodes.append(NodeRecord(
            id=nid,
            kind='function',
            name=fn.get('name', '') or '',
            fqn=fn_id_str,
            file_id=file_id,
            line=int(fn.get('line_number', 0) or fn.get('line', 0) or 0),
            col=0,
            byte_start=int(fn.get('start_byte', 0) or fn.get('byte_start', 0) or 0),
            byte_end=int(fn.get('end_byte', 0) or fn.get('byte_end', 0) or 0),
            type_spelling=fn.get('return_type', '') or fn.get('type_spelling', '') or '',
            attrs=attrs,
            source_layer='legacy',
            confidence=0.9,
            commit_hash=commit_hash or None,
            first_seen_version=version_id,
            last_seen_version=version_id,
            legacy_function_id=fn_id_str,
        ))
    for e in scan_result.get('edges', []):
        caller = e.get('invoker_id', '') or e.get('invoker', '') or e.get('invoker_id', '') or e.get('caller', '')
        callee = e.get('invoked_id', '') or e.get('invoked', '') or e.get('invoked_id', '') or e.get('callee', '') or e.get('target', '')
        if not caller or not callee:
            continue
        caller_nid = unified_node_id(language, caller)
        callee_nid = unified_node_id(language, callee)
        # Ensure both endpoints exist as nodes — edges frequently reference
        # external/builtin callees that have no definition in this scan.
        for nid, fqn in ((caller_nid, caller), (callee_nid, callee)):
            if nid in existing_node_ids:
                continue
            existing_node_ids.add(nid)
            short_name = fqn.split('.')[-1] if '.' in fqn else fqn
            batch.nodes.append(NodeRecord(
                id=nid,
                kind='function',
                name=short_name,
                fqn=fqn,
                file_id=file_id,
                line=0, col=0,
                byte_start=0, byte_end=0,
                attrs={'external': True},
                source_layer='legacy',
                confidence=0.5,
                commit_hash=commit_hash or None,
                first_seen_version=version_id,
                last_seen_version=version_id,
                legacy_function_id=fqn,
            ))
        # Edge kind: most legacy edges are INVOKES
        kind = 'INVOKES'
        if e.get('relation') == 'OPS_BIND':
            kind = 'OPS_BIND'
        elif e.get('relation') == 'READS':
            kind = 'READS'
        elif e.get('relation') == 'WRITES':
            kind = 'WRITES'
        elif e.get('relation') == 'IMPLEMENTS':
            kind = 'IMPLEMENTS'
        elif e.get('relation') == 'IMPORTS':
            kind = 'IMPORTS'
        line = int(e.get('line', 0) or 0) if e.get('line') else None
        edge_id = unified_edge_id(caller_nid, callee_nid, kind, line)
        batch.edges.append(EdgeRecord(
            edge_id=edge_id,
            src_id=caller_nid,
            dst_id=callee_nid,
            kind=kind,
            file_id=file_id,
            line=line,
            attrs={},
            commit_hash=commit_hash or None,
            first_seen_version=version_id,
            last_seen_version=version_id,
        ))
