"""Disposition selection from already-validated mechanical outcomes."""

from __future__ import annotations

import json
from typing import Any

from . import operator_alerts


def configuration_key(state: dict) -> tuple[str, str, str, str, bool]:
    paths = state.get("requested_paths") or {}
    return (
        state["repository"].lower(),
        paths.get("project_path") or "",
        paths.get("comparator_config_path") or "comparator.json",
        paths.get("formalization_metadata_path") or "formalization.yaml",
        state.get("test_submission") is True,
    )


def desired_alerts(state: dict, outcomes: list[dict], *, at: str, is_successor=None) -> dict | None:
    alerts = operator_alerts.upgrade_alerts(state)
    if not alerts:
        return None
    for item in alerts["items"]:
        # A maintainer's audited correction must not be replaced by a later
        # automated outcome or trigger an unapproved Zulip disposition edit.
        if (item.get("disposition") or {}).get("kind") == "reclassified":
            continue
        disposition: dict[str, Any] | None = None
        if state.get("status") == "withdrawn":
            disposition = {"kind": "withdrawn", "at": at}
        else:
            candidates = [
                value
                for value in outcomes
                if value["configuration"] == configuration_key(state)
                and value["verified_at"] >= item["queued_at"]
                and (
                    value["commit"] == state["commit"]
                    or (is_successor is not None and is_successor(state, value))
                )
                and value["run_url"] != item["origin"]["failure"]["run"].get("url")
            ]
            # A verified original commit is stronger evidence than a successor.
            candidates.sort(
                key=lambda value: (value["commit"] == state["commit"], value["verified_at"]), reverse=True
            )
            if candidates:
                outcome = candidates[0]
                disposition = {
                    "kind": "recovered" if outcome["commit"] == state["commit"] else "superseded",
                    "at": at,
                    "evidence": {key: outcome[key] for key in ("submission_id", "commit", "run_url")},
                }
        if disposition:
            previous = item.get("disposition")
            if previous and {key: value for key, value in previous.items() if key != "at"} == {
                key: value for key, value in disposition.items() if key != "at"
            }:
                disposition = previous
            item["disposition"] = disposition
        # Never revoke an established disposition just because a retained
        # workflow artifact has expired since its evidence was validated.
    return alerts


def outcome(state: dict, mechanical: dict) -> dict:
    """Call only after the normal successful mechanical-report trust validator."""
    if not operator_alerts.TIMESTAMP_RE.fullmatch(str(mechanical.get("checked_at", ""))):
        from .errors import ReviewerError

        raise ReviewerError("outcome requires a canonical UTC verification timestamp")
    return {
        "configuration": configuration_key(state),
        "submission_id": state["id"],
        "commit": state["commit"],
        "run_url": mechanical["workflow_url"],
        "verified_at": mechanical["checked_at"],
    }


def recovery_index(states: list[dict]) -> dict:
    groups: dict[str, list[str]] = {}
    for state in states:
        if state.get("operator_alerts"):
            key = operator_alerts.content_hash(json.dumps(configuration_key(state)))
            groups.setdefault(key, []).append(state["id"])
    return {"schema_version": 1, "groups": {key: sorted(ids) for key, ids in sorted(groups.items())}}


def validated_cached_outcomes(index: dict | None) -> dict:
    """Reject corrupt derived evidence rather than authorizing a public edit."""
    from .errors import ReviewerError

    if index is None:
        return {}
    if (
        set(index) - {"_blob_sha"} != {"schema_version", "groups", "outcomes"}
        or type(index["schema_version"]) is not int
        or index["schema_version"] != 1
    ):
        raise ReviewerError("unsupported operator alert index")
    outcomes = index["outcomes"]
    if not isinstance(outcomes, dict):
        raise ReviewerError("operator alert outcomes must be an object")
    for identifier, value in outcomes.items():
        required = {
            "configuration",
            "submission_id",
            "commit",
            "run_url",
            "verified_at",
            "execution_attempt",
            "workflow_commit",
            "report_sha256",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ReviewerError("invalid cached operator alert outcome")
        if identifier != value["submission_id"] or not operator_alerts.SUBMISSION_ID_RE.fullmatch(identifier):
            raise ReviewerError("invalid cached outcome submission")
        for key, pattern in (
            ("commit", operator_alerts.COMMIT_RE),
            ("workflow_commit", operator_alerts.COMMIT_RE),
            ("report_sha256", operator_alerts.SHA256_RE),
            ("run_url", operator_alerts.RUN_URL_RE),
            ("verified_at", operator_alerts.TIMESTAMP_RE),
        ):
            if not isinstance(value[key], str) or not pattern.fullmatch(value[key]):
                raise ReviewerError(f"invalid cached outcome {key}")
        configuration = value["configuration"]
        if (
            not isinstance(configuration, list)
            or len(configuration) != 5
            or not all(isinstance(part, str) for part in configuration[:4])
            or type(configuration[4]) is not bool
            or not operator_alerts.REPOSITORY_RE.fullmatch(configuration[0])
        ):
            raise ReviewerError("invalid cached outcome configuration")
        attempt = value["execution_attempt"]
        if attempt is not None and (
            not isinstance(attempt, str) or not operator_alerts.re.fullmatch(r"[0-9a-f]{32}", attempt)
        ):
            raise ReviewerError("invalid cached outcome execution attempt")
    return outcomes
