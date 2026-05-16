"""Configuration and environment loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"


def load_env(env_path: str | Path | None = None) -> None:
    """Load .env values without requiring dependencies at import time."""

    target = Path(env_path) if env_path else PROJECT_ROOT / ".env"

    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_env_fallback(target)
        return

    load_dotenv(target)


def _load_env_fallback(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def project_path(value: str | Path) -> Path:
    """Resolve relative paths from the project root."""

    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def get_database_path(default: str = "data/leads.db") -> Path:
    configured = os.environ.get("DATABASE_PATH", default)
    return project_path(configured)


def load_yaml_config(filename: str) -> dict[str, Any]:
    """Load a YAML config file from config/."""

    path = CONFIG_DIR / filename
    text = path.read_text(encoding="utf-8")

    try:
        import yaml
    except ImportError:
        loaded = _load_simple_yaml(text)
    else:
        loaded = yaml.safe_load(text)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a top-level mapping.")
    return loaded


def _load_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, result)]
    last_key_at_indent: dict[int, tuple[dict[str, Any], str]] = {}
    block_scalar_indent: int | None = None

    for raw_line in text.splitlines():
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if block_scalar_indent is not None:
            if not raw_line.strip() or indent > block_scalar_indent:
                continue
            block_scalar_indent = None

        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line = raw_line.strip()
        is_list_item = line.startswith("- ")

        while stack and (indent < stack[-1][0] or (indent == stack[-1][0] and not is_list_item)):
            stack.pop()
        parent = stack[-1][1]

        if is_list_item:
            value = _parse_simple_scalar(line[2:].strip())
            if not isinstance(parent, list):
                if indent not in last_key_at_indent:
                    raise ValueError("Unsupported YAML list structure.")
                owner, key = last_key_at_indent[indent]
                owner[key] = []
                parent = owner[key]
                stack.append((indent, parent))
            parent.append(value)
            continue

        if ":" not in line:
            raise ValueError(f"Unsupported YAML line: {raw_line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()

        if raw_value:
            if raw_value in {"|", ">"}:
                parent[key] = ""
                block_scalar_indent = indent
                continue
            parent[key] = _parse_simple_scalar(raw_value)
        else:
            parent[key] = {}
            last_key_at_indent[indent + 2] = (parent, key)
            last_key_at_indent[indent] = (parent, key)
            stack.append((indent, parent[key]))

    return result


def _parse_simple_scalar(value: str) -> Any:
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value
