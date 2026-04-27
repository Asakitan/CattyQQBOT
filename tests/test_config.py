from pathlib import Path
import importlib.util
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "config.py"
_SPEC = importlib.util.spec_from_file_location("config", _MODULE_PATH)
_config = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_config)


class ConfigTests(unittest.TestCase):
    def test_special_care_user_ids_parse_from_json_strings(self) -> None:
        config = _config.Config(
            catty_special_care_user_ids="[1001, 1002]",
            catty_group_special_care_user_ids='{"12345":[1003,"1004"],"67890":"1005,1006"}',
        )

        self.assertEqual(config.catty_special_care_user_ids, {1001, 1002})
        self.assertEqual(config.catty_group_special_care_user_ids["12345"], {1003, 1004})
        self.assertEqual(config.catty_group_special_care_user_ids["67890"], {1005, 1006})


if __name__ == "__main__":
    unittest.main()
