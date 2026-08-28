#!/usr/bin/env python3
"""Memory manager for Code2Database.

Memory is the LLM's OWN persistent brain on disk. It needs algorithmic
correctness + easy LLM extraction via multi-layer packs, not human readability.
Format: JSON with layered packs. Storage: code2db-out/memory/

Provides a layered memory system with:
- Persistent memory (code2db-out/memory/): root/leaf structure, decay, merge
- Temporary memory (code2db-out/.scratch/): session-scoped with auto-expiry
- Script-based operations (add/correct/reshape/decay/promote/refine/consolidate)
- Multi-tier memory_pack extraction (lite/standard/deep/full)

Can be used standalone or via code2database_builder.py manage-memory command.
"""

import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import fcntl  # POSIX only; gracefully degrade on non-POSIX
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

from _builder.utils import _simple_tokenize, _similarity_score, _extract_chain_node_ids
import logging


# Decay parameters
DECAY_LAMBDA = 0.05        # ~14 days to 50% weight
MERGE_BONUS = 0.10         # +0.10 per merge
LENGTH_BONUS_FACTOR = 0.05  # +0.05 per 1000 chars answer length
ACCESS_BONUS = 0.10         # +0.10 per access
MERGE_SIMILARITY_THRESHOLD = 0.7


def _atomic_write_locked(path: str, content: str):
    """O21: Atomic file write with exclusive lock.

    Writes to a temp file then renames into place (atomic on POSIX). An
    exclusive flock is held during the write to prevent concurrent writes
    from interleaving. On non-POSIX systems (no fcntl), falls back to
    plain write_text — still atomic via rename, but without cross-process
    locking.
    """
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = str(path_obj) + ".tmp." + str(os.getpid())
    # Write to temp file
    Path(tmp_path).write_text(content, encoding="utf-8")
    # Hold an exclusive lock on the target path during rename
    if _HAS_FCNTL:
        lock_path = str(path_obj) + ".lock"
        with open(lock_path, "w") as lock_fd:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
                os.replace(tmp_path, str(path_obj))
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
    else:
        os.replace(tmp_path, str(path_obj))


def _locked_read(path: str) -> str:
    """O21: Read a file with a shared lock to avoid torn reads.

    Returns the file content as a string. On non-POSIX systems, falls back
    to plain read_text.
    """
    if _HAS_FCNTL:
        lock_path = path + ".lock"
        with open(lock_path, "w") as lock_fd:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_SH)
                return Path(path).read_text(encoding="utf-8")
            finally:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
    return Path(path).read_text(encoding="utf-8")


class MemoryManager:
    """Manages persistent and temporary memory for callgraph analysis."""

    def __init__(self, graph_dir: str):
        self.graph_dir = graph_dir
        self.mem_dir = os.path.join(graph_dir, "memory")
        self.exp_dir = os.path.join(graph_dir, "memory", "experience")
        self.root_dir = os.path.join(graph_dir, "memory", "root")
        self.leaf_dir = os.path.join(graph_dir, "memory", "leaf")
        self.scratch_dir = os.path.join(graph_dir, ".scratch")
        os.makedirs(self.mem_dir, exist_ok=True)
        os.makedirs(self.exp_dir, exist_ok=True)
        os.makedirs(self.root_dir, exist_ok=True)
        os.makedirs(self.leaf_dir, exist_ok=True)
        os.makedirs(self.scratch_dir, exist_ok=True)

    # -----------------------------------------------------------------------
    # Index management
    # -----------------------------------------------------------------------

    def _load_index(self) -> dict:
        path = os.path.join(self.mem_dir, "index.json")
        if os.path.exists(path):
            index = json.loads(_locked_read(path))
            # Sanitize entries: filter out non-dict items (defensive against
            # corrupt/legacy memory.json files where entries may be plain
            # strings instead of {"id": ..., "question": ...} dicts).
            # Without this, downstream loops call entry_meta.get("status")
            # which crashes with `AttributeError: 'str' object has no
            # attribute 'get'` (seen in inline memory-merge scripts run on
            # another environment).
            if isinstance(index, dict) and isinstance(index.get("entries"), list):
                _orig_len = len(index["entries"])
                index["entries"] = [
                    e for e in index["entries"] if isinstance(e, dict)
                ]
                if len(index["entries"]) != _orig_len:
                    print(f"[memory] Warning: filtered {_orig_len - len(index['entries'])} "
                          f"non-dict entries from {path}", file=sys.stderr)
            elif not isinstance(index, dict):
                # Totally corrupt index file — return empty default
                print(f"[memory] Warning: index at {path} is not a dict, "
                      f"returning empty default", file=sys.stderr)
                return {"entries": [], "next_id": 1, "roots": []}
            else:
                # dict but missing 'entries' key
                index.setdefault("entries", [])
                index.setdefault("next_id", 1)
                index.setdefault("roots", [])
            return index
        return {"entries": [], "next_id": 1, "roots": []}

    def _save_index(self, index: dict):
        path = os.path.join(self.mem_dir, "index.json")
        _atomic_write_locked(path,
            json.dumps(index, ensure_ascii=False, indent=2) + "\n")

    def _load_exp_index(self) -> dict:
        path = os.path.join(self.exp_dir, "index.json")
        if os.path.exists(path):
            index = json.loads(_locked_read(path))
            # Same defensive sanitize as _load_index — experience entries
            # can also be corrupted by inline merge scripts.
            if isinstance(index, dict) and isinstance(index.get("entries"), list):
                _orig_len = len(index["entries"])
                index["entries"] = [
                    e for e in index["entries"] if isinstance(e, dict)
                ]
                if len(index["entries"]) != _orig_len:
                    print(f"[memory] Warning: filtered {_orig_len - len(index['entries'])} "
                          f"non-dict entries from {path}", file=sys.stderr)
            elif not isinstance(index, dict):
                print(f"[memory] Warning: exp index at {path} is not a dict, "
                      f"returning empty default", file=sys.stderr)
                return {"entries": []}
            else:
                index.setdefault("entries", [])
            return index
        return {"entries": []}

    def _save_exp_index(self, index: dict):
        path = os.path.join(self.exp_dir, "index.json")
        _atomic_write_locked(path,
            json.dumps(index, ensure_ascii=False, indent=2) + "\n")

    # -----------------------------------------------------------------------
    # Weight calculation
    # -----------------------------------------------------------------------

    def _compute_weight(self, entry: dict) -> float:
        """Compute memory weight using recency * importance * access factors."""
        base = 1.0
        created = entry.get("created", "")
        last_access = entry.get("last_accessed", entry.get("validated_at", created))
        now = time.time()

        # Recency factor (exp decay)
        try:
            from datetime import datetime
            if last_access:
                dt = datetime.fromisoformat(last_access)
                days = (now - dt.timestamp()) / 86400
            else:
                days = 365  # unknown = old
        except (ValueError, OSError):
            days = 365
        recency = math.exp(-DECAY_LAMBDA * max(0, days))

        # Importance factor (merge count + answer length)
        merged_count = entry.get("merged_count", 0)
        answer_len = len(entry.get("answer", ""))
        importance = 1.0 + merged_count * MERGE_BONUS + (answer_len / 1000) * LENGTH_BONUS_FACTOR

        # Access factor
        access_count = entry.get("access_count", 0)
        access = 1.0 + ACCESS_BONUS * access_count

        weight = base * recency * importance * access
        return min(weight, 10.0)  # cap at 10.0

    def _update_weight(self, entry: dict) -> dict:
        """Recompute and store weight in entry."""
        entry["weight"] = round(self._compute_weight(entry), 4)
        return entry

    # -----------------------------------------------------------------------
    # Layered index
    # -----------------------------------------------------------------------

    def _build_layered_index(self, index: dict) -> dict:
        """Build L0/L1/L2 indexes based on weights."""
        l0, l1, l2 = [], [], []
        for entry_meta in index["entries"]:
            eid = entry_meta["id"]
            entry = self._load_entry(eid)
            if not entry:
                continue
            w = entry.get("weight", 1.0)
            meta = {"id": eid, "question": entry_meta.get("question", ""),
                    "weight": w, "status": entry_meta.get("status", "trusted")}
            if w > 0.7:
                l0.append(meta)
            elif w > 0.3:
                l1.append(meta)
            else:
                l2.append(meta)

        return {"L0": l0, "L1": l1, "L2": l2}

    def _save_layered_indexes(self, index: dict):
        """Write L0/L1/L2 index files."""
        layered = self._build_layered_index(index)
        for level, entries in layered.items():
            path = os.path.join(self.mem_dir, f"{level}_index.json")
            Path(path).write_text(
                json.dumps({"entries": entries}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

    # -----------------------------------------------------------------------
    # Entry I/O
    # -----------------------------------------------------------------------

    def _entry_path(self, entry_id: int, is_root: bool = False) -> str:
        if is_root:
            return os.path.join(self.root_dir, f"root_{entry_id}.json")
        return os.path.join(self.leaf_dir, f"mem_{entry_id}.json")

    def _load_entry(self, entry_id: int, is_root: bool = False) -> dict:
        path = self._entry_path(entry_id, is_root)
        if os.path.exists(path):
            return json.loads(_locked_read(path))
        # Fallback: check old flat location
        old_path = os.path.join(self.mem_dir, f"memory_{entry_id}.json")
        if os.path.exists(old_path):
            return json.loads(_locked_read(old_path))
        return {}

    def _save_entry(self, entry: dict, is_root: bool = False):
        entry_id = entry.get("id", 0)
        path = self._entry_path(entry_id, is_root)
        _atomic_write_locked(path,
            json.dumps(entry, ensure_ascii=False, indent=2) + "\n")

    # -----------------------------------------------------------------------
    # Root memory merging
    # -----------------------------------------------------------------------

    def _find_root_match(self, question: str, index: dict) -> int:
        """Find root memory with similar question. Returns root_id or 0."""
        q_tokens = _simple_tokenize(question)
        best_id = 0
        best_score = 0.0
        for root_meta in index.get("roots", []):
            r_entry = self._load_entry(root_meta["id"], is_root=True)
            if not r_entry:
                continue
            r_tokens = _simple_tokenize(r_entry.get("question", ""))
            score = _similarity_score(q_tokens, r_tokens)
            if score > best_score and score >= MERGE_SIMILARITY_THRESHOLD:
                best_score = score
                best_id = root_meta["id"]
        return best_id

    def _merge_to_root(self, root_id: int, entry: dict):
        """Merge a leaf entry into an existing root memory."""
        root = self._load_entry(root_id, is_root=True)
        if not root:
            return

        # Save current root as version
        version_num = len(root.get("versions", [])) + 1
        version = {
            "answer": root.get("answer", ""),
            "created": root.get("created", ""),
            "merged_from": entry.get("id", 0),
            "version": version_num,
        }
        root.setdefault("versions", []).append(version)

        # New answer replaces root answer if it's stronger
        new_weight = self._compute_weight(entry)
        old_weight = self._compute_weight(root)
        if new_weight > old_weight or entry.get("answer", ""):
            root["answer"] = entry.get("answer", root.get("answer", ""))

        # Merge tags and node_ids
        root_tags = set(root.get("tags", []))
        root_tags.update(entry.get("tags", []))
        root["tags"] = sorted(root_tags)

        root_nodes = set(root.get("node_ids", []))
        root_nodes.update(entry.get("node_ids", []))
        root["node_ids"] = sorted(root_nodes)

        root["merged_count"] = root.get("merged_count", 0) + 1
        root["last_merged"] = entry.get("created", "")

        self._update_weight(root)
        self._save_entry(root, is_root=True)

    # -----------------------------------------------------------------------
    # Public API: memory operations
    # -----------------------------------------------------------------------

    def add(self, question: str, answer: str = "", tags: list = None,
            node_ids: list = None, chains: list = None,
            no_merge: bool = False) -> int:
        """Add a new memory entry. Returns entry ID."""
        from datetime import datetime

        tags = tags or []
        node_ids = node_ids or []
        chains = chains or []

        index = self._load_index()
        entry_id = index["next_id"]
        index["next_id"] += 1

        now = datetime.now().isoformat()
        entry = {
            "id": entry_id,
            "question": question,
            "answer": answer,
            "chains": chains,
            "node_ids": node_ids,
            "tags": tags,
            "status": "trusted",
            "weight": 1.0,
            "created": now,
            "last_accessed": now,
            "validated_at": now,
            "merged_count": 0,
            "access_count": 0,
            "root_id": 0,
            "knowledge_refs": [],
        }

        # Check for root memory merge
        root_id = self._find_root_match(question, index)
        if root_id and not no_merge:
            entry["root_id"] = root_id
            self._merge_to_root(root_id, entry)
            # Still save as leaf for traceability
            self._save_entry(entry, is_root=False)
            index["entries"].append({
                "id": entry_id, "question": question, "tags": tags,
                "status": "trusted", "root_id": root_id,
            })
            print(f"Merged with root #{root_id} as leaf #{entry_id}")
        else:
            # Create new root
            entry["root_id"] = entry_id  # self-referencing root
            self._save_entry(entry, is_root=True)
            index["entries"].append({
                "id": entry_id, "question": question, "tags": tags,
                "status": "trusted", "root_id": entry_id,
            })
            index.setdefault("roots", []).append({"id": entry_id, "question": question})
            print(f"Created root #{entry_id}")

        self._save_index(index)
        self._save_layered_indexes(index)
        return entry_id

    def correct(self, mem_id: int, field: str, value: str):
        """Correct a specific field of a memory entry."""
        entry = self._load_entry(mem_id, is_root=True)
        is_root = bool(entry)
        if not entry:
            entry = self._load_entry(mem_id, is_root=False)
            is_root = False

        if not entry:
            print(f"Memory #{mem_id} not found", file=sys.stderr)
            return

        # Save version before correction
        version_num = len(entry.get("versions", [])) + 1
        entry.setdefault("versions", []).append({
            "field": field,
            "old_value": entry.get(field, ""),
            "version": version_num,
            "corrected_at": datetime.now().isoformat(),
        })

        entry[field] = value
        self._save_entry(entry, is_root=is_root)
        print(f"Corrected #{mem_id}.{field}")

    def reshape(self, root_id: int, answer: str):
        """Replace the entire root memory answer with a stronger one."""
        root = self._load_entry(root_id, is_root=True)
        if not root:
            print(f"Root #{root_id} not found", file=sys.stderr)
            return

        # Save current as version
        version_num = len(root.get("versions", [])) + 1
        root.setdefault("versions", []).append({
            "answer": root.get("answer", ""),
            "version": version_num,
            "reshaped_at": datetime.now().isoformat(),
        })

        root["answer"] = answer
        root["reshaped_count"] = root.get("reshaped_count", 0) + 1
        self._update_weight(root)
        self._save_entry(root, is_root=True)
        print(f"Reshaped root #{root_id} (version {version_num})")

    def decay(self) -> int:
        """Run weight decay on all entries. Returns count of decayed entries."""
        index = self._load_index()
        decayed = 0
        archived = 0

        for entry_meta in list(index["entries"]):
            eid = entry_meta["id"]
            # Check root first
            entry = self._load_entry(eid, is_root=True)
            is_root = bool(entry)
            if not entry:
                entry = self._load_entry(eid, is_root=False)
                is_root = False
            if not entry:
                continue

            old_weight = entry.get("weight", 1.0)
            self._update_weight(entry)
            new_weight = entry["weight"]

            if abs(old_weight - new_weight) > 0.01:
                self._save_entry(entry, is_root=is_root)
                decayed += 1

            # Archive very low weight memories
            if new_weight < 0.1 and entry_meta.get("status") != "experience":
                entry["status"] = "experience"
                entry["archived_reason"] = "weight decayed below 0.1"
                entry["archived_at"] = datetime.now().isoformat()

                # Move to experience
                exp_path = os.path.join(self.exp_dir, f"experience_{eid}.json")
                Path(exp_path).write_text(
                    json.dumps(entry, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                # Remove from leaf/root
                old_path = self._entry_path(eid, is_root=False)
                if os.path.exists(old_path):
                    os.remove(old_path)
                old_root_path = self._entry_path(eid, is_root=True)
                if os.path.exists(old_root_path):
                    os.remove(old_root_path)

                entry_meta["status"] = "experience"
                archived += 1

                # Remove from roots list
                index["roots"] = [r for r in index.get("roots", []) if r["id"] != eid]

        if archived > 0:
            # Update experience index
            exp_index = self._load_exp_index()
            for em in index["entries"]:
                if em.get("status") == "experience":
                    if not any(e.get("id") == em["id"] for e in exp_index["entries"]):
                        exp_index["entries"].append(em)
            self._save_exp_index(exp_index)

        self._save_index(index)
        self._save_layered_indexes(index)

        print(f"Decay: {decayed} updated, {archived} archived to experience")
        return decayed

    def promote(self, mem_id: int, boost: float = 1.0):
        """Promote a memory by boosting its weight (reset decay)."""
        entry = self._load_entry(mem_id, is_root=True)
        is_root = bool(entry)
        if not entry:
            entry = self._load_entry(mem_id, is_root=False)
            is_root = False
        if not entry:
            print(f"Memory #{mem_id} not found", file=sys.stderr)
            return

        from datetime import datetime
        entry["last_accessed"] = datetime.now().isoformat()
        entry["access_count"] = entry.get("access_count", 0) + 1
        entry["weight"] = min(entry.get("weight", 1.0) + boost, 10.0)
        self._save_entry(entry, is_root=is_root)
        print(f"Promoted #{mem_id}: weight → {entry['weight']:.2f}")

    def query(self, query_text: str, top_n: int = 5, min_weight: float = 0.3) -> list:
        """Query memories by similarity and weight."""
        index = self._load_index()
        q_tokens = _simple_tokenize(query_text)
        scored = []

        for entry_meta in index["entries"]:
            if entry_meta.get("status") == "experience":
                continue
            eid = entry_meta["id"]
            e_tokens = _simple_tokenize(entry_meta.get("question", ""))
            tag_tokens = set()
            for tag in entry_meta.get("tags", []):
                tag_tokens |= _simple_tokenize(tag)

            sim = _similarity_score(q_tokens, e_tokens | tag_tokens)

            # Load weight
            entry = self._load_entry(eid, is_root=(entry_meta.get("root_id") == eid))
            if not entry:
                entry = self._load_entry(eid, is_root=False)
            w = entry.get("weight", 1.0) if entry else 1.0

            if w < min_weight:
                continue

            combined = sim * (0.5 + 0.5 * min(w / 2.0, 1.0))  # weight-normalized
            if combined > 0:
                scored.append({
                    "id": eid,
                    "root_id": entry_meta.get("root_id", 0),
                    "score": round(combined, 4),
                    "similarity": round(sim, 4),
                    "weight": round(w, 4),
                    "question": entry_meta.get("question", ""),
                    "answer": entry.get("answer", "") if entry else "",
                    "tags": entry_meta.get("tags", []),
                })

        scored.sort(key=lambda x: -x["score"])

        # Update access count for top results
        for r in scored[:top_n]:
            entry = self._load_entry(r["id"], is_root=(r.get("root_id") == r["id"]))
            if entry:
                from datetime import datetime
                entry["access_count"] = entry.get("access_count", 0) + 1
                entry["last_accessed"] = datetime.now().isoformat()
                self._save_entry(entry, is_root=(r.get("root_id") == r["id"]))

        return scored[:top_n]

    # -----------------------------------------------------------------------
    # Scratch (temporary) memory
    # -----------------------------------------------------------------------

    def save_scratch(self, session_id: str, chain_context: dict = None,
                     react_state: dict = None):
        """Save temporary thinking state for session recovery."""
        scratch = {
            "session_id": session_id,
            "chain_context": chain_context or {},
            "react_state": react_state or {},
            "saved_at": datetime.now().isoformat(),
        }
        path = os.path.join(self.scratch_dir, f"session_{session_id}.json")
        Path(path).write_text(
            json.dumps(scratch, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        # Also save as chain_context.json for quick access
        if chain_context:
            ctx_path = os.path.join(self.scratch_dir, "chain_context.json")
            Path(ctx_path).write_text(
                json.dumps(chain_context, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

    def load_scratch(self, session_id: str) -> dict:
        """Load temporary thinking state for session recovery."""
        path = os.path.join(self.scratch_dir, f"session_{session_id}.json")
        if os.path.exists(path):
            return json.loads(Path(path).read_text(encoding="utf-8"))
        return {}

    def refine_scratch(self, scratch_id: str, question: str, answer: str,
                       tags: list = None, node_ids: list = None,
                       graph_dir: str = None) -> int:
        """Refine a scratch/temporary memory into a persistent memory.

        Preserves call chain context and parameter bindings.
        Verifies node_ids still exist in the graph before promoting.
        Skips promotion and logs a warning if referenced nodes are gone.
        """
        # Load scratch
        path = os.path.join(self.scratch_dir, f"session_{scratch_id}.json")
        chain_context = {}
        param_bindings = {}
        if os.path.exists(path):
            scratch = json.loads(Path(path).read_text(encoding="utf-8"))
            chain_context = scratch.get("chain_context", {})
            param_bindings = scratch.get("param_bindings", {})
            # Also check new save_session format
            if not chain_context and scratch.get("call_chains"):
                chain_context = {"chains": scratch["call_chains"]}

        # Extract node_ids from chain context if not provided
        if not node_ids and chain_context.get("chains"):
            node_ids = _extract_chain_node_ids(chain_context["chains"])

        # Verify node_ids against the graph if graph_dir provided
        if node_ids and graph_dir:
            # Check that the graph directory and master file exist
            master_path = os.path.join(graph_dir, "code2database_master.json")
            if not os.path.exists(master_path):
                print(f"Warning: graph file not found at {master_path}; "
                      f"skipping promotion of scratch {scratch_id} "
                      f"(cannot verify {len(node_ids)} node_id(s))", file=sys.stderr)
                return 0

            try:
                from _builder.graph_build import _load_full_graph
                G = _load_full_graph(graph_dir)
                current_nodes = set(G.nodes())
                valid_ids = [nid for nid in node_ids if nid in current_nodes]
                invalid_count = len(node_ids) - len(valid_ids)
                if invalid_count > 0:
                    print(f"Warning: {invalid_count} node(s) no longer in graph", file=sys.stderr)
                if not valid_ids:
                    print(f"Warning: all {len(node_ids)} referenced node_ids are absent from graph; "
                          f"skipping promotion of scratch {scratch_id}", file=sys.stderr)
                    return 0
                node_ids = valid_ids
            except Exception as e:
                print(f"Warning: failed to load graph for node verification ({e}); "
                      f"skipping promotion of scratch {scratch_id}", file=sys.stderr)
                return 0

        entry_id = self.add(
            question=question, answer=answer,
            tags=tags or [], node_ids=node_ids or [],
            chains=chain_context.get("chains", []),
        )

        # Add provenance: link to source scratch
        # Try loading as root first (if entry is its own root_id), then as leaf
        entry = self._load_entry(entry_id, is_root=True)
        if not entry:
            entry = self._load_entry(entry_id, is_root=False)
        if entry:
            entry["promoted_from"] = scratch_id
            entry["param_bindings"] = param_bindings
            is_root = entry.get("root_id") == entry_id
            self._save_entry(entry, is_root=is_root)

        # Remove scratch after refining
        if os.path.exists(path):
            os.remove(path)

        print(f"Refined scratch {scratch_id} → memory #{entry_id}")
        return entry_id

    def save_session(self, session_id: str, call_chains: list = None,
                     param_bindings: dict = None, react_state: dict = None,
                     ttl_hours: float = 24.0):
        """Save session state with structured context and auto-expiry.

        Unlike save_scratch, this stores structured call chains, parameter
        bindings, and ReAct state for precise session recovery.
        """
        scratch = {
            "session_id": session_id,
            "call_chains": call_chains or [],
            "param_bindings": param_bindings or {},
            "react_state": react_state or {},
            "react_phase": react_state.get("phase", "") if react_state else "",
            "verification_status": react_state.get("verification", "pending") if react_state else "pending",
            "saved_at": datetime.now().isoformat(),
            "expires_at": datetime.fromtimestamp(
                time.time() + ttl_hours * 3600).isoformat(),
            "ttl_hours": ttl_hours,
        }
        path = os.path.join(self.scratch_dir, f"session_{session_id}.json")
        Path(path).write_text(
            json.dumps(scratch, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    def restore_session(self, session_id: str) -> dict:
        """Restore session state. Returns empty dict if expired or not found."""
        path = os.path.join(self.scratch_dir, f"session_{session_id}.json")
        if not os.path.exists(path):
            return {}

        scratch = json.loads(Path(path).read_text(encoding="utf-8"))

        # Check expiry
        expires = scratch.get("expires_at", "")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires)
                if datetime.now() > exp_dt:
                    os.remove(path)
                    return {}
            except ValueError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        return scratch

    def list_sessions(self) -> list:
        """List all active scratch sessions."""
        sessions = []
        for fname in sorted(os.listdir(self.scratch_dir)):
            if not fname.startswith("session_") or not fname.endswith(".json"):
                continue
            path = os.path.join(self.scratch_dir, fname)
            try:
                scratch = json.loads(Path(path).read_text(encoding="utf-8"))
                expires = scratch.get("expires_at", "unknown")
                sessions.append({
                    "session_id": scratch.get("session_id", ""),
                    "saved_at": scratch.get("saved_at", ""),
                    "expires_at": expires,
                    "has_chains": bool(scratch.get("call_chains")),
                    "react_phase": scratch.get("react_phase", ""),
                })
            except (json.JSONDecodeError, OSError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
        return sessions

    def cleanup_expired(self) -> int:
        """Remove expired scratch entries. Returns count removed."""
        removed = 0
        now = datetime.now()
        for fname in list(os.listdir(self.scratch_dir)):
            if not fname.startswith("session_") or not fname.endswith(".json"):
                continue
            path = os.path.join(self.scratch_dir, fname)
            try:
                scratch = json.loads(Path(path).read_text(encoding="utf-8"))
                expires = scratch.get("expires_at", "")
                if expires:
                    exp_dt = datetime.fromisoformat(expires)
                    if now > exp_dt:
                        os.remove(path)
                        removed += 1
            except (ValueError, json.JSONDecodeError, OSError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                continue
        return removed

    # -----------------------------------------------------------------------
    # Script-based bulk operations (no LLM tokens needed)
    # -----------------------------------------------------------------------

    def consolidate(self) -> dict:
        """One-pass: decay all entries, archive low-weight, rebuild indexes.

        Returns summary dict with counts.
        """
        decayed = self.decay()
        index = self._load_index()
        self._save_layered_indexes(index)

        # Rebuild experience index
        exp_index = self._load_exp_index()
        self._save_exp_index(exp_index)

        summary = {
            "decayed": decayed,
            "trusted": sum(1 for e in index["entries"] if e.get("status") == "trusted"),
            "experience": sum(1 for e in index["entries"] if e.get("status") == "experience"),
            "roots": len(index.get("roots", [])),
            "scratch_sessions": len(self.list_sessions()),
        }
        print(f"Consolidated: {summary}")
        return summary

    def export_for_debug(self, output_path: str) -> str:
        """Export full memory state to a single JSON for inspection."""
        index = self._load_index()
        export = {
            "index": index,
            "entries": {},
            "experience": {},
            "scratch_sessions": {},
        }

        for entry_meta in index["entries"]:
            eid = entry_meta["id"]
            entry = self._load_entry(eid, is_root=(entry_meta.get("root_id") == eid))
            if not entry:
                entry = self._load_entry(eid, is_root=False)
            if entry:
                export["entries"][str(eid)] = entry

        # Experience entries
        for fname in os.listdir(self.exp_dir):
            if fname.startswith("experience_") and fname.endswith(".json"):
                epath = os.path.join(self.exp_dir, fname)
                export["experience"][fname] = json.loads(Path(epath).read_text(encoding="utf-8"))

        # Scratch sessions
        for fname in os.listdir(self.scratch_dir):
            if fname.startswith("session_") and fname.endswith(".json"):
                spath = os.path.join(self.scratch_dir, fname)
                export["scratch_sessions"][fname] = json.loads(Path(spath).read_text(encoding="utf-8"))

        Path(output_path).write_text(
            json.dumps(export, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"Exported memory state to {output_path}")
        return output_path

    def import_from_json(self, input_path: str, merge: bool = True) -> int:
        """Import memories from JSON, with optional merge detection."""
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
        imported = 0

        entries = data if isinstance(data, list) else data.get("entries", [])
        if isinstance(data, dict) and "entries" in data and isinstance(data["entries"], dict):
            entries = list(data["entries"].values())

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            question = entry.get("question", "")
            answer = entry.get("answer", "")
            if not question:
                continue

            if merge:
                # Check if similar memory already exists
                existing = self.query(question, top_n=1, min_weight=0.0)
                if existing and existing[0].get("similarity", 0) > 0.8:
                    continue  # Skip, already have similar memory

            self.add(
                question=question,
                answer=answer,
                tags=entry.get("tags", []),
                node_ids=entry.get("node_ids", []),
                chains=entry.get("chains", []),
            )
            imported += 1

        print(f"Imported {imported} memory entries from {input_path}")
        return imported

    # -----------------------------------------------------------------------
    # Memory pack (multi-tier extraction)
    # -----------------------------------------------------------------------

    def generate_pack(self, tier: str = "lite") -> dict:
        """Generate a memory pack for LLM consumption.

        tier: "lite" (~100 tokens), "standard" (~600 tokens),
              "deep" (~2000 tokens), "full" (complete dump)
        """
        index = self._load_index()

        if tier == "lite":
            return self._pack_lite(index)
        elif tier == "deep":
            return self._pack_deep(index)
        elif tier == "full":
            return self._pack_full(index)
        else:
            return self._pack_standard(index)

    def _pack_lite(self, index: dict) -> dict:
        """~200 tokens: top questions + hot memory list."""
        top_q = []
        hot = []
        for entry_meta in index["entries"][:20]:
            if entry_meta.get("status") == "experience":
                continue
            eid = entry_meta["id"]
            entry = self._load_entry(eid, is_root=(entry_meta.get("root_id") == eid))
            if not entry:
                entry = self._load_entry(eid, is_root=False)
            if not entry:
                continue
            w = entry.get("weight", 1.0)
            q = entry_meta.get("question", "")
            if w > 0.7:
                hot.append({"id": eid, "q": q[:60], "w": round(w, 2)})
            if w > 0.5 or len(top_q) < 5:
                top_q.append(q[:80])

        pack = {
            "top_questions": top_q[:5],
            "hot_memories": sorted(hot, key=lambda x: -x["w"])[:10],
        }

        # Write to graph dir
        pack_path = os.path.join(self.graph_dir, ".memory_pack_lite.json")
        Path(pack_path).write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return pack

    def _pack_standard(self, index: dict) -> dict:
        """~600 tokens: all hot memories + warm summaries."""
        hot = []
        warm = []
        for entry_meta in index["entries"][:30]:
            if entry_meta.get("status") == "experience":
                continue
            eid = entry_meta["id"]
            entry = self._load_entry(eid, is_root=(entry_meta.get("root_id") == eid))
            if not entry:
                entry = self._load_entry(eid, is_root=False)
            if not entry:
                continue
            w = entry.get("weight", 1.0)
            q = entry_meta.get("question", "")
            a = entry.get("answer", "")[:200]

            if w > 0.7:
                hot.append({"id": eid, "q": q, "a": a, "w": round(w, 2),
                            "tags": entry.get("tags", [])[:3]})
            elif w > 0.3:
                warm.append({"id": eid, "q": q[:80], "a": a[:100], "w": round(w, 2)})

        pack = {
            "top_questions": [e["q"] for e in sorted(hot, key=lambda x: -x["w"])[:5]],
            "all_hot": sorted(hot, key=lambda x: -x["w"]),
            "warm_summaries": sorted(warm, key=lambda x: -x["w"])[:15],
        }

        pack_path = os.path.join(self.graph_dir, ".memory_pack_standard.json")
        Path(pack_path).write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return pack

    def _pack_deep(self, index: dict) -> dict:
        """~2000 tokens: full Q+A + chains + node_ids for all trusted memories."""
        entries = []
        for entry_meta in index["entries"]:
            if entry_meta.get("status") == "experience":
                continue
            eid = entry_meta["id"]
            entry = self._load_entry(eid, is_root=(entry_meta.get("root_id") == eid))
            if not entry:
                entry = self._load_entry(eid, is_root=False)
            if not entry:
                continue

            entries.append({
                "id": eid,
                "question": entry.get("question", ""),
                "answer": entry.get("answer", ""),
                "chains": entry.get("chains", [])[:10],
                "node_ids": entry.get("node_ids", [])[:20],
                "tags": entry.get("tags", []),
                "weight": round(entry.get("weight", 1.0), 4),
                "merged_count": entry.get("merged_count", 0),
            })

        entries.sort(key=lambda x: -x["weight"])
        pack = {"total": len(entries), "entries": entries}

        pack_path = os.path.join(self.graph_dir, ".memory_pack_deep.json")
        Path(pack_path).write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return pack

    def _pack_full(self, index: dict) -> dict:
        """Complete memory state: everything including versions and merge history."""
        entries = []
        experience = []

        for entry_meta in index["entries"]:
            eid = entry_meta["id"]
            is_root = (entry_meta.get("root_id") == eid)
            entry = self._load_entry(eid, is_root=is_root)
            if not entry:
                entry = self._load_entry(eid, is_root=False)
            if not entry:
                continue

            if entry_meta.get("status") == "experience":
                experience.append(entry)
            else:
                entries.append(entry)

        # Load experience directory entries not in index
        extra_experience = []
        try:
            for fname in os.listdir(self.exp_dir):
                if fname.startswith("experience_") and fname.endswith(".json"):
                    epath = os.path.join(self.exp_dir, fname)
                    exp_entry = json.loads(Path(epath).read_text(encoding="utf-8"))
                    extra_experience.append(exp_entry)
        except OSError:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        pack = {
            "index_meta": {
                "total_entries": len(index["entries"]),
                "roots": len(index.get("roots", [])),
                "next_id": index.get("next_id", 1),
            },
            "entries": entries,
            "experience": experience + extra_experience,
            "versions_included": True,
        }

        pack_path = os.path.join(self.graph_dir, ".memory_pack_full.json")
        Path(pack_path).write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return pack

    # -----------------------------------------------------------------------
    # Migration from old format
    # -----------------------------------------------------------------------

    def migrate_from_legacy(self):
        """Migrate old flat memory files to root/leaf structure."""
        index = self._load_index()
        migrated = 0

        for entry_meta in index["entries"]:
            eid = entry_meta["id"]
            # Check if already in new location
            leaf_path = os.path.join(self.leaf_dir, f"mem_{eid}.json")
            root_path = os.path.join(self.root_dir, f"root_{eid}.json")

            if os.path.exists(leaf_path) or os.path.exists(root_path):
                continue

            # Load from old location
            old_path = os.path.join(self.mem_dir, f"memory_{eid}.json")
            if not os.path.exists(old_path):
                continue

            entry = json.loads(Path(old_path).read_text(encoding="utf-8"))
            self._update_weight(entry)

            # Determine if this is a root
            root_id = entry_meta.get("root_id", 0)
            is_root = (root_id == eid or root_id == 0)

            if is_root:
                entry["root_id"] = eid
                self._save_entry(entry, is_root=True)
                if not any(r["id"] == eid for r in index.get("roots", [])):
                    index.setdefault("roots", []).append({"id": eid, "question": entry_meta.get("question", "")})
                entry_meta["root_id"] = eid
            else:
                entry["root_id"] = root_id
                self._save_entry(entry, is_root=False)

            migrated += 1

        self._save_index(index)
        self._save_layered_indexes(index)
        print(f"Migrated {migrated} entries to root/leaf structure")


# ---------------------------------------------------------------------------
# CLI command handler: memory-health
# ---------------------------------------------------------------------------

def cmd_memory_health(args):
    """Report memory system health statistics."""
    graph_dir = args.graph
    mgr = MemoryManager(graph_dir)

    index = mgr._load_index()
    layered = mgr._build_layered_index(index)

    total_entries = len(index["entries"])
    expired_entries = sum(1 for e in index["entries"] if e.get("status") == "experience")

    l0_count = len(layered.get("L0", []))
    l1_count = len(layered.get("L1", []))
    l2_count = len(layered.get("L2", []))

    # Oldest entry timestamp
    oldest_ts = None
    for entry_meta in index["entries"]:
        ts = entry_meta.get("created", "") or entry_meta.get("validated_at", "")
        if ts:
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts

    # Scratch session stats
    sessions = mgr.list_sessions()
    scratch_count = len(sessions)
    now = datetime.now()
    expired_scratch = 0
    for s in sessions:
        exp = s.get("expires_at", "")
        if exp:
            try:
                if now > datetime.fromisoformat(exp):
                    expired_scratch += 1
            except ValueError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
    stats = {
        "total_entries": total_entries,
        "expired_entries": expired_entries,
        "active_entries": total_entries - expired_entries,
        "layer_counts": {
            "L0": l0_count,
            "L1": l1_count,
            "L2": l2_count,
        },
        "oldest_entry_timestamp": oldest_ts,
        "scratch_sessions": scratch_count,
        "expired_scratch_sessions": expired_scratch,
        "roots": len(index.get("roots", [])),
    }

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


# ---------------------------------------------------------------------------
# CLI command handler
# ---------------------------------------------------------------------------

def cmd_manage_memory(args):
    """Handle manage-memory command with various actions."""
    graph_dir = args.graph
    mgr = MemoryManager(graph_dir)

    # Auto-migrate on first use
    index = mgr._load_index()
    if not index.get("roots") and index.get("entries"):
        mgr.migrate_from_legacy()

    action = args.action

    if action == "add":
        mgr.add(
            question=args.question,
            answer=getattr(args, "answer", ""),
            tags=args.tags.split(",") if getattr(args, "tags", "") else [],
            node_ids=args.node_ids.split(",") if getattr(args, "node_ids", "") else [],
        )
    elif action == "correct":
        mgr.correct(
            mem_id=int(args.id),
            field=args.field,
            value=args.value,
        )
    elif action == "reshape":
        mgr.reshape(
            root_id=int(args.root_id),
            answer=args.answer,
        )
    elif action == "decay":
        mgr.decay()
    elif action == "promote":
        mgr.promote(
            mem_id=int(args.id),
            boost=float(getattr(args, "boost", "1.0")),
        )
    elif action == "refine":
        mgr.refine_scratch(
            scratch_id=args.scratch_id,
            question=args.question,
            answer=args.answer,
            tags=args.tags.split(",") if getattr(args, "tags", "") else [],
        )
    elif action == "query":
        results = mgr.query(
            query_text=args.query,
            top_n=int(getattr(args, "top", "5")),
            min_weight=float(getattr(args, "min_weight", "0.3")),
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif action == "pack":
        tier = getattr(args, "tier", "lite")
        pack = mgr.generate_pack(tier=tier)
        print(json.dumps(pack, ensure_ascii=False, indent=2))
    elif action == "consolidate":
        mgr.consolidate()
    elif action == "export":
        output = getattr(args, "output", os.path.join(graph_dir, "memory_export.json"))
        mgr.export_for_debug(output)
    elif action == "import":
        mgr.import_from_json(
            input_path=args.input,
            merge=getattr(args, "merge", True),
        )
    elif action == "scratch-save":
        mgr.save_session(
            session_id=args.session_id,
            call_chains=json.loads(args.chains) if getattr(args, "chains", "") else None,
            param_bindings=json.loads(args.params) if getattr(args, "params", "") else None,
            react_state=json.loads(args.react) if getattr(args, "react", "") else None,
            ttl_hours=float(getattr(args, "ttl", "24")),
        )
    elif action == "scratch-restore":
        result = mgr.restore_session(args.session_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif action == "scratch-list":
        sessions = mgr.list_sessions()
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
    elif action == "scratch-cleanup":
        removed = mgr.cleanup_expired()
        print(f"Removed {removed} expired scratch session(s)")
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        print("Available: add, correct, reshape, decay, promote, refine, query, pack, "
              "consolidate, export, import, scratch-save, scratch-restore, scratch-list, scratch-cleanup")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Memory manager")
    parser.add_argument("--graph", required=True, help="Graph directory")
    sub = parser.add_subparsers(dest="action")

    p_add = sub.add_parser("add")
    p_add.add_argument("--question", required=True)
    p_add.add_argument("--answer", default="")
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--node-ids", default="")

    p_correct = sub.add_parser("correct")
    p_correct.add_argument("--id", required=True)
    p_correct.add_argument("--field", required=True)
    p_correct.add_argument("--value", required=True)

    p_reshape = sub.add_parser("reshape")
    p_reshape.add_argument("--root-id", required=True)
    p_reshape.add_argument("--answer", required=True)

    sub.add_parser("decay")

    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--id", required=True)
    p_promote.add_argument("--boost", default="1.0")

    p_refine = sub.add_parser("refine")
    p_refine.add_argument("--scratch-id", required=True)
    p_refine.add_argument("--question", required=True)
    p_refine.add_argument("--answer", required=True)
    p_refine.add_argument("--tags", default="")

    p_query = sub.add_parser("query")
    p_query.add_argument("--query", required=True)
    p_query.add_argument("--top", default="5")
    p_query.add_argument("--min-weight", default="0.3")

    p_pack = sub.add_parser("pack")
    p_pack.add_argument("--tier", default="lite", choices=["lite", "standard", "deep", "full"])

    sub.add_parser("consolidate")

    p_export = sub.add_parser("export")
    p_export.add_argument("--output", default="")

    p_import = sub.add_parser("import")
    p_import.add_argument("--input", required=True)
    p_import.add_argument("--merge", default=True, action="store_true",
                          help="Merge on import (default: True)")
    p_import.add_argument("--no-merge", dest="merge", action="store_false",
                          help="Do not merge on import")

    p_ssave = sub.add_parser("scratch-save")
    p_ssave.add_argument("--session-id", required=True)
    p_ssave.add_argument("--chains", default="")
    p_ssave.add_argument("--params", default="")
    p_ssave.add_argument("--react", default="")
    p_ssave.add_argument("--ttl", default="24")

    p_srestore = sub.add_parser("scratch-restore")
    p_srestore.add_argument("--session-id", required=True)

    sub.add_parser("scratch-list")

    sub.add_parser("scratch-cleanup")

    args = parser.parse_args()
    if not args.action:
        parser.print_help()
        sys.exit(1)
    cmd_manage_memory(args)
