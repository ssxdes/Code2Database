#!/usr/bin/env python3
"""Memory manager facade for Code2Database.

Memory is the LLM's OWN persistent brain on disk — big, messy, shared
by many people, and organized by a hierarchical category tree. The
storage engine is the SQLite-backed MemoryStore (memory/memory.db);
this module keeps the historical MemoryManager API stable for existing
call sites (graph_build auto-consolidate, MCP fallback, CLI commands)
and hosts the manage-memory / memory-health CLI handlers.

Temporary memory (.scratch/) remains file-based: sessions are ephemeral
TTL-scoped state, not knowledge worth querying.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import logging

from _builder.memory_store import (
    MemoryStore,
    _memory_lock,
)
from _builder.utils import _extract_chain_node_ids


class MemoryManager:
    """Facade over MemoryStore keeping the historical API stable."""

    def __init__(self, graph_dir: str):
        self.graph_dir = graph_dir
        self.mem_dir = os.path.join(graph_dir, "memory")
        self.scratch_dir = os.path.join(graph_dir, ".scratch")
        self.store = MemoryStore(graph_dir)

    # ------------------------------------------------------------------
    # Persistent memory — delegated to MemoryStore
    # ------------------------------------------------------------------

    def add(self, question: str, answer: str = "", tags: list = None,
            node_ids: list = None, chains: list = None,
            category: str = None, author: str = "",
            no_merge: bool = False) -> int:
        return self.store.add(
            question=question, answer=answer, tags=tags,
            node_ids=node_ids, chains=chains, category=category,
            author=author, no_merge=no_merge)

    def query(self, query_text: str, top_n: int = 5,
              min_weight: float = 0.3) -> list:
        return self.store.query(query_text, top_n=top_n,
                                min_weight=min_weight)

    def search(self, query: str, top_n: int = 10, category: str = None,
               tags: List[str] = None, author: str = None,
               include_experience: bool = False,
               min_weight: float = 0.0) -> list:
        return self.store.search(
            query, top_n=top_n, category=category, tags=tags,
            author=author, include_experience=include_experience,
            min_weight=min_weight)

    def get(self, mem_id: int) -> Optional[dict]:
        return self.store.get(mem_id)

    def correct(self, mem_id: int, field: str, value: str):
        return self.store.correct(mem_id, field, value)

    def reshape(self, root_id: int, answer: str):
        return self.store.reshape(root_id, answer)

    def decay(self) -> int:
        return self.store.decay()

    def promote(self, mem_id: int, boost: float = 1.0):
        return self.store.promote(mem_id, boost)

    def split(self, mem_id: int, parts: List[dict]) -> List[int]:
        return self.store.split(mem_id, parts)

    def merge(self, mem_ids: List[int], canonical_id: int = None,
              question: str = None, answer: str = None) -> int:
        return self.store.merge(mem_ids, canonical_id=canonical_id,
                                question=question, answer=answer)

    def move(self, mem_id: int, new_category: str):
        return self.store.move(mem_id, new_category)

    def lineage(self) -> dict:
        return self.store.lineage()

    def authors(self) -> list:
        return self.store.authors()

    def categories(self) -> list:
        return self.store.categories()

    def validate_against_graph(self, current_nodes: set) -> dict:
        return self.store.validate_against_graph(current_nodes)

    def consolidate(self) -> dict:
        return self.store.consolidate()

    def stats(self) -> dict:
        return self.store.stats()

    def generate_pack(self, tier: str = "lite") -> dict:
        return self.store.generate_pack(tier)

    def export_for_debug(self, output_path: str) -> str:
        return self.store.export_for_debug(output_path)

    def import_from_json(self, input_path: str, merge: bool = True) -> int:
        return self.store.import_from_json(input_path, merge=merge)

    # ------------------------------------------------------------------
    # Scratch (temporary) memory — file-based, TTL-scoped
    # ------------------------------------------------------------------

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
        if chain_context:
            ctx_path = os.path.join(self.scratch_dir, "chain_context.json")
            Path(ctx_path).write_text(
                json.dumps(chain_context, ensure_ascii=False, indent=2)
                + "\n", encoding="utf-8")

    def load_scratch(self, session_id: str) -> dict:
        path = os.path.join(self.scratch_dir, f"session_{session_id}.json")
        if os.path.exists(path):
            return json.loads(Path(path).read_text(encoding="utf-8"))
        return {}

    def refine_scratch(self, scratch_id: str, question: str, answer: str,
                       tags: list = None, node_ids: list = None,
                       graph_dir: str = None) -> int:
        """Refine a scratch/temporary memory into a persistent memory.

        Preserves call chain context and parameter bindings. Verifies
        node_ids still exist in the graph before promoting; skips
        promotion (returns 0) when referenced nodes are gone.
        """
        path = os.path.join(self.scratch_dir, f"session_{scratch_id}.json")
        chain_context = {}
        param_bindings = {}
        if os.path.exists(path):
            scratch = json.loads(Path(path).read_text(encoding="utf-8"))
            chain_context = scratch.get("chain_context", {})
            param_bindings = scratch.get("param_bindings", {})
            if not chain_context and scratch.get("call_chains"):
                chain_context = {"chains": scratch["call_chains"]}

        if not node_ids and chain_context.get("chains"):
            node_ids = _extract_chain_node_ids(chain_context["chains"])

        if node_ids and graph_dir:
            master_path = os.path.join(graph_dir,
                                       "code2database_master.json")
            if not os.path.exists(master_path):
                print(f"Warning: graph file not found at {master_path}; "
                      f"skipping promotion of scratch {scratch_id} "
                      f"(cannot verify {len(node_ids)} node_id(s))",
                      file=sys.stderr)
                return 0
            try:
                from _builder.graph_build import _load_full_graph
                G = _load_full_graph(graph_dir)
                current_nodes = set(G.nodes())
                valid_ids = [nid for nid in node_ids
                             if nid in current_nodes]
                invalid_count = len(node_ids) - len(valid_ids)
                if invalid_count > 0:
                    print(f"Warning: {invalid_count} node(s) no longer in "
                          f"graph", file=sys.stderr)
                if not valid_ids:
                    print(f"Warning: all {len(node_ids)} referenced "
                          f"node_ids are absent from graph; skipping "
                          f"promotion of scratch {scratch_id}",
                          file=sys.stderr)
                    return 0
                node_ids = valid_ids
            except Exception as e:
                print(f"Warning: failed to load graph for node verification "
                      f"({e}); skipping promotion of scratch {scratch_id}",
                      file=sys.stderr)
                return 0

        entry_id = self.add(
            question=question, answer=answer,
            tags=tags or [], node_ids=node_ids or [],
            chains=chain_context.get("chains", []),
        )
        self.store.set_provenance(entry_id, scratch_id, param_bindings)

        if os.path.exists(path):
            os.remove(path)
        print(f"Refined scratch {scratch_id} → memory #{entry_id}")
        return entry_id

    def save_session(self, session_id: str, call_chains: list = None,
                     param_bindings: dict = None, react_state: dict = None,
                     ttl_hours: float = 24.0):
        """Save session state with structured context and auto-expiry."""
        import time
        scratch = {
            "session_id": session_id,
            "call_chains": call_chains or [],
            "param_bindings": param_bindings or {},
            "react_state": react_state or {},
            "react_phase": react_state.get("phase", "") if react_state else "",
            "verification_status":
                react_state.get("verification", "pending") if react_state
                else "pending",
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
        """Restore session state. Empty dict if expired or not found."""
        import time
        path = os.path.join(self.scratch_dir, f"session_{session_id}.json")
        if not os.path.exists(path):
            return {}
        scratch = json.loads(Path(path).read_text(encoding="utf-8"))
        expires = scratch.get("expires_at", "")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires)
                if datetime.now() > exp_dt:
                    os.remove(path)
                    return {}
            except ValueError:
                logging.getLogger(__name__).debug(
                    "silent exception", exc_info=True)
        return scratch

    def list_sessions(self) -> list:
        sessions = []
        for fname in sorted(os.listdir(self.scratch_dir)):
            if not fname.startswith("session_") or not fname.endswith(".json"):
                continue
            path = os.path.join(self.scratch_dir, fname)
            try:
                scratch = json.loads(Path(path).read_text(encoding="utf-8"))
                sessions.append({
                    "session_id": scratch.get("session_id", ""),
                    "saved_at": scratch.get("saved_at", ""),
                    "expires_at": scratch.get("expires_at", "unknown"),
                    "has_chains": bool(scratch.get("call_chains")),
                    "react_phase": scratch.get("react_phase", ""),
                })
            except (json.JSONDecodeError, OSError):
                logging.getLogger(__name__).debug(
                    "silent exception", exc_info=True)
                continue
        return sessions

    def cleanup_expired(self) -> int:
        """Remove expired scratch sessions. Returns count removed."""
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
                logging.getLogger(__name__).debug(
                    "silent exception", exc_info=True)
                continue
        return removed


# ---------------------------------------------------------------------------
# CLI command handler: memory-health
# ---------------------------------------------------------------------------

def cmd_memory_health(args):
    """Report memory system health statistics."""
    graph_dir = args.graph
    mgr = MemoryManager(graph_dir)

    stats = mgr.stats()

    # Scratch session stats
    sessions = mgr.list_sessions()
    now = datetime.now()
    expired_scratch = 0
    for s in sessions:
        exp = s.get("expires_at", "")
        if exp:
            try:
                if now > datetime.fromisoformat(exp):
                    expired_scratch += 1
            except ValueError:
                logging.getLogger(__name__).debug(
                    "silent exception", exc_info=True)
    stats["scratch_sessions"] = len(sessions)
    stats["expired_scratch_sessions"] = expired_scratch

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


# ---------------------------------------------------------------------------
# CLI command handler: manage-memory
# ---------------------------------------------------------------------------

def cmd_manage_memory(args):
    """Handle manage-memory command with various actions."""
    graph_dir = args.graph
    mgr = MemoryManager(graph_dir)

    action = args.action

    if action == "add":
        mgr.add(
            question=args.question,
            answer=getattr(args, "answer", ""),
            tags=args.tags.split(",") if getattr(args, "tags", "") else [],
            node_ids=args.node_ids.split(",")
                if getattr(args, "node_ids", "") else [],
            category=getattr(args, "category", "") or None,
            author=getattr(args, "author", ""),
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
    elif action == "search":
        results = mgr.search(
            query=args.query,
            top_n=int(getattr(args, "top", "10")),
            category=getattr(args, "category", "") or None,
            tags=args.tags.split(",") if getattr(args, "tags", "") else None,
            author=getattr(args, "author", "") or None,
            include_experience=bool(getattr(args, "include_experience",
                                             False)),
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif action == "get":
        entry = mgr.get(int(args.id))
        if entry is None:
            print(f"Memory #{args.id} not found", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(entry, ensure_ascii=False, indent=2))
    elif action == "categories":
        tree = mgr.categories()

        def _render(nodes, indent=0):
            for n in nodes:
                print(f"{'  ' * indent}{n['name']}  ({n['count']} direct, "
                      f"{n['subtree_count']} subtree)  [{n['path']}]")
                _render(n["children"], indent + 1)

        if not tree:
            print("No categories yet.")
        else:
            _render(tree)
    elif action == "split":
        parts = json.loads(args.parts) if isinstance(args.parts, str) \
            else args.parts
        if not isinstance(parts, list) or not parts:
            print("Error: --parts must be a non-empty JSON array of "
                  "{question, answer, category?, tags?} objects",
                  file=sys.stderr)
            sys.exit(1)
        try:
            mgr.split(int(args.id), parts)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    elif action == "merge":
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        if len(ids) < 2:
            print("Error: --ids needs at least two comma-separated ids",
                  file=sys.stderr)
            sys.exit(1)
        try:
            mgr.merge(
                ids,
                canonical_id=int(args.canonical) if getattr(args, "canonical", "") else None,
                question=getattr(args, "question", "") or None,
                answer=getattr(args, "answer", "") or None,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    elif action == "move":
        mgr.move(int(args.id), args.category)
    elif action == "lineage":
        data = mgr.lineage()
        by_id = {n["id"]: n for n in data["nodes"]}
        children = {}
        for e in data["edges"]:
            children.setdefault(e["from"], []).append(e)
        labels = {"split": "split", "merged_into": "merged into",
                  "variant": "variant"}
        if not data["nodes"]:
            print("No memories yet.")
        else:
            has_parent = {e["to"] for e in data["edges"]}
            seen = set()

            def _walk(node, indent):
                if node["id"] in seen:
                    return
                seen.add(node["id"])
                meta = [node["status"]]
                if node["category"]:
                    meta.append(node["category"])
                if node["author"]:
                    meta.append(node["author"])
                print(f"{'  ' * indent}#{node['id']} "
                      f"{node['question']}  ({', '.join(meta)})")
                for e in children.get(node["id"], []):
                    child = by_id.get(e["to"])
                    if child is None:
                        continue
                    print(f"{'  ' * (indent + 1)}└─{labels[e['type']]}→")
                    _walk(child, indent + 2)

            for n in data["nodes"]:
                if n["id"] not in has_parent:
                    _walk(n, 0)
    elif action == "authors":
        for a in mgr.authors():
            print(f"{a['author']}: {a['entries']} entries "
                  f"({a['active']} active)")
    elif action == "pack":
        tier = getattr(args, "tier", "lite")
        pack = mgr.generate_pack(tier=tier)
        print(json.dumps(pack, ensure_ascii=False, indent=2))
    elif action == "consolidate":
        mgr.consolidate()
    elif action == "export":
        output = getattr(args, "output", os.path.join(graph_dir,
                                                      "memory_export.json"))
        mgr.export_for_debug(output)
    elif action == "import":
        mgr.import_from_json(
            input_path=args.input,
            merge=getattr(args, "merge", True),
        )
    elif action == "scratch-save":
        mgr.save_session(
            session_id=args.session_id,
            call_chains=json.loads(args.chains)
                if getattr(args, "chains", "") else None,
            param_bindings=json.loads(args.params)
                if getattr(args, "params", "") else None,
            react_state=json.loads(args.react)
                if getattr(args, "react", "") else None,
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
        print("Available: add, correct, reshape, decay, promote, refine, "
              "query, search, get, categories, split, merge, move, pack, "
              "consolidate, export, import, scratch-save, scratch-restore, "
              "scratch-list, scratch-cleanup")


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
    p_add.add_argument("--category", default="")
    p_add.add_argument("--author", default="")

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

    p_search = sub.add_parser("search")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--top", default="10")
    p_search.add_argument("--category", default="")
    p_search.add_argument("--tags", default="")
    p_search.add_argument("--author", default="")
    p_search.add_argument("--include-experience", action="store_true")

    p_get = sub.add_parser("get")
    p_get.add_argument("--id", required=True)

    sub.add_parser("categories")

    p_split = sub.add_parser("split")
    p_split.add_argument("--id", required=True)
    p_split.add_argument("--parts", required=True,
                         help="JSON array [{question, answer, ...}]")

    p_merge = sub.add_parser("merge")
    p_merge.add_argument("--ids", required=True, help="Comma-separated ids")
    p_merge.add_argument("--canonical", default="")
    p_merge.add_argument("--question", default="")
    p_merge.add_argument("--answer", default="")

    p_move = sub.add_parser("move")
    p_move.add_argument("--id", required=True)
    p_move.add_argument("--category", required=True)

    p_pack = sub.add_parser("pack")
    p_pack.add_argument("--tier", default="lite",
                        choices=["lite", "standard", "deep", "full"])

    sub.add_parser("consolidate")

    p_export = sub.add_parser("export")
    p_export.add_argument("--output", default="")

    p_import = sub.add_parser("import")
    p_import.add_argument("--input", required=True)
    p_import.add_argument("--merge", default=True, action="store_true")
    p_import.add_argument("--no-merge", dest="merge", action="store_false")

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
