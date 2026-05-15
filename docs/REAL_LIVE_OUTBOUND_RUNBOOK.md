# Real Live Outbound Runbook

Generated: 2026-05-12

Repo root inspected: `C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads`

Scope: code-specific operating instructions for the first real outbound batch for Trade Growth Studio. This document is based on actual repo code, scripts, config, docs, and read-only inspection of `data/leads.db`.

Safety of this runbook creation: no application code changed, no schema changed, no config changed, no external API calls, no Playwright audits, no deploys, no SMTP, no email sends, no SQLite mutations, and no file writes except this document.

## 1. Executive Summary

The repo currently implements a private local Flask + SQLite dashboard for lead collection, market management, pipeline jobs, website audits, scoring, case review, visual critique, contact readiness, public audit packets, outreach drafts, outbound readiness, controlled queue-based sending, inbox sync, and Docker/server readiness.

Use it now for a controlled first learning loop: produce 10-30 audited cases, manually approve only 5-15 strong prospects, generate contact-ready public-packet-linked drafts, queue only the best few, and send exactly 3 first emails after infrastructure passes.

Before first real outbound, configure SMTP, sender identity, physical mailing address, unsubscribe mailbox, DNS, `PUBLIC_PACKET_BASE_URL`, generated public packets, contact readiness, and approved email drafts.

Recommended first batch size: **3 real emails**, then wait 24 hours and monitor replies, bounces, spam placement, and unsubscribes.

Recommended first market/niche from current `data/leads.db`: **`akron_oh` / `roofing`** after finishing manual review/contact/public-packet/draft prep. It has audited pending-review leads. `mckinney_tx` / `roofing` is the backup. Current DB inspection found zero approved prospects and zero contacts, so no real send should happen yet.

Strict no-go conditions:

- Do not send if `runs/latest/email_infra_check.json` has any `FAIL` rows.
- Do not send if `contacts` has no verified website-published/manual email for the prospect.
- Do not send if `artifacts` has no `public_packet` artifact for the prospect.
- Do not send if no ready `email_draft` exists.
- Do not send if `human_review_decision != APPROVED`.
- Do not send if the email appears in `suppression_list`.
- Do not send if a step-1 `sent` event already exists for the same prospect/email/campaign.
- Do not send from `/run`; `/run` is pipeline-only.
- Do not use `src.send_outreach --send` for the first live batch; it exists, but the approved first-batch send path is `/outbound` -> `/send`.
- Do not expose the Flask dashboard publicly.
- Do not batch more than 10. For the first run, send exactly 3.

## 2. Current Repo Capability Inventory

| Feature | Implemented | Code path / route / command | Notes |
|---|---:|---|---|
| Local dashboard | Yes | `src/dashboard_app.py`, `GET /`, `scripts/start_dashboard.bat`, `scripts/start_dashboard.ps1` | Private local dashboard defaults to `127.0.0.1:8787`. |
| Run tab / pipeline controls | Yes | `GET /run`, `POST /jobs/start`, `POST /run/full-pipeline` | Whitelisted jobs only. No send job. |
| Market manager | Yes | `GET /markets`, `POST /markets/add`, `config/markets.yaml` | Adds markets and backs up `config/markets.yaml` with microsecond timestamp. |
| Job runner | Yes | `src/dashboard_jobs.py`, `dashboard_jobs` table, `GET /jobs` | One active job at a time; `shell=False`; logs in `runs/dashboard_jobs/`. |
| Places pull | Yes | `python -m src.places_pull`, job type `places_pull` | Calls Google Places Text Search. `--dry-run` still uses API quota but does not write prospects. |
| Eligibility | Yes | `python -m src.eligibility`, job type `eligibility` | Local DB only; writes qualification/status when not dry-run. |
| Fast/deep audit | Yes | `python -m src.audit_site`, job type `audit` | `--fast` skips PageSpeed by default, lighter crawl, screenshots. Deep is default. |
| Scoring | Yes | `python -m src.score_leads`, job type `score` | Uses stored local audit data and `config/scoring.yaml`. |
| Artifact generation | Yes | `python -m src.generate_artifacts`, job type `artifacts` | Writes `audit_card` and `homepage_preview` under `runs/latest/artifacts/`. Current code skips if PageSpeed scores are missing. |
| Case file review | Yes | `GET /case/<int:prospect_id>`, `POST /case/<int:prospect_id>/review` | Approve/reject/hold manual review. |
| Review tile quick delete | Yes | `POST /review/<int:prospect_id>/delete` | Marks `REJECTED_REVIEW`, removes from queue, preserves history. |
| Screenshot viewer | Yes | `templates/dashboard/case.html`, `GET /media/<path>` | Desktop/mobile panels with full-image links, fit/natural/zoom controls. |
| Visual critique | Yes | `POST /case/<int:prospect_id>/visual-review` | Saves `visual_review` audit row and email-safe claims. |
| CRM stages | Yes | `GET /crm`, `GET /crm/stage/<stage>`, `POST /case/<id>/stage` | Uses canonical stages in `src/state.py`. |
| Contact readiness | Yes | `python -m src.contact_readiness` | Reads local score/audit/contact data; does not fetch websites; writes `contacts` and prospect metadata when not dry-run. |
| Public audit packet generation | Yes | `python -m src.public_packets` | Generates static noindex packets under `public_outreach/p/<token>/`. |
| Public packet deployment scripts | Yes | `scripts/deploy_public_packets_cloudflare.ps1`, `.bat` | Deploys only `public_outreach/` via Wrangler Pages deploy. |
| Outreach draft generation | Yes | `python -m src.outreach_drafts`, case page `Generate Drafts` | Writes four `email_draft` artifacts under `runs/latest/outreach_drafts/`; no email send. |
| Email infrastructure checker | Yes | `python -m src.email_infra_check` | Checks config, MX/SPF/DMARC/DKIM if selector, optional SMTP login/test. |
| Outbound readiness dashboard | Yes | `GET /outbound`, `POST /outbound/queue` | Shows blockers and creates queue rows only. No sending. |
| Send queue | Yes | `outreach_queue` table, `POST /outbound/queue` | Partial unique index prevents non-cancelled duplicates for same prospect/email/campaign/step. |
| Sender page | Yes | `GET /send`, `POST /send/test`, `POST /send/batch` | Controlled real sending exists here only. Batch sends queued step-1 rows only. |
| CLI sender | Yes | `python -m src.send_outreach` | Has a real `--send` mode, but it is not approved for the first live batch because it does not require public packets/queue. Use dashboard `/outbound` + `/send` only. |
| Inbox/reply sync | Yes | `python -m src.inbox_sync` | IMAP read-only fetch or manual CSV; `--apply` updates CRM/suppression and cancels queued follow-ups. |
| Docker/server setup | Yes | `Dockerfile`, `docker-compose.yml`, `docs/DOCKER_RUNBOOK.md` | Compose binds host port to `127.0.0.1:8787`. |
| Showcase website | No | Not implemented in current repo | No WordPress/theme/showcase app files found in this repo. Fulfillment remains external. |

## 3. Local Environment Setup

Python version: README specifies Python 3.11. The current `.venv` exists and the repo command examples use Windows PowerShell.

Initial setup from repo root:

```powershell
cd "C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
python -m src.db
```

If `py -3.11` is unavailable, use the installed Python 3.11 executable. If the generic `python` command is not on PATH, use:

```powershell
.\.venv\Scripts\python.exe -m src.db
```

Dependencies in `requirements.txt`:

- `python-dotenv==1.0.1`
- `PyYAML==6.0.2`
- `requests==2.32.3`
- `beautifulsoup4==4.12.3`
- `playwright==1.49.1`
- `Jinja2>=3.1.4,<4.0`
- `Flask>=3.0.0,<4.0`
- `dnspython>=2.6.1,<3.0`

Playwright setup:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Node/npm setup is only needed for Cloudflare Wrangler deployment:

```powershell
npm install -g wrangler
wrangler login
```

LocalWP/WordPress relevance: not implemented in current repo. No WordPress theme or LocalWP app path was found in `ai-local-site-leads`.

Docker setup exists:

```powershell
docker compose build
docker compose up
```

## 4. Environment Variables and Config Checklist

Source files inspected: `.env.example`, `config/outreach.yaml`, `src/config.py`, `src/places_pull.py`, `src/pagespeed.py`, `src/audit_site.py`, `src/email_infra_check.py`, `src/send_outreach.py`, `src/dashboard_app.py`, `src/inbox_sync.py`, and deploy scripts.

Do not paste real secrets into docs or tickets. The examples below are formats only.

| Variable/config key | Required before first live run? | Used by | Example value format | Notes | Safe to leave blank? |
|---|---:|---|---|---|---:|
| `DATABASE_PATH` | Yes | `src/config.py`, dashboard, all CLI commands | `data/leads.db` | Defaults to `data/leads.db` if unset. | Yes if default is intended |
| `LOG_LEVEL` | No | `src/cli_utils.py` | `INFO` | Controls CLI JSON log level. | Yes |
| `GOOGLE_MAPS_API_KEY` | Yes for Places | `src.places_pull` | API key string | Required for `places_pull`; Places preview still uses quota. | Yes if not pulling Places |
| `PAGESPEED_API_KEY` | Optional for deep audit | `src.pagespeed` | API key string | PageSpeed can run without a key, but quota/ownership is clearer with one. | Yes |
| `ARTIFACT_BASE_URL` | No | `src.generate_artifacts` | `https://example.com/private-drafts` | Only populates `artifacts.artifact_url` for local generated artifacts. | Yes |
| `USER_AGENT` | No | `src.audit_site` | `ai-local-site-leads/0.1 (+local batch pipeline)` | Polite crawler identity. | Yes |
| `SMTP_HOST` | Yes before send | `src.email_infra_check`, `/send`, `src.send_outreach` | `smtp.example.com` | Required for SMTP test/send. | No for live send |
| `SMTP_PORT` | Yes before send | same | `587` or `465` | `.env.example` defaults format to `587`; checker requires valid port. | No for live send |
| `SMTP_USERNAME` | Yes before send | same | mailbox login | Required by checker and dashboard send config. | No for live send |
| `SMTP_PASSWORD` | Yes before send | same | app password/token | Required by checker and dashboard send config. | No for live send |
| `SMTP_STARTTLS` | Optional | `/send`, `send_outreach`, `email_infra_check` | `true` | Defaults to true except port `465`. | Yes |
| `OUTREACH_SMTP_STARTTLS` | Optional | same | `true` | Alias for SMTP STARTTLS. | Yes |
| `OUTREACH_FROM_EMAIL` | Yes before send | `email_infra_check`, `/send`, `send_outreach` | `hello@example.com` | Used as sender and to infer DNS domain. | No for live send |
| `OUTREACH_FROM_NAME` | Yes before send | same | `Trade Growth Studio` | Defaults in config to `Local Growth Audit`; change before live sending. | No for live send |
| `OUTREACH_BUSINESS_NAME` | Yes before dashboard batch send | `/send`, `send_outreach` | `Trade Growth Studio` | Dashboard batch send requires sender business name. | No for live send |
| `OUTREACH_PHYSICAL_ADDRESS` | Yes before send | `/send`, `send_outreach`, `email_infra_check` | real mailing address | Present in `.env.example`; accepted as an alias for physical address. | No for live send |
| `PHYSICAL_MAILING_ADDRESS` | Yes before send | `/send`, `send_outreach`, `email_infra_check` | real mailing address | Preferred by newer docs/checker; not currently shown in `.env.example`. | No for live send |
| `OUTREACH_UNSUBSCRIBE_EMAIL` | Yes before send | `/send`, `send_outreach`, `email_infra_check` | `unsubscribe@example.com` | Present in `.env.example`; accepted as an alias for unsubscribe mailbox. | No for live send |
| `UNSUBSCRIBE_EMAIL` | Yes before send | `/send`, `send_outreach`, `email_infra_check` | `unsubscribe@example.com` | Preferred by newer docs/checker; not currently shown in `.env.example`. | No for live send |
| `PUBLIC_PACKET_BASE_URL` | Yes before queue/send | `email_infra_check`, `/outbound`, `/send`, deploy script | `https://audit.example.com` | Used to compose `/p/<token>/` packet URLs. | No for live send |
| `PUBLIC_PACKET_PAGES_PROJECT` | Yes for Cloudflare deploy script | `scripts/deploy_public_packets_cloudflare.ps1` | `trade-growth-audits` | Cloudflare Pages project name. | Yes if deploying manually |
| `DKIM_SELECTOR` | Recommended | `email_infra_check` | `selector1` | Enables generic DKIM TXT check at `<selector>._domainkey.<domain>`. | Yes, but DKIM check becomes WARN |
| `IMAP_HOST` | Needed for IMAP sync | `src.inbox_sync` | `imap.example.com` | If absent, inbox sync uses manual CSV fallback. | Yes if manual import |
| `IMAP_PORT` | Needed for IMAP sync if non-default | `src.inbox_sync` | `993` | Defaults to `993` with SSL or `143` without SSL. | Yes |
| `IMAP_USERNAME` | Needed for IMAP sync | `src.inbox_sync` | mailbox login | Required for IMAP mode. | Yes if manual import |
| `IMAP_PASSWORD` | Needed for IMAP sync | `src.inbox_sync` | app password/token | Required for IMAP mode. | Yes if manual import |
| `IMAP_USE_SSL` | Optional | `src.inbox_sync` | `true` | Defaults true. | Yes |
| `config/outreach.yaml:defaults.daily_cap` | Yes for send cap | `/send`, `send_outreach` | `10` | Current value is `10`. First batch should send exactly 3. | No for live send |
| `config/outreach.yaml:defaults.max_emails_per_run` | Partial legacy | `send_outreach`, `/send` fallback | `25` | Used as fallback if `daily_cap` missing. | Yes if `daily_cap` set |
| `config/outreach.yaml:defaults.physical_address` | Yes if env absent | `/send`, `send_outreach`, `email_infra_check` | real mailing address | Current file has blank value. | No for live send |
| `config/outreach.yaml:defaults.unsubscribe_email` | Yes if env absent | same | `unsubscribe@example.com` | Current file has blank value. | No for live send |
| `config/outreach.yaml:defaults.unsubscribe_instruction` | Recommended | `/send` footer | `To opt out, reply...` | Auto-generated if unsubscribe email exists. | Yes if email set |
| `config/outreach.yaml:defaults.attach_screenshots_default` | No | `/send`, `send_outreach` | `false` | Current value is `false`; keep it false. | Yes |
| `config/markets.yaml` | Yes for markets | `/markets`, `places_pull`, filters | YAML mapping | Current keys: `mckinney_tx`, `cincinnati`, `dayton`, `columbus_oh`, `akron_oh`. | No for Places |
| `config/niches.yaml` | Yes for niches | `/run`, `places_pull` | YAML mapping | Current keys: `roofing`, `hvac`, `plumbing`, `electrical`, `garage_doors`. | No for Places |

Dashboard host/port:

- `src.dashboard_app` defaults: `--host 127.0.0.1`, `--port 8787`.
- Docker runs Flask inside container on `0.0.0.0:8787`, but compose binds host `127.0.0.1:8787:8787`.

## 5. Domain, DNS, and Mailbox Setup

This is operational guidance, not legal advice. Verify compliance requirements independently. Use the FTC CAN-SPAM Act business compliance guide, Google Email Sender Guidelines, and your SMTP provider acceptable-use policy as external references. Passing the repo checker is not a legal compliance guarantee and does not guarantee inbox placement.

Required real-world setup before live sending:

1. Choose a dedicated outbound domain or mailbox. Prefer something like `hello@your-outbound-domain.com` or `ross@audit.yourdomain.com`, not a free personal Gmail account.
2. Set up a mailbox/SMTP provider that allows low-volume B2B outbound under its terms.
3. Configure an MX record so the sending domain can receive replies and unsubscribes.
4. Configure SPF TXT for the SMTP provider.
5. Configure DKIM using the selector provided by the SMTP provider.
6. Configure DMARC at `_dmarc.<sending-domain>`.
7. Create and monitor an unsubscribe mailbox, for example `unsubscribe@your-outbound-domain.com`.
8. Configure a valid physical mailing address in `.env` or `config/outreach.yaml`.
9. Add required values to `.env`:

```env
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
OUTREACH_FROM_EMAIL=
OUTREACH_FROM_NAME=Trade Growth Studio
OUTREACH_BUSINESS_NAME=Trade Growth Studio
PHYSICAL_MAILING_ADDRESS=
UNSUBSCRIBE_EMAIL=
PUBLIC_PACKET_BASE_URL=https://audit.example.com
DKIM_SELECTOR=
```

10. Run the checker:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --domain example.com
```

11. Test SMTP login without sending:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --test-smtp
```

12. Send one infrastructure test only to yourself:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --send-test-to you@example.com
```

Must pass before live sending:

- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`
- `OUTREACH_FROM_EMAIL`, `OUTREACH_FROM_NAME`
- physical mailing address
- unsubscribe email
- `PUBLIC_PACKET_BASE_URL`
- MX, SPF, DMARC
- SMTP login/test to self

Acceptable warnings for a tiny first batch:

- DKIM generic check may be `WARN` if `DKIM_SELECTOR` is not configured but the provider confirms DKIM is active. Better: set `DKIM_SELECTOR` and make it pass.
- SMTP connection may be untested only before you are ready. It is not acceptable for live send.

Hard no-go failures:

- No SMTP config.
- No valid sender email.
- No physical mailing address.
- No unsubscribe mailbox/instruction.
- No public packet base URL.
- DNS domain missing MX/SPF/DMARC.
- SMTP test send to yourself fails or lands in spam and you have not diagnosed why.

## 6. Public Audit Packet Hosting Setup

Implemented by `src/public_packets.py`, `templates/public_packet/index.html.j2`, `static/public_packet.css`, `scripts/deploy_public_packets_cloudflare.ps1`, and `scripts/deploy_public_packets_cloudflare.bat`.

Generate public packets:

```powershell
.\.venv\Scripts\python.exe -m src.public_packets --market akron_oh --niche roofing --limit 5
```

Generate one packet:

```powershell
.\.venv\Scripts\python.exe -m src.public_packets --prospect-id 123
```

Dry-run:

```powershell
.\.venv\Scripts\python.exe -m src.public_packets --market akron_oh --niche roofing --limit 5 --dry-run
```

Candidate requirements in code:

- `human_review_decision = 'APPROVED'`
- `status IN ('APPROVED_FOR_OUTREACH', 'OUTREACH_DRAFTED')`
- `audit_data_status = 'READY'`

Output folder:

```text
public_outreach/
  robots.txt
  _headers
  assets/public_packet.css
  p/<token>/index.html
  p/<token>/desktop.png
  p/<token>/mobile.png
```

Artifact row:

- `artifact_type = 'public_packet'`
- `artifact_key = '<prospect_id>:public_packet'`
- `artifact_url = '/p/<token>/'`
- metadata includes `token`, `relative_url`, `selected_issues`, screenshot filenames.

Token behavior:

- `TOKEN_BYTES = 24`
- `secrets.token_urlsafe(24)`
- Existing token is reused unless `--rotate-token` is passed.

Noindex/sanitization:

- HTML includes `<meta name="robots" content="noindex,nofollow">`.
- `public_outreach/robots.txt` disallows all.
- `public_outreach/_headers` includes `X-Robots-Tag: noindex, nofollow`.
- Template includes disclaimer: "Private website audit draft for discussion. This page is not affiliated with or published by {business_name}."
- It does not include dashboard controls, CRM notes, job logs, or visible SQLite IDs.
- Noindex is a crawler instruction, not access control. Treat each `/p/<token>/` URL as a bearer link and send it only to the intended recipient.

Deploy to Cloudflare Pages:

```powershell
.\scripts\deploy_public_packets_cloudflare.ps1
```

or:

```bat
scripts\deploy_public_packets_cloudflare.bat
```

The PowerShell script checks:

- `public_outreach/` exists.
- `PUBLIC_PACKET_PAGES_PROJECT` exists in env, `.env`, or `config/outreach.yaml`.
- `wrangler` is installed.

Then it runs:

```powershell
wrangler pages deploy public_outreach --project-name <configured project>
```

Config needed:

```env
PUBLIC_PACKET_PAGES_PROJECT=your-cloudflare-pages-project
PUBLIC_PACKET_BASE_URL=https://audit.example.com
```

Verify a packet URL:

```text
https://audit.example.com/p/<token>/
```

Avoid exposing Flask:

- Deploy only `public_outreach/`.
- Do not upload `.env`, `data/`, `runs/`, `screenshots/`, `templates/`, or the repo root.
- Use Cloudflare Pages Direct Upload/static folder deploy or Wrangler Pages Deploy docs as the external reference.

Current repo state: `public_outreach/` does not exist yet, so no packets are currently generated in the inspected working tree.

## 7. Dashboard Startup

Simple Windows launcher:

```bat
scripts\start_dashboard.bat
```

PowerShell launcher:

```powershell
.\scripts\start_dashboard.ps1
```

Direct command:

```powershell
.\.venv\Scripts\python.exe -m src.dashboard_app --host 127.0.0.1 --port 8787
```

Open:

```text
http://127.0.0.1:8787
```

Health check:

```text
http://127.0.0.1:8787/health
```

Expected health response:

```text
OK
```

Files/folders that should exist:

- `.venv/`
- `.env`
- `data/leads.db`
- `config/markets.yaml`
- `config/niches.yaml`
- `config/outreach.yaml`
- `runs/`
- `screenshots/`

If dashboard fails to start:

- Confirm you are in `C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads`.
- Confirm `.venv\Scripts\python.exe` exists.
- Run `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.
- Run `.\.venv\Scripts\python.exe -m src.db`.
- If port 8787 is occupied, stop the old terminal or run a temporary alternate port:

```powershell
.\.venv\Scripts\python.exe -m src.dashboard_app --host 127.0.0.1 --port 8799
```

Docker start:

```powershell
docker compose build
docker compose up
```

Docker URL is also:

```text
http://127.0.0.1:8787
```

## 8. First Live Market/Niche Selection

Configured markets in `config/markets.yaml`:

| Key | Label | State | Cities source |
|---|---|---|---|
| `mckinney_tx` | `McKinney-Frisco-Prosper/Celina, TX` | `TX` | `included_cities` |
| `cincinnati` | `Cincinnati, OH` | `OH` | `cities` |
| `dayton` | `Dayton, OH` | `OH` | `cities` |
| `columbus_oh` | `Columbus` | `OH` | `included_cities` |
| `akron_oh` | `Akron` | `OH` | `included_cities` |

Configured niches in `config/niches.yaml`:

| Key | Label | Current search terms |
|---|---|---|
| `roofing` | Roofing | `roof repair`, `roof replacement` |
| `hvac` | HVAC | `hvac contractor`, `air conditioning repair`, `furnace repair` |
| `plumbing` | Plumbing | `plumber`, `emergency plumber` |
| `electrical` | Electrical | `electrician`, `electrical repair` |
| `garage_doors` | Garage Doors | `garage door repair`, `garage door installation` |

Current read-only DB snapshot from `data/leads.db`:

- `prospects`: 245
- `contacts`: 0
- `artifacts`: 112
- `website_audits`: 242
- `outreach_queue`: 0
- `outreach_events`: 2
- `suppression_list`: 0
- `dashboard_jobs`: 20
- `public_outreach/`: not present
- approved prospects found by DB query: 0

Current lead counts by notable market/niche/status:

| Market/niche | Current useful state |
|---|---|
| `akron_oh` / `roofing` | 12 `PENDING_REVIEW` / `HUMAN_REVIEW`, 5 audited but `DISCARD`, 1 `NEEDS_SCREENSHOTS`, 1 `AUDIT_FAILED`, 1 ineligible. |
| `cincinnati` / `roofing` | 15 `PENDING_REVIEW` / `HUMAN_REVIEW`, 6 audited but `DISCARD`, 1 `AUDIT_FAILED`, 1 ineligible. |
| `columbus_oh` / `roofing` | 20 `ELIGIBLE_FOR_AUDIT` / `RUN_AUDIT`, `qualification_status = QUALIFIED`, `audit_data_status = PENDING`. |
| `mckinney_tx` / `roofing` | 5 `PENDING_REVIEW`, 36 `ELIGIBLE_FOR_AUDIT`, 2 rejected, 2 ineligible. |
| `mckinney_tx` / `hvac` | 33 `ELIGIBLE_FOR_AUDIT`, 4 ineligible. |
| `mckinney_tx` / `plumbing` | 34 `ELIGIBLE_FOR_AUDIT`, 4 ineligible. |
| `mckinney_tx` / `electrical` | 33 `ELIGIBLE_FOR_AUDIT`, 2 ineligible. |
| `mckinney_tx` / `garage_doors` | 26 `ELIGIBLE_FOR_AUDIT`, 1 ineligible. |

Recommendation:

- First run should use existing audited review queue before pulling more Places.
- Start with `akron_oh` / `roofing` because it has current pending-review audited leads and recent audit activity.
- Backup: `mckinney_tx` / `roofing`.
- Avoid pulling more Places until existing pending-review leads are processed.

Places request-budget warning:

- `places_pull` loops every included city and every search term until `--limit` is reached.
- Each city/search-term pair makes one Google Places Text Search request.
- `--limit` controls processed places, not the total possible number of city/search-term combinations.
- Dashboard text correctly says Places preview/dry-run still calls Google API quota.

Recommended first limits:

- Places limit: 10-20 max for a test; avoid if existing leads are enough.
- Max Places requests: keep to one niche and one market.
- Audit limit: 5-10 fast audits, then inspect. Dashboard max is 50.
- Artifact limit: 10-25.
- Send limit: 3 first live emails; absolute dashboard max is 10.

## 9. Lead Generation and Pipeline Run Sequence

### 9.1 Add/select market

Dashboard route:

```text
GET /markets
POST /markets/add
```

Required fields:

- `label`
- `state` as two uppercase letters
- `included_cities` as one city per line or comma-separated

Optional fields:

- `market_key`
- `priority`
- `notes`

Validation in `src/dashboard_app.py`:

- `market_key` generated from label/state if blank.
- `market_key` must match lowercase letters, numbers, underscores.
- Duplicate key rejected.
- At least one city required.
- Backup is written before saving: `config/markets.yaml.bak.<timestamp-with-microseconds>`.

What not to do:

- Do not add markets expecting Places to run automatically. Adding a market only edits YAML.
- Do not use punctuation or spaces in manual `market_key`.

### 9.2 Pull Places leads

Dashboard route:

```text
GET /run
POST /jobs/start
```

Job type:

```text
places_pull
```

CLI:

```powershell
.\.venv\Scripts\python.exe -m src.places_pull --limit 10 --market akron_oh --niche roofing
```

Dry-run/preview:

```powershell
.\.venv\Scripts\python.exe -m src.places_pull --dry-run --limit 10 --market akron_oh --niche roofing
```

Important: `--dry-run` still calls Google Places API because the command must fetch data before deciding what it would insert/update.

Required env:

```env
GOOGLE_MAPS_API_KEY=
```

Recommended first live values:

- Market: one configured market only.
- Niche: one niche only.
- Limit: 10-20.
- Use live non-dry-run only when you actually want DB inserts/updates.

### 9.3 Run eligibility

Dashboard:

```text
GET /run -> Run Eligibility
```

Job type:

```text
eligibility
```

CLI:

```powershell
.\.venv\Scripts\python.exe -m src.eligibility --limit 100 --market akron_oh --niche roofing
```

Expected state changes:

- Qualified leads become `qualification_status = QUALIFIED`.
- Qualified leads generally move to `status = ELIGIBLE_FOR_AUDIT`, `next_action = RUN_AUDIT`.
- Disqualified leads move toward `INELIGIBLE` / `DISCARD`.

Verify:

- Dashboard `/run` recommended-action panel.
- Dashboard `/leads?market=akron_oh`.
- CLI dry-run first if unsure.

### 9.4 Run audit

Dashboard:

```text
GET /run -> Run Audits
```

Job type:

```text
audit
```

Fast audit CLI:

```powershell
.\.venv\Scripts\python.exe -m src.audit_site --fast --skip-pagespeed --limit 5 --market akron_oh --niche roofing
```

Deep audit CLI:

```powershell
.\.venv\Scripts\python.exe -m src.audit_site --limit 5 --market akron_oh --niche roofing
```

Fast mode behavior:

- `--fast` implies no PageSpeed unless `--include-pagespeed`.
- Default fast crawl pages: 2.
- Default fast page timeout: 5000 ms.
- Default fast screenshot timeout: 8000 ms.
- Captures desktop/mobile screenshots.
- Stores `audit_mode = "fast"` in audit findings/log summary.

Deep mode behavior:

- Default crawl pages: 5.
- Runs screenshots and PageSpeed unless `--skip-pagespeed`.

Selection safety in `src.audit_site._select_audit_prospects`:

- Batch selection requires `qualification_status = 'QUALIFIED'`.
- Excludes `INELIGIBLE`, `DISQUALIFIED`, `DISCARDED`, `REJECTED`, `REJECTED_REVIEW`, `CLOSED_LOST`, `CLOSED_WON`, `PROJECT_ACTIVE`, `PROJECT_COMPLETE`.
- Excludes `next_action IN ('DISCARD', 'DISQUALIFIED')`.
- Requires nonblank `website_url`.
- Skips `audit_data_status = READY` unless `--force` is passed.
- Dashboard does not pass `--force`.

Recommended first mode:

- Use Fast Audit for batch triage.
- Use Deep Audit only for high-priority cases where PageSpeed evidence matters.

Expected artifacts/data:

- `website_audits` rows for `site`, `screenshots`, optionally `pagespeed_mobile`, `pagespeed_desktop`.
- `artifacts` rows `screenshot_desktop`, `screenshot_mobile`.
- Image files under `screenshots/desktop/<prospect_id>.png` and `screenshots/mobile/<prospect_id>.png`.

Operational caveat:

- `src.generate_artifacts` currently skips prospects with missing PageSpeed scores. If you rely only on fast audit with `--skip-pagespeed`, review queue and scoring may still work, but local `audit_card` artifact generation can skip with `missing_pagespeed_scores`. For first outbound public packets, `src.public_packets` can still use stored site/visual/score evidence if candidate gates are met.

### 9.5 Run scoring

Dashboard:

```text
GET /run -> Run Scoring
```

Job type:

```text
score
```

CLI:

```powershell
.\.venv\Scripts\python.exe -m src.score_leads --limit 25 --market akron_oh --niche roofing
```

Expected state changes:

- Updates score columns on `prospects`.
- Writes `website_audits` row with `audit_type = 'lead_score'`.
- Exports `runs/latest/top_leads.csv`.
- Leads with `audit_data_status = READY` and review-worthy next action enter manual review flow.

### 9.6 Generate artifacts

Dashboard:

```text
GET /run -> Generate Artifacts
```

Job type:

```text
artifacts
```

CLI:

```powershell
.\.venv\Scripts\python.exe -m src.generate_artifacts --score-min 0 --limit 10 --market akron_oh --niche roofing
```

Expected outputs:

- `runs/latest/artifacts/<prospect_id>/audit_card.html`
- `runs/latest/artifacts/<prospect_id>/preview_homepage.html` when applicable
- `runs/latest/artifact_summary.csv`
- `artifacts` rows with `artifact_type = 'audit_card'` and `homepage_preview`

Current code caveat:

- `_artifact_block_reason` returns `missing_pagespeed_scores` when mobile or desktop PageSpeed scores are missing.
- If using fast audit only, local artifact generation may skip. Do not confuse this with public packet generation, which is separate.

### 9.7 Reconcile statuses

Dashboard:

```text
GET /run -> Reconcile Statuses
```

Job type:

```text
reconcile_statuses
```

Dry-run:

```powershell
.\.venv\Scripts\python.exe -m src.reconcile_statuses --dry-run --limit 100
```

Apply:

```powershell
.\.venv\Scripts\python.exe -m src.reconcile_statuses --apply --limit 100
```

When to run:

- After scoring/artifact generation if stages look inconsistent.
- Before outbound readiness if rows appear stuck in the wrong stage.

Expected effect:

- Normalizes `status` and `next_action` based on canonical mappings in `src/state.py`.

### 9.8 Open review queue

Route:

```text
GET /review
GET /review?market=akron_oh
```

Use the market filter. Count should increase after scoring/reconciliation creates `PENDING_REVIEW` / `HUMAN_REVIEW` rows.

The review tile now includes:

- Desktop/mobile thumbnails.
- Opportunity score.
- Pain score.
- Audit mode badge.
- Red `Delete` button for obvious good/no-fit sites.
- Green `Open Case` button.

## 10. Manual Review Procedure

Open:

```text
GET /review?market=akron_oh
```

Fast tile triage:

- If the thumbnail shows a polished, modern, clear site with strong mobile presentation, click `Delete` on the review card.
- This calls `POST /review/<prospect_id>/delete`, marks the lead rejected, and removes it from queue without physically deleting history.

Case review:

1. Click `Open Case`.
2. Use `Open Website` to inspect the current site in a new tab.
3. Use the case screenshot panels:
   - `Open Full Image`
   - `Fit Width`
   - `Natural Size`
   - 50/75/100/125/150 percent zoom
4. Check `Score Reasons`.
5. Check `Contact Signals`.
6. Check `Business`, `Places Signals`, and `Scores`.
7. Use `Jump to Visual Critique` and `Back to screenshots`.
8. Save visual critique before approval when possible.

Visual critique categories available in the case form:

- Mobile Layout
- Hero Section
- CTA Clarity
- Header Navigation
- Visual Clutter
- Readability
- Design Age
- Form Or Booking Path
- Service Clarity
- Trust Signals
- Content Depth
- SEO Structure
- Performance Perception
- Layout Consistency
- Conversion Path

Approval rubric. Approve only if:

- Business looks viable.
- Existing site pain is visible from screenshot/audit.
- Website exists and is not obviously broken data.
- Evidence is specific enough for a non-generic email.
- Contact email is available or likely contact-ready from website-published data.
- A public packet can be generated.
- Outreach would feel grounded, not generic.

Reject if:

- Site is already excellent.
- Franchise/corporate/large chain.
- No usable contact route and no safe email source.
- No real visible website pain.
- Business looks inactive.
- Bad data/wrong niche/wrong market.
- Prospect already contacted, closed, discarded, or suppressed.

Hold if:

- Evidence is ambiguous.
- Screenshots failed.
- You need a deep audit/PageSpeed pass.
- Contact readiness is unclear but the business might be worth revisiting.

Manual review form actions:

- `Approve for Outreach` sets `human_review_status = APPROVED`, `human_review_decision = APPROVED`, `status = APPROVED_FOR_OUTREACH`, `next_action = APPROVED_FOR_OUTREACH`.
- `Reject / Discard` sets review rejected and `status = REJECTED_REVIEW`.
- `Hold for Later` keeps `PENDING_REVIEW` / `HUMAN_REVIEW`.

Contact selection:

- The case page can save a primary email in the manual review form.
- The Contacts panel can save a primary contact through `POST /case/<id>/contact`.
- Use only website-published or manually verified business emails for the first batch. Do not guess owner emails.

## 11. Contact Readiness Procedure

Implemented in `src/contact_readiness.py`.

Dry-run for market/niche:

```powershell
.\.venv\Scripts\python.exe -m src.contact_readiness --dry-run --market akron_oh --niche roofing --limit 100
```

Apply for market/niche:

```powershell
.\.venv\Scripts\python.exe -m src.contact_readiness --market akron_oh --niche roofing --limit 100
```

One prospect:

```powershell
.\.venv\Scripts\python.exe -m src.contact_readiness --prospect-id 123
```

What it reads:

- `prospects.score_explanation_json.signals.email_candidates`
- `prospects.score_explanation_json.signals.business_domain_emails`
- `website_audits.findings_json` keys: `mailto_emails`, `visible_emails`, `email_candidates`, `business_domain_emails`
- existing `contacts` rows
- `suppression_list`

What it does not do:

- It does not fetch websites.
- It does not use paid enrichment APIs.
- It does not guess emails.
- It does not do SMTP RCPT verification.

What it writes when not dry-run:

- Upserts `contacts` rows.
- Preserves manual contacts with `source = 'dashboard_manual'`.
- Adds `contacts.metadata_json.contact_readiness`.
- Updates `prospects.metadata_json.contact_readiness`.
- Writes `runs/latest/contact_readiness.csv`.

Email categories:

- `existing_manual_contact`
- `website_business_domain_direct`
- `website_role_email`
- `website_free_email`
- `unknown_source`
- `rejected_invalid`

Conservative first-batch rule:

- Use `existing_manual_contact`, `website_business_domain_direct`, or `website_role_email`.
- Free emails found on site can be acceptable only if clearly published by the business.
- Do not use `unknown_source` for the first batch.

Current DB state:

- `contacts = 0`
- `runs/latest/contact_readiness.csv` shows no ready email in the inspected sample.
- Run contact readiness only after approvals exist.

## 12. Public Packet Procedure

Implemented in `src/public_packets.py`.

Dry-run:

```powershell
.\.venv\Scripts\python.exe -m src.public_packets --dry-run --market akron_oh --niche roofing --limit 5
```

Generate:

```powershell
.\.venv\Scripts\python.exe -m src.public_packets --market akron_oh --niche roofing --limit 5
```

One prospect:

```powershell
.\.venv\Scripts\python.exe -m src.public_packets --prospect-id 123
```

Required candidate status:

- `human_review_decision = APPROVED`
- `status` is `APPROVED_FOR_OUTREACH` or `OUTREACH_DRAFTED`
- `audit_data_status = READY`

Expected output:

- `public_outreach/p/<token>/index.html`
- `public_outreach/p/<token>/desktop.png` if screenshot exists
- `public_outreach/p/<token>/mobile.png` if screenshot exists
- `public_outreach/assets/public_packet.css`
- `public_outreach/robots.txt`
- `public_outreach/_headers`

Inspect locally:

- Open `public_outreach/p/<token>/index.html` in a browser.
- Confirm disclaimer is present.
- Confirm no CRM notes, job logs, dashboard controls, or raw IDs are visible.
- Confirm screenshots are correct.

Deploy:

```powershell
.\scripts\deploy_public_packets_cloudflare.ps1
```

Confirm URL:

```text
PUBLIC_PACKET_BASE_URL + /p/<token>/
```

If public packets are missing, do not send. The first email should link to a packet rather than attach screenshots.

## 13. Outreach Draft Procedure

Implemented in `src/outreach_drafts.py` and case page `POST /case/<id>/outreach-drafts`.

Generate from case page:

```text
GET /case/<prospect_id> -> Outreach Drafts -> Generate Drafts
```

CLI dry-run:

```powershell
.\.venv\Scripts\python.exe -m src.outreach_drafts --dry-run --market akron_oh --niche roofing --limit 5
```

CLI generate:

```powershell
.\.venv\Scripts\python.exe -m src.outreach_drafts --market akron_oh --niche roofing --limit 5
```

One prospect:

```powershell
.\.venv\Scripts\python.exe -m src.outreach_drafts --prospect-id 123
```

Inputs:

- Approved prospects only.
- `next_action = APPROVED_FOR_OUTREACH` for batch mode.
- Contact email unless `--include-missing-email` is passed. Do not use `--include-missing-email` for first batch.
- Stored site audit, PageSpeed/fallback where available, visual review, and lead score reasons.
- `templates/outreach/email_1.txt.j2` through `email_4.txt.j2`.

Outputs:

- Text files under `runs/latest/outreach_drafts/`.
- `artifacts` rows with `artifact_type = 'email_draft'`, `artifact_key = '<prospect_id>:email_<step>'`, `status = 'ready'`.
- After generation, prospect becomes `status = OUTREACH_DRAFTED`, `next_action = SEND_OUTREACH`.

Displayed in dashboard:

- Case page `Outreach Drafts` panel shows subject, recipient, body, and text file link.

Copy rules:

- Do not position as "AI websites."
- Position as website replacement, audit-backed redesign, mobile-first call/request path, service-page/service-area architecture, conversion infrastructure, public audit artifact, tracking-ready managed web operations.
- Do not claim guaranteed rankings, guaranteed lead volume, licensing, insurance, warranties, years in business, or affiliation unless independently verified.
- Top 3-5 issues in email; full evidence in packet.

Preferred first email structure:

```text
Subject: Website audit draft for {business_name}

Hi {contact_name or "there"},

I'm {sender_name}. I reviewed the public website for {business_name} and pulled together a private audit draft.

A few items stood out:
- {specific issue 1}
- {specific issue 2}
- {specific issue 3}

I kept the full issue list here so this first note stays readable:
{public_packet_url}

Would you want a short walkthrough of the most fixable items?

{opt-out line}

-- {sender_name}
{business name}
{physical mailing address}
```

Do not list every issue in the email body.

## 14. Outbound Readiness Gate

Implemented route:

```text
GET /outbound
POST /outbound/queue
```

Use:

```text
GET /outbound?market=akron_oh&niche=roofing
```

The page reads latest:

```text
runs/latest/email_infra_check.json
```

Required gates in code:

- Approved: `human_review_decision = APPROVED` and status in `APPROVED_FOR_OUTREACH` or `OUTREACH_DRAFTED`.
- Contact ready: selected contact email exists.
- Public packet ready: `public_packet` artifact exists, `status = ready`, URL can be composed.
- Draft ready: step-1 `email_draft` artifact exists, `status = ready`, file exists, subject/body exist.
- Not suppressed: email not active in `suppression_list`.
- Not already sent: no sent `outreach_events` row for same prospect/email/campaign/step.
- Not already queued: no active `outreach_queue` row for same prospect/email/campaign/step.
- Infra ready: latest email infra check has zero FAIL rows.

Create queue:

```text
GET /outbound?market=akron_oh&niche=roofing -> Create Step 1 Send Queue
```

Default queue size:

- `OUTBOUND_DEFAULT_QUEUE_LIMIT = 10`
- `OUTBOUND_MAX_QUEUE_LIMIT = 100`

Queue details:

- Table: `outreach_queue`
- Campaign: `intro_email`
- Step: `1`
- Status: `queued`
- Duplicate protection: unique index `idx_outreach_queue_active_unique` on `(prospect_id, email, campaign, step)` where `status <> 'cancelled'`.

No email is sent from `/outbound`. It only queues.

## 15. Email Infrastructure Check

Implemented:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check
```

Specific domain:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --domain example.com
```

SMTP login test without sending:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --test-smtp
```

One test email to yourself:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --send-test-to you@example.com
```

Output files:

```text
runs/latest/email_infra_check.json
runs/latest/email_infra_check.txt
```

Exit codes:

- `0`: pass or warnings only.
- `1`: required config missing or DNS critical failure.
- `2`: SMTP test/send requested and failed.

Current latest report in inspected repo:

- `exit_code = 1`
- FAIL rows for `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `OUTREACH_FROM_EMAIL`, `PHYSICAL_MAILING_ADDRESS`, `UNSUBSCRIBE_EMAIL`, `PUBLIC_PACKET_BASE_URL`, and sending domain.
- SMTP test is WARN because it was not tested.

No-go failures:

- Missing SMTP.
- Missing physical mailing address.
- Missing unsubscribe.
- Missing public packet base URL.
- DNS unknown/unresolved.
- SMTP test not completed before real send.

Do not send if any FAIL rows remain.

## 16. First Send Procedure

Implemented sender page:

```text
GET /send
POST /send/test
POST /send/batch
```

Test send procedure:

1. Open `/send`.
2. Enter your own test recipient address.
3. Check `I confirm this sends one test email only.`
4. Click `Send Test Email`.

Test send behavior:

- Sends one infrastructure test.
- Subject: `Outbound infrastructure test`.
- Does not touch prospects, queue, or prospect outreach status.
- Writes dashboard test logs to `runs/latest` via `write_send_test_log`.

Real batch procedure:

1. Open `/send`.
2. Confirm Safety Gate is Ready.
3. Confirm `Daily cap` is `10` or lower.
4. Confirm queued rows show `Sendable`.
5. Set batch limit to `3` for first live batch.
6. Do not check screenshot attachments.
7. Check the real-send confirmation checkbox.
8. Click `Send Real Batch`.

Dashboard constants:

- `SEND_DEFAULT_LIMIT = 5`
- `SEND_MAX_LIMIT = 10`
- `SEND_DEFAULT_DAILY_CAP = 10`
- Required confirmation: `confirm_send` checkbox.
- Campaign: `intro_email`
- Step: `1`
- Attachment max size: 1,500,000 bytes, but attachments are off unless explicitly selected and queue metadata allows them.

Batch send gates in code:

- Latest infra check has no FAIL rows.
- Send config complete, including sender business name, physical address, unsubscribe email.
- Daily cap remaining.
- Queue row status is `queued`.
- Step is `1`.
- `human_review_decision = APPROVED`.
- Prospect status is `OUTREACH_DRAFTED` or `APPROVED_FOR_OUTREACH`.
- `next_action = SEND_OUTREACH`.
- Email exists and is not suppressed.
- Draft artifact exists, ready, file exists, subject/body exist.
- Public packet artifact exists, ready, URL exists.
- No duplicate sent event exists.
- `send_after` not in future.

After success:

- `outreach_queue.status = sent`
- `prospects.status = OUTREACH_SENT`
- `prospects.next_action = WAIT_FOR_REPLY`
- `outreach_events.event_type = sent`
- `outreach_events.status = sent`

On failure:

- `outreach_queue.status = failed`
- `outreach_events.event_type = send_failed`
- error summary stored in metadata.

CLI sender exists for legacy/operator use, but it is **not** the approved first-batch live send path.

Safe preview only:

```powershell
.\.venv\Scripts\python.exe -m src.send_outreach --dry-run --limit 5 --market akron_oh --niche roofing --campaign intro_email
```

Do not run CLI `--send` for the first live batch. The CLI sender can send approved drafted prospects without requiring the dashboard queue or public packet gate, so this runbook intentionally does not provide a live CLI send command. Use dashboard `/outbound` + `/send`.

Rules:

- First live batch: exactly 3.
- No attachments.
- No tracking pixels.
- No link tracking unless implemented safely later.
- No follow-up automation until inbox sync/manual reply handling is proven.
- Step 1 only.
- Verify `outreach_events` and CRM status after send.

## 17. Inbox, Replies, Unsubscribes, and Bounces

Implemented in `src/inbox_sync.py`.

Dry-run:

```powershell
.\.venv\Scripts\python.exe -m src.inbox_sync --dry-run --since-days 14
```

Apply:

```powershell
.\.venv\Scripts\python.exe -m src.inbox_sync --apply --since-days 14
```

Manual CSV fallback:

```powershell
.\.venv\Scripts\python.exe -m src.inbox_sync --dry-run --manual-csv runs/latest/inbound_replies.csv
.\.venv\Scripts\python.exe -m src.inbox_sync --apply --manual-csv runs/latest/inbound_replies.csv
```

Manual CSV columns:

```text
email,category,note
```

Valid categories:

- `interested`
- `not_interested`
- `unsubscribe`
- `bounce`
- `auto_reply`
- `unknown_reply`

IMAP behavior:

- Connects only when CLI is run.
- Uses `readonly=True` when selecting mailbox.
- Does not delete or move messages.
- Requires `IMAP_HOST`, `IMAP_USERNAME`, `IMAP_PASSWORD`; optional `IMAP_PORT`, `IMAP_USE_SSL`.
- If IMAP is not configured, uses manual CSV fallback.

Classification:

- `unsubscribe`: clear unsubscribe/stop/remove/do-not-contact terms.
- `bounce`: mailer-daemon/postmaster or delivery failure indicators.
- `auto_reply`: out-of-office/vacation/autoreply indicators.
- `not_interested`: "not interested", "no thanks", "all set", etc.
- `interested`: call, interested, price, quote, more info, discuss, send details.
- Otherwise `unknown_reply`.

Apply effects:

- `unsubscribe`: upserts `suppression_list`, sets prospect `DISCARDED`, `next_action = NONE`.
- `bounce`: marks contact bounced/bad, suppresses email, sets `CLOSED_LOST`, `next_action = NONE`.
- `not_interested`: sets `CLOSED_LOST`, `next_action = NONE`.
- `interested` or `unknown_reply`: sets `CONTACT_MADE`, `next_action = SCHEDULE_CALL`.
- `auto_reply`: records event; does not change prospect status.
- All matched non-auto-replies cancel queued follow-ups for that prospect/email.

Summary files:

```text
runs/latest/inbox_sync.json
runs/latest/inbox_sync.txt
```

Dashboard `/send` shows latest inbox sync summary.

Manual operation after every send:

1. Watch mailbox.
2. Record any reply/unsubscribe/bounce immediately.
3. Run inbox sync dry-run.
4. If classifications look safe, run apply.
5. Do not send follow-up when status is uncertain.

## 18. Sales Call and Close Procedure

When a reply comes in:

1. Run inbox sync or manually update CRM.
2. Move interested prospects to `CONTACT_MADE` / `SCHEDULE_CALL`.
3. Open the case page: `/case/<prospect_id>`.
4. If available, open sales packet: `/sales-packet/<prospect_id>` for stages `CONTACT_MADE`, `CALL_BOOKED`, `PROPOSAL_SENT`, `CLOSED_WON`.
5. Re-read public packet and visual critique before the call.

15-minute call flow:

1. Confirm their role and whether they manage the website.
2. Ask what they want the website to do better: calls, quote requests, service-area visibility, trust, speed.
3. Walk through 2-3 visible issues from the public packet.
4. Ask if those issues match what they see internally.
5. Explain offer as website replacement and managed web operations, not AI.
6. Confirm constraints: timeline, budget, domain/hosting access, service areas, priority services.
7. Offer next step: paid build proposal or simple replacement scope.

Pricing options:

- Website Replacement: `$3,500-$8,500`
- Managed Web Operations: `$299-$699/mo`
- Custom Growth Build: `$10,000+`
- Fallback: lower upfront plus 12-month managed term.

Before making claims:

- Verify licensing, insurance, warranties, service guarantees, locations, service areas, and emergency availability.
- Do not claim ranking, revenue, lead volume, or conversion lift guarantees.

Deposit:

- Ask for deposit after scope is agreed.
- Do not start fulfillment without signed SOW/contract and deposit.

After call:

- Send recap.
- Send scope/SOW.
- Request intake/access.
- Move CRM to `CALL_BOOKED`, `PROPOSAL_SENT`, `CLOSED_WON`, or `CLOSED_LOST` as appropriate.

## 19. Fulfillment Handoff After Close

Repo status: no WordPress/theme/LocalWP fulfillment files found in this repo. Fulfillment is an external process.

Client intake:

- Legal business name.
- Main domain and registrar access.
- Hosting/admin access.
- Current website CMS/admin access.
- Primary phone number.
- Primary email and forwarding preferences.
- Service areas.
- Services and priority services.
- Licenses, insurance, warranties, financing claims, only if verified.
- Photos they own or have rights to use.
- Testimonials/reviews they have rights to use.
- Logo/brand files.
- Analytics/Search Console/Tag Manager access if available.
- CRM/form destination.

Contract/SOW:

- Define website replacement scope.
- Define page count, service pages, service-area architecture, forms/calls, tracking setup, hosting/maintenance, revision limit, launch target.
- Include managed web operations monthly terms if sold.
- Collect deposit before work.

Launch checklist:

- DNS/domain plan.
- Staging review.
- Mobile call/request path tested.
- Forms tested.
- Analytics/tracking installed.
- Redirects if replacing existing site.
- Backup old site when possible.
- Post-launch QA on mobile/desktop.

## 20. First-Batch Recommended Sequence

Concrete recipe for first real loop:

1. Start dashboard:

```powershell
.\scripts\start_dashboard.ps1
```

2. Open:

```text
http://127.0.0.1:8787
```

3. Go to `/run?market=akron_oh&niche=roofing`.
4. Do not pull more Places yet unless you need more raw leads.
5. Open `/review?market=akron_oh`.
6. Delete obvious good/no-fit sites from review cards.
7. Open 10 case files.
8. Inspect screenshots, current website, score reasons, contact signals.
9. Save visual critique for each serious candidate.
10. Approve 5-10 max. Reject/hold the rest, but only queue the best 3 for the first send.
11. Run contact readiness:

```powershell
.\.venv\Scripts\python.exe -m src.contact_readiness --market akron_oh --niche roofing --limit 20
```

12. Re-open cases with missing contacts. Manually save only verified website-published emails.
13. Generate public packets:

```powershell
.\.venv\Scripts\python.exe -m src.public_packets --market akron_oh --niche roofing --limit 10
```

14. Inspect packet HTML locally under `public_outreach/p/<token>/index.html`.
15. Configure:

```env
PUBLIC_PACKET_PAGES_PROJECT=
PUBLIC_PACKET_BASE_URL=
```

16. Deploy packets:

```powershell
.\scripts\deploy_public_packets_cloudflare.ps1
```

17. Confirm packet URLs open from `PUBLIC_PACKET_BASE_URL`.
18. Generate outreach drafts from case page or CLI:

```powershell
.\.venv\Scripts\python.exe -m src.outreach_drafts --market akron_oh --niche roofing --limit 10
```

19. Read every draft on the case page. Edit manually in the text file if needed.
20. Configure email infrastructure.
21. Run:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --domain example.com
```

22. Fix every FAIL.
23. Run SMTP login test:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --test-smtp
```

24. Send one test to yourself:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --send-test-to you@example.com
```

25. Open `/outbound?market=akron_oh&niche=roofing`.
26. Create Step 1 queue with max size 3 for the first send.
27. Open `/send`.
28. Confirm queued rows are sendable.
29. Send first real batch of 3.
30. Monitor mailbox manually for 24 hours.
31. Run inbox dry-run:

```powershell
.\.venv\Scripts\python.exe -m src.inbox_sync --dry-run --since-days 2
```

32. Apply only if classifications look right:

```powershell
.\.venv\Scripts\python.exe -m src.inbox_sync --apply --since-days 2
```

33. Do not send more until 24 hours of clean behavior.

## 21. Go / No-Go Checklist

GO only if:

- [ ] Dashboard starts at `http://127.0.0.1:8787`.
- [ ] Dashboard is not publicly exposed.
- [ ] Selected market/niche has reviewed approved prospects.
- [ ] `contacts` rows exist for selected prospects.
- [ ] Contact emails are website-published or manually verified.
- [ ] Public packet URLs work from `PUBLIC_PACKET_BASE_URL`.
- [ ] Draft artifacts exist and are reviewed.
- [ ] Latest `email_infra_check` has no FAIL rows.
- [ ] Physical mailing address is present.
- [ ] Unsubscribe email/instruction is present.
- [ ] Suppression list check works.
- [ ] Duplicate send protection is clear.
- [ ] Test email to self is received.
- [ ] Daily cap is `10` or lower.
- [ ] First send batch limit is exactly 3.
- [ ] Mailbox will be monitored after send.

NO-GO if:

- [ ] No public packet URL.
- [ ] No unsubscribe/physical address.
- [ ] SMTP untested.
- [ ] Email infrastructure checker has FAIL rows.
- [ ] Sender can email unapproved prospects from the chosen path.
- [ ] Duplicate send protection unclear.
- [ ] Batch size greater than 10.
- [ ] Status gates are ambiguous.
- [ ] `/run` can send email. Current code says it cannot; if this changes, stop.
- [ ] Dashboard exposed publicly.
- [ ] Mailbox not monitored.
- [ ] Prospect is suppressed.
- [ ] Prospect lacks approved manual review.

## 22. Failure Handling

Places job fails:

- Open `/jobs/<job_key>`.
- Check log under `runs/dashboard_jobs/<job_key>.log`.
- If missing `GOOGLE_MAPS_API_KEY`, configure `.env`.
- If quota/cost concern, lower limit and use one niche/market.

Audit job hangs:

- Use `/jobs/<job_key>` log.
- Prefer fast audit:

```powershell
.\.venv\Scripts\python.exe -m src.audit_site --fast --skip-pagespeed --limit 5 --market akron_oh --niche roofing
```

- Lower `--screenshot-timeout-ms` and `--page-timeout-ms` if using CLI.
- Dashboard currently supports fast/deep, not worker parallelism.

Screenshots missing:

- Open case page and confirm artifact status.
- Re-run a small audit.
- Confirm Playwright Chromium installed:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

PageSpeed fails:

- For triage, use fast audit and skip PageSpeed.
- For artifact generation that requires PageSpeed scores, run a small deep audit or diagnose PageSpeed key/quota.

Scoring produces no review leads:

- Check `/run` recommended-action panel.
- Confirm `audit_data_status = READY`.
- Confirm audit succeeded and score command ran.
- Run `reconcile_statuses --dry-run`.

Packet missing:

- Confirm prospect is approved and `audit_data_status = READY`.
- Run public packet dry-run.
- Check for screenshots; screenshots are preferred but not strictly required by generator.

Cloudflare deploy fails:

- Confirm `public_outreach/` exists.
- Confirm `PUBLIC_PACKET_PAGES_PROJECT`.
- Confirm Wrangler installed:

```powershell
npm install -g wrangler
wrangler login
```

- Run deploy script again.

SMTP login fails:

- Do not send.
- Check host/port/TLS.
- Use provider app password/token.
- Run `email_infra_check --test-smtp`.

Test email goes to spam:

- Do not send live batch.
- Check SPF/DKIM/DMARC.
- Reduce links/copy complexity.
- Confirm sender identity and mailbox reputation.

Real email bounces:

- Run inbox sync or manual CSV.
- Suppress email.
- Mark contact bad/bounced.
- Do not retry automatically.

Unsubscribe received:

- Add to `suppression_list` via inbox sync apply or manual DB/admin process.
- Cancel queued follow-ups.
- Set `next_action = NONE`.

Angry reply:

- Treat as unsubscribe/not interested.
- Suppress.
- Do not argue.
- Update CRM `CLOSED_LOST` or `DISCARDED`.

Duplicate queue row:

- The DB unique index should block non-cancelled duplicates.
- If a duplicate appears from legacy data, cancel extras before sending.

Dashboard server stops:

- Restart using `scripts/start_dashboard.ps1`.
- Jobs running in Flask background thread may be interrupted.
- `dashboard_jobs.mark_stale_jobs` marks old queued/running jobs stale on next dashboard launch after six hours.

Docker container restarts:

- Data persists through bind mounts.
- Check:

```powershell
docker compose ps
docker logs lead-dashboard
```

SQLite backup/restore basics:

- Before live sending or schema changes, copy `data/leads.db` to a timestamped backup.
- Do not copy while a job is actively writing if avoidable.
- Also back up `data/leads.db-wal` and `data/leads.db-shm` if present.

## 23. Maintenance and Backups

Back up these folders before a live outbound batch:

```text
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads\data\
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads\config\
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads\runs\
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads\screenshots\
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads\artifacts\
C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads\public_outreach\
```

Minimum backup moments:

- Before first real email.
- Before schema changes.
- Before large Places pull.
- Before large audit run.
- Before public packet token rotation.
- Before Docker migration/server move.

Suggested manual backup command:

```powershell
Copy-Item .\data\leads.db .\data\leads.backup.YYYYMMDD-HHMMSS.db
```

If WAL files exist:

```powershell
Copy-Item .\data\leads.db-wal .\data\leads.backup.YYYYMMDD-HHMMSS.db-wal
Copy-Item .\data\leads.db-shm .\data\leads.backup.YYYYMMDD-HHMMSS.db-shm
```

Avoid losing SQLite state:

- Do not delete `data/`.
- Do not run destructive git reset/checkouts over generated local state.
- Do not expose or upload `data/leads.db`.
- Keep `.gitignore` entry for `public_outreach/`, `runs/`, `screenshots/`, `data/*.db`.

## 24. Docker / Server Mode

Implemented files:

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `docs/DOCKER_RUNBOOK.md`

Build:

```powershell
docker compose build
```

Run:

```powershell
docker compose up
```

Open:

```text
http://127.0.0.1:8787
```

Compose port binding:

```yaml
ports:
  - "127.0.0.1:8787:8787"
```

Persistent volumes:

```yaml
./data:/app/data
./runs:/app/runs
./artifacts:/app/artifacts
./screenshots:/app/screenshots
./public_outreach:/app/public_outreach
./config:/app/config
```

Base image:

```text
mcr.microsoft.com/playwright/python:v1.49.1-jammy
```

Do not expose dashboard publicly. Avoid:

```yaml
ports:
  - "0.0.0.0:8787:8787"
```

If remote:

- Use SSH tunnel:

```powershell
ssh -L 8787:127.0.0.1:8787 user@server
```

- Or use Tailscale/private network.
- Do not serve public packets from Flask. Deploy `public_outreach/` separately as static files.

## 25. Appendix: Exact Commands

### Setup

```powershell
cd "C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
python -m src.db
```

### Start Dashboard

```powershell
.\scripts\start_dashboard.ps1
```

```bat
scripts\start_dashboard.bat
```

```powershell
.\.venv\Scripts\python.exe -m src.dashboard_app --host 127.0.0.1 --port 8787
```

### Pipeline

Places:

```powershell
.\.venv\Scripts\python.exe -m src.places_pull --dry-run --limit 10 --market akron_oh --niche roofing
.\.venv\Scripts\python.exe -m src.places_pull --limit 10 --market akron_oh --niche roofing
```

Eligibility:

```powershell
.\.venv\Scripts\python.exe -m src.eligibility --dry-run --limit 100 --market akron_oh --niche roofing
.\.venv\Scripts\python.exe -m src.eligibility --limit 100 --market akron_oh --niche roofing
```

Audit:

```powershell
.\.venv\Scripts\python.exe -m src.audit_site --dry-run --fast --skip-pagespeed --limit 5 --market akron_oh --niche roofing
.\.venv\Scripts\python.exe -m src.audit_site --fast --skip-pagespeed --limit 5 --market akron_oh --niche roofing
.\.venv\Scripts\python.exe -m src.audit_site --limit 5 --market akron_oh --niche roofing
```

Scoring:

```powershell
.\.venv\Scripts\python.exe -m src.score_leads --dry-run --limit 25 --market akron_oh --niche roofing
.\.venv\Scripts\python.exe -m src.score_leads --limit 25 --market akron_oh --niche roofing
```

Artifacts:

```powershell
.\.venv\Scripts\python.exe -m src.generate_artifacts --dry-run --score-min 0 --limit 10 --market akron_oh --niche roofing
.\.venv\Scripts\python.exe -m src.generate_artifacts --score-min 0 --limit 10 --market akron_oh --niche roofing
```

Reconcile:

```powershell
.\.venv\Scripts\python.exe -m src.reconcile_statuses --dry-run --limit 100
.\.venv\Scripts\python.exe -m src.reconcile_statuses --apply --limit 100
```

### Contact Readiness

```powershell
.\.venv\Scripts\python.exe -m src.contact_readiness --dry-run --market akron_oh --niche roofing --limit 100
.\.venv\Scripts\python.exe -m src.contact_readiness --market akron_oh --niche roofing --limit 100
.\.venv\Scripts\python.exe -m src.contact_readiness --prospect-id 123
```

### Public Packets

```powershell
.\.venv\Scripts\python.exe -m src.public_packets --dry-run --market akron_oh --niche roofing --limit 5
.\.venv\Scripts\python.exe -m src.public_packets --market akron_oh --niche roofing --limit 5
.\.venv\Scripts\python.exe -m src.public_packets --prospect-id 123
.\.venv\Scripts\python.exe -m src.public_packets --prospect-id 123 --rotate-token
```

### Deploy Packets

```powershell
npm install -g wrangler
wrangler login
.\scripts\deploy_public_packets_cloudflare.ps1
```

```bat
scripts\deploy_public_packets_cloudflare.bat
```

### Outreach Drafts

```powershell
.\.venv\Scripts\python.exe -m src.outreach_drafts --dry-run --market akron_oh --niche roofing --limit 5
.\.venv\Scripts\python.exe -m src.outreach_drafts --market akron_oh --niche roofing --limit 5
.\.venv\Scripts\python.exe -m src.outreach_drafts --prospect-id 123
.\.venv\Scripts\python.exe -m src.outreach_drafts --prospect-id 123 --force
```

### Email Infra Check

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check
.\.venv\Scripts\python.exe -m src.email_infra_check --domain example.com
.\.venv\Scripts\python.exe -m src.email_infra_check --test-smtp
.\.venv\Scripts\python.exe -m src.email_infra_check --send-test-to you@example.com
```

### Outbound Readiness

Dashboard only:

```text
GET /outbound?market=akron_oh&niche=roofing
POST /outbound/queue
```

### Send Test

Dashboard:

```text
GET /send
POST /send/test
```

Email infra CLI test:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --send-test-to you@example.com
```

### Send Batch

Dashboard preferred:

```text
GET /send
POST /send/batch
```

Real-send confirmation uses the `confirm_send` checkbox.

CLI sender exists but is not recommended for first live batch:

```powershell
.\.venv\Scripts\python.exe -m src.send_outreach --dry-run --limit 5 --market akron_oh --niche roofing --campaign intro_email
```

No live CLI send command is listed intentionally. For the first batch, use only `/outbound` to create the queue and `/send` to send queued rows after all readiness gates pass.

### Inbox Sync

```powershell
.\.venv\Scripts\python.exe -m src.inbox_sync --dry-run --since-days 14
.\.venv\Scripts\python.exe -m src.inbox_sync --apply --since-days 14
.\.venv\Scripts\python.exe -m src.inbox_sync --dry-run --manual-csv runs/latest/inbound_replies.csv
.\.venv\Scripts\python.exe -m src.inbox_sync --apply --manual-csv runs/latest/inbound_replies.csv
```

### Docker

```powershell
docker compose build
docker compose up
docker compose ps
docker logs lead-dashboard
docker compose down
```

## 26. Appendix: Route Map

| Route | Method | Purpose | Reads DB | Writes DB | Can call external API | Can send email | Safe before config |
|---|---|---|---:|---:|---:|---:|---:|
| `/` | GET | Overview | Yes | No | No | No | Yes |
| `/review` | GET | Review queue | Yes | No | No | No | Yes |
| `/review/<int:prospect_id>/delete` | POST | Quick reject/delete from queue | Yes | Yes | No | No | Yes, but mutates review state |
| `/leads` | GET | Lead table | Yes | No | No | No | Yes |
| `/crm` | GET | CRM board | Yes | No | No | No | Yes |
| `/crm/stage/<stage>` | GET | CRM stage detail | Yes | No | No | No | Yes |
| `/outbound` | GET | Outbound readiness | Yes | No | No | No | Yes |
| `/outbound/queue` | POST | Create queued send rows | Yes | Yes | No | No | Only after infra/packets/drafts ready |
| `/send` | GET | Send dashboard | Yes | No | No | No | Yes |
| `/send/test` | POST | One test email | No prospect mutation | Writes test log | SMTP only | Yes, test only | Only after SMTP config |
| `/send/batch` | POST | Real queued batch send | Yes | Yes | SMTP | Yes | No, live send gate |
| `/sales-packet/<int:prospect_id>` | GET | Sales packet | Yes | No | No | No | Yes |
| `/sales-packet/<int:prospect_id>/notes` | POST | Save sales notes | Yes | Yes | No | No | Yes, mutates notes |
| `/pipeline` | GET | Redirect to Run | No | No | No | No | Yes |
| `/run` | GET | Pipeline controls | Yes | No | No | No | Yes |
| `/run/full-pipeline` | POST | Start full pipeline job | Yes | Yes job row | Places/audit when live | No | Only with deliberate confirmation |
| `/jobs` | GET | Job list/start form | Yes | No | No | No | Yes |
| `/jobs/start` | POST | Start whitelisted job | Yes | Yes job row | Places/audit when live | No | Only with deliberate confirmation |
| `/jobs/<job_key>` | GET | Job detail/log | Yes | No | No | No | Yes |
| `/jobs/<job_key>/status` | GET | Job status JSON | Yes | No | No | No | Yes |
| `/pipeline/run` | POST | Retired old runner | No | No | No | No | Yes |
| `/markets` | GET | Market manager | Yes | No | No | No | Yes |
| `/markets/add` | POST | Add market YAML | Counts DB | Writes config | No | No | Yes, config mutation only |
| `/case/<int:prospect_id>` | GET | Case page | Yes | No | No | No | Yes |
| `/case/<int:prospect_id>/review` | POST | Manual approve/reject/hold | Yes | Yes | No | No | Yes, mutates review state |
| `/case/<int:prospect_id>/stage` | POST | CRM stage update | Yes | Yes | No | No | Yes, mutates CRM state |
| `/case/<int:prospect_id>/contact` | POST | Save contact | Yes | Yes | No | No | Yes, mutates contact |
| `/case/<int:prospect_id>/visual-review` | POST | Save visual critique | Yes | Yes | No | No | Yes |
| `/case/<int:prospect_id>/outreach-drafts` | POST | Generate local drafts | Yes | Yes | No | No | Yes, writes local drafts |
| `/media/<path>` | GET | Serve approved screenshots/artifacts/runs media | No | No | No | No | Yes |
| `/files/<path>` | GET | Serve project file | No | No | No | No | Be careful; local operator only |
| `/health` | GET | Health check | No | No | No | No | Yes |

## 27. Appendix: State and Gate Map

Canonical classes in `src/state.py`:

`qualification_status`:

- `DISCOVERED`
- `QUALIFIED`
- `DISQUALIFIED`
- Code also writes `AUDITED` and `AUDIT_FAILED` in `src.audit_site`.

`audit_data_status`:

- `PENDING`
- `NEEDS_SITE_AUDIT`
- `NEEDS_SCREENSHOTS`
- `NEEDS_PAGESPEED`
- `READY`

`human_review_status`:

- `NOT_READY`
- `PENDING`
- `APPROVED`
- `REJECTED`

`human_review_decision`:

- `APPROVED`
- `REJECTED`

`prospects.status`:

- `NEW`
- `ELIGIBLE_FOR_AUDIT`
- `INELIGIBLE`
- `AUDIT_READY`
- `PENDING_REVIEW`
- `APPROVED_FOR_OUTREACH`
- `REJECTED_REVIEW`
- `OUTREACH_DRAFTED`
- `OUTREACH_SENT`
- `CONTACT_MADE`
- `CALL_BOOKED`
- `PROPOSAL_SENT`
- `CLOSED_WON`
- `CLOSED_LOST`
- `PROJECT_ACTIVE`
- `PROJECT_COMPLETE`
- `DISCARDED`

`next_action`:

- `RUN_AUDIT`
- `HUMAN_REVIEW`
- `APPROVED_FOR_OUTREACH`
- `SEND_OUTREACH`
- `WAIT_FOR_REPLY`
- `SCHEDULE_CALL`
- `TAKE_CALL`
- `FOLLOW_UP_PROPOSAL`
- `START_PROJECT`
- `FULFILL_PROJECT`
- `DISCARD`
- `NONE`

`outreach_events` key fields:

- `event_key`
- `prospect_id`
- `contact_id`
- `campaign_key`
- `channel`
- `event_type`
- `status`
- `subject`
- `body_path`
- `metadata_json`
- `scheduled_for`
- `sent_at`

`outreach_queue` statuses:

- `queued`
- `sent`
- `skipped`
- `cancelled`
- `failed` used by dashboard send code even though original prompt listed queued/sent/skipped/cancelled.

Minimum state required:

| Operation | Required state |
|---|---|
| Audit selection | Batch: `qualification_status = QUALIFIED`, status not blocked, next action not blocked, website URL present, not `audit_data_status = READY` unless `--force`. |
| Review queue | `human_review_status = PENDING`, `next_action = HUMAN_REVIEW`, scored/audit-ready rows loaded by `load_review_queue`. |
| Outreach draft generation | `human_review_decision = APPROVED`, `next_action = APPROVED_FOR_OUTREACH`, status blank/`APPROVED_FOR_OUTREACH`/`OUTREACH_DRAFTED`, contact email unless override. |
| Public packet generation | `human_review_decision = APPROVED`, status `APPROVED_FOR_OUTREACH` or `OUTREACH_DRAFTED`, `audit_data_status = READY`. |
| Queue creation | Approved outbound row, contact ready, public packet ready, draft ready, no suppression, not already sent, not already queued, infra ready. |
| Actual dashboard send | Queued step 1, approved, status compatible, `next_action = SEND_OUTREACH`, contact email, draft ready, packet ready, no suppression, no duplicate sent event, infra ready, daily cap remaining, exact confirmation text. |

## 28. Appendix: Open Gaps

### P0 - Must Fix Before Any Real Outbound Email

1. Email infrastructure is not configured in the inspected state.
   - Evidence: latest `runs/latest/email_infra_check.json` has `exit_code = 1` and FAIL rows for SMTP, sender email, physical address, unsubscribe, public packet base URL, and sending domain.
   - Fix: complete Section 5 and Section 15 before queueing/sending.

2. No sendable prospect data exists in the inspected `data/leads.db`.
   - Evidence: `contacts = 0`, `outreach_queue = 0`, no approved prospect rows returned, `public_outreach/` missing.
   - Fix: approve prospects, run contact readiness, generate/deploy packets, generate drafts, then queue.

3. Public packet folder is not generated yet.
   - Evidence: `public_outreach/` does not exist.
   - Fix: run `src.public_packets` after approvals.

### P1 - Fix Before More Than 25 Emails

1. CLI sender is less strict than dashboard sender.
   - Evidence: `src.send_outreach` can send approved drafted prospects with `--send` but does not require `outreach_queue` or `public_packet`.
   - Mitigation: use `/outbound` + `/send` only for first batch. Later align CLI gates or reserve CLI for emergencies.

2. Fast audit and artifact-generation expectations differ.
   - Evidence: `src.audit_site --fast` skips PageSpeed by default; `src.generate_artifacts._artifact_block_reason` skips rows with missing PageSpeed scores.
   - Mitigation: use public packets and case screenshots for fast-audited first batch, or run deep/PageSpeed on final prospects before local artifact generation.

3. Inbox sync needs operational proof.
   - Evidence: `runs/latest/inbox_sync.json` currently shows manual CSV dry-run with zero processed.
   - Mitigation: test manual CSV dry-run/apply on a safe copied DB or after first reply using current DB carefully; configure IMAP later if desired.

### P2 - Cleanup / After First Sales Loop

1. `.env.example` lacks newer Phase 2 keys.
   - Missing examples include `PUBLIC_PACKET_BASE_URL`, `PUBLIC_PACKET_PAGES_PROJECT`, `PHYSICAL_MAILING_ADDRESS`, `UNSUBSCRIBE_EMAIL`, `DKIM_SELECTOR`, and IMAP keys. Docs contain some of them, but `.env.example` is behind.

2. README still describes older CLI-only/no-send posture in places.
   - Current dashboard now has `/send`; README_OPERATOR says dashboard send may be implemented later.

3. Dashboard `/files/<path>` is broad project-root file serving.
   - It is local-only and not public, but keep dashboard private. Do not expose Flask.

4. Parallel audit workers are not implemented.
   - Current job runner allows one active job and `src.audit_site` processes sequentially.

5. No WordPress/showcase fulfillment module exists in this repo.
   - Fulfillment needs external SOP/SOW until a separate project exists.

## QA Verdict

- Ready for first live test? **No, not in the currently inspected state.** The code has a controlled dashboard send path, but the repo state still has P0 blockers: email infrastructure is failing, no approved/contact-ready/public-packet-ready prospects were found, and `public_outreach/` has not been generated.
- Required P0 fixes: configure SMTP/from identity/physical address/unsubscribe/public packet base URL; rerun `python -m src.email_infra_check` until there are no FAIL rows; approve prospects through manual review; create verified website-published/manual contacts; generate and deploy public packets; generate and review drafts; create `/outbound` queue rows only for send-ready prospects.
- Recommended first batch size: **3 real emails**. Do not use the dashboard max of 10 for the first live test.
- Recommended first market/niche: **`akron_oh` / `roofing`** after finishing review and readiness gates. Use `mckinney_tx` / `roofing` only if Akron does not produce 3 strong, contact-ready, packet-ready prospects.
- Exact final send/no-send condition: **Send only from `/send` after `/outbound` shows the prospects as send-ready, latest email infra check has zero FAIL rows, packet URLs open publicly, each draft has been manually reviewed, each email is not suppressed and not previously sent, the mailbox is actively monitored, and the batch limit is 3. Otherwise, no-send.**
