# Email Infrastructure Setup

This project can prepare outreach, but real sending should wait until the outbound mailbox, DNS, compliance fields, and public packet links are configured. Use `src.email_infra_check` as the preflight gate before any small B2B cold outreach test.

The dashboard does not expose email sending. Keep it that way until the sending setup is proven.

## Recommended Sending Setup

Use a dedicated outbound domain or mailbox, not your primary personal inbox. A simple pattern is:

```text
hello@your-outbound-domain.com
```

or:

```text
ross@audit.yourdomain.com
```

Avoid using a free personal Gmail account for serious outbound. Provider rules, deliverability, and suspension risk are all worse when a personal/free mailbox is used for commercial cold outreach.

## Required Configuration

Set these in `.env`:

```env
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
OUTREACH_FROM_EMAIL=
OUTREACH_FROM_NAME=

PHYSICAL_MAILING_ADDRESS=
UNSUBSCRIBE_EMAIL=

PUBLIC_PACKET_BASE_URL=https://audit.example.com
```

`PHYSICAL_MAILING_ADDRESS` and `UNSUBSCRIBE_EMAIL` may also be configured as `physical_address` and `unsubscribe_email` under `defaults` in `config/outreach.yaml`.

If your SMTP provider gives you a DKIM selector, add:

```env
DKIM_SELECTOR=selector1
```

## DNS Records

Configure DNS for the sending domain:

- MX record so the domain can receive mail.
- SPF TXT record authorizing your SMTP provider.
- DKIM TXT record from your SMTP provider.
- DMARC TXT record at `_dmarc.yourdomain.com`.

The checker can validate MX, SPF, and DMARC generically. DKIM can only be checked when `DKIM_SELECTOR` is configured.

## Public Packet Links

Public audit packets are generated under `public_outreach/` and deployed separately from the local dashboard. Set:

```env
PUBLIC_PACKET_BASE_URL=https://audit.example.com
```

Then packet URLs can be composed as:

```text
PUBLIC_PACKET_BASE_URL + /p/<token>/
```

Do not expose the Flask dashboard to make packet links work. Only deploy the static `public_outreach/` folder.

## Run The Preflight

After pulling this change, refresh dependencies so DNS checks can run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Default check:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check
```

Check a specific sending domain:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --domain example.com
```

Test SMTP login without sending:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --test-smtp
```

Send one test email to yourself:

```powershell
.\.venv\Scripts\python.exe -m src.email_infra_check --send-test-to you@example.com
```

The test email subject is `Outbound infrastructure test`, and the body says it is a test, identifies the configured sender, and says it is not outreach. No prospect records or outreach events are touched.

Reports are written to:

```text
runs/latest/email_infra_check.json
runs/latest/email_infra_check.txt
```

## Exit Codes

- `0`: pass or warnings only.
- `1`: required config is missing or DNS has a critical failure.
- `2`: SMTP test/send was requested and failed.

## Practical Sending Rules

- Send low volume first.
- Do not use tracking pixels.
- Do not attach screenshots by default; link to public packets instead.
- Use a real unsubscribe mailbox and monitor it.
- Include a physical mailing address when sending commercial outreach.
- Check acceptable-use policies for your SMTP provider before using it for cold outreach.
- Keep daily caps conservative until replies, bounces, and spam complaints are understood.
