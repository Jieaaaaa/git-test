import asyncio
import json
import sys
import time
import unittest
from unittest.mock import patch

from semantic_agent import (byte_size, encode_for_transport, grounded_repairs, number_gaps,
                            audit_obligations, merge_grounded_reviews, pack_source, refine_record, unpack_source)


class GroundingTests(unittest.TestCase):
    def test_exclusion_audit_prioritizes_missing_clauses_and_deduplicates_templates(self):
        source = "负责人没有批准。低于80%暂停扩建，而不是停止日常监测。低于90%暂停扩建，而不是停止日常监测。"
        checks = audit_obligations(source, "负责人没有批准；低于阈值暂停扩建。")
        self.assertEqual(checks[0]["source_quote"], "而不是停止日常监测")
        self.assertEqual(len(checks), 2)
        self.assertTrue(all(check["source_quote"] in source for check in checks))

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

    def test_independent_reviews_merge_identical_shared_rule_corrections(self):
        source = "Alpha requires approval. Beta requires approval."
        review = {"verdict": "repair", "repairs": [
            {"old": "permit", "new": "approval", "source_quote": "requires approval"},
            {"old": "", "new": "Alpha requires approval", "source_quote": "Alpha requires approval"}]}
        self.assertEqual(merge_grounded_reviews(source, "All require permit", [review, review]),
                         "All require approval\nAlpha requires approval")

    def test_independent_reviews_reject_conflicting_corrections(self):
        source = "Alpha approved; Beta rejected."
        reviews = [{"verdict": "repair", "repairs": [
            {"old": "pending", "new": outcome, "source_quote": source}]}
            for outcome in ("approved", "rejected")]
        with self.assertRaisesRegex(ValueError, "conflicting_critic_repairs"):
            merge_grounded_reviews(source, "Decision pending", reviews)

    def test_independent_reviews_reject_overlapping_original_targets(self):
        source = "Alpha requires signed approval."
        reviews = [{"verdict": "repair", "repairs": [
            {"old": old, "new": new, "source_quote": source}]}
            for old, new in (("requires approval", "requires signed approval"),
                             ("approval", "signed approval"))]
        with self.assertRaisesRegex(ValueError, "overlapping_critic_repairs"):
            merge_grounded_reviews(source, "Alpha requires approval", reviews)

    def test_independent_replacements_use_original_positions(self):
        source = "Alpha blue. Beta green."
        reviews = [{"verdict": "repair", "repairs": [
            {"old": old, "new": new, "source_quote": source}]}
            for old, new in (("red", "blue"), ("blue", "green"))]
        self.assertEqual(merge_grounded_reviews(source, "Alpha red. Beta blue.", reviews),
                         "Alpha blue. Beta green.")

    def test_independent_reviews_preserve_distinct_appends(self):
        source = "Alpha approved. Beta requires signed approval."
        reviews = [{"verdict": "repair", "repairs": [
            {"old": "", "new": new, "source_quote": source}]}
            for new in ("Alpha approved", "Beta requires signed approval", "Alpha approved")]
        self.assertEqual(merge_grounded_reviews(source, "Decisions", reviews),
                         "Decisions\nAlpha approved\nBeta requires signed approval")

    def test_independent_reviews_validate_each_source_quote(self):
        review = {"verdict": "repair", "repairs": [
            {"old": "pending", "new": "approved", "source_quote": "Invented evidence"}]}
        with self.assertRaisesRegex(ValueError, "ungrounded_critic_repair"):
            merge_grounded_reviews("Alpha approved", "Alpha pending", [review])

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

    def test_dotted_dates_are_equivalent_without_corrupting_decimal_values(self):
        self.assertEqual(number_gaps("2025.05.07费用12.50元", "2025-5-7费用12.5元"), [])
        self.assertEqual(number_gaps("2025.05.07费用12.50元", "2025-5-7费用12元"), ["12.5"])
        self.assertEqual(number_gaps("12 units in 2025", "12,2025"), [])

    @unittest.skipIf(sys.version_info < (3, 11), "uses image Python 3.11")
    def test_strict_critic_requires_source_grounded_numeric_accounting(self):
        from semantic_agent import verify_candidate

        async def request(messages, tokens, budget):
            return '{"verdict":"accept","repairs":[]}'

        with self.assertRaisesRegex(ValueError, "unaccounted_numeric_omission"):
            asyncio.run(verify_candidate("Record 7: Alpha received 12 in 2025", "Alpha:12;2025",
                request, time.monotonic() + 20, require_numeric_accounting=True))

        async def accounted(messages, tokens, budget):
            return json.dumps({"verdict": "accept", "repairs": [], "nonmaterial_numbers": [
                {"number": "7", "reason": "record index", "source_quote": "Record 7"}]})

        text, _ = asyncio.run(verify_candidate("Record 7: Alpha received 12 in 2025", "Alpha:12;2025",
            accounted, time.monotonic() + 20, require_numeric_accounting=True))
        self.assertEqual(text, "Alpha:12;2025")


@unittest.skipIf(sys.version_info < (3, 11), "uses image Python 3.11")
class SemanticAgentTests(unittest.IsolatedAsyncioTestCase):
    source = "Agency A received 12 units in 2025. Agency B was not approved. " + "Narrative framing. " * 30
    baseline = "Agency A received 12 units in 2025. Agency B was not approved. " + "Repeated context. " * 10

    def deadline(self):
        return time.monotonic() + 20

    async def test_numeric_only_evidence_does_not_prove_a_condition(self):
        from semantic_agent import verify_candidate

        async def request(messages, tokens, budget):
            return json.dumps({"verdict": "accept", "repairs": [], "checks": [{
                "source_quote": "Approval after 12 days", "candidate_quotes": ["12"]}]})

        with self.assertRaisesRegex(ValueError, "critic_evidence_only_numbers"):
            await verify_candidate("Approval after 12 days", "Approval:12 days", request,
                                   self.deadline(), require_evidence=True)

    async def test_critic_must_audit_tool_selected_exclusion(self):
        from semantic_agent import verify_candidate

        source = "Suspend expansion, not daily monitoring."

        async def missing(messages, tokens, budget):
            self.assertIn("required_checks", messages[1]["content"])
            return json.dumps({"verdict": "accept", "repairs": [], "checks": [{
                "source_quote": "Suspend expansion", "candidate_quotes": ["Do not expand"]}]})

        with self.assertRaisesRegex(ValueError, "critic_missing_required_check"):
            await verify_candidate(source, "Do not expand", missing,
                                   self.deadline(), require_evidence=True)

        async def repaired(messages, tokens, budget):
            observation = json.loads(messages[1]["content"].split("TOOL OBSERVATION:\n")[1])
            obligation = observation["required_checks"][0]
            return json.dumps({"verdict": "repair", "repairs": [{"old": "",
                "new": "Do not suspend daily monitoring", "source_quote": obligation["source_quote"]}],
                "checks": [{**obligation, "candidate_quotes": ["Do not suspend daily monitoring"]}]})

        result, _ = await verify_candidate(source, "Do not expand", repaired,
                                           self.deadline(), require_evidence=True)
        self.assertIn("Do not suspend daily monitoring", result)

    async def test_critic_cannot_accept_misaligned_table(self):
        from semantic_agent import verify_candidate

        source = "Alpha: temperature above 31 degrees for 11 minutes."
        candidate = "Station | temperature duration: Alpha | 31 | 11"

        async def request(messages, tokens, budget):
            self.assertIn("expected 2 columns", messages[1]["content"])
            return json.dumps({"verdict": "accept", "repairs": [], "checks": [{
                "source_quote": "Alpha", "candidate_quotes": ["Alpha"]}]})

        with self.assertRaisesRegex(ValueError, "critic_table_shape_invalid"):
            await verify_candidate(source, candidate, request, self.deadline(), require_evidence=True)

        async def repaired(messages, tokens, budget):
            return json.dumps({"verdict": "repair", "repairs": [{
                "old": "temperature duration", "new": "temperature degrees | duration minutes",
                "source_quote": "temperature above 31 degrees for 11 minutes"}],
                "checks": [{"source_quote": "Alpha", "candidate_quotes": ["Alpha"]}]})

        result, _ = await verify_candidate(source, candidate, repaired, self.deadline(), require_evidence=True)
        self.assertIn("temperature degrees | duration minutes", result)

    async def test_scoped_critic_can_reference_table_fields_and_other_article_numbers(self):
        from semantic_agent import verify_candidate

        source = "Alpha received 12 units only after approval."
        whole = source + " Beta received 20 units."
        candidate = "Units after approval: Alpha 12; Beta 20"

        async def request(messages, tokens, budget):
            self.assertIn("SOURCE EXCERPT", messages[0]["content"])
            self.assertNotIn("against its only allowed SOURCE", messages[0]["content"])
            self.assertIn("WHOLE ARTICLE", messages[1]["content"])
            return json.dumps({"verdict": "accept", "repairs": [], "checks": [{
                "source_quote": "12 units only after approval",
                "candidate_quotes": ["Units after approval", "Alpha 12"]}]})

        result, _ = await verify_candidate(source, candidate, request, self.deadline(),
            allowed_source=whole, require_evidence=True, require_numeric_accounting=True)
        self.assertEqual(result, candidate)

    async def test_critic_cannot_concatenate_distant_fields_into_an_exact_quote(self):
        from semantic_agent import verify_candidate

        async def request(messages, tokens, budget):
            return json.dumps({"verdict": "accept", "repairs": [], "checks": [{
                "source_quote": "Alpha received 12 units",
                "candidate_quotes": ["Units Alpha 12"]}]})

        with self.assertRaisesRegex(ValueError, "invalid_critic_evidence"):
            await verify_candidate("Alpha received 12 units", "Units: Alpha 12", request,
                                   self.deadline(), require_evidence=True)

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
