#!/usr/bin/env python3
"""Recipe registry: executable routing tables for the c2d umbrella.

Each recipe maps a question family ("is X thread-safe?", "what breaks
if I change X?") to a deterministic sequence of read-only CLI steps.
The registry is the executable counterpart of the routing tables in
SKILL.md / SKILL_analysis.md / SKILL_ops.md — the same routing
intelligence, but runnable via `c2d ask` instead of requiring the
agent to read docs and assemble commands by hand.

Design rules:
- Every step must be a read-only query command. The engine refuses
  steps whose command appears in entry.WRITE_COMMANDS, and the test
  suite pins every step against the real argparse tree.
- Placeholders ("{target}", "{from}", ...) are filled from explicit
  CLI flags first (--target/--from/...), then from named regex groups
  captured while classifying --question.
- Steps marked "optional": true are skipped (with a note) when their
  placeholders cannot be resolved; non-optional steps rely on
  placeholders covered by the recipe's "requires" list.

Adding a recipe = appending one entry here + tests in
tests/test_c2d_entry.py. No engine changes needed.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

# Placeholders any recipe may reference. Keep in sync with the explicit
# flags on the `c2d` parser (entry.cmd_c2d).
KNOWN_PARAMS = ("target", "from", "to", "query", "source")

RECIPES: List[Dict[str, Any]] = [
    {
        "name": "thread-safety",
        "summary": "Thread-safety assessment of one function",
        "requires": ["target"],
        "example": "is bdev_start thread safe?",
        "patterns": [
            r"(?:is|are)\s+([\w:.]+)\s+thread[\s-]?safe",
            r"thread[\s-]?safety\s+(?:of|for|check)\s+(?:the\s+)?(?:function\s+)?([\w:.]+)",
            r"check\s+(?:the\s+)?thread[\s-]?safety\s+of\s+([\w:.]+)",
        ],
        "steps": [
            {"cmd": "concurrency-analyze", "args": ["--node", "{target}"],
             "note": "pair-wise concurrency analysis"},
            {"cmd": "detect-races", "args": ["--node", "{target}"],
             "note": "race pairs involving the function"},
            {"cmd": "lock-coverage", "args": ["--node", "{target}"],
             "note": "lock-held regions"},
            {"cmd": "memory-ordering", "args": ["--node", "{target}"],
             "note": "memory-ordering annotations"},
        ],
    },
    {
        "name": "race-scan",
        "summary": "Graph-wide data-race and concurrency-risk scan",
        "requires": [],
        "example": "are there any data races?",
        "patterns": [
            r"(?:any|all|the)\s+data\s+race",
            r"race\s+scan",
            r"scan\s+for\s+races",
            r"concurrency\s+risks?",
            r"threading\s+risks?",
        ],
        "steps": [
            {"cmd": "concurrency-risks", "args": [],
             "note": "function-level risk pairs"},
            {"cmd": "detect-races", "args": [],
             "note": "cross-thread data races"},
        ],
    },
    {
        "name": "value-origin",
        "summary": "Where a value or variable comes from and flows to",
        "requires": ["target"],
        "example": "where does the nvme_request value come from?",
        "patterns": [
            r"value\s+flow\s+(?:of|for|from)\s+([\w:.]+)",
            r"where\s+does\s+([\w:.]+)\s+(?:get\s+set|come\s+from|originate)",
            r"origin\s+of\s+(?:the\s+)?(?:value|variable)\s+([\w:.]+)",
            r"where\s+does\s+(?:the\s+)?value\s+(?:of|for)\s+([\w:.]+)\s+come\s+from",
            r"where\s+does\s+(?:the\s+)?([\w:.]+)\s+variable\s+come\s+from",
        ],
        "steps": [
            {"cmd": "value-flow", "args": ["--node", "{target}"],
             "note": "DATA_FLOW propagation"},
            {"cmd": "data-dep", "args": ["--node", "{target}"],
             "note": "cross-function data dependencies"},
        ],
    },
    {
        "name": "impact",
        "summary": "Blast radius of changing a function",
        "requires": ["target"],
        "example": "what breaks if I change bdev_start?",
        "patterns": [
            r"(?:what|who)\s+breaks?\s+if\s+I\s+(?:change|modify|touch|refactor)\s+([\w:.]+)",
            r"impact\s+(?:of|on)\s+(?:changing\s+|modifying\s+)?([\w:.]+)",
            r"blast\s+radius\s+(?:of|for)\s+([\w:.]+)",
            r"(?:change|modify|refactor)\s+([\w:.]+)\s+impact",
        ],
        "steps": [
            {"cmd": "impact", "args": ["--node", "{target}"],
             "note": "impact analysis"},
            {"cmd": "blast-radius", "args": ["--node", "{target}"],
             "note": "affected APIs / tests / domains"},
            {"cmd": "neighbors", "args": ["--node", "{target}"],
             "note": "direct callers / callees"},
        ],
    },
    {
        "name": "call-path",
        "summary": "Call chain from function A to function B",
        "requires": ["from", "to"],
        "example": "call chain from spdk_app_start to bdev_start",
        "patterns": [
            r"(?:call\s+chain|path)\s+from\s+(\w+)\s+to\s+(\w+)",
            r"how\s+does\s+(\w+)\s+(?:reach|call|get\s+to)\s+(\w+)",
            r"does\s+(\w+)\s+(?:ever\s+)?call\s+(\w+)",
        ],
        "steps": [
            {"cmd": "path", "args": ["--from", "{from}", "--to", "{to}"],
             "note": "shortest call path"},
        ],
    },
    {
        "name": "path-feasibility",
        "summary": "Guards and feasibility of paths between two functions",
        "requires": ["from", "to"],
        "example": "is the path from io_submit to nvme_admin_cmd feasible?",
        "patterns": [
            r"is\s+the\s+path\s+from\s+(\w+)\s+to\s+(\w+)\s+(?:feasible|possible|reachable)",
            r"path\s+feasibility\s+(?:from|between)\s+(\w+)\s+(?:to|and)\s+(\w+)",
            r"can\s+(\w+)\s+reach\s+(\w+)\s+under\s+(?:these\s+)?constraints",
        ],
        "steps": [
            {"cmd": "path-guards", "args": ["--from", "{from}", "--to", "{to}"],
             "note": "guards on paths between the two functions"},
            {"cmd": "path-feasible", "args": ["--node", "{from}"],
             "note": "SMT feasibility walk"},
        ],
    },
    {
        "name": "invariants",
        "summary": "Invariants a function enforces",
        "requires": ["target"],
        "example": "what invariants does bdev_start enforce?",
        "patterns": [
            r"(?:what\s+)?invariants?\s+(?:does|enforce|of|for)\s+([\w:.]+)",
            r"preconditions?\s+(?:of|for)\s+([\w:.]+)",
            r"postconditions?\s+(?:of|for)\s+([\w:.]+)",
        ],
        "steps": [
            {"cmd": "extract-invariants", "args": ["--node", "{target}"],
             "note": "read-only invariant extraction"},
            {"cmd": "find-invariants", "args": [],
             "note": "previously stored invariants"},
        ],
    },
    {
        "name": "ffi",
        "summary": "Cross-language FFI boundaries and chains",
        "requires": [],
        "example": "which Python or Go functions call into C?",
        "patterns": [
            r"(?:ffi|foreign\s+function)\s+(?:calls?|boundar|interface)",
            r"cross[\s-]?language\s+calls?",
            r"(?:python|go|rust)\s+(?:calls?\s+into|bindings?\s+to)\s+c\b",
            r"which\s+[\w\s]*\s*call(?:s)?\s+into\s+C\b",
        ],
        "steps": [
            {"cmd": "ffi-detect", "args": [],
             "note": "detect binding sites (read-only)"},
            {"cmd": "ffi-list", "args": [],
             "note": "list boundary sites"},
            {"cmd": "ffi-trace", "args": ["--node", "{target}"],
             "note": "trace chains through a known site", "optional": True},
        ],
    },
    {
        "name": "provenance",
        "summary": "Which commits introduced or changed a function",
        "requires": ["target"],
        "example": "which commit introduced bdev_start?",
        "patterns": [
            r"which\s+commit\s+(?:introduced|added|changed|touched|last\s+changed)\s+([\w:.]+)",
            r"(?:who|what)\s+(?:introduced|wrote)\s+([\w:.]+)",
            r"blame\s+([\w:.]+)",
            r"history\s+of\s+([\w:.]+)",
        ],
        "steps": [
            {"cmd": "blame-node", "args": ["--node", "{target}"],
             "note": "introducing commit"},
            {"cmd": "node-history", "args": ["--node", "{target}"],
             "note": "node change history"},
            {"cmd": "find-commits", "args": ["--function", "{target}"],
             "note": "commits touching the function"},
        ],
    },
    {
        "name": "resource",
        "summary": "Who allocates and frees a resource",
        "requires": ["target"],
        "example": "who allocates and frees the io_buffer resource?",
        "patterns": [
            r"who\s+allocates?\s+(?:the\s+)?([\w:.]+)",
            r"who\s+frees?\s+(?:the\s+)?([\w:.]+)",
            r"(?:alloc|free)\s+sites?\s+(?:of|for)\s+([\w:.]+)",
            r"leak\w*\s+(?:of|for|on)\s+([\w:.]+)",
        ],
        "steps": [
            {"cmd": "who-allocates", "args": ["--resource", "{target}"],
             "note": "allocation sites"},
            {"cmd": "who-frees", "args": ["--resource", "{target}"],
             "note": "free sites"},
            {"cmd": "unbalanced-alloc-free", "args": [],
             "note": "graph-wide balance check"},
        ],
    },
    {
        "name": "quality",
        "summary": "Code-quality scan: cycles, recursion, bounds, loops, clones",
        "requires": [],
        "example": "run a quality scan for cycles and clones",
        "patterns": [
            r"quality\s+(?:scan|check|report)",
            r"(?:check|scan)\s+for\s+(?:cycles|recursion|clones?|infinite\s+loops?|bounds)",
            r"circular\s+(?:dependency|dependencies|calls?)",
            r"code\s+quality\s+(?:scan|check|report)",
        ],
        "steps": [
            {"cmd": "check-cycles", "args": [],
             "note": "call / include cycles"},
            {"cmd": "check-recursion", "args": [],
             "note": "recursion + termination staging"},
            {"cmd": "check-bounds", "args": [],
             "note": "array subscript guards"},
            {"cmd": "check-infinite-loop", "args": [],
             "note": "unbounded loops"},
            {"cmd": "check-clones", "args": [],
             "note": "near-clone groups"},
        ],
    },
    {
        "name": "doc-alignment",
        "summary": "Documentation-vs-code alignment check",
        "requires": [],
        "example": "is the documentation stale or aligned with the code?",
        "patterns": [
            r"(?:is\s+the\s+)?doc(?:umentation)?\s+(?:correct|stale|accurate|aligned)",
            r"doc[\s-]?code\s+(?:alignment|check|mismatch)",
            r"stale\s+docs?",
        ],
        "steps": [
            {"cmd": "doc-code-check", "args": [],
             "note": "mismatch scan"},
            {"cmd": "doc-alignment-report", "args": ["--source", "{source}"],
             "note": "full alignment report", "optional": True},
        ],
    },
    {
        "name": "explore",
        "summary": "Topic exploration: hybrid semantic + graph search",
        "requires": ["query"],
        "example": "explore the nvme queue handling architecture",
        "patterns": [
            r"explore\s+(?:the\s+)?(.+?)\s*$",
            r"(?:find|search)\s+(?:functions?|code)\s+(?:related\s+to|about)\s+(.+?)\s*$",
        ],
        "steps": [
            {"cmd": "hybrid-search", "args": ["--query", "{query}"],
             "note": "semantic + FTS fusion"},
            {"cmd": "explore-flow", "args": ["--query", "{query}"],
             "note": "graph exploration"},
        ],
    },
]

_RECIPE_INDEX = {r["name"]: r for r in RECIPES}


def get_recipe(name: str) -> Optional[Dict[str, Any]]:
    """Return the recipe registered under `name`, or None."""
    return _RECIPE_INDEX.get(name)


def classify_question(question: str) -> Tuple[Optional[Dict[str, Any]],
                                               Dict[str, str]]:
    """Match a natural-language question against the recipe patterns.

    Returns (recipe, extracted_params) or (None, {}). Scoring: a pattern
    that captures groups scores 1.0, a plain keyword pattern 0.8; ties
    resolve to the earlier recipe, so classification is deterministic.
    Captured groups map positionally onto the recipe's "requires" list
    (group 1 -> requires[0], group 2 -> requires[1]); empty groups are
    skipped without shifting the alignment.
    """
    if not question or not question.strip():
        return None, {}
    text = question.strip()
    best: Optional[Tuple[float, Dict[str, Any], Dict[str, str]]] = None
    for recipe in RECIPES:
        for pat in recipe["patterns"]:
            m = re.search(pat, text, re.IGNORECASE)
            if m is None:
                continue
            params: Dict[str, str] = {}
            requires = recipe["requires"]
            for i, g in enumerate(m.groups()):
                if g and i < len(requires):
                    params[requires[i]] = g.strip()
            score = 1.0 if m.groups() else 0.8
            if best is None or score > best[0]:
                best = (score, recipe, params)
    if best is None:
        return None, {}
    return best[1], best[2]


def missing_params(recipe: Dict[str, Any],
                   params: Dict[str, str]) -> List[str]:
    """Required placeholders that resolved to nothing."""
    return [p for p in recipe.get("requires", []) if not params.get(p)]
