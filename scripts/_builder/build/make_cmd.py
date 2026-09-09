"""One-click project ingestion: env-check + full build pipeline.

Two phases, strictly ordered:
- Phase 1 (env-check): validate the full environment BEFORE any build step
  starts — source readability, language census, extraction backend
  resolvability (libclang / tree-sitter grammars), compile_commands.json
  discovery, graph-dir writability, free disk. Hard errors abort here, so
  the user never discovers a missing dependency halfway through a build.
- Phase 2 (do_make): scan -> build -> derived artifacts -> exports:
  value-flow/data-dep edge builds, #ifdef signal map, FFI SQLite persist,
  brief bootstrap, unified KB index, embeddings, Obsidian vault + HTML
  export, profile-health report. Core steps (scan, build) are fatal;
  enrichment/export steps degrade to warnings so a usable graph survives.

``make --check`` runs phase 1 only (a dry-run of the prerequisites).
"""

import json
import os
import subprocess
import sys

from _scanner.utils import LANG_EXTENSIONS

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCANNER = os.path.join(_SCRIPTS_DIR, "code2database_scanner.py")
_BUILDER = os.path.join(_SCRIPTS_DIR, "code2database_builder.py")

# Directories skipped during the language census (mirrors scanner skips).
_SKIP_DIRS = {".git", ".svn", "__pycache__", "node_modules", "build",
              "dist", "venv", "code2db-out", ".cache"}

# tree-sitter grammar packages required per language family.
# c/cpp share one scanner that imports both grammars at module load.
_GRAMMAR_MODULES = {
    "c": ("tree_sitter_c", "tree_sitter_cpp"),
    "cpp": ("tree_sitter_c", "tree_sitter_cpp"),
    "go": ("tree_sitter_go",),
    "python": ("tree_sitter_python",),
    "java": ("tree_sitter_java",),
    "rust": ("tree_sitter_rust",),
    "asm": (),  # regex scanner, no tree-sitter dependency
}

_MIN_PYTHON = (3, 10)


def _module_available(name: str) -> bool:
    """True if the module is importable (spec lookup, no execution)."""
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def detect_languages(source: str) -> dict:
    """Count source files per language family under source/."""
    counts = {}
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs
                   if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            for lang, exts in LANG_EXTENSIONS.items():
                if ext in exts:
                    counts[lang] = counts.get(lang, 0) + 1
                    break
    return counts


def check_libclang() -> dict:
    """Report libclang availability: python bindings + shared library."""
    info = {"bindings": False, "lib_path": ""}
    if not _module_available("clang.cindex"):
        return info
    info["bindings"] = True
    try:
        import clang.cindex as ci
        configured = getattr(ci.Config, "library_file", None)
        if configured and os.path.exists(configured):
            info["lib_path"] = configured
            return info
    except Exception:
        pass
    from _scanner.clang_scanner import _LIBCLANG_PATHS
    for p in _LIBCLANG_PATHS:
        if os.path.exists(p):
            info["lib_path"] = p
            break
    return info


def check_grammars(lang_counts: dict) -> dict:
    """Map language -> list of missing grammar modules for detected languages."""
    missing = {}
    for lang, count in lang_counts.items():
        if not count:
            continue
        mods = _GRAMMAR_MODULES.get(lang, ())
        miss = [m for m in mods if not _module_available(m)]
        if miss:
            missing[lang] = miss
    return missing


def find_compile_commands(source: str) -> str:
    """Auto-discover compile_commands.json near the source root."""
    for cand in (os.path.join(source, "compile_commands.json"),
                 os.path.join(source, "build", "compile_commands.json")):
        if os.path.isfile(cand):
            return cand
    return ""


def _pip_hint(packages):
    return "pip install " + " ".join(packages)


def decide_backend(requested, lang_counts, libclang, grammars_missing):
    """Resolve the effective extraction backend.

    Returns (backend, notes, errors). Errors mean the run cannot proceed:
    no usable backend for a detected language family.
    """
    notes, errors = [], []
    has_cc = bool(lang_counts.get("c") or lang_counts.get("cpp"))
    clang_ok = libclang["bindings"] and bool(libclang["lib_path"])
    ts_langs = [l for l in ("go", "python", "java", "rust") if lang_counts.get(l)]

    if requested == "clang":
        if not clang_ok:
            detail = ("python bindings missing" if not libclang["bindings"]
                      else "libclang shared library not found")
            errors.append(
                "extraction-backend=clang forced but libclang is unavailable (%s); "
                "install with: apt install libclang-18-dev && pip install libclang"
                % detail)
        if not has_cc:
            notes.append("clang backend only affects C/C++ files; none detected")
        for lang in ts_langs:
            if lang in grammars_missing:
                errors.append("tree-sitter grammars missing for %s: %s (%s) — "
                              "needed alongside the clang backend for %s files"
                              % (lang, ", ".join(grammars_missing[lang]),
                                 _pip_hint(grammars_missing[lang]), lang))
        return "clang", notes, errors

    if requested == "tree-sitter":
        for lang, miss in grammars_missing.items():
            errors.append("tree-sitter grammars missing for %s: %s (%s)"
                          % (lang, ", ".join(miss), _pip_hint(miss)))
        return "tree-sitter", notes, errors

    # auto: clang for C/C++ when available, tree-sitter otherwise
    if has_cc and clang_ok:
        return "clang", notes, errors
    if has_cc and not clang_ok:
        notes.append(
            "libclang unavailable -> C/C++ uses tree-sitter (cgdb semantic layer "
            "disabled: the 19 cgdb_* MCP tools and clang-accurate types/macros). "
            "To enable: apt install libclang-18-dev && pip install libclang")
        for lang in ("c", "cpp"):
            if lang in grammars_missing:
                errors.append(
                    "no usable C/C++ backend: libclang unavailable AND tree-sitter "
                    "grammars missing (%s; %s)"
                    % (", ".join(grammars_missing[lang]),
                       _pip_hint(grammars_missing[lang])))
    for lang in ts_langs:
        if lang in grammars_missing:
            errors.append("tree-sitter grammars missing for %s: %s (%s)"
                          % (lang, ", ".join(grammars_missing[lang]),
                             _pip_hint(grammars_missing[lang])))
    return "tree-sitter", notes, errors


def run_env_check(source, graph, backend_requested="auto",
                  compile_commands="", workers=0, lang_requested="auto") -> dict:
    """Phase 1: validate everything the full pipeline will need, up front.

    Pure checks only — no subprocess is started, nothing is written.
    Returns a report dict; rep['ok'] is False iff hard errors were found.
    """
    rep = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "notes": [],
        "source": os.path.abspath(source),
        "graph": os.path.abspath(graph),
        "python": sys.version.split()[0],
        "lang_counts": {},
        "langs": [],
        "lang_requested": lang_requested,
        "backend_requested": backend_requested,
        "backend": "",
        "libclang": {"bindings": False, "lib_path": ""},
        "compile_commands": "",
        "existing_graph": False,
        "existing_brief": False,
        "free_gb": 0.0,
        "extraction_path": "",
    }

    # Python version
    if sys.version_info < _MIN_PYTHON:
        rep["warnings"].append(
            "python %s detected; %s+ is recommended"
            % (rep["python"], ".".join(str(v) for v in _MIN_PYTHON)))

    # Source directory
    if not os.path.isdir(rep["source"]):
        rep["errors"].append("source directory not found: %s" % rep["source"])
        return rep
    rep["lang_counts"] = detect_languages(rep["source"])
    rep["langs"] = sorted(rep["lang_counts"])
    if not any(rep["lang_counts"].values()):
        rep["errors"].append(
            "no source files detected under %s "
            "(supported: C/C++/Go/Python/Java/Rust/ASM)" % rep["source"])
        return rep

    # libclang + grammars + backend resolution
    rep["libclang"] = check_libclang()
    grammars_missing = check_grammars(rep["lang_counts"])
    backend, notes, errors = decide_backend(
        backend_requested, rep["lang_counts"], rep["libclang"],
        grammars_missing)
    rep["backend"] = backend
    rep["notes"].extend(notes)
    rep["errors"].extend(errors)

    # compile_commands.json (only meaningful when C/C++ is present)
    has_cc = bool(rep["lang_counts"].get("c") or rep["lang_counts"].get("cpp"))
    if has_cc:
        cc = compile_commands or find_compile_commands(rep["source"])
        rep["compile_commands"] = cc
        if not cc and not compile_commands:
            rep["warnings"].append(
                "compile_commands.json not found — clang include/macro resolution "
                "falls back to heuristics. Generate with 'bear -- make' (or "
                "cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON) for accurate "
                "clang-based extraction")

    # Existing graph state (rebuild semantics: memory/knowledge preserved)
    rep["existing_graph"] = os.path.isfile(
        os.path.join(rep["graph"], "code2database.db"))
    rep["existing_brief"] = os.path.isfile(
        os.path.join(rep["graph"], "knowledge", "brief.json"))
    if rep["existing_graph"]:
        rep["notes"].append(
            "existing graph found — graph artifacts are rebuilt; "
            "memory/ and knowledge/ are preserved")

    # Graph dir creatable + writable (this is where extraction.json lands)
    rep["extraction_path"] = os.path.join(rep["graph"], "extraction.json")
    try:
        os.makedirs(rep["graph"], exist_ok=True)
        if not os.access(rep["graph"], os.W_OK):
            rep["errors"].append("graph directory not writable: %s"
                                 % rep["graph"])
    except OSError as e:
        rep["errors"].append("cannot create graph directory %s: %s"
                             % (rep["graph"], e))

    # Free disk on the graph filesystem
    try:
        st = os.statvfs(rep["graph"])
        free = st.f_bavail * st.f_frsize / (1024 ** 3)
        rep["free_gb"] = round(free, 1)
        if free < 1.0:
            rep["warnings"].append(
                "only %.1f GB free on the graph filesystem — "
                "large projects can need several GB" % free)
    except OSError:
        pass

    # Worker count sanity
    if workers < 0:
        rep["errors"].append("workers must be >= 0 (got %d)" % workers)

    rep["ok"] = not rep["errors"]
    return rep


def _fmt_langs(rep) -> str:
    parts = []
    for lang in sorted(rep["lang_counts"], key=lambda l: -rep["lang_counts"][l]):
        parts.append("%s:%d" % (lang, rep["lang_counts"][lang]))
    return ", ".join(parts) if parts else "none"


def print_env_check_report(rep):
    print("[make] phase 1/2: environment check")
    print("  python            : %s" % rep["python"])
    print("  source            : %s" % rep["source"])
    print("  languages         : %s" % _fmt_langs(rep))
    print("  graph output      : %s%s"
          % (rep["graph"], " (existing graph; rebuild)" if rep["existing_graph"]
             else " (new)"))
    if rep["lang_counts"].get("c") or rep["lang_counts"].get("cpp"):
        print("  libclang          : %s"
              % (rep["libclang"]["lib_path"] or "not found"))
        print("  compile_commands  : %s"
              % (rep["compile_commands"] or "not found"))
    backend_line = rep["backend"]
    if rep["backend"] != rep["backend_requested"]:
        backend_line += " (requested: %s)" % rep["backend_requested"]
    print("  extraction backend: %s" % backend_line)
    if rep["free_gb"]:
        print("  free disk         : %.1f GB" % rep["free_gb"])
    for w in rep["warnings"]:
        print("  WARN: %s" % w)
    for n in rep["notes"]:
        print("  NOTE: %s" % n)
    for e in rep["errors"]:
        print("  ERROR: %s" % e)
    print("[make] env-check %s (%d error(s), %d warning(s))"
          % ("OK" if rep["ok"] else "FAILED",
             len(rep["errors"]), len(rep["warnings"])))


def _cond_index_has_data(graph: str) -> bool:
    """True if build produced a non-empty #ifdef condition index."""
    p = os.path.join(graph, ".code2database_condition_index.json")
    if not os.path.isfile(p):
        return False
    try:
        with open(p, encoding="utf-8") as f:
            return bool(json.load(f))
    except (OSError, ValueError):
        return False


def _build_steps(rep, args):
    """Assemble the full pipeline.

    Each step: (name, argv, fatal, note, requires, skip_reason).
    ``requires`` (optional callable) is evaluated at EXECUTION time —
    after earlier steps ran — so steps can depend on artifacts build
    just produced (e.g. the condition index). Core steps (scan, build)
    are fatal. Enrichment steps produce derived artifacts or exports;
    a failure there degrades to a warning so a partial-but-usable graph
    is kept.
    """
    src, graph = rep["source"], rep["graph"]
    py = sys.executable

    scan_cmd = [py, _SCANNER, "scan",
                "--source", src,
                "--output", rep["extraction_path"],
                "--extraction-backend", rep["backend"],
                "--no-interactive"]
    if getattr(args, "profile", ""):
        scan_cmd += ["--profile", args.profile]
    else:
        scan_cmd += ["--auto-profile"]
    if getattr(args, "lang", "auto") != "auto":
        scan_cmd += ["--lang", args.lang]
    if rep["compile_commands"]:
        scan_cmd += ["--compile-commands", rep["compile_commands"]]
    if getattr(args, "clang_args", ""):
        scan_cmd += ["--clang-args", args.clang_args]
    if getattr(args, "workers", 0):
        scan_cmd += ["-j", str(args.workers)]
    if getattr(args, "large_project", False):
        scan_cmd += ["--large-project"]
    if getattr(args, "memory_limit", 0):
        scan_cmd += ["--memory-limit", str(args.memory_limit)]
    if getattr(args, "parallel_mode", None):
        scan_cmd += ["--parallel-mode", args.parallel_mode]
    if getattr(args, "max_workers", 0):
        scan_cmd += ["--max-workers", str(args.max_workers)]
    if getattr(args, "macros", ""):
        scan_cmd += ["--macros", args.macros]
    if getattr(args, "macros_from", ""):
        scan_cmd += ["--macros-from", args.macros_from]
    if getattr(args, "memory_warn_threshold", 0.0):
        scan_cmd += ["--memory-warn-threshold", str(args.memory_warn_threshold)]
    if getattr(args, "memory_crit_threshold", 0.0):
        scan_cmd += ["--memory-crit-threshold", str(args.memory_crit_threshold)]
    if getattr(args, "no_body_text", False):
        scan_cmd += ["--no-body-text"]
    if getattr(args, "exclude_dirs", ""):
        scan_cmd += ["--exclude-dirs", args.exclude_dirs]
    if getattr(args, "scan_subsystems", ""):
        scan_cmd += ["--scan-subsystems", args.scan_subsystems]

    build_cmd = [py, _BUILDER, "build",
                 "--extraction", rep["extraction_path"],
                 "--outdir", graph]
    if getattr(args, "profile", ""):
        build_cmd += ["--profile", args.profile]
    if getattr(args, "workers", 0):
        build_cmd += ["-j", str(args.workers)]
    if getattr(args, "large_project", False):
        build_cmd += ["--large-project"]
    if getattr(args, "parallel_mode", None):
        build_cmd += ["--parallel-mode", args.parallel_mode]
    if getattr(args, "max_workers", 0):
        build_cmd += ["--max-workers", str(args.max_workers)]
    if getattr(args, "macros", ""):
        build_cmd += ["--macros", args.macros]
    if getattr(args, "build_config", None):
        build_cmd += ["--build-config", args.build_config]
    for plugin in getattr(args, "plugin", []) or []:
        build_cmd += ["--plugin", plugin]
    if getattr(args, "plugin_config", None):
        build_cmd += ["--plugin-config", args.plugin_config]
    storage = getattr(args, "storage", "auto")
    if storage != "auto":
        build_cmd += ["--storage", storage]
    if getattr(args, "skip_community", False):
        build_cmd += ["--skip-community"]
    if getattr(args, "low_memory", False):
        build_cmd += ["--low-memory"]
    if getattr(args, "memory_warn_threshold", 0.0):
        build_cmd += ["--memory-warn-threshold", str(args.memory_warn_threshold)]
    if getattr(args, "memory_crit_threshold", 0.0):
        build_cmd += ["--memory-crit-threshold", str(args.memory_crit_threshold)]
    if getattr(args, "memory_warn_mb", None) is not None:
        build_cmd += ["--memory-warn-mb", str(args.memory_warn_mb)]
    if getattr(args, "memory_crit_mb", None) is not None:
        build_cmd += ["--memory-crit-mb", str(args.memory_crit_mb)]
    if getattr(args, "memory_dynamic", None) is False:
        build_cmd += ["--no-memory-dynamic"]
    if getattr(args, "auto_enhance", None) is False:
        build_cmd += ["--no-auto-enhance"]

    return [
        ("scan", scan_cmd, True, "AST extraction -> %s"
         % os.path.basename(rep["extraction_path"]), None, ""),
        ("build", build_cmd, True,
         "graph construction (vtable/callback/invariants/FFI detection, "
         "doc-code alignment, context packs, cgdb layers, version snapshot)",
         None, ""),
        ("value-flow", [py, _BUILDER, "value-flow",
                        "--graph", graph, "--build"], False,
         "DATA_FLOW/RETURN_FLOW edges -> .code2database_data_flow.json",
         None, ""),
        ("data-dep", [py, _BUILDER, "data-dep",
                      "--graph", graph, "--build"], False,
         "cross-function DATA_DEP edges -> .code2database_data_dep.json",
         None, ""),
        ("extract-signals", [py, _BUILDER, "extract-signals",
                             "--graph", graph], False,
         "#ifdef condition->edges map -> .code2database_signal_map.json",
         lambda: _cond_index_has_data(graph),
         "graph has no #ifdef condition data"),
        ("ffi-detect", [py, _BUILDER, "ffi-detect",
                        "--graph", graph, "--source", src, "--apply"],
         False,
         "FFI edges -> .code2database_ffi.json + SQLite bridge tables "
         "(no-op without FFI)", None, ""),
        ("brief-extract", [py, _BUILDER, "brief-extract",
                           "--graph", graph], False,
         "project brief template -> knowledge/brief.json", None, ""),
        ("kb-rebuild-index", [py, _BUILDER, "kb-rebuild-index",
                              "--graph", graph], False,
         "unified FTS5 index (memory + brief -> kb_paragraphs)", None, ""),
        ("embeddings-build", [py, _BUILDER, "embeddings-build",
                              "--graph", graph], False,
         "TF-IDF n-gram embeddings -> embeddings.json "
         "(powers hybrid/semantic search)", None, ""),
        ("export-obsidian", [py, _BUILDER, "export-obsidian",
                             "--graph", graph], False,
         "Obsidian vault -> obsidian-vault/", None, ""),
        ("export-html", [py, _BUILDER, "export-html",
                         "--graph", graph, "--format", "vis-network"],
         False, "interactive HTML -> callgraph.html", None, ""),
        ("profile-health", [py, _BUILDER, "profile-health",
                            "--graph", graph, "--source", src]
         + (["--profile", args.profile] if getattr(args, "profile", "") else []),
         False,
         "profile health report (0-100 score)", None, ""),
    ]


def _verify_scan_completed(source, graph_dir, extraction_path):
    """Post-scan sanity check. Returns (errors, warnings) string lists.

    The scanner exits 0 even when MemoryGuard cancels the remaining
    files under system memory pressure (observed: a 4-file scan cancelled
    at 82% RAM on a busy machine), silently producing a PARTIAL graph.
    The only durable evidence is the resume checkpoint the scanner
    leaves next to its output — it is removed on clean completion, so a
    leftover checkpoint (matching this source) is treated as fatal.

    A missing or empty extraction alone is NOT proof of failure
    (header-only projects are legitimately empty; mocked runs don't
    write one), so it only warrants a warning.
    """
    errors, warnings = [], []
    checkpoint = os.path.join(graph_dir, "_scan_checkpoint.json")
    if os.path.exists(checkpoint):
        stale = False
        try:
            with open(checkpoint, encoding="utf-8") as f:
                cp = json.load(f)
            # Ignore checkpoints left by a scan of a DIFFERENT source.
            stale = cp.get("source_root", "") != os.path.abspath(source)
        except (OSError, ValueError):
            stale = False  # unreadable — assume it's ours, fail loud
        if not stale:
            errors.append(
                "scan exited 0 but a resume checkpoint remains (%s): the "
                "scan was interrupted (memory pressure / limits) and the "
                "extraction is PARTIAL. Free memory or re-run with "
                "--large-project / -j 1; the checkpoint resumes the "
                "remaining files" % checkpoint)
    # check for both monolithic extraction.json
    # AND split extraction.json.d/ directory. --large-project mode writes
    # to the split directory (per-file chunks) instead of the monolithic
    # file; the previous check only looked for the monolithic file and
    # emitted a false "scan produced no extraction" warning even when
    # the scan succeeded (SPDK: 14847 functions, DPDK: 46931 functions).
    if not os.path.isfile(extraction_path) and \
            not os.path.isdir(extraction_path + ".d"):
        warnings.append(
            "scan produced no extraction at %s (or %s.d/) — the build "
            "step will decide whether this is fatal"
            % (extraction_path, extraction_path))
    else:
        try:
            with open(extraction_path, encoding="utf-8") as f:
                data = json.load(f)
            if not data.get("functions") and not data.get("edges"):
                warnings.append(
                    "extraction is empty (0 functions, 0 edges) — if the "
                    "source has implementation files, check the scan "
                    "output above (backend / compile_commands issues)")
        except (OSError, ValueError):
            warnings.append(
                "extraction at %s is unreadable" % extraction_path)
    return errors, warnings


def _do_make(rep, args):
    """Phase 2: run the full pipeline with per-step failure policy.

    Derived steps that produce only JSON/file artifacts (no DB writes)
    run in a ThreadPoolExecutor — each is a subprocess that loads the
    graph independently, so this trades memory for wall-clock. DB-writing
    steps (ffi-detect, kb-rebuild-index) and the brief-extract →
    kb-rebuild-index dependency chain stay serial. --serial-derived
    opts out for memory-constrained environments.
    """
    # Daemon conflict detection: make writes to the same SQLite DB the
    # daemon watches. If the daemon is online its inotify watcher may
    # trigger a concurrent sync mid-build → "database is locked" and
    # inconsistent graph state. Abort unless --force is given.
    graph_dir = rep.get("graph", "")
    if graph_dir:
        try:
            from _builder.daemon.daemon import is_daemon_running
            if is_daemon_running(graph_dir):
                if getattr(args, "force", False):
                    print("[make] WARNING: daemon is running for %s — "
                          "proceeding with --force (SQLite write-lock "
                          "conflicts may occur)" % graph_dir, file=sys.stderr)
                else:
                    print("[make] ERROR: daemon is running for %s.\n"
                          "  The daemon's inotify watcher will trigger a "
                          "concurrent sync during the build, causing\n"
                          "  SQLite 'database is locked' errors and "
                          "potential graph corruption.\n"
                          "  Run 'daemon-stop --graph %s' first, or use "
                          "'--force' to proceed at your own risk."
                          % (graph_dir, graph_dir), file=sys.stderr)
                    sys.exit(1)
        except ImportError:
            pass  # daemon module unavailable — skip check

    steps = _build_steps(rep, args)
    total = len(steps)
    failures, skipped = [], []
    serial_derived = bool(getattr(args, "serial_derived", False))

    # Steps whose only output is JSON/files (no SQLite writes, no
    # inter-step dependency) — safe to run concurrently.
    _PARALLEL = frozenset({
        "value-flow", "data-dep", "extract-signals",
        "embeddings-build", "export-obsidian", "export-html",
        "profile-health",
    })

    print("\n[make] phase 2/2: build pipeline (%d steps)" % total)
    step_num = 0

    # --- Fatal steps (scan, build) — always serial ---
    for name, cmd, fatal, note, requires, skip_reason in steps:
        if not fatal:
            break
        step_num += 1
        print("\n[make] step %d/%d: %s — %s" % (step_num, total, name, note))
        if requires is not None and not requires():
            print("  SKIPPED (%s)" % skip_reason)
            skipped.append(name)
            continue
        print("  $ %s" % " ".join(cmd))
        rc = subprocess.run(cmd).returncode
        if rc == 0 and name == "scan":
            _errs, _warns = _verify_scan_completed(
                rep["source"], rep["graph"], rep["extraction_path"])
            for _w in _warns:
                print("[make] WARN: %s" % _w, file=sys.stderr)
            if _errs:
                for _e in _errs:
                    print("\n[make] FAILED after step %d/%d (scan): %s"
                          % (step_num, total, _e), file=sys.stderr)
                sys.exit(1)
        if rc != 0:
            print("\n[make] FAILED at step %d/%d (%s, exit %d) — "
                  "fix the issue above and re-run make"
                  % (step_num, total, name, rc), file=sys.stderr)
            sys.exit(rc)

    # --- Derived steps ---
    derived = [s for s in steps if not s[2]]
    if serial_derived:
        parallel_steps, serial_steps = [], derived
    else:
        parallel_steps = [s for s in derived if s[0] in _PARALLEL]
        serial_steps = [s for s in derived if s[0] not in _PARALLEL]

    # Phase A: parallel (JSON/file-only steps)
    if parallel_steps:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        n_par = len(parallel_steps)
        print("\n[make] derived steps (parallel, %d)" % n_par)
        with ThreadPoolExecutor(max_workers=min(4, n_par)) as pool:
            futures = {}
            for name, cmd, _fatal, note, requires, skip_reason in parallel_steps:
                step_num += 1
                if requires is not None and not requires():
                    print("  step %d/%d: %s — SKIPPED (%s)"
                          % (step_num, total, name, skip_reason))
                    skipped.append(name)
                    continue
                print("  step %d/%d: %s — %s" % (step_num, total, name, note))
                futures[pool.submit(subprocess.run, cmd)] = name
            for future in as_completed(futures):
                name = futures[future]
                rc = future.result().returncode
                if rc != 0:
                    print("[make] WARN: step %s failed (exit %d) — continuing"
                          % (name, rc), file=sys.stderr)
                    failures.append(name)

    # Phase B: serial (DB writes + brief→kb dependency chain)
    for name, cmd, _fatal, note, requires, skip_reason in serial_steps:
        step_num += 1
        print("\n[make] step %d/%d: %s — %s" % (step_num, total, name, note))
        if requires is not None and not requires():
            print("  SKIPPED (%s)" % skip_reason)
            skipped.append(name)
            continue
        print("  $ %s" % " ".join(cmd))
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print("[make] WARN: step %s failed (exit %d) — continuing; "
                  "the artifact it produces will be missing until re-run"
                  % (name, rc), file=sys.stderr)
            failures.append(name)

    graph = rep["graph"]
    print("\n[make] done: %d/%d steps OK, %d skipped, %d failed"
          % (total - len(failures) - len(skipped), total,
             len(skipped), len(failures)))
    if skipped:
        print("[make] skipped: %s" % ", ".join(skipped))
    if failures:
        print("[make] failed (non-fatal, graph remains usable): %s"
              % ", ".join(failures), file=sys.stderr)
    print("[make] graph: %s" % graph)
    print("[make] next steps:")
    print("  session context: %s session-init --graph %s"
          % (_BUILDER, graph))
    print("  MCP server     : %s serve --graph %s   (83 tools)"
          % (_BUILDER, graph))
    print("  web UI         : %s web-ui --graph %s"
          % (_BUILDER, graph))
    if not rep["existing_brief"]:
        print("  brief curation : %s brief-update --set one_liner "
              "--value '...'" % _BUILDER)
    if failures:
        sys.exit(2)


def cmd_make(args):
    """CLI entry: env-check (fail fast) then build."""
    rep = run_env_check(
        source=args.source,
        graph=args.graph,
        backend_requested=getattr(args, "extraction_backend", "auto"),
        compile_commands=getattr(args, "compile_commands", ""),
        workers=getattr(args, "workers", 0),
        lang_requested=getattr(args, "lang", "auto"),
    )
    print_env_check_report(rep)
    if not rep["ok"]:
        print("[make] env-check failed — nothing was built; "
              "fix the ERROR items above and re-run",
              file=sys.stderr)
        sys.exit(1)
    if getattr(args, "check", False):
        print("[make] --check given: environment check only, no build")
        return
    _do_make(rep, args)
