"""Offline synthetic harness renderer checks; no live stores, network or model calls."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import statistics
import sys
import time
import types
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load_harness(baseline: Path | None = None):
    package_name = "_harness_benchmark_baseline" if baseline else "_harness_benchmark_current"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT / "src" / "catty_qq_ai")]
    sys.modules[package_name] = package
    if baseline is None:
        return importlib.import_module(f"{package_name}.fadianji_harness")
    # Only use a source snapshot produced by the local engineering session.
    with zipfile.ZipFile(baseline) as archive:
        members = [name for name in archive.namelist()
                   if name == "fadianji_harness.py"
                   or name.replace("\\", "/").endswith("src/catty_qq_ai/fadianji_harness.py")]
        if len(members) != 1:
            raise ValueError("baseline must contain exactly one harness source")
        source = archive.read(members[0]).decode("utf-8-sig")
    module = types.ModuleType(f"{package_name}.fadianji_harness")
    module.__file__ = str(baseline) + "!/" + members[0]
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def render(module, rows, budget):
    flags = module.QueryFlags("help", True, False)
    return module._render_packet_info(flags, rows, "group:fixture", False, budget)


def fixtures(module, *, priority=False):
    if priority:
        return [module.Evidence("FACT.fixture", "group:fixture", "次要事实" * 65, rank=30),
                module.Evidence("MEMORY.fixture", "group:fixture", "重要记忆" * 12, rank=10)]
    return [module.Evidence("MEMORY.fixture", "group:fixture", f"合成条目{i}:" + "测试正文" * 50, rank=i)
            for i in range(25)]


def measure(module, iterations):
    rows = fixtures(module)
    for _ in range(10):
        render(module, rows, 12000)
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        packet = render(module, rows, 12000)
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return {"median_ms": round(statistics.median(samples), 4),
            "rows": sum(packet["rendered_counts"].values()), "chars": len(packet["text"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-zip", type=Path, help="Trusted local pre-edit source snapshot")
    parser.add_argument("--iterations", type=int, default=300)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    current = load_harness()
    rows = fixtures(current)
    checked = [0, 1, 128, 256, 512, 12000]
    for budget in checked:
        packet = render(current, rows, budget)
        assert packet["rendered_chars"] == len(packet["text"]) <= budget
        assert len(packet["rendered_ids"]) + len(packet["omitted_ids"]) == len(rows)
        assert not set(packet["rendered_ids"]) & set(packet["omitted_ids"])
        assert sum(packet["omitted_counts"].values()) == packet["omitted_count"]
    result = {"scope": "synthetic_renderer_only", "budgets_checked": checked,
              "current": measure(current, args.iterations)}
    if args.baseline_zip:
        baseline = load_harness(args.baseline_zip)
        result["baseline"] = measure(baseline, args.iterations)
        old_rows, new_rows = fixtures(baseline, priority=True), fixtures(current, priority=True)
        for budget in range(200, 1200):
            old, new = render(baseline, old_rows, budget), render(current, new_rows, budget)
            if old["rendered_counts"] == {"FACT": 1} and new["rendered_counts"] == {"MEMORY": 1}:
                result["priority_regression"] = {"budget": budget, "baseline": old["rendered_counts"], "current": new["rendered_counts"]}
                break
        assert "priority_regression" in result, "priority regression not demonstrated"
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())