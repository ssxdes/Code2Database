#!/usr/bin/env python3
"""Rust scanner for invocation graph extraction using tree-sitter."""

import os
import re
from _scanner.base import BaseScanner
from _scanner.utils import check_python_version

try:
    import tree_sitter_rust as tsrust
    from tree_sitter import Language, Parser
    _HAS_RUST = True
except ImportError:
    _HAS_RUST = False


class RustTreeSitterScanner(BaseScanner):
    def __init__(self):
        if not _HAS_RUST:
            raise ImportError("Rust scanner requires tree-sitter-rust. Install: pip install tree-sitter-rust")
        self.lang = Language(tsrust.language())
        self.parser = Parser(self.lang)
        self._api_prefixes = []  # Set by scanner factory from profile api_detection.public_prefixes

    def _parse(self, source_bytes: bytes):
        return self.parser.parse(source_bytes)

    def _extract(self, tree, source_bytes: bytes, filepath: str,
                 source_root: str, domain: str):
        functions = []
        edges = []
        import_edges = []
        rel_path = os.path.relpath(filepath, source_root)
        self._current_source_file = rel_path
        # Track type names already emitted as struct/trait nodes to avoid
        # duplicates when an impl_item references a previously declared type.
        seen_type_names = set()
        self._walk_rust(tree.root_node, source_bytes, filepath, source_root,
                        domain, functions, edges, import_edges,
                        impl_type="", seen_type_names=seen_type_names)
        return functions, edges, import_edges

    def _walk_rust(self, node, source_bytes, filepath, source_root,
                   domain, functions, edges, import_edges, impl_type="",
                   seen_type_names=None):
        if seen_type_names is None:
            seen_type_names = set()
        rel_path = os.path.relpath(filepath, source_root)
        for child in node.children:
            if child.type == 'function_item':
                self._process_rust_func(child, source_bytes, filepath, source_root,
                                        domain, functions, edges, import_edges,
                                        impl_type)
            elif child.type == 'impl_item':
                # Extract the type being implemented
                type_node = child.child_by_field_name('type')
                impl_name = ""
                if type_node:
                    impl_name = self._node_text(type_node, source_bytes)

                # Check for trait implementation (impl Trait for Type)
                trait_node = child.child_by_field_name('trait')
                if trait_node:
                    trait_name = self._node_text(trait_node, source_bytes)
                    impl_type_id = self._make_func_id(domain, impl_name)
                    import_edges.append({
                        "source": impl_type_id,
                        "target": trait_name.lower(),
                        "relation": "IMPLEMENTS",
                        "source_file": rel_path,
                    })

                # Emit a struct/class-like node for the impl type ONLY
                # if we haven't already seen this type as a standalone item
                if impl_name and type_node and impl_name not in seen_type_names:
                    self._extract_type_node(
                        type_node, source_bytes, filepath, source_root,
                        domain, functions, rel_path, "struct"
                    )
                    seen_type_names.add(impl_name)

                self._walk_rust(child, source_bytes, filepath, source_root,
                                domain, functions, edges, import_edges,
                                impl_type=impl_name,
                                seen_type_names=seen_type_names)
            elif child.type == 'struct_item':
                name_node = child.child_by_field_name('name')
                struct_name = self._node_text(name_node, source_bytes) if name_node else ""
                self._extract_type_node(
                    child, source_bytes, filepath, source_root,
                    domain, functions, rel_path, "struct"
                )
                if struct_name:
                    seen_type_names.add(struct_name)
            elif child.type == 'trait_item':
                name_node = child.child_by_field_name('name')
                trait_name_text = self._node_text(name_node, source_bytes) if name_node else ""
                self._extract_type_node(
                    child, source_bytes, filepath, source_root,
                    domain, functions, rel_path, "trait"
                )
                if trait_name_text:
                    seen_type_names.add(trait_name_text)
                self._walk_rust(child, source_bytes, filepath, source_root,
                                domain, functions, edges, import_edges,
                                impl_type, seen_type_names=seen_type_names)
            elif child.type == 'use_declaration':
                self._extract_imports(child, source_bytes, domain, rel_path,
                                      import_edges)
            elif child.type == 'mod_item':
                self._extract_mod_import(child, source_bytes, domain, rel_path,
                                         import_edges)
            elif child.type in ('block', 'declaration_list'):
                self._walk_rust(child, source_bytes, filepath, source_root,
                                domain, functions, edges, import_edges,
                                impl_type, seen_type_names=seen_type_names)

    # ------------------------------------------------------------------
    # Import / use extraction
    # ------------------------------------------------------------------

    def _extract_imports(self, use_node, source_bytes, domain, rel_path,
                         import_edges):
        """Extract import edges from a `use` declaration."""
        # A use_declaration contains a use_clause or argument with the path.
        # Walk children to find the argument (the imported path).
        path_text = ""
        for child in use_node.children:
            if child.type in ('use_clause', 'argument', 'use_list',
                              'scoped_use_list', 'use_wildcard',
                              'use_as_clause'):
                path_text = self._node_text(child, source_bytes)
                break
        if not path_text:
            # Fallback: take the full text minus the 'use' keyword
            full = self._node_text(use_node, source_bytes).strip()
            if full.startswith('use'):
                path_text = full[3:].strip().rstrip(';').strip()
            else:
                path_text = full.rstrip(';').strip()

        if not path_text:
            return

        source_id = "file_" + domain.replace(".", "_")

        # Handle grouped imports like {a, b, c} — expand them
        # e.g.  use std::sync::{Arc, Mutex}  ->  two edges
        # e.g.  use std::io::Read            ->  one edge
        base_path, specifics = self._split_use_path(path_text)
        if specifics:
            for spec in specifics:
                target = (base_path + "::" + spec if base_path else spec).lower()
                import_edges.append({
                    "source": source_id,
                    "target": target,
                    "relation": "IMPORTS",
                    "source_file": rel_path,
                })
        else:
            import_edges.append({
                "source": source_id,
                "target": base_path.lower() if base_path else path_text.lower(),
                "relation": "IMPORTS",
                "source_file": rel_path,
            })

    @staticmethod
    def _split_use_path(path_text: str):
        """Split a use path into (base, [specifics]).

        Examples:
            "std::sync::{Arc, Mutex}" -> ("std::sync", ["Arc", "Mutex"])
            "std::io::Read"           -> ("std::io::Read", [])
            "self::module::*"         -> ("self::module::*", [])
        """
        # Find the innermost {…} group
        brace_start = path_text.find('{')
        if brace_start == -1:
            return path_text.strip(), []

        brace_end = path_text.rfind('}')
        if brace_end == -1 or brace_end <= brace_start:
            return path_text.strip(), []

        base = path_text[:brace_start].rstrip(':').strip()
        inner = path_text[brace_start + 1:brace_end]
        specifics = [s.strip() for s in inner.split(',') if s.strip()]
        return base, specifics

    def _extract_mod_import(self, mod_node, source_bytes, domain, rel_path,
                            import_edges):
        """Treat a `mod item;` declaration as an import edge for the module."""
        name_node = mod_node.child_by_field_name('name')
        if name_node is None:
            return
        mod_name = self._node_text(name_node, source_bytes)
        if not mod_name:
            return
        source_id = "file_" + domain.replace(".", "_")
        import_edges.append({
            "source": source_id,
            "target": mod_name.lower(),
            "relation": "IMPORTS",
            "source_file": rel_path,
        })

    # ------------------------------------------------------------------
    # Struct / trait node extraction
    # ------------------------------------------------------------------

    def _extract_type_node(self, type_or_decl_node, source_bytes, filepath,
                           source_root, domain, functions, rel_path,
                           node_type):
        """Emit a class-like node for struct_item or trait_item."""
        # Determine the name: for struct_item/trait_item, use 'name' field;
        # for a type_identifier passed from impl, use its text directly.
        if type_or_decl_node.type in ('struct_item', 'trait_item'):
            name_node = type_or_decl_node.child_by_field_name('name')
            type_name = self._node_text(name_node, source_bytes) if name_node else ""
        else:
            # type_identifier from impl block
            type_name = self._node_text(type_or_decl_node, source_bytes)

        if not type_name:
            return

        line_no = type_or_decl_node.start_point[0] + 1
        type_id = self._make_func_id(domain, type_name)

        # Build a signature-like text: "struct Name { fields }" or "trait Name"
        fields_text = ""
        body_child = type_or_decl_node.child_by_field_name('body')
        if body_child:
            fields_text = " " + self._node_text(body_child, source_bytes)
        elif type_or_decl_node.type == 'struct_item':
            # struct with tuple fields: struct Foo(i32, String);
            for ch in type_or_decl_node.children:
                if ch.type in ('field_list', 'ordered_field_list',
                               'declaration_list'):
                    fields_text = " " + self._node_text(ch, source_bytes)
                    break

        # For trait_item, include any trait bounds
        bounds_text = ""
        if type_or_decl_node.type == 'trait_item':
            for ch in type_or_decl_node.children:
                if ch.type == 'bounds_clause':
                    bounds_text = ": " + self._node_text(ch, source_bytes)
                    break

        signature = f"{node_type} {type_name}{bounds_text}{fields_text}"

        functions.append({
            "id": type_id,
            "name": type_name,
            "source_file": rel_path,
            "line": line_no,
            "domain": domain,
            "labels": [node_type],
            "is_empty": False,
            "api_constraints": "",
            "body_text": "",
            "signature": signature,
            "params": [],
            "local_vars": [],
            "callee_args": [],
            "condition_vars": [],
            "node_type": node_type,
            "start_byte": type_or_decl_node.start_byte,
            "end_byte": type_or_decl_node.end_byte,
        })

    # ------------------------------------------------------------------
    # Visibility extraction
    # ------------------------------------------------------------------

    def _extract_visibility(self, node, source_bytes):
        """Extract visibility modifier from a function or item node.

        Returns (visibility_str, modifiers_list).
        visibility_str is one of: "pub", "pub(crate)", "pub(super)",
                                   "pub(self)", "private".
        modifiers_list contains visibility + async + unsafe + const.
        """
        visibility = "private"
        modifiers = []

        # Check for visibility_modifier child
        for child in node.children:
            if child.type == 'visibility_modifier':
                vis_text = self._node_text(child, source_bytes).strip()
                # pub(crate), pub(super), pub(self), pub(in path)
                if vis_text == 'pub':
                    visibility = "pub"
                elif vis_text.startswith('pub('):
                    # Extract the restriction: pub(crate), pub(super), etc.
                    m = re.match(r'pub\((\w+.*?)\)', vis_text)
                    if m:
                        visibility = f"pub({m.group(1).strip()})"
                    else:
                        visibility = "pub"
                else:
                    visibility = vis_text
                modifiers.append(visibility)
                break

        # Check for async, unsafe, const keywords
        for child in node.children:
            text = self._node_text(child, source_bytes).strip()
            if child.type == 'async_keyword' or text == 'async':
                if 'async' not in modifiers:
                    modifiers.append('async')
            elif child.type == 'unsafe_keyword' or text == 'unsafe':
                if 'unsafe' not in modifiers:
                    modifiers.append('unsafe')
            elif child.type == 'const_keyword' or (text == 'const' and
                    child.type not in ('const_item', 'constant_item',
                                       'const_block')):
                if 'const' not in modifiers:
                    modifiers.append('const')

        return visibility, modifiers

    # ------------------------------------------------------------------
    # Return type extraction
    # ------------------------------------------------------------------

    def _extract_return_type(self, func_node, source_bytes):
        """Extract the return type from a function_item node.

        In tree-sitter-rust, the return type appears as a child `type` node
        after the `->` token.
        """
        # Look for return_type field or a `type` child after parameters
        ret_node = func_node.child_by_field_name('return_type')
        if ret_node:
            return self._node_text(ret_node, source_bytes).strip()

        # Fallback: scan children for -> followed by a type node
        found_arrow = False
        for child in func_node.children:
            text = self._node_text(child, source_bytes).strip()
            if text == '->':
                found_arrow = True
                continue
            if found_arrow and child.type == 'type':
                return self._node_text(child, source_bytes).strip()
            # The return type might be a more specific node type
            if found_arrow and child.type in (
                'type_identifier', 'generic_type', 'primitive_type',
                'qualified_type', 'reference_type', 'pointer_type',
                'array_type', 'tuple_type', 'unit_type', 'never_type',
                'function_type', 'scoped_type_identifier',
            ):
                return self._node_text(child, source_bytes).strip()
            if found_arrow:
                # If we found the arrow but the next node isn't a recognized
                # type, still grab its text
                return text
        return ""

    # ------------------------------------------------------------------
    # Function processing (enhanced)
    # ------------------------------------------------------------------

    def _process_rust_func(self, node, source_bytes, filepath, source_root,
                           domain, functions, edges, import_edges,
                           impl_type=""):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        func_name = self._node_text(name_node, source_bytes)
        if impl_type:
            func_name = f"{impl_type}::{func_name}"

        func_line = node.start_point[0] + 1
        func_id = self._make_func_id(domain, func_name)
        body_node = node.child_by_field_name('body')
        body_text = self._node_text(node, source_bytes)

        labels = self._detect_rust_labels(func_name, body_text)
        is_api, constraints = self._detect_api_entry(func_name, node, source_bytes)
        if is_api:
            labels.append("API_entry")

        # Visibility and modifiers
        visibility, modifiers = self._extract_visibility(node, source_bytes)

        # Return type
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
            "visibility": visibility,
            "modifiers": modifiers,
            "return_type": return_type,
            "doc_comment": self._extract_doc_comment(node, source_bytes),
            "start_byte": int(node.start_byte),
            "end_byte": int(node.end_byte),
        }
        functions.append(func_dict)

        if body_node:
            callee_args_list = []
            cond_vars_list = []
            self._extract_rust_calls(body_node, source_bytes, func_id, domain, edges,
                                     callee_args_list, cond_vars_list)
            if callee_args_list:
                functions[-1]["callee_args"] = callee_args_list
            if cond_vars_list:
                functions[-1]["condition_vars"] = cond_vars_list

    def _detect_api_entry(self, func_name: str, func_node, source_bytes: bytes) -> tuple:
        """Rust: pub fn functions are API candidates, but with exclusions.

        Exclusions:
        - Functions in std/core/alloc crates (standard library internals)
        - Generic impl methods (e.g., Arc<T>::method, Box<T>::method)
        - Functions with pub(crate)/pub(super) restricted visibility
        - Test/benchmark/example paths
        """
        # Check visibility first
        is_pub = False
        for child in func_node.children:
            if child.type == 'visibility_modifier':
                vis_text = self._node_text(child, source_bytes).strip()
                # Only unrestricted 'pub' counts; pub(crate), pub(super) etc. do NOT
                if vis_text == 'pub':
                    is_pub = True
                break
        if not is_pub:
            # Regex fallback for tree-sitter-rust version differences
            func_text = self._node_text(func_node, source_bytes)
            if not re.match(r'\s*pub\s+(async\s+)?fn\b', func_text):
                return False, ""

        # Exclusion: standard library crates (std/core/alloc) are not project API
        source_file = getattr(self, '_current_source_file', '')
        if source_file:
            parts = source_file.replace('\\', '/').lower().split('/')
            # Rust std/core/alloc crate directories
            if any(p in ('std', 'core', 'alloc') for p in parts[:3]):
                return False, ""
            # Test/benchmark/example paths
            _NON_API_PATHS = ('test', 'tests', 'testing', 'selftests',
                              'example', 'examples', 'benchmark', 'benches',
                              'fuzz', 'tools', 'scripts')
            if any(p in _NON_API_PATHS for p in parts):
                return False, ""

        # Exclusion: generic impl methods (e.g., Arc<T>::method, Box<T, A>::method)
        # These are standard library patterns that look pub but are not project APIs.
        # Match Name<T...>:: or Name<T, A>:: where Name is a known std type.
        _STD_CONTAINER_RE = re.compile(
            r'^(?:Arc|Box|Vec|VecDeque|HashMap|HashSet|BTreeMap|BTreeSet|'
            r'Option|Result|Cow|Rc|Pin|Cell|RefCell|Mutex|RWLock|'
            r'ARef|Weak|NonNull|ManuallyDrop|MaybeUninit)'
            r'(?:<[^>]*>)?::')
        if _STD_CONTAINER_RE.match(func_name):
            return False, ""

        # Profile-driven prefix check: if api_prefixes is configured,
        # require the function name to match one of the prefixes.
        api_prefixes = getattr(self, '_api_prefixes', [])
        if api_prefixes:
            func_lower = func_name.lower()
            # For impl methods (Type::method), check the method part
            method_name = func_name.rsplit('::', 1)[-1] if '::' in func_name else func_name
            method_lower = method_name.lower()
            if not any(method_lower.startswith(p) for p in api_prefixes):
                # If profile specifies prefixes, only those count as API
                return False, ""

        params_node = func_node.child_by_field_name('parameters')
        constraints = self._extract_param_constraints(
            self._node_text(params_node, source_bytes) if params_node else "()"
        )
        return True, constraints

    def _detect_rust_labels(self, func_name: str, body_text: str) -> list:
        labels = []
        # Thread
        if re.search(r'\bspawn\s*\(', body_text) or re.search(r'\bthread::spawn\b', body_text):
            labels.append("thread_processor")
        # Destructor: Drop::drop impl
        if '::drop' in func_name.lower():
            labels.append("destructor")
        # Callback: closure passed as argument
        if re.search(r'\bcallback\s*[\(=]', body_text, re.IGNORECASE):
            labels.append("callback_func")
        # Callback by naming convention (shared suffixes)
        if self._is_callback_by_name(func_name):
            if "callback_func" not in labels:
                labels.append("callback_func")
        return labels

    def _extract_rust_calls(self, body_node, source_bytes, invoker_id,
                            domain, edges, callee_args_list, cond_vars_list):
        call_order = [0]
        cond_stack = []

        def _walk(node):
            if node.type == 'call_expression':
                callee, full_callee = self._extract_rust_callee(node, source_bytes)
                if callee:
                    call_order[0] += 1

                    # Capture call arguments
                    args_text = self._extract_callee_args(node, source_bytes)
                    args_structured = self._extract_callee_args_structured(node, source_bytes)
                    concurrency_info = self._detect_concurrency_info(callee, args_structured)
                    callee_entry = {
                        "call_order": call_order[0],
                        "callee": callee,
                        "args_snippet": args_text,
                        "args": args_structured,
                        "concurrency_info": concurrency_info,
                    }
                    if full_callee and full_callee != callee:
                        callee_entry["full_callee"] = full_callee
                    callee_args_list.append(callee_entry)

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

            if node.type == 'if_expression':
                cond_node = node.child_by_field_name('condition')
                cond_text = self._node_text(cond_node, source_bytes).strip() if cond_node else ""
                cond_label = f"if({cond_text})" if cond_text else "if"
                empty_id = self._make_empty_id(invoker_id, len(cond_stack))

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

            if node.type == 'match_expression':
                for arm in node.children:
                    if arm.type == 'match_arm':
                        pattern = arm.child_by_field_name('pattern')
                        arm_text = self._node_text(pattern, source_bytes) if pattern else ""
                        empty_id = self._make_empty_id(invoker_id, len(cond_stack))

                        cvars = self._extract_condition_vars(arm_text)
                        if cvars:
                            cond_vars_list.append({"condition": arm_text, "vars": cvars})

                        cond_stack.append({"condition": arm_text, "empty_id": empty_id, "has_calls": False})
                        value = arm.child_by_field_name('value')
                        if value:
                            _walk(value)
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

    def _extract_rust_callee(self, call_node, source_bytes: bytes):
        """Extract callee name from a call expression.

        Returns (short_name, full_path) where short_name is the final
        segment after ::, and full_path is the complete qualified path.
        """
        func_node = call_node.child_by_field_name('function')
        if func_node is None:
            if call_node.children:
                func_node = call_node.children[0]
            else:
                return "", ""
        full_path = self._node_text(func_node, source_bytes)
        # Handle path::func or Type::method
        parts = full_path.rsplit('::', 1)
        short_name = parts[-1] if parts else full_path
        return short_name, full_path
