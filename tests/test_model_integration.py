import asyncio
import hashlib
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
    async def test_truncated_gateway_output_never_becomes_success(self):
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
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="length", message=SimpleNamespace(content="Truncated text"))])

        with patch.dict(os.environ, {"PLATFORM_LLM_URL": "http://unused/v1", "PLATFORM_API_KEY": "test"}), \
                patch.object(agent_app, "AsyncOpenAI", FakeClient):
            with self.assertRaisesRegex(RuntimeError, "no_complete_model_coverage"):
                await agent_app.call_model("Received source text", False, time.monotonic() + 20)

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

        async def model(*args):
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
                content = ('{"patches":[]}' if "Audit the factual" in kwargs["messages"][0]["content"]
                           else "Model-generated record following retry.")
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=content))])

        with patch.dict(os.environ, {"PLATFORM_LLM_URL": "http://unused/v1", "PLATFORM_API_KEY": "test"}), \
                patch.object(agent_app, "AsyncOpenAI", FakeClient):
            result = await agent_app.call_model("Received source", False, time.monotonic() + 20)
        self.assertIn("following retry", result)
        self.assertEqual(FakeClient.calls, 3)


if __name__ == "__main__":
    unittest.main()
