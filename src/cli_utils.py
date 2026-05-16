"""Shared CLI setup for batch commands."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass

from . import actor_context
from .config import load_env
from .db import init_db
from .logging_config import configure_logging


@dataclass(frozen=True)
class CommandContext:
    args: argparse.Namespace
    logger: logging.Logger


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("--limit must be a positive integer")
    return parsed


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--dry-run", action="store_true", help="Log planned work only.")
    parser.add_argument("--limit", type=positive_int, default=None, help="Maximum rows to process.")
    parser.add_argument("--market", default=None, help="Market key from config/markets.yaml.")
    parser.add_argument("--niche", default=None, help="Niche key from config/niches.yaml.")
    parser.add_argument("--db-path", default=None, help="Override DATABASE_PATH.")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR.")
    return parser


def setup_command(args: argparse.Namespace, command_name: str) -> CommandContext:
    load_env()
    configure_logging(args.log_level or os.environ.get("LOG_LEVEL", "INFO"))
    init_db(args.db_path).close()

    logger = logging.getLogger(command_name)
    logger.info(
        "command_started",
        extra={
            "event": "command_started",
            "command": command_name,
            "dry_run": args.dry_run,
            "limit": args.limit,
            "market": args.market,
            "niche": args.niche,
            **actor_context.actor_display_fields(),
        },
    )
    actor_context.log_actor_scope(logger)
    return CommandContext(args=args, logger=logger)


def finish_command(context: CommandContext, **fields: object) -> None:
    context.logger.info(
        "command_finished",
        extra={"event": "command_finished", "command": context.logger.name, **fields},
    )
