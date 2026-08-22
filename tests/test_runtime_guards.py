"""Smoke tests for _builder.runtime_guards.check_runtime_guards().

Covers the 4 runtime-guard detection categories described in the module
docstring (acquire/release, type predicates, identity predicates, lock
state checks). The intent is to verify the module imports cleanly and the
public API does not crash on representative inputs (smoke-test level).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _builder.runtime_guards import (
    check_runtime_guards,
    _strip_condition_wrapper,
    _detect_acquire_release,
    _extract_predicates,
)


class TestRuntimeGuardsImport(unittest.TestCase):
    """Verify the module and its primary entry point are importable."""

    def test_module_imports_cleanly(self):
        import _builder.runtime_guards as rg
        self.assertTrue(hasattr(rg, 'check_runtime_guards'))
        self.assertTrue(callable(rg.check_runtime_guards))

    def test_check_runtime_guards_callable(self):
        result = check_runtime_guards([])
        self.assertIsInstance(result, dict)
        self.assertTrue(result['feasible'])

    def test_strip_condition_wrapper_strips_prefix(self):
        self.assertEqual(_strip_condition_wrapper('if (x > 0)'), 'x > 0')
        self.assertEqual(_strip_condition_wrapper('if(x > 0)'), 'x > 0')
        self.assertEqual(_strip_condition_wrapper('else if (y)'), 'y')
        self.assertEqual(_strip_condition_wrapper('switch (case_val)'), 'case_val')


class TestAcquireReleaseGuards(unittest.TestCase):
    """Guard type 1: acquire/release (mutex_lock/unlock, spin_lock, etc)."""

    def test_mutex_lock_detected_as_acquire(self):
        events = _detect_acquire_release(['if (mutex_lock(&m))'])
        self.assertTrue(any(e['kind'] == 'acquire' and e['function'] == 'mutex_lock'
                            for e in events))

    def test_mutex_unlock_detected_as_release(self):
        events = _detect_acquire_release(['if (mutex_unlock(&m))'])
        self.assertTrue(any(e['kind'] == 'release' for e in events))

    def test_check_runtime_guards_records_unbalanced_release(self):
        result = check_runtime_guards(['if (mutex_unlock(&m))'])
        # An unlock without prior lock is an unbalanced release
        self.assertGreater(len(result['unbalanced_releases']), 0)
        self.assertLess(result['confidence'], 1.0)

    def test_held_at_end_when_acquire_without_release(self):
        result = check_runtime_guards(['if (mutex_lock(&m))'])
        self.assertGreater(len(result['held_at_end']), 0)


class TestTypePredicates(unittest.TestCase):
    """Guard type 2: type predicates (sb_is_blkdev_sb, folio_test_*)."""

    def test_folio_test_predicate_extracted(self):
        preds = _extract_predicates(['if (folio_test_dirty(folio))'])
        self.assertTrue(any(p['predicate'] == 'folio_test_dirty'
                            for p in preds['type_predicates']))

    def test_negated_type_predicate_marked(self):
        preds = _extract_predicates(['if (!sb_is_blkdev_sb(sb))'])
        neg = [p for p in preds['type_predicates'] if p['negated']]
        self.assertTrue(neg)

    def test_type_predicate_contradiction_detected(self):
        result = check_runtime_guards([
            'if (folio_test_dirty(f))',
            'if (!folio_test_dirty(f))',
        ])
        self.assertFalse(result['feasible'])
        self.assertGreater(len(result['contradictions']), 0)


class TestIdentityPredicates(unittest.TestCase):
    """Guard type 3: identity predicates (x != y / x == y)."""

    def test_identity_ne_extracted(self):
        preds = _extract_predicates(['if (bd_holder != sb)'])
        self.assertTrue(any(p['op'] == '!=' and p['var'] == 'bd_holder'
                            for p in preds['identity_predicates']))

    def test_identity_eq_extracted(self):
        preds = _extract_predicates(['if (bd_holder == sb)'])
        self.assertTrue(any(p['op'] == '==' for p in preds['identity_predicates']))

    def test_identity_contradiction_infeasible(self):
        result = check_runtime_guards([
            'if (bd_holder != sb)',
            'if (bd_holder == sb)',
        ])
        self.assertFalse(result['feasible'])

    def test_null_identity_not_recorded(self):
        preds = _extract_predicates(['if (ptr != NULL)'])
        self.assertFalse(preds['identity_predicates'])


class TestLockStateChecks(unittest.TestCase):
    """Guard type 4: lock state checks (mutex_is_locked, spin_is_locked)."""

    def test_mutex_is_locked_extracted(self):
        preds = _extract_predicates(['if (mutex_is_locked(&m))'])
        self.assertTrue(any(p['function'] == 'mutex_is_locked'
                            for p in preds['lock_state_predicates']))

    def test_spin_is_locked_extracted(self):
        preds = _extract_predicates(['if (spin_is_locked(&lock))'])
        self.assertTrue(any(p['function'] == 'spin_is_locked'
                            for p in preds['lock_state_predicates']))

    def test_combined_guards_returned_in_guards_list(self):
        result = check_runtime_guards([
            'if (mutex_lock(&m))',
            'if (mutex_is_locked(&m))',
            'if (sb_is_blkdev_sb(sb))',
            'if (bd_holder != sb)',
        ])
        self.assertGreater(result['guard_count'], 0)


class TestEmptyAndNoContradiction(unittest.TestCase):
    """Edge cases — empty input and contradiction-free conditions."""

    def test_empty_conditions_feasible(self):
        result = check_runtime_guards([])
        self.assertTrue(result['feasible'])
        self.assertEqual(result['reason'], 'no conditions')

    def test_no_contradictions_feasible(self):
        result = check_runtime_guards(['if (mutex_lock(&m))'])
        self.assertTrue(result['feasible'])
        self.assertGreater(result['confidence'], 0.0)


if __name__ == '__main__':
    unittest.main()
