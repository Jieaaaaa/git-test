import bz2
import hashlib
import lzma
import unittest
import zlib
from unittest.mock import patch

from wire import (
    ACK_FRAME,
    BLOCK_DATA_FRAME,
    BLOCK_HEADER,
    COMPRESSED_DATA_FRAME,
    DATA_FRAME,
    BlockDecoder,
    FrameError,
    ack_bitmap,
    bitmap_has,
    build_transmission_plan,
    compress_block,
    decode_frame,
    decompress_chunk,
    decompress_block,
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

    def test_each_block_codec_preserves_verified_source_after_reordering(self):
        source = b"Names dates quantities conditions and exceptions. " * 120
        encoded_versions = [(0, source), (1, zlib.compress(source)),
                            (2, bz2.compress(source, compresslevel=1)),
                            (3, lzma.compress(source, filters=[{"id": lzma.FILTER_LZMA2,
                                                               "dict_size": 65536}]))]
        for codec, encoded in encoded_versions:
            with self.subTest(codec=codec):
                parts = [encoded[index:index + 900] for index in range(0, len(encoded), 900)]
                decoder = BlockDecoder(len(parts))
                for index in reversed(range(len(parts))):
                    descriptor = BLOCK_HEADER.pack(0, len(source), index, len(parts), codec,
                                                   len(encoded), hashlib.sha256(source).digest()[:16])
                    packet = encode_frame(BLOCK_DATA_FRAME, "codec", index, len(parts),
                                          descriptor + parts[index])
                    decoder.add(decode_frame(packet), len(source))
                self.assertEqual(decoder.assembled(len(source), 1), source)

    def test_compressed_block_rejects_truncation_trailing_and_excess_output(self):
        source = b"fact " * 300
        encoded_versions = [(1, zlib.compress(source)),
                            (2, bz2.compress(source, compresslevel=1)),
                            (3, lzma.compress(source, filters=[{"id": lzma.FILTER_LZMA2,
                                                               "dict_size": 65536}]))]
        for codec, encoded in encoded_versions:
            for malformed, declared_size in ((encoded[:-1], len(source)),
                                              (encoded + b"trailing", len(source)),
                                              (encoded + encoded, len(source)),
                                              (encoded, 16)):
                with self.subTest(codec=codec, size=declared_size, wire_size=len(malformed)):
                    with self.assertRaises(FrameError):
                        decompress_block(codec, malformed, declared_size)

    def test_lzma_rejects_stream_dictionary_above_memory_limit(self):
        encoded = lzma.compress(b"x" * 512, filters=[{"id": lzma.FILTER_LZMA2,
                                                    "dict_size": 8 * 1024 * 1024}])
        with self.assertRaises(FrameError):
            decompress_block(3, encoded, 512)

    def test_bzip2_rejects_large_working_memory_header(self):
        with self.assertRaises(FrameError):
            decompress_block(2, bz2.compress(b"x" * 512, compresslevel=9), 512)

    def test_adaptive_codec_is_no_more_expensive_than_zlib_framed_plan(self):
        source = b"".join(("Event %d: team %d received %d units in 2025; approval required.\n" %
                            (index, index % 7, index * 13)).encode() for index in range(180))
        adaptive = build_transmission_plan("compare", source, 512)

        def zlib_only(payload):
            compressed = zlib.compress(payload, 9)
            return (1, compressed) if len(compressed) < len(payload) else (0, payload)

        with patch("wire.compress_block", zlib_only):
            baseline = build_transmission_plan("compare", source, 512)
        self.assertLessEqual(adaptive.estimated_bytes, baseline.estimated_bytes)
        self.assertEqual(decompress_block(*compress_block(source[:8000]), len(source[:8000])),
                         source[:8000])


if __name__ == "__main__":
    unittest.main()
