"""Run the reviewed local regression modules without importing live benchmarks."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "test_tool_history_recovery", "test_reply_deadline", "test_deferred_image_delivery",
    "test_reply_delivery_state", "test_reply_delivery_integration",
    "test_file_formats", "test_tool_audit_fixes", "test_tool_message_scan",
    "test_tool_contracts", "test_local_tool_contracts", "test_file_delivery",
    "test_tool_dispatcher", "test_tool_telemetry", "test_code_sandbox",
    "test_sandbox_artifact_isolation", "test_prompt_audit_fixes",
    "test_live_benchmark_import", "test_config", "test_config_loader",
    "test_training_dashboard", "test_prompt_cache_deepseek", "test_prompt_compressor",
    "test_cache_cpu", "test_input_context", "test_memory_isolation",
    "test_personas", "test_persona_prompts", "test_reply_split_reminder",
    "test_output_control_markers", "test_persona_memory_isolation",
    "test_reply_message_composition", "test_vision_context_images",
    "test_fadianji_nsfw_split_bubbles", "test_fadianji_nsfw_split", "test_nsfw_freeform_state",
    "test_quick_ack", "test_auto_web_fetch",
    "test_openai_client",
    "test_fadianji_harness", "test_harness_interface", "test_harness_v2",
    "test_harness_prompt", "test_harness_cli_v2", "test_harness_config_aliases",
    "test_fdj_corpus_store", "test_fdj_scene_retrieval", "test_fdj_style_stats", "test_jiji_flavor_bench",
    "test_cognitive_runtime_integration", "test_qzone_harness_feed",
    "test_persona_store", "test_custom_personas", "test_persona_admin",
    "test_persona_tool", "test_request_intent", "test_persona_runtime",
    "test_hot_reload", "test_reply_queue_retention",
    "test_skill_store", "test_skill_runtime", "test_skill_tools", "test_skill_disclosure",
    "test_cognitive_admin", "test_dashboard_state", "test_sim_chat", "test_harness_audit_regressions",
    "test_security_image_urls", "test_security_dashboard_auth", "test_security_sandbox_paths", "test_security_persona_concurrency",
    "test_fdj_proactive", "test_fdj_proactive_config_prompt", "test_fdj_proactive_integration", "test_reply_admission",
    "test_shared_persona_capabilities", "test_shared_persona_evidence",
    "test_shared_persona_proactive", "test_shared_persona_style",
    "test_shared_persona_integration", "test_shared_persona_nsfw_imagegen",
    "test_owner_forward", "test_fadianji_style_critic", "test_imagegen_force_tool",
)

_RUNNER = r'''
import io, json, sys, unittest
sys.path.insert(0, sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[2])
def cases(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from cases(case)
        else:
            yield case
# Use synthetic secrets only; do not attempt to open the real credential file.
suite = unittest.TestSuite(case for case in cases(suite)
    if not case.id().endswith('.test_project_config_is_not_readable'))
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
if not result.wasSuccessful():
    print(stream.getvalue())
print('CATTY_TEST_RESULT=' + json.dumps(dict(
    tests=result.testsRun, failures=len(result.failures),
    errors=len(result.errors), skipped=len(result.skipped))))
sys.exit(not result.wasSuccessful())
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", action="append", choices=MODULES,
                        help="Run selected reviewed modules; repeat to select more than one.")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="Per-module timeout in seconds (default: 180).")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    totals = dict(tests=0, failures=0, errors=0, skipped=0)
    failed_modules: list[str] = []
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    for name in args.module or MODULES:
        command = [sys.executable, "-B", "-X", "utf8", "-c", _RUNNER,
                   str(ROOT / "tests"), name]
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=args.timeout)
        except subprocess.TimeoutExpired:
            failed_modules.append(name)
            totals["errors"] += 1
            print(f"FAIL {name}: exceeded {args.timeout:g}s")
            continue
        metrics = None
        for line in result.stdout.splitlines():
            if line.startswith("CATTY_TEST_RESULT="):
                metrics = json.loads(line.split("=", 1)[1])
        if metrics is not None:
            for key in totals:
                totals[key] += metrics[key]
        else:
            totals["errors"] += 1
        if result.returncode or metrics is None:
            failed_modules.append(name)
            print(f"FAIL {name} (exit={result.returncode})")
            print(result.stdout[-16000:])
            print(result.stderr[-8000:])
        else:
            print(f"PASS {name}: {metrics['tests']} tests, {metrics['skipped']} skipped")
    print("SUMMARY=" + json.dumps({**totals, "failed_modules": failed_modules}))
    return int(bool(failed_modules))


if __name__ == "__main__":
    raise SystemExit(main())