#!/usr/bin/env python3
"""Go scanner for invocation graph extraction using tree-sitter."""

import os
import re
from _scanner.base import BaseScanner

try:
    import tree_sitter_go as tsgo
    from tree_sitter import Language, Parser
    _HAS_GO = True
except ImportError:
    _HAS_GO = False


class GoTreeSitterScanner(BaseScanner):
    def __init__(self):
        if not _HAS_GO:
            raise ImportError("Go scanner requires tree-sitter-go. Install: pip install tree-sitter-go")
        self.lang = Language(tsgo.language())
        self.parser = Parser(self.lang)
        # Set by scanner factory from profile project_boundaries.non_api_paths.
        # Functions in these paths (test/, examples/, app/, etc.) are never
        # tagged API_entry, even if exported (uppercase) or named main()/init().
        self._non_api_paths = []

    def _parse(self, source_bytes: bytes):
        return self.parser.parse(source_bytes)

    def _extract(self, tree, source_bytes: bytes, filepath: str,
                 source_root: str, domain: str):
        functions = []
        edges = []

        root = tree.root_node
        for node in root.children:
            if node.type == 'function_declaration':
                self._process_func(node, source_bytes, filepath, source_root,
                                   domain, functions, edges)
            elif node.type == 'method_declaration':
                self._process_func(node, source_bytes, filepath, source_root,
                                   domain, functions, edges, is_method=True)

        return functions, edges

    def _process_func(self, node, source_bytes, filepath, source_root,
                      domain, functions, edges, is_method=False):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        func_name = self._node_text(name_node, source_bytes)

        if is_method:
            receiver = node.child_by_field_name('receiver')
            if receiver:
                recv_text = self._node_text(receiver, source_bytes).strip('() ')
                # Extract type name from receiver
                m = re.search(r'(\w+)\s*$', recv_text)
                if m:
                    func_name = m.group(1) + "." + func_name

        func_line = node.start_point[0] + 1
        func_id = self._make_func_id(domain, func_name)

        body_node = node.child_by_field_name('body')
        body_text = self._node_text(node, source_bytes)

        labels = self._detect_go_labels(func_name, body_text)
        self._current_filepath = filepath
        is_api, constraints = self._detect_api_entry(func_name, node, source_bytes)
        if is_api:
            labels.append("API_entry")

        params_list = self._extract_params(node, source_bytes)
        functions.append({
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
        })

        if body_node:
            callee_args_list = []
            cond_vars_list = []
            goto_jumps_list = []
            goto_labels_list = []
            self._extract_go_calls(body_node, source_bytes, func_id, domain, edges,
                                   callee_args_list, cond_vars_list,
                                   goto_jumps_list, goto_labels_list)
            if callee_args_list:
                functions[-1]["callee_args"] = callee_args_list
            if cond_vars_list:
                functions[-1]["condition_vars"] = cond_vars_list
            if goto_jumps_list:
                functions[-1]["goto_jumps"] = goto_jumps_list
            if goto_labels_list:
                functions[-1]["goto_labels"] = goto_labels_list

    def _detect_go_labels(self, func_name: str, body_text: str) -> list:
        labels = []
        # Goroutine: go func()
        if re.search(r'\bgo\s+(func|[a-zA-Z])', body_text):
            labels.append("thread_processor")
        # Callback: function passed as argument
        if re.search(r'\b(?:callback|handler|fn)\s*[\(=]', body_text, re.IGNORECASE):
            labels.append("callback_func")
        # Callback by naming convention (shared suffixes + Go-specific)
        if self._is_callback_by_name(func_name) or func_name.endswith('HandleFunc'):
            if "callback_func" not in labels:
                labels.append("callback_func")
        return labels

    def _detect_api_entry(self, func_name: str, func_node, source_bytes: bytes) -> tuple:
        """Go: exported functions (first letter uppercase) are public API.
        Also recognizes main() and init() as entry points.

        Functions in project-declared non-API paths (test/, examples/, etc.)
        are never tagged API_entry, even if they are main()/init() or
        uppercase-named — they are test/example entry points, not library API.

        Go also has universal test conventions that apply regardless of
        profile: files ending in `_test.go` are test files, and functions
        named Test*/Benchmark*/Example*/Fuzz* are test entry points, not
        library API. These are skipped by default to keep the tool
        project-agnostic.
        """
        short_name = func_name.rsplit('.', 1)[-1]
        current_fp = getattr(self, '_current_filepath', '')
        current_fp_norm = current_fp.replace(os.sep, '/')

        # Project-declared non-API paths from profile.project_boundaries.non_api_paths.
        _non_api_paths = tuple(getattr(self, '_non_api_paths', []) or [])
        if _non_api_paths:
            for nap in _non_api_paths:
                if nap and nap in current_fp_norm:
                    return False, ""

        # Universal Go test-file convention: any file ending in _test.go is
        # a test file, regardless of path. Functions defined there are test
        # helpers, not library API.
        if current_fp_norm.endswith('_test.go'):
            return False, ""

        # Universal Go test-function naming convention: Test*/Benchmark*/
        # Example*/Fuzz* are test entry points (the `go test` runner invokes
        # them). Even though they start with an uppercase letter (Go exported
        # convention), they are NOT library API.
        if short_name.startswith(('Test', 'Benchmark', 'Example', 'Fuzz')):
            # Only treat as test entry if signature matches the `go test`
            # runner convention:
            #   TestXxx(t *testing.T)
            #   BenchmarkXxx(b *testing.B)
            #   FuzzXxx(f *testing.F)
            #   ExampleXxx()       (no params)
            # This avoids false positives on real exported functions that
            # happen to start with "Test" (e.g., TestController() int).
            params_node = func_node.child_by_field_name('parameters')
            params_text = self._node_text(params_node, source_bytes) if params_node else "()"
            is_test_sig = (
                'testing.T' in params_text or
                'testing.B' in params_text or
                'testing.F' in params_text
            )
            # ExampleXxx has no params and is invoked by `go test` as an
            # example. But only treat no-param functions as examples when
            # the name starts with "Example" — TestXxx() and BenchmarkXxx()
            # with no params are not valid test entries, so leave them as
            # regular exported functions.
            if is_test_sig:
                return False, ""
            if short_name.startswith('Example') and params_text == '()':
                return False, ""

        # main() and init() are always entry points regardless of case
        if short_name in ('main', 'init'):
            params_node = func_node.child_by_field_name('parameters')
            constraints = self._extract_param_constraints(
                self._node_text(params_node, source_bytes) if params_node else "()"
            )
            return True, constraints
        # func name starts with uppercase letter → exported
        if short_name and short_name[0].isupper():
            params_node = func_node.child_by_field_name('parameters')
            constraints = self._extract_param_constraints(
                self._node_text(params_node, source_bytes) if params_node else "()"
            )
            return True, constraints
        return False, ""

    def _extract_go_calls(self, body_node, source_bytes, invoker_id,
                          domain, edges, callee_args_list, cond_vars_list,
                          goto_jumps_list=None, goto_labels_list=None):
        call_order = [0]
        cond_stack = []
        _goto_jumps = goto_jumps_list if goto_jumps_list is not None else []
        _goto_labels = goto_labels_list if goto_labels_list is not None else []

        # Pre-scan: collect ALL label positions so forward goto direction
        # can be determined correctly.
        def _collect_labels(node):
            if node.type == 'labeled_statement':
                for child in node.children:
                    if child.type == 'label_name':
                        _goto_labels.append({
                            "label": self._node_text(child, source_bytes),
                            "line": node.start_point[0] + 1,
                        })
                        break
            for child in node.children:
                _collect_labels(child)
        _collect_labels(body_node)

        def _walk(node):
            if node.type == 'call_expression':
                callee = self._extract_go_callee(node, source_bytes)
                if callee:
                    call_order[0] += 1

                    # Capture call arguments
                    args_text = self._extract_callee_args(node, source_bytes)
                    args_structured = self._extract_callee_args_structured(node, source_bytes)

                    # Check if this is a goroutine launch
                    is_goroutine = (node.parent and node.parent.type == 'go_statement')

                    concurrency_info = {"is_spawn": is_goroutine, "spawn_target": "",
                                        "spawn_arg": "", "concurrency_type": ""}
                    if is_goroutine:
                        concurrency_info["concurrency_type"] = "goroutine"
                        # The callee itself IS the spawned function
                        concurrency_info["spawn_target"] = callee

                    callee_args_list.append({
                        "call_order": call_order[0],
                        "callee": callee,
                        "args_snippet": args_text,
                        "args": args_structured,
                        "concurrency_info": concurrency_info,
                        "line": node.start_point[0] + 1,
                    })

                    # Build edge with concurrency info
                    edge_base = {
                        "target": callee.lower(),
                        "call_order": call_order[0],
                        "call_condition": "",
                    }
                    if is_goroutine:
                        edge_base["concurrency"] = "goroutine"

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
                return

            if node.type == 'if_statement':
                cond_node = node.child_by_field_name('condition')
                cond_text = self._node_text(cond_node, source_bytes).strip('() ') if cond_node else ""
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

            if node.type == 'switch_statement':
                body = node.child_by_field_name('body')
                if body:
                    for case_node in body.children:
                        if case_node.type == 'case_clause':
                            case_text = self._node_text(case_node, source_bytes).split(':', 1)[0].strip()
                            empty_id = self._make_empty_id(invoker_id, len(cond_stack))
                            cond_stack.append({"condition": case_text, "empty_id": empty_id, "has_calls": False})
                            for stmt in case_node.children:
                                _walk(stmt)
                            scope = cond_stack.pop()
                            if scope["has_calls"]:
                                edges.append({
                                    "source": invoker_id, "target": scope["empty_id"],
                                    "call_order": None, "call_condition": scope["condition"],
                                })
                return

            # === goto_statement: extract goto target label ===
            if node.type == 'goto_statement':
                for child in node.children:
                    if child.type == 'label_name':
                        _label_name = self._node_text(child, source_bytes)
                        _goto_line = node.start_point[0] + 1
                        _direction = "unknown"
                        for _lbl in _goto_labels:
                            if _lbl["label"] == _label_name:
                                if _lbl["line"] < _goto_line:
                                    _direction = "backward"
                                elif _lbl["line"] > _goto_line:
                                    _direction = "forward"
                                break
                        _goto_jumps.append({
                            "label": _label_name,
                            "line": _goto_line,
                            "direction": _direction,
                        })
                        break
                return

            # === labeled_statement: process the labeled body ===
            # Labels were already collected in the pre-scan pass.
            if node.type == 'labeled_statement':
                # Continue processing the labeled statement's body
                for child in node.children:
                    if child.type not in ('label_name', ':'):
                        _walk(child)
                return

            for child in node.children:
                _walk(child)

        _walk(body_node)

    def _extract_go_callee(self, call_node, source_bytes: bytes) -> str:
        func_node = call_node.child_by_field_name('function')
        if func_node is None:
            return ""
        text = self._node_text(func_node, source_bytes)
        # package.Func or struct.Method or just func
        parts = text.rsplit('.', 1)
        return parts[-1] if parts else text


# ---------------------------------------------------------------------------
# Python Scanner
# ---------------------------------------------------------------------------

