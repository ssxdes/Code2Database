"""Tests for make — one-click env-check + build orchestration."""
import argparse
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
sys.path.insert(0, SCRIPTS_DIR)

from _builder import make_cmd  # noqa: E402


def _ns(**kw):
    """Build an argparse-like namespace for cmd_make."""
    base = dict(source="", graph="code2db-out", lang="auto",
                extraction_backend="auto", compile_commands="",
                clang_args="", profile="", workers=0,
                large_project=False, check=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestDetectLanguages(unittest.TestCase):
    def test_counts_per_family(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src"))
            for f in ("a.c", "b.h", "c.cpp", "d.py", "e.go", "f.s"):
                open(os.path.join(d, "src", f), "w").close()
            counts = make_cmd.detect_languages(d)
            self.assertEqual(counts.get("c"), 2)   # .c + .h
            self.assertEqual(counts.get("cpp"), 1)
            self.assertEqual(counts.get("python"), 1)
            self.assertEqual(counts.get("go"), 1)
            self.assertEqual(counts.get("asm"), 1)

    def test_skips_hidden_and_build_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in (".git", ".cache", "build", "__pycache__",
                        "node_modules", "code2db-out", "normal"):
                os.makedirs(os.path.join(d, sub))
                open(os.path.join(d, sub, "x.c"), "w").close()
            counts = make_cmd.detect_languages(d)
            self.assertEqual(counts.get("c"), 1)  # only normal/x.c


class TestFindCompileCommands(unittest.TestCase):
    def test_root_and_build_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(make_cmd.find_compile_commands(d), "")
            root_cc = os.path.join(d, "compile_commands.json")
            open(root_cc, "w").close()
            self.assertEqual(make_cmd.find_compile_commands(d), root_cc)
            os.remove(root_cc)
            build_cc = os.path.join(d, "build", "compile_commands.json")
            os.makedirs(os.path.dirname(build_cc))
            open(build_cc, "w").close()
            self.assertEqual(make_cmd.find_compile_commands(d), build_cc)


class TestCheckGrammars(unittest.TestCase):
    def test_grammars_detected_via_module_lookup(self):
        with mock.patch.object(make_cmd, "_module_available",
                               side_effect=lambda n: n != "tree_sitter_go"):
            missing = make_cmd.check_grammars({"go": 3, "python": 1})
            self.assertEqual(missing, {"go": ["tree_sitter_go"]})

    def test_asm_has_no_grammar_requirement(self):
        with mock.patch.object(make_cmd, "_module_available",
                               return_value=False):
            missing = make_cmd.check_grammars({"asm": 5})
            self.assertEqual(missing, {})


class TestDecideBackend(unittest.TestCase):
    _CLANG_OK = {"bindings": True, "lib_path": "/usr/lib/libclang-18.so.1"}
    _CLANG_NO = {"bindings": False, "lib_path": ""}

    def test_auto_prefers_clang_for_cc(self):
        backend, notes, errors = make_cmd.decide_backend(
            "auto", {"c": 10}, self._CLANG_OK, {})
        self.assertEqual(backend, "clang")
        self.assertFalse(errors)

    def test_auto_falls_back_with_note(self):
        backend, notes, errors = make_cmd.decide_backend(
            "auto", {"c": 10}, self._CLANG_NO, {})
        self.assertEqual(backend, "tree-sitter")
        self.assertFalse(errors)
        self.assertTrue(any("cgdb" in n for n in notes))

    def test_auto_no_backend_for_cc_is_error(self):
        backend, notes, errors = make_cmd.decide_backend(
            "auto", {"c": 10}, self._CLANG_NO,
            {"c": ["tree_sitter_c", "tree_sitter_cpp"]})
        self.assertEqual(backend, "tree-sitter")
        self.assertTrue(any("no usable C/C++ backend" in e for e in errors))

    def test_forced_clang_without_libclang_is_error(self):
        backend, notes, errors = make_cmd.decide_backend(
            "clang", {"c": 10}, self._CLANG_NO, {})
        self.assertTrue(any("libclang" in e for e in errors))

    def test_forced_clang_non_cc_project_is_note_not_error(self):
        backend, notes, errors = make_cmd.decide_backend(
            "clang", {"go": 4}, self._CLANG_OK, {})
        self.assertEqual(backend, "clang")
        self.assertFalse(errors)
        self.assertTrue(any("only affects C/C++" in n for n in notes))

    def test_go_without_grammar_is_error(self):
        backend, notes, errors = make_cmd.decide_backend(
            "auto", {"go": 4}, self._CLANG_OK,
            {"go": ["tree_sitter_go"]})
        self.assertTrue(any("tree-sitter" in e and "go" in e for e in errors))

    def test_asm_only_needs_nothing(self):
        backend, notes, errors = make_cmd.decide_backend(
            "auto", {"asm": 9}, self._CLANG_NO, {})
        self.assertEqual(backend, "tree-sitter")
        self.assertFalse(errors)


class TestRunEnvCheck(unittest.TestCase):
    def _src(self, files=("a.c",)):
        d = tempfile.mkdtemp()
        for f in files:
            open(os.path.join(d, f), "w").close()
        return d

    def test_missing_source_is_error_without_side_steps(self):
        rep = make_cmd.run_env_check("/nonexistent/xyz", "g-out")
        self.assertFalse(rep["ok"])
        self.assertTrue(any("source directory not found" in e
                            for e in rep["errors"]))

    def test_no_source_files_is_error(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "README.md"), "w").close()
        rep = make_cmd.run_env_check(d, os.path.join(d, "g-out"))
        self.assertFalse(rep["ok"])
        self.assertTrue(any("no source files" in e for e in rep["errors"]))

    def test_cc_project_clang_ok(self):
        d = self._src(("a.c", "b.h"))
        with mock.patch.object(make_cmd, "check_libclang",
                               return_value=TestDecideBackend._CLANG_OK):
            rep = make_cmd.run_env_check(d, os.path.join(d, "g-out"))
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertEqual(rep["backend"], "clang")
        self.assertEqual(rep["lang_counts"], {"c": 2})

    def test_cc_project_clang_missing_warns_compile_commands_absent(self):
        d = self._src(("a.c",))
        with mock.patch.object(make_cmd, "check_libclang",
                               return_value=TestDecideBackend._CLANG_NO), \
             mock.patch.object(make_cmd, "_module_available", return_value=True):
            rep = make_cmd.run_env_check(d, os.path.join(d, "g-out"))
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertEqual(rep["backend"], "tree-sitter")
        self.assertTrue(any("compile_commands.json not found" in w
                            for w in rep["warnings"]))

    def test_compile_commands_discovered_from_source(self):
        d = self._src(("a.c",))
        open(os.path.join(d, "compile_commands.json"), "w").close()
        with mock.patch.object(make_cmd, "check_libclang",
                               return_value=TestDecideBackend._CLANG_OK):
            rep = make_cmd.run_env_check(d, os.path.join(d, "g-out"))
        self.assertEqual(rep["compile_commands"],
                         os.path.join(d, "compile_commands.json"))
        self.assertFalse(any("compile_commands" in w for w in rep["warnings"]))

    def test_explicit_compile_commands_wins(self):
        d = self._src(("a.c",))
        explicit = os.path.join(d, "my_cc.json")
        open(explicit, "w").close()
        with mock.patch.object(make_cmd, "check_libclang",
                               return_value=TestDecideBackend._CLANG_OK):
            rep = make_cmd.run_env_check(d, os.path.join(d, "g-out"),
                                         compile_commands=explicit)
        self.assertEqual(rep["compile_commands"], explicit)

    def test_existing_graph_and_brief_detected(self):
        d = self._src(("a.c",))
        g = os.path.join(d, "g-out")
        os.makedirs(os.path.join(g, "knowledge"))
        open(os.path.join(g, "code2database.db"), "w").close()
        open(os.path.join(g, "knowledge", "brief.json"), "w").close()
        with mock.patch.object(make_cmd, "check_libclang",
                               return_value=TestDecideBackend._CLANG_OK):
            rep = make_cmd.run_env_check(d, g)
        self.assertTrue(rep["existing_graph"])
        self.assertTrue(rep["existing_brief"])
        self.assertTrue(any("preserved" in n for n in rep["notes"]))

    def test_unwritable_graph_dir_is_error(self):
        d = self._src(("a.c",))
        g = os.path.join(d, "ro-out")
        os.makedirs(g)
        os.chmod(g, 0o500)
        try:
            rep = make_cmd.run_env_check(d, g)
            if os.geteuid() == 0:  # root ignores mode bits
                self.skipTest("running as root")
            self.assertFalse(rep["ok"])
            self.assertTrue(any("not writable" in e for e in rep["errors"]))
        finally:
            os.chmod(g, 0o700)

    def test_negative_workers_is_error(self):
        d = self._src(("a.c",))
        with mock.patch.object(make_cmd, "check_libclang",
                               return_value=TestDecideBackend._CLANG_OK):
            rep = make_cmd.run_env_check(d, os.path.join(d, "g-out"),
                                         workers=-1)
        self.assertFalse(rep["ok"])


class TestCmdMake(unittest.TestCase):
    """Orchestration: phase ordering, subprocess commands, failure abort."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.source = os.path.join(self._tmp, "proj")
        os.makedirs(self.source)
        open(os.path.join(self.source, "main.c"), "w").write(
            "int main(void){return 0;}\n")
        self.graph = os.path.join(self._tmp, "g-out")

    def _run(self, extra=None):
        args = _ns(source=self.source, graph=self.graph, **(extra or {}))
        make_cmd.cmd_make(args)

    def _patch_env(self):
        patcher_lib = mock.patch.object(
            make_cmd, "check_libclang",
            return_value=TestDecideBackend._CLANG_OK)
        patcher_mod = mock.patch.object(make_cmd, "_module_available",
                                        return_value=True)
        patcher_lib.start()
        patcher_mod.start()
        self.addCleanup(patcher_lib.stop)
        self.addCleanup(patcher_mod.stop)

    def test_check_runs_no_subprocess(self):
        self._patch_env()
        calls = []
        with mock.patch.object(make_cmd.subprocess, "run",
                               side_effect=lambda c: calls.append(c)):
            self._run({"check": True})
        self.assertEqual(calls, [])

    def test_failed_env_check_builds_nothing(self):
        # Force an env-check error: forced clang backend without libclang.
        calls = []
        with mock.patch.object(
                make_cmd, "check_libclang",
                return_value=TestDecideBackend._CLANG_NO), \
             mock.patch.object(make_cmd.subprocess, "run",
                               side_effect=lambda c: calls.append(c)):
            with self.assertRaises(SystemExit) as cm:
                self._run({"extraction_backend": "clang"})
            self.assertEqual(cm.exception.code, 1)
        self.assertEqual(calls, [])

    def test_step_sequence(self):
        """Full pipeline order: scan, build, then enrichment + exports.

        No condition index in the graph dir (build is mocked) ->
        extract-signals runs its requires() check at execution time,
        decides SKIP, so 11 real subprocess calls.
        """
        self._patch_env()
        calls = []
        with mock.patch.object(
                make_cmd.subprocess, "run",
                side_effect=lambda c: calls.append(c) or SimpleNamespace(returncode=0)):
            self._run()
        names = [c[2] for c in calls]
        self.assertEqual(names, [
            "scan", "build", "value-flow", "data-dep", "ffi-detect",
            "brief-extract", "kb-rebuild-index", "embeddings-build",
            "export-obsidian", "export-html", "profile-health",
        ])
        # scan: [python, scanner.py, scan, ...]
        scan = calls[0]
        self.assertIn("code2database_scanner.py", scan[1])
        self.assertEqual(scan[2], "scan")
        joined = " ".join(scan)
        self.assertIn("--source", joined)
        self.assertIn("extraction.json", joined)
        self.assertIn("--auto-profile", joined)
        self.assertIn("--no-interactive", joined)
        self.assertIn("--extraction-backend clang", joined)
        # build
        build = calls[1]
        self.assertIn("code2database_builder.py", build[1])
        self.assertEqual(build[2], "build")
        # derived edges carry --build
        for idx in (2, 3):
            self.assertIn("--build", calls[idx])
            self.assertIn("--graph", calls[idx])
        # ffi-detect applies to both JSON + SQLite
        self.assertIn("--apply", calls[4])
        self.assertIn("--source", calls[4])
        # exports + report
        self.assertIn("--format", calls[9])
        self.assertIn("vis-network", calls[9])
        self.assertIn("--source", calls[10])

    def test_extract_signals_runs_when_condition_index_has_data(self):
        self._patch_env()
        os.makedirs(self.graph, exist_ok=True)
        with open(os.path.join(self.graph,
                               ".code2database_condition_index.json"), "w") as f:
            f.write('{"node1": [{"condition": "FEATURE_X"}]}')
        calls = []
        with mock.patch.object(
                make_cmd.subprocess, "run",
                side_effect=lambda c: calls.append(c) or SimpleNamespace(returncode=0)):
            self._run()
        names = [c[2] for c in calls]
        self.assertEqual(names, [
            "scan", "build", "value-flow", "data-dep", "extract-signals",
            "ffi-detect", "brief-extract", "kb-rebuild-index",
            "embeddings-build", "export-obsidian", "export-html",
            "profile-health",
        ])
        # extract-signals needs no --build flag (it always writes)
        self.assertNotIn("--build", calls[4])

    def test_extract_signals_skipped_on_empty_condition_index(self):
        self._patch_env()
        os.makedirs(self.graph, exist_ok=True)
        with open(os.path.join(self.graph,
                               ".code2database_condition_index.json"), "w") as f:
            f.write("{}")
        calls = []
        with mock.patch.object(
                make_cmd.subprocess, "run",
                side_effect=lambda c: calls.append(c) or SimpleNamespace(returncode=0)):
            self._run()
        self.assertNotIn("extract-signals", [c[2] for c in calls])

    def test_enrichment_failure_degrades_to_warning(self):
        """value-flow failing must not abort the remaining steps."""
        self._patch_env()
        calls = []

        def _run(cmd):
            calls.append(cmd)
            # Fail only the value-flow invocation (3rd call).
            rc = 2 if len(calls) == 3 else 0
            return SimpleNamespace(returncode=rc)

        with mock.patch.object(make_cmd.subprocess, "run",
                               side_effect=_run):
            self._run()  # must NOT raise SystemExit
        self.assertEqual([c[2] for c in calls],
                         ["scan", "build", "value-flow", "data-dep",
                          "ffi-detect", "brief-extract", "kb-rebuild-index",
                          "embeddings-build", "export-obsidian",
                          "export-html", "profile-health"])

    def test_profile_flag_skips_auto_profile(self):
        self._patch_env()
        calls = []
        with mock.patch.object(
                make_cmd.subprocess, "run",
                side_effect=lambda c: calls.append(c) or SimpleNamespace(returncode=0)):
            self._run({"profile": "p.json"})
        self.assertNotIn("--auto-profile", calls[0])
        self.assertIn("--profile", calls[0])

    def test_compile_commands_forwarded_to_scan(self):
        self._patch_env()
        cc = os.path.join(self.source, "compile_commands.json")
        open(cc, "w").close()
        calls = []
        with mock.patch.object(
                make_cmd.subprocess, "run",
                side_effect=lambda c: calls.append(c) or SimpleNamespace(returncode=0)):
            self._run()
        self.assertIn("--compile-commands", calls[0])
        i = calls[0].index("--compile-commands")
        self.assertEqual(calls[0][i + 1], cc)

    def test_memory_limit_forwarded_to_scan(self):
        """--memory-limit must reach the scanner: on busy machines the
        auto cap (total RAM * 0.8) cancels scans mid-way, and make now
        fails fast on the leftover checkpoint."""
        self._patch_env()
        calls = []
        with mock.patch.object(
                make_cmd.subprocess, "run",
                side_effect=lambda c: calls.append(c)
                or SimpleNamespace(returncode=0)):
            self._run({"memory_limit": 9999})
        self.assertIn("--memory-limit", calls[0])
        self.assertIn("9999", " ".join(calls[0]))

    def test_core_step_failure_aborts_pipeline(self):
        """scan or build failing is fatal: nothing later runs."""
        self._patch_env()
        for fail_at in (1, 2):
            calls = []

            def _fail(cmd, _at=fail_at):
                calls.append(cmd)
                return SimpleNamespace(returncode=0 if len(calls) < _at
                                       else 2)

            with mock.patch.object(make_cmd.subprocess, "run",
                                   side_effect=_fail):
                with self.assertRaises(SystemExit) as cm:
                    self._run()
                self.assertEqual(cm.exception.code, 1)
            self.assertEqual(len(calls), fail_at)

    def test_partial_scan_checkpoint_aborts_make(self):
        """MemoryGuard-canceled scan: scanner exits 0 but leaves a resume
        checkpoint — make must abort before build instead of happily
        building a partial graph and reporting 12/12 OK."""
        import json
        self._patch_env()
        os.makedirs(self.graph, exist_ok=True)
        with open(os.path.join(self.graph, "_scan_checkpoint.json"),
                  "w") as f:
            json.dump({"source_root": self.source, "completed_files": [],
                       "stats": {"functions": 1, "edges": 0,
                                 "stopped_early": True}}, f)
        calls = []
        with mock.patch.object(
                make_cmd.subprocess, "run",
                side_effect=lambda c: calls.append(c)
                or SimpleNamespace(returncode=0)):
            with self.assertRaises(SystemExit) as cm:
                self._run()
            self.assertEqual(cm.exception.code, 1)
        # Only the scan step ran; build and enrichment never started.
        self.assertEqual(len(calls), 1)
        self.assertIn("scan", " ".join(calls[0]))


if __name__ == "__main__":
    unittest.main()


class TestScanVerification(unittest.TestCase):
    """Post-scan sanity check: the scanner exits 0 even when MemoryGuard
    cancels remaining files under system memory pressure (observed on a
    busy WSL1 box: 4-file scan cancelled at 82% RAM). The only durable
    evidence is the resume checkpoint left next to the extraction output
    (removed on clean completion); a partial/empty extraction alone is
    not proof of failure (header-only projects are legitimately empty),
    so it only warrants a warning."""

    def _write_checkpoint(self, graph_dir, stopped_early=True,
                          source_root="/proj"):
        import json
        cp = {"source_root": source_root, "completed_files": [],
              "stats": {"functions": 1, "edges": 0,
                        "stopped_early": stopped_early}}
        path = os.path.join(graph_dir, "_scan_checkpoint.json")
        with open(path, "w") as f:
            json.dump(cp, f)
        return path

    def _write_extraction(self, graph_dir, functions):
        import json
        path = os.path.join(graph_dir, "extraction.json")
        with open(path, "w") as f:
            json.dump({"functions": functions, "edges": []}, f)
        return path

    def _verify(self, source, graph_dir):
        return make_cmd._verify_scan_completed(
            source, graph_dir,
            os.path.join(graph_dir, "extraction.json"))

    def test_checkpoint_left_behind_is_fatal_error(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src"); os.makedirs(src)
            self._write_checkpoint(d, source_root=src)
            self._write_extraction(d, functions=[])
            errors, warnings = self._verify(src, d)
            self.assertTrue(errors,
                            "leftover checkpoint must be a fatal signal")
            self.assertIn("checkpoint", " ".join(errors).lower())

    def test_checkpoint_from_other_source_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src"); os.makedirs(src)
            self._write_checkpoint(d, source_root="/somewhere/else")
            self._write_extraction(d, functions=[{"id": "a"}])
            errors, _ = self._verify(src, d)
            self.assertFalse(errors,
                             "stale checkpoint for another source must not "
                             "poison this run: %r" % errors)

    def test_clean_scan_no_errors_no_warnings(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src"); os.makedirs(src)
            self._write_extraction(d, functions=[{"id": "a"}, {"id": "b"}])
            errors, warnings = self._verify(src, d)
            self.assertFalse(errors)
            self.assertFalse(warnings)

    def test_empty_extraction_only_warns(self):
        """Header-only projects legitimately produce empty extractions:
        must warn (surfaces silent no-op scans) but never fail."""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src"); os.makedirs(src)
            self._write_extraction(d, functions=[])
            errors, warnings = self._verify(src, d)
            self.assertFalse(errors)
            self.assertTrue(warnings)

    def test_missing_extraction_only_warns(self):
        """Compatibility contract: mocked-scan tests and unusual scan
        backends may not write the extraction where make expects it —
        warn loudly, don't abort (that's the next step's job)."""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src"); os.makedirs(src)
            errors, warnings = self._verify(src, d)
            self.assertFalse(errors)
            self.assertTrue(warnings)
