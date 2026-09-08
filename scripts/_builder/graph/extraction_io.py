"""callgraph builder module: extraction_io — split from graph_build.py."""

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


def _load_extraction_chunked(extraction_path: str, memory_guard=None):
    """Load extraction JSON with memory-aware chunking.

    For files > 1GB, automatically strips body_text to save memory.
    Reads the file only once (not twice like the old code).

    Returns:
        Tuple of (data_dict, extraction_tokens)
    """
    import gc as _gc
    from _builder.token_budget import estimate_tokens

    file_size = os.path.getsize(extraction_path)
    raw_text = Path(extraction_path).read_text(encoding="utf-8")
    extraction_tokens = estimate_tokens(raw_text)
    data = json.loads(raw_text)
    del raw_text
    _gc.collect()

    # For large files, strip body_text proactively
    if file_size > 1_000_000_000:  # > 1GB
        print(f"[MemoryGuard] Large extraction file ({file_size / 1e9:.1f}GB), "
              f"stripping body_text to save memory", file=sys.stderr)
        dropped = 0
        for func in data.get("functions", []):
            if "body_text" in func:
                del func["body_text"]
                dropped += 1
        _gc.collect()
        if dropped:
            print(f"[MemoryGuard] Stripped body_text from {dropped} functions",
                  file=sys.stderr)

    return data, extraction_tokens



def _read_file_bytes(fpath: str) -> bytes | None:
    """Read a file's raw bytes (top-level function for ThreadPoolExecutor).

    Returns the file content as bytes, or None on error.
    I/O is GIL-free, so multiple threads can read files in parallel.
    The caller deserializes with json.loads() in the main thread.
    """
    import sys as _sys
    import os as _os
    try:
        with open(fpath, "rb") as f:
            return f.read()
    except OSError as e:
        print(f"[build] WARNING: Cannot read file {_os.path.basename(fpath)}: "
              f"{e}", file=_sys.stderr)
        return None



def _load_json_file(fpath: str):
    """Load a single JSON file (top-level function for ProcessPoolExecutor).

    Returns the deserialized Python object, or None on error
    (error is printed to stderr).
    """
    import json as _json
    import sys as _sys
    import os as _os
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return _json.load(f)
    except _json.JSONDecodeError as e:
        print(f"[build] WARNING: Corrupt file {_os.path.basename(fpath)}: "
              f"{e.msg} at pos {e.pos}, skipping", file=_sys.stderr)
        return None
    except OSError as e:
        print(f"[build] WARNING: Cannot read file {_os.path.basename(fpath)}: "
              f"{e}", file=_sys.stderr)
        return None



def _load_split_extraction(extraction_dir: str, strip_body_text: bool = False) -> dict:
    """Load per-domain extraction files incrementally.

    Reads functions and edges from separate per-domain JSON files
    to avoid loading a single monolithic extraction JSON.

    Args:
        extraction_dir: Path to directory with split extraction files
        strip_body_text: If True, strip body_text from functions during loading
                         to reduce memory usage by ~60%. Body_text is only needed
                         at query time and can be re-read from SQLite later.
                         Note: state_access (globals_read/written, fields_read/
                         written) is derived from body_text BEFORE stripping,
                         so it is preserved even when strip_body_text=True.

    Returns:
        Combined extraction data dict
    """
    import glob as _glob
    import gc as _gc
    data = {}

    # Load metadata
    meta_path = os.path.join(extraction_dir, "_metadata.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            data.update(json.load(f))

    # Load globals and field_assignments BEFORE functions.
    # Reason: when strip_body_text=True, we strip body_text after deriving
    # state_access (globals_read/written, fields_read/written) per-function
    # in the loop below. That derivation needs globals_data and
    # field_assignments to be already loaded. Loading them first costs a
    # small constant memory increase but preserves correctness for large
    # projects that use --low-memory.
    _LARGE_FILE_THRESHOLD = 500_000_000  # 500MB

    def _load_aux_file(key: str) -> None:
        """Load one auxiliary extraction file (globals, field_assignments, etc.).

        Handles monolithic file (key.json) and chunked files (key_*.json).
        Writes result into data[key].
        """
        fpath = os.path.join(extraction_dir, f"{key}.json")
        if os.path.exists(fpath):
            fsize = os.path.getsize(fpath)
            try:
                if fsize > _LARGE_FILE_THRESHOLD:
                    print(f"[build] Warning: {key}.json is {fsize/1e9:.1f}GB, "
                          f"loading with reduced detail to save memory", file=sys.stderr)
                    with open(fpath, "r", encoding="utf-8") as f:
                        data[key] = json.load(f)
                    # For globals: strip large sub-lists that aren't needed for graph building
                    if key == "globals" and isinstance(data[key], dict):
                        for subkey in ("global_vars",):
                            gv = data[key].get(subkey, [])
                            if len(gv) > 50000:
                                print(f"[build] Truncating globals.{subkey} from {len(gv)} to 50000 entries",
                                      file=sys.stderr)
                                data[key][subkey] = gv[:50000]
                    _gc.collect()
                else:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data[key] = json.load(f)
            except json.JSONDecodeError as e:
                print(f"[build] WARNING: Corrupt file {os.path.basename(fpath)}: "
                      f"{e.msg} at pos {e.pos}, skipping", file=sys.stderr)
        else:
            # Try chunked files (e.g., globals_0.json, globals_1.json, ...)
            _chunk_files = sorted(_glob.glob(os.path.join(extraction_dir, f"{key}_*.json")))
            if _chunk_files:
                if key in ("fn_ptr_calls", "passthrough_reg_funcs"):
                    # Dict-type: merge all chunks
                    data[key] = {}
                    for cf in _chunk_files:
                        try:
                            with open(cf, "r", encoding="utf-8") as f:
                                chunk_data = json.load(f)
                            for k, v in chunk_data.items():
                                if k in data[key]:
                                    if isinstance(data[key][k], list):
                                        data[key][k].extend(v if isinstance(v, list) else [v])
                                    elif isinstance(data[key][k], dict):
                                        data[key][k].update(v)
                                else:
                                    data[key][k] = v
                            del chunk_data
                        except json.JSONDecodeError as e:
                            print(f"[build] WARNING: Corrupt chunk {os.path.basename(cf)}: "
                                  f"{e.msg} at pos {e.pos}, skipping", file=sys.stderr)
                elif key == "globals":
                    # Dict of lists: merge by extending lists
                    data[key] = {"enums": [], "constants": [], "typedefs": [], "global_vars": []}
                    for cf in _chunk_files:
                        try:
                            with open(cf, "r", encoding="utf-8") as f:
                                chunk_data = json.load(f)
                            for subkey in ("enums", "constants", "typedefs", "global_vars"):
                                if subkey in chunk_data:
                                    data[key].setdefault(subkey, []).extend(chunk_data[subkey])
                            del chunk_data
                        except json.JSONDecodeError as e:
                            print(f"[build] WARNING: Corrupt chunk {os.path.basename(cf)}: "
                                  f"{e.msg} at pos {e.pos}, skipping", file=sys.stderr)
                else:
                    # List-type: extend
                    data[key] = []
                    for cf in _chunk_files:
                        try:
                            with open(cf, "r", encoding="utf-8") as f:
                                chunk_data = json.load(f)
                            data[key].extend(chunk_data)
                            del chunk_data
                        except json.JSONDecodeError as e:
                            print(f"[build] WARNING: Corrupt chunk {os.path.basename(cf)}: "
                                  f"{e.msg} at pos {e.pos}, skipping", file=sys.stderr)

    # Load globals and field_assignments first (needed for state_access in func loop)
    _load_aux_file("globals")
    _load_aux_file("field_assignments")
    _globals_data = data.get("globals", {"enums": [], "constants": [], "typedefs": [], "global_vars": []})
    _field_assignments = data.get("field_assignments", [])

    # Load functions by domain — parallel I/O with ThreadPoolExecutor.
    # The approach uses two phases:
    #   Phase 1 (parallel I/O): ThreadPoolExecutor reads file contents into
    #     memory in parallel. Python releases the GIL during I/O, so multiple
    #     threads can read files concurrently.
    #   Phase 2 (serial deserialize): json.loads() in the main thread
    #     deserializes the raw bytes. This avoids the massive pickle overhead
    #     of ProcessPoolExecutor (transferring 15GB of Python objects across
    #     process boundaries), while still overlapping I/O with CPU work.
    #   strip_body_text / _extract_state_access are applied in the main
    #   process after deserialization, because they depend on _globals_data
    #   and _field_assignments which are too large to pickle efficiently.
    functions_dir = os.path.join(extraction_dir, "functions")
    # Initialize up front — the edges/ and cgdb/ blocks below read
    # _use_parallel even when functions/ is absent (which would otherwise
    # leave it unbound → NameError).
    _use_parallel = False
    if os.path.isdir(functions_dir):
        data["functions"] = []
        _func_files = sorted(_glob.glob(os.path.join(functions_dir, "*.json")))
        _total_func_files = len(_func_files)

        # Use parallel I/O when there are enough files to amortize the
        # ThreadPool overhead. Threshold is conservative (50+ files).
        _use_parallel = _total_func_files > 50 and (os.cpu_count() or 1) > 1
        if _use_parallel:
            _load_start = time.time()
            try:
                from concurrent.futures import ThreadPoolExecutor
                _n_workers = min(os.cpu_count() or 4, 8)  # Cap at 8 I/O threads
                # Phase 1: parallel I/O — read all files into memory
                with ThreadPoolExecutor(max_workers=_n_workers) as _pool:
                    _raw_chunks = list(_pool.map(_read_file_bytes, _func_files))
                # Phase 2: serial deserialize + merge
                _gc_milestone = 0
                for _fi, _raw in enumerate(_raw_chunks):
                    if _raw is None:
                        continue  # read error already printed
                    try:
                        domain_funcs = json.loads(_raw)
                    except json.JSONDecodeError as e:
                        print(f"[build] WARNING: Corrupt file "
                              f"{os.path.basename(_func_files[_fi])}: "
                              f"{e.msg} at pos {e.pos}, skipping",
                              file=sys.stderr)
                        continue
                    if strip_body_text:
                        for func in domain_funcs:
                            body = func.get("body_text", "")
                            if not body:
                                continue
                            access_info = _extract_state_access(
                                body,
                                func.get("local_vars", []),
                                func.get("params", []),
                                _globals_data,
                                _field_assignments,
                                func.get("name", ""))
                            for _ak in ("globals_read", "globals_written",
                                        "fields_read", "fields_written"):
                                _av = access_info.get(_ak, [])
                                if _av:
                                    func[_ak] = _av
                        _STRIP_KEYS = ("body_text", "macros")
                        for func in domain_funcs:
                            for k in _STRIP_KEYS:
                                func.pop(k, None)
                    data["functions"].extend(domain_funcs)
                    del _raw, domain_funcs
                    _func_count = len(data["functions"])
                    _next_milestone = (_gc_milestone + 1) * 100000
                    if _func_count >= _next_milestone:
                        _gc.collect()
                        _gc_milestone += 1
                        print(f"[build] Loaded {_fi+1}/{_total_func_files} function files "
                              f"({_func_count} functions)", file=sys.stderr)
                _load_elapsed = time.time() - _load_start
                print(f"[build] Loaded {_total_func_files} function files "
                      f"({len(data['functions'])} functions) in {_load_elapsed:.1f}s "
                      f"(parallel I/O, {_n_workers} threads)", file=sys.stderr)
            except (ImportError, OSError):
                # Fallback to serial if ThreadPool fails
                _use_parallel = False

        if not _use_parallel:
            # Serial path (original logic, kept as fallback)
            _gc_milestone = 0
            for _fi, fpath in enumerate(_func_files):
                with open(fpath, "r", encoding="utf-8") as f:
                    domain_funcs = json.load(f)
                    if strip_body_text:
                        for func in domain_funcs:
                            body = func.get("body_text", "")
                            if not body:
                                continue
                            access_info = _extract_state_access(
                                body,
                                func.get("local_vars", []),
                                func.get("params", []),
                                _globals_data,
                                _field_assignments,
                                func.get("name", ""))
                            for _ak in ("globals_read", "globals_written",
                                        "fields_read", "fields_written"):
                                _av = access_info.get(_ak, [])
                                if _av:
                                    func[_ak] = _av
                    if strip_body_text:
                        _STRIP_KEYS = ("body_text", "macros")
                        for func in domain_funcs:
                            for k in _STRIP_KEYS:
                                func.pop(k, None)
                    data["functions"].extend(domain_funcs)
                del domain_funcs
                _func_count = len(data["functions"])
                _next_milestone = (_gc_milestone + 1) * 100000
                if _func_count >= _next_milestone:
                    _gc.collect()
                    _gc_milestone += 1
                    print(f"[build] Loaded {_fi+1}/{_total_func_files} function files "
                          f"({_func_count} functions)", file=sys.stderr)

    # Load edges by domain — also parallelized with ThreadPoolExecutor.
    edges_dir = os.path.join(extraction_dir, "edges")
    if os.path.isdir(edges_dir):
        data["edges"] = []
        _edge_files = sorted(_glob.glob(os.path.join(edges_dir, "*.json")))
        _total_edge_files = len(_edge_files)
        if _use_parallel and _total_edge_files > 50:
            try:
                from concurrent.futures import ThreadPoolExecutor
                _n_workers = min(os.cpu_count() or 4, 8)
                with ThreadPoolExecutor(max_workers=_n_workers) as _pool:
                    _raw_edges = list(_pool.map(_read_file_bytes, _edge_files))
                for _ei, _raw in enumerate(_raw_edges):
                    if _raw is None:
                        continue
                    try:
                        domain_edges = json.loads(_raw)
                    except json.JSONDecodeError as e:
                        print(f"[build] WARNING: Corrupt edge file "
                              f"{os.path.basename(_edge_files[_ei])}: "
                              f"{e.msg} at pos {e.pos}, skipping",
                              file=sys.stderr)
                        continue
                    data["edges"].extend(domain_edges)
                    del _raw, domain_edges
            except (ImportError, OSError):
                # Fallback to serial
                for fpath in _edge_files:
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            domain_edges = json.load(f)
                            data["edges"].extend(domain_edges)
                        del domain_edges
                    except json.JSONDecodeError as e:
                        print(f"[build] WARNING: Corrupt edge file {os.path.basename(fpath)}: "
                              f"{e.msg} at pos {e.pos}, skipping", file=sys.stderr)
        else:
            for fpath in _edge_files:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        domain_edges = json.load(f)
                        data["edges"].extend(domain_edges)
                    del domain_edges
                except json.JSONDecodeError as e:
                    print(f"[build] WARNING: Corrupt edge file {os.path.basename(fpath)}: "
                          f"{e.msg} at pos {e.pos}, skipping", file=sys.stderr)

    # Load remaining auxiliary files (skip globals/field_assignments — already loaded)
    for key in ("vtable_registrations", "import_edges",
                "fn_ptr_calls", "passthrough_reg_funcs",
                "macro_registrations", "token_paste_functions",
                "container_of_usages", "conversion_funcs", "struct_defs"):
        _load_aux_file(key)

    # Load cgdb layer chunk files from cgdb/ subdirectory.
    # The scanner writes cgdb_nodes/cgdb_edges/cgdb_data_flow/etc. as
    # chunked JSON files under <split_dir>/cgdb/ to avoid accumulating
    # 20+GB in memory during kernel-scale scans. Each key may have
    # multiple chunks (cgdb_nodes_0.json, cgdb_nodes_1.json, ...).
    # Without this, cgdb layer data is silently dropped for split-output
    # scans, and the resulting graph has empty cgdb_nodes/cgdb_edges
    # tables even though the scanner produced them.
    _cgdb_dir = os.path.join(extraction_dir, "cgdb")
    if os.path.isdir(_cgdb_dir):
        _cgdb_start = time.time()
        for key in ("cgdb_nodes", "cgdb_types", "cgdb_edges",
                    "cgdb_invoke_sites", "cgdb_predicates",
                    "cgdb_ops_bindings", "cgdb_basic_blocks",
                    "cgdb_cfg_edges", "cgdb_data_flow",
                    "cgdb_sync_primitives", "cgdb_happens_before",
                    "cgdb_alias_sets", "cgdb_doc_comments",
                    "cgdb_metadata", "cgdb_includes", "conditions"):
            _chunk_files = sorted(_glob.glob(
                os.path.join(_cgdb_dir, f"{key}_*.json")))
            if not _chunk_files:
                continue
            data[key] = []
            if _use_parallel and len(_chunk_files) > 10:
                # Parallel I/O for cgdb chunks (large files)
                try:
                    from concurrent.futures import ThreadPoolExecutor
                    _n_workers = min(os.cpu_count() or 4, 8)
                    with ThreadPoolExecutor(max_workers=_n_workers) as _pool:
                        _raw_chunks = list(_pool.map(_read_file_bytes, _chunk_files))
                    for _ci, _raw in enumerate(_raw_chunks):
                        if _raw is None:
                            continue
                        try:
                            chunk_data = json.loads(_raw)
                        except json.JSONDecodeError as e:
                            print(f"[build] WARNING: Corrupt cgdb chunk "
                                  f"{os.path.basename(_chunk_files[_ci])}: "
                                  f"{e.msg} at pos {e.pos}, skipping",
                                  file=sys.stderr)
                            continue
                        data[key].extend(chunk_data)
                        del _raw, chunk_data
                except (ImportError, OSError):
                    # Fallback to serial
                    for cf in _chunk_files:
                        try:
                            with open(cf, "r", encoding="utf-8") as f:
                                chunk_data = json.load(f)
                            data[key].extend(chunk_data)
                            del chunk_data
                        except json.JSONDecodeError as e:
                            print(f"[build] WARNING: Corrupt cgdb chunk "
                                  f"{os.path.basename(cf)}: {e.msg} at pos {e.pos}, "
                                  f"skipping", file=sys.stderr)
            else:
                for cf in _chunk_files:
                    try:
                        with open(cf, "r", encoding="utf-8") as f:
                            chunk_data = json.load(f)
                        data[key].extend(chunk_data)
                        del chunk_data
                    except json.JSONDecodeError as e:
                        print(f"[build] WARNING: Corrupt cgdb chunk "
                              f"{os.path.basename(cf)}: {e.msg} at pos {e.pos}, "
                              f"skipping", file=sys.stderr)
            _gc.collect()
        _cgdb_elapsed = time.time() - _cgdb_start
        _cgdb_total = sum(len(data.get(k, [])) for k in
                          ("cgdb_nodes", "cgdb_types", "cgdb_edges",
                           "cgdb_invoke_sites", "cgdb_predicates",
                           "cgdb_ops_bindings", "cgdb_basic_blocks",
                           "cgdb_cfg_edges", "cgdb_data_flow",
                           "cgdb_sync_primitives", "cgdb_happens_before",
                           "cgdb_alias_sets", "cgdb_doc_comments",
                           "cgdb_metadata", "cgdb_includes", "conditions")
                          if k in data)
        print(f"[build] Loaded cgdb layer: {_cgdb_total} records in "
              f"{_cgdb_elapsed:.1f}s", file=sys.stderr)

    # Set defaults for missing keys
    data.setdefault("functions", [])
    data.setdefault("edges", [])
    data.setdefault("import_edges", [])
    data.setdefault("globals", {"enums": [], "constants": [], "typedefs": [], "global_vars": []})
    data.setdefault("vtable_registrations", [])
    data.setdefault("macro_registrations", [])
    data.setdefault("token_paste_functions", [])
    data.setdefault("container_of_usages", [])
    data.setdefault("conversion_funcs", [])
    data.setdefault("struct_defs", [])
    data.setdefault("fn_ptr_calls", {})
    data.setdefault("passthrough_reg_funcs", {})
    data.setdefault("field_assignments", [])

    return data


