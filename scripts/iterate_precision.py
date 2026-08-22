#!/usr/bin/env python3
"""Iterative callgraph precision analysis and improvement.

This script systematically analyzes callgraph output, identifies issues,
categorizes them, and produces a report of findings that need fixing.

Usage:
  python3 iterate_precision.py --extraction PATH --source PATH [--iteration N]
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load_extraction(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_master(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def analyze_unresolved_callees(data, source_root, external_prefixes=None):
    """Find callee names that don't match any extracted function definition."""
    funcs = data.get('functions', [])
    edges = data.get('edges', [])
    func_names = {f['name'] for f in funcs}
    # Also build func_id set for target resolution check
    func_ids = {f['id'] for f in funcs}

    if external_prefixes is None:
        external_prefixes = []

    unresolved = defaultdict(list)  # callee_name -> [(source, source_file, line)]
    for e in edges:
        target = e.get('target', '')
        if not target:
            continue
        # Skip if target matches an external lib prefix (expected unresolved)
        if any(target.startswith(p) for p in external_prefixes):
            continue
        # Check if target is a resolved func ID (contains '.') or a known name
        if target in func_ids or target in func_names:
            continue
        # Skip function pointer calls — these are struct field/variable calls
        # that can't be resolved statically (not a scanner bug)
        if e.get('concurrency', '') in ('callback', 'fn_ptr'):
            continue
        source = e.get('source', '')
        # Find the source function's file
        src_file = ''
        for f in funcs:
            if f.get('id', '') == source or f.get('name', '') == source.split('.')[-1]:
                src_file = f.get('source_file', '')
                break
        unresolved[target].append((source, src_file))

    findings = []
    # Cache all C/H/CPP source files in a single walk, then search
    # cached contents per callee — avoids O(K×N) repeated walks where
    # K = number of unresolved callees, N = number of source files.
    _source_files: dict = {}  # path → content text
    for dirpath, dirnames, filenames in os.walk(source_root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for fname in filenames:
            if not fname.endswith(('.c', '.h', '.cpp')):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    _source_files[fpath] = f.read()
            except (IOError, OSError):
                continue

    for callee, callers in sorted(unresolved.items()):
        found_in_source = False
        found_type = None
        for fpath, content in _source_files.items():

            # Check if it's a function declaration
            if re.search(rf'\b{re.escape(callee)}\s*\(', content):
                found_in_source = True
                # Determine type — check definition vs declaration
                # A definition has the name followed by params and then '{'
                # A declaration ends with ';' after the param list
                # Check for declaration first (more specific pattern)
                if re.search(rf'(?:^|\n)\s*(?:static\s+)?\w+[\s*]+\s*{re.escape(callee)}\s*\([^)]*\)\s*;', content):
                    found_type = 'function_declaration'
                elif re.search(rf'(?:^|\n)\s*(?:static\s+)?\w+[\s*]+\s*{re.escape(callee)}\s*\([^)]*\)\s*\{{', content):
                    found_type = 'function_definition'
                elif re.search(rf'\b{re.escape(callee)}\s*\([^)]*\)\s*\{{', content):
                    found_type = 'function_definition'
                elif re.search(rf'#define\s+{re.escape(callee)}', content):
                    found_type = 'macro_definition'
                else:
                    # Has the name with parens but unclear if def or decl
                    # Check if there's a '{' on the same or next line after the callee name
                    lines = content.split('\n')
                    has_def = False
                    for li, line in enumerate(lines):
                        if re.search(rf'\b{re.escape(callee)}\s*\(', line):
                            # Look ahead for opening brace
                            for j in range(li, min(li + 5, len(lines))):
                                if '{' in lines[j]:
                                    has_def = True
                                    break
                            break
                    if has_def:
                        found_type = 'function_definition'
                    else:
                        found_type = 'reference_only'
                break
        if found_in_source:
            break

        if not found_in_source:
            findings.append({
                'type': 'UNRESOLVED_CALLEE_NOT_IN_SOURCE',
                'callee': callee,
                'caller_count': len(callers),
                'sample_callers': callers[:3],
                'severity': 'HIGH',
                'description': f'Callee "{callee}" not found in source code — possible false edge or scanner gap',
            })
        elif found_type == 'function_definition':
            findings.append({
                'type': 'UNRESOLVED_CALLEE_EXISTS_BUT_NOT_EXTRACTED',
                'callee': callee,
                'caller_count': len(callers),
                'sample_callers': callers[:3],
                'severity': 'MEDIUM',
                'description': f'Callee "{callee}" has definition in source but was not extracted — scanner missed it',
            })
        elif found_type == 'macro_definition':
            findings.append({
                'type': 'UNRESOLVED_CALLEE_IS_MACRO',
                'callee': callee,
                'caller_count': len(callers),
                'sample_callers': callers[:3],
                'severity': 'LOW',
                'description': f'Callee "{callee}" is a macro definition — not a real function call',
            })
        elif found_type == 'function_declaration':
            findings.append({
                'type': 'UNRESOLVED_CALLEE_HEADER_ONLY',
                'callee': callee,
                'caller_count': len(callers),
                'sample_callers': callers[:3],
                'severity': 'LOW',
                'description': f'Callee "{callee}" only has declaration (header) — external library function',
            })

    return findings


def analyze_self_edges(data):
    """Find self-calling edges that may be false positives."""
    funcs = data.get('functions', [])
    edges = data.get('edges', [])

    findings = []
    for e in edges:
        source = e.get('source', '')
        target = e.get('target', '')
        source_name = source.split('.')[-1] if '.' in source else source
        if source_name == target and source_name:
            findings.append({
                'type': 'SELF_EDGE',
                'function': source_name,
                'source_id': source,
                'condition': e.get('call_condition', ''),
                'severity': 'LOW',
                'description': f'Function "{source_name}" calls itself — verify this is genuine recursion',
            })

    return findings


def analyze_empty_functions(data, source_root):
    """Find functions with empty body_text that may indicate extraction failures."""
    funcs = data.get('functions', [])
    findings = []

    for f in funcs:
        if f.get('is_empty', False) and not f.get('labels', []):
            name = f.get('name', '')
            src_file = f.get('source_file', '')
            line = f.get('line', 0)

            # Check if the source file actually has a body
            abs_path = os.path.join(source_root, src_file)
            if os.path.isfile(abs_path):
                try:
                    with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                        lines = fh.readlines()
                    if 0 < line <= len(lines):
                        # Look for function body on/after this line
                        body_found = False
                        for i in range(line - 1, min(line + 5, len(lines))):
                            if '{' in lines[i]:
                                body_found = True
                                break
                        if body_found:
                            findings.append({
                                'type': 'EMPTY_BODY_BUT_HAS_SOURCE',
                                'function': name,
                                'source_file': src_file,
                                'line': line,
                                'severity': 'MEDIUM',
                                'description': f'Function "{name}" has empty body_text but source file has a body — extraction missed it',
                            })
                except (IOError, OSError):
                    pass

    return findings[:50]  # Limit to first 50


def analyze_fn_ptr_calls(data, source_root):
    """Analyze function pointer calls for resolution quality."""
    fn_ptr_calls = data.get('fn_ptr_calls', {})
    findings = []

    if isinstance(fn_ptr_calls, dict):
        items = list(fn_ptr_calls.items())
    elif isinstance(fn_ptr_calls, list):
        items = [(i, v) for i, v in enumerate(fn_ptr_calls)]
    else:
        return findings

    for caller, targets in items[:30]:
        if not targets:
            findings.append({
                'type': 'FN_PTR_UNRESOLVED',
                'caller': caller,
                'call_expr': '',
                'severity': 'MEDIUM',
                'description': f'Function pointer call in "{caller}" has no resolved targets',
            })
        elif isinstance(targets, list):
            # Check for targets with empty field_name
            empty_targets = [t for t in targets if isinstance(t, dict) and not t.get('field_name', '')]
            if empty_targets:
                findings.append({
                    'type': 'FN_PTR_PARTIAL_RESOLVE',
                    'caller': caller,
                    'total_targets': len(targets),
                    'empty_targets': len(empty_targets),
                    'severity': 'LOW',
                    'description': f'Function pointer call in "{caller}" has {len(empty_targets)}/{len(targets)} targets without field_name',
                })

    return findings


def analyze_edge_confidence(data, source_root):
    """Check for edges that may be false positives based on patterns."""
    edges = data.get('edges', [])
    funcs = data.get('functions', [])
    findings = []

    # Build source file map
    func_src_map = {}
    for f in funcs:
        func_src_map[f.get('name', '')] = f.get('source_file', '')

    # Check for edges where caller and callee are in unrelated directories
    # (possible false positive from name collision)
    name_to_funcs = defaultdict(list)
    for f in funcs:
        name_to_funcs[f.get('name', '')].append(f)

    # Functions with common names that appear in multiple files
    ambiguous_funcs = {name: flist for name, flist in name_to_funcs.items() if len(flist) > 1}

    for e in edges:
        # Skip FN_PTR and CALLBACK edges — their targets are field/parameter
        # names that intentionally don't resolve to a single function definition
        if e.get('concurrency', '') in ('fn_ptr', 'callback'):
            continue
        target = e.get('target', '')
        if target in ambiguous_funcs:
            # Check if the edge resolved to the wrong file
            source_id = e.get('source', '')
            # Get source function's domain/file
            src_file = ''
            for f in funcs:
                if f.get('id', '') == source_id:
                    src_file = f.get('source_file', '')
                    break

            if src_file:
                target_files = [f.get('source_file', '') for f in ambiguous_funcs[target]]
                # Skip if the ambiguity is only .h vs .c (declaration vs definition)
                c_files = [tf for tf in target_files if tf.endswith('.c') or tf.endswith('.cpp')]
                h_files = [tf for tf in target_files if tf.endswith('.h') or tf.endswith('.hpp')]
                if len(c_files) == 1 and len(h_files) >= 1:
                    # Only one definition + declarations — not truly ambiguous
                    continue
                # Skip if all instances are in the same directory
                target_dirs = set(os.path.dirname(tf) for tf in target_files)
                if len(target_dirs) == 1:
                    continue

                # Genuine ambiguity: multiple definitions in different directories
                findings.append({
                    'type': 'AMBIGUOUS_CALLEE_RESOLUTION',
                    'callee': target,
                    'source_id': source_id,
                    'source_file': src_file,
                    'possible_files': target_files,
                    'severity': 'HIGH',
                    'description': f'Ambiguous callee "{target}" resolved without disambiguation — '
                                  f'caller in {src_file}, callee could be in {target_files}',
                })

    return findings[:30]


def analyze_vtable_registrations(data, source_root):
    """Check vtable registrations for quality."""
    vtables = data.get('vtable_registrations', [])
    findings = []

    for vt in vtables[:50]:
        struct_type = vt.get('struct_type', '')
        var_name = vt.get('var_name', '')
        fields = vt.get('registrations', [])  # Scanner uses 'registrations'
        register_file = vt.get('source_file', '')
        ops_id = f"{struct_type}.{var_name}" if struct_type and var_name else var_name or struct_type or "(unknown)"

        # Check for empty fields
        if not fields:
            findings.append({
                'type': 'VTABLE_NO_FIELDS',
                'ops_name': ops_id,
                'source_file': register_file,
                'severity': 'MEDIUM',
                'description': f'Vtable "{ops_id}" has no fields extracted',
            })

        # Check for fields that are NULL or empty
        null_fields = [f for f in fields if f.get('func_name', '') in ('NULL', '', 'null')]
        if null_fields:
            findings.append({
                'type': 'VTABLE_NULL_FIELDS',
                'ops_name': ops_id,
                'null_count': len(null_fields),
                'total_fields': len(fields),
                'severity': 'LOW',
                'description': f'Vtable "{ops_id}" has {len(null_fields)}/{len(fields)} NULL fields',
            })

        # Check for fields where func_name equals field name AND the func_name
        # doesn't appear as a function definition in the same file
        unresolved = []
        for f in fields:
            fn = f.get('func_name', '')
            if fn and fn == f.get('field', '') and fn not in ('NULL', '', 'null'):
                # Check if this func_name exists as a definition in the same file
                # If it does, it's a legitimate short-name function
                abs_path = os.path.join(source_root, register_file)
                if os.path.isfile(abs_path):
                    try:
                        with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                            file_content = fh.read()
                        # Check if func_name has a definition in this file
                        has_def = bool(re.search(
                            rf'(?:^|\n)\s*(?:static\s+)?\w+[\s*]+\s*{re.escape(fn)}\s*\(',
                            file_content))
                        if not has_def:
                            unresolved.append(f)
                    except (IOError, OSError):
                        unresolved.append(f)
                else:
                    unresolved.append(f)

    return findings


def analyze_import_edges(data, source_root):
    """Check import edges for correctness."""
    import_edges = data.get('import_edges', [])
    funcs = data.get('functions', [])
    findings = []

    func_names = {f['name'] for f in funcs}

    # Check for import edges where the imported function doesn't exist
    missing_imports = set()
    for ie in import_edges:
        imported_fn = ie.get('imported_function', '')
        if imported_fn and imported_fn not in func_names:
            missing_imports.add(imported_fn)

    if missing_imports:
        findings.append({
            'type': 'IMPORT_EDGE_MISSING_FUNCTION',
            'missing_functions': sorted(list(missing_imports))[:20],
            'count': len(missing_imports),
            'severity': 'MEDIUM',
            'description': f'{len(missing_imports)} import edges reference functions not in extraction',
        })

    return findings


def analyze_duplicate_edges(data):
    """Find duplicate edges (same source->target with same condition)."""
    edges = data.get('edges', [])
    findings = []

    edge_counter = Counter()
    for e in edges:
        key = (e.get('source', ''), e.get('target', ''), e.get('call_condition', ''))
        edge_counter[key] += 1

    dupes = [(k, v) for k, v in edge_counter.items() if v > 1]
    if dupes:
        findings.append({
            'type': 'DUPLICATE_EDGES',
            'count': len(dupes),
            'total_duplicate_instances': sum(v - 1 for _, v in dupes),
            'samples': [{'source': k[0], 'target': k[1], 'condition': k[2], 'count': v}
                       for k, v in sorted(dupes, key=lambda x: -x[1])[:10]],
            'severity': 'MEDIUM',
            'description': f'{len(dupes)} duplicate edge groups found ({sum(v-1 for _,v in dupes)} extra edges)',
        })

    return findings


def analyze_callee_edge_consistency(data):
    """Verify that functions listed as callees have edges pointing to them."""
    funcs = data.get('functions', [])
    edges = data.get('edges', [])

    # Functions with no incoming edges but also no outgoing edges — isolated
    func_ids = {f.get('id', '') for f in funcs}
    func_names = {f.get('name', '') for f in funcs}

    targets = set()
    sources = set()
    for e in edges:
        targets.add(e.get('target', ''))
        sources.add(e.get('source', '').split('.')[-1] if '.' in e.get('source', '') else e.get('source', ''))

    # Functions that are never called and never call anything
    isolated = []
    for f in funcs:
        name = f.get('name', '')
        fid = f.get('id', '')
        is_target = name in targets or any(t == name for t in targets)
        is_source = fid in sources or any(s == name for s in sources)
        if not is_target and not is_source and not f.get('is_empty', True):
            isolated.append(name)

    if len(isolated) > 10:
        findings = [{
            'type': 'ISOLATED_FUNCTIONS',
            'count': len(isolated),
            'samples': isolated[:20],
            'severity': 'LOW',
            'description': f'{len(isolated)} non-empty functions have no incoming or outgoing edges — possible extraction gap',
        }]
    else:
        findings = []

    return findings


def main():
    parser = argparse.ArgumentParser(description='Iterative callgraph precision analysis')
    parser.add_argument('--extraction', required=True, help='Path to extraction.json')
    parser.add_argument('--source', required=True, help='Path to source root')
    parser.add_argument('--iteration', type=int, default=1, help='Iteration number')
    parser.add_argument('--output', help='Output findings JSON')
    parser.add_argument('--profile', help='Path to project profile JSON for external prefix info')
    args = parser.parse_args()

    data = load_extraction(args.extraction)
    all_findings = []

    # Load external prefixes from profile if provided
    external_prefixes = []
    if args.profile:
        try:
            profile_data = load_master(args.profile)
            ext_lib = profile_data.get('skip_names', {}).get('external_lib_prefixes', {})
            external_prefixes = list(ext_lib.keys())
            print(f"Loaded {len(external_prefixes)} external prefixes from profile", file=sys.stderr)
        except Exception as e:
            print(f"Warning: could not load profile: {e}", file=sys.stderr)

    print(f"=== Iteration {args.iteration} ===", file=sys.stderr)
    print(f"Functions: {len(data.get('functions', []))}", file=sys.stderr)
    print(f"Edges: {len(data.get('edges', []))}", file=sys.stderr)
    print(f"Import edges: {len(data.get('import_edges', []))}", file=sys.stderr)
    print(f"Vtable registrations: {len(data.get('vtable_registrations', []))}", file=sys.stderr)
    print(f"Fn ptr calls: {len(data.get('fn_ptr_calls', []))}", file=sys.stderr)

    # Run all analyses
    print(f"\nRunning analyses...", file=sys.stderr)

    analyses = [
        ("Unresolved callees", lambda: analyze_unresolved_callees(data, args.source, external_prefixes)),
        ("Self edges", lambda: analyze_self_edges(data)),
        ("Empty functions", lambda: analyze_empty_functions(data, args.source)),
        ("Fn ptr calls", lambda: analyze_fn_ptr_calls(data, args.source)),
        ("Edge confidence", lambda: analyze_edge_confidence(data, args.source)),
        ("Vtable registrations", lambda: analyze_vtable_registrations(data, args.source)),
        ("Import edges", lambda: analyze_import_edges(data, args.source)),
        ("Duplicate edges", lambda: analyze_duplicate_edges(data)),
        ("Callee-edge consistency", lambda: analyze_callee_edge_consistency(data)),
    ]

    for name, analyzer in analyses:
        print(f"  {name}...", file=sys.stderr, end='')
        try:
            findings = analyzer()
            all_findings.extend(findings)
            print(f" {len(findings)} findings", file=sys.stderr)
        except Exception as e:
            print(f" ERROR: {e}", file=sys.stderr)

    # Summary
    by_type = Counter(f['type'] for f in all_findings)
    by_severity = Counter(f['severity'] for f in all_findings)

    print(f"\n--- Summary ---", file=sys.stderr)
    print(f"Total findings: {len(all_findings)}", file=sys.stderr)
    for sev, count in sorted(by_severity.items()):
        print(f"  {sev}: {count}", file=sys.stderr)
    for ftype, count in sorted(by_type.items()):
        print(f"  {ftype}: {count}", file=sys.stderr)

    # Output
    result = {
        'iteration': args.iteration,
        'total_findings': len(all_findings),
        'by_severity': dict(by_severity),
        'by_type': dict(by_type),
        'findings': all_findings,
    }

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nFindings saved to: {args.output}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))

    return len(all_findings)


if __name__ == '__main__':
    sys.exit(main())
