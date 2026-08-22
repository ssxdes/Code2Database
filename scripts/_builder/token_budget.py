"""callgraph builder module: token budget control.

Provides token counting, truncation, budget-aware output formatting,
and pipeline phase tracking.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Dict


# Rough estimate: 1 token ≈ 4 chars for English, ~2 chars for CJK
_CHARS_PER_TOKEN = 4
_CJK_RANGES = re.compile(r'[一-鿿㐀-䶿豈-﫿]')


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string.

    Uses ~4 chars/token for Latin text, ~2 chars/token for CJK.
    """
    if not text:
        return 0
    cjk_chars = len(_CJK_RANGES.findall(text))
    latin_chars = len(text) - cjk_chars
    return (latin_chars // _CHARS_PER_TOKEN) + (cjk_chars // 2) + 1


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within max_tokens, adding ellipsis if truncated."""
    if not text:
        return text
    est = estimate_tokens(text)
    if est <= max_tokens:
        return text
    # Approximate cut position
    target_chars = max_tokens * _CHARS_PER_TOKEN
    if target_chars >= len(text):
        return text
    # Find a good break point (newline or space)
    cut = text.rfind('\n', 0, target_chars)
    if cut < target_chars * 0.7:
        cut = text.rfind(' ', 0, target_chars)
    if cut < target_chars * 0.5:
        cut = target_chars
    return text[:cut] + f"\n... [truncated at ~{max_tokens} tokens]"


def budget_pack(data: dict, max_tokens: int = 0) -> dict:
    """Trim a pack dict to fit within max_tokens.

    Strategy: progressively drop lowest-priority fields until budget met.
    Priority order (highest first): stats, key_paths, top_apis, domains,
    hub_functions, hotspots, communities, scenarios, entry_scores, processes.
    """
    if max_tokens <= 0:
        data["_token_count"] = estimate_tokens(json.dumps(data, ensure_ascii=False))
        return data

    # Field priority tiers (drop from bottom up)
    tier3 = ["processes", "entry_scores", "communities"]  # drop first
    tier2 = ["scenarios", "hotspots", "hub_functions"]     # drop second
    tier1 = ["domains", "top_apis", "key_paths"]           # drop third
    tier0 = ["stats"]                                       # never drop

    result = dict(data)
    for tier in [tier3, tier2, tier1]:
        current_tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
        if current_tokens <= max_tokens:
            break
        for field in tier:
            if field in result:
                del result[field]
                current_tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
                if current_tokens <= max_tokens:
                    break

    result["_token_count"] = estimate_tokens(json.dumps(result, ensure_ascii=False))
    return result


def budget_describe(data: dict, max_tokens: int = 0) -> dict:
    """Trim a describe-node result dict to fit within max_tokens.

    Strategy: drop body_text first, then truncate large lists,
    then drop lower-priority fields.
    """
    if max_tokens <= 0:
        data["_token_count"] = estimate_tokens(json.dumps(data, ensure_ascii=False))
        return data

    drop_order = ["body_text", "local_vars", "callee_args", "param_flow",
                  "globals_context", "related_chains", "branches",
                  "condition_vars", "concurrency_info", "semantic_desc",
                  "external_desc", "api_constraints", "params", "labels_source"]

    result = dict(data)

    # Step 1: Check if data already fits budget
    current_tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
    if current_tokens <= max_tokens:
        result["_token_count"] = current_tokens
        return result

    # Step 2: Drop body_text (usually the largest field)
    if "body_text" in result:
        del result["body_text"]
        # Re-estimate after body_text deletion
        current_tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
        if current_tokens <= max_tokens:
            result["_token_count"] = current_tokens
            return result

    # Step 3: Truncate large lists
    for list_field in ("callers", "callees"):
        if list_field in result and isinstance(result[list_field], list):
            current_tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
            if current_tokens > max_tokens:
                # Try cutting the list in half
                lst = result[list_field]
                while len(lst) > 3 and estimate_tokens(json.dumps(result, ensure_ascii=False)) > max_tokens:
                    lst = lst[:len(lst)//2]
                    result[list_field] = lst
                    # Add truncation marker
                if len(data.get(list_field, [])) > len(lst):
                    result[f"_{list_field}_truncated"] = f"showing {len(lst)} of {len(data[list_field])}"

    # Step 4: Drop fields by priority
    for field in drop_order:
        current_tokens = estimate_tokens(json.dumps(result, ensure_ascii=False))
        if current_tokens <= max_tokens:
            break
        if field in result:
            del result[field]

    result["_token_count"] = estimate_tokens(json.dumps(result, ensure_ascii=False))
    return result


# ---------------------------------------------------------------------------
# Complexity evaluator + dynamic LLM context budget allocation (D36)
# ---------------------------------------------------------------------------

def evaluate_query_complexity(query: str, related_nodes_count: int = 0,
                              max_depth: int = 0) -> Dict:
    """Evaluate the complexity of an LLM query to inform budget allocation.

    Returns a dict with:
      - score: 0.0 to 1.0 (low to high complexity)
      - factors: dict of individual factor scores
      - recommended_budget: suggested token budget tier

    Factors considered:
      - query length (longer = more complex)
      - related_nodes_count (more nodes to consider)
      - max_depth (deeper traversal)
      - query type (Cypher / natural language / why-question)
      - has_negation (NOT, "why not", "why doesn't")
      - has_aggregation (count, sum, avg)
    """
    factors = {}
    query_len = len(query or "")

    # Factor 1: query length (capped at 200 chars -> 1.0)
    factors["query_length"] = min(query_len / 200.0, 1.0)

    # Factor 2: related nodes count (capped at 50 -> 1.0)
    factors["nodes_count"] = min((related_nodes_count or 0) / 50.0, 1.0)

    # Factor 3: max depth (capped at 10 -> 1.0)
    factors["depth"] = min((max_depth or 0) / 10.0, 1.0)

    # Factor 4: query type
    q_lower = (query or "").lower()
    if any(kw in q_lower for kw in ("match", "where", "return", "create", "delete")):
        # Cypher query
        factors["query_type"] = 0.7
    elif q_lower.startswith(("why", "how", "what", "when", "where", "who")):
        # Natural-language question — usually harder than Cypher
        factors["query_type"] = 0.8
    else:
        # Keyword search
        factors["query_type"] = 0.3

    # Factor 5: negation / ambiguity
    if any(kw in q_lower for kw in (" not ", " why not", "why doesn't", "isn't",
                                     "doesn't", "shouldn't")):
        factors["negation"] = 0.8
    else:
        factors["negation"] = 0.0

    # Factor 6: aggregation
    if any(kw in q_lower for kw in ("count", "sum", "average", "avg", "total")):
        factors["aggregation"] = 0.6
    else:
        factors["aggregation"] = 0.0

    # Weighted combination
    weights = {
        "query_length": 0.15,
        "nodes_count": 0.25,
        "depth": 0.20,
        "query_type": 0.20,
        "negation": 0.10,
        "aggregation": 0.10,
    }
    score = sum(factors[k] * weights[k] for k in weights)

    # Recommended budget tier
    if score < 0.3:
        recommended_budget = "micro"  # ~500 tokens
    elif score < 0.5:
        recommended_budget = "lite"   # ~2000 tokens
    elif score < 0.7:
        recommended_budget = "standard"  # ~5000 tokens
    else:
        recommended_budget = "deep"   # ~10000 tokens

    return {
        "score": round(score, 3),
        "factors": {k: round(v, 3) for k, v in factors.items()},
        "recommended_budget": recommended_budget,
    }


# Token budget tiers
BUDGET_TIERS = {
    "micro": 500,      # context_pack_micro
    "lite": 2000,      # context_pack_lite
    "standard": 5000,  # explore-flow default
    "deep": 10000,     # deep traversal
    "unlimited": 0,    # no limit
}


def allocate_budget(complexity: Dict, base_budget: int = 2000,
                    max_budget: int = 10000) -> Dict:
    """Allocate an LLM context budget based on query complexity.

    Args:
        complexity: dict from evaluate_query_complexity
        base_budget: minimum budget to allocate (default 2000)
        max_budget: upper cap (default 10000)

    Returns:
        {
            "tokens": allocated token budget,
            "tier": tier name,
            "pack_layers": list of pack layers to include,
            "rationale": explanation string,
        }
    """
    score = complexity.get("score", 0.0)
    rec = complexity.get("recommended_budget", "lite")

    # Allocate tokens proportional to complexity score, clamped to [base, max]
    target = int(base_budget + (max_budget - base_budget) * score)
    target = max(base_budget, min(target, max_budget))

    # Choose pack layers based on tier
    if rec == "micro":
        pack_layers = ["micro"]
    elif rec == "lite":
        pack_layers = ["micro", "lite"]
    elif rec == "standard":
        pack_layers = ["micro", "lite", "describe_node"]
    else:  # deep
        pack_layers = ["micro", "lite", "describe_node", "explore_flow"]

    rationale = (f"complexity_score={score:.2f}, "
                 f"tier={rec}, "
                 f"factors={complexity.get('factors', {})}")

    return {
        "tokens": target,
        "tier": rec,
        "pack_layers": pack_layers,
        "rationale": rationale,
    }


def adaptive_upgrade(current_response_tokens: int, max_tokens: int,
                     truncation_marker: str = "") -> Dict:
    """Decide whether to upgrade to a deeper pack layer based on truncation.

    Use when a query response was truncated (contains truncation marker or
    exceeds max_tokens) — recommend an upgrade to the next pack layer.

    Returns:
        {
            "should_upgrade": bool,
            "reason": str,
            "next_tier": suggested next tier,
        }
    """
    should_upgrade = False
    reason = ""
    next_tier = ""

    if truncation_marker and "truncated" in truncation_marker:
        should_upgrade = True
        reason = f"response contains truncation marker: {truncation_marker[:60]}"
        next_tier = "deeper_pack_layer"
    elif current_response_tokens > max_tokens * 0.9:
        should_upgrade = True
        reason = (f"response used {current_response_tokens} tokens "
                  f"(>90% of {max_tokens} budget)")
        next_tier = "deeper_pack_layer"

    return {
        "should_upgrade": should_upgrade,
        "reason": reason,
        "next_tier": next_tier,
        "current_tokens": current_response_tokens,
        "max_tokens": max_tokens,
    }


class PipelineTracker:
    """Track pipeline phase execution times and token consumption.

    Records per-stage wall clock time and estimated output tokens
    via estimate_tokens(). Writes .code2database_pipeline_stats.json.
    """

    def __init__(self):
        self._stages = []
        self._current = None
        self._start = 0.0

    def begin(self, name: str, metadata: dict = None):
        """Start timing a stage."""
        self._current = {
            "name": name,
            "metadata": metadata or {},
        }
        self._start = time.time()

    def end(self, output_tokens: int = 0, extra: dict = None):
        """End current stage, record elapsed time and tokens."""
        elapsed = time.time() - self._start
        stage = {
            "name": self._current["name"],
            "elapsed_sec": round(elapsed, 3),
            "output_tokens": output_tokens,
            "metadata": self._current["metadata"],
        }
        if extra:
            stage.update(extra)
        self._stages.append(stage)
        self._current = None

    def end_with_files(self, file_paths: list, extra: dict = None):
        """End stage, compute output_tokens from actual files on disk.

        For large files (>100MB), uses file size for token estimation
        to avoid loading the entire file into memory.
        """
        total_bytes = 0
        total_tokens = 0
        existing = 0
        _LARGE_FILE_THRESHOLD = 100 * 1024 * 1024  # 100MB
        for fp in file_paths:
            if os.path.exists(fp):
                existing += 1
                file_size = os.path.getsize(fp)
                total_bytes += file_size
                if file_size > _LARGE_FILE_THRESHOLD:
                    # Estimate tokens from file size (~4 chars/token for ASCII)
                    # Avoids loading huge files into memory
                    total_tokens += file_size // 4
                else:
                    content = Path(fp).read_text(encoding="utf-8", errors="replace")
                    total_tokens += estimate_tokens(content)
        self.end(output_tokens=total_tokens, extra={
            "output_bytes": total_bytes,
            "output_files": existing,
            **(extra or {}),
        })

    def to_dict(self) -> dict:
        """Return the full report as a dict."""
        total_tokens = sum(s["output_tokens"] for s in self._stages)
        total_bytes = sum(s.get("output_bytes", 0) for s in self._stages)
        total_elapsed = sum(s["elapsed_sec"] for s in self._stages)
        return {
            "stages": self._stages,
            "summary": {
                "total_stages": len(self._stages),
                "total_elapsed_sec": round(total_elapsed, 3),
                "total_output_tokens": total_tokens,
                "total_output_bytes": total_bytes,
            },
            "comparison": self._compute_comparison(),
        }

    def write_report(self, outdir: str) -> dict:
        """Write .code2database_pipeline_stats.json to outdir. Returns the report dict."""
        report = self.to_dict()
        path = os.path.join(outdir, ".code2database_pipeline_stats.json")
        Path(path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return report

    def format_stage_summary(self) -> str:
        """O28: Return a human-readable per-stage timing summary.

        Format: one line per stage with name, elapsed seconds, percentage
        of total, and output tokens. Stages are sorted by elapsed time
        descending so the slowest stages are at the top.
        """
        if not self._stages:
            return "(no stages recorded)"
        total = sum(s["elapsed_sec"] for s in self._stages)
        lines = ["Per-stage timing:"]
        # Sort by elapsed_sec descending
        sorted_stages = sorted(self._stages, key=lambda s: -s["elapsed_sec"])
        for s in sorted_stages:
            elapsed = s["elapsed_sec"]
            pct = (elapsed / total * 100) if total > 0 else 0
            tokens = s.get("output_tokens", 0)
            tok_str = f", {tokens} tokens" if tokens > 0 else ""
            lines.append(f"  {s['name']:<28} {elapsed:>7.3f}s ({pct:5.1f}%){tok_str}")
        lines.append(f"  {'TOTAL':<28} {total:>7.3f}s")
        return "\n".join(lines)

    def _compute_comparison(self) -> dict:
        """Compare extracted output size vs raw source size.

        Reports byte counts so users can see how much data was extracted
        compared to the original source files.
        """
        source_bytes = 0
        source_files = 0
        for stage in self._stages:
            meta = stage.get("metadata", {})
            source_bytes += meta.get("source_bytes", 0)
            source_files += meta.get("source_files", 0)

        graph_bytes = sum(s.get("output_bytes", 0) for s in self._stages)

        return {
            "source_files": source_files,
            "raw_source_bytes_estimate": source_bytes,
            "output_bytes": graph_bytes,
        }

        return {
            "raw_source_tokens_estimate": source_tokens,
            "raw_source_files_estimate": source_files,
            "raw_source_bytes_estimate": source_bytes,
            "graph_output_tokens": graph_tokens,
            "savings_ratio": savings_ratio,
            "note": ("raw_source_tokens_estimate assumes ~4 chars/token for source code; "
                     "actual LLM tokenization varies by model. savings_ratio = "
                     "1 - (graph_output_tokens / raw_source_tokens_estimate)"),
        }
