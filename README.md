# ai-local-site-leads

Local Python 3.11 command-line project for a batch lead-generation pipeline.

This scaffold intentionally uses no web framework, no Supabase, no n8n, and no paid SaaS integrations. Persistence is SQLite, secrets live in `.env`, and each command is designed to be idempotent so reruns update or skip existing records instead of duplicating prospects, artifacts, or outreach events.

## Setup

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

If `py -3.11` is not available, use the Python 3.11 executable on your machine.

## Project Layout

```text
config/
  markets.yaml
  niches.yaml
  scoring.yaml
  outreach.yaml
src/
  db.py
  places_pull.py
  audit_site.py
  screenshot_site.py
  pagespeed.py
  score_leads.py
  review_leads.py
  generate_artifacts.py
  build_review_dashboard.py
  send_outreach.py
```

## Common Command Options

Every batch command supports:

```powershell
--dry-run
--limit 10
--market mckinney_tx
--niche roofing
```

Shared operational options:

```powershell
--db-path data/leads.db
--log-level INFO
```

Logs are emitted as JSON lines to stdout.

## Commands

Initialize or migrate the SQLite database:

```powershell
python -m src.db
```

Pull prospects from Google Places Text Search (New):

```powershell
python -m src.places_pull --dry-run --limit 10 --market mckinney_tx --niche roofing
python -m src.places_pull --limit 10 --market mckinney_tx --niche roofing
```

Set `GOOGLE_MAPS_API_KEY` in `.env` before running. The puller queries each configured city/search-term pair once, stores both discovered and disqualified prospects, and deduplicates by Google place ID, normalized website domain, and normalized phone number.

Queue placeholder site audits:

```powershell
python -m src.audit_site --dry-run --limit 5 --market mckinney_tx --niche roofing
python -m src.audit_site --limit 5 --market mckinney_tx --niche roofing
```

Capture screenshots only:

```powershell
python -m src.screenshot_site --dry-run --limit 5 --market mckinney_tx --niche roofing
python -m src.screenshot_site --limit 5 --market mckinney_tx --niche roofing
```

Screenshots are saved to `screenshots/desktop/{prospect_id}.png` and `screenshots/mobile/{prospect_id}.png`. They are also stored in the `artifacts` table as `screenshot_desktop` and `screenshot_mobile` rows, with file paths, content hashes, viewport metadata, and `usage = review_dashboard`.

Run PageSpeed checks only:

```powershell
python -m src.pagespeed --dry-run --limit 5 --market mckinney_tx --niche roofing
python -m src.pagespeed --limit 5 --market mckinney_tx --niche roofing
python -m src.pagespeed --failed-only --retries 2 --retry-delay 30 --timeout 75 --limit 5 --market mckinney_tx --niche roofing
```

`audit_site` orchestrates the HTML audit, screenshots, and PageSpeed checks. Keep early limits small because PageSpeed runs both mobile and desktop per prospect. If PageSpeed Insights fails because of quota, timeout, or API setup, `pagespeed.py` stores a diagnosed failure and falls back to a local speed probe so review artifacts do not show missing speed data. Use `--no-fallback` only for debugging.

Score leads using `config/scoring.yaml`:

```powershell
python -m src.score_leads --dry-run --limit 25 --market mckinney_tx --niche roofing
python -m src.score_leads --limit 25 --market mckinney_tx --niche roofing
```

Scoring updates score columns on `prospects`, writes a `lead_score` audit row, and exports `runs/latest/top_leads.csv`. Business quality, contactability, data availability, and market fit are grouped as `business_eligibility_score`; `expected_close_score` is driven by website pain for eligible businesses so polished sites do not create artifacts just because they are strong companies. A lead must have `audit_data_status = READY` before it is presented for human review.

List the human review queue or record a human decision:

```powershell
python -m src.review_leads --limit 25 --market mckinney_tx --niche roofing
python -m src.review_leads --prospect-id 7 --decision approved --score 80 --notes "Human reviewed and approved for outreach."
python -m src.review_leads --prospect-id 7 --decision rejected --notes "Good enough website; skip."
```

Human review is the approval gate for outreach. `send_outreach.py` skips every prospect that does not have `human_review_decision = APPROVED`.

Generate local outreach artifacts:

```powershell
python -m src.generate_artifacts --dry-run --score-min 0 --limit 10 --market mckinney_tx --niche roofing
python -m src.generate_artifacts --score-min 0 --limit 10 --market mckinney_tx --niche roofing
```

Artifact pages are written to `runs/latest/artifacts/{prospect_id}/` and indexed in
`runs/latest/artifact_summary.csv`. The `artifacts.path` column stores the local
artifact path; `artifacts.artifact_url` is populated only if `ARTIFACT_BASE_URL`
is set in `.env`. Artifact generation skips prospects without complete speed data.

Build a static local review dashboard:

```powershell
python -m src.build_review_dashboard --dry-run --limit 100 --market mckinney_tx --niche roofing
python -m src.build_review_dashboard --limit 100 --market mckinney_tx --niche roofing
```

Queue outreach events. This scaffold does not send external email; it only creates idempotent `outreach_events` rows:

```powershell
python -m src.send_outreach --dry-run --limit 10 --market mckinney_tx --niche roofing --campaign intro_email
python -m src.send_outreach --limit 10 --market mckinney_tx --niche roofing --campaign intro_email
```

## Import Checks

```powershell
python -m compileall src
python -c "import importlib; mods=['src.db','src.places_pull','src.audit_site','src.screenshot_site','src.pagespeed','src.score_leads','src.review_leads','src.generate_artifacts','src.build_review_dashboard','src.send_outreach']; [importlib.import_module(m) for m in mods]; print('imports ok')"
```
