from __future__ import annotations

import ast
import asyncio
import importlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from test_memory_persistence import memory


_ENTRY_POINT = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "__init__.py"
_SOURCE = ast.parse(_ENTRY_POINT.read_text(encoding="utf-8"))


def _runtime_functions(*names: str) -> dict:
    # Execute the real orchestration functions in an isolated namespace. Importing
    # the entry point itself would initialize stores and register the live plugin.
    selected = [
        node for node in _SOURCE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    if len(selected) != len(names):
        raise AssertionError("Requested runtime function was removed or renamed")
    namespace = {
        "__package__": memory.__package__,
        "Config": memory.Config,
        "MemoryStore": memory.MemoryStore,
        "MemoryPersistenceError": memory.MemoryPersistenceError,
        "Path": Path,
        "logger": Mock(),
        "asyncio": asyncio,
        "os": types.SimpleNamespace(environ={}),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(_ENTRY_POINT), "exec"), namespace)
    return namespace


class MemoryConfigTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = memory.Config(catty_memory_path=str(Path(temporary.name) / "memory.json"))
        self.store = memory.MemoryStore(self.config)
        self.store.set_preferred_name("10001", "pending name")
        self.ns = _runtime_functions("_apply_runtime_config")
        self.ns.update({
            "config": self.config,
            "memory_store": self.store,
            "affection_store": Mock(flush_sync=Mock(return_value=False)),
            "timeline_store": Mock(flush_sync=Mock(return_value=False)),
            "adaptive_prompt_store": Mock(flush_sync=Mock(return_value=False)),
            "qzone_feed_store": None,
            "_qzone_feed_store_generation": 0,
            "_rebuild_persona_emoji_stores": Mock(),
            "LegsPicker": Mock(),
            "AffectionStore": Mock(),
            "DailyTimelineStore": Mock(),
            "AdaptiveEvolutionPromptStore": Mock(),
            "_memory_sidecar_path": lambda cfg, name: Path(cfg.catty_memory_path).with_name(name),
            "_legs_last_sent_at": {},
            "_keyword_reply_last_sent_at": {},
            "_sync_hot_reload_signatures": Mock(),
        })

    def test_failed_preflush_preserves_store_and_config_until_retry(self) -> None:
        new_config = self.config.model_copy()
        with patch.object(self.store, "_persist_now", side_effect=OSError("disk unavailable")):
            with self.assertRaises(memory.MemoryPersistenceError):
                self.ns["_apply_runtime_config"](new_config)
        self.assertIs(self.ns["config"], self.config)
        self.assertIs(self.ns["memory_store"], self.store)
        self.ns["_sync_hot_reload_signatures"].assert_not_called()
        self.ns["affection_store"].flush_sync.assert_not_called()
        self.ns["_apply_runtime_config"](new_config)
        self.assertIs(self.ns["config"], new_config)
        self.assertIsNot(self.ns["memory_store"], self.store)
        self.assertEqual(self.ns["memory_store"].preferred_name_for("10001"), "pending name")
        self.ns["_sync_hot_reload_signatures"].assert_called_once()

    def test_disabling_memory_still_saves_the_old_enabled_store(self) -> None:
        new_config = self.config.model_copy(update={"catty_memory_enabled": False})
        self.ns["_apply_runtime_config"](new_config)
        self.assertFalse(self.ns["memory_store"].enabled)
        self.assertEqual(memory.MemoryStore(self.config).preferred_name_for("10001"), "pending name")

    def test_path_change_still_saves_to_old_path_before_switching(self) -> None:
        new_config = self.config.model_copy(update={
            "catty_memory_path": str(Path(self.config.catty_memory_path).with_name("other.json")),
        })
        self.ns["_apply_runtime_config"](new_config)
        self.assertEqual(memory.MemoryStore(self.config).preferred_name_for("10001"), "pending name")
        self.assertEqual(self.ns["memory_store"].path, Path(new_config.catty_memory_path))


class MemoryReloadEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_transition_rolls_back_only_its_environment_changes(self) -> None:
        ns = _runtime_functions("_reload_runtime_config_from_path")
        env = ns["os"].environ
        env.update({"CHANGED": "old", "REMOVED": "old", "LATER": "old", "UNTOUCHED": "old"})
        new_config = object()

        def load_config(path):
            env.update({"CHANGED": "parsed", "ADDED": "parsed", "LATER": "parsed"})
            env.pop("REMOVED")
            return new_config

        async def transition(config):
            self.assertIs(config, new_config)
            env["LATER"] = "another update"
            env["UNRELATED"] = "another update"
            raise memory.MemoryPersistenceError("disk unavailable")

        ns["_load_runtime_config_from_path"] = load_config
        ns["_transition_runtime_config"] = transition
        with self.assertRaises(memory.MemoryPersistenceError):
            await ns["_reload_runtime_config_from_path"](Path("config.json"))
        self.assertEqual(env, {
            "CHANGED": "old", "REMOVED": "old", "LATER": "another update",
            "UNTOUCHED": "old", "UNRELATED": "another update",
        })
        ns["logger"].info.assert_not_called()

    async def test_successful_transition_keeps_loaded_environment(self) -> None:
        ns = _runtime_functions("_reload_runtime_config_from_path")
        new_config = object()

        def load_config(path):
            ns["os"].environ["CHANGED"] = "parsed"
            return new_config

        ns["_load_runtime_config_from_path"] = load_config
        ns["_transition_runtime_config"] = AsyncMock()
        self.assertTrue(await ns["_reload_runtime_config_from_path"](Path("config.json")))
        self.assertEqual(ns["os"].environ, {"CHANGED": "parsed"})
        ns["_transition_runtime_config"].assert_awaited_once_with(new_config)
        ns["logger"].info.assert_called_once()


class MemoryReloadWatcherTests(unittest.IsolatedAsyncioTestCase):
    def _watcher(self, *, polls: int = 2) -> dict:
        ns = _runtime_functions("_hot_reload_loop", "_remember_hot_reload_config_signature")
        ns.update({
            "config": types.SimpleNamespace(catty_hot_reload_poll_seconds=0.2, catty_hot_reload_enabled=True),
            "_sync_hot_reload_signatures": Mock(),
            "_runtime_config_path": Mock(return_value=Path("config.json")),
            "_file_signature": Mock(return_value="old config"),
            "_hot_reload_config_signature": "old config",
            "_hot_reload_emoji_signature": "old emoji",
            "_hot_reload_memory_signature": "old memory",
            "_emoji_signature_for_config": Mock(return_value="old emoji"),
            "_memory_signature_for_store": Mock(return_value="old memory"),
            "_refresh_all_emoji_stores": Mock(),
            "_reload_runtime_config_from_path": AsyncMock(return_value=True),
            "memory_store": Mock(),
            "qzone_feed_store": None,
        })
        # Stop deterministically after the requested number of polls; no real waits.
        sleep = AsyncMock(side_effect=[None] * polls + [asyncio.CancelledError()])
        ns["asyncio"] = types.SimpleNamespace(sleep=sleep)
        return ns

    async def _run_watcher(self, ns: dict) -> None:
        with self.assertRaises(asyncio.CancelledError):
            await ns["_hot_reload_loop"]()

    async def test_deferred_config_retries_without_acknowledging_failed_signature(self) -> None:
        ns = self._watcher()
        ns["_file_signature"].return_value = "new config"
        calls = []

        async def reload_config(path):
            calls.append(path)
            self.assertEqual(ns["_hot_reload_config_signature"], "old config")
            if len(calls) == 1:
                raise memory.MemoryPersistenceError("disk unavailable")
            return True

        ns["_reload_runtime_config_from_path"] = reload_config
        await self._run_watcher(ns)
        self.assertEqual(len(calls), 2)
        self.assertEqual(ns["_hot_reload_config_signature"], "new config")
        ns["_sync_hot_reload_signatures"].assert_called_once()

    async def test_failed_memory_refresh_retries_and_logs_success_only_after_recovery(self) -> None:
        ns = self._watcher()
        ns["_memory_signature_for_store"].return_value = "new memory"
        ns["memory_store"].refresh.side_effect = [memory.MemoryPersistenceError("disk unavailable"), None]
        await self._run_watcher(ns)
        self.assertEqual(ns["memory_store"].refresh.call_count, 2)
        self.assertEqual(ns["_hot_reload_memory_signature"], "new memory")
        ns["logger"].info.assert_called_once_with("Hot reloaded memory files")
        ns["_sync_hot_reload_signatures"].assert_called_once()

    async def test_emoji_reload_does_not_acknowledge_pending_memory_change(self) -> None:
        ns = self._watcher()
        ns["_emoji_signature_for_config"].return_value = "new emoji"
        ns["_memory_signature_for_store"].return_value = "new memory"
        await self._run_watcher(ns)
        ns["_refresh_all_emoji_stores"].assert_called_once()
        ns["memory_store"].refresh.assert_called_once()
        self.assertEqual(ns["_hot_reload_emoji_signature"], "new emoji")
        self.assertEqual(ns["_hot_reload_memory_signature"], "new memory")
        ns["_sync_hot_reload_signatures"].assert_called_once()

    async def test_emoji_refresh_self_write_does_not_starve_memory_refresh(self) -> None:
        ns = self._watcher(polls=3)
        current_signature = "external emoji edit"

        def refresh_emoji():
            nonlocal current_signature
            # Real EmojiStore.refresh() saves manifest.json on each refresh.
            current_signature += " + saved manifest"

        ns["_emoji_signature_for_config"].side_effect = lambda config: current_signature
        ns["_refresh_all_emoji_stores"].side_effect = refresh_emoji
        ns["_memory_signature_for_store"].return_value = "new memory"
        await self._run_watcher(ns)
        ns["_refresh_all_emoji_stores"].assert_called_once()
        ns["memory_store"].refresh.assert_called_once()
        self.assertEqual(ns["_hot_reload_emoji_signature"], current_signature)

    async def test_emoji_failure_keeps_other_refreshes_reachable(self) -> None:
        ns = self._watcher()
        ns["_emoji_signature_for_config"].return_value = "new emoji"
        ns["_refresh_all_emoji_stores"].side_effect = OSError("manifest temporarily locked")
        ns["_memory_signature_for_store"].return_value = "new memory"
        ns["qzone_feed_store"] = Mock()
        await self._run_watcher(ns)
        self.assertEqual(ns["_refresh_all_emoji_stores"].call_count, 2)
        ns["memory_store"].refresh.assert_called_once()
        self.assertEqual(ns["qzone_feed_store"].refresh.call_count, 2)
        self.assertEqual(ns["_hot_reload_emoji_signature"], "old emoji")

    async def test_real_emoji_manifest_self_write_settles_after_one_refresh(self) -> None:
        emoji_module = importlib.import_module(f"{memory.__package__}.emoji_store")
        signature_functions = _runtime_functions("_tree_signature")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "emojis"
            store = emoji_module.EmojiStore(
                memory.Config(), root=root, download_dir=root / "downloaded",
                manifest_path=root / "manifest.json", allow_bundled_fallback=False,
            )
            signature = lambda cfg: signature_functions["_tree_signature"]([root])
            ns = self._watcher(polls=3)
            ns["_emoji_signature_for_config"].side_effect = signature
            ns["_hot_reload_emoji_signature"] = signature(ns["config"])
            # The scanner uses paths/metadata, not decoded pixels.
            (root / "offline.png").write_bytes(b"offline metadata fixture")
            ns["_refresh_all_emoji_stores"] = Mock(wraps=store.refresh)
            ns["_memory_signature_for_store"].return_value = "new memory"
            ns["qzone_feed_store"] = Mock()
            await self._run_watcher(ns)
            ns["_refresh_all_emoji_stores"].assert_called_once()
            ns["memory_store"].refresh.assert_called_once()
            self.assertEqual(ns["qzone_feed_store"].refresh.call_count, 3)
            self.assertEqual(ns["_hot_reload_emoji_signature"], signature(ns["config"]))

    async def test_config_edit_during_transition_is_not_silently_acknowledged(self) -> None:
        ns = self._watcher(polls=1)
        ns["_file_signature"].return_value = "parsed config"

        async def reload_config(path):
            # The existing config application refreshes all signatures after
            # awaiting locks. A subsequent file edit must remain pending.
            ns["_hot_reload_config_signature"] = "newer edit"
            return True

        ns["_reload_runtime_config_from_path"] = reload_config
        await self._run_watcher(ns)
        self.assertEqual(ns["_hot_reload_config_signature"], "parsed config")


if __name__ == "__main__":
    unittest.main()
