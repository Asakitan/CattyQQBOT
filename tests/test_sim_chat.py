from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import types
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "src" / "catty_qq_ai" / "catty_sim_chat.py"
_PACKAGE_NAME = "_sim_chat_lock_test_package"
_MODULE_NAME = f"{_PACKAGE_NAME}.catty_sim_chat"


class _TraceLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.attempted = asyncio.Event()
        self.waiting = asyncio.Event()
        self.acquire_count = 0
        self.release_count = 0

    async def acquire(self) -> bool:
        self.attempted.set()
        if self._lock.locked():
            self.waiting.set()
        await self._lock.acquire()
        self.acquire_count += 1
        return True

    def release(self) -> None:
        self._lock.release()
        self.release_count += 1

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        self.release()
        return False


def _load_sim_chat_module():
    for name in (
        _MODULE_NAME,
        f"{_PACKAGE_NAME}.message_utils",
        f"{_PACKAGE_NAME}.openai_client",
        _PACKAGE_NAME,
    ):
        sys.modules.pop(name, None)

    package = types.ModuleType(_PACKAGE_NAME)
    package.__path__ = []
    sys.modules[_PACKAGE_NAME] = package
    intent = types.ModuleType(f"{_PACKAGE_NAME}.request_intent")
    intent.append_context_request_hint = lambda messages, *args, **kwargs: None
    sys.modules[intent.__name__] = intent
    package.request_intent = intent

    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return package, module


class _BuildFailure(RuntimeError):
    pass


class _SnapshotFailure(RuntimeError):
    pass


class SimChatLockingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.package, self.sim_module = _load_sim_chat_module()

    def tearDown(self) -> None:
        for name in (
            _MODULE_NAME,
            f"{_PACKAGE_NAME}.message_utils",
            f"{_PACKAGE_NAME}.openai_client",
            f"{_PACKAGE_NAME}.request_intent",
            _PACKAGE_NAME,
        ):
            sys.modules.pop(name, None)

    def _install_runtime(
        self,
        *,
        config: SimpleNamespace,
        conversation_locks: dict[str, _TraceLock],
        transition_gate: _TraceLock,
        on_build=None,
        on_provider=None,
    ) -> tuple[dict[str, list[object]], SimpleNamespace]:
        records: dict[str, list[object]] = {
            "history_config_scopes": [],
            "extracted_configs": [],
            "build_keys": [],
            "global_configs_at_build": [],
            "scope_at_build": [],
            "logical_turn_at_build": [],
            "session_at_build": [],
            "session_snapshot_keys": [],
            "global_configs_at_snapshot": [],
            "completion_configs": [],
            "appended_history": [],
            "global_configs_at_append": [],
        }
        context = SimpleNamespace(
            scope=contextvars.ContextVar("scope", default="outer-scope"),
            logical_turn=contextvars.ContextVar(
                "logical_turn",
                default={"logical_turn_id": "outer-turn"},
            ),
            session=contextvars.ContextVar("session", default={"history_key": "outer"}),
            persona=contextvars.ContextVar("persona", default="outer-persona"),
        )

        def build_history_key(event, current_config) -> str:
            scope = current_config.catty_group_history_scope
            records["history_config_scopes"].append(scope)
            if hasattr(event, "group_id"):
                if scope == "user":
                    return f"group:{event.group_id}:user:{event.user_id}"
                return f"group:{event.group_id}"
            return f"private:{event.user_id}"

        def extract_incoming_message(_account, event, current_config):
            records["extracted_configs"].append(current_config)
            return SimpleNamespace(
                text=event.text,
                history_content=event.text,
                image_urls=[],
                directly_requested=False,
            )

        message_utils = types.ModuleType(f"{_PACKAGE_NAME}.message_utils")
        message_utils.build_history_key = build_history_key
        message_utils.extract_incoming_message = extract_incoming_message
        sys.modules[message_utils.__name__] = message_utils
        self.package.message_utils = message_utils

        def set_current_scope_key(value):
            return context.scope.set(value)

        def reset_current_scope_key(token) -> None:
            context.scope.reset(token)

        def set_current_logical_turn(logical_turn_id: str | None = None):
            return context.logical_turn.set({
                "logical_turn_id": logical_turn_id or "sim-turn",
            })

        def reset_current_logical_turn(token) -> None:
            context.logical_turn.reset(token)

        def get_current_logical_turn():
            return context.logical_turn.get()

        def set_current_session_context(value):
            return context.session.set(value)

        def reset_current_session_context(token) -> None:
            context.session.reset(token)

        def set_current_persona_reply_context(value):
            return context.persona.set(value)

        def reset_current_persona_reply_context(token) -> None:
            context.persona.reset(token)

        async def chat_completion(current_config, _messages):
            records["completion_configs"].append(current_config)
            if on_provider is not None:
                await on_provider()
            return "simulated reply"

        async def chat_completion_with_tools(current_config, messages, **_kwargs):
            return await chat_completion(current_config, messages)

        openai_client = types.ModuleType(f"{_PACKAGE_NAME}.openai_client")
        openai_client.chat_completion = chat_completion
        openai_client.chat_completion_with_tools = chat_completion_with_tools
        openai_client.get_current_logical_turn = get_current_logical_turn
        openai_client.reset_current_logical_turn = reset_current_logical_turn
        openai_client.reset_current_persona_reply_context = reset_current_persona_reply_context
        openai_client.reset_current_session_context = reset_current_session_context
        openai_client.reset_current_scope_key = reset_current_scope_key
        openai_client.set_current_logical_turn = set_current_logical_turn
        openai_client.set_current_persona_reply_context = set_current_persona_reply_context
        openai_client.set_current_session_context = set_current_session_context
        openai_client.set_current_scope_key = set_current_scope_key
        sys.modules[openai_client.__name__] = openai_client
        self.package.openai_client = openai_client

        async def build_messages(event, key, incoming):
            records["build_keys"].append(key)
            records["global_configs_at_build"].append(self.package.config)
            records["scope_at_build"].append(context.scope.get())
            records["logical_turn_at_build"].append(context.logical_turn.get())
            records["session_at_build"].append(context.session.get())
            if on_build is not None:
                await on_build(event, key, incoming)
            return [{"role": "user", "content": "prompt"}], False

        def session_context_snapshot(key: str) -> dict[str, str]:
            records["session_snapshot_keys"].append(key)
            records["global_configs_at_snapshot"].append(self.package.config)
            return {"history_key": key}

        def append_history(key: str, incoming_text: str, reply: str) -> None:
            records["appended_history"].append((key, incoming_text, reply))
            records["global_configs_at_append"].append(self.package.config)

        self.package._build_messages = build_messages
        self.package._conversation_locks = conversation_locks
        self.package._persona_config_transition_lock = transition_gate
        self.package._persona_reply_context_for_event = lambda _event: SimpleNamespace(
            persona=SimpleNamespace(),
        )
        self.package._session_context_snapshot = session_context_snapshot
        self.package._append_history = append_history
        # sim_chat(live=True) imports these from the parent package at call time.
        self.package._persona_for_event = lambda _event: SimpleNamespace(name="catty")
        self.package._strip_catty_markers = lambda text, keep=None: str(text or "")
        self.package._finalize_fadianji_reply = (
            lambda _event, text, **kwargs: str(text or "")
        )
        self.package.config = config

        self.sim_module._make_mock_group_event = (
            lambda user_id, group_id, text, **_kwargs: SimpleNamespace(
                user_id=user_id,
                group_id=group_id,
                text=text,
            )
        )
        self.sim_module._make_mock_private_event = (
            lambda user_id, text, **_kwargs: SimpleNamespace(
                user_id=user_id,
                text=text,
            )
        )
        return records, context

    async def test_admission_keeps_group_key_and_config_snapshot_through_transition(self) -> None:
        old_config = SimpleNamespace(
            qq_account="0",
            catty_group_history_scope="group",
            catty_test_providers={},
        )
        new_config = SimpleNamespace(
            qq_account="0",
            catty_group_history_scope="user",
            catty_test_providers={},
        )
        old_key = "group:100"
        new_key = "group:100:user:7"
        old_lock = _TraceLock()
        new_lock = _TraceLock()
        transition_gate = _TraceLock()
        config_changed = asyncio.Event()
        build_started = asyncio.Event()
        release_build = asyncio.Event()
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()

        async def hold_build(_event, _key, _incoming) -> None:
            build_started.set()
            await release_build.wait()

        async def hold_provider() -> None:
            provider_started.set()
            await release_provider.wait()

        records, context = self._install_runtime(
            config=old_config,
            conversation_locks={old_key: old_lock, new_key: new_lock},
            transition_gate=transition_gate,
            on_build=hold_build,
            on_provider=hold_provider,
        )

        await old_lock.acquire()
        held_by_test = True
        simulation_task = asyncio.create_task(
            self.sim_module.sim_chat(
                text="hello",
                user_id=7,
                group_id=100,
                live=True,
                with_tools=False,
                persist=True,
                full_context=False,
            )
        )
        transition_task = None
        try:
            await old_lock.waiting.wait()
            transition_gate.attempted.clear()

            async def transition_config() -> None:
                async with transition_gate:
                    self.package.config = new_config
                    config_changed.set()

            transition_task = asyncio.create_task(transition_config())
            await transition_gate.attempted.wait()
            self.assertTrue(transition_gate.waiting.is_set())
            self.assertFalse(config_changed.is_set())
            self.assertFalse(transition_task.done())
            self.assertIs(self.package.config, old_config)

            old_lock.release()
            held_by_test = False
            await asyncio.wait_for(build_started.wait(), timeout=1)
            self.assertFalse(config_changed.is_set())
            self.assertFalse(transition_task.done())
            self.assertIs(self.package.config, old_config)
            release_build.set()
            await asyncio.wait_for(provider_started.wait(), timeout=1)
            await asyncio.sleep(0)
            self.assertFalse(config_changed.is_set())
            self.assertFalse(transition_task.done())
            self.assertIs(self.package.config, old_config)
            release_provider.set()
            result = await asyncio.wait_for(simulation_task, timeout=1)
            await asyncio.wait_for(transition_task, timeout=1)
        finally:
            if held_by_test:
                old_lock.release()
            for task in (simulation_task, transition_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (simulation_task, transition_task):
                if task is not None:
                    with contextlib.suppress(BaseException):
                        await task

        self.assertEqual(result["reply"], "simulated reply")
        self.assertEqual(records["history_config_scopes"], ["group"])
        self.assertEqual(records["extracted_configs"], [old_config])
        self.assertEqual(records["build_keys"], [old_key])
        self.assertEqual(records["global_configs_at_build"], [old_config])
        self.assertEqual(records["scope_at_build"], [old_key])
        self.assertEqual(
            records["logical_turn_at_build"],
            [{"logical_turn_id": "sim-turn"}],
        )
        self.assertEqual(records["session_at_build"], [{"history_key": old_key}])
        self.assertEqual(records["session_snapshot_keys"], [old_key, old_key])
        self.assertEqual(
            records["global_configs_at_snapshot"],
            [old_config, old_config],
        )
        self.assertEqual(records["completion_configs"], [old_config])
        self.assertEqual(
            records["appended_history"],
            [(old_key, "prompt", "simulated reply")],
        )
        self.assertEqual(records["global_configs_at_append"], [old_config])
        self.assertIs(self.package.config, new_config)
        self.assertEqual(context.scope.get(), "outer-scope")
        self.assertEqual(context.logical_turn.get(), {"logical_turn_id": "outer-turn"})
        self.assertEqual(context.session.get(), {"history_key": "outer"})
        self.assertFalse(old_lock.locked())
        self.assertEqual(old_lock.acquire_count, 2)
        self.assertEqual(old_lock.release_count, 2)
        self.assertEqual(new_lock.acquire_count, 0)

    async def test_build_exception_releases_lock_and_restores_request_contexts(self) -> None:
        config = SimpleNamespace(
            qq_account="0",
            catty_group_history_scope="group",
            catty_test_providers={},
        )
        key = "group:100"
        conversation_lock = _TraceLock()
        transition_gate = _TraceLock()

        async def raise_build_failure(_event, _key, _incoming) -> None:
            raise _BuildFailure("build failed")

        records, context = self._install_runtime(
            config=config,
            conversation_locks={key: conversation_lock},
            transition_gate=transition_gate,
            on_build=raise_build_failure,
        )

        with self.assertRaisesRegex(_BuildFailure, "build failed"):
            await self.sim_module.sim_chat(
                text="hello",
                user_id=7,
                group_id=100,
                live=False,
                full_context=False,
            )

        self.assertEqual(records["history_config_scopes"], ["group"])
        self.assertEqual(records["extracted_configs"], [config])
        self.assertEqual(records["build_keys"], [key])
        self.assertEqual(records["scope_at_build"], [key])
        self.assertEqual(
            records["logical_turn_at_build"],
            [{"logical_turn_id": "sim-turn"}],
        )
        self.assertEqual(records["session_at_build"], [{"history_key": key}])
        self.assertEqual(context.scope.get(), "outer-scope")
        self.assertEqual(context.logical_turn.get(), {"logical_turn_id": "outer-turn"})
        self.assertEqual(context.session.get(), {"history_key": "outer"})
        self.assertFalse(conversation_lock.locked())
        self.assertFalse(transition_gate.locked())
        self.assertEqual(conversation_lock.acquire_count, 1)
        self.assertEqual(conversation_lock.release_count, 1)

    async def test_session_snapshot_failure_restores_partial_build_contexts(self) -> None:
        config = SimpleNamespace(
            qq_account="0",
            catty_group_history_scope="group",
            catty_test_providers={},
        )
        key = "group:100"
        conversation_lock = _TraceLock()
        transition_gate = _TraceLock()
        _records, context = self._install_runtime(
            config=config,
            conversation_locks={key: conversation_lock},
            transition_gate=transition_gate,
        )

        def fail_snapshot(_key: str):
            raise _SnapshotFailure("snapshot failed")

        self.package._session_context_snapshot = fail_snapshot
        with self.assertRaisesRegex(_SnapshotFailure, "snapshot failed"):
            await self.sim_module.sim_chat(
                text="hello",
                user_id=7,
                group_id=100,
                live=False,
                full_context=False,
            )

        self.assertEqual(context.scope.get(), "outer-scope")
        self.assertEqual(context.logical_turn.get(), {"logical_turn_id": "outer-turn"})
        self.assertEqual(context.session.get(), {"history_key": "outer"})
        self.assertFalse(conversation_lock.locked())
        self.assertFalse(transition_gate.locked())

    async def test_live_provider_failure_restores_all_contexts_and_locks(self) -> None:
        config = SimpleNamespace(
            qq_account="0",
            catty_group_history_scope="group",
            catty_test_providers={},
        )
        key = "group:100"
        conversation_lock = _TraceLock()
        transition_gate = _TraceLock()
        _records, context = self._install_runtime(
            config=config,
            conversation_locks={key: conversation_lock},
            transition_gate=transition_gate,
        )

        async def fail_provider(_config, _messages):
            raise RuntimeError("provider failed")

        openai_client = sys.modules[f"{_PACKAGE_NAME}.openai_client"]
        openai_client.chat_completion = fail_provider
        result = await self.sim_module.sim_chat(
            text="hello",
            user_id=7,
            group_id=100,
            live=True,
            with_tools=False,
            full_context=False,
        )

        self.assertIn("provider failed", result["reply"])
        self.assertEqual(context.scope.get(), "outer-scope")
        self.assertEqual(context.logical_turn.get(), {"logical_turn_id": "outer-turn"})
        self.assertEqual(context.session.get(), {"history_key": "outer"})
        self.assertEqual(context.persona.get(), "outer-persona")
        self.assertFalse(conversation_lock.locked())
        self.assertFalse(transition_gate.locked())


if __name__ == "__main__":
    unittest.main()