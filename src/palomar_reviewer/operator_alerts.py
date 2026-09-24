"""Durable, bounded operator alerts for non-submitter verification failures."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import ReviewerError

ZULIP_MESSAGES_URL = "https://leanprover.zulipchat.com/api/v1/messages"
ZULIP_CHANNEL = "Palomar maintainers"
ZULIP_TOPIC = "operator alerts"
ZULIP_EMAIL_ENV = "PALOMAR_ZULIP_EMAIL"
ZULIP_API_KEY_ENV = "PALOMAR_ZULIP_API_KEY"
TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
SUBMISSION_ID_RE = re.compile(r"[0-9a-z]{12}\Z")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
DIAGNOSTIC_CODE_RE = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+\Z")
RUN_URL_RE = re.compile(
    r"https://github\.com/PalomarRegistry/PalomarSubmission/actions/runs/[1-9][0-9]*\Z"
)


def alert_key(state: dict[str, Any], failure: dict[str, Any], index: int) -> str:
    """Bind one alert to its submission, run, position, and bounded diagnostic."""
    diagnostics = failure.get("diagnostics")
    if not isinstance(diagnostics, list) or not 0 <= index < len(diagnostics):
        raise ReviewerError("operator alert diagnostic index is out of range")
    document = {
        "submission_id": state.get("id"),
        "repository": state.get("repository"),
        "commit": state.get("commit"),
        "run": failure.get("run"),
        "diagnostic_index": index,
        "diagnostic": diagnostics[index],
    }
    encoded = json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def queued_alerts(
    state: dict[str, Any], failure: dict[str, Any], *, queued_at: str
) -> dict[str, Any] | None:
    """Create one pending item for every diagnostic the UI assigns to Palomar."""
    diagnostics = failure.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise ReviewerError("operator alert failure has no diagnostics")
    items = [
        {
            "key": alert_key(state, failure, index),
            "diagnostic_index": index,
            "status": "pending",
            "queued_at": queued_at,
        }
        for index, diagnostic in enumerate(diagnostics)
        if isinstance(diagnostic, dict) and diagnostic.get("owner") != "submitter"
    ]
    previous = upgrade_alerts(state)
    old = previous["items"] if previous else []
    origin = alert_origin(state, failure)
    keys = {item["key"] for item in old}
    items = old + [{**item, "origin": origin} for item in items if item["key"] not in keys]
    if len(items) > 50:
        raise ReviewerError("operator alert history requires archival before another failure")
    return {"schema_version": 2, "items": items} if items else None


def validated_alert_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the private outbox before it can cause a public side effect."""
    marker = state.get("operator_alerts")
    if marker is None:
        return []
    if not isinstance(marker, dict) or set(marker) != {"schema_version", "items"}:
        raise ReviewerError("operator alert outbox has an unsupported shape")
    if type(marker.get("schema_version")) is int and marker["schema_version"] == 2:
        return validated_v2_items(state, marker)
    if marker.get("schema_version") != 1 or isinstance(marker.get("schema_version"), bool):
        raise ReviewerError("operator alert outbox has an unsupported schema version")
    items = marker.get("items")
    failure = state.get("failure")
    diagnostics = failure.get("diagnostics") if isinstance(failure, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ReviewerError("operator alert outbox must contain between one and 50 items")
    if not isinstance(diagnostics, list):
        raise ReviewerError("operator alert outbox has no bound failure diagnostics")
    seen_indexes: set[int] = set()
    seen_keys: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ReviewerError("operator alert item is not an object")
        status = item.get("status")
        expected_fields = (
            {"key", "diagnostic_index", "status", "queued_at"}
            if status == "pending"
            else {
                "key", "diagnostic_index", "status", "queued_at", "sent_at", "message_id"
            }
        )
        if set(item) != expected_fields or status not in {"pending", "sent"}:
            raise ReviewerError("operator alert item has an unsupported shape")
        index = item.get("diagnostic_index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(diagnostics):
            raise ReviewerError("operator alert diagnostic index is out of range")
        key = item.get("key")
        if not isinstance(key, str) or not SHA256_RE.fullmatch(key):
            raise ReviewerError("operator alert key is malformed")
        if index in seen_indexes or key in seen_keys:
            raise ReviewerError("operator alert outbox contains a duplicate item")
        seen_indexes.add(index)
        seen_keys.add(key)
        diagnostic = diagnostics[index]
        if not isinstance(diagnostic, dict) or diagnostic.get("owner") not in {
            "palomar",
            "provider",
        }:
            raise ReviewerError("operator alert is not bound to a Palomar-visible diagnostic")
        code = diagnostic.get("code")
        if not isinstance(code, str) or not DIAGNOSTIC_CODE_RE.fullmatch(code):
            raise ReviewerError("operator alert diagnostic code is malformed")
        for field, maximum in (
            ("stage", 500),
            ("summary", 500),
            ("explanation", 2_000),
            ("next_action", 2_000),
        ):
            value = diagnostic.get(field)
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise ReviewerError(f"operator alert diagnostic {field} is malformed")
        if type(diagnostic.get("retryable")) is not bool:
            raise ReviewerError("operator alert diagnostic retryable flag is malformed")
        if key != alert_key(state, failure, index):
            raise ReviewerError("operator alert key does not match its failure diagnostic")
        queued_at = item.get("queued_at")
        if not isinstance(queued_at, str) or not TIMESTAMP_RE.fullmatch(queued_at):
            raise ReviewerError("operator alert queued_at is malformed")
        if status == "sent":
            sent_at = item.get("sent_at")
            message_id = item.get("message_id")
            if not isinstance(sent_at, str) or not TIMESTAMP_RE.fullmatch(sent_at):
                raise ReviewerError("operator alert sent_at is malformed")
            if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 1:
                raise ReviewerError("operator alert message_id is malformed")
        result.append(dict(item))
    return result


def has_pending_alerts(state: dict[str, Any]) -> bool:
    """Whether a terminal submission still has operator notification work."""
    return any(item["status"] == "pending" for item in validated_alert_items(state))


def _plain_code_block(value: Any) -> str:
    text = str(value or "").strip().replace("```", "` ` `")
    return f"```text\n{text}\n```"


def _inline_code(value: Any) -> str:
    text = str(value or "").replace("`", "'").replace("\r", " ").replace("\n", " ")
    return f"`{text}`"


def alert_message(state: dict[str, Any], item: dict[str, Any]) -> str:
    """Render actionable context without a private status URL or active Markdown."""
    items = validated_alert_items(state)
    matching = [candidate for candidate in items if candidate["key"] == item.get("key")]
    if len(matching) != 1:
        raise ReviewerError("operator alert item is not present in its outbox")
    item = matching[0]
    if "origin" in item:
        origin = item["origin"]
        legacy_item = {key: value for key, value in item.items() if key not in V2_FIELDS}
        legacy_state = {**origin, "operator_alerts": {"schema_version": 1, "items": [legacy_item]}}
        original = alert_message(legacy_state, legacy_item)
        disposition = item.get("disposition")
        if not disposition or disposition["kind"] == "unresolved":
            return original
        kind = disposition["kind"]
        label = {
            "recovered": "Recovered: the same commit and configuration verified successfully.",
            "superseded": "Superseded: a newer commit for this configuration verified successfully. "
            "This does not establish that the original commit works.",
            "withdrawn": ("Withdrawn: this submission is no longer active. "
                          "The original failure is not marked fixed."),
            "reclassified": ("Reclassified: the original operator alert was reviewed "
                             "and its failure diagnosis corrected. See the current submission status."),
        }[kind]
        evidence = disposition.get("evidence")
        suffix = f"\n\n**Disposition — {label}**"
        if kind == "reclassified":
            suffix += (f"\nEvidence: {evidence['run_url']} "
                       f"(report SHA-256 `{evidence['report_sha256']}`, "
                       f"basis `{evidence['basis']}`).")
        elif evidence:
            suffix += (f"\nEvidence: {evidence['run_url']} (submission `{evidence['submission_id']}`, "
                       f"commit `{evidence['commit']}`).")
        return original + suffix
    submission_id = state.get("id")
    repository = state.get("repository")
    commit = state.get("commit")
    if not isinstance(submission_id, str) or not SUBMISSION_ID_RE.fullmatch(submission_id):
        raise ReviewerError("operator alert submission id is malformed")
    if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
        raise ReviewerError("operator alert repository is malformed")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise ReviewerError("operator alert commit is malformed")
    failure = state["failure"]
    diagnostic = failure["diagnostics"][item["diagnostic_index"]]
    run = failure.get("run")
    run_url = run.get("url") if isinstance(run, dict) else None
    if not isinstance(run_url, str) or not RUN_URL_RE.fullmatch(run_url):
        raise ReviewerError("operator alert workflow URL is malformed")
    source_url = f"https://github.com/{repository}/tree/{commit}"
    test_label = " **[technical test]**" if state.get("test_submission") is True else ""
    return "\n".join(
        [
            f"**Palomar must fix this**{test_label}",
            "",
            f"- Submission: `{submission_id}`",
            f"- Source: [{repository}@{commit[:12]}]({source_url})",
            f"- Workflow: {run_url}",
            f"- Phase/stage: {_inline_code(failure.get('phase', 'unknown'))} / "
            f"{_inline_code(diagnostic['stage'])}",
            f"- Diagnostic: {_inline_code(diagnostic['code'])} (owner {_inline_code(diagnostic['owner'])})",
            f"- Retryable unchanged: `{'yes' if diagnostic['retryable'] else 'no'}`",
            f"- Alert key: `{item['key'][:16]}`",
            "",
            "**Summary**",
            _plain_code_block(diagnostic["summary"]),
            "",
            "**Detail shown to the submitter**",
            _plain_code_block(diagnostic["explanation"]),
            "",
            "**Next action shown to the submitter**",
            _plain_code_block(diagnostic["next_action"]),
        ]
    )


def send_zulip_message(content: str, *, email: str, api_key: str) -> int:
    """Send one bounded channel message and return Zulip's durable message id."""
    if not email.strip() or len(email) > 500 or "\n" in email or "\r" in email:
        raise ReviewerError(f"{ZULIP_EMAIL_ENV} is missing or malformed")
    if not api_key.strip() or len(api_key) > 500 or "\n" in api_key or "\r" in api_key:
        raise ReviewerError(f"{ZULIP_API_KEY_ENV} is missing or malformed")
    encoded_auth = base64.b64encode(f"{email}:{api_key}".encode()).decode("ascii")
    body = urllib.parse.urlencode(
        {
            "type": "stream",
            "to": ZULIP_CHANNEL,
            "topic": ZULIP_TOPIC,
            "content": content,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ZULIP_MESSAGES_URL,
        data=body,
        headers={
            "Authorization": f"Basic {encoded_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = response.read(16_385)
    except urllib.error.HTTPError as error:
        raise ReviewerError(f"Zulip rejected the operator alert with HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ReviewerError("Zulip could not be reached for the operator alert") from error
    if len(payload) > 16_384:
        raise ReviewerError("Zulip returned an oversized operator-alert response")
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewerError("Zulip returned an invalid operator-alert response") from error
    message_id = result.get("id") if isinstance(result, dict) else None
    if (
        not isinstance(result, dict)
        or result.get("result") != "success"
        or not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id < 1
    ):
        raise ReviewerError("Zulip did not acknowledge the operator alert")
    return message_id


V2_FIELDS = {"origin", "disposition", "delivered_sha256", "edit_error"}


def alert_origin(state: dict, failure: dict) -> dict:
    import copy
    return {"id": state["id"], "repository": state["repository"], "commit": state["commit"],
            "test_submission": state.get("test_submission") is True, "failure": copy.deepcopy(failure)}


def upgrade_alerts(state: dict) -> dict | None:
    items = validated_alert_items(state)
    if not items:
        return None
    if state["operator_alerts"]["schema_version"] == 1:
        items = [{**item, "origin": alert_origin(state, state["failure"])} for item in items]
    return {"schema_version": 2, "items": items}


def validated_v2_items(state: dict, marker: dict) -> list[dict]:
    items = marker.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ReviewerError("operator alert outbox must contain between one and 50 items")
    keys = set()
    for item in items:
        if not isinstance(item, dict):
            raise ReviewerError("operator alert item must be an object")
        origin = item.get("origin")
        if not isinstance(origin, dict) or set(origin) != {
            "id",
            "repository",
            "commit",
            "test_submission",
            "failure",
        }:
            raise ReviewerError("operator alert origin is malformed")
        if any(origin.get(key) != state.get(key) for key in ("id", "repository", "commit")):
            raise ReviewerError("operator alert origin belongs to another submission")
        if (type(origin.get("test_submission")) is not bool
                or origin["test_submission"] != (state.get("test_submission") is True)):
            raise ReviewerError("operator alert origin test flag is malformed")
        legacy = {key: value for key, value in item.items() if key not in V2_FIELDS}
        validated_alert_items({**origin, "operator_alerts": {"schema_version": 1, "items": [legacy]}})
        if item["key"] in keys:
            raise ReviewerError("duplicate operator alert key")
        keys.add(item["key"])
        disposition = item.get("disposition")
        if disposition is not None:
            if not isinstance(disposition, dict) or disposition.get("kind") not in {
                "unresolved",
                "recovered",
                "superseded",
                "withdrawn",
                "reclassified",
            }:
                raise ReviewerError("invalid operator alert disposition")
            expected = {"kind", "at"} | (
                {"evidence"} if disposition["kind"] in {"recovered", "superseded", "reclassified"} else set()
            )
            if set(disposition) != expected or not TIMESTAMP_RE.fullmatch(str(disposition.get("at", ""))):
                raise ReviewerError("malformed operator alert disposition")
            if disposition["kind"] == "reclassified" and item["status"] != "sent":
                raise ReviewerError("only a sent operator alert can be reclassified")
            if "evidence" in disposition:
                evidence = disposition["evidence"]
                if disposition["kind"] == "reclassified":
                    if (
                        not isinstance(evidence, dict)
                        or set(evidence) != {"run_url", "report_sha256", "basis"}
                        or not RUN_URL_RE.fullmatch(str(evidence.get("run_url", "")))
                        or evidence.get("run_url") != (origin["failure"].get("run") or {}).get("url")
                        or not SHA256_RE.fullmatch(str(evidence.get("report_sha256", "")))
                        or evidence.get("basis") not in {"mechanical-report", "maintainer-analysis"}
                    ):
                        raise ReviewerError("invalid operator alert reclassification evidence")
                elif (
                    not isinstance(evidence, dict)
                    or set(evidence) != {"submission_id", "commit", "run_url"}
                    or not SUBMISSION_ID_RE.fullmatch(str(evidence.get("submission_id", "")))
                    or not COMMIT_RE.fullmatch(str(evidence.get("commit", "")))
                    or not RUN_URL_RE.fullmatch(str(evidence.get("run_url", "")))
                ):
                    raise ReviewerError("invalid operator alert disposition evidence")
                elif (disposition["kind"] == "recovered") != (evidence["commit"] == origin["commit"]):
                    raise ReviewerError("operator alert disposition does not match the evidence commit")
        if "delivered_sha256" in item and not SHA256_RE.fullmatch(str(item["delivered_sha256"])):
            raise ReviewerError("invalid operator alert delivered hash")
        if "edit_error" in item and item["edit_error"] not in {"conflict", "unavailable"}:
            raise ReviewerError("invalid operator alert edit error")
    return [dict(item) for item in items]


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def edit_zulip_message(message_id: int, content: str, *, expected_hash: str, email: str, api_key: str) -> str:
    """CAS update. A lost acknowledgment is repaired by matching desired content."""
    if type(message_id) is not int or message_id < 1 or not SHA256_RE.fullmatch(expected_hash):
        raise ReviewerError("invalid operator alert edit binding")
    auth = base64.b64encode(f"{email}:{api_key}".encode()).decode("ascii")
    headers = {"Authorization": f"Basic {auth}"}
    url = f"{ZULIP_MESSAGES_URL}/{message_id}"

    def request(method, suffix="", body=None):
        req = urllib.request.Request(
            url + suffix,
            headers=headers,
            method=method,
            data=urllib.parse.urlencode(body).encode() if body is not None else None,
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            raise ReviewerError("oversized Zulip message response")
        result = json.loads(raw)
        if result.get("result") != "success":
            raise ReviewerError("Zulip did not acknowledge the alert edit")
        return result

    try:
        current = request("GET", "?apply_markdown=false")["message"]["content"]
        if content_hash(current) == content_hash(content):
            return "delivered"
        if content_hash(current) != expected_hash:
            return "conflict"
        settings_request = urllib.request.Request(
            ZULIP_MESSAGES_URL.removesuffix("/messages") + "/server_settings", headers=headers)
        with urllib.request.urlopen(settings_request, timeout=30) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            return "unavailable"
        level = json.loads(raw).get("zulip_feature_level")
        if type(level) is not int or level < 379:
            return "unavailable"
        result = request("PATCH", body={"content": content, "prev_content_sha256": expected_hash})
        if "prev_content_sha256" in result.get("ignored_parameters_unsupported", []):
            return "unavailable"
        return "delivered"
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(16384))
        except (ValueError, OSError):
            detail = {}
        return "conflict" if detail.get("code") == "EXPECTATION_MISMATCH" else "unavailable"
    except (urllib.error.URLError, ValueError, KeyError, TypeError, AttributeError, ReviewerError):
        return "unavailable"
