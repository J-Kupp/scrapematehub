# Troubleshooting

Use this guide to diagnose production safely. Start with the dashboard and GitHub Actions; do not
edit application source directly on EC2.

## Dashboard Is Unavailable

1. Check the GitHub deploy run that produced the current release.
2. On EC2, inspect `sudo systemctl status yourbarmate-suppliers`.
3. Check the local health endpoint from EC2: `curl --fail http://127.0.0.1:8765/healthz`.
4. Check Caddy only if the local health endpoint works but the public URL does not.
5. Read service logs before restarting anything. A restart is appropriate only after identifying a
   transient service failure or after a completed deployment.

## A Fargate Job Stays Queued Or Pending

1. Open the job detail and verify backend is `ecs_fargate`, not `local_subprocess`.
2. Inspect task status, stopped reason, and CloudWatch log stream from the job detail.
3. Check ECS cluster capacity/networking, task definition, execution role, and security group only
   through AWS configuration; do not switch to local execution as an unrecorded workaround.
4. If the task never starts, verify ECR image availability and the ECS task execution role.

## Progress Does Not Update

1. Confirm the job has a CloudWatch log group and stream.
2. Look for both standard `PROGRESS` lines in the worker log.
3. If progress lines exist in CloudWatch but not in the dashboard, investigate the job log reader.
4. If no progress lines exist, fix the adapter in Git. Do not patch its live source on EC2.

## Scrape Finds Zero Products Or Too Few

1. Inspect listing diagnostics and the first failure messages.
2. Compare current public page markup/API behavior with committed fixtures.
3. Check login, bot challenge, robots policy, menu/subgroup navigation, sitemap, pagination, and
   canonical URL de-duplication.
4. Add a tested fallback where the public site supports one. A zero discovery result must be a clear
   failed run, never a silent successful sync.
5. Use `keep_existing` until catalogue completeness is proven; do not use missing-product
   inactivation with an incomplete discovery result.

## Sync Fails After A Successful Scrape

1. Read the run summary stage breakdown: scrape, validation, and sync are separate.
2. Resolve validation errors before changing API behavior.
3. Confirm the supplier token is present in the dashboard; do not request or paste the value into
   chat or logs.
4. Use a dry run to inspect create/update/inactivate counts before retrying a live sync.
5. Check the selected catalog policy. `delete_missing` can inactivate products that a complete run
   no longer returns; `keep_existing` does not.
6. A `5xx` from YourBarMate is treated as transient for product creation: the worker checks the
   deterministic product ID and retries safely. A `400` remains a real payload error and needs a
   data or mapping fix.
7. YourBarMate accepts products without a price. Its product API rejects fractional `ml` vessel
   sizes with a server `500`; the cleaner converts those records to `1 quantity` while preserving
   the original size in the product name.

## Scheduled Job Did Not Run

1. Confirm the supplier is enabled, not archived, and has a non-disabled schedule in the dashboard.
2. Check the System page: scheduler must be enabled and in `internal` mode.
3. Review the job history for blocked/queued jobs; only one global job runs at once.
4. Check the EC2 service log for scheduler startup or configuration errors.
5. Do not edit `suppliers.json` expecting it to overwrite a current dashboard schedule. Runtime
   schedule state is persistent by design.

## Automated Error Email Did Not Arrive

1. Alerts apply to scheduled jobs, not normal manual runs.
2. Confirm SES SMTP variables and sender identity are configured in the shared secret.
3. Check the supplier alert thresholds: failure count and failure-rate threshold must both be met
   for partial scrape failures.
4. Check the scheduled job result and service log for alert-delivery errors.

## Deployment Appears Inconsistent

1. Check that **both** EC2 deploy and worker-image workflows succeeded for adapter/shared code.
2. Compare the System page release revision with the merged commit.
3. Hard-refresh the browser before assuming a UI regression.
4. If the control plane is current but a Fargate job uses old behavior, wait for or repair the ECR
   worker-image workflow, then run a limited smoke test.

## Escalation Data

When opening an issue or asking another AI for help, include only non-secret data:

- supplier slug and job ID
- job type, status, backend, task status, exit code, and stopped reason
- relevant log lines with token values removed
- run-summary stage counts and validation/sync errors
- release revision and Actions run URL
