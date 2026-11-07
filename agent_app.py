"""Sender and Receiver HTTP/UDP services for challenge 2."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from openai import APIConnectionError, AsyncOpenAI

from agenthack import AgentRuntime, BaseAgent
from agenthack.telemetry import task
from model_policy import request_budget
from semantic_agent import (PACKET_PREFIX, SourceView, byte_size, encode_for_transport,
                            pack_source, unpack_source)
from semantic_receiver import generate_adaptive_record
from semantic_guide import decode_guide, validated_guide

from wire import (
    ACK_FRAME,
    ACK_QUERY,
    ACK_QUERY_FRAME,
    ACK_STATE,
    ACK_STATE_FRAME,
    BLOCK_DATA_FRAME,
    BLOCK_META,
    BLOCK_META_FRAME,
    BlockDecoder,
    COMPRESSED_DATA_FRAME,
    DATA_FRAME,
    GUIDE_FRAME,
    META,
    META_FRAME,
    FrameError,
    ack_bitmap,
    bitmap_has,
    decode_frame,
    decompress_chunk,
    encode_frame,
    max_payload,
    compress_chunk,
    build_transmission_plan,
    MAX_SOURCE_BYTES,
)

logging.basicConfig(level=os.getenv("AGENTHACK_LOG_LEVEL", "INFO"))
logger = logging.getLogger("challenge-agent")

ROLE = os.getenv("NETWORK_ROLE", os.getenv("AGENT_ROLE", "")).strip().lower()
UDP_HOST = os.getenv("UDP_PEER_HOST", "emulator")
UDP_PORT = int(os.getenv("UDP_PEER_PORT", "9000"))
UDP_PEER_ADDR = None
peer_resolver_task = None
SEND_WINDOW = 16
ACK_TIMEOUT_SECONDS = 1.5
MAX_BATCH_ATTEMPTS = 5
SENDER_BUDGET_BYTES = 15 * 1024 * 1024
RECEIVER_BUDGET_BYTES = 1 * 1024 * 1024
MAX_CHUNKS = 65536
# A partial reconstruction is a real model workflow (several parallel
# requests plus an optional critic), so leave it a generous window instead of
# waiting until the last half of the transport deadline.  The 8-second
# finalization reserve in model_policy.py is deducted separately.
PARTIAL_MODEL_WINDOW_SECONDS = 90.0
MODEL_NAME = os.getenv("PLATFORM_MODEL_NAME", os.getenv("MODEL_NAME", "deepseek_v4"))


class RuntimeAgent(BaseAgent):
    async def solve(self, task_input):
        return {"answer": ""}


runtime = AgentRuntime(RuntimeAgent())
telemetry = runtime.telemetry
app = FastAPI(title="Challenge 2 Agent")


@dataclass
class ReceiverTask:
    task_id: str
    total_chunks: int
    created_at: float = field(default_factory=time.monotonic)
    expected_bytes: Optional[int] = None
    chunk_size: Optional[int] = None
    digest: Optional[bytes] = None
    deadline_at: Optional[float] = None
    chunks: Dict[int, bytes] = field(default_factory=dict)
    transport_mode: Optional[str] = None
    block_count: Optional[int] = None
    block_decoder: Optional[BlockDecoder] = None
    guide: dict = field(default_factory=dict)
    status: str = "pending"
    content: Optional[str] = None
    error: Optional[str] = None
    udp_bytes_sent: int = 0
    model_task: Optional[asyncio.Task] = None
    # True while the currently published candidate was made from incomplete
    # source.  A complete source arriving later is allowed to supersede it.
    partial_result: bool = False
    finalize_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def assembled(self):
        if self.transport_mode == "blocks":
            if self.block_decoder is None or self.expected_bytes is None or self.block_count is None:
                return b""
            return self.block_decoder.assembled(self.expected_bytes, self.block_count)
        if self.chunk_size is None:
            return b""
        return b"".join(self.chunks[index] for index in sorted(self.chunks))

    def partial_content(self):
        if self.transport_mode == "blocks":
            return self.block_decoder.partial_content(self.expected_bytes) if self.block_decoder else b""
        segments = []
        current = bytearray()
        for index in range(self.total_chunks):
            chunk = self.chunks.get(index)
            if chunk is None:
                if current:
                    segments.append(bytes(current))
                    current.clear()
                segments.append(("\n[segment %d missing]\n" % index).encode("utf-8"))
            else:
                current.extend(chunk)
        if current:
            segments.append(bytes(current))
        return b"".join(segments)

    def missing(self):
        return [index for index in range(self.total_chunks) if index not in self.chunks]


class SenderProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.ack_queues = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        try:
            frame = decode_frame(data)
        except FrameError:
            return
        if frame.kind not in (ACK_FRAME, ACK_STATE_FRAME):
            return
        queue = self.ack_queues.get(frame.task_id)
        if queue is not None:
            while queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(frame)

    def error_received(self, exc):
        logger.debug("sender UDP error: %s", type(exc).__name__)


class ReceiverProtocol(asyncio.DatagramProtocol):
    def __init__(self, tasks):
        self.tasks = tasks
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, address):
        try:
            frame = decode_frame(data)
        except FrameError:
            return
        if frame.kind == GUIDE_FRAME:
            try:
                guide = decode_guide(frame.payload)
            except ValueError:
                return
            state = self.tasks.get(frame.task_id)
            if state is None:
                state = ReceiverTask(frame.task_id, 1)
                self.tasks[frame.task_id] = state
            if state.status not in ("success", "error"):
                def rank(value):
                    return len(value.get("anchors", [])) + len(value.get("entities", [])) + bool(value.get("layout"))
                if (state.guide.get("source_sha256") != guide["source_sha256"]
                        or rank(guide) >= rank(state.guide)):
                    state.guide = guide
            return
        if frame.kind not in (META_FRAME, DATA_FRAME, COMPRESSED_DATA_FRAME, ACK_QUERY_FRAME,
                              BLOCK_META_FRAME, BLOCK_DATA_FRAME):
            return
        if frame.total < 1 or frame.total > MAX_CHUNKS:
            return
        state = self.tasks.get(frame.task_id)
        if state is None:
            state = ReceiverTask(frame.task_id, max(1, frame.total))
            self.tasks[frame.task_id] = state
        if frame.kind == META_FRAME:
            self._on_meta(state, frame)
        elif frame.kind == BLOCK_META_FRAME:
            self._on_block_meta(state, frame)
        elif frame.kind == BLOCK_DATA_FRAME:
            self._on_block_data(state, frame)
        elif frame.kind in (DATA_FRAME, COMPRESSED_DATA_FRAME):
            self._on_data(state, frame)
        else:
            self._send_ack(state, frame)

    def _on_meta(self, state, frame):
        if state.transport_mode == "blocks":
            return
        if len(frame.payload) != META.size:
            return
        expected_bytes, deadline_ms, chunk_size, digest = META.unpack(frame.payload)
        expected_chunks = max(1, (expected_bytes + chunk_size - 1) // chunk_size) if chunk_size else 0
        if (not chunk_size or chunk_size > 1200 or frame.total != expected_chunks
                or expected_bytes > MAX_SOURCE_BYTES):
            return
        if any(index >= frame.total for index in state.chunks):
            return
        if state.expected_bytes is not None and (state.digest != digest
                or state.expected_bytes != expected_bytes or state.chunk_size != chunk_size):
            return
        state.transport_mode = "legacy"
        state.total_chunks = frame.total
        state.expected_bytes = expected_bytes
        state.chunk_size = chunk_size
        state.digest = digest
        incoming_deadline = time.monotonic() + max(1, deadline_ms) / 1000
        state.deadline_at = min(state.deadline_at or incoming_deadline, incoming_deadline)
        if state.finalize_task is None:
            state.finalize_task = asyncio.create_task(self._partial_at_deadline(state))
        self._maybe_complete(state)

    def _on_data(self, state, frame):
        if state.transport_mode == "blocks":
            return
        if frame.sequence >= frame.total:
            return
        if (state.expected_bytes is not None or state.chunks) and frame.total != state.total_chunks:
            return
        state.total_chunks = frame.total
        try:
            payload = decompress_chunk(frame.kind, frame.payload)
        except FrameError:
            return
        if state.chunk_size is not None:
            expected_size = (state.chunk_size if frame.sequence < state.total_chunks - 1
                             else state.expected_bytes - state.chunk_size * (state.total_chunks - 1))
            if len(payload) != expected_size:
                return
        old = state.chunks.get(frame.sequence)
        if old is None:
            state.chunks[frame.sequence] = payload
        elif old != payload:
            return
        state.transport_mode = "legacy"
        self._maybe_complete(state)

    def _on_block_meta(self, state, frame):
        if state.transport_mode == "legacy" or len(frame.payload) != BLOCK_META.size:
            return
        expected_bytes, deadline_ms, block_count, digest = BLOCK_META.unpack(frame.payload)
        if not 1 <= expected_bytes <= MAX_SOURCE_BYTES or not 1 <= block_count <= frame.total:
            return
        if state.chunks and state.total_chunks != frame.total:
            return
        if state.block_decoder is not None and (state.block_decoder.total != frame.total
                                                  or not state.block_decoder.within(expected_bytes)):
            return
        if state.expected_bytes is not None and (state.expected_bytes != expected_bytes
                or state.digest != digest or state.block_count != block_count):
            return
        state.transport_mode = "blocks"
        state.total_chunks = frame.total
        state.expected_bytes = expected_bytes
        state.block_count = block_count
        state.digest = digest
        if state.block_decoder is None:
            state.block_decoder = BlockDecoder(frame.total)
        incoming_deadline = time.monotonic() + max(1, deadline_ms) / 1000
        state.deadline_at = min(state.deadline_at or incoming_deadline, incoming_deadline)
        if state.finalize_task is None:
            state.finalize_task = asyncio.create_task(self._partial_at_deadline(state))
        self._maybe_complete(state)

    def _on_block_data(self, state, frame):
        if state.transport_mode == "legacy" or frame.sequence >= frame.total:
            return
        if state.chunks and frame.total != state.total_chunks:
            return
        if state.expected_bytes is not None and frame.total != state.total_chunks:
            return
        decoder = state.block_decoder
        if decoder is None:
            decoder = BlockDecoder(frame.total)
        try:
            decoder.add(frame, state.expected_bytes)
        except FrameError:
            for sequence in decoder.discard_incomplete(frame.sequence):
                state.chunks.pop(sequence, None)
            return
        state.transport_mode = "blocks"
        state.total_chunks = frame.total
        state.block_decoder = decoder
        state.chunks[frame.sequence] = frame.payload
        self._maybe_complete(state)

    def _send_ack(self, state, frame):
        if self.transport is None or len(frame.payload) != ACK_QUERY.size:
            return
        count = ACK_QUERY.unpack(frame.payload)[0]
        if count < 1 or count > 256:
            return
        if (state.expected_bytes is not None or state.chunks) and frame.total != state.total_chunks:
            return
        state.total_chunks = frame.total
        start = frame.sequence
        count = min(count, max(0, state.total_chunks - start))
        if count == 0:
            return
        payload = ACK_STATE.pack(count, int(state.expected_bytes is not None))
        payload += ack_bitmap(state.chunks, start, count)
        packet = encode_frame(ACK_STATE_FRAME, state.task_id, start, state.total_chunks, payload)
        if state.udp_bytes_sent + len(packet) > RECEIVER_BUDGET_BYTES:
            return
        if UDP_PEER_ADDR is None:
            return
        self.transport.sendto(packet, UDP_PEER_ADDR)
        state.udp_bytes_sent += len(packet)

    def _maybe_complete(self, state):
        if state.expected_bytes is None or state.digest is None:
            return
        if len(state.chunks) != state.total_chunks:
            return
        content = state.assembled()
        if len(content) != state.expected_bytes or hashlib.sha256(content).digest() != state.digest:
            state.status = "error"
            state.error = "integrity_check_failed"
            return
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            state.status = "error"
            state.error = "invalid_utf8"
            return

        # A partial model failure should not discard a source that completed
        # during or just after that failed attempt.  Retry once on the now
        # complete source while keeping ordinary transport/model errors final.
        if state.status == "error":
            if state.partial_result:
                state.status = "processing"
                state.partial_result = False
                state.model_task = asyncio.create_task(
                    self._generate(state, content, partial=False))
            return

        # A partial result may have been published just before the final
        # retransmission arrived.  Re-open the task and let the complete
        # source get one grounded model pass; this replaces the provisional
        # result without starting duplicate upgrades for later duplicate UDP
        # frames.
        if state.status == "success":
            if state.partial_result:
                state.status = "processing"
                state.partial_result = False
                state.model_task = asyncio.create_task(
                    self._generate(state, content, partial=False))
            return
        if state.model_task is not None:
            return
        state.status = "processing"
        state.partial_result = False
        state.model_task = asyncio.create_task(self._generate(state, content, partial=False))

    async def _partial_at_deadline(self, state):
        deadline = state.deadline_at or (state.created_at + 150)
        # Keep a fixed, generous model window.  The previous proportional
        # delay left only ~46 seconds on a 120-second instance, which is too
        # little for several concurrent Gateway requests under load.
        delay = max(0, deadline - time.monotonic() - PARTIAL_MODEL_WINDOW_SECONDS)
        await asyncio.sleep(delay)
        async with state.lock:
            if state.status in ("success", "error") or state.model_task is not None:
                return
            if not state.chunks:
                return
            raw = state.partial_content()
            if not raw:
                return
            text = raw.decode("utf-8", errors="ignore")
            if not text:
                return
            state.status = "processing"
            state.partial_result = True
            state.model_task = asyncio.create_task(self._generate(state, text.encode("utf-8"), partial=True))

    async def _generate(self, state, raw, partial):
        task_type = "semantic_reconstruction_partial" if partial else "semantic_reconstruction"
        started = time.monotonic()
        try:
            text = raw.decode("utf-8")
            with telemetry.session(state.task_id, task_type,
                                   os.getenv("CHALLENGE_SESSION_ID")):
                with task() as span:
                    primary_error = None
                    try:
                        result = await call_model(text, partial, state.deadline_at,
                                                  guide_provider=lambda: state.guide)
                    except Exception as exc:
                        if not partial:
                            raise
                        primary_error = exc
                        result = None
                    used_partial = partial
                    if partial and len(state.chunks) == state.total_chunks:
                        complete = state.assembled()
                        if (len(complete) == state.expected_bytes
                                and hashlib.sha256(complete).digest() == state.digest):
                            try:
                                result = await call_model(complete.decode("utf-8"), False, state.deadline_at,
                                                          guide_provider=lambda: state.guide)
                                used_partial = False
                            except Exception as exc:
                                logger.warning("late complete-source refinement unavailable: %s",
                                               type(exc).__name__)
                    if not result:
                        if primary_error is not None:
                            raise primary_error
                        raise RuntimeError("empty_model_output")
                    span.result(result)
            # Export the closed task/session before the platform can lock B and stop this process.
            await asyncio.to_thread(telemetry.flush)
            if time.monotonic() >= state.deadline_at:
                raise TimeoutError("finalization_deadline_exceeded")
            state.content = result
            state.partial_result = used_partial
            state.status = "success"
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            request_id = getattr(exc, "request_id", None)
            logger.error(
                "model generation failed task=%s type=%s status=%s request_id=%s elapsed=%.1fs",
                state.task_id, type(exc).__name__, status_code, request_id,
                time.monotonic() - started,
            )
            state.status = "error"
            state.error = "model_call_failed"
            await asyncio.to_thread(telemetry.flush)

    def error_received(self, exc):
        logger.debug("receiver UDP error: %s", type(exc).__name__)


sender_protocol = None
sender_transport = None
sender_lock = asyncio.Lock()
sender_jobs = set()
sender_background_tasks = set()
receiver_tasks = {}
receiver_transport = None


async def call_model(article_text, partial, deadline_at, purpose="receiver", guide_provider=None):
    base_url = os.getenv("PLATFORM_LLM_URL") or os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("PLATFORM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not base_url or not api_key:
        raise RuntimeError("platform_model_configuration_missing")
    if deadline_at is None:
        raise RuntimeError("task_deadline_missing")
    initial_budget = request_budget(deadline_at)
    async with AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=initial_budget,
                           max_retries=0) as client:
        async def request(messages, max_tokens, budget):
            # A length-terminated JSON response is not parseable reliably and
            # a factual record may end in the middle of a number or negation.
            # Retry once with a larger output ceiling while retaining the same
            # absolute request budget; a second truncation is a hard failure
            # and lets the caller choose an independent grounded path.
            request_started = time.monotonic()
            output_tokens = max(256, int(max_tokens))
            for attempt in range(2):
                try:
                    remaining = max(0.05, budget - (time.monotonic() - request_started))
                    async with asyncio.timeout(remaining):
                        response = await client.with_options(timeout=remaining).chat.completions.create(
                            model=MODEL_NAME,
                            temperature=0,
                            max_tokens=output_tokens,
                            messages=messages,
                        )
                except APIConnectionError as exc:
                    raise ConnectionError("model_gateway_connection_failed") from exc
                if not response.choices:
                    raise RuntimeError("model_output_incomplete")
                choice = response.choices[0]
                content = choice.message.content
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("model_returned_empty_output")
                if choice.finish_reason == "stop":
                    return content.strip()
                if choice.finish_reason != "length" or attempt:
                    raise RuntimeError("model_output_truncated")
                logger.warning("model output reached token limit; retrying with a larger output ceiling")
                output_tokens = min(max(512, output_tokens * 2), 8192)
            raise RuntimeError("model_output_truncated")

        if purpose == "sender":
            return await encode_for_transport(article_text, request, deadline_at)
        try:
            source = unpack_source(article_text)
        except ValueError:
            if not partial:
                raise
            source = SourceView(article_text, byte_size(article_text))
        return await generate_adaptive_record(source.text, partial, deadline_at, request,
            encoded=source.encoded,
            guide_provider=lambda: validated_guide(guide_provider() if guide_provider else {}, source.text))


def send_packet(protocol, packet):
    if protocol is None or protocol.transport is None:
        raise RuntimeError("sender_udp_not_ready")
    if UDP_PEER_ADDR is None:
        raise RuntimeError("udp_peer_address_not_resolved")
    protocol.transport.sendto(packet, UDP_PEER_ADDR)


def make_data_packet(task_id, sequence, total, payload):
    kind, encoded = compress_chunk(payload)
    return encode_frame(kind, task_id, sequence, total, encoded)


async def await_batch_ack(protocol, task_id, start, count, timeout, total=None):
    queue = protocol.ack_queues[task_id]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            frame = await asyncio.wait_for(queue.get(), deadline - time.monotonic())
        except asyncio.TimeoutError:
            return None
        if frame.sequence != start or (total is not None and frame.total != total):
            continue
        bitmap_bytes = (count + 7) // 8
        if frame.kind == ACK_STATE_FRAME:
            if len(frame.payload) != ACK_STATE.size + bitmap_bytes:
                continue
            received_count, flags = ACK_STATE.unpack_from(frame.payload)
            if received_count != count or flags not in (0, 1):
                continue
            return frame.payload[ACK_STATE.size:], bool(flags)
        if frame.kind == ACK_FRAME and len(frame.payload) == bitmap_bytes:
            return frame.payload, True
    return None


async def send_article(task_id, article, chunk_size, deadline_ms, accepted_at=None):
    if sender_protocol is None or sender_protocol.transport is None:
        raise RuntimeError("sender_udp_not_ready")
    plan = build_transmission_plan(task_id, article, chunk_size, SEND_WINDOW)
    total = len(plan.packets)
    deadline_at = (accepted_at if accepted_at is not None else time.monotonic()) + deadline_ms / 1000

    def meta_packet():
        remaining_ms = max(1, int((deadline_at - time.monotonic()) * 1000))
        return plan.metadata(task_id, remaining_ms)
    while UDP_PEER_ADDR is None and time.monotonic() < deadline_at:
        await asyncio.sleep(0.1)
    if UDP_PEER_ADDR is None:
        raise RuntimeError("udp_peer_host_unavailable")
    queue = asyncio.Queue(maxsize=64)
    sent_bytes = 0
    data_bytes = 0
    control_bytes = 0
    retransmit_bytes = 0
    transmitted_sequences = set()
    ack_timeout = ACK_TIMEOUT_SECONDS

    def send_limited(packet, sequence=None):
        nonlocal sent_bytes, data_bytes, control_bytes, retransmit_bytes
        if sent_bytes + len(packet) > SENDER_BUDGET_BYTES:
            return False
        send_packet(sender_protocol, packet)
        sent_bytes += len(packet)
        if sequence is None:
            control_bytes += len(packet)
        else:
            data_bytes += len(packet)
            if sequence in transmitted_sequences:
                retransmit_bytes += len(packet)
            transmitted_sequences.add(sequence)
        return True

    sender_protocol.ack_queues[task_id] = queue
    try:
        async with sender_lock:
            if time.monotonic() >= deadline_at:
                return
            if not send_limited(meta_packet()):
                return
            await asyncio.sleep(0.02)
            if not send_limited(meta_packet()):
                return
            for start in range(0, total, SEND_WINDOW):
                count = min(SEND_WINDOW, total - start)
                pending = set(range(start, start + count))
                metadata_confirmed = False
                for attempt in range(MAX_BATCH_ATTEMPTS):
                    if time.monotonic() >= deadline_at:
                        return
                    if not send_limited(meta_packet()):
                        return
                    await asyncio.sleep(0.015)
                    for index in sorted(pending):
                        if not send_limited(plan.packets[index], sequence=index):
                            return
                        await asyncio.sleep(0.002)
                    query = encode_frame(ACK_QUERY_FRAME, task_id, start, total,
                                         ACK_QUERY.pack(count))
                    # Re-query state before replaying data: lost feedback is not evidence of lost data.
                    for query_attempt in range(2):
                        if time.monotonic() >= deadline_at or not send_limited(query):
                            return
                        queried_at = time.monotonic()
                        receipt = await await_batch_ack(protocol=sender_protocol, task_id=task_id,
                            start=start, count=count, total=total,
                            timeout=min(ack_timeout, max(0.01, deadline_at - time.monotonic())))
                        if receipt is not None:
                            bitmap, has_metadata = receipt
                            metadata_confirmed = metadata_confirmed or has_metadata
                            pending.difference_update(index for index in range(start, start + count)
                                                      if bitmap_has(bitmap, index - start))
                            sample = time.monotonic() - queried_at
                            ack_timeout = min(3.0, max(0.4, 0.75 * ack_timeout + 0.25 * sample * 2.5))
                            if not pending and metadata_confirmed:
                                break
                            if not has_metadata and not send_limited(meta_packet()):
                                return
                        await asyncio.sleep(min(0.03, max(0, deadline_at - time.monotonic())))
                    if not pending and metadata_confirmed:
                        break
                    await asyncio.sleep(min(0.1 * (attempt + 1), max(0, deadline_at - time.monotonic())))
    finally:
        sender_protocol.ack_queues.pop(task_id, None)
        logger.info("transport task=%s mode=%s packets=%s planned_bytes=%s sent_bytes=%s "
                    "data_bytes=%s control_bytes=%s retransmit_bytes=%s",
                    task_id, plan.mode, total, plan.estimated_bytes, sent_bytes,
                    data_bytes, control_bytes, retransmit_bytes)


async def sender_job(task_id, article, chunk_size, deadline_ms, accepted_at):
    deadline_at = accepted_at + deadline_ms / 1000

    try:
        with telemetry.session(task_id, "sender_transport", os.getenv("CHALLENGE_SESSION_ID")):
            with task() as span:
                raw_payload = (pack_source(article.decode("utf-8"), len(article), encoded=False).encode("utf-8")
                               if article.startswith(PACKET_PREFIX.encode("ascii")) else article)
                payload = raw_payload
                remaining = deadline_at - time.monotonic()
                mode = os.getenv("SEMANTIC_MODE", "aggressive").strip().lower()
                if mode != "lossless" and len(article) >= 1024 and remaining > 30:
                    try:
                        encoding_deadline = time.monotonic() + remaining * 0.40
                        candidate = await call_model(article.decode("utf-8"), False,
                                                     encoding_deadline, purpose="sender")
                        raw_plan = build_transmission_plan(task_id, raw_payload, chunk_size, SEND_WINDOW)
                        semantic_plan = build_transmission_plan(task_id, candidate, chunk_size, SEND_WINDOW)
                        if semantic_plan.estimated_bytes >= raw_plan.estimated_bytes * 0.96:
                            logger.info("sender agent action=lossless predicted_semantic=%s predicted_lossless=%s",
                                        semantic_plan.estimated_bytes, raw_plan.estimated_bytes)
                        else:
                            payload = candidate
                    except Exception as exc:
                        logger.warning("sender semantic encoding unavailable task=%s reason=%s",
                                       task_id, type(exc).__name__)
                await send_article(task_id, payload, chunk_size, deadline_ms, accepted_at)
                span.result({"task_id": task_id, "article_bytes": len(article),
                             "transport_source_bytes": len(payload), "semantic_encoded": payload != raw_payload})
    except Exception as exc:
        logger.exception("sender job failed for task %s: %s", task_id, type(exc).__name__)
    finally:
        await asyncio.to_thread(telemetry.flush)
        sender_jobs.discard(task_id)


@app.on_event("startup")
async def startup():
    global sender_protocol, sender_transport, receiver_transport, peer_resolver_task
    if ROLE not in ("sender", "receiver"):
        raise RuntimeError("NETWORK_ROLE must be sender or receiver")
    loop = asyncio.get_running_loop()
    if ROLE == "sender":
        sender_protocol = SenderProtocol()
        sender_transport, _ = await loop.create_datagram_endpoint(
            lambda: sender_protocol, local_addr=("0.0.0.0", 9000), family=socket.AF_INET)
    else:
        receiver_transport, _ = await loop.create_datagram_endpoint(
            lambda: ReceiverProtocol(receiver_tasks), local_addr=("0.0.0.0", 9000), family=socket.AF_INET)
    peer_resolver_task = asyncio.create_task(resolve_peer_forever(loop))


async def resolve_peer_forever(loop):
    global UDP_PEER_ADDR
    while UDP_PEER_ADDR is None:
        try:
            peer_infos = await loop.getaddrinfo(UDP_HOST, UDP_PORT, family=socket.AF_INET,
                                                type=socket.SOCK_DGRAM)
            if peer_infos:
                UDP_PEER_ADDR = peer_infos[0][4]
                logger.info("UDP peer resolved")
                return
        except (OSError, asyncio.TimeoutError) as exc:
            logger.debug("UDP peer DNS is not ready: %s", type(exc).__name__)
        await asyncio.sleep(1.0)


@app.on_event("shutdown")
async def shutdown():
    if sender_transport is not None:
        sender_transport.close()
    if receiver_transport is not None:
        receiver_transport.close()
    pending_jobs = []
    for pending in sender_background_tasks:
        if not pending.done():
            pending.cancel()
            pending_jobs.append(pending)
    if peer_resolver_task is not None and not peer_resolver_task.done():
        peer_resolver_task.cancel()
        pending_jobs.append(peer_resolver_task)
    for state in receiver_tasks.values():
        for pending in (state.finalize_task, state.model_task):
            if pending and not pending.done():
                pending.cancel()
                pending_jobs.append(pending)
    await asyncio.gather(*pending_jobs, return_exceptions=True)
    await asyncio.to_thread(telemetry.flush)
    await asyncio.to_thread(telemetry.shutdown)


@app.get("/health")
async def health():
    return {"status": "ok", "role": ROLE,
            "telemetry_configured": bool(telemetry.export_enabled)}


@app.post("/send")
async def send(request: Request):
    if ROLE != "sender":
        raise HTTPException(status_code=404, detail="not_sender")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail={"error": "invalid_json"})
    task_id = body.get("task_id") if isinstance(body, dict) else None
    payload_b64 = body.get("payload_b64") if isinstance(body, dict) else None
    chunk_size = body.get("chunk_size") if isinstance(body, dict) else None
    deadline_ms = body.get("deadline_ms") if isinstance(body, dict) else None
    if not isinstance(task_id, str) or not task_id or len(task_id.encode("utf-8")) > 128:
        raise HTTPException(status_code=400, detail={"error": "invalid_task_id"})
    if not isinstance(payload_b64, str):
        raise HTTPException(status_code=400, detail={"error": "invalid_payload_base64"})
    try:
        article = base64.b64decode(payload_b64, validate=True)
        article.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail={"error": "invalid_payload_base64_or_utf8"})
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or not 1 <= chunk_size <= 1200:
        raise HTTPException(status_code=400, detail={"error": "invalid_chunk_size"})
    if not isinstance(deadline_ms, int) or isinstance(deadline_ms, bool) or not 1000 <= deadline_ms <= 3600000:
        raise HTTPException(status_code=400, detail={"error": "invalid_deadline_ms"})
    if task_id in sender_jobs:
        raise HTTPException(status_code=409, detail={"error": "task_already_accepted"})
    sender_jobs.add(task_id)
    background = asyncio.create_task(sender_job(task_id, article, chunk_size, deadline_ms, time.monotonic()))
    sender_background_tasks.add(background)
    background.add_done_callback(sender_background_tasks.discard)
    return JSONResponse(status_code=202, content={"accepted": True, "task_id": task_id})


@app.get("/result/{task_id}")
async def result(task_id: str):
    if ROLE != "receiver":
        raise HTTPException(status_code=404, detail="not_receiver")
    state = receiver_tasks.get(task_id)
    if state is None:
        return {"task_id": task_id, "status": "pending",
                "received_chunks": [], "missing_chunks": []}
    received = sorted(state.chunks)
    missing = state.missing()
    if state.status == "success":
        return {"task_id": task_id, "status": "success",
                "reconstructed": {"content": state.content, "format": "text"},
                "received_chunks": received, "missing_chunks": missing}
    if state.status == "error":
        return {"task_id": task_id, "status": "error", "error": state.error,
                "received_chunks": received, "missing_chunks": missing}
    return {"task_id": task_id, "status": state.status,
            "received_chunks": received, "missing_chunks": missing}


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
