# Runtime Configuration Guide

**File**: `config/runtime.json`

This file provides default runtime parameters for the code graph pipeline. All values can be manually edited. Changes take effect on the next pipeline run.

> **Tuning the database for your environment.** `runtime.json` is the knob you turn when the defaults don't fit: scan parallelism for a constrained CI box, memory-consolidation thresholds for a long-running agent session, stale-detection sensitivity for a fast-moving repo. The graph itself is project data; this file is *operational* data — how the pipeline runs, not what it extracts.

---

## When to Edit

Edit this file when you need to tune pipeline behavior without modifying source code — for example, adjusting parallelism for a constrained machine, changing memory management thresholds, or customizing query defaults.

---

## Field Reference

### `scan` — Scan Phase Parameters

| Field | Type | Default | CLI Override | Description |
|-------|------|---------|--------------|-------------|
| `workers` | int | `0` | `--workers` | Number of parallel scan workers. `0` = auto (min of CPU count and 8). `1` = sequential (no parallelism). Increase for faster scanning on multi-core machines; decrease if memory is tight. |
| `parallel_mode` | string | `"thread"` | `--parallel-mode` | Parallelism model for multi-worker scans: `"thread"` (ThreadPoolExecutor — GIL-limited but zero pickling overhead; tree-sitter releases the GIL during parse so C/C++ scans see real speedup) or `"process"` (ProcessPoolExecutor with fork COW — each child gets its own interpreter and tree-sitter parser, bypassing the GIL for Python-heavy post-processing). Use `"process"` for CPU-bound workloads where post-processing dominates (>1000 files with substantial per-file work). Children inherit memory via copy-on-write, so memory cost is bounded (~500MB/child for the Python + module baseline; results do not accumulate in children). Falls back to threads when fork is unavailable (non-Linux). |
| `max_file_size_kb` | int | `1024` | — | Maximum file size (in KB) to scan. Files larger than this are skipped. Increase for projects with large generated files that you want included; decrease to skip bulky auto-generated code. |
| `skip_dirs` | string[] | `[".git", "__pycache__", "node_modules", "build", ".cache"]` | — | Directory names to skip during scanning (matched by directory name, not path). These are **merged** with the scanner's built-in skip set (`__pycache__`, `node_modules`, `.git`, `build`, `dist`, `out`, `bin`, `obj`, `venv`, `.venv`, `.tox`, `.cache`, `third_party`, `vendor`, etc.). Add project-specific directories you want excluded (e.g., `["generated", "vendor"]`). |

### `build` — Build Phase Parameters

| Field | Type | Default | CLI Override | Description |
|-------|------|---------|--------------|-------------|
| `default_config` | string | `"auto"` | `--build-config` | Build configuration for `#ifdef` macro resolution. `"auto"` = auto-detect from build system. Can also be a path to a compile_commands.json or a build type name like `"Release"`, `"Debug"`. |
| `max_domain_files` | int | `50` | `--max-domain-files` | Maximum number of JSON files per subdirectory when writing domain-split output. `0` = flat (all in one file). Increase for very large projects to keep individual files smaller; decrease to reduce file count. |

### `query` — Query Phase Parameters

| Field | Type | Default | CLI Override | Description |
|-------|------|---------|--------------|-------------|
| `default_detail` | string | `"brief"` | `--detail` | Default detail level for query responses. One of: `"brief"` (one-line summaries), `"standard"` (moderate detail), `"full"` (complete information). |
| `default_max_tokens` | int | `500` | `--max-tokens` | Default maximum output tokens for query responses. `0` = unlimited. Lower values produce shorter responses; higher values give more detail. |
| `explore_max_nodes` | int | `15` | — | Maximum number of nodes to return in `explore` queries. Controls the breadth of neighborhood exploration around a target node. |
| `explore_max_tokens` | int | `2000` | — | Maximum output tokens for `explore` query responses. |

### `memory` — Memory System Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `decay_factor` | float | `0.95` | Weight decay multiplier applied during memory consolidation. Controls how quickly unused memory entries lose weight over time. Range 0.0–1.0. Higher values = slower decay (memories persist longer); lower values = faster decay (stale memories are pruned sooner). The actual decay uses exponential decay with `DECAY_LAMBDA = -ln(decay_factor)` per day. |
| `consolidate_threshold` | int | `100` | Minimum number of memory entries before automatic consolidation runs. Consolidation performs weight decay, archives low-weight entries, and rebuilds indexes. Set higher to reduce consolidation frequency; lower to keep memory tighter. |
| `scratch_ttl_hours` | float | `24.0` | Time-to-live (in hours) for scratch (temporary) memory entries. Scratch entries auto-expire after this duration. Increase for longer-lived temporary context; decrease to clean up faster. |

### `semantic` — Semantic Analysis Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `stale_ratio_threshold` | float | `0.15` | Stale node ratio threshold for recommending a semantic update. When the fraction of stale (outdated) nodes exceeds this value, the system recommends running a full semantic update. Lower = more sensitive (recommends updates sooner); higher = more tolerant. |
| `stale_api_threshold` | int | `1` | Minimum number of stale API entry points that triggers a semantic update recommendation. If any API entry becomes stale, a refresh is recommended. Set to `0` to never trigger on stale APIs alone; increase to tolerate more stale APIs. |

### `invariants` — Invariant Extraction Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `state_machine_threshold` | int | `1` | Minimum assignment count to a single state variable before treating it as a state machine. Increase to `2` to reduce false positives on projects with many counter variables. |
| `extract_preconditions` | bool | `true` | Extract `if (!cond) return` patterns near function entry as preconditions. |
| `extract_postconditions` | bool | `true` | Extract `return cond` / `assert(cond)` near exit as postconditions. |
| `extract_loop_invariants` | bool | `true` | Extract loop invariants from `for`/`while` bodies. |
| `reject_ambiguous` | bool | `true` | Never apply AMBIGUOUS-confidence invariants. INFERRED require user confirmation; EXTRACTED auto-applied. |

### `auto_enhance` — LLM Auto-Enhancement Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `auto_apply_extracted` | bool | `true` | Auto-apply EXTRACTED+evidence enhancements without prompting. |
| `require_confirm_inferred` | bool | `true` | Require user confirmation for INFERRED enhancements. |
| `reject_ambiguous` | bool | `true` | Reject AMBIGUOUS enhancements outright. |
| `rollback_window` | int | `100` | Number of applied enhancements retained in the rollback log. Older entries beyond this window are pruned. |
| `batch_confirm_size` | int | `20` | Number of pending INFERRED enhancements per `batch-confirm` call. |

### `transactions` — Transactional Update Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `wal_enabled` | bool | `true` | Write-ahead log enabled. Set to `false` for non-critical workspaces (faster but no crash recovery). |
| `snapshot_keep_count` | int | `10` | Number of named snapshots to retain. Older snapshots are pruned. |
| `lock_timeout_seconds` | int | `30` | fcntl lock acquisition timeout. Increase for slow disks or busy CI. |
| `auto_replay_on_start` | bool | `true` | Automatically replay unfinished WAL on next process start. |

### `ffi` — Cross-Language FFI Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `detect_python_ctypes` | bool | `true` | Detect Python `CDLL`/`WinDLL`/`cffi`/`pybind11` bindings. |
| `detect_go_cgo` | bool | `true` | Detect Go `import "C"` cgo bindings. |
| `detect_rust_extern` | bool | `true` | Detect Rust `extern "C"` / `#[no_mangle]` bindings. |
| `flag_lossy_conversions` | bool | `true` | Mark type marshalling edges as `lossy: true` when conversion may lose data (e.g., int64 → int32). |

### `web_ui` — Web UI Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8765` | HTTP port for the interactive Web UI. |
| `host` | string | `"127.0.0.1"` | Bind host. Use `"0.0.0.0"` for shared access (not recommended). |
| `open_browser` | bool | `false` | Auto-open the browser on start. |
| `max_nodes_render` | int | `5000` | Maximum nodes rendered in the SVG; larger graphs are sampled. |

### `benchmark` — BUG Benchmark Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `recall_target` | float | `0.95` | Target recall for GraphInvestigator. Below this, the benchmark report flags the test as "graph-insufficient". |
| `max_tool_calls` | int | `30` | Hard cap on tool calls per investigator. |
| `max_tokens` | int | `20000` | Token budget per investigator. |

### `profile_health` — Profile Health Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `min_score` | int | `70` | Minimum acceptable profile health score. Below this, `profile-health` flags the profile as stale. |
| `auto_apply_extracted` | bool | `true` | `profile-evolve --apply` auto-applies EXTRACTED-confidence suggestions. |
| `require_confirm_inferred` | bool | `true` | INFERRED suggestions require user confirmation. |
| `bind_to_head` | bool | `true` | Bind profile to git/svn HEAD on `profile-bind-version`. Stale profiles (HEAD mismatch) are flagged. |

### `doc_code` — Doc-Code Alignment Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `check_on_describe` | bool | `true` | `describe-node` surfaces `doc_code_mismatches` by default. |
| `check_on_knowledge_validate` | bool | `true` | `knowledge-validate` runs doc-code alignment check. |
| `signature_diff_strict` | bool | `false` | Strict signature diffing (parameter order matters). |

### `daemon` — Background Daemon Parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Auto-start the daemon after build. Recommended: keep `false` and start explicitly with `daemon-start`. |
| `watch_paths` | string[] | `[]` | Source paths to watch. Empty = use the scan `--source` from the most recent scan. |
| `exclude_patterns` | string[] | `["*.swp", "*.tmp", ".git/*"]` | Glob patterns to exclude from watching. |
| `debounce_ms` | int | `500` | Debounce window for editor saves. Lower = more responsive but more re-scans. |
| `batch_window_ms` | int | `1000` | Batch window for coalescing multiple events. |
| `auto_rebuild_outputs` | bool | `true` | Touch `CODE2DATABASE_SUMMARY.md`, context packs, indices after each sync. |
| `idle_sleep_minutes` | int | `30` | Idle sleep before daemon enters low-power polling. |
| `max_events_per_minute` | int | `1000` | Circuit breaker threshold; above this, bulk rebuild is triggered. |
| `backend` | string | `"auto"` | `"inotify"` (Linux), `"polling"`, or `"auto"` (inotify if available, else polling). |

---

## Relationship to CLI Arguments

Most `runtime.json` fields provide defaults that can be overridden by command-line arguments:

| runtime.json path | CLI argument | Notes |
|---|---|---|
| `scan.workers` | `code2database scan --workers N` | CLI takes precedence |
| `build.default_config` | `code2database build --build-config VALUE` | CLI takes precedence |
| `build.max_domain_files` | `code2database build --max-domain-files N` | CLI takes precedence |
| `query.default_detail` | `code2database describe --detail LEVEL` | CLI takes precedence |
| `query.default_max_tokens` | `code2database describe --max-tokens N` | CLI takes precedence |
| `scan.max_file_size_kb` | — | No CLI override, runtime.json only |
| `scan.skip_dirs` | — | No CLI override, runtime.json only |
| `memory.*` | — | No CLI override, runtime.json only |
| `semantic.*` | — | No CLI override, runtime.json only |

---

## Common Customizations

### For memory-constrained machines (< 16GB RAM)
```json
{
  "scan": {
    "workers": 1,
    "max_file_size_kb": 512
  },
  "memory": {
    "consolidate_threshold": 50,
    "scratch_ttl_hours": 4
  }
}
```

### For high-performance machines (64+ cores, 64GB+ RAM)
```json
{
  "scan": {
    "workers": 16,
    "max_file_size_kb": 4096
  },
  "build": {
    "max_domain_files": 100
  }
}
```

### For projects with many auto-generated directories
```json
{
  "scan": {
    "skip_dirs": [".git", "__pycache__", "node_modules", "build", ".cache",
                  "generated", "autogen", "protobuf"]
  }
}
```

### For more aggressive stale detection
```json
{
  "semantic": {
    "stale_ratio_threshold": 0.08,
    "stale_api_threshold": 0
  }
}
```
