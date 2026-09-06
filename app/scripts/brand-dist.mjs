/**
 * Investment Auto — web dist branding patch.
 *
 * Rewrites the three user-visible brand surfaces served by dsh-web-frontend:
 *   dist/index.html          <title>            -> Investment Auto
 *   dist/manifest.webmanifest name / short_name -> Investment Auto
 *   dist/favicon.svg         fish logo          -> Investment Auto mark
 *
 * Run after `npm ci` (dev) and before packaging the desktop installer:
 *   node app/scripts/brand-dist.mjs [distDir]
 *
 * The runtime JS bundles are untouched (no user-visible DSH strings live in
 * them; only lowercase module ids and CSS token names do).
 */
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const appRoot = join(here, "..");
const distDir = process.argv[2] ?? join(appRoot, "node_modules", "@deepseek-ai", "dsh-web-frontend", "dist");

if (!existsSync(distDir)) {
  console.error(`brand-dist: dist not found at ${distDir}`);
  process.exit(1);
}

const htmlPath = join(distDir, "index.html");
let html = readFileSync(htmlPath, "utf-8");
html = html.replace(/<title>[^<]*<\/title>/, "<title>Investment Auto</title>");
writeFileSync(htmlPath, html);
console.log(`branded ${htmlPath}`);

const manifestPath = join(distDir, "manifest.webmanifest");
let manifest = readFileSync(manifestPath, "utf-8");
manifest = manifest
  .replace(/"name":\s*"[^"]*"/, '"name": "Investment Auto"')
  .replace(/"short_name":\s*"[^"]*"/, '"short_name": "Investment Auto"');
writeFileSync(manifestPath, manifest);
console.log(`branded ${manifestPath}`);

const faviconPath = join(distDir, "favicon.svg");
const favicon = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50" viewBox="0 0 50 50">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#2f6fed"/>
      <stop offset="1" stop-color="#4cc2ff"/>
    </linearGradient>
  </defs>
  <rect width="50" height="50" rx="11" fill="url(#g)"/>
  <g fill="#ffffff">
    <rect x="14" y="16" width="5" height="18" rx="2.5"/>
    <rect x="22.5" y="12" width="5" height="22" rx="2.5"/>
    <rect x="31" y="20" width="5" height="14" rx="2.5"/>
  </g>
  <polyline points="20,30 25,24 30,27 35,20" fill="none" stroke="#ffffff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" opacity="0.85"/>
</svg>
`;
writeFileSync(faviconPath, favicon);
console.log(`branded ${faviconPath}`);

console.log("brand-dist: done");
