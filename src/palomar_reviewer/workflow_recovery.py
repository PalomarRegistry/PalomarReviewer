"""Trusted workflow metadata diagnostics; never classify by candidate log text."""

from __future__ import annotations

from typing import Any

from .errors import ReviewerError

PROFILES = {"palomar-standard-v1", "palomar-namespace-16x32-v1"}
SETUP_STEPS = {
    "Checkout submission pipeline",
    "Set up Python",
    "Set up Ruby",
    "Set up Go",
    "Resolve approved execution profile",
    "Validate pinned pipeline revision",
    "Finalize interrupted mechanical report",
    "Fail the run when verification did not pass",
    "Install verifier dependencies",
    "Parse and fetch immutable source",
    "Free disk space",
    "Check palomar-standard-v1 runner capacity",
    "Install pinned elan",
    "Build pinned landrun",
    "Build pinned Comparator",
    "Build pinned NanoDa kernel",
    "Build toolchain-matched lean4export",
    "Upload bounded mechanical report",
    "Retry bounded mechanical report upload 2",
    "Retry bounded mechanical report upload 3",
}


def validate_execution_binding(report: dict, state: dict) -> None:
    execution = state.get("execution") or {}
    profile = execution.get("profile", "palomar-standard-v1")
    if profile not in PROFILES or report.get("execution_profile", "palomar-standard-v1") != profile:
        raise ReviewerError("mechanical report execution profile does not match admitted State")
    evidence = report.get("verification_profile")
    if evidence is not None and (not isinstance(evidence, dict) or evidence.get("id") != profile):
        raise ReviewerError("mechanical report resource evidence names a different execution profile")
    if profile != "palomar-standard-v1" and evidence is None:
        raise ReviewerError("nonstandard execution requires resource profile evidence")
    if report.get("execution_attempt") != execution.get("attempt"):
        raise ReviewerError("mechanical report belongs to another execution attempt")


def metadata_diagnostic(document: Any, run: dict) -> dict:
    """Accept only complete metadata for the exact recorded GitHub run attempt."""
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if (
        not isinstance(jobs, list)
        or not jobs
        or len(jobs) > 100
        or type(document.get("total_count")) is not int
        or document["total_count"] != len(jobs)
    ):
        raise ReviewerError("incomplete workflow job metadata")
    failed_steps = []
    profile_failed = False
    for job in jobs:
        if (
            not isinstance(job, dict)
            or type(job.get("run_id")) is not int
            or type(job.get("run_attempt")) is not int
            or job.get("run_id") != run["databaseId"]
            or job.get("run_attempt") != run["attempt"]
            or job.get("status") != "completed"
        ):
            raise ReviewerError("workflow jobs do not bind the recorded run attempt")
        if job.get("name") == "profile" and job.get("conclusion") == "failure":
            profile_failed = True
        for step in job.get("steps", []):
            if step.get("conclusion") in {"failure", "timed_out", "cancelled"}:
                failed_steps.append(step.get("name"))
    setup = next((name for name in failed_steps if name in SETUP_STEPS), None)
    if run["conclusion"] == "timed_out":
        code, owner = "provider.workflow_timeout", "provider"
        summary = "GitHub reports that verification exceeded its workflow time limit."
    elif setup or profile_failed:
        code, owner = "palomar.workflow_step_failed", "palomar"
        summary = f"The trusted workflow did not complete step: {setup or 'execution profile resolution'}."
    else:
        code, owner = "provider.workflow_interrupted", "provider"
        summary = "The workflow ended without a usable terminal mechanical report."
    return {
        "code": code,
        "owner": owner,
        "stage": "workflow",
        "summary": summary,
        "explanation": summary
        + " This diagnosis uses GitHub job metadata; it does not establish an OOM or a proof failure.",
        "next_action": (
            "Palomar should inspect the recorded workflow and, if appropriate, retry the admitted submission."
        ),
        "retryable": True,
        "repairable": False,
    }
