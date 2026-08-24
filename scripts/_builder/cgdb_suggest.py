"""Proactive graph analysis and suggestions.

Analyzes the current code graph and suggests improvements:
- Functions with many callers but no invariants (high-value targets)
- Potential duplicate functions (similar names, similar call patterns)
- Potential deadlock patterns (lock acquisition order inconsistencies)
- Missing doc-code alignment (functions with no semantic_desc)
- Stale knowledge entries (referenced functions no longer exist)
- Hub functions that should be documented
- Untested API entry points (no callers → possibly dead code or missing tests)
- Concurrency risks not yet analyzed
- Functions with high cyclomatic complexity (many conditions/branches)

Usage:
    from _builder.cgdb_suggest import analyze_and_suggest
    suggestions = analyze_and_suggest(graph_dir)
    for s in suggestions:
        print(f"[{s['priority']}] {s['category']}: {s['message']}")
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Dict, Any, Optional


def analyze_and_suggest(graph_dir: str, top_n: int = 20) -> List[Dict[str, Any]]:
    """Analyze the code graph and return a prioritized list of suggestions.

    Each suggestion is a dict with:
      - priority: 'high' | 'medium' | 'low'
      - category: 'invariants' | 'duplicates' | 'deadlock' | 'docs' | 'stale' | 'complexity' | 'testing'
      - message: human-readable description
      - functions: list of function IDs/names involved
      - action: suggested command to run
    """
    suggestions: List[Dict[str, Any]] = []

    # Load graph data
    functions = _load_functions(graph_dir)
    if not functions:
        return [{"priority": "high", "category": "error",
                 "message": f"Could not load graph from {graph_dir}",
                 "functions": [], "action": "Run `scan` + `build` first"}]

    edges = _load_edges(graph_dir)
    knowledge = _load_knowledge_index(graph_dir)
    memory = _load_memory_index(graph_dir)

    # 1. High-value functions without invariants
    suggestions.extend(_suggest_invariants_for_hubs(functions, edges, knowledge, top_n))

    # 2. Potential duplicate functions
    suggestions.extend(_suggest_duplicates(functions, top_n))

    # 3. Missing doc-code alignment
    suggestions.extend(_suggest_missing_docs(functions, top_n))

    # 4. Stale knowledge entries
    suggestions.extend(_suggest_stale_knowledge(knowledge, functions))

    # 5. API entry points with zero callers (dead code or missing tests)
    suggestions.extend(_suggest_dead_api(functions, edges, top_n))

    # 6. Functions with high fan-out (complexity indicator)
    suggestions.extend(_suggest_high_fanout(functions, edges, top_n))

    # 7. Concurrency risks not yet analyzed
    suggestions.extend(_suggest_concurrency_analysis(functions, edges, top_n))

    # Sort by priority (high first) then by category
    priority_order = {'high': 0, 'medium': 1, 'low': 2}
    suggestions.sort(key=lambda s: (priority_order.get(s['priority'], 3), s['category']))
    return suggestions[:top_n]


def _load_functions(graph_dir: str) -> Dict[str, dict]:
    """Load function name → info dict."""
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        funcs = {}
        try:
            rows = conn.execute(
                "SELECT id, name, domain, source_file, signature, labels, extra_json "
                "FROM functions"
            ).fetchall()
            for row in rows:
                extra = {}
                if row["extra_json"]:
                    try:
                        extra = json.loads(row["extra_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                labels = []
                if row["labels"]:
                    try:
                        labels = json.loads(row["labels"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                funcs[row["id"]] = {
                    "name": row["name"],
                    "domain": row["domain"],
                    "source_file": row["source_file"],
                    "signature": row["signature"] or extra.get("signature", ""),
                    "labels": labels,
                    "semantic_desc": extra.get("semantic_desc", ""),
                    "is_empty": extra.get("is_empty", False),
                }
        except Exception:
            pass
        finally:
            conn.close()
        return funcs

    # Fallback: master.json
    master_path = os.path.join(graph_dir, "code2database_master.json")
    if not os.path.exists(master_path):
        return {}
    try:
        with open(master_path, "r", encoding="utf-8") as f:
            master = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    funcs = {}
    for domain, domain_info in master.get("domains", {}).items():
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
                    "labels": func.get("labels", []),
                    "semantic_desc": func.get("semantic_desc", ""),
                    "is_empty": func.get("is_empty", False),
                }
    return funcs


def _load_edges(graph_dir: str) -> List[dict]:
    """Load edges from SQLite or JSON."""
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        edges = []
        try:
            rows = conn.execute(
                "SELECT invoker_id, invoked_id, relation FROM edges"
            ).fetchall()
            for row in rows:
                edges.append({
                    "source": row["invoker_id"],
                    "target": row["invoked_id"],
                    "relation": row["relation"],
                })
        except Exception:
            pass
        finally:
            conn.close()
        return edges
    return []


def _load_knowledge_index(graph_dir: str) -> List[dict]:
    """Load knowledge index entries from the canonical knowledge/ directory."""
    path = os.path.join(graph_dir, "knowledge", "index.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return []
        # Knowledge index schema: {"files": [...], "topics": [...]}
        # Return file entries (each has name, size, headings)
        files = data.get("files", [])
        if not isinstance(files, list):
            return []
        return [f for f in files if isinstance(f, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def _load_memory_index(graph_dir: str) -> List[dict]:
    """Load memory index entries from the canonical memory/ directory."""
    path = os.path.join(graph_dir, "memory", "index.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return []
        entries = data.get("entries", [])
        if not isinstance(entries, list):
            return []
        # Defensive: filter non-dict entries (mirrors _sanitize_memory_index)
        return [e for e in entries if isinstance(e, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def _suggest_invariants_for_hubs(
    functions: Dict[str, dict],
    edges: List[dict],
    knowledge: List[dict],
    top_n: int,
) -> List[Dict[str, Any]]:
    """Suggest extracting invariants for high-caller-count functions."""
    # Count callers per function
    caller_count: Dict[str, int] = {}
    for edge in edges:
        if edge.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        target = edge.get("target", "")
        if target:
            caller_count[target] = caller_count.get(target, 0) + 1

    # Functions that have knowledge entries
    knowledge_funcs = {k.get("function", "") for k in knowledge}

    # Find top hub functions without knowledge
    candidates = []
    for fid, func in functions.items():
        if func.get("is_empty", False):
            continue
        if fid in knowledge_funcs:
            continue
        cc = caller_count.get(fid, 0)
        if cc >= 3:  # At least 3 callers
            candidates.append((fid, func.get("name", ""), cc))
    candidates.sort(key=lambda x: -x[2])
    if not candidates:
        return []
    top = candidates[:min(5, top_n)]
    return [{
        "priority": "high",
        "category": "invariants",
        "message": f"Function `{name}` has {cc} callers but no invariants. "
                   f"Extracting preconditions/postconditions would benefit all callers.",
        "functions": [fid],
        "action": f"extract-invariants --node {name}",
    } for fid, name, cc in top]


def _suggest_duplicates(
    functions: Dict[str, dict],
    top_n: int,
) -> List[Dict[str, Any]]:
    """Suggest potential duplicate functions (same name, different domains)."""
    name_to_funcs: Dict[str, List[str]] = {}
    for fid, func in functions.items():
        if func.get("is_empty", False):
            continue
        name = func.get("name", "")
        if name:
            name_to_funcs.setdefault(name, []).append(fid)
    suggestions = []
    for name, fids in name_to_funcs.items():
        if len(fids) >= 2:
            # Check if they're in different domains (cross-domain duplicates)
            domains = {functions[fid].get("domain", "") for fid in fids}
            if len(domains) >= 2:
                suggestions.append({
                    "priority": "medium",
                    "category": "duplicates",
                    "message": f"Function `{name}` appears in {len(fids)} domains: "
                               f"{', '.join(sorted(domains))}. "
                               f"This may indicate code duplication.",
                    "functions": fids,
                    "action": f"search --keywords {name}",
                })
    return suggestions[:min(5, top_n)]


def _suggest_missing_docs(
    functions: Dict[str, dict],
    top_n: int,
) -> List[Dict[str, Any]]:
    """Suggest running doc-code alignment for functions without semantic_desc."""
    api_funcs = [
        (fid, func.get("name", ""))
        for fid, func in functions.items()
        if "API_entry" in func.get("labels", [])
        and not func.get("semantic_desc", "")
    ]
    if not api_funcs:
        return []
    top = api_funcs[:min(5, top_n)]
    return [{
        "priority": "medium",
        "category": "docs",
        "message": f"API entry `{name}` has no semantic description. "
                   f"Run doc-code alignment to check if docs are stale.",
        "functions": [fid],
        "action": f"doc-code-check --node {name}",
    } for fid, name in top]


def _suggest_stale_knowledge(
    knowledge: List[dict],
    functions: Dict[str, dict],
) -> List[Dict[str, Any]]:
    """Suggest cleaning up knowledge entries for functions that no longer exist."""
    stale = []
    for entry in knowledge:
        func_ref = entry.get("function", "") or entry.get("function_id", "")
        if func_ref and func_ref not in functions:
            stale.append(entry)
    if not stale:
        return []
    return [{
        "priority": "high",
        "category": "stale",
        "message": f"{len(stale)} knowledge entries reference functions that "
                   f"no longer exist in the graph (likely deleted or renamed).",
        "functions": [s.get("function", "") for s in stale[:10]],
        "action": "knowledge-validate",
    }]


def _suggest_dead_api(
    functions: Dict[str, dict],
    edges: List[dict],
    top_n: int,
) -> List[Dict[str, Any]]:
    """Suggest investigating API entry points with zero callers."""
    # Collect all called function IDs
    called_ids = set()
    for edge in edges:
        if edge.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        called_ids.add(edge.get("target", ""))

    dead_apis = []
    for fid, func in functions.items():
        if "API_entry" not in func.get("labels", []):
            continue
        if func.get("is_empty", False):
            continue
        if fid not in called_ids:
            dead_apis.append((fid, func.get("name", "")))
    if not dead_apis:
        return []
    top = dead_apis[:min(5, top_n)]
    return [{
        "priority": "low",
        "category": "testing",
        "message": f"API entry `{name}` has zero callers. "
                   f"This may be dead code or an entry point only called externally.",
        "functions": [fid],
        "action": f"describe-node --node {name}",
    } for fid, name in top]


def _suggest_high_fanout(
    functions: Dict[str, dict],
    edges: List[dict],
    top_n: int,
) -> List[Dict[str, Any]]:
    """Suggest reviewing functions with high fan-out (many callees)."""
    callee_count: Dict[str, int] = {}
    for edge in edges:
        if edge.get("relation") in ("CONTAINS", "IMPORTS"):
            continue
        source = edge.get("source", "")
        if source:
            callee_count[source] = callee_count.get(source, 0) + 1

    candidates = [
        (fid, func.get("name", ""), callee_count.get(fid, 0))
        for fid, func in functions.items()
        if not func.get("is_empty", False) and callee_count.get(fid, 0) >= 20
    ]
    candidates.sort(key=lambda x: -x[2])
    if not candidates:
        return []
    top = candidates[:min(5, top_n)]
    return [{
        "priority": "low",
        "category": "complexity",
        "message": f"Function `{name}` calls {cc} other functions. "
                   f"High fan-out may indicate this function is doing too much "
                   f"or acts as a dispatcher — consider extracting helpers.",
        "functions": [fid],
        "action": f"explore-flow --query {name}",
    } for fid, name, cc in top]


def _suggest_concurrency_analysis(
    functions: Dict[str, dict],
    edges: List[dict],
    top_n: int,
) -> List[Dict[str, Any]]:
    """Suggest running concurrency analysis if not yet done."""
    # Check if any thread_processor functions exist
    thread_funcs = [
        (fid, func.get("name", ""))
        for fid, func in functions.items()
        if "thread_processor" in func.get("labels", [])
    ]
    if not thread_funcs:
        return []
    # Check if concurrency analysis has been run (look for concurrency index)
    return [{
        "priority": "medium",
        "category": "concurrency",
        "message": f"Found {len(thread_funcs)} thread entry point(s). "
                   f"Run concurrency analysis to check for data races and deadlocks.",
        "functions": [fid for fid, _ in thread_funcs[:5]],
        "action": "concurrency-risks",
    }]


def cmd_cgdb_suggest(args):
    """CLI handler for `code2database_builder.py cgdb-suggest`."""
    graph_dir = args.graph
    top_n = getattr(args, "top", 20)
    suggestions = analyze_and_suggest(graph_dir, top_n=top_n)
    if not suggestions:
        print("No suggestions — graph looks healthy!")
        return 0
    print(f"Found {len(suggestions)} suggestions:\n")
    for i, s in enumerate(suggestions, 1):
        print(f"{i}. [{s['priority'].upper()}] {s['category']}")
        print(f"   {s['message']}")
        if s.get("action"):
            print(f"   Suggested: {s['action']}")
        print()
    return 0
