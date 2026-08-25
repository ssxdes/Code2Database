"""callgraph builder module: update_sync."""

import os
import json
import sys
import re
from pathlib import Path
from collections import defaultdict

# Ensure _vendor/networkx shim and parent dirs are found
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "_vendor"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import networkx as nx
import subprocess
import shutil
from _builder.utils import _output_result, _print_structured, _find_node_id, _parse_bindings, _load_globals, _is_condition_alive
from _builder.graph_build import _load_full_graph, build_graph, split_by_domain
from _builder.index_pack import _build_indexes, _mark_endpoint_nodes
from _builder.memory_cmd import _auto_validate_memory


def _merge_json_union(local_path: str, git_path: str, out_path: str,
                      list_keys: list, dedup_key: str = "id",
                      local_wins: bool = True):
    """Merge two JSON files by unioning list-valued keys, deduplicating.

    For each key in list_keys, takes the union of both arrays.
    Deduplicates by `dedup_key`. If local_wins, local entry overwrites git
    entry on overlap; otherwise git wins.
    """
    local_data = {}
    if os.path.exists(local_path):
        local_data = json.loads(Path(local_path).read_text(encoding="utf-8"))

    git_data = {}
    if os.path.exists(git_path):
        git_data = json.loads(Path(git_path).read_text(encoding="utf-8"))

    merged_data = {}
    for key in list_keys:
        local_list = local_data.get(key, [])
        git_list = git_data.get(key, [])

        seen = {}
        _nokey_idx = 0
        for item in git_list:
            k = item.get(dedup_key, "")
            if k:
                seen[k] = item
            else:
                # No dedup key — include with synthetic key (same as local items below)
                seen[f"__git_nokey_{_nokey_idx}"] = item
                _nokey_idx += 1

        for item in local_list:
            k = item.get(dedup_key, "")
            if k:
                if local_wins or k not in seen:
                    seen[k] = item
            else:
                # No dedup key — always include
                local_list_idx = len(seen)
                seen[f"__nokey_{local_list_idx}"] = item

        merged_data[key] = list(seen.values())

    # Carry over any non-list keys from local (they're scalar metadata)
    for key in local_data:
        if key not in list_keys:
            merged_data[key] = local_data[key]

    Path(out_path).write_text(
        json.dumps(merged_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )




def _merge_manifest(local_path: str, git_path: str, out_path: str,
                     source_root: str):
    """Merge two .code2database_manifest.json files.

    Union of file fingerprints; local wins on conflict.
    """
    local_data = {"source_root": source_root, "files": {}}
    if os.path.exists(local_path):
        local_data = json.loads(Path(local_path).read_text(encoding="utf-8"))

    git_data = {"source_root": source_root, "files": {}}
    if os.path.exists(git_path):
        git_data = json.loads(Path(git_path).read_text(encoding="utf-8"))

    # Union fingerprints, local wins on conflict
    merged_files = dict(git_data.get("files", {}))
    merged_files.update(local_data.get("files", {}))

    merged = {
        "source_root": source_root,
        "files": merged_files,
    }
    Path(out_path).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )




def _merge_memory_dir(local_graph_dir: str, git_graph_dir: str):
    """Merge memory directories: union of entries, local wins by id."""
    local_mem = os.path.join(local_graph_dir, "memory")
    git_mem = os.path.join(git_graph_dir, "memory")

    if not os.path.exists(git_mem):
        return
    if not os.path.exists(local_mem):
        # Copy git memory entirely
        shutil.copytree(git_mem, local_mem)
        return

    # Merge index.json
    local_idx_path = os.path.join(local_mem, "index.json")
    git_idx_path = os.path.join(git_mem, "index.json")

    if os.path.exists(git_idx_path):
        git_idx = json.loads(Path(git_idx_path).read_text(encoding="utf-8"))
        local_idx = {"entries": []}
        if os.path.exists(local_idx_path):
            local_idx = json.loads(Path(local_idx_path).read_text(encoding="utf-8"))

        local_ids = {e.get("id") for e in local_idx.get("entries", [])}
        for entry in git_idx.get("entries", []):
            if entry.get("id") not in local_ids:
                local_idx["entries"].append(entry)
                # Copy the memory file — try root/leaf layout, then old flat path
                eid = entry.get("id", "")
                is_root = entry.get("root_id") == eid
                # Ensure root/leaf dirs exist
                os.makedirs(os.path.join(local_mem, "root"), exist_ok=True)
                os.makedirs(os.path.join(local_mem, "leaf"), exist_ok=True)
                if is_root:
                    src = os.path.join(git_mem, "root", f"root_{eid}.json")
                    dst = os.path.join(local_mem, "root", f"root_{eid}.json")
                else:
                    src = os.path.join(git_mem, "leaf", f"mem_{eid}.json")
                    dst = os.path.join(local_mem, "leaf", f"mem_{eid}.json")
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                else:
                    # Fallback to old flat path
                    old_src = os.path.join(git_mem, f"memory_{eid}.json")
                    if os.path.exists(old_src):
                        shutil.copy2(old_src, os.path.join(local_mem, f"memory_{eid}.json"))

        Path(local_idx_path).write_text(
            json.dumps(local_idx, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )

    # Merge experience subdirectory
    local_exp = os.path.join(local_mem, "experience")
    git_exp = os.path.join(git_mem, "experience")
    if os.path.exists(git_exp) and os.path.exists(os.path.join(git_exp, "index.json")):
        os.makedirs(local_exp, exist_ok=True)
        git_exp_idx = json.loads(Path(os.path.join(git_exp, "index.json")).read_text(encoding="utf-8"))
        local_exp_idx = {"entries": []}
        local_exp_idx_path = os.path.join(local_exp, "index.json")
        if os.path.exists(local_exp_idx_path):
            local_exp_idx = json.loads(Path(local_exp_idx_path).read_text(encoding="utf-8"))

        local_exp_ids = {e.get("id") for e in local_exp_idx.get("entries", [])}
        for entry in git_exp_idx.get("entries", []):
            if entry.get("id") not in local_exp_ids:
                local_exp_idx["entries"].append(entry)
                git_entry_path = os.path.join(git_exp, f"experience_{entry['id']}.json")
                if os.path.exists(git_entry_path):
                    shutil.copy2(git_entry_path, os.path.join(local_exp, f"experience_{entry['id']}.json"))

        Path(local_exp_idx_path).write_text(
            json.dumps(local_exp_idx, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8"
        )




def _prune_nodes_by_source(G: nx.DiGraph, deleted_relpaths: list) -> int:
    """Remove nodes whose source_file matches any deleted relative path."""
    # Build a set of exact paths and path suffixes for fast matching
    # Use full relative paths to avoid basename over-matching
    deleted_exact = set(deleted_relpaths)
    deleted_suffixes = set()
    for dp in deleted_relpaths:
        # "lib/bdev/bdev.c" → match "lib/bdev/bdev.c" at end of any source_file
        deleted_suffixes.add("/" + dp)

    pruned = 0
    for nid in list(G.nodes()):
        ndata = G.nodes[nid]
        src = ndata.get("source_file", "")
        if not src:
            continue
        # Exact match or proper suffix match (full relative path, not just basename)
        if src in deleted_exact or any(src.endswith(s) for s in deleted_suffixes):
            G.remove_node(nid)
            pruned += 1
    return pruned




def cmd_merge(args):
    """Merge new extraction data into existing invocation graph (append-only, safe)."""
    graph_dir = args.graph
    extraction_path = args.extraction

    # Load existing graph
    G = _load_full_graph(graph_dir)
    # LazySQLiteGraph (read-only SQLite view for large projects) is
    # incompatible with nx.compose below. Detect early and exit with a
    # clear error directing users to daemon-start / build.
    from _builder.utils import _ensure_mutable_graph
    _ensure_mutable_graph(G, "merge")

    # Load new extraction
    new_data = json.loads(Path(extraction_path).read_text(encoding="utf-8"))
    build_result = build_graph(new_data)
    # build_graph returns (G, file_nodes) tuple
    new_G = build_result[0] if isinstance(build_result, tuple) else build_result

    # Compose merged graph
    merged = nx.compose(G, new_G)

    # For edges that exist in both, prefer new_G's attributes (call edges only)
    for u, v, edata in new_G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if merged.has_edge(u, v):
            # Update attributes from new graph
            merged[u][v].update(edata)

    # Write back
    master = json.loads(Path(os.path.join(graph_dir, "code2database_master.json")).read_text(encoding="utf-8"))
    source_root = master.get("source_root", "")
    split_by_domain(merged, graph_dir, source_root)

    # Rebuild indexes after merge so queries reflect the new data
    from _builder.index_pack import _build_indexes, _mark_endpoint_nodes
    _mark_endpoint_nodes(merged, graph_dir)
    _build_indexes(merged, graph_dir)

    print(f"Merged: {G.number_of_nodes()}+{new_G.number_of_nodes()} → {merged.number_of_nodes()} nodes, "
          f"{merged.number_of_edges()} edges")




def cmd_sync(args):
    """Sync local code2db-out with git-tracked version (local wins on overlap)."""
    import subprocess
    import tempfile

    graph_dir = os.path.abspath(args.graph)
    if not os.path.isdir(graph_dir):
        print(f"Error: {graph_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    local_master = os.path.join(graph_dir, "code2database_master.json")
    if not os.path.exists(local_master):
        print(f"Error: {local_master} not found. Run 'build' first.", file=sys.stderr)
        sys.exit(1)

    dry_run = args.dry_run

    # --- Step 1: Obtain git-tracked code2db-out ---
    git_graph_dir = None
    temp_dir = None

    if args.git_path:
        # User provided a direct path to git-tracked code2db-out
        git_graph_dir = os.path.abspath(args.git_path)
        if not os.path.exists(os.path.join(git_graph_dir, "code2database_master.json")):
            print(f"Error: {git_graph_dir} does not contain a code2database_master.json",
                  file=sys.stderr)
            sys.exit(1)
    else:
        # Auto-detect from git repo
        # Find the git repo root by walking up from graph_dir's parent
        search_dir = os.path.dirname(graph_dir)
        if not search_dir:
            search_dir = "."

        # Check if search_dir is inside a git repo
        git_check = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=search_dir
        )
        if git_check.returncode != 0:
            print("Error: Current directory is not inside a git repository.\n"
                  "Options:\n"
                  "  1. Run from within a git repo that tracks code2db-out/\n"
                  "  2. Use --git-path to specify the path to a git-tracked code2db-out",
                  file=sys.stderr)
            sys.exit(1)

        git_root = git_check.stdout.strip()
        # Determine the relative path of code2db-out within the repo
        try:
            cg_relpath = os.path.relpath(graph_dir, git_root)
        except ValueError:
            cg_relpath = ""

        # Check if code2db-out is tracked in git
        git_ls = subprocess.run(
            ["git", "ls-files", "--error-unmatch", cg_relpath],
            capture_output=True, text=True, cwd=git_root
        )
        if git_ls.returncode != 0:
            print(f"Error: code2db-out/ ('{cg_relpath}') is not tracked in git.\n"
                  "Either:\n"
                  "  1. Add and commit code2db-out/ to the repo: git add code2db-out/ && git commit\n"
                  "  2. Use --git-path to specify a pre-existing code2db-out directory",
                  file=sys.stderr)
            sys.exit(1)

        remote = args.remote or "origin"
        branch = args.branch

        # Auto-detect branch if not specified
        if not branch:
            branch_check = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                capture_output=True, text=True, cwd=git_root
            )
            if branch_check.returncode == 0 and branch_check.stdout.strip():
                # upstream is like "origin/main" — extract branch name
                parts = branch_check.stdout.strip().split("/", 1)
                if len(parts) == 2 and parts[0] == remote:
                    branch = parts[1]
                else:
                    branch = branch_check.stdout.strip()
            else:
                # Fallback: current local branch
                head_check = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, cwd=git_root
                )
                branch = head_check.stdout.strip() if head_check.returncode == 0 else "main"

        # Fetch from remote
        print(f"Fetching from {remote}...")
        fetch_result = subprocess.run(
            ["git", "fetch", "--quiet", remote],
            capture_output=True, text=True, cwd=git_root
        )
        if fetch_result.returncode != 0:
            print(f"Warning: git fetch failed: {fetch_result.stderr.strip()}", file=sys.stderr)
            print("Continuing with locally available remote data...")

        # Extract code2db-out from remote branch into temp directory
        temp_dir = tempfile.mkdtemp(prefix="code2database_sync_")
        archive_ref = f"{remote}/{branch}"

        # Try git archive first
        archive_result = subprocess.run(
            ["git", "archive", archive_ref, "--", cg_relpath],
            capture_output=True, cwd=git_root
        )
        if archive_result.returncode == 0 and archive_result.stdout:
            extract_result = subprocess.run(
                ["tar", "-x", "-C", temp_dir],
                input=archive_result.stdout, cwd=git_root
            )
            if extract_result.returncode != 0:
                print("Error: Failed to extract git archive", file=sys.stderr)
                shutil.rmtree(temp_dir, ignore_errors=True)
                sys.exit(1)
            git_graph_dir = os.path.join(temp_dir, cg_relpath)
        else:
            # Fallback: use git show for individual files
            # This handles cases where git archive isn't available (e.g., shallow clones)
            print("git archive unavailable, extracting files individually...")
            git_graph_dir = os.path.join(temp_dir, os.path.basename(graph_dir))
            os.makedirs(git_graph_dir, exist_ok=True)

            # List all files under cg_relpath in the remote
            ls_result = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", archive_ref, "--", cg_relpath],
                capture_output=True, text=True, cwd=git_root
            )
            if ls_result.returncode != 0:
                print(f"Error: Could not list remote files: {ls_result.stderr.strip()}",
                      file=sys.stderr)
                shutil.rmtree(temp_dir, ignore_errors=True)
                sys.exit(1)

            for relpath in ls_result.stdout.strip().split("\n"):
                if not relpath:
                    continue
                show_result = subprocess.run(
                    ["git", "show", f"{archive_ref}:{relpath}"],
                    capture_output=True, text=True, cwd=git_root
                )
                if show_result.returncode == 0:
                    dest = os.path.join(temp_dir, relpath)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    Path(dest).write_text(show_result.stdout, encoding="utf-8")

    # Verify git version has master
    if not git_graph_dir or not os.path.exists(os.path.join(git_graph_dir, "code2database_master.json")):
        print("Error: Git-tracked code2db-out does not contain code2database_master.json",
              file=sys.stderr)
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        sys.exit(1)

    # --- Step 2: Load both graphs ---
    print("Loading local graph...")
    local_G = _load_full_graph(graph_dir)
    print("Loading git-tracked graph...")
    git_G = _load_full_graph(git_graph_dir)
    # Both graphs must be in-memory nx.DiGraph for nx.compose below.
    # LazySQLiteGraph (read-only SQLite view for >=50K-function projects)
    # will crash at nx.compose with AttributeError on .graph.
    from _builder.utils import _ensure_mutable_graph
    _ensure_mutable_graph(local_G, "sync (local graph)")
    _ensure_mutable_graph(git_G, "sync (git-tracked graph)")

    local_nodes = set(local_G.nodes())
    git_nodes = set(git_G.nodes())

    # --- Step 3: Merge graphs (local wins) ---
    # nx.compose(G1, G2) gives priority to G2 for overlapping nodes
    merged_G = nx.compose(git_G, local_G)

    # For overlapping edges, local_G attributes take precedence (call edges only)
    for u, v, edata in local_G.edges(data=True):
        if edata.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        if merged_G.has_edge(u, v):
            merged_G[u][v].update(edata)

    # Ensure all local-only nodes are present (compose should handle this, but verify)
    for nid, ndata in local_G.nodes(data=True):
        if nid not in merged_G:
            merged_G.add_node(nid, **ndata)

    # --- Step 4: Compute merge stats ---
    git_only_nodes = git_nodes - local_nodes
    local_only_nodes = local_nodes - git_nodes
    common_nodes = local_nodes & git_nodes

    # Count domains unique to each side
    local_domains = {local_G.nodes[n].get("domain", "root") for n in local_nodes}
    git_domains = {git_G.nodes[n].get("domain", "root") for n in git_nodes}
    new_domains_from_git = git_domains - local_domains

    # Count edges
    git_only_edges = set(git_G.edges()) - set(local_G.edges())
    local_only_edges = set(local_G.edges()) - set(git_G.edges())

    print()
    print(f"Local:  {local_G.number_of_nodes()} nodes, {local_G.number_of_edges()} edges, "
          f"{len(local_domains)} domains")
    print(f"Git:    {git_G.number_of_nodes()} nodes, {git_G.number_of_edges()} edges, "
          f"{len(git_domains)} domains")
    print(f"Merged: {merged_G.number_of_nodes()} nodes, {merged_G.number_of_edges()} edges")
    print()
    print(f"Git-only nodes added:  {len(git_only_nodes)}")
    print(f"Local-only nodes kept: {len(local_only_nodes)}")
    print(f"Common nodes:          {len(common_nodes)} (local data preserved)")
    if new_domains_from_git:
        print(f"New domains from git:  {', '.join(sorted(new_domains_from_git))}")
    if git_only_edges:
        print(f"New edges from git:    {len(git_only_edges)}")
    print()

    if dry_run:
        print("[DRY RUN] No files written.")
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # --- Step 5: Write merged result ---
    # Get source_root from local master, fallback to git
    local_master_data = json.loads(Path(local_master).read_text(encoding="utf-8"))
    source_root = local_master_data.get("source_root", "")
    if not source_root:
        git_master = os.path.join(git_graph_dir, "code2database_master.json")
        git_master_data = json.loads(Path(git_master).read_text(encoding="utf-8"))
        source_root = git_master_data.get("source_root", "")

    # Re-split domains and rebuild indexes
    print("Rebuilding domain files and indexes...")
    split_by_domain(merged_G, graph_dir, source_root)
    _build_indexes(merged_G, graph_dir)

    # Merge auxiliary JSON files
    print("Merging auxiliary files...")

    # Globals
    _merge_json_union(
        os.path.join(graph_dir, ".code2database_globals.json"),
        os.path.join(git_graph_dir, ".code2database_globals.json"),
        os.path.join(graph_dir, ".code2database_globals.json"),
        list_keys=["enums", "constants", "typedefs", "global_vars"],
        dedup_key="name",
        local_wins=True,
    )

    # Endpoints
    _merge_json_union(
        os.path.join(graph_dir, ".code2database_endpoints.json"),
        os.path.join(git_graph_dir, ".code2database_endpoints.json"),
        os.path.join(graph_dir, ".code2database_endpoints.json"),
        list_keys=["endpoints"],
        dedup_key="id",
        local_wins=True,
    )

    # Manifest
    _merge_manifest(
        os.path.join(graph_dir, ".code2database_manifest.json"),
        os.path.join(git_graph_dir, ".code2database_manifest.json"),
        os.path.join(graph_dir, ".code2database_manifest.json"),
        source_root=source_root,
    )

    # Memory
    _merge_memory_dir(graph_dir, git_graph_dir)

    # Clean up temp directory
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # --- Step 6: Final report ---
    print()
    print(f"Sync complete: {merged_G.number_of_nodes()} nodes, "
          f"{merged_G.number_of_edges()} edges, "
          f"{len(local_domains | git_domains)} domains")
    if git_only_nodes:
        print(f"  Added {len(git_only_nodes)} nodes from git")
    print()
    print("Review the merged code2db-out/ and commit when ready:")
    print(f"  git add {os.path.relpath(graph_dir, os.getcwd())}")
    print(f"  git commit -m \"sync callgraph: merge with remote\"")




def cmd_update(args):
    """Incremental update: detect changed files, re-scan only those, merge into existing graph."""
    import subprocess
    import tempfile

    source = args.source
    graph_dir = args.graph
    # Find scanner script: check parent directory (scripts/) as well as current dir
    scanner_script = os.path.join(os.path.dirname(__file__), "code2database_scanner.py")
    if not os.path.exists(scanner_script):
        scanner_script = os.path.join(os.path.dirname(__file__), "..", "code2database_scanner.py")

    # Step 1: Detect changes
    detect_result = subprocess.run(
        [sys.executable, scanner_script, "detect-changes",
         "--source", source, "--outdir", graph_dir],
        capture_output=True, text=True,
        stdin=subprocess.DEVNULL
    )
    if detect_result.returncode != 0:
        print(f"Error detecting changes: {detect_result.stderr}", file=sys.stderr)
        sys.exit(1)

    changes = json.loads(detect_result.stdout)

    if changes.get("needs_full_scan"):
        print("No manifest found — full scan required. Run 'build' first.")
        sys.exit(1)

    new_files = changes.get("new_paths", [])
    changed_files = changes.get("changed_paths", [])
    deleted_files = changes.get("deleted_relpaths", [])

    to_scan = new_files + changed_files
    total_changed = len(to_scan)
    deleted_count = len(deleted_files)

    if total_changed == 0 and deleted_count == 0:
        print("No files changed since last scan. Graph is up to date.")
        return

    print(f"Incremental update: {total_changed} file(s) to re-scan, "
          f"{deleted_count} file(s) deleted, "
          f"{changes.get('unchanged_count', 0)} unchanged")

    # Step 2: Load existing graph and prune deleted
    G = _load_full_graph(graph_dir)
    # Error 3 defense: cmd_update's merge pipeline uses nx.compose + per-node
    # attribute writes (split_by_domain → G.nodes[nid]["domain"] = ...),
    # both of which require an in-memory nx.DiGraph. When the graph is large
    # (>=50K functions), _load_full_graph returns a LazySQLiteGraph (read-only
    # SQLite view) which has no `.graph` attribute (breaks nx.compose) and
    # rejects item assignment (breaks split_by_domain). Detect this early and
    # direct the user to daemon-start (cgdb incremental sync) which is the
    # correct path for SQLite-backed large graphs. Without this check, the
    # user sees a cryptic "AttributeError: 'LazySQLiteGraph' object has no
    # attribute 'graph'" at nx.compose.
    _G_CLASS = type(G).__name__
    if _G_CLASS == "LazySQLiteGraph":
        print(
            "Error: 'update' is not supported on SQLite-backed large graphs\n"
            f"  Loaded graph: {G.number_of_nodes()} nodes via LazySQLiteGraph "
            f"(db: {getattr(G, '_db_path', '?')})\n"
            "  Reason: 'update' uses in-memory nx.compose + per-node writes "
            "(split_by_domain),\n"
            "          but LazySQLiteGraph is a read-only SQLite view with no "
            ".graph attribute.\n"
            "Alternatives:\n"
            "  1. Run 'daemon-start' for real-time incremental sync "
            "(cgdb_incremental path,\n"
            "     designed for SQLite-backed large graphs).\n"
            "  2. Run 'build' for a full rebuild from scratch "
            "(rebuilds the SQLite db).\n"
            "  3. If you must use 'update', force eager load by removing the "
            "code2database.db file\n"
            "     first (NOTE: this may OOM for >100K-function projects).",
            file=sys.stderr)
        sys.exit(1)
    if deleted_files:
        pruned = _prune_nodes_by_source(G, deleted_files)
        print(f"Pruned {pruned} node(s) from deleted files")

    # Step 3: Scan changed files (if any)
    # Load build config macros if available
    bc_path = os.path.join(graph_dir, ".code2database_build_config.json")
    macros_str = ""
    if os.path.exists(bc_path):
        bc_data = json.loads(Path(bc_path).read_text(encoding="utf-8"))
        macros = bc_data.get("defined_macros", {})
        if macros:
            macros_str = " ".join(f"-D{k}={v}" if v else f"-D{k}" for k, v in macros.items())

    if to_scan:
        if args.extraction:
            extraction_path = args.extraction
        else:
            extraction_path = os.path.join(graph_dir, ".code2database_incremental.json")
            # Use --files-from (temp file) when the file list is large to avoid
            # OSError: [Errno 7] Argument list too long (Linux ARG_MAX ~128KB).
            # Threshold: 1000 files or any single path containing commas that
            # would inflate the joined string. Fall back to --files for small
            # lists to preserve backward compatibility with older scanners.
            _FILES_FROM_THRESHOLD = 200
            files_list_fd = None
            files_list_path = None
            try:
                if len(to_scan) >= _FILES_FROM_THRESHOLD:
                    files_list_fd, files_list_path = tempfile.mkstemp(
                        prefix="c2d_update_files_", suffix=".txt")
                    with os.fdopen(files_list_fd, "w", encoding="utf-8") as _fh:
                        for _p in to_scan:
                            _fh.write(_p + "\n")
                    files_list_fd = None  # closed by _fh.__exit__
                    scan_cmd = [sys.executable, scanner_script, "scan",
                                "--source", source,
                                "--files-from", files_list_path,
                                "--output", extraction_path,
                                "--no-interactive"]
                else:
                    files_arg = ",".join(to_scan)
                    # #15 fix: byte-length check — even <200 files with
                    # long paths can exceed ARG_MAX (~128KB on Linux).
                    if len(files_arg.encode("utf-8")) > 100_000:
                        # Fall back to --files-from for safety
                        import tempfile as _tf2
                        files_list_fd2, files_list_path2 = _tf2.mkstemp(
                            prefix="code2db_files2_", suffix=".txt")
                        try:
                            with os.fdopen(files_list_fd2, "w", encoding="utf-8") as _f2:
                                _f2.write("\n".join(to_scan))
                            scan_cmd = [sys.executable, scanner_script, "scan",
                                        "--source", source, "--files-from", files_list_path2,
                                        "--output", extraction_path,
                                        "--no-interactive"]
                        finally:
                            try: os.close(files_list_fd2)
                            except OSError: pass
                            try: os.remove(files_list_path2)
                            except OSError: pass
                    else:
                        scan_cmd = [sys.executable, scanner_script, "scan",
                                    "--source", source, "--files", files_arg,
                                    "--output", extraction_path,
                                    "--no-interactive"]
                if macros_str:
                    scan_cmd.extend(["--macros", macros_str])
                scan_result = subprocess.run(
                    scan_cmd,
                    capture_output=True, text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=3600
                )
                if scan_result.returncode != 0:
                    print(f"Error scanning changed files: {scan_result.stderr}", file=sys.stderr)
                    sys.exit(1)
                print(scan_result.stderr.strip() if scan_result.stderr else "", file=sys.stderr)
            finally:
                if files_list_fd is not None:
                    try:
                        os.close(files_list_fd)
                    except OSError:
                        pass
                if files_list_path and os.path.exists(files_list_path):
                    try:
                        os.remove(files_list_path)
                    except OSError:
                        pass

        new_data = json.loads(Path(extraction_path).read_text(encoding="utf-8"))
        result = build_graph(new_data)
        new_G = result[0] if isinstance(result, tuple) else result

        # Merge: new graph takes precedence for overlapping nodes (call edges only)
        merged = nx.compose(G, new_G)
        for u, v, edata in new_G.edges(data=True):
            if edata.get("relation") in ("CONTAINS", "IMPORTS"):
                continue
            if merged.has_edge(u, v):
                merged[u][v].update(edata)

        # Add new nodes that might not be in compose
        for nid, ndata in new_G.nodes(data=True):
            if nid not in merged:
                merged.add_node(nid, **ndata)
    else:
        merged = G

    # Step 4: Re-mark endpoints and split
    ep_count = _mark_endpoint_nodes(merged, graph_dir)
    master = json.loads(Path(os.path.join(graph_dir, "code2database_master.json")).read_text(encoding="utf-8"))
    source_root = master.get("source_root", source)
    split_by_domain(merged, graph_dir, source_root)

    # Step 5: Update manifest
    subprocess.run(
        [sys.executable, scanner_script, "manifest",
         "--source", source, "--outdir", graph_dir],
        capture_output=True, text=True,
        stdin=subprocess.DEVNULL
    )

    print(f"Updated invocation graph: {merged.number_of_nodes()} nodes, {merged.number_of_edges()} edges")
    if ep_count > 0:
        print(f"Endpoints: {ep_count} external endpoint(s) — run classify-endpoints to finalize")

    # Validate memory after graph update (nodes may have been removed)
    mem_dir = os.path.join(graph_dir, "memory")
    if os.path.exists(os.path.join(mem_dir, "index.json")):
        _auto_validate_memory(merged, mem_dir, graph_dir)


