import base64
import csv
import hashlib
import io
import os
import re
import tempfile
import unittest
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest import mock

from tools import runtime_artifact

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
            metadata = BytesParser(policy=default).parsebytes(
                archive.read("palomar_reviewer-0.1.0.dist-info/METADATA")
            )
            self.assertEqual(metadata["Name"], "palomar-reviewer")
            self.assertEqual(metadata["Version"], "0.1.0")
            self.assertEqual(
                metadata.get_all("Requires-Dist"),
                ["jsonschema<5,>=4.25", "pyyaml<7,>=6"],
            )
            self.assertEqual(
                archive.read("palomar_reviewer-0.1.0.dist-info/licenses/LICENSE"),
                (ROOT / "LICENSE").read_bytes(),
            )
            self.assertEqual(
                archive.read("palomar_reviewer-0.1.0.dist-info/WHEEL"),
                b"Wheel-Version: 1.0\nGenerator: hatchling 1.31.0\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n",
            )
            record = list(
                csv.reader(
                    io.StringIO(
                        archive.read("palomar_reviewer-0.1.0.dist-info/RECORD").decode(
                            "utf-8"
                        )
                    )
                )
            )
            self.assertEqual([row[0] for row in record], names)
            for name, digest, size in record:
                if name.endswith(".dist-info/RECORD"):
                    self.assertEqual((digest, size), ("", ""))
                    continue
                encoded = base64.urlsafe_b64encode(
                    hashlib.sha256(archive.read(name)).digest()
                ).rstrip(b"=")
                self.assertEqual(digest, f"sha256={encoded.decode('ascii')}", name)
                self.assertEqual(size, str(len(archive.read(name))), name)
        self.assertEqual(entry_points, "[console_scripts]\npalomar-review = palomar_reviewer.cli:main\n")

    def test_reproducer_environment_is_an_explicit_stable_allowlist(self):
        ambient = {
            "HOME": "/tmp/reviewer-home",
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": "/attacker",
            "UV_INDEX_URL": "https://attacker.invalid/simple",
            "GIT_INDEX_FILE": "/attacker/index",
            "HATCH_BUILD_HOOKS_ENABLE": "true",
            "SOURCE_DATE_EPOCH": "1",
            "TZ": "Pacific/Honolulu",
        }
        with mock.patch.dict(os.environ, ambient, clear=True):
            self.assertEqual(
                runtime_artifact.clean_environment(),
                {
                    "HOME": "/tmp/reviewer-home",
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "TZ": "UTC",
                },
            )

    def test_artifact_checker_refuses_extra_stale_executable_and_symlink_files(self):
        expected = {"wheel.whl": b"wheel", "requirements.txt": b"requirements"}
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "runtime"
            artifact.mkdir()
            for name, content in expected.items():
                (artifact / name).write_bytes(content)
            modes = {name: "100644" for name in expected}
            with (
                mock.patch.object(runtime_artifact, "ARTIFACT_DIR", artifact),
                mock.patch.object(runtime_artifact, "git_modes", return_value=modes),
            ):
                runtime_artifact.check_artifact(expected)

                extra = artifact / "extra"
                extra.write_bytes(b"extra")
                with self.assertRaisesRegex(runtime_artifact.ArtifactError, "entries differ"):
                    runtime_artifact.check_artifact(expected)
                extra.unlink()

                wheel = artifact / "wheel.whl"
                wheel.write_bytes(b"stale")
                with self.assertRaisesRegex(runtime_artifact.ArtifactError, "is stale"):
                    runtime_artifact.check_artifact(expected)
                wheel.write_bytes(expected["wheel.whl"])

                wheel.chmod(0o755)
                with self.assertRaisesRegex(runtime_artifact.ArtifactError, "non-executable"):
                    runtime_artifact.check_artifact(expected)
                wheel.chmod(0o644)

                wheel.unlink()
                wheel.symlink_to(artifact / "requirements.txt")
                with self.assertRaisesRegex(runtime_artifact.ArtifactError, "ordinary"):
                    runtime_artifact.check_artifact(expected)

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
