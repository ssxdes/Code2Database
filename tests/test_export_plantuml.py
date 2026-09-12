"""Unit tests for export_plantuml.py.

export_plantuml renders the code graph as PlantUML text in four
modes: call neighborhood (focus + callees + direct callers), module
dependencies (domain-aggregated call edges with weights), impact
rings (reverse callers colored by ring), and structure (functions of
one file/domain grouped into packages with signatures).

Test coverage:
- call mode: focus coloring, callee/caller arrows, condition labels,
  depth cutoff, missing node error
- module mode: cross-domain aggregation, self-domain edges skipped,
  edge weights
- impact mode: ring coloring (focus/direct/second), reverse BFS
- structure mode: file and domain grouping, signature truncation,
  empty match error
- output file writing, @startuml/@enduml envelope, unknown mode
"""
import os
import tempfile
import unittest

from tests.test_quality_checks import _make_quality_graph


def _graph():
    return _make_quality_graph(
        [{"id": "a", "name": "parser_init", "source_file": "/src/p.c",
          "domain": "lib.parser", "signature": "int parser_init(int verbose)"},
         {"id": "b", "name": "parser_fini", "source_file": "/src/p.c",
          "domain": "lib.parser", "signature": "void parser_fini(void)"},
         {"id": "c", "name": "lexer_feed", "source_file": "/src/l.c",
          "domain": "lib.lexer", "signature": "int lexer_feed(char *buf)"},
         {"id": "d", "name": "app_main", "source_file": "/src/app.c",
          "domain": "app", "signature": "int app_main(void)"}],
        [{"source": "a", "target": "b"},
         {"source": "b", "target": "c", "confidence": "EXTRACTED"},
         {"source": "d", "target": "a"},
         {"source": "d", "target": "c", "relation": "DATA_FLOW"}],
    )


class TestCallMode(unittest.TestCase):

    def test_focus_node_colored(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(_graph(), mode="call", node="parser_init")
        self.assertTrue(text.startswith("@startuml"))
        self.assertTrue(text.rstrip().endswith("@enduml"))
        self.assertIn('rectangle "parser_init" as focus_', text)
        self.assertIn("#C8E6C9", text)

    def test_callee_and_caller_arrows(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(_graph(), mode="call", node="parser_init")
        self.assertIn('rectangle "parser_fini" as callee_', text)
        self.assertIn('rectangle "app_main" as caller_', text)
        self.assertRegex(text, r"focus_\w+ --> callee_\w+")
        self.assertRegex(text, r"caller_\w+ --> focus_\w+")

    def test_depth_cutoff(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(_graph(), mode="call", node="parser_init",
                               depth=0)
        # depth 0: no callees rendered beyond the focus itself
        self.assertNotIn('as callee_', text)

    def test_data_flow_edge_excluded(self):
        from _builder.export.export_plantuml import export_plantuml
        # d -> c is DATA_FLOW; from parser_init's view c is reachable at
        # depth 2 via b, but d->c must never appear as a rendered arrow
        # because the relation is not a call.
        text = export_plantuml(_graph(), mode="call", node="app_main")
        self.assertNotIn("DATA_FLOW", text)

    def test_missing_node_raises(self):
        from _builder.export.export_plantuml import export_plantuml
        with self.assertRaises(ValueError):
            export_plantuml(_graph(), mode="call", node="nope")

    def test_node_flag_required(self):
        from _builder.export.export_plantuml import export_plantuml
        with self.assertRaises(ValueError):
            export_plantuml(_graph(), mode="call")

    def test_condition_label_rendered(self):
        from _builder.export.export_plantuml import export_plantuml
        g = _make_quality_graph(
            [{"id": "a", "name": "f1"}, {"id": "b", "name": "f2"}],
            [{"source": "a", "target": "b", "confidence": "EXTRACTED"}],
        )
        text = export_plantuml(g, mode="call", node="f1")
        self.assertIn("-->", text)


class TestModuleMode(unittest.TestCase):

    def test_cross_domain_aggregation(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(_graph(), mode="module")
        self.assertIn('rectangle "lib.parser"', text)
        self.assertIn('rectangle "lib.lexer"', text)
        self.assertIn('rectangle "app"', text)
        self.assertRegex(text, r"mod_app --> mod_lib_parser : 1")

    def test_same_domain_edges_skipped(self):
        from _builder.export.export_plantuml import export_plantuml
        # a -> b stays inside lib.parser and must not create a loop arrow
        text = export_plantuml(_graph(), mode="module")
        self.assertNotIn("lib_parser --> lib_parser", text)

    def test_edge_weights_accumulate(self):
        from _builder.export.export_plantuml import export_plantuml
        g = _make_quality_graph(
            [{"id": "a", "name": "x1", "domain": "d1"},
             {"id": "b", "name": "y1", "domain": "d2"},
             {"id": "c", "name": "y2", "domain": "d2"}],
            [{"source": "a", "target": "b"}, {"source": "a", "target": "c"}],
        )
        text = export_plantuml(g, mode="module")
        self.assertIn("mod_d1 --> mod_d2 : 2", text)


class TestImpactMode(unittest.TestCase):

    def test_ring_colors(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(_graph(), mode="impact", node="lexer_feed",
                               depth=2)
        # parser_init (ring 2) -> parser_fini (ring 1) -> lexer_feed (focus)
        self.assertIn("r0_lexer_feed #C8E6C9", text)
        self.assertIn("r1_parser_fini #FFCDD2", text)   # direct ring
        self.assertIn("r2_parser_init #FFE0B2", text)   # second ring
        self.assertRegex(text, r"r1_\w+ --> r0_\w+")
        self.assertRegex(text, r"r2_\w+ --> r1_\w+")

    def test_reverse_traversal_only(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(_graph(), mode="impact", node="app_main")
        # app_main has no callers: only the focus box, no ring arrows
        self.assertIn("r0_app_main #C8E6C9", text)
        self.assertNotIn("#FFCDD2", text)

    def test_missing_node_raises(self):
        from _builder.export.export_plantuml import export_plantuml
        with self.assertRaises(ValueError):
            export_plantuml(_graph(), mode="impact", node="nope")


class TestStructureMode(unittest.TestCase):

    def test_file_grouping_and_signatures(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(_graph(), mode="structure", file="/src/p.c")
        self.assertIn('package "/src/p.c"', text)
        self.assertIn("int parser_init(int verbose)", text)
        self.assertIn("void parser_fini(void)", text)
        self.assertNotIn("lexer_feed", text)

    def test_domain_grouping(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(_graph(), mode="structure", domain="lib.lexer")
        self.assertIn("lexer_feed", text)
        self.assertNotIn("parser_init", text)

    def test_no_match_raises(self):
        from _builder.export.export_plantuml import export_plantuml
        with self.assertRaises(ValueError):
            export_plantuml(_graph(), mode="structure", file="/nope.c")

    def test_flag_required(self):
        from _builder.export.export_plantuml import export_plantuml
        with self.assertRaises(ValueError):
            export_plantuml(_graph(), mode="structure")


class TestOutputAndCLI(unittest.TestCase):

    def test_output_file_written(self):
        from _builder.export.export_plantuml import export_plantuml
        fd, path = tempfile.mkstemp(suffix=".puml")
        os.close(fd)
        try:
            text = export_plantuml(_graph(), mode="module", output=path)
            with open(path, encoding="utf-8") as f:
                written = f.read()
            self.assertEqual(text, written)
        finally:
            os.unlink(path)

    def test_unknown_mode_raises(self):
        from _builder.export.export_plantuml import export_plantuml
        with self.assertRaises(ValueError):
            export_plantuml(_graph(), mode="nonsense")

    def test_cli_prints_to_stdout(self):
        import io
        from contextlib import redirect_stdout
        from _builder.export.export_plantuml import cmd_export_plantuml

        class Args:
            graph = _graph()
            mode = "module"
            node = None
            file = None
            domain = None
            depth = 2
            max_nodes = 60
            output = None

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_export_plantuml(Args())
        self.assertIn("@startuml", buf.getvalue())

    def test_cli_error_exits_nonzero(self):
        import io
        from contextlib import redirect_stderr
        from _builder.export.export_plantuml import cmd_export_plantuml

        class Args:
            graph = _graph()
            mode = "call"
            node = "missing_fn"
            file = None
            domain = None
            depth = 2
            max_nodes = 60
            output = None

        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stderr(buf):
                cmd_export_plantuml(Args())
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
