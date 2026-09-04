"""Tests for cgdb_config_predicates (L3.5).

Verifies the three-pass pipeline:
  Pass 1: macro universe extraction (clang -E -dM -P)
  Pass 2: range → predicate mapping (regex-based #ifdef tracking)
  Pass 3: AST node → predicate annotation

Test cases mirror cdb 5.2.3 example: #ifdef CONFIG_X / #else / #endif
should produce three predicates:
  - defined(CONFIG_X)
  - !defined(CONFIG_X)
  - unconditional (for code outside the #ifdef)
"""
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.cgdb_config_predicates import (
    ConfigPredicate, ConfigPredicateExtractor,
    UNCONDITIONAL, CONTRADICTORY,
    predicate_id_for, _extract_config_macros, _to_z3_form,
)


class TestPredicateIdFor(unittest.TestCase):
    def test_stable_id_for_same_text(self):
        self.assertEqual(predicate_id_for('defined(CONFIG_X)'),
                         predicate_id_for('defined(CONFIG_X)'))

    def test_different_text_different_id(self):
        self.assertNotEqual(predicate_id_for('defined(CONFIG_X)'),
                            predicate_id_for('defined(CONFIG_Y)'))

    def test_id_fits_in_signed_64bit(self):
        pid = predicate_id_for('defined(CONFIG_X)')
        self.assertLess(pid, 0x7FFF_FFFF_FFFF_FFFF)
        self.assertGreaterEqual(pid, 0)


class TestExtractConfigMacros(unittest.TestCase):
    def test_extracts_defined_macro(self):
        macros = _extract_config_macros('defined(CONFIG_X)')
        self.assertIn('CONFIG_X', macros)

    def test_extracts_multiple_macros(self):
        macros = _extract_config_macros('defined(CONFIG_X) && defined(CONFIG_Y)')
        self.assertIn('CONFIG_X', macros)
        self.assertIn('CONFIG_Y', macros)

    def test_extracts_macro_without_defined(self):
        macros = _extract_config_macros('CONFIG_X && !CONFIG_Y')
        self.assertIn('CONFIG_X', macros)
        self.assertIn('CONFIG_Y', macros)

    def test_excludes_c_keywords(self):
        macros = _extract_config_macros('NULL && TRUE && FALSE')
        self.assertEqual(macros, [])

    def test_empty_condition(self):
        self.assertEqual(_extract_config_macros(''), [])


class TestToZ3Form(unittest.TestCase):
    def test_translates_defined(self):
        z3 = _to_z3_form('defined(CONFIG_X)')
        self.assertIn('CONFIG_X', z3)
        self.assertNotIn('defined', z3)

    def test_translates_and_or_not(self):
        z3 = _to_z3_form('defined(CONFIG_X) && !defined(CONFIG_Y)')
        self.assertIn(' and ', z3)
        self.assertIn(' not ', z3)

    def test_empty_form(self):
        self.assertEqual(_to_z3_form(''), '')


class TestConfigPredicate(unittest.TestCase):
    def test_unconditional_sentinel(self):
        self.assertTrue(UNCONDITIONAL.is_unconditional)
        self.assertEqual(UNCONDITIONAL.text_form, '')

    def test_predicate_dedup_by_text(self):
        p1 = ConfigPredicate(text_form='defined(CONFIG_X)')
        p2 = ConfigPredicate(text_form='defined(CONFIG_X)')
        self.assertEqual(p1.id, p2.id)

    def test_to_record(self):
        p = ConfigPredicate(text_form='defined(CONFIG_X)')
        rec = p.to_record()
        self.assertEqual(rec.text_form, 'defined(CONFIG_X)')
        self.assertEqual(rec.id, p.id)
        self.assertIn('CONFIG_X', rec.config_macros)


class TestPass2RangeToPredicate(unittest.TestCase):
    def setUp(self):
        self.extractor = ConfigPredicateExtractor()

    def test_no_ifdefs_returns_empty(self):
        text = 'int main() { return 0; }'
        ranges = self.extractor.pass2_range_to_predicate(text)
        self.assertEqual(ranges, [])

    def test_single_ifdef(self):
        text = textwrap.dedent("""\
            #ifdef CONFIG_X
            int foo() { return 1; }
            #endif
            """)
        ranges = self.extractor.pass2_range_to_predicate(text)
        self.assertEqual(len(ranges), 1)
        _start, _end, pred = ranges[0]
        self.assertIn('CONFIG_X', pred.text_form)
        self.assertIn('defined', pred.text_form)

    def test_ifdef_else(self):
        text = textwrap.dedent("""\
            #ifdef CONFIG_X
            int foo() { return 1; }
            #else
            int foo() { return 0; }
            #endif
            """)
        ranges = self.extractor.pass2_range_to_predicate(text)
        # Should produce two ranges: one for #ifdef, one for #else
        self.assertEqual(len(ranges), 2)
        preds = [r[2] for r in ranges]
        # First range should be defined(CONFIG_X)
        self.assertTrue(any('defined(CONFIG_X)' in p.text_form for p in preds))
        # Second range should be !defined(CONFIG_X)
        self.assertTrue(any('!defined(CONFIG_X)' in p.text_form
                            or '!(' in p.text_form for p in preds))

    def test_ifdef_elif_endif(self):
        text = textwrap.dedent("""\
            #if defined(CONFIG_X)
            int a = 1;
            #elif defined(CONFIG_Y)
            int a = 2;
            #endif
            """)
        ranges = self.extractor.pass2_range_to_predicate(text)
        self.assertEqual(len(ranges), 2)
        preds = [r[2] for r in ranges]
        # First range: defined(CONFIG_X)
        self.assertTrue(any('CONFIG_X' in p.text_form for p in preds))
        # Second range: !(defined(CONFIG_X)) && defined(CONFIG_Y) or just CONFIG_Y
        self.assertTrue(any('CONFIG_Y' in p.text_form for p in preds))

    def test_if_0_is_contradictory(self):
        text = textwrap.dedent("""\
            #if 0
            int dead_code() { return 0; }
            #endif
            """)
        ranges = self.extractor.pass2_range_to_predicate(text)
        self.assertEqual(len(ranges), 1)
        pred = ranges[0][2]
        self.assertTrue(pred.is_contradictory)

    def test_nested_ifdefs(self):
        text = textwrap.dedent("""\
            #ifdef CONFIG_X
            #ifdef CONFIG_Y
            int both() { return 1; }
            #endif
            #endif
            """)
        ranges = self.extractor.pass2_range_to_predicate(text)
        # Two ranges: inner (CONFIG_X && CONFIG_Y), outer (CONFIG_X)
        self.assertEqual(len(ranges), 2)
        # Sort by range size — the smaller (inner) should be more specific
        ranges.sort(key=lambda r: r[1] - r[0])
        inner_pred = ranges[0][2]
        outer_pred = ranges[1][2]
        # Inner should mention both CONFIG_X and CONFIG_Y
        self.assertIn('CONFIG_X', inner_pred.text_form)
        self.assertIn('CONFIG_Y', inner_pred.text_form)
        # Outer should mention only CONFIG_X
        self.assertIn('CONFIG_X', outer_pred.text_form)


class TestPass3PredicateForRange(unittest.TestCase):
    def setUp(self):
        self.extractor = ConfigPredicateExtractor()

    def test_unconditional_node(self):
        text = 'int main() { return 0; }'
        ranges = self.extractor.pass2_range_to_predicate(text)
        pred = self.extractor.pass3_predicate_for_range(ranges, 0, len(text))
        self.assertTrue(pred.is_unconditional)

    def test_node_in_ifdef(self):
        text = textwrap.dedent("""\
            #ifdef CONFIG_X
            int foo() { return 1; }
            #endif
            """)
        ranges = self.extractor.pass2_range_to_predicate(text)
        # Find the position of 'int foo' inside the #ifdef
        foo_pos = text.find('int foo')
        pred = self.extractor.pass3_predicate_for_range(
            ranges, foo_pos, foo_pos + len('int foo() { return 1; }')
        )
        self.assertIn('CONFIG_X', pred.text_form)

    def test_node_in_nested_ifdef_gets_innermost(self):
        text = textwrap.dedent("""\
            #ifdef CONFIG_X
            #ifdef CONFIG_Y
            int both() { return 1; }
            #endif
            #endif
            """)
        ranges = self.extractor.pass2_range_to_predicate(text)
        both_pos = text.find('int both')
        pred = self.extractor.pass3_predicate_for_range(
            ranges, both_pos, both_pos + len('int both() { return 1; }')
        )
        # Should get the innermost predicate (both CONFIG_X and CONFIG_Y)
        self.assertIn('CONFIG_X', pred.text_form)
        self.assertIn('CONFIG_Y', pred.text_form)


class TestExtractPredicates(unittest.TestCase):
    def setUp(self):
        self.extractor = ConfigPredicateExtractor()

    def test_returns_unconditional_sentinel(self):
        text = 'int main() { return 0; }'
        preds = self.extractor.extract_predicates(text)
        # Should always include UNCONDITIONAL
        ids = [p.id for p in preds]
        self.assertIn(UNCONDITIONAL.id, ids)

    def test_deduplicates_predicates(self):
        text = textwrap.dedent("""\
            #ifdef CONFIG_X
            int a() { return 1; }
            #endif
            #ifdef CONFIG_X
            int b() { return 1; }
            #endif
            """)
        preds = self.extractor.extract_predicates(text)
        # Should have 2 unique predicates: UNCONDITIONAL + defined(CONFIG_X)
        self.assertEqual(len(preds), 2)


class TestAnnotateNodes(unittest.TestCase):
    def setUp(self):
        self.extractor = ConfigPredicateExtractor()

    def test_annotates_nodes_with_predicate_id(self):
        text = textwrap.dedent("""\
            #ifdef CONFIG_X
            int foo() { return 1; }
            #endif
            int bar() { return 0; }
            """)
        foo_pos = text.find('int foo')
        bar_pos = text.find('int bar')
        nodes = [
            {'name': 'foo', 'byte_start': foo_pos,
             'byte_end': foo_pos + len('int foo() { return 1; }')},
            {'name': 'bar', 'byte_start': bar_pos,
             'byte_end': bar_pos + len('int bar() { return 0; }')},
        ]
        preds, annotated = self.extractor.annotate_nodes(text, nodes)
        # foo should have a predicate that mentions CONFIG_X
        foo_node = next(n for n in annotated if n['name'] == 'foo')
        foo_pred = next(p for p in preds if p.id == foo_node['config_predicate_id'])
        self.assertIn('CONFIG_X', foo_pred.text_form)
        # bar should have UNCONDITIONAL predicate
        bar_node = next(n for n in annotated if n['name'] == 'bar')
        bar_pred = next(p for p in preds if p.id == bar_node['config_predicate_id'])
        self.assertTrue(bar_pred.is_unconditional)


class TestPass1MacroUniverse(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.extractor = ConfigPredicateExtractor()

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_extracts_macros_from_source(self):
        # A small source file with a #define
        src = '#define CONFIG_X 1\n#define CONFIG_Y 0\nint main() { return 0; }'
        src_path = os.path.join(self.tmpdir, 'test.c')
        with open(src_path, 'w') as f:
            f.write(src)
        macros = self.extractor.pass1_macro_universe(src_path)
        # Should contain at least CONFIG_X and CONFIG_Y (clang may define
        # many other built-in macros too)
        self.assertIn('CONFIG_X', macros)
        self.assertIn('CONFIG_Y', macros)
        self.assertEqual(macros['CONFIG_X'], '1')
        self.assertEqual(macros['CONFIG_Y'], '0')

    def test_returns_empty_on_missing_file(self):
        macros = self.extractor.pass1_macro_universe('/nonexistent/path.c')
        self.assertEqual(macros, {})


if __name__ == '__main__':
    unittest.main()
