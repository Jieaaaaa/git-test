import asyncio
import json
import sys
import time
import unittest
from unittest.mock import patch

from semantic_receiver import generate_adaptive_record


def packet(subject, *facts):
    return json.dumps({"groups": [{"subject": subject, "facts": list(facts)}]})


@unittest.skipIf(sys.version_info < (3, 11), "uses image Python 3.11")
class ProgressiveRecordTests(unittest.IsolatedAsyncioTestCase):
    passages = ["Alpha received 12 units in 2025. ", "Beta received 20 units in 2025. "]

    def setUp(self):
        self.splitter = patch("semantic_receiver.split_passages", side_effect=
                              lambda text, target_chars, max_sections=12:
                              [text] if max_sections == 3 else self.passages)
        self.splitter.start()
        self.addCleanup(self.splitter.stop)

    async def generate(self, request, budget=0.5, **kwargs):
        return await generate_adaptive_record("".join(self.passages), False,
                                               time.monotonic() + 8 + budget, request, **kwargs)

    async def test_global_candidate_requires_independent_source_review(self):
        calls = []

        async def request(messages, tokens, budget):
            system, user = [message["content"] for message in messages]
            calls.append(system)
            if "You verify a semantic" in system:
                self.assertIn("EVERY remaining numeric gap", system)
                return '{"verdict":"accept","repairs":[],"checks":[{"source_quote":"Alpha","candidate_quote":"Alpha"}]}'
            if user.startswith("SOURCE:"):
                return packet("Grants 2025", "Alpha 12 units; Beta 20 units")
            return packet("Longer factual grant report", "Alpha 12 units in 2025; Beta 20 units in 2025")

        self.assertEqual(await self.generate(request), "Grants 2025: Alpha 12 units; Beta 20 units")
        self.assertTrue(any("You verify a semantic" in item for item in calls))

    async def test_plain_compact_record_with_source_grounded_omission_repair(self):
        async def request(messages, tokens, budget):
            system = messages[0]["content"]
            if "You verify a semantic" in system:
                self.assertIn("No checks array", system)
                return json.dumps({"verdict": "repair", "repairs": [{"old": "",
                    "new": "Beta:20 units,2025", "source_quote": "Beta received 20 units in 2025"}]})
            if "SEMANTIC_LAYOUT_V2" in system:
                self.assertIn("plain text only", system)
                return "Alpha:12 units,2025"
            return packet("Longer factual grant report", "Alpha 12 units in 2025; Beta 20 units in 2025")

        self.assertEqual(await self.generate(request), "Alpha:12 units,2025\nBeta:20 units,2025")

    async def test_simple_audit_still_rejects_unrepaired_invented_value(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[]}'
            if "SEMANTIC_LAYOUT_V2" in messages[0]["content"]:
                return "Alpha 999 units; Beta 20 units,2025"
            return packet("Valid", "Alpha 12 units; Beta 20 units,2025")

        self.assertEqual(await self.generate(request), "Valid: Alpha 12 units; Beta 20 units,2025")

    async def test_rejected_global_uses_already_completed_packets(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"reject","repairs":[],"risk":"wrong entity binding"}'
            if messages[1]["content"].startswith("SOURCE:"):
                return packet("Bad", "Alpha 20 units; Beta 12 units,2025")
            section = messages[1]["content"].split("TARGET SECTION:\n")[1].split("\nFOLLOWING")[0]
            return packet("Alpha" if "Alpha" in section else "Beta",
                          "12 units in 2025" if "Alpha" in section else "20 units in 2025")

        result = await self.generate(request)
        self.assertIn("Alpha: 12 units in 2025", result)
        self.assertIn("Beta: 20 units in 2025", result)

    async def test_reviewed_nonnumeric_condition_wins_over_short_unreviewed_packets(self):
        source = "Alpha received 12 units in 2025 only after engineer signature. Beta received 20 units in 2025."

        async def request(messages, tokens, budget):
            system, user = [message["content"] for message in messages]
            if "You verify a semantic" in system:
                await asyncio.sleep(0.01)
                return json.dumps({"verdict": "repair", "repairs": [{"old": "Alpha 12 units",
                    "new": "Alpha 12 units only after engineer signature",
                    "source_quote": "Alpha received 12 units in 2025 only after engineer signature"}],
                    "checks": [{"source_quote": "engineer signature", "candidate_quote": "engineer signature"}]})
            return packet("Grants 2025", "Alpha 12 units; Beta 20 units")

        result = await generate_adaptive_record(source, False, time.monotonic() + 8.5, request)
        self.assertIn("only after engineer signature", result)

    async def test_critic_observes_validation_failure_and_repairs_its_review(self):
        attempts = []

        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                attempts.append(messages)
                if len(attempts) == 1:
                    return json.dumps({"verdict": "repair", "repairs": [{
                        "old": "", "new": "Alpha", "source_quote": "fabricated quote"}]})
                self.assertIn("VALIDATION TOOL: ungrounded_critic_repair", messages[-1]["content"])
                self.assertEqual(messages[-2]["role"], "assistant")
                return json.dumps({"verdict": "accept", "repairs": [], "checks": [{
                    "source_quote": "Alpha", "candidate_quotes": ["Alpha 12 units"]}]})
            if messages[1]["content"].startswith("SOURCE:"):
                return packet("Grants 2025", "Alpha 12 units; Beta 20 units")
            return packet("Longer factual grant report", "Alpha 12 units in 2025; Beta 20 units in 2025")

        self.assertEqual(await self.generate(request), "Grants 2025: Alpha 12 units; Beta 20 units")
        self.assertEqual(len(attempts), 2)

    async def test_one_stalled_section_does_not_erase_other_successful_packet(self):
        checkpoints = []
        cancelled = []

        async def request(messages, tokens, budget):
            if "TARGET SECTION:\nAlpha" in messages[1]["content"]:
                return packet("Alpha", "12 units in 2025")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        started = time.monotonic()
        result = await self.generate(request, on_checkpoint=checkpoints.append)
        self.assertEqual(result, "Alpha: 12 units in 2025")
        self.assertIn(result, checkpoints)
        self.assertTrue(cancelled)
        self.assertLess(time.monotonic() - started, 0.6)

    async def test_checkpoints_preserve_order_despite_out_of_order_completions(self):
        checkpoints = []

        async def request(messages, tokens, budget):
            user = messages[1]["content"]
            if user.startswith("SOURCE:"):
                raise ConnectionError()
            if "TARGET SECTION:\nAlpha" in user:
                await asyncio.sleep(0.02)
                return packet("Alpha", "12 units in 2025")
            return packet("Beta", "20 units in 2025")

        result = await self.generate(request, on_checkpoint=checkpoints.append)
        self.assertTrue(checkpoints[0].startswith("Beta"))
        self.assertLess(result.index("Alpha"), result.index("Beta"))

    async def test_short_packet_requests_have_bounded_concurrency(self):
        passages = ["Section %d has value %d. " % (i, i + 10) for i in range(10)]
        active = peak = 0

        async def request(messages, tokens, budget):
            nonlocal active, peak
            if messages[1]["content"].startswith("SOURCE:"):
                raise RuntimeError()
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                section = messages[1]["content"].split("TARGET SECTION:\n")[1].split("\nFOLLOWING")[0]
                return packet("Record", section)
            finally:
                active -= 1

        with patch("semantic_receiver.split_passages", return_value=passages):
            result = await generate_adaptive_record("".join(passages), False,
                                                     time.monotonic() + 9, request)
        for i in range(10):
            self.assertIn("Section %d has value %d" % (i, i + 10), result)
        self.assertEqual(peak, 4)
        self.assertEqual(active, 0)

    async def test_unsupported_numbers_and_malformed_packets_are_not_checkpoints(self):
        checkpoints = []

        async def request(messages, tokens, budget):
            if messages[1]["content"].startswith("SOURCE:"):
                return '{"groups":['
            return packet("Alpha", "999 units in 2025")

        with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
            await self.generate(request, on_checkpoint=checkpoints.append)
        self.assertEqual(checkpoints, [])

    async def test_numeric_gap_in_global_is_not_hidden_by_accept_verdict(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[],"checks":[{"source_quote":"Alpha","candidate_quote":"Alpha"}]}'
            if messages[1]["content"].startswith("SOURCE:"):
                return packet("Grants", "Alpha 12 units in 2025")
            return packet("Beta", "20 units in 2025")

        self.assertEqual(await self.generate(request), "Beta: 20 units in 2025")

    async def test_global_unsupported_number_can_only_be_published_after_source_repair(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                self.assertIn('"unsupported_numeric_atoms": ["999"]', messages[1]["content"])
                return json.dumps({"verdict": "repair", "repairs": [{
                    "old": "999", "new": "12", "source_quote": "Alpha received 12 units"}],
                    "checks": [{"source_quote": "Alpha", "candidate_quotes": ["Alpha 12 units"]}]})
            if messages[1]["content"].startswith("SOURCE:"):
                return packet("Grants 2025", "Alpha 999 units; Beta 20 units")
            return packet("Longer factual grant report", "Alpha 12 units in 2025; Beta 20 units in 2025")

        self.assertEqual(await self.generate(request), "Grants 2025: Alpha 12 units; Beta 20 units")

    async def test_all_model_failures_cannot_return_source(self):
        async def request(*args):
            raise RuntimeError("Gateway failed")

        with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
            await self.generate(request)

    async def test_cancellation_cleans_up_all_workers(self):
        started = asyncio.Event()
        active = 0

        async def request(*args):
            nonlocal active
            active += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        job = asyncio.create_task(self.generate(request))
        await started.wait()
        await asyncio.sleep(0)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(active, 0)

    async def test_encoded_input_has_no_second_percentage_summary(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[],"checks":[{"source_quote":"Alpha","candidate_quote":"Alpha"}]}'
            self.assertNotIn("24%", messages[0]["content"])
            self.assertNotIn("30%", messages[0]["content"])
            return packet("Grants 2025", "Alpha 12 units; Beta 20 units")

        self.assertIn("Alpha 12 units", await self.generate(request, encoded=True))


if __name__ == "__main__":
    unittest.main()
