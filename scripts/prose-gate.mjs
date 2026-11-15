import fs from "node:fs";
import path from "node:path";

const directories = ["src", "tests", "scripts", "docs", ".github", ".claude", ".agents", ".codex"];
const rootFiles = [
  "AGENTS.md", "CLAUDE.md", "README.md", "Makefile", "pyproject.toml", "mise.toml",
  ".pre-commit-config.yaml",
];
const extensions = /\.(py|md|ya?ml|mjs|toml|json)$/;
const cyrillic = /\p{Script=Cyrillic}/u;
const emoji = /\p{Emoji_Presentation}|\uFE0F|\u20E3/u;
const commentLabel = /^(NOTE|FYI|TODO|FIXME|HACK):/i;
const narration = /\b(previously|used to be|moved to)\b/i;

function filesUnder(directory) {
  if (!fs.existsSync(directory)) return [];
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const name = path.join(directory, entry.name);
    if (entry.name.startsWith("__") && entry.isDirectory()) return [];
    return entry.isDirectory() ? filesUnder(name) : [name];
  });
}

function check(file) {
  if (!extensions.test(file) && path.basename(file) !== "Makefile") return [];
  return fs.readFileSync(file, "utf8").split("\n").flatMap((line, index) => {
    const issues = [];
    if (cyrillic.test(line)) issues.push("use English");
    if (emoji.test(line)) issues.push("remove the emoji");
    const comment = /^\s*#\s+(.+)$/.exec(line)?.[1];
    if (comment && /\.(py|toml|ya?ml)$/.test(file)) {
      if (commentLabel.test(comment)) issues.push("remove the comment label");
      if (narration.test(comment)) issues.push("describe the current behavior");
      if (/^[A-Z][a-z]+\s/.test(comment)) issues.push("start the comment in lowercase");
      if (/[a-z]\.$/.test(comment)) issues.push("omit the terminal period");
    }
    return issues.map((issue) => `${file}:${index + 1}: ${issue}`);
  });
}

let files = process.argv.slice(2);
if (files[0] === "--hook") {
  const event = JSON.parse(fs.readFileSync(0, "utf8"));
  const file = event.tool_input?.file_path;
  files = file && fs.existsSync(file) ? [file] : [];
} else if (files.length === 0) {
  files = [...rootFiles.filter((file) => fs.existsSync(file)), ...directories.flatMap(filesUnder)];
}
const findings = files.flatMap(check);
if (findings.length > 0) {
  process.stderr.write(`${findings.join("\n")}\n`);
  process.exitCode = 2;
}
