"""kb sub-skill commands: store init and cross-domain registry.

kb-init provisions a knowledge/memory store with no graph — the
deployment shape where the kb runs standalone (or alongside a graph
store that uses different .db files). The kb-domain-* commands manage
this store's identity and the other kb stores it may query.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, Any

import logging

_log = logging.getLogger(__name__)


def cmd_kb_init(args):
    """Provision a knowledge/memory store (no graph required)."""
    graph_dir = args.graph
    domain = (getattr(args, "name", "") or "").strip()
    created: Dict[str, Any] = {}

    # memory store (memory/memory.db)
    from _builder.memory.memory_store import MemoryStore
    if not os.path.exists(os.path.join(graph_dir, "memory",
                                       "memory.db")):
        MemoryStore(graph_dir)
        created["memory_store"] = True
    else:
        created["memory_store"] = False

    # knowledge store (knowledge/knowledge.db) — created read-write;
    # a legacy brief.json is imported once.
    from _builder.kb.knowledge_store import open_knowledge, \
        knowledge_db_path
    if not os.path.exists(knowledge_db_path(graph_dir)):
        store = open_knowledge(graph_dir, create_if_missing=True)
        if store is not None:
            store.close()
        created["knowledge_store"] = True
    else:
        created["knowledge_store"] = False

    # unified index (kb_index.db)
    from _builder.kb.kb_index import _kb_connect
    if not os.path.exists(os.path.join(graph_dir, "kb_index.db")):
        conn = _kb_connect(graph_dir)
        if conn is not None:
            conn.close()
        created["kb_index"] = True
    else:
        created["kb_index"] = False

    # domain identity
    from _builder.kb.kb_index import get_domain_name, set_domain_name
    if domain:
        set_domain_name(graph_dir, domain)
    effective_domain = get_domain_name(graph_dir)

    print(f"Knowledge store ready at {os.path.abspath(graph_dir)}")
    print(f"  domain:            {effective_domain}")
    print(f"  memory store:      memory/memory.db"
          + (" (created)" if created["memory_store"] else " (existing)"))
    print(f"  knowledge store:   knowledge/knowledge.db"
          + (" (created)" if created["knowledge_store"]
             else " (existing)"))
    print(f"  unified index:     kb_index.db"
          + (" (created)" if created["kb_index"] else " (existing)"))
    print("  graph artifacts:  none required — this store runs "
          "standalone")
    if getattr(args, "json", False):
        print(json.dumps({"domain": effective_domain, "created": created},
                         ensure_ascii=False, indent=2))
        return
    print()
    print("Next steps:")
    print("  save-memory --question '...' --answer '...' "
          "--category path/to/topic --author you "
          "[--version-scope <branch>]")
    print("  kb-query --query '...'")
    print("  session-init        # brief + memory digest + known "
          "unknowns")
    print("  kb-domain-add <other-store-dir>   # query other kb "
          "domains (cross=... on kb-query)")


def cmd_kb_domain_name(args):
    """Get or set this store's domain identity."""
    from _builder.kb.kb_index import get_domain_name, set_domain_name
    graph_dir = args.graph
    name = (getattr(args, "name", "") or "").strip()
    if name:
        set_domain_name(graph_dir, name)
        print(json.dumps({"domain_name": name}, ensure_ascii=False))
        return
    print(json.dumps({"domain_name": get_domain_name(graph_dir)},
                     ensure_ascii=False))


def cmd_kb_domain_add(args):
    """Register another knowledge base as a queryable domain."""
    from _builder.kb.kb_index import watch_kb
    out = watch_kb(args.graph, args.path,
                   domain_name=(getattr(args, "name", "") or "").strip())
    if "error" in out:
        print(out["error"], file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_kb_domain_list(args):
    """List watched kb domains."""
    from _builder.kb.kb_index import list_watched_kbs, get_domain_name
    out = {
        "self_domain": get_domain_name(args.graph),
        "watched": list_watched_kbs(args.graph),
    }
    if not out["watched"]:
        print("No watched kb domains (register one with "
              "kb-domain-add <store-dir>).")
        return
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_kb_domain_remove(args):
    """Stop querying another knowledge base."""
    from _builder.kb.kb_index import unwatch_kb
    out = unwatch_kb(args.graph, args.path)
    print(json.dumps(out, ensure_ascii=False, indent=2))
