# Investment-Auto Skill Runtime

## Runtime contract

The conversation Manager is a Harness coordinator, not a router over business functions. Its public tool surface is intentionally bounded:

- `run_skill`
- `list_skills`
- `schedule_skill`
- `list_skill_schedules`
- `manage_runtime`
- `handoff_session`

All investment data and execution functions live in `ActionRegistry` and are invisible to the top-level model.

## Session scopes

| Scope | Purpose | Maximum authority |
|---|---|---|
| `investment_research` | security research and screening | read-only |
| `portfolio_management` | account review and optimization | portfolio write |
| `investment_execution` | complete paper-investment cycles and controls | investment execution |
| `system_admin` | code, provider configuration, and Skill creation | project administration |

Session state is stored under `runtime/manager/sessions/`. Continuation requests can reuse the last entities and Skill without putting all prior transcripts into every prompt.

## Skill package

Every Skill directory contains:

```text
<skill>/
├── SKILL.md
├── manifest.json
├── workflow.json
└── completion.schema.json
```

The manifest declares selection phrases, session scope, side-effect level and allowed Actions. The workflow lists ordered steps, required/optional status and bounded retries. The completion contract lists required completed steps, required output paths, and semantic quality gates such as identity, freshness, fact consistency and evidence coverage.

The runtime rejects:

- undeclared Actions;
- Actions unavailable to the selected session;
- an Action whose side-effect level exceeds the Skill declaration;
- missing required steps or outputs;
- runtime-created Skills that silently overwrite built-ins.

## Execution and verification

`SkillRuntime.run()` selects or loads one Skill, executes its workflow, validates the completion contract, persists the session and writes a trajectory under `runtime/manager/trajectories/<date>/`.

`status=completed` requires `validation.passed=true`. `degraded` means the workflow produced a report but one or more semantic quality gates failed. `incomplete` means a required step/output or fatal Action failed. Calling one or more Actions is never verification.

Security identities preserve canonical symbol, provider symbol, exchange, asset type, market and normalized name across every Action. A quote whose returned identity differs from the resolved identity is rejected before report generation. The dedicated `market-overview` Skill uses fixed exchange-qualified A-share benchmarks and never substitutes a similarly numbered equity for an index.

## Incident repair

`incident-repair` reads a failed/degraded trajectory, classifies the failure, and replays the original request. The system-admin supervisor permits at most eight model requests, six tool calls and one code mutation per incident. Existing files only accept one hash-bound exact snippet replacement (up to 200 lines), never a whole-file rewrite. A candidate patch runs the installed-runtime verifier and then replays the original read-only request in a fresh Python interpreter, so newly written modules are actually imported. Verifier launch failures, timeouts, replay crashes, parse failures, and failed semantic gates all restore the backup. Billing, network, provider-account and permission failures are returned as `external_blocker` without a code mutation. Every outcome is persisted in the Harness repair audit.

## Skill creation

`system_admin` calls `create_skill`, which validates the manifest and workflow, creates any missing read-only custom Actions through the transactional tool factory, forces a real test invocation, registers the package atomically, and optionally runs `fulfill_request` immediately.

Generated Action code retains the existing AST import restrictions: it cannot import trading, portfolio, investment-command, research-sandbox or subprocess modules. Execution authority must use built-in reviewed Actions.

## Scheduling

Schedules are stored under `runtime/manager/skill_schedules/` and pin:

- schedule id;
- Skill name and version;
- five-field Cron expression;
- IANA timezone;
- complete structured inputs.

The scheduler calls the Skill directly. It does not send a natural-language message to the chat Manager. `system_admin` Skills cannot be scheduled.

The desktop `/harness` workspace exposes the redacted Skill catalog, the four persistent sessions, recent completion-validated trajectories, and structured schedule controls. Schedule changes are detected across processes and hot-loaded by the investment scheduler within 15 seconds.

## Acceptance checks

Run:

```bash
python scripts/eval-skill-runtime.py
python -m pytest -q
```

The deterministic selector evaluation covers 56 Chinese and English formulations across market overview, security analysis, screening, portfolio review, optimization, complete cycles, runtime control, incident repair and Harness inspection.
