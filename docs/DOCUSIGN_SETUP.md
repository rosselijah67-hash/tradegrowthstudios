# DocuSign Setup

This project uses DocuSign eSignature with JWT impersonation for the first
integration pass. The app-generated contract document must already exist before
an envelope is built or sent.

## Environment

Use demo first:

```text
DOCUSIGN_ENVIRONMENT=demo
DOCUSIGN_AUTH_SERVER=account-d.docusign.com
DOCUSIGN_BASE_PATH=https://demo.docusign.net/restapi
DOCUSIGN_ACCOUNT_ID=
DOCUSIGN_INTEGRATION_KEY=
DOCUSIGN_USER_ID=
DOCUSIGN_RSA_PRIVATE_KEY_PATH=secrets/docusign_private_key.pem
DOCUSIGN_SCOPES=signature impersonation
DOCUSIGN_WEBHOOK_ENABLED=false
DOCUSIGN_WEBHOOK_SECRET=
```

Production uses:

```text
DOCUSIGN_ENVIRONMENT=production
DOCUSIGN_AUTH_SERVER=account.docusign.com
DOCUSIGN_BASE_PATH=https://www.docusign.net/restapi
```

Keep `DOCUSIGN_ACCOUNT_ID`, `DOCUSIGN_INTEGRATION_KEY`, `DOCUSIGN_USER_ID`, and
the RSA private key private. Do not commit `.env` or anything under `secrets/`.

## RSA Key

Store the JWT RSA private key under `secrets/`, for example:

```text
secrets/docusign_private_key.pem
```

The configured path may be absolute or relative to the project root. The client
module reads the private key only inside explicit JWT/API functions and never
prints the key or access token.

## JWT Consent

JWT impersonation requires user consent before token requests will succeed. Grant
consent in the DocuSign developer/admin flow for the integration key, impersonated
user, and scopes:

```text
signature impersonation
```

Until consent is granted, DocuSign token requests can fail even when all env
values are correct.

## First Test

Use the demo environment for the first test. Start with configuration validation
and a redacted payload preview. Those actions do not call DocuSign.

Only call the explicit send/status functions when you intentionally want a
DocuSign API call:

- `get_jwt_access_token()`
- `send_envelope_from_document(...)`
- `get_envelope_status(...)`

Generated contracts must exist before envelope creation. The DocuSign client
does not generate contracts, alter quotes, send app emails, add dashboard routes,
or mutate SQLite.

This first pass sends app-generated documents with anchor tabs. Composite
templates are a future option only if reusable DocuSign template roles/tabs are
needed later.

## Optional Webhook

Manual status refresh works without a webhook. The webhook endpoint is disabled
by default and must fail closed unless both values are configured:

```text
DOCUSIGN_WEBHOOK_ENABLED=true
DOCUSIGN_WEBHOOK_SECRET=<long random shared secret>
```

Endpoint:

```text
POST /webhooks/docusign
```

The dashboard accepts webhook updates only when the request includes the shared
secret in one of these headers:

```text
X-DocuSign-Webhook-Secret: <secret>
X-DocuSign-Connect-Secret: <secret>
X-Webhook-Secret: <secret>
Authorization: Bearer <secret>
```

Configure DocuSign Connect, or a trusted relay in front of it, to send one of
those headers. If that cannot be configured confidently, leave the webhook
disabled and use the contract detail page's manual refresh button.

The webhook stores only envelope/status metadata against an existing
`docusign_envelope_id`; it does not accept arbitrary contract IDs, render
dashboard templates, or log contract contents.
