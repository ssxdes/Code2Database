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


class TestModuleModeDomainNoise(unittest.TestCase):
    """The module diagram aggregates architecture: the root domain only
    holds functions no rule could classify, and per-file test domains
    collapse into a single test node."""

    def _graph(self):
        return _make_quality_graph(
            [{"id": "r1", "name": "unclear_fn", "domain": "root"},
             {"id": "b1", "name": "blob_fn", "domain": "spdk.lib.blob"},
             {"id": "n1", "name": "nvme_fn", "domain": "spdk.lib.nvme"},
             {"id": "t1", "name": "t_blob", "domain": "spdk.test.unit.lib.blob.c"},
             {"id": "t2", "name": "t_nvme", "domain": "spdk.test.unit.lib.nvme.c"},
             {"id": "u1", "name": "uio_fn", "domain": "libstorage_uio.cli"}],
            [{"source": "r1", "target": "n1"},        # root noise
             {"source": "b1", "target": "r1"},        # root noise
             {"source": "t1", "target": "b1"},        # test → production
             {"source": "t2", "target": "n1"},        # test → production
             {"source": "t1", "target": "t2"},        # test → test
             {"source": "u1", "target": "b1"}],       # production → production
        )

    def test_root_domain_omitted(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(self._graph(), mode="module")
        self.assertNotIn("mod_root", text)
        self.assertNotIn('"root"', text)
        self.assertIn("unclassified-root edges omitted", text)
        self.assertEqual(text.count("omitted"), 1)  # title only

    def test_test_domains_merge_into_one_node(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(self._graph(), mode="module")
        self.assertIn('rectangle "test" as mod_test', text)
        self.assertNotIn("spdk.test.unit.lib.blob.c", text)
        # Both test→production edges land on the merged node.
        self.assertIn("mod_test --> mod_spdk_lib_blob : 1", text)
        self.assertIn("mod_test --> mod_spdk_lib_nvme : 1", text)
        # test→test disappears entirely.
        self.assertNotIn("mod_test --> mod_test", text)
        # Production edges survive.
        self.assertIn("mod_libstorage_uio_cli --> mod_spdk_lib_blob : 1", text)


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


class TestStructureDomainMatching(unittest.TestCase):
    """Domain arguments resolve exact-first, then the subtree, then a
    concrete hint — a parent domain whose functions all live in child
    domains is a structure request for the whole subtree."""

    def _tree_graph(self):
        return _make_quality_graph(
            [{"id": "c1", "name": "cli_main", "source_file": "/u/cli.c",
              "domain": "ublock.cli"},
             {"id": "c2", "name": "cli_err", "source_file": "/u/cli.c",
              "domain": "ublock.cli.error_inject"},
             {"id": "o1", "name": "io_start", "source_file": "/u/io.c",
              "domain": "ublock.io"},
             {"id": "x1", "name": "other_fn", "source_file": "/o/x.c",
              "domain": "other"}],
            [])

    def test_parent_domain_expands_to_subtree(self):
        from _builder.export.export_plantuml import export_plantuml
        text = export_plantuml(self._tree_graph(), mode="structure",
                               domain="ublock")
        self.assertIn("cli_main", text)
        self.assertIn("cli_err", text)
        self.assertIn("io_start", text)
        self.assertNotIn("other_fn", text)
        self.assertIn("subtree (3 domains)", text)

    def test_exact_domain_with_children_includes_subtree(self):
        from _builder.export.export_plantuml import export_plantuml
        g = self._tree_graph()
        # --domain ublock.cli renders ublock.cli's own function and the
        # child domain's, but not the sibling ublock.io branch.
        text = export_plantuml(g, mode="structure", domain="ublock.cli")
        self.assertIn("cli_main", text)
        self.assertIn("cli_err", text)
        self.assertNotIn("io_start", text)
        self.assertIn("subtree (2 domains)", text)  # ublock.cli + child

    def test_hyphen_variant_resolves(self):
        from _builder.export.export_plantuml import export_plantuml
        g = _make_quality_graph(
            [{"id": "u1", "name": "uio_open", "source_file": "/u/uio.c",
              "domain": "libstorage_uio"},
             {"id": "x1", "name": "other_fn", "source_file": "/o/x.c",
              "domain": "other"}],
            [])
        text = export_plantuml(g, mode="structure", domain="libstorage-uio")
        self.assertIn("uio_open", text)
        self.assertNotIn("other_fn", text)

    def test_unknown_domain_error_lists_hints(self):
        from _builder.export.export_plantuml import export_plantuml
        with self.assertRaises(ValueError) as cm:
            export_plantuml(self._tree_graph(), mode="structure",
                            domain="ublok")
        self.assertIn("no functions matched domain 'ublok'", str(cm.exception))
        self.assertIn("Did you mean:", str(cm.exception))
        self.assertIn("ublock.cli", str(cm.exception))


class TestSingleLineLabels(unittest.TestCase):
    """PlantUML string labels must stay on one source line: bare line
    breaks inside a quoted label split the statement into two lines and
    the diagram stops rendering. C signatures frequently span lines."""

    def test_quote_escapes_line_breaks(self):
        from _builder.export.export_plantuml import _quote
        self.assertEqual(_quote("a\nb"), '"a\\nb"')
        self.assertEqual(_quote("a\r\nb"), '"a\\nb"')
        self.assertEqual(_quote("a\rb"), '"ab"')
        self.assertEqual(_quote('say "hi"\\ok'), '"say \\"hi\\"\\\\ok"')

    def test_truncate_flattens_line_breaks_and_tabs(self):
        from _builder.export.export_plantuml import _truncate
        self.assertEqual(_truncate("static int\nfoo(int a,\n\tint b)"),
                         "static int foo(int a, int b)")
        self.assertEqual(_truncate("a\r\nb"), "a b")

    def test_multiline_signature_stays_on_one_line(self):
        from _builder.export.export_plantuml import export_plantuml
        g = _make_quality_graph(
            [{"id": "f", "name": "cmp_int", "source_file": "/bdev/bdev.c",
              "domain": "lib.bdev",
              "signature": "static int\ncmp_int(int a,\n         int b)"},
             {"id": "g", "name": "cmp_long", "source_file": "/bdev/bdev.c",
              "domain": "lib.bdev",
              "signature": "static int\r\ncmp_long(int a, int b)"}],
            [])
        text = export_plantuml(g, mode="structure", file="/bdev/bdev.c")
        for line in text.splitlines():
            if "cmp_int" in line or "cmp_long" in line:
                self.assertTrue(line.lstrip().startswith("rectangle"), line)
                self.assertIn('"', line)
                self.assertIn('" as fn_', line)
        self.assertIn('rectangle "static int cmp_int(int a, int b)" as fn_',
                      text)
        self.assertIn('rectangle "static int cmp_long(int a, int b)" as fn_',
                      text)

    def test_multiline_condition_label_flattened(self):
        from _builder.export.export_plantuml import export_plantuml
        g = _make_quality_graph(
            [{"id": "a", "name": "f1"}, {"id": "b", "name": "f2"}],
            [{"source": "a", "target": "b",
              "call_condition": "if (a &&\n    b)"}],
        )
        text = export_plantuml(g, mode="call", node="f1")
        arrow = [ln for ln in text.splitlines() if "-->" in ln][0]
        self.assertIn("if (a && b)", arrow)
        self.assertEqual(len([ln for ln in text.splitlines() if "if (a" in ln]), 1)


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
