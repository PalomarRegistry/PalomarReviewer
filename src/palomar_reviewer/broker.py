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
* The request body must be one JSON object of the shape pinned Codex sends: the
  configured model, the configured reasoning effort exactly, `stream` true,
  `store` false, and no background, continued, priority or provider-hosted-tool
  request, because those are the ones whose cost or reach nothing here could
  bound. `store` is required to be there and to be `false`, not merely not
  `true`: omitting it asks the provider for its default, and that default is to
  keep the response for thirty days.
* Request headers are forwarded from a fixed allowlist of what pinned Codex
  sends, and response headers from what it reads. Anything else a process in
  the namespace invents is dropped rather than handed to the provider, and
  nothing about the provider account is repeated back into the namespace.
* Responses are streamed through in bounded chunks. The broker reads the usage
  from the SSE terminal event as it passes and holds nothing else about it. A
  request that ends without one is charged a standing estimate.
* Ceilings are per pass, because one broker serves one engine pass: requests,
  cumulative tokens, estimated spend, request bytes, response bytes,
  concurrency and open connections. Reaching one refuses further requests
  rather than failing the pass silently, and the refusal is recorded in the
  usage summary.
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
import socket
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
# The reviewer's rendered prompt is capped at 300,000 bytes, and a whole
# conversation at the 272,000-token price threshold is on the order of one
# mebibyte of request JSON. This is not a statement about the model's context
# window, which is larger: it is the point past which a body is not a
# conversation any more.
MAX_REQUEST_BYTES = 16 * 1024 * 1024
# A streamed answer including reasoning summaries and tool arguments, with the
# same kind of headroom.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Cumulative across the pass, so the whole re-sent conversation counts again on
# every request. An arithmetic backstop under the spend ceiling below.
MAX_TOTAL_TOKENS = 20_000_000
# The ceiling that actually binds, in USD estimated at current list prices. It
# is a runaway backstop and not a budget: an ordinary pass is orders of
# magnitude below it, and the intended cost controls remain `--max-reviews` and
# a provider-side spend limit, which is the only hard guarantee. Four requests
# may be in flight when the last of them crosses this, so the true overshoot is
# the cost of the concurrency ceiling and not zero.
MAX_SPEND_USD = 30.0
# Pinned Codex issues one request at a time per turn. Four leaves room for the
# client's own retry overlap without letting a namespace process open an
# unbounded number of provider connections.
MAX_CONCURRENT_REQUESTS = 4
# Connections, not requests: a process in the namespace that opens sockets and
# then says nothing must not be able to hold threads open indefinitely. Well
# above what the client's connection pool needs.
MAX_CONNECTIONS = 32
UPSTREAM_CONNECT_SECONDS = 30.0
# Applies to one read or write on a client connection, not to a whole request.
CLIENT_SOCKET_SECONDS = 300.0
# A reasoning model can think for a long time before the first streamed event.
# Pinned Codex is configured with a longer idle timeout than this, so that a
# stalled provider is refused here rather than retried from there while this
# still holds the first billable request open.
UPSTREAM_READ_SECONDS = 600.0
STREAM_CHUNK_BYTES = 64 * 1024
# How much of a refused request's body is read before the connection closes.
DRAIN_LIMIT_BYTES = 64 * 1024
# An SSE event that never ends its line must not grow the usage reader's
# holdover buffer without bound.
MAX_STREAM_LINE_BYTES = 1024 * 1024
BROKER_START_SECONDS = 30.0
BROKER_STOP_SECONDS = 30.0
# What a forwarded request is charged when the provider never said what it
# cost. Deliberately not zero and deliberately not free: a hundred of these
# reach the spend ceiling, so a pass that hides its usage runs out sooner than
# one that reports it, which is the right way round.
UNMEASURED_REQUEST_TOKENS = {
    "input_tokens": 50_000,
    "cached_input_tokens": 0,
    "output_tokens": 4_000,
    "reasoning_output_tokens": 0,
}
# The events that end a response. The provider reports usage on all three, and
# a pass that stops at the incomplete or failed ones is still a pass that spent
# what those requests cost.
TERMINAL_STREAM_EVENTS = frozenset(
    {"response.completed", "response.incomplete", "response.failed"}
)

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
    # Sticky routing state the provider issues on the first response of a turn
    # and pinned Codex replays on the rest of it. Dropping it does not fail a
    # request, it just sends the later ones of a turn somewhere else, which is
    # how a turn loses its prompt cache and costs several times what it should.
    "x-codex-turn-state",
)
MAX_FORWARDED_HEADER_BYTES = 16 * 1024
# Tools the client runs itself. A hosted tool would have the provider act on
# the internet for whoever asked, which is not something this reviewer's policy
# says its passes can do.
CLIENT_RUN_TOOL_TYPES = frozenset({"function", "custom"})
# Request fields pinned Codex never sends, and that would take the request
# outside what this broker can bound. `background` would have the provider keep
# working after the connection closes, where no ceiling here can see it;
# `previous_response_id`, `prompt` and `conversation` reach for provider-side
# state this reviewer does not keep.
REFUSED_REQUEST_FIELDS = ("background", "previous_response_id", "prompt", "conversation")
# Priority processing is billed above the list prices the ceiling is estimated
# from, so a request asking for it is a request whose cost this cannot bound.
REFUSED_SERVICE_TIERS = frozenset({"priority", "scale"})
# Returned to the client: what it needs to read the stream, to keep the timing
# of a refusal, and to route the rest of its turn. The provider's account-level
# rate-limit and credit-balance headers are deliberately not among them: they
# are facts about the Palomar account, and the namespace has no use for them.
RETURNED_RESPONSE_HEADERS = (
    "content-type",
    "retry-after",
    "x-codex-turn-state",
    "x-models-etag",
    "openai-model",
    "x-openai-model",
    "x-request-id",
    "x-reasoning-included",
)

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

    Two shapes, and no others: the provider itself, and an http origin on the
    loopback address, which is the fake upstream the integration test runs so
    that proving the wire protocol costs no model tokens. Another https host is
    refused rather than trusted, because the one thing this must never do is
    hand the reusable key to somewhere a typo in a deployment environment
    named. A plaintext origin on a routable address is refused for the same
    reason and more obviously.
    """
    text = origin.strip()
    if text.startswith("https://") and text.rstrip("/") != DEFAULT_UPSTREAM_ORIGIN:
        raise BrokerError("the only remote upstream this broker serves is the provider")
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
        """Take the provider's own token counts, and price them if they are usable.

        A request whose usage never arrives, because the stream was truncated
        or the client hung up before the terminal event, is charged a standing
        estimate instead. Otherwise a pass that disconnects every request just
        before the provider reports it would never approach any ceiling while
        spending whatever it liked.
        """
        counts = usage_accounting.responses_usage_tokens(usage)
        with self._lock:
            if counts is None:
                self._unpriced += 1
                counts = dict(UNMEASURED_REQUEST_TOKENS)
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
            if not isinstance(event, dict) or event.get("type") not in TERMINAL_STREAM_EVENTS:
                continue
            response = event.get("response")
            if isinstance(response, dict):
                self.seen = True
                self._ledger.record_usage(response.get("usage"))


class _BoundedServer(ThreadingHTTPServer):
    """A threading server that will not open unbounded threads.

    A process in the namespace knows the port. Opening connections and then
    saying nothing costs it nothing and would cost the runner a thread and a
    stack each, so past a generous cap a new connection is closed rather than
    handed to a thread. This is a separate bound from the concurrency ceiling,
    which counts requests that reach the provider.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._connections = threading.Semaphore(MAX_CONNECTIONS)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that hangs up mid-exchange is ordinary here, and the
        # traceback socketserver would print says nothing worth saying.
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # The client is inside the sandbox. It learns the port it was told to use
    # and nothing about what is listening on it.
    server_version = "palomar-broker"
    sys_version = ""
    # Every socket operation on a connection is bounded, so a process in the
    # namespace cannot hold threads open by connecting and then saying nothing.
    # It is per operation rather than per connection, and a long wait here is
    # for the provider rather than the client, so this does not cut a slow
    # answer short.
    timeout = CLIENT_SOCKET_SECONDS
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
        """Read a little of a refused body, so the refusal reaches the client.

        A little, and not all of it: an unauthenticated client that announces
        a large body is not owed the time it would take to read one, and the
        connection is closed after this either way.
        """
        remaining = min(length, DRAIN_LIMIT_BYTES)
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
        if request.get("store") is not False:
            # Exactly `store: false`, which is what pinned Codex sends on every
            # call, and not "false or nothing": the Responses API stores the
            # response for thirty days when `store` is omitted, so an omitted
            # or null field is a request for provider-side state this review
            # never asked for and cannot clean up.
            return "this broker serves only unstored responses", "unexpected store mode"
        for field in REFUSED_REQUEST_FIELDS:
            if request.get(field) not in (None, False):
                return f"this broker does not serve {field}", f"unexpected {field}"
        tier = request.get("service_tier")
        if isinstance(tier, str) and tier.lower() in REFUSED_SERVICE_TIERS:
            return "this broker serves only ordinary processing", "unexpected service tier"
        effort = self.policy.reasoning_effort
        if effort is not None:
            reasoning = request.get("reasoning")
            offered = reasoning.get("effort") if isinstance(reasoning, dict) else None
            # Exactly the configured effort, not "the configured effort or
            # nothing": omitting it is how a request asks for the provider's
            # default, which is not the effort this review was configured for.
            if offered != effort:
                return f"this broker serves only reasoning effort {effort}", "unexpected reasoning effort"
        tools = request.get("tools")
        if tools is not None:
            # Pinned Codex sends no top-level `tools` at all: its own tools
            # travel inside `input`, and the client runs them. What this
            # refuses is a hosted tool, which the provider would run on the
            # requester's behalf, and which is how a process in the namespace
            # would give the reviewer the web research the policy says it does
            # not have. It is one layer and not a proof: a hosted tool smuggled
            # into `input` by a future client shape is not inspected here.
            if not isinstance(tools, list):
                return "this broker serves only client-run tools", "unexpected tools"
            for tool in tools:
                kind = tool.get("type") if isinstance(tool, dict) else None
                if kind not in CLIENT_RUN_TOOL_TYPES:
                    return "this broker serves only client-run tools", "unexpected tools"
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
        connection: http.client.HTTPConnection | None = None
        # One `finally` around the whole exchange, including the paths that
        # fail before there is a response: a provider that stops answering
        # must not leave this holding an open socket to it until a garbage
        # collection nobody scheduled.
        try:
            try:
                if secure:
                    connection = http.client.HTTPSConnection(
                        host,
                        port,
                        timeout=self.policy.connect_seconds,
                        context=ssl.create_default_context(),
                    )
                else:
                    connection = http.client.HTTPConnection(
                        host, port, timeout=self.policy.connect_seconds
                    )
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
            if 300 <= response.status < 400:
                self._refuse(502, "the model provider redirected", "upstream redirect")
                return False
            return self._stream(response)
        finally:
            if connection is not None:
                with contextlib.suppress(OSError, http.client.HTTPException):
                    connection.close()

    def _stream(self, response: http.client.HTTPResponse) -> bool:
        """Copy the upstream answer through in bounded chunks.

        Nothing is buffered whole: the response can be a long stream of
        reasoning and tool events, and the client is waiting on it. Only the
        terminal-event usage is taken out of the bytes as they pass, and a
        started response that never reports its usage is charged the standing
        estimate on the way out, however it ended.
        """
        reader = _UsageReader(self.ledger)
        try:
            return self._relay(response, reader)
        finally:
            if response.status == 200 and not reader.seen:
                self.ledger.record_usage(None)

    def _relay(self, response: http.client.HTTPResponse, reader: _UsageReader) -> bool:
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
        return response.status == 200


def build_server(
    *,
    policy: BrokerPolicy,
    capability: str,
    upstream_key: str,
    host: str = LOOPBACK_HOST,
    listen_socket: socket.socket | None = None,
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
    server = _BoundedServer((host, 0), handler, bind_and_activate=listen_socket is None)
    if listen_socket is not None:
        # The trusted parent bound and listened; this adopts that socket rather
        # than binding one of its own. See `BrokerProcess.start`.
        server.socket.close()
        server.socket = listen_socket
        server.server_address = listen_socket.getsockname()
        # `server_bind` would have set these, and it is not being run.
        server.server_name, server.server_port = server.server_address
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
    listening: socket.socket | None = None
    try:
        config = json.loads(line)
        policy = BrokerPolicy.from_json(config.get("policy"))
        capability = str(config["capability"])
        upstream_key = str(config["upstream_key"])
        # The parent bound and listened before this process existed, and holds
        # its own reference for the whole pass. Adopting that socket is what
        # keeps the port from becoming available to anything else the moment
        # this process dies: a listener on the same port could otherwise answer
        # a retry with a fabricated review.
        listening = socket.socket(fileno=int(config["listen_fd"]))
        server, ledger = build_server(
            policy=policy,
            capability=capability,
            upstream_key=upstream_key,
            listen_socket=listening,
        )
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
        if listening is not None:
            listening.close()
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
        self._listener: socket.socket | None = None
        self._port: int | None = None
        self.summary: dict[str, Any] | None = None
        self.exit_status: int | None = None

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
        # The port is bound here, in the trusted parent, and this reference to
        # it is held for the whole pass. The child serves on the same socket.
        # If the child dies, the port stays taken rather than becoming
        # available: a process in the namespace knows the port and the
        # capability, and a listener of its own on that port could answer a
        # client retry with a fabricated review.
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind((LOOPBACK_HOST, 0))
            listener.listen(MAX_CONNECTIONS)
            listener.set_inheritable(True)
        except OSError as error:
            raise BrokerError(f"could not bind the model broker: {error}") from error
        self._listener = listener
        self._port = listener.getsockname()[1]
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "palomar_reviewer.broker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                env=broker_environment(),
                close_fds=True,
                pass_fds=(listener.fileno(),),
            )
        except OSError as error:
            self.close()
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
                        "listen_fd": listener.fileno(),
                    }
                )
                + "\n"
            )
            process.stdin.flush()
        except OSError as error:
            self.close()
            raise BrokerError(f"could not configure the model broker: {error}") from error
        try:
            started = self._next_line(BROKER_START_SECONDS)
        except BrokerError:
            # A broker that never answered is still a child of this process,
            # and reaping it is this method's job whether it worked or not.
            self.close()
            raise
        port = started.get("port") if isinstance(started, dict) else None
        if port != self._port:
            self.close()
            raise BrokerError("the model broker did not report the port it was given")

    def close(self) -> dict[str, Any] | None:
        """Stop the broker and invalidate the capability, whatever happened."""
        process = self._process
        if process is None:
            self._release_port()
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
        self.exit_status = process.returncode
        # Last, so that the port stays taken until the process that was serving
        # on it is gone rather than merely asked to stop.
        self._release_port()
        return self.summary

    def _release_port(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()

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
