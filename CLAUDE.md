# CLAUDE.md

This file provides guidance to Claude Code when working with Code2Database.

> **Boundary**: Skill usage instructions live in `SKILL.md` (`docs/en/SKILL.md` in the source repo). Do not load `OVERVIEW.md` or `scripts/` into context unless debugging the tool itself. Developer protocol (scope, testing, MemoryGuard quirk, manifest pins): `AGENTS.md`.

## What This Is

Code2Database scans C/C++/Go/Python/Java/Rust/ASM codebases into a queryable directed invocation graph — conditional paths, concurrency analysis, data flow, FFI tracing, commit provenance, and a dual knowledge/memory store. The one-shot lifecycle is the `c2d` umbrella: `c2d setup --source DIR` (ingest) → `c2d session` (context) → `c2d ask --question "..."` (read-only recipe) → `c2d capture` (save memory), plus `c2d freshen` (freshness routing) and `c2d report` (artifacts). `c2d recipes` lists the question→command routing table. The full surface — 260 CLI commands (252 builder + 8 scanner) and 83 MCP tools (36 `code2database_*` + 19 `cgdb_*` + 28 design-report) — stays available for direct use.

## Where to Look

| Concern | Location |
|---------|----------|
| Usage, Quick Start, usage constraints | `SKILL.md` / `docs/en/SKILL.md` |
| Command catalog (intent index → pipeline walkthrough → all 260 commands) | `docs/en/references/usage_reference.md` |
| Worked examples | `docs/en/references/usage_examples.md` |
| Analysis / ops sub-skills | `docs/en/SKILL_analysis.md`, `docs/en/SKILL_ops.md` |
| Runtime tuning, profile authoring | `docs/en/RUNTIME_CONFIG.md`, `docs/en/PROFILE_MANUAL.md` |
| Developer protocol, key directories, testing | `AGENTS.md` |

CLI: `python3 scripts/code2database_builder.py <command>` · scanner: `python3 scripts/code2database_scanner.py <command>` · MCP: `serve` (stdio, or `--transport http` for remote with auth/TLS).

## Non-negotiables

- Run `session-init` (or `c2d session`) once per AI session before other C2D commands
- Global-to-local: context packs → describe/trace; never bulk-read output files
- Only 7 labels; every edge carries EXTRACTED/INFERRED/AMBIGUOUS confidence
- DB writes require user confirmation; wrap multi-step writes in `tx-begin`/`tx-commit`
- Never pre-load `scripts/config/profiles/` or `docs/*/references/` into context
