import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from palomar_reviewer import broker, engine

BROKER_CAPABILITY = "capability-for-one-pass"
BROKER_SUMMARY = {"schema_version": 1, "requests": 2, "refusals": {}}


@contextlib.contextmanager
def pinned_codex():
    """Take the installed-Codex check as given, for tests that are not it."""
    with mock.patch.object(engine, "require_pinned_codex"):
        yield


@contextlib.contextmanager
def fake_broker(summary=None):
    """A started broker without the child process, for the pass around it."""
    handle = SimpleNamespace(
        base_url="http://127.0.0.1:4321/v1",
        capability=BROKER_CAPABILITY,
        summary=BROKER_SUMMARY if summary is None else summary,
        exit_status=0,
    )

    @contextlib.contextmanager
    def started(*, policy, upstream_key):
        handle.policy = policy
        handle.upstream_key = upstream_key
        yield handle

    with pinned_codex(), mock.patch.object(engine.model_broker, "started_broker", started):
        yield handle


class EngineConfigurationTests(unittest.TestCase):
    def test_supported_engines_and_custom_command_requirement_are_one_contract(self):
        engine.validate_config("codex", None)
        engine.validate_config("claude", None)
        engine.validate_config("command", "review --json")
        with self.assertRaisesRegex(engine.EngineError, "--command is required"):
            engine.validate_config("command", None)
        with self.assertRaisesRegex(engine.EngineError, "--command is required"):
            engine.validate_config("command", "  \t")
        with self.assertRaisesRegex(engine.EngineError, "unsupported engine: future"):
            engine.validate_config("future", None)

    def test_codex_arguments_preserve_model_reasoning_and_output_contract(self):
        self.assertEqual(
            engine.codex_arguments(
                schema_name="schema.json",
                output_name="message.txt",
                model="gpt-test",
                reasoning_effort="high",
                broker_base_url="http://127.0.0.1:4321/v1",
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
                'model_provider="palomar_broker"',
                "-c",
                'model_providers.palomar_broker.name="Palomar loopback model broker"',
                "-c",
                'model_providers.palomar_broker.base_url="http://127.0.0.1:4321/v1"',
                "-c",
                'model_providers.palomar_broker.wire_api="responses"',
                "-c",
                'model_providers.palomar_broker.env_key="PALOMAR_MODEL_BROKER_TOKEN"',
                "-c",
                "model_providers.palomar_broker.stream_idle_timeout_ms=660000",
                "-c",
                "model_reasoning_effort=high",
                "-",
            ],
        )

    def test_the_provider_is_not_named_as_one_with_a_second_endpoint(self):
        """Pinned Codex compacts remotely for a provider it takes for OpenAI.

        It decides that from the provider name and the base URL, and it does it
        on `/responses/compact`, which the broker does not serve. Naming the
        provider for what it is keeps a long pass on the one endpoint.
        """
        argv = engine.codex_arguments(
            schema_name="schema.json",
            output_name="message.txt",
            model="gpt-test",
            reasoning_effort=None,
            broker_base_url="http://127.0.0.1:4321/v1",
        )
        configured = " ".join(argv).lower()
        self.assertNotIn('.name="openai"', configured)
        for azure in ("openai.azure.", "cognitiveservices.azure.", "aoai.azure."):
            self.assertNotIn(azure, configured)

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
        claude_without_tools = engine.claude_arguments(
            schema=schema,
            model=None,
            allow_network=False,
        )
        self.assertEqual(
            claude_without_tools[claude_without_tools.index("--tools") + 1],
            "",
        )
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
        with self.assertRaisesRegex(engine.EngineError, "invalid --command"):
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

    def test_timeout_is_an_engine_failure_without_provider_output(self):
        with mock.patch.object(
            engine.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["engine", "one"], 7),
        ):
            with self.assertRaisesRegex(
                engine.EngineError, "engine one timed out after 7 seconds"
            ):
                engine._run(["engine", "one"], timeout=7)

    def test_subprocess_start_and_decode_failures_are_engine_failures(self):
        failures = (
            (OSError("could not start"), "could not start"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), "invalid"),
        )
        for error, message in failures:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(engine.subprocess, "run", side_effect=error):
                    with self.assertRaisesRegex(engine.EngineError, message):
                        engine._run(["engine"])


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
                fake_broker() as handle,
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

            self.assertEqual(handle.policy.model, "gpt-test")
            self.assertEqual(handle.policy.reasoning_effort, "medium")
            self.assertEqual(usage["broker"], BROKER_SUMMARY)
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

    def test_codex_namespace_binds_the_exact_native_platform_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope = root / "node_modules" / "@openai"
            wrapper = scope / "codex" / "bin" / "codex.js"
            native = scope / "codex-linux-x64"
            binary = root / "node_modules" / ".bin" / "codex"
            wrapper.parent.mkdir(parents=True)
            native.mkdir()
            binary.parent.mkdir(parents=True)
            wrapper.write_text("", encoding="utf-8")
            (native / "package.json").write_text("{}\n", encoding="utf-8")
            binary.symlink_to(Path("../@openai/codex/bin/codex.js"))
            source = root / "source"
            source.mkdir()

            programs = {
                "bwrap": "/usr/bin/bwrap",
                "codex": str(binary),
                "node": str(Path(engine.shutil.which("node") or "").resolve()),
            }
            with (
                mock.patch.object(engine.shutil, "which", side_effect=programs.get),
                mock.patch.object(engine.sys, "platform", "linux"),
                mock.patch.object(engine.platform, "machine", return_value="x86_64"),
            ):
                command = engine.isolated_command(
                    "codex",
                    ["codex", "exec"],
                    cwd=source,
                    output_dir=root / "output",
                    secret_args_fd=9,
                )

            bind = command.index("/engine/node_modules/@openai/codex-linux-x64")
            self.assertEqual(
                command[bind - 2 : bind + 1],
                ["--ro-bind", str(native), command[bind]],
            )

            (native / "package.json").unlink()
            native.rmdir()
            with (
                mock.patch.object(engine.shutil, "which", side_effect=programs.get),
                mock.patch.object(engine.sys, "platform", "linux"),
                mock.patch.object(engine.platform, "machine", return_value="x86_64"),
                self.assertRaisesRegex(engine.EngineError, "codex-linux-x64"),
            ):
                engine.isolated_command(
                    "codex",
                    ["codex", "exec"],
                    cwd=source,
                    output_dir=root / "other-output",
                    secret_args_fd=9,
                )
    def test_codex_requires_a_regular_final_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with (
                mock.patch.object(
                    engine,
                    "isolated_command",
                    side_effect=lambda _engine, argv, **_kwargs: argv,
                ),
                mock.patch.object(
                    engine,
                    "_run",
                    return_value=SimpleNamespace(stdout=""),
                ),
                fake_broker(),
            ):
                with self.assertRaisesRegex(
                    engine.EngineError, "regular final-message file"
                ):
                    engine.execute(
                        "prompt",
                        engine="codex",
                        command=None,
                        model="gpt-test",
                        cwd=source,
                        schema={"type": "object"},
                        raw_path=root / "raw" / "result.txt",
                    )

    def test_codex_refuses_to_run_without_a_model_the_broker_can_enforce(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with (
                mock.patch.object(engine, "_run") as runner,
                mock.patch.object(engine.model_broker, "started_broker") as started,
            ):
                with self.assertRaisesRegex(engine.EngineError, "requires --model"):
                    engine.execute(
                        "prompt",
                        engine="codex",
                        command=None,
                        model=None,
                        cwd=source,
                        schema={"type": "object"},
                        raw_path=root / "raw" / "result.txt",
                    )
            started.assert_not_called()
            runner.assert_not_called()

    def test_a_broker_that_cannot_start_fails_the_pass_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with (
                pinned_codex(),
                mock.patch.object(engine, "_run") as runner,
                mock.patch.object(
                    engine.model_broker,
                    "started_broker",
                    side_effect=broker.BrokerError("no upstream credential"),
                ),
            ):
                with self.assertRaisesRegex(
                    engine.EngineError, "loopback model broker failed: no upstream credential"
                ):
                    engine.execute(
                        "prompt",
                        engine="codex",
                        command=None,
                        model="gpt-test",
                        cwd=source,
                        schema={"type": "object"},
                        raw_path=root / "raw" / "result.txt",
                    )
            # Nothing ran: a pass with no boundary in front of it is not a pass
            # that runs with the boundary missing.
            runner.assert_not_called()

    def test_a_codex_that_is_not_the_pinned_one_does_not_run(self):
        """Every header and route assumption was read off one release."""
        with mock.patch.object(engine, "codex_version", return_value="codex-cli 0.147.0"):
            engine.require_pinned_codex()
        for found in ("codex-cli 0.148.0", "codex-cli 0.146.9", "", "codex 0.147.0"):
            with self.subTest(version=found):
                with mock.patch.object(engine, "codex_version", return_value=found):
                    with self.assertRaisesRegex(engine.EngineError, "0.147.0"):
                        engine.require_pinned_codex()

    def test_a_pass_whose_broker_left_no_account_is_refused(self):
        turn = {
            "input_tokens": 1,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
        }
        events = json.dumps({"type": "turn.completed", "usage": turn}) + "\n"
        for name, broken in (
            ("no summary", {"summary": None, "exit_status": 0}),
            ("an abnormal exit", {"summary": BROKER_SUMMARY, "exit_status": -9}),
        ):
            with self.subTest(broker=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "source"
                    raw = root / "raw" / "metadata.txt"
                    source.mkdir()

                    def completed(argv, _raw=raw, **kwargs):
                        output = _raw.parent / ".metadata.txt.engine-output"
                        (output / "message.txt").write_text("{}", encoding="utf-8")
                        return SimpleNamespace(stdout=events)

                    with (
                        mock.patch.object(
                            engine,
                            "isolated_command",
                            side_effect=lambda _engine, argv, **_kwargs: argv,
                        ),
                        mock.patch.object(engine, "_run", side_effect=completed),
                        fake_broker() as handle,
                    ):
                        handle.summary = broken["summary"]
                        handle.exit_status = broken["exit_status"]
                        with self.assertRaisesRegex(
                            engine.EngineError, "did not shut down cleanly"
                        ):
                            engine.execute(
                                "prompt",
                                engine="codex",
                                command=None,
                                model="gpt-test",
                                cwd=source,
                                schema={"type": "object"},
                                raw_path=raw,
                            )

    def test_the_capability_reaches_the_namespace_off_the_command_line(self):
        """The one credential in the namespace arrives through a pipe, not argv."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            raw = root / "raw" / "metadata.txt"
            source.mkdir()
            seen: dict[str, object] = {}

            def completed(argv, **kwargs):
                fd = seen["fd"]
                assert isinstance(fd, int)
                seen["args"] = os.read(fd, 4096)
                seen["pass_fds"] = kwargs["pass_fds"]
                output = raw.parent / ".metadata.txt.engine-output"
                (output / "message.txt").write_text("{}", encoding="utf-8")
                return SimpleNamespace(stdout="")

            def isolated(_engine, argv, *, secret_args_fd=None, **_kwargs):
                seen["fd"] = secret_args_fd
                return ["bwrap", "--args", str(secret_args_fd), "--", *argv]

            with (
                mock.patch.object(engine, "isolated_command", side_effect=isolated),
                mock.patch.object(engine, "_run", side_effect=completed) as runner,
                fake_broker(),
            ):
                engine.execute(
                    "prompt",
                    engine="codex",
                    command=None,
                    model="gpt-test",
                    cwd=source,
                    schema={"type": "object"},
                    raw_path=raw,
                )

            self.assertEqual(
                seen["args"],
                b"--setenv\0PALOMAR_MODEL_BROKER_TOKEN\0" + BROKER_CAPABILITY.encode("utf-8"),
            )
            self.assertEqual(seen["pass_fds"], (seen["fd"],))
            self.assertNotIn(BROKER_CAPABILITY, " ".join(runner.call_args.args[0]))
            # And the descriptor is closed again, whatever the pass did.
            with self.assertRaises(OSError):
                os.fstat(seen["fd"])

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

    def test_result_must_be_exactly_one_json_object(self):
        self.assertEqual(engine.parse_json('  {"accepted":true}\n'), {"accepted": True})
        for text in (
            "not a result",
            'result\n```json\n{"accepted":true}\n```',
            'result: {"accepted":true}',
            '{"accepted":true}\nfinished',
            '[{"accepted":true}]',
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(
                    engine.EngineError, "did not return a JSON object"
                ):
                    engine.parse_json(text)

    def test_result_schema_failure_is_a_concise_engine_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with (
                mock.patch.object(
                    engine,
                    "isolated_command",
                    side_effect=lambda _engine, argv, **_kwargs: argv,
                ),
                mock.patch.object(
                    engine,
                    "_run",
                    return_value=SimpleNamespace(stdout='{"accepted":"secret value"}'),
                ),
            ):
                with self.assertRaisesRegex(
                    engine.EngineError,
                    r"result did not match the required schema \(type\)",
                ) as raised:
                    engine.execute(
                        "prompt",
                        engine="command",
                        command="reviewer",
                        model=None,
                        cwd=source,
                        schema={
                            "type": "object",
                            "properties": {"accepted": {"type": "boolean"}},
                        },
                        raw_path=root / "raw" / "result.txt",
                    )
        self.assertNotIn("secret value", str(raised.exception))

    def test_invalid_result_schema_is_an_engine_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with (
                mock.patch.object(
                    engine,
                    "isolated_command",
                    side_effect=lambda _engine, argv, **_kwargs: argv,
                ),
                mock.patch.object(
                    engine,
                    "_run",
                    return_value=SimpleNamespace(stdout="{}"),
                ),
            ):
                with self.assertRaisesRegex(
                    engine.EngineError, "review result schema is invalid"
                ):
                    engine.execute(
                        "prompt",
                        engine="command",
                        command="reviewer",
                        model=None,
                        cwd=source,
                        schema={"type": 12},
                        raw_path=root / "raw" / "result.txt",
                    )

    def test_invalid_config_touches_no_output_path(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "uncreated" / "result.txt"
            with self.assertRaisesRegex(engine.EngineError, "unsupported engine"):
                engine.execute(
                    "prompt",
                    engine="future",
                    command=None,
                    model=None,
                    cwd=Path(directory),
                    schema={"type": "object"},
                    raw_path=raw,
                )
            self.assertFalse(raw.parent.exists())

    def test_filesystem_failure_is_an_engine_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_file = root / "not-a-directory"
            parent_file.write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(engine.EngineError, "review engine I/O failed"):
                engine.execute(
                    "prompt",
                    engine="command",
                    command="reviewer",
                    model=None,
                    cwd=root,
                    schema={"type": "object"},
                    raw_path=parent_file / "result.txt",
                )

    def test_output_decode_failure_is_an_engine_failure(self):
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid output")
        with (
            mock.patch.object(engine, "_execute", side_effect=error),
            self.assertRaisesRegex(engine.EngineError, "review engine I/O failed"),
        ):
            engine.execute(
                "prompt",
                engine="command",
                command="reviewer",
                model=None,
                cwd=Path("."),
                schema={"type": "object"},
                raw_path=Path("result.txt"),
            )

    def test_programmer_and_process_control_exceptions_are_not_hidden(self):
        for error in (TypeError("programmer bug"), KeyboardInterrupt(), SystemExit(3)):
            with self.subTest(error=type(error).__name__):
                with (
                    mock.patch.object(engine, "_execute", side_effect=error),
                    self.assertRaises(type(error)),
                ):
                    engine.execute(
                        "prompt",
                        engine="command",
                        command="reviewer",
                        model=None,
                        cwd=Path("."),
                        schema={"type": "object"},
                        raw_path=Path("result.txt"),
                    )


if __name__ == "__main__":
    unittest.main()
