"""Co-change coupling edge extraction from git log.

Fills the CO_CHANGE edge gap identified by comparing with emerge
(PyDriller-based git metrics). Mines the git log for files that are
frequently changed together in the same commit, then emits CO_CHANGE
edges between their corresponding graph nodes.

Co-change coupling is a strong predictor of hidden dependencies
that call-graph analysis misses: two files may have no direct call
edge but are coupled by shared maintenance concerns.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from typing import Dict, List, Set


def extract_co_change_edges(source_root: str, graph_dir: str,
                            min_co_changes: int = 3,
                            max_commits: int = 5000) -> List[Dict]:
    """Mine git log for co-change coupling patterns.

    Two files are co-changed when they appear in the same commit.
    The coupling strength is the number of shared commits.

    Args:
        source_root: Path to the git repository root.
        graph_dir: Path to the C2D graph directory.
        min_co_changes: Minimum shared commits to emit an edge.
        max_commits: Maximum commits to scan (safety cap).

    Returns:
        List of {source_file, target_file, co_change_count, edge_type: "CO_CHANGE"}
    """
    # Get git log: commit → set of changed files
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--format=__COMMIT__%H",
             "--no-merges", f"-{max_commits}"],
            cwd=source_root, capture_output=True, text=True,
            timeout=60, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return []

    # Parse commits → file sets
    commit_files: List[Set[str]] = []
    current_files: Set[str] = set()
    for line in result.stdout.split("\n"):
        if line.startswith("__COMMIT__"):
            if current_files:
                commit_files.append(current_files)
                current_files = set()
        elif line.strip() and not line.startswith("commit "):
            f = line.strip()
            if f and not f.startswith("."):
                current_files.add(f)
    if current_files:
        commit_files.append(current_files)

    # Count co-occurrences
    co_change_count: Counter = Counter()  # (file_a, file_b) → count
    for files in commit_files:
        file_list = sorted(files)
        for i in range(len(file_list)):
            for j in range(i + 1, len(file_list)):
                pair = (file_list[i], file_list[j])
                co_change_count[pair] += 1

    # Filter by minimum and build edges
    edges = []
    for (file_a, file_b), count in co_change_count.most_common():
        if count < min_co_changes:
            break
        edges.append({
            "source_file": file_a,
            "target_file": file_b,
            "co_change_count": count,
            "edge_type": "CO_CHANGE",
        })

    return edges


def cmd_co_change(args):
    """CLI handler: co-change."""
    edges = extract_co_change_edges(
        source_root=args.source,
        graph_dir=args.graph,
        min_co_changes=getattr(args, "min_co_changes", 3),
        max_commits=getattr(args, "max_commits", 5000),
    )
    # Output as JSON
    result = {
        "source": args.source,
        "total_edges": len(edges),
        "edges": edges,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
