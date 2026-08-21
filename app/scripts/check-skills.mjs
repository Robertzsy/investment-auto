/**
 * Structural check for Investment Auto 2.0 skills.
 *
 * Verifies: directory layout + SKILL.md frontmatter (name/description,
 * kebab-case, dir/name agreement) and that every `investment_*` tool a skill
 * references is actually registered by the bridge plugin.
 *
 * Run: node app/scripts/check-skills.mjs   (exit 1 on any failure)
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname, basename } from "node:path";
import { fileURLToPath } from "node:url";
import { apply } from "../plugins/dsh-investment-tools/lib/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const skillsRoot = join(here, "..", "skills");

const failures = [];
const check = (ok, message) => {
  if (!ok) failures.push(message);
};

// 1. Collect registered tool names from the plugin.
const registered = [];
apply({ tools: { register: (definition) => registered.push(definition) } }, { engineUrl: "http://127.0.0.1:1" });
const toolNames = new Set(registered.map((tool) => tool.name));

// 2. Walk skills.
const entries = readdirSync(skillsRoot).filter((name) => {
  try {
    return statSync(join(skillsRoot, name)).isDirectory();
  } catch {
    return false;
  }
});
if (entries.length === 0) failures.push("no skill directories found under app/skills");

const requiredSkills = [
  "security-analysis",
  "market-overview",
  "stock-screening",
  "portfolio-review",
  "portfolio-optimization",
  "complete-investment-cycle",
  "account-management",
];
for (const name of requiredSkills) {
  check(entries.includes(name), `missing required skill directory: ${name}`);
}

for (const dir of entries) {
  const skillPath = join(skillsRoot, dir, "SKILL.md");
  let body;
  try {
    body = readFileSync(skillPath, "utf-8");
  } catch {
    failures.push(`${dir}: SKILL.md missing`);
    continue;
  }
  const match = body.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!match) {
    failures.push(`${dir}: missing frontmatter block`);
    continue;
  }
  const frontmatter = match[1];
  const nameMatch = frontmatter.match(/^name:\s*(\S+)\s*$/m);
  const descMatch = frontmatter.match(/^description:\s*(.+)$/m);
  check(nameMatch, `${dir}: frontmatter missing name`);
  if (nameMatch) {
    check(/^[a-z0-9][a-z0-9-]*$/.test(nameMatch[1]), `${dir}: name not kebab-case (${nameMatch[1]})`);
    check(nameMatch[1] === dir, `${dir}: frontmatter name (${nameMatch[1]}) != directory name`);
  }
  check(descMatch, `${dir}: frontmatter missing description`);
  if (descMatch) check(descMatch[1].length >= 10, `${dir}: description too short`);

  const content = body.slice((match.index ?? 0) + match[0].length);
  check(content.trim().length > 200, `${dir}: body too short (${content.trim().length} chars)`);

  for (const ref of content.matchAll(/`?(investment_[a-z_]+)`?\s*\(/g)) {
    const toolName = ref[1];
    check(toolNames.has(toolName), `${dir}: references unregistered tool ${toolName}`);
  }
}

if (failures.length > 0) {
  console.error(`skill check FAILED (${failures.length}):`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}
console.log(`skill check OK: ${entries.length} skills, ${toolNames.size} registered tools`);
