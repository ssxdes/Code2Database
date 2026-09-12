#!/usr/bin/env python3
"""c2d umbrella command: goal-oriented entry into the Code2Database CLI.

The builder CLI has 250+ subcommands — powerful, but neither humans nor
agents can hold that surface in their heads. `c2d` exposes a small set
of lifecycle verbs and executes multi-step "recipes" (see recipes.py)
so a single natural-language question turns into the right read-only
command sequence with aggregated output.

Implemented verbs (this module grows verb by verb):

    setup    --source DIR
              one-click ingest (delegates to make)
    session  load the project context (delegates to session-init)
    ask      --question "..." | --recipe NAME [explicit params]
              classify the question, run the matched recipe
    capture  --question ... --answer ...
              save a Q&A into project memory (delegates to save-memory)
    freshen  check graph freshness and route to the right update path
    report   --kind design|diagnose|html|mermaid|plantuml
              produce an artifact (delegates to the export commands)
    recipes  [NAME]
              list recipes, or show one recipe in detail
    verbs    print the lifecycle cheat sheet (also the default action)

Every recipe step and every delegation runs as a subprocess of this
same CLI (same model as `make`), so steps are isolated, streaming, and
use the exact same code paths as manual invocations. Recipe steps are
restricted to read-only commands: WRITE_COMMANDS is refused at
execution time, and the test suite pins every recipe step against the
real argparse tree.
"""
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from _builder.flow.recipes import (
    KNOWN_PARAMS, RECIPES, classify_question, get_recipe, missing_params,
)

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                            "..", ".."))
_BUILDER = os.path.join(_SCRIPTS_DIR, "code2database_builder.py")

# Commands that mutate the graph, the database, the memory stores, or
# the project profile. Recipe steps may never be any of these — `c2d
# ask` is a read-only surface by contract (DB writes keep their
# report-and-confirm protocol in the dedicated commands).
WRITE_COMMANDS = frozenset({
    # graph / DB writes
    "update-node", "update-edge", "patch-profile", "apply-semantics",
    "apply-invariants", "auto-enhance", "batch-confirm", "rollback",
    "fill-request", "doc-mark-stale", "profile-evolve", "merge-changes",
    "tx-begin", "tx-commit", "tx-rollback", "tx-restore", "tx-snapshot",
    "add-function", "delete-node", "insert-node-after",
    "add-semantic-edges", "ffi-persist", "ffi-auto-link",
    "commit-db-transaction", "rollback-db-transaction",
    "classify-endpoints", "graph-record-version", "heuristic-enhance",
    "brief-extract", "brief-update", "brief-migrate-legacy",
    "kb-global-add", "kb-global-import", "kb-global-import-memory",
    "kb-global-share", "kb-global-share-memory", "kb-forget",
    "kb-rollback", "kb-migrate", "manage-memory", "kb-rebuild-index",
    # memory capture (the dedicated `capture` verb is the surfaced path)
    "save-memory", "save",
    # ingest / rebuild — the `setup` verb is the surfaced path
    "make", "build", "build-multi", "build-diff", "update", "quick-update",
    "build-update", "merge", "sync", "watch", "install-hook",
    "patch-from-diff", "patch-from-git", "light-scan", "scan-rpc",
    "daemon-start", "daemon-stop", "daemon-force-refresh",
    "daemon-pause", "daemon-resume", "daemon-reload",
    "embeddings-build",
})

# Step-level flags that flip an otherwise read-only command into a
# writer (e.g. `extract-invariants --apply`). Refused alongside
# WRITE_COMMANDS.
WRITE_FLAGS = frozenset({"--apply", "--correct"})


def _resolve_graph_dir() -> str:
    """Locate the graph directory when --graph is omitted.

    Mirrors the builder's own auto-discovery (code2database_builder.py
    _resolve_graph_dir). The umbrella resolves the graph lazily — only
    inside verbs that need one (ask) — so non-graph verbs (verbs,
    recipes, setup) stay noise-free and setup can forward the user's
    explicit --graph (or none) to make untouched.
    """
    d = os.getcwd()
    while True:
        cand = os.path.join(d, "code2db-out")
        if os.path.isfile(os.path.join(cand, "code2database.db")):
            return cand
        if os.path.isfile(os.path.join(d, "code2database.db")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return "code2db-out"
        d = parent


def _builder_argv(*extra: str) -> List[str]:
    """Argv for a subprocess of this same CLI."""
    return [sys.executable, _BUILDER] + list(extra)


def _delegate(argv: List[str], dry_run: bool) -> int:
    """Run a delegated command as a subprocess, echoing it first."""
    print("[c2d] $ %s" % " ".join(argv))
    if dry_run:
        return 0
    return subprocess.run(argv).returncode


def _placeholder_name(token: str) -> Optional[str]:
    """Return the param name for a '{name}' token, else None."""
    if token.startswith("{") and token.endswith("}") and len(token) > 2:
        return token[1:-1]
    return None


def resolve_step_args(args_template: List[str],
                      params: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """Fill '{param}' placeholders in a step's argv template.

    Returns (resolved_argv, unresolved_names). Unresolved placeholders
    keep their '{name}' form in the argv so dry-run output shows what
    is missing.
    """
    out: List[str] = []
    unresolved: List[str] = []
    for token in args_template:
        name = _placeholder_name(token)
        if name is None:
            out.append(token)
            continue
        if params.get(name):
            out.append(params[name])
        else:
            out.append(token)
            unresolved.append(name)
    return out, unresolved


def execute_recipe(recipe: Dict[str, Any], params: Dict[str, str],
                   graph: str = "", dry_run: bool = False,
                   json_out: bool = False) -> Dict[str, Any]:
    """Run every step of `recipe` as a subprocess of this CLI.

    Streams each step's output under a banner; a failing step warns and
    the run continues (analysis recipes produce partial value). Returns
    a structured summary dict (also printed as JSON with json_out).
    """
    steps = recipe["steps"]
    total = len(steps)
    report: Dict[str, Any] = {
        "recipe": recipe["name"],
        "params": dict(params),
        "steps": [],
        "run": 0, "skipped": 0, "failed": 0,
    }
    print("[c2d] recipe: %s — %s" % (recipe["name"], recipe["summary"]))
    shown = {k: v for k, v in params.items() if v}
    if shown:
        print("[c2d] params: %s"
              % ", ".join("%s=%s" % kv for kv in sorted(shown.items())))
    for i, step in enumerate(steps, 1):
        argv_extra, unresolved = resolve_step_args(step.get("args", []),
                                                   params)
        label = "%d/%d: %s" % (i, total, step["cmd"])
        if unresolved:
            reason = "missing param: %s" % ", ".join(sorted(unresolved))
            if step.get("optional"):
                print("[c2d] step %s — SKIPPED (%s)" % (label, reason))
                report["steps"].append({"cmd": step["cmd"],
                                        "skipped": reason})
                report["skipped"] += 1
                continue
            # Defensive: a non-optional step with an unresolved
            # placeholder is a registry defect (tests pin this); skip
            # loudly rather than run a malformed command.
            print("[c2d] step %s — SKIPPED (%s; non-optional step)"
                  % (label, reason), file=sys.stderr)
            report["steps"].append({"cmd": step["cmd"],
                                    "skipped": reason + " (non-optional)"})
            report["skipped"] += 1
            continue
        if step["cmd"] in WRITE_COMMANDS:
            reason = "command is not read-only"
            print("[c2d] step %s — REFUSED (%s)" % (label, reason),
                  file=sys.stderr)
            report["steps"].append({"cmd": step["cmd"],
                                    "skipped": reason})
            report["skipped"] += 1
            continue
        bad_flags = [a for a in argv_extra if a in WRITE_FLAGS]
        if bad_flags:
            reason = "write flag %s" % ", ".join(bad_flags)
            print("[c2d] step %s — REFUSED (%s)" % (label, reason),
                  file=sys.stderr)
            report["steps"].append({"cmd": step["cmd"], "skipped": reason})
            report["skipped"] += 1
            continue
        cmd_argv = [sys.executable, _BUILDER, step["cmd"]] + argv_extra
        if graph:
            cmd_argv += ["--graph", graph]
        print("[c2d] step %s — %s" % (label, step.get("note", "")))
        print("  $ %s" % " ".join(cmd_argv))
        if dry_run:
            report["steps"].append({"cmd": step["cmd"],
                                    "argv": cmd_argv[2:], "dry_run": True})
            report["run"] += 1
            continue
        rc = subprocess.run(cmd_argv).returncode
        entry = {"cmd": step["cmd"], "argv": cmd_argv[2:], "rc": rc}
        if rc != 0:
            report["failed"] += 1
            print("[c2d] step %s — FAILED (exit %d), continuing"
                  % (label, rc), file=sys.stderr)
        else:
            report["run"] += 1
        report["steps"].append(entry)
    print("[c2d] recipe complete: %d steps — %d ok, %d failed, %d skipped"
          % (total, report["run"], report["failed"], report["skipped"]))
    if json_out:
        print(json.dumps(report, indent=2))
    return report


def _explicit_params(args) -> Dict[str, str]:
    """Explicit CLI flags, mapped to recipe placeholder names."""
    out = {
        "target": getattr(args, "target", "") or "",
        "from": getattr(args, "from_node", "") or "",
        "to": getattr(args, "to_node", "") or "",
        "query": getattr(args, "query", "") or "",
        "source": getattr(args, "source", "") or "",
    }
    return {k: v for k, v in out.items() if v}


def _run_intent_fallback(question: str, graph: str,
                         dry_run: bool) -> int:
    """No recipe matched: try the single-command intent router.

    Executes the suggested read-only command directly (validated
    against WRITE_COMMANDS), or points at `c2d recipes` when nothing
    matches at all.
    """
    from _builder.misc.intent_router import classify_intent
    routing = classify_intent(question)
    if routing is None or not routing.get("command"):
        print("[c2d] no recipe matched: %r" % question)
        print("[c2d] list available recipes with: c2d recipes")
        print("[c2d] or run a recipe explicitly: c2d ask --recipe <name>")
        return 1
    cmd = routing["command"]
    if cmd in WRITE_COMMANDS:
        print("[c2d] intent routed to %s, which is not read-only; "
              "refusing to auto-run it" % cmd, file=sys.stderr)
        return 1
    argv_extra = []
    for k, v in sorted(routing.get("args", {}).items()):
        if v:
            argv_extra += ["--%s" % k, v]
    bad = [a for a in argv_extra if a in WRITE_FLAGS]
    if bad:
        print("[c2d] intent args carry write flag %s; refusing"
              % ", ".join(bad), file=sys.stderr)
        return 1
    print("[c2d] no recipe matched; single-command intent: %s (%s)"
          % (routing["matched_intent"], routing["reason"]))
    cmd_argv = [sys.executable, _BUILDER, cmd] + argv_extra
    if graph:
        cmd_argv += ["--graph", graph]
    print("  $ %s" % " ".join(cmd_argv))
    if dry_run:
        return 0
    rc = subprocess.run(cmd_argv).returncode
    print("[c2d] intent command finished (exit %d)" % rc)
    return rc


def _action_ask(args) -> int:
    explicit = _explicit_params(args)
    graph = getattr(args, "graph", "") or ""
    if not graph:
        graph = _resolve_graph_dir()
        print("[graph] --graph not given; using %s" % graph,
              file=sys.stderr)
    recipe = None
    extracted: Dict[str, str] = {}
    if getattr(args, "recipe", ""):
        recipe = get_recipe(args.recipe)
        if recipe is None:
            print("[c2d] unknown recipe: %r" % args.recipe, file=sys.stderr)
            print("[c2d] available recipes: %s"
                  % ", ".join(r["name"] for r in RECIPES),
                  file=sys.stderr)
            return 2
    elif getattr(args, "question", ""):
        recipe, extracted = classify_question(args.question)
        if recipe is None:
            return _run_intent_fallback(args.question, graph,
                                        bool(getattr(args, "dry_run", False)))
    else:
        print("[c2d] ask needs --question \"...\" or --recipe <name>",
              file=sys.stderr)
        print("[c2d] examples:", file=sys.stderr)
        print("  c2d ask --question \"is bdev_start thread safe?\"",
              file=sys.stderr)
        print("  c2d ask --recipe impact --target bdev_start",
              file=sys.stderr)
        return 2
    params = dict(extracted)
    params.update(explicit)  # explicit flags win over extraction
    gaps = missing_params(recipe, params)
    if gaps:
        print("[c2d] recipe %r needs: %s"
              % (recipe["name"], ", ".join(gaps)), file=sys.stderr)
        print("[c2d] example question: %r"
              % recipe.get("example", ""), file=sys.stderr)
        print("[c2d] or pass explicitly: %s"
              % " ".join("--%s <value>" % g for g in gaps), file=sys.stderr)
        return 2
    execute_recipe(recipe, params, graph=graph,
                   dry_run=bool(getattr(args, "dry_run", False)),
                   json_out=bool(getattr(args, "json", False)))
    return 0


def _action_setup(args) -> int:
    """One-click ingest: delegate to make with translated args."""
    source = getattr(args, "source", "") or ""
    if not source:
        print("[c2d] setup needs --source <dir>", file=sys.stderr)
        print("[c2d] example: c2d setup --source /path/to/project",
              file=sys.stderr)
        return 2
    dry_run = bool(getattr(args, "dry_run", False))
    argv = _builder_argv("make", "--source", source)
    graph = getattr(args, "graph", "") or ""
    if graph:
        argv += ["--graph", graph]
    if getattr(args, "check", False):
        argv += ["--check"]
    rc = _delegate(argv, dry_run)
    if rc == 0 and not dry_run and not getattr(args, "check", False):
        print("[c2d] ingest complete — next: c2d session")
    return rc


def _action_session(args) -> int:
    """One-shot context load: delegate to session-init."""
    argv = _builder_argv("session-init")
    graph = getattr(args, "graph", "") or ""
    if graph:
        argv += ["--graph", graph]
    top = getattr(args, "top", 0) or 0
    if top:
        argv += ["--top", str(top)]
    if getattr(args, "json", False):
        argv += ["--json"]
    return _delegate(argv, bool(getattr(args, "dry_run", False)))


def _action_capture(args) -> int:
    """Save a Q&A into project memory: delegate to save-memory."""
    question = getattr(args, "question", "") or ""
    answer = getattr(args, "answer", "") or ""
    if not question or not answer:
        print("[c2d] capture needs --question and --answer",
              file=sys.stderr)
        print("[c2d] example: c2d capture --question \"...\" "
              "--answer \"...\" --category bdev --author you",
              file=sys.stderr)
        return 2
    argv = _builder_argv("save-memory", "--question", question,
                         "--answer", answer)
    graph = getattr(args, "graph", "") or ""
    if graph:
        argv += ["--graph", graph]
    category = getattr(args, "category", "") or ""
    if category:
        argv += ["--category", category]
    author = getattr(args, "author", "") or ""
    if author:
        argv += ["--author", author]
    for sym in getattr(args, "symbol", None) or []:
        argv += ["--symbol", sym]
    if getattr(args, "correct", False):
        argv += ["--correct"]
    return _delegate(argv, bool(getattr(args, "dry_run", False)))


def _action_freshen(args) -> int:
    """Freshness check with routing to the right update path.

    Exit codes: 0 = fresh (or daemon active), 1 = action needed,
    2 = usage error.
    """
    graph = getattr(args, "graph", "") or ""
    if not graph:
        graph = _resolve_graph_dir()
        print("[graph] --graph not given; using %s" % graph,
              file=sys.stderr)
    if not os.path.isfile(os.path.join(graph, "code2database_master.json")):
        print("[c2d] no graph found at %s" % graph, file=sys.stderr)
        print("[c2d] build one first: c2d setup --source <dir>",
              file=sys.stderr)
        return 1
    # A running daemon keeps the graph fresh on its own — show its
    # status rather than duplicating freshness logic.
    try:
        from _builder.daemon.daemon import is_daemon_running
        if is_daemon_running(graph):
            print("[c2d] daemon is active for this graph and keeps it "
                  "fresh")
            print("[c2d] before important queries, block on any in-flight "
                  "sync with daemon-wait-sync")
            return _delegate(_builder_argv("daemon-status", "--graph",
                                           graph),
                             bool(getattr(args, "dry_run", False)))
    except ImportError:
        pass  # daemon module unavailable — fall through to the check
    # Derive the source root the same way session-init does: the
    # build wrote the real source path into the master manifest; the
    # parent dir is only the legacy convention.
    src_root = ""
    try:
        with open(os.path.join(graph, "code2database_master.json"),
                  encoding="utf-8") as f:
            src_root = (json.load(f).get("source_root", "") or "")
    except Exception:
        pass
    if not src_root:
        src_root = os.path.dirname(os.path.abspath(graph))
    try:
        from _builder.cgdb.cgdb_freshness import check_freshness
        fr = check_freshness(graph, src_root)
    except Exception as exc:
        print("[c2d] freshness check unavailable: %s" % exc,
              file=sys.stderr)
        return 1
    if fr.get("is_fresh", True):
        print("[c2d] graph is fresh (source matches the build)")
        return 0
    counts = (fr.get("changed_count", 0), fr.get("new_count", 0),
              fr.get("deleted_count", 0))
    rec = fr.get("recommendation", "")
    if any(counts):
        print("[c2d] graph is STALE — %d changed / %d new / %d deleted"
              % counts, file=sys.stderr)
        samples = (fr.get("changed_files") or [])[:3]
        for s in samples:
            print("[c2d]   e.g. %s" % s, file=sys.stderr)
        if fr.get("git_head_changed", False):
            print("[c2d]   git HEAD moved since the build", file=sys.stderr)
        if rec:
            print("[c2d] %s" % rec)
    else:
        # Zero counts + not fresh: the check itself could not run
        # (e.g. no scan manifest) — surface its recommendation.
        print("[c2d] graph freshness cannot be confirmed", file=sys.stderr)
        if rec:
            print("[c2d] %s" % rec, file=sys.stderr)
    print("[c2d] pick an update path:")
    print("[c2d]   c2d setup --source %s      (full rebuild, safest)"
          % src_root)
    print("[c2d]   daemon-start --graph %s    (watch + auto-sync)"
          % graph)
    print("[c2d]   build-update --source %s --graph %s  (per-file)"
          % (src_root, graph))
    return 1


_REPORT_KINDS = {
    "design": "design-doc",
    "diagnose": "diagnose",
    "html": "export-html",
    "mermaid": "export-mermaid",
    "plantuml": "export-plantuml",
}


def _action_report(args) -> int:
    """Produce an artifact by delegating to the export commands."""
    kind = getattr(args, "kind", "") or ""
    if not kind:
        print("[c2d] report needs --kind (one of: %s)"
              % ", ".join(sorted(_REPORT_KINDS)), file=sys.stderr)
        print("[c2d] example: c2d report --kind design --module fs",
              file=sys.stderr)
        return 2
    if kind not in _REPORT_KINDS:
        print("[c2d] unknown report kind: %r (valid: %s)"
              % (kind, ", ".join(sorted(_REPORT_KINDS))), file=sys.stderr)
        return 2
    graph = getattr(args, "graph", "") or ""
    if not graph:
        graph = _resolve_graph_dir()
        print("[graph] --graph not given; using %s" % graph,
              file=sys.stderr)
    argv = _builder_argv(_REPORT_KINDS[kind], "--graph", graph)
    module = getattr(args, "module", "") or ""
    if module:
        argv += ["--module", module]
    symbols = getattr(args, "symbol", None) or []
    if kind == "diagnose":
        if not symbols:
            print("[c2d] report --kind diagnose needs --symbol <fn>",
                  file=sys.stderr)
            return 2
        argv += ["--symbol", symbols[0]]
        log = getattr(args, "log", "") or ""
        if log:
            argv += ["--log", log]
    target = getattr(args, "target", "") or ""
    if kind in ("mermaid", "plantuml") and target:
        argv += ["--node", target]
    mode = getattr(args, "mode", "") or ""
    if kind in ("mermaid", "plantuml") and mode:
        argv += ["--mode", mode]
    output = getattr(args, "output", "") or ""
    if output:
        argv += ["--output", output]
    return _delegate(argv, bool(getattr(args, "dry_run", False)))


def _action_recipes(args) -> int:
    name = getattr(args, "recipe", "") or ""
    if name:
        recipe = get_recipe(name)
        if recipe is None:
            print("[c2d] unknown recipe: %r" % name, file=sys.stderr)
            print("[c2d] available recipes: %s"
                  % ", ".join(r["name"] for r in RECIPES),
                  file=sys.stderr)
            return 2
        print("recipe: %s" % recipe["name"])
        print("summary: %s" % recipe["summary"])
        print("requires: %s"
              % (", ".join(recipe["requires"]) if recipe["requires"]
                 else "(nothing — runs graph-wide)"))
        print("example: %s" % recipe.get("example", ""))
        print("steps:")
        for i, step in enumerate(recipe["steps"], 1):
            note = (" — " + step["note"]) if step.get("note") else ""
            opt = " [optional]" if step.get("optional") else ""
            print("  %d. %s %s%s%s"
                  % (i, step["cmd"],
                     " ".join(step.get("args", [])), note, opt))
        print("patterns (used by --question classification):")
        for pat in recipe["patterns"]:
            print("  %r" % pat)
        return 0
    print("Available ask recipes (%d):" % len(RECIPES))
    for recipe in RECIPES:
        req = ("requires: %s" % ", ".join(recipe["requires"])
               if recipe["requires"] else "graph-wide")
        print("  %-18s %s (%s)" % (recipe["name"], recipe["summary"], req))
    print("run with: c2d ask --recipe <name> [params]  or  "
          "c2d ask --question \"...\"")
    return 0


def _print_lifecycle() -> None:
    print("Code2Database lifecycle — the only flow you need:")
    print()
    print("  1. setup    — build the code database from a source tree")
    print("                  c2d setup --source /path/to/project")
    print()
    print("  2. session  — load the project context (brief + memory +")
    print("                  graph state + known-unknowns)")
    print("                  c2d session")
    print()
    print("  3. ask      — ask any code question; recipes pick the right")
    print("                  read-only command sequence and aggregate output")
    print("                  c2d ask --question \"is bdev_start thread safe?\"")
    print("                  c2d ask --recipe impact --target bdev_start")
    print()
    print("  4. capture  — save a Q&A into project memory for future")
    print("                  sessions")
    print("                  c2d capture --question \"...\" --answer \"...\"")
    print()
    print("  as needed:  c2d freshen — check graph freshness, route to")
    print("              the right update path")
    print("              c2d report --kind design|diagnose|html|")
    print("              mermaid|plantuml — produce an artifact")
    print("              c2d recipes — list the ask recipes")
    print("              (self-documenting, with detail views)")
    print()
    print("Every recipe step and delegation is a normal CLI subcommand,")
    print("shown before it runs (preview with --dry-run). The full")
    print("command surface stays available for direct use.")


def cmd_c2d(args) -> None:
    """CLI handler for the c2d umbrella command."""
    action = getattr(args, "action", "verbs") or "verbs"
    if action == "ask":
        sys.exit(_action_ask(args))
    if action == "setup":
        sys.exit(_action_setup(args))
    if action == "session":
        sys.exit(_action_session(args))
    if action == "capture":
        sys.exit(_action_capture(args))
    if action == "freshen":
        sys.exit(_action_freshen(args))
    if action == "report":
        sys.exit(_action_report(args))
    if action == "recipes":
        sys.exit(_action_recipes(args))
    if action == "verbs":
        _print_lifecycle()
        return
    print("[c2d] unknown action: %r" % action, file=sys.stderr)
    sys.exit(2)
