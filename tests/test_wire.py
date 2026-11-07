import unittest

from wire import (
    ACK_FRAME,
    COMPRESSED_DATA_FRAME,
    DATA_FRAME,
    FrameError,
    ack_bitmap,
    bitmap_has,
    decode_frame,
    decompress_chunk,
    encode_frame,
    max_payload,
    compress_chunk,
)


class WireTests(unittest.TestCase):
    def test_round_trip_preserves_utf8_task_and_payload(self):
        packet = encode_frame(DATA_FRAME, "任务-1", 7, 12, "正文🙂".encode("utf-8"))
        frame = decode_frame(packet)
        self.assertEqual(frame.task_id, "任务-1")
        self.assertEqual(frame.sequence, 7)
        self.assertEqual(frame.total, 12)
        self.assertEqual(frame.payload, "正文🙂".encode("utf-8"))

    def test_rejects_corrupt_payload(self):
        packet = bytearray(encode_frame(DATA_FRAME, "t", 0, 1, b"payload"))
        packet[-1] ^= 1
        with self.assertRaises(FrameError):
            decode_frame(bytes(packet))

    def test_enforces_datagram_limit(self):
        task_id = "eval-1"
        packet = encode_frame(DATA_FRAME, task_id, 0, 1, b"x" * max_payload(task_id))
        self.assertLessEqual(len(packet), 1400)
        with self.assertRaises(FrameError):
            encode_frame(DATA_FRAME, task_id, 0, 1, b"x" * (max_payload(task_id) + 1))

    def test_ack_bitmap_tracks_sparse_chunks(self):
        bitmap = ack_bitmap({2, 4, 11}, 1, 12)
        self.assertTrue(bitmap_has(bitmap, 1))
        self.assertTrue(bitmap_has(bitmap, 3))
        self.assertTrue(bitmap_has(bitmap, 10))
        self.assertFalse(bitmap_has(bitmap, 2))
        self.assertFalse(bitmap_has(bitmap, 11))

    def test_ack_frame_uses_same_framing(self):
        packet = encode_frame(ACK_FRAME, "t", 0, 8, b"\xff")
        self.assertEqual(decode_frame(packet).kind, ACK_FRAME)

    def test_repetitive_text_chunk_is_losslessly_compressed(self):
        source = ("日期、人物、地點、數量與因果關係需要逐項保留。" * 20).encode("utf-8")
        kind, encoded = compress_chunk(source)
        self.assertEqual(kind, COMPRESSED_DATA_FRAME)
        self.assertLess(len(encoded), len(source))
        self.assertEqual(decompress_chunk(kind, encoded, max_output=len(source)), source)

    def test_short_chunk_stays_uncompressed(self):
        source = b"xyz"
        kind, encoded = compress_chunk(source)
        self.assertEqual(kind, DATA_FRAME)
        self.assertEqual(decompress_chunk(kind, encoded), source)

    def test_compressed_data_round_trips_through_udp_frame(self):
        source = b"event date quantity cause relation " * 20
        kind, encoded = compress_chunk(source)
        frame = decode_frame(encode_frame(kind, "task-1", 0, 1, encoded))
        self.assertEqual(decompress_chunk(frame.kind, frame.payload), source)


if __name__ == "__main__":
    unittest.main()
