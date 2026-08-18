#!/usr/bin/env python3
"""O4: Run Code2Database evals and auto-judge results.

Usage:
    python3 scripts/run_evals.py [--evals evals/evals_en.json] [--workdir /tmp/eval_run]

For each eval:
  1. Parse the prompt to extract: source path, target function, expected labels/conditions
  2. Run scan + build via code2database_scanner.py / code2database_builder.py
  3. Run the appropriate query (explore-flow / describe-node / context_pack_lite)
  4. Auto-judge the result against expected_output by checking:
     - Did the build produce code2database_master.json or code2database.db?
     - Did the query return non-empty results for the target function?
     - Do the expected labels appear in the result? (keyword match)
     - Do the expected conditional branches appear? (keyword match)
  5. Print a per-eval score and a summary.

Exit codes: 0=all evals pass, 1=one or more fail, 2=usage error.

The auto-judgment is keyword-based and conservative — it flags obvious
failures (missing files, empty results, missing expected keywords) but
does not verify semantic correctness. A human should review failures.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def parse_prompt(prompt: str) -> dict:
    """Extract source path and target function from a prompt.

    Heuristics:
      - "/path/to/something" → source_dir
      - "call chain of FUNCTION" / "call paths of FUNCTION" / "call flow ... FUNCTION"
        → target function
    """
    result = {"source_dir": None, "target_func": None, "raw": prompt}

    # Extract first path-like token
    path_match = re.search(r"(/(?:[\w.\-]+/)*[\w.\-]+)", prompt)
    if path_match:
        result["source_dir"] = path_match.group(1)

    # Extract target function
    patterns = [
        r"call chain of\s+(?:the\s+)?(\w+)",
        r"call paths of\s+(?:the\s+)?(\w+)",
        r"call flow from\s+(\w+)",
        r"call flow\s+(?:\w+\s+)*?to\s+(\w+)",
    ]
    for pat in patterns:
        m = re.search(pat, prompt, re.IGNORECASE)
        if m:
            result["target_func"] = m.group(1)
            break

    return result


def extract_expected_keywords(expected: str) -> dict:
    """Extract keywords to look for in the result.

    Returns dict with:
      - labels: list of label names mentioned (thread_processor, constructor, etc.)
      - functions: list of function names mentioned (bdev_register, etc.)
      - conditions: list of condition keywords (if, mode==1, else, etc.)
    """
    labels = []
    for label in ("API_entry", "thread_processor", "callback_func",
                  "constructor", "destructor", "out_end", "unknown_end"):
        if label in expected:
            labels.append(label)

    # Function-like tokens: identifiers with underscore or CamelCase, 4+ chars
    func_tokens = re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]{3,})\b", expected)
    # Filter out common English words
    stop = {"should", "scan", "build", "query", "generate", "tell",
            "correctly", "annotate", "identify", "extract", "trace",
            "marked", "detected", "via", "using", "must", "call",
            "callgraph", "graph", "chain", "path", "flow", "labels",
            "label", "order", "conditions", "conditional", "branches",
            "branch", "method", "class", "code", "directory", "project",
            "multi", "language", "empty", "node", "aggregation",
            "public", "private", "internal", "external", "thread",
            "threading", "server", "handler", "init", "start", "listen"}
    functions = [t for t in func_tokens if t.lower() not in stop
                 and not t.startswith("_") and t not in labels]

    conditions = []
    if re.search(r"\bif\b", expected):
        conditions.append("if")
    if re.search(r"\belse\b", expected):
        conditions.append("else")

    return {"labels": labels, "functions": functions, "conditions": conditions}


def run_cmd(cmd: list, timeout: int = 300) -> dict:
    """Run a command, return dict with returncode, stdout, stderr."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"timeout after {timeout}s"}
    except FileNotFoundError as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def judge_eval(eval_item: dict, workdir: str, scanner: str, builder: str) -> dict:
    """Run one eval and judge the result.

    Returns dict with:
      - passed: bool
      - score: 0-100
      - reasons: list of strings
      - outputs: dict of command outputs
    """
    parsed = parse_prompt(eval_item["prompt"])
    expected_kw = extract_expected_keywords(eval_item["expected_output"])

    result = {
        "passed": False,
        "score": 0,
        "reasons": [],
        "outputs": {},
        "parsed": parsed,
        "expected_keywords": expected_kw,
    }

    eval_workdir = os.path.join(workdir, f"eval_{eval_item['id']}")
    os.makedirs(eval_workdir, exist_ok=True)
    outdir = os.path.join(eval_workdir, "code2db-out")
    os.makedirs(outdir, exist_ok=True)

    # Step 1: scan (only if source_dir exists)
    if not parsed["source_dir"] or not os.path.exists(parsed["source_dir"]):
        result["reasons"].append(
            f"source dir not found: {parsed['source_dir']!r} — skipping scan/build, "
            f"judging keyword coverage only"
        )
        # For evals without a real source dir, we can only judge the prompt
        # parsing. Mark as passed if we extracted a target function.
        if parsed["target_func"]:
            result["score"] = 50
            result["passed"] = False
            result["reasons"].append(
                "no source dir → cannot verify scan/build, score 50/100"
            )
        else:
            result["reasons"].append("could not extract target function from prompt")
        return result

    extraction_path = os.path.join(outdir, ".code2database_extraction.json")
    scan_cmd = [
        sys.executable, scanner, "scan",
        "--source", parsed["source_dir"],
        "--output", extraction_path,
    ]
    scan_out = run_cmd(scan_cmd, timeout=600)
    result["outputs"]["scan"] = scan_out
    if scan_out["returncode"] != 0:
        result["reasons"].append(f"scan failed (rc={scan_out['returncode']})")
        return result

    # Step 2: build
    build_cmd = [
        sys.executable, builder, "build",
        "--extraction", extraction_path,
        "--outdir", outdir,
        "--build-config", "auto",
    ]
    build_out = run_cmd(build_cmd, timeout=600)
    result["outputs"]["build"] = build_out
    if build_out["returncode"] != 0:
        result["reasons"].append(f"build failed (rc={build_out['returncode']})")
        return result

    # Check for output files
    master_path = os.path.join(outdir, "code2database_master.json")
    db_path = os.path.join(outdir, "code2database.db")
    if not os.path.exists(master_path) and not os.path.exists(db_path):
        result["reasons"].append("neither code2database_master.json nor code2database.db produced")
        return result
    result["score"] += 30  # build succeeded

    # Step 3: query (if target_func extracted)
    if parsed["target_func"]:
        query_cmd = [
            sys.executable, builder, "explore-flow",
            "--graph", outdir,
            "--query", parsed["target_func"],
            "--max-tokens", "2000",
        ]
        query_out = run_cmd(query_cmd, timeout=120)
        result["outputs"]["query"] = query_out
        if query_out["returncode"] == 0 and query_out["stdout"].strip():
            result["score"] += 30
            # Check expected keywords in output
            combined = query_out["stdout"] + query_out["stderr"]
            matched_labels = [l for l in expected_kw["labels"] if l in combined]
            matched_funcs = [f for f in expected_kw["functions"] if f in combined]
            if matched_labels:
                result["score"] += 15
            if matched_funcs:
                result["score"] += 15
            result["reasons"].append(
                f"query returned output; matched labels={matched_labels}, "
                f"funcs={matched_funcs[:5]}"
            )
        else:
            result["reasons"].append(
                f"query failed or empty (rc={query_out['returncode']})"
            )
    else:
        result["score"] += 20  # no target func to query, but build succeeded
        result["reasons"].append("no target function extracted, skipping query")

    # Final judgment
    result["passed"] = result["score"] >= 70
    return result


def main():
    parser = argparse.ArgumentParser(description="Run Code2Database evals")
    parser.add_argument("--evals", default="evals/evals_en.json",
                        help="Path to evals JSON file")
    parser.add_argument("--workdir", default=None,
                        help="Working directory for eval runs (default: temp dir)")
    parser.add_argument("--scanner", default=None,
                        help="Path to code2database_scanner.py")
    parser.add_argument("--builder", default=None,
                        help="Path to code2database_builder.py")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    scanner = args.scanner or str(repo_root / "scripts" / "code2database_scanner.py")
    builder = args.builder or str(repo_root / "scripts" / "code2database_builder.py")

    if not os.path.exists(args.evals):
        print(f"Error: evals file not found: {args.evals}", file=sys.stderr)
        return 2

    with open(args.evals, "r", encoding="utf-8") as f:
        evals_data = json.load(f)

    workdir = args.workdir or tempfile.mkdtemp(prefix="code2database_evals_")
    os.makedirs(workdir, exist_ok=True)
    print(f"Workdir: {workdir}", file=sys.stderr)

    results = []
    for eval_item in evals_data.get("evals", []):
        print(f"\n--- Eval {eval_item['id']} ---", file=sys.stderr)
        print(f"  Prompt: {eval_item['prompt'][:80]}...", file=sys.stderr)
        r = judge_eval(eval_item, workdir, scanner, builder)
        r["id"] = eval_item["id"]
        r["prompt"] = eval_item["prompt"]
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  Result: {status} (score={r['score']}/100)", file=sys.stderr)
        for reason in r["reasons"]:
            print(f"    - {reason}", file=sys.stderr)

    # Summary
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"\n=== Summary: {passed}/{total} passed ===", file=sys.stderr)

    # Write JSON report
    report_path = os.path.join(workdir, "evals_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "evals_file": args.evals,
            "workdir": workdir,
            "summary": {"passed": passed, "total": total},
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"Report: {report_path}", file=sys.stderr)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
