"""Unit tests for the shared domain-matching helpers.

Matching contract: exact hits always win; separator canonicalization
(-, _, . collapse to one class) is a fallback that resolves only when
it names exactly one real domain, so two domains differing only in
separator style never blend; suggestions order separator variants
first, then child domains, then edit-distance neighbors.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from tests.test_quality_checks import _make_quality_graph


def _graph(domains):
    from _builder.graph.graph_build import _load_full_graph
    return _load_full_graph(_make_quality_graph(
        [{"id": "n%d" % i, "name": "fn_%d" % i, "domain": d,
          "source_file": "/%d.c" % i}
         for i, d in enumerate(domains)],
        []))


class TestExactDomainNodes(unittest.TestCase):

    def test_exact_match_wins(self):
        from _builder.export.domain_match import exact_domain_nodes
        g = _graph(["lib.bdev", "lib_bdev"])
        # Exact input resolves the exact domain even though a
        # separator-class sibling exists.
        self.assertEqual(exact_domain_nodes(g, "lib.bdev"), ["n0"])
        self.assertEqual(exact_domain_nodes(g, "lib_bdev"), ["n1"])

    def test_separator_slip_resolves_when_unique(self):
        from _builder.export.domain_match import exact_domain_nodes
        g = _graph(["libstorage_uio", "other.dom"])
        self.assertEqual(exact_domain_nodes(g, "libstorage-uio"), ["n0"])
        self.assertEqual(exact_domain_nodes(g, "libstorage.uio"), ["n0"])

    def test_ambiguous_separator_match_refuses(self):
        from _builder.export.domain_match import exact_domain_nodes
        g = _graph(["lib.bdev", "lib_bdev"])
        # "lib-bdev" canonicalizes to both — must not blend them.
        self.assertEqual(exact_domain_nodes(g, "lib-bdev"), [])

    def test_no_match_returns_empty(self):
        from _builder.export.domain_match import exact_domain_nodes
        g = _graph(["lib.bdev"])
        self.assertEqual(exact_domain_nodes(g, "ghost"), [])


class TestSubtreeDomainNodes(unittest.TestCase):

    def test_subtree_includes_domain_and_children(self):
        from _builder.export.domain_match import subtree_domain_nodes
        g = _graph(["ublock", "ublock.cli", "ublock.cli.err", "ublock.io",
                    "other"])
        self.assertEqual(subtree_domain_nodes(g, "ublock"),
                         ["n0", "n1", "n2", "n3"])

    def test_subtree_separator_slip(self):
        from _builder.export.domain_match import subtree_domain_nodes
        # "ublock-cli" canonicalizes to the branch root ublock.cli —
        # its subtree, not the sibling ublock.io branch.
        g = _graph(["ublock.cli", "ublock.cli.err", "ublock.io", "other"])
        self.assertEqual(subtree_domain_nodes(g, "ublock-cli"),
                         ["n0", "n1"])

    def test_subtree_of_unknown_is_empty(self):
        from _builder.export.domain_match import subtree_domain_nodes
        g = _graph(["ublock.cli"])
        self.assertEqual(subtree_domain_nodes(g, "ghost"), [])


class TestDomainSuggestions(unittest.TestCase):

    def test_separator_variant_ranks_first(self):
        from _builder.export.domain_match import domain_suggestions
        g = _graph(["libstorage_uio", "ublock.cli"])
        hints = domain_suggestions(g, "libstorage-uio")
        self.assertEqual(hints[0], "libstorage_uio")

    def test_children_hint_parent_slip(self):
        from _builder.export.domain_match import domain_suggestions
        g = _graph(["ublock.cli", "ublock.io", "other"])
        hints = domain_suggestions(g, "ublock")
        self.assertIn("ublock.cli", hints)
        self.assertIn("ublock.io", hints)
        self.assertNotIn("other", hints)

    def test_close_match_hint(self):
        from _builder.export.domain_match import domain_suggestions
        g = _graph(["libstorage_uio", "other"])
        hints = domain_suggestions(g, "libstorage_uo")
        self.assertIn("libstorage_uio", hints)

    def test_limit_respected(self):
        from _builder.export.domain_match import domain_suggestions
        g = _graph(["t.a", "t.b", "t.c", "t.d", "t.e", "t.f", "t.g"])
        self.assertLessEqual(len(domain_suggestions(g, "t", limit=3)), 3)


if __name__ == "__main__":
    unittest.main()


class TestIsTestDomain(unittest.TestCase):

    def test_test_components(self):
        from _builder.export.domain_match import is_test_domain
        for d in ("test", "spdk.test.unit.lib.blob.c", "app.tests.io",
                  "pkg.ut.core", "a.unit.b", "x.unittest", "y.fuzz.case"):
            self.assertTrue(is_test_domain(d), d)

    def test_production_domains_stay(self):
        from _builder.export.domain_match import is_test_domain
        for d in ("spdk.lib.blob", "libstorage_uio", "contest",
                  "attest.verify", "latest"):
            self.assertFalse(is_test_domain(d), d)
