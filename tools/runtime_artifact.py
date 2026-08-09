#!/usr/bin/env python3
"""Build or verify the exact Reviewer runtime artifact committed to Git."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / "runtime"
WHEEL_NAME = "palomar_reviewer-0.1.0-py3-none-any.whl"
REQUIREMENTS_NAME = "requirements.txt"
SUMS_NAME = "SHA256SUMS"
PYTHON_VERSION = "3.11.10"
UV_VERSION = "0.12.1"
# ZIP cannot represent an earlier timestamp. A fixed epoch makes identical
# source and build inputs produce identical wheel bytes on every promotion.
SOURCE_DATE_EPOCH = "315532800"


class ArtifactError(RuntimeError):
    """The promoted artifact cannot be reproduced exactly."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTHON", "UV_"))
    }


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        command, cwd=ROOT, env=clean_environment() if env is None else env, check=True
    )


def export_lock(output: Path, *selection: str) -> None:
    run(
        [
            "uv",
            "export",
            "--locked",
            "--python",
            PYTHON_VERSION,
            *selection,
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
            "--format",
            "requirements.txt",
            "--output-file",
            str(output),
            "--no-config",
            "--quiet",
        ]
    )


def reproduce() -> dict[str, bytes]:
    version = subprocess.check_output(
        ["uv", "--version"], env=clean_environment(), text=True
    ).strip()
    if version != f"uv {UV_VERSION} (x86_64-unknown-linux-gnu)":
        raise ArtifactError(
            f"runtime artifact requires uv {UV_VERSION} for x86_64 Linux, found {version!r}"
        )

    with tempfile.TemporaryDirectory(prefix="palomar-runtime-") as temporary:
        work = Path(temporary)
        build_constraints = work / "build-constraints.txt"
        dependencies = work / "dependencies.txt"
        wheel_dir = work / "wheel"
        export_lock(build_constraints, "--only-group", "artifact")
        export_lock(dependencies, "--no-dev")

        environment = clean_environment()
        environment["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
        environment["PYTHONHASHSEED"] = "0"
        run(
            [
                "uv",
                "build",
                "--wheel",
                "--python",
                PYTHON_VERSION,
                "--build-constraints",
                str(build_constraints),
                "--require-hashes",
                "--out-dir",
                str(wheel_dir),
                "--no-config",
                "--quiet",
            ],
            env=environment,
        )
        built = sorted(wheel_dir.glob("*.whl"))
        if [path.name for path in built] != [WHEEL_NAME]:
            raise ArtifactError(f"build produced unexpected wheels: {[path.name for path in built]}")

        wheel = built[0].read_bytes()
        requirements = dependencies.read_bytes()
        files = {
            WHEEL_NAME: wheel,
            REQUIREMENTS_NAME: requirements,
        }
        sums = "".join(
            f"{sha256(content)}  {name}\n" for name, content in sorted(files.items())
        ).encode("ascii")
        files[SUMS_NAME] = sums
        return files


def write_artifact(expected: dict[str, bytes]) -> None:
    if ARTIFACT_DIR.exists() and (
        not ARTIFACT_DIR.is_dir() or ARTIFACT_DIR.is_symlink()
    ):
        raise ArtifactError("runtime artifact path is not an ordinary directory")
    ARTIFACT_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    extras = {path.name for path in ARTIFACT_DIR.iterdir()} - set(expected)
    if extras:
        raise ArtifactError(f"refusing to replace artifact directory with extra entries: {sorted(extras)}")
    for name, content in expected.items():
        destination = ARTIFACT_DIR / name
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=ROOT, prefix=".palomar-runtime-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
            temporary_path.chmod(0o644)
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def git_modes() -> dict[str, str]:
    output = subprocess.check_output(
        ["git", "ls-files", "--stage", "--", str(ARTIFACT_DIR.relative_to(ROOT))],
        cwd=ROOT,
        text=True,
    )
    result: dict[str, str] = {}
    for line in output.splitlines():
        metadata, relative = line.split("\t", 1)
        result[Path(relative).name] = metadata.split(" ", 1)[0]
    return result


def check_artifact(expected: dict[str, bytes]) -> None:
    if not ARTIFACT_DIR.is_dir() or ARTIFACT_DIR.is_symlink():
        raise ArtifactError("runtime artifact directory is missing or is not an ordinary directory")
    actual_names = {path.name for path in ARTIFACT_DIR.iterdir()}
    if actual_names != set(expected):
        raise ArtifactError(
            f"runtime artifact entries differ: expected {sorted(expected)}, found {sorted(actual_names)}"
        )
    for name, content in expected.items():
        path = ARTIFACT_DIR / name
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o111:
            raise ArtifactError(f"runtime/{name} must be an ordinary non-executable file")
        if path.read_bytes() != content:
            raise ArtifactError(
                f"runtime/{name} is stale; run `python3 tools/runtime_artifact.py --write`"
            )
    modes = git_modes()
    if modes != {name: "100644" for name in expected}:
        raise ArtifactError(f"runtime artifact Git modes must all be 100644, found {modes}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="reproduce and compare the artifact")
    action.add_argument("--write", action="store_true", help="reproduce and replace the artifact")
    arguments = parser.parse_args()
    try:
        expected = reproduce()
        if arguments.write:
            write_artifact(expected)
        else:
            check_artifact(expected)
    except (ArtifactError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"runtime artifact error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
