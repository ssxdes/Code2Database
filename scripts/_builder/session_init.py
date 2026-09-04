#!/usr/bin/env python3
"""One-shot session context loader for Code2Database.

The session-start entry point for BOTH audiences:
- AI agents: one command loads everything needed to continue exploring
  a project across sessions — the project brief (mandatory rules,
  modes), the memory digest (veteran experience), graph state, and the
  questions nobody has answered yet (known unknowns).
- Humans: the same output is a "project context" briefing; the web UI
  renders the same data interactively.

Why this exists: the brief alone doesn't carry accumulated experience,
memory search requires knowing what to ask, and the known-unknowns
feedback loop (queries that repeatedly miss) was invisible unless
someone ran kb-known-unknowns by hand. session-init bundles all four
layers in a stable, prompt-ready form.
"""

import json
import os
import sys
from typing import List, Dict, Any, Optional

import logging


def build_session_context(graph_dir: str, memory_top: int = 10) -> dict:
    """Assemble the full session context for a graph dir.

    Never raises for missing pieces — each layer degrades to a hint.
    """
    from _builder.brief import load_brief, render_brief_prompt, \
        compute_graph_stats

    # --- Layer 1: project brief ---
    brief = load_brief(graph_dir)
    # render_brief_prompt handles brief=None with a bootstrap hint
    brief_rendered = render_brief_prompt(graph_dir, brief)

    # --- Layer 2: memory digest (veteran experience) ---
    memory: Dict[str, Any] = {"stats": None, "digest": []}
    try:
        from _builder.memory_store import MemoryStore
        store = MemoryStore(graph_dir)
        memory["stats"] = store.stats()
        memory["digest"] = store.digest(limit=memory_top)
    except Exception as e:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        memory["error"] = str(e)

    # --- Layer 3: graph state ---
    graph = compute_graph_stats(graph_dir)
    drift = None
    if brief is not None:
        stats = brief.get("graph_stats") or {}
        for key in ("nodes", "edges"):
            old, new = stats.get(key, 0), graph.get(key, 0)
            if old and new and abs(new - old) / max(old, 1) > 0.2:
                drift = (f"graph {key} drifted {old} → {new} (>20%) "
                         f"since last brief refresh")
                break

    # --- Layer 3b: source-vs-graph freshness ---
    # A stale graph silently produces wrong answers (and wrong saved
    # memories) — surface it at session start so the agent rebuilds
    # before trusting the graph.
    freshness: Optional[Dict[str, Any]] = None
    try:
        from _builder.cgdb_freshness import check_freshness
        src_root = os.path.dirname(os.path.abspath(graph_dir))
        fr = check_freshness(graph_dir, src_root)
        freshness = {
            "is_fresh": fr.get("is_fresh", True),
            "staleness_ratio": fr.get("staleness_ratio", 0.0),
            "changed_files": fr.get("changed_count", 0),
            "new_files": fr.get("new_count", 0),
            "deleted_files": fr.get("deleted_count", 0),
            "git_head_changed": fr.get("git_head_changed", False),
            "recommendation": fr.get("recommendation", ""),
            "samples": (fr.get("changed_files") or [])[:3],
        }
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        freshness = None

    # --- Layer 4: known unknowns (unanswered recurring queries) ---
    known_unknowns: List[dict] = []
    db_path = os.path.join(graph_dir, "code2database.db")
    if os.path.exists(db_path):
        try:
            from _builder.kb_index import get_known_unknowns
            known_unknowns = get_known_unknowns(graph_dir, top_n=5,
                                                min_occurrences=2)
        except Exception:
            logging.getLogger(__name__).debug("silent exception",
                                              exc_info=True)

    # --- Hints ---
    hints: List[str] = []
    if brief is None:
        hints.append("No project brief yet — run `brief-extract` to "
                     "bootstrap, then curate with `brief-update`")
    else:
        if not brief.get("hard_rules"):
            hints.append("Brief has no hard_rules — document mandatory "
                         "macros/branches if any exist")
        for qp in (brief.get("query_paths") or [])[:5]:
            hints.append(qp)
    if memory["stats"] and memory["stats"].get("active_entries", 0) == 0:
        hints.append("Memory store is empty — save the first Q&A with "
                     "`save-memory --question ... --answer ... "
                     "--category path/to/topic --author you`")
    if drift:
        hints.append(drift + " — run `brief-update --refresh-stats` "
                     "after reviewing")
    if freshness is not None and not freshness.get("is_fresh", True):
        detail = (f"{freshness['changed_files']} changed / "
                  f"{freshness['new_files']} new / "
                  f"{freshness['deleted_files']} deleted")
        samples = ", ".join(freshness.get("samples") or [])
        hint = (f"Graph is STALE ({detail}"
                + (f"; e.g. {samples}" if samples else "")
                + f") — {freshness.get('recommendation', 'rebuild')} "
                "before trusting graph answers")
        hints.append(hint)
    for ku in known_unknowns[:3]:
        hints.append(f"Unanswered (asked {ku['occurrences']}×): "
                     f"\"{ku['query']}\" — know the answer? save-memory it")

    return {
        "brief": brief,
        "brief_rendered": brief_rendered,
        "memory": memory,
        "graph": graph,
        "brief_drift": drift,
        "freshness": freshness,
        "known_unknowns": known_unknowns,
        "hints": hints,
    }


def render_session_context(ctx: dict) -> str:
    """Render the session context as compact, prompt-ready text."""
    lines: List[str] = ["=== Code2Database Session Context ==="]

    # Brief
    lines.append("\n--- Project Brief ---")
    if ctx["brief_rendered"]:
        lines.append(ctx["brief_rendered"].rstrip())
    else:
        lines.append("(no brief — run brief-extract to bootstrap)")

    # Memory digest
    mem = ctx["memory"]
    stats = mem.get("stats")
    lines.append("\n--- Memory Digest (veteran experience) ---")
    if stats:
        lines.append(f"{stats.get('active_entries', 0)} active / "
                     f"{stats.get('categories', 0)} categories / "
                     f"L0 hot: {stats.get('layer_counts', {}).get('L0', 0)}")
        digest = mem.get("digest") or []
        if digest:
            for i, e in enumerate(digest, 1):
                meta = f"w={e['weight']}"
                if e["access_count"]:
                    meta += f", {e['access_count']} reads"
                if e["author"]:
                    meta += f", {e['author']}"
                cat = f"[{e['category']}] " if e["category"] else ""
                syms = e.get("symbols") or []
                sym_note = f" ⟨{', '.join(syms)}⟩" if syms else ""
                lines.append(f"{i}. {cat}({meta}) {e['question']}{sym_note}")
                if e["answer"]:
                    lines.append(f"   → {e['answer']}")
        else:
            lines.append("(no active memories yet)")
    else:
        lines.append("(memory store unavailable)")

    # Graph
    g = ctx["graph"]
    lines.append("\n--- Graph ---")
    lines.append(f"{g.get('nodes', 0)} nodes | {g.get('edges', 0)} edges | "
                 f"{g.get('domains', 0)} domains")
    if ctx.get("brief_drift"):
        lines.append(f"⚠ {ctx['brief_drift']}")
    fr = ctx.get("freshness")
    if fr is not None:
        if fr.get("is_fresh"):
            lines.append("✓ graph fresh (source files match manifest)")
        else:
            samples = ", ".join(fr.get("samples") or [])
            lines.append(
                f"⚠ graph STALE: {fr['changed_files']} changed / "
                f"{fr['new_files']} new / {fr['deleted_files']} deleted "
                f"(staleness {int(fr['staleness_ratio'] * 100)}%)"
                + (f" — e.g. {samples}" if samples else ""))
            if fr.get("recommendation"):
                lines.append(f"  → {fr['recommendation']}")

    # Known unknowns
    ku = ctx.get("known_unknowns") or []
    lines.append("\n--- Known Unknowns (unanswered — save answers) ---")
    if ku:
        for k in ku:
            lines.append(f"- \"{k['query']}\" (asked {k['occurrences']}×, "
                         f"last {k.get('last_asked', '?')})")
    else:
        lines.append("(none — every logged query matched)")

    # Hints
    hints = ctx.get("hints") or []
    if hints:
        lines.append("\n--- Suggested Next Steps ---")
        for h in hints:
            lines.append(f"- {h}")

    return "\n".join(lines) + "\n"


def cmd_session_init(args):
    """Print the full session context (the session-start entry point)."""
    graph_dir = args.graph
    ctx = build_session_context(graph_dir,
                                memory_top=int(getattr(args, "top", 10)))
    if getattr(args, "json", False):
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
        return
    print(render_session_context(ctx))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Session init")
    parser.add_argument("--graph", required=True)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cmd_session_init(args)
