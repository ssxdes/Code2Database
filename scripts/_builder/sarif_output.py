"""SARIF 2.1.0 output adapter — industry-standard findings format.

Converts C2D analysis results (detect-races, blast-radius, taint-analysis)
to SARIF 2.1.0 JSON for CI/IDE ingestion (GitHub Code Scanning, VS Code,
Azure DevOps, etc.).

Spec: https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List


def results_to_sarif(results: List[Dict], tool_name: str = "Code2Database",
                    tool_version: str = "1.3.0") -> Dict:
    """Convert a list of finding dicts to SARIF 2.1.0 format.

    Each finding should have:
    - rule_id: short rule identifier
    - message: human-readable description
    - level: "error" | "warning" | "note"
    - file: source file path
    - line: line number (1-based)
    - function: function name (optional)
    """
    rules = []
    results_sarif = []
    seen_rules = set()

    for r in results:
        rule_id = r.get("rule_id", r.get("type", "issue"))
        rule_short = r.get("rule_short_desc", rule_id)
        rule_full = r.get("rule_full_desc", r.get("message", ""))

        if rule_id not in seen_rules:
            seen_rules.add(rule_id)
            rules.append({
                "id": rule_id,
                "name": rule_id.replace("-", "_").upper(),
                "shortDescription": {"text": rule_short},
                "fullDescription": {"text": rule_full},
                "defaultConfiguration": {"level": r.get("level", "warning")},
            })

        level = r.get("level", "warning")
        if level == "high":
            level = "error"
        elif level == "low":
            level = "note"

        result = {
            "ruleId": rule_id,
            "level": level,
            "message": {"text": r.get("message", "")},
            "locations": [],
        }

        if r.get("file"):
            loc = {
                "physicalLocation": {
                    "artifactLocation": {"uri": r["file"]},
                }
            }
            if r.get("line"):
                loc["physicalLocation"]["region"] = {
                    "startLine": r["line"],
                }
            result["locations"].append(loc)

        if r.get("function"):
            result.setdefault("properties", {})["function"] = r["function"]

        results_sarif.append(result)

    return {
        "version": "2.1.0",
        "$schema": "https://docs.oasis-open.org/sarif/sarif/v2.1.0/cs01/schemas/sarif-schema-2.1.0.json",
        "runs": [{
            "tool": {
                "driver": {
                    "name": tool_name,
                    "version": tool_version,
                    "rules": rules,
                }
            },
            "results": results_sarif,
        }]
    }


def races_to_sarif(races: List[Dict]) -> Dict:
    """Convert detect-races output to SARIF."""
    results = []
    for r in races:
        results.append({
            "rule_id": f"race_{r.get('type', 'data_race')}",
            "rule_short_desc": f"Data race: {r.get('shared_resource', {}).get('name', '?')}",
            "message": r.get("description", str(r)),
            "level": "error" if r.get("severity") == "high" else "warning",
            "file": r.get("reader", {}).get("source_file", r.get("writer", {}).get("source_file", "")),
            "line": r.get("reader", {}).get("line", r.get("writer", {}).get("line", 0)),
            "function": r.get("reader", {}).get("function", ""),
        })
    return results_to_sarif(results, tool_name="Code2Database-races")


def taint_to_sarif(flows: List[Dict]) -> Dict:
    """Convert taint-analysis flows to SARIF."""
    results = []
    for f in flows:
        results.append({
            "rule_id": "taint_flow",
            "rule_short_desc": f"Taint flow: {f.get('source', '?')} → {f.get('sink', '?')}",
            "message": f"Taint flows from {f.get('source', '?')} to {f.get('sink', '?')} "
                       f"{'(sanitized)' if f.get('sanitized') else '(UNSANITIZED)'} "
                       f"via {len(f.get('path', []))} hops",
            "level": "note" if f.get("sanitized") else "error",
            "file": "",
            "function": f.get("source", ""),
        })
    return results_to_sarif(results, tool_name="Code2Database-taint")


def cmd_sarif_export(args):
    """CLI handler: sarif-export."""
    # Read input JSON from --input
    input_path = getattr(args, "input", "")
    if not input_path or not os.path.exists(input_path):
        print("Usage: sarif-export --input <results.json> [--type races|taint|generic]")
        return
    with open(input_path) as f:
        data = json.load(f)

    sarif_type = getattr(args, "type", "generic")
    if sarif_type == "races":
        races = data.get("races", [data] if isinstance(data, dict) else data)
        sarif = races_to_sarif(races)
    elif sarif_type == "taint":
        flows = data.get("flows", [data] if isinstance(data, dict) else data)
        sarif = taint_to_sarif(flows)
    else:
        findings = data if isinstance(data, list) else [data]
        sarif = results_to_sarif(findings)

    output = getattr(args, "output", "")
    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(sarif, f, ensure_ascii=False, indent=2)
        print(f"SARIF written to {output}")
    else:
        print(json.dumps(sarif, ensure_ascii=False, indent=2))
