"""A registration allocation is given back on every path that stops using it.

A held day lock defers every later first version registered that day, so a
lock nothing will ever release stalls the queue until midnight. These tests
drive the lock helpers against an in-memory State so that every write is seen.
"""

from __future__ import annotations

import contextlib
import copy
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import palomar_reviewer.checkpoint as registration_checkpoint
import palomar_reviewer.cli as cli
from palomar_reviewer.errors import AlreadyRegisteredError

HOLDER = "a1b2c3d4e5f6"
WAITING = "b2c3d4e5f6a1"
DAY = "2026-08-25"
LOCK_PATH = f"index/registration-locks/day-{DAY}.json"


def identity(serial: int = 1, submission: str = HOLDER) -> dict:
    return {
        "id": f"PALOMAR-{DAY}-{serial:06d}",
        "version": 1,
        "first_registered_on": DAY,
        "registered_at": f"{DAY}T02:36:51Z",
        "review_sha256": "1" * 64,
        "source_repository": f"example/{submission}",
        "source_commit": "2" * 40,
        "existing_id": None,
    }


def public_use(phase: str, held: dict) -> dict:
    row = {
        "schema_version": 1,
        "identity": held,
        "phase": phase,
        "basis": "prospective",
        "recorded_at": f"{DAY}T02:37:00Z",
        "updated_at": f"{DAY}T02:37:00Z",
    }
    if phase in {"published", "registered"}:
        row["archive"] = {"receipt_sha256": "4" * 64, "repositories": []}
    return row


class FakeState:
    """Just enough of the State contents API to see every conditional write."""

    def __init__(self, files: dict[str, dict]):
        self.files = {path: copy.deepcopy(value) for path, value in files.items()}
        self.shas = {path: f"sha-{index}" for index, path in enumerate(self.files)}
        self.writes: list[tuple[str, str]] = []
        self.counter = 0

    def read(self, path: str):
        if path not in self.files:
            return None
        value = copy.deepcopy(self.files[path])
        value["_blob_sha"] = self.shas[path]
        return value

    def write(self, path, value, message, blob_sha=None):
        if path in self.files and blob_sha != self.shas[path]:
            raise cli.ReviewerError(f"{path} changed (HTTP 409)")
        self.counter += 1
        self.files[path] = {k: v for k, v in value.items() if k != "_blob_sha"}
        self.shas[path] = f"sha-w{self.counter}"
        self.writes.append((path, message))
        return self.shas[path]

    @contextlib.contextmanager
    def installed(self, today: str = f"{DAY}T09:00:00Z"):
        with (
            mock.patch.object(cli, "state_json", side_effect=self.read),
            mock.patch.object(cli, "put_state", side_effect=self.write),
            mock.patch.object(cli, "utc_now", return_value=today),
        ):
            yield self

    def lock(self) -> dict:
        return self.files[LOCK_PATH]


def held_lock(held: dict, status: str = "held", submission: str = HOLDER) -> dict:
    return registration_checkpoint.lock_document(
        identity=held,
        submission_id=submission,
        database_base="3" * 40,
        acquired_at=held["registered_at"],
        status=status,
        updated_at=held["registered_at"],
    )


def submission(ident: str, status: str, **fields) -> dict:
    return {"id": ident, "status": status, "events": [], **fields}


def waiting() -> dict:
    return submission(WAITING, "review-ready", registration_consent=True)


class ReclaimTests(unittest.TestCase):
    def store(self, holder_state: dict, *, lock_status: str = "held") -> FakeState:
        held = identity()
        return FakeState({
            LOCK_PATH: held_lock(held, lock_status),
            f"submissions/{HOLDER}/state.json": holder_state,
            f"submissions/{WAITING}/state.json": waiting(),
        })

    def test_a_registered_holder_whose_release_failed_is_released(self):
        """Registered, then the release write failed and nothing retried it."""
        held = identity()
        store = self.store(submission(
            HOLDER, "registered",
            registered_entry=f"{held['id']}-v1",
            registration_attempt={"schema_version": 3, **held},
            public_identity_uses=[public_use("registered", held)],
        ))
        with store.installed():
            cli.defer_if_registration_lock_held(waiting())
        self.assertIsNone(store.lock()["holder"])
        self.assertEqual(
            [message for _, message in store.writes],
            [f"Release registered allocation for {HOLDER}",
             f"Clear registered allocation for {HOLDER}"],
        )

    def test_a_release_interrupted_between_its_two_writes_is_finished(self):
        """Marked releasing, then never cleared."""
        held = identity()
        store = self.store(
            submission(
                HOLDER, "registered",
                registered_entry=f"{held['id']}-v1",
                registration_attempt={"schema_version": 3, **held},
                public_identity_uses=[public_use("registered", held)],
            ),
            lock_status="releasing",
        )
        with store.installed():
            cli.defer_if_registration_lock_held(waiting())
        self.assertIsNone(store.lock()["holder"])

    def test_a_withdrawn_holder_with_an_unpublished_attempt_gives_it_back(self):
        held = identity()
        store = self.store(submission(
            HOLDER, "withdrawn", registration_attempt={"schema_version": 3, **held},
        ))
        with store.installed():
            cli.defer_if_registration_lock_held(waiting())
        self.assertIsNone(store.lock()["holder"])
        # State validation requires a schema-3 attempt to hold a lock, so the
        # attempt is cleared with it rather than left dangling.
        self.assertIsNone(store.files[f"submissions/{HOLDER}/state.json"]["registration_attempt"])

    def test_a_paused_holder_with_an_unpublished_attempt_gives_it_back(self):
        held = identity()
        store = self.store(submission(
            HOLDER, "registration-paused", registration_attempt={"schema_version": 3, **held},
        ))
        with store.installed():
            cli.defer_if_registration_lock_held(waiting())
        self.assertIsNone(store.lock()["holder"])

    def test_an_acquisition_its_holder_never_saved_is_reclaimed(self):
        store = self.store(submission(HOLDER, "withdrawn"), lock_status="acquiring")
        with store.installed():
            cli.defer_if_registration_lock_held(waiting())
        self.assertIsNone(store.lock()["holder"])

    def test_a_holder_whose_identity_is_public_keeps_the_lock(self):
        """Releasing it would let the next registration take the same ID."""
        held = identity()
        store = self.store(submission(
            HOLDER, "withdrawn",
            registration_attempt={"schema_version": 3, **held},
            public_identity_uses=[public_use("published", held)],
        ))
        with store.installed(), self.assertRaisesRegex(cli.RegistrationDeferred, HOLDER):
            cli.defer_if_registration_lock_held(waiting())
        self.assertEqual(store.writes, [])

    def test_an_active_holder_keeps_the_lock(self):
        held = identity()
        store = self.store(submission(
            HOLDER, "review-ready", registration_consent=True,
            registration_attempt={"schema_version": 3, **held},
            public_identity_uses=[public_use("published", held)],
            registration_pr=7,
        ))
        with store.installed(), self.assertRaisesRegex(cli.RegistrationDeferred, HOLDER):
            cli.defer_if_registration_lock_held(waiting())
        self.assertEqual(store.writes, [])

    def test_a_holder_whose_record_cannot_be_read_keeps_the_lock(self):
        """An unreadable record is not evidence that the holder is gone."""
        store = FakeState({LOCK_PATH: held_lock(identity())})
        with store.installed(), self.assertRaisesRegex(cli.RegistrationDeferred, HOLDER):
            cli.defer_if_registration_lock_held(waiting())
        self.assertEqual(store.writes, [])

    def test_a_registered_holder_of_some_other_identity_keeps_the_lock(self):
        held = identity()
        other = identity(serial=2)
        store = self.store(submission(
            HOLDER, "registered",
            registered_entry=f"{other['id']}-v1",
            public_identity_uses=[public_use("registered", other)],
        ))
        with store.installed(), self.assertRaisesRegex(cli.RegistrationDeferred, HOLDER):
            cli.defer_if_registration_lock_held(waiting())
        self.assertEqual(store.writes, [])
        self.assertEqual(store.lock()["holder"]["identity"], held)

    def test_a_free_lock_or_a_lock_of_another_day_does_not_defer(self):
        store = FakeState({LOCK_PATH: held_lock(identity())})
        # The next day names a different lock file, which does not exist yet.
        with store.installed(today="2026-08-26T00:39:00Z"):
            cli.defer_if_registration_lock_held(waiting())
        self.assertEqual(store.writes, [])

    def test_acquisition_reclaims_an_inactive_holder_but_takes_it_next_pass(self):
        """The identity was computed from a Database checkout taken while the
        holder still owned the allocation, so it is not used."""
        held = identity()
        store = self.store(submission(
            HOLDER, "registered",
            registered_entry=f"{held['id']}-v1",
            registration_attempt={"schema_version": 3, **held},
            public_identity_uses=[public_use("registered", held)],
        ))
        mine = identity(serial=2, submission=WAITING)
        with store.installed(), self.assertRaisesRegex(cli.RegistrationDeferred, "next pass"):
            cli._acquire_registration_lock(
                state=store.read(f"submissions/{WAITING}/state.json"),
                identity=mine,
                database_base="3" * 40,
            )
        self.assertIsNone(store.lock()["holder"])

    def test_an_unsaved_acquisition_is_fenced_before_it_is_cleared(self):
        store = self.store(submission(HOLDER, "withdrawn"), lock_status="acquiring")
        with store.installed():
            cli.defer_if_registration_lock_held(waiting())
        self.assertEqual(
            [message.split(" from ")[0] for _, message in store.writes],
            ["Fence registration allocation", "Reclaim registration allocation"],
        )

    def test_an_acquisition_saved_while_it_was_being_fenced_is_released_with_its_attempt(self):
        held = identity()
        state_path = f"submissions/{HOLDER}/state.json"
        store = self.store(submission(HOLDER, "withdrawn"), lock_status="acquiring")
        reads = {"count": 0}
        original = store.read

        def read(path):
            if path == state_path:
                reads["count"] += 1
                if reads["count"] == 2:
                    # The holder's pass saved its attempt after the first look.
                    store.files[state_path]["registration_attempt"] = {
                        "schema_version": 3, **held,
                    }
            return original(path)

        with store.installed(), mock.patch.object(cli, "state_json", side_effect=read):
            cli.defer_if_registration_lock_held(waiting())
        self.assertIsNone(store.lock()["holder"])
        self.assertIsNone(store.files[state_path]["registration_attempt"])


class PauseReleaseTests(unittest.TestCase):
    def test_pausing_an_unpublished_attempt_releases_its_allocation(self):
        held = identity()
        state_path = f"submissions/{HOLDER}/state.json"
        store = FakeState({
            LOCK_PATH: held_lock(held),
            state_path: submission(
                HOLDER, "review-ready", registration_consent=True,
                registration_attempts=1,
                registration_attempt={"schema_version": 3, **held},
            ),
        })
        with store.installed():
            paused = cli.record_registration_failure(
                store.read(state_path),
                cli.ReviewerError("archive fork has no immutable-tag ruleset"),
                deterministic=True,
            )
        self.assertEqual(paused["status"], "registration-paused")
        self.assertIsNone(paused["registration_attempt"])
        self.assertIsNone(store.lock()["holder"])
        self.assertEqual(store.files[state_path]["status"], "registration-paused")

    def test_pausing_a_published_attempt_keeps_its_allocation(self):
        held = identity()
        state_path = f"submissions/{HOLDER}/state.json"
        store = FakeState({
            LOCK_PATH: held_lock(held),
            state_path: submission(
                HOLDER, "review-ready", registration_consent=True,
                registration_attempts=1,
                registration_attempt={"schema_version": 3, **held},
                public_identity_uses=[public_use("published", held)],
            ),
        })
        with store.installed():
            paused = cli.record_registration_failure(
                store.read(state_path), cli.ReviewerError("broken"), deterministic=True,
            )
        self.assertEqual(paused["status"], "registration-paused")
        self.assertEqual(store.lock()["holder"]["submission_id"], HOLDER)

    def test_a_transient_failure_in_backoff_keeps_its_allocation(self):
        """It is still the active registration of that day and comes back."""
        held = identity()
        state_path = f"submissions/{HOLDER}/state.json"
        store = FakeState({
            LOCK_PATH: held_lock(held),
            state_path: submission(
                HOLDER, "review-ready", registration_consent=True,
                registration_attempts=1,
                registration_attempt={"schema_version": 3, **held},
            ),
        })
        with store.installed():
            retrying = cli.record_registration_failure(
                store.read(state_path), cli.ReviewerError("GitHub 502"), deterministic=False,
            )
        self.assertEqual(retrying["status"], "review-ready")
        self.assertEqual(store.lock()["holder"]["submission_id"], HOLDER)


class DeferBeforeAnnouncingTests(unittest.TestCase):
    def test_a_held_lock_defers_before_registration_is_announced(self):
        """Each deferral used to add another "Registration is starting"."""
        args = SimpleNamespace(
            submission=WAITING, work_dir="/nonexistent", render_result=None,
            dry_run=False, count_attempt=True,
        )
        with (
            mock.patch.object(cli, "submission_state", return_value=waiting()),
            mock.patch.object(
                cli, "defer_if_registration_lock_held",
                side_effect=cli.RegistrationDeferred("held by a1b2c3d4e5f6"),
            ),
            mock.patch.object(cli, "delivered_review") as review,
            mock.patch.object(cli, "begin_registration") as began,
            self.assertRaises(cli.RegistrationDeferred),
        ):
            cli.register(args)
        review.assert_not_called()
        began.assert_not_called()

    def test_an_operator_registration_does_not_look_first(self):
        args = SimpleNamespace(
            submission=WAITING, work_dir="/nonexistent", render_result=None,
            dry_run=False, count_attempt=False,
        )
        with (
            mock.patch.object(cli, "defer_if_registration_lock_held") as looked,
            mock.patch.object(cli, "delivered_review", side_effect=cli.ReviewerError("stop")),
            self.assertRaisesRegex(cli.ReviewerError, "stop"),
        ):
            cli.register(args)
        looked.assert_not_called()


class AlreadyRegisteredTests(unittest.TestCase):
    def error(self) -> AlreadyRegisteredError:
        return AlreadyRegisteredError(
            "this repository, project, and Comparator configuration are already "
            "registered as PALOMAR-2026-08-09-000001; submit an update using that Palomar ID",
            registration_id="PALOMAR-2026-08-09-000001",
        )

    def test_it_settles_with_a_submitter_next_action(self):
        state = submission(
            WAITING, "review-ready", registration_consent=True,
            registration_consent_review_sha256="1" * 64, registration_attempts=1,
        )
        with mock.patch.object(cli, "put_state", return_value="sha"):
            settled = cli.record_already_registered(state, self.error())
        self.assertEqual(settled["status"], "verification-failed")
        self.assertFalse(settled["registration_consent"])
        self.assertIsNone(settled["registration_failure"])
        (diagnostic,) = settled["failure"]["diagnostics"]
        self.assertEqual(diagnostic["owner"], "submitter")
        self.assertEqual(diagnostic["code"], "registration.already_registered")
        self.assertFalse(diagnostic["retryable"])
        self.assertIn("Existing Palomar ID set to PALOMAR-2026-08-09-000001",
                      diagnostic["next_action"])
        self.assertIn("PALOMAR-2026-08-09-000001", settled["events"][-1]["note"])

    def automatic_pass(self, state):
        by_id = {state["id"]: state}
        opts = SimpleNamespace(
            max_reviews=5, policy_ref="main", engine="codex", model=None,
            reasoning_effort=None, command=None, work_dir=".palomar-reviews",
            pass_seconds=7200, self_dispatch=False, dispatch_depth=0,
        )
        with (
            mock.patch.object(cli, "open_index", return_value={"open": list(by_id)}),
            mock.patch.object(cli, "submission_state", side_effect=by_id.get),
            mock.patch.object(
                cli, "submission_states",
                side_effect=lambda ids: {name: by_id.get(name) for name in ids},
            ),
            mock.patch.object(cli, "register", side_effect=self.error()),
            mock.patch.object(cli, "record_already_registered") as settled,
            mock.patch.object(cli, "record_registration_failure") as failed,
        ):
            cli.auto(opts)
        return settled, failed

    def test_the_automatic_pass_does_not_retry_or_pause_it(self):
        state = submission(
            WAITING, "review-ready", registration_consent=True,
            review_schema_version=3, created_at="2026-08-25T00:00:00Z",
        )
        settled, failed = self.automatic_pass(state)
        settled.assert_called_once()
        failed.assert_not_called()

    def test_a_submission_with_public_history_is_left_for_an_operator(self):
        held = identity(submission=WAITING)
        state = submission(
            WAITING, "review-ready", registration_consent=True,
            review_schema_version=3, created_at="2026-08-25T00:00:00Z",
            public_identity_uses=[public_use("publication-started", held)],
        )
        settled, failed = self.automatic_pass(state)
        settled.assert_not_called()
        self.assertTrue(failed.call_args.kwargs["deterministic"])


def run_answer(head: str, conclusion: str | None, attempt: int, status: str = "completed"):
    return {
        "id": 99, "status": status, "conclusion": conclusion, "head_sha": head,
        "event": "pull_request", "run_number": 1, "run_attempt": attempt,
    }


class ValidationRerunTests(unittest.TestCase):
    HEAD = "f" * 40

    def github(self, runs: list[list[dict]]):
        """Answer the PR view and then each successive look at Actions."""
        looks = []

        def answer(args, **_kwargs):
            if args and args[0] == "api":
                looks.append(args)
                return json.dumps({"workflow_runs": runs[min(len(looks), len(runs)) - 1]})
            return json.dumps({"state": "OPEN", "mergeStateStatus": "CLEAN",
                               "headRefOid": self.HEAD})

        return answer

    def test_a_failed_validation_is_rerun_and_then_waited_for(self):
        answer = self.github([
            [run_answer(self.HEAD, "failure", 1)],
            [run_answer(self.HEAD, None, 2, status="queued")],
            [run_answer(self.HEAD, "success", 2)],
        ])
        reran = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(cli, "gh", side_effect=answer),
            mock.patch.object(cli, "run", return_value=reran) as ran,
            mock.patch.object(cli.time, "sleep"),
        ):
            view = cli.await_database_checks(7, 3600)
        self.assertEqual(view["validation"], "passed")
        self.assertEqual(
            ran.call_args.args[0],
            ["gh", "run", "rerun", "99", "--failed", "--repo", cli.DATABASE_REPO],
        )

    def test_the_attempt_a_rerun_replaces_is_not_its_verdict(self):
        """Actions may still show the failed attempt just after the rerun."""
        answer = self.github([
            [run_answer(self.HEAD, "failure", 1)],
            [run_answer(self.HEAD, "failure", 1)],
            [run_answer(self.HEAD, "success", 2)],
        ])
        reran = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(cli, "gh", side_effect=answer),
            mock.patch.object(cli, "run", return_value=reran) as ran,
            mock.patch.object(cli.time, "sleep"),
        ):
            view = cli.await_database_checks(7, 3600)
        self.assertEqual(view["validation"], "passed")
        ran.assert_called_once()

    def test_a_validation_that_keeps_failing_is_left_for_a_person(self):
        answer = self.github([[run_answer(self.HEAD, "failure", cli.DATABASE_VALIDATE_ATTEMPT_LIMIT)]])
        with (
            mock.patch.object(cli, "gh", side_effect=answer),
            mock.patch.object(cli, "run") as ran,
            mock.patch.object(cli.time, "sleep") as slept,
        ):
            view = cli.await_database_checks(7, 3600)
        self.assertEqual(view["validation"], "failed")
        ran.assert_not_called()
        slept.assert_not_called()

    def test_a_rerun_the_credential_cannot_start_stops_the_wait(self):
        answer = self.github([[run_answer(self.HEAD, "failure", 1)]])
        refused = SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403")
        with (
            mock.patch.object(cli, "gh", side_effect=answer),
            mock.patch.object(cli, "run", return_value=refused),
            mock.patch.object(cli.time, "sleep") as slept,
        ):
            view = cli.await_database_checks(7, 3600)
        self.assertEqual(view["validation"], "failed")
        slept.assert_not_called()

    def test_a_zero_wait_still_starts_the_rerun(self):
        """The recovery arm looks once; the rerun is what lets the next look pass."""
        answer = self.github([[run_answer(self.HEAD, "failure", 1)]])
        reran = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(cli, "gh", side_effect=answer),
            mock.patch.object(cli, "run", return_value=reran) as ran,
        ):
            view = cli.await_database_checks(7, 0)
        ran.assert_called_once()
        self.assertEqual(view["validation"], "pending")


if __name__ == "__main__":
    unittest.main()
