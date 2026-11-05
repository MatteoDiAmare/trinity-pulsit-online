"""Run a stateless task on an explicit provider/model in the existing workspace.

The native chat runtime stays intact. OpenCode supplies the provider adapters
and actual tool loop; Trinity supplies identity, MCP, limits and process lifecycle.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from ..models import ExecutionMetadata
from ..provider_models import is_provider_task_model
from ..utils.credential_sanitizer import sanitize_dict, sanitize_text
from ._runtime_config import _load_guardrails, _DEFAULT_MAX_TURNS_TASK
from .execution_env import build_execution_env
from .process_registry import get_process_registry
from .subprocess_lifecycle import _capture_pgid, _terminate_process_group

AGENT_HOME = Path("/home/developer")
GUARDRAIL_PLUGIN = Path("/opt/trinity/hooks/opencode-guardrails.mjs")

# OpenCode's edit permission covers all file-mutating built-ins.
_TOOLS = {
    "Bash": "bash", "Read": "read", "Write": "edit", "Edit": "edit",
    "MultiEdit": "edit", "NotebookEdit": "edit", "Glob": "glob",
    "Grep": "grep", "WebFetch": "webfetch", "WebSearch": "websearch",
    "Task": "task", "Agent": "task", "Skill": "skill", "TodoWrite": "todowrite",
}


def _tool_name(name: str) -> str:
    if name in _TOOLS:
        return _TOOLS[name]
    if name.startswith("mcp__"):
        return name[5:].replace("__", "_")
    if re.fullmatch(r"[a-z][a-z0-9_*-]*", name):
        return name
    raise ValueError(f"Unsupported tool restriction: {name!r}")


def translate_mcp(servers: dict) -> dict:
    """Convert both MCP transports without rewriting the private source file."""
    result = {}
    for name, item in servers.items():
        if not isinstance(item, dict):
            raise ValueError(f"MCP server {name!r} must be an object")
        enabled = not item.get("disabled", False)
        if item.get("url") and item.get("type", "http") in ("http", "sse"):
            result[name] = {
                "type": "remote", "url": item["url"], "enabled": enabled,
                "headers": copy.deepcopy(item.get("headers", {})), "oauth": False,
            }
        elif isinstance(item.get("command"), str):
            result[name] = {
                "type": "local",
                "command": [item["command"], *item.get("args", [])],
                "environment": copy.deepcopy(item.get("env", {})), "enabled": enabled,
            }
            if item.get("cwd"):
                result[name]["cwd"] = item["cwd"]
        else:
            raise ValueError(f"MCP server {name!r} has an unsupported transport")
    return result


def read_only_enabled() -> bool:
    path = AGENT_HOME / ".trinity/read-only-config.json"
    if not path.exists():
        return False
    try:
        config = json.loads(path.read_text())
        if not isinstance(config, dict):
            raise ValueError("expected object")
        return bool(config.get("enabled"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read read-only configuration") from exc


def build_task_config(system_prompt, allowed_tools, max_turns, guardrails, read_only,
                      base_config=None):
    if not GUARDRAIL_PLUGIN.is_file():
        raise ValueError("OpenCode guardrail plugin is missing; rebuild the agent image")
    config = copy.deepcopy(base_config or {})
    if not isinstance(config.get("agent", {}), dict) or not isinstance(config.get("plugin", []), list):
        raise ValueError("OpenCode agent and plugin configuration have invalid types")
    identity = next((p for p in (AGENT_HOME / "CLAUDE.md", AGENT_HOME / "AGENTS.md")
                     if p.is_file()), None)
    if identity is None:
        raise ValueError("Agent identity file CLAUDE.md or AGENTS.md is missing")
    platform = (system_prompt or "").replace("mcp__trinity__", "trinity_")
    prompt = identity.read_text() + "\n\n" + platform
    permissions = {"*": "allow" if allowed_tools is None else "deny"}
    for tool in allowed_tools or []:
        permissions[_tool_name(tool)] = "allow"
    for tool in guardrails.get("disallowed_tools", []):
        permissions[_tool_name(tool)] = "deny"
    if read_only:
        permissions.update({"bash": "deny", "edit": "deny"})
    # A task has no interactive terminal to answer this tool.
    permissions["question"] = "deny"
    agent = {
        "description": "Execute this existing Trinity agent's assigned task",
        "mode": "primary", "prompt": prompt, "steps": max_turns,
        "permission": permissions,
    }
    config.setdefault("agent", {})["trinity-task"] = agent
    config["permission"] = permissions
    config["share"] = "disabled"
    config["autoupdate"] = False
    # The highest-priority inline config includes the root-owned bridge.
    plugins = config.setdefault("plugin", [])
    if GUARDRAIL_PLUGIN.as_uri() not in plugins:
        plugins.append(GUARDRAIL_PLUGIN.as_uri())
    mcp_path = AGENT_HOME / ".mcp.json"
    if mcp_path.exists():
        try:
            mcp = json.loads(mcp_path.read_text())
            if not isinstance(mcp.get("mcpServers"), dict):
                raise ValueError("expected mcpServers object")
            config.setdefault("mcp", {}).update(translate_mcp(mcp["mcpServers"]))
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Cannot load existing MCP configuration") from exc
    return config


def _failure(status, code, message, metadata, execution_log=None):
    metadata.status = "error"
    metadata.error_code = code
    metadata.error_message = sanitize_text(message)[:2000]
    return HTTPException(status_code=status, detail={
        "error": metadata.error_message, "message": metadata.error_message, "error_code": code,
        "metadata": sanitize_dict(metadata.model_dump()),
        "execution_log": sanitize_dict({"events": execution_log or []})["events"],
    })


def parse_events(stdout, stderr, returncode, model, elapsed_ms, execution_id):
    """Interpret JSON events; exit zero alone is not proof of task completion."""
    metadata = ExecutionMetadata(model_name=model, execution_id=execution_id,
                                 duration_ms=elapsed_ms, num_turns=0)
    messages, texts, errors = [], [], []
    saw_cost = False
    error_status, error_code = 502, "AGENT_ERROR"
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # CLI startup messages are not assistant replies.
        if not isinstance(event, dict):
            continue
        event = sanitize_dict(event)
        messages.append(event)
        metadata.session_id = event.get("sessionID") or metadata.session_id
        part = event.get("part") or {}
        if not isinstance(part, dict):
            errors.append("OpenCode returned a malformed event")
            continue
        if event.get("type") == "text":
            texts.append(part.get("text", ""))
        elif event.get("type") == "tool_use":
            metadata.tool_count += 1
        elif event.get("type") == "step_finish":
            metadata.num_turns += 1
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            metadata.input_tokens = max(metadata.input_tokens,
                                        tokens.get("input", 0) + cache.get("read", 0))
            metadata.output_tokens += tokens.get("output", 0)
            metadata.cache_read_tokens += cache.get("read", 0)
            metadata.cache_creation_tokens += cache.get("write", 0)
            if part.get("cost") is not None:
                saw_cost = True
                metadata.cost_usd = (metadata.cost_usd or 0) + part["cost"]
            if part.get("reason") in ("length", "max-steps", "max_steps"):
                errors.append("Model step or output limit reached")
        elif event.get("type") == "error":
            error = event.get("error") or {}
            data = error.get("data") or {}
            message = data.get("message") or error.get("message") or error.get("name") or "Provider error"
            status = data.get("statusCode")
            if status in (401, 403):
                error_status, error_code = 401, "AUTH"
            elif status == 429:
                error_status, error_code = 429, "RATE_LIMIT"
            errors.append(message)
    if not saw_cost:
        metadata.cost_usd = None
    if errors or returncode:
        raise _failure(error_status, error_code, "; ".join(errors) or stderr or
                       f"OpenCode exited with status {returncode}", metadata, messages)
    answer = "".join(texts).strip()
    if not answer:
        raise _failure(502, "AGENT_ERROR", stderr or "OpenCode returned no final text", metadata, messages)
    metadata.status = "success"
    return sanitize_text(answer), messages, metadata, metadata.session_id or ""


class OpenCodeTaskRuntime:
    """Task-only engine; no mutations to native chat/session state."""

    async def execute_headless(self, prompt, model=None, allowed_tools=None,
                               system_prompt=None, timeout_seconds=900, max_turns=None,
                               execution_id=None, resume_session_id=None,
                               persist_session=False, images=None):
        if resume_session_id:
            raise HTTPException(400, "Provider tasks are stateless; native sessions cannot be resumed")
        if images:
            raise HTTPException(400, "Provider task image attachments are not supported")
        if not is_provider_task_model(model):
            raise HTTPException(400, "Provider tasks require an explicit provider/model")
        binary = shutil.which("opencode")
        if not binary:
            raise _failure(503, "RUNTIME_UNAVAILABLE",
                           "OpenCode CLI is not installed; rebuild the agent image",
                           ExecutionMetadata(model_name=model))
        guardrails = _load_guardrails()
        turns = max_turns if max_turns is not None else (
            guardrails.get("max_turns_task") or _DEFAULT_MAX_TURNS_TASK)
        if not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 500:
            raise HTTPException(400, "max_turns must be an integer from 1 to 500")
        limit = guardrails.get("max_turns_task")
        if limit:
            turns = min(turns, int(limit))
        timeout = min(float(timeout_seconds),
                      float(guardrails.get("execution_timeout_sec") or timeout_seconds))
        if timeout <= 0:
            raise HTTPException(400, "timeout_seconds must be positive")
        task_id = execution_id or str(uuid.uuid4())
        try:
            env = build_execution_env({"TRINITY_EXECUTION_ID": task_id})
            base = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
            if not isinstance(base, dict):
                raise ValueError("OpenCode config must be an object")
            cfg = build_task_config(system_prompt, allowed_tools, turns, guardrails,
                                    read_only_enabled(), base)
            cfg["model"] = model
            cfg.setdefault("small_model", model)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(400, sanitize_text(str(exc))) from exc
        env.update({
            "OPENCODE_CONFIG_CONTENT": json.dumps(cfg),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_TERMINAL_TITLE": "true",
        })
        if env.get("GOOGLE_API_KEY") and not env.get("GOOGLE_GENERATIVE_AI_API_KEY"):
            env["GOOGLE_GENERATIVE_AI_API_KEY"] = env["GOOGLE_API_KEY"]
        cmd = [binary, "run", "--format", "json", "--model", model,
               "--agent", "trinity-task", "--dir", str(AGENT_HOME)]
        cancelled = threading.Event()
        handle = {}
        worker = asyncio.create_task(asyncio.to_thread(
            self._run, cmd, env, prompt, timeout, task_id, model, cancelled, handle))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            process = handle.get("process")
            if process is not None:
                await asyncio.to_thread(_terminate_process_group, process, 0,
                                        pgid=handle.get("pgid"), execution_tag=task_id)
            try:
                await asyncio.shield(worker)
            except Exception:
                pass  # The caller's cancellation remains authoritative.
            raise

    @staticmethod
    def _run(cmd, env, prompt, timeout, task_id, model, cancelled, handle):
        if cancelled.is_set():
            raise HTTPException(499, "Task cancelled before spawn")
        registry = get_process_registry()
        start = time.monotonic()
        process = None
        pgid = None
        try:
            process = subprocess.Popen(
                cmd, cwd=AGENT_HOME, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True,
            )
            pgid = _capture_pgid(process)
            handle.update(process=process, pgid=pgid)
            registry.register(task_id, process, {"type": "provider-task", "model": model, "pgid": pgid})
            if cancelled.is_set():
                _terminate_process_group(process, 0, pgid=pgid, execution_tag=task_id)
            try:
                stdout, stderr = process.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process, 0, pgid=pgid, execution_tag=task_id)
                stdout, stderr = process.communicate(timeout=5)
                elapsed_ms = int((time.monotonic() - start) * 1000)
                try:
                    _, logs, md, _ = parse_events(stdout, stderr, process.returncode, model, elapsed_ms, task_id)
                except HTTPException as exc:
                    md = ExecutionMetadata(**exc.detail["metadata"])
                    logs = exc.detail["execution_log"]
                raise _failure(504, "TIMEOUT", "Provider task exceeded its execution timeout", md, logs)
            return parse_events(stdout, sanitize_text(stderr), process.returncode, model,
                                int((time.monotonic() - start) * 1000), task_id)
        except OSError as exc:
            raise _failure(503, "RUNTIME_UNAVAILABLE", str(exc),
                           ExecutionMetadata(model_name=model, execution_id=task_id)) from exc
        finally:
            if process is not None:
                _terminate_process_group(process, 0, pgid=pgid, execution_tag=task_id)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            registry.unregister(task_id)
