from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from .task_contracts import TASK_ROLES, TERMINAL_STATES, TaskError, TaskIdentity, TaskPolicy, TaskResult


_ACTIVE = ("confirming", "queued", "running", "interrupted", "result_ready", "delivering", "delivery_unknown")
_CANCELLABLE = ("quoted", "confirming", "queued", "running", "interrupted", "result_ready")


def _json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text) > 2_000_000:
        raise TaskError("payload_too_large", "task payload exceeds the storage limit")
    return text


def _identity(value: TaskIdentity) -> TaskIdentity:
    if not isinstance(value, TaskIdentity):
        raise TypeError("a verified TaskIdentity is required")
    return value


def _count(value: int, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TaskError("invalid_usage", f"{name} must be a nonnegative integer")
    return value


class TaskBudget:
    def __init__(self, store: TaskStore, task_id: str):
        self.store = store
        self.task_id = task_id

    def reserve_request(self, input_tokens: int, max_output_tokens: int) -> str:
        return self.store._reserve_request(self.task_id, input_tokens, max_output_tokens)

    def settle_request(
        self, reservation_id: str, prompt_tokens: int | None, completion_tokens: int | None,
        *, receipt_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store._settle_request(self.task_id, reservation_id, prompt_tokens, completion_tokens, receipt_id)

    def snapshot(self) -> dict[str, Any]:
        with self.store._transaction() as connection:
            task = self.store._find(connection, self.task_id)
            return self.store._budget_snapshot(connection, task)

    @property
    def remaining_tokens(self) -> int:
        return int(self.snapshot()["remaining_tokens"])

    def raise_if_cancelled(self) -> None:
        with self.store._transaction() as connection:
            row = self.store._find(connection, self.task_id)
            if row["cancel_requested"] or row["state"] not in ("running", "queued"):
                raise TaskError("cancelled", "the task is no longer executable")


class TaskStore:
    def __init__(self, path: str | Path, policy: TaskPolicy, *, clock: Callable[[], float] = time.time):
        self.policy = policy
        self.clock = clock
        self.timezone = ZoneInfo(policy.timezone)
        self.path = Path(path).expanduser().resolve()
        if self.path.is_dir():
            self.path = self.path / "tasks.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS task_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO task_meta(singleton,revision) VALUES(1,0);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, request_id TEXT NOT NULL,
                user_id TEXT NOT NULL, scope TEXT NOT NULL, persona TEXT NOT NULL, is_owner INTEGER NOT NULL,
                title TEXT NOT NULL, role TEXT NOT NULL, description TEXT NOT NULL, tier TEXT NOT NULL,
                payload TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                points INTEGER NOT NULL, max_tokens INTEGER NOT NULL, max_steps INTEGER NOT NULL,
                timeout_seconds INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                expires_at REAL NOT NULL, confirmed_at REAL, quota_day TEXT, tokens_held INTEGER NOT NULL DEFAULT 0,
                payment_state TEXT NOT NULL DEFAULT 'none', cancel_requested INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT, lease_owner TEXT, lease_until REAL, result TEXT, error TEXT, delivery TEXT,
                schedule_id TEXT, reminder INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, scope, persona, request_id)
            );
            CREATE INDEX IF NOT EXISTS task_queue ON tasks(state, created_at);
            CREATE INDEX IF NOT EXISTS task_user ON tasks(user_id, state);
            CREATE TABLE IF NOT EXISTS day_usage (
                user_id TEXT NOT NULL, day TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, day), CHECK(used >= 0 AND reserved >= 0)
            );
            CREATE TABLE IF NOT EXISTS tier_usage (
                user_id TEXT NOT NULL, day TEXT NOT NULL, tier TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, day, tier)
            );
            CREATE TABLE IF NOT EXISTS token_requests (
                reservation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                day TEXT NOT NULL, reserved INTEGER NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
                receipt_id TEXT UNIQUE, state TEXT NOT NULL DEFAULT 'reserved', created_at REAL NOT NULL,
                CHECK(reserved >= 0)
            );
            CREATE TABLE IF NOT EXISTS task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                kind TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delivery_receipts (
                receipt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id)
            );
            CREATE TABLE IF NOT EXISTS schedules (
                schedule_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, user_id TEXT NOT NULL,
                scope TEXT NOT NULL, persona TEXT NOT NULL, is_owner INTEGER NOT NULL,
                template TEXT NOT NULL, spec TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                next_run REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                claim_token TEXT, claim_until REAL, run_id TEXT, last_task_id TEXT, last_error TEXT,
                UNIQUE(user_id, scope, persona, request_id)
            );
            CREATE INDEX IF NOT EXISTS schedule_due ON schedules(state, next_run);
        """)
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(tasks)")}
        if "schedule_id" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN schedule_id TEXT")
        if "reminder" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN reminder INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            before = self._connection.total_changes
            try:
                yield self._connection
                if self._connection.total_changes != before:
                    self._connection.execute("UPDATE task_meta SET revision=revision+1 WHERE singleton=1")
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def revision(self) -> int:
        with self._transaction() as connection:
            return int(connection.execute("SELECT revision FROM task_meta WHERE singleton=1").fetchone()[0])

    @staticmethod
    def _check_revision(connection, expected_revision: int | None) -> None:
        if expected_revision is not None:
            current = connection.execute("SELECT revision FROM task_meta WHERE singleton=1").fetchone()[0]
            if type(expected_revision) is not int or expected_revision != current:
                raise TaskError("stale_revision", "task state changed; refresh before retrying")

    def _day(self, now: float | None = None) -> str:
        return datetime.fromtimestamp(self.clock() if now is None else now, self.timezone).date().isoformat()

    @staticmethod
    def identity_of(record: Mapping[str, Any]) -> TaskIdentity:
        return TaskIdentity(str(record["user_id"]), str(record["scope"]), str(record["persona"]), bool(record["is_owner"]))

    @staticmethod
    def _find(connection, task_id: str, identity: TaskIdentity | None = None):
        if identity is None:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        else:
            identity = _identity(identity)
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=? AND user_id=? AND scope=? AND persona=?",
                (task_id, identity.user_id, identity.scope, identity.persona),
            ).fetchone()
        if row is None:
            raise TaskError("not_found", "task not found")
        return row

    @staticmethod
    def _record(row) -> dict[str, Any]:
        result = dict(row)
        for key in ("payload", "result", "delivery", "error", "template", "spec", "last_error"):
            if key in result:
                result[key] = json.loads(result[key]) if result[key] is not None else None
        if "is_owner" in result:
            result["is_owner"] = bool(result["is_owner"])
        return result

    def _event(self, connection, task_id: str, kind: str, data: Any = None):
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise TaskError("invalid_event", "invalid event name")
        connection.execute("INSERT INTO task_events(task_id,kind,data,created_at) VALUES(?,?,?,?)", (task_id, kind, _json(data or {}), self.clock()))

    @staticmethod
    def _day_row(connection, user_id: str, day: str):
        connection.execute("INSERT OR IGNORE INTO day_usage(user_id,day) VALUES(?,?)", (user_id, day))
        return connection.execute("SELECT * FROM day_usage WHERE user_id=? AND day=?", (user_id, day)).fetchone()

    def _price(self, connection, identity: TaskIdentity, tier: str, day: str) -> int:
        policy = self.policy.tier(tier)
        row = connection.execute("SELECT attempts FROM tier_usage WHERE user_id=? AND day=? AND tier=?", (identity.user_id, day, tier)).fetchone()
        count = int(row[0]) if row is not None else 0
        return 0 if identity.is_owner or count < policy.free_per_day else policy.points

    def quote(
        self, identity: TaskIdentity, request_id: str, title: str, role: str, description: str,
        tier: str = "light", payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        identity = _identity(identity)
        if not self.policy.enabled:
            raise TaskError("disabled", "task execution is disabled")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise TaskError("invalid_request", "a bounded request id is required")
        if role not in TASK_ROLES:
            raise TaskError("invalid_role", "unsupported task role")
        if not isinstance(title, str) or not title.strip() or len(title) > 256:
            raise TaskError("invalid_title", "invalid task title")
        if not isinstance(description, str) or not description.strip() or len(description) > 100_000:
            raise TaskError("invalid_description", "invalid task description")
        if payload is not None and not isinstance(payload, Mapping):
            raise TaskError("invalid_payload", "task payload must be an object")
        configured = self.policy.tier(tier)
        serialized = _json(dict(payload or {}))
        fingerprint = hashlib.sha256(_json([title, role, description, tier, json.loads(serialized)]).encode()).hexdigest()
        now = self.clock()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE user_id=? AND scope=? AND persona=? AND request_id=?",
                (identity.user_id, identity.scope, identity.persona, request_id),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise TaskError("request_conflict", "request id already has a different payload")
                return self._record(existing)
            task_id = uuid.uuid4().hex
            connection.execute("""
                INSERT INTO tasks(task_id,root_id,request_id,user_id,scope,persona,is_owner,title,role,description,
                    tier,payload,fingerprint,state,points,max_tokens,max_steps,timeout_seconds,created_at,updated_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'quoted',?,?,?,?,?,?,?)
                """, (
                    task_id, task_id, request_id, identity.user_id, identity.scope, identity.persona, int(identity.is_owner),
                    title, role, description, tier, serialized, fingerprint,
                    self._price(connection, identity, tier, self._day(now)), configured.max_tokens,
                    configured.max_steps, configured.timeout_seconds, now, now, now + self.policy.quote_ttl_seconds,
                ))
            self._event(connection, task_id, "quoted")
            return self._record(self._find(connection, task_id))

    def confirm(self, task_id: str, identity: TaskIdentity, account) -> dict[str, Any]:
        identity = _identity(identity)
        with self._transaction() as connection:
            task = self._find(connection, task_id, identity)
            if task["state"] not in ("quoted", "confirming"):
                return self._record(task)
            now = self.clock()
            if task["state"] == "quoted" and task["expires_at"] <= now:
                connection.execute("UPDATE tasks SET state='expired',payment_state='refund_pending',updated_at=? WHERE task_id=?", (now, task_id))
                self._event(connection, task_id, "expired")
            else:
                if bool(task["is_owner"]) != identity.is_owner:
                    raise TaskError("identity_changed", "task ownership privileges changed; request a new quote")
                if not self.policy.enabled:
                    raise TaskError("disabled", "task execution is disabled")
                if task["state"] == "quoted":
                    day = self._day(now)
                    price = self._price(connection, identity, task["tier"], day)
                    if price != task["points"]:
                        connection.execute("UPDATE tasks SET points=?,updated_at=? WHERE task_id=?", (price, now, task_id))
                        self._event(connection, task_id, "quote_changed", {"points": price})
                        result = self._record(self._find(connection, task_id))
                        result["requires_confirmation"] = True
                        return result
                    placeholders = ",".join("?" for _ in _ACTIVE)
                    active = connection.execute(f"SELECT COUNT(*) FROM tasks WHERE user_id=? AND state IN ({placeholders})", (identity.user_id, *_ACTIVE)).fetchone()[0]
                    if not identity.is_owner and active >= self.policy.per_user_active:
                        raise TaskError("active_limit", "user already has the maximum active tasks across conversations")
                    daily = self._day_row(connection, identity.user_id, day)
                    if not identity.is_owner and daily["attempts"] >= self.policy.daily_attempt_limit:
                        raise TaskError("daily_attempt_limit", "daily task limit reached")
                    if not identity.is_owner and daily["used"] + daily["reserved"] + task["max_tokens"] > self.policy.daily_token_limit:
                        raise TaskError("daily_token_limit", "not enough daily token allowance for this task")
                    connection.execute("""
                        UPDATE tasks SET state='confirming',payment_state='reserving',confirmed_at=?,updated_at=?,quota_day=?,tokens_held=?
                        WHERE task_id=?
                        """, (now, now, day, task["max_tokens"], task_id))
                    connection.execute("UPDATE day_usage SET attempts=attempts+1,reserved=reserved+? WHERE user_id=? AND day=?", (task["max_tokens"], identity.user_id, day))
                    connection.execute("""
                        INSERT INTO tier_usage(user_id,day,tier,attempts) VALUES(?,?,?,1)
                        ON CONFLICT(user_id,day,tier) DO UPDATE SET attempts=attempts+1
                        """, (identity.user_id, day, task["tier"]))
                    self._event(connection, task_id, "confirmation_started", {"points": price, "quota_day": day})
            current = self._record(self._find(connection, task_id))
        if current["state"] == "expired":
            return self._finish_payment(task_id, account)
        error = None
        with self._transaction() as connection:
            task = self._find(connection, task_id)
            if task["state"] != "confirming":
                return self._record(task)
            try:
                reservation = account.reserve_task_points(identity.user_id, task_id, task["points"])
            except Exception as exc:
                raise TaskError("persistence_failed", "point reservation must be retried using the same task id") from exc
            if not isinstance(reservation, Mapping) or not reservation.get("ok"):
                error = str(reservation.get("error", "reservation_failed")) if isinstance(reservation, Mapping) else "reservation_failed"
                if error == "insufficient_points":
                    self._release_hold(connection, task)
                    connection.execute("UPDATE day_usage SET attempts=attempts-1 WHERE user_id=? AND day=?", (task["user_id"], task["quota_day"]))
                    connection.execute("UPDATE tier_usage SET attempts=attempts-1 WHERE user_id=? AND day=? AND tier=?", (task["user_id"], task["quota_day"], task["tier"]))
                    connection.execute("UPDATE tasks SET state='quoted',payment_state='none',confirmed_at=NULL,quota_day=NULL,updated_at=? WHERE task_id=?", (self.clock(), task_id))
                self._event(connection, task_id, "confirmation_failed", {"error": error})
            else:
                connection.execute("UPDATE tasks SET state='queued',payment_state='reserved',updated_at=? WHERE task_id=?", (self.clock(), task_id))
                self._event(connection, task_id, "confirmed", {"points": task["points"], "quota_day": task["quota_day"]})
        if error is not None:
            raise TaskError(error, "points could not be durably reserved")
        return self._finish_payment(task_id, account)

    def resume_confirmations(self, account) -> int:
        if not self.policy.enabled:
            return 0
        with self._transaction() as connection:
            rows = [self._record(row) for row in connection.execute("SELECT * FROM tasks WHERE state='confirming'").fetchall()]
        for task in rows:
            try:
                self.confirm(task["task_id"], self.identity_of(task), account)
            except TaskError:
                continue
        return len(rows)

    def get_task(self, task_id: str, identity: TaskIdentity) -> dict[str, Any]:
        with self._transaction() as connection:
            return self._record(self._find(connection, task_id, _identity(identity)))

    def list_tasks(self, identity: TaskIdentity, limit: int = 50) -> list[dict[str, Any]]:
        identity = _identity(identity)
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM tasks WHERE user_id=? AND scope=? AND persona=? ORDER BY created_at DESC,task_id LIMIT ?", (identity.user_id, identity.scope, identity.persona, min(max(int(limit), 1), 200))).fetchall()
            return [self._record(row) for row in rows]

    def admin_get_task(self, task_id: str, identity: TaskIdentity | None = None) -> dict[str, Any]:
        if identity is not None and not _identity(identity).is_owner:
            raise TaskError("forbidden", "owner privileges required")
        with self._transaction() as connection:
            return self._record(self._find(connection, task_id))

    def admin_list_tasks(self, status: str | None = None, user_id: str | None = None, limit: int = 50, offset: int = 0, *, identity: TaskIdentity | None = None) -> list[dict[str, Any]]:
        if identity is not None and not _identity(identity).is_owner:
            raise TaskError("forbidden", "owner privileges required")
        predicates = []
        parameters: list[Any] = []
        if status is not None:
            predicates.append("state=?")
            parameters.append(str(status))
        if user_id is not None:
            predicates.append("user_id=?")
            parameters.append(str(user_id))
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        with self._transaction() as connection:
            return [self._record(row) for row in connection.execute(f"SELECT * FROM tasks{where} ORDER BY created_at DESC,task_id LIMIT ? OFFSET ?", (*parameters, min(max(int(limit), 1), 500), max(int(offset), 0))).fetchall()]

    def admin_task_count(self, status: str | None = None, user_id: str | None = None) -> int:
        predicates = []
        parameters = []
        if status is not None:
            predicates.append("state=?")
            parameters.append(str(status))
        if user_id is not None:
            predicates.append("user_id=?")
            parameters.append(str(user_id))
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        with self._transaction() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM tasks{where}", parameters).fetchone()[0])

    def admin_events(self, task_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            if task_id is None:
                rows = connection.execute("SELECT * FROM task_events ORDER BY event_id DESC LIMIT ?", (min(max(int(limit), 1), 1000),)).fetchall()
            else:
                self._find(connection, task_id)
                rows = connection.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY event_id DESC LIMIT ?", (task_id, min(max(int(limit), 1), 1000))).fetchall()
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def admin_list_schedules(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            return [self._record(row) for row in connection.execute("SELECT * FROM schedules ORDER BY created_at DESC LIMIT ?", (min(max(int(limit), 1), 500),)).fetchall()]

    def _release_hold(self, connection, task) -> None:
        if task["tokens_held"]:
            connection.execute("UPDATE day_usage SET reserved=reserved-? WHERE user_id=? AND day=?", (task["tokens_held"], task["user_id"], task["quota_day"]))
            connection.execute("UPDATE tasks SET tokens_held=0 WHERE task_id=?", (task["task_id"],))

    def _settle_points(self, connection, task_id: str, account, charge: bool) -> None:
        task = self._find(connection, task_id)
        if task["payment_state"] in ("none", "charged", "refunded"):
            return
        pending = "charge_pending" if charge else "refund_pending"
        connection.execute("UPDATE tasks SET payment_state=? WHERE task_id=?", (pending, task_id))
        try:
            result = account.settle_task_points(task["user_id"], task_id, charge)
        except Exception as exc:
            self._event(connection, task_id, "payment_retry", {"error": type(exc).__name__})
            return
        if isinstance(result, Mapping) and result.get("ok"):
            connection.execute("UPDATE tasks SET payment_state=? WHERE task_id=?", ("charged" if charge else "refunded", task_id))
        else:
            self._event(connection, task_id, "payment_retry", {"error": result.get("error", "settlement_failed") if isinstance(result, Mapping) else "settlement_failed"})

    def _finish_payment(self, task_id: str, account) -> dict[str, Any]:
        with self._transaction() as connection:
            task = self._find(connection, task_id)
            if account is not None and task["payment_state"] in ("charge_pending", "refund_pending"):
                self._settle_points(connection, task_id, account, task["payment_state"] == "charge_pending")
            return self._record(self._find(connection, task_id))

    def reconcile_payments(self, account) -> int:
        with self._transaction() as connection:
            rows = connection.execute("SELECT task_id,payment_state FROM tasks WHERE payment_state IN ('charge_pending','refund_pending')").fetchall()
            for row in rows:
                self._settle_points(connection, row["task_id"], account, row["payment_state"] == "charge_pending")
            return len(rows)

    def expire_quotes(self, account) -> int:
        with self._transaction() as connection:
            rows = connection.execute("SELECT task_id FROM tasks WHERE state='quoted' AND expires_at<=?", (self.clock(),)).fetchall()
            for row in rows:
                connection.execute("UPDATE tasks SET state='expired',payment_state='refund_pending',updated_at=? WHERE task_id=?", (self.clock(), row["task_id"]))
                self._event(connection, row["task_id"], "expired")
        for row in rows:
            self._finish_payment(row["task_id"], account)
        return len(rows)

    def cancel(self, task_id: str, identity: TaskIdentity, account, *, expected_revision: int | None = None) -> dict[str, Any]:
        with self._transaction() as connection:
            self._check_revision(connection, expected_revision)
            task = self._find(connection, task_id, _identity(identity))
            if task["state"] in TERMINAL_STATES:
                pass
            elif task["state"] in ("delivering", "delivery_unknown"):
                connection.execute("UPDATE tasks SET cancel_requested=1,state='delivery_unknown',updated_at=? WHERE task_id=?", (self.clock(), task_id))
                self._event(connection, task_id, "cancel_awaiting_delivery_reconciliation")
            elif task["state"] in _CANCELLABLE:
                connection.execute("UPDATE tasks SET cancel_requested=1,state='cancelled',updated_at=? WHERE task_id=?", (self.clock(), task_id))
                if task["payment_state"] in ("none", "reserving", "reserved"):
                    connection.execute("UPDATE tasks SET payment_state='refund_pending' WHERE task_id=?", (task_id,))
                self._release_hold(connection, task)
                self._event(connection, task_id, "cancelled")
        return self._finish_payment(task_id, account)

    def claim(self, worker_id: str = "worker", *, lease_seconds: float = 30) -> dict[str, Any] | None:
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        with self._transaction() as connection:
            if connection.execute("SELECT COUNT(*) FROM tasks WHERE state='running'").fetchone()[0] >= self.policy.global_concurrency:
                return None
            task = connection.execute("SELECT * FROM tasks WHERE state='queued' AND cancel_requested=0 ORDER BY created_at,task_id LIMIT 1").fetchone()
            if task is None:
                return None
            now = self.clock()
            token = uuid.uuid4().hex
            connection.execute("UPDATE tasks SET state='running',lease_token=?,lease_owner=?,lease_until=?,updated_at=? WHERE task_id=? AND state='queued'", (token, str(worker_id), now + lease_seconds, now, task["task_id"]))
            self._event(connection, task["task_id"], "claimed", {"worker": str(worker_id)})
            return self._record(self._find(connection, task["task_id"]))

    def renew_lease(self, task_id: str, lease_token: str, *, lease_seconds: float = 30) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute("UPDATE tasks SET lease_until=? WHERE task_id=? AND lease_token=? AND state IN ('running','delivering') AND cancel_requested=0", (self.clock() + lease_seconds, task_id, lease_token))
            return cursor.rowcount == 1

    def recover_interrupted(self) -> int:
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM tasks WHERE state IN ('running','delivering') AND lease_until<=?", (self.clock(),)).fetchall()
            for task in rows:
                state = "delivery_unknown" if task["state"] == "delivering" else "interrupted"
                connection.execute("UPDATE tasks SET state=?,updated_at=? WHERE task_id=?", (state, self.clock(), task["task_id"]))
                self._event(connection, task["task_id"], state, {"reason": "lease_expired"})
            return len(rows)

    def mark_interrupted(self, task_id: str, lease_token: str) -> None:
        with self._transaction() as connection:
            task = self._find(connection, task_id)
            if task["lease_token"] == lease_token and task["state"] in ("running", "delivering"):
                state = "delivery_unknown" if task["state"] == "delivering" else "interrupted"
                connection.execute("UPDATE tasks SET state=?,updated_at=? WHERE task_id=?", (state, self.clock(), task_id))
                self._event(connection, task_id, state, {"reason": "worker_stopped"})

    def resume(self, task_id: str, identity: TaskIdentity) -> dict[str, Any]:
        with self._transaction() as connection:
            task = self._find(connection, task_id, _identity(identity))
            if task["state"] != "interrupted" or task["cancel_requested"]:
                raise TaskError("not_resumable", "only explicitly interrupted tasks may be resumed")
            connection.execute("UPDATE tasks SET state='queued',lease_token=NULL,lease_until=NULL,updated_at=? WHERE task_id=?", (self.clock(), task_id))
            self._event(connection, task_id, "resumed", {"previous_effects": "caller_acknowledged"})
            return self._record(self._find(connection, task_id))

    def complete(
        self, task_id: str, result: TaskResult | None, account=None,
        *, lease_token: str | None = None, error: str | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if result is not None and not isinstance(result, TaskResult):
            raise TypeError("executor result must be TaskResult")
        if result is None and error is None:
            raise ValueError("a result or explicit error is required")
        with self._transaction() as connection:
            task = self._find(connection, task_id)
            if task["state"] != "running":
                return self._record(task)
            if lease_token is not None and task["lease_token"] != lease_token:
                raise TaskError("stale_lease", "task claim is no longer current")
            state = "failed" if error is not None else "result_ready"
            connection.execute("UPDATE tasks SET state=?,result=?,error=?,updated_at=? WHERE task_id=?", (state, _json(result.as_dict()) if result is not None else None, _json(error) if error is not None else None, self.clock(), task_id))
            self._release_hold(connection, task)
            if error is not None:
                connection.execute("UPDATE tasks SET payment_state='refund_pending' WHERE task_id=? AND payment_state='reserved'", (task_id,))
            self._event(connection, task_id, state)
        return self._finish_payment(task_id, account)

    def claim_delivery(self, worker_id: str = "delivery", *, lease_seconds: float = 30) -> dict[str, Any] | None:
        with self._transaction() as connection:
            task = connection.execute("SELECT * FROM tasks WHERE state='result_ready' AND cancel_requested=0 ORDER BY updated_at,task_id LIMIT 1").fetchone()
            if task is None:
                return None
            connection.execute("UPDATE tasks SET state='delivering',lease_token=?,lease_owner=?,lease_until=?,updated_at=? WHERE task_id=?", (uuid.uuid4().hex, str(worker_id), self.clock() + lease_seconds, self.clock(), task["task_id"]))
            self._event(connection, task["task_id"], "delivery_started")
            return self._record(self._find(connection, task["task_id"]))

    def delivery_confirm(self, task_id: str, delivery: Mapping[str, Any], account) -> dict[str, Any]:
        if not isinstance(delivery, Mapping):
            raise TypeError("delivery must be a status object")
        status = delivery.get("status")
        if status in ("sent", "confirmed", "completed"):
            status = "delivered"
        if status not in ("delivered", "delivery_unknown", "failed", "not_delivered"):
            raise TaskError("invalid_delivery", "delivery status must be explicit")
        with self._transaction() as connection:
            task = self._find(connection, task_id)
            if task["state"] == "completed":
                already_completed = True
            elif task["state"] not in ("delivering", "delivery_unknown"):
                raise TaskError("invalid_state", "task is not awaiting a delivery receipt")
            else:
                already_completed = False
            if not already_completed:
                receipt = delivery.get("receipt_id", delivery.get("message_id"))
                if status == "delivered" and receipt is not None:
                    receipt = str(receipt)
                    existing = connection.execute("SELECT task_id FROM delivery_receipts WHERE receipt_id=?", (receipt,)).fetchone()
                    if existing is not None and existing[0] != task_id:
                        raise TaskError("receipt_conflict", "delivery receipt belongs to another task")
                    connection.execute("INSERT OR IGNORE INTO delivery_receipts(receipt_id,task_id) VALUES(?,?)", (receipt, task_id))
                state = {"delivered": "completed", "delivery_unknown": "delivery_unknown", "failed": "failed", "not_delivered": "failed"}[str(status)]
                connection.execute("UPDATE tasks SET state=?,delivery=?,updated_at=? WHERE task_id=?", (state, _json(dict(delivery)), self.clock(), task_id))
                if status != "delivery_unknown" and task["payment_state"] == "reserved":
                    connection.execute("UPDATE tasks SET payment_state=? WHERE task_id=?", ("charge_pending" if status == "delivered" else "refund_pending", task_id))
                self._event(connection, task_id, state, {"delivery_status": status})
        return self._finish_payment(task_id, account)

    def append_event(self, task_id: str, kind: str, data: Any = None) -> None:
        with self._transaction() as connection:
            self._find(connection, task_id)
            self._event(connection, task_id, kind, data)

    def events(self, task_id: str, identity: TaskIdentity, limit: int = 100) -> list[dict[str, Any]]:
        with self._transaction() as connection:
            self._find(connection, task_id, _identity(identity))
            rows = connection.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY event_id DESC LIMIT ?", (task_id, min(max(int(limit), 1), 1000))).fetchall()
            return [{**dict(row), "data": json.loads(row["data"])} for row in reversed(rows)]

    def budget(self, task_id: str) -> TaskBudget:
        with self._transaction() as connection:
            self._find(connection, task_id)
        return TaskBudget(self, task_id)

    @staticmethod
    def _budget_snapshot(connection, task) -> dict[str, Any]:
        totals = connection.execute("""
            SELECT COALESCE(SUM(CASE WHEN state='settled' THEN prompt_tokens+completion_tokens ELSE 0 END),0) AS used,
                   COALESCE(SUM(CASE WHEN state='reserved' THEN reserved ELSE 0 END),0) AS reserved
            FROM token_requests WHERE task_id=?
            """, (task["task_id"],)).fetchone()
        return {"task_id": task["task_id"], "max_tokens": task["max_tokens"], "used_tokens": totals["used"], "reserved_tokens": totals["reserved"], "remaining_tokens": max(task["max_tokens"] - totals["used"] - totals["reserved"], 0), "cancel_requested": bool(task["cancel_requested"]), "state": task["state"]}

    def _reserve_request(self, task_id: str, input_tokens: int, max_output_tokens: int) -> str:
        amount = _count(input_tokens, "input_tokens") + _count(max_output_tokens, "max_output_tokens")
        if amount <= 0:
            raise TaskError("invalid_usage", "a request must reserve a positive token allowance")
        with self._transaction() as connection:
            task = self._find(connection, task_id)
            if task["state"] != "running" or task["cancel_requested"]:
                raise TaskError("cancelled", "task is not running")
            if self._budget_snapshot(connection, task)["remaining_tokens"] < amount:
                raise TaskError("task_token_limit", "request exceeds the remaining root task budget")
            day = self._day()
            held = min(amount, task["tokens_held"])
            if held:
                connection.execute("UPDATE day_usage SET reserved=reserved-? WHERE user_id=? AND day=?", (held, task["user_id"], task["quota_day"]))
            daily = self._day_row(connection, task["user_id"], day)
            if not task["is_owner"] and daily["used"] + daily["reserved"] + amount > self.policy.daily_token_limit:
                raise TaskError("daily_token_limit", "request exceeds the user's daily token budget")
            reservation_id = uuid.uuid4().hex
            connection.execute("UPDATE day_usage SET reserved=reserved+? WHERE user_id=? AND day=?", (amount, task["user_id"], day))
            connection.execute("UPDATE tasks SET tokens_held=tokens_held-? WHERE task_id=?", (held, task_id))
            connection.execute("INSERT INTO token_requests(reservation_id,task_id,day,reserved,created_at) VALUES(?,?,?,?,?)", (reservation_id, task_id, day, amount, self.clock()))
            self._event(connection, task_id, "request_reserved", {"reservation_id": reservation_id, "tokens": amount})
            return reservation_id

    def _settle_request(self, task_id: str, reservation_id: str, prompt_tokens: int | None, completion_tokens: int | None, receipt_id: str | None) -> dict[str, Any]:
        with self._transaction() as connection:
            task = self._find(connection, task_id)
            reservation = connection.execute("SELECT * FROM token_requests WHERE reservation_id=? AND task_id=?", (reservation_id, task_id)).fetchone()
            if reservation is None:
                raise TaskError("reservation_missing", "token reservation not found")
            if prompt_tokens is None or completion_tokens is None:
                self._event(connection, task_id, "usage_unknown", {"reservation_id": reservation_id})
                return dict(reservation)
            actual = _count(prompt_tokens, "prompt_tokens") + _count(completion_tokens, "completion_tokens")
            if reservation["state"] in ("settled", "duplicate"):
                if reservation["prompt_tokens"] != prompt_tokens or reservation["completion_tokens"] != completion_tokens:
                    raise TaskError("receipt_conflict", "usage changed for an already settled request")
                return dict(reservation)
            if receipt_id is not None:
                receipt_id = str(receipt_id)
                if not receipt_id or len(receipt_id) > 512:
                    raise TaskError("invalid_receipt", "invalid provider receipt")
                prior = connection.execute("SELECT * FROM token_requests WHERE receipt_id=?", (receipt_id,)).fetchone()
                if prior is not None:
                    if prior["task_id"] != task_id or prior["prompt_tokens"] != prompt_tokens or prior["completion_tokens"] != completion_tokens:
                        raise TaskError("receipt_conflict", "provider receipt belongs to a different request")
                    connection.execute("UPDATE day_usage SET reserved=reserved-? WHERE user_id=? AND day=?", (reservation["reserved"], task["user_id"], reservation["day"]))
                    connection.execute("UPDATE token_requests SET state='duplicate',prompt_tokens=?,completion_tokens=? WHERE reservation_id=?", (prompt_tokens, completion_tokens, reservation_id))
                    self._event(connection, task_id, "duplicate_receipt", {"reservation_id": reservation_id, "receipt_id": receipt_id})
                    return {**dict(reservation), "state": "duplicate", "duplicate_receipt": True}
            connection.execute("UPDATE day_usage SET reserved=reserved-?,used=used+? WHERE user_id=? AND day=?", (reservation["reserved"], actual, task["user_id"], reservation["day"]))
            connection.execute("UPDATE token_requests SET state='settled',prompt_tokens=?,completion_tokens=?,receipt_id=? WHERE reservation_id=?", (prompt_tokens, completion_tokens, receipt_id, reservation_id))
            self._event(connection, task_id, "usage_settled", {"reservation_id": reservation_id, "tokens": actual})
            return dict(connection.execute("SELECT * FROM token_requests WHERE reservation_id=?", (reservation_id,)).fetchone())

    def daily_usage(self, identity: TaskIdentity, day: str | None = None) -> dict[str, Any]:
        identity = _identity(identity)
        with self._transaction() as connection:
            return dict(self._day_row(connection, identity.user_id, day or self._day()))

    def _next_occurrence(self, spec: Mapping[str, Any], after: float) -> float | None:
        timezone = ZoneInfo(str(spec["timezone"]))
        value = str(spec["value"])
        if spec["kind"] == "once":
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone)
            return parsed.timestamp() if parsed.timestamp() > after else None
        start = datetime.fromtimestamp(after, timezone)
        if spec["kind"] == "daily":
            try:
                hour, minute = (int(part) for part in value.split(":"))
                candidate = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except (ValueError, TypeError):
                raise TaskError("invalid_schedule", "daily schedule must be HH:MM") from None
            if candidate.timestamp() <= after:
                candidate += timedelta(days=1)
            return candidate.timestamp()
        if spec["kind"] == "cron":
            if len(value.split()) != 5 or len(value) > 120:
                raise TaskError("invalid_schedule", "cron must contain five fields")
            try:
                from croniter import croniter

                return croniter(value, start, max_years_between_matches=5).get_next(datetime).timestamp()
            except ImportError:
                raise TaskError("schedule_dependency", "cron scheduling requires croniter") from None
            except (ValueError, KeyError, OverflowError):
                raise TaskError("invalid_schedule", "cron has no bounded next occurrence") from None
        raise TaskError("invalid_schedule", "unsupported schedule kind")

    def create_schedule(
        self, identity: TaskIdentity, request_id: str, title: str, role: str, description: str,
        *, when: str | None = None, cron: str | None = None, daily: str | None = None,
        tier: str = "light", payload: Mapping[str, Any] | None = None, reminder: bool = False,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        identity = _identity(identity)
        if not self.policy.enabled:
            raise TaskError("disabled", "task scheduling is disabled")
        if not request_id or len(request_id) > 256 or role not in TASK_ROLES or not title or len(title) > 256 or not description or len(description) > 100_000:
            raise TaskError("invalid_schedule", "invalid schedule template")
        self.policy.tier(tier)
        if sum(item is not None for item in (when, cron, daily)) != 1:
            raise TaskError("invalid_schedule", "specify exactly one schedule trigger")
        spec = {"kind": "once" if when is not None else "cron" if cron is not None else "daily", "value": when if when is not None else cron if cron is not None else daily, "timezone": timezone or self.policy.timezone}
        template = {"title": title, "role": role, "description": description, "tier": tier, "payload": dict(payload or {}), "reminder": bool(reminder)}
        fingerprint = hashlib.sha256(_json([template, spec]).encode()).hexdigest()
        now = self.clock()
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM schedules WHERE user_id=? AND scope=? AND persona=? AND request_id=?", (identity.user_id, identity.scope, identity.persona, request_id)).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise TaskError("request_conflict", "schedule request id has a different payload")
                return self._record(existing)
            local = datetime.fromtimestamp(now, self.timezone)
            day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            next_day = day_start + timedelta(days=1)
            created = connection.execute("SELECT COUNT(*) FROM schedules WHERE user_id=? AND created_at>=? AND created_at<?", (identity.user_id, day_start.timestamp(), next_day.timestamp())).fetchone()[0]
            if not identity.is_owner and created >= 5:
                raise TaskError("schedule_limit", "a user may create at most five schedules per day")
            next_run = self._next_occurrence(spec, now)
            if next_run is None:
                raise TaskError("invalid_schedule", "schedule time must be in the future")
            following = self._next_occurrence(spec, next_run)
            if following is not None and following - next_run < 60:
                raise TaskError("schedule_interval", "schedule interval must be at least sixty seconds")
            schedule_id = uuid.uuid4().hex
            connection.execute("""
                INSERT INTO schedules(schedule_id,request_id,user_id,scope,persona,is_owner,template,spec,fingerprint,state,next_run,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?)
                """, (schedule_id, request_id, identity.user_id, identity.scope, identity.persona, int(identity.is_owner), _json(template), _json(spec), fingerprint, next_run, now, now))
            return self._record(connection.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone())

    @staticmethod
    def _find_schedule(connection, schedule_id: str, identity: TaskIdentity):
        identity = _identity(identity)
        row = connection.execute("SELECT * FROM schedules WHERE schedule_id=? AND user_id=? AND scope=? AND persona=?", (schedule_id, identity.user_id, identity.scope, identity.persona)).fetchone()
        if row is None:
            raise TaskError("not_found", "schedule not found")
        return row

    def list_schedules(self, identity: TaskIdentity, limit: int = 50) -> list[dict[str, Any]]:
        identity = _identity(identity)
        with self._transaction() as connection:
            return [self._record(row) for row in connection.execute("SELECT * FROM schedules WHERE user_id=? AND scope=? AND persona=? ORDER BY created_at DESC LIMIT ?", (identity.user_id, identity.scope, identity.persona, min(max(int(limit), 1), 200))).fetchall()]

    def cancel_schedule(self, schedule_id: str, identity: TaskIdentity, account=None, *, expected_revision: int | None = None) -> dict[str, Any]:
        with self._transaction() as connection:
            self._check_revision(connection, expected_revision)
            schedule = self._find_schedule(connection, schedule_id, identity)
            connection.execute("UPDATE schedules SET state='cancelled',claim_token=NULL,claim_until=NULL,updated_at=? WHERE schedule_id=?", (self.clock(), schedule_id))
            pending = [row[0] for row in connection.execute("SELECT task_id FROM tasks WHERE schedule_id=? AND state NOT IN ('completed','failed','cancelled','expired')", (schedule_id,)).fetchall()]
            result = self._record(connection.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone())
        if account is not None:
            for task_id in pending:
                self.cancel(task_id, identity, account)
        return result

    def bind_schedule_task(self, schedule_id: str, claim_token: str, task_id: str) -> bool:
        with self._transaction() as connection:
            schedule = connection.execute("SELECT * FROM schedules WHERE schedule_id=? AND claim_token=? AND state='active' AND claim_until>?", (schedule_id, claim_token, self.clock())).fetchone()
            if schedule is None:
                return False
            task = self._find(connection, task_id)
            if any(task[key] != schedule[key] for key in ("user_id", "scope", "persona")) or task["request_id"] != f"schedule:{schedule['run_id']}":
                raise TaskError("identity_mismatch", "schedule task ownership does not match")
            template = json.loads(schedule["template"])
            connection.execute("UPDATE tasks SET schedule_id=?,reminder=? WHERE task_id=?", (schedule_id, int(template["reminder"]), task_id))
            return True

    def schedule_claim_current(self, schedule_id: str, claim_token: str) -> bool:
        with self._transaction() as connection:
            return connection.execute("SELECT 1 FROM schedules WHERE schedule_id=? AND claim_token=? AND state='active' AND claim_until>?", (schedule_id, claim_token, self.clock())).fetchone() is not None

    def resume_schedule(self, schedule_id: str, identity: TaskIdentity) -> dict[str, Any]:
        with self._transaction() as connection:
            schedule = self._find_schedule(connection, schedule_id, identity)
            if schedule["state"] != "paused":
                raise TaskError("not_resumable", "schedule is not paused")
            next_run = self._next_occurrence(json.loads(schedule["spec"]), self.clock())
            if next_run is None:
                raise TaskError("expired", "one-time schedule has expired")
            connection.execute("UPDATE schedules SET state='active',next_run=?,claim_token=NULL,claim_until=NULL,run_id=NULL,last_error=NULL,updated_at=? WHERE schedule_id=?", (next_run, self.clock(), schedule_id))
            return self._record(connection.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone())

    def pause_schedule(self, schedule_id: str, identity: TaskIdentity, *, expected_revision: int | None = None) -> dict[str, Any]:
        with self._transaction() as connection:
            self._check_revision(connection, expected_revision)
            schedule = self._find_schedule(connection, schedule_id, identity)
            if schedule["state"] not in ("cancelled", "completed"):
                connection.execute("UPDATE schedules SET state='paused',claim_token=NULL,claim_until=NULL,last_error=?,updated_at=? WHERE schedule_id=?", (_json("manual_pause"), self.clock(), schedule_id))
            return self._record(connection.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone())

    def claim_due(self, *, lease_seconds: float = 30) -> dict[str, Any] | None:
        with self._transaction() as connection:
            now = self.clock()
            schedule = connection.execute("SELECT * FROM schedules WHERE state='active' AND next_run<=? AND (claim_until IS NULL OR claim_until<=?) ORDER BY next_run,schedule_id LIMIT 1", (now, now)).fetchone()
            if schedule is None:
                return None
            run_id = schedule["run_id"] or uuid.uuid5(uuid.NAMESPACE_URL, f"catty:{schedule['schedule_id']}:{schedule['next_run']:.6f}").hex
            connection.execute("UPDATE schedules SET claim_token=?,claim_until=?,run_id=?,updated_at=? WHERE schedule_id=?", (uuid.uuid4().hex, now + lease_seconds, run_id, now, schedule["schedule_id"]))
            return self._record(connection.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule["schedule_id"],)).fetchone())

    def finish_schedule_claim(self, schedule_id: str, claim_token: str, *, task_id: str | None = None, error: str | None = None) -> bool:
        with self._transaction() as connection:
            schedule = connection.execute("SELECT * FROM schedules WHERE schedule_id=? AND claim_token=? AND state='active'", (schedule_id, claim_token)).fetchone()
            if schedule is None:
                return False
            next_run = self._next_occurrence(json.loads(schedule["spec"]), self.clock()) if error is None else schedule["next_run"]
            state = "paused" if error is not None else "active" if next_run is not None else "completed"
            connection.execute("UPDATE schedules SET state=?,next_run=?,last_task_id=?,last_error=?,claim_token=NULL,claim_until=NULL,run_id=NULL,updated_at=? WHERE schedule_id=?", (state, next_run, task_id, _json(error) if error is not None else None, self.clock(), schedule_id))
            return True