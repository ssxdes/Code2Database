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
    from _builder.misc import web_ui

    blocks = re.findall(r"<script>(.*?)</script>", web_ui._HTML_UI, flags=re.S)
    if not blocks:
        raise AssertionError("no <script> blocks found in _HTML_UI")
    return blocks[-1]


def _extract_function(js: str, name: str) -> str:
    """Extract one `[async] function name(...) {...}` declaration
    (balanced braces — the UI functions contain no braces inside
    strings)."""
    m = re.search(r"\b(?:async\s+)?function\s+%s\s*\(" % re.escape(name), js)
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
let expandChildren = {};
let activeNodeId = null;
let navHistory = [];
let syncCyCalls = 0;
let layoutCalls = 0;
let userPositioned = new Set();
function runLayout() { layoutCalls++; }
function placeNewNodesAround() {}  // replaced by the real one when extracted
function applyCommunityColors() {}
function syncCyFromModel() { syncCyCalls++; }
function drawMinimap() {}
function loadNodeDetails(id) {}
function focusNode(id, depth) {}  // breadcrumb crumbs reference it in markup only
function showLoading() {}   // replaced by the real one when extracted
function hideLoading() {}
let cache = { nodes: [], edges: [], focus: null };
// localStorage is NOT stubbed here: tests that exercise persistence
// bring their own in-memory stub (see the line-number roundtrip test).
// Minimal document stub. Elements are cached per id (a function that
// fetches the same element twice must see its own earlier writes) and
// carry classList/setAttribute/textContent for the panel toggles.
const _els = {};
function _mkEl() {
  const el = { innerHTML: '', textContent: '', title: '',
               _cls: new Set(), _attrs: {}, _children: [], _listeners: {} };
  const style = {};
  style.setProperty = (k, v) => { style[k] = v; };
  el.style = style;
  el.classList = {
    add: (c) => el._cls.add(c),
    remove: (c) => el._cls.delete(c),
    toggle: (c) => { el._cls.has(c) ? el._cls.delete(c) : el._cls.add(c);
                     return el._cls.has(c); },
    contains: (c) => el._cls.has(c),
  };
  el.setAttribute = (k, v) => { el._attrs[k] = v; };
  el.getAttribute = (k) => el._attrs[k];
  el.appendChild = (c) => { el._children.push(c); return c; };
  el.addEventListener = (t, fn) => { (el._listeners[t] = el._listeners[t] || []).push(fn); };
  // Fixed geometry — the context-menu clamp test only needs a stable
  // size to reason about edge overflow.
  el.getBoundingClientRect = () => ({ left: 0, top: 0, width: 150, height: 200 });
  return el;
}
const window = { _lastStatsHtml: '', innerWidth: 1280, innerHeight: 800 };
const _docListeners = {};
const document = {
  getElementById: (id) => { if (!_els[id]) _els[id] = _mkEl(); return _els[id]; },
  createElement: (tag) => _mkEl(),
  addEventListener: (t, fn) => { (_docListeners[t] = _docListeners[t] || []).push(fn); },
  removeEventListener: (t, fn) => {
    const l = _docListeners[t] || [];
    const i = l.lastIndexOf(fn);
    if (i !== -1) l.splice(i, 1);
  },
  documentElement: _mkEl(),
  body: _mkEl(),
};
// Computed-style double for the resize handles (panel start width).
const getComputedStyle = (el) =>
  ({ width: '320px', getPropertyValue: () => '' });

function _mkEle(id, data) {
  const el = {
    _d: data || {},
    _pos: { x: 0, y: 0 },
    id: () => id,
    data: (k) => (data || {})[k],
    _cls: '',
    classes: function (c) { if (c === undefined) return this._cls; this._cls = c; },
    position: (p) => { if (p === undefined) return el._pos; Object.assign(el._pos, p); },
  };
  return el;
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

// --- test-local stubs (module scope: shipped code resolves globals
// here, so per-test fetch/navigator/localStorage doubles live at this
// level, NOT inside the async test body) ---
%(preamble)s

// --- tests ---
const assert = require('assert');
// Async wrapper: test bodies may await promises (clipboard round
// trips, fetch stubs); ALL_OK prints only after they settle and a
// rejection fails the run with a nonzero exit code.
(async () => {
%(tests)s
})().then(
  () => console.log('ALL_OK'),
  (err) => { console.error(err); process.exit(1); });
"""


class TestWebUIJs(unittest.TestCase):
    """Model→view sync behavior of the shipped frontend code."""

    def _run_harness(self, tests: str, function_names):
        js = _ui_js()
        functions = "\n\n".join(_extract_function(js, n) for n in function_names)
        proc = _run_node(_HARNESS % {"functions": functions, "preamble": "", "tests": tests})
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

    def test_collapse_removes_exclusive_children(self):
        """Collapse should remove a node's expand-children that no
        other expand path reaches, including their edges."""
        self._run_harness("""
            allNodes = {
              root: { id: 'root', name: 'Root', labels: [] },
              a: { id: 'a', name: 'A', labels: [] },
              b: { id: 'b', name: 'B', labels: [] },
            };
            allEdges = {
              'root->a': { source: 'root', target: 'a', relation: 'INVOKES' },
              'root->b': { source: 'root', target: 'b', relation: 'INVOKES' },
            };
            expandChildren = { root: new Set(['a', 'b']) };
            activeNodeId = 'root';
            collapseNode('root');
            assert.ok(!('a' in allNodes), 'exclusive child a removed');
            assert.ok(!('b' in allNodes), 'exclusive child b removed');
            assert.ok(!('root->a' in allEdges), 'edge to a removed');
            assert.ok(!('root->b' in allEdges), 'edge to b removed');
            assert.ok('root' in allNodes, 'focus target preserved');
            assert.ok(syncCyCalls > 0, 'view refreshed');
        """, ["collapseNode", "_countExpandParents"])

    def test_collapse_preserves_shared_children(self):
        """A child reached by two expand paths must survive collapsing
        only one of them."""
        self._run_harness("""
            allNodes = {
              root: { id: 'root', name: 'Root', labels: [] },
              a: { id: 'a', name: 'A', labels: [] },
              shared: { id: 'shared', name: 'S', labels: [] },
            };
            allEdges = {
              'root->shared': { source: 'root', target: 'shared' },
              'a->shared': { source: 'a', target: 'shared' },
            };
            expandChildren = {
              root: new Set(['shared']),
              a: new Set(['shared']),
            };
            activeNodeId = 'root';
            collapseNode('root');
            // 'shared' is also a child of 'a' — must survive.
            assert.ok('shared' in allNodes, 'shared child preserved');
            assert.ok(expandChildren.root.size === 0, 'root cleared');
            assert.ok(expandChildren.a.has('shared'), 'a still claims shared');
        """, ["collapseNode", "_countExpandParents"])

    def test_collapse_recurses_into_subtree(self):
        """Collapsing a node should also remove grandchildren that the
        child's own expand introduced."""
        self._run_harness("""
            allNodes = {
              root: { id: 'root', name: 'Root', labels: [] },
              child: { id: 'child', name: 'C', labels: [] },
              grandchild: { id: 'grandchild', name: 'G', labels: [] },
            };
            allEdges = {
              'root->child': { source: 'root', target: 'child' },
              'child->grandchild': { source: 'child', target: 'grandchild' },
            };
            expandChildren = {
              root: new Set(['child']),
              child: new Set(['grandchild']),
            };
            activeNodeId = 'root';
            collapseNode('root');
            assert.ok(!('child' in allNodes), 'child removed');
            assert.ok(!('grandchild' in allNodes), 'grandchild removed by recursion');
            assert.ok(!('child' in expandChildren), "child's expand-map cleared");
        """, ["collapseNode", "_countExpandParents"])

    def test_delete_removes_node_and_edges(self):
        """Delete should remove the node, its edges, and cascade-delete
        any leaf nodes that become fully isolated (no callers, no
        callees) as a result."""
        self._run_harness("""
            allNodes = {
              root: { id: 'root', name: 'Root', labels: [] },
              a: { id: 'a', name: 'A', labels: [] },
              leaf: { id: 'leaf', name: 'Leaf', labels: [] },
            };
            allEdges = {
              'root->a': { source: 'root', target: 'a' },
              'a->leaf': { source: 'a', target: 'leaf' },
            };
            activeNodeId = 'root';
            deleteNode('a');
            // 'a' itself is gone
            assert.ok(!('a' in allNodes), 'deleted node removed');
            assert.ok(!('root->a' in allEdges), 'edge to a removed');
            assert.ok(!('a->leaf' in allEdges), 'edge from a removed');
            // 'leaf' was only called by 'a' and calls nothing — now
            // fully isolated (0 in-degree, 0 out-degree) → cascade-deleted
            assert.ok(!('leaf' in allNodes), 'orphaned leaf cascade-deleted');
            // root survives (it's the activeNodeId)
            assert.ok('root' in allNodes, 'active node preserved');
            assert.ok(syncCyCalls > 0, 'view refreshed');
        """, ["deleteNode"])

    def test_delete_preserves_connected_nodes(self):
        """Delete should NOT remove nodes that still have other edges."""
        self._run_harness("""
            allNodes = {
              root: { id: 'root', name: 'Root', labels: [] },
              a: { id: 'a', name: 'A', labels: [] },
              b: { id: 'b', name: 'B', labels: [] },
              c: { id: 'c', name: 'C', labels: [] },
            };
            allEdges = {
              'root->a': { source: 'root', target: 'a' },
              'root->b': { source: 'root', target: 'b' },
              'c->b': { source: 'c', target: 'b' },
            };
            activeNodeId = 'root';
            deleteNode('root');
            // root deleted, root->a and root->b removed
            assert.ok(!('root' in allNodes), 'root removed');
            assert.ok(!('root->a' in allEdges), 'root->a removed');
            assert.ok(!('root->b' in allEdges), 'root->b removed');
            // 'a' is now isolated (no callers, no callees) → cascade-deleted
            assert.ok(!('a' in allNodes), 'isolated a cascade-deleted');
            // 'b' still has caller 'c' → preserved
            assert.ok('b' in allNodes, 'b preserved (still has caller c)');
            // 'c' still has callee 'b' → preserved
            assert.ok('c' in allNodes, 'c preserved (still has callee b)');
            assert.ok('c->b' in allEdges, 'c->b edge preserved');
        """, ["deleteNode"])


if __name__ == "__main__":
    unittest.main()


class TestCodePanelJs(unittest.TestCase):
    """Source-panel behavior: no phantom blank lines between code lines
    (the spans are display:block; a '\n' join inside white-space:pre
    renders an empty line between every pair), and the line-number
    toggle persists."""

    def _run_harness(self, tests: str, function_names, preamble=""):
        js = _ui_js()
        functions = "\n\n".join(_extract_function(js, n) for n in function_names)
        proc = _run_node(_HARNESS % {"functions": functions,
                                     "preamble": preamble, "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    def test_load_code_renders_one_span_per_line_without_newlines(self):
        self._run_harness("""
            await loadCode('n1');
            const html = document.getElementById('code-content').innerHTML;
            assert.strictEqual(
              html,
              '<span class="code-line">int a;</span>'
              + '<span class="code-line">int b;</span>',
              'spans must join with no newline text nodes');
            assert.strictEqual(
              document.getElementById('code-content').style.counterReset,
              'lineno 4', 'counter must start at the source line');
            assert.strictEqual(
              document.getElementById('code-panel-title').textContent,
              'y.c:5');
        """, ["loadCode", "escapeHtml"],
        preamble="""
            async function api() {
              return { code: 'int a;\\nint b;', line: 5, file: '/x/y.c' };
            }
        """)

    def test_line_number_toggle_roundtrip_persists(self):
        self._run_harness("""
            toggleLineNumbers();
            assert.ok(document.getElementById('code-content')
                      .classList.contains('hide-lineno'),
                      'first toggle hides the gutter');
            assert.strictEqual(store['c2d-lineno'], 'off');
            toggleLineNumbers();
            assert.ok(!document.getElementById('code-content')
                      .classList.contains('hide-lineno'),
                      'second toggle restores the gutter');
            assert.strictEqual(store['c2d-lineno'], 'on');
        """, ["toggleLineNumbers"],
        preamble="""
            const store = {};
            const localStorage = {
              getItem: (k) => (k in store ? store[k] : null),
              setItem: (k, v) => { store[k] = v; },
            };
        """)


class TestLayoutPreservationJs(unittest.TestCase):
    """Clicking a node (to read its full name in the details panel)
    and expanding one hop must never reflow an arrangement the user
    dragged into place. Auto-layout runs only while nobody has
    arranged the canvas."""

    def _run_harness(self, tests: str, function_names):
        js = _ui_js()
        functions = "\n\n".join(_extract_function(js, n) for n in function_names)
        proc = _run_node(_HARNESS % {"functions": functions, "preamble": "", "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    def test_click_without_additions_never_relayouts(self):
        self._run_harness("""
            cy = _mkCy(['a', 'b'], [['a->b', 'a', 'b']]);
            userPositioned.add('a');
            allNodes = { a: { id: 'a', name: 'A', labels: [] },
                         b: { id: 'b', name: 'B', labels: [] } };
            layoutCalls = 0;
            syncCyFromModel();
            assert.strictEqual(layoutCalls, 0,
                'a details click that adds nothing must not reflow');
        """, ["syncCyFromModel", "nodeClasses"])

    def test_arranged_canvas_places_new_nodes_not_reflow(self):
        self._run_harness("""
            cy = _mkCy(['a', 'b'], [['a->b', 'a', 'b']]);
            cy.getElementById('a').position({ x: 100, y: 100 });
            cy.getElementById('b').position({ x: 200, y: 120 });
            userPositioned.add('a');
            allNodes = { a: { id: 'a', name: 'A', labels: [] },
                         b: { id: 'b', name: 'B', labels: [] },
                         c: { id: 'c', name: 'C', labels: [] } };
            allEdges = { 'a->b': { source: 'a', target: 'b' },
                         'a->c': { source: 'a', target: 'c' } };
            layoutCalls = 0;
            syncCyFromModel();
            assert.strictEqual(layoutCalls, 0,
                'an arranged canvas must keep its layout');
            const pa = cy.getElementById('a').position();
            assert.strictEqual(pa.x, 100, 'anchor position preserved');
            const pc = cy.getElementById('c').position();
            const dx = pc.x - pa.x, dy = pc.y - pa.y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            assert.ok(Math.abs(dist - 70) < 1e-6,
                'new node placed on a ring around its anchor, dist=' + dist);
        """, ["syncCyFromModel", "placeNewNodesAround", "nodeClasses", "edgeClasses"])

    def test_untouched_canvas_still_auto_layouts(self):
        self._run_harness("""
            cy = _mkCy([], []);
            allNodes = { a: { id: 'a', name: 'A', labels: [] } };
            allEdges = {};
            layoutCalls = 0;
            syncCyFromModel();
            assert.strictEqual(layoutCalls, 1,
                'fresh canvas keeps the auto-layout behavior');
        """, ["syncCyFromModel", "nodeClasses"])


class TestBreadcrumbCollapse(unittest.TestCase):
    """Navigation-trail fold toggle: the breadcrumb floats over the
    canvas at toolbar height, so it must collapse down to a single
    re-open button (and remember the choice) on demand."""
    def _run_harness(self, tests, preamble=""):
        js = _ui_js()
        names = ("renderBreadcrumb", "toggleBreadcrumb", "_crumbToggleHtml",
                 "_syncCrumbToggle", "escapeHtml", "jsAttr")
        functions = "\n\n".join(_extract_function(js, n) for n in names)
        proc = _run_node(_HARNESS % {"functions": functions,
                                     "preamble": preamble, "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    _LS_STUB = """
        const store = {};
        const localStorage = {
          getItem: (k) => (k in store ? store[k] : null),
          setItem: (k, v) => { store[k] = v; },
        };
    """

    def test_history_renders_toggle_and_crumbs(self):
        self._run_harness("""
            allNodes['func1'] = { id: 'func1', name: 'func1' };
            allNodes['func2'] = { id: 'func2', name: 'func2' };
            navHistory.push('func1');
            navHistory.push('func2');
            renderBreadcrumb();
            const bc = document.getElementById('breadcrumb');
            assert.ok(bc.classList.contains('visible'),
                'a non-empty trail must be visible');
            assert.ok(bc.innerHTML.includes('crumb-toggle'),
                'the fold button must ship with the trail');
            assert.ok(bc.innerHTML.includes('func1'));
            assert.ok(bc.innerHTML.includes('func2'));
            assert.ok(bc.innerHTML.includes('focusNode'),
                'earlier crumbs must stay clickable jump-backs');
            assert.ok(!bc.classList.contains('collapsed'));
            const btn = document.getElementById('crumb-toggle');
            assert.strictEqual(btn.getAttribute('aria-pressed'), 'false');
        """)

    def test_toggle_folds_trail_and_persists(self):
        self._run_harness("""
            allNodes['func1'] = { id: 'func1', name: 'func1' };
            navHistory.push('func1');
            renderBreadcrumb();
            toggleBreadcrumb();
            const bc = document.getElementById('breadcrumb');
            assert.ok(bc.classList.contains('collapsed'),
                'one toggle must fold the trail');
            assert.strictEqual(localStorage.getItem('c2d-breadcrumb'), 'off');
            const btn = document.getElementById('crumb-toggle');
            assert.strictEqual(btn.getAttribute('aria-pressed'), 'true');
            assert.strictEqual(btn.getAttribute('aria-label'),
                'Show navigation trail');
            // Round trip: unfold again.
            toggleBreadcrumb();
            assert.ok(!bc.classList.contains('collapsed'));
            assert.strictEqual(localStorage.getItem('c2d-breadcrumb'), 'on');
            assert.strictEqual(btn.getAttribute('aria-pressed'), 'false');
        """, preamble=self._LS_STUB)

    def test_collapsed_render_keeps_fold_and_reopen_button(self):
        self._run_harness("""
            allNodes['func1'] = { id: 'func1', name: 'func1' };
            navHistory.push('func1');
            renderBreadcrumb();
            toggleBreadcrumb();
            // A new focus re-renders while folded.
            navHistory.push('func2');
            allNodes['func2'] = { id: 'func2', name: 'func2' };
            renderBreadcrumb();
            const bc = document.getElementById('breadcrumb');
            assert.ok(bc.classList.contains('collapsed'),
                'a re-render must not lose the fold state');
            assert.ok(bc.innerHTML.includes('crumb-toggle'),
                'the re-open button must stay reachable');
            assert.ok(bc.innerHTML.includes('func2'),
                'the trail content stays in the DOM (CSS hides it)');
            const btn = document.getElementById('crumb-toggle');
            assert.strictEqual(btn.getAttribute('aria-pressed'), 'true');
        """, preamble=self._LS_STUB)

    def test_empty_history_hides_container(self):
        self._run_harness("""
            renderBreadcrumb();
            const bc = document.getElementById('breadcrumb');
            assert.ok(!bc.classList.contains('visible'),
                'no history means no overlay');
            assert.strictEqual(bc.innerHTML, '');
        """)


class TestKeyboardShortcutGuardJs(unittest.TestCase):
    """Shortcut keys must stay idle while a form control has focus —
    the layout dropdown kept focus after a selection, so the next "d"
    flipped the theme mid-interaction."""

    def _run_harness(self, tests):
        js = _ui_js()
        functions = "\n\n".join(_extract_function(js, n)
                                for n in ["_keyTargetBlocksShortcuts"])
        proc = _run_node(_HARNESS % {"functions": functions, "preamble": "", "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    def test_form_controls_block_shortcuts(self):
        self._run_harness("""
            for (const tag of ['INPUT', 'SELECT', 'TEXTAREA']) {
                assert.ok(_keyTargetBlocksShortcuts({ tagName: tag }),
                    tag + ' must block shortcuts');
            }
            assert.ok(_keyTargetBlocksShortcuts(
                { tagName: 'DIV', isContentEditable: true }),
                'editable regions must block shortcuts');
        """)

    def test_plain_targets_keep_shortcuts(self):
        self._run_harness("""
            for (const tag of ['BODY', 'DIV', 'CANVAS']) {
                assert.ok(!_keyTargetBlocksShortcuts({ tagName: tag }),
                    tag + ' must keep shortcuts live');
            }
            assert.ok(!_keyTargetBlocksShortcuts(null),
                'a missing target must not throw');
        """)


class TestContextMenuClampJs(unittest.TestCase):
    """The right-click menu is fixed-positioned at the click point —
    near the right or bottom screen edge it used to overflow and get
    clipped by the viewport. It must clamp itself inside."""

    def _run_harness(self, tests):
        js = _ui_js()
        # closeContextMenu is referenced by the sibling handlers only;
        # the menu builder itself needs no graph functions.
        functions = "\n\n".join(_extract_function(js, n)
                                for n in ["showContextMenu"])
        proc = _run_node(_HARNESS % {"functions": functions, "preamble": "", "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    def test_menu_clamps_inside_the_viewport(self):
        self._run_harness("""
            // Stub geometry: 150x200 menu on a 1280x800 viewport.
            showContextMenu('func1', 1250, 300);   // overflows right
            let m = document.getElementById('ctx-menu');
            assert.strictEqual(m.style.left, '1122px',
                'right-edge open shifts left to 1280-8-150');
            assert.strictEqual(m.style.top, '300px', 'no vertical clamp needed');

            showContextMenu('func1', 100, 700);    // overflows bottom
            assert.strictEqual(m.style.left, '100px', 'no horizontal clamp needed');
            assert.strictEqual(m.style.top, '592px',
                'bottom-edge open shifts up to 800-8-200');

            showContextMenu('func1', 40, 40);      // mid-screen stays put
            assert.strictEqual(m.style.left, '40px');
            assert.strictEqual(m.style.top, '40px');
            assert.strictEqual(m.style.display, 'block');
        """)

    def test_menu_still_lists_every_action(self):
        self._run_harness("""
            showContextMenu('func1', 40, 40);
            const m = document.getElementById('ctx-menu');
            assert.strictEqual(m._children.length, 8,
                'all eight context actions render');
            const labels = m._children.map(c => c.textContent);
            for (const want of ['Focus', 'Collapse All', 'View Code', 'Copy ID']) {
                assert.ok(labels.includes(want), 'missing action: ' + want);
            }
            assert.strictEqual(m.style.display, 'block');
        """)


class TestCopyCodeJs(unittest.TestCase):
    """Copy-to-clipboard takes the button explicitly (the old implicit
    global `event` read is Chromium-only) and must surface a denied
    clipboard on the button instead of failing silently."""
    def _run_harness(self, tests, preamble=""):
        js = _ui_js()
        functions = "\n\n".join(_extract_function(js, n)
                                for n in ["copyCode"])
        proc = _run_node(_HARNESS % {"functions": functions,
                                     "preamble": preamble, "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    def test_copy_roundtrip_flashes_label(self):
        self._run_harness("""
            document.getElementById('code-content').textContent = 'int a;';
            const btn = _mkEl();
            btn.textContent = 'Copy';
            await copyCode(btn);
            assert.deepStrictEqual(copied, ['int a;'],
                'the panel text reaches the clipboard');
            assert.strictEqual(btn.textContent, 'Copied');
            await new Promise(r => setTimeout(r, 1400));
            assert.strictEqual(btn.textContent, 'Copy',
                'the label restores after the flash');
        """, preamble="""
            const copied = [];
            const navigator = { clipboard: {
              writeText: (t) => { copied.push(t); return Promise.resolve(); },
            } };
        """)

    def test_clipboard_denial_surfaces_on_button(self):
        self._run_harness("""
            document.getElementById('code-content').textContent = 'int a;';
            const btn = _mkEl();
            btn.textContent = 'Copy';
            await copyCode(btn);
            assert.strictEqual(btn.textContent, 'Copy failed',
                'a denied clipboard must show on the button');
        """, preamble="""
            const navigator = { clipboard: {
              writeText: () => Promise.reject(new Error('denied')),
            } };
        """)

    def test_empty_panel_short_circuits(self):
        self._run_harness("""
            document.getElementById('code-content').textContent = '';
            await copyCode(null);
            assert.ok(called === false, 'nothing to copy means no clipboard call');
        """, preamble="""
            let called = false;
            const navigator = { clipboard: {
              writeText: () => { called = true; return Promise.resolve(); },
            } };
        """)


class TestFailureSurfacingJs(unittest.TestCase):
    """Backend call failures used to vanish as unhandled rejections —
    the code panel never opened, focus/impact clicks did nothing. Each
    failure path must land somewhere the user can see."""

    _FETCH_500 = """
        const fetch = async () =>
          ({ ok: false, status: 500, statusText: 'boom' });
    """

    def _run_harness(self, tests, function_names, preamble=""):
        js = _ui_js()
        functions = "\n\n".join(_extract_function(js, n) for n in function_names)
        proc = _run_node(_HARNESS % {"functions": functions,
                                     "preamble": preamble, "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    def test_focus_failure_lands_in_stats(self):
        self._run_harness("""
            await focusNode('func1', 1);
            const stats = document.getElementById('stats');
            assert.ok(stats.textContent.indexOf(
                'Failed to load neighborhood: boom') === 0,
                'got: ' + stats.textContent);
        """, ["focusNode", "api"], preamble=self._FETCH_500)

    def test_impact_failure_lands_in_stats(self):
        self._run_harness("""
            await loadImpact('func1');
            const stats = document.getElementById('stats');
            assert.ok(stats.textContent.indexOf('Impact failed: boom') === 0,
                'got: ' + stats.textContent);
        """, ["loadImpact", "api"], preamble=self._FETCH_500)

    def test_code_failure_opens_panel_with_reason(self):
        self._run_harness("""
            await loadCode('func1');
            const panel = document.getElementById('code-panel');
            const empty = document.getElementById('code-empty');
            const content = document.getElementById('code-content');
            assert.ok(panel.classList.contains('visible'),
                'the panel opens even on failure');
            assert.strictEqual(empty.style.display, 'block');
            assert.strictEqual(content.style.display, 'none');
            assert.ok(empty.textContent.indexOf(
                'Failed to load source: boom') === 0,
                'got: ' + empty.textContent);
        """, ["loadCode", "api"], preamble=self._FETCH_500)


class TestPanelWidthPersistenceJs(unittest.TestCase):
    """The drag handles set panel widths but reset on every refresh —
    the settled width must persist (theme-toggle pattern) and restore
    clamped into the drag bounds."""

    _SPECS = """
        const PANEL_SPECS = [
          { handle: 'sidebar-resize', cssVar: '--sidebar-w',
            key: 'c2d-sidebar-w', min: 200, max: 700 },
          { handle: 'code-panel-resize', cssVar: '--code-panel-w',
            key: 'c2d-code-w', min: 300, max: 900 },
        ];
    """
    _LS_STUB = """
        const store = {};
        const localStorage = {
          getItem: (k) => (k in store ? store[k] : null),
          setItem: (k, v) => { store[k] = v; },
        };
    """

    def _run_harness(self, tests, function_names, preamble=""):
        js = _ui_js()
        functions = "\n\n".join(_extract_function(js, n) for n in function_names)
        proc = _run_node(_HARNESS % {"functions": functions,
                                     "preamble": preamble, "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    def test_drag_settles_into_storage(self):
        self._run_harness("""
            initResize('sidebar-resize', '--sidebar-w', 200, 700, 'c2d-sidebar-w');
            const handle = document.getElementById('sidebar-resize');
            handle._listeners['mousedown'][0](
              { preventDefault() {}, stopPropagation() {}, clientX: 500 });
            // Drag left by 100px: start 320 + delta 100 = 420.
            _docListeners['mousemove'].pop()({ clientX: 400 });
            _docListeners['mouseup'].pop()();
            assert.strictEqual(localStorage.getItem('c2d-sidebar-w'), '420px',
                'the settled width persists');
            assert.strictEqual(
              document.documentElement.style['--sidebar-w'], '420px',
                'the CSS variable tracks the drag');
            // A drag far past the bound clamps before storing.
            handle._listeners['mousedown'][0](
              { preventDefault() {}, stopPropagation() {}, clientX: 500 });
            _docListeners['mousemove'].pop()({ clientX: -2000 });
            _docListeners['mouseup'].pop()();
            assert.strictEqual(localStorage.getItem('c2d-sidebar-w'), '700px',
                'out-of-range drags clamp to the max bound');
        """, ["initResize"],
        preamble=self._SPECS + self._LS_STUB)

    def test_saved_widths_restore_clamped(self):
        self._run_harness("""
            store['c2d-sidebar-w'] = '550px';
            store['c2d-code-w'] = '5000px';   // stale storage, must clamp
            applySavedPanelWidths();
            const de = document.documentElement;
            assert.strictEqual(de.style['--sidebar-w'], '550px',
                'an in-range width restores as-is');
            assert.strictEqual(de.style['--code-panel-w'], '900px',
                'an out-of-range width clamps to the max bound');
        """, ["applySavedPanelWidths"],
        preamble=self._SPECS + self._LS_STUB)

    def test_missing_or_garbage_storage_is_ignored(self):
        self._run_harness("""
            store['c2d-sidebar-w'] = 'not-a-number';
            applySavedPanelWidths();
            assert.strictEqual(
              document.documentElement.style['--sidebar-w'], undefined,
                'garbage storage must leave the default width alone');
        """, ["applySavedPanelWidths"],
        preamble=self._SPECS + self._LS_STUB)


class TestSearchDropdownJs(unittest.TestCase):
    """The search dropdown lists 10 of the backend's 30 matches — the
    hidden remainder must be counted, not silently dropped."""

    def _run_harness(self, tests, preamble=""):
        js = _ui_js()
        names = ["search", "api", "hideSearchResults", "shortLoc"]
        functions = "\n\n".join(_extract_function(js, n) for n in names)
        proc = _run_node(_HARNESS % {"functions": functions,
                                     "preamble": preamble, "tests": tests})
        self.assertEqual(
            proc.returncode, 0,
            "node harness failed:\nSTDOUT: %s\nSTDERR: %s" % (proc.stdout, proc.stderr))
        self.assertIn("ALL_OK", proc.stdout)

    @staticmethod
    def _fetch_with(n):
        return """
            const _mkResults = (n) => Array.from({ length: n }, (_, i) => ({
              id: 'f' + i, name: 'fun' + i, labels: [], domain: 'd',
              source_file: 'a.c', line: i + 1 }));
            const fetch = async () =>
              ({ ok: true, json: async () => ({ results: _mkResults(%d) }) });
        """ % n

    def test_hidden_matches_are_counted(self):
        self._run_harness("""
            document.getElementById('search').value = 'fun';
            await search();
            const resEl = document.getElementById('search-results');
            assert.strictEqual(resEl.style.display, 'block');
            assert.strictEqual(resEl._children.length, 11,
                'ten result rows plus the overflow hint');
            const hint = resEl._children[10];
            assert.strictEqual(hint.className, 'sr-more');
            assert.ok(hint.textContent.indexOf('+2 more') === 0,
                'got: ' + hint.textContent);
            assert.ok(hint._listeners === undefined || !hint._listeners.click,
                'the hint is informational, not clickable');
        """, preamble=self._fetch_with(12))

    def test_exact_ten_gets_no_hint(self):
        self._run_harness("""
            document.getElementById('search').value = 'fun';
            await search();
            const resEl = document.getElementById('search-results');
            assert.strictEqual(resEl._children.length, 10,
                'exactly ten results render ten rows and no hint');
            assert.ok(!resEl._children.some(c => c.className === 'sr-more'));
        """, preamble=self._fetch_with(10))
