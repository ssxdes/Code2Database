"""Tests for the heuristic description fallback (P5-2).

Verifies that generate_heuristic_description produces sensible supplements
from node attributes and that apply_heuristic_enhancement writes them
through the same supplement path used by LLM auto-enhance.
"""
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _builder.auto_enhance import (
    generate_heuristic_description,
    apply_heuristic_enhancement,
    _is_likely_builtin,
    _humanize,
    _label_phrase,
    _domain_phrase,
)


def test_humanize_snake_case():
    assert _humanize("load_full_graph") == "Load full graph"
    assert _humanize("simple_name") == "Simple name"


def test_humanize_camel_case():
    assert _humanize("loadFullGraph") == "Load full graph"
    assert _humanize("simpleName") == "Simple name"


def test_humanize_empty():
    assert _humanize("") == ""
    assert _humanize("x") == "X"


def test_label_phrase_known_labels():
    assert "Entry point" in _label_phrase(["API_entry"])
    assert "worker" in _label_phrase(["thread_processor"]).lower()
    assert "callback" in _label_phrase(["callback_func"]).lower()
    assert "Initializes" in _label_phrase(["constructor"])
    assert "Releases" in _label_phrase(["destructor"])


def test_label_phrase_unknown_falls_back_to_function():
    assert _label_phrase(["unknown_label"]) == "Function"
    assert _label_phrase([]) == "Function"


def test_domain_phrase_known_domains():
    assert "I/O" in _domain_phrase("io")
    assert "Network" in _domain_phrase("net")
    assert "lock" in _domain_phrase("lock").lower()


def test_domain_phrase_unknown():
    assert _domain_phrase("custom") == "custom path"
    assert _domain_phrase("") == ""
    assert _domain_phrase("root") == ""


def test_is_likely_builtin_python_stdlib():
    assert _is_likely_builtin("os.path.join")
    assert _is_likely_builtin("sys.exit")
    assert _is_likely_builtin("json.loads")
    assert _is_likely_builtin("PyList_Append")
    assert _is_likely_builtin("PyObject_GetAttr")


def test_is_likely_builtin_user_code():
    assert not _is_likely_builtin("my_custom_function")
    assert not _is_likely_builtin("load_full_graph")
    assert not _is_likely_builtin("")


def test_generate_heuristic_description_api_entry():
    node = {
        "name": "spdk_env_init",
        "fqn": "spdk_env_init",
        "labels": ["API_entry"],
        "domain": "init",
        "signature": "int spdk_env_init(struct spdk_env_opts *opts)",
        "body_text": "if (opts == NULL) return -1;\nreturn 0;",
        "params": [{"name": "opts", "type": "struct spdk_env_opts*"}],
        "source_file": "lib/env.c",
        "line": 42,
        "semantic_desc": "",
        "external_desc": "",
        "api_constraints": "",
        "preconditions": [],
        "postconditions": [],
    }
    out = generate_heuristic_description(node)
    assert "semantic_desc" in out
    assert "spdk_env_init" in out["semantic_desc"] or "Spdk env init" in out["semantic_desc"]
    assert "external_desc" in out
    assert "Public entry point" in out["external_desc"]
    assert "api_constraints" in out
    assert "opts != NULL" in out["api_constraints"]
    assert "preconditions" in out
    assert any("locks" in p.lower() for p in out["preconditions"])


def test_generate_heuristic_description_internal_function():
    node = {
        "name": "load_full_graph",
        "fqn": "load_full_graph",
        "labels": [],
        "domain": "_builder",
        "signature": "def _load_full_graph(graph_dir: str) -> nx.DiGraph:",
        "body_text": "if not os.path.exists(master_path):\n    return None\nreturn G",
        "source_file": "_builder/graph_build.py",
        "line": 671,
        "semantic_desc": "",
        "external_desc": "",
        "api_constraints": "",
        "preconditions": [],
        "postconditions": [],
    }
    out = generate_heuristic_description(node)
    assert "semantic_desc" in out
    assert "load full graph" in out["semantic_desc"].lower()
    # No API_entry label, so no external_desc
    assert "external_desc" not in out


def test_generate_heuristic_description_skips_filled_fields():
    node = {
        "name": "my_func",
        "fqn": "my_func",
        "labels": ["API_entry"],
        "domain": "io",
        "signature": "int my_func(int x)",
        "body_text": "return x + 1;",
        "params": [],
        "source_file": "src/foo.c",
        "line": 10,
        "semantic_desc": "Already described",
        "external_desc": "",
        "api_constraints": "",
        "preconditions": [],
        "postconditions": [],
    }
    out = generate_heuristic_description(node)
    # semantic_desc is already set, so it should not be regenerated
    assert "semantic_desc" not in out
    # external_desc is empty, so it should be generated
    assert "external_desc" in out


def test_generate_heuristic_description_external_node():
    node = {
        "name": "external_func",
        "fqn": "external_func",
        "labels": [],
        "domain": "",
        "signature": "",
        "body_text": "",
        "params": [],
        "source_file": "",
        "line": 0,
        "external": True,
        "semantic_desc": "",
        "external_desc": "",
        "api_constraints": "",
        "preconditions": [],
        "postconditions": [],
    }
    out = generate_heuristic_description(node)
    assert "semantic_desc" in out
    assert "external" in out["semantic_desc"].lower()


def test_generate_heuristic_description_lock_domain_postcondition():
    node = {
        "name": "acquire_lock",
        "fqn": "acquire_lock",
        "labels": [],
        "domain": "lock",
        "signature": "void acquire_lock(pthread_mutex_t *m)",
        "body_text": "pthread_mutex_lock(m);",
        "params": [{"name": "m", "type": "pthread_mutex_t*"}],
        "source_file": "lib/lock.c",
        "line": 10,
        "semantic_desc": "",
        "external_desc": "",
        "api_constraints": "",
        "preconditions": [],
        "postconditions": [],
    }
    out = generate_heuristic_description(node)
    assert "postconditions" in out
    assert any("released" in p.lower() for p in out["postconditions"])


def test_apply_heuristic_enhancement_dry_run(tmp_path):
    """Verify dry-run mode generates but does not write."""
    # Create a minimal graph directory with master.json
    graph_dir = tmp_path / "graph"
    graph_dir.mkdir()
    master = {"domains": {"_test": "_test.json"}, "source_root": ""}
    (graph_dir / "code2database_master.json").write_text(json.dumps(master))
    # Domain file with one function in compact tuple format:
    # [nid, name, source_file, line, labels_json, signature]
    domain_data = {
        "domain": "_test",
        "functions": [["test_func", "test_func", "test.c", 1, "[]",
                       "int test_func(int x)"]],
        "function_details": {"test_func": {
            "params": [{"name": "x", "type": "int"}],
            "body_text": "return x + 1;",
        }},
        "empty_nodes": [],
    }
    (graph_dir / "_test.json").write_text(json.dumps(domain_data))

    res = apply_heuristic_enhancement(str(graph_dir), "test_func", write=False)
    assert res["applied"] is False
    assert "semantic_desc" in res["generated"]


def test_apply_heuristic_enhancement_node_not_found(tmp_path):
    """Verify graceful handling when node doesn't exist."""
    graph_dir = tmp_path / "graph"
    graph_dir.mkdir()
    master = {"domains": {}, "source_root": ""}
    (graph_dir / "code2database_master.json").write_text(json.dumps(master))

    res = apply_heuristic_enhancement(str(graph_dir), "nonexistent_node")
    assert res["applied"] is False
    assert "not in graph" in res["skipped"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
