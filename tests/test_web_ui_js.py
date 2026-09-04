"""JS-level tests for the embedded Web UI frontend.

The frontend ships as a <script> block inside web_ui._HTML_UI with no
build step and no JS test runner. These tests extract the shipped
functions and exercise them under Node.js against a stubbed cytoscape
instance, guarding the classes of frontend regressions Python-side
tests cannot see:

- syntax errors anywhere in the app block (node --check)
- model→view sync bugs: the canvas silently blanking (dangling edges
  make cytoscape throw during initCy) or rendering edgeless graphs
  (syncCyFromModel used to sync nodes only, never edges — so both
  exploration AND the cycle-highlight toggle lost all edges after the
  first render).

Skipped when Node.js is not on PATH.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

NODE_BIN = shutil.which("node")


def _ui_js() -> str:
    """Return the app <script> block (the last one; earlier ones just
    load cytoscape with a CDN fallback)."""
    from _builder import web_ui
    blocks = re.findall(r"<script>(.*?)</script>", web_ui._HTML_UI, flags=re.S)
    if not blocks:
        raise AssertionError("no <script> blocks found in _HTML_UI")
    return blocks[-1]


def _extract_function(js: str, name: str) -> str:
    """Extract one `function name(...) {...}` declaration (balanced
    braces — the UI functions contain no braces inside strings)."""
    m = re.search(r"\bfunction\s+%s\s*\(" % re.escape(name), js)
    if not m:
        raise AssertionError("function %s not found in UI JS" % name)
    start = js.find("{", m.end())
    if start == -1:
        raise AssertionError("function %s has no body" % name)
    depth = 0
    for i in range(start, len(js)):
        c = js[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return js[m.start():i + 1]
    raise AssertionError("unbalanced braces in function %s" % name)


def _run_node(script: str, check_only: bool = False) -> subprocess.CompletedProcess:
    if not NODE_BIN:
        raise unittest.SkipTest("node not available")
    fd, path = tempfile.mkstemp(suffix=".js", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
        cmd = [NODE_BIN] + (["--check"] if check_only else []) + [path]
        return subprocess.run(cmd, capture_output=True,
                              text=True, timeout=60)
    finally:
        os.unlink(path)


_HARNESS = """
'use strict';
// --- stubs mirroring the globals the extracted functions rely on ---
let cy = null;
let allNodes = {};
let allEdges = {};
let cycleEdges = new Set();
let highlightPath = [];
function runLayout() {}
function applyCommunityColors() {}

function _mkEle(id, data) {
  return {
    _d: data || {},
    id: () => id,
    data: (k) => (data || {})[k],
    _cls: '',
    classes: function (c) { if (c === undefined) return this._cls; this._cls = c; },
  };
}

function _mkCy(initialNodeIds, initialEdgeSpecs) {
  const nodes = (initialNodeIds || []).map((id) => _mkEle(id, { id: id }));
  const edges = (initialEdgeSpecs || []).map(([k, s, t]) =>
    _mkEle(k, { id: k, source: s, target: t }));
  const state = { added: [], removed: [], nodes, edges };
  return {
    nodes: () => nodes,
    edges: () => edges,
    added: state.added,
    removed: state.removed,
    add: (ele) => {
      state.added.push(ele);
      (ele.data.source !== undefined ? edges : nodes)
        .push(_mkEle(ele.data.id, ele.data));
    },
    remove: (sel) => { state.removed.push(sel); },
    getElementById: (id) =>
      edges.find((e) => e.id() === id) || nodes.find((e) => e.id() === id) || _mkEle(id),
  };
}

// --- shipped functions under test ---
%(functions)s

// --- tests ---
const assert = require('assert');
%(tests)s
console.log('ALL_OK');
"""


class TestWebUIJs(unittest.TestCase):
    """Model→view sync behavior of the shipped frontend code."""

    def _run_harness(self, tests: str, function_names):
        js = _ui_js()
        functions = "\n\n".join(_extract_function(js, n) for n in function_names)
        proc = _run_node(_HARNESS % {"functions": functions, "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    def test_app_block_is_valid_js(self):
        """node --check the whole app block — catches syntax errors
        anywhere (the block has no other compile-time validation)."""
        proc = _run_node(_ui_js(), check_only=True)
        self.assertEqual(proc.returncode, 0,
                         "app block has a syntax error:\n%s" % proc.stderr)

    def test_build_cy_elements_skips_dangling_edges(self):
        """/api/neighbors deliberately returns edges to depth-boundary
        nodes that are not in the node set; cytoscape throws on edges
        with nonexistent endpoints, which blanks the canvas."""
        self._run_harness("""
            allNodes = { a: { id: 'a', name: 'A', labels: [] },
                         b: { id: 'b', name: 'B', labels: [] } };
            allEdges = {
              'a->b': { source: 'a', target: 'b', relation: 'INVOKES' },
              'a->zzz': { source: 'a', target: 'zzz', relation: 'INVOKES' },
            };
            const eles = buildCyElements();
            const edgeEles = eles.filter((e) => e.data.source !== undefined);
            assert.strictEqual(edgeEles.length, 1, 'dangling edge must be skipped');
            assert.strictEqual(edgeEles[0].data.id, 'a->b');
            assert.strictEqual(edgeEles[0].data.source, 'a');
            assert.strictEqual(edgeEles[0].data.target, 'b');
        """, ["buildCyElements", "nodeClasses", "edgeClasses"])

    def test_sync_adds_edges_and_classes(self):
        """After the first initCy() the view must keep gaining edges —
        the old sync only added nodes, so exploration rendered
        edgeless graphs and the cycle toggle never restyled anything."""
        self._run_harness("""
            allNodes = { a: { id: 'a', name: 'A', labels: [] },
                         b: { id: 'b', name: 'B', labels: ['API_entry'] } };
            allEdges = { 'a->b': { source: 'a', target: 'b',
                                   relation: 'INVOKES', confidence: 'INFERRED' } };
            cy = _mkCy(['a'], []);
            syncCyFromModel();
            const addedEdges = cy.added.filter((e) => e.data.source !== undefined);
            assert.strictEqual(addedEdges.length, 1, 'edge must be added on sync');
            assert.strictEqual(addedEdges[0].data.source, 'a');
            assert.strictEqual(addedEdges[0].data.target, 'b');
            assert.ok(String(addedEdges[0].classes).includes('inferred'),
                      'edge classes applied');
            const addedNodes = cy.added.filter((e) => e.data.source === undefined);
            assert.strictEqual(addedNodes.length, 1, 'new node added');
            assert.ok(String(addedNodes[0].classes).includes('entry'),
                      'node classes applied');
        """, ["syncCyFromModel", "nodeClasses", "edgeClasses"])

    def test_sync_updates_existing_edge_classes(self):
        """Toggling cycles must restyle edges already on canvas."""
        self._run_harness("""
            allNodes = { a: { id: 'a', name: 'A', labels: [] },
                         b: { id: 'b', name: 'B', labels: [] } };
            allEdges = { 'a->b': { source: 'a', target: 'b', relation: 'INVOKES' } };
            cy = _mkCy(['a', 'b'], [['a->b', 'a', 'b']]);
            cycleEdges.add('a->b');
            syncCyFromModel();
            const e = cy.getElementById('a->b');
            assert.ok(String(e.classes()).includes('cycle-edge'),
                      'existing edge restyled from live cycleEdges');
        """, ["syncCyFromModel", "nodeClasses", "edgeClasses"])

    def test_sync_skips_dangling_edges(self):
        self._run_harness("""
            allNodes = { a: { id: 'a', name: 'A', labels: [] } };
            allEdges = { 'a->zzz': { source: 'a', target: 'zzz' } };
            cy = _mkCy(['a'], []);
            syncCyFromModel();
            assert.strictEqual(
              cy.added.filter((e) => e.data.source !== undefined).length, 0,
              'dangling edge must not be added');
        """, ["syncCyFromModel", "nodeClasses", "edgeClasses"])

    def test_sync_still_removes_stale_nodes(self):
        self._run_harness("""
            allNodes = { a: { id: 'a', name: 'A', labels: [] } };
            allEdges = {};
            cy = _mkCy(['a', 'b'], []);
            syncCyFromModel();
            assert.ok(cy.removed.includes('#b'), 'stale node removed');
        """, ["syncCyFromModel", "nodeClasses", "edgeClasses"])


if __name__ == "__main__":
    unittest.main()
