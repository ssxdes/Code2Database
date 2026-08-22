#!/usr/bin/env python3
"""Lightweight offline graph patcher — no LLM needed.

Patches the invocation graph based on code changes detected via:
1. Unified diff text (patch-from-diff)
2. Git diff (patch-from-git)
3. Light scan of changed files (light-scan)

All operations are script-based and require zero LLM tokens.
New/modified nodes are created with minimal attributes (skeleton nodes)
and marked as stale. Semantic descriptions are filled later when
LLM encounters them (lazy-fill via describe-node --lazy-fill).

Can be used standalone or via code2database_builder.py commands:
  patch-from-diff, patch-from-git, light-scan
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from _builder.graph_build import _load_full_graph, split_by_domain
from _builder.utils import _normalize_id, _detect_language_from_path, _resolve_invoked_id, _build_suffix_index


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

def _parse_unified_diff(diff_text: str) -> dict:
    """Parse unified diff into structured change records.

    Returns: {
        "added_files": [path, ...],
        "modified_files": [path, ...],
        "deleted_files": [path, ...],
        "hunks": [{"file": path, "added_lines": [n,...], "removed_lines": [n,...]}]
    }
    """
    result = {
        "added_files": [],
        "modified_files": [],
        "deleted_files": [],
        "hunks": [],
    }

    current_file = None
    pending_old_file = None
    current_hunk = None
    added_lines = []
    removed_lines = []
    old_line = 0
    new_line = 0

    for line in diff_text.split("\n"):
        # File headers
        if line.startswith("+++ /dev/null"):
            # File deleted — use the pending old file name
            if pending_old_file:
                result["deleted_files"].append(pending_old_file)
            current_file = None
            pending_old_file = None
            continue
        if line.startswith("--- /dev/null"):
            # New file
            current_file = "NEW"
            pending_old_file = None
            continue
        if line.startswith("--- a/"):
            # Old file path — save but don't set current_file yet
            pending_old_file = line[6:].strip()
            continue
        if line.startswith("--- "):
            pending_old_file = line[4:].strip()
            continue
        if line.startswith("+++ b/"):
            fname = line[6:].strip()
            if current_file == "NEW":
                result["added_files"].append(fname)
                current_file = fname
            else:
                current_file = fname
                if fname not in result["deleted_files"]:
                    result["modified_files"].append(fname)
            pending_old_file = None
            continue

        # Hunk headers: @@ -old_start,old_count +new_start,new_count @@
        hunk_match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
        if hunk_match:
            # Save previous hunk
            if current_hunk and (added_lines or removed_lines):
                result["hunks"].append(current_hunk)

            old_line = int(hunk_match.group(1))
            new_line = int(hunk_match.group(3))
            current_hunk = {
                "file": current_file,
                "added_lines": [],
                "removed_lines": [],
            }
            added_lines = current_hunk["added_lines"]
            removed_lines = current_hunk["removed_lines"]
            continue

        # Diff content
        if current_hunk is None:
            continue
        if line.startswith("+"):
            added_lines.append(new_line)
            new_line += 1
        elif line.startswith("-"):
            removed_lines.append(old_line)
            old_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1

    # Save last hunk
    if current_hunk and (added_lines or removed_lines):
        result["hunks"].append(current_hunk)

    return result


# ---------------------------------------------------------------------------
# Graph patching
# ---------------------------------------------------------------------------



def patch_from_diff(graph_dir: str, diff_text: str, source_root: str = ""):
    """Patch graph based on unified diff text.

    - Added functions → add skeleton nodes (stale=True, semantic_desc="")
    - Modified functions → mark as stale, update line numbers
    - Deleted functions → remove nodes and connected edges
    """
    changes = _parse_unified_diff(diff_text)

    # Load current graph
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if not os.path.exists(master_path):
        print("Error: No graph found at " + graph_dir, file=sys.stderr)
        return

    G = _load_full_graph(graph_dir)

    # Track changes
    added_nodes = 0
    stale_nodes = 0
    removed_nodes = 0

    # Process deleted files: remove all nodes from that file
    for filepath in changes["deleted_files"]:
        rel = os.path.relpath(filepath, source_root) if source_root else filepath
        to_remove = [nid for nid, nd in G.nodes(data=True)
                     if nd.get("source_file", "") == rel or
                     nd.get("source_file", "") == filepath]
        for nid in to_remove:
            G.remove_node(nid)
            removed_nodes += 1

    # Process added files: create file-level skeleton nodes for tracking
    # (actual function nodes require AST scanning via light-scan)
    for filepath in changes["added_files"]:
        lang = _detect_language_from_path(filepath)
        if not lang:
            continue
        # Create a placeholder node for the new file so the graph knows
        # it exists. Real function nodes should be added via light-scan.
        rel = os.path.relpath(filepath, source_root) if source_root else filepath
        fid = _normalize_id(f"root_{Path(filepath).stem}")
        if fid not in G:
            G.add_node(fid,
                       name=Path(filepath).stem,
                       source_file=rel,
                       line=0,
                       domain="root",
                       labels=["file"],
                       labels_source={},
                       is_empty=True,
                       condition="",
                       stale=True,
                       semantic_desc="",
                       body_text="",
                       signature="",
                       params=[],
                       local_vars=[],
                       callee_args=[],
                       condition_vars=[],
                       preproc_alive=True)
            added_nodes += 1

    # Process hunks: mark affected nodes as stale
    for hunk in changes["hunks"]:
        filepath = hunk["file"]
        if not filepath:
            continue

        rel = os.path.relpath(filepath, source_root) if source_root else filepath

        # Find nodes from this file
        for nid, nd in G.nodes(data=True):
            if nd.get("source_file", "") not in (rel, filepath):
                continue

            node_line = nd.get("line", 0)
            if node_line == 0:
                continue

            # Check if node line is in removed lines (function modified)
            for rm_line in hunk["removed_lines"]:
                if abs(node_line - rm_line) <= 5:  # within 5 lines = likely affected
                    nd["stale"] = True
                    nd["semantic_desc"] = ""
                    stale_nodes += 1
                    break

            # Check if node line is near added lines (new code nearby)
            for add_line in hunk["added_lines"]:
                if abs(node_line - add_line) <= 2:
                    nd["stale"] = True
                    stale_nodes += 1
                    break

    # Rebuild domain files
    if added_nodes > 0 or stale_nodes > 0 or removed_nodes > 0:
        split_by_domain(G, graph_dir, source_root)
        print(f"Patched: +{added_nodes} added, ~{stale_nodes} stale, -{removed_nodes} removed")
        # Invalidate query cache for changed nodes
        try:
            from _builder.query_cache import invalidate_node, invalidate_all
            for nid in list(getattr(G, "_patched_nodes", set()) or []):
                invalidate_node(graph_dir, nid)
            # If any nodes were removed or added, clear the whole cache
            # (the touched-set tracking only covers modified nodes)
            if added_nodes > 0 or removed_nodes > 0:
                invalidate_all(graph_dir)
        except Exception:
            pass
        # Audit log: record this patch operation
        try:
            from _builder.audit_log import log_audit, new_tx_id
            tx_id = new_tx_id()
            for nid in list(getattr(G, "_patched_nodes", set()) or []):
                log_audit(graph_dir,
                          command="patch-from-diff",
                          target_kind="node",
                          target_id=nid,
                          action="update",
                          reason=f"diff-patch (added={added_nodes}, stale={stale_nodes}, removed={removed_nodes})",
                          tx_id=tx_id)
            if added_nodes > 0:
                log_audit(graph_dir,
                          command="patch-from-diff",
                          target_kind="graph",
                          target_id="*",
                          action="insert",
                          attribute="added_nodes",
                          after_value=added_nodes,
                          reason=f"diff-patch added {added_nodes} node(s)",
                          tx_id=tx_id)
            if removed_nodes > 0:
                log_audit(graph_dir,
                          command="patch-from-diff",
                          target_kind="graph",
                          target_id="*",
                          action="delete",
                          attribute="removed_nodes",
                          after_value=removed_nodes,
                          reason=f"diff-patch removed {removed_nodes} node(s)",
                          tx_id=tx_id)
        except Exception:
            pass
    else:
        print("No graph changes needed from this diff")


def patch_from_git(graph_dir: str, source_root: str, commit_range: str = None):
    """Patch graph using git diff.

    commit_range: e.g. 'HEAD~3', 'HEAD~1..HEAD', 'abc123..def456'
    Default: all uncommitted changes + last commit
    """
    if not os.path.exists(os.path.join(source_root, ".git")):
        print(f"Error: {source_root} is not a git repository", file=sys.stderr)
        return

    # Build git diff command
    if commit_range:
        cmd = ["git", "-C", source_root, "diff", commit_range]
    else:
        # Uncommitted changes (staged + unstaged) + last commit
        # First get uncommitted (working tree vs HEAD)
        cmd = ["git", "-C", source_root, "diff", "HEAD"]

    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        # Fallback: just staged/unstaged
        cmd = ["git", "-C", source_root, "diff"]
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    if not result.stdout:
        print("No changes detected in git diff")
        return

    patch_from_diff(graph_dir, result.stdout, source_root)


# ---------------------------------------------------------------------------
# Light scan
# ---------------------------------------------------------------------------

def light_scan(source_root: str, graph_dir: str, changed_files: list = None):
    """Run lightweight AST scan on changed files only.

    Faster than full scan because:
    - Only scans changed files (no walk)
    - Skips semantic extraction
    - Produces skeleton nodes with minimal attributes
    - Merges into existing graph without rebuilding
    """
    from _builder.graph_build import _load_full_graph, split_by_domain

    # Detect changed files if not provided
    if not changed_files:
        # Try git
        if os.path.exists(os.path.join(source_root, ".git")):
            result = subprocess.run(
                ["git", "-C", source_root, "diff", "--name-only"],
                capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if result.returncode == 0 and result.stdout:
                changed_files = [os.path.join(source_root, f) for f in result.stdout.strip().split("\n") if f]
            else:
                changed_files = []
        else:
            print("No changed files specified and not a git repo. Use --files or run from git repo.")
            return

    if not changed_files:
        print("No changed files to scan")
        return

    # Load current graph
    G = _load_full_graph(graph_dir)

    # Scan each changed file
    added = 0
    stale = 0

    for fpath in changed_files:
        if not os.path.exists(fpath):
            continue

        lang = _detect_language_from_path(fpath)
        if not lang:
            continue

        # Try to import the appropriate scanner
        try:
            if lang in ("c", "cpp"):
                from _scanner.c_scanner import CTreeSitterScanner
                scanner = CTreeSitterScanner(is_cpp=(lang == "cpp"))
            elif lang == "go":
                from _scanner.go_scanner import GoTreeSitterScanner
                scanner = GoTreeSitterScanner()
            elif lang == "python":
                from _scanner.python_scanner import PythonTreeSitterScanner
                scanner = PythonTreeSitterScanner()
            elif lang == "java":
                from _scanner.java_scanner import JavaTreeSitterScanner
                scanner = JavaTreeSitterScanner()
            elif lang == "rust":
                from _scanner.rust_scanner import RustTreeSitterScanner
                scanner = RustTreeSitterScanner()
            else:
                continue
        except ImportError:
            # Fallback: mark existing nodes from this file as stale
            rel = os.path.relpath(fpath, source_root)
            for nid, nd in G.nodes(data=True):
                if nd.get("source_file", "") == rel:
                    nd["stale"] = True
                    stale += 1
            continue

        # Scan the file
        result = scanner.scan_file(fpath, source_root)

        # Add new nodes as skeleton (stale=True)
        rel = os.path.relpath(fpath, source_root)
        existing_name_files = {(G.nodes[nid].get("name", ""),
                                G.nodes[nid].get("source_file", ""))
                               for nid in G.nodes()
                               if not G.nodes[nid].get("is_empty", False)}

        for func in result.get("functions", []):
            name = func.get("name", "")
            key = (name, rel)
            if key in existing_name_files:
                # Update existing node - mark stale
                for nid, nd in G.nodes(data=True):
                    if nd.get("name", "") == name and nd.get("source_file", "") == rel:
                        nd["stale"] = True
                        nd["line"] = func.get("line", nd.get("line", 0))
                        stale += 1
                        break
            else:
                # New function - add skeleton node
                fid = func.get("id", _normalize_id(f"{func.get('domain', 'root')}_{name}"))
                G.add_node(fid,
                           name=name,
                           source_file=rel,
                           line=func.get("line", 0),
                           domain=func.get("domain", "root"),
                           labels=func.get("labels", []),
                           labels_source=func.get("labels_source", {}),
                           is_empty=False,
                           condition="",
                           stale=True,
                           semantic_desc="",
                           body_text="",
                           signature=func.get("signature", ""),
                           params=func.get("params", []),
                           local_vars=[],
                           callee_args=[],
                           condition_vars=[],
                           preproc_alive=True)
                added += 1

        # Pre-build id_registry and suffix_index once (not per-edge)
        _patch_id_registry = {n: G.nodes[n] for n in G.nodes}
        _patch_suffix_index = _build_suffix_index(_patch_id_registry)

        # Add new edges
        for edge in result.get("edges", []):
            source_id = edge.get("source", "")
            target_name = edge.get("target", "")
            if not source_id or not target_name:
                continue
            # Resolve target to full node ID using suffix index
            source_domain = ""
            if source_id in G:
                source_domain = G.nodes[source_id].get("domain", "root")
            target_id = _resolve_invoked_id(target_name, source_domain,
                                           _patch_id_registry,
                                           suffix_index=_patch_suffix_index)
            if not target_id:
                target_id = _normalize_id(target_name)
            # Check if edge already exists
            if source_id in G and not G.has_edge(source_id, target_id):
                G.add_edge(source_id, target_id,
                           call_order=edge.get("call_order"),
                           call_condition=edge.get("call_condition", ""),
                           concurrency=edge.get("concurrency", ""),
                           confidence="EXTRACTED",
                           source=edge.get("source_tag") or "patch",
                           confidence_score=1.0,
                           source_tag="patch")

    # Rebuild if there were changes
    if added > 0 or stale > 0:
        split_by_domain(G, graph_dir, source_root)
        print(f"Light scan: +{added} new, ~{stale} stale")
    else:
        print("No changes from light scan")


# ---------------------------------------------------------------------------
# Threshold-based update
# ---------------------------------------------------------------------------

def check_update_threshold(source_root: str, graph_dir: str,
                           semantic_threshold: float = 0.15) -> dict:
    """Check if changes warrant a full semantic update or light update.

    Returns: {
        "change_ratio": float,
        "new_files": int, "changed_files": int, "deleted_files": int,
        "unchanged_count": int,
        "needs_semantic_update": bool
    }
    """
    # Use the scanner's detect_changes
    from _scanner.changes import detect_changes

    changes = detect_changes(source_root, graph_dir)

    total = changes["unchanged_count"] + len(changes["new_files"]) + len(changes["changed_files"])
    if total == 0:
        return {
            "change_ratio": 0.0,
            "new_files": 0, "changed_files": 0, "deleted_files": len(changes["deleted_files"]),
            "unchanged_count": 0,
            "needs_semantic_update": False,
        }

    change_ratio = (len(changes["new_files"]) + len(changes["changed_files"])) / total

    return {
        "change_ratio": change_ratio,
        "new_files": len(changes["new_files"]),
        "changed_files": len(changes["changed_files"]),
        "deleted_files": len(changes["deleted_files"]),
        "unchanged_count": changes["unchanged_count"],
        "needs_semantic_update": change_ratio >= semantic_threshold,
    }


# ---------------------------------------------------------------------------
# Lazy fill for describe-node
# ---------------------------------------------------------------------------

def lazy_fill_node(G, node_id: str, source_root: str) -> dict:
    """Fill in basic node info from source file for stale/empty nodes.

    Returns dict with extracted info. No LLM needed.
    """
    nd = G.nodes[node_id]
    if not nd.get("stale", False) and nd.get("semantic_desc", ""):
        return {}  # Nothing to fill

    source_file = nd.get("source_file", "")
    if not source_file:
        return {}

    full_path = os.path.join(source_root, source_file) if source_root else source_file
    if not os.path.exists(full_path):
        return {}

    # Read source file and extract basic info at the function's line
    try:
        with open(full_path, "rb") as f:
            source_bytes = f.read()
    except OSError:
        return {}

    line_num = nd.get("line", 0)
    if line_num == 0:
        return {}

    # Extract lines around the function definition
    lines = source_bytes.decode("utf-8", errors="replace").split("\n")
    if line_num > len(lines):
        return {}

    # Get the function body (rough: from definition line to next def or end)
    start = max(0, line_num - 1)
    end = min(len(lines), line_num + 50)  # Up to 50 lines
    for i in range(start + 1, min(len(lines), start + 200)):
        if re.match(r'^(def |class |void |int |static |public |private |fn |func )', lines[i]):
            end = i
            break

    body_text = "\n".join(lines[start:end])
    signature = lines[start].strip() if start < len(lines) else ""

    return {
        "body_text": body_text,
        "signature": signature,
        "stale": False,
        "source_file": source_file,
        "line": line_num,
    }


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def cmd_patch_from_diff(args):
    """Handle patch-from-diff command (transactional).

    Wraps the patch in a transaction: snapshot + WAL + write lock. If
    the patch fails mid-way, the snapshot is restored automatically.
    """
    diff_path = getattr(args, "diff_file", "")
    if diff_path and os.path.exists(diff_path):
        diff_text = Path(diff_path).read_text(encoding="utf-8")
    else:
        diff_text = sys.stdin.read()

    if not diff_text:
        print("No diff input provided. Use --diff-file or pipe diff text.")
        return

    # Wrap in a transaction so partial failures roll back cleanly.
    try:
        from _builder.transactions import transaction
    except ImportError:
        # transactions module unavailable — fall back to non-transactional
        patch_from_diff(args.graph, diff_text, getattr(args, "source", ""))
        return

    no_tx = getattr(args, "no_transaction", False)
    if no_tx:
        patch_from_diff(args.graph, diff_text, getattr(args, "source", ""))
        return

    try:
        with transaction(args.graph, description="patch-from-diff"):
            patch_from_diff(args.graph, diff_text, getattr(args, "source", ""))
        print("[tx] patch-from-diff committed", file=sys.stderr)
    except Exception as exc:
        print(f"[tx] patch-from-diff rolled back: {exc}", file=sys.stderr)
        raise


def cmd_patch_from_git(args):
    """Handle patch-from-git command (transactional)."""
    try:
        from _builder.transactions import transaction
    except ImportError:
        patch_from_git(args.graph, args.source, getattr(args, "commit_range", None))
        return

    no_tx = getattr(args, "no_transaction", False)
    if no_tx:
        patch_from_git(args.graph, args.source, getattr(args, "commit_range", None))
        return

    try:
        with transaction(args.graph, description="patch-from-git"):
            patch_from_git(args.graph, args.source, getattr(args, "commit_range", None))
        print("[tx] patch-from-git committed", file=sys.stderr)
    except Exception as exc:
        print(f"[tx] patch-from-git rolled back: {exc}", file=sys.stderr)
        raise


def cmd_light_scan(args):
    """Handle light-scan command."""
    changed_files = []
    files_str = getattr(args, "files", "")
    if files_str:
        changed_files = files_str.split(",")

    light_scan(args.source, args.graph, changed_files or None)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Graph patcher")
    sub = parser.add_subparsers(dest="command")

    p_diff = sub.add_parser("patch-from-diff")
    p_diff.add_argument("--graph", required=True)
    p_diff.add_argument("--source", default="")
    p_diff.add_argument("--diff-file", default="")

    p_git = sub.add_parser("patch-from-git")
    p_git.add_argument("--graph", required=True)
    p_git.add_argument("--source", required=True)
    p_git.add_argument("--commit-range", default=None)

    p_scan = sub.add_parser("light-scan")
    p_scan.add_argument("--source", required=True)
    p_scan.add_argument("--graph", required=True)
    p_scan.add_argument("--files", default="")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "patch-from-diff":
        cmd_patch_from_diff(args)
    elif args.command == "patch-from-git":
        cmd_patch_from_git(args)
    elif args.command == "light-scan":
        cmd_light_scan(args)
