"""Contract and injected-I/O orchestration for registration checkpoints."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from . import authorization
from . import mechanical as mechanical_evidence
from . import registration as registration_authority
from .errors import ReviewerError

TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
TITLE_RE = re.compile(r"[^\x00-\x1f\x7f]{1,300}")
GitHub = Callable[[list[str]], str]


def saved_identity(
    state: dict[str, Any],
    *,
    review_sha256: str,
    source_repository: str,
    source_commit: str,
    existing_id: str | None,
) -> tuple[str, str, str, int] | None:
    """Validate and return the retry-stable identity reserved in State."""
    attempt = state.get("registration_attempt")
    if attempt is None:
        return None
    keys = {
        "schema_version",
        "id",
        "version",
        "first_registered_on",
        "registered_at",
        "review_sha256",
        "source_repository",
        "source_commit",
        "existing_id",
    }
    if (
        not isinstance(attempt, dict)
        or bool(set(attempt) - keys)
        or type(attempt.get("schema_version")) is not int
        or attempt["schema_version"] != 2
    ):
        raise ReviewerError("saved registration attempt is malformed")
    bindings = {
        "review_sha256": review_sha256,
        "source_repository": source_repository,
        "source_commit": source_commit,
        "existing_id": existing_id,
    }
    if any(attempt.get(field) != value for field, value in bindings.items()):
        raise ReviewerError("saved registration attempt belongs to different review evidence")

    identifier = attempt.get("id")
    first_registered_on = attempt.get("first_registered_on")
    registered_at = attempt.get("registered_at")
    version = attempt.get("version")
    match = (
        registration_authority.PALOMAR_ID_RE.fullmatch(identifier)
        if isinstance(identifier, str)
        else None
    )
    if (
        match is None
        or not isinstance(first_registered_on, str)
        or match.group("date") != first_registered_on
        or not isinstance(registered_at, str)
        or TIMESTAMP_RE.fullmatch(registered_at) is None
        or type(version) is not int
        or not 1 <= version <= registration_authority.MAX_VERSIONS_PER_RESULT
        or (existing_id is None and version != 1)
        or (existing_id is not None and (identifier != existing_id or version < 2))
    ):
        raise ReviewerError("saved registration attempt has an invalid permanent identity")
    return identifier, first_registered_on, registered_at, version


def branch(submission_id: str, version: int) -> str:
    """Return the sole current branch name for one reserved attempt."""
    if (
        registration_authority.SUBMISSION_ID_RE.fullmatch(submission_id) is None
        or type(version) is not int
        or not 1 <= version <= registration_authority.MAX_VERSIONS_PER_RESULT
    ):
        raise ReviewerError("registration branch identity is malformed")
    return f"submission-{submission_id}-v{version}"


def validate_pr(
    view: Any,
    *,
    number: int,
    branch_name: str,
    database_repository: str,
) -> dict[str, Any]:
    """Require one open same-repository PR for the exact reserved branch."""
    owner = view.get("headRepositoryOwner") if isinstance(view, dict) else None
    head_repository = view.get("headRepository") if isinstance(view, dict) else None
    expected_owner = database_repository.split("/", 1)[0]
    expected_url = f"https://github.com/{database_repository}/pull/{number}"
    if (
        not isinstance(view, dict)
        or view.get("number") != number
        or view.get("state") != "OPEN"
        or view.get("baseRefName") != "main"
        or view.get("headRefName") != branch_name
        or not isinstance(owner, dict)
        or owner.get("login") != expected_owner
        or not isinstance(head_repository, dict)
        or head_repository.get("nameWithOwner") != database_repository
        or view.get("isCrossRepository") is not False
        or not isinstance(view.get("headRefOid"), str)
        or re.fullmatch(r"[0-9a-f]{40}", view["headRefOid"]) is None
        or not isinstance(view.get("createdAt"), str)
        or TIMESTAMP_RE.fullmatch(view["createdAt"]) is None
        or view.get("url") != expected_url
    ):
        raise ReviewerError(f"database PR #{number} is not the expected registration change")
    return view


def entry_path(identity: tuple[str, str, str, int]) -> str:
    """Return the immutable entry path named by a saved identity."""
    identifier, _first_registered_on, _registered_at, version = identity
    return f"entries/{identifier}-v{version}.json"


def validate_record(
    record: Any,
    *,
    submission_id: str,
    review: dict[str, Any],
    state: dict[str, Any],
    identity: tuple[str, str, str, int],
) -> dict[str, Any]:
    """Bind the branch record to the saved identity, review, source, and State."""
    identifier, first_registered_on, registered_at, version = identity
    source = record.get("source") if isinstance(record, dict) else None
    submission = record.get("submission") if isinstance(record, dict) else None
    recorded_review = record.get("review") if isinstance(record, dict) else None
    verification = record.get("verification") if isinstance(record, dict) else None
    workflow_url = verification.get("workflow_url") if isinstance(verification, dict) else None
    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 3
        or record.get("id") != identifier
        or record.get("version") != version
        or record.get("first_registered_on") != first_registered_on
        or record.get("registered_at") != registered_at
        or record.get("status") != "registered"
        or not isinstance(source, dict)
        or source.get("repository") != state.get("repository")
        or source.get("commit") != state.get("commit")
        or not isinstance(submission, dict)
        or submission.get("submission_id") != submission_id
        or not isinstance(recorded_review, dict)
        or recorded_review.get("outcome") != "neutral"
        or recorded_review.get("reviewed_at") != review.get("reviewed_at")
        or recorded_review.get("policy_commit") != review.get("policy_commit")
        or recorded_review.get("reviewer_models") != review.get("reviewer_models")
        or not isinstance(record.get("title"), str)
        or TITLE_RE.fullmatch(record["title"]) is None
        or not isinstance(verification, dict)
        or not isinstance(workflow_url, str)
        or re.fullmatch(
            rf"https://github\.com/{re.escape(mechanical_evidence.SUBMISSION_REPO)}"
            r"/actions/runs/[1-9][0-9]*",
            workflow_url,
        )
        is None
    ):
        raise ReviewerError(
            f"registration branch record {entry_path(identity)} does not match the reserved submission"
        )
    return record


def pr_body(
    submission_id: str,
    record: dict[str, Any],
    *,
    render_workflow_url: str | None,
) -> str:
    """Build the current registration PR description from its immutable record."""
    lines = [
        f"Registers submission `{submission_id}`.",
        "",
        f"- Source: `{record['source']['repository']}@{record['source']['commit']}`",
        f"- Mechanical run: {record['verification']['workflow_url']}",
    ]
    if render_workflow_url is not None:
        lines.append(f"- Render run: {render_workflow_url}")
    return "\n".join(
        [
            *lines,
            f"- Policy: `{record['review']['policy_commit']}`",
            "",
            "This PR was prepared by PalomarReviewer. Merging is the registration event.",
        ]
    )


def remote_branch_commit(gh: GitHub, database_repository: str, branch_name: str) -> str | None:
    """Read an exact ref while distinguishing absence from API failure."""
    owner, repository = database_repository.split("/", 1)
    query = """
        query($owner: String!, $repository: String!, $qualified: String!) {
          repository(owner: $owner, name: $repository) {
            ref(qualifiedName: $qualified) { target { oid } }
          }
        }
    """
    sha = gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repository={repository}",
            "-F",
            f"qualified=refs/heads/{branch_name}",
            "--jq",
            ".data.repository.ref.target.oid // empty",
        ]
    ).strip()
    if not sha:
        return None
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ReviewerError(f"registration branch {branch_name} has an invalid commit identity")
    return sha


def open_pr(gh: GitHub, database_repository: str, branch_name: str) -> int | None:
    """Find at most one same-repository open PR for an exact branch."""
    owner = database_repository.split("/", 1)[0]
    try:
        listed = json.loads(
            gh(
                [
                    "api",
                    "--method",
                    "GET",
                    f"repos/{database_repository}/pulls",
                    "-f",
                    "state=open",
                    "-f",
                    f"head={owner}:{branch_name}",
                    "-f",
                    "per_page=100",
                    "--jq",
                    (
                        "[.[] | {number, state, head: {ref: .head.ref, "
                        "repository: .head.repo.full_name}, base: {ref: .base.ref, "
                        "repository: .base.repo.full_name}}]"
                    ),
                ]
            )
        )
    except json.JSONDecodeError as error:
        raise ReviewerError(f"could not read open registration PRs for {branch_name}") from error
    if not isinstance(listed, list):
        raise ReviewerError(f"could not read open registration PRs for {branch_name}")
    candidates = []
    for item in listed:
        head = item.get("head") if isinstance(item, dict) else None
        base = item.get("base") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or type(item.get("number")) is not int
            or item["number"] <= 0
            or item.get("state") != "open"
            or not isinstance(head, dict)
            or not isinstance(base, dict)
        ):
            raise ReviewerError(f"could not read open registration PRs for {branch_name}")
        if (
            head.get("ref") == branch_name
            and head.get("repository") == database_repository
            and base.get("ref") == "main"
            and base.get("repository") == database_repository
        ):
            candidates.append(item["number"])
    if len(candidates) > 1:
        raise ReviewerError(f"registration branch {branch_name} has multiple open pull requests")
    return candidates[0] if candidates else None


def saved_pr(state: dict[str, Any]) -> int | None:
    """Return one complete current State checkpoint, rejecting half-records."""
    number = state.get("registration_pr")
    created_at = state.get("registration_pr_at")
    if number is None and created_at is None:
        return None
    if (
        type(number) is not int
        or number <= 0
        or not isinstance(created_at, str)
        or TIMESTAMP_RE.fullmatch(created_at) is None
    ):
        raise ReviewerError("submission State has a malformed registration PR checkpoint")
    return number


def read_pr(gh: GitHub, database_repository: str, number: int, branch_name: str) -> dict[str, Any]:
    """Read and validate one exact open registration PR."""
    fields = (
        "number,state,baseRefName,headRefName,headRefOid,headRepository,"
        "headRepositoryOwner,isCrossRepository,createdAt,url"
    )
    try:
        view = json.loads(
            gh(
                [
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    database_repository,
                    "--json",
                    fields,
                ]
            )
        )
    except json.JSONDecodeError as error:
        raise ReviewerError(f"database PR #{number} returned malformed metadata") from error
    return validate_pr(
        view,
        number=number,
        branch_name=branch_name,
        database_repository=database_repository,
    )


def read_record(
    gh: GitHub,
    database_repository: str,
    commit: str,
    *,
    submission_id: str,
    review: dict[str, Any],
    state: dict[str, Any],
    identity: tuple[str, str, str, int],
) -> dict[str, Any]:
    """Load and validate the reserved record at one immutable commit."""
    relative = entry_path(identity)
    try:
        record = json.loads(
            gh(
                [
                    "api",
                    "-H",
                    "Accept: application/vnd.github.raw+json",
                    (
                        f"repos/{database_repository}/contents/{quote(relative, safe='/')}"
                        f"?ref={commit}"
                    ),
                ]
            )
        )
    except json.JSONDecodeError as error:
        raise ReviewerError(f"registration branch record {relative} is malformed") from error
    return validate_record(
        record,
        submission_id=submission_id,
        review=review,
        state=state,
        identity=identity,
    )


def create_pr(
    gh: GitHub,
    database_repository: str,
    branch_name: str,
    *,
    submission_id: str,
    record: dict[str, Any],
    render_workflow_url: str | None,
) -> int:
    """Open a PR for an already-created registration branch."""
    pr_url = gh(
        [
            "pr",
            "create",
            "--repo",
            database_repository,
            "--head",
            branch_name,
            "--base",
            "main",
            "--title",
            f"Add {record['id']} v{record['version']}: {record['title']}",
            "--body",
            pr_body(submission_id, record, render_workflow_url=render_workflow_url),
        ]
    ).strip()
    match = re.fullmatch(
        rf"https://github\.com/{re.escape(database_repository)}/pull/([1-9][0-9]*)",
        pr_url,
    )
    if match is None:
        raise ReviewerError("creating the registration PR returned an unexpected URL")
    return int(match.group(1))


def checkpoint_pr(
    gh: GitHub,
    database_repository: str,
    *,
    submission_id: str,
    review: dict[str, Any],
    state: dict[str, Any],
    identity: tuple[str, str, str, int],
    pr_number: int,
    write_checkpoint: Callable[[dict[str, Any], int, str], None],
) -> int:
    """Bind one validated public PR to State so later passes only finalize it."""
    branch_name = branch(submission_id, identity[3])
    pr = read_pr(gh, database_repository, pr_number, branch_name)
    read_record(
        gh,
        database_repository,
        pr["headRefOid"],
        submission_id=submission_id,
        review=review,
        state=state,
        identity=identity,
    )
    existing = saved_pr(state)
    if existing is not None:
        if existing != pr_number or state.get("registration_pr_at") != pr["createdAt"]:
            raise ReviewerError("submission State points to a different registration PR")
        return pr_number
    write_checkpoint(state, pr_number, pr["createdAt"])
    return pr_number


def recover_change(
    gh: GitHub,
    database_repository: str,
    state_repository: str,
    *,
    submission_id: str,
    review: dict[str, Any],
    read_state: Callable[[str], dict[str, Any] | None],
    write_checkpoint: Callable[[dict[str, Any], int, str], None],
) -> int | None:
    """Recover a reserved branch/PR before repeating public side effects."""
    state = authorization.validate_registration_checkpoint(
        submission_id,
        review,
        read_state(submission_id),
        state_repository=state_repository,
    )
    source = review["source"]
    existing = saved_pr(state)
    identity = saved_identity(
        state,
        review_sha256=authorization.document_digest(review),
        source_repository=source["repository"],
        source_commit=source["commit"],
        existing_id=state.get("existing_id") or None,
    )
    if identity is None:
        if existing is not None:
            raise ReviewerError(
                "submission State has a registration PR without a saved registration attempt"
            )
        return None
    branch_name = branch(submission_id, identity[3])
    if existing is not None:
        return checkpoint_pr(
            gh,
            database_repository,
            submission_id=submission_id,
            review=review,
            state=state,
            identity=identity,
            pr_number=existing,
            write_checkpoint=write_checkpoint,
        )
    pr_number = open_pr(gh, database_repository, branch_name)
    if pr_number is not None:
        return checkpoint_pr(
            gh,
            database_repository,
            submission_id=submission_id,
            review=review,
            state=state,
            identity=identity,
            pr_number=pr_number,
            write_checkpoint=write_checkpoint,
        )

    branch_commit = remote_branch_commit(gh, database_repository, branch_name)
    if branch_commit is None:
        return None
    record = read_record(
        gh,
        database_repository,
        branch_commit,
        submission_id=submission_id,
        review=review,
        state=state,
        identity=identity,
    )
    pr_number = create_pr(
        gh,
        database_repository,
        branch_name,
        submission_id=submission_id,
        record=record,
        render_workflow_url=None,
    )
    return checkpoint_pr(
        gh,
        database_repository,
        submission_id=submission_id,
        review=review,
        state=state,
        identity=identity,
        pr_number=pr_number,
        write_checkpoint=write_checkpoint,
    )
