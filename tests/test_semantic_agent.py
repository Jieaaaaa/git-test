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
    def test_accepted_critic_allows_equivalent_date_and_thousands_format(self):
        from semantic_agent import verify_candidate

        source = "甲公司于２０２５年５月７日取得1,000.00万元；乙公司未获批。"
        candidate = "甲公司:2025-5-7取得1000万元；乙公司:未获批。"

        async def request(messages, tokens, budget):
            return '{"verdict":"accept","repairs":[]}'

        result, verdict = asyncio.run(verify_candidate(source, candidate, request, time.monotonic() + 20))
        self.assertEqual((result, verdict), (candidate, "accept"))
        self.assertEqual(number_gaps(source, candidate), [])

    def test_numeric_normalization_still_detects_missing_real_value(self):
        self.assertEqual(number_gaps("2025年5月7日取得1,000.00万元", "2025-5-7取得100万元"), ["1000"])

    def test_equivalent_dates_separators_and_fullwidth_digits(self):
        self.assertEqual(number_gaps("２０２５年５月７日，1,000.00元", "2025-5-7:1000元"), [])
        self.assertEqual(number_gaps("2025-5-7: 1,200.50元", "2025年5月7日：1200.5元"), [])

    def test_numeric_normalization_still_detects_changed_values(self):
        self.assertEqual(number_gaps("1,200.50元", "1200元"), ["1200.5"])


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

    async def test_model_critic_can_accept_a_nonmaterial_numeric_omission(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[],"risk":"none"}'
            return '{"action":"propose","strategy":"short","content":"A:12 units; B:not approved"}'

        result = await refine_record(self.source, self.baseline, request, self.deadline())
        self.assertEqual(result, "A:12 units; B:not approved")

    async def test_critic_repairs_a_material_numeric_omission_with_source_evidence(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return json.dumps({"verdict": "repair", "repairs": [{"old": "", "new": "A received in 2025",
                    "source_quote": "Agency A received 12 units in 2025"}], "risk": "missing date"})
            return '{"action":"propose","strategy":"short","content":"A:12 units; B:not approved"}'

        result = await refine_record(self.source, self.baseline, request, self.deadline())
        self.assertIn("2025", result)
        self.assertLess(byte_size(result), byte_size(self.baseline))

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
                return json.dumps({"action": "propose", "strategy": "ledger", "content": self.baseline})
            self.assertIn("proposal_did_not_reduce_measured_bytes", messages[1]["content"])
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
