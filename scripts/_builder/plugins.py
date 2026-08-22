"""callgraph builder module: plugins."""

import os
import json
import sys
import re
from pathlib import Path
from collections import defaultdict
import networkx as nx
from _builder.utils import _output_result


def _discover_plugins(source_root: str) -> list:
    """Auto-discover plugins in .code2database_plugins/ directories."""
    plugin_paths = []
    # Check source_root/.code2database_plugins/
    plugins_dir = os.path.join(source_root, ".code2database_plugins")
    if os.path.isdir(plugins_dir):
        for fname in sorted(os.listdir(plugins_dir)):
            if fname.endswith(".py") and not fname.startswith("_"):
                plugin_paths.append(os.path.join(plugins_dir, fname))
    # Check outdir/.code2database_plugins/ (if different)
    return plugin_paths




def _load_plugins(plugin_paths: list, source_root: str = "") -> list:
    """Load CallgraphPlugin instances from given Python file paths.

    Each plugin file should define a class inheriting from CallgraphPlugin
    (or just implement enrich_functions and/or enrich_graph methods).
    Returns a list of (name, plugin_instance) tuples.
    """
    import importlib.util

    plugins = []
    for path in plugin_paths:
        if not os.path.exists(path):
            print(f"Warning: Plugin not found: {path}", file=sys.stderr)
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"code2database_plugin_{os.path.basename(path)}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Find the plugin class
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and hasattr(obj, 'enrich_functions'):
                    instance = obj()
                    plugins.append((attr_name, instance))
                    break
            else:
                # No class found — check for module-level functions
                if hasattr(mod, 'enrich_functions') or hasattr(mod, 'enrich_graph'):
                    plugins.append((os.path.basename(path), mod))
        except Exception as e:
            print(f"Warning: Failed to load plugin {path}: {e}", file=sys.stderr)
    return plugins




def _run_plugins(plugins: list, G: nx.DiGraph, extraction: dict = None) -> nx.DiGraph:
    """Run all loaded plugins against the graph."""
    for name, plugin in plugins:
        try:
            if hasattr(plugin, 'enrich_graph'):
                G = plugin.enrich_graph(G)
                if G is None:
                    print(f"Warning: Plugin {name}.enrich_graph() returned None, skipping", file=sys.stderr)
                    continue
            if hasattr(plugin, 'enrich_functions') and extraction:
                functions = extraction.get("functions", [])
                edges = extraction.get("edges", [])
                functions, edges = plugin.enrich_functions(functions, edges,
                                                           extraction.get("source_root", ""))
                if functions is not None:
                    extraction["functions"] = functions
                if edges is not None:
                    extraction["edges"] = edges
        except Exception as e:
            print(f"Warning: Plugin {name} failed: {e}", file=sys.stderr)
    return G




def cmd_plugins(args):
    """List available callgraph plugins."""
    source_root = args.source if args.source else "."
    plugins_dir = os.path.join(source_root, ".code2database_plugins")

    found = []
    if os.path.isdir(plugins_dir):
        for fname in sorted(os.listdir(plugins_dir)):
            if fname.endswith(".py") and not fname.startswith("_"):
                fpath = os.path.join(plugins_dir, fname)
                # Try to read docstring
                desc = ""
                try:
                    with open(fpath, 'r') as f:
                        for line in f:
                            if line.strip().startswith('"""') or line.strip().startswith("'''"):
                                desc = line.strip().strip('"').strip("'")
                                break
                            if line.strip().startswith('#'):
                                desc = line.strip().lstrip('#').strip()
                                break
                except IOError:
                    pass
                found.append({"file": fname, "path": fpath, "description": desc})

    # Also check --plugin paths
    if args.plugin:
        for p in args.plugin:
            if os.path.exists(p) and p not in [f["path"] for f in found]:
                found.append({"file": os.path.basename(p), "path": p,
                               "description": "explicitly specified"})

    if not found:
        print("No plugins found.")
        print(f"Place .py files in {plugins_dir}/ or use --plugin <path>")
        return

    print(json.dumps(found, ensure_ascii=False, indent=2))




def cmd_validate_plugin(args):
    """Validate a plugin file for interface compliance and edge format rules."""
    plugin_path = args.plugin
    if not os.path.isfile(plugin_path):
        print(f"Plugin file not found: {plugin_path}", file=sys.stderr)
        sys.exit(1)

    issues = []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("test_plugin", plugin_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(json.dumps({"valid": False, "errors": [f"Import failed: {e}"]}, indent=2))
        return

    # Check for CallgraphPlugin class
    if not hasattr(mod, 'CallgraphPlugin'):
        issues.append("Missing 'CallgraphPlugin' class")
    else:
        cls = mod.CallgraphPlugin
        required = ['enrich_functions', 'enrich_graph']
        for method in required:
            if not hasattr(cls, method):
                issues.append(f"Missing required method: {method}")

    result = {
        "plugin": plugin_path,
        "valid": len(issues) == 0,
        "errors": issues,
        "has_custom_scan": hasattr(mod.CallgraphPlugin, 'custom_scan') if hasattr(mod, 'CallgraphPlugin') else False,
        "has_custom_query": hasattr(mod.CallgraphPlugin, 'custom_query') if hasattr(mod, 'CallgraphPlugin') else False,
        "has_custom_output": hasattr(mod.CallgraphPlugin, 'custom_output') if hasattr(mod, 'CallgraphPlugin') else False,
    }
    print(json.dumps(result, indent=2))


