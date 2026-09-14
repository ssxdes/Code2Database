"""Mid-loop batch flush ordering in cmd_build's SQLite export.

cmd_build's non-streaming export path batches function rows and
field/global-access rows independently (threshold 5000). access rows
reference functions(id) with PRAGMA foreign_keys=ON, so an access-batch
flush must not run while referenced functions still sit in the pending
function batch. When a graph interleaves access-qualifying nodes with
file/empty nodes (the common real shape), the access batch reaches its
threshold thousands of nodes AFTER the function batch flushed, holding
un-inserted ids — the flush used to raise IntegrityError and abort the
whole build (observed on a 1.6M-node kernel build at step 2/13).
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts')
BUILDER = os.path.join(SCRIPTS_DIR, 'code2database_builder.py')

_BUILD_MEM_FLAGS = ["--memory-warn-threshold", "0.99",
                    "--memory-crit-threshold", "0.999"]

# Interleave 1 empty node per 6 real ones: the access batch reaches
# 5000 only after node ~6000, while the function batch already flushed
# at 5000 and holds ~1000 un-inserted ids at the access flush point.
_TOTAL = 6200


def _make_extraction(path):
    functions = []
    real = 0
    for i in range(_TOTAL):
        if i % 6 == 5:
            functions.append({
                "id": f"root_mod___cond_{i}", "name": f"<cond{i}>",
                "domain": "root", "source_file": "mod.c",
                "line_number": 0, "is_empty": True,
                "condition": f"CONFIG_{i}",
            })
            continue
        functions.append({
            "id": f"root_mod_fn{i}", "name": f"fn{i}", "domain": "root",
            "source_file": "mod.c", "line_number": i + 1,
            "signature": f"int fn{i}(void)",
            "labels": ["API_entry"] if i == 0 else [],
            "body_text": "return 0;",
            "fields_read": [{"struct_chain": "ops", "field_name": "pool"}],
        })
        real += 1
    assert real >= 5000, real
    extraction = {
        "functions": functions,
        "edges": [],
        "source_root": os.path.dirname(path),
        "files": [{"path": "mod.c", "language": "c"}],
        "stats": {"total_functions": len(functions)},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(extraction, f)
    return real


class TestBuildFlushOrdering(unittest.TestCase):
    def test_access_flush_after_pending_functions(self):
        with tempfile.TemporaryDirectory() as d:
            extraction = os.path.join(d, "extraction.json")
            real = _make_extraction(extraction)
            outdir = os.path.join(d, "out")
            env = dict(os.environ, PYTHONPATH=SCRIPTS_DIR)
            proc = subprocess.run(
                [sys.executable, BUILDER, "build",
                 "--extraction", extraction,
                 "--outdir", outdir,
                 "--storage", "sqlite",
                 "--no-auto-enhance"] + _BUILD_MEM_FLAGS,
                capture_output=True, text=True, timeout=600, env=env)
            self.assertEqual(
                proc.returncode, 0,
                "build crashed:\n%s\n%s" % (proc.stdout[-3000:],
                                            proc.stderr[-3000:]))
            db = os.path.join(outdir, "code2database.db")
            self.assertTrue(os.path.exists(db))
            conn = sqlite3.connect(db)
            try:
                n_funcs = conn.execute(
                    "SELECT COUNT(*) FROM functions").fetchone()[0]
                n_access = conn.execute(
                    "SELECT COUNT(*) FROM field_access").fetchone()[0]
            finally:
                conn.close()
            # 6200 nodes + 1 synthesized file node
            self.assertEqual(n_funcs, _TOTAL + 1)
            self.assertEqual(n_access, real)


if __name__ == "__main__":
    unittest.main()
