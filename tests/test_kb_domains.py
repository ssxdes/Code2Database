"""Cross-KB domain queries.

Every kb store is one domain (named via kb_meta, defaulting to the
store directory's parent name). Watching another store registers it
as a queryable domain; kb-query cross=true searches every watched
store's index read-only and labels hits with source_domain. Version
priority applies within other domains too.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.memory.memory_store import MemoryStore
from _builder.kb.kb_index import (
    get_domain_name, set_domain_name, watch_kb, unwatch_kb,
    list_watched_kbs, query_kb, rebuild_kb_index,
)


class _DomainBase(unittest.TestCase):
    def _make_store(self, name):
        d = os.path.join(self.tmp.name, name, "code2db-out")
        os.makedirs(d, exist_ok=True)
        MemoryStore(d)  # creates memory/ + memory.db
        return d


class TestDomainIdentity(_DomainBase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_default_name_from_directory(self):
        d = self._make_store("alpha")
        self.assertEqual(get_domain_name(d), "alpha")

    def test_set_and_get(self):
        d = self._make_store("alpha")
        set_domain_name(d, "team-alpha")
        self.assertEqual(get_domain_name(d), "team-alpha")

    def test_set_rejects_empty(self):
        d = self._make_store("alpha")
        with self.assertRaises(ValueError):
            set_domain_name(d, "  ")

    def test_get_without_store_uses_fallback(self):
        # No kb_index.db yet: the name derives from the directory.
        d = os.path.join(self.tmp.name, "ghost", "code2db-out")
        self.assertEqual(get_domain_name(d), "ghost")


class TestWatchedKbs(_DomainBase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = self._make_store("local")
        self.remote = self._make_store("remote")

    def _seed_remote(self, question, answer, scope="default"):
        store = MemoryStore(self.remote)
        store.add(question, answer, no_merge=True, version_scope=scope)
        rebuild_kb_index(self.remote, verbose=False)

    def test_watch_requires_kb_store(self):
        empty = os.path.join(self.tmp.name, "empty", "code2db-out")
        os.makedirs(empty, exist_ok=True)
        out = watch_kb(self.local, empty)
        self.assertIn("error", out)

    def test_watch_list_remove_cycle(self):
        self._seed_remote("how does locking work", "take the rq lock")
        out = watch_kb(self.local, self.remote)
        self.assertTrue(out["watched"])
        self.assertEqual(out["domain_name"], "remote")
        watched = list_watched_kbs(self.local)
        self.assertEqual(len(watched), 1)
        self.assertEqual(watched[0]["domain_name"], "remote")
        self.assertTrue(unwatch_kb(self.local, self.remote)["removed"])
        self.assertEqual(list_watched_kbs(self.local), [])
        self.assertFalse(unwatch_kb(self.local, self.remote)["removed"])

    def test_cross_query_returns_labeled_domain_hits(self):
        self._seed_remote("how does locking work", "take the rq lock")
        watch_kb(self.local, self.remote)
        results = query_kb(self.local, "locking work", cross=True)
        self.assertTrue(any(r.get("source_domain") == "remote"
                            for r in results), results)
        remote_hits = [r for r in results
                       if r.get("source_domain") == "remote"]
        self.assertEqual(remote_hits[0]["source_kb"], self.remote)
        # Without cross, nothing leaks from other domains.
        plain = query_kb(self.local, "locking work")
        self.assertFalse(any(r.get("source_domain") for r in plain))

    def test_cross_query_honors_version_scope_in_domains(self):
        self._seed_remote("how does locking work", "on main", scope="main")
        self._seed_remote("how does locking work", "on branch",
                          scope="feature/y")
        watch_kb(self.local, self.remote)
        results = query_kb(self.local, "locking work", cross=True,
                           version_scope="feature/y")
        remote = [r for r in results if r.get("source_domain")]
        self.assertEqual(len(remote), 2)
        self.assertEqual(remote[0]["version_scope"], "feature/y")

    def test_cross_query_skips_missing_store(self):
        # Registered but the remote store was deleted: silent skip.
        self._seed_remote("how does locking work", "take the rq lock")
        watch_kb(self.local, self.remote)
        os.remove(os.path.join(self.remote, "kb_index.db"))
        results = query_kb(self.local, "locking work", cross=True)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
