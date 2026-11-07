import unittest

from wire import (
    ACK_FRAME,
    DATA_FRAME,
    FrameError,
    ack_bitmap,
    bitmap_has,
    decode_frame,
    encode_frame,
    max_payload,
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


if __name__ == "__main__":
    unittest.main()
