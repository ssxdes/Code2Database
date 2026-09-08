"""query.query_provenance — split from query.py."""

import os
import json
import sys
import re
import logging
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.utils import _is_condition_alive, _output_result, _find_node_id, _parse_bindings, _load_globals, _streaming_json_lookup, _streaming_json_has_keys
from _builder.graph.graph_build import _load_full_graph
from _builder.token_budget import estimate_tokens, truncate_to_tokens, budget_describe
from _builder.query.query_cache import cached_query, invalidate_node as _cache_invalidate_node



def cmd_describe_commit(args):
    """Describe which nodes/edges a commit affected.

    Usage: describe-commit --commit a1b2c3d4
    Returns: list of nodes changed by this commit, with diff summaries.

    Engineer question: "I just pulled commit a1b2c3d4 — what does the graph
    show as affected?" This replaces manually running `git show a1b2c3d4`
    then grepping the graph.
    """
    graph_dir = args.graph
    commit = args.commit
    if not commit:
        print("Error: --commit is required", file=sys.stderr)
        sys.exit(1)

    # Try SQL path first (query router)
    try:
        from _builder.query.query_router import _open_store
        store, _ = _open_store(graph_dir)
        if store is not None:
            try:
                rows = store.query_change_log_by_commit(commit)
                result = {
                    "commit": commit,
                    "affected_nodes": rows,
                    "_source": "sqlite",
                }
                _output_result(result, getattr(args, 'json', False))
                return
            finally:
                store.close()
    except Exception as exc:
        print(f"[describe-commit] SQL path failed, falling back: {exc}",
              file=sys.stderr)

    # Fallback: read change_log.json if it exists
    log_path = os.path.join(graph_dir, ".code2database_change_log.json")
    log_corrupt = False
    if os.path.exists(log_path):
        try:
            log_data = json.loads(Path(log_path).read_text(encoding="utf-8"))
            entries = [e for e in log_data
                       if e.get("commit_hash") == commit or e.get("commit_short") == commit]
            result = {"commit": commit, "affected_nodes": entries, "_source": "json"}
            _output_result(result, getattr(args, 'json', False))
            return
        except Exception as exc:
            # The log EXISTS but can't be read/parsed. Saying "run a
            # build with --track-commits" here would be a lie — the data
            # was tracked, the file is corrupt. Say so.
            logging.getLogger(__name__).debug(
                "change_log read failed: %s", exc, exc_info=True)
            log_corrupt = True
    if log_corrupt:
        print(f"Change log at {log_path} exists but could not be read "
              f"(corrupt or truncated). Delete it and re-run a build "
              f"with --track-commits to regenerate.", file=sys.stderr)
    else:
        print(f"No change log found for commit {commit}. "
              f"Run a build with --track-commits to populate change_log.",
              file=sys.stderr)
    sys.exit(1)



def cmd_node_history(args):
    """Show commit history for a node (introduced/modified through commits).

    Usage: node-history --node <node-id>
    Returns: chronological list of commits that touched this node.

    Engineer question: "This function broke — which commits recently changed
    it?" Replaces manually running `git log -- <file>` and trying to map
    commits to function changes.
    """
    graph_dir = args.graph
    node_id = args.node

    # Try SQL path
    try:
        from _builder.query.query_router import _open_store
        store, _ = _open_store(graph_dir)
        if store is not None:
            try:
                rows = store.query_change_log_by_node(node_id)
                result = {
                    "node": node_id,
                    "history": rows,
                    "_source": "sqlite",
                }
                _output_result(result, getattr(args, 'json', False))
                return
            finally:
                store.close()
    except Exception as exc:
        print(f"[node-history] SQL path failed, falling back: {exc}",
              file=sys.stderr)

    # Fallback: read change_log.json
    log_path = os.path.join(graph_dir, ".code2database_change_log.json")
    log_corrupt = False
    if os.path.exists(log_path):
        try:
            log_data = json.loads(Path(log_path).read_text(encoding="utf-8"))
            entries = [e for e in log_data if e.get("node_id") == node_id]
            # Sort by commit_date descending
            entries.sort(key=lambda e: e.get("commit_date", ""), reverse=True)
            result = {"node": node_id, "history": entries, "_source": "json"}
            _output_result(result, getattr(args, 'json', False))
            return
        except Exception as exc:
            # See cmd_describe_commit: a corrupt log must not be
            # reported as "no change log found".
            logging.getLogger(__name__).debug(
                "change_log read failed: %s", exc, exc_info=True)
            log_corrupt = True
    if log_corrupt:
        print(f"Change log at {log_path} exists but could not be read "
              f"(corrupt or truncated). Delete it and re-run a build "
              f"with --track-commits to regenerate.", file=sys.stderr)
    else:
        print(f"No change log found for node {node_id}.", file=sys.stderr)
    sys.exit(1)



def cmd_graph_provenance(args):
    """Show which commit the current graph corresponds to.

    Usage: graph-provenance
    Returns: the source_commit from .code2database_manifest.json.

    Engineer question: "Does this graph correspond to main HEAD or my
    feature branch?" This reads manifest.source_commit, not the database
    write time — engineers want the code commit, not the DB timestamp.
    """
    graph_dir = args.graph
    manifest_path = os.path.join(graph_dir, ".code2database_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"No manifest found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to read manifest: {exc}", file=sys.stderr)
        sys.exit(1)

    source_commit = manifest.get("source_commit")
    if not source_commit:
        print("Manifest has no source_commit. Re-run scan to populate.",
              file=sys.stderr)
        sys.exit(1)

    result = {
        "source_root": manifest.get("source_root"),
        "source_commit": source_commit,
        "file_count": len(manifest.get("files", {})),
        "build_timestamp": manifest.get("build_timestamp"),
        "schema_version": manifest.get("schema_version"),
    }
    _output_result(result, getattr(args, 'json', False))



def cmd_blame_node(args):
    """Attribute a node to its introducing/last-modifying commit.

    Usage: blame-node --node <node-id>
    Returns: commit_meta with introduced_commit, last_modified_commit.

    Engineer question: "Who wrote this function, and when?" — but in commit
    terms (git show <hash>), not vague timestamps.
    """
    graph_dir = args.graph
    node_id = args.node

    # Need the source_root to query git
    manifest_path = os.path.join(graph_dir, ".code2database_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"No manifest found at {manifest_path}", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    source_root = manifest.get("source_root", "")
    if not source_root or not os.path.isdir(source_root):
        print(f"source_root not found or invalid: {source_root}", file=sys.stderr)
        sys.exit(1)

    # Get the node's source file and line
    G = _load_full_graph(graph_dir)
    if node_id not in G:
        candidates = [n for n in G.nodes if node_id.lower() in n.lower()]
        if candidates:
            print(f"Node '{node_id}' not found. Similar: {candidates[:5]}",
                  file=sys.stderr)
        else:
            print(f"Node '{node_id}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    nd = G.nodes[node_id]
    source_file = nd.get("source_file", "")
    line = nd.get("line", 0)
    if not source_file:
        print(f"Node has no source_file attribute", file=sys.stderr)
        sys.exit(1)

    # Resolve absolute path
    if not os.path.isabs(source_file):
        source_file = os.path.join(source_root, source_file)

    try:
        from _builder.commit_meta import commit_meta_for_node
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        from _builder.commit_meta import commit_meta_for_node

    meta = commit_meta_for_node(source_root, node_id, source_file, line)
    result = {
        "node": node_id,
        "name": nd.get("name", ""),
        "source_file": source_file,
        "line": line,
        "commit_meta": meta,
    }
    _output_result(result, getattr(args, 'json', False))



def cmd_find_commits(args):
    """Find commits that recently modified a function or file.

    Usage: find-commits --function <name> [--since 2026-07-01] [--limit 20]
    Returns: commits in reverse-chronological order.

    Engineer question: "Show me the last N commits that touched this function."
    """
    graph_dir = args.graph
    func_name = getattr(args, "function", None) or getattr(args, "node", None)
    since = getattr(args, "since", "")
    limit = getattr(args, "limit", 20)

    if not func_name:
        print("Error: --function is required", file=sys.stderr)
        sys.exit(1)

    # Need source_root
    manifest_path = os.path.join(graph_dir, ".code2database_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"No manifest found at {manifest_path}", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    source_root = manifest.get("source_root", "")
    if not source_root or not os.path.isdir(source_root):
        print(f"source_root not found: {source_root}", file=sys.stderr)
        sys.exit(1)

    # Find the node to get its source file
    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, func_name)
    if not node_id:
        print(f"Function '{func_name}' not found in graph.", file=sys.stderr)
        sys.exit(1)

    nd = G.nodes[node_id]
    source_file = nd.get("source_file", "")
    if not source_file:
        print(f"Node has no source_file", file=sys.stderr)
        sys.exit(1)
    if not os.path.isabs(source_file):
        source_file = os.path.join(source_root, source_file)

    rel = os.path.relpath(source_file, source_root) if os.path.isabs(source_file) else source_file

    # Run git log
    import subprocess
    cmd_args = ["git", "-c", "core.pager=cat", "--no-pager", "log",
                f"--pretty=format:%H|%h|%an|%aI|%s", f"-{limit}"]
    if since:
        cmd_args.append(f"--since={since}")
    cmd_args += ["--", rel]

    try:
        result = subprocess.run(cmd_args, cwd=source_root, capture_output=True,
                                text=True, timeout=30, check=False)
        if result.returncode != 0:
            print(f"git log failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"git log error: {exc}", file=sys.stderr)
        sys.exit(1)

    commits = []
    for line in result.stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) >= 5:
            commits.append({
                "commit": parts[0], "commit_short": parts[1],
                "author": parts[2], "date": parts[3], "subject": parts[4],
            })

    out = {
        "function": func_name,
        "node_id": node_id,
        "source_file": rel,
        "commits": commits,
        "_source": "git",
    }
    _output_result(out, getattr(args, 'json', False))



