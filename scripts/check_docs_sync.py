#!/usr/bin/env python3
"""O24: Check that docs/en/ and docs/zh/ are in sync.

Compares the structure (headings, code blocks, CLI references) of the
English and Chinese documentation and reports discrepancies. This is a
structural check — it does NOT verify translation correctness, only that
both versions cover the same sections and CLI commands.

Additionally checks the English tree for untranslated CJK prose: CJK
text outside fenced code blocks and inline code spans, and outside the
explicit allowlist of deliberate bilingual terms, is reported.

Usage:
    python3 scripts/check_docs_sync.py [--docs-dir docs]

Exit codes:
    0 = docs are in sync (or only cosmetic differences)
    1 = structural or language differences found
    2 = usage error
"""

import argparse
import re
import sys
from pathlib import Path

# CJK scripts (Hiragana, Katakana, CJK ideograph blocks). Used to spot
# untranslated prose leaked into the English tree.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
                     r"\uf900-\ufaff\ufeff]+")

# Deliberate bilingual content in the English tree: official layer names
# quoted from the Chinese design document, and table cells that show
# Chinese example payloads (memory Q&A is user-facing i18n content).
# Anything CJK outside code spans NOT covered here is a finding.
_EN_CJK_ALLOWLIST = {
    "OVERVIEW.md": (
        "无损重建层",
        "AST 层",
        "IR 层",
        "派生层",
        "Report-多库",
        "Report-跨语言",
    ),
    "references/memory_knowledge.md": (
        "强制开启 SPDK_CONFIG_PCI 宏",
    ),
}


def strip_code_spans(line: str) -> str:
    """Remove inline `code` spans from a prose line."""
    return re.sub(r"`[^`]*`", "", line)


def find_cjk_in_prose(text: str):
    """Yield (line_number, cjk_text) for CJK outside fenced/inline code.

    Fenced code blocks are skipped wholesale (they are example data);
    inline code spans are removed from prose lines before scanning.
    """
    in_code = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        prose = strip_code_spans(line)
        m = _CJK_RE.search(prose)
        if m:
            yield lineno, m.group(0)


def check_english_language(en_dir: Path) -> list:
    """Report untranslated CJK prose in the English docs tree."""
    findings = []
    for en_path in sorted(en_dir.rglob("*.md")):
        rel = str(en_path.relative_to(en_dir))
        allowed = _EN_CJK_ALLOWLIST.get(rel, ())
        lines = en_path.read_text(encoding="utf-8").splitlines()
        for lineno, cjk in find_cjk_in_prose("\n".join(lines)):
            line = lines[lineno - 1]
            if any(term in line for term in allowed):
                continue
            findings.append(
                f"  untranslated CJK at {rel}:{lineno}: ...{line.strip()}..."
            )
    return findings



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
    # Recursive glob: check all .md files including references/ subdirectory.
    en_files = sorted(p for p in en_dir.rglob("*.md"))
    for en_path in en_files:
        # Preserve relative subpath (e.g. references/foo.md) for pairing.
        rel = en_path.relative_to(en_dir)
        zh_path = zh_dir / rel
        diffs = compare_docs(en_path, zh_path)
        if diffs:
            all_diffs.append((str(rel), diffs))

    # Also check files only in zh/ (like README.md). If the EN counterpart is the
    # repo-root README.md, compare against that instead of flagging as missing.
    zh_only_files = sorted(
        p for p in zh_dir.rglob("*.md")
        if not (en_dir / p.relative_to(zh_dir)).exists()
    )
    for zh_path in zh_only_files:
        rel = zh_path.relative_to(zh_dir)
        en_alt = root_readme if rel == Path("README.md") and root_readme.exists() else None
        if en_alt is not None:
            diffs = compare_docs(en_alt, zh_path)
            if diffs:
                all_diffs.append((str(rel), diffs))
        else:
            all_diffs.append((str(rel), [f"  EN missing: {en_dir / rel}"]))

    # Language check: the English tree must not carry untranslated
    # CJK prose (fenced blocks, inline code, and the explicit
    # allowlist of deliberate bilingual terms are exempt).
    language_findings = check_english_language(en_dir)
    if language_findings:
        all_diffs.append(("english-tree language check", language_findings))

    if not all_diffs:
        print(f"OK: docs/en/ and docs/zh/ are in sync ({len(en_files) + len(zh_only_files)} files checked)")
        sys.exit(0)

    print(f"Found differences in {len(all_diffs)} file(s):")
    for name, diffs in all_diffs:
        print(f"\n{name}:")
        for d in diffs:
            print(d)
    sys.exit(1)


if __name__ == "__main__":
    main()
