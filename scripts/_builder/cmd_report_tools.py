"""cmd_report_tools — CLI wrappers for the 13 design-report write-back/L1 commands.

These commands wrap the MCP tool handlers in mcp_report_tools.py so they
can be invoked from the code2database_builder CLI. Each cmd_* function
takes argparse args, builds a dict matching the MCP tool's inputSchema,
calls the underlying _tool_* handler, and prints JSON to stdout.

Implements the 13 commands listed in Code2Database-最终差距分析与优化报告.md
(RPT-P0-13):

  L1 写回与一致性 (5):
    - render-source        (file_id)
    - verify-consistency   (file_id)
    - edit-token           (token_id, new_text)
    - insert-token         (after_token_id, tokens JSON)
    - delete-token         (token_id)

  L1 信息查询 (3):
    - find-macros          (name?)
    - get-pp-branches      (file_id)
    - get-string-literals  (pattern?)

  事务化写回 (2):
    - commit-db-transaction       (transaction_id, run_compile, run_lint,
                                   run_clang_format, git_commit, commit_message)
    - rollback-db-transaction     (transaction_id)

  高级语义编辑 (3):
    - insert-node-after    (ast_node_id, node_spec JSON)
    - delete-node          (ast_node_id)
    - add-function         (signature, body_tokens JSON)

Convention: <graph_dir>/code2database.db (same as MCP server and cgdb_commands).
"""
import json
import os
from typing import Any

from _builder.mcp_report_tools import (
    _tool_render_source,
    _tool_verify_consistency,
    _tool_edit_token,
    _tool_insert_token,
    _tool_delete_token,
    _tool_find_macros,
    _tool_get_pp_branches,
    _tool_get_string_literals,
    _tool_commit_db_transaction,
    _tool_rollback_db_transaction,
    _tool_insert_node_after,
    _tool_delete_node,
    _tool_add_function,
)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _graph_or_cwd(args) -> str:
    """Return graph_dir; warn if --graph not given."""
    _g = getattr(args, "graph", None)
    if not _g:
        print("[warning] --graph not specified; using current directory",
              file=sys.stderr)
        return os.getcwd()
    return _g


# ---------------------------------------------------------------------------
# L1 写回与一致性 (5)
# ---------------------------------------------------------------------------

def cmd_render_source(args):
    """Render source code from DB tokens for a file_id."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_render_source({"file_id": args.file_id}, graph_dir)
    _print_json(result)


def cmd_verify_consistency(args):
    """Verify DB-token-rendered source sha256 matches disk file sha256."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_verify_consistency({"file_id": args.file_id}, graph_dir)
    _print_json(result)


def cmd_edit_token(args):
    """Edit a single token's spelling by token_id (DB only — commit to write)."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_edit_token(
        {"token_id": args.token_id, "new_text": args.new_text},
        graph_dir,
    )
    _print_json(result)


def cmd_insert_token(args):
    """Insert tokens after a given anchor token_id (DB only — commit to write)."""
    graph_dir = _graph_or_cwd(args)
    tokens = json.loads(args.tokens_json) if args.tokens_json else []
    result = _tool_insert_token(
        {"after_token_id": args.after_token_id, "tokens": tokens},
        graph_dir,
    )
    _print_json(result)


def cmd_delete_token(args):
    """Delete a token by token_id (DB only — commit to write)."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_delete_token({"token_id": args.token_id}, graph_dir)
    _print_json(result)


# ---------------------------------------------------------------------------
# L1 信息查询 (3)
# ---------------------------------------------------------------------------

def cmd_find_macros(args):
    """Find macros by name (or list all if no name)."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_find_macros({"name": args.name}, graph_dir)
    _print_json(result)


def cmd_get_pp_branches(args):
    """Get #if/#ifdef/#elif branches for a file_id."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_get_pp_branches({"file_id": args.file_id}, graph_dir)
    _print_json(result)


def cmd_get_string_literals(args):
    """Find string literals matching a regex pattern."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_get_string_literals({"pattern": args.pattern}, graph_dir)
    _print_json(result)


# ---------------------------------------------------------------------------
# 事务化写回 (2)
# ---------------------------------------------------------------------------

def cmd_commit_db_transaction(args):
    """Commit a pending DB transaction: render + clang-format + compile + lint
    + sha256 verify + write to disk + (optional) git commit. Rolls back on
    any failure."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_commit_db_transaction(
        {
            "transaction_id": args.transaction_id,
            "run_compile": args.run_compile,
            "run_lint": args.run_lint,
            "run_clang_format": args.run_clang_format,
            "git_commit": args.git_commit,
            "commit_message": args.commit_message,
        },
        graph_dir,
    )
    _print_json(result)


def cmd_rollback_db_transaction(args):
    """Roll back a pending DB transaction (undo DB-side edits)."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_rollback_db_transaction(
        {"transaction_id": args.transaction_id},
        graph_dir,
    )
    _print_json(result)


# ---------------------------------------------------------------------------
# 高级语义编辑 (3)
# ---------------------------------------------------------------------------

def cmd_insert_node_after(args):
    """Insert a new AST node after the given anchor ast_node_id."""
    graph_dir = _graph_or_cwd(args)
    node_spec = json.loads(args.node_spec_json) if args.node_spec_json else {}
    result = _tool_insert_node_after(
        {"ast_node_id": args.ast_node_id, "node_spec": node_spec},
        graph_dir,
    )
    _print_json(result)


def cmd_delete_node(args):
    """Delete an AST node by ast_node_id (DB only — commit to write)."""
    graph_dir = _graph_or_cwd(args)
    result = _tool_delete_node({"ast_node_id": args.ast_node_id}, graph_dir)
    _print_json(result)


def cmd_add_function(args):
    """Add a new function node with the given signature and body tokens."""
    graph_dir = _graph_or_cwd(args)
    body_tokens = json.loads(args.body_tokens_json) if args.body_tokens_json else []
    result = _tool_add_function(
        {"signature": args.signature, "body_tokens": body_tokens},
        graph_dir,
    )
    _print_json(result)
