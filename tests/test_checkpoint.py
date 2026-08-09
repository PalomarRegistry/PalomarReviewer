import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

import palomar_reviewer.authorization as registration_authorization
import palomar_reviewer.checkpoint as checkpoint
import palomar_reviewer.cli as cli
from palomar_reviewer.errors import ReviewerError


def push_proof(commit):
    return {
        "schema_version": 1,
        "method": "oauth",
        "binding": "same-account",
        "verified_at": "2026-08-09T10:00:00Z",
        "repository_id": 123,
        "commit": commit,
        "principal": {"login": "submitter", "id": 456},
    }


class RegistrationCheckpointRecoveryTests(unittest.TestCase):
    submission_id = "a1b2c3d4e5f6"
    identifier = "PALOMAR-2026-08-09-000123"
    registered_at = "2026-08-09T12:34:56Z"

    def fixtures(self):
        review = {
            "schema_version": 2,
            "submission_id": self.submission_id,
            "decision": "accept",
            "source": {"repository": "example/project", "commit": "1" * 40},
            "reviewed_at": "2026-08-09T11:00:00Z",
            "policy_commit": "2" * 40,
            "reviewer_models": ["gpt-5.6-sol"],
        }
        digest = registration_authorization.document_digest(review)
        state = {
            "id": self.submission_id,
            "repository": "example/project",
            "commit": "1" * 40,
            "authorization": {"relationship": "maintainer"},
            "existing_id": None,
            "push_verified": True,
            "push_proof": push_proof("1" * 40),
            "status": "review-ready",
            "registration_consent": True,
            "review_sha256": digest,
            "registration_consent_review_sha256": digest,
            "registration_attempt": {
                "schema_version": 1,
                "id": self.identifier,
                "version": 1,
                "accepted_at": "2026-08-09",
                "registered_at": self.registered_at,
                "review_sha256": digest,
                "source_repository": "example/project",
                "source_commit": "1" * 40,
                "existing_id": None,
            },
            "_blob_sha": "state-blob",
        }
        record = {
            "schema_version": 2,
            "id": self.identifier,
            "version": 1,
            "accepted_at": "2026-08-09",
            "registered_at": self.registered_at,
            "status": "accepted",
            "title": "A result",
            "source": {"repository": "example/project", "commit": "1" * 40},
            "submission": {"submission_id": self.submission_id},
            "verification": {
                "workflow_url": (
                    "https://github.com/PalomarRegistry/PalomarSubmission/actions/runs/123"
                )
            },
            "review": {
                "verdict": "accept",
                "reviewed_at": review["reviewed_at"],
                "policy_commit": review["policy_commit"],
                "reviewer_models": review["reviewer_models"],
            },
        }
        return review, state, record

    def pr(self, number=34, **overrides):
        return {
            "number": number,
            "state": "OPEN",
            "baseRefName": "main",
            "headRefName": f"submission-{self.submission_id}-v1",
            "headRefOid": "3" * 40,
            "headRepository": {
                "name": "PalomarDatabase",
                "nameWithOwner": "PalomarRegistry/PalomarDatabase",
            },
            "headRepositoryOwner": {"login": "PalomarRegistry"},
            "isCrossRepository": False,
            "createdAt": "2026-08-09T12:40:00Z",
            "url": f"https://github.com/PalomarRegistry/PalomarDatabase/pull/{number}",
            **overrides,
        }

    def test_retry_checkpoints_an_open_pr_before_workspace_or_public_side_effects(self):
        review, state, record = self.fixtures()
        args = SimpleNamespace(
            submission=self.submission_id,
            work_dir="/unused",
            render_result=None,
            dry_run=False,
        )
        blocked_names = (
            "prepare_workspace",
            "preserve_sources",
            "request_render",
            "resolve_remote_commit",
            "clone_at",
            "push_registration_branch",
        )
        with contextlib.ExitStack() as stack:
            blocked = {
                name: stack.enter_context(mock.patch.object(cli, name))
                for name in blocked_names
            }
            stack.enter_context(mock.patch.object(cli, "delivered_review", return_value=review))
            stack.enter_context(mock.patch.object(cli, "submission_state", return_value=state))
            stack.enter_context(mock.patch.object(checkpoint, "open_pr", return_value=34))
            stack.enter_context(mock.patch.object(checkpoint, "read_pr", return_value=self.pr()))
            loaded = stack.enter_context(
                mock.patch.object(checkpoint, "read_record", return_value=record)
            )
            created = stack.enter_context(mock.patch.object(checkpoint, "create_pr"))
            advanced = stack.enter_context(mock.patch.object(cli, "advance_state"))

            self.assertEqual(cli.register(args), 0)

        for operation in blocked.values():
            operation.assert_not_called()
        created.assert_not_called()
        loaded.assert_called_once_with(
            cli.gh,
            cli.DATABASE_REPO,
            "3" * 40,
            submission_id=self.submission_id,
            review=review,
            state=state,
            identity=(self.identifier, "2026-08-09", self.registered_at, 1),
        )
        self.assertEqual(advanced.call_args.kwargs["registration_pr"], 34)
        self.assertEqual(
            advanced.call_args.kwargs["registration_pr_at"], "2026-08-09T12:40:00Z"
        )

    def test_retry_opens_a_pr_from_a_valid_created_branch_without_rebuilding(self):
        review, state, record = self.fixtures()
        with (
            mock.patch.object(checkpoint, "open_pr", return_value=None),
            mock.patch.object(checkpoint, "remote_branch_commit", return_value="3" * 40),
            mock.patch.object(checkpoint, "read_record", return_value=record) as loaded,
            mock.patch.object(checkpoint, "create_pr", return_value=35) as created,
            mock.patch.object(checkpoint, "read_pr", return_value=self.pr(35)),
        ):
            recovered = checkpoint.recover_change(
                cli.gh,
                cli.DATABASE_REPO,
                cli.STATE_REPO,
                submission_id=self.submission_id,
                review=review,
                read_state=lambda _submission: state,
                write_checkpoint=lambda *_args: None,
            )
        self.assertEqual(recovered, 35)
        self.assertEqual(loaded.call_count, 2)
        self.assertEqual(created.call_args.kwargs["render_workflow_url"], None)
        self.assertEqual(created.call_args.kwargs["record"], record)

    def test_no_saved_attempt_performs_no_database_lookup(self):
        review, state, _record = self.fixtures()
        state["registration_attempt"] = None
        github = mock.Mock()
        recovered = checkpoint.recover_change(
            github,
            cli.DATABASE_REPO,
            cli.STATE_REPO,
            submission_id=self.submission_id,
            review=review,
            read_state=lambda _submission: state,
            write_checkpoint=lambda *_args: None,
        )
        self.assertIsNone(recovered)
        github.assert_not_called()

    def test_a_checkpoint_without_its_saved_attempt_is_rejected_without_database_reads(self):
        review, state, _record = self.fixtures()
        state["registration_attempt"] = None
        state.update(
            registration_pr=34,
            registration_pr_at="2026-08-09T12:40:00Z",
        )
        github = mock.Mock()
        with self.assertRaisesRegex(ReviewerError, "without a saved registration attempt"):
            checkpoint.recover_change(
                github,
                cli.DATABASE_REPO,
                cli.STATE_REPO,
                submission_id=self.submission_id,
                review=review,
                read_state=lambda _submission: state,
                write_checkpoint=lambda *_args: None,
            )
        github.assert_not_called()

    def test_an_already_recorded_exact_checkpoint_is_idempotent(self):
        review, state, record = self.fixtures()
        state.update(
            registration_pr=34,
            registration_pr_at="2026-08-09T12:40:00Z",
        )
        write_checkpoint = mock.Mock()
        with (
            mock.patch.object(checkpoint, "read_pr", return_value=self.pr()),
            mock.patch.object(checkpoint, "read_record", return_value=record),
        ):
            recovered = checkpoint.checkpoint_pr(
                cli.gh,
                cli.DATABASE_REPO,
                submission_id=self.submission_id,
                review=review,
                state=state,
                identity=(self.identifier, "2026-08-09", self.registered_at, 1),
                pr_number=34,
                write_checkpoint=write_checkpoint,
            )
        self.assertEqual(recovered, 34)
        write_checkpoint.assert_not_called()

    def test_saved_checkpoint_is_validated_before_discovering_or_creating_a_pr(self):
        review, state, _record = self.fixtures()
        state.update(
            registration_pr=34,
            registration_pr_at="2026-08-09T12:40:00Z",
        )
        with (
            mock.patch.object(checkpoint, "checkpoint_pr", return_value=34) as checked,
            mock.patch.object(checkpoint, "open_pr") as discovered,
            mock.patch.object(checkpoint, "remote_branch_commit") as branch_read,
            mock.patch.object(checkpoint, "create_pr") as created,
        ):
            recovered = checkpoint.recover_change(
                cli.gh,
                cli.DATABASE_REPO,
                cli.STATE_REPO,
                submission_id=self.submission_id,
                review=review,
                read_state=lambda _submission: state,
                write_checkpoint=lambda *_args: None,
            )
        self.assertEqual(recovered, 34)
        checked.assert_called_once()
        discovered.assert_not_called()
        branch_read.assert_not_called()
        created.assert_not_called()

    def test_half_recorded_checkpoint_is_rejected_before_database_reads(self):
        review, state, _record = self.fixtures()
        state["registration_pr"] = 34
        github = mock.Mock()
        with self.assertRaisesRegex(ReviewerError, "malformed registration PR checkpoint"):
            checkpoint.recover_change(
                github,
                cli.DATABASE_REPO,
                cli.STATE_REPO,
                submission_id=self.submission_id,
                review=review,
                read_state=lambda _submission: state,
                write_checkpoint=lambda *_args: None,
            )
        github.assert_not_called()

    def test_checkpoint_refuses_a_pr_from_another_owner_or_head(self):
        for label, override in (
            ("owner", {"headRepositoryOwner": {"login": "attacker"}}),
            ("head", {"headRefOid": "not-a-commit"}),
            ("base", {"baseRefName": "other"}),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(
                ReviewerError, "not the expected"
            ):
                checkpoint.validate_pr(
                    self.pr(**override),
                    number=34,
                    branch_name=f"submission-{self.submission_id}-v1",
                    database_repository=cli.DATABASE_REPO,
                )

    def test_current_pr_and_record_contract_accepts_the_complete_fixture(self):
        review, state, record = self.fixtures()
        pr = self.pr()
        self.assertIs(
            checkpoint.validate_pr(
                pr,
                number=34,
                branch_name=f"submission-{self.submission_id}-v1",
                database_repository=cli.DATABASE_REPO,
            ),
            pr,
        )
        self.assertIs(
            checkpoint.validate_record(
                record,
                submission_id=self.submission_id,
                review=review,
                state=state,
                identity=(self.identifier, "2026-08-09", self.registered_at, 1),
            ),
            record,
        )

    def test_checkpoint_refuses_a_record_that_does_not_match_the_reservation(self):
        review, state, record = self.fixtures()
        record["submission"] = {"submission_id": "b2c3d4e5f6a1"}
        with self.assertRaisesRegex(ReviewerError, "does not match the reserved submission"):
            checkpoint.validate_record(
                record,
                submission_id=self.submission_id,
                review=review,
                state=state,
                identity=(self.identifier, "2026-08-09", self.registered_at, 1),
            )

    def test_checkpoint_refuses_record_fields_used_to_create_the_pr(self):
        review, state, record = self.fixtures()
        for label, replacement in (
            ("schema", {"schema_version": 1}),
            ("title", {"title": ""}),
            ("multiline title", {"title": "A result\nwith a second line"}),
            ("verification", {"verification": {"workflow_url": "https://attacker.test"}}),
        ):
            with self.subTest(label=label):
                malformed = {**record, **replacement}
                with self.assertRaisesRegex(
                    ReviewerError, "does not match the reserved submission"
                ):
                    checkpoint.validate_record(
                        malformed,
                        submission_id=self.submission_id,
                        review=review,
                        state=state,
                        identity=(self.identifier, "2026-08-09", self.registered_at, 1),
                    )


if __name__ == "__main__":
    unittest.main()
