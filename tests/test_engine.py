import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from palomar_reviewer import engine


class EngineConfigurationTests(unittest.TestCase):
    def test_supported_engines_and_custom_command_requirement_are_one_contract(self):
        engine.validate_config("codex", None)
        engine.validate_config("claude", None)
        engine.validate_config("command", "review --json")
        with self.assertRaisesRegex(engine.EngineError, "--command is required"):
            engine.validate_config("command", None)
        with self.assertRaisesRegex(engine.EngineError, "unsupported engine: future"):
            engine.validate_config("future", None)

    def test_codex_arguments_preserve_model_reasoning_and_output_contract(self):
        self.assertEqual(
            engine.codex_arguments(
                schema_name="schema.json",
                output_name="message.txt",
                model="gpt-test",
                reasoning_effort="high",
            ),
            [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--ignore-user-config",
                "--json",
                "--output-schema",
                "/output/schema.json",
                "--output-last-message",
                "/output/message.txt",
                "--cd",
                "/workspace",
                "--model",
                "gpt-test",
                "-c",
                "model_reasoning_effort=high",
                "-",
            ],
        )

    def test_claude_and_custom_arguments_preserve_current_capabilities(self):
        schema = {"type": "object"}
        claude = engine.claude_arguments(
            schema=schema,
            model="claude-test",
            allow_network=True,
        )
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "auto")
        self.assertEqual(claude[claude.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(claude[claude.index("--json-schema") + 1], '{"type":"object"}')
        self.assertEqual(claude[-2:], ["--model", "claude-test"])
        self.assertEqual(
            engine.custom_arguments("review --format json 'two words'"),
            ["review", "--format", "json", "two words"],
        )

    def test_model_identity_uses_the_executable_the_engine_will_run(self):
        self.assertEqual(engine.identity("codex", "gpt-test", None), "codex:gpt-test")
        self.assertEqual(engine.identity("claude", None, None), "claude:default")
        self.assertEqual(
            engine.identity(
                "command",
                None,
                "'/opt/Palomar Reviewer/bin/reviewer' --json",
            ),
            "command:/opt/Palomar Reviewer/bin/reviewer",
        )
        with self.assertRaisesRegex(ValueError, "No closing quotation"):
            engine.identity("command", None, "'unterminated")

    def test_subprocess_failure_retains_the_cli_diagnostic_shape(self):
        failed = subprocess.CompletedProcess(
            ["engine", "one", "two", "ignored"],
            7,
            "",
            "provider unavailable\n",
        )
        with mock.patch.object(engine.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(
                engine.EngineError,
                r"engine one two failed \(7\): provider unavailable",
            ):
                engine._run(["engine", "one", "two", "ignored"], input_text="prompt")


class EngineExecutionTests(unittest.TestCase):
    def test_codex_collects_events_usage_and_the_final_message(self):
        turn = {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "cache_write_input_tokens": 10,
            "output_tokens": 5,
            "reasoning_output_tokens": 2,
        }
        events = json.dumps({"type": "turn.completed", "usage": turn}) + "\n"
        schema = {
            "type": "object",
            "required": ["step"],
            "properties": {"step": {"type": "string"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            raw = root / "raw" / "metadata.txt"
            source.mkdir()

            def completed(argv, **kwargs):
                output = raw.parent / ".metadata.txt.engine-output"
                (output / "message.txt").write_text('{"step":"metadata"}', encoding="utf-8")
                return SimpleNamespace(stdout=events)

            with (
                mock.patch.object(
                    engine,
                    "isolated_command",
                    side_effect=lambda _engine, argv, **_kwargs: argv,
                ),
                mock.patch.object(engine, "_run", side_effect=completed) as runner,
            ):
                result, usage = engine.execute(
                    "review this",
                    engine="codex",
                    command=None,
                    model="gpt-test",
                    cwd=source,
                    schema=schema,
                    raw_path=raw,
                    reasoning_effort="medium",
                )

            argv = runner.call_args.args[0]
            self.assertEqual(argv[:2], ["codex", "exec"])
            self.assertIn("--ignore-user-config", argv)
            self.assertEqual(argv[argv.index("--model") + 1], "gpt-test")
            self.assertIn("model_reasoning_effort=medium", argv)
            self.assertEqual(runner.call_args.kwargs["input_text"], "review this")
            self.assertEqual(runner.call_args.kwargs["timeout"], 7200)
            self.assertEqual(result, {"step": "metadata"})
            self.assertEqual(usage["usage_status"], "recorded")
            self.assertEqual(usage["turns"], [turn])
            self.assertEqual(raw.read_text(encoding="utf-8"), '{"step":"metadata"}')
            self.assertEqual(
                (raw.parent / "metadata.events.jsonl").read_text(encoding="utf-8"),
                events,
            )
            self.assertEqual(
                json.loads(
                    (raw.parent / ".metadata.txt.engine-output" / "schema.json").read_text(
                        encoding="utf-8"
                    )
                ),
                schema,
            )

    def test_custom_command_receives_prompt_and_persists_its_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            raw = root / "raw" / "custom.txt"
            source.mkdir()
            completed = SimpleNamespace(stdout='{"accepted":true}\n')
            with (
                mock.patch.object(
                    engine,
                    "isolated_command",
                    side_effect=lambda _engine, argv, **_kwargs: argv,
                ),
                mock.patch.object(engine, "_run", return_value=completed) as runner,
            ):
                result, usage = engine.execute(
                    "custom prompt",
                    engine="command",
                    command="reviewer --mode 'two words'",
                    model=None,
                    cwd=source,
                    schema={
                        "type": "object",
                        "required": ["accepted"],
                        "properties": {"accepted": {"type": "boolean"}},
                    },
                    raw_path=raw,
                )

            self.assertEqual(runner.call_args.args[0], ["reviewer", "--mode", "two words"])
            self.assertEqual(runner.call_args.kwargs["input_text"], "custom prompt")
            self.assertEqual(result, {"accepted": True})
            self.assertEqual(usage["usage_status"], "unavailable")
            self.assertEqual(raw.read_text(encoding="utf-8"), '{"accepted":true}\n')

    def test_non_json_output_is_an_engine_failure(self):
        with self.assertRaisesRegex(engine.EngineError, "did not return a JSON object"):
            engine.parse_json("not a result")


if __name__ == "__main__":
    unittest.main()
