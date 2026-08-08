"""Isolated review-engine execution, independent of CLI orchestration.

This module owns the executable/config boundary for Codex, Claude, and custom
commands: exact argument construction, the Bubblewrap namespace, subprocess
collection, Codex event evidence, and the final JSON result. It deliberately
does not own prompts, rubric policy, delivery, or the credential-output
backstop. Keeping the provider credential outside the namespace requires the
separate broker redesign; this extraction preserves the current containment.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import jsonschema

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
_ENGINE_CREDENTIAL_DIR: Path | None = None


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


def codex_arguments(
    *,
    schema_name: str,
    output_name: str,
    model: str | None,
    reasoning_effort: str | None,
) -> list[str]:
    """The exact Codex invocation for one rubric pass."""
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
    ]
    if model:
        argv.extend(["--model", model])
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


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 3600,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
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


def engine_credential_file(api_key: str) -> Path:
    """A private, 0600 credential file for the engine to read inside its namespace.

    Held for the lifetime of the process, in a directory only this user can
    enter, and never under the workspace or the engine's output directory: the
    model can read anything bound into its namespace, and an output directory is
    exactly where a prompt-injected model would try to copy a secret to.

    None of which keeps the key from the model. It cannot: the engine has to
    read this file, and the read-only sandbox the engine runs under is read-only
    about writing. Every other host credential remains outside the namespace.
    The CLI's credential-output backstop checks what the model writes for a
    plainly copied key, but it cannot stop an encoding or another channel. The
    planned broker boundary will remove the provider key from this namespace;
    it is not implemented here.
    """
    global _ENGINE_CREDENTIAL_DIR
    if _ENGINE_CREDENTIAL_DIR is None:
        _ENGINE_CREDENTIAL_DIR = Path(tempfile.mkdtemp(prefix="palomar-engine-"))
        _ENGINE_CREDENTIAL_DIR.chmod(0o700)
    path = _ENGINE_CREDENTIAL_DIR / "auth.json"
    path.write_text(json.dumps({"OPENAI_API_KEY": api_key}), encoding="utf-8")
    path.chmod(0o600)
    return path


def isolated_command(
    engine: str,
    argv: list[str],
    *,
    cwd: Path,
    output_dir: Path,
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
        # An API key, when one is configured, is written to a private file and
        # bound in as the engine's credential rather than passed with --setenv,
        # which would put it in argv where any process on the host can read it.
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key:
            auth = engine_credential_file(api_key)
        else:
            auth = host_home / ".codex" / "auth.json"
            if not auth.is_file() or auth.is_symlink():
                raise EngineError(
                    "set OPENAI_API_KEY, or sign in with `codex login`: the Codex engine "
                    "has no credential"
                )
        _bind_if_present(command, auth, "/home/reviewer/.codex/auth.json")
        command.extend(["--setenv", "CODEX_HOME", "/home/reviewer/.codex"])
        argv = [str(Path(node).resolve(strict=True)), "/engine/codex/bin/codex.js", *argv[1:]]
    elif engine == "claude":
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
    return [*command, "--", *argv]


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
        argv = codex_arguments(
            schema_name=schema_path.name,
            output_name=output_path.name,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        events = _run(
            isolated_command("codex", argv, cwd=cwd, output_dir=engine_output),
            input_text=prompt,
            timeout=7200,
        ).stdout
        (raw_path.parent / f"{raw_path.stem}.events.jsonl").write_text(
            events, encoding="utf-8"
        )
        usage = usage_accounting.codex_usage(events)
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
