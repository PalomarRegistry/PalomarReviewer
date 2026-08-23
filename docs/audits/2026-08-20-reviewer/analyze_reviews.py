#!/usr/bin/env python3
"""Build a compact index and descriptive statistics for retrieved reviews."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
STATE = ROOT / "state-main" / "submissions"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_states() -> dict[str, dict]:
    result = {}
    for path in STATE.glob("*/state.json"):
        try:
            result[path.parent.name] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return result


def packet_roots() -> list[tuple[Path, dict]]:
    packets = []
    for manifest_path in RUNS.glob("*/extraction.json"):
        manifest = json.loads(manifest_path.read_text())
        run_dir = manifest_path.parent
        for child in run_dir.iterdir():
            if child.is_dir() and len(child.name) == 12:
                packets.append((child, manifest))
    return packets


def main() -> None:
    states = current_states()
    reviews_by_hash: dict[str, dict] = {}
    raw_attempts_by_hash: dict[str, dict] = {}
    packet_copies = 0
    packets_with_raw = 0

    for packet, manifest in packet_roots():
        packet_copies += 1
        raw_files = sorted((packet / "raw").glob("*.events.jsonl"))
        pass_files = sorted((packet / "passes").glob("*.json"))
        prompt_files = sorted((packet / "prompts").glob("*.md"))
        if raw_files:
            packets_with_raw += 1
            signature_input = "".join(
                f"{path.relative_to(packet)}:{digest(path)}\n"
                for path in raw_files + pass_files + prompt_files
            ).encode()
            signature = hashlib.sha256(signature_input).hexdigest()
            attempt = raw_attempts_by_hash.setdefault(
                signature,
                {
                    "signature": signature,
                    "submission_id": packet.name,
                    "raw_passes": [path.stem.removesuffix(".events") for path in raw_files],
                    "structured_passes": [path.stem for path in pass_files],
                    "complete": (packet / "review.json").exists(),
                    "first_artifact_created_at": manifest["artifact_created_at"],
                    "copies": [],
                },
            )
            attempt["copies"].append(
                {
                    "workflow_run_id": manifest["workflow_run_id"],
                    "artifact_id": manifest["artifact_id"],
                    "path": str(packet.relative_to(ROOT)),
                }
            )

        review_path = packet / "review.json"
        if not review_path.exists():
            continue
        review_hash = digest(review_path)
        review = json.loads(review_path.read_text())
        # Registration packets may contain the separately projected public
        # review, which intentionally omits private check evidence and may also
        # omit scores. It is a copy of the result, not another model review.
        checks = review.get("passes") or review.get("checks") or []
        if not checks or not review.get("scores"):
            continue
        indexed = reviews_by_hash.setdefault(
            review_hash,
            {
                "sha256": review_hash,
                "submission_id": review.get("submission_id", packet.name),
                "source": review.get("source"),
                "reviewed_at": review.get("reviewed_at"),
                "schema_version": review.get("schema_version"),
                "policy_commit": review.get("policy_commit"),
                "reviewer_models": review.get("reviewer_models", []),
                "decision": review.get("decision") or review.get("outcome"),
                "scores": review.get("scores", {}),
                "summary": review.get("summary"),
                "warnings": review.get("warnings", []),
                "requested_changes": review.get("requested_changes", []),
                "passes": [],
                "copies": [],
            },
        )
        if not indexed["passes"]:
            for review_pass in checks:
                indexed["passes"].append(
                    {
                        "step": review_pass.get("step"),
                        "verdict": review_pass.get("verdict") or review_pass.get("outcome"),
                        "scores": {
                            key: value
                            for key, value in review_pass.get("scores", {}).items()
                            if value is not None
                        },
                        "trust_level": review_pass.get("trust_level"),
                        "findings": review_pass.get("findings", []),
                        "internal_notes": review_pass.get("internal_notes", []),
                        "sources_checked": review_pass.get("sources_checked", []),
                        "declarations_checked": review_pass.get("declarations_checked", []),
                        "codes_checked": review_pass.get("codes_checked", []),
                    }
                )
        indexed["copies"].append(
            {
                "workflow_run_id": manifest["workflow_run_id"],
                "artifact_id": manifest["artifact_id"],
                "path": str(review_path.relative_to(ROOT)),
            }
        )

    reviews = sorted(
        reviews_by_hash.values(), key=lambda item: (item["reviewed_at"] or "", item["submission_id"])
    )
    attempts = sorted(
        raw_attempts_by_hash.values(),
        key=lambda item: (item["first_artifact_created_at"], item["submission_id"]),
    )
    for review in reviews:
        state = states.get(review["submission_id"], {})
        review["current_status"] = state.get("status")
        review["current_review_attempts"] = state.get("review_attempts")
        spend_paths = [
            ROOT / copy["path"] for copy in review["copies"]
        ]
        spend = None
        for review_copy in spend_paths:
            candidate = review_copy.with_name("spend.json")
            if candidate.exists():
                spend = json.loads(candidate.read_text())
                break
        review["spend"] = spend

    decisions = Counter(review["decision"] or "missing" for review in reviews)
    current_statuses = Counter(review["current_status"] or "missing" for review in reviews)
    score_values: dict[str, list[float]] = defaultdict(list)
    for review in reviews:
        for key, value in review["scores"].items():
            if isinstance(value, (int, float)):
                score_values[key].append(value)
    score_stats = {
        key: {
            "count": len(values),
            "mean": mean(values),
            "median": median(values),
            "min": min(values),
            "max": max(values),
            "distribution": dict(sorted(Counter(values).items())),
        }
        for key, values in sorted(score_values.items())
    }
    all_findings = [
        finding
        for review in reviews
        for review_pass in review["passes"]
        for finding in review_pass["findings"]
    ]
    summary = {
        "artifact_count": len(list(RUNS.glob("*/extraction.json"))),
        "packet_copies": packet_copies,
        "packet_copies_with_raw_events": packets_with_raw,
        "unique_raw_attempts": len(attempts),
        "unique_complete_reviews": len(reviews),
        "unique_incomplete_raw_attempts": sum(not attempt["complete"] for attempt in attempts),
        "decisions": dict(decisions),
        "current_statuses": dict(current_statuses),
        "reviews_with_warnings": sum(bool(review["warnings"]) for review in reviews),
        "total_public_warnings": sum(len(review["warnings"]) for review in reviews),
        "total_pass_findings": len(all_findings),
        "finding_severities": dict(
            Counter(item.get("severity") or "missing" for item in all_findings)
        ),
        "score_statistics": score_stats,
    }
    (ROOT / "review-index.json").write_text(
        json.dumps({"summary": summary, "reviews": reviews, "attempts": attempts}, indent=2, sort_keys=True)
        + "\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
