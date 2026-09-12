"""Unit tests for design_doc.py.

design_doc renders a nine-section Markdown design document for one
module (domain exact match, else source-file substring) entirely
from graph data.

Test coverage:
- all nine section headers present
- domain matching and file-substring matching
- implementation model table content
- context view: external callers/dependencies counted and listed
- logical view: hub ranking by internal degree
- interface design: API_entry signatures
- data model: globals/fields read/written aggregation
- algorithm section: deepest internal chain
- security section: memory sinks and async spawns
- test model: test-domain callers and coverage ratio
- runtime view: thread entries and callbacks
- output file writing, missing module error, CLI behaviors
"""
import os
import tempfile
import unittest

from tests.test_quality_checks import _make_quality_graph


def _doc_graph():
    return _make_quality_graph(
        [{"id": "open", "name": "bdev_open", "domain": "lib.bdev",
          "source_file": "/bdev/core.c", "labels": ["API_entry"],
          "signature": "int bdev_open(const char *name)",
          "globals_read": ["g_bdev_list"], "fields_written": ["bdev->state"],
          "thread_entry": False},
         {"id": "process", "name": "bdev_process", "domain": "lib.bdev",
          "source_file": "/bdev/core.c", "labels": [],
          "signature": "void bdev_process(struct bdev *b)",
          "fields_read": ["bdev->state"], "globals_written": ["g_bdev_list"]},
         {"id": "flush", "name": "bdev_flush", "domain": "lib.bdev",
          "source_file": "/bdev/io.c", "labels": ["callback_func"],
          "signature": "void bdev_flush(void *ctx)"},
         {"id": "worker", "name": "bdev_worker", "domain": "lib.bdev",
          "source_file": "/bdev/io.c", "labels": ["thread_processor"],
          "thread_entry": True},
         {"id": "ext_user", "name": "app_use", "domain": "app",
          "source_file": "/app/main.c"},
         {"id": "test_caller", "name": "test_bdev_open", "domain": "ut.bdev",
          "source_file": "/test/test_bdev.c"},
         {"id": "memcpy_fn", "name": "memcpy", "domain": "libc",
          "source_file": ""},
         {"id": "spawned", "name": "bdev_poll", "domain": "lib.bdev",
          "source_file": "/bdev/io.c"}],
        [{"source": "open", "target": "process"},
         {"source": "process", "target": "flush"},
         {"source": "worker", "target": "process"},
         {"source": "ext_user", "target": "open"},
         {"source": "test_caller", "target": "open"},
         {"source": "flush", "target": "memcpy_fn"},
         {"source": "worker", "target": "spawned",
          "confidence": "EXTRACTED", "concurrency": "async_spawn"}],
    )


class TestDesignDoc(unittest.TestCase):

    def setUp(self):
        from _builder.export.design_doc import design_doc
        self.doc = design_doc(_doc_graph(), "lib.bdev")

    def test_all_nine_sections_present(self):
        for i, title in enumerate(
                ["Implementation Model", "Context View", "Logical View",
                 "Interface Design", "Data Model", "Algorithm Implementation",
                 "Security Design", "Developer Test Model", "Runtime View"], 1):
            self.assertIn(f"## {i}. {title}", self.doc)

    def test_title_and_function_count(self):
        self.assertIn("# Design Document: lib.bdev", self.doc)
        self.assertIn("5 functions", self.doc)

    def test_implementation_model_lists_members(self):
        self.assertIn("`bdev_open`", self.doc)
        self.assertIn("`bdev_flush`", self.doc)
        self.assertIn("API_entry", self.doc)

    def test_context_view_external_callers(self):
        self.assertIn("External callers (who calls into the module): 2", self.doc)
        self.assertIn("`app_use`", self.doc)
        self.assertIn("`test_bdev_open`", self.doc)

    def test_logical_view_hub(self):
        # bdev_process has 3 internal callers/links: open, worker (in)
        # and flush (out)
        self.assertIn("`bdev_process` — 3 internal call links", self.doc)

    def test_interface_design_signature(self):
        self.assertIn("int bdev_open(const char *name)", self.doc)

    def test_data_model_aggregates(self):
        self.assertIn("g_bdev_list", self.doc)
        self.assertIn("bdev->state", self.doc)

    def test_algorithm_chain(self):
        self.assertIn("`bdev_open`", self.doc.split("## 6.")[1].split("## 7.")[0])
        chain_section = self.doc.split("## 6.")[1].split("## 7.")[0]
        self.assertIn("→", chain_section)

    def test_security_section_sinks_and_spawns(self):
        sec = self.doc.split("## 7.")[1].split("## 8.")[0]
        self.assertIn("memcpy", sec)
        self.assertIn("`bdev_worker` spawns `bdev_poll`", sec)

    def test_test_model_coverage(self):
        sec = self.doc.split("## 8.")[1].split("## 9.")[0]
        self.assertIn("test_bdev_open", sec)
        self.assertIn("1/5 (20%)", sec)

    def test_runtime_view(self):
        sec = self.doc.split("## 9.")[1]
        self.assertIn("bdev_worker", sec)
        self.assertIn("bdev_flush", sec)


class TestDesignDocMatching(unittest.TestCase):

    def test_file_substring_match(self):
        from _builder.export.design_doc import design_doc
        doc = design_doc(_doc_graph(), "/bdev/io.c")
        self.assertIn("3 functions", doc)
        self.assertIn("matched by file", doc)
        self.assertIn("`bdev_flush`", doc)
        self.assertNotIn("bdev_process", doc.split("## 2.")[0])

    def test_missing_module_raises(self):
        from _builder.export.design_doc import design_doc
        with self.assertRaises(ValueError):
            design_doc(_doc_graph(), "no.such.module")

    def test_output_file_written(self):
        from _builder.export.design_doc import design_doc
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            doc = design_doc(_doc_graph(), "lib.bdev", output=path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), doc)
        finally:
            os.unlink(path)


class TestDesignDocCLI(unittest.TestCase):

    def test_cli_prints_document(self):
        import io
        from contextlib import redirect_stdout
        from _builder.export.design_doc import cmd_design_doc

        class Args:
            graph = _doc_graph()
            module = "lib.bdev"
            output = None

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_design_doc(Args())
        self.assertIn("## 1. Implementation Model", buf.getvalue())

    def test_cli_missing_module_exits(self):
        import io
        from contextlib import redirect_stderr
        from _builder.export.design_doc import cmd_design_doc

        class Args:
            graph = _doc_graph()
            module = "ghost"
            output = None

        with self.assertRaises(SystemExit) as cm:
            with redirect_stderr(io.StringIO()):
                cmd_design_doc(Args())
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
