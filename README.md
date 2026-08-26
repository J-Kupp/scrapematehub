# ScrapeMate Hub

ScrapeMate Hub is the YourBarMate supplier-catalog platform. It discovers supplier products,
normalizes them into the shared import model, validates exports, and optionally synchronizes
them to YourBarMate. The dashboard runs on an EC2 control plane; scraper jobs run on-demand in
ECS/Fargate.

## Start Here

These documents are authoritative. Read the relevant one before changing code, AWS resources,
or dashboard configuration.

- [Architecture](docs/architecture.md): system boundaries, data flow, and sources of truth
- [New Supplier](docs/new-supplier.md): required adapter structure, tests, and delivery checklist
- [AWS Runbook](docs/aws-runbook.md): production services, secrets, permissions, and operations
- [Deployment and Rollback](docs/deployment-and-rollback.md): GitHub Actions delivery process
- [Troubleshooting](docs/troubleshooting.md): safe diagnosis of dashboard, job, schedule, and sync issues
- [Contribution rules](AGENTS.md): instructions that every AI agent and contributor must follow

## Current Platform

- Registered adapters: `fideco`, `gourmador`, `laenggasstee`, `swissbox`, `terravigna`, and `walker`.
- Shared code lives in `core/`, `models.py`, `orchestrator.py`, validation/export modules, and `ybm.py`.
- Supplier-specific code lives only in `adapters/<supplier_slug>/`.
- Each production scraper job uses ECS/Fargate on demand. The EC2 instance hosts the dashboard,
  scheduler, SQLite job history, and persistent runtime state.
- Git owns source code, tests, deployment assets, and supplier defaults. The dashboard owns
  current supplier settings, schedules, enablement, and secret entry.

## Local Development

Python 3.12 is the CI and deployment baseline.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" .venv/bin/python -m playwright install chromium
.venv/bin/python tools/check_repo_hygiene.py
.venv/bin/python -m unittest discover -s tests -v
```

Use `.env.example`, `suppliers.example.json`, and `control_panel.example.json` only as local
templates. Never commit `.env.local`, tokens, SSH keys, generated output, browser caches, or
production state.

## Delivery Rule

Every change follows:

```text
codex/<topic> branch -> commit -> push -> PR -> CI -> merge -> deploy -> live verification
```

Do not edit production source code on EC2. A deployment preserves dashboard settings rather than
overwriting them from Git; see [Architecture](docs/architecture.md#configuration-and-state-ownership).
