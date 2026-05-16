"""Safe local dashboard job runner for whitelisted pipeline commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import db
from .config import PROJECT_ROOT, get_database_path, project_path


ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "stale"}
EXTERNAL_JOB_TYPES = {"places_pull", "audit", "full_pipeline"}
LOG_ROOT = "runs/dashboard_jobs"
SUMMARY_FIELD_ORDER = [
    "audit_mode",
    "selected",
    "api_queries_made",
    "raw_places_returned",
    "inserted",
    "updated",
    "duplicates_skipped",
    "disqualified",
    "processed",
    "qualified",
    "generated",
    "skipped",
    "failed",
    "audited",
    "screenshots_succeeded",
    "pagespeed_succeeded",
    "pagespeed_skipped",
]
SUMMARY_LABELS = {
    "api_queries_made": "API Queries Made",
    "raw_places_returned": "Raw Places Returned",
    "duplicates_skipped": "Duplicates Skipped",
}
SUMMARY_EXCLUDED_KEYS = {
    "command",
    "dry_run",
    "event",
    "level",
    "logger",
    "message",
    "timestamp",
    "market",
    "niche",
    "limit",
}

ALLOWED_JOBS: dict[str, dict[str, Any]] = {
    "places_pull": {
        "label": "Places Pull",
        "module": "src.places_pull",
        "external": True,
        "description": "Pulls local businesses from Google Places. May use paid API quota.",
    },
    "eligibility": {
        "label": "Eligibility",
        "module": "src.eligibility",
        "external": False,
        "description": "Scores local eligibility from existing database records.",
    },
    "audit": {
        "label": "Website Audit",
        "module": "src.audit_site",
        "external": True,
        "description": "Audits websites and may capture screenshots/PageSpeed data.",
    },
    "score": {
        "label": "Lead Scoring",
        "module": "src.score_leads",
        "external": False,
        "description": "Scores audited leads from existing local audit data.",
    },
    "artifacts": {
        "label": "Generate Artifacts",
        "module": "src.generate_artifacts",
        "external": False,
        "description": "Generates local review and sales artifacts.",
    },
    "reconcile_statuses": {
        "label": "Reconcile Statuses",
        "module": "src.reconcile_statuses",
        "external": False,
        "description": "Normalizes status/next-action drift.",
    },
    "full_pipeline": {
        "label": "Full Market Pipeline",
        "module": None,
        "external": True,
        "description": "Runs fixed market pipeline steps sequentially for selected niches.",
    },
}

RUNNER_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | Path | None = None):
    return db.init_db(db_path or get_database_path())


def ensure_schema(db_path: str | Path | None = None) -> None:
    connect(db_path).close()


def parse_json_field(value: Any, fallback: Any) -> Any:
    if value in {None, ""}:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def row_to_job(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    job = db.row_to_dict(row)
    job["dry_run"] = bool(job.get("dry_run"))
    job["command"] = parse_json_field(job.get("command_json"), [])
    job["metadata"] = parse_json_field(job.get("metadata_json"), {})
    return job


def make_job_key(job_type: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{job_type}_{stamp}_{uuid.uuid4().hex[:8]}"


def log_path_for(job_key: str) -> str:
    return f"{LOG_ROOT}/{job_key}.log"


def resolved_log_path(job_key: str) -> Path:
    path = project_path(log_path_for(job_key)).resolve(strict=False)
    root = project_path(LOG_ROOT).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid job log path.") from exc
    return path


def create_job(
    job_type: str,
    market: str | None = None,
    niche: str | None = None,
    limit_count: int | None = None,
    dry_run: bool = True,
    metadata: dict[str, Any] | None = None,
    requested_by_user: str | None = None,
    market_state: str | None = None,
) -> str:
    if job_type not in ALLOWED_JOBS:
        raise ValueError("Unsupported dashboard job type.")
    if limit_count is not None and limit_count < 1:
        raise ValueError("Limit must be a positive integer.")

    metadata = dict(metadata or {})
    if requested_by_user:
        metadata["requested_by_user"] = requested_by_user
    if market_state:
        metadata["market_state"] = market_state
    db_path = metadata.get("db_path")
    command_options = metadata.get("command_options")
    if not isinstance(command_options, dict):
        command_options = {}
    command = build_whitelisted_command(
        job_type,
        {
            "market": market,
            "niche": niche,
            "limit": limit_count,
            "dry_run": dry_run,
            "db_path": db_path,
            **command_options,
        },
    )
    job_key = make_job_key(job_type)
    log_path = log_path_for(job_key)
    now = utc_now()

    with connect(db_path) as connection:
        if has_active_job(connection=connection):
            raise ValueError("A dashboard job is already queued or running.")
        connection.execute(
            """
            INSERT INTO dashboard_jobs (
                job_key, job_type, status, market, market_state, niche,
                limit_count, dry_run, requested_by_user, command_json,
                metadata_json, log_path, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_key,
                job_type,
                market or None,
                market_state or None,
                niche or None,
                limit_count,
                1 if dry_run else 0,
                requested_by_user or None,
                json.dumps(command),
                json.dumps(metadata, sort_keys=True),
                log_path,
                now,
                now,
            ),
        )
    append_job_log(job_key, f"queued {job_type}\n", db_path=db_path)
    return job_key


def create_full_pipeline_job(
    *,
    market: str,
    niches: list[str],
    per_niche_places_limit: int,
    audit_limit: int,
    artifact_limit: int,
    dry_run_all: bool = True,
    audit_fast: bool = False,
    metadata: dict[str, Any] | None = None,
    requested_by_user: str | None = None,
    market_state: str | None = None,
) -> str:
    if not market:
        raise ValueError("Full pipeline requires a selected market.")
    niches = [str(niche).strip() for niche in niches if str(niche).strip()]
    if not niches:
        raise ValueError("Full pipeline requires at least one selected niche.")
    if per_niche_places_limit < 1 or audit_limit < 1 or artifact_limit < 1:
        raise ValueError("Pipeline limits must be positive integers.")

    metadata = dict(metadata or {})
    if requested_by_user:
        metadata["requested_by_user"] = requested_by_user
    if market_state:
        metadata["market_state"] = market_state
    db_path = metadata.get("db_path")
    include_reconcile_statuses = bool(metadata.get("include_reconcile_statuses", True))
    steps = build_full_pipeline_steps(
        market=market,
        niches=niches,
        per_niche_places_limit=per_niche_places_limit,
        audit_limit=audit_limit,
        artifact_limit=artifact_limit,
        dry_run_all=dry_run_all,
        audit_fast=audit_fast,
        db_path=db_path,
        include_reconcile_statuses=include_reconcile_statuses,
    )
    metadata.update(
        {
            "steps": steps,
            "per_niche_places_limit": per_niche_places_limit,
            "audit_limit": audit_limit,
            "artifact_limit": artifact_limit,
            "dry_run_all": bool(dry_run_all),
            "audit_mode": "fast" if audit_fast else "deep",
            "include_reconcile_statuses": include_reconcile_statuses,
        }
    )

    job_key = make_job_key("full_pipeline")
    log_path = log_path_for(job_key)
    command = ["full_pipeline", "--market", market, "--niches", ",".join(niches)]
    now = utc_now()

    with connect(db_path) as connection:
        if has_active_job(connection=connection):
            raise ValueError("A dashboard job is already queued or running.")
        connection.execute(
            """
            INSERT INTO dashboard_jobs (
                job_key, job_type, status, market, market_state, niche,
                limit_count, dry_run, requested_by_user, command_json,
                metadata_json, log_path, created_at, updated_at
            ) VALUES (?, 'full_pipeline', 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_key,
                market,
                market_state or None,
                ",".join(niches),
                per_niche_places_limit,
                1 if dry_run_all else 0,
                requested_by_user or None,
                json.dumps(command),
                json.dumps(metadata, sort_keys=True),
                log_path,
                now,
                now,
            ),
        )
    append_job_log(job_key, f"queued full_pipeline with {len(steps)} steps\n", db_path=db_path)
    return job_key


def get_job(job_key: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM dashboard_jobs WHERE job_key = ?",
            (job_key,),
        ).fetchone()
    return row_to_job(row)


def list_jobs(limit: int = 50, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(int(limit or 50), 200))
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM dashboard_jobs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (normalized_limit,),
        ).fetchall()
    return [job for row in rows if (job := row_to_job(row)) is not None]


def has_active_job(
    db_path: str | Path | None = None,
    *,
    connection: Any | None = None,
) -> bool:
    owns_connection = connection is None
    if connection is None:
        connection = connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT 1
            FROM dashboard_jobs
            WHERE status IN ('queued', 'running')
            LIMIT 1
            """
        ).fetchone()
        return row is not None
    finally:
        if owns_connection:
            connection.close()


def mark_job_running(job_key: str, db_path: str | Path | None = None) -> None:
    now = utc_now()
    with connect(db_path) as connection:
        connection.execute(
            """
            UPDATE dashboard_jobs
            SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?
            WHERE job_key = ?
            """,
            (now, now, job_key),
        )


def mark_job_succeeded(job_key: str, db_path: str | Path | None = None) -> None:
    now = utc_now()
    with connect(db_path) as connection:
        connection.execute(
            """
            UPDATE dashboard_jobs
            SET status = 'succeeded', finished_at = ?, updated_at = ?
            WHERE job_key = ?
            """,
            (now, now, job_key),
        )


def mark_job_failed(
    job_key: str,
    error_summary: str,
    db_path: str | Path | None = None,
) -> None:
    now = utc_now()
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM dashboard_jobs WHERE job_key = ?",
            (job_key,),
        ).fetchone()
        metadata = parse_json_field(row["metadata_json"] if row else None, {})
        metadata["error_summary"] = error_summary[-2000:]
        connection.execute(
            """
            UPDATE dashboard_jobs
            SET status = 'failed', finished_at = ?, updated_at = ?, metadata_json = ?
            WHERE job_key = ?
            """,
            (now, now, json.dumps(metadata, sort_keys=True), job_key),
        )


def append_job_log(
    job_key: str,
    text: str,
    db_path: str | Path | None = None,
) -> None:
    job = get_job(job_key, db_path=db_path)
    if job is None:
        return
    log_path = project_path(str(job.get("log_path") or log_path_for(job_key)))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(text)


def read_job_log(job_key: str, tail_chars: int | None = None, db_path: str | Path | None = None) -> str:
    job = get_job(job_key, db_path=db_path)
    if job is None:
        return ""
    log_path = project_path(str(job.get("log_path") or log_path_for(job_key)))
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if tail_chars is not None and tail_chars > 0:
        return text[-tail_chars:]
    return text


def read_job_summary(job_key: str, db_path: str | Path | None = None) -> dict[str, Any]:
    return extract_job_summary(read_job_log(job_key, db_path=db_path))


def extract_job_summary(log_text: str) -> dict[str, Any]:
    payload = latest_command_finished_payload(log_text)
    if not payload:
        return {"rows": [], "raw": {}}

    rows = []
    seen = set()
    for key in SUMMARY_FIELD_ORDER:
        if key in payload:
            rows.append({"key": key, "label": summary_label(key), "value": payload[key]})
            seen.add(key)

    for key in sorted(payload):
        if key in seen or key in SUMMARY_EXCLUDED_KEYS:
            continue
        value = payload[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            rows.append({"key": key, "label": summary_label(key), "value": value})

    return {"rows": rows, "raw": payload}


def latest_command_finished_payload(log_text: str) -> dict[str, Any]:
    fallback: dict[str, Any] = {}
    for raw_line in reversed(log_text.splitlines()):
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("event") == "command_finished":
            return payload
        if not fallback and any(key in payload for key in SUMMARY_FIELD_ORDER):
            fallback = payload
    return fallback


def actor_env_for_job(job: dict[str, Any]) -> dict[str, str] | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    username = str(job.get("requested_by_user") or metadata.get("requested_by_user") or "").strip()
    if not username:
        return None

    role = str(metadata.get("requested_by_role") or "").strip().lower() or "user"
    allowed_states = metadata.get("requested_by_allowed_states")
    if isinstance(allowed_states, str):
        allowed_values = [value.strip() for value in allowed_states.split(",") if value.strip()]
    elif isinstance(allowed_states, list):
        allowed_values = [str(value).strip() for value in allowed_states if str(value).strip()]
    else:
        allowed_values = []
    if role == "admin" or "*" in allowed_values:
        allowed_text = "*"
    else:
        allowed_text = ",".join(allowed_values)

    actor_env = {
        "APP_ACTOR_USERNAME": username,
        "APP_ACTOR_ROLE": role,
        "APP_ACTOR_ALLOWED_STATES": allowed_text,
        "APP_ACTOR_MARKET": str(job.get("market") or metadata.get("market") or ""),
        "APP_ACTOR_MARKET_STATE": str(job.get("market_state") or metadata.get("market_state") or ""),
    }
    return actor_env


def subprocess_env_for_job(job: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    actor_env = actor_env_for_job(job)
    if actor_env:
        env.update(actor_env)
    return env


def log_actor_context_for_job(job: dict[str, Any], *, db_path: str | Path | None = None) -> None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    allowed_states = metadata.get("requested_by_allowed_states")
    if isinstance(allowed_states, list):
        allowed_text = ",".join(str(value) for value in allowed_states)
    elif allowed_states is None:
        allowed_text = ""
    else:
        allowed_text = str(allowed_states)
    lines = [
        "actor context:",
        f"  requested_by_user={job.get('requested_by_user') or metadata.get('requested_by_user') or ''}",
        f"  actor_role={metadata.get('requested_by_role') or ''}",
        f"  market={job.get('market') or ''}",
        f"  market_state={job.get('market_state') or metadata.get('market_state') or ''}",
        f"  allowed_states={allowed_text}",
        "",
    ]
    append_job_log(str(job["job_key"]), "\n".join(lines), db_path=db_path)


def log_command_started(
    job: dict[str, Any],
    command: list[Any],
    *,
    db_path: str | Path | None = None,
    prefix: str = "running",
) -> None:
    append_job_log(str(job["job_key"]), f"{prefix}: {json.dumps(command)}\n", db_path=db_path)
    log_actor_context_for_job(job, db_path=db_path)


def summary_label(key: str) -> str:
    return SUMMARY_LABELS.get(key, key.replace("_", " ").strip().title())


def build_whitelisted_command(job_type: str, params: dict[str, Any]) -> list[str]:
    if job_type not in ALLOWED_JOBS:
        raise ValueError("Unsupported dashboard job type.")
    if job_type == "full_pipeline":
        raise ValueError("Full pipeline jobs are built from fixed step commands.")
    dry_run = bool(params.get("dry_run", True))
    command = [
        sys.executable,
        "-m",
        str(ALLOWED_JOBS[job_type]["module"]),
    ]
    db_path = params.get("db_path")
    if db_path:
        command.extend(["--db-path", str(db_path)])

    limit = params.get("limit")
    if limit not in {None, ""}:
        command.extend(["--limit", str(int(limit))])

    if job_type != "reconcile_statuses":
        market = str(params.get("market") or "").strip()
        niche = str(params.get("niche") or "").strip()
        if market:
            command.extend(["--market", market])
        if niche:
            command.extend(["--niche", niche])
        if dry_run:
            command.append("--dry-run")
        if job_type == "audit":
            if params.get("audit_fast"):
                command.append("--fast")
            if params.get("skip_pagespeed"):
                command.append("--skip-pagespeed")
            max_pages = params.get("max_pages")
            if max_pages not in {None, ""}:
                command.extend(["--max-pages", str(int(max_pages))])
            screenshot_timeout_ms = params.get("screenshot_timeout_ms")
            if screenshot_timeout_ms not in {None, ""}:
                command.extend(["--screenshot-timeout-ms", str(int(screenshot_timeout_ms))])
            page_timeout_ms = params.get("page_timeout_ms")
            if page_timeout_ms not in {None, ""}:
                command.extend(["--page-timeout-ms", str(int(page_timeout_ms))])
    elif dry_run:
        command.append("--dry-run")
    else:
        command.append("--apply")
    return command


def build_full_pipeline_steps(
    *,
    market: str,
    niches: list[str],
    per_niche_places_limit: int,
    audit_limit: int,
    artifact_limit: int,
    dry_run_all: bool,
    audit_fast: bool = False,
    db_path: str | Path | None = None,
    include_reconcile_statuses: bool = True,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for niche in niches:
        steps.append(
            {
                "label": f"Pull Places Leads: {market}/{niche}",
                "job_type": "places_pull",
                "command": build_whitelisted_command(
                    "places_pull",
                    {
                        "market": market,
                        "niche": niche,
                        "limit": per_niche_places_limit,
                        "dry_run": dry_run_all,
                        "db_path": db_path,
                    },
                ),
            }
        )
        steps.append(
            {
                "label": f"Run Eligibility: {market}/{niche}",
                "job_type": "eligibility",
                "command": build_whitelisted_command(
                    "eligibility",
                    {
                        "market": market,
                        "niche": niche,
                        "dry_run": dry_run_all,
                        "db_path": db_path,
                    },
                ),
            }
        )

    steps.extend(
        [
            {
                "label": f"Run Audits: {market}",
                "job_type": "audit",
                "command": build_whitelisted_command(
                    "audit",
                    {
                        "market": market,
                        "limit": audit_limit,
                        "dry_run": dry_run_all,
                        "audit_fast": audit_fast,
                        "skip_pagespeed": audit_fast,
                        "db_path": db_path,
                    },
                ),
            },
            {
                "label": f"Run Scoring: {market}",
                "job_type": "score",
                "command": build_whitelisted_command(
                    "score",
                    {
                        "market": market,
                        "dry_run": dry_run_all,
                        "db_path": db_path,
                    },
                ),
            },
            {
                "label": f"Generate Artifacts: {market}",
                "job_type": "artifacts",
                "command": build_whitelisted_command(
                    "artifacts",
                    {
                        "market": market,
                        "limit": artifact_limit,
                        "dry_run": dry_run_all,
                        "db_path": db_path,
                    },
                ),
            },
        ]
    )
    if include_reconcile_statuses:
        steps.append(
            {
                "label": "Reconcile Statuses",
                "job_type": "reconcile_statuses",
                "command": build_whitelisted_command(
                    "reconcile_statuses",
                    {
                        "dry_run": dry_run_all,
                        "db_path": db_path,
                    },
                ),
            }
        )
    return steps


def run_job_async(job_key: str, db_path: str | Path | None = None) -> None:
    thread = threading.Thread(
        target=_run_job_worker,
        args=(job_key, str(db_path) if db_path else None),
        name=f"dashboard-job-{job_key}",
        daemon=True,
    )
    thread.start()


def _run_job_worker(job_key: str, db_path: str | Path | None = None) -> None:
    if not RUNNER_LOCK.acquire(blocking=False):
        mark_job_failed(job_key, "Another dashboard job is already running.", db_path=db_path)
        append_job_log(job_key, "blocked: another dashboard job is already running\n", db_path=db_path)
        return

    try:
        job = get_job(job_key, db_path=db_path)
        if job is None:
            return
        if job.get("job_type") == "full_pipeline":
            mark_job_running(job_key, db_path=db_path)
            _run_full_pipeline_worker(job, db_path=db_path)
            return
        command = job.get("command") or []
        if not isinstance(command, list) or not command:
            mark_job_failed(job_key, "Job command is missing.", db_path=db_path)
            return

        mark_job_running(job_key, db_path=db_path)
        log_command_started(job, command, db_path=db_path)
        log_path = project_path(str(job.get("log_path") or log_path_for(job_key)))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = subprocess_env_for_job(job)

        with log_path.open("a", encoding="utf-8", errors="replace") as handle:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=False,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                handle.write(line)
                handle.flush()
            returncode = process.wait()
            handle.write(f"\nprocess exited with code {returncode}\n")

        if returncode == 0:
            mark_job_succeeded(job_key, db_path=db_path)
        else:
            mark_job_failed(job_key, f"Process exited with code {returncode}.", db_path=db_path)
    except Exception as exc:  # pragma: no cover - worker safety net
        mark_job_failed(job_key, str(exc), db_path=db_path)
        append_job_log(job_key, f"\nrunner failed: {exc}\n", db_path=db_path)
    finally:
        RUNNER_LOCK.release()


def _run_full_pipeline_worker(job: dict[str, Any], db_path: str | Path | None = None) -> None:
    job_key = str(job["job_key"])
    steps = job.get("metadata", {}).get("steps") or []
    if not isinstance(steps, list) or not steps:
        mark_job_failed(job_key, "Full pipeline steps are missing.", db_path=db_path)
        return

    log_path = project_path(str(job.get("log_path") or log_path_for(job_key)))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    append_job_log(job_key, f"running full_pipeline with {len(steps)} steps\n\n", db_path=db_path)
    log_actor_context_for_job(job, db_path=db_path)
    env = subprocess_env_for_job(job)

    with log_path.open("a", encoding="utf-8", errors="replace") as handle:
        for index, step in enumerate(steps, start=1):
            label = str(step.get("label") or f"Step {index}")
            command = step.get("command") or []
            if not isinstance(command, list) or not command:
                handle.write(f"step {index}/{len(steps)} failed: missing command for {label}\n")
                mark_job_failed(job_key, f"Missing command for {label}.", db_path=db_path)
                return

            handle.write(f"step {index}/{len(steps)} started: {label}\n")
            handle.write(f"running: {json.dumps(command)}\n\n")
            handle.flush()
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=False,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                handle.write(line)
                handle.flush()
            returncode = process.wait()
            handle.write(f"\nstep {index}/{len(steps)} exited with code {returncode}: {label}\n\n")
            handle.flush()

            if returncode != 0:
                mark_job_failed(
                    job_key,
                    f"Step {index} failed with code {returncode}: {label}.",
                    db_path=db_path,
                )
                return

    mark_job_succeeded(job_key, db_path=db_path)


def mark_stale_jobs(
    db_path: str | Path | None = None,
    *,
    older_than_hours: int = 6,
) -> int:
    threshold = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    threshold_text = threshold.replace(microsecond=0).isoformat()
    now = utc_now()
    with connect(db_path) as connection:
        running_cursor = connection.execute(
            """
            UPDATE dashboard_jobs
            SET status = 'stale', finished_at = COALESCE(finished_at, ?), updated_at = ?
            WHERE status = 'running'
              AND started_at IS NOT NULL
              AND started_at < ?
            """,
            (now, now, threshold_text),
        )
        queued_cursor = connection.execute(
            """
            UPDATE dashboard_jobs
            SET status = 'stale', finished_at = COALESCE(finished_at, ?), updated_at = ?
            WHERE status = 'queued'
              AND created_at < ?
            """,
            (now, now, threshold_text),
        )
        return int(running_cursor.rowcount or 0) + int(queued_cursor.rowcount or 0)
