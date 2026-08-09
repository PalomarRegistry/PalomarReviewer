"""Isolated review-engine execution, independent of CLI orchestration.

This module owns the executable/config boundary for Codex, Claude, and custom
commands: exact argument construction, the Bubblewrap namespace, subprocess
collection, Codex event evidence, and the final JSON result. It deliberately
does not own prompts, rubric policy, delivery, or the credential-output
backstop.

The production Codex path authenticates through `palomar_reviewer.broker`: a
loopback broker holds the real provider key outside the namespace, and the
namespace receives only a per-pass capability that is worthless once the pass
ends. There is no direct-authentication fallback, because a fallback is what an
attacker would aim for. The Claude and custom-command engines have no such
boundary: a custom command is given no provider credential at all, and Claude
still binds its own login file into the namespace, so it refuses to run unless
an operator says out loud that this run is not production.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import jsonschema

from . import broker as model_broker
from . import usage as usage_accounting


class EngineError(RuntimeError):
    """An engine configuration, containment, or execution failure."""


SYSTEM_RESOLUTION_PATHS = (
    Path("/etc/ssl/certs"),
    # NixOS keeps the certificate bundle behind absolute symlinks through
    # /etc/static. Binding /etc/ssl/certs alone leaves those links dangling
    # inside the namespace even though the final Nix store is available.
    Path("/etc/static/ssl/certs"),
    Path("/etc/pki"),
    Path("/etc/resolv.conf"),
    Path("/etc/hosts"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/gai.conf"),
    Path("/etc/host.conf"),
    Path("/etc/ld.so.cache"),
)
# The Claude engine binds a reusable provider login into the same namespace as
# the attacker-authored workspace, which is exactly what the Codex broker
# exists to stop. It has no broker of its own yet, so it is not a production
# engine, and running it has to be a decision somebody made on purpose.
UNBROKERED_CLAUDE_ENV = "PALOMAR_ALLOW_UNBROKERED_CLAUDE"


def validate_config(engine: str, command: str | None) -> None:
    """Reject an unsupported engine or a custom engine with no command."""
    if engine not in {"codex", "claude", "command"}:
        raise EngineError(f"unsupported engine: {engine}")
    if engine == "command" and not (command or "").strip():
        raise EngineError("--command is required with --engine command")


def parse_json(text: str) -> dict[str, Any]:
    """Require the complete stripped engine output to be one JSON object."""
    try:
        result = json.loads(text.strip())
    except json.JSONDecodeError:
        raise EngineError("review engine did not return a JSON object") from None
    if not isinstance(result, dict):
        raise EngineError("review engine did not return a JSON object")
    return result


CODEX_PROVIDER_ID = "palomar_broker"
# The Codex release the provider wiring below and the broker's forwarded-header
# allowlist were both derived from, and the one CI installs to run the
# integration test against a fake provider. A different release is not refused
# here, because Codex is installed outside this Python artifact; it is stated so
# that a drifting client shows up as a named difference rather than a surprise.
PINNED_CODEX_VERSION = "0.147.0"
CODEX_VERSION_LINE = f"codex-cli {PINNED_CODEX_VERSION}"
CODEX_STREAM_IDLE_MS = int((model_broker.UPSTREAM_READ_SECONDS + 60) * 1000)


def codex_version() -> str:
    """What the installed Codex says it is, as one line."""
    codex = shutil.which("codex")
    if not codex:
        raise EngineError("codex is required for the codex review engine")
    try:
        found = subprocess.run(
            [codex, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            env=sandbox_process_environment(),
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise EngineError(f"could not read the Codex version: {error}") from error
    if found.returncode:
        raise EngineError(f"codex --version failed ({found.returncode})")
    return found.stdout.strip()


def require_pinned_codex() -> None:
    """Refuse a Codex this runner's provider and header contract was not read from.

    The broker forwards the headers pinned Codex sends, serves the one route it
    calls, and enforces the request shape it makes. Those are read off a
    particular release, and the integration test proves them against that
    release. A different client is not necessarily wrong, but nothing here has
    checked it, and quietly running a security boundary against assumptions
    nobody verified is the failure this refuses. Upgrading Codex means
    rederiving the contract and moving the pin.
    """
    found = codex_version()
    if found != CODEX_VERSION_LINE:
        raise EngineError(
            f"this reviewer is written against {CODEX_VERSION_LINE} and found {found!r}; "
            "rederive the broker's request and header contract, then move "
            "PINNED_CODEX_VERSION"
        )


def codex_arguments(
    *,
    schema_name: str,
    output_name: str,
    model: str,
    reasoning_effort: str | None,
    broker_base_url: str,
) -> list[str]:
    """The exact Codex invocation for one rubric pass.

    The provider is stated here rather than in a configuration file because
    `--ignore-user-config` means Codex loads no `config.toml` at all, not even
    from the temporary `CODEX_HOME`: the trusted machine-level configuration
    has to arrive as overrides, where the submitted repository cannot reach it
    either. Nothing in this list is a credential. The per-pass capability is
    passed to the namespace out of band, so that it is not in the argv of any
    process on the host.

    The provider name matters, and not for display. Pinned Codex offers remote
    compaction, on a second endpoint `{base_url}/responses/compact`, to a
    provider it believes is OpenAI: one named exactly "OpenAI", or one whose
    base URL looks like Azure OpenAI. Anything else compacts locally through
    the ordinary `/responses` call. So a provider named for what it is keeps
    the whole pass on the one endpoint the broker serves, and a rename to
    "OpenAI" would send a long pass at a route the broker refuses.
    """
    provider = f"model_providers.{CODEX_PROVIDER_ID}"
    argv = [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        # Events on stdout, so the available turn aggregate is retained from
        # the engine rather than estimated.
        "--json",
        "--output-schema",
        f"/output/{schema_name}",
        "--output-last-message",
        f"/output/{output_name}",
        "--cd",
        "/workspace",
        "--model",
        model,
        "-c",
        f'model_provider="{CODEX_PROVIDER_ID}"',
        "-c",
        f'{provider}.name="Palomar loopback model broker"',
        "-c",
        f'{provider}.base_url="{broker_base_url}"',
        "-c",
        f'{provider}.wire_api="responses"',
        "-c",
        f'{provider}.env_key="{model_broker.CAPABILITY_ENV}"',
        # Longer than the broker's own upstream read deadline, so a provider
        # that stops sending is refused by the broker rather than abandoned and
        # retried by Codex while the broker still holds the first billable
        # request open. Codex's default for a custom provider is five minutes,
        # which is shorter than the broker waits.
        "-c",
        f"{provider}.stream_idle_timeout_ms={CODEX_STREAM_IDLE_MS}",
    ]
    if reasoning_effort:
        argv.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
    argv.append("-")
    return argv


def claude_arguments(
    *,
    schema: dict[str, Any],
    model: str | None,
    allow_network: bool,
) -> list[str]:
    """The exact Claude invocation for one rubric pass."""
    argv = [
        "claude",
        "--print",
        "--safe-mode",
        "--no-session-persistence",
        "--permission-mode",
        "auto",
        "--tools",
        "WebSearch,WebFetch" if allow_network else "",
        "--output-format",
        "text",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
    ]
    if model:
        argv.extend(["--model", model])
    return argv


def custom_arguments(command: str) -> list[str]:
    """Split a configured custom command exactly as a shell command line."""
    try:
        return shlex.split(command)
    except ValueError as error:
        raise EngineError(f"invalid --command: {error}") from error


def identity(engine: str, model: str | None, command: str | None) -> str:
    """Return the durable identity for the configured review engine."""
    validate_config(engine, command)
    if engine == "command":
        # Preserve the configured executable token, including a quoted path.
        # The namespace builder may resolve that token to a bound absolute path
        # later. Malformed shell syntax fails here rather than being recorded.
        parts = custom_arguments(command or "")
        return f"command:{parts[0]}"
    return f"{engine}:{model or 'default'}"


def sandbox_process_environment() -> dict[str, str]:
    """The environment Bubblewrap itself is started with, holding nothing.

    `--clearenv` answers what the engine process sees, which is not the same
    question. Bubblewrap leaves a reaper at PID 1 inside the namespace and that
    reaper is Bubblewrap, which never execs, so `/proc/1/environ` in there is
    whatever Bubblewrap was started with. A model that thought to read it would
    find every operator credential the reviewer holds: the upstream provider
    key, `GH_TOKEN`, the archive token. `--clearenv` cannot help, because by
    then it has already done its job on a different process.

    So the reviewer hands Bubblewrap an environment with nothing in it worth
    reading. `PATH` is kept because it names directories rather than secrets,
    and dropping it would change how an operator's own tooling resolves.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 3600,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            pass_fds=pass_fds,
            # Every command this runs is a namespace launch, and the namespace
            # can read the launcher's environment. See above.
            env=sandbox_process_environment(),
        )
    except subprocess.TimeoutExpired:
        raise EngineError(
            f"{' '.join(command[:3])} timed out after {timeout} seconds"
        ) from None
    except (OSError, UnicodeError) as error:
        raise EngineError(f"could not run {' '.join(command[:3])}: {error}") from error
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()[-5000:]
        raise EngineError(f"{' '.join(command[:3])} failed ({proc.returncode}): {detail}")
    return proc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _bind_if_present(command: list[str], source: Path, destination: str) -> None:
    if source.exists():
        command.extend(["--ro-bind", str(source), destination])


def sandbox_environment_args(values: dict[str, str]) -> bytes:
    """Bubblewrap `--setenv` arguments for values that must not appear in argv.

    Bubblewrap reads NUL-separated arguments from a file descriptor with
    `--args`, which is how the per-pass capability reaches the namespace
    environment without also reaching the process table, where every account on
    the host could read it.
    """
    parts: list[str] = []
    for name, value in values.items():
        if "\0" in name or "\0" in value:
            raise EngineError("a sandbox environment value contains a NUL byte")
        parts.extend(("--setenv", name, value))
    return b"\0".join(part.encode("utf-8") for part in parts)


def isolated_command(
    engine: str,
    argv: list[str],
    *,
    cwd: Path,
    output_dir: Path,
    secret_args_fd: int | None = None,
) -> list[str]:
    """Build a fail-closed Linux namespace with no ambient operator home."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise EngineError("bubblewrap is required to isolate untrusted editorial evidence")
    cwd = cwd.resolve(strict=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve(strict=True)
    host_home = Path.home().resolve()
    command = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        # The CLI transport must reach its model API. Per-pass browsing is
        # controlled by the engine tool list; a private netns would also cut
        # the transport and make every non-literature pass non-functional.
        "--share-net",
        "--clearenv",
        "--tmpfs",
        "/home",
        "--dir",
        "/home/reviewer",
        "--dir",
        "/home/reviewer/.codex",
        "--dir",
        "/home/reviewer/.claude",
        "--dir",
        "/engine",
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        str(cwd),
        "/workspace",
        "--bind",
        str(output_dir),
        "/output",
        "--setenv",
        "HOME",
        "/home/reviewer",
        "--setenv",
        "PATH",
        "/run/current-system/sw/bin:/usr/bin:/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "LC_ALL",
        "C.UTF-8",
        "--chdir",
        "/workspace",
    ]
    for path in (
        Path("/nix/store"),
        Path("/run/current-system/sw"),
        Path("/usr"),
        Path("/bin"),
        Path("/lib"),
        Path("/lib64"),
    ):
        _bind_if_present(command, path, str(path))
    for path in SYSTEM_RESOLUTION_PATHS:
        _bind_if_present(command, path, str(path))

    if engine == "codex":
        codex = shutil.which("codex")
        node = shutil.which("node")
        if not codex or not node:
            raise EngineError("codex and node are required for the codex review engine")
        codex_entry = Path(codex).resolve(strict=True)
        try:
            codex_root = next(parent for parent in codex_entry.parents if parent.name == "@openai") / "codex"
        except StopIteration as error:
            raise EngineError("could not locate the installed Codex package") from error
        _bind_if_present(command, codex_root, "/engine/codex")
        # No credential is bound in, and no Codex login file is reachable. The
        # only authentication in this namespace is the per-pass broker
        # capability, which arrives through `secret_args_fd`. Without that
        # channel there is nothing to authenticate with, and rather than
        # falling back to a reusable key the pass refuses to start.
        if secret_args_fd is None:
            raise EngineError(
                "the Codex engine runs only through the loopback model broker, and no "
                "per-pass capability channel was supplied"
            )
        # CODEX_HOME is an empty tmpfs directory. `--ephemeral` keeps sessions
        # out of it, and `--ignore-user-config` means no config.toml is read
        # from it, so nothing an earlier run or the host left behind applies.
        command.extend(["--setenv", "CODEX_HOME", "/home/reviewer/.codex"])
        argv = [str(Path(node).resolve(strict=True)), "/engine/codex/bin/codex.js", *argv[1:]]
    elif engine == "claude":
        if os.environ.get(UNBROKERED_CLAUDE_ENV, "").strip() != "1":
            raise EngineError(
                "the Claude engine binds a reusable provider login into the model "
                "namespace and has no broker, so it is not a production engine; set "
                f"{UNBROKERED_CLAUDE_ENV}=1 to run it anyway"
            )
        claude = shutil.which("claude")
        if not claude:
            raise EngineError("claude is required for the Claude review engine")
        _bind_if_present(command, Path(claude).resolve(strict=True), "/engine/claude")
        credentials = host_home / ".claude" / ".credentials.json"
        if not credentials.is_file() or credentials.is_symlink():
            raise EngineError("Claude authentication file is missing or symbolic")
        _bind_if_present(
            command,
            credentials,
            "/home/reviewer/.claude/.credentials.json",
        )
        current_account = host_home / ".claude" / ".current-account"
        if current_account.is_file() and not current_account.is_symlink():
            _bind_if_present(
                command,
                current_account,
                "/home/reviewer/.claude/.current-account",
            )
        argv = ["/engine/claude", *argv[1:]]
    elif engine == "command":
        executable = shutil.which(argv[0])
        if not executable:
            raise EngineError(f"custom review command is unavailable: {argv[0]}")
        resolved = Path(executable).resolve(strict=True)
        if str(resolved).startswith(("/nix/store/", "/run/current-system/sw/", "/usr/", "/bin/")):
            argv[0] = str(resolved)
        else:
            root = resolved.parent.parent if resolved.parent.name == "bin" else resolved.parent
            _bind_if_present(command, root, "/engine/custom-root")
            library_dir = root / "lib"
            if library_dir.is_dir():
                command.extend(["--setenv", "LD_LIBRARY_PATH", "/engine/custom-root/lib"])
            argv[0] = f"/engine/custom-root/{resolved.relative_to(root)}"
    else:
        raise EngineError(f"unsupported isolated review engine: {engine}")
    if secret_args_fd is not None:
        # Last, so that the `--clearenv` above has already run when Bubblewrap
        # reads these and sets them.
        command.extend(["--args", str(secret_args_fd)])
    return [*command, "--", *argv]


def _run_brokered_codex(
    prompt: str,
    *,
    model: str | None,
    reasoning_effort: str | None,
    cwd: Path,
    engine_output: Path,
    schema_name: str,
    output_name: str,
) -> tuple[str, dict[str, Any]]:
    """One Codex pass, authenticated only by a broker that outlives nothing.

    The broker starts before Codex and is shut down in a `finally`, so a
    timeout, a cancellation, a schema failure, or an exception all invalidate
    the capability on the way out. Anything that stops the broker from starting
    stops the pass: there is no unbrokered path to fall back to.
    """
    if not (model or "").strip():
        raise EngineError(
            "the Codex engine requires --model, because the broker enforces the model "
            "it was configured for"
        )
    require_pinned_codex()
    policy = model_broker.BrokerPolicy(
        model=model or "",
        reasoning_effort=reasoning_effort,
        upstream_origin=model_broker.configured_upstream_origin(),
    )
    upstream_key = os.environ.get(model_broker.UPSTREAM_KEY_ENV, "").strip()
    try:
        with model_broker.started_broker(policy=policy, upstream_key=upstream_key) as broker:
            argv = codex_arguments(
                schema_name=schema_name,
                output_name=output_name,
                model=model or "",
                reasoning_effort=reasoning_effort,
                broker_base_url=broker.base_url,
            )
            payload = sandbox_environment_args({model_broker.CAPABILITY_ENV: broker.capability})
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, payload)
                os.close(write_fd)
                write_fd = -1
                events = _run(
                    isolated_command(
                        "codex",
                        argv,
                        cwd=cwd,
                        output_dir=engine_output,
                        secret_args_fd=read_fd,
                    ),
                    input_text=prompt,
                    timeout=7200,
                    pass_fds=(read_fd,),
                ).stdout
            finally:
                if write_fd != -1:
                    os.close(write_fd)
                os.close(read_fd)
    except model_broker.BrokerError as error:
        raise EngineError(f"the loopback model broker failed: {error}") from error
    # A pass that produced output but lost its boundary partway is not a pass
    # that succeeded. The summary is the broker saying it served every request
    # this review made and stopped cleanly; without it there is no account of
    # what the ceilings saw, and the result is refused rather than delivered.
    if broker.summary is None or broker.exit_status != 0:
        raise EngineError(
            "the loopback model broker did not shut down cleanly, so this pass has no "
            "account of what it served"
        )
    return events, broker.summary


def _execute(
    prompt: str,
    *,
    engine: str,
    command: str | None,
    model: str | None,
    cwd: Path,
    schema: dict[str, Any],
    raw_path: Path,
    reasoning_effort: str | None = None,
    allow_network: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Implement one engine pass behind the public operational-error boundary."""
    validate_config(engine, command)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_path.is_symlink() or (raw_path.exists() and not raw_path.is_file()):
        raise EngineError("review raw-output path is not a regular file")
    usage = usage_accounting.unavailable_usage(engine)
    engine_output = raw_path.parent / f".{raw_path.name}.engine-output"
    if engine_output.is_symlink() or (engine_output.exists() and not engine_output.is_dir()):
        raise EngineError("review engine-output path is not a real directory")
    if engine_output.is_dir():
        shutil.rmtree(engine_output)
    engine_output.mkdir()
    if engine == "codex":
        schema_path = engine_output / "schema.json"
        output_path = engine_output / "message.txt"
        _write_json(schema_path, schema)
        events, broker_summary = _run_brokered_codex(
            prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            cwd=cwd,
            engine_output=engine_output,
            schema_name=schema_path.name,
            output_name=output_path.name,
        )
        (raw_path.parent / f"{raw_path.stem}.events.jsonl").write_text(
            events, encoding="utf-8"
        )
        usage = usage_accounting.codex_usage(events)
        # Alongside the engine's own turn aggregate, never instead of it: the
        # durable spend evidence stays what Codex reported, and this records
        # what the boundary in front of it saw, including anything it refused.
        usage["broker"] = broker_summary
        if output_path.is_symlink() or not output_path.is_file():
            raise EngineError("Codex did not create a regular final-message file")
        text = output_path.read_text(encoding="utf-8")
    elif engine == "claude":
        argv = claude_arguments(schema=schema, model=model, allow_network=allow_network)
        text = _run(
            isolated_command("claude", argv, cwd=cwd, output_dir=engine_output),
            input_text=prompt,
            timeout=7200,
        ).stdout
    elif engine == "command":
        text = _run(
            isolated_command(
                "command",
                custom_arguments(command or ""),
                cwd=cwd,
                output_dir=engine_output,
            ),
            input_text=prompt,
            timeout=7200,
        ).stdout
    else:
        # Keep this defensive branch aligned with validate_config if another
        # engine is ever admitted there without an implementation here.
        raise EngineError(f"unsupported engine: {engine}")
    if raw_path.is_symlink():
        raise EngineError("review raw-output path became symbolic")
    raw_path.write_text(text, encoding="utf-8")
    result = parse_json(text)
    try:
        jsonschema.validate(result, schema)
    except jsonschema.ValidationError as error:
        raise EngineError(
            "review engine result did not match the required schema "
            f"({error.validator})"
        ) from error
    except jsonschema.SchemaError as error:
        raise EngineError("review result schema is invalid") from error
    return result, usage


def execute(
    prompt: str,
    *,
    engine: str,
    command: str | None,
    model: str | None,
    cwd: Path,
    schema: dict[str, Any],
    raw_path: Path,
    reasoning_effort: str | None = None,
    allow_network: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one engine pass, exposing expected operational failures as EngineError."""
    try:
        return _execute(
            prompt,
            engine=engine,
            command=command,
            model=model,
            cwd=cwd,
            schema=schema,
            raw_path=raw_path,
            reasoning_effort=reasoning_effort,
            allow_network=allow_network,
        )
    except (OSError, UnicodeError) as error:
        raise EngineError(f"review engine I/O failed: {error}") from error
