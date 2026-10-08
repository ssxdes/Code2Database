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


# ---------------------------------------------------------------------------
# Number cross-check: docs cite counts (MCP tools, sub-skills, tier-1
# commands, CLI spellings) that are derived from code and manifests.
# Both language trees can carry the same stale number ("mutually in
# sync but both wrong"), so structural parity alone never catches the
# drift — every cited number is validated against its canonical source.
# ---------------------------------------------------------------------------

def _sniff_subcommands(repo_root: Path, entry_script: str) -> set:
    """Instantiate a CLI entry script and capture its subparser names."""
    import argparse
    import contextlib
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location(
        "_number_probe_" + Path(entry_script).stem,
        repo_root / "scripts" / entry_script)
    mod = importlib.util.module_from_spec(spec)
    captured = set()
    orig = argparse.ArgumentParser.parse_known_args

    def sniff(self, args=None, namespace=None):
        for act in self._actions:
            if isinstance(act, argparse._SubParsersAction):
                captured.update(act.choices)
        return orig(self, args, namespace)

    argparse.ArgumentParser.parse_known_args = sniff
    old_argv = sys.argv[:]
    try:
        sys.argv = [entry_script, "--help"]
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                spec.loader.exec_module(mod)
                mod.main()
            except SystemExit:
                pass
    finally:
        argparse.ArgumentParser.parse_known_args = orig
        sys.argv = old_argv
    return captured


def _canonical_numbers(repo_root: Path) -> dict:
    """Numbers docs cite, derived from the code and manifests."""
    import json as _json

    scripts_dir = str(repo_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from _builder.mcp.mcp_server import TOOLS, TOOLS_REPORT
    from _builder.umbrella import _umbrella_legacy_names

    builder = _sniff_subcommands(repo_root, "code2database_builder.py")
    scanner = _sniff_subcommands(repo_root, "code2database_scanner.py")
    if not builder or not scanner:
        raise RuntimeError("CLI introspection came up empty")

    tier = {}
    for fname, key in (("skill.json", "core"),
                       ("skill_analysis.json", "analysis"),
                       ("skill_ops.json", "ops"),
                       ("skill_kb.json", "kb")):
        manifest = _json.loads(
            (repo_root / fname).read_text(encoding="utf-8"))
        tier[key] = len(manifest["tier_1_commands"])

    total = len(TOOLS)
    report = len(TOOLS_REPORT)
    legacy = len(_umbrella_legacy_names())
    return {
        "total": total,
        "c2d": sum(1 for k in TOOLS if k.startswith("code2database_")),
        "cgdb": sum(1 for k in TOOLS if k.startswith("cgdb_")),
        "report": report,
        "base": total - report,
        "sub_skills": len(tier),
        "tier": tier,
        "spellings": len(builder),
        "visible": len(builder) - legacy,
        "scanner": len(scanner),
    }


# (regex, canonical key) pairs where the number is unambiguous.
# The tools/subcommands rules need window context (cgdb- or
# scanner-scoped counts) and are handled separately below.
_NUMBER_RULES = [
    (re.compile(r'(\d+)\s*(?:个\s*)?`?code2database_\*`?'), "c2d"),
    (re.compile(r'(\d+)\s*(?:个\s*)?`?cgdb_\*`?'), "cgdb"),
    (re.compile(r'(\d+)\s*design-report'), "report"),
    (re.compile(r'sub_skills-(\d+)'), "sub_skills"),
    (re.compile(r'(\d+)\s+sub-skills'), "sub_skills"),
    (re.compile(r'(\d+)\s*个子 skill'), "sub_skills"),
    (re.compile(r'(\d+)\s+(?:CLI\s+)?spellings'), "spellings"),
    (re.compile(r'(\d+)\s*个 CLI 拼写'), "spellings"),
    (re.compile(r'(\d+)\s+visible\b'), "visible"),
    (re.compile(r'(\d+)_visible'), "visible"),
    (re.compile(r'(\d+)\s*(?:个\s*)?可见'), "visible"),
    (re.compile(r'(\d+)_可见'), "visible"),
]

# A tier-1 count belongs to the sub-skill named nearest before it.
_TIER_KEYWORDS = [
    ("analysis", "analysis"), ("分析", "analysis"),
    ("ops", "ops"), ("运维", "ops"),
    ("kb", "kb"), ("知识库", "kb"),
    ("core", "core"), ("核心", "core"),
]
_TIER_RE = re.compile(r'(\d+)\s*(?:个\s*)?Tier-1')
_TOOLS_RE = re.compile(r'(\d+)\s*(?:个工具|tools\b)')
_SUBCMD_RE = re.compile(r'(\d+)\s+subcommands')
_SCANNER_SUBCMD_RE = re.compile(r'(\d+)\s+scanner\s+subcommands')
_ZH_SUBCMD_RE = re.compile(r'(\d+)\s*个子命令')
_BASE_RE = re.compile(r'(\d+)\s*base\s*\+\s*(\d+)\s*design-report')


def _nearest_tier_key(window: str):
    best_pos, best_key = -1, None
    lowered = window.lower()
    for needle, key in _TIER_KEYWORDS:
        pos = lowered.rfind(needle.lower()) if needle.isascii() \
            else window.rfind(needle)
        if pos > best_pos:
            best_pos, best_key = pos, key
    return best_key


def _number_findings_for_line(rel, lineno, line, canon):
    findings = []
    # tuple spans first: "55 base + 28 design-report" is validated as a
    # pair, so the plain design-report rule skips covered matches
    tuple_spans = [m.span(2) for m in _BASE_RE.finditer(line)]
    for rule_re, key in _NUMBER_RULES:
        for m in rule_re.finditer(line):
            if key == "report" and any(s <= m.start() < e
                                       for s, e in tuple_spans):
                continue
            if int(m.group(1)) != canon[key]:
                findings.append(
                    f"  {rel}:{lineno}: cites {m.group(1)} for {key} "
                    f"(canonical {canon[key]}): ...{line.strip()[:70]}...")
    for m in _TOOLS_RE.finditer(line):
        window = line[max(0, m.start() - 60):m.start()].lower()
        expected = canon["cgdb"] if "cgdb" in window else canon["total"]
        if int(m.group(1)) != expected:
            scope = "cgdb tools" if "cgdb" in window else "total tools"
            findings.append(
                f"  {rel}:{lineno}: cites {m.group(1)} for {scope} "
                f"(canonical {expected}): ...{line.strip()[:70]}...")
    for m in _BASE_RE.finditer(line):
        for value, key in ((m.group(1), "base"), (m.group(2), "report")):
            if int(value) != canon[key]:
                findings.append(
                    f"  {rel}:{lineno}: cites {value} for {key} tools "
                    f"(canonical {canon[key]}): ...{line.strip()[:70]}...")
    for m in _SCANNER_SUBCMD_RE.finditer(line):
        if int(m.group(1)) != canon["scanner"]:
            findings.append(
                f"  {rel}:{lineno}: cites {m.group(1)} for scanner "
                f"subcommands (canonical {canon['scanner']})")
    for m in _SUBCMD_RE.finditer(line):
        window = line[max(0, m.start() - 60):m.start()].lower()
        if "scanner" in window:
            expected = canon["scanner"]
            scope = "scanner subcommands"
        else:
            expected = canon["spellings"]
            scope = "builder subcommands"
        if int(m.group(1)) != expected:
            findings.append(
                f"  {rel}:{lineno}: cites {m.group(1)} for {scope} "
                f"(canonical {expected})")
    for m in _ZH_SUBCMD_RE.finditer(line):
        window = line[max(0, m.start() - 60):m.start()].lower()
        if ("scanner" in window or "扫描器" in window) \
                and int(m.group(1)) != canon["scanner"]:
            findings.append(
                f"  {rel}:{lineno}: cites {m.group(1)} for scanner "
                f"subcommands (canonical {canon['scanner']})")
    for m in _TIER_RE.finditer(line):
        window = line[max(0, m.start() - 80):m.start()]
        key = _nearest_tier_key(window)
        if key is None:
            continue
        if int(m.group(1)) != canon["tier"][key]:
            findings.append(
                f"  {rel}:{lineno}: cites {m.group(1)} for {key} tier-1 "
                f"commands (canonical {canon['tier'][key]}): "
                f"...{line.strip()[:70]}...")
    return findings


def check_numbers(repo_root: Path) -> list:
    """Cross-check numbers cited in docs against canonical sources."""
    try:
        canon = _canonical_numbers(repo_root)
    except Exception as exc:  # canonical unavailable: report, do not skip
        return [f"  number cross-check unavailable: {exc}"]

    targets = [repo_root / "README.md", repo_root / "AGENTS.md",
               repo_root / "install.sh"]
    docs_dir = repo_root / "docs"
    targets += sorted((docs_dir / "en").rglob("*.md"))
    targets += sorted((docs_dir / "zh").rglob("*.md"))

    findings = []
    for path in targets:
        if not path.exists():
            continue
        rel = str(path.relative_to(repo_root))
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace")
                .splitlines(), start=1):
            findings.extend(
                _number_findings_for_line(rel, lineno, line, canon))
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

    # Number check: cited counts must match the code/manifest sources.
    number_findings = check_numbers(docs_dir.parent.resolve())
    if number_findings:
        all_diffs.append(("cited numbers vs canonical sources",
                          number_findings))

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
