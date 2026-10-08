"""Number cross-check: doc-cited counts match canonical sources.

Both language trees can carry the same stale number ("mutually in
sync but both wrong"), so structural parity never catches the drift.
These tests pin the checker itself: the real corpus passes clean,
and every historically-drifted shape is caught with the right key.
"""
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from check_docs_sync import (  # noqa: E402
    _canonical_numbers, _number_findings_for_line, check_numbers,
)


class CanonicalNumbersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canon = _canonical_numbers(REPO)

    def test_canonical_matches_pinned_values(self):
        self.assertEqual(self.canon["total"], 83)
        self.assertEqual(self.canon["c2d"], 36)
        self.assertEqual(self.canon["cgdb"], 19)
        self.assertEqual(self.canon["report"], 28)
        self.assertEqual(self.canon["base"], 55)
        self.assertEqual(self.canon["sub_skills"], 4)
        self.assertEqual(self.canon["spellings"], 275)
        self.assertEqual(self.canon["visible"], 120)
        self.assertEqual(self.canon["scanner"], 8)
        self.assertEqual(self.canon["tier"],
                         {"core": 21, "analysis": 12, "ops": 9, "kb": 6})

    def test_real_corpus_has_no_stale_numbers(self):
        self.assertEqual(check_numbers(REPO), [])


class LineRuleTest(unittest.TestCase):
    """Each rule catches its drifted shape and passes its correct one."""

    CANON = {"total": 83, "c2d": 36, "cgdb": 19, "report": 28,
             "base": 55, "sub_skills": 4, "spellings": 275,
             "visible": 120, "scanner": 8,
             "tier": {"core": 21, "analysis": 12, "ops": 9, "kb": 6}}

    def _keys(self, line):
        findings = _number_findings_for_line("t.md", 1, line, self.CANON)
        labels = []
        for f in findings:
            # "t.md:1: cites 13 for analysis tier-1 commands
            #  (canonical 12): ..." -> "analysis tier-1 commands"
            labels.append(f.split(" for ", 1)[1].split(" (", 1)[0])
        return sorted(labels)

    def test_tool_totals(self):
        self.assertEqual(self._keys("serve exposes 83 tools"), [])
        self.assertEqual(self._keys("serve exposes 82 tools"),
                         ["total tools"])
        self.assertEqual(self._keys("启动服务器（83 个工具）"), [])
        self.assertEqual(self._keys("启动服务器（82 个工具）"),
                         ["total tools"])

    def test_cgdb_scoped_tool_count_uses_context(self):
        self.assertEqual(
            self._keys("cgdb MCP Tools (clang backend — 19 tools)"), [])
        self.assertEqual(
            self._keys("cgdb MCP Tools (clang backend — 18 tools)"),
            ["cgdb tools"])

    def test_label_adjacent_counts(self):
        ok = ("83 tools: 36 `code2database_*` + 19 `cgdb_*` "
              "+ 28 design-report")
        self.assertEqual(self._keys(ok), [])
        self.assertEqual(
            self._keys("82 tools: 35 `code2database_*` + 19 `cgdb_*`"),
            ["c2d", "total tools"])

    def test_base_and_report_tuple(self):
        self.assertEqual(self._keys("(55 base + 28 design-report)"), [])
        self.assertEqual(self._keys("(54 base + 28 design-report)"),
                         ["base tools"])
        self.assertEqual(self._keys("(55 base + 27 design-report)"),
                         ["report tools"])

    def test_sub_skill_counts(self):
        self.assertEqual(self._keys("[![Sub-skills]"
                                    "(badge/sub_skills-4-9cf)]"), [])
        self.assertEqual(self._keys("[![Sub-skills]"
                                    "(badge/sub_skills-3-9cf)]"),
                         ["sub_skills"])
        self.assertEqual(self._keys("The skill ships as 4 sub-skills"), [])
        self.assertEqual(self._keys("skill 以 4 个子 skill 形式发布"), [])
        self.assertEqual(self._keys("skill 以 3 个子 skill 形式发布"),
                         ["sub_skills"])

    def test_cli_spelling_counts(self):
        self.assertEqual(self._keys("All 275 CLI spellings parse"), [])
        self.assertEqual(self._keys("全部 275 个 CLI 拼写都可解析"), [])
        self.assertEqual(self._keys("All 263 CLI spellings parse"),
                         ["spellings"])
        self.assertEqual(self._keys("275 spellings incl. hidden legacy"), [])
        self.assertEqual(self._keys("255 spellings incl. hidden legacy"),
                         ["spellings"])

    def test_visible_counts(self):
        self.assertEqual(self._keys("(120 visible umbrella)"), [])
        self.assertEqual(self._keys("120 visible CLI commands"), [])
        self.assertEqual(self._keys("query_commands-120_visible-success"), [])
        self.assertEqual(self._keys("查询命令-120_可见-success"), [])
        self.assertEqual(self._keys("119 visible CLI commands"),
                         ["visible"])

    def test_subcommand_counts_with_scanner_context(self):
        self.assertEqual(self._keys("plus 8 scanner subcommands"), [])
        self.assertEqual(self._keys("plus 9 scanner subcommands"),
                         ["scanner subcommands"])
        self.assertEqual(
            self._keys("scanner entry point （8 个子命令）"), [])
        self.assertEqual(self._keys("scanner entry point （9 个子命令）"),
                         ["scanner subcommands"])
        self.assertEqual(
            self._keys("builder CLI, 275 subcommands — 120 visible"), [])
        self.assertEqual(
            self._keys("builder CLI, 263 subcommands — 120 visible"),
            ["builder subcommands"])

    def test_tier_counts_use_nearest_sub_skill(self):
        self.assertEqual(
            self._keys("| `Code2Database-analysis` | ... | "
                       "12 Tier-1 commands |"), [])
        self.assertEqual(
            self._keys("| `Code2Database-analysis` | ... | "
                       "13 Tier-1 commands |"),
            ["analysis tier-1 commands"])
        self.assertEqual(self._keys("分析（12 个 Tier-1）"), [])
        self.assertEqual(self._keys("运维（23 个 Tier-1 命令）"),
                         ["ops tier-1 commands"])
        self.assertEqual(self._keys("知识库 6 个 Tier-1 命令"), [])
        self.assertEqual(self._keys("kb 8 Tier-1 commands"),
                         ["kb tier-1 commands"])
        self.assertEqual(self._keys("Core (22 Tier-1)"),
                         ["core tier-1 commands"])
        self.assertEqual(
            self._keys("核心 21、分析 12、运维 9、知识库 6 个 Tier-1"), [])
        # Enumeration lines only validate the count adjacent to the
        # "Tier-1" label; mid-enumeration counts (分析 13) sit too far
        # from the label for a low-false-positive rule.
        self.assertEqual(
            self._keys("核心 21、分析 13、运维 9、知识库 6 个 Tier-1"),
            [])
        self.assertEqual(
            self._keys("核心 21、分析 12、运维 9、知识库 8 个 Tier-1"),
            ["kb tier-1 commands"])

    def test_tier_without_sub_skill_context_is_skipped(self):
        self.assertEqual(self._keys("5 Tier-1 command families exist"), [])


if __name__ == "__main__":
    unittest.main()
