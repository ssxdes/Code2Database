"""unified_id — cross-language unified node ID generation.

Per cgdb-architecture-and-poc-report.md 5.6, every node in cgdb_nodes
shares a single 60-bit ID space. The clang path uses USR-based hashing
(see clang_scanner.cgdb_node_id); non-C/C++ languages don't have a USR
concept, so this module provides a language-aware equivalent:

  - Go:      fully-qualified name (package.Func or package.Type.Method)
  - Rust:    path-qualified name (crate::module::fn_name)
  - Python:  module-qualified name (pkg.module.Class.method)
  - Java:    package-qualified name (com.example.Class.method)
  - ASM:     file-local label / file-scope symbol

Each language builds a "USR equivalent" string by combining the
language name + FQN + (optional) signature hash, then hashes it with
SHA-256 truncated to 60 bits (high bit clear) so it fits in SQLite's
signed INTEGER and doesn't collide with clang's USR-based IDs (which
use the same hashing scheme).

This means: a Go function `main.foo` and a C function `foo` in the
same project get different cgdb_node_ids, even if their names match —
the language prefix prevents cross-language collisions while keeping
IDs stable across scans.
"""
import hashlib
from typing import Optional


# High bit clear so IDs fit in SQLite's signed 64-bit INTEGER.
_AST_NODE_MASK = 0x7FFF_FFFF_FFFF_FFFF


def unified_node_id(language: str, fqn: str,
                     signature: str = "",
                     byte_offset: Optional[int] = None) -> int:
    """Compute a stable cross-language node ID.

    Args:
      language: short code ('go', 'rust', 'python', 'java', 'asm', etc.)
      fqn: fully-qualified name (e.g., 'main.foo', 'pkg.mod.Class.method')
      signature: optional function signature (for overload disambiguation)
      byte_offset: optional byte offset in the source file (last-resort
        disambiguation when FQN+signature isn't unique, e.g., ASM labels)

    Returns:
      60-bit integer node ID (high bit clear).
    """
    if not fqn and byte_offset is None:
        return 0
    # Build a "USR equivalent" string: lang|fqn|sig|offset
    parts = [language, fqn or '']
    if signature:
        parts.append(f'sig:{signature}')
    if byte_offset is not None:
        parts.append(f'off:{byte_offset}')
    src = '|'.join(parts)
    h = hashlib.sha256(src.encode('utf-8')).hexdigest()[:16]
    return int(h, 16) & _AST_NODE_MASK


def unified_edge_id(src_id: int, dst_id: int, kind: str,
                     line: Optional[int] = None) -> int:
    """Compute a stable cross-language edge ID.

    Args:
      src_id: source node ID
      dst_id: destination node ID
      kind: edge kind ('INVOKES', 'HAS_FIELD', 'IMPLEMENTS', etc.)
      line: optional source line number (for distinguishing multiple
        same-kind edges between the same pair, e.g., repeated calls)

    Returns:
      60-bit integer edge ID (high bit clear).
    """
    parts = [str(src_id), str(dst_id), kind]
    if line is not None:
        parts.append(f'L{line}')
    src = '|'.join(parts)
    h = hashlib.sha256(src.encode('utf-8')).hexdigest()[:16]
    return int(h, 16) & _AST_NODE_MASK


# ---- Language-specific helpers ----

def go_node_id(package: str, func_name: str,
                signature: str = "") -> int:
    """Go: fqn = package.FuncName or package.TypeName.MethodName."""
    fqn = f'{package}.{func_name}' if package else func_name
    return unified_node_id('go', fqn, signature=signature)


def rust_node_id(crate: str, mod_path: list, func_name: str,
                  signature: str = "") -> int:
    """Rust: fqn = crate::mod1::mod2::func_name."""
    parts = [crate] + list(mod_path) + [func_name]
    fqn = '::'.join(p for p in parts if p)
    return unified_node_id('rust', fqn, signature=signature)


def python_node_id(module: str, class_path: list, func_name: str,
                    signature: str = "") -> int:
    """Python: fqn = module.ClassName.method_name."""
    parts = [module] + list(class_path) + [func_name]
    fqn = '.'.join(p for p in parts if p)
    return unified_node_id('python', fqn, signature=signature)


def java_node_id(package: str, class_path: list, func_name: str,
                  signature: str = "") -> int:
    """Java: fqn = package.ClassName.methodName."""
    parts = [package] + list(class_path) + [func_name]
    fqn = '.'.join(p for p in parts if p)
    return unified_node_id('java', fqn, signature=signature)


def asm_node_id(filepath: str, label: str,
                 byte_offset: Optional[int] = None) -> int:
    """ASM: fqn = filepath|label, plus byte_offset for disambiguation."""
    fqn = f'{filepath}|{label}' if label else filepath
    return unified_node_id('asm', fqn, byte_offset=byte_offset)


# ---- File ID (shared across languages) ----

def unified_file_id(filepath: str) -> int:
    """Compute a stable file ID from a file path.

    Same scheme as cgdb_ingest.file_id_for: SHA-256 of the path
    truncated to 60 bits.
    """
    h = hashlib.sha256(filepath.encode('utf-8')).hexdigest()[:15]
    return int(h, 16) & 0x0FFF_FFFF_FFFF_FFFF
