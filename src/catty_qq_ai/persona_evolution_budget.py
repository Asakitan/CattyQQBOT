from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path


HARD_DAILY_LIMIT = 30_000_000
_APPLICATION_ID = 0x43544247
_SCHEMA_VERSION = 1
_META_SQL = """CREATE TABLE ledger_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    daily_limit INTEGER NOT NULL CHECK (
        typeof(daily_limit) = 'integer' AND daily_limit BETWEEN 0 AND 30000000
    )
)"""
_RESERVATIONS_SQL = """CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY NOT NULL,
    start_day TEXT NOT NULL,
    max_tokens INTEGER NOT NULL CHECK (
        typeof(max_tokens) = 'integer' AND max_tokens BETWEEN 0 AND 30000000
    ),
    used_tokens INTEGER,
    end_day TEXT,
    CHECK (
        (used_tokens IS NULL AND end_day IS NULL) OR
        (typeof(used_tokens) = 'integer' AND used_tokens BETWEEN 0 AND max_tokens
         AND end_day IS NOT NULL)
    )
)"""
_EXPECTED_SCHEMA = {
    ("table", "ledger_meta", _META_SQL),
    ("table", "reservations", _RESERVATIONS_SQL),
}


class _UninitializedLedger(sqlite3.DatabaseError):
    pass


class BudgetLedger:
    def __init__(self, path, daily_limit=HARD_DAILY_LIMIT):
        if type(daily_limit) is not int or not 0 <= daily_limit <= HARD_DAILY_LIMIT:
            raise ValueError("daily_limit must be an integer between 0 and 30000000")
        if path is None or os.fspath(path) in ("", ":memory:", b"", b":memory:"):
            raise ValueError("path must name a persistent SQLite database")
        self._path = Path(path).resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._uri = self._path.as_uri() + "?mode=rw"
        self._daily_limit = daily_limit
        created = False
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            created = True
            os.close(fd)
        self._initialize(created)

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self._uri, uri=True, isolation_level=None, timeout=30)
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self, created):
        deadline = time.monotonic() + 2.0
        while True:
            try:
                with self._transaction() as connection:
                    if created:
                        objects = connection.execute(
                            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                        ).fetchall()
                        if objects:
                            raise sqlite3.DatabaseError("new budget ledger is not empty")
                        connection.execute(_META_SQL)
                        connection.execute(_RESERVATIONS_SQL)
                        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                        connection.execute(
                            "INSERT INTO ledger_meta (singleton, daily_limit) VALUES (1, ?)",
                            (self._daily_limit,),
                        )
                    else:
                        self._validate(connection)
                        self._usage(connection, date.today().isoformat())
                        # A lower configured limit cannot be raised by another process.
                        connection.execute(
                            "UPDATE ledger_meta SET daily_limit = MIN(daily_limit, ?) "
                            "WHERE singleton = 1",
                            (self._daily_limit,),
                        )
                return
            except _UninitializedLedger:
                if created or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _validate(self, connection):
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError("budget ledger integrity check failed")
        schema = set(connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ))
        if not schema and self._path.stat().st_size == 0:
            raise _UninitializedLedger("budget ledger is empty or initialization is incomplete")
        if schema != _EXPECTED_SCHEMA:
            raise sqlite3.DatabaseError("budget ledger schema is invalid")
        if connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
            raise sqlite3.DatabaseError("budget ledger application ID is invalid")
        if connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
            raise sqlite3.DatabaseError("budget ledger schema version is invalid")
        rows = connection.execute("SELECT singleton, daily_limit FROM ledger_meta").fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise sqlite3.DatabaseError("budget ledger metadata is invalid")
        limit = rows[0][1]
        if type(limit) is not int or not 0 <= limit <= HARD_DAILY_LIMIT:
            raise sqlite3.DatabaseError("budget ledger limit is invalid")
        return limit

    @staticmethod
    def _day(now):
        if now is None:
            now = datetime.now()
        if not isinstance(now, datetime):
            raise ValueError("now must be a datetime")
        if now.tzinfo is not None and now.utcoffset() is not None:
            now = now.astimezone()
        return now.date().isoformat()

    @staticmethod
    def _usage(connection, day):
        total = 0
        rows = connection.execute(
            "SELECT reservation_id, start_day, max_tokens, used_tokens, end_day "
            "FROM reservations"
        )
        for reservation_id, start_day, maximum, used, end_day in rows:
            if (not isinstance(reservation_id, str) or len(reservation_id) != 32
                    or any(char not in "0123456789abcdef" for char in reservation_id)):
                raise sqlite3.DatabaseError("budget ledger reservation ID is invalid")
            try:
                if not isinstance(start_day, str) or date.fromisoformat(start_day).isoformat() != start_day:
                    raise ValueError("invalid start day")
                if end_day is not None and (
                    not isinstance(end_day, str) or date.fromisoformat(end_day).isoformat() != end_day
                ):
                    raise ValueError("invalid end day")
            except (TypeError, ValueError) as exc:
                raise sqlite3.DatabaseError("budget ledger date is invalid") from exc
            if type(maximum) is not int or not 0 <= maximum <= HARD_DAILY_LIMIT:
                raise sqlite3.DatabaseError("budget ledger reservation is invalid")
            if used is None:
                if end_day is not None:
                    raise sqlite3.DatabaseError("budget ledger pending state is invalid")
                # A clock rollback must not make unresolved reservations disappear.
                total += maximum
            else:
                if type(used) is not int or not 0 <= used <= maximum or end_day is None:
                    raise sqlite3.DatabaseError("budget ledger settlement is invalid")
                if min(start_day, end_day) <= day <= max(start_day, end_day):
                    total += used
        return total

    def reserve(self, max_tokens, *, now=None) -> str | None:
        if type(max_tokens) is not int or not 0 <= max_tokens <= HARD_DAILY_LIMIT:
            return None
        if now is not None and not isinstance(now, datetime):
            return None
        with self._transaction() as connection:
            limit = self._validate(connection)
            day = self._day(now)
            if self._usage(connection, day) + max_tokens > limit:
                return None
            reservation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO reservations (reservation_id, start_day, max_tokens) VALUES (?, ?, ?)",
                (reservation_id, day, max_tokens),
            )
        return reservation_id

    def settle(self, reservation_id, used_tokens, *, now=None) -> bool:
        if (not isinstance(reservation_id, str) or not reservation_id
                or type(used_tokens) is not int or not 0 <= used_tokens <= HARD_DAILY_LIMIT):
            return False
        if now is not None and not isinstance(now, datetime):
            return False
        with self._transaction() as connection:
            self._validate(connection)
            day = self._day(now)
            self._usage(connection, day)
            cursor = connection.execute(
                "UPDATE reservations SET used_tokens = ?, end_day = ? "
                "WHERE reservation_id = ? AND used_tokens IS NULL AND max_tokens >= ?",
                (used_tokens, day, reservation_id, used_tokens),
            )
            return cursor.rowcount == 1

    def used_today(self, *, now=None) -> int:
        with self._transaction() as connection:
            self._validate(connection)
            return self._usage(connection, self._day(now))
