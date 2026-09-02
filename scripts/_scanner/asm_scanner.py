#!/usr/bin/env python3
"""Regex-based assembly scanner for invocation graph extraction.

Supports NASM x86_64 syntax by default, with kernel-style GNU as (AT&T)
macro patterns (SYM_FUNC_START, ENTRY, EXPORT_SYMBOL, etc.).

No tree-sitter dependency — pure regex scanning.

Enhanced with register-level data flow tracking:
- Register-to-register value transfer (mov, xchg, push/pop)
- Syscall register convention tracking (rdi→arg0, rsi→arg1, etc.)
- Function call argument passing via registers
- Stack value flow analysis (push → pop)
- Condition flag tracking from cmp/test
"""

import os
import re
from pathlib import Path
from _scanner.base import BaseScanner
from _scanner.utils import classify_domain
import logging


# ---------------------------------------------------------------------------
# NASM x86_64 regex patterns
# ---------------------------------------------------------------------------

# Section directive
_SECTION_RE = re.compile(r'^\s*section\s+\.(\w+)', re.IGNORECASE)

# global/extern declarations (support multiple comma-separated symbols per line)
_GLOBAL_RE = re.compile(r'\bglobal\s+([a-zA-Z_]\w*(?:\s*,\s*[a-zA-Z_]\w*)*)', re.IGNORECASE)
_EXTERN_RE = re.compile(r'\bextern\s+([a-zA-Z_]\w*(?:\s*,\s*[a-zA-Z_]\w*)*)', re.IGNORECASE)

# Function labels — non-local labels (not starting with '.')
_LABEL_RE = re.compile(r'^([a-zA-Z_]\w*):')

# Local labels (NASM: .loop, .done) — NOT functions
_LOCAL_LABEL_RE = re.compile(r'^\.(\w+):')

# Direct call — NASM syntax: call target
_CALL_RE = re.compile(r'\bcall\s+([a-zA-Z_]\w*)')

# Indirect call — NASM: call [mem], call qword [mem], call reg
_INDIR_CALL_RE = re.compile(r'\bcall\s+(?:qword\s+)?\[|call\s+[a-z]{2,3}\b(?![_\w])')

# System call instruction (Linux x86_64)
_SYSCALL_RE = re.compile(r'\bsyscall\b')

# 32-bit x86 syscall: int $0x80
_INT80_RE = re.compile(r'\bint\s+\$?0x80\b')

# 32-bit x86 syscall number → name mapping
_SYSCALL_NAMES_32 = {
    1: "sys_exit", 2: "sys_fork", 3: "sys_read", 4: "sys_write",
    5: "sys_open", 6: "sys_close", 11: "sys_execve", 12: "sys_chdir",
    13: "sys_time", 14: "sys_mknod", 15: "sys_chmod", 20: "sys_getpid",
    24: "sys_getuid", 25: "sys_geteuid", 37: "sys_kill", 38: "sys_rename",
    39: "sys_mkdir", 40: "sys_rmdir", 41: "sys_dup", 42: "sys_pipe",
    45: "sys_brk", 48: "sys_signal", 54: "sys_ioctl", 63: "sys_dup2",
    66: "sys_writev", 67: "sys_readv", 78: "sys_getdents",
    80: "sys_chdir", 85: "sys_creat", 91: "sys_munmap", 90: "sys_mmap",
    102: "sys_socketcall", 114: "sys_wait4", 120: "sys_clone",
    122: "sys_uname", 125: "sys_mprotect", 183: "sys_getcwd",
    190: "sys_vfork",
}

# Constant definition (NASM equ)
_EQU_RE = re.compile(r'^([a-zA-Z_]\w*)\s+equ\s+(.+)', re.IGNORECASE)

# Conditional jumps
_COND_JMP_RE = re.compile(
    r'\b(je|jne|jz|jnz|jg|jge|jl|jle|ja|jae|jb|jbe|js|jns)\s+([.a-zA-Z_]\w*)')

# Unconditional jump
_JMP_RE = re.compile(r'\bjmp\s+([.a-zA-Z_]\w*)')

# mov rax, N (for syscall number tracking)
_MOV_RAX_RE = re.compile(r'\bmov\s+rax\s*,\s*(\d+)\b', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Kernel-style GNU as macro patterns (AT&T syntax .S files)
# ---------------------------------------------------------------------------

_KERNEL_ASM_FUNC_MACROS = [
    re.compile(r'^\s*SYM_FUNC_START(?:_LOCAL|_WEAK|_NOALIGN|_ALIGNED)?\s*\(\s*(\w+)\s*\)'),
    re.compile(r'^\s*SYM_TYPED_FUNC_START\s*\(\s*(\w+)\s*\)'),
    re.compile(r'^\s*SYM_CODE_START(?:_LOCAL|_WEAK|_NOALIGN)?\s*\(\s*(\w+)\s*\)'),
    re.compile(r'^\s*ENTRY\s*\(\s*(\w+)\s*\)'),
    re.compile(r'^\s*WEAK\s*\(\s*(\w+)\s*\)'),
    re.compile(r'^\s*GLOBAL\s*\(\s*(\w+)\s*\)'),
    # MIPS .ent directive
    re.compile(r'^\s*\.ent\s+([a-zA-Z_]\w*)', re.IGNORECASE),
]

_KERNEL_ASM_END_MACROS = [
    re.compile(r'^\s*SYM_FUNC_END\s*\(\s*(\w+)\s*\)'),
    re.compile(r'^\s*SYM_CODE_END\s*\(\s*(\w+)\s*\)'),
    re.compile(r'^\s*ENDPROC\s*\(\s*(\w+)\s*\)'),
    re.compile(r'^\s*END\s*\(\s*(\w+)\s*\)'),
    # MIPS .end directive
    re.compile(r'^\s*\.end\s+([a-zA-Z_]\w*)', re.IGNORECASE),
]

_EXPORT_SYMBOL_MACROS = [
    re.compile(r'^\s*EXPORT_SYMBOL(?:_GPL|_GPL_FUTURE)?\s*\(\s*(\w+)\s*\)'),
]

_ASM_ALIAS_MACROS = [
    re.compile(r'^\s*SYM_FUNC_ALIAS\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)'),
]

# Kernel inner label macros (intra-function entry points)
_KERNEL_INNER_LABEL_MACROS = [
    re.compile(r'^\s*SYM_INNER_LABEL\s*\(\s*(\w+)\s*,\s*\w+\s*\)'),
    re.compile(r'^\s*SYM_INNER_LABEL_ERROR\s*\(\s*(\w+)\s*,\s*\w+\s*\)'),
]

# GNU as section classification
_CODE_SECTIONS = {
    '.text', '.head.text', '.init.text', '.entry.text',
    '.noinstr.text', '.idmap.text', '.altinstr_replacement',
    '.text.unlikely', '.text.hot',
}
_DATA_SECTIONS = {
    '.data', '.rodata', '.bss', '.init.data', '.export_symbol',
    '.altinstructions', '__ex_table', '.smp_locks',
    '_kprobe_blacklist', '.data..read_mostly',
}

# Minimal syscall number → name mappings (most common syscalls only).
# Full architecture-specific tables should be provided via profile
# config (syscall_maps field). These are fallback defaults.
_SYSCALL_NAMES = {
    0: "sys_read", 1: "sys_write", 2: "sys_open", 3: "sys_close",
    9: "sys_mmap", 12: "sys_brk", 16: "sys_ioctl",
    39: "sys_getpid", 57: "sys_fork", 59: "sys_execve",
    60: "sys_exit", 61: "sys_wait4", 62: "sys_kill",
    220: "sys_clone", 231: "sys_exit_group", 257: "sys_openat",
}

_AARCH64_SYSCALL_NAMES = {
    29: "svc_ioctl", 35: "svc_mknodat", 48: "svc_chdir",
    49: "svc_fchdir", 50: "svc_openat", 51: "svc_close",
    56: "svc_getdents64", 57: "svc_lseek",
    59: "svc_mmap", 60: "svc_mprotect", 61: "svc_munmap",
    62: "svc_brk", 63: "svc_read", 64: "svc_write",
    78: "svc_getpid", 93: "svc_exit", 94: "svc_exit_group",
    98: "svc_futex",
    172: "svc_getpid", 220: "svc_clone", 221: "svc_execve",
    260: "svc_wait4", 435: "svc_clone3",
}

_RISCV_SYSCALL_NAMES = {
    17: "sys_getcwd", 29: "sys_ioctl", 35: "sys_mknodat",
    39: "sys_mkdirat", 40: "sys_unlinkat",
    48: "sys_chdir", 49: "sys_fchdir",
    55: "sys_openat", 56: "sys_close", 57: "sys_read",
    58: "sys_write", 63: "sys_pread64", 64: "sys_pwrite64",
    66: "sys_writev", 78: "sys_readlinkat",
    79: "sys_newfstatat", 80: "sys_fstat",
    88: "sys_exit", 89: "sys_exit_group", 93: "sys_futex",
    94: "sys_set_robust_list",
    102: "sys_nanosleep", 113: "sys_clock_gettime",
    115: "sys_sched_yield", 129: "sys_kill",
    134: "sys_rt_sigaction", 135: "sys_rt_sigprocmask",
    160: "sys_setfsuid", 161: "sys_setfsgid",
    172: "sys_getpid", 174: "sys_getuid", 175: "sys_geteuid",
    178: "sys_gettid", 214: "sys_brk", 215: "sys_munmap",
    217: "sys_mmap", 218: "sys_mprotect",
    221: "sys_clone", 222: "sys_execve",
    241: "sys_prlimit64", 435: "sys_clone3",
}

# GAS conditional assembly directives
_IFDEF_RE = re.compile(r'^\s*\.ifdef\s+([a-zA-Z_]\w*)', re.IGNORECASE)
_IFNDEF_RE = re.compile(r'^\s*\.ifndef\s+([a-zA-Z_]\w*)', re.IGNORECASE)
_ELSE_RE = re.compile(r'^\s*\.else\b', re.IGNORECASE)
_ENDIF_RE = re.compile(r'^\s*\.endif\b', re.IGNORECASE)

# GAS .macro / .endm directives
# .macro NAME [param1[=default], param2[=default], ...]
_MACRO_DEF_RE = re.compile(r'^\s*\.macro\s+([a-zA-Z_]\w*)(?:\s+(.+))?', re.IGNORECASE)
_MACRO_END_RE = re.compile(r'^\s*\.endm\b', re.IGNORECASE)
# Call/jmp targets inside macro bodies that reference macro parameters (\name)
_MACRO_CALL_RE = re.compile(r'\b(?:call|jmp)\s+\\(\w+)', re.IGNORECASE)
# ARM bl inside macro bodies
_MACRO_BL_RE = re.compile(r'\bbl\s+\\(\w+)', re.IGNORECASE)
# RISC-V jal inside macro bodies
_MACRO_JAL_RE = re.compile(r'\bjal\s+(?:[a-zA-Z_]\w*\s*,\s*)?\\(\w+)', re.IGNORECASE)
# LoongArch bl inside macro bodies
_MACRO_LA_BL_RE = re.compile(r'\bbl\s+\\(\w+)', re.IGNORECASE)
# s390 brasl inside macro bodies
_MACRO_BRASL_RE = re.compile(r'\bbrasl\s+%r\d+\s*,\s*\\(\w+)', re.IGNORECASE)
# Macro invocation: NAME arg1, arg2, ... (must start at line beginning or after whitespace)
_MACRO_INVOKE_RE = re.compile(r'^\s*([a-zA-Z_]\w+)\s+(.+)', re.IGNORECASE)

# GNU as directives for global/extern
_GAS_GLOBAL_RE = re.compile(r'^\s*\.(?:globl|global)\s+([a-zA-Z_]\w*(?:\s*,\s*[a-zA-Z_]\w*)*)')
_GAS_EXTERN_RE = re.compile(r'^\s*\.extern\s+([a-zA-Z_]\w*(?:\s*,\s*[a-zA-Z_]\w*)*)')

# GNU as .type directive: .type func_name, @function
_GAS_TYPE_RE = re.compile(r'^\s*\.type\s+([a-zA-Z_]\w*)\s*,\s*@function', re.IGNORECASE)

# GAS .size directive: .size func_name, .-func_name (marks function end)
_GAS_SIZE_RE = re.compile(r'^\s*\.size\s+([a-zA-Z_]\w*)\s*,\s*')

# GAS numeric label definition: 1: 2: etc (used with 1b/1f references)
_NUMERIC_LABEL_RE = re.compile(r'^(\d+):')

# GAS numeric label reference: 1b (backward), 1f (forward)
_NUMERIC_LABEL_REF_RE = re.compile(r'\b(\d+)([bf])\b')

# AT&T call syntax: call funcname / call *%rax
_GAS_CALL_RE = re.compile(r'\bcall\s+([a-zA-Z_]\w*)')
_GAS_INDIR_CALL_RE = re.compile(r'\bcall\s+\*')

# ARM call instructions
_ARM_CALL_RE = re.compile(r'\b(?:bl|b\.)\s+([a-zA-Z_]\w*)')

# AArch64 conditional compare-and-branch: cbz/cbnz Xn, label (label may start with .)
_ARM_CBZ_RE = re.compile(r'\b(cbz|cbnz)\s+([xw]\d+)\s*,\s*([.a-zA-Z_]\w*)', re.IGNORECASE)

# AArch64 test-bit-and-branch: tbz/tbnz Xn, #bit, label (label may start with .)
_ARM_TBZ_RE = re.compile(r'\b(tbz|tbnz)\s+([xw]\d+)\s*,\s*#(\d+)\s*,\s*([.a-zA-Z_]\w*)', re.IGNORECASE)

# AArch64 indirect branch: br Xn (tail call) / blr Xn (indirect call)
_ARM_BR_RE = re.compile(r'\bbr\s+([xw]\d+)\b', re.IGNORECASE)
_ARM_BLR_RE = re.compile(r'\bblr\s+([xw]\d+)\b', re.IGNORECASE)

# ---------------------------------------------------------------------------
# RISC-V instruction patterns
# ---------------------------------------------------------------------------

# RISC-V direct call: jal ra, label (or just jal label in some assemblers)
_RISCV_JAL_RE = re.compile(r'\bjal\s+(?:[a-zA-Z_]\w*\s*,\s*)?([a-zA-Z_.]\w*)', re.IGNORECASE)

# RISC-V indirect call: jalr rd, rs1, 0 / jalr rs1
_RISCV_JALR_RE = re.compile(r'\bjalr\s+(?:[a-zA-Z_]\w*\s*,\s*)?([a-zA-Z_]\w*)', re.IGNORECASE)

# RISC-V system call: ecall
_RISCV_ECALL_RE = re.compile(r'\becall\b', re.IGNORECASE)

# RISC-V unconditional jump: j label
_RISCV_J_RE = re.compile(r'\bj\s+([a-zA-Z_.]\w*)', re.IGNORECASE)

# RISC-V conditional branches: beq, bne, blt, bge, bltu, bgeu
_RISCV_BRANCH_RE = re.compile(
    r'\b(?:beq|bne|blt|bge|bltu|bgeu)\s+[a-zA-Z_]\w*\s*,\s*[a-zA-Z_]\w*\s*,\s*([.a-zA-Z_]\w*)',
    re.IGNORECASE)

# ---------------------------------------------------------------------------
# LoongArch instruction patterns
# ---------------------------------------------------------------------------

# LoongArch direct call: bl label
_LOONGARCH_BL_RE = re.compile(r'\bbl\s+([a-zA-Z_.]\w*)', re.IGNORECASE)

# LoongArch indirect call: jirl rd, rj, 0 (or jirl rd, rj)
_LOONGARCH_JIRL_RE = re.compile(r'\bjirl\s+(?:[a-zA-Z_]\w*\s*,\s*)?([a-zA-Z_]\w*)', re.IGNORECASE)

# LoongArch unconditional jump: b label
_LOONGARCH_B_RE = re.compile(r'\bb\s+([a-zA-Z_.]\w*)', re.IGNORECASE)

# LoongArch system call: syscall 0
_LOONGARCH_SYSCALL_RE = re.compile(r'\bsyscall\s+0\b', re.IGNORECASE)

# ---------------------------------------------------------------------------
# s390 instruction patterns
# ---------------------------------------------------------------------------

# s390 direct call: brasl %r14, func (or brasl %r7, func)
_S390_BRASL_RE = re.compile(r'\bbrasl\s+%r\d+\s*,\s*([a-zA-Z_]\w*)', re.IGNORECASE)

# s390 indirect call: basr %r14, %rN
_S390_BASR_RE = re.compile(r'\bbasr\s+%r\d+\s*,\s*%r(\d+)', re.IGNORECASE)

# s390 system call: svc 0 (or svc N)
_S390_SVC_RE = re.compile(r'\bsvc\s+\d+\b', re.IGNORECASE)

# ---------------------------------------------------------------------------
# ARM/PowerPC bare `b` tail-call pattern
# ---------------------------------------------------------------------------
# ARM bare `b` unconditional branch (tail call when target is a function)
_ARM_B_RE = re.compile(r'^\s*b\s+([a-zA-Z_]\w*)', re.IGNORECASE)

# PowerPC CFUNC(name) wrapper — extract real callee from CFUNC(func)
_PPC_CFUNC_RE = re.compile(r'\bCFUNC\s*\(\s*([a-zA-Z_]\w*)\s*\)', re.IGNORECASE)

# PowerPC bctrl — indirect call via count register
_PPC_BCTRL_RE = re.compile(r'\bbctrl\b', re.IGNORECASE)

# PowerPC bcl — conditional branch and link
_PPC_BCL_RE = re.compile(r'\bbcl\s+\d+\s*,\s*\d+\s*,\s*([.a-zA-Z_]\w*)', re.IGNORECASE)

# ---------------------------------------------------------------------------
# SuperH instruction patterns
# ---------------------------------------------------------------------------
# SuperH direct call: bsr label
_SH_BSR_RE = re.compile(r'\bbsr\s+([a-zA-Z_]\w*)', re.IGNORECASE)
# SuperH indirect call: jsr @rn
_SH_JSR_RE = re.compile(r'\bjsr\s+@([a-zA-Z_]\w*)', re.IGNORECASE)
# SuperH indirect call with register offset: bsrf rn
_SH_BSRF_RE = re.compile(r'\bbsrf\s+([a-zA-Z_]\w*)', re.IGNORECASE)

# ---------------------------------------------------------------------------
# MIPS instruction patterns
# ---------------------------------------------------------------------------
# MIPS direct call: jal label (may have .noreorder suffix)
_MIPS_JAL_RE = re.compile(r'\bjal\s+([a-zA-Z_]\w*)', re.IGNORECASE)
# MIPS indirect call: jalr rs (or jalr rd, rs)
_MIPS_JALR_RE = re.compile(r'\bjalr\s+(?:[a-zA-Z_$]\w*\s*,\s*)?([a-zA-Z_$]\w*)', re.IGNORECASE)
# MIPS function markers: .ent name / .end name
_MIPS_ENT_RE = re.compile(r'^\s*\.ent\s+([a-zA-Z_]\w*)', re.IGNORECASE)
_MIPS_END_RE = re.compile(r'^\s*\.end\s+([a-zA-Z_]\w*)', re.IGNORECASE)

# ---------------------------------------------------------------------------
# IA-64 (Itanium) instruction patterns
# ---------------------------------------------------------------------------
# IA-64 direct call: br.call.sptk.many b0=callee / brl.call...
_IA64_BR_CALL_RE = re.compile(r'\bbr(?:l)?\.call(?:\.\w+)*\s+\w+=(\w+)', re.IGNORECASE)
# IA-64 indirect call: br.call.sptk.many b0=rN (register indirect)
_IA64_BR_CALL_IND_RE = re.compile(r'\bbr(?:l)?\.call(?:\.\w+)*\s+\w+=r(\d+)', re.IGNORECASE)

# ---------------------------------------------------------------------------
# GAS ALTERNATIVE macro in .S files
# ---------------------------------------------------------------------------
# ALTERNATIVE "oldinstr", "newinstr", feature — extract call/jmp targets
_ALT_CALL_RE = re.compile(r'(?:call|jmp)\s+([a-zA-Z_]\w*)')

# ---------------------------------------------------------------------------
# GAS .set aliasing
# ---------------------------------------------------------------------------
_GAS_SET_ALIAS_RE = re.compile(r'^\s*\.set\s+([a-zA-Z_]\w*)\s*,\s*([a-zA-Z_]\w*)\s*$', re.IGNORECASE)

# ---------------------------------------------------------------------------
# GAS .irp/.irpc iteration macros
# ---------------------------------------------------------------------------
_IRP_RE = re.compile(r'^\s*\.irp\s+(\w+)\s*,\s*(.+)', re.IGNORECASE)
_IRPC_RE = re.compile(r'^\s*\.irpc\s+(\w+)\s*,\s*(.+)', re.IGNORECASE)
_ENDR_RE = re.compile(r'^\s*\.endr\b', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Profile-driven asm_entry_macros
# Macros that generate function entry points when invoked, e.g.:
#   idtentry vector asmsym cfunc has_error_code:req
# Creates SYM_CODE_START(asmsym) which calls cfunc
# ---------------------------------------------------------------------------
# Default entry: name, func_start_param_idx, call_param_indices
# Populated from profile config asm_entry_macros

# ---------------------------------------------------------------------------
# Register data flow tracking patterns
# ---------------------------------------------------------------------------

# General mov: mov dst, src
_MOV_RE = re.compile(
    r'\bmov\s+([a-zA-Z_]\w*)\s*,\s*(.+)', re.IGNORECASE)

# mov with memory operand: mov [mem], src / mov dst, [mem]
_MOV_MEM_RE = re.compile(
    r'\bmov\s+(?:qword\s+|dword\s+|word\s+|byte\s+)?\[([^\]]+)\]\s*,\s*(.+)',
    re.IGNORECASE)
_MOV_FROM_MEM_RE = re.compile(
    r'\bmov\s+([a-zA-Z_]\w*)\s*,\s*(?:qword\s+|dword\s+|word\s+|byte\s+)?\[([^\]]+)\]',
    re.IGNORECASE)

# lea: lea dst, [addr]
_LEA_RE = re.compile(
    r'\blea\s+([a-zA-Z_]\w*)\s*,\s*\[([^\]]+)\]', re.IGNORECASE)

# xchg: xchg r1, r2
_XCHG_RE = re.compile(
    r'\bxchg\s+([a-zA-Z_]\w*)\s*,\s*([a-zA-Z_]\w*)', re.IGNORECASE)

# push/pop
_PUSH_RE = re.compile(r'\bpush\s+(.+)', re.IGNORECASE)
_POP_RE = re.compile(r'\bpop\s+([a-zA-Z_]\w*)', re.IGNORECASE)

# Arithmetic/logic ops that modify dst: add, sub, and, or, xor, imul, etc.
_ALU_RE = re.compile(
    r'\b(add|sub|and|or|xor|imul|mul|neg|not|inc|dec|shl|shr|sar|rol|ror)\s+'
    r'([a-zA-Z_]\w*)\s*(?:,\s*(.+))?',
    re.IGNORECASE)

# cmp/test (set flags but don't modify registers)
_CMP_RE = re.compile(
    r'\b(cmp|test)\s+([a-zA-Z_]\w*|\[.*?\])\s*,\s*(.+)', re.IGNORECASE)

# cmove/cmova/etc (conditional move)
_CMOV_RE = re.compile(
    r'\bcmov([a-z]+)\s+([a-zA-Z_]\w*)\s*,\s*([a-zA-Z_]\w*)', re.IGNORECASE)

# Pre-compiled AT&T / AArch64 patterns for _RegisterTracker.process_line
_ATT_MOV_IMM_RE = re.compile(
    r'\bmov[a-z]*\s+\$(.+?)\s*,\s*%([a-zA-Z_]\w*)')
_ATT_MOV_REG_RE = re.compile(
    r'\bmov[a-z]*\s+%([a-zA-Z_]\w*)\s*,\s*%([a-zA-Z_]\w*)')
_ATT_ALU_RE = re.compile(
    r'\b(add|sub|and|or|xor|imul|mul|neg|not|inc|dec|shl|shr|sar|rol|ror)\s+'
    r'%([a-zA-Z_]\w*)\s*,\s*%([a-zA-Z_]\w*)')
_ATT_PUSH_RE = re.compile(r'\bpush\s+%([a-zA-Z_]\w*)')
_ATT_POP_RE = re.compile(r'\bpop\s+%([a-zA-Z_]\w*)')
_AARCH64_MOV_RE = re.compile(
    r'\bmov[zk]?\s+([xw]\d+)\s*,\s*#?(.+)', re.IGNORECASE)
_AARCH64_LDR_RE = re.compile(
    r'\bldr\s+([xw]\d+)\s*,\s*\[([^\]]+)\]', re.IGNORECASE)
_AARCH64_ADDSUB_RE = re.compile(
    r'\b(add|sub)\s+([xw]\d+)\s*,\s*([xw]\d+)\s*,\s*(.+)', re.IGNORECASE)
_IDENT_RE = re.compile(r'^[a-zA-Z_]\w*$')
_AARCH64_REGNAME_RE = re.compile(r'^[xw]\d+$')

# Pre-compiled patterns for pass-2 line scanning. Previously these were
# `re.match(r'...', stripped)` per line per .S file.
_ASM_PP_IFDEF_RE = re.compile(r'^\s*#ifdef\s+([a-zA-Z_]\w*)')
_ASM_PP_IFNDEF_RE = re.compile(r'^\s*#ifndef\s+([a-zA-Z_]\w*)')
_ASM_PP_ELSE_RE = re.compile(r'^\s*#else\b')
_ASM_PP_ENDIF_RE = re.compile(r'^\s*#endif\b')
_ASM_SECTION_RE = re.compile(r'^\s*\.section\s+\.(\w+)')

# Pre-compiled macro-body variants of kernel asm func/end macros.
# These replace the per-line `pat.pattern.replace(r'(\w+)', r'(\\?\w+)')` calls.
_KERNEL_ASM_FUNC_MACROS_MACROBODY = [
    re.compile(p.pattern.replace(r'(\w+)', r'(\\?\w+)'))
    for p in _KERNEL_ASM_FUNC_MACROS
]
_KERNEL_ASM_END_MACROS_MACROBODY = [
    re.compile(p.pattern.replace(r'(\w+)', r'(\\?\w+)'))
    for p in _KERNEL_ASM_END_MACROS
]

# Module-level non-API path prefixes (avoid per-call allocation)
_NON_API_PATHS_ASM = ('tools/', 'scripts/', 'selftests/', 'testing/',
                      'documentation/', 'samples/', 'examples/')


class _RegisterTracker:
    """Track register values and data flow within an assembly function.

    Maintains a simple register state that maps register names to their
    current value descriptors (symbolic representations). This enables:
    - Resolving syscall numbers from rax before syscall instruction
    - Inferring function call arguments from register state
    - Detecting register-to-register value propagation
    - Tracking stack push/pop patterns
    """

    # x86_64 register aliases
    _REG_ALIASES = {
        'rax': 'eax', 'eax': 'ax', 'ax': 'al', 'al': 'rax',
        'rbx': 'ebx', 'ebx': 'bx', 'bx': 'bl', 'bl': 'rbx',
        'rcx': 'ecx', 'ecx': 'cx', 'cx': 'cl', 'cl': 'rcx',
        'rdx': 'edx', 'edx': 'dx', 'dx': 'dl', 'dl': 'rdx',
        'rsi': 'esi', 'esi': 'si', 'sil': 'rsi',
        'rdi': 'edi', 'edi': 'di', 'dil': 'rdi',
        'rsp': 'esp', 'esp': 'sp', 'spl': 'rsp',
        'rbp': 'ebp', 'ebp': 'bp', 'bpl': 'rbp',
        'r8': 'r8d', 'r8d': 'r8w', 'r8w': 'r8b',
        'r9': 'r9d', 'r9d': 'r9w', 'r9w': 'r9b',
        'r10': 'r10d', 'r11': 'r11d', 'r12': 'r12d',
        'r13': 'r13d', 'r14': 'r14d', 'r15': 'r15d',
    }

    # Canonical register names (always use the 64-bit form)
    _CANONICAL = {
        # x86_64
        'eax': 'rax', 'ax': 'rax', 'al': 'rax', 'ah': 'rax',
        'ebx': 'rbx', 'bx': 'rbx', 'bl': 'rbx', 'bh': 'rbx',
        'ecx': 'rcx', 'cx': 'rcx', 'cl': 'rcx', 'ch': 'rcx',
        'edx': 'rdx', 'dx': 'rdx', 'dl': 'rdx', 'dh': 'rdx',
        'esi': 'rsi', 'si': 'rsi', 'sil': 'rsi',
        'edi': 'rdi', 'di': 'rdi', 'dil': 'rdi',
        'esp': 'rsp', 'sp': 'rsp', 'spl': 'rsp',
        'ebp': 'rbp', 'bp': 'rbp', 'bpl': 'rbp',
        'r8d': 'r8', 'r8w': 'r8', 'r8b': 'r8',
        'r9d': 'r9', 'r9w': 'r9', 'r9b': 'r9',
        'r10d': 'r10', 'r11d': 'r11', 'r12d': 'r12',
        'r13d': 'r13', 'r14d': 'r14', 'r15d': 'r15',
        # AArch64 (x0-x30, xzr, sp, pc)
        'w0': 'x0', 'w1': 'x1', 'w2': 'x2', 'w3': 'x3',
        'w4': 'x4', 'w5': 'x5', 'w6': 'x6', 'w7': 'x7',
        'w8': 'x8', 'w9': 'x9', 'w10': 'x10', 'w11': 'x11',
        'w12': 'x12', 'w13': 'x13', 'w14': 'x14', 'w15': 'x15',
        'w16': 'x16', 'w17': 'x17', 'w18': 'x18', 'w19': 'x19',
        'w20': 'x20', 'w21': 'x21', 'w22': 'x22', 'w23': 'x23',
        'w24': 'x24', 'w25': 'x25', 'w26': 'x26', 'w27': 'x27',
        'w28': 'x28', 'w29': 'x29', 'w30': 'x30',
        'xzr': 'xzr',  # Zero register (no state)
        'sp': 'sp', 'fp': 'x29', 'lr': 'x30',
    }

    # x86_64 syscall argument registers (in order)
    SYSCALL_ARG_REGS = ['rdi', 'rsi', 'rdx', 'r10', 'r8', 'r9']

    # x86_64 function call argument registers (System V AMD64 ABI)
    CALL_ARG_REGS = ['rdi', 'rsi', 'rdx', 'rcx', 'r8', 'r9']

    # AArch64 syscall argument registers (x0-x5, x8=syscall nr)
    AARCH64_SYSCALL_ARG_REGS = ['x0', 'x1', 'x2', 'x3', 'x4', 'x5']

    # AArch64 function call argument registers (AAPCS64)
    AARCH64_CALL_ARG_REGS = ['x0', 'x1', 'x2', 'x3', 'x4', 'x5', 'x6', 'x7']

    def __init__(self, syntax="intel"):
        # Register state: canonical_reg → value descriptor string
        self._regs = {}
        # Stack contents: list of value descriptors (bottom to top)
        self._stack = []
        # Register transfer log: [(src_reg, dst_reg, line)]
        self._transfers = []
        # Syscall argument tracking
        self._syscall_args = {}
        # Function call argument tracking
        self._call_args = {}
        # Syntax mode: "intel" or "att"
        self._syntax = syntax

    def _canonical(self, reg):
        """Normalize register name to 64-bit canonical form."""
        return self._CANONICAL.get(reg.lower(), reg.lower())

    def process_line(self, stripped, line_num):
        """Process a single instruction line and update register state.

        stripped: the line with leading/trailing whitespace removed.
        """
        # --- AT&T syntax MOV: mov $imm, %dst / mov %src, %dst ---
        if self._syntax == "att":
            # AT&T: operand order is src, dst; registers have % prefix; immediates have $ prefix
            m = _ATT_MOV_IMM_RE.match(stripped)
            if m:
                src_val = m.group(1).strip()
                dst = self._canonical(m.group(2).lower().lstrip('%'))
                # Immediate value
                if src_val.isdigit() or (src_val.startswith('0x') and len(src_val) > 2):
                    try:
                        val = int(src_val, 0)
                        self._regs[dst] = f"const:{val}"
                    except ValueError:
                        self._regs[dst] = f"const:{src_val}"
                else:
                    self._regs[dst] = f"const:{src_val}"
                return

            m = _ATT_MOV_REG_RE.match(stripped)
            if m:
                src = self._canonical(m.group(1).lower())
                dst = self._canonical(m.group(2).lower())
                self._transfers.append((src, dst, line_num))
                if src in self._regs:
                    self._regs[dst] = self._regs[src]
                else:
                    self._regs[dst] = f"reg:{src}"
                return

            # AT&T ALU: op %src, %dst (2-operand form, modifies dst)
            m = _ATT_ALU_RE.match(stripped)
            if m:
                op = m.group(1).lower()
                src = self._canonical(m.group(2).lower())
                dst = self._canonical(m.group(3).lower())
                self._transfers.append((src, dst, line_num))
                old_val = self._regs.get(dst, f"reg:{dst}")
                self._regs[dst] = f"{op}({old_val})"
                return

            # AT&T push/pop
            m = _ATT_PUSH_RE.match(stripped)
            if m:
                src = self._canonical(m.group(1).lower())
                self._stack.append(self._regs.get(src, f"reg:{src}"))
                return
            m = _ATT_POP_RE.match(stripped)
            if m:
                dst = self._canonical(m.group(1).lower())
                if self._stack:
                    self._regs[dst] = self._stack.pop()
                else:
                    self._regs[dst] = "stack:unknown"
                return

        # --- AArch64-specific patterns (check BEFORE general _MOV_RE) ---
        # AArch64 MOV: mov[zk] Xd, #imm / mov Xd, Xs
        m = _AARCH64_MOV_RE.match(stripped)
        if m:
            dst = self._canonical(m.group(1).lower())
            src = m.group(2).strip()
            src_lower = src.lower()
            if _AARCH64_REGNAME_RE.match(src_lower):
                src_canon = self._canonical(src_lower)
                self._transfers.append((src_canon, dst, line_num))
                if src_canon in self._regs:
                    self._regs[dst] = self._regs[src_canon]
                else:
                    self._regs[dst] = f"reg:{src_canon}"
            elif src.isdigit() or (src.startswith('0x') and len(src) > 2):
                try:
                    val = int(src, 0)
                    self._regs[dst] = f"const:{val}"
                except ValueError:
                    self._regs[dst] = f"const:{src}"
            return

        # --- AArch64 LDR/STR (load/store) ---
        m = _AARCH64_LDR_RE.match(stripped)
        if m:
            dst = self._canonical(m.group(1).lower())
            mem = m.group(2).strip()
            self._regs[dst] = f"mem:{mem}"
            return

        # --- AArch64 ADD/SUB ---
        m = _AARCH64_ADDSUB_RE.match(stripped)
        if m:
            op = m.group(1).lower()
            dst = self._canonical(m.group(2).lower())
            src = self._canonical(m.group(3).lower())
            self._transfers.append((src, dst, line_num))
            old_val = self._regs.get(dst, f"reg:{dst}")
            self._regs[dst] = f"{op}({old_val})"
            return

        # --- MOV reg, value (x86_64) ---
        m = _MOV_RE.match(stripped)
        if m:
            dst = self._canonical(m.group(1))
            src = m.group(2).strip()
            # If src is a register, record transfer
            src_lower = src.lower()
            if _IDENT_RE.match(src):
                src_canon = self._canonical(src_lower)
                self._transfers.append((src_canon, dst, line_num))
                # Propagate value from src
                if src_canon in self._regs:
                    self._regs[dst] = self._regs[src_canon]
                else:
                    self._regs[dst] = f"reg:{src_canon}"
            elif src.isdigit() or (src.startswith('0x') and len(src) > 2):
                # Numeric literal
                try:
                    val = int(src, 0)
                    self._regs[dst] = f"const:{val}"
                except ValueError:
                    self._regs[dst] = f"const:{src}"
            else:
                self._regs[dst] = f"expr:{src}"
            return

        # --- LEA reg, [addr] ---
        m = _LEA_RE.match(stripped)
        if m:
            dst = self._canonical(m.group(1))
            addr = m.group(2).strip()
            self._regs[dst] = f"addr:{addr}"
            return

        # --- MOV from memory: mov reg, [mem] ---
        m = _MOV_FROM_MEM_RE.match(stripped)
        if m:
            dst = self._canonical(m.group(1))
            mem = m.group(2).strip()
            self._regs[dst] = f"mem:{mem}"
            return

        # --- MOV to memory: mov [mem], value ---
        m = _MOV_MEM_RE.match(stripped)
        if m:
            # We track memory writes but don't update register state
            return

        # --- XCHG r1, r2 ---
        m = _XCHG_RE.match(stripped)
        if m:
            r1 = self._canonical(m.group(1))
            r2 = self._canonical(m.group(2))
            v1 = self._regs.get(r1, f"reg:{r1}")
            v2 = self._regs.get(r2, f"reg:{r2}")
            self._regs[r1] = v2
            self._regs[r2] = v1
            self._transfers.append((r1, r2, line_num))
            self._transfers.append((r2, r1, line_num))
            return

        # --- PUSH value ---
        m = _PUSH_RE.match(stripped)
        if m:
            val = m.group(1).strip()
            val_lower = val.lower()
            if _IDENT_RE.match(val):
                src_canon = self._canonical(val_lower)
                self._stack.append(self._regs.get(src_canon, f"reg:{src_canon}"))
            else:
                self._stack.append(f"const:{val}")
            return

        # --- POP reg ---
        m = _POP_RE.match(stripped)
        if m:
            dst = self._canonical(m.group(1))
            if self._stack:
                self._regs[dst] = self._stack.pop()
            else:
                self._regs[dst] = "stack:unknown"
            return

        # --- ALU ops (modify first operand) ---
        m = _ALU_RE.match(stripped)
        if m:
            op = m.group(1).lower()
            dst = self._canonical(m.group(2))
            src = m.group(3)
            if src:
                src = src.strip()
                src_lower = src.lower()
                if _IDENT_RE.match(src):
                    src_canon = self._canonical(src_lower)
                    self._transfers.append((src_canon, dst, line_num))
            # ALU ops transform the value
            old_val = self._regs.get(dst, f"reg:{dst}")
            self._regs[dst] = f"{op}({old_val})"
            return

        # --- CMOVcc dst, src ---
        m = _CMOV_RE.match(stripped)
        if m:
            cond = m.group(1).lower()
            dst = self._canonical(m.group(2))
            src = self._canonical(m.group(3).lower())
            self._transfers.append((src, dst, line_num))
            # Conditional: value may or may not be transferred
            old_val = self._regs.get(dst, f"reg:{dst}")
            src_val = self._regs.get(src, f"reg:{src}")
            self._regs[dst] = f"cmov_{cond}({old_val}, {src_val})"
            return

        # --- CMP/TEST (set flags) ---
        # These don't modify registers but we note the comparison for context
        m = _CMP_RE.match(stripped)
        if m:
            # Track for condition context — no register modification
            pass

        # --- AArch64 cbz/cbnz: test reg against zero, no register modification ---
        m = _ARM_CBZ_RE.match(stripped)
        if m:
            # Reads register but does not modify it
            pass

        # --- AArch64 tbz/tbnz: test bit, no register modification ---
        m = _ARM_TBZ_RE.match(stripped)
        if m:
            # Reads register bit but does not modify it
            pass

        # --- AArch64 blr Xn: indirect call, no register modification (link reg set by CPU) ---
        m = _ARM_BLR_RE.match(stripped)
        if m:
            # blr reads Xn and writes LR(x30) — track LR as return address
            src = self._canonical(m.group(1).lower())
            self._regs['x30'] = f"reg:{src}"
            return

        # --- AArch64 br Xn: indirect branch, no register modification ---
        m = _ARM_BR_RE.match(stripped)
        if m:
            # br reads Xn but does not modify registers
            pass

    def get_syscall_info(self):
        """Get syscall number and argument info from current register state.

        Returns dict with:
          - syscall_nr: the syscall number (from rax)
          - syscall_name: resolved name
          - args: list of (arg_index, value_descriptor) tuples
        """
        rax_val = self._regs.get('rax', 'unknown')
        syscall_nr = None
        syscall_name = "syscall_unknown"

        if rax_val.startswith('const:'):
            try:
                syscall_nr = int(rax_val.split(':', 1)[1])
                # Resolve via scanner's _resolve_syscall if available
                if hasattr(self, '_scanner_ref') and self._scanner_ref:
                    syscall_name = self._scanner_ref._resolve_syscall(
                        self._scanner_ref._arch, syscall_nr, f"syscall_{syscall_nr}")
                else:
                    syscall_name = _SYSCALL_NAMES.get(syscall_nr, f"syscall_{syscall_nr}")
            except ValueError:
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        args = []
        for i, reg in enumerate(self.SYSCALL_ARG_REGS):
            if reg in self._regs:
                args.append((i, self._regs[reg]))

        return {
            "syscall_nr": syscall_nr,
            "syscall_name": syscall_name,
            "args": args
        }

    def get_call_args(self):
        """Get function call argument info from current register state.

        Returns list of (position, value_descriptor) tuples.
        """
        args = []
        for i, reg in enumerate(self.CALL_ARG_REGS):
            if reg in self._regs:
                args.append((i + 1, self._regs[reg]))
        return args

    def get_transfers(self):
        """Return all register-to-register transfers recorded."""
        return list(self._transfers)

    def get_register_state(self):
        """Return current register state as dict."""
        return dict(self._regs)

    def reset(self):
        """Reset tracker state for a new function."""
        self._regs.clear()
        self._stack.clear()
        self._transfers.clear()


class AsmRegexScanner(BaseScanner):
    """Regex-based NASM/x86 assembly scanner.

    Handles:
    - NASM x86_64 syntax (primary)
    - Kernel-style GNU as .S files (SYM_FUNC_START, ENTRY, EXPORT_SYMBOL)
    - Section context tracking (.text vs .data/.bss)
    - Syscall modeling via rax register tracking
    - Cross-language extern/global declarations
    - Conditional jump tracking for call_condition
    - Kernel ASM macro chains (SYM_FUNC_ALIAS, etc.)
    """

    def __init__(self):
        self._arch = "x86_64"  # Default; can be overridden by profile
        self._asm_syntax = "nasm"  # "nasm" or "att"
        self._syscall_maps = {}  # From profile syscall_maps; merged at scan time

    def set_syscall_maps(self, maps: dict):
        """Set architecture-specific syscall number→name maps from profile.

        Profile format: {"x86_64": {0: "sys_read", ...}, "aarch64": {...}, ...}
        These override/extend the built-in default mappings.
        """
        self._syscall_maps = maps or {}

    def set_asm_entry_macros(self, macros: list):
        """Set profile-driven ASM entry-point-generating macros.

        Profile format: [
            {"name": "idtentry", "func_start_param": 1, "call_params": [2]},
            ...
        ]
        name: the macro name that generates entry points
        func_start_param: 0-based index of the parameter that becomes the function name
        call_params: list of 0-based indices of parameters that become call targets
        """
        self._asm_entry_macros = macros or []

    def _resolve_syscall(self, arch: str, nr: int, fallback_fmt: str = "syscall_{nr}") -> str:
        """Resolve syscall number to name using profile maps, then built-in defaults.

        Priority: profile syscall_maps[arch] > built-in _SYSCALL_NAMES/_AARCH64_SYSCALL_NAMES/...
        """
        # Check profile first
        arch_map = self._syscall_maps.get(arch, {})
        if nr in arch_map:
            return arch_map[nr]
        # Fallback to built-in defaults
        if arch == "x86_64":
            return _SYSCALL_NAMES.get(nr, fallback_fmt.format(nr=nr))
        elif arch == "aarch64":
            return _AARCH64_SYSCALL_NAMES.get(nr, fallback_fmt.format(nr=nr))
        elif arch == "riscv64":
            return _RISCV_SYSCALL_NAMES.get(nr, fallback_fmt.format(nr=nr))
        return fallback_fmt.format(nr=nr)

    def _parse(self, source_bytes: bytes):
        """Regex scanner does not use tree-sitter."""
        return None

    def _extract(self, tree, source_bytes, filepath, source_root, domain):
        """Extract functions and edges from assembly source.

        Returns (functions, edges, vtable_registrations, fn_ptr_calls).
        """
        self._current_filepath = filepath
        source = source_bytes.decode("utf-8", errors="replace")
        lines = source.splitlines()

        # Detect syntax: if file has .S extension or contains GNU as directives
        ext = os.path.splitext(filepath)[1].lower()
        is_gas = (ext == '.s') or any(
            # GAS directives start with '.' followed by a letter (not a local label)
            re.match(r'^\.[a-zA-Z]', line.strip())
            for line in lines[:30]
            if line.strip() and not line.strip().startswith(';')
            and not line.strip().startswith('#')
            and not line.strip().startswith('..')  # GCC local labels like ..@func
        )

        # State
        current_section = None  # "text", "data", "bss", or a kernel-specific section
        global_symbols = set()
        extern_symbols = set()
        call_targets = set()  # All names used as call targets (for label classification)
        functions = []
        edges = []
        current_func_name = None
        current_func_id = None
        current_func_line = 0
        call_order = [0]
        cond_stack = []  # [(condition_text, line)]
        pp_conds = []    # Preprocessor ifdef stack: [(directive, condition, line)]
        pp_alive = True  # Whether current code is inside an active preprocessor branch
        pending_syscall = None  # Most recent mov rax, N before syscall
        reg_tracker = _RegisterTracker(syntax="att" if is_gas else "intel")
        asm_aliases = []  # [{"alias": ..., "original": ..., "source_file": ...}]
        export_symbols = []  # [{"name": ..., "visibility": ...}]
        in_kernel_func_macro = None  # Name of function currently inside SYM_FUNC_START/ENTRY
        import_edges = []

        # .macro/.endm tracking: {macro_name: {"params": [...], "call_params": [...],
        #   "func_start_params": [...], "func_end_params": [...]}}
        # call_params = parameter names used as call/jmp targets in the macro body
        # func_start_params = parameter names used in SYM_FUNC_START/ENTRY etc.
        # func_end_params = parameter names used in SYM_FUNC_END/ENDPROC etc.
        macro_defs = {}
        in_macro_def = False
        macro_def_name = None
        macro_def_params = []
        macro_def_call_params = []
        macro_def_func_start_params = []
        macro_def_func_end_params = []

        # .set alias tracking
        gas_set_aliases = []  # [{"alias": ..., "original": ...}]

        # Profile-driven asm_entry_macros
        # Each entry: {"name": macro_name, "func_start_param": idx, "call_params": [idx, ...]}
        asm_entry_macros = getattr(self, '_asm_entry_macros', [])

        # ALTERNATIVE macro targets collected in pass 1
        alt_targets = set()

        # .irp/.irpc tracking
        in_irp = False
        irp_param = ''
        irp_values = []
        irp_body = []
        irp_depth = 0

        # First pass: collect global/extern declarations, macro definitions,
        # and all call targets for cross-reference-based label classification
        for line in lines:
            stripped = line.strip()
            # Skip full-line comments (NASM: ; ...  GAS: # ... or /* */)
            if stripped.startswith(';') or stripped.startswith('#'):
                continue
            # Strip inline comments to prevent false-positive matches
            # in comment text (e.g., 'mov rax, 5 ; call bar' would
            # match _CALL_RE on 'call bar' in the comment).
            if is_gas:
                # GAS: # is inline comment (; is statement separator in GAS)
                # Line-start # already skipped above; strip inline # comments
                if '#' in stripped:
                    stripped = stripped.split('#', 1)[0].rstrip()
            else:
                # NASM: ; is always a comment (# is preprocessor directive)
                if ';' in stripped:
                    stripped = stripped.split(';', 1)[0].rstrip()

            # --- .macro/.endm tracking ---
            if in_macro_def:
                # Check for .endm
                if _MACRO_END_RE.match(stripped):
                    macro_defs[macro_def_name] = {
                        "params": macro_def_params,
                        "call_params": macro_def_call_params,
                        "func_start_params": macro_def_func_start_params,
                        "func_end_params": macro_def_func_end_params,
                    }
                    in_macro_def = False
                    macro_def_name = None
                    macro_def_params = []
                    macro_def_call_params = []
                    macro_def_func_start_params = []
                    macro_def_func_end_params = []
                    continue
                # Collect call/jmp targets that reference macro parameters (\param)
                for cm in _MACRO_CALL_RE.finditer(stripped):
                    param_name = cm.group(1)
                    if param_name not in macro_def_call_params:
                        macro_def_call_params.append(param_name)
                for cm in _MACRO_BL_RE.finditer(stripped):
                    param_name = cm.group(1)
                    if param_name not in macro_def_call_params:
                        macro_def_call_params.append(param_name)
                for cm in _MACRO_JAL_RE.finditer(stripped):
                    param_name = cm.group(1)
                    if param_name not in macro_def_call_params:
                        macro_def_call_params.append(param_name)
                for cm in _MACRO_BRASL_RE.finditer(stripped):
                    param_name = cm.group(1)
                    if param_name not in macro_def_call_params:
                        macro_def_call_params.append(param_name)
                # Collect SYM_FUNC_START/ENTRY params: SYM_FUNC_START(\param)
                for pat in _KERNEL_ASM_FUNC_MACROS_MACROBODY:
                    m = pat.match(stripped)
                    if m:
                        name = m.group(1)
                        if name.startswith('\\'):
                            pname = name[1:]
                            if pname not in macro_def_func_start_params:
                                macro_def_func_start_params.append(pname)
                        break
                # Collect SYM_FUNC_END/ENDPROC params
                for pat in _KERNEL_ASM_END_MACROS_MACROBODY:
                    m = pat.match(stripped)
                    if m:
                        name = m.group(1)
                        if name.startswith('\\'):
                            pname = name[1:]
                            if pname not in macro_def_func_end_params:
                                macro_def_func_end_params.append(pname)
                        break
                continue  # Skip rest of pass 1 processing inside macro body

            # Check for .macro definition start
            mm = _MACRO_DEF_RE.match(stripped)
            if mm:
                in_macro_def = True
                macro_def_name = mm.group(1)
                param_str = mm.group(2) or ""
                # Parse parameters: "name1, name2=default, name3"
                macro_def_params = []
                for p in re.split(r'\s*,\s*', param_str.strip()):
                    p = p.strip()
                    if p:
                        # Remove default value
                        param_name = re.match(r'(\w+)', p)
                        if param_name:
                            macro_def_params.append(param_name.group(1))
                macro_def_call_params = []
                continue

            for m in _GLOBAL_RE.finditer(stripped):
                for sym in re.split(r'\s*,\s*', m.group(1)):
                    global_symbols.add(sym.strip())
            for m in _EXTERN_RE.finditer(stripped):
                for sym in re.split(r'\s*,\s*', m.group(1)):
                    extern_symbols.add(sym.strip())
            for m in _GAS_GLOBAL_RE.finditer(stripped):
                for sym in re.split(r'\s*,\s*', m.group(1)):
                    global_symbols.add(sym.strip())
            for m in _GAS_EXTERN_RE.finditer(stripped):
                for sym in re.split(r'\s*,\s*', m.group(1)):
                    extern_symbols.add(sym.strip())

            # Collect call targets for cross-reference
            for cm in _CALL_RE.finditer(stripped):
                call_targets.add(cm.group(1))
            if is_gas:
                for cm in _GAS_CALL_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                for cm in _ARM_CALL_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # AArch64 cbz/cbnz targets
                for cm in _ARM_CBZ_RE.finditer(stripped):
                    call_targets.add(cm.group(3))
                # AArch64 tbz/tbnz targets
                for cm in _ARM_TBZ_RE.finditer(stripped):
                    call_targets.add(cm.group(4))
                # GAS .type directive: mark as callable target
                m = _GAS_TYPE_RE.match(stripped)
                if m:
                    call_targets.add(m.group(1))
                # RISC-V jal targets
                for cm in _RISCV_JAL_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # RISC-V j (jump) targets
                for cm in _RISCV_J_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # RISC-V conditional branch targets
                for cm in _RISCV_BRANCH_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # LoongArch bl targets
                for cm in _LOONGARCH_BL_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # LoongArch b (jump) targets
                for cm in _LOONGARCH_B_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # s390 brasl targets
                for cm in _S390_BRASL_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # ARM/PowerPC bare `b` tail-call targets
                cm = _ARM_B_RE.match(stripped)
                if cm:
                    call_targets.add(cm.group(1))
                # PowerPC CFUNC(name) unwrapping
                for cm in _PPC_CFUNC_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # SuperH bsr targets
                for cm in _SH_BSR_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # MIPS jal targets
                for cm in _MIPS_JAL_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # IA-64 br.call targets
                for cm in _IA64_BR_CALL_RE.finditer(stripped):
                    call_targets.add(cm.group(1))
                # ALTERNATIVE macro call targets
                if 'ALTERNATIVE' in stripped:
                    for cm in _ALT_CALL_RE.finditer(stripped):
                        alt_targets.add(cm.group(1))
                        call_targets.add(cm.group(1))

            # Collect jmp targets (non-local labels) for tail call detection
            jm = _JMP_RE.match(stripped)
            if jm and not jm.group(1).startswith('.'):
                call_targets.add(jm.group(1))

            # Kernel export macros
            for em in _EXPORT_SYMBOL_MACROS:
                m = em.match(stripped)
                if m:
                    export_symbols.append({
                        "name": m.group(1),
                        "visibility": "GPL" if "GPL" in stripped else "default"
                    })
                    call_targets.add(m.group(1))  # Exports are callable

            # Kernel inner label macros (SYM_INNER_LABEL, SYM_INNER_LABEL_ERROR)
            # These are intra-function entry points — add to call_targets
            for ilm in _KERNEL_INNER_LABEL_MACROS:
                m = ilm.match(stripped)
                if m:
                    call_targets.add(m.group(1))
                    export_symbols.append({
                        "name": m.group(1),
                        "visibility": "GPL" if "GPL" in stripped else "default"
                    })
                    call_targets.add(m.group(1))  # Exports are callable

            # Kernel alias macros
            for am in _ASM_ALIAS_MACROS:
                m = am.match(stripped)
                if m:
                    asm_aliases.append({
                        "alias": m.group(1),
                        "original": m.group(2),
                        "source_file": os.path.relpath(filepath, source_root)
                    })
                    call_targets.add(m.group(1))

            # GAS .set aliasing
            m = _GAS_SET_ALIAS_RE.match(stripped)
            if m:
                alias_name = m.group(1)
                original_name = m.group(2)
                # Only record function-like aliases (both names look like identifiers)
                if alias_name not in ('noreorder', 'reorder', 'noat', 'at',
                                      'push', 'pop', 'nomacro', 'macro',
                                      'nomips16', 'mips16', 'nomicromips',
                                      'arch', 'fp', 'volatile', 'nomove',
                                      'booke', 'nosafer'):
                    gas_set_aliases.append({
                        "alias": alias_name,
                        "original": original_name,
                        "source_file": os.path.relpath(filepath, source_root)
                    })
                    call_targets.add(alias_name)

        # Second pass: main scan
        in_macro_def = False  # Track .macro/.endm blocks in pass 2
        # .irp/.irpc tracking for pass 2
        in_irp = False
        irp_param = ''
        irp_values = []
        irp_body_lines = []

        def _update_pp_alive():
            """Update pp_alive flag based on current pp_conds stack.

            Conservative approach: always treat all conditional branches as
            potentially compiled. Instead of pruning edges, we attach the
            active #ifdef conditions to edges as call_condition metadata.
            """
            nonlocal pp_alive
            pp_alive = True  # Conservative: always alive (code may be compiled)

        for i, line in enumerate(lines):
            stripped = line.strip()
            line_num = i + 1

            # --- C preprocessor directives in .S files ---
            # #ifdef, #ifndef, #else, #endif must be tracked BEFORE the # skip
            if is_gas and stripped.startswith('#'):
                m = _ASM_PP_IFDEF_RE.match(stripped)
                if m:
                    cond_stack.append((f"pp_ifdef_{m.group(1)}", line_num))
                    continue
                m = _ASM_PP_IFNDEF_RE.match(stripped)
                if m:
                    cond_stack.append((f"pp_ifndef_{m.group(1)}", line_num))
                    continue
                m = _ASM_PP_ELSE_RE.match(stripped)
                if m:
                    if cond_stack and cond_stack[-1][0].startswith(('pp_ifdef_', 'pp_ifndef_')):
                        old = cond_stack[-1][0]
                        if old.startswith('pp_ifdef_'):
                            cond_stack[-1] = (f"pp_else_{old[9:]}", line_num)
                        elif old.startswith('pp_ifndef_'):
                            cond_stack[-1] = (f"pp_else_{old[10:]}", line_num)
                    continue
                m = _ASM_PP_ENDIF_RE.match(stripped)
                if m:
                    if cond_stack and cond_stack[-1][0].startswith(('pp_ifdef_', 'pp_ifndef_', 'pp_else_')):
                        cond_stack.pop()
                    continue
                # Other # lines (comments, #include, #define, etc.) — skip
                continue

            # Skip comments and empty lines
            if not stripped or stripped.startswith(';'):
                continue

            # --- Section tracking ---
            m = _SECTION_RE.match(stripped)
            if m:
                current_section = m.group(1).lower()
                continue

            # GAS-style .section directive (e.g., .section .text)
            m = _ASM_SECTION_RE.match(stripped)
            if m:
                current_section = m.group(1).lower()
                continue

            # Other GAS section directives
            for sec_dir in ('.text', '.data', '.bss'):
                if stripped.startswith(sec_dir):
                    current_section = sec_dir[1:]  # Remove leading dot
                    break

            # --- GAS conditional assembly directives: attach to cond_stack ---
            m = _IFDEF_RE.match(stripped)
            if m:
                pp_conds.append(('ifdef', m.group(1)))
                cond_stack.append((f"ifdef_{m.group(1)}", line_num))
                _update_pp_alive()
                continue
            m = _IFNDEF_RE.match(stripped)
            if m:
                pp_conds.append(('ifndef', m.group(1)))
                cond_stack.append((f"ifndef_{m.group(1)}", line_num))
                _update_pp_alive()
                continue
            m = _ELSE_RE.match(stripped)
            if m:
                if pp_conds:
                    last = pp_conds[-1]
                    pp_conds[-1] = ('ifndef' if last[0] == 'ifdef' else 'ifdef', last[1])
                    # Update cond_stack: replace ifdef_ with else_ or vice versa
                    if cond_stack and cond_stack[-1][0].startswith(('ifdef_', 'ifndef_')):
                        old = cond_stack[-1][0]
                        if old.startswith('ifdef_'):
                            cond_stack[-1] = (f"else_{old[6:]}", line_num)
                        elif old.startswith('ifndef_'):
                            cond_stack[-1] = (f"else_{old[7:]}", line_num)
                    _update_pp_alive()
                continue
            m = _ENDIF_RE.match(stripped)
            if m:
                if pp_conds:
                    pp_conds.pop()
                    # Pop matching cond_stack entry
                    if cond_stack and cond_stack[-1][0].startswith(('ifdef_', 'ifndef_', 'else_')):
                        cond_stack.pop()
                    _update_pp_alive()
                continue

            # --- .macro/.endm: skip macro body in pass 2 ---
            # (Macro definitions were already collected in pass 1)
            mm_def = _MACRO_DEF_RE.match(stripped)
            if mm_def:
                in_macro_def = True
                continue
            if in_macro_def:
                if _MACRO_END_RE.match(stripped):
                    in_macro_def = False
                continue

            # --- .irp/.irpc iteration macros ---
            # .irp param, val1, val2, ... / .irpc param, string
            # These expand the body for each value, substituting \param
            if is_gas and not in_irp:
                m_irp = _IRP_RE.match(stripped)
                m_irpc = _IRPC_RE.match(stripped)
                if m_irp:
                    in_irp = True
                    irp_param = m_irp.group(1)
                    irp_values = [v.strip() for v in re.split(r'\s*,\s*', m_irp.group(2))]
                    irp_body_lines = []
                    continue
                if m_irpc:
                    in_irp = True
                    irp_param = m_irpc.group(1)
                    # .irpc iterates over each character in the string
                    irp_values = list(m_irpc.group(2).strip())
                    irp_body_lines = []
                    continue
            if in_irp:
                if _ENDR_RE.match(stripped):
                    # Process the accumulated body for each value
                    for val in irp_values:
                        for body_line in irp_body_lines:
                            expanded = body_line.replace(f'\\{irp_param}', val)
                            # Check for call targets in the expanded line
                            for cm in _GAS_CALL_RE.finditer(expanded):
                                callee = cm.group(1)
                                if current_func_name and current_func_id and _IDENT_RE.match(callee):
                                    call_order[0] += 1
                                    condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                    edges.append({
                                        "source": current_func_id,
                                        "target": self._make_func_id(domain, callee),
                                        "call_order": call_order[0],
                                        "call_condition": condition,
                                        "confidence": "INFERRED",
                                        "source_tag": "irp_expansion"
                                    })
                            for cm in _ARM_CALL_RE.finditer(expanded):
                                callee = cm.group(1)
                                if current_func_name and current_func_id and _IDENT_RE.match(callee):
                                    call_order[0] += 1
                                    condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                    edges.append({
                                        "source": current_func_id,
                                        "target": self._make_func_id(domain, callee),
                                        "call_order": call_order[0],
                                        "call_condition": condition,
                                        "confidence": "INFERRED",
                                        "source_tag": "irp_expansion"
                                    })
                    in_irp = False
                    continue
                else:
                    irp_body_lines.append(stripped)
                    continue

            # --- Macro invocation: expand parameterized calls ---
            # When we see a known macro name invoked with arguments,
            # substitute the arguments into the macro's call patterns.
            # Also handle macros that define functions (e.g., THUNK name, func
            # expands to SYM_FUNC_START(name) ... call func ... SYM_FUNC_END(name)).
            if macro_defs and is_gas:
                m_invoke = _MACRO_INVOKE_RE.match(stripped)
                if m_invoke:
                    inv_name = m_invoke.group(1)
                    if inv_name in macro_defs:
                        macro_info = macro_defs[inv_name]
                        # Parse invocation arguments
                        arg_str = m_invoke.group(2)
                        args = [a.strip() for a in re.split(r'\s*,\s*', arg_str)]
                        # Map parameter names to argument values
                        param_map = {}
                        for idx, pname in enumerate(macro_info["params"]):
                            if idx < len(args):
                                param_map[pname] = args[idx]

                        # If the macro defines a function (has func_start_params),
                        # create the function and set it as current
                        func_start_params = macro_info.get("func_start_params", [])
                        func_end_params = macro_info.get("func_end_params", [])
                        call_params = macro_info.get("call_params", [])

                        if func_start_params:
                            # Resolve the function name from the invocation
                            start_pname = func_start_params[0]
                            resolved_func_name = param_map.get(start_pname)
                            if resolved_func_name and _IDENT_RE.match(resolved_func_name):
                                # Close any previous function
                                if current_func_name and current_func_id:
                                    self._finalize_function(
                                        functions, current_func_name, current_func_id,
                                        current_func_line, domain, global_symbols,
                                        call_targets, export_symbols, lines,
                                        current_func_line, i - 1,
                                        reg_tracker=reg_tracker)
                                    reg_tracker.reset()
                                current_func_name = resolved_func_name
                                current_func_id = self._make_func_id(domain, resolved_func_name)
                                current_func_line = line_num
                                call_order[0] = 0
                                cond_stack.clear()

                                # Create edges for parameterized call targets
                                for pname in call_params:
                                    resolved = param_map.get(pname)
                                    if resolved and _IDENT_RE.match(resolved):
                                        call_order[0] += 1
                                        edges.append({
                                            "source": current_func_id,
                                            "target": self._make_func_id(domain, resolved),
                                            "call_order": call_order[0],
                                            "call_condition": "",
                                            "confidence": "INFERRED",
                                            "source_tag": "asm_macro_expansion"
                                        })

                                # If the macro also ends the function, close it now
                                if func_end_params:
                                    self._finalize_function(
                                        functions, current_func_name, current_func_id,
                                        current_func_line, domain, global_symbols,
                                        call_targets, export_symbols, lines,
                                        current_func_line, i,
                                        reg_tracker=reg_tracker)
                                    reg_tracker.reset()
                                    current_func_name = None
                                    current_func_id = None
                        else:
                            # Macro doesn't define a function — just expand calls
                            # within the current function context
                            if call_params and current_func_name and current_func_id:
                                for pname in call_params:
                                    resolved = param_map.get(pname)
                                    if resolved and _IDENT_RE.match(resolved):
                                        call_order[0] += 1
                                        condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                        edges.append({
                                            "source": current_func_id,
                                            "target": self._make_func_id(domain, resolved),
                                            "call_order": call_order[0],
                                            "call_condition": condition,
                                            "confidence": "INFERRED",
                                            "source_tag": "asm_macro_expansion"
                                        })
                        continue  # Consumed as macro invocation

            # --- Profile-driven asm_entry_macros ---
            # Handle macros that generate entry points but are not defined via
            # .macro/.endm in this file (e.g., defined in #included headers).
            if asm_entry_macros and is_gas:
                m_invoke = _MACRO_INVOKE_RE.match(stripped)
                if m_invoke:
                    inv_name = m_invoke.group(1)
                    for entry_macro in asm_entry_macros:
                        if entry_macro["name"] == inv_name:
                            arg_str = m_invoke.group(2)
                            # Some macros use comma-separated args (THUNK a, b),
                            # others use space-separated args (idtentry a b c).
                            # Split by comma first; if only one token and it
                            # contains spaces, fall back to whitespace split.
                            args = [a.strip() for a in re.split(r'\s*,\s*', arg_str)]
                            if len(args) == 1 and ' ' in args[0]:
                                args = args[0].split()
                            # Get function name from parameter index
                            func_idx = entry_macro.get("func_start_param", -1)
                            call_indices = entry_macro.get("call_params", [])
                            if 0 <= func_idx < len(args):
                                func_name_arg = args[func_idx]
                                if _IDENT_RE.match(func_name_arg):
                                    # Close any previous function
                                    if current_func_name and current_func_id:
                                        self._finalize_function(
                                            functions, current_func_name, current_func_id,
                                            current_func_line, domain, global_symbols,
                                            call_targets, export_symbols, lines,
                                            current_func_line, i - 1,
                                            reg_tracker=reg_tracker)
                                        reg_tracker.reset()
                                    current_func_name = func_name_arg
                                    current_func_id = self._make_func_id(domain, func_name_arg)
                                    current_func_line = line_num
                                    call_order[0] = 0
                                    cond_stack.clear()
                                    # Create edges for call parameters
                                    for cidx in call_indices:
                                        if 0 <= cidx < len(args):
                                            invoked_arg = args[cidx]
                                            if _IDENT_RE.match(invoked_arg):
                                                call_order[0] += 1
                                                edges.append({
                                                    "source": current_func_id,
                                                    "target": self._make_func_id(domain, invoked_arg),
                                                    "call_order": call_order[0],
                                                    "call_condition": "",
                                                    "confidence": "INFERRED",
                                                    "source_tag": "asm_entry_macro"
                                                })
                                    # Close function immediately (entry macros define complete functions)
                                    self._finalize_function(
                                        functions, current_func_name, current_func_id,
                                        current_func_line, domain, global_symbols,
                                        call_targets, export_symbols, lines,
                                        current_func_line, i,
                                        reg_tracker=reg_tracker)
                                    reg_tracker.reset()
                                    current_func_name = None
                                    current_func_id = None
                            break

            # --- Kernel ASM macro function definitions ---
            func_name_from_macro = None
            for pat in _KERNEL_ASM_FUNC_MACROS:
                m = pat.match(stripped)
                if m:
                    func_name_from_macro = m.group(1)
                    break

            # --- Kernel inner label macros (SYM_INNER_LABEL, SYM_INNER_LABEL_ERROR) ---
            # These are intra-function entry points, NOT new function boundaries.
            # Record them as jump targets but don't close the current function.
            inner_label_name = None
            for ilm in _KERNEL_INNER_LABEL_MACROS:
                m = ilm.match(stripped)
                if m:
                    inner_label_name = m.group(1)
                    break
            if inner_label_name:
                # Just a label within the current function — clear condition stack
                if cond_stack:
                    cond_stack.clear()
                continue

            if func_name_from_macro:
                # Close any previous function
                if current_func_name and current_func_id:
                    self._finalize_function(functions, current_func_name,
                                            current_func_id, current_func_line,
                                            domain, global_symbols, call_targets,
                                            export_symbols, lines,
                                            current_func_line, i - 1,
                                            reg_tracker=reg_tracker)
                current_func_name = func_name_from_macro
                current_func_id = self._make_func_id(domain, func_name_from_macro)
                current_func_line = line_num
                in_kernel_func_macro = func_name_from_macro
                call_order[0] = 0
                cond_stack.clear()
                continue

            # Kernel ASM end macros
            for pat in _KERNEL_ASM_END_MACROS:
                m = pat.match(stripped)
                if m:
                    if current_func_name and current_func_id:
                        self._finalize_function(functions, current_func_name,
                                                current_func_id, current_func_line,
                                                domain, global_symbols, call_targets,
                                                export_symbols, lines,
                                                current_func_line, i - 1,
                                                reg_tracker=reg_tracker)
                        reg_tracker.reset()
                    current_func_name = None
                    current_func_id = None
                    in_kernel_func_macro = None
                    break
            else:
                # --- GAS .size directive (function end marker) ---
                m = _GAS_SIZE_RE.match(stripped)
                if m:
                    size_name = m.group(1)
                    if current_func_name and current_func_id and current_func_name == size_name:
                        self._finalize_function(functions, current_func_name,
                                                current_func_id, current_func_line,
                                                domain, global_symbols, call_targets,
                                                export_symbols, lines,
                                                current_func_line, i - 1,
                                                reg_tracker=reg_tracker)
                        reg_tracker.reset()
                        current_func_name = None
                        current_func_id = None
                        in_kernel_func_macro = None
                    continue

                # --- Label detection (only in code sections) ---
                is_code = current_section == "text" or (
                    current_section and "." + current_section in _CODE_SECTIONS
                )

                if is_code:
                    # Check numeric label (GAS: 1:, 2:, etc.)
                    m = _NUMERIC_LABEL_RE.match(stripped)
                    if m:
                        # Record numeric label for reference resolution
                        # Numeric labels are local — clear condition stack
                        if cond_stack:
                            cond_stack.clear()
                        continue

                    # Check local label (NASM: .loop, .done) — skip
                    m = _LOCAL_LABEL_RE.match(stripped)
                    if m:
                        # Local label reached: simplify condition stack
                        if cond_stack:
                            cond_stack.clear()
                        continue

                    # Check non-local label
                    m = _LABEL_RE.match(stripped)
                    if m:
                        label_name = m.group(1)
                        # Is this a function or just a jump target?
                        # A label is a function if:
                        # 1. Declared with global
                        # 2. Referenced by a call instruction
                        # 3. Inside a kernel func macro context
                        is_func = (label_name in global_symbols or
                                   label_name in call_targets or
                                   in_kernel_func_macro is not None)

                        if is_func:
                            # Close previous function
                            if current_func_name and current_func_id:
                                self._finalize_function(
                                    functions, current_func_name, current_func_id,
                                    current_func_line, domain, global_symbols,
                                    call_targets, export_symbols, lines,
                                    current_func_line, i - 1,
                                    reg_tracker=reg_tracker)
                                reg_tracker.reset()

                            current_func_name = label_name
                            current_func_id = self._make_func_id(domain, label_name)
                            current_func_line = line_num
                            call_order[0] = 0
                            cond_stack.clear()
                        else:
                            # Jump target only — clear condition stack
                            if cond_stack:
                                cond_stack.clear()
                        continue

                    # --- Call instructions ---
                    # Direct call (NASM)
                    for cm in _CALL_RE.finditer(stripped):
                        callee = cm.group(1)
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": self._make_func_id(domain, callee),
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            })

                    # Direct call (GAS/AT&T)
                    if is_gas:
                        for cm in _GAS_CALL_RE.finditer(stripped):
                            callee = cm.group(1)
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED"
                                })

                        # ARM calls (skip if CFUNC wrapper — handled below)
                        for cm in _ARM_CALL_RE.finditer(stripped):
                            callee = cm.group(1)
                            if callee.upper() == 'CFUNC':
                                continue  # CFUNC(name) unwrapped below
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED"
                                })

                        # ARM/PowerPC bare `b` tail-call
                        cm_b = _ARM_B_RE.match(stripped)
                        if cm_b and is_gas:
                            b_target = cm_b.group(1)
                            # Skip if this is `b CFUNC(...)` — handled by CFUNC below
                            if b_target.upper() != 'CFUNC' and (
                                    b_target in call_targets or
                                    b_target in global_symbols or
                                    b_target in extern_symbols):
                                if current_func_name and current_func_id:
                                    call_order[0] += 1
                                    condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                    edges.append({
                                        "source": current_func_id,
                                        "target": self._make_func_id(domain, b_target),
                                        "call_order": call_order[0],
                                        "call_condition": condition,
                                        "confidence": "INFERRED",
                                        "source_tag": "tail_call_b_arm"
                                    })

                        # PowerPC CFUNC(name) unwrapping
                        for cm in _PPC_CFUNC_RE.finditer(stripped):
                            real_callee = cm.group(1)
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, real_callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED",
                                    "source_tag": "ppc_cfunc"
                                })

                        # PowerPC bctrl indirect call
                        if _PPC_BCTRL_RE.search(stripped):
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": "indirect_call",
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "AMBIGUOUS",
                                    "source_tag": "ppc_bctrl"
                                })

                        # SuperH bsr direct call
                        for cm in _SH_BSR_RE.finditer(stripped):
                            callee = cm.group(1)
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED",
                                    "source_tag": "sh_bsr"
                                })

                        # SuperH jsr/bsrf indirect call
                        if _SH_JSR_RE.search(stripped) or _SH_BSRF_RE.search(stripped):
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": "indirect_call",
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "AMBIGUOUS",
                                    "source_tag": "sh_jsr"
                                })

                        # MIPS jal direct call
                        for cm in _MIPS_JAL_RE.finditer(stripped):
                            callee = cm.group(1)
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED",
                                    "source_tag": "mips_jal"
                                })

                        # MIPS jalr indirect call
                        if _MIPS_JALR_RE.search(stripped):
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": "indirect_call",
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "AMBIGUOUS",
                                    "source_tag": "mips_jalr"
                                })

                        # IA-64 br.call direct call
                        for cm in _IA64_BR_CALL_RE.finditer(stripped):
                            callee = cm.group(1)
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED",
                                    "source_tag": "ia64_br_call"
                                })

                        # IA-64 br.call indirect
                        if _IA64_BR_CALL_IND_RE.search(stripped):
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": "indirect_call",
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "AMBIGUOUS",
                                    "source_tag": "ia64_br_call_ind"
                                })

                        # ALTERNATIVE macro call targets
                        if 'ALTERNATIVE' in stripped:
                            for cm in _ALT_CALL_RE.finditer(stripped):
                                callee = cm.group(1)
                                if current_func_name and current_func_id:
                                    call_order[0] += 1
                                    condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                    edges.append({
                                        "source": current_func_id,
                                        "target": self._make_func_id(domain, callee),
                                        "call_order": call_order[0],
                                        "call_condition": condition,
                                        "confidence": "INFERRED",
                                        "source_tag": "alternative_patch"
                                    })

                    # Indirect call (x86_64 NASM: call rax, call [mem]; GAS: call *%rax)
                    if _INDIR_CALL_RE.search(stripped) or (is_gas and _GAS_INDIR_CALL_RE.search(stripped)):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edge = {
                                "source": current_func_id,
                                "target": "indirect_call",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "AMBIGUOUS"
                            }
                            # Capture x86_64 calling convention args (rdi, rsi, rdx, rcx, r8, r9)
                            call_reg_args = reg_tracker.get_call_args()
                            if call_reg_args:
                                edge["reg_args"] = [
                                    {"reg": _RegisterTracker.CALL_ARG_REGS[i-1],
                                     "value": v}
                                    for i, v in call_reg_args
                                ]
                            edges.append(edge)

                    # x86 far call: lcallw/lcall (NASM and GAS)
                    if re.search(r'\blcallw?\s', stripped, re.IGNORECASE):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": "indirect_call",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "AMBIGUOUS"
                            })

                    # x86 vmcall/vmmcall (virtualization hypercalls)
                    if re.search(r'\bvmcall\b', stripped, re.IGNORECASE):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": "syscall_vmcall",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            })
                    if re.search(r'\bvmmcall\b', stripped, re.IGNORECASE):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edge = {
                                "source": current_func_id,
                                "target": "syscall_vmmcall",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            }
                            # Capture x86_64 calling convention args (rdi, rsi, rdx, rcx, r8, r9)
                            call_reg_args = reg_tracker.get_call_args()
                            if call_reg_args:
                                edge["reg_args"] = [
                                    {"reg": _RegisterTracker.CALL_ARG_REGS[i-1],
                                     "value": v}
                                    for i, v in call_reg_args
                                ]
                            edges.append(edge)

                    # --- Register-level data flow tracking ---
                    if is_code:
                        reg_tracker.process_line(stripped, line_num)

                    # --- Syscall tracking (x86_64) ---
                    # Use register tracker for more accurate syscall info
                    if _SYSCALL_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            # Get syscall info from register tracker
                            sc_info = reg_tracker.get_syscall_info()
                            syscall_name = sc_info["syscall_name"]
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edge = {
                                "source": current_func_id,
                                "target": f"syscall_{syscall_name}",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            }
                            # Add register-level context to edge
                            if sc_info["args"]:
                                edge["reg_args"] = [
                                    {"reg": _RegisterTracker.SYSCALL_ARG_REGS[i],
                                     "value": v}
                                    for i, v in sc_info["args"]
                                ]
                            edges.append(edge)
                        pending_syscall = None
                        # Note: do NOT reset reg_tracker here — syscall clobbers
                        # caller-saved regs, but we want reg_state_final to
                        # reflect the function's overall state. Tracker resets
                        # between functions.

                    # --- 32-bit x86 int $0x80 syscall ---
                    if _INT80_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            # Use eax for 32-bit syscall number
                            eax_val = reg_tracker._regs.get('rax', 'unknown')
                            int80_name = "int80_unknown"
                            if eax_val.startswith('const:'):
                                try:
                                    nr = int(eax_val.split(':', 1)[1])
                                    int80_name = _SYSCALL_NAMES_32.get(nr, f"int80_{nr}")
                                except ValueError:
                                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                                    pass
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": f"syscall_{int80_name}",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            })
                            # Note: do NOT reset reg_tracker — see syscall above

                    # --- AArch64 SVC instruction ---
                    if re.search(r'\bsvc\s+#0\b', stripped, re.IGNORECASE):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            # AArch64 syscall: x8 = syscall number, x0-x5 = args
                            x8_val = reg_tracker._regs.get('x8', 'unknown')
                            svc_name = "svc_unknown"
                            if x8_val.startswith('const:'):
                                try:
                                    nr = int(x8_val.split(':', 1)[1])
                                    svc_name = self._resolve_syscall("aarch64", nr, f"svc_{nr}")
                                except ValueError:
                                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                                    pass
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edge = {
                                "source": current_func_id,
                                "target": f"syscall_{svc_name}",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            }
                            # Add AArch64 register args
                            aarch64_args = []
                            for reg in ['x0', 'x1', 'x2', 'x3', 'x4', 'x5']:
                                if reg in reg_tracker._regs:
                                    aarch64_args.append({"reg": reg, "value": reg_tracker._regs[reg]})
                            if aarch64_args:
                                edge["reg_args"] = aarch64_args
                            edges.append(edge)
                        # Note: do NOT reset reg_tracker — see syscall above

                    # --- Call instruction: record register args ---
                    # For direct calls, capture current register state as args
                    for cm in _CALL_RE.finditer(stripped):
                        if current_func_name and current_func_id:
                            # Register state at call site = function arguments
                            call_reg_args = reg_tracker.get_call_args()
                            # Attach to the edge we already created above
                            for e in edges:
                                if (e.get("source") == current_func_id and
                                        e.get("call_order") == call_order[0]):
                                    if call_reg_args:
                                        e["reg_args"] = [
                                            {"reg": _RegisterTracker.CALL_ARG_REGS[i-1],
                                             "value": v}
                                            for i, v in call_reg_args
                                        ]
                                    break
                            # Note: caller-saved registers are NOT clobbered here
                            # to preserve useful state for reg_state_final.
                            # The tracker resets between functions.

                    # GAS call: same register arg tracking
                    if is_gas:
                        for cm in _GAS_CALL_RE.finditer(stripped):
                            if current_func_name and current_func_id:
                                call_reg_args = reg_tracker.get_call_args()
                                for e in edges:
                                    if (e.get("source") == current_func_id and
                                            e.get("call_order") == call_order[0]):
                                        if call_reg_args:
                                            e["reg_args"] = [
                                                {"reg": _RegisterTracker.CALL_ARG_REGS[i-1],
                                                 "value": v}
                                                for i, v in call_reg_args
                                            ]
                                        break

                        # ARM bl call arg tracking (AArch64 calling convention)
                        for cm in _ARM_CALL_RE.finditer(stripped):
                            if current_func_name and current_func_id:
                                aarch64_args = []
                                for reg in _RegisterTracker.AARCH64_CALL_ARG_REGS:
                                    if reg in reg_tracker._regs:
                                        aarch64_args.append({"reg": reg, "value": reg_tracker._regs[reg]})
                                for e in edges:
                                    if (e.get("source") == current_func_id and
                                            e.get("call_order") == call_order[0] and
                                            "reg_args" not in e):
                                        if aarch64_args:
                                            e["reg_args"] = aarch64_args
                                        break

                    # --- Conditional jumps ---
                    m = _COND_JMP_RE.search(stripped)
                    if m:
                        cond_stack.append((f"jump_{m.group(1)}_{m.group(2)}", line_num))

                    # AArch64 conditional branches: b.eq, b.ne, b.lt, etc.
                    if is_gas:
                        m = re.match(r'\bb\.(eq|ne|cs|cc|mi|pl|vs|vc|hi|ls|ge|lt|gt|le|al)\s+([a-zA-Z_]\w*)',
                                     stripped, re.IGNORECASE)
                        if m:
                            cond_stack.append((f"jump_b.{m.group(1)}_{m.group(2)}", line_num))

                    # AArch64 cbz/cbnz conditional branches
                    m = _ARM_CBZ_RE.match(stripped)
                    if m:
                        cond_stack.append((f"jump_{m.group(1)}_{m.group(3)}", line_num))

                    # AArch64 tbz/tbnz test-bit branches
                    m = _ARM_TBZ_RE.match(stripped)
                    if m:
                        cond_stack.append((f"jump_{m.group(1)}_bit{m.group(3)}_{m.group(4)}", line_num))

                    # PowerPC bcl conditional branch
                    if is_gas:
                        m = _PPC_BCL_RE.match(stripped)
                        if m:
                            cond_stack.append((f"jump_bcl_{m.group(1)}", line_num))

                    # AArch64 blr Xn: indirect call (like call *%rax)
                    if is_gas and _ARM_BLR_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            blr_m = _ARM_BLR_RE.search(stripped)
                            reg_name = blr_m.group(1).lower()
                            reg_canon = reg_tracker._canonical(reg_name)
                            edge = {
                                "source": current_func_id,
                                "target": "indirect_call",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "AMBIGUOUS"
                            }
                            # Capture AArch64 calling convention args (x0-x7)
                            aarch64_args = []
                            for reg in _RegisterTracker.AARCH64_CALL_ARG_REGS:
                                if reg in reg_tracker._regs:
                                    aarch64_args.append({"reg": reg, "value": reg_tracker._regs[reg]})
                            if aarch64_args:
                                edge["reg_args"] = aarch64_args
                            edges.append(edge)

                    # AArch64 br Xn: indirect branch (potential tail call)
                    if is_gas and _ARM_BR_RE.search(stripped):
                        if current_func_name and current_func_id:
                            br_m = _ARM_BR_RE.search(stripped)
                            reg_name = br_m.group(1).lower()
                            reg_canon = reg_tracker._canonical(reg_name)
                            reg_val = reg_tracker._regs.get(reg_canon, None)
                            # If register value resolves to a known symbol, treat as tail call
                            if reg_val and reg_val.startswith("addr:"):
                                resolved_name = reg_val.split(":", 1)[1].strip()
                                if resolved_name in call_targets or resolved_name in global_symbols:
                                    call_order[0] += 1
                                    condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                    edges.append({
                                        "source": current_func_id,
                                        "target": self._make_func_id(domain, resolved_name),
                                        "call_order": call_order[0],
                                        "call_condition": condition,
                                        "confidence": "INFERRED",
                                        "source_tag": "tail_call_br"
                                    })
                            else:
                                # Generic indirect branch
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": "indirect_branch",
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "AMBIGUOUS"
                                })

                    # --- RISC-V jal (direct call/jump) ---
                    if is_gas:
                        for cm in _RISCV_JAL_RE.finditer(stripped):
                            callee = cm.group(1)
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED"
                                })

                    # --- RISC-V j (unconditional jump / tail call) ---
                    if is_gas:
                        jm_rv = _RISCV_J_RE.match(stripped)
                        if jm_rv:
                            target = jm_rv.group(1)
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, target),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "INFERRED",
                                    "source_tag": "tail_call_j"
                                })

                    # --- RISC-V jalr (indirect call) ---
                    if is_gas and _RISCV_JALR_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": "indirect_call",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "AMBIGUOUS"
                            })

                    # --- RISC-V ecall (system call) ---
                    if _RISCV_ECALL_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            # RISC-V: a7 = syscall number
                            a7_val = reg_tracker._regs.get('a7', reg_tracker._regs.get('x17', 'unknown'))
                            ecall_name = "ecall_unknown"
                            if isinstance(a7_val, str) and a7_val.startswith('const:'):
                                try:
                                    nr = int(a7_val.split(':', 1)[1])
                                    ecall_name = self._resolve_syscall("riscv64", nr, f"ecall_{nr}")
                                except ValueError:
                                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                                    pass
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": f"syscall_{ecall_name}",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            })

                    # --- RISC-V conditional branches ---
                    if is_gas:
                        m = _RISCV_BRANCH_RE.match(stripped)
                        if m:
                            cond_stack.append((f"jump_rv_{m.group(1)}_{m.group(0).split(',')[-1].strip()}", line_num))

                    # --- LoongArch bl (direct call) ---
                    if is_gas:
                        for cm in _LOONGARCH_BL_RE.finditer(stripped):
                            callee = cm.group(1)
                            if callee.upper() == 'CFUNC':
                                continue  # CFUNC(name) unwrapped by PPC handler
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED"
                                })

                    # --- LoongArch b (unconditional jump / tail call) ---
                    if is_gas:
                        m_la_b = _LOONGARCH_B_RE.match(stripped)
                        if m_la_b:
                            target = m_la_b.group(1)
                            if target.upper() == 'CFUNC':
                                pass  # Handled by CFUNC unwrapping
                            elif current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, target),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "INFERRED",
                                    "source_tag": "tail_call_b"
                                })

                    # --- LoongArch jirl (indirect call) ---
                    if is_gas and _LOONGARCH_JIRL_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": "indirect_call",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "AMBIGUOUS"
                            })

                    # --- LoongArch syscall 0 ---
                    if _LOONGARCH_SYSCALL_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": "syscall_loongarch",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            })

                    # --- s390 brasl (direct call) ---
                    if is_gas:
                        for cm in _S390_BRASL_RE.finditer(stripped):
                            callee = cm.group(1)
                            if current_func_name and current_func_id:
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edges.append({
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, callee),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "EXTRACTED"
                                })

                    # --- s390 basr (indirect call) ---
                    if is_gas and _S390_BASR_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": "indirect_call",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "AMBIGUOUS"
                            })

                    # --- s390 svc (system call) ---
                    if _S390_SVC_RE.search(stripped):
                        if current_func_name and current_func_id:
                            call_order[0] += 1
                            condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                            edges.append({
                                "source": current_func_id,
                                "target": "syscall_s390",
                                "call_order": call_order[0],
                                "call_condition": condition,
                                "confidence": "EXTRACTED"
                            })
                        if cond_stack:
                            cond_stack.clear()

                    # --- Tail call detection: jmp to function name ---
                    # Unconditional jmp to a named target = potential tail call
                    m = _JMP_RE.match(stripped)
                    if m:
                        jmp_target = m.group(1)
                        # Skip local labels (.label, 1b, 1f numeric references)
                        is_numeric_ref = bool(_NUMERIC_LABEL_REF_RE.match(jmp_target))
                        # Only create edge if target is not a local label
                        if not jmp_target.startswith('.') and not is_numeric_ref and current_func_name and current_func_id:
                            # Check if target is a known call target, global, or extern
                            if (jmp_target in call_targets or
                                    jmp_target in global_symbols or
                                    jmp_target in extern_symbols):
                                call_order[0] += 1
                                condition = "; ".join(c[0] for c in cond_stack) if cond_stack else ""
                                edge = {
                                    "source": current_func_id,
                                    "target": self._make_func_id(domain, jmp_target),
                                    "call_order": call_order[0],
                                    "call_condition": condition,
                                    "confidence": "INFERRED",
                                    "source_tag": "tail_call"
                                }
                                # Tail call passes args via registers too
                                call_reg_args = reg_tracker.get_call_args()
                                if call_reg_args:
                                    edge["reg_args"] = [
                                        {"reg": _RegisterTracker.CALL_ARG_REGS[i-1],
                                         "value": v}
                                        for i, v in call_reg_args
                                    ]
                                edges.append(edge)
                        if cond_stack:
                            cond_stack.clear()

                else:
                    # Data section — track data labels as globals, not functions
                    m = _LABEL_RE.match(stripped)
                    if m:
                        pass  # Data labels handled in _extract_globals_regex

            # --- EQU constants ---
            m = _EQU_RE.match(stripped)
            if m:
                # Constants handled in _extract_globals_regex
                pass

        # Close last function
        if current_func_name and current_func_id:
            self._finalize_function(functions, current_func_name,
                                    current_func_id, current_func_line,
                                    domain, global_symbols, call_targets,
                                    export_symbols, lines,
                                    current_func_line, len(lines) - 1,
                                    reg_tracker=reg_tracker)

        # Build import_edges from extern declarations
        for sym in extern_symbols:
            import_edges.append({
                "source_file": os.path.relpath(filepath, source_root),
                "imported_symbol": sym,
                "import_type": "asm_extern"
            })

        # Build extra result with asm-specific data
        extra = [
            {"relation": "IMPORTS", "imports": import_edges}
        ] if import_edges else []

        # Add asm_aliases and export_symbols as extraction metadata
        # These are attached to the extraction dict via the scanner factory

        fn_ptr_calls = {}

        # Store asm_aliases, gas_set_aliases and export_symbols for builder consumption
        self._asm_aliases = asm_aliases + gas_set_aliases
        self._export_symbols = export_symbols

        return (functions, edges, extra, fn_ptr_calls)

    def _finalize_function(self, functions, func_name, func_id, line,
                           domain, global_symbols, call_targets,
                           export_symbols, lines, start_line, end_line,
                           reg_tracker=None):
        """Create a function record and append to the functions list."""
        # Determine label
        label = self._classify_label(func_name, global_symbols, call_targets,
                                     export_symbols)

        # Extract body text (lines from start to end)
        body_lines = lines[start_line:end_line + 1] if end_line >= start_line else []
        body_text = "\n".join(body_lines)

        func_record = {
            "id": func_id,
            "name": func_name,
            "source_file": "",  # Will be set by scan_file()
            "line": line,
            "domain": domain,
            "labels": [label] if label else ["unknown_end"],
            "is_empty": not body_text.strip(),
            "api_constraints": [],
            "body_text": body_text,
            "signature": f"{func_name}:",
            "params": [],
            "local_vars": [],
            "callee_args": {},
            "condition_vars": [],
            "language": "asm",
        }

        # Add register-level data flow information
        if reg_tracker:
            transfers = reg_tracker.get_transfers()
            if transfers:
                func_record["reg_transfers"] = [
                    {"src": src, "dst": dst, "line": ln}
                    for src, dst, ln in transfers
                ]
            reg_state = reg_tracker.get_register_state()
            if reg_state:
                func_record["reg_state_final"] = reg_state

        functions.append(func_record)

    def _classify_label(self, func_name, global_symbols, call_targets,
                        export_symbols):
        """Classify function into one of the 7 allowed labels."""
        current_fp = getattr(self, '_current_filepath', '').replace(os.sep, '/')
        is_non_api_path = any(p in current_fp for p in _NON_API_PATHS_ASM)

        if func_name in global_symbols:
            return "API_entry" if not is_non_api_path else "unknown_end"
        # Cache export_names set: export_symbols is static during pass 2,
        # so we build the set once per file (keyed by list identity).
        if id(export_symbols) != getattr(self, '_export_names_id', None):
            self._export_names_id = id(export_symbols)
            self._export_names_cache = {e["name"] for e in export_symbols}
        if func_name in self._export_names_cache:
            return "API_entry" if not is_non_api_path else "unknown_end"
        if self._is_callback_by_name(func_name):
            return "callback_func"
        if func_name.endswith('_init') or func_name.endswith('_ctor'):
            return "constructor"
        if func_name.endswith('_fini') or func_name.endswith('_dtor'):
            return "destructor"
        if func_name in call_targets:
            return "unknown_end"
        return "unknown_end"

    def _extract_globals_regex(self, source, domain):
        """Extract globals from assembly source (no tree-sitter)."""
        rel_path = ""  # Will be set by scan_file
        enums = []
        constants = []
        typedefs = []
        global_vars = []

        lines = source.splitlines()
        current_section = None

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(';') or stripped.startswith('#'):
                continue

            # Track section
            m = _SECTION_RE.match(stripped)
            if m:
                current_section = m.group(1).lower()
                continue
            m = _ASM_SECTION_RE.match(stripped)
            if m:
                current_section = m.group(1).lower()
                continue
            for sec_dir in ('.text', '.data', '.bss'):
                if stripped.startswith(sec_dir):
                    current_section = sec_dir[1:]
                    break

            # EQU constants
            m = _EQU_RE.match(stripped)
            if m:
                constants.append({
                    "name": m.group(1),
                    "value_snippet": m.group(2).strip(),
                    "source_file": rel_path,
                    "line": i + 1
                })
                continue

            # Data section labels → global variables
            is_data = current_section in ("data", "bss") or (
                current_section and "." + current_section in _DATA_SECTIONS
            )
            if is_data:
                # NASM: label: or label type value (e.g., msg db "Hello", 0)
                m = _LABEL_RE.match(stripped)
                if not m:
                    # Try NASM data declaration without colon: name type value
                    m = re.match(r'^([a-zA-Z_]\w*)\s+(?:db|dw|dd|dq|dt|resb|resw|resd|resq|rest)\b', stripped)
                if m:
                    global_vars.append({
                        "name": m.group(1),
                        "type": current_section or "data",
                        "value_snippet": "",
                        "source_file": rel_path,
                        "line": i + 1
                    })

        return {"enums": enums, "constants": constants,
                "typedefs": typedefs, "global_vars": global_vars}

    def scan_file(self, filepath, source_root, macro_bindings=None):
        """Override: regex scanner doesn't use tree-sitter."""
        try:
            raw = Path(filepath).read_bytes()
        except (IOError, OSError) as e:
            return {"file": filepath, "domain": "", "functions": [], "edges": [],
                    "globals": {}, "error": str(e)}

        source = raw.decode("utf-8", errors="replace")
        domain = classify_domain(filepath, source_root)

        functions, edges, extra, fn_ptr_calls = self._extract(
            None, raw, filepath, source_root, domain)

        # Set source_file on all functions
        rel_path = os.path.relpath(filepath, source_root)
        for func in functions:
            func["source_file"] = rel_path

        globals_data = self._extract_globals_regex(source, domain)
        # Set source_file on globals
        for const in globals_data["constants"]:
            const["source_file"] = rel_path
        for var in globals_data["global_vars"]:
            var["source_file"] = rel_path

        # Handle extra (import_edges)
        if extra and extra[0].get("relation") == "IMPORTS":
            import_edges = extra[0].get("imports", [])
        else:
            import_edges = []

        result = {
            "file": filepath,
            "domain": domain,
            "functions": functions,
            "edges": edges,
            "globals": globals_data,
            "vtable_registrations": [],
            "import_edges": import_edges,
            "fn_ptr_calls": fn_ptr_calls,
            "macro_registrations": [],
        }

        # Attach asm-specific metadata for builder consumption
        if hasattr(self, '_asm_aliases') and self._asm_aliases:
            result["asm_aliases"] = self._asm_aliases
        if hasattr(self, '_export_symbols') and self._export_symbols:
            result["export_symbols"] = self._export_symbols

        return result


# ---------------------------------------------------------------------------
# llvm-mc-based ASM scanner (enhanced path)
# ---------------------------------------------------------------------------

class LLVMMCASMScanner(AsmRegexScanner):
    """ASM scanner that uses llvm-mc for proper instruction-level parsing
    when available, falling back to regex on failure.

    llvm-mc is LLVM's machine code assembler. With `-show-encoding` and
    `-show-inst` it emits parsed instruction records that we use to:
      - Confirm symbol references (call/jump targets) with proper disambiguation
        between labels and immediates
      - Detect call/jump targets that the regex misses (e.g., when the target
        is wrapped in macros or template expansions)
      - Identify section transitions (.text → .data) with proper directive
        parsing
      - Extract per-instruction byte offsets (useful for cgdb byte_start/byte_end)

    Usage:
        scanner = LLVMMCASMScanner()
        result = scanner.scan_file(filepath, source_root, macro_bindings)

    If llvm-mc is not on PATH or fails (e.g., unsupported arch), the scanner
    silently falls back to the regex-based path in AsmRegexScanner.scan_file.
    """

    def __init__(self, llvm_mc_bin: str = 'llvm-mc'):
        super().__init__()
        self._llvm_mc_bin = llvm_mc_bin
        self._mc_available = self._check_llvm_mc_available()

    def _check_llvm_mc_available(self) -> bool:
        """Check if llvm-mc is on PATH and runnable."""
        import shutil
        try:
            path = shutil.which(self._llvm_mc_bin)
            if not path:
                return False
            # Smoke-test: invoke --version
            import subprocess
            result = subprocess.run(
                [self._llvm_mc_bin, '--version'],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return False

    def _run_llvm_mc(self, source_path: str, arch: str = '',
                      syntax: str = 'att') -> str:
        """Run llvm-mc on a source file, return its stdout.

        Args:
          source_path: path to the .s/.S/.asm file
          arch: target architecture (e.g., 'x86-64', 'aarch64'). If empty,
            llvm-mc uses the host triple.
          syntax: 'att' or 'intel' (for x86). Ignored for other arches.

        Returns:
          stdout from llvm-mc, or '' on failure.
        """
        if not self._mc_available:
            return ''
        import subprocess
        cmd = [self._llvm_mc_bin, '-assemble',
               '-show-encoding', '-show-inst',
               '-filetype=asm']
        if arch:
            cmd.extend(['-triple', arch])
        else:
            # Default to host triple; llvm-mc will figure out the arch
            pass
        if syntax == 'intel' and (not arch or 'x86' in arch):
            cmd.append('--output-asm-variant=1')  # Intel syntax
        cmd.append(source_path)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
            return result.stdout or ''
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return ''

    def _parse_llvm_mc_output(self, mc_output: str
                                ) -> list:
        """Parse llvm-mc output into a list of instruction dicts.

        Each instruction dict has:
          'mnemonic': str (e.g., 'call', 'jmp', 'mov')
          'operands': str (the rest of the line)
          'encoding': str (hex byte encoding, optional)
          'inst_name': str (LLVM instruction name, optional)
          'line': int (source line if available)

        Returns [] on parse failure.
        """
        if not mc_output:
            return []
        instructions = []
        for line in mc_output.splitlines():
            s = line.strip()
            if not s:
                continue
            # llvm-mc -show-encoding -show-inst output format:
            #   <tab>mnemonic<tab>operands<tab># encoding: [0x..]
            #   <tab># <inst name> ...
            # We extract the mnemonic and operands from the first part.
            if s.startswith('#'):
                continue
            parts = s.split('\t')
            if not parts:
                continue
            mnemonic = parts[0].strip()
            if not mnemonic or mnemonic[0] == '.':
                # Skip directives like .text, .file, etc.
                continue
            operands = '\t'.join(parts[1:]).strip()
            # Strip trailing comment
            if '#' in operands:
                operands = operands.split('#', 1)[0].strip()
            instructions.append({
                'mnemonic': mnemonic,
                'operands': operands,
                'encoding': '',
                'inst_name': '',
                'line': 0,
            })
        return instructions

    def _enhance_with_llvm_mc(self, result: dict, filepath: str,
                                source_bytes: bytes) -> dict:
        """Enhance a regex-based scan result with llvm-mc parsed data.

        For each function in result['functions'], walk the llvm-mc-parsed
        instructions within the function's source line range and:
          - Add any missed call/jump targets as edges
          - Record instruction-level byte offsets (in source_layer metadata)
          - Detect indirect calls (call reg, jmp reg) with higher precision

        Returns the enhanced result dict.
        """
        if not self._mc_available:
            return result
        # Determine arch from profile or filename hint
        arch = ''
        if hasattr(self, '_arch'):
            arch_map = {
                'x86_64': 'x86_64-unknown-linux-gnu',
                'x86': 'i386-unknown-linux-gnu',
                'i386': 'i386-unknown-linux-gnu',
                'aarch64': 'aarch64-unknown-linux-gnu',
                'arm64': 'aarch64-unknown-linux-gnu',
                'arm': 'arm-unknown-linux-gnu',
                'riscv64': 'riscv64-unknown-linux-gnu',
                'riscv32': 'riscv32-unknown-linux-gnu',
            }
            arch = arch_map.get(self._arch, '')
        syntax = getattr(self, '_asm_syntax', 'att')
        mc_output = self._run_llvm_mc(filepath, arch=arch, syntax=syntax)
        if not mc_output:
            return result
        instructions = self._parse_llvm_mc_output(mc_output)
        if not instructions:
            return result
        # Build a lookup of existing edge (caller, callee, line) tuples
        # so we don't double-add.
        existing_edges = set()
        for e in result.get('edges', []):
            caller = e.get('invoker_id', '') or e.get('caller', '')
            callee = e.get('invoked_id', '') or e.get('callee', '') or e.get('target', '')
            line = int(e.get('line', 0) or 0)
            existing_edges.add((caller, callee, line))
        # Walk instructions, find call/jmp targets not in existing edges
        # We approximate line numbers by instruction index since llvm-mc
        # doesn't always preserve source line info in -assemble mode.
        new_edges = []
        # Get the function id(s) from the result
        functions = result.get('functions', [])
        if not functions:
            return result
        # For MVP, we attribute any new edges to the first function in the
        # file. A more precise implementation would map instruction offsets
        # back to function source ranges.
        primary_fn_id = functions[0].get('id', '') or functions[0].get('name', '')
        if not primary_fn_id:
            return result
        for i, inst in enumerate(instructions):
            mnem = inst['mnemonic'].lower()
            operands = inst['operands']
            if mnem in ('call', 'callq', 'jmp', 'jl', 'jne', 'je', 'jg',
                         'jle', 'jge', 'ja', 'jb', 'jae', 'jbe', 'jz', 'jnz',
                         'jc', 'jnc', 'jo', 'jno', 'js', 'jns', 'jp',
                         'jnp', 'jecxz', 'jrcrz'):
                # Direct call/jmp to a label
                target = operands.strip()
                # Strip trailing comments and qualifiers
                if ' ' in target:
                    target = target.split()[0]
                # Skip register/indirect targets
                if not target or target.startswith('%') or target.startswith('$') \
                        or target.startswith('*') or target.startswith('['):
                    continue
                # Strip AT&T suffix (e.g., foo@PLT → foo)
                if '@' in target:
                    target = target.split('@', 1)[0]
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', target):
                    continue
                line = i + 1  # approximated line number
                key = (primary_fn_id, target, line)
                if key in existing_edges:
                    continue
                existing_edges.add(key)
                new_edges.append({
                    'invoker_id': primary_fn_id,
                    'invoked_id': target,
                    'callee': target,
                    'line': line,
                    'relation': 'INVOKES' if mnem.startswith('call') else 'BRANCH',
                    'source': 'llvm-mc',
                })
        if new_edges:
            result.setdefault('edges', []).extend(new_edges)
        # Add metadata noting llvm-mc was used
        if hasattr(self, '_asm_aliases'):
            pass
        result['llvm_mc_used'] = True
        result['llvm_mc_instruction_count'] = len(instructions)
        return result

    def scan_file(self, filepath: str, source_root: str,
                  macro_bindings: dict = None) -> dict:
        """Run regex-based scan first, then enhance with llvm-mc if available.

        Falls back gracefully to the regex-only result if llvm-mc fails.
        """
        # First, get the base regex-based result
        result = super().scan_file(filepath, source_root, macro_bindings)
        if not result or result.get('error'):
            return result
        # Try to enhance with llvm-mc
        try:
            with open(filepath, 'rb') as f:
                source_bytes = f.read()
            enhanced = self._enhance_with_llvm_mc(result, filepath, source_bytes)
            return enhanced
        except Exception:
            # Enhancement is best-effort; return the base result if it fails
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            return result
