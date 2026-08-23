# Changelog

[简体中文](CHANGELOG.md) | [English](CHANGELOG_EN.md)

This English changelog covers the complete Investment Auto 2.x line. For installation and usage, see [README_EN.md](README_EN.md). The exact bilingual GitHub Release body is available in [docs/RELEASE_NOTES_2.1.3.md](docs/RELEASE_NOTES_2.1.3.md).

## 2.1.3 — Full IA Self-Maintenance and Reliability Closure (2026-08-24, branch `dsch/2.0`)

- Restored the complete DSH coding surface for the desktop investment preset and headless profile: PowerShell/shell, filesystem, search, background jobs, subagents, and Ralph. Desktop, development, and headless processes now use `danger-full-access` and receive the authoritative `INVESTMENT_AUTO_ROOT`.
- Added the `self-maintenance` Skill. IA can diagnose real logs and persisted state, edit authoritative source, run tests and builds, replay the failed task, and improve prompts, Skills, or fixed workflows. It must use recoverable backups before replacing an installed version and must not treat a seeded DSH Home as source.
- Rewrote every workflow `agent()` schema to the supported root-level `required` format, extracted shared schema constants, and added startup preflight validation through DSH's public schema validator.
- Fixed the failed-cycle retry race. Only the orchestrator that owns the cross-process cycle lease may reopen a failed run; internal repeated starts no longer erase the original error, and generic headless failures no longer mask a specific workflow exception.
- Fixed recurring Windows `WinError 5` failures in command-bus atomic writes by using unique temporary files and bounded exponential retries.
- Excluded `app/dev-home` from the installer and added upgrade cleanup for the development sessions, credentials, and duplicate dependencies accidentally shipped by older builds. The real user-data directory remains untouched.
- Kept system authority separate from trading authority. Paper-only execution, user approval, decision fingerprints, idempotency, and deterministic Python risk controls remain mandatory.
- Replayed the original failed AAPL cycle on the installed 2.1.3 build with the same cycle ID. It completed `base_research`, `research_debate`, `portfolio_draft`, `risk_review`, and `final_decision`, reaching `ready_for_execution`. The validation used `submit=false` and placed no trade.
- Regression baseline: 184 Python tests, 22 Node plugin tests, and 20 Windows desktop tests, plus Skills/plugin composition, real-profile validation, and in-place upgrade checks. All 76,167 user-data files were preserved with zero missing, changed, or added files.

Installer: `InvestmentAuto-Setup-x64.exe` (161,497,160 bytes)

SHA-256: `00522AA80EAEF39BB9B59F1B50B458A2177F909AD807914DDB34367A85B49560`

## 2.1.2 — Execution Safety and Durable Cycle Recovery (2026-08-23, branch `dsch/2.0`)

- Made the paper account the source of truth for execution idempotency. Fills and bounded execution receipts are committed together under the account lock; a retry after “fill completed, external bookkeeping missing” replays the receipt instead of filling again.
- Made `idempotency_key` mandatory and bound it to a normalized decision fingerprint containing market, symbols, actions, weights, and confidence. Reusing a key with different content is rejected.
- Added the explicit `ready_for_execution` state. Final decisions and their fingerprint are persisted before manual approval or autonomous submission; both paths execute through the same binding check.
- Bound user-specified securities to a completed analysis cycle. A cycle may expand the execution allow-list only when its terminal decision exactly matches the submitted fingerprint.
- Derived cycle identity from the platform session and direct user-message identity. Retries reuse the same cycle; a new user request creates a new cycle even when the text is identical.
- Added cross-process cycle leases and startup recovery. The serving engine resumes orphaned running cycles from checkpoints without double-running or double-submitting.
- Moved headless cycles to an isolated internal DSH Home. The product session list filters subagent sessions, correctly maps search results, and conservatively archives previously leaked internal sessions after backup.
- Enforced the screening/analysis boundary in code: manual analysis rejects an empty symbol list, while autonomous scheduling retains its internal screening entry point. Headless symbol arrays now use strict JSON encoding.
- Regression baseline: 182 Python tests, 21 Node tests, Skills/plugin contracts, 29 headless-browser assertions, and a real wire conversation.

## 2.1.1 — Analysis Routing and Execution Repair (2026-08-22, branch `dsch/2.0`)

- Split the product intent into two explicit paths. A user-named security skips screening and enters `investment_analysis_workflow`; a screening request first produces a normalized candidate list and then enters the same workflow.
- Registered the fixed workflow on the headless host plane so web conversations and autonomous cycles execute the same staged implementation instead of asking a generic Agent to reconstruct the process from prose.
- Retired the old model-visible `investment_run_cycle` entry point and updated the persona, preset, and core Skills so full security analysis has one trusted route.
- Added asynchronous analysis starts and the `investment_analysis_status` polling tool. Long-running analysis no longer holds a browser request open for five minutes or triggers duplicate cycles after a fetch timeout.
- Added initial cycle-level submission idempotency and a background round orchestrator with checkpoint replay.
- Regression baseline: 160 Python tests, 17 Node plugin tests, profile/skill/plugin checks, 27 browser assertions, and real wire validation.

## 2.1.0 — Standalone Investment Auto Product Shell (2026-08-22, branch `dsch/2.0`)

- Removed user-visible DSH branding, workspace selection, permission/mode selection, preset selection, and generic platform settings while retaining DSH as the internal runtime.
- Added the `@investment-auto/dsh-product-shell` client: product navigation, flat session list, Dashboard, Investment Assistant, live Analysis Workflow, Settings, and a collapsible investment context panel. The unnecessary Account navigation item was removed.
- Preserved the native DSH conversation core. Messages, reasoning, streaming output, tools, Skills, subagents, goals, plans, and trajectory bundles were not changed; the product shell only hosts the existing conversation slot.
- Added a real Dashboard and workflow telemetry backed by engine data rather than synthetic UI state.
- Added product settings for models and DPAPI credentials, strategy and risk, autonomous scheduling, markets and screening, notifications, and application behavior. Browser JavaScript never receives the engine URL or token.
- Restored the complete fixed multi-role workflow: four parallel research roles, bull/bear debate, research manager, per-symbol trader, portfolio draft, three-way risk debate, risk manager, and final portfolio manager.
- Added atomic stage checkpoints and `/api/analysis/*` endpoints. Python owns state and hard controls while all LLM work stays in DSH.
- Hardened the self-contained Windows publish and forced product-tree reseeding during upgrades without overwriting user sessions, credentials, configuration, or overrides.
- Regression baseline: 147 Python tests, 12 Node plugin tests, 20 desktop tests, profile validation, browser acceptance, and a real wire conversation.

## 2.0.0 — DSH Runtime Rebuild (2026-08-22, branch `dsch/2.0`)

Investment Auto 2.0 rebuilt the application on DeepSeek Harness. Conversations, sessions, model access, tools, Skills, plans, goals, subagents, and workflows moved to DSH. The 1.x investment business logic was reduced to a standalone Python engine connected through a token-protected loopback HTTP tools bridge.

- **P0 — Foundation:** reorganized investment business logic under `engine/`, removed the old Agent/manager/UI/LLM layers from the 2.x runtime, pinned the complete DSH `0.1.0-rc.6` dependency tree, and created investment web/headless profiles plus the investment preset.
- **P1 — Conversation and tools bridge:** exposed engine status, market data, screening, portfolios, reports, macro data, and mandates through native `investment_*` tools and an investment persona.
- **P2 — Investment Skills and paper execution:** added seven product Skills and the guarded `submit_decisions` chain: engine price resolution, mandate checks, deterministic order construction, paper execution, audit, report, and reflection.
- **P3 — Autonomous cycles:** the Python scheduler spawns DSH headless sessions using the same tools and Skills as the desktop assistant; the engine remains the source of truth for decisions, execution, and recovery.
- **P4 — Windows distribution:** added the WPF/WebView2 desktop process model, dynamic ports, per-launch access tokens, DPAPI credentials, first-run migration/setup, automatic DSH Home seeding, bundled Node/Python runtimes, and the Inno Setup installer.
- **P5 — Product verification:** added investment tool cards, release gates across Python/C#/Node/Skills/plugins, a live autonomous model cycle through paper execution, real 1.x data migration, and automated web-conversation verification.
- On-machine acceptance covered silent install, first-run setup, DPAPI keys, engine startup, installed conversation, and byte-for-byte user-data preservation during upgrade.
- Regression baseline: 121 Python tests, 20 desktop tests, 6 Node plugin tests, and Skills/plugin contracts.

The 1.x line remains on `master` as history and a fallback. Its original changelog is retained in [CHANGELOG.md](CHANGELOG.md).
