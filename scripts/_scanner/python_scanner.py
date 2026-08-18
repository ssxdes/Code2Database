#!/usr/bin/env python3
"""Python scanner for invocation graph extraction using tree-sitter."""

import os
import re
from _scanner.base import BaseScanner
from _scanner.utils import check_python_version

try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser
    _HAS_PYTHON = True
except ImportError:
    _HAS_PYTHON = False


class PythonTreeSitterScanner(BaseScanner):
    def __init__(self):
        if not _HAS_PYTHON:
            raise ImportError("Python scanner requires tree-sitter-python. Install: pip install tree-sitter-python")
        self.lang = Language(tspython.language())
        self.parser = Parser(self.lang)
        # Set by scanner factory from profile project_boundaries.non_api_paths.
        # Functions defined in these paths are never tagged API_entry.
        self._non_api_paths = []

    def _parse(self, source_bytes: bytes):
        return self.parser.parse(source_bytes)

    def _extract(self, tree, source_bytes: bytes, filepath: str,
                 source_root: str, domain: str):
        functions = []
        edges = []
        import_edges = []
        rel_path = os.path.relpath(filepath, source_root)
        self._current_source_file = rel_path

        # Extract import statements
        self._extract_imports(tree.root_node, source_bytes, filepath,
                              source_root, domain, import_edges)

        # Walk module for functions, classes, and call edges
        self._walk_module(tree.root_node, source_bytes, filepath, source_root,
                          domain, functions, edges, class_name="")

        return functions, edges, import_edges

    # ------------------------------------------------------------------
    # Import extraction
    # ------------------------------------------------------------------

    def _extract_imports(self, root_node, source_bytes, filepath,
                         source_root, domain, import_edges):
        """Walk AST for import_statement and import_from_statement nodes."""
        rel_path = os.path.relpath(filepath, source_root)
        file_id = domain.replace(".", "_") + "_" + self._normalize_name(rel_path).lower()

        stack = [root_node]
        while stack:
            node = stack.pop()
            if node.type == 'import_statement':
                # import foo, bar.baz
                for child in node.children:
                    if child.type == 'dotted_name':
                        mod_name = self._node_text(child, source_bytes)
                        import_edges.append({
                            "source": file_id,
                            "target": mod_name.lower(),
                            "relation": "IMPORTS",
                            "source_file": rel_path,
                        })
                    elif child.type == 'aliased_import':
                        name_node = child.child_by_field_name('name')
                        if name_node:
                            mod_name = self._node_text(name_node, source_bytes)
                            import_edges.append({
                                "source": file_id,
                                "target": mod_name.lower(),
                                "relation": "IMPORTS",
                                "source_file": rel_path,
                            })
            elif node.type == 'import_from_statement':
                # from foo.bar import baz, qux as q
                module_name = ""
                for child in node.children:
                    if child.type == 'dotted_name':
                        module_name = self._node_text(child, source_bytes)
                        break
                if module_name:
                    import_edges.append({
                        "source": file_id,
                        "target": module_name.lower(),
                        "relation": "IMPORTS",
                        "source_file": rel_path,
                    })
                # Also add edges for each imported name
                for child in node.children:
                    if child.type == 'dotted_name':
                        # Skip the module dotted_name (already handled above)
                        # Only the first dotted_name is the module source
                        continue
                    if child.type == 'identifier':
                        imported_name = self._node_text(child, source_bytes)
                        full_target = f"{module_name}.{imported_name}" if module_name else imported_name
                        import_edges.append({
                            "source": file_id,
                            "target": full_target.lower(),
                            "relation": "IMPORTS",
                            "source_file": rel_path,
                        })
                    elif child.type == 'aliased_import':
                        name_node = child.child_by_field_name('name')
                        if name_node:
                            imported_name = self._node_text(name_node, source_bytes)
                            full_target = f"{module_name}.{imported_name}" if module_name else imported_name
                            import_edges.append({
                                "source": file_id,
                                "target": full_target.lower(),
                                "relation": "IMPORTS",
                                "source_file": rel_path,
                            })
            # Don't recurse into function bodies for imports
            if node.type not in ('function_definition', 'class_definition'):
                for child in reversed(node.children):
                    stack.append(child)

    # ------------------------------------------------------------------
    # Module walk
    # ------------------------------------------------------------------

    def _walk_module(self, node, source_bytes, filepath, source_root,
                     domain, functions, edges, class_name=""):
        for child in node.children:
            if child.type == 'function_definition':
                self._process_py_func(child, source_bytes, filepath, source_root,
                                      domain, functions, edges, class_name)
            elif child.type == 'class_definition':
                self._process_py_class(child, source_bytes, filepath, source_root,
                                       domain, functions, edges)
                # Also walk into the class body for method definitions
                cls_name_node = next((c for c in child.children if c.type == 'identifier'), None)
                cls_name = self._node_text(cls_name_node, source_bytes) if cls_name_node else ""
                self._walk_module(child, source_bytes, filepath, source_root,
                                  domain, functions, edges, class_name=cls_name)
            elif child.type == 'decorated_definition':
                # Collect decorator info before processing the decorated entity
                decorators = self._extract_decorators(child, source_bytes)
                for sub in child.children:
                    if sub.type == 'function_definition':
                        self._process_py_func(sub, source_bytes, filepath, source_root,
                                              domain, functions, edges, class_name,
                                              decorators=decorators)
                    elif sub.type == 'class_definition':
                        self._process_py_class(sub, source_bytes, filepath, source_root,
                                               domain, functions, edges,
                                               decorators=decorators)
                        cls_name_node = next((c for c in sub.children if c.type == 'identifier'), None)
                        cls_name = self._node_text(cls_name_node, source_bytes) if cls_name_node else ""
                        self._walk_module(sub, source_bytes, filepath, source_root,
                                          domain, functions, edges, class_name=cls_name)
            elif child.type == 'block':
                # class_definition > block contains method definitions
                self._walk_module(child, source_bytes, filepath, source_root,
                                  domain, functions, edges, class_name)

    # ------------------------------------------------------------------
    # Class processing
    # ------------------------------------------------------------------

    def _process_py_class(self, node, source_bytes, filepath, source_root,
                          domain, functions, edges, decorators=None):
        """Extract a class node and IMPLEMENTS edges for inheritance."""
        cls_name_node = next((c for c in node.children if c.type == 'identifier'), None)
        cls_name = self._node_text(cls_name_node, source_bytes) if cls_name_node else ""
        if not cls_name:
            return

        cls_line = node.start_point[0] + 1
        rel_path = os.path.relpath(filepath, source_root)
        cls_id = self._make_func_id(domain, cls_name)

        # Build inheritance text for signature
        inheritance_text = ""
        parent_classes = []
        for child in node.children:
            if child.type == 'argument_list':
                inheritance_text = self._node_text(child, source_bytes)
                for arg in child.children:
                    if arg.type in ('identifier', 'type_identifier', 'attribute', 'dotted_name'):
                        parent_name = self._node_text(arg, source_bytes)
                        parent_classes.append(parent_name)
                    elif arg.type == 'keyword_argument':
                        # e.g., metaclass=ABCMeta — skip for inheritance
                        pass

        # Labels
        labels = ["class"]
        if decorators:
            for dec in decorators:
                labels.append(f"@{dec}")
            if "staticmethod" in decorators:
                labels.append("static_class")
            if "abstractmethod" in decorators or "abstract" in decorators:
                labels.append("abstract_class")

        # Add class node to functions list
        functions.append({
            "id": cls_id, "name": cls_name,
            "source_file": rel_path, "line": cls_line, "domain": domain,
            "labels": labels, "is_empty": False,
            "api_constraints": "",
            "body_text": self._node_text(node, source_bytes),
            "signature": "class " + cls_name + ("(" + inheritance_text + ")" if inheritance_text else ""),
            "params": [],
            "local_vars": [],
            "callee_args": [],
            "condition_vars": [],
            "node_type": "class",
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
        })

        # IMPLEMENTS edges for each parent class
        for parent_cls in parent_classes:
            edges.append({
                "source": cls_id,
                "target": parent_cls.lower(),
                "relation": "IMPLEMENTS",
                "source_file": rel_path,
            })

    # ------------------------------------------------------------------
    # Decorator extraction
    # ------------------------------------------------------------------

    def _extract_decorators(self, decorated_node, source_bytes):
        """Extract decorator names from a decorated_definition node.

        Returns a list of decorator name strings, e.g. ['staticmethod', 'classmethod'].
        """
        decorators = []
        for child in decorated_node.children:
            if child.type == 'decorator':
                # @foo or @foo.bar or @foo(args)
                dec_text = self._node_text(child, source_bytes).lstrip('@')
                # Strip call arguments: @foo(args) -> foo
                paren_idx = dec_text.find('(')
                if paren_idx > 0:
                    dec_text = dec_text[:paren_idx]
                # For dotted decorators like @abc.abstractmethod, take the last part
                # but also store the full name
                decorators.append(dec_text)
        return decorators

    # ------------------------------------------------------------------
    # Function processing
    # ------------------------------------------------------------------

    def _process_py_func(self, node, source_bytes, filepath, source_root,
                         domain, functions, edges, class_name="",
                         decorators=None):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        func_name = self._node_text(name_node, source_bytes)
        if class_name:
            func_name = f"{class_name}.{func_name}"

        func_line = node.start_point[0] + 1
        func_id = self._make_func_id(domain, func_name)

        body_node = node.child_by_field_name('body')
        body_text = self._node_text(node, source_bytes)

        labels = self._detect_py_labels(func_name, body_text)

        # Add decorator labels
        if decorators:
            for dec in decorators:
                labels.append(f"@{dec}")
            if "staticmethod" in decorators:
                labels.append("static_method")
            if "classmethod" in decorators:
                labels.append("class_method")
            if "property" in decorators:
                labels.append("property_getter")
            if "abstractmethod" in decorators or "abc.abstractmethod" in decorators:
                labels.append("abstract_method")

        is_api, constraints = self._detect_api_entry(func_name, node, source_bytes, class_name)
        if is_api:
            labels.append("API_entry")

        # Extract return type annotation
        return_type = self._extract_return_type(node, source_bytes)

        params_list = self._extract_params(node, source_bytes)
        func_dict = {
            "id": func_id, "name": func_name,
            "source_file": os.path.relpath(filepath, source_root),
            "line": func_line, "domain": domain,
            "labels": labels, "is_empty": False,
            "api_constraints": constraints,
            "body_text": self._extract_body_text(node, source_bytes),
            "signature": self._extract_signature(node, source_bytes),
            "params": params_list,
            "local_vars": self._extract_local_vars(body_node, source_bytes, params_list) if body_node else params_list,
            "callee_args": [],
            "condition_vars": [],
            "doc_comment": self._extract_doc_comment(node, source_bytes),
            "start_byte": int(node.start_byte),
            "end_byte": int(node.end_byte),
        }
        if return_type:
            func_dict["return_type"] = return_type

        functions.append(func_dict)

        if body_node:
            callee_args_list = []
            cond_vars_list = []
            self._extract_py_calls(body_node, source_bytes, func_id, domain, edges,
                                   callee_args_list, cond_vars_list,
                                   class_name=class_name)
            if callee_args_list:
                functions[-1]["callee_args"] = callee_args_list
            if cond_vars_list:
                functions[-1]["condition_vars"] = cond_vars_list

        # Recurse into nested function definitions
        if body_node:
            self._walk_module(body_node, source_bytes, filepath, source_root,
                              domain, functions, edges, class_name=class_name)

    # ------------------------------------------------------------------
    # Return type extraction
    # ------------------------------------------------------------------

    def _extract_return_type(self, func_node, source_bytes):
        """Extract the return type annotation from a Python function definition.

        Tree-sitter Python uses a child with type 'type' for the return annotation.
        """
        for child in func_node.children:
            if child.type == 'type':
                return self._node_text(child, source_bytes)
        return ""

    # ------------------------------------------------------------------
    # Label detection
    # ------------------------------------------------------------------

    def _detect_py_labels(self, func_name: str, body_text: str) -> list:
        labels = []
        short_name = func_name.rsplit('.', 1)[-1]

        # Constructor
        if short_name == '__init__':
            labels.append("constructor")
        # Destructor
        if short_name == '__del__':
            labels.append("destructor")

        # Thread
        if re.search(r'\bThread\s*\(\s*target\s*=', body_text):
            labels.append("thread_processor")
        if re.search(r'\bthreading\.', body_text):
            if "thread_processor" not in labels:
                labels.append("thread_processor")

        # Callback
        if re.search(r'\bcallback\s*[\(=]', body_text, re.IGNORECASE):
            labels.append("callback_func")
        # Callback by naming convention (shared suffixes)
        if self._is_callback_by_name(func_name):
            if "callback_func" not in labels:
                labels.append("callback_func")

        return labels

    def _detect_api_entry(self, func_name: str, func_node, source_bytes: bytes,
                          class_name: str = "") -> tuple:
        """Python: module-level functions (not nested in class, not private) are API.
        Public methods in a class are NOT automatically API — only module-level
        functions and __init__.py exports qualify.
        Paths containing test/tool/script directories are excluded.
        """
        short_name = func_name.rsplit('.', 1)[-1]
        # Private/protected by convention
        if short_name.startswith('_'):
            return False, ""
        # Skip non-API paths — test, tool, script, documentation directories.
        # Combine the hardcoded baseline with project-declared non_api_paths
        # from profile.project_boundaries.non_api_paths (set by scanner factory).
        _NON_API_PATHS = ('documentation', 'doc', 'test', 'tests', 'testing',
                          'selftests', 'example', 'examples', 'sample',
                          'samples', 'benchmark', 'fuzz', 'tools', 'scripts',
                          'script', 'conformance')
        source_file = self._current_source_file if hasattr(self, '_current_source_file') else ""
        if source_file:
            src_lower = source_file.replace('\\', '/').lower()
            parts = src_lower.split('/')
            if any(p in _NON_API_PATHS for p in parts):
                return False, ""
            # Profile-declared non_api_paths (substring match on the original case).
            for nap in getattr(self, '_non_api_paths', []) or []:
                if nap and nap in source_file:
                    return False, ""
        # Only module-level functions (not class methods) are API entries.
        # Class methods are implementation details unless the class itself
        # is an explicit public interface (e.g., in __init__.py __all__).
        if not class_name:
            params_node = func_node.child_by_field_name('parameters')
            constraints = self._extract_param_constraints(
                self._node_text(params_node, source_bytes) if params_node else "()"
            )
            return True, constraints
        # Class methods are NOT API entries by default
        return False, ""

    # ------------------------------------------------------------------
    # Call extraction
    # ------------------------------------------------------------------

    def _extract_py_calls(self, body_node, source_bytes, invoker_id,
                          domain, edges, callee_args_list, cond_vars_list,
                          class_name=""):
        call_order = [0]
        cond_stack = []

        def _walk(node):
            if node.type == 'call':
                callee, full_callee = self._extract_py_callee(node, source_bytes, class_name=class_name)
                if callee:
                    call_order[0] += 1

                    # Capture call arguments
                    args_text = self._extract_callee_args(node, source_bytes)
                    args_structured = self._extract_callee_args_structured(node, source_bytes)
                    concurrency_info = self._detect_concurrency_info(callee, args_structured)
                    callee_arg_dict = {
                        "call_order": call_order[0],
                        "callee": callee,
                        "args_snippet": args_text,
                        "args": args_structured,
                        "concurrency_info": concurrency_info,
                    }
                    # If full_callee differs from callee (i.e., has a receiver),
                    # store it as an additional field
                    if full_callee and full_callee != callee:
                        callee_arg_dict["full_callee"] = full_callee
                    callee_args_list.append(callee_arg_dict)

                    edge_base = {
                        "target": callee.lower(),
                        "call_order": call_order[0],
                        "call_condition": "",
                    }
                    if concurrency_info.get("is_spawn"):
                        edge_base["concurrency"] = "thread_spawn"
                    elif concurrency_info.get("concurrency_type") == "callback_register":
                        edge_base["concurrency"] = "callback"

                    if cond_stack:
                        scope = cond_stack[-1]
                        scope["has_calls"] = True
                        edges.append({
                            "source": scope["empty_id"],
                            "is_cond_child": True,
                            **edge_base,
                        })
                    else:
                        edges.append({
                            "source": invoker_id,
                            **edge_base,
                        })

                    # If this is a thread spawn, add edge to spawned function
                    if concurrency_info.get("spawn_target"):
                        spawn_target_name = concurrency_info["spawn_target"]
                        edges.append({
                            "source": invoker_id,
                            "target": spawn_target_name.lower(),
                            "call_order": call_order[0],
                            "call_condition": "",
                            "concurrency": "spawn_target",
                        })
                        callee_args_list[-1]["callback_target"] = spawn_target_name

                return

            if node.type == 'if_statement':
                cond_node = node.child_by_field_name('condition')
                cond_text = self._node_text(cond_node, source_bytes).strip() if cond_node else ""
                cond_label = f"if({cond_text})" if cond_text else "if"
                empty_id = self._make_empty_id(invoker_id, len(cond_stack))
                # Capture condition variables
                cvars = self._extract_condition_vars(cond_text)
                if cvars:
                    cond_vars_list.append({"condition": cond_label, "vars": cvars})
                cond_stack.append({"condition": cond_label, "empty_id": empty_id, "has_calls": False})

                consequence = node.child_by_field_name('consequence')
                if consequence:
                    _walk(consequence)

                alternative = node.child_by_field_name('alternative')
                if alternative:
                    scope = cond_stack[-1]
                    if scope["has_calls"]:
                        edges.append({
                            "source": invoker_id, "target": scope["empty_id"],
                            "call_order": None, "call_condition": scope["condition"],
                        })
                    else_cond = f"!({cond_text})" if cond_text else "else"
                    cond_stack[-1] = {"condition": else_cond,
                                      "empty_id": scope["empty_id"] + "_else",
                                      "has_calls": False}
                    _walk(alternative)

                scope = cond_stack.pop()
                if scope["has_calls"]:
                    edges.append({
                        "source": invoker_id, "target": scope["empty_id"],
                        "call_order": None, "call_condition": scope["condition"],
                    })
                return

            if node.type == 'match_statement':
                for case_node in node.children:
                    if case_node.type == 'case_clause':
                        case_text = self._node_text(case_node, source_bytes).strip()
                        empty_id = self._make_empty_id(invoker_id, len(cond_stack))
                        cond_stack.append({"condition": case_text, "empty_id": empty_id, "has_calls": False})
                        for stmt in case_node.children:
                            if stmt.type != 'case_pattern':
                                _walk(stmt)
                        scope = cond_stack.pop()
                        if scope["has_calls"]:
                            edges.append({
                                "source": invoker_id, "target": scope["empty_id"],
                                "call_order": None, "call_condition": scope["condition"],
                            })
                return

            for child in node.children:
                _walk(child)

        _walk(body_node)

    def _extract_py_callee(self, call_node, source_bytes: bytes,
                           class_name="") -> tuple:
        """Extract callee name from a call expression.

        Returns (callee_short, full_callee) where:
        - callee_short: the last part of the callee (e.g., 'method' from 'obj.method')
        - full_callee: the full dotted callee text (e.g., 'obj.method')

        When inside a class method and the receiver is 'self' or 'cls',
        replaces the receiver with the class name.
        """
        func_node = call_node.child_by_field_name('function')
        if func_node is None:
            return "", ""
        text = self._node_text(func_node, source_bytes)

        full_callee = text
        # If the callee has a receiver (contains a dot), try to resolve self/cls
        if '.' in text and class_name:
            parts = text.split('.', 1)
            if parts[0] in ('self', 'cls'):
                full_callee = f"{class_name}.{parts[1]}"

        # obj.method -> extract method; module.func -> extract func
        parts = text.rsplit('.', 1)
        callee_short = parts[-1] if parts else text
        return callee_short, full_callee


# ---------------------------------------------------------------------------
# Java Scanner
# ---------------------------------------------------------------------------
