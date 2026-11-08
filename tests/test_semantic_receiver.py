import asyncio
import json
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

from semantic_receiver import generate_adaptive_record


@unittest.skipIf(sys.version_info < (3, 11), "uses image Python 3.11")
class AdaptiveRecordTests(unittest.IsolatedAsyncioTestCase):
    source = ("Agency A received 12 units in 2025. Agency B was not approved. "
              + "Narrative framing and repeated context. " * 12)
    baseline = "Agency A received 12 units in 2025. Agency B was not approved."
    compact = "Agency A:12 units,2025; Agency B:not approved."

    def setUp(self):
        self.fallback = AsyncMock(return_value=self.baseline)
        self.coverage_patch = patch("semantic_receiver.generate_record", self.fallback)
        self.coverage_patch.start()
        self.addCleanup(self.coverage_patch.stop)

    def deadline(self, budget=4):
        return time.monotonic() + 8 + budget

    async def test_review_uses_latest_guide_and_coverage_is_already_running(self):
        current_guide = {}

        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                await asyncio.sleep(0)
                self.assertTrue(self.fallback.await_count)
                self.assertIn("INDEPENDENT COVERAGE DRAFT", messages[1]["content"])
                self.assertIn(self.baseline, messages[1]["content"])
                self.assertIn('"layout": "ledger"', messages[1]["content"])
                return '{"verdict":"accept","repairs":[]}'
            self.assertIn("24%", messages[0]["content"])
            current_guide["layout"] = "ledger"
            await asyncio.sleep(0)
            return self.compact

        result = await generate_adaptive_record(self.source, False, self.deadline(), request,
                                                guide_provider=lambda: current_guide)
        self.assertEqual(result, self.compact)

    async def test_source_grounded_repair_can_win(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return json.dumps({"verdict": "repair", "repairs": [
                    {"old": "approved", "new": "not approved",
                     "source_quote": "Agency B was not approved."}]})
            return "Agency A:12 units,2025; Agency B:approved."

        self.assertEqual(await generate_adaptive_record(self.source, False, self.deadline(), request),
                         self.compact)

    async def test_review_timeout_uses_independent_coverage(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                raise TimeoutError("slow review")
            return self.compact

        self.assertEqual(await generate_adaptive_record(self.source, False, self.deadline(), request),
                         self.baseline)

    async def test_explicit_rejection_never_publishes_compact_candidate(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"reject","repairs":[],"risk":"entity binding incorrect"}'
            return self.compact

        self.assertEqual(await generate_adaptive_record(self.source, False, self.deadline(), request),
                         self.baseline)
        self.fallback.side_effect = RuntimeError("coverage failed")
        with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
            await generate_adaptive_record(self.source, False, self.deadline(), request)

    async def test_missing_material_date_can_be_rejected_and_recovered(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                self.assertIn("2025", messages[1]["content"])
                return '{"verdict":"reject","repairs":[],"risk":"missing year"}'
            return "Agency A:12 units; Agency B:not approved."

        self.assertEqual(await generate_adaptive_record(self.source, False, self.deadline(), request),
                         self.baseline)

    async def test_overlong_candidate_skips_review(self):
        prompts = []

        async def request(messages, tokens, budget):
            prompts.append(messages[0]["content"])
            return "A faithful record: " + self.source

        self.assertEqual(await generate_adaptive_record(self.source, False, self.deadline(), request),
                         self.baseline)
        self.assertEqual(len(prompts), 1)

    async def test_completed_coverage_does_not_wait_for_hung_compiler(self):
        cancelled = asyncio.Event()

        async def request(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        started = time.monotonic()
        result = await generate_adaptive_record(self.source, False, self.deadline(0.4), request)
        self.assertEqual(result, self.baseline)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(cancelled.is_set())

    async def test_completed_coverage_does_not_wait_for_hung_review(self):
        cancelled = asyncio.Event()

        async def request(messages, tokens, budget):
            if "You verify a semantic" not in messages[0]["content"]:
                return self.compact
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        started = time.monotonic()
        self.assertEqual(await generate_adaptive_record(self.source, False, self.deadline(0.4), request),
                         self.baseline)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(cancelled.is_set())

    async def test_model_draft_survives_unavailable_review_when_coverage_failed(self):
        self.fallback.side_effect = RuntimeError("coverage failed")

        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                await asyncio.Event().wait()
            return self.compact

        self.assertEqual(await generate_adaptive_record(self.source, False, self.deadline(0.15), request),
                         self.compact)

    async def test_unreviewed_draft_with_missing_numbers_is_not_emergency_success(self):
        self.fallback.side_effect = RuntimeError("coverage failed")

        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                raise TimeoutError()
            return "Agency A:12 units; Agency B:not approved."

        with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
            await generate_adaptive_record(self.source, False, self.deadline(), request)

    async def test_encoded_input_is_not_compressed_a_second_time(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return '{"verdict":"accept","repairs":[]}'
            self.assertNotIn("24%", messages[0]["content"])
            self.assertIn("Do not summarize it again", messages[0]["content"])
            return self.compact

        result = await generate_adaptive_record(self.compact, False, self.deadline(), request,
                                                encoded=True, original_bytes=7000)
        self.assertEqual(result, self.compact)

    async def test_all_model_paths_failed_never_return_original(self):
        self.fallback.side_effect = ConnectionError("model unavailable")

        async def failed(*args):
            raise ConnectionError("model unavailable")

        with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
            await generate_adaptive_record(self.source, False, self.deadline(), failed)

    async def test_cancellation_cleans_up_both_model_paths(self):
        started = asyncio.Event()
        stopped = []

        async def hanging(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

        self.fallback.side_effect = hanging
        job = asyncio.create_task(generate_adaptive_record(self.source, False, self.deadline(), hanging))
        await started.wait()
        await asyncio.sleep(0)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(len(stopped), 2)


if __name__ == "__main__":
    unittest.main()
