# Auth Environment Setup

Generate password hashes locally, then copy only the hashes into Railway
Variables:

```powershell
cd "C:\Users\rosse\OneDrive\Desktop\ROSS WEB DESIGN\ai-local-site-leads"
.\.venv\Scripts\python.exe scripts\generate_password_hashes.py --generate-secret
```

To generate only some users:

```powershell
.\.venv\Scripts\python.exe scripts\generate_password_hashes.py --users QWHITE,JROSS
```

Set these Railway variables from the script output:

```text
APP_SECRET_KEY=<generated-secret>
AUTH_ADMIN_PASSWORD_HASH=<generated-hash>
AUTH_QWHITE_PASSWORD_HASH=<generated-hash>
AUTH_JROSS_PASSWORD_HASH=<generated-hash>
AUTH_AG_PASSWORD_HASH=<generated-hash>
```

The script also prints `railway variables set ...` examples. After changing
Railway variables, restart or redeploy the service so the app reads the new
environment.

Never commit plaintext passwords. Keep `.env.example` as placeholders in the
repo, and keep real hashes and `APP_SECRET_KEY` in local `.env` files or
Railway Variables.

This app uses SQLite at `data/leads.db` by default. On Railway, mount a
persistent volume for the data folder, or use `USE_STORAGE_SYMLINKS=1` with
`STORAGE_ROOT=/app/storage`, so SQLite data and generated files survive
redeploys.
