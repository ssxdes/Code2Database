"""Pre-compiled merged sync primitive patterns per language.

Builds a single alternation regex per language from the individual
sync primitive patterns, so _emit_sync_primitives can do a single
finditer pass per function body instead of P separate passes.

Each pattern is wrapped in a named group so m.lastgroup identifies
which pattern matched, and alt_meta maps group names to
(kind, style, var_group_idx, alt_name) tuples.
"""
import re

_SYNC_PATTERNS_BY_LANG = {
    'python': [
        (r'\bwith\s+([A-Za-z_][A-Za-z0-9_\.]*)\s*:', None,
         'lock_acquire', 'with_stmt'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.acquire\(\)', None,
         'lock_acquire', 'method_call'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.release\(\)', None,
         'lock_release', 'method_call'),
    ],
    'go': [
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.Lock\(\)', None,
         'lock_acquire', 'method_call'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.Unlock\(\)', None,
         'lock_release', 'method_call'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.RLock\(\)', None,
         'lock_acquire', 'method_call'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.RUnlock\(\)', None,
         'lock_release', 'method_call'),
        (r'<-\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_acquire', 'channel_recv'),
        (r'([A-Za-z_][A-Za-z0-9_]*)\s*<-', None,
         'lock_release', 'channel_send'),
    ],
    'rust': [
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.lock\(\)\.unwrap\(\)', None,
         'lock_acquire', 'method_call'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.read\(\)\.unwrap\(\)', None,
         'lock_acquire', 'method_call'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.write\(\)\.unwrap\(\)', None,
         'lock_acquire', 'method_call'),
    ],
    'java': [
        (r'\bsynchronized\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)', None,
         'lock_acquire', 'synchronized'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.lock\(\)', None,
         'lock_acquire', 'method_call'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.unlock\(\)', None,
         'lock_release', 'method_call'),
    ],
    'c': [
        (r'\bpthread_mutex_lock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_acquire', 'pthread_mutex'),
        (r'\bpthread_mutex_unlock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_release', 'pthread_mutex'),
        (r'\bpthread_rwlock_rdlock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_acquire', 'pthread_rwlock'),
        (r'\bpthread_rwlock_wrlock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_acquire', 'pthread_rwlock'),
        (r'\bpthread_rwlock_unlock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_release', 'pthread_rwlock'),
        (r'\bpthread_spin_lock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_acquire', 'pthread_spin'),
        (r'\bpthread_spin_unlock\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_release', 'pthread_spin'),
        (r'\bsem_wait\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_acquire', 'semaphore'),
        (r'\bsem_post\s*\(\s*&?\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_release', 'semaphore'),
    ],
    'cpp': [
        (r'\bstd::mutex\b', None, 'lock_var', 'std_mutex'),
        (r'\bstd::lock_guard\s*<[^>]*>\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_acquire', 'lock_guard'),
        (r'\bstd::unique_lock\s*<[^>]*>\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)', None,
         'lock_acquire', 'unique_lock'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.lock\(\)', None,
         'lock_acquire', 'method_call'),
        (r'\b([A-Za-z_][A-Za-z0-9_]*)\.unlock\(\)', None,
         'lock_release', 'method_call'),
    ],
}

_CACHE = {}


def get_sync_patterns(language: str):
    """Return (merged_re, alt_meta) for the given language.

    merged_re: a compiled regex with named groups for each pattern.
    alt_meta: list of (kind, style, var_group_idx, alt_name) tuples.
    """
    if language in _CACHE:
        return _CACHE[language]

    patterns = _SYNC_PATTERNS_BY_LANG.get(language)
    if not patterns:
        _CACHE[language] = (None, [])
        return (None, [])

    merged_parts = []
    alt_meta = []
    group_offset = 1

    for i, (acq_pat, _rel_pat, kind, style) in enumerate(patterns):
        tmp = re.compile(acq_pat)
        n_groups = tmp.groups
        alt_name = f'sync{i}'
        merged_parts.append(f'(?P<{alt_name}>{acq_pat})')
        var_grp_idx = group_offset + 1 if n_groups >= 1 else None
        alt_meta.append((kind, style, var_grp_idx, alt_name))
        group_offset += 1 + n_groups

    merged_re = re.compile('|'.join(merged_parts))
    _CACHE[language] = (merged_re, alt_meta)
    return (merged_re, alt_meta)
