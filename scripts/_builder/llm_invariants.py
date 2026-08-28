"""LLM-assisted invariant extraction with consensus and continuous confidence.

Extends the rule-based extract_invariants_for_node with an optional LLM
channel that can propose invariants from the function body and signature.
To reduce hallucination, the LLM is called N times (default 3) and only
invariants that appear in >= 2 of the N responses are kept (majority
consensus). Each accepted invariant gets a continuous confidence_score
in [0.0, 1.0] computed from agreement ratio and rule-based corroboration.

The LLM is invoked only when explicitly requested (--use-llm flag) — the
default extraction path remains rule-based and offline.

Consensus protocol:
  1. Call LLM N times with the same prompt (temperature > 0 for variance).
  2. Normalize each response to a list of invariant dicts (pre/post/loop).
  3. Group by (kind, condition_string) — case-insensitive, whitespace-trimmed.
  4. Accept invariants that appear in >= ceil(N/2) responses.
  5. confidence_score = (agreement_count / N) * corroboration_bonus
     corroboration_bonus = 1.0 + 0.2 if a rule-based invariant matches

If LLM is unavailable or all calls fail, returns the rule-based invariants
unchanged with confidence_score = 0.5 (rule-based only).
"""
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import logging


def _normalize_condition(cond: str) -> str:
    """Normalize a condition string for comparison.

    Lowercases, collapses whitespace, strips surrounding spaces.
    """
    if not cond:
        return ""
    return re.sub(r"\s+", " ", cond.strip().lower())


def _group_invariants_by_kind(invariants: List[Dict]) -> Dict[Tuple[str, str], List[Dict]]:
    """Group invariants by (kind, normalized_condition).

    Returns a dict mapping (kind, normalized_condition) -> list of variants.
    """
    groups: Dict[Tuple[str, str], List[Dict]] = {}
    for inv in invariants:
        kind = inv.get("kind", "")
        cond = _normalize_condition(inv.get("condition", ""))
        key = (kind, cond)
        groups.setdefault(key, []).append(inv)
    return groups


def _parse_llm_invariants(response_text: str) -> List[Dict]:
    """Parse an LLM response into a list of invariant dicts.

    Accepts JSON of the form:
        {"invariants": [
            {"kind": "precondition", "condition": "x != NULL",
             "evidence": "if (!x) return -EINVAL;"},
            ...
        ]}

    Falls back to per-line parsing if JSON parsing fails:
        precondition: x != NULL
        postcondition: returns 0 on success
    """
    if not response_text:
        return []
    invariants: List[Dict] = []

    # Try JSON first
    try:
        data = json.loads(response_text)
        if isinstance(data, dict) and "invariants" in data:
            for inv in data["invariants"]:
                if isinstance(inv, dict) and "kind" in inv and "condition" in inv:
                    invariants.append({
                        "kind": inv["kind"],
                        "condition": inv["condition"],
                        "evidence": inv.get("evidence", ""),
                        "line": inv.get("line", 0),
                    })
        return invariants
    except json.JSONDecodeError:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        pass
    valid_kinds = {"precondition", "postcondition", "loop_invariant"}
    for line in response_text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        kind, _, cond = line.partition(":")
        kind = kind.strip().lower().replace(" ", "_")
        if kind in valid_kinds and cond.strip():
            invariants.append({
                "kind": kind,
                "condition": cond.strip(),
                "evidence": "",
                "line": 0,
            })
    return invariants


def _call_llm(prompt: str, model: str = "") -> Optional[str]:
    """Call the LLM with a prompt. Returns the response text or None on failure.

    This is a thin wrapper that tries to use whichever LLM client is
    configured in the environment. If no client is available, returns None.
    """
    # Try OpenAI-compatible API via env vars
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    api_base = os.environ.get("OPENAI_API_BASE") or os.environ.get(
        "LLM_API_BASE", "https://api.openai.com/v1")
    model_name = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
    if not api_key:
        return None
    try:
        import urllib.request
        req_data = json.dumps({
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{api_base}/chat/completions",
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return None


def _build_prompt(function_name: str, signature: str, body_text: str) -> str:
    """Build the LLM prompt for invariant extraction."""
    # Truncate very long bodies to keep prompts manageable
    body_excerpt = body_text[:4000]
    if len(body_text) > 4000:
        body_excerpt += "\n...[truncated]"
    return f"""Extract invariants from the following function. Return JSON only.

Function: {function_name}
Signature: {signature}

Body:
```c
{body_excerpt}
```

Return a JSON object with an "invariants" array. Each invariant is a dict
with "kind" (one of "precondition", "postcondition", "loop_invariant"),
"condition" (a short string describing the invariant), "evidence" (the
source line that supports it), and "line" (line number if known).

Example:
{{"invariants": [
  {{"kind": "precondition", "condition": "ctx != NULL", "evidence": "if (!ctx) return -EINVAL;"}},
  {{"kind": "postcondition", "condition": "returns 0 on success", "evidence": "return 0;"}}
]}}"""


def _build_rule_invariant_index(rule_invariants: Dict) -> Dict[Tuple[str, str], Dict]:
    """Index rule-based invariants by (kind, normalized_condition) for fast lookup."""
    index = {}
    for kind in ("preconditions", "postconditions", "loop_invariants"):
        singular = kind.rstrip("s") if kind.endswith("s") else kind
        # singular: "precondition", "postcondition", "loop_invariant"
        for inv in rule_invariants.get(kind, []) or []:
            cond = _normalize_condition(inv.get("condition", ""))
            if cond:
                index[(singular, cond)] = inv
    return index


def extract_invariants_with_llm(node_data: Dict,
                                num_calls: int = 3,
                                consensus_threshold: Optional[int] = None,
                                llm_client: Any = None) -> Dict:
    """Extract invariants with LLM consensus and continuous confidence.

    Args:
        node_data: function node dict with body_text, signature, params, name
        num_calls: how many LLM calls to make (default 3)
        consensus_threshold: minimum agreement count (default ceil(num_calls/2))
        llm_client: callable taking (prompt) -> response_text; defaults to
                    _call_llm which uses OPENAI_API_KEY env var

    Returns:
        {
            "preconditions": [...],
            "postconditions": [...],
            "loop_invariants": [...],
            "state_machine": {...} or None,
            "llm_consensus": {
                "num_calls": int,
                "agreement_ratios": {condition: float},
                "corroborated_with_rules": int,
            },
        }
    """
    from _builder.invariants import extract_invariants_for_node

    # Always run rule-based extraction first
    rule_inv = extract_invariants_for_node(node_data)
    rule_index = _build_rule_invariant_index(rule_inv)

    if consensus_threshold is None:
        consensus_threshold = (num_calls + 1) // 2  # ceil(num_calls / 2)

    body = node_data.get("body_text", "") or ""
    sig = node_data.get("signature", "") or ""
    name = node_data.get("name", "") or ""

    if not body:
        # No body — return rule-based only with default confidence
        return _with_confidence_scores(rule_inv, base_score=0.5,
                                       llm_consensus=None)

    client = llm_client or _call_llm
    prompt = _build_prompt(name, sig, body)

    # Make N LLM calls and collect parsed invariants
    all_responses: List[List[Dict]] = []
    for _ in range(num_calls):
        resp = client(prompt)
        if resp:
            parsed = _parse_llm_invariants(resp)
            if parsed:
                all_responses.append(parsed)

    if not all_responses:
        # LLM unavailable or all calls failed — return rule-based only
        return _with_confidence_scores(rule_inv, base_score=0.5,
                                       llm_consensus={
                                           "num_calls": 0,
                                           "agreement_ratios": {},
                                           "corroborated_with_rules": 0,
                                           "error": "llm unavailable",
                                       })

    # Group LLM invariants by (kind, normalized_condition) across responses
    # Count how many responses contain each group
    group_response_counts: Dict[Tuple[str, str], int] = {}
    group_example: Dict[Tuple[str, str], Dict] = {}
    for response in all_responses:
        seen_in_this_response = set()
        for inv in response:
            key = (inv.get("kind", ""),
                   _normalize_condition(inv.get("condition", "")))
            if key not in seen_in_this_response:
                seen_in_this_response.add(key)
                group_response_counts[key] = group_response_counts.get(key, 0) + 1
                if key not in group_example:
                    group_example[key] = inv

    # Accept invariants that meet the consensus threshold
    accepted = []
    agreement_ratios = {}
    corroborated_count = 0
    for key, count in group_response_counts.items():
        ratio = count / num_calls
        agreement_ratios[key[1] or key[0]] = ratio
        if count >= consensus_threshold:
            inv = dict(group_example[key])
            inv["confidence"] = "INFERRED" if ratio < 1.0 else "EXTRACTED"
            # Continuous confidence_score: agreement ratio * corroboration bonus
            bonus = 1.0
            if key in rule_index:
                bonus = 1.2  # rule-based invariant matches
                corroborated_count += 1
            inv["confidence_score"] = round(min(ratio * bonus, 1.0), 3)
            inv["source"] = "llm_consensus"
            inv["agreement_count"] = count
            inv["num_calls"] = num_calls
            accepted.append(inv)

    # Merge accepted LLM invariants with rule-based invariants
    merged = _merge_invariants(rule_inv, accepted)

    return {
        **merged,
        "llm_consensus": {
            "num_calls": num_calls,
            "successful_calls": len(all_responses),
            "agreement_ratios": agreement_ratios,
            "corroborated_with_rules": corroborated_count,
            "accepted_count": len(accepted),
        },
    }


def _with_confidence_scores(rule_inv: Dict, base_score: float,
                            llm_consensus: Optional[Dict]) -> Dict:
    """Add a confidence_score to each rule-based invariant."""
    out = dict(rule_inv)
    for kind in ("preconditions", "postconditions", "loop_invariants"):
        out[kind] = [
            {**inv, "confidence_score": base_score,
             "source": inv.get("source", "rule")}
            for inv in (rule_inv.get(kind) or [])
        ]
    if llm_consensus is not None:
        out["llm_consensus"] = llm_consensus
    return out


def _merge_invariants(rule_inv: Dict, llm_accepted: List[Dict]) -> Dict:
    """Merge rule-based invariants with accepted LLM invariants.

    Rule-based invariants keep their original confidence.
    LLM invariants are added; duplicates (same kind+condition) are skipped.
    """
    merged = {
        "preconditions": list(rule_inv.get("preconditions") or []),
        "postconditions": list(rule_inv.get("postconditions") or []),
        "loop_invariants": list(rule_inv.get("loop_invariants") or []),
        "state_machine": rule_inv.get("state_machine"),
    }
    # Index existing for dedup
    existing = set()
    for kind_plural, kind_singular in [
        ("preconditions", "precondition"),
        ("postconditions", "postcondition"),
        ("loop_invariants", "loop_invariant"),
    ]:
        for inv in merged[kind_plural]:
            existing.add((kind_singular, _normalize_condition(inv.get("condition", ""))))

    # Add LLM-accepted invariants that aren't duplicates
    for inv in llm_accepted:
        kind = inv.get("kind", "")
        cond_norm = _normalize_condition(inv.get("condition", ""))
        if (kind, cond_norm) in existing:
            continue
        existing.add((kind, cond_norm))
        if kind == "precondition":
            merged["preconditions"].append(inv)
        elif kind == "postcondition":
            merged["postconditions"].append(inv)
        elif kind == "loop_invariant":
            merged["loop_invariants"].append(inv)

    return merged


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def cmd_extract_invariants_llm(args):
    """Extract invariants with LLM consensus and continuous confidence."""
    from _builder.graph_build import _load_full_graph
    from _builder.utils import _find_node_id

    graph_dir = args.graph
    node_hint = args.node
    num_calls = getattr(args, "num_calls", 3)

    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        print(f"Error: node matching {node_hint!r} not found", file=sys.stderr)
        sys.exit(1)

    ndata = G.nodes[node_id]
    result = extract_invariants_with_llm(ndata, num_calls=num_calls)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
