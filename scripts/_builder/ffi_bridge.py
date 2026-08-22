#!/usr/bin/env python3
"""Cross-language FFI boundary modeling.

Models function calls that cross language boundaries via FFI
(Foreign Function Interface). The existing graph merges INVOKES edges
across languages (e.g., ASM → C), but doesn't model:

1. **FFI edges** as a distinct edge type — needed because FFI calls
   have different semantics (type marshalling, GIL release in Python,
   panic-safety in Rust).
2. **Type marshalling** — Python `int` → C `long` may lose precision
   on 32-bit; Python `bytes` → C `char*` requires null-termination.
3. **Error propagation** — C returns `-EINVAL`, Python raises
   `OSError(EINVAL)`, Rust returns `Err(io::Error)`. Mapping these
   lets the engineer trace "which C error code becomes which Python
   exception".

Supports three FFI mechanisms:
- Python: ctypes (CDLL/WinDLL), cffi, pybind11 (cdef/verify)
- Go: cgo (import "C", //go:cgo_import)
- Rust: extern "C" blocks, #[no_mangle] exports

Each FFI detection produces an FFI_EDGE with:
    {
        "caller": "python:foo.py:bar",  # the FFI caller
        "callee": "c:lib.so:cfun",      # the C function being called
        "relation": "FFI",
        "ffi_mechanism": "ctypes" | "cgo" | "extern_c" | "cffi" | "pybind11",
        "type_mapping": [
            {"from": "int", "to": "long", "lossy": false},
            ...
        ],
        "error_mapping": "errno_to_oserror" | "errno_to_result" | "none",
        "line": N,
        "evidence": "ctypes.CDLL('libfoo.so').bar(argtypes=[c_int])",
    }

CLI commands:
    ffi-detect --graph <dir> --source <root>   # scan and add FFI edges
    ffi-list --graph <dir>                     # list all FFI edges
    ffi-trace --graph <dir> --node <id>        # trace FFI call chain
    ffi-types --graph <dir> --from int --to long  # find type mappings
"""

import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any, Set, Tuple

from _builder.line_utils import build_line_starts, line_for_offset


# ---------------------------------------------------------------------------
# Type marshalling tables
# ---------------------------------------------------------------------------

# Python ctypes → C type mapping
_PY_CTYPE_TO_C = {
    "c_int": "int",
    "c_long": "long",
    "c_longlong": "long long",
    "c_uint": "unsigned int",
    "c_ulong": "unsigned long",
    "c_ulonglong": "unsigned long long",
    "c_short": "short",
    "c_ushort": "unsigned short",
    "c_char": "char",
    "c_char_p": "char*",
    "c_wchar": "wchar_t",
    "c_wchar_p": "wchar_t*",
    "c_void_p": "void*",
    "c_float": "float",
    "c_double": "double",
    "c_bool": "_Bool",
    "c_size_t": "size_t",
    "c_ssize_t": "ssize_t",
    "c_int8": "int8_t",
    "c_uint8": "uint8_t",
    "c_int16": "int16_t",
    "c_uint16": "uint16_t",
    "c_int32": "int32_t",
    "c_uint32": "uint32_t",
    "c_int64": "int64_t",
    "c_uint64": "uint64_t",
}

# Lossy type conversions (potential bug sources)
_LOSSY_CONVERSIONS = {
    ("float", "int"),  # truncation
    ("double", "int"),  # truncation
    ("long", "int"),  # narrowing on 32-bit
    ("long long", "int"),  # narrowing
    ("unsigned int", "int"),  # sign loss
    ("unsigned long", "int"),  # sign + width loss
    ("void*", "int"),  # pointer-to-int truncation
}

# Go cgo → C type mapping
_GO_CGO_TO_C = {
    "C.int": "int",
    "C.long": "long",
    "C.short": "short",
    "C.char": "char",
    "C.uchar": "unsigned char",
    "C.uint": "unsigned int",
    "C.ulong": "unsigned long",
    "C.float": "float",
    "C.double": "double",
    "C.size_t": "size_t",
    "C.longlong": "long long",
    "C.ulonglong": "unsigned long long",
}

# Rust extern "C" → C type mapping
_RUST_TO_C = {
    "i8": "int8_t",
    "i16": "int16_t",
    "i32": "int32_t",
    "i64": "int64_t",
    "isize": "ssize_t",
    "u8": "uint8_t",
    "u16": "uint16_t",
    "u32": "uint32_t",
    "u64": "uint64_t",
    "usize": "size_t",
    "f32": "float",
    "f64": "double",
    "c_char": "char",
    "c_int": "int",
    "c_long": "long",
    "c_void": "void",
    "c_double": "double",
    "*const": "const*",
    "*mut": "*",
}


# ---------------------------------------------------------------------------
# D22: Platform ABI models — type sizes/alignments differ across ABIs
# ---------------------------------------------------------------------------

# Common platform ABIs: type name → byte width
# Linux/macOS LP64: long=8, pointer=8, int=4
# Windows LLP64: long=4, pointer=8, int=4
# 32-bit ILP32: long=4, pointer=4, int=4
PLATFORM_ABIS = {
    "lp64": {  # Linux, macOS (x86-64, arm64)
        "char": 1, "short": 2, "int": 4, "long": 8, "long long": 8,
        "float": 4, "double": 8, "long double": 16,
        "void*": 8, "size_t": 8, "ssize_t": 8, "intptr_t": 8,
        "int8_t": 1, "uint8_t": 1, "int16_t": 2, "uint16_t": 2,
        "int32_t": 4, "uint32_t": 4, "int64_t": 8, "uint64_t": 8,
        "wchar_t": 4,
    },
    "llp64": {  # Windows x64
        "char": 1, "short": 2, "int": 4, "long": 4, "long long": 8,
        "float": 4, "double": 8, "long double": 8,
        "void*": 8, "size_t": 8, "ssize_t": 8, "intptr_t": 8,
        "int8_t": 1, "uint8_t": 1, "int16_t": 2, "uint16_t": 2,
        "int32_t": 4, "uint32_t": 4, "int64_t": 8, "uint64_t": 8,
        "wchar_t": 2,
    },
    "ilp32": {  # 32-bit Linux/Windows
        "char": 1, "short": 2, "int": 4, "long": 4, "long long": 8,
        "float": 4, "double": 8, "long double": 12,
        "void*": 4, "size_t": 4, "ssize_t": 4, "intptr_t": 4,
        "int8_t": 1, "uint8_t": 1, "int16_t": 2, "uint16_t": 2,
        "int32_t": 4, "uint32_t": 4, "int64_t": 8, "uint64_t": 8,
        "wchar_t": 4,
    },
}

# Default platform inferred at runtime (can be overridden via env var)
_DEFAULT_PLATFORM = "lp64"
if sys.platform.startswith("win"):
    _DEFAULT_PLATFORM = "llp64"
elif sys.maxsize <= 2**32:
    _DEFAULT_PLATFORM = "ilp32"


def _platform_abi(name: str = "") -> Dict[str, int]:
    """Get the type-width table for a platform ABI.

    Falls back to lp64 if unknown name given.
    """
    if not name:
        name = os.environ.get("CALLGRAPH_FFI_ABI", _DEFAULT_PLATFORM)
    return PLATFORM_ABIS.get(name, PLATFORM_ABIS["lp64"])


def is_lossy_conversion(src_type: str, dst_type: str,
                          platform: str = "") -> bool:
    """Determine if converting src_type → dst_type is lossy on the given ABI.

    A conversion is lossy when:
    - dst width < src width (narrowing)
    - signed/unsigned mismatch with high-bit information loss
    - pointer cast to integer with smaller width
    """
    abi = _platform_abi(platform)
    src_w = abi.get(src_type, 0)
    dst_w = abi.get(dst_type, 0)
    if src_w == 0 or dst_w == 0:
        # Unknown type — fall back to hardcoded lossy table
        return (src_type, dst_type) in _LOSSY_CONVERSIONS
    if dst_w < src_w:
        return True
    # Sign loss: unsigned → signed of same width
    src_unsigned = src_type.startswith("unsigned") or src_type.startswith("u")
    dst_signed = not (dst_type.startswith("unsigned") or dst_type.startswith("u"))
    if src_unsigned and dst_signed and src_w == dst_w:
        return True
    # Pointer → int conversion
    if "*" in src_type and "int" in dst_type and dst_w < src_w:
        return True
    return False


def _infer_py_arg_type(arg_text: str) -> str:
    """Infer the Python ctypes type for a Python argument expression.

    Used when argtypes is not specified — we infer from how the Python
    caller constructs the argument.
    """
    arg_text = arg_text.strip()
    if not arg_text:
        return "c_void_p"
    # bytes literal → c_char_p
    if arg_text.startswith(("b'", 'b"')):
        return "c_char_p"
    # str literal → c_wchar_p (or c_char_p depending on codec)
    if arg_text.startswith(("'", '"')):
        return "c_char_p"
    # None → c_void_p (NULL)
    if arg_text == "None":
        return "c_void_p"
    # True/False → c_bool
    if arg_text in ("True", "False"):
        return "c_bool"
    # Float literal (has '.') → c_double
    if "." in arg_text and not arg_text.startswith(("b'", 'b"')):
        try:
            float(arg_text.rstrip("f"))
            return "c_double"
        except ValueError:
            pass
    # Integer literal → c_int (small) or c_longlong (big)
    try:
        val = int(arg_text, 0)
        if -2**31 <= val < 2**31:
            return "c_int"
        return "c_longlong"
    except ValueError:
        pass
    # ctypes explicit construction: ctypes.c_int(42) → c_int
    m = re.match(r'(?:ctypes\.)?(c_\w+)\s*\(', arg_text)
    if m:
        return m.group(1)
    # Variable name heuristic — defaults to c_void_p (pointer/opaque)
    return "c_void_p"


def infer_signature_from_call(call_text: str, platform: str = "") -> List[Dict]:
    """Infer a type_mapping list from a Python ctypes call expression.

    When `argtypes` is not specified, we infer each argument's type
    from the call site's actual argument expression. Returns a list
    of {from, to, lossy, inferred} dicts.
    """
    abi = _platform_abi(platform)
    # Find the first '(' and balance to the matching ')'
    open_idx = call_text.find("(")
    if open_idx < 0:
        return []
    depth = 0
    end_idx = -1
    for i in range(open_idx, len(call_text)):
        c = call_text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx < 0:
        return []
    args_str = call_text[open_idx + 1:end_idx].strip()
    if not args_str:
        return []
    # Split top-level commas (handles nested calls)
    args = []
    depth = 0
    cur = ""
    for c in args_str:
        if c == "(":
            depth += 1
            cur += c
        elif c == ")":
            depth -= 1
            cur += c
        elif c == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += c
    if cur.strip():
        args.append(cur.strip())
    type_mapping = []
    for a in args:
        py_type = _infer_py_arg_type(a)
        c_type = _PY_CTYPE_TO_C.get(py_type, py_type)
        type_mapping.append({
            "from": py_type,
            "to": c_type,
            "lossy": is_lossy_conversion(py_type, c_type, platform),
            "inferred": True,
        })
    return type_mapping


# ---------------------------------------------------------------------------
# Python ctypes / cffi / pybind11 detection
# ---------------------------------------------------------------------------

# Pattern: ctypes.CDLL("path.so") or ctypes.WinDLL("path.dll")
_CTYPES_LOAD = re.compile(
    r'(?:ctypes\.)?(CDLL|WinDLL|OleDLL|PyDLL)\s*\(\s*[\'"]([^\'"]+)[\'"]'
)

# Pattern: lib = ctypes.CDLL(...); lib.func(argtypes=[...], restype=...)
# We capture the variable name and the loaded library
_CTYPES_VAR_ASSIGN = re.compile(
    r'(\w+)\s*=\s*(?:ctypes\.)?(?:CDLL|WinDLL|OleDLL|PyDLL)\s*\(\s*[\'"]([^\'"]+)[\'"]'
)

# Pattern: var.func_name(...) — calls into the loaded lib
_CTYPES_CALL = re.compile(
    r'(\w+)\.(\w+)\s*\('
)

# Pattern: argtypes=[c_int, c_char_p, ...]
_ARGTYPES = re.compile(
    r'argtypes\s*=\s*\[([^\]]+)\]'
)

# Pattern: restype=c_int or restype=ctypes.c_int
_RESTYPE = re.compile(
    r'restype\s*=\s*(?:ctypes\.)?(\w+)'
)

# Pattern: cffi cdef
_CFFI_CDEF = re.compile(
    r'cdef\s*\(\s*(?:["\']{3}|["\'])([^"\']+?)(?:["\']{3}|["\'])\s*\)',
    re.DOTALL
)

# Pattern: from pybind11 import / PYBIND11_MODULE
_PYBIND11_MODULE = re.compile(
    r'PYBIND11_MODULE\s*\(\s*(\w+)\s*,\s*\w+\s*\)'
)


def detect_python_ffi(file_text: str, file_path: str) -> List[Dict]:
    """Detect Python FFI calls (ctypes/cffi/pybind11) in a file.

    Returns a list of FFI edge dicts.
    """
    edges = []
    rel = file_path
    _line_starts = build_line_starts(file_text)

    # Track variable → library mapping (for ctypes)
    var_to_lib: Dict[str, str] = {}
    for m in _CTYPES_VAR_ASSIGN.finditer(file_text):
        var, lib = m.group(1), m.group(2)
        var_to_lib[var] = lib
        line_no = line_for_offset(_line_starts, m.start())
        edges.append({
            "caller": f"python:{rel}:load_{var}",
            "callee": f"c:{lib}",
            "relation": "FFI",
            "ffi_mechanism": "ctypes",
            "ffi_direction": "python_to_c",
            "type_mapping": [],
            "error_mapping": "errno_to_oserror",
            "line": line_no,
            "evidence": m.group(0).strip(),
            "source_file": rel,
        })

    # Find calls through the loaded variables
    for m in _CTYPES_CALL.finditer(file_text):
        var, func = m.group(1), m.group(2)
        if var not in var_to_lib:
            continue
        line_no = line_for_offset(_line_starts, m.start())
        # (search a 200-char window before the call)
        window_start = max(0, m.start() - 500)
        window = file_text[window_start:m.end() + 200]
        type_mapping = []
        explicit_argtypes = False
        for am in _ARGTYPES.finditer(window):
            explicit_argtypes = True
            arg_str = am.group(1)
            for arg in arg_str.split(","):
                arg = arg.strip()
                # Strip 'ctypes.' prefix
                if arg.startswith("ctypes."):
                    arg = arg[len("ctypes."):]
                c_type = _PY_CTYPE_TO_C.get(arg, arg)
                type_mapping.append({
                    "from": arg, "to": c_type,
                    "lossy": is_lossy_conversion(arg, c_type),
                })
        for rm in _RESTYPE.finditer(window):
            rtype = rm.group(1)
            if rtype.startswith("ctypes."):
                rtype = rtype[len("ctypes."):]
            c_rtype = _PY_CTYPE_TO_C.get(rtype, rtype)
            type_mapping.append({
                "from": rtype, "to": c_rtype, "lossy": False,
                "direction": "return",
            })
        # D22: signature inference — when argtypes is missing, infer
        # from the call-site argument expressions
        if not explicit_argtypes:
            # Get the full call text (var.func(args)) — balance parens
            # to capture nested calls in arguments
            call_start = m.start()
            open_idx = m.end() - 1  # the '(' position
            depth = 0
            call_end = open_idx
            for i in range(open_idx, len(file_text)):
                c = file_text[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        call_end = i + 1
                        break
            call_text = file_text[call_start:call_end]
            inferred = infer_signature_from_call(call_text)
            if inferred:
                type_mapping.extend(inferred)
        callee_lib = var_to_lib[var]
        edges.append({
            "caller": f"python:{rel}:unknown",  # caller function name unknown without AST
            "callee": f"c:{callee_lib}:{func}",
            "relation": "FFI",
            "ffi_mechanism": "ctypes",
            "ffi_direction": "python_to_c",
            "type_mapping": type_mapping,
            "error_mapping": "errno_to_oserror",
            "line": line_no,
            "evidence": m.group(0).strip(),
            "source_file": rel,
        })

    # cffi cdef
    for m in _CFFI_CDEF.finditer(file_text):
        line_no = line_for_offset(_line_starts, m.start())
        cdef_body = m.group(1).strip()
        edges.append({
            "caller": f"python:{rel}:cdef",
            "callee": f"c:cffi_decl",
            "relation": "FFI",
            "ffi_mechanism": "cffi",
            "ffi_direction": "python_to_c",
            "type_mapping": [],
            "error_mapping": "errno_to_oserror",
            "line": line_no,
            "evidence": f"cdef(...): {cdef_body[:80]}...",
            "source_file": rel,
            "cdef_declarations": cdef_body.split("\n")[:20],
        })

    # pybind11 module
    for m in _PYBIND11_MODULE.finditer(file_text):
        line_no = line_for_offset(_line_starts, m.start())
        mod = m.group(1)
        edges.append({
            "caller": f"python:{rel}:pybind11_{mod}",
            "callee": f"c++:pybind11:{mod}",
            "relation": "FFI",
            "ffi_mechanism": "pybind11",
            "ffi_direction": "python_to_cpp",
            "type_mapping": [],
            "error_mapping": "cpp_exception_to_py_exception",
            "line": line_no,
            "evidence": m.group(0).strip(),
            "source_file": rel,
        })

    return edges


# ---------------------------------------------------------------------------
# Go cgo detection
# ---------------------------------------------------------------------------

# Pattern: import "C"
_CGO_IMPORT = re.compile(r'import\s+"C"')

# Pattern: // #cgo LDFLAGS: -L./lib -lfoo
_CGO_LDFLAGS = re.compile(r'//\s*#cgo\s+LDFLAGS:\s*([^\n]+)')

# Pattern: C.function_name(...) — calls into C
_CGO_CALL = re.compile(r'\bC\.(\w+)\s*\(')

# Pattern: // #cgo pkg-config: foo
_CGO_PKG_CONFIG = re.compile(r'//\s*#cgo\s+pkg-config:\s*([^\n]+)')


def detect_go_cgo_ffi(file_text: str, file_path: str) -> List[Dict]:
    """Detect Go cgo FFI calls in a file.

    Returns a list of FFI edge dicts.
    """
    if not _CGO_IMPORT.search(file_text):
        return []

    edges = []
    rel = file_path
    _line_starts = build_line_starts(file_text)

    # Find C function calls
    for m in _CGO_CALL.finditer(file_text):
        func = m.group(1)
        if func in ("C.int", "C.char"):  # type cast, not call
            continue
        line_no = line_for_offset(_line_starts, m.start())
        # Look for type info in surrounding text
        window_start = max(0, m.start() - 200)
        window = file_text[window_start:m.end() + 200]
        type_mapping = []
        # Find C.<type> references (these are type conversions)
        for tm in re.finditer(r'C\.(\w+)', window):
            t = tm.group(1)
            c_type = _GO_CGO_TO_C.get(f"C.{t}", None)
            if c_type:
                type_mapping.append({
                    "from": f"C.{t}", "to": c_type,
                    "lossy": False,
                })
        edges.append({
            "caller": f"go:{rel}:unknown",
            "callee": f"c:unknown:{func}",
            "relation": "FFI",
            "ffi_mechanism": "cgo",
            "ffi_direction": "go_to_c",
            "type_mapping": type_mapping,
            "error_mapping": "errno_to_result",
            "line": line_no,
            "evidence": m.group(0).strip(),
            "source_file": rel,
        })

    # Find cgo LDFLAGS (linking info)
    for m in _CGO_LDFLAGS.finditer(file_text):
        line_no = line_for_offset(_line_starts, m.start())
        edges.append({
            "caller": f"go:{rel}:cgo_link",
            "callee": f"c:linker",
            "relation": "FFI",
            "ffi_mechanism": "cgo",
            "ffi_direction": "go_to_c",
            "type_mapping": [],
            "error_mapping": "none",
            "line": line_no,
            "evidence": m.group(0).strip(),
            "ldflags": m.group(1).strip(),
            "source_file": rel,
        })

    return edges


# ---------------------------------------------------------------------------
# Rust extern "C" detection
# ---------------------------------------------------------------------------

# Pattern: extern "C" { fn foo(...); }
_EXTERN_C_BLOCK = re.compile(
    r'extern\s*"C"\s*\{([^}]+)\}',
    re.DOTALL
)

# Pattern: fn inside extern block
_EXTERN_FN = re.compile(
    r'(?:pub\s+)?fn\s+(\w+)\s*\(([^)]*)\)(?:\s*->\s*([^;{]+))?'
)

# Pattern: #[no_mangle] pub extern "C" fn foo(...) — Rust exports
_NO_MANGLE_EXPORT = re.compile(
    r'#\[\s*no_mangle\s*\]\s*(?:pub\s+)?extern\s*"C"\s+fn\s+(\w+)\s*\(([^)]*)\)(?:\s*->\s*([^;{]+))?'
)


def detect_rust_ffi(file_text: str, file_path: str) -> List[Dict]:
    """Detect Rust extern "C" FFI in a file.

    Returns a list of FFI edge dicts.
    """
    edges = []
    rel = file_path
    _line_starts = build_line_starts(file_text)
    _block_line_starts_cache: Dict[int, List[int]] = {}

    # extern "C" { ... } blocks — Rust calling INTO C
    for m in _EXTERN_C_BLOCK.finditer(file_text):
        block = m.group(1)
        block_line = line_for_offset(_line_starts, m.start())
        block_starts = _block_line_starts_cache.get(id(block))
        if block_starts is None:
            block_starts = build_line_starts(block)
            _block_line_starts_cache[id(block)] = block_starts
        for fm in _EXTERN_FN.finditer(block):
            func, args, ret = fm.group(1), fm.group(2), fm.group(3)
            line_no = block_line + line_for_offset(block_starts, fm.start()) - 1
            # Parse args
            type_mapping = []
            for arg in args.split(","):
                arg = arg.strip()
                if ":" in arg:
                    _, rust_type = arg.split(":", 1)
                    rust_type = rust_type.strip()
                    c_type = _map_rust_to_c(rust_type)
                    type_mapping.append({
                        "from": rust_type, "to": c_type,
                        "lossy": (rust_type, c_type) in _LOSSY_CONVERSIONS,
                    })
            if ret:
                ret = ret.strip()
                c_ret = _map_rust_to_c(ret)
                type_mapping.append({
                    "from": ret, "to": c_ret, "lossy": False,
                    "direction": "return",
                })
            edges.append({
                "caller": f"rust:{rel}:unknown",
                "callee": f"c:unknown:{func}",
                "relation": "FFI",
                "ffi_mechanism": "extern_c",
                "ffi_direction": "rust_to_c",
                "type_mapping": type_mapping,
                "error_mapping": "errno_to_result",
                "line": line_no,
                "evidence": f"extern \"C\" {{ fn {func}(...); }}",
                "source_file": rel,
            })

    # #[no_mangle] exports — C calling INTO Rust
    for m in _NO_MANGLE_EXPORT.finditer(file_text):
        func, args, ret = m.group(1), m.group(2), m.group(3)
        line_no = line_for_offset(_line_starts, m.start())
        type_mapping = []
        for arg in args.split(","):
            arg = arg.strip()
            if ":" in arg:
                _, rust_type = arg.split(":", 1)
                rust_type = rust_type.strip()
                c_type = _map_rust_to_c(rust_type)
                type_mapping.append({
                    "from": c_type, "to": rust_type,  # caller (C) passes, callee (Rust) receives
                    "lossy": (c_type, rust_type) in _LOSSY_CONVERSIONS,
                })
        if ret:
            ret = ret.strip()
            c_ret = _map_rust_to_c(ret)
            type_mapping.append({
                "from": ret, "to": c_ret, "lossy": False,
                "direction": "return",
            })
        edges.append({
            "caller": f"c:unknown:{func}",
            "callee": f"rust:{rel}:{func}",
            "relation": "FFI",
            "ffi_mechanism": "extern_c",
            "ffi_direction": "c_to_rust",
            "type_mapping": type_mapping,
            "error_mapping": "result_to_errno",
            "line": line_no,
            "evidence": m.group(0).strip()[:200],
            "source_file": rel,
            "is_export": True,  # Rust function exported to C
        })

    return edges


def _map_rust_to_c(rust_type: str) -> str:
    """Map a Rust type to its C equivalent."""
    rust_type = rust_type.strip()
    # Handle pointers
    if rust_type.startswith("*const") or rust_type.startswith("*mut"):
        base = rust_type.replace("*const", "").replace("*mut", "").strip()
        c_base = _RUST_TO_C.get(base, base)
        return f"const {c_base}*" if "const" in rust_type else f"{c_base}*"
    return _RUST_TO_C.get(rust_type, rust_type)


# ---------------------------------------------------------------------------
# Whole-source-tree FFI detection
# ---------------------------------------------------------------------------

def detect_all_ffi(source_root: str) -> List[Dict]:
    """Walk a source tree and detect all FFI edges.

    Scans .py / .go / .rs files for ctypes/cgo/extern "C" patterns.
    Returns a flat list of FFI edge dicts.
    """
    all_edges = []
    ext_to_handler = {
        ".py": detect_python_ffi,
        ".go": detect_go_cgo_ffi,
        ".rs": detect_rust_ffi,
    }
    for dirpath, dirnames, filenames in os.walk(source_root):
        # Skip common noise directories
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in
                       ("__pycache__", "node_modules", "build", "target",
                        "venv", ".venv")]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            handler = ext_to_handler.get(ext)
            if not handler:
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                text = Path(fpath).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = os.path.relpath(fpath, source_root)
            edges = handler(text, rel)
            all_edges.extend(edges)
    return all_edges


# ---------------------------------------------------------------------------
# Attach FFI edges to the graph
# ---------------------------------------------------------------------------

def attach_ffi_edges(G, ffi_edges: List[Dict]) -> int:
    """Add FFI edges to the NetworkX graph.

    Returns the number of edges added.
    """
    added = 0
    for edge in ffi_edges:
        caller = edge["caller"]
        callee = edge["callee"]
        # Add nodes if they don't exist (FFI nodes are placeholders)
        if caller not in G:
            G.add_node(caller, name=caller, node_type="ffi_boundary",
                       is_empty=False, source_file=edge.get("source_file", ""),
                       line=edge.get("line", 0), domain="ffi",
                       labels=["ffi_boundary"])
        if callee not in G:
            G.add_node(callee, name=callee, node_type="ffi_boundary",
                       is_empty=False, source_file=edge.get("source_file", ""),
                       line=edge.get("line", 0), domain="ffi",
                       labels=["ffi_boundary"])
        # Add the edge
        G.add_edge(caller, callee, **edge)
        added += 1
    return added


# ---------------------------------------------------------------------------
# SQLite persistence (RPT-P1-17)
# ---------------------------------------------------------------------------

# ffi_mechanism (used in edge dict) → cross_lang_bindings.ffi_kind (schema CHECK)
# Both use the same vocabulary, but we normalize defensively.
_FFI_MECHANISM_TO_KIND = {
    "ctypes": "ctypes",
    "cffi": "cffi",
    "pybind11": "pybind11",
    "cython": "cython",
    "cgo": "cgo",
    "extern_c": "extern_c",
    "bindgen": "bindgen",
    "cbindgen": "cbindgen",
    "jni": "jni",
    "jna": "jna",
    "napi": "napi",
    "emscripten": "emscripten",
    "wasm": "wasm",
}


def _parse_ffi_symbol_id(symbol_id: str) -> Dict[str, str]:
    """Parse an FFI edge caller/callee string like 'python:foo.py:bar' into
    {language, file_path, function_name}.

    Format conventions (see detect_python_ffi / detect_go_cgo_ffi / detect_rust_ffi):
      - 'python:<rel_path>:<func_name>'   (ctypes/cffi/pybind11)
      - 'python:<rel_path>:load_<var>'    (ctypes library load site)
      - 'python:<rel_path>:unknown'       (ctypes call with unknown caller func)
      - 'c:<lib_name>:<func_name>'        (C library symbol)
      - 'c:<lib_name>'                    (whole-library binding)
      - 'c:cffi_decl'                     (cffi cdef declaration)
      - 'go:<rel_path>:<func_name>'       (cgo call site)
      - 'cgo:<rel_path>:<func_name>'      (cgo wrapper)
      - 'rust:<rel_path>:<func_name>'     (extern "C" block)
      - 'c:<crate>:<func_name>'           (Rust extern target)

    Missing fields default to ''.
    """
    parts = symbol_id.split(":", 2)
    result = {"language": "", "file_path": "", "function_name": ""}
    if len(parts) >= 1 and parts[0]:
        result["language"] = parts[0]
    if len(parts) >= 2:
        result["file_path"] = parts[1]
    if len(parts) >= 3:
        result["function_name"] = parts[2]
    return result


def _resolve_or_create_symbol(conn, symbol_id: str, default_kind: str = "function",
                              default_line: int = 0) -> Optional[int]:
    """Resolve an FFI symbol string ID to a cgdb_nodes.id.

    Strategy:
      1. Parse 'lang:file_path:func_name' format.
      2. If file_path matches a cgdb_files row (by LIKE '%file_path%'),
         look up cgdb_nodes with matching name AND file_id.
      3. If function_name is 'unknown' / 'load_*' / empty, fall back to
         file-level symbol (any node in that file).
      4. If still not found, create a placeholder cgdb_nodes row with
         kind=default_kind, source_layer='analysis', so the cross_lang_bindings
         FK is satisfied. The placeholder's fqn is the original symbol_id.

    Returns the cgdb_nodes.id, or None if resolution failed and placeholder
    creation also failed (e.g., due to a constraint violation).
    """
    parsed = _parse_ffi_symbol_id(symbol_id)
    func_name = parsed["function_name"]
    file_path = parsed["file_path"]

    # Case 1: have both function name and file path — look up by name+file
    if func_name and file_path and not func_name.startswith("load_") and func_name != "unknown":
        row = conn.execute(
            "SELECT n.id FROM cgdb_nodes n "
            "JOIN cgdb_files f ON n.file_id = f.id "
            "WHERE n.name = ? AND (f.path = ? OR f.path LIKE ?) "
            "AND n.kind IN ('function','method','var','field','enum_constant') "
            "LIMIT 1",
            (func_name, file_path, f"%/{file_path}")
        ).fetchone()
        if row:
            return row[0]

    # Case 2: have file path only — look up any function node in that file
    if file_path:
        row = conn.execute(
            "SELECT n.id FROM cgdb_nodes n "
            "JOIN cgdb_files f ON n.file_id = f.id "
            "WHERE (f.path = ? OR f.path LIKE ?) "
            "AND n.kind IN ('function','method') "
            "ORDER BY n.line LIMIT 1",
            (file_path, f"%/{file_path}")
        ).fetchone()
        if row:
            return row[0]

    # Case 3: function_name only (no file_path) — look up by name across all files
    if func_name and not func_name.startswith("load_") and func_name != "unknown":
        row = conn.execute(
            "SELECT id FROM cgdb_nodes "
            "WHERE name = ? AND kind IN ('function','method','var','field') "
            "LIMIT 1",
            (func_name,)
        ).fetchone()
        if row:
            return row[0]

    # Case 4: create a placeholder cgdb_nodes row so the FK is satisfiable.
    # Use the full symbol_id as the fqn for traceability.
    try:
        cur = conn.execute(
            "INSERT INTO cgdb_nodes (kind, name, fqn, line, source_layer, confidence, "
            "first_seen_version, last_seen_version, commit_hash, attrs) "
            "VALUES (?, ?, ?, ?, 'analysis', 0.5, 1, 1, 'ffi-detector', ?)",
            (default_kind, func_name or symbol_id, symbol_id, default_line,
             json.dumps({"ffi_placeholder": True, "source_symbol_id": symbol_id}))
        )
        return cur.lastrowid
    except Exception as _e:
        print(f"[ffi] WARNING: failed to create placeholder for {symbol_id!r}: {_e}",
              file=sys.stderr)
        return None


def persist_ffi_to_sqlite(conn, ffi_edges: List[Dict],
                          clear_existing: bool = True) -> Dict[str, int]:
    """Persist FFI edges into the 3 cross-language bridge tables.

    Tables populated (cgdb schema v4):
      - cross_lang_bindings: one row per FFI binding (caller → callee)
      - type_mappings: one row per (from_type, to_type) pair in edge.type_mapping
      - ffi_call_sites: one row per FFI call site (file_id + line + binding_id)

    Args:
      conn: open sqlite3.Connection to code2database.db
      ffi_edges: list of FFI edge dicts (output of detect_all_ffi)
      clear_existing: if True, DELETE existing rows from the 3 tables first
                      (idempotent re-runs of ffi-detect --apply)

    Returns: {{"bindings": N, "type_mappings": M, "call_sites": K, "skipped": S}}
    """
    stats = {"bindings": 0, "type_mappings": 0, "call_sites": 0, "skipped": 0}

    if clear_existing:
        # Clear in FK-safe order: ffi_call_sites first (references bindings),
        # then cross_lang_bindings. type_mappings has no FK to bindings.
        conn.execute("DELETE FROM ffi_call_sites")
        conn.execute("DELETE FROM cross_lang_bindings")
        conn.execute("DELETE FROM type_mappings")
        conn.commit()

    for edge in ffi_edges:
        caller_id_str = edge.get("caller", "")
        callee_id_str = edge.get("callee", "")
        mechanism = edge.get("ffi_mechanism", "other")
        ffi_kind = _FFI_MECHANISM_TO_KIND.get(mechanism, "other")
        line_no = int(edge.get("line", 0) or 0)
        source_file_rel = edge.get("source_file", "")
        evidence = edge.get("evidence", "")

        # Resolve caller and callee to cgdb_nodes.id integers
        from_symbol_id = _resolve_or_create_symbol(conn, caller_id_str)
        to_symbol_id = _resolve_or_create_symbol(conn, callee_id_str)
        if from_symbol_id is None or to_symbol_id is None:
            stats["skipped"] += 1
            continue

        # Determine calling_convention from ffi_direction (best-effort)
        direction = edge.get("ffi_direction", "")
        if "python_to_c" in direction or "go_to_c" in direction or "rust_to_c" in direction:
            calling_convention = "cdecl"
        else:
            calling_convention = "cdecl"

        # Insert cross_lang_bindings row
        try:
            cur = conn.execute(
                "INSERT INTO cross_lang_bindings "
                "(from_symbol_id, to_symbol_id, ffi_kind, calling_convention, "
                "binding_source, confidence, aligned) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (from_symbol_id, to_symbol_id, ffi_kind, calling_convention,
                 source_file_rel or evidence[:200], 1.0, 1)
            )
            binding_id = cur.lastrowid
            stats["bindings"] += 1
        except Exception as _e:
            print(f"[ffi] WARNING: failed to insert cross_lang_bindings for "
                  f"{caller_id_str!r} → {callee_id_str!r}: {_e}", file=sys.stderr)
            stats["skipped"] += 1
            continue

        # Insert type_mappings rows (one per {from, to, lossy} entry)
        for tm in edge.get("type_mapping", []) or []:
            from_type = str(tm.get("from", "") or "")
            to_type = str(tm.get("to", "") or "")
            lossy = bool(tm.get("lossy", False))
            if not from_type and not to_type:
                continue
            mapping_kind = "lossy" if lossy else "marshalling"
            marshalling_cost = "lossy" if lossy else "none"
            try:
                # Resolve type_ids via cgdb_types (may be NULL — schema allows)
                from_type_id = None
                to_type_id = None
                if from_type:
                    row = conn.execute(
                        "SELECT id FROM cgdb_types WHERE spelling = ? LIMIT 1",
                        (from_type,)
                    ).fetchone()
                    if row:
                        from_type_id = row[0]
                if to_type:
                    row = conn.execute(
                        "SELECT id FROM cgdb_types WHERE spelling = ? LIMIT 1",
                        (to_type,)
                    ).fetchone()
                    if row:
                        to_type_id = row[0]
                conn.execute(
                    "INSERT INTO type_mappings "
                    "(from_type_id, to_type_id, from_type_spelling, to_type_spelling, "
                    "from_language, to_language, mapping_kind, marshalling_cost, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (from_type_id, to_type_id, from_type, to_type,
                     _parse_ffi_symbol_id(caller_id_str)["language"] or "unknown",
                     _parse_ffi_symbol_id(callee_id_str)["language"] or "c",
                     mapping_kind, marshalling_cost,
                     f"ffi_kind={ffi_kind}; direction={direction}")
                )
                stats["type_mappings"] += 1
            except Exception as _e:
                print(f"[ffi] WARNING: failed to insert type_mapping "
                      f"{from_type!r}→{to_type!r}: {_e}", file=sys.stderr)

        # Insert ffi_call_sites row (resolve file_id from source_file)
        if source_file_rel:
            file_row = conn.execute(
                "SELECT id FROM cgdb_files WHERE path = ? OR path LIKE ? LIMIT 1",
                (source_file_rel, f"%/{source_file_rel}")
            ).fetchone()
            if file_row:
                file_id = file_row[0]
                try:
                    conn.execute(
                        "INSERT INTO ffi_call_sites "
                        "(binding_id, file_id, line, col, at_token_id, "
                        "cross_lang_call_edge_id, marshalling_data_symbol_id) "
                        "VALUES (?, ?, ?, 0, NULL, NULL, NULL)",
                        (binding_id, file_id, line_no)
                    )
                    stats["call_sites"] += 1
                except Exception as _e:
                    print(f"[ffi] WARNING: failed to insert ffi_call_site for "
                          f"binding_id={binding_id}: {_e}", file=sys.stderr)

    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def list_ffi_edges(G) -> List[Dict]:
    """List all FFI edges in the graph."""
    edges = []
    for u, v, ed in G.edges(data=True):
        if ed.get("relation") != "FFI":
            continue
        edges.append({"caller": u, "callee": v, **ed})
    return edges


def trace_ffi_chain(G, start_id: str, max_depth: int = 10) -> Dict:
    """Trace the FFI call chain starting from a node.

    Walks both directions through FFI edges to find the full chain
    of language crossings.
    """
    visited = set()
    chain = []
    queue = [(start_id, 0)]
    while queue:
        cur, depth = queue.pop(0)
        if depth >= max_depth or cur in visited:
            continue
        visited.add(cur)
        chain.append({
            "node": cur,
            "name": G.nodes[cur].get("name", cur) if cur in G else cur,
            "depth": depth,
        })
        # Forward FFI edges
        for succ in G.successors(cur):
            ed = G.get_edge_data(cur, succ) or {}
            if ed.get("relation") == "FFI":
                queue.append((succ, depth + 1))
        # Reverse FFI edges
        for pred in G.predecessors(cur):
            ed = G.get_edge_data(pred, cur) or {}
            if ed.get("relation") == "FFI":
                queue.append((pred, depth + 1))
    return {"start": start_id, "chain": chain, "length": len(chain)}


def find_type_mappings(G, from_type: str = "", to_type: str = "") -> List[Dict]:
    """Find FFI edges with type mappings matching the given patterns."""
    results = []
    for u, v, ed in G.edges(data=True):
        if ed.get("relation") != "FFI":
            continue
        for tm in ed.get("type_mapping", []) or []:
            if from_type and from_type not in tm.get("from", ""):
                continue
            if to_type and to_type not in tm.get("to", ""):
                continue
            results.append({
                "caller": u, "callee": v,
                "from": tm.get("from"), "to": tm.get("to"),
                "lossy": tm.get("lossy", False),
                "direction": tm.get("direction", "arg"),
                "mechanism": ed.get("ffi_mechanism"),
                "line": ed.get("line"),
            })
    return results


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_ffi_detect(args):
    """Detect FFI edges across a source tree and write them to a JSON file.

    Usage: ffi-detect --graph <dir> --source <root> [--apply]
    """
    source_root = args.source
    graph_dir = args.graph
    edges = detect_all_ffi(source_root)

    # Group by mechanism for summary
    by_mechanism = defaultdict(int)
    for e in edges:
        by_mechanism[e["ffi_mechanism"]] += 1

    out_path = os.path.join(graph_dir, ".code2database_ffi.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "edge_count": len(edges),
            "by_mechanism": dict(by_mechanism),
            "edges": edges,
        }, f, ensure_ascii=False, indent=2, default=str)

    print(f"Detected {len(edges)} FFI edges across {len(by_mechanism)} mechanisms",
          file=sys.stderr)
    for mech, count in by_mechanism.items():
        print(f"  {mech}: {count}", file=sys.stderr)
    print(f"Wrote {out_path}", file=sys.stderr)

    if getattr(args, "apply", False):
        try:
            from _builder.graph_build import _load_full_graph, split_by_domain
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from _builder.graph_build import _load_full_graph, split_by_domain
        G = _load_full_graph(graph_dir)
        added = attach_ffi_edges(G, edges)
        # Write back
        master_path = os.path.join(graph_dir, "code2database_master.json")
        if os.path.exists(master_path):
            master = json.loads(open(master_path).read())
            split_by_domain(G, graph_dir, master.get("source_root", ""))
        print(f"Applied {added} FFI edges to graph", file=sys.stderr)

        # RPT-P1-17: Persist FFI edges to the 3 cross-language bridge tables
        # (cross_lang_bindings / type_mappings / ffi_call_sites) in SQLite.
        # This runs *after* attach_ffi_edges so any placeholder cgdb_nodes
        # rows created by the graph attach are already on disk; the
        # persist_ffi_to_sqlite function will resolve them by name/file.
        db_path = os.path.join(graph_dir, "code2database.db")
        if os.path.exists(db_path):
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(db_path, timeout=30.0)
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                stats = persist_ffi_to_sqlite(conn, edges, clear_existing=True)
                print(f"Persisted FFI to SQLite: "
                      f"{stats['bindings']} bindings, "
                      f"{stats['type_mappings']} type_mappings, "
                      f"{stats['call_sites']} call_sites, "
                      f"{stats['skipped']} skipped", file=sys.stderr)
            except Exception as _e:
                print(f"[ffi] WARNING: persist_ffi_to_sqlite failed: {_e}",
                      file=sys.stderr)
            finally:
                conn.close()
        else:
            print(f"[ffi] NOTE: code2database.db not found at {db_path}; "
                  f"skipping SQLite persist (graph JSON updated only)",
                  file=sys.stderr)


def cmd_ffi_persist(args):
    """Re-persist FFI edges from .code2database_ffi.json into the 3 SQLite
    cross-language bridge tables (cross_lang_bindings / type_mappings /
    ffi_call_sites).

    Usage: ffi-persist --graph <dir>
    """
    graph_dir = args.graph
    ffi_json_path = os.path.join(graph_dir, ".code2database_ffi.json")
    db_path = os.path.join(graph_dir, "code2database.db")
    if not os.path.exists(ffi_json_path):
        print(f"Error: {ffi_json_path} not found. Run `ffi-detect` first.",
              file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found. Build the graph first.",
              file=sys.stderr)
        sys.exit(1)

    with open(ffi_json_path, encoding="utf-8") as f:
        ffi_data = json.load(f)
    edges = ffi_data.get("edges", [])
    print(f"Loaded {len(edges)} FFI edges from {ffi_json_path}", file=sys.stderr)

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        stats = persist_ffi_to_sqlite(conn, edges, clear_existing=True)
        print(json.dumps({
            "status": "ok",
            "edge_count": len(edges),
            "bindings": stats["bindings"],
            "type_mappings": stats["type_mappings"],
            "call_sites": stats["call_sites"],
            "skipped": stats["skipped"],
        }, ensure_ascii=False, indent=2))
    except Exception as _e:
        print(f"Error: persist_ffi_to_sqlite failed: {_e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def cmd_ffi_list(args):
    """List all FFI edges in the graph.

    Usage: ffi-list --graph <dir> [--mechanism ctypes|cgo|extern_c]
    """
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    mechanism_filter = getattr(args, "mechanism", "")
    edges = list_ffi_edges(G)
    if mechanism_filter:
        edges = [e for e in edges if e.get("ffi_mechanism") == mechanism_filter]
    print(json.dumps({
        "ffi_edge_count": len(edges),
        "edges": edges,
    }, ensure_ascii=False, indent=2, default=str))


def cmd_ffi_trace(args):
    """Trace the FFI call chain from a node.

    Usage: ffi-trace --graph <dir> --node <id>
    """
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)

    from _builder.utils import _find_node_id
    node_id = _find_node_id(G, args.node)
    if not node_id:
        print(f"Node not found: {args.node}", file=sys.stderr)
        sys.exit(1)
    result = trace_ffi_chain(G, node_id, getattr(args, "max_depth", 10))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_ffi_types(args):
    """Find FFI type mappings.

    Usage: ffi-types --graph <dir> --from int --to long
    """
    graph_dir = args.graph
    try:
        from _builder.graph_build import _load_full_graph
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _builder.graph_build import _load_full_graph
    G = _load_full_graph(graph_dir)
    results = find_type_mappings(G, getattr(args, "from_type", ""),
                                 getattr(args, "to_type", ""))
    print(json.dumps({
        "from": getattr(args, "from_type", ""),
        "to": getattr(args, "to_type", ""),
        "matches": results,
        "count": len(results),
    }, ensure_ascii=False, indent=2, default=str))
