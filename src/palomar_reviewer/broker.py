"""An ephemeral loopback Responses broker that keeps the provider key out of the sandbox.

The reviewer runs a model over an attacker-authored repository. Whatever else
that namespace contains, it must not contain a credential that is still worth
something after the job: a prompt injection that talks the model into printing
its own API key would otherwise turn one review into a reusable provider
credential, and no output check can stop it encoding the key or posting it
somewhere.

So the key never goes in. This module is the trusted side of that boundary. A
short-lived child process holds the real upstream key, binds one automatically
allocated port on `127.0.0.1`, and accepts exactly the one Responses API call
pinned Codex makes. The sandbox is given a random per-pass capability instead.
Stolen, that capability buys an attacker nothing off the runner: the listener
is loopback-only, and the process holding it is gone when the pass ends.

The contract, exactly:

* One accepted request: `POST /v1/responses`, no query string. Every other
  method, path, or query is refused without reaching the provider.
* Authentication is `Authorization: Bearer <capability>`, exactly one such
  header, compared in constant time. The broker replaces it with the real
  upstream authorization before forwarding; the capability never leaves the
  runner and the upstream key never enters the namespace.
* The request body must be one JSON object naming the configured model, and
  the configured reasoning effort when one is configured. `stream` must be
  true, which is what pinned Codex sends and what the ceilings below assume.
* Request headers are forwarded from a fixed allowlist of what pinned Codex
  sends. Anything else a process in the namespace invents is dropped rather
  than handed to the provider.
* Responses are streamed through in bounded chunks. The broker reads the SSE
  `response.completed` usage as it passes and holds nothing else about it.
* Ceilings are per pass, because one broker serves one engine pass: requests,
  cumulative tokens, estimated spend, request bytes, response bytes and
  concurrency. Reaching one refuses further requests rather than failing the
  pass silently, and the refusal is recorded in the usage summary.
* Nothing here logs, returns, or records either credential.

What this does not do: it is not general network isolation. The namespace still
shares the runner's network because the Codex transport has to reach this
listener, and a custom provider other than the pinned Codex path is out of
scope. It also does nothing for the Claude engine, whose credential is a
different provider's and needs its own broker.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import queue
import secrets
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import usage as usage_accounting

# Read by trusted reviewer code only. The production workflow may keep storing
# the GitHub secret under any name it likes, as long as it arrives here: an
# `OPENAI_API_KEY` in the reviewer's own environment is deliberately not used,
# so that nothing can quietly authenticate Codex directly and skip the broker.
UPSTREAM_KEY_ENV = "PALOMAR_OPENAI_UPSTREAM_KEY"
# The one environment variable the model namespace receives. It holds the
# per-pass capability and nothing reusable.
CAPABILITY_ENV = "PALOMAR_MODEL_BROKER_TOKEN"
# A test seam, and the only way the upstream origin can differ from the
# provider. It is read from the trusted reviewer environment, never from the
# namespace, and is still validated below: https, or loopback for a fake
# upstream in the integration test.
UPSTREAM_ORIGIN_ENV = "PALOMAR_BROKER_UPSTREAM_ORIGIN"

DEFAULT_UPSTREAM_ORIGIN = "https://api.openai.com"
RESPONSES_PATH = "/v1/responses"
LOOPBACK_HOST = "127.0.0.1"

# One Codex turn is many provider requests: every tool call comes back for
# another one. An ordinary pass makes tens of them, and the engine timeout of
# two hours with reasoning effort high cannot fit many hundreds. This bounds a
# loop, not a budget.
MAX_REQUESTS = 250
# The model's context window is 272,000 input tokens, and the reviewer's
# rendered prompt is capped at 300,000 bytes well below it. Eight mebibytes of
# request JSON is several times the largest conversation that window can hold.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
# A streamed answer including reasoning summaries and tool arguments, with the
# same kind of headroom.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Cumulative across the pass, so the whole re-sent conversation counts again on
# every request. The arithmetic ceiling: 250 requests of a full context window
# would be 68,000,000 tokens, and this refuses well before that.
MAX_TOTAL_TOKENS = 20_000_000
# The ceiling that actually binds, in USD at current list prices. It is a
# runaway backstop and not a budget: an ordinary pass is orders of magnitude
# below it, and the intended cost controls remain `--max-reviews` and a
# provider-side spend limit.
MAX_SPEND_USD = 30.0
# Pinned Codex issues one request at a time per turn. Four leaves room for the
# client's own retry overlap without letting a namespace process open an
# unbounded number of provider connections.
MAX_CONCURRENT_REQUESTS = 4
UPSTREAM_CONNECT_SECONDS = 30.0
# A reasoning model can think for a long time before the first streamed event.
UPSTREAM_READ_SECONDS = 600.0
STREAM_CHUNK_BYTES = 64 * 1024
# An SSE event that never ends its line must not grow the usage reader's
# holdover buffer without bound.
MAX_STREAM_LINE_BYTES = 1024 * 1024
BROKER_START_SECONDS = 30.0
BROKER_STOP_SECONDS = 30.0

# Exactly what pinned Codex 0.147 sends. Everything else, including anything a
# process inside the namespace invents, is dropped before the provider sees it.
FORWARDED_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "originator",
    "user-agent",
    "session-id",
    "thread-id",
    "x-client-request-id",
    "x-codex-beta-features",
    "x-codex-window-id",
    "x-codex-turn-metadata",
    "x-openai-internal-codex-responses-lite",
)
MAX_FORWARDED_HEADER_BYTES = 16 * 1024
# Returned to the client so a streamed answer is readable, and so a refused or
# rate-limited upstream keeps the timing the client needs. No upstream header
# outside this list is repeated.
RETURNED_RESPONSE_HEADERS = ("content-type", "retry-after")

# The environment the broker child is started with. It holds no GitHub token,
# no archive token, no state-write authority, and no provider credential: the
# upstream key arrives on a pipe instead, so a broker that is somehow made to
# print its environment prints nothing worth having.
BROKER_ENVIRONMENT_NAMES = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


class BrokerError(RuntimeError):
    """A broker configuration, startup, or shutdown failure."""


@dataclass(frozen=True)
class BrokerPolicy:
    """Everything the broker enforces, decided by trusted code before it starts."""

    model: str
    reasoning_effort: str | None = None
    upstream_origin: str = DEFAULT_UPSTREAM_ORIGIN
    max_requests: int = MAX_REQUESTS
    max_total_tokens: int = MAX_TOTAL_TOKENS
    max_spend_usd: float = MAX_SPEND_USD
    max_request_bytes: int = MAX_REQUEST_BYTES
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS
    connect_seconds: float = UPSTREAM_CONNECT_SECONDS
    read_seconds: float = UPSTREAM_READ_SECONDS

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise BrokerError("the broker enforces one model and was given none")
        upstream_origin(self.upstream_origin)

    def as_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "upstream_origin": self.upstream_origin,
            "max_requests": self.max_requests,
            "max_total_tokens": self.max_total_tokens,
            "max_spend_usd": self.max_spend_usd,
            "max_request_bytes": self.max_request_bytes,
            "max_response_bytes": self.max_response_bytes,
            "max_concurrent_requests": self.max_concurrent_requests,
            "connect_seconds": self.connect_seconds,
            "read_seconds": self.read_seconds,
        }

    @classmethod
    def from_json(cls, value: Any) -> BrokerPolicy:
        if not isinstance(value, dict):
            raise BrokerError("broker policy is not an object")
        known = {field: value[field] for field in cls.__dataclass_fields__ if field in value}
        return cls(**known)


def upstream_origin(origin: str) -> tuple[bool, str, int]:
    """Split a permitted upstream origin into transport, host and port.

    Only two shapes are permitted: an https origin, which is the provider, and
    an http origin on the loopback address, which is the fake upstream the
    integration test runs so that proving the wire protocol costs no model
    tokens. Anything else, including a plaintext origin on a routable address,
    is a configuration error rather than something to warn about.
    """
    text = origin.strip()
    for scheme, secure, default_port in (("https://", True, 443), ("http://", False, 80)):
        if not text.startswith(scheme):
            continue
        authority = text[len(scheme):].rstrip("/")
        if "/" in authority or not authority:
            break
        host, _, port_text = authority.rpartition(":")
        if not host:
            host, port_text = authority, ""
        if not secure and host not in {LOOPBACK_HOST, "localhost"}:
            raise BrokerError(f"refusing a plaintext upstream origin that is not loopback: {origin}")
        try:
            port = int(port_text) if port_text else default_port
        except ValueError:
            break
        if not 1 <= port <= 65535:
            break
        return secure, host, port
    raise BrokerError(f"unsupported upstream origin: {origin}")


def broker_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """The minimal environment the broker child runs with.

    Nothing is inherited except what an outbound TLS request needs. The
    package location is stated explicitly rather than inherited so that a
    source checkout and an installed wheel both start the same child.
    """
    environment = {
        name: value
        for name in BROKER_ENVIRONMENT_NAMES
        if (value := (source if source is not None else os.environ).get(name))
    }
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return environment


class Ledger:
    """Per-pass counters, and the refusals that keep them from being exceeded."""

    def __init__(self, policy: BrokerPolicy) -> None:
        self._policy = policy
        self._lock = threading.Lock()
        self._requests = 0
        self._forwarded = 0
        self._active = 0
        self._input_tokens = 0
        self._cached_input_tokens = 0
        self._output_tokens = 0
        self._reasoning_output_tokens = 0
        self._usd = 0.0
        self._unpriced = 0
        self._refusals: dict[str, int] = {}

    def refuse(self, reason: str) -> None:
        with self._lock:
            self._refusals[reason] = self._refusals.get(reason, 0) + 1

    def begin(self) -> str | None:
        """Admit one request, or say why the pass may not make another."""
        with self._lock:
            if self._requests >= self._policy.max_requests:
                return "request ceiling reached"
            if self._input_tokens + self._output_tokens >= self._policy.max_total_tokens:
                return "token ceiling reached"
            if self._usd >= self._policy.max_spend_usd:
                return "spend ceiling reached"
            if self._active >= self._policy.max_concurrent_requests:
                return "too many concurrent requests"
            self._requests += 1
            self._active += 1
            return None

    def end(self, *, forwarded: bool) -> None:
        with self._lock:
            self._active -= 1
            if forwarded:
                self._forwarded += 1

    def record_usage(self, usage: Any) -> None:
        """Take the provider's own token counts, and price them if they are usable."""
        counts = usage_accounting.responses_usage_tokens(usage)
        with self._lock:
            if counts is None:
                self._unpriced += 1
                return
            self._input_tokens += counts["input_tokens"]
            self._cached_input_tokens += counts["cached_input_tokens"]
            self._output_tokens += counts["output_tokens"]
            self._reasoning_output_tokens += counts["reasoning_output_tokens"]
            self._usd += usage_accounting.responses_usage_cost(counts)

    def summary(self) -> dict[str, Any]:
        """The structured record handed back to the reviewer's accounting path."""
        with self._lock:
            return {
                "schema_version": 1,
                "model": self._policy.model,
                "requests": self._requests,
                "forwarded_requests": self._forwarded,
                "refusals": dict(sorted(self._refusals.items())),
                "input_tokens": self._input_tokens,
                "cached_input_tokens": self._cached_input_tokens,
                "output_tokens": self._output_tokens,
                "reasoning_output_tokens": self._reasoning_output_tokens,
                "responses_without_usage": self._unpriced,
                "estimated_usd": round(self._usd, 6),
                "limits": {
                    "max_requests": self._policy.max_requests,
                    "max_total_tokens": self._policy.max_total_tokens,
                    "max_spend_usd": self._policy.max_spend_usd,
                },
            }


class _UsageReader:
    """Reads completed-response usage out of the SSE bytes going past.

    Only the `data:` lines are looked at, and only for the terminal event. A
    partial line is held over to the next chunk, up to a bound: an upstream
    that never emits a newline must not be able to grow this buffer. Nothing
    else about the stream is kept, and the stream is not delayed by this: it
    has already been written to the client by the time a line completes.
    """

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._pending = bytearray()
        self.seen = False

    def feed(self, chunk: bytes) -> None:
        pending = self._pending
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                if len(pending) > MAX_STREAM_LINE_BYTES:
                    del pending[:]
                return
            line = bytes(pending[:newline])
            del pending[:newline + 1]
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[len(b"data:"):].strip() or b"null")
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "response.completed":
                continue
            response = event.get("response")
            if isinstance(response, dict):
                self.seen = True
                self._ledger.record_usage(response.get("usage"))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # The client is inside the sandbox. It learns the port it was told to use
    # and nothing about what is listening on it.
    server_version = "palomar-broker"
    sys_version = ""
    # Set by the server factory below, which is the only thing that builds one.
    policy: BrokerPolicy
    capability: str
    upstream_authorization: str
    ledger: Ledger
    scrub: Callable[[str], str]

    def do_POST(self) -> None:  # noqa: N802 - the BaseHTTPRequestHandler contract
        self._handle_post()

    def do_GET(self) -> None:  # noqa: N802
        self._refuse(405, "only POST is accepted", "unexpected method")

    def do_PUT(self) -> None:  # noqa: N802
        self._refuse(405, "only POST is accepted", "unexpected method")

    def do_DELETE(self) -> None:  # noqa: N802
        self._refuse(405, "only POST is accepted", "unexpected method")

    def do_HEAD(self) -> None:  # noqa: N802
        self._refuse(405, "only POST is accepted", "unexpected method")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._refuse(405, "only POST is accepted", "unexpected method")

    def log_message(self, format: str, *args: Any) -> None:
        # The default access log writes the request line to stderr. Nothing
        # here is a credential today, and a per-request line in the operator's
        # run log is noise; refusals below are what is worth saying.
        return

    def log_error(self, format: str, *args: Any) -> None:
        return

    def _note(self, message: str) -> None:
        print(f"palomar-broker: {self.scrub(message)}", file=sys.stderr, flush=True)

    def _refuse(self, status: int, message: str, reason: str) -> None:
        self.ledger.refuse(reason)
        self._note(f"refused: {reason}")
        self._send_json(status, {"error": {"type": "palomar_broker_refusal", "message": message}})

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            # The client can disappear mid-refusal; that is its business.
            return

    def _authenticated(self) -> bool:
        offered = self.headers.get_all("Authorization") or []
        if len(offered) != 1:
            return False
        expected = f"Bearer {self.capability}"
        return secrets.compare_digest(offered[0].strip(), expected)

    def _drain(self, length: int) -> None:
        """Read a refused body so the connection can be closed cleanly."""
        remaining = min(length, self.policy.max_request_bytes)
        while remaining > 0:
            chunk = self.rfile.read(min(STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _handle_post(self) -> None:
        if self.headers.get("transfer-encoding"):
            self._refuse(411, "a content length is required", "chunked request")
            return
        try:
            length = int(self.headers.get("content-length", ""))
        except ValueError:
            self._refuse(411, "a content length is required", "no content length")
            return
        if length < 0 or length > self.policy.max_request_bytes:
            self._refuse(413, "the request body is too large", "oversized request body")
            return
        if self.path != RESPONSES_PATH:
            self._drain(length)
            self._refuse(404, f"only {RESPONSES_PATH} is accepted", "unexpected path")
            return
        if not self._authenticated():
            self._drain(length)
            self._refuse(401, "the job capability is missing or wrong", "bad capability")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._refuse(400, "the request body was truncated", "truncated request body")
            return
        problem = self._policy_problem(body)
        if problem is not None:
            message, reason = problem
            self._refuse(403, message, reason)
            return
        blocked = self.ledger.begin()
        if blocked is not None:
            self._refuse(403, f"this review pass is bounded: {blocked}", blocked)
            return
        forwarded = False
        try:
            forwarded = self._forward(body)
        finally:
            self.ledger.end(forwarded=forwarded)

    def _policy_problem(self, body: bytes) -> tuple[str, str] | None:
        try:
            request = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "the request body is not JSON", "unparsable request body"
        if not isinstance(request, dict):
            return "the request body is not a JSON object", "unparsable request body"
        if request.get("model") != self.policy.model:
            # The requested model is not repeated: it is namespace-authored text.
            return f"this broker serves only {self.policy.model}", "unexpected model"
        if request.get("stream") is not True:
            return "this broker serves only streamed responses", "unexpected stream mode"
        effort = self.policy.reasoning_effort
        if effort is not None:
            reasoning = request.get("reasoning")
            offered = reasoning.get("effort") if isinstance(reasoning, dict) else None
            if offered is not None and offered != effort:
                return f"this broker serves only reasoning effort {effort}", "unexpected reasoning effort"
        return None

    def _upstream_headers(self) -> list[tuple[str, str]]:
        headers = [("authorization", self.upstream_authorization)]
        for name in FORWARDED_REQUEST_HEADERS:
            value = self.headers.get(name)
            if value is not None and len(value) <= MAX_FORWARDED_HEADER_BYTES:
                headers.append((name, value))
        return headers

    def _forward(self, body: bytes) -> bool:
        secure, host, port = upstream_origin(self.policy.upstream_origin)
        connection: http.client.HTTPConnection
        try:
            if secure:
                connection = http.client.HTTPSConnection(
                    host, port, timeout=self.policy.connect_seconds, context=ssl.create_default_context()
                )
            else:
                connection = http.client.HTTPConnection(host, port, timeout=self.policy.connect_seconds)
            connection.connect()
            connection.sock.settimeout(self.policy.read_seconds)
            connection.putrequest("POST", RESPONSES_PATH, skip_host=True, skip_accept_encoding=True)
            connection.putheader("host", host if port in (80, 443) else f"{host}:{port}")
            for name, value in self._upstream_headers():
                connection.putheader(name, value)
            connection.putheader("content-length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
        except TimeoutError:
            self._refuse(504, "the model provider did not respond in time", "upstream timeout")
            return False
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            # The exception text is scrubbed and kept general: it can quote
            # request material, and the client is untrusted.
            self._note(f"upstream transport failure: {type(error).__name__}")
            self._refuse(502, "the model provider could not be reached", "upstream transport failure")
            return False
        try:
            if 300 <= response.status < 400:
                self._refuse(502, "the model provider redirected", "upstream redirect")
                return False
            return self._stream(response)
        finally:
            with contextlib.suppress(OSError, http.client.HTTPException):
                connection.close()

    def _stream(self, response: http.client.HTTPResponse) -> bool:
        """Copy the upstream answer through in bounded chunks.

        Nothing is buffered whole: the response can be a long stream of
        reasoning and tool events, and the client is waiting on it. Only the
        completed-response usage is taken out of the bytes as they pass.
        """
        self.close_connection = True
        try:
            self.send_response(response.status)
            for name in RETURNED_RESPONSE_HEADERS:
                value = response.getheader(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header("transfer-encoding", "chunked")
            self.send_header("connection", "close")
            self.end_headers()
        except OSError:
            return False
        reader = _UsageReader(self.ledger)
        total = 0
        while True:
            try:
                # `read1`, not `read`: a buffered read of a fixed size waits
                # for that many bytes, and an event stream is exactly the case
                # where the next 64 kibibytes may be minutes away.
                chunk = response.read1(STREAM_CHUNK_BYTES)
            except TimeoutError:
                self.ledger.refuse("upstream timeout")
                self._note("refused: upstream stalled mid-stream")
                return False
            except (OSError, http.client.HTTPException):
                self.ledger.refuse("upstream disconnect")
                self._note("refused: upstream disconnected mid-stream")
                return False
            if not chunk:
                break
            total += len(chunk)
            if total > self.policy.max_response_bytes:
                self.ledger.refuse("oversized response")
                self._note("refused: oversized response")
                return False
            if response.status == 200:
                reader.feed(chunk)
            try:
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            except OSError:
                # The sandboxed client hung up. Stop reading rather than
                # paying the provider to finish a stream nobody will read.
                return False
        with contextlib.suppress(OSError):
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        if response.length:
            # A length-delimited answer that stopped early. The client has
            # already been given what arrived, and the pass will fail on it;
            # what matters here is that the record says so.
            self.ledger.refuse("upstream truncated")
            self._note("refused: upstream truncated the response")
            return False
        if response.status == 200 and not reader.seen:
            # A completed stream that reported nothing. It is counted, because
            # a provider request whose cost is unknown is exactly what the
            # ceilings below cannot see.
            self.ledger.record_usage(None)
        return response.status == 200


def build_server(
    *,
    policy: BrokerPolicy,
    capability: str,
    upstream_key: str,
    host: str = LOOPBACK_HOST,
) -> tuple[ThreadingHTTPServer, Ledger]:
    """A bound, not yet serving, loopback broker and its ledger."""
    if host != LOOPBACK_HOST:
        raise BrokerError(f"the broker binds {LOOPBACK_HOST} only, not {host}")
    if not capability.strip() or not upstream_key.strip():
        raise BrokerError("the broker needs both a job capability and an upstream key")
    ledger = Ledger(policy)
    secrets_to_scrub = (upstream_key, capability)

    def scrub(message: str) -> str:
        for secret in secrets_to_scrub:
            message = message.replace(secret, "[redacted]")
        return message

    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "policy": policy,
            "capability": capability,
            "upstream_authorization": f"Bearer {upstream_key}",
            "ledger": ledger,
            "scrub": staticmethod(scrub),
        },
    )
    server = ThreadingHTTPServer((host, 0), handler)
    server.daemon_threads = True
    return server, ledger


def serve(stdin: Any, stdout: Any, stderr: Any) -> int:
    """Run one broker until the trusted parent closes the control pipe.

    The whole configuration, including the upstream key, arrives as one JSON
    line on the inherited pipe: not in argv, where any process on the host
    could read it, and not in the environment, which a child would inherit.
    """
    line = stdin.readline()
    if not line.strip():
        print("palomar-broker: no configuration was supplied", file=stderr, flush=True)
        return 2
    try:
        config = json.loads(line)
        policy = BrokerPolicy.from_json(config.get("policy"))
        capability = str(config["capability"])
        upstream_key = str(config["upstream_key"])
        server, ledger = build_server(policy=policy, capability=capability, upstream_key=upstream_key)
    except (
        json.JSONDecodeError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        BrokerError,
        OSError,
    ) as error:
        # BrokerError text is authored here and quotes no credential; the rest
        # are shapes of a malformed control line, which the parent wrote.
        print(f"palomar-broker: refusing to start: {error}", file=stderr, flush=True)
        return 2
    worker = threading.Thread(target=server.serve_forever, name="palomar-broker", daemon=True)
    worker.start()
    try:
        print(json.dumps({"port": server.server_address[1]}), file=stdout, flush=True)
        # Any further line, or the pipe closing, ends the pass. The parent
        # closes it in a `finally`, including on timeout and cancellation.
        stdin.readline()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=BROKER_STOP_SECONDS)
        print(json.dumps({"summary": ledger.summary()}), file=stdout, flush=True)
    return 0


class BrokerProcess:
    """The trusted-side handle: a child holding the key, and where to reach it."""

    def __init__(self, *, policy: BrokerPolicy, upstream_key: str) -> None:
        if not upstream_key.strip():
            raise BrokerError(
                f"set {UPSTREAM_KEY_ENV}: the model broker has no upstream credential"
            )
        self._policy = policy
        self._upstream_key = upstream_key
        # A fresh capability for every pass, from the system CSPRNG. It is
        # worthless once this process is gone, and worthless off the runner
        # while it is not.
        self.capability = secrets.token_urlsafe(32)
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._port: int | None = None
        self.summary: dict[str, Any] | None = None

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise BrokerError("the model broker is not running")
        return f"http://{LOOPBACK_HOST}:{self._port}/v1"

    @property
    def pid(self) -> int:
        if self._process is None:
            raise BrokerError("the model broker is not running")
        return self._process.pid

    def start(self) -> None:
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "palomar_reviewer.broker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                env=broker_environment(),
                close_fds=True,
            )
        except OSError as error:
            raise BrokerError(f"could not start the model broker: {error}") from error
        self._process = process
        threading.Thread(
            target=self._read_lines, args=(process,), name="palomar-broker-reader", daemon=True
        ).start()
        try:
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(
                    {
                        "policy": self._policy.as_json(),
                        "capability": self.capability,
                        "upstream_key": self._upstream_key,
                    }
                )
                + "\n"
            )
            process.stdin.flush()
        except OSError as error:
            self.close()
            raise BrokerError(f"could not configure the model broker: {error}") from error
        started = self._next_line(BROKER_START_SECONDS)
        port = started.get("port") if isinstance(started, dict) else None
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            self.close()
            raise BrokerError("the model broker did not report a loopback port")
        self._port = port

    def close(self) -> dict[str, Any] | None:
        """Stop the broker and invalidate the capability, whatever happened."""
        process = self._process
        if process is None:
            return self.summary
        self._process = None
        self._port = None
        with contextlib.suppress(OSError):
            if process.stdin is not None:
                process.stdin.close()
        try:
            reported = self._next_line(BROKER_STOP_SECONDS)
        except BrokerError:
            reported = {}
        summary = reported.get("summary") if isinstance(reported, dict) else None
        if isinstance(summary, dict):
            self.summary = summary
        try:
            process.wait(timeout=BROKER_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=BROKER_STOP_SECONDS)
        with contextlib.suppress(OSError):
            if process.stdout is not None:
                process.stdout.close()
        return self.summary

    def _read_lines(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put("")

    def _next_line(self, seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerError("the model broker did not answer in time")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise BrokerError("the model broker did not answer in time") from None
            if not line.strip():
                raise BrokerError("the model broker stopped before answering")
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value


@contextlib.contextmanager
def started_broker(*, policy: BrokerPolicy, upstream_key: str) -> Any:
    """A running broker for exactly one engine pass, always shut down."""
    broker = BrokerProcess(policy=policy, upstream_key=upstream_key)
    broker.start()
    try:
        yield broker
    finally:
        broker.close()


def configured_upstream_origin(environment: dict[str, str] | None = None) -> str:
    """The provider, unless trusted configuration names a permitted alternative."""
    source = os.environ if environment is None else environment
    origin = (source.get(UPSTREAM_ORIGIN_ENV) or "").strip()
    if not origin:
        return DEFAULT_UPSTREAM_ORIGIN
    upstream_origin(origin)
    return origin


def main() -> int:
    return serve(sys.stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
