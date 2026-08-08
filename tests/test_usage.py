import json
import unittest

from palomar_reviewer import usage as usage_accounting


class UsageAccountingTests(unittest.TestCase):
    """Turn aggregates are retained faithfully and priced only when sufficient."""

    measured_at = "2026-08-08T00:00:00Z"

    def usage(
        self,
        *,
        input_tokens,
        cached=0,
        cache_write=0,
        output=0,
        reasoning=0,
        total=None,
    ):
        usage = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
        }
        if total is not None:
            usage["total_tokens"] = total
        return usage

    def evidence(self, usage):
        return {"usage_status": "recorded", "usage_reason": None, "turns": [usage]}

    def entry(self, step, usage):
        return {"step": step, **self.evidence(usage)}

    def event(self, usage):
        return json.dumps({"type": "turn.completed", "usage": usage})

    def accounting(self, model, passes):
        return usage_accounting.review_spend(
            model,
            passes,
            measured_at=self.measured_at,
        )

    def test_verified_turn_aggregate_shape_is_not_mistaken_for_one_request(self):
        # One production Codex diagnostic made four requests, then emitted one
        # completed-turn aggregate. The request inputs sum exactly to the turn.
        request_inputs = [15_611, 15_699, 15_772, 15_843]
        aggregate = self.usage(
            input_tokens=62_925,
            cached=52_224,
            output=163,
            total=63_088,
        )
        evidence = usage_accounting.codex_usage(self.event(aggregate))
        self.assertEqual(evidence["usage_status"], "recorded")
        self.assertEqual(evidence["turns"], [aggregate])
        self.assertEqual(evidence["turns"][0]["total_tokens"], 63_088)
        self.assertEqual(sum(request_inputs), aggregate["input_tokens"])
        self.assertLess(max(request_inputs), aggregate["input_tokens"])

    def test_base_categories_are_exact_at_272000_total_input(self):
        turn = self.usage(
            input_tokens=272_000,
            cached=100_000,
            cache_write=20_000,
            output=10_000,
            reasoning=4_000,
        )
        # $0.76 ordinary + $0.05 cached + $0.125 cache write + $0.30 output.
        self.assertAlmostEqual(
            usage_accounting.usage_cost(
                usage_accounting.GPT_5_6_SOL_MODEL,
                self.evidence(turn),
            ),
            1.235,
        )

    def test_turn_aggregate_above_272000_is_not_exactly_priceable(self):
        evidence = self.evidence(self.usage(input_tokens=272_001, output=10))
        self.assertIsNone(usage_accounting.usage_cost(usage_accounting.GPT_5_6_SOL_MODEL, evidence))
        accounting = self.accounting(
            usage_accounting.GPT_5_6_SOL_MODEL,
            [{"step": "metadata", **evidence}],
        )
        self.assertIsNone(usage_accounting.review_cost(accounting))
        self.assertIn("aggregate exceeds 272,000", usage_accounting.spend_summary(accounting))
        self.assertIn("request boundaries", usage_accounting.spend_summary(accounting))

    def test_review_aggregate_never_controls_the_request_tier(self):
        accounting = self.accounting(
            usage_accounting.GPT_5_6_SOL_MODEL,
            [
                self.entry("metadata", self.usage(input_tokens=200_000)),
                self.entry("synthesis", self.usage(input_tokens=200_000)),
            ],
        )
        self.assertAlmostEqual(usage_accounting.review_cost(accounting), 2.0)
        one_turn = self.evidence(self.usage(input_tokens=400_000))
        self.assertIsNone(usage_accounting.usage_cost(usage_accounting.GPT_5_6_SOL_MODEL, one_turn))

    def test_missing_and_malformed_usage_is_retained_without_raising(self):
        unavailable = usage_accounting.codex_usage("")
        self.assertEqual(unavailable["usage_status"], "unavailable")
        self.assertEqual(unavailable["turns"], [])

        absent = usage_accounting.codex_usage('{"type":"turn.completed"}')
        self.assertEqual(absent["usage_status"], "invalid")
        self.assertEqual(absent["turns"], [None])

        malformed = {"input_tokens": 10, "cached_input_tokens": 0}
        evidence = usage_accounting.codex_usage(self.event(malformed))
        self.assertEqual(evidence["usage_status"], "invalid")
        self.assertEqual(evidence["turns"], [malformed])
        self.assertIn("cache_write_input_tokens", evidence["usage_reason"])
        self.assertIsNone(usage_accounting.usage_cost(usage_accounting.GPT_5_6_SOL_MODEL, evidence))

    def test_contradictory_usage_is_retained_without_raising(self):
        contradictory = self.usage(input_tokens=10, cached=8, cache_write=3)
        evidence = usage_accounting.codex_usage(self.event(contradictory))
        self.assertEqual(evidence["usage_status"], "invalid")
        self.assertEqual(evidence["turns"], [contradictory])
        self.assertIn("exceed total input", evidence["usage_reason"])

    def test_multiple_completed_turns_are_preserved_and_unpriceable(self):
        first = self.usage(input_tokens=10, output=2)
        second = self.usage(input_tokens=20, output=3)
        evidence = usage_accounting.codex_usage("\n".join([self.event(first), self.event(second)]))
        self.assertEqual(evidence["usage_status"], "multiple")
        self.assertEqual(evidence["turns"], [first, second])
        self.assertIsNone(usage_accounting.usage_cost(usage_accounting.GPT_5_6_SOL_MODEL, evidence))

    def test_non_codex_usage_is_unavailable_not_zero(self):
        evidence = usage_accounting.unavailable_usage("claude")
        self.assertEqual(evidence["usage_status"], "unavailable")
        self.assertEqual(evidence["turns"], [])
        accounting = self.accounting(
            "claude:default",
            [{"step": "metadata", **evidence}],
        )
        self.assertIsNone(usage_accounting.review_cost(accounting))
        self.assertNotIn("0 in", usage_accounting.spend_summary(accounting))
        self.assertIn("no current price", usage_accounting.spend_summary(accounting))

    def test_durable_accounting_keeps_raw_passes_time_and_no_vendor_dollars(self):
        turn = self.usage(
            input_tokens=100,
            cached=30,
            cache_write=20,
            output=10,
            total=110,
        )
        passes = [self.entry("metadata", turn)]
        accounting = self.accounting(usage_accounting.GPT_5_6_SOL_MODEL, passes)
        self.assertEqual(accounting["schema_version"], 2)
        self.assertEqual(accounting["measured_at"], self.measured_at)
        self.assertEqual(accounting["passes"], passes)
        self.assertEqual(accounting["passes"][0]["turns"], [turn])
        self.assertEqual(accounting["passes"][0]["usage_status"], "recorded")
        self.assertNotIn("usd", accounting)
        self.assertNotIn("usd", accounting["passes"][0])


if __name__ == "__main__":
    unittest.main()
