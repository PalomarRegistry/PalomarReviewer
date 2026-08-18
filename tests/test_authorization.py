import unittest

import palomar_reviewer.authorization as authorization
from palomar_reviewer.errors import ReviewerError

SUBMISSION_ID = "a1b2c3d4e5f6"
STATE_REPOSITORY = "PalomarRegistry/PalomarSubmissionState"


def push_proof(**overrides):
    proof = {
        "schema_version": 1,
        "method": "oauth",
        "binding": "same-account",
        "verified_at": "2026-08-08T00:00:00Z",
        "repository_id": 987654321,
        "commit": "1" * 40,
        "principal": {"login": "someone", "id": 12345},
    }
    proof.update(overrides)
    return proof


def current_contract():
    mechanical = {
        "submission": {
            "submission_id": SUBMISSION_ID,
            "authorization": {"relationship": "maintainer"},
        },
        "source": {"repository": "example/project", "commit": "1" * 40},
    }
    review = {
        "submission_id": SUBMISSION_ID,
        "outcome": "neutral",
        "source": {"repository": "example/project", "commit": "1" * 40},
    }
    digest = authorization.document_digest(review)
    state = {
        "id": SUBMISSION_ID,
        "repository": "example/project",
        "commit": "1" * 40,
        "authorization": {"relationship": "maintainer"},
        "existing_id": None,
        "push_verified": True,
        "push_proof": push_proof(),
        "status": "review-ready",
        "registration_consent": True,
        "review_sha256": digest,
        "registration_consent_review_sha256": digest,
    }
    return mechanical, review, state


def validate(mechanical, review, state):
    return authorization.validate_registration(
        SUBMISSION_ID,
        mechanical,
        review,
        state,
        state_repository=STATE_REPOSITORY,
    )


class DocumentDigestTests(unittest.TestCase):
    def test_digest_is_key_order_independent_and_unicode_preserving(self):
        left = {"snow": "雪", "values": [1, True]}
        right = {"values": [1, True], "snow": "雪"}
        self.assertEqual(authorization.document_digest(left), authorization.document_digest(right))
        self.assertEqual(
            authorization.document_digest(left),
            "8fde5d487dd7cedc3be1103fb5effbc7202426315e535c59cc50736473f64973",
        )


class RegistrationAuthorizationOrderTests(unittest.TestCase):
    def test_the_current_contract_authorizes_the_exact_state_object(self):
        mechanical, review, state = current_contract()
        self.assertIs(validate(mechanical, review, state), state)

    def test_a_technical_team_test_cannot_be_registered_even_with_forged_consent(self):
        mechanical, review, state = current_contract()
        mechanical["submission"]["authorization"] = {"relationship": "technical-test"}
        state.update({
            "authorization": {"relationship": "technical-test"},
            "test_submission": True,
            "push_verified": False,
            "push_proof": {
                **push_proof(),
                "method": "technical-team-test",
                "binding": "active-technical-team-membership",
            },
        })
        with self.assertRaisesRegex(ReviewerError, "technical-team test.*cannot be registered"):
            validate(mechanical, review, state)

    def test_each_technical_team_marker_independently_blocks_registration(self):
        for marker in ("state", "relationship", "proof"):
            with self.subTest(marker=marker):
                mechanical, review, state = current_contract()
                if marker == "state":
                    state["test_submission"] = True
                elif marker == "relationship":
                    authorization_value = {"relationship": "technical-test"}
                    mechanical["submission"]["authorization"] = authorization_value
                    state["authorization"] = authorization_value
                else:
                    state["push_proof"] = push_proof(
                        method="technical-team-test",
                        binding="active-technical-team-membership",
                    )
                with self.assertRaisesRegex(
                    ReviewerError,
                    "technical-team test.*cannot be registered",
                ):
                    validate(mechanical, review, state)

    def test_a_future_nonregistrable_relationship_fails_closed(self):
        mechanical, review, state = current_contract()
        relationship = {"relationship": "future-review-only"}
        mechanical["submission"]["authorization"] = relationship
        state["authorization"] = relationship
        with self.assertRaisesRegex(ReviewerError, "future-review-only.*not registrable"):
            validate(mechanical, review, state)

    def test_registration_recovery_checkpoint_also_refuses_a_technical_test(self):
        _, review, state = current_contract()
        state["test_submission"] = True
        with self.assertRaisesRegex(ReviewerError, "technical-team test.*cannot be registered"):
            authorization.validate_registration_checkpoint(
                SUBMISSION_ID,
                review,
                state,
                state_repository=STATE_REPOSITORY,
            )

    def test_missing_state_fails_with_the_retrieval_context(self):
        mechanical, review, _ = current_contract()
        with self.assertRaisesRegex(
            ReviewerError,
            r"no record in PalomarRegistry/PalomarSubmissionState.*never created it",
        ):
            validate(mechanical, review, None)

    def test_identity_and_source_bindings_fail_before_any_write_proof_claim(self):
        cases = (
            ("state id", lambda mechanical, review, state: state.update(id="other"), "filed under"),
            (
                "report id",
                lambda mechanical, review, state: mechanical["submission"].update(
                    submission_id="other"
                ),
                "mechanical report and state disagree on the submission id",
            ),
            (
                "review id",
                lambda mechanical, review, state: review.update(submission_id="other"),
                "review and state disagree on the submission id",
            ),
            (
                "repository",
                lambda mechanical, review, state: mechanical["source"].update(
                    repository="attacker/project"
                ),
                "disagree on repository",
            ),
            (
                "authorization",
                lambda mechanical, review, state: mechanical["submission"].update(
                    authorization={"relationship": "approved"}
                ),
                "disagree on the authorization",
            ),
        )
        for label, mutate, expected in cases:
            with self.subTest(label=label):
                mechanical, review, state = current_contract()
                state["push_verified"] = False
                state["push_proof"] = None
                mutate(mechanical, review, state)
                with self.assertRaisesRegex(ReviewerError, expected):
                    validate(mechanical, review, state)

    def test_write_proof_fails_before_status_or_stale_consent_can_authorize(self):
        mechanical, review, state = current_contract()
        state.update(
            push_proof=push_proof(method="trust-me"),
            status="withdrawn",
            registered_entry="PALOMAR-2026-08-08-000001-v1",
            registration_consent=False,
        )
        with self.assertRaisesRegex(ReviewerError, "unrecognised method"):
            validate(mechanical, review, state)

    def test_positive_status_one_time_use_and_consent_precede_digest_binding(self):
        cases = (
            ("withdrawn", None, False, "only a submission holding"),
            (
                "review-ready",
                "PALOMAR-2026-08-08-000001-v1",
                False,
                "already registered",
            ),
            ("review-ready", None, False, "has not consented"),
        )
        for status, registered, consent, expected in cases:
            with self.subTest(expected=expected):
                mechanical, review, state = current_contract()
                state.update(
                    status=status,
                    registered_entry=registered,
                    registration_consent=consent,
                    review_sha256="0" * 64,
                    registration_consent_review_sha256="0" * 64,
                )
                with self.assertRaisesRegex(ReviewerError, expected):
                    validate(mechanical, review, state)

    def test_delivered_digest_is_checked_before_the_consented_digest(self):
        mechanical, review, state = current_contract()
        state["review_sha256"] = "0" * 64
        state["registration_consent_review_sha256"] = "f" * 64
        with self.assertRaisesRegex(ReviewerError, "not the review delivered"):
            validate(mechanical, review, state)

        state["review_sha256"] = authorization.document_digest(review)
        with self.assertRaisesRegex(ReviewerError, "consented to a different review"):
            validate(mechanical, review, state)


class RegistrationCheckpointAuthorizationTests(unittest.TestCase):
    def validate(self, review, state):
        return authorization.validate_registration_checkpoint(
            SUBMISSION_ID,
            review,
            state,
            state_repository=STATE_REPOSITORY,
        )

    def test_current_private_standing_authorizes_checkpoint_recovery(self):
        _mechanical, review, state = current_contract()
        self.assertIs(self.validate(review, state), state)

    def test_recovery_binds_the_delivered_review_source_to_state(self):
        _mechanical, review, state = current_contract()
        for field, value in (("repository", "attacker/project"), ("commit", "2" * 40)):
            with self.subTest(field=field):
                changed = {**review, "source": {**review["source"], field: value}}
                digest = authorization.document_digest(changed)
                standing = {
                    **state,
                    "review_sha256": digest,
                    "registration_consent_review_sha256": digest,
                }
                with self.assertRaisesRegex(ReviewerError, f"disagree on {field}"):
                    self.validate(changed, standing)

    def test_recovery_rechecks_consent_before_checkpointing_public_work(self):
        _mechanical, review, state = current_contract()
        state["registration_consent"] = False
        with self.assertRaisesRegex(ReviewerError, "has not consented"):
            self.validate(review, state)

    def test_operator_retry_only_authorizes_a_still_consented_paused_record(self):
        _mechanical, review, state = current_contract()
        state["status"] = "registration-paused"
        self.assertIs(
            authorization.validate_registration_retry(
                SUBMISSION_ID,
                review,
                state,
                state_repository=STATE_REPOSITORY,
            ),
            state,
        )

        state["status"] = "review-ready"
        with self.assertRaisesRegex(ReviewerError, "not registration-paused"):
            authorization.validate_registration_retry(
                SUBMISSION_ID,
                review,
                state,
                state_repository=STATE_REPOSITORY,
            )


if __name__ == "__main__":
    unittest.main()
