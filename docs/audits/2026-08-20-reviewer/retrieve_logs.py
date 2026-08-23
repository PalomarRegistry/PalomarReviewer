#!/usr/bin/env python3
"""Retrieve every retained Palomar AI-review log from GitHub Actions artifacts.

The workflow artifact also contains full source, policy, and database checkouts.
This extractor retains the model prompts, raw event streams, structured pass
results, final review, accounting, mechanical evidence, and exact policy prompt
contract while omitting those bulky checkouts.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

REPOSITORY = "PalomarRegistry/PalomarSubmissionState"
ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"

ROOT_FILES = {
    "mechanical-report-bytes-sha256",
    "mechanical-report-sha256",
    "mechanical-report-url",
    "review-sha256",
    "review.json",
    "spend.json",
    "state.json",
    "workflow-run-sha256",
    "workflow-run.json",
}


def decode_concatenated_json(raw: str) -> list[object]:
    decoder = json.JSONDecoder()
    values: list[object] = []
    offset = 0
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset == len(raw):
            break
        value, offset = decoder.raw_decode(raw, offset)
        values.append(value)
    return values


def artifact_inventory() -> list[dict[str, object]]:
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{REPOSITORY}/actions/artifacts?per_page=100",
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    artifacts: list[dict[str, object]] = []
    for page in decode_concatenated_json(result.stdout):
        assert isinstance(page, dict)
        for artifact in page["artifacts"]:
            if (
                not artifact["expired"]
                and artifact["name"].startswith("review-packets-")
            ):
                artifacts.append(artifact)
    return sorted(artifacts, key=lambda item: (item["created_at"], item["id"]))


def keep_member(name: str) -> bool:
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or path.is_absolute() or ".." in parts:
        return False
    if any(part in {"raw", "passes", "prompts"} for part in parts):
        return True
    if len(parts) >= 2 and parts[-1] in ROOT_FILES:
        return True
    if "mechanical-download" in parts and parts[-1] == "mechanical-report.json":
        return True
    if "policy" in parts and parts[-1] in {
        "rubric.json",
        "review.schema.json",
        "public-review.schema.json",
        "classification-guide.md",
        "materiality.md",
    }:
        return True
    return False


def retrieve(artifact: dict[str, object]) -> dict[str, object]:
    artifact_id = int(artifact["id"])
    run_id = int(artifact["workflow_run"]["id"])
    destination = RUNS / f"{run_id}-{artifact_id}"
    manifest_path = destination / "extraction.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())

    destination.mkdir(parents=True, exist_ok=True)
    fd, archive_name = tempfile.mkstemp(prefix=f"palomar-{artifact_id}-", suffix=".zip")
    os.close(fd)
    archive_path = Path(archive_name)
    entries: list[dict[str, object]] = []
    try:
        with archive_path.open("wb") as archive_file:
            subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip",
                ],
                check=True,
                stdout=archive_file,
            )
        archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if member.is_dir() or not keep_member(member.filename):
                    continue
                relative = PurePosixPath(member.filename)
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                data = archive.read(member)
                target.write_bytes(data)
                entries.append(
                    {
                        "path": member.filename,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
        manifest = {
            "artifact_id": artifact_id,
            "artifact_name": artifact["name"],
            "artifact_created_at": artifact["created_at"],
            "artifact_size_bytes": artifact["size_in_bytes"],
            "archive_sha256": archive_sha256,
            "workflow_run_id": run_id,
            "entries": entries,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return manifest
    finally:
        archive_path.unlink(missing_ok=True)


def main() -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    artifacts = artifact_inventory()
    (ROOT / "artifact-inventory.json").write_text(
        json.dumps(artifacts, indent=2, sort_keys=True) + "\n"
    )
    manifests: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(retrieve, artifact): artifact for artifact in artifacts}
        for completed, future in enumerate(concurrent.futures.as_completed(futures), 1):
            manifest = future.result()
            manifests.append(manifest)
            print(
                f"[{completed}/{len(artifacts)}] run {manifest['workflow_run_id']}: "
                f"{len(manifest['entries'])} retained files",
                flush=True,
            )
    manifests.sort(key=lambda item: (item["artifact_created_at"], item["artifact_id"]))
    (ROOT / "extraction-index.json").write_text(
        json.dumps(manifests, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
