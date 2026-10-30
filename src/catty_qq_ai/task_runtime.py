from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import suppress
from typing import Any, Callable, Mapping

from .task_contracts import TaskError, TaskIdentity, TaskPolicy, TaskResult
from .task_store import TaskStore


class TaskRuntime:
    def __init__(
        self, store: TaskStore, policy: TaskPolicy, account, executor: Callable, deliver: Callable,
        *, permission_check: Callable | None = None, poll_seconds: float = 0.25,
        lease_seconds: float = 30, delivery_timeout: float = 60,
    ):
        if poll_seconds <= 0 or lease_seconds <= poll_seconds * 2 or delivery_timeout <= 0:
            raise ValueError("invalid task runtime timing")
        self.store = store
        self.policy = policy
        self.account = account
        self.executor = executor
        self.deliver = deliver
        self.permission_check = permission_check
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.delivery_timeout = delivery_timeout
        self.worker_id = uuid.uuid4().hex
        self._loop: asyncio.Task | None = None
        self._jobs: dict[str, tuple[asyncio.Task, dict[str, Any]]] = {}
        self._wake = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        self._stopping = False
        self.last_error: str | None = None

    async def _store(self, function: Callable, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    @staticmethod
    async def _invoke(function: Callable, *args):
        if inspect.iscoroutinefunction(function):
            return await function(*args)
        result = await asyncio.to_thread(function, *args)
        return await result if inspect.isawaitable(result) else result

    async def start(self) -> None:
        async with self._lifecycle:
            if self._loop is not None and not self._loop.done():
                return
            self._stopping = False
            await self._store(self.store.recover_interrupted)
            await self._store(self.store.resume_confirmations, self.account)
            await self._store(self.store.expire_quotes, self.account)
            await self._store(self.store.reconcile_payments, self.account)
            self._loop = asyncio.create_task(self._run(), name=f"catty-task-runtime-{self.worker_id}")

    async def stop(self) -> None:
        async with self._lifecycle:
            self._stopping = True
            if self._loop is not None:
                self._loop.cancel()
                with suppress(asyncio.CancelledError):
                    await self._loop
                self._loop = None
            jobs = [job for job, _ in self._jobs.values()]
            for job in jobs:
                job.cancel()
            if jobs:
                await asyncio.gather(*jobs, return_exceptions=True)
            self._jobs.clear()

    def notify(self) -> None:
        self._wake.set()

    async def confirm(self, task_id: str, identity: TaskIdentity) -> dict[str, Any]:
        result = await self._store(self.store.confirm, task_id, identity, self.account)
        self.notify()
        return result

    async def cancel(self, task_id: str, identity: TaskIdentity) -> dict[str, Any]:
        result = await self._store(self.store.cancel, task_id, identity, self.account)
        job = self._jobs.get(task_id)
        if job is not None:
            job[0].cancel()
        self.notify()
        return result

    async def resume(self, task_id: str, identity: TaskIdentity) -> dict[str, Any]:
        result = await self._store(self.store.resume, task_id, identity)
        self.notify()
        return result

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
            self._wake.clear()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)

    async def poll_once(self) -> None:
        await self._store(self.store.recover_interrupted)
        await self._store(self.store.resume_confirmations, self.account)
        await self._store(self.store.expire_quotes, self.account)
        await self._store(self.store.reconcile_payments, self.account)
        for task_id, (job, record) in list(self._jobs.items()):
            if job.done():
                self._jobs.pop(task_id, None)
                with suppress(asyncio.CancelledError, Exception):
                    job.result()
                continue
            current = await self._store(self.store.get_task, task_id, self.store.identity_of(record))
            if current["cancel_requested"] or current["state"] in ("cancelled", "interrupted", "delivery_unknown"):
                job.cancel()
                continue
            await self._store(self.store.renew_lease, task_id, record["lease_token"], lease_seconds=self.lease_seconds)
        await self._schedule_due()
        if not self.policy.enabled:
            return
        while len(self._jobs) < self.policy.global_concurrency and not self._stopping:
            record = await self._store(self.store.claim_delivery, self.worker_id, lease_seconds=self.lease_seconds)
            delivering = record is not None
            if record is None:
                record = await self._store(self.store.claim, self.worker_id, lease_seconds=self.lease_seconds)
            if record is None:
                break
            job = asyncio.create_task(self._deliver(record) if delivering else self._execute(record), name=f"catty-task-{record['task_id']}")
            self._jobs[record["task_id"]] = (job, record)

    async def _verified(self, identity: TaskIdentity) -> TaskIdentity | None:
        if self.permission_check is None:
            return None
        result = await self._invoke(self.permission_check, identity)
        if isinstance(result, TaskIdentity):
            if (result.user_id, result.scope, result.persona) == (identity.user_id, identity.scope, identity.persona):
                return result
            return None
        return identity if result is True and not identity.is_owner else None

    async def _execute(self, record: dict[str, Any]) -> None:
        task_id = record["task_id"]
        identity = self.store.identity_of(record)
        try:
            if record.get("schedule_id") and await self._verified(identity) is None:
                raise TaskError("permission_denied", "schedule creator is no longer authorized")
            if record.get("reminder"):
                result = TaskResult(record["description"], metadata={"reminder": True})
            else:
                result = await asyncio.wait_for(
                    self._invoke(self.executor, record, identity, self.store.budget(task_id)),
                    timeout=record["timeout_seconds"],
                )
            if not isinstance(result, TaskResult):
                raise TaskError("invalid_result", "executor must return TaskResult")
            await self._store(self.store.complete, task_id, result, self.account, lease_token=record["lease_token"])
        except asyncio.CancelledError:
            await asyncio.shield(self._store(self.store.mark_interrupted, task_id, record["lease_token"]))
            raise
        except Exception as exc:
            error = {"code": exc.code if isinstance(exc, TaskError) else "timeout" if isinstance(exc, asyncio.TimeoutError) else "executor_failed", "type": type(exc).__name__}
            await self._store(self.store.complete, task_id, None, self.account, lease_token=record["lease_token"], error=error)
        finally:
            self.notify()

    async def _deliver(self, record: dict[str, Any]) -> None:
        task_id = record["task_id"]
        identity = self.store.identity_of(record)
        try:
            if record.get("schedule_id") and await self._verified(identity) is None:
                delivery = {"status": "not_delivered", "reason": "permission_denied"}
            else:
                raw = record["result"]
                result = TaskResult(raw["summary"], tuple(raw.get("artifacts", ())), tuple(raw.get("sources", ())), raw.get("metadata", {}))
                delivery = await asyncio.wait_for(self._invoke(self.deliver, record, identity, result), timeout=self.delivery_timeout)
                if not isinstance(delivery, Mapping):
                    delivery = {"status": "delivery_unknown", "reason": "invalid_delivery_receipt"}
            await self._store(self.store.delivery_confirm, task_id, delivery, self.account)
        except asyncio.CancelledError:
            await asyncio.shield(self._store(self.store.mark_interrupted, task_id, record["lease_token"]))
            raise
        except Exception as exc:
            await self._store(self.store.delivery_confirm, task_id, {"status": "delivery_unknown", "reason": type(exc).__name__}, self.account)
        finally:
            self.notify()

    async def _schedule_due(self) -> None:
        for _ in range(self.policy.global_concurrency):
            schedule = await self._store(self.store.claim_due, lease_seconds=self.lease_seconds)
            if schedule is None:
                return
            task_id = None
            try:
                if not self.policy.enabled:
                    raise TaskError("disabled", "task scheduling is disabled")
                identity = await self._verified(self.store.identity_of(schedule))
                if identity is None:
                    raise TaskError("permission_denied", "schedule creator authorization cannot be verified")
                if not await self._store(self.store.schedule_claim_current, schedule["schedule_id"], schedule["claim_token"]):
                    continue
                template = schedule["template"]
                quote = await self._store(
                    self.store.quote, identity, f"schedule:{schedule['run_id']}", template["title"],
                    template["role"], template["description"], template["tier"], template["payload"],
                )
                task_id = quote["task_id"]
                if not await self._store(self.store.bind_schedule_task, schedule["schedule_id"], schedule["claim_token"], task_id):
                    await self._store(self.store.cancel, task_id, identity, self.account)
                    continue
                if quote["points"] > 0 and not identity.is_owner:
                    available = await self._store(self.account.get_points, identity.user_id)
                    code = "insufficient_points" if available < quote["points"] else "confirmation_required"
                    raise TaskError(code, "a paid scheduled run requires explicit user confirmation")
                confirmed = await self._store(self.store.confirm, task_id, identity, self.account)
                if confirmed["state"] not in ("queued", "running", "result_ready", "delivering", "completed"):
                    raise TaskError("confirmation_required", "scheduled quote requires a fresh confirmation")
                accepted = await self._store(self.store.finish_schedule_claim, schedule["schedule_id"], schedule["claim_token"], task_id=task_id)
                if not accepted:
                    await self._store(self.store.cancel, task_id, identity, self.account)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._store(self.store.finish_schedule_claim, schedule["schedule_id"], schedule["claim_token"], task_id=task_id, error=exc.code if isinstance(exc, TaskError) else type(exc).__name__)