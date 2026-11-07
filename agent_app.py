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
from openai import AsyncOpenAI

from agenthack import AgentRuntime, BaseAgent
from agenthack.telemetry import task
from model_policy import request_budget

from wire import (
    ACK_FRAME,
    ACK_QUERY,
    ACK_QUERY_FRAME,
    COMPRESSED_DATA_FRAME,
    DATA_FRAME,
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
PARTIAL_RESULT_MARGIN_SECONDS = 22.0
SENDER_BUDGET_BYTES = 15 * 1024 * 1024
RECEIVER_BUDGET_BYTES = 1 * 1024 * 1024
MAX_CHUNKS = 65536
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
    status: str = "pending"
    content: Optional[str] = None
    error: Optional[str] = None
    udp_bytes_sent: int = 0
    model_task: Optional[asyncio.Task] = None
    finalize_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def assembled(self):
        if self.chunk_size is None:
            return b""
        return b"".join(self.chunks[index] for index in sorted(self.chunks))

    def partial_content(self):
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
        if frame.kind != ACK_FRAME:
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
        if frame.kind not in (META_FRAME, DATA_FRAME, COMPRESSED_DATA_FRAME, ACK_QUERY_FRAME):
            return
        if frame.total < 1 or frame.total > MAX_CHUNKS:
            return
        state = self.tasks.get(frame.task_id)
        if state is None:
            state = ReceiverTask(frame.task_id, max(1, frame.total))
            self.tasks[frame.task_id] = state
        if frame.kind == META_FRAME:
            self._on_meta(state, frame)
        elif frame.kind in (DATA_FRAME, COMPRESSED_DATA_FRAME):
            self._on_data(state, frame)
        else:
            self._send_ack(state, frame)

    def _on_meta(self, state, frame):
        if len(frame.payload) != META.size:
            return
        expected_bytes, deadline_ms, chunk_size, digest = META.unpack(frame.payload)
        expected_chunks = max(1, (expected_bytes + chunk_size - 1) // chunk_size) if chunk_size else 0
        if not chunk_size or chunk_size > 1200 or frame.total != expected_chunks:
            return
        if any(index >= frame.total for index in state.chunks):
            return
        if state.expected_bytes is not None and state.digest != digest:
            return
        state.total_chunks = frame.total
        state.expected_bytes = expected_bytes
        state.chunk_size = chunk_size
        state.digest = digest
        state.deadline_at = state.created_at + max(1, deadline_ms) / 1000
        if state.finalize_task is None:
            state.finalize_task = asyncio.create_task(self._partial_at_deadline(state))
        self._maybe_complete(state)

    def _on_data(self, state, frame):
        if frame.sequence >= frame.total:
            return
        if state.expected_bytes is not None and frame.total != state.total_chunks:
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
        self._maybe_complete(state)

    def _send_ack(self, state, frame):
        if self.transport is None or len(frame.payload) != ACK_QUERY.size:
            return
        count = ACK_QUERY.unpack(frame.payload)[0]
        if count < 1 or count > 256:
            return
        if state.expected_bytes is not None and frame.total != state.total_chunks:
            return
        state.total_chunks = frame.total
        start = frame.sequence
        count = min(count, max(0, state.total_chunks - start))
        if count == 0:
            return
        payload = ack_bitmap(state.chunks, start, count)
        packet = encode_frame(ACK_FRAME, state.task_id, start, state.total_chunks, payload)
        if state.udp_bytes_sent + len(packet) > RECEIVER_BUDGET_BYTES:
            return
        if UDP_PEER_ADDR is None:
            return
        self.transport.sendto(packet, UDP_PEER_ADDR)
        state.udp_bytes_sent += len(packet)

    def _maybe_complete(self, state):
        if state.status in ("success", "error") or state.model_task is not None:
            return
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
        state.status = "processing"
        state.model_task = asyncio.create_task(self._generate(state, content, partial=False))

    async def _partial_at_deadline(self, state):
        deadline = state.deadline_at or (state.created_at + 150)
        delay = max(0, deadline - time.monotonic() - PARTIAL_RESULT_MARGIN_SECONDS)
        await asyncio.sleep(delay)
        async with state.lock:
            if state.status in ("success", "error") or state.model_task is not None:
                return
            raw = state.partial_content()
            if not raw:
                return
            text = raw.decode("utf-8", errors="ignore")
            if not text:
                return
            state.status = "processing"
            state.model_task = asyncio.create_task(self._generate(state, text.encode("utf-8"), partial=True))

    async def _generate(self, state, raw, partial):
        task_type = "semantic_reconstruction_partial" if partial else "semantic_reconstruction"
        started = time.monotonic()
        try:
            text = raw.decode("utf-8")
            with telemetry.session(state.task_id, task_type,
                                   os.getenv("CHALLENGE_SESSION_ID")):
                try:
                    with task() as span:
                        result = await call_model(text, partial, state.deadline_at)
                        if not result:
                            raise RuntimeError("empty_model_output")
                        span.result(result)
                        state.content = result
                        state.status = "success"
                finally:
                    await asyncio.to_thread(telemetry.flush)
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

    def error_received(self, exc):
        logger.debug("receiver UDP error: %s", type(exc).__name__)


sender_protocol = None
sender_transport = None
sender_lock = asyncio.Lock()
sender_jobs = set()
receiver_tasks = {}
receiver_transport = None


async def call_model(article_text, partial, deadline_at):
    base_url = os.getenv("PLATFORM_LLM_URL") or os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("PLATFORM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not base_url or not api_key:
        raise RuntimeError("platform_model_configuration_missing")
    if deadline_at is None:
        raise RuntimeError("task_deadline_missing")
    initial_budget = request_budget(deadline_at)
    async with AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=initial_budget,
                           max_retries=0) as client:
        completeness = (
            "The source is incomplete. Preserve every fact present in received segments and do not bridge missing "
            "segments."
            if partial else "The source is complete."
        )
        compact_prompt = (
            "Create the lockable factual record B in the source language. Preserve each distinct answer-bearing "
            "fact, including names, roles, dates, places, quantities, units, actions, relations, causes, sequence, "
            "negation, uncertainty, conditions, and exceptions. Keep facts explicit, but remove repeated wording, "
            "narrative transitions, and generic framing. Prefer compact clauses separated by semicolons and terse "
            "labels such as 'date:', 'actor:', 'action:', 'amount:', 'cause:'. Do not write an introduction or "
            "conclusion. Aim for less than 80% of the source UTF-8 byte length while retaining all testable facts; "
            "when that target conflicts with factual coverage, retain the fact. Do not add outside knowledge, "
            "questions, or answers. " + completeness
        )
        max_tokens = min(6000, max(1400, int(len(article_text) * 1.2)))

        async def request(messages, budget):
            async with asyncio.timeout(budget):
                response = await client.with_options(timeout=budget).chat.completions.create(
                    model=MODEL_NAME,
                    temperature=0,
                    max_tokens=max_tokens,
                    messages=messages,
                )
            return (response.choices[0].message.content or "").strip()

        primary = None
        last_error = None
        for attempt in range(2):
            try:
                budget = request_budget(deadline_at, fraction=0.68 if attempt == 0 else 1.0)
                primary = await request([
                    {"role": "system", "content": compact_prompt},
                    {"role": "user", "content": article_text},
                ], budget)
                if primary:
                    break
                last_error = RuntimeError("model_returned_empty_output")
            except Exception as exc:
                last_error = exc
                status = getattr(exc, "status_code", None)
                logger.warning("primary model call attempt=%s type=%s status=%s",
                               attempt + 1, type(exc).__name__, status)
                if status in (400, 401, 403) or attempt == 1:
                    break

        if not primary:
            if last_error:
                raise last_error
            raise RuntimeError("model_returned_empty_output")

        # The first successful model output is always a usable deadline fallback.
        candidate = primary
        try:
            audit_budget = request_budget(deadline_at, fraction=0.5)
            audit_prompt = (
                "Audit the draft factual record against the original article. Correct omissions or distortions, "
                "preserve every distinct answer-bearing detail, and return only the final factual record. Keep the "
                "source language. Compress repeated words and transitions; target less than 80% of the original "
                "article's UTF-8 byte length when factual coverage permits. Never remove a distinct fact just to "
                "meet the size target. Do not add questions, answers, or commentary. " + completeness
            )
            audited = await request([
                {"role": "system", "content": audit_prompt},
                {"role": "user", "content": "ORIGINAL ARTICLE:\n" + article_text
                 + "\n\nDRAFT RECORD:\n" + primary},
            ], audit_budget)
            if audited and len(audited.encode("utf-8")) < len(candidate.encode("utf-8")):
                candidate = audited
        except Exception as exc:
            logger.warning("optional model audit unavailable; preserving primary output: %s",
                           type(exc).__name__)
        return candidate


def send_packet(protocol, packet):
    if protocol is None or protocol.transport is None:
        raise RuntimeError("sender_udp_not_ready")
    if UDP_PEER_ADDR is None:
        raise RuntimeError("udp_peer_address_not_resolved")
    protocol.transport.sendto(packet, UDP_PEER_ADDR)


def make_data_packet(task_id, sequence, total, payload):
    kind, encoded = compress_chunk(payload)
    return encode_frame(kind, task_id, sequence, total, encoded)


async def await_batch_ack(protocol, task_id, start, count, timeout):
    queue = protocol.ack_queues[task_id]
    end = start + count
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            frame = await asyncio.wait_for(queue.get(), deadline - time.monotonic())
        except asyncio.TimeoutError:
            return None
        if frame.sequence == start and len(frame.payload) >= (count + 7) // 8:
            return frame.payload
        if start <= frame.sequence < end:
            return frame.payload
    return None


async def send_article(task_id, article, chunk_size, deadline_ms):
    if sender_protocol is None or sender_protocol.transport is None:
        raise RuntimeError("sender_udp_not_ready")
    usable = max_payload(task_id)
    actual_chunk_size = min(max(chunk_size, 256), usable, 1200)
    if actual_chunk_size < 1:
        raise ValueError("invalid_chunk_size")
    chunks = [article[i:i + actual_chunk_size] for i in range(0, len(article), actual_chunk_size)] or [b""]
    total = len(chunks)
    digest = hashlib.sha256(article).digest()
    meta = META.pack(len(article), deadline_ms, actual_chunk_size, digest)
    meta_packet = encode_frame(META_FRAME, task_id, 0, total, meta)
    deadline_at = time.monotonic() + deadline_ms / 1000
    while UDP_PEER_ADDR is None and time.monotonic() < deadline_at:
        await asyncio.sleep(0.1)
    if UDP_PEER_ADDR is None:
        raise RuntimeError("udp_peer_host_unavailable")
    queue = asyncio.Queue(maxsize=64)
    sent_bytes = 0

    def send_limited(packet):
        nonlocal sent_bytes
        if sent_bytes + len(packet) > SENDER_BUDGET_BYTES:
            return False
        send_packet(sender_protocol, packet)
        sent_bytes += len(packet)
        return True

    sender_protocol.ack_queues[task_id] = queue
    try:
        async with sender_lock:
            if not send_limited(meta_packet):
                return
            await asyncio.sleep(0.02)
            if not send_limited(meta_packet):
                return
            for start in range(0, total, SEND_WINDOW):
                count = min(SEND_WINDOW, total - start)
                pending = set(range(start, start + count))
                for attempt in range(MAX_BATCH_ATTEMPTS):
                    if time.monotonic() >= deadline_at:
                        return
                    if not send_limited(meta_packet):
                        return
                    await asyncio.sleep(0.015)
                    for index in sorted(pending):
                        packet = make_data_packet(task_id, index, total, chunks[index])
                        if not send_limited(packet):
                            return
                        await asyncio.sleep(0.002)
                    query = encode_frame(ACK_QUERY_FRAME, task_id, start, total,
                                         ACK_QUERY.pack(count))
                    if not send_limited(query):
                        return
                    bitmap = await await_batch_ack(protocol=sender_protocol, task_id=task_id,
                                                   start=start, count=count,
                                                   timeout=min(ACK_TIMEOUT_SECONDS, max(0.1, deadline_at - time.monotonic())))
                    if bitmap is not None:
                        pending = {index for index in range(start, start + count)
                                   if not bitmap_has(bitmap, index - start)}
                        if not pending:
                            break
                    await asyncio.sleep(min(0.1 * (attempt + 1), max(0, deadline_at - time.monotonic())))
    finally:
        sender_protocol.ack_queues.pop(task_id, None)


async def sender_job(task_id, article, chunk_size, deadline_ms):
    try:
        with telemetry.session(task_id, "sender_transport", os.getenv("CHALLENGE_SESSION_ID")):
            with task() as span:
                await send_article(task_id, article, chunk_size, deadline_ms)
                span.result({"task_id": task_id, "article_bytes": len(article)})
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
    if peer_resolver_task is not None and not peer_resolver_task.done():
        peer_resolver_task.cancel()
    for state in receiver_tasks.values():
        for pending in (state.finalize_task, state.model_task):
            if pending and not pending.done():
                pending.cancel()
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
    asyncio.create_task(sender_job(task_id, article, chunk_size, deadline_ms))
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
