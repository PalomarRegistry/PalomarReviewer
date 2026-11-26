"""Confidential submissions: run origin, caller trust, stored evidence, alerts and refusals."""

import base64
import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import palomar_reviewer.cli as cli
import palomar_reviewer.confidential as confidential
import palomar_reviewer.engine as engine_execution
import palomar_reviewer.mechanical as mechanical_evidence
import palomar_reviewer.operator_alerts as operator_alerts
from palomar_reviewer.errors import ReviewerError

SUBMISSION = "c0nf1d3nt1a1"
PIPELINE = "1a" * 20
CALLER_HEAD = "2b" * 20
RUNS_URL = f"https://github.com/{confidential.RUNS_REPO}/actions/runs/202"
PUBLIC_URL = "https://github.com/PalomarRegistry/PalomarSubmission/actions/runs/202"
# A string that must never reach anything outside private State.
CANARY = "canary-owner-7f3e/canary-repo-91d2"


TEMPLATES = Path(__file__).resolve().parent / "fixtures" / "confidential-callers"
VERIFY_TEMPLATE = (TEMPLATES / "verify.yml").read_text()
RENDER_TEMPLATE = (TEMPLATES / "render.yml").read_text()
SUMS_TEXT = (TEMPLATES / "SHA256SUMS").read_text()
SUMS = confidential.parse_sha256sums(SUMS_TEXT)
ALLOWED = {confidential.PIPELINE_ALLOWLIST_ENV: PIPELINE}


def caller_file(template=VERIFY_TEMPLATE, pin=PIPELINE):
    return template.replace(confidential.PIPELINE_PLACEHOLDER, pin)


def run_document(**overrides):
    document = {
        "id": 202,
        "name": f"Verify {SUBMISSION}",
        "display_title": f"Verify {SUBMISSION}",
        "path": ".github/workflows/verify.yml",
        "head_branch": "main",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_sha": CALLER_HEAD,
        "run_attempt": 1,
        "html_url": RUNS_URL,
        "created_at": "2026-11-20T09:00:00Z",
        "updated_at": "2026-11-20T09:30:00Z",
        "referenced_workflows": [
            {
                "path": f"PalomarRegistry/PalomarSubmission/.github/workflows/submission.yml@{PIPELINE}",
                "sha": PIPELINE,
                "ref": PIPELINE,
            }
        ],
    }
    document.update(overrides)
    return document


def confidential_state(**overrides):
    state = {
        "id": SUBMISSION,
        "visibility": "confidential",
        "repository": CANARY,
        "commit": "3" * 40,
        "run": {"id": 202},
        "confidential": {"run_repository": confidential.RUNS_REPO},
    }
    state.update(overrides)
    return state


def mechanical_report(workflow_url=RUNS_URL):
    """A schema-1 report shaped like the producer's, for SUBMISSION."""
    return {
        "schema_version": 1, "status": "pass", "stage": "complete",
        "submission": {
            "submission_id": SUBMISSION,
            "authorization": {"relationship": "maintainer"},
            "requested_paths": {
                "project_path": "", "comparator_config_path": "", "formalization_metadata_path": "",
            },
        },
        "source": {
            "repository": CANARY, "repository_url": f"https://github.com/{CANARY}",
            "commit": "3" * 40, "tree_url": f"https://github.com/{CANARY}/tree/" + "3" * 40,
        },
        "license": {
            "path": "LICENSE", "sha256": "a" * 64,
            "declared_identifier": "MIT", "detected_identifier": "MIT",
        },
        "provenance": {
            "result_origin": "original", "repository_role": "substantive-development",
            "responsible_maintainers": [{"name": "Example Maintainer"}],
            "mathematical_sources": [], "related_formalizations": [],
        },
        "challenge": {
            "path": "Challenge.lean", "module": "Challenge", "sha256": "2" * 64, "lines": 10,
            "bytes": 100, "direct_imports": ["Mathlib"], "dependencies": [], "trust_level": "high",
        },
        "solution": {"sha256": "3" * 64, "path": "Solution.lean", "module": "Solution"},
        "lean_toolchain": "leanprover/lean4:v4.31.0",
        "lean_toolchain_path": "lean-toolchain",
        "formalization": {"path": "formalization.yaml", "sha256": "a" * 64},
        "lakefile": {"path": "lakefile.toml", "sha256": "9" * 64, "format": "toml"},
        "comparator": {
            "path": "comparator.json", "sha256": "b" * 64, "challenge_module": "Challenge",
            "solution_module": "Solution", "theorem_names": ["Example.result"],
            "definition_names": [], "permitted_axioms": ["propext"],
        },
        "comparator_commit": "4" * 40, "lean4export_commit": "5" * 40,
        "landrun_commit": "6" * 40, "nanoda_commit": "8" * 40,
        "checked_at": "2026-11-20T09:20:00Z",
        "workflow_url": workflow_url,
        "run_repository": "/".join(workflow_url.split("/")[3:5]),
        "pipeline_commit": PIPELINE,
        "project_dependencies": [],
    }


class FakeGitHub:
    """Answers `cli.run` and `cli.gh` from a table, and records every argv."""

    def __init__(self, document, caller_text=None, compare="ahead"):
        self.document = document
        self.caller_text = caller_file() if caller_text is None else caller_text
        self.compare = compare
        self.calls = []

    def run(self, command, **_kwargs):
        self.calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(self.document), "")

    def gh(self, args, **_kwargs):
        self.calls.append(["gh", *args])
        endpoint = args[1] if args[:1] == ["api"] else ""
        if endpoint.startswith(f"repos/{confidential.RUNS_REPO}/contents/"):
            return base64.b64encode(self.caller_text.encode()).decode()
        if endpoint == (
            "repos/PalomarRegistry/PalomarSubmission/contents/templates/confidential-callers/"
            f"SHA256SUMS?ref={PIPELINE}"
        ):
            return base64.b64encode(SUMS_TEXT.encode()).decode()
        if "/compare/" in endpoint:
            return self.compare + "\n"
        raise AssertionError(f"unexpected gh call {args}")

    @contextlib.contextmanager
    def installed(self, environment=ALLOWED):
        with mock.patch.object(cli, "run", side_effect=self.run), mock.patch.object(
            cli, "gh", side_effect=self.gh
        ), mock.patch.dict(cli.os.environ, environment, clear=False):
            yield self


class VisibilityTests(unittest.TestCase):
    def test_absent_visibility_is_public(self):
        self.assertEqual(confidential.run_origin({"id": SUBMISSION}), confidential.PUBLIC_ORIGIN)

    def test_an_unknown_visibility_fails_closed(self):
        with self.assertRaisesRegex(ReviewerError, "not recognized"):
            confidential.run_origin({"id": SUBMISSION, "visibility": "private"})

    def test_confidential_mode_is_only_for_new_results(self):
        for field in ("existing_id", "registry_correction", "preflight_repair"):
            with self.subTest(field=field), self.assertRaisesRegex(ReviewerError, field):
                confidential.run_origin(confidential_state(**{field: {"x": 1}}))

    def test_a_confidential_record_cannot_name_another_run_repository(self):
        state = confidential_state(confidential={"run_repository": "PalomarRegistry/PalomarSubmission"})
        with self.assertRaisesRegex(ReviewerError, "unrecognized run repository"):
            confidential.run_origin(state)


class CallerTemplateTests(unittest.TestCase):
    def test_the_template_at_its_pin_is_accepted(self):
        for caller, template in (("verify.yml", VERIFY_TEMPLATE), ("render.yml", RENDER_TEMPLATE)):
            with self.subTest(caller=caller):
                self.assertEqual(
                    confidential.validate_caller_file(caller_file(template), caller, PIPELINE, SUMS),
                    SUMS[caller],
                )

    def test_a_modified_caller_is_refused(self):
        modified = caller_file().replace("permissions:\n  contents: read\n", "permissions: write-all\n", 1)
        with self.assertRaisesRegex(ReviewerError, "not the reviewed template"):
            confidential.validate_caller_file(modified, "verify.yml", PIPELINE, SUMS)

    def test_a_caller_pinned_elsewhere_is_refused(self):
        with self.assertRaises(ReviewerError):
            confidential.validate_caller_file(caller_file(pin="4" * 40), "verify.yml", PIPELINE, SUMS)

    def test_a_caller_passing_another_pipeline_commit_is_refused(self):
        text = caller_file().replace(f"pipeline_commit: {PIPELINE}", "pipeline_commit: " + "4" * 40)
        with self.assertRaisesRegex(ReviewerError, "exactly in uses and pipeline_commit"):
            confidential.validate_caller_file(text, "verify.yml", PIPELINE, SUMS)

    def test_sha256sums_must_list_exactly_the_two_callers(self):
        for text in ("", SUMS_TEXT + SUMS_TEXT.splitlines()[0] + "\n", "x  verify.yml\n"):
            with self.subTest(text=text), self.assertRaises(ReviewerError):
                confidential.parse_sha256sums(text)

    def test_a_run_must_reference_exactly_the_called_workflow(self):
        cases = {
            "two references": [run_document()["referenced_workflows"][0]] * 2,
            "another workflow": [{
                "path": f"PalomarRegistry/PalomarSubmission/.github/workflows/other.yml@{PIPELINE}",
                "sha": PIPELINE,
            }],
            "another repository": [{
                "path": f"someone/PalomarSubmission/.github/workflows/submission.yml@{PIPELINE}",
                "sha": PIPELINE,
            }],
            "sha disagrees with path": [{
                "path": f"PalomarRegistry/PalomarSubmission/.github/workflows/submission.yml@{PIPELINE}",
                "sha": "5" * 40,
            }],
        }
        for name, references in cases.items():
            with self.subTest(name), self.assertRaises(ReviewerError):
                confidential.referenced_pipeline_commit(
                    {"referenced_workflows": references}, "verify.yml"
                )


class TrustedConfidentialRunTests(unittest.TestCase):
    def test_a_confidential_run_is_read_from_the_runs_repository(self):
        github = FakeGitHub(run_document())
        with github.installed():
            run_data = cli.trusted_verification_run(confidential_state())
        self.assertEqual(run_data["url"], RUNS_URL)
        self.assertEqual(run_data["pipelineCommit"], PIPELINE)
        self.assertEqual(run_data["headSha"], CALLER_HEAD)
        self.assertEqual(github.calls[0], ["gh", "api", f"repos/{confidential.RUNS_REPO}/actions/runs/202"])
        self.assertIn(f"?ref={CALLER_HEAD}", github.calls[1][2])

    def test_a_public_run_is_unchanged(self):
        document = run_document(
            name=f"Verify submission {SUBMISSION}", display_title=f"Verify submission {SUBMISSION}",
            path=".github/workflows/submission.yml", html_url=PUBLIC_URL,
        )
        github = FakeGitHub(document)
        with github.installed():
            run_data = cli.trusted_verification_run({"id": SUBMISSION, "run": {"id": 202}})
        self.assertEqual(run_data["url"], PUBLIC_URL)
        self.assertNotIn("pipelineCommit", run_data)
        self.assertEqual(len(github.calls), 1)

    def test_a_modified_caller_file_is_refused(self):
        github = FakeGitHub(run_document(), caller_text=caller_file() + "# extra step\n")
        with github.installed(), self.assertRaisesRegex(ReviewerError, "not the reviewed template"):
            cli.trusted_verification_run(confidential_state())

    def test_a_confidential_submission_refuses_a_public_run(self):
        document = run_document(
            name=f"Verify submission {SUBMISSION}", display_title=f"Verify submission {SUBMISSION}",
            path=".github/workflows/submission.yml", html_url=PUBLIC_URL,
        )
        with FakeGitHub(document).installed(), self.assertRaises(ReviewerError):
            cli.trusted_verification_run(confidential_state())

    def test_a_public_submission_refuses_a_confidential_run(self):
        with FakeGitHub(run_document()).installed(), self.assertRaises(ReviewerError):
            cli.trusted_verification_run({"id": SUBMISSION, "run": {"id": 202}})

    def test_a_pipeline_commit_outside_the_allowlist_is_refused(self):
        for environment in ({confidential.PIPELINE_ALLOWLIST_ENV: "4" * 40}, {}):
            with self.subTest(environment=environment), mock.patch.dict(
                cli.os.environ, {}, clear=True
            ), FakeGitHub(run_document()).installed(environment):
                with self.assertRaisesRegex(ReviewerError, "not allowlisted"):
                    cli.trusted_verification_run(confidential_state())

    def test_a_revoked_pipeline_commit_is_refused(self):
        environment = {**ALLOWED, confidential.PIPELINE_REVOKED_ENV: PIPELINE}
        with FakeGitHub(run_document()).installed(environment), self.assertRaisesRegex(
            ReviewerError, "revoked"
        ):
            cli.trusted_verification_run(confidential_state())

    def test_an_untrusted_referenced_pipeline_commit_is_refused(self):
        github = FakeGitHub(run_document(), compare="diverged")
        with github.installed():
            run_data = cli.trusted_verification_run(confidential_state())
            with self.assertRaisesRegex(ReviewerError, "not an ancestor of main"):
                cli.validate_workflow_commit_on_main(run_data)
        compare = [call for call in github.calls if "/compare/" in " ".join(call)]
        self.assertEqual(
            compare[0][2], f"repos/PalomarRegistry/PalomarSubmission/compare/{PIPELINE}...main"
        )


class ReportOriginTests(unittest.TestCase):
    def run_data(self, url=RUNS_URL):
        return {"url": url, "headSha": CALLER_HEAD, "event": "workflow_dispatch", "pipelineCommit": PIPELINE}

    def test_a_confidential_report_binds_the_called_pipeline_commit(self):
        workflow_commit = mechanical_evidence.validate_report_contract(
            mechanical_report(), confidential_state(), self.run_data()
        )
        self.assertEqual(workflow_commit, PIPELINE)

    def test_a_public_report_cannot_answer_for_a_confidential_submission(self):
        with self.assertRaisesRegex(ReviewerError, "visibility does not allow"):
            mechanical_evidence.validate_report_contract(
                mechanical_report(PUBLIC_URL), confidential_state(), self.run_data(PUBLIC_URL)
            )

    def test_a_confidential_report_cannot_answer_for_a_public_submission(self):
        state = {key: value for key, value in confidential_state().items()
                 if key not in {"visibility", "confidential"}}
        with self.assertRaisesRegex(ReviewerError, "visibility does not allow"):
            mechanical_evidence.validate_report_contract(mechanical_report(), state, self.run_data())

    def test_a_report_that_executed_another_pipeline_commit_is_refused(self):
        report = {**mechanical_report(), "pipeline_commit": "7" * 40}
        with self.assertRaisesRegex(ReviewerError, "did not execute the pipeline commit"):
            mechanical_evidence.validate_report_contract(report, confidential_state(), self.run_data())

    def test_a_confidential_report_must_name_its_run_repository(self):
        report = {key: value for key, value in mechanical_report().items() if key != "run_repository"}
        with self.assertRaisesRegex(ReviewerError, "does not name its run repository"):
            mechanical_evidence.validate_report_contract(report, confidential_state(), self.run_data())

    def test_a_confidential_run_without_its_pipeline_commit_is_refused(self):
        run_data = {key: value for key, value in self.run_data().items() if key != "pipelineCommit"}
        with self.assertRaisesRegex(ReviewerError, "no trusted pipeline commit"):
            mechanical_evidence.validate_report_contract(mechanical_report(), confidential_state(), run_data)


class ConfidentialRenderTests(unittest.TestCase):
    def test_a_confidential_render_is_dispatched_only_to_the_runs_repository(self):
        mechanical = mechanical_report()
        mechanical["execution_profile"] = "palomar-standard-v1"
        calls = []

        def gh(args, **_kwargs):
            calls.append(args)
            return "[]" if args[:2] == ["run", "list"] else ""

        clock = iter([0, 0, 10_000])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cli, "gh", side_effect=gh
        ), mock.patch.object(cli.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(ReviewerError, "not visible"):
                cli.request_render(Path(directory), mechanical)
        dispatch = calls[0]
        self.assertEqual(dispatch[:5], ["workflow", "run", "render.yml", "--repo", confidential.RUNS_REPO])
        for call in calls:
            self.assertNotIn("PalomarRegistry/PalomarSubmission", call)
        self.assertEqual(
            confidential.CONFIDENTIAL_ORIGIN.render_titles(mechanical, "f" * 32),
            ("Render challenge [" + "f" * 32 + "]",),
        )


class AlertTests(unittest.TestCase):
    def failure(self):
        return {
            "phase": "verification",
            "run": {"id": 202, "url": RUNS_URL},
            "diagnostics": [{
                "owner": "palomar", "code": "palomar.workflow_incomplete", "stage": "build",
                "summary": f"build of {CANARY} failed in Challenge.lean",
                "explanation": "x", "next_action": "y", "retryable": True,
            }],
        }

    PINNED = "\n".join([
        "**Palomar must fix this** [confidential]",
        "",
        f"- Submission: `{SUBMISSION}`",
        f"- Source: withheld (confidential; see State submissions/{SUBMISSION}/state.json)",
        "- Workflow: private run 202 in PalomarConfidentialRuns",
        "- Phase/stage: `verification` / `build`",
        "- Diagnostic: `palomar.workflow_incomplete` (owner `palomar`)",
        "- Retryable unchanged: `yes`",
    ])

    def queued(self, **extra):
        state = {**confidential_state(), "failure": self.failure(), **extra}
        outbox = operator_alerts.queued_alerts(state, state["failure"], queued_at="2026-11-20T10:00:00Z")
        return {**state, "operator_alerts": outbox}, outbox["items"][0]

    def test_a_confidential_failure_posts_only_the_pinned_redacted_alert(self):
        state, item = self.queued()
        message = operator_alerts.alert_message(state, item)
        self.assertEqual(message, self.PINNED + f"\n- Alert key: `{item['key'][:16]}`")
        for forbidden in (CANARY, "3" * 12, "Challenge.lean", "failed in", RUNS_URL):
            self.assertNotIn(forbidden, message)

    def test_a_technical_test_alert_says_so(self):
        state, item = self.queued(test_submission=True)
        self.assertTrue(operator_alerts.alert_message(state, item).startswith(
            "**Palomar must fix this** [confidential] **[technical test]**\n"
        ))

    def test_a_disposition_adds_only_its_label(self):
        state, item = self.queued()
        item = {**item, "disposition": {
            "kind": "recovered", "at": "2026-11-21T00:00:00Z",
            "evidence": {"submission_id": SUBMISSION, "commit": "3" * 40, "run_url": RUNS_URL},
        }}
        state = {**state, "operator_alerts": {"schema_version": 2, "items": [item]}}
        message = operator_alerts.alert_message(state, item)
        self.assertTrue(message.endswith(
            "\n\n**Disposition: Recovered: the same commit and configuration verified successfully.**"
        ))
        self.assertNotIn("3" * 40, message)
        self.assertNotIn(RUNS_URL, message)

    def test_a_confidential_alert_must_name_a_private_run(self):
        failure = {**self.failure(), "run": {"id": 202, "url": PUBLIC_URL}}
        with self.assertRaisesRegex(ReviewerError, "private run"):
            operator_alerts.confidential_alert_message(
                {"id": SUBMISSION, "failure": failure}, "a" * 64, 0
            )

    def test_public_alerts_are_unchanged(self):
        public = {key: value for key, value in confidential_state().items()
                  if key not in {"visibility", "confidential"}}
        public["failure"] = {**self.failure(), "run": {"id": 202, "url": PUBLIC_URL}}
        queued = operator_alerts.queued_alerts(public, public["failure"], queued_at="2026-11-20T10:00:00Z")
        message = operator_alerts.alert_message({**public, "operator_alerts": queued}, queued["items"][0])
        self.assertIn(CANARY, message)


class RefusalTests(unittest.TestCase):
    def test_repair_is_refused(self):
        with self.assertRaisesRegex(ReviewerError, "not available for a confidential submission"):
            cli._validate_repair({"schema_version": 4, "submission_id": SUBMISSION}, confidential_state())

    def test_engines_that_may_expose_web_tools_are_refused(self):
        for engine in ("claude", "command"):
            with self.subTest(engine=engine), self.assertRaises(engine_execution.EngineError):
                engine_execution.refuse_web_tools(engine, confidential=True)
        engine_execution.refuse_web_tools("codex", confidential=True)
        engine_execution.refuse_web_tools("claude", confidential=False)


if __name__ == "__main__":
    unittest.main()
