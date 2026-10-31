from __future__ import annotations

import importlib.util
import ast
import copy
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]


def command_fixture(owner_id=7):
    source = ROOT / "src" / "catty_qq_ai" / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_EVOLUTION_CMD_RE", "_PERSONA_EVOLUTION_CMD_RE", "_evolution_cmd_rule", "handle_evolution_cmd"}
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets):
            selected.append(node)
        elif isinstance(node, ast.AsyncFunctionDef) and node.name in names:
            node = copy.deepcopy(node)
            node.decorator_list = []
            selected.append(node)
    namespace = {
        "re": re, "Bot": object, "MessageEvent": object, "Matcher": object, "T_State": dict,
        "_event_is_owner": lambda event: event.user_id == owner_id,
        "event_plain_text": lambda event: event.text,
        "_conversation_queue_key": lambda event: event.scope,
        "_evolve_persona_for_scope": AsyncMock(return_value="updated"),
        "logger": Mock(),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class PersonaEvolutionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_commands_run_once_for_current_scope(self):
        for text in ("/人格进化", "#人格进化", "/persona_evolve", "#PERSONA_EVOLVE"):
            with self.subTest(text=text):
                namespace = command_fixture()
                event = types.SimpleNamespace(user_id=7, text=text, scope="group:TARGET")
                state = {}
                self.assertTrue(await namespace["_evolution_cmd_rule"](types.SimpleNamespace(self_id=99), event, state))
                matcher = types.SimpleNamespace(send=AsyncMock(), finish=AsyncMock())
                await namespace["handle_evolution_cmd"](matcher, event, state)
                namespace["_evolve_persona_for_scope"].assert_awaited_once_with("group:TARGET")
                matcher.finish.assert_awaited_once_with("人格语气已更新；定时进化仍关闭。")

    async def test_other_users_cannot_match_or_invoke_the_handler(self):
        namespace = command_fixture()
        event = types.SimpleNamespace(user_id=8, text="/人格进化", scope="group:TARGET")
        self.assertFalse(await namespace["_evolution_cmd_rule"](types.SimpleNamespace(self_id=99), event, {}))
        matcher = types.SimpleNamespace(send=AsyncMock(), finish=AsyncMock())
        await namespace["handle_evolution_cmd"](matcher, event, {"evolution_cmd": "persona_evolve"})
        namespace["_evolve_persona_for_scope"].assert_not_awaited()
        matcher.send.assert_not_awaited()
        matcher.finish.assert_not_awaited()

    async def test_explicit_prefix_is_required_and_arguments_do_not_generate(self):
        namespace = command_fixture()
        event = types.SimpleNamespace(user_id=7, text="人格进化", scope="private:TARGET")
        self.assertFalse(await namespace["_evolution_cmd_rule"](types.SimpleNamespace(self_id=99), event, {}))
        event.text = "/人格进化 another"
        state = {}
        self.assertTrue(await namespace["_evolution_cmd_rule"](types.SimpleNamespace(self_id=99), event, state))
        matcher = types.SimpleNamespace(send=AsyncMock(), finish=AsyncMock())
        await namespace["handle_evolution_cmd"](matcher, event, state)
        namespace["_evolve_persona_for_scope"].assert_not_awaited()
        matcher.send.assert_not_awaited()
        matcher.finish.assert_awaited_once()

    async def test_legacy_engine_commands_keep_their_existing_routing(self):
        namespace = command_fixture()
        for text, command, argument in (("/evolve", "evolve", ""), ("#rollback_evolution 2", "rollback_evolution", "2")):
            event = types.SimpleNamespace(user_id=7, text=text, scope="group:TARGET")
            state = {}
            self.assertTrue(await namespace["_evolution_cmd_rule"](types.SimpleNamespace(self_id=99), event, state))
            self.assertEqual(state, {"evolution_cmd": command, "evolution_arg": argument})


class PersonaEvolutionPluginTests(unittest.TestCase):
    def test_startup_does_not_schedule_persona_evolution(self):
        source = ROOT / "src" / "catty_qq_ai" / "__init__.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        startup = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "start_memory_summary_loop"
        )
        calls = {
            node.func.id for node in ast.walk(startup)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("_persona_evolution_loop", calls)
        self.assertNotIn("_persona_evolution_tick", calls)

    @unittest.skipIf(importlib.util.find_spec("nonebot") is None, "nonebot not installed")
    def test_real_message_builder_uses_only_current_persona_style(self):
        script = textwrap.dedent('''
            import asyncio, os, sys
            from types import SimpleNamespace
            root = sys.argv[1]
            sys.path[:0] = [root, os.path.join(root, 'src')]
            sys.modules['chromadb'] = None
            import nonebot
            nonebot.init(log_level='ERROR')
            assert nonebot.load_plugin('catty_qq_ai') is not None
            import catty_qq_ai as app
            import catty_qq_ai.prompt_manager as pm
            from catty_qq_ai.persona_runtime import PersonaRuntime
            from catty_qq_ai.persona_override_store import PersonaOverrideStore
            from catty_qq_ai.persona_evolution_store import PersonaEvolutionStore
            from catty_qq_ai.persona_evolution_runtime import persona_base_text
            from catty_qq_ai.personas import get_persona
            from nonebot.adapters.onebot.v11 import PrivateMessageEvent, Message
            from nonebot.adapters.onebot.v11.event import Sender
            pm._TIKTOKEN_TRIED, pm._TIKTOKEN_ENC = True, None
            app.config = app.config.model_copy(update={
                'catty_default_persona': 'catty',
                'catty_memory_path': os.path.join(os.getcwd(), 'memory.json'),
                'catty_persona_evolution_enabled': True,
            })
            app.persona_runtime = PersonaRuntime(os.path.join(os.getcwd(), 'personas'))
            app.persona_override_store = PersonaOverrideStore(app.config.catty_memory_path)
            store = PersonaEvolutionStore(app.persona_runtime.root)
            cat = get_persona('catty')
            other = get_persona('fadianji')
            assert store.save('catty', persona_base_text(cat), None, 'CAT_STYLE_FIXTURE')
            assert store.save('fadianji', persona_base_text(other), None, 'FDJ_STYLE_FIXTURE')
            text = 'fixture question'
            msg = Message(text)
            event = PrivateMessageEvent(time=0, self_id=99, post_type='message', sub_type='friend', user_id=7, message_type='private', message_id=1, message=msg, original_message=msg, raw_message=text, font=0, sender=Sender(user_id=7, nickname='user'))
            incoming = app.ExtractedMessage(text=text, history_content=text, mentioned=False, replied_to_self=False, used_prefix=False, image_urls=[], image_keys=[], has_image=False, directed=True, directed_strength='direct', directly_requested=True, needs_filter=False)
            async def main():
                app._event_is_owner = lambda value: value.user_id == 7
                command = Message('/人格进化')
                command_event = event.model_copy(update={'message': command, 'original_message': command, 'raw_message': str(command)})
                bot = SimpleNamespace(self_id=99)
                assert not await app._persona_cmd_rule(bot, command_event, {})
                state = {}
                assert await app._evolution_cmd_rule(bot, command_event, state)
                assert state['evolution_cmd'] == 'persona_evolve'
                first, _ = await app._build_messages(event, 'private:7', incoming)
                rendered = str(first)
                assert 'CAT_STYLE_FIXTURE' in rendered and 'FDJ_STYLE_FIXTURE' not in rendered, rendered[:500]
                app.persona_override_store.set('private:7', 'fadianji')
                second, _ = await app._build_messages(event, 'private:7', incoming)
                rendered = str(second)
                assert 'FDJ_STYLE_FIXTURE' in rendered and 'CAT_STYLE_FIXTURE' not in rendered, rendered[:500]
                app.config = app.config.model_copy(update={'catty_persona_evolution_enabled': False})
                third, _ = await app._build_messages(event, 'private:7', incoming)
                assert 'FDJ_STYLE_FIXTURE' not in str(third)
                print('PERSONA_EVOLUTION_REAL_BUILD_OK')
            asyncio.run(main())
        ''')
        allowed = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env["PYTHONUTF8"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-c", script, str(ROOT)],
                cwd=directory, env=env, capture_output=True, text=True, encoding="utf-8", timeout=40,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PERSONA_EVOLUTION_REAL_BUILD_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
