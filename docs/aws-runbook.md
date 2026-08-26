# AWS Runbook

This runbook describes the production roles and ownership model without exposing credentials or
account-specific network identifiers. Use AWS Console/IAM and GitHub repository secrets for those
values; never add them to Git or tickets.

## Services

| Service | Role |
| --- | --- |
| EC2 | Always-on control plane: dashboard, scheduler, SQLite state, release metadata |
| ECS/Fargate | On-demand scraper worker task for each job |
| ECR | Worker container image registry |
| CloudWatch Logs | Fargate worker logs and dashboard live progress source |
| Secrets Manager | Supplier tokens, dashboard secrets, and SMTP credentials |
| SES SMTP | Automated scheduled-job alert emails |
| GitHub Actions | CI, worker-image publishing via OIDC, and EC2 deployment via SSH |

## Runtime State

The EC2 runtime stores durable operational state outside the deployed application directory:

```text
/var/lib/yourbarmate-suppliers/control_panel/  dashboard database, supplier config, release metadata
/var/lib/yourbarmate-suppliers/output/         local artifacts
/var/lib/yourbarmate-suppliers/state/          supplier state and snapshots
/var/log/yourbarmate-suppliers/                service and supplier logs
/etc/yourbarmate-suppliers.env                 runtime environment references
```

The deployed source lives under `/opt/yourbarmate-suppliers/app` and must be replaced only by the
deployment workflow. See [Deployment and Rollback](deployment-and-rollback.md).

## Secrets

Production uses `aws-secrets-manager`. The secret value is a JSON object whose keys are runtime
environment-variable names, for example supplier token variables and alert SMTP variables.

- The EC2 service reads the secret at startup.
- Fargate workers read the same secret at task startup.
- The dashboard can update a supplier token through its supported secret-management flow.
- Never print secret values, place them in `suppliers.json`, add them to a PR, or copy them to a
  worker command override.

The expected production configuration is documented in
[`deploy/aws/control_panel.production.json`](../deploy/aws/control_panel.production.json).

## IAM Boundaries

- **GitHub Actions OIDC role**: ECR image publishing only, using `AWS_ROLE_TO_ASSUME`.
- **ECS task execution role**: pull the image and write CloudWatch logs.
- **ECS task runtime role**: read the configured Secrets Manager secret; add only the minimum
  additional permissions needed by an adapter.
- **EC2 control-plane role**: launch/describe/stop ECS tasks and read task logs, plus access to the
  shared secret as configured.

Any permission change should be reviewed in a PR or IAM change record. Prefer a named resource
scope over wildcards wherever AWS supports it.

## GitHub Repository Secrets

These secrets are required for the two deployment paths:

| Secret | Used by |
| --- | --- |
| `AWS_ROLE_TO_ASSUME`, `AWS_REGION`, `ECR_REPOSITORY` | Build Worker Image workflow |
| `EC2_HOST`, `EC2_USER`, `EC2_APP_DIR`, `EC2_SSH_PRIVATE_KEY` | Deploy to AWS EC2 workflow |

The private key remains only in GitHub Secrets and approved operator key storage. It must never be
sent to chat, committed, or added to an environment example.

## Operations

Useful safe checks:

```bash
aws sts get-caller-identity
aws ecs describe-tasks --cluster <cluster> --tasks <task-arn>
aws logs get-log-events --log-group-name <group> --log-stream-name <stream> --start-from-head
```

On EC2, inspect service state and release metadata rather than changing source:

```bash
sudo systemctl status yourbarmate-suppliers
cat /var/lib/yourbarmate-suppliers/control_panel/release.json
```

## Backups And Cost

- `deploy/aws/backup.sh` archives the persistent control-panel, state, and output directories.
  Schedule and test restoration separately; an untested backup is not a recovery plan.
- EC2 is the baseline always-on cost. Fargate billing occurs only while an ECS task runs.
- Fargate task size is currently defined by the worker task definition. Change CPU/memory only after
  measuring scraper throughput and expected job duration.
