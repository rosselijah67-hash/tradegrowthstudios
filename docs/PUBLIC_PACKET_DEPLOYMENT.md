# Public Packet Deployment

Public audit packets are static files generated into `public_outreach/`. They are safe to host publicly because they do not include Flask dashboard controls, job logs, CRM notes, or database IDs.

The local Flask dashboard must stay private. Do not deploy the dashboard app, `data/`, `.env`, `runs/`, or the repository root.

## Cloudflare Pages Setup

1. Create or sign in to a Cloudflare account.
2. Create a Cloudflare Pages project for the public packet folder.
3. Set a custom domain for the Pages project, for example `audit.example.com`.
4. Configure these values in `.env` or `config/outreach.yaml`:

   ```env
   PUBLIC_PACKET_PAGES_PROJECT=your-cloudflare-pages-project
   PUBLIC_PACKET_BASE_URL=https://audit.example.com
   ```

5. Install Wrangler if it is not installed:

   ```powershell
   npm install -g wrangler
   wrangler login
   ```

6. Generate packet files locally:

   ```powershell
   .\.venv\Scripts\python.exe -m src.public_packets --market mckinney_tx --limit 25
   ```

   Or generate one packet:

   ```powershell
   .\.venv\Scripts\python.exe -m src.public_packets --prospect-id 123
   ```

7. Deploy only the static packet folder:

   ```powershell
   .\scripts\deploy_public_packets_cloudflare.ps1
   ```

   Or double-click/run:

   ```bat
   scripts\deploy_public_packets_cloudflare.bat
   ```

8. Confirm a packet opens at:

   ```text
   https://audit.example.com/p/<token>/
   ```

## What The Script Does

The deploy script checks that:

- `public_outreach/` exists.
- `wrangler` is installed.
- `PUBLIC_PACKET_PAGES_PROJECT` exists in the environment, `.env`, or `config/outreach.yaml`.

Then it runs:

```powershell
wrangler pages deploy public_outreach --project-name <configured project>
```

The script does not embed secrets, send email, delete packets, or deploy the Flask dashboard.

## Static Folder Contents

Expected output:

```text
public_outreach/
  robots.txt
  _headers
  assets/
    public_packet.css
  p/
    <token>/
      index.html
      desktop.png
      mobile.png
```

`robots.txt`, `_headers`, and each packet page tell crawlers not to index the packet pages. Tokens are still bearer-style private links, so do not post them publicly.

## Safety Notes

- Host `public_outreach/` only.
- Keep the Flask dashboard on `127.0.0.1`.
- Do not upload `.env`, `data/leads.db`, `runs/`, `screenshots/`, or dashboard templates.
- Rotating a packet token creates a new URL, but old static folders may still exist until manually removed from the deployed static output.
- Cloudflare Pages may cache files. The generated `_headers` includes `Cache-Control: private, no-cache`, but free-tier or default Cloudflare behavior may still override some edge caching details. Verify headers in browser DevTools after the first deploy, especially if token rotation becomes routine.
