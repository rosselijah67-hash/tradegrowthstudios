"""Reusable authentication primitives for dashboard users.

Password hashes are read only from environment variables named by
``config/users.yaml``. Sessions should store only a canonical username/user id;
roles, territory lists, plaintext passwords, and password hashes stay out of
the client-side session payload.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TypeVar

from werkzeug.exceptions import Forbidden, Unauthorized
from werkzeug.routing import BuildError
from werkzeug.security import check_password_hash

from .config import _load_simple_yaml, load_env, load_yaml_config

try:  # Flask-Login is optional at import time but supported when installed.
    from flask_login import UserMixin as _UserMixin
except ImportError:  # pragma: no cover - exercised only without Flask-Login.

    class _UserMixin:
        @property
        def is_active(self) -> bool:
            return True

        @property
        def is_authenticated(self) -> bool:
            return True

        @property
        def is_anonymous(self) -> bool:
            return False


logger = logging.getLogger(__name__)

AUTH_SESSION_USER_KEY = "user_id"
USERNAME_SESSION_KEYS = (AUTH_SESSION_USER_KEY, "username")
STATE_CODE_RE = re.compile(r"^[A-Z]{2}$")
F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class User(_UserMixin):
    """Configured application user compatible with Flask-Login loaders."""

    username: str
    role: str
    display_name: str
    allowed_states: tuple[str, ...]
    password_hash_env: str
    password_hash: str | None = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return self.username

    def get_id(self) -> str:
        return self.username

    @property
    def can_login(self) -> bool:
        return bool(self.password_hash)


def load_user_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate configured auth users from YAML."""

    data = _load_yaml_mapping(path) if path else load_yaml_config("users.yaml")
    users = data.get("users")
    if not isinstance(users, Mapping):
        raise ValueError("config/users.yaml must contain a top-level 'users' mapping.")

    normalized_users: dict[str, dict[str, Any]] = {}
    for raw_username, raw_details in users.items():
        username = _canonical_username(raw_username)
        if not username:
            raise ValueError("Usernames in config/users.yaml cannot be blank.")
        if username in normalized_users:
            raise ValueError(f"Duplicate auth user after normalization: {username}")
        if not isinstance(raw_details, Mapping):
            raise ValueError(f"Auth user {username} must be a mapping.")

        role = str(raw_details.get("role") or "").strip().lower()
        if role not in {"admin", "user"}:
            raise ValueError(f"Auth user {username} has unsupported role: {role!r}")

        display_name = str(raw_details.get("display_name") or username).strip() or username
        password_hash_env = str(raw_details.get("password_hash_env") or "").strip()
        if not password_hash_env:
            raise ValueError(f"Auth user {username} is missing password_hash_env.")

        allowed_states = _canonical_allowed_states(
            raw_details.get("allowed_states"),
            role=role,
            username=username,
        )

        normalized_users[username] = {
            "role": role,
            "display_name": display_name,
            "allowed_states": list(allowed_states),
            "password_hash_env": password_hash_env,
        }

    return {"users": normalized_users}


def load_users() -> dict[str, User]:
    """Backward-compatible alias for loading auth users."""

    return load_auth_users()


def load_auth_users() -> dict[str, User]:
    """Return configured users with password hashes hydrated from env vars."""

    load_env()
    config = load_user_config()
    users: dict[str, User] = {}
    for username, details in config["users"].items():
        password_hash_env = details["password_hash_env"]
        password_hash = str(os.environ.get(password_hash_env) or "").strip() or None
        if not password_hash:
            logger.warning(
                "Password hash env %s is not set; user %s cannot log in.",
                password_hash_env,
                username,
            )

        users[username] = User(
            username=username,
            role=details["role"],
            display_name=details["display_name"],
            allowed_states=tuple(details["allowed_states"]),
            password_hash_env=password_hash_env,
            password_hash=password_hash,
        )
    return users


def get_user(username: str | None) -> User | None:
    """Find a configured user by username, case-insensitively."""

    canonical = _canonical_username(username)
    if not canonical:
        return None
    return load_auth_users().get(canonical)


def load_user(user_id: str | None) -> User | None:
    """Flask-Login user_loader-compatible helper."""

    return get_user(user_id)


def verify_password(username: str | None, password: str | None) -> bool:
    """Verify a plaintext login attempt against the configured hash env var."""

    if not password:
        return False

    user = get_user(username)
    if user is None:
        return False
    if not user.password_hash:
        logger.warning(
            "Login rejected for %s because %s is not set.",
            user.username,
            user.password_hash_env,
        )
        return False

    try:
        return check_password_hash(user.password_hash, password)
    except (TypeError, ValueError):
        logger.warning(
            "Login rejected for %s because %s does not contain a valid hash.",
            user.username,
            user.password_hash_env,
        )
        return False


def is_admin(user: User | None) -> bool:
    return bool(user and user.role == "admin")


def user_allowed_states(user: User | None) -> tuple[str, ...]:
    if user is None:
        return ()
    return user.allowed_states


def user_can_access_state(user: User | None, state_code: str | None) -> bool:
    if user is None:
        return False
    if is_admin(user) or "*" in user.allowed_states:
        return True

    normalized_state = _normalize_state_code(state_code)
    if normalized_state is None:
        return False
    return normalized_state in user.allowed_states


def current_app_user() -> User | None:
    """Return the configured user for the active Flask request, if any."""

    try:
        from flask import has_request_context, session
    except ImportError:  # pragma: no cover - Flask is a project dependency.
        return None

    if not has_request_context():
        return None

    flask_login_user = _current_flask_login_user()
    if flask_login_user is not None:
        return flask_login_user

    for session_key in USERNAME_SESSION_KEYS:
        username = session.get(session_key)
        user = get_user(username)
        if user is not None:
            return user

    if session.get("dashboard_authenticated"):
        return get_user(session.get("dashboard_username"))
    return None


def current_user() -> User | None:
    """Backward-compatible alias for the current configured app user."""

    return current_app_user()


def login_user(user: User) -> None:
    """Store only the canonical username in the Flask session."""

    from flask import session

    session.pop("dashboard_authenticated", None)
    session.pop("dashboard_username", None)
    session.pop("role", None)
    session.pop("states", None)
    session.pop("allowed_states", None)
    session.pop("password", None)
    session.pop("password_hash", None)
    session[AUTH_SESSION_USER_KEY] = user.username


def logout_user() -> None:
    """Remove auth identity from the Flask session."""

    from flask import session

    session.pop(AUTH_SESSION_USER_KEY, None)
    session.pop("username", None)
    session.pop("dashboard_authenticated", None)
    session.pop("dashboard_username", None)
    session.pop("role", None)
    session.pop("states", None)
    session.pop("allowed_states", None)
    session.pop("password", None)
    session.pop("password_hash", None)


def require_authenticated(view: F | None = None) -> Callable[[F], F] | F:
    """Decorator for routes that require any configured authenticated user."""

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if current_app_user() is None:
                return _unauthenticated_response()
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    if view is None:
        return decorator
    return decorator(view)


login_required = require_authenticated


def admin_required(view: F | None = None) -> Callable[[F], F] | F:
    """Decorator for routes that require an admin user."""

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            user = current_app_user()
            if user is None:
                return _unauthenticated_response()
            if not is_admin(user):
                raise Forbidden()
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    if view is None:
        return decorator
    return decorator(view)


def _load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    yaml_path = Path(path)
    text = yaml_path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        loaded = _load_simple_yaml(text)
    else:
        loaded = yaml.safe_load(text)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{yaml_path} must contain a top-level mapping.")
    return loaded


def _canonical_allowed_states(
    value: Any,
    *,
    role: str,
    username: str,
) -> tuple[str, ...]:
    states = tuple(_iter_allowed_state_values(value))
    if not states:
        raise ValueError(f"Auth user {username} must have allowed_states.")

    if role == "admin" and "*" in states:
        return ("*",)
    if "*" in states:
        raise ValueError(f"Non-admin auth user {username} cannot use wildcard states.")

    normalized_states: list[str] = []
    for state in states:
        normalized_state = _normalize_state_code(state)
        if normalized_state is None:
            raise ValueError(
                f"Auth user {username} has non-canonical state code: {state!r}"
            )
        if normalized_state not in normalized_states:
            normalized_states.append(normalized_state)
    return tuple(normalized_states)


def _iter_allowed_state_values(value: Any) -> Iterable[str]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return (stripped,)
            if isinstance(parsed, list):
                return tuple(str(item).strip() for item in parsed)
        return (stripped,)
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value)
    return (str(value).strip(),)


def _normalize_state_code(value: str | None) -> str | None:
    state = str(value or "").strip().upper()
    if not STATE_CODE_RE.fullmatch(state):
        return None
    return state


def _canonical_username(value: Any) -> str:
    return str(value or "").strip().upper()


def _current_flask_login_user() -> User | None:
    try:
        from flask_login import current_user as flask_current_user
    except ImportError:
        return None

    try:
        if not bool(getattr(flask_current_user, "is_authenticated", False)):
            return None
        username = getattr(flask_current_user, "username", None)
        if not username and hasattr(flask_current_user, "get_id"):
            username = flask_current_user.get_id()
    except RuntimeError:
        return None

    return get_user(username)


def _unauthenticated_response() -> Any:
    try:
        from flask import abort, redirect, request, url_for
    except ImportError:  # pragma: no cover - Flask is a project dependency.
        raise Unauthorized()

    if _request_prefers_json():
        abort(401)

    try:
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_url))
    except BuildError:
        abort(401)


def _request_prefers_json() -> bool:
    from flask import request

    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return bool(request.is_json or best == "application/json")
