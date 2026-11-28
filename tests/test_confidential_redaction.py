"""Confidential text stays out of State, its packets and its artifacts (contract v3.1, C4 and C8)."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import palomar_reviewer.cli as cli
import palomar_reviewer.private_bucket as private_bucket
from palomar_reviewer.errors import ReviewerError

SUBMISSION = "c0nf1d3nt1a1"
RUNS_URL = "https://github.com/PalomarRegistry/PalomarConfidentialRuns/actions/runs/202"
CANARY = "canary/secret-path/Main.lean: theorem private_lemma"
BUCKET_ENV = {
    private_bucket.ENDPOINT_ENV: "https://account.r2.cloudflarestorage.com",
    private_bucket.BUCKET_ENV: "palomar-confidential",
    private_bucket.ACCESS_KEY_ENV: "key-id",
    private_bucket.SECRET_KEY_ENV: "key-secret",
}


class FakeR2:
    """An `aws` CLI that keeps objects in a dict."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[list[str]] = []

    def __call__(self, command, *, env, **_kwargs):
        self.calls.append(command)
        assert command[:3] == ["aws", "--endpoint-url", BUCKET_ENV[private_bucket.ENDPOINT_ENV]]
        assert env["AWS_SECRET_ACCESS_KEY"] == "key-secret"
        args = command[3:]
        option = lambda name: args[args.index(name) + 1]  # noqa: E731
        if args[:2] == ["s3api", "head-object"]:
            if option("--key") in self.objects:
                return subprocess.CompletedProcess(command, 0, "{}", "")
            return subprocess.CompletedProcess(
                command, 254, "", "An error occurred (404) when calling the HeadObject operation: Not Found"
            )
        if args[:2] == ["s3api", "put-object"]:
            self.objects[option("--key")] = Path(option("--body")).read_bytes()
            return subprocess.CompletedProcess(command, 0, "{}", "")
        if args[:2] == ["s3api", "get-object"]:
            Path(args[-1]).write_bytes(self.objects[option("--key")])
            return subprocess.CompletedProcess(command, 0, "{}", "")
        if args[:2] == ["s3", "presign"]:
            key = args[2].removeprefix("s3://palomar-confidential/")
            return subprocess.CompletedProcess(
                command, 0, f"https://r2.example/{key}?X-Amz-Expires={args[-1]}\n", ""
            )
        raise AssertionError(f"unexpected aws call {args}")


def confidential_record(**overrides):
    record = {
        "id": SUBMISSION,
        "visibility": "confidential",
        "repository": "canary-owner/canary-repo",
        "commit": "3" * 40,
        "status": "verification-reporting",
        "run": {"id": 202},
        "confidential": {"source_sha256": "e" * 64},
        "_blob_sha": "b1",
    }
    record.update(overrides)
    return record


def public_record():
    return {key: value for key, value in confidential_record().items()
            if key not in {"visibility", "confidential"}}


class BucketCase(unittest.TestCase):
    def setUp(self):
        self.r2 = FakeR2()
        cli._BUCKET.clear()
        cli._BUCKET.append(private_bucket.Bucket(BUCKET_ENV, runner=self.r2))
        self.addCleanup(cli._BUCKET.clear)

    def withdraw(self):
        self.r2.objects[private_bucket.tombstone_key(SUBMISSION)] = b'{"cleanup":"pending"}'


class BucketTests(BucketCase):
    def test_text_is_stored_under_the_submissions_prefix_by_digest(self):
        pointer = cli.confidential_bucket().store_text(SUBMISSION, {"error": CANARY})
        data = self.r2.objects[pointer["key"]]
        self.assertEqual(pointer["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(pointer["key"], f"conf/{SUBMISSION}/text/{pointer['sha256']}.json")
        self.assertIn(CANARY, data.decode())

    def test_a_missing_credential_fails_closed(self):
        with self.assertRaisesRegex(ReviewerError, "needs the private bucket"):
            private_bucket.Bucket({}, runner=self.r2)

    def test_an_unreadable_tombstone_fails_closed(self):
        def broken(command, **_kwargs):
            return subprocess.CompletedProcess(command, 255, "", "Could not connect to the endpoint URL")

        bucket = private_bucket.Bucket(BUCKET_ENV, runner=broken)
        with self.assertRaisesRegex(ReviewerError, "could not say"):
            bucket.withdrawn(SUBMISSION)

    def test_keys_name_only_a_well_formed_id(self):
        for bad in ("../x", "C0NF1D3NT1A1", ""):
            with self.subTest(bad=bad), self.assertRaises(ReviewerError):
                private_bucket.source_key(bad)


class ContentFreeStateTests(BucketCase):
    def failure(self):
        return {
            "phase": "verification",
            "run": {"id": 202, "url": RUNS_URL},
            "repair_draft": {"x": CANARY},
            "diagnostics": [{
                "owner": "palomar", "code": "palomar.workflow_incomplete", "stage": "build",
                "summary": CANARY, "explanation": CANARY, "next_action": CANARY,
                "location": CANARY, "retryable": True,
            }],
        }

    def test_a_confidential_failure_keeps_only_codes_and_its_alert_still_renders(self):
        record = confidential_record()
        failure = cli.redacted_confidential_failure(record, self.failure())
        self.assertNotIn(CANARY, json.dumps(failure))
        self.assertNotIn("repair_draft", failure)
        self.assertEqual(failure["diagnostics"][0], {
            "owner": "palomar", "code": "palomar.workflow_incomplete", "stage": "build", "retryable": True,
        })
        self.assertIn(CANARY, self.r2.objects[failure["text"]["key"]].decode())
        outbox = cli.operator_notifications.queued_alerts(record, failure, queued_at="2026-11-20T10:00:00Z")
        stored = {**record, "failure": failure, "operator_alerts": outbox}
        message = cli.operator_notifications.alert_message(stored, outbox["items"][0])
        self.assertIn("palomar.workflow_incomplete", message)
        self.assertNotIn(CANARY, message)

    def test_a_redacted_diagnostic_is_never_given_the_public_alert(self):
        failure = cli.redacted_confidential_failure(confidential_record(), self.failure())
        public = public_record()
        outbox = cli.operator_notifications.queued_alerts(public, failure, queued_at="2026-11-20T10:00:00Z")
        with self.assertRaisesRegex(ReviewerError, "only the confidential alert form"):
            cli.operator_notifications.alert_message(
                {**public, "failure": failure, "operator_alerts": outbox}, outbox["items"][0]
            )

    def test_error_strings_are_written_as_null_with_a_private_pointer(self):
        writes = []
        record = lambda _path, value, *_args, **_kwargs: writes.append(value)  # noqa: E731
        with mock.patch.object(cli, "put_state", side_effect=record):
            cli.advance_state(
                confidential_record(), "awaiting-review", "The automated review did not complete",
                review_error=CANARY, renderability_error=CANARY,
                registration_failure={"category": "transient", "detail": CANARY},
            )
        written = writes[-1]
        self.assertNotIn(CANARY, json.dumps(written))
        self.assertIsNone(written["review_error"])
        self.assertIsNone(written["renderability_error"])
        self.assertNotIn("detail", written["registration_failure"])
        self.assertEqual(written["confidential"]["source_sha256"], "e" * 64)
        pointer = written["confidential"]["error_text"]
        self.assertEqual(json.loads(self.r2.objects[pointer["key"]])["review_error"], CANARY)

    def test_an_ingested_confidential_failure_writes_no_prose_to_state(self):
        """The whole ingest path, with the run's failure report quoting the source."""
        failure_report = {"stage": "build", "diagnostics": self.failure()["diagnostics"]}
        writes = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"RUNNER_TEMP": directory}
        ), mock.patch.object(
            cli, "trusted_submission_run",
            return_value={"databaseId": 202, "attempt": 1, "url": RUNS_URL, "conclusion": "failure"},
        ), mock.patch.object(cli, "validate_workflow_commit_on_main"), mock.patch.object(
            cli, "download_mechanical_artifact", return_value=Path(directory) / "report.json",
        ) as download, mock.patch.object(cli, "load_json", return_value=failure_report), mock.patch.object(
            cli.workflow_recovery, "validate_execution_binding"
        ), mock.patch.object(
            cli, "validated_failure_report",
            return_value={"phase": "verification", "diagnostics": self.failure()["diagnostics"],
                          "repair_draft": {"x": CANARY}, "profile_version": 4},
        ), mock.patch.object(
            cli, "put_state", side_effect=lambda path, value, *_a, **_k: writes.append((path, value))
        ):
            root = Path(directory) / ".palomar-reviews"
            try:
                cli.ingest_failure_diagnostics(confidential_record(), root)
            except ReviewerError:
                pass
            self.assertFalse(download.call_args.args[2].is_relative_to(root))
        self.assertTrue(writes)
        for _path, value in writes:
            self.assertNotIn(CANARY, json.dumps(value))

    def test_public_error_strings_are_unchanged(self):
        writes = []
        record = lambda _path, value, *_args, **_kwargs: writes.append(value)  # noqa: E731
        with mock.patch.object(cli, "put_state", side_effect=record):
            cli.advance_state(public_record(), "awaiting-review", "note", review_error="plain text")
        self.assertEqual(writes[-1]["review_error"], "plain text")
        self.assertEqual(self.r2.calls, [])


class PacketTests(BucketCase):
    def test_a_confidential_workspace_is_outside_the_uploaded_packet_directory(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"RUNNER_TEMP": str(Path(directory) / "temp")}
        ):
            root = Path(directory) / "workspace" / ".palomar-reviews"
            work = cli.submission_workspace(root, SUBMISSION, confidential_record())
            self.assertFalse(work.is_relative_to(root))
            work.mkdir(parents=True)
            self.assertEqual(cli.submission_workspace(root, SUBMISSION), work)
            self.assertEqual(cli.submission_workspace(root, SUBMISSION, public_record()), root / SUBMISSION)

    def test_a_workspace_inside_the_packet_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"RUNNER_TEMP": str(Path(directory) / ".palomar-reviews")}
        ), self.assertRaisesRegex(ReviewerError, "outside the uploaded work directory"):
            cli.submission_workspace(Path(directory) / ".palomar-reviews", SUBMISSION, confidential_record())

    def test_the_state_workflow_uploads_only_the_work_directory(self):
        """State's review-packet artifact is `.palomar-reviews/`, the default work directory."""
        args = cli.build_parser().parse_args(["auto"])
        self.assertEqual(Path(args.work_dir).name, ".palomar-reviews")


class LogTests(unittest.TestCase):
    def setUp(self):
        cli.confidential_mode._MASKED.clear()
        self.addCleanup(cli.confidential_mode._MASKED.clear)

    def test_a_confidential_records_source_is_masked_in_the_actions_log_once(self):
        masked = []
        record = confidential_record()
        for _ in range(2):
            cli.confidential_mode.withhold_from_logs(record, emit=masked.append)
        self.assertEqual(sorted(masked), sorted({
            "canary-owner/canary-repo", "canary-owner-canary-repo", "3" * 40, "3" * 12, "3" * 7,
        }))
        cli.confidential_mode.withhold_from_logs(public_record(), emit=masked.append)
        self.assertEqual(len(masked), 5)

    def test_reading_a_record_masks_it_before_anything_can_print_it(self):
        import contextlib
        import io

        output = io.StringIO()
        with mock.patch.object(cli, "state_json", return_value=confidential_record()), \
                contextlib.redirect_stdout(output):
            cli.submission_state(SUBMISSION)
        self.assertIn("::add-mask::canary-owner/canary-repo\n", output.getvalue())
        self.assertIn("::add-mask::" + "3" * 40 + "\n", output.getvalue())


class WithdrawalTests(BucketCase):
    def test_a_withdrawn_submission_is_left_out_of_the_pass_but_kept_queued(self):
        self.withdraw()
        records = {SUBMISSION: confidential_record(status="awaiting-review"), "pub11pub11pu": {
            **public_record(), "id": "pub11pub11pu", "status": "awaiting-review"}}
        with mock.patch.object(
            cli, "open_index", return_value={"open": list(records), "_blob_sha": "i1"}
        ), mock.patch.object(cli, "submission_states", return_value=records), mock.patch.object(
            cli, "_write_open_index"
        ) as write_index:
            selected = cli.open_submissions()
        self.assertEqual([record["id"] for record in selected], ["pub11pub11pu"])
        write_index.assert_not_called()

    def test_an_unreachable_bucket_skips_only_the_confidential_submission(self):
        cli._BUCKET.clear()
        records = {SUBMISSION: confidential_record(status="awaiting-review"), "pub11pub11pu": {
            **public_record(), "id": "pub11pub11pu", "status": "awaiting-review"}}
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            cli, "open_index", return_value={"open": list(records), "_blob_sha": "i1"}
        ), mock.patch.object(cli, "submission_states", return_value=records), mock.patch.object(
            cli, "_write_open_index"
        ) as write_index:
            selected = cli.open_submissions()
        self.assertEqual([record["id"] for record in selected], ["pub11pub11pu"])
        write_index.assert_not_called()

    def test_nothing_is_written_for_a_withdrawn_submission(self):
        self.withdraw()
        with mock.patch.dict(os.environ, {"PALOMAR_ALLOW_STATE_WRITES": "1"}), mock.patch.object(
            cli, "gh"
        ) as gh:
            with self.assertRaisesRegex(ReviewerError, "withdrawn"):
                cli.advance_state(confidential_record(), "awaiting-review", "note")
            with self.assertRaisesRegex(ReviewerError, "withdrawn"):
                cli.store_confidential_text(confidential_record(), {"error": CANARY})
        gh.assert_not_called()
        self.assertEqual(
            [key for key in self.r2.objects if key.startswith("conf/")], []
        )

    def test_public_submissions_never_read_the_bucket(self):
        self.assertFalse(cli.confidential_withdrawn(public_record()))
        self.assertEqual(self.r2.calls, [])


if __name__ == "__main__":
    unittest.main()
