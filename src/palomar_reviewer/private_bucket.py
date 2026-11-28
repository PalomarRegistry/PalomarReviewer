"""Palomar's private bucket, as the reviewer sees it (confidential contract v3.1).

The Server stores a confidential submission's source tarball at
``conf/<id>/source.tar.gz`` and, on withdrawal, a tombstone at
``conf-tombstones/<id>``. The reviewer reads both, and writes the
content-bearing text that must not be committed to State (failure prose and
error strings) under ``conf/<id>/text/<sha256>.json``, which withdrawal deletes
with the rest of ``conf/<id>/``.

Access is one R2 API token for that bucket, given to the State workflow as four
secrets, through the runner's ``aws`` CLI. Nothing here writes the Server's
lifecycle record or certificates; an R2 token cannot be limited by prefix, so
that is a convention of this module rather than of the credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import ReviewerError

ENDPOINT_ENV = "PALOMAR_CONFIDENTIAL_R2_ENDPOINT"
BUCKET_ENV = "PALOMAR_CONFIDENTIAL_R2_BUCKET"
ACCESS_KEY_ENV = "PALOMAR_CONFIDENTIAL_R2_ACCESS_KEY_ID"
SECRET_KEY_ENV = "PALOMAR_CONFIDENTIAL_R2_SECRET_ACCESS_KEY"
SUBMISSION_ID_RE = re.compile(r"[a-z0-9]{12}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _id(submission_id: str) -> str:
    if not isinstance(submission_id, str) or not SUBMISSION_ID_RE.fullmatch(submission_id):
        raise ReviewerError("confidential submission id is malformed")
    return submission_id


def source_key(submission_id: str) -> str:
    return f"conf/{_id(submission_id)}/source.tar.gz"


def text_key(submission_id: str, sha256: str) -> str:
    if not SHA256_RE.fullmatch(sha256):
        raise ReviewerError("confidential text digest is malformed")
    return f"conf/{_id(submission_id)}/text/{sha256}.json"


def tombstone_key(submission_id: str) -> str:
    return f"conf-tombstones/{_id(submission_id)}"


def canonical_bytes(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class Bucket:
    """The four operations the reviewer needs, each one ``aws`` call."""

    def __init__(
        self,
        environ: dict[str, str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        environ = dict(os.environ if environ is None else environ)
        missing = [
            name for name in (ENDPOINT_ENV, BUCKET_ENV, ACCESS_KEY_ENV, SECRET_KEY_ENV)
            if not environ.get(name)
        ]
        if missing:
            raise ReviewerError(
                "a confidential submission needs the private bucket, but "
                + ", ".join(missing) + " is not set"
            )
        self.endpoint = environ[ENDPOINT_ENV]
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+/?", self.endpoint):
            raise ReviewerError(f"{ENDPOINT_ENV} must be an https origin")
        self.bucket = environ[BUCKET_ENV]
        self.runner = runner
        self.environment = {
            **{key: value for key, value in environ.items() if not key.startswith("AWS_")},
            "AWS_ACCESS_KEY_ID": environ[ACCESS_KEY_ENV],
            "AWS_SECRET_ACCESS_KEY": environ[SECRET_KEY_ENV],
            "AWS_DEFAULT_REGION": "auto",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            # R2 does not want the CLI's default request checksums.
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
            "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        }

    def _aws(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = self.runner(
            ["aws", "--endpoint-url", self.endpoint, *args],
            env=self.environment, capture_output=True, text=True, check=False,
        )
        if check and completed.returncode:
            # The key names an id, never content; stderr is the CLI's own.
            detail = (completed.stderr or completed.stdout or "").strip()[-500:]
            raise ReviewerError(f"private bucket request failed: {detail or 'no detail'}")
        return completed

    def exists(self, key: str) -> bool:
        completed = self._aws("s3api", "head-object", "--bucket", self.bucket, "--key", key, check=False)
        if completed.returncode == 0:
            return True
        detail = completed.stderr or ""
        if "(404)" in detail or "Not Found" in detail or "NoSuchKey" in detail:
            return False
        raise ReviewerError(f"private bucket could not say whether {key} exists: {detail.strip()[-500:]}")

    def put(self, key: str, data: bytes) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(data)
            handle.flush()
            self._aws("s3api", "put-object", "--bucket", self.bucket, "--key", key, "--body", handle.name)

    def download(self, key: str, destination: Path) -> None:
        self._aws("s3api", "get-object", "--bucket", self.bucket, "--key", key, str(destination))

    def presign(self, key: str, seconds: int) -> str:
        url = self._aws(
            "s3", "presign", f"s3://{self.bucket}/{key}", "--expires-in", str(seconds)
        ).stdout.strip()
        if not url.startswith("https://") or any(character.isspace() for character in url):
            raise ReviewerError("private bucket returned no usable presigned URL")
        return url

    def withdrawn(self, submission_id: str) -> bool:
        """Whether the author has withdrawn: any tombstone, pending or done."""
        return self.exists(tombstone_key(submission_id))

    def store_text(self, submission_id: str, document: Any) -> dict[str, str]:
        """Store one bundle of content-bearing text and name it for State."""
        data = canonical_bytes(document)
        digest = hashlib.sha256(data).hexdigest()
        key = text_key(submission_id, digest)
        self.put(key, data)
        return {"key": key, "sha256": digest}
