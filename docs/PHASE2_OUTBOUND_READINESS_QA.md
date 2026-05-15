# Phase 2 Outbound Readiness QA

Generated: 2026-05-12

Scope: report-only inspection of dashboard routes, contact readiness, public packets, email infra checks, outbound readiness, send controls, `send_outreach.py`, suppression behavior, queue/event tables, inbox sync, and Docker readiness.

Safety performed: no email sent, no SMTP login, no external API calls, no audits, no deletes. SQLite checks were read-only.

## 1. Current Route Inventory

| Route | Method | Purpose | Can send email? | Notes |
|---|---:|---|---|---|
| `/` | GET | Overview dashboard | No | Reads counts only. |
| `/review` | GET | Review queue | No | Reads prospects/artifacts. |
| `/leads` | GET | Lead list | No | Reads prospects. |
| `/crm` | GET | CRM board | No | Reads prospects/events. |
| `/crm/stage/<stage>` | GET | CRM stage detail | No | Reads filtered prospects. |
| `/outbound` | GET | Outbound readiness | No | Shows blockers and send-ready rows. |
| `/outbound/queue` | POST | Creates step-1 queue rows | No | Writes `outreach_queue`; copy states queued emails are not sent. |
| `/send` | GET | Controlled send dashboard | No | Reads queue, infra report, inbox summary. |
| `/send/test` | POST | Infrastructure test email | Yes, test only | Requires checkbox; does not touch prospects or queue. |
| `/send/batch` | POST | Controlled real send | Yes | Requires exact confirmation text and queue gates. |
| `/pipeline` | GET | Redirects to `/run` | No | No send path. |
| `/run` | GET | Pipeline controls | No | No send job in UI. |
| `/run/full-pipeline` | POST | Starts whitelisted pipeline job | No | `dashboard_jobs.ALLOWED_JOBS` excludes send/outreach. |
| `/jobs` | GET | Job list | No | Lists whitelisted pipeline jobs. |
| `/jobs/start` | POST | Starts whitelisted job | No | Allows Places/audit/etc.; no `send_outreach`. |
| `/jobs/<job_key>` | GET | Job detail/log | No | Read-only job view. |
| `/jobs/<job_key>/status` | GET | Job status JSON | No | Read-only. |
| `/pipeline/run` | POST | Retired old runner | No | Redirects with error. |
| `/markets` | GET | Market manager | No | Reads config and counts. |
| `/markets/add` | POST | Add market config | No | Writes `config/markets.yaml` only. |
| `/case/<id>` | GET | Case page | No | Read-only page. |
| `/case/<id>/review` | POST | Manual review decision | No | Writes review/contact state only. |
| `/case/<id>/stage` | POST | CRM stage update | No | Writes CRM state/event. |
| `/case/<id>/contact` | POST | Save contact | No | Writes contact only. |
| `/case/<id>/visual-review` | POST | Save visual critique | No | Writes visual review. |
| `/case/<id>/outreach-drafts` | POST | Generate local drafts | No | Generates draft artifacts; no SMTP. |
| `/media/<path>` | GET | Safe local media serving | No | Limited to approved media roots. |
| `/files/<path>` | GET | Project file serving | No | Local file helper. |
| `/health` | GET | Health check | No | Used by Docker healthcheck. |

Route verdict: email sending is isolated to `/send/test` and `/send/batch`. `/run` and job runner paths still cannot send email.

## 2. Send Gate Audit

Dashboard send path:
- `/outbound` computes readiness in `src/dashboard_app.py` via `load_outbound_readiness()` and `build_outbound_row()`.
- `/outbound/queue` creates queue rows only from `ready_rows`.
- `/send/batch` sends only queued rows through `send_dashboard_batch()`.

Required gates:

| Gate | Dashboard status | Evidence |
|---|---|---|
| Approved | Pass | `prepare_send_queue_row()` requires `human_review_decision = APPROVED`. |
| Drafted / send action | Pass | Requires status in `OUTREACH_DRAFTED` or compatible approved state and `next_action = SEND_OUTREACH`. |
| Contact ready | Pass | Queue row must have a valid email from contact/queue. Current DB has `contacts = 0`, so none are ready. |
| Public packet ready | Pass | Queue row must have ready public packet artifact and URL. Current DB has `public_packet artifacts = 0`. |
| Draft ready | Pass | Queue row must have ready draft artifact, file, subject, and body. Current DB has `email_draft artifacts = 0`. |
| Not suppressed | Pass | `/outbound` checks suppression at queue creation and `/send/batch` re-checks before SMTP. Current DB has `suppression_list = 0`. |
| Not duplicate | Pass | Checks prior sent event key and queue uniqueness. Current DB has `outreach_queue = 0`. |
| Infra configured | Pass as a blocker | `/send/batch` requires latest infra report with no FAIL rows and complete send config. Current infra report has FAIL rows. |
| Daily cap | Pass | Dashboard default batch limit 5, max 10; daily cap from `config/outreach.yaml` defaults to 10. |

Current data snapshot:
- `prospects = 222`
- `contacts = 0`
- `outreach_queue = 0`
- `suppression_list = 0`
- `artifacts = 55`
- `email_draft artifacts = 0`
- `public_packet artifacts = 0`
- `website_audits = 136`
- Latest infra report: FAIL for SMTP host/user/password/from email, physical address, unsubscribe email, public packet base URL, and sending domain.

CLI sender note:
- `src/send_outreach.py` is still capable of real SMTP sending when run manually with `--send`.
- It has good basic gates: approved/status/next_action for normal batch sends, contact/draft, suppression, duplicate event, daily cap, footer, List-Unsubscribe, and dry-run default.
- It does not require a public packet artifact and `--prospect-id` step-1 selection is looser than the dashboard queue path.
- Recommendation: do not use `send_outreach.py --send` for the first batch. Use `/send` only, or align the CLI gates before relying on it.

## 3. Public Packet Audit

Implementation:
- Generator: `src/public_packets.py`
- Template: `templates/public_packet/index.html.j2`
- CSS: `static/public_packet.css`
- Output root: `public_outreach/`

Findings:
- Static output: Pass. Generates `public_outreach/p/{token}/index.html` plus copied screenshots.
- Tokenized URLs: Pass. Uses `secrets.token_urlsafe(TOKEN_BYTES)` with `TOKEN_BYTES = 24`.
- Token reuse/rotation: Pass. Reuses stored token unless `--rotate-token`.
- Candidate gate: Pass. Requires `human_review_decision = APPROVED`, status in `APPROVED_FOR_OUTREACH` or `OUTREACH_DRAFTED`, and `audit_data_status = READY`.
- Noindex: Pass. Template includes `<meta name="robots" content="noindex,nofollow">`; generator writes `robots.txt` disallow all and `_headers` with `X-Robots-Tag`.
- Sanitization: Pass. Template shows business name, website URL, audit date, selected issues, screenshots, improvement map, and disclaimer. It does not show job logs, CRM notes, raw IDs as visible page content, or dashboard controls.
- Linkability: Pass in code. `artifact_url`/metadata stores `/p/{token}/`, and `/outbound` composes full URL using `PUBLIC_PACKET_BASE_URL`.

Current data blocker:
- No `public_packet` artifacts exist in the current DB. Public packets must be generated before any send-ready row can exist.

## 4. Contact Readiness Audit

Implementation:
- `src/contact_readiness.py`

Findings:
- No guessed emails: Pass. Sources are local audit/score data and existing contacts. No enrichment, LinkedIn, SMTP RCPT, or website fetch in this version.
- Candidate grading: Pass. Separates website business-domain direct, role email, free email, existing manual contact, unknown source, and invalid.
- Conservative first-batch posture: Pass. Unknown-source emails are lower priority; invalid/suppressed candidates are rejected.
- Existing manual contacts: Pass. Manual contacts are preserved and prioritized.
- Duplicate contacts: Pass. Upserts by prospect/email/contact key.

Current data blocker:
- `contacts = 0`.
- Latest `runs/latest/contact_readiness.csv` shows at least one processed prospect with `contact_ready=False`.
- No approved/drafted prospects currently have sendable contact rows.

## 5. Compliance Audit

| Item | Status | Evidence |
|---|---|---|
| Physical address | Blocked | Latest `email_infra_check.json` reports `PHYSICAL_MAILING_ADDRESS` FAIL. |
| Unsubscribe email/instruction | Blocked | Latest report has `UNSUBSCRIBE_EMAIL` FAIL; `config/outreach.yaml` has placeholders only. |
| Suppression list | Pass | Table exists; send path checks it before sending; inbox sync writes unsub/bounce suppressions. |
| List-Unsubscribe header | Pass when configured | Dashboard and CLI compose `List-Unsubscribe: <mailto:...>` if unsubscribe email exists. |
| No deceptive subject lines | Pass in current templates | Draft subject default is `Quick website audit for {business_name}`; no false claims observed. |
| No tracking pixels | Pass | Send page states no tracking; sender composes plain text only. |
| No click tracking | Pass | No click-tracking rewrite path found. |
| Screenshot attachments default off | Pass | `attach_screenshots_default: false`; dashboard requires user checkbox plus queue metadata approval. |

Compliance blocker: do not send until physical address, unsubscribe mailbox/instruction, from email/name, SMTP, DNS, and public packet base URL are configured and `email_infra_check` has no FAIL rows.

## 6. Deliverability Audit

Implementation:
- `src/email_infra_check.py`
- `docs/EMAIL_INFRA_SETUP.md`
- `src/inbox_sync.py`

Findings:
- SPF/DKIM/DMARC checker: Pass. DNS checks exist; DKIM requires selector config.
- SMTP test path: Pass. `email_infra_check --test-smtp` logs in without sending; `--send-test-to` sends one test only. Dashboard `/send/test` also sends one test only with checkbox.
- Daily cap: Pass. `config/outreach.yaml` has `daily_cap: 10`; dashboard real batch max is 10 with default 5.
- Batch limit: Pass. `/send/batch` default 5, max 10.
- Bounce/reply/unsubscribe capture: Pass. `src/inbox_sync.py` supports IMAP read-only sync or manual CSV import; apply mode updates suppression/CRM and cancels queued follow-ups. Auto-replies do not cancel follow-ups after the latest fix.
- Current infra posture: Not ready. Latest report has config/DNS FAIL rows and no SMTP test.

## 7. Docker / Server Audit

Implementation:
- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `docs/DOCKER_RUNBOOK.md`

Findings:
- Playwright support: Pass. Dockerfile uses `mcr.microsoft.com/playwright/python:v1.49.1-jammy` and `requirements.txt` pins `playwright==1.49.1`.
- Private binding: Pass. Compose binds `127.0.0.1:8787:8787`, not public `0.0.0.0`.
- Persistence: Pass. Volumes persist `data`, `runs`, `artifacts`, `screenshots`, `public_outreach`, and `config`.
- Secrets/data excluded from image: Pass. `.dockerignore` excludes `.env`, DBs, runs, artifacts, screenshots, and public output.
- Healthcheck: Pass. Compose checks `http://127.0.0.1:8787/health`.
- Public packets: Pass. Docs say deploy `public_outreach/` to static hosting, not Flask.

Docker verdict: ready for private local/VPS operation after normal environment configuration. Do not bind dashboard publicly without separate auth/proxy work.

## 8. First-Batch Recommendation

Recommendation: **Do not send real email yet.**

Why:
- No contacts exist.
- No public packet artifacts exist.
- No email draft artifacts exist.
- No queue rows exist.
- Latest email infrastructure report has FAIL rows.
- No current market/niche has a send-ready row.

Max batch size when ready:
- First live batch: **3 emails**.
- Do not use the dashboard max of 10 for the first send. Use 3, inspect inbox/replies/bounces, then move to 5.

Recommended market/niche:
- Start with `akron_oh / roofing` after completing review and prep.
- Reason: current DB has 12 `PENDING_REVIEW / HUMAN_REVIEW` Akron roofing leads, making it the most immediate review pool.
- Backup option: `mckinney_tx / roofing` has 6 pending review leads.

Required before first send:
1. Configure email infrastructure:
   - `SMTP_HOST`
   - `SMTP_PORT`
   - `SMTP_USERNAME`
   - `SMTP_PASSWORD`
   - `OUTREACH_FROM_EMAIL`
   - `OUTREACH_FROM_NAME`
   - `PHYSICAL_MAILING_ADDRESS` or config equivalent
   - `UNSUBSCRIBE_EMAIL`
   - `PUBLIC_PACKET_BASE_URL`
2. Configure DNS for the sending domain:
   - MX
   - SPF
   - DMARC
   - DKIM if selector is available
3. Run `python -m src.email_infra_check --domain <domain>` until there are no FAIL rows.
4. Review Akron roofing case pages and approve only genuinely good prospects.
5. Run contact readiness for the selected market/niche; verify only website-published/manual emails are used.
6. Generate outreach drafts.
7. Generate public packets and deploy `public_outreach/` to static hosting.
8. Confirm packet URLs open from `PUBLIC_PACKET_BASE_URL`.
9. Open `/outbound?market=akron_oh&niche=roofing`; create a Step 1 queue only for send-ready rows.
10. Open `/send`; confirm rows are sendable and no blockers remain.
11. Run one `/send/test` to your own address.
12. Send a real batch of 3 from `/send/batch`.
13. Run `python -m src.inbox_sync --dry-run` the next day; use manual CSV import if IMAP is not configured.

Do not use:
- `src.send_outreach --send` for the first batch. It is less strict than `/send` because it does not require queued rows or public packet readiness.

## Issues

### P0 - Must Fix Before Any Real Email

1. Email infrastructure is not ready.
   - Evidence: `runs/latest/email_infra_check.json` has FAIL rows for SMTP config, physical address, unsubscribe email, public packet base URL, and sending domain.
   - Fix: configure env/config and rerun `src.email_infra_check` until no FAIL rows.

2. No sendable prospect data exists.
   - Evidence: current DB has `contacts = 0`, `public_packet artifacts = 0`, `email_draft artifacts = 0`, and `outreach_queue = 0`.
   - Fix: run review, contact readiness, draft generation, public packet generation/deployment, then create queue from `/outbound`.

### P1 - Fix Before More Than 25 Emails

1. CLI sender is less strict than dashboard sender.
   - Path: `src/send_outreach.py`.
   - Risk: manual `--send` can bypass queue/public-packet readiness and has escape flags for missing address/unsubscribe.
   - Minimal fix: either align CLI with `/send` gates or document it as deprecated/operator-only dry-run tooling. For first batch, use `/send` only.

2. Inbox sync should be run operationally after first send.
   - Path: `src/inbox_sync.py`.
   - Code is present, but no real IMAP/manual reply data has been applied yet.
   - Minimal fix: configure IMAP or prepare `runs/latest/inbound_replies.csv` before any follow-up sequence.

### P2 - Cleanup

1. Add a timestamp to `email_infra_check.json`.
   - Current report has results and exit code but no generated-at field.
   - This is not a send blocker because `/send` still treats FAIL rows as blocking.

2. Consider a dashboard note that `send_outreach.py` is not the preferred Phase 2 send path.
   - This is operator clarity only; routes already keep sending isolated to `/send`.

## Final Verdict

Current state: **not ready to send**.

The architecture is close and the dashboard send gate is appropriately conservative, but the local data and infrastructure are not ready. The safe path is to finish one market/niche prep cycle, create queue rows from `/outbound`, then send a tiny first batch from `/send` only.
