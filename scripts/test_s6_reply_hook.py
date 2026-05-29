"""S6 reply-hook 安全阀烟测 (主人 2026-05-29).

验证 openai_client 的统一蒸馏 hook + contextvar 安全阀逻辑, 不碰网络/不依赖 nonebot:
用 sys.modules stub 掉 openai_client 的重依赖 (httpx/PIL/config/mc_status/...),
单独加载 openai_client.py, 然后逐条断言 _maybe_distill_reply 在各场景下是否触发 hook.

用法 (本地或远端均可):
    python scripts/test_s6_reply_hook.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_openai_client():
    # 重依赖 stub (openai_client module-level import 的)
    _stub("httpx")
    pil = _stub("PIL")
    pil.Image = _stub("PIL.Image")
    pil.ImageSequence = _stub("PIL.ImageSequence")

    # catty_qq_ai 包 + openai_client 的相对依赖
    pkg = types.ModuleType("catty_qq_ai")
    pkg.__path__ = [str(SRC / "catty_qq_ai")]
    sys.modules["catty_qq_ai"] = pkg

    class _Config:  # 占位, openai_client module-level 不实例化
        pass

    _stub("catty_qq_ai.config", Config=_Config)
    _stub("catty_qq_ai.mc_status", mc_has_players=lambda *a, **k: False)
    _stub("catty_qq_ai.parsers", lenient_json_object=lambda t: None)
    _stub(
        "catty_qq_ai.reply_markers",
        INLINE_IMAGE_PREFIX="[img]",
        INLINE_IMAGE_SUFFIX="[/img]",
    )

    path = SRC / "catty_qq_ai" / "openai_client.py"
    spec = importlib.util.spec_from_file_location("catty_qq_ai.openai_client", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["catty_qq_ai.openai_client"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    oc = _load_openai_client()

    calls: list[tuple] = []

    def fake_hook(user_text, assistant_text, scope, terms, source):
        calls.append((user_text, assistant_text, scope, list(terms), source))

    oc.set_reply_distill_hook(fake_hook)

    failures: list[str] = []

    def check(name: str, cond: bool):
        status = "OK " if cond else "FAIL"
        print(f"  [{status}] {name}")
        if not cond:
            failures.append(name)

    # case 1: 没 set distill ctx → 不蒸 (安全阀: 非 handle_chat 入口路径)
    calls.clear()
    oc.set_current_scope_key("private:123")
    oc.clear_current_distill_context()
    oc._maybe_distill_reply("回复喵", source="deepseek")
    check("no-ctx → 不蒸", calls == [])

    # case 2: set ctx + scope=private → 蒸, 参数透传正确
    calls.clear()
    oc.set_current_scope_key("private:123")
    oc.set_current_distill_context("你好", ["主人", "123"])
    oc._maybe_distill_reply("你好喵~贴贴", source="deepseek")
    check(
        "private + ctx → 蒸 + 参数正确",
        len(calls) == 1
        and calls[0][0] == "你好"
        and calls[0][1] == "你好喵~贴贴"
        and calls[0][2] == "private:123"
        and calls[0][3] == ["主人", "123"]
        and calls[0][4] == "deepseek",
    )

    # case 3: scope=summary:* → 不蒸 (即使有 ctx; 后台总结路径)
    calls.clear()
    oc.set_current_scope_key("summary:group:1")
    oc.set_current_distill_context("总结输入", [])
    oc._maybe_distill_reply("总结结果", source="deepseek")
    check("summary scope → 不蒸", calls == [])

    # case 4: group scope 正常蒸
    calls.clear()
    oc.set_current_scope_key("group:895042854")
    oc.set_current_distill_context("中午吃啥", [])
    oc._maybe_distill_reply("火锅喵~", source="deepseek_codex")
    check(
        "group + ctx → 蒸 (source 透传)",
        len(calls) == 1 and calls[0][2] == "group:895042854" and calls[0][4] == "deepseek_codex",
    )

    # case 5: 空/纯空白 reply → 不蒸
    calls.clear()
    oc.set_current_scope_key("group:9")
    oc.set_current_distill_context("hi", [])
    oc._maybe_distill_reply("   ", source="deepseek")
    check("空白 reply → 不蒸", calls == [])

    # case 6: 空 user_text → 不蒸
    calls.clear()
    oc.set_current_distill_context("", [])
    oc._maybe_distill_reply("有回复内容", source="deepseek")
    check("空 user_text → 不蒸", calls == [])

    # case 7: scope=None (空) → 不蒸
    calls.clear()
    oc.set_current_scope_key(None)
    oc.set_current_distill_context("hi", [])
    oc._maybe_distill_reply("回复", source="deepseek")
    check("无 scope → 不蒸", calls == [])

    # case 8: hook=None → 静默不报错
    calls.clear()
    oc.set_reply_distill_hook(None)
    oc.set_current_scope_key("private:1")
    oc.set_current_distill_context("hi", [])
    try:
        oc._maybe_distill_reply("回复", source="deepseek")
        check("hook=None → 不报错且不蒸", calls == [])
    except Exception as exc:  # noqa: BLE001
        check(f"hook=None → 不报错 (raised {exc!r})", False)

    # case 9: suppress (占位话路径) → 不蒸; reset 后恢复
    calls.clear()
    oc.set_reply_distill_hook(fake_hook)  # case 8 设过 None, 重新注册
    oc.set_current_scope_key("private:1")
    oc.set_current_distill_context("吃了吗", [])
    tok = oc.set_distill_suppressed(True)
    oc._maybe_distill_reply("猫猫现在很忙~稍等喵", source="deepseek_codex")
    check("suppressed (占位话) → 不蒸", calls == [])
    oc.reset_distill_suppressed(tok)
    oc._maybe_distill_reply("吃过啦主人嗷呜~", source="deepseek")
    check(
        "reset 后 → 正式回复正常蒸",
        len(calls) == 1 and calls[0][1] == "吃过啦主人嗷呜~",
    )

    total = 10
    print()
    if failures:
        print(f"=== FAILED {len(failures)}/{total}: {failures} ===")
        return 1
    print(f"=== ALL {total} CASES PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
