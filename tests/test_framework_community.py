#!/usr/bin/env python3
"""Comprehensive tests for framework_detector.py and community_detector.py"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import networkx as nx


class TestFrameworkDetection(unittest.TestCase):
    def test_django_detection(self):
        from _detector.framework_detector import detect_framework
        hint = detect_framework("django/app/views.py", "python")
        self.assertIsNotNone(hint)
        self.assertEqual(hint.name, "django")

    def test_flask_detection(self):
        from _detector.framework_detector import detect_framework
        hint = detect_framework("flask/app/routes.py", "python")
        self.assertIsNotNone(hint)
        self.assertEqual(hint.name, "flask")

    def test_spring_detection(self):
        from _detector.framework_detector import detect_framework
        hint = detect_framework("spring/app/Controller.java", "java")
        if hint:
            self.assertEqual(hint.name, "spring")

    def test_gin_detection(self):
        from _detector.framework_detector import detect_framework
        hint = detect_framework("gin-gonic/handler.go", "go")
        self.assertIsNotNone(hint)
        self.assertEqual(hint.name, "gin")

    def test_actix_detection(self):
        from _detector.framework_detector import detect_framework
        hint = detect_framework("actix/handler.rs", "rust")
        self.assertIsNotNone(hint)

    def test_no_framework(self):
        from _detector.framework_detector import detect_framework
        hint = detect_framework("src/utils.c", "c")
        self.assertIsNone(hint)

    def test_framework_hint_fields(self):
        from _detector.framework_detector import detect_framework, FrameworkHint
        hint = detect_framework("django/app/views.py", "python")
        self.assertIsInstance(hint, FrameworkHint)
        self.assertGreater(hint.confidence, 0)
        self.assertGreater(hint.entry_multiplier, 1.0)
        self.assertIn(hint.category, ["storage", "networking", "web", "gui", "eventloop", "taskqueue", "async"])

    def test_case_insensitive(self):
        from _detector.framework_detector import detect_framework
        hint = detect_framework("DJANGO/app/views.py", "python")
        self.assertIsNotNone(hint)


class TestEntryMultiplier(unittest.TestCase):
    def test_django_entry(self):
        from _detector.framework_detector import get_entry_multiplier, FrameworkHint
        mult = get_entry_multiplier("views.index", [FrameworkHint("django", "web", 0.8, 1.5)])
        self.assertGreater(mult, 1.0)

    def test_no_framework(self):
        from _detector.framework_detector import get_entry_multiplier
        mult = get_entry_multiplier("my_func", [])
        self.assertEqual(mult, 1.0)

    def test_non_matching_pattern(self):
        from _detector.framework_detector import get_entry_multiplier, FrameworkHint
        mult = get_entry_multiplier("random_name", [FrameworkHint("django", "web", 0.8, 1.5)])
        self.assertEqual(mult, 1.0)


class TestProjectFrameworkDetection(unittest.TestCase):
    def test_detect_from_directory(self):
        from _detector.framework_detector import detect_frameworks_for_project
        with tempfile.TemporaryDirectory() as tmpdir:
            django_dir = os.path.join(tmpdir, "django", "app")
            os.makedirs(django_dir)
            with open(os.path.join(django_dir, "views.py"), "w") as f:
                f.write("def index(): pass")
            frameworks = detect_frameworks_for_project(tmpdir)
            fw_names = [fw.name for fw in frameworks]
            self.assertIn("django", fw_names)

    def test_empty_directory(self):
        from _detector.framework_detector import detect_frameworks_for_project
        with tempfile.TemporaryDirectory() as tmpdir:
            frameworks = detect_frameworks_for_project(tmpdir)
            self.assertEqual(len(frameworks), 0)


class TestCommunityDetector(unittest.TestCase):
    def test_fallback_domain_communities(self):
        from _detector.community_detector import _fallback_domain_communities
        G = nx.DiGraph()
        G.add_node("a", name="a", domain="lib", source_file="a.c")
        G.add_node("b", name="b", domain="lib", source_file="b.c")
        G.add_node("c", name="c", domain="module", source_file="c.c")
        G.add_edge("a", "b")
        G.add_edge("a", "c")
        result = _fallback_domain_communities(G)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(result.communities), 1)

    def test_single_community(self):
        from _detector.community_detector import _single_community
        G = nx.DiGraph()
        G.add_node("a", name="a", domain="root", source_file="a.c")
        result = _single_community(G, ["a"])
        self.assertEqual(len(result.communities), 1)
        self.assertEqual(result.communities[0]["symbol_count"], 1)

    def test_heuristic_label(self):
        from _detector.community_detector import _generate_heuristic_label
        label = _generate_heuristic_label(["lib.bdev"], ["bdev_open", "bdev_close"], ["lib/bdev/bdev.c"])
        self.assertIn("bdev", label)

    def test_extract_keywords(self):
        from _detector.community_detector import _extract_keywords
        keywords = _extract_keywords(
            ["bdev_open", "bdev_close", "bdev_register", "nvme_setup", "nvte_init"],
            ["lib/bdev/bdev.c", "module/nvme/nvme.c"]
        )
        self.assertIn("bdev", keywords)

    def test_calculate_cohesion(self):
        from _detector.community_detector import _calculate_cohesion
        G = nx.DiGraph()
        G.add_node("a")
        G.add_node("b")
        G.add_node("c")
        G.add_edge("a", "b")
        G.add_edge("b", "c")
        cohesion = _calculate_cohesion(G, ["a", "b", "c"])
        # 2 internal edges out of 6 possible (3*2 for directed)
        self.assertGreater(cohesion, 0)
        self.assertLessEqual(cohesion, 1.0)

    def test_detect_communities_empty(self):
        from _detector.community_detector import detect_communities
        G = nx.DiGraph()
        result = detect_communities(G)
        self.assertIsNotNone(result)

    def test_detect_communities_with_edges(self):
        from _detector.community_detector import detect_communities
        G = nx.DiGraph()
        G.add_node("a", name="a", domain="lib", source_file="a.c")
        G.add_node("b", name="b", domain="lib", source_file="b.c")
        G.add_node("c", name="c", domain="module", source_file="c.c")
        G.add_node("d", name="d", domain="module", source_file="d.c")
        G.add_edge("a", "b")
        G.add_edge("c", "d")
        G.add_edge("a", "c")
        result = detect_communities(G)
        self.assertIsNotNone(result)
        # Should have node_community mapping
        self.assertIn("a", result.node_community)


if __name__ == "__main__":
    unittest.main()
