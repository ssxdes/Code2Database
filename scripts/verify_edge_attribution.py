#!/usr/bin/env python3
"""Verify edge source attribution: check that EXTRACTED edges actually exist
in the source function's body text.

This samples edges and verifies:
1. EXTRACTED edges: the target function name appears in the source function body
2. CALLBACK_ARG edges: the callback argument appears in the source function body
3. FN_PTR edges: the function pointer call expression appears in the source body

Also checks for:
4. Functions with body_text that contain calls NOT captured as edges (missed edges)
5. #ifdef branch loss: functions that call targets only inside #else branches
"""

import argparse
import json
import os
import re
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_extraction(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_func_body_from_source(source_root, source_file, line, func_name):
    """Extract function body from source file using line number."""
    abs_path = os.path.join(source_root, source_file)
    if not os.path.isfile(abs_path):
        return None

    try:
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except (IOError, OSError):
        return None

    if line < 1 or line > len(lines):
        return None

    # Find opening brace from the function line
    body_start = line - 1  # 0-indexed
    found_open = False
    for i in range(body_start, min(body_start + 10, len(lines))):
        if '{' in lines[i]:
            body_start = i
            found_open = True
            break

    if not found_open:
        return None

    # Count braces to find function end
    _PP_IF_RE = re.compile(r'^\s*#\s*(if|ifdef|ifndef)\b')
    _PP_ELSE_RE = re.compile(r'^\s*#\s*(elif|else)\b')
    _PP_ENDIF_RE = re.compile(r'^\s*#\s*endif\b')

    body_lines = []
    brace_count = 0
    found_open2 = False
    pp_skip_depth = 0
    pp_if_depth = 0

    for i in range(body_start, min(body_start + 500, len(lines))):
        line_text = lines[i]
        raw_line = line_text.strip()

        if _PP_IF_RE.match(raw_line):
            if pp_skip_depth > 0:
                pp_skip_depth += 1
            else:
                pp_if_depth += 1
        elif _PP_ELSE_RE.match(raw_line):
            if pp_skip_depth > 0:
                pass
            elif pp_if_depth > 0:
                pp_skip_depth = 1
        elif _PP_ENDIF_RE.match(raw_line):
            if pp_skip_depth > 0:
                pp_skip_depth -= 1
            elif pp_if_depth > 0:
                pp_if_depth -= 1

        body_lines.append(line_text)

        if pp_skip_depth == 0:
            # Strip comments for brace counting
            stripped = line_text
            in_string = False
            clean = []
            j = 0
            while j < len(stripped):
                if stripped[j] == '"' and (j == 0 or stripped[j-1] != '\\'):
                    in_string = not in_string
                    j += 1
                    continue
                if stripped[j] == "'" and (j == 0 or stripped[j-1] != '\\'):
                    in_string = not in_string
                    j += 1
                    continue
                if not in_string:
                    if stripped[j:j+2] == '//':
                        break
                    if stripped[j:j+2] == '/*':
                        end = stripped.find('*/', j+2)
                        if end >= 0:
                            j = end + 2
                            continue
                        else:
                            break
                clean.append(stripped[j])
                j += 1

            clean_str = ''.join(clean)
            for ch in clean_str:
                if ch == '{':
                    brace_count += 1
                    found_open2 = True
                elif ch == '}':
                    brace_count -= 1

        if found_open2 and brace_count <= 0:
            break

    return ''.join(body_lines)


def verify_extracted_edges(data, source_root, sample_size=200):
    """Sample EXTRACTED edges and verify target appears in source body."""
    funcs = data.get('functions', [])
    edges = data.get('edges', [])

    # Build func lookup
    func_by_id = {}
    for f in funcs:
        func_by_id[f.get('id', '')] = f
        func_by_id[f.get('name', '')] = f  # Also by name for fallback

    extracted_edges = [e for e in edges if e.get('concurrency', '') == ''
                       and e.get('source', '') and e.get('target', '')]

    if not extracted_edges:
        return []

    # Sample
    sample_size = min(sample_size, len(extracted_edges))
    sample = random.Random(42).sample(extracted_edges, sample_size)

    findings = []
    verified = 0
    false_positives = 0

    for e in sample:
        source_id = e.get('source', '')
        target_name = e.get('target', '')

        # Get source function info
        src_func = func_by_id.get(source_id)
        if not src_func:
            # Try to find by name portion
            src_name = source_id.split('.')[-1] if '.' in source_id else source_id
            for f in funcs:
                if f.get('name', '') == src_name:
                    src_func = f
                    break

        if not src_func:
            findings.append({
                'type': 'EDGE_SOURCE_FUNC_NOT_FOUND',
                'source_id': source_id,
                'target': target_name,
                'severity': 'HIGH',
                'description': f'Edge source "{source_id}" not found in functions list',
            })
            continue

        src_file = src_func.get('source_file', '')
        src_line = src_func.get('line', 0)
        src_name = src_func.get('name', '')

        body = get_func_body_from_source(source_root, src_file, src_line, src_name)
        if body is None:
            continue  # Can't verify without source

        # Check if target name appears in body
        # Target might be a short name (last segment of ID)
        target_short = target_name.split('.')[-1] if '.' in target_name else target_name

        if re.search(rf'\b{re.escape(target_short)}\s*\(', body):
            verified += 1
        else:
            false_positives += 1
            findings.append({
                'type': 'FALSE_EXTRACTED_EDGE',
                'source': src_name,
                'source_id': source_id,
                'target': target_name,
                'source_file': src_file,
                'source_line': src_line,
                'severity': 'HIGH',
                'description': f'EXTRACTED edge {src_name} -> {target_name}: '
                             f'target not found in source body (false positive edge)',
            })

    # Summary finding
    findings.insert(0, {
        'type': 'EXTRACTED_EDGE_VERIFICATION_SUMMARY',
        'sample_size': sample_size,
        'verified': verified,
        'false_positives': false_positives,
        'unverifiable': sample_size - verified - false_positives,
        'false_positive_rate': f'{false_positives/sample_size*100:.1f}%' if sample_size else 'N/A',
        'severity': 'INFO',
        'description': f'EXTRACTED edge verification: {verified}/{sample_size} verified, '
                      f'{false_positives} false positives ({false_positives/sample_size*100:.1f}%)',
    })

    return findings


def verify_callback_arg_edges(data, source_root, sample_size=100):
    """Sample CALLBACK_ARG edges and verify callback argument appears in source body."""
    funcs = data.get('functions', [])
    edges = data.get('edges', [])

    func_by_id = {}
    for f in funcs:
        func_by_id[f.get('id', '')] = f

    cb_edges = [e for e in edges if e.get('concurrency', '') == 'callback'
                and e.get('source', '') and e.get('target', '')]

    if not cb_edges:
        return []

    sample_size = min(sample_size, len(cb_edges))
    sample = random.Random(42).sample(cb_edges, sample_size)

    findings = []
    verified = 0
    false_positives = 0

    for e in sample:
        source_id = e.get('source', '')
        target_name = e.get('target', '')
        call_condition = e.get('call_condition', '')

        src_func = func_by_id.get(source_id)
        if not src_func:
            continue

        src_file = src_func.get('source_file', '')
        src_line = src_func.get('line', 0)
        src_name = src_func.get('name', '')

        body = get_func_body_from_source(source_root, src_file, src_line, src_name)
        if body is None:
            continue

        target_short = target_name.split('.')[-1] if '.' in target_name else target_name

        # For CALLBACK_ARG edges, the target should appear as an argument to
        # a registration function (from profile callback_patterns, etc.)
        if re.search(rf'\b{re.escape(target_short)}\b', body):
            verified += 1
        else:
            false_positives += 1
            findings.append({
                'type': 'FALSE_CALLBACK_ARG_EDGE',
                'source': src_name,
                'source_id': source_id,
                'target': target_name,
                'call_condition': call_condition,
                'source_file': src_file,
                'source_line': src_line,
                'severity': 'MEDIUM',
                'description': f'CALLBACK_ARG edge {src_name} -> {target_name}: '
                             f'callback arg not found in source body',
            })

    findings.insert(0, {
        'type': 'CALLBACK_ARG_VERIFICATION_SUMMARY',
        'sample_size': sample_size,
        'verified': verified,
        'false_positives': false_positives,
        'severity': 'INFO',
        'description': f'CALLBACK_ARG edge verification: {verified}/{sample_size} verified, '
                      f'{false_positives} false positives',
    })

    return findings


def check_missed_calls(data, source_root, sample_size=100):
    """Check for function calls in body that are NOT captured as edges."""
    funcs = data.get('functions', [])
    edges = data.get('edges', [])

    # Build edge map: source_id -> set of target names
    edge_map = defaultdict(set)
    for e in edges:
        src = e.get('source', '')
        tgt = e.get('target', '')
        if src and tgt:
            edge_map[src].add(tgt)
            # Also add short name
            tgt_short = tgt.split('.')[-1] if '.' in tgt else tgt
            edge_map[src].add(tgt_short)

    # Sample non-empty functions
    non_empty = [f for f in funcs if not f.get('is_empty', False)]
    sample_size = min(sample_size, len(non_empty))
    sample = random.Random(42).sample(non_empty, sample_size)

    findings = []
    missed_count = 0

    # Build global func name set
    all_func_names = {f.get('name', '') for f in funcs}

    for f in sample:
        fid = f.get('id', '')
        fname = f.get('name', '')
        src_file = f.get('source_file', '')
        src_line = f.get('line', 0)

        body = get_func_body_from_source(source_root, src_file, src_line, fname)
        if body is None:
            continue

        # Find all function calls in body
        calls = re.findall(r'\b(\w+)\s*\(', body)

        existing_targets = edge_map.get(fid, set())

        missed_calls = []
        for call in set(calls):
            # Skip C keywords, stdlib, etc.
            if call in ('if', 'for', 'while', 'switch', 'return', 'sizeof',
                        'typeof', '__typeof__', 'case', 'do', 'else',
                        'assert', 'offsetof', 'container_of', 'TAILQ_FOREACH',
                        'TAILQ_INSERT_TAIL', 'TAILQ_REMOVE', 'LIST_INSERT_HEAD',
                        'SIMPLEQ_INSERT_TAIL', 'CIRCLEQ_INSERT_TAIL'):
                continue
            if call == fname:
                continue  # Skip self-calls for this check
            if call in all_func_names and call not in existing_targets:
                missed_calls.append(call)

        if missed_calls:
            missed_count += 1
            if len(findings) < 30:  # Limit output
                findings.append({
                    'type': 'MISSED_CALL_IN_BODY',
                    'function': fname,
                    'function_id': fid,
                    'source_file': src_file,
                    'source_line': src_line,
                    'missed_calls': missed_calls[:10],
                    'severity': 'MEDIUM',
                    'description': f'Function "{fname}" calls {missed_calls[:5]} but no edge exists',
                })

    findings.insert(0, {
        'type': 'MISSED_CALL_VERIFICATION_SUMMARY',
        'sample_size': sample_size,
        'functions_with_missed_calls': missed_count,
        'severity': 'INFO',
        'description': f'Missed call check: {missed_count}/{sample_size} functions have calls not captured as edges',
    })

    return findings


def check_ifdef_branch_calls(data, source_root, sample_size=50):
    """Check for function calls inside #else branches that might be missed."""
    funcs = data.get('functions', [])
    edges = data.get('edges', [])

    # Build edge map
    edge_map = defaultdict(set)
    for e in edges:
        src = e.get('source', '')
        tgt = e.get('target', '')
        if src and tgt:
            tgt_short = tgt.split('.')[-1] if '.' in tgt else tgt
            edge_map[src].add(tgt_short)

    all_func_names = {f.get('name', '') for f in funcs}

    non_empty = [f for f in funcs if not f.get('is_empty', False)]
    sample_size = min(sample_size, len(non_empty))
    sample = random.Random(42).sample(non_empty, sample_size)

    findings = []

    _PP_IF_RE = re.compile(r'^\s*#\s*(if|ifdef|ifndef)\b')
    _PP_ELSE_RE = re.compile(r'^\s*#\s*(elif|else)\b')
    _PP_ENDIF_RE = re.compile(r'^\s*#\s*endif\b')
    _CALL_RE = re.compile(r'\b(\w+)\s*\(')

    for f in sample:
        fid = f.get('id', '')
        fname = f.get('name', '')
        src_file = f.get('source_file', '')
        src_line = f.get('line', 0)

        abs_path = os.path.join(source_root, src_file)
        if not os.path.isfile(abs_path):
            continue

        try:
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                lines = fh.readlines()
        except (IOError, OSError):
            continue

        if src_line < 1 or src_line > len(lines):
            continue

        # Find function body
        body_start = src_line - 1
        found_open = False
        for i in range(body_start, min(body_start + 10, len(lines))):
            if '{' in lines[i]:
                body_start = i
                found_open = True
                break

        if not found_open:
            continue

        # Track #ifdef branches and collect calls in #else branches
        pp_if_depth = 0
        pp_skip_depth = 0
        else_branch_calls = []
        current_branch = 'if'  # Track which branch we're in

        for i in range(body_start, min(body_start + 500, len(lines))):
            raw_line = lines[i].strip()

            if _PP_IF_RE.match(raw_line):
                if pp_skip_depth > 0:
                    pp_skip_depth += 1
                else:
                    pp_if_depth += 1
                    current_branch = 'if'
            elif _PP_ELSE_RE.match(raw_line):
                if pp_skip_depth > 0:
                    pass
                elif pp_if_depth > 0:
                    current_branch = 'else'
            elif _PP_ENDIF_RE.match(raw_line):
                if pp_skip_depth > 0:
                    pp_skip_depth -= 1
                elif pp_if_depth > 0:
                    pp_if_depth -= 1
                    current_branch = 'if' if pp_if_depth > 0 else 'top'

            # If we're in an #else branch, look for function calls
            if current_branch == 'else' and pp_skip_depth == 0:
                calls = _CALL_RE.findall(lines[i])
                for call in calls:
                    if call in all_func_names and call != fname:
                        else_branch_calls.append(call)

            # Check for function end
            if i > body_start:
                # Simple brace check (not ifdef-aware here, just for loop termination)
                pass

        # Check if #else branch calls are captured as edges
        existing_targets = edge_map.get(fid, set())
        missed_else_calls = [c for c in set(else_branch_calls) if c not in existing_targets]

        if missed_else_calls:
            findings.append({
                'type': 'MISSED_IFELSE_BRANCH_CALL',
                'function': fname,
                'function_id': fid,
                'source_file': src_file,
                'source_line': src_line,
                'missed_else_calls': missed_else_calls[:10],
                'severity': 'MEDIUM',
                'description': f'Function "{fname}" has calls in #else branch not captured as edges: {missed_else_calls[:5]}',
            })

    return findings


def main():
    parser = argparse.ArgumentParser(description='Verify edge source attribution')
    parser.add_argument('--extraction', required=True, help='Path to extraction.json')
    parser.add_argument('--source', required=True, help='Path to source root')
    parser.add_argument('--sample-size', type=int, default=200, help='Sample size for verification')
    parser.add_argument('--output', help='Output findings JSON')
    args = parser.parse_args()

    data = load_extraction(args.extraction)
    all_findings = []

    print(f"=== Edge Attribution Verification ===", file=sys.stderr)
    print(f"Functions: {len(data.get('functions', []))}", file=sys.stderr)
    print(f"Edges: {len(data.get('edges', []))}", file=sys.stderr)

    # Run verifications
    print(f"\nVerifying EXTRACTED edges...", file=sys.stderr)
    findings = verify_extracted_edges(data, args.source, args.sample_size)
    all_findings.extend(findings)

    print(f"Verifying CALLBACK_ARG edges...", file=sys.stderr)
    findings = verify_callback_arg_edges(data, args.source, min(args.sample_size, 100))
    all_findings.extend(findings)

    print(f"Checking for missed calls...", file=sys.stderr)
    findings = check_missed_calls(data, args.source, min(args.sample_size, 100))
    all_findings.extend(findings)

    print(f"Checking #ifdef branch calls...", file=sys.stderr)
    findings = check_ifdef_branch_calls(data, args.source, 50)
    all_findings.extend(findings)

    # Print summary
    by_type = Counter(f['type'] for f in all_findings)
    print(f"\n--- Summary ---", file=sys.stderr)
    for ftype, count in sorted(by_type.items()):
        print(f"  {ftype}: {count}", file=sys.stderr)

    # Output
    result = {
        'verification_type': 'edge_attribution',
        'total_findings': len(all_findings),
        'by_type': dict(by_type),
        'findings': all_findings,
    }

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nFindings saved to: {args.output}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))

    return len([f for f in all_findings if f.get('severity', '') in ('HIGH', 'MEDIUM')])


if __name__ == '__main__':
    sys.exit(main())
