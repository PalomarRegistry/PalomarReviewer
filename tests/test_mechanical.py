import tempfile
import unittest
from pathlib import Path

from palomar_reviewer import mechanical
from palomar_reviewer.errors import ReviewerError


class MechanicalPathContractTests(unittest.TestCase):
    def test_repository_paths_accept_only_relative_posix_components(self):
        self.assertEqual(
            mechanical.safe_repository_path("examples/project/Task.lean", "source"),
            "examples/project/Task.lean",
        )
        for path in (
            "",
            "/Task.lean",
            "../Task.lean",
            "examples//Task.lean",
            "examples/./Task.lean",
            "C:/Task.lean",
            "Task.lean?raw=1",
            "Task.lean#fragment",
            "examples\\Task.lean",
            "Task.lean\x00",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ReviewerError, "safe repository-relative"):
                    mechanical.safe_repository_path(path, "source")

    def test_source_path_rejects_a_symlinked_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "Task.lean").write_text("theorem task : True := by trivial\n")
            source = root / "source"
            source.mkdir()
            (source / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ReviewerError, "symlinked component"):
                mechanical.source_path(source, "linked/Task.lean", "challenge.path")

    def test_source_path_requires_a_regular_file_inside_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            with self.assertRaisesRegex(ReviewerError, "missing or escapes"):
                mechanical.source_path(source, "missing.lean", "challenge.path")

    def test_project_readme_prefers_a_regular_project_file_and_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "README.md").write_text("root\n", encoding="utf-8")
            project = source / "examples" / "one"
            project.mkdir(parents=True)
            project_readme = project / "README.md"
            project_readme.write_text("project\n", encoding="utf-8")
            report = {"source": {"project_path": "examples/one"}}
            self.assertEqual(
                mechanical.project_readme_relative(report, source),
                "examples/one/README.md",
            )
            project_readme.unlink()
            project_readme.symlink_to(source / "README.md")
            self.assertEqual(mechanical.project_readme_relative(report, source), "README.md")

    def test_source_tree_url_encodes_each_project_path_component(self):
        source = {
            "repository_url": "https://github.com/example/project",
            "commit": "1" * 40,
            "project_path": "examples/a b",
        }
        self.assertEqual(
            mechanical.source_tree_url(source),
            f"https://github.com/example/project/tree/{'1' * 40}/examples/a%20b",
        )

    def test_named_report_paths_are_read_from_one_mapping(self):
        report = {
            "challenge": {"path": "Challenge.lean"},
            "solution": {"path": "Solution.lean"},
            "comparator": {"path": "comparator.json"},
            "formalization": {"path": "formalization.yaml"},
            "lakefile": {"path": "lakefile.toml"},
            "lean_toolchain_path": "lean-toolchain",
        }
        expected = {
            "challenge_source": "Challenge.lean",
            "solution_source": "Solution.lean",
            "comparator_config": "comparator.json",
            "formalization_metadata": "formalization.yaml",
            "lakefile": "lakefile.toml",
            "lean_toolchain": "lean-toolchain",
        }
        self.assertEqual(
            {name: mechanical.relative_path(report, name) for name in expected},
            expected,
        )

if __name__ == "__main__":
    unittest.main()
