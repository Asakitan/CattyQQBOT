import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_qq_ai_emoji_store_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules.setdefault(_PACKAGE_NAME, _PACKAGE)

_CONFIG_MODULE = types.ModuleType(f"{_PACKAGE_NAME}.config")
_CONFIG_MODULE.Config = object
sys.modules.setdefault(f"{_PACKAGE_NAME}.config", _CONFIG_MODULE)

_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "emoji_store.py"
_SPEC = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.emoji_store", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_emoji_store = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _emoji_store
_SPEC.loader.exec_module(_emoji_store)
EmojiStore = _emoji_store.EmojiStore


class EmojiStoreTests(unittest.TestCase):
    def test_default_emoji_files_are_auto_registered_and_unknown_query_does_not_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "emojis"
            root.mkdir()
            (root / "唐猫不屑.jpg").write_bytes(b"registered")
            (root / "未登记.jpg").write_bytes(b"unregistered")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "emojis": {
                            "唐猫不屑.jpg": {
                                "meaning": "唐猫不屑",
                                "tags": ["唐猫不屑"],
                                "source": "default",
                                "priority": 100,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = types.SimpleNamespace(
                catty_emoji_enabled=True,
                catty_emoji_dir=str(root),
                catty_emoji_download_dir=str(root / "downloaded"),
                catty_emoji_manifest_path=str(manifest_path),
                catty_emoji_max_candidates=8,
            )

            store = EmojiStore(config)

            self.assertEqual([entry.path.name for entry in store._entries], ["唐猫不屑.jpg", "未登记.jpg"])
            self.assertEqual(store.choose("不屑").path.name, "唐猫不屑.jpg")
            self.assertIsNone(store.choose("给爷喵一个"))
            self.assertEqual(store.choose("未登记").path.name, "未登记.jpg")
            self.assertEqual(store.choose("事后喵。", refresh_on_miss=True), None)
            self.assertEqual(store.choose("").path.name, "唐猫不屑.jpg")

    def test_adopts_matching_downloaded_file_only_when_requested(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "emojis"
            download_dir = root / "downloaded"
            download_dir.mkdir(parents=True)
            (download_dir / "傻猫.jpg").write_bytes(b"downloaded")
            (root / "未登记.jpg").write_bytes(b"default")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"version": 1, "emojis": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = types.SimpleNamespace(
                catty_emoji_enabled=True,
                catty_emoji_dir=str(root),
                catty_emoji_download_dir=str(download_dir),
                catty_emoji_manifest_path=str(manifest_path),
                catty_emoji_max_candidates=8,
            )

            store = EmojiStore(config)

            self.assertIsNone(store.choose("傻猫"))
            self.assertIsNone(store.adopt_downloaded("不相关"))
            adopted = store.adopt_downloaded("傻猫")
            self.assertIsNotNone(adopted)
            self.assertEqual(adopted.path.name, "傻猫.jpg")
            self.assertEqual(store.choose("未登记").path.name, "未登记.jpg")

    def test_choose_can_refresh_after_manifest_or_files_change(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "emojis"
            root.mkdir()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"version": 1, "emojis": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = types.SimpleNamespace(
                catty_emoji_enabled=True,
                catty_emoji_dir=str(root),
                catty_emoji_download_dir=str(root / "downloaded"),
                catty_emoji_manifest_path=str(manifest_path),
                catty_emoji_max_candidates=8,
            )
            store = EmojiStore(config)

            (root / "事后喵.jpg").write_bytes(b"registered later")
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "emojis": {
                            "事后喵.jpg": {
                                "meaning": "事后喵",
                                "tags": ["事后喵"],
                                "source": "default",
                                "priority": 100,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertIsNone(store.choose("事后喵"))
            self.assertEqual(store.choose("事后喵", refresh_on_miss=True).path.name, "事后喵.jpg")
            self.assertEqual(store.choose("事后喵。").path.name, "事后喵.jpg")


if __name__ == "__main__":
    unittest.main()
