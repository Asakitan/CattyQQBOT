from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from typing import Any, Iterator, Mapping, Protocol

from .task_contracts import TaskError


def _token_count(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0


def normalize_usage(usage: Any) -> NormalizedUsage | None:
    if not isinstance(usage, Mapping):
        return None
    input_tokens = _token_count(usage.get("prompt_tokens"))
    output_tokens = _token_count(usage.get("completion_tokens"))
    cached = _token_count(usage.get("prompt_cache_hit_tokens"))
    missed = _token_count(usage.get("prompt_cache_miss_tokens"))
    if input_tokens is None and cached is not None and missed is not None:
        input_tokens = cached + missed
    if input_tokens is None:
        input_tokens = _token_count(usage.get("input_tokens"))
        if input_tokens is not None and any(
            key in usage for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
        ):
            cache_read = _token_count(usage.get("cache_read_input_tokens", 0))
            cache_create = _token_count(usage.get("cache_creation_input_tokens", 0))
            if cache_read is None or cache_create is None:
                return None
            input_tokens += cache_read + cache_create
            cached = cache_read
    if output_tokens is None:
        output_tokens = _token_count(usage.get("output_tokens"))
    total = _token_count(usage.get("total_tokens"))
    if total is not None:
        if input_tokens is not None and output_tokens is None and total >= input_tokens:
            output_tokens = total - input_tokens
        if output_tokens is not None and input_tokens is None and total >= output_tokens:
            input_tokens = total - output_tokens
    if input_tokens is None or output_tokens is None:
        return None
    input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    output_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
    if cached is None and isinstance(input_details, Mapping):
        cached = _token_count(input_details.get("cached_tokens"))
    reasoning = (
        _token_count(output_details.get("reasoning_tokens"))
        if isinstance(output_details, Mapping) else None
    )
    if (cached or 0) > input_tokens or (reasoning or 0) > output_tokens:
        return None
    return NormalizedUsage(
        input_tokens, output_tokens, max(total or 0, input_tokens + output_tokens),
        cached or 0, reasoning or 0,
    )


def estimate_request_tokens(payload: Mapping[str, Any]) -> int:
    """Conservative UTF-8 byte estimate, not a provider token measurement."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(text.encode("utf-8")) + 256


class TaskBudgetProtocol(Protocol):
    def reserve_request(self, input_tokens: int, max_output_tokens: int) -> str: ...

    def settle_request(
        self, request_id: str, prompt_tokens: int, completion_tokens: int, *, receipt_id: str | None = None,
    ) -> Mapping[str, Any] | bool: ...

    def snapshot(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkerAccounting:
    budget: TaskBudgetProtocol
    worker_model: str
    max_input_tokens: int
    max_output_tokens: int
    base_url: str | None = None
    api_key: str | None = None


_worker_accounting: ContextVar[WorkerAccounting | None] = ContextVar("catty_worker_accounting", default=None)


def current_worker_accounting() -> WorkerAccounting | None:
    return _worker_accounting.get()


@contextmanager
def worker_accounting(
    budget: TaskBudgetProtocol, worker_model: str, *, max_input_tokens: int = 16_000,
    max_output_tokens: int = 4096, base_url: str | None = None, api_key: str | None = None,
) -> Iterator[WorkerAccounting]:
    if not isinstance(worker_model, str) or not worker_model.startswith("deepseek-"):
        raise TaskError("worker_model_mismatch", "task worker must use the configured DeepSeek model")
    for value in (max_input_tokens, max_output_tokens):
        if type(value) is not int or value < 1:
            raise TaskError("request_limit", "worker request limits must be positive integers")
    if (base_url is None) != (api_key is None):
        raise TaskError("worker_route_mismatch", "worker route requires both endpoint and credentials")
    active = WorkerAccounting(budget, worker_model, max_input_tokens, max_output_tokens, base_url, api_key)
    token = _worker_accounting.set(active)
    try:
        yield active
    finally:
        _worker_accounting.reset(token)


def check_worker_model(model: str) -> None:
    active = current_worker_accounting()
    if active is not None and model != active.worker_model:
        raise TaskError("worker_model_mismatch", "worker route cannot change its configured model")


def check_worker_route(base_url: str, api_key: str, model: str) -> None:
    check_worker_model(model)
    active = current_worker_accounting()
    if active is not None and active.base_url is not None:
        if base_url.strip().rstrip("/") != active.base_url.strip().rstrip("/") or api_key.strip() != active.api_key:
            raise TaskError("worker_route_mismatch", "worker request cannot borrow another AI route")


def begin_worker_request(model: str, payload: Mapping[str, Any]) -> tuple[TaskBudgetProtocol, str] | None:
    active = current_worker_accounting()
    if active is None:
        return None
    check_worker_model(model)
    check_worker_model(payload.get("model", model))
    max_output = payload.get("max_completion_tokens", payload.get("max_output_tokens", payload.get("max_tokens")))
    input_tokens = estimate_request_tokens(payload)
    if type(max_output) is not int or not 1 <= max_output <= active.max_output_tokens:
        raise TaskError("request_limit", "worker output limit is missing or exceeds the policy")
    if input_tokens > active.max_input_tokens:
        raise TaskError("request_limit", "worker input estimate exceeds the policy")
    reservation = active.budget.reserve_request(input_tokens, max_output)
    return active.budget, reservation


def settle_worker_request(
    request: tuple[TaskBudgetProtocol, str] | None, response: Mapping[str, Any],
) -> None:
    if request is None:
        return
    usage = normalize_usage(response.get("usage")) if isinstance(response, Mapping) else None
    if usage is not None:
        budget, reservation = request
        budget.settle_request(
            reservation, usage.input_tokens, usage.total_tokens - usage.input_tokens, receipt_id=reservation,
        )