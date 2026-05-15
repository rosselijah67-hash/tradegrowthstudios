# Docker Runbook

This runs the local dashboard in Docker while keeping the Flask app private by default. Public audit packets should still be deployed from `public_outreach/` to static hosting such as Cloudflare Pages; do not expose the Flask dashboard as the public packet host.

## Build

From the repo root:

```powershell
docker compose build
```

The image uses the official Playwright Python base image so browser binaries and system dependencies are already present for screenshots and audits.

`requirements.txt` pins `playwright==1.49.1` to match the base image tag. Keep those versions aligned if you upgrade Playwright later.

## Run

```powershell
docker compose up
```

Open:

```text
http://127.0.0.1:8787
```

The compose file binds the dashboard to localhost only:

```yaml
ports:
  - "127.0.0.1:8787:8787"
```

Inside the container Flask listens on `0.0.0.0` so Docker can reach it, but the host port is only exposed on `127.0.0.1`.

The compose service is named `dashboard` and the container is named `lead-dashboard`, so logs are predictable:

```powershell
docker logs lead-dashboard
```

Compose also includes a `/health` check. After startup, `docker compose ps` should show the service as healthy.

## Persistent Data

These host folders are mounted into `/app`:

- `./data:/app/data` for SQLite databases
- `./runs:/app/runs` for job logs and latest reports
- `./artifacts:/app/artifacts` for generated files
- `./screenshots:/app/screenshots` for audit screenshots
- `./public_outreach:/app/public_outreach` for generated static packet output
- `./config:/app/config` for editable markets, niches, and outreach config

Because they are bind mounts, the data survives container rebuilds.

## Environment

Compose reads `.env` using:

```yaml
env_file:
  - .env
```

Keep `.env` local and private. Do not copy it into public packet hosting. Typical variables include database path overrides, API keys for audit commands, SMTP settings, IMAP settings, and public packet base URL.

## Audits And Playwright

The Docker image is based on:

```text
mcr.microsoft.com/playwright/python:v1.49.1-jammy
```

That base image includes Chromium and browser system dependencies. The app still only runs audits when you explicitly start audit jobs from the dashboard or CLI. Building or starting the dashboard does not run audits or call external APIs.

## VPS Safety

Do not bind the dashboard publicly on a VPS. Avoid this:

```yaml
ports:
  - "0.0.0.0:8787:8787"
```

Use one of these instead:

- SSH tunnel: `ssh -L 8787:127.0.0.1:8787 user@server`
- Tailscale or another private network
- A reverse proxy with authentication added separately

This module intentionally does not add dashboard authentication.

## Public Packets

Generated public packets live under `public_outreach/`, but they should be deployed to static hosting. Do not serve the Flask dashboard publicly for prospects.

Use the existing public packet deployment docs/scripts for Cloudflare Pages. The dashboard remains the private operator console.

## Stop

```powershell
docker compose down
```

This stops the container without deleting mounted data.
