"""Tests for the scenario chain noise filter in _builder/query.py.

Verifies that _resolve_detailed_chain and _trace_simple_chain skip
auto-created placeholder nodes for builtins/external methods (e.g.,
`client.call('...')` → callee `call`, `dict.items()` → `items`) and
BSD queue macro expansions, so scenario chains terminate at real
project functions instead of noise.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import networkx as nx

from _builder.query import (
    _resolve_detailed_chain,
    _trace_simple_chain,
    _is_scenario_noise_target,
)


def _build_test_graph_with_noise_endpoints():
    """Build a graph where an API_entry calls a real function and a builtin.

    api_fn -> process_request  (real)
    api_fn -> call              (builtin-style: client.call(...))
    api_fn -> items             (builtin: dict.items())
    api_fn -> stailq_insert_tail (BSD queue macro)
    api_fn -> decode            (builtin: bytes.decode())
    """
    G = nx.DiGraph()
    G.add_node("api", name="api_fn", labels=["API_entry"], is_empty=False)
    G.add_node("proc", name="process_request", labels=[], is_empty=False)
    G.add_node("call_node", name="call", labels=[], is_empty=True)
    G.add_node("items_node", name="items", labels=[], is_empty=True)
    G.add_node("stailq_node", name="stailq_insert_tail", labels=[], is_empty=True)
    G.add_node("decode_node", name="decode", labels=[], is_empty=True)

    G.add_edge("api", "proc", relation="CALLS", concurrency="",
               call_condition="", confidence="EXTRACTED")
    G.add_edge("api", "call_node", relation="CALLS", concurrency="",
               call_condition="", confidence="EXTRACTED")
    G.add_edge("api", "items_node", relation="CALLS", concurrency="",
               call_condition="", confidence="EXTRACTED")
    G.add_edge("api", "stailq_node", relation="CALLS", concurrency="",
               call_condition="", confidence="EXTRACTED")
    G.add_edge("api", "decode_node", relation="CALLS", concurrency="",
               call_condition="", confidence="EXTRACTED")
    return G


class TestScenarioNoiseTargetDetector(unittest.TestCase):

    def test_python_builtin_method_is_noise(self):
        self.assertTrue(_is_scenario_noise_target("print"))
        self.assertTrue(_is_scenario_noise_target("add"))
        self.assertTrue(_is_scenario_noise_target("items"))
        self.assertTrue(_is_scenario_noise_target("decode"))
        self.assertTrue(_is_scenario_noise_target("pop"))
        self.assertTrue(_is_scenario_noise_target("replace"))

    def test_generic_external_method_is_noise(self):
        self.assertTrue(_is_scenario_noise_target("call"))
        self.assertTrue(_is_scenario_noise_target("marshal"))
        self.assertTrue(_is_scenario_noise_target("unmarshal"))
        self.assertTrue(_is_scenario_noise_target("info"))
        self.assertTrue(_is_scenario_noise_target("errorf"))
        self.assertTrue(_is_scenario_noise_target("argumentparser"))
        self.assertTrue(_is_scenario_noise_target("add_argument"))
        self.assertTrue(_is_scenario_noise_target("parse_args"))

    def test_python_stdlib_functions_are_noise(self):
        # json.dump, json.load, etc. — the scanner extracts only the last
        # part ('dump') as the callee, so the bare name must be filtered.
        self.assertTrue(_is_scenario_noise_target("dump"))
        self.assertTrue(_is_scenario_noise_target("load"))
        self.assertTrue(_is_scenario_noise_target("loads"))
        self.assertTrue(_is_scenario_noise_target("dumps"))
        self.assertTrue(_is_scenario_noise_target("exec"))
        self.assertTrue(_is_scenario_noise_target("eval"))

    def test_dotted_builtin_module_is_noise(self):
        self.assertTrue(_is_scenario_noise_target("os.path.join"))
        self.assertTrue(_is_scenario_noise_target("json.loads"))
        self.assertTrue(_is_scenario_noise_target("sys.exit"))

    def test_bsd_queue_macro_is_noise(self):
        self.assertTrue(_is_scenario_noise_target("stailq_insert_tail"))
        self.assertTrue(_is_scenario_noise_target("tailq_insert_head"))
        self.assertTrue(_is_scenario_noise_target("list_remove"))
        self.assertTrue(_is_scenario_noise_target("splay_left"))

    def test_real_function_name_is_not_noise(self):
        self.assertFalse(_is_scenario_noise_target("spdk_nvme_init"))
        self.assertFalse(_is_scenario_noise_target("process_request"))
        self.assertFalse(_is_scenario_noise_target("vfu_tgt_set_base_path"))

    def test_generic_verb_that_can_be_real_function_is_not_noise(self):
        self.assertFalse(_is_scenario_noise_target("start"))
        self.assertFalse(_is_scenario_noise_target("stop"))
        self.assertFalse(_is_scenario_noise_target("run"))

    def test_conditional_placeholder_is_not_noise(self):
        self.assertFalse(_is_scenario_noise_target("<conditional:if(x > 0)>"))
        self.assertFalse(_is_scenario_noise_target("<conditional:while_cond(i++)>"))

    def test_empty_name_is_not_noise(self):
        self.assertFalse(_is_scenario_noise_target(""))


class TestResolveDetailedChainSkipsNoise(unittest.TestCase):

    def test_resolve_detailed_chain_skips_builtin_and_macro_endpoints(self):
        G = _build_test_graph_with_noise_endpoints()
        result = _resolve_detailed_chain(G, "api", {})
        targets = [step["target"] for step in result["steps"]]
        self.assertIn("process_request", targets)
        self.assertNotIn("call", targets)
        self.assertNotIn("items", targets)
        self.assertNotIn("stailq_insert_tail", targets)
        self.assertNotIn("decode", targets)

    def test_resolve_detailed_chain_returns_only_real_steps(self):
        G = _build_test_graph_with_noise_endpoints()
        result = _resolve_detailed_chain(G, "api", {})
        self.assertEqual(len(result["steps"]), 1)
        self.assertEqual(result["steps"][0]["target"], "process_request")


class TestTraceSimpleChainSkipsNoise(unittest.TestCase):

    def test_trace_simple_chain_skips_builtin_endpoints(self):
        G = _build_test_graph_with_noise_endpoints()
        chain = _trace_simple_chain(G, "api", {})
        self.assertIn("api_fn", chain)
        joined = " ".join(chain)
        self.assertIn("process_request", joined)
        self.assertNotIn("→call", joined)
        self.assertNotIn("→items", joined)
        self.assertNotIn("→stailq_insert_tail", joined)
        self.assertNotIn("→decode", joined)


if __name__ == "__main__":
    unittest.main()
