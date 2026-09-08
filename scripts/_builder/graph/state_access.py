"""callgraph builder module: state_access — split from graph_build.py."""

import logging
import os
import json
import sys
import re
import time
from pathlib import Path
from collections import defaultdict, Counter
import networkx as nx
from _builder.graph.streaming_graph import StreamingGraph
from _builder.utils import _resolve_invoked_id
import _builder.utils as _utils


_FIELD_ACCESS_RE = re.compile(
    r'(\b[A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*)\b'
)

_FIELD_WRITE_RE = re.compile(
    r'(\b[A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*)\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*([^;,\n]{1,80})'
)

def _extract_module_hint(var_name: str, struct_type: str = "",
                          source_file: str = "") -> str:
    """Extract module hint from a vtable registration variable name.

    Uses generic heuristics to derive a module name from the variable name
    used in a struct initializer (e.g., g_nvme_fn_table → nvme).
    Falls back to source_file directory path if var_name yields no hint.

    Args:
        var_name: Variable name from struct initializer (e.g., g_sw_module)
        struct_type: Struct type name (e.g., file_operations)
        source_file: Source file path (e.g., lib/nvme/nvme.c)

    Returns:
        Module hint string (e.g., "nvme") or empty string if no hint found.
    """
    module_hint = ""
    if "_fn_table" in var_name:
        prefix = var_name.replace("_fn_table", "").replace("lib", "")
        if prefix:
            module_hint = prefix.lstrip("g_").rstrip("_")
    elif var_name.startswith("g_") and var_name.endswith("_module"):
        mid = var_name[2:-7]
        if mid:
            module_hint = mid
    elif var_name.startswith("g_"):
        mid = var_name[2:]
        if mid and len(mid) > 1:
            module_hint = mid
    elif var_name == "fn_table" or var_name.startswith("fn_table_"):
        # Bare fn_table — no hint from var_name; try source_file
        pass
    elif var_name.startswith("g") and len(var_name) > 3 and not var_name[1:2].isupper():
        # g-prefix without underscore: gscheduler → scheduler
        # But avoid stripping 'g' from real words like 'governor'
        _G_PREFIX_WORDS = frozenset({
            'governor', 'get', 'given', 'global', 'group', 'grant',
            'grow', 'guide', 'guard', 'guess', 'guest',
        })
        if var_name not in _G_PREFIX_WORDS:
            mid = var_name[1:]
            if mid and len(mid) > 2:
                module_hint = mid
    else:
        # Try suffix patterns where the PREFIX encodes the module name.
        # Order matters: longer/more-specific suffixes first.
        _SUFFIX_PATTERNS = [
            ("_governor", 9),   # e.g., xxx_governor → xxx
            ("_fn_table", 9),   # nvme_fn_table → nvme
            ("_module", 7),     # bdev_module → bdev, accel_module → accel
            ("_bdev", 5),       # base_bdev → base
            ("_impl", 5),       # net_impl → net
            ("_ops", 4),        # md_ops → md, modern_ops → modern
            ("_if", 3),         # aio_if → aio, compress_if → compress
            ("_dev", 4),        # bs_dev → bs, backing_dev → backing
        ]
        matched = False
        for suffix, slen in _SUFFIX_PATTERNS:
            if var_name.endswith(suffix):
                mid = var_name[:-slen]
                if mid and len(mid) > 1:
                    module_hint = mid
                matched = True
                break
        if not matched:
            # No suffix matched — try underscore split
            if "_" in var_name and not var_name.startswith("_"):
                parts = var_name.rsplit("_", 1)
                if len(parts) == 2 and parts[1] and len(parts[1]) > 1:
                    # If the suffix is generic, use the prefix instead
                    _GENERIC_SUFFIXES = frozenset({
                        'ctx', 'req', 'args', 'data', 'entry', 'obj',
                        'handle', 'ptr', 'buf', 'cfg', 'dev', 'impl',
                        'desc', 'cb', 'fn', 'info', 'ops',
                    })
                    if parts[1] in _GENERIC_SUFFIXES and parts[0] and len(parts[0]) > 1:
                        module_hint = parts[0]
                    else:
                        module_hint = parts[1]
            elif var_name and len(var_name) > 2:
                if struct_type and struct_type.endswith("_" + var_name):
                    module_hint = "static"
                else:
                    module_hint = var_name

    # Filter out generic module hints that provide no useful dispatch narrowing.
    # These are common variable names that don't identify a specific module.
    _GENERIC_HINTS = frozenset({
        'ops', 'op', 'ctx', 'req', 'args', 'data', 'entry', 'obj',
        'handle', 'ptr', 'buf', 'result', 'ret', 'base', 'dev',
        'module', 'impl', 'desc', 'table', 'fn', 'cb', 'config',
        'state', 'info', 'param', 'params', 'opts', 'cfg',
    })

    # Fallback: derive module hint from source_file path
    if not module_hint and source_file:
        # Extract the directory name containing the source file
        # e.g., "lib/subsystem/pci_device.c" → "pci_device"
        # e.g., "module/scheduler/governor/governor.c" → "governor"
        # e.g., "lib/base/base_impl.c" → "base_impl" (dirname="base" is generic)
        parts = source_file.replace("\\", "/").split("/")
        if len(parts) >= 2:
            # Use the immediate parent directory name
            dirname = parts[-2] if len(parts) >= 2 else ""
            basename = os.path.splitext(parts[-1])[0]
            # Prefer dirname unless it's generic, then fall back to basename
            if dirname and len(dirname) > 1 and dirname not in _GENERIC_HINTS:
                module_hint = dirname
            elif basename and len(basename) > 1:
                module_hint = basename
            elif dirname and len(dirname) > 1:
                module_hint = dirname

    if module_hint in _GENERIC_HINTS:
        module_hint = ""

    return module_hint


# Module-level compiled regexes for _extract_state_access. Compiled once at
# import time instead of per-call (called per-node during build → re-compiling
# on a 35K-node graph wastes ~3-5s).
_SA_WORD_RE = re.compile(r'\b[A-Za-z_]\w*\b')
_SA_GLOBAL_PREFIX_RE = re.compile(r'\b(g_[A-Za-z_]\w*|g[A-Z][A-Za-z0-9_]*)\b')
_SA_ANY_WRITE_RE = re.compile(
    r'\b([A-Za-z_]\w*)\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*[^=]'
)


_GUARD_KW_RE = re.compile(r'\b(if|switch|else\s+if)\s*\(')


# Match `<obj_name> = <source_expr>` where source_expr is a
# field-chain (e.g., `jh->bh`, `mapping->private_list`) or function call.
# Used by _trace_object_origin to find where a struct pointer variable was
# initialized, so we can distinguish buffer_head objects from different
# address_spaces — the key signal from KASAN_FINAL_REPORT that proves
# journal_unmap_buffer's bh is a different object from the reader's bh.
_OBJ_ASSIGN_RE = re.compile(
    r'\b([A-Za-z_]\w*)\s*=\s*'
    r'((?:[A-Za-z_]\w*\s*(?:->|\.)\s*)+[A-Za-z_]\w*'  # field chain: a->b->c
    r'|[A-Za-z_]\w*\s*\([^)]*\))'  # or function call: foo(...)
)

# Module-level map of allocation_function_name -> object_type.
# Populated by build_call_graph_from_extraction from the project profile's
# `allocation_sites` list. Read by _trace_object_origin (as fallback when the
# caller doesn't pass `allocation_sites` explicitly) so that field-access /
# field-flow can annotate writer/reader entries with object_origin_type without
# threading the profile through every _extract_state_access call site.
_ALLOCATION_SITES_MAP: dict = {}



def _trace_object_origin(body_text: str, obj_name: str, max_depth: int = 3,
                          allocation_sites: list = None,
                          _cached_assignments: list = None) -> str:
    """Trace where `obj_name` was initialized — backward through assignments.

    For a field access like `bh->b_bdev`, the variable `bh` may
    itself be assigned from a field chain (e.g., `bh = jh->bh`). Following
    this chain gives us the object's "origin" — useful for distinguishing
    buffer_head objects from different address_spaces.

    When `allocation_sites` is provided (list of profile
    entries with `function` and `object_type`), OR when the module-level
    `_ALLOCATION_SITES_MAP` has been populated by `build_call_graph_from_extraction`,
    the trace inspects function call sources. If the source is a call to a
    declared allocation function, the returned origin is annotated with the
    object type as `"<func_name>(...):<object_type>"` (e.g.,
    `"alloc_buffer_head(...):buffer_head"`). This lets field-access /
    field-flow consumers distinguish same-typed-different-instance objects
    without needing full type-flow analysis.

    When `_cached_assignments` is provided (list of _OBJ_ASSIGN_RE matches),
    uses it instead of calling finditer on body_text again. This is critical
    for performance: _extract_state_access calls _trace_object_origin for
    each field read/write — without caching, each call does 3 finditer
    scans of the full body_text. With 3 fields per function × 1.5M
    functions = 13.5B character scans. With caching: 1 finditer per
    function, then O(N_matches) iteration per field.

    Returns the source expression (e.g., "jh->bh" or "mapping->private_list"
    or "alloc_buffer_head(...):buffer_head"), or "" if no assignment is found
    within max_depth hops.
    """
    if not obj_name or max_depth <= 0:
        return ""
    # Build a lookup of allocation function name → object_type for fast match.
    # Priority: explicit arg > module-level global (set by build_call_graph_from_extraction).
    alloc_map = {}
    if allocation_sites:
        for entry in allocation_sites:
            fn = entry.get("function", "")
            ot = entry.get("object_type", "")
            if fn and ot:
                alloc_map[fn] = ot
    else:
        alloc_map = _ALLOCATION_SITES_MAP
    seen = {obj_name}
    current = obj_name
    last_source = ""
    for _ in range(max_depth):
        # Find the last assignment to `current` in body_text.
        # Use cached assignments if provided (avoids re-scanning body_text
        # for every field access — _extract_state_access caches once per
        # function and passes the list to all field lookups).
        last_match = None
        if _cached_assignments is not None:
            for m in _cached_assignments:
                if m.group(1) == current:
                    last_match = m
        else:
            for m in _OBJ_ASSIGN_RE.finditer(body_text):
                if m.group(1) == current:
                    last_match = m
        if not last_match:
            break
        source = last_match.group(2).strip()
        last_source = source
        # If source is a field chain, extract the new head variable
        # e.g., "jh->bh" → head = "jh"
        head_match = re.match(r'([A-Za-z_]\w*)\s*(?:->|\.)', source)
        if head_match:
            head = head_match.group(1)
            if head in seen:
                return source  # cycle — return what we have
            seen.add(head)
            current = head
            continue
        else:
            # Source is a function call or terminal.
            # if it's a call to a profile-declared
            # allocation function, annotate with object_type.
            if alloc_map:
                call_match = re.match(r'([A-Za-z_]\w*)\s*\(', source)
                if call_match and call_match.group(1) in alloc_map:
                    obj_type = alloc_map[call_match.group(1)]
                    return f"{source}:{obj_type}"
            return source
    return last_source



def _find_enclosing_guard(body_text: str, write_pos: int) -> str:
    """Find the nearest enclosing if/switch guard condition for a field write at write_pos.

    Walks body_text forward tracking brace depth and a stack of
    (depth_at_block_entry, condition) for each if/switch block. Returns the
    condition of the innermost block whose range contains write_pos, or "" if
    the write is not inside any guarded block.

    Used by null-pointer-deref analysis to surface the guard that protects a
    NULL writer — e.g., `if (!sb_is_blkdev_sb(sb)) { bh->b_bdev = NULL; }` →
    guard_condition = "!sb_is_blkdev_sb(sb)" → reachable_in_scene = "guarded".
    This is the key piece that distinguishes a real bug from a false positive:
    writers guarded by !sb_is_blkdev_sb() are unreachable during ext4 mount.

    Limitations: only handles braced if/switch blocks (not single-statement
    forms). `else` clauses are not handled separately — the if's condition is
    returned for both branches, which is conservative (the agent can infer
    that an `else` branch implies the negation).
    """
    if write_pos >= len(body_text):
        return ""
    stack = []  # list of (depth_inside_block, condition_text)
    depth = 0
    i = 0
    n = len(body_text)
    while i < n and i < write_pos:
        ch = body_text[i]
        if ch == '{':
            depth += 1
            i += 1
            continue
        if ch == '}':
            depth -= 1
            while stack and stack[-1][0] > depth:
                stack.pop()
            i += 1
            continue
        m = _GUARD_KW_RE.match(body_text, i)
        if m:
            j = m.end()  # position just after '('
            paren_depth = 1
            k = j
            while k < n and paren_depth > 0:
                if body_text[k] == '(':
                    paren_depth += 1
                elif body_text[k] == ')':
                    paren_depth -= 1
                k += 1
            if paren_depth != 0:
                i += 1
                continue
            condition = body_text[j:k - 1].strip()
            p = k
            while p < n and body_text[p] in ' \t\n\r':
                p += 1
            if p < n and body_text[p] == '{':
                stack.append((depth + 1, condition))
                i = p
                continue
            i = k
            continue
        i += 1
    return stack[-1][1] if stack else ""



def _extract_state_access(body_text: str, local_vars: list, params: list,
                          globals_data: dict, field_assignments: list,
                          node_name: str = "",
                          _cached_globals: dict = None) -> dict:
    """Extract global variable and struct field read/write information from body_text.

    Scans function body text for patterns indicating access to global variables
    and struct fields, filtering out local variables and parameters to reduce
    false positives.

    Args:
        body_text: Function body text to scan.
        local_vars: List of local variable dicts (each has 'name' key).
        params: List of parameter dicts (each has 'name' key).
        globals_data: Globals dict from extraction (has 'global_vars', 'enums', 'constants').
        field_assignments: List of field_assignment dicts from extraction.
        node_name: Function name (used to match field_assignments by caller).
        _cached_globals: Optional pre-computed ``{"var_names": {name: info},
            "assign_ops_re": compiled_regex, "var_names_keys": set}`` dict
            built once per build by the caller. Avoids re-compiling the
            per-build assignment-ops regex on every node.

    Returns:
        Dict with keys: globals_read, globals_written, fields_read, fields_written.
        Each value is a list of dicts describing the access.
    """
    if not body_text:
        return {"globals_read": [], "globals_written": [],
                "fields_read": [], "fields_written": []}

    # Build set of local/param names to exclude from global detection
    local_names = set()
    for lv in local_vars:
        name = lv.get("name", "")
        if name:
            local_names.add(name)
    for p in params:
        name = p.get("name", "")
        if name:
            local_names.add(name)

    # --- Global variable access ---
    if _cached_globals is not None:
        # Per-build cache hit: skip the dict-build and regex-compile work.
        # We still need to filter out locals/params that happen to share a
        # name with a global (rare but possible in C with shadowing).
        global_var_names_full = _cached_globals["var_names"]
        global_var_names_keys = _cached_globals["var_names_keys"]
        _ASSIGN_OPS = _cached_globals["assign_ops_re"]
        # Filter: remove names that are shadowed by a local/param this call.
        if local_names:
            shadowed = local_names & global_var_names_keys
            if shadowed:
                # Don't recompile the massive regex! Instead, use the
                # pre-compiled _ASSIGN_OPS and filter out shadowed names
                # from the match results. This changes the cost from
                # O(N_shadowed * re.compile(30K_branches)) — ~30s per
                # call × 7500 calls = ~62 hours — to O(N_matches * 1
                # dict lookup) — milliseconds per call.
                #
                # The pre-compiled regex will match shadowed names too,
                # but we skip them in the write-detection loop below.
                global_var_names = {k: v for k, v in global_var_names_full.items()
                                    if k not in shadowed}
                # Keep _ASSIGN_OPS as the pre-compiled version; the
                # write-detection loop checks `if vname in global_var_names`
                # which automatically excludes shadowed names.
            else:
                global_var_names = global_var_names_full
        else:
            global_var_names = global_var_names_full
    else:
        # No pre-built cache: build the name map from scratch.
        # _ASSIGN_OPS must be initialized here too — the branch above sets
        # it from the cache; without this, line ~671 raises
        # UnboundLocalError on the uncached path (all split-extraction
        # / --low-memory callers pass _cached_globals=None).
        _ASSIGN_OPS = None
        global_vars_list = globals_data.get("global_vars", [])
        global_var_names = {}  # name → info dict
        for gv in global_vars_list:
            gname = gv.get("name", "")
            if gname and gname not in local_names:
                global_var_names[gname] = gv

    globals_read = []
    globals_written = []

    # Detect writes: identifier on LHS of assignment
    # Patterns: "var =", "var +=", "var -=", "var *=", "var /=", "var |=",
    #           "var &=", "var ^=", "var %=", "var <<=", "var >>="
    if global_var_names:
        if _ASSIGN_OPS is None:
            _ASSIGN_OPS = re.compile(
                r'\b(' + '|'.join(re.escape(gn) for gn in sorted(global_var_names.keys(),
                                                                  key=len, reverse=True))
                + r')\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*[^=]'
            )
        written_names = set()
        for m in _ASSIGN_OPS.finditer(body_text):
            vname = m.group(1)
            if vname in global_var_names:
                written_names.add(vname)
    else:
        written_names = set()

    # Detect reads: identifier appearing anywhere not on LHS of assignment.
    # Use a single tokenizer pass over body_text to find all word-boundary
    # identifiers, then intersect with global_var_names. This avoids
    # re.search/re.findall per global name (which is O(N_globals * len(body))).
    if global_var_names:
        all_tokens = set(_SA_WORD_RE.findall(body_text))
        global_tokens_in_body = all_tokens & set(global_var_names.keys())

        for gname, ginfo in global_var_names.items():
            if gname not in global_tokens_in_body:
                continue
            if gname in written_names:
                globals_read.append({"name": gname, "type": ginfo.get("type", ""),
                                     "source_file": ginfo.get("source_file", "")})
                globals_written.append({"name": gname, "type": ginfo.get("type", ""),
                                        "source_file": ginfo.get("source_file", "")})
            else:
                globals_read.append({"name": gname, "type": ginfo.get("type", ""),
                                     "source_file": ginfo.get("source_file", "")})

    # Also scan for extern/global-scope variable patterns in body_text
    # Common patterns: g_xxx, gXxx (Hungarian notation globals), or
    # uppercase identifiers (macro constants) used in conditions
    # Build a single write-match set: all identifiers that appear on LHS of
    # an assignment operator. Used to detect inferred globals that are written.
    written_any = set(m.group(1) for m in _SA_ANY_WRITE_RE.finditer(body_text))

    seen_written_names = set(e["name"] for e in globals_written)
    seen_read_names = set(e["name"] for e in globals_read)
    for m in _SA_GLOBAL_PREFIX_RE.finditer(body_text):
        vname = m.group(1)
        if vname in local_names or vname in global_var_names:
            continue
        # Skip very short names and common C keywords/types
        if len(vname) <= 2 or vname in ('goto', 'get'):
            continue
        is_write = vname in written_any
        entry = {"name": vname, "type": "", "source_file": "",
                 "inferred": True}
        if is_write:
            if vname not in seen_written_names:
                seen_written_names.add(vname)
                globals_written.append(entry)
            # Inferred globals written are also implicitly read (write-then-read
            # pattern is common; conservative assumption to surface the var).
            if vname not in seen_read_names:
                seen_read_names.add(vname)
                globals_read.append(entry)
        else:
            if vname not in seen_read_names:
                seen_read_names.add(vname)
                globals_read.append(entry)

    # --- Struct field access ---
    fields_read = []
    fields_written = []

    # 1. From field_assignments data: entries where caller matches node_name
    # These represent explicit struct field write assignments (e.g., table->init = foo_init)
    for fa in field_assignments:
        fa_caller = fa.get("caller", "")
        # Match by function name or by caller field containing node_name
        if fa_caller == node_name or (node_name and fa_caller.endswith("_" + node_name)):
            field_name = fa.get("field_name", "")
            struct_chain = fa.get("struct_chain", "")
            target_func = fa.get("target_func", "")
            is_param_bridged = fa.get("is_param", False)
            entry = {
                "struct_chain": struct_chain,
                "field_name": field_name,
                "target_func": target_func,
            }
            if is_param_bridged:
                entry["is_param"] = True
            fields_written.append(entry)

    # 2. From body_text: scan for struct field dereference patterns
    # Read patterns: obj->field, obj.field (not on LHS of assignment)
    # Write patterns: obj->field =, obj.field = (on LHS of assignment)
    # Regexes are module-level (_FIELD_ACCESS_RE, _FIELD_WRITE_RE) to
    # avoid recompiling on every call (1.5M+ calls on kernel-scale).

    # Cache all object-assignment matches in body_text so _trace_object_origin
    # doesn't re-scan the full body for each field access. Without this cache,
    # each field read/write triggers 3 finditer scans × 3 fields = 9 full-body
    # scans per function × 1.5M functions = 13.5B character scans.
    _cached_obj_assigns = list(_OBJ_ASSIGN_RE.finditer(body_text))

    written_field_keys = set()  # (obj, field) pairs that are written
    seen_written_keys = set()  # for O(1) dedup of fields_written entries
    for e in fields_written:
        seen_written_keys.add((e.get("struct_chain", ""), e.get("field_name", "")))
    for m in _FIELD_WRITE_RE.finditer(body_text):
        obj_name = m.group(1)
        field_name = m.group(2)
        # Skip common non-struct identifiers and C keywords.
        # Note: do NOT skip local/param names here — field access through
        # a parameter (e.g., bdev->name where bdev is a function parameter)
        # is the canonical case for struct field tracking.
        if obj_name in ('return', 'if', 'else', 'while', 'for', 'switch',
                        'case', 'break', 'continue', 'sizeof', 'typeof',
                        'struct', 'enum', 'union', 'NULL', 'true', 'false'):
            continue
        key = (obj_name, field_name)
        written_field_keys.add(key)
        if key in seen_written_keys:
            continue
        seen_written_keys.add(key)
        entry = {"struct_chain": obj_name, "field_name": field_name}
        # Capture the assigned value (RHS) — strip trailing whitespace.
        # This enables NULL-write detection: query field-access --value NULL
        # to find only writers that explicitly assign NULL.
        rhs = (m.group(4) or "").strip()
        if rhs:
            entry["assigned_value"] = rhs
        # Capture the enclosing if/switch guard condition.
        # This lets field-flow surface guards_on_path and reachable_in_scene,
        # which is the key signal that distinguishes a real bug (writer
        # reachable in scene) from a false positive (writer guarded out).
        guard = _find_enclosing_guard(body_text, m.start())
        if guard:
            entry["guard_condition"] = guard
        # Trace where obj_name was initialized, to distinguish
        # objects from different address_spaces. The key KASAN_FINAL_REPORT
        # insight: journal_unmap_buffer's bh comes from a different
        # address_space than the reader's bh, so the writer doesn't affect
        # the reader. object_origin captures the source chain (e.g.,
        # "jh->bh") so the agent can compare writer and reader origins.
        origin = _trace_object_origin(body_text, obj_name, _cached_assignments=_cached_obj_assigns)
        if origin:
            entry["object_origin"] = origin
        fields_written.append(entry)

    # Read patterns: field access not on LHS.
    # For fields that are also written in this function, skip the read
    # entry — the write entry already establishes that the field is
    # accessed by this function, and field-level tracking is set-based
    # (we don't need to record both reads and writes for the same field).
    seen_read_keys = set()
    for m in _FIELD_ACCESS_RE.finditer(body_text):
        obj_name = m.group(1)
        field_name = m.group(2)
        if obj_name in ('return', 'if', 'else', 'while', 'for', 'switch',
                        'case', 'break', 'continue', 'sizeof', 'typeof',
                        'struct', 'enum', 'union', 'NULL', 'true', 'false'):
            continue
        key = (obj_name, field_name)
        if key in written_field_keys or key in seen_read_keys:
            continue
        seen_read_keys.add(key)
        read_entry = {"struct_chain": obj_name, "field_name": field_name}
        # Trace object_origin for reads too — lets the agent
        # compare writer's object_origin vs reader's object_origin to detect
        # when they operate on different objects (e.g., buffer_head from
        # bdev->bd_inode->i_mapping vs ext4_inode->i_mapping).
        origin = _trace_object_origin(body_text, obj_name, _cached_assignments=_cached_obj_assigns)
        if origin:
            read_entry["object_origin"] = origin
        fields_read.append(read_entry)

    return {
        "globals_read": globals_read,
        "globals_written": globals_written,
        "fields_read": fields_read,
        "fields_written": fields_written,
    }



def _extract_state_access_all(G: nx.DiGraph, extraction: dict,
                              jobs: int = 0,
                              max_workers: int = 0,
                              parallel_mode: str = "thread",
                              explicit_parallel_mode: bool = False) -> None:
    """Extract shared state access info for all non-empty nodes in the graph.

    When parallel_mode='process', uses ProcessPoolExecutor to bypass the
    GIL. The pool uses the spawn start method: this runs AFTER the graph
    is constructed, so the parent process holds the full in-memory graph
    (tens of GB on large builds) — fork() would hand every worker a
    copy-on-write mapping of all of it and OOM the box. All worker inputs
    (node data, field_assignments, the cached globals regex) are passed
    explicitly per item, so spawn needs no COW inheritance. Only the
    small result dict per node is sent back via pipe.
    This gives TRUE multi-core parallelism for the regex + dict
    construction work that dominates _extract_state_access.

    Sets node attributes: globals_read, globals_written, fields_read, fields_written.
    Called during build after graph construction, before freeing extraction data.

    When ``jobs`` > 1 (or 0=auto and graph is large), the per-node regex
    extraction runs on a ThreadPoolExecutor. ``re`` releases the GIL during
    matching, so this yields real speedup on multi-core boxes.
    """
    globals_data = extraction.get("globals", {})
    field_assignments = extraction.get("field_assignments", [])

    # Pre-build per-build cache: ``global_var_names`` dict + the compiled
    # ``_ASSIGN_OPS`` regex. Both depend only on ``globals_data``, which is
    # fixed for the entire build. Compiling once here saves ~5-10s on SPDK
    # (16K nodes × ~3ms compile = ~50s wasted).
    global_var_names_full = {}
    for gv in globals_data.get("global_vars", []):
        gname = gv.get("name", "")
        if gname:
            global_var_names_full[gname] = gv
    _cached_globals = None
    if global_var_names_full:
        _ASSIGN_OPS = re.compile(
            r'\b(' + '|'.join(re.escape(gn) for gn in
                              sorted(global_var_names_full.keys(),
                                     key=len, reverse=True))
            + r')\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*[^=]'
        )
        _cached_globals = {
            "var_names": global_var_names_full,
            "var_names_keys": set(global_var_names_full.keys()),
            "assign_ops_re": _ASSIGN_OPS,
        }

    # Filter to candidate nodes once — avoids re-checking is_empty per worker.
    # Skip nodes that already have state_access populated (e.g., from the
    # pre-strip extraction path that runs before body_text is dropped for
    # memory savings on large projects). Re-extracting would be wasted work
    # and would also fail because body_text is gone.
    candidates = [(nid, nd) for nid, nd in G.nodes(data=True)
                  if not nd.get("is_empty", False)
                  and nd.get("node_type") != "file"
                  and nd.get("body_text", "")
                  and not (nd.get("fields_read") or nd.get("fields_written")
                           or nd.get("globals_read") or nd.get("globals_written"))]

    if not candidates:
        return

    def _work(nid, ndata):
        local_vars = ndata.get("local_vars", [])
        params = ndata.get("params", [])
        node_name = ndata.get("name", "")
        body = ndata.get("body_text", "")
        access_info = _extract_state_access(body, local_vars, params,
                                            globals_data, field_assignments,
                                            node_name,
                                            _cached_globals=_cached_globals)
        out = {}
        for key in ("globals_read", "globals_written",
                    "fields_read", "fields_written"):
            val = access_info.get(key, [])
            if val:
                out[key] = val
        return out or None

    # Decide sequential vs parallel
    try:
        from _builder.build.parallel import resolve_jobs
        workers = resolve_jobs(jobs, max_workers_cap=max_workers)
    except ImportError:
        workers = 1

    if workers <= 1:
        for nid, ndata in candidates:
            res = _work(nid, ndata)
            if res:
                for k, v in res.items():
                    G.nodes[nid][k] = v
        return

    # When parallel_mode='process', use ProcessPoolExecutor to bypass
    # the GIL. spawn (not fork): the parent holds the full graph here —
    # fork COW would map all of it into every worker (see the 2026-09-02
    # OOM: 86GB parent x N workers). Every worker input is passed
    # explicitly in the args tuple, so nothing relies on inheritance.
    if parallel_mode == "process" and len(candidates) > 100:
        try:
            from concurrent.futures import ProcessPoolExecutor
            import multiprocessing as _mp
            ctx = _mp.get_context("spawn")
            # Pack all dependencies into the args tuple so the
            # module-level _proc_state_access can work without closures.
            _cached = _cached_globals
            items = [
                (nid, G.nodes[nid], field_assignments, _cached)
                for nid, _ in candidates
            ]
            chunk_size = max(1, len(items) // (workers * 4))
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                results = list(pool.map(
                    _proc_state_access,
                    items,
                    chunksize=chunk_size,
                ))
            for nid, res in results:
                if res:
                    for k, v in res.items():
                        if v:
                            G.nodes[nid][k] = v
            return
        except (ImportError, OSError, BrokenPipeError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass

    from _builder.build.parallel import merge_node_attributes
    merge_node_attributes(G, candidates, _work, jobs=workers,
                          max_workers_cap=max_workers,
                          explicit_parallel_mode=explicit_parallel_mode,
                          desc="state_access")



def _proc_state_access(args):
    """Module-level worker for ProcessPoolExecutor — must be top-level for pickling.

    Receives a tuple: (nid, ndata, field_assignments, cached_globals)
    Returns: (nid, result_dict)
    """
    nid, ndata, field_assignments, cached_globals = args
    result = _extract_state_access(
        ndata.get("body_text", ""),
        ndata.get("local_vars", []),
        ndata.get("params", []),
        globals_data=None,
        field_assignments=field_assignments,
        node_name=ndata.get("name", ""),
        _cached_globals=cached_globals,
    )
    return nid, result


# Module-level globals for the pre-strip ProcessPoolExecutor path.
# Set per-worker in each spawned child by _pre_strip_worker_init (the
# pool initializer) — with the spawn start method nothing is inherited
# from the parent process.
_PRE_STRIP_GLOBALS = None
_PRE_STRIP_FIELD_ASSIGNMENTS = None
_PRE_STRIP_CACHED = None



def _pre_strip_worker_init(globals_data, field_assignments, cached_globals):
    """ProcessPoolExecutor initializer for the pre-strip state_access
    pool: set the _PRE_STRIP_* module globals in each spawned child.
    With the spawn start method nothing is inherited from the parent,
    so the worker context crosses the process boundary exactly once
    per worker via initargs.
    """
    global _PRE_STRIP_GLOBALS, _PRE_STRIP_FIELD_ASSIGNMENTS, _PRE_STRIP_CACHED
    _PRE_STRIP_GLOBALS = globals_data
    _PRE_STRIP_FIELD_ASSIGNMENTS = field_assignments
    _PRE_STRIP_CACHED = cached_globals



def _proc_pre_strip_state_access(item):
    """Module-level worker for pre-strip state_access extraction via ProcessPoolExecutor.

    Receives: (index, function_dict) tuple from the pre-strip candidate list.
    Returns: (index, access_info_dict) — caller merges results back.

    Reads module-level _PRE_STRIP_* globals (set per-worker by
    _pre_strip_worker_init, the pool initializer — NOT inherited from
    the parent: the pool uses spawn because the parent process holds
    the extraction payload in memory and fork COW would map all of it
    into every worker).
    """
    _idx, _func = item
    _body = _func.get("body_text", "")
    if not _body:
        return _idx, None
    _access_info = _extract_state_access(
        _body,
        _func.get("local_vars", []),
        _func.get("params", []),
        _PRE_STRIP_GLOBALS,
        _PRE_STRIP_FIELD_ASSIGNMENTS,
        _func.get("name", ""),
        _cached_globals=_PRE_STRIP_CACHED,
    )
    return _idx, _access_info


