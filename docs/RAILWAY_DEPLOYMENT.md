# Railway Deployment Runbook

This runbook deploys the private Trade Growth Studio CRM to Railway while keeping the public marketing site on Namecheap.

## Target Shape

- Public site: `tradegrowthstudios.com` on Namecheap shared hosting.
- Private CRM: `crm.tradegrowthstudios.com` on Railway.
- Public packets: same Railway app at `https://crm.tradegrowthstudios.com/p/<token>/` at first, or `audit.tradegrowthstudios.com` later.
- Database: SQLite at `data/leads.db`, persisted on a Railway volume.
- App server: Docker + Gunicorn.
- Access: dashboard login from env vars.

## Files Added For Railway

- `railway.json` tells Railway to build from the Dockerfile and use `/health`.
- `Dockerfile` now starts Gunicorn instead of Flask's dev server.
- `scripts/docker-entrypoint.sh` can symlink mutable folders into one Railway volume.
- `scripts/backup_sqlite.py` creates safe SQLite backups.
- Dashboard login is controlled by env vars.

## One-Time Local Prep

Generate a password hash:

```powershell
cd "C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads"
.\.venv\Scripts\python.exe -c "from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass('CRM password: ')))"
```

Generate a Flask secret:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

Keep both values private.

## Railway Setup

1. Create a Railway account.
2. Create a new project from the GitHub repo.
3. Railway should detect `railway.json` and use the Dockerfile.
4. Add a volume to the CRM service.
5. Mount the volume at:

```text
/app/storage
```

6. Add these Railway variables:

```text
DATABASE_PATH=data/leads.db
USE_STORAGE_SYMLINKS=1
STORAGE_ROOT=/app/storage
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_USERNAME=<your-admin-user>
DASHBOARD_PASSWORD_HASH=<generated-password-hash>
FLASK_SECRET_KEY=<generated-secret>
LOG_LEVEL=INFO
PUBLIC_PACKET_BASE_URL=https://crm.tradegrowthstudios.com
DASHBOARD_DB_IMPORT_ENABLED=false
DASHBOARD_MEDIA_IMPORT_ENABLED=false
```

7. Add outbound/email variables:

```text
SMTP_HOST=smtp.mailgun.org
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_USERNAME=<mailgun-smtp-user>
SMTP_PASSWORD=<mailgun-smtp-password>
OUTREACH_FROM_EMAIL=grow@mg.tradegrowthstudios.com
OUTREACH_REPLY_TO_EMAIL=grow@tradegrowthstudios.com
OUTREACH_FROM_NAME=Elijah Ross
OUTREACH_BUSINESS_NAME=Trade Growth Studio
OUTREACH_PHYSICAL_ADDRESS=<business-mailing-address>
OUTREACH_UNSUBSCRIBE_EMAIL=unsubscribe@tradegrowthstudios.com
```

8. Deploy.
9. Open the Railway-provided URL and confirm `/login` appears.
10. Add custom domain `crm.tradegrowthstudios.com`.
11. Add the DNS record Railway gives you in Cloudflare or Namecheap DNS.

## Uploading The Existing Database

The first deploy will create a blank `data/leads.db` if no database exists on the volume.

Best first-load path:

1. Create a local backup:

```powershell
.\.venv\Scripts\python.exe scripts\backup_sqlite.py
```

2. Upload `data/leads.db` to a temporary private file location.
3. Use Railway SSH to enter the service:

```bash
railway ssh
```

4. Download the DB into the volume-backed path:

```bash
curl -L "<temporary-private-download-url>" -o /app/storage/data/leads.db
```

5. Restart the Railway service.
6. Delete the temporary uploaded DB link.

Do not overwrite the Railway database after real users begin editing unless you are intentionally restoring a backup.

## Uploading Screenshots And Generated Media

The SQLite database stores paths like `screenshots/desktop/1.png`; the actual image files are separate. If the database is uploaded without these folders, the review queue will show missing desktop/mobile images.

Use this only while importing media:

1. In Railway variables, set:

```text
DASHBOARD_MEDIA_IMPORT_ENABLED=true
```

2. Let Railway redeploy.
3. Open:

```text
https://<your-railway-domain>/admin/media
```

4. Upload each local zip from:

```text
data/media_uploads/
```

5. Refresh the review queue and confirm screenshots appear.
6. In Railway variables, set:

```text
DASHBOARD_MEDIA_IMPORT_ENABLED=false
```

7. Let Railway redeploy again.

Keep `DASHBOARD_DB_IMPORT_ENABLED=false` and `DASHBOARD_MEDIA_IMPORT_ENABLED=false` during normal use.

## Public Packet URLs

This app now serves generated public packets at:

```text
/p/<token>/
/assets/public_packet.css
```

Those routes are public even when dashboard login is enabled. CRM pages remain behind login.

For a cleaner split later, add `audit.tradegrowthstudios.com` as another Railway custom domain and set:

```text
PUBLIC_PACKET_BASE_URL=https://audit.tradegrowthstudios.com
```

## Backups

Before every deploy that changes schema or sending logic, create a backup from inside Railway SSH:

```bash
python scripts/backup_sqlite.py
```

Backups are written to:

```text
/app/storage/backups/
```

Download important backups occasionally so Railway is not the only copy.

## Updating The CRM

Normal code update flow:

1. Make local changes.
2. Run checks:

```powershell
.\.venv\Scripts\python.exe -m compileall src scripts
```

3. Commit to Git.
4. Push to GitHub.
5. Railway auto-deploys the new commit.
6. Check Railway deploy logs.
7. Open `/health`.
8. Open the CRM and verify login.

Do not commit `.env`, `data/leads.db`, `runs/`, `artifacts/`, `screenshots/`, `public_outreach/`, or `backups/`.

## Rollback

If a deploy is bad:

1. In Railway, redeploy the previous successful deployment.
2. If database state is also bad, restore from a known backup:

```bash
cp /app/storage/backups/<backup-file>.db /app/storage/data/leads.db
```

3. Restart the service.

## Collaboration

Your friend does not need Railway, GitHub, Python, or Docker.

They only need:

```text
https://crm.tradegrowthstudios.com
CRM username/password
```

The Railway SQLite file is the shared source of truth.
