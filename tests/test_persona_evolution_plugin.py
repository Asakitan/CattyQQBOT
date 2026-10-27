from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PersonaEvolutionPluginTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("nonebot") is None, "nonebot not installed")
    def test_real_message_builder_uses_only_current_persona_style(self):
        script = textwrap.dedent('''
            import asyncio, os, sys
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
