"""Multi-project aggregate build — manifest-driven joint C2D construction.

Phase 1 of the multi-project support design. Builds a single unified
C2D from a manifest of interdependent projects (A -> B -> C dependency
chain), forcing each function's domain to start with the project name
so that A:init() and B:init() never collide.

Key design:
- Manifest JSON lists projects with source/include_paths/compile_commands
- Topological sort by depends_on (cycles fail with clear error)
- For each project: scan + post-process to prefix domain with project name
- Aggregate all include_paths into one --clang-args string
- Merge all compile_commands.json into a single temp file
- Call existing build_graph() on the joint extraction
- Reuse mode: if existing_c2d provided + fresh, import nodes/edges
  directly instead of re-scanning

The domain prefix is enforced by _prefix_domain_with_project() which
rewrites the extraction JSON in-place: every function's `domain` field
gets `<project_name>.` prepended (or replaced if "root"), and the
`id` field (legacy string ID) is regenerated as
`<project_name>_<function_name>` so it stays unique.

This works because _make_func_id() in _scanner/base.py:2041 already
derives the legacy id from domain+name, and unified_node_id() in
_scanner/unified_id.py:35 hashes (language, fqn, signature) where fqn
includes the legacy id — so domain-prefixed projects get distinct
cgdb_node_ids automatically.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional
import logging


# ---------------------------------------------------------------------------
# Manifest parsing + topological sort
# ---------------------------------------------------------------------------

def _parse_manifest(manifest_path: str) -> Dict[str, Any]:
    """Parse and validate the build-multi manifest JSON.

    Required top-level keys: version, projects, output
    Each project: name (unique), source OR existing_c2d
    Optional: include_paths, compile_commands, macros, depends_on
    """
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Failed to parse manifest {manifest_path}: {e}")
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    if "version" not in manifest:
        raise ValueError(
            "Manifest is missing required 'version' field (expected 1). "
            "See docs/en/references/manifest_schema.md for the schema."
        )
    if manifest.get("version") != 1:
        raise ValueError(f"Unsupported manifest version: {manifest.get('version')} (expected 1)")
    projects = manifest.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("Manifest must have non-empty 'projects' list")
    # Validate each project entry
    names = set()
    for i, p in enumerate(projects):
        if not isinstance(p, dict):
            raise ValueError(f"projects[{i}] must be a dict")
        name = p.get("name")
        if not name:
            raise ValueError(f"projects[{i}] missing 'name'")
        if name in names:
            raise ValueError(f"Duplicate project name: {name}")
        names.add(name)
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
            raise ValueError(f"Invalid project name '{name}' — must be a valid identifier")
        if not p.get("source") and not p.get("existing_c2d"):
            raise ValueError(f"project '{name}' must have 'source' or 'existing_c2d'")
    return manifest


def _topo_sort(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Topologically sort projects by depends_on.

    Cycles fail with ValueError listing the cycle path.
    """
    name_to_proj = {p["name"]: p for p in projects}
    # Validate depends_on references
    for p in projects:
        for dep in p.get("depends_on", []):
            if dep not in name_to_proj:
                raise ValueError(
                    f"project '{p['name']}' depends on unknown project '{dep}'"
                )
    visited: Dict[str, int] = {}  # name -> 0=visiting, 1=done
    order: List[Dict[str, Any]] = []
    cycle_path: List[str] = []

    def visit(name: str, path: List[str]) -> None:
        state = visited.get(name)
        if state == 1:
            return
        if state == 0:
            cycle_path.extend(path[path.index(name):] + [name])
            raise ValueError(f"Circular dependency: {' -> '.join(cycle_path)}")
        visited[name] = 0
        p = name_to_proj[name]
        for dep in p.get("depends_on", []):
            visit(dep, path + [name])
        visited[name] = 1
        order.append(p)

    for p in projects:
        visit(p["name"], [])
    return order


# ---------------------------------------------------------------------------
# Domain prefix enforcement
# ---------------------------------------------------------------------------

def _prefix_domain_with_project(data: Dict[str, Any], project_name: str) -> int:
    """In-place rewrite every function's domain to start with project_name.

    Also updates the `id` field (legacy string ID) to use the new
    domain-prefixed format, and remaps edges' source/target fields.

    Returns the number of functions updated.
    """
    prefix = project_name + "."
    updated = 0
    # Build old_id -> new_id map for edge remapping
    id_remap: Dict[str, str] = {}
    for fn in data.get("functions", []):
        if not isinstance(fn, dict):
            continue
        old_id = fn.get("id", "")
        old_domain = fn.get("domain", "root")
        # Force prefix — avoid double-prefixing if already starts with project_name
        if old_domain == project_name or old_domain.startswith(prefix):
            new_domain = old_domain  # already prefixed
        elif old_domain == "root" or not old_domain:
            new_domain = project_name
        else:
            new_domain = prefix + old_domain
        fn["domain"] = new_domain
        # Regenerate legacy id from new domain + name
        name = fn.get("name", "")
        # Mirror _make_func_id: domain.replace('.', '_') + '_' + name.lower()
        new_id = new_domain.replace(".", "_") + "_" + _normalize_name(name).lower()
        fn["id"] = new_id
        if old_id:
            id_remap[old_id] = new_id
        updated += 1
    # Remap edges
    for edge in data.get("edges", []):
        if not isinstance(edge, dict):
            continue
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if src in id_remap:
            edge["source"] = id_remap[src]
        if tgt in id_remap:
            edge["target"] = id_remap[tgt]
    return updated


def _normalize_name(name: str) -> str:
    """Mirror _scanner/base.py _normalize_name for legacy id regeneration."""
    # Replace non-alphanumeric with underscore
    return re.sub(r'[^A-Za-z0-9_]+', '_', name).strip('_')


# ---------------------------------------------------------------------------
# Compile_commands.json merging
# ---------------------------------------------------------------------------

def _merge_compile_commands(project_entries: List[Dict[str, Any]],
                             tmpdir: str) -> Optional[str]:
    """Merge multiple compile_commands.json into a single temp file.

    Returns the path to the merged file, or None if no project has one.
    Entries are de-duplicated by (directory, file) key.
    """
    all_entries: List[Dict[str, Any]] = []
    seen: set = set()
    for p in project_entries:
        cc_path = p.get("compile_commands")
        if not cc_path or not os.path.exists(cc_path):
            continue
        try:
            with open(cc_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, json.JSONDecodeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        if not isinstance(entries, list):
            continue
        project_name = p["name"]
        for e in entries:
            if not isinstance(e, dict):
                continue
            directory = e.get("directory", "")
            file_path = e.get("file", "")
            key = (directory, file_path)
            if key in seen:
                continue
            seen.add(key)
            # Tag the entry with project name for traceability
            e.setdefault("_project", project_name)
            all_entries.append(e)
    if not all_entries:
        return None
    merged_path = os.path.join(tmpdir, "compile_commands.json")
    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(all_entries, f, ensure_ascii=False)
    return merged_path


# ---------------------------------------------------------------------------
# Reuse mode: import from existing C2D
# ---------------------------------------------------------------------------

def _import_from_existing_c2d(joint_db_path: str, existing_c2d_path: str,
                                project_name: str) -> Dict[str, int]:
    """Import nodes/edges from an existing C2D into the joint db.

    Domain is re-prefixed with project_name. Legacy id is regenerated.

    Returns counts: {functions_imported, edges_imported}
    """
    if not os.path.exists(joint_db_path):
        return {"functions_imported": 0, "edges_imported": 0,
                "error": "joint db does not exist"}
    existing_db = os.path.join(existing_c2d_path, "code2database.db")
    if not os.path.exists(existing_db):
        return {"functions_imported": 0, "edges_imported": 0,
                "error": f"existing c2d db not found: {existing_db}"}
    conn = sqlite3.connect(joint_db_path)
    conn.row_factory = sqlite3.Row
    counts = {"functions_imported": 0, "edges_imported": 0}
    try:
        conn.execute(f"ATTACH DATABASE '{existing_db}' AS src")
        _changes_before = conn.total_changes
        # Import functions with re-prefixed domain + regenerated legacy id.
        # Batch with executemany instead of per-row INSERT.
        rows = conn.execute(
            "SELECT id, name, domain, source_file, line_number, signature, "
            "labels, body_text_compressed, extra_json FROM src.functions"
        ).fetchall()
        _func_batch = []
        id_remap: Dict[str, str] = {}
        for r in rows:
            old_domain = r["domain"] or "root"
            if old_domain == "root" or not old_domain:
                new_domain = project_name
            elif old_domain.startswith(project_name + "."):
                new_domain = old_domain
            else:
                new_domain = project_name + "." + old_domain
            name = r["name"] or ""
            new_id = new_domain.replace(".", "_") + "_" + _normalize_name(name).lower()
            id_remap[r["id"]] = new_id
            _func_batch.append((
                new_id, name, new_domain, r["source_file"],
                r["line_number"], r["signature"], r["labels"],
                r["body_text_compressed"], r["extra_json"]
            ))
        if _func_batch:
            conn.executemany(
                "INSERT OR IGNORE INTO functions "
                "(id, name, domain, source_file, line_number, signature, "
                "labels, body_text_compressed, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _func_batch
            )
            counts["functions_imported"] = conn.total_changes - _changes_before
            _changes_before = conn.total_changes
        # Import edges with remapped IDs using executemany.
        edge_rows = conn.execute(
            "SELECT invoker_id, invoked_id, relation, call_order, "
            "call_condition, concurrency, confidence, confidence_score, "
            "source, evidence, invoked_arg_json, reg_args_json, "
            "vtable_type, vtable_bound_module FROM src.edges"
        ).fetchall()
        _edge_batch = []
        for r in edge_rows:
            new_invoker = id_remap.get(r["invoker_id"], r["invoker_id"])
            new_invoked = id_remap.get(r["invoked_id"], r["invoked_id"])
            _edge_batch.append((
                new_invoker, new_invoked, r["relation"],
                r["call_order"], r["call_condition"], r["concurrency"],
                r["confidence"], r["confidence_score"], r["source"],
                r["evidence"], r["invoked_arg_json"], r["reg_args_json"],
                r["vtable_type"], r["vtable_bound_module"]
            ))
        if _edge_batch:
            conn.executemany(
                "INSERT OR IGNORE INTO edges (invoker_id, invoked_id, relation, "
                "call_order, call_condition, concurrency, confidence, "
                "confidence_score, source, evidence, invoked_arg_json, "
                "reg_args_json, vtable_type, vtable_bound_module) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _edge_batch
            )
            counts["edges_imported"] = conn.total_changes - _changes_before
        conn.execute("DETACH DATABASE src")
        conn.commit()
    except sqlite3.Error as e:
        counts["error"] = str(e)
    finally:
        conn.close()
    return counts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_multi(manifest_path: str, outdir: str, jobs: int = 0,
                max_workers: int = 0,
                force_rescan: Optional[List[str]] = None,
                no_clang: bool = False, verbose: bool = True) -> Dict[str, Any]:
    """Build a unified C2D from a multi-project manifest.

    Args:
        manifest_path: Path to manifest JSON.
        outdir: Output directory for the joint C2D.
        jobs: Parallel workers (0=auto).
        force_rescan: List of project names to force re-scan
                      (ignore existing_c2d).
        no_clang: Force tree-sitter (no libclang).
        verbose: Print progress.

    Returns: summary dict with per-project counts.
    """
    force_rescan = force_rescan or []
    manifest = _parse_manifest(manifest_path)
    projects = _topo_sort(manifest["projects"])
    os.makedirs(outdir, exist_ok=True)
    # Temp dir for merged compile_commands + per-project extractions
    tmpdir = tempfile.mkdtemp(prefix="c2d_multi_")
    summary: Dict[str, Any] = {
        "manifest": manifest_path,
        "outdir": outdir,
        "projects": [],
        "started_at": datetime.now().isoformat(),
    }
    # Step 1: Collect all include paths + merge compile_commands
    all_include_paths: List[str] = []
    scan_projects: List[Dict[str, Any]] = []
    reuse_projects: List[Dict[str, Any]] = []
    for p in projects:
        # Add this project's include_paths to the global list
        for ip in p.get("include_paths", []):
            if ip not in all_include_paths:
                all_include_paths.append(ip)
        # Decide: scan or reuse?
        existing_c2d = p.get("existing_c2d")
        if existing_c2d and p["name"] not in force_rescan:
            # Check freshness
            freshness_hours = p.get("rescan_if_older_than_hours", 0)
            db_path = os.path.join(existing_c2d, "code2database.db")
            if os.path.exists(db_path) and freshness_hours > 0:
                mtime = os.path.getmtime(db_path)
                age_hours = (datetime.now().timestamp() - mtime) / 3600
                if age_hours > freshness_hours:
                    if verbose:
                        print(f"[build-multi] {p['name']}: existing c2d "
                              f"stale ({age_hours:.1f}h > {freshness_hours}h), "
                              f"will re-scan", file=sys.stderr)
                    scan_projects.append(p)
                    continue
            reuse_projects.append(p)
        else:
            scan_projects.append(p)
    # Step 2: Merge compile_commands.json from all projects that have one
    merged_cc_path = _merge_compile_commands(scan_projects + reuse_projects, tmpdir)
    # Step 3: Scan each scan-project, prefix domain, accumulate joint extraction
    joint_extraction: Dict[str, Any] = {"functions": [], "edges": [],
                                        "globals": {}, "vtables": [],
                                        "imports": []}
    for p in scan_projects:
        project_name = p["name"]
        source = p.get("source")
        if not source or not os.path.isdir(source):
            summary["projects"].append({
                "name": project_name, "mode": "scan",
                "error": f"source not found: {source}",
            })
            continue
        if verbose:
            print(f"[build-multi] scanning {project_name} from {source}",
                  file=sys.stderr)
        # Build scanner args
        scan_kwargs: Dict[str, Any] = {
            "source_root": source,
            "lang": "auto",
            "workers": jobs,
            "max_workers": max_workers or jobs,
        }
        # Macros
        macros = p.get("macros", [])
        if macros:
            scan_kwargs["macro_bindings"] = {
                m.split('=', 1)[0]: (m.split('=', 1)[1] if '=' in m else "")
                for m in macros
            }
        # Clang args from include_paths
        clang_args: List[str] = []
        for ip in all_include_paths:
            clang_args.append(f"-I{ip}")
        if clang_args:
            scan_kwargs["clang_args"] = clang_args
        if merged_cc_path:
            scan_kwargs["compile_commands_path"] = merged_cc_path
        if no_clang:
            scan_kwargs["extraction_backend"] = "tree-sitter"
        # Run the scan
        try:
            # Local import to avoid scanner import side-effects at module load
            from code2database_scanner import scan_directory
            project_data = scan_directory(**scan_kwargs)
        except Exception as e:
            summary["projects"].append({
                "name": project_name, "mode": "scan",
                "error": f"scan failed: {e}",
            })
            continue
        # Force project-name domain prefix
        n_updated = _prefix_domain_with_project(project_data, project_name)
        # Merge into joint extraction
        joint_extraction["functions"].extend(project_data.get("functions", []))
        joint_extraction["edges"].extend(project_data.get("edges", []))
        # Merge globals (project-prefixed keys)
        for k, v in (project_data.get("globals") or {}).items():
            joint_extraction["globals"][f"{project_name}.{k}"] = v
        if project_data.get("vtables"):
            joint_extraction["vtables"].extend(project_data["vtables"])
        if project_data.get("imports"):
            joint_extraction["imports"].extend(project_data["imports"])
        summary["projects"].append({
            "name": project_name, "mode": "scan",
            "functions": len(project_data.get("functions", [])),
            "edges": len(project_data.get("edges", [])),
            "domain_prefix": project_name,
        })
    # Step 4: Write joint extraction JSON
    joint_extraction_path = os.path.join(tmpdir, "joint_extraction.json")
    with open(joint_extraction_path, "w", encoding="utf-8") as f:
        json.dump(joint_extraction, f, ensure_ascii=False)
    # Step 5: Build the joint C2D using the full cmd_build pipeline.
    # We construct an argparse.Namespace with the fields cmd_build reads
    # (args.extraction, args.outdir, plus getattr-defaulted fields like
    # jobs, max_workers, build_config, etc.) and call cmd_build(args)
    # instead of build_graph(...) directly — build_graph's signature is
    # build_graph(extraction: dict, profile=None, graph=None) and does
    # NOT accept outdir/jobs, so the old call was a guaranteed TypeError.
    if joint_extraction["functions"]:
        try:
            import argparse
            from _builder.graph_build import cmd_build
            build_args = argparse.Namespace(
                extraction=joint_extraction_path,
                outdir=outdir,
                jobs=jobs,
                max_workers=max_workers or 0,
                build_config="auto",
                profile=None,
                macros=None,
                storage="sqlite",
                auto_enhance=False,
                large_project=False,
                low_memory=False,
                skip_community=False,
                plugin=None,
                profile_timing=False,
                max_domain_files=0,
                memory_warn_threshold=0.75,
                memory_crit_threshold=0.85,
            )
            cmd_build(build_args)
        except Exception as e:
            summary["build_error"] = str(e)
    # Step 6: Import from existing C2Ds (reuse mode)
    joint_db_path = os.path.join(outdir, "code2database.db")
    for p in reuse_projects:
        project_name = p["name"]
        existing_c2d = p["existing_c2d"]
        if verbose:
            print(f"[build-multi] importing {project_name} from "
                  f"{existing_c2d}", file=sys.stderr)
        counts = _import_from_existing_c2d(joint_db_path, existing_c2d, project_name)
        summary["projects"].append({
            "name": project_name, "mode": "reuse",
            "imported_from": existing_c2d,
            **counts,
        })
    # Step 7: Write manifest metadata to joint db
    if os.path.exists(joint_db_path):
        try:
            conn = sqlite3.connect(joint_db_path)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("multi_project_manifest", json.dumps(manifest, ensure_ascii=False))
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("multi_project_built_at", datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
        except sqlite3.Error:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    summary["finished_at"] = datetime.now().isoformat()
    summary["total_functions"] = sum(p.get("functions", 0) + p.get("functions_imported", 0)
                                       for p in summary["projects"])
    summary["total_edges"] = sum(p.get("edges", 0) + p.get("edges_imported", 0)
                                   for p in summary["projects"])
    # Cleanup tmpdir (best-effort)
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    return summary


def cmd_build_multi(args):
    """CLI handler for build-multi command."""
    summary = build_multi(
        manifest_path=args.manifest,
        outdir=args.outdir,
        jobs=getattr(args, "jobs", 0),
        force_rescan=[s.strip() for s in (args.force_rescan or "").split(",")
                      if s.strip()] or None,
        no_clang=getattr(args, "no_clang", False),
        verbose=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
