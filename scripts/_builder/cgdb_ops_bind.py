"""cgdb_ops_bind — L7 typed vtable dispatch via clang type system.

Per cgdb-architecture-and-poc-report.md 5.4.1, the cgdb ops_bind mechanism
uses clang's type system (not name patterns) to detect ops tables:
  - Scan VarDecl whose type is a struct with function-pointer fields
  - Find InitListExpr children with `.field = function` designators
  - Record: ops_table_id, field_node_id (FieldDecl node),
    impl_function_id (FunctionDecl node), signature_match

This replaces the legacy tree-sitter heuristic (name-based `_ops`/`_operations`
suffix matching) with type-based detection — more precise, fewer false positives.

The libclang AST shape for `.field = function`:
  VAR_DECL 'my_fops' type='struct file_operations'
    INIT_LIST_EXPR
      UNEXPOSED_EXPR  ← one per field initializer
        MEMBER_REF 'read_iter' type='int (*)(void *, char *, int)'
        UNEXPOSED_EXPR 'my_read'
          DECL_REF_EXPR 'my_read' type='int (void *, char *, int)'

The OpsBindDeriver walks this structure and emits OpsBindingRecord entries
plus the corresponding cgdb_edges (OPS_BIND) for the IngestBatch.
"""
from typing import List, Optional, Tuple

from _builder.cgdb_records import (
    OpsBindingRecord, EdgeRecord, NodeRecord,
)


def _is_function_pointer_type(t) -> bool:
    """True if type is a pointer-to-function type."""
    if t is None:
        return False
    try:
        kind_name = t.kind.name if t.kind else ''
        if kind_name == 'POINTER':
            pointee = t.get_pointee()
            if pointee is None:
                return False
            pk = pointee.kind.name if pointee.kind else ''
            if pk in ('FUNCTIONPROTO', 'FUNCTIONNOPROTO'):
                return True
        return False
    except Exception:
        return False


def _struct_has_function_pointer_fields(cursor) -> bool:
    """True if the cursor is a struct/union with at least one function-pointer field."""
    try:
        kind_name = cursor.kind.name if cursor.kind else ''
        if kind_name not in ('STRUCT_DECL', 'UNION_DECL', 'CLASS_DECL'):
            return False
        for child in cursor.get_children():
            ck = child.kind.name if child.kind else ''
            if ck == 'FIELD_DECL':
                if _is_function_pointer_field(child):
                    return True
        return False
    except Exception:
        return False


def _is_function_pointer_field(cursor) -> bool:
    """True if the FIELD_DECL cursor's type is a function pointer."""
    try:
        if cursor.kind and cursor.kind.name != 'FIELD_DECL':
            return False
        return _is_function_pointer_type(cursor.type)
    except Exception:
        return False


def _find_field_decl_by_name(struct_cursor, field_name: str):
    """Find a FieldDecl child of struct_cursor by name. Returns cursor or None."""
    if not field_name:
        return None
    try:
        for child in struct_cursor.get_children():
            ck = child.kind.name if child.kind else ''
            if ck == 'FIELD_DECL' and child.spelling == field_name:
                return child
    except Exception:
        pass
    return None


def _find_function_decl_by_name(decl_ref_cursor):
    """Resolve a DECL_REF_EXPR to its FunctionDecl definition. Returns cursor or None."""
    try:
        # decl_ref.referenced gives the canonical declaration
        ref = decl_ref_cursor.referenced
        if ref is None:
            return None
        ck = ref.kind.name if ref.kind else ''
        if ck == 'FUNCTION_DECL':
            return ref
        return None
    except Exception:
        return None


def _signatures_match(field_type, func_type) -> bool:
    """Compare a function-pointer field type to a function type.

    Returns True if the function's signature matches the field's pointed-to
    function type. Uses clang's canonical type comparison.
    """
    try:
        if field_type is None or func_type is None:
            return False
        # Get the pointee of the field type (the function type the pointer points to)
        kind_name = field_type.kind.name if field_type.kind else ''
        if kind_name != 'POINTER':
            return False
        pointee = field_type.get_pointee()
        if pointee is None:
            return False
        # Compare canonical spellings — robust across typedefs
        try:
            pe_canon = pointee.get_canonical().spelling
        except Exception:
            pe_canon = pointee.spelling
        try:
            ft_canon = func_type.get_canonical().spelling
        except Exception:
            ft_canon = func_type.spelling
        return pe_canon == ft_canon
    except Exception:
        return False


def _walk_init_list_for_ops_binds(init_list_cursor, ops_table_id: int,
                                   struct_cursor, add_node_fn,
                                   cgdb_edges: list, ops_bindings: list,
                                   filepath: str, config_predicate_id=None,
                                   commit_hash=None, version_id=1) -> int:
    """Walk an INIT_LIST_EXPR, emit OPS_BIND edges + OpsBindingRecord for each
    `.field = function` designator.

    Returns the count of ops_bindings emitted.
    """
    count = 0
    try:
        for child in init_list_cursor.get_children():
            ck = child.kind.name if child.kind else ''
            # Each .field = function is wrapped in UNEXPOSED_EXPR
            if ck != 'UNEXPOSED_EXPR':
                continue
            # Find MEMBER_REF (field) and DECL_REF_EXPR (function) inside
            field_ref = None
            func_decl_ref = None
            try:
                for sub in child.get_children():
                    sk = sub.kind.name if sub.kind else ''
                    if sk == 'MEMBER_REF' and field_ref is None:
                        field_ref = sub
                    elif sk == 'UNEXPOSED_EXPR':
                        # The function reference is wrapped in another UNEXPOSED_EXPR
                        for inner in sub.get_children():
                            ik = inner.kind.name if inner.kind else ''
                            if ik == 'DECL_REF_EXPR' and func_decl_ref is None:
                                func_decl_ref = inner
                    elif sk == 'DECL_REF_EXPR' and func_decl_ref is None:
                        func_decl_ref = sub
            except Exception:
                continue
            if field_ref is None or func_decl_ref is None:
                continue
            # Resolve field_ref to its FieldDecl definition
            try:
                field_def = field_ref.referenced
                if field_def is None or (field_def.kind and
                                         field_def.kind.name != 'FIELD_DECL'):
                    field_def = _find_field_decl_by_name(
                        struct_cursor, field_ref.spelling or ''
                    )
            except Exception:
                field_def = _find_field_decl_by_name(
                    struct_cursor, field_ref.spelling or ''
                )
            if field_def is None:
                continue
            # Resolve function
            func_def = _find_function_decl_by_name(func_decl_ref)
            if func_def is None:
                continue
            # Add nodes for field and function (de-dup by add_node_fn)
            field_node_id = add_node_fn(field_def, 'field')
            impl_func_id = add_node_fn(func_def, 'function')
            if not field_node_id or not impl_func_id:
                continue
            # Compute signature_match
            sig_match = _signatures_match(field_ref.type, func_decl_ref.type)
            # Emit OPS_BIND edge (field_node → impl_function)
            edge_id = _make_edge_id(field_node_id, impl_func_id, 'OPS_BIND')
            cgdb_edges.append({
                'src_id': field_node_id,
                'dst_id': impl_func_id,
                'kind': 'OPS_BIND',
                'edge_id': edge_id,
                'file_path': filepath,
                'line': field_ref.location.line if field_ref.location else 0,
                'col': field_ref.location.column if field_ref.location else 0,
                'config_predicate_id': config_predicate_id,
                'attrs': {
                    'ops_table_id': ops_table_id,
                    'field_name': field_ref.spelling or '',
                    'signature_match': sig_match,
                },
            })
            ops_bindings.append({
                'edge_id': edge_id,
                'ops_table_id': ops_table_id,
                'field_node_id': field_node_id,
                'impl_function_id': impl_func_id,
                'field_name': field_ref.spelling or '',
                'signature_match': sig_match,
            })
            count += 1
    except Exception:
        pass
    return count


def _make_edge_id(src_id: int, dst_id: int, kind: str) -> int:
    """Stable 60-bit edge ID from (src_id, dst_id, kind).

    Uses SHA-256 of `src_id|dst_id|kind` to ensure the same edge has the
    same ID across runs (needed for ops_bindings.edge_id FK stability).
    """
    import hashlib
    h = hashlib.sha256(f"{src_id}|{dst_id}|{kind}".encode('utf-8')).hexdigest()[:15]
    return int(h, 16) & 0x0FFF_FFFF_FFFF_FFFF


class OpsBindDeriver:
    """Type-based ops_bind detector per cdb 5.4.1.

    Walks the AST looking for VarDecls whose type is a struct with
    function-pointer fields, then finds InitListExpr initializers with
    `.field = function` designators and emits OpsBindingRecord + OPS_BIND edges.
    """

    def __init__(self):
        pass

    def derive_from_tu(self, tu_cursor, add_node_fn, cgdb_edges: list,
                       ops_bindings: list, filepath: str,
                       config_predicate_id=None, commit_hash=None,
                       version_id: int = 1) -> int:
        """Walk the TU's top-level decls, emit ops_bindings.

        Returns the count of ops_bindings emitted.

        - tu_cursor: the TranslationUnit's root cursor
        - add_node_fn: callable(cursor, kind_override) → node_id (de-dups)
        - cgdb_edges: list to append OPS_BIND edge dicts to
        - ops_bindings: list to append ops_binding dicts to
        - filepath: source file path (for edge metadata)
        - config_predicate_id: optional, applied to emitted edges
        """
        count = 0
        try:
            for top in tu_cursor.get_children():
                tk = top.kind.name if top.kind else ''
                if tk != 'VAR_DECL':
                    continue
                # Check if VarDecl's type is a struct with function-pointer fields
                try:
                    var_type = top.type
                    if var_type is None:
                        continue
                    # Get the type declaration (the struct)
                    type_decl = var_type.get_declaration()
                    if type_decl is None:
                        continue
                    if not _struct_has_function_pointer_fields(type_decl):
                        continue
                except Exception:
                    continue
                # Find the INIT_LIST_EXPR child (the initializer)
                init_list = None
                try:
                    for child in top.get_children():
                        ck = child.kind.name if child.kind else ''
                        if ck == 'INIT_LIST_EXPR':
                            init_list = child
                            break
                except Exception:
                    pass
                if init_list is None:
                    continue
                # Add the VarDecl as a node (de-dup by add_node_fn)
                ops_table_id = add_node_fn(top, 'var')
                if not ops_table_id:
                    continue
                # Walk the init list for .field = function bindings
                count += _walk_init_list_for_ops_binds(
                    init_list, ops_table_id, type_decl, add_node_fn,
                    cgdb_edges, ops_bindings, filepath,
                    config_predicate_id=config_predicate_id,
                    commit_hash=commit_hash, version_id=version_id,
                )
        except Exception:
            pass
        return count

    def derive_call_candidates(self, tu_cursor, ops_bindings: list,
                                cgdb_edges: list, add_node_fn,
                                filepath: str,
                                config_predicate_id=None,
                                commit_hash=None,
                                version_id: int = 1) -> int:
        """Walk function bodies for `ops->field(...)` call sites and link
        them to the impl function via INVOKES edges.

        For each call site that invokes a function pointer through an
        ops_table's field, emits a INVOKES edge from the calling function
        to the impl function bound to that field. This makes the call
        graph include indirect dispatch edges, so queries like
        find_invokers(impl_function) return both direct callers and
        indirect callers via ops_table.

        Args:
          tu_cursor: the TranslationUnit's root cursor
          ops_bindings: list of existing OpsBindingRecord-like dicts
            (must contain ops_table_id, field_node_id, impl_function_id)
          cgdb_edges: list to append INVOKES edge dicts to
          add_node_fn: callable(cursor, kind_override) → node_id (de-dups)
          filepath: source file path
          config_predicate_id: optional, applied to emitted edges

        Returns the count of call-candidate edges emitted.
        """
        if not ops_bindings:
            return 0
        # Build a lookup table: (ops_table_id, field_name) → impl_function_id
        # We need field_name to match against MEMBER_REF_EXPR at call sites.
        field_to_impl = {}
        for ob in ops_bindings:
            try:
                ops_table_id = ob.get('ops_table_id')
                field_node_id = ob.get('field_node_id')
                impl_function_id = ob.get('impl_function_id')
                # Look up field name — ob should have it via the field_node
                # We need to query add_node_fn or accept it as a dict key.
                # For MVP, we use 'field_name' if present, else fall back
                # to field_node_id as the lookup key.
                field_name = ob.get('field_name', '')
                if ops_table_id and impl_function_id and field_name:
                    field_to_impl[(ops_table_id, field_name)] = impl_function_id
            except Exception:
                continue
        if not field_to_impl:
            return 0
        # Walk all function bodies for MEMBER_REF_EXPR that look like
        # `ops->field(...)` or `ops.field(...)`. The MEMBER_REF_EXPR's
        # parent should be a CALL_EXPR.
        count = 0
        try:
            # First, find all function definitions in this TU
            for top in tu_cursor.get_children():
                tk = top.kind.name if top.kind else ''
                if tk != 'FUNCTION_DECL':
                    continue
                # Only consider definitions (have a body)
                has_body = False
                for child in top.get_children():
                    if child.kind and child.kind.name == 'COMPOUND_STMT':
                        has_body = True
                        break
                if not has_body:
                    continue
                invoker_id = add_node_fn(top, 'function')
                if not invoker_id:
                    continue
                # Walk the function body looking for MEMBER_REF_EXPR
                for descendant in top.walk_preorder():
                    dk = descendant.kind.name if descendant.kind else ''
                    if dk != 'MEMBER_REF_EXPR':
                        continue
                    # Get the member name
                    member_name = descendant.spelling or ''
                    if not member_name:
                        continue
                    # Check parent is a CALL_EXPR
                    parent = descendant.semantic_parent
                    if parent is None or not parent.kind or parent.kind.name != 'CALL_EXPR':
                        continue
                    # Walk the MEMBER_REF_EXPR's children to find the base
                    # (DECL_REF_EXPR pointing to the ops_table var)
                    base_decl_ref = None
                    for sub in descendant.get_children():
                        if sub.kind and sub.kind.name == 'DECL_REF_EXPR':
                            base_decl_ref = sub
                            break
                    if base_decl_ref is None:
                        continue
                    # Resolve the base to a VarDecl
                    base_def = base_decl_ref.referenced
                    if base_def is None:
                        continue
                    base_id = add_node_fn(base_def, 'var')
                    if not base_id:
                        continue
                    # Look up the impl function for (base_id, member_name)
                    impl_id = field_to_impl.get((base_id, member_name))
                    if impl_id is None:
                        continue
                    # Emit a INVOKES edge from invoker_id → impl_id
                    try:
                        edge_id = _make_edge_id(invoker_id, impl_id, 'INVOKES')
                        cgdb_edges.append({
                            'id': edge_id,
                            'src_id': invoker_id,
                            'dst_id': impl_id,
                            'kind': 'INVOKES',
                            'file_path': filepath,
                            'line': descendant.location.line if descendant.location else 0,
                            'col': descendant.location.column if descendant.location else 0,
                            'config_predicate_id': config_predicate_id,
                            'commit_hash': commit_hash,
                            'first_seen_version': version_id,
                            'last_seen_version': version_id,
                            'is_indirect': True,  # indirect dispatch via ops_table
                            'via_ops_table_id': base_id,
                            'via_field_name': member_name,
                            'confidence': 'INFERRED',  # inferred via ops_bind
                            'source': 'ops_bind',
                        })
                        count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return count
