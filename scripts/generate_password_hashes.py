"""Generate environment-variable password hashes for configured auth users."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.auth import load_user_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Werkzeug password hashes for config/users.yaml users.",
    )
    parser.add_argument(
        "--users",
        help="Comma-separated usernames to generate, such as QWHITE,JROSS.",
    )
    parser.add_argument(
        "--generate-secret",
        action="store_true",
        help="Also generate an APP_SECRET_KEY value.",
    )
    parser.add_argument(
        "--write-env-example",
        action="store_true",
        help="Opt-in: write generated values into .env.example instead of only printing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_user_config()
    configured_users = config["users"]
    selected_usernames = _selected_usernames(args.users, configured_users)

    assignments: dict[str, str] = {}
    if args.generate_secret:
        assignments["APP_SECRET_KEY"] = secrets.token_urlsafe(48)

    for username in selected_usernames:
        details = configured_users[username]
        password = _prompt_password(username, details["display_name"])
        assignments[details["password_hash_env"]] = generate_password_hash(password)

    if not assignments:
        print("No values generated.")
        return 0

    print("\nEnvironment variable assignments:\n")
    for key, value in assignments.items():
        print(f"{key}={value}")

    print("\nRailway CLI examples:\n")
    for key, value in assignments.items():
        print(f"railway variables set {key}='{value}'")

    if args.write_env_example:
        _write_env_example(assignments)
        print(f"\nWrote generated values to {PROJECT_ROOT / '.env.example'}")

    return 0


def _selected_usernames(
    requested_users: str | None,
    configured_users: dict[str, dict[str, object]],
) -> list[str]:
    if not requested_users:
        return list(configured_users)

    selected = [part.strip().upper() for part in requested_users.split(",") if part.strip()]
    unknown = [username for username in selected if username not in configured_users]
    if unknown:
        known = ", ".join(configured_users)
        raise SystemExit(f"Unknown user(s): {', '.join(unknown)}. Known users: {known}")
    return selected


def _prompt_password(username: str, display_name: str) -> str:
    while True:
        password = getpass.getpass(f"Password for {username} ({display_name}): ")
        if not password:
            print("Password cannot be blank.", file=sys.stderr)
            continue

        confirm = getpass.getpass(f"Confirm password for {username}: ")
        if password != confirm:
            print("Passwords did not match; try again.", file=sys.stderr)
            continue
        return password


def _write_env_example(assignments: dict[str, str]) -> None:
    path = PROJECT_ROOT / ".env.example"
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output_lines: list[str] = []

    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in assignments and not line.lstrip().startswith("#"):
            output_lines.append(f"{key}={assignments[key]}")
            seen.add(key)
        else:
            output_lines.append(line)

    missing_keys = [key for key in assignments if key not in seen]
    if missing_keys:
        if output_lines and output_lines[-1].strip():
            output_lines.append("")
        output_lines.append("# Generated auth values")
        for key in missing_keys:
            output_lines.append(f"{key}={assignments[key]}")

    path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
