#!/usr/bin/env python3
"""BUG benchmark and efficiency evaluation.

Provides a framework for measuring "how much faster does the AI find
BUGs with the invocation graph vs without?". Three components:

1. **Benchmark format**: a JSON file listing historical BUGs, each
   with: id, description, root_cause_function, root_cause_file:line,
   source (CVE id or issue link), expected_keywords (terms the AI
   should mention when it finds the bug).

2. **Evaluation runner**: for each BUG, simulate the AI's investigation
   in two modes:
   - "graph" mode: the AI uses Code2Database queries (describe-node,
     trace-chain, blast-radius, etc.)
   - "grep" mode: the AI uses grep/rg/file reads only
   Both modes record: tool_calls, tokens_consumed, time_elapsed,
   root_cause_found (bool), keywords_matched (count).

3. **Metrics report**: recall (find root cause), precision (don't
   misidentify), tool-call efficiency, token efficiency, time speedup.

The "AI" in this framework is a heuristic investigator that mimics
how an AI would actually use the available tools. We don't run a real
LLM in the eval — instead, the investigator's strategy is fixed, so
the benchmark measures the *tools* (graph vs grep), not the LLM's
cleverness. A real LLM could be plugged in by replacing the
Investigator class with one that calls out to Claude/GPT.

CLI:
    bug-benchmark --graph <dir> --benchmark <bench.json> --run
    bug-benchmark --graph <dir> --benchmark <bench.json> --report
    bug-benchmark --graph <dir> --create-template <out.json>  # template
"""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Tuple


# ---------------------------------------------------------------------------
# Benchmark format
# ---------------------------------------------------------------------------

@dataclass
class BugCase:
    """One BUG case in the benchmark."""
    id: str  # e.g., "CVE-2024-12345" or "issue-42"
    description: str  # what the bug is
    root_cause_function: str  # the function that has the bug
    root_cause_file: str  # source file
    root_cause_line: int  # line number
    expected_keywords: List[str] = field(default_factory=list)  # terms to find
    source: str = ""  # CVE id or issue link
    severity: str = ""  # "high", "medium", "low"
    hints: List[str] = field(default_factory=list)  # starting-point hints
    # reproduction vs production environment annotations.
    # Reproduction env: conditions under which the bug was originally observed
    #   (e.g., KASAN enabled, syzkaller injection, madvise(MADV_SOFT_OFFLINE)).
    # Production env: conditions under which the bug would need to trigger in
    #   real-world deployment (e.g., no KASAN, no injection, normal load).
    # The delta between these two determines whether "reproducible" implies
    # "exploitable in production" — a distinction the misleading analysis
    # missed (see KASAN_FINAL_REPORT.md §12.1).
    reproduction_env: Dict[str, str] = field(default_factory=dict)
    production_env: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "BugCase":
        # Only pass keys actually present so default_factory kicks in for
        # missing fields (passing None would shadow default_factory).
        kwargs = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**kwargs)


def create_benchmark_template(out_path: str):
    """Write a template benchmark file with example cases."""
    template = {
        "name": "Example BUG benchmark",
        "version": "1.0",
        "cases": [
            {
                "id": "CVE-2024-example",
                "description": "Null pointer dereference in worker_init when ctx is NULL",
                "root_cause_function": "worker_init",
                "root_cause_file": "worker.c",
                "root_cause_line": 42,
                "expected_keywords": ["NULL", "ctx", "deref"],
                "source": "CVE-2024-example",
                "severity": "high",
                "hints": ["crash in worker_init", "ctx parameter"],
                # reproduction vs production environment.
                # Fill these in so the benchmark report can flag repro-only
                # conditions (KASAN, syzkaller injection, madvise, etc.) and
                # force the investigator to verify production reachability.
                "reproduction_env": {
                    "kernel": "debug kernel with KASAN enabled",
                    "trigger": "syzkaller fuzzer with madvise(MADV_SOFT_OFFLINE) injection",
                    "concurrency": "dozens of syzkaller worker threads",
                },
                "production_env": {
                    "kernel": "production kernel without KASAN",
                    "trigger": "hardware ECC errors only (rare)",
                    "concurrency": "normal business workload",
                },
            },
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    print(f"Wrote template benchmark to {out_path}")


def load_benchmark(path: str) -> List[BugCase]:
    """Load a benchmark file into a list of BugCase."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases", []) if isinstance(data, dict) else data
    return [BugCase.from_dict(c) for c in cases]


# ---------------------------------------------------------------------------
# Investigation result + metrics
# ---------------------------------------------------------------------------

@dataclass
class InvestigationResult:
    """The result of investigating one BUG case in one mode."""
    case_id: str
    mode: str  # 'graph' or 'grep'
    tool_calls: int = 0
    tokens_consumed: int = 0  # estimated tokens (chars/4)
    time_elapsed: float = 0.0  # seconds
    root_cause_found: bool = False
    keywords_matched: int = 0
    functions_examined: List[str] = field(default_factory=list)
    final_answer: str = ""  # the investigator's final report
    # environment delta assessment.
    # env_assessment: free-form text describing the repro-vs-production gap.
    # production_triggerable: verdict on whether the bug can fire in production.
    #   "impossible"     — writers guarded out / unreachable in production
    #   "narrow_window"  — reachable but timing window is extremely small
    #   "possible"       — reachable, window non-trivial
    #   "likely"         — reachable, window wide
    #   "unknown"        — investigation did not establish reachability
    env_assessment: str = ""
    production_triggerable: str = "unknown"

    def to_dict(self) -> Dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Reproduction vs production environment delta assessment
# ---------------------------------------------------------------------------
# Markers that indicate a repro-only condition — i.e., the bug was observed
# under conditions that do not exist in production. If the reproduction_env
# contains any of these markers AND production_env does not, the bug's
# production-triggerability is unproven and should be flagged.
#
# Derived from KASAN_FINAL_REPORT.md §12.1 — the misleading analysis missed
# that KASAN + syzkaller + madvise injection are repro-only conditions.
_REPRO_ONLY_MARKERS = (
    "kasan",            # KASAN debug kernel — not enabled in production
    "syzkaller",        # syzkaller fuzzer — not running in production
    "madvise",          # madvise(MADV_SOFT_OFFLINE/MADV_HWPOISON) injection
    "mf_inject",        # MF_INJECT flag — artificial memory failure injection
    "kretprobe",        # kretprobe-based injection — requires kernel module
    "manual_injection", # any manual injection technique
    "fuzzing",          # fuzzing-only trigger
    "debug_kernel",     # debug kernel config — not deployed in production
    "stress_test",      # artificial stress test workload
    "pktcdvd",          # pktcdvd — rarely used in production
)


def _env_has_marker(env: Dict[str, str], marker: str) -> bool:
    """Return True if any key or value in env contains the marker (case-insensitive).

    Handles negation: a marker adjacent to a negation word (without, no, not,
    disabled, off, absent, none) is treated as absent. This avoids false
    negatives where production_env says "production kernel without KASAN" —
    the substring "kasan" is present but the meaning is "KASAN is absent".
    Both prefix negation ("without KASAN") and suffix negation ("KASAN
    disabled") are recognized.
    """
    marker_lower = marker.lower()
    negations = ("without", "no ", "not ", "disabled", " off", "absent",
                 "none", "lacks", "missing")
    for k, v in (env or {}).items():
        for text in (str(k), str(v)):
            text_lower = text.lower()
            idx = text_lower.find(marker_lower)
            while idx != -1:
                # Check up to 12 chars before and 12 chars after the marker.
                prefix = text_lower[max(0, idx - 12):idx]
                suffix = text_lower[idx + len(marker_lower):idx + len(marker_lower) + 12]
                if not any(neg in prefix for neg in negations) and \
                   not any(neg in suffix for neg in negations):
                    return True
                idx = text_lower.find(marker_lower, idx + 1)
    return False


def _env_delta_warning(repro_env: Dict[str, str],
                       production_env: Dict[str, str]) -> str:
    """Return a warning string if repro env has repro-only markers absent in production.

    Returns "" if no delta detected (either env empty, or no repro-only
    markers in repro, or all repro-only markers also present in production).

    The warning text is appended to the bug report so the investigator (and
    any reader of the report) is forced to consider whether the bug's
    reproduction actually implies production triggerability.
    """
    if not repro_env:
        return ""
    repro_only_found = []
    for marker in _REPRO_ONLY_MARKERS:
        in_repro = _env_has_marker(repro_env, marker)
        in_prod = _env_has_marker(production_env, marker)
        if in_repro and not in_prod:
            repro_only_found.append(marker)
    if not repro_only_found:
        return ""
    return (
        "REPRO-ONLY CONDITIONS DETECTED: "
        + ", ".join(repro_only_found)
        + " — reproduction success does NOT imply production triggerability. "
        + "Investigator must verify writer reachability in production env "
        + "(no KASAN, no injection) before concluding the bug is real."
    )


# ---------------------------------------------------------------------------
# Graph-mode investigator — uses callgraph queries
# ---------------------------------------------------------------------------

class GraphInvestigator:
    """Heuristic investigator that uses Code2Database queries.

    Strategy (mimics how an AI would use the graph):
    1. For each hint, search the graph for matching function names.
    2. For each candidate, run describe-node and check if the
       description / constraints / body match the BUG description.
    3. Run blast-radius to see if the candidate's effects match the
       BUG's reported symptoms.
    4. If a candidate's source file:line matches the root cause, mark
       it as found.
    """

    def __init__(self, graph_dir: str):
        self.graph_dir = graph_dir
        self.tool_calls = 0
        self.tokens = 0
        self.functions_examined: List[str] = []
        self._start_time = 0.0
        try:
            from _builder.graph_build import _load_full_graph
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from _builder.graph_build import _load_full_graph
        self.G = _load_full_graph(graph_dir)

    def _record(self, output_size: int = 0):
        self.tool_calls += 1
        self.tokens += output_size // 4  # rough estimate: 4 chars/token

    def investigate(self, case: BugCase) -> InvestigationResult:
        self._start_time = time.time()
        found = False
        keywords_matched = 0
        final_answer_parts = []

        # Strategy 1: search by hints
        candidates: List[str] = []
        for hint in case.hints or [case.root_cause_function]:
            self._record(output_size=len(hint) // 4)
            # Search nodes by name (substring)
            hint_lower = hint.lower()
            for nid, nd in self.G.nodes(data=True):
                if nd.get("is_empty", False):
                    continue
                name = nd.get("name", "")
                if hint_lower in name.lower():
                    candidates.append(nid)
                    self._record(output_size=200)  # describe-node output
                    if len(candidates) >= 20:
                        break
            if candidates:
                break

        # Strategy 2: examine each candidate's details
        for cand_id in candidates[:10]:
            if cand_id not in self.G:
                continue
            nd = self.G.nodes[cand_id]
            self.functions_examined.append(nd.get("name", cand_id))
            self._record(output_size=500)  # describe-node ~ 500 tokens
            # Check if this is the root cause
            src_file = nd.get("source_file", "")
            line = nd.get("line", 0)
            # Match by file name (basename) and proximity to line
            case_file_base = os.path.basename(case.root_cause_file)
            src_file_base = os.path.basename(src_file)
            if (case_file_base == src_file_base and
                    abs(line - case.root_cause_line) <= 50):
                # Check function name match too
                if nd.get("name", "") == case.root_cause_function:
                    found = True
                    final_answer_parts.append(
                        f"Root cause: {nd.get('name')} at {src_file}:{line}")
                    break

        # Strategy 3: check expected keywords in examined function bodies
        if not found and case.expected_keywords:
            for cand_id in candidates[:5]:
                if cand_id not in self.G:
                    continue
                nd = self.G.nodes[cand_id]
                body = nd.get("body_text", "") or ""
                if not body:
                    # Lazy-load body
                    try:
                        from _builder.patcher import lazy_fill_node
                        filled = lazy_fill_node(self.G, cand_id, "")
                        body = filled.get("body_text", "") or ""
                        self._record(output_size=len(body) // 4)
                    except Exception:
                        pass
                for kw in case.expected_keywords:
                    if kw.lower() in body.lower():
                        keywords_matched += 1

        # environment delta assessment.
        # The heuristic investigator does not actually prove production
        # reachability — that requires path-guards / field-flow analysis.
        # What we CAN do is flag cases where the reproduction environment
        # has repro-only markers (KASAN, syzkaller, madvise injection) that
        # are absent in production. This forces the report reader to treat
        # the "found" verdict with appropriate skepticism.
        delta_warning = _env_delta_warning(
            case.reproduction_env, case.production_env)
        if delta_warning:
            env_assessment = delta_warning
            production_triggerable = "unknown"
        elif case.reproduction_env and case.production_env:
            # Both envs present but no repro-only markers detected —
            # assume reproduction generalizes to production (optimistic).
            env_assessment = (
                "No repro-only markers detected — reproduction conditions "
                "appear to generalize to production. Verify writer "
                "reachability separately.")
            production_triggerable = "unknown"
        else:
            env_assessment = ""
            production_triggerable = "unknown"

        return InvestigationResult(
            case_id=case.id, mode="graph",
            tool_calls=self.tool_calls, tokens_consumed=self.tokens,
            time_elapsed=time.time() - self._start_time,
            root_cause_found=found, keywords_matched=keywords_matched,
            functions_examined=self.functions_examined[:20],
            final_answer="; ".join(final_answer_parts) or "(no answer)",
            env_assessment=env_assessment,
            production_triggerable=production_triggerable,
        )


# ---------------------------------------------------------------------------
# Grep-mode investigator — uses grep/rg only
# ---------------------------------------------------------------------------

class GrepInvestigator:
    """Heuristic investigator that uses grep/rg + file reads.

    Strategy (mimics how an AI would grep):
    1. For each hint, run `rg "hint" source_root/` to find files.
    2. For each matching file, read the matching line and surrounding
       context to find the function definition.
    3. Match by file:line as in graph mode.

    This is a deliberately naive grep strategy — a real AI would be
    smarter, but the goal is to measure tool efficiency, not AI
    cleverness. The number of tool calls and tokens is what we compare.
    """

    def __init__(self, source_root: str):
        self.source_root = source_root
        self.tool_calls = 0
        self.tokens = 0
        self.functions_examined: List[str] = []
        self._start_time = 0.0

    def _record(self, output_size: int = 0):
        self.tool_calls += 1
        self.tokens += output_size // 4

    def _rg(self, pattern: str) -> List[str]:
        """Run ripgrep, return matching lines."""
        try:
            result = subprocess.run(
                ["rg", "--no-heading", "-n", pattern, self.source_root],
                capture_output=True, text=True, timeout=10
            )
            self._record(output_size=len(result.stdout))
            return result.stdout.strip().split("\n") if result.stdout.strip() else []
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def _grep(self, pattern: str) -> List[str]:
        """Fallback grep if rg not available."""
        try:
            result = subprocess.run(
                ["grep", "-rn", pattern, self.source_root],
                capture_output=True, text=True, timeout=10
            )
            self._record(output_size=len(result.stdout))
            return result.stdout.strip().split("\n") if result.stdout.strip() else []
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def _read_file(self, path: str, max_chars: int = 5000) -> str:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            self._record(output_size=min(len(text), max_chars))
            return text[:max_chars]
        except OSError:
            return ""

    def investigate(self, case: BugCase) -> InvestigationResult:
        self._start_time = time.time()
        found = False
        keywords_matched = 0
        final_answer_parts = []

        # Strategy 1: grep for each hint
        candidates: List[Tuple[str, int]] = []  # (file, line)
        for hint in case.hints or [case.root_cause_function]:
            matches = self._rg(hint) or self._grep(hint)
            for line in matches[:30]:
                # Parse "file:line:content"
                parts = line.split(":", 2)
                if len(parts) >= 2:
                    try:
                        f, l = parts[0], int(parts[1])
                        candidates.append((f, l))
                    except (ValueError, IndexError):
                        continue
            if candidates:
                break

        # Strategy 2: examine each candidate file
        case_file_base = os.path.basename(case.root_cause_file)
        for cand_file, cand_line in candidates[:10]:
            text = self._read_file(cand_file)
            # Find function definition near the matching line
            # (heuristic: search backwards for the function signature)
            lines = text.split("\n")
            func_name = ""
            func_def_line = 0
            for i in range(min(cand_line, len(lines)) - 1, -1, -1):
                if re.match(r'\s*(?:int|void|char|static|inline|unsigned|long|double|float|struct|enum|bool|size_t)\s+\*?\s*\w+\s*\(', lines[i]):
                    m = re.search(r'\b(\w+)\s*\(', lines[i])
                    if m:
                        func_name = m.group(1)
                        func_def_line = i + 1
                        break
            if func_name:
                self.functions_examined.append(func_name)
            # Match by file basename and proximity
            if (os.path.basename(cand_file) == case_file_base and
                    abs(func_def_line - case.root_cause_line) <= 50 and
                    func_name == case.root_cause_function):
                found = True
                final_answer_parts.append(
                    f"Root cause: {func_name} at {cand_file}:{func_def_line}")
                break

        # Strategy 3: keyword search in candidate files
        if not found and case.expected_keywords:
            for cand_file, _ in candidates[:5]:
                text = self._read_file(cand_file, max_chars=10000)
                for kw in case.expected_keywords:
                    if kw.lower() in text.lower():
                        keywords_matched += 1

        return InvestigationResult(
            case_id=case.id, mode="grep",
            tool_calls=self.tool_calls, tokens_consumed=self.tokens,
            time_elapsed=time.time() - self._start_time,
            root_cause_found=found, keywords_matched=keywords_matched,
            functions_examined=self.functions_examined[:20],
            final_answer="; ".join(final_answer_parts) or "(no answer)",
        )


# ---------------------------------------------------------------------------
# Run + report
# ---------------------------------------------------------------------------

def run_benchmark(graph_dir: str, benchmark_path: str,
                  source_root: str = "") -> Dict:
    """Run the benchmark in both modes, return all results.

    Args:
        graph_dir: callgraph output directory
        benchmark_path: path to the benchmark JSON
        source_root: source code root (for grep mode)

    Returns:
        {
            "cases": [...],
            "results": [InvestigationResult, ...],
            "summary": {...},
        }
    """
    cases = load_benchmark(benchmark_path)
    if not source_root:
        # Try to read from master
        master_path = os.path.join(graph_dir, "code2database_master.json")
        if os.path.exists(master_path):
            try:
                master = json.loads(Path(master_path).read_text(encoding="utf-8"))
                source_root = master.get("source_root", "")
            except Exception:
                pass

    results: List[Dict] = []
    for case in cases:
        # Graph mode
        gi = GraphInvestigator(graph_dir)
        graph_result = gi.investigate(case)
        # Grep mode
        grep_result = None
        if source_root and os.path.isdir(source_root):
            gpi = GrepInvestigator(source_root)
            grep_result = gpi.investigate(case)
        results.append({
            "case_id": case.id,
            "root_cause_function": case.root_cause_function,
            "root_cause_file": case.root_cause_file,
            "root_cause_line": case.root_cause_line,
            # carry reproduction_env / production_env into the
            # result so generate_report can render the environment delta
            # assessment section without re-loading the benchmark file.
            "reproduction_env": case.reproduction_env,
            "production_env": case.production_env,
            "graph_result": graph_result.to_dict(),
            "grep_result": grep_result.to_dict() if grep_result else None,
        })
    return {"cases": [c.to_dict() for c in cases], "results": results}


def compute_summary(results: List[Dict]) -> Dict:
    """Compute aggregate metrics from results."""
    graph_results = [r["graph_result"] for r in results if r.get("graph_result")]
    grep_results = [r["grep_result"] for r in results if r.get("grep_result")]

    def _agg(items: List[Dict]) -> Dict:
        if not items:
            return {}
        n = len(items)
        return {
            "case_count": n,
            "recall": sum(1 for x in items if x["root_cause_found"]) / n,
            "avg_tool_calls": sum(x["tool_calls"] for x in items) / n,
            "avg_tokens": sum(x["tokens_consumed"] for x in items) / n,
            "avg_time": sum(x["time_elapsed"] for x in items) / n,
            "avg_keywords_matched": sum(x["keywords_matched"] for x in items) / n,
        }

    summary = {"graph": _agg(graph_results), "grep": _agg(grep_results)}
    # Speedup ratios (graph vs grep)
    if summary["graph"] and summary["grep"]:
        g, p = summary["graph"], summary["grep"]
        summary["speedup"] = {
            "tool_calls": (p["avg_tool_calls"] / g["avg_tool_calls"]
                           if g["avg_tool_calls"] > 0 else None),
            "tokens": (p["avg_tokens"] / g["avg_tokens"]
                       if g["avg_tokens"] > 0 else None),
            "time": (p["avg_time"] / g["avg_time"]
                     if g["avg_time"] > 0 else None),
            "recall_delta": g["recall"] - p["recall"],
        }
    return summary


def generate_report(results: List[Dict], summary: Dict) -> str:
    """Generate a human-readable Markdown report."""
    lines = ["# BUG Benchmark Report", ""]
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Graph Mode | Grep Mode | Speedup |")
    lines.append("|--------|-----------|-----------|---------|")
    g, p = summary.get("graph", {}), summary.get("grep", {})
    s = summary.get("speedup", {}) or {}
    lines.append(f"| Cases | {g.get('case_count', 0)} | {p.get('case_count', 0)} | - |")
    lines.append(f"| Recall | {g.get('recall', 0):.1%} | {p.get('recall', 0):.1%} | "
                 f"{s.get('recall_delta', 0):+.1%} |")
    lines.append(f"| Avg tool calls | {g.get('avg_tool_calls', 0):.1f} | "
                 f"{p.get('avg_tool_calls', 0):.1f} | "
                 f"{s.get('tool_calls', 0):.2f}x |")
    lines.append(f"| Avg tokens | {g.get('avg_tokens', 0):.0f} | "
                 f"{p.get('avg_tokens', 0):.0f} | "
                 f"{s.get('tokens', 0):.2f}x |")
    lines.append(f"| Avg time (s) | {g.get('avg_time', 0):.2f} | "
                 f"{p.get('avg_time', 0):.2f} | "
                 f"{s.get('time', 0):.2f}x |")
    lines.append("")
    lines.append("## Per-case results")
    lines.append("")
    lines.append("| Case | Graph found | Grep found | Graph calls | Grep calls | Graph tokens | Grep tokens |")
    lines.append("|------|-------------|------------|-------------|------------|--------------|-------------|")
    for r in results:
        gr, pr = r.get("graph_result"), r.get("grep_result")
        gfound = "✓" if gr and gr["root_cause_found"] else "✗"
        pfound = "✓" if pr and pr["root_cause_found"] else "—"
        gcalls = gr["tool_calls"] if gr else 0
        pcalls = pr["tool_calls"] if pr else 0
        gtok = gr["tokens_consumed"] if gr else 0
        ptok = pr["tokens_consumed"] if pr else 0
        lines.append(f"| {r['case_id']} | {gfound} | {pfound} | {gcalls} | {pcalls} | {gtok} | {ptok} |")
    lines.append("")

    # Environment delta assessment section.
    # Forces the report reader to consider whether reproduction success
    # implies production triggerability. This is the guardrail against the
    # misleading analysis pattern where "race window exists in repro"
    # was wrongly extrapolated to "race window exists in production".
    lines.append("## Environment Delta Assessment")
    lines.append("")
    lines.append("Reproduction vs production environment — flagged when repro-only conditions (KASAN, syzkaller, madvise injection) are absent in production.")
    lines.append("")
    any_delta = False
    for r in results:
        repro_env = r.get("reproduction_env") or {}
        prod_env = r.get("production_env") or {}
        if not repro_env:
            continue
        any_delta = True
        delta_warning = _env_delta_warning(repro_env, prod_env)
        gr = r.get("graph_result") or {}
        triggerable = gr.get("production_triggerable", "unknown")
        lines.append(f"### {r['case_id']}")
        lines.append("")
        lines.append("| Aspect | Reproduction Env | Production Env |")
        lines.append("|--------|-----------------|----------------|")
        all_keys = sorted(set(list(repro_env.keys()) + list(prod_env.keys())))
        for k in all_keys:
            rv = repro_env.get(k, "—")
            pv = prod_env.get(k, "—")
            lines.append(f"| {k} | {rv} | {pv} |")
        lines.append("")
        lines.append(f"- **Production triggerable**: `{triggerable}`")
        if delta_warning:
            lines.append(f"- **WARNING**: {delta_warning}")
        else:
            lines.append("- No repro-only markers detected (or envs match).")
        lines.append("")
    if not any_delta:
        lines.append("_No reproduction_env provided for any case — consider filling in `reproduction_env` and `production_env` fields in the benchmark file to enable environment delta assessment._")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_bug_benchmark(args):
    """Run a BUG benchmark and report results.

    Usage:
        bug-benchmark --graph <dir> --benchmark <file> --run [--source <root>]
        bug-benchmark --create-template <out.json>
    """
    if getattr(args, "create_template", ""):
        create_benchmark_template(args.create_template)
        return

    graph_dir = args.graph
    benchmark_path = args.benchmark
    if not os.path.exists(benchmark_path):
        print(f"Benchmark file not found: {benchmark_path}", file=sys.stderr)
        sys.exit(1)

    source_root = getattr(args, "source", "")
    print(f"Running benchmark: {benchmark_path}", file=sys.stderr)
    data = run_benchmark(graph_dir, benchmark_path, source_root)
    summary = compute_summary(data["results"])
    data["summary"] = summary

    # Save raw results
    out_path = os.path.join(graph_dir, ".code2database_bug_benchmark.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"Wrote raw results: {out_path}", file=sys.stderr)

    # Generate + print Markdown report
    report = generate_report(data["results"], summary)
    report_path = os.path.join(graph_dir, ".code2database_bug_benchmark.md")
    Path(report_path).write_text(report, encoding="utf-8")
    print(report)
    print(f"\nWrote report: {report_path}", file=sys.stderr)
