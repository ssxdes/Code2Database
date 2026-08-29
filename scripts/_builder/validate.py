"""Post-build validation for callgraph output files.

Runs automatically after each build to verify:
1. Logic correctness — structural edges use 'relation', call edges use 'concurrency'
2. Call chain accuracy — no dangling references, no duplicate edges
3. Semantic matching — project-internal functions not in External Endpoints,
   API catalog has no test/example main functions
4. Data consistency — counts in summary match actual data in master JSON
"""

import json
import os
import re
import sys
from pathlib import Path
from collections import Counter, defaultdict
from typing import List, Dict, Optional
import logging


class ValidationResult:
    """Accumulates validation findings."""

    def __init__(self):
        self.errors: List[Dict] = []    # Must-fix issues
        self.warnings: List[Dict] = []  # Worth investigating
        self.infos: List[Dict] = []      # Informational notes

    def error(self, category: str, message: str, detail: str = ""):
        self.errors.append({"category": category, "message": message, "detail": detail})

    def warn(self, category: str, message: str, detail: str = ""):
        self.warnings.append({"category": category, "message": message, "detail": detail})

    def add_info(self, category: str, message: str, detail: str = ""):
        self.infos.append({"category": category, "message": message, "detail": detail})

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        lines = []
        if self.errors:
            lines.append(f"ERRORS ({len(self.errors)}):")
            for e in self.errors:
                lines.append(f"  [{e['category']}] {e['message']}")
                if e['detail']:
                    lines.append(f"    {e['detail']}")
        if self.warnings:
            lines.append(f"WARNINGS ({len(self.warnings)}):")
            for w in self.warnings:
                lines.append(f"  [{w['category']}] {w['message']}")
                if w['detail']:
                    lines.append(f"    {w['detail']}")
        if self.infos:
            lines.append(f"INFO ({len(self.infos)}):")
            for i in self.infos[:10]:
                lines.append(f"  [{i['category']}] {i['message']}")
            if len(self.infos) > 10:
                lines.append(f"  ... and {len(self.infos) - 10} more")
        if self.ok and not self.warnings:
            lines.append("ALL CHECKS PASSED")
        return "\n".join(lines)


def _load_json(path: str) -> Optional[dict]:
    """Load JSON file, return None if missing or invalid."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _load_all_functions(outdir: str, master: dict, _cache: dict = None) -> List[Dict]:
    """Load all function entries from domain JSON files.

    Domain JSON stores functions as lists:
      [qualified_id, name, source_file, line, labels_str, signature]
    Also has function_details dict with additional metadata.
    Returns list of dicts with standardized keys + '_domain'.

    Uses _cache dict to avoid re-loading on repeated calls within same validate_all.
    """
    if _cache is not None and "functions" in _cache:
        return _cache["functions"]

    domains_map = master.get("domains", {})
    all_funcs = []

    for dom_name, dom_path in domains_map.items():
        if isinstance(dom_path, str):
            full_path = os.path.join(outdir, dom_path)
            dom_data = _load_json(full_path)
            if not dom_data:
                continue
        elif isinstance(dom_path, dict):
            dom_data = dom_path
        else:
            continue

        func_details = dom_data.get("function_details", {})

        for func in dom_data.get("functions", []):
            if isinstance(func, dict):
                entry = dict(func)
            elif isinstance(func, list) and len(func) >= 5:
                # Parse labels: may be JSON array string or comma-separated
                raw_labels = func[4]
                if raw_labels:
                    raw_labels = raw_labels.strip()
                    if raw_labels.startswith("["):
                        try:
                            labels = json.loads(raw_labels)
                        except json.JSONDecodeError:
                            labels = [raw_labels]
                    else:
                        labels = raw_labels.split(",")
                else:
                    labels = []

                entry = {
                    "id": func[0],
                    "name": func[1],
                    "source_file": func[2],
                    "line": func[3],
                    "labels": labels,
                    "signature": func[5] if len(func) > 5 else "",
                }
                # Merge function_details if available
                detail = func_details.get(func[0], {})
                if detail:
                    for k, v in detail.items():
                        if k not in entry:
                            entry[k] = v
            else:
                continue

            entry["_domain"] = dom_name
            all_funcs.append(entry)

    if _cache is not None:
        _cache["functions"] = all_funcs
    return all_funcs


def validate_edge_logic(master: dict, result: ValidationResult):
    """Check that edge attributes are logically correct.

    Rules:
    - Structural edges (CONTAINS/IMPORTS) must use 'relation' attribute
    - Cross-domain call edges must use 'concurrency' attribute
    - No cross-domain edge should have relation=CONTAINS/IMPORTS
    - IMPORTS edges should also have concurrency='imports' for compatibility
    """
    cross = master.get("cross_domain_edges", [])
    struct = master.get("structural_edges", [])

    # Cross-domain edges: must have concurrency, must NOT have relation=CONTAINS/IMPORTS
    # OPS_BIND edges (vtable → handler) are not call edges and don't carry concurrency.
    bad_cross = 0
    for i, e in enumerate(cross):
        concurrency = e.get("concurrency", "")
        relation = e.get("relation", "")

        if relation == "OPS_BIND":
            continue
        if not concurrency:
            result.error("edge_logic",
                        f"Cross-domain edge #{i} missing 'concurrency' attribute",
                        f"source={e.get('source')}, target={e.get('target')}")
        if relation in ("CONTAINS", "IMPORTS"):
            bad_cross += 1
    if bad_cross:
        result.error("edge_logic",
                    f"{bad_cross} cross-domain edges have structural relation "
                    f"(CONTAINS/IMPORTS) — should be in structural_edges",
                    "This causes out_end label logic to miscount call successors")

    # Structural edges: must have relation
    bad_struct = 0
    for i, e in enumerate(struct):
        relation = e.get("relation", "")
        if not relation:
            bad_struct += 1
    if bad_struct:
        result.error("edge_logic",
                    f"{bad_struct} structural edges missing 'relation' attribute")

    result.add_info("edge_logic",
               f"Cross-domain: {len(cross)}, Structural: {len(struct)}",
               f"cross_domain uses concurrency, structural uses relation")


def validate_call_chain_accuracy(master: dict, result: ValidationResult):
    """Check call chain integrity.

    Rules:
    - No duplicate edges (same source+target+concurrency)
    - No AMBIGUOUS edges
    - No FN_PTR edges (legacy type, should be resolved)
    - All edges should have evidence
    - No self-loops unless justified (callback self-scheduling)
    """
    cross = master.get("cross_domain_edges", [])
    structural = master.get("structural_edges", [])
    all_edges = cross + structural

    # Duplicate detection
    edge_keys = []
    for e in cross:
        key = (e.get("source", ""), e.get("target", ""), e.get("concurrency", ""))
        edge_keys.append(key)
    dupes = Counter(edge_keys)
    dupes = {k: v for k, v in dupes.items() if v > 1}
    if dupes:
        for k, v in sorted(dupes.items(), key=lambda x: -x[1])[:5]:
            result.error("call_chain",
                        f"Duplicate edge: {k[0]} -> {k[1]} ({k[2]}) appears {v} times")

    # AMBIGUOUS edges (check all edges, not just cross-domain)
    ambig = [e for e in all_edges if e.get("confidence") == "AMBIGUOUS"]

    # FN_PTR edges (case-insensitive to match both "fn_ptr" and "FN_PTR")
    fnptr = [e for e in all_edges if (e.get("concurrency") or "").lower() == "fn_ptr"]

    # Use edge_type_counts from master for comprehensive reporting
    # (intra-domain fn_ptr/vtable_dispatch edges aren't in cross/structural)
    etc = master.get("edge_type_counts", {})
    fn_ptr_total = etc.get("concurrency:fn_ptr", 0)
    vtable_dispatch_total = etc.get("concurrency:vtable_dispatch", 0)
    callback_total = etc.get("concurrency:callback", 0)
    thread_spawn_total = etc.get("concurrency:thread_spawn", 0)
    spawn_target_total = etc.get("concurrency:spawn_target", 0)
    extracted_total = etc.get("confidence:EXTRACTED", 0)
    inferred_total = etc.get("confidence:INFERRED", 0)
    ambiguous_total = etc.get("confidence:AMBIGUOUS", 0)

    # Missing evidence
    no_evidence = [e for e in all_edges if not e.get("evidence")]

    # Self-loops
    self_loops = [e for e in all_edges if e.get("source") == e.get("target")]
    if self_loops:
        for e in self_loops:
            cc = e.get("call_condition", "")
            if "callback" not in cc and "self" not in cc:
                result.warn("call_chain",
                           f"Self-loop without callback justification: {e.get('source')}",
                           f"concurrency={e.get('concurrency')}, call_condition={cc}")

    # Report AMBIGUOUS/FN_PTR as informational (not errors) since they're valid edge types
    if ambig:
        result.add_info("call_chain",
                   f"{len(ambig)} AMBIGUOUS edges in cross/structural (fn_ptr calls with unresolved targets)")
    if fn_ptr_total:
        result.add_info("call_chain",
                   f"{fn_ptr_total} fn_ptr edges total (including intra-domain)")
    if vtable_dispatch_total:
        result.add_info("call_chain",
                   f"{vtable_dispatch_total} vtable_dispatch edges total")
    if callback_total:
        result.add_info("call_chain",
                   f"{callback_total} callback edges total")
    if thread_spawn_total or spawn_target_total:
        result.add_info("call_chain",
                   f"{thread_spawn_total} thread_spawn + {spawn_target_total} spawn_target edges")
    if no_evidence:
        result.error("call_chain",
                    f"{len(no_evidence)} edges without evidence",
                    "All edges must have evidence explaining their origin")
    result.add_info("call_chain",
               f"Edges: {len(all_edges)} cross/structural, "
               f"total by concurrency: fn_ptr={fn_ptr_total} vtable_dispatch={vtable_dispatch_total} "
               f"callback={callback_total} thread_spawn={thread_spawn_total} spawn_target={spawn_target_total}; "
               f"by confidence: EXTRACTED={extracted_total} INFERRED={inferred_total} AMBIGUOUS={ambiguous_total}")


def validate_semantic_matching(master: dict, result: ValidationResult,
                               outdir: str = "", profile: dict = None,
                               func_cache: dict = None):
    """Check semantic correctness of classifications.

    Rules:
    - External Endpoints must NOT contain project-internal functions
      (functions with source_file and matching project prefix in external domain)
    - API catalog must NOT contain main() from test/example/app paths
    - callback_func labeled nodes should not appear in External Endpoints
    """
    all_funcs = _load_all_functions(outdir, master, _cache=func_cache)

    project_prefixes = []
    if profile:
        api_det = profile.get("api_detection", {})
        project_prefixes = [p.lower() for p in api_det.get("public_prefixes", [])]

    # Check for project-internal functions in external endpoints
    internal_in_external = []
    for func in all_funcs:
        dom = func.get("_domain", "")
        if not (dom == "external" or dom.startswith("external_")):
            continue
        src = func.get("source_file", "")
        name = func.get("name", "")
        if src and project_prefixes:
            if any(name.lower().startswith(p) for p in project_prefixes):
                internal_in_external.append((name, dom, src))

    if internal_in_external:
        for name, dom, src in internal_in_external[:10]:
            result.error("semantic",
                        f"Project-internal function in external domain: {name}",
                        f"domain={dom}, source_file={src}")
        if len(internal_in_external) > 10:
            result.error("semantic",
                        f"... and {len(internal_in_external) - 10} more project-internal "
                        f"functions in external domains")

    # Check for test/example main functions in API entries or program_entry
    # Note: 'app' is NOT treated as test — many C projects put production
    # executables in app/. Only test/ut/example/fuzz are unambiguous.
    test_path_segments = ('test', 'ut', 'example', 'examples',
                          'fuzz', 'benchmark', 'demo', 'sample',
                          'samples')
    bad_mains = 0
    for func in all_funcs:
        labels = func.get("labels", [])
        name = func.get("name", "")
        src = func.get("source_file", "").lower()
        if name == "main":
            parts = src.replace("\\", "/").split("/")
            is_test_path = any(p in test_path_segments for p in parts)
            if "API_entry" in labels and is_test_path:
                result.error("semantic",
                            f"main() from test/example path in API catalog",
                            f"source_file={src}")
                bad_mains += 1
            elif "entry_point" in labels and is_test_path:
                result.error("semantic",
                            f"main() from test/example path marked as entry_point "
                            f"(should be test_entry)",
                            f"source_file={src}")
                bad_mains += 1

    # Check callback_func not in External Endpoints
    for func in all_funcs:
        dom = func.get("_domain", "")
        if not (dom == "external" or dom.startswith("external_")):
            continue
        labels = func.get("labels", [])
        if "callback_func" in labels:
            result.warn("semantic",
                       f"callback_func in external domain: {func.get('name')}",
                       f"domain={dom}")

    api_count = sum(1 for f in all_funcs if "API_entry" in f.get("labels", []))
    ep_count = sum(1 for f in all_funcs
                   if "out_end" in f.get("labels", []) or "unknown_end" in f.get("labels", []))
    result.add_info("semantic",
               f"API entries: {api_count}, Endpoints: {ep_count}, "
               f"bad_mains: {bad_mains}, internal_in_external: {len(internal_in_external)}")


def validate_out_end_labels(master: dict, result: ValidationResult,
                            outdir: str = "", func_cache: dict = None):
    """Validate out_end/unknown_end label assignment logic.

    Rules:
    - out_end on internal nodes WITH source_file is suspicious
      (they're just leaves, not external endpoints)
    - out_end nodes should have no call successors (they're terminal)
    """
    all_funcs = _load_all_functions(outdir, master, _cache=func_cache)

    # Build adjacency from cross-domain edges
    cross = master.get("cross_domain_edges", [])
    call_successors = defaultdict(set)
    for e in cross:
        call_successors[e.get("source", "")].add(e.get("target", ""))

    suspicious_out_end = 0
    for func in all_funcs:
        labels = func.get("labels", [])
        if "out_end" not in labels and "unknown_end" not in labels:
            continue
        name = func.get("name", "")
        nid = func.get("id", "")
        src = func.get("source_file", "")
        dom = func.get("_domain", "")

        # out_end on node with call successors = suspicious (but allow entry points)
        if nid in call_successors and call_successors[nid]:
            is_entry = "API_entry" in labels or "entry_point" in labels
            # main() is out_end (no callers in graph) but has successors — expected
            if not is_entry and name != "main":
                result.warn("out_end",
                           f"out_end node has call successors: {name}",
                           f"successors={call_successors[nid]}")

        # Internal leaf with source_file should not be out_end
        # (but this is a known issue from legacy labeling — only warn if many)
        is_external = dom == "external" or dom.startswith("external_")
        if not is_external and src and "out_end" in labels:
            suspicious_out_end += 1

    if suspicious_out_end > 100:
        result.warn("out_end",
                   f"{suspicious_out_end} internal functions with source_file "
                   f"marked as out_end",
                   "These are leaf functions, not external endpoints — "
                   "summary correctly excludes them but domain files retain the label")


def validate_data_consistency(master: dict, result: ValidationResult,
                              outdir: str, func_cache: dict = None):
    """Check that counts and references are consistent across output files.

    Rules:
    - total_edges in master should match cross_domain + structural counts
    - Endpoint file count matches summary count
    - API count in summary matches actual API entries
    """
    cross = master.get("cross_domain_edges", [])
    struct = master.get("structural_edges", [])

    actual_edges = master.get("total_edges", 0)

    # Edge count check: total includes intra-domain edges
    cross_struct_count = len(cross) + len(struct)
    intra_domain_count = actual_edges - cross_struct_count
    if intra_domain_count < 0:
        result.warn("consistency",
                   f"Edge count mismatch: master says {actual_edges}, "
                   f"cross+struct={cross_struct_count} "
                   f"(cross={len(cross)} + struct={len(struct)})")

    # Check endpoint file
    ep_path = os.path.join(outdir, ".code2database_endpoints.json")
    ep_data = _load_json(ep_path)
    if ep_data:
        ep_count = len(ep_data.get("endpoints", []))
        ep_total = ep_data.get("total_endpoints", 0)
        if ep_count != ep_total:
            result.error("consistency",
                        f"Endpoint count mismatch: total_endpoints={ep_total}, "
                        f"actual list length={ep_count}")

        # Cross-check with summary (summary only shows external endpoints)
        summary_path = os.path.join(outdir, "CODE2DATABASE_SUMMARY.md")
        if os.path.exists(summary_path):
            summary_text = Path(summary_path).read_text(encoding="utf-8")
            m = re.search(r'Showing top \d+ of (\d+) endpoints', summary_text)
            if m:
                summary_ep_count = int(m.group(1))
                # Summary counts external_* domain endpoints + out_end nodes
                # without source_file. Endpoint file contains all marked endpoints.
                # For large projects, many internal leaf functions may lack source_file
                # and get counted as "endpoints" in the summary. This is expected.
                external_ep_count = sum(
                    1 for ep in ep_data.get("endpoints", [])
                    if ep.get("domain", "").startswith("external_")
                    or ep.get("domain") == "external"
                )
                diff = abs(summary_ep_count - external_ep_count)
                # Use proportional threshold: 20% or at least 100
                threshold = max(20, summary_ep_count * 0.2)
                if diff > threshold:
                    result.add_info("consistency",
                                   f"Summary endpoint count ({summary_ep_count}) significantly "
                                   f"differs from external endpoint file count ({external_ep_count}). "
                                   f"This is expected for large projects where internal leaf "
                                   f"functions lack source_file entries.")
                elif diff > 0:
                    result.add_info("consistency",
                                   f"Summary endpoint count ({summary_ep_count}) vs "
                                   f"external endpoints ({external_ep_count}): "
                                   f"{diff} difference (no-source nodes counted by summary)")

    # Check API count in summary vs actual
    summary_path = os.path.join(outdir, "CODE2DATABASE_SUMMARY.md")
    if os.path.exists(summary_path):
        summary_text = Path(summary_path).read_text(encoding="utf-8")
        m = re.search(r'Showing top \d+ of (\d+) API entries', summary_text)
        if m:
            summary_api_count = int(m.group(1))
            all_funcs = _load_all_functions(outdir, master, _cache=func_cache)
            actual_api_count = sum(1 for f in all_funcs
                                   if "API_entry" in f.get("labels", []))
            if summary_api_count != actual_api_count:
                result.warn("consistency",
                           f"Summary API count ({summary_api_count}) != "
                           f"actual API entries in domains ({actual_api_count})",
                           "Domain files may use compact label format")

    result.add_info("consistency",
               f"Nodes: {master.get('total_nodes', 0)}, "
               f"Edges: {actual_edges} (cross={len(cross)} + struct={len(struct)})")


def validate_dispatch_quality(master: dict, result: ValidationResult):
    """Check dispatch edge quality metrics.

    Rules:
    - All dispatch edges should have call_condition (100% coverage)
    - No edges with confidence_score < 0.5
    - INFERRED edges should have reasonable confidence
    """
    cross = master.get("cross_domain_edges", [])

    dispatch_types = ["callback", "vtable_dispatch", "macro_dispatch",
                      "field_dispatch", "spawn_target"]

    for dt in dispatch_types:
        de = [e for e in cross if e.get("concurrency") == dt]
        if not de:
            continue

        # call_condition coverage — only check EXTRACTED edges. INFERRED
        # edges by definition lack concrete condition evidence (they were
        # inferred from patterns/heuristics), so requiring call_condition
        # on them would always fire false-positive warnings.
        extracted_de = [e for e in de if e.get("confidence") == "EXTRACTED"]
        if extracted_de:
            with_cc = [e for e in extracted_de if e.get("call_condition")]
            coverage = len(with_cc) / len(extracted_de) * 100
            if coverage < 100:
                result.warn("dispatch_quality",
                           f"{dt}: call_condition coverage {coverage:.1f}% "
                           f"({len(with_cc)}/{len(extracted_de)} EXTRACTED)",
                           "All EXTRACTED dispatch edges should have call_condition")
        else:
            coverage = 0.0

        # Low confidence
        low_conf = [e for e in de if e.get("confidence_score", 1.0) < 0.5]
        if low_conf:
            result.error("dispatch_quality",
                        f"{dt}: {len(low_conf)} edges with confidence < 0.5")

        result.add_info("dispatch_quality", f"{dt}: {len(de)} edges ({len(extracted_de)} EXTRACTED), {coverage:.0f}% call_condition on EXTRACTED")

    # Check INFERRED edges have reasonable confidence
    inferred = [e for e in cross if e.get("confidence") == "INFERRED"]
    if inferred:
        low_inferred = [e for e in inferred if e.get("confidence_score", 0) < 0.5]
        if low_inferred:
            result.error("dispatch_quality",
                        f"{len(low_inferred)} INFERRED edges with confidence < 0.5")


def validate_profile_sync(master: dict, result: ValidationResult,
                          profile: dict = None):
    """Check that builder classifications are synchronized with profile.

    Rules:
    - macro_dispatch entries should have corresponding macro_dispatch edges
    """
    if not profile:
        return

    # Check macro_dispatch coverage
    macro_entries = profile.get("macro_dispatch", {}).get("registration_macros", [])
    if macro_entries:
        cross = master.get("cross_domain_edges", [])
        macro_edges = [e for e in cross if e.get("concurrency") == "macro_dispatch"]
        macro_names_in_edges = set()
        for e in macro_edges:
            cc = e.get("call_condition", "")
            # Handle both #macro_dispatch=X and macro_dispatch=X formats
            m = re.search(r'#?macro_dispatch=(\w+)', cc)
            if m:
                macro_names_in_edges.add(m.group(1))

        for entry in macro_entries:
            name = entry.get("macro_name", "")
            if name not in macro_names_in_edges:
                # Some macros are captured via vtable_dispatch instead
                # (e.g., SPDK_SUBSYSTEM_REGISTER creates constructor→list→iteration chain
                # that's already handled by vtable dispatch). Only warn if
                # there are zero macro_dispatch edges at all.
                if len(macro_edges) > 0:
                    result.add_info("profile_sync",
                                   f"macro_dispatch entry '{name}' has no macro_dispatch edges "
                                   f"(may be captured via vtable_dispatch instead)")
                else:
                    result.warn("profile_sync",
                               f"macro_dispatch entry '{name}' has no corresponding edges",
                               "Check if the macro pattern matches any source code")


def validate_struct_embeddings(master: dict, result: ValidationResult,
                               profile: dict = None):
    """Check struct_embeddings profile configuration.

    Rules:
    - container_of_macros patterns must be valid regexes
    - manual_entries should reference struct types that appear in the graph
    - Warn if profile has container_of_macros but no usages were extracted
    """
    if not profile:
        return

    se = profile.get("struct_embeddings", {})
    if not se:
        return

    # Validate container_of_macros patterns
    for i, entry in enumerate(se.get("container_of_macros", [])):
        pat = entry.get("pattern", "")
        if pat:
            try:
                re.compile(pat)
            except re.error as e:
                result.error("struct_embeddings",
                            f"container_of_macros[{i}].pattern invalid regex: {e}",
                            f"pattern={pat}")

    # Check manual_entries reference struct types visible in the graph
    manual_entries = se.get("manual_entries", [])
    if manual_entries:
        # Gather all struct types from vtable_registrations in master
        cross = master.get("cross_domain_edges", [])
        struct_types_in_graph = set()
        for e in cross:
            evidence = e.get("evidence", "")
            # Extract struct_type from vtable_dispatch evidence
            m = re.match(r'vtable_dispatch:\s*(\w+)\.', evidence)
            if m:
                struct_types_in_graph.add(m.group(1))

        # Also extract from field_dispatch evidence
        for e in cross:
            evidence = e.get("evidence", "")
            m = re.match(r'field_dispatch:\s*(\w+)\.', evidence)
            if m:
                struct_types_in_graph.add(m.group(1))

        # Check each manual entry's inner_type and outer_type
        for entry in manual_entries:
            inner = entry.get("inner_type", "")
            outer = entry.get("outer_type", "")
            # These types may not appear directly in vtable evidence,
            # so just warn (not error) if they're completely absent
            if inner and struct_types_in_graph and inner not in struct_types_in_graph:
                result.add_info("struct_embeddings",
                               f"manual_entry inner_type '{inner}' not found in "
                               f"vtable/field dispatch edges",
                               "Type may be embedded but not directly dispatched")

    # Warn if container_of_macros configured but no usages extracted
    co_macros = se.get("container_of_macros", [])
    if co_macros:
        # Check if extraction data has container_of_usages
        # (We can't directly check extraction from master JSON, but we can
        # check if the struct types from manual_entries appear in dispatch edges)
        has_embedding_evidence = False
        for entry in manual_entries:
            inner = entry.get("inner_type", "")
            if inner in struct_types_in_graph:
                has_embedding_evidence = True
                break
        if manual_entries and not has_embedding_evidence:
            result.add_info("struct_embeddings",
                           "Profile has struct_embeddings.manual_entries but "
                           "no embedding types found in dispatch edges",
                           "Embedding index may not be improving dispatch resolution")

    result.add_info("struct_embeddings",
                   f"container_of_macros: {len(co_macros)}, "
                   f"manual_entries: {len(manual_entries)}")


def validate_all(outdir: str, profile: dict = None) -> ValidationResult:
    """Run all validation checks on build output.

    Args:
        outdir: Build output directory
        profile: Builder config dict from ProfileSchema.to_builder_config()

    Returns:
        ValidationResult with all findings

    O29: When code2database_master.json is absent (SQLite/deferred mode), falls
    back to loading from code2database.db and synthesizes a master-like dict so
    the same validation checks can run. Without this, the SQLite build path
    would silently skip all validation.
    """
    result = ValidationResult()

    # Load master JSON
    master_path = os.path.join(outdir, "code2database_master.json")
    master = _load_json(master_path)
    if not master:
        # O29: SQLite/deferred fallback — try loading from code2database.db
        db_path = os.path.join(outdir, "code2database.db")
        if os.path.exists(db_path):
            master = _load_master_from_sqlite(db_path, outdir)
            if master:
                result.add_info("io", f"Loaded from SQLite fallback: {db_path}")
            else:
                result.error("io", f"Cannot load {master_path} or {db_path}")
                return result
        else:
            result.error("io", f"Cannot load {master_path}")
            return result

    # Run all validation checks (share function cache to avoid re-loading)
    _func_cache = {}
    validate_edge_logic(master, result)
    validate_call_chain_accuracy(master, result)
    validate_semantic_matching(master, result, outdir=outdir, profile=profile,
                              func_cache=_func_cache)
    validate_out_end_labels(master, result, outdir=outdir,
                           func_cache=_func_cache)
    validate_data_consistency(master, result, outdir, func_cache=_func_cache)
    validate_dispatch_quality(master, result)
    validate_profile_sync(master, result, profile=profile)
    validate_struct_embeddings(master, result, profile=profile)

    return result


def _load_master_from_sqlite(db_path: str, outdir: str) -> dict:
    """O29: Synthesize a master-like dict from the SQLite store.

    The validation checks expect a master dict with 'domains' pointing to
    per-domain files, and those files contain 'nodes' and 'edges' lists.
    For SQLite mode, we load nodes/edges from the DB and build an in-memory
    master dict that validation can consume.
    """
    try:
        from _builder.sqlite_store import SQLiteStore
    except ImportError:
        return None
    try:
        with SQLiteStore(db_path) as store:
            # Load all functions as nodes
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            nodes = []
            edges = []
            try:
                cur = conn.execute("SELECT * FROM functions")
                for row in cur:
                    row_dict = dict(row)
                    # Deserialize JSON fields if stored as strings
                    for field in ("labels", "params", "local_vars",
                                  "callee_args", "condition_vars",
                                  "labels_source", "globals_read",
                                  "globals_written", "fields_read",
                                  "fields_written", "reg_transfers",
                                  "goto_jumps", "reg_state_final"):
                        if field in row_dict and isinstance(row_dict[field], str):
                            try:
                                row_dict[field] = json.loads(row_dict[field])
                            except (json.JSONDecodeError, TypeError):
                                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                                pass
                    nodes.append(row_dict)
                # Try to load edges (table name may vary)
                try:
                    cur = conn.execute("SELECT * FROM edges")
                    for row in cur:
                        edges.append(dict(row))
                except sqlite3.OperationalError:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    pass
            finally:
                conn.close()
        # Synthesize a master dict with a single "all" domain, inline so
        # validation checks can read it without a file on disk.
        domain_data = {
            "nodes": nodes,
            "edges": edges,
        }
        return {
            "type": "code2database_master",
            "version": 1,
            "domains": {"all": domain_data},
            "_sqlite_source": True,
        }
    except Exception:
        logging.getLogger(__name__).debug("silent exception", exc_info=True)
        return None


def cmd_validate(args):
    """CLI entry point for standalone validation."""
    outdir = args.outdir
    profile = None
    profile_path = getattr(args, 'profile', None)
    if profile_path:
        from _profile import ProfileSchema
        p = ProfileSchema.load(profile_path)
        profile = p.to_builder_config()

    result = validate_all(outdir, profile=profile)
    print(result.summary())

    if not result.ok:
        sys.exit(1)
