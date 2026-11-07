"""Compact, checksummed UDP frames for the challenge data plane."""

import struct
import zlib
from dataclasses import dataclass

MAGIC = b"SCN2"
VERSION = 1
MAX_DATAGRAM = 1400
MAX_TASK_ID_BYTES = 128
HEADER = struct.Struct("!4sBBHIIHHI")
META = struct.Struct("!IIH32s")
ACK_QUERY = struct.Struct("!H")

META_FRAME = 1
DATA_FRAME = 2
ACK_QUERY_FRAME = 3
ACK_FRAME = 4
COMPRESSED_DATA_FRAME = 5


class FrameError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    kind: int
    task_id: str
    sequence: int
    total: int
    payload: bytes


def max_payload(task_id):
    task_size = len(task_id.encode("utf-8"))
    return MAX_DATAGRAM - HEADER.size - task_size


def encode_frame(kind, task_id, sequence, total, payload=b""):
    task_bytes = task_id.encode("utf-8")
    if not task_bytes or len(task_bytes) > MAX_TASK_ID_BYTES:
        raise FrameError("invalid_task_id_length")
    if not 0 <= sequence <= 0xFFFFFFFF or not 0 <= total <= 0xFFFFFFFF:
        raise FrameError("invalid_sequence")
    if len(payload) > 0xFFFF:
        raise FrameError("payload_too_large")
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    header = HEADER.pack(MAGIC, VERSION, kind, len(task_bytes), sequence,
                         total, len(payload), 0, checksum)
    packet = header + task_bytes + payload
    if len(packet) > MAX_DATAGRAM:
        raise FrameError("datagram_too_large")
    return packet


def decode_frame(packet):
    if len(packet) < HEADER.size:
        raise FrameError("short_header")
    magic, version, kind, task_size, sequence, total, payload_size, _, checksum = HEADER.unpack_from(packet)
    if magic != MAGIC or version != VERSION:
        raise FrameError("unsupported_frame")
    if task_size == 0 or task_size > MAX_TASK_ID_BYTES:
        raise FrameError("invalid_task_id_length")
    expected_size = HEADER.size + task_size + payload_size
    if len(packet) != expected_size or len(packet) > MAX_DATAGRAM:
        raise FrameError("invalid_frame_size")
    task_start = HEADER.size
    try:
        task_id = packet[task_start:task_start + task_size].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrameError("invalid_task_id") from exc
    payload = packet[task_start + task_size:]
    if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
        raise FrameError("checksum_mismatch")
    return Frame(kind, task_id, sequence, total, payload)


def ack_bitmap(received, start, count):
    bitmap = bytearray((count + 7) // 8)
    for offset in range(count):
        if start + offset in received:
            bitmap[offset // 8] |= 1 << (offset % 8)
    return bytes(bitmap)


def bitmap_has(bitmap, offset):
    return offset // 8 < len(bitmap) and bool(bitmap[offset // 8] & (1 << (offset % 8)))


def compress_chunk(payload):
    compressed = zlib.compress(payload, level=1)
    if len(compressed) < len(payload):
        return COMPRESSED_DATA_FRAME, compressed
    return DATA_FRAME, payload


def decompress_chunk(kind, payload, max_output=1200):
    if kind == DATA_FRAME:
        if len(payload) > max_output:
            raise FrameError("chunk_too_large")
        return payload
    if kind != COMPRESSED_DATA_FRAME:
        raise FrameError("invalid_data_frame_kind")
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(payload, max_output + 1)
    except zlib.error as exc:
        raise FrameError("invalid_compressed_chunk") from exc
    if len(decoded) > max_output or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise FrameError("invalid_compressed_chunk_size")
    return decoded
