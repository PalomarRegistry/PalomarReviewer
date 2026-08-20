"""Read and extend the Database's segmented registration authority.

Registration has no whole-registry index. An immutable identity binding owns
one repository/project/configuration tuple, a submission binding answers
whether one intake already registered, a result projection owns bounded
version history, and a day projection owns the next serial for that date.
Ordinary work is therefore independent of the number of results in the
registry.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ReviewerError

SCHEMA_VERSION = 2
MAX_VERSIONS_PER_RESULT = 500
RESULTS_DIRECTORY = "registrations/results"
SUBMISSIONS_DIRECTORY = "registrations/submissions"
DAYS_DIRECTORY = "registrations/days"
IDENTITIES_DIRECTORY = "registrations/identities"
PALOMAR_ID_RE = re.compile(
    r"PALOMAR-(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})-(?P<serial>[0-9]{6})\Z"
)
SUBMISSION_ID_RE = re.compile(r"[0-9a-z]{12}\Z")
IDENTITY_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


class _InvalidJSON(ValueError):
    pass


@dataclass(frozen=True)
class ProjectionChange:
    """One exact registration projection and its expected Git transition."""

    path: str
    document: dict[str, Any]
    status: str


def result_path(identifier: str) -> str:
    return f"{RESULTS_DIRECTORY}/{identifier}.json"


def submission_path(submission_id: str) -> str:
    return f"{SUBMISSIONS_DIRECTORY}/{submission_id}.json"


def day_path(day: str) -> str:
    return f"{DAYS_DIRECTORY}/{day}.json"


def identity_digest(identity: dict[str, Any]) -> str:
    preimage = "\0".join(
        (
            str(identity["source_repository"]),
            str(identity["project_path"] or ""),
            str(identity["comparator_config_path"]),
        )
    ).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


def identity_path(identity: dict[str, Any]) -> str:
    return f"{IDENTITIES_DIRECTORY}/{identity_digest(identity)}.json"


def allocate_identifier(registered_on: str, last_serial: int) -> str:
    """Allocate the serial immediately after one day's bounded counter."""
    try:
        dt.date.fromisoformat(registered_on)
    except (TypeError, ValueError) as error:
        raise ReviewerError("registration has no valid allocation date") from error
    if type(last_serial) is not int or not 0 <= last_serial <= 999_999:
        raise ReviewerError("registration day counter is outside the identifier range")
    serial = last_serial + 1
    if serial > 999_999:
        raise ReviewerError(
            f"could not allocate a free permanent identifier: {registered_on} has used "
            "all 999,999 serials"
        )
    return f"PALOMAR-{registered_on}-{serial:06d}"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidJSON
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise _InvalidJSON


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _InvalidJSON
    return parsed


def _strict_json(encoded: bytes, label: str) -> dict[str, Any]:
    try:
        document = json.loads(
            encoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
            parse_float=_finite_float,
        )
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise ReviewerError(f"{label}: is not valid strict JSON") from error
    if not isinstance(document, dict):
        raise ReviewerError(f"{label}: must be a JSON object")
    return document


def _load_projection(
    database: Path,
    relative: str,
    *,
    git_env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Read one optional authority document from the exact checked-out commit.

    Registration projections are deliberately outside the sparse worktree.  A
    blobless checkout can still inspect the HEAD tree without fetching every
    historical projection, then fetch only the one blob named by that tree.
    If a projection is materialized (as in a developer checkout), it must be
    the unchanged, ordinary non-executable file represented by HEAD.
    """
    path = database / relative
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(database),
            "ls-files",
            "-t",
            "--stage",
            "-z",
            "--",
            relative,
        ],
        check=False,
        capture_output=True,
        env=git_env,
    )
    if listed.returncode != 0:
        raise ReviewerError(f"{relative}: Git index cannot be inspected")
    indexed = re.fullmatch(
        rb"([HS]) 100644 ([0-9a-f]+) 0\t" + re.escape(relative.encode()) + rb"\0",
        listed.stdout,
    )
    tree = subprocess.run(
        ["git", "-C", str(database), "ls-tree", "-z", "HEAD", "--", relative],
        check=False,
        capture_output=True,
        env=git_env,
    )
    if tree.returncode != 0:
        raise ReviewerError(f"{relative}: checked-out Git tree cannot be inspected")
    committed = re.fullmatch(
        rb"100644 blob ([0-9a-f]+)\t" + re.escape(relative.encode()) + rb"\0",
        tree.stdout,
    )
    if not listed.stdout and not tree.stdout:
        if path.is_symlink() or path.exists():
            raise ReviewerError(f"{relative}: is not authority from the checked-out commit")
        return None
    if indexed is None or committed is None or indexed.group(2) != committed.group(1):
        raise ReviewerError(
            f"{relative}: must be unchanged authority at exact Git mode 100644"
        )
    blob = subprocess.run(
        ["git", "-C", str(database), "cat-file", "blob", committed.group(1).decode()],
        check=False,
        capture_output=True,
        env=git_env,
    )
    if blob.returncode != 0:
        raise ReviewerError(f"{relative}: authority blob cannot be read")
    if path.is_symlink():
        raise ReviewerError(f"{relative}: must be an ordinary non-executable JSON file")
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        if indexed.group(1) != b"S":
            raise ReviewerError(f"{relative}: tracked authority file is missing") from None
        encoded = blob.stdout
    except OSError as error:
        raise ReviewerError(f"{relative}: cannot be inspected") from error
    else:
        if path.is_symlink() or not stat.S_ISREG(mode) or mode & 0o111:
            raise ReviewerError(
                f"{relative}: must be an ordinary non-executable JSON file"
            )
        try:
            encoded = path.read_bytes()
        except OSError as error:
            raise ReviewerError(f"{relative}: cannot be read") from error
        if encoded != blob.stdout:
            raise ReviewerError(f"{relative}: differs from the checked-out authority")
    return _strict_json(encoded, relative)


def materialize_changes(
    database: Path, changes: tuple[ProjectionChange, ...]
) -> None:
    """Write exact projections without following a planted sparse-path symlink."""
    prepared: list[tuple[ProjectionChange, bytes]] = []
    for change in changes:
        relative = Path(change.path)
        name = relative.name.removesuffix(".json")
        exact_path = False
        if len(relative.parts) == 3:
            directory = relative.parts[1]
            if directory == "results":
                exact_path = (
                    PALOMAR_ID_RE.fullmatch(name) is not None
                    and change.path == result_path(name)
                )
            elif directory == "submissions":
                exact_path = (
                    SUBMISSION_ID_RE.fullmatch(name) is not None
                    and change.path == submission_path(name)
                )
            elif directory == "days":
                try:
                    real_day = dt.date.fromisoformat(name).isoformat() == name
                except ValueError:
                    real_day = False
                exact_path = real_day and change.path == day_path(name)
            elif directory == "identities":
                exact_path = (
                    IDENTITY_DIGEST_RE.fullmatch(name) is not None
                    and change.path == f"{IDENTITIES_DIRECTORY}/{name}.json"
                )
        if (
            change.status not in {"A", "M"}
            or relative.is_absolute()
            or len(relative.parts) != 3
            or relative.parts[0] != "registrations"
            or not exact_path
        ):
            raise ReviewerError("registration projection transition is malformed")
        prepared.append(
            (
                change,
                (json.dumps(change.document, indent=2, sort_keys=True) + "\n").encode(),
            )
        )

        parent_missing = False
        parent = database
        for component in relative.parent.parts:
            parent /= component
            if parent_missing:
                continue
            try:
                mode = parent.lstat().st_mode
            except FileNotFoundError:
                parent_missing = True
            except OSError as error:
                raise ReviewerError(f"{change.path}: parent cannot be inspected") from error
            else:
                if not stat.S_ISDIR(mode):
                    raise ReviewerError(
                        f"{change.path}: projection parent must be an ordinary directory"
                    )

        if parent_missing:
            continue
        target = database / relative
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ReviewerError(f"{change.path}: target cannot be inspected") from error
        if change.status == "A" or not stat.S_ISREG(mode) or mode & 0o111:
            raise ReviewerError(
                f"{change.path}: projection target must be an expected ordinary file"
            )

    for change, encoded in prepared:
        target = database / change.path
        parent = database
        for component in Path(change.path).parent.parts:
            parent /= component
            try:
                parent.mkdir(mode=0o755)
            except FileExistsError:
                pass
            try:
                mode = parent.lstat().st_mode
            except OSError as error:
                raise ReviewerError(f"{change.path}: parent cannot be created safely") from error
            if not stat.S_ISDIR(mode):
                raise ReviewerError(
                    f"{change.path}: projection parent must be an ordinary directory"
                )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
            temporary.chmod(0o644)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _validate_result(document: dict[str, Any], identifier: str, where: str) -> None:
    if set(document) != {"schema_version", "id", "first_registered_on", "identity", "versions"}:
        raise ReviewerError(f"{where}: has an unsupported result projection shape")
    if type(document.get("schema_version")) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ReviewerError(f"{where}: unsupported schema_version")
    match = PALOMAR_ID_RE.fullmatch(identifier)
    first_registered_on = document.get("first_registered_on")
    if document.get("id") != identifier:
        raise ReviewerError(f"{where}: id disagrees with its path")
    if match is None or first_registered_on != match.group("date"):
        raise ReviewerError(f"{where}: first_registered_on disagrees with id")
    try:
        dt.date.fromisoformat(str(first_registered_on))
    except ValueError as error:
        raise ReviewerError(f"{where}: first_registered_on is not a real date") from error
    identity = document.get("identity")
    if not isinstance(identity, dict) or set(identity) != {
        "source_repository",
        "project_path",
        "comparator_config_path",
    }:
        raise ReviewerError(f"{where}: has a malformed stable identity")
    source_repository = identity.get("source_repository")
    if (
        not isinstance(source_repository, str)
        or source_repository != source_repository.casefold()
        or not isinstance(identity.get("comparator_config_path"), str)
        or (
            identity.get("project_path") is not None
            and not isinstance(identity.get("project_path"), str)
        )
    ):
        raise ReviewerError(f"{where}: has a malformed stable identity")
    versions = document.get("versions")
    if (
        not isinstance(versions, list)
        or not versions
        or len(versions) > MAX_VERSIONS_PER_RESULT
    ):
        raise ReviewerError(f"{where}: versions must be a non-empty bounded array")
    keys = {
        "version",
        "submission_id",
        "registered_at",
        "title",
        "status",
        "path",
        "abstract",
        "classification",
    }
    submissions: set[str] = set()
    for expected_version, row in enumerate(versions, 1):
        if not isinstance(row, dict) or set(row) != keys:
            raise ReviewerError(
                f"{where}: version {expected_version} has an unsupported shape"
            )
        submission_id = row.get("submission_id")
        if (
            type(row.get("version")) is not int
            or row["version"] != expected_version
            or not isinstance(submission_id, str)
            or SUBMISSION_ID_RE.fullmatch(submission_id) is None
            or submission_id in submissions
        ):
            raise ReviewerError(
                f"{where}: versions are unordered or have malformed submission ids"
            )
        submissions.add(submission_id)
        if row.get("path") != f"entries/{identifier}-v{expected_version}.json":
            raise ReviewerError(
                f"{where}: version {expected_version} path disagrees with identity"
            )
        if not isinstance(row.get("registered_at"), str):
            raise ReviewerError(
                f"{where}: version {expected_version} has no valid registration instant"
            )
        if not all(isinstance(row.get(field), str) for field in ("title", "status", "abstract")):
            raise ReviewerError(
                f"{where}: version {expected_version} has malformed presentation text"
            )
        classification = row.get("classification")
        if (
            not isinstance(classification, dict)
            or set(classification) != {"arxiv", "msc2020"}
            or any(
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                for values in classification.values()
            )
        ):
            raise ReviewerError(
                f"{where}: version {expected_version} has malformed classification"
            )


def load_result(
    database: Path,
    identifier: str,
    *,
    git_env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    relative = result_path(identifier)
    document = _load_projection(database, relative, git_env=git_env)
    if document is not None:
        _validate_result(document, identifier, relative)
    return document


def load_identity(
    database: Path,
    identity: dict[str, Any],
    *,
    git_env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    relative = identity_path(identity)
    document = _load_projection(database, relative, git_env=git_env)
    if document is None:
        return None
    identifier = document.get("registration_id")
    if (
        set(document) != {"schema_version", "identity", "registration_id"}
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != SCHEMA_VERSION
        or document.get("identity") != identity
        or not isinstance(identifier, str)
        or PALOMAR_ID_RE.fullmatch(identifier) is None
    ):
        raise ReviewerError(f"{relative}: has a malformed identity binding")
    return document


def load_submission(
    database: Path,
    submission_id: str,
    *,
    git_env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    relative = submission_path(submission_id)
    document = _load_projection(database, relative, git_env=git_env)
    if document is None:
        return None
    if set(document) != {"schema_version", "submission_id", "id", "version", "entry_path"}:
        raise ReviewerError(f"{relative}: has an unsupported submission projection shape")
    identifier, version = document.get("id"), document.get("version")
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != SCHEMA_VERSION
        or document.get("submission_id") != submission_id
        or not isinstance(identifier, str)
        or PALOMAR_ID_RE.fullmatch(identifier) is None
        or type(version) is not int
        or version < 1
        or document.get("entry_path") != f"entries/{identifier}-v{version}.json"
    ):
        raise ReviewerError(f"{relative}: has a malformed submission binding")
    return document


def load_day(
    database: Path,
    day: str,
    *,
    git_env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    relative = day_path(day)
    try:
        parsed = dt.date.fromisoformat(day)
    except ValueError as error:
        raise ReviewerError(f"{relative}: date is not a real calendar date") from error
    if parsed.isoformat() != day:
        raise ReviewerError(f"{relative}: date is not in canonical YYYY-MM-DD form")
    document = _load_projection(database, relative, git_env=git_env)
    if document is None:
        return None
    last_serial = document.get("last_serial")
    if (
        set(document) != {"schema_version", "date", "last_serial"}
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != SCHEMA_VERSION
        or document.get("date") != day
        or type(last_serial) is not int
        or not 1 <= last_serial <= 999_999
    ):
        raise ReviewerError(f"{relative}: has a malformed day counter")
    return document


def _mechanical_identity(mechanical: dict[str, Any]) -> dict[str, Any]:
    try:
        source = mechanical["source"]
        comparator = mechanical["comparator"]
        repository = source["repository"]
        identity = {
            "source_repository": repository.casefold(),
            "project_path": source.get("project_path") or None,
            "comparator_config_path": comparator["path"],
        }
    except (AttributeError, KeyError, TypeError) as error:
        raise ReviewerError("mechanical report has no complete registration identity") from error
    if (
        not isinstance(identity["source_repository"], str)
        or not isinstance(identity["comparator_config_path"], str)
        or (
            identity["project_path"] is not None
            and not isinstance(identity["project_path"], str)
        )
    ):
        raise ReviewerError("mechanical report has no complete registration identity")
    return identity


def _assert_identity_matches(
    identifier: str,
    registered: dict[str, Any],
    submitted: dict[str, Any],
) -> None:
    current = registered["identity"]
    if current["source_repository"] != submitted["source_repository"]:
        raise ReviewerError(
            f"update to {identifier} comes from {submitted['source_repository']}, "
            f"not {current['source_repository']}"
        )
    if current["project_path"] != submitted["project_path"]:
        raise ReviewerError(
            f"update to {identifier} comes from project "
            f"{submitted['project_path'] or 'the repository root'}, not "
            f"{current['project_path'] or 'the repository root'}"
        )
    if current["comparator_config_path"] != submitted["comparator_config_path"]:
        raise ReviewerError(
            f"update to {identifier} uses Comparator configuration "
            f"{submitted['comparator_config_path']}, not "
            f"{current['comparator_config_path']}"
        )


def _assert_commit_is_new(
    database: Path,
    identifier: str,
    registered: dict[str, Any],
    mechanical: dict[str, Any],
    *,
    git_env: dict[str, str] | None = None,
) -> None:
    source = mechanical.get("source")
    commit = source.get("commit") if isinstance(source, dict) else None
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise ReviewerError("mechanical report has no complete source commit")
    for row in reversed(registered["versions"]):
        relative = str(row["path"])
        entry = _load_projection(database, relative, git_env=git_env)
        entry_source = entry.get("source") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or entry.get("id") != identifier
            or entry.get("version") != row["version"]
            or not isinstance(entry_source, dict)
            or not isinstance(entry_source.get("commit"), str)
            or COMMIT_RE.fullmatch(entry_source["commit"]) is None
        ):
            raise ReviewerError(f"{relative}: has no complete registered source identity")
        if entry_source["commit"] == commit:
            raise ReviewerError(
                f"{identifier} already has a registered version at source commit {commit}"
            )


def registration_identity(
    database: Path,
    *,
    submission_id: str,
    existing_id: object,
    reviewed_at: object,
    registered_at: str,
    mechanical: dict[str, Any],
    reserved: tuple[str, str, str, int] | None = None,
    git_env: dict[str, str] | None = None,
) -> tuple[str, str, str, int]:
    """Resolve one registration from only its submission, result, and day state."""
    if SUBMISSION_ID_RE.fullmatch(submission_id) is None:
        raise ReviewerError(f"invalid submission id: {submission_id}")
    if existing_id and PALOMAR_ID_RE.fullmatch(str(existing_id)) is None:
        raise ReviewerError(f"requested existing ID is invalid: {existing_id}")
    if not _valid_timestamp(registered_at):
        raise ReviewerError("registration has no valid registration instant")

    binding = load_submission(database, submission_id, git_env=git_env)
    if binding is not None:
        identifier = str(binding["id"])
        if existing_id and identifier != str(existing_id):
            raise ReviewerError("this submission is already associated with another permanent ID")
        raise ReviewerError(
            f"this submission already has a permanent ID; register an update to: {identifier}"
        )

    submitted_identity = _mechanical_identity(mechanical)
    if existing_id:
        identifier = str(existing_id)
        result = load_result(database, identifier, git_env=git_env)
        if result is None:
            raise ReviewerError(f"requested existing ID is not in the database: {identifier}")
        _assert_identity_matches(identifier, result, submitted_identity)
        identity_binding = load_identity(
            database, submitted_identity, git_env=git_env
        )
        if identity_binding is None:
            raise ReviewerError(
                f"requested existing ID has no stable identity binding: {identifier}"
            )
        if identity_binding["registration_id"] != identifier:
            raise ReviewerError(
                "this repository, project, and Comparator configuration already belong "
                f"to {identity_binding['registration_id']}"
            )
        versions = result["versions"]
        if len(versions) >= MAX_VERSIONS_PER_RESULT:
            raise ReviewerError(
                f"{identifier} has reached the {MAX_VERSIONS_PER_RESULT}-version limit"
            )
        _assert_commit_is_new(
            database,
            identifier,
            result,
            mechanical,
            git_env=git_env,
        )
        resolved = (identifier, str(result["first_registered_on"]), registered_at, len(versions) + 1)
        if reserved is not None and reserved != resolved:
            raise ReviewerError("saved registration attempt disagrees with the requested update")
        return resolved

    identity_binding = load_identity(
        database, submitted_identity, git_env=git_env
    )
    if identity_binding is not None:
        raise ReviewerError(
            "this repository, project, and Comparator configuration are already "
            f"registered as {identity_binding['registration_id']}; submit an update "
            "using that Palomar ID"
        )

    try:
        dt.date.fromisoformat(str(reviewed_at)[:10])
    except ValueError as error:
        raise ReviewerError("review has no valid review date") from error

    if reserved is not None:
        identifier, first_registered_on, reserved_instant, version = reserved
        match = PALOMAR_ID_RE.fullmatch(identifier)
        if (
            match is None
            or match.group("date") != first_registered_on
            or not _valid_timestamp(reserved_instant)
            or reserved_instant[:10] != first_registered_on
            or version != 1
        ):
            raise ReviewerError("saved registration attempt has an invalid permanent identity")
        day = first_registered_on
        registered_at = reserved_instant
    else:
        day = registered_at[:10]
        first_registered_on = day
        identifier = ""

    counter = load_day(database, day, git_env=git_env)
    last_serial = int(counter["last_serial"]) if counter is not None else 0
    if reserved is None:
        identifier = allocate_identifier(day, last_serial)
    match = PALOMAR_ID_RE.fullmatch(identifier)
    if match is None or int(match.group("serial")) != last_serial + 1:
        raise ReviewerError(
            f"saved registration attempt {identifier} is not the next serial for {day}"
        )
    if load_result(database, identifier, git_env=git_env) is not None:
        raise ReviewerError(
            f"saved registration attempt {identifier} is already used by another submission"
        )
    return identifier, first_registered_on, registered_at, 1


def reservation_superseded(
    database: Path,
    reserved: tuple[str, str, str, int],
    *,
    existing_id: object,
    git_env: dict[str, str] | None = None,
) -> bool:
    """Whether a saved first-version reservation can never become a registration.

    A reserved serial is usable only while it is still the next one for its day.
    A registration that failed and was retried after another result registered in
    the meantime holds an identifier that will never be allocatable again.

    Only a serial at or behind the counter has been overtaken. One ahead of it
    means the day projection has gone backwards, which is not a stale
    reservation but a registry that disagrees with itself, so it is raised
    rather than quietly resolved by allocating something else.

    An update to an existing result is never superseded: its identifier comes
    from the result it updates rather than from a day counter, so no other
    registration can take it.
    """
    if existing_id:
        return False
    identifier, first_registered_on, _reserved_instant, version = reserved
    if version != 1:
        return False
    match = PALOMAR_ID_RE.fullmatch(identifier)
    if match is None:
        return False
    counter = load_day(database, first_registered_on, git_env=git_env)
    last_serial = int(counter["last_serial"]) if counter is not None else 0
    serial = int(match.group("serial"))
    if serial > last_serial + 1:
        raise ReviewerError(
            f"saved registration attempt {identifier} is ahead of the last serial "
            f"recorded for {first_registered_on}"
        )
    return serial <= last_serial


def projection_changes(
    database: Path,
    *,
    record: dict[str, Any],
    entry_relative: str,
    git_env: dict[str, str] | None = None,
) -> tuple[ProjectionChange, ...]:
    """Build the exact local projection transition for one registered record."""
    identifier = record.get("id")
    version = record.get("version")
    submission = record.get("submission")
    submission_id = submission.get("submission_id") if isinstance(submission, dict) else None
    if (
        not isinstance(identifier, str)
        or PALOMAR_ID_RE.fullmatch(identifier) is None
        or type(version) is not int
        or version < 1
        or not isinstance(submission_id, str)
        or SUBMISSION_ID_RE.fullmatch(submission_id) is None
        or entry_relative != f"entries/{identifier}-v{version}.json"
    ):
        raise ReviewerError("built record has no valid registration projection identity")
    if load_submission(database, submission_id, git_env=git_env) is not None:
        raise ReviewerError(f"submission {submission_id} already has a registration binding")

    source = record.get("source")
    formalization = record.get("formalization")
    repository = source.get("repository") if isinstance(source, dict) else None
    project_path = source.get("project_path") if isinstance(source, dict) else None
    registered_identity = {
        "source_repository": repository.casefold() if isinstance(repository, str) else None,
        "project_path": project_path or None,
        "comparator_config_path": (
            formalization.get("comparator_config_path")
            if isinstance(formalization, dict)
            else None
        ),
    }
    row = {
        "version": version,
        "submission_id": submission_id,
        "registered_at": record.get("registered_at"),
        "title": record.get("title"),
        "status": record.get("status"),
        "path": entry_relative,
        "abstract": record.get("abstract"),
        "classification": copy.deepcopy(record.get("classification")),
    }
    result_relative = result_path(identifier)
    current = load_result(database, identifier, git_env=git_env)
    identity_binding = load_identity(
        database, registered_identity, git_env=git_env
    )
    if version == 1:
        if current is not None:
            raise ReviewerError(f"result projection already exists: {result_relative}")
        if identity_binding is not None:
            raise ReviewerError(
                "registration identity is already bound to "
                f"{identity_binding['registration_id']}"
            )
        result = {
            "schema_version": SCHEMA_VERSION,
            "id": identifier,
            "first_registered_on": record.get("first_registered_on"),
            "identity": registered_identity,
            "versions": [row],
        }
        result_status = "A"
    else:
        if current is None:
            raise ReviewerError(f"result projection does not exist: {result_relative}")
        if identity_binding is None or identity_binding["registration_id"] != identifier:
            raise ReviewerError(
                f"{result_relative}: stable identity binding is missing or disagrees"
            )
        _assert_identity_matches(identifier, current, registered_identity)
        if record.get("first_registered_on") != current["first_registered_on"]:
            raise ReviewerError(f"{result_relative}: stable first registration date changed")
        if version != len(current["versions"]) + 1:
            raise ReviewerError(f"{result_relative}: record is not the next ordered version")
        if len(current["versions"]) >= MAX_VERSIONS_PER_RESULT:
            raise ReviewerError(
                f"{identifier} has reached the {MAX_VERSIONS_PER_RESULT}-version limit"
            )
        result = {**current, "versions": [*current["versions"], row]}
        result_status = "M"
    _validate_result(result, identifier, result_relative)

    binding_relative = submission_path(submission_id)
    changes = [
        ProjectionChange(result_relative, result, result_status),
        ProjectionChange(
            binding_relative,
            {
                "schema_version": SCHEMA_VERSION,
                "submission_id": submission_id,
                "id": identifier,
                "version": version,
                "entry_path": entry_relative,
            },
            "A",
        ),
    ]
    if version == 1:
        match = PALOMAR_ID_RE.fullmatch(identifier)
        assert match is not None
        day, serial = match.group("date"), int(match.group("serial"))
        current_day = load_day(database, day, git_env=git_env)
        last_serial = int(current_day["last_serial"]) if current_day is not None else 0
        if serial != last_serial + 1:
            raise ReviewerError(f"{identifier} is not the next contiguous allocation for {day}")
        changes.append(
            ProjectionChange(
                day_path(day),
                {
                    "schema_version": SCHEMA_VERSION,
                    "date": day,
                    "last_serial": serial,
                },
                "M" if current_day is not None else "A",
            )
        )
        changes.append(
            ProjectionChange(
                identity_path(registered_identity),
                {
                    "schema_version": SCHEMA_VERSION,
                    "identity": registered_identity,
                    "registration_id": identifier,
                },
                "A",
            )
        )
    return tuple(changes)
