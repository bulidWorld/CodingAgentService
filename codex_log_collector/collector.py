"""Tail Codex app-server SQLite logs and emit JSON lines to stdout."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


LEVELS = {"TRACE": 5, "DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}
OUTPUT_LEVELS = {"TRACE": "DEBUG", "DEBUG": "DEBUG", "INFO": "INFO", "WARN": "WARNING", "WARNING": "WARNING", "ERROR": "ERROR"}
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|bearer[_-]?token)\s*[:=]\s*)[^\s,;\"']+"),
)
OPTIONAL_COLUMNS = ("ts_nanos", "module_path", "file", "line", "thread_id", "process_uuid")
REQUIRED_COLUMNS = {"id", "ts", "level", "target", "feedback_log_body"}


def emit(level: str, message: str, *, event: str = "codex_log_collector", **fields: Any) -> None:
    """Write one JSON log record to stdout for Kubernetes collection."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": OUTPUT_LEVELS.get(level.upper(), "INFO"),
        "log_type": "llm",
        "provider": "codex",
        "event": event,
        "message": message,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)


def redact_secrets(value: str) -> str:
    """Redact common credential forms before copying runtime logs to stdout."""
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(r"\1[REDACTED]", value)
    return value


def connect_readonly(database: Path) -> sqlite3.Connection:
    """Open SQLite read-only, using immutable mode only when no WAL is present."""
    uri = f"{database.resolve().as_uri()}?mode=ro"
    if not database.with_name(f"{database.name}-wal").exists():
        # An idle Codex DB may have no -shm file; immutable mode reads the
        # checkpointed main DB without trying to create shared-memory files.
        uri += "&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=1.0)


def latest_id(database: Path) -> int:
    connection = connect_readonly(database)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(logs)")}
        if "id" not in columns:
            return 0
        result = connection.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()
        return int(result[0])
    finally:
        connection.close()


def read_rows(database: Path, after_id: int, batch_size: int) -> list[dict[str, Any]]:
    connection = connect_readonly(database)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(logs)")}
        if not REQUIRED_COLUMNS.issubset(columns):
            return []
        selected = ["id", "ts", "level", "target", "feedback_log_body"]
        selected.extend(column if column in columns else f"NULL AS {column}" for column in OPTIONAL_COLUMNS)
        query = f"SELECT {', '.join(selected)} FROM logs WHERE id > ? ORDER BY id LIMIT ?"
        result = connection.execute(query, (after_id, batch_size))
        names = [item[0] for item in result.description]
        return [dict(zip(names, row, strict=True)) for row in result.fetchall()]
    finally:
        connection.close()


def row_timestamp(seconds: int, nanos: int | None) -> str:
    value = datetime.fromtimestamp(seconds, timezone.utc)
    if nanos:
        value += timedelta(microseconds=nanos / 1000)
    return value.isoformat()


def forward_row(row: dict[str, Any]) -> None:
    runtime_level = str(row.get("level") or "INFO").upper()
    timestamp = row.get("ts")
    fields: dict[str, Any] = {
        "event": "runtime_sqlite_log",
        "runtime_level": runtime_level,
        "target": row.get("target"),
        "message": redact_secrets(str(row.get("feedback_log_body") or "")),
    }
    if timestamp is not None:
        fields["timestamp"] = row_timestamp(int(timestamp), row.get("ts_nanos"))
    for name in OPTIONAL_COLUMNS[1:]:
        value = row.get(name)
        if value is not None:
            fields[name] = value
    emit(runtime_level, fields.pop("message"), event="runtime_sqlite_log", **fields)


def configured_level() -> tuple[str, int]:
    level = os.getenv("CODEX_LOG_MIN_LEVEL", "WARN").strip().upper()
    if level not in LEVELS:
        raise ValueError(f"Unsupported CODEX_LOG_MIN_LEVEL: {level}")
    return level, LEVELS[level]


def run() -> None:
    codex_home = Path(os.getenv("CODEX_HOME", "/var/service/coding-agent-service/share/.codex"))
    minimum_name, minimum_value = configured_level()
    poll_seconds = max(0.2, float(os.getenv("CODEX_LOG_POLL_SECONDS", "1")))
    batch_size = max(1, int(os.getenv("CODEX_LOG_BATCH_SIZE", "500")))
    start_position = os.getenv("CODEX_LOG_START_POSITION", "latest").strip().lower()
    if start_position not in {"latest", "beginning"}:
        raise ValueError("CODEX_LOG_START_POSITION must be 'latest' or 'beginning'")

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stopping.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stopping.set())
    initial_databases = {path.resolve() for path in codex_home.glob("logs_*.sqlite")}
    cursors: dict[Path, int] = {}
    reported_errors: dict[Path, float] = {}
    emit(
        "INFO",
        "Codex SQLite log collector started",
        event="collector_started",
        codex_home=str(codex_home),
        min_level=minimum_name,
        start_position=start_position,
        poll_interval_seconds=poll_seconds,
    )

    while not stopping.is_set():
        for database in sorted(codex_home.glob("logs_*.sqlite")):
            database = database.resolve()
            try:
                if database not in cursors:
                    cursors[database] = (
                        latest_id(database)
                        if start_position == "latest" and database in initial_databases
                        else 0
                    )
                rows = read_rows(database, cursors[database], batch_size)
                for row in rows:
                    row_id = int(row["id"])
                    cursors[database] = max(cursors[database], row_id)
                    runtime_level = str(row.get("level") or "INFO").upper()
                    if LEVELS.get(runtime_level, LEVELS["INFO"]) >= minimum_value:
                        forward_row(row)
            except (OSError, sqlite3.Error, ValueError) as error:
                now = time.monotonic()
                if now - reported_errors.get(database, 0) >= 30:
                    reported_errors[database] = now
                    emit(
                        "WARNING",
                        "Unable to read Codex SQLite log database",
                        event="collector_read_error",
                        database=str(database),
                        error_type=type(error).__name__,
                    )
        stopping.wait(poll_seconds)


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        emit(
            "ERROR",
            "Codex SQLite log collector stopped",
            event="collector_stopped",
            error_type=type(error).__name__,
            detail=str(error),
        )
        sys.exit(1)
