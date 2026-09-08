"""callgraph builder module: query."""

import os
import json
import sys
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.utils import _is_condition_alive, _output_result, _find_node_id, _parse_bindings, _load_globals, _streaming_json_lookup, _streaming_json_has_keys
from _builder.graph.graph_build import _load_full_graph
from _builder.token_budget import estimate_tokens, truncate_to_tokens, budget_describe
from _builder.query.query_helpers import _is_scenario_noise_target, _describe_node_touched, _load_profile_from_graph_dir, _is_vtable_dispatch_alive, _resolve_detailed_chain, _resolve_simple_chain, _trace_simple_chain, _compute_exec_summary, _compute_hub_info, _fetch_foreign_refs_for_node, _get_code_snippet, _collect_dispatch_info, _get_io_keywords, _io_path_score, _io_path_bfs, _value_is_null_form, _value_is_null_form_match
from _builder.query.query_cache import cached_query, invalidate_node as _cache_invalidate_node









from _builder.query.query_describe import cmd_describe_node, cmd_diff_chains, cmd_resolve_chain, _cmd_trace_chain_touched, cmd_trace_chain

from _builder.query.query_io_flow import cmd_get_code_snippet, cmd_io_path, cmd_param_flow, cmd_blast_radius, cmd_field_access, cmd_field_flow, _cmd_reverse_trace_touched, cmd_reverse_trace

from _builder.query.query_provenance import cmd_describe_commit, cmd_node_history, cmd_graph_provenance, cmd_blame_node, cmd_find_commits
