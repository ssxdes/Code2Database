#!/usr/bin/env python3
"""O24: Check that docs/en/ and docs/zh/ are in sync.

Compares the structure (headings, code blocks, CLI references) of the
English and Chinese documentation and reports discrepancies. This is a
structural check — it does NOT verify translation correctness, only that
both versions cover the same sections and CLI commands.

Usage:
    python3 scripts/check_docs_sync.py [--docs-dir docs]

Exit codes:
    0 = docs are in sync (or only cosmetic differences)
    1 = structural differences found
    2 = usage error
"""

import argparse
import re
import sys
from pathlib import Path


def extract_structure(text: str) -> dict:
    """Extract structural elements from a markdown doc.

    Returns a dict with:
      - headings: list of heading texts (without # prefix)
      - code_blocks: count of ``` fenced blocks
      - cli_commands: set of CLI command names mentioned. Catches
        ``code2database_*`` / ``cgdb_*`` MCP tool names (snake_case)
        AND ``code2database-*`` / ``cgdb-*`` CLI subcommand names
        (kebab-case). The legacy ``callgraph_*`` prefix is no longer
        used by this skill — replaced by code2database_* in v1.0.
      - sections: list of (level, title) tuples
    """
    headings = []
    sections = []
    code_blocks = 0
    cli_commands = set()
    in_code = False
    # Match both snake_case (MCP tools: code2database_load, cgdb_find_invoked)
    # and kebab-case (CLI subcommands: code2database-builder has subcommands
    # like kb-query, blast-radius, cgdb-time-travel).
    _CLI_NAME_RE = re.compile(
        r"\b((?:code2database|cgdb)[_\-][a-z][a-z0-9_\-]*)(?![\w])"
    )
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            code_blocks += 1
            continue
        if in_code:
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            headings.append(title)
            sections.append((level, title))
        for m in _CLI_NAME_RE.finditer(line):
            cli_commands.add(m.group(1))
    return {
        "headings": headings,
        "sections": sections,
        "code_blocks": code_blocks,
        "cli_commands": cli_commands,
    }


def compare_docs(en_path: Path, zh_path: Path) -> list:
    """Compare two docs and return a list of differences."""
    en_text = en_path.read_text(encoding="utf-8") if en_path.exists() else ""
    zh_text = zh_path.read_text(encoding="utf-8") if zh_path.exists() else ""
    diffs = []
    if not en_path.exists():
        diffs.append(f"  EN missing: {en_path}")
        return diffs
    if not zh_path.exists():
        diffs.append(f"  ZH missing: {zh_path}")
        return diffs
    en_struct = extract_structure(en_text)
    zh_struct = extract_structure(zh_text)
    # Compare heading counts (structural parity, not text equality)
    if len(en_struct["headings"]) != len(zh_struct["headings"]):
        diffs.append(
            f"  heading count mismatch: EN={len(en_struct['headings'])} "
            f"vs ZH={len(zh_struct['headings'])}"
        )
    # Compare section levels (heading hierarchy should match)
    en_levels = [lvl for lvl, _ in en_struct["sections"]]
    zh_levels = [lvl for lvl, _ in zh_struct["sections"]]
    if en_levels != zh_levels:
        diffs.append(
            f"  heading hierarchy mismatch: EN levels={en_levels[:10]}... "
            f"vs ZH levels={zh_levels[:10]}..."
        )
    # Compare code block counts
    if en_struct["code_blocks"] != zh_struct["code_blocks"]:
        diffs.append(
            f"  code block count mismatch: EN={en_struct['code_blocks']} "
            f"vs ZH={zh_struct['code_blocks']}"
        )
    # Compare CLI commands (set difference — commands only in one version)
    en_only = en_struct["cli_commands"] - zh_struct["cli_commands"]
    zh_only = zh_struct["cli_commands"] - en_struct["cli_commands"]
    if en_only:
        diffs.append(f"  CLI commands only in EN: {sorted(en_only)}")
    if zh_only:
        diffs.append(f"  CLI commands only in ZH: {sorted(zh_only)}")
    return diffs


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--docs-dir", default="docs",
                        help="Root docs directory (containing en/ and zh/)")
    args = parser.parse_args()

    docs_dir = Path(args.docs_dir)
    en_dir = docs_dir / "en"
    zh_dir = docs_dir / "zh"
    if not en_dir.is_dir() or not zh_dir.is_dir():
        print(f"Error: expected {en_dir} and {zh_dir} to exist", file=sys.stderr)
        sys.exit(2)

    # Root README.md is the canonical English counterpart for docs/zh/README.md
    # when docs/en/README.md is absent (common project layout).
    root_readme = docs_dir.parent / "README.md"

    all_diffs = []
    en_files = sorted(p for p in en_dir.glob("*.md"))
    for en_path in en_files:
        zh_path = zh_dir / en_path.name
        diffs = compare_docs(en_path, zh_path)
        if diffs:
            all_diffs.append((en_path.name, diffs))

    # Also check files only in zh/ (like README.md). If the EN counterpart is the
    # repo-root README.md, compare against that instead of flagging as missing.
    zh_only_files = sorted(p for p in zh_dir.glob("*.md") if not (en_dir / p.name).exists())
    for zh_path in zh_only_files:
        en_alt = root_readme if zh_path.name == "README.md" and root_readme.exists() else None
        if en_alt is not None:
            diffs = compare_docs(en_alt, zh_path)
            if diffs:
                all_diffs.append((zh_path.name, diffs))
        else:
            all_diffs.append((zh_path.name, [f"  EN missing: {en_dir / zh_path.name}"]))

    if not all_diffs:
        print(f"OK: docs/en/ and docs/zh/ are in sync ({len(en_files)} files checked)")
        sys.exit(0)

    print(f"Found differences in {len(all_diffs)} file(s):")
    for name, diffs in all_diffs:
        print(f"\n{name}:")
        for d in diffs:
            print(d)
    sys.exit(1)


if __name__ == "__main__":
    main()
