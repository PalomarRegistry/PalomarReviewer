"""Pure registration authorization and consent bindings.

The CLI remains the composition root: it retrieves the private State record,
validates the inspected workspace, and performs every network, filesystem, and
public registration action. This module decides whether that already-retrieved
record authorizes the exact reviewed evidence to proceed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .errors import ReviewerError

# How a submitter may prove they can write to the repository they are
# submitting. Adding a route to the submission server is not enough to admit a
# new one: it has to be named here, which is the point. Each entry says what the
# method actually establishes, because they do not all establish the same thing.
PUSH_PROOF_METHODS = {
    # The submitter authorised Palomar and GitHub answered `permissions.push`
    # for that same authenticated account. One account, both facts.
    "oauth": "same-account",
    # A tag created at the submitted commit proves someone holds `contents:
    # write`; a gist proves an account named itself. Nothing ties the two
    # together, so two colluding accounts could separate the credential that
    # can push from the identity on the record. Weaker than `oauth`, knowingly.
    "tag-and-gist": "separately-attested",
}

# Registration is an allowlist. A future relationship may be valid for review
# without being valid for registration, and must not become registrable merely
# because a mechanical-report enum was widened elsewhere.
REGISTRABLE_RELATIONSHIPS = frozenset({"maintainer", "approved"})


def document_digest(document: dict[str, Any]) -> str:
    """Hash one JSON document in the canonical form used by evidence bindings."""
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_push_proof(state: dict[str, Any]) -> None:
    """Require a described proof, not merely a boolean somebody set.

    `push_verified` is a hardcoded literal in the submission server: it records
    that a code path ran, not what it observed. That was adequate while one
    path could set it. With more than one, the record has to say which, so that
    admitting a new method is a decision taken here rather than a side effect
    of deploying a server. Creation time is not proof and survives a later
    review rerun, so it deliberately creates no exception to this requirement.

    The submission server records the GitHub repository and principal IDs. The
    reviewer validates that evidence contract and binds the proof to the
    reviewed commit; it does not independently resolve the repository ID from
    GitHub.
    """
    proof = state.get("push_proof")
    if proof is None:
        raise ReviewerError(
            "the submission record carries no push_proof; every registration "
            "must describe how write access was proved"
        )

    if not isinstance(proof, dict):
        raise ReviewerError("push_proof is not an object")
    schema_version = proof.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise ReviewerError("push_proof schema_version must be the integer 1")
    method = proof.get("method")
    if method not in PUSH_PROOF_METHODS:
        raise ReviewerError(
            f"push_proof names an unrecognised method: {method!r}. A method must be "
            "described in PUSH_PROOF_METHODS before a record relying on it registers."
        )
    if proof.get("binding") != PUSH_PROOF_METHODS[method]:
        raise ReviewerError(
            f"push_proof method {method!r} claims binding {proof.get('binding')!r}, "
            f"but that method establishes {PUSH_PROOF_METHODS[method]!r}"
        )
    for field in ("verified_at", "commit"):
        if not proof.get(field):
            raise ReviewerError(f"push_proof is missing {field}")
    repository_id = proof.get("repository_id")
    if (
        not isinstance(repository_id, int)
        or isinstance(repository_id, bool)
        or repository_id <= 0
    ):
        raise ReviewerError("push_proof repository_id must be a positive integer")
    if proof.get("commit") != state.get("commit"):
        raise ReviewerError("push_proof was made against a different commit")
    principal = proof.get("principal")
    if not isinstance(principal, dict) or "id" not in principal:
        raise ReviewerError("push_proof does not identify who proved it")
    principal_id = principal.get("id")
    if (
        not isinstance(principal_id, int)
        or isinstance(principal_id, bool)
        or principal_id <= 0
    ):
        raise ReviewerError("push_proof principal.id must be a positive integer")


def is_technical_team_test(state: dict[str, Any]) -> bool:
    """Recognize every redundant marker emitted for an operator test."""
    state_authorization = state.get("authorization")
    state_proof = state.get("push_proof")
    return (
        state.get("test_submission") is True
        or (
            isinstance(state_authorization, dict)
            and state_authorization.get("relationship") == "technical-test"
        )
        or (
            isinstance(state_proof, dict)
            and state_proof.get("method") == "technical-team-test"
        )
    )


def _validate_registration_standing(
    submission_id: str,
    review: dict[str, Any],
    state: dict[str, Any],
) -> None:
    """Require current consent and authority for one delivered review."""
    state_authorization = state.get("authorization")
    if is_technical_team_test(state):
        raise ReviewerError("a technical-team test submission cannot be registered")
    relationship = (
        state_authorization.get("relationship")
        if isinstance(state_authorization, dict)
        else None
    )
    if relationship not in REGISTRABLE_RELATIONSHIPS:
        raise ReviewerError(f"authorization relationship {relationship!r} is not registrable")
    if state.get("push_verified") is not True:
        raise ReviewerError("the submitter never proved write access to the repository")
    validate_push_proof(state)
    # A positive status, not merely "not withdrawn": a stale consent flag on a
    # record that has gone back to any other state must not authorize anything.
    if state.get("status") != "review-ready":
        raise ReviewerError(
            f"submission {submission_id} is {state.get('status')}, and only a submission "
            "holding a delivered review may be registered"
        )
    if state.get("registered_entry"):
        raise ReviewerError(
            f"submission {submission_id} was already registered as {state['registered_entry']}"
        )
    if state.get("registration_consent") is not True:
        raise ReviewerError(
            "the submitter has not consented to registration; "
            "nothing is registered until they choose to"
        )
    # Consent is to the exact review the submitter read. The digest recorded at
    # delivery, the digest they consented to, and the review about to be
    # archived must all be the same canonical document.
    delivered = state.get("review_sha256")
    consented = state.get("registration_consent_review_sha256")
    registering = document_digest(review)
    if delivered != registering:
        raise ReviewerError(
            "the review being registered is not the review delivered to the submitter"
        )
    if consented != registering:
        raise ReviewerError("the submitter consented to a different review")


def validate_registration_checkpoint(
    submission_id: str,
    review: dict[str, Any],
    state: dict[str, Any] | None,
    *,
    state_repository: str,
) -> dict[str, Any]:
    """Authorize recovery of an already-created registration branch.

    The public branch is inspected separately. This narrower check exists so a
    retry can recover that branch before rebuilding a workspace or repeating
    archive/render side effects, while still re-reading the private consent and
    exact delivered-review binding which authorize registration.
    """
    if state is None:
        raise ReviewerError(
            f"submission {submission_id} has no record in {state_repository}: "
            "the submission server never created it"
        )
    if state.get("id") != submission_id:
        raise ReviewerError("state record is filed under a different submission id")
    if review.get("submission_id") != submission_id:
        raise ReviewerError("review and state disagree on the submission id")
    source = review.get("source")
    if not isinstance(source, dict):
        raise ReviewerError("review has no source identity for registration recovery")
    if (
        not isinstance(source.get("repository"), str)
        or not source["repository"]
        or not isinstance(source.get("commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", source["commit"]) is None
    ):
        raise ReviewerError("review has a malformed source identity for registration recovery")
    for field in ("repository", "commit"):
        if source.get(field) != state.get(field):
            raise ReviewerError(f"review and state disagree on {field}")
    _validate_registration_standing(submission_id, review, state)
    return state


def validate_registration(
    submission_id: str,
    mechanical: dict[str, Any],
    review: dict[str, Any],
    state: dict[str, Any] | None,
    *,
    state_repository: str,
) -> dict[str, Any]:
    """Refuse evidence the retrieved private State record did not authorize.

    The submission id is public: it appears in the verification run's name, so
    anyone able to dispatch the workflow can produce a mechanical report
    carrying a real one. Existence of a state record is therefore not enough.
    What is checked is that the private record and the report describe the same
    submission, that the record carries the Server's supported write-access
    proof contract, that the submitter has not withdrawn and explicitly
    consented to registration, and that nothing has been registered for this
    submission already. Reviewer validates and binds that recorded contract; it
    does not independently resolve its repository ID against GitHub.

    ``state_repository`` is used only to retain an actionable missing-record
    diagnostic while the caller remains responsible for retrieving the record.
    """
    if state is None:
        raise ReviewerError(
            f"submission {submission_id} has no record in {state_repository}: "
            "the submission server never created it"
        )

    submission = mechanical.get("submission", {})
    if state.get("id") != submission_id:
        raise ReviewerError("state record is filed under a different submission id")
    if submission.get("submission_id") != submission_id:
        raise ReviewerError("mechanical report and state disagree on the submission id")
    if review.get("submission_id") != submission_id:
        raise ReviewerError("review and state disagree on the submission id")

    for field, reported, recorded in (
        ("repository", mechanical["source"]["repository"], state.get("repository")),
        ("commit", mechanical["source"]["commit"], state.get("commit")),
    ):
        if reported != recorded:
            raise ReviewerError(
                f"mechanical report and state disagree on {field}: "
                f"{reported!r} against {recorded!r}"
            )

    if submission.get("authorization") != state.get("authorization"):
        raise ReviewerError("mechanical report and state disagree on the authorization")
    if (mechanical.get("existing_id") or None) != (state.get("existing_id") or None):
        raise ReviewerError("mechanical report and state disagree on the update intent")
    _validate_registration_standing(submission_id, review, state)
    return state
