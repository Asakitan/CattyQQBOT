import logging
import sys
from pathlib import Path

src_path = Path(__file__).with_name("src")
if src_path.is_dir():
    sys.path.insert(0, str(src_path))

from catty_config_loader import load_config_to_env
import nonebot
from nonebot import logger
from nonebot.log import logger_id, default_format
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


loaded_config = load_config_to_env()
if loaded_config is not None:
    print(f"Loaded config: {loaded_config}")

_log_dir = Path(__file__).with_name("logs")
_log_dir.mkdir(parents=True, exist_ok=True)

# 干掉 nonebot 默认的 stderr sink: 它按 LOG_LEVEL=DEBUG 跑, 会把 matcher 优先级检查
# 这种纯噪音全刷到控制台。我们重建三条 sink:
#   1. stderr 只收 INFO+
#   2. bot_live.log 只收 INFO+, 滚动归档
#   3. logs/YYYY-MM-DD-debug.txt 只收 DEBUG, 按日切割, 保留 7 天
try:
    logger.remove(logger_id)
except ValueError:
    pass

# 主人 2026-05-27: 屏蔽『每条消息触发』的纯噪音 INFO, 让面板只显示业务相关日志.
# nonebot._run_matcher 实际在 nonebot.message 模块, simple_run 在 nonebot 子模块,
# 所以用 startswith('nonebot') 覆盖整个 nonebot.* namespace.
#
# 屏蔽的三类:
#   (1) nonebot._run_matcher/simple_run: catch-all observe_matcher 每消息打 2 条 matcher 命中/完成
#   (2) nonebot.handle_event 的 [message.group.normal] / [message.private] / [notice.*]: SUCCESS
#       级 incoming 消息明细 (不相关消息内容直接在面板裸露). [message_sent] 保留, 让主人能看笨猫回了啥.
#   (3) catty_qq_ai._hot_reload_loop 的 INFO: corpus 每写一次 memory.json → signature 变 →
#       打一条 "Hot reloaded memory files". 功能正常但纯噪音. warning/error 保留 (refresh 失败要看到).
#
# 业务路径 (handle_chat enter / NSFW route 等) 在 catty_qq_ai 模块自己打 INFO, 不受此过滤.
def _drop_nonebot_matcher_noise(record: "dict") -> bool:
    name = record.get("name") or ""
    func = record.get("function") or ""
    if name.startswith("nonebot") and func in ("_run_matcher", "simple_run"):
        return False
    if name.startswith("nonebot") and func == "handle_event":
        msg = str(record.get("message") or "")
        # 只屏蔽 incoming event, 保留 [message_sent] (笨猫自己发的)
        if "[message_sent]" not in msg:
            return False
    if name == "catty_qq_ai" and func == "_hot_reload_loop":
        # 只屏蔽 INFO 级 refresh 成功, warning/error (refresh 失败) 保留
        level = record.get("level")
        level_name = level.name if level is not None else ""
        if level_name == "INFO":
            return False
    return True


logger.add(
    sys.stderr,
    level="INFO",
    format=default_format,
    diagnose=False,
    filter=_drop_nonebot_matcher_noise,
)

logger.add(
    str(_log_dir / "bot_live.log"),
    rotation="20 MB",
    retention=3,
    encoding="utf-8",
    level="INFO",
    enqueue=True,
    filter=_drop_nonebot_matcher_noise,
)

logger.add(
    str(_log_dir / "{time:YYYY-MM-DD}-debug.txt"),
    rotation="00:00",
    retention="7 days",
    encoding="utf-8",
    level="DEBUG",
    filter=lambda record: record["level"].name == "DEBUG",
    format=default_format,
    enqueue=True,
)


# stdlib logging → loguru 桥接:openai_client / tools / web_search / hot_trends 等
# 大量模块用 _logger = logging.getLogger("catty_qq_ai.xxx"),它们的 INFO 因为 stdlib
# 默认 root level=WARNING 一直被吞掉,导致 tool_chat / tool_call / fallback routing 等
# 关键 observability 日志在 bot_live.log 里完全看不见。加 InterceptHandler 把这些
# 桥接到 loguru, 同时把 catty_qq_ai.* 的 stdlib level 显式调到 INFO。
class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


_intercept = _InterceptHandler()
for _name in ("catty_qq_ai", "nonebot"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.INFO)
    if not any(isinstance(h, _InterceptHandler) for h in _lg.handlers):
        _lg.addHandler(_intercept)
    _lg.propagate = False

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_plugin("catty_qq_ai")

if loaded_config is not None:
    from catty_integrations import start_integrated_processes

    @driver.on_startup
    async def _start_integrations() -> None:
        start_integrated_processes(loaded_config.data, loaded_config.path.parent)


if __name__ == "__main__":
    nonebot.run()
