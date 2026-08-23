/**
 * One-time cleanup of background (headless) session groups leaked into the
 * user's session store by pre-isolation builds.
 *
 * Background rounds used to share the user's DSH home; their sessions were
 * created with the APP ROOT as cwd, so they live in their own cwd-keyed
 * directory under <home>/sessions/ while real user sessions live under the
 * fixed workspace's cwd key. This script moves every cwd-keyed session
 * group whose sessions are NOT referenced by any workspace record into a
 * timestamped backup directory — it NEVER deletes anything.
 *
 * Usage:
 *   node app/scripts/cleanup-internal-sessions.mjs [home]
 *   (home defaults to $DSH_HOME, then %LocalAppData%\InvestmentAuto)
 *
 * Run with the desktop app stopped. Exit 0 = cleanup performed or nothing to
 * do; exit 1 = failure (nothing moved halfway: each group is one rename).
 */
import { readFileSync, readdirSync, statSync, mkdirSync, renameSync, existsSync } from "node:fs";
import { join, basename } from "node:path";
import { homedir } from "node:os";

const home = process.argv[2] ??
  process.env.DSH_HOME ??
  join(process.env.LOCALAPPDATA ?? homedir(), "InvestmentAuto");

const sessionsRoot = join(home, "sessions");
const workspacePath = join(home, "storages", "workspace.json");

if (!existsSync(sessionsRoot)) {
  console.log("cleanup-internal-sessions: no sessions dir at " + sessionsRoot);
  process.exit(0);
}

// Sessions referenced by any workspace are USER sessions — never touch them.
const referenced = new Set();
if (existsSync(workspacePath)) {
  try {
    const workspace = JSON.parse(readFileSync(workspacePath, "utf-8"));
    for (const record of Object.values(workspace?.tables?.workspaces ?? {})) {
      for (const id of record.sessionIds ?? []) referenced.add(String(id));
      for (const id of record.archivedSessionIds ?? []) referenced.add(String(id));
    }
  } catch (error) {
    console.error("cleanup-internal-sessions: workspace.json unreadable: " + error.message);
    process.exit(1);
  }
}

const candidates = [];
for (const groupName of readdirSync(sessionsRoot)) {
  const groupPath = join(sessionsRoot, groupName);
  let stat;
  try {
    stat = statSync(groupPath);
  } catch {
    continue;
  }
  if (!stat.isDirectory()) continue;
  let sessionDirs = [];
  try {
    sessionDirs = readdirSync(groupPath).filter((name) => {
      try {
        return statSync(join(groupPath, name)).isDirectory();
      } catch {
        return false;
      }
    });
  } catch {
    continue;
  }
  if (sessionDirs.length === 0) continue;
  const anyUserSession = sessionDirs.some((name) => referenced.has(name));
  if (!anyUserSession) candidates.push({ groupName, groupPath, count: sessionDirs.length });
}

if (candidates.length === 0) {
  console.log("cleanup-internal-sessions: no internal session groups found; nothing moved.");
  process.exit(0);
}

const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
const backupRoot = join(home, `sessions-backup-internal-${stamp}`);
mkdirSync(backupRoot, { recursive: true });
let moved = 0;
for (const candidate of candidates) {
  const target = join(backupRoot, candidate.groupName);
  try {
    renameSync(candidate.groupPath, target);
    console.log(`archived ${candidate.count} internal session(s): ${candidate.groupName} -> ${target}`);
    moved += 1;
  } catch (error) {
    console.error(`FAILED to archive ${candidate.groupName}: ${error.message} (left in place)`);
  }
}
console.log(`cleanup-internal-sessions: ${moved}/${candidates.length} group(s) moved to ${backupRoot}`);
process.exit(0);
