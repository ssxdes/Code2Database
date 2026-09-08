"""callgraph builder module: index_pack."""

import os
import json
import sys
import re
from pathlib import Path
from collections import Counter, defaultdict
import networkx as nx
from _builder.query.query import _resolve_detailed_chain, _trace_simple_chain
from _builder.token_budget import estimate_tokens
from _builder.utils import normalize_str_field
import logging








# Import universal skip names from scanner for automatic external endpoint classification
try:
    from _vendor._regex_c_scanner import _UNIVERSAL_SKIP_NAMES as _SCANNER_SKIP_NAMES
except ImportError:
    _SCANNER_SKIP_NAMES = frozenset()



from _builder.export.context_packs import _build_context_pack, _build_micro_pack, _write_context_pack_micro_md, _derive_architecture_patterns, _write_context_pack_lite_md, _truncate_desc, _write_review_checklist

from _builder.export.indexes import _build_indexes, _build_scenarios_file, _build_scenarios_summary_md, _compute_cross_domain_hotspots, _compute_data_flow, _compute_hub_functions, _compute_scenarios, _generate_mermaid_path_diagram, _classify_endpoint, _mark_endpoint_nodes

from _builder.export.summary_md import _build_callgraph_summary_md, _build_architecture_flows_md
