"""LLM integration points for profile phases 4 and 6.

These phases are optional and do NOT modify scanner/builder code.
They produce structured prompts that an LLM (or MCP tool) can consume,
and parse the LLM's structured output back into profile updates.

Phase 4 (LLM header analysis):
  - Input: key header files + draft profile
  - Output: callback registration patterns, thread/task/message dispatch mechanisms

Phase 6 (LLM result check):
  - Input: extraction.json + profile
  - Output: quality findings (missing edges, false positives, misclassified endpoints)
"""

import json
import os
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Phase 4: LLM header analysis — generate prompt & parse response
# ---------------------------------------------------------------------------

def collect_key_headers(source_root: str, profile: dict = None,
                        max_headers: int = 20, max_bytes: int = 512000) -> list:
    """Collect key header files for LLM analysis.

    Priority order:
      1. public_header_paths from profile
      2. header_priority_dirs from scan_hints
      3. top-level include/ dir

    Returns list of dicts: [{"path": relpath, "content": text}, ...]
    """
    if profile is None:
        profile = {}

    scan_hints = profile.get("scan_hints", {})
    api_det = profile.get("api_detection", {})
    priority_dirs = scan_hints.get("header_priority_dirs", ["include"])
    public_paths = api_det.get("public_header_paths", [])

    # Collect candidate header files
    candidates = []
    seen = set()

    # 1. Public header paths (explicit)
    for pub_path in public_paths:
        abs_path = os.path.join(source_root, pub_path)
        if not os.path.isdir(abs_path):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_path):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for fname in filenames:
                if fname.endswith(('.h', '.hpp')):
                    fpath = os.path.join(dirpath, fname)
                    if fpath not in seen:
                        candidates.append(fpath)
                        seen.add(fpath)

    # 2. Priority dirs (include/)
    for pdir in priority_dirs:
        abs_path = os.path.join(source_root, pdir)
        if not os.path.isdir(abs_path):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_path):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for fname in filenames:
                if fname.endswith(('.h', '.hpp')):
                    fpath = os.path.join(dirpath, fname)
                    if fpath not in seen:
                        candidates.append(fpath)
                        seen.add(fpath)

    # Read headers, limit total size
    headers = []
    total_bytes = 0
    for fpath in candidates[:max_headers * 3]:  # allow more candidates, filter by size
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except (IOError, OSError):
            continue

        size = len(content.encode('utf-8'))
        if total_bytes + size > max_bytes:
            # Truncate this header if it would exceed budget
            remaining = max_bytes - total_bytes
            if remaining > 500:
                content = content[:remaining] + "\n/* ... truncated ... */\n"
                size = remaining + 30
            else:
                break

        relpath = os.path.relpath(fpath, source_root)
        headers.append({"path": relpath, "content": content})
        total_bytes += size

        if len(headers) >= max_headers:
            break

    return headers


def generate_header_analysis_prompt(source_root: str, profile: dict = None) -> str:
    """Generate an LLM prompt for header analysis (Phase 4).

    The prompt asks the LLM to identify:
      - Callback registration functions and their patterns
      - Thread/task/message dispatch mechanisms
      - Event handler registration patterns
    """
    headers = collect_key_headers(source_root, profile)

    if not headers:
        return ""

    # Build header content block
    header_block = ""
    for h in headers:
        header_block += f"\n--- {h['path']} ---\n{h['content']}\n"

    # Existing patterns from profile (if any)
    existing_patterns = []
    if profile:
        for pat in profile.get("callback_detection", {}).get("static_patterns", []):
            existing_patterns.append(
                f"  - {pat['register_func']}: regex={pat['regex']}, "
                f"cb_arg_index={pat['cb_arg_index']}, "
                f"concurrency_type={pat['concurrency_type']}"
            )

    existing_section = ""
    if existing_patterns:
        existing_section = (
            "\nAlready-known callback patterns (do NOT repeat these):\n"
            + "\n".join(existing_patterns) + "\n"
        )

    prompt = f"""Analyze the following C/C++ header files from a project and identify callback registration patterns and concurrency mechanisms.

For each callback registration function you find, provide:
1. register_func: The function name that registers a callback
2. regex: A Python regex that captures the callback function name from a call site.
   The regex must have exactly one capture group for the callback name.
   Example: pthread_create\\s*\\(\\s*[^,]*,\\s*[^,]*,\\s*(\\w+)
3. cb_arg_index: The 0-based argument index of the callback argument
4. concurrency_type: One of "callback", "spawn_target", "event_handler", "task", "signal_handler"

Also identify:
- Thread creation wrappers (functions that create threads or submit work to thread pools)
- Message dispatch mechanisms (event loops, message queues, work submission)
- Signal/event handler registration patterns

CRITICAL — Also identify macro-based registration dispatch patterns:
- Constructor macros: #define MACRO_NAME(...) that expand to __attribute__((constructor))
  These auto-register structs/modules before main() runs.
- Token-paste macros: Macros using ## to generate function names invisible to regex scanning.
- Iterator functions: Functions that walk global lists of registered modules and call dispatch fields.

For each registration macro, provide:
1. macro_name: The macro name (e.g., PROJ_SUBSYSTEM_REGISTER)
2. pattern: A Python regex matching the macro invocation with a capture group for the struct variable
3. struct_arg_index: Which capture group (0-based) contains the struct/module variable name
4. register_func: The function called inside the constructor to add the struct to a global list
5. iterator_func: The function that iterates the global list and calls dispatch fields (if known)
6. dispatch_field: The struct field name called during iteration (e.g., "init", "module_init")

For token-paste macros, provide:
1. macro_name: The macro name
2. template: The ## expression that generates function names (e.g., "_name##_register")
3. param_names: List of macro parameter names used in the template
4. generates: What the generated function does ("constructor", "registration", "handler")

Output ONLY a JSON object with this structure (no other text):
{{
  "callback_patterns": [
    {{
      "register_func": "func_name",
      "regex": "regex_pattern",
      "cb_arg_index": 0,
      "concurrency_type": "callback"
    }}
  ],
  "macro_dispatch_patterns": [
    {{
      "macro_name": "MACRO_NAME",
      "pattern": "MACRO_NAME\\\\s*\\\\(\\\\s*(\\\\w+)\\\\s*\\\\)",
      "struct_arg_index": 0,
      "register_func": "add_func",
      "iterator_func": "init_func",
      "dispatch_field": "init"
    }}
  ],
  "token_paste_macros": [
    {{
      "macro_name": "MACRO_NAME",
      "template": "_name##_register",
      "param_names": ["_name"],
      "generates": "constructor"
    }}
  ],
  "concurrency_mechanisms": [
    {{
      "name": "mechanism name",
      "type": "thread_pool|event_loop|message_queue|work_stealing",
      "description": "brief description"
    }}
  ],
  "notes": "any additional observations"
}}
{existing_section}
Header files:
{header_block}"""

    return prompt


def parse_header_analysis_response(response_text: str) -> dict:
    """Parse the LLM response from header analysis (Phase 4).

    Returns a dict with:
      - callback_patterns: list of pattern dicts (may be empty)
      - concurrency_mechanisms: list of mechanism dicts (may be empty)
      - notes: str or None
      - parse_error: str or None
    """
    result = {
        "callback_patterns": [],
        "macro_dispatch_patterns": [],
        "token_paste_macros": [],
        "concurrency_mechanisms": [],
        "notes": None,
        "parse_error": None,
    }

    # Try to extract JSON from the response
    # The LLM might wrap it in ```json ... ``` or just output raw JSON
    text = response_text.strip()

    # Remove markdown code fences if present
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        result["parse_error"] = f"JSON parse error: {e}"
        return result

    if not isinstance(data, dict):
        result["parse_error"] = "Expected JSON object, got {type(data).__name__}"
        return result

    # Extract callback_patterns
    for pat in data.get("callback_patterns", []):
        if not isinstance(pat, dict):
            continue
        required = ("register_func", "regex", "cb_arg_index", "concurrency_type")
        if all(k in pat for k in required):
            result["callback_patterns"].append({
                "register_func": str(pat["register_func"]),
                "regex": str(pat["regex"]),
                "cb_arg_index": int(pat["cb_arg_index"]),
                "concurrency_type": str(pat["concurrency_type"]),
            })

    # Extract macro_dispatch_patterns
    for pat in data.get("macro_dispatch_patterns", []):
        if not isinstance(pat, dict):
            continue
        required = ("macro_name", "pattern", "struct_arg_index")
        if all(k in pat for k in required):
            entry = {
                "macro_name": str(pat["macro_name"]),
                "pattern": str(pat["pattern"]),
                "struct_arg_index": int(pat["struct_arg_index"]),
            }
            # Optional fields
            for opt_key in ("register_func", "iterator_func", "dispatch_field",
                            "global_list_var", "handler_arg_index"):
                if opt_key in pat and pat[opt_key] is not None:
                    entry[opt_key] = pat[opt_key]
            result["macro_dispatch_patterns"].append(entry)

    # Extract token_paste_macros
    for pat in data.get("token_paste_macros", []):
        if not isinstance(pat, dict):
            continue
        required = ("macro_name", "template", "param_names")
        if all(k in pat for k in required):
            entry = {
                "macro_name": str(pat["macro_name"]),
                "template": str(pat["template"]),
                "param_names": list(pat["param_names"]),
            }
            if "generates" in pat:
                entry["generates"] = str(pat["generates"])
            result["token_paste_macros"].append(entry)

    # Extract concurrency_mechanisms
    for mech in data.get("concurrency_mechanisms", []):
        if not isinstance(mech, dict):
            continue
        result["concurrency_mechanisms"].append({
            "name": str(mech.get("name", "")),
            "type": str(mech.get("type", "")),
            "description": str(mech.get("description", "")),
        })

    result["notes"] = data.get("notes")

    return result


def apply_header_analysis_to_profile(profile: dict, analysis: dict) -> dict:
    """Merge LLM header analysis results into a profile dict.

    Adds new callback_patterns to callback_detection.static_patterns
    (skips duplicates by register_func name).

    Returns the updated profile dict (mutates in place).
    """
    existing_funcs = set()
    for pat in profile.get("callback_detection", {}).get("static_patterns", []):
        existing_funcs.add(pat.get("register_func"))

    for pat in analysis.get("callback_patterns", []):
        if pat["register_func"] not in existing_funcs:
            if "callback_detection" not in profile:
                profile["callback_detection"] = {}
            if "static_patterns" not in profile["callback_detection"]:
                profile["callback_detection"]["static_patterns"] = []
            profile["callback_detection"]["static_patterns"].append(pat)
            existing_funcs.add(pat["register_func"])

    # Merge macro_dispatch_patterns
    existing_macro_names = set()
    for pat in profile.get("macro_dispatch", {}).get("registration_macros", []):
        existing_macro_names.add(pat.get("macro_name"))

    for pat in analysis.get("macro_dispatch_patterns", []):
        if pat["macro_name"] not in existing_macro_names:
            if "macro_dispatch" not in profile:
                profile["macro_dispatch"] = {"registration_macros": [], "token_paste_macros": []}
            if "registration_macros" not in profile["macro_dispatch"]:
                profile["macro_dispatch"]["registration_macros"] = []
            profile["macro_dispatch"]["registration_macros"].append(pat)
            existing_macro_names.add(pat["macro_name"])

    # Merge token_paste_macros
    existing_tp_names = set()
    for pat in profile.get("macro_dispatch", {}).get("token_paste_macros", []):
        existing_tp_names.add(pat.get("macro_name"))

    for pat in analysis.get("token_paste_macros", []):
        if pat["macro_name"] not in existing_tp_names:
            if "macro_dispatch" not in profile:
                profile["macro_dispatch"] = {"registration_macros": [], "token_paste_macros": []}
            if "token_paste_macros" not in profile["macro_dispatch"]:
                profile["macro_dispatch"]["token_paste_macros"] = []
            profile["macro_dispatch"]["token_paste_macros"].append(pat)
            existing_tp_names.add(pat["macro_name"])

    # Mark phase completed
    if "phases" not in profile:
        profile["phases"] = {}
    profile["phases"]["llm_header_analysis_completed"] = True

    return profile


# ---------------------------------------------------------------------------
# Phase 6: LLM result check — generate prompt & parse response
# ---------------------------------------------------------------------------

def generate_result_check_prompt(extraction_path: str, profile: dict = None,
                                 max_bytes: int = 200000) -> str:
    """Generate an LLM prompt for result quality check (Phase 6).

    The prompt asks the LLM to identify:
      - Missing call edges (functions that should be connected but aren't)
      - False positive edges (connections that shouldn't exist)
      - Misclassified endpoints (internal vs external)
    """
    if not os.path.isfile(extraction_path):
        return ""

    try:
        with open(extraction_path, 'r', encoding='utf-8') as f:
            extraction = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return f"Error reading extraction file: {e}"

    # Summarize extraction (don't send full JSON if too large)
    summary = _summarize_extraction(extraction, max_bytes)

    # Profile info
    profile_info = ""
    if profile:
        api = profile.get("api_detection", {})
        cb = profile.get("callback_detection", {})
        ep = profile.get("endpoint_classification", {})
        profile_info = f"""
Project profile info:
- Public API prefixes: {api.get('public_prefixes', [])}
- Callback patterns: {[p['register_func'] for p in cb.get('static_patterns', [])]}
- External lib prefix map: {ep.get('lib_prefix_map', {})}
"""

    prompt = f"""Review the following invocation graph extraction results for quality issues.

Check for:
1. Missing call edges: Functions that are called but don't appear as callees, or
   callback registrations where the callback target has no incoming edge
2. False positive edges: Unlikely call relationships (e.g., init functions calling
   cleanup functions, or test-only code calling production code)
3. Misclassified endpoints: Functions marked as "External Endpoint" that are actually
   project-internal, or internal functions that should be external endpoints
4. Disconnected components: Significant clusters of functions with no connection to
   the main invocation graph

Output ONLY a JSON object with this structure (no other text):
{{
  "missing_edges": [
    {{"caller": "func_a", "callee": "func_b", "reason": "why this edge should exist"}}
  ],
  "false_positives": [
    {{"caller": "func_a", "callee": "func_b", "reason": "why this edge is likely wrong"}}
  ],
  "misclassified_endpoints": [
    {{"function": "func_name", "current": "External Endpoint", "suggested": "Internal", "reason": "why"}}
  ],
  "disconnected_components": [
    {{"functions": ["func1", "func2"], "note": "why this seems wrong"}}
  ],
  "overall_quality": "high|medium|low",
  "notes": "any additional observations"
}}
{profile_info}
Extraction summary:
{summary}"""

    return prompt


def parse_result_check_response(response_text: str) -> dict:
    """Parse the LLM response from result check (Phase 6).

    Returns a dict with quality findings.
    """
    result = {
        "missing_edges": [],
        "false_positives": [],
        "misclassified_endpoints": [],
        "disconnected_components": [],
        "overall_quality": None,
        "notes": None,
        "parse_error": None,
    }

    text = response_text.strip()

    # Remove markdown code fences if present
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        result["parse_error"] = f"JSON parse error: {e}"
        return result

    if not isinstance(data, dict):
        result["parse_error"] = f"Expected JSON object, got {type(data).__name__}"
        return result

    for edge in data.get("missing_edges", []):
        if isinstance(edge, dict) and "caller" in edge and "callee" in edge:
            result["missing_edges"].append(edge)

    for edge in data.get("false_positives", []):
        if isinstance(edge, dict) and "caller" in edge and "callee" in edge:
            result["false_positives"].append(edge)

    for ep in data.get("misclassified_endpoints", []):
        if isinstance(ep, dict) and "function" in ep:
            result["misclassified_endpoints"].append(ep)

    for comp in data.get("disconnected_components", []):
        if isinstance(comp, dict) and "functions" in comp:
            result["disconnected_components"].append(comp)

    result["overall_quality"] = data.get("overall_quality")
    result["notes"] = data.get("notes")

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarize_extraction(extraction: dict, max_bytes: int = 200000) -> str:
    """Create a compact summary of extraction.json for LLM consumption.

    Includes:
      - Function count and top-level stats
      - All endpoint classifications
      - Sample of edges (limited by size)
    """
    parts = []

    # Stats
    functions = extraction.get("functions", {})
    edges = extraction.get("edges", [])
    parts.append(f"Total functions: {len(functions)}")
    parts.append(f"Total edges: {len(edges)}")

    # Endpoint classifications
    endpoints = {}
    for fname, finfo in functions.items():
        cls = finfo.get("classification", "unknown")
        endpoints.setdefault(cls, []).append(fname)
    for cls, fnames in sorted(endpoints.items()):
        parts.append(f"\n{cls} ({len(fnames)} functions):")
        # Show up to 20 per category
        for fn in sorted(fnames)[:20]:
            parts.append(f"  - {fn}")
        if len(fnames) > 20:
            parts.append(f"  ... and {len(fnames) - 20} more")

    # Sample edges
    parts.append(f"\nSample edges (first 200):")
    for edge in edges[:200]:
        caller = edge.get("caller", "?")
        callee = edge.get("callee", "?")
        etype = edge.get("type", "")
        parts.append(f"  {caller} -> {callee} [{etype}]")
    if len(edges) > 200:
        parts.append(f"  ... and {len(edges) - 200} more edges")

    summary = "\n".join(parts)

    # Truncate if too large
    if len(summary.encode('utf-8')) > max_bytes:
        summary = summary[:max_bytes] + "\n... (truncated)"

    return summary
