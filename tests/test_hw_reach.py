"""Unit tests for hw_reach.py and the hardware_terminals profile key.

hw_reach traces call chains from a symbol toward hardware terminal
functions and classifies the symbol into one of four levels:
hardware-reaching, hold-flush, software-gate, software-only.

Test coverage:
- terminal hit through direct and transitive call chains
- the symbol itself being a terminal
- hold-flush naming
- software-gate (gate-style name, few callees, no terminal path)
- software-only
- depth cutoff
- max path cap
- profile-driven hardware_terminals merging
- --terminals extra patterns
- missing symbol error
- profile schema: hardware_terminals validation + to_builder_config
"""
import json
import os
import tempfile
import unittest

from tests.test_quality_checks import _make_quality_graph


def _hw_graph():
    return _make_quality_graph(
        [{"id": "drv_init", "name": "drv_init"},
         {"id": "drv_cfg", "name": "drv_configure_port"},
         {"id": "reg_write", "name": "Write32BitReg"},
         {"id": "spi_xfer", "name": "SpiWrite"},
         {"id": "hold_one", "name": "SetCfgHold"},
         {"id": "helper", "name": "drv_helper"},
         {"id": "plain", "name": "drv_stats_collect"}],
        [{"source": "drv_init", "target": "reg_write"},
         {"source": "drv_cfg", "target": "spi_xfer"},
         {"source": "drv_init", "target": "plain"},
         {"source": "plain", "target": "helper"},
         {"source": "hold_one", "target": "drv_init"},
         {"source": "drv_init", "target": "drv_init", "relation": "DATA_FLOW"}],
    )


class TestHwReach(unittest.TestCase):

    def test_direct_terminal_reach(self):
        from _builder.analysis.hw_reach import hw_reach
        result = hw_reach(_hw_graph(), "drv_init")
        self.assertEqual(result["classification"], "hardware-reaching")
        self.assertIn(["drv_init", "Write32BitReg"],
                      [p for p in result["paths"]])

    def test_transitive_terminal_reach(self):
        from _builder.analysis.hw_reach import hw_reach
        result = hw_reach(_hw_graph(), "drv_configure_port")
        self.assertEqual(result["classification"], "hardware-reaching")
        self.assertEqual(result["paths"][0],
                         ["drv_configure_port", "SpiWrite"])

    def test_symbol_itself_is_terminal(self):
        from _builder.analysis.hw_reach import hw_reach
        result = hw_reach(_hw_graph(), "SpiWrite")
        self.assertEqual(result["classification"], "hardware-reaching")
        self.assertEqual(result["paths"], [["SpiWrite"]])

    def test_hold_flush_classification(self):
        from _builder.analysis.hw_reach import hw_reach
        result = hw_reach(_hw_graph(), "SetCfgHold")
        self.assertEqual(result["classification"], "hold-flush")
        self.assertEqual(result["paths"], [])

    def test_software_gate(self):
        from _builder.analysis.hw_reach import hw_reach
        # drv_configure_port reaches hardware, so build a gate-only graph
        g = _make_quality_graph(
            [{"id": "gate", "name": "SetLaneEnable"},
             {"id": "inner", "name": "mark_lane_dirty"}],
            [{"source": "gate", "target": "inner"}],
        )
        result = hw_reach(g, "SetLaneEnable")
        self.assertEqual(result["classification"], "software-gate")

    def test_software_only(self):
        from _builder.analysis.hw_reach import hw_reach
        result = hw_reach(_hw_graph(), "drv_stats_collect")
        self.assertEqual(result["classification"], "software-only")

    def test_software_only_when_gate_name_but_many_callees(self):
        from _builder.analysis.hw_reach import hw_reach
        nodes = [{"id": "g", "name": "SetEverything"}]
        nodes += [{"id": "c%d" % i, "name": "helper%d" % i} for i in range(5)]
        edges = [{"source": "g", "target": "c%d" % i} for i in range(5)]
        g = _make_quality_graph(nodes, edges)
        result = hw_reach(g, "SetEverything")
        self.assertEqual(result["classification"], "software-only")

    def test_leaf_with_gate_name_is_software_only(self):
        from _builder.analysis.hw_reach import hw_reach
        g = _make_quality_graph([{"id": "l", "name": "EnableClock"}], [])
        result = hw_reach(g, "EnableClock")
        self.assertEqual(result["classification"], "software-only")
        self.assertEqual(result["callee_count"], 0)

    def test_depth_cutoff(self):
        from _builder.analysis.hw_reach import hw_reach
        g = _make_quality_graph(
            [{"id": "cfg", "name": "ConfigurePort"},
             {"id": "t", "name": "SpiWrite"}],
            [{"source": "cfg", "target": "t"}],
        )
        # ConfigurePort -> SpiWrite is depth 1; depth 0 must not see it
        result = hw_reach(g, "ConfigurePort", depth=0)
        self.assertEqual(result["classification"], "software-gate")
        result = hw_reach(g, "ConfigurePort", depth=1)
        self.assertEqual(result["classification"], "hardware-reaching")

    def test_max_paths_cap(self):
        from _builder.analysis.hw_reach import hw_reach
        g = _make_quality_graph(
            [{"id": "root", "name": "fan_out"},
             {"id": "t1", "name": "Write16BitReg"},
             {"id": "t2", "name": "Write32BitReg"},
             {"id": "t3", "name": "SpiWrite"}],
            [{"source": "root", "target": "t1"},
             {"source": "root", "target": "t2"},
             {"source": "root", "target": "t3"}],
        )
        result = hw_reach(g, "fan_out", max_paths=2)
        self.assertEqual(result["classification"], "hardware-reaching")
        self.assertEqual(len(result["paths"]), 2)
        self.assertEqual(len(result["terminal_hits"]), 2)

    def test_data_flow_edges_not_traversed(self):
        from _builder.analysis.hw_reach import hw_reach
        # drv_init has a DATA_FLOW self-edge only; without call edges to
        # terminals it would still be hardware-reaching via reg_write,
        # so use a graph where the only terminal link is DATA_FLOW.
        g = _make_quality_graph(
            [{"id": "a", "name": "plain_fn"},
             {"id": "t", "name": "Write8BitReg"}],
            [{"source": "a", "target": "t", "relation": "DATA_FLOW"}],
        )
        result = hw_reach(g, "plain_fn")
        self.assertEqual(result["classification"], "software-only")

    def test_missing_symbol_raises(self):
        from _builder.analysis.hw_reach import hw_reach
        with self.assertRaises(ValueError):
            hw_reach(_hw_graph(), "no_such_fn")

    def test_profile_terminals_merge(self):
        from _builder.analysis.hw_reach import hw_reach
        g = _make_quality_graph(
            [{"id": "a", "name": "custom_entry"},
             {"id": "t", "name": "MyCustomHwWrite"}],
            [{"source": "a", "target": "t"}],
        )
        # without the project pattern: gate (custom_entry has 1 callee,
        # but "custom_entry" has no gate keyword -> software-only)
        result = hw_reach(g, "custom_entry")
        self.assertEqual(result["classification"], "software-only")
        # with the project pattern: hardware-reaching
        result = hw_reach(g, "custom_entry",
                          profile={"hardware_terminals": [r"\bMyCustomHwWrite\b"]})
        self.assertEqual(result["classification"], "hardware-reaching")
        self.assertEqual(result["terminal_hits"][0]["pattern"],
                         r"\bMyCustomHwWrite\b")

    def test_extra_patterns_cli_style(self):
        from _builder.analysis.hw_reach import hw_reach
        g = _make_quality_graph(
            [{"id": "a", "name": "dispatch_cmd"},
             {"id": "t", "name": "MmioWrite32"}],
            [{"source": "a", "target": "t"}],
        )
        result = hw_reach(g, "dispatch_cmd", extra_patterns=[r"\bMmioWrite32\b"])
        self.assertEqual(result["classification"], "hardware-reaching")


class TestHwProfileLoading(unittest.TestCase):

    def test_load_from_graph_dir(self):
        from _builder.analysis.hw_reach import load_hw_profile
        tmp = tempfile.mkdtemp(prefix="c2d_hwprof_")
        with open(os.path.join(tmp, ".code2database_profile.json"), "w") as f:
            json.dump({"hardware_terminals": [r"\bFoo\b"]}, f)
        prof = load_hw_profile(tmp)
        self.assertEqual(prof["hardware_terminals"], [r"\bFoo\b"])

    def test_explicit_path_wins(self):
        from _builder.analysis.hw_reach import load_hw_profile
        tmp = tempfile.mkdtemp(prefix="c2d_hwprof_")
        persisted = os.path.join(tmp, ".code2database_profile.json")
        with open(persisted, "w") as f:
            json.dump({"hardware_terminals": [r"\bPersisted\b"]}, f)
        explicit = os.path.join(tmp, "other.json")
        with open(explicit, "w") as f:
            json.dump({"hardware_terminals": [r"\bExplicit\b"]}, f)
        prof = load_hw_profile(tmp, explicit)
        self.assertEqual(prof["hardware_terminals"], [r"\bExplicit\b"])

    def test_missing_profile_returns_empty(self):
        from _builder.analysis.hw_reach import load_hw_profile
        self.assertEqual(load_hw_profile("/nonexistent_dir_xyz"), {})


class TestHardwareTerminalsSchema(unittest.TestCase):

    def test_default_profile_has_empty_list(self):
        from _profile.schema import ProfileSchema
        prof = ProfileSchema.defaults()
        self.assertEqual(prof._raw["hardware_terminals"], [])

    def test_valid_patterns_accepted(self):
        from _profile.schema import ProfileSchema
        prof = ProfileSchema.from_dict(
            {"hardware_terminals": [r"\bMmioWrite\d+\b"]})
        self.assertEqual(prof._raw["hardware_terminals"], [r"\bMmioWrite\d+\b"])

    def test_non_list_rejected(self):
        from _profile.schema import ProfileSchema
        with self.assertRaises(ValueError):
            ProfileSchema.from_dict({"hardware_terminals": "not-a-list"})

    def test_invalid_regex_rejected(self):
        from _profile.schema import ProfileSchema
        with self.assertRaises(ValueError):
            ProfileSchema.from_dict({"hardware_terminals": ["[unclosed"]})

    def test_builder_config_passthrough(self):
        from _profile.schema import ProfileSchema
        prof = ProfileSchema.from_dict(
            {"hardware_terminals": [r"\bBar\b"]})
        cfg = prof.to_builder_config()
        self.assertEqual(cfg["hardware_terminals"], [r"\bBar\b"])


class TestHwReachCLI(unittest.TestCase):

    def test_cli_prints_json(self):
        import io
        from contextlib import redirect_stdout
        from _builder.analysis.hw_reach import cmd_hw_reach

        class Args:
            graph = _hw_graph()
            node = "drv_init"
            depth = 6
            max_paths = 5
            profile = None
            terminals = ""

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_hw_reach(Args())
        data = json.loads(buf.getvalue())
        self.assertEqual(data["classification"], "hardware-reaching")

    def test_cli_missing_node_exits(self):
        import io
        from contextlib import redirect_stderr
        from _builder.analysis.hw_reach import cmd_hw_reach

        class Args:
            graph = _hw_graph()
            node = "ghost_fn"
            depth = 6
            max_paths = 5
            profile = None
            terminals = ""

        with self.assertRaises(SystemExit) as cm:
            with redirect_stderr(io.StringIO()):
                cmd_hw_reach(Args())
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
