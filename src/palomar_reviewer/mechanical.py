"""Current mechanical-report schema, trust validation, and confined path lookup.

The CLI remains the composition root: it selects and downloads the recorded
workflow artifact and verifies the workflow commit's ancestry. This module owns
the data contract checked before source checkout or review orchestration. Its
path helpers inspect metadata only inside a caller-supplied source checkout.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jsonschema

from .errors import ReviewerError

# This producer identity is part of both the report schema and the CLI's trusted
# workflow queries. The current database schema also pins it in registered
# verification provenance, so all three consumers share this canonical value.
SUBMISSION_REPO = "PalomarRegistry/PalomarSubmission"

MECHANICAL_REPORT_SCHEMA = {
    "type": "object",
    "required": [
        "schema_version",
        "status",
        "stage",
        "submission",
        "source",
        "challenge",
        "solution",
        "lean_toolchain",
        "comparator",
        "comparator_commit",
        "lean4export_commit",
        "landrun_commit",
        "nanoda_commit",
        "checked_at",
        "workflow_url",
        "project_dependencies",
        "provenance",
        "license",
        "formalization",
        "lakefile",
        "lean_toolchain_path",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "status": {"const": "pass"},
        "stage": {"const": "complete"},
        # Closed, and deliberately narrow: the whole report is archived in the
        # public evidence bundle, so anything this block accepts becomes public.
        # A submitter identity must not be able to ride in here.
        "submission": {
            "type": "object",
            "additionalProperties": False,
            "required": ["submission_id", "authorization"],
            "properties": {
                "submission_id": {"type": "string", "pattern": "^[0-9a-z]{12}$"},
                "authorization": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["relationship"],
                    "properties": {
                        "relationship": {
                            "enum": ["maintainer", "approved", "technical-test"]
                        },
                        "evidence": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                },
                # What the submitter asked the workflow to verify. Kept so the
                # archived report says what it was asked for, not only what it
                # found; the reviewer binds these to the private record.
                "requested_paths": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "project_path": {"type": "string", "maxLength": 400},
                        "comparator_config_path": {"type": "string", "maxLength": 400},
                        "formalization_metadata_path": {"type": "string", "maxLength": 400},
                    },
                },
            },
        },
        "provenance": {
            "type": "object",
            "required": [
                "result_origin",
                "repository_role",
                "responsible_maintainers",
                "mathematical_sources",
                "related_formalizations",
            ],
            "properties": {
                "result_origin": {"enum": ["original", "source-based", "unspecified"]},
                "repository_role": {
                    "enum": ["substantive-development", "thin-wrapper", "unspecified"]
                },
                "responsible_maintainers": {"type": "array"},
                "mathematical_sources": {"type": "array"},
                "related_formalizations": {"type": "array"},
                "substantive_formalization": {"type": "object"},
                "declared": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "result_origin",
                        "repository_role",
                        "responsible_maintainers",
                    ],
                    "properties": {
                        "result_origin": {"type": "boolean"},
                        "repository_role": {"type": "boolean"},
                        "responsible_maintainers": {"type": "boolean"},
                    },
                },
            },
        },
        "source": {
            "type": "object",
            "required": ["repository", "repository_url", "commit", "tree_url"],
            "properties": {
                "repository": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
                "repository_url": {"type": "string", "pattern": r"^https://github\.com/"},
                "commit": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
                "tree_url": {"type": "string", "pattern": r"^https://github\.com/.+/tree/[0-9a-f]{40}(?:/.+)?$"},
                "project_path": {"type": "string", "minLength": 1},
            },
        },
        "license": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "path",
                "sha256",
                "declared_identifier",
                "detected_identifier",
            ],
            "properties": {
                "path": {
                    "type": "string",
                    "pattern": r"(?i)^(?:licen[cs]e|copying|unlicense|ofl)(?:\.(?:md|markdown|txt))?$",
                },
                "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "declared_identifier": {"type": "string", "minLength": 1},
                "detected_identifier": {"type": "string", "minLength": 1},
            },
        },
        "challenge": {
            "type": "object",
            "required": [
                "path",
                "sha256",
                "lines",
                "bytes",
                "direct_imports",
                "dependencies",
                "trust_level",
            ],
            "properties": {
                "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "path": {"type": "string", "minLength": 1},
                "module": {"type": "string", "minLength": 1},
                "lines": {"type": "integer", "minimum": 1},
                "bytes": {"type": "integer", "minimum": 1},
                "direct_imports": {"type": "array", "items": {"type": "string"}},
                "dependencies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["repository", "provenance"],
                        "properties": {
                            "repository": {
                                "type": "string",
                                "pattern": r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
                            },
                            "provenance": {"const": "allowlisted"},
                        },
                    },
                },
                "trust_level": {"enum": ["high", "qualified"]},
            },
        },
        "solution": {
            "type": "object",
            "required": ["path", "sha256"],
            "properties": {
                "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "path": {"type": "string", "minLength": 1},
                "module": {"type": "string", "minLength": 1},
            },
        },
        "lean_toolchain": {"type": "string", "pattern": r"^leanprover/lean4:"},
        "comparator": {
            "type": "object",
            "required": ["path", "theorem_names", "definition_names", "permitted_axioms"],
            "properties": {
                "theorem_names": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "definition_names": {"type": "array", "items": {"type": "string"}},
                "permitted_axioms": {"type": "array", "items": {"type": "string"}},
                "path": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "challenge_module": {"type": "string", "minLength": 1},
                "solution_module": {"type": "string", "minLength": 1},
            },
        },
        "comparator_commit": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
        "lean4export_commit": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
        "landrun_commit": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
        "nanoda_commit": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
        "checked_at": {"type": "string", "format": "date-time"},
        "workflow_url": {
            "type": "string",
            "pattern": rf"^https://github\.com/{re.escape(SUBMISSION_REPO)}/actions/runs/[1-9][0-9]*$",
        },
        "existing_id": {
            "type": ["string", "null"],
            "pattern": r"^PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}$",
        },
        "project_dependencies": {
            "type": "array",
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "repository", "url", "revision"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "repository": {"type": "string", "minLength": 1},
                            "url": {"type": "string", "minLength": 1},
                            "revision": {"type": "string", "minLength": 1},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "path"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "path": {"type": "string", "minLength": 1},
                        },
                    },
                ]
            },
        },
        "lean_toolchain_path": {"type": "string", "minLength": 1},
        "lakefile": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "sha256", "format"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "format": {"enum": ["toml", "lean"]},
            },
        },
        "formalization": {
            "type": "object",
            "required": ["path", "sha256"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
            },
        },
    },
}


def validate_report_schema(report: dict[str, Any]) -> None:
    """Reject a malformed current report before any path is dereferenced."""
    try:
        jsonschema.validate(
            report,
            MECHANICAL_REPORT_SCHEMA,
            format_checker=jsonschema.FormatChecker(),
        )
    except jsonschema.ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path)
        where = f" at {location}" if location else ""
        raise ReviewerError(
            f"mechanical report violates the current artifact contract{where}: {error.message}"
        ) from error


def safe_repository_path(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ReviewerError(f"{field} must be a repository-relative POSIX path")
    segments = value.split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "?" in value
        or "#" in value
        or any(not segment or segment in {".", ".."} for segment in segments)
        or ":" in segments[0]
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReviewerError(f"{field} must be a safe repository-relative POSIX path")
    return value


def source_tree_url(source: dict[str, Any]) -> str:
    base = f"{source['repository_url']}/tree/{source['commit']}"
    project_path = source.get("project_path")
    if not project_path:
        return base
    safe_repository_path(project_path, "source.project_path")
    encoded = "/".join(quote(segment, safe="") for segment in project_path.split("/"))
    return f"{base}/{encoded}"


def source_path(source: Path, value: str, field: str) -> Path:
    relative = safe_repository_path(value, field)
    candidate = source
    for segment in relative.split("/"):
        candidate /= segment
        if candidate.is_symlink():
            raise ReviewerError(f"{field} contains a symlinked component")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(source.resolve()) or not resolved.is_file():
        raise ReviewerError(f"{field} is missing or escapes the source checkout")
    return resolved


def relative_path(mechanical: dict[str, Any], name: str) -> str:
    mapping = {
        "challenge_source": ("challenge", "path"),
        "solution_source": ("solution", "path"),
        "comparator_config": ("comparator", "path"),
        "formalization_metadata": ("formalization", "path"),
        "lakefile": ("lakefile", "path"),
    }
    value = (
        mechanical["lean_toolchain_path"]
        if name == "lean_toolchain"
        else mechanical[mapping[name][0]][mapping[name][1]]
    )
    return safe_repository_path(value, name)


def project_readme_relative(mechanical: dict[str, Any], source: Path) -> str:
    project_path = mechanical["source"].get("project_path")
    if project_path:
        candidate = f"{safe_repository_path(project_path, 'source.project_path')}/README.md"
        path = source / candidate
        if path.is_file() and not path.is_symlink():
            return candidate
    return "README.md"


def validate_report_contract(
    report: dict[str, Any], state: dict[str, Any], run_data: dict[str, Any]
) -> str:
    """Return the workflow head the caller must still check against main's lineage."""
    # Said plainly before the schema says it in five hundred lines of diff. A
    # submission whose verification failed should never reach review at all,
    # so reaching here means something upstream let it through.
    if report.get("status") != "pass":
        problems = "; ".join(str(e) for e in report.get("errors", [])) or "no reason recorded"
        raise ReviewerError(
            f"mechanical verification did not pass ({report.get('status')}): {problems}"
        )
    validate_report_schema(report)
    required_paths = (
        (report.get("challenge", {}), "path", "challenge.path"),
        (report.get("solution", {}), "path", "solution.path"),
        (report.get("comparator", {}), "path", "comparator.path"),
        (report.get("formalization", {}), "path", "formalization.path"),
        (report.get("lakefile", {}), "path", "lakefile.path"),
        (report, "lean_toolchain_path", "lean_toolchain_path"),
    )
    for container, key, field in required_paths:
        safe_repository_path(container.get(key), field)
    for field in ("module",):
        if not isinstance(report["challenge"].get(field), str):
            raise ReviewerError("mechanical report has no configured Challenge module")
        if not isinstance(report["solution"].get(field), str):
            raise ReviewerError("mechanical report has no configured Solution module")
    if report["challenge"]["module"] != report["comparator"].get("challenge_module"):
        raise ReviewerError("mechanical report disagrees on the configured Challenge module")
    if report["solution"]["module"] != report["comparator"].get("solution_module"):
        raise ReviewerError("mechanical report disagrees on the configured Solution module")
    project_path = report["source"].get("project_path")
    prefix = f"{project_path}/" if project_path else ""
    for container, key, field in (
        (report["challenge"], "path", "challenge.path"),
        (report["solution"], "path", "solution.path"),
        (report["comparator"], "path", "comparator.path"),
        (report["lakefile"], "path", "lakefile.path"),
    ):
        if not container[key].startswith(prefix):
            raise ReviewerError(f"mechanical report {field} is outside the selected project")
    if report["lakefile"]["path"] not in {
        f"{prefix}lakefile.toml",
        f"{prefix}lakefile.lean",
    }:
        raise ReviewerError("mechanical report Lakefile is not the selected project's Lakefile")
    expected_format = "lean" if report["lakefile"]["path"].endswith(".lean") else "toml"
    if report["lakefile"]["format"] != expected_format:
        raise ReviewerError("mechanical report Lakefile format disagrees with its path")
    if report["lean_toolchain_path"] not in {
        f"{prefix}lean-toolchain",
        "lean-toolchain",
    }:
        raise ReviewerError("mechanical report lean-toolchain is outside its accepted locations")
    seen_dependency_names: set[str] = set()
    for dependency in report["project_dependencies"]:
        if dependency["name"] in seen_dependency_names:
            raise ReviewerError("mechanical report repeats a project dependency name")
        seen_dependency_names.add(dependency["name"])
        if "path" in dependency and dependency["path"] != ".":
            safe_repository_path(dependency["path"], "project dependency path")
    if report["submission"]["submission_id"] != state["id"]:
        raise ReviewerError("mechanical report names a different submission")
    if report["workflow_url"] != run_data.get("url"):
        raise ReviewerError("mechanical report does not name its trusted workflow run")
    source = report["source"]
    if source["repository_url"] != f"https://github.com/{source['repository']}":
        raise ReviewerError("mechanical report source repository URL is inconsistent")
    if source["tree_url"] != source_tree_url(source):
        raise ReviewerError("mechanical report source tree URL is inconsistent")
    if (
        source["repository"].lower() != str(state["repository"]).lower()
        or source["commit"] != state["commit"]
    ):
        raise ReviewerError("mechanical report source does not match the submission")
    # Every path the submitter chose, bound to the run that acted on it.
    #
    # The submission id is public in the run name, so a run can be dispatched by
    # somebody else for the same repository, commit and project while naming a
    # different comparator configuration or metadata file, and findVerificationRun
    # can pin it. Checking only the project directory would accept that run: the
    # report would agree about everything compared and differ about what was
    # actually read. So the requested paths are compared as a whole, and then the
    # paths the run resolved are compared against what those requests mean.
    requested = state.get("requested_paths") or {}
    reported_request = (report["submission"].get("requested_paths") or {})
    path_keys = ("project_path", "comparator_config_path", "formalization_metadata_path")
    for key in path_keys:
        wanted = requested.get(key, "") or ""
        if wanted:
            safe_repository_path(wanted, f"requested {key}")
        if (reported_request.get(key, "") or "") != wanted:
            raise ReviewerError(f"mechanical report was asked for a different {key}")

    requested_project = requested.get("project_path", "") or ""
    if (source.get("project_path") or "") != requested_project:
        raise ReviewerError("mechanical report project path does not match the submission")
    prefix_for = f"{requested_project}/" if requested_project else ""
    for key, container, default in (
        ("comparator_config_path", report["comparator"], "comparator.json"),
        ("formalization_metadata_path", report["formalization"], "formalization.yaml"),
    ):
        expected = (requested.get(key, "") or "") or f"{prefix_for}{default}"
        if container.get("path") != expected:
            raise ReviewerError(
                f"mechanical report read a different {key} than the submission asked for"
            )
    head_sha = run_data.get("headSha")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ReviewerError("trusted verification run has no full workflow commit")
    # The submission server dispatches verification and nothing else does. A run
    # triggered any other way did not come from an intake the server recorded.
    if run_data.get("event") != "workflow_dispatch":
        raise ReviewerError(
            f"verification run was triggered by {run_data.get('event')!r}, not a dispatch"
        )

    return head_sha
