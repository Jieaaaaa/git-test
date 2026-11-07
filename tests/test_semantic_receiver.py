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
    compact = "A:12 units,2025; B:not approved."

    def deadline(self):
        return time.monotonic() + 12

    async def test_compact_record_requires_review_and_uses_latest_guide(self):
        prompts = []
        current_guide = {}

        async def request(messages, tokens, budget):
            prompt = messages[0]["content"]
            prompts.append(prompt)
            if "You verify a semantic" in prompt:
                self.assertIn('"layout": "ledger"', messages[1]["content"])
                return '{"verdict":"accept","repairs":[]}'
            self.assertIn("24%", prompt)
            current_guide["layout"] = "ledger"
            return self.compact

        result = await generate_adaptive_record(self.source, False, self.deadline(), request,
                                                guide_provider=lambda: current_guide)
        self.assertEqual(result, self.compact)
        self.assertEqual(len(prompts), 2)

    async def test_source_grounded_repair_can_win(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                return json.dumps({"verdict": "repair", "repairs": [
                    {"old": "approved", "new": "not approved",
                     "source_quote": "Agency B was not approved."}]})
            return "A:12 units,2025; B:approved."

        result = await generate_adaptive_record(self.source, False, self.deadline(), request)
        self.assertEqual(result, self.compact)

    async def test_critic_timeout_keeps_model_generated_fallback(self):
        fallback = AsyncMock(return_value=self.baseline)

        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                raise TimeoutError("slow review")
            return self.compact

        with patch("semantic_receiver.generate_record", fallback):
            result = await generate_adaptive_record(self.source, False, self.deadline(), request)
        self.assertEqual(result, self.baseline)
        fallback.assert_awaited_once_with(self.source, False, unittest.mock.ANY, request,
                                           allow_verbatim=False, audit=False)

    async def test_critic_rejects_missing_material_date_and_falls_back(self):
        async def request(messages, tokens, budget):
            if "You verify a semantic" in messages[0]["content"]:
                self.assertIn('2025', messages[1]["content"])
                return '{"verdict":"reject","repairs":[],"risk":"missing year"}'
            return "A:12 units; B:not approved."

        with patch("semantic_receiver.generate_record", AsyncMock(return_value=self.baseline)):
            result = await generate_adaptive_record(self.source, False, self.deadline(), request)
        self.assertEqual(result, self.baseline)

    async def test_overlong_candidate_skips_critic_and_falls_back(self):
        prompts = []

        async def request(messages, tokens, budget):
            prompts.append(messages[0]["content"])
            return "A faithful record: " + self.source

        with patch("semantic_receiver.generate_record", AsyncMock(return_value=self.baseline)):
            result = await generate_adaptive_record(self.source, False, self.deadline(), request)
        self.assertEqual(result, self.baseline)
        self.assertEqual(len(prompts), 1)
        self.assertFalse(any("You verify a semantic" in prompt for prompt in prompts))

    async def test_compact_model_failure_uses_faithful_fallback(self):
        async def request(*args, **kwargs):
            raise ConnectionError("model unavailable")

        with patch("semantic_receiver.generate_record", AsyncMock(return_value=self.baseline)):
            result = await generate_adaptive_record(self.source, True, self.deadline(), request)
        self.assertEqual(result, self.baseline)

    async def test_all_model_paths_failed_never_produce_success(self):
        async def failed(*args, **kwargs):
            raise ConnectionError("model unavailable")

        with patch("semantic_receiver.generate_record", failed):
            with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
                await generate_adaptive_record(self.source, False, self.deadline(), failed)

    async def test_compact_phase_respects_remaining_budget(self):
        started = time.monotonic()
        request_started = asyncio.Event()

        async def request(*args, **kwargs):
            request_started.set()
            await asyncio.Event().wait()

        with patch("semantic_receiver.generate_record", AsyncMock(return_value=self.baseline)):
            result = await generate_adaptive_record(self.source, False, started + 8.3, request)
        self.assertTrue(request_started.is_set())
        self.assertEqual(result, self.baseline)
        self.assertLess(time.monotonic() - started, 0.2)

    async def test_cancellation_cleans_up_active_compact_request(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def request(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch("semantic_receiver.generate_record", AsyncMock(side_effect=AssertionError)):
            job = asyncio.create_task(generate_adaptive_record(self.source, False, self.deadline(), request))
            await started.wait()
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
        self.assertTrue(stopped.is_set())


if __name__ == "__main__":
    unittest.main()
