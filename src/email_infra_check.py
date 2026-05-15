"""Preflight checks for outbound email infrastructure.

This command does not inspect outreach candidates or create outreach events.
It only validates local configuration, DNS posture, and optional SMTP access.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any

from .config import load_env, load_yaml_config, project_path


JSON_REPORT_PATH = "runs/latest/email_infra_check.json"
TEXT_REPORT_PATH = "runs/latest/email_infra_check.txt"
DEFAULT_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class CheckResult:
    category: str
    check: str
    status: str
    detail: str


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check SMTP, DNS, compliance config, and public packet link readiness."
    )
    parser.add_argument(
        "--domain",
        default=None,
        help="Sending domain to check. Defaults to OUTREACH_FROM_EMAIL domain.",
    )
    parser.add_argument(
        "--test-smtp",
        action="store_true",
        help="Connect and authenticate to SMTP, but do not send mail.",
    )
    parser.add_argument(
        "--send-test-to",
        default=None,
        help="Send one infrastructure test email to this exact address.",
    )
    return parser


def _load_outreach_config() -> dict[str, Any]:
    try:
        return load_yaml_config("outreach.yaml")
    except FileNotFoundError:
        return {}


def _defaults(config: dict[str, Any]) -> dict[str, Any]:
    defaults = config.get("defaults") if isinstance(config, dict) else {}
    return defaults if isinstance(defaults, dict) else {}


def _config_value(
    env_keys: str | list[str],
    *,
    defaults: dict[str, Any] | None = None,
    default_key: str | None = None,
    root_config: dict[str, Any] | None = None,
    root_key: str | None = None,
) -> str | None:
    keys = [env_keys] if isinstance(env_keys, str) else env_keys
    for key in keys:
        value = os.environ.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    if defaults is not None and default_key:
        value = defaults.get(default_key)
        if value is not None and str(value).strip():
            return str(value).strip()

    if root_config is not None and root_key:
        value = root_config.get(root_key)
        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _valid_email(value: str | None) -> str | None:
    if not value:
        return None
    _name, address = parseaddr(value)
    address = address.strip().lower()
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        return None
    local, domain = address.rsplit("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return address


def _domain_from_email(email: str | None) -> str | None:
    normalized = _valid_email(email)
    if not normalized:
        return None
    return normalized.rsplit("@", 1)[1]


def _smtp_port(value: str | None) -> int | None:
    try:
        port = int(str(value or "").strip())
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _smtp_starttls(port: int, config: dict[str, Any], defaults: dict[str, Any]) -> bool:
    configured = _config_value(
        ["SMTP_STARTTLS", "OUTREACH_SMTP_STARTTLS"],
        defaults=defaults,
        default_key="smtp_starttls",
        root_config=config,
        root_key="smtp_starttls",
    )
    if configured is None:
        return port != 465
    return _truthy(configured)


def _smtp_config(config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    port = _smtp_port(_config_value("SMTP_PORT"))
    # Contains raw SMTP credentials; never serialize this dict into reports.
    return {
        "host": _config_value("SMTP_HOST"),
        "port": port,
        "username": _config_value("SMTP_USERNAME"),
        "password": _config_value("SMTP_PASSWORD"),
        "from_email": _valid_email(_config_value("OUTREACH_FROM_EMAIL")),
        "from_name": _config_value(
            "OUTREACH_FROM_NAME",
            defaults=defaults,
            default_key="from_name",
        ),
        "starttls": _smtp_starttls(port or 587, config, defaults),
        "timeout": DEFAULT_TIMEOUT_SECONDS,
    }


def _public_packet_base_url(config: dict[str, Any], defaults: dict[str, Any]) -> str | None:
    return _config_value(
        "PUBLIC_PACKET_BASE_URL",
        defaults=defaults,
        default_key="public_packet_base_url",
        root_config=config,
        root_key="PUBLIC_PACKET_BASE_URL",
    )


def _dkim_selector(config: dict[str, Any], defaults: dict[str, Any]) -> str | None:
    return _config_value(
        "DKIM_SELECTOR",
        defaults=defaults,
        default_key="dkim_selector",
        root_config=config,
        root_key="DKIM_SELECTOR",
    )


def _compliance_config(config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, str | None]:
    return {
        "physical_address": _config_value(
            ["PHYSICAL_MAILING_ADDRESS", "OUTREACH_PHYSICAL_ADDRESS"],
            defaults=defaults,
            default_key="physical_address",
        ),
        "unsubscribe_email": _valid_email(
            _config_value(
                ["UNSUBSCRIBE_EMAIL", "OUTREACH_UNSUBSCRIBE_EMAIL"],
                defaults=defaults,
                default_key="unsubscribe_email",
            )
        ),
    }


def _add_config_checks(
    results: list[CheckResult],
    *,
    smtp: dict[str, Any],
    compliance: dict[str, str | None],
    public_packet_base_url: str | None,
) -> None:
    required = [
        ("SMTP_HOST", smtp.get("host"), "SMTP server host configured"),
        ("SMTP_PORT", smtp.get("port"), "SMTP server port configured and valid"),
        ("SMTP_USERNAME", smtp.get("username"), "SMTP username configured"),
        ("SMTP_PASSWORD", smtp.get("password"), "SMTP password configured"),
        ("OUTREACH_FROM_EMAIL", smtp.get("from_email"), "sender email configured and valid"),
        ("OUTREACH_FROM_NAME", smtp.get("from_name"), "sender display name configured"),
        (
            "PHYSICAL_MAILING_ADDRESS",
            compliance.get("physical_address"),
            "physical mailing address configured",
        ),
        (
            "UNSUBSCRIBE_EMAIL",
            compliance.get("unsubscribe_email"),
            "unsubscribe mailbox configured and valid",
        ),
        (
            "PUBLIC_PACKET_BASE_URL",
            public_packet_base_url,
            "public packet base URL configured",
        ),
    ]
    for name, value, detail in required:
        if value:
            results.append(CheckResult("config", name, "PASS", detail))
        else:
            results.append(CheckResult("config", name, "FAIL", f"{name} is missing or invalid"))


def _resolve_txt(resolver: Any, name: str) -> list[str]:
    answers = resolver.resolve(name, "TXT")
    output: list[str] = []
    for answer in answers:
        chunks = getattr(answer, "strings", None)
        if chunks is None:
            output.append(str(answer).strip('"'))
        else:
            output.append("".join(part.decode("utf-8", "replace") for part in chunks))
    return output


def _add_dns_checks(
    results: list[CheckResult],
    *,
    domain: str | None,
    dkim_selector: str | None,
) -> None:
    if not domain:
        results.append(
            CheckResult(
                "dns",
                "sending_domain",
                "FAIL",
                "No sending domain available. Provide --domain or OUTREACH_FROM_EMAIL.",
            )
        )
        return

    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        results.append(
            CheckResult(
                "dns",
                "dnspython",
                "FAIL",
                "dnspython is not installed. Run: pip install -r requirements.txt",
            )
        )
        return

    resolver = dns.resolver.Resolver()
    resolver.lifetime = 8
    resolver.timeout = 4

    try:
        mx_records = resolver.resolve(domain, "MX")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException) as exc:
        results.append(CheckResult("dns", "MX", "FAIL", f"No usable MX record found for {domain}: {exc}"))
    else:
        results.append(CheckResult("dns", "MX", "PASS", f"{len(mx_records)} MX record(s) found"))

    try:
        txt_records = _resolve_txt(resolver, domain)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException) as exc:
        results.append(CheckResult("dns", "SPF", "FAIL", f"No TXT records available for SPF check: {exc}"))
    else:
        spf_records = [record for record in txt_records if record.lower().startswith("v=spf1")]
        if spf_records:
            results.append(CheckResult("dns", "SPF", "PASS", "SPF TXT record found"))
        else:
            results.append(CheckResult("dns", "SPF", "FAIL", "No SPF TXT record found"))

    dmarc_name = f"_dmarc.{domain}"
    try:
        dmarc_txt = _resolve_txt(resolver, dmarc_name)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException) as exc:
        results.append(CheckResult("dns", "DMARC", "FAIL", f"No DMARC TXT record found: {exc}"))
    else:
        dmarc_records = [record for record in dmarc_txt if record.lower().startswith("v=dmarc1")]
        if dmarc_records:
            results.append(CheckResult("dns", "DMARC", "PASS", "DMARC TXT record found"))
        else:
            results.append(CheckResult("dns", "DMARC", "FAIL", "No DMARC TXT record found"))

    if not dkim_selector:
        results.append(
            CheckResult(
                "dns",
                "DKIM",
                "WARN",
                "DKIM selector not configured; generic DKIM lookup skipped",
            )
        )
        return

    dkim_name = f"{dkim_selector}._domainkey.{domain}"
    try:
        dkim_txt = _resolve_txt(resolver, dkim_name)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.exception.DNSException) as exc:
        results.append(
            CheckResult("dns", "DKIM", "FAIL", f"No DKIM TXT record found for {dkim_name}: {exc}")
        )
    else:
        if dkim_txt:
            results.append(CheckResult("dns", "DKIM", "PASS", f"DKIM TXT record found for {dkim_name}"))
        else:
            results.append(CheckResult("dns", "DKIM", "FAIL", f"No DKIM TXT record found for {dkim_name}"))


def _login_if_configured(smtp: smtplib.SMTP, smtp_config: dict[str, Any]) -> None:
    username = smtp_config.get("username")
    password = smtp_config.get("password")
    if username or password:
        smtp.login(username or "", password or "")


def _connect_smtp(smtp_config: dict[str, Any]) -> smtplib.SMTP:
    host = smtp_config.get("host")
    port = int(smtp_config.get("port") or 587)
    timeout = int(smtp_config.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    if not host:
        raise RuntimeError("SMTP_HOST is not configured")

    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        _login_if_configured(smtp, smtp_config)
        return smtp

    smtp = smtplib.SMTP(host, port, timeout=timeout)
    smtp.ehlo()
    if smtp_config.get("starttls") and smtp.has_extn("starttls"):
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
    _login_if_configured(smtp, smtp_config)
    return smtp


def _build_test_message(smtp_config: dict[str, Any], recipient: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Outbound infrastructure test"
    message["From"] = formataddr((smtp_config.get("from_name") or "", smtp_config["from_email"]))
    message["To"] = recipient
    message.set_content(
        "\n".join(
            [
                "This is an outbound infrastructure test email.",
                f"Sent from: {smtp_config.get('from_name') or ''} <{smtp_config['from_email']}>",
                "",
                "It is not outreach, not a campaign email, and not connected to any prospect.",
            ]
        )
    )
    return message


def _add_smtp_check(
    results: list[CheckResult],
    *,
    smtp_config: dict[str, Any],
    test_smtp: bool,
    send_test_to: str | None,
) -> bool:
    if not test_smtp and not send_test_to:
        results.append(
            CheckResult(
                "smtp",
                "SMTP test",
                "WARN",
                "SMTP connection not tested. Use --test-smtp or --send-test-to to test it.",
            )
        )
        return False

    recipient = _valid_email(send_test_to) if send_test_to else None
    if send_test_to and not recipient:
        results.append(CheckResult("smtp", "test recipient", "FAIL", "--send-test-to is not a valid email"))
        return True

    if not smtp_config.get("from_email"):
        results.append(CheckResult("smtp", "sender", "FAIL", "OUTREACH_FROM_EMAIL is required for SMTP test"))
        return True

    try:
        with _connect_smtp(smtp_config) as smtp:
            if recipient:
                smtp.send_message(_build_test_message(smtp_config, recipient))
                results.append(
                    CheckResult(
                        "smtp",
                        "test send",
                        "PASS",
                        f"One infrastructure test email sent to {recipient}",
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "smtp",
                        "connection/login",
                        "PASS",
                        "SMTP connection and login completed; no email was sent",
                    )
                )
    except Exception as exc:
        action = "test send" if recipient else "connection/login"
        results.append(CheckResult("smtp", action, "FAIL", str(exc)))
        return True
    return False


def _format_table(results: list[CheckResult]) -> str:
    rows = [["STATUS", "CATEGORY", "CHECK", "DETAIL"]]
    rows.extend([["[" + item.status + "]", item.category, item.check, item.detail] for item in results])
    widths = [max(len(str(row[index])) for row in rows) for index in range(4)]

    lines = []
    for index, row in enumerate(rows):
        line = "  ".join(str(row[column]).ljust(widths[column]) for column in range(4))
        lines.append(line.rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def _write_reports(results: list[CheckResult], *, domain: str | None, exit_code: int) -> None:
    latest_dir = project_path("runs/latest")
    latest_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "domain": domain,
        "exit_code": exit_code,
        "results": [asdict(result) for result in results],
    }
    project_path(JSON_REPORT_PATH).write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    project_path(TEXT_REPORT_PATH).write_text(_format_table(results) + "\n", encoding="utf-8")


def _exit_code(results: list[CheckResult], *, smtp_failed: bool) -> int:
    if smtp_failed:
        return 2
    if any(result.status == "FAIL" for result in results):
        return 1
    return 0


def main() -> int:
    args = build_arg_parser().parse_args()
    load_env()
    config = _load_outreach_config()
    defaults = _defaults(config)
    smtp_config = _smtp_config(config, defaults)
    compliance = _compliance_config(config, defaults)
    public_base_url = _public_packet_base_url(config, defaults)
    domain = (args.domain or _domain_from_email(smtp_config.get("from_email")) or "").strip().lower() or None

    results: list[CheckResult] = []
    _add_config_checks(
        results,
        smtp=smtp_config,
        compliance=compliance,
        public_packet_base_url=public_base_url,
    )
    _add_dns_checks(results, domain=domain, dkim_selector=_dkim_selector(config, defaults))
    smtp_failed = _add_smtp_check(
        results,
        smtp_config=smtp_config,
        test_smtp=args.test_smtp,
        send_test_to=args.send_test_to,
    )
    exit_code = _exit_code(results, smtp_failed=smtp_failed)
    _write_reports(results, domain=domain, exit_code=exit_code)

    print(_format_table(results))
    print()
    print(f"Wrote {JSON_REPORT_PATH}")
    print(f"Wrote {TEXT_REPORT_PATH}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
