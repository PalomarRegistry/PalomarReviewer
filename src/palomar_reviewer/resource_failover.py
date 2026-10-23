"""Attest resource failures only after the failed workflow has been authenticated."""

from __future__ import annotations

from typing import Any

NAMESPACE_PROFILES = {
    "palomar-namespace-16x32-v1": "nscloud-ubuntu-24.04-amd64-16x32-with-features",
    "palomar-namespace-32x64-v1": "nscloud-ubuntu-24.04-amd64-32x64-with-features",
}


def report_evidence(report: dict[str, Any], run_id: int) -> dict[str, Any] | None:
    resource = report.get("resource_failure")
    diagnostics = report.get("diagnostics", [])
    profile = report.get("execution_profile")
    if (
        report.get("status") != "error" or report.get("phase") != "verification"
        or not isinstance(resource, dict) or set(resource) != {"kind"}
        or resource.get("kind") not in ("oom", "timeout")
        or profile not in NAMESPACE_PROFILES or not diagnostics
        or any(item.get("owner") != "provider" or item.get("code") != "provider.resource_exhausted"
               for item in diagnostics)
    ):
        return None
    return {"kind": resource["kind"], "profile": profile, "run_id": run_id}


def timeout_evidence(jobs: dict, run: dict, state: dict) -> dict[str, Any] | None:
    """Use already-validated GitHub metadata when a hard timeout lost the artifact."""
    if run.get("conclusion") != "timed_out":
        return None
    matches = [job for job in jobs["jobs"] if job.get("name") == "verify"
               and job.get("conclusion") == "timed_out"]
    if len(matches) != 1:
        return None
    labels = matches[0].get("labels", [])
    profiles = [profile for profile, label in NAMESPACE_PROFILES.items() if label in labels]
    if len(profiles) != 1 or "namespace-features:container.privileged=true" not in labels:
        return None
    profile = profiles[0]
    expected = (state.get("execution") or {}).get("profile", profile)
    if profile != expected:
        return None
    return {"kind": "timeout", "profile": profile, "run_id": run["databaseId"]}
