#!/usr/bin/env python3
"""Family umbrellas: the visible command surface and its legacy aliases.

The builder CLI collapses 27 prefix families into one visible umbrella
command each (`tx begin`, `kb query`, `cgdb find-invokers`, ...) while
the 155 legacy spellings (`tx-begin`, `kb-query`, ...) keep parsing —
they are hidden from --help only, so scripts, tests, c2d recipes and
muscle memory keep working.

This module is the single source of truth for the family tables:

- code2database_builder.py registers the umbrella parsers and rewrites
  argv before argparse sees it (``_apply_family_umbrella``);
- flow/entry.py and misc/intent_router.py render and execute the
  umbrella spelling (``umbrella_display`` / ``umbrella_argv``), so the
  c2d recipes and the intent router teach the visible surface.

Invariant (pinned by tests/test_family_umbrellas.py): for every legacy
name ``n``, ``_apply_family_umbrella(umbrella_argv(n) + rest) == [n] + rest``
— the umbrella spelling executes identically to the legacy one.
"""
import sys
from typing import Dict, List

_FAMILY_UMBRELLAS: Dict[str, Dict[str, str]] = {
    "tx": {
        "begin": "tx-begin", "commit": "tx-commit", "rollback": "tx-rollback",
        "status": "tx-status", "snapshot": "tx-snapshot", "restore": "tx-restore",
        "list-snapshots": "tx-list-snapshots", "replay-wal": "tx-replay-wal",
    },
    "kb": {
        "init": "kb-init", "query": "kb-query",
        "rebuild-index": "kb-rebuild-index", "cluster": "kb-cluster",
        "migrate": "kb-migrate", "known-unknowns": "kb-known-unknowns",
        "audit": "kb-audit", "conflict": "kb-conflict",
        "rollback": "kb-rollback", "forget": "kb-forget",
    },
    "kb-global": {
        "add": "kb-global-add", "search": "kb-global-search",
        "share": "kb-global-share", "import": "kb-global-import",
        "share-memory": "kb-global-share-memory",
        "search-memory": "kb-global-search-memory",
        "import-memory": "kb-global-import-memory",
    },
    "kb-domain": {
        "name": "kb-domain-name", "add": "kb-domain-add",
        "list": "kb-domain-list", "remove": "kb-domain-remove",
    },
    "foreign": {
        "add": "c2d-add-foreign", "sync": "c2d-sync-foreign",
        "list": "c2d-list-foreign", "remove": "c2d-remove-foreign",
        "resolve": "c2d-resolve-foreign", "prune": "c2d-prune-foreign",
        "pin": "c2d-pin-foreign", "unpin": "c2d-unpin-foreign",
        "check-compat": "c2d-check-compat",
        "add-stub": "c2d-add-foreign-stub",
    },
    "brief": {
        "show": "knowledge-brief", "update": "brief-update",
        "extract": "brief-extract", "validate": "brief-validate",
        "suggest": "brief-suggest", "migrate-legacy": "brief-migrate-legacy",
    },
    "check": {
        "cycles": "check-cycles", "recursion": "check-recursion",
        "bounds": "check-bounds", "infinite-loop": "check-infinite-loop",
        "clones": "check-clones",
    },
    "doc": {
        "code-check": "doc-code-check", "mark-stale": "doc-mark-stale",
        "alignment-report": "doc-alignment-report",
        "signature-diff": "doc-signature-diff",
    },
    "ffi": {
        "detect": "ffi-detect", "list": "ffi-list", "trace": "ffi-trace",
        "types": "ffi-types", "auto-link": "ffi-auto-link",
        "persist": "ffi-persist",
    },
    "who": {
        "allocates": "who-allocates", "frees": "who-frees",
        "locks": "who-locks", "unbalanced": "unbalanced-alloc-free",
    },
    "graph": {
        "history": "graph-history", "diff": "graph-diff",
        "record-version": "graph-record-version",
        "provenance": "graph-provenance",
    },
    "cgdb": {
        "query": "cgdb-query", "time-travel": "cgdb-time-travel",
        "configs-for": "cgdb-configs-for", "ops-impls": "cgdb-ops-impls",
        "cfg-paths": "cgdb-cfg-paths", "data-flow": "cgdb-data-flow",
        "race-check": "cgdb-race-check", "index-status": "cgdb-index-status",
        "sql": "cgdb-sql", "views": "cgdb-views",
        "schema-version": "cgdb-schema-version", "versions": "cgdb-versions",
        "find-invokers": "cgdb-find-invokers", "find-invoked": "cgdb-find-invoked",
        "path": "cgdb-path", "definition": "cgdb-definition",
        "function-body": "cgdb-function-body",
        "struct-layout": "cgdb-struct-layout",
        "type-definition": "cgdb-type-definition",
        "nodes-under-config": "cgdb-nodes-under-config",
        "path-feasible": "cgdb-path-feasible", "get-source": "cgdb-get-source",
        "layer-summary": "cgdb-layer-summary",
        "merge-knowledge": "cgdb-merge-knowledge", "suggest": "cgdb-suggest",
        "tour": "cgdb-tour", "freshness": "cgdb-freshness",
        "compare": "cgdb-compare", "coverage": "cgdb-coverage",
        "write-coverage": "cgdb-write-coverage",
    },
    "profile": {
        "health": "profile-health", "evolve": "profile-evolve",
        "bind-version": "profile-bind-version",
    },
    "search": {
        "hybrid": "hybrid-search", "semantic": "semantic-search",
    },
    "embeddings": {
        "build": "embeddings-build", "search": "embeddings-search",
    },
    "memory": {
        "save": "save-memory", "search": "search-memory",
        "validate": "validate-memory", "manage": "manage-memory",
        "health": "memory-health",
    },
    "pp": {
        "macros": "find-macros", "branches": "get-pp-branches",
        "strings": "get-string-literals",
    },
    "token": {
        "edit": "edit-token", "insert": "insert-token",
        "delete": "delete-token",
    },
    "node": {
        "insert-after": "insert-node-after", "delete": "delete-node",
        "add-function": "add-function",
    },
    "writeback": {
        "commit": "commit-db-transaction",
        "rollback": "rollback-db-transaction",
    },
    "fed": {
        "register": "federate-register", "list": "federate-list",
        "remove": "federate-remove", "search": "fed-search",
        "neighbors": "fed-neighbors", "path": "fed-path",
    },
    "trace": {
        "forward": "trace-chain", "reverse": "reverse-trace",
        "diff": "diff-chains",
    },
    "concurrency": {
        "risks": "concurrency-risks", "detect-races": "detect-races",
        "analyze": "concurrency-analyze",
        "happens-before": "happens-before",
        "memory-ordering": "memory-ordering",
    },
    "invariants": {
        "extract": "extract-invariants", "find": "find-invariants",
        "apply": "apply-invariants", "extract-llm": "extract-invariants-llm",
    },
    "export": {
        "mermaid": "export-mermaid", "plantuml": "export-plantuml",
        "sarif": "sarif-export",
    },
    "build": {
        "update": "build-update", "multi": "build-multi",
        "diff": "build-diff",
    },
    "daemon": {
        "start": "daemon-start", "stop": "daemon-stop",
        "status": "daemon-status", "force-refresh": "daemon-force-refresh",
        "pause": "daemon-pause", "resume": "daemon-resume",
        "wait-sync": "daemon-wait-sync", "logs": "daemon-logs",
        "reload": "daemon-reload", "list-projects": "daemon-list-projects",
    },
}

# Every hidden legacy spelling is an action target of exactly one
# family; the alias primaries (trace, concurrency, export, daemon,
# brief, build, search) additionally keep their bare form (= canonical).

# Inverse map: legacy spelling -> umbrella spelling, for rendering.
_LEGACY_TO_UMBRELLA = {
    legacy: "%s %s" % (fam, act)
    for fam, actions in _FAMILY_UMBRELLAS.items()
    for act, legacy in actions.items()
}


def _umbrella_legacy_names():
    """All legacy command names the umbrellas replace (hidden, still parse)."""
    names = set()
    for actions in _FAMILY_UMBRELLAS.values():
        names.update(actions.values())
    return names


def umbrella_display(cmd):
    """Legacy command name -> umbrella spelling for display.

    "tx-begin" -> "tx begin"; "trace-chain" -> "trace forward"; visible
    commands (not hidden behind a family) are returned unchanged.
    """
    return _LEGACY_TO_UMBRELLA.get(cmd, cmd)


def umbrella_argv(cmd):
    """Legacy command name -> umbrella argv tokens for execution.

    The builder's argv rewrite turns these tokens back into the legacy
    command before argparse, so execution is identical.
    """
    return umbrella_display(cmd).split()


def _apply_family_umbrella(argv):
    """Rewrite `<family> <action> ...` to the legacy `<command> ...` form.

    Runs before argparse so every legacy subparser keeps its exact flag
    surface — no flag-union parsers to maintain. Global flags may precede
    the subcommand: --log-level/--log-file take a value, --log-json and
    --version do not.
    """
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i]
        if tok in ("--log-level", "--log-file"):
            i += 2
        elif tok in ("--log-json", "--version"):
            i += 1
        else:
            break
    if 0 <= i < n - 1 and argv[i] in _FAMILY_UMBRELLAS:
        legacy = _FAMILY_UMBRELLAS[argv[i]].get(argv[i + 1])
        if legacy is not None:
            return argv[:i] + [legacy] + argv[i + 2:]
    return argv


def _make_umbrella_handler(family, actions):
    """Bare/invalid umbrella invocation -> action table on stderr, exit 2."""

    def _handler(args):
        rest = getattr(args, "umbrella_action", None) or []
        print(f"usage: {family} <action> [flags]", file=sys.stderr)
        print(file=sys.stderr)
        print(f"{family} umbrella commands "
              f"(legacy '{family}-<action>' spellings still work):",
              file=sys.stderr)
        for act, legacy in sorted(actions.items()):
            print(f"  {family} {act:<18} -> {legacy}", file=sys.stderr)
        if rest:
            print(f"\nerror: unknown {family} action: {rest[0]!r}",
                  file=sys.stderr)
        else:
            print(f"\nerror: missing {family} action", file=sys.stderr)
        print(f"Run '{family} <action> --help' for flags.", file=sys.stderr)
        sys.exit(2)

    return _handler
