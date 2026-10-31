#!/usr/bin/env python3
"""Print a compact, secret-masked trace of a Trinity execution log (stdin: JSON from .../executions/<id>/log).
Shows what the agent actually did: its text, every tool call with input, a snippet of each result, and the final answer."""
import json
import re
import sys

MASK = re.compile(r"(ghp_|gho_|github_pat_|sk-ant-|trinity_mcp_|Bearer\s+)[A-Za-z0-9_\-\.]{8,}")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 700


def clean(s, n=LIMIT):
    s = MASK.sub(lambda m: m.group(1) + "***", s if isinstance(s, str) else json.dumps(s, ensure_ascii=False))
    return s if len(s) <= n else s[:n] + f" ...[+{len(s) - n} chars]"


data = json.load(sys.stdin)
log = data.get("log") if isinstance(data, dict) and "log" in data else data
if not log:
    sys.exit("no execution log available")
calls = 0
for entry in log if isinstance(log, list) else [log]:
    kind = entry.get("type") if isinstance(entry, dict) else None
    msg = entry.get("message", {}) if isinstance(entry, dict) else {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if kind == "assistant" and isinstance(content, list):
        for b in content:
            if b.get("type") == "text" and b.get("text", "").strip():
                print(f"\n[Saba] {clean(b['text'], 1500)}")
            elif b.get("type") == "tool_use":
                calls += 1
                print(f"\n[tool call #{calls}] {b.get('name')}")
                inp = b.get("input", {})
                print("   input:", clean(inp.get("command") or inp.get("file_path") or inp))
    elif kind == "user" and isinstance(content, list):
        for b in content:
            if b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                print("   result:", clean(c or ""))
    elif kind == "result":
        print(f"\n[final] status={entry.get('subtype')} turns={entry.get('num_turns')} cost={entry.get('total_cost_usd')} duration_ms={entry.get('duration_ms')}")
        if entry.get("result"):
            print(clean(entry["result"], 3000))
print(f"\n({calls} tool calls shown)")
