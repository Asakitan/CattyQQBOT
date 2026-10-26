"""Offline scheduler tests: package shell only, no NoneBot, network or real clock."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/catty_qq_ai/fdj_proactive.py"
PACKAGE = "_catty_fdj_proactive_tests"
shell = types.ModuleType(PACKAGE)
shell.__path__ = [str(MODULE_PATH.parent)]
sys.modules.setdefault(PACKAGE, shell)
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.fdj_proactive", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
BubblePolicy = module.BubblePolicy
BubbleCandidate = module.BubbleCandidate
BubbleScheduler = module.BubbleScheduler
TZ = timezone(timedelta(hours=8))
DAY = "2026-10-27"
MIDNIGHT = datetime(2026, 10, 27, tzinfo=TZ).timestamp()


class BubbleSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "scheduler"
        self.policy = BubblePolicy()
        self.scheduler = BubbleScheduler(self.root, self.policy)

    @staticmethod
    def hot(now, count=5):
        return [now - i for i in range(count)]

    def slots(self, scheduler=None, group="100", now=MIDNIGHT):
        result = (scheduler or self.scheduler).status(group, now)
        self.assertTrue(result["ok"], result)
        return result["slots"]

    def join_slot(self, scheduler=None, group="100", now=MIDNIGHT):
        return next(s for s in self.slots(scheduler, group, now)
                    if s["mode"] == "join" and not s["night"])

    def offered(self, scheduler=None, group="100", slot=None):
        scheduler = scheduler or self.scheduler
        slot = slot or self.join_slot(scheduler, group)
        now = slot["at"] + 1
        candidate = scheduler.due(group, now, human_times=self.hot(now))
        self.assertIsNotNone(candidate)
        return candidate, now

    def claimed(self, scheduler=None, group="100", slot=None):
        scheduler = scheduler or self.scheduler
        candidate, now = self.offered(scheduler, group, slot)
        attempt = scheduler.claim(candidate, now)
        self.assertIsNotNone(attempt)
        return candidate, now, attempt

    def disk(self, group="100"):
        return json.loads((self.root / f"{group}.json").read_text(encoding="utf-8"))

    def assert_no_temporaries(self):
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_default_policy_and_candidates_are_frozen(self):
        self.assertEqual((self.policy.daily_min, self.policy.daily_max), (5, 10))
        self.assertEqual((self.policy.active_window_seconds, self.policy.active_min_messages), (900, 5))
        self.assertEqual(self.policy.night_chance, 0.2)
        with self.assertRaises(FrozenInstanceError):
            self.policy.daily_min = 1
        candidate = BubbleCandidate("100", DAY, f"{DAY}:00", "join", False)
        with self.assertRaises(FrozenInstanceError):
            candidate.mode = "topic"

    def test_invalid_policy_and_timestamps_are_rejected(self):
        changes = ({"daily_min": 0}, {"daily_min": 11}, {"daily_max": 65},
                   {"daily_min": True}, {"active_min_messages": 0},
                   {"slot_grace_seconds": 0}, {"min_gap_seconds": float("nan")},
                   {"night_chance": -1}, {"night_chance": 1.1},
                   {"warm_window_seconds": 1}, {"failure_backoff_seconds": float("inf")})
        for values in changes:
            with self.subTest(values=values), self.assertRaises(ValueError):
                BubblePolicy(**values)
        for now in (-1, True, float("nan"), float("inf"), "today"):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.scheduler.status("100", now)

    def test_constructor_has_no_filesystem_io(self):
        with patch.object(module.Path, "resolve", side_effect=AssertionError("resolve")), \
                patch.object(module.Path, "mkdir", side_effect=AssertionError("mkdir")), \
                patch.object(module.Path, "open", side_effect=AssertionError("open")):
            fresh = BubbleScheduler(self.root, self.policy)
            self.assertEqual(fresh._offers, {})
        self.assertFalse(self.root.exists())

    def test_four_hundred_group_day_plans_obey_distribution(self):
        night_days = 0
        quotas = set()
        seeds = set()
        for group in range(20):
            for offset in range(20):
                day = (datetime(2026, 10, 1) + timedelta(days=offset)).date().isoformat()
                plan = module._plan(str(group), day, self.policy)
                self.assertEqual(plan, module._plan(str(group), day, self.policy))
                quota, slots = plan["quota"], plan["slots"]
                self.assertTrue(5 <= quota <= 10)
                self.assertEqual(len(slots), quota)
                self.assertLessEqual(sum(s["mode"] == "topic" for s in slots), quota // 4)
                self.assertGreaterEqual(sum(s["mode"] == "join" for s in slots), quota * 0.75)
                nights = [s for s in slots if s["night"]]
                self.assertLessEqual(len(nights), 1)
                night_days += len(nights)
                quotas.add(quota)
                seeds.add(tuple(s["at"] for s in slots))
                daytime = []
                for slot in slots:
                    local = datetime.fromtimestamp(slot["at"], TZ)
                    self.assertEqual(local.date().isoformat(), day)
                    self.assertTrue(22 <= local.hour < 24 if slot["night"] else 8 <= local.hour < 22)
                    self.assertLess(slot["at"], slot["expires_at"])
                    self.assertLessEqual(slot["expires_at"] - slot["at"], 1800)
                    if slot["night"]:
                        self.assertEqual(slot["mode"], "join")
                    else:
                        daytime.append(slot["at"])
                self.assertTrue(all(b - a >= 3600 for a, b in zip(daytime, daytime[1:])))
        self.assertEqual(quotas, set(range(5, 11)))
        self.assertEqual(len(seeds), 400)
        self.assertTrue(40 < night_days < 120, night_days)

    def test_same_group_day_plan_survives_new_instance_and_other_root(self):
        first = self.scheduler.status("100", MIDNIGHT)
        reopened = BubbleScheduler(self.root, self.policy)
        elsewhere = BubbleScheduler(self.root.parent / "other", self.policy)
        self.assertEqual(reopened.status("100", MIDNIGHT), first)
        self.assertEqual(elsewhere.status("100", MIDNIGHT), first)

    def test_groups_have_independent_plans_and_pending_state(self):
        first = self.slots(group="100")
        second = self.slots(group="200")
        self.assertNotEqual(first, second)
        self.claimed(group="100", slot=first[0])
        candidate, now = self.offered(group="200", slot=second[0])
        self.assertIsNotNone(self.scheduler.claim(candidate, now))
        self.assertEqual(len(list(self.root.glob("*.json"))), 2)

    def test_active_group_gets_join_candidate(self):
        candidate, now = self.offered()
        self.assertEqual((candidate.group_id, candidate.day, candidate.mode, candidate.night),
                         ("100", DAY, "join", False))
        self.assertEqual(self.scheduler.status("100", now)["attempted"], 0)

    def test_join_slots_never_force_cold_or_merely_warm_groups(self):
        slot = self.join_slot()
        now = slot["at"] + 1
        for human_times in ([], [now - 3600], self.hot(now, count=4)):
            self.assertIsNone(self.scheduler.due("100", now, human_times=human_times))

    def test_topic_slot_needs_recent_human_activity(self):
        slot = next(s for s in self.slots() if s["mode"] == "topic")
        now = slot["at"] + 1
        self.assertIsNone(self.scheduler.due("100", now, human_times=[]))
        self.assertIsNone(self.scheduler.due("100", now, human_times=[now - 21601]))
        candidate = self.scheduler.due("100", now, human_times=[now - 21600])
        self.assertEqual(candidate.mode, "topic")
        self.assertFalse(candidate.night)

    def test_topic_slot_joins_instead_when_group_is_active(self):
        slot = next(s for s in self.slots() if s["mode"] == "topic")
        candidate, _ = self.offered(slot=slot)
        self.assertEqual(candidate.mode, "join")

    def test_night_slot_always_requires_active_group(self):
        scheduler = BubbleScheduler(self.root, replace(self.policy, night_chance=1))
        slot = next(s for s in self.slots(scheduler) if s["night"])
        now = slot["at"] + 1
        self.assertIsNone(scheduler.due("100", now, human_times=[now - 3600]))
        candidate = scheduler.due("100", now, human_times=self.hot(now))
        self.assertEqual(candidate.mode, "join")
        self.assertTrue(candidate.night)

    def test_midnight_and_before_eight_never_emit(self):
        for hour in (0, 1, 4, 7):
            now = MIDNIGHT + hour * 3600
            self.assertIsNone(self.scheduler.due("100", now, human_times=self.hot(now)))
        self.assertIsNone(self.scheduler.due("100", MIDNIGHT + 8 * 3600 - 0.001,
                                            human_times=self.hot(MIDNIGHT + 8 * 3600 - 0.001)))
        self.assertEqual(self.scheduler.status("100", MIDNIGHT)["attempted"], 0)

    def test_future_nan_infinite_and_non_numeric_activity_is_ignored(self):
        now = self.join_slot()["at"] + 1
        invalid = [now + 1] * 20 + [float("nan"), float("inf"), True, "message", None]
        self.assertIsNone(self.scheduler.due("100", now, human_times=invalid))
        self.assertIsNotNone(self.scheduler.due("100", now, human_times=invalid + self.hot(now)))

    def test_active_threshold_and_inclusive_window_boundary(self):
        now = self.join_slot()["at"] + 1
        self.assertIsNone(self.scheduler.due("100", now, human_times=self.hot(now, 4)))
        edge = [now - 900] * 5
        self.assertIsNotNone(self.scheduler.due("100", now, human_times=edge))
        self.assertIsNone(self.scheduler.due("100", now + 0.01, human_times=edge))

    def test_slot_before_start_and_at_expiry_is_not_due(self):
        slot = self.slots()[0]
        for now in (slot["at"] - 0.01, slot["expires_at"]):
            self.assertIsNone(self.scheduler.due("100", now, human_times=self.hot(now)))
        self.assertEqual(self.scheduler.status("100", slot["expires_at"])["slots"][0]["state"], "expired")

    def test_late_start_discards_old_slots_without_catchup(self):
        scheduler = BubbleScheduler(self.root, replace(self.policy, night_chance=0))
        slots = module._plan("100", DAY, scheduler.policy)["slots"]
        now = slots[-1]["expires_at"] + 1
        self.assertIsNone(scheduler.due("100", now, human_times=self.hot(now)))
        status = scheduler.status("100", now)
        self.assertEqual(status["expired"], status["quota"])
        self.assertEqual(status["attempted"], 0)
        tomorrow = MIDNIGHT + 86400
        self.assertIsNone(scheduler.due("100", tomorrow, human_times=self.hot(tomorrow)))

    def test_claim_requires_due_proof_and_authentic_candidate_fields(self):
        slot = self.join_slot()
        now = slot["at"] + 1
        fake = BubbleCandidate("100", DAY, slot["slot_id"], "join", False)
        self.assertIsNone(self.scheduler.claim(fake, now))
        candidate, now = self.offered(slot=slot)
        for changed in (replace(candidate, night=True), replace(candidate, mode="topic"),
                        replace(candidate, day="2026-10-26"), replace(candidate, slot_id="other")):
            self.assertIsNone(self.scheduler.claim(changed, now))
        self.assertIsNotNone(self.scheduler.claim(candidate, now))

    def test_claim_persists_before_permission_and_repeat_is_noop(self):
        candidate, now, attempt = self.claimed()
        disk = self.disk()
        self.assertEqual(disk["attempts"][0]["attempt_id"], attempt)
        self.assertEqual(disk["attempts"][0]["outcome"], "pending")
        before = (self.root / "100.json").read_bytes()
        self.assertIsNone(self.scheduler.claim(candidate, now))
        self.assertEqual((self.root / "100.json").read_bytes(), before)
        self.assert_no_temporaries()

    def test_claim_rechecks_activity_proof_and_slot_deadline(self):
        slot = self.join_slot()
        now = slot["at"] + 1
        candidate = self.scheduler.due("100", now, human_times=[now - 900] * 5)
        self.assertIsNotNone(candidate)
        self.assertIsNone(self.scheduler.claim(candidate, now + 1))
        refreshed = self.scheduler.due("100", now + 1, human_times=self.hot(now + 1))
        self.assertIsNone(self.scheduler.claim(refreshed, slot["expires_at"]))

    def test_two_instances_racing_claim_have_exactly_one_winner(self):
        other = BubbleScheduler(self.root, self.policy)
        candidate, now = self.offered()
        other_candidate = other.due("100", now, human_times=self.hot(now))
        self.assertEqual(other_candidate, candidate)
        barrier = threading.Barrier(2)

        def compete(scheduler, offered):
            barrier.wait()
            return scheduler.claim(offered, now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(compete, self.scheduler, candidate),
                       pool.submit(compete, other, other_candidate)]
            results = [f.result() for f in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.scheduler.status("100", now)["attempted"], 1)

    def test_pending_blocks_later_slots_and_same_process_reopen(self):
        slots = self.slots()
        _, now, attempt = self.claimed(slot=slots[0])
        other = BubbleScheduler(self.root, self.policy)
        later = slots[1]["at"] + 1
        self.assertIsNone(other.due("100", later, human_times=self.hot(later)))
        self.assertEqual(other.status("100", later)["pending"], attempt)
        self.assertEqual(other.status("100", later)["unknown"], 0)
        self.assertTrue(other.finish("100", attempt, "sent", later))
        self.assertEqual(other.status("100", later)["sent"], 1)

    def test_finish_is_idempotent_and_conflicting_outcome_is_rejected(self):
        _, now, attempt = self.claimed()
        self.assertTrue(self.scheduler.finish("100", attempt, "sent", now + 1))
        before = (self.root / "100.json").read_bytes()
        self.assertTrue(self.scheduler.finish("100", attempt, "sent", now + 10))
        self.assertFalse(self.scheduler.finish("100", attempt, "failed", now + 10))
        self.assertFalse(self.scheduler.finish("100", "absent", "sent", now + 10))
        self.assertFalse(self.scheduler.finish("100", attempt, "bogus", now + 10))
        self.assertEqual((self.root / "100.json").read_bytes(), before)
        self.assertEqual(self.scheduler.status("100", now + 10)["sent"], 1)

    def test_failure_consumes_slot_and_backoff_never_counts_as_sent(self):
        scheduler = BubbleScheduler(self.root, replace(self.policy, failure_backoff_seconds=86400))
        slots = self.slots(scheduler)
        _, now, attempt = self.claimed(scheduler, slot=slots[0])
        self.assertTrue(scheduler.finish("100", attempt, "failed", now + 10))
        later = slots[1]["at"] + 1
        self.assertIsNone(scheduler.due("100", later, human_times=self.hot(later)))
        status = scheduler.status("100", later)
        self.assertEqual((status["sent"], status["failed"], status["attempted"]), (0, 1, 1))
        self.assertEqual(status["backoff_until"], now + 10 + 86400)

    def test_unknown_consumes_opportunity_without_retry(self):
        candidate, now, attempt = self.claimed()
        self.assertTrue(self.scheduler.finish("100", attempt, "unknown", now + 1))
        self.assertIsNone(self.scheduler.claim(candidate, now + 3601))
        status = self.scheduler.status("100", now + 3601)
        self.assertEqual((status["sent"], status["unknown"], status["attempted"]), (0, 1, 1))
        self.assertFalse(self.scheduler.finish("100", attempt, "sent", now + 3601))

    def test_skip_consumes_once_without_persisting_reason_or_activity(self):
        candidate, now = self.offered()
        private_reason = "PRIVATE_USER_123_AND_CHAT_BODY"
        self.assertTrue(self.scheduler.skip(candidate, now, reason=private_reason))
        self.assertTrue(self.scheduler.skip(candidate, now, reason=private_reason))
        self.assertIsNone(self.scheduler.due("100", now + 1, human_times=self.hot(now + 1)))
        self.assertIsNone(self.scheduler.claim(candidate, now + 1))
        status = self.scheduler.status("100", now + 1)
        self.assertEqual((status["skipped"], status["attempted"], status["sent"]), (1, 0, 0))
        payload = (self.root / "100.json").read_text(encoding="utf-8")
        self.assertNotIn(private_reason, payload)
        self.assertNotIn("human_times", payload)
        self.assertNotIn("user_id", payload)

    def test_skip_after_generation_consumes_even_if_activity_evidence_expired(self):
        now = self.join_slot()["at"] + 1
        candidate = self.scheduler.due("100", now, human_times=[now - 900] * 5)
        self.scheduler.due("200", now + 5, human_times=[])
        self.assertTrue(self.scheduler.skip(candidate, now + 10))
        self.assertIsNone(self.scheduler.due("100", now + 11, human_times=self.hot(now + 11)))

    def test_process_restart_recovers_pending_as_unknown_before_new_send(self):
        candidate, now, attempt = self.claimed()
        with patch.object(module, "_PROCESS_TOKEN", "f" * 32):
            reopened = BubbleScheduler(self.root, self.policy)
            status = reopened.status("100", now + 10)
            self.assertEqual((status["sent"], status["unknown"], status["attempted"]), (0, 1, 1))
            self.assertIsNone(status["pending"])
            self.assertIsNone(reopened.claim(candidate, now + 10))
            self.assertFalse(reopened.finish("100", attempt, "sent", now + 11))
        self.assertEqual(self.disk()["attempts"][0]["outcome"], "unknown")

    def test_sent_usage_and_quota_survive_restart(self):
        _, now, attempt = self.claimed()
        self.assertTrue(self.scheduler.finish("100", attempt, "sent", now + 1))
        before = self.scheduler.status("100", now + 1)
        with patch.object(module, "_PROCESS_TOKEN", "f" * 32):
            reopened = BubbleScheduler(self.root, self.policy)
            self.assertEqual(reopened.status("100", now + 1), before)

    def test_cross_day_rolling_gap_is_not_reset(self):
        policy = replace(self.policy, min_gap_seconds=86400, night_chance=1)
        scheduler = BubbleScheduler(self.root, policy)
        night = next(s for s in self.slots(scheduler) if s["night"])
        _, now, attempt = self.claimed(scheduler, slot=night)
        self.assertTrue(scheduler.finish("100", attempt, "sent", now + 10))
        tomorrow = MIDNIGHT + 86400
        first = self.slots(scheduler, now=tomorrow)[0]
        later = first["at"] + 1
        self.assertIsNone(scheduler.due("100", later, human_times=self.hot(later)))
        status = scheduler.status("100", later)
        self.assertEqual(status["last_send_at"], now + 10)
        self.assertEqual(status["last_attempt_at"], now)
        self.assertEqual((status["day"], status["sent"], status["attempted"]), ("2026-10-28", 0, 0))

    def test_pending_can_finish_after_midnight_without_spending_new_day(self):
        policy = replace(self.policy, night_chance=1)
        scheduler = BubbleScheduler(self.root, policy)
        night = next(s for s in self.slots(scheduler) if s["night"])
        _, _, attempt = self.claimed(scheduler, slot=night)
        tomorrow = MIDNIGHT + 86400
        self.assertEqual(scheduler.status("100", tomorrow)["pending"], attempt)
        self.assertTrue(scheduler.finish("100", attempt, "sent", tomorrow + 1))
        status = scheduler.status("100", tomorrow + 1)
        self.assertEqual((status["attempted"], status["sent"], status["last_send_at"]), (0, 0, tomorrow + 1))
        self.assertTrue(scheduler.finish("100", attempt, "sent", tomorrow + 2))

    def test_clock_and_date_rollback_never_reopens_used_allowance(self):
        candidate, now, attempt = self.claimed()
        self.assertFalse(self.scheduler.finish("100", attempt, "sent", now - 1))
        self.assertTrue(self.scheduler.finish("100", attempt, "sent", now + 1))
        self.assertIsNone(self.scheduler.due("100", now - 1, human_times=self.hot(now - 1)))
        tomorrow = MIDNIGHT + 86400
        self.scheduler.status("100", tomorrow)
        before = (self.root / "100.json").read_bytes()
        self.assertEqual(self.scheduler.status("100", now)["error"], "clock_rollback")
        self.assertIsNone(self.scheduler.claim(candidate, now))
        self.assertEqual((self.root / "100.json").read_bytes(), before)

    def test_fsync_failure_never_grants_permission_or_publishes_attempt(self):
        candidate, now = self.offered()
        before = (self.root / "100.json").read_bytes()
        with patch.object(module.os, "fsync", side_effect=OSError("fsync fixture")):
            self.assertIsNone(self.scheduler.claim(candidate, now))
        self.assertEqual((self.root / "100.json").read_bytes(), before)
        self.assertFalse(self.scheduler.status("100", now)["ok"])
        other = BubbleScheduler(self.root, self.policy)
        self.assertIsNone(other.due("100", now, human_times=self.hot(now)))
        self.assert_no_temporaries()

    def test_replace_failure_and_initial_write_failure_fail_closed(self):
        candidate, now = self.offered()
        before = (self.root / "100.json").read_bytes()
        with patch.object(module.os, "replace", side_effect=OSError("replace fixture")):
            self.assertIsNone(self.scheduler.claim(candidate, now))
            self.assertFalse(self.scheduler.status("fresh", now)["ok"])
        self.assertEqual((self.root / "100.json").read_bytes(), before)
        self.assertFalse((self.root / "fresh.json").exists())
        self.assertIsNone(self.scheduler.due("fresh", now, human_times=self.hot(now)))
        self.assert_no_temporaries()

    def test_finish_write_failure_keeps_pending_then_restart_marks_unknown(self):
        _, now, attempt = self.claimed()
        with patch.object(module.os, "replace", side_effect=OSError("finish fixture")):
            self.assertFalse(self.scheduler.finish("100", attempt, "sent", now + 1))
        self.assertEqual(self.disk()["attempts"][0]["outcome"], "pending")
        self.assertFalse(self.scheduler.status("100", now + 1)["ok"])
        with patch.object(module, "_PROCESS_TOKEN", "f" * 32), \
                patch.object(module, "_BLOCKED_FILES", {}), patch.object(module, "_KNOWN_FILES", set()):
            reopened = BubbleScheduler(self.root, self.policy)
            status = reopened.status("100", now + 2)
            self.assertEqual((status["sent"], status["unknown"]), (0, 1))

    def test_corrupt_truncated_deleted_and_tampered_state_never_reset(self):
        for group, raw in (("nulled", b"\0" * 100), ("truncated", b'{"version":1'),
                           ("empty", b"{}"), ("oversize", b" " * (module._MAX_BYTES + 1))):
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{group}.json"
            path.write_bytes(raw)
            self.assertFalse(self.scheduler.status(group, MIDNIGHT)["ok"])
            self.assertEqual(path.read_bytes(), raw)
        self.scheduler.status("tampered", MIDNIGHT)
        value = self.disk("tampered")
        value["plan"]["quota"] += 1
        (self.root / "tampered.json").write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.scheduler.status("tampered", MIDNIGHT)["error"], "invalid_state")
        self.scheduler.status("deleted", MIDNIGHT)
        (self.root / "deleted.json").unlink()
        self.assertEqual(self.scheduler.status("deleted", MIDNIGHT)["error"], "missing_state")
        self.assertFalse((self.root / "deleted.json").exists())

    def test_read_and_rollover_io_failure_leave_previous_state_untouched(self):
        self.scheduler.status("100", MIDNIGHT)
        before = (self.root / "100.json").read_bytes()
        with patch.object(module.Path, "open", side_effect=PermissionError("read fixture")):
            self.assertEqual(self.scheduler.status("100", MIDNIGHT)["error"], "storage_error")
        self.assertEqual((self.root / "100.json").read_bytes(), before)
        self.scheduler.status("rollover", MIDNIGHT)
        old = (self.root / "rollover.json").read_bytes()
        with patch.object(module.os, "fsync", side_effect=OSError("rollover fixture")):
            self.assertFalse(self.scheduler.status("rollover", MIDNIGHT + 86400)["ok"])
        self.assertEqual((self.root / "rollover.json").read_bytes(), old)

    def test_path_identifiers_are_validated_and_windows_aliases_canonicalized(self):
        for group in ("", "..", "../escape", "a/b", "a\\b", "C:drive", "a.json", "nul",
                      "CON", "lpt1", "trailing ", "中文", "x" * 65, "a\0b", 123):
            with self.subTest(group=group), self.assertRaises(ValueError):
                self.scheduler.status(group, MIDNIGHT)
        self.assertTrue(self.scheduler.status("Team_123-abc", MIDNIGHT)["ok"])
        self.assertTrue((self.root / "team_123-abc.json").exists())
        self.assertEqual(self.scheduler.status("Team_123-abc", MIDNIGHT),
                         self.scheduler.status("team_123-abc", MIDNIGHT))
        # A directory at the state-file path is unsafe, never replaced.
        (self.root / "directory.json").mkdir()
        self.assertFalse(self.scheduler.status("directory", MIDNIGHT)["ok"])

    def test_status_is_detached_metadata_only(self):
        value = self.scheduler.status("100", MIDNIGHT)
        self.assertEqual(set(value), {"ok", "error", "day", "quota", "sent", "unknown", "attempted",
                                     "failed", "skipped", "expired", "pending", "last_attempt_at",
                                     "last_send_at", "backoff_until", "slots"})
        self.assertEqual(set(value["slots"][0]), {"slot_id", "at", "expires_at", "mode", "night",
                                                 "state", "attempt_id"})
        value["slots"][0]["state"] = "sent"
        self.assertEqual(self.scheduler.status("100", MIDNIGHT)["slots"][0]["state"], "open")

    def test_policy_change_does_not_reroll_existing_day(self):
        initial = self.scheduler.status("100", MIDNIGHT)
        changed = BubbleScheduler(self.root, replace(self.policy, daily_min=1, daily_max=1, night_chance=0))
        self.assertEqual(changed.status("100", MIDNIGHT), initial)
        tomorrow = changed.status("100", MIDNIGHT + 86400)
        self.assertEqual(tomorrow["quota"], 1)
        self.assertFalse(tomorrow["slots"][0]["night"])

    def test_all_daily_slots_are_at_most_once_and_never_carry_over(self):
        # Shorter gap isolates quota accounting from the separate spacing tests.
        scheduler = BubbleScheduler(self.root, replace(self.policy, min_gap_seconds=1))
        slots = self.slots(scheduler)
        attempts = []
        for slot in slots:
            candidate, now, attempt = self.claimed(scheduler, slot=slot)
            self.assertTrue(scheduler.finish("100", attempt, "sent", now + 0.1))
            self.assertIsNone(scheduler.claim(candidate, now + 0.2))
            attempts.append(attempt)
        final_time = slots[-1]["at"] + 2
        status = scheduler.status("100", final_time)
        self.assertEqual(status["sent"], status["quota"])
        self.assertEqual(status["attempted"], status["quota"])
        self.assertEqual(len(set(attempts)), status["quota"])
        self.assertIsNone(scheduler.due("100", final_time, human_times=self.hot(final_time)))
        tomorrow = scheduler.status("100", MIDNIGHT + 86400)
        self.assertEqual((tomorrow["sent"], tomorrow["attempted"]), (0, 0))
        self.assertTrue(all(s["state"] == "open" for s in tomorrow["slots"]))


if __name__ == "__main__":
    unittest.main()