# Deployment And Rollback

## Normal Delivery

Every source change uses this sequence:

```text
codex/<topic> branch -> commit -> push -> pull request -> CI -> merge main -> deploy -> live verification
```

1. Create a `codex/<topic>` branch from current `main`.
2. Make code, test, fixture, and documentation changes together.
3. Run `.venv/bin/python tools/check_repo_hygiene.py` and `.venv/bin/python -m unittest discover -s tests -v` locally.
4. Commit, push, and open a PR.
5. Wait for GitHub CI to pass before merging.
6. Merging to `main` triggers three workflows:
   - **CI**: compile and run tests.
   - **Deploy to AWS EC2**: run tests again, rsync source to EC2, merge new supplier defaults into
     persistent dashboard state, restart the service, and call `/healthz`.
   - **Build Worker Image**: build/push the Fargate image to ECR when worker-relevant files change.
7. Verify the deployment: GitHub workflow success, System page release revision, dashboard health,
   then a limited Fargate job for worker changes.

## Important Deployment Semantics

- GitHub Actions deploys source to the EC2 control plane. It never replaces dashboard schedules,
  supplier enablement, catalog policy, tokens, job history, or other persistent dashboard state.
- `suppliers.json` is deployed as defaults. The merge script only adds supplier slugs that do not
  already exist in the runtime config.
- The worker image is published separately and currently uses ECR's `latest` tag. The EC2 and worker
  workflows can finish in either order. For adapter/worker changes, wait for **both** workflows to
  succeed before launching a Fargate job.
- Do not deploy by manually editing EC2 source files or by running unreviewed commands from the
  production host.

## Verification Checklist

1. Confirm the PR and its CI result in GitHub.
2. Confirm `Deploy to AWS EC2` succeeded.
3. If adapter/shared worker code changed, confirm `Build Worker Image` succeeded.
4. Open the dashboard System page and check release revision/time and ECS readiness.
5. For UI-only changes, verify the affected page after a hard refresh.
6. For scraper changes, run a limited scrape or dry run and confirm progress markers, result summary,
   product count, and failures.
7. For sync changes, use a dry run before any live write.

## Rollback

Use Git to roll back source. Do not SSH into EC2 to change source files.

1. Identify the last known-good merge commit and its successful Actions runs.
2. Create a revert branch from current `main`.
3. Revert the problematic commit, add a regression test if appropriate, then open a PR.
4. Merge only after CI passes.
5. Wait for EC2 deploy and worker-image build where applicable.
6. Verify the release revision and a limited Fargate job.

Dashboard settings are intentionally not reverted by a source rollback. If a schedule, token, or
catalog policy caused the incident, correct that runtime setting in the dashboard and record the
operational change separately.

## Failed Deploys

- **CI failed**: fix locally; do not retry by changing EC2.
- **EC2 deploy failed**: inspect the Actions log, SSH connectivity, service status, and `/healthz`.
- **Worker image failed**: inspect ECR/OIDC Actions output; existing workers may still use the last
  successful `latest` image.
- **Dashboard healthy but worker stale**: do not launch new scraper jobs until the worker image build
  completes successfully.
