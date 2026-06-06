"""Unit tests for master.sync.agent_worker."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from master.sync.agent_worker import AgentTaskWorker, make_orchestrator_dispatch


def _envelope(raw_input="hello", *, with_payload=True, device="dev-1"):
    inner = json.dumps({"raw_input": raw_input, "intent": "chat"}).encode()
    env = {
        "action_id": "act-1",
        "action_type": "agent.execute",
        "device_id": device,
        "queued_at": 123,
        "payload": inner.hex() if with_payload else None,
    }
    return json.dumps(env).encode()


class TestParse:
    def test_decodes_envelope_and_inner(self):
        req = AgentTaskWorker._parse(_envelope("do the thing"))
        assert req["task_id"] == "act-1"
        assert req["device_id"] == "dev-1"
        assert req["action_type"] == "agent.execute"
        assert req["raw_input"] == "do the thing"
        assert req["intent"] == "chat"

    def test_missing_payload_yields_empty_input(self):
        req = AgentTaskWorker._parse(_envelope(with_payload=False))
        assert req["raw_input"] == ""
        assert req["device_id"] == "dev-1"

    def test_garbage_payload_is_tolerated(self):
        env = json.dumps({"action_id": "a", "device_id": "d", "payload": "zzzz"}).encode()
        req = AgentTaskWorker._parse(env)
        assert req["raw_input"] == ""  # bad hex -> empty inner, no raise


class TestHandle:
    async def test_dispatches_publishes_and_acks(self):
        js = AsyncMock()
        dispatch = AsyncMock(
            return_value={"task_id": "act-1", "agent_id": "personal-agent", "final_output": "hi"}
        )
        worker = AgentTaskWorker(js, dispatch)

        msg = AsyncMock()
        msg.data = _envelope("hello", device="dev-9")
        await worker._handle(msg)

        # dispatched with the parsed request
        dispatch.assert_awaited_once()
        sent = dispatch.await_args.args[0]
        assert sent["raw_input"] == "hello"
        assert sent["device_id"] == "dev-9"

        # result published to the per-device result subject
        js.publish.assert_awaited_once()
        subject, body = js.publish.await_args.args
        assert subject == "agent.result.dev-9"
        assert json.loads(body)["final_output"] == "hi"

        msg.ack.assert_awaited_once()
        msg.nak.assert_not_awaited()

    async def test_buffers_result_to_redis_when_configured(self):
        js = AsyncMock()
        redis = AsyncMock()
        dispatch = AsyncMock(
            return_value={"task_id": "act-1", "agent_id": "a", "final_output": "hi"}
        )
        worker = AgentTaskWorker(js, dispatch, redis=redis)

        msg = AsyncMock()
        msg.data = _envelope("hello", device="dev-7")
        await worker._handle(msg)

        redis.lpush.assert_awaited_once()
        key, body = redis.lpush.await_args.args
        assert key == "lucifer:agent_results:dev-7"
        assert json.loads(body)["final_output"] == "hi"
        redis.ltrim.assert_awaited_once()
        msg.ack.assert_awaited_once()

    async def test_nak_on_dispatch_error(self):
        js = AsyncMock()
        dispatch = AsyncMock(side_effect=RuntimeError("boom"))
        worker = AgentTaskWorker(js, dispatch)

        msg = AsyncMock()
        msg.data = _envelope()
        await worker._handle(msg)

        msg.nak.assert_awaited_once()
        msg.ack.assert_not_awaited()
        js.publish.assert_not_awaited()


class TestOrchestratorDispatch:
    async def test_builds_state_and_extracts_output(self):
        graph = AsyncMock()
        graph.ainvoke = AsyncMock(
            return_value={"agent_id": "coding-agent", "final_output": "answer"}
        )
        dispatch = make_orchestrator_dispatch(graph)

        result = await dispatch({"task_id": "t-1", "device_id": "dev-2", "raw_input": "write code"})

        graph.ainvoke.assert_awaited_once()
        state, kwargs = graph.ainvoke.await_args.args[0], graph.ainvoke.await_args.kwargs
        assert state["raw_input"] == "write code"
        assert state["device_id"] == "dev-2"
        assert state["task_id"] == "t-1"
        assert kwargs["config"]["configurable"]["thread_id"] == "t-1"

        assert result == {
            "task_id": "t-1",
            "agent_id": "coding-agent",
            "final_output": "answer",
        }

    async def test_defaults_when_fields_absent(self):
        graph = AsyncMock()
        graph.ainvoke = AsyncMock(return_value={})
        dispatch = make_orchestrator_dispatch(graph)

        result = await dispatch({"raw_input": "hi"})
        assert result["agent_id"] == "personal-agent"
        assert result["final_output"] == ""
        assert result["task_id"]  # a uuid was generated
