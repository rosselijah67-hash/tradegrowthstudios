"""Generate one password hash for any dashboard username."""

from __future__ import annotations

import argparse
import getpass
import re

from werkzeug.security import generate_password_hash


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate one dashboard password hash.")
    parser.add_argument("--username", help="Dashboard username, such as QWHITE or NEWUSER.")
    return parser


def normalize_username(value: str | None) -> str:
    username = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", username):
        raise SystemExit("Username must be 2-32 chars: A-Z, 0-9, underscore, starting with a letter.")
    return username


def env_var_for_username(username: str) -> str:
    safe_username = re.sub(r"[^A-Z0-9_]+", "_", username.strip().upper()).strip("_")
    return f"AUTH_{safe_username}_PASSWORD_HASH"


def prompt_password(username: str) -> str:
    while True:
        password = getpass.getpass(f"Password for {username}: ")
        if not password:
            print("Password cannot be blank.")
            continue
        confirm = getpass.getpass(f"Confirm password for {username}: ")
        if password != confirm:
            print("Passwords did not match; try again.")
            continue
        return password


def main() -> int:
    args = build_parser().parse_args()
    raw_username = args.username or input("Username: ")
    username = normalize_username(raw_username)
    env_var = env_var_for_username(username)
    password_hash = generate_password_hash(prompt_password(username))

    print("")
    print("Railway variable name:")
    print(env_var)
    print("")
    print("Railway variable value:")
    print(password_hash)
    print("")
    print("Railway CLI:")
    print(f"railway variables set {env_var}='{password_hash}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
