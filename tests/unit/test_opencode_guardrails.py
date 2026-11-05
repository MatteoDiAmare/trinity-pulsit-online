"""Execute the OpenCode plugin hooks in Node, including patch and read-only paths."""
import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PLUGIN = Path(__file__).resolve().parents[2] / "docker/base-image/hooks/opencode-guardrails.mjs"


def test_hooks_execute_existing_checks_and_reject_writes_in_read_only():
    # No model, network or external application is involved.
    result = subprocess.run(["node", "--input-type=module", "-e", """
      const mod = await import(process.argv[1]);
      if (Object.keys(mod).join() !== "default") throw new Error("multiple plugin exports");
      const calls = [];
      const hooks = await mod.default({}, {
        check: (script, payload) => calls.push({script, payload}),
        isReadOnly: () => false,
      });
      await hooks["tool.execute.before"]({tool:"bash"}, {args:{command:"pwd"}});
      await hooks["tool.execute.before"]({tool:"write"}, {args:{filePath:"a.txt"}});
      await hooks["tool.execute.before"]({tool:"apply_patch"}, {args:{
        patchText:"*** Update File: a.txt\\n*** Move to: b.txt\\n*** Delete File: c.txt"
      }});
      await hooks["tool.execute.after"]({tool:"bash"}, {output:"output"});
      const locked = await mod.default({}, {isReadOnly: () => true});
      let denied = 0;
      for (const tool of ["bash","write","apply_patch"]) {
        try { await locked["tool.execute.before"]({tool}, {args:{filePath:"x"}}); }
        catch { denied++; }
      }
      if (denied !== 3) throw new Error("read-only gap");
      console.log(JSON.stringify(calls));
    """, PLUGIN.as_uri()], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout)
    assert calls[0]["script"] == "bash-guardrail.py"
    file_calls = [c for c in calls if c["script"] == "file-guardrail.py"]
    assert [c["payload"]["tool_input"]["file_path"] for c in file_calls] == [
        "a.txt", "a.txt", "b.txt", "c.txt",
    ]
    assert calls[-1]["script"] == "output-scanner.py"


def test_failed_hook_blocks_the_tool():
    result = subprocess.run(["node", "--input-type=module", "-e", """
      const {default: plugin} = await import(process.argv[1]);
      const hooks = await plugin({}, {
        check: () => { throw new Error("guard denied"); },
        isReadOnly: () => false,
      });
      try {
        await hooks["tool.execute.before"]({tool:"bash"}, {args:{command:"pwd"}});
        process.exit(2);
      } catch (error) {
        console.log(error.message);
      }
    """, PLUGIN.as_uri()], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout.strip() == "guard denied"
