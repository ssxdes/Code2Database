#!/usr/bin/env python3
"""Comprehensive tests for build_detector.py"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from _detector.build_detector import BuildDetector, BuildInfo, evaluate_pp_condition


class TestCMakeDetection(unittest.TestCase):
    def test_cmake_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "CMakeLists.txt"), "w") as f:
                f.write('cmake_minimum_required(VERSION 3.10)\nproject(test)\n'
                        'add_definitions(-DDEBUG)\n'
                        'target_compile_definitions(mytarget PRIVATE NDEBUG HAVE_CONFIG_H)\n')
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertEqual(info.build_system, "cmake")
            self.assertIn("DEBUG", info.macros)

    def test_cmake_cache_variable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "CMakeLists.txt"), "w") as f:
                f.write('set(MY_VAR "ON" CACHE BOOL "desc")\n')
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertEqual(info.build_system, "cmake")


class TestMakeDetection(unittest.TestCase):
    def test_make_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "Makefile"), "w") as f:
                f.write('CFLAGS += -DFOO -DBAR=1\nall:\n\techo hello\n')
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertEqual(info.build_system, "make")
            self.assertIn("FOO", info.macros)
            self.assertEqual(info.macros["BAR"], "1")

    def test_gnumakefile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "GNUmakefile"), "w") as f:
                f.write('CFLAGS = -DTEST\n')
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertIn("make", info.build_system)


class TestSpecDetection(unittest.TestCase):
    def test_spec_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "test.spec"), "w") as f:
                f.write('Name: test\nVersion: 1.0\n%define with_feature 1\n'
                        '%build\n%configure --enable-feature\n')
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertEqual(info.build_system, "spec")


class TestMesonDetection(unittest.TestCase):
    def test_meson_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "meson.build"), "w") as f:
                f.write("project('test', 'c')\nadd_project_arguments('-DTEST', language: 'c')\n")
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertEqual(info.build_system, "meson")


class TestAutotoolsDetection(unittest.TestCase):
    def test_autotools_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "configure.ac"), "w") as f:
                f.write('AC_INIT([test], [1.0])\nAC_DEFINE([HAVE_FEATURE], [1], [desc])\n')
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertEqual(info.build_system, "autotools")


class TestBazelDetection(unittest.TestCase):
    def test_bazel_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "WORKSPACE"), "w") as f:
                f.write('workspace(name = "test")\n')
            with open(os.path.join(tmpdir, "BUILD"), "w") as f:
                f.write('cc_library(name = "mylib")\n')
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertEqual(info.build_system, "bazel")


class TestNoBuildSystem(unittest.TestCase):
    def test_no_build_system(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bd = BuildDetector()
            info = bd.detect(tmpdir)
            self.assertEqual(info.build_system, "")


class TestEvaluatePPCondition(unittest.TestCase):
    def test_ifdef_defined(self):
        macros = {"FOO": "1", "BAR": ""}
        self.assertTrue(evaluate_pp_condition("defined(FOO)", "if", macros))
        self.assertFalse(evaluate_pp_condition("defined(BAZ)", "if", macros))

    def test_ifdef_directive(self):
        macros = {"FOO": "1"}
        self.assertTrue(evaluate_pp_condition("FOO", "ifdef", macros))
        self.assertFalse(evaluate_pp_condition("BAZ", "ifdef", macros))

    def test_ifndef_directive(self):
        macros = {"FOO": "1"}
        self.assertFalse(evaluate_pp_condition("FOO", "ifndef", macros))
        self.assertTrue(evaluate_pp_condition("BAZ", "ifndef", macros))

    def test_simple_comparison(self):
        macros = {"MODE": "1", "VER": "2"}
        self.assertTrue(evaluate_pp_condition("MODE == 1", "if", macros))
        self.assertFalse(evaluate_pp_condition("VER == 1", "if", macros))
        self.assertTrue(evaluate_pp_condition("VER != 1", "if", macros))

    def test_numeric_gt_lt(self):
        macros = {"LEVEL": "5"}
        self.assertTrue(evaluate_pp_condition("LEVEL > 3", "if", macros))
        self.assertFalse(evaluate_pp_condition("LEVEL < 3", "if", macros))

    def test_no_bindings_conservative(self):
        # Without bindings, should return True (conservative)
        self.assertTrue(evaluate_pp_condition("FOO", "ifdef", {}))
        self.assertTrue(evaluate_pp_condition("BAR == 1", "if", {}))

    def test_no_macros_flag(self):
        macros = {"_no_macros": "1"}
        # Only #if 1 or #if true should be alive
        self.assertTrue(evaluate_pp_condition("1", "if", macros))
        self.assertTrue(evaluate_pp_condition("true", "if", macros))
        self.assertFalse(evaluate_pp_condition("FOO", "ifdef", macros))

    def test_logical_and(self):
        macros = {"A": "1", "B": "1"}
        self.assertTrue(evaluate_pp_condition("defined(A) && defined(B)", "if", macros))
        macros2 = {"A": "1"}
        self.assertFalse(evaluate_pp_condition("defined(A) && defined(B)", "if", macros2))

    def test_logical_or(self):
        macros = {"A": "1"}
        self.assertTrue(evaluate_pp_condition("defined(A) || defined(B)", "if", macros))
        macros2 = {}
        self.assertTrue(evaluate_pp_condition("defined(A) || defined(B)", "if", macros2))  # conservative

    def test_hex_values(self):
        macros = {"FLAG": "0xFF"}
        self.assertTrue(evaluate_pp_condition("FLAG == 255", "if", macros))


class TestBuildInfo(unittest.TestCase):
    def test_build_info_fields(self):
        info = BuildInfo(source_root="/src")
        self.assertEqual(info.source_root, "/src")
        self.assertEqual(info.build_system, "")
        self.assertEqual(info.macros, {})
        self.assertEqual(info.config_files, [])
        self.assertEqual(info.targets, [])
        self.assertEqual(info.include_dirs, [])


if __name__ == "__main__":
    unittest.main()
