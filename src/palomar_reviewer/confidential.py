"""Confidential submissions: where their runs happen and how those runs are trusted.

A confidential submission (``state.visibility == "confidential"``) is verified
and rendered in the private caller repository
``PalomarRegistry/PalomarConfidentialRuns`` instead of the public
PalomarSubmission. The caller holds two tiny workflows that call
PalomarSubmission's reusable workflows at one pinned full commit, so the
verifier code is still the public, reviewed code; only the run, its logs and
its artifacts are private.

The run's repository is therefore not evidence by itself. A confidential run
is trusted only if all of these hold (contract v3.1, component C3):

1. its workflow path is the caller's ``verify.yml`` (or ``render.yml``);
2. the run's ``referenced_workflows`` names exactly one called
   PalomarSubmission workflow, at one full commit P;
3. the report says it executed P, and P is on PalomarSubmission's trusted
   history (checked by the caller).

The caller file itself is not compared with a template. GitHub resolves the
reusable workflow at P, so the verifier is PalomarSubmission's reviewed code
whatever the caller passes it, and the caller repository's members are the
Technical Maintainers, who can read the source anyway.

Visibility is chosen at intake and never comes from the run itself: the
submission's State record says which origin its runs must have, and a run from
the other origin is refused.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

from .errors import ReviewerError

SUBMISSION_REPO = "PalomarRegistry/PalomarSubmission"
RUNS_REPO = "PalomarRegistry/PalomarConfidentialRuns"
VISIBILITIES = ("public", "confidential")


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
    return CONFIDENTIAL_ORIGIN if is_confidential(state) else PUBLIC_ORIGIN


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


@dataclass(frozen=True)
class CallerRun:
    pipeline_commit: str


def validate_caller_run(document: dict[str, Any], caller: str) -> CallerRun:
    """Check one confidential run document and return the commit it called.

    The caller still has to confirm P is on PalomarSubmission's trusted
    history, and that the report names P.
    """
    path = f".github/workflows/{caller}"
    if document.get("path") != path:
        raise ReviewerError(f"confidential run has path {document.get('path')!r}, not {path!r}")
    return CallerRun(referenced_pipeline_commit(document, caller))


_MASKED: set[str] = set()


def withhold_from_logs(state: Any, *, emit: Any = None) -> None:
    """Mask a confidential record's repository and commit in the Actions log.

    State's Actions logs are shared with public passes and kept, so these must
    never appear there, whichever error message, progress line or subprocess
    happens to quote one. `::add-mask::` makes the runner replace them in
    every later line of the job, including child processes' output, and the
    command itself is not shown.
    """
    if not isinstance(state, dict) or state.get("visibility") != "confidential":
        return
    values: set[str] = set()
    repository, commit = state.get("repository"), state.get("commit")
    if isinstance(repository, str) and "/" in repository:
        values.update({repository, repository.replace("/", "-")})
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
        values.update({commit, commit[:12], commit[:7]})
    for value in sorted(values - _MASKED):
        (emit or _print_mask)(value)
        _MASKED.add(value)


def _print_mask(value: str) -> None:
    sys.stdout.write(f"::add-mask::{value}\n")
    sys.stdout.flush()
