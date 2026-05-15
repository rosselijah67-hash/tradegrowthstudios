# Operator Runbook

## Purpose

This app is a local lead-review and outreach-prep dashboard for finding home-service prospects, auditing their public websites, manually reviewing the evidence, approving only good-fit leads, drafting grounded outreach, tracking CRM stages, and preparing sales packets. It is meant to keep the daily workflow in one browser dashboard so you do not have to remember command-line steps after a long shift.

## Start The Dashboard

1. Open the project folder, which is the folder containing `scripts\start_dashboard.bat`.
2. Double-click `scripts\start_dashboard.bat`.
3. Leave the black terminal window open.
4. If the browser does not open, go to:
   `http://127.0.0.1:8787`
5. To stop the dashboard, click the terminal window and press `Ctrl+C`.

The PowerShell alternative is `scripts\start_dashboard.ps1`, but the batch file is the simplest launcher.

## Daily Workflow

1. Open the dashboard and start at `CRM`.
   Check replies, `CONTACT_MADE`, `CALL_BOOKED`, `PROPOSAL_SENT`, and any active projects first.

2. If new Places leads were added, run eligibility from the `Pipeline` page.
   Keep limits small enough to review the results the same day.

3. Audit qualified prospects from the `Pipeline` page.
   This is the step most likely to make external calls for website checks, screenshots, and PageSpeed. Use small batches.

4. Score leads.
   Scoring turns stored audit data into review priority and expected close signals.

5. Generate artifacts.
   Artifacts make the case file easier to inspect, including screenshots and audit cards.

6. Review pending cases.
   Open `Review`, inspect each case file, check the public website, screenshots, scores, and contact signals.

7. Save the visual critique.
   Mark only issues you can defend from the screenshot or stored audit evidence.

8. Approve or reject.
   Approve only prospects that have a real reason for outreach. Rejected or discarded leads should not be contacted.

9. Generate outreach drafts.
   Drafts are local text files only. Read them before sending anything.

10. Send only a small approved batch.
   Do not send from memory. Confirm the prospect is approved, has drafts, has a valid email, is not suppressed, and has required sender compliance info. Real sending requires explicit `--send` or a dashboard send confirmation if that is later implemented.

## Pipeline Stage Definitions

- `NEW`: Lead exists, but has not been fully qualified or audited yet.
- `ELIGIBLE_FOR_AUDIT`: Lead looks eligible for a website audit.
- `INELIGIBLE`: Lead failed eligibility or is not worth auditing.
- `AUDIT_READY`: Website audit data is ready for scoring or review.
- `PENDING_REVIEW`: Needs human/manual review before outreach.
- `APPROVED_FOR_OUTREACH`: Manually approved, ready for draft generation.
- `REJECTED_REVIEW`: Manually rejected during review.
- `OUTREACH_DRAFTED`: Outreach drafts exist, but email has not necessarily been sent.
- `OUTREACH_SENT`: First outreach has been sent.
- `CONTACT_MADE`: The prospect replied or contact was otherwise made.
- `CALL_BOOKED`: A call is scheduled.
- `PROPOSAL_SENT`: A proposal has been sent or should be followed up.
- `CLOSED_WON`: Prospect became a customer.
- `CLOSED_LOST`: Opportunity is closed and should not be pursued.
- `PROJECT_ACTIVE`: Customer project is in progress.
- `PROJECT_COMPLETE`: Customer project is complete.
- `DISCARDED`: Removed from active work for any reason.

## What Not To Do

- Do not send outreach without manual review approval.
- Do not send to prospects with missing, uncertain, or suppressed emails.
- Do not attach screenshots by default.
- Do not run massive audits. Keep batches small so you can inspect failures and avoid noisy external usage.
- Do not claim guaranteed SEO results, rankings, lead volume, or revenue impact.
- Do not invent business claims such as licensing, warranties, emergency service, years in business, or affiliation.
- Do not imply the business hired you or asked for the audit.
- Do not treat pricing recommendations as contracts.

## Troubleshooting

### Dashboard Will Not Start

- Make sure you launched `scripts\start_dashboard.bat` from the project folder.
- If the window says Python was not found, the virtual environment may be missing. The project normally uses `.venv\Scripts\python.exe`.
- If port `8787` is already in use, close the other dashboard window or change the port in the launcher temporarily.
- If dependencies are missing, the setup may need to be repaired with `python -m pip install -r requirements.txt`.

### Database Missing

- The expected local database is usually under `data\leads.db`.
- If it is missing, initialize it once with `python -m src.db`.
- If you are unsure, do not delete any database files. Back them up first.

### Screenshots Missing

- Open the case file and confirm screenshot artifacts are missing rather than just slow to load.
- Run a small audit batch from the `Pipeline` page.
- If screenshots still fail, check whether Playwright/Chromium was installed during setup.

### Artifacts Missing

- Run `Generate artifacts` from the `Pipeline` page after audits and scoring are complete.
- Artifacts depend on stored audit and screenshot data, so missing upstream audit data can leave artifacts incomplete.

### No Email Candidates

- Do not approve for sending just because the company looks good.
- Check the case file for visible emails, mailto links, contact pages, and business-domain emails.
- If you manually add an email, verify it belongs to the business before using it.

### SMTP Not Configured

- SMTP settings live in `.env` and `.env.example` lists the required names.
- Missing SMTP config is fine for review, CRM, artifacts, sales packets, and draft generation.
- Do not attempt real sending until sender identity, physical address, unsubscribe handling, and SMTP settings are correct.

## Safety Notes

- The dashboard itself is local and runs at `127.0.0.1`.
- Most review, CRM, draft, and sales-packet work uses stored SQLite data only.
- External calls happen only when you run pipeline jobs that fetch or inspect outside data, such as Places pulls, website audits, screenshots, or PageSpeed checks.
- Email sending is not automatic. It requires explicit `--send` or a dashboard send confirmation if that feature is implemented later.
- When tired, default to reviewing and drafting. Save real sending for small, deliberate batches.
