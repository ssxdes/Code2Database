#!/usr/bin/env python3
"""Turn pytest results into GitHub annotations and a step summary.

Reads the junit xml pytest wrote next to the log and emits one
::error annotation per failing test carrying the test id, the
assertion message and the tail of its traceback, so the run page
names each breakage without opening the artifact. With no xml at
hand it falls back to the FAILED/ERROR summary lines of the log.
Diagnostics never change the step's exit status.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET

MAX_ANNOTATIONS = 10
TB_TAIL_ANNOTATION = 12
TB_TAIL_SUMMARY = 30


def _escape(msg):
    return (msg.replace("%", "%25")
               .replace("\r", "%0D")
               .replace("\n", "%0A"))


def _tail(text, n):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def _location(text, fallback_file, fallback_line):
    hit = None
    for hit in re.finditer(r"^\s*([^\s:]+\.py):(\d+):", text, re.M):
        pass
    if hit:
        return hit.group(1), hit.group(2)
    return fallback_file or "", fallback_line or ""


def _ident(case):
    file = case.get("file") or ""
    name = case.get("name") or ""
    cls = (case.get("classname") or "").rsplit(".", 1)[-1]
    stem = os.path.basename(file).rsplit(".", 1)[0] if file else ""
    parts = [p for p in (file, cls if cls and cls != stem else "", name) if p]
    return "::".join(parts)


def _from_xml(path):
    cases = []
    totals = {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return cases, totals
    for attr in ("tests", "failures", "errors", "skipped", "time"):
        v = root.get(attr)
        if v is None and root.tag == "testsuites":
            for child in root.iter("testsuite"):
                v = child.get(attr)
                if v is not None:
                    break
        if v is not None:
            totals[attr] = v
    for tc in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = tc.find(kind)
            if node is None:
                continue
            cases.append({
                "file": tc.get("file") or "",
                "line": tc.get("line") or "",
                "classname": tc.get("classname") or "",
                "name": tc.get("name") or "",
                "message": node.get("message") or "",
                "text": node.text or "",
            })
    return cases, totals


def _from_log(path):
    cases = []
    try:
        with open(path, errors="replace") as f:
            for ln in f:
                if ln.startswith(("FAILED ", "ERROR ")):
                    cases.append({
                        "file": "", "line": "", "classname": "",
                        "name": ln.strip(), "message": "", "text": "",
                    })
    except OSError:
        pass
    return cases


def main():
    xml_path = sys.argv[1] if len(sys.argv) > 1 else "pytest-results.xml"
    log_path = sys.argv[2] if len(sys.argv) > 2 else "pytest-output.log"
    cases, totals = _from_xml(xml_path)
    if not cases:
        cases = _from_log(log_path)

    reported = []
    for idx, c in enumerate(cases):
        where, line = _location(c["text"], c["file"], c["line"])
        ident = _ident(c)
        if where and not c["file"]:
            ident = f"{where}::{ident}"
        body_lines = []
        if c["message"]:
            body_lines.append(c["message"].splitlines()[0])
        tail = _tail(c["text"], TB_TAIL_ANNOTATION)
        if tail:
            body_lines.append("--- traceback tail ---")
            body_lines.append(tail)
        body = "\n".join(body_lines) or ident
        if idx < MAX_ANNOTATIONS:
            props = []
            if where:
                props.append(f"file={where}")
            if line:
                props.append(f"line={line}")
            props_str = ",".join(props)
            prefix = f"::{props_str}::" if props_str else "::"
            payload = ident if body == ident else ident + chr(10) + body
            print(f"::error{prefix}{_escape(payload)}")
        reported.append((ident, body, _tail(c["text"], TB_TAIL_SUMMARY)))

    lines = ["## Test results", ""]
    if totals:
        lines.append(
            f"{totals.get('failures', '?')} failed, "
            f"{totals.get('errors', '?')} errored, "
            f"{totals.get('skipped', '?')} skipped, "
            f"{totals.get('tests', '?')} total "
            f"({totals.get('time', '?')}s)")
        lines.append("")
    if not reported:
        lines.append("No failing tests.")
    else:
        lines.append(f"{len(reported)} failing test(s):")
        lines.append("")
        if len(reported) > MAX_ANNOTATIONS:
            lines.append(
                f"Only the first {MAX_ANNOTATIONS} also appear as "
                "annotations; the artifact carries every traceback.")
            lines.append("")
        for ident, body, tail in reported:
            lines.append(f"<details><summary><code>{ident}</code></summary>")
            lines.append("")
            lines.append("```")
            lines.append(tail or body)
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    blob = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(blob)
    else:
        print(blob)
    return 0


if __name__ == "__main__":
    sys.exit(main())
