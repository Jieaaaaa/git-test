import asyncio
import hashlib
import json
import os
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import agent_app
except ImportError:
    agent_app = None


@unittest.skipIf(agent_app is None, "requires official base-image dependencies")
class ClientAndLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_length_gateway_output_is_rejected(self):
        class FakeClient:
            calls = 0

            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def with_options(self, **kwargs):
                return self

            async def create(self, **kwargs):
                FakeClient.calls += 1
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="length", message=SimpleNamespace(content="Truncated text"))])

        with patch.dict(os.environ, {"PLATFORM_LLM_URL": "http://unused/v1", "PLATFORM_API_KEY": "test"}), \
                patch.object(agent_app, "AsyncOpenAI", FakeClient):
            with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
                await agent_app.call_model("Received source text", False, time.monotonic() + 20)
        self.assertGreaterEqual(FakeClient.calls, 2)

    async def test_length_gateway_output_retries_and_accepts_complete_response(self):
        class FakeClient:
            calls = 0

            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def with_options(self, **kwargs):
                return self

            async def create(self, **kwargs):
                FakeClient.calls += 1
                if FakeClient.calls == 1:
                    return SimpleNamespace(choices=[SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(content="Partial record"))])
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps({"groups": [
                        {"subject": "Record", "facts": ["Complete model record"]}]})))])

        with patch.dict(os.environ, {"PLATFORM_LLM_URL": "http://unused/v1", "PLATFORM_API_KEY": "test"}), \
                patch.object(agent_app, "AsyncOpenAI", FakeClient):
            result = await agent_app.call_model("Received source text", False, time.monotonic() + 20)
        self.assertIn("Complete model record", result)
        self.assertGreaterEqual(FakeClient.calls, 2)

    async def test_success_is_published_after_session_close_and_flush(self):
        events = []
        state = agent_app.ReceiverTask("lifecycle-test", 1, status="processing",
                                       deadline_at=time.monotonic() + 20)

        @contextmanager
        def session(*args):
            events.append("session_enter")
            yield
            events.append("session_exit")

        @contextmanager
        def task():
            yield SimpleNamespace(result=lambda value: events.append("task_result"))
            events.append("task_exit")

        def flush():
            self.assertEqual(state.status, "processing")
            self.assertEqual(events[-1], "session_exit")
            events.append("flush")

        async def model(*args, **kwargs):
            return "Model-produced factual record"

        fake_telemetry = SimpleNamespace(session=session, flush=flush)
        protocol = agent_app.ReceiverProtocol({state.task_id: state})
        with patch.object(agent_app, "telemetry", fake_telemetry), \
                patch.object(agent_app, "task", task), patch.object(agent_app, "call_model", model):
            await protocol._generate(state, b"Received source", False)
        self.assertEqual(state.status, "success")
        self.assertEqual(events, ["session_enter", "task_result", "task_exit", "session_exit", "flush"])

    async def test_repeated_meta_cannot_extend_deadline(self):
        state = agent_app.ReceiverTask("deadline-test", 1)
        protocol = agent_app.ReceiverProtocol({state.task_id: state})
        frame = SimpleNamespace(total=1, payload=agent_app.META.pack(100, 10000, 512, b"x" * 32))
        protocol._on_meta(state, frame)
        deadline = state.deadline_at
        frame.payload = agent_app.META.pack(100, 120000, 512, b"x" * 32)
        protocol._on_meta(state, frame)
        self.assertEqual(state.deadline_at, deadline)
        state.finalize_task.cancel()
        await asyncio.gather(state.finalize_task, return_exceptions=True)

    async def test_partial_reconstruction_starts_with_fixed_model_window(self):
        raw = b"A complete source fragment."
        state = agent_app.ReceiverTask(
            "partial-window-test", 1, expected_bytes=len(raw), chunk_size=len(raw),
            digest=hashlib.sha256(raw).digest(), chunks={0: raw},
            deadline_at=time.monotonic() + 0.16)
        started = asyncio.Event()

        async def fake_generate(*args, **kwargs):
            started.set()

        with patch.object(agent_app, "PARTIAL_MODEL_WINDOW_SECONDS", 0.10), \
                patch.object(agent_app.ReceiverProtocol, "_generate", fake_generate):
            await agent_app.ReceiverProtocol({state.task_id: state})._partial_at_deadline(state)
            await asyncio.wait_for(started.wait(), 0.08)
        self.assertEqual(state.status, "processing")
        self.assertTrue(state.partial_result)
        self.assertIsNotNone(state.model_task)
        await state.model_task

    async def test_late_complete_source_never_reopens_a_published_result(self):
        raw = b"A complete source fragment."
        state = agent_app.ReceiverTask(
            "partial-upgrade-test", 1, expected_bytes=len(raw), chunk_size=len(raw),
            digest=hashlib.sha256(raw).digest(), chunks={0: raw}, status="success",
            content="Partial record", partial_result=True)
        protocol = agent_app.ReceiverProtocol({state.task_id: state})
        with patch.object(agent_app.ReceiverProtocol, "_generate", AsyncMock()) as generate:
            protocol._maybe_complete(state)
        self.assertEqual(state.status, "success")
        self.assertEqual(state.content, "Partial record")
        self.assertTrue(state.partial_result)
        generate.assert_not_awaited()

    async def test_structurally_complete_fact_packet_is_kept_at_token_limit(self):
        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def with_options(self, **kwargs):
                return self

            async def create(self, **kwargs):
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length",
                    message=SimpleNamespace(content='{"groups":[{"subject":"Alpha","facts":["12 units in 2025"]}]}'))])

        with patch.dict(os.environ, {"PLATFORM_LLM_URL": "http://unused/v1", "PLATFORM_API_KEY": "test"}), \
                patch.object(agent_app, "AsyncOpenAI", FakeClient):
            result = await agent_app.call_model("Alpha received 12 units in 2025", False,
                                                 time.monotonic() + 20)
        self.assertEqual(result, "Alpha: 12 units in 2025")

    async def test_late_full_source_improves_partial_model_candidate(self):
        for partial_result, full_result, expected in (
                ("Partial-source model record", "Full-source model record", "Full-source model record"),
                ("Partial-source model record", RuntimeError("full model failed"), "Partial-source model record"),
                (RuntimeError("partial model failed"), "Full-source model record", "Full-source model record")):
            raw = b"The entire received source text."
            state = agent_app.ReceiverTask("late-complete-test", 1, expected_bytes=len(raw),
                chunk_size=512, digest=hashlib.sha256(raw).digest(), chunks={0: raw},
                status="processing", deadline_at=time.monotonic() + 20)
            model = AsyncMock(side_effect=[partial_result, full_result])
            with patch.object(agent_app, "call_model", model):
                await agent_app.ReceiverProtocol({})._generate(state, b"Received fragment", True)
            self.assertEqual(state.status, "success")
            self.assertEqual(model.call_count, 2)
            self.assertEqual(model.call_args_list[1].args[0], raw.decode())
            self.assertEqual(state.content, expected)

    async def test_real_sdk_connection_error_gets_one_fast_retry(self):
        import httpx

        class FakeClient:
            calls = 0

            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def with_options(self, **kwargs):
                return self

            async def create(self, **kwargs):
                FakeClient.calls += 1
                if FakeClient.calls == 1:
                    raise agent_app.APIConnectionError(request=httpx.Request("POST", "http://unused/v1"))
                content = json.dumps({"groups": [{"subject": "Record",
                    "facts": ["Model-generated record following retry."]}]})
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=content))])

        with patch.dict(os.environ, {"PLATFORM_LLM_URL": "http://unused/v1", "PLATFORM_API_KEY": "test"}), \
                patch.object(agent_app, "AsyncOpenAI", FakeClient):
            result = await agent_app.call_model("Received source", False, time.monotonic() + 20)
        self.assertIn("following retry", result)
        self.assertGreaterEqual(FakeClient.calls, 2)

    async def test_sender_encoder_failure_preserves_original_udp_source(self):
        source = ("Approved in 2025 with conditions. " * 80).encode()
        accepted_at = time.monotonic()
        transmit = AsyncMock()
        encoder = AsyncMock(side_effect=TimeoutError())
        with patch.dict(os.environ, {"SEMANTIC_MODE": "aggressive"}), \
                patch.object(agent_app, "call_model", encoder), patch.object(agent_app, "send_article", transmit):
            await agent_app.sender_job("sender-fallback", source, 512, 120000, accepted_at)
        self.assertEqual(encoder.call_args.kwargs["purpose"], "sender")
        transmit.assert_awaited_once_with("sender-fallback", source, 512, 120000, accepted_at)

    async def test_sender_uses_a_shorter_model_checked_semantic_payload(self):
        source = ("Agency A received 12 units in 2025; the alternative was rejected. " * 80).encode()
        candidate = agent_app.pack_source("A:12 units,2025; alternative rejected.", len(source)).encode()
        accepted_at = time.monotonic()
        transmit = AsyncMock()
        encoder = AsyncMock(return_value=candidate)
        with patch.dict(os.environ, {"SEMANTIC_MODE": "aggressive"}), \
                patch.object(agent_app, "call_model", encoder), patch.object(agent_app, "send_article", transmit):
            await agent_app.sender_job("sender-semantic", source, 512, 120000, accepted_at)
        self.assertEqual(encoder.call_args.kwargs["purpose"], "sender")
        self.assertGreater(accepted_at + 120, encoder.call_args.args[2])
        transmit.assert_awaited_once_with("sender-semantic", candidate, 512, 120000, accepted_at)

    async def test_sender_lossless_mode_does_not_run_semantic_encoder(self):
        source = ("Approved in 2025 with conditions. " * 80).encode()
        accepted_at = time.monotonic()
        transmit = AsyncMock()
        encoder = AsyncMock()
        with patch.dict(os.environ, {"SEMANTIC_MODE": "lossless"}), \
                patch.object(agent_app, "call_model", encoder), patch.object(agent_app, "send_article", transmit):
            await agent_app.sender_job("sender-lossless", source, 512, 120000, accepted_at)
        encoder.assert_not_awaited()
        transmit.assert_awaited_once_with("sender-lossless", source, 512, 120000, accepted_at)

    async def test_sender_encoder_gets_a_bounded_fraction_of_remaining_time(self):
        source = ("Approved in 2025 with conditions. " * 80).encode()
        accepted_at = time.monotonic()
        transmit = AsyncMock()
        observed = {}

        async def encoder(*args, **kwargs):
            observed["deadline_at"] = args[2]
            observed["purpose"] = kwargs["purpose"]
            raise TimeoutError()

        with patch.dict(os.environ, {"SEMANTIC_MODE": "aggressive"}), \
                patch.object(agent_app, "call_model", encoder), patch.object(agent_app, "send_article", transmit):
            await agent_app.sender_job("bounded-encoder", source, 512, 120000, accepted_at)
        encoding_deadline = observed["deadline_at"]
        self.assertEqual(observed["purpose"], "sender")
        self.assertGreater(encoding_deadline, accepted_at)
        self.assertLess(encoding_deadline, accepted_at + 120)
        transmit.assert_awaited_once_with("bounded-encoder", source, 512, 120000, accepted_at)

    async def test_default_sender_transmits_losslessly_without_waiting_for_model(self):
        source = ("Approved in 2025 with conditions. " * 80).encode()
        accepted_at = time.monotonic()
        transmit = AsyncMock()
        encoder = AsyncMock()
        with patch.dict(os.environ), patch.object(agent_app, "call_model", encoder), \
                patch.object(agent_app, "send_article", transmit):
            os.environ.pop("SEMANTIC_MODE", None)
            await agent_app.sender_job("default-lossless", source, 512, 120000, accepted_at)
        encoder.assert_not_awaited()
        transmit.assert_awaited_once_with("default-lossless", source, 512, 120000, accepted_at)

    async def test_guide_arriving_first_is_not_source_or_acknowledgement(self):
        from semantic_guide import encode_guide

        source = "Agency received 12 units in 2025."
        guide = {"version": 1, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                 "layout": "ledger", "entities": [], "anchors": [source]}
        states = {}
        protocol = agent_app.ReceiverProtocol(states)
        protocol.datagram_received(agent_app.encode_frame(agent_app.GUIDE_FRAME, "guide-first", 0, 1,
            encode_guide(guide)), ("127.0.0.1", 9000))
        state = states["guide-first"]
        self.assertEqual(state.guide["source_sha256"], guide["source_sha256"])
        self.assertEqual(state.chunks, {})
        self.assertIsNone(state.expected_bytes)
        self.assertIsNone(state.model_task)
        self.assertEqual(state.status, "pending")
        stale_guide = dict(guide, anchors=[])
        protocol.datagram_received(agent_app.encode_frame(agent_app.GUIDE_FRAME, "guide-first", 0, 1,
            encode_guide(stale_guide)), ("127.0.0.1", 9000))
        self.assertEqual(state.guide["anchors"], [source])

    async def test_stalled_full_source_upgrade_keeps_completed_partial_and_flush_reserve(self):
        raw = b"The entire received source text."
        state = agent_app.ReceiverTask("bounded-late-complete", 1, expected_bytes=len(raw),
            chunk_size=512, digest=hashlib.sha256(raw).digest(), chunks={0: raw},
            status="processing", deadline_at=time.monotonic() + 8.08)
        events = []
        deadlines = []

        async def generate(text, partial, deadline_at, **kwargs):
            if partial:
                return "Completed partial-source model record"
            deadlines.append(deadline_at)
            try:
                await asyncio.Event().wait()
            finally:
                events.append("upgrade_cancelled")

        with patch.object(agent_app, "call_model", AsyncMock(side_effect=generate)), \
                patch.object(agent_app.telemetry, "flush", side_effect=lambda: events.append("flushed")):
            await agent_app.ReceiverProtocol({})._generate(state, b"Received fragment", True)
        self.assertEqual(state.status, "success")
        self.assertEqual(state.content, "Completed partial-source model record")
        self.assertTrue(state.partial_result)
        self.assertLess(deadlines[0], state.deadline_at)
        self.assertGreater(state.deadline_at - time.monotonic(), 7.8)
        self.assertEqual(events, ["upgrade_cancelled", "flushed"])

    async def test_completed_partial_skips_upgrade_when_only_flush_reserve_remains(self):
        raw = b"The entire received source text."
        state = agent_app.ReceiverTask("late-complete-without-budget", 1, expected_bytes=len(raw),
            chunk_size=512, digest=hashlib.sha256(raw).digest(), chunks={0: raw},
            status="processing", deadline_at=time.monotonic() + 7)
        model = AsyncMock(return_value="Completed partial-source model record")
        with patch.object(agent_app, "call_model", model), \
                patch.object(agent_app.telemetry, "flush") as flush:
            await agent_app.ReceiverProtocol({})._generate(state, b"Received fragment", True)
        self.assertEqual(state.status, "success")
        self.assertEqual(state.content, "Completed partial-source model record")
        self.assertTrue(state.partial_result)
        model.assert_awaited_once()
        flush.assert_called_once()

    async def test_completed_packet_checkpoint_survives_later_orchestration_failure(self):
        state = agent_app.ReceiverTask("checkpoint-survival", 1, status="processing",
                                       deadline_at=time.monotonic() + 20)

        async def model(*args, **kwargs):
            kwargs["on_checkpoint"]("Alpha:12 units in 2025")
            raise TimeoutError("another model path stalled")

        with patch.object(agent_app, "call_model", model), patch.object(agent_app.telemetry, "flush") as flush:
            await agent_app.ReceiverProtocol({})._generate(state, b"Alpha received 12 units in 2025", False)
        self.assertEqual(state.status, "success")
        self.assertEqual(state.content, "Alpha:12 units in 2025")
        flush.assert_called_once()

    async def test_full_source_upgrade_checkpoint_survives_when_partial_generation_failed(self):
        raw = b"Alpha received 12 units in 2025. Beta received 20 units."
        state = agent_app.ReceiverTask("upgrade-checkpoint-survival", 1, expected_bytes=len(raw),
            chunk_size=512, digest=hashlib.sha256(raw).digest(), chunks={0: raw},
            status="processing", deadline_at=time.monotonic() + 20)

        async def model(text, partial, deadline_at, **kwargs):
            if partial:
                raise RuntimeError("partial source generation failed")
            self.assertEqual(text, raw.decode())
            kwargs["on_checkpoint"]("Alpha:12 units in 2025")
            raise TimeoutError("later full-source path stalled")

        with patch.object(agent_app, "call_model", model), patch.object(agent_app.telemetry, "flush") as flush:
            await agent_app.ReceiverProtocol({})._generate(state, b"Alpha received", True)
        self.assertEqual(state.status, "success")
        self.assertEqual(state.content, "Alpha:12 units in 2025")
        self.assertFalse(state.partial_result)
        flush.assert_called_once()

    async def test_failed_upgrade_checkpoint_does_not_replace_existing_partial_result(self):
        raw = b"Alpha received 12 units in 2025. Beta received 20 units."
        state = agent_app.ReceiverTask("upgrade-checkpoint-keeps-prior", 1, expected_bytes=len(raw),
            chunk_size=512, digest=hashlib.sha256(raw).digest(), chunks={0: raw},
            status="processing", deadline_at=time.monotonic() + 20)
        partial_record = "Alpha:12 units in 2025; Beta:20 units"

        async def model(text, partial, deadline_at, **kwargs):
            if partial:
                return partial_record
            kwargs["on_checkpoint"]("Alpha:12 units in 2025")
            raise TimeoutError("later full-source path stalled")

        with patch.object(agent_app, "call_model", model), patch.object(agent_app.telemetry, "flush") as flush:
            await agent_app.ReceiverProtocol({})._generate(state, b"Received fragment", True)
        self.assertEqual(state.status, "success")
        self.assertEqual(state.content, partial_record)
        self.assertTrue(state.partial_result)
        flush.assert_called_once()

if __name__ == "__main__":
    unittest.main()
