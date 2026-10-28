from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path


_APPLICATION_ID = 0x504C4447
_SCHEMA_VERSION = 1
_TTL_SECONDS = 30 * 24 * 60 * 60
_TEXT_LIMIT = 200
_EVIDENCE_LIMIT = 5
_ACTIVE = ("hypothesis", "confirmed")
_TIME_WORDS = r"(?:今天|明天|后天|这周|下周|这个月|下个月|今年|最近|以后|接下来|近期)"
_INTENT = re.compile(
    rf"^(?:{_TIME_WORDS}\s*)?(?:我|俺|本人)(?:{_TIME_WORDS}|真的|以后|现在|一直)?"
    r"(?:想要|想|打算|计划|准备|希望|期望|决定|立志|愿意|的目标是|的目标为)\s*(?P<goal>.+)$"
)
_PROMISE = re.compile(
    r"^(?:我|俺|本人)(?:答应|承诺|保证|会(?=帮你|替你|给你|为你|去|把|查|做|处理|完成|整理|发送|联系|修复|安排)|来)(?P<goal>.+)$"
)
_EN_INTENT = re.compile(
    r"^i\s+(?:want to|would like to|plan to|intend to|hope to|am going to|decided to)\s+(?P<goal>.+)$",
    re.IGNORECASE,
)
_EN_PROMISE = re.compile(r"^i(?:'ll| will| promise to)\s+(?P<goal>.+)$", re.IGNORECASE)
_REJECTION = re.compile(
    r"(?:不想了|不想再|不想继续|不想跟进|不想.{1,20}(?:了|啦)|不要了|不要再|别再|别提了|不需要了|不用了|算了|取消|放弃|拒绝|别跟进|停止跟进)"
    r"|^(?:不|不要|不想|我不想|不需要|不用|拒绝|no|no thanks|stop|cancel)$",
    re.IGNORECASE,
)
_NEGATED_REJECTION = re.compile(r"(?:不|没|没有|并非|不是|无需|从未)(?:想|要|会|打算|计划|应该|再)?(?:拒绝|放弃|取消|停止)")
_NOT_DONE = re.compile(r"(?:还没|尚未|并未|没有|没|未|不曾|不能|不算|not|haven't|hasn't)\s*(?:.*?)(?:完成|做完|搞定|办好|done|finished|completed)", re.IGNORECASE)
_DONE = re.compile(r"(?:完成了|完成啦|已经完成|已完成|做完了|做完啦|搞定了|办好了|全部完成|i(?:'ve| have) finished|i(?:'m| am) done|^完成$|^做完$|^搞定$|^done$|^finished$|^completed$)", re.IGNORECASE)
_AFFIRMATIVE = re.compile(r"^(?:是|是的|对|对的|没错|好|好的|好呀|可以|愿意|继续|没问题|yes|yep|yeah|sure|okay|ok)$", re.IGNORECASE)


def _identifier(value, name, *, required=True):
    text = "" if value is None else str(value).strip()
    if (required and not text) or len(text) > 160 or any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError(f"invalid {name}")
    return text


def _timestamp(now):
    value = time.time() if now is None else float(now)
    if not math.isfinite(value):
        raise ValueError("invalid now")
    return value


def _clean(value):
    text = unicodedata.normalize("NFKC", str(value or "")[:4096])
    return "".join("\n" if char in "\r\n" else " " if unicodedata.category(char).startswith("C") else char for char in text)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _intent(text, owner):
    cleaned = _clean(text)
    for item in re.finditer(r"[^,，。!?！？;；\n]+(?:[,，。!?！？;；\n]|$)", cleaned):
        part = item.group(0).strip()
        if part.endswith(("?", "？")):
            continue
        part = part.rstrip(",，。!！;；\n ").strip()
        if not part or part.endswith(("吗", "么")) or re.fullmatch(r"(?:我|俺|本人)想(?:你|他|她|它)(?:了|啦|喵|呀)?", part):
            continue
        match = _INTENT.fullmatch(part) or _EN_INTENT.fullmatch(part)
        if match is None and owner == "self":
            match = _PROMISE.fullmatch(part) or _EN_PROMISE.fullmatch(part)
        if match is None:
            continue
        key = re.sub(r"[\W_]+", "", match.group("goal").casefold())
        if not key or key in {"啊", "了", "呢", "着", "的", "吧"}:
            continue
        return part[:_TEXT_LIMIT], _digest(key), _digest(part)
    return None


def _feedback_kind(text):
    value = " ".join(_clean(text).split()).strip(" .。!！~～")
    if not value:
        return None
    if _NEGATED_REJECTION.search(value):
        return "neutral"
    if _REJECTION.search(value):
        return "rejected"
    if _NOT_DONE.search(value):
        return "neutral"
    if _DONE.search(value):
        return "done"
    if _AFFIRMATIVE.fullmatch(value):
        return "affirmative"
    return "neutral"


class PurposeLedger:
    def __init__(self, db_path):
        self._path = str(db_path) if str(db_path) == ":memory:" else str(Path(db_path).expanduser())
        self._lock = threading.RLock()
        self._memory = None
        existed = self._path != ":memory:" and Path(self._path).exists()
        if self._path == ":memory:":
            self._memory = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            if existed and Path(self._path).stat().st_size == 0:
                raise sqlite3.DatabaseError("existing purpose ledger database is empty")
            check = db.execute("PRAGMA quick_check(1)").fetchone()[0]
            if check != "ok":
                raise sqlite3.DatabaseError(f"purpose ledger integrity check failed: {check}")
            application_id = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if application_id == 0 and version == 0 and not tables:
                self._create_schema(db)
            elif application_id != _APPLICATION_ID or version != _SCHEMA_VERSION:
                raise sqlite3.DatabaseError("not a supported purpose ledger database")
            self._check_schema(db)
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.DatabaseError("purpose ledger has invalid references")

    @contextmanager
    def _transaction(self):
        with self._lock:
            db = self._memory if self._memory is not None else sqlite3.connect(self._path, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise
            finally:
                if self._memory is None:
                    db.close()

    @staticmethod
    def _create_schema(db):
        db.execute("""CREATE TABLE purpose_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL, persona TEXT NOT NULL,
            owner TEXT NOT NULL CHECK(owner IN ('self','user','member')),
            owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
            text TEXT NOT NULL, goal_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('hypothesis','confirmed','rejected','done','expired')),
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
            delivery_count INTEGER NOT NULL DEFAULT 0, last_delivery_at REAL,
            UNIQUE(scope, persona, owner, owner_id, goal_key)
        )""")
        db.execute("""CREATE TABLE purpose_observations (
            id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES purpose_goals(id) ON DELETE CASCADE,
            scope TEXT NOT NULL, persona TEXT NOT NULL, source_id TEXT NOT NULL,
            owner TEXT NOT NULL, owner_id TEXT NOT NULL, text TEXT NOT NULL,
            text_hash TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(scope, persona, source_id)
        )""")
        db.execute("""CREATE TABLE purpose_deliveries (
            id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES purpose_goals(id) ON DELETE CASCADE,
            scope TEXT NOT NULL, persona TEXT NOT NULL, delivery_id TEXT NOT NULL,
            created_at REAL NOT NULL, UNIQUE(scope, persona, delivery_id)
        )""")
        db.execute("""CREATE TABLE purpose_feedback (
            id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES purpose_goals(id) ON DELETE CASCADE,
            scope TEXT NOT NULL, persona TEXT NOT NULL, source_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, text TEXT NOT NULL, text_hash TEXT NOT NULL,
            kind TEXT NOT NULL, result_status TEXT NOT NULL, created_at REAL NOT NULL,
            UNIQUE(scope, persona, source_id)
        )""")
        db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
        db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    @staticmethod
    def _check_schema(db):
        expected = {
            "purpose_goals": {"id", "scope", "persona", "owner", "owner_id", "source_id", "text", "goal_key", "status", "confidence", "created_at", "updated_at", "expires_at", "delivery_count", "last_delivery_at"},
            "purpose_observations": {"id", "goal_id", "scope", "persona", "source_id", "owner", "owner_id", "text", "text_hash", "created_at"},
            "purpose_deliveries": {"id", "goal_id", "scope", "persona", "delivery_id", "created_at"},
            "purpose_feedback": {"id", "goal_id", "scope", "persona", "source_id", "owner_id", "text", "text_hash", "kind", "result_status", "created_at"},
        }
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != set(expected):
            raise sqlite3.DatabaseError("invalid purpose ledger tables")
        for table, columns in expected.items():
            actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if actual != columns:
                raise sqlite3.DatabaseError(f"invalid purpose ledger schema: {table}")

    @staticmethod
    def _expire(db, scope, persona, now):
        db.execute(
            "UPDATE purpose_goals SET status='expired', updated_at=MAX(updated_at, expires_at) "
            "WHERE scope=? AND persona=? AND status IN ('hypothesis','confirmed') AND expires_at<=?",
            (scope, persona, now),
        )

    def observe(self, scope, persona, source_id, text, *, owner, owner_id='', now=None):
        scope = _identifier(scope, "scope")
        persona = _identifier(persona, "persona")
        source_id = _identifier(source_id, "source_id")
        owner = _identifier(owner, "owner")
        owner_id = _identifier(owner_id, "owner_id", required=False)
        if owner not in {"self", "user", "member"}:
            raise ValueError("owner must be self, user, or member")
        timestamp = _timestamp(now)
        found = _intent(text, owner)
        if found is None:
            return None
        evidence, goal_key, text_hash = found
        if not owner_id:
            goal_key = _digest(goal_key + "\x00" + source_id)
        with self._transaction() as db:
            self._expire(db, scope, persona, timestamp)
            existing = db.execute(
                "SELECT goal_id, owner, owner_id, text_hash FROM purpose_observations WHERE scope=? AND persona=? AND source_id=?",
                (scope, persona, source_id),
            ).fetchone()
            if existing is not None:
                if (existing["owner"], existing["owner_id"], existing["text_hash"]) == (owner, owner_id, text_hash):
                    return existing["goal_id"]
                return None
            if db.execute("SELECT 1 FROM purpose_feedback WHERE scope=? AND persona=? AND source_id=?", (scope, persona, source_id)).fetchone():
                return None
            goal = db.execute(
                "SELECT id, status, created_at FROM purpose_goals WHERE scope=? AND persona=? AND owner=? AND owner_id=? AND goal_key=?",
                (scope, persona, owner, owner_id, goal_key),
            ).fetchone()
            if goal is not None:
                if goal["status"] not in _ACTIVE or timestamp < goal["created_at"]:
                    return None
                goal_id = goal["id"]
                db.execute(
                    "UPDATE purpose_goals SET confidence=MIN(1.0, confidence+0.1), updated_at=MAX(updated_at, ?), "
                    "expires_at=MAX(expires_at, ?) WHERE id=?",
                    (timestamp, timestamp + _TTL_SECONDS, goal_id),
                )
            else:
                cursor = db.execute(
                    "INSERT INTO purpose_goals(scope, persona, owner, owner_id, source_id, text, goal_key, status, confidence, created_at, updated_at, expires_at) "
                    "VALUES(?,?,?,?,?,?,?,'hypothesis',0.5,?,?,?)",
                    (scope, persona, owner, owner_id, source_id, evidence, goal_key, timestamp, timestamp, timestamp + _TTL_SECONDS),
                )
                goal_id = cursor.lastrowid
            db.execute(
                "INSERT INTO purpose_observations(goal_id, scope, persona, source_id, owner, owner_id, text, text_hash, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (goal_id, scope, persona, source_id, owner, owner_id, evidence, text_hash, timestamp),
            )
            return goal_id

    def candidates(self, scope, persona, *, now=None, limit=5):
        scope = _identifier(scope, "scope")
        persona = _identifier(persona, "persona")
        timestamp = _timestamp(now)
        maximum = min(max(int(limit), 0), 100)
        with self._transaction() as db:
            self._expire(db, scope, persona, timestamp)
            rows = db.execute(
                "SELECT id, scope, persona, owner, owner_id, source_id, text, status, confidence, created_at, updated_at, expires_at, delivery_count, last_delivery_at "
                "FROM purpose_goals WHERE scope=? AND persona=? AND status IN ('hypothesis','confirmed') "
                "ORDER BY confidence DESC, delivery_count ASC, updated_at DESC, id ASC LIMIT ?",
                (scope, persona, maximum),
            ).fetchall()
            result = []
            for row in rows:
                entry = dict(row)
                delivery = db.execute(
                    "SELECT delivery_id FROM purpose_deliveries WHERE goal_id=? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1", (row["id"],),
                ).fetchone()
                entry["last_delivery_id"] = delivery["delivery_id"] if delivery is not None else None
                observations = db.execute(
                    "SELECT source_id, owner, owner_id, text, created_at FROM purpose_observations WHERE goal_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (row["id"], _EVIDENCE_LIMIT),
                ).fetchall()
                feedback = db.execute(
                    "SELECT source_id, owner_id, text, kind, created_at FROM purpose_feedback WHERE goal_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (row["id"], _EVIDENCE_LIMIT),
                ).fetchall()
                evidence = [dict(item, kind="observation", trust="untrusted_data") for item in observations]
                evidence.extend(dict(item, owner="user" if row["owner"] == "self" else row["owner"], trust="untrusted_data") for item in feedback)
                evidence.sort(key=lambda item: item["created_at"], reverse=True)
                entry["evidence"] = evidence[:_EVIDENCE_LIMIT]
                entry["trust"] = "untrusted_data"
                result.append(entry)
            return result

    def note_delivery(self, scope, persona, goal_id, *, delivery_id, now=None):
        scope = _identifier(scope, "scope")
        persona = _identifier(persona, "persona")
        delivery_id = _identifier(delivery_id, "delivery_id")
        timestamp = _timestamp(now)
        try:
            goal_id = int(goal_id)
        except (TypeError, ValueError, OverflowError):
            return False
        with self._transaction() as db:
            self._expire(db, scope, persona, timestamp)
            prior = db.execute(
                "SELECT goal_id FROM purpose_deliveries WHERE scope=? AND persona=? AND delivery_id=?",
                (scope, persona, delivery_id),
            ).fetchone()
            if prior is not None:
                return prior["goal_id"] == goal_id
            goal = db.execute(
                "SELECT status, created_at FROM purpose_goals WHERE id=? AND scope=? AND persona=?",
                (goal_id, scope, persona),
            ).fetchone()
            if goal is None or goal["status"] not in _ACTIVE or timestamp < goal["created_at"]:
                return False
            db.execute(
                "INSERT INTO purpose_deliveries(goal_id, scope, persona, delivery_id, created_at) VALUES(?,?,?,?,?)",
                (goal_id, scope, persona, delivery_id, timestamp),
            )
            db.execute(
                "UPDATE purpose_goals SET delivery_count=delivery_count+1, last_delivery_at=MAX(COALESCE(last_delivery_at, ?), ?) WHERE id=?",
                (timestamp, timestamp, goal_id),
            )
            return True

    def feedback(self, scope, persona, goal_id, *, source_id, owner_id, text, now=None):
        scope = _identifier(scope, "scope")
        persona = _identifier(persona, "persona")
        source_id = _identifier(source_id, "source_id")
        owner_id = _identifier(owner_id, "owner_id", required=False)
        timestamp = _timestamp(now)
        if not owner_id:
            return None
        try:
            goal_id = int(goal_id)
        except (TypeError, ValueError, OverflowError):
            return None
        cleaned = " ".join(_clean(text).split())
        evidence = cleaned[:_TEXT_LIMIT]
        kind = _feedback_kind(evidence)
        if kind is None:
            return None
        text_hash = _digest(cleaned)
        with self._transaction() as db:
            self._expire(db, scope, persona, timestamp)
            prior = db.execute(
                "SELECT goal_id, owner_id, text_hash, kind, result_status FROM purpose_feedback WHERE scope=? AND persona=? AND source_id=?",
                (scope, persona, source_id),
            ).fetchone()
            if prior is not None:
                if (prior["goal_id"], prior["owner_id"], prior["text_hash"]) != (goal_id, owner_id, text_hash):
                    return None
                return {"goal_id": goal_id, "kind": prior["kind"], "status": prior["result_status"], "applied": False}
            if db.execute("SELECT 1 FROM purpose_observations WHERE scope=? AND persona=? AND source_id=?", (scope, persona, source_id)).fetchone():
                return None
            goal = db.execute(
                "SELECT owner_id, status, created_at FROM purpose_goals WHERE id=? AND scope=? AND persona=?",
                (goal_id, scope, persona),
            ).fetchone()
            if goal is None or not goal["owner_id"] or goal["owner_id"] != owner_id or goal["status"] not in _ACTIVE or timestamp < goal["created_at"]:
                return None
            status = {"affirmative": "confirmed", "rejected": "rejected", "done": "done"}.get(kind, goal["status"])
            db.execute(
                "INSERT INTO purpose_feedback(goal_id, scope, persona, source_id, owner_id, text, text_hash, kind, result_status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (goal_id, scope, persona, source_id, owner_id, evidence, text_hash, kind, status, timestamp),
            )
            if kind == "affirmative":
                db.execute(
                    "UPDATE purpose_goals SET status=?, confidence=MIN(1.0, confidence+0.2), updated_at=MAX(updated_at, ?), "
                    "expires_at=MAX(expires_at, ?) WHERE id=?",
                    (status, timestamp, timestamp + _TTL_SECONDS, goal_id),
                )
            elif kind in {"rejected", "done"}:
                db.execute("UPDATE purpose_goals SET status=?, updated_at=MAX(updated_at, ?) WHERE id=?", (status, timestamp, goal_id))
            return {"goal_id": goal_id, "kind": kind, "status": status, "applied": True}

    def clear_scope(self, scope):
        scope = _identifier(scope, "scope")
        with self._transaction() as db:
            return db.execute("DELETE FROM purpose_goals WHERE scope=?", (scope,)).rowcount
