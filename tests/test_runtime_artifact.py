import hashlib
import re
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
WHEEL = RUNTIME / "palomar_reviewer-0.1.0-py3-none-any.whl"
REQUIREMENTS = RUNTIME / "requirements.txt"
SUMS = RUNTIME / "SHA256SUMS"
CI = ROOT / ".github" / "workflows" / "ci.yml"
HASH_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)")


class RuntimeArtifactTests(unittest.TestCase):
    def test_manifest_closes_and_hashes_the_promoted_artifact(self):
        lines = SUMS.read_text(encoding="ascii").splitlines()
        parsed = [HASH_LINE.fullmatch(line) for line in lines]
        self.assertTrue(all(parsed))
        manifest = {match.group(2): match.group(1) for match in parsed if match}
        self.assertEqual(
            manifest,
            {
                "palomar_reviewer-0.1.0-py3-none-any.whl": hashlib.sha256(
                    WHEEL.read_bytes()
                ).hexdigest(),
                "requirements.txt": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
            },
        )
        self.assertEqual(
            {path.name for path in RUNTIME.iterdir()}, set(manifest) | {"SHA256SUMS"}
        )

    def test_runtime_lock_names_the_local_wheel_and_hashes_every_requirement(self):
        requirements = REQUIREMENTS.read_text(encoding="utf-8")
        self.assertNotIn("git+", requirements)
        self.assertNotIn("--editable", requirements)
        self.assertNotIn("--exclude-newer", requirements)
        self.assertNotIn("palomar-reviewer", requirements)
        requirement_lines = [
            line for line in requirements.splitlines() if line and not line.startswith(" ")
        ]
        self.assertEqual(
            [line.split("==", 1)[0] for line in requirement_lines],
            [
                "attrs",
                "jsonschema",
                "jsonschema-specifications",
                "pyyaml",
                "referencing",
                "rpds-py",
                "typing-extensions",
            ],
        )
        requirement_blocks = re.split(r"\n(?=[^ \n])", requirements.strip())
        self.assertEqual(len(requirement_blocks), len(requirement_lines))
        for block in requirement_blocks:
            self.assertIn("--hash=sha256:", block, block.splitlines()[0])

    def test_wheel_contains_only_the_runtime_package_and_metadata(self):
        with zipfile.ZipFile(WHEEL) as archive:
            names = archive.namelist()
            package = ROOT / "src" / "palomar_reviewer"
            source_members = {
                "palomar_reviewer/__init__.py",
                "palomar_reviewer/authorization.py",
                "palomar_reviewer/cli.py",
                "palomar_reviewer/engine.py",
                "palomar_reviewer/errors.py",
                "palomar_reviewer/mechanical.py",
                "palomar_reviewer/registration.py",
                "palomar_reviewer/usage.py",
            }
            metadata_members = {
                "palomar_reviewer-0.1.0.dist-info/METADATA",
                "palomar_reviewer-0.1.0.dist-info/RECORD",
                "palomar_reviewer-0.1.0.dist-info/WHEEL",
                "palomar_reviewer-0.1.0.dist-info/entry_points.txt",
                "palomar_reviewer-0.1.0.dist-info/licenses/LICENSE",
            }
            self.assertEqual(
                source_members,
                {
                    f"palomar_reviewer/{path.relative_to(package).as_posix()}"
                    for path in package.rglob("*.py")
                },
            )
            self.assertEqual(set(names), source_members | metadata_members)
            for member in source_members:
                source = package / Path(member).relative_to("palomar_reviewer")
                self.assertEqual(archive.read(member), source.read_bytes(), member)
            self.assertTrue(
                all((info.external_attr >> 16) & 0o111 == 0 for info in archive.infolist())
            )
            entry_points = archive.read(
                "palomar_reviewer-0.1.0.dist-info/entry_points.txt"
            ).decode("utf-8")
        self.assertEqual(entry_points, "[console_scripts]\npalomar-review = palomar_reviewer.cli:main\n")

    def test_ci_reproduces_then_cold_installs_the_promoted_bytes(self):
        workflow = CI.read_text(encoding="utf-8")
        self.assertIn("python3 tools/runtime_artifact.py --check", workflow)
        self.assertNotIn("python3 tools/runtime_artifact.py --write", workflow)
        self.assertIn("uv venv --no-project --python 3.11.10 --no-managed-python", workflow)
        self.assertIn("sha256sum --check SHA256SUMS", workflow)
        self.assertIn("(cd runtime && uv pip install", workflow)
        self.assertIn("--require-hashes --no-deps --only-binary :all:", workflow)
        self.assertIn("--no-deps --no-index", workflow)
        self.assertIn("runtime/palomar_reviewer-0.1.0-py3-none-any.whl", workflow)
        self.assertIn('uv pip check --python "$runtime_env/bin/python"', workflow)
        self.assertIn("--no-cache --no-config", workflow)
        for command in ("", "auto", "rebuild-queue", "doctor", "star-registered"):
            self.assertIn(
                f'"$runtime_env/bin/palomar-review" {command + " " if command else ""}--help',
                workflow,
            )


if __name__ == "__main__":
    unittest.main()
