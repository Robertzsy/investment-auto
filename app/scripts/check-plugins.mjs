/**
 * Structural check for Investment Auto 2.0 plugins.
 *
 * Verifies every plugin under app/plugins:
 *  - package.json: scoped name, type module, main entry exists
 *  - client plugins (dsh.client): exports["./client"], lib/client.js present,
 *    script-style `window.__ModuleLoader__.load({ id: <pkg name>, ... })`,
 *    syntax-valid (node --check)
 *  - host half (main): exports name/inject/apply
 *  - every @investment-auto/* row referenced in app/profiles patches
 *    resolves to a real plugin package name
 *
 * Run: node app/scripts/check-plugins.mjs   (exit 1 on any failure)
 */
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const pluginsRoot = join(here, "..", "plugins");
const profilesRoot = join(here, "..", "profiles");

const failures = [];
const check = (ok, message) => {
  if (!ok) failures.push(message);
};

const pluginDirs = readdirSync(pluginsRoot).filter((name) => {
  try {
    return statSync(join(pluginsRoot, name)).isDirectory();
  } catch {
    return false;
  }
});
if (pluginDirs.length === 0) failures.push("no plugins under app/plugins");
const packageNames = new Set();

for (const dir of pluginDirs) {
  const root = join(pluginsRoot, dir);
  const manifestPath = join(root, "package.json");
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, "utf-8"));
  } catch (error) {
    failures.push(`${dir}: package.json missing or invalid (${error.message})`);
    continue;
  }
  const pkgName = manifest.name;
  check(typeof pkgName === "string" && pkgName.includes("/"), `${dir}: missing scoped package name`);
  if (pkgName) packageNames.add(pkgName);
  check(manifest.type === "module", `${dir}: package must be type module`);
  check(typeof manifest.main === "string" && existsSync(join(root, manifest.main)), `${dir}: main entry missing`);
  const mainText = readFileSync(join(root, manifest.main), "utf-8");
  for (const keyword of ["export const name", "export function apply", "export const inject"]) {
    check(mainText.includes(keyword) || mainText.includes(keyword.replace("const ", "")), `${dir}: node half missing ${keyword}`);
  }

  if (manifest.dsh?.client) {
    const clientExport = manifest.exports?.["./client"];
    check(typeof clientExport === "string", `${dir}: dsh.client declared but exports["./client"] missing`);
    const clientPath = clientExport ? join(root, clientExport) : null;
    check(Boolean(clientPath) && existsSync(clientPath), `${dir}: client entry missing`);
    if (clientPath && existsSync(clientPath)) {
      const clientText = readFileSync(clientPath, "utf-8");
      check(
        clientText.includes(`window.__ModuleLoader__.load({`) && clientText.includes(`id: "${pkgName}"`),
        `${dir}: client.js must be a script-style __ModuleLoader__.load with id "${pkgName}"`,
      );
      try {
        execFileSync(process.execPath, ["--check", clientPath], { stdio: "pipe" });
      } catch (error) {
        failures.push(`${dir}: client.js syntax check failed (${error.message.split("\n")[0]})`);
      }
    }
  }
}

// Every @investment-auto/* row referenced in the profile patches must exist.
const referenced = new Set();
for (const profile of ["investment", "investment-web"]) {
  const patchPath = join(profilesRoot, profile, "cordis.patch.yml");
  if (!existsSync(patchPath)) continue;
  for (const match of readFileSync(patchPath, "utf-8").matchAll(/@investment-auto\/[a-z0-9-]+/g)) {
    referenced.add(match[0]);
  }
}
for (const name of referenced) {
  check(packageNames.has(name), `profile patches reference missing plugin ${name}`);
}

if (failures.length > 0) {
  console.error(`plugin check FAILED (${failures.length}):`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}
console.log(`plugin check OK: ${pluginDirs.length} plugins (${[...packageNames].join(", ")})`);
