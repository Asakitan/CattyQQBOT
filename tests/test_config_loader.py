from pathlib import Path
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


_MODULE_PATH = Path(__file__).resolve().parents[1] / "catty_config_loader.py"
_SPEC = importlib.util.spec_from_file_location("catty_config_loader", _MODULE_PATH)
_loader = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = _loader
_SPEC.loader.exec_module(_loader)


class ConfigLoaderTests(unittest.TestCase):
    def test_local_critic_extra_body_gets_keep_alive_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            data = {
                "server": {},
                "qq": {},
                "ai": {},
                "audit_ai": {},
                "vision": {},
                "filter": {},
                "local_critic": {"extra_body": {"think": False}},
                "local_training": {},
                "web_search": {},
                "turtle_soup": {},
                "chat": {},
                "emoji": {},
                "memory": {},
                "proactive": {},
                "access": {},
            }

            with patch.dict(os.environ, {}, clear=True):
                _loader._apply_config(data, base_dir)
                extra_body = json.loads(os.environ["CATTY_LOCAL_CRITIC_EXTRA_BODY"])

        self.assertFalse(extra_body["think"])
        self.assertEqual(extra_body["keep_alive"], "30m")


if __name__ == "__main__":
    unittest.main()
