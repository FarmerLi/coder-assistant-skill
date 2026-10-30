"""验证链接检查的结果，避免将文档措辞当作技能行为测试。"""

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_links.py"
SPEC = importlib.util.spec_from_file_location("check_links", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class LinkCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_resolves_relative_files_and_unicode_encoded_headings(self):
        self.write("SKILL.md", "[说明](references/%E8%AF%B4%E6%98%8E.md#开发-验证)\n")
        self.write("references/说明.md", "# 开发 验证\n")
        self.assertEqual(checker.check_package(self.root), [])

    def test_missing_file_reports_source_and_line(self):
        self.write("SKILL.md", "# 技能\n[说明](missing.md)\n")
        errors = checker.check_package(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("SKILL.md:2", errors[0])
        self.assertIn("missing.md", errors[0])

    def test_missing_heading_is_an_error(self):
        self.write("SKILL.md", "[说明](说明.md#不存在)\n")
        self.write("说明.md", "# 实际标题\n")
        self.assertEqual(len(checker.check_package(self.root)), 1)

    def test_local_fragment_and_duplicate_heading_suffix(self):
        self.write("SKILL.md", "# 范围\n# 范围\n[第二节](#范围-1)\n")
        self.assertEqual(checker.check_package(self.root), [])

    def test_code_examples_and_external_urls_are_not_followed(self):
        self.write("SKILL.md", "```md\n[示例](missing.md)\n```\n`[例](missing.md)`\n[外部](https://example.com/a)\n[邮件](mailto:x@example.com)\n")
        self.assertEqual(checker.check_package(self.root), [])

    def test_angle_bracket_target_supports_spaces(self):
        self.write("SKILL.md", "[说明](<有 空格.md#标题>)\n")
        self.write("有 空格.md", "# 标题\n")
        self.assertEqual(checker.check_package(self.root), [])

    def test_cannot_escape_package_even_if_file_exists(self):
        with tempfile.TemporaryDirectory() as container:
            outer = Path(container)
            (outer / "outside.md").write_text("# 外部\n", encoding="utf-8")
            root = outer / "package"
            root.mkdir()
            (root / "SKILL.md").write_text("[外部](../outside.md)\n", encoding="utf-8")
            self.assertEqual(len(checker.check_package(root)), 1)

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.md"
            target.write_text("# 外部\n", encoding="utf-8")
            (self.root / "说明.md").symlink_to(target)
            self.write("SKILL.md", "[说明](说明.md)\n")
            self.assertEqual(len(checker.check_package(self.root)), 1)

    def test_heading_inside_fence_does_not_satisfy_fragment(self):
        self.write("SKILL.md", "[例](#假标题)\n~~~md\n# 假标题\n~~~\n")
        self.assertEqual(len(checker.check_package(self.root)), 1)

    def test_reports_each_broken_reference(self):
        self.write("SKILL.md", "[一](a.md) [二](b.md)\n")
        self.assertEqual(len(checker.check_package(self.root)), 2)


if __name__ == "__main__":
    unittest.main()
