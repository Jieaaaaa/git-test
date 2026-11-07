import asyncio
import json
import sys
import time
import unittest
from unittest.mock import patch

from semantic_agent import (byte_size, encode_for_transport, grounded_repairs, number_gaps,
                            pack_source, refine_record, unpack_source)


class GroundingTests(unittest.TestCase):
    def test_envelope_preserves_source_budget_and_unicode(self):
        source = "甲公司：2025年获批12万元。"
        view = unpack_source(pack_source(source, 9000))
        self.assertEqual(view.text, source)
        self.assertEqual(view.original_bytes, 9000)
        self.assertTrue(view.encoded)
        self.assertEqual(unpack_source(source).original_bytes, byte_size(source))

    def test_malformed_or_unbounded_metadata_is_rejected(self):
        for text in ('SCNSEM1 {}\nrecord', 'SCNSEM1 {"version":1,"source_bytes":999999999}\nrecord',
                     'SCNSEM1 {"version":1,"source_bytes":12}'):
            with self.assertRaises(ValueError):
                unpack_source(text)

    def test_reserved_prefix_in_original_is_preserved_by_raw_escape(self):
        for original in ("SCNSEM1 not-json\nActual article", pack_source("Article body", 5000)):
            view = unpack_source(pack_source(original, byte_size(original), encoded=False))
            self.assertEqual(view.text, original)
            self.assertFalse(view.encoded)

    def test_critic_cannot_attach_invented_source_quote(self):
        response = {"verdict": "repair", "repairs": [{"old": "", "new": "Approved 2025",
                                                        "source_quote": "invented evidence"}]}
        with self.assertRaisesRegex(ValueError, "ungrounded"):
            grounded_repairs("Rejected 2025", "Decision 2025", response)

    def test_grounded_correction_can_add_negation(self):
        source = "Agency A was not approved in 2025."
        response = {"verdict": "repair", "repairs": [{"old": "approved", "new": "not approved",
                                                        "source_quote": source}]}
        self.assertEqual(grounded_repairs(source, "A approved in 2025", response), "A not approved in 2025")

    def test_numeric_coverage_is_not_claimed_from_model_confidence(self):
        self.assertEqual(number_gaps("Approved 12 in 2025, not 20", "12 in 2025"), ["20"])


@unittest.skipIf(sys.version_info < (3, 11), "uses image Python 3.11")
class SemanticAgentTests(unittest.IsolatedAsyncioTestCase):
    source = "Agency A received 12 units in 2025. Agency B was not approved. " + "Narrative framing. " * 30
    baseline = "Agency A received 12 units in 2025. Agency B was not approved. " + "Repeated context. " * 10

    def deadline(self):
        return time.monotonic() + 20

    async def test_shorter_candidate_needs_independent_critic(self):
        calls = []

        async def request(messages, tokens, budget):
            calls.append(messages[0]["content"])
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[],"risk":"none"}'
            return json.dumps({"action": "propose", "strategy": "entity ledger",
                               "content": "A:12 units,2025; B:not approved.", "risk": "none"})

        result = await refine_record(self.source, self.baseline, request, self.deadline())
        self.assertEqual(result, "A:12 units,2025; B:not approved.")
        self.assertEqual(len(calls), 2)

    async def test_planner_keep_action_preserves_completed_baseline(self):
        async def request(messages, tokens, budget):
            return '{"action":"keep","risk":"dense source"}'

        self.assertEqual(await refine_record(self.source, self.baseline, request, self.deadline()), self.baseline)

    async def test_critic_rejection_does_not_replace_good_record(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"reject","repairs":[],"risk":"lost negation"}'
            return '{"action":"propose","strategy":"short","content":"A:12 units,2025; B:approved"}'

        self.assertEqual(await refine_record(self.source, self.baseline, request, self.deadline()), self.baseline)

    async def test_false_acceptance_with_missing_number_is_rejected(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[],"risk":"none"}'
            return '{"action":"propose","strategy":"short","content":"A:12 units; B:not approved"}'

        self.assertEqual(await refine_record(self.source, self.baseline, request, self.deadline()), self.baseline)

    async def test_false_acceptance_that_drops_all_negation_is_rejected(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[],"risk":"none"}'
            return '{"action":"propose","strategy":"short","content":"A:12 units,2025; B:approved"}'

        self.assertEqual(await refine_record(self.source, self.baseline, request, self.deadline()), self.baseline)

    async def test_sender_outputs_only_checked_encoding(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[],"risk":"none"}'
            return '{"strategy":"entity ledger","content":"A:12 units,2025; B:not approved","risk":"none"}'

        payload = await encode_for_transport(self.source, request, self.deadline())
        view = unpack_source(payload.decode())
        self.assertIn("B:not approved", view.text)
        self.assertEqual(view.original_bytes, byte_size(self.source))
        self.assertLess(len(payload), byte_size(self.source) * 0.82)

    async def test_sender_rejects_oversized_encoding_even_if_critic_accepts(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[],"risk":"none"}'
            return json.dumps({"strategy": "verbose", "content": "Record " + self.source})

        with self.assertRaisesRegex(ValueError, "saving_too_small"):
            await encode_for_transport(self.source, request, self.deadline())

    async def test_critic_timeout_preserves_receiver_baseline(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                raise TimeoutError()
            return '{"action":"propose","strategy":"short","content":"A:12 units,2025; B:not approved"}'

        self.assertEqual(await refine_record(self.source, self.baseline, request, self.deadline()), self.baseline)

    async def test_reflection_uses_rejection_reason_to_repair_next_proposal(self):
        turns = 0

        async def request(messages, tokens, budget):
            nonlocal turns
            prompt = messages[0]["content"]
            if "You verify a semantic" in prompt:
                return '{"verdict":"accept","repairs":[],"risk":"none"}'
            turns += 1
            if turns == 1:
                return '{"action":"propose","strategy":"ledger","content":"A:12 units; B:not approved"}'
            self.assertIn("numeric_coverage_not_restored", messages[1]["content"])
            return '{"action":"propose","strategy":"dated ledger","content":"A:12 units,2025; B:not approved"}'

        result = await refine_record(self.source, self.baseline, request, self.deadline())
        self.assertEqual(turns, 2)
        self.assertIn("2025", result)
        self.assertLess(byte_size(result), byte_size(self.baseline))

    async def test_expired_refinement_budget_never_erases_valid_baseline(self):
        async def request(*args):
            raise AssertionError("must not request with expired deadline")

        self.assertEqual(await refine_record(self.source, self.baseline, request, time.monotonic()), self.baseline)


if __name__ == "__main__":
    unittest.main()
