"""Cross-graph knowledge/memory merge — transfer borrowable content
between two cgdb instances of the same project (different branches).

Scenario: user has two checkouts of the same project on different git
branches, each with its own cgdb build. They want to transfer
knowledge/memory/invariants from the source branch to the target
branch without overwriting the target's graph structure.

What can be merged (borrowable content):
  - knowledge entries (invariants, preconditions, postconditions,
    state machines) — matched by function name, not by node ID
  - memory entries (Q&A history, debugging insights) — matched by
    function name or topic
  - doc-code alignment observations that may still apply
  - profile evolution suggestions

What is NOT merged:
  - node IDs / edge structure (different code → different graph)
  - function locations (different in each branch)
  - raw scan data (AST / CFG / data flow)

Match strategy:
  1. Match by function FQN (fully-qualified name) — most reliable
  2. Match by function name + signature hash — fallback
  3. Verify target function exists before copying
  4. If signature changed: copy with `needs_review` flag + lower confidence

Conflict resolution:
  - If target already has knowledge for the same function: keep target
    (don't overwrite), but add the source's knowledge as a supplement
    with `source: "merged:from:<branch>"` tag.
  - Knowledge merging is retired: the project brief is curated
    per-project and is never auto-merged across branches.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
import logging


@dataclass
class MergeResult:
    """Outcome of a cross-graph merge operation."""
    source_graph_dir: str
    target_graph_dir: str
    knowledge_entries_merged: int = 0
    knowledge_entries_skipped: int = 0
    knowledge_entries_needs_review: int = 0
    memory_entries_merged: int = 0
    memory_entries_skipped: int = 0
    invariants_merged: int = 0
    invariants_skipped: int = 0
    conflicts_resolved: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_graph_dir": self.source_graph_dir,
            "target_graph_dir": self.target_graph_dir,
            "knowledge_merged": self.knowledge_entries_merged,
            "knowledge_skipped": self.knowledge_entries_skipped,
            "knowledge_needs_review": self.knowledge_entries_needs_review,
            "memory_merged": self.memory_entries_merged,
            "memory_skipped": self.memory_entries_skipped,
            "invariants_merged": self.invariants_merged,
            "invariants_skipped": self.invariants_skipped,
            "conflicts_resolved": self.conflicts_resolved,
            "errors": self.errors,
        }


def _load_graph_functions(graph_dir: str) -> Dict[str, dict]:
    """Load function name → function info from a graph directory.

    Uses the master.json file to get function names and signatures.
    Returns a dict mapping FQN → {name, signature, domain, source_file}.
    """
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if not os.path.exists(master_path):
        # Try SQLite
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            funcs = {}
            try:
                rows = conn.execute(
                    "SELECT id, name, domain, source_file, signature, extra_json "
                    "FROM functions"
                ).fetchall()
                for row in rows:
                    extra = {}
                    if row["extra_json"]:
                        try:
                            extra = json.loads(row["extra_json"])
                        except (json.JSONDecodeError, TypeError):
                            logging.getLogger(__name__).debug("silent exception", exc_info=True)
                            pass
                    funcs[row["id"]] = {
                        "name": row["name"],
                        "domain": row["domain"],
                        "source_file": row["source_file"],
                        "signature": row["signature"] or extra.get("signature", ""),
                    }
            except Exception:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
            finally:
                conn.close()
            return funcs
        return {}

    try:
        with open(master_path, "r", encoding="utf-8") as f:
            master = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    funcs = {}
    domains = master.get("domains", {})
    for domain, domain_info in domains.items():
        domain_file = domain_info.get("file", "")
        if not domain_file:
            continue
        domain_path = os.path.join(graph_dir, domain_file)
        if not os.path.exists(domain_path):
            continue
        try:
            with open(domain_path, "r", encoding="utf-8") as f:
                domain_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            continue
        for func in domain_data.get("functions", []):
            fid = func.get("id", "")
            if fid:
                funcs[fid] = {
                    "name": func.get("name", ""),
                    "domain": func.get("domain", domain),
                    "source_file": func.get("source_file", ""),
                    "signature": func.get("signature", ""),
                }
    return funcs


def _load_knowledge(graph_dir: str) -> List[dict]:
    """Knowledge is now the per-project brief (knowledge/brief.json).

    Briefs are curated per project and are NOT auto-merged across
    branches — always returns []. Kept as a stub so merge_cross_graph's
    knowledge branch degrades to a no-op instead of crashing.
    """
    return []


def _load_memory(graph_dir: str) -> List[dict]:
    """Load memory entries from the SQLite memory store (memory.db).

    Returns ALL non-tombstone rows (active + experience) so merge
    logic can see the complete memory state. Returns [] when the
    source has no memory store yet.
    """
    db_path = os.path.join(graph_dir, "memory", "memory.db")
    if not os.path.exists(db_path):
        return []
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    entries = []
    try:
        rows = conn.execute(
            "SELECT * FROM memories WHERE status IN "
            "('active', 'experience') ORDER BY id").fetchall()
    except sqlite3.Error:
        conn.close()
        return []
    for r in rows:
        d = dict(r)
        for col in ("tags", "node_ids", "chains"):
            try:
                d[col] = json.loads(d.get(col) or "[]")
            except (json.JSONDecodeError, TypeError):
                d[col] = []
        entries.append(d)
    conn.close()
    return entries


def _save_merged_knowledge(
    target_graph_dir: str,
    entries: List[dict],
    source_branch: str,
) -> int:
    """Knowledge merging is retired — briefs are curated per project.

    Always returns 0. Kept so merge_cross_graph's knowledge branch
    (which now feeds it nothing) stays callable.
    """
    return 0


def _save_merged_memory(
    target_graph_dir: str,
    entries: List[dict],
    source_branch: str,
) -> int:
    """Save merged memory entries into the target memory store (DB).

    Entries are inserted via MemoryStore.add with --no-merge semantics
    (fresh roots in the target; node_ids never transfer across
    branches). The target user can run validate-memory to flag stale
    refs and promote/merge as needed.
    """
    from _builder.memory_store import MemoryStore
    store = MemoryStore(target_graph_dir)
    saved = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        question = entry.get("question", "")
        if not question:
            continue
        store.add(
            question=question,
            answer=entry.get("answer", ""),
            tags=entry.get("tags", []),
            chains=entry.get("chains", []),
            author=entry.get("author", ""),
            no_merge=True,
        )
        saved += 1
    return saved


def _now_iso() -> str:
    """Current ISO timestamp."""
    from datetime import datetime
    return datetime.now().isoformat()


def merge_cross_graph(
    source_graph_dir: str,
    target_graph_dir: str,
    merge_knowledge: bool = True,
    merge_memory: bool = True,
    merge_invariants: bool = True,
    dry_run: bool = False,
) -> MergeResult:
    """Merge borrowable content from source graph to target graph.

    The target graph's structure (nodes, edges, AST, CFG) is preserved.
    Only knowledge/memory/invariants are transferred, matched by function name.

    Args:
        source_graph_dir: Path to the source branch's graph directory.
        target_graph_dir: Path to the target branch's graph directory.
        merge_knowledge: If True, transfer knowledge entries.
        merge_memory: If True, transfer memory entries.
        merge_invariants: If True, transfer invariant entries.
        dry_run: If True, only report what would be merged without writing.

    Returns:
        MergeResult with counts and any errors.
    """
    result = MergeResult(
        source_graph_dir=source_graph_dir,
        target_graph_dir=target_graph_dir,
    )

    # Detect source branch name from git
    source_branch = "unknown_branch"
    source_root = os.path.dirname(source_graph_dir)
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=source_root,
        )
        if proc.returncode == 0:
            source_branch = proc.stdout.strip()
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    target_functions = _load_graph_functions(target_graph_dir)
    if not target_functions:
        result.errors.append(
            f"Could not load target functions from {target_graph_dir}"
        )
        return result

    # Merge knowledge
    if merge_knowledge:
        source_knowledge = _load_knowledge(source_graph_dir)
        # Knowledge is now the per-project brief — never auto-merged.
        # _load_knowledge returns [] so this branch is a no-op; kept for
        # result-field compatibility.
        mergeable = []
        needs_review = []
        skipped = 0
        for entry in source_knowledge:
            if entry.get("kind") == "knowledge_md":
                # Project-level knowledge: transfer directly
                mergeable.append(entry)
            else:
                # Legacy fact-level knowledge with function refs (kept for
                # backward compat with any old schema that might exist)
                func_fqn = entry.get("function", "") or entry.get("function_id", "")
                func_name = entry.get("function_name", "")
                source_sig = entry.get("signature", "")
                target_fqn, sig_compat = _match_function(
                    func_name, func_fqn, source_sig, target_functions
                )
                if target_fqn is None:
                    skipped += 1
                    continue
                entry["function"] = target_fqn
                if not sig_compat:
                    entry["needs_review"] = True
                    entry["review_reason"] = "function signature changed between branches"
                    needs_review.append(entry)
                else:
                    mergeable.append(entry)
        if not dry_run:
            saved = _save_merged_knowledge(
                target_graph_dir, mergeable + needs_review, source_branch
            )
            result.knowledge_entries_merged = saved
        else:
            result.knowledge_entries_merged = len(mergeable) + len(needs_review)
        result.knowledge_entries_skipped = skipped
        result.knowledge_entries_needs_review = len(needs_review)

    # Merge memory
    if merge_memory:
        source_memory = _load_memory(source_graph_dir)
        # Memory entries are project-level Q&A; transfer all (target user
        # can validate-memory against the target graph to flag stale refs).
        mergeable = []
        skipped = 0
        for entry in source_memory:
            if not isinstance(entry, dict):
                continue
            # Strip target-incompatible fields; the target will re-validate
            entry.pop("node_ids", None)  # node_ids don't transfer across branches
            entry.pop("root_id", None)
            mergeable.append(entry)
        if not dry_run:
            saved = _save_merged_memory(
                target_graph_dir, mergeable, source_branch
            )
            result.memory_entries_merged = saved
        else:
            result.memory_entries_merged = len(mergeable)
        result.memory_entries_skipped = skipped

    return result


def cmd_cgdb_merge_knowledge(args):
    """CLI handler for `code2database_builder.py cgdb-merge-knowledge`."""
    source = args.source_graph
    target = args.graph
    dry_run = getattr(args, "dry_run", False)
    merge_know = not getattr(args, "no_knowledge", False)
    merge_mem = not getattr(args, "no_memory", False)

    print(f"Merging from {source} → {target}")
    if dry_run:
        print("(dry run — no files will be written)")

    result = merge_cross_graph(
        source, target,
        merge_knowledge=merge_know,
        merge_memory=merge_mem,
        dry_run=dry_run,
    )

    print(f"\nMerge results:")
    print(f"  Knowledge entries merged:       {result.knowledge_entries_merged}")
    print(f"  Knowledge entries skipped:       {result.knowledge_entries_skipped}")
    print(f"  Knowledge entries needs review:  {result.knowledge_entries_needs_review}")
    print(f"  Memory entries merged:           {result.memory_entries_merged}")
    print(f"  Memory entries skipped:          {result.memory_entries_skipped}")
    if result.errors:
        print(f"\nErrors:")
        for err in result.errors:
            print(f"  - {err}")

    if result.knowledge_entries_needs_review > 0:
        print(f"\n⚠ {result.knowledge_entries_needs_review} entries need review")
        print("  Review the target project brief (knowledge-brief) manually.")
    return 0
