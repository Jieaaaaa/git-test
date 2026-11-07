"""Compact, checksummed UDP frames for the challenge data plane."""

import hashlib
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
BLOCK_META = struct.Struct("!III32s")
BLOCK_HEADER = struct.Struct("!IIHHBI16s")
ACK_STATE = struct.Struct("!HB")
MAX_BLOCK_BYTES = 8192
MAX_SOURCE_BYTES = 16 * 1024 * 1024

META_FRAME = 1
DATA_FRAME = 2
ACK_QUERY_FRAME = 3
ACK_FRAME = 4
COMPRESSED_DATA_FRAME = 5
BLOCK_META_FRAME = 6
BLOCK_DATA_FRAME = 7
ACK_STATE_FRAME = 8
GUIDE_FRAME = 9


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


@dataclass(frozen=True)
class TransmissionPlan:
    packets: tuple
    mode: str
    chunk_size: int
    block_count: int
    original_bytes: int
    digest: bytes
    estimated_bytes: int

    def metadata(self, task_id, remaining_ms):
        if self.mode == "blocks":
            kind = BLOCK_META_FRAME
            payload = BLOCK_META.pack(self.original_bytes, remaining_ms, self.block_count, self.digest)
        else:
            kind = META_FRAME
            payload = META.pack(self.original_bytes, remaining_ms, self.chunk_size, self.digest)
        return encode_frame(kind, task_id, 0, len(self.packets), payload)


def build_transmission_plan(task_id, article, suggested_size, window=16):
    """Choose by framed bytes, including the same metadata and ACK schedule for each candidate."""
    if len(article) > MAX_SOURCE_BYTES:
        raise FrameError("source_too_large")
    usable = min(max_payload(task_id), 1200)
    chunk_size = min(max(suggested_size, 256), usable)
    digest = hashlib.sha256(article).digest()
    candidates = []

    def candidate(mode, parts, source_chunk_size=0, block_count=0):
        total = len(parts)
        packets = tuple(encode_frame(kind, task_id, index, total, payload)
                        for index, (kind, payload) in enumerate(parts))
        provisional = TransmissionPlan(packets, mode, source_chunk_size, block_count,
                                       len(article), digest, 0)
        metadata_bytes = len(provisional.metadata(task_id, 1))
        control_bytes = 2 * metadata_bytes
        for start in range(0, total, window):
            count = min(window, total - start)
            control_bytes += metadata_bytes
            control_bytes += len(encode_frame(ACK_QUERY_FRAME, task_id, start, total, ACK_QUERY.pack(count)))
            ack = ACK_STATE.pack(count, 1) + bytes((count + 7) // 8)
            control_bytes += len(encode_frame(ACK_STATE_FRAME, task_id, start, total, ack))
        estimated = sum(map(len, packets)) + control_bytes
        candidates.append(TransmissionPlan(packets, mode, source_chunk_size, block_count,
                                            len(article), digest, estimated))

    for actual_size in sorted({chunk_size, usable}):
        chunks = [article[start:start + actual_size] for start in range(0, len(article), actual_size)] or [b""]
        candidate("legacy", [compress_chunk(chunk) for chunk in chunks], actual_size)
    if article:
        for block_size in (4096, MAX_BLOCK_BYTES):
            parts = []
            block_count = 0
            for offset in range(0, len(article), block_size):
                raw = article[offset:offset + block_size]
                compressed = zlib.compress(raw, level=6)
                codec, encoded = (1, compressed) if len(compressed) < len(raw) else (0, raw)
                fragment_size = usable - BLOCK_HEADER.size
                fragments = [encoded[start:start + fragment_size]
                             for start in range(0, len(encoded), fragment_size)]
                block_digest = hashlib.sha256(raw).digest()[:16]
                for index, fragment in enumerate(fragments):
                    header = BLOCK_HEADER.pack(offset, len(raw), index, len(fragments), codec,
                                               len(encoded), block_digest)
                    parts.append((BLOCK_DATA_FRAME, header + fragment))
                block_count += 1
            candidate("blocks", parts, block_count=block_count)
    return min(candidates, key=lambda plan: plan.estimated_bytes)


class BlockDecoder:
    """Recover independent verified source blocks; a missing block cannot corrupt another."""

    def __init__(self, total):
        self.total = total
        self.parts = {}
        self.blocks = {}
        self.owners = {}

    def add(self, frame, expected_bytes=None):
        if frame.kind != BLOCK_DATA_FRAME or frame.total != self.total:
            raise FrameError("incompatible_block_frame")
        if len(frame.payload) <= BLOCK_HEADER.size:
            raise FrameError("short_block_frame")
        offset, length, index, count, codec, encoded_length, digest = BLOCK_HEADER.unpack_from(frame.payload)
        limit = MAX_SOURCE_BYTES if expected_bytes is None else expected_bytes
        if (not 1 <= length <= MAX_BLOCK_BYTES or offset + length > limit
                or codec not in (0, 1) or not 1 <= encoded_length <= MAX_BLOCK_BYTES
                or not 1 <= count <= 64 or index >= count or frame.sequence < index
                or frame.sequence - index + count > self.total):
            raise FrameError("invalid_block_descriptor")
        if codec == 0 and encoded_length != length:
            raise FrameError("invalid_raw_block_length")
        start = frame.sequence - index
        descriptor = (offset, length, count, codec, encoded_length, digest)
        prior = self.parts.get(start)
        if prior is None:
            for other_start, (other, fragments) in self.parts.items():
                other_offset, other_length = other[:2]
                if max(offset, other_offset) < min(offset + length, other_offset + other_length):
                    raise FrameError("overlapping_source_blocks")
            if any(sequence in self.owners for sequence in range(start, start + count)):
                raise FrameError("overlapping_block_sequences")
            fragments = {}
            self.parts[start] = (descriptor, fragments)
            for sequence in range(start, start + count):
                self.owners[sequence] = start
        else:
            other, fragments = prior
            if other != descriptor:
                raise FrameError("conflicting_block_descriptor")
        fragment = frame.payload[BLOCK_HEADER.size:]
        if len(fragment) > encoded_length or (index in fragments and fragments[index] != fragment):
            raise FrameError("conflicting_block_fragment")
        fragments[index] = fragment
        if sum(map(len, fragments.values())) > encoded_length:
            fragments.pop(index)
            raise FrameError("oversized_encoded_block")
        if len(fragments) != count or offset in self.blocks:
            return
        encoded = b"".join(fragments[part] for part in range(count))
        if len(encoded) != encoded_length:
            raise FrameError("incomplete_encoded_block")
        if codec:
            try:
                decoder = zlib.decompressobj()
                decoded = decoder.decompress(encoded, length + 1)
            except zlib.error as exc:
                raise FrameError("invalid_compressed_block") from exc
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise FrameError("invalid_compressed_block_tail")
        else:
            decoded = encoded
        if len(decoded) != length or hashlib.sha256(decoded).digest()[:16] != digest:
            raise FrameError("block_integrity_check_failed")
        self.blocks[offset] = decoded

    def discard_incomplete(self, sequence):
        start = self.owners.get(sequence)
        if start is None:
            return ()
        descriptor, fragments = self.parts[start]
        if descriptor[0] in self.blocks:
            return ()
        count = descriptor[2]
        sequences = tuple(range(start, start + count))
        for item in sequences:
            self.owners.pop(item, None)
        self.parts.pop(start)
        return sequences

    def within(self, expected_bytes):
        return all(descriptor[0] + descriptor[1] <= expected_bytes
                   for descriptor, fragments in self.parts.values())

    def assembled(self, expected_bytes, expected_blocks):
        if len(self.blocks) != expected_blocks:
            return b""
        position = 0
        output = []
        for offset, block in sorted(self.blocks.items()):
            if offset != position:
                return b""
            output.append(block)
            position += len(block)
        return b"".join(output) if position == expected_bytes else b""

    def partial_content(self, expected_bytes=None):
        output = []
        position = 0
        for offset, block in sorted(self.blocks.items()):
            if offset > position:
                output.append(("\n[source bytes %d:%d missing]\n" % (position, offset)).encode("ascii"))
            output.append(block)
            position = offset + len(block)
        if expected_bytes is not None and position < expected_bytes:
            output.append(("\n[source bytes %d:%d missing]\n" % (position, expected_bytes)).encode("ascii"))
        return b"".join(output) if self.blocks else b""
