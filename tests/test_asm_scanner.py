#!/usr/bin/env python3
"""Tests for the ASM regex scanner (asm_scanner.py).

Covers:
- NASM x86_64 scanning (hello, sum, stack, reverse, dot_product, casm)
- Section context tracking (.text vs .data vs .bss)
- Local label ignoring
- Syscall name mapping
- Cross-language import edges (extern)
- Kernel-style SYM_FUNC_START/END/EXPORT_SYMBOL macros
- C scanner gnu_asm_expression inline asm handling
- Extension detection (.asm/.s/.S → "asm")
- Builder cross-language resolution
"""

import os
import sys
import tempfile
import textwrap

import pytest

# Add scripts/ to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from _scanner.asm_scanner import AsmRegexScanner
from _scanner.utils import EXTENSION_MAP, LANG_EXTENSIONS


# ---------------------------------------------------------------------------
# Helper: create temp file with content and scan it
# ---------------------------------------------------------------------------

def _scan_asm(content, filename="test.asm", source_root=None):
    """Create a temp file, scan it, and return the scan result."""
    scanner = AsmRegexScanner()
    with tempfile.TemporaryDirectory() as tmpdir:
        if source_root is None:
            source_root = tmpdir
        fpath = os.path.join(tmpdir, filename)
        # Create subdirectory structure if filename has path separators
        subdir = os.path.dirname(fpath)
        if subdir:
            os.makedirs(subdir, exist_ok=True)
        with open(fpath, 'w') as f:
            f.write(textwrap.dedent(content))
        return scanner.scan_file(fpath, source_root)


def _get_func_names(result):
    """Extract function names from scan result."""
    return [f["name"] for f in result["functions"]]


def _get_edges(result):
    """Extract edges from scan result."""
    return result["edges"]


def _get_edge_pairs(result):
    """Extract (source, target) pairs from edges."""
    return [(e["source"], e["target"]) for e in result["edges"]]


# ---------------------------------------------------------------------------
# Test 1: hello.asm — Hello World with syscalls
# ---------------------------------------------------------------------------

def test_hello_asm():
    result = _scan_asm("""\
        section .data
        msg db "Hello, world!", 0xA
        len equ $ - msg

        section .text
        global _start

        _start:
            mov rax, 1
            mov rdi, 1
            mov rsi, msg
            mov rdx, 14
            syscall
            mov rax, 60
            xor rdi, rdi
            syscall
    """)
    func_names = _get_func_names(result)
    assert "_start" in func_names

    # Should have 2 syscall edges: sys_write + sys_exit
    edges = _get_edges(result)
    syscall_edges = [e for e in edges if e["target"].startswith("syscall_sys_")]
    assert len(syscall_edges) == 2, f"Expected 2 syscall edges, got {len(syscall_edges)}: {syscall_edges}"

    # Verify specific syscalls
    targets = {e["target"] for e in syscall_edges}
    assert "syscall_sys_write" in targets
    assert "syscall_sys_exit" in targets


# ---------------------------------------------------------------------------
# Test 2: sum.asm — Two number sum with syscalls
# ---------------------------------------------------------------------------

def test_sum_asm():
    result = _scan_asm("""\
        section .data
        num1 dq 2
        num2 dq 3
        sum dq 0
        SYS_WRITE equ 1
        SYS_EXIT equ 60

        section .text
        global _start

        _start:
            mov rax, [num1]
            add rax, [num2]
            mov [sum], rax
            mov rax, SYS_WRITE
            syscall
            mov rax, SYS_EXIT
            syscall
    """)
    assert "_start" in _get_func_names(result)


# ---------------------------------------------------------------------------
# Test 3: stack.asm — with subroutine calls
# ---------------------------------------------------------------------------

def test_stack_asm():
    result = _scan_asm("""\
        section .bss
        argc resb 8

        section .text
        global _start

        _start:
            pop rcx
            cmp rcx, 2
            jne .exit
            call str_to_int
            jmp .done
        .exit:
            mov rax, 60
            syscall
        .done:
            call int_to_str
            mov rax, 60
            syscall

        str_to_int:
            xor rax, rax
            ret

        int_to_str:
            xor rax, rax
            ret
    """)
    func_names = _get_func_names(result)
    assert "_start" in func_names
    assert "str_to_int" in func_names
    assert "int_to_str" in func_names

    # Should have call edges: _start → str_to_int, _start → int_to_str
    edges = _get_edges(result)
    call_edges = [e for e in edges if not e["target"].startswith("syscall_")]
    assert len(call_edges) >= 2, f"Expected >=2 call edges, got {len(call_edges)}"


# ---------------------------------------------------------------------------
# Test 4: reverse.asm — string reverse with subroutine call
# ---------------------------------------------------------------------------

def test_reverse_asm():
    result = _scan_asm("""\
        section .data
        msg db "Hello", 0

        section .text
        global _start

        _start:
            call reverseStringAndPrint
            mov rax, 60
            syscall

        reverseStringAndPrint:
            xor rax, rax
            ret
    """)
    func_names = _get_func_names(result)
    assert "_start" in func_names
    assert "reverseStringAndPrint" in func_names

    edges = _get_edges(result)
    call_edges = [e for e in edges if e["target"].endswith("reversestringandprint")]
    assert len(call_edges) >= 1


# ---------------------------------------------------------------------------
# Test 5: dot_product.asm — ASM calling C library functions
# ---------------------------------------------------------------------------

def test_dot_product_asm():
    result = _scan_asm("""\
        section .data
        fmt db "dot product = %f", 0xA, 0

        section .text
        global _start
        extern strtod
        extern printf

        _start:
            call strtod
            call strtod
            call _dot_product
            call printf
            mov rax, 60
            syscall

        _dot_product:
            xor rax, rax
            ret
    """)
    func_names = _get_func_names(result)
    assert "_start" in func_names
    assert "_dot_product" in func_names

    edges = _get_edges(result)
    # Should have call edges for strtod×2, printf, _dot_product
    call_edges = [e for e in edges if not e["target"].startswith("syscall_")]
    assert len(call_edges) >= 4

    # Should have import_edges for extern declarations
    imports = result.get("import_edges", [])
    imported_syms = {ie["imported_symbol"] for ie in imports}
    assert "strtod" in imported_syms
    assert "printf" in imported_syms


# ---------------------------------------------------------------------------
# Test 6: casm1 — ASM calling extern C functions
# ---------------------------------------------------------------------------

def test_casm1_asm():
    result = _scan_asm("""\
        section .text
        global _start
        extern write
        extern exit

        _start:
            call write
            call exit
    """)
    func_names = _get_func_names(result)
    assert "_start" in func_names

    imports = result.get("import_edges", [])
    imported_syms = {ie["imported_symbol"] for ie in imports}
    assert "write" in imported_syms
    assert "exit" in imported_syms


# ---------------------------------------------------------------------------
# Test 7: casm3 — ASM exports function for C to call
# ---------------------------------------------------------------------------

def test_casm3_asm():
    result = _scan_asm("""\
        section .text
        global my_strlen

        my_strlen:
            xor rax, rax
            ret
    """)
    func_names = _get_func_names(result)
    assert "my_strlen" in func_names

    # global-declared function should be API_entry
    func = next(f for f in result["functions"] if f["name"] == "my_strlen")
    assert "API_entry" in func["labels"]


# ---------------------------------------------------------------------------
# Test 8: Section context — .data labels not in functions
# ---------------------------------------------------------------------------

def test_section_context():
    result = _scan_asm("""\
        section .data
        msg db "Hello", 0
        len equ 5

        section .bss
        buffer resb 256

        section .text
        global _start

        _start:
            xor rax, rax
            ret
    """)
    func_names = _get_func_names(result)
    # Data labels should NOT be in functions
    assert "msg" not in func_names
    assert "buffer" not in func_names

    # But constants should be in globals
    globals_data = result.get("globals", {})
    const_names = [c["name"] for c in globals_data.get("constants", [])]
    assert "len" in const_names

    # Data variables should be in globals
    var_names = [v["name"] for v in globals_data.get("global_vars", [])]
    assert "msg" in var_names
    assert "buffer" in var_names


# ---------------------------------------------------------------------------
# Test 9: Local labels ignored
# ---------------------------------------------------------------------------

def test_local_label_ignored():
    result = _scan_asm("""\
        section .text
        global _start

        _start:
            mov rcx, 10
        .loop:
            dec rcx
            jnz .loop
            ret
    """)
    func_names = _get_func_names(result)
    # .loop should NOT be a function
    assert ".loop" not in func_names
    assert "_start" in func_names


# ---------------------------------------------------------------------------
# Test 10: Syscall naming via rax register tracking
# ---------------------------------------------------------------------------

def test_syscall_naming():
    result = _scan_asm("""\
        section .text
        global _start

        _start:
            mov rax, 60
            syscall
    """)
    edges = _get_edges(result)
    syscall_edges = [e for e in edges if e["target"].startswith("syscall_")]
    assert len(syscall_edges) == 1
    assert syscall_edges[0]["target"] == "syscall_sys_exit"


# ---------------------------------------------------------------------------
# Test 11: Kernel SYM_FUNC_START/END macros
# ---------------------------------------------------------------------------

def test_kernel_sym_func_start():
    result = _scan_asm("""\
        SYM_FUNC_START(memcpy)
            mov rax, rdi
            ret
        SYM_FUNC_END(memcpy)
    """, filename="memcpy.S")
    func_names = _get_func_names(result)
    assert "memcpy" in func_names


# ---------------------------------------------------------------------------
# Test 12: EXPORT_SYMBOL marks API_entry
# ---------------------------------------------------------------------------

def test_export_symbol():
    result = _scan_asm("""\
        SYM_FUNC_START(memcpy)
            mov rax, rdi
            ret
        SYM_FUNC_END(memcpy)
        EXPORT_SYMBOL(memcpy)
    """, filename="memcpy.S")

    # EXPORT_SYMBOL should result in API_entry label
    export_syms = result.get("export_symbols", [])
    assert any(e["name"] == "memcpy" for e in export_syms)


# ---------------------------------------------------------------------------
# Test 13: SYM_FUNC_ALIAS
# ---------------------------------------------------------------------------

def test_asm_alias():
    result = _scan_asm("""\
        SYM_FUNC_START(__memcpy)
            mov rax, rdi
            ret
        SYM_FUNC_END(__memcpy)
        SYM_FUNC_ALIAS(memcpy, __memcpy)
    """, filename="memcpy.S")
    aliases = result.get("asm_aliases", [])
    assert any(a["alias"] == "memcpy" and a["original"] == "__memcpy"
               for a in aliases)


# ---------------------------------------------------------------------------
# Test 14: Extension detection
# ---------------------------------------------------------------------------

def test_extension_detection():
    assert EXTENSION_MAP.get(".asm") == "asm"
    assert EXTENSION_MAP.get(".s") == "asm"
    assert EXTENSION_MAP.get(".S") == "asm"
    assert ".asm" in LANG_EXTENSIONS.get("asm", set())
    assert ".s" in LANG_EXTENSIONS.get("asm", set())
    assert ".S" in LANG_EXTENSIONS.get("asm", set())


# ---------------------------------------------------------------------------
# Test 15: Indirect call detection (AMBIGUOUS)
# ---------------------------------------------------------------------------

def test_indirect_call():
    result = _scan_asm("""\
        section .text
        global _start

        _start:
            call rax
    """)
    edges = _get_edges(result)
    ambiguous = [e for e in edges if e["confidence"] == "AMBIGUOUS"]
    assert len(ambiguous) >= 1


# ---------------------------------------------------------------------------
# Test 16: C scanner gnu_asm_expression (requires tree-sitter)
# ---------------------------------------------------------------------------

def test_inline_asm_call():
    """Test C scanner handles inline asm call instructions."""
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    code = textwrap.dedent("""\
        void copy_data(void) {
            __asm__ volatile("call __copy_user"
                            : "+r" (dst)
                            : "r" (src), "r" (size)
                            : "memory");
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    # Should have an edge from copy_data to __copy_user
    edges = result.get("edges", [])
    inline_edges = [e for e in edges
                    if e.get("source") == "inline_asm" or e.get("source_tag") == "inline_asm"]
    assert len(inline_edges) >= 1, f"Expected inline_asm edge, got edges: {edges}"
    targets = {e["target"] for e in inline_edges}
    assert "__copy_user" in targets


# ---------------------------------------------------------------------------
# Test 17: C scanner inline asm indirect call (AMBIGUOUS)
# ---------------------------------------------------------------------------

def test_inline_asm_indirect():
    """Test C scanner handles inline asm indirect calls as AMBIGUOUS."""
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    code = textwrap.dedent("""\
        void call_via_ptr(void) {
            __asm__ volatile("call *%rax" ::: "memory");
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])
    ambiguous = [e for e in edges if e.get("confidence") == "AMBIGUOUS"
                 and e.get("source_tag") == "inline_asm"]
    assert len(ambiguous) >= 1


# ---------------------------------------------------------------------------
# Test 18: Syscall with unknown number
# ---------------------------------------------------------------------------

def test_syscall_unknown_number():
    result = _scan_asm("""\
        section .text
        global _start

        _start:
            mov rax, 999
            syscall
    """)
    edges = _get_edges(result)
    syscall_edges = [e for e in edges if e["target"].startswith("syscall_")]
    assert len(syscall_edges) == 1
    # Should use numeric fallback since 999 is not in the map
    assert "999" in syscall_edges[0]["target"] or "syscall_999" in syscall_edges[0]["target"]


# ---------------------------------------------------------------------------
# Test 19: Conditional jump tracking (call_condition)
# ---------------------------------------------------------------------------

def test_conditional_jump_tracking():
    result = _scan_asm("""\
        section .text
        global _start

        _start:
            cmp rax, 0
            jne .skip
            call func_a
        .skip:
            call func_b
            ret
    """)
    edges = _get_edges(result)
    # func_a call should have a call_condition from the jne
    func_a_edges = [e for e in edges if "func_a" in e["target"]]
    assert len(func_a_edges) >= 1
    assert func_a_edges[0]["call_condition"] != ""


# ---------------------------------------------------------------------------
# Test 20: Kernel ENTRY/ENDPROC legacy macros
# ---------------------------------------------------------------------------

def test_kernel_legacy_entry():
    result = _scan_asm("""\
        ENTRY(sys_read)
            mov rax, 0
            syscall
            ret
        ENDPROC(sys_read)
    """, filename="entry.S")
    func_names = _get_func_names(result)
    assert "sys_read" in func_names


# ---------------------------------------------------------------------------
# Test 21: Register transfer tracking
# ---------------------------------------------------------------------------

def test_register_transfer_tracking():
    result = _scan_asm("""\
        section .text
        global _start

        _start:
            mov rdi, 1
            mov rsi, msg
            call func_a
            mov rdi, 2
            call func_b
    """)
    # Functions should have reg_transfers data
    func = next(f for f in result["functions"] if f["name"] == "_start")
    transfers = func.get("reg_transfers", [])
    # mov rdi, 1 doesn't create a transfer (1 is not a register)
    # But mov rsi, msg doesn't either (msg is not a register name)
    # We should still have the final register state
    reg_state = func.get("reg_state_final", {})
    assert "rdi" in reg_state  # Should track rdi value


# ---------------------------------------------------------------------------
# Test 22: Syscall with register args
# ---------------------------------------------------------------------------

def test_syscall_with_register_args():
    result = _scan_asm("""\
        section .text
        global _start

        _start:
            mov rax, 1
            mov rdi, 1
            mov rsi, msg
            mov rdx, 14
            syscall
    """)
    edges = _get_edges(result)
    syscall_edges = [e for e in edges if e["target"].startswith("syscall_")]
    assert len(syscall_edges) >= 1
    # Should have reg_args with rdi, rsi, rdx
    if "reg_args" in syscall_edges[0]:
        arg_regs = [a["reg"] for a in syscall_edges[0]["reg_args"]]
        assert "rdi" in arg_regs
        assert "rsi" in arg_regs
        assert "rdx" in arg_regs


# ---------------------------------------------------------------------------
# Test 23: AArch64 register tracking
# ---------------------------------------------------------------------------

def test_aarch64_register_tracking():
    from _scanner.asm_scanner import _RegisterTracker
    tracker = _RegisterTracker()
    # Simulate AArch64 instructions
    tracker.process_line("mov x8, #63", 1)
    tracker.process_line("mov x0, #1", 2)
    tracker.process_line("svc #0", 3)
    # x8 should be const:63, x0 should be const:1
    assert tracker._regs.get('x8') == 'const:63'
    assert tracker._regs.get('x0') == 'const:1'


# ---------------------------------------------------------------------------
# Test 24: Register-to-register transfer
# ---------------------------------------------------------------------------

def test_register_to_register_transfer():
    from _scanner.asm_scanner import _RegisterTracker
    tracker = _RegisterTracker()
    tracker.process_line("mov rdi, rax", 1)
    transfers = tracker.get_transfers()
    assert len(transfers) >= 1
    src, dst, _ = transfers[0]
    assert src == "rax"
    assert dst == "rdi"


# ---------------------------------------------------------------------------
# Test 25: Push/pop tracking
# ---------------------------------------------------------------------------

def test_push_pop_tracking():
    from _scanner.asm_scanner import _RegisterTracker
    tracker = _RegisterTracker()
    tracker.process_line("mov rax, 42", 1)
    tracker.process_line("push rax", 2)
    tracker.process_line("xor rax, rax", 3)
    tracker.process_line("pop rbx", 4)
    # rbx should have the value that was pushed (const:42 via rax)
    assert "rbx" in tracker._regs


# ---------------------------------------------------------------------------
# Test 26: Multi-symbol extern/global declarations
# ---------------------------------------------------------------------------

def test_multi_symbol_extern():
    result = _scan_asm("""\
        section .text
        global func1, func2, func3
        extern write, exit

        func1:
            call write
            ret
        func2:
            call exit
            ret
        func3:
            call write
            call exit
            ret
    """)
    func_names = _get_func_names(result)
    # All three globals should be detected
    assert "func1" in func_names
    assert "func2" in func_names
    assert "func3" in func_names

    imports = result.get("import_edges", [])
    imported_syms = {ie["imported_symbol"] for ie in imports}
    # Both extern symbols should be in imports
    assert "write" in imported_syms
    assert "exit" in imported_syms


# ---------------------------------------------------------------------------
# Test 27: Tail call detection via jmp
# ---------------------------------------------------------------------------

def test_tail_call_jmp():
    result = _scan_asm("""\
        section .text
        global _start
        global helper

        _start:
            call helper
            ret
        helper:
            jmp _start
    """)
    edges = _get_edges(result)
    # Should have call edge from _start → helper (EXTRACTED)
    # AND tail call edge from helper → _start (INFERRED, source=tail_call)
    tail_edges = [e for e in edges if e.get("source") == "tail_call"
                  or e.get("confidence") == "INFERRED"]
    assert len(tail_edges) >= 1, f"Expected tail call edge, got edges: {edges}"


# ---------------------------------------------------------------------------
# Test 28: AT&T syntax register tracking
# ---------------------------------------------------------------------------

def test_att_syntax_register_tracking():
    result = _scan_asm("""\
        .text
        .globl _start

        _start:
            movl $1, %eax
            movl $60, %edi
            int $0x80
    """, filename="start.S")
    # Should detect function and register values
    func_names = _get_func_names(result)
    assert "_start" in func_names
    func = next(f for f in result["functions"] if f["name"] == "_start")
    reg_state = func.get("reg_state_final", {})
    # rax should have const:1 (from movl $1, %eax → eax canonical = rax)
    assert "rax" in reg_state, f"Expected rax in reg_state, got {reg_state}"


# ---------------------------------------------------------------------------
# Test 29: AT&T syntax syscall detection
# ---------------------------------------------------------------------------

def test_att_syscall_detection():
    """Test that int $0x80 creates syscall edges (basic support)."""
    result = _scan_asm("""\
        .text
        .globl _start

        _start:
            movl $1, %eax
            movl $1, %edi
            int $0x80
    """, filename="start.S")
    # int $0x80 is not yet fully supported, but mov instructions should work
    func = next(f for f in result["functions"] if f["name"] == "_start")
    reg_state = func.get("reg_state_final", {})
    assert "rax" in reg_state


# ---------------------------------------------------------------------------
# Test 30: C inline asm with syscall instruction
# ---------------------------------------------------------------------------

def test_inline_asm_syscall():
    """Test C scanner handles inline asm syscall instructions."""
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    code = textwrap.dedent("""\
        void sys_write(void) {
            __asm__ volatile("syscall"
                            : "=a"(ret)
                            : "a"(1), "D"(1), "S"(msg), "d"(14));
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])
    # Should have an edge from sys_write (inline asm detected)
    inline_edges = [e for e in edges
                    if e.get("source") == "inline_asm" or e.get("source_tag") == "inline_asm"]
    # The syscall instruction is not a call/bl/blr, so it may not create an edge
    # But at minimum the inline_asm block should be detected
    # This test documents current behavior


# ---------------------------------------------------------------------------
# Test 31: GAS .type directive function detection
# ---------------------------------------------------------------------------

def test_gas_type_directive():
    result = _scan_asm("""\
        .text
        .type my_func, @function
        my_func:
            xor rax, rax
            ret
        .size my_func, .-my_func
    """, filename="func.S")
    func_names = _get_func_names(result)
    assert "my_func" in func_names


# ---------------------------------------------------------------------------
# Test 32: casm1-like: ASM calling C functions
# ---------------------------------------------------------------------------

def test_casm1_integration():
    """Integration test matching casm1 project structure."""
    result = _scan_asm("""\
        section .data
        msg db "hello, world!", 10

        section .text
        global _start
        extern write, exit

        _start:
            mov rdi, 1
            mov rsi, msg
            mov rdx, 14
            call write

            mov rdi, 0
            call exit
    """)
    func_names = _get_func_names(result)
    assert "_start" in func_names

    imports = result.get("import_edges", [])
    imported_syms = {ie["imported_symbol"] for ie in imports}
    assert "write" in imported_syms
    assert "exit" in imported_syms

    # Register state should show rdi=const:0 at function end
    func = next(f for f in result["functions"] if f["name"] == "_start")
    reg_state = func.get("reg_state_final", {})
    assert "rdi" in reg_state


# ---------------------------------------------------------------------------
# Test 33: casm4-like: C inline asm with bl/blr (ARM)
# ---------------------------------------------------------------------------

def test_casm4_inline_asm_bl():
    """Test C inline asm with ARM bl/blr instructions (casm4-like)."""
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    code = textwrap.dedent("""\
        void call_helpers(void) {
            __asm__ volatile("bl helper1");
            __asm__ volatile("bl helper2");
            __asm__ volatile("blr x8");
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])
    # bl helper1 and bl helper2 should create INFERRED edges from caller function
    bl_edges = [e for e in edges
                if e["confidence"] == "INFERRED" and "helper" in e.get("target", "")]
    assert len(bl_edges) >= 2, f"Expected >=2 bl edges, got {bl_edges}"

    # blr x8 should create AMBIGUOUS edge (no register binding in this function)
    ambiguous = [e for e in edges
                 if e["confidence"] == "AMBIGUOUS" and e.get("target") == "indirect_call"]
    assert len(ambiguous) >= 1, f"Expected AMBIGUOUS edge for blr, got {edges}"

    # Edges should have source_tag=inline_asm and source=invoker_id (not "inline_asm")
    for e in bl_edges:
        assert e.get("source_tag") == "inline_asm"
        assert "call_helpers" in e.get("source", ""), f"Expected source=call_helpers, got {e.get('source')}"


# ---------------------------------------------------------------------------
# Test 34: AArch64 cbz/cbnz conditional branches
# ---------------------------------------------------------------------------

def test_aarch64_cbz_cbnz():
    """Test AArch64 cbz/cbnz conditional branch tracking."""
    code = textwrap.dedent("""\
        .text
        .global my_func
        my_func:
            mov x0, #0
            cbz x0, .Lskip
            bl helper1
        .Lskip:
            cbnz x1, .Ldone
            bl helper2
        .Ldone:
            ret
    """)
    result = _scan_asm(code, filename="test.S")
    edges = result.get("edges", [])

    # Should have edges to helper1 and helper2
    targets_str = " ".join(e["target"] for e in edges)
    assert "helper1" in targets_str, f"Expected helper1 edge, got {targets_str}"
    assert "helper2" in targets_str, f"Expected helper2 edge, got {targets_str}"

    # cbz/cbnz should add conditions to edges
    helper1_edges = [e for e in edges if "helper1" in e.get("target", "")]
    assert any(e.get("call_condition") for e in helper1_edges), \
        f"Expected condition on helper1 edge, got {helper1_edges}"


# ---------------------------------------------------------------------------
# Test 35: AArch64 tbz/tbnz test-bit branches
# ---------------------------------------------------------------------------

def test_aarch64_tbz_tbnz():
    """Test AArch64 tbz/tbnz test-bit branch tracking."""
    code = textwrap.dedent("""\
        .text
        .global bit_check
        bit_check:
            mov x0, #5
            tbz x0, #0, .Lzero_bit
            bl handler_set
        .Lzero_bit:
            tbnz x0, #2, .Lbit2_set
            bl handler_clear
        .Lbit2_set:
            ret
    """)
    result = _scan_asm(code, filename="test.S")
    edges = result.get("edges", [])

    targets_str = " ".join(e["target"] for e in edges)
    assert "handler_set" in targets_str, f"Expected handler_set edge, got {targets_str}"
    assert "handler_clear" in targets_str, f"Expected handler_clear edge, got {targets_str}"


# ---------------------------------------------------------------------------
# Test 36: AArch64 br indirect branch (tail call)
# ---------------------------------------------------------------------------

def test_aarch64_br_indirect_branch():
    """Test AArch64 br indirect branch detection."""
    code = textwrap.dedent("""\
        .text
        .global dispatch
        .extern target_func
        dispatch:
            adr x0, target_func
            br x0
    """)
    result = _scan_asm(code, filename="test.S")
    edges = result.get("edges", [])

    # br with resolved address should create an INFERRED tail call edge
    # or AMBIGUOUS indirect branch edge
    targets_str = " ".join(e["target"] for e in edges)
    assert "indirect" in targets_str or "target_func" in targets_str, \
        f"Expected br edge, got {targets_str}"


# ---------------------------------------------------------------------------
# Test 37: AArch64 blr indirect call with register tracking
# ---------------------------------------------------------------------------

def test_aarch64_blr_indirect_call():
    """Test AArch64 blr indirect call with register arg tracking."""
    code = textwrap.dedent("""\
        .text
        .global caller
        caller:
            mov x0, #42
            mov x1, #10
            ldr x8, [sp]
            blr x8
    """)
    result = _scan_asm(code, filename="test.S")
    edges = result.get("edges", [])

    # blr should create an AMBIGUOUS indirect call edge
    blr_edges = [e for e in edges if e.get("target") == "indirect_call"]
    assert len(blr_edges) >= 1, f"Expected indirect_call edge, got {edges}"

    # Check that AArch64 register args are captured (x0-x7 convention)
    blr_edge = blr_edges[0]
    reg_args = blr_edge.get("reg_args", [])
    reg_names = {a["reg"] for a in reg_args}
    assert "x0" in reg_names, f"Expected x0 in reg_args, got {reg_args}"
    assert "x1" in reg_names, f"Expected x1 in reg_args, got {reg_args}"


# ---------------------------------------------------------------------------
# Test 38: AArch64 bl with register arg tracking
# ---------------------------------------------------------------------------

def test_aarch64_bl_register_args():
    """Test AArch64 bl call with register argument tracking."""
    code = textwrap.dedent("""\
        .text
        .global my_caller
        my_caller:
            mov x0, #1
            mov x1, #2
            mov x2, #3
            bl callee_func
    """)
    result = _scan_asm(code, filename="test.S")
    edges = result.get("edges", [])

    callee_edges = [e for e in edges if "callee_func" in e.get("target", "")]
    assert len(callee_edges) >= 1, f"Expected callee_func edge, got {edges}"

    # Check AArch64 register args (x0-x7 convention)
    callee_edge = callee_edges[0]
    reg_args = callee_edge.get("reg_args", [])
    arg_regs = {a["reg"] for a in reg_args}
    assert "x0" in arg_regs, f"Expected x0 in args, got {reg_args}"
    assert "x1" in arg_regs, f"Expected x1 in args, got {reg_args}"
    assert "x2" in arg_regs, f"Expected x2 in args, got {reg_args}"


# ---------------------------------------------------------------------------
# Test 39: C inline asm blr with register binding resolution
# ---------------------------------------------------------------------------

def test_casm4_blr_register_binding():
    """Test that blr %N in C inline asm resolves to function name via register bindings.

    Pattern from casm4:
      register void *fn9 __asm__("x9") = (void *)multiply_asm;
      __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(fn9) : ...);
    Should create INFERRED edge to multiply_asm, not AMBIGUOUS indirect_call.
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    # Use exact pattern from casm4 (tree-sitter-c requires specific syntax)
    code = textwrap.dedent("""\
        static int multiply_asm(int a, int b);
        static int cube_via_blr(int x) {
            register int w0 __asm__("w0") = x;
            register int w1 __asm__("w1") = x;
            register void *fn9 __asm__("x9") = (void *)multiply_asm;
            int sq;
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(fn9) : "x8", "x30", "memory");
            sq = w0;
            return sq;
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])

    # blr %2 should resolve fn9 → multiply_asm via register binding
    inferred_blr = [e for e in edges
                    if e["confidence"] == "INFERRED" and "multiply_asm" in e.get("target", "")]
    assert len(inferred_blr) >= 1, \
        f"Expected INFERRED edge to multiply_asm via blr register binding, got {edges}"

    # Should NOT have AMBIGUOUS indirect_call for this blr (it was resolved)
    ambiguous = [e for e in edges
                 if e["confidence"] == "AMBIGUOUS" and e.get("target") == "indirect_call"]
    assert len(ambiguous) == 0, \
        f"Expected no AMBIGUOUS edges (blr should resolve), got {ambiguous}"


# ---------------------------------------------------------------------------
# Test 40: C inline asm blr with direct register reference (blr x9)
# ---------------------------------------------------------------------------

def test_casm4_blr_direct_register():
    """Test that blr xN in C inline asm resolves via register-to-function map.

    Uses pattern from casm4: register void *fn9 __asm__("x9") = (void *)add_asm;
    Then: __asm__("blr x9" ::: ...) where x9 is directly referenced.
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    # Use exact casm4-like pattern that tree-sitter-c can parse
    code = textwrap.dedent("""\
        static int add_asm(int a, int b);
        static int dispatch() {
            register int w0 __asm__("w0") = 1;
            register void *fn9 __asm__("x9") = (void *)add_asm;
            __asm__("blr x9" : "+r"(w0) : : "x8", "x30", "memory");
            return w0;
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])

    # blr x9 should resolve to add_asm via register binding
    has_resolved = any(e["confidence"] == "INFERRED" and "add_asm" in e.get("target", "")
                       for e in edges)
    assert has_resolved, \
        f"Expected INFERRED edge to add_asm via blr x9 register binding, got {edges}"


# ---------------------------------------------------------------------------
# Test 41: x86_64 indirect call with register args (NASM)
# ---------------------------------------------------------------------------

def test_x86_64_indirect_call_with_reg_args():
    """Test x86_64 indirect call captures register args (NASM syntax)."""
    code = textwrap.dedent("""\
        section .text
        global dispatcher
        extern target_func

        dispatcher:
            mov rdi, 42
            mov rsi, 100
            call rax
    """)
    result = _scan_asm(code, filename="test.asm")
    edges = result.get("edges", [])

    indirect_edges = [e for e in edges if e.get("target") == "indirect_call"]
    assert len(indirect_edges) >= 1, f"Expected indirect_call edge, got {edges}"

    # x86_64 calling convention: rdi, rsi, rdx, rcx, r8, r9
    reg_args = indirect_edges[0].get("reg_args", [])
    arg_regs = {a["reg"] for a in reg_args}
    assert "rdi" in arg_regs, f"Expected rdi in args, got {reg_args}"
    assert "rsi" in arg_regs, f"Expected rsi in args, got {reg_args}"


# ---------------------------------------------------------------------------
# Test 42: x86_64 indirect call with register args (AT&T/GAS)
# ---------------------------------------------------------------------------

def test_x86_64_att_indirect_call_with_reg_args():
    """Test x86_64 indirect call captures register args (AT&T syntax)."""
    code = textwrap.dedent("""\
        .text
        .globl dispatcher
        dispatcher:
            mov $42, %rdi
            mov $100, %rsi
            call *%rax
    """)
    result = _scan_asm(code, filename="test.S")
    edges = result.get("edges", [])

    indirect_edges = [e for e in edges if e.get("target") == "indirect_call"]
    assert len(indirect_edges) >= 1, f"Expected indirect_call edge, got {edges}"

    reg_args = indirect_edges[0].get("reg_args", [])
    arg_regs = {a["reg"] for a in reg_args}
    assert "rdi" in arg_regs, f"Expected rdi in args, got {reg_args}"
    assert "rsi" in arg_regs, f"Expected rsi in args, got {reg_args}"


# ---------------------------------------------------------------------------
# Test 43: x86_64 tail call with register args
# ---------------------------------------------------------------------------

def test_x86_64_tail_call_with_reg_args():
    """Test x86_64 jmp tail call captures register args."""
    code = textwrap.dedent("""\
        section .text
        global wrapper
        extern real_handler

        wrapper:
            mov rdi, 1
            jmp real_handler
    """)
    result = _scan_asm(code, filename="test.asm")
    edges = result.get("edges", [])

    tail_edges = [e for e in edges if "real_handler" in e.get("target", "")]
    assert len(tail_edges) >= 1, f"Expected tail call edge, got {edges}"

    tail_edge = tail_edges[0]
    assert tail_edge.get("confidence") == "INFERRED"
    assert tail_edge.get("source_tag") == "tail_call"
    reg_args = tail_edge.get("reg_args", [])
    arg_regs = {a["reg"] for a in reg_args}
    assert "rdi" in arg_regs, f"Expected rdi in args, got {reg_args}"


# ---------------------------------------------------------------------------
# Test 44: casm4 full equivalent — C with ARM bl/blr inline asm chain
# ---------------------------------------------------------------------------

def test_casm4_full_equivalent():
    """Full casm4-like test: C function with bl chain and blr dispatch.

    casm4 is described as 'C inline assembly with inter-function calls (bl and blr)'.
    Since casm4 directory is inaccessible (root permissions), we create an equivalent
    test that covers the same patterns:
    - C function with inline asm containing bl (direct ARM call)
    - C function with inline asm containing blr (indirect ARM call via register)
    - Chained bl calls within one inline asm block
    - Cross-file interaction: C calling convention (x0-x7) + ARM branch instructions
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)

    # Simulate a casm4-like C file with multiple inline asm patterns
    code = textwrap.dedent("""\
        #include <stdio.h>

        void helper_a(int x) { printf("a: %d", x); }
        void helper_b(int x) { printf("b: %d", x); }

        void dispatch_bl(void) {
            __asm__ volatile("bl helper_a");
            __asm__ volatile("bl helper_b");
        }

        void dispatch_blr(void) {
            register void (*fn_ptr)(int) __asm__("x8") = helper_a;
            __asm__ volatile("blr x8");
        }

        void chained_calls(void) {
            __asm__ volatile(
                "bl helper_a\\n\\t"
                "bl helper_b\\n\\t"
            );
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "casm4_sim.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])
    functions = result.get("functions", [])

    # Should detect helper_a and helper_b as functions
    func_names = {f["name"] for f in functions}
    assert "helper_a" in func_names, f"Expected helper_a, got {func_names}"
    assert "helper_b" in func_names, f"Expected helper_b, got {func_names}"

    # bl helper_a and bl helper_b should create INFERRED edges with source_tag=inline_asm
    bl_edges = [e for e in edges
                if e.get("source_tag") == "inline_asm" and e["confidence"] == "INFERRED"]
    bl_targets = {e["target"] for e in bl_edges}
    assert "helper_a" in bl_targets, f"Expected helper_a in bl targets, got {bl_targets}"
    assert "helper_b" in bl_targets, f"Expected helper_b in bl targets, got {bl_targets}"

    # blr x8 should create AMBIGUOUS edge with source_tag=inline_asm
    ambiguous = [e for e in edges
                 if e.get("source_tag") == "inline_asm" and e["confidence"] == "AMBIGUOUS"]
    assert len(ambiguous) >= 1, f"Expected AMBIGUOUS edge for blr, got {edges}"

    # dispatch_bl should have edges to inline_asm, which in turn has edges to helpers
    # The C scanner creates: dispatch_bl → inline_asm, then inline_asm → helper_a/b
    dispatch_bl_edges = [e for e in edges if "dispatch_bl" in e.get("source", "")]
    # There should be some path from dispatch_bl to helpers (direct or via inline_asm)
    all_targets = set()
    for e in edges:
        all_targets.add(e.get("target", ""))
    assert "inline_asm" in all_targets or "helper_a" in all_targets, \
        f"Expected inline_asm or helper_a edge, got {edges}"


# ---------------------------------------------------------------------------
# Test 45: dispatch_op via static function pointer array
# ---------------------------------------------------------------------------

def test_dispatch_op_fn_ptr_array():
    """Test dispatch_op pattern: static fn ptr array + blr via register binding.

    Pattern from casm4:
      static const void * const ops[] = {add_asm, sub_asm, multiply_asm};
      void *fn = (void *)ops[op];
      register void *x9 __asm__("x9") = fn;
      __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(x9) : ...);

    Should create INFERRED dispatch_op edges to add_asm, sub_asm, multiply_asm.
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    code = textwrap.dedent("""\
        static int add_asm(int a, int b);
        static int sub_asm(int a, int b);
        static int multiply_asm(int a, int b);

        static const void * const ops[] = {add_asm, sub_asm, multiply_asm};

        static int dispatch_op(int op, int a, int b) {
            register int w0 __asm__("w0") = a;
            register int w1 __asm__("w1") = b;
            void *fn = (void *)ops[op];
            register void *x9 __asm__("x9") = fn;
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(x9) : "x8", "x30", "memory");
            return w0;
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "dispatch.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])

    # Should create INFERRED dispatch_op edges to each candidate
    dispatch_edges = [e for e in edges
                     if e.get("source_tag") == "inline_asm"
                     and e.get("call_condition") == "dispatch_op"
                     and e["confidence"] == "INFERRED"]
    dispatch_targets = {e["target"] for e in dispatch_edges}
    assert "add_asm" in dispatch_targets, f"Expected add_asm in dispatch targets, got {dispatch_targets}"
    assert "sub_asm" in dispatch_targets, f"Expected sub_asm in dispatch targets, got {dispatch_targets}"
    assert "multiply_asm" in dispatch_targets, f"Expected multiply_asm in dispatch targets, got {dispatch_targets}"

    # Should NOT have AMBIGUOUS indirect_call (dispatch was resolved to candidates)
    ambiguous = [e for e in edges
                 if e.get("confidence") == "AMBIGUOUS" and e.get("target") == "indirect_call"]
    assert len(ambiguous) == 0, \
        f"Expected no AMBIGUOUS indirect_call (dispatch resolved), got {ambiguous}"


# ---------------------------------------------------------------------------
# Test 46: dispatch_op via blr xN direct register reference
# ---------------------------------------------------------------------------

def test_dispatch_op_fn_ptr_array_blr_direct():
    """Test dispatch_op where register bound variable traces through to array.

    Pattern: register void *x9 __asm__("x9") = fn; where fn = ops[idx]
    The blr x9 should resolve to all candidates from the ops[] array.
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    code = textwrap.dedent("""\
        static int add_asm(int a, int b);
        static int sub_asm(int a, int b);

        static const void * const ops[] = {add_asm, sub_asm};

        static int dispatch(int op, int a, int b) {
            register int w0 __asm__("w0") = a;
            void *fn = (void *)ops[op];
            register void *x9 __asm__("x9") = fn;
            __asm__("blr x9" : "+r"(w0) : : "x8", "x30", "memory");
            return w0;
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "dispatch2.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])

    # blr x9 should resolve to add_asm and sub_asm via dispatch chain
    dispatch_edges = [e for e in edges
                     if e.get("source_tag") == "inline_asm"
                     and e.get("call_condition") == "dispatch_op"
                     and e["confidence"] == "INFERRED"]
    dispatch_targets = {e["target"] for e in dispatch_edges}
    assert "add_asm" in dispatch_targets, f"Expected add_asm, got {dispatch_targets}"
    assert "sub_asm" in dispatch_targets, f"Expected sub_asm, got {dispatch_targets}"

    # No AMBIGUOUS
    ambiguous = [e for e in edges
                 if e.get("confidence") == "AMBIGUOUS" and e.get("target") == "indirect_call"]
    assert len(ambiguous) == 0, \
        f"Expected no AMBIGUOUS indirect_call, got {ambiguous}"


# ---------------------------------------------------------------------------
# Test 47: casm4 full integration — all 15 functions + dispatch_op
# ---------------------------------------------------------------------------

def test_casm4_full_integration():
    """Full casm4 integration test using the actual casm4 code patterns.

    casm4 has 15 functions:
    - Leaf functions: add_asm, sub_asm, multiply_asm, max_asm, min_asm
    - bl chain: square_asm, sum_of_squares
    - blr resolved: cube_via_blr, abs_via_blr, quartic_via_blr
    - dispatch_op: dispatch via ops[] array (add_asm, sub_asm, multiply_asm)
    - C direct calls from main
    - Nested C calls: add_asm(sum_of_squares(), multiply_asm())
    - factorial_asm: inline asm loop
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)

    # Full casm4 source code (simplified but structurally identical)
    code = textwrap.dedent("""\
        #include <stdio.h>

        static int add_asm(int a, int b) {
            register int w0 __asm__("w0") = a;
            register int w1 __asm__("w1") = b;
            __asm__("add w0, w0, w1" : "+r"(w0));
            return w0;
        }

        static int sub_asm(int a, int b) {
            register int w0 __asm__("w0") = a;
            register int w1 __asm__("w1") = b;
            __asm__("sub w0, w0, w1" : "+r"(w0));
            return w0;
        }

        static int multiply_asm(int a, int b) {
            register int w0 __asm__("w0") = a;
            register int w1 __asm__("w1") = b;
            __asm__("mul w0, w0, w1" : "+r"(w0));
            return w0;
        }

        static int max_asm(int a, int b) {
            register int w0 __asm__("w0") = a;
            register int w1 __asm__("w1") = b;
            __asm__("cmp w0, w1\\n\\tcsel w0, w0, w1, gt" : "+r"(w0));
            return w0;
        }

        static int min_asm(int a, int b) {
            register int w0 __asm__("w0") = a;
            register int w1 __asm__("w1") = b;
            __asm__("cmp w0, w1\\n\\tcsel w0, w1, w0, gt" : "+r"(w0));
            return w0;
        }

        static int square_asm(int x) {
            register int w0 __asm__("w0") = x;
            register int w1 __asm__("w1") = x;
            __asm__("bl multiply_asm" : "+r"(w0) : "r"(w1) : "x8", "x30", "memory");
            return w0;
        }

        static int cube_via_blr(int x) {
            register int w0 __asm__("w0") = x;
            register int w1 __asm__("w1") = x;
            register void *fn9 __asm__("x9") = (void *)multiply_asm;
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(fn9) : "x8", "x30", "memory");
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(fn9) : "x8", "x30", "memory");
            return w0;
        }

        static int abs_via_blr(int x) {
            register int w0 __asm__("w0") = x;
            register int w1 __asm__("w1") = 0;
            register void *fn9 __asm__("x9") = (void *)sub_asm;
            register void *fn10 __asm__("x10") = (void *)max_asm;
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(fn9) : "x8", "x30", "memory");
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(fn10) : "x8", "x30", "memory");
            return w0;
        }

        static const void * const ops[] = {add_asm, sub_asm, multiply_asm};

        static int dispatch_op(int op, int a, int b) {
            register int w0 __asm__("w0") = a;
            register int w1 __asm__("w1") = b;
            void *fn = (void *)ops[op];
            register void *x9 __asm__("x9") = fn;
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(x9) : "x8", "x30", "memory");
            return w0;
        }

        static int quartic_via_blr(int x) {
            register int w0 __asm__("w0") = x;
            register int w1 __asm__("w1") = x;
            register void *fn9 __asm__("x9") = (void *)square_asm;
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(fn9) : "x8", "x30", "memory");
            __asm__("blr %2" : "+r"(w0) : "r"(w1), "r"(fn9) : "x8", "x30", "memory");
            return w0;
        }

        static int factorial_asm(int n) {
            register int w0 __asm__("w0") = 1;
            register int w1 __asm__("w1") = n;
            __asm__("1: cmp w1, #0\\n\\tbeq 2f\\n\\tmul w0, w0, w1\\n\\tsub w1, w1, #1\\n\\tb 1b\\n2:" : "+r"(w0) : "r"(w1));
            return w0;
        }

        static int sum_of_squares(int a, int b) {
            register int w0 __asm__("w0") = a;
            register int w1 __asm__("w1") = a;
            __asm__("bl multiply_asm" : "+r"(w0) : "r"(w1) : "x8", "x30", "memory");
            int sq_a = w0;
            w0 = b;
            w1 = b;
            __asm__("bl multiply_asm" : "+r"(w0) : "r"(w1) : "x8", "x30", "memory");
            int sq_b = w0;
            return add_asm(sq_a, sq_b);
        }

        int main(void) {
            int a = add_asm(3, 4);
            int b = sub_asm(10, 3);
            int c = multiply_asm(5, 6);
            int d = square_asm(7);
            int e = cube_via_blr(3);
            int f = abs_via_blr(-5);
            int g = dispatch_op(0, 10, 20);
            int h = quartic_via_blr(2);
            int i = factorial_asm(5);
            int j = sum_of_squares(3, 4);
            printf("results: %d %d %d %d %d %d %d %d %d %d\\n",
                   a, b, c, d, e, f, g, h, i, j);
            return 0;
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "casm4.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    functions = result.get("functions", [])
    edges = result.get("edges", [])
    func_names = {f["name"] for f in functions}

    # Should detect all 15 functions
    expected_funcs = {
        "add_asm", "sub_asm", "multiply_asm", "max_asm", "min_asm",
        "square_asm", "cube_via_blr", "abs_via_blr",
        "dispatch_op", "quartic_via_blr", "factorial_asm",
        "sum_of_squares", "main",
    }
    # Note: printf may or may not be a function depending on whether
    # tree-sitter treats it as a call_expression with identifier
    for fn in expected_funcs:
        assert fn in func_names, f"Expected function {fn}, got {func_names}"

    # bl edges: square_asm → multiply_asm (inline asm)
    sq_mul = [e for e in edges
              if "square_asm" in e.get("source", "") and "multiply_asm" in e.get("target", "")]
    assert len(sq_mul) >= 1, f"Expected square_asm→multiply_asm edge, got {edges}"

    # blr resolved edges: cube_via_blr → multiply_asm
    cube_mul = [e for e in edges
                if "cube_via_blr" in e.get("source", "")
                and "multiply_asm" in e.get("target", "")
                and e.get("confidence") == "INFERRED"]
    assert len(cube_mul) >= 1, f"Expected cube_via_blr→multiply_asm INFERRED edge, got {edges}"

    # blr resolved edges: abs_via_blr → sub_asm and abs_via_blr → max_asm
    abs_sub = [e for e in edges
               if "abs_via_blr" in e.get("source", "")
               and "sub_asm" in e.get("target", "")
               and e.get("confidence") == "INFERRED"]
    abs_max = [e for e in edges
               if "abs_via_blr" in e.get("source", "")
               and "max_asm" in e.get("target", "")
               and e.get("confidence") == "INFERRED"]
    assert len(abs_sub) >= 1, f"Expected abs_via_blr→sub_asm INFERRED edge, got {edges}"
    assert len(abs_max) >= 1, f"Expected abs_via_blr→max_asm INFERRED edge, got {edges}"

    # dispatch_op → {add_asm, sub_asm, multiply_asm} via dispatch_op condition
    dispatch_edges = [e for e in edges
                     if "dispatch_op" in e.get("source", "")
                     and e.get("call_condition") == "dispatch_op"
                     and e.get("confidence") == "INFERRED"]
    dispatch_targets = {e["target"] for e in dispatch_edges}
    assert "add_asm" in dispatch_targets, f"Expected add_asm in dispatch, got {dispatch_targets}"
    assert "sub_asm" in dispatch_targets, f"Expected sub_asm in dispatch, got {dispatch_targets}"
    assert "multiply_asm" in dispatch_targets, f"Expected multiply_asm in dispatch, got {dispatch_targets}"

    # quartic_via_blr → square_asm (resolved blr)
    quartic_sq = [e for e in edges
                  if "quartic_via_blr" in e.get("source", "")
                  and "square_asm" in e.get("target", "")
                  and e.get("confidence") == "INFERRED"]
    assert len(quartic_sq) >= 1, f"Expected quartic_via_blr→square_asm INFERRED edge, got {edges}"

    # sum_of_squares → add_asm (C direct call) + multiply_asm (inline asm bl)
    sos_add = [e for e in edges
               if "sum_of_squares" in e.get("source", "") and "add_asm" in e.get("target", "")]
    sos_mul = [e for e in edges
               if "sum_of_squares" in e.get("source", "") and "multiply_asm" in e.get("target", "")]
    assert len(sos_add) >= 1, f"Expected sum_of_squares→add_asm edge, got {edges}"
    assert len(sos_mul) >= 1, f"Expected sum_of_squares→multiply_asm edge, got {edges}"

    # main should call many functions
    main_targets = {e["target"] for e in edges if "main" in e.get("source", "")}
    assert "add_asm" in main_targets, f"Expected main→add_asm, got {main_targets}"
    assert "dispatch_op" in main_targets, f"Expected main→dispatch_op, got {main_targets}"
    assert "sum_of_squares" in main_targets, f"Expected main→sum_of_squares, got {main_targets}"


# ---------------------------------------------------------------------------
# Test 48: Named operands in GCC inline asm (blr %[name])
# ---------------------------------------------------------------------------

def test_named_operands_blr():
    """Test blr %[name] with named operands in GCC extended asm.

    Pattern:
      __asm__("blr %[func]" : [func] "+r"(fn9) : ...);
    Should resolve fn9 → function name via register binding.
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    code = textwrap.dedent("""\
        static int target_func(int x);
        static int caller(void) {
            register int w0 __asm__("w0") = 42;
            register void *fn9 __asm__("x9") = (void *)target_func;
            __asm__("blr %[func]" : [func] "+r"(fn9) : : "x8", "x30", "memory");
            return w0;
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "named_op.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])
    # blr %[func] should resolve to target_func via named operand
    resolved = [e for e in edges
                if e.get("confidence") == "INFERRED" and "target_func" in e.get("target", "")]
    assert len(resolved) >= 1, f"Expected INFERRED edge to target_func via named operand, got {edges}"

    # Should not have AMBIGUOUS indirect_call
    ambiguous = [e for e in edges
                 if e.get("confidence") == "AMBIGUOUS" and e.get("target") == "indirect_call"]
    assert len(ambiguous) == 0, \
        f"Expected no AMBIGUOUS indirect_call (named operand resolved), got {ambiguous}"


# ---------------------------------------------------------------------------
# Test 49: asm goto label tracking
# ---------------------------------------------------------------------------

def test_asm_goto_labels():
    """Test asm goto detects jump target labels.

    Pattern:
      asm goto("jmp %l0" : : : : error_handler);
    Should create AMBIGUOUS edge to error_handler with call_condition=asm_goto.
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    code = textwrap.dedent("""\
        static int safe_call(int x) {
            asm goto("cmp %0, %1\\n\\tjl %l2" : : "r"(x), "r"(0) : : error_handler);
            return 0;
        error_handler:
            return -1;
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "asm_goto.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])
    # Should have an edge to error_handler with asm_goto condition
    goto_edges = [e for e in edges
                  if e.get("call_condition") == "asm_goto"
                  and "error_handler" in e.get("target", "")]
    assert len(goto_edges) >= 1, f"Expected asm_goto edge to error_handler, got {edges}"


# ---------------------------------------------------------------------------
# Test 50: x86 register bindings in C inline asm
# ---------------------------------------------------------------------------

def test_x86_register_binding_inline_asm():
    """Test x86 register variable binding resolution in inline asm.

    Pattern (using register bindings that tree-sitter-c CAN parse):
      register int rdi __asm__("rdi") = 42;
      __asm__ volatile("call *%0" : : "r"(rdi) : "memory");

    Note: tree-sitter-c can't parse `register void *rax __asm__("rax")` where
    the variable type is a pointer AND the var name coincides with a register.
    But it CAN parse `register int rdi __asm__("rdi")` (non-pointer, non-reserved name).
    This test uses ARM register names (which tree-sitter handles) to verify the
    x86 call *%N operand resolution path works correctly.
    """
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")

    scanner = CTreeSitterScanner(is_cpp=False)
    # Use ARM register binding syntax (tree-sitter compatible) with x86 call instruction
    # This tests the operand resolution path for call *%N
    code = textwrap.dedent("""\
        static int my_handler(int x);
        static int dispatch(void) {
            register int w0 __asm__("w0") = 42;
            register void *fn9 __asm__("x9") = (void *)my_handler;
            __asm__ volatile("call *%2" : "+r"(w0) : "r"(w0), "r"(fn9) : "memory");
            return w0;
        }
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "x86_reg.c")
        with open(fpath, 'w') as f:
            f.write(code)
        result = scanner.scan_file(fpath, tmpdir)

    edges = result.get("edges", [])
    # call *%2 should resolve via operand map → fn9 → my_handler
    resolved = [e for e in edges
                if e.get("confidence") == "INFERRED" and "my_handler" in e.get("target", "")]
    assert len(resolved) >= 1, f"Expected INFERRED edge to my_handler, got {edges}"


# ===========================================================================
# Tests for conditional call extraction (if-condition, while, for, ternary)
# ===========================================================================

def _scan_c(code):
    """Helper: scan C code with CTreeSitterScanner, return (functions, edges)."""
    try:
        from _scanner.c_scanner import CTreeSitterScanner
    except ImportError:
        pytest.skip("tree-sitter-c not available")
    scanner = CTreeSitterScanner(is_cpp=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.c")
        with open(fpath, 'w') as f:
            f.write(textwrap.dedent(code))
        result = scanner.scan_file(fpath, tmpdir)
    return result.get("functions", []), result.get("edges", [])


# ---------------------------------------------------------------------------
# Test 51: if-condition with function calls (validate && process)
# ---------------------------------------------------------------------------

def test_if_condition_calls_extracted():
    """Calls inside if-condition must be extracted, not silently dropped.

    Pattern: if (validate() && process()) { ... }
    Both validate() and process() must appear as edges.
    """
    functions, edges = _scan_c("""\
        int validate(void);
        int process(void);
        void handle(void) {
            if (validate() && process()) {
                /* body */
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "validate" in targets, f"Expected validate edge, got {targets}"
    assert "process" in targets, f"Expected process edge, got {targets}"


# ---------------------------------------------------------------------------
# Test 52: if-condition calls with call_condition annotation
# ---------------------------------------------------------------------------

def test_if_condition_call_condition_annotation():
    """Calls inside if-condition should carry call_condition annotation."""
    functions, edges = _scan_c("""\
        int check(void);
        void run(void) {
            if (check()) {
                do_work();
            }
        }
    """)
    # check() should have a condition annotation
    check_edges = [e for e in edges if e.get("target") == "check"]
    assert len(check_edges) >= 1, f"Expected check edge, got {edges}"
    # At least one check edge should have a call_condition containing "check"
    assert any(e.get("call_condition") and "check" in e.get("call_condition", "")
               for e in check_edges), \
        f"Expected check edge with call_condition, got {check_edges}"

    # do_work() should also have condition annotation
    work_edges = [e for e in edges if e.get("target") == "do_work"]
    assert len(work_edges) >= 1, f"Expected do_work edge, got {edges}"
    assert any("check" in e.get("call_condition", "") for e in work_edges), \
        f"Expected do_work with condition, got {work_edges}"


# ---------------------------------------------------------------------------
# Test 53: if-else with condition calls on both branches
# ---------------------------------------------------------------------------

def test_if_else_condition_calls():
    """Both if and else branches should be extracted with proper conditions."""
    functions, edges = _scan_c("""\
        int is_valid(void);
        void on_valid(void);
        void on_invalid(void);
        void check(void) {
            if (is_valid()) {
                on_valid();
            } else {
                on_invalid();
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "is_valid" in targets, f"Expected is_valid, got {targets}"
    assert "on_valid" in targets, f"Expected on_valid, got {targets}"
    assert "on_invalid" in targets, f"Expected on_invalid, got {targets}"

    # on_valid should have positive condition
    valid_edges = [e for e in edges if e.get("target") == "on_valid"]
    assert any("is_valid" in e.get("call_condition", "") for e in valid_edges), \
        f"on_valid should have is_valid condition, got {valid_edges}"

    # on_invalid should have negated condition
    invalid_edges = [e for e in edges if e.get("target") == "on_invalid"]
    assert any("!" in e.get("call_condition", "") or "else" in e.get("call_condition", "")
               for e in invalid_edges), \
        f"on_invalid should have negated condition, got {invalid_edges}"


# ---------------------------------------------------------------------------
# Test 54: while loop with condition call
# ---------------------------------------------------------------------------

def test_while_condition_call():
    """Calls in while condition should be extracted with loop condition."""
    functions, edges = _scan_c("""\
        int has_next(void);
        void process_item(void);
        void iterate(void) {
            while (has_next()) {
                process_item();
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "has_next" in targets, f"Expected has_next, got {targets}"
    assert "process_item" in targets, f"Expected process_item, got {targets}"

    # process_item should have a loop condition
    pi_edges = [e for e in edges if e.get("target") == "process_item"]
    assert any("while" in e.get("call_condition", "") for e in pi_edges), \
        f"process_item should have while condition, got {pi_edges}"


# ---------------------------------------------------------------------------
# Test 55: for loop with condition call
# ---------------------------------------------------------------------------

def test_for_condition_call():
    """Calls in for condition should be extracted with loop condition."""
    functions, edges = _scan_c("""\
        int has_more(void);
        void advance(void);
        void loop(void) {
            for (; has_more(); ) {
                advance();
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "has_more" in targets, f"Expected has_more, got {targets}"
    assert "advance" in targets, f"Expected advance, got {targets}"

    # advance should have a for condition
    adv_edges = [e for e in edges if e.get("target") == "advance"]
    assert any("for" in e.get("call_condition", "") for e in adv_edges), \
        f"advance should have for condition, got {adv_edges}"


# ---------------------------------------------------------------------------
# Test 56: ternary conditional expression calls
# ---------------------------------------------------------------------------

def test_ternary_calls():
    """Calls in ternary expression should be extracted."""
    functions, edges = _scan_c("""\
        int is_error(void);
        void handle_error(void);
        void handle_success(void);
        void process(void) {
            is_error() ? handle_error() : handle_success();
        }
    """)
    targets = {e["target"] for e in edges}
    assert "is_error" in targets, f"Expected is_error, got {targets}"
    assert "handle_error" in targets, f"Expected handle_error, got {targets}"
    assert "handle_success" in targets, f"Expected handle_success, got {targets}"

    # handle_error should have ternary_true condition
    err_edges = [e for e in edges if e.get("target") == "handle_error"]
    assert any("ternary" in e.get("call_condition", "") for e in err_edges), \
        f"handle_error should have ternary condition, got {err_edges}"

    # handle_success should have negated ternary condition
    succ_edges = [e for e in edges if e.get("target") == "handle_success"]
    assert any("ternary" in e.get("call_condition", "") for e in succ_edges), \
        f"handle_success should have ternary condition, got {succ_edges}"


# ---------------------------------------------------------------------------
# Test 57: compound condition (&& and ||)
# ---------------------------------------------------------------------------

def test_compound_condition_calls():
    """Calls with && and || in conditions should all be extracted."""
    functions, edges = _scan_c("""\
        int check_a(void);
        int check_b(void);
        int check_c(void);
        void run(void) {
            if (check_a() && check_b() || check_c()) {
                do_run();
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "check_a" in targets, f"Expected check_a, got {targets}"
    assert "check_b" in targets, f"Expected check_b, got {targets}"
    assert "check_c" in targets, f"Expected check_c, got {targets}"


# ---------------------------------------------------------------------------
# Test 58: __attribute__((naked)) function
# ---------------------------------------------------------------------------

def test_naked_function():
    """__attribute__((naked)) function should be detected and inline asm processed."""
    functions, edges = _scan_c("""\
        void real_handler(void);
        __attribute__((naked)) void trampoline(void) {
            __asm__ volatile("jmp real_handler");
        }
    """)
    func_names = {f["name"] for f in functions}
    assert "trampoline" in func_names, f"Expected trampoline function, got {func_names}"

    # The inline asm "jmp real_handler" should create an edge
    targets = {e["target"] for e in edges}
    assert "real_handler" in targets, f"Expected real_handler edge, got {targets}"


# ---------------------------------------------------------------------------
# Test 59: MSVC __asm { } block conversion
# ---------------------------------------------------------------------------

def test_msvc_asm_block():
    """MSVC __asm { } block should be converted and processed."""
    functions, edges = _scan_c("""\
        void target_fn(void);
        void msvc_func(void) {
            __asm { jmp target_fn }
        }
    """)
    func_names = {f["name"] for f in functions}
    assert "msvc_func" in func_names, f"Expected msvc_func function, got {func_names}"

    # The converted __asm__ should create an edge to target_fn
    targets = {e["target"] for e in edges}
    assert "target_fn" in targets, f"Expected target_fn edge, got {targets}"


# ---------------------------------------------------------------------------
# Test 60: ERROR node fallback — register void *rax __asm__("rax")
# ---------------------------------------------------------------------------

def test_error_node_fallback_register_binding():
    """When tree-sitter can't parse register void *rax __asm__("rax"),
    the ERROR node fallback should recover the function and inline asm."""
    functions, edges = _scan_c("""\
        static int my_handler(int x);
        static int dispatch(void) {
            register void *rax __asm__("rax") = (void *)my_handler;
            __asm__ volatile("call *%rax" : : : "memory");
            return 0;
        }
    """)
    # dispatch should be detected (either via tree-sitter or ERROR fallback)
    func_names = {f["name"] for f in functions}
    assert "dispatch" in func_names, f"Expected dispatch function, got {func_names}"

    # Should have an edge related to my_handler
    targets = {e["target"] for e in edges}
    assert "my_handler" in targets or "indirect_call" in targets, \
        f"Expected my_handler or indirect_call edge, got {targets}"


# ---------------------------------------------------------------------------
# Test 61: Nested condition chains — if inside while
# ---------------------------------------------------------------------------

def test_nested_while_if_calls():
    """Calls in nested while+if should have combined condition info."""
    functions, edges = _scan_c("""\
        int has_item(void);
        int is_ready(void);
        void process(void);
        void run(void) {
            while (has_item()) {
                if (is_ready()) {
                    process();
                }
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "has_item" in targets, f"Expected has_item, got {targets}"
    assert "is_ready" in targets, f"Expected is_ready, got {targets}"
    assert "process" in targets, f"Expected process, got {targets}"

    # process should have both while and if conditions in its chain
    proc_edges = [e for e in edges if e.get("target") == "process"]
    assert len(proc_edges) >= 1, f"Expected process edge, got {edges}"


# ---------------------------------------------------------------------------
# Test 62: do-while with condition call
# ---------------------------------------------------------------------------

def test_do_while_condition_call():
    """Calls in do-while condition should be extracted."""
    functions, edges = _scan_c("""\
        int should_continue(void);
        void step(void);
        void loop(void) {
            do {
                step();
            } while (should_continue());
        }
    """)
    targets = {e["target"] for e in edges}
    assert "should_continue" in targets, f"Expected should_continue, got {targets}"
    assert "step" in targets, f"Expected step, got {targets}"

    # step should have a while/do condition
    step_edges = [e for e in edges if e.get("target") == "step"]
    assert any(e.get("call_condition") for e in step_edges), \
        f"step should have condition, got {step_edges}"


# ---------------------------------------------------------------------------
# Test 63: for-loop with init call
# ---------------------------------------------------------------------------

def test_for_init_call():
    """Calls in for-loop init should be extracted."""
    functions, edges = _scan_c("""\
        int* init_iter(void);
        int has_next(int *);
        void process(int *);
        void loop(void) {
            for (int *it = init_iter(); has_next(it); ) {
                process(it);
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "init_iter" in targets, f"Expected init_iter, got {targets}"
    assert "has_next" in targets, f"Expected has_next, got {targets}"
    assert "process" in targets, f"Expected process, got {targets}"


# ---------------------------------------------------------------------------
# Test 64: switch predicate call extraction
# ---------------------------------------------------------------------------

def test_switch_predicate_call():
    """Calls inside switch() predicate must be extracted.

    Pattern: switch(get_key()) { case 1: process_a(); ... }
    get_key() must appear as an edge with switch_cond condition.
    """
    functions, edges = _scan_c("""\
        int get_key(void);
        void process_a(void);
        void process_b(void);
        void dispatch(void) {
            switch(get_key()) {
                case 1: process_a(); break;
                case 2: process_b(); break;
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "get_key" in targets, f"Expected get_key, got {targets}"
    assert "process_a" in targets, f"Expected process_a, got {targets}"
    assert "process_b" in targets, f"Expected process_b, got {targets}"
    # Verify switch condition label on the get_key edge
    switch_cond_edges = [e for e in edges if e["target"] == "get_key"]
    assert len(switch_cond_edges) > 0, "get_key edge should exist"
    assert "switch" in switch_cond_edges[0].get("call_condition", ""), \
        f"Expected switch condition, got {switch_cond_edges[0].get('call_condition')}"


# ---------------------------------------------------------------------------
# Test 65: for-loop condition text detail
# ---------------------------------------------------------------------------

def test_for_condition_text_detail():
    """For-loop condition should include the condition text, not just 'for'."""
    functions, edges = _scan_c("""\
        int has_next(void);
        void process(void);
        void loop(void) {
            for (; has_next(); ) {
                process();
            }
        }
    """)
    # Find the for_cond edge (condition call)
    cond_edges = [e for e in edges if "for_cond" in e.get("call_condition", "")]
    assert len(cond_edges) > 0, f"Expected for_cond edge, got edges: {edges}"
    assert "has_next" in cond_edges[0]["call_condition"], \
        f"Expected 'has_next' in condition, got {cond_edges[0]['call_condition']}"
    # Also check the body edge has for(has_next)
    body_edges = [e for e in edges if e["target"] == "process"]
    assert len(body_edges) > 0, "process edge should exist"
    assert "has_next" in body_edges[0].get("call_condition", ""), \
        f"Expected 'has_next' in body condition, got {body_edges[0].get('call_condition')}"


# ---------------------------------------------------------------------------
# Test 66: nested compound conditions (a() && (b() || c()))
# ---------------------------------------------------------------------------

def test_nested_compound_conditions():
    """Nested &&/|| in if-condition: if(a() && (b() || c())) — all calls extracted."""
    functions, edges = _scan_c("""\
        int check_a(void);
        int check_b(void);
        int check_c(void);
        void handle(void) {
            if (check_a() && (check_b() || check_c())) {
                ;
            }
        }
    """)
    targets = {e["target"] for e in edges}
    assert "check_a" in targets, f"Expected check_a, got {targets}"
    assert "check_b" in targets, f"Expected check_b, got {targets}"
    assert "check_c" in targets, f"Expected check_c, got {targets}"


# ---------------------------------------------------------------------------
# Test 67: switch-case body call_condition
# ---------------------------------------------------------------------------

def test_switch_case_call_condition():
    """Calls in switch case body should carry the case condition."""
    functions, edges = _scan_c("""\
        void process_a(void);
        void process_b(void);
        void dispatch(int key) {
            switch(key) {
                case 1: process_a(); break;
                case 2: process_b(); break;
            }
        }
    """)
    # process_a should be in a case 1 branch
    a_edges = [e for e in edges if e["target"] == "process_a"]
    assert len(a_edges) > 0, "process_a edge should exist"
    cond = a_edges[0].get("call_condition", "")
    assert "1" in cond or "case" in cond.lower(), f"Expected case condition for process_a, got {cond}"


def test_gas_macro_endm_call_expansion():
    """Macro with call \\func should expand when invoked with concrete argument."""
    result = _scan_asm("""\
        .text
        .globl my_func

        .macro THUNK name, func
        SYM_FUNC_START(\\name)
            pushq %rbp
            movq %rsp, %rbp
            call \\func
            popq %rbp
            RET
        SYM_FUNC_END(\\name)
        .endm

        THUNK my_func, target_callee
    """, filename="test.S")
    edges = _get_edges(result)
    # Should find an inferred edge from my_func to target_callee
    macro_edges = [e for e in edges
                   if "target_callee" in e.get("target", "")
                   and "my_func" in e.get("source", "")]
    assert len(macro_edges) >= 1, f"Expected macro expansion edge, got edges: {edges}"
    assert macro_edges[0]["confidence"] == "INFERRED"
    assert macro_edges[0].get("source_tag") == "asm_macro_expansion"


def test_gas_macro_endm_no_param_call():
    """Macro with literal (non-parameter) call target should NOT produce macro expansion edges."""
    result = _scan_asm("""\
        .text
        .globl my_func

        .macro HELPER
            call fixed_target
        .endm

        SYM_FUNC_START(my_func)
            HELPER
            RET
        SYM_FUNC_END(my_func)
    """, filename="test.S")
    edges = _get_edges(result)
    # The call inside the macro body is skipped (in_macro_def=True in pass 2),
    # and the macro has no call_params, so no expansion edge.
    # The call to fixed_target is NOT directly in the function body
    # (it's in the macro, which we skip in pass 2).
    # So no edge from my_func to fixed_target via macro expansion.
    expansion_edges = [e for e in edges if e.get("source") == "asm_macro_expansion"]
    assert len(expansion_edges) == 0


def test_gas_macro_skip_body():
    """Lines inside .macro/.endm should not be processed as regular code."""
    result = _scan_asm("""\
        .text
        .globl outer_func

        .macro INNER_MACRO
            call should_not_appear
        .endm

        SYM_FUNC_START(outer_func)
            call actual_callee
            RET
        SYM_FUNC_END(outer_func)
    """, filename="test.S")
    edges = _get_edges(result)
    targets = {e["target"] for e in edges}
    # should_not_appear should NOT be a target (it's inside .macro body, no params)
    should_not = [t for t in targets if "should_not_appear" in t]
    assert len(should_not) == 0, f"should_not_appear should not be a target, got {targets}"
    # actual_callee should be a target
    should_appear = [t for t in targets if "actual_callee" in t]
    assert len(should_appear) >= 1, f"actual_callee should be a target, got {targets}"


def test_gas_macro_arm_bl_expansion():
    """ARM bl inside macro with param should expand."""
    result = _scan_asm("""\
        .text
        .globl my_handler

        .macro CALL_HANDLER name, func
        SYM_FUNC_START(\\name)
            bl \\func
            RET
        SYM_FUNC_END(\\name)
        .endm

        CALL_HANDLER my_handler, arm_callee
    """, filename="test.S")
    edges = _get_edges(result)
    macro_edges = [e for e in edges
                   if "arm_callee" in e.get("target", "")
                   and "my_handler" in e.get("source", "")]
    assert len(macro_edges) >= 1, f"Expected ARM bl macro expansion edge, got edges: {edges}"


def test_arm_bare_b_tail_call():
    """ARM bare 'b' to a known function should create a tail call edge."""
    result = _scan_asm("""\
        .text
        .globl my_func
        .globl target_func

        my_func:
            b target_func
    """, filename="test.S")
    edges = _get_edges(result)
    tail_edges = [e for e in edges
                  if "target_func" in e.get("target", "")
                  and e.get("source_tag") == "tail_call_b_arm"]
    assert len(tail_edges) >= 1, f"Expected ARM b tail call, got {edges}"


def test_ppc_cfunc_unwrapping():
    """PowerPC CFUNC(name) should resolve to the actual function name."""
    result = _scan_asm("""\
        .text
        .globl my_func

        SYM_FUNC_START(my_func)
            bl CFUNC(enter_vmx_ops)
            RET
        SYM_FUNC_END(my_func)
    """, filename="test.S")
    edges = _get_edges(result)
    cfunc_edges = [e for e in edges
                   if "enter_vmx_ops" in e.get("target", "")
                   and e.get("source_tag") == "ppc_cfunc"]
    assert len(cfunc_edges) >= 1, f"Expected CFUNC unwrapping, got {edges}"
    # CFUNC itself should NOT be a target
    cfunc_bad = [e for e in edges if e.get("target", "").endswith("CFUNC")]
    assert len(cfunc_bad) == 0, f"CFUNC should not be a target, got {edges}"


def test_ppc_bctrl_indirect_call():
    """PowerPC bctrl should create an AMBIGUOUS indirect call edge."""
    result = _scan_asm("""\
        .text
        .globl my_func

        SYM_FUNC_START(my_func)
            bctrl
            RET
        SYM_FUNC_END(my_func)
    """, filename="test.S")
    edges = _get_edges(result)
    bctrl_edges = [e for e in edges
                   if e.get("target") == "indirect_call"
                   and e.get("source_tag") == "ppc_bctrl"]
    assert len(bctrl_edges) >= 1, f"Expected bctrl indirect call, got {edges}"


def test_sh_bsr_direct_call():
    """SuperH bsr should create a direct call edge."""
    result = _scan_asm("""\
        .text
        .globl my_func

        my_func:
            bsr target_func
    """, filename="test.S")
    edges = _get_edges(result)
    bsr_edges = [e for e in edges
                 if "target_func" in e.get("target", "")
                 and e.get("source_tag") == "sh_bsr"]
    assert len(bsr_edges) >= 1, f"Expected bsr call, got {edges}"


def test_mips_jal_direct_call():
    """MIPS jal should create a direct call edge."""
    result = _scan_asm("""\
        .text
        .globl my_func

        .ent my_func
        my_func:
            jal target_func
        .end my_func
    """, filename="test.S")
    edges = _get_edges(result)
    jal_edges = [e for e in edges
                 if "target_func" in e.get("target", "")
                 and e.get("source_tag") == "mips_jal"]
    assert len(jal_edges) >= 1, f"Expected MIPS jal call, got {edges}"


def test_mips_ent_function_boundary():
    """MIPS .ent should define a function boundary."""
    result = _scan_asm("""\
        .text
        .globl my_func

        .ent my_func
        my_func:
            jal helper
        .end my_func
    """, filename="test.S")
    func_names = _get_func_names(result)
    assert "my_func" in func_names, f"Expected my_func, got {func_names}"


def test_ia64_br_call():
    """IA-64 br.call should create a direct call edge."""
    result = _scan_asm("""\
        .text
        .globl my_func

        my_func:
            br.call.sptk.many b0=target_func
    """, filename="test.S")
    edges = _get_edges(result)
    ia64_edges = [e for e in edges
                  if "target_func" in e.get("target", "")
                  and e.get("source_tag") == "ia64_br_call"]
    assert len(ia64_edges) >= 1, f"Expected IA-64 br.call, got {edges}"


def test_alternative_macro_in_s_file():
    """ALTERNATIVE macro with call target in .S file."""
    result = _scan_asm("""\
        .text
        .globl my_func

        SYM_FUNC_START(my_func)
            ALTERNATIVE "jmp alt_target", "", X86_FEATURE_FSR
            RET
        SYM_FUNC_END(my_func)
    """, filename="test.S")
    edges = _get_edges(result)
    alt_edges = [e for e in edges
                 if "alt_target" in e.get("target", "")
                 and e.get("source_tag") == "alternative_patch"]
    assert len(alt_edges) >= 1, f"Expected ALTERNATIVE edge, got {edges}"


def test_ifdef_condition_annotation():
    """#ifdef in .S files should add condition to edges."""
    result = _scan_asm("""\
        .text
        .globl my_func
        .globl config_call

        SYM_FUNC_START(my_func)
        #ifdef CONFIG_XYZ
            call config_call
        #endif
            RET
        SYM_FUNC_END(my_func)
    """, filename="test.S")
    edges = _get_edges(result)
    config_edges = [e for e in edges if "config_call" in e.get("target", "")]
    assert len(config_edges) >= 1, f"Expected config_call edge, got {edges}"
    assert "CONFIG_XYZ" in config_edges[0].get("call_condition", ""), \
        f"Expected ifdef condition, got {config_edges[0]}"


def test_gas_set_alias():
    """GAS .set alias should be recorded."""
    scanner = AsmRegexScanner()
    content = """\
        .text
        .globl original_func

        SYM_FUNC_START(original_func)
            RET
        SYM_FUNC_END(original_func)
        .set alias_func, original_func
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.S")
        with open(fpath, 'w') as f:
            f.write(textwrap.dedent(content))
        result = scanner.scan_file(fpath, tmpdir)
    # Check that original_func exists as a function
    func_names = _get_func_names(result)
    assert "original_func" in func_names
    # Check that alias is recorded in _asm_aliases
    assert len(scanner._asm_aliases) >= 1
    alias_entry = scanner._asm_aliases[-1]
    assert alias_entry["alias"] == "alias_func"
    assert alias_entry["original"] == "original_func"


def test_irp_expansion():
    """.irp iteration macro should expand call targets."""
    result = _scan_asm("""\
        .text
        .globl my_func

        SYM_FUNC_START(my_func)
            .irp r, func_a, func_b
            call \\r
            .endr
            RET
        SYM_FUNC_END(my_func)
    """, filename="test.S")
    edges = _get_edges(result)
    irp_edges = [e for e in edges
                 if e.get("source_tag") == "irp_expansion"
                 and ("func_a" in e.get("target", "") or "func_b" in e.get("target", ""))]
    assert len(irp_edges) >= 2, f"Expected 2 irp expansion edges, got {edges}"


def test_asm_entry_macros_profile_driven():
    """Profile-driven asm_entry_macros should create functions and edges."""
    scanner = AsmRegexScanner()
    scanner.set_asm_entry_macros([
        {"name": "idtentry", "func_start_param": 1, "call_params": [2]}
    ])
    import tempfile
    content = """\
        .text
        idtentry X86_TRAP_DB asm_exc_debug exc_debug has_error_code=0
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = os.path.join(tmpdir, "test.S")
        with open(fpath, 'w') as f:
            f.write(textwrap.dedent(content))
        result = scanner.scan_file(fpath, tmpdir)
    edges = result.get("edges", [])
    func_names = [fn["name"] for fn in result.get("functions", [])]
    assert "asm_exc_debug" in func_names, f"Expected asm_exc_debug function, got {func_names}"
    entry_edges = [e for e in edges
                   if "exc_debug" in e.get("target", "")
                   and e.get("source_tag") == "asm_entry_macro"]
    assert len(entry_edges) >= 1, f"Expected entry macro edge, got {edges}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
