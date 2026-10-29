import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

src_path = Path(__file__).with_name("src")
if src_path.is_dir():
    sys.path.insert(0, str(src_path))


def _disable_console_quick_edit() -> None:
    """关掉控制台「快速编辑」: 开着时在窗口里点一下就进入选择状态, 往控制台写输出的进程
    会被挂起直到取消选择。2026-10 两次 RDP 重连后 bot 无日志卡死多天, 高度疑似此因。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        handle = kernel32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))  # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        enable_quick_edit, enable_extended_flags = 0x0040, 0x0080
        new_mode = (mode.value | enable_extended_flags) & ~enable_quick_edit
        if new_mode != mode.value:
            kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


_disable_console_quick_edit()

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

# 主人 2026-05-27: 『每条消息触发』的纯噪音 INFO 不进 stderr/bot_live.log, 改路由到 noise.log
# 让 AI 还能 grep 排查, 但主人面板/主日志干净.
# nonebot._run_matcher 实际在 nonebot.message 模块, simple_run 在 nonebot 子模块,
# 所以用 startswith('nonebot') 覆盖整个 nonebot.* namespace.
#
# 三类被分流到 noise.log:
#   (1) nonebot._run_matcher/simple_run: catch-all observe_matcher 每消息打 2 条 matcher 命中/完成
#   (2) nonebot.handle_event 的 [message.group.normal] / [message.private] / [notice.*]: SUCCESS
#       级 incoming 消息明细. [message_sent] 留在主日志, 让主人能看笨猫回了啥.
#   (3) catty_qq_ai._hot_reload_loop 的 INFO: corpus 每写一次 memory.json → signature 变 →
#       打一条 "Hot reloaded memory files". 功能正常但纯噪音. warning/error 保留在主日志 (refresh 失败要看到).
#
# 业务路径 (handle_chat enter / NSFW route 等) 在 catty_qq_ai 模块自己打 INFO, 不受此过滤.
def _is_panel_noise(record: "dict") -> bool:
    name = record.get("name") or ""
    func = record.get("function") or ""
    if name.startswith("nonebot") and func in ("_run_matcher", "simple_run"):
        return True
    if name.startswith("nonebot") and func == "handle_event":
        msg = str(record.get("message") or "")
        if "[message_sent]" not in msg:
            return True
    if name == "catty_qq_ai" and func == "_hot_reload_loop":
        level = record.get("level")
        level_name = level.name if level is not None else ""
        if level_name == "INFO":
            return True
    return False


def _drop_panel_noise(record: "dict") -> bool:
    return not _is_panel_noise(record)


# enqueue: 控制台写入在后台线程完成。控制台被挂起 (QuickEdit 选择/窗口卡顿) 时
# 同步写 stderr 会把整个事件循环一起卡住, 连文件日志都停。
logger.add(
    sys.stderr,
    level="INFO",
    format=default_format,
    diagnose=False,
    filter=_drop_panel_noise,
    enqueue=True,
)

logger.add(
    str(_log_dir / "bot_live.log"),
    rotation="20 MB",
    retention=3,
    encoding="utf-8",
    level="INFO",
    enqueue=True,
    filter=_drop_panel_noise,
)

# 噪音独立归档: AI / 排查脚本可 grep noise.log; 主人面板/bot_live.log 看不到.
logger.add(
    str(_log_dir / "noise.log"),
    rotation="20 MB",
    retention=3,
    encoding="utf-8",
    level="DEBUG",
    enqueue=True,
    filter=_is_panel_noise,
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

# Windows + Python 3.14 asyncio Proactor: 远端 RST 后 _call_connection_lost 调 socket.shutdown()
# 抛 ConnectionResetError(WinError 10054), 业务无影响, 静默掉避免刷屏.
def _silence_proactor_connection_reset(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError) and getattr(exc, "winerror", None) == 10054:
        return
    loop.default_exception_handler(context)


def _install_loop_exception_handler() -> None:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.set_exception_handler(_silence_proactor_connection_reset)


_install_loop_exception_handler()

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

@driver.on_startup
async def _reinstall_loop_exception_handler() -> None:
    asyncio.get_running_loop().set_exception_handler(_silence_proactor_connection_reset)


# 守护进程 (scripts/catty_hot_reload.py) 通过 CATTY_HEARTBEAT_FILE 传入心跳文件:
# 事件循环每 15s 写一次, 卡死时停更, 守护进程超时后重启 bot。直接跑 bot.py 时不启用。
_HEARTBEAT_INTERVAL_SECONDS = 15.0
_heartbeat_file = os.environ.get("CATTY_HEARTBEAT_FILE", "").strip()
_background_tasks: set[asyncio.Task] = set()


async def _supervisor_heartbeat_loop(path: Path) -> None:
    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{time.time():.0f}\n", encoding="utf-8")
        except OSError:
            pass
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)


if _heartbeat_file:

    @driver.on_startup
    async def _start_supervisor_heartbeat() -> None:
        task = asyncio.create_task(_supervisor_heartbeat_loop(Path(_heartbeat_file)))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

_catty_plugin = nonebot.load_plugin("catty_qq_ai")

# 修复发布的启动凭据：旧心跳不能证明新插件成功导入，绑定本次真实 PID。
_startup_ready_file = os.environ.get("CATTY_SELF_REPAIR_STARTUP_FILE", "").strip()
if _startup_ready_file:

    @driver.on_startup
    async def _mark_supervisor_startup_ready() -> None:
        path = Path(_startup_ready_file)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({
                "pid": os.getpid(), "at": time.time(),
                "plugin_loaded": _catty_plugin is not None,
            }), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            logger.warning("Could not write supervisor startup readiness marker")
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

if loaded_config is not None:
    from catty_integrations import start_integrated_processes

    @driver.on_startup
    async def _start_integrations() -> None:
        # 2026-08-18: start_integrated_processes is fully synchronous; the qzone
        # bridge branch polls health 30x1.6s (~50s) while it can't log in. Awaiting
        # that on_startup delayed uvicorn's listen -> NapCat kept ECONNREFUSED and
        # the bot looked frozen after every restart. Fire-and-forget on a thread:
        # server ready in ms, integrations keep starting in the background.
        asyncio.create_task(
            asyncio.to_thread(start_integrated_processes, loaded_config.data, loaded_config.path.parent)
        )


if __name__ == "__main__":
    nonebot.run()
