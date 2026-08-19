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
  - User can later run `knowledge-validate` to check if merged
    knowledge still holds against the target graph.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple


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
                            pass
                    funcs[row["id"]] = {
                        "name": row["name"],
                        "domain": row["domain"],
                        "source_file": row["source_file"],
                        "signature": row["signature"] or extra.get("signature", ""),
                    }
            except Exception:
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
    """Load knowledge entries from a graph directory."""
    knowledge_dir = os.path.join(graph_dir, ".code2database_knowledge")
    entries = []
    index_path = os.path.join(knowledge_dir, "index.json")
    if not os.path.exists(index_path):
        return entries
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, json.JSONDecodeError):
        return entries
    for entry_meta in index.get("entries", []):
        entry_id = entry_meta.get("id", "")
        if not entry_id:
            continue
        entry_path = os.path.join(knowledge_dir, "entries", f"{entry_id}.json")
        if os.path.exists(entry_path):
            try:
                with open(entry_path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                entries.append(entry)
            except (OSError, json.JSONDecodeError):
                pass
    return entries


def _load_memory(graph_dir: str) -> List[dict]:
    """Load memory entries from a graph directory."""
    memory_dir = os.path.join(graph_dir, ".code2database_memory")
    entries = []
    index_path = os.path.join(memory_dir, "index.json")
    if not os.path.exists(index_path):
        return entries
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, json.JSONDecodeError):
        return entries
    for entry_meta in index.get("entries", []):
        entry_id = entry_meta.get("id", "")
        if not entry_id:
            continue
        # Memory entries are stored in root/leaf structure
        root_path = os.path.join(memory_dir, "root", f"root_{entry_id}.json")
        if os.path.exists(root_path):
            try:
                with open(root_path, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                entries.append(entry)
            except (OSError, json.JSONDecodeError):
                pass
    return entries


def _match_function(
    source_func_name: str,
    source_func_fqn: str,
    source_signature: str,
    target_functions: Dict[str, dict],
) -> Tuple[Optional[str], bool]:
    """Match a source function to a target function.

    Returns (target_fqn, signature_compatible).
    """
    # Strategy 1: exact FQN match
    if source_func_fqn in target_functions:
        target_sig = target_functions[source_func_fqn].get("signature", "")
        sig_compat = _signatures_compatible(source_signature, target_sig)
        return source_func_fqn, sig_compat

    # Strategy 2: match by name (last component of FQN)
    source_name = source_func_name or source_func_fqn.split(".")[-1]
    for target_fqn, target_info in target_functions.items():
        if target_info.get("name") == source_name:
            target_sig = target_info.get("signature", "")
            sig_compat = _signatures_compatible(source_signature, target_sig)
            return target_fqn, sig_compat

    # Strategy 3: match by normalized FQN (strip domain prefix)
    source_norm = source_func_fqn.split("__")[-1] if "__" in source_func_fqn else source_func_fqn
    for target_fqn, target_info in target_functions.items():
        target_norm = target_fqn.split("__")[-1] if "__" in target_fqn else target_fqn
        if target_norm == source_norm:
            target_sig = target_info.get("signature", "")
            sig_compat = _signatures_compatible(source_signature, target_sig)
            return target_fqn, sig_compat

    return None, False


def _signatures_compatible(sig1: str, sig2: str) -> bool:
    """Check if two function signatures are compatible (same parameter count
    and types, ignoring whitespace differences)."""
    if not sig1 or not sig2:
        return True  # Can't compare, assume compatible
    # Normalize: remove whitespace, lowercase
    s1 = "".join(sig1.split()).lower()
    s2 = "".join(sig2.split()).lower()
    if s1 == s2:
        return True
    # Check parameter count
    try:
        p1 = sig1[sig1.index("(") + 1:sig1.rindex(")")] if "(" in sig1 else ""
        p2 = sig2[sig2.index("(") + 1:sig2.rindex(")")] if "(" in sig2 else ""
        count1 = len([p for p in p1.split(",") if p.strip()]) if p1.strip() else 0
        count2 = len([p for p in p2.split(",") if p.strip()]) if p2.strip() else 0
        return count1 == count2
    except (ValueError, IndexError):
        return True  # Can't parse, assume compatible


def _save_merged_knowledge(
    target_graph_dir: str,
    entries: List[dict],
    source_branch: str,
) -> int:
    """Save merged knowledge entries to the target graph directory."""
    knowledge_dir = os.path.join(target_graph_dir, ".code2database_knowledge")
    entries_dir = os.path.join(knowledge_dir, "entries")
    os.makedirs(entries_dir, exist_ok=True)
    index_path = os.path.join(knowledge_dir, "index.json")

    # Load existing index
    existing_index = {"entries": []}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                existing_index = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    existing_ids = {e.get("id") for e in existing_index.get("entries", [])}
    saved = 0
    for entry in entries:
        entry_id = entry.get("id", "")
        if not entry_id or entry_id in existing_ids:
            continue
        # Tag as merged
        entry["source"] = f"merged:from:{source_branch}"
        entry["merge_timestamp"] = _now_iso()
        entry_path = os.path.join(entries_dir, f"{entry_id}.json")
        try:
            with open(entry_path, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False, indent=2)
            existing_index["entries"].append({
                "id": entry_id,
                "function": entry.get("function", ""),
                "kind": entry.get("kind", ""),
                "source": entry["source"],
            })
            existing_ids.add(entry_id)
            saved += 1
        except OSError:
            pass

    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(existing_index, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return saved


def _save_merged_memory(
    target_graph_dir: str,
    entries: List[dict],
    source_branch: str,
) -> int:
    """Save merged memory entries to the target graph directory."""
    memory_dir = os.path.join(target_graph_dir, ".code2database_memory")
    root_dir = os.path.join(memory_dir, "root")
    os.makedirs(root_dir, exist_ok=True)
    index_path = os.path.join(memory_dir, "index.json")

    existing_index = {"entries": []}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                existing_index = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    existing_ids = {e.get("id") for e in existing_index.get("entries", [])}
    saved = 0
    for entry in entries:
        entry_id = entry.get("id", "")
        if not entry_id or entry_id in existing_ids:
            continue
        entry["source"] = f"merged:from:{source_branch}"
        entry["merge_timestamp"] = _now_iso()
        entry_path = os.path.join(root_dir, f"root_{entry_id}.json")
        try:
            with open(entry_path, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False, indent=2)
            existing_index["entries"].append({
                "id": entry_id,
                "topic": entry.get("topic", ""),
                "source": entry["source"],
            })
            existing_ids.add(entry_id)
            saved += 1
        except OSError:
            pass

    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(existing_index, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
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
        pass

    # Load target function names for matching
    target_functions = _load_graph_functions(target_graph_dir)
    if not target_functions:
        result.errors.append(
            f"Could not load target functions from {target_graph_dir}"
        )
        return result

    # Merge knowledge
    if merge_knowledge:
        source_knowledge = _load_knowledge(source_graph_dir)
        mergeable = []
        needs_review = []
        skipped = 0
        for entry in source_knowledge:
            func_fqn = entry.get("function", "") or entry.get("function_id", "")
            func_name = entry.get("function_name", "")
            source_sig = entry.get("signature", "")
            target_fqn, sig_compat = _match_function(
                func_name, func_fqn, source_sig, target_functions
            )
            if target_fqn is None:
                skipped += 1
                continue
            # Remap function reference to target's FQN
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
        mergeable = []
        skipped = 0
        for entry in source_memory:
            # Memory entries may reference function names or be topic-based
            func_ref = entry.get("function", "") or entry.get("node_id", "")
            if func_ref:
                target_fqn, _ = _match_function(
                    "", func_ref, "", target_functions
                )
                if target_fqn is None:
                    # Topic-based memory — merge if no function reference
                    if entry.get("topic"):
                        mergeable.append(entry)
                    else:
                        skipped += 1
                    continue
                entry["function"] = target_fqn
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
        print("  Run `knowledge-validate` to check if merged knowledge still holds.")
    return 0
