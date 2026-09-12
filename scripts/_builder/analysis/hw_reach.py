"""Hardware reachability analysis — trace call chains to hardware terminals.

For driver and embedded code the decisive question about a function is
often whether it eventually reaches a register read/write or a bus
transaction. This module BFS-traces the call graph from a symbol
along call edges and stops at hardware terminal functions (matched by
name patterns), then classifies the symbol:

  - ``hardware-reaching`` — at least one path ends at a terminal
  - ``hold-flush`` — the symbol itself belongs to a config
    hold/flush mechanism (batched register download) and is tracked
    separately from direct access
  - ``software-gate`` — no terminal path, but the symbol has few
    callees and a gate-style name (Set/Update/Enable/...), so it
    likely controls a later hardware operation
  - ``software-only`` — plain software logic

Terminal patterns come from built-in defaults plus the project
profile key ``hardware_terminals`` (list of regex strings; see
``_DEFAULT_PROFILE`` in ``_profile/schema.py``), so projects declare
their own register-access API names without code changes.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Pattern

from _builder.analysis.quality_checks import _is_call_relation

_DEFAULT_HW_TERMINALS = [
    r"\bWrite\d*BitReg\b",
    r"\bRead\d*BitReg\b",
    r"\bWrite\d*BitData\b",
    r"\bRead\d*BitData\b",
    r"\bSpiWrite\b",
    r"\bSpiRead\b",
    r"\bWritePcieI2c\b",
    r"\bReadPcieI2c\b",
    r"\bWriteRegByAddr\b",
    r"\bWriteRegByName\b",
    r"\bWriteFlowCtrl\b",
    r"\bWriteFrameData\b",
    r"\bBatchWrite\b",
]
_HOLD_FLUSH_PATTERNS = [
    r"\bCfgHold\b",
    r"\bConfigHold\b",
    r"\bSetCfgHold\b",
    r"\bResendAllCfg\b",
    r"\bResendCfg\b",
    r"\bFlushCfg\b",
]
_GATE_NAME_RE = re.compile(
    r"\b(?:Set|Update|Recover|Control|Enable|Disable|Config|Init|Apply|Commit)\w*")
_MAX_GATE_CALLEES = 3


def load_hw_profile(graph_dir: str,
                    profile_path: Optional[str] = None) -> Dict[str, Any]:
    """Load the builder profile for terminal patterns.

    Prefers an explicit ``--profile`` path, then the profile persisted
    in the graph directory by the build command. Returns {} when no
    profile is available (built-in defaults still apply).
    """
    candidates = [profile_path] if profile_path else [
        os.path.join(graph_dir or "", ".code2database_profile.json")]
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (IOError, OSError, ValueError):
                continue
    return {}


def _compile_all(patterns: List[str]) -> List[Pattern]:
    compiled = []
    for p in patterns:
        if p:
            compiled.append(re.compile(p))
    return compiled


def hw_reach(graph_dir: str, node: str, depth: int = 6, max_paths: int = 5,
             profile: Optional[Dict[str, Any]] = None,
             extra_patterns: Optional[List[str]] = None) -> Dict[str, Any]:
    """Classify a symbol by whether its call chain reaches hardware.

    Args:
        graph_dir: C2D graph directory.
        node: Function name or node ID.
        depth: Maximum BFS depth along call edges.
        max_paths: Maximum terminal paths to report.
        profile: Builder profile dict (``hardware_terminals`` key adds
            project-specific terminal patterns to the defaults).
        extra_patterns: Additional terminal regex patterns.

    Returns:
        {symbol, name, classification, paths, terminal_hits,
        depth_searched, callee_count}
    """
    from _builder.graph.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    target = node if node in G else None
    if target is None:
        for nid in G.nodes:
            if G.nodes[nid].get("name", "") == node:
                target = nid
                break
    if target is None:
        lowered = node.lower()
        for nid in G.nodes:
            if lowered in nid.lower():
                target = nid
                break
    if target is None:
        raise ValueError(f"function '{node}' not found")

    patterns = list(_DEFAULT_HW_TERMINALS)
    if profile:
        patterns.extend(profile.get("hardware_terminals", [])
                        or [])
    if extra_patterns:
        patterns.extend(extra_patterns)
    terminal_res = _compile_all(patterns)
    hold_res = _compile_all(_HOLD_FLUSH_PATTERNS)

    name = G.nodes[target].get("name", "") or target

    def _call_succ(nid: str) -> List[str]:
        out = []
        for succ in G.successors(nid):
            ed = G.get_edge_data(nid, succ) or {}
            if _is_call_relation(ed.get("relation", "")):
                out.append(succ)
        return out

    callee_count = len(_call_succ(target))

    if any(rx.search(name) for rx in terminal_res):
        classification = "hardware-reaching"
        paths = [[name]]
        terminal_hits = [{"name": name}]
    elif any(rx.search(name) for rx in hold_res):
        classification = "hold-flush"
        paths = []
        terminal_hits = []
    else:
        paths: List[List[str]] = []
        hits: List[Dict[str, str]] = []

        def _name_of(nid: str) -> str:
            return G.nodes[nid].get("name", "") or nid

        def _dfs(cur: str, chain: List[str]) -> None:
            if len(paths) >= max_paths:
                return
            if len(chain) - 1 >= depth:
                return  # hop budget exhausted
            for succ in _call_succ(cur):
                sname = _name_of(succ)
                matched = next((rx.pattern for rx in terminal_res
                                if rx.search(sname)), None)
                if matched is not None:
                    paths.append(chain + [sname])
                    hits.append({"name": sname, "pattern": matched})
                    if len(paths) >= max_paths:
                        return
                    continue  # terminals are not traversed further
                if succ in on_path:
                    continue
                on_path.add(succ)
                _dfs(succ, chain + [sname])
                on_path.discard(succ)

        on_path = {target}
        _dfs(target, [name])

        if paths:
            classification = "hardware-reaching"
            terminal_hits = hits
        elif callee_count and callee_count <= _MAX_GATE_CALLEES \
                and _GATE_NAME_RE.search(name):
            classification = "software-gate"
            terminal_hits = []
        else:
            classification = "software-only"
            terminal_hits = []

    return {
        "symbol": target,
        "name": name,
        "classification": classification,
        "paths": paths,
        "terminal_hits": terminal_hits,
        "depth_searched": depth,
        "callee_count": callee_count,
    }


def cmd_hw_reach(args):
    """CLI handler for `code2database_builder.py hw-reach`."""
    import sys
    profile = load_hw_profile(args.graph,
                              getattr(args, "profile", None))
    extra = []
    raw_extra = getattr(args, "terminals", "") or ""
    if raw_extra:
        extra = [p.strip() for p in raw_extra.split(",") if p.strip()]
    try:
        result = hw_reach(
            args.graph,
            args.node,
            depth=getattr(args, "depth", 6),
            max_paths=getattr(args, "max_paths", 5),
            profile=profile,
            extra_patterns=extra,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
