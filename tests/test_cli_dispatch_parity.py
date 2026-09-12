"""Parser <-> dispatch parity guard for the builder CLI.

Two wiring defects pass every existing test today:

1. A subcommand registered with ``sub.add_parser(...)`` but missing from
   the ``commands`` dispatch dict inside ``main()`` — argparse accepts
   the invocation, then ``commands.get()`` returns None and the CLI
   crashes with "Unknown command" at runtime.
2. A dict entry whose key has no matching subparser — unreachable from
   the CLI (a ghost handler).

test_skill_manifest pins manifest == parser choices, but nothing pins
dispatch dict == parser choices. This test extracts both sides from the
real source via AST and holds them equal, and additionally verifies
that every handler reference in the dict actually resolves:

- plain names must be module-level defs or imports in the entry script
- ``_lazy(module, attr)`` targets must import and expose ``attr``
- every ``_SKILL_ALIASES`` canonical target must be a real subparser
  (the alias loop already raises at startup, but the canonical side
  stays pinned here so removing a canonical command updates the alias
  map in the same change)
"""
import ast
import importlib
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILDER = os.path.join(REPO, "scripts", "code2database_builder.py")


def _extract():
    """Return (parser_names, dict_names, aliases, dict_node, module_tree)."""
    src = open(BUILDER, encoding="utf-8").read()
    tree = ast.parse(src)

    parser_names = set()
    alias_pairs = {}
    dispatch_keys = set()
    dispatch_node = None

    class V(ast.NodeVisitor):
        def visit_Call(self, node):
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_parser"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                parser_names.add(node.args[0].value)
            self.generic_visit(node)

    V().visit(tree)

    def walk_fn(fn):
        nonlocal parser_names, dispatch_keys
        for stmt in ast.walk(fn):
            if not isinstance(stmt, ast.Assign):
                continue
            if not isinstance(stmt.value, ast.Dict):
                continue
            names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            keys = set()
            for k in stmt.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
            if not keys:
                continue
            if "_SKILL_ALIASES" in names:
                for k, v in zip(stmt.value.keys, stmt.value.values):
                    if (isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                            and isinstance(k.value, str) and isinstance(v.value, str)):
                        alias_pairs[k.value] = v.value
                # Aliases are registered as subparsers by the loop
                # `for _alias, _canonical in _SKILL_ALIASES.items()`
                # (dynamic add_parser call — invisible to literal-based
                # extraction), so they count as parser-side names here.
                parser_names |= set(alias_pairs)
            elif "commands" in names and len(keys) > 100:
                dispatch_keys |= keys
                dispatch_node = stmt.value
        return dispatch_node

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            dispatch_node = walk_fn(node)

    return parser_names, dispatch_keys, alias_pairs, dispatch_node, tree


class TestDispatchParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parser_names, cls.dict_names, cls.alias_pairs, \
            cls.dispatch_node, cls.tree = _extract()

    def test_extraction_found_both_sides(self):
        """Sanity: both the subparsers and the dispatch dict were found."""
        self.assertGreater(len(self.parser_names), 200,
                           "add_parser extraction found too few subcommands")
        self.assertGreater(len(self.dict_names), 200,
                           "dispatch dict extraction found too few entries")
        self.assertGreaterEqual(len(self.alias_pairs), 13,
                                "_SKILL_ALIASES extraction found too few")

    def test_every_subcommand_has_dispatch_entry(self):
        """Parser subcommand without a dict entry = runtime 'Unknown
        command' crash after argparse already accepted the invocation."""
        missing = sorted(self.parser_names - self.dict_names)
        self.assertEqual(
            missing, [],
            "subcommands accepted by argparse but absent from the "
            "dispatch dict (crash at runtime): %s" % missing)

    def test_every_dispatch_entry_is_reachable(self):
        """Dict entry without a subparser is unreachable from the CLI."""
        ghosts = sorted(self.dict_names - self.parser_names)
        self.assertEqual(
            ghosts, [],
            "dispatch dict entries with no matching subcommand "
            "(unreachable): %s" % ghosts)

    def test_alias_canonical_targets_are_subcommands(self):
        """Each alias must point at a canonical subparser that exists."""
        bad = sorted(c for c in self.alias_pairs.values()
                     if c not in self.parser_names)
        self.assertEqual(
            bad, [],
            "alias canonical targets that are not subcommands: %s" % bad)


class TestHandlerReferences(unittest.TestCase):
    """Every handler reference in the dispatch dict must resolve."""

    @classmethod
    def setUpClass(cls):
        cls.parser_names, cls.dict_names, cls.alias_pairs, \
            cls.dispatch_node, cls.tree = _extract()
        src = open(BUILDER, encoding="utf-8").read()
        cls.module = ast.parse(src)

    def _module_level_names(self):
        """Names defined or imported at module level of the entry script."""
        defined = set()
        imported = set()
        for node in self.module.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            defined.add(n.id)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name != "*":
                        imported.add(a.asname or a.name)
        return defined | imported

    def test_plain_handler_names_resolve(self):
        """Dict values that are plain names must be defined/imported at
        module level — a typo NameErrors the moment main() runs."""
        defined = self._module_level_names()
        unresolved = []
        for k, v in zip(self.dispatch_node.keys, self.dispatch_node.values):
            if not isinstance(k, ast.Constant):
                continue
            if isinstance(v, ast.Name) and v.id not in defined:
                unresolved.append("%s -> %s" % (k.value, v.id))
        self.assertEqual(
            unresolved, [],
            "dispatch dict references undefined module-level names: %s"
            % unresolved)

    def test_lazy_targets_resolve(self):
        """Every _lazy(module, attr) pair must import and expose attr —
        a misspelled target only explodes when the command runs."""
        targets = []
        for v in self.dispatch_node.values:
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                    and v.func.id == "_lazy" and len(v.args) == 2
                    and all(isinstance(a, ast.Constant) for a in v.args)):
                targets.append((v.args[0].value, v.args[1].value))
        self.assertGreater(len(targets), 20,
                           "expected a meaningful number of _lazy handlers")
        broken = []
        for module_path, attr in targets:
            try:
                mod = importlib.import_module(module_path)
            except Exception as exc:
                broken.append("%s: import failed (%s)" % (module_path, exc))
                continue
            if not hasattr(mod, attr):
                broken.append("%s: missing attr %s" % (module_path, attr))
        self.assertEqual(
            broken, [],
            "_lazy targets that cannot resolve at runtime: %s" % broken)


if __name__ == "__main__":
    unittest.main()
