#!/usr/bin/env python3
"""Exercise the real OpenCode task/tool protocol without external API credentials.

Requires the agent-server Python dependencies and opencode-ai@1.19.5 on PATH.
The only model endpoint is a loopback SSE fixture; all agent files are temporary.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "docker/base-image"))
from agent_server.services import provider_task_runtime as runtime  # noqa: E402


def main():
    binary = shutil.which("opencode")
    if not binary:
        raise SystemExit("Install opencode-ai@1.19.5 and put opencode on PATH first")
    requests_seen = []
    mcp_calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/mcp":
                method = body.get("method")
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "fixture", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "record", "description": "Record a fixture result",
                        "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}]}
                elif method == "tools/call":
                    mcp_calls.append(body["params"])
                    result = {"content": [{"type": "text", "text": "Recorded fixture evidence"}]}
                else:
                    result = {}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": body.get("id"), "result": result}).encode())
                return
            requests_seen.append(body)
            results = sum(m.get("role") == "tool" for m in body.get("messages", []))
            tools = {t.get("function", {}).get("name") for t in body.get("tools", [])}
            delta = {"role": "assistant", "content": "Task completed. Marker created."}
            reason = "stop"
            if "bash" in tools and not results:
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": "call_fixture", "type": "function", "function": {
                        "name": "bash", "arguments": json.dumps({
                            "command": "printf provider-task-ok > proof.txt",
                            "description": "Create synthetic protocol-test marker",
                        }),
                    },
                }]}
                reason = "tool_calls"
            elif "trinity_record" in tools and results == 1:
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": "call_mcp", "type": "function", "function": {
                        "name": "trinity_record", "arguments": json.dumps({"value": "provider-task-ok"}),
                    },
                }]}
                reason = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk_delta, finish_reason in ((delta, None), ({}, reason)):
                chunk = {
                    "id": "fixture", "object": "chat.completion.chunk", "created": 1,
                    "model": "test", "choices": [{
                        "index": 0, "delta": chunk_delta, "finish_reason": finish_reason,
                    }],
                }
                if finish_reason:
                    chunk["usage"] = {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="trinity-provider-probe-") as directory:
            home = Path(directory)
            (home / "CLAUDE.md").write_text("You are the synthetic existing agent. Execute the assigned task.")
            (home / ".mcp.json").write_text(json.dumps({"mcpServers": {
                "trinity": {"type": "http", "url": f"http://127.0.0.1:{server.server_port}/mcp"},
            }}))
            config = {
                "enabled_providers": ["fixture"], "small_model": "fixture/test",
                "provider": {"fixture": {
                    "npm": "@ai-sdk/openai-compatible", "name": "Local fixture",
                    "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "synthetic"},
                    "models": {"test": {"name": "test", "limit": {"context": 8192, "output": 1024}}},
                }},
            }
            # Deliberately do not inherit real provider credentials or user config.
            env = {
                "PATH": os.environ["PATH"], "HOME": directory,
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local/share"),
                "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                "OPENCODE_CONFIG_CONTENT": json.dumps(config), "NO_PROXY": "127.0.0.1,localhost",
            }
            with (
                patch.object(runtime, "AGENT_HOME", home),
                patch.object(runtime, "GUARDRAIL_PLUGIN", REPO / "docker/base-image/hooks/opencode-guardrails.mjs"),
                patch.object(runtime, "_load_guardrails", lambda: {}),
                patch.object(runtime, "build_execution_env", lambda extra=None: {**env, **(extra or {})}),
            ):
                answer, events, metadata, _session = asyncio.run(runtime.OpenCodeTaskRuntime().execute_headless(
                    "Create proof.txt using the bash tool, then report the result.",
                    model="fixture/test", timeout_seconds=120, max_turns=4,
                    execution_id="provider-protocol-probe",
                ))
            assert (home / "proof.txt").read_text() == "provider-task-ok"
            assert metadata.tool_count == 2
            assert len(mcp_calls) == 1, mcp_calls
            assert mcp_calls[0]["name"] == "record", mcp_calls
            assert mcp_calls[0]["arguments"] == {"value": "provider-task-ok"}, mcp_calls
            assert requests_seen and all(body["model"] == "test" for body in requests_seen)
            print(json.dumps({
                "status": "passed", "requests": len(requests_seen), "marker_verified": True,
                "mcp_verified": True,
                "tools": metadata.tool_count, "events": [e.get("type") for e in events], "response": answer,
            }))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
