"""Coverage report generation and path-not-found hints.

Generates subsystem-level and file-level coverage reports during build,
and provides actionable hints when path queries fail (e.g., "function
looks like a net/ function — consider scanning net/").
"""
import json
import os
import re
import sys
from typing import Dict, List, Optional, Any

_KERNEL_SUBSYSTEMS = [
    'mm', 'kernel', 'lib', 'block', 'net', 'drivers', 'fs', 'security',
    'crypto', 'ipc', 'sound', 'usr', 'init', 'virt', 'scripts', 'tools',
    'arch', 'include', 'Documentation',
]

_NAME_TO_SUBSYSTEM_HINTS = [
    (re.compile(r'^(alloc|free|kmalloc|kfree|page_|folio_|slab|slob|slub|vmalloc|vfree|memcg|compaction|migrate| numa|hugetlb|cma|balloon|oom|page_alloc|__alloc|__free)'), 'mm'),
    (re.compile(r'^(blk_|bio_|bdev_|gendisk|request_queue|submit_bio|endio|blkdev_|block_|__blk|__bio)'), 'block'),
    (re.compile(r'^(kernel_|_kernel|do_|sys_|ksys_|__do_|start_kernel|rest_init|cpu_|smp_|init_|setup_)'), 'kernel'),
    (re.compile(r'^(lib_|strtol|kstrtoul|list_|hlist_|rbtree|radix_tree|idr|flex_|string|checksum|crc|bitop|find_)'), 'lib'),
    (re.compile(r'^(skb_|sock_|net_|dev_|ethtool|napi|ip_|tcp_|udp_|icmp|arp_|netlink|route_|fib_|neigh_|inet_)'), 'net'),
    (re.compile(r'^(ext4_|ext3_|xfs_|btrfs_|f2fs_|nfs_|fat_|vfat_|isofs_|jffs|ubifs|gfs2|ocfs2|nilfs|open_|close_|read_|write_|fs_|inode_|dentry_|file_|super_|mount_|namei_|stat_|fallocate|fadvise|readahead|truncate|setattr|getattr)'), 'fs'),
    (re.compile(r'^(drv_|dev_|platform_|pci_|usb_|i2c_|spi_|mmc_|dma_|irq_|request_irq|free_irq|enable_irq|disable_irq|probe_|remove_|suspend_|resume_|driver_)'), 'drivers'),
    (re.compile(r'^(security_|selinux_|smack_|apparmor|tomoyo|cap_|key_|cred_|audit|lsa|keyctl)'), 'security'),
    (re.compile(r'^(crypto_|crypto|sha|md5|aes|des|rsa|ecdh|crc32|crc16|ghash|chacha|poly1305|hkdf)'), 'crypto'),
    (re.compile(r'^(ipc_|msg_|sem_|shm_|mq_|pipe_|fifo_)'), 'ipc'),
]


def _subsystem_from_path(path: str) -> str:
    norm = path.replace('\\', '/')
    parts = norm.split('/')
    if len(parts) >= 1 and parts[0]:
        return parts[0]
    return ''


def write_coverage_report(graph_dir: str) -> Optional[str]:
    db_path = os.path.join(graph_dir, 'code2database.db')
    if not os.path.exists(db_path):
        return None
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT path, language, line_count, byte_count FROM cgdb_files"
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        if not rows:
            return None
        subsystems = {}
        for row in rows:
            sub = _subsystem_from_path(row['path'])
            if sub:
                entry = subsystems.setdefault(sub, {
                    'file_count': 0, 'line_count': 0,
                    'languages': set(),
                })
                entry['file_count'] += 1
                entry['line_count'] += row['line_count'] or 0
                if row['language']:
                    entry['languages'].add(row['language'])
        scanned = sorted(subsystems.keys())
        missing = [s for s in _KERNEL_SUBSYSTEMS if s not in subsystems]
        for s in sorted(subsystems.keys()):
            if s not in _KERNEL_SUBSYSTEMS:
                missing.append(s)
        report = {
            'type': 'code2database_coverage_report',
            'generated_by': 'graph_build',
            'graph_dir': graph_dir,
            'scanned_subsystems': scanned,
            'missing_common_subsystems': missing,
            'total_files': len(rows),
            'subsystem_details': {
                s: {'file_count': v['file_count'],
                    'line_count': v['line_count'],
                    'languages': sorted(v['languages'])}
                for s, v in sorted(subsystems.items())
            },
        }
        out_path = os.path.join(graph_dir, '.code2database_coverage_report.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return out_path
    except Exception as exc:
        return None
    finally:
        conn.close()


def write_file_coverage(graph_dir: str) -> Optional[str]:
    db_path = os.path.join(graph_dir, 'code2database.db')
    if not os.path.exists(db_path):
        return None
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT path, language, line_count, byte_count, commit_hash "
                "FROM cgdb_files ORDER BY path"
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        if not rows:
            return None
        files = []
        langs = {}
        subs = {}
        total_lines = 0
        total_bytes = 0
        for row in rows:
            sub = _subsystem_from_path(row['path'])
            files.append({
                'path': row['path'], 'language': row['language'] or '',
                'line_count': row['line_count'] or 0,
                'byte_count': row['byte_count'] or 0,
                'commit_hash': row['commit_hash'] or '',
            })
            lang = row['language'] or 'unknown'
            langs[lang] = langs.get(lang, 0) + 1
            if sub:
                subs[sub] = subs.get(sub, 0) + 1
            total_lines += row['line_count'] or 0
            total_bytes += row['byte_count'] or 0
        report = {
            'type': 'code2database_file_coverage',
            'generated_by': 'graph_build',
            'graph_dir': graph_dir,
            'total_files': len(rows),
            'total_lines': total_lines,
            'total_bytes': total_bytes,
            'languages': langs,
            'subsystems': subs,
            'files': files,
        }
        out_path = os.path.join(graph_dir, '.code2database_file_coverage.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return out_path
    except Exception:
        return None
    finally:
        conn.close()


def _guess_subsystem_for_name(name: str) -> List[str]:
    hints = []
    for pat, sub in _NAME_TO_SUBSYSTEM_HINTS:
        if pat.match(name):
            if sub not in hints:
                hints.append(sub)
    return hints


def path_not_found_hints(graph_dir: str, missing_src: Optional[str] = None,
                         missing_dst: Optional[str] = None) -> Dict[str, Any]:
    coverage_path = os.path.join(graph_dir, '.code2database_coverage_report.json')
    scanned = []
    coverage_report_path = None
    if os.path.exists(coverage_path):
        try:
            with open(coverage_path, 'r', encoding='utf-8') as f:
                cov = json.load(f)
            scanned = cov.get('scanned_subsystems', [])
            coverage_report_path = coverage_path
        except Exception:
            pass
    if not scanned:
        db_path = os.path.join(graph_dir, 'code2database.db')
        if os.path.exists(db_path):
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                try:
                    rows = conn.execute("SELECT DISTINCT path FROM cgdb_files").fetchall()
                    for row in rows:
                        sub = _subsystem_from_path(row[0])
                        if sub and sub not in scanned:
                            scanned.append(sub)
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
    suggestions = []
    for name, missing in [('source', missing_src), ('target', missing_dst)]:
        if not missing:
            continue
        hints = _guess_subsystem_for_name(missing)
        for hint in hints:
            if hint not in scanned:
                suggestions.append(
                    f"{name} '{missing}' looks like a {hint}/ function — "
                    f"consider scanning {hint}/ (currently not in graph)")
    missing_common = [s for s in _KERNEL_SUBSYSTEMS if s not in scanned]
    if not suggestions and not missing_common:
        suggestion = "no specific subsystem hints available"
    else:
        parts = []
        if suggestions:
            parts.append('; '.join(suggestions))
        if missing_common:
            parts.append(f"Missing common subsystems: {', '.join(missing_common[:5])}")
        suggestion = ' | '.join(parts) if parts else "no hints"
    return {
        'suggestion': suggestion,
        'scanned_subsystems': scanned,
        'missing_common_subsystems': missing_common,
        'coverage_report_path': coverage_report_path,
    }


def query_coverage(graph_dir: str, function_name: str = '',
                   file_path: str = '') -> Dict[str, Any]:
    db_path = os.path.join(graph_dir, 'code2database.db')
    if not os.path.exists(db_path):
        return {'status': 'error', 'error': f'Database not found: {db_path}'}
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if function_name:
            rows = conn.execute(
                "SELECT n.id, n.name, n.fqn, f.path AS file_path, n.line "
                "FROM cgdb_nodes n LEFT JOIN cgdb_files f ON n.file_id = f.id "
                "WHERE n.name = ? AND n.kind IN ('function','method','constructor','destructor') "
                "LIMIT 50", (function_name,)).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT n.id, n.name, n.fqn, f.path AS file_path, n.line "
                    "FROM cgdb_nodes n LEFT JOIN cgdb_files f ON n.file_id = f.id "
                    "WHERE n.fqn LIKE ? AND n.kind IN ('function','method') "
                    "LIMIT 50", (f'%{function_name}',)).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT n.id, n.name, n.fqn, f.path AS file_path, n.line "
                    "FROM cgdb_nodes n LEFT JOIN cgdb_files f ON n.file_id = f.id "
                    "WHERE n.name LIKE ? AND n.kind IN ('function','method') "
                    "LIMIT 50", (f'%{function_name}%',)).fetchall()
            if not rows:
                hints = _guess_subsystem_for_name(function_name)
                return {
                    'status': 'not_found', 'function': function_name,
                    'match_count': 0, 'subsystem_hints': hints,
                    'suggestion': f"'{function_name}' may be in {', '.join(hints)}/ — not scanned" if hints else '',
                }
            return {
                'status': 'found', 'function': function_name,
                'match_count': len(rows),
                'matches': [{'id': r['id'], 'name': r['name'], 'fqn': r['fqn'],
                              'file_path': r['file_path'], 'line': r['line']}
                             for r in rows],
            }
        elif file_path:
            rows = conn.execute(
                "SELECT id, path, language, line_count FROM cgdb_files "
                "WHERE path = ? LIMIT 1", (file_path,)).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT id, path, language, line_count FROM cgdb_files "
                    "WHERE path LIKE ? LIMIT 1", (f'%/{file_path}',)).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT id, path, language, line_count FROM cgdb_files "
                    "WHERE path LIKE ? LIMIT 1", (f'%{file_path}%',)).fetchall()
            if not rows:
                return {'status': 'not_scanned', 'file': file_path, 'scanned': False}
            file_row = rows[0]
            funcs = conn.execute(
                "SELECT id, name, fqn, line FROM cgdb_nodes "
                "WHERE file_id = ? AND kind IN ('function','method','constructor','destructor') "
                "ORDER BY line LIMIT 200", (file_row['id'],)).fetchall()
            return {
                'status': 'found', 'file': file_path, 'scanned': True,
                'file_id': file_row['id'], 'language': file_row['language'],
                'line_count': file_row['line_count'],
                'function_count': len(funcs),
                'functions': [{'id': r['id'], 'name': r['name'], 'fqn': r['fqn'], 'line': r['line']}
                               for r in funcs],
            }
        else:
            scanned = []
            try:
                file_rows = conn.execute("SELECT DISTINCT path FROM cgdb_files").fetchall()
                for fr in file_rows:
                    sub = _subsystem_from_path(fr['path'])
                    if sub and sub not in scanned:
                        scanned.append(sub)
            except sqlite3.OperationalError:
                pass
            missing = [s for s in _KERNEL_SUBSYSTEMS if s not in scanned]
            return {
                'status': 'summary', 'scanned_subsystems': scanned,
                'missing_common_subsystems': missing,
                'total_files': len(file_rows) if 'file_rows' in dir() else 0,
                'coverage_report_path': os.path.join(graph_dir, '.code2database_coverage_report.json')
                    if os.path.exists(os.path.join(graph_dir, '.code2database_coverage_report.json')) else None,
                'file_coverage_path': os.path.join(graph_dir, '.code2database_file_coverage.json')
                    if os.path.exists(os.path.join(graph_dir, '.code2database_file_coverage.json')) else None,
            }
    except Exception as exc:
        return {'status': 'error', 'error': str(exc)}
    finally:
        conn.close()
