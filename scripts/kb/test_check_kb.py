"""Focused standard-library tests for the local KB checker."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_kb


class CheckKBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.write(
            ".repo-kb.json",
            json.dumps(
                {
                    "include": [
                        "docs/**/*.md",
                        "README.md",
                        "MCP/README.md",
                        "resources/help.md",
                    ]
                }
            ),
        )
        self.write(
            "README.md",
            "[Docs](docs/README.md) [MCP](MCP/README.md) " "[Resources](resources/README.md)",
        )
        self.write("docs/README.md", "[Feature](feature.md)")
        self.write("docs/feature.md", "**Code:** `src/feature.py`, [config](config.json)<br>")
        self.write("MCP/README.md", "# MCP")
        self.write("resources/README.md", "[Help](help.md)")
        self.write("resources/help.md", "# Help")
        self.write("resources/model/config.json", "{}")
        self.write("src/feature.py", "print('feature')")
        self.write("config.json", "{}")
        self.write("docs/build/ignored.md", "**Code:** `absent.py`")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def git(self, *args):
        return check_kb.git(self.root, *args)

    def baseline(self):
        self.git("init", "-q")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=KB Test",
            "-c",
            "user.email=kb@example.invalid",
            "commit",
            "-qm",
            "baseline",
        )

    def test_positive_and_generated_resources(self):
        errors, coverage, _ = check_kb.check(self.root)
        self.assertEqual(errors, [])
        self.assertEqual(coverage, {Path("src/feature.py"), Path("config.json")})

    def test_multiple_tokens_and_fenced_example(self):
        text = (
            "```md\n**Code:** `ignore.py`\n```\n"
            "**Code:** `file with spaces.py`, [two](src/two.py); src\\three.py<br>\n"
        )
        self.assertEqual(
            check_kb.code_paths(text),
            [
                (4, "file with spaces.py"),
                (4, "src/two.py"),
                (4, "src\\three.py"),
            ],
        )

    def test_missing_and_invalid_paths_are_not_rescued(self):
        self.write("docs/feature.md", "**Code:** `feature.py`, `../src/feature.py`, `C:\\file.py`")
        errors, coverage, _ = check_kb.check(self.root)
        self.assertEqual(len(errors), 3)
        self.assertEqual(coverage, set())
        self.assertIn("does not exist", errors[0])
        self.assertIn("invalid Code path", errors[1])

    def test_missing_page_and_subfolder_links(self):
        self.write("docs/new.md", "# New")
        self.write("docs/nested/README.md", "# Nested")
        self.write("docs/empty/.keep", "")
        errors, _, _ = check_kb.check(self.root)
        self.assertTrue(any("new.md" in error for error in errors))
        self.assertTrue(any("nested" in error for error in errors))
        self.assertTrue(any("empty" in error for error in errors))

    def test_nondefault_guide_siblings_and_root_pages(self):
        self.write("MCP/extra.md", "# Extra")
        self.write("ROOT_GUIDE.md", "# Root")
        errors, _, _ = check_kb.check(self.root)
        self.assertTrue(any("extra.md" in error for error in errors))
        self.assertTrue(any("ROOT_GUIDE.md" in error for error in errors))

    def test_diff_parent_coverage_and_uncovered_file(self):
        self.write("docs/feature.md", "**Code:** `src/`")
        self.baseline()
        self.write("src/feature.py", "print('changed')")
        self.write("outside.py", "print('new')")
        self.write("build/generated.py", "print('generated')")
        self.git("add", ".")
        errors, coverage, _ = check_kb.check(self.root)
        self.assertEqual(errors, [])
        warnings = check_kb.diff_warnings(self.root, "HEAD", coverage)
        self.assertEqual(len(warnings), 1)
        self.assertIn("outside.py", warnings[0])

    def test_rename_and_delete_keep_old_named_references(self):
        self.baseline()
        self.git("mv", "src/feature.py", "src/renamed.py")
        (self.root / "config.json").unlink()
        self.write("docs/feature.md", "**Code:** `src/renamed.py`")
        errors, coverage, _ = check_kb.check(self.root)
        self.assertEqual(errors, [])
        warnings = check_kb.diff_warnings(self.root, "HEAD", coverage)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("feature.py" in warning for warning in warnings))
        self.assertTrue(any("config.json" in warning for warning in warnings))

    def test_exit_status_and_invalid_ref(self):
        with (
            patch.object(check_kb, "check", return_value=([], set(), 1)),
            patch.object(check_kb, "diff_warnings", return_value=["uncovered file"]),
        ):
            self.assertEqual(check_kb.main(["--diff", "HEAD"]), 0)
        with patch.object(check_kb, "check", return_value=(["missing path"], set(), 1)):
            self.assertEqual(check_kb.main([]), 1)
        self.baseline()
        with self.assertRaises(subprocess.CalledProcessError):
            check_kb.diff_warnings(self.root, "missing-ref", set())

    def test_deleted_named_folder(self):
        self.write("docs/feature.md", "**Code:** `src/`")
        self.baseline()
        (self.root / "src/feature.py").unlink()
        (self.root / "src").rmdir()
        self.write("docs/feature.md", "# Removed feature")
        errors, coverage, _ = check_kb.check(self.root)
        self.assertEqual(errors, [])
        warnings = check_kb.diff_warnings(self.root, "HEAD", coverage)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Code path: src", warnings[0])


if __name__ == "__main__":
    unittest.main()
