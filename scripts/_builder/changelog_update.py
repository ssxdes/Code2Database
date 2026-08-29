"""Changelog-based graph update — efficient updates without LLM.

Provides:
- quick_update: One-click patch + light-scan, no LLM needed
- export_change_graph: Read git/svn changelog, produce change graph JSON
- merge_change_graph: Apply change graph to existing invocation graph
- get_semantic_update_status: Check if semantic update is recommended
- cmd_quick_update, cmd_export_changes, cmd_merge_changes, cmd_semantic_status: CLI handlers
"""

import json
import os
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from _builder.graph_build import _load_full_graph, split_by_domain
from _builder.utils import _normalize_id, _detect_language_from_path, _resolve_invoked_id, _build_suffix_index
from _builder.index_pack import _mark_endpoint_nodes, _build_indexes


# ---------------------------------------------------------------------------
# VCS detection
# ---------------------------------------------------------------------------

def _detect_vcs(source_root: str) -> str:
    """Detect VCS type: 'git', 'svn', or ''."""
    if os.path.exists(os.path.join(source_root, ".git")):
        return "git"
    if os.path.exists(os.path.join(source_root, ".svn")):
        return "svn"
    return ""


def _git_changed_files(source_root: str, commit_range: str = None) -> list:
    """Get list of changed files from git."""
    if commit_range:
        cmd = ["git", "-C", source_root, "diff", "--name-only", commit_range]
    else:
        # Uncommitted changes + last commit
        cmd = ["git", "-C", source_root, "diff", "--name-only", "HEAD"]

    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        # Try staged/unstaged
        cmd = ["git", "-C", source_root, "diff", "--name-only"]
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    if result.returncode != 0 or not result.stdout:
        return []

    return [os.path.join(source_root, f.strip()) for f in result.stdout.strip().split("\n") if f.strip()]


def _svn_changed_files(source_root: str, revision_range: str = None) -> list:
    """Get list of changed files from SVN."""
    if revision_range:
        cmd = ["svn", "diff", "-r", revision_range, "--summarize", source_root]
    else:
        cmd = ["svn", "status", source_root]

    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        return []

    files = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        # SVN status format: "M       path/to/file"
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] in ("M", "A", "D"):
            fpath = parts[1]
            if not os.path.isabs(fpath):
                fpath = os.path.join(source_root, fpath)
            files.append(fpath)

    return files


# ---------------------------------------------------------------------------
# Quick update (one-click patch + light-scan)
# ---------------------------------------------------------------------------

def quick_update(source_root: str, graph_dir: str,
                 commit_range: str = None) -> dict:
    """One-click: detect changes -> patch graph -> light-scan new files.

    No LLM needed. Produces skeleton nodes marked stale for later lazy-fill.
    Returns change summary dict.
    """
    from _builder.patcher import patch_from_git, light_scan

    vcs = _detect_vcs(source_root)

    # Step 1: Patch existing graph from git diff
    if vcs == "git":
        patch_from_git(graph_dir, source_root, commit_range)
    elif vcs == "svn":
        # SVN: use changed files for light-scan
        pass

    # Step 2: Light-scan changed files for new functions
    if vcs == "git":
        changed_files = _git_changed_files(source_root, commit_range)
    elif vcs == "svn":
        changed_files = _svn_changed_files(source_root, commit_range)
    else:
        changed_files = []

    if changed_files:
        light_scan(source_root, graph_dir, changed_files)

    # Step 3: Check semantic update threshold
    from _builder.patcher import check_update_threshold
    threshold_info = check_update_threshold(source_root, graph_dir)

    # Step 4: Get stale node count from graph
    G = _load_full_graph(graph_dir)
    stale_nodes = [nid for nid, nd in G.nodes(data=True) if nd.get("stale", False)]

    summary = {
        "changed_files": len(changed_files),
        "stale_nodes": len(stale_nodes),
        "vcs": vcs,
        "threshold_info": threshold_info,
        "recommend_semantic_update": threshold_info.get("needs_semantic_update", False),
    }

    # Step 5: Write semantic status
    _write_semantic_status(graph_dir, stale_count=len(stale_nodes),
                           changed_files=len(changed_files))

    return summary


# ---------------------------------------------------------------------------
# Change graph export/merge
# ---------------------------------------------------------------------------



def export_change_graph(source_root: str, graph_dir: str,
                        commit_range: str = None,
                        output_path: str = None) -> str:
    """Read git/svn changelog, produce a change graph JSON.

    The change graph contains:
    - added_functions: [{id, name, source_file, line, signature, domain}]
    - removed_functions: [{id, name, source_file}]
    - modified_functions: [{id, name, source_file, line}]
    - added_edges: [{source, target}]
    - removed_edges: [{source, target}]

    All extracted via AST scan (no LLM).
    """
    vcs = _detect_vcs(source_root)

    if vcs == "git":
        changed_files = _git_changed_files(source_root, commit_range)
    elif vcs == "svn":
        changed_files = _svn_changed_files(source_root, commit_range)
    else:
        changed_files = []

    # Load current graph to get existing function names
    G = _load_full_graph(graph_dir)
    # Use (name, source_file) pairs — function names are NOT unique across files
    existing_name_files = {(G.nodes[nid].get("name", ""),
                            G.nodes[nid].get("source_file", "")): nid
                           for nid in G.nodes()
                           if not G.nodes[nid].get("is_empty", False)}

    change_graph = {
        "added_functions": [],
        "removed_functions": [],
        "modified_functions": [],
        "added_edges": [],
        "removed_edges": [],
        "source_root": source_root,
        "vcs": vcs,
        "changed_files": [os.path.relpath(f, source_root) for f in changed_files],
    }

    # Scan each changed file
    for fpath in changed_files:
        if not os.path.exists(fpath):
            # Deleted file — mark all functions from this file as removed
            rel = os.path.relpath(fpath, source_root)
            for nid, nd in G.nodes(data=True):
                if nd.get("source_file", "") == rel:
                    change_graph["removed_functions"].append({
                        "id": nid, "name": nd.get("name", ""), "source_file": rel,
                    })
            continue

        lang = _detect_language_from_path(fpath)
        if not lang:
            continue

        rel = os.path.relpath(fpath, source_root)

        # Mark existing functions from this file as modified
        for nid, nd in G.nodes(data=True):
            if nd.get("source_file", "") == rel:
                change_graph["modified_functions"].append({
                    "id": nid, "name": nd.get("name", ""),
                    "source_file": rel, "line": nd.get("line", 0),
                })

        # Try to scan the file for new functions
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

            result = scanner.scan_file(fpath, source_root)

            for func in result.get("functions", []):
                name = func.get("name", "")
                key = (name, rel)
                if key not in existing_name_files:
                    change_graph["added_functions"].append({
                        "id": func.get("id", _normalize_id(f"{func.get('domain', 'root')}_{name}")),
                        "name": name,
                        "source_file": rel,
                        "line": func.get("line", 0),
                        "signature": func.get("signature", ""),
                        "domain": func.get("domain", "root"),
                    })

            # Pre-build id_registry and suffix_index once for this file's edges
            _chg_id_registry = {n: G.nodes[n] for n in G.nodes}
            _chg_suffix_index = _build_suffix_index(_chg_id_registry)

            for edge in result.get("edges", []):
                source_id = edge.get("source", "")
                target_name = edge.get("target", "")
                source_domain = ""
                if source_id in G:
                    source_domain = G.nodes[source_id].get("domain", "root")
                target_id = _resolve_invoked_id(target_name, source_domain,
                                               _chg_id_registry,
                                               suffix_index=_chg_suffix_index)
                if not target_id:
                    target_id = _normalize_id(target_name)
                if not G.has_edge(source_id, target_id):
                    change_graph["added_edges"].append({
                        "source": source_id, "target": target_name,
                    })

        except ImportError:
            # Fallback: just mark as modified, no scan
            pass

    # Write output
    if not output_path:
        output_path = os.path.join(graph_dir, ".code2database_changes.json")

    # Atomic write: tmp + os.replace
    _tmp = output_path + ".tmp"
    Path(_tmp).write_text(
        json.dumps(change_graph, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(_tmp, output_path)

    return output_path


def merge_change_graph(graph_dir: str, change_graph_path: str,
                       source_root: str = "") -> dict:
    """Merge a change graph into the existing invocation graph.

    - Added functions -> add skeleton nodes (stale=True)
    - Removed functions -> remove nodes and connected edges
    - Modified functions -> mark stale, update line numbers
    - Added edges -> add edges
    """
    G = _load_full_graph(graph_dir)
    # merge_change_graph mutates the graph (G.nodes[fid]["stale"]=True,
    # G.nodes[fid]["semantic_desc"]="", G.add_node, G.add_edge, G.remove_node);
    # LazySQLiteGraph (read-only SQLite view for >=50K-function projects)
    # rejects item assignment. Detect early and exit with a clear error.
    from _builder.utils import _ensure_mutable_graph
    _ensure_mutable_graph(G, "merge-changes")
    changes = json.loads(Path(change_graph_path).read_text(encoding="utf-8"))

    added = 0
    removed = 0
    modified = 0
    edges_added = 0

    # Add new functions as skeleton nodes
    for func in changes.get("added_functions", []):
        fid = func.get("id", "")
        if fid and fid not in G:
            G.add_node(fid,
                       name=func.get("name", ""),
                       source_file=func.get("source_file", ""),
                       line=func.get("line", 0),
                       domain=func.get("domain", "root"),
                       labels=[], labels_source={},
                       is_empty=False, condition="",
                       stale=True, semantic_desc="",
                       body_text="", signature=func.get("signature", ""),
                       params=[], local_vars=[], callee_args=[],
                       condition_vars=[], preproc_alive=True)
            added += 1

    # Remove deleted functions
    for func in changes.get("removed_functions", []):
        fid = func.get("id", "")
        if fid and fid in G:
            G.remove_node(fid)
            removed += 1

    # Mark modified functions as stale
    for func in changes.get("modified_functions", []):
        fid = func.get("id", "")
        if fid and fid in G:
            G.nodes[fid]["stale"] = True
            G.nodes[fid]["semantic_desc"] = ""
            if func.get("line"):
                G.nodes[fid]["line"] = func["line"]
            modified += 1

    # Pre-build id_registry and suffix_index once for edge resolution
    _merge_id_registry = {n: G.nodes[n] for n in G.nodes}
    _merge_suffix_index = _build_suffix_index(_merge_id_registry)

    # Add new edges
    for edge in changes.get("added_edges", []):
        source_id = edge.get("source", "")
        target_name = edge.get("target", "")
        source_domain = ""
        if source_id in G:
            source_domain = G.nodes[source_id].get("domain", "root")
        target_id = _resolve_invoked_id(target_name, source_domain,
                                       _merge_id_registry,
                                       suffix_index=_merge_suffix_index)
        if not target_id:
            target_id = _normalize_id(target_name)
        # Only add edge if target_id exists in graph (avoid orphan nodes)
        if source_id in G and target_id in G and not G.has_edge(source_id, target_id):
            G.add_edge(source_id, target_id,
                       call_order=None, call_condition="",
                       concurrency="", confidence="EXTRACTED",
                       source="patch", confidence_score=1.0,
                       source_tag="patch")
            edges_added += 1

    # Rebuild if there were changes
    if added > 0 or removed > 0 or modified > 0 or edges_added > 0:
        if not source_root:
            master_path = os.path.join(graph_dir, "code2database_master.json")
            if os.path.exists(master_path):
                master = json.loads(Path(master_path).read_text(encoding="utf-8"))
                source_root = master.get("source_root", "")
        split_by_domain(G, graph_dir, source_root)
        _mark_endpoint_nodes(G, graph_dir)
        _build_indexes(G, graph_dir)

    summary = {
        "added": added, "removed": removed, "modified": modified,
        "edges_added": edges_added,
    }
    return summary


# ---------------------------------------------------------------------------
# Semantic update status
# ---------------------------------------------------------------------------

def _write_semantic_status(graph_dir: str, stale_count: int = 0,
                           changed_files: int = 0,
                           semantic_update: bool = False):
    """Write .code2database_semantic_status.json tracking update state."""
    status_path = os.path.join(graph_dir, ".code2database_semantic_status.json")

    existing = {}
    if os.path.exists(status_path):
        existing = json.loads(Path(status_path).read_text(encoding="utf-8"))

    from datetime import datetime
    existing.update({
        "last_quick_update": datetime.now().isoformat(),
        "stale_count_at_last_quick_update": stale_count,
        "changed_files_at_last_quick_update": changed_files,
    })
    if semantic_update:
        existing["last_semantic_update"] = datetime.now().isoformat()

    # Atomic write: tmp + os.replace
    _tmp = status_path + ".tmp"
    Path(_tmp).write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(_tmp, status_path)


def get_semantic_update_status(graph_dir: str) -> dict:
    """Check if enough changes have accumulated for a full semantic update.

    Returns: {
        stale_count, stale_ratio, stale_apis,
        recommend_semantic_update, priority_domains,
        last_semantic_update, last_quick_update
    }
    """
    G = _load_full_graph(graph_dir)

    total_nodes = G.number_of_nodes()
    stale_nodes = [(nid, nd) for nid, nd in G.nodes(data=True) if nd.get("stale", False)]
    stale_count = len(stale_nodes)

    # Stale API entries
    stale_apis = [nd.get("name", "") for nid, nd in stale_nodes
                  if "API_entry" in nd.get("labels", [])]

    # Stale ratio
    stale_ratio = stale_count / total_nodes if total_nodes > 0 else 0.0

    # Priority domains (domains with high stale ratio)
    domain_stale = defaultdict(int)
    domain_total = defaultdict(int)
    for nid, nd in G.nodes(data=True):
        domain = nd.get("domain", "root")
        domain_total[domain] += 1
        if nd.get("stale", False):
            domain_stale[domain] += 1

    priority_domains = []
    for domain in sorted(domain_total.keys()):
        stale_r = domain_stale.get(domain, 0) / domain_total[domain]
        if stale_r > 0.1:
            priority_domains.append({
                "domain": domain,
                "stale_ratio": round(stale_r, 3),
                "stale_count": domain_stale.get(domain, 0),
                "total_count": domain_total[domain],
            })

    # Read status file for timestamps
    status_path = os.path.join(graph_dir, ".code2database_semantic_status.json")
    last_semantic = ""
    last_quick = ""
    if os.path.exists(status_path):
        status = json.loads(Path(status_path).read_text(encoding="utf-8"))
        last_semantic = status.get("last_semantic_update", "")
        last_quick = status.get("last_quick_update", "")

    # Recommendation logic
    recommend = (stale_ratio >= 0.15 or
                 len(stale_apis) > 0 or
                 any(d["stale_ratio"] > 0.3 for d in priority_domains))

    return {
        "stale_count": stale_count,
        "stale_ratio": round(stale_ratio, 4),
        "stale_apis": stale_apis,
        "recommend_semantic_update": recommend,
        "priority_domains": priority_domains[:10],
        "last_semantic_update": last_semantic,
        "last_quick_update": last_quick,
    }


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def cmd_quick_update(args):
    """Handle quick-update command."""
    source_root = args.source
    graph_dir = args.graph
    commit_range = getattr(args, "commit_range", None)
    auto_threshold = getattr(args, "auto_threshold", None)

    summary = quick_update(source_root, graph_dir, commit_range)

    # If --auto-threshold is set, automatically trigger semantic update
    # when stale ratio exceeds the threshold
    if auto_threshold is not None:
        try:
            threshold_val = float(auto_threshold)
        except (ValueError, TypeError):
            threshold_val = 0.15
        status = get_semantic_update_status(graph_dir)
        stale_ratio = status.get("stale_ratio", 0.0)
        if stale_ratio >= threshold_val:
            summary["auto_semantic_update_triggered"] = True
            summary["stale_ratio"] = stale_ratio
            summary["auto_threshold"] = threshold_val
            # Write timestamp so semantic-status knows an update was triggered
            _write_semantic_status(graph_dir,
                                   stale_count=status.get("stale_count", 0),
                                   changed_files=summary.get("changed_files", 0),
                                   semantic_update=True)
        else:
            summary["auto_semantic_update_triggered"] = False
            summary["stale_ratio"] = stale_ratio
            summary["auto_threshold"] = threshold_val

    print(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_export_changes(args):
    """Handle export-changes command."""
    source_root = args.source
    graph_dir = args.graph
    commit_range = getattr(args, "commit_range", None)
    output_path = getattr(args, "output", "")

    result_path = export_change_graph(source_root, graph_dir, commit_range, output_path or None)
    print(f"Change graph exported to: {result_path}")


def cmd_merge_changes(args):
    """Handle merge-changes command."""
    graph_dir = args.graph
    changes_path = args.changes
    source_root = getattr(args, "source", "")

    summary = merge_change_graph(graph_dir, changes_path, source_root)
    print(f"Merged: {summary['added']} added, {summary['removed']} removed, "
          f"{summary['modified']} modified, {summary['edges_added']} edges added")


def cmd_semantic_status(args):
    """Handle semantic-status command."""
    graph_dir = args.graph
    status = get_semantic_update_status(graph_dir)
    print(json.dumps(status, ensure_ascii=False, indent=2))
