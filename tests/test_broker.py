"""What the loopback model broker refuses, and what the namespace never holds.

These are the regressions for the one property the boundary exists for: a
review of an attacker-authored repository must not be able to come away with a
credential that still works afterwards. Most of it runs against a fake local
provider, so proving the wire protocol costs no model tokens. Two of them run
the real namespace and real pinned Codex, because a boundary that is only ever
tested against a simplified fixture is a boundary nobody has tested.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_cli import UsesCapabilities

from palomar_reviewer import broker, engine

UPSTREAM_CANARY = "sk-palomar-canary-upstream-key-0123456789"
CAPABILITY = "capability-for-exactly-one-pass"
MODEL = "gpt-5.6-sol"
USAGE = {
    "input_tokens": 1000,
    "input_tokens_details": {"cached_tokens": 400},
    "output_tokens": 200,
    "output_tokens_details": {"reasoning_tokens": 50},
    "total_tokens": 1200,
}


def sse(events: list[dict]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode("utf-8")


def completed_stream(text: str = '{"ok":true}', usage: dict | None = None) -> bytes:
    return sse(
        [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                },
            },
            {
                "type": "response.completed",
                "response": {"id": "resp_1", "usage": USAGE if usage is None else usage},
            },
        ]
    )


def send_stream(handler: BaseHTTPRequestHandler, payload: bytes, *, status: int = 200) -> None:
    handler.send_response(status)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("content-length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class FakeUpstream:
    """A local stand-in for the provider, so nothing here spends model tokens."""

    def __init__(self, respond=None) -> None:
        self.requests: list[dict] = []
        self.respond = respond or (lambda handler: send_stream(handler, completed_stream()))
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length)
                upstream.requests.append(
                    {
                        "path": self.path,
                        "headers": {name.lower(): value for name, value in self.headers.items()},
                        "body": body,
                    }
                )
                upstream.respond(self)

            def log_message(self, *args) -> None:
                return

        class Server(ThreadingHTTPServer):
            # Several tests make this end of the connection fail on purpose.
            # The traceback socketserver would print is the test working.
            def handle_error(self, request, client_address) -> None:
                return

        self._server = Server(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True

    def __enter__(self) -> FakeUpstream:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_exception) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"


@contextlib.contextmanager
def running_broker(policy: broker.BrokerPolicy, *, capability: str = CAPABILITY):
    """The real broker, served in this process so a test can hold its ledger."""
    server, ledger = broker.build_server(
        policy=policy, capability=capability, upstream_key=UPSTREAM_CANARY
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", ledger, server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=10)


def request(
    base: str,
    *,
    capability: str | None = CAPABILITY,
    body: dict | bytes | None = None,
    path: str = broker.RESPONSES_PATH,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    duplicate_capability: bool = False,
    chunked: bool = False,
    declared_length: int | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], bytes]:
    """One call at the broker, from where a process in the namespace stands."""
    host = base.split("//", 1)[1]
    payload = body if isinstance(body, bytes) else json.dumps(
        {"model": MODEL, "stream": True} if body is None else body
    ).encode("utf-8")
    connection = http.client.HTTPConnection(host, timeout=timeout)
    try:
        connection.putrequest(method, path, skip_accept_encoding=True)
        if capability is not None:
            connection.putheader("authorization", f"Bearer {capability}")
            if duplicate_capability:
                connection.putheader("authorization", f"Bearer {capability}")
        connection.putheader("content-type", "application/json")
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        if chunked:
            connection.putheader("transfer-encoding", "chunked")
        else:
            connection.putheader(
                "content-length", str(len(payload) if declared_length is None else declared_length)
            )
        connection.endheaders()
        connection.send(payload)
        response = connection.getresponse()
        try:
            content = response.read()
        except http.client.HTTPException:
            content = b""
        return response.status, {k.lower(): v for k, v in response.getheaders()}, content
    finally:
        connection.close()


class UpstreamOriginTests(unittest.TestCase):
    def test_only_the_provider_or_a_loopback_upstream_is_permitted(self):
        self.assertEqual(
            broker.upstream_origin("https://api.openai.com"), (True, "api.openai.com", 443)
        )
        self.assertEqual(broker.upstream_origin("http://127.0.0.1:8123"), (False, "127.0.0.1", 8123))
        for refused in (
            "http://api.openai.com",
            "http://10.0.0.1:8080",
            "ftp://api.openai.com",
            "api.openai.com",
            "https://api.openai.com/v1/responses",
            "",
        ):
            with self.subTest(origin=refused):
                with self.assertRaises(broker.BrokerError):
                    broker.upstream_origin(refused)

    def test_the_default_upstream_is_the_provider_and_the_seam_is_validated(self):
        self.assertEqual(broker.configured_upstream_origin({}), broker.DEFAULT_UPSTREAM_ORIGIN)
        self.assertEqual(
            broker.configured_upstream_origin(
                {broker.UPSTREAM_ORIGIN_ENV: "http://127.0.0.1:9999"}
            ),
            "http://127.0.0.1:9999",
        )
        with self.assertRaises(broker.BrokerError):
            broker.configured_upstream_origin({broker.UPSTREAM_ORIGIN_ENV: "http://example.com"})


class BrokerEnvironmentTests(unittest.TestCase):
    def test_the_broker_child_inherits_no_operator_credential(self):
        environment = broker.broker_environment(
            {
                "PATH": "/usr/bin",
                "LANG": "C.UTF-8",
                "GH_TOKEN": "github-token",
                "GITHUB_TOKEN": "github-token",
                "PALOMAR_ARCHIVE_TOKEN": "archive-token",
                "PALOMAR_ALLOW_STATE_WRITES": "1",
                "OPENAI_API_KEY": UPSTREAM_CANARY,
                broker.UPSTREAM_KEY_ENV: UPSTREAM_CANARY,
                "HOME": "/home/operator",
            }
        )
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(
            set(environment) - {"PYTHONPATH"},
            {"PATH", "LANG"},
        )
        self.assertNotIn(UPSTREAM_CANARY, json.dumps(environment))

    def test_the_upstream_key_is_never_an_environment_value_of_the_child(self):
        # Read from the running child's own /proc, not from what this test
        # asked for: the point is what the process holds, not what was meant.
        with FakeUpstream() as upstream:
            policy = broker.BrokerPolicy(model=MODEL, upstream_origin=upstream.origin)
            with broker.started_broker(policy=policy, upstream_key=UPSTREAM_CANARY) as running:
                environ = Path(f"/proc/{running.pid}/environ").read_bytes()
                argv = Path(f"/proc/{running.pid}/cmdline").read_bytes()
        for held in (environ, argv):
            self.assertNotIn(UPSTREAM_CANARY.encode("utf-8"), held)
            self.assertNotIn(running.capability.encode("utf-8"), held)
            self.assertNotIn(b"GH_TOKEN", held)
            self.assertNotIn(b"PALOMAR_ARCHIVE_TOKEN", held)


class ForwardingTests(unittest.TestCase):
    def test_the_capability_is_replaced_by_the_upstream_authorization(self):
        with FakeUpstream() as upstream:
            policy = broker.BrokerPolicy(
                model=MODEL, reasoning_effort="high", upstream_origin=upstream.origin
            )
            with running_broker(policy) as (base, ledger, _server):
                status, headers, body = request(
                    base,
                    body={"model": MODEL, "stream": True, "reasoning": {"effort": "high"}},
                    headers={
                        "accept": "text/event-stream",
                        "x-codex-window-id": "window-1",
                        "x-smuggled-header": "should not reach the provider",
                    },
                )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertEqual(body, completed_stream())
        self.assertEqual(len(upstream.requests), 1)
        forwarded = upstream.requests[0]
        self.assertEqual(forwarded["path"], broker.RESPONSES_PATH)
        self.assertEqual(forwarded["headers"]["authorization"], f"Bearer {UPSTREAM_CANARY}")
        self.assertEqual(forwarded["headers"]["x-codex-window-id"], "window-1")
        self.assertNotIn("x-smuggled-header", forwarded["headers"])
        self.assertNotIn(CAPABILITY, json.dumps(forwarded["headers"]))
        summary = ledger.summary()
        self.assertEqual(summary["forwarded_requests"], 1)
        self.assertEqual(summary["input_tokens"], 1000)
        self.assertEqual(summary["cached_input_tokens"], 400)
        self.assertEqual(summary["output_tokens"], 200)
        self.assertEqual(summary["refusals"], {})
        self.assertGreater(summary["estimated_usd"], 0)

    def test_the_stream_reaches_the_client_before_it_has_finished(self):
        """A boundary that buffered the answer whole would stall every pass."""
        released = threading.Event()

        def respond(handler):
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.send_header("transfer-encoding", "chunked")
            handler.end_headers()
            first = b"event: response.created\ndata: {}\n\n"
            handler.wfile.write(b"%x\r\n" % len(first) + first + b"\r\n")
            handler.wfile.flush()
            released.wait(10)
            rest = completed_stream()
            handler.wfile.write(b"%x\r\n" % len(rest) + rest + b"\r\n")
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()

        with FakeUpstream(respond) as upstream:
            policy = broker.BrokerPolicy(model=MODEL, upstream_origin=upstream.origin)
            with running_broker(policy) as (base, ledger, _server):
                host = base.split("//", 1)[1]
                connection = http.client.HTTPConnection(host, timeout=30)
                connection.request(
                    "POST",
                    broker.RESPONSES_PATH,
                    body=json.dumps({"model": MODEL, "stream": True}).encode("utf-8"),
                    headers={"authorization": f"Bearer {CAPABILITY}"},
                )
                response = connection.getresponse()
                early = response.read1(4096)
                released.set()
                rest = response.read()
                connection.close()
        self.assertIn(b"response.created", early)
        self.assertNotIn(b"response.completed", early)
        self.assertIn(b"response.completed", rest)
        self.assertEqual(ledger.summary()["input_tokens"], 1000)


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.upstream.__enter__()
        self.addCleanup(self.upstream.__exit__, None, None, None)

    def policy(self, **overrides) -> broker.BrokerPolicy:
        return broker.BrokerPolicy(
            model=MODEL, upstream_origin=self.upstream.origin, **overrides
        )

    def test_a_wrong_missing_or_duplicated_capability_never_reaches_the_provider(self):
        with running_broker(self.policy()) as (base, ledger, _server):
            for name, kwargs in (
                ("wrong", {"capability": "not-the-capability"}),
                ("missing", {"capability": None}),
                ("prefix", {"capability": CAPABILITY[:-1]}),
                ("extended", {"capability": CAPABILITY + "x"}),
                ("duplicated", {"duplicate_capability": True}),
            ):
                with self.subTest(capability=name):
                    status, _headers, body = request(base, **kwargs)
                    self.assertEqual(status, 401)
                    self.assertNotIn(CAPABILITY.encode("utf-8"), body)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(ledger.summary()["refusals"]["bad capability"], 5)

    def test_only_the_pinned_responses_call_is_served(self):
        with running_broker(self.policy()) as (base, ledger, _server):
            for path, expected in (
                ("/v1/responses?store=true", 404),
                ("/v1/chat/completions", 404),
                ("/v1/models", 404),
                ("/", 404),
                ("/v1/responses/../v1/responses", 404),
            ):
                with self.subTest(path=path):
                    status, _headers, _body = request(base, path=path)
                    self.assertEqual(status, expected)
            for method in ("GET", "PUT", "DELETE", "OPTIONS", "PATCH"):
                with self.subTest(method=method):
                    status, _headers, _body = request(base, method=method)
                    self.assertIn(status, (405, 501))
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(ledger.summary()["forwarded_requests"], 0)

    def test_the_configured_model_and_effort_are_the_only_ones_served(self):
        policy = broker.BrokerPolicy(
            model=MODEL, reasoning_effort="high", upstream_origin=self.upstream.origin
        )
        with running_broker(policy) as (base, ledger, _server):
            for name, body in (
                ("another model", {"model": "gpt-4o", "stream": True}),
                ("no model", {"stream": True}),
                ("not streamed", {"model": MODEL, "stream": False}),
                (
                    "another effort",
                    {"model": MODEL, "stream": True, "reasoning": {"effort": "low"}},
                ),
                ("not an object", [{"model": MODEL}]),
            ):
                with self.subTest(request=name):
                    status, _headers, _body = request(base, body=body)
                    self.assertEqual(status, 403)
            status, _headers, _body = request(base, body=b"{not json")
            self.assertEqual(status, 403)
        self.assertEqual(self.upstream.requests, [])
        refusals = ledger.summary()["refusals"]
        self.assertEqual(refusals["unexpected model"], 2)
        self.assertEqual(refusals["unexpected stream mode"], 1)
        self.assertEqual(refusals["unexpected reasoning effort"], 1)
        self.assertEqual(refusals["unparsable request body"], 2)

    def test_an_oversized_or_undeclared_body_is_refused_unread(self):
        with running_broker(self.policy(max_request_bytes=512)) as (base, ledger, _server):
            status, _headers, _body = request(base, body=b"x" * 1024)
            self.assertEqual(status, 413)
            status, _headers, _body = request(base, chunked=True)
            self.assertEqual(status, 411)
        self.assertEqual(self.upstream.requests, [])
        refusals = ledger.summary()["refusals"]
        self.assertEqual(refusals["oversized request body"], 1)
        self.assertEqual(refusals["chunked request"], 1)

    def test_the_pass_is_bounded_by_requests_tokens_and_spend(self):
        for limit, reason in (
            ({"max_requests": 1}, "request ceiling reached"),
            ({"max_total_tokens": 100}, "token ceiling reached"),
            ({"max_spend_usd": 0.001}, "spend ceiling reached"),
        ):
            with self.subTest(limit=reason):
                self.upstream.requests.clear()
                with running_broker(self.policy(**limit)) as (base, ledger, _server):
                    self.assertEqual(request(base)[0], 200)
                    status, _headers, body = request(base)
                self.assertEqual(status, 403)
                self.assertIn(b"bounded", body)
                self.assertEqual(len(self.upstream.requests), 1)
                self.assertEqual(ledger.summary()["refusals"][reason], 1)

    def test_concurrent_requests_beyond_the_ceiling_are_refused(self):
        holding = threading.Event()

        def respond(handler):
            holding.wait(10)
            send_stream(handler, completed_stream())

        self.upstream.respond = respond
        results: list[int] = []
        with running_broker(self.policy(max_concurrent_requests=1)) as (base, ledger, _server):
            first = threading.Thread(target=lambda: results.append(request(base)[0]))
            first.start()
            # The first request is inside the broker and waiting on the fake
            # provider by the time the second arrives.
            time.sleep(0.5)
            second = request(base)[0]
            holding.set()
            first.join(timeout=20)
        self.assertEqual(second, 403)
        self.assertEqual(results, [200])
        self.assertEqual(ledger.summary()["refusals"]["too many concurrent requests"], 1)

    def test_an_oversized_response_is_cut_off_rather_than_relayed(self):
        self.upstream.respond = lambda handler: send_stream(handler, b"d" * 100_000)
        with running_broker(self.policy(max_response_bytes=1024)) as (base, ledger, _server):
            _status, _headers, body = request(base)
        self.assertLess(len(body), 100_000)
        self.assertEqual(ledger.summary()["refusals"]["oversized response"], 1)

    def test_a_redirect_is_never_followed(self):
        def respond(handler):
            handler.send_response(302)
            handler.send_header("location", "https://elsewhere.example/v1/responses")
            handler.send_header("content-length", "0")
            handler.end_headers()

        self.upstream.respond = respond
        with running_broker(self.policy()) as (base, ledger, _server):
            status, headers, _body = request(base)
        self.assertEqual(status, 502)
        self.assertNotIn("location", headers)
        self.assertEqual(ledger.summary()["refusals"]["upstream redirect"], 1)

    def test_an_upstream_that_stalls_or_disconnects_fails_the_request(self):
        def stall(handler):
            time.sleep(3)
            send_stream(handler, completed_stream())

        self.upstream.respond = stall
        with running_broker(self.policy(read_seconds=0.5)) as (base, ledger, _server):
            status, _headers, _body = request(base)
        self.assertEqual(status, 504)
        self.assertEqual(ledger.summary()["refusals"]["upstream timeout"], 1)

        def hang_up(handler):
            handler.close_connection = True
            handler.wfile.close()

        self.upstream.respond = hang_up
        with running_broker(self.policy()) as (base, ledger, _server):
            status, _headers, _body = request(base)
        self.assertEqual(status, 502)
        self.assertEqual(ledger.summary()["refusals"]["upstream transport failure"], 1)

    def test_a_truncated_stream_is_recorded_rather_than_priced(self):
        def truncated(handler):
            payload = completed_stream()
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.send_header("content-length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload[: len(payload) // 2])
            handler.wfile.close()

        self.upstream.respond = truncated
        with running_broker(self.policy()) as (base, ledger, _server):
            request(base)
        summary = ledger.summary()
        self.assertEqual(summary["input_tokens"], 0)
        self.assertEqual(summary["refusals"]["upstream truncated"], 1)

    def test_a_stream_with_malformed_or_absent_usage_is_counted_not_guessed(self):
        self.upstream.respond = lambda handler: send_stream(
            handler,
            sse([{"type": "response.completed", "response": {"usage": {"input_tokens": "many"}}}])
            + b"data: {not json\n\ndata:\n\n",
        )
        with running_broker(self.policy()) as (base, ledger, _server):
            self.assertEqual(request(base)[0], 200)
        summary = ledger.summary()
        self.assertEqual(summary["input_tokens"], 0)
        self.assertEqual(summary["responses_without_usage"], 1)
        self.assertEqual(summary["estimated_usd"], 0)

    def test_an_upstream_error_keeps_its_status_and_loses_its_headers(self):
        def refused(handler):
            payload = b'{"error":{"message":"slow down"}}'
            handler.send_response(429)
            handler.send_header("content-type", "application/json")
            handler.send_header("retry-after", "7")
            handler.send_header("openai-organization", "palomar-private-org")
            handler.send_header("content-length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        self.upstream.respond = refused
        with running_broker(self.policy()) as (base, _ledger, _server):
            status, headers, body = request(base)
        self.assertEqual(status, 429)
        self.assertEqual(headers["retry-after"], "7")
        self.assertNotIn("openai-organization", headers)
        self.assertIn(b"slow down", body)


class DiagnosticTests(unittest.TestCase):
    def test_no_diagnostic_repeats_either_credential(self):
        with FakeUpstream() as upstream:
            policy = broker.BrokerPolicy(model=MODEL, upstream_origin=upstream.origin)
            said = io.StringIO()
            with contextlib.redirect_stderr(said):
                with running_broker(policy) as (base, ledger, server):
                    request(base, capability="wrong")
                    request(base, body={"model": "gpt-4o", "stream": True})
                    request(base)
                scrub = server.RequestHandlerClass.scrub
        spoken = said.getvalue()
        self.assertIn("refused: bad capability", spoken)
        self.assertNotIn(CAPABILITY, spoken)
        self.assertNotIn(UPSTREAM_CANARY, spoken)
        self.assertNotIn(UPSTREAM_CANARY, json.dumps(ledger.summary()))
        self.assertNotIn(CAPABILITY, json.dumps(ledger.summary()))
        self.assertEqual(
            scrub(f"key {UPSTREAM_CANARY} and capability {CAPABILITY}"),
            "key [redacted] and capability [redacted]",
        )

    def test_the_broker_refuses_to_bind_anything_but_loopback(self):
        policy = broker.BrokerPolicy(model=MODEL)
        for host in ("0.0.0.0", "::", "example.com"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(broker.BrokerError, "binds 127.0.0.1 only"):
                    broker.build_server(
                        policy=policy, capability=CAPABILITY, upstream_key=UPSTREAM_CANARY, host=host
                    )

    def test_a_broker_without_a_model_or_a_credential_does_not_start(self):
        with self.assertRaises(broker.BrokerError):
            broker.BrokerPolicy(model="  ")
        with self.assertRaises(broker.BrokerError):
            broker.build_server(
                policy=broker.BrokerPolicy(model=MODEL), capability="", upstream_key=UPSTREAM_CANARY
            )
        with self.assertRaisesRegex(broker.BrokerError, broker.UPSTREAM_KEY_ENV):
            broker.BrokerProcess(policy=broker.BrokerPolicy(model=MODEL), upstream_key="")


class BrokerProcessTests(unittest.TestCase):
    def test_the_capability_stops_working_when_the_pass_ends(self):
        with FakeUpstream() as upstream:
            policy = broker.BrokerPolicy(model=MODEL, upstream_origin=upstream.origin)
            with broker.started_broker(policy=policy, upstream_key=UPSTREAM_CANARY) as running:
                base = running.base_url.removesuffix("/v1")
                capability = running.capability
                self.assertGreaterEqual(len(capability), 32)
                self.assertTrue(base.startswith("http://127.0.0.1:"))
                status, _headers, _body = request(base, capability=capability)
                self.assertEqual(status, 200)
            self.assertEqual(running.summary["forwarded_requests"], 1)
            self.assertEqual(running.summary["input_tokens"], 1000)
            with self.assertRaises(OSError):
                request(base, capability=capability, timeout=5)
            # Closing twice is what a `finally` inside a `finally` does.
            self.assertIsNotNone(running.close())
            with self.assertRaises(broker.BrokerError):
                self.fail(f"a stopped broker still has {running.base_url}")

    def test_an_exception_in_the_pass_still_shuts_the_broker_down(self):
        with FakeUpstream() as upstream:
            policy = broker.BrokerPolicy(model=MODEL, upstream_origin=upstream.origin)
            held = {}
            with self.assertRaises(ZeroDivisionError):
                with broker.started_broker(policy=policy, upstream_key=UPSTREAM_CANARY) as running:
                    held["base"] = running.base_url.removesuffix("/v1")
                    held["capability"] = running.capability
                    held["pid"] = running.pid
                    raise ZeroDivisionError("the pass failed")
            with self.assertRaises(OSError):
                request(held["base"], capability=held["capability"], timeout=5)
            self.assertFalse(Path(f"/proc/{held['pid']}/cmdline").exists())

    def test_a_broker_that_crashes_at_startup_is_a_failure_and_not_a_fallback(self):
        exits = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(3)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        with mock.patch.object(broker.subprocess, "Popen", return_value=exits):
            running = broker.BrokerProcess(
                policy=broker.BrokerPolicy(model=MODEL), upstream_key=UPSTREAM_CANARY
            )
            with self.assertRaises(broker.BrokerError):
                running.start()

    def test_a_broker_killed_mid_pass_refuses_every_later_request(self):
        with FakeUpstream() as upstream:
            policy = broker.BrokerPolicy(model=MODEL, upstream_origin=upstream.origin)
            running = broker.BrokerProcess(policy=policy, upstream_key=UPSTREAM_CANARY)
            running.start()
            base = running.base_url.removesuffix("/v1")
            self.assertEqual(request(base, capability=running.capability)[0], 200)
            os.kill(running.pid, 9)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    request(base, capability=running.capability, timeout=5)
                except OSError:
                    break
                time.sleep(0.1)
            else:
                self.fail("the killed broker kept serving")
            # And shutting a dead broker down is not itself a failure.
            self.assertIsNone(running.close())

    def test_a_malformed_control_line_starts_nothing(self):
        for line in ("", "not json\n", '{"policy": {}}\n', '{"policy": {"model": ""}}\n'):
            with self.subTest(line=line):
                out, err = io.StringIO(), io.StringIO()
                self.assertEqual(broker.serve(io.StringIO(line), out, err), 2)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("palomar-broker", err.getvalue())


class PinnedClientTests(unittest.TestCase):
    def test_ci_and_the_readme_install_the_codex_release_this_was_written_for(self):
        pinned = f"@openai/codex@{engine.PINNED_CODEX_VERSION}"
        root = Path(__file__).resolve().parents[1]
        for relative in (".github/workflows/ci.yml", "README.md"):
            with self.subTest(relative=relative):
                self.assertIn(pinned, (root / relative).read_text(encoding="utf-8"))


class NamespaceCredentialTests(UsesCapabilities, unittest.TestCase):
    """The real Bubblewrap namespace, searched for a credential it must not hold."""

    PROBE = (
        "echo '=== env'; env; "
        "echo '=== argv'; cat /proc/*/cmdline 2>/dev/null | tr '\\0' '\\n'; "
        "echo '=== paths'; find /home /output /tmp /workspace -maxdepth 6 2>/dev/null; "
        "echo '=== content'; "
        "find /home /output /tmp /workspace -maxdepth 6 -type f -size -64k "
        "-exec cat {} + 2>/dev/null"
    )

    def test_no_reusable_credential_is_reachable_from_inside_the_codex_namespace(self):
        self.require("sandbox", "codex")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            (source / "README.md").write_text("a submitted repository\n", encoding="utf-8")
            capability = "capability-the-probe-should-find"
            payload = engine.sandbox_environment_args({broker.CAPABILITY_ENV: capability})
            read_fd, write_fd = os.pipe()
            os.write(write_fd, payload)
            os.close(write_fd)
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        broker.UPSTREAM_KEY_ENV: UPSTREAM_CANARY,
                        # Set as well, precisely because it is what the old
                        # design bound in: if anything still reaches for it,
                        # this test is where that shows up.
                        "OPENAI_API_KEY": UPSTREAM_CANARY,
                        "GH_TOKEN": "github-token-canary",
                    },
                ):
                    command = engine.isolated_command(
                        "codex",
                        engine.codex_arguments(
                            schema_name="schema.json",
                            output_name="message.txt",
                            model=MODEL,
                            reasoning_effort="high",
                            broker_base_url="http://127.0.0.1:1/v1",
                        ),
                        cwd=source,
                        output_dir=output,
                        secret_args_fd=read_fd,
                    )
                # Everything about the namespace, with the engine replaced by a
                # probe of it. Nothing else about the command is changed.
                separator = command.index("--")
                probe = [*command[: separator + 1], "/bin/sh", "-c", self.PROBE]
                found = subprocess.run(
                    probe, capture_output=True, text=True, pass_fds=(read_fd,), timeout=120
                )
            finally:
                os.close(read_fd)

        self.assertEqual(found.returncode, 0, found.stderr)
        namespace = found.stdout
        # The probe is looking at the real namespace: the one credential that
        # belongs there is there.
        self.assertIn(capability, namespace)
        self.assertIn(broker.CAPABILITY_ENV, namespace)
        # And nothing that outlives the pass is.
        self.assertNotIn(UPSTREAM_CANARY, namespace)
        self.assertNotIn("github-token-canary", namespace)
        self.assertNotIn("OPENAI_API_KEY", namespace)
        self.assertNotIn("GH_TOKEN", namespace)
        self.assertNotIn("auth.json", namespace)
        # The engine's home holds nothing at all: no credential file, and
        # nothing an earlier pass or the operator's own Codex install left.
        paths = namespace.split("=== paths\n", 1)[1].split("=== content", 1)[0].split()
        self.assertEqual(
            sorted(path for path in paths if path.startswith("/home")),
            ["/home", "/home/reviewer", "/home/reviewer/.claude", "/home/reviewer/.codex"],
        )

    def test_the_namespace_command_carries_no_credential_in_argv(self):
        self.require("sandbox", "codex")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": UPSTREAM_CANARY}):
                command = engine.isolated_command(
                    "codex",
                    engine.codex_arguments(
                        schema_name="schema.json",
                        output_name="message.txt",
                        model=MODEL,
                        reasoning_effort="high",
                        broker_base_url="http://127.0.0.1:1/v1",
                    ),
                    cwd=source,
                    output_dir=root / "output",
                    secret_args_fd=9,
                )
        rendered = " ".join(command)
        self.assertNotIn(UPSTREAM_CANARY, rendered)
        self.assertNotIn("auth.json", rendered)
        self.assertNotIn("OPENAI_API_KEY", rendered)
        self.assertIn("--clearenv", command)
        self.assertEqual(command[command.index("--args") + 1], "9")
        self.assertLess(command.index("--clearenv"), command.index("--args"))

    def test_codex_refuses_to_start_without_the_broker_capability_channel(self):
        self.require("sandbox", "codex")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with self.assertRaisesRegex(engine.EngineError, "loopback model broker"):
                engine.isolated_command(
                    "codex",
                    ["codex", "exec"],
                    cwd=source,
                    output_dir=root / "output",
                )

    def test_the_claude_engine_is_not_a_production_engine(self):
        self.require("sandbox")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(engine.UNBROKERED_CLAUDE_ENV, None)
                with self.assertRaisesRegex(engine.EngineError, "not a production engine"):
                    engine.isolated_command(
                        "claude", ["claude"], cwd=source, output_dir=root / "output"
                    )


class CodexIntegrationTests(UsesCapabilities, unittest.TestCase):
    """Pinned Codex, the real namespace, the real broker, a fake provider."""

    SCHEMA = {
        "type": "object",
        "required": ["step", "summary"],
        "additionalProperties": False,
        "properties": {"step": {"type": "string"}, "summary": {"type": "string"}},
    }
    RESULT = {"step": "metadata", "summary": "the fake provider answered"}

    def responses(self, handler_state: dict):
        def respond(handler):
            handler_state["turns"] += 1
            if handler_state["turns"] == 1:
                # One tool round trip, so the streamed protocol is exercised
                # rather than a single answer that never uses a tool.
                items = [
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "input": "text('the tool ran')",
                            "call_id": "call_1",
                        },
                    }
                ]
            else:
                items = [
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": json.dumps(self.RESULT)}
                            ],
                        },
                    }
                ]
            send_stream(
                handler,
                sse(
                    [
                        {"type": "response.created", "response": {"id": "resp"}},
                        *items,
                        {"type": "response.completed", "response": {"id": "resp", "usage": USAGE}},
                    ]
                ),
            )

        return respond

    def test_a_whole_codex_pass_runs_through_the_broker(self):
        self.require("sandbox", "codex")
        state = {"turns": 0}
        with FakeUpstream(self.responses(state)) as upstream, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "README.md").write_text("a submitted repository\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet"], cwd=source, check=True)
            with mock.patch.dict(
                os.environ,
                {
                    broker.UPSTREAM_KEY_ENV: UPSTREAM_CANARY,
                    broker.UPSTREAM_ORIGIN_ENV: upstream.origin,
                },
            ):
                result, usage = engine.execute(
                    "Return the review result as JSON.",
                    engine="codex",
                    command=None,
                    model=MODEL,
                    cwd=source,
                    schema=self.SCHEMA,
                    raw_path=root / "raw" / "metadata.txt",
                    reasoning_effort="high",
                )
            events = (root / "raw" / "metadata.events.jsonl").read_text(encoding="utf-8")

        self.assertEqual(result, self.RESULT)
        self.assertGreaterEqual(state["turns"], 2)
        self.assertEqual(usage["usage_status"], "recorded")
        summary = usage["broker"]
        self.assertEqual(summary["forwarded_requests"], state["turns"])
        self.assertEqual(summary["refusals"], {})
        self.assertGreater(summary["input_tokens"], 0)
        for seen in upstream.requests:
            self.assertEqual(seen["path"], broker.RESPONSES_PATH)
            self.assertEqual(seen["headers"]["authorization"], f"Bearer {UPSTREAM_CANARY}")
            self.assertEqual(json.loads(seen["body"])["model"], MODEL)
        self.assertIn("turn.completed", events)
        self.assertNotIn(UPSTREAM_CANARY, events)


if __name__ == "__main__":
    unittest.main()
