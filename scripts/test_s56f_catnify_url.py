"""S5.6f 验证: catnify 现在走本地 ollama (127.0.0.1) 不是 deepseek.com.

直接调 catnify_rewrite() 函数, 不走 nonebot 完整链路.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from catty_config_loader import load_config_to_env  # noqa: E402

load_config_to_env()


def _load_module(modname: str, relpath: str):
    full = ROOT / "src" / "catty_qq_ai" / relpath
    spec = importlib.util.spec_from_file_location(modname, str(full))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


_config_mod = _load_module("_test_config_only", "config.py")
Config = _config_mod.Config

_catnify_mod = _load_module("_test_catnify", "cpu_engine/catnify_rewrite.py")
catnify_rewrite = _catnify_mod.catnify_rewrite


async def main() -> int:
    config = Config()
    print("=== Config dump ===")
    print(f"  catnify_base_url   = {config.catty_cpu_engine_l4_catnify_base_url!r}")
    print(f"  catnify_api_key    = {config.catty_cpu_engine_l4_catnify_api_key!r}")
    print(f"  catnify_model      = {config.catty_cpu_engine_l4_catnify_model!r}")
    print(f"  ai_fallback url    = {config.catty_ai_fallback_base_url!r}")
    print()
    print("=== T1: 改写日常 candidate ===")
    t0 = time.monotonic()
    r = await catnify_rewrite(
        config=config,
        candidate="早上好~",
        user_text="早安笨猫",
        user_addr="主人",
        scope_type="private",
    )
    ms = (time.monotonic() - t0) * 1000.0
    print(f"  latency_ms = {ms:.0f}")
    if r is None:
        print("  result = None (LLM 调用失败/超时)")
    else:
        print(f"  result.deepseek_reason = {r.deepseek_reason!r}")
        print(f"  result.text            = {r.text!r}")
        print(f"  result.latency_ms      = {r.latency_ms:.0f}")
    return 0 if r is not None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
