"""Per-task provider execution: routing, MCP preservation and real child lifecycle."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from agent_server.services import provider_task_runtime as provider
from agent_server.services import runtime_adapter
from services.model_catalog import validate_dispatch_model, InvalidModelError

pytestmark = pytest.mark.unit


def test_provider_model_shape():
    for name in ("xai/grok-4.7", "deepseek/deepseek-flash", "google/gemini-3.8-flash",
                 "openai/gpt-6.1-sol", "openrouter/moonshotai/kimi-k3"):
        assert validate_dispatch_model(name) == name
        assert provider.is_provider_task_model(name)
    for name in ("xai/", "/grok", "xai/grok --help", "xai/../secret", "-xai/grok",
                 "xai/grok\nother", "xai/grok;echo"):
        assert not provider.is_provider_task_model(name)
        with pytest.raises(InvalidModelError):
            validate_dispatch_model(name)
    assert not provider.is_provider_task_model("claude-opus-5-5")


def test_task_routing_does_not_change_native_runtime(monkeypatch):
    native = object()
    monkeypatch.setattr(runtime_adapter, "get_runtime", lambda: native)
    assert runtime_adapter.get_task_runtime(None) is native
    assert runtime_adapter.get_task_runtime("sonnet") is native
    assert isinstance(runtime_adapter.get_task_runtime("xai/grok-4.7"), provider.OpenCodeTaskRuntime)
    assert runtime_adapter.get_runtime() is native


def test_mcp_conversion_keeps_both_transports_and_does_not_mutate():
    original = {
        "trinity": {"type": "http", "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer synthetic"}},
        "playwright": {"command": "npx", "args": ["@playwright/mcp@0.0.84"],
                       "env": {"DISPLAY": ":99"}, "cwd": "/home/developer"},
        "off": {"command": "false", "disabled": True},
    }
    before = json.dumps(original, sort_keys=True)
    actual = provider.translate_mcp(original)
    assert actual["trinity"]["headers"] == original["trinity"]["headers"]
    assert actual["trinity"]["type"] == "remote"
    assert actual["trinity"]["oauth"] is False
    assert actual["playwright"]["command"] == ["npx", "@playwright/mcp@0.0.84"]
    assert actual["playwright"]["environment"] == {"DISPLAY": ":99"}
    assert actual["off"]["enabled"] is False
    assert json.dumps(original, sort_keys=True) == before
    with pytest.raises(ValueError, match="unsupported"):
        provider.translate_mcp({"bad": {"type": "unknown"}})


@pytest.fixture
def runtime_home(tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "AGENT_HOME", tmp_path)
    plugin = tmp_path / "guardrails.mjs"
    plugin.write_text("export default async () => ({})")
    monkeypatch.setattr(provider, "GUARDRAIL_PLUGIN", plugin)
    (tmp_path / "CLAUDE.md").write_text("You are the existing agent. Keep your own judgment.")
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"trinity": {"type": "http", "url": "https://example.com/mcp"}}
    }))
    monkeypatch.setattr(provider, "build_execution_env",
                        lambda extra=None: {**os.environ, **(extra or {})})
    return tmp_path


def test_config_identity_platform_prompt_and_tool_scope(runtime_home):
    config = provider.build_task_config(
        "Use mcp__trinity__list_agents and mcp__trinity__report",
        ["Read", "mcp__trinity__report"], 7,
        {"disallowed_tools": ["Bash"]}, False,
    )
    agent = config["agent"]["trinity-task"]
    assert "Keep your own judgment" in agent["prompt"]
    assert "trinity_list_agents" in agent["prompt"]
    assert "mcp__trinity__" not in agent["prompt"]
    assert agent["steps"] == 7
    assert agent["permission"]["*"] == "deny"
    assert agent["permission"]["read"] == "allow"
    assert agent["permission"]["trinity_report"] == "allow"
    assert agent["permission"]["bash"] == "deny"
    assert config["share"] == "disabled"
    assert config["plugin"] == [provider.GUARDRAIL_PLUGIN.as_uri()]


def test_read_only_takes_precedence_over_allow_list(runtime_home):
    cfg = provider.build_task_config(None, ["Bash", "Write"], 4, {}, True)
    permissions = cfg["agent"]["trinity-task"]["permission"]
    assert permissions["bash"] == "deny"
    assert permissions["edit"] == "deny"
    with pytest.raises(ValueError, match="tool"):
        provider.build_task_config(None, ["UnknownTool"], 4, {}, False)


def test_missing_plugin_and_malformed_mcp_are_errors(runtime_home, monkeypatch):
    provider.GUARDRAIL_PLUGIN.unlink()
    with pytest.raises(ValueError, match="plugin"):
        provider.build_task_config(None, None, 4, {}, False)
    provider.GUARDRAIL_PLUGIN.write_text("")
    (runtime_home / ".mcp.json").write_text("{broken")
    with pytest.raises(ValueError, match="MCP"):
        provider.build_task_config(None, None, 4, {}, False)


@pytest.fixture
def fake_cli(runtime_home, monkeypatch):
    script = runtime_home / "fake-opencode"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json,os,sys,time\n"
        "from pathlib import Path\n"
        "Path('received.json').write_text(json.dumps({'argv':sys.argv[1:],"
        "'prompt':sys.stdin.read(),'config':json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])}))\n"
        "if os.environ.get('FAKE_SLEEP'): time.sleep(10)\n"
        "if os.environ.get('FAKE_ERROR'):\n"
        " print(json.dumps({'type':'error','error':{'name':'APIError','data':"
        "{'statusCode':429,'message':'synthetic rate limit'}}}),flush=True)\n"
        " sys.exit(0)\n"
        "for event in [\n"
        " {'type':'step_start','sessionID':'ses_synthetic'},\n"
        " {'type':'tool_use','sessionID':'ses_synthetic','part':{'tool':'trinity_report',"
        "'state':{'status':'completed','input':{'title':'result'},'output':'recorded'}}},\n"
        " {'type':'text','sessionID':'ses_synthetic','part':{'text':'Task completed'}},\n"
        " {'type':'step_finish','sessionID':'ses_synthetic','part':{'reason':'stop',"
        "'cost':0.002,'tokens':{'input':20,'output':8,'reasoning':3,'cache':{'read':10,'write':0}}}}"
        "]: print(json.dumps(event),flush=True)\n"
    )
    script.chmod(0o700)
    monkeypatch.setattr(provider.shutil, "which", lambda _: str(script))
    monkeypatch.setattr(provider, "_load_guardrails", lambda: {})
    monkeypatch.setattr(provider, "read_only_enabled", lambda: False)
    return script


@pytest.mark.asyncio
async def test_real_child_executes_qualified_model_and_returns_evidence(fake_cli, runtime_home):
    response, logs, metadata, session = await provider.OpenCodeTaskRuntime().execute_headless(
        "Carry this task through", model="xai/grok-4.7", max_turns=6,
        system_prompt="Use mcp__trinity__report", execution_id="provider-task-success",
    )
    received = json.loads((runtime_home / "received.json").read_text())
    assert received["argv"][received["argv"].index("--model") + 1] == "xai/grok-4.7"
    assert received["prompt"] == "Carry this task through"
    assert received["config"]["mcp"]["trinity"]["url"] == "https://example.com/mcp"
    assert received["config"]["model"] == received["config"]["small_model"] == "xai/grok-4.7"
    assert response == "Task completed"
    assert session == "ses_synthetic"
    assert metadata.model_name == "xai/grok-4.7"
    assert metadata.tool_count == 1
    assert metadata.output_tokens == 8  # reasoning is not added twice
    assert metadata.input_tokens == 30
    assert metadata.cost_usd == 0.002
    assert any(row["type"] == "tool_use" for row in logs)
    assert not provider.get_process_registry().list_running()


@pytest.mark.asyncio
async def test_provider_error_even_when_cli_exits_zero(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_ERROR", "1")
    with pytest.raises(HTTPException) as caught:
        await provider.OpenCodeTaskRuntime().execute_headless("x", model="xai/grok-4.7")
    assert caught.value.status_code == 429
    assert caught.value.detail["error_code"] == "RATE_LIMIT"


@pytest.mark.asyncio
async def test_timeout_kills_child_and_unregisters(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "1")
    with pytest.raises(HTTPException) as caught:
        await provider.OpenCodeTaskRuntime().execute_headless(
            "x", model="deepseek/deepseek-flash", timeout_seconds=0.2,
            execution_id="provider-task-timeout",
        )
    assert caught.value.status_code == 504
    assert not provider.get_process_registry().list_running()


@pytest.mark.asyncio
async def test_cancellation_reaps_child(fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "1")
    task = asyncio.create_task(provider.OpenCodeTaskRuntime().execute_headless(
        "x", model="xai/grok-4.7", execution_id="provider-task-cancel",
    ))
    for _ in range(100):
        if provider.get_process_registry().list_running():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not provider.get_process_registry().list_running()


@pytest.mark.asyncio
async def test_missing_cli_and_native_resume_fail_explicitly(runtime_home, monkeypatch):
    monkeypatch.setattr(provider.shutil, "which", lambda _: None)
    with pytest.raises(HTTPException) as caught:
        await provider.OpenCodeTaskRuntime().execute_headless("x", model="xai/grok-4.7")
    assert caught.value.status_code == 503
    with pytest.raises(HTTPException) as caught:
        await provider.OpenCodeTaskRuntime().execute_headless(
            "x", model="xai/grok-4.7", resume_session_id="native-session-uuid",
        )
    assert caught.value.status_code == 400


def test_failed_task_preserves_tool_evidence_and_scrubs_secrets():
    secret = "sk-" + "syntheticsecret" * 4
    events = [
        {"type": "tool_use", "part": {"tool": "bash", "state": {"output": secret}}},
        {"type": "error", "error": {"data": {"statusCode": 429, "message": "Quota " + secret}}},
    ]
    with pytest.raises(HTTPException) as caught:
        provider.parse_events("\n".join(json.dumps(e) for e in events), "", 0, "xai/grok-4.7", 10, "fixture")
    assert caught.value.status_code == 429
    assert caught.value.detail["metadata"]["tool_count"] == 1
    assert len(caught.value.detail["execution_log"]) == 2
    assert secret not in json.dumps(caught.value.detail)


def test_empty_or_malformed_output_is_never_success():
    for stdout in ("CLI startup message", '{"type":"text","part":[]}'):
        with pytest.raises(HTTPException) as caught:
            provider.parse_events(stdout, "", 0, "xai/grok-4.7", 10, "fixture")
        assert caught.value.status_code == 502
