"""Confidential source snapshots and evidence objects (contract v2 sections 2.2, 3.2 to 3.4).

A confidential submission's source is read only from the bundles its snapshot
job captured, never from the upstream. The snapshot descriptor, finalized by
the Worker after a validation job restored every bundle, is named by one
digest, ``snapshot_sha256``, recorded in the State record before verification
began. Every later stage names it.

Everything content-bearing (mechanical report, render result, delivered
review) goes to the private bucket under ``v2/<submission_id>/evidence/``,
content-addressed, through a per-submission capability the Worker grants a
State reviewer job that proves its identity with a GitHub OIDC token. State
keeps only the key and the digest. No bucket-wide credential exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ReviewerError

SERVER = "https://submit.palomar-registry.org"
AUDIENCE = f"{SERVER}/confidential"
DESCRIPTOR_SCHEMA = "palomar-snapshot/1"
MANIFEST_SCHEMA = "palomar-source-manifest/1"
VERIFIED_REF = "refs/palomar/verified"
EVIDENCE_KINDS = ("mechanical-report", "workflow-run", "render-result", "review")
MAX_OBJECT_BYTES = 500 * 1024 * 1024
SHA40 = re.compile(r"[0-9a-f]{40}")
SHA64 = re.compile(r"[0-9a-f]{64}")
SUBMISSION_ID = re.compile(r"[0-9a-z]{12}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recorded_snapshot(state: dict[str, Any]) -> str:
    """The snapshot digest the State record carries, which every stage must name."""
    value = ((state.get("confidential") or {}).get("snapshot") or {}).get("sha256")
    if not isinstance(value, str) or not SHA64.fullmatch(value):
        raise ReviewerError("confidential submission has no finalized snapshot")
    return value


def snapshot_prefix(submission_id: str) -> str:
    if not SUBMISSION_ID.fullmatch(submission_id):
        raise ReviewerError("submission id is malformed")
    return f"v2/{submission_id}/"


def evidence_key(submission_id: str, kind: str, data: bytes) -> str:
    if kind not in EVIDENCE_KINDS:
        raise ReviewerError(f"evidence kind {kind!r} is not recognized")
    return f"{snapshot_prefix(submission_id)}evidence/{kind}/{sha256_bytes(data)}"


# --- The object client ---------------------------------------------------


def github_oidc_token(environment: dict[str, str] | None = None) -> str:
    """A GitHub Actions OIDC token for the confidential audience.

    Only the pinned reusable workflow `confidential-objects.yml` has
    `id-token: write`; anywhere else the request variables are absent and this
    refuses.
    """
    environment = os.environ if environment is None else environment
    url = environment.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = environment.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise ReviewerError("confidential objects need the pinned OIDC workflow; no token is available")
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}audience={urllib.parse.quote(AUDIENCE, safe='')}",
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read(65537))
    except (urllib.error.URLError, ValueError) as error:
        raise ReviewerError(f"could not obtain a GitHub OIDC token: {error}") from error
    token = value.get("value") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise ReviewerError("GitHub returned no OIDC token")
    return token


Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return response.status, dict(response.headers), response.read(MAX_OBJECT_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read(65536)
    except urllib.error.URLError as error:
        raise ReviewerError(f"the confidential object service could not be reached: {error}") from error


@dataclass
class ObjectClient:
    """One submission's capability-scoped access to the private bucket."""

    submission_id: str
    operation: str
    token: Callable[[], str] = github_oidc_token
    transport: Transport = urllib_transport
    _capability: dict[str, Any] | None = field(default=None, repr=False)

    def _jwt_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}

    def capability(self) -> dict[str, Any]:
        """Ask the submission's lifecycle actor for this operation's capability.

        State runs present `{submission_id, operation}` instead of a ticket;
        the actor grants it only if its phase matrix allows the operation.
        """
        if self._capability is not None:
            return self._capability
        if self.operation not in {"restore", "evidence", "publish"}:
            raise ReviewerError(f"a State job cannot request the {self.operation} operation")
        status, _headers, body = self.transport(
            "POST", f"{SERVER}/api/confidential/broker", self._jwt_headers(),
            json.dumps({"submission_id": self.submission_id, "operation": self.operation}).encode(),
        )
        if status != 200:
            raise ReviewerError(
                f"the lifecycle actor refused the {self.operation} capability (HTTP {status})"
            )
        try:
            value = json.loads(body)
        except ValueError as error:
            raise ReviewerError("the capability response is not JSON") from error
        prefix = snapshot_prefix(self.submission_id)
        if (
            not isinstance(value, dict)
            or value.get("submission_id") != self.submission_id
            or value.get("operation") != self.operation
            or not isinstance(value.get("capability"), str)
            or any(not str(key).startswith(prefix) for key in value.get("object_keys") or [])
        ):
            raise ReviewerError("the capability response does not describe this submission's operation")
        self._capability = value
        return value

    def _object_headers(self) -> dict[str, str]:
        return {
            **self._jwt_headers(),
            "X-Palomar-Capability": self.capability()["capability"],
            "Content-Type": "application/octet-stream",
        }

    def _url(self, key: str) -> str:
        if not key.startswith(snapshot_prefix(self.submission_id)) or ".." in key.split("/"):
            raise ReviewerError("object key is outside this submission's prefix")
        return f"{SERVER}/api/confidential/objects/{urllib.parse.quote(key, safe='/')}"

    def get(self, key: str, sha256: str) -> bytes:
        """Download one object and recheck its digest; a mismatch fails closed."""
        if not SHA64.fullmatch(sha256):
            raise ReviewerError("expected object digest is malformed")
        status, headers, body = self.transport("GET", self._url(key), self._object_headers(), None)
        if status != 200:
            raise ReviewerError(f"could not read confidential object {key} (HTTP {status})")
        if len(body) > MAX_OBJECT_BYTES:
            raise ReviewerError("confidential object exceeds the size cap")
        actual = sha256_bytes(body)
        declared = {name.lower(): value for name, value in headers.items()}.get("x-palomar-sha256")
        if actual != sha256 or (declared is not None and declared != actual):
            raise ReviewerError(f"confidential object {key} does not match its recorded digest")
        return body

    def put_evidence(self, kind: str, data: bytes) -> dict[str, str]:
        """Write one content-addressed evidence object; an identical retry succeeds."""
        if self.operation != "evidence":
            raise ReviewerError("only an evidence capability may write evidence")
        key = evidence_key(self.submission_id, kind, data)
        headers = {**self._object_headers(), "X-Palomar-Sha256": sha256_bytes(data)}
        status, _headers, _body = self.transport("PUT", self._url(key), headers, data)
        if status not in {200, 201}:
            raise ReviewerError(f"could not store confidential evidence {kind} (HTTP {status})")
        return {"key": key, "sha256": sha256_bytes(data)}


# --- Descriptor and bundles ---------------------------------------------


def validate_descriptor(
    descriptor: Any, state: dict[str, Any], *, snapshot_sha256: str
) -> dict[str, Any]:
    """Bind a downloaded descriptor to the State record before any bundle is used."""
    if not isinstance(descriptor, dict) or descriptor.get("schema") != DESCRIPTOR_SCHEMA:
        raise ReviewerError("snapshot descriptor has an unsupported schema")
    if canonical_sha256(descriptor) != snapshot_sha256:
        raise ReviewerError("snapshot descriptor does not match the recorded snapshot digest")
    if descriptor.get("submission_id") != state.get("id"):
        raise ReviewerError("snapshot descriptor names a different submission")
    source = descriptor.get("source")
    recorded = (state.get("confidential") or {}).get("source") or {}
    if (
        not isinstance(source, dict)
        or source.get("commit") != state.get("commit")
        or not SHA40.fullmatch(str(source.get("tree", "")))
        or (recorded.get("repository_id") is not None
            and source.get("repository_id") != recorded.get("repository_id"))
    ):
        raise ReviewerError("snapshot descriptor does not describe the admitted source")
    bundle = source.get("bundle")
    if not isinstance(bundle, dict) or bundle.get("ref") != VERIFIED_REF:
        raise ReviewerError("snapshot descriptor has no source bundle")
    run = descriptor.get("run")
    if not isinstance(run, dict) or type(run.get("id")) is not int or type(run.get("attempt")) is not int:
        raise ReviewerError("snapshot descriptor names no run attempt")
    return descriptor


def bind_report_to_snapshot(
    report: dict[str, Any], run_data: dict[str, Any], descriptor: dict[str, Any], snapshot_sha256: str
) -> None:
    """Ingest a report only from the recorded snapshot and the recorded run attempt."""
    if report.get("snapshot_sha256") != snapshot_sha256:
        raise ReviewerError("mechanical report names a different snapshot than the one recorded")
    run = descriptor["run"]
    if run_data.get("databaseId") != run["id"] or run_data.get("attempt") != run["attempt"]:
        raise ReviewerError("mechanical report comes from a run attempt other than the snapshot's")


def _git_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in ("PATH", "HOME", "LANG") if name in os.environ
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
    })
    return environment


def _git(arguments: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", *arguments],
        cwd=cwd, env=_git_environment(), capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode:
        raise ReviewerError(f"git {arguments[0]} failed: {(proc.stderr or proc.stdout).strip()[-1500:]}")
    return proc.stdout


def bundle_header(path: Path) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """Prerequisites, refs and capabilities named by a v2 or v3 bundle header."""
    with path.open("rb") as handle:
        signature = handle.readline()
        if signature not in {b"# v2 git bundle\n", b"# v3 git bundle\n"}:
            raise ReviewerError("snapshot bundle is not a git bundle")
        prerequisites, refs, capabilities = [], [], []
        while True:
            line = handle.readline()
            if not line:
                raise ReviewerError("snapshot bundle header is truncated")
            if line == b"\n":
                break
            text = line.decode("utf-8", errors="strict").rstrip("\n")
            if text.startswith("@"):
                capabilities.append(text[1:])
            elif text.startswith("-"):
                prerequisites.append(text[1:])
            else:
                oid, _, name = text.partition(" ")
                refs.append((oid, name))
    return prerequisites, refs, capabilities


def check_bundle_header(path: Path, commit: str) -> None:
    prerequisites, refs, capabilities = bundle_header(path)
    if prerequisites:
        raise ReviewerError("snapshot bundle has prerequisites; only a complete history is accepted")
    if any(capability.startswith("filter") for capability in capabilities):
        raise ReviewerError("snapshot bundle is filtered")
    if refs != [(commit, VERIFIED_REF)]:
        raise ReviewerError(f"snapshot bundle must carry exactly {VERIFIED_REF} at the verified commit")


def restore_bundle(path: Path, commit: str, destination: Path) -> Path:
    """Restore one bundle into an empty repository and check it completely.

    The header check is necessary, not sufficient: the import, `git bundle
    verify` and a strict full fsck establish that the whole history is present.
    The working tree is checked out at the verified commit.
    """
    if not SHA40.fullmatch(commit):
        raise ReviewerError("verified commit is malformed")
    check_bundle_header(path, commit)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    _git(["init", "--quiet", "--initial-branch=palomar-review", str(destination)])
    _git(["bundle", "verify", "--quiet", str(path)], cwd=destination)
    _git(["fetch", "--quiet", "--no-tags", str(path), f"{VERIFIED_REF}:{VERIFIED_REF}"], cwd=destination)
    if _git(["rev-parse", f"{VERIFIED_REF}^{{commit}}"], cwd=destination).strip() != commit:
        raise ReviewerError("restored bundle does not resolve to the verified commit")
    _git(["fsck", "--full", "--strict", "--no-dangling"], cwd=destination)
    _git(["checkout", "--quiet", "--detach", commit], cwd=destination)
    if _git(["rev-parse", "HEAD"], cwd=destination).strip() != commit:
        raise ReviewerError("source checkout does not match the verified commit")
    return destination


def restore_source(
    client: ObjectClient, state: dict[str, Any], destination: Path
) -> tuple[dict[str, Any], Path]:
    """The verified source, from the recorded snapshot only. No upstream fetch exists."""
    snapshot_sha256 = recorded_snapshot(state)
    prefix = snapshot_prefix(state["id"])
    # The Worker writes the descriptor canonically, so its bytes digest is the snapshot digest.
    descriptor_bytes = client.get(f"{prefix}snapshot/descriptor.json", snapshot_sha256)
    try:
        descriptor = json.loads(descriptor_bytes)
    except ValueError as error:
        raise ReviewerError("snapshot descriptor is not JSON") from error
    validate_descriptor(descriptor, state, snapshot_sha256=snapshot_sha256)
    bundle = descriptor["source"]["bundle"]
    expected_key = f"{prefix}snapshot/{descriptor['source']['repository_id']}-{state['commit']}.bundle"
    if bundle.get("key") != expected_key:
        raise ReviewerError("snapshot descriptor names an unexpected bundle key")
    data = client.get(bundle["key"], bundle["sha256"])
    with tempfile.TemporaryDirectory(prefix="palomar-bundle-") as directory:
        path = Path(directory) / "source.bundle"
        path.write_bytes(data)
        restore_bundle(path, state["commit"], destination)
    return descriptor, destination
