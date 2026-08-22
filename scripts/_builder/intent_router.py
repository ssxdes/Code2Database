#!/usr/bin/env python3
"""Intent router: classify a natural-language question and route to a command.

Instead of forcing the user (or LLM) to pick among 105+ CLI commands, this
module maps a free-form question like "why is this function dead code?" to
the right command (e.g., `describe-node --explain-label dead_code`).

Design:
- Keyword + intent-pattern based classification (no ML dependencies).
- Each intent has a list of trigger patterns (regex/keyword) and a target
  command template with parameter extraction.
- Returns a structured suggestion: {command, args, confidence, reason}.
- Lightweight and extensible — new intents are added to INTENT_RULES.

CLI command:
    intent-query --question "<text>" [--graph <dir>]
"""
import re
from typing import Optional, List, Dict, Any


# Each rule has:
# - name: short identifier
# - patterns: list of regex patterns (case-insensitive) — any match triggers
# - command: target CLI command name
# - args: dict of arg-name → extraction function (or static value)
# - description: human-readable reason for the routing
# - min_confidence: float in [0, 1] — how strongly this matches

INTENT_RULES: List[Dict[str, Any]] = [
    {
        "name": "why_dead_code",
        "patterns": [
            r"why\s+(?:is\s+|does\s+)?(?:this\s+|the\s+)?(?:function|func|code)\s+(?:dead|unreachable|unused)",
            r"dead\s*code\s+reason",
            r"why\s+(?:is|does).+\bdead\b",
        ],
        "command": "describe-node",
        "args": {"explain-label": "dead_code"},
        "description": "Explains why a function is dead/unreachable code",
        "min_confidence": 0.8,
    },
    {
        "name": "why_ambiguous",
        "patterns": [
            r"why\s+(?:is|are).+ambiguous",
            r"why\s+uncertain",
            r"explain\s+ambig",
        ],
        "command": "describe-node",
        "args": {"explain-label": "ambiguous"},
        "description": "Explains why a function has ambiguous labeling",
        "min_confidence": 0.8,
    },
    {
        "name": "find_invokers",
        "patterns": [
            r"(?:who|what)\s+calls?\s+([\w.]+)",
            r"callers?\s+of\s+([\w.]+)",
            r"find\s+callers?\s+(?:for|of)\s+([\w.]+)",
        ],
        "command": "callers",
        "args": {"node": "{extracted_1}"},
        "description": "Find functions that call the named function",
        "min_confidence": 0.7,
    },
    {
        "name": "find_invoked",
        "patterns": [
            r"what\s+does\s+([\w.]+)\s+call",
            r"callees?\s+of\s+([\w.]+)",
            r"find\s+callees?\s+(?:for|of)\s+([\w.]+)",
        ],
        "command": "callees",
        "args": {"node": "{extracted_1}"},
        "description": "Find functions called by the named function",
        "min_confidence": 0.7,
    },
    {
        "name": "find_call_chain",
        "patterns": [
            r"call\s+chain\s+(?:from\s+)?(\w+)\s+to\s+(\w+)",
            r"path\s+from\s+(\w+)\s+to\s+(\w+)",
            r"how\s+does\s+(\w+)\s+reach\s+(\w+)",
        ],
        "command": "call-chain",
        "args": {"from": "{extracted_1}", "to": "{extracted_2}"},
        "description": "Find a call chain from one function to another",
        "min_confidence": 0.75,
    },
    {
        "name": "find_data_flow",
        "patterns": [
            r"data\s+flow\s+(?:from\s+)?(\w+)\s+to\s+(\w+)",
            r"value\s+flow\s+(?:from\s+)?(\w+)\s+to\s+(\w+)",
            r"how\s+does\s+data\s+flow\s+(?:from\s+)?(\w+)\s+to\s+(\w+)",
        ],
        "command": "value-flow",
        "args": {"from": "{extracted_1}", "to": "{extracted_2}"},
        "description": "Trace value flow from one function to another",
        "min_confidence": 0.75,
    },
    {
        "name": "find_locks",
        "patterns": [
            r"(?:what|which)\s+locks?\s+(?:does|did)\s+([\w.]+)\s+(?:hold|acquire|take)",
            r"lock\s+(?:pattern|held)\s+(?:in|for)\s+([\w.]+)",
            r"locking\s+(?:in|for)\s+([\w.]+)",
        ],
        "command": "lock-coverage",
        "args": {"node": "{extracted_1}"},
        "description": "Show lock-held regions in a function",
        "min_confidence": 0.7,
    },
    {
        "name": "find_race_conditions",
        "patterns": [
            r"race\s+condition",
            r"data\s+race",
            r"concurrency\s+(?:bug|issue|problem)",
        ],
        "command": "race-detect",
        "args": {},
        "description": "Detect potential data races",
        "min_confidence": 0.85,
    },
    {
        "name": "find_invariants",
        "patterns": [
            r"(?:what\s+are\s+|find\s+)?invariants?\s+(?:of|for)\s+([\w.]+)",
            r"preconditions?\s+(?:of|for)\s+([\w.]+)",
            r"postconditions?\s+(?:of|for)\s+([\w.]+)",
        ],
        "command": "find-invariants",
        "args": {"node": "{extracted_1}"},
        "description": "Find invariants for a function",
        "min_confidence": 0.7,
    },
    {
        "name": "find_ffi",
        "patterns": [
            r"(?:ffi|foreign\s+function)\s+(?:calls?|boundaries?)",
            r"cross[-\s]language\s+calls?",
            r"(?:python|go|rust)\s+(?:to|from)\s+c\s+calls?",
        ],
        "command": "ffi-list",
        "args": {},
        "description": "List FFI boundaries",
        "min_confidence": 0.8,
    },
    {
        "name": "find_concept",
        "patterns": [
            r"(?:c\+\+|cpp)\s+(?:concepts?|templates?)",
            r"(?:what|which)\s+concepts?\s+(?:satisfy|constraint)",
            r"template\s+(?:instantiat|specializ)",
        ],
        "command": "find-concepts",
        "args": {},
        "description": "List C++ concepts and template instantiations",
        "min_confidence": 0.75,
    },
    {
        "name": "explore_flow",
        "patterns": [
            r"explore\s+([\w\s]+?)\s*[?.!]*$",
            r"find\s+(?:functions?|code)\s+(?:related\s+to|about)\s+([\w\s]+?)\s*[?.!]*$",
            r"search\s+for\s+([\w\s]+?)\s*[?.!]*$",
        ],
        "command": "explore-flow",
        "args": {"query": "{extracted_1}"},
        "description": "Explore the invocation graph for a topic",
        "min_confidence": 0.6,
    },
    {
        "name": "describe_node",
        "patterns": [
            r"(?:describe|explain|what\s+is)\s+([\w.]+)",
            r"(?:tell\s+me\s+about|show\s+me)\s+([\w.]+)",
        ],
        "command": "describe-node",
        "args": {"node": "{extracted_1}"},
        "description": "Describe a node in detail",
        "min_confidence": 0.5,
    },
    {
        "name": "doc_code_check",
        "patterns": [
            r"doc(?:umentation)?\s+code\s+(?:mismatch|alignment|check)",
            r"stale\s+doc",
            r"is\s+the\s+doc(?:umentation)?\s+(?:correct|stale|accurate)",
        ],
        "command": "doc-code-check",
        "args": {},
        "description": "Check doc-code alignment",
        "min_confidence": 0.8,
    },
    {
        "name": "daemon_status",
        "patterns": [
            r"daemon\s+status",
            r"is\s+the\s+daemon\s+(?:running|up|alive)",
            r"sync\s+status",
        ],
        "command": "daemon-status",
        "args": {},
        "description": "Check daemon status",
        "min_confidence": 0.85,
    },
    {
        "name": "path_feasible",
        "patterns": [
            r"path\s+feasib",
            r"is\s+(?:this\s+|the\s+)?path\s+(?:feasible|possible|reachable)",
            r"can\s+(?:this\s+|the\s+)?path\s+execute",
        ],
        "command": "path-feasible",
        "args": {},
        "description": "Check path feasibility with SMT",
        "min_confidence": 0.8,
    },
]


def _extract_pattern_groups(pattern: str, text: str) -> Optional[List[str]]:
    """If pattern matches text, return the captured groups (or [text] if none)."""
    m = re.search(pattern, text, re.IGNORECASE)
    if m is None:
        return None
    groups = list(m.groups())
    if not groups:
        return [text.strip()]
    return groups


def _resolve_args(args_template: Dict[str, str],
                   extracted: List[str]) -> Dict[str, str]:
    """Resolve {extracted_N} placeholders in args_template."""
    resolved = {}
    for k, v in args_template.items():
        if isinstance(v, str) and v.startswith("{extracted_"):
            try:
                idx = int(v[len("{extracted_"):].rstrip("}")) - 1
                if 0 <= idx < len(extracted):
                    resolved[k] = extracted[idx]
                else:
                    resolved[k] = ""
            except (ValueError, IndexError):
                resolved[k] = v
        else:
            resolved[k] = v
    return resolved


def classify_intent(question: str) -> Optional[Dict[str, Any]]:
    """Classify a natural-language question and return a routing suggestion.

    Returns None if no intent matches with sufficient confidence.
    Otherwise returns a dict with:
        - command: target CLI command
        - args: dict of arg name → value (with extracted parameters)
        - confidence: float in [0, 1]
        - reason: human-readable explanation
        - matched_intent: the intent rule name
    """
    if not question or not question.strip():
        return None
    text = question.strip()
    best_match = None
    best_confidence = 0.0
    for rule in INTENT_RULES:
        for pattern in rule["patterns"]:
            extracted = _extract_pattern_groups(pattern, text)
            if extracted is None:
                continue
            # Confidence is the rule's min_confidence, boosted slightly
            # when the pattern has captures (more specific)
            confidence = rule["min_confidence"]
            if len(extracted) > 1 or (extracted and extracted[0] != text):
                confidence = min(1.0, confidence + 0.1)
            if confidence > best_confidence:
                best_confidence = confidence
                best_match = {
                    "command": rule["command"],
                    "args": _resolve_args(rule["args"], extracted),
                    "confidence": confidence,
                    "reason": rule["description"],
                    "matched_intent": rule["name"],
                }
    return best_match


def intent_query(question: str, graph_dir: str = "") -> Dict[str, Any]:
    """Top-level intent-query entry point.

    Returns a dict with:
        - ok: True if a match was found
        - question: the original question
        - routing: the classify_intent result (or None)
        - suggestion: human-readable suggestion string
    """
    routing = classify_intent(question)
    if routing is None:
        return {
            "ok": False,
            "question": question,
            "routing": None,
            "suggestion": (
                f"No matching intent found for: {question!r}. "
                "Try rephrasing or use a specific command."
            ),
        }
    args_str = " ".join(f"--{k} {v}" for k, v in routing["args"].items()
                        if v)
    cmd = routing["command"]
    if args_str:
        suggestion = f"code2database_builder.py {cmd} {args_str}"
    else:
        suggestion = f"code2database_builder.py {cmd}"
    return {
        "ok": True,
        "question": question,
        "routing": routing,
        "suggestion": suggestion,
    }


def cmd_intent_query(args):
    """CLI handler for intent-query."""
    question = args.question
    result = intent_query(question, getattr(args, "graph", ""))
    if not result["ok"]:
        print(result["suggestion"])
        return 1
    routing = result["routing"]
    print(f"Intent: {routing['matched_intent']}")
    print(f"Confidence: {routing['confidence']:.2f}")
    print(f"Reason: {routing['reason']}")
    print(f"Suggested command: {result['suggestion']}")
    return 0
