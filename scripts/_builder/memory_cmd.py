"""callgraph builder module: memory_cmd (SQLite memory store commands)."""

import json
import sys
from datetime import datetime

from _builder.utils import _extract_chain_node_ids
from _builder.graph_build import _load_full_graph
import logging


_log = logging.getLogger(__name__)


def _auto_validate_memory(G, mem_dir: str, graph_dir: str):
    """Auto-validate memory entries against current graph after update.

    DB-backed: entries whose node_ids left the graph are demoted to
    experience with a reason. Called by the daemon after each sync.
    """
    from _builder.memory_store import MemoryStore
    store = MemoryStore(graph_dir)
    store.validate_against_graph(set(G.nodes()))


def cmd_save_memory(args):
    """Save a Q&A memory entry (with optional call chains + category)."""
    graph_dir = args.graph

    question = args.question
    answer = args.answer
    chains = args.chains  # JSON string of chain data
    tags = args.tags.split(",") if args.tags else []
    node_ids = args.node_ids.split(",") if args.node_ids else []

    # Auto-extract node_ids from chains if not provided
    if not node_ids and chains:
        chain_data = json.loads(chains) if isinstance(chains, str) else chains
        node_ids = _extract_chain_node_ids(chain_data)

    from _builder.memory_store import MemoryStore
    store = MemoryStore(graph_dir)

    # Correct-first path: reshape the similar existing entry instead of
    # saving a duplicate variant of a wrong answer.
    if getattr(args, "correct", False):
        result = store.correct_similar(
            question=question, answer=answer,
            author=getattr(args, "author", ""),
            symbols=getattr(args, "symbol", None))
        if result["action"] == "corrected":
            print(f"Corrected memory #{result['id']} "
                  f"(was: {result['matched_question']!r}, "
                  f"similarity {result['score']:.2f}) — "
                  "no new variant created")
        else:
            print(f"No similar memory found — saved as new "
                  f"#{result['id']}")
        return

    entry_id = store.add(
        question=question,
        answer=answer,
        tags=tags,
        node_ids=node_ids,
        chains=json.loads(chains) if chains and isinstance(chains, str)
            else (chains or []),
        category=getattr(args, "category", "") or None,
        author=getattr(args, "author", ""),
        no_merge=(args.no_merge is True),
        symbols=getattr(args, "symbol", None),
    )
    category = getattr(args, "category", "") or "uncategorized"
    syms = getattr(args, "symbol", None) or []
    print(f"Saved memory #{entry_id} (category: {category}, "
          f"{len(node_ids)} nodes tracked"
          + (f", symbols: {', '.join(syms)}" if syms else "") + ")")


def cmd_search_memory(args):
    """Search memory (FTS5 BM25 + filters) and print results as JSON."""
    graph_dir = args.graph
    query = args.query
    top = args.top

    from _builder.memory_store import MemoryStore
    store = MemoryStore(graph_dir)
    results = store.search(
        query,
        top_n=top,
        category=getattr(args, "category", "") or None,
        tags=args.tags.split(",") if getattr(args, "tags", "") else None,
        author=getattr(args, "author", "") or None,
        include_experience=bool(getattr(args, "include_experience", False)),
        symbol=getattr(args, "symbol", "") or None,
    )

    if not results:
        print("No similar memories found.")
        return
    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_validate_memory(args):
    """Validate memory entries against current graph.

    Entries referencing removed nodes are demoted to experience.
    """
    graph_dir = args.graph

    G = _load_full_graph(graph_dir)
    current_nodes = set(G.nodes())

    from _builder.memory_store import MemoryStore
    store = MemoryStore(graph_dir)
    result = store.validate_against_graph(current_nodes)

    invalidated_ids = result.get("invalidated_ids", [])
    if invalidated_ids:
        print("Invalidated entries:")
        for inv in invalidated_ids[:10]:
            print(f"  - #{inv['id']}: {inv['question']} "
                  f"({inv['missing_nodes']} nodes missing)")
        if len(invalidated_ids) > 10:
            print(f"  ... and {len(invalidated_ids) - 10} more")
