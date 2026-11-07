import asyncio
import hashlib
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from wire import (
    ACK_QUERY, ACK_QUERY_FRAME, ACK_STATE, ACK_STATE_FRAME, BLOCK_DATA_FRAME,
    BLOCK_HEADER, BLOCK_META_FRAME, MAX_BLOCK_BYTES, BlockDecoder, FrameError,
    build_transmission_plan, compress_chunk, decode_frame, encode_frame,
)

try:
    import agent_app
except ImportError:
    agent_app = None


SOURCE = ("研究项目记录：主管批准于2025年拨款120万元；候选方案未获批准，条件是完成审计。\n" * 240).encode()


class BlockTransportTests(unittest.TestCase):
    def test_plan_chooses_fewer_framed_bytes_and_preserves_source(self):
        task_id = "eval-transport-example"
        plan = build_transmission_plan(task_id, SOURCE, 512)
        self.assertEqual(plan.mode, "blocks")
        old_chunks = [SOURCE[start:start + 512] for start in range(0, len(SOURCE), 512)]
        old_bytes = sum(len(encode_frame(kind, task_id, index, len(old_chunks), data))
                        for index, source in enumerate(old_chunks)
                        for kind, data in [compress_chunk(source)])
        self.assertLess(plan.estimated_bytes, old_bytes)
        decoder = BlockDecoder(len(plan.packets))
        packets = list(plan.packets)
        random.Random(7).shuffle(packets)
        for packet in packets + packets:
            self.assertLessEqual(len(packet), 1400)
            decoder.add(decode_frame(packet), len(SOURCE))
        self.assertEqual(decoder.assembled(len(SOURCE), plan.block_count), SOURCE)

    def test_small_source_uses_cheaper_legacy_representation(self):
        plan = build_transmission_plan("small", b"abc", 512)
        self.assertEqual(plan.mode, "legacy")
        self.assertEqual(decode_frame(plan.packets[0]).payload, b"abc")

    def test_missing_block_preserves_other_verified_source_ranges(self):
        plan = build_transmission_plan("partial", SOURCE, 512)
        frames = [decode_frame(packet) for packet in plan.packets]
        offsets = sorted({BLOCK_HEADER.unpack_from(frame.payload)[0] for frame in frames})
        missing_offset = offsets[1]
        decoder = BlockDecoder(len(frames))
        for frame in reversed(frames):
            if BLOCK_HEADER.unpack_from(frame.payload)[0] != missing_offset:
                decoder.add(frame, len(SOURCE))
        self.assertNotIn(missing_offset, decoder.blocks)
        self.assertEqual(decoder.assembled(len(SOURCE), plan.block_count), b"")
        self.assertIn(("[source bytes %d:" % missing_offset).encode(), decoder.partial_content(len(SOURCE)))
        for offset, block in decoder.blocks.items():
            self.assertEqual(SOURCE[offset:offset + len(block)], block)

    def test_block_digest_rejects_even_crc_valid_forged_data(self):
        plan = build_transmission_plan("digest", SOURCE, 512)
        frame = decode_frame(plan.packets[0])
        descriptor = list(BLOCK_HEADER.unpack_from(frame.payload))
        descriptor[-1] = b"0" * 16
        payload = BLOCK_HEADER.pack(*descriptor) + frame.payload[BLOCK_HEADER.size:]
        packet = encode_frame(BLOCK_DATA_FRAME, frame.task_id, frame.sequence, frame.total, payload)
        decoder = BlockDecoder(frame.total)
        with self.assertRaises(FrameError):
            decoder.add(decode_frame(packet), len(SOURCE))
        self.assertFalse(decoder.blocks)

    def test_decoded_block_limit_is_checked_before_allocation(self):
        digest = hashlib.sha256(b"x").digest()[:16]
        payload = BLOCK_HEADER.pack(0, MAX_BLOCK_BYTES + 1, 0, 1, 0, 1, digest) + b"x"
        frame = decode_frame(encode_frame(BLOCK_DATA_FRAME, "oversized", 0, 1, payload))
        with self.assertRaises(FrameError):
            BlockDecoder(1).add(frame)

    def test_utf8_may_cross_block_boundary_without_changing_a_character(self):
        plan = build_transmission_plan("utf8", ("甲乙丙丁戊" * 2200).encode(), 512)
        decoder = BlockDecoder(len(plan.packets))
        for packet in plan.packets:
            decoder.add(decode_frame(packet))
        self.assertEqual(decoder.assembled(plan.original_bytes, plan.block_count).decode(), "甲乙丙丁戊" * 2200)


@unittest.skipIf(agent_app is None, "requires official base image dependencies")
class ReceiverTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_data_before_meta_reordering_and_full_hash(self):
        plan = build_transmission_plan("receiver-blocks", SOURCE, 512)
        states = {}
        protocol = agent_app.ReceiverProtocol(states)
        generation = AsyncMock()
        with patch.object(protocol, "_generate", generation):
            for packet in reversed(plan.packets):
                protocol.datagram_received(packet, ("127.0.0.1", 9000))
            state = states["receiver-blocks"]
            self.assertEqual(state.status, "pending")
            self.assertEqual(state.missing(), [])
            protocol.datagram_received(plan.metadata(state.task_id, 30000), ("127.0.0.1", 9000))
            await state.model_task
            self.assertEqual(state.assembled(), SOURCE)
            generation.assert_awaited_once_with(state, SOURCE, partial=False)
            state.finalize_task.cancel()
            await asyncio.gather(state.finalize_task, return_exceptions=True)

    async def test_ack_rejects_wrong_start_total_and_count(self):
        queue = asyncio.Queue()
        protocol = SimpleNamespace(ack_queues={"ack": queue})
        for start, total, count in ((1, 4, 4), (0, 9, 4), (0, 4, 3), (0, 4, 4)):
            payload = ACK_STATE.pack(count, 1) + b"\x0f"
            queue.put_nowait(decode_frame(encode_frame(ACK_STATE_FRAME, "ack", start, total, payload)))
        receipt = await agent_app.await_batch_ack(protocol, "ack", 0, 4, 0.1, total=4)
        self.assertEqual(receipt, (b"\x0f", True))
        self.assertTrue(queue.empty())

    async def test_complete_blocks_still_require_the_full_source_hash(self):
        plan = build_transmission_plan("full-hash", SOURCE, 512)
        states = {}
        protocol = agent_app.ReceiverProtocol(states)
        frame = decode_frame(plan.metadata("full-hash", 30000))
        fields = list(agent_app.BLOCK_META.unpack(frame.payload))
        fields[-1] = b"x" * 32
        forged_meta = encode_frame(BLOCK_META_FRAME, frame.task_id, 0, frame.total,
                                   agent_app.BLOCK_META.pack(*fields))
        protocol.datagram_received(forged_meta, ("127.0.0.1", 9000))
        for packet in plan.packets:
            protocol.datagram_received(packet, ("127.0.0.1", 9000))
        state = states["full-hash"]
        self.assertEqual(state.status, "error")
        self.assertEqual(state.error, "integrity_check_failed")
        self.assertIsNone(state.model_task)
        state.finalize_task.cancel()
        await asyncio.gather(state.finalize_task, return_exceptions=True)

    async def test_lost_ack_queries_again_before_replaying_data(self):
        packets = []
        calls = 0

        async def feedback(**kwargs):
            nonlocal calls
            calls += 1
            return None if calls == 1 else (b"\xff\xff", True)

        protocol = SimpleNamespace(transport=object(), ack_queues={})
        with patch.object(agent_app, "sender_protocol", protocol), \
                patch.object(agent_app, "UDP_PEER_ADDR", ("127.0.0.1", 9000)), \
                patch.object(agent_app, "send_packet", lambda protocol, packet: packets.append(decode_frame(packet))), \
                patch.object(agent_app, "await_batch_ack", feedback):
            await agent_app.send_article("lost-ack", SOURCE, 512, 30000)
        data_sequences = [frame.sequence for frame in packets
                          if frame.kind not in (ACK_QUERY_FRAME, BLOCK_META_FRAME, agent_app.META_FRAME)]
        self.assertEqual(len(data_sequences), len(set(data_sequences)))
        self.assertGreaterEqual(calls, 2)

    async def test_cumulative_ack_never_reintroduces_confirmed_sequences(self):
        sent = []
        replies = [(b"\x01", True), (b"\x02", True), (b"\xff", True)]

        async def feedback(**kwargs):
            return replies.pop(0) if replies else (b"\xff", True)

        protocol = SimpleNamespace(transport=object(), ack_queues={})
        source = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(100))
        with patch.object(agent_app, "sender_protocol", protocol), \
                patch.object(agent_app, "UDP_PEER_ADDR", ("127.0.0.1", 9000)), \
                patch.object(agent_app, "send_packet", lambda protocol, packet: sent.append(decode_frame(packet))), \
                patch.object(agent_app, "await_batch_ack", feedback):
            await agent_app.send_article("merge-ack", source, 512, 30000)
        data = [frame.sequence for frame in sent if frame.kind in
                (agent_app.DATA_FRAME, agent_app.COMPRESSED_DATA_FRAME, BLOCK_DATA_FRAME)]
        self.assertEqual(data.count(0), 1)
        self.assertEqual(data.count(1), 1)

    async def test_missing_metadata_is_repaired_without_replaying_received_data(self):
        sent = []
        replies = [(b"\xff", False), (b"\xff", True)]

        async def feedback(**kwargs):
            return replies.pop(0)

        protocol = SimpleNamespace(transport=object(), ack_queues={})
        with patch.object(agent_app, "sender_protocol", protocol), \
                patch.object(agent_app, "UDP_PEER_ADDR", ("127.0.0.1", 9000)), \
                patch.object(agent_app, "send_packet", lambda protocol, packet: sent.append(decode_frame(packet))), \
                patch.object(agent_app, "await_batch_ack", feedback):
            await agent_app.send_article("meta-loss", SOURCE, 512, 30000)
        data = [frame.sequence for frame in sent if frame.kind == BLOCK_DATA_FRAME]
        self.assertEqual(len(data), len(set(data)))
        self.assertEqual(sum(frame.kind == BLOCK_META_FRAME for frame in sent), 4)


if __name__ == "__main__":
    unittest.main()
