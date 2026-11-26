"""Confidential snapshots: bundle restore, descriptor binding, object access and evidence placement."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import palomar_reviewer.cli as cli
import palomar_reviewer.snapshot as snapshot
from palomar_reviewer.errors import ReviewerError

SUBMISSION = "c0nf1d3nt1a1"


def git(cwd, *arguments, env=None):
    environment = {
        "PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1", **(env or {}),
    }
    return subprocess.run(
        ["git", *arguments], cwd=cwd, env=environment, check=True, capture_output=True, text=True
    ).stdout.strip()


class FixtureRepository:
    """Three commits, a removed file and two authors, as contract v2 part 4 asks."""

    def __init__(self, root: Path):
        self.root = root
        self.upstream = root / "upstream"
        self.upstream.mkdir()
        git(self.upstream, "init", "--quiet", "--initial-branch=main")
        people = [("Ada Author", "ada@example.org"), ("Bo Builder", "bo@example.org")]
        for index, (name, email) in enumerate(people + [people[0]]):
            identity = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
            if index == 0:
                (self.upstream / "Scratch.lean").write_text("-- removed later\n")
            if index == 2:
                (self.upstream / "Scratch.lean").unlink()
            (self.upstream / "Challenge.lean").write_text(f"theorem t{index} : True := trivial\n")
            git(self.upstream, "add", "-A")
            git(self.upstream, "commit", "--quiet", "-m", f"commit {index}", env=identity)
        self.commit = git(self.upstream, "rev-parse", "HEAD")
        git(self.upstream, "update-ref", snapshot.VERIFIED_REF, self.commit)

    def bundle(self, name="source.bundle", *revisions):
        path = self.root / name
        git(self.upstream, "bundle", "create", str(path), *(revisions or (snapshot.VERIFIED_REF,)))
        return path


class BundleRestoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.fixture = FixtureRepository(self.root)

    def test_a_complete_bundle_restores_the_verified_commit_with_its_history(self):
        restored = snapshot.restore_bundle(self.fixture.bundle(), self.fixture.commit, self.root / "out")
        self.assertEqual(git(restored, "rev-parse", "HEAD"), self.fixture.commit)
        self.assertEqual(git(restored, "rev-list", "--count", "HEAD"), "3")
        self.assertEqual((restored / "Challenge.lean").read_text(), "theorem t2 : True := trivial\n")
        self.assertFalse((restored / "Scratch.lean").exists())

    def test_the_upstream_deleted_after_the_snapshot_changes_nothing(self):
        bundle = self.fixture.bundle()
        shutil.rmtree(self.fixture.upstream)
        restored = snapshot.restore_bundle(bundle, self.fixture.commit, self.root / "out")
        self.assertEqual(git(restored, "rev-parse", "HEAD"), self.fixture.commit)

    def test_a_shallow_bundle_with_prerequisites_is_refused(self):
        bundle = self.fixture.bundle("thin.bundle", f"{self.fixture.commit}~1..{snapshot.VERIFIED_REF}")
        with self.assertRaisesRegex(ReviewerError, "prerequisites"):
            snapshot.restore_bundle(bundle, self.fixture.commit, self.root / "out")

    def test_a_bundle_with_two_refs_is_refused(self):
        bundle = self.fixture.bundle("two.bundle", snapshot.VERIFIED_REF, "refs/heads/main")
        with self.assertRaisesRegex(ReviewerError, "exactly"):
            snapshot.restore_bundle(bundle, self.fixture.commit, self.root / "out")

    def test_a_bundle_with_another_ref_name_is_refused(self):
        bundle = self.fixture.bundle("other.bundle", "refs/heads/main")
        with self.assertRaisesRegex(ReviewerError, "exactly"):
            snapshot.restore_bundle(bundle, self.fixture.commit, self.root / "out")

    def test_a_bundle_whose_ref_is_at_another_commit_is_refused(self):
        parent = git(self.fixture.upstream, "rev-parse", "HEAD~1")
        with self.assertRaisesRegex(ReviewerError, "exactly"):
            snapshot.restore_bundle(self.fixture.bundle(), parent, self.root / "out")

    def test_something_that_is_not_a_bundle_is_refused(self):
        path = self.root / "fake.bundle"
        path.write_bytes(b"PK\x03\x04 not a bundle")
        with self.assertRaisesRegex(ReviewerError, "not a git bundle"):
            snapshot.restore_bundle(path, self.fixture.commit, self.root / "out")


def descriptor(commit="3" * 40, run_id=202, attempt=1):
    return {
        "schema": snapshot.DESCRIPTOR_SCHEMA, "submission_id": SUBMISSION, "nonce": "0" * 32,
        "pipeline_commit": "1a" * 20, "caller": {"repository_id": 9, "workflow_sha": "2b" * 20},
        "run": {"id": run_id, "attempt": attempt}, "captured_at": "2026-11-20T09:00:00Z",
        "source": {
            "route": "public", "repository_id": 77, "owner_id": 5, "full_name": "o/r",
            "visibility": "public", "commit": commit, "tree": "4" * 40,
            "history": {"commit_count": 3, "root_commits": ["5" * 40], "history_sha256": "6" * 64},
            "bundle": {"key": f"v2/{SUBMISSION}/snapshot/77-{commit}.bundle", "sha256": "7" * 64,
                       "size": 10, "ref": snapshot.VERIFIED_REF},
        },
        "project": {}, "lake_manifest_sha256": "8" * 64, "source_manifest_sha256": "9" * 64,
        "dependencies": [],
    }


def state(snapshot_sha256, commit="3" * 40):
    return {
        "id": SUBMISSION, "visibility": "confidential", "commit": commit, "repository": "o/r",
        "confidential": {"run_repository": "PalomarRegistry/PalomarConfidentialRuns",
                         "source": {"repository_id": 77},
                         "snapshot": {"sha256": snapshot_sha256}},
    }


class DescriptorBindingTests(unittest.TestCase):
    def test_the_recorded_descriptor_is_accepted(self):
        value = descriptor()
        digest = snapshot.canonical_sha256(value)
        self.assertEqual(snapshot.validate_descriptor(value, state(digest), snapshot_sha256=digest), value)

    def test_a_descriptor_other_than_the_recorded_one_is_refused(self):
        digest = snapshot.canonical_sha256(descriptor())
        tampered = {**descriptor(), "nonce": "f" * 32}
        with self.assertRaisesRegex(ReviewerError, "recorded snapshot digest"):
            snapshot.validate_descriptor(tampered, state(digest), snapshot_sha256=digest)

    def test_a_descriptor_for_another_commit_is_refused(self):
        value = descriptor(commit="a" * 40)
        digest = snapshot.canonical_sha256(value)
        with self.assertRaisesRegex(ReviewerError, "admitted source"):
            snapshot.validate_descriptor(value, state(digest), snapshot_sha256=digest)

    def test_a_report_naming_another_snapshot_is_refused(self):
        value = descriptor()
        digest = snapshot.canonical_sha256(value)
        with self.assertRaisesRegex(ReviewerError, "different snapshot"):
            snapshot.bind_report_to_snapshot(
                {"snapshot_sha256": "e" * 64}, {"databaseId": 202, "attempt": 1}, value, digest
            )

    def test_a_report_from_another_run_attempt_is_refused(self):
        value = descriptor()
        digest = snapshot.canonical_sha256(value)
        for run_data in ({"databaseId": 202, "attempt": 2}, {"databaseId": 203, "attempt": 1}):
            with self.subTest(run_data=run_data), self.assertRaisesRegex(ReviewerError, "run attempt"):
                snapshot.bind_report_to_snapshot({"snapshot_sha256": digest}, run_data, value, digest)

    def test_a_record_without_a_finalized_snapshot_is_refused(self):
        with self.assertRaisesRegex(ReviewerError, "no finalized snapshot"):
            snapshot.recorded_snapshot({"id": SUBMISSION, "confidential": {}})


class Transport:
    def __init__(self, objects=None, capability=None, status=200):
        self.objects = objects or {}
        self.status = status
        self.capability = capability or {
            "capability": "cap", "submission_id": SUBMISSION, "operation": "restore",
            "object_keys": [f"v2/{SUBMISSION}/snapshot/descriptor.json"],
        }
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/api/confidential/broker"):
            return self.status, {}, json.dumps(self.capability).encode()
        key = url.split("/api/confidential/objects/", 1)[1]
        if method == "PUT":
            self.objects[key] = body
            return 201, {}, b""
        if key not in self.objects:
            return 404, {}, b""
        return 200, {}, self.objects[key]


class ObjectClientTests(unittest.TestCase):
    def client(self, transport, operation="restore"):
        return snapshot.ObjectClient(SUBMISSION, operation, token=lambda: "jwt", transport=transport)

    def test_an_object_is_returned_only_with_its_recorded_digest(self):
        key = f"v2/{SUBMISSION}/snapshot/descriptor.json"
        transport = Transport({key: b"bytes"})
        client = self.client(transport)
        self.assertEqual(client.get(key, snapshot.sha256_bytes(b"bytes")), b"bytes")
        with self.assertRaisesRegex(ReviewerError, "recorded digest"):
            client.get(key, "0" * 64)

    def test_another_submissions_key_is_refused_before_any_request(self):
        transport = Transport()
        with self.assertRaisesRegex(ReviewerError, "outside this submission's prefix"):
            self.client(transport).get("v2/a1b2c3d4e5f6/snapshot/descriptor.json", "0" * 64)
        self.assertEqual(transport.calls, [])

    def test_a_capability_for_another_submission_or_operation_is_refused(self):
        for capability in (
            {"capability": "c", "submission_id": "a1b2c3d4e5f6", "operation": "restore"},
            {"capability": "c", "submission_id": SUBMISSION, "operation": "publish"},
            {"capability": "c", "submission_id": SUBMISSION, "operation": "restore",
             "object_keys": ["v2/a1b2c3d4e5f6/snapshot/x"]},
        ):
            with self.subTest(capability=capability), self.assertRaises(ReviewerError):
                self.client(Transport(capability=capability)).capability()

    def test_a_refused_capability_fails_closed(self):
        with self.assertRaisesRegex(ReviewerError, "refused"):
            self.client(Transport(status=409)).capability()

    def test_a_state_job_cannot_ask_for_snapshot_or_validate(self):
        for operation in ("snapshot", "validate"):
            with self.subTest(operation=operation), self.assertRaises(ReviewerError):
                self.client(Transport(), operation).capability()

    def test_evidence_is_content_addressed_under_the_submission(self):
        capability = {"capability": "c", "submission_id": SUBMISSION, "operation": "evidence"}
        transport = Transport(capability=capability)
        entry = self.client(transport, "evidence").put_evidence("review", b"{}")
        self.assertEqual(entry["key"], f"v2/{SUBMISSION}/evidence/review/{snapshot.sha256_bytes(b'{}')}")
        method, _url, headers, body = transport.calls[-1]
        self.assertEqual((method, body, headers["X-Palomar-Sha256"]), ("PUT", b"{}", entry["sha256"]))

    def test_no_token_outside_the_pinned_workflow(self):
        with self.assertRaisesRegex(ReviewerError, "pinned OIDC workflow"):
            snapshot.github_oidc_token({})


class EvidencePlacementTests(unittest.TestCase):
    def setUp(self):
        cli._OBJECT_CLIENTS.clear()

    def test_a_confidential_review_never_reaches_state_git(self):
        record = {**state("a" * 64), "status": "reviewing", "_blob_sha": "blob-1"}
        review = {"schema_version": 3, "outcome": "neutral", "summary": "canary prose"}
        client = mock.Mock()
        client.put_evidence.return_value = {"key": "k", "sha256": "s"}
        writes = []

        def put_state(path, value, message, blob_sha=None):
            writes.append((path, value, blob_sha))
            return f"blob-{len(writes) + 1}"

        with mock.patch.object(cli, "confidential_objects", return_value=client), mock.patch.object(
            cli, "put_state", side_effect=put_state
        ), mock.patch.object(cli, "state_json", return_value=None), mock.patch.object(
            cli, "refuse_engine_credential"
        ):
            try:
                cli.deliver_review(record, review, None, mechanical={}, renderability=None)
            except (KeyError, TypeError, ReviewerError):
                # Later delivery bookkeeping needs a full review; where the
                # prose went is decided before it.
                pass
        self.assertEqual(client.put_evidence.call_args.args[0], "review")
        self.assertTrue(writes, "the evidence pointer was recorded")
        self.assertEqual(writes[0][2], "blob-1")
        for path, value, _blob in writes:
            self.assertNotIn("review.json", path)
            self.assertNotIn("canary prose", json.dumps(value))
        self.assertEqual(writes[0][1]["confidential"]["evidence"]["review"], {"key": "k", "sha256": "s"})

    def test_a_withdrawal_is_never_overwritten_by_a_stale_evidence_write(self):
        stale = {**state("a" * 64), "status": "reviewing", "_blob_sha": "before-withdrawal"}

        def refuse(path, value, message, blob_sha=None):
            self.assertEqual(blob_sha, "before-withdrawal")
            raise ReviewerError("HTTP 409: sha does not match")

        with mock.patch.object(cli, "put_state", side_effect=refuse):
            with self.assertRaises(ReviewerError):
                cli.record_confidential_evidence(stale, "review", {"key": "k", "sha256": "s"})
        self.assertEqual(stale["_blob_sha"], "before-withdrawal")

    def test_a_new_review_replaces_its_pointer_but_a_report_never_does(self):
        record = {**state("a" * 64), "_blob_sha": "b"}
        record["confidential"]["evidence"] = {
            "review": {"key": "k1", "sha256": "s1"}, "mechanical_report": {"key": "m1", "sha256": "t1"},
        }
        with mock.patch.object(cli, "put_state", return_value="b2"):
            cli.record_confidential_evidence(record, "review", {"key": "k2", "sha256": "s2"})
            with self.assertRaisesRegex(ReviewerError, "already recorded"):
                cli.record_confidential_evidence(record, "mechanical_report", {"key": "m2", "sha256": "t2"})

    def test_each_operation_reads_only_its_own_area(self):
        client = snapshot.ObjectClient(SUBMISSION, "restore", token=lambda: "jwt", transport=Transport())
        with self.assertRaisesRegex(ReviewerError, "may not touch"):
            client.get(f"v2/{SUBMISSION}/evidence/review/" + "0" * 64, "0" * 64)

    def test_one_job_reuses_one_client_per_operation(self):
        record = state("a" * 64)
        evidence = cli.confidential_objects(record, "evidence")
        self.assertIs(evidence, cli.confidential_objects(record, "evidence"))
        self.assertIsNot(evidence, cli.confidential_objects(record, "restore"))


RUNS_URL = "https://github.com/PalomarRegistry/PalomarConfidentialRuns/actions/runs/202"


class ContentFreeStateTests(unittest.TestCase):
    """Failure prose, error strings and packets never reach State or its artifacts."""

    CANARY = "canary/secret-path/Main.lean: theorem private_lemma"

    def setUp(self):
        cli._OBJECT_CLIENTS.clear()
        self.client = mock.Mock()
        self.client.put_evidence.side_effect = lambda kind, data: {
            "key": snapshot.evidence_key(SUBMISSION, kind, data), "sha256": snapshot.sha256_bytes(data)
        }
        patcher = mock.patch.object(cli, "confidential_objects", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self):
        return {**state("a" * 64), "status": "verification-reporting", "_blob_sha": "b1",
                "run": {"id": 202}}

    def public_record(self):
        private = {"visibility", "confidential"}
        return {key: value for key, value in self.record().items() if key not in private}

    def failure(self):
        return {
            "phase": "verification",
            "run": {"id": 202, "url": RUNS_URL},
            "repair_draft": {"x": self.CANARY},
            "diagnostics": [{
                "owner": "palomar", "code": "palomar.workflow_incomplete", "stage": "build",
                "summary": self.CANARY, "explanation": self.CANARY, "next_action": self.CANARY,
                "location": self.CANARY, "retryable": True,
            }],
        }

    def test_a_confidential_failure_keeps_only_codes_and_its_alert_still_renders(self):
        record = self.record()
        failure = cli.redacted_confidential_failure(record, self.failure())
        self.assertNotIn(self.CANARY, json.dumps(failure))
        self.assertNotIn("repair_draft", failure)
        self.assertEqual(failure["diagnostics"][0], {
            "owner": "palomar", "code": "palomar.workflow_incomplete", "stage": "build", "retryable": True,
        })
        kind, data = self.client.put_evidence.call_args.args
        self.assertEqual(kind, "failure-text")
        self.assertIn(self.CANARY, data.decode())
        self.assertEqual(failure["text"]["sha256"], snapshot.sha256_bytes(data))
        outbox = cli.operator_notifications.queued_alerts(record, failure, queued_at="2026-11-20T10:00:00Z")
        stored = {**record, "failure": failure, "operator_alerts": outbox}
        message = cli.operator_notifications.alert_message(stored, outbox["items"][0])
        self.assertIn("palomar.workflow_incomplete", message)
        self.assertNotIn(self.CANARY, message)

    def test_a_redacted_diagnostic_is_never_given_the_public_alert(self):
        failure = cli.redacted_confidential_failure(self.record(), self.failure())
        public = self.public_record()
        outbox = cli.operator_notifications.queued_alerts(public, failure, queued_at="2026-11-20T10:00:00Z")
        with self.assertRaisesRegex(ReviewerError, "only the confidential alert form"):
            cli.operator_notifications.alert_message(
                {**public, "failure": failure, "operator_alerts": outbox}, outbox["items"][0]
            )

    def test_error_strings_are_written_as_null_with_a_private_pointer(self):
        writes = []
        def put_state(_path, value, *_args, **_kwargs):
            writes.append(value)

        with mock.patch.object(cli, "put_state", side_effect=put_state):
            cli.advance_state(
                self.record(), "awaiting-review", "The automated review did not complete",
                review_error=self.CANARY, renderability_error=self.CANARY,
                registration_failure={"category": "transient", "detail": self.CANARY},
            )
        written = writes[-1]
        self.assertNotIn(self.CANARY, json.dumps(written))
        self.assertIsNone(written["review_error"])
        self.assertIsNone(written["renderability_error"])
        self.assertNotIn("detail", written["registration_failure"])
        self.assertTrue(written["confidential"]["error_text"]["key"].startswith(
            f"v2/{SUBMISSION}/evidence/failure-text/"
        ))

    def test_public_error_strings_are_unchanged(self):
        record = self.public_record()
        writes = []
        def put_state(_path, value, *_args, **_kwargs):
            writes.append(value)

        with mock.patch.object(cli, "put_state", side_effect=put_state):
            cli.advance_state(record, "awaiting-review", "note", review_error="plain text")
        self.assertEqual(writes[-1]["review_error"], "plain text")
        self.client.put_evidence.assert_not_called()

    def test_a_confidential_workspace_is_outside_the_uploaded_packet_directory(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"RUNNER_TEMP": directory}
        ):
            root = Path(directory) / ".palomar-reviews"
            work = cli.submission_workspace(root, SUBMISSION, self.record())
            self.assertFalse(work.is_relative_to(root))
            work.mkdir(parents=True)
            self.assertEqual(cli.submission_workspace(root, SUBMISSION), work)
            public = {key: value for key, value in self.record().items()
                      if key not in {"visibility", "confidential"}}
            self.assertEqual(cli.submission_workspace(root, SUBMISSION, public), root / SUBMISSION)


if __name__ == "__main__":
    unittest.main()
