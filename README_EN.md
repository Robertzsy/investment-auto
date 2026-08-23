# Investment Auto

[简体中文](README.md) | [English](README_EN.md)

Investment Auto 2.1.3 is a desktop application for investment research and paper trading across China A-shares, Hong Kong stocks, U.S. equities, and exchange-traded funds. Version 2.x is deeply rebuilt on DeepSeek Harness (DSH), but presents a standalone Investment Auto product: no workspace selector, runtime-mode selector, or underlying platform branding—only the Dashboard, Investment Assistant, Analysis Workflow, and Settings.

> This project supports research and paper trading only. It does not connect to a live broker and should not be used directly with real capital.

## Download

- [Investment Auto v2.1.3 Release](https://github.com/Robertzsy/investment-auto/releases/tag/v2.1.3)
- Installer: `InvestmentAuto-Setup-x64.exe`
- Windows 10/11 x64. The installer bundles Python, Node.js, the .NET desktop runtime, and a WebView2 fallback installer.

SHA-256:

```text
00522AA80EAEF39BB9B59F1B50B458A2177F909AD807914DDB34367A85B49560
```

The program is installed under `%LocalAppData%\Programs\InvestmentAuto`, while user data lives under `%LocalAppData%\InvestmentAuto`. In-place upgrades preserve accounts, holdings, reports, configuration, credentials, and sessions. The 2.1.3 on-machine upgrade check preserved all 76,167 user-data files with zero missing or changed files.

## What Changed Since 2.0

| Version | Core changes |
|---|---|
| 2.0.0 | Replaced the 1.x custom Agent/window split with DSH-native conversations, tools, Skills, subagents, and workflows. Investment business logic moved into an independent Python engine, with a Windows desktop release, DPAPI credentials, and 1.x data migration. |
| 2.1.0 | Turned “DSH plus an investment preset” into the standalone Investment Auto product. The UI gained a Dashboard, live Analysis Workflow, and Settings while removing workspace/mode selection and runtime branding. Native DSH conversation, reasoning, streaming, and tool rendering remained unchanged. |
| 2.1.1 | Separated stock screening from security analysis. User-specified symbols now enter the fixed full workflow directly. Added asynchronous cycles, status polling, and first-generation execution idempotency to eliminate ad-hoc window analysis, long-request timeouts, and duplicate starts. |
| 2.1.2 | Added durable broker receipts, decision fingerprints, cross-process leases, restart recovery, and strong binding between user-requested symbols and completed analysis. Internal headless and role sessions moved to an isolated DSH Home and no longer pollute the user session list. |
| 2.1.3 | Enabled IA filesystem, PowerShell, search, background-job, and Ralph self-maintenance capabilities. Added the self-maintenance Skill and fixed workflow-schema compatibility, failed-cycle retry races, Windows atomic writes, and accidental packaging of development data. |

See the [Chinese changelog](CHANGELOG.md), [English changelog](CHANGELOG_EN.md), and [bilingual v2.1.3 release notes](docs/RELEASE_NOTES_2.1.3.md) for the complete record.

## Current Product Capabilities

- Market data, screening, security analysis, portfolio review and optimization, macro reports, and independent paper accounts across four markets.
- A Dashboard covering engine health, risk state, market assets, recent cycles, and reports.
- An Investment Assistant that retains DSH-native sessions, reasoning, streaming, tools, Skills, plans, goals, and subagents.
- A live Analysis Workflow backed by real cycle stages, evidence counts, checkpoints, and final decisions—not role conversations exposed as fake progress.
- Unified Settings for models and DPAPI credentials, strategy, deterministic risk controls, markets, screening, schedules, and notifications.
- A single-instance Windows shell with dynamic loopback ports, per-launch access tokens, managed background processes, and safe in-place upgrades.
- IA source maintenance: log inspection, authoritative source edits, tests, builds, task replay, and rollback.

## Screening and Security Analysis

Version 2.1 defines two explicit entry paths:

```text
Screening request
  → deterministic market filters and factor scoring
  → Agent reranking
  → normalized candidate list
  → fixed full analysis workflow

User-specified security
  → security identity resolution
  → skip screening
  → fixed full analysis workflow
```

The fixed workflow is:

```text
Technical / fundamental / news / sentiment research
  → bull-bear debate
  → research manager and per-symbol trader
  → portfolio draft
  → aggressive / conservative / neutral risk debate
  → risk manager
  → final portfolio decision
  → user approval or autonomous-cycle authorization
  → deterministic Python risk controls
  → paper execution, audit, and report
```

Every stage is atomically persisted under `runtime/analysis_runs/`. A crash or machine restart resumes from checkpoints. Cycle IDs, decision fingerprints, and broker receipts stored with the account prevent duplicate analysis and duplicate fills.

## Architecture

```text
Windows WPF + WebView2
          │
Investment Auto product shell
          │
DSH conversation / tools / Skills / subagents / workflows
          │  token-protected loopback HTTP API
Python investment engine
          │
market data, screening, portfolio, risk, paper broker, audit, scheduler
```

- `app/`: DSH profiles, preset, Skills, investment tools bridge, fixed workflow, and product UI.
- `engine/`: investment facts, configuration, cycle state, hard risk controls, paper broker, and scheduler.
- `windows/desktop/`: WPF/WebView2 shell and process lifecycle.
- `installer/`: self-contained Windows installer.
- `tests/`: engine, recovery, idempotency, configuration, and desktop regressions.

Read the [2.0 architecture](docs/ARCHITECTURE_2.0.md), [engine API](docs/ENGINE_API.md), [product shell](docs/PRODUCT_SHELL.md), and [app runtime guide](app/README.md) for details.

## Permissions and Safety Boundaries

IA runs with the DSH `danger-full-access` preset. Within the current Windows user's authority, it can inspect logs, modify project source, run PowerShell, execute tests and builds, and complete a “diagnose → repair → verify → replay → rollback” maintenance loop.

System permissions and trading authority remain separate:

- Paper accounts and the paper broker are mandatory.
- Manual submissions still require user approval.
- Every decision must pass the Python mandate and deterministic risk checks.
- Cycle IDs, decision fingerprints, and account-embedded execution receipts prevent duplicate fills.
- API keys and webhooks are stored through Windows DPAPI and never written to ordinary configuration or logs.
- Headless and role subagent sessions live in an isolated internal DSH Home and do not enter the user session list.

## Run from Source

Python 3.10+, Node.js 22+, and the .NET 8 SDK are required; .NET is needed only when building the desktop shell.

```powershell
# Install Python dependencies
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# Terminal 1: start the investment engine
.\.venv\Scripts\python.exe -m engine.main serve

# Terminal 2: start the Investment Auto web product shell
.\app\scripts\dev.ps1 -Port 4567
```

Open `http://127.0.0.1:4567`, then configure the model and API key in Settings.

## Tests and Release Gate

Investment Auto 2.1.3 passed:

- 184 Python tests.
- 22 Node plugin tests.
- 20 Windows desktop tests.
- Skills and plugin composition checks, real-profile validation, in-place upgrade verification, and a live recovery of the complete AAPL workflow.

Run the complete release gate with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release-check.ps1
```

## Branches and Compatibility

- `dsch/2.0`: current 2.x development and release branch.
- `master`: retained 1.x (v0.9.1) history and fallback.
- 1.x user data can be migrated to 2.x; migration and in-place upgrades never delete the source data.

## License

[MIT License](LICENSE)
