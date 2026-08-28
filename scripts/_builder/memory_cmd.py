"""callgraph builder module: memory_cmd."""

import os
import json
import sys
from datetime import datetime
from pathlib import Path
import networkx as nx
from _builder.utils import _experience_dir, _extract_chain_node_ids, _memory_dir, _similarity_score, _simple_tokenize
from _builder.graph_build import _load_full_graph


def _sanitize_memory_index(index, path_for_warn: str = ""):
    """Sanitize a memory index dict loaded from JSON.

    Defensive against corrupt/legacy memory.json files where `entries` may
    contain plain strings instead of {"id": ..., "question": ...} dicts.
    Without this, downstream loops call entry_meta.get("status") which
    crashes with `AttributeError: 'str' object has no attribute 'get'`
    (seen in inline memory-merge scripts run on another environment).

    Filters out non-dict entries, ensures required keys exist, and logs
    a warning when items are dropped.
    """
    if not isinstance(index, dict):
        if path_for_warn:
            print(f"[memory] Warning: index at {path_for_warn} is not a dict, "
                  f"returning empty default", file=sys.stderr)
        return {"entries": [], "next_id": 1, "roots": []}
    if not isinstance(index.get("entries"), list):
        index["entries"] = []
    else:
        _orig_len = len(index["entries"])
        index["entries"] = [e for e in index["entries"] if isinstance(e, dict)]
        if len(index["entries"]) != _orig_len and path_for_warn:
            print(f"[memory] Warning: filtered {_orig_len - len(index['entries'])} "
                  f"non-dict entries from {path_for_warn}", file=sys.stderr)
    index.setdefault("next_id", 1)
    index.setdefault("roots", [])
    return index


def _auto_validate_memory(G: nx.DiGraph, mem_dir: str, graph_dir: str):
    """Auto-validate memory entries against current graph after update."""
    current_nodes = set(G.nodes())
    exp_dir = _experience_dir(graph_dir)

    index_path = os.path.join(mem_dir, "index.json")
    index = _sanitize_memory_index(
        json.loads(Path(index_path).read_text(encoding="utf-8")),
        index_path)

    invalidated = 0
    for entry_meta in list(index["entries"]):
        eid = entry_meta["id"]
        if entry_meta.get("status") == "experience":
            continue

        # Try new root/leaf paths, then fall back to old flat path
        is_root = entry_meta.get("root_id") == eid
        if is_root:
            entry_path = os.path.join(mem_dir, "root", f"root_{eid}.json")
        else:
            entry_path = os.path.join(mem_dir, "leaf", f"mem_{eid}.json")
        if not os.path.exists(entry_path):
            entry_path = os.path.join(mem_dir, f"memory_{eid}.json")
        if not os.path.exists(entry_path):
            continue

        entry = json.loads(Path(entry_path).read_text(encoding="utf-8"))
        node_ids = entry.get("node_ids", [])
        if not node_ids:
            continue

        missing = [nid for nid in node_ids if nid not in current_nodes]
        if missing:
            entry["status"] = "experience"
            entry["invalidated_reason"] = f"{len(missing)} node(s) removed by update: {missing[:5]}"
            entry["invalidated_at"] = datetime.now().isoformat()

            # Move to experience
            exp_entry_path = os.path.join(exp_dir, f"experience_{eid}.json")
            Path(exp_entry_path).write_text(
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.remove(entry_path)
            # Also clean up the other location if it exists
            if is_root:
                leaf_path = os.path.join(mem_dir, "leaf", f"mem_{eid}.json")
                if os.path.exists(leaf_path):
                    os.remove(leaf_path)
            else:
                root_path = os.path.join(mem_dir, "root", f"root_{eid}.json")
                if os.path.exists(root_path):
                    os.remove(root_path)
            entry_meta["status"] = "experience"
            invalidated += 1

    if invalidated > 0:
        # Update experience index
        exp_index_path = os.path.join(exp_dir, "index.json")
        exp_entries = []
        if os.path.exists(exp_index_path):
            exp_idx = _sanitize_memory_index(
                json.loads(Path(exp_index_path).read_text(encoding="utf-8")),
                exp_index_path)
            exp_entries = exp_idx.get("entries", [])
        for em in index["entries"]:
            if em.get("status") == "experience" and not any(e.get("id") == em["id"] for e in exp_entries):
                exp_entries.append(em)
        Path(exp_index_path).write_text(
            json.dumps({"entries": exp_entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        Path(index_path).write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Memory validated: {invalidated} entry/entries → experience (graph changed)")




def cmd_save_memory(args):
    """Save a Q&A memory entry with associated call chains."""
    graph_dir = args.graph
    mem_dir = _memory_dir(graph_dir)

    question = args.question
    answer = args.answer
    chains = args.chains  # JSON string of chain data
    tags = args.tags.split(",") if args.tags else []
    node_ids = args.node_ids.split(",") if args.node_ids else []

    # Auto-extract node_ids from chains if not provided
    if not node_ids and chains:
        chain_data = json.loads(chains) if isinstance(chains, str) else chains
        node_ids = _extract_chain_node_ids(chain_data)

    # Load existing memories to check for merge
    index_path = os.path.join(mem_dir, "index.json")
    if os.path.exists(index_path):
        index = _sanitize_memory_index(
            json.loads(Path(index_path).read_text(encoding="utf-8")),
            index_path)
    else:
        index = {"entries": [], "next_id": 1}

    # Compute similarity with existing entries for merge detection
    q_tokens = _simple_tokenize(question)
    best_match_id = None
    best_score = 0.0
    for entry in index["entries"]:
        e_tokens = _simple_tokenize(entry.get("question", ""))
        score = _similarity_score(q_tokens, e_tokens)
        if score > best_score and score >= 0.6:
            best_score = score
            best_match_id = entry["id"]

    if best_match_id and args.no_merge is not True:
        # Merge: append new info to existing entry
        # Try root/leaf paths first, then fall back to old flat path
        best_meta = next((e for e in index["entries"] if e["id"] == best_match_id), {})
        is_root = best_meta.get("root_id") == best_match_id
        if is_root:
            entry_path = os.path.join(mem_dir, "root", f"root_{best_match_id}.json")
        else:
            entry_path = os.path.join(mem_dir, "leaf", f"mem_{best_match_id}.json")
        if not os.path.exists(entry_path):
            entry_path = os.path.join(mem_dir, f"memory_{best_match_id}.json")
        if not os.path.exists(entry_path):
            best_match_id = None  # Can't find the entry to merge
    if best_match_id and args.no_merge is not True:
        entry = json.loads(Path(entry_path).read_text(encoding="utf-8"))
        entry["answer"] = answer if answer else entry.get("answer", "")
        if chains:
            chain_data = json.loads(chains) if isinstance(chains, str) else chains
            entry.setdefault("chains", []).extend(chain_data)
        if tags:
            entry.setdefault("tags", []).extend(tags)
            entry["tags"] = list(set(entry["tags"]))
        if node_ids:
            entry.setdefault("node_ids", []).extend(node_ids)
            entry["node_ids"] = sorted(set(entry["node_ids"]))
        entry["merged_count"] = entry.get("merged_count", 0) + 1
        Path(entry_path).write_text(
            json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Merged with existing memory #{best_match_id} (similarity: {best_score:.2f})")
    else:
        # New entry — use MemoryManager for proper root/leaf layout
        from _builder.memory_manager import MemoryManager
        mgr = MemoryManager(graph_dir)
        entry_id = mgr.add(
            question=question,
            answer=answer,
            tags=tags,
            node_ids=node_ids,
            chains=json.loads(chains) if chains and isinstance(chains, str) else (chains or []),
            no_merge=(args.no_merge is True),
        )
        print(f"Saved memory #{entry_id} (trusted, {len(node_ids)} nodes tracked)")
        return

    # For merge path, update the index
    Path(index_path).write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")




def cmd_search_memory(args):
    """Search memory and experience for similar questions.

    Prefers the unified FTS5+BM25 index (kb_paragraphs) when
    code2database.db exists; falls back to the legacy in-memory
    Jaccard-token search otherwise.
    """
    graph_dir = args.graph
    query = args.query
    top = args.top

    # Try unified FTS5 path first (Phase 1+2 upgrade)
    try:
        from _builder.kb_index import query_kb
        results = query_kb(
            graph_dir=graph_dir,
            query=query,
            top_n=top,
            kinds=["memory_qa", "memory_experience"],
            min_weight=0.0,  # no weight filter — let BM25 rank
            max_tokens=4000,
        )
        if results:
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return
        # No FTS5 hits or no db — fall through to legacy
    except Exception as _e:
        # Fall back to legacy search if FTS5 path fails (no db, schema
        # mismatch, etc.). Don't print the error — it's expected when
        # the project hasn't run `kb-rebuild-index` yet.
        pass

    # Legacy path: Jaccard token-set similarity on filesystem JSON entries
    mem_dir = _memory_dir(graph_dir)
    exp_dir = _experience_dir(graph_dir)

    index_path = os.path.join(mem_dir, "index.json")
    if not os.path.exists(index_path):
        print("No memory entries found.", file=sys.stderr)
        sys.exit(0)

    index = _sanitize_memory_index(
        json.loads(Path(index_path).read_text(encoding="utf-8")),
        index_path)
    query_tokens = _simple_tokenize(query)

    scored = []
    for entry_meta in index["entries"]:
        e_tokens = _simple_tokenize(entry_meta.get("question", ""))
        tag_tokens = set()
        for tag in entry_meta.get("tags", []):
            tag_tokens |= _simple_tokenize(tag)
        all_tokens = e_tokens | tag_tokens

        score = _similarity_score(query_tokens, all_tokens)
        # Experience entries get a penalty so trusted memory ranks higher
        status = entry_meta.get("status", "trusted")
        if status == "experience":
            score *= 0.7
        if score > 0:
            scored.append({
                "id": entry_meta["id"],
                "score": score,
                "question": entry_meta["question"],
                "status": status,
            })

    # Also search experience directory entries not in main index
    if os.path.exists(exp_dir):
        exp_index_path = os.path.join(exp_dir, "index.json")
        if os.path.exists(exp_index_path):
            exp_index = _sanitize_memory_index(
                json.loads(Path(exp_index_path).read_text(encoding="utf-8")),
                exp_index_path)
            for entry_meta in exp_index.get("entries", []):
                e_tokens = _simple_tokenize(entry_meta.get("question", ""))
                tag_tokens = set()
                for tag in entry_meta.get("tags", []):
                    tag_tokens |= _simple_tokenize(tag)
                all_tokens = e_tokens | tag_tokens
                score = _similarity_score(query_tokens, all_tokens) * 0.5
                if score > 0:
                    scored.append({
                        "id": entry_meta["id"],
                        "score": score,
                        "question": entry_meta["question"],
                        "status": "experience",
                        "source": "experience",
                    })

    scored.sort(key=lambda x: -x["score"])
    results = scored[:top]

    if not results:
        print("No similar memories found.")
        return

    # Load full entries for top results
    for r in results:
        if r.get("source") == "experience":
            entry_path = os.path.join(exp_dir, f"experience_{r['id']}.json")
        else:
            # Try root/leaf paths first, then old flat path
            is_root = r.get("root_id") == r["id"]
            if is_root:
                entry_path = os.path.join(mem_dir, "root", f"root_{r['id']}.json")
            else:
                entry_path = os.path.join(mem_dir, "leaf", f"mem_{r['id']}.json")
            if not os.path.exists(entry_path):
                entry_path = os.path.join(mem_dir, f"memory_{r['id']}.json")
        if os.path.exists(entry_path):
            entry = json.loads(Path(entry_path).read_text(encoding="utf-8"))
            r["answer"] = entry.get("answer", "")
            r["chains"] = entry.get("chains", [])
            r["tags"] = entry.get("tags", [])
            r["merged_count"] = entry.get("merged_count", 0)
            r["invalidated_reason"] = entry.get("invalidated_reason", "")

    print(json.dumps(results, ensure_ascii=False, indent=2))




def cmd_validate_memory(args):
    """Validate memory entries against current graph. Invalidate stale entries → experience."""
    graph_dir = args.graph
    mem_dir = _memory_dir(graph_dir)
    exp_dir = _experience_dir(graph_dir)

    index_path = os.path.join(mem_dir, "index.json")
    if not os.path.exists(index_path):
        print("No memory entries to validate.", file=sys.stderr)
        return

    # Load current graph node set
    G = _load_full_graph(graph_dir)
    current_nodes = set(G.nodes())

    index = _sanitize_memory_index(
        json.loads(Path(index_path).read_text(encoding="utf-8")),
        index_path)

    validated = 0
    invalidated = 0
    invalidated_ids = []

    for entry_meta in list(index["entries"]):
        eid = entry_meta["id"]
        status = entry_meta.get("status", "trusted")
        if status == "experience":
            validated += 1
            continue

        # Try root/leaf paths first, then fall back to old flat path
        is_root = entry_meta.get("root_id") == eid
        if is_root:
            entry_path = os.path.join(mem_dir, "root", f"root_{eid}.json")
        else:
            entry_path = os.path.join(mem_dir, "leaf", f"mem_{eid}.json")
        if not os.path.exists(entry_path):
            entry_path = os.path.join(mem_dir, f"memory_{eid}.json")
        if not os.path.exists(entry_path):
            continue

        entry = json.loads(Path(entry_path).read_text(encoding="utf-8"))
        node_ids = entry.get("node_ids", [])

        if not node_ids:
            # No node tracking — cannot validate, keep trusted
            validated += 1
            continue

        # Check how many referenced nodes still exist
        missing = [nid for nid in node_ids if nid not in current_nodes]
        if missing:
            # Some nodes are gone → invalidate
            entry["status"] = "experience"
            entry["invalidated_reason"] = f"{len(missing)} node(s) no longer in graph: {missing[:5]}"
            entry["invalidated_at"] = datetime.now().isoformat()

            # Move to experience directory
            exp_entry_path = os.path.join(exp_dir, f"experience_{eid}.json")
            Path(exp_entry_path).write_text(
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            # Remove from memory directory (and companion root/leaf file)
            os.remove(entry_path)
            if is_root:
                leaf_path = os.path.join(mem_dir, "leaf", f"mem_{eid}.json")
                if os.path.exists(leaf_path):
                    os.remove(leaf_path)
            else:
                root_path = os.path.join(mem_dir, "root", f"root_{eid}.json")
                if os.path.exists(root_path):
                    os.remove(root_path)

            # Update index entry status
            entry_meta["status"] = "experience"
            invalidated += 1
            invalidated_ids.append({
                "id": eid,
                "question": entry.get("question", ""),
                "missing_nodes": len(missing),
            })
        else:
            # All nodes still present → still trusted
            entry["validated_at"] = datetime.now().isoformat()
            Path(entry_path).write_text(
                json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            validated += 1

    # Update experience index
    exp_index_path = os.path.join(exp_dir, "index.json")
    exp_entries = []
    if os.path.exists(exp_index_path):
        exp_index = _sanitize_memory_index(
            json.loads(Path(exp_index_path).read_text(encoding="utf-8")),
            exp_index_path)
        exp_entries = exp_index.get("entries", [])

    for inv in invalidated_ids:
        exp_entries.append({
            "id": inv["id"],
            "question": inv["question"],
            "status": "experience",
            "tags": [],
        })

    Path(exp_index_path).write_text(
        json.dumps({"entries": exp_entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    # Save updated main index
    Path(index_path).write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Memory validation: {validated} trusted, {invalidated} → experience")
    if invalidated_ids:
        print("Invalidated entries:")
        for inv in invalidated_ids[:10]:
            print(f"  - #{inv['id']}: {inv['question']} ({inv['missing_nodes']} nodes missing)")
        if len(invalidated_ids) > 10:
            print(f"  ... and {len(invalidated_ids) - 10} more")


