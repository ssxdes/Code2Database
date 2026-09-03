#!/usr/bin/env python3
"""Project brief — the lean, mandatory-load knowledge for Code2Database.

Knowledge is the SMALL, curated, fixed description of THIS project:
architecture, functionality, design, usage. It is what an AI MUST load
into its prompt before working on the project via the C2D skill. It is
NOT an accumulation store (that's memory/memory.db) — it is a tight
brief with a size budget, adjusted only in small scope when the
architecture genuinely changes.

Storage: graph_dir/knowledge/brief.json

Sections:
    project          — project name
    one_liner        — one-line positioning
    description      — 2-4 sentence architecture/function description
    hard_rules       — [{rule, type: macro|branch|config|api, detail,
                        evidence}]  e.g. "强制开启 SPDK_CONFIG_PCI 宏"
    modes            — [{name, when, differences}]
                        e.g. pcie / tcp / rdma transport usage scenarios
    key_abstractions — [{name, role}]
    conventions      — [str] coding/naming conventions
    pitfalls         — [str] known traps
    query_paths      — [str] suggested C2D query routes for this project
    must_know        — free-form final word
    graph_stats      — auto-refreshed {nodes, edges, domains}

Size budget: rendered prompt warns above 3000 chars, errors above 6000
(knowledge must stay lean; overflow belongs in memory or the graph).
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import logging

BRIEF_SCHEMA_VERSION = 1
SIZE_WARN_CHARS = 3000
SIZE_ERROR_CHARS = 6000

_ITEM_SECTIONS = ("hard_rules", "modes", "key_abstractions",
                  "conventions", "pitfalls", "query_paths")
_SCALAR_SECTIONS = ("project", "one_liner", "description", "must_know")
_ALL_SECTIONS = _ITEM_SECTIONS + _SCALAR_SECTIONS

_EMPTY_BRIEF = {
    "schema_version": BRIEF_SCHEMA_VERSION,
    "project": "",
    "one_liner": "",
    "description": "",
    "hard_rules": [],
    "modes": [],
    "key_abstractions": [],
    "conventions": [],
    "pitfalls": [],
    "query_paths": [],
    "must_know": "",
    "graph_stats": {},
    "updated_at": "",
}


def brief_path(graph_dir: str) -> str:
    return os.path.join(graph_dir, "knowledge", "brief.json")


def load_brief(graph_dir: str) -> Optional[dict]:
    """Load the brief; None when absent, default-shape on corruption."""
    path = brief_path(graph_dir)
    if not os.path.exists(path):
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[brief] Warning: {path} is corrupt ({e}); "
              f"treating as empty", file=sys.stderr)
        data = {}
    if not isinstance(data, dict):
        print(f"[brief] Warning: {path} is not a JSON object; "
              f"treating as empty", file=sys.stderr)
        data = {}
    brief = json.loads(json.dumps(_EMPTY_BRIEF))
    for key in _ALL_SECTIONS:
        if key in data:
            brief[key] = data[key]
    brief["graph_stats"] = data.get("graph_stats", {}) or {}
    if data.get("updated_at"):
        brief["updated_at"] = data["updated_at"]
    return brief


def save_brief(graph_dir: str, brief: dict) -> str:
    """Atomically write the brief (tmp + rename, like the old store)."""
    path = brief_path(graph_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    brief["schema_version"] = BRIEF_SCHEMA_VERSION
    brief["updated_at"] = datetime.now().isoformat()
    tmp = path + ".tmp." + str(os.getpid())
    Path(tmp).write_text(
        json.dumps(brief, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Graph stats
# ---------------------------------------------------------------------------

def compute_graph_stats(graph_dir: str) -> dict:
    """Node/edge/domain counts from the built graph (streaming, cheap)."""
    master_path = os.path.join(graph_dir, "code2database_master.json")
    stats = {"nodes": 0, "edges": 0, "domains": 0, "generated_at":
             datetime.now().isoformat()}
    if not os.path.exists(master_path):
        return stats
    try:
        master = json.loads(
            Path(master_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return stats
    domains = master.get("domains", {}) or {}
    stats["domains"] = len(domains)
    for fname in domains.values():
        fpath = os.path.join(graph_dir, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        nodes = data.get("nodes", [])
        stats["nodes"] += sum(1 for n in nodes
                              if not n.get("is_empty", False))
        stats["edges"] += len(data.get("edges", []))
    return stats


def refresh_graph_stats(graph_dir: str, brief: Optional[dict] = None) -> dict:
    """Refresh the auto section of the brief (in place + saved)."""
    if brief is None:
        brief = load_brief(graph_dir)
        if brief is None:
            brief = json.loads(json.dumps(_EMPTY_BRIEF))
    brief["graph_stats"] = compute_graph_stats(graph_dir)
    save_brief(graph_dir, brief)
    return brief


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_brief_prompt(graph_dir: str, brief: dict = None) -> str:
    """Render the brief as compact prompt text (the session-load form)."""
    if brief is None:
        brief = load_brief(graph_dir)
        if brief is None:
            return ("No project brief found. Initialize one with "
                    "`brief-extract`, then curate it with `brief-update`.")
    lines: List[str] = []
    project = brief.get("project", "") or "(unnamed project)"
    lines.append(f"# Project Brief: {project}")
    if brief.get("one_liner"):
        lines.append(f"\n{brief['one_liner']}")
    if brief.get("description"):
        lines.append(f"\n{brief['description']}")

    hard_rules = brief.get("hard_rules") or []
    if hard_rules:
        lines.append("\n## Hard Rules (MUST follow)")
        for hr in hard_rules:
            if isinstance(hr, str):
                lines.append(f"- {hr}")
                continue
            rule = hr.get("rule", "")
            htype = hr.get("type", "")
            detail = hr.get("detail", "")
            evidence = hr.get("evidence", "")
            entry = f"- [{htype}] {rule}" if htype else f"- {rule}"
            if detail:
                entry += f" — {detail}"
            if evidence:
                entry += f" (evidence: {evidence})"
            lines.append(entry)

    modes = brief.get("modes") or []
    if modes:
        lines.append("\n## Usage Modes (pick by scenario)")
        for m in modes:
            if isinstance(m, str):
                lines.append(f"- {m}")
                continue
            name = m.get("name", "")
            when = m.get("when", "")
            diff = m.get("differences", "")
            entry = f"- **{name}**"
            if when:
                entry += f": use when {when}"
            if diff:
                entry += f" — {diff}"
            lines.append(entry)

    abstractions = brief.get("key_abstractions") or []
    if abstractions:
        lines.append("\n## Key Abstractions")
        for ab in abstractions:
            if isinstance(ab, str):
                lines.append(f"- {ab}")
                continue
            name = ab.get("name", "")
            role = ab.get("role", "")
            lines.append(f"- **{name}**: {role}" if role else f"- {name}")

    for title, key in (("Conventions", "conventions"),
                       ("Pitfalls", "pitfalls"),
                       ("Suggested Query Paths", "query_paths")):
        items = brief.get(key) or []
        if items:
            lines.append(f"\n## {title}")
            for item in items:
                lines.append(f"- {item}")

    if brief.get("must_know"):
        lines.append(f"\n## Must Know\n{brief['must_know']}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Update operations
# ---------------------------------------------------------------------------

def _coerce_value(field: str, raw: str) -> Any:
    """Try JSON for item sections; fall back to plain string."""
    try:
        parsed = json.loads(raw)
        return parsed
    except (json.JSONDecodeError, TypeError):
        return raw


def brief_update(graph_dir: str, set_field: str = None, set_value: str = None,
                 add_section: str = None, add_value: str = None,
                 remove_section: str = None, remove_index: int = None,
                 refresh_stats: bool = False) -> dict:
    """Apply one update operation to the brief and save it."""
    brief = load_brief(graph_dir)
    if brief is None:
        brief = json.loads(json.dumps(_EMPTY_BRIEF))
        print("[brief] No brief found — initializing an empty one. "
              "Consider `brief-extract` for a graph-informed template.",
              file=sys.stderr)

    changed = False

    if set_field is not None:
        if set_field not in _SCALAR_SECTIONS:
            raise ValueError(
                f"--set field must be one of {_SCALAR_SECTIONS}, "
                f"got {set_field!r}")
        brief[set_field] = set_value if set_value is not None else ""
        changed = True

    if add_section is not None:
        if add_section not in _ITEM_SECTIONS:
            raise ValueError(
                f"--add section must be one of {_ITEM_SECTIONS}, "
                f"got {add_section!r}")
        value = _coerce_value(add_section, add_value or "")
        if add_section in ("hard_rules", "modes", "key_abstractions"):
            if not isinstance(value, dict):
                raise ValueError(
                    f"{add_section} items must be JSON objects "
                    f"(e.g. '{{\"rule\": ...}}')")
        else:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{add_section} items must be "
                                 "non-empty strings")
        brief.setdefault(add_section, []).append(value)
        changed = True

    if remove_section is not None:
        if remove_section not in _ITEM_SECTIONS:
            raise ValueError(
                f"--remove section must be one of {_ITEM_SECTIONS}, "
                f"got {remove_section!r}")
        items = brief.get(remove_section) or []
        if remove_index is None or not (0 <= remove_index < len(items)):
            raise ValueError(
                f"--index must be 0..{len(items) - 1} for "
                f"{remove_section} (currently {len(items)} items)")
        removed = items.pop(remove_index)
        brief[remove_section] = items
        print(f"Removed {remove_section}[{remove_index}]: "
              f"{json.dumps(removed, ensure_ascii=False)[:120]}")
        changed = True

    if refresh_stats:
        brief["graph_stats"] = compute_graph_stats(graph_dir)
        changed = True

    if not changed:
        raise ValueError("no operation specified (use --set/--add/"
                         "--remove/--refresh-stats)")

    save_brief(graph_dir, brief)
    return brief


def brief_extract(graph_dir: str) -> dict:
    """Bootstrap/refresh a brief template from the current graph.

    Preserves already-curated content; only fills graph_stats and, for
    a brand-new brief, seeds an empty structure with guidance notes.
    Also writes the brief so subsequent brief-update calls work.
    """
    existing = load_brief(graph_dir)
    brief = existing if existing is not None \
        else json.loads(json.dumps(_EMPTY_BRIEF))
    brief["graph_stats"] = compute_graph_stats(graph_dir)
    save_brief(graph_dir, brief)
    return brief


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_brief(graph_dir: str) -> dict:
    """Schema + size budget + graph drift validation.

    Returns {ok, errors: [...], warnings: [...], rendered_chars,
    estimated_tokens}.
    """
    errors: List[str] = []
    warnings: List[str] = []
    brief = load_brief(graph_dir)
    if brief is None:
        return {"ok": False,
                "errors": ["brief.json not found — run brief-extract "
                           "to initialize"],
                "warnings": [], "rendered_chars": 0,
                "estimated_tokens": 0}

    if not brief.get("project"):
        warnings.append("'project' is empty")
    if not brief.get("description"):
        warnings.append("'description' is empty — the brief should at "
                        "least describe the architecture/functionality")
    if not brief.get("hard_rules"):
        warnings.append("'hard_rules' is empty — document mandatory "
                        "macros/branches/configs if any exist")

    # Item shapes
    for i, hr in enumerate(brief.get("hard_rules") or []):
        if isinstance(hr, dict) and not hr.get("rule"):
            errors.append(f"hard_rules[{i}] is missing 'rule'")
    for i, m in enumerate(brief.get("modes") or []):
        if isinstance(m, dict) and not m.get("name"):
            errors.append(f"modes[{i}] is missing 'name'")
    for i, ab in enumerate(brief.get("key_abstractions") or []):
        if isinstance(ab, dict) and not ab.get("name"):
            errors.append(f"key_abstractions[{i}] is missing 'name'")

    # Size budget
    rendered = render_brief_prompt(graph_dir, brief)
    n_chars = len(rendered)
    est_tokens = n_chars // 4
    if n_chars > SIZE_ERROR_CHARS:
        errors.append(
            f"rendered brief is {n_chars} chars (> {SIZE_ERROR_CHARS}) — "
            "knowledge must stay lean; move overflow into memory "
            "(save-memory) or the graph")
    elif n_chars > SIZE_WARN_CHARS:
        warnings.append(
            f"rendered brief is {n_chars} chars (> {SIZE_WARN_CHARS}) — "
            "consider trimming")

    # Graph drift
    stats = brief.get("graph_stats") or {}
    if stats:
        current = compute_graph_stats(graph_dir)
        for key in ("nodes", "edges"):
            old, new = stats.get(key, 0), current.get(key, 0)
            if old and new and abs(new - old) / max(old, 1) > 0.2:
                warnings.append(
                    f"graph {key} drifted {old} → {new} (>20%) since "
                    "last refresh — run brief-update --refresh-stats "
                    "after reviewing architecture changes")

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "rendered_chars": n_chars, "estimated_tokens": est_tokens}


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def cmd_knowledge_brief(args):
    """Render the project brief as prompt text (session-start load)."""
    graph_dir = args.graph
    if getattr(args, "json", False):
        brief = load_brief(graph_dir)
        if brief is None:
            print("No project brief found. Run brief-extract to "
                  "initialize.", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(brief, ensure_ascii=False, indent=2))
        return
    rendered = render_brief_prompt(graph_dir)
    print(rendered)
    n_chars = len(rendered)
    if n_chars > SIZE_WARN_CHARS and "No project brief" not in rendered:
        print(f"[brief] WARNING: {n_chars} chars (~{n_chars // 4} tokens) "
              f"exceeds the {SIZE_WARN_CHARS}-char budget",
              file=sys.stderr)


def cmd_brief_update(args):
    """Update a section of the project brief (small-scope adjustments)."""
    graph_dir = args.graph
    remove_index = None
    if getattr(args, "index", None) is not None \
            and str(getattr(args, "index", "")) != "":
        remove_index = int(args.index)
    try:
        brief_update(
            graph_dir,
            set_field=getattr(args, "set", None),
            set_value=getattr(args, "value", None),
            add_section=getattr(args, "add", None),
            add_value=getattr(args, "value", None),
            remove_section=getattr(args, "remove", None),
            remove_index=remove_index,
            refresh_stats=bool(getattr(args, "refresh_stats", False)),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print("Brief updated.")


def cmd_brief_extract(args):
    """Initialize/refresh the brief template from graph stats."""
    graph_dir = args.graph
    brief = brief_extract(graph_dir)
    stats = brief.get("graph_stats", {})
    print(f"Brief template ready at {brief_path(graph_dir)}")
    print(f"  Graph stats: {stats.get('nodes', 0)} nodes, "
          f"{stats.get('edges', 0)} edges, "
          f"{stats.get('domains', 0)} domains")
    print("Next: curate with brief-update, e.g.:")
    print("  brief-update --set one_liner --value '...'")
    print("  brief-update --add hard_rules --json "
          "'{\"rule\": \"...\", \"type\": \"macro\"}'")
    print("  brief-update --add modes --json "
          "'{\"name\": \"pcie\", \"when\": \"...\", "
          "\"differences\": \"...\"}'")


def cmd_brief_validate(args):
    """Validate the brief (schema, size budget, graph drift)."""
    result = validate_brief(args.graph)
    if result["errors"]:
        print("ERRORS:")
        for e in result["errors"]:
            print(f"  - {e}")
    if result["warnings"]:
        print("WARNINGS:")
        for w in result["warnings"]:
            print(f"  - {w}")
    status = "VALID" if result["ok"] else "INVALID"
    print(f"\nBrief {status}: {result['rendered_chars']} chars "
          f"(~{result['estimated_tokens']} tokens)")
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Project brief")
    parser.add_argument("--graph", required=True)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("show")
    p_set = sub.add_parser("update")
    p_set.add_argument("--set", default=None)
    p_set.add_argument("--add", default=None)
    p_set.add_argument("--remove", default=None)
    p_set.add_argument("--index", default=None)
    p_set.add_argument("--value", default=None)
    p_set.add_argument("--refresh-stats", dest="refresh_stats",
                       action="store_true")
    sub.add_parser("extract")
    sub.add_parser("validate")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    {"show": cmd_knowledge_brief, "update": cmd_brief_update,
     "extract": cmd_brief_extract,
     "validate": cmd_brief_validate}[args.command](args)
