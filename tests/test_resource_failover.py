import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from palomar_reviewer import cli, resource_failover, workflow_recovery

PROFILE = "palomar-namespace-16x32-v1"
LABEL = resource_failover.NAMESPACE_PROFILES[PROFILE]


class ResourceEvidenceTests(unittest.TestCase):
    def report(self):
        return {"schema_version": 2, "status": "error", "phase": "verification",
                "execution_profile": PROFILE, "verification_profile": {"id": PROFILE},
                "resource_failure": {"kind": "oom"}, "diagnostics_schema_version": 1,
                "submission": {"submission_id": "abcdefghijkl"},
                "source": {"repository": "owner/project", "commit": "a" * 40},
                "diagnostics": [{"owner": "provider", "code": "provider.resource_exhausted",
                    "stage": "build", "summary": "Worker memory limit", "explanation": "Cgroup OOM",
                    "next_action": "Retry", "retryable": True, "repairable": False}]}

    def test_report_requires_closed_resource_telemetry_and_namespace_profile(self):
        report = self.report()
        self.assertEqual(resource_failover.report_evidence(report, 42),
                         {"kind": "oom", "profile": PROFILE, "run_id": 42})
        for change in ({"resource_failure": {"kind": "disk"}}, {"resource_failure": None},
                       {"resource_failure": {"kind": "oom", "extra": True}},
                       {"execution_profile": "palomar-standard-v1"}, {"status": "fail"},
                       {"phase": "preparation"}, {"diagnostics": []}):
            with self.subTest(change=change):
                self.assertIsNone(resource_failover.report_evidence({**report, **change}, 42))
        report["diagnostics"][0]["owner"] = "submitter"
        self.assertIsNone(resource_failover.report_evidence(report, 42))

    def test_hard_timeout_requires_exact_namespace_verify_job_and_admitted_profile(self):
        run = {"databaseId": 42, "attempt": 1, "conclusion": "timed_out"}
        job = {"name": "verify", "run_id": 42, "run_attempt": 1, "status": "completed",
               "conclusion": "timed_out", "labels": [LABEL, "namespace-features:container.privileged=true"]}
        jobs = {"total_count": 1, "jobs": [job]}
        workflow_recovery.metadata_diagnostic(jobs, run)
        self.assertEqual(resource_failover.timeout_evidence(jobs, run, {}),
                         {"kind": "timeout", "profile": PROFILE, "run_id": 42})
        self.assertIsNone(resource_failover.timeout_evidence(jobs, {**run, "conclusion": "failure"}, {}))
        for change in ({"name": "profile"}, {"conclusion": "failure"}, {"labels": ["ubuntu-24.04"]}):
            self.assertIsNone(resource_failover.timeout_evidence({"jobs": [{**job, **change}]}, run, {}))
        self.assertIsNone(resource_failover.timeout_evidence({"jobs": [job, job]}, run, {}))
        self.assertIsNone(resource_failover.timeout_evidence(jobs, run,
            {"execution": {"profile": "palomar-namespace-32x64-v1"}}))

    def ingest(self, *, untrusted=False, missing=False, timed_out=False, mode="full", synthetic=False):
        state = {"id": "abcdefghijkl", "repository": "owner/project", "commit": "a" * 40,
                 "status": "verification-reporting" if mode == "full" else "preflight-reporting",
                 "run": {"id": 42}, "preflight_run": {"id": 42}}
        run = {"databaseId": 42, "url": "https://github.com/PalomarRegistry/PalomarSubmission/actions/runs/42",
               "attempt": 1, "conclusion": "timed_out" if timed_out else "failure"}
        jobs = {"total_count": 1, "jobs": [{"name": "verify", "run_id": 42, "run_attempt": 1,
            "status": "completed", "conclusion": "timed_out" if timed_out else "failure",
            "labels": [LABEL, "namespace-features:container.privileged=true"]}]}
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "report.json"
            report = self.report()
            if synthetic:
                del report["resource_failure"]
                report["diagnostics"][0].update(owner="palomar", code="palomar.workflow_incomplete")
            artifact.write_text(json.dumps(report))
            with mock.patch.object(cli, "trusted_submission_run", return_value=run), \
                 mock.patch.object(cli, "validate_workflow_commit_on_main",
                                   side_effect=ValueError("untrusted commit") if untrusted else None), \
                 mock.patch.object(cli, "download_mechanical_artifact", return_value=artifact,
                                   side_effect=ValueError("artifact lost") if missing else None), \
                 mock.patch.object(cli, "gh", return_value=json.dumps(jobs)), \
                 mock.patch.object(cli, "advance_state", side_effect=lambda state, status, note, **kw: kw):
                return cli.ingest_failure_diagnostics(state, Path(raw))["failure"]

    def test_authenticated_report_changes_public_advice_before_alerts_are_bound(self):
        failure = self.ingest()
        self.assertEqual(failure["resource_failure"], {"kind": "oom", "profile": PROFILE, "run_id": 42})
        self.assertFalse(failure["diagnostics"][0]["retryable"])
        self.assertIn("Contact Kim Morrison", failure["diagnostics"][0]["next_action"])

    def test_untrusted_run_and_preflight_cannot_authorize_escalation(self):
        self.assertNotIn("resource_failure", self.ingest(untrusted=True))
        self.assertNotIn("resource_failure", self.ingest(mode="preflight"))

    def test_missing_artifact_uses_trusted_timeout_metadata_but_never_guesses_oom(self):
        self.assertNotIn("resource_failure", self.ingest(missing=True))
        failure = self.ingest(missing=True, timed_out=True)
        self.assertEqual(failure["resource_failure"]["kind"], "timeout")
        self.assertEqual(failure["diagnostics"][0]["code"], "provider.workflow_timeout")

    def test_synthetic_finalizer_report_does_not_hide_a_hard_timeout(self):
        failure = self.ingest(synthetic=True, timed_out=True)
        self.assertEqual(failure["resource_failure"]["kind"], "timeout")
        self.assertEqual(failure["diagnostics"][0]["code"], "provider.workflow_timeout")
        self.assertNotIn("resource_failure", self.ingest(synthetic=True))

    def test_recorded_namespace_hard_timeout_is_cancelled_and_cannot_authorize_retry(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "namespace-hard-timeout.json").read_text())
        self.assertEqual(fixture["run"]["conclusion"], "cancelled")
        diagnostic = workflow_recovery.metadata_diagnostic(fixture["jobs"], fixture["run"])
        self.assertEqual(diagnostic["code"], "provider.workflow_interrupted")
        self.assertIsNone(resource_failover.timeout_evidence(fixture["jobs"], fixture["run"], {}))
        finalizer = next(step for step in fixture["jobs"]["jobs"][0]["steps"]
                         if step["name"] == "Observe finalizer scheduling")
        self.assertEqual(finalizer["conclusion"], "success")
