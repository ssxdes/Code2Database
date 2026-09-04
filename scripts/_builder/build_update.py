#!/usr/bin/env python3
"""build-update — precise per-file graph DB updates from disk changes.

The missing incremental piece between `scan --incremental` (extraction
JSON only) and full `build`: detect changed files (content hash vs the
cgdb_files baseline — language-agnostic, unlike the C/C++-only include
walk), rescan just those TUs (plus the #include closure for changed
C/C++ headers), and rewrite their records:

  - cgdb 13-layer tables: delete_file_records (cascading) + write_batch
    (id-keyed, INSERT OR REPLACE)
  - legacy functions: delete by source_file + INSERT OR REPLACE by id
  - legacy edges / field_access / global_access: delete via the file's
    old function ids, then re-insert the rescan's (scan-level) edges

Full `build` (via make) remains the fidelity bar — it runs the whole
inference pipeline (cross-file resolution, vtable dispatch, community
detection). build-update trades that for turnaround: the SQLite graph
tracks source edits in seconds. Derived artifacts (indexes, docs,
embeddings) refresh on the next make/build.
"""

import json
import os
import sqlite3
import sys

_SKIP_DIRS = ('.git', '.svn', 'build', 'dist', 'out', '__pycache__',
              '.cache', 'third_party', 'vendor', 'code2db-out')

_C_CPP_HDR_EXTS = ('.h', '.hpp', '.hh', '.hxx')


def _code_extensions():
    """All source extensions the scanners handle."""
    from _scanner.utils import LANG_EXTENSIONS
    exts = set()
    for ext_set in LANG_EXTENSIONS.values():
        exts |= set(ext_set)
    return exts


def _content_hash(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def detect_db_changes(source_root: str, db_path: str) -> dict:
    """Language-agnostic change detection vs the cgdb_files baseline.

    Returns {changed, added, deleted} ABSOLUTE file path lists plus
    `deleted_stored` (the path form recorded in cgdb_files, which the
    scanner writes relative to source_root). Files in the source tree
    with a known code extension are compared by content hash against
    cgdb_files.content_hash (stored paths are normalized to absolute
    for comparison); unknown-to-the-DB files are new; DB files absent
    from disk are deleted.
    """
    source_root = os.path.abspath(source_root)
    raw_stored = {}
    conn = sqlite3.connect(db_path)
    try:
        for path, h in conn.execute(
                "SELECT path, content_hash FROM cgdb_files").fetchall():
            raw_stored[path] = h or ''
    except sqlite3.OperationalError:
        pass  # no cgdb_files table — everything is new
    finally:
        conn.close()

    # The scanner records paths relative to source_root; builds may
    # also contain absolute rows. Normalize to absolute for comparison
    # but keep the stored form for row deletion.
    stored = {}  # abspath -> (stored_path, hash)
    for p, h in raw_stored.items():
        ap = p if os.path.isabs(p) else os.path.abspath(
            os.path.join(source_root, p))
        prev = stored.get(ap)
        if prev is None or not prev[1]:
            stored[ap] = (p, h)

    exts = _code_extensions()
    changed, added = [], []
    seen = set()
    for root, dirs, files in os.walk(source_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if os.path.splitext(fname)[1].lower() not in exts:
                continue
            fpath = os.path.abspath(os.path.join(root, fname))
            seen.add(fpath)
            if fpath in stored:
                if _content_hash(fpath) != stored[fpath][1]:
                    changed.append(fpath)
            else:
                added.append(fpath)
    deleted, deleted_stored = [], []
    for ap, (orig, _h) in stored.items():
        if ap not in seen and not os.path.exists(ap):
            deleted.append(ap)
            deleted_stored.append(orig)
    return {"changed": sorted(changed), "added": sorted(added),
            "deleted": sorted(deleted),
            "deleted_stored": sorted(deleted_stored)}


def _stored_forms(file_path: str, source_root: str):
    """Both path forms a file may be recorded under (scanner emits
    source-root-relative paths; some rows are absolute)."""
    rel = os.path.relpath(file_path, source_root)
    return [file_path, rel]


def _old_function_ids(conn, file_path, source_root):
    """All legacy rows owned by a file: functions with matching
    source_file, the 'file:' pseudo-node, and synthetic nodes reachable
    only via its CONTAINS edges (e.g. <conditional:...> loop nodes the
    builder synthesizes — they carry no source_file of their own)."""
    ids = []
    for form in _stored_forms(file_path, source_root):
        for r in conn.execute(
                "SELECT id FROM functions WHERE source_file = ?",
                (form,)).fetchall():
            ids.append(r[0])
        fnode = "file:" + form
        row = conn.execute("SELECT id FROM functions WHERE id = ?",
                           (fnode,)).fetchone()
        if row:
            ids.append(row[0])
            ids.extend(r[0] for r in conn.execute(
                "SELECT invoked_id FROM edges WHERE invoker_id = ? "
                "AND relation = 'CONTAINS'", (fnode,)).fetchall())
    return list(dict.fromkeys(ids))


def _delete_legacy_rows(conn, file_path, source_root):
    """Remove a file's legacy functions/edges/access rows. Returns the
    number of function rows removed."""
    old_ids = _old_function_ids(conn, file_path, source_root)
    if not old_ids:
        return 0
    qmarks = ",".join("?" * len(old_ids))
    conn.execute(
        f"DELETE FROM edges WHERE invoker_id IN ({qmarks}) "
        f"OR invoked_id IN ({qmarks})", old_ids + old_ids)
    for table in ("field_access", "global_access"):
        try:
            conn.execute(
                f"DELETE FROM {table} WHERE function_id IN ({qmarks})",
                old_ids)
        except sqlite3.OperationalError:
            pass  # table may not exist in older graphs
    for form in _stored_forms(file_path, source_root):
        conn.execute("DELETE FROM functions WHERE source_file = ?",
                     (form,))
    q2 = ",".join("?" * len(old_ids))
    conn.execute(f"DELETE FROM functions WHERE id IN ({q2})", old_ids)
    return len(old_ids)


def _store_resolved_edges(conn, store, raw_edges) -> int:
    """Best-effort resolution of raw scanner edges against the DB.

    Raw edges name callees ('add', not 'src_util_add') and reference
    builder-synthesized nodes (<conditional:...>) that don't exist as
    rows. Writing them raw would pollute the edges table with dangling
    endpoints, so both ends must resolve — by exact id, or by a
    globally-unique function name (the builder's unique_name strategy;
    ambiguous names are conservatively dropped).
    """
    ids = set()
    by_name = {}
    for r in conn.execute("SELECT id, name FROM functions").fetchall():
        ids.add(r[0])
        if r[1]:
            by_name.setdefault(r[1], set()).add(r[0])

    def _resolve(token):
        if token in ids:
            return token
        cands = by_name.get(token) or ()
        if len(cands) == 1:
            return next(iter(cands))
        return None

    resolved = []
    for e in raw_edges:
        inv = (e.get("invoker") or e.get("caller") or e.get("source")
               or "")
        cal = (e.get("invoked") or e.get("callee") or e.get("target")
               or "")
        inv_id = _resolve(inv)
        cal_id = _resolve(cal)
        if not inv_id or not cal_id or inv_id == cal_id:
            continue
        resolved.append({
            "invoker": inv_id, "invoked": cal_id,
            "relation": e.get("relation") or "INVOKES",
            "call_order": e.get("call_order"),
            "call_condition": e.get("call_condition", ""),
            "confidence": "EXTRACTED",
        })
    if resolved:
        store.store_edges(resolved, autocommit=False)
    return len(resolved)


def _write_file_node(conn, store, rel_form, functions) -> None:
    """Re-create the 'file:' pseudo-node + CONTAINS edges the builder
    synthesizes per file (drives UI file grouping)."""
    if not functions:
        return
    fnode_id = "file:" + rel_form
    domain = functions[0].get("domain", "")
    conn.execute(
        "INSERT OR REPLACE INTO functions "
        "(id, name, domain, source_file, node_type, is_empty, labels) "
        "VALUES (?, ?, ?, ?, 'file', 0, '[]')",
        (fnode_id, os.path.basename(rel_form), domain, rel_form))
    contains = [{"invoker": fnode_id, "invoked": f["id"],
                 "relation": "CONTAINS", "confidence": "EXTRACTED"}
                for f in functions if f.get("id")]
    if contains:
        store.store_edges(contains, autocommit=False)


def _cgdb_delete_file(cgdb_store, conn, file_path, source_root):
    """delete_file_records for whichever stored path form exists."""
    for form in _stored_forms(file_path, source_root):
        row = conn.execute(
            "SELECT id FROM cgdb_files WHERE path = ?", (form,)).fetchone()
        if row:
            return cgdb_store.delete_file_records(form)
    return (0, 0)


def _scan_one(file_path, source_root, extraction_backend=None):
    from code2database_scanner import scan_files
    return scan_files([file_path], source_root,
                      extraction_backend=extraction_backend)


def _commit_hash(source_root):
    try:
        from _builder.graph_build import _detect_commit_hash
        return _detect_commit_hash(source_root) or "unknown"
    except Exception:
        return "unknown"


def build_update(source_root: str, graph_dir: str,
                 extraction_backend: str = None,
                 dry_run: bool = False) -> dict:
    """Apply per-file updates to the SQLite graph. Returns a report."""
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        raise RuntimeError(
            f"no SQLite graph at {db_path} — build-update requires a "
            "SQLite-backed graph (build --storage sqlite); JSON-storage "
            "graphs must use the full build")

    changes = detect_db_changes(source_root, db_path)
    changed = changes["changed"]
    added = changes["added"]
    deleted = changes["deleted"]

    # C/C++ header edits re-scan every TU that (transitively) includes
    # the header — a struct change alters extraction of dependents.
    hdr_changed = [p for p in changed
                   if os.path.splitext(p)[1].lower() in _C_CPP_HDR_EXTS]
    affected = set(changed) | set(added)
    if hdr_changed:
        try:
            from _builder.cgdb_incremental import IncrementalSync
            sync = IncrementalSync(source_root)
            for tu in sync.compute_affected_tus(hdr_changed):
                if os.path.exists(tu):
                    affected.add(tu)
        except Exception as exc:
            print(f"[build-update] include-closure expansion failed "
                  f"({exc}); updating only the changed files",
                  file=sys.stderr)

    report = {"changed_files": len(changed), "added_files": len(added),
              "deleted_files": len(deleted),
              "affected_tus": sorted(affected),
              "updated_files": 0, "removed_functions": 0,
              "written_functions": 0, "written_edges": 0}
    if dry_run:
        report["dry_run"] = True
        report["updated_files"] = len(affected)
        return report

    if not affected and not deleted:
        return report

    from _builder.sqlite_store import SQLiteStore
    from _builder.cgdb_store import SQLiteCGDBStore
    from _builder.cgdb_ingest import extract_cgdb_batch

    store = SQLiteStore(db_path)
    # WAL recovery on WSL1 can briefly fail the journal-mode PRAGMA when
    # another process (web UI, a just-exited build) still holds the shm;
    # retry instead of failing the update.
    import time as _time
    for attempt in range(3):
        try:
            store.connect()
            break
        except sqlite3.OperationalError as exc:
            if "lock" not in str(exc).lower() or attempt == 2:
                raise
            _time.sleep(1.0 + attempt)
    conn = store._conn
    cgdb_store = SQLiteCGDBStore(db_path, conn=conn)
    commit = _commit_hash(source_root)
    bulk_ok = False
    cgdb_store.begin_bulk_load()
    try:
        for fp in sorted(affected):
            result = _scan_one(fp, source_root,
                               extraction_backend=extraction_backend)
            # scan_files aggregates; for a single file the aggregate IS
            # the per-file result — stamp the file key extract_cgdb_batch
            # requires (it returns an empty batch without one). Absolute
            # form: extract_cgdb_batch reads the file to hash it, and the
            # build's relative-path batches left content_hash empty
            # whenever the build CWD differed from the source root
            # (empty hashes make files look perpetually changed).
            result["file"] = fp
            report["removed_functions"] += _delete_legacy_rows(
                conn, fp, source_root)
            _cgdb_delete_file(cgdb_store, conn, fp, source_root)
            functions = result.get("functions") or []
            if functions:
                store.store_functions(functions, autocommit=False)
                report["written_functions"] += len(functions)
            edges = result.get("edges") or []
            if edges:
                report["written_edges"] += _store_resolved_edges(
                    conn, store, edges)
            _write_file_node(conn, store, result["file"], functions)
            batch = extract_cgdb_batch(result, commit_hash=commit)
            if batch.file and batch.file.path:
                cgdb_store.write_batch(batch)
            report["updated_files"] += 1
        for fp in deleted:
            report["removed_functions"] += _delete_legacy_rows(
                conn, fp, source_root)
            _cgdb_delete_file(cgdb_store, conn, fp, source_root)
        cgdb_store.finalize()
        bulk_ok = True
    finally:
        if not bulk_ok:
            try:
                cgdb_store.abort_bulk_load()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    # Record a version so time-travel can distinguish build-update
    # snapshots from full builds.
    try:
        from _builder.cgdb_versions import VersionController
        VersionController(db_path).record_version(
            commit_hash=commit,
            commit_subject=(f"build-update: {report['updated_files']} "
                            f"updated, {report['deleted_files']} deleted"),
        )
    except Exception as exc:
        print(f"[build-update] version record skipped: {exc}",
              file=sys.stderr)

    # Refresh the fingerprint manifest so freshness checks (session-init,
    # web UI badge) reflect the synced state instead of reporting stale.
    try:
        from _scanner.changes import save_manifest
        save_manifest(source_root, graph_dir)
    except Exception as exc:
        print(f"[build-update] manifest refresh skipped: {exc}",
              file=sys.stderr)
    return report


def cmd_build_update(args):
    """CLI entry: build-update --source SRC --graph DIR."""
    graph_dir = args.graph
    source_root = os.path.abspath(args.source)
    if not os.path.isdir(source_root):
        print(f"Error: source directory not found: {source_root}",
              file=sys.stderr)
        sys.exit(1)
    try:
        report = build_update(
            source_root, graph_dir,
            extraction_backend=getattr(args, "extraction_backend", None),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"build-update: {report['changed_files']} changed, "
          f"{report['added_files']} added, {report['deleted_files']} deleted")
    if report.get("dry_run"):
        print(f"  (dry run) would rescan {len(report['affected_tus'])} TU(s)")
        return
    print(f"  rescaned {report['updated_files']} file(s): "
          f"{report['removed_functions']} function row(s) removed, "
          f"{report['written_functions']} written")
