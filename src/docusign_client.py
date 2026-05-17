"""DocuSign eSignature client foundation.

This module intentionally has no dashboard wiring and no import-time SDK
dependency. Payload previews and configuration validation can run locally
without contacting DocuSign; token, send, and status calls are isolated in the
explicit functions below.
"""

from __future__ import annotations

import base64
import copy
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import PROJECT_ROOT, load_env, project_path

DOCUSIGN_CONFIG_KEYS = (
    "DOCUSIGN_ENVIRONMENT",
    "DOCUSIGN_AUTH_SERVER",
    "DOCUSIGN_BASE_PATH",
    "DOCUSIGN_ACCOUNT_ID",
    "DOCUSIGN_INTEGRATION_KEY",
    "DOCUSIGN_USER_ID",
    "DOCUSIGN_RSA_PRIVATE_KEY",
    "DOCUSIGN_RSA_PRIVATE_KEY_PATH",
    "DOCUSIGN_SCOPES",
)

DEFAULT_ENVIRONMENT = "demo"
DEFAULT_DEMO_AUTH_SERVER = "account-d.docusign.com"
DEFAULT_PRODUCTION_AUTH_SERVER = "account.docusign.com"
DEFAULT_DEMO_BASE_PATH = "https://demo.docusign.net/restapi"
DEFAULT_PRODUCTION_BASE_PATH = "https://www.docusign.net/restapi"
DEFAULT_PRIVATE_KEY_PATH = "secrets/docusign_private_key.pem"
DEFAULT_SCOPES = ("signature", "impersonation")
VALID_ENVELOPE_STATUSES = {"created", "sent"}

CLIENT_ANCHORS = {
    1: {"sign": "/tgs_client1_sign/", "date": "/tgs_client1_date/"},
    2: {"sign": "/tgs_client2_sign/", "date": "/tgs_client2_date/"},
    3: {"sign": "/tgs_client3_sign/", "date": "/tgs_client3_date/"},
}
PROVIDER_ANCHORS = {"sign": "/tgs_provider_sign/", "date": "/tgs_provider_date/"}
ANCHOR_TAB_OFFSETS = {
    "sign": {"x_offset": "-170", "y_offset": "-18"},
    "date": {"x_offset": "-92", "y_offset": "-14"},
}


class DocusignConfigurationError(ValueError):
    """Raised when DocuSign configuration is missing or unsafe."""


class DocusignDependencyError(ImportError):
    """Raised when an explicit DocuSign API call needs the SDK dependency."""


@dataclass(frozen=True)
class DocusignConfig:
    environment: str
    auth_server: str
    base_path: str
    account_id: str
    integration_key: str
    user_id: str
    rsa_private_key_pem: str
    rsa_private_key_path: Path
    scopes: tuple[str, ...]


def load_docusign_config(env_path: str | Path | None = None) -> DocusignConfig:
    """Load DocuSign settings from environment variables.

    Values are loaded for use by callers only; this function never logs or
    prints secrets.
    """

    load_env(env_path)
    environment = _clean_text(os.environ.get("DOCUSIGN_ENVIRONMENT")) or DEFAULT_ENVIRONMENT
    normalized_environment = _normalize_environment(environment)
    auth_server_default = (
        DEFAULT_PRODUCTION_AUTH_SERVER
        if normalized_environment == "production"
        else DEFAULT_DEMO_AUTH_SERVER
    )
    base_path_default = (
        DEFAULT_PRODUCTION_BASE_PATH
        if normalized_environment == "production"
        else DEFAULT_DEMO_BASE_PATH
    )
    private_key_path = _clean_text(os.environ.get("DOCUSIGN_RSA_PRIVATE_KEY_PATH"))
    private_key_pem = _normalize_private_key_pem(os.environ.get("DOCUSIGN_RSA_PRIVATE_KEY"))
    return DocusignConfig(
        environment=normalized_environment,
        auth_server=_strip_url_scheme(
            _clean_text(os.environ.get("DOCUSIGN_AUTH_SERVER")) or auth_server_default
        ),
        base_path=(
            _clean_text(os.environ.get("DOCUSIGN_BASE_PATH")) or base_path_default
        ).rstrip("/"),
        account_id=_clean_text(os.environ.get("DOCUSIGN_ACCOUNT_ID")) or "",
        integration_key=_clean_text(os.environ.get("DOCUSIGN_INTEGRATION_KEY")) or "",
        user_id=_clean_text(os.environ.get("DOCUSIGN_USER_ID")) or "",
        rsa_private_key_pem=private_key_pem,
        rsa_private_key_path=project_path(private_key_path or DEFAULT_PRIVATE_KEY_PATH),
        scopes=_parse_scopes(os.environ.get("DOCUSIGN_SCOPES")),
    )


def validate_docusign_config(
    config: DocusignConfig | None = None,
    *,
    check_private_key_file: bool = True,
) -> list[str]:
    """Return configuration problems without making a DocuSign API call."""

    loaded = config or load_docusign_config()
    errors: list[str] = []

    if loaded.environment not in {"demo", "production"}:
        errors.append("DOCUSIGN_ENVIRONMENT must be demo or production.")
    if not loaded.auth_server:
        errors.append("DOCUSIGN_AUTH_SERVER is required.")
    if loaded.auth_server.startswith(("http://", "https://")) or "/" in loaded.auth_server:
        errors.append("DOCUSIGN_AUTH_SERVER must be a host name, not a URL.")
    if not loaded.base_path.startswith(("https://", "http://")):
        errors.append("DOCUSIGN_BASE_PATH must be an absolute URL.")
    if not loaded.account_id:
        errors.append("DOCUSIGN_ACCOUNT_ID is required.")
    if not loaded.integration_key:
        errors.append("DOCUSIGN_INTEGRATION_KEY is required.")
    if not loaded.user_id:
        errors.append("DOCUSIGN_USER_ID is required for JWT impersonation.")
    if loaded.rsa_private_key_pem:
        if "PRIVATE KEY" not in loaded.rsa_private_key_pem:
            errors.append("DOCUSIGN_RSA_PRIVATE_KEY does not look like a PEM private key.")
    elif not loaded.rsa_private_key_path:
        errors.append("DOCUSIGN_RSA_PRIVATE_KEY or DOCUSIGN_RSA_PRIVATE_KEY_PATH is required.")
    elif check_private_key_file and not loaded.rsa_private_key_path.exists():
        errors.append("DOCUSIGN_RSA_PRIVATE_KEY_PATH does not point to an existing file.")
    missing_scopes = [scope for scope in DEFAULT_SCOPES if scope not in loaded.scopes]
    if missing_scopes:
        errors.append("DOCUSIGN_SCOPES must include signature and impersonation.")

    return errors


def get_private_key_bytes(config: DocusignConfig | None = None) -> bytes:
    """Read the configured RSA private key bytes without logging them."""

    loaded = config or load_docusign_config()
    errors = validate_docusign_config(loaded, check_private_key_file=True)
    if errors:
        raise DocusignConfigurationError("; ".join(errors))

    if loaded.rsa_private_key_pem:
        key_bytes = loaded.rsa_private_key_pem.encode("utf-8")
        source_label = "DOCUSIGN_RSA_PRIVATE_KEY"
    else:
        key_bytes = loaded.rsa_private_key_path.read_bytes()
        source_label = "DOCUSIGN_RSA_PRIVATE_KEY_PATH"
    if not key_bytes.strip():
        raise DocusignConfigurationError(f"{source_label} is empty.")
    if b"PRIVATE KEY" not in key_bytes:
        raise DocusignConfigurationError(
            f"{source_label} does not look like a PEM private key."
        )
    return key_bytes


def get_api_client(
    config: DocusignConfig | None = None,
    *,
    access_token: str | None = None,
) -> Any:
    """Build a DocuSign SDK API client without requesting a token."""

    loaded = config or load_docusign_config()
    errors = validate_docusign_config(loaded, check_private_key_file=False)
    if errors:
        raise DocusignConfigurationError("; ".join(errors))

    docusign_esign = _require_docusign_sdk()
    api_client = docusign_esign.ApiClient(
        host=loaded.base_path,
        oauth_host_name=loaded.auth_server,
    )
    if access_token:
        api_client.set_default_header("Authorization", f"Bearer {access_token}")
    return api_client


def get_jwt_access_token(
    config: DocusignConfig | None = None,
    *,
    expires_in: int = 3600,
) -> str:
    """Request a JWT impersonation access token.

    This is an explicit network call to DocuSign. The token is returned to the
    caller and is never logged.
    """

    loaded = config or load_docusign_config()
    key_bytes = get_private_key_bytes(loaded)
    api_client = get_api_client(loaded)
    token = api_client.request_jwt_user_token(
        loaded.integration_key,
        loaded.user_id,
        loaded.auth_server,
        key_bytes,
        expires_in,
        list(loaded.scopes),
    )
    access_token = _clean_text(getattr(token, "access_token", None))
    if not access_token:
        raise DocusignConfigurationError("DocuSign JWT response did not include an access token.")
    return access_token


def build_document_from_file(path: str | Path) -> dict[str, Any]:
    """Build a DocuSign document payload from an existing local file."""

    document_path = Path(path)
    if not document_path.is_absolute():
        document_path = PROJECT_ROOT / document_path
    if not document_path.exists() or not document_path.is_file():
        raise FileNotFoundError(f"Document does not exist: {document_path}")

    suffix = document_path.suffix.lower().lstrip(".")
    file_extension = suffix or "pdf"
    return {
        "documentBase64": base64.b64encode(document_path.read_bytes()).decode("ascii"),
        "documentId": "1",
        "fileExtension": file_extension,
        "name": document_path.name,
    }


def build_signer_recipient(
    signer: Mapping[str, Any],
    recipient_id: int | str,
    routing_order: int | str,
) -> dict[str, Any]:
    """Build a DocuSign signer recipient with anchor tabs."""

    normalized = _normalize_signer(signer)
    if not normalized["name"]:
        raise ValueError("Signer name is required.")
    if not normalized["email"]:
        raise ValueError("Signer email is required.")

    signer_index = normalized.get("_anchor_index") or _int_value(recipient_id, default=1)
    recipient = {
        "email": normalized["email"],
        "name": normalized["name"],
        "recipientId": str(recipient_id),
        "routingOrder": str(routing_order),
        "tabs": build_anchor_tabs_for_signer(normalized, signer_index),
    }
    return recipient


def build_anchor_tabs_for_signer(
    signer: Mapping[str, Any],
    signer_index: int,
) -> dict[str, list[dict[str, Any]]]:
    """Build signature/date tabs for a configured client or provider signer."""

    anchors = PROVIDER_ANCHORS if _is_provider_signer(signer) else CLIENT_ANCHORS.get(signer_index)
    if anchors is None:
        raise ValueError("Built-in DocuSign anchors support up to three client signers.")

    label_prefix = "provider" if anchors is PROVIDER_ANCHORS else f"client{signer_index}"
    sign_offsets = ANCHOR_TAB_OFFSETS["sign"]
    date_offsets = ANCHOR_TAB_OFFSETS["date"]
    return {
        "signHereTabs": [
            _anchor_tab(
                anchors["sign"],
                tab_label=f"tgs_{label_prefix}_sign",
                x_offset=sign_offsets["x_offset"],
                y_offset=sign_offsets["y_offset"],
            )
        ],
        "dateSignedTabs": [
            _anchor_tab(
                anchors["date"],
                tab_label=f"tgs_{label_prefix}_date",
                x_offset=date_offsets["x_offset"],
                y_offset=date_offsets["y_offset"],
            )
        ],
    }


def build_envelope_definition_from_contract(
    contract: Mapping[str, Any],
    document_path: str | Path,
    signers: Sequence[Mapping[str, Any]] | None,
    email_subject: str | None = None,
    email_blurb: str | None = None,
    status: str = "created",
) -> dict[str, Any]:
    """Build a DocuSign envelope definition from a generated contract document."""

    normalized_status = _normalize_envelope_status(status)
    normalized_signers = _normalize_signers_for_contract(contract, signers)
    if not normalized_signers:
        raise ValueError("At least one signer is required to build a DocuSign envelope.")

    signer_recipients = []
    client_anchor_index = 0
    for recipient_index, signer in enumerate(normalized_signers, start=1):
        anchor_index = 0
        if _is_provider_signer(signer):
            anchor_index = 0
        else:
            requested_anchor_index = _int_value(signer.get("_anchor_index"), default=0)
            if requested_anchor_index in CLIENT_ANCHORS:
                anchor_index = requested_anchor_index
                client_anchor_index = max(client_anchor_index, anchor_index)
            else:
                client_anchor_index += 1
                anchor_index = client_anchor_index
        signer_with_anchor = {**signer, "_anchor_index": anchor_index}
        routing_order = signer.get("routing_order") or signer.get("routingOrder") or recipient_index
        signer_recipients.append(
            build_signer_recipient(
                signer_with_anchor,
                recipient_id=recipient_index,
                routing_order=routing_order,
            )
        )

    contract_key = _clean_text(contract.get("contract_key")) or _clean_text(contract.get("id"))
    envelope = {
        "emailSubject": email_subject or _default_email_subject(contract),
        "documents": [build_document_from_file(document_path)],
        "recipients": {"signers": signer_recipients},
        "status": normalized_status,
    }
    if email_blurb:
        envelope["emailBlurb"] = email_blurb
    if contract_key:
        envelope["customFields"] = {
            "textCustomFields": [
                {
                    "name": "contract_key",
                    "required": "false",
                    "show": "false",
                    "value": contract_key,
                }
            ]
        }
    return envelope


def send_envelope_from_document(
    contract: Mapping[str, Any],
    document_path: str | Path,
    signers: Sequence[Mapping[str, Any]] | None,
    status: str = "sent",
) -> dict[str, Any]:
    """Create a DocuSign envelope from a generated document.

    This function makes explicit DocuSign network calls. It does not mutate
    SQLite or any local CRM state.
    """

    loaded = load_docusign_config()
    access_token = get_jwt_access_token(loaded)
    api_client = get_api_client(loaded, access_token=access_token)
    envelope_definition = build_envelope_definition_from_contract(
        contract,
        document_path,
        signers,
        status=status,
    )
    envelopes_api = _require_docusign_sdk().EnvelopesApi(api_client)
    summary = envelopes_api.create_envelope(
        loaded.account_id,
        envelope_definition=envelope_definition,
    )
    safe_summary = _sdk_object_to_dict(summary)
    return {
        "envelope_id": safe_summary.get("envelopeId") or safe_summary.get("envelope_id"),
        "status": safe_summary.get("status"),
        "status_date_time": safe_summary.get("statusDateTime")
        or safe_summary.get("status_date_time"),
        "uri": safe_summary.get("uri"),
    }


def get_envelope_status(envelope_id: str) -> dict[str, Any]:
    """Fetch envelope status from DocuSign without mutating local state."""

    clean_envelope_id = _clean_text(envelope_id)
    if not clean_envelope_id:
        raise ValueError("envelope_id is required.")

    loaded = load_docusign_config()
    access_token = get_jwt_access_token(loaded)
    api_client = get_api_client(loaded, access_token=access_token)
    envelope = _require_docusign_sdk().EnvelopesApi(api_client).get_envelope(
        loaded.account_id,
        clean_envelope_id,
    )
    payload = _sdk_object_to_dict(envelope)
    return {
        "envelope_id": payload.get("envelopeId") or payload.get("envelope_id") or clean_envelope_id,
        "status": payload.get("status"),
        "status_changed_date_time": payload.get("statusChangedDateTime")
        or payload.get("status_changed_date_time"),
        "sent_date_time": payload.get("sentDateTime") or payload.get("sent_date_time"),
        "completed_date_time": payload.get("completedDateTime")
        or payload.get("completed_date_time"),
    }


def build_redacted_envelope_payload_preview(
    contract: Mapping[str, Any],
    document_path: str | Path,
    signers: Sequence[Mapping[str, Any]] | None,
    email_subject: str | None = None,
    email_blurb: str | None = None,
    status: str = "created",
) -> dict[str, Any]:
    """Build a redacted JSON-safe preview without calling DocuSign."""

    payload = build_envelope_definition_from_contract(
        contract,
        document_path,
        signers,
        email_subject=email_subject,
        email_blurb=email_blurb,
        status=status,
    )
    preview = _redact_envelope_payload(payload)
    preview["previewGeneratedAt"] = datetime.now(timezone.utc).isoformat()
    preview["redactionNotice"] = (
        "documentBase64 is redacted and signer emails are masked; this preview is not sent."
    )
    return preview


def _require_docusign_sdk() -> Any:
    try:
        import docusign_esign
    except ImportError as exc:
        raise DocusignDependencyError(
            "Install the docusign-esign package from requirements.txt before calling DocuSign."
        ) from exc
    return docusign_esign


def _normalize_signers_for_contract(
    contract: Mapping[str, Any],
    signers: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    source = list(signers or [])
    if not source:
        contract_signers = contract.get("signers")
        if isinstance(contract_signers, Sequence) and not isinstance(contract_signers, (str, bytes)):
            source = [item for item in contract_signers if isinstance(item, Mapping)]
    if not source:
        primary_name = (
            _clean_text(contract.get("signer_name"))
            or _clean_text(contract.get("client_contact_name"))
            or _clean_text(contract.get("client_business_name"))
        )
        primary_email = _clean_text(contract.get("signer_email")) or _clean_text(
            contract.get("client_email")
        )
        if primary_name and primary_email:
            source = [
                {
                    "role": "client",
                    "name": primary_name,
                    "email": primary_email,
                    "title": _clean_text(contract.get("signer_title")) or "",
                    "routing_order": 1,
                }
            ]
    return [_normalize_signer(signer) for signer in source]


def _normalize_signer(signer: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "role": _clean_text(signer.get("role")) or "client",
        "name": _clean_text(signer.get("name")) or _clean_text(signer.get("full_name")) or "",
        "email": _clean_text(signer.get("email")) or "",
        "title": _clean_text(signer.get("title")) or "",
        "routing_order": _int_value(
            signer.get("routing_order") or signer.get("routingOrder"),
            default=0,
        ),
        "_anchor_index": _int_value(signer.get("_anchor_index"), default=0),
    }


def _anchor_tab(
    anchor_string: str,
    *,
    tab_label: str,
    x_offset: str,
    y_offset: str,
) -> dict[str, str]:
    return {
        "anchorString": anchor_string,
        "anchorUnits": "pixels",
        "anchorXOffset": x_offset,
        "anchorYOffset": y_offset,
        "tabLabel": tab_label,
    }


def _default_email_subject(contract: Mapping[str, Any]) -> str:
    title = _clean_text(contract.get("title")) or "Service Agreement"
    business = _clean_text(contract.get("client_business_name"))
    if business and business.lower() not in title.lower():
        return f"{title} for {business}"
    return title


def _redact_envelope_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    redacted = copy.deepcopy(dict(payload))
    for document in redacted.get("documents", []) or []:
        if not isinstance(document, dict):
            continue
        encoded = document.get("documentBase64")
        if encoded:
            document["documentBase64"] = f"<redacted {len(str(encoded))} base64 chars>"
    recipients = redacted.get("recipients") or {}
    for signer in recipients.get("signers", []) or []:
        if isinstance(signer, dict) and signer.get("email"):
            signer["email"] = mask_email(str(signer["email"]))
    return redacted


def mask_email(email: str) -> str:
    clean = _clean_text(email) or ""
    if "@" not in clean:
        return "<redacted>"
    local, domain = clean.split("@", 1)
    local_mask = local[:1] + "***" if local else "***"
    domain_parts = domain.split(".")
    if len(domain_parts) >= 2:
        domain_mask = domain_parts[0][:1] + "***." + ".".join(domain_parts[1:])
    else:
        domain_mask = domain[:1] + "***"
    return f"{local_mask}@{domain_mask}"


def _sdk_object_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    output: dict[str, Any] = {}
    for key in (
        "envelope_id",
        "envelopeId",
        "status",
        "status_date_time",
        "statusDateTime",
        "uri",
        "status_changed_date_time",
        "statusChangedDateTime",
        "sent_date_time",
        "sentDateTime",
        "completed_date_time",
        "completedDateTime",
    ):
        if hasattr(value, key):
            output[key] = getattr(value, key)
    return output


def _normalize_envelope_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized not in VALID_ENVELOPE_STATUSES:
        raise ValueError("DocuSign envelope status must be created or sent.")
    return normalized


def _normalize_environment(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"prod", "production", "live"}:
        return "production"
    if normalized in {"demo", "developer", "sandbox"}:
        return "demo"
    return normalized


def _parse_scopes(value: str | None) -> tuple[str, ...]:
    scopes = tuple(scope.strip() for scope in (value or "").split() if scope.strip())
    return scopes or DEFAULT_SCOPES


def _normalize_private_key_pem(value: str | None) -> str:
    clean = str(value or "").strip().strip('"').strip("'")
    if not clean:
        return ""
    # Railway variables may be pasted as a multiline value or as escaped \n text.
    clean = clean.replace("\\r\\n", "\n").replace("\\n", "\n")
    return clean.strip() + "\n"


def _strip_url_scheme(value: str) -> str:
    clean = value.strip().removeprefix("https://").removeprefix("http://")
    return clean.strip("/")


def _is_provider_signer(signer: Mapping[str, Any]) -> bool:
    role = (_clean_text(signer.get("role")) or "").lower()
    return role in {"provider", "company", "service_provider", "tgs_provider"}


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default
