#!/usr/bin/env python3
"""Knowledge manager for Code2Database.

Knowledge stores PRINCIPLED, INVARIANT information that cannot be represented
in the invocation graph or memory — e.g., protocol standards (RDMA flow), hardware
constraints, algorithmic principles, domain-specific rules that don't change
when code changes. This is distinct from memory (LLM's working state) and
from the graph (function relationships).

Knowledge must be human-readable (Markdown) first, LLM-readable via packs second.
It is curated by humans/LLMs, not auto-generated noise.

Directory: code2db-out/knowledge/
- principles.md: Domain principles, protocol flows, invariant rules
- architecture.md: Project architecture overview (graph-inferred)
- constraints.md: API constraints, configuration rules
- glossary.md: Terminology
- custom_*.md: User/LLM-defined knowledge topics

Knowledge packs (lite/standard/full) provide tiered LLM consumption.

Can be used standalone or via code2database_builder.py commands:
  extract-knowledge, apply-knowledge, knowledge-query
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import logging


class KnowledgeManager:
    """Manages structured project knowledge extracted from docs and graph."""

    def __init__(self, graph_dir: str):
        self.graph_dir = graph_dir
        self.knowledge_dir = os.path.join(graph_dir, "knowledge")
        os.makedirs(self.knowledge_dir, exist_ok=True)

    # -----------------------------------------------------------------------
    # Document scanning
    # -----------------------------------------------------------------------

    def extract_from_docs(self, doc_paths: list) -> dict:
        """Scan documentation files and extract knowledge templates.

        Returns a dict with keys: architecture, modules, constraints,
        glossary, patterns, build_rules — each containing raw text.
        """
        knowledge = {
            "architecture": "",
            "modules": {},
            "constraints": "",
            "glossary": "",
            "patterns": "",
            "build_rules": "",
        }

        for doc_path in doc_paths:
            if not os.path.exists(doc_path):
                continue
            text = Path(doc_path).read_text(encoding="utf-8", errors="replace")
            fname = os.path.basename(doc_path).lower()

            if fname in ("readme.md", "readme.rst", "readme.txt"):
                knowledge["architecture"] = text
            elif fname in ("design.md", "architecture.md", "design.rst"):
                knowledge["architecture"] = text
            elif "contributing" in fname:
                knowledge["constraints"] += f"\n--- From {doc_path} ---\n{text}"
            elif "glossary" in fname:
                knowledge["glossary"] = text
            elif "pattern" in fname or "idiom" in fname:
                knowledge["patterns"] = text
            elif "build" in fname or "cmake" in fname or "makefile" in fname:
                knowledge["build_rules"] += f"\n--- From {doc_path} ---\n{text}"
            else:
                # Generic doc → add to architecture if it looks like overview
                if len(text) > 200 and ("overview" in text[:500].lower() or
                                         "architecture" in text[:500].lower()):
                    knowledge["architecture"] += f"\n--- From {doc_path} ---\n{text}"

        return knowledge

    def _extract_structured_sections(self, text: str, source_path: str) -> dict:
        """Parse Markdown headings and extract named sections.

        Only extracts sections with significant content (>200 chars) and
        only from top-level headings (##, not ###) to avoid noise.
        Returns dict of {sanitized_title: content}.
        """
        sections = {}
        current_title = None
        current_lines = []

        for line in text.split("\n"):
            # Only match ## (not # or ###) to limit scope
            heading_match = re.match(r'^##\s+(.+)$', line)
            if heading_match:
                # Save previous section
                if current_title and current_lines:
                    content = "\n".join(current_lines).strip()
                    # Only save sections with significant content
                    if len(content) > 200:
                        safe = re.sub(r'[^a-zA-Z0-9_]', '_', current_title).strip('_')[:60]
                        sections[safe] = f"# {current_title}\n\n{content}\n\n*Source: {source_path}*\n"
                current_title = heading_match.group(1).strip()
                current_lines = []
            else:
                if current_title:
                    current_lines.append(line)

        # Save last section
        if current_title and current_lines:
            content = "\n".join(current_lines).strip()
            if len(content) > 200:
                safe = re.sub(r'[^a-zA-Z0-9_]', '_', current_title).strip('_')[:60]
                sections[safe] = f"# {current_title}\n\n{content}\n\n*Source: {source_path}*\n"

        return sections

    def extract_from_source_patterns(self, source_root: str, graph_dir: str) -> dict:
        """Extract structural patterns from source code that don't fit in graph nodes.

        No LLM needed — pure script-based extraction.
        Returns dict of {detail_filename: content_markdown}.
        """
        details = {}
        macros = []
        structs = []
        error_codes = []
        callback_regs = []

        # Walk source files
        ext_map = {".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
                   ".go": "go", ".py": "python", ".java": "java", ".rs": "rust"}

        for dirpath, dirnames, filenames in os.walk(source_root):
            dirnames[:] = [d for d in dirnames
                          if not d.startswith('.') and d not in ("__pycache__", "node_modules", "build")]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                lang = ext_map.get(ext, "")
                if not lang:
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    text = Path(fpath).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    logging.getLogger(__name__).debug("silent exception", exc_info=True)
                    continue
                rel = os.path.relpath(fpath, source_root)

                # Extract #define macros/constants
                for m in re.finditer(r'^\s*#\s*define\s+([A-Z_][A-Z0-9_]*)\s+(.+?)$',
                                     text, re.MULTILINE):
                    name, value = m.group(1), m.group(2).strip()
                    if name.startswith('_') or len(name) < 3:
                        continue
                    macros.append(f"- `{name}` = `{value}` — *{rel}*")

                # Extract struct/typedef definitions (C/C++)
                if lang in ("c", "cpp"):
                    for m in re.finditer(
                        r'(?:typedef\s+)?struct\s+(\w+)\s*\{([^}]{0,500})\}',
                        text, re.DOTALL):
                        sname = m.group(1)
                        body = m.group(2).strip()
                        if sname.startswith('_'):
                            continue
                        fields = [l.strip() for l in body.split("\n") if l.strip() and not l.strip().startswith("//")]
                        structs.append(f"- **`{sname}`** ({len(fields)} fields) — *{rel}*\n  ```\n  " +
                                      "\n  ".join(fields[:10]) + "\n  ```")

                    # Extract enum error codes
                    for m in re.finditer(r'enum\s+(\w+)\s*\{([^}]{0,500})\}',
                                         text, re.DOTALL):
                        ename = m.group(1)
                        body = m.group(2).strip()
                        entries = [l.strip().rstrip(',').split('=')[0].strip()
                                  for l in body.split("\n")
                                  if l.strip() and not l.strip().startswith("//")]
                        if any('ERR' in e.upper() or 'ERROR' in e.upper() or e.endswith('_FAIL')
                              for e in entries):
                            error_codes.append(f"- **`{ename}`** — *{rel}*\n  " +
                                             ", ".join(f"`{e}`" for e in entries[:15]))

                # Extract callback registration patterns
                for m in re.finditer(
                    r'(\w+)\s*=\s*(\w+)_cb[^;]*;|'
                    r'register_(\w+)_callback\s*\(|'
                    r'(\w+)_register_cb\s*\(|'
                    r'\.(\w+)_cb\s*=\s*(\w+)|'
                    r'pthread_create\s*\([^,]+,\s*[^,]+,\s*\*?(\w+)',
                    text):
                    groups = [g for g in m.groups() if g]
                    if groups:
                        callback_regs.append(f"- `{'`, `'.join(groups)}` — *{rel}*")

        if macros:
            details["detail_macros_constants.md"] = (
                "# Macros & Constants\n\n"
                "Auto-extracted from source code.\n\n" +
                "\n".join(macros[:200]) + "\n")
        if structs:
            details["detail_data_structures.md"] = (
                "# Data Structures\n\n"
                "Auto-extracted from source code.\n\n" +
                "\n".join(structs[:100]) + "\n")
        if error_codes:
            details["detail_error_codes.md"] = (
                "# Error Codes\n\n"
                "Auto-extracted enum definitions containing error values.\n\n" +
                "\n".join(error_codes[:50]) + "\n")
        if callback_regs:
            details["detail_callback_registrations.md"] = (
                "# Callback Registrations\n\n"
                "Auto-extracted callback registration patterns.\n\n" +
                "\n".join(callback_regs[:100]) + "\n")

        # Cap total detail files at 50 to avoid knowledge directory bloat
        if len(details) > 50:
            # Keep the most important ones (source patterns + largest doc sections)
            priority = [k for k in details if k.startswith("detail_macros")
                       or k.startswith("detail_data_struct")
                       or k.startswith("detail_error")
                       or k.startswith("detail_callback")]
            remaining = sorted([k for k in details if k not in priority],
                              key=lambda k: len(details[k]), reverse=True)
            keep = priority + remaining[:50 - len(priority)]
            details = {k: v for k, v in details.items() if k in keep}

        return details

    def extract_stale_node_knowledge(self, stale_nodes: list) -> int:
        """Save semantic descriptions from stale nodes before they are cleared.

        Called by patcher before clearing semantic_desc on stale nodes.
        Returns the number of entries saved.
        """
        if not stale_nodes:
            return 0

        lines = ["# Stale Node Semantics\n\n"]
        lines.append("Semantic descriptions preserved from nodes marked stale "
                     "during graph update.\n\n")

        saved = 0
        for node in stale_nodes:
            name = node.get("name", node.get("id", ""))
            desc = node.get("semantic_desc", "")
            if not desc:
                continue
            source = node.get("source_file", "")
            line_num = node.get("line", 0)
            lines.append(f"## `{name}`\n")
            lines.append(f"- Source: `{source}:{line_num}`")
            lines.append(f"- Description: {desc}\n")
            saved += 1

        if saved > 0:
            path = os.path.join(self.knowledge_dir, "detail_stale_semantics.md")
            # Append if file exists (don't overwrite previous stale semantics)
            existing = ""
            if os.path.exists(path):
                existing = Path(path).read_text(encoding="utf-8", errors="replace")
            Path(path).write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")

        return saved

    # -----------------------------------------------------------------------
    # Graph-based inference
    # -----------------------------------------------------------------------

    def infer_from_graph(self, graph_dir: str) -> dict:
        """Infer knowledge from graph structure (communities, domains, etc.).

        IMPORTANT: Does NOT generate per-module files. Module-level function
        listings are noise — they duplicate what's already in the graph.
        Only generates architecture overview and glossary as template for
        LLM curation.
        """
        from _builder.graph_build import _load_full_graph

        knowledge = {
            "architecture": "",
            "modules": {},  # Kept empty — no auto-generated module files
            "constraints": "",
            "glossary": "",
            "patterns": "",
            "build_rules": "",
            "_source": "auto_extracted",  # Mark all infer_from_graph output
        }

        # Load graph
        master_path = os.path.join(graph_dir, "code2database_master.json")
        if not os.path.exists(master_path):
            return knowledge

        try:
            G = _load_full_graph(graph_dir)
        except Exception:
            return knowledge

        # Infer architecture from domain structure
        domain_nodes = defaultdict(list)
        for nid, ndata in G.nodes(data=True):
            domain = ndata.get("domain", "root")
            if not ndata.get("is_empty", False):
                domain_nodes[domain].append(ndata)

        # Build architecture overview — structured for LLM curation
        arch_lines = ["# Project Architecture\n"]
        arch_lines.append("<!-- TEMPLATE: Fill in architecture description below. -->")
        arch_lines.append("<!-- Graph stats and domain list are auto-generated. -->")
        arch_lines.append("<!-- Replace this with principled, invariant architecture knowledge. -->\n")

        arch_lines.append(f"**Stats**: {G.number_of_nodes()} functions | "
                         f"{G.number_of_edges()} edges | "
                         f"{len(domain_nodes)} domains\n")

        # Top domains by size (condensed)
        arch_lines.append("## Domain Summary (top 30 by size)\n")
        sorted_domains = sorted(domain_nodes.items(), key=lambda x: -len(x[1]))[:30]
        for domain, nodes in sorted_domains:
            api_count = sum(1 for n in nodes if "API_entry" in n.get("labels", []))
            thread_count = sum(1 for n in nodes if "thread_processor" in n.get("labels", []))
            arch_lines.append(f"- **{domain}** ({len(nodes)} funcs, {api_count} APIs"
                             + (f", {thread_count} threads" if thread_count else "") + ")")

        # Hub functions
        from _builder.index_pack import _compute_hub_functions
        hubs = _compute_hub_functions(G, top_n=10)
        if hubs:
            arch_lines.append("\n## Hub Functions (top 10 betweenness)\n")
            for h in hubs:
                arch_lines.append(f"- **{h['name']}** ({h['domain']}) — "
                                 f"betweenness={h['betweenness']:.4f}")

        # Key entry points
        scores_path = os.path.join(graph_dir, ".code2database_entry_scores.json")
        if os.path.exists(scores_path):
            scores_data = json.loads(Path(scores_path).read_text(encoding="utf-8"))
            arch_lines.append(f"\n## Top Entry Points\n")
            for ep in scores_data.get("top_entries", [])[:15]:
                arch_lines.append(f"- `{ep['name']}` (score: {ep['score']:.2f}, domain: {ep.get('domain', '')})")

        # Cross-domain hotspots
        from _builder.index_pack import _compute_cross_domain_hotspots
        hotspots = _compute_cross_domain_hotspots(G, top_n=10)
        if hotspots:
            arch_lines.append("\n## Cross-Domain Hotspots\n")
            for hs in hotspots:
                arch_lines.append(f"- **{hs['caller_domain']}** → **{hs['callee_domain']}** "
                                 f"({hs['edge_count']} edges)")

        arch_lines.append("\n---\n")
        arch_lines.append("<!-- FILL IN: Architecture principles, design patterns, key data flows -->")
        arch_lines.append("<!-- Example: This project uses an event-driven reactor pattern where... -->")

        knowledge["architecture"] = "\n".join(arch_lines) + "\n"

        # Principles template — principled invariants that don't change with code refactoring
        principles_lines = ["# Design Principles and Protocol Standards\n"]
        principles_lines.append("<!-- TEMPLATE: Fill in principled, invariant knowledge below. -->")
        principles_lines.append("<!-- This file stores knowledge that does NOT change when code is refactored. -->")
        principles_lines.append("<!-- Examples: protocol standards, API contracts, safety rules, threading models -->\n")
        principles_lines.append("## Protocol Standards\n")
        principles_lines.append("<!-- FILL IN: e.g., Protocol connection follows: create → configure → start → stop -->\n")
        principles_lines.append("## API Contracts\n")
        principles_lines.append("<!-- FILL IN: e.g., All public APIs require initialization before use -->\n")
        principles_lines.append("## Threading Model\n")
        principles_lines.append("<!-- FILL IN: e.g., Event-driven model with per-thread event loops, no locks needed within thread -->\n")
        principles_lines.append("## Safety Rules\n")
        principles_lines.append("<!-- FILL IN: e.g., Context objects are per-thread, must not be shared across threads -->\n")
        knowledge["principles"] = "\n".join(principles_lines) + "\n"

        # Glossary from labels
        label_set = set()
        for nid, ndata in G.nodes(data=True):
            for label in ndata.get("labels", []):
                label_set.add(label)
        if label_set:
            glossary_lines = ["# Glossary\n"]
            label_descs = {
                "API_entry": "Public API entry point — function accessible to external callers",
                "out_end": "External function call (leaf, no definition in source)",
                "unknown_end": "External function with unclear purpose",
                "thread_processor": "Thread/process entry point",
                "callback_func": "Callback function registered with a framework",
                "constructor": "Object constructor/initializer",
                "destructor": "Object destructor/cleanup",
                "dead_code": "Code excluded by preprocessor conditions",
                "hub": "Hub function with high betweenness centrality",
                "file": "Synthetic file node (contains function nodes)",
            }
            for label in sorted(label_set):
                desc = label_descs.get(label, "")
                glossary_lines.append(f"- **{label}**: {desc}" if desc else f"- **{label}**")
            knowledge["glossary"] = "\n".join(glossary_lines) + "\n"

        return knowledge

    # -----------------------------------------------------------------------
    # Knowledge file I/O
    # -----------------------------------------------------------------------

    def write_knowledge_files(self, knowledge: dict):
        """Write knowledge dict to files in knowledge/ directory.

        Per-module files are NOT auto-generated — they are noise.
        Only write architecture, glossary, constraints, and LLM-curated content.

        The knowledge dict may contain a ``_source`` key (value: "manual",
        "llm_generated", or "auto_extracted") that sets the default source
        for all files written in this batch. Individual files can override
        this via a ``_file_sources`` dict mapping filename -> source.
        """
        batch_source = knowledge.get("_source", "")
        file_source_overrides = knowledge.get("_file_sources", {})
        # Clean up existing auto-generated module files (noise from previous versions)
        for fname in os.listdir(self.knowledge_dir):
            if fname.startswith("module_") and fname.endswith(".md"):
                fpath = os.path.join(self.knowledge_dir, fname)
                # Only delete files that look like auto-generated (function listing only)
                content = Path(fpath).read_text(encoding="utf-8", errors="replace")
                non_sig_lines = len([l for l in content.split("\n")
                                    if l.strip() and not l.startswith("#")
                                    and not l.startswith("- `") and not l.startswith("##")])
                if non_sig_lines <= 2:
                    os.remove(fpath)

        # Architecture
        if knowledge.get("architecture"):
            path = os.path.join(self.knowledge_dir, "architecture.md")
            Path(path).write_text(knowledge["architecture"], encoding="utf-8")

        # Constraints
        if knowledge.get("constraints"):
            path = os.path.join(self.knowledge_dir, "constraints.md")
            Path(path).write_text(knowledge["constraints"], encoding="utf-8")

        # Glossary
        if knowledge.get("glossary"):
            path = os.path.join(self.knowledge_dir, "glossary.md")
            Path(path).write_text(knowledge["glossary"], encoding="utf-8")

        # Patterns
        if knowledge.get("patterns"):
            path = os.path.join(self.knowledge_dir, "patterns.md")
            Path(path).write_text(knowledge["patterns"], encoding="utf-8")

        # Build rules
        if knowledge.get("build_rules"):
            path = os.path.join(self.knowledge_dir, "build_rules.md")
            Path(path).write_text(knowledge["build_rules"], encoding="utf-8")

        # Detail sections (from doc headings, source patterns, stale nodes)
        for detail_name, content in knowledge.get("design_details", {}).items():
            if content:
                fname = detail_name if detail_name.endswith(".md") else f"{detail_name}.md"
                path = os.path.join(self.knowledge_dir, fname)
                Path(path).write_text(content, encoding="utf-8")

        # Principles (domain principles, protocol flows, invariant rules)
        if knowledge.get("principles"):
            path = os.path.join(self.knowledge_dir, "principles.md")
            Path(path).write_text(knowledge["principles"], encoding="utf-8")

        # Custom topics (LLM/human-defined knowledge)
        for topic_name, content in knowledge.get("custom_topics", {}).items():
            if content:
                fname = f"custom_{topic_name}.md" if not topic_name.endswith(".md") else topic_name
                path = os.path.join(self.knowledge_dir, fname)
                Path(path).write_text(content, encoding="utf-8")

        # Write _meta.json tracking knowledge provenance
        existing_meta = {}
        meta_path = os.path.join(self.knowledge_dir, "_meta.json")
        if os.path.exists(meta_path):
            try:
                existing_meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        file_sources = {}
        for fname in sorted(os.listdir(self.knowledge_dir)):
            if fname.endswith(".md"):
                # Priority: existing saved value > explicit override > batch source > filename inference
                if fname in existing_meta.get("file_sources", {}):
                    # Preserve the existing source value on re-run
                    file_sources[fname] = existing_meta["file_sources"][fname]
                elif fname in file_source_overrides:
                    # Explicit per-file override from the knowledge dict
                    file_sources[fname] = file_source_overrides[fname]
                elif batch_source:
                    # Batch-level source (e.g. "auto_extracted" from infer_from_graph)
                    file_sources[fname] = batch_source
                else:
                    # Infer from filename pattern
                    if fname in ("architecture.md", "glossary.md", "constraints.md",
                                 "patterns.md", "build_rules.md", "principles.md"):
                        file_sources[fname] = "auto_extracted"
                    elif fname.startswith("detail_"):
                        file_sources[fname] = "auto_extracted"
                    elif fname.startswith("custom_"):
                        file_sources[fname] = "llm_generated"
                    else:
                        file_sources[fname] = "manual"

        meta = {
            "format_version": 2,
            "last_updated": datetime.now().isoformat(),
            "files": sorted([f for f in os.listdir(self.knowledge_dir) if f.endswith(".md")]),
            "file_sources": file_sources,
        }
        meta_path = os.path.join(self.knowledge_dir, "_meta.json")
        Path(meta_path).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

        # Generate _memory_links.json mapping knowledge files to related memory entries
        self._generate_memory_links()

    def _generate_memory_links(self):
        """Generate _memory_links.json mapping each knowledge file to related memory entries.

        Links are based on domain overlap: knowledge file headings/tags are matched
        against memory entry tags and question tokens. This enables bidirectional
        discovery between knowledge and memory.
        """
        from _builder.utils import _simple_tokenize, _similarity_score

        # Load knowledge file topics
        knowledge_topics = {}  # fname -> set of tokens
        for fname in sorted(os.listdir(self.knowledge_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(self.knowledge_dir, fname)
            content = Path(fpath).read_text(encoding="utf-8", errors="replace")
            # Extract tokens from headings and key terms
            headings = re.findall(r'^##?\s+(.+)$', content, re.MULTILINE)
            heading_text = " ".join(headings)
            # Also include the filename stem as a token source
            stem = fname.replace(".md", "").replace("_", " ")
            tokens = _simple_tokenize(heading_text) | _simple_tokenize(stem)
            if tokens:
                knowledge_topics[fname] = tokens

        # Load memory entries
        mem_dir = os.path.join(self.graph_dir, "memory")
        mem_index_path = os.path.join(mem_dir, "index.json")
        memory_entries = []
        if os.path.exists(mem_index_path):
            try:
                mem_index = json.loads(Path(mem_index_path).read_text(encoding="utf-8"))
                for entry_meta in mem_index.get("entries", []):
                    if entry_meta.get("status") == "experience":
                        continue
                    eid = entry_meta["id"]
                    q_tokens = _simple_tokenize(entry_meta.get("question", ""))
                    tag_tokens = set()
                    for tag in entry_meta.get("tags", []):
                        tag_tokens |= _simple_tokenize(tag)
                    memory_entries.append({
                        "id": eid,
                        "question": entry_meta.get("question", ""),
                        "tokens": q_tokens | tag_tokens,
                        "tags": entry_meta.get("tags", []),
                    })
            except (json.JSONDecodeError, OSError):
                logging.getLogger(__name__).debug("silent exception", exc_info=True)
                pass
        links = {}
        SIM_THRESHOLD = 0.15  # Low threshold — broad linking is fine
        for fname, k_tokens in knowledge_topics.items():
            related = []
            for mem in memory_entries:
                if not mem["tokens"]:
                    continue
                sim = _similarity_score(k_tokens, mem["tokens"])
                if sim >= SIM_THRESHOLD:
                    related.append({
                        "id": mem["id"],
                        "question": mem["question"],
                        "tags": mem["tags"][:3],
                        "similarity": round(sim, 4),
                    })
            # Sort by similarity, keep top 10
            related.sort(key=lambda x: -x["similarity"])
            if related:
                links[fname] = related[:10]

        # Write _memory_links.json
        links_path = os.path.join(self.knowledge_dir, "_memory_links.json")
        links_data = {
            "format_version": 1,
            "generated_at": datetime.now().isoformat(),
            "links": links,
        }
        Path(links_path).write_text(
            json.dumps(links_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

        # Back-fill knowledge_refs into memory entries
        self._update_memory_knowledge_refs(links)

    def _update_memory_knowledge_refs(self, links: dict):
        """Update memory entries with knowledge_refs based on _memory_links.

        For each memory entry that appears in the links, add a knowledge_refs
        field listing the knowledge files that relate to it.
        """
        # Invert the links: mem_id -> [fname, ...]
        mem_to_knowledge = {}  # mem_id -> list of filenames
        for fname, related in links.items():
            for r in related:
                mid = r["id"]
                mem_to_knowledge.setdefault(mid, []).append(fname)

        if not mem_to_knowledge:
            return

        # Load memory index and update entries
        from _builder.memory_manager import MemoryManager
        mem_mgr = MemoryManager(self.graph_dir)
        mem_index = mem_mgr._load_index()

        for entry_meta in mem_index.get("entries", []):
            mid = entry_meta["id"]
            if mid not in mem_to_knowledge:
                continue

            # Load the entry (try root first, then leaf)
            is_root = (entry_meta.get("root_id") == mid)
            entry = mem_mgr._load_entry(mid, is_root=is_root)
            if not entry:
                entry = mem_mgr._load_entry(mid, is_root=False)
            if not entry:
                continue

            # Set knowledge_refs, preserving any existing ones
            existing_refs = set(entry.get("knowledge_refs", []))
            existing_refs.update(mem_to_knowledge[mid])
            entry["knowledge_refs"] = sorted(existing_refs)
            mem_mgr._save_entry(entry, is_root=(entry_meta.get("root_id") == mid))

    def _discover_doc_files(self, source_root: str) -> list:
        """Find documentation files in a source directory."""
        doc_files = []
        doc_names = {"readme.md", "readme.rst", "readme.txt", "readme",
                     "design.md", "architecture.md", "contributing.md",
                     "glossary.md", "patterns.md", "changelog.md"}
        doc_dirs = {"docs", "doc", "documentation", "design"}

        for dirpath, dirnames, filenames in os.walk(source_root):
            # Skip hidden and build directories
            dirnames[:] = [d for d in dirnames
                          if not d.startswith('.') and d not in ("__pycache__", "node_modules", "build")]

            for fname in filenames:
                lower = fname.lower()
                if lower in doc_names or lower.endswith((".md", ".rst", ".txt")):
                    if lower not in ("license.md", "license.txt", "copying"):
                        doc_files.append(os.path.join(dirpath, fname))

        return doc_files[:50]  # Cap at 50 docs

    # -----------------------------------------------------------------------
    # Knowledge index and pack
    # -----------------------------------------------------------------------

    def build_index(self) -> dict:
        """Build knowledge index from existing files."""
        index = {"files": [], "topics": []}
        for fname in sorted(os.listdir(self.knowledge_dir)):
            if fname.endswith(".md"):
                fpath = os.path.join(self.knowledge_dir, fname)
                stat = os.stat(fpath)
                content = Path(fpath).read_text(encoding="utf-8", errors="replace")
                # Extract headings as topics
                headings = re.findall(r'^##\s+(.+)$', content, re.MULTILINE)
                entry = {
                    "name": fname,
                    "size": stat.st_size,
                    "headings": headings[:20],
                }
                index["files"].append(entry)
                index["topics"].extend(headings)

        index["topics"] = sorted(set(index["topics"]))[:100]

        # Write index
        index_path = os.path.join(self.knowledge_dir, "index.json")
        Path(index_path).write_text(
            json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return index

    def generate_pack(self, tier: str = "lite") -> dict:
        """Generate knowledge pack for LLM consumption.

        tier: "lite" (~300 tokens), "standard" (~800 tokens), "full" (~2000 tokens)
        """
        index = self.build_index()

        if tier == "lite":
            return self._pack_lite(index)
        if tier == "full":
            return self._pack_full(index)
        return self._pack_standard(index)

    def _pack_lite(self, index: dict) -> dict:
        """~300 tokens: file list + top headings + architecture summary."""
        pack = {
            "files": [f["name"] for f in index["files"]],
            "topics": index["topics"][:30],
            "architecture_summary": "",
        }

        # Read first 500 chars of architecture.md
        arch_path = os.path.join(self.knowledge_dir, "architecture.md")
        if os.path.exists(arch_path):
            text = Path(arch_path).read_text(encoding="utf-8", errors="replace")
            pack["architecture_summary"] = text[:500]

        pack_path = os.path.join(self.graph_dir, ".knowledge_pack_lite.json")
        Path(pack_path).write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return pack

    def _pack_standard(self, index: dict) -> dict:
        """~800 tokens: full architecture + constraints + glossary."""
        pack = {
            "files": [{"name": f["name"], "headings": f["headings"]} for f in index["files"]],
            "architecture": "",
            "constraints": "",
            "glossary": "",
        }

        # Architecture
        arch_path = os.path.join(self.knowledge_dir, "architecture.md")
        if os.path.exists(arch_path):
            pack["architecture"] = Path(arch_path).read_text(encoding="utf-8", errors="replace")[:2000]

        # Constraints
        constr_path = os.path.join(self.knowledge_dir, "constraints.md")
        if os.path.exists(constr_path):
            pack["constraints"] = Path(constr_path).read_text(encoding="utf-8", errors="replace")[:500]

        # Glossary
        gloss_path = os.path.join(self.knowledge_dir, "glossary.md")
        if os.path.exists(gloss_path):
            pack["glossary"] = Path(gloss_path).read_text(encoding="utf-8", errors="replace")[:500]

        pack_path = os.path.join(self.graph_dir, ".knowledge_pack_standard.json")
        Path(pack_path).write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return pack

    def _pack_full(self, index: dict) -> dict:
        """~2000 tokens: all knowledge files, full architecture, all detail sections."""
        pack = {
            "files": [{"name": f["name"], "headings": f["headings"], "size": f["size"]}
                      for f in index["files"]],
            "architecture": "",
            "constraints": "",
            "glossary": "",
            "patterns": "",
            "build_rules": "",
            "design_details": {},
        }

        # Full architecture
        arch_path = os.path.join(self.knowledge_dir, "architecture.md")
        if os.path.exists(arch_path):
            pack["architecture"] = Path(arch_path).read_text(encoding="utf-8", errors="replace")

        # Constraints, glossary, patterns, build_rules
        for key in ("constraints", "glossary", "patterns", "build_rules"):
            fpath = os.path.join(self.knowledge_dir, f"{key}.md")
            if os.path.exists(fpath):
                pack[key] = Path(fpath).read_text(encoding="utf-8", errors="replace")[:1000]

        # Detail sections
        for f in index["files"]:
            if f["name"].startswith("detail_"):
                fpath = os.path.join(self.knowledge_dir, f["name"])
                text = Path(fpath).read_text(encoding="utf-8", errors="replace")
                detail_name = f["name"].replace("detail_", "").replace(".md", "")
                pack["design_details"][detail_name] = text[:1000]

        pack_path = os.path.join(self.graph_dir, ".knowledge_pack_full.json")
        Path(pack_path).write_text(
            json.dumps(pack, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        return pack

    # -----------------------------------------------------------------------
    # Knowledge query
    # -----------------------------------------------------------------------

    def query_knowledge(self, topic: str, max_tokens: int = 500) -> str:
        """Search knowledge files for a topic, return relevant content.

        Prefers the unified FTS5+BM25 index (kb_paragraphs) when
        code2database.db exists; falls back to legacy substring search.
        """
        # Phase 1+2 upgrade: FTS5 path
        try:
            from _builder.kb_index import query_kb
            results = query_kb(
                graph_dir=self.graph_dir,
                query=topic,
                top_n=10,
                kinds=["principle", "fact", "pattern", "glossary"],
                min_weight=0.0,
                max_tokens=max_tokens,
            )
            if results:
                lines = []
                for r in results:
                    lines.append(f"--- {r['source_file']} (score={r['score']}) ---")
                    if r["title"]:
                        lines.append(f"## {r['title']}")
                    lines.append(r["body"])
                    lines.append("")
                return "\n".join(lines)
            # FTS5 returned no hits — fall through to legacy
        except Exception:
            logging.getLogger(__name__).debug("silent exception", exc_info=True)
            pass
        topic_lower = topic.lower()
        results = []

        for fname in sorted(os.listdir(self.knowledge_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(self.knowledge_dir, fname)
            content = Path(fpath).read_text(encoding="utf-8", errors="replace")

            # Simple keyword matching
            if topic_lower in content.lower():
                # Extract relevant paragraphs
                paragraphs = content.split("\n\n")
                relevant = []
                for para in paragraphs:
                    if topic_lower in para.lower():
                        relevant.append(para.strip())
                if relevant:
                    results.append(f"--- {fname} ---\n" + "\n".join(relevant[:5]))

        if not results:
            return f"No knowledge found for topic: {topic}"

        combined = "\n\n".join(results)
        # Truncate if needed
        if len(combined) > max_tokens * 4:  # rough char-to-token ratio
            combined = combined[:max_tokens * 4] + "\n... (truncated)"

        return combined


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def cmd_extract_knowledge(args):
    """Extract knowledge template for LLM curation.

    Produces a .code2database_knowledge_input.json template with:
    - Graph-inferred architecture (domains, communities, entry points)
    - Doc section headings as suggested topics for LLM to fill
    - Source pattern summaries as topic suggestions

    The LLM should then fill in principled knowledge (protocol flows,
    domain rules, invariant constraints) and run 'apply-knowledge'.
    """
    graph_dir = args.graph
    source_root = getattr(args, "source", "")
    docs_path = getattr(args, "docs", "")

    mgr = KnowledgeManager(graph_dir)

    # Infer architecture from graph (always useful as context)
    graph_knowledge = mgr.infer_from_graph(graph_dir)

    # Discover doc files for topic suggestions
    doc_files = []
    if docs_path and os.path.exists(docs_path):
        if os.path.isdir(docs_path):
            for f in sorted(os.listdir(docs_path)):
                if f.endswith((".md", ".rst", ".txt", ".pdf")):
                    doc_files.append(os.path.join(docs_path, f))
        else:
            doc_files.append(docs_path)

    if source_root and os.path.isdir(source_root):
        doc_files.extend(mgr._discover_doc_files(source_root))

    # Extract doc section titles as suggested topics
    suggested_topics = []
    for doc_path in doc_files:
        if not os.path.exists(doc_path):
            continue
        text = Path(doc_path).read_text(encoding="utf-8", errors="replace")
        sections = mgr._extract_structured_sections(text, doc_path)
        for safe_title in sections:
            suggested_topics.append({
                "title": safe_title.replace("_", " "),
                "source": doc_path,
            })

    # Source pattern summaries (just counts, not full content)
    pattern_summary = {}
    if source_root and os.path.isdir(source_root):
        source_details = mgr.extract_from_source_patterns(source_root, graph_dir)
        for fname, content in source_details.items():
            lines = content.strip().split("\n")
            # Count items (lines starting with - )
            item_count = sum(1 for l in lines if l.strip().startswith("- "))
            pattern_summary[fname] = f"{item_count} items found"

    # Build template for LLM
    template = {
        "_instructions": (
            "Fill in principled, invariant knowledge that cannot be represented "
            "in the invocation graph. Examples: protocol flows (e.g., RDMA standard flow), "
            "hardware constraints, algorithmic principles, domain-specific rules. "
            "Only include knowledge that won't change when code changes. "
            "Remove any auto-suggested topics that are not meaningful."
        ),
        "principles": "",  # Domain principles, protocol flows, invariant rules
        "architecture": graph_knowledge.get("architecture", ""),
        "constraints": graph_knowledge.get("constraints", ""),
        "glossary": graph_knowledge.get("glossary", ""),
        "custom_topics": {},  # LLM-defined: {"topic_name": "content markdown"}
        "_suggested_topics": suggested_topics[:20],
        "_source_pattern_summary": pattern_summary,
    }

    # Write template
    input_path = os.path.join(graph_dir, ".code2database_knowledge_input.json")
    Path(input_path).write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    # Also write graph-inferred files (these are always useful as context)
    mgr.write_knowledge_files({
        "architecture": graph_knowledge.get("architecture", ""),
        "modules": graph_knowledge.get("modules", {}),
        "glossary": graph_knowledge.get("glossary", ""),
    })
    mgr.build_index()
    mgr.generate_pack("lite")
    mgr.generate_pack("standard")
    mgr.generate_pack("full")

    file_count = len([f for f in os.listdir(mgr.knowledge_dir) if f.endswith(".md")])
    print(f"Knowledge template written to: {input_path}")
    print(f"  Graph context files: {file_count}")
    print(f"  Suggested topics: {len(suggested_topics[:20])}")
    print(f"  Source patterns found: {list(pattern_summary.keys())}")
    print(f"\nNext: Fill .code2database_knowledge_input.json with principled knowledge,")
    print(f"then run: code2database_builder.py apply-knowledge --graph {graph_dir}")


def cmd_apply_knowledge(args):
    """Apply LLM-filled knowledge back to knowledge directory.

    Reads .code2database_knowledge_input.json from graph_dir, which contains
    LLM-filled knowledge to write back.
    """
    graph_dir = args.graph
    mgr = KnowledgeManager(graph_dir)

    input_path = os.path.join(graph_dir, ".code2database_knowledge_input.json")
    if not os.path.exists(input_path):
        print("No .code2database_knowledge_input.json found. Create this file with LLM-filled knowledge.")
        print("Expected format: {\"architecture\": \"...\", \"modules\": {...}, ...}")
        return

    knowledge = json.loads(Path(input_path).read_text(encoding="utf-8"))
    mgr.write_knowledge_files(knowledge)
    mgr.build_index()
    mgr.generate_pack("lite")
    mgr.generate_pack("standard")
    mgr.generate_pack("full")

    print("Knowledge applied and packs regenerated.")


def cmd_knowledge_query(args):
    """Query knowledge by topic."""
    graph_dir = args.graph
    topic = args.topic
    max_tokens = getattr(args, "max_tokens", 500)

    mgr = KnowledgeManager(graph_dir)
    result = mgr.query_knowledge(topic, max_tokens=max_tokens)
    print(result)


def validate_knowledge(graph_dir: str) -> dict:
    """Validate knowledge files against current graph state and content quality.

    Checks:
    - Knowledge references functions/domains that still exist (stale/missing)
    - Empty files (0 content lines)
    - Files containing only function signatures (no actual knowledge)
    - Files with only template comments (LLM_FILL / FILL IN markers)
    Returns dict of inconsistencies and quality issues.
    """
    mgr = KnowledgeManager(graph_dir)
    result = {}

    # --- Domain consistency check ---
    from _builder.graph_build import _load_full_graph
    try:
        G = _load_full_graph(graph_dir)
    except Exception:
        result["error"] = "Cannot load graph"
        G = None

    if G is not None:
        current_domains = set()
        for nid, ndata in G.nodes(data=True):
            domain = ndata.get("domain", "root")
            if not ndata.get("is_empty", False):
                current_domains.add(domain)

        # Read architecture.md domain list
        arch_path = os.path.join(mgr.knowledge_dir, "architecture.md")
        arch_domains_mentioned = set()
        if os.path.exists(arch_path):
            arch_text = Path(arch_path).read_text(encoding="utf-8", errors="replace")
            for line in arch_text.split("\n"):
                m = re.match(r'-\s+\*\*(.+?)\*\*', line)
                if m:
                    arch_domains_mentioned.add(m.group(1))

        # Domains in knowledge but not in graph (stale)
        stale_domains = arch_domains_mentioned - current_domains
        # Domains in graph but not in knowledge (missing)
        missing_domains = current_domains - arch_domains_mentioned

        result["graph_domains"] = len(current_domains)
        result["knowledge_domains"] = len(arch_domains_mentioned)
        result["stale_domains"] = sorted(stale_domains)
        result["missing_domains"] = sorted(missing_domains)[:20]

    # --- Content quality checks ---
    quality_issues = []
    for fname in sorted(os.listdir(mgr.knowledge_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(mgr.knowledge_dir, fname)
        content = Path(fpath).read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")

        # Count non-empty, non-heading lines
        content_lines = [l for l in lines if l.strip()
                         and not l.strip().startswith("#")
                         and not l.strip().startswith("<!--")]
        # Count lines that look like function signatures: - `func_name` or - **func_name**
        sig_lines = [l for l in content_lines
                     if re.match(r'^-\s+`[\w.]+`', l)
                     or re.match(r'^-\s+\*\*[\w.]+\*\*', l)]
        # Count template/fill-marker lines
        template_markers = re.findall(
            r'(?:LLM_FILL|FILL\s*IN|<!--\s*FILL|<!--\s*TEMPLATE|REPLACE\s+THIS)',
            content, re.IGNORECASE)

        # Check: empty file (0 content lines)
        if len(content_lines) == 0:
            quality_issues.append({
                "file": fname,
                "issue": "empty_file",
                "detail": "0 content lines (file is empty or contains only headings/comments)",
            })
        # Check: only function signatures, no actual knowledge
        elif len(sig_lines) > 0 and len(content_lines) == len(sig_lines):
            quality_issues.append({
                "file": fname,
                "issue": "signatures_only",
                "detail": f"{len(sig_lines)} signature lines, 0 knowledge content lines",
            })
        # Check: only template comments / fill markers
        elif len(template_markers) > 0 and len(content_lines) <= len(template_markers):
            quality_issues.append({
                "file": fname,
                "issue": "template_only",
                "detail": f"{len(template_markers)} template/fill markers, {len(content_lines)} content lines",
            })
        # Warning: file has template markers but also has some content
        elif len(template_markers) > 0 and len(content_lines) > len(template_markers):
            quality_issues.append({
                "file": fname,
                "issue": "has_unfilled_templates",
                "detail": f"{len(template_markers)} unfilled template marker(s) remain",
            })

    result["quality_issues"] = quality_issues
    result["quality_issue_count"] = len(quality_issues)
    result["files_checked"] = len([f for f in os.listdir(mgr.knowledge_dir) if f.endswith(".md")])

    # --- Doc-code alignment check ---
    # Detect semantic mismatches between docs (semantic_desc/external_desc)
    # and code (body_text/signature). Flag nodes where doc says one thing
    # but code does another.
    try:
        from _builder.doc_code_align import check_doc_code_alignment
        align = check_doc_code_alignment(graph_dir)
        if "error" not in align:
            result["doc_code_alignment"] = {
                "checked_count": align.get("checked_count", 0),
                "mismatched_count": align.get("mismatched_count", 0),
                "by_kind": align.get("by_kind", {}),
                "mismatches": align.get("mismatches", []),
            }
    except Exception as exc:
        result["doc_code_alignment_error"] = str(exc)

    return result


def cmd_knowledge_validate(args):
    """Validate knowledge against current graph state and content quality."""
    result = validate_knowledge(args.graph)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("stale_domains"):
        print(f"\nStale domains in knowledge (no longer in graph): {result['stale_domains']}")
    if result.get("missing_domains"):
        print(f"Missing domains in knowledge (new in graph): {result['missing_domains'][:10]}")
    if result.get("quality_issues"):
        print(f"\nContent quality issues ({result['quality_issue_count']}):")
        for issue in result["quality_issues"]:
            print(f"  [{issue['issue']}] {issue['file']}: {issue['detail']}")
    else:
        print(f"\nAll {result.get('files_checked', 0)} files passed quality checks.")
    # Doc-code alignment
    align = result.get("doc_code_alignment")
    if align:
        print(f"\nDoc-code alignment: checked {align['checked_count']} nodes, "
              f"{align['mismatched_count']} with mismatches")
        if align.get("by_kind"):
            for kind, count in align["by_kind"].items():
                print(f"  {kind}: {count}")
        if align.get("mismatches"):
            print("  Sample mismatches:")
            for m in align["mismatches"][:5]:
                print(f"    [{m['kind']}] {m['node_id']}: {m.get('detail', m.get('doc_claim', ''))}")
            if len(align["mismatches"]) > 5:
                print(f"    ... and {len(align['mismatches']) - 5} more")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Knowledge manager")
    sub = parser.add_subparsers(dest="command")

    p_extract = sub.add_parser("extract")
    p_extract.add_argument("--graph", required=True)
    p_extract.add_argument("--source", default="")
    p_extract.add_argument("--docs", default="")

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--graph", required=True)

    p_query = sub.add_parser("query")
    p_query.add_argument("--graph", required=True)
    p_query.add_argument("--topic", required=True)
    p_query.add_argument("--max-tokens", type=int, default=500)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "extract":
        cmd_extract_knowledge(args)
    elif args.command == "apply":
        cmd_apply_knowledge(args)
    elif args.command == "query":
        cmd_knowledge_query(args)
