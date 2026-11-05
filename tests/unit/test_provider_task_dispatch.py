"""Provider selection survives every task transport without rotating Claude auth."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from agent_server.routers import chat
from agent_server.services import pull_worker, result_callback, runtime_adapter
from services import subscription_auto_switch, task_execution_service as tasks
from services import pull_coordination_service as pull_coordination
from services import chat_execution_service as chat_execution

pytestmark = pytest.mark.unit
MODEL = "xai/grok-4.7"


def test_shared_model_validator_is_identical_in_both_images():
    repo = Path(__file__).resolve().parents[2]
    assert (repo / "src/backend/provider_models.py").read_bytes() == (
        repo / "docker/base-image/agent_server/provider_models.py").read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sync", "callback", "pull"])
async def test_all_transports_select_provider_and_preserve_task(monkeypatch, transport):
    metadata = SimpleNamespace(model_dump=lambda: {"model_name": MODEL, "cost_usd": 0.01})
    runtime = SimpleNamespace(execute_headless=AsyncMock(return_value=("done", [], metadata, "ses_fixture")))
    choose = MagicMock(return_value=runtime)
    native = MagicMock(side_effect=AssertionError("must not select native runtime"))
    monkeypatch.setattr(runtime_adapter, "get_task_runtime", choose)
    monkeypatch.setattr(runtime_adapter, "get_runtime", native)
    registry = MagicMock()
    registry.was_terminated.return_value = False
    for module in (chat, result_callback, pull_worker):
        monkeypatch.setattr(module, "agent_state", MagicMock())
    for module in (chat, result_callback):
        monkeypatch.setattr(module, "get_process_registry", lambda: registry)
    request = SimpleNamespace(
        message="Complete the assigned task", model=MODEL, execution_id="exec-provider",
        allowed_tools=["Read"], system_prompt="Existing platform prompt", timeout_seconds=30,
        max_turns=4, resume_session_id=None, persist_session=False, images=None,
    )
    if transport == "sync":
        monkeypatch.setattr(chat, "get_task_runtime", choose)
        monkeypatch.setattr(chat, "get_runtime", native)
        monkeypatch.setattr(chat.result_callback, "try_spawn_async", lambda _: False)
        result = await chat.execute_task(request)
        assert result["response"] == "done"
    elif transport == "callback":
        deliver = AsyncMock(return_value=True)
        monkeypatch.setattr(result_callback, "_deliver", deliver)
        monkeypatch.setattr(result_callback, "_persist", MagicMock())
        monkeypatch.setattr(result_callback, "_delete", MagicMock())
        await result_callback._run_and_report(request, "http://fixture", "synthetic", 0)
        assert deliver.await_args.args[2]["response"] == "done"
    else:
        deliver = AsyncMock(return_value=True)
        monkeypatch.setattr(pull_worker, "_deliver_result", deliver)
        monkeypatch.setattr(pull_worker, "_persist_pull_result", MagicMock())
        monkeypatch.setattr(pull_worker, "_delete_pull_result", MagicMock())
        claim = {"execution_id": request.execution_id, "claim_token": "synthetic", "envelope": {
            "payload": {"message": request.message, "task_overrides": {
                "model": MODEL, "allowed_tools": request.allowed_tools,
                "system_prompt": request.system_prompt, "max_turns": request.max_turns,
                "timeout_seconds": request.timeout_seconds,
            }}}}
        await pull_worker._run_and_report(claim, None, "http://fixture", "synthetic", "fixture-agent")
        assert deliver.await_args.args[2]["content"] == "done"
    choose.assert_called_once_with(MODEL)
    native.assert_not_called()
    kwargs = runtime.execute_headless.await_args.kwargs
    assert kwargs["model"] == MODEL
    assert kwargs["prompt"] == request.message
    assert kwargs["execution_id"] == request.execution_id
    assert kwargs["allowed_tools"] == ["Read"]
    assert kwargs["system_prompt"] == request.system_prompt


@pytest.mark.parametrize("status", [401, 429, 503, 504])
def test_provider_failures_keep_trace_without_claude_auth_classification(status):
    events = [{"type": "tool_use", "part": {"tool": "bash"}}]
    exc = HTTPException(status, {"message": "Provider task failed", "execution_log": events,
                                "metadata": {"model_name": MODEL}})
    callback = result_callback._envelope_from_http_exception(exc)
    pull = pull_worker._failed_result_body_from_http("synthetic", exc)
    expected = "timeout" if status == 504 else "agent_error"
    assert callback["error_code"] == pull["error_code"] == expected
    assert callback["execution_log"] == pull["execution_log"] == events


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_provider_failure_never_switches_native_auth_or_reruns_task(monkeypatch, status):
    request = httpx.Request("POST", "http://fixture/api/task")
    response = httpx.Response(status, request=request, json={"detail": {
        "message": "Provider authentication or quota error", "metadata": {"model_name": MODEL}}})
    post = AsyncMock(return_value=response)
    pre_switch, switch, fallback = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(tasks, "agent_post_with_retry", post)
    monkeypatch.setattr(subscription_auto_switch, "ensure_serviceable_subscription", pre_switch)
    monkeypatch.setattr(subscription_auto_switch, "handle_subscription_failure", switch)
    monkeypatch.setattr(subscription_auto_switch, "fallback_to_api_key", fallback)
    service = tasks.TaskExecutionService()
    state = tasks._AttemptState(start_time=datetime.utcnow())
    with pytest.raises(httpx.HTTPStatusError) as caught:
        returned = await service._call_agent_with_retries(
            agent_name="fixture", execution_id="exec-provider", payload={
                "model": MODEL, "message": "do it", "allowed_tools": None,
            }, timeout_seconds=30, circuit=MagicMock(), state=state)
        returned.raise_for_status()
    assert state.provider_task is True
    service.apply_result = AsyncMock()
    await service._handle_http_error(caught.value, agent_name="fixture", execution_id="exec-provider",
                                    activity_id=None, breaker_enabled=True, state=state)
    terminal = service.apply_result.await_args.args[1]
    assert terminal.error_code is None  # never AUTH/BILLING for a Claude subscription
    pre_switch.assert_not_awaited()
    switch.assert_not_awaited()
    fallback.assert_not_awaited()
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_chat_rejects_provider_override_before_history_mutation(monkeypatch):
    state = MagicMock()
    monkeypatch.setattr(chat, "agent_state", state)
    with pytest.raises(HTTPException) as caught:
        await chat.chat(SimpleNamespace(model=MODEL))
    assert caught.value.status_code == 400
    state.add_message.assert_not_called()


def test_provider_prompt_keeps_canonical_tool_names_even_on_codex_agent(monkeypatch):
    compose = MagicMock(return_value="Use mcp__trinity__list_agents")
    monkeypatch.setattr(tasks, "compose_system_prompt", compose)
    monkeypatch.setattr(tasks, "_resolve_agent_runtime", lambda _: "codex")
    tasks.TaskExecutionService()._compose_effective_system_prompt(
        agent_name="fixture", triggered_by="mcp", source_user_email=None,
        source_agent_name=None, source_mcp_key_name=None, model=MODEL,
        timeout_seconds=30, attempt=None, schedule_context=None,
        execution_id="exec-provider", system_prompt=None,
    )
    assert compose.call_args.kwargs["runtime"] == "claude-code"
    monkeypatch.setattr(pull_coordination, "compose_system_prompt", compose)
    monkeypatch.setattr(pull_coordination, "_resolve_agent_runtime", lambda _: "codex")
    pull_coordination._compose_pull_system_prompt("fixture", "mcp", None, execution_id="exec-provider", model=MODEL)
    assert compose.call_args.kwargs["runtime"] == "claude-code"


@pytest.mark.asyncio
async def test_provider_task_is_not_billed_to_assigned_claude_subscription(monkeypatch):
    db = MagicMock()
    db.create_task_execution.return_value = SimpleNamespace(id="exec-provider")
    monkeypatch.setattr(chat_execution, "db", db)
    monkeypatch.setattr(chat_execution, "_inherited_channel_context", lambda *a, **kw: (None,) * 5)
    monkeypatch.setattr(chat_execution.idempotency_service, "attach_execution", MagicMock())
    _execution_id, subscription_id, *_ = await chat_execution.create_task_execution_and_activities(
        request=SimpleNamespace(model=MODEL, message="do it"), name="fixture",
        current_user=SimpleNamespace(id=1, email="fixture@example.com", username="fixture"),
        x_source_agent=None, triggered_by="manual", is_self_task=False, idem=None,
    )
    assert subscription_id is None
    db.get_agent_subscription_id.assert_not_called()
    assert db.create_task_execution.call_args.kwargs["subscription_id"] is None
