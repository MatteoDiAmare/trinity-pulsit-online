// PULSIT-MODELS-001: use the existing root-owned hooks on OpenCode's tool path.
// No workspace configuration is rewritten and no new behavioral policy is added.
import { spawnSync } from "node:child_process";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";

function checkHook(script, payload) {
  const run = spawnSync("/usr/bin/python3", [fileURLToPath(new URL("./" + script, import.meta.url))], {
    input: JSON.stringify(payload), encoding: "utf8", timeout: 10_000,
    maxBuffer: 1024 * 1024,
  });
  if (run.error || run.status !== 0) {
    throw new Error((run.stderr || "Trinity tool check failed").trim());
  }
}

function readOnly() {
  const path = (process.env.HOME || "/home/developer") + "/.trinity/read-only-config.json";
  if (!existsSync(path)) return false;
  return Boolean(JSON.parse(readFileSync(path, "utf8")).enabled);
}

function createGuardrails(check = checkHook, isReadOnly = readOnly) {
  return {
    "tool.execute.before": async (input, output) => {
      const tool = input.tool;
      const args = output.args || {};
      if (tool === "bash") {
        if (isReadOnly()) throw new Error("Trinity agent is in read-only mode");
        check("bash-guardrail.py", { tool_name: "Bash", tool_input: args });
      }
      let paths = [];
      if (["write", "edit", "multiedit"].includes(tool)) {
        if (isReadOnly()) throw new Error("Trinity agent is in read-only mode");
        paths = [args.filePath || args.file_path];
      } else if (tool === "apply_patch") {
        if (isReadOnly()) throw new Error("Trinity agent is in read-only mode");
        paths = [...(args.patchText || "").matchAll(
          /^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm,
        )].map((match) => match[1]);
        if (!paths.length) throw new Error("Cannot inspect patch paths");
      }
      for (const path of paths) {
        if (!path) throw new Error("Cannot inspect file tool path");
        const payload = { tool_name: "Write", tool_input: { file_path: path } };
        check("file-guardrail.py", payload);
        check("read-only-guard.py", payload);
      }
    },
    "tool.execute.after": async (input, output) => {
      if (input.tool === "bash") {
        check("output-scanner.py", {
          tool_name: "Bash", tool_response: { output: output.output || "" },
        });
      }
    },
  };
}

// Export exactly ONE plugin. OpenCode invokes every function export as a plugin.
export default async function TrinityGuardrails(_input, options = {}) {
  return createGuardrails(
    typeof options.check === "function" ? options.check : checkHook,
    typeof options.isReadOnly === "function" ? options.isReadOnly : readOnly,
  );
}
