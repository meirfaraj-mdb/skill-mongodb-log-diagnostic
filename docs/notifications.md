# P0 → Jira and P1 → Slack notifications (per environment)

The pipeline can turn the cluster report into Jira tickets for P0 items and a Slack message for P1 items. Each
deployment (dev, staging, production) sets its own `MONGODB_LOG_DIAG_ENVIRONMENT` and modes. When a single
configuration is shared across deployments, use `MONGODB_LOG_DIAG_NOTIFY_ENV_OVERRIDES` instead.

| Channel | `off` (default) | `json` | `create` / `send` |
|---|---|---|---|
| Jira (`JIRA_MODE`, P0) | nothing (unchanged behaviour) | `cluster/reports/jira-issues.json`, a Jira REST bulk-create body | issues created through the Jira REST API |
| Slack (`SLACK_MODE`, P1) | nothing | `cluster/reports/slack-messages.json`, Block Kit payloads | posted via incoming webhook or bot token |

## Inputs
The step reads `<prefix>/<day>/cluster/reports/`:

- `cluster-summary.md`, or `cluster-summary.from-node-reports.md` when that is the configured summary source.
- `manifest.json`.
- `trend-alerts.json`, which is linked from the notification.

With `NOTIFY_SOURCE=auto`, the node `report.md` files (section 3, prioritized findings) are used only when the
cluster summary contains no P0/P1 item. When any mode is enabled, the cluster-summary prompt also asks for a
machine-readable `## Prioritized findings` table with a stable `Key` column. The parser also accepts
`### P0 — …` headings and `- **P1** …` bullets.

## "Only if not yet created"
- Each finding gets a fingerprint built from the environment, the cluster, and the issue identity.
- Issue identity is the table `Key`. Without a `Key`, it is the category plus namespaces, query hashes and error codes. Counts, dates and priority are not part of it.
- Every Jira issue carries the label `mldiag-<fingerprint>` (plus `mldiag-env-<env>`).
- `create` mode runs `project = KEY AND labels = "mldiag-<fp>"` (plus `AND statusCategory != Done` when `JIRA_DEDUPE_SCOPE=open`) before creating anything. If that search fails, nothing is created.
- A ledger at `<prefix>/notifications/<env>/<cluster>/ledger.json` keeps `json` mode from exporting the same issue again.
- The same ledger stops Slack from re-sending an item for the same day, or within `SLACK_REPEAT_AFTER_DAYS`.
- Optional: `JIRA_COMMENT_ON_EXISTING=true` adds one "still observed" comment per day to the existing ticket.

## Credentials
Notification credentials live in a separate secret (`NOTIFICATION_SECRET_ID`, on AWS Secrets Manager or GCP
Secret Manager), or in `NOTIFICATION_CONFIG_FILE` for local runs. Allowed keys: `jira_email` + `jira_api_token`
(Jira Cloud), `jira_pat` (Data Center), `slack_webhook_url`, `slack_bot_token`. See
`samples/secret.notifications.example.json`. The secret is read only when a `create`/`send` mode needs it. The
Atlas secret still accepts only Atlas keys.

Grant the runtime identity read access to the new secret (`secretsmanager:GetSecretValue` on AWS,
`roles/secretmanager.secretAccessor` on GCP) and allow outbound HTTPS to Jira and Slack.

## Running
The notification step runs automatically at the end of `--stage report`, `cluster-summary` and
`cluster-summary-md`. Errors are recorded in `notifications.json` and in `manifest.json → notifications`;
they never fail the report. To re-run it alone, which is idempotent:

```bash
python -m agent.handler --stage notify --log-date 2026-11-02
```

All settings are listed in `env.example` under "Per-environment notifications".
