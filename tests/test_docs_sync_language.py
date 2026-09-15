"""check_docs_sync language guard: the English tree stays English.

The en/zh parity check compares structure only, so untranslated CJK
prose leaked into docs/en/ (leftover Chinese from translation) was
invisible to CI. The language check closes that blind spot: CJK outside
fenced code blocks, inline code spans, and the explicit per-file
allowlist of deliberate bilingual terms is a finding.
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from check_docs_sync import (  # noqa: E402
    check_english_language,
    find_cjk_in_prose,
)


class TestFindCjkInProse(unittest.TestCase):

    def test_plain_prose_cjk_detected(self):
        hits = list(find_cjk_in_prose("plain knowledge底蕴 leak\n"))
        self.assertEqual(hits, [(1, "底蕴")])

    def test_fenced_block_cjk_ignored(self):
        text = "before\n```bash\necho 中文示例\n```\nafter\n"
        self.assertEqual(list(find_cjk_in_prose(text)), [])

    def test_inline_code_span_cjk_ignored(self):
        text = "see `C代码数据库化方案.md` for details\n"
        self.assertEqual(list(find_cjk_in_prose(text)), [])

    def test_cjk_after_inline_span_still_detected(self):
        text = "see `code` then 知识底蕴 leaks\n"
        hits = list(find_cjk_in_prose(text))
        self.assertEqual(hits, [(1, "知识底蕴")])

    def test_unbalanced_fence_keeps_scanning(self):
        # A lone fence opener flips to code mode; a second one flips
        # back — prose after it must still be scanned.
        text = "```\ncode\n```\n知识底蕴\n"
        self.assertEqual(list(find_cjk_in_prose(text)),
                         [(4, "知识底蕴")])


class TestCheckEnglishLanguage(unittest.TestCase):

    def _run(self, files):
        with tempfile.TemporaryDirectory() as d:
            en = Path(d) / "en"
            en.mkdir()
            for rel, content in files.items():
                p = en / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            return check_english_language(en)

    def test_leak_reported_with_location(self):
        findings = self._run({
            "SKILL.md": "# T\n\nprose with knowledge底蕴 leak\n",
        })
        self.assertEqual(len(findings), 1)
        self.assertIn("SKILL.md:3", findings[0])
        self.assertIn("底蕴", findings[0])

    def test_allowlist_is_per_file(self):
        # "Report-多库" is deliberately bilingual in OVERVIEW.md only;
        # the same term in another file is still a finding.
        self.assertEqual(
            self._run({"OVERVIEW.md": "Report-多库 term\n"}), [])
        self.assertEqual(
            len(self._run({"SKILL.md": "Report-多库 term\n"})), 1)

    def test_multiple_leaks_all_reported(self):
        findings = self._run({
            "a.md": "知识 one\n",
            "sub/b.md": "知识 two\n",
        })
        self.assertEqual(len(findings), 2)

    def test_repo_english_tree_is_clean(self):
        """The shipped English tree passes the language check.

        This pins the current allowlist against real docs: a new
        untranslated leak (or a stale allowlist entry whose doc line
        was reworded) fails here and in CI via check_docs_sync.py.
        """
        findings = check_english_language(REPO / "docs" / "en")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
