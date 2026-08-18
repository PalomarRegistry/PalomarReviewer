from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import palomar_reviewer.registration as registration
from palomar_reviewer.errors import ReviewerError

FIRST = "PALOMAR-2026-08-09-000001"


def write_json(root: Path, relative: str, document: object) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def commit(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-C",
            str(root),
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )


def result_document(*, versions: int = 1) -> dict:
    return {
        "schema_version": 2,
        "id": FIRST,
        "first_registered_on": "2026-08-09",
        "identity": {
            "source_repository": "example/project",
            "project_path": None,
            "comparator_config_path": "comparator.json",
        },
        "versions": [
            {
                "version": version,
                "submission_id": f"{version:012d}",
                "registered_at": f"2026-08-{min(9 + version - 1, 31):02d}T12:00:00Z",
                "title": f"Version {version}",
                "status": "registered",
                "path": f"entries/{FIRST}-v{version}.json",
                "abstract": f"Abstract {version}",
                "classification": {"arxiv": ["math.CO"], "msc2020": ["05C10"]},
            }
            for version in range(1, versions + 1)
        ],
    }


def identity_document(identifier: str = FIRST) -> dict:
    identity = result_document()["identity"]
    return {
        "schema_version": 2,
        "identity": identity,
        "registration_id": identifier,
    }


def write_identity(root: Path, identifier: str = FIRST) -> None:
    document = identity_document(identifier)
    write_json(root, registration.identity_path(document["identity"]), document)


def record(*, identifier: str = FIRST, version: int = 1, submission: str = "a1b2c3d4e5f6") -> dict:
    return {
        "id": identifier,
        "first_registered_on": identifier[len("PALOMAR-") :][:10],
        "registered_at": "2026-08-09T12:00:00Z",
        "version": version,
        "title": "A result",
        "status": "registered",
        "abstract": "An abstract",
        "classification": {"arxiv": ["math.CO"], "msc2020": ["05C10"]},
        "source": {"repository": "example/project", "commit": "1" * 40},
        "formalization": {"comparator_config_path": "comparator.json"},
        "submission": {"submission_id": submission},
    }


class RegistrationProjectionTests(unittest.TestCase):
    def repo(self) -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        (root / ".fixture").write_text("segmented authority\n")
        commit(root)
        return root

    def test_authority_json_is_strict(self):
        for encoded in (
            '{"schema_version":1,"schema_version":1}\n',
            '{"schema_version":NaN}\n',
            '{"schema_version":Infinity}\n',
            '{"schema_version":1e400}\n',
        ):
            with self.subTest(encoded=encoded):
                repo = self.repo()
                relative = registration.day_path("2026-08-09")
                path = repo / relative
                path.parent.mkdir(parents=True)
                path.write_text(encoded)
                commit(repo)
                with self.assertRaisesRegex(ReviewerError, "is not valid strict JSON"):
                    registration.load_day(repo, "2026-08-09")

    def test_every_existing_authority_path_requires_exact_git_mode_100644(self):
        documents = {
            "result": (registration.result_path(FIRST), result_document()),
            "submission": (
                registration.submission_path("a1b2c3d4e5f6"),
                {
                    "schema_version": 2,
                    "submission_id": "a1b2c3d4e5f6",
                    "id": FIRST,
                    "version": 1,
                    "entry_path": f"entries/{FIRST}-v1.json",
                },
            ),
            "day": (
                registration.day_path("2026-08-09"),
                {"schema_version": 2, "date": "2026-08-09", "last_serial": 1},
            ),
            "identity": (
                registration.identity_path(result_document()["identity"]),
                identity_document(),
            ),
        }
        for kind, (relative, document) in documents.items():
            with self.subTest(kind=kind):
                repo = self.repo()
                write_json(repo, relative, document)
                commit(repo)
                subprocess.run(
                    ["git", "-C", str(repo), "update-index", "--chmod=+x", "--", relative],
                    check=True,
                )
                with self.assertRaisesRegex(ReviewerError, "exact Git mode 100644"):
                    if kind == "result":
                        registration.load_result(repo, FIRST)
                    elif kind == "submission":
                        registration.load_submission(repo, "a1b2c3d4e5f6")
                    elif kind == "identity":
                        registration.load_identity(repo, result_document()["identity"])
                    else:
                        registration.load_day(repo, "2026-08-09")

    def test_every_materialized_authority_path_must_be_non_executable(self):
        documents = {
            "result": (registration.result_path(FIRST), result_document()),
            "submission": (
                registration.submission_path("a1b2c3d4e5f6"),
                {
                    "schema_version": 2,
                    "submission_id": "a1b2c3d4e5f6",
                    "id": FIRST,
                    "version": 1,
                    "entry_path": f"entries/{FIRST}-v1.json",
                },
            ),
            "day": (
                registration.day_path("2026-08-09"),
                {"schema_version": 2, "date": "2026-08-09", "last_serial": 1},
            ),
            "identity": (
                registration.identity_path(result_document()["identity"]),
                identity_document(),
            ),
        }
        for kind, (relative, document) in documents.items():
            with self.subTest(kind=kind):
                repo = self.repo()
                write_json(repo, relative, document)
                commit(repo)
                (repo / relative).chmod(0o755)
                with self.assertRaisesRegex(ReviewerError, "non-executable"):
                    if kind == "result":
                        registration.load_result(repo, FIRST)
                    elif kind == "submission":
                        registration.load_submission(repo, "a1b2c3d4e5f6")
                    elif kind == "identity":
                        registration.load_identity(repo, result_document()["identity"])
                    else:
                        registration.load_day(repo, "2026-08-09")

    def test_a_tracked_projection_missing_from_the_worktree_is_not_absence(self):
        repo = self.repo()
        relative = registration.day_path("2026-08-09")
        write_json(
            repo,
            relative,
            {"schema_version": 2, "date": "2026-08-09", "last_serial": 1},
        )
        commit(repo)
        (repo / relative).unlink()
        with self.assertRaisesRegex(ReviewerError, "tracked authority file is missing"):
            registration.load_day(repo, "2026-08-09")

    def test_a_sparse_projection_is_read_from_head_without_materializing_it(self):
        repo = self.repo()
        relative = registration.day_path("2026-08-09")
        document = {"schema_version": 2, "date": "2026-08-09", "last_serial": 1}
        write_json(repo, relative, document)
        commit(repo)
        subprocess.run(
            ["git", "-C", str(repo), "sparse-checkout", "init", "--no-cone"],
            check=True,
        )
        (repo / ".git" / "info" / "sparse-checkout").write_text(
            "/*\n!/registrations/\n"
        )
        subprocess.run(["git", "-C", str(repo), "read-tree", "-mu", "HEAD"], check=True)
        self.assertFalse((repo / relative).exists())
        self.assertEqual(registration.load_day(repo, "2026-08-09"), document)
        self.assertFalse((repo / relative).exists())

    def test_a_dangling_sparse_projection_symlink_is_not_authority(self):
        repo = self.repo()
        relative = registration.day_path("2026-08-09")
        write_json(
            repo,
            relative,
            {"schema_version": 2, "date": "2026-08-09", "last_serial": 1},
        )
        commit(repo)
        subprocess.run(
            ["git", "-C", str(repo), "sparse-checkout", "init", "--no-cone"],
            check=True,
        )
        (repo / ".git" / "info" / "sparse-checkout").write_text(
            "/*\n!/registrations/\n"
        )
        subprocess.run(["git", "-C", str(repo), "read-tree", "-mu", "HEAD"], check=True)
        target = repo / relative
        target.parent.mkdir(parents=True)
        target.symlink_to(repo / "does-not-exist.json")
        with self.assertRaisesRegex(ReviewerError, "ordinary non-executable"):
            registration.load_day(repo, "2026-08-09")

    def test_an_untracked_projection_cannot_override_validated_main(self):
        repo = self.repo()
        relative = registration.day_path("2026-08-09")
        write_json(
            repo,
            relative,
            {"schema_version": 2, "date": "2026-08-09", "last_serial": 1},
        )
        with self.assertRaisesRegex(ReviewerError, "not authority from the checked-out commit"):
            registration.load_day(repo, "2026-08-09")

    def test_a_result_projection_rejects_repeated_submission_identity(self):
        repo = self.repo()
        document = result_document(versions=2)
        document["versions"][1]["submission_id"] = document["versions"][0]["submission_id"]
        write_json(repo, registration.result_path(FIRST), document)
        commit(repo)
        with self.assertRaisesRegex(ReviewerError, "malformed submission ids"):
            registration.load_result(repo, FIRST)

    def test_a_result_projection_rejects_malformed_historical_presentation(self):
        malformed = (
            ("title", None, "presentation text"),
            ("status", ["accepted"], "presentation text"),
            ("abstract", 7, "presentation text"),
            ("classification", {"arxiv": [7], "msc2020": []}, "classification"),
            ("classification", {"arxiv": []}, "classification"),
        )
        for field, value, message in malformed:
            with self.subTest(field=field, value=value):
                repo = self.repo()
                document = result_document()
                document["versions"][0][field] = value
                write_json(repo, registration.result_path(FIRST), document)
                commit(repo)
                with self.assertRaisesRegex(ReviewerError, message):
                    registration.load_result(repo, FIRST)

    def test_update_repository_identity_uses_the_github_comparison_key(self):
        repo = self.repo()
        write_json(repo, registration.result_path(FIRST), result_document())
        write_json(repo, f"entries/{FIRST}-v1.json", record())
        write_identity(repo)
        commit(repo)
        identity = registration.registration_identity(
            repo,
            submission_id="newversion12",
            existing_id=FIRST,
            reviewed_at="2026-08-09T10:00:00Z",
            registered_at="2026-08-09T12:00:00Z",
            mechanical={
                "source": {"repository": "Example/Project", "commit": "2" * 40},
                "comparator": {"path": "comparator.json"},
            },
        )
        self.assertEqual(identity[-1], 2)

    def test_an_immutable_submission_binding_stops_duplicate_registration(self):
        repo = self.repo()
        write_json(
            repo,
            registration.submission_path("a1b2c3d4e5f6"),
            {
                "schema_version": 2,
                "submission_id": "a1b2c3d4e5f6",
                "id": FIRST,
                "version": 1,
                "entry_path": f"entries/{FIRST}-v1.json",
            },
        )
        commit(repo)
        with self.assertRaisesRegex(ReviewerError, "already has a permanent ID"):
            registration.registration_identity(
                repo,
                submission_id="a1b2c3d4e5f6",
                existing_id=None,
                reviewed_at="2026-08-09T10:00:00Z",
                registered_at="2026-08-09T12:00:00Z",
                mechanical={
                    "source": {"repository": "example/project"},
                    "comparator": {"path": "comparator.json"},
                },
            )

    def test_an_existing_identity_cannot_be_allocated_again_without_its_id(self):
        repo = self.repo()
        write_identity(repo)
        commit(repo)
        with self.assertRaisesRegex(ReviewerError, f"registered as {FIRST}"):
            registration.registration_identity(
                repo,
                submission_id="newresult123",
                existing_id=None,
                reviewed_at="2026-08-09T10:00:00Z",
                registered_at="2026-08-09T12:00:00Z",
                mechanical={
                    "source": {"repository": "example/project", "commit": "2" * 40},
                    "comparator": {"path": "comparator.json"},
                },
            )

    def test_a_malformed_submission_binding_is_not_retry_identity(self):
        repo = self.repo()
        relative = registration.submission_path("a1b2c3d4e5f6")
        write_json(
            repo,
            relative,
            {
                "schema_version": 2,
                "submission_id": "a1b2c3d4e5f6",
                "id": FIRST,
                "version": 2,
                "entry_path": f"entries/{FIRST}-v1.json",
            },
        )
        commit(repo)
        with self.assertRaisesRegex(ReviewerError, "malformed submission binding"):
            registration.load_submission(repo, "a1b2c3d4e5f6")

    def test_a_malformed_day_counter_is_not_allocation_input(self):
        repo = self.repo()
        write_json(
            repo,
            registration.day_path("2026-08-09"),
            {"schema_version": 2, "date": "2026-08-09", "last_serial": 0},
        )
        commit(repo)
        with self.assertRaisesRegex(ReviewerError, "malformed day counter"):
            registration.load_day(repo, "2026-08-09")

    def test_a_noncanonical_iso_day_is_not_a_projection_path(self):
        with self.assertRaisesRegex(ReviewerError, "canonical YYYY-MM-DD"):
            registration.load_day(self.repo(), "20260809")

    def test_first_projection_transition_includes_the_stable_identity_binding(self):
        first = record()
        first["source"]["repository"] = "Example/Project"
        changes = registration.projection_changes(
            self.repo(),
            record=first,
            entry_relative=f"entries/{FIRST}-v1.json",
        )
        self.assertEqual(
            {(change.path, change.status) for change in changes},
            {
                (registration.result_path(FIRST), "A"),
                (registration.submission_path("a1b2c3d4e5f6"), "A"),
                (registration.day_path("2026-08-09"), "A"),
                (registration.identity_path(result_document()["identity"]), "A"),
            },
        )
        result = next(
            change.document
            for change in changes
            if change.path.startswith("registrations/results/")
        )
        self.assertEqual(
            result["identity"]["source_repository"],
            "example/project",
        )
        self.assertEqual(
            result["versions"],
            [
                {
                    "version": 1,
                    "submission_id": "a1b2c3d4e5f6",
                    "registered_at": "2026-08-09T12:00:00Z",
                    "title": "A result",
                    "status": "registered",
                    "path": f"entries/{FIRST}-v1.json",
                    "abstract": "An abstract",
                    "classification": {"arxiv": ["math.CO"], "msc2020": ["05C10"]},
                }
            ],
        )

    def test_projection_materialization_rejects_every_symlinked_parent_before_writing(self):
        for planted in (
            "registrations",
            "registrations/results",
            "registrations/submissions",
            "registrations/days",
            "registrations/identities",
        ):
            with self.subTest(planted=planted):
                repo = self.repo()
                changes = registration.projection_changes(
                    repo,
                    record=record(),
                    entry_relative=f"entries/{FIRST}-v1.json",
                )
                external = repo / "external"
                external.mkdir()
                parent = repo / planted
                parent.parent.mkdir(parents=True, exist_ok=True)
                parent.symlink_to(external, target_is_directory=True)
                with self.assertRaisesRegex(ReviewerError, "ordinary directory"):
                    registration.materialize_changes(repo, changes)
                self.assertEqual(list(external.iterdir()), [])
                self.assertFalse((repo / registration.result_path(FIRST)).is_file())
                self.assertFalse(
                    (repo / registration.submission_path("a1b2c3d4e5f6")).is_file()
                )

    def test_projection_materialization_rejects_a_dangling_target_before_writing(self):
        repo = self.repo()
        changes = registration.projection_changes(
            repo,
            record=record(),
            entry_relative=f"entries/{FIRST}-v1.json",
        )
        result = repo / registration.result_path(FIRST)
        result.parent.mkdir(parents=True)
        escaped = repo / "escaped.json"
        result.symlink_to(escaped)
        with self.assertRaisesRegex(ReviewerError, "expected ordinary file"):
            registration.materialize_changes(repo, changes)
        self.assertFalse(escaped.exists())
        self.assertFalse(
            (repo / registration.submission_path("a1b2c3d4e5f6")).exists()
        )
        self.assertFalse((repo / registration.day_path("2026-08-09")).exists())

    def test_a_first_projection_must_advance_the_day_counter_exactly(self):
        repo = self.repo()
        write_json(
            repo,
            registration.day_path("2026-08-09"),
            {"schema_version": 2, "date": "2026-08-09", "last_serial": 1},
        )
        commit(repo)
        with self.assertRaisesRegex(ReviewerError, "not the next contiguous allocation"):
            registration.projection_changes(
                repo,
                record=record(),
                entry_relative=f"entries/{FIRST}-v1.json",
            )

    def test_a_later_first_registration_modifies_the_existing_day_counter(self):
        repo = self.repo()
        write_json(
            repo,
            registration.day_path("2026-08-09"),
            {"schema_version": 2, "date": "2026-08-09", "last_serial": 1},
        )
        commit(repo)
        identifier = "PALOMAR-2026-08-09-000002"
        changes = registration.projection_changes(
            repo,
            record=record(identifier=identifier),
            entry_relative=f"entries/{identifier}-v1.json",
        )
        day = next(
            change for change in changes if change.path.startswith("registrations/days/")
        )
        self.assertEqual((day.status, day.document["last_serial"]), ("M", 2))

    def test_a_501st_version_is_refused_before_building_a_projection(self):
        repo = self.repo()
        write_json(repo, registration.result_path(FIRST), result_document(versions=500))
        write_identity(repo)
        commit(repo)
        with self.assertRaisesRegex(ReviewerError, "500-version limit"):
            registration.registration_identity(
                repo,
                submission_id="newversion12",
                existing_id=FIRST,
                reviewed_at="2026-08-09T10:00:00Z",
                registered_at="2026-08-09T12:00:00Z",
                mechanical={
                    "source": {"repository": "example/project"},
                    "comparator": {"path": "comparator.json"},
                },
            )
