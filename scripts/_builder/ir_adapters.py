"""IR Adapter unified interface — design-report §12.3.

Defines the abstract base class `IRAdapter` and concrete adapter stubs for
each language tier (A/B/C). Per the design report:

  Report-L3 IR Adapter 统一接口
  ```python
  class IRAdapter:
      def parse(self, source_or_bytecode) -> IRModule
      def get_functions(self) -> List[IRFunction]
      def get_cfg(self, func) -> CFG
      def get_ssa_values(self, func) -> List[SSAValue]
      def get_mem_accesses(self, func) -> List[MemAccess]
      def get_call_edges(self) -> List[CallEdge]
      def get_alias_sets(self, scope) -> List[AliasSet]
      def get_data_deps(self, func) -> List[DataDep]
      def align_to_ast(self, ir_node) -> ASTNode  # via debug info
  ```

Concrete adapters (per design-report §12.3 + appendix A.2):
  - LLVMIRAdapter          (C/C++/ObjC/Rust/Swift/Zig/CUDA — tier A)
  - JimpleIRAdapter        (Java/Kotlin/Scala — tier B)
  - MSILIRAdapter          (C#/F#/VB.NET — tier B)
  - GoSSAAdapter           (Go — tier B)
  - PythonBytecodeAdapter  (Python — tier C, weak)
  - TSCompilerAdapter      (TypeScript/JavaScript — tier C-strong)
  - NoneIRAdapter          (Lua/Shell — tier C, no IR)

The base class defines the contract; concrete adapters either delegate to
the real analysis backend (LLVM/SVF/Soot/Roslyn/etc.) when available, or
fall back to AST-derived approximations otherwise.

This module is a stub-able framework — adapters return [] or heuristic
results when the underlying IR toolchain is not installed. The DB layer
(cgdb_schema v4 `ir_functions`/`ssa_values`/`mem_accesses`/`points_to`/
`indirect_calls`/`data_deps`/`path_states`) is populated by the adapter
when real IR data is available; otherwise the cgdb legacy L4/L5 tables
(basic_blocks/cfg_edges/data_flow/alias_sets) are used as approximation.
"""
from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


# ============================================================================
# Data classes (mirror cgdb_schema v4 IR tables)
# ============================================================================

@dataclass
class IRModule:
    """A parsed IR module (one TU / one .bc / one .class)."""
    name: str
    language: str
    ir_kind: str  # 'llvm-ir' | 'jimple' | 'msil' | 'go-ssa' | 'python-bytecode' | 'ts-compiler' | 'none'
    source_path: Optional[str] = None
    ir_path: Optional[str] = None  # path to .bc/.ll/.class/.dll etc.
    functions: List["IRFunction"] = field(default_factory=list)
    debug_metadata_present: bool = False


@dataclass
class IRFunction:
    """An IR-level function (one entry in `ir_functions` table)."""
    ir_name: str
    symbol_id: Optional[int] = None  # aligned to cgdb_nodes.id
    entry_block_id: Optional[int] = None
    num_blocks: int = 0
    num_instructions: int = 0
    aligned: bool = False
    debug_metadata_present: bool = False
    blocks: List["BasicBlock"] = field(default_factory=list)
    ssa_values: List["SSAValue"] = field(default_factory=list)
    mem_accesses: List["MemAccess"] = field(default_factory=list)


@dataclass
class BasicBlock:
    block_id: int
    block_index: int
    is_entry: bool = False
    is_exit: bool = False
    successors: List[int] = field(default_factory=list)  # next block ids
    predecessors: List[int] = field(default_factory=list)


@dataclass
class CFG:
    """Control-flow graph for one function."""
    function_ir_name: str
    blocks: List[BasicBlock] = field(default_factory=list)
    edges: List["CFGEdge"] = field(default_factory=list)


@dataclass
class CFGEdge:
    src_block_id: int
    dst_block_id: int
    kind: str  # 'fallthrough' | 'true_branch' | 'false_branch' | 'exception'
    condition_id: Optional[int] = None


@dataclass
class SSAValue:
    """One SSA value (one entry in `ssa_values` table)."""
    value_name: str
    type: str
    def_kind: str  # 'entry' | 'instruction' | 'constant' | 'phi' | ...
    def_block_id: Optional[int] = None
    def_line: Optional[int] = None
    def_col: Optional[int] = None
    aligned_symbol_id: Optional[int] = None
    aligned_ast_node_id: Optional[int] = None
    aligned_token_id: Optional[int] = None


@dataclass
class MemAccess:
    """One memory access site (one entry in `mem_accesses` table)."""
    function_ir_name: str
    kind: str = "load"  # 'load' | 'store' | 'memcpy' | ...
    block_id: Optional[int] = None
    ptr_ssa_id: Optional[int] = None
    value_ssa_id: Optional[int] = None
    line: int = 0
    col: int = 0
    aligned_symbol_id: Optional[int] = None
    aligned_ast_node_id: Optional[int] = None
    aligned_token_id: Optional[int] = None


@dataclass
class CallEdge:
    """One IR-level call edge."""
    caller_ir_name: str
    callee_ir_name: str
    call_site_id: Optional[int] = None
    is_indirect: bool = False
    possible_targets: List[int] = field(default_factory=list)  # symbol_ids
    confidence: float = 1.0
    analysis: str = "manual"  # 'svf' | 'andersen' | 'devirt' | 'heuristic' | 'manual'


@dataclass
class AliasSet:
    """One alias-set entry (one row in `alias_sets` table)."""
    ptr1_ssa_id: int
    ptr2_ssa_id: int
    kind: str = "may_alias"  # 'must_alias' | 'may_alias' | 'no_alias'
    analysis: str = "heuristic"
    confidence: float = 1.0


@dataclass
class DataDep:
    """One data dependency edge (one row in `data_deps` table)."""
    from_ssa_id: int
    to_ssa_id: int
    kind: str = "def-use"  # 'def-use' | 'mem-dep' | 'ctrl-dep' | 'phi' | 'alias'
    function_ir_name: str = ""


@dataclass
class PathState:
    """One path-sensitive analysis state (one row in `path_states` table)."""
    function_ir_name: str
    path_id: str = ""
    block_id: Optional[int] = None
    constraints: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)
    line: int = 0


# ============================================================================
# Abstract base class — design-report §12.3 contract
# ============================================================================

class IRAdapter(ABC):
    """Abstract base class for IR adapters.

    Each language tier implements one adapter. The adapter takes either
    source code or compiled bytecode/IR as input and produces a normalized
    IR representation that can be ingested into the cgdb v4 IR tables
    (ir_functions, ssa_values, mem_accesses, points_to, indirect_calls,
    data_deps, path_states).

    Per design-report §2.5.3 layer capabilities:
        - L1+L2 only: no IR available (adapter returns empty IRModule)
        - L1+L2+L3-weak: partial IR (CFG + data_flow, no SSA/alias)
        - L1+L2+L3: full IR (SSA + mem_accesses + alias_sets + points_to)
        - L1+L2+L3+L4: full IR + precomputed reachability/embeddings

    Subclasses should set `tier` and `ir_kind` accordingly.
    """

    # Subclass-overridden class attributes
    language: str = "unknown"
    tier: str = "C"  # 'A' | 'B' | 'C'
    ir_kind: str = "none"  # 'llvm-ir' | 'jimple' | 'msil' | 'go-ssa' | 'python-bytecode' | 'ts-compiler' | 'none'
    coverage_level: str = "L1+L2"  # 'L1+L2' | 'L1+L2+L3-weak' | 'L1+L2+L3' | 'L1+L2+L3+L4'
    requires_external_toolchain: bool = False
    external_toolchain_name: Optional[str] = None  # e.g., 'llvm', 'soot', 'roslyn'

    @abstractmethod
    def parse(self, source_or_bytecode: str) -> IRModule:
        """Parse source code or bytecode path → IRModule.

        For A-tier (LLVM): input is a .c/.cpp/.rs/.swift/.zig/.cu file path
            or a .bc/.ll bitcode path.
        For B-tier: input is a .java/.kt/.scala/.cs file path or a .class/.dll.
        For C-tier: input is a .py/.js/.ts/.rb/.php file path.
        """
        raise NotImplementedError

    @abstractmethod
    def get_functions(self, module: IRModule) -> List[IRFunction]:
        """Extract all IR functions from a parsed module."""
        raise NotImplementedError

    @abstractmethod
    def get_cfg(self, func: IRFunction) -> CFG:
        """Build the CFG for one IR function."""
        raise NotImplementedError

    @abstractmethod
    def get_ssa_values(self, func: IRFunction) -> List[SSAValue]:
        """Extract SSA values for one IR function. May return [] if the
        adapter only provides CFG (tier C / weak)."""
        raise NotImplementedError

    @abstractmethod
    def get_mem_accesses(self, func: IRFunction) -> List[MemAccess]:
        """Extract memory access sites (load/store/memcpy/etc.) for one
        IR function. May return [] if the adapter doesn't expose this."""
        raise NotImplementedError

    @abstractmethod
    def get_call_edges(self, module: IRModule) -> List[CallEdge]:
        """Extract all call edges (direct + indirect) from the module.
        Indirect edges should populate `possible_targets` and `analysis`."""
        raise NotImplementedError

    @abstractmethod
    def get_alias_sets(self, scope: IRFunction) -> List[AliasSet]:
        """Compute alias sets for one function scope. May return [] if
        the adapter doesn't have a real alias analysis (tier C / heuristic)."""
        raise NotImplementedError

    @abstractmethod
    def get_data_deps(self, func: IRFunction) -> List[DataDep]:
        """Compute SSA-level data dependencies for one function."""
        raise NotImplementedError

    @abstractmethod
    def align_to_ast(self, ir_node: Any) -> Optional[int]:
        """Align an IR node (SSA value / mem_access / call_site) to its
        corresponding AST node id (cgdb_nodes.id), via debug metadata
        (`!DILocation`, `!DILocalVariable`, `!DISubprogram` for LLVM).

        Returns the AST node id, or None if alignment failed.
        """
        raise NotImplementedError

    # --- Optional hooks (default implementations) ---

    def get_path_states(self, func: IRFunction) -> List[PathState]:
        """Path-sensitive analysis states. Default: empty (no path-sensitive
        analysis). Subclasses (LLVM with Clang Static Analyzer, JVM with
        Spark, etc.) should override."""
        return []

    def is_available(self) -> bool:
        """Check whether the external toolchain is installed. Default: True
        (the adapter itself doesn't require external tools)."""
        return True

    def coverage_summary(self) -> Dict[str, Any]:
        """Return a summary of what this adapter provides."""
        return {
            "language": self.language,
            "tier": self.tier,
            "ir_kind": self.ir_kind,
            "coverage_level": self.coverage_level,
            "requires_external_toolchain": self.requires_external_toolchain,
            "external_toolchain_name": self.external_toolchain_name,
            "is_available": self.is_available(),
        }


# ============================================================================
# Concrete adapter stubs (one per language tier)
# ============================================================================

class _NoIRAdapter(IRAdapter):
    """Fallback adapter for languages with no IR (Lua, Shell).

    Returns empty IRModule. The cgdb L4/L5 (CFG/data_flow) tables will be
    populated from AST walk + regex heuristics (via base._emit_cfg /
    base._emit_cgdb_records) instead.
    """

    language = "none"
    tier = "C"
    ir_kind = "none"
    coverage_level = "L1+L2"

    def parse(self, source_or_bytecode: str) -> IRModule:
        return IRModule(
            name=os.path.basename(source_or_bytecode) if source_or_bytecode else "unknown",
            language=self.language,
            ir_kind=self.ir_kind,
            source_path=source_or_bytecode,
        )

    def get_functions(self, module: IRModule) -> List[IRFunction]:
        return []

    def get_cfg(self, func: IRFunction) -> CFG:
        return CFG(function_ir_name=func.ir_name)

    def get_ssa_values(self, func: IRFunction) -> List[SSAValue]:
        return []

    def get_mem_accesses(self, func: IRFunction) -> List[MemAccess]:
        return []

    def get_call_edges(self, module: IRModule) -> List[CallEdge]:
        return []

    def get_alias_sets(self, scope: IRFunction) -> List[AliasSet]:
        return []

    def get_data_deps(self, func: IRFunction) -> List[DataDep]:
        return []

    def align_to_ast(self, ir_node: Any) -> Optional[int]:
        return None


class LLVMIRAdapter(IRAdapter):
    """LLVM IR adapter — tier A (C/C++/ObjC/Rust/Swift/Zig/CUDA).

    Delegates to `clang -emit-llvm -g` for IR generation and (optionally)
    SVF for pointer alias analysis. Falls back to libclang AST walk when
    clang/SVF are not installed (returns empty SSA / alias_sets).
    """

    language = "c"  # subclassed for cpp/rust/swift/zig/cuda
    tier = "A"
    ir_kind = "llvm-ir"
    coverage_level = "L1+L2+L3"
    requires_external_toolchain = True
    external_toolchain_name = "llvm"

    def __init__(self, language: str = "c"):
        self.language = language

    def is_available(self) -> bool:
        """Check if clang is installed and supports -emit-llvm."""
        try:
            proc = subprocess.run(
                ["clang", "--version"],
                capture_output=True, timeout=5
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def parse(self, source_or_bytecode: str) -> IRModule:
        """Parse a source file or .bc/.ll bitcode → IRModule.

        For source files: runs `clang -emit-llvm -g -S -o - <file>` to
        produce LLVM IR text, then parses it (very minimal parse — just
        counts functions/blocks/instructions, no full IR walk).
        """
        if not self.is_available():
            return IRModule(
                name=os.path.basename(source_or_bytecode),
                language=self.language,
                ir_kind=self.ir_kind,
                source_path=source_or_bytecode,
                debug_metadata_present=False,
            )
        # Run clang -emit-llvm -g
        ext = os.path.splitext(source_or_bytecode)[1].lower()
        if ext in (".bc", ".ll"):
            # Already bitcode/IR text — use llvm-dis to get text
            try:
                proc = subprocess.run(
                    ["llvm-dis", "-o", "-", source_or_bytecode],
                    capture_output=True, timeout=60
                )
                ir_text = proc.stdout.decode("utf-8", errors="replace")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                ir_text = ""
        else:
            try:
                # Map language to clang -x flag
                lang_x_flag = {
                    "c": "c",
                    "cpp": "c++",
                    "cuda": "cuda",
                    "rust": "rust",  # requires rustc --emit=llvm-bitcode instead
                    "swift": "swift",  # requires swiftc -emit-llvm
                    "zig": "zig",  # requires zig ast-check + LLVM IR
                }.get(self.language, "c")
                cmd = ["clang", "-emit-llvm", "-g", "-S",
                       "-x", lang_x_flag, "-o", "-", source_or_bytecode]
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=120
                )
                ir_text = proc.stdout.decode("utf-8", errors="replace")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                ir_text = ""
        module = IRModule(
            name=os.path.basename(source_or_bytecode),
            language=self.language,
            ir_kind=self.ir_kind,
            source_path=source_or_bytecode,
            debug_metadata_present="!DILocation" in ir_text or "!DISubprogram" in ir_text,
        )
        # Minimal parse: count define declarations
        for line in ir_text.splitlines():
            if line.startswith("define "):
                # Extract function name
                parts = line.split("@", 1)
                if len(parts) > 1:
                    fn_name = parts[1].split("(")[0].strip()
                    module.functions.append(IRFunction(
                        ir_name=fn_name,
                        debug_metadata_present=module.debug_metadata_present,
                    ))
        return module

    def get_functions(self, module: IRModule) -> List[IRFunction]:
        return module.functions

    def get_cfg(self, func: IRFunction) -> CFG:
        # Without a real LLVM Pass, we can't extract CFG from IR text
        # reliably. Return empty CFG — the caller should fall back to
        # the cgdb basic_blocks/cfg_edges tables (which are populated
        # from libclang AST walk via cgdb_analysis.CFGExtractor).
        return CFG(function_ir_name=func.ir_name)

    def get_ssa_values(self, func: IRFunction) -> List[SSAValue]:
        # SSA extraction requires LLVM Pass. Return empty — caller falls
        # back to cgdb data_flow table (which is def-use chain, not SSA).
        return []

    def get_mem_accesses(self, func: IRFunction) -> List[MemAccess]:
        # Mem access extraction requires LLVM Pass. Return empty.
        return []

    def get_call_edges(self, module: IRModule) -> List[CallEdge]:
        # Direct call edges can be extracted from IR text via `call @fn`
        # regex. Indirect calls require SVF.
        edges = []
        return edges

    def get_alias_sets(self, scope: IRFunction) -> List[AliasSet]:
        # SVF integration would go here. Return empty — caller falls
        # back to cgdb alias_sets table (which is heuristic stub).
        return []

    def get_data_deps(self, func: IRFunction) -> List[DataDep]:
        # LLVM Pass for data deps. Return empty.
        return []

    def align_to_ast(self, ir_node: Any) -> Optional[int]:
        # Alignment via !DILocation/!DILocalVariable/!DISubprogram.
        # Requires parsing debug metadata. Stub: return None.
        return None


class JimpleIRAdapter(IRAdapter):
    """Jimple IR adapter — tier B (Java/Kotlin/Scala).

    Delegates to Soot for Jimple IR + Spark pointer analysis.
    """

    language = "java"
    tier = "B"
    ir_kind = "jimple"
    coverage_level = "L1+L2+L3"
    requires_external_toolchain = True
    external_toolchain_name = "soot"

    def is_available(self) -> bool:
        try:
            # Check if java is installed (Soot requires JVM)
            proc = subprocess.run(["java", "-version"], capture_output=True, timeout=5)
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def parse(self, source_or_bytecode: str) -> IRModule:
        # Soot integration would go here. Stub.
        return IRModule(
            name=os.path.basename(source_or_bytecode),
            language=self.language,
            ir_kind=self.ir_kind,
            source_path=source_or_bytecode,
        )

    def get_functions(self, module: IRModule) -> List[IRFunction]:
        return []

    def get_cfg(self, func: IRFunction) -> CFG:
        return CFG(function_ir_name=func.ir_name)

    def get_ssa_values(self, func: IRFunction) -> List[SSAValue]:
        return []

    def get_mem_accesses(self, func: IRFunction) -> List[MemAccess]:
        return []

    def get_call_edges(self, module: IRModule) -> List[CallEdge]:
        return []

    def get_alias_sets(self, scope: IRFunction) -> List[AliasSet]:
        # Spark pointer analysis would go here. Stub.
        return []

    def get_data_deps(self, func: IRFunction) -> List[DataDep]:
        return []

    def align_to_ast(self, ir_node: Any) -> Optional[int]:
        # JVM bytecode LineNumberTable alignment.
        return None


class MSILIRAdapter(IRAdapter):
    """MSIL (CIL) adapter — tier B (C#/F#/VB.NET).

    Delegates to Roslyn for AST + ILReaper for MSIL.
    """

    language = "csharp"
    tier = "B"
    ir_kind = "msil"
    coverage_level = "L1+L2+L3"
    requires_external_toolchain = True
    external_toolchain_name = "roslyn"

    def is_available(self) -> bool:
        try:
            proc = subprocess.run(["dotnet", "--version"], capture_output=True, timeout=5)
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def parse(self, source_or_bytecode: str) -> IRModule:
        return IRModule(
            name=os.path.basename(source_or_bytecode),
            language=self.language,
            ir_kind=self.ir_kind,
            source_path=source_or_bytecode,
        )

    def get_functions(self, module: IRModule) -> List[IRFunction]:
        return []

    def get_cfg(self, func: IRFunction) -> CFG:
        return CFG(function_ir_name=func.ir_name)

    def get_ssa_values(self, func: IRFunction) -> List[SSAValue]:
        return []

    def get_mem_accesses(self, func: IRFunction) -> List[MemAccess]:
        return []

    def get_call_edges(self, module: IRModule) -> List[CallEdge]:
        return []

    def get_alias_sets(self, scope: IRFunction) -> List[AliasSet]:
        return []

    def get_data_deps(self, func: IRFunction) -> List[DataDep]:
        return []

    def align_to_ast(self, ir_node: Any) -> Optional[int]:
        # PDB alignment.
        return None


class GoSSAAdapter(IRAdapter):
    """Go SSA adapter — tier B (Go).

    Delegates to golang.org/x/tools/go/ssa for SSA + go/pointer for alias.
    """

    language = "go"
    tier = "B"
    ir_kind = "go-ssa"
    coverage_level = "L1+L2+L3"
    requires_external_toolchain = True
    external_toolchain_name = "go-ssa"

    def is_available(self) -> bool:
        try:
            proc = subprocess.run(["go", "version"], capture_output=True, timeout=5)
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def parse(self, source_or_bytecode: str) -> IRModule:
        return IRModule(
            name=os.path.basename(source_or_bytecode),
            language=self.language,
            ir_kind=self.ir_kind,
            source_path=source_or_bytecode,
        )

    def get_functions(self, module: IRModule) -> List[IRFunction]:
        return []

    def get_cfg(self, func: IRFunction) -> CFG:
        return CFG(function_ir_name=func.ir_name)

    def get_ssa_values(self, func: IRFunction) -> List[SSAValue]:
        return []

    def get_mem_accesses(self, func: IRFunction) -> List[MemAccess]:
        return []

    def get_call_edges(self, module: IRModule) -> List[CallEdge]:
        return []

    def get_alias_sets(self, scope: IRFunction) -> List[AliasSet]:
        return []

    def get_data_deps(self, func: IRFunction) -> List[DataDep]:
        return []

    def align_to_ast(self, ir_node: Any) -> Optional[int]:
        # Go SSA Pos field alignment.
        return None


class PythonBytecodeAdapter(IRAdapter):
    """Python bytecode adapter — tier C (Python, weak).

    Uses CPython bytecode disassembly for runtime-observed types/calls.
    Static type inference via Pytype/Pyright is the primary L3 source.
    """

    language = "python"
    tier = "C"
    ir_kind = "python-bytecode"
    coverage_level = "L1+L2+L3-weak"
    requires_external_toolchain = False
    external_toolchain_name = None

    def is_available(self) -> bool:
        # Python is always available (we're running in it).
        return True

    def parse(self, source_or_bytecode: str) -> IRModule:
        # For Python source, use ast + dis modules to extract function
        # signatures and bytecode (when source is importable).
        return IRModule(
            name=os.path.basename(source_or_bytecode),
            language=self.language,
            ir_kind=self.ir_kind,
            source_path=source_or_bytecode,
        )

    def get_functions(self, module: IRModule) -> List[IRFunction]:
        return []

    def get_cfg(self, func: IRFunction) -> CFG:
        # Python bytecode has no real CFG — fall back to AST-walk CFG.
        return CFG(function_ir_name=func.ir_name)

    def get_ssa_values(self, func: IRFunction) -> List[SSAValue]:
        # No SSA in Python bytecode. Use Pytype/Pyright for type inference.
        return []

    def get_mem_accesses(self, func: IRFunction) -> List[MemAccess]:
        return []

    def get_call_edges(self, module: IRModule) -> List[CallEdge]:
        return []

    def get_alias_sets(self, scope: IRFunction) -> List[AliasSet]:
        # Type inference (Pytype/Pyright) for static alias approximation.
        return []

    def get_data_deps(self, func: IRFunction) -> List[DataDep]:
        return []

    def align_to_ast(self, ir_node: Any) -> Optional[int]:
        return None


class TSCompilerAdapter(IRAdapter):
    """TypeScript Compiler adapter — tier C-strong (TS/JS).

    Uses tsserver for type information + static analysis. Between B/C tier.
    """

    language = "typescript"
    tier = "C"
    ir_kind = "ts-compiler"
    coverage_level = "L1+L2+L3-weak"
    requires_external_toolchain = True
    external_toolchain_name = "tsserver"

    def is_available(self) -> bool:
        try:
            proc = subprocess.run(["tsc", "--version"], capture_output=True, timeout=5)
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def parse(self, source_or_bytecode: str) -> IRModule:
        return IRModule(
            name=os.path.basename(source_or_bytecode),
            language=self.language,
            ir_kind=self.ir_kind,
            source_path=source_or_bytecode,
        )

    def get_functions(self, module: IRModule) -> List[IRFunction]:
        return []

    def get_cfg(self, func: IRFunction) -> CFG:
        return CFG(function_ir_name=func.ir_name)

    def get_ssa_values(self, func: IRFunction) -> List[SSAValue]:
        return []

    def get_mem_accesses(self, func: IRFunction) -> List[MemAccess]:
        return []

    def get_call_edges(self, module: IRModule) -> List[CallEdge]:
        return []

    def get_alias_sets(self, scope: IRFunction) -> List[AliasSet]:
        return []

    def get_data_deps(self, func: IRFunction) -> List[DataDep]:
        return []

    def align_to_ast(self, ir_node: Any) -> Optional[int]:
        return None


# ============================================================================
# Registry / factory
# ============================================================================

_ADAPTER_REGISTRY: Dict[str, type] = {
    "c": LLVMIRAdapter,
    "cpp": LLVMIRAdapter,
    "rust": LLVMIRAdapter,
    "swift": LLVMIRAdapter,
    "zig": LLVMIRAdapter,
    "cuda": LLVMIRAdapter,
    "java": JimpleIRAdapter,
    "kotlin": JimpleIRAdapter,
    "scala": JimpleIRAdapter,
    "csharp": MSILIRAdapter,
    "fsharp": MSILIRAdapter,
    "vbnet": MSILIRAdapter,
    "go": GoSSAAdapter,
    "python": PythonBytecodeAdapter,
    "typescript": TSCompilerAdapter,
    "javascript": TSCompilerAdapter,
    "lua": _NoIRAdapter,
    "shell": _NoIRAdapter,
}


def get_ir_adapter(language: str) -> Optional[IRAdapter]:
    """Get an IR adapter instance for the given language.

    Returns None if no adapter is registered for the language.
    """
    cls = _ADAPTER_REGISTRY.get(language.lower())
    if cls is None:
        return None
    # LLVMIRAdapter takes a language arg
    if cls is LLVMIRAdapter:
        return cls(language=language)
    return cls()


def list_supported_languages() -> List[Dict[str, Any]]:
    """List all supported languages and their adapter coverage."""
    seen = set()
    result = []
    for lang, cls in _ADAPTER_REGISTRY.items():
        if lang in seen:
            continue
        seen.add(lang)
        # Instantiate to get coverage summary
        if cls is LLVMIRAdapter:
            inst = cls(language=lang)
        else:
            inst = cls()
        result.append({
            "language": lang,
            "tier": inst.tier,
            "ir_kind": inst.ir_kind,
            "coverage_level": inst.coverage_level,
            "requires_external_toolchain": inst.requires_external_toolchain,
            "external_toolchain_name": inst.external_toolchain_name,
        })
    return result


def register_adapter(language: str, adapter_cls: type) -> None:
    """Register a custom adapter for a language."""
    _ADAPTER_REGISTRY[language.lower()] = adapter_cls
