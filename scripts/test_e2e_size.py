"""端到端 size 测试 — 主人 /loop C16 模式自测.

走完整 _build_messages → sweep → split_system → 量化每段 size, 不发送也不部署.
跑 group + private 各一次, 输出 sys_blocks_bytes / msg_bytes / tools_bytes 表格.

可选: 用主人提供的 endpoint 实测 live call 看 anthropic 返 token count.

运行: python scripts/test_e2e_size.py
环境:
  ANTHROPIC_BASE_URL=http://47.79.240.254:4000/api
  ANTHROPIC_AUTH_TOKEN=cr_254372b8f8074f38d8ae0c7ff0eaab222dc9faa05369ac1d071aacb279599ef8
  CATTY_LIVE_CALL=1 (打开实际 anthropic API 调用)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))


def _print_block_table(label: str, blocks: list[dict]) -> int:
    total_bytes = 0
    print(f"\n=== {label}: {len(blocks)} blocks ===")
    print(f"{'#':>3} | {'role':<10} | {'chars':>7} | {'utf8_bytes':>10} | head")
    print("-" * 100)
    for i, b in enumerate(blocks):
        role = b.get("role", "?")
        content = b.get("content", "")
        if isinstance(content, list):
            text = "\n".join(
                str(x.get("text", "") or "") for x in content
                if isinstance(x, dict) and x.get("type") == "text"
            )
        else:
            text = str(content or "")
        chars = len(text)
        bytes_ = len(text.encode("utf-8"))
        total_bytes += bytes_
        head = text.replace("\n", " ")[:80]
        print(f"{i:>3} | {role:<10} | {chars:>7} | {bytes_:>10} | {head}")
    print(f"  TOTAL: {total_bytes} utf8 bytes")
    return total_bytes


async def _run_scenario(label: str, *, text: str, user_id, group_id):
    print(f"\n{'#' * 80}\n# Scenario: {label}\n{'#' * 80}")
    from catty_qq_ai.catty_sim_chat import sim_chat

    result = await sim_chat(
        text=text, user_id=user_id, group_id=group_id,
        live=False, history_replace=True,
    )
    messages = result["messages"]
    stats = result["stats"]
    print(f"sim_chat: {result['system_blocks']} sys + {result['history_count']} history, "
          f"raw total: {stats.get('total_chars', 0)} chars, "
          f"sys: {stats.get('system_chars', 0)} chars, history: {stats.get('history_chars', 0)} chars")

    # Anthropic native 路径会 sweep + split_system, 模拟一次看真实发送 shape
    from catty_qq_ai.prompt_cache import sweep_floating_systems_into_user_content
    messages_swept = sweep_floating_systems_into_user_content(messages)

    print(f"\n--- AFTER sweep_floating_systems ({len(messages)} → {len(messages_swept)} msgs) ---")
    sys_blocks = [m for m in messages_swept if isinstance(m, dict) and m.get("role") == "system"]
    other_msgs = [m for m in messages_swept if isinstance(m, dict) and m.get("role") != "system"]
    sys_bytes = _print_block_table("SYS blocks (will go to system[] field)", sys_blocks)
    msg_bytes = _print_block_table("MSG blocks (will go to messages[] field)", other_msgs)

    # tools
    try:
        from catty_qq_ai.tools import available_tool_schemas
        from catty_qq_ai import config as catty_config
        import json
        tools = available_tool_schemas(
            catty_config,
            is_private=(group_id is None),
            user_text=text,
            has_image=False,
        )
        tools_total = sum(len(json.dumps(t, ensure_ascii=False).encode("utf-8")) for t in tools)
        print(f"\n--- TOOLS ({len(tools)} tools, total {tools_total} utf8 bytes) ---")
        for t in tools:
            name = t.get("name") or t.get("function", {}).get("name", "?")
            desc = t.get("description") or t.get("function", {}).get("description", "")
            sch = t.get("input_schema") or t.get("function", {}).get("parameters", {})
            t_bytes = len(json.dumps(t, ensure_ascii=False).encode("utf-8"))
            print(f"  {name:<28} | {t_bytes:>6} bytes | desc:{len(desc)}c schema:{len(json.dumps(sch, ensure_ascii=False))}c")
    except Exception as exc:
        print(f"tools enumeration failed: {exc}")
        tools_total = 0
        tools = []

    grand = sys_bytes + msg_bytes + tools_total
    print(f"\n=== SCENARIO TOTAL ({label}) ===")
    print(f"  sys (system[]):    {sys_bytes:>8} utf8 bytes ({sys_bytes / 1024:.1f}K)")
    print(f"  msg (messages[]):  {msg_bytes:>8} utf8 bytes ({msg_bytes / 1024:.1f}K)")
    print(f"  tools:             {tools_total:>8} utf8 bytes ({tools_total / 1024:.1f}K)")
    print(f"  GRAND TOTAL:       {grand:>8} utf8 bytes ({grand / 1024:.1f}K)")

    return {
        "label": label,
        "sys_bytes": sys_bytes,
        "msg_bytes": msg_bytes,
        "tools_bytes": tools_total,
        "grand": grand,
        "messages_swept": messages_swept,
        "tools": tools,
    }


async def _live_call(scenario_result: dict, *, model: str):
    """实际调用主人提供的 anthropic endpoint, 看 anthropic 返 input_tokens (server-side 真 token)."""
    import os as _os
    base_url = _os.environ.get("ANTHROPIC_BASE_URL", "")
    auth = _os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not base_url or not auth:
        print(f"\n[live skip] ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN not set, skipping live call")
        return
    print(f"\n--- LIVE CALL ({scenario_result['label']}) -> {base_url} model={model} ---")
    try:
        from catty_qq_ai.anthropic_native_client import post_messages_native
        # NOTE: post_messages_native 内部会再 sweep 一次, 这里我们已经 sweep, 但函数是幂等的 (没 floating sys 就不动).
        # 直接传原始 messages_swept.
        # 注意: 它需要 messages 包含一个 user msg, 我们 sim 出来的有。
        r = await post_messages_native(
            base_url=base_url,
            api_key=auth,
            model=model,
            messages=scenario_result["messages_swept"],
            max_tokens=120,
            temperature=0.6,
            timeout=120,
            tools=scenario_result["tools"] or None,
            metadata_user_id=f"qq_test_{scenario_result['label']}",
        )
        usage = r.get("usage", {}) if isinstance(r, dict) else {}
        choices = r.get("choices", []) if isinstance(r, dict) else []
        reply = ""
        if choices:
            reply = choices[0].get("message", {}).get("content", "")
        print(f"  reply: {str(reply)[:200]!r}")
        print(f"  usage: {usage}")
    except Exception as exc:
        print(f"  live call failed: {type(exc).__name__}: {exc}")


async def main():
    # Step 1: bootstrap catty (nonebot)
    print("=" * 80)
    print("Bootstrapping catty (nonebot.init + load plugin)...")
    print("=" * 80)
    try:
        from catty_config_loader import load_config_to_env
        load_config_to_env()
    except Exception as e:
        print(f"  config loader failed (probably non-fatal): {e}")

    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    try:
        nonebot.init()
        driver = nonebot.get_driver()
        try:
            driver.register_adapter(OneBotV11Adapter)
        except Exception:
            pass
        nonebot.load_plugin("catty_qq_ai")
    except Exception as e:
        print(f"  bootstrap warn: {e}")

    # Step 2: 跑场景
    scenarios = [
        ("group_normal", {"text": "诶笨猫今晚有空打卡丘吗", "user_id": 993255714, "group_id": 477970838}),
        ("private_normal", {"text": "笨猫晚饭吃啥喵", "user_id": 993255714, "group_id": None}),
    ]
    results = []
    for label, kw in scenarios:
        try:
            r = await _run_scenario(label, **kw)
            results.append(r)
        except Exception as exc:
            print(f"\n[scenario {label} failed]: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()

    # Step 3: 可选 live call
    if os.environ.get("CATTY_LIVE_CALL"):
        model = os.environ.get("CATTY_TEST_MODEL", "claude-opus-4-6")
        for r in results:
            await _live_call(r, model=model)

    # Step 4: 摘要
    print(f"\n{'=' * 80}\nSUMMARY (主人指标: 群聊 sys 8.4K cache + 2K non-cache = 10.4K total)\n{'=' * 80}")
    print(f"{'scenario':<20} | {'sys':>10} | {'msg':>10} | {'tools':>10} | {'GRAND':>10}")
    for r in results:
        print(f"{r['label']:<20} | {r['sys_bytes']:>10} | {r['msg_bytes']:>10} | {r['tools_bytes']:>10} | {r['grand']:>10}")


if __name__ == "__main__":
    asyncio.run(main())
