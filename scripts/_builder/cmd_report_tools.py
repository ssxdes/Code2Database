"""CLI wrappers for the 13 report-layer MCP tools.

Each function takes argparse args, constructs the dict expected by the
corresponding _tool_* handler in mcp_report_tools.py, and prints JSON.
"""
import json
import os
import sys


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _graph_or_cwd(args):
    return getattr(args, 'graph', None) or os.getcwd()


def _get_file_id(args):
    fid = getattr(args, 'file_id', None)
    if fid is not None:
        return int(fid)
    name = getattr(args, 'name', None) or getattr(args, 'node', None)
    if name:
        db_path = os.path.join(_graph_or_cwd(args), 'code2database.db')
        if os.path.exists(db_path):
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT id FROM cgdb_files WHERE path LIKE ? LIMIT 1",
                    (f'%{name}%',)).fetchone()
                if row:
                    return row[0]
            finally:
                conn.close()
    return 0


def cmd_render_source(args):
    from _builder.mcp_report_tools import _tool_render_source
    graph_dir = _graph_or_cwd(args)
    file_id = _get_file_id(args)
    _print_json(_tool_render_source({'file_id': file_id}, graph_dir))


def cmd_verify_consistency(args):
    from _builder.mcp_report_tools import _tool_verify_consistency
    graph_dir = _graph_or_cwd(args)
    file_id = _get_file_id(args)
    _print_json(_tool_verify_consistency({'file_id': file_id}, graph_dir))


def cmd_edit_token(args):
    from _builder.mcp_report_tools import _tool_edit_token
    graph_dir = _graph_or_cwd(args)
    _print_json(_tool_edit_token({
        'token_id': int(getattr(args, 'token_id', 0)),
        'new_text': getattr(args, 'new_text', ''),
    }, graph_dir))


def cmd_insert_token(args):
    from _builder.mcp_report_tools import _tool_insert_token
    graph_dir = _graph_or_cwd(args)
    import json as _json
    tokens = []
    tokens_json = getattr(args, 'tokens_json', None)
    if tokens_json:
        try:
            tokens = _json.loads(tokens_json)
        except _json.JSONDecodeError:
            print(f"Error: invalid --tokens-json", file=sys.stderr)
            sys.exit(1)
    _print_json(_tool_insert_token({
        'after_token_id': int(getattr(args, 'after_token_id', 0)),
        'tokens': tokens,
    }, graph_dir))


def cmd_delete_token(args):
    from _builder.mcp_report_tools import _tool_delete_token
    graph_dir = _graph_or_cwd(args)
    _print_json(_tool_delete_token({
        'token_id': int(getattr(args, 'token_id', 0)),
    }, graph_dir))


def cmd_find_macros(args):
    from _builder.mcp_report_tools import _tool_find_macros
    graph_dir = _graph_or_cwd(args)
    _print_json(_tool_find_macros({
        'name': getattr(args, 'name', None) or '',
    }, graph_dir))


def cmd_get_pp_branches(args):
    from _builder.mcp_report_tools import _tool_get_pp_branches
    graph_dir = _graph_or_cwd(args)
    file_id = _get_file_id(args)
    _print_json(_tool_get_pp_branches({'file_id': file_id}, graph_dir))


def cmd_get_string_literals(args):
    from _builder.mcp_report_tools import _tool_get_string_literals
    graph_dir = _graph_or_cwd(args)
    _print_json(_tool_get_string_literals({
        'pattern': getattr(args, 'pattern', None) or '',
    }, graph_dir))


def cmd_commit_db_transaction(args):
    from _builder.writeback_pipeline import commit_db_transaction as _commit
    from _builder.writeback_pipeline import WritebackResult
    graph_dir = _graph_or_cwd(args)
    tx_id = getattr(args, 'transaction_id', '') or ''
    if not tx_id:
        print("Error: --transaction-id required", file=sys.stderr)
        sys.exit(1)
    import sqlite3
    db_path = os.path.join(graph_dir, 'code2database.db')
    if not os.path.exists(db_path):
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    try:
        result = _commit(
            conn, graph_dir, graph_dir, tx_id,
            run_compile=not getattr(args, 'no_compile', False),
            run_lint=getattr(args, 'run_lint', False),
            run_clang_format=getattr(args, 'run_clang_format', False),
            git_commit=getattr(args, 'git_commit', False),
            commit_message=getattr(args, 'commit_message', None),
        )
        _print_json(result.to_dict())
    finally:
        conn.close()


def cmd_rollback_db_transaction(args):
    from _builder.writeback_pipeline import rollback_db_transaction as _rollback
    graph_dir = _graph_or_cwd(args)
    tx_id = getattr(args, 'transaction_id', '') or ''
    if not tx_id:
        print("Error: --transaction-id required", file=sys.stderr)
        sys.exit(1)
    import sqlite3
    db_path = os.path.join(graph_dir, 'code2database.db')
    conn = sqlite3.connect(db_path)
    try:
        ok = _rollback(conn, graph_dir, tx_id)
        _print_json({'rolled_back': ok})
    finally:
        conn.close()


def cmd_insert_node_after(args):
    from _builder.mcp_report_tools import _tool_insert_node_after
    graph_dir = _graph_or_cwd(args)
    import json as _json
    node_spec = {}
    spec_json = getattr(args, 'node_spec_json', None)
    if spec_json:
        try:
            node_spec = _json.loads(spec_json)
        except _json.JSONDecodeError:
            print("Error: invalid --node-spec-json", file=sys.stderr)
            sys.exit(1)
    _print_json(_tool_insert_node_after({
        'ast_node_id': int(getattr(args, 'ast_node_id', 0)),
        'node_spec': node_spec,
    }, graph_dir))


def cmd_delete_node(args):
    from _builder.mcp_report_tools import _tool_delete_node
    graph_dir = _graph_or_cwd(args)
    _print_json(_tool_delete_node({
        'ast_node_id': int(getattr(args, 'ast_node_id', 0)),
    }, graph_dir))


def cmd_add_function(args):
    from _builder.mcp_report_tools import _tool_add_function
    graph_dir = _graph_or_cwd(args)
    import json as _json
    body_tokens = []
    body_json = getattr(args, 'body_tokens_json', None)
    if body_json:
        try:
            body_tokens = _json.loads(body_json)
        except _json.JSONDecodeError:
            pass
    _print_json(_tool_add_function({
        'signature': getattr(args, 'signature', ''),
        'body_tokens': body_tokens,
    }, graph_dir))
