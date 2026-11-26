"""Confidential submissions: where their runs happen and how those runs are trusted.

A confidential submission (``state.visibility == "confidential"``) is verified
and rendered in the private caller repository
``PalomarRegistry/PalomarConfidentialRuns`` instead of the public
PalomarSubmission. The caller holds two tiny workflows that call
PalomarSubmission's reusable workflows at one pinned full commit, so the
verifier code is still the public, reviewed code; only the run, its logs and
its artifacts are private.

The run's repository is therefore not evidence by itself. A confidential run
is trusted only if all of these hold (contract v2 sections 3.1 and 3.2a):

1. its workflow path is the caller's ``verify.yml`` (or ``render.yml``);
2. the run's ``referenced_workflows`` names the called PalomarSubmission
   workflow at one commit P, and P is in the confidential pipeline allowlist
   and not revoked;
3. the caller file at the run's head commit, with P put back as the
   placeholder, has the digest PalomarSubmission's canonical ``SHA256SUMS``
   lists at P, so a modified caller is refused;
4. the report says it executed P, and P is on PalomarSubmission's trusted
   history (checked by the caller).

Visibility is chosen at intake and never comes from the run itself: the
submission's State record says which origin its runs must have, and a run from
the other origin is refused.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import ReviewerError

SUBMISSION_REPO = "PalomarRegistry/PalomarSubmission"
RUNS_REPO = "PalomarRegistry/PalomarConfidentialRuns"
VISIBILITIES = ("public", "confidential")
PIPELINE_PLACEHOLDER = "<pipeline-commit>"

# The canonical caller templates live in PalomarSubmission at
# `templates/confidential-callers/` with their `SHA256SUMS`, the placeholder
# standing for the one pipeline commit P used in both `uses:` and
# `pipeline_commit` (contract v2 section 3.2a). A caller file is checked by
# putting the placeholder back and comparing its digest with SHA256SUMS read
# from PalomarSubmission at P itself, so no template text is copied here.
TEMPLATE_DIRECTORY = "templates/confidential-callers"
CALLERS = ("verify.yml", "render.yml")
PIPELINE_ALLOWLIST_ENV = "PALOMAR_CONFIDENTIAL_PIPELINE_ALLOWLIST"
PIPELINE_REVOKED_ENV = "PALOMAR_CONFIDENTIAL_PIPELINE_REVOKED"


def template_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_sha256sums(text: str) -> dict[str, str]:
    """`sha256sum` output for exactly the two caller templates."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64}) [ *]([a-z]+\.yml)", line)
        if match is None or match.group(2) in sums:
            raise ReviewerError("caller template SHA256SUMS is malformed")
        sums[match.group(2)] = match.group(1)
    if set(sums) != set(CALLERS):
        raise ReviewerError("caller template SHA256SUMS must list exactly verify.yml and render.yml")
    return sums


def _commit_set(value: str | None) -> set[str]:
    commits = {item for item in re.split(r"[\s,]+", value or "") if item}
    if any(not re.fullmatch(r"[0-9a-f]{40}", item) for item in commits):
        raise ReviewerError("confidential pipeline allowlist or revocation list is malformed")
    return commits


def require_allowed_pipeline(pipeline_commit: str, environment: dict[str, str]) -> None:
    """P must be allowlisted and not revoked, independent of trusted Git history.

    The lists are the State workflow's copies of the Worker's
    `CONFIDENTIAL_PIPELINE_ALLOWLIST` and `CONFIDENTIAL_PIPELINE_REVOKED`.
    An absent allowlist refuses every confidential run.
    """
    allowed = _commit_set(environment.get(PIPELINE_ALLOWLIST_ENV))
    revoked = _commit_set(environment.get(PIPELINE_REVOKED_ENV))
    if pipeline_commit in revoked:
        raise ReviewerError("confidential run used a revoked pipeline commit")
    if pipeline_commit not in allowed:
        raise ReviewerError("confidential run used a pipeline commit that is not allowlisted")


CALLED_WORKFLOW = {"verify.yml": "submission.yml", "render.yml": "render-challenge.yml"}


@dataclass(frozen=True)
class RunOrigin:
    """Where a submission's verification and render runs must have happened."""

    repository: str
    verify_workflow: str
    render_workflow: str

    @property
    def confidential(self) -> bool:
        return self.repository == RUNS_REPO

    def run_url(self, run_id: int | str) -> str:
        return f"https://github.com/{self.repository}/actions/runs/{run_id}"

    def run_url_pattern(self) -> str:
        return rf"https://github\.com/{re.escape(self.repository)}/actions/runs/[1-9][0-9]*"

    def verify_title(self, submission_id: str, mode: str = "full") -> str:
        if not self.confidential:
            return {
                "preflight": f"Preflight submission {submission_id}",
                "correction": f"Validate registry correction {submission_id}",
            }.get(mode, f"Verify submission {submission_id}")
        if mode != "full":
            raise ReviewerError(f"a confidential submission has no {mode} runs")
        return f"Verify {submission_id}"

    def render_titles(self, mechanical: dict[str, Any], request_id: str) -> tuple[str, ...]:
        """Run names a render run for this request may carry.

        Render runs are named by request id only. A public render dispatched
        before PalomarSubmission dropped the source from its run name still
        carries the old ``Render <repository>@<commit>`` name, so a public
        request accepts both; a confidential one never named its source.
        """
        quiet = f"Render challenge [{request_id}]"
        if self.confidential:
            return (quiet,)
        source = mechanical["source"]
        return (quiet, f"Render {source['repository']}@{source['commit']} [{request_id}]")


PUBLIC_ORIGIN = RunOrigin(SUBMISSION_REPO, "submission.yml", "render-challenge.yml")
CONFIDENTIAL_ORIGIN = RunOrigin(RUNS_REPO, "verify.yml", "render.yml")
ORIGINS = (PUBLIC_ORIGIN, CONFIDENTIAL_ORIGIN)
RUN_URL_RE = re.compile(
    r"https://github\.com/PalomarRegistry/(?:PalomarSubmission|PalomarConfidentialRuns)"
    r"/actions/runs/[1-9][0-9]*\Z"
)


def visibility(state: dict[str, Any]) -> str:
    """The submission's visibility. Absent means public; anything else is refused."""
    value = state.get("visibility", "public")
    if value not in VISIBILITIES:
        raise ReviewerError(f"submission visibility {value!r} is not recognized")
    if value == "confidential":
        for field in ("existing_id", "registry_correction", "preflight_repair"):
            if state.get(field):
                raise ReviewerError(
                    f"a confidential submission cannot carry {field}; confidential mode "
                    "is only for new results"
                )
    return value


def is_confidential(state: dict[str, Any]) -> bool:
    return visibility(state) == "confidential"


def run_origin(state: dict[str, Any]) -> RunOrigin:
    """The origin the State record requires, never the one a run claims."""
    if not is_confidential(state):
        return PUBLIC_ORIGIN
    recorded = (state.get("confidential") or {}).get("run_repository", RUNS_REPO)
    if recorded != RUNS_REPO:
        raise ReviewerError("confidential submission names an unrecognized run repository")
    return CONFIDENTIAL_ORIGIN


def origin_for_url(url: Any) -> RunOrigin:
    """The origin a run URL names. Callers bind it to the State record separately."""
    if isinstance(url, str):
        for origin in ORIGINS:
            if re.fullmatch(origin.run_url_pattern(), url):
                return origin
    raise ReviewerError("workflow URL names no recognized Palomar run repository")


def referenced_pipeline_commit(document: dict[str, Any], caller: str) -> str:
    """The one PalomarSubmission commit a caller run says it called."""
    called = CALLED_WORKFLOW[caller]
    references = document.get("referenced_workflows")
    if not isinstance(references, list) or len(references) != 1:
        raise ReviewerError("confidential run must reference exactly one called workflow")
    reference = references[0]
    sha = reference.get("sha") if isinstance(reference, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReviewerError("confidential run references no full PalomarSubmission commit")
    expected = f"{SUBMISSION_REPO}/.github/workflows/{called}@{sha}"
    if reference.get("path") != expected:
        raise ReviewerError(
            f"confidential run references {reference.get('path')!r}, not {expected!r}"
        )
    return sha


def validate_caller_file(text: str, caller: str, pipeline_commit: str, sums: dict[str, str]) -> str:
    """Refuse a caller file that is not exactly the canonical template at this pin.

    Returns the template digest it matched.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", pipeline_commit):
        raise ReviewerError("confidential caller pin is not a full commit")
    if PIPELINE_PLACEHOLDER in text:
        raise ReviewerError("confidential caller file carries an unsubstituted pin")
    normalized = text.replace(pipeline_commit, PIPELINE_PLACEHOLDER)
    if normalized.count(PIPELINE_PLACEHOLDER) != 2:
        raise ReviewerError("confidential caller must use its pin exactly in uses and pipeline_commit")
    digest = template_sha256(normalized)
    if digest != sums.get(caller):
        raise ReviewerError(
            f"confidential caller {caller} at the run's commit is not the reviewed template"
        )
    return digest


@dataclass(frozen=True)
class CallerRun:
    pipeline_commit: str
    template_sha256: str


def validate_caller_run(
    document: dict[str, Any],
    caller: str,
    read_caller_file: Callable[[str, str], str],
    read_template_sums: Callable[[str], dict[str, str]],
    environment: dict[str, str],
) -> CallerRun:
    """Check one confidential run document and return what it ran.

    ``read_caller_file(path, ref)`` returns the caller repository's file at a
    commit; ``read_template_sums(P)`` the canonical SHA256SUMS at P. Path,
    allowlist, template and called-workflow reference are all checked; the
    caller still has to confirm P is on PalomarSubmission's trusted history.
    """
    path = f".github/workflows/{caller}"
    if document.get("path") != path:
        raise ReviewerError(f"confidential run has path {document.get('path')!r}, not {path!r}")
    head_sha = document.get("head_sha")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ReviewerError("confidential run has no full caller commit")
    pipeline_commit = referenced_pipeline_commit(document, caller)
    require_allowed_pipeline(pipeline_commit, environment)
    digest = validate_caller_file(
        read_caller_file(path, head_sha), caller, pipeline_commit, read_template_sums(pipeline_commit)
    )
    return CallerRun(pipeline_commit, digest)
