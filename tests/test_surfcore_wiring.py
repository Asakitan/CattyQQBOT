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


class SurfcoreWiringTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("nonebot") is None, "nonebot not installed")
    def test_real_plugin_event_memory_goal_and_wait(self):
        script = textwrap.dedent('''
            import asyncio, os, sqlite3, sys, types
            from pathlib import Path
            root = sys.argv[1]
            sys.path[:0] = [root, os.path.join(root, 'src')]
            sys.modules['chromadb'] = None
            import nonebot
            nonebot.init(log_level='ERROR')
            assert nonebot.load_plugin('catty_qq_ai') is not None
            import catty_qq_ai as app
            from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
            from nonebot.adapters.onebot.v11.event import Sender
            app.config = app.config.model_copy(update={'catty_memory_path': os.path.join(os.getcwd(), 'memory.json')})
            app.memory_store = app.MemoryStore(app.config)
            extensionless = app.config.model_copy(update={'catty_memory_path': os.path.join(os.getcwd(), 'memory')})
            assert app._memory_sidecar_path(extensionless, 'insight_memory.sqlite3') == Path(os.getcwd()) / 'insight_memory.sqlite3'
            persona = types.SimpleNamespace(name='catty', char_name='猫猫', definition_revision=None)
            app._persona_for_scope = lambda scope: persona
            app._persona_for_event = lambda event: persona
            bot = types.SimpleNamespace(self_id=999)
            tasks = []
            original_queue = app._queue_layered_event
            def queue(event):
                task = original_queue(event)
                if task is not None:
                    tasks.append(task)
                return task
            app._queue_layered_event = queue
            def event(message_id, text, *, user=7, group=1, reply=None):
                message = Message()
                if reply is not None:
                    message += MessageSegment.reply(reply)
                message += MessageSegment.text(text)
                return GroupMessageEvent(time=0, self_id=999, post_type='message', sub_type='normal',
                    user_id=user, message_type='group', message_id=message_id, message=message,
                    original_message=message, raw_message=str(message), font=0,
                    sender=Sender(user_id=user, nickname='群友'), group_id=group)
            async def observe(value):
                await app.observe_memory(bot, value)
                pending, tasks[:] = list(tasks), []
                if pending:
                    await asyncio.gather(*pending)
            async def main():
                first = event(1, '我喜欢喝咖啡')
                await observe(first)
                memory_path = app._memory_sidecar_path(app.config, 'insight_memory.sqlite3')
                memory = app._layered_store(memory_path, 'memory')
                assert [r['text'] for r in memory.archive('group:1', 'catty')] == ['我喜欢喝咖啡']
                assert not memory.archive('group:2', 'catty')
                assert not memory.archive('group:1', 'fadianji')
                assert '咖啡' in memory.context('group:1', 'catty', '喜欢什么')
                await observe(event(2, '我想去跑步'))
                goals = await app._proactive_purpose_candidates('group:1', 'catty')
                assert len(goals) == 1 and goals[0]['owner'] == 'member'
                assert await app._proactive_purpose_delivered('group:1', 'catty', goals[0], '88')
                await observe(event(3, '完成了', user=8, reply=88))
                assert len(await app._proactive_purpose_candidates('group:1', 'catty')) == 1
                await observe(event(4, '完成了', user=7, reply=88))
                assert not await app._proactive_purpose_candidates('group:1', 'catty')
                with sqlite3.connect(app._memory_sidecar_path(app.config, 'purpose_ledger.sqlite3')) as db:
                    assert db.execute('SELECT status FROM purpose_goals').fetchone()[0] == 'done'
                rows = app._recent_messages_snapshot_for_scope('group:1')
                last = rows[-1]['seq']
                waiter = asyncio.create_task(app._wait_for_recent_conversation('group:1', last, 1.0))
                await asyncio.sleep(0)
                app._remember_recent_conversation_event(event(5, '别群消息', group=2))
                await asyncio.sleep(0)
                assert not waiter.done()
                new_event = event(6, '晚安')
                app._remember_recent_conversation_event(new_event)
                received = await asyncio.wait_for(waiter, .5)
                assert [row['text'] for row in received] == ['晚安']
                seq = received[-1]['seq']
                assert app._recent_conversation_cursor('group:1', 'catty', 'set', seq) == seq
                assert app._recent_conversation_cursor('group:1', 'fadianji', 'get', 0) == 0
                proposal = {'reason': '自然结束', 'observed_seq': seq}
                assert not app._commit_conversation_close(new_event, 'catty', proposal, [], user_text='晚安')
                assert not app._commit_conversation_close(new_event, 'catty', proposal, ['晚安'], user_text='还有问题吗？')
                assert not app._commit_conversation_close(new_event, 'catty', proposal, ['晚安'], user_text='晚安', pending_work=True)
                assert app._commit_conversation_close(new_event, 'catty', proposal, ['晚安'], user_text='晚安')
                app._remember_recent_conversation_event(event(7, '又来啦'))
                assert not app._commit_conversation_close(new_event, 'catty', proposal, ['晚安'], user_text='晚安')
                old_generation = app._layered_scope_generations['group:1']
                app._layered_scope_generations['group:1'] += 1
                purpose_path = app._memory_sidecar_path(app.config, 'purpose_ledger.sqlite3')
                await asyncio.to_thread(app._clear_layered_scope, 'group:1', old_generation + 1, memory_path, purpose_path)
                await asyncio.to_thread(app._record_layered_event, memory_path, purpose_path, 'group:1', 'catty',
                    'qq:1', '我喜欢喝咖啡', 'member', '7', '群友', (), old_generation)
                assert memory.archive('group:1', 'catty') == []
                print('SURFCORE_REAL_PLUGIN_WIRING_OK')
            asyncio.run(main())
        ''')
        allowed = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env["PYTHONUTF8"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-c", script, str(ROOT)],
                cwd=directory, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SURFCORE_REAL_PLUGIN_WIRING_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
