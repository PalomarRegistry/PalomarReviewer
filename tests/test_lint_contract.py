import pathlib
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LINT_COMMAND = (
    "uv run --isolated --python 3.11.10 --locked --only-group dev ruff check ."
)


class LintContractTests(unittest.TestCase):
    def test_ci_and_readme_run_the_reviewed_lint_environment(self):
        for relative in (".github/workflows/ci.yml", "README.md"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(LINT_COMMAND, text)

    def test_the_required_rule_set_cannot_silently_narrow(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["tool"]["ruff"]["lint"]["select"], ["E", "F", "I", "UP", "B"])
        self.assertEqual(project["dependency-groups"]["dev"], ["ruff==0.16.2"])


if __name__ == "__main__":
    unittest.main()
