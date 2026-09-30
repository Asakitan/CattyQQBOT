#!/usr/bin/env python3
"""Reproducible, offline comparison against a local Git baseline.

Example:
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python scripts/benchmark_cache_cpu.py

Only the two pure implementation modules are loaded. The bot entry point is never
imported; session files are generated in a temporary directory. No model, account,
network API, or actual user history is used. NumPy is the only required optional
dependency. The semantic full-match measurement uses a precomputed query vector:
it covers matrix multiplication + winner selection + result construction, NOT
embedding inference, the full CPU routing chain, network calls or bot latency.
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import types
from pathlib import Path

# Respect explicit operator settings; use one BLAS worker for a stable default.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

try:
    import numpy as np
except ImportError as exc:
    raise SystemExit("This offline benchmark needs NumPy; use an environment with the nlu dependencies.") from exc

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str, baseline_ref: str | None):
    if baseline_ref is None:
        source = (ROOT / relative).read_text(encoding="utf-8")
    else:
        source = subprocess.run(
            ["git", "show", f"{baseline_ref}:{relative}"], cwd=ROOT,
            check=True, capture_output=True, text=True,
        ).stdout
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / relative)
    logger = logging.getLogger(name)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    stubs = {}
    for dependency in ("nonebot", "loguru"):
        stub = types.ModuleType(dependency)
        stub.logger = logger
        stubs[dependency] = stub
    missing = object()
    previous = {key: sys.modules.get(key, missing) for key in stubs}
    sys.modules[name] = module
    sys.modules.update(stubs)
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    finally:
        for key, value in previous.items():
            if value is missing:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
    return module


def measure(call, iterations: int) -> dict:
    call()  # Warm filesystem/NumPy code paths before timing.
    wall, cpu = [], []
    for _ in range(iterations):
        cpu_start, wall_start = time.process_time(), time.perf_counter()
        call()
        wall.append((time.perf_counter() - wall_start) * 1000)
        cpu.append((time.process_time() - cpu_start) * 1000)
    return {
        "median_wall_ms": round(statistics.median(wall), 4),
        "p95_wall_ms": round(sorted(wall)[math.ceil(0.95 * len(wall)) - 1], 4),
        "median_process_cpu_ms": round(statistics.median(cpu), 4),
    }


def old_selection(router, scores):
    """The baseline's unmodified Python grouping/reduction, timed separately."""
    best_per_route = {}
    for utt_idx, score in enumerate(scores):
        route_idx = router._flat_route_idx[utt_idx]
        weighted = float(score) * router._routes[route_idx].weight
        current = best_per_route.get(route_idx)
        if current is None or weighted > current[0]:
            best_per_route[route_idx] = (weighted, utt_idx)
    if not best_per_route:
        return None
    route_idx, (score, utt_idx) = max(best_per_route.items(), key=lambda item: item[1][0])
    return route_idx, score, utt_idx


def session_benchmark(modules, args):
    results = {}
    with tempfile.TemporaryDirectory(prefix="catty_cache_benchmark_") as directory:
        directory = Path(directory)
        for i in range(args.sessions):
            payload = {
                "key": f"session:{i}",
                "messages": [
                    {"role": "user" if j % 2 == 0 else "assistant", "content": f"ordinary {i}/{j} " + "x" * args.characters}
                    for j in range(args.messages)
                ],
                "last_access": i + 10, "last_turn": i + 9,
                "history_tokens_estimate": 9999,
                "trim_epoch": 0, "trim_count": 0, "context_updated_at": i + 9,
            }
            (directory / f"{i}.json").write_text(json.dumps(payload), encoding="utf-8")
        expected_hot = min(args.sessions, args.max_sessions)
        for label, module in modules.items():
            def load():
                cache = module.SessionCache(directory, max_sessions=args.max_sessions)
                assert cache.load_from_disk() == expected_hot
                assert len(cache._metadata) == args.sessions
                return cache

            timings = measure(load, args.session_iterations)
            gc.collect()
            tracemalloc.start()
            cache = load()
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            timings.update({
                "traced_resident_MiB": round(current / 1024**2, 4),
                "traced_peak_MiB": round(peak / 1024**2, 4),
                "resident_sessions": cache.total_sessions(),
                "indexed_sessions": len(cache._metadata),
            })
            # Verify all cold data is still readable without including it in timing.
            for i in range(args.sessions):
                messages = cache.get(f"session:{i}")
                assert len(messages) == args.messages
                assert messages[0]["content"].startswith(f"ordinary {i}/0 ")
            results[label] = timings
            del cache
    return results


def semantic_benchmark(modules, args):
    n = args.routes * args.utterances_per_route
    rng = np.random.default_rng(args.seed)
    vectors = rng.standard_normal((n, args.dimensions), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    query = rng.standard_normal(args.dimensions, dtype=np.float32)
    query /= np.linalg.norm(query)
    scores = vectors @ query
    weights = rng.uniform(0.5, 1.5, args.routes)
    results, expected_winner, expected_result = {}, None, None
    for label, module in modules.items():
        routes = [
            module.SemanticRoute(
                str(i), "ordinary", [f"question {i}/{j}" for j in range(args.utterances_per_route)],
                [f"ordinary answer {i}"], float(weights[i]),
            )
            for i in range(args.routes)
        ]
        cursor = 0

        def embed(texts):
            nonlocal cursor
            chunk = vectors[cursor:cursor + len(texts)]
            cursor += len(texts)
            return chunk

        router = module.SemanticRouter(routes, embed)
        assert router.prepare()
        selection = (lambda: router._select_winner(scores)) if hasattr(router, "_select_winner") else (lambda: old_selection(router, scores))
        winner = selection()
        if expected_winner is None:
            expected_winner = winner
        assert winner == expected_winner, (label, winner, expected_winner)

        def full_match():
            result = router.match(
                "ordinary query", embed_query_fn=lambda _: query,
                candidate_threshold=-1.0, direct_threshold=0.82,
            )
            return (result.route_name, result.response, result.confidence, result.matched_utterance, result.is_direct)

        result = full_match()
        if expected_result is None:
            expected_result = result
        assert result == expected_result, (label, result, expected_result)
        results[label] = {
            "aggregation_only": measure(selection, args.iterations),
            "full_match_precomputed_query": measure(full_match, args.iterations),
            "winner_equivalence": True,
        }
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="607cf48", help="Local Git revision to compare; never fetched")
    parser.add_argument("--sessions", type=int, default=700)
    parser.add_argument("--messages", type=int, default=40)
    parser.add_argument("--characters", type=int, default=900)
    parser.add_argument("--max-sessions", type=int, default=20)
    parser.add_argument("--session-iterations", type=int, default=5)
    parser.add_argument("--routes", type=int, default=20000)
    parser.add_argument("--utterances-per-route", type=int, default=9)
    parser.add_argument("--dimensions", type=int, default=32)
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--seed", type=int, default=20260930)
    args = parser.parse_args()
    for key, value in vars(args).items():
        if isinstance(value, int) and key != "seed" and value <= 0:
            parser.error(f"--{key.replace('_', '-')} must be positive")
    sessions, semantics = {}, {}
    for label, ref in [("baseline", args.baseline_ref), ("working_tree", None)]:
        sessions[label] = load_module(f"_catty_bench_session_{label}", "src/catty_qq_ai/session_cache.py", ref)
        semantics[label] = load_module(f"_catty_bench_semantic_{label}", "src/catty_qq_ai/cpu_engine/semantic_route.py", ref)
    report = {
        "environment": {
            "python": platform.python_version(), "numpy": np.__version__,
            "platform": platform.platform(),
            "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS"),
            "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
        },
        "parameters": vars(args),
        "scope": "Offline synthetic data; semantic full_match excludes real embedding inference and bot/network latency. Memory is tracemalloc Python allocations, not process RSS.",
        "session_startup": session_benchmark(sessions, args),
        "semantic_query": semantic_benchmark(semantics, args),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
