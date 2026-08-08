"""Normalize and price review-engine usage evidence.

This module owns the durable spend-document boundary. It receives engine event
text, pass evidence, and an explicit measurement time; it performs no
filesystem, subprocess, network, clock, or state-repository work.
"""

from __future__ import annotations

import json
from typing import Any

# The one model production runs, at the provider's current USD prices per
# million tokens. These values are deliberately not copied into the durable
# usage record: that record retains the engine evidence, not a vendor estimate.
GPT_5_6_SOL_MODEL = "codex:gpt-5.6-sol"
GPT_5_6_SOL_INPUT_USD_PER_MTOK = 5.00
GPT_5_6_SOL_CACHED_INPUT_USD_PER_MTOK = 0.50
GPT_5_6_SOL_CACHE_WRITE_INPUT_USD_PER_MTOK = 1.25 * GPT_5_6_SOL_INPUT_USD_PER_MTOK
GPT_5_6_SOL_OUTPUT_USD_PER_MTOK = 30.00
GPT_5_6_SOL_LONG_CONTEXT_INPUT_TOKENS = 272_000
CODEX_REQUIRED_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def usage_problem(usage: Any) -> str | None:
    """Why a raw turn aggregate cannot be split into the four billed categories."""
    if not isinstance(usage, dict):
        return "completed-turn usage is not an object"
    for key in CODEX_REQUIRED_USAGE_KEYS:
        value = usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return f"completed-turn usage has no valid {key}"
    if usage["cached_input_tokens"] + usage["cache_write_input_tokens"] > usage["input_tokens"]:
        return "cache-read and cache-write tokens exceed total input tokens"
    if usage["reasoning_output_tokens"] > usage["output_tokens"]:
        return "reasoning tokens exceed total output tokens"
    if "total_tokens" in usage:
        total = usage["total_tokens"]
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            return "completed-turn usage has no valid total_tokens"
        if total != usage["input_tokens"] + usage["output_tokens"]:
            return "total_tokens does not equal input_tokens plus output_tokens"
    return None


def unavailable_usage(engine: str) -> dict[str, Any]:
    """A durable statement that this engine exposed no token-usage contract."""
    return {
        "usage_status": "unavailable",
        "usage_reason": f"{engine} did not report token usage through this runner",
        "turns": [],
    }


def priceable_turn_usage(evidence: dict[str, Any]) -> dict[str, int] | None:
    """One valid turn aggregate known to contain no long-context request."""
    if evidence.get("usage_status") != "recorded":
        return None
    turns = evidence.get("turns")
    if not isinstance(turns, list) or len(turns) != 1:
        return None
    usage = turns[0]
    if usage_problem(usage) is not None:
        return None
    if usage["input_tokens"] > GPT_5_6_SOL_LONG_CONTEXT_INPUT_TOKENS:
        # A Codex turn may cover several model requests. Above the threshold,
        # its aggregate does not reveal which requests received tiered pricing.
        return None
    return usage


def usage_cost(model: str, evidence: dict[str, Any]) -> float | None:
    """Exact current USD for one provably base-rate turn, otherwise None."""
    if model != GPT_5_6_SOL_MODEL:
        return None
    usage = priceable_turn_usage(evidence)
    if usage is None:
        return None
    cached = usage["cached_input_tokens"]
    cache_write = usage["cache_write_input_tokens"]
    ordinary = usage["input_tokens"] - cached - cache_write
    total_per_million = (
        ordinary * GPT_5_6_SOL_INPUT_USD_PER_MTOK
        + cached * GPT_5_6_SOL_CACHED_INPUT_USD_PER_MTOK
        + cache_write * GPT_5_6_SOL_CACHE_WRITE_INPUT_USD_PER_MTOK
        + usage["output_tokens"] * GPT_5_6_SOL_OUTPUT_USD_PER_MTOK
    )
    return total_per_million / 1_000_000


def codex_usage(events: str) -> dict[str, Any]:
    """Preserve completed-turn aggregates without mistaking them for requests."""
    turns: list[Any] = []
    for line in events.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        # Keep the emitted object exactly, including total_tokens and future
        # fields. None or another malformed value is evidence too, not a reason
        # to discard the successfully produced review.
        turns.append(event.get("usage"))
    if not turns:
        return {
            "usage_status": "unavailable",
            "usage_reason": "Codex emitted no completed-turn usage",
            "turns": [],
        }
    if len(turns) > 1:
        return {
            "usage_status": "multiple",
            "usage_reason": f"Codex emitted {len(turns)} completed-turn usage records",
            "turns": turns,
        }
    problem = usage_problem(turns[0])
    return {
        "usage_status": "invalid" if problem else "recorded",
        "usage_reason": problem,
        "turns": turns,
    }


def review_spend(
    model_id: str,
    passes: list[dict[str, Any]],
    *,
    measured_at: str,
) -> dict[str, Any]:
    """Build durable turn evidence stamped as canonical UTC seconds.

    ``measured_at`` is supplied by orchestration in ``YYYY-MM-DDTHH:MM:SSZ``
    form. This pure module deliberately does not read the clock itself.
    """
    return {
        "schema_version": 2,
        "model": model_id,
        "measured_at": measured_at,
        "passes": passes,
    }


def review_usage_totals(accounting: dict[str, Any]) -> dict[str, int] | None:
    """Aggregate only turns that are all provably on the linear base tier."""
    totals = dict.fromkeys(CODEX_REQUIRED_USAGE_KEYS, 0)
    for entry in accounting["passes"]:
        usage = priceable_turn_usage(entry)
        if usage is None:
            return None
        for key in CODEX_REQUIRED_USAGE_KEYS:
            totals[key] += usage[key]
    return totals


def review_cost(accounting: dict[str, Any]) -> float | None:
    """Current exact total only when every turn is provably on the base tier."""
    costs = [usage_cost(accounting["model"], entry) for entry in accounting["passes"]]
    if not costs or any(cost is None for cost in costs):
        return None
    return sum(cost for cost in costs if cost is not None)


def review_pricing_problem(accounting: dict[str, Any]) -> str:
    """Explain why the retained evidence cannot produce an exact current price."""
    if accounting["model"] != GPT_5_6_SOL_MODEL:
        return f"no current price is configured for {accounting['model']}"
    if not accounting["passes"]:
        return "no model passes were recorded"
    for entry in accounting["passes"]:
        if entry.get("usage_status") != "recorded":
            return str(entry.get("usage_reason") or "turn usage was not recorded")
        turns = entry.get("turns")
        if not isinstance(turns, list) or len(turns) != 1:
            return "the pass does not contain exactly one completed-turn usage record"
        problem = usage_problem(turns[0])
        if problem:
            return problem
        if turns[0]["input_tokens"] > GPT_5_6_SOL_LONG_CONTEXT_INPUT_TOKENS:
            return (
                "a completed-turn aggregate exceeds 272,000 input tokens, but Codex "
                "does not expose its constituent request boundaries"
            )
    return "the retained usage cannot be priced exactly"


def spend_summary(accounting: dict[str, Any]) -> str:
    """Render the operator summary without changing the durable evidence."""
    usage = review_usage_totals(accounting)
    usd = review_cost(accounting)
    if usage is None or usd is None:
        return (
            "Usage evidence retained in spend.json; exact current USD is unavailable: "
            f"{review_pricing_problem(accounting)}."
        )
    ordinary = usage["input_tokens"] - usage["cached_input_tokens"] - usage["cache_write_input_tokens"]
    tokens = (
        f"{usage['input_tokens']:,} in "
        f"({ordinary:,} ordinary, {usage['cached_input_tokens']:,} cached, "
        f"{usage['cache_write_input_tokens']:,} cache write), "
        f"{usage['output_tokens']:,} out"
    )
    return f"Spend: {tokens} — ${usd:.2f} at current base list prices."
