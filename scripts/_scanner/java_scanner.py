#!/usr/bin/env python3
"""Java scanner for invocation graph extraction using tree-sitter."""

import os
import re
from _scanner.base import BaseScanner

try:
    import tree_sitter_java as tsjava
    from tree_sitter import Language, Parser
    _HAS_JAVA = True
except ImportError:
    _HAS_JAVA = False


class JavaTreeSitterScanner(BaseScanner):
    def __init__(self):
        if not _HAS_JAVA:
            raise ImportError("Java scanner requires tree-sitter-java. Install: pip install tree-sitter-java")
        self.lang = Language(tsjava.language())
        self.parser = Parser(self.lang)

    def _parse(self, source_bytes: bytes):
        return self.parser.parse(source_bytes)

    def _extract(self, tree, source_bytes: bytes, filepath: str,
                 source_root: str, domain: str):
        functions = []
        edges = []
        import_edges = []

        # Extract package declaration to override domain
        package_name = self._extract_package(tree.root_node, source_bytes)
        if package_name:
            domain = package_name

        self._walk_java(tree.root_node, source_bytes, filepath, source_root,
                        domain, functions, edges, import_edges, class_name="")

        # Extract imports
        import_edges = self._extract_imports(tree.root_node, source_bytes,
                                              filepath, source_root, domain)

        return functions, edges, import_edges

    # ------------------------------------------------------------------
    # Package extraction
    # ------------------------------------------------------------------
    def _extract_package(self, root_node, source_bytes: bytes) -> str:
        """Extract package declaration from the AST root and return as domain."""
        for child in root_node.children:
            if child.type == 'package_declaration':
                # The package name is in a 'scoped_identifier' or 'identifier' child
                for sub in child.children:
                    if sub.type in ('scoped_identifier', 'identifier'):
                        return self._node_text(sub, source_bytes)
        return ""

    # ------------------------------------------------------------------
    # Import extraction
    # ------------------------------------------------------------------
    def _extract_imports(self, root_node, source_bytes: bytes,
                         filepath: str, source_root: str, domain: str) -> list:
        """Walk AST for import_declaration nodes and build IMPORTS edges."""
        import_edges = []
        rel_path = os.path.relpath(filepath, source_root)
        source_id = "file_" + domain.replace(".", "_")

        for child in root_node.children:
            if child.type == 'import_declaration':
                # The imported name is in a 'scoped_identifier' or 'identifier' child,
                # possibly preceded by 'static' keyword
                imported_name = ""
                for sub in child.children:
                    if sub.type in ('scoped_identifier', 'identifier'):
                        imported_name = self._node_text(sub, source_bytes)
                        break
                if imported_name:
                    import_edges.append({
                        "source": source_id,
                        "target": imported_name.lower(),
                        "relation": "IMPORTS",
                        "source_file": rel_path,
                    })
        return import_edges

    # ------------------------------------------------------------------
    # Class/interface node extraction
    # ------------------------------------------------------------------
    def _extract_class_node(self, node, source_bytes: bytes, filepath: str,
                            source_root: str, domain: str, class_name: str,
                            functions: list, edges: list, node_type: str):
        """Create a class/interface node and extract IMPLEMENTS/EXTENDS edges."""
        rel_path = os.path.relpath(filepath, source_root)
        cls_line = node.start_point[0] + 1
        cls_id = self._make_func_id(domain, class_name)

        # Build signature text including extends/implements
        sig_parts = [node_type, class_name]
        extends_names = []
        implements_names = []

        for child in node.children:
            if child.type == 'superclass':
                # extends clause: superclass > type_identifier / scoped_identifier
                for sub in child.children:
                    if sub.type in ('type_identifier', 'scoped_identifier'):
                        extends_names.append(self._node_text(sub, source_bytes))
            elif child.type == 'interfaces':
                # implements clause: interfaces > type_list > type_identifier/scoped_identifier
                for sub in child.children:
                    if sub.type == 'type_list':
                        for t in sub.children:
                            if t.type in ('type_identifier', 'scoped_identifier'):
                                implements_names.append(self._node_text(t, source_bytes))

        if extends_names:
            sig_parts.append("extends")
            sig_parts.append(", ".join(extends_names))
        if implements_names:
            sig_parts.append("implements")
            sig_parts.append(", ".join(implements_names))

        labels = [node_type]

        functions.append({
            "id": cls_id, "name": class_name,
            "source_file": rel_path, "line": cls_line, "domain": domain,
            "labels": labels, "is_empty": False,
            "api_constraints": "",
            "body_text": "",
            "signature": " ".join(sig_parts),
            "params": [], "local_vars": [], "callee_args": [],
            "condition_vars": [], "node_type": node_type,
            "start_byte": node.start_byte,
            "end_byte": node.end_byte,
        })

        # Add IMPLEMENTS edges for extends
        for parent_name in extends_names:
            edges.append({
                "source": cls_id,
                "target": parent_name.lower(),
                "relation": "IMPLEMENTS",
                "source_file": rel_path,
            })

        # Add IMPLEMENTS edges for implements
        for iface_name in implements_names:
            edges.append({
                "source": cls_id,
                "target": iface_name.lower(),
                "relation": "IMPLEMENTS",
                "source_file": rel_path,
            })

    # ------------------------------------------------------------------
    # Modifier extraction
    # ------------------------------------------------------------------
    def _extract_modifiers(self, node, source_bytes: bytes) -> list:
        """Extract modifiers (public, private, protected, static, final,
        synchronized, abstract) from a method/constructor node."""
        modifiers = []
        for child in node.children:
            if child.type == 'modifiers':
                for mod in child.children:
                    mod_text = self._node_text(mod, source_bytes)
                    if mod_text in ('public', 'private', 'protected',
                                    'static', 'final', 'synchronized', 'abstract'):
                        modifiers.append(mod_text)
            elif child.type in ('public', 'private', 'protected',
                                'static', 'final', 'synchronized', 'abstract'):
                modifiers.append(self._node_text(child, source_bytes))
        return modifiers

    # ------------------------------------------------------------------
    # AST walk
    # ------------------------------------------------------------------
    def _walk_java(self, node, source_bytes, filepath, source_root,
                   domain, functions, edges, import_edges, class_name=""):
        for child in node.children:
            if child.type == 'class_declaration' or child.type == 'interface_declaration':
                cls_name_node = next((c for c in child.children if c.type == 'identifier'), None)
                cls_name = self._node_text(cls_name_node, source_bytes) if cls_name_node else class_name
                node_type = 'class' if child.type == 'class_declaration' else 'interface'

                # Create class/interface node + IMPLEMENTS edges
                self._extract_class_node(child, source_bytes, filepath,
                                         source_root, domain, cls_name,
                                         functions, edges, node_type)

                self._walk_java(child, source_bytes, filepath, source_root,
                                domain, functions, edges, import_edges, class_name=cls_name)
            elif child.type == 'method_declaration':
                self._process_java_method(child, source_bytes, filepath, source_root,
                                          domain, functions, edges, class_name)
            elif child.type == 'constructor_declaration':
                self._process_java_constructor(child, source_bytes, filepath, source_root,
                                               domain, functions, edges, class_name)
            elif child.type == 'class_body':
                # class_declaration > class_body contains method/constructor declarations
                self._walk_java(child, source_bytes, filepath, source_root,
                                domain, functions, import_edges, class_name)
            elif child.type == 'interface_body':
                # interface_body can contain abstract method declarations
                self._walk_java(child, source_bytes, filepath, source_root,
                                domain, functions, import_edges, class_name)

    def _process_java_method(self, node, source_bytes, filepath, source_root,
                             domain, functions, edges, class_name):
        name_node = node.child_by_field_name('name')
        if name_node is None:
            return
        method_name = self._node_text(name_node, source_bytes)
        if class_name:
            method_name = f"{class_name}.{method_name}"

        func_line = node.start_point[0] + 1
        func_id = self._make_func_id(domain, method_name)
        body_node = node.child_by_field_name('body')
        body_text = self._node_text(node, source_bytes)

        labels = self._detect_java_labels(method_name, body_text, node, source_bytes)
        is_api, constraints = self._detect_api_entry(method_name, node, source_bytes)
        if is_api:
            labels.append("API_entry")

        # Extract modifiers
        modifiers = self._extract_modifiers(node, source_bytes)

        # Add modifier-based labels
        if 'static' in modifiers and "static_method" not in labels:
            labels.append("static_method")
        if 'synchronized' in modifiers and "synchronized" not in labels:
            labels.append("synchronized")

        # Extract return type
        return_type = self._extract_return_type(node, source_bytes)

        params_list = self._extract_params(node, source_bytes)
        functions.append({
            "id": func_id, "name": method_name,
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
            "modifiers": modifiers,
            "return_type": return_type,
            "doc_comment": self._extract_doc_comment(node, source_bytes),
            "start_byte": int(node.start_byte),
            "end_byte": int(node.end_byte),
        })

        if body_node:
            callee_args_list = []
            cond_vars_list = []
            self._extract_java_calls(body_node, source_bytes, func_id, domain, edges,
                                     callee_args_list, cond_vars_list)
            if callee_args_list:
                functions[-1]["callee_args"] = callee_args_list
            if cond_vars_list:
                functions[-1]["condition_vars"] = cond_vars_list

    def _process_java_constructor(self, node, source_bytes, filepath, source_root,
                                  domain, functions, edges, class_name):
        func_name = f"{class_name}.<init>" if class_name else "<init>"
        func_line = node.start_point[0] + 1
        func_id = self._make_func_id(domain, func_name)
        body_node = node.child_by_field_name('body')

        # Constructors are public API by default
        is_api, constraints = self._detect_api_entry(func_name, node, source_bytes)
        labels = ["constructor"]
        if is_api:
            labels.append("API_entry")

        # Extract modifiers for constructors too
        modifiers = self._extract_modifiers(node, source_bytes)

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
            "modifiers": modifiers,
            "return_type": "",
            "doc_comment": self._extract_doc_comment(node, source_bytes),
        })

        if body_node:
            callee_args_list = []
            cond_vars_list = []
            self._extract_java_calls(body_node, source_bytes, func_id, domain, edges,
                                     callee_args_list, cond_vars_list)
            if callee_args_list:
                functions[-1]["callee_args"] = callee_args_list
            if cond_vars_list:
                functions[-1]["condition_vars"] = cond_vars_list

    # ------------------------------------------------------------------
    # Return type extraction
    # ------------------------------------------------------------------
    def _extract_return_type(self, node, source_bytes: bytes) -> str:
        """Extract the return type from a method_declaration node."""
        for child in node.children:
            if child.type in ('type_identifier', 'primitive_type',
                              'generic_type', 'array_type',
                              'scoped_type_identifier', 'void_type'):
                return self._node_text(child, source_bytes)
        return ""

    def _detect_java_labels(self, func_name: str, body_text: str,
                            node, source_bytes: bytes) -> list:
        labels = []
        # Thread
        if re.search(r'\bnew\s+Thread\s*\(', body_text):
            labels.append("thread_processor")
        if re.search(r'\.start\s*\(\s*\)', body_text):
            if "thread_processor" not in labels:
                labels.append("thread_processor")

        # Callback
        if re.search(r'\bcallback\s*[\(=]', body_text, re.IGNORECASE):
            labels.append("callback_func")
        # Check for @Override on callback-like interfaces
        if node.parent and node.parent.type == 'decorated_definition':
            for dec in node.parent.children:
                if dec.type == 'marker_annotation' or dec.type == 'annotation':
                    dec_text = self._node_text(dec, source_bytes)
                    if any(kw in dec_text for kw in ('Override', 'EventListener', 'Handler')):
                        labels.append("callback_func")
        # Callback by naming convention (shared suffixes + Java-specific)
        if self._is_callback_by_name(func_name) or \
           func_name.endswith('Listener') or func_name.endswith('Handler'):
            if "callback_func" not in labels:
                labels.append("callback_func")

        # Destructor: finalize() or close() methods
        short = func_name.rsplit('.', 1)[-1]
        if short in ('finalize', 'close', 'dispose'):
            labels.append("destructor")

        return labels

    def _extract_java_calls(self, body_node, source_bytes, invoker_id,
                            domain, edges, callee_args_list, cond_vars_list):
        call_order = [0]
        cond_stack = []

        def _walk(node):
            if node.type == 'method_invocation':
                callee = self._extract_java_callee(node, source_bytes)
                if callee:
                    call_order[0] += 1

                    # Capture call arguments
                    args_text = self._extract_callee_args(node, source_bytes)
                    args_structured = self._extract_callee_args_structured(node, source_bytes)
                    concurrency_info = self._detect_concurrency_info(callee, args_structured)

                    # Extract full callee (obj.method form)
                    full_callee = self._extract_full_callee(node, source_bytes)

                    callee_args_list.append({
                        "call_order": call_order[0],
                        "callee": callee,
                        "full_callee": full_callee,
                        "args_snippet": args_text,
                        "args": args_structured,
                        "concurrency_info": concurrency_info,
                    })

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
                cond_text = self._node_text(cond_node, source_bytes).strip('() ') if cond_node else ""
                cond_label = f"if({cond_text})" if cond_text else "if"

                # Capture condition variables
                cvars = self._extract_condition_vars(cond_text)
                if cvars:
                    cond_vars_list.append({"condition": cond_label, "vars": cvars})

                empty_id = self._make_empty_id(invoker_id, len(cond_stack))
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
                        if case_node.type == 'switch_block_statement_group':
                            case_text = self._node_text(case_node, source_bytes).split('\n')[0].strip()
                            empty_id = self._make_empty_id(invoker_id, len(cond_stack))

                            cvars = self._extract_condition_vars(case_text)
                            if cvars:
                                cond_vars_list.append({"condition": case_text, "vars": cvars})

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

            for child in node.children:
                _walk(child)

        _walk(body_node)

    def _extract_java_callee(self, call_node, source_bytes: bytes) -> str:
        name_node = call_node.child_by_field_name('name')
        if name_node:
            return self._node_text(name_node, source_bytes)
        # Fallback
        text = self._node_text(call_node, source_bytes)
        m = re.match(r'([A-Za-z_]\w*)', text)
        return m.group(1) if m else ""

    def _extract_full_callee(self, call_node, source_bytes: bytes) -> str:
        """Extract the full callee expression (e.g., 'obj.method' for obj.method()).

        For method_invocation nodes, the object reference is in the 'object' field.
        Returns empty string if no object reference exists (bare method call).
        """
        obj_node = call_node.child_by_field_name('object')
        if obj_node:
            obj_text = self._node_text(obj_node, source_bytes)
            name_node = call_node.child_by_field_name('name')
            method_name = self._node_text(name_node, source_bytes) if name_node else ""
            return f"{obj_text}.{method_name}" if method_name else obj_text
        return ""


# ---------------------------------------------------------------------------
# Rust Scanner
# ---------------------------------------------------------------------------
