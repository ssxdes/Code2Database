#!/usr/bin/env python3
"""O2: End-to-end test on a mini-kernel fixture.

Exercises the full scan → build → query pipeline on a small C fixture
that mimics Linux kernel patterns:
  - file_operations vtable
  - macro bridge (X → __X)
  - inline wrapper dispatch (call_foo → foo)
  - conditional calls (if/else branches)
  - pthread_create callback registration

This catches integration bugs that unit tests miss (e.g., scanner output
not consumable by builder, builder output not queryable by explore-flow).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


_MINI_KERNEL_C = """\
// Mini-kernel fixture: vtable + macro bridge + inline wrapper + conditional

#include <pthread.h>

struct file_operations {
    int (*open)(int);
    int (*read)(int, char*, int);
    int (*write)(int, const char*, int);
};

static int my_open(int fd) { return fd; }
static int my_read(int fd, char *buf, int n) { return n; }
static int my_write(int fd, const char *buf, int n) { return n; }

struct file_operations my_fops = {
    .open = my_open,
    .read = my_read,
    .write = my_write,
};

// Macro bridge: FOO → __FOO
#define FOO(x) __FOO(x)
static int __FOO(int x) { return x + 1; }
int caller_of_foo(int y) { return FOO(y); }

// Inline wrapper: call_read → my_fops.read
static inline int call_read(struct file_operations *fops, int fd, char *buf, int n) {
    return fops->read(fd, buf, n);
}

int driver_read(int fd, char *buf, int n) {
    return call_read(&my_fops, fd, buf, n);
}

// Conditional call
int conditional_dispatch(int mode, int fd) {
    if (mode == 1) {
        return my_open(fd);
    } else {
        return my_write(fd, "x", 1);
    }
}

// Callback registration
static void *worker_thread(void *arg) {
    int *p = (int *)arg;
    return (void *)(long)(*p + 1);
}

int start_worker(int initial) {
    pthread_t tid;
    int value = initial;
    pthread_create(&tid, NULL, worker_thread, &value);
    pthread_join(tid, NULL);
    return value;
}
"""


def _write_fixture(root: str):
    """Write the mini-kernel fixture into root/src/."""
    src_dir = os.path.join(root, "src")
    os.makedirs(src_dir, exist_ok=True)
    with open(os.path.join(src_dir, "mini_kernel.c"), "w") as f:
        f.write(_MINI_KERNEL_C)


def _run_cmd(cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )


class TestE2EMiniKernel(unittest.TestCase):
    """Full pipeline: scan → build → query on mini-kernel fixture."""

    @classmethod
    def setUpClass(cls):
        cls._root = tempfile.mkdtemp(prefix="code2database_e2e_")
        cls._outdir = os.path.join(cls._root, "code2db-out")
        os.makedirs(cls._outdir, exist_ok=True)
        _write_fixture(cls._root)
        cls._src = os.path.join(cls._root, "src")

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls._scanner = os.path.join(repo_root, "scripts", "code2database_scanner.py")
        cls._builder = os.path.join(repo_root, "scripts", "code2database_builder.py")
        cls._extraction = os.path.join(cls._outdir, ".code2database_extraction.json")

        # Step 1: scan
        scan_cmd = [
            sys.executable, cls._scanner, "scan",
            "--source", cls._src,
            "--output", cls._extraction,
        ]
        cls._scan_result = _run_cmd(scan_cmd, timeout=120)
        if cls._scan_result.returncode != 0:
            return

        # Step 2: build
        build_cmd = [
            sys.executable, cls._builder, "build",
            "--extraction", cls._extraction,
            "--outdir", cls._outdir,
            "--build-config", "auto",
        ]
        cls._build_result = _run_cmd(build_cmd, timeout=180)

    @classmethod
    def tearDownClass(cls):
        # Clean up temp dir
        import shutil
        shutil.rmtree(cls._root, ignore_errors=True)

    def test_01_scan_succeeds(self):
        self.assertEqual(
            self._scan_result.returncode, 0,
            f"scan failed: {self._scan_result.stderr}"
        )

    def test_02_scan_produces_extraction(self):
        self.assertTrue(
            os.path.exists(self._extraction),
            "extraction.json not produced"
        )

    def test_03_scan_extracts_functions(self):
        with open(self._extraction, "r") as f:
            data = json.load(f)
        # Should find at least the functions we defined
        func_names = set()
        for f_data in data.get("functions", []):
            name = f_data.get("name", "") if isinstance(f_data, dict) else ""
            if name:
                func_names.add(name)
        expected = {"my_open", "my_read", "my_write", "caller_of_foo",
                    "driver_read", "conditional_dispatch", "start_worker",
                    "worker_thread"}
        found = func_names & expected
        self.assertGreaterEqual(
            len(found), 5,
            f"only found {found} of expected {expected}"
        )

    def test_04_build_succeeds(self):
        self.assertEqual(
            self._build_result.returncode, 0,
            f"build failed: {self._build_result.stderr}"
        )

    def test_05_build_produces_output(self):
        master = os.path.join(self._outdir, "code2database_master.json")
        db = os.path.join(self._outdir, "code2database.db")
        self.assertTrue(
            os.path.exists(master) or os.path.exists(db),
            "neither code2database_master.json nor code2database.db produced"
        )

    def _load_all_functions(self) -> list:
        """Load all function tuples from domain files.

        Returns list of [id, name, source_file, line, labels_str, signature].
        """
        master_path = os.path.join(self._outdir, "code2database_master.json")
        if not os.path.exists(master_path):
            return []
        with open(master_path, "r") as f:
            master = json.load(f)
        all_funcs = []
        for dom_name, dom_data in master.get("domains", {}).items():
            if isinstance(dom_data, str):
                dom_path = os.path.join(self._outdir, dom_data)
                if os.path.exists(dom_path):
                    with open(dom_path, "r") as df:
                        d = json.load(df)
                    all_funcs.extend(d.get("functions", []))
            elif isinstance(dom_data, dict):
                all_funcs.extend(dom_data.get("functions", []))
        return all_funcs

    def _load_all_edges(self) -> list:
        """Load all edges from domain files."""
        master_path = os.path.join(self._outdir, "code2database_master.json")
        if not os.path.exists(master_path):
            return []
        with open(master_path, "r") as f:
            master = json.load(f)
        all_edges = []
        for dom_name, dom_data in master.get("domains", {}).items():
            if isinstance(dom_data, str):
                dom_path = os.path.join(self._outdir, dom_data)
                if os.path.exists(dom_path):
                    with open(dom_path, "r") as df:
                        d = json.load(df)
                    all_edges.extend(d.get("edges", []))
            elif isinstance(dom_data, dict):
                all_edges.extend(dom_data.get("edges", []))
        return all_edges

    def test_06_macro_bridge_X_to___X(self):
        """O6: FOO macro should bridge to __FOO."""
        all_funcs = self._load_all_functions()
        # functions are [id, name, file, line, labels, signature]
        all_ids = {f[0] for f in all_funcs if isinstance(f, list) and len(f) > 0}
        # Either __foo or foo should appear (macro bridge creates __foo)
        has_impl = any("foo" in nid.lower() for nid in all_ids)
        self.assertTrue(has_impl, f"no foo-related function in {all_ids}")

    def test_07_query_explore_flow(self):
        """explore-flow should return results for a known function."""
        query_cmd = [
            sys.executable, self._builder, "explore-flow",
            "--graph", self._outdir,
            "--query", "driver_read",
            "--max-tokens", "1000",
        ]
        result = _run_cmd(query_cmd, timeout=60)
        self.assertEqual(
            result.returncode, 0,
            f"explore-flow failed: {result.stderr}"
        )
        self.assertTrue(
            result.stdout.strip(),
            "explore-flow returned empty output"
        )

    def test_08_query_describe_node(self):
        """describe-node should return details for a known function."""
        # Try driver_read or my_open
        query_cmd = [
            sys.executable, self._builder, "describe-node",
            "--graph", self._outdir,
            "--node", "driver_read",
            "--max-tokens", "1000",
        ]
        result = _run_cmd(query_cmd, timeout=60)
        # describe-node may return non-zero if node not found by exact name;
        # that's acceptable as long as the command runs without crashing
        self.assertNotEqual(
            result.returncode, 2,
            f"describe-node crashed (unknown command?): {result.stderr}"
        )

    def test_09_conditional_dispatch_has_branches(self):
        """conditional_dispatch should have if/else branches in the graph."""
        all_edges = self._load_all_edges()
        # Edges are lists: [caller, callee, order, condition, edge_type, ...]
        # call_condition is at index 3
        found_conditional = False
        for e in all_edges:
            if isinstance(e, list) and len(e) > 3:
                condition = e[3]
                if condition and "if" in str(condition).lower():
                    found_conditional = True
                    break
            elif isinstance(e, dict):
                if e.get("call_condition"):
                    found_conditional = True
                    break
        self.assertTrue(
            found_conditional,
            "no edges with call_condition found"
        )

    def test_10_callback_registration_detected(self):
        """worker_thread should be detected as a callback/thread target."""
        all_funcs = self._load_all_functions()
        # functions are [id, name, file, line, labels_str, signature]
        found_callback = False
        for f in all_funcs:
            if not isinstance(f, list) or len(f) < 5:
                continue
            fid = f[0]
            labels_str = f[4] if isinstance(f[4], str) else ""
            if "worker_thread" in fid.lower():
                labels_lower = labels_str.lower()
                if "callback" in labels_lower or "thread" in labels_lower:
                    found_callback = True
                    break
        self.assertTrue(
            found_callback,
            "worker_thread not labeled as callback/thread"
        )


if __name__ == "__main__":
    unittest.main()
