#!/usr/bin/env python3
"""Compare offline request preparation against a local Git revision.

Runs only the real preparation function plus synthetic fixtures. It stops before
session diagnostics or HTTP, and never initializes NoneBot or calls a provider.
The test harness supplies isolated relative imports and restores them afterward.
This measures Python preparation cost, not model speed or remote cache hit rate.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import json
import logging
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from test_input_context import RequestInputIsolationTests  # noqa: E402


class _Prepared(Exception):
    def __init__(self, payload: dict) -> None:
        self.payload = payload


def _stop_at_prepared(payload: dict, **kwargs) -> None:
    raise _Prepared(payload)


def _baseline_function(source: str, namespace: dict):
    node = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_post_chat_completion_raw"
    )
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    tree = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    exec(compile(tree, "baseline_openai_client.py", "exec"), namespace)
    return namespace["_post_chat_completion_raw"]


async def _measure(case, baseline_source: str, iterations: int) -> list[dict]:
    baseline_namespace = dict(case.openai_namespace)
    before = _baseline_function(baseline_source, baseline_namespace)
    baseline_namespace["_prepare_session_context_payload"] = _stop_at_prepared
    case.openai_namespace["_prepare_session_context_payload"] = _stop_at_prepared
    after = case.openai_namespace["_post_chat_completion_raw"]
    seed_messages = [{"role": "system", "content": "Neutral offline benchmark instructions."}]
    seed_messages.extend(
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": f"Neutral historical message {index:04d}. " + "local detail " * 20}
        for index in range(1000)
    )
    seed_messages.append({"role": "user", "content": "Latest neutral question"})
    seed_tools = [
        {"type": "function", "function": {
            "name": f"neutral_tool_{index:02d}",
            "parameters": {"type": "object", "properties": {"label": {"type": "string"}}},
        }, "cache_control": {"type": "ephemeral"}}
        for index in range(21)
    ]

    async def sample(function, enabled):
        # Fresh fixture copying is outside the timed section for both versions.
        messages, tools = copy.deepcopy((seed_messages, seed_tools))
        start = time.perf_counter()
        try:
            await function(
                base_url="https://example.invalid/v1", api_key="offline-test-key",
                model="deepseek-offline", messages=messages, tools=tools,
                max_tokens=17, timeout=1, proxy="", temperature=None,
                extra_headers={}, extra_body={}, enable_cache=enabled,
            )
        except _Prepared as prepared:
            elapsed = (time.perf_counter() - start) * 1000
            payload = json.dumps(prepared.payload, ensure_ascii=False, separators=(",", ":"))
            return elapsed, payload, messages == seed_messages and tools == seed_tools
        raise AssertionError("Request unexpectedly passed the offline preparation stop")

    results = []
    for enabled in (False, True):
        times = {"before": [], "after": []}
        wire_equal = True
        isolated = True
        for iteration in range(iterations + 5):
            previous = await sample(before, enabled)
            current = await sample(after, enabled)
            wire_equal &= previous[1] == current[1]
            isolated &= current[2]
            if iteration >= 5:
                times["before"].append(previous[0])
                times["after"].append(current[0])
        result = {
            "enable_cache": enabled,
            "messages": len(seed_messages), "tools": len(seed_tools),
            "iterations": iterations, "prepared_json_equal": wire_equal,
            "after_inputs_unchanged": isolated,
        }
        for label, samples in times.items():
            result[label] = {
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3),
            }
        results.append(result)
        if not wire_equal or not isolated:
            raise AssertionError(f"Request contract regression: {result}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="607cf48", help="Local Git revision to compare (no fetch)")
    parser.add_argument("--iterations", type=int, default=80)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    baseline_source = subprocess.check_output(
        ["git", "show", f"{args.baseline_ref}:src/catty_qq_ai/openai_client.py"],
        cwd=REPO, text=True,
    )
    logging.disable(logging.CRITICAL)
    case = RequestInputIsolationTests()
    case.setUp()
    try:
        results = asyncio.run(_measure(case, baseline_source, args.iterations))
    finally:
        case.doCleanups()
    print(json.dumps({
        "scope": "offline request preparation only; no provider or QQ calls",
        "baseline_ref": args.baseline_ref, "results": results,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
