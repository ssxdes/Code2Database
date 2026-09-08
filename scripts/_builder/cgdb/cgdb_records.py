"""cgdb record dataclasses — IngestBatch transport between parser and storage.

Per cdb-architecture-and-poc-report.md 5.4.4: IngestBatch is the unit of
transport from parser (clang scanner) to storage (CGDBWriter). Each batch
covers one translation unit and carries 13-layer records.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class NodeRecord:
    """L1: a multi-kind first-class node (function/var/field/type/stmt/expr)."""
    id: int                              # USR-based hash (see cgdb_ingest.node_id)
    kind: str                            # 'function' | 'var' | 'field' | ...
    name: str                            # short name
    fqn: str                             # fully qualified name (USR or hash)
    file_id: Optional[int] = None
    line: int = 0
    col: int = 0
    byte_start: int = 0
    byte_end: int = 0
    type_spelling: str = ""
    type_id: Optional[int] = None
    config_predicate_id: Optional[int] = None
    # Per cgdb-architecture doc 5.4.2: enclosing FunctionDecl's node_id, via
    # cursor.semantic_parent walk. 0 = file/TU scope.
    enclosing_symbol_id: Optional[int] = None
    attrs: Dict[str, Any] = field(default_factory=dict)
    source_layer: str = "ast"
    confidence: float = 1.0
    first_seen_version: int = 1
    last_seen_version: int = 1
    commit_hash: Optional[str] = None
    legacy_function_id: Optional[str] = None
    source_snippet: str = ""


@dataclass
class TypeRecord:
    """L2: a type record (independent type system)."""
    id: int
    spelling: str
    canonical_spelling: str
    kind: str                            # 'builtin'|'pointer'|'record'|...
    size_bytes: Optional[int] = None
    alignment: Optional[int] = None
    is_const: bool = False
    is_volatile: bool = False
    pointee_type_id: Optional[int] = None
    element_type_id: Optional[int] = None
    record_id: Optional[int] = None
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EdgeRecord:
    """L1: a semantic edge between two nodes."""
    src_id: int
    dst_id: int
    kind: str                            # 'INVOKES'|'OPS_BIND'|'READS'|...
    file_id: Optional[int] = None
    line: Optional[int] = None
    col: Optional[int] = None
    byte_start: Optional[int] = None
    byte_end: Optional[int] = None
    condition_id: Optional[int] = None
    config_predicate_id: Optional[int] = None
    # Enclosing function node_id — same as NodeRecord.enclosing_symbol_id.
    enclosing_symbol_id: Optional[int] = None
    attrs: Dict[str, Any] = field(default_factory=dict)
    source_layer: str = "ast"
    confidence: float = 1.0
    first_seen_version: int = 1
    last_seen_version: int = 1
    commit_hash: Optional[str] = None
    edge_id: Optional[int] = None        # explicit ID (overrides AUTOINCREMENT)


@dataclass
class ConfigPredicateRecord:
    """L3.5: a #ifdef predicate tree (BDD + Z3 + text form)."""
    id: int
    root_expr_id: Optional[int] = None
    text_form: str = ""                  # '(defined(CONFIG_X) && !defined(CONFIG_Y))'
    z3_form: str = ""                    # SMT-LIB form for Z3
    bdd_serialized: str = ""             # JSON-serialized BDD
    config_macros: List[str] = field(default_factory=list)
    is_unconditional: bool = False
    is_contradictory: bool = False


@dataclass
class ConditionRecord:
    """L3: a boolean expression tree node (for CFG branch conditions)."""
    id: int
    root_expr_id: Optional[int] = None
    kind: str = "atom"                   # 'comparison'|'logical'|'unary'|'atom'|'macro_call'
    operator: str = ""
    left_expr_id: Optional[int] = None
    right_expr_id: Optional[int] = None
    text_form: str = ""
    z3_form: str = ""
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BasicBlockRecord:
    """L4: a basic block in a function's CFG."""
    id: int
    function_id: int
    block_index: int
    is_entry: bool = False
    is_exit: bool = False
    stmt_ids: List[int] = field(default_factory=list)
    byte_start: Optional[int] = None
    byte_end: Optional[int] = None


@dataclass
class CFGEdgeRecord:
    """L4: an edge between basic blocks."""
    function_id: int
    src_block_id: int
    dst_block_id: int
    kind: str                            # 'fallthrough'|'true_branch'|'false_branch'|'exception'
    condition_id: Optional[int] = None


@dataclass
class DataFlowRecord:
    """L5: a def-use chain entry."""
    function_id: int
    var_id: int
    def_block_id: Optional[int] = None
    def_stmt_id: Optional[int] = None
    use_block_id: Optional[int] = None
    use_stmt_id: Optional[int] = None
    kind: str = "def"                    # 'def'|'use'|'def_use'
    path_condition_ids: List[int] = field(default_factory=list)


@dataclass
class AliasSetRecord:
    """L6: a pointer alias relationship."""
    ptr1_node_id: int
    ptr2_node_id: int
    kind: str = "may_alias"              # 'must_alias'|'may_alias'|'no_alias'
    confidence: float = 1.0


@dataclass
class InvokeSiteRecord:
    """L7: an invoke site with arg bindings."""
    invoker_id: int
    invoked_id: int
    invoke_expr_id: Optional[int] = None
    arg_bindings: List[Dict[str, Any]] = field(default_factory=list)
    invoke_kind: str = "direct"          # 'direct'|'ops_bind'|'virtual'|'function_pointer'|'template_instantiation'
    dispatch_candidates: List[int] = field(default_factory=list)


@dataclass
class OpsBindingRecord:
    """L7: a typed vtable dispatch binding (.field = function)."""
    edge_id: int                         # references cgdb_edges.id
    ops_table_id: int
    field_node_id: int
    impl_function_id: int
    signature_match: bool = True


@dataclass
class SyncPrimitiveRecord:
    """L8: a synchronization primitive operation.

    Field semantics depend on `kind`:
      - lock_acquire:   acquire_stmt_id = the CALL_EXPR that acquires
      - lock_release:   release_stmt_id = the CALL_EXPR that releases
      - write_once / atomic_store: acquire_stmt_id is REPURPOSED as the
        write-event id (no schema column for it — documented convention)
      - read_once / atomic_load: release_stmt_id is REPURPOSED as the
        read-event id
      - barrier: acquire_stmt_id = the barrier call stmt

    Consumers MUST check `kind` before interpreting acquire/release_stmt_id.
    """
    function_id: int
    kind: str                            # 'lock_acquire'|'lock_release'|'write_once'|'read_once'|'barrier'|...
    sync_var_id: Optional[int] = None
    acquire_stmt_id: Optional[int] = None
    release_stmt_id: Optional[int] = None
    memory_order: str = ""


@dataclass
class HappensBeforeRecord:
    """L8: a happens-before relationship between write and read events."""
    write_event_id: int
    read_event_id: int
    reason: str = "program_order"        # 'lock'|'rcu'|'atomic'|'memory_barrier'|'program_order'
    confidence: float = 1.0


@dataclass
class FileRecord:
    """L9: a source file."""
    id: int
    path: str
    is_system: bool = False
    language: str = "c"
    sha256: str = ""
    line_count: Optional[int] = None
    byte_count: Optional[int] = None
    commit_hash: Optional[str] = None
    last_modified: Optional[int] = None
    content_hash: str = ""


@dataclass
class IncludeRecord:
    """L9: an #include relationship."""
    source_file_id: int
    included_file_id: Optional[int] = None
    included_path: str = ""
    is_system: bool = False


@dataclass
class DocCommentRecord:
    """L10: a doc comment attached to a node.

    Captures raw comment text, comment kind (Doxygen-block, JavaDoc, line,
    block), and a parsed tags dict (e.g., `@param`, `@return`, `@note`).
    The doc-code alignment pipeline (doc_code_align.py) compares these
    against the actual node signature/body to detect stale docs.
    """
    node_id: int                           # the node this comment is attached to
    file_id: Optional[int] = None
    line: int = 0
    col: int = 0
    comment_kind: str = ""                 # 'doxygen_block' | 'javadoc' | 'line' | 'block'
    raw_text: str = ""                     # the comment text including markers
    cleaned_text: str = ""                 # markers stripped, leading * removed
    tags: Dict[str, Any] = field(default_factory=dict)  # {'param': [...], 'return': ..., 'note': ...}
    byte_start: int = 0
    byte_end: int = 0


@dataclass
class MetadataRecord:
    """L11: arbitrary key-value metadata attached to a node or edge.

    Used for tool-emitted auxiliary information that doesn't fit the
    typed schema: scanner version, language version, profile name,
    processing timestamps, original AST cursor kind, etc.
    """
    target_id: int                         # node_id or edge_id this metadata is attached to
    target_kind: str = "node"              # 'node' | 'edge' | 'file' | 'type'
    key: str = ""                          # metadata key (e.g., 'scanner_version')
    value: str = ""                        # metadata value (string)
    value_type: str = "str"                # 'str' | 'int' | 'float' | 'bool' | 'json'
    source: str = ""                       # 'scanner' | 'builder' | 'llm' | 'manual'


@dataclass
class IngestBatch:
    """13-layer batch: parser → storage transport unit (per cdb 5.4.4).

    One batch per translation unit. Carries records for all 13 layers;
    layers without data are empty lists.
    """
    file: Optional[FileRecord] = None
    tu_id: int = 0
    # L1
    nodes: List[NodeRecord] = field(default_factory=list)
    edges: List[EdgeRecord] = field(default_factory=list)
    # L2
    types: List[TypeRecord] = field(default_factory=list)
    # L3
    conditions: List[ConditionRecord] = field(default_factory=list)
    # L3.5
    config_predicates: List[ConfigPredicateRecord] = field(default_factory=list)
    # L4
    basic_blocks: List[BasicBlockRecord] = field(default_factory=list)
    cfg_edges: List[CFGEdgeRecord] = field(default_factory=list)
    # L5
    data_flow: List[DataFlowRecord] = field(default_factory=list)
    # L6
    alias_sets: List[AliasSetRecord] = field(default_factory=list)
    # L7
    invoke_sites: List[InvokeSiteRecord] = field(default_factory=list)
    ops_bindings: List[OpsBindingRecord] = field(default_factory=list)
    # L8
    sync_primitives: List[SyncPrimitiveRecord] = field(default_factory=list)
    happens_before: List[HappensBeforeRecord] = field(default_factory=list)
    # L9
    includes: List[IncludeRecord] = field(default_factory=list)
    # L10
    doc_comments: List[DocCommentRecord] = field(default_factory=list)
    # L11
    metadata: List[MetadataRecord] = field(default_factory=list)

    def record_count(self) -> int:
        """Total number of records across all layers (for logging)."""
        return (len(self.nodes) + len(self.edges) + len(self.types)
                + len(self.conditions) + len(self.config_predicates)
                + len(self.basic_blocks) + len(self.cfg_edges)
                + len(self.data_flow) + len(self.alias_sets)
                + len(self.invoke_sites) + len(self.ops_bindings)
                + len(self.sync_primitives) + len(self.happens_before)
                + len(self.includes) + len(self.doc_comments)
                + len(self.metadata) + (1 if self.file else 0))
