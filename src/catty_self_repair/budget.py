"""Durable, fail-closed weekly token accounting for the independent repair LLM."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator
import uuid

from .config import HARD_WEEKLY_LIMIT
from .storage import reject_links


_TIMEZONE = timezone(timedelta(hours=8))
_APPLICATION_ID = 0x43535242
_SCHEMA_VERSION = 1
_META_SQL = """CREATE TABLE repair_budget_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    weekly_limit INTEGER NOT NULL CHECK (
        typeof(weekly_limit) = 'integer' AND weekly_limit BETWEEN 0 AND 100000000
    )
)"""
_RESERVATIONS_SQL = """CREATE TABLE repair_budget_reservations (
    reservation_id TEXT PRIMARY KEY NOT NULL,
    first_week TEXT NOT NULL,
    last_week TEXT NOT NULL,
    max_tokens INTEGER NOT NULL CHECK (
        typeof(max_tokens) = 'integer' AND max_tokens BETWEEN 0 AND 100000000
    ),
    used_tokens INTEGER,
    CHECK (first_week <= last_week),
    CHECK (
        used_tokens IS NULL OR
        (typeof(used_tokens) = 'integer' AND used_tokens BETWEEN 0 AND max_tokens)
    )
)"""
_EXPECTED_SCHEMA = {
    ("table", "repair_budget_meta", _META_SQL),
    ("table", "repair_budget_reservations", _RESERVATIONS_SQL),
    ("index", "sqlite_autoindex_repair_budget_reservations_1", None),
}


class _UninitializedLedger(sqlite3.DatabaseError):
    """Another first opener may own initialization; never initialize on its behalf."""


def _week(now: datetime | None) -> str:
    if now is None:
        now = datetime.now(_TIMEZONE)
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    try:
        local = now.replace(tzinfo=_TIMEZONE) if now.utcoffset() is None else now.astimezone(_TIMEZONE)
        day = local.date()
        return (day - timedelta(days=day.weekday())).isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("now cannot be represented in UTC+8") from exc


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def _validate_week(value: object) -> None:
    try:
        if not isinstance(value, str):
            raise ValueError("week must be a string")
        day = date.fromisoformat(value)
        if day.isoformat() != value or day.weekday() != 0:
            raise ValueError("week must be an ISO Monday date")
    except (TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError("self repair budget week is invalid") from exc


class WeeklyBudget:
    """One persistent ledger shared by repair processes; unavailable is not free.

    ``used`` includes every unresolved reservation at its maximum; ``pending`` is
    a count. Reservations do not expire. A lower limit is durable, not auto-raised.
    """

    def __init__(self, path: Path, limit: int = HARD_WEEKLY_LIMIT) -> None:
        if type(limit) is not int or not 0 <= limit <= HARD_WEEKLY_LIMIT:
            raise ValueError("limit must be an integer between 0 and 100000000")
        if path is None or os.fspath(path) in ("", ":memory:", b"", b":memory:"):
            raise ValueError("path must name a persistent SQLite database")
        lexical = Path(path).absolute()
        reject_links(lexical)
        lexical.parent.mkdir(parents=True, exist_ok=True)
        reject_links(lexical)
        self._path = lexical.resolve()
        self._uri = self._path.as_uri() + "?mode=rw"
        self._check_paths()
        created = False
        try:
            descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            created = True
            os.close(descriptor)
        self._initialize(created, limit)

    def _check_paths(self) -> None:
        reject_links(self._path)
        for suffix in ("-journal", "-wal", "-shm"):
            reject_links(self._path.with_name(self._path.name + suffix))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._check_paths()
        connection = sqlite3.connect(self._uri, uri=True, isolation_level=None, timeout=30)
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self, created: bool, limit: int) -> None:
        deadline = time.monotonic() + 2.0
        while True:
            try:
                with self._transaction() as connection:
                    if created:
                        if (
                            connection.execute("SELECT name FROM sqlite_master").fetchall()
                            or connection.execute("PRAGMA application_id").fetchone()[0] != 0
                            or connection.execute("PRAGMA user_version").fetchone()[0] != 0
                        ):
                            raise sqlite3.DatabaseError("new self repair budget is not empty")
                        connection.execute(_META_SQL)
                        connection.execute(_RESERVATIONS_SQL)
                        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                        connection.execute("INSERT INTO repair_budget_meta (singleton, weekly_limit) VALUES (1, ?)", (limit,))
                    else:
                        current_limit = self._validate(connection)
                        self._usage(connection, None)
                        if limit < current_limit:
                            connection.execute("UPDATE repair_budget_meta SET weekly_limit = ? WHERE singleton = 1", (limit,))
                return
            except _UninitializedLedger:
                if created or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _validate(self, connection: sqlite3.Connection) -> int:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError("self repair budget integrity check failed")
        schema = set(connection.execute("SELECT type, name, sql FROM sqlite_master"))
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if not schema and application_id == 0 and version == 0 and self._path.stat().st_size == 0:
            raise _UninitializedLedger("self repair budget initialization is incomplete or empty")
        if schema != _EXPECTED_SCHEMA:
            raise sqlite3.DatabaseError("self repair budget schema is invalid")
        if application_id != _APPLICATION_ID or version != _SCHEMA_VERSION:
            raise sqlite3.DatabaseError("self repair budget identity/version is invalid")
        rows = connection.execute("SELECT singleton, weekly_limit FROM repair_budget_meta").fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise sqlite3.DatabaseError("self repair budget metadata is invalid")
        limit = rows[0][1]
        if type(limit) is not int or not 0 <= limit <= HARD_WEEKLY_LIMIT:
            raise sqlite3.DatabaseError("self repair budget limit is invalid")
        return limit

    @staticmethod
    def _usage(connection: sqlite3.Connection, week: str | None) -> tuple[int, int]:
        used = pending = 0
        rows = connection.execute("SELECT reservation_id, first_week, last_week, max_tokens, used_tokens FROM repair_budget_reservations")
        for reservation_id, first, last, maximum, actual in rows:
            if not _valid_id(reservation_id):
                raise sqlite3.DatabaseError("self repair budget reservation ID is invalid")
            _validate_week(first)
            _validate_week(last)
            if first > last:
                raise sqlite3.DatabaseError("self repair budget week range is invalid")
            if type(maximum) is not int or not 0 <= maximum <= HARD_WEEKLY_LIMIT:
                raise sqlite3.DatabaseError("self repair budget reservation is invalid")
            if actual is None:
                used += maximum
                pending += 1
            else:
                if type(actual) is not int or not 0 <= actual <= maximum:
                    raise sqlite3.DatabaseError("self repair budget settlement is invalid")
                if week is not None and first <= week <= last:
                    used += actual
        return used, pending

    @staticmethod
    def _touch_pending(connection: sqlite3.Connection, week: str) -> None:
        # W1 -> W4 -> W2 settlement must still charge the observed W3/W4 range.
        connection.execute(
            "UPDATE repair_budget_reservations "
            "SET first_week = MIN(first_week, ?), last_week = MAX(last_week, ?) "
            "WHERE used_tokens IS NULL AND (first_week > ? OR last_week < ?)",
            (week, week, week, week),
        )

    def reserve(self, max_tokens: int, *, now: datetime | None = None) -> str | None:
        if type(max_tokens) is not int or not 0 <= max_tokens <= HARD_WEEKLY_LIMIT:
            return None
        with self._transaction() as connection:
            try:
                week = _week(now)
            except ValueError:
                return None
            limit = self._validate(connection)
            used, _ = self._usage(connection, week)
            self._touch_pending(connection, week)
            if used + max_tokens > limit:
                return None
            reservation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO repair_budget_reservations (reservation_id, first_week, last_week, max_tokens) VALUES (?, ?, ?, ?)",
                (reservation_id, week, week, max_tokens),
            )
        return reservation_id

    def settle(self, reservation_id: str, used_tokens: int, *, now: datetime | None = None) -> bool:
        if not _valid_id(reservation_id):
            return False
        with self._transaction() as connection:
            try:
                week = _week(now)
            except ValueError:
                return False
            self._validate(connection)
            self._usage(connection, week)
            self._touch_pending(connection, week)
            if type(used_tokens) is not int or not 0 <= used_tokens <= HARD_WEEKLY_LIMIT:
                return False
            cursor = connection.execute(
                "UPDATE repair_budget_reservations SET used_tokens = ? "
                "WHERE reservation_id = ? AND used_tokens IS NULL AND max_tokens >= ?",
                (used_tokens, reservation_id, used_tokens),
            )
            return cursor.rowcount == 1

    def snapshot(self, *, now: datetime | None = None) -> dict:
        with self._transaction() as connection:
            week = _week(now)
            limit = self._validate(connection)
            used, pending = self._usage(connection, week)
            self._touch_pending(connection, week)
            return {"week": week, "limit": limit, "used": used, "remaining": max(0, limit - used), "pending": pending}