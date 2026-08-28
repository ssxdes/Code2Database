#!/usr/bin/env python3
"""Auto semantic enhancement for Code2Database.

Closes the loop between LLM and the graph by automating the
"export → LLM fills → write back" cycle. Four improvements:

1. **Auto-fill on query**: when describe-node sees a node with empty
   semantic_desc, it returns the node AND a "fill_request" listing what
   the LLM should fill. The LLM's response can be piped to
   `auto-enhance --apply` without manual export/import.

2. **Confidence-threshold auto-write**: supplements marked EXTRACTED
   with sufficient evidence are written automatically (no confirmation
   needed). Supplements marked INFERRED require confirmation. AMBIGUOUS
   is rejected unless --allow-ambiguous.

3. **Batch confirm**: instead of one prompt per supplement, collect
   many into a batch session and confirm them all at once via
   `batch-confirm` (accept-all / reject-all / per-item).

4. **Rollback**: every write is logged to a JSONL rollback log. The
   `rollback` command reverts to the state before a given write.

Engineer flow (the new pattern):
    $ code2database_builder describe-node --graph out/ --node foo
    # output includes "auto_fill_request": [
    #     {"field": "semantic_desc", "reason": "empty", "context": "..."}]
    # LLM reads the request, fills the field
    $ code2database_builder auto-enhance --graph out/ --node foo \
        --attr 'semantic_desc=Manages worker pool lifecycle'
    # Auto-writes if EXTRACTED+evidence, prompts if INFERRED, logs to rollback
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple


# ---------------------------------------------------------------------------
# Rollback log — every write is recorded so it can be undone
# ---------------------------------------------------------------------------

ROLLBACK_LOG_NAME = ".code2database_rollback.jsonl"


def _rollback_log_path(graph_dir: str) -> str:
    return os.path.join(graph_dir, ROLLBACK_LOG_NAME)


def _append_rollback_entry(graph_dir: str, entry: Dict) -> int:
    """Append an entry to the rollback log. Returns the entry ID."""
    log_path = _rollback_log_path(graph_dir)
    # Compute next ID
    next_id = 1
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        last = json.loads(line)
                        if isinstance(last, dict) and "id" in last:
                            next_id = max(next_id, last["id"] + 1)
                    except json.JSONDecodeError:
                        pass
    entry = {"id": next_id, "timestamp": time.time(), **entry}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return next_id


def list_rollback_entries(graph_dir: str, limit: int = 50) -> List[Dict]:
    """List the most recent rollback entries (newest first)."""
    log_path = _rollback_log_path(graph_dir)
    if not os.path.exists(log_path):
        return []
    entries = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # Newest first, capped
    return list(reversed(entries[-limit:]))


def rollback_to_entry(graph_dir: str, entry_id: int) -> Dict:
    """Rollback a specific entry by ID — restores the previous value.

    Returns a dict describing what was rolled back.
    """
    log_path = _rollback_log_path(graph_dir)
    if not os.path.exists(log_path):
        return {"rolled_back": False, "reason": "no rollback log"}

    target = None
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    if entry.get("id") == entry_id:
                        target = entry
                        break
                except json.JSONDecodeError:
                    pass

    if not target:
        return {"rolled_back": False, "reason": f"entry {entry_id} not found"}

    # Restore the previous value based on the entry
    target_type = target.get("type", "node")
    node_id = target.get("node_id", "")
    field_name = target.get("field", "")
    old_value = target.get("old_value")
    new_value = target.get("new_value")

    if target_type == "node" and node_id and field_name:
        from _builder.update_cmd import (
            _sqlite_update_node, _json_update_node, _detect_backend,
        )
        # Restore by writing the old_value back as a supplement
        # (or removing the supplement if old_value was None)
        backend = _detect_backend(graph_dir)
        attrs = {field_name: old_value} if old_value is not None else {}
        if backend == "sqlite":
            _sqlite_update_node(graph_dir, node_id, attrs,
                                source="rollback", confidence="EXTRACTED")
        else:
            _json_update_node(graph_dir, node_id, attrs,
                              source="rollback", confidence="EXTRACTED")
        # Mark the rollback entry as reverted (don't double-revert)
        target["reverted"] = True
        # Rewrite the log with the updated entry
        _rewrite_rollback_entry(graph_dir, target)
        return {"rolled_back": True, "entry_id": entry_id,
                "field": field_name, "restored_to": old_value}
    return {"rolled_back": False, "reason": "unsupported entry type"}


def _rewrite_rollback_entry(graph_dir: str, updated_entry: Dict):
    """Rewrite a single entry in the rollback log (used to mark reverted)."""
    log_path = _rollback_log_path(graph_dir)
    if not os.path.exists(log_path):
        return
    lines = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    entry = json.loads(line)
                    if entry.get("id") == updated_entry.get("id"):
                        lines.append(json.dumps(updated_entry, ensure_ascii=False,
                                                default=str) + "\n")
                    else:
                        lines.append(line)
                except json.JSONDecodeError:
                    lines.append(line)
    with open(log_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Auto-fill request — what does a node need filled?
# ---------------------------------------------------------------------------

# Fields that LLM can supplement and the rules for when they're "empty"
_FILLABLE_FIELDS = {
    "semantic_desc": lambda v: not v,
    "external_desc": lambda v: not v,
    "api_constraints": lambda v: not v,
    "preconditions": lambda v: not v or (isinstance(v, list) and len(v) == 0),
    "postconditions": lambda v: not v or (isinstance(v, list) and len(v) == 0),
    "loop_invariants": lambda v: not v or (isinstance(v, list) and len(v) == 0),
}


def compute_fill_request(ndata: Dict) -> List[Dict]:
    """Compute which fields on a node are empty and should be LLM-filled.

    Returns a list of {field, reason, context} dicts. The LLM can use
    this to know exactly what to fill, then pipe the result to
    auto-enhance --apply.
    """
    requests = []
    for field, is_empty in _FILLABLE_FIELDS.items():
        value = ndata.get(field, "")
        if is_empty(value):
            requests.append({
                "field": field,
                "reason": "empty",
                "context": _build_fill_context(ndata, field),
            })
    return requests


def _build_fill_context(ndata: Dict, field: str) -> Dict:
    """Build context info to help the LLM fill a field."""
    ctx = {
        "name": ndata.get("name", ""),
        "source_file": ndata.get("source_file", ""),
        "line": ndata.get("line", 0),
        "labels": ndata.get("labels", []),
        "domain": ndata.get("domain", ""),
        "params": ndata.get("params", []),
    }
    if field == "semantic_desc":
        ctx["hint"] = "1-3 sentence description of what this function does"
    elif field == "external_desc":
        ctx["hint"] = "What this endpoint exposes to external callers"
    elif field == "api_constraints":
        ctx["hint"] = "Preconditions on parameters (e.g., 'ctx != NULL')"
    elif field == "preconditions":
        ctx["hint"] = "List of conditions that must hold before calling"
    elif field == "postconditions":
        ctx["hint"] = "List of conditions that hold after return"
    elif field == "loop_invariants":
        ctx["hint"] = "List of loop invariants (one per loop)"
    return ctx


# ---------------------------------------------------------------------------
# Heuristic description fallback — derive fields from node attributes
# ---------------------------------------------------------------------------

_LABEL_VERBS = {
    "API_entry": "Entry point invoked by external callers",
    "thread_processor": "Background worker that processes queued work",
    "callback_func": "Callback invoked through a registered handle",
    "constructor": "Initializes a new instance",
    "destructor": "Releases resources held by an instance",
    "out_end": "Terminal node that produces output",
    "unknown_end": "Terminal node with no observed callees",
}

_DOMAIN_HINTS = {
    "io": "Performs I/O against an external resource",
    "net": "Network path — sends or receives packets",
    "fs": "Filesystem path — reads or writes files",
    "mem": "Memory management — allocates or frees buffers",
    "lock": "Synchronization — acquires or releases locks",
    "init": "Initialization path — sets up runtime state",
    "teardown": "Teardown path — releases runtime state",
    "config": "Configuration path — reads or applies settings",
    "crypto": "Cryptographic path — encrypts, decrypts, or hashes",
    "parse": "Parser path — decodes input into structured form",
}

_BUILTIN_PREFIXES = (
    "Py_", "PyObject_", "PyType_", "PyList_", "PyDict_", "PyTuple_",
    "PyBytes_", "PyUnicode_", "PyLong_", "PyFloat_", "PySet_", "PyFrozen_",
    "PyMethod_", "PyMember_", "PySequence_", "PyMapping_", "PyNumber_",
    "PyIter_", "PyGen_",
)

# Bare lowercase identifiers that are common Python builtin methods / types.
# When a cgdb_node has one of these as its `name` and no byte range, it's an
# auto-created placeholder for an external/builtin callee (e.g., list.append,
# dict.get, str.split) — not a real function in the project. Skip heuristic
# enhancement for these so the review checklist isn't polluted with builtins.
_BUILTIN_METHOD_NAMES = frozenset({
    "append", "extend", "insert", "remove", "pop", "clear", "copy",
    "count", "index", "reverse", "sort", "fromkeys", "get", "setdefault",
    "items", "keys", "values", "update", "popitem",
    "add", "discard", "isdisjoint", "issubset", "issuperset",
    "union", "intersection", "difference", "symmetric_difference",
    "find", "rfind", "index", "rindex", "count", "replace", "split",
    "rsplit", "splitlines", "join", "lower", "upper", "title", "capitalize",
    "swapcase", "strip", "lstrip", "rstrip", "ljust", "rjust", "center",
    "startswith", "endswith", "isalpha", "isdigit", "isalnum", "isspace",
    "isupper", "islower", "istitle", "encode", "decode", "format",
    "format_map", "zfill", "expandtabs", "maketrans", "translate",
    "encode", "decode",
    "read", "readline", "readlines", "write", "writelines", "writeline",
    "seek", "tell", "close", "flush", "fileno", "isatty",
    "open", "input", "print",
    "str", "int", "float", "bool", "list", "tuple", "set", "dict",
    "frozenset", "bytes", "bytearray", "complex", "object", "type",
    "range", "enumerate", "zip", "map", "filter", "reversed", "sorted",
    "iter", "next", "len", "max", "min", "sum", "abs", "round", "pow",
    "divmod", "hash", "id", "repr", "ascii", "chr", "ord", "bin", "oct",
    "hex", "format", "vars", "dir", "locals", "globals", "getattr",
    "setattr", "delattr", "hasattr", "isinstance", "issubclass",
    "callable", "classmethod", "staticmethod", "property", "super",
    "memoryview",
    "wait", "notify", "notify_all", "acquire", "release",
    "submit", "shutdown", "result", "cancel", "running", "done", "cancelled",
    "lock", "unlock", "trylock",
    "put", "get_nowait", "put_nowait", "qsize", "empty", "full",
    "task_done", "join",
    "send", "recv", "recvfrom", "sendto", "sendall", "connect", "bind",
    "listen", "accept", "shutdown", "settimeout", "gettimeout",
    "setsockopt", "getsockopt", "fileno",
})


def _is_likely_builtin(name: str) -> bool:
    if not name:
        return False
    if name.startswith(_BUILTIN_PREFIXES):
        return True
    # Bare builtin method/type names (no dot) — auto-created external callees.
    if name in _BUILTIN_METHOD_NAMES:
        return True
    if "." in name:
        head = name.split(".", 1)[0]
        if head in ("__builtins__", "builtins", "os", "sys", "io",
                    "math", "time", "threading", "asyncio", "logging",
                    "collections", "itertools", "functools",
                    "json", "re", "socket", "struct", "subprocess",
                    "tarfile", "gzip", "hashlib", "hmac", "ssl",
                    "urllib", "http", "sqlite3", "ctypes", "pprint",
                    "weakref", "gc", "signal", "errno", "select",
                    "queue", "multiprocessing", "concurrent",
                    "pathlib", "shutil", "tempfile", "fnmatch",
                    "glob", "argparse", "configparser", "csv",
                    "datetime", "decimal", "fractions", "random",
                    "statistics", "string", "textwrap", "unicodedata",
                    "zlib", "bz2", "lzma", "copy", "enum", "typing",
                    "dataclasses", "inspect", "traceback", "warnings",
                    "contextlib", "unittest", "doctest", "pdb",
                    "profile", "pstats", "timeit", "ast", "dis",
                    "compile", "code", "codeop", "imp", "importlib",
                    "pkgutil", "modulefinder", "pickle", "shelve",
                    "marshal", "array", "bisect", "heapq",
                    "operator", "abc", "types", "copyreg",
                    "platform", "locale", "gettext", "calendar"):
            return True
    return False


def _split_camel(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if i > 0 and ch.isupper() and name[i - 1].islower():
            out.append(" ")
        out.append(ch.lower())
    return "".join(out)


def _split_snake(name: str) -> str:
    return name.replace("_", " ")


def _humanize(name: str) -> str:
    """Convert an identifier into a human-readable phrase."""
    if not name:
        return ""
    if "_" in name:
        phrase = _split_snake(name)
    else:
        phrase = _split_camel(name)
    phrase = phrase.strip()
    if not phrase:
        return name
    return phrase[0].upper() + phrase[1:]


def _label_phrase(labels: List[str]) -> str:
    for lbl in labels or []:
        verb = _LABEL_VERBS.get(lbl)
        if verb:
            return verb
    return "Function"


def _domain_phrase(domain: str) -> str:
    if not domain or domain == "root":
        return ""
    hint = _DOMAIN_HINTS.get(domain)
    if hint:
        return hint
    return f"{domain} path"


def _signature_phrase(signature: str) -> str:
    """Pull a short return-type hint out of a C/Python signature."""
    if not signature:
        return ""
    sig = " ".join(signature.split())
    if "(" in sig:
        ret = sig.split("(", 1)[0].strip()
        ret = ret.replace("static", "").replace("inline", "").strip()
        if ret and ret not in ("void",):
            return f"Returns {ret}"
    return ""


def _body_phrases(body_text: str) -> List[str]:
    """Pull lightweight signal phrases out of a function body."""
    phrases: List[str] = []
    if not body_text:
        return phrases
    body = body_text
    if "pthread_mutex_lock" in body or "mtx_lock" in body or "EnterCriticalSection" in body:
        phrases.append("Acquires a lock")
    if "pthread_mutex_unlock" in body or "mtx_unlock" in body or "LeaveCriticalSection" in body:
        phrases.append("Releases a lock")
    if "malloc" in body or "calloc" in body or "kmalloc" in body or "PyMem_Malloc" in body:
        phrases.append("Allocates memory")
    if "free" in body or "kfree" in body or "PyMem_Free" in body:
        phrases.append("Frees memory")
    if "return" in body and "return;" not in body:
        phrases.append("Returns a value to the caller")
    if "fprintf(stderr" in body or "log_" in body or "pr_" in body:
        phrases.append("Emits log output")
    if "fopen" in body or "open(" in body or "read(" in body or "write(" in body:
        phrases.append("Performs file I/O")
    if "socket(" in body or "send(" in body or "recv(" in body or "connect(" in body:
        phrases.append("Performs network I/O")
    if "for (" in body or "for(" in body or "while (" in body or "while(" in body:
        phrases.append("Iterates over a collection")
    if "if (" in body or "if(" in body:
        phrases.append("Branches on a condition")
    return phrases


def generate_heuristic_description(node_data: Dict) -> Dict[str, Any]:
    """Produce rule-based supplements for a node — no LLM required.

    Returns a dict mapping fillable field -> generated value. Only fields
    that are empty in node_data are populated. Fields with no usable signal
    are omitted from the result.

    This is the always-works fallback for builds without an LLM API key.
    """
    out: Dict[str, Any] = {}

    name = node_data.get("name", "")
    fqn = node_data.get("fqn", "") or name
    labels = node_data.get("labels", []) or []
    domain = node_data.get("domain", "") or ""
    signature = node_data.get("signature", "") or ""
    body_text = node_data.get("body_text", "") or ""
    params = node_data.get("params", []) or []
    file_path = node_data.get("source_file", "") or node_data.get("file_path", "") or ""
    line = node_data.get("line", 0) or 0
    is_external = bool(node_data.get("external") or
                       (isinstance(node_data.get("attrs"), dict) and
                        node_data["attrs"].get("external")))

    semantic_desc = node_data.get("semantic_desc", "") or ""
    external_desc = node_data.get("external_desc", "") or ""
    api_constraints = node_data.get("api_constraints", "") or ""
    preconditions = node_data.get("preconditions", []) or []
    postconditions = node_data.get("postconditions", []) or []

    name_phrase = _humanize(name)
    label_phrase = _label_phrase(labels)
    domain_phrase = _domain_phrase(domain)
    sig_phrase = _signature_phrase(signature)
    body_phrases = _body_phrases(body_text)

    if not semantic_desc:
        parts: List[str] = []
        if is_external:
            parts.append(f"External reference to {name_phrase}")
        else:
            parts.append(f"{label_phrase}: {name_phrase}")
        if domain_phrase:
            parts.append(domain_phrase.lower())
        if sig_phrase:
            parts.append(sig_phrase.lower())
        if file_path:
            parts.append(f"defined in {file_path}" + (f":{line}" if line else ""))
        if body_phrases:
            parts.extend(p.lower() for p in body_phrases[:3])
        if len(parts) > 1:
            desc = parts[0] + " — " + ", ".join(parts[1:])
        else:
            desc = parts[0]
        out["semantic_desc"] = desc

    if not external_desc and "API_entry" in labels:
        param_summary = ""
        if params:
            try:
                param_names = [p.get("name", "") if isinstance(p, dict) else str(p)
                               for p in params]
                param_summary = " with parameters: " + ", ".join(
                    n for n in param_names if n)
            except Exception:
                param_summary = ""
        out["external_desc"] = (
            f"Public entry point exposed as {fqn}{param_summary}. "
            f"Invoked by external callers; treat as a stable surface.")

    if not api_constraints:
        constraints: List[str] = []
        if params:
            for p in params:
                try:
                    pname = p.get("name", "") if isinstance(p, dict) else str(p)
                    ptype = p.get("type", "") if isinstance(p, dict) else ""
                except Exception:
                    continue
                if not pname:
                    continue
                if ptype and ptype.endswith("*"):
                    constraints.append(f"{pname} != NULL")
        if constraints:
            out["api_constraints"] = "; ".join(constraints)

    if not preconditions:
        pre: List[str] = []
        if "API_entry" in labels:
            pre.append("Caller holds no internal locks")
        if "thread_processor" in labels:
            pre.append("Worker queue is initialized")
        if "constructor" in labels:
            pre.append("Instance memory is allocated")
        if pre:
            out["preconditions"] = pre

    if not postconditions:
        post: List[str] = []
        if "destructor" in labels:
            post.append("Instance resources are released")
        if "constructor" in labels:
            post.append("Instance is in a usable state")
        if "lock" in domain or any("Acquires a lock" in p for p in body_phrases):
            post.append("Lock is released on return")
        if post:
            out["postconditions"] = post

    return out


def apply_heuristic_enhancement(graph_dir: str, node_id: str,
                                fields: Optional[List[str]] = None,
                                write: bool = True) -> Dict[str, Any]:
    """Generate heuristic supplements for one node and optionally write them.

    Reads the node from the legacy graph (functions table / NetworkX), runs
    generate_heuristic_description, and writes any non-empty fields back
    through the same supplement path used by LLM-driven auto-enhance. The
    supplement source is recorded as 'heuristic' with confidence=INFERRED
    so the engineer can still review or roll back via batch-confirm.

    Returns {"node_id":..., "generated":{...}, "applied":bool, "skipped":...}.
    """
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph

    G = _load_full_graph(graph_dir)
    if node_id not in G:
        return {"node_id": node_id, "generated": {}, "applied": False,
                "skipped": "node not in graph"}

    nd = G.nodes[node_id]
    generated = generate_heuristic_description(nd)
    if fields:
        generated = {k: v for k, v in generated.items() if k in fields}
    if not generated:
        return {"node_id": node_id, "generated": {}, "applied": False,
                "skipped": "no heuristic signal"}

    if not write:
        return {"node_id": node_id, "generated": generated,
                "applied": False, "skipped": ""}

    session = BatchConfirmSession(graph_dir)
    applied_fields: List[str] = []
    for field, value in generated.items():
        item_id = session.add(
            node_id=node_id, field=field, value=value,
            source="heuristic", confidence="INFERRED",
            has_evidence=True, threshold="INFERRED",
        )
        ok = _apply_supplement(graph_dir, session.items[-1])
        if ok:
            session.items[-1].status = "applied"
            applied_fields.append(field)
        else:
            session.items[-1].status = "rejected"
    session.save()
    return {"node_id": node_id, "generated": generated,
            "applied": bool(applied_fields),
            "applied_fields": applied_fields,
            "skipped": ""}


def apply_heuristic_enhancement_batch(graph_dir: str,
                                      limit: int = 500) -> Dict[str, Any]:
    """Efficiently generate + write heuristic descriptions for many nodes.

    Loads the graph once, iterates all function nodes, generates heuristic
    descriptions, and writes them directly to:
      1. The JSON-side supplement store (via _json_update_node) — once at end
         via split_by_domain.
      2. The SQLite cgdb_nodes.description column — incrementally as we go.

    Also processes var/enum/field/ops_table nodes from cgdb_nodes table that
    are not in the legacy graph (which only contains functions).

    Returns {"processed": N, "applied": M, "skipped_no_signal": K,
             "skipped_builtin": B}.
    """
    try:
        from _builder.graph_build import _load_full_graph, split_by_domain
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph, split_by_domain

    G = _load_full_graph(graph_dir)

    # Locate SQLite DB if it coexists with JSON.
    db_path = os.path.join(graph_dir, "code2database.db")
    has_sqlite = os.path.exists(db_path)
    conn = None
    if has_sqlite:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(db_path)

    from datetime import datetime as _datetime
    timestamp = _datetime.now().strftime("%Y-%m-%d %H:%M")
    processed = 0
    applied = 0
    no_signal = 0
    builtin = 0

    try:
        for nid, nd in G.nodes(data=True):
            if nd.get("is_empty", False) or nd.get("node_type") == "file":
                continue
            # Skip auto-created external/builtin callee placeholders — these
            # are not real functions in the project and shouldn't get heuristic
            # descriptions (they would pollute the review checklist with noise
            # like "Function: Append — _builder path").
            attrs = nd.get("attrs", {}) or {}
            if attrs.get("external") is True or nd.get("external") is True:
                builtin += 1
                continue
            name = nd.get("name", "")
            if _is_likely_builtin(name):
                builtin += 1
                continue
            # Skip SQL-string / non-identifier fqns (defensive: stray string
            # literals should never become "function" nodes, but if they slip
            # through, don't generate fake descriptions for them either).
            if name and not re.match(r'^[A-Za-z_][A-Za-z0-9_]*([.:][A-Za-z_][A-Za-z0-9_]*)*$', name):
                builtin += 1
                continue
            fill = compute_fill_request(nd)
            if not fill:
                no_signal += 1
                continue
            processed += 1
            generated = generate_heuristic_description(nd)
            sem = generated.get("semantic_desc")

            # Update JSON-side graph node attrs (will be flushed by split_by_domain).
            meta = nd.get("_supplement_meta", {})
            applied_this = False
            if sem:
                stored_key = "semantic_desc_supplemented"
                meta[stored_key] = {
                    "source": "heuristic",
                    "confidence": "INFERRED",
                    "timestamp": timestamp,
                    "original": nd.get("semantic_desc", ""),
                }
                nd[stored_key] = sem
                applied_this = True
                # Direct SQLite cgdb_nodes update (fast).
                if conn is not None:
                    try:
                        conn.execute(
                            "UPDATE cgdb_nodes SET description=? WHERE fqn=?",
                            (sem, nid))
                    except Exception:
                        pass

            # Also write other heuristic-generated fields (external_desc,
            # api_constraints, preconditions, postconditions) to the node
            # so they're persisted in the JSON side and SQLite attrs.
            for field_name, field_val in generated.items():
                if field_name == "semantic_desc":
                    continue
                if not field_val:
                    continue
                # Don't overwrite a non-empty existing value.
                existing = nd.get(field_name)
                if existing:
                    continue
                nd[field_name] = field_val
                meta[field_name + "_supplemented"] = {
                    "source": "heuristic",
                    "confidence": "INFERRED",
                    "timestamp": timestamp,
                    "original": "",
                }
                applied_this = True

            if applied_this:
                nd["_supplement_meta"] = meta
                applied += 1
            else:
                no_signal += 1
            if applied >= limit:
                break

        # Second pass: process var/enum/field/ops_table nodes from cgdb_nodes
        # table that are NOT in the legacy graph. Generate simple heuristic
        # descriptions based on kind, name, type_spelling, and attrs.
        if conn is not None and applied < limit:
            try:
                rows = conn.execute(
                    "SELECT id, kind, name, fqn, type_spelling, attrs, "
                    "description FROM cgdb_nodes "
                    "WHERE kind IN ('var', 'enum', 'field', 'ops_table') "
                    "AND (description = '' OR description IS NULL)"
                ).fetchall()
                for row in rows:
                    if applied >= limit:
                        break
                    node_id, kind, name, fqn, type_spelling, attrs_json, desc = row
                    if not name or _is_likely_builtin(name):
                        builtin += 1
                        continue
                    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
                        builtin += 1
                        continue
                    # Build a simple heuristic description for var/enum/field
                    import json as _json
                    try:
                        attrs = _json.loads(attrs_json) if attrs_json else {}
                    except Exception:
                        attrs = {}
                    desc_text = ""
                    if kind == 'var':
                        desc_text = f"Global variable: {name}"
                        if type_spelling:
                            desc_text += f" of type {type_spelling}"
                        if attrs.get('is_param'):
                            desc_text = f"Parameter: {name}"
                            if type_spelling:
                                desc_text += f" of type {type_spelling}"
                    elif kind == 'enum':
                        values = attrs.get('values', []) or []
                        desc_text = f"Enum type: {name}"
                        if values:
                            member_names = [v.get('member', '') for v in values if isinstance(v, dict)]
                            member_names = [m for m in member_names if m][:5]
                            if member_names:
                                desc_text += f" with members {', '.join(member_names)}"
                    elif kind == 'field':
                        desc_text = f"Function pointer field: {name}"
                        ops_table = attrs.get('ops_table', '')
                        if ops_table:
                            desc_text += f" in {ops_table}"
                    elif kind == 'ops_table':
                        struct_type = attrs.get('struct_type', '')
                        desc_text = f"Operations table: {name}"
                        if struct_type:
                            desc_text += f" implementing {struct_type}"
                    if desc_text:
                        conn.execute(
                            "UPDATE cgdb_nodes SET description=? WHERE id=?",
                            (desc_text, node_id))
                        applied += 1
                        processed += 1
            except Exception as _e:
                pass
    finally:
        if conn is not None:
            conn.commit()
            conn.close()

    # Flush JSON side via split_by_domain.
    try:
        master_path = os.path.join(graph_dir, "code2database_master.json")
        if os.path.exists(master_path):
            master = json.loads(Path(master_path).read_text(encoding="utf-8"))
            source_root = master.get("source_root", "")
            split_by_domain(G, graph_dir, source_root)
    except Exception:
        pass

    return {
        "processed": processed,
        "applied": applied,
        "skipped_no_signal": no_signal,
        "skipped_builtin": builtin,
    }


# ---------------------------------------------------------------------------
# Confidence-threshold gate — auto-write EXTRACTED, prompt for INFERRED
# ---------------------------------------------------------------------------

# Confidence ordering: higher = more trustworthy
_CONFIDENCE_RANK = {"EXTRACTED": 3, "INFERRED": 2, "AMBIGUOUS": 1}


def should_auto_write(confidence: str, has_evidence: bool = True,
                      threshold: str = "EXTRACTED") -> Tuple[bool, str]:
    """Decide whether a supplement should be auto-written (no confirmation).

    Returns (auto_write, reason).

    Rules:
    - EXTRACTED with evidence → auto-write (high confidence, source-backed)
    - EXTRACTED without evidence → prompt (claim without proof)
    - INFERRED → prompt unless threshold is INFERRED or lower
    - AMBIGUOUS → reject (unless --allow-ambiguous)
    """
    conf_rank = _CONFIDENCE_RANK.get(confidence, 0)
    threshold_rank = _CONFIDENCE_RANK.get(threshold, 0)

    if confidence == "AMBIGUOUS":
        return False, "AMBIGUOUS rejected (use --allow-ambiguous to override)"
    if confidence == "EXTRACTED":
        if has_evidence:
            return True, "EXTRACTED with evidence — auto-written"
        return False, "EXTRACTED but no evidence — prompt required"
    if confidence == "INFERRED":
        if threshold_rank <= _CONFIDENCE_RANK["INFERRED"]:
            return True, f"INFERRED, threshold is {threshold} — auto-written"
        return False, "INFERRED — prompt required (threshold is EXTRACTED)"
    return False, f"Unknown confidence '{confidence}' — prompt required"


# ---------------------------------------------------------------------------
# Batch confirm session
# ---------------------------------------------------------------------------

BATCH_SESSION_NAME = ".code2database_batch_session.json"


@dataclass
class BatchItem:
    """One pending supplement in a batch confirm session."""
    node_id: str
    field: str
    value: Any
    source: str = "llm_supplement"
    confidence: str = "INFERRED"
    has_evidence: bool = False
    auto_writable: bool = False
    reason: str = ""
    status: str = "pending"  # pending / accepted / rejected / applied
    id: int = 0


class BatchConfirmSession:
    """Collects supplements and lets the user accept/reject in bulk.

    Lifecycle:
        session = BatchConfirmSession(graph_dir)
        session.add(node_id, "semantic_desc", "...", confidence="EXTRACTED")
        session.add(node_id, "preconditions", [...], confidence="INFERRED")
        # Auto-write the EXTRACTED ones; leave INFERRED for batch-confirm
        session.save()
        # Later, the user runs batch-confirm --accept-all or interactive
    """

    def __init__(self, graph_dir: str):
        self.graph_dir = graph_dir
        self.session_path = os.path.join(graph_dir, BATCH_SESSION_NAME)
        self.items: List[BatchItem] = []
        self._next_id = 1
        self._load()

    def _load(self):
        if not os.path.exists(self.session_path):
            return
        try:
            data = json.loads(Path(self.session_path).read_text(encoding="utf-8"))
            for item_data in data.get("items", []):
                item = BatchItem(**item_data)
                self.items.append(item)
                if item.id >= self._next_id:
                    self._next_id = item.id + 1
        except (json.JSONDecodeError, TypeError):
            pass

    def save(self):
        data = {
            "items": [asdict(i) for i in self.items],
            "updated_at": time.time(),
        }
        Path(self.session_path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

    def add(self, node_id: str, field: str, value: Any,
            source: str = "llm_supplement", confidence: str = "INFERRED",
            has_evidence: bool = False, threshold: str = "EXTRACTED") -> int:
        """Add a pending supplement. Returns the item ID."""
        auto, reason = should_auto_write(confidence, has_evidence, threshold)
        item = BatchItem(
            node_id=node_id, field=field, value=value,
            source=source, confidence=confidence,
            has_evidence=has_evidence, auto_writable=auto, reason=reason,
            id=self._next_id,
        )
        self.items.append(item)
        self._next_id += 1
        return item.id

    def pending_items(self) -> List[BatchItem]:
        """Items that haven't been applied yet (pending or accepted)."""
        return [i for i in self.items if i.status in ("pending", "accepted")]

    def apply_auto_writable(self) -> Dict:
        """Auto-apply all items where auto_writable is True. Returns counts."""
        applied = 0
        skipped = 0
        for item in self.items:
            if item.status != "pending":
                continue
            if not item.auto_writable:
                skipped += 1
                continue
            ok = _apply_supplement(self.graph_dir, item)
            if ok:
                item.status = "applied"
                applied += 1
            else:
                item.status = "rejected"
        self.save()
        return {"applied": applied, "skipped": skipped,
                "remaining": len(self.pending_items())}

    def accept_all(self) -> int:
        """Mark all pending items as accepted (will be applied on next apply)."""
        n = 0
        for item in self.items:
            if item.status == "pending":
                item.status = "accepted"
                n += 1
        self.save()
        return n

    def reject_all(self) -> int:
        """Mark all pending items as rejected."""
        n = 0
        for item in self.items:
            if item.status == "pending":
                item.status = "rejected"
                n += 1
        self.save()
        return n

    def accept(self, item_ids: List[int]) -> int:
        """Accept specific items by ID."""
        n = 0
        for item in self.items:
            if item.id in item_ids and item.status == "pending":
                item.status = "accepted"
                n += 1
        self.save()
        return n

    def reject(self, item_ids: List[int]) -> int:
        """Reject specific items by ID."""
        n = 0
        for item in self.items:
            if item.id in item_ids and item.status == "pending":
                item.status = "rejected"
                n += 1
        self.save()
        return n

    def apply_accepted(self) -> Dict:
        """Apply all items with status='accepted'."""
        applied = 0
        failed = 0
        for item in self.items:
            if item.status != "accepted":
                continue
            ok = _apply_supplement(self.graph_dir, item)
            if ok:
                item.status = "applied"
                applied += 1
            else:
                failed += 1
        self.save()
        return {"applied": applied, "failed": failed,
                "remaining": len(self.pending_items())}

    def clear_applied(self):
        """Remove applied/rejected items from the session."""
        self.items = [i for i in self.items if i.status in ("pending", "accepted")]
        self.save()


def _apply_supplement(graph_dir: str, item: BatchItem) -> bool:
    """Apply a single supplement to the graph (with rollback logging)."""
    try:
        from _builder.update_cmd import (
            _sqlite_update_node, _json_update_node,
            _sqlite_get_node_extra,
            _detect_backend,
        )
        backend = _detect_backend(graph_dir)

        # Capture old value for rollback
        if backend == "sqlite":
            old_extra = _sqlite_get_node_extra(graph_dir, item.node_id)
            old_value = old_extra.get(item.field)
        else:
            from _builder.graph_build import _load_full_graph
            G = _load_full_graph(graph_dir)
            old_value = G.nodes[item.node_id].get(item.field) if item.node_id in G else None

        attrs = {item.field: item.value}
        if backend == "sqlite":
            ok = _sqlite_update_node(graph_dir, item.node_id, attrs,
                                     source=item.source, confidence=item.confidence)
        else:
            ok = _json_update_node(graph_dir, item.node_id, attrs,
                                   source=item.source, confidence=item.confidence)

        if ok:
            # Log for rollback
            _append_rollback_entry(graph_dir, {
                "type": "node",
                "node_id": item.node_id,
                "field": item.field,
                "old_value": old_value,
                "new_value": item.value,
                "source": item.source,
                "confidence": item.confidence,
            })
            # Audit log: trace operator/command-driven edit
            try:
                from _builder.audit_log import log_audit
                log_audit(graph_dir,
                          command="auto-enhance",
                          target_kind="node",
                          target_id=item.node_id,
                          action="apply",
                          attribute=item.field,
                          before_value=old_value,
                          after_value=item.value,
                          reason=f"llm_supplement (confidence={item.confidence})")
            except Exception:
                pass
            # Invalidate query cache entries that touched this node so the
            # next describe-node / explore-flow sees the supplemented value.
            try:
                from _builder.query_cache import invalidate_node
                invalidate_node(graph_dir, item.node_id)
            except Exception:
                pass
        return bool(ok)
    except Exception as exc:
        print(f"Apply failed for {item.node_id}.{item.field}: {exc}",
              file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _parse_attr_assignments(attr_strs: List[str]) -> Dict[str, Any]:
    """Parse 'key=value' assignments, auto-detecting JSON values."""
    out = {}
    for s in attr_strs or []:
        if "=" not in s:
            raise ValueError(f"expected key=value, got {s!r}")
        k, v = s.split("=", 1)
        k = k.strip()
        # Try JSON parse for lists/dicts/numbers/bools
        try:
            v_parsed = json.loads(v)
        except json.JSONDecodeError:
            v_parsed = v
        out[k] = v_parsed
    return out


def cmd_auto_enhance(args):
    """Auto-enhance a node with LLM-supplied attributes.

    Confidence-threshold auto-write: EXTRACTED+evidence writes automatically,
    INFERRED prompts, AMBIGUOUS rejects. All writes are logged for rollback.

    Usage:
        auto-enhance --graph <dir> --node <id> --attr 'semantic_desc=...' \
                     --confidence EXTRACTED --evidence "source line 42"
        auto-enhance --graph <dir> --batch  # process pending batch session
    """
    graph_dir = args.graph

    if getattr(args, "batch", False):
        # Process the batch session: auto-write EXTRACTED, leave rest for review
        session = BatchConfirmSession(graph_dir)
        result = session.apply_auto_writable()
        print(json.dumps({
            "auto_applied": result["applied"],
            "skipped_needs_confirm": result["skipped"],
            "remaining_for_review": result["remaining"],
        }, ensure_ascii=False, indent=2))
        return

    # Single-supplement mode
    node_hint = args.node
    confidence = getattr(args, "confidence", "INFERRED")
    evidence = getattr(args, "evidence", "")
    threshold = getattr(args, "threshold", "EXTRACTED")
    allow_ambiguous = getattr(args, "allow_ambiguous", False)

    try:
        attrs = _parse_attr_assignments(args.attr)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not attrs:
        print("Error: at least one --attr key=value is required", file=sys.stderr)
        sys.exit(1)

    # Resolve node ID
    from _builder.utils import _find_node_id
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        print(f"Node not found: {node_hint}", file=sys.stderr)
        sys.exit(1)

    # Apply each attribute
    session = BatchConfirmSession(graph_dir)
    results = []
    for field, value in attrs.items():
        has_evidence = bool(evidence)
        auto, reason = should_auto_write(
            confidence, has_evidence, threshold)
        if confidence == "AMBIGUOUS" and not allow_ambiguous:
            results.append({"field": field, "action": "rejected", "reason": reason})
            continue

        item_id = session.add(
            node_id=node_id, field=field, value=value,
            source="llm_supplement", confidence=confidence,
            has_evidence=has_evidence, threshold=threshold,
        )

        if auto:
            # Apply immediately
            ok = _apply_supplement(graph_dir, session.items[-1])
            if ok:
                session.items[-1].status = "applied"
                results.append({"field": field, "action": "auto_written",
                                "reason": reason, "item_id": item_id})
            else:
                session.items[-1].status = "rejected"
                results.append({"field": field, "action": "failed",
                                "reason": "apply error", "item_id": item_id})
        else:
            results.append({"field": field, "action": "pending_confirmation",
                            "reason": reason, "item_id": item_id})

    session.save()
    print(json.dumps({
        "node": node_id,
        "results": results,
        "next_step": "Run 'batch-confirm --list' to review pending items, "
                     "or 'batch-confirm --accept-all' to confirm all.",
    }, ensure_ascii=False, indent=2, default=str))


def cmd_batch_confirm(args):
    """Batch-confirm pending supplements.

    Usage:
        batch-confirm --graph <dir> --list           # list pending items
        batch-confirm --graph <dir> --accept-all     # accept all pending
        batch-confirm --graph <dir> --reject-all      # reject all pending
        batch-confirm --graph <dir> --accept 1,2,3    # accept specific IDs
        batch-confirm --graph <dir> --reject 4,5      # reject specific IDs
        batch-confirm --graph <dir> --apply           # apply all accepted
    """
    graph_dir = args.graph
    session = BatchConfirmSession(graph_dir)

    if getattr(args, "list", False):
        items = [asdict(i) for i in session.items if i.status != "applied"]
        print(json.dumps({
            "pending_count": len([i for i in session.items if i.status == "pending"]),
            "accepted_count": len([i for i in session.items if i.status == "accepted"]),
            "items": items,
        }, ensure_ascii=False, indent=2, default=str))
        return

    if getattr(args, "accept_all", False):
        n = session.accept_all()
        print(f"Accepted {n} pending items")
        return

    if getattr(args, "reject_all", False):
        n = session.reject_all()
        print(f"Rejected {n} pending items")
        session.clear_applied()
        return

    accept_ids = getattr(args, "accept", "")
    if accept_ids:
        ids = [int(x.strip()) for x in accept_ids.split(",") if x.strip().isdigit()]
        n = session.accept(ids)
        print(f"Accepted {n} items by ID")
        return

    reject_ids = getattr(args, "reject", "")
    if reject_ids:
        ids = [int(x.strip()) for x in reject_ids.split(",") if x.strip().isdigit()]
        n = session.reject(ids)
        print(f"Rejected {n} items by ID")
        return

    if getattr(args, "apply", False):
        result = session.apply_accepted()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        session.clear_applied()
        return

    print("Specify --list, --accept-all, --reject-all, --accept IDs, "
          "--reject IDs, or --apply", file=sys.stderr)
    sys.exit(1)


def cmd_rollback(args):
    """Rollback supplement writes.

    Usage:
        rollback --graph <dir> --list                # list recent writes
        rollback --graph <dir> --to <entry_id>       # revert a specific write
        rollback --graph <dir> --last                # revert the most recent write
    """
    graph_dir = args.graph

    if getattr(args, "list", False):
        limit = getattr(args, "limit", 50)
        entries = list_rollback_entries(graph_dir, limit=limit)
        print(json.dumps({
            "entries": entries,
            "count": len(entries),
        }, ensure_ascii=False, indent=2, default=str))
        return

    if getattr(args, "last", False):
        entries = list_rollback_entries(graph_dir, limit=1)
        if not entries:
            print("No rollback entries", file=sys.stderr)
            sys.exit(1)
        result = rollback_to_entry(graph_dir, entries[0]["id"])
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    entry_id = getattr(args, "to", 0)
    if entry_id:
        result = rollback_to_entry(graph_dir, int(entry_id))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    print("Specify --list, --to <id>, or --last", file=sys.stderr)
    sys.exit(1)


def cmd_fill_request(args):
    """Compute the auto-fill request for a node — what fields are empty
    and should be LLM-filled.

    Usage:
        fill-request --graph <dir> --node <id>
        fill-request --graph <dir> --all --limit 100   # find nodes needing fill
    """
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    if getattr(args, "all", False):
        # Find nodes with empty fillable fields
        limit = getattr(args, "limit", 100)
        results = []
        for nid, nd in G.nodes(data=True):
            if nd.get("is_empty", False) or nd.get("node_type") == "file":
                continue
            fill = compute_fill_request(nd)
            if fill:
                results.append({
                    "node_id": nid,
                    "name": nd.get("name", ""),
                    "needs_fill": [f["field"] for f in fill],
                })
            if len(results) >= limit:
                break
        print(json.dumps({
            "nodes_needing_fill": results,
            "count": len(results),
        }, ensure_ascii=False, indent=2, default=str))
        return

    node_hint = getattr(args, "node", "")
    if not node_hint:
        print("Specify --node or --all", file=sys.stderr)
        sys.exit(1)
    from _builder.utils import _find_node_id
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        print(f"Node not found: {node_hint}", file=sys.stderr)
        sys.exit(1)
    nd = G.nodes[node_id]
    fill = compute_fill_request(nd)
    print(json.dumps({
        "node_id": node_id,
        "name": nd.get("name", ""),
        "fill_request": fill,
        "fillable_count": len(fill),
    }, ensure_ascii=False, indent=2, default=str))


def cmd_heuristic_enhance(args):
    """Generate heuristic supplements for empty fields — no LLM required.

    Derives semantic_desc / external_desc / api_constraints / preconditions /
    postconditions from node attributes (name, labels, domain, signature,
    body_text). Writes through the same supplement path as LLM auto-enhance
    so the results show up in describe-node and can be rolled back.

    Usage:
        heuristic-enhance --graph <dir> --node <id>            # one node, write
        heuristic-enhance --graph <dir> --node <id> --dry-run  # preview only
        heuristic-enhance --graph <dir> --all --limit 100      # batch
    """
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph

    dry_run = bool(getattr(args, "dry_run", False))
    fields_filter = getattr(args, "fields", None)
    if fields_filter:
        fields_filter = [f.strip() for f in fields_filter.split(",") if f.strip()]

    if getattr(args, "all", False):
        limit = int(getattr(args, "limit", 100))
        if dry_run:
            # Dry-run mode: don't write, just preview.
            G = _load_full_graph(graph_dir)
            results = []
            applied_count = 0
            for nid, nd in G.nodes(data=True):
                if nd.get("is_empty", False) or nd.get("node_type") == "file":
                    continue
                name = nd.get("name", "")
                if _is_likely_builtin(name):
                    continue
                fill = compute_fill_request(nd)
                if not fill:
                    continue
                res = apply_heuristic_enhancement(
                    graph_dir, nid,
                    fields=fields_filter,
                    write=False,
                )
                if res.get("generated"):
                    results.append({
                        "node_id": nid,
                        "name": name,
                        "fields": list(res["generated"].keys()),
                        "applied": False,
                    })
                if len(results) >= limit:
                    break
            print(json.dumps({
                "mode": "dry-run",
                "processed": len(results),
                "applied": 0,
                "results": results,
            }, ensure_ascii=False, indent=2, default=str))
            return
        # Write mode: use the efficient batch function.
        summary = apply_heuristic_enhancement_batch(graph_dir, limit=limit)
        # Regenerate REVIEW_CHECKLIST.md and CODE2DATABASE_SUMMARY.md so the
        # heuristic-filled nodes appear in the review report and coverage
        # stats reflect the new state immediately (without a separate rebuild).
        try:
            from _builder.graph_build import _load_full_graph
            from _builder.index_pack import _write_review_checklist
            G = _load_full_graph(graph_dir)
            _write_review_checklist(graph_dir, G)
        except Exception:
            pass
        try:
            import os as _os
            db_path = _os.path.join(graph_dir, "code2database.db")
            if _os.path.exists(db_path):
                from _builder.sqlite_postprocess import (
                    _build_callgraph_summary_md_from_sqlite,
                )
                master_path = _os.path.join(graph_dir, "code2database_master.json")
                source_root_for_summary = ""
                if _os.path.exists(master_path):
                    import json as _json
                    with open(master_path, "r", encoding="utf-8") as _mf:
                        _master = _json.load(_mf)
                        source_root_for_summary = _master.get("source_root", "")
                _build_callgraph_summary_md_from_sqlite(
                    db_path, graph_dir,
                    source_root=source_root_for_summary,
                    build_info=None,
                )
        except Exception:
            pass
        print(json.dumps({
            "mode": "write",
            **summary,
        }, ensure_ascii=False, indent=2, default=str))
        return

    node_hint = getattr(args, "node", "")
    if not node_hint:
        print("Specify --node or --all", file=sys.stderr)
        sys.exit(1)
    from _builder.utils import _find_node_id
    G = _load_full_graph(graph_dir)
    node_id = _find_node_id(G, node_hint)
    if not node_id:
        print(f"Node not found: {node_hint}", file=sys.stderr)
        sys.exit(1)
    res = apply_heuristic_enhancement(
        graph_dir, node_id,
        fields=fields_filter,
        write=not dry_run,
    )
    print(json.dumps({
        "mode": "dry-run" if dry_run else "write",
        **res,
    }, ensure_ascii=False, indent=2, default=str))
