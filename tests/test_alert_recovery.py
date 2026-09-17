import copy
import json
import unittest
from unittest.mock import patch

from palomar_reviewer import alert_recovery, operator_alerts, workflow_recovery
from palomar_reviewer.errors import ReviewerError


class AlertRecoveryTests(unittest.TestCase):
    def state(self):
        state = {
            "id": "abcdefghijkl",
            "repository": "owner/repo",
            "commit": "a" * 40,
            "created_at": "2026-09-01T00:00:00Z",
            "status": "verification-error",
            "requested_paths": {},
            "failure": {
                "phase": "verification",
                "run": {"id": 1, "url": operator_alerts.ZULIP_MESSAGES_URL},
                "diagnostics": [
                    {
                        "code": "provider.timeout",
                        "owner": "provider",
                        "stage": "build",
                        "summary": "timeout",
                        "explanation": "worker timed out",
                        "next_action": "ask operator",
                        "retryable": True,
                        "repairable": False,
                    }
                ],
            },
        }
        state["failure"]["run"]["url"] = "https://github.com/PalomarRegistry/PalomarSubmission/actions/runs/1"
        state["operator_alerts"] = operator_alerts.queued_alerts(
            state, state["failure"], queued_at=state["created_at"]
        )
        return state

    def outcome(self, state, commit=None):
        return {
            "configuration": alert_recovery.configuration_key(state),
            "commit": commit or state["commit"],
            "submission_id": "bcdefghijklm",
            "verified_at": "2026-09-02T00:00:00Z",
            "run_url": "https://github.com/PalomarRegistry/PalomarSubmission/actions/runs/2",
        }

    def test_origin_survives_a_later_attempt_replacing_failure(self):
        state = self.state()
        original = operator_alerts.alert_message(state, state["operator_alerts"]["items"][0])
        state["failure"] = {"diagnostics": []}
        self.assertEqual(operator_alerts.alert_message(state, state["operator_alerts"]["items"][0]), original)
        corrupt = copy.deepcopy(state)
        corrupt["operator_alerts"]["items"][0]["origin"]["commit"] = "b" * 40
        with self.assertRaises(ReviewerError):
            operator_alerts.validated_alert_items(corrupt)

    def test_recovery_supersession_withdrawal_and_configuration_isolation(self):
        state = self.state()
        at = "2026-09-03T00:00:00Z"
        for commit, kind in (("a" * 40, "recovered"), ("b" * 40, "superseded")):
            state["operator_alerts"] = alert_recovery.desired_alerts(
                state, [self.outcome(state, commit)], at=at, is_successor=lambda *_: True
            )
            self.assertEqual(state["operator_alerts"]["items"][0]["disposition"]["kind"], kind)
            text = operator_alerts.alert_message(state, state["operator_alerts"]["items"][0])
            self.assertIn("worker timed out", text)
            self.assertNotIn("status?", text)
        state["status"] = "withdrawn"
        result = alert_recovery.desired_alerts(state, [self.outcome(state)], at=at)
        self.assertEqual(result["items"][0]["disposition"]["kind"], "withdrawn")
        state = self.state()
        other = {**state, "requested_paths": {"project_path": "different"}}
        result = alert_recovery.desired_alerts(state, [self.outcome(other)], at=at)
        self.assertNotIn("disposition", result["items"][0])

    def test_lost_ack_is_idempotent_and_human_edits_conflict(self):
        class Response:
            def __init__(self, content):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, limit):
                return json.dumps(
                    {"result": "success", "zulip_feature_level": 511, "message": {"content": self.content}}
                ).encode()

        for current, expected in (("desired", "delivered"), ("human edit", "conflict")):
            with patch.object(
                operator_alerts.urllib.request, "urlopen", return_value=Response(current)
            ) as request:
                self.assertEqual(
                    operator_alerts.edit_zulip_message(
                        1,
                        "desired",
                        expected_hash=operator_alerts.content_hash("original"),
                        email="bot",
                        api_key="key",
                    ),
                    expected,
                )
                self.assertEqual(request.call_count, 1)
        with patch.object(
            operator_alerts.urllib.request, "urlopen", return_value=Response("original")
        ) as request:
            self.assertEqual(
                operator_alerts.edit_zulip_message(
                    1,
                    "desired",
                    expected_hash=operator_alerts.content_hash("original"),
                    email="bot",
                    api_key="key",
                ),
                "delivered",
            )
            self.assertEqual(request.call_args.args[0].method, "PATCH")
            self.assertIn(b"prev_content_sha256=", request.call_args.args[0].data)

    def test_exact_workflow_attempt_and_non_log_diagnosis(self):
        run = {"databaseId": 1, "attempt": 2, "conclusion": "failure"}
        jobs = {
            "total_count": 1,
            "jobs": [
                {
                    "run_id": 1,
                    "run_attempt": 2,
                    "status": "completed",
                    "steps": [{"name": "Install pinned elan", "conclusion": "failure"}],
                }
            ],
        }
        diagnostic = workflow_recovery.metadata_diagnostic(jobs, run)
        self.assertEqual(diagnostic["code"], "palomar.workflow_step_failed")
        jobs["jobs"][0]["run_attempt"] = 1
        with self.assertRaises(ReviewerError):
            workflow_recovery.metadata_diagnostic(jobs, run)
        with self.assertRaises(ReviewerError):
            workflow_recovery.validate_execution_binding({"execution_attempt": "a" * 32}, {})
        with self.assertRaises(ReviewerError):
            workflow_recovery.validate_execution_binding({}, {"execution": {"attempt": "b" * 32}})

    def test_success_predating_alert_does_not_hide_a_later_failure(self):
        state = self.state()
        state["operator_alerts"]["items"][0]["queued_at"] = "2026-09-03T00:00:00Z"
        result = alert_recovery.desired_alerts(state, [self.outcome(state)], at="2026-09-04T00:00:00Z")
        self.assertNotIn("disposition", result["items"][0])
        state = self.state()
        result = alert_recovery.desired_alerts(
            state, [self.outcome(state, "b" * 40)], at="2026-09-04T00:00:00Z", is_successor=lambda *_: False
        )
        self.assertNotIn("disposition", result["items"][0])

    def test_edit_permission_failure_is_not_a_human_content_conflict(self):
        import io
        import urllib.error

        for code, expected in (("EXPECTATION_MISMATCH", "conflict"), ("BAD_REQUEST", "unavailable")):
            error = urllib.error.HTTPError(
                "https://example.com", 400, "Bad request", {}, io.BytesIO(json.dumps({"code": code}).encode())
            )
            with patch.object(operator_alerts.urllib.request, "urlopen", side_effect=error):
                result = operator_alerts.edit_zulip_message(
                    1, "desired", expected_hash="a" * 64, email="bot", api_key="key"
                )
                self.assertEqual(result, expected)

    def test_sent_disposition_records_the_exact_delivered_content(self):
        from palomar_reviewer import cli

        state = self.state()
        state["operator_alerts"] = alert_recovery.desired_alerts(
            state, [self.outcome(state)], at="2026-09-04T00:00:00Z"
        )
        item = state["operator_alerts"]["items"][0]
        content = operator_alerts.alert_message(state, item)
        with patch.object(cli, "put_state", return_value="blob"):
            updated = cli._record_operator_alert_sent(state, key=item["key"], message_id=1, content=content)
        self.assertEqual(
            updated["operator_alerts"]["items"][0]["delivered_sha256"], operator_alerts.content_hash(content)
        )
        operator_alerts.validated_alert_items(updated)

    def test_malformed_outbox_does_not_stop_unrelated_review_work(self):
        from palomar_reviewer import cli

        state = self.state()
        state["operator_alerts"]["items"][0]["key"] = "forged"
        self.assertFalse(cli.finished_with(state))
        with patch.object(cli, "open_submissions", return_value=[state]):
            self.assertEqual(cli.notify_operator_alerts(None), 1)

    def test_corrupt_cached_evidence_is_refused(self):
        index = {"schema_version": 1, "groups": {}, "outcomes": {"abcdefghijkl": {"commit": "a" * 40}}}
        with self.assertRaises(ReviewerError):
            alert_recovery.validated_cached_outcomes(index)
        self.assertEqual(alert_recovery.validated_cached_outcomes(None), {})
        with self.assertRaises(ReviewerError):
            alert_recovery.validated_cached_outcomes({"schema_version": True, "groups": {}, "outcomes": {}})

    def test_run_name_must_bind_the_operator_execution_attempt(self):
        from palomar_reviewer.cli import normalized_submission_run

        title = "Verify submission abcdefghijkl"
        document = {
            "id": 1,
            "name": title,
            "display_title": title,
            "path": ".github/workflows/submission.yml",
            "head_branch": "main",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "a" * 40,
            "run_attempt": 1,
            "created_at": "2026-09-16T00:00:00Z",
            "updated_at": "2026-09-16T01:00:00Z",
            "html_url": "https://github.com/PalomarRegistry/PalomarSubmission/actions/runs/1",
        }
        with self.assertRaises(ReviewerError):
            normalized_submission_run(document, 1, "abcdefghijkl", execution_attempt="b" * 32)
        document["name"] = document["display_title"] = title + " [" + "b" * 32 + "]"
        self.assertEqual(
            normalized_submission_run(document, 1, "abcdefghijkl", execution_attempt="b" * 32)["databaseId"],
            1,
        )

    def test_scoped_reconciliation_uses_index_without_cloning_all_state(self):
        from types import SimpleNamespace

        from palomar_reviewer import cli

        original = self.state()
        candidate = {
            **original,
            "id": "bcdefghijklm",
            "operator_alerts": None,
            "run": {"id": 2, "conclusion": "success", "url": self.outcome(original)["run_url"]},
        }
        group = operator_alerts.content_hash(json.dumps(alert_recovery.configuration_key(candidate)))
        index = {"schema_version": 1, "groups": {group: [original["id"]]}, "outcomes": {}}
        mechanical = {"checked_at": "2026-09-02T00:00:00Z", "workflow_url": candidate["run"]["url"]}
        with (
            patch.object(cli, "state_json", return_value=index),
            patch.object(
                cli,
                "submission_state",
                side_effect=lambda identifier: {original["id"]: original, candidate["id"]: candidate}[
                    identifier
                ],
            ),
            patch.object(
                cli,
                "mechanical_report",
                return_value=(mechanical, candidate["run"]["url"], {"headSha": "a" * 40}),
            ),
            patch.object(cli, "run", side_effect=AssertionError("must not clone")),
            patch.object(cli, "put_state") as write,
        ):
            self.assertEqual(
                cli.reconcile_operator_alerts(
                    SimpleNamespace(apply=False, deliver=False, submission=candidate["id"])
                ),
                0,
            )
            write.assert_not_called()

    def test_noncanonical_outcome_timestamp_cannot_poison_the_cache(self):
        with self.assertRaises(ReviewerError):
            alert_recovery.outcome(self.state(), {"checked_at": "2026-09-01T00:00:00+01:00"})

    def test_old_zulip_server_never_receives_an_unconditional_edit(self):
        class Response:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, limit):
                return json.dumps(self.data).encode()

        with patch.object(
            operator_alerts.urllib.request,
            "urlopen",
            side_effect=[
                Response({"result": "success", "message": {"content": "original"}}),
                Response({"zulip_feature_level": 378}),
            ],
        ) as request:
            self.assertEqual(
                operator_alerts.edit_zulip_message(
                    1,
                    "desired",
                    expected_hash=operator_alerts.content_hash("original"),
                    email="bot",
                    api_key="key",
                ),
                "unavailable",
            )
            self.assertEqual(request.call_count, 2)
            self.assertTrue(all(call.args[0].get_method() == "GET" for call in request.call_args_list))
