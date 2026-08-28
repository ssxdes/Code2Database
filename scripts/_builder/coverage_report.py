"""coverage_report — build-time coverage report + path-not-found hints.

Generates `<graph_dir>/.code2database_coverage_report.json` after build,
summarizing which subsystems (top-level source directories like fs/, mm/,
kernel/, lib/, block/, net/) were scanned and how many files each contains.

Also provides `path_not_found_hints()` — called from `cmd_cgdb_path` when
src or dst node is missing, to suggest which common kernel subsystems the
user might be missing from the graph.

Closes the KERNEL-D14 gap from skill_vs_kasan_comparison.md: when path-not-found,
the user gets actionable hints instead of a bare "not found" error.
"""
import json
import os
import re
import sqlite3
from collections import Counter
from typing import Dict, List, Optional
import logging


# Common Linux kernel subsystems — used to suggest "you scanned fs/ but
# might also need mm/ for this function name". Order roughly by how often
# they appear in cross-subsystem bug investigations.
_KERNEL_SUBSYSTEMS = [
    "mm", "kernel", "lib", "block", "net", "drivers", "fs", "security",
    "crypto", "ipc", "sound", "usr", "init", "virt", "scripts", "tools",
    "arch", "include", "Documentation",
]

# Function-name → likely subsystem heuristics. Used when path-not-found to
# suggest which subsystems might contain a definition for the missing name.
# Keys are matched as substrings (lowercased) against the missing function name.
_NAME_TO_SUBSYSTEM_HINTS = [
    # mm/ — memory management
    (r"^(alloc|free|kmalloc|kfree|vmalloc|vfree|kmalloc|kzalloc|kcalloc|"
     r"page_|__page_|folio_|memmap|pfn|gfp|slab|slob|sput|krealloc|"
     r"memblock|numa|zap|unmap|munmap|mmap|brk|mremap|truncate|writeback|"
     r"swap|swap_|readahead|filemap|folio|shrink|isolate|"
     r"pgd|pud|pmd|pte|tlb|set_pte|pte_|tlb_|__tlb|flush_tlb)", "mm"),
    # block/ — block layer
    (r"^(blk_|bio_|submit_|rq_|__rq_|request_queue|blkdev_|gendisk|"
     r"bio_alloc|bio_put|blk_queue|blk_mq|blkcg|blk_|elevator)", "block"),
    # kernel/ — core kernel
    (r"^(sched_|scheduler|task_|rcu_|spin_|raw_spin|mutex_|sema|"
     r"completion_|wait_|wake_up_|try_to_wake|sched_|__sched|"
     r"printk|pr_|panic|oops|bug|warn|kobject|kref|klist|"
     r"cpu_|cpumask|smp_|ipi_|irq_|hrtimer|timer_|work_|"
     r"notifier|atomic_|refcount|percpu)", "kernel"),
    # lib/ — helpers
    (r"^(str|memcmp|memcpy|memset|memmove|strlen|strncpy|strscpy|"
     r"list_|hlist|rbtree|radix_tree|xarray|bitmap|"
     r"sort|bsearch|find_|__find_|idr|crc|hash|rhashtable)", "lib"),
    # net/ — networking
    (r"^(net_|sock_|socket_|sk_|skb_|dev_|netdev_|eth_|ipv[46]|tcp_|udp_|"
     r"ip_|packet_|recv_|send_|sendmsg|recvmsg|netlink_|"
     r"nla_|nlmsg|nf_|xt_|conntrack|neigh_|route_|dst_|fib_|"
     r"inet_|sin_|saddr|daddr)", "net"),
    # fs/ — filesystems (already scanned typically, but if missing)
    (r"^(vfs_|inode_|dentry_|file_|super_|s_op|i_op|f_op|"
     r"path_|d_path|mount_|kern_mount|do_mount|"
     r"open_|close_|read_|write_|lseek|fsync|flock|"
     r"pagecache|address_space|a_ops|readpage|writepage|"
     r"buffer_|bh_|ll_rw_block|submit_bh|mark_buffer_|"
     r"ext4_|ext3_|ext2_|xfs_|btrfs_|f2fs_|nfs_|fat_|ntfs_)", "fs"),
    # drivers/ — device drivers
    (r"^(driver_|pci_|usb_|platform_|of_|devm_|clk_|regmap_|"
     r"i2c_|spi_|dma_|dmaengine|gpio_|irq_|interrupt_|"
     r"rtc_|hwmon_|input_|tty_|serial_|char_|misc_|"
     r"scsi_|ata_|sata_|nvme_|drm_|gpu_|fb_|lcd_|backlight)", "drivers"),
    # security/ — security subsystem
    (r"^(security_|selinux_|smack_|apparmor|cap_|"
     r"selinux|capable|ns_capable|has_capability)", "security"),
    # crypto/ — crypto
    (r"^(crypto_|cipher_|hash_|hmac_|aead_|skcipher|"
     r"sha[0-9]|md5|aes|rsa|ecdh|crc32|crc16)", "crypto"),
    # ipc/ — IPC
    (r"^(ipc_|shm_|sem_|msg_|mq_|mqueue|shmget|shmctl)", "ipc"),
]


def _subsystem_from_path(path: str) -> str:
    """Extract the top-level subsystem from a file path.

    'fs/ext4/inode.c' → 'fs'
    'mm/page_alloc.c' → 'mm'
    'include/linux/sched.h' → 'include'
    'foo/bar.c' → 'foo' (non-kernel paths get their top dir)
    """
    if not path:
        return "root"
    # Strip leading ./ or /
    p = path.lstrip("./")
    parts = p.split("/")
    if len(parts) == 1:
        return "root"
    return parts[0]


def write_coverage_report(graph_dir: str) -> Optional[str]:
    """Write `<graph_dir>/.code2database_coverage_report.json`.

    Reads `cgdb_files.path` from the SQLite DB (if present) and groups files
    by top-level subsystem. Returns the path written, or None if DB missing
    or no cgdb_files rows.
    """
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT path, language FROM cgdb_files"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    subsystem_files: Dict[str, List[str]] = {}
    subsystem_langs: Dict[str, Counter] = {}
    for path, lang in rows:
        sub = _subsystem_from_path(path)
        subsystem_files.setdefault(sub, []).append(path)
        subsystem_langs.setdefault(sub, Counter())[lang or "unknown"] += 1

    total_files = len(rows)
    report = {
        "type": "code2database_coverage_report",
        "generated_by": "",
        "graph_dir": graph_dir,
        "total_files": total_files,
        "subsystem_count": len(subsystem_files),
        "scanned_subsystems": sorted(subsystem_files.keys()),
        "subsystems": [
            {
                "name": sub,
                "file_count": len(subsystem_files[sub]),
                "languages": dict(subsystem_langs.get(sub, Counter())),
                "sample_paths": sorted(subsystem_files[sub])[:5],
            }
            for sub, _files in sorted(
                subsystem_files.items(),
                key=lambda kv: (-len(kv[1]), kv[0]),
            )
        ],
        "missing_common_subsystems": [
            s for s in _KERNEL_SUBSYSTEMS
            if s not in subsystem_files and s != "root"
        ],
    }
    out_path = os.path.join(graph_dir, ".code2database_coverage_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return out_path


def _guess_subsystem_for_name(name: str) -> List[str]:
    """Given a function name like 'folio_batch_add' or 'blk_mq_submit_bio',
    return a list of subsystems likely to contain its definition, ordered
    by match strength.
    """
    if not name:
        return []
    # Strip common prefixes (root_, domain_, legacy_) used in JSON graph IDs
    cleaned = name
    for prefix in ("root_", "domain_", "legacy_"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    cleaned_lower = cleaned.lower()
    matches = []
    for pattern, sub in _NAME_TO_SUBSYSTEM_HINTS:
        if re.match(pattern, cleaned_lower):
            matches.append(sub)
    # Deduplicate while preserving order
    seen = set()
    ordered = []
    for s in matches:
        if s not in seen:
            ordered.append(s)
            seen.add(s)
    return ordered


def path_not_found_hints(
    graph_dir: str,
    missing_src: Optional[str] = None,
    missing_dst: Optional[str] = None,
) -> Dict:
    """Generate actionable hints when `cgdb-path` cannot find src/dst.

    Returns a dict with:
      - `scanned_subsystems`: list of subsystems already in the graph
      - `missing_common_subsystems`: list of common kernel subsystems NOT in graph
      - `src_subsystem_hints`: subsystems likely to contain missing_src's definition
      - `dst_subsystem_hints`: subsystems likely to contain missing_dst's definition
      - `coverage_report_path`: path to .code2database_coverage_report.json (if exists)
      - `suggestion`: human-readable string with concrete next steps
    """
    cov_path = os.path.join(graph_dir, ".code2database_coverage_report.json")
    scanned = []
    missing_common = []
    if os.path.exists(cov_path):
        try:
            with open(cov_path, "r", encoding="utf-8") as f:
                cov = json.load(f)
            scanned = [s["name"] for s in cov.get("subsystems", [])]
            missing_common = cov.get("missing_common_subsystems", [])
        except (json.JSONDecodeError, OSError):
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
    else:
        # Fallback: query SQLite directly
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                rows = conn.execute("SELECT path FROM cgdb_files").fetchall()
                conn.close()
                scanned = sorted(set(
                    _subsystem_from_path(r[0]) for r in rows
                ))
                missing_common = [
                    s for s in _KERNEL_SUBSYSTEMS
                    if s not in scanned and s != "root"
                ]
            except sqlite3.Error:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
    src_hints = _guess_subsystem_for_name(missing_src) if missing_src else []
    dst_hints = _guess_subsystem_for_name(missing_dst) if missing_dst else []

    # Build the suggestion string
    parts = []
    if missing_src and src_hints:
        unscanned_src = [s for s in src_hints if s not in scanned]
        if unscanned_src:
            parts.append(
                f"source '{missing_src}' looks like a {unscanned_src[0]}/ "
                f"function — consider scanning {unscanned_src[0]}/ "
                f"(currently not in graph)"
            )
    if missing_dst and dst_hints:
        unscanned_dst = [s for s in dst_hints if s not in scanned]
        if unscanned_dst:
            parts.append(
                f"target '{missing_dst}' looks like a {unscanned_dst[0]}/ "
                f"function — consider scanning {unscanned_dst[0]}/ "
                f"(currently not in graph)"
            )
    if missing_common and not parts:
        parts.append(
            f"common kernel subsystems NOT in graph: "
            f"{', '.join(missing_common[:5])}. Consider re-scanning with "
            f"`--source /path/to/kernel` to include those subsystems."
        )
    if not parts:
        parts.append(
            "no specific subsystem hints — verify the function name is "
            "spelled correctly, or re-scan the full source tree."
        )

    return {
        "scanned_subsystems": scanned,
        "missing_common_subsystems": missing_common,
        "src_subsystem_hints": src_hints,
        "dst_subsystem_hints": dst_hints,
        "coverage_report_path": cov_path if os.path.exists(cov_path) else None,
        "suggestion": " | ".join(parts),
    }


def write_file_coverage(graph_dir: str) -> Optional[str]:
    """Write `<graph_dir>/.code2database_file_coverage.json` listing every
    scanned file path (relative to source root) and per-file stats.

    This complements `write_coverage_report()` (which groups by subsystem)
    by providing the full file-level list — useful for diagnosing "was
    file Y scanned?" without opening SQLite directly.

    Returns the path written, or None if DB missing or no cgdb_files rows.
    """
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT path, language, line_count, byte_count, "
            "commit_hash FROM cgdb_files ORDER BY path"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    files = []
    by_lang: Counter = Counter()
    by_subsystem: Counter = Counter()
    for path, lang, line_count, byte_count, commit_hash in rows:
        files.append({
            "path": path,
            "language": lang or "unknown",
            "line_count": line_count or 0,
            "byte_count": byte_count or 0,
            "commit_hash": commit_hash or "",
        })
        by_lang[lang or "unknown"] += 1
        by_subsystem[_subsystem_from_path(path)] += 1

    report = {
        "type": "code2database_file_coverage",
        "generated_by": "",
        "graph_dir": graph_dir,
        "total_files": len(files),
        "total_lines": sum(f["line_count"] for f in files),
        "total_bytes": sum(f["byte_count"] for f in files),
        "languages": dict(by_lang),
        "subsystems": dict(by_subsystem.most_common()),
        "files": files,
    }
    out_path = os.path.join(graph_dir, ".code2database_file_coverage.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return out_path


def query_coverage(
    graph_dir: str,
    function_name: Optional[str] = None,
    file_path: Optional[str] = None,
) -> Dict:
    """Answer two questions:

    - `--function X`: Is function X in the graph? Returns matching
      cgdb_nodes rows (name, fqn, file_path, line) — useful for
      diagnosing "why didn't analysis find this function?"
    - `--file Y`: Was file Y scanned? Returns the cgdb_files row if
      present, plus a list of all functions defined in that file.

    If neither argument is given, returns the subsystem-level summary
    (same as `path_not_found_hints()` minus the suggestion).
    """
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(db_path):
        return {
            "status": "error",
            "error": f"{db_path} not found",
        }
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        return {"status": "error", "error": f"cannot open DB: {exc}"}

    result: Dict = {"status": "ok", "graph_dir": graph_dir}

    try:
        if function_name:
            # Match by exact name, then by FQN suffix, then by name LIKE
            rows = conn.execute(
                "SELECT n.id, n.name, n.fqn, n.file_id, n.line, "
                "f.path AS file_path, n.kind, n.source_layer "
                "FROM cgdb_nodes n "
                "LEFT JOIN cgdb_files f ON n.file_id = f.id "
                "WHERE n.name = ? OR n.fqn LIKE ? OR n.name LIKE ? "
                "ORDER BY n.name LIMIT 100",
                (function_name, f"%{function_name}",
                 f"%{function_name}%")
            ).fetchall()
            matches = [
                {
                    "id": r[0], "name": r[1], "fqn": r[2], "file_id": r[3],
                    "line": r[4], "file_path": r[5], "kind": r[6],
                    "source_layer": r[7],
                }
                for r in rows
            ]
            result["match_count"] = len(matches)
            result["matches"] = matches
            result["function_query"] = {
                "query": function_name,
                "match_count": len(matches),
                "matches": matches,
            }
            if not matches:
                hints = _guess_subsystem_for_name(function_name)
                result["subsystem_hints"] = hints
                result["function_query"]["subsystem_hints"] = hints
                result["function_query"]["suggestion"] = (
                    f"function '{function_name}' not in graph; "
                    f"name looks like it could belong to: "
                    f"{', '.join(hints) if hints else '(no match)'}. "
                    f"Consider re-scanning that subsystem."
                )

        if file_path:
            rows = conn.execute(
                "SELECT id, path, language, line_count, byte_count, "
                "commit_hash FROM cgdb_files "
                "WHERE path = ? OR path LIKE ? OR path LIKE ? "
                "ORDER BY path LIMIT 10",
                (file_path, f"%/{file_path}", f"%{file_path}%")
            ).fetchall()
            scanned_files = [
                {
                    "id": r[0], "path": r[1], "language": r[2],
                    "line_count": r[3], "byte_count": r[4],
                    "commit_hash": r[5],
                }
                for r in rows
            ]
            result["scanned"] = len(scanned_files) > 0
            result["file_query"] = {
                "query": file_path,
                "scanned": len(scanned_files) > 0,
                "matches": scanned_files,
            }
            if scanned_files:
                file_id = scanned_files[0]["id"]
                fn_rows = conn.execute(
                    "SELECT name, fqn, line, kind FROM cgdb_nodes "
                    "WHERE file_id = ? AND kind IN "
                    "('function', 'method', 'constructor', 'destructor') "
                    "ORDER BY line LIMIT 200",
                    (file_id,)
                ).fetchall()
                result["function_count"] = len(fn_rows)
                result["file_query"]["functions_in_file"] = [
                    {"name": r[0], "fqn": r[1], "line": r[2], "kind": r[3]}
                    for r in fn_rows
                ]
                result["file_query"]["function_count"] = len(fn_rows)

        if not function_name and not file_path:
            file_rows = conn.execute(
                "SELECT path FROM cgdb_files"
            ).fetchall()
            scanned = sorted(set(
                _subsystem_from_path(r[0]) for r in file_rows
            ))
            missing_common = [
                s for s in _KERNEL_SUBSYSTEMS
                if s not in scanned and s != "root"
            ]
            result["scanned_subsystems"] = scanned
            result["summary"] = {
                "scanned_subsystems": scanned,
                "missing_common_subsystems": missing_common,
                "total_files": len(file_rows),
                "coverage_report_path": (
                    os.path.join(graph_dir,
                                ".code2database_coverage_report.json")
                    if os.path.exists(os.path.join(
                        graph_dir, ".code2database_coverage_report.json"))
                    else None
                ),
                "file_coverage_path": (
                    os.path.join(graph_dir,
                                ".code2database_file_coverage.json")
                    if os.path.exists(os.path.join(
                        graph_dir, ".code2database_file_coverage.json"))
                    else None
                ),
            }
    finally:
        conn.close()

    return result
