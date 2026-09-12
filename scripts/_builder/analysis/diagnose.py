"""Symptom-driven diagnosis reports from the code graph.

`diagnose` produces a six-dimension report around one symbol, aimed
at the moment a failure is being investigated:

1. Symptom Parsing — symbols and error-indicative lines extracted
   from an optional log file
2. Impact Area — direct and second-order callers (who is affected)
3. Call Chain Tracing — forward chains from the symbol to leaf
   endpoints
4. Cross Validation — callees shared by multiple chains (convergence
   points that multiple paths depend on)
5. Special Patterns — hardware reachability classification, direct
   recursion, async spawns, callback registration
6. Root-Cause Hypotheses — impact-area candidates ranked by local
   signals (caller fan-in, unguarded array access, constant-true
   loops without exits)

Output: Markdown for humans, ``--json`` for tooling.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from _builder.analysis.hw_reach import hw_reach
from _builder.analysis.quality_checks import (
    _LOOP_HEADER_RES,
    _classify_loop_body,
    _extract_brace_block,
    _is_call_relation,
)

_ERROR_LINE_RE = re.compile(
    r"error|fail|panic|assert|fault|abort|crash|timeout|deadlock",
    re.IGNORECASE)
_SUBSCRIPT_RE = re.compile(r"(\w+)\s*\[\s*([^\]\'\"]+?)\s*\]")
_CONST_INDEX_RE = re.compile(r"^(?:\d+|0[xX][0-9a-fA-F]+|sizeof.*|[A-Z][A-Z_0-9]*)$")
_GUARD_RE = re.compile(r"\b(?:if|assert|unlikely|likely)\b")


def _name(G, nid: str) -> str:
    return G.nodes[nid].get("name") or nid


def _resolve(G, node: str) -> Optional[str]:
    if node in G:
        return node
    for nid in G.nodes:
        if G.nodes[nid].get("name", "") == node:
            return nid
    lowered = node.lower()
    for nid in G.nodes:
        if lowered in nid.lower():
            return nid
    return None


def _call_succ(G, nid: str) -> List[str]:
    return [v for v in G.successors(nid)
            if _is_call_relation((G.get_edge_data(nid, v) or {})
                                 .get("relation", ""))]


def _call_pred(G, nid: str) -> List[str]:
    return [u for u in G.predecessors(nid)
            if _is_call_relation((G.get_edge_data(u, nid) or {})
                                 .get("relation", ""))]


def _parse_log(log_path: str, G) -> Dict[str, Any]:
    """Extract error-indicative lines and graph symbols from a log."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except (IOError, OSError) as e:
        return {"error": f"cannot read log: {e}", "error_lines": [],
                "symbols": []}
    error_lines = [ln.strip() for ln in lines
                   if _ERROR_LINE_RE.search(ln)][:20]
    names = sorted({G.nodes[n].get("name", n) for n in G.nodes
                    if G.nodes[n].get("name")},
                   key=len, reverse=True)
    symbols: List[str] = []
    pool = "\n".join(error_lines) if error_lines else "\n".join(lines[:500])
    for nm in names:
        if len(symbols) >= 20:
            break
        if re.search(r"\b" + re.escape(nm) + r"\b", pool):
            symbols.append(nm)
    return {"error_lines": error_lines, "symbols": symbols,
            "lines_scanned": len(lines)}


def _forward_chains(G, start: str, depth: int,
                    max_chains: int) -> List[List[str]]:
    """Forward call chains from start to leaf endpoints."""
    chains: List[List[str]] = []

    def _dfs(chain: List[str]) -> None:
        if len(chains) >= max_chains:
            return
        if len(chain) - 1 >= depth:
            chains.append(list(chain))
            return
        cur = chain[-1]
        succs = _call_succ(G, cur)
        if not succs:
            chains.append(list(chain))
            return
        extended = False
        for s in sorted(succs):
            if s in chain:
                continue
            _dfs(chain + [s])
            extended = True
        if not extended:
            chains.append(list(chain))

    _dfs([start])
    return chains


def _body_signals(body: str) -> List[str]:
    """Light-weight risk signals in a function body."""
    signals: List[str] = []
    if not body or not body.strip():
        return signals
    for pattern, rx in _LOOP_HEADER_RES:
        for m in rx.finditer(body):
            after = m.end()
            while after < len(body) and body[after] in " \t\r\n":
                after += 1
            if after < len(body) and body[after] == "{":
                span = _extract_brace_block(body, after)
                block = body[span[0]:span[1] + 1] if span else ""
            else:
                end = body.find(";", after)
                block = body[after:end if end != -1 else len(body)]
            if block and _classify_loop_body(block)["classification"] == "risky":
                signals.append("unbounded_loop")
    lines = body.split("\n")
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#")):
            continue
        m = _SUBSCRIPT_RE.search(raw)
        if not m:
            continue
        index_expr = m.group(2).strip()
        if _CONST_INDEX_RE.match(index_expr):
            continue
        if index_expr.startswith(('"', "'")):
            continue
        window = lines[max(0, i - 3):i + 1]
        if not any(_GUARD_RE.search(w) and index_expr in w for w in window):
            signals.append("unguarded_subscript")
            break
    return signals


def diagnose(graph_dir: str, symbol: str, log_file: Optional[str] = None,
             depth: int = 6, max_chains: int = 5, output: Optional[str] = None,
             as_json: bool = False) -> Any:
    """Render the six-dimension diagnosis report for a symbol.

    Returns the Markdown string (or the structured dict with
    ``as_json=True``); ``output`` additionally writes the Markdown.
    """
    from _builder.graph.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    target = _resolve(G, symbol)
    if target is None:
        raise ValueError(f"function '{symbol}' not found")

    data: Dict[str, Any] = {"symbol": target, "name": _name(G, target)}

    # 1. Symptom parsing
    if log_file:
        data["symptoms"] = _parse_log(log_file, G)
    else:
        data["symptoms"] = {"note": "no log supplied"}

    # 2. Impact area (the symbol itself is excluded — self-recursion is
    # reported under special patterns instead)
    ring1 = sorted(set(_call_pred(G, target)) - {target})
    ring2_set = set()
    for p in ring1:
        ring2_set.update(_call_pred(G, p))
    ring2_set.discard(target)
    ring2 = sorted(ring2_set - set(ring1))
    data["impact"] = {
        "direct_callers": [{"id": p, "name": _name(G, p)} for p in ring1],
        "second_ring": [{"id": p, "name": _name(G, p)} for p in ring2],
    }

    # 3. Call chains
    chains = _forward_chains(G, target, depth, max_chains)
    data["chains"] = [[_name(G, n) for n in ch] for ch in chains]

    # 4. Cross validation: shared callees across chains
    counts: Dict[str, int] = {}
    for ch in chains:
        for nid in set(ch[1:]):
            counts[nid] = counts.get(nid, 0) + 1
    shared = sorted(((cnt, _name(G, nid)) for nid, cnt in counts.items()
                     if cnt >= 2), reverse=True)
    data["shared_callees"] = [{"name": nm, "chains": cnt} for cnt, nm in shared]

    # 5. Special patterns
    patterns: Dict[str, Any] = {}
    try:
        hw = hw_reach(graph_dir, _name(G, target), depth=depth)
        patterns["hardware_reachability"] = hw["classification"]
        if hw["paths"]:
            patterns["hardware_path"] = hw["paths"][0]
    except (ValueError, FileNotFoundError):
        patterns["hardware_reachability"] = "unknown"
    if target in _call_succ(G, target):
        patterns["direct_recursion"] = True
    spawns_out = [v for v in G.successors(target)
                  if (G.get_edge_data(target, v) or {})
                  .get("concurrency") == "async_spawn"]
    spawns_in = [u for u in G.predecessors(target)
                 if (G.get_edge_data(u, target) or {})
                 .get("concurrency") == "async_spawn"]
    if spawns_out:
        patterns["spawns"] = [_name(G, v) for v in spawns_out]
    if spawns_in:
        patterns["spawned_by"] = [_name(G, u) for u in spawns_in]
    if "callback_func" in (G.nodes[target].get("labels", []) or []):
        patterns["callback"] = True
    data["patterns"] = patterns

    # 6. Root-cause hypotheses — the symbol itself, its caller rings,
    # and its direct callees (a fault may originate in what it calls)
    candidates: List[Dict[str, Any]] = []
    pool = [target] + ring1 + ring2 + sorted(set(_call_succ(G, target)) - {target})
    seen: Set[str] = set()
    for nid in pool:
        if nid in seen:
            continue
        seen.add(nid)
        body = G.nodes[nid].get("body_text", "") or ""
        signals = _body_signals(body)
        fan_in = len(set(_call_pred(G, nid)) - {nid})
        score = fan_in + 2 * len(signals)
        entry = {"id": nid, "name": _name(G, nid), "fan_in": fan_in,
                 "signals": signals, "score": score}
        if (G.get_edge_data(nid, target) or {}).get("concurrency") == "async_spawn":
            entry["signals"] = entry["signals"] + ["async_spawn_edge"]
            entry["score"] += 1
        candidates.append(entry)
    candidates.sort(key=lambda c: (-c["score"], c["name"]))
    data["hypotheses"] = candidates[:10]

    if as_json:
        return data

    md = _render_markdown(G, data)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(md)
    return md


def _render_markdown(G, data: Dict[str, Any]) -> str:
    name = data["name"]
    lines = [f"# Diagnosis Report: {name}", ""]

    lines += ["## 1. Symptom Parsing", ""]
    sym = data["symptoms"]
    if "error_lines" in sym:
        if sym.get("error_lines"):
            lines.append(f"- Error-indicative lines in log: "
                         f"{len(sym['error_lines'])} "
                         f"(of {sym.get('lines_scanned', 0)} scanned)")
            for ln in sym["error_lines"][:8]:
                lines.append(f"  - `{ln[:120]}`")
        else:
            lines.append(f"- No error-indicative lines found "
                         f"({sym.get('lines_scanned', 0)} lines scanned)")
        if sym.get("symbols"):
            lines.append("- Graph symbols mentioned: "
                         + ", ".join(f"`{s}`" for s in sym["symbols"][:10]))
    else:
        lines.append("- No log supplied (`--log` to add one).")
    lines.append("")

    lines += ["## 2. Impact Area", ""]
    imp = data["impact"]
    lines.append(f"- Direct callers: {len(imp['direct_callers'])}"
                 + (" — " + ", ".join(f"`{c['name']}`"
                                      for c in imp["direct_callers"][:10])
                    if imp["direct_callers"] else ""))
    lines.append(f"- Second-ring callers: {len(imp['second_ring'])}"
                 + (" — " + ", ".join(f"`{c['name']}`"
                                      for c in imp["second_ring"][:10])
                    if imp["second_ring"] else ""))
    lines.append("")

    lines += ["## 3. Call Chain Tracing", ""]
    if data["chains"]:
        for ch in data["chains"]:
            lines.append("- " + " → ".join(f"`{n}`" for n in ch))
    else:
        lines.append("- No forward chains (leaf function).")
    lines.append("")

    lines += ["## 4. Cross Validation", ""]
    if data["shared_callees"]:
        for s in data["shared_callees"][:8]:
            lines.append(f"- `{s['name']}` appears in {s['chains']} chains "
                         f"(convergence point)")
    else:
        lines.append("- No shared callees across chains.")
    lines.append("")

    lines += ["## 5. Special Patterns", ""]
    p = data["patterns"]
    lines.append(f"- Hardware reachability: `{p.get('hardware_reachability', 'unknown')}`")
    if p.get("hardware_path"):
        lines.append("  - path: " + " → ".join(p["hardware_path"]))
    if p.get("direct_recursion"):
        lines.append("- Direct recursion detected.")
    if p.get("callback"):
        lines.append("- Registered as callback.")
    if p.get("spawns"):
        lines.append("- Spawns async work: "
                     + ", ".join(f"`{x}`" for x in p["spawns"]))
    if p.get("spawned_by"):
        lines.append("- Spawned asynchronously by: "
                     + ", ".join(f"`{x}`" for x in p["spawned_by"]))
    lines.append("")

    lines += ["## 6. Root-Cause Hypotheses", ""]
    if data["hypotheses"]:
        lines += ["| Candidate | Score | Fan-in | Signals |", "|---|---|---|---|"]
        for h in data["hypotheses"]:
            sigs = ", ".join(h["signals"]) if h["signals"] else "—"
            lines.append(f"| `{h['name']}` | {h['score']} | {h['fan_in']} "
                         f"| {sigs} |")
        lines.append("")
        lines.append("Candidates are ranked by caller fan-in plus body "
                     "signals (unguarded subscripts, constant-true loops "
                     "without exits, async spawn edges); they are leads to "
                     "verify, not verdicts.")
    else:
        lines.append("- No candidates in the impact area.")
    lines.append("")
    return "\n".join(lines)


def cmd_diagnose(args):
    """CLI handler for `code2database_builder.py diagnose`."""
    import sys
    try:
        result = diagnose(
            args.graph,
            args.symbol,
            log_file=getattr(args, "log", None),
            depth=getattr(args, "depth", 6),
            max_chains=getattr(args, "max_chains", 5),
            output=getattr(args, "output", None),
            as_json=bool(getattr(args, "json", False)),
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif not getattr(args, "output", None):
        print(result, end="")
