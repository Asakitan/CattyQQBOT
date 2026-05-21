import sys
from pathlib import Path

src_path = Path(__file__).with_name("src")
if src_path.is_dir():
    sys.path.insert(0, str(src_path))

from catty_config_loader import load_config_to_env
import nonebot
from nonebot import logger
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter


loaded_config = load_config_to_env()
if loaded_config is not None:
    print(f"Loaded config: {loaded_config}")

_log_dir = Path(__file__).with_name("logs")
_log_dir.mkdir(parents=True, exist_ok=True)
logger.add(
    str(_log_dir / "bot_live.log"),
    rotation="20 MB",
    retention=3,
    encoding="utf-8",
    level="INFO",
    enqueue=True,
)

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
