# Investment Auto

**AI-Driven Global Portfolio Optimization & Multi-Agent Trading Automation System**

[简体中文](README.md) | [English](README_EN.md)

[![Release](https://img.shields.io/badge/release-v2.1.3-brightgreen)](https://github.com/Robertzsy/ai-trading-automation/releases/tag/v2.1.3)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-lightgrey)]()

Investment Auto 2.1.3 is a desktop application for investment research and **paper trading** across China A-shares, Hong Kong stocks, U.S. equities, and exchange-traded funds, with deterministic screening, a 13-role multi-agent analysis pipeline, and hard risk-controlled execution. Version 2.x is deeply rebuilt on DeepSeek Harness (DSH), but presents a standalone product: no workspace selector, runtime-mode selector, or platform branding—only the Dashboard, Investment Assistant, Analysis Workflow, and Settings.

> This project supports research and paper trading only. It does not connect to a live broker and should not be used directly with real capital.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Run from Source](#run-from-source)
- [Core Investment Logic](#core-investment-logic)
- [Architecture](#architecture)
- [Version History](#version-history)
- [Safety Boundaries](#safety-boundaries)
- [Tests and Release Gate](#tests-and-release-gate)
- [Documentation](#documentation)
- [Branches and Compatibility](#branches-and-compatibility)
- [License](#license)

## Features

- **Four markets in one app**: unified market data, screening, analysis, and paper accounts for A-shares, Hong Kong stocks, U.S. equities, and ETFs; T+1/T+0, lot sizes, price limits, commission, stamp tax, and slippage rules are built in per market.
- **Deterministic screening**: hard filters plus a six-factor weighted score (momentum / trend / liquidity / valuation / volume / low volatility); every selected stock carries Chinese-language evidence, and weights and thresholds are configurable.
- **13-role committee-style analysis**: four base-research tracks → bull-bear debate → research manager and per-symbol trader → portfolio draft → three-way risk debate → final decision; conclusions must cite evidence, and live stages and checkpoints are visible.
- **Risk controls the AI cannot bypass**: three-tier strategy mandate, position capping, drawdown circuit breaker, and built-in stop-loss/take-profit; every AI decision must pass the deterministic Python risk layer and paper broker.
- **Productized desktop app**: Dashboard, Investment Assistant, Analysis Workflow, and Settings; one-click install, single instance, tray icon, autostart, and in-place upgrades that preserve data.
- **Conversational AI assistant**: retains DSH-native reasoning, streaming, tools, Skills, plans, and subagents; IA can also inspect logs, edit source, and run tests to maintain itself.

## Installation

- Download `InvestmentAuto-Setup-x64.exe` from the [v2.1.3 release](https://github.com/Robertzsy/ai-trading-automation/releases/tag/v2.1.3)
- Windows 10/11 x64. The installer bundles Python, Node.js, the .NET desktop runtime, and a WebView2 fallback installer.

SHA-256:

```text
00522AA80EAEF39BB9B59F1B50B458A2177F909AD807914DDB34367A85B49560
```

The program is installed under `%LocalAppData%\Programs\InvestmentAuto`, while user data lives under `%LocalAppData%\InvestmentAuto`. In-place upgrades preserve accounts, holdings, reports, configuration, credentials, and sessions. The 2.1.3 on-machine upgrade check preserved all 76,167 user-data files with zero missing or changed files.

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

## Core Investment Logic

Investment Auto's investment intelligence is built from three deterministic blocks: **exclude, score, and explain screening**, a **13-role committee-style analysis**, and **risk discipline the AI cannot bypass**.

- **Screening**: hard filters first (symbol normalization, minimum price / turnover / market cap, PE/PB caps, excluding ST/delisting/warrants), then a six-factor weighted ranking with Chinese-language evidence per pick;
- **Multi-role analysis**: five stages and 13 roles (technical/fundamental/news/sentiment → bull-bear debate → research manager and trader → portfolio draft → three-way risk debate → risk manager → portfolio manager); conclusions must cite evidence, and under-researched holdings are forced to HOLD;
- **Decision and risk control**: a three-tier strategy mandate defines hard boundaries; position = min(market per-stock cap, strategy per-stock cap); a drawdown circuit breaker force-liquidates; stop-loss/take-profit outrank AI suggestions; trading is limited to the allowed pool and prices are fetched live by the engine.

See [Investment Logic](docs/INVESTMENT_LOGIC_EN.md) for the full details: factor formulas, role responsibilities, risk parameters, and a business-value assessment.

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

## Version History

| Version | Core changes |
|---|---|
| 2.0.0 | Replaced the 1.x custom Agent/window split with DSH-native conversations, tools, Skills, subagents, and workflows. Investment business logic moved into an independent Python engine, with a Windows desktop release, DPAPI credentials, and 1.x data migration. |
| 2.1.0 | Turned “DSH plus an investment preset” into the standalone Investment Auto product. The UI gained a Dashboard, live Analysis Workflow, and Settings while removing workspace/mode selection and runtime branding. Native DSH conversation, reasoning, streaming, and tool rendering remained unchanged. |
| 2.1.1 | Separated stock screening from security analysis. User-specified symbols now enter the fixed full workflow directly. Added asynchronous cycles, status polling, and first-generation execution idempotency to eliminate ad-hoc window analysis, long-request timeouts, and duplicate starts. |
| 2.1.2 | Added durable broker receipts, decision fingerprints, cross-process leases, restart recovery, and strong binding between user-requested symbols and completed analysis. Internal headless and role sessions moved to an isolated DSH Home and no longer pollute the user session list. |
| 2.1.3 | Enabled IA filesystem, PowerShell, search, background-job, and Ralph self-maintenance capabilities. Added the self-maintenance Skill and fixed workflow-schema compatibility, failed-cycle retry races, Windows atomic writes, and accidental packaging of development data. |

See the [Chinese changelog](CHANGELOG.md), [English changelog](CHANGELOG_EN.md), and [bilingual v2.1.3 release notes](docs/RELEASE_NOTES_2.1.3.md) for the complete record.

## Safety Boundaries

IA runs with the DSH `danger-full-access` preset. Within the current Windows user's authority, it can inspect logs, modify project source, run PowerShell, execute tests and builds. System permissions and trading authority remain separate:

- Paper accounts and the paper broker are mandatory.
- Manual submissions still require user approval.
- Every decision must pass the Python mandate and deterministic risk checks.
- Cycle IDs, decision fingerprints, and account-embedded execution receipts prevent duplicate fills.
- API keys and webhooks are stored through Windows DPAPI and never written to ordinary configuration or logs.
- Headless and role subagent sessions live in an isolated internal DSH Home and do not enter the user session list.

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

## Documentation

- [Investment Logic (EN)](docs/INVESTMENT_LOGIC_EN.md) · [投资逻辑详解](docs/INVESTMENT_LOGIC.md)
- [2.0 architecture](docs/ARCHITECTURE_2.0.md) · [Engine API](docs/ENGINE_API.md) · [Product shell](docs/PRODUCT_SHELL.md) · [App runtime guide](app/README.md)

## Branches and Compatibility

- `dsch/2.0`: current 2.x development and release branch.
- `master`: retained 1.x (v0.9.1) history and fallback.
- 1.x user data can be migrated to 2.x; migration and in-place upgrades never delete the source data.

## License

[MIT License](LICENSE)
