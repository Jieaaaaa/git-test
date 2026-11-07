import asyncio
import hashlib
import json
import sys
import time
import unittest
import zlib

from semantic_guide import (MAX_GUIDE_BYTES, build_guide, decode_guide, encode_guide,
                            validated_guide)
from wire import max_payload


class GuideWireTests(unittest.TestCase):
    source = "National Energy Board (NEB) approved 12 units, unless the audit fails."

    def guide(self):
        return {"version": 1, "source_sha256": hashlib.sha256(self.source.encode()).hexdigest(),
                "layout": "ledger", "entities": [{"name": "National Energy Board", "alias": "NEB"}],
                "anchors": ["12 units, unless the audit fails."]}

    def test_roundtrip_is_source_grounded_and_bounded_for_long_task_ids(self):
        payload = encode_guide(self.guide())
        self.assertLessEqual(len(payload), max_payload("x" * 128))
        decoded = decode_guide(payload)
        self.assertEqual(validated_guide(decoded, self.source), self.guide())

    def test_source_hash_mismatch_or_invented_names_and_quotations_are_rejected(self):
        self.assertEqual(validated_guide(self.guide(), self.source + " Changed."), {})
        for field, value in (("anchors", ["invented quotation"]),
                             ("entities", [{"name": "National Energy Board", "alias": "invented"}])):
            guide = self.guide()
            guide[field] = value
            self.assertEqual(validated_guide(guide, self.source), {})

    def test_decompression_bombs_trailing_data_and_truncated_streams_are_rejected(self):
        payload = encode_guide(self.guide())
        for invalid in (zlib.compress(b"x" * (MAX_GUIDE_BYTES + 1)), payload + b"extra",
                        payload[:-2], b"not zlib"):
            with self.assertRaises(ValueError):
                decode_guide(invalid)

    def test_oversized_lists_and_invalid_schema_are_rejected(self):
        guide = self.guide()
        guide["anchors"] *= 9
        with self.assertRaises(ValueError):
            encode_guide(guide)
        self.assertEqual(validated_guide({"version": 1}, self.source), {})

    def test_small_communication_budget_is_a_hard_bound(self):
        payload = encode_guide(self.guide(), capacity=170)
        self.assertLessEqual(len(payload), 170)
        self.assertTrue(validated_guide(decode_guide(payload), self.source))
        with self.assertRaisesRegex(ValueError, "too_large"):
            encode_guide(self.guide(), capacity=1)

    def test_byte_limit_prunes_long_unicode_guides_without_losing_grounding(self):
        source = "甲" * 180 + "乙" * 180 + "丙" * 180 + "丁" * 180
        guide = {"version": 1, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                 "layout": "timeline", "entities": [],
                 "anchors": [char * 180 for char in "甲乙丙丁甲乙丙丁"]}
        payload = encode_guide(guide)
        self.assertLessEqual(len(zlib.decompress(payload)), MAX_GUIDE_BYTES)
        self.assertTrue(validated_guide(decode_guide(payload), source))


@unittest.skipIf(sys.version_info < (3, 11), "uses image Python 3.11")
class GuideModelTests(unittest.IsolatedAsyncioTestCase):
    source = GuideWireTests.source

    async def test_both_model_roles_run_concurrently_and_drop_fabricated_entries(self):
        roles = set()
        ready = asyncio.Event()

        async def request(messages, tokens, budget):
            self.assertGreater(budget, 0)
            self.assertLessEqual(tokens, 650)
            role = "planner" if "entity/layout planner" in messages[0]["content"] else "sentinel"
            roles.add(role)
            if len(roles) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 1)
            if role == "planner":
                return json.dumps({"layout": "ledger", "entities": [
                    {"name": "National Energy Board", "alias": "NEB"},
                    {"name": "National Energy Board", "alias": "fake"}]})
            return json.dumps({"anchors": ["12 units, unless the audit fails.", "invented evidence"]})

        guide = await build_guide(self.source, request, time.monotonic() + 20)
        self.assertEqual(roles, {"planner", "sentinel"})
        self.assertEqual(len(guide["entities"]), 1)
        self.assertEqual(guide["anchors"], ["12 units, unless the audit fails."])

    async def test_one_role_failure_keeps_other_grounded_hints(self):
        async def request(messages, tokens, budget):
            if "entity/layout planner" in messages[0]["content"]:
                raise ConnectionError("unavailable")
            return '{"anchors":["12 units, unless the audit fails."]}'

        result = await build_guide(self.source, request, time.monotonic() + 20)
        self.assertEqual(result["layout"], "")
        self.assertEqual(result["anchors"], ["12 units, unless the audit fails."])

    async def test_all_failed_roles_return_no_guide(self):
        async def request(*args):
            raise TimeoutError()

        self.assertEqual(await build_guide(self.source, request, time.monotonic() + 20), {})

    async def test_completed_role_is_published_before_slow_sibling(self):
        published = asyncio.Event()
        updates = []

        async def request(messages, tokens, budget):
            if "entity/layout planner" in messages[0]["content"]:
                return '{"layout":"timeline","entities":[]}'
            await asyncio.wait_for(published.wait(), 0.5)
            return '{"anchors":["12 units, unless the audit fails."]}'

        async def update(guide):
            updates.append(guide)
            published.set()

        result = await build_guide(self.source, request, time.monotonic() + 20, on_update=update)
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0]["anchors"], [])
        self.assertEqual(result["anchors"], ["12 units, unless the audit fails."])

    async def test_parent_cancellation_cleans_up_both_model_roles(self):
        started = asyncio.Event()
        active = set()
        cancelled = set()

        async def request(messages, tokens, budget):
            role = messages[0]["content"]
            active.add(role)
            if len(active) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(role)

        job = asyncio.create_task(build_guide(self.source, request, time.monotonic() + 20))
        await asyncio.wait_for(started.wait(), 1)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(len(cancelled), 2)


if __name__ == "__main__":
    unittest.main()
