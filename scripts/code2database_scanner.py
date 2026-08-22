#!/usr/bin/env python3
# Prevent stale .pyc cache from masking source edits during iterative development
import os as _os; _os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')

"""Multi-language code scanner for invocation graph extraction using tree-sitter.

Supports: C, C++, Go, Python, Java, Rust, ASM.
Auto-detects language from file extension; override with --lang flag.

Implementation lives in the _scanner package. This file is the CLI entry
point that imports the sub-modules and dispatches.

Usage:
  code2database_scanner.py scan --source PATH [--lang auto|c|cpp|go|python|java|rust|asm] [--output OUT.json]
"""

import argparse
import gc
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# Ensure _vendor/networkx shim is found before the real networkx
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_vendor"))

from _scanner.utils import check_python_version, EXTENSION_MAP, LANG_EXTENSIONS
from _scanner.changes import detect_changes, save_manifest

# Lazy imports — each scanner module has its own try/except for tree-sitter deps
from _scanner.c_scanner import CTreeSitterScanner
from _scanner.go_scanner import GoTreeSitterScanner
from _scanner.python_scanner import PythonTreeSitterScanner
from _scanner.java_scanner import JavaTreeSitterScanner
from _scanner.rust_scanner import RustTreeSitterScanner
from _scanner.asm_scanner import AsmRegexScanner


# ---------------------------------------------------------------------------
# Scanner dispatcher
# ---------------------------------------------------------------------------

def get_scanner(lang: str, api_prefixes: list = None, export_macros: list = None,
                callback_patterns: list = None, struct_op_types: list = None,
                macro_dispatch_patterns: list = None, profile: dict = None,
                extraction_backend: str = None,
                compile_commands_path: str = None,
                clang_args: list = None):
    """Return the appropriate scanner for the given language.

    extraction_backend: 'auto' (default), 'clang', or 'tree-sitter'.
      - 'auto' for c/cpp: returns DualBackendScanner (runs both, merges).
      - 'clang' for c/cpp: returns ClangScanner only (cgdb-only output).
      - 'tree-sitter' (or any other value, or None): legacy tree-sitter only.
      Ignored for non-c/cpp languages.

    compile_commands_path: path to compile_commands.json (Clang Compilation
      Database). Per-file -I/-D/-std flags are looked up by filepath when
      clang backend is in use.

    clang_args: list of extra clang args (e.g., ['-I/inc', '-DCONFIG_X=1'])
      applied to all c/cpp files when no compile_commands entry matches.
    """
    # Resolve extraction backend from profile if not explicitly passed.
    if extraction_backend is None and profile:
        extraction_backend = profile.get("extraction_backend", "auto")
    extraction_backend = extraction_backend or "auto"

    # C/C++ may use a dual backend or clang-only path.
    if lang in ("c", "cpp") and extraction_backend in ("auto", "clang"):
        try:
            from _scanner.clang_scanner import ClangScanner, is_clang_available
            from _scanner.dual_scanner import DualBackendScanner
        except ImportError:
            # Fall back to tree-sitter if dual scanner modules unavailable
            extraction_backend = "tree-sitter"
        else:
            if not is_clang_available():
                extraction_backend = "tree-sitter"

    scanners = {
        "c": lambda: CTreeSitterScanner(is_cpp=False),
        "cpp": lambda: CTreeSitterScanner(is_cpp=True),
        "go": lambda: GoTreeSitterScanner(),
        "python": lambda: PythonTreeSitterScanner(),
        "java": lambda: JavaTreeSitterScanner(),
        "rust": lambda: RustTreeSitterScanner(),
        "asm": lambda: AsmRegexScanner(),
    }
    factory = scanners.get(lang)
    if factory is None:
        raise ValueError(f"Unsupported language: {lang}. Supported: {list(scanners.keys())}")
    scanner = factory()
    # For c/cpp with auto/clang backend, wrap with DualBackendScanner.
    if lang in ("c", "cpp") and extraction_backend in ("auto", "clang"):
        try:
            from _scanner.clang_scanner import ClangScanner
            from _scanner.dual_scanner import DualBackendScanner
            clang_scanner = ClangScanner(
                is_cpp=(lang == "cpp"),
                extra_clang_args=clang_args or [],
                compile_commands_path=compile_commands_path or '',
            )
            if extraction_backend == "clang":
                # clang-only path: return ClangScanner directly
                scanner = clang_scanner
            else:
                scanner = DualBackendScanner(scanner, clang_scanner)
        except ImportError:
            pass  # fall back to tree-sitter scanner already in `scanner`
    # Set C-specific API prefixes for entry detection
    if api_prefixes and lang in ("c", "cpp") and hasattr(scanner, '_api_prefixes'):
        scanner._api_prefixes = api_prefixes
    # Set export macros for API entry detection (e.g., EXPORT_SYMBOL, EXPORT_SYMBOL_GPL)
    if export_macros and lang in ("c", "cpp"):
        scanner._export_macros = export_macros
    # Set public header paths for API entry detection (from profile api_detection.public_header_paths)
    if lang in ("c", "cpp") and hasattr(scanner, '_public_header_paths'):
        _php = profile.get("public_header_paths", []) if profile else []
        if _php:
            scanner._public_header_paths = _php
    # Set project-declared non-API paths (test/, examples/, app/, scripts/, etc.).
    # Functions defined in these paths are never tagged API_entry at scan time,
    # even if they have public-looking names. This complements the scanner's
    # builtin non-API path list (tools/, samples/, etc.) with project specifics.
    if lang in ("c", "cpp", "go", "python") and hasattr(scanner, '_non_api_paths'):
        _nap = profile.get("non_api_paths", []) if profile else []
        if _nap:
            scanner._non_api_paths = _nap
    # Set API prefixes for Rust scanner entry detection
    if api_prefixes and lang == "rust" and hasattr(scanner, '_api_prefixes'):
        scanner._api_prefixes = api_prefixes
    # Set profile callback patterns for concurrency detection in tree-sitter scanner
    # (e.g., kthread_run → spawn_target, INIT_WORK → poller, call_rcu → callback)
    if callback_patterns and lang in ("c", "cpp") and hasattr(scanner, '_callback_patterns'):
        scanner._callback_patterns = {
            pat["register_func"]: (pat["cb_arg_index"], pat["concurrency_type"])
            for pat in callback_patterns
        }
    # Set struct_op_types for vtable detection (e.g., file_operations, inode_operations)
    if struct_op_types and lang in ("c", "cpp") and hasattr(scanner, '_struct_op_types'):
        scanner._struct_op_types = struct_op_types
    # Set macro_dispatch patterns for macro registration detection
    # (e.g., module_init, module_platform_driver, etc.)
    if macro_dispatch_patterns and lang in ("c", "cpp") and hasattr(scanner, '_macro_dispatch_patterns'):
        scanner._macro_dispatch_patterns = {
            pat["macro_name"]: pat for pat in macro_dispatch_patterns
        }
    # O8: propagate fn_ptr_call_require_evidence from profile.dispatch_tuning
    # to the C/C++ scanner. When True, the scanner's identifier-name heuristic
    # for fn_ptr_calls is disabled — only explicit field_expression /
    # pointer_expression indirect calls count as fn_ptr evidence.
    if lang in ("c", "cpp") and hasattr(scanner, '_fn_ptr_call_require_evidence'):
        _dt = (profile or {}).get("dispatch_tuning", {}) or {}
        scanner._fn_ptr_call_require_evidence = bool(_dt.get("fn_ptr_call_require_evidence", False))
    # Set ASM-specific profile settings
    if lang == "asm":
        asm_syntax = profile.get("asm_syntax", "nasm") if profile else "nasm"
        if hasattr(scanner, '_asm_syntax'):
            scanner._asm_syntax = asm_syntax
        arch = profile.get("arch", "x86_64") if profile else "x86_64"
        if hasattr(scanner, '_arch'):
            scanner._arch = arch
        # Set syscall maps from profile (architecture-specific number→name mappings)
        syscall_maps = profile.get("syscall_maps", {}) if profile else {}
        if syscall_maps and hasattr(scanner, 'set_syscall_maps'):
            scanner.set_syscall_maps(syscall_maps)
        # Set asm_entry_macros from profile
        asm_entry_macros = profile.get("asm_entry_macros", []) if profile else []
        if asm_entry_macros and hasattr(scanner, 'set_asm_entry_macros'):
            scanner.set_asm_entry_macros(asm_entry_macros)
    return scanner


def detect_language(filepath: str) -> str:
    """Detect language from file extension."""
    ext = Path(filepath).suffix.lower()
    return EXTENSION_MAP.get(ext, "")


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Checkpoint / resume helpers
# ---------------------------------------------------------------------------
_CHECKPOINT_FILENAME = "_scan_checkpoint.json"
_SCAN_ERRORS_FILENAME = ".code2database_scan_errors.json"


def _write_scan_errors_sidecar(streaming_output: str, errors: list, warnings: list,
                               total_files: int, lang_stats: dict) -> None:
    """Write per-file scan errors/warnings to a sidecar JSON file.

    Placed next to the extraction output so the builder and downstream tools
    can discover it. Silently skips when there's no output path or when the
    error list is empty AND warnings list is empty.
    """
    if not streaming_output:
        return
    if not errors and not warnings:
        return
    sidecar_path = streaming_output + ".errors.json"
    try:
        os.makedirs(os.path.dirname(sidecar_path) or ".", exist_ok=True)
        summary = {
            "total_files": total_files,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "error_kind_counts": {},
            "lang_counts": {},
        }
        for err in errors:
            kind = err.get("error_kind", "unknown")
            summary["error_kind_counts"][kind] = summary["error_kind_counts"].get(kind, 0) + 1
            lang = err.get("lang", "unknown")
            summary["lang_counts"][lang] = summary["lang_counts"].get(lang, 0) + 1
        payload = {"summary": summary, "errors": errors, "warnings": warnings}
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[scan] Wrote {len(errors)} error(s) / {len(warnings)} warning(s) "
              f"to {sidecar_path}", file=sys.stderr)
    except OSError as exc:
        print(f"[scan] Warning: failed to write scan errors sidecar: {exc}",
              file=sys.stderr)


def _save_checkpoint(checkpoint_path: str, source_root: str,
                     completed_files: set, stats: dict) -> None:
    """Save scan checkpoint for resume after interruption."""
    try:
        data = {
            "source_root": source_root,
            "completed_files": sorted(completed_files),
            "timestamp": time.time(),
            "stats": stats,
        }
        os.makedirs(os.path.dirname(checkpoint_path) or ".", exist_ok=True)
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[Checkpoint] Warning: failed to save checkpoint: {e}", file=sys.stderr)


def _load_checkpoint(checkpoint_path: str, source_root: str):
    """Load scan checkpoint. Returns set of completed relative paths or None."""
    try:
        if not os.path.exists(checkpoint_path):
            return None
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Validate source root matches
        if data.get("source_root") != source_root:
            return None
        completed = set(data.get("completed_files", []))
        if completed:
            print(f"[Checkpoint] Found checkpoint: {len(completed)} files already processed",
                  file=sys.stderr)
        return completed
    except Exception:
        return None


def _remove_checkpoint(checkpoint_path: str) -> None:
    """Remove checkpoint file after successful completion."""
    try:
        if checkpoint_path and os.path.exists(checkpoint_path):
            os.remove(checkpoint_path)
    except OSError:
        pass


def _extract_state_access_then_drop_body_text(functions: list,
                                              globals_data: dict,
                                              field_assignments: list) -> int:
    """Extract state_access from body_text, then drop body_text to free memory.

    This must run BEFORE any drop_body_text call. Once body_text is gone,
    the builder cannot recover fields_read/fields_written/globals_read/
    globals_written, and the field_access / global_access SQLite tables
    end up empty for large projects that hit memory pressure during scan.

    Idempotent: skips functions that already have state_access populated.

    Returns the number of functions that had body_text dropped.
    """
    try:
        from _builder.graph_build import _extract_state_access
    except ImportError:
        # Builder module unavailable — just drop body_text without state_access.
        dropped = 0
        for _func in functions:
            if _func.get("body_text"):
                del _func["body_text"]
                dropped += 1
        return dropped

    # Build per-build cache for global var names
    _gv_names = {}
    for _gv in (globals_data or {}).get("global_vars", []):
        _gn = _gv.get("name", "")
        if _gn:
            _gv_names[_gn] = _gv
    _cached_g = None
    if _gv_names:
        import re as _re
        _assign_ops = _re.compile(
            r'\b(' + '|'.join(_re.escape(gn) for gn in
                              sorted(_gv_names.keys(),
                                     key=len, reverse=True))
            + r')\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*[^=]'
        )
        _cached_g = {
            "var_names": _gv_names,
            "var_names_keys": set(_gv_names.keys()),
            "assign_ops_re": _assign_ops,
        }

    _sa_count = 0
    _cand_count = 0
    _dropped = 0
    _fa_list = field_assignments if isinstance(field_assignments, list) else []
    for _func in functions:
        _body = _func.get("body_text", "")
        if not _body:
            continue
        # Idempotent: skip if state_access already populated
        if _func.get("fields_read") or _func.get("fields_written") \
           or _func.get("globals_read") or _func.get("globals_written"):
            del _func["body_text"]
            _dropped += 1
            continue
        _cand_count += 1
        _ai = _extract_state_access(
            _body,
            _func.get("local_vars", []),
            _func.get("params", []),
            globals_data or {},
            _fa_list,
            _func.get("name", ""),
            _cached_globals=_cached_g)
        _had = False
        for _k in ("globals_read", "globals_written",
                   "fields_read", "fields_written"):
            _v = _ai.get(_k, [])
            if _v:
                _func[_k] = _v
                _had = True
        if _had:
            _sa_count += 1
        del _func["body_text"]
        _dropped += 1

    if _cand_count:
        print(f"[MemoryGuard] Pre-drop state_access: extracted from "
              f"{_sa_count}/{_cand_count} function(s)", file=sys.stderr)
    # Release per-build cache before returning
    del _cached_g, _gv_names
    import gc as _gc
    _gc.collect()
    return _dropped


def scan_directory(source_root: str, lang: str = "auto",
                   macro_bindings: dict = None, workers: int = 0,
                   api_prefixes: list = None,
                   profile: dict = None,
                   memory_guard=None,
                   streaming_output: str = None,
                   memory_limit_gb: float = 0,
                   max_functions: int = 0,
                   exclude_dirs: list = None,
                   progress_callback=None,
                   quiet: bool = False,
                   split_output: bool = False,
                   no_body_text: bool = False,
                   extraction_backend: str = None,
                   compile_commands_path: str = None,
                   clang_args: list = None,
                   scan_subsystems: list = None) -> dict:
    """Scan all source files in a directory tree.

    Args:
        workers: Number of parallel workers (0=auto, 1=sequential).
                 Auto uses min(cpu_count, 8).
        api_prefixes: Public API prefixes for C/C++ entry detection.
        profile: Scanner config dict from ProfileSchema.to_scanner_config().
        memory_guard: Optional MemoryGuard instance for memory management.
        streaming_output: If set, path to write results incrementally via
                          StreamingJsonObjectWriter instead of accumulating
                          all data in memory. Returns lightweight metadata dict.
        memory_limit_gb: Stop scanning gracefully when RSS approaches this
                         limit in GB (0=no limit, auto-detect from system).
        max_functions: Stop scanning after extracting this many functions
                       (0=no limit).
        progress_callback: Optional callable invoked every N files with
                           (files_processed, total_files, current_file).
                           When None and quiet is False, prints to stderr.
        quiet: If True, suppress progress output to stderr.
        scan_subsystems: RPT-KERNEL-D9 — optional list of subsystem names
                         (e.g., ['fs', 'mm', 'block', 'kernel', 'lib']) to
                         restrict the scan to. Files whose path (relative to
                         source_root) starts with `<subsystem>/` are kept;
                         all others are filtered out. None or empty list =
                         scan everything (default behavior).
    """
    # Default to sequential scanning if memory guard indicates critical memory
    _force_sequential = False
    if memory_guard and memory_guard.is_memory_critical():
        _force_sequential = True
        print(f"[MemoryGuard] Forcing sequential mode due to memory pressure", file=sys.stderr)

    all_functions = []
    all_edges = []
    all_import_edges = []
    all_globals = {"enums": [], "constants": [], "typedefs": [], "global_vars": []}
    all_vtable_registrations = []
    all_macro_registrations = []
    all_token_paste_functions = []
    all_container_of_usages = []
    all_conversion_funcs = []
    all_struct_defs = []
    all_fn_ptr_calls = {}
    all_passthrough_reg_funcs = {}
    all_field_assignments = []
    # cgdb (code graph database) 13-layer records — aggregated from clang/dual
    # scanner output, written to cgdb tables by the builder.
    all_cgdb_nodes = []
    all_cgdb_types = []
    all_cgdb_edges = []
    all_cgdb_invoke_sites = []
    all_cgdb_predicates = []  # L3.5: #ifdef predicate trees
    all_cgdb_ops_bindings = []  # L7: typed vtable dispatch bindings
    all_cgdb_basic_blocks = []  # L4: CFG basic blocks
    all_cgdb_cfg_edges = []  # L4: CFG edges
    all_cgdb_data_flow = []  # L5: def-use chains
    all_cgdb_sync_primitives = []  # L8: lock/rcu/atomic ops
    all_cgdb_happens_before = []  # L8: happens-before relations
    all_cgdb_alias_sets = []  # L6: pointer alias sets
    all_cgdb_doc_comments = []  # L10: doc comments per node
    all_cgdb_metadata = []  # L10: per-node metadata (e.g. metrics)
    all_cgdb_includes = []  # L9: #include / import dependency graph
    all_conditions = []  # L3: preprocessor conditions / config branches
    all_scan_errors = []  # Per-file errors: [{file, error, error_kind, lang}]
    all_scan_warnings = []  # Per-file non-fatal warnings
    domains = set()
    lang_stats = defaultdict(int)

    # Memory stats tracking
    _scan_start_time = time.time()
    _last_report_time = _scan_start_time
    _processed_files = 0
    _report_interval = 50  # Report progress every N files

    # Default progress callback: print to stderr unless quiet
    if progress_callback is None and not quiet:
        def _default_progress_callback(processed, total, current_file):
            pct = int(processed * 100 / total) if total > 0 else 0
            print(f"[scan] {processed}/{total} files ({pct}%) — processing {current_file}",
                  file=sys.stderr)
        progress_callback = _default_progress_callback

    # Collect all source files first
    file_list = []
    # Directories to skip during scanning (generated/build artifacts, VCS, dependencies)
    _SKIP_DIRS = frozenset({
        '__pycache__', 'node_modules', '.git', '.svn', '.hg',
        'build', 'dist', 'out', 'bin', 'obj',
        'venv', '.venv', '.env',
        '.tox', '.mypy_cache', '.pytest_cache',
        'target',  # Rust/Java
        'CMakeFiles', 'cmake-build-debug', 'cmake-build-release',
        '.cache',  # ccache, etc.
        'third_party', 'vendor', 'external', '3rdparty', 'deps', 'contrib',
    })
    _skip_dirs = _SKIP_DIRS
    if exclude_dirs:
        _skip_dirs = _SKIP_DIRS | frozenset(exclude_dirs)
    for dirpath, dirnames, filenames in os.walk(source_root):
        # Skip hidden directories, build artifacts, VCS, and dependency dirs
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in _skip_dirs]
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            ext = Path(fpath).suffix.lower()
            file_lang = lang if lang != "auto" else detect_language(fpath)
            if not file_lang:
                continue
            if ext not in LANG_EXTENSIONS.get(file_lang, set()):
                if lang != "auto":
                    continue
            file_list.append((fpath, file_lang))

    # RPT-KERNEL-D9: subsystem filter — restrict scan to specified
    # top-level subsystem directories (e.g., ['fs', 'mm', 'block',
    # 'kernel', 'lib'] for cross-subsystem Linux kernel analysis).
    # Paths are matched relative to source_root using forward slashes.
    if scan_subsystems:
        _subsystem_set = {s.strip('/') for s in scan_subsystems if s.strip()}
        if _subsystem_set:
            source_root_abs = os.path.abspath(source_root)
            filtered = []
            for fpath, flang in file_list:
                rel = os.path.relpath(os.path.abspath(fpath), source_root_abs)
                rel = rel.replace(os.sep, '/')
                top = rel.split('/', 1)[0]
                if top in _subsystem_set:
                    filtered.append((fpath, flang))
            _filtered_count = len(file_list) - len(filtered)
            file_list = filtered
            if not quiet:
                print(f"[scan] Subsystem filter {sorted(_subsystem_set)}: "
                      f"kept {len(file_list)} / {len(file_list) + _filtered_count} "
                      f"files (dropped {_filtered_count})", file=sys.stderr)

    # Auto-detect large project: if > 10000 source files, treat as large.
    # This auto-enables split-output for better memory management.
    _auto_large = len(file_list) > 10000
    if _auto_large and not split_output:
        split_output = True
        print(f"[scan] Auto-detected large project ({len(file_list)} source files), "
              f"enabling split output", file=sys.stderr)

    # Determine split-output mode early so scan loop can use it for periodic flushing
    # Auto-generate streaming_output path when split_output is requested but no output path given
    if split_output and not streaming_output:
        streaming_output = os.path.join(source_root, "..",
                                        os.path.basename(source_root) + "_extraction.json")
    _use_split = (streaming_output and (len(file_list) > 5000 or split_output))
    # Adaptive flush interval: for small/medium projects, flush more aggressively
    # so progress is visible and memory stays bounded. For large projects, the
    # 2000-file interval keeps per-flush overhead low.
    _n_files = len(file_list)
    if _n_files <= 200:
        _FLUSH_INTERVAL = 50
    elif _n_files <= 2000:
        _FLUSH_INTERVAL = 200
    else:
        _FLUSH_INTERVAL = 2000
    _PAR_FLUSH_INTERVAL = max(50, _FLUSH_INTERVAL // 4)
    _BATCH_SUBMIT_SIZE = 5000  # Submit futures in batches to reduce memory pressure
    _split_dir = (streaming_output + ".d") if _use_split else None

    # Define streaming helper functions early so scan loop can call _flush_accumulated
    if _use_split:
        _flush_chunk = [0]  # mutable counter for chunk numbering

        def _flush_accumulated():
            """Flush accumulated scan data to disk and release memory.
            Called periodically during scanning to keep memory bounded.
            Writes data as numbered chunk files to avoid slow read-modify-write."""
            nonlocal all_functions, all_edges, all_import_edges
            nonlocal all_vtable_registrations, all_macro_registrations
            nonlocal all_token_paste_functions, all_container_of_usages
            nonlocal all_conversion_funcs, all_struct_defs
            nonlocal all_fn_ptr_calls, all_passthrough_reg_funcs
            nonlocal all_field_assignments, all_globals

            os.makedirs(os.path.join(_split_dir, "functions"), exist_ok=True)
            os.makedirs(os.path.join(_split_dir, "edges"), exist_ok=True)
            os.makedirs(os.path.join(_split_dir, "cgdb"), exist_ok=True)

            _chunk = _flush_chunk[0]
            _flush_chunk[0] += 1

            # Extract state_access from body_text BEFORE writing to disk.
            # The builder relies on fields_read/fields_written/globals_read/
            # globals_written to populate the field_access and global_access
            # SQLite tables. Without this extraction at flush time, the
            # builder's tables end up empty for split-output scans (kernel,
            # SPDK, etc.) — the Pre-OOM callback can't help here because
            # all_functions is cleared after each flush.
            # Keep body_text in the output (the builder uses it for many
            # purposes, including describe-node --detail full).
            if all_functions:
                try:
                    from _builder.graph_build import _extract_state_access
                    _gv_names = {}
                    for _gv in all_globals.get("global_vars", []):
                        _gn = _gv.get("name", "")
                        if _gn:
                            _gv_names[_gn] = _gv
                    _cached_g = None
                    if _gv_names:
                        import re as _re
                        _assign_ops = _re.compile(
                            r'\b(' + '|'.join(_re.escape(gn) for gn in
                                              sorted(_gv_names.keys(),
                                                     key=len, reverse=True))
                            + r')\s*(\+|-|\*|\/|\||\&|\^|\%|<<|>>)?=\s*[^=]'
                        )
                        _cached_g = {
                            "var_names": _gv_names,
                            "var_names_keys": set(_gv_names.keys()),
                            "assign_ops_re": _assign_ops,
                        }
                    _fa_list = all_field_assignments if isinstance(all_field_assignments, list) else []
                    _sa_count = 0
                    _cand_count = 0
                    for _func in all_functions:
                        _body = _func.get("body_text", "")
                        if not _body:
                            continue
                        if _func.get("fields_read") or _func.get("fields_written") \
                           or _func.get("globals_read") or _func.get("globals_written"):
                            continue
                        _cand_count += 1
                        _ai = _extract_state_access(
                            _body,
                            _func.get("local_vars", []),
                            _func.get("params", []),
                            all_globals,
                            _fa_list,
                            _func.get("name", ""),
                            _cached_globals=_cached_g)
                        _had = False
                        for _k in ("globals_read", "globals_written",
                                   "fields_read", "fields_written"):
                            _v = _ai.get(_k, [])
                            if _v:
                                _func[_k] = _v
                                _had = True
                        if _had:
                            _sa_count += 1
                    if _cand_count:
                        print(f"[flush] Pre-write state_access: extracted from "
                              f"{_sa_count}/{_cand_count} function(s) for chunk {_chunk}",
                              file=sys.stderr)
                    del _cached_g, _gv_names
                except Exception as _e:
                    print(f"[flush] state_access extraction failed for chunk {_chunk}: {_e}",
                          file=sys.stderr)

            # Group functions by domain and write per-domain chunk files
            _funcs_by_domain = {}
            for func in all_functions:
                dom = func.get("domain", "unknown")
                _funcs_by_domain.setdefault(dom, []).append(func)
            for dom, funcs in _funcs_by_domain.items():
                _safe_name = dom.replace("/", "_").replace(".", "_")
                Path(os.path.join(_split_dir, "functions", f"{_safe_name}_{_chunk}.json")).write_text(
                    json.dumps(funcs, ensure_ascii=False), encoding="utf-8")
            all_functions.clear()

            # Write edges as numbered chunk files (no domain grouping needed;
            # the builder reads all edge files regardless of name)
            Path(os.path.join(_split_dir, "edges", f"edges_{_chunk}.json")).write_text(
                json.dumps(all_edges, ensure_ascii=False), encoding="utf-8")
            all_edges.clear()

            # Write import edges chunk
            Path(os.path.join(_split_dir, f"import_edges_{_chunk}.json")).write_text(
                json.dumps(all_import_edges, ensure_ascii=False), encoding="utf-8")
            all_import_edges.clear()

            # Write globals chunk
            Path(os.path.join(_split_dir, f"globals_{_chunk}.json")).write_text(
                json.dumps(all_globals, ensure_ascii=False), encoding="utf-8")
            all_globals = {"enums": [], "constants": [], "typedefs": [], "global_vars": []}

            # Write other list-type field chunks
            for name, items in [
                ("vtable_registrations", all_vtable_registrations),
                ("macro_registrations", all_macro_registrations),
                ("token_paste_functions", all_token_paste_functions),
                ("container_of_usages", all_container_of_usages),
                ("conversion_funcs", all_conversion_funcs),
                ("struct_defs", all_struct_defs),
                ("field_assignments", all_field_assignments),
            ]:
                Path(os.path.join(_split_dir, f"{name}_{_chunk}.json")).write_text(
                    json.dumps(items, ensure_ascii=False), encoding="utf-8")

            all_vtable_registrations.clear()
            all_macro_registrations.clear()
            all_token_paste_functions.clear()
            all_container_of_usages.clear()
            all_conversion_funcs.clear()
            all_struct_defs.clear()
            all_field_assignments.clear()

            # Write dict-type field chunks
            Path(os.path.join(_split_dir, f"fn_ptr_calls_{_chunk}.json")).write_text(
                json.dumps(all_fn_ptr_calls, ensure_ascii=False), encoding="utf-8")
            Path(os.path.join(_split_dir, f"passthrough_reg_funcs_{_chunk}.json")).write_text(
                json.dumps(all_passthrough_reg_funcs, ensure_ascii=False), encoding="utf-8")
            all_fn_ptr_calls.clear()
            all_passthrough_reg_funcs.clear()

            # Flush cgdb layer accumulators — without this, scanning kernel-scale
            # projects accumulates 20+GB of cgdb_nodes/cgdb_edges/cgdb_data_flow/
            # cgdb_basic_blocks in memory even with --split-output, because the
            # cgdb layer produces ~10x more records than the legacy function
            # layer (one cgdb_node per AST node, one cgdb_edge per AST edge).
            # This was the root cause of kernel scan OOM at 24GB after only
            # 1300 C files.
            for name, items in [
                ("cgdb_nodes", all_cgdb_nodes),
                ("cgdb_types", all_cgdb_types),
                ("cgdb_edges", all_cgdb_edges),
                ("cgdb_invoke_sites", all_cgdb_invoke_sites),
                ("cgdb_predicates", all_cgdb_predicates),
                ("cgdb_ops_bindings", all_cgdb_ops_bindings),
                ("cgdb_basic_blocks", all_cgdb_basic_blocks),
                ("cgdb_cfg_edges", all_cgdb_cfg_edges),
                ("cgdb_data_flow", all_cgdb_data_flow),
                ("cgdb_sync_primitives", all_cgdb_sync_primitives),
                ("cgdb_happens_before", all_cgdb_happens_before),
                ("cgdb_alias_sets", all_cgdb_alias_sets),
                ("cgdb_doc_comments", all_cgdb_doc_comments),
                ("cgdb_metadata", all_cgdb_metadata),
                ("cgdb_includes", all_cgdb_includes),
                ("conditions", all_conditions),
            ]:
                if not items:
                    continue
                Path(os.path.join(_split_dir, "cgdb", f"{name}_{_chunk}.json")).write_text(
                    json.dumps(items, ensure_ascii=False), encoding="utf-8")
                items.clear()

            gc.collect()
    else:
        # Dummy flush for non-split mode (no-op)
        def _flush_accumulated():
            pass

    # Checkpoint / resume support
    _checkpoint_dir = os.path.dirname(streaming_output) if streaming_output else None
    if not _checkpoint_dir and memory_limit_gb > 0:
        _checkpoint_dir = "."  # fallback for memory-limited scans
    _checkpoint_path = os.path.join(_checkpoint_dir, _CHECKPOINT_FILENAME) if _checkpoint_dir else None
    _completed_files = set()
    _scan_stopped_early = False

    if _checkpoint_path:
        _loaded = _load_checkpoint(_checkpoint_path, source_root)
        if _loaded:
            _completed_files = _loaded
            # Filter out already-processed files
            _before = len(file_list)
            file_list = [(fp, fl) for fp, fl in file_list
                         if os.path.relpath(fp, source_root) not in _completed_files]
            _skipped = _before - len(file_list)
            if _skipped > 0:
                print(f"[Checkpoint] Resuming: skipping {_skipped} already-processed files",
                      file=sys.stderr)

    # Auto-detect memory limit from system if not specified
    if memory_limit_gb < 0:
        memory_limit_gb = 0
    if memory_limit_gb == 0 and memory_guard:
        try:
            info = memory_guard.get_memory_info()
            if info["total_mb"] > 0:
                # Default to 80% of total RAM
                memory_limit_gb = info["total_mb"] / 1024 * 0.80
        except Exception:
            pass

    # Pre-scan: collect function-like macro names from all header files.
    # Macros like #define ftl_bug(cond) expand inline and should NOT create
    # INVOKES edges. We must collect these BEFORE the main scan so that
    # effective_skip in scan_c_file can filter them out.
    # For very large projects (>50K headers), sample only a subset to avoid
    # reading GB of headers into memory.
    try:
        from _vendor._regex_c_scanner import _FUNC_MACRO_DEF_RE, _PROJECT_FUNC_MACROS
        _macro_count = 0
        _header_files = [(fp, fl) for fp, fl in file_list if fp.endswith('.h')]
        _max_headers = 50000  # Limit header pre-scan for memory safety
        if len(_header_files) > _max_headers:
            print(f"[scan] Large project: sampling {_max_headers}/{len(_header_files)} headers "
                  f"for macro pre-scan", file=sys.stderr)
            import random
            _header_files = random.sample(_header_files, _max_headers)
        for fpath, file_lang in _header_files:
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as mf:
                    msrc = mf.read()
                for mm in _FUNC_MACRO_DEF_RE.finditer(msrc):
                    mname = mm.group(1)
                    # Only cache non-ALL-UPPER macros (ALL-UPPER are already
                    # in _SKIP_NAMES as macro patterns)
                    if not mname.isupper():
                        _PROJECT_FUNC_MACROS.add(mname)
                        _macro_count += 1
            except (IOError, OSError):
                pass
            # Periodic memory check during header pre-scan
            if _macro_count > 0 and _macro_count % 10000 == 0 and memory_guard:
                memory_guard.maybe_gc(force=False)
        if _macro_count:
            import sys as _sys
            _sys.stdout.write(f"  Pre-scanned headers: found {_macro_count} function-like macros\n")
    except ImportError:
        pass

    # Pre-create scanner FACTORIES (not instances) for thread-safety.
    # tree-sitter Parser.parse() is NOT thread-safe — sharing one parser
    # across threads corrupts internal state.  Each worker must get its
    # own scanner instance.
    _export_macros = profile.get("export_macros", []) if profile else []
    _callback_patterns = profile.get("callback_patterns", []) if profile else []
    # Fallback: if callback_patterns is empty but callback_detection.static_patterns
    # exists (same format with register_func, cb_arg_index, concurrency_type),
    # use it so the scanner can detect profile-driven callback patterns.
    if not _callback_patterns and profile:
        _callback_patterns = profile.get("callback_detection", {}).get("static_patterns", [])
    _struct_op_types = profile.get("struct_op_types", []) if profile else []
    _macro_dispatch_patterns = profile.get("macro_dispatch_patterns", []) if profile else []
    # Fallback: if macro_dispatch_patterns is empty but macro_dispatch.registration_macros
    # exists (nested format from raw profile), use it.
    if not _macro_dispatch_patterns and profile:
        _macro_dispatch_patterns = profile.get("macro_dispatch", {}).get("registration_macros", [])
    scanner_factories = {}   # lang → callable() → scanner instance
    for file_lang in set(fl for _, fl in file_list):
        try:
            # Capture the factory closure with api_prefixes, export_macros, callback_patterns, struct_op_types
            scanner_factories[file_lang] = (lambda _lang=file_lang: get_scanner(_lang, api_prefixes=api_prefixes, export_macros=_export_macros, callback_patterns=_callback_patterns, struct_op_types=_struct_op_types, macro_dispatch_patterns=_macro_dispatch_patterns, profile=profile, extraction_backend=extraction_backend, compile_commands_path=compile_commands_path, clang_args=clang_args))
        except (ValueError, ImportError):
            if file_lang in ("c", "cpp"):
                from _vendor._regex_c_scanner import scan_c_file
                scanner_factories[file_lang] = ("regex_fallback", scan_c_file)
            else:
                continue

    # Thread-local scanner cache: each thread creates its own instances
    import threading
    _tls = threading.local()

    def _get_scanner(file_lang):
        """Get a thread-local scanner instance for the given language."""
        # Regex fallback scanners are stateless — safe to share
        factory = scanner_factories.get(file_lang)
        if factory is None:
            return None
        if isinstance(factory, tuple) and factory[0] == "regex_fallback":
            return factory  # ("regex_fallback", scan_c_file)

        if not hasattr(_tls, 'scanners'):
            _tls.scanners = {}
        if file_lang not in _tls.scanners:
            try:
                _tls.scanners[file_lang] = factory()
            except ImportError:
                # tree-sitter not installed — fall back to regex scanner for C/C++
                if file_lang in ("c", "cpp"):
                    from _vendor._regex_c_scanner import scan_c_file
                    scanner_factories[file_lang] = ("regex_fallback", scan_c_file)
                    return ("regex_fallback", scan_c_file)
                return None
        return _tls.scanners[file_lang]

    # Determine worker count
    if workers == 0:
        import multiprocessing
        workers = min(multiprocessing.cpu_count(), 8)
    if workers < 2 or len(file_list) < 2:
        workers = 1

    def _scan_one(item):
        fpath, file_lang = item
        scanner = _get_scanner(file_lang)
        if scanner is None:
            return None
        # Increase recursion limit for deeply nested ASTs (e.g., Linux kernel macros)
        # Will be restored after scanning
        _old_limit = sys.getrecursionlimit()
        if _old_limit < 5000:
            sys.setrecursionlimit(5000)
        try:
            if isinstance(scanner, tuple) and scanner[0] == "regex_fallback":
                result = scanner[1](fpath, source_root, api_prefixes=api_prefixes,
                                     profile=profile)
            else:
                result = scanner.scan_file(fpath, source_root, macro_bindings=macro_bindings)
            return result
        except RecursionError:
            print(f"[MemoryGuard] RecursionError scanning {fpath} - skipping", file=sys.stderr)
            sys.setrecursionlimit(_old_limit)
            return None
        except MemoryError:
            print(f"[MemoryGuard] MemoryError scanning {fpath} - skipping", file=sys.stderr)
            sys.setrecursionlimit(_old_limit)
            return None
        except Exception:
            return None

    if workers > 1:
        # Multi-threaded scanning — each thread gets its own tree-sitter
        # parser instance via _get_scanner() for thread-safety.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Submit futures in batches to avoid holding all 709K Future objects at once
            _total_files = len(file_list)
            _par_processed = 0
            _active_futures = {}  # Only track currently-running futures
            _file_idx = 0
            _draining = False

            while _file_idx < _total_files or _active_futures:
                # Submit a batch of futures
                if not _draining:
                    _batch_end = min(_file_idx + _BATCH_SUBMIT_SIZE, _total_files)
                    while _file_idx < _batch_end:
                        item = file_list[_file_idx]
                        fut = pool.submit(_scan_one, item)
                        _active_futures[fut] = item
                        _file_idx += 1

                # Wait for at least one to complete
                if not _active_futures:
                    break

                # Process completed futures
                _done_futures = set()
                for fut in list(_active_futures.keys()):
                    if fut.done():
                        _done_futures.add(fut)

                if not _done_futures:
                    # Nothing done yet, wait briefly for next completion
                    import concurrent.futures
                    try:
                        done, _ = concurrent.futures.wait(
                            _active_futures.keys(),
                            timeout=1.0,
                            return_when=concurrent.futures.FIRST_COMPLETED)
                        _done_futures = done
                    except Exception:
                        continue

                for future in _done_futures:
                    del _active_futures[future]
                    _par_processed += 1
                    try:
                        result = future.result()
                    except Exception:
                        continue
                    if progress_callback and _par_processed % _report_interval == 0:
                        item = _active_futures.get(future, file_list[_par_processed - 1])
                        progress_callback(_par_processed, _total_files,
                                          os.path.relpath(item[0], source_root))
                    if result is None:
                        continue
                    if result.get("error"):
                        all_scan_errors.append({
                            "file": result.get("file", item[0]),
                            "lang": item[1],
                            "error": result["error"],
                            "error_kind": result.get("error_kind", "unknown"),
                        })
                        continue
                    if result.get("warning"):
                        all_scan_warnings.append({
                            "file": result.get("file", item[0]),
                            "lang": item[1],
                            "warning": result["warning"],
                        })
                    all_functions.extend(result.get("functions", []))
                    if no_body_text:
                        for f in all_functions[-len(result.get("functions", [])):]:
                            f.pop("body_text", None)
                    all_edges.extend(result.get("edges", []))
                    all_import_edges.extend(result.get("import_edges", []))
                    for key in ("enums", "constants", "typedefs", "global_vars"):
                        all_globals[key].extend(result.get("globals", {}).get(key, []))
                    # Aggregate vtable registrations
                    vregs = result.get("vtable_registrations", [])
                    if vregs:
                        all_vtable_registrations.extend(vregs)
                    # Aggregate macro registrations
                    mregs = result.get("macro_registrations", [])
                    if mregs:
                        all_macro_registrations.extend(mregs)
                    tpfs = result.get("token_paste_functions", [])
                    if tpfs:
                        all_token_paste_functions.extend(tpfs)
                    # Aggregate container_of usages and struct embedding data
                    co_usages = result.get("container_of_usages", [])
                    if co_usages:
                        all_container_of_usages.extend(co_usages)
                    conv_funcs = result.get("conversion_funcs", [])
                    if conv_funcs:
                        all_conversion_funcs.extend(conv_funcs)
                    s_defs = result.get("struct_defs", [])
                    if s_defs:
                        all_struct_defs.extend(s_defs)
                    fnpc = result.get("fn_ptr_calls", {})
                    for caller, calls in fnpc.items():
                        if caller not in all_fn_ptr_calls:
                            all_fn_ptr_calls[caller] = []
                        all_fn_ptr_calls[caller].extend(calls)
                    # Aggregate passthrough registration functions
                    pt_funcs = result.get("passthrough_reg_funcs", {})
                    for func_name, info in pt_funcs.items():
                        if func_name not in all_passthrough_reg_funcs:
                            all_passthrough_reg_funcs[func_name] = info
                    # Aggregate field assignments
                    fa = result.get("field_assignments", [])
                    if fa:
                        all_field_assignments.extend(fa)
                    # Aggregate cgdb records (from clang/dual scanner)
                    if result.get("cgdb_nodes"):
                        all_cgdb_nodes.extend(result["cgdb_nodes"])
                    if result.get("cgdb_types"):
                        all_cgdb_types.extend(result["cgdb_types"])
                    if result.get("cgdb_edges"):
                        all_cgdb_edges.extend(result["cgdb_edges"])
                    if result.get("cgdb_invoke_sites"):
                        all_cgdb_invoke_sites.extend(result["cgdb_invoke_sites"])
                    if result.get("cgdb_predicates"):
                        all_cgdb_predicates.extend(result["cgdb_predicates"])
                    if result.get("cgdb_ops_bindings"):
                        all_cgdb_ops_bindings.extend(result["cgdb_ops_bindings"])
                    if result.get("cgdb_basic_blocks"):
                        all_cgdb_basic_blocks.extend(result["cgdb_basic_blocks"])
                    if result.get("cgdb_cfg_edges"):
                        all_cgdb_cfg_edges.extend(result["cgdb_cfg_edges"])
                    if result.get("cgdb_data_flow"):
                        all_cgdb_data_flow.extend(result["cgdb_data_flow"])
                    if result.get("cgdb_sync_primitives"):
                        all_cgdb_sync_primitives.extend(result["cgdb_sync_primitives"])
                    if result.get("cgdb_happens_before"):
                        all_cgdb_happens_before.extend(result["cgdb_happens_before"])
                    if result.get("cgdb_alias_sets"):
                        all_cgdb_alias_sets.extend(result["cgdb_alias_sets"])
                    if result.get("cgdb_doc_comments"):
                        all_cgdb_doc_comments.extend(result["cgdb_doc_comments"])
                    if result.get("cgdb_metadata"):
                        all_cgdb_metadata.extend(result["cgdb_metadata"])
                    if result.get("cgdb_includes"):
                        all_cgdb_includes.extend(result["cgdb_includes"])
                    if result.get("conditions"):
                        all_conditions.extend(result["conditions"])
                    if result.get("cgdb_conditions"):
                        all_conditions.extend(result["cgdb_conditions"])
                    if result.get("domain"):
                        domains.add(result["domain"])
                    item = _active_futures.get(future, file_list[_par_processed - 1])
                    lang_stats[item[1]] += 1

                # Periodic flush in parallel mode — keep memory bounded
                if _use_split and _par_processed % _PAR_FLUSH_INTERVAL < len(_done_futures):
                    _flush_accumulated()

                # Check memory limit in parallel mode
                if memory_guard:
                    _mem_info = memory_guard.get_memory_info()
                    if memory_limit_gb > 0 and _mem_info["used_mb"] / 1024 >= memory_limit_gb * 0.90:
                        if _use_split:
                            # Flush accumulated data to disk and continue scanning
                            print(f"[MemoryGuard] Memory at {_mem_info['used_mb']/1024:.1f}GB/{memory_limit_gb:.1f}GB "
                                  f"— flushing to disk to continue scanning", file=sys.stderr)
                            _flush_accumulated()
                            gc.collect()
                            # Check again after flush
                            _mem_info2 = memory_guard.get_memory_info()
                            if _mem_info2["used_mb"] / 1024 >= memory_limit_gb * 0.95:
                                print(f"[MemoryGuard] Memory still critical after flush "
                                      f"({_mem_info2['used_mb']/1024:.1f}GB). Stopping scan.",
                                      file=sys.stderr)
                                _scan_stopped_early = True
                                _draining = True
                                pool.shutdown(wait=False, cancel_futures=True)
                                break
                        else:
                            print(f"[MemoryGuard] Memory limit approaching: "
                                  f"{_mem_info['used_mb']/1024:.1f}GB/{memory_limit_gb:.1f}GB. "
                                  f"Cancelling remaining scans.", file=sys.stderr)
                            _scan_stopped_early = True
                            _draining = True
                            pool.shutdown(wait=False, cancel_futures=True)
                            break

                    # Check max functions limit in parallel mode
                    if max_functions > 0 and len(all_functions) >= max_functions:
                        print(f"[MemoryGuard] Max functions limit reached: "
                              f"{len(all_functions)}/{max_functions}. Cancelling remaining scans.",
                              file=sys.stderr)
                        _scan_stopped_early = True
                        _draining = True
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
    else:
        # Sequential scanning
        _total_files = len(file_list)
        _old_limit = sys.getrecursionlimit()
        if _old_limit < 5000:
            sys.setrecursionlimit(5000)
        for idx, (fpath, file_lang) in enumerate(file_list):
            scanner = _get_scanner(file_lang)
            if scanner is None:
                continue
            try:
                if isinstance(scanner, tuple) and scanner[0] == "regex_fallback":
                    result = scanner[1](fpath, source_root, api_prefixes=api_prefixes,
                                         profile=profile)
                else:
                    result = scanner.scan_file(fpath, source_root, macro_bindings=macro_bindings)
            except RecursionError:
                print(f"[MemoryGuard] RecursionError scanning {fpath} - skipping", file=sys.stderr)
                continue
            except MemoryError:
                print(f"[MemoryGuard] MemoryError scanning {fpath} - skipping", file=sys.stderr)
                continue
            except Exception as _scan_exc:
                # O_fix: sequential path must catch all exceptions like the
                # parallel path does (line 580). Without this, a single
                # buggy file crashes the entire large-project scan.
                print(f"[scan] Error scanning {fpath}: {_scan_exc} - skipping",
                      file=sys.stderr)
                continue
            if result.get("error"):
                all_scan_errors.append({
                    "file": fpath,
                    "lang": file_lang,
                    "error": result["error"],
                    "error_kind": result.get("error_kind", "unknown"),
                })
                continue
            if result.get("warning"):
                all_scan_warnings.append({
                    "file": fpath,
                    "lang": file_lang,
                    "warning": result["warning"],
                })
            all_functions.extend(result.get("functions", []))
            if no_body_text:
                for f in all_functions[-len(result.get("functions", [])):]:
                    f.pop("body_text", None)
            all_edges.extend(result.get("edges", []))
            all_import_edges.extend(result.get("import_edges", []))
            for key in ("enums", "constants", "typedefs", "global_vars"):
                all_globals[key].extend(result.get("globals", {}).get(key, []))
            # Aggregate vtable registrations
            vregs = result.get("vtable_registrations", [])
            if vregs:
                all_vtable_registrations.extend(vregs)
            # Aggregate macro registrations
            mregs = result.get("macro_registrations", [])
            if mregs:
                all_macro_registrations.extend(mregs)
            tpfs = result.get("token_paste_functions", [])
            if tpfs:
                all_token_paste_functions.extend(tpfs)
            # Aggregate container_of usages and struct embedding data
            co_usages = result.get("container_of_usages", [])
            if co_usages:
                all_container_of_usages.extend(co_usages)
            conv_funcs = result.get("conversion_funcs", [])
            if conv_funcs:
                all_conversion_funcs.extend(conv_funcs)
            s_defs = result.get("struct_defs", [])
            if s_defs:
                all_struct_defs.extend(s_defs)
            fnpc = result.get("fn_ptr_calls", {})
            for caller, calls in fnpc.items():
                if caller not in all_fn_ptr_calls:
                    all_fn_ptr_calls[caller] = []
                all_fn_ptr_calls[caller].extend(calls)
            # Aggregate passthrough registration functions
            pt_funcs = result.get("passthrough_reg_funcs", {})
            for func_name, info in pt_funcs.items():
                if func_name not in all_passthrough_reg_funcs:
                    all_passthrough_reg_funcs[func_name] = info
            # Aggregate field assignments
            fa = result.get("field_assignments", [])
            if fa:
                all_field_assignments.extend(fa)
            # Aggregate cgdb records (from clang/dual scanner)
            if result.get("cgdb_nodes"):
                all_cgdb_nodes.extend(result["cgdb_nodes"])
            if result.get("cgdb_types"):
                all_cgdb_types.extend(result["cgdb_types"])
            if result.get("cgdb_edges"):
                all_cgdb_edges.extend(result["cgdb_edges"])
            if result.get("cgdb_invoke_sites"):
                all_cgdb_invoke_sites.extend(result["cgdb_invoke_sites"])
            if result.get("cgdb_predicates"):
                all_cgdb_predicates.extend(result["cgdb_predicates"])
            if result.get("cgdb_ops_bindings"):
                all_cgdb_ops_bindings.extend(result["cgdb_ops_bindings"])
            if result.get("cgdb_basic_blocks"):
                all_cgdb_basic_blocks.extend(result["cgdb_basic_blocks"])
            if result.get("cgdb_cfg_edges"):
                all_cgdb_cfg_edges.extend(result["cgdb_cfg_edges"])
            if result.get("cgdb_data_flow"):
                all_cgdb_data_flow.extend(result["cgdb_data_flow"])
            if result.get("cgdb_sync_primitives"):
                all_cgdb_sync_primitives.extend(result["cgdb_sync_primitives"])
            if result.get("cgdb_happens_before"):
                all_cgdb_happens_before.extend(result["cgdb_happens_before"])
            if result.get("cgdb_alias_sets"):
                all_cgdb_alias_sets.extend(result["cgdb_alias_sets"])
            if result.get("cgdb_doc_comments"):
                all_cgdb_doc_comments.extend(result["cgdb_doc_comments"])
            if result.get("cgdb_metadata"):
                all_cgdb_metadata.extend(result["cgdb_metadata"])
            if result.get("cgdb_includes"):
                all_cgdb_includes.extend(result["cgdb_includes"])
            if result.get("conditions"):
                all_conditions.extend(result["conditions"])
            if result.get("cgdb_conditions"):
                all_conditions.extend(result["cgdb_conditions"])
            if result.get("domain"):
                domains.add(result["domain"])
            lang_stats[file_lang] += 1

            # Track completed file for checkpoint
            if _checkpoint_path:
                _completed_files.add(os.path.relpath(fpath, source_root))

            # Progress reporting and memory management
            _processed_files += 1
            if _processed_files % _report_interval == 0 and progress_callback:
                progress_callback(_processed_files, _total_files,
                                  os.path.relpath(fpath, source_root))

                # Memory guard integration
                if memory_guard:
                    memory_guard.check_and_adapt()
                    # Periodic garbage collection every 500 files
                    if _processed_files % 500 == 0:
                        memory_guard.maybe_gc(force=False)

                # Periodic flush to disk for split-output: keep memory bounded
                # so even 10GB RAM systems can scan 20GB+ source trees
                if _use_split and _processed_files % _FLUSH_INTERVAL == 0:
                    _flush_accumulated()

                # Check memory limit: flush to disk instead of cancelling
                if memory_limit_gb > 0 and memory_guard:
                    _mem_info = memory_guard.get_memory_info()
                    if _mem_info["used_mb"] / 1024 >= memory_limit_gb * 0.90:
                        if _use_split:
                            # Flush accumulated data to disk and continue scanning
                            print(f"[MemoryGuard] Memory at {_mem_info['used_mb']/1024:.1f}GB/{memory_limit_gb:.1f}GB "
                                  f"— flushing to disk to continue scanning", file=sys.stderr)
                            _flush_accumulated()
                            # Extract state_access from body_text BEFORE
                            # dropping it, then drop body_text to free memory.
                            # Without state_access extraction, the field_access
                            # and global_access SQLite tables end up empty
                            # (the builder cannot recover this info from
                            # already-flushed extraction files).
                            if all_functions:
                                dropped = _extract_state_access_then_drop_body_text(
                                    all_functions, all_globals, all_field_assignments)
                                if dropped:
                                    print(f"[MemoryGuard] Dropped body_text from {dropped} in-memory functions "
                                          f"(state_access preserved)", file=sys.stderr)
                            gc.collect()
                            # Check again after flush — only stop if still critical
                            _mem_info2 = memory_guard.get_memory_info()
                            if _mem_info2["used_mb"] / 1024 >= memory_limit_gb * 0.95:
                                print(f"[MemoryGuard] Memory still critical after flush "
                                      f"({_mem_info2['used_mb']/1024:.1f}GB). Stopping scan.",
                                      file=sys.stderr)
                                _scan_stopped_early = True
                                break
                        else:
                            print(f"[MemoryGuard] Memory limit approaching: "
                                  f"{_mem_info['used_mb']/1024:.1f}GB/{memory_limit_gb:.1f}GB. "
                                  f"Stopping scan gracefully.", file=sys.stderr)
                            _completed_files.add(os.path.relpath(fpath, source_root))
                            _scan_stopped_early = True
                            break

                # Check max functions limit
                if max_functions > 0 and len(all_functions) >= max_functions:
                    print(f"[MemoryGuard] Max functions limit reached: "
                          f"{len(all_functions)}/{max_functions}. Stopping scan.",
                          file=sys.stderr)
                    _completed_files.add(os.path.relpath(fpath, source_root))
                    _scan_stopped_early = True
                    break

                # Save checkpoint every 2000 files
                if _checkpoint_path and _processed_files > 0 and _processed_files % 2000 == 0:
                    _save_checkpoint(_checkpoint_path, source_root, _completed_files,
                                     {"functions": len(all_functions),
                                      "edges": len(all_edges)})

    # Checkpoint cleanup: on successful completion, remove checkpoint
    if not _scan_stopped_early:
        _remove_checkpoint(_checkpoint_path)
    else:
        # Save final checkpoint so we can resume later
        if _checkpoint_path:
            _save_checkpoint(_checkpoint_path, source_root, _completed_files,
                             {"functions": len(all_functions),
                              "edges": len(all_edges),
                              "stopped_early": True})

    # For streaming split output: do a final flush of any remaining data
    if _use_split and streaming_output:
        _flush_accumulated()
        # Update metadata with final counts
        _total_func_count = sum(
            len(json.loads(Path(os.path.join(_split_dir, "functions", f)).read_text(encoding="utf-8")))
            for f in os.listdir(os.path.join(_split_dir, "functions"))
            if f.endswith(".json"))
        _meta = {
            "source_root": source_root,
            "domains": sorted(domains),
            "lang_stats": dict(lang_stats),
            "scan_complete": not _scan_stopped_early,
        }
        Path(os.path.join(_split_dir, "_metadata.json")).write_text(
            json.dumps(_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        gc.collect()
        print(f"[scan] Split output written to {_split_dir} ({_total_func_count} functions across {len(domains)} domains)",
              file=sys.stderr)
        _write_scan_errors_sidecar(streaming_output, all_scan_errors, all_scan_warnings,
                                   len(file_list), dict(lang_stats))
        return {
            "source_root": source_root,
            "domains": sorted(domains),
            "lang_stats": dict(lang_stats),
            "_function_count": _total_func_count,
            "_edge_count": 0,  # edges are in per-domain files
            "_streamed_to": streaming_output,
            "_split_dir": _split_dir,
            "_stopped_early": _scan_stopped_early,
            "_scan_error_count": len(all_scan_errors),
            "_scan_warning_count": len(all_scan_warnings),
        }

    # For non-streaming path: disambiguate IDs and detect callbacks in-memory
    # Disambiguate duplicate function IDs
    from _vendor._regex_c_scanner import _disambiguate_func_ids
    all_functions, all_edges = _disambiguate_func_ids(all_functions, all_edges)

    # If stopped early due to max_functions, truncate to requested count
    if max_functions > 0 and len(all_functions) > max_functions:
        _retained_ids = {f.get("id") for f in all_functions[:max_functions]}
        all_functions = all_functions[:max_functions]
        all_edges = [e for e in all_edges
                     if e.get("caller") in _retained_ids or e.get("callee") in _retained_ids]
        print(f"[MemoryGuard] Truncated to {max_functions} functions, "
              f"{len(all_edges)} edges", file=sys.stderr)

    # Post-processing: detect cross-file callback arguments.
    # The per-file CALLBACK_ARG detection only finds callbacks defined in the
    # same file as the caller. This step uses the global func_names set to
    # find callbacks passed as arguments across file boundaries.
    from _vendor._regex_c_scanner import _detect_cross_file_callbacks, _CALLBACK_ARG_SKIP_CALLEES
    # Build effective skip_callees from profile + built-in
    _skip_callees = _CALLBACK_ARG_SKIP_CALLEES | set((profile or {}).get("skip_callees", []))
    all_edges = _detect_cross_file_callbacks(all_functions, all_edges,
                                              source_root=source_root,
                                              passthrough_reg_funcs=all_passthrough_reg_funcs,
                                              field_assignments=all_field_assignments,
                                              skip_callees=_skip_callees)

    # Proactively release body_text when memory is tight.
    # After cross-file callback detection, body_text is no longer needed
    # by the scanner. The builder only uses it for state-access extraction,
    # so we extract state_access here BEFORE dropping body_text — otherwise
    # the builder's field_access / global_access tables end up empty.
    _body_text_dropped = False
    if memory_guard and memory_guard.is_memory_low():
        dropped = _extract_state_access_then_drop_body_text(
            all_functions, all_globals, all_field_assignments)
        if dropped:
            _body_text_dropped = True
            print(f"[MemoryGuard] Released body_text from {dropped} functions "
                  f"after scan phase (state_access preserved)", file=sys.stderr)

    # Streaming output path: write each array incrementally and free memory
    if streaming_output:
        # For large projects, use split output (per-domain files) to keep
        # memory manageable during the build phase. The build phase can then
        # use _load_split_extraction() which loads files incrementally.
        # _use_split was already determined above for the scan loop.
        # Helper functions (_merge_list_file, _merge_dict_file, _flush_accumulated)
        # are also already defined above.
        if _use_split:
            # Flush any remaining edges from post-scan processing
            # (e.g., _detect_cross_file_callbacks adds CALLBACK_ARG edges after
            # the scan loop but before this return — they would be lost otherwise).
            if all_edges:
                _extra_chunk = _flush_chunk[0]
                _flush_chunk[0] += 1
                _extra_edge_count = len(all_edges)
                Path(os.path.join(_split_dir, "edges", f"edges_{_extra_chunk}.json")).write_text(
                    json.dumps(all_edges, ensure_ascii=False), encoding="utf-8")
                all_edges.clear()
                print(f"[scan] Flushed {_extra_edge_count} post-scan edges to chunk {_extra_chunk}",
                      file=sys.stderr)
            # Write metadata
            _meta = {
                "source_root": source_root,
                "domains": sorted(domains),
                "lang_stats": dict(lang_stats),
                "scan_complete": not _scan_stopped_early,
            }
            Path(os.path.join(_split_dir, "_metadata.json")).write_text(
                json.dumps(_meta, ensure_ascii=False, indent=2), encoding="utf-8")
            gc.collect()
            print(f"[scan] Split output written to {_split_dir}", file=sys.stderr)
            _write_scan_errors_sidecar(streaming_output, all_scan_errors, all_scan_warnings,
                                       len(file_list), dict(lang_stats))
            return {
                "source_root": source_root,
                "domains": sorted(domains),
                "lang_stats": dict(lang_stats),
                "_function_count": 0,  # counted from files
                "_edge_count": 0,
                "_streamed_to": streaming_output,
                "_split_dir": _split_dir,
                "_stopped_early": _scan_stopped_early,
                "_scan_error_count": len(all_scan_errors),
                "_scan_warning_count": len(all_scan_warnings),
            }

        # Original monolithic streaming path for smaller projects
        from _builder.memory_guard import StreamingJsonObjectWriter
        writer = StreamingJsonObjectWriter(streaming_output, chunk_size=500)
        writer.begin()
        writer.write_scalar("source_root", source_root)
        writer.write_scalar("domains", sorted(domains))
        writer.write_scalar("lang_stats", dict(lang_stats))

        # Write functions array then free
        _function_count_val = len(all_functions)
        writer.begin_array("functions")
        for func in all_functions:
            writer.write_array_item(func)
        writer.end_array()
        all_functions.clear()
        all_functions = None

        # Write edges array then free
        _edge_count_val = len(all_edges)
        writer.begin_array("edges")
        for edge in all_edges:
            writer.write_array_item(edge)
        writer.end_array()
        all_edges.clear()
        all_edges = None

        # Write import_edges
        writer.begin_array("import_edges")
        for item in all_import_edges:
            writer.write_array_item(item)
        writer.end_array()
        all_import_edges = None

        # Write globals
        writer.write_scalar("globals", all_globals)

        # Write each collected list-type field
        for name, items in [
            ("vtable_registrations", all_vtable_registrations),
            ("macro_registrations", all_macro_registrations),
            ("token_paste_functions", all_token_paste_functions),
            ("container_of_usages", all_container_of_usages),
            ("conversion_funcs", all_conversion_funcs),
            ("struct_defs", all_struct_defs),
            ("field_assignments", all_field_assignments),
            # cgdb 13-layer records (from clang/dual scanner)
            ("cgdb_nodes", all_cgdb_nodes),
            ("cgdb_types", all_cgdb_types),
            ("cgdb_edges", all_cgdb_edges),
            ("cgdb_invoke_sites", all_cgdb_invoke_sites),
            ("cgdb_predicates", all_cgdb_predicates),
            ("cgdb_ops_bindings", all_cgdb_ops_bindings),
            ("cgdb_basic_blocks", all_cgdb_basic_blocks),
            ("cgdb_cfg_edges", all_cgdb_cfg_edges),
            ("cgdb_data_flow", all_cgdb_data_flow),
            ("cgdb_sync_primitives", all_cgdb_sync_primitives),
            ("cgdb_happens_before", all_cgdb_happens_before),
            ("cgdb_alias_sets", all_cgdb_alias_sets),
            ("cgdb_doc_comments", all_cgdb_doc_comments),
            ("cgdb_metadata", all_cgdb_metadata),
            ("cgdb_includes", all_cgdb_includes),
            ("conditions", all_conditions),
        ]:
            writer.begin_array(name)
            if items:
                for item in items:
                    writer.write_array_item(item)
            writer.end_array()

        # Write dict-type fields
        writer.write_scalar("fn_ptr_calls", all_fn_ptr_calls)
        writer.write_scalar("passthrough_reg_funcs", all_passthrough_reg_funcs)

        writer.end()
        # Force GC after streaming write
        gc.collect()

        _write_scan_errors_sidecar(streaming_output, all_scan_errors, all_scan_warnings,
                                   len(file_list), dict(lang_stats))
        # Return lightweight metadata (data already on disk)
        return {
            "source_root": source_root,
            "domains": sorted(domains),
            "lang_stats": dict(lang_stats),
            "_function_count": _function_count_val,
            "_edge_count": _edge_count_val,
            "_streamed_to": streaming_output,
            "_stopped_early": _scan_stopped_early,
            "_scan_error_count": len(all_scan_errors),
            "_scan_warning_count": len(all_scan_warnings),
        }

    # Non-streaming path: return full dict (backward compatible)
    if streaming_output:
        _write_scan_errors_sidecar(streaming_output, all_scan_errors, all_scan_warnings,
                                   len(file_list), dict(lang_stats))
    return {
        "source_root": source_root,
        "functions": all_functions,
        "edges": all_edges,
        "import_edges": all_import_edges,
        "globals": all_globals,
        "domains": sorted(domains),
        "lang_stats": dict(lang_stats),
        "vtable_registrations": all_vtable_registrations,
        "macro_registrations": all_macro_registrations,
        "token_paste_functions": all_token_paste_functions,
        "container_of_usages": all_container_of_usages,
        "conversion_funcs": all_conversion_funcs,
        "struct_defs": all_struct_defs,
        "fn_ptr_calls": all_fn_ptr_calls,
        "field_assignments": all_field_assignments,
        "passthrough_reg_funcs": all_passthrough_reg_funcs,
        # cgdb (code graph database) 13-layer records — written to cgdb
        # tables by the builder (cgdb_store.SQLiteCGDBStore.write_batch).
        "cgdb_nodes": all_cgdb_nodes,
        "cgdb_types": all_cgdb_types,
        "cgdb_edges": all_cgdb_edges,
        "cgdb_invoke_sites": all_cgdb_invoke_sites,
        "cgdb_predicates": all_cgdb_predicates,
        "cgdb_ops_bindings": all_cgdb_ops_bindings,
        "cgdb_basic_blocks": all_cgdb_basic_blocks,
        "cgdb_cfg_edges": all_cgdb_cfg_edges,
        "cgdb_data_flow": all_cgdb_data_flow,
        "cgdb_sync_primitives": all_cgdb_sync_primitives,
        "cgdb_happens_before": all_cgdb_happens_before,
        "cgdb_alias_sets": all_cgdb_alias_sets,
        "cgdb_doc_comments": all_cgdb_doc_comments,
        "cgdb_metadata": all_cgdb_metadata,
        "cgdb_includes": all_cgdb_includes,
        "conditions": all_conditions,
        "_stopped_early": _scan_stopped_early,
        "_body_text_dropped": _body_text_dropped,
        "scan_errors": all_scan_errors,
        "scan_warnings": all_scan_warnings,
    }


def scan_files(file_list: list, source_root: str, lang: str = "auto",
               macro_bindings: dict = None, api_prefixes: list = None,
               profile: dict = None,
               extraction_backend: str = None,
               compile_commands_path: str = None,
               clang_args: list = None) -> dict:
    """Scan a specific list of files (for incremental update)."""
    all_functions = []
    all_edges = []
    all_import_edges = []
    all_globals = {"enums": [], "constants": [], "typedefs": [], "global_vars": []}
    all_vtable_registrations = []
    all_macro_registrations = []
    all_cgdb_nodes = []
    all_cgdb_types = []
    all_cgdb_edges = []
    all_cgdb_invoke_sites = []
    all_cgdb_predicates = []
    all_cgdb_ops_bindings = []
    all_cgdb_basic_blocks = []
    all_cgdb_cfg_edges = []
    all_cgdb_data_flow = []
    all_cgdb_sync_primitives = []
    all_cgdb_happens_before = []
    all_cgdb_alias_sets = []
    all_cgdb_doc_comments = []
    all_cgdb_metadata = []
    all_cgdb_includes = []
    all_conditions = []
    domains = set()
    lang_stats = defaultdict(int)

    # Scanner cache: reuses the same scanner instance for files of the same language.
    # This is safe for sequential use. For parallel scanning, use scan_directory()
    # which creates thread-local scanner instances via _get_scanner().
    _export_macros = profile.get("export_macros", []) if profile else []
    _callback_patterns = profile.get("callback_patterns", []) if profile else []
    if not _callback_patterns and profile:
        _callback_patterns = profile.get("callback_detection", {}).get("static_patterns", [])
    _macro_dispatch_patterns = profile.get("macro_dispatch_patterns", []) if profile else []
    # Fallback: if macro_dispatch_patterns is empty but macro_dispatch.registration_macros
    # exists (nested format from raw profile), use it.
    if not _macro_dispatch_patterns and profile:
        _macro_dispatch_patterns = profile.get("macro_dispatch", {}).get("registration_macros", [])
    scanner_cache = {}
    for fpath in file_list:
        ext = Path(fpath).suffix.lower()
        file_lang = lang if lang != "auto" else detect_language(fpath)
        if not file_lang:
            continue
        if ext not in LANG_EXTENSIONS.get(file_lang, set()):
            if lang != "auto":
                continue
        if file_lang not in scanner_cache:
            try:
                scanner_cache[file_lang] = get_scanner(file_lang, api_prefixes=api_prefixes, export_macros=_export_macros, callback_patterns=_callback_patterns, macro_dispatch_patterns=_macro_dispatch_patterns, profile=profile, extraction_backend=extraction_backend, compile_commands_path=compile_commands_path, clang_args=clang_args)
            except (ValueError, ImportError):
                if file_lang in ("c", "cpp"):
                    from _vendor._regex_c_scanner import scan_c_file
                    scanner_cache[file_lang] = ("regex_fallback", scan_c_file)
                else:
                    continue
        scanner = scanner_cache[file_lang]
        if isinstance(scanner, tuple) and scanner[0] == "regex_fallback":
            result = scanner[1](fpath, source_root, api_prefixes=api_prefixes,
                                 profile=profile)
        else:
            result = scanner.scan_file(fpath, source_root, macro_bindings=macro_bindings)
        all_functions.extend(result.get("functions", []))
        all_edges.extend(result.get("edges", []))
        all_import_edges.extend(result.get("import_edges", []))
        for key in ("enums", "constants", "typedefs", "global_vars"):
            all_globals[key].extend(result.get("globals", {}).get(key, []))
        # Aggregate vtable and macro registrations
        vregs = result.get("vtable_registrations", [])
        if vregs:
            all_vtable_registrations.extend(vregs)
        mregs = result.get("macro_registrations", [])
        if mregs:
            all_macro_registrations.extend(mregs)
        # Aggregate cgdb records
        if result.get("cgdb_nodes"):
            all_cgdb_nodes.extend(result["cgdb_nodes"])
        if result.get("cgdb_types"):
            all_cgdb_types.extend(result["cgdb_types"])
        if result.get("cgdb_edges"):
            all_cgdb_edges.extend(result["cgdb_edges"])
        if result.get("cgdb_invoke_sites"):
            all_cgdb_invoke_sites.extend(result["cgdb_invoke_sites"])
        if result.get("cgdb_predicates"):
            all_cgdb_predicates.extend(result["cgdb_predicates"])
        if result.get("cgdb_ops_bindings"):
            all_cgdb_ops_bindings.extend(result["cgdb_ops_bindings"])
        if result.get("cgdb_basic_blocks"):
            all_cgdb_basic_blocks.extend(result["cgdb_basic_blocks"])
        if result.get("cgdb_cfg_edges"):
            all_cgdb_cfg_edges.extend(result["cgdb_cfg_edges"])
        if result.get("cgdb_data_flow"):
            all_cgdb_data_flow.extend(result["cgdb_data_flow"])
        if result.get("cgdb_sync_primitives"):
            all_cgdb_sync_primitives.extend(result["cgdb_sync_primitives"])
        if result.get("cgdb_happens_before"):
            all_cgdb_happens_before.extend(result["cgdb_happens_before"])
        if result.get("cgdb_alias_sets"):
            all_cgdb_alias_sets.extend(result["cgdb_alias_sets"])
        if result.get("cgdb_doc_comments"):
            all_cgdb_doc_comments.extend(result["cgdb_doc_comments"])
        if result.get("cgdb_metadata"):
            all_cgdb_metadata.extend(result["cgdb_metadata"])
        if result.get("cgdb_includes"):
            all_cgdb_includes.extend(result["cgdb_includes"])
        if result.get("conditions"):
            all_conditions.extend(result["conditions"])
        if result.get("cgdb_conditions"):
            all_conditions.extend(result["cgdb_conditions"])
        if result.get("domain"):
            domains.add(result["domain"])
        lang_stats[file_lang] += 1

    return {
        "source_root": source_root,
        "functions": all_functions,
        "edges": all_edges,
        "import_edges": all_import_edges,
        "globals": all_globals,
        "vtable_registrations": all_vtable_registrations,
        "macro_registrations": all_macro_registrations,
        "cgdb_nodes": all_cgdb_nodes,
        "cgdb_types": all_cgdb_types,
        "cgdb_edges": all_cgdb_edges,
        "cgdb_invoke_sites": all_cgdb_invoke_sites,
        "cgdb_predicates": all_cgdb_predicates,
        "cgdb_ops_bindings": all_cgdb_ops_bindings,
        "cgdb_basic_blocks": all_cgdb_basic_blocks,
        "cgdb_cfg_edges": all_cgdb_cfg_edges,
        "cgdb_data_flow": all_cgdb_data_flow,
        "cgdb_sync_primitives": all_cgdb_sync_primitives,
        "cgdb_happens_before": all_cgdb_happens_before,
        "cgdb_alias_sets": all_cgdb_alias_sets,
        "cgdb_doc_comments": all_cgdb_doc_comments,
        "cgdb_metadata": all_cgdb_metadata,
        "cgdb_includes": all_cgdb_includes,
        "conditions": all_conditions,
        "domains": sorted(domains),
        "lang_stats": dict(lang_stats),
    }


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _parse_macros_str(text: str) -> dict:
    """Parse macro string like 'NDEBUG FEATURE_X=1 -DFOO' into dict."""
    macros = {}
    if not text:
        return macros
    for token in text.split():
        token = token.strip()
        if token.startswith('-D'):
            token = token[2:]
        if not token:
            continue
        if '=' in token:
            k, v = token.split('=', 1)
            macros[k] = v
        else:
            macros[token] = ""
    return macros


def _parse_clang_args(text: str) -> list:
    """Parse a clang-args string into a list of args, preserving -I/-D/-std values.

    Uses shlex.split to handle quoted args correctly (e.g., -D 'NAME="value"').
    Returns [] for empty input.
    """
    if not text:
        return []
    import shlex
    try:
        return shlex.split(text)
    except ValueError:
        # Unbalanced quotes — fall back to naive split
        return [t for t in text.split() if t]


def _prompt_for_compile_commands(source_root: str) -> str:
    """Interactively prompt the user for a compile_commands.json path.

    Returns the path string (validated to exist), or '' if the user
    declines or provides an invalid path.

    The prompt is shown when:
      - extraction_backend is 'auto' or 'clang'
      - the source tree contains at least one c/cpp/asm file
      - neither --compile-commands nor --clang-args was provided
      - --no-interactive is not set

    The prompt explains what compile_commands.json is, how to generate
    one for common build systems, and lets the user enter a path or
    decline.
    """
    print("\n" + "=" * 78, file=sys.stderr)
    print("Detected C/C++/ASM source files and clang backend is active.",
          file=sys.stderr)
    print("For accurate clang parsing, a compile_commands.json is strongly "
          "recommended.", file=sys.stderr)
    print("It provides per-file -I (include paths), -D (macro definitions), "
          "and -std (language standard).", file=sys.stderr)
    print("-" * 78, file=sys.stderr)
    print("How to generate compile_commands.json:", file=sys.stderr)
    print("  • CMake project:  cd build && cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ..", file=sys.stderr)
    print("                    (creates build/compile_commands.json)", file=sys.stderr)
    print("  • Make project:   bear -- make", file=sys.stderr)
    print("                    (creates compile_commands.json in CWD)", file=sys.stderr)
    print("  • Ninja project:  ninja -t compdb c cc > compile_commands.json", file=sys.stderr)
    print("  • Meson project:  meson setup build && ln -s build/compile_commands.json .", file=sys.stderr)
    print("  • Bazel project:  use bazel-compile-commands-extractor or similar", file=sys.stderr)
    print("-" * 78, file=sys.stderr)
    # Auto-suggest: look for compile_commands.json in source_root or common build dirs
    suggestions = []
    for candidate in (
            os.path.join(source_root, 'compile_commands.json'),
            os.path.join(source_root, 'build', 'compile_commands.json'),
            os.path.join(source_root, 'build-debug', 'compile_commands.json'),
            os.path.join(source_root, 'build-release', 'compile_commands.json'),
            os.path.join(source_root, 'out', 'compile_commands.json'),
    ):
        if os.path.isfile(candidate):
            suggestions.append(candidate)
    if suggestions:
        print(f"Auto-detected existing compile_commands.json:", file=sys.stderr)
        for i, s in enumerate(suggestions, 1):
            print(f"  [{i}] {s}", file=sys.stderr)
        print(f"  [0] Enter a different path manually", file=sys.stderr)
        print(f"  [s] Skip — proceed without compile_commands (NOT recommended)",
              file=sys.stderr)
        try:
            choice = input("Select [1-{} / 0 / s] (default=1): ".format(len(suggestions))).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            return ''
        if choice == 's':
            return ''
        if choice == '' or choice == '1':
            return suggestions[0]
        try:
            idx = int(choice)
            if 1 <= idx <= len(suggestions):
                return suggestions[idx - 1]
            if idx == 0:
                # Manual entry
                try:
                    path = input("Path to compile_commands.json: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("", file=sys.stderr)
                    return ''
                if path and os.path.isfile(path):
                    return path
                if path:
                    print(f"File not found: {path}", file=sys.stderr)
                return ''
        except ValueError:
            pass
        # Fallback: treat as path
        if os.path.isfile(choice):
            return choice
        print(f"Invalid choice: {choice}", file=sys.stderr)
        return ''
    else:
        print("No compile_commands.json found in source tree.", file=sys.stderr)
        print("  [1] Enter path manually", file=sys.stderr)
        print("  [s] Skip — proceed without compile_commands (NOT recommended, "
              "clang may miss includes/macros)", file=sys.stderr)
        try:
            choice = input("Select [1 / s] (default=s): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("", file=sys.stderr)
            return ''
        if choice == '1':
            try:
                path = input("Path to compile_commands.json: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("", file=sys.stderr)
                return ''
            if path and os.path.isfile(path):
                return path
            if path:
                print(f"File not found: {path}", file=sys.stderr)
            return ''
        return ''


def cmd_scan(args):
    from _builder.token_budget import PipelineTracker, estimate_tokens

    source = args.source
    if not os.path.isdir(source):
        print(f"Error: {source} is not a directory", file=sys.stderr)
        sys.exit(1)

    macro_bindings = _parse_macros_str(args.macros) if args.macros else None
    api_prefixes = args.api_prefixes.split(",") if args.api_prefixes else None

    # --auto-profile: generate .code2database_profile.json before scanning
    if getattr(args, 'auto_profile', False):
        from _profile.generate import write_auto_profile
        auto_profile_path = write_auto_profile(source)
        print(f"[Auto-profile] Generated: {auto_profile_path}", file=sys.stderr)
        # Report what was detected
        from _profile.generate import detect_project_type, auto_detect_struct_op_types
        from _profile.generate import auto_detect_api_prefixes, auto_detect_callback_patterns
        project_type = detect_project_type(source)
        struct_ops = auto_detect_struct_op_types(source)
        api_prefs = auto_detect_api_prefixes(source)
        cb_patterns = auto_detect_callback_patterns(source)
        print(f"[Auto-profile] Project type: {project_type}", file=sys.stderr)
        if struct_ops:
            print(f"[Auto-profile] struct_op_types: {struct_ops}", file=sys.stderr)
        if api_prefs:
            print(f"[Auto-profile] api_prefixes: {api_prefs}", file=sys.stderr)
        if cb_patterns:
            cb_names = [p["register_func"] for p in cb_patterns]
            print(f"[Auto-profile] callback_patterns: {cb_names}", file=sys.stderr)

    # Initialize memory guard if available
    memory_guard = None
    try:
        from _builder.memory_guard import MemoryGuard, set_global_guard
        warn_thresh = getattr(args, 'memory_warn_threshold', 0.75)
        crit_thresh = getattr(args, 'memory_crit_threshold', 0.85)
        memory_guard = MemoryGuard(
            warn_threshold=warn_thresh,
            crit_threshold=crit_thresh
        )

        # More aggressive memory management for large projects.
        # Previously this forced workers=1 (single CPU); users on multi-core
        # boxes want speedup. We now allow parallelism but cap the worker
        # count and tighten batch size + GC cadence to bound memory growth.
        if getattr(args, 'large_project', False):
            memory_guard.batch_reduction_factor = 0.3
            memory_guard.gc_interval = 1.0
            if args.workers == 0:
                # Auto-pick a conservative parallel level for large projects.
                # tree-sitter (C) releases the GIL, so 4 threads give real
                # speedup without exploding per-worker memory.
                try:
                    import multiprocessing as _mp
                    _cpu = _mp.cpu_count()
                except (NotImplementedError, OSError):
                    _cpu = 4
                args.workers = max(2, min(_cpu, 4))
                print(f"[MemoryGuard] Large project mode: using {args.workers} "
                      f"scan workers (auto-capped for memory safety)", file=sys.stderr)
            elif args.workers > 4:
                print(f"[MemoryGuard] Large project mode: capping workers "
                      f"{args.workers} → 4 to bound memory", file=sys.stderr)
                args.workers = 4

        # Set stats file if specified
        stats_file = getattr(args, 'memory_stats', None)
        if stats_file:
            memory_guard.stats_file = stats_file

        set_global_guard(memory_guard)
        memory_guard.start_monitoring(interval=10.0)
        print(f"[MemoryGuard] Started monitoring (warn={warn_thresh*100:.0f}%, crit={crit_thresh*100:.0f}%)",
              file=sys.stderr)

        # Register OOM auto-save callback: when memory hits critical,
        # extract state_access from body_text (so field_access / global_access
        # tables can still be populated by the builder), then drop body_text
        # to free memory. Without the pre-drop extraction, the builder's
        # field_access/global_access tables end up empty for any scan that
        # hits critical memory (kernel, SPDK, etc.) — which silently breaks
        # field-access queries ("who writes bh->b_bdev?" returns []).
        def _pre_oom_callback(mem_info):
            """Callback invoked when memory reaches critical level."""
            print(f"[MemoryGuard] Pre-OOM callback triggered at "
                  f"{mem_info.get('usage_percent', 0)*100:.0f}% — "
                  f"attempting emergency memory release", file=sys.stderr)
            dropped = _extract_state_access_then_drop_body_text(
                all_functions, all_globals, all_field_assignments)
            if dropped:
                print(f"[MemoryGuard] Pre-OOM: dropped body_text from {dropped} functions "
                      f"(state_access preserved)", file=sys.stderr)
            gc.collect()
        memory_guard.register_degradation_callback(_pre_oom_callback)
    except ImportError:
        print("Warning: memory_guard module not available, memory management disabled", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Failed to initialize memory guard: {e}", file=sys.stderr)

    # Load profile if specified or auto-discover
    profile = None
    profile_path = getattr(args, 'profile', None)

    tracker = PipelineTracker()

    tracker.begin("load_profile")
    if profile_path:
        from _profile import ProfileSchema
        p = ProfileSchema.load(profile_path)
        profile = p.to_scanner_config()
        # Merge profile's api_prefixes with CLI --api-prefixes
        if profile.get("api_prefixes"):
            extra = profile["api_prefixes"]
            if api_prefixes:
                api_prefixes = list(set(api_prefixes) | set(extra))
            else:
                api_prefixes = extra
    elif not api_prefixes:
        # Auto-discover: check source_root/.code2database_profile.json
        auto_path = os.path.join(source, ".code2database_profile.json")
        if os.path.exists(auto_path):
            from _profile import ProfileSchema
            p = ProfileSchema.load(auto_path)
            profile = p.to_scanner_config()
            if profile.get("api_prefixes"):
                api_prefixes = profile["api_prefixes"]
    tracker.end()

    # Interactive prompt for compile_commands.json when clang backend is active.
    # This gives the user a chance to provide accurate per-file compile flags
    # (-I include paths, -D macro defs, -std version) which are essential for
    # clang to correctly parse real-world projects. Without them, most
    # non-trivial C/C++ files will fail with "file not found" errors and
    # the cgdb tables will be missing most nodes.
    _extraction_backend = getattr(args, 'extraction_backend', 'auto')
    if not _extraction_backend or _extraction_backend == 'auto':
        if profile:
            _extraction_backend = profile.get("extraction_backend", "auto") or "auto"
    _compile_commands_arg = getattr(args, 'compile_commands', '') or ''
    _clang_args_arg = getattr(args, 'clang_args', '') or ''
    _no_interactive = getattr(args, 'no_interactive', False)
    # Determine if we'll actually use the clang backend (c/cpp files + auto/clang)
    _will_use_clang = _extraction_backend in ('auto', 'clang')
    if _will_use_clang and _extraction_backend == 'auto':
        # For 'auto' on non-c/cpp projects, clang is not used; skip prompt.
        # Detect by scanning source for c/cpp files.
        _has_c_cpp = False
        for _dirpath, _dirnames, _filenames in os.walk(source):
            _dirnames[:] = [d for d in _dirnames if not d.startswith('.')
                            and d not in ('__pycache__', 'node_modules',
                                          '.git', 'build', 'dist', 'venv')]
            for _fn in _filenames:
                if os.path.splitext(_fn)[1].lower() in (
                        '.c', '.h', '.cpp', '.hpp', '.cc', '.cxx', '.s', '.S'):
                    _has_c_cpp = True
                    break
            if _has_c_cpp:
                break
        _will_use_clang = _has_c_cpp
    if _will_use_clang and not _compile_commands_arg and not _clang_args_arg:
        if _no_interactive or not sys.stdin.isatty():
            print("[CompileCommands] No --compile-commands or --clang-args provided "
                  "and --no-interactive set (or stdin is not a TTY). clang parsing "
                  "may miss include paths and macro definitions. Set --compile-commands "
                  "or --clang-args for accurate results.", file=sys.stderr)
        else:
            _cc_path = _prompt_for_compile_commands(source)
            if _cc_path:
                # Validate the path exists before accepting
                if os.path.isfile(_cc_path):
                    args.compile_commands = _cc_path
                    print(f"[CompileCommands] Using: {_cc_path}", file=sys.stderr)
                else:
                    print(f"[CompileCommands] Path not found: {_cc_path} — "
                          f"proceeding without compile_commands.", file=sys.stderr)
            else:
                print("[CompileCommands] Proceeding without compile_commands. "
                      "If clang reports 'file not found' errors, generate one via:\n"
                      "  CMake:  cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ..\n"
                      "  Make:   bear -- make\n"
                      "  Ninja:  ninja -t compdb c cc > compile_commands.json\n"
                      "Then re-run with --compile-commands <path>.",
                      file=sys.stderr)

    # Compute source size for comparison
    source_bytes = 0
    source_files = 0
    _SOURCE_EXTS = {'.c', '.h', '.cpp', '.hpp', '.cc', '.cxx', '.go', '.py', '.java', '.rs', '.s', '.S', '.asm'}
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in
                       ('__pycache__', 'node_modules', '.git', 'build', 'dist', 'venv')]
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in _SOURCE_EXTS:
                fpath = os.path.join(dirpath, fname)
                try:
                    source_bytes += os.path.getsize(fpath)
                    source_files += 1
                except OSError:
                    pass

    tracker.begin("scan", metadata={
        "source_files": source_files,
        "source_bytes": source_bytes,
    })

    # Incremental scan: only rescan changed files
    if getattr(args, 'incremental', False):
        from _scanner.changes import detect_changes, save_manifest
        changes = detect_changes(source, os.path.dirname(args.output) if args.output else source)
        if changes["needs_full_scan"]:
            # No manifest exists, fall through to full scan
            print("No manifest found, performing full scan", file=sys.stderr)
        else:
            changed_files = changes["new_files"] + changes["changed_files"]
            deleted_files = changes["deleted_files"]
            if not changed_files and not deleted_files:
                print("No changes detected.", file=sys.stderr)
                return
            if changed_files:
                print(f"Incremental scan: {len(changed_files)} changed/new, "
                      f"{len(deleted_files)} deleted, {changes['unchanged_count']} unchanged",
                      file=sys.stderr)
                result = scan_files(changed_files, source, args.lang,
                                    macro_bindings=macro_bindings,
                                    api_prefixes=api_prefixes, profile=profile)
            else:
                result = {"source_root": source, "functions": [], "edges": [],
                          "import_edges": [], "globals": {"enums": [], "constants": [],
                          "typedefs": [], "global_vars": []}, "domains": [],
                          "lang_stats": {}, "vtable_registrations": [],
                          "macro_registrations": [], "token_paste_functions": [],
                          "container_of_usages": [], "conversion_funcs": [],
                          "struct_defs": [], "fn_ptr_calls": {},
                          "field_assignments": [], "passthrough_reg_funcs": {}}
            # Merge with existing extraction data
            if deleted_files or changed_files:
                existing_path = args.output or os.path.join(
                    os.path.dirname(args.output) if args.output else source,
                    ".code2database_extraction.json")
                if os.path.exists(existing_path):
                    # Check file size before loading to prevent OOM
                    _existing_size = os.path.getsize(existing_path)
                    if _existing_size > 2_000_000_000:  # > 2GB
                        print(f"[scan] Warning: existing extraction file is {_existing_size/1e9:.1f}GB, "
                              f"skipping incremental merge (use full rescan)", file=sys.stderr)
                        existing = {}
                    else:
                        with open(existing_path, "r", encoding="utf-8") as f:
                            existing = json.load(f)
                    # Remove entries from deleted/changed files
                    del_set = set(deleted_files + changed_files)
                    existing["functions"] = [
                        fn for fn in existing.get("functions", [])
                        if fn.get("source_file", "") not in del_set
                    ]
                    existing["edges"] = [
                        e for e in existing.get("edges", [])
                        if e.get("_source_file", "") not in del_set
                    ]
                    existing["import_edges"] = [
                        e for e in existing.get("import_edges", [])
                        if e.get("_source_file", e.get("source_file", "")) not in del_set
                    ]
                    existing["vtable_registrations"] = [
                        v for v in existing.get("vtable_registrations", [])
                        if v.get("source_file", "") not in del_set
                    ]
                    existing["field_assignments"] = [
                        fa for fa in existing.get("field_assignments", [])
                        if fa.get("source_file", "") not in del_set
                    ]
                    # Append new data
                    for key in ("functions", "edges", "import_edges",
                                "vtable_registrations", "macro_registrations",
                                "token_paste_functions", "container_of_usages",
                                "conversion_funcs", "struct_defs", "field_assignments"):
                        existing.setdefault(key, []).extend(result.get(key, []))
                    # Merge fn_ptr_calls (dict)
                    for caller, calls in result.get("fn_ptr_calls", {}).items():
                        existing.setdefault("fn_ptr_calls", {}).setdefault(caller, []).extend(calls)
                    # Merge passthrough_reg_funcs (dict)
                    for fn, info in result.get("passthrough_reg_funcs", {}).items():
                        existing.setdefault("passthrough_reg_funcs", {})[fn] = info
                    # Update domains and lang_stats
                    existing["domains"] = list(set(
                        existing.get("domains", []) + result.get("domains", [])))
                    # Rebuild lang_stats
                    for fn in result.get("functions", []):
                        lang = fn.get("domain", "").split(".")[0] if fn.get("domain") else "unknown"
                        existing.setdefault("lang_stats", {})[lang] = existing.get("lang_stats", {}).get(lang, 0) + 1
                    result = existing
            # Update manifest
            save_manifest(source, os.path.dirname(args.output) if args.output else source)
            tracker.end(output_tokens=estimate_tokens(json.dumps(result, ensure_ascii=False)),
                        extra={"functions": len(result.get('functions', [])),
                               "edges": len(result.get('edges', [])),
                               "domains": len(result.get('domains', [])),
                               "incremental": True})
            output = args.output
            if output:
                os.makedirs(os.path.dirname(output) or '.', exist_ok=True)
                Path(output).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                outdir = os.path.dirname(output) or '.'
                report = tracker.write_report(outdir)
                total = report["summary"]
                comp = report.get("comparison", {})
                print(f"Incremental scan: {len(result['functions'])} functions, "
                      f"{len(result['edges'])} edges "
                      f"(pipeline: {total['total_elapsed_sec']:.1f}s)",
                      file=sys.stderr)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return

    if args.files:
        file_list = args.files.split(",")
        result = scan_files(file_list, source, args.lang, macro_bindings=macro_bindings,
                            api_prefixes=api_prefixes, profile=profile,
                            extraction_backend=getattr(args, 'extraction_backend', 'auto'),
                            compile_commands_path=getattr(args, 'compile_commands', '') or None,
                            clang_args=_parse_clang_args(getattr(args, 'clang_args', '')))
    else:
        # Use streaming output when an output path is given to avoid
        # accumulating all scan results in memory before writing.
        _streaming_path = args.output if args.output else None
        # --large-project auto-enables split_output for better memory management
        _auto_split = getattr(args, 'split_output', False) or getattr(args, 'large_project', False)
        # O10: merge profile.scan_hints.skip_dirs into --exclude-dirs. This lets
        # profiles ship project-specific skip lists (e.g., kernel profiles can
        # skip tools/, samples/, Documentation/) without requiring the user to
        # pass --exclude-dirs on the CLI every time.
        _exclude = []
        if args.exclude_dirs:
            _exclude.extend(args.exclude_dirs.split(","))
        if profile:
            _skip_dirs = (profile.get("scan_hints", {}) or {}).get("skip_dirs", []) or []
            for _sd in _skip_dirs:
                if _sd and _sd not in _exclude:
                    _exclude.append(_sd)
        # RPT-KERNEL-D9: --scan-subsystems filter — CLI takes precedence;
        # fall back to profile.scan_hints.scan_subsystems if specified.
        # Lets the linux_kernel profile default to ['fs', 'mm', 'block',
        # 'kernel', 'lib'] without requiring the user to pass the flag.
        _scan_subsystems_list = None
        _cli_subs = getattr(args, 'scan_subsystems', '') or ''
        if _cli_subs:
            _scan_subsystems_list = [s.strip() for s in _cli_subs.split(',') if s.strip()]
        elif profile:
            _prof_subs = (profile.get("scan_hints", {}) or {}).get("scan_subsystems", []) or []
            if _prof_subs:
                _scan_subsystems_list = list(_prof_subs)
        result = scan_directory(source, args.lang, macro_bindings=macro_bindings,
                                workers=args.workers, api_prefixes=api_prefixes,
                                profile=profile, memory_guard=memory_guard,
                                streaming_output=_streaming_path,
                                memory_limit_gb=getattr(args, 'memory_limit', 0),
                                max_functions=getattr(args, 'max_functions', 0),
                                exclude_dirs=_exclude if _exclude else None,
                                quiet=getattr(args, 'quiet', False),
                                split_output=_auto_split,
                                no_body_text=getattr(args, 'no_body_text', False),
                                extraction_backend=getattr(args, 'extraction_backend', 'auto'),
                                compile_commands_path=getattr(args, 'compile_commands', '') or None,
                                clang_args=_parse_clang_args(getattr(args, 'clang_args', '')),
                                scan_subsystems=_scan_subsystems_list)
    _streamed = result.get("_streamed_to") is not None
    _split_dir = result.get("_split_dir")

    # Token estimation: use file size for streaming/split, json.dumps for in-memory
    if _split_dir and os.path.isdir(_split_dir):
        # Split output: sum sizes of all JSON files in the directory tree
        extraction_tokens = 0
        for root, dirs, files in os.walk(_split_dir):
            for fname in files:
                if fname.endswith(".json"):
                    extraction_tokens += os.path.getsize(os.path.join(root, fname)) // 4
        func_count = result.get("_function_count", 0)
        edge_count = result.get("_edge_count", 0)
    elif _streamed and args.output and os.path.exists(args.output):
        extraction_tokens = os.path.getsize(args.output) // 4
        func_count = result.get("_function_count", 0)
        edge_count = result.get("_edge_count", 0)
    else:
        # For non-streaming path, estimate tokens from result dict size
        # to avoid serializing huge dicts just for counting
        _result_size = sum(
            len(json.dumps(v, ensure_ascii=False)) if isinstance(v, (list, dict)) else len(str(v))
            for v in result.values()
        ) if result else 0
        extraction_tokens = _result_size // 4 if _result_size > 10_000_000 else estimate_tokens(json.dumps(result, ensure_ascii=False))
        func_count = len(result.get('functions', []))
        edge_count = len(result.get('edges', []))
    tracker.end(output_tokens=extraction_tokens,
                extra={"functions": func_count,
                       "edges": edge_count,
                       "domains": len(result.get('domains', []))})

    output = args.output
    if output:
        tracker.begin("write_extraction")
        if _split_dir and os.path.isdir(_split_dir):
            # Split output: files already written by scan_directory
            _split_files = []
            for root, dirs, files in os.walk(_split_dir):
                for fname in files:
                    if fname.endswith(".json"):
                        _split_files.append(os.path.join(root, fname))
            tracker.end_with_files(_split_files)
        elif _streamed:
            # Monolithic streaming path: file was already written by scan_directory
            tracker.end_with_files([output])
        else:
            # Non-streaming path: write from returned dict
            os.makedirs(os.path.dirname(output) or '.', exist_ok=True)
            Path(output).write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            tracker.end_with_files([output])
        print(f"Scanned {source}: {func_count} functions, "
              f"{edge_count} edges, {len(result.get('domains', []))} domains",
              file=sys.stderr)
        if result.get('lang_stats'):
            print(f"Language stats: {result['lang_stats']}", file=sys.stderr)
        # Write pipeline stats
        outdir = os.path.dirname(output) or '.'
        report = tracker.write_report(outdir)
        total = report["summary"]
        comp = report.get("comparison", {})
        print(f"Pipeline stats: {total['total_stages']} stages, "
              f"{total['total_elapsed_sec']:.1f}s, "
              f"{total['total_output_bytes']} bytes output "
              f"(source: {comp.get('source_files', 0)} files, "
              f"{comp.get('raw_source_bytes_estimate', 0)} bytes)",
              file=sys.stderr)
    else:
        # No output file: print to stdout.
        # For large results, use streaming write to a temp file first to avoid
        # building the entire JSON string in memory (json.dumps can OOM).
        _func_count = len(result.get('functions', []))
        if _func_count > 10000:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=True,
                                             encoding='utf-8') as _tf:
                from _builder.memory_guard import StreamingJsonObjectWriter
                _writer = StreamingJsonObjectWriter(_tf.name, chunk_size=500)
                _writer.begin()
                for key, value in result.items():
                    if key.startswith('_'):
                        continue  # Skip internal metadata
                    if isinstance(value, list):
                        _writer.begin_array(key)
                        for item in value:
                            _writer.write_array_item(item)
                        _writer.end_array()
                    else:
                        _writer.write_scalar(key, value)
                _writer.end()
                # Stream the file to stdout
                _tf.flush()
                _tf.seek(0)
                import shutil
                shutil.copyfileobj(_tf, sys.stdout)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    # Cleanup: stop memory guard monitoring
    if memory_guard:
        memory_guard.stop_monitoring()
        stats = memory_guard.get_stats()
        if stats.get('peak_memory_mb', 0) > 0:
            print(f"[MemoryGuard] Stats: peak={stats['peak_memory_mb']:.0f}MB, "
                  f"gc={stats['gc_count']}, criticals={stats['criticals']}, "
                  f"warnings={stats['warnings']}", file=sys.stderr)


def cmd_manifest(args):
    source = args.source
    outdir = args.outdir
    if not os.path.isdir(source):
        print(f"Error: {source} is not a directory", file=sys.stderr)
        sys.exit(1)
    count = save_manifest(source, outdir)
    print(f"Manifest saved: {count} source files fingerprinted → {outdir}/.code2database_manifest.json")


def cmd_profile(args):
    """Generate a project profile by scanning source directories."""
    source = args.source
    if not os.path.isdir(source):
        print(f"Error: {source} is not a directory", file=sys.stderr)
        sys.exit(1)

    from _profile.generate import prescan, test_scan, auto_config

    # Determine which phases to run
    phases_str = getattr(args, 'phases', 'prescan,testscan,autoconfig')
    skip_str = getattr(args, 'skip_phases', '')
    phases = [p.strip() for p in phases_str.split(',') if p.strip()]
    skip = set(p.strip() for p in skip_str.split(',') if p.strip())
    phases = [p for p in phases if p not in skip]

    prescan_result = None
    test_scan_result = None
    profile_dict = None

    if 'prescan' in phases:
        print("Phase 1: Pre-scanning source directories...", file=sys.stderr)
        prescan_result = prescan(source)
        print(f"  Public prefixes: {prescan_result.get('public_prefixes', [])}", file=sys.stderr)
        print(f"  External libs: {list(prescan_result.get('external_lib_prefixes', {}).keys())}", file=sys.stderr)
        print(f"  Macro condition prefixes: {prescan_result.get('macro_condition_prefixes', [])}", file=sys.stderr)
        print(f"  Detected frameworks: {prescan_result.get('detected_frameworks', [])}", file=sys.stderr)

    if 'testscan' in phases:
        print("Phase 2: Scanning test directories...", file=sys.stderr)
        test_scan_result = test_scan(source)
        print(f"  Test frameworks: {test_scan_result.get('test_framework_prefixes', [])}", file=sys.stderr)
        print(f"  Test dirs found: {test_scan_result.get('test_dirs_found', [])}", file=sys.stderr)

    if 'autoconfig' in phases:
        print("Phase 3: Auto-configuring profile...", file=sys.stderr)
        profile_dict = auto_config(source, prescan_result, test_scan_result)

    # Phase 4: LLM header analysis (generates a prompt for external LLM use)
    if 'llm_header' in phases:
        if profile_dict is None:
            print("Warning: llm_header phase requires autoconfig phase first, skipping",
                  file=sys.stderr)
        else:
            print("Phase 4: Generating LLM header analysis prompt...", file=sys.stderr)
            from _profile.llm_phases import generate_header_analysis_prompt
            prompt = generate_header_analysis_prompt(source, profile_dict)
            if prompt:
                print("--- BEGIN LLM PROMPT ---", file=sys.stderr)
                print(prompt)
                print("--- END LLM PROMPT ---", file=sys.stderr)
                print("Feed this prompt to an LLM, then apply the response with:",
                      file=sys.stderr)
                print("  code2database_scanner.py profile --source <path> "
                      "--phases llm_header_apply --llm-response <file>",
                      file=sys.stderr)
            else:
                print("  No header files found for analysis", file=sys.stderr)

    # Phase 4 apply: parse LLM response and merge into profile
    if 'llm_header_apply' in phases:
        llm_response_file = getattr(args, 'llm_response', None)
        if not llm_response_file:
            print("Error: llm_header_apply requires --llm-response <file>", file=sys.stderr)
            sys.exit(1)
        if profile_dict is None:
            # Try to load existing profile
            output = getattr(args, 'output', None)
            if output and os.path.isfile(output):
                from _profile import ProfileSchema
                p = ProfileSchema.load(output)
                profile_dict = p.raw
            else:
                print("Error: llm_header_apply requires autoconfig phase or existing "
                      "profile via --output", file=sys.stderr)
                sys.exit(1)

        print("Phase 4 (apply): Parsing LLM header analysis response...", file=sys.stderr)
        from _profile.llm_phases import parse_header_analysis_response, apply_header_analysis_to_profile
        try:
            with open(llm_response_file, 'r', encoding='utf-8') as f:
                response_text = f.read()
        except (IOError, OSError) as e:
            print(f"Error reading LLM response: {e}", file=sys.stderr)
            sys.exit(1)

        analysis = parse_header_analysis_response(response_text)
        if analysis.get("parse_error"):
            print(f"  Parse error: {analysis['parse_error']}", file=sys.stderr)
        else:
            n_patterns = len(analysis.get("callback_patterns", []))
            n_mechanisms = len(analysis.get("concurrency_mechanisms", []))
            print(f"  Found {n_patterns} callback patterns, {n_mechanisms} concurrency mechanisms",
                  file=sys.stderr)
            profile_dict = apply_header_analysis_to_profile(profile_dict, analysis)

    # Phase 6: LLM result check (generates a prompt for external LLM use)
    if 'llm_result' in phases:
        extraction_path = getattr(args, 'extraction', None)
        if not extraction_path:
            print("Error: llm_result phase requires --extraction <path>", file=sys.stderr)
            sys.exit(1)
        print("Phase 6: Generating LLM result check prompt...", file=sys.stderr)
        from _profile.llm_phases import generate_result_check_prompt
        prompt = generate_result_check_prompt(extraction_path, profile_dict)
        if prompt:
            print("--- BEGIN LLM PROMPT ---", file=sys.stderr)
            print(prompt)
            print("--- END LLM PROMPT ---", file=sys.stderr)
        else:
            print("  Could not generate result check prompt", file=sys.stderr)

    # Output final profile
    if profile_dict is not None:
        output = getattr(args, 'output', None)
        if output:
            from _profile import ProfileSchema
            p = ProfileSchema.from_dict(profile_dict)
            p.save(output)
            print(f"Profile saved to: {output}", file=sys.stderr)
        else:
            print(json.dumps(profile_dict, ensure_ascii=False, indent=2))
    else:
        # Output raw findings when autoconfig was not run
        findings = {}
        if prescan_result:
            findings["prescan"] = prescan_result
        if test_scan_result:
            findings["test_scan"] = test_scan_result
        print(json.dumps(findings, ensure_ascii=False, indent=2))


def cmd_discover(args):
    """Discover macro-based registration dispatch patterns from headers."""
    source = args.source
    if not os.path.isdir(source):
        print(f"Error: {source} is not a directory", file=sys.stderr)
        sys.exit(1)

    from _profile.generate import discover_macro_dispatch

    print(f"Discovering macro dispatch patterns in: {source}", file=sys.stderr)
    result = discover_macro_dispatch(source)

    n_reg = len(result.get("registration_macros", []))
    n_tp = len(result.get("token_paste_macros", []))
    print(f"Found {n_reg} registration macros, {n_tp} token-paste macros", file=sys.stderr)

    # If --profile specified, merge into existing profile
    profile_path = getattr(args, 'profile', None)
    if profile_path:
        from _profile import ProfileSchema
        if os.path.isfile(profile_path):
            p = ProfileSchema.load(profile_path)
            profile_dict = dict(p.raw)
        else:
            print(f"Error: Profile not found: {profile_path}", file=sys.stderr)
            sys.exit(1)

        # Merge discovered patterns into profile (strip heuristic-only keys)
        existing_macros = {m["macro_name"] for m in
                          profile_dict.get("macro_dispatch", {}).get("registration_macros", [])}
        for entry in result.get("registration_macros", []):
            if entry["macro_name"] not in existing_macros:
                if "macro_dispatch" not in profile_dict:
                    profile_dict["macro_dispatch"] = {"registration_macros": [], "token_paste_macros": []}
                if "registration_macros" not in profile_dict["macro_dispatch"]:
                    profile_dict["macro_dispatch"]["registration_macros"] = []
                # Strip heuristic-only keys that aren't in the profile schema
                clean = {k: v for k, v in entry.items()
                         if k not in ("_confidence", "_needs_review")}
                profile_dict["macro_dispatch"]["registration_macros"].append(clean)
                existing_macros.add(entry["macro_name"])

        existing_tp = {m["macro_name"] for m in
                      profile_dict.get("macro_dispatch", {}).get("token_paste_macros", [])}
        for entry in result.get("token_paste_macros", []):
            if entry["macro_name"] not in existing_tp:
                if "macro_dispatch" not in profile_dict:
                    profile_dict["macro_dispatch"] = {"registration_macros": [], "token_paste_macros": []}
                profile_dict["macro_dispatch"]["token_paste_macros"].append(entry)
                existing_tp.add(entry["macro_name"])

        # Validate and output
        p = ProfileSchema.from_dict(profile_dict)
        output = getattr(args, 'output', None)
        if output:
            p.save(output)
            print(f"Updated profile saved to: {output}", file=sys.stderr)
        else:
            print(p.to_json())
    else:
        # Just output discovered patterns
        output = getattr(args, 'output', None)
        output_json = json.dumps(result, ensure_ascii=False, indent=2)
        if output:
            os.makedirs(os.path.dirname(output) or '.', exist_ok=True)
            Path(output).write_text(output_json, encoding="utf-8")
            print(f"Discover results saved to: {output}", file=sys.stderr)
        else:
            print(output_json)


def cmd_validate(args):
    """Validate build output files for correctness."""
    from _builder.validate import cmd_validate as _cmd_validate
    _cmd_validate(args)


def _detect_project_type(source_root: str) -> str:
    """Detect project type from build system markers and directory structure."""
    # Linux kernel: Kbuild/Makefile with obj-$(CONFIG), or Kconfig
    kbuild = os.path.exists(os.path.join(source_root, "Kbuild"))
    kconfig = os.path.exists(os.path.join(source_root, "Kconfig"))
    makefile = os.path.join(source_root, "Makefile")
    has_config_make = False
    if os.path.exists(makefile):
        try:
            with open(makefile, "r", errors="replace") as f:
                if "obj-$(CONFIG" in f.read():
                    has_config_make = True
        except OSError:
            pass
    # Check for kernel directory structure
    has_kernel_dirs = any(os.path.isdir(os.path.join(source_root, d))
                          for d in ("drivers", "fs", "kernel", "net", "mm"))
    if (kbuild or kconfig or has_config_make) and has_kernel_dirs:
        return "linux_kernel"
    # SPDK: lib/spdk/ + include/spdk/
    if os.path.isdir(os.path.join(source_root, "lib", "spdk")) and \
       os.path.isdir(os.path.join(source_root, "include", "spdk")):
        return "spdk"
    return "default"


def cmd_auto_profile(args):
    """Auto-detect project type and generate/recommend profile."""
    source = args.source
    outdir = args.outdir
    if not os.path.isdir(source):
        print(f"Error: {source} is not a directory", file=sys.stderr)
        sys.exit(1)

    from _profile.generate import (
        write_auto_profile,
    )

    os.makedirs(outdir, exist_ok=True)

    # Use write_auto_profile which runs full auto_config and writes .code2database_profile.json
    # (Single-pass SourceInfoCollector is created inside auto_config and shared across
    # all auto-discovery functions — no redundant traversals)
    profile_path = write_auto_profile(source, outdir)
    print(f"[Auto-profile] Generated: {profile_path}", file=sys.stderr)

    # Phase 4: LLM header analysis (optional, enabled by --llm-profile)
    if getattr(args, 'llm_profile', False):
        try:
            from _profile.llm_phases import (
                collect_key_headers, generate_header_analysis_prompt,
                parse_header_analysis_response, apply_header_analysis_to_profile,
            )
            print("[Auto-profile] Running LLM header analysis (Phase 4)...", file=sys.stderr)

            # Load the generated profile
            with open(profile_path, 'r', encoding='utf-8') as f:
                profile_data = json.load(f)

            headers = collect_key_headers(source, profile_data)
            if headers:
                prompt = generate_header_analysis_prompt(source, profile_data)
                print(f"[Auto-profile] Generated LLM prompt from {len(headers)} headers", file=sys.stderr)
                print(f"[Auto-profile] To use: paste the prompt into an LLM and save the response, "
                      f"then run: code2database_scanner profile --apply-llm-analysis {profile_path} <response_file>",
                      file=sys.stderr)
            else:
                print("[Auto-profile] No key headers found for LLM analysis", file=sys.stderr)
        except ImportError:
            print("[Auto-profile] LLM phases module not available", file=sys.stderr)
        except Exception as e:
            print(f"[Auto-profile] LLM analysis failed: {e}", file=sys.stderr)

    # Also write a standard profile.json for backward compatibility
    from _profile.generate import detect_project_type
    project_type = detect_project_type(source)

    dest = os.path.join(outdir, "profile.json")
    if project_type != "generic_c_cpp":
        # Check if a built-in profile exists for this project type
        from pathlib import Path as _P
        profile_dir = _P(__file__).resolve().parent / "config" / "profiles"
        built_in_path = profile_dir / f"{project_type}.json"
        if built_in_path.exists():
            import shutil
            shutil.copy2(built_in_path, dest)
            print(f"Built-in profile copied to: {dest}", file=sys.stderr)
            print(f"Project type '{project_type}' has a built-in profile with "
                  f"concurrency patterns, skip names, and API detection rules.",
                  file=sys.stderr)
        else:
            # No built-in profile: copy the auto-generated one
            import shutil
            shutil.copy2(profile_path, dest)
            print(f"Generated profile also saved to: {dest}", file=sys.stderr)
    else:
        # Copy the auto-generated profile
        import shutil
        shutil.copy2(profile_path, dest)
        print(f"Generated profile from source analysis: {dest}", file=sys.stderr)


def cmd_detect_changes(args):
    source = args.source
    outdir = args.outdir
    changes = detect_changes(source, outdir)
    print(json.dumps({
        "new_files": len(changes["new_files"]),
        "changed_files": len(changes["changed_files"]),
        "deleted_files": len(changes["deleted_files"]),
        "unchanged_count": changes["unchanged_count"],
        "needs_full_scan": changes["needs_full_scan"],
        "new_paths": changes["new_files"],
        "changed_paths": changes["changed_files"],
        "deleted_relpaths": [os.path.relpath(p, source) for p in changes["deleted_files"]],
    }, ensure_ascii=False, indent=2))


def main():
    check_python_version()

    parser = argparse.ArgumentParser(description="Multi-language code graph scanner")
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Scan source files for invocation graph extraction")
    p_scan.add_argument("--source", required=True, help="Source directory to scan")
    p_scan.add_argument("--lang", default="auto",
                         choices=["auto", "c", "cpp", "go", "python", "java", "rust", "asm"],
                         help="Language (default: auto-detect from extension)")
    p_scan.add_argument("--output", help="Output JSON file path")
    p_scan.add_argument("--files", help="Comma-separated list of specific files to scan (incremental)")
    p_scan.add_argument("--macros", help="Macro bindings for #ifdef resolution (e.g., 'NDEBUG FEATURE_X=1 -DFOO')")
    p_scan.add_argument("-j", "--workers", type=int, default=0,
                         help="Parallel scan workers (0=auto, 1=sequential, "
                              "N=N threads). Tree-sitter (C) releases the GIL, "
                              "so multi-worker scans get real speedup. "
                              "With --large-project, auto-picks 2-4 workers "
                              "and caps explicit values at 4 to bound memory.")
    p_scan.add_argument("--api-prefixes", default="",
                         help="Comma-separated public API prefixes for entry detection (e.g., 'mylib_,api_')")
    p_scan.add_argument("--profile", default="",
                         help="Project profile JSON file (e.g., scripts/config/profiles/spdk.json). "
                              "Auto-discovers .code2database_profile.json in source root if not specified.")
    p_scan.add_argument("--incremental", action="store_true",
                         help="Incremental scan: only scan changed files since last scan")
    p_scan.add_argument("--memory-warn-threshold", type=float, default=0.75,
                         help="Memory usage threshold (0.0-1.0) to trigger warnings (default: 0.75)")
    p_scan.add_argument("--memory-crit-threshold", type=float, default=0.85,
                         help="Memory usage threshold (0.0-1.0) to trigger critical actions (default: 0.85)")
    p_scan.add_argument("--memory-stats", default="",
                         help="Output file for memory statistics (default: no stats file)")
    p_scan.add_argument("--large-project", action="store_true",
                         help="Optimize for very large projects (Linux kernel scale). "
                              "More aggressive memory management and sequential processing.")
    p_scan.add_argument("--split-output", action="store_true",
                         help="Write extraction output as per-domain JSON files under "
                              "the output directory instead of a single monolithic file. "
                              "Better for large projects -- builder can load incrementally.")
    p_scan.add_argument("--memory-limit", type=float, default=0,
                         help="Memory limit in GB. Stop scanning gracefully when "
                              "approaching this limit (0=auto from system).")
    p_scan.add_argument("--no-body-text", action="store_true",
                         help="Skip body_text extraction to save memory. "
                              "Body text can be re-read from source files at query time.")
    p_scan.add_argument("--max-functions", type=int, default=0,
                         help="Maximum number of functions to extract (0=no limit). "
                              "Use for very large projects where full extraction is not needed.")
    p_scan.add_argument("--exclude-dirs", default="",
                         help="Comma-separated additional directory names to skip during scanning "
                              "(e.g., 'huawei,internal_tools')")
    p_scan.add_argument("--scan-subsystems", default="",
                         help="RPT-KERNEL-D9: comma-separated list of top-level subsystem "
                              "directories to include in the scan (e.g., 'fs,mm,block,kernel,lib' "
                              "for Linux kernel cross-subsystem analysis). Files whose path "
                              "(relative to --source) does not start with one of these "
                              "subsystems are filtered out. Useful when scanning a large "
                              "monorepo but only specific subsystems are needed.")
    p_scan.add_argument("--auto-profile", action="store_true",
                         help="Auto-detect project configuration and generate "
                              ".code2database_profile.json before scanning. Runs "
                              "prescan, test_scan, and auto_config phases to "
                              "detect project type, struct_op_types, api_prefixes, "
                              "and callback_patterns.")
    p_scan.add_argument("--quiet", action="store_true",
                         help="Suppress progress output to stderr")
    p_scan.add_argument("--extraction-backend", default="auto",
                         choices=["auto", "clang", "tree-sitter"],
                         help="C/C++ extraction backend: 'auto' (dual: tree-sitter+clang), "
                              "'clang' (cgdb-only, requires libclang), or 'tree-sitter' (legacy). "
                              "Default: auto. Ignored for non-c/cpp languages.")
    p_scan.add_argument("--compile-commands", default="",
                         help="Path to compile_commands.json (Clang Compilation Database). "
                              "Provides per-file compile flags (-I, -D, -std) for accurate "
                              "clang parsing. Generate via: "
                              "CMake: cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ..; "
                              "Make/Bear: bear -- make; "
                              "Ninja: ninja -t compdb c cc > compile_commands.json")
    p_scan.add_argument("--clang-args", default="",
                         help="Direct clang compile args for files not in compile_commands.json. "
                              "Example: -I/path/to/include -DCONFIG_X=1 -std=c11. "
                              "Applied to ALL c/cpp files in this scan.")
    p_scan.add_argument("--no-interactive", action="store_true",
                         help="Skip interactive prompts (for CI/automated runs). "
                              "When set, scan proceeds with whatever --compile-commands / "
                              "--clang-args were provided (or none), without prompting.")

    p_manifest = sub.add_parser("manifest", help="Save file fingerprint manifest")
    p_manifest.add_argument("--source", required=True, help="Source directory")
    p_manifest.add_argument("--outdir", required=True, help="Output directory for manifest")

    p_detect = sub.add_parser("detect-changes", help="Detect changed files since last manifest")
    p_detect.add_argument("--source", required=True, help="Source directory")
    p_detect.add_argument("--outdir", required=True, help="Callgraph output directory with manifest")

    p_profile = sub.add_parser("profile", help="Generate project profile by scanning source directories")
    p_profile.add_argument("--source", required=True, help="Source root directory of the project")
    p_profile.add_argument("--output", help="Output profile JSON file path (default: print to stdout)")
    p_profile.add_argument("--phases", default="prescan,testscan,autoconfig",
                           help="Comma-separated phases to run "
                                "(prescan,testscan,autoconfig,llm_header,llm_header_apply,llm_result)")
    p_profile.add_argument("--skip-phases", default="",
                           help="Comma-separated phases to skip (e.g., llm_header,llm_result)")
    p_profile.add_argument("--llm-response",
                           help="File containing LLM response for llm_header_apply phase")
    p_profile.add_argument("--extraction",
                           help="Path to extraction.json for llm_result phase")

    p_discover = sub.add_parser("discover",
                                help="Discover macro-based registration dispatch patterns from headers")
    p_discover.add_argument("--source", required=True,
                            help="Source root directory of the project")
    p_discover.add_argument("--output",
                            help="Output JSON file path (default: print to stdout)")
    p_discover.add_argument("--profile",
                            help="Existing profile JSON to extend with discovered patterns")

    p_validate = sub.add_parser("validate",
                                help="Validate build output files for correctness")
    p_validate.add_argument("--outdir", required=True,
                            help="Build output directory to validate")
    p_validate.add_argument("--profile",
                            help="Project profile JSON file for semantic checks")

    p_auto_profile = sub.add_parser("auto-profile",
                                     help="Auto-detect project type and generate/recommend profile")
    p_auto_profile.add_argument("--source", required=True,
                                help="Source root directory of the project")
    p_auto_profile.add_argument("--outdir", required=True,
                                help="Output directory for the generated profile")
    p_auto_profile.add_argument("--llm-profile", action="store_true",
                                help="Run LLM header analysis (Phase 4) after auto-detection to discover "
                                     "additional callback patterns and registration macros")

    p_validate_profile = sub.add_parser("validate-profile",
                                         help="Validate a profile JSON against coverage metrics")
    p_validate_profile.add_argument("--profile", required=True,
                                    help="Path to the profile JSON file")
    p_validate_profile.add_argument("--source", required=True,
                                    help="Source root directory of the project")
    p_validate_profile.add_argument("--extraction", default=None,
                                    help="Optional extraction.json for cross-referencing")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(2)

    commands = {
        "scan": cmd_scan,
        "manifest": cmd_manifest,
        "detect-changes": cmd_detect_changes,
        "profile": cmd_profile,
        "discover": cmd_discover,
        "validate": cmd_validate,
        "auto-profile": cmd_auto_profile,
        "validate-profile": cmd_validate_profile,
    }
    handler = commands.get(args.command)
    if handler is None:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(2)
    # O15: unified exit codes — 0=success (implicit), 1=error, 2=unknown
    # command, 130=KeyboardInterrupt. Avoids inconsistent exit behavior
    # across subcommands.
    try:
        handler(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise  # propagate explicit sys.exit() calls from handlers
    except Exception as exc:
        import traceback
        print(f"Error: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


def cmd_validate_profile(args):
    """Validate a profile by checking coverage metrics."""
    profile_path = args.profile
    source_root = args.source

    from _profile import ProfileSchema
    try:
        profile = ProfileSchema.load(profile_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error loading profile: {e}", file=sys.stderr)
        sys.exit(1)

    raw = profile.raw
    issues = []
    warnings = []
    metrics = {}

    # 1. Check struct_op_types coverage
    struct_op_types = raw.get("api_detection", {}).get("struct_op_types", [])
    metrics["struct_op_types_count"] = len(struct_op_types)

    # Check if vtable registrations were found (from extraction)
    extraction_path = args.extraction
    if extraction_path and os.path.isfile(extraction_path):
        try:
            with open(extraction_path, 'r', encoding='utf-8') as f:
                extraction = json.load(f)
            vtable_regs = extraction.get("vtable_registrations", [])
            metrics["vtable_registrations_count"] = len(vtable_regs)

            # Check: each struct type with vtable registrations should be in struct_op_types
            vtable_struct_types = set()
            for vt in vtable_regs:
                st = vt.get("struct_type", "")
                if st:
                    vtable_struct_types.add(st)
            if vtable_struct_types:
                covered = vtable_struct_types & set(struct_op_types)
                uncovered = vtable_struct_types - set(struct_op_types)
                metrics["struct_op_types_vtable_coverage"] = f"{len(covered)}/{len(vtable_struct_types)}"
                if uncovered:
                    warnings.append(f"Struct types with vtable registrations but NOT in struct_op_types: {sorted(uncovered)}")
        except (json.JSONDecodeError, IOError) as e:
            warnings.append(f"Could not read extraction file: {e}")

    # 2. Check callback_patterns coverage
    cb_patterns = raw.get("callback_detection", {}).get("static_patterns", [])
    metrics["callback_patterns_count"] = len(cb_patterns)
    cb_func_names = [p["register_func"] for p in cb_patterns]
    metrics["callback_register_funcs"] = cb_func_names

    # Check for common callback patterns that might be missing
    from _profile.generate import auto_detect_callback_patterns
    detected_cb = auto_detect_callback_patterns(source_root)
    detected_func_names = {p["register_func"] for p in detected_cb}
    profile_func_names = set(cb_func_names)
    missing_cb = detected_func_names - profile_func_names
    if missing_cb:
        warnings.append(f"Callback patterns detected in source but NOT in profile: {sorted(missing_cb)}")
    metrics["callback_detection_coverage"] = f"{len(profile_func_names & detected_func_names)}/{len(detected_func_names)}"

    # 3. Check registration_macros coverage
    reg_macros = raw.get("macro_dispatch", {}).get("registration_macros", [])
    metrics["registration_macros_count"] = len(reg_macros)

    from _profile.generate import discover_macro_dispatch
    detected_macros = discover_macro_dispatch(source_root)
    detected_macro_names = {m["macro_name"] for m in detected_macros.get("registration_macros", [])}
    profile_macro_names = {m["macro_name"] for m in reg_macros}
    missing_macros = detected_macro_names - profile_macro_names
    if missing_macros:
        warnings.append(f"Registration macros detected in headers but NOT in profile: {sorted(missing_macros)}")
    metrics["registration_macro_coverage"] = f"{len(profile_macro_names & detected_macro_names)}/{len(detected_macro_names)}"

    # 4. Check endpoint_rules
    endpoint_rules = raw.get("endpoint_classification", {}).get("endpoint_rules", [])
    metrics["endpoint_rules_count"] = len(endpoint_rules)

    # 5. Check domain_rules
    domain_rules = raw.get("scan_hints", {}).get("domain_rules", [])
    metrics["domain_rules_count"] = len(domain_rules)

    # 6. Check external_lib_prefixes
    ext_prefixes = raw.get("skip_names", {}).get("external_lib_prefixes", {})
    metrics["external_lib_prefixes_count"] = len(ext_prefixes)

    # 7. Check for project_type match
    project_type = raw.get("project", {}).get("project_type", "")
    detected_type = detect_project_type(source_root)
    if project_type != detected_type:
        issues.append(f"Profile project_type '{project_type}' doesn't match detected type '{detected_type}'")
    metrics["project_type"] = project_type

    # Print results
    print("=== Profile Validation Report ===")
    print(f"\nProject: {raw.get('project', {}).get('name', 'unknown')}")
    print(f"Type: {project_type}")
    print(f"\n--- Metrics ---")
    for key, value in sorted(metrics.items()):
        print(f"  {key}: {value}")

    if warnings:
        print(f"\n--- Warnings ({len(warnings)}) ---")
        for w in warnings:
            print(f"  ⚠ {w}")

    if issues:
        print(f"\n--- Issues ({len(issues)}) ---")
        for i in issues:
            print(f"  ✗ {i}")
    else:
        print(f"\n--- No critical issues found ---")

    # Overall assessment
    if not issues and not warnings:
        print("\n✓ Profile looks good!")
    elif not issues:
        print(f"\n⚠ Profile is valid but has {len(warnings)} warning(s) — consider addressing them for better coverage")
    else:
        print(f"\n✗ Profile has {len(issues)} issue(s) and {len(warnings)} warning(s)")


if __name__ == "__main__":
    main()
