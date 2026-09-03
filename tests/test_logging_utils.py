"""Unit tests for logging_utils.py — structured logging helpers.

Covers: parse_log_level normalization, get_logger namespacing +
NullHandler idempotence, configure_logging (idempotence, force, level,
json formatter, log file), LogContext field attachment, StageTimer
stage_done/stage_failed records, and _JsonFormatter output shape.
"""
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder import logging_utils
from _builder.logging_utils import (
    get_logger, configure_logging, LogContext, StageTimer,
    parse_log_level, is_configured, _JsonFormatter,
)


class _StateSaver(unittest.TestCase):
    """Save/restore the module-global configured state and handlers."""

    def setUp(self):
        self._saved_configured = logging_utils._CONFIGURED
        root = logging.getLogger("callgraph")
        self._saved_handlers = list(root.handlers)
        self._saved_level = root.level
        self._saved_propagate = root.propagate

    def tearDown(self):
        logging_utils._CONFIGURED = self._saved_configured
        root = logging.getLogger("callgraph")
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in self._saved_handlers:
            root.addHandler(h)
        root.setLevel(self._saved_level)
        root.propagate = self._saved_propagate


class TestParseLogLevel(unittest.TestCase):
    def test_none_returns_info(self):
        self.assertEqual(parse_log_level(None), "INFO")

    def test_empty_returns_info(self):
        self.assertEqual(parse_log_level(""), "INFO")

    def test_warn_alias_maps_to_info(self):
        self.assertEqual(parse_log_level("warn"), "INFO")
        self.assertEqual(parse_log_level("WARN"), "INFO")

    def test_valid_levels_pass_through_case_insensitive(self):
        self.assertEqual(parse_log_level("debug"), "DEBUG")
        self.assertEqual(parse_log_level("Info"), "INFO")
        self.assertEqual(parse_log_level("WARNING"), "WARNING")
        self.assertEqual(parse_log_level(" error "), "ERROR")
        self.assertEqual(parse_log_level("CRITICAL"), "CRITICAL")

    def test_garbage_falls_back_to_info(self):
        self.assertEqual(parse_log_level("loud"), "INFO")
        self.assertEqual(parse_log_level("123"), "INFO")


class TestGetLogger(_StateSaver):
    def test_bare_name_namespaced_under_callgraph(self):
        log = get_logger("myscan")
        self.assertTrue(log.name.startswith("callgraph."))

    def test_prefixed_name_unchanged(self):
        log = get_logger("callgraph.builder")
        self.assertEqual(log.name, "callgraph.builder")

    def test_default_is_callgraph_root(self):
        log = get_logger()
        self.assertEqual(log.name, "callgraph")

    def test_null_handler_attached_exactly_once(self):
        log1 = get_logger("dupe")
        log2 = get_logger("dupe")
        nulls = [h for h in log1.handlers if isinstance(h, logging.NullHandler)]
        self.assertEqual(len(nulls), 1)
        self.assertIs(log1, log2)


class TestConfigureLogging(_StateSaver):
    def test_configure_sets_flag_and_level(self):
        logging_utils._CONFIGURED = False
        root = configure_logging(level="DEBUG")
        self.assertTrue(is_configured())
        self.assertEqual(root.level, logging.DEBUG)
        self.assertFalse(root.propagate)
        self.assertEqual(len(root.handlers), 1)

    def test_idempotent_without_force(self):
        logging_utils._CONFIGURED = False
        configure_logging(level="INFO")
        root = logging.getLogger("callgraph")
        n_handlers = len(root.handlers)
        configure_logging(level="ERROR")  # no force → ignored
        self.assertEqual(len(root.handlers), n_handlers)
        self.assertEqual(root.level, logging.INFO)

    def test_force_reconfigures(self):
        logging_utils._CONFIGURED = False
        configure_logging(level="INFO")
        configure_logging(level="ERROR", force=True)
        root = logging.getLogger("callgraph")
        self.assertEqual(root.level, logging.ERROR)
        # reconfigure must not stack duplicate handlers
        self.assertEqual(len(root.handlers), 1)

    def test_json_format_installs_json_formatter(self):
        logging_utils._CONFIGURED = False
        configure_logging(level="INFO", json_format=True)
        root = logging.getLogger("callgraph")
        self.assertIsInstance(root.handlers[0].formatter, _JsonFormatter)

    def test_log_file_handler_added(self):
        logging_utils._CONFIGURED = False
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "out.log")
            configure_logging(level="INFO", log_file=path)
            root = logging.getLogger("callgraph")
            self.assertEqual(len(root.handlers), 2)
            get_logger("test").info("hello file")
            for h in root.handlers:
                h.flush()
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("hello file", content)

    def test_output_goes_to_stderr(self):
        err = io.StringIO()
        logging_utils._CONFIGURED = False
        # The handler binds sys.stderr AT CONSTRUCTION — redirect first so
        # it captures our StringIO, then configure.
        with redirect_stderr(err):
            configure_logging(level="INFO")
            get_logger("streamtest").info("to-stderr-marker")
        self.assertIn("to-stderr-marker", err.getvalue())


class TestJsonFormatter(unittest.TestCase):
    def _format(self, record):
        return json.loads(_JsonFormatter().format(record))

    def test_basic_fields(self):
        log = logging.getLogger("callgraph.jsontest")
        record = log.makeRecord(
            "callgraph.jsontest", logging.INFO, "p", 1, "boom %s", ("x",),
            None)
        payload = self._format(record)
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["logger"], "callgraph.jsontest")
        self.assertEqual(payload["msg"], "boom x")
        self.assertIn("ts", payload)

    def test_extra_fields_included(self):
        log = logging.getLogger("callgraph.jsontest2")
        record = log.makeRecord(
            "callgraph.jsontest2", logging.INFO, "p", 1, "m", (),
            None, extra={"stage": "scan", "items": 7})
        payload = self._format(record)
        self.assertEqual(payload["stage"], "scan")
        self.assertEqual(payload["items"], 7)

    def test_exc_info_serialized(self):
        log = logging.getLogger("callgraph.jsontest3")
        try:
            raise ValueError("kaput")
        except ValueError:
            import sys as _sys
            record = log.makeRecord(
                "callgraph.jsontest3", logging.ERROR, "p", 1,
                "failed", (), _sys.exc_info())
        payload = self._format(record)
        self.assertIn("kaput", payload["exc"])


class TestLogContext(_StateSaver):
    def test_fields_attached_to_records(self):
        captured = []

        class _Cap(logging.Handler):
            def emit(self, record):
                captured.append(record)

        log = get_logger("ctxtest")
        log.setLevel(logging.INFO)  # child defaults inherit WARNING → info dropped
        cap = _Cap()
        log.addHandler(cap)
        try:
            with LogContext(log, stage="vtable_dispatch") as adapter:
                adapter.info("start")
        finally:
            log.removeHandler(cap)
        self.assertEqual(len(captured), 1)
        rec = captured[0]
        self.assertEqual(rec.stage, "vtable_dispatch")
        self.assertEqual(rec.getMessage(), "start")


class TestStageTimer(_StateSaver):
    def _capture_records(self):
        captured = []
        cap = logging.Handler()
        cap.emit = lambda record: captured.append(record)
        log = get_logger("stagetest")
        log.setLevel(logging.INFO)
        log.addHandler(cap)
        return log, captured

    def test_stage_done_emitted_with_ms(self):
        log, captured = self._capture_records()
        try:
            with StageTimer(log, "build", items=10) as timer:
                timer.items = 42
        finally:
            pass
        done = [r for r in captured if r.getMessage() == "stage_done"]
        self.assertEqual(len(done), 1)
        rec = done[0]
        self.assertEqual(rec.stage, "build")
        self.assertGreaterEqual(rec.ms, 0.0)
        self.assertEqual(rec.items, 42)  # updated value, not the initial 10

    def test_stage_start_emitted_first(self):
        log, captured = self._capture_records()
        with StageTimer(log, "scan"):
            pass
        self.assertEqual(captured[0].getMessage(), "stage_start")

    def test_exception_emits_stage_failed_and_propagates(self):
        log, captured = self._capture_records()
        with self.assertRaises(RuntimeError):
            with StageTimer(log, "risky"):
                raise RuntimeError("nope")
        failed = [r for r in captured if r.getMessage() == "stage_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error, "RuntimeError")

    def test_items_only_included_when_int(self):
        log, captured = self._capture_records()
        with StageTimer(log, "noitems", label="x"):
            pass
        done = [r for r in captured if r.getMessage() == "stage_done"][0]
        self.assertFalse(hasattr(done, "items"))
        self.assertEqual(done.label, "x")


if __name__ == "__main__":
    unittest.main()
