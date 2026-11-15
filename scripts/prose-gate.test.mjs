import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const gate = fileURLToPath(new URL("./prose-gate.mjs", import.meta.url));
const fastapi = fileURLToPath(new URL("./fastapi-gate.mjs", import.meta.url));
const git = fileURLToPath(new URL("./git-readonly.mjs", import.meta.url));

test("the prose gate catches comment style and English violations", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "claudex-prose-"));
  try {
    const file = path.join(directory, "probe.py");
    fs.writeFileSync(file, "# NOTE: Previously this worked.\nmessage = '\\u0430'\n".replace(
      "\\u0430", String.fromCodePoint(0x0430),
    ));
    const blocked = spawnSync(process.execPath, [gate, file], { encoding: "utf8" });
    assert.equal(blocked.status, 2);
    assert.match(blocked.stderr, /remove the comment label/);
    assert.match(blocked.stderr, /describe the current behavior/);
    assert.match(blocked.stderr, /use English/);
    fs.writeFileSync(file, "# guard the clock skew on refresh\n");
    assert.equal(spawnSync(process.execPath, [gate, file]).status, 0);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("the FastAPI gate rejects a bare exception", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "claudex-fastapi-"));
  try {
    const file = path.join(directory, "probe.py");
    fs.writeFileSync(file, "try:\n    pass\nexcept:\n    pass\n");
    const result = spawnSync(process.execPath, [fastapi], {
      input: JSON.stringify({ cwd: directory, tool_input: { file_path: file } }),
      encoding: "utf8",
    });
    assert.equal(result.status, 2);
    assert.match(result.stderr, /bare except/);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("the Git guard allows status and rejects a write", () => {
  function check(command) {
    return spawnSync(process.execPath, [git], {
      input: JSON.stringify({ tool_name: "Bash", tool_input: { command } }),
      encoding: "utf8",
    });
  }
  assert.equal(check("git status --short").status, 0);
  assert.equal(check("git add --dry-run .").status, 2);
});
