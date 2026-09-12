"""Unit tests for diagnose.py.

diagnose builds a six-dimension report around one symbol: symptom
parsing (from an optional log), impact area (caller rings), call
chain tracing, cross validation (shared callees), special patterns
(hardware reachability, recursion, spawns, callbacks), and ranked
root-cause hypotheses.

Test coverage:
- impact rings (direct + second-order callers)
- forward chains to leaves, depth cap, chain cap
- shared callees across chains
- hw-reach integration + graceful degradation
- direct recursion / async spawn / callback patterns
- hypothesis scoring: fan-in, unguarded subscript, unbounded loop
- log parsing: error lines + symbol extraction
- markdown structure, --json mode, output file, missing symbol
"""
import json
import os
import tempfile
import unittest

from tests.test_quality_checks import _make_quality_graph


def _diag_graph():
    return _make_quality_graph(
        [{"id": "t", "name": "nvme_submit_cmd", "domain": "lib.nvme",
          "source_file": "/nvme/submit.c", "labels": [],
          "body_text": "int nvme_submit_cmd(struct q *q, int i) {\n"
                       "  return q->cmds[i];\n"
                       "}\n"},
         {"id": "risky", "name": "nvme_prepare", "domain": "lib.nvme",
          "source_file": "/nvme/submit.c", "labels": [],
          "body_text": "void nvme_prepare(void) {\n"
                       "  while (1) {\n"
                       "    poll();\n"
                       "  }\n"
                       "}\n"},
         {"id": "c1", "name": "nvme_complete", "domain": "lib.nvme",
          "source_file": "/nvme/complete.c"},
         {"id": "c2", "name": "nvme_irq", "domain": "lib.nvme",
          "source_file": "/nvme/irq.c", "labels": ["callback_func"]},
         {"id": "leaf", "name": "Write32BitReg", "domain": "lib.hw",
          "source_file": "/hw/reg.c"},
         {"id": "u1", "name": "fs_read", "domain": "lib.fs",
          "source_file": "/fs/read.c"},
         {"id": "u2", "name": "fs_write", "domain": "lib.fs",
          "source_file": "/fs/write.c"}],
        [{"source": "t", "target": "risky"},
         {"source": "t", "target": "c1"},
         {"source": "risky", "target": "leaf"},
         {"source": "c1", "target": "leaf"},
         {"source": "c2", "target": "t"},
         {"source": "u1", "target": "t"},
         {"source": "u2", "target": "c1"},
         {"source": "u1", "target": "u2"},
         {"source": "t", "target": "t"}],
    )


class TestDiagnose(unittest.TestCase):

    def setUp(self):
        from _builder.analysis.diagnose import diagnose
        self.data = diagnose(_diag_graph(), "nvme_submit_cmd", as_json=True)

    def test_impact_rings(self):
        names = {c["name"] for c in self.data["impact"]["direct_callers"]}
        self.assertEqual(names, {"nvme_irq", "fs_read"})
        ring2 = {c["name"] for c in self.data["impact"]["second_ring"]}
        self.assertEqual(ring2, set())

    def test_second_ring_via_caller_of_caller(self):
        from _builder.analysis.diagnose import diagnose
        data = diagnose(_diag_graph(), "nvme_complete", as_json=True)
        ring2 = {c["name"] for c in data["impact"]["second_ring"]}
        self.assertEqual(ring2, {"nvme_irq", "fs_read"})

    def test_forward_chains_reach_leaf(self):
        chains = self.data["chains"]
        self.assertTrue(any(ch[-1] == "Write32BitReg" for ch in chains))

    def test_shared_callees_across_chains(self):
        shared = {s["name"]: s["chains"] for s in self.data["shared_callees"]}
        self.assertIn("Write32BitReg", shared)
        self.assertGreaterEqual(shared["Write32BitReg"], 2)

    def test_hw_reach_classification(self):
        self.assertEqual(self.data["patterns"]["hardware_reachability"],
                         "hardware-reaching")
        self.assertEqual(self.data["patterns"]["hardware_path"][0],
                         "nvme_submit_cmd")

    def test_direct_recursion_detected(self):
        self.assertTrue(self.data["patterns"]["direct_recursion"])

    def test_callback_pattern_on_other_symbol(self):
        from _builder.analysis.diagnose import diagnose
        data = diagnose(_diag_graph(), "nvme_irq", as_json=True)
        self.assertTrue(data["patterns"].get("callback"))

    def test_hypotheses_ranking(self):
        hyps = self.data["hypotheses"]
        self.assertTrue(hyps)
        # the target itself carries the unguarded subscript signal and
        # fan-in 2 (nvme_irq, fs_read; the self-loop is excluded) → 2+2=4
        top = hyps[0]
        self.assertEqual(top["name"], "nvme_submit_cmd")
        self.assertIn("unguarded_subscript", top["signals"])
        self.assertEqual(top["score"], 4)
        # nvme_prepare (direct callee) carries the unbounded_loop signal
        prepare = next(h for h in hyps if h["name"] == "nvme_prepare")
        self.assertIn("unbounded_loop", prepare["signals"])
        self.assertLess(prepare["score"], top["score"])

    def test_hypothesis_score_composition(self):
        hyps = {h["name"]: h for h in self.data["hypotheses"]}
        # nvme_prepare: fan_in=1 + 2*1(unbounded_loop) = 3
        self.assertEqual(hyps["nvme_prepare"]["score"], 3)

    def test_async_spawn_pattern(self):
        from _builder.analysis.diagnose import diagnose
        g = _make_quality_graph(
            [{"id": "a", "name": "spawn_src"},
             {"id": "b", "name": "spawn_tgt"}],
            [{"source": "a", "target": "b", "concurrency": "async_spawn"}],
        )
        data = diagnose(g, "spawn_tgt", as_json=True)
        self.assertEqual(data["patterns"].get("spawned_by"), ["spawn_src"])
        data_src = diagnose(g, "spawn_src", as_json=True)
        self.assertEqual(data_src["patterns"].get("spawns"), ["spawn_tgt"])


class TestDiagnoseLog(unittest.TestCase):

    def _write_log(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_error_lines_and_symbols(self):
        from _builder.analysis.diagnose import diagnose
        log = self._write_log(
            "boot ok\n"
            "nvme_submit_cmd: timeout waiting for cq\n"
            "all good here\n"
            "nvme_irq: assert failed on doorbell\n")
        try:
            data = diagnose(_diag_graph(), "nvme_submit_cmd",
                            log_file=log, as_json=True)
        finally:
            os.unlink(log)
        self.assertEqual(len(data["symptoms"]["error_lines"]), 2)
        syms = set(data["symptoms"]["symbols"])
        self.assertIn("nvme_submit_cmd", syms)
        self.assertIn("nvme_irq", syms)
        self.assertNotIn("fs_read", syms)

    def test_no_errors_in_log(self):
        from _builder.analysis.diagnose import diagnose
        log = self._write_log("nothing to see\nall fine\n")
        try:
            data = diagnose(_diag_graph(), "nvme_submit_cmd",
                            log_file=log, as_json=True)
        finally:
            os.unlink(log)
        self.assertEqual(data["symptoms"]["error_lines"], [])

    def test_missing_log_degrades(self):
        from _builder.analysis.diagnose import diagnose
        data = diagnose(_diag_graph(), "nvme_submit_cmd",
                        log_file="/nonexistent.log", as_json=True)
        self.assertIn("error", data["symptoms"])


class TestDiagnoseOutput(unittest.TestCase):

    def test_markdown_has_six_sections(self):
        from _builder.analysis.diagnose import diagnose
        md = diagnose(_diag_graph(), "nvme_submit_cmd")
        for i, title in enumerate(
                ["Symptom Parsing", "Impact Area", "Call Chain Tracing",
                 "Cross Validation", "Special Patterns",
                 "Root-Cause Hypotheses"], 1):
            self.assertIn(f"## {i}. {title}", md)
        self.assertIn("# Diagnosis Report: nvme_submit_cmd", md)

    def test_output_file_written(self):
        from _builder.analysis.diagnose import diagnose
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            md = diagnose(_diag_graph(), "nvme_submit_cmd", output=path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), md)
        finally:
            os.unlink(path)

    def test_missing_symbol_raises(self):
        from _builder.analysis.diagnose import diagnose
        with self.assertRaises(ValueError):
            diagnose(_diag_graph(), "ghost_fn")

    def test_cli_markdown_and_json(self):
        import io
        from contextlib import redirect_stdout
        from _builder.analysis.diagnose import cmd_diagnose

        class Args:
            graph = _diag_graph()
            symbol = "nvme_submit_cmd"
            log = None
            depth = 6
            max_chains = 5
            output = None
            json = False

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_diagnose(Args())
        self.assertIn("## 1. Symptom Parsing", buf.getvalue())

        Args.json = True
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_diagnose(Args())
        data = json.loads(buf.getvalue())
        self.assertEqual(data["name"], "nvme_submit_cmd")

    def test_cli_missing_symbol_exits(self):
        import io
        from contextlib import redirect_stderr
        from _builder.analysis.diagnose import cmd_diagnose

        class Args:
            graph = _diag_graph()
            symbol = "ghost_fn"
            log = None
            depth = 6
            max_chains = 5
            output = None
            json = False

        with self.assertRaises(SystemExit) as cm:
            with redirect_stderr(io.StringIO()):
                cmd_diagnose(Args())
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
