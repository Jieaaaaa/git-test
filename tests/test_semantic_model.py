import asyncio
import json
import sys
import time
import unittest
from unittest.mock import patch

from semantic_model import apply_patches, generate_record, is_verbatim, numeric_gaps, parse_coverage, split_source


class SourcePolicyTests(unittest.TestCase):
    def test_source_partition_preserves_every_character_in_order(self):
        for source in ("", "Heading\n" + "Alpha was not approved; Beta received 12 units.\n" * 200,
                       "标题\n" + "甲公司于2025年发放12万元；乙公司没有获批。\n" * 200,
                       "x" * 9000):
            pieces = split_source(source)
            self.assertEqual("".join(pieces), source)
            self.assertLessEqual(len(pieces), 3)
            if source:
                self.assertTrue(all(pieces))

    def test_coverage_requires_every_section(self):
        self.assertEqual(parse_coverage('{"records":["first","second"]}', 2), ["first", "second"])
        for response in ('{"records":["first"]}', '{"records":["first",""]}', '{"records":"text"}'):
            with self.assertRaises(ValueError):
                parse_coverage(response, 2)

    def test_factual_repairs_can_increase_length(self):
        result = apply_patches(["Funding approved."], json.dumps({"patches": [
            {"section": 1, "old": "Funding approved.", "new": "Funding was not approved before 2025."},
            {"section": 1, "old": "", "new": "The vote was 12 to 9."},
        ]}))
        self.assertEqual(result, ["Funding was not approved before 2025.\nThe vote was 12 to 9."])

    def test_invalid_patch_does_not_modify_original(self):
        records = ["value 12"]
        with self.assertRaises(ValueError):
            apply_patches(records, '{"patches":[{"section":1,"old":"missing","new":"other"}]}')
        self.assertEqual(records, ["value 12"])

    def test_numeric_check_is_only_a_hint(self):
        self.assertEqual(numeric_gaps("In 2025: 12.5%, not 20%", "In 2025: 12.5%"), ["20%"])

    def test_newlines_cannot_disguise_a_verbatim_source(self):
        self.assertTrue(is_verbatim("原文。另一段。", "原文。\n另一段。"))


@unittest.skipIf(sys.version_info < (3, 11), "model workflow requires the image's Python 3.11")
class ModelWorkflowTests(unittest.IsolatedAsyncioTestCase):
    article = "Article title.\n" + "The council rejected option A; option B costs 12 units in 2025.\n" * 65

    def budget(self, deadline, now=None, fraction=1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        return remaining * fraction

    async def generate(self, request, duration=1):
        with patch("semantic_model.request_budget", self.budget):
            return await generate_record(self.article, False, time.monotonic() + duration, request)

    async def test_parallel_calls_and_factual_audit(self):
        active, peak, calls = 0, 0, []

        async def request(messages, tokens, budget):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            calls.append(messages[0]["content"])
            try:
                await asyncio.sleep(0.01)
                if "Audit the factual" in messages[0]["content"]:
                    return '{"patches":[{"section":1,"old":"","new":"Option A was rejected."}]}'
                if "independent, concise coverage" in messages[0]["content"]:
                    return json.dumps({"records": ["Backup record"] * len(split_source(self.article))})
                return "Option B: 12 units, 2025."
            finally:
                active -= 1

        result = await self.generate(request, duration=0.15)
        self.assertIn("Option A was rejected", result)
        self.assertGreaterEqual(peak, 2)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(active, 0)

    async def test_slow_section_uses_model_coverage_and_cleans_up(self):
        cancelled = []

        async def request(messages, tokens, budget):
            prompt = messages[0]["content"]
            if "independent, concise coverage" in prompt:
                return json.dumps({"records": ["Model coverage %d" % i
                                               for i in range(len(split_source(self.article)))]})
            if "Audit the factual" in prompt:
                return '{"patches":[]}'
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        started = time.monotonic()
        result = await self.generate(request)
        self.assertIn("Model coverage", result)
        self.assertNotIn("Article title", result)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(cancelled), len(split_source(self.article)))

    async def test_audit_failure_keeps_usable_model_records(self):
        async def request(messages, tokens, budget):
            if "Audit the factual" in messages[0]["content"]:
                await asyncio.sleep(5)
            return "Completed factual model record."

        result = await self.generate(request, duration=0.2)
        self.assertIn("Completed factual model record.", result)

    async def test_all_failed_or_empty_does_not_return_source(self):
        for response in (None, ""):
            async def request(messages, tokens, budget):
                if response is None:
                    raise RuntimeError("Gateway unavailable")
                return response

            with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
                await self.generate(request)

    async def test_copied_source_sections_are_rejected(self):
        async def request(messages, tokens, budget):
            if "independent, concise coverage" in messages[0]["content"]:
                return json.dumps({"records": split_source(self.article)})
            return messages[1]["content"].split("TARGET SECTION:\n")[1].split("\nFOLLOWING CONTEXT")[0]

        with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
            await self.generate(request)

    async def test_receiver_can_validate_already_encoded_dense_facts(self):
        async def request(messages, tokens, budget):
            if "Audit the factual" in messages[0]["content"]:
                return '{"patches":[]}'
            return "A:12 units,2025; B:not approved."

        with patch("semantic_model.request_budget", self.budget):
            result = await generate_record("A:12 units,2025; B:not approved.", False,
                                           time.monotonic() + 1, request, allow_verbatim=True)
        self.assertEqual(result, "A:12 units,2025; B:not approved.")

    async def test_fast_server_failure_gets_one_retry(self):
        class ServerError(Exception):
            status_code = 503

        attempts = {}

        async def request(messages, tokens, budget):
            prompt = messages[1]["content"]
            attempts[prompt] = attempts.get(prompt, 0) + 1
            if "Audit the factual" in messages[0]["content"]:
                return '{"patches":[]}'
            if attempts[prompt] == 1:
                raise ServerError()
            if "independent, concise coverage" in messages[0]["content"]:
                return json.dumps({"records": ["Backup"] * len(split_source(self.article))})
            return "Retried model record"

        self.assertIn("Retried model record", await self.generate(request))
        self.assertEqual(max(attempts.values()), 2)


if __name__ == "__main__":
    unittest.main()
