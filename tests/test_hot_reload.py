from pathlib import Path
import ast
import asyncio
from collections import defaultdict
import copy
from functools import lru_cache
import importlib.util
import sys
import tempfile
from types import SimpleNamespace
import unittest


_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "catty_hot_reload.py"
_SPEC = importlib.util.spec_from_file_location("catty_hot_reload", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_hot_reload = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _hot_reload
_SPEC.loader.exec_module(_hot_reload)


_INIT_PATH = Path(__file__).resolve().parents[1] / "src" / "catty_qq_ai" / "__init__.py"
_OPENAI_CLIENT_PATH = _INIT_PATH.with_name("openai_client.py")


@lru_cache(maxsize=4)
def _source_tree(path: Path) -> ast.Module:
    # One immutable disk snapshot per subprocess; callers only mutate deep copies.
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _source_async_function(name: str) -> ast.AsyncFunctionDef:
    tree = _source_tree(_INIT_PATH)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing async function: {name}")


def _source_function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = _source_tree(path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing function: {name}")


class _StripRelativeImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.level:
            return ast.copy_location(ast.Pass(), node)
        return node


def _load_isolated_function(
    path: Path,
    name: str,
    namespace: dict[str, object],
    *,
    strip_relative_imports: bool = False,
):
    function = copy.deepcopy(_source_function(path, name))
    if strip_relative_imports:
        function = _StripRelativeImports().visit(function)
        assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    runtime: dict[str, object] = {"__name__": "catty_isolated_function", "Any": object}
    runtime.update(namespace)
    exec(compile(module, str(path), "exec"), runtime)
    return runtime[name]


class HotReloadIgnoredDataPathTests(unittest.TestCase):
    def test_fdj_scene_delta_is_ignored_by_process_supervisor(self) -> None:
        root = _MODULE_PATH.parent.parent
        path = root / "src" / "catty_qq_ai" / "data" / "fdj_scene_delta" / "manifest.json"
        self.assertTrue(_hot_reload.is_ignored_path(path, root))
        self.assertIn(
            "src/catty_qq_ai/data/fdj_scene_delta",
            _hot_reload.IGNORED_PATH_PREFIXES,
        )


def _assigns_name(statement: ast.stmt, name: str) -> bool:
    if isinstance(statement, ast.Assign):
        return any(
            isinstance(target, ast.Name) and target.id == name
            for target in statement.targets
        )
    if isinstance(statement, ast.AnnAssign):
        return isinstance(statement.target, ast.Name) and statement.target.id == name
    return False


def _is_async_exit_stack(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.AsyncWith):
        return False
    return any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == "AsyncExitStack"
        for item in statement.items
    )


def _handle_chat_admission_stack(handle: ast.AsyncFunctionDef) -> ast.AsyncWith:
    for statement in handle.body:
        if _is_async_exit_stack(statement):
            assert isinstance(statement, ast.AsyncWith)
            return statement
    raise AssertionError("handle_chat admission AsyncExitStack not found")


def _global_admission(stack: ast.AsyncWith) -> tuple[int, ast.AsyncWith]:
    matches = [
        (index, statement)
        for index, statement in enumerate(stack.body)
        if isinstance(statement, ast.AsyncWith)
        and any(isinstance(item.context_expr, ast.Name)
                and item.context_expr.id == "_persona_config_transition_lock"
                for item in statement.items)
    ]
    if len(matches) != 1:
        raise AssertionError("expected one global admission gate")
    return matches[0]


def _wire_active_group_counter(namespace: dict[str, object]) -> None:
    namespace.setdefault("_active_group_replies", defaultdict(int))
    namespace["_release_active_group_reply"] = _load_isolated_function(
        _INIT_PATH, "_release_active_group_reply", namespace,
    )


def _load_handle_chat_admission(namespace: dict[str, object]):
    _wire_active_group_counter(namespace)
    handle = _source_async_function("handle_chat")
    stack_index = next(
        index
        for index, statement in enumerate(handle.body)
        if _is_async_exit_stack(statement)
    )
    transition_scope_index = next(
        index
        for index, statement in enumerate(handle.body)
        if _assigns_name(statement, "transition_scope")
    )
    admission_stack = copy.deepcopy(handle.body[stack_index])
    assert isinstance(admission_stack, ast.AsyncWith)
    admission_body: list[ast.stmt] = []
    for statement in admission_stack.body:
        if _assigns_name(statement, "queue_wait_seconds"):
            break
        admission_body.append(statement)
    else:
        raise AssertionError("handle_chat admission end not found")
    admission_stack.body = admission_body

    module = ast.parse(
        "async def _admit(event):\n"
        "    queue_key = _conversation_queue_key(event)\n"
        "    return queue_was_busy\n"
    )
    admission = module.body[0]
    assert isinstance(admission, ast.AsyncFunctionDef)
    admission.body = [
        admission.body[0],
        *[
            copy.deepcopy(statement)
            for statement in handle.body[transition_scope_index:stack_index]
        ],
        admission_stack,
        admission.body[-1],
    ]
    ast.fix_missing_locations(module)
    exec(compile(module, str(_INIT_PATH), "exec"), namespace)
    return namespace["_admit"]


def _load_handle_chat_lock_lifecycle(namespace: dict[str, object]):
    _wire_active_group_counter(namespace)
    handle = _source_async_function("handle_chat")
    stack_index = next(
        index
        for index, statement in enumerate(handle.body)
        if _is_async_exit_stack(statement)
    )
    transition_scope_index = next(
        index
        for index, statement in enumerate(handle.body)
        if _assigns_name(statement, "transition_scope")
    )
    admission_stack = copy.deepcopy(handle.body[stack_index])
    assert isinstance(admission_stack, ast.AsyncWith)
    admission_body: list[ast.stmt] = []
    for statement in admission_stack.body:
        if _assigns_name(statement, "queue_wait_seconds"):
            break
        admission_body.append(statement)
    else:
        raise AssertionError("handle_chat admission end not found")

    lifecycle_module = ast.parse(
        "async def _run_lifecycle(event, on_history, on_prompt, on_provider, on_append):\n"
        "    queue_key = _conversation_queue_key(event)\n"
    )
    lifecycle = lifecycle_module.body[0]
    assert isinstance(lifecycle, ast.AsyncFunctionDef)
    lifecycle_steps = ast.parse(
        "async def _steps():\n"
        "    await on_history()\n"
        "    await on_prompt()\n"
        "    await on_provider()\n"
        "    await on_append()\n"
    ).body[0]
    assert isinstance(lifecycle_steps, ast.AsyncFunctionDef)
    admission_stack.body = [*admission_body, *lifecycle_steps.body]
    lifecycle.body.extend(
        [
            *[
                copy.deepcopy(statement)
                for statement in handle.body[transition_scope_index:stack_index]
            ],
            admission_stack,
        ]
    )
    ast.fix_missing_locations(lifecycle_module)
    exec(compile(lifecycle_module, str(_INIT_PATH), "exec"), namespace)
    return namespace["_run_lifecycle"]


def _message_method_call(
    statement: ast.stmt,
    method: str,
    *,
    argument_name: str | None = None,
) -> bool:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    if not isinstance(call.func, ast.Attribute) or call.func.attr != method:
        return False
    if not isinstance(call.func.value, ast.Name) or call.func.value.id != "messages":
        return False
    return (
        argument_name is None
        or (
            bool(call.args)
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == argument_name
        )
    )


def _messages_append_role(statement: ast.stmt, role: str) -> bool:
    if not _message_method_call(statement, "append"):
        return False
    assert isinstance(statement, ast.Expr)
    assert isinstance(statement.value, ast.Call)
    if not statement.value.args or not isinstance(statement.value.args[0], ast.Dict):
        return False
    message = statement.value.args[0]
    return any(
        isinstance(key, ast.Constant)
        and key.value == "role"
        and isinstance(value, ast.Constant)
        and value.value == role
        for key, value in zip(message.keys, message.values)
    )


def _build_messages_session_wiring_statements() -> list[ast.stmt]:
    source = _INIT_PATH.read_text(encoding="utf-8")
    statements = _source_async_function("_build_messages").body
    trim_index = next(
        index
        for index, statement in enumerate(statements)
        if isinstance(statement, ast.Try)
        and "monotonic_history_trim" in (ast.get_source_segment(source, statement) or "")
    )
    marker_index = next(
        index
        for index in range(trim_index + 1, len(statements))
        if isinstance(statements[index], ast.If)
        and "_SESSION_HISTORY_MARKER"
        in (ast.get_source_segment(source, statements[index]) or "")
    )
    history_extend_index = next(
        index
        for index in range(marker_index + 1, len(statements))
        if _message_method_call(statements[index], "extend", argument_name="history_messages")
    )
    user_content_index = next(
        index
        for index in range(history_extend_index + 1, len(statements))
        if _assigns_name(statements[index], "_user_content_raw")
    )
    user_append_index = next(
        index
        for index in range(user_content_index + 1, len(statements))
        if _messages_append_role(statements[index], "user")
    )
    dynamic_system_index = next(
        index
        for index in range(user_append_index + 1, len(statements))
        if isinstance(statements[index], ast.If)
        and isinstance(statements[index].test, ast.Name)
        and statements[index].test.id == "_post_boundary"
    )
    return [
        copy.deepcopy(statements[index])
        for index in (
            trim_index,
            marker_index,
            history_extend_index,
            user_content_index,
            user_append_index,
            dynamic_system_index,
        )
    ]


def _load_build_messages_session_wiring(namespace: dict[str, object]):
    module = ast.parse(
        "async def _wire_session_messages(history_messages, incoming, post_boundary):\n"
        "    messages = []\n"
        "    _is_private_event = True\n"
        "    key = 'private:1'\n"
        "    image_description = None\n"
        "    image_parts = None\n"
        "    quoted_image_parts = None\n"
        "    context_image_parts = None\n"
        "    context_image_notes = None\n"
        "    web_image_parts = None\n"
        "    _post_boundary = post_boundary\n"
        "    _pending_persona_reminder_note = None\n"
    )
    wire = module.body[0]
    assert isinstance(wire, ast.AsyncFunctionDef)
    wire.body.extend(_build_messages_session_wiring_statements())
    wire.body.append(ast.Return(value=ast.Name(id="messages", ctx=ast.Load())))
    ast.fix_missing_locations(module)
    exec(compile(module, str(_INIT_PATH), "exec"), namespace)
    return namespace["_wire_session_messages"]


class _NoopLogger:
    def debug(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass


class _RecordingAsyncLock:
    def __init__(self, label: str, events: list[str]) -> None:
        self._label = label
        self._events = events
        self._locked = False

    async def acquire(self) -> bool:
        self._events.append(f"{self._label}:acquire")
        self._locked = True
        return True

    def release(self) -> None:
        self._events.append(f"{self._label}:release")
        self._locked = False

    def locked(self) -> bool:
        return self._locked

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args) -> None:
        self.release()


class _FakeHistoryCache:
    def __init__(self, history: list[dict[str, object]], metadata: dict[str, object]) -> None:
        self.history = list(history)
        self.metadata = dict(metadata)
        self.metadata_writes: list[dict[str, object]] = []

    def get(self, key: str) -> list[dict[str, object]]:
        return list(self.history)

    def get_metadata(self, key: str) -> dict[str, object]:
        return dict(self.metadata)

    def set(self, key: str, history: list[dict[str, object]]) -> None:
        self.history = list(history)

    def update_metadata(self, key: str, **metadata: object) -> None:
        self.metadata.update(metadata)
        self.metadata_writes.append(dict(metadata))

    def flush_sync(self) -> bool:
        return True


def _load_runtime_config_helpers(*names: str, extra_globals: dict[str, object]) -> dict[str, object]:
    source = _INIT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [name for name in names if name not in functions]
    if missing:
        raise AssertionError(f"missing runtime config helper(s): {missing}")
    module = ast.Module(
        body=[copy.deepcopy(functions[name]) for name in names],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {
        "__name__": "catty_hot_reload_runtime_helpers",
        "Config": object,
        "Any": object,
        "MessageEvent": object,
        "asyncio": asyncio,
    }
    namespace.update(extra_globals)
    exec(compile(module, str(_INIT_PATH), "exec"), namespace)
    return namespace


class _FakeSessionCache:
    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)

    def list_sessions(self):
        return [(key, 1, 0.0) for key in self._keys]


class _FakeBucketStore:
    def __init__(self, scopes: list[str]) -> None:
        self._scopes = list(scopes)

    def known_scopes(self, prefix: str = "") -> list[str]:
        return [scope for scope in self._scopes if scope.startswith(prefix)]


class _FakeOverrideStore:
    def __init__(self, overrides: dict[str, str]) -> None:
        self._overrides = dict(overrides)

    def all_overrides(self) -> dict[str, str]:
        return dict(self._overrides)


def _resolve_persona(scope: str, current_config, override_store: _FakeOverrideStore) -> str:
    override = override_store.all_overrides().get(scope)
    if override:
        return override
    if scope.startswith("group:"):
        group_id = scope.split(":", 2)[1]
        mapped = (getattr(current_config, "catty_group_personas", {}) or {}).get(group_id)
        if mapped:
            return mapped
    return getattr(current_config, "catty_default_persona", "catty")


def _make_runtime_config_transition(
    old_config,
    *,
    session_keys: list[str],
    bucket_scopes: list[str],
    overrides: dict[str, str],
):
    cache = _FakeSessionCache(session_keys)
    buckets = _FakeBucketStore(bucket_scopes)
    override_store = _FakeOverrideStore(overrides)
    state: dict[str, object] = {
        "applied": [],
        "cleared": [],
        "baselines": [],
    }
    namespace = _load_runtime_config_helpers(
        "_base_persona_scope",
        "_persona_baseline_scope",
        "_configured_group_concurrency",
        "_known_persona_base_scopes",
        "_persona_scopes_changed_by_config",
        "_enter_persona_transition_lock",
        "_transition_runtime_config",
        extra_globals={
            "config": old_config,
            "logger": _NoopLogger(),
            "_session_persona_names": {},
            "_load_session_persona_baselines": lambda **_kwargs: None,
            "_get_session_cache": lambda: cache,
            "_get_time_bucket_context_store": lambda: buckets,
            "persona_override_store": override_store,
            "_group_concurrency_semas": {},
            "_group_concurrency_sema_capacities": {},
            "_persona_transition_locks": defaultdict(asyncio.Lock),
            "_persona_config_transition_lock": asyncio.Lock(),
            "_locks": defaultdict(asyncio.Lock),
            "_user_in_scope_locks": defaultdict(asyncio.Lock),
        },
    )

    namespace["_effective_persona_name_for_config"] = (
        lambda scope, current_config: _resolve_persona(scope, current_config, override_store)
    )

    def _apply(new_config) -> None:
        namespace["config"] = new_config
        state["applied"].append(new_config)

    def _clear(scope: str) -> int:
        state["cleared"].append(scope)
        return 0

    def _record(personas: dict[str, str]) -> None:
        state["baselines"].append(dict(personas))

    namespace["_apply_runtime_config"] = _apply
    namespace["_clear_persona_runtime_state"] = _clear
    namespace["_record_session_persona_baselines"] = _record
    return namespace, state


class FadianjiCognitiveWiringTests(unittest.TestCase):
    def test_unified_cognitive_context_replaces_scene_only_wiring(self) -> None:
        build_messages = _source_async_function("_build_messages")
        old_scene_calls = [
            node
            for node in ast.walk(build_messages)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_scene_reference_block"
        ]
        self.assertEqual(old_scene_calls, [])
        to_thread_calls = [
            node
            for node in ast.walk(build_messages)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and node.func.attr == "to_thread"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "_build_cognitive_turn_context_sync"
        ]
        self.assertEqual(len(to_thread_calls), 1)


class HotReloadWatcherTests(unittest.TestCase):
    def test_ignored_directories_are_not_watched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "catty.py").write_text("print('ok')", encoding="utf-8")
            (root / ".venv" / "Lib").mkdir(parents=True)
            (root / ".venv" / "Lib" / "ignored.py").write_text("print('no')", encoding="utf-8")
            (root / "build").mkdir()
            (root / "build" / "ignored.py").write_text("print('no')", encoding="utf-8")

            watched = {path.relative_to(root).as_posix() for path in _hot_reload.iter_watch_files(root)}

        self.assertIn("src/catty.py", watched)
        self.assertNotIn(".venv/Lib/ignored.py", watched)
        self.assertNotIn("build/ignored.py", watched)

    def test_changed_files_reports_modified_files(self) -> None:
        before = {"src/catty.py": (1, 10), "README.md": (1, 20)}
        after = {"src/catty.py": (2, 10), "README.md": (1, 20)}

        self.assertEqual(_hot_reload.changed_files(before, after), ["src/catty.py"])

    def test_ignored_data_prefix_and_pycache_are_pruned_from_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "catty_qq_ai"
            (package / "data" / "cpu_engine" / "routes").mkdir(parents=True)
            (package / "data" / "cpu_engine" / "routes" / "a.yaml").write_text("x: 1", encoding="utf-8")
            (package / "__pycache__").mkdir()
            (package / "__pycache__" / "m.py").write_text("", encoding="utf-8")
            (package / "core.py").write_text("print('ok')", encoding="utf-8")
            (package / "notes.txt").write_text("not watched", encoding="utf-8")
            (root / "bot.py").write_text("print('bot')", encoding="utf-8")

            snapshot = _hot_reload.snapshot_files(root)
            self.assertEqual(set(snapshot), {"bot.py", "src/catty_qq_ai/core.py"})

            (package / "core.py").write_text("print('changed!')", encoding="utf-8")
            after = _hot_reload.snapshot_files(root)
        self.assertEqual(_hot_reload.changed_files(snapshot, after), ["src/catty_qq_ai/core.py"])


class _FakeChild:
    def __init__(self, *, exits_on_signal: bool) -> None:
        self.exits_on_signal = exits_on_signal
        self.returncode = None
        self.signals: list[object] = []
        self.killed = False

    def poll(self):
        return self.returncode

    def send_signal(self, sig) -> None:
        self.signals.append(sig)
        if self.exits_on_signal:
            self.returncode = 0

    def wait(self, timeout=None):
        if self.returncode is None:
            raise _hot_reload.subprocess.TimeoutExpired("bot.py", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1


class HotReloadSupervisorTests(unittest.TestCase):
    def test_stop_requests_graceful_exit_before_killing(self) -> None:
        child = _FakeChild(exits_on_signal=True)
        _hot_reload._stop_bot(child, timeout=0.1)
        expected = (
            _hot_reload.signal.CTRL_BREAK_EVENT
            if _hot_reload.os.name == "nt"
            else _hot_reload.signal.SIGTERM
        )
        self.assertEqual(child.signals, [expected])
        self.assertFalse(child.killed)

    def test_stop_kills_after_graceful_timeout(self) -> None:
        child = _FakeChild(exits_on_signal=False)
        _hot_reload._stop_bot(child, timeout=0.01)
        self.assertTrue(child.killed)

    def test_stop_kills_immediately_when_signal_cannot_be_sent(self) -> None:
        child = _FakeChild(exits_on_signal=False)
        waits: list[object] = []

        def no_console(_sig) -> None:
            raise OSError("no console attached")

        def record_wait(timeout=None):
            waits.append(timeout)
            return _FakeChild.wait(child, timeout)

        child.send_signal = no_console
        child.wait = record_wait
        _hot_reload._stop_bot(child, timeout=30.0)
        self.assertTrue(child.killed)
        self.assertEqual(waits[0], 0.0)

    def test_heartbeat_only_counts_beats_written_by_the_current_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot_heartbeat"
            monitor = _hot_reload.HeartbeatMonitor(path, started_at=1_000.0)
            self.assertFalse(monitor.is_stale(now=5_000.0, timeout=180.0))  # 还没写过
            path.write_text("1", encoding="utf-8")
            _hot_reload.os.utime(path, (900.0, 900.0))  # 上一轮残留
            self.assertFalse(monitor.is_stale(now=5_000.0, timeout=180.0))
            _hot_reload.os.utime(path, (1_100.0, 1_100.0))
            self.assertFalse(monitor.is_stale(now=1_200.0, timeout=180.0))
            self.assertTrue(monitor.is_stale(now=1_400.0, timeout=180.0))
            self.assertFalse(monitor.is_stale(now=1_400.0, timeout=0.0))

    def test_crash_backoff_grows_and_caps(self) -> None:
        delays = [_hot_reload.crash_backoff_seconds(streak) for streak in range(8)]
        self.assertEqual(delays[0], _hot_reload.CRASH_BACKOFF_SECONDS[0])
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[-1], _hot_reload.CRASH_BACKOFF_SECONDS[-1])


class RuntimePersonaConfigTransitionTests(unittest.TestCase):
    def test_group_mapping_and_default_changes_only_clear_affected_known_scopes(self) -> None:
        old_config = SimpleNamespace(
            catty_default_persona="catty",
            catty_group_personas={"100": "fadianji"},
            catty_reply_group_concurrency=2,
        )
        new_config = SimpleNamespace(
            catty_default_persona="fadianji",
            catty_group_personas={"100": "fadianji", "101": "catty", "102": "fadianji"},
            catty_reply_group_concurrency=2,
        )
        runtime, state = _make_runtime_config_transition(
            old_config,
            session_keys=[
                "group:100:user:1",
                "group:101",
                "group:103",
                "private:7",
            ],
            bucket_scopes=["group:104:user:8"],
            overrides={"group:105": "catty"},
        )
        expected = {"group:102", "group:103", "group:104:user:8", "private:7"}

        self.assertEqual(
            runtime["_persona_scopes_changed_by_config"](old_config, new_config),
            expected,
        )
        self.assertEqual(asyncio.run(runtime["_transition_runtime_config"](new_config)), expected)
        self.assertEqual(set(state["cleared"]), expected)
        self.assertEqual(len(state["baselines"]), 1)
        baselines = state["baselines"][0]
        self.assertEqual(baselines["group:100"], "fadianji")
        self.assertEqual(baselines["group:100:user:1"], "fadianji")
        self.assertEqual(baselines["group:101"], "catty")
        self.assertEqual(baselines["group:105"], "catty")

    def test_group_transition_waits_until_all_existing_semaphore_slots_are_free(self) -> None:
        old_config = SimpleNamespace(
            catty_default_persona="catty",
            catty_group_personas={},
            catty_reply_group_concurrency=2,
        )
        new_config = SimpleNamespace(
            catty_default_persona="fadianji",
            catty_group_personas={},
            catty_reply_group_concurrency=2,
        )
        runtime, state = _make_runtime_config_transition(
            old_config,
            session_keys=["group:100"],
            bucket_scopes=[],
            overrides={},
        )

        async def scenario() -> None:
            semaphore = asyncio.Semaphore(2)
            await semaphore.acquire()
            runtime["_group_concurrency_semas"]["group:100"] = semaphore
            runtime["_group_concurrency_sema_capacities"]["group:100"] = 2
            transition_gate = runtime["_persona_transition_locks"]["group:100"]
            task = asyncio.create_task(runtime["_transition_runtime_config"](new_config))
            await asyncio.sleep(0)
            self.assertEqual(state["applied"], [])
            self.assertTrue(transition_gate.locked())
            new_admission = asyncio.create_task(transition_gate.acquire())
            await asyncio.sleep(0)
            self.assertFalse(new_admission.done())
            semaphore.release()
            await task
            await new_admission
            transition_gate.release()

        asyncio.run(scenario())
        self.assertEqual(state["cleared"], ["group:100"])

    def test_private_transition_waits_for_the_same_user_lock_as_handle_chat(self) -> None:
        old_config = SimpleNamespace(
            catty_default_persona="catty",
            catty_group_personas={},
            catty_reply_group_concurrency=2,
        )
        new_config = SimpleNamespace(
            catty_default_persona="fadianji",
            catty_group_personas={},
            catty_reply_group_concurrency=2,
        )
        runtime, state = _make_runtime_config_transition(
            old_config,
            session_keys=["private:7"],
            bucket_scopes=[],
            overrides={},
        )

        async def scenario() -> None:
            user_lock = runtime["_user_in_scope_locks"]["private:7"]
            await user_lock.acquire()
            task = asyncio.create_task(runtime["_transition_runtime_config"](new_config))
            await asyncio.sleep(0)
            self.assertEqual(state["applied"], [])
            user_lock.release()
            await task

        asyncio.run(scenario())
        self.assertEqual(state["cleared"], ["private:7"])

    def test_group_semaphore_rebuilds_for_smaller_and_larger_configured_capacity(self) -> None:
        class _GroupEvent:
            group_id = 100

        def assert_rebuild(initial_capacity: int, target_capacity: int) -> None:
            current_config = SimpleNamespace(
                catty_reply_group_concurrency=initial_capacity,
            )
            runtime = _load_runtime_config_helpers(
                "_configured_group_concurrency",
                "_group_concurrency_sema_for",
                extra_globals={
                    "config": current_config,
                    "GroupMessageEvent": _GroupEvent,
                    "_group_concurrency_semas": {},
                    "_group_concurrency_sema_capacities": {},
                },
            )
            event = _GroupEvent()
            old_semaphore = runtime["_group_concurrency_sema_for"](event)
            self.assertIsNotNone(old_semaphore)
            self.assertEqual(
                runtime["_group_concurrency_sema_capacities"]["group:100"],
                initial_capacity,
            )

            current_config.catty_reply_group_concurrency = target_capacity
            new_semaphore = runtime["_group_concurrency_sema_for"](event)
            self.assertIsNot(old_semaphore, new_semaphore)
            self.assertEqual(
                runtime["_group_concurrency_sema_capacities"]["group:100"],
                target_capacity,
            )
            self.assertEqual(new_semaphore._value, target_capacity)

        assert_rebuild(3, 1)
        assert_rebuild(3, 5)

    def test_per_user_override_scope_does_not_promote_siblings_to_a_group_transition(self) -> None:
        old_config = SimpleNamespace(
            catty_default_persona="catty",
            catty_group_personas={},
            catty_reply_group_concurrency=2,
        )
        new_config = SimpleNamespace(
            catty_default_persona="fadianji",
            catty_group_personas={},
            catty_reply_group_concurrency=2,
        )
        runtime, state = _make_runtime_config_transition(
            old_config,
            session_keys=["group:100:user:1", "group:100:user:2"],
            bucket_scopes=[],
            overrides={"group:100:user:1": "catty"},
        )

        changed = asyncio.run(runtime["_transition_runtime_config"](new_config))

        self.assertEqual(changed, {"group:100:user:2"})
        self.assertEqual(state["cleared"], ["group:100:user:2"])
        self.assertEqual(
            state["baselines"][0]["group:100:user:1"],
            "catty",
        )
        self.assertEqual(
            state["baselines"][0]["group:100:user:2"],
            "fadianji",
        )

    def test_hot_reload_loop_awaits_the_runtime_transition(self) -> None:
        source = _INIT_PATH.read_text(encoding="utf-8")
        self.assertIn("reloaded = await _reload_runtime_config_from_path(config_path)", source)
        self.assertIn("if reloaded:", source)


class HandleChatAdmissionLockingTests(unittest.TestCase):
    def test_handle_chat_global_lock_scopes_only_admission_in_order(self) -> None:
        source = _INIT_PATH.read_text(encoding="utf-8")
        handle = _source_async_function("handle_chat")
        admission_stack = _handle_chat_admission_stack(handle)

        self.assertGreaterEqual(len(admission_stack.body), 2)
        gate_index, global_admission = _global_admission(admission_stack)
        self.assertIsInstance(global_admission, ast.AsyncWith)
        assert isinstance(global_admission, ast.AsyncWith)
        self.assertIsInstance(global_admission.items[0].context_expr, ast.Name)
        assert isinstance(global_admission.items[0].context_expr, ast.Name)
        self.assertEqual(
            global_admission.items[0].context_expr.id,
            "_persona_config_transition_lock",
        )
        self.assertTrue(_assigns_name(admission_stack.body[gate_index + 1], "queue_wait_seconds"))
        before_gate = admission_stack.body[:gate_index]
        self.assertEqual(len(before_gate), 1)
        self.assertIsInstance(before_gate[0], ast.If)
        self.assertFalse(any(isinstance(node, ast.Await) for node in ast.walk(before_gate[0])))
        busy_source = ast.get_source_segment(source, before_gate[0]) or ""
        self.assertIn("_active_group_replies[queue_key] += 1", busy_source)
        self.assertIn("_lock_stack.callback(_release_active_group_reply, queue_key)", busy_source)

        global_source = ast.get_source_segment(source, global_admission) or ""
        self.assertIn(
            "transition_lock = _persona_transition_locks[transition_scope]",
            global_source,
        )
        self.assertIn("group_sema = _group_concurrency_sema_for(event)", global_source)
        self.assertLess(
            global_source.index("await transition_lock.acquire()"),
            global_source.index("await _lock_stack.enter_async_context(user_lock)"),
        )
        self.assertLess(
            global_source.index("await _lock_stack.enter_async_context(user_lock)"),
            global_source.index("await _lock_stack.enter_async_context(group_sema)"),
        )
        self.assertLess(
            global_source.index("await _lock_stack.enter_async_context(user_lock)"),
            global_source.index("await _lock_stack.enter_async_context(group_lock)"),
        )

        prefix = "\n".join(
            source.splitlines()[handle.lineno - 1 : global_admission.lineno - 1]
        )
        self.assertNotIn("_persona_transition_locks[transition_scope]", prefix)
        self.assertNotIn("_group_concurrency_sema_for(event)", prefix)
        scope_context_calls = [
            node
            for node in ast.walk(handle)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_current_scope_key"
        ]
        self.assertTrue(scope_context_calls)
        self.assertGreater(
            min(node.lineno for node in scope_context_calls),
            global_admission.end_lineno,
        )

    def test_lazy_group_admission_waits_for_global_transition_before_semaphore_creation(self) -> None:
        old_config = SimpleNamespace(
            catty_default_persona="catty",
            catty_group_personas={},
            catty_reply_group_concurrency=2,
        )
        new_config = SimpleNamespace(
            catty_default_persona="catty",
            catty_group_personas={},
            catty_reply_group_concurrency=5,
        )
        runtime, state = _make_runtime_config_transition(
            old_config,
            session_keys=[],
            bucket_scopes=[],
            overrides={},
        )

        class _GroupEvent:
            group_id = 100
            user_id = 7

        runtime.update(
            {
                "GroupMessageEvent": _GroupEvent,
                "_conversation_queue_key": lambda event: f"group:{event.group_id}",
                "build_history_key": lambda event, config: f"group:{event.group_id}",
                "_conversation_locks": defaultdict(asyncio.Lock),
                "_user_in_scope_lock_key": (
                    lambda event: f"group:{event.group_id}:user:{event.user_id}"
                ),
                "time": SimpleNamespace(monotonic=lambda: 0.0),
            }
        )

        def group_concurrency_sema_for(event):
            key = f"group:{event.group_id}"
            capacity = int(runtime["config"].catty_reply_group_concurrency)
            capacities = runtime["_group_concurrency_sema_capacities"]
            semaphores = runtime["_group_concurrency_semas"]
            if capacities.get(key) != capacity or key not in semaphores:
                capacities[key] = capacity
                semaphores[key] = asyncio.Semaphore(capacity)
            return semaphores[key]

        runtime["_group_concurrency_sema_for"] = group_concurrency_sema_for
        admit = _load_handle_chat_admission(runtime)

        async def scenario() -> None:
            global_lock = runtime["_persona_config_transition_lock"]
            await global_lock.acquire()
            transition_task = asyncio.create_task(
                runtime["_transition_runtime_config"](new_config)
            )
            admission_task = asyncio.create_task(admit(_GroupEvent()))
            await asyncio.sleep(0)
            self.assertFalse(admission_task.done())
            self.assertNotIn("group:100", runtime["_group_concurrency_semas"])
            self.assertEqual(runtime["_active_group_replies"]["group:100"], 1)

            global_lock.release()
            await transition_task
            await admission_task
            self.assertEqual(dict(runtime["_active_group_replies"]), {})

        asyncio.run(scenario())
        self.assertEqual(state["applied"], [new_config])
        self.assertEqual(
            runtime["_group_concurrency_sema_capacities"]["group:100"],
            5,
        )
        self.assertEqual(runtime["_group_concurrency_semas"]["group:100"]._value, 5)

    def test_multiscope_transition_keeps_global_gate_through_scope_selection(self) -> None:
        source = _INIT_PATH.read_text(encoding="utf-8")
        transition = _source_async_function("_transition_runtime_config")
        transition_source = ast.get_source_segment(source, transition) or ""
        global_enter = (
            "await lock_stack.enter_async_context(_persona_config_transition_lock)"
        )
        scope_loop = "for scope in sorted(transition_scopes):"
        apply_config = "_apply_runtime_config(new_config)"

        self.assertIn(global_enter, transition_source)
        self.assertIn(scope_loop, transition_source)
        self.assertIn(apply_config, transition_source)
        self.assertLess(transition_source.index(global_enter), transition_source.index(scope_loop))
        self.assertLess(transition_source.index(scope_loop), transition_source.index(apply_config))
        self.assertNotIn("_persona_config_transition_lock.release()", transition_source)
        self.assertNotIn("lock_stack.pop_all()", transition_source)

    def test_canonical_conversation_lock_encloses_history_provider_and_append_lifecycle(self) -> None:
        source = _INIT_PATH.read_text(encoding="utf-8")
        handle = _source_async_function("handle_chat")
        admission_stack = _handle_chat_admission_stack(handle)
        _, global_admission = _global_admission(admission_stack)
        self.assertIsInstance(global_admission, ast.AsyncWith)
        assert isinstance(global_admission, ast.AsyncWith)
        global_source = ast.get_source_segment(source, global_admission) or ""

        self.assertLess(
            global_source.index("history_key = build_history_key(event, config)"),
            global_source.index("conversation_lock = _conversation_locks[history_key]"),
        )
        self.assertLess(
            global_source.index("await transition_lock.acquire()"),
            global_source.index("await _lock_stack.enter_async_context(conversation_lock)"),
        )

        conversation_enter = next(
            node
            for node in ast.walk(global_admission)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "enter_async_context"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "conversation_lock"
        )
        build_call = next(
            node
            for node in ast.walk(handle)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_build_messages"
        )
        provider_call = next(
            node
            for node in ast.walk(handle)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"chat_completion_with_tools", "_main_reply_with_deadline"}
        )
        append_call = next(
            node
            for node in ast.walk(handle)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_append_history"
        )
        build_source = ast.get_source_segment(source, _source_async_function("_build_messages")) or ""
        self.assertIn("history_messages = list(_get_session_cache().get(key))", build_source)
        self.assertLess(conversation_enter.lineno, build_call.lineno)
        self.assertLess(build_call.lineno, provider_call.lineno)
        self.assertLess(provider_call.lineno, append_call.lineno)
        for lifecycle_call in (build_call, provider_call, append_call):
            self.assertGreater(lifecycle_call.lineno, global_admission.end_lineno or 0)
            self.assertLess(lifecycle_call.lineno, admission_stack.end_lineno or 0)

    def test_extracted_conversation_lock_lifecycle_releases_on_provider_error(self) -> None:
        class _PrivateEvent:
            pass

        class _ProviderFailure(Exception):
            pass

        def make_runtime(events: list[str]):
            gate = _RecordingAsyncLock("config-gate", events)
            transition = _RecordingAsyncLock("transition-gate", events)
            conversation = _RecordingAsyncLock("conversation", events)
            runtime: dict[str, object] = {
                "config": SimpleNamespace(),
                "time": SimpleNamespace(monotonic=lambda: 0.0),
                "GroupMessageEvent": type("GroupMessageEvent", (), {}),
                "_conversation_queue_key": lambda event: "queue:current",
                "_base_persona_scope": lambda key: "scope:current",
                "build_history_key": lambda event, config: "history:canonical",
                "_persona_config_transition_lock": gate,
                "_persona_transition_locks": defaultdict(lambda: transition),
                "_conversation_locks": defaultdict(lambda: conversation),
                "_user_in_scope_locks": defaultdict(asyncio.Lock),
                "_user_in_scope_lock_key": lambda event: "user:current",
                "_group_concurrency_sema_for": lambda event: None,
                "_locks": defaultdict(asyncio.Lock),
            }
            return _load_handle_chat_lock_lifecycle(runtime), conversation, runtime

        async def checked_stage(lock: _RecordingAsyncLock, events: list[str], name: str) -> None:
            self.assertTrue(lock.locked())
            events.append(name)

        success_events: list[str] = []
        run_success, success_lock, success_runtime = make_runtime(success_events)
        asyncio.run(
            run_success(
                _PrivateEvent(),
                lambda: checked_stage(success_lock, success_events, "history"),
                lambda: checked_stage(success_lock, success_events, "prompt"),
                lambda: checked_stage(success_lock, success_events, "provider"),
                lambda: checked_stage(success_lock, success_events, "append"),
            )
        )
        self.assertIn("history:canonical", success_runtime["_conversation_locks"])
        self.assertLess(success_events.index("config-gate:acquire"), success_events.index("conversation:acquire"))
        self.assertLess(success_events.index("conversation:acquire"), success_events.index("history"))
        self.assertLess(success_events.index("history"), success_events.index("prompt"))
        self.assertLess(success_events.index("prompt"), success_events.index("provider"))
        self.assertLess(success_events.index("provider"), success_events.index("append"))
        self.assertLess(success_events.index("append"), success_events.index("conversation:release"))
        self.assertFalse(success_lock.locked())

        failure_events: list[str] = []
        run_failure, failure_lock, _ = make_runtime(failure_events)

        async def failing_provider() -> None:
            self.assertTrue(failure_lock.locked())
            failure_events.append("provider")
            raise _ProviderFailure()

        with self.assertRaises(_ProviderFailure):
            asyncio.run(
                run_failure(
                    _PrivateEvent(),
                    lambda: checked_stage(failure_lock, failure_events, "history"),
                    lambda: checked_stage(failure_lock, failure_events, "prompt"),
                    failing_provider,
                    lambda: checked_stage(failure_lock, failure_events, "append"),
                )
            )
        self.assertIn("provider", failure_events)
        self.assertNotIn("append", failure_events)
        self.assertLess(failure_events.index("provider"), failure_events.index("conversation:release"))
        self.assertFalse(failure_lock.locked())


class SessionContextWiringTests(unittest.TestCase):
    def test_session_mode_marks_only_history_skips_legacy_trim_and_cleans_provider_payload(self) -> None:
        source = _INIT_PATH.read_text(encoding="utf-8")
        build = _source_async_function("_build_messages")
        trim_block = next(
            node
            for node in build.body
            if isinstance(node, ast.Try)
            and "monotonic_history_trim" in (ast.get_source_segment(source, node) or "")
        )
        trim_source = ast.get_source_segment(source, trim_block) or ""
        self.assertIn(
            "and not getattr(config, \"catty_session_context_enabled\", False)",
            trim_source,
        )

        marker = "_catty_session_history"
        runtime_config = SimpleNamespace(
            catty_prompt_compressor_enabled=True,
            catty_session_context_enabled=True,
            catty_session_context_target_tokens=256_000,
            catty_session_context_trim_to_tokens=192_000,
            catty_session_context_headroom_tokens=32_000,
            catty_model_context_tokens=1_000_000,
        )
        wire = _load_build_messages_session_wiring(
            {
                "config": runtime_config,
                "_SESSION_HISTORY_MARKER": marker,
                "_build_user_content": lambda incoming, **kwargs: "current user",
                "logger": _NoopLogger(),
            }
        )
        history = [
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old assistant"},
        ]
        messages = asyncio.run(
            wire(
                history,
                SimpleNamespace(),
                [{"role": "system", "content": "dynamic system"}],
            )
        )
        self.assertEqual(
            [message["role"] for message in messages],
            ["user", "assistant", "user", "system"],
        )
        self.assertTrue(all(message.get(marker) is True for message in messages[:2]))
        self.assertNotIn(marker, messages[2])
        self.assertNotIn(marker, messages[3])

        cleanup = _load_isolated_function(
            _OPENAI_CLIENT_PATH,
            "_prepare_session_context_payload",
            {
                "ChatMessage": dict,
                "_SESSION_HISTORY_MARKER": marker,
                "_get_runtime_config": lambda: runtime_config,
                "get_session_token_estimator_multiplier": lambda model: 1.0,
                "count_history_tokens": lambda entries: len(entries),
                "count_message_tokens": lambda message: 1,
                "count_tokens": lambda text: 0,
                "trim_history_to_token_budget": lambda entries, budget: list(entries),
                "get_current_session_context": lambda: {},
            },
            strip_relative_imports=True,
        )
        payload = {"messages": messages, "max_tokens": 0}
        cleanup(payload, model="test")
        self.assertEqual(
            payload["messages"],
            [
                {"role": "user", "content": "old user"},
                {"role": "assistant", "content": "old assistant"},
                {"role": "user", "content": "current user"},
                {"role": "system", "content": "dynamic system"},
            ],
        )
        self.assertTrue(all(marker not in message for message in payload["messages"]))


class SessionContextHistoryWatermarkTests(unittest.TestCase):
    def _append_history(
        self,
        cache: _FakeHistoryCache,
        *,
        count_history_tokens,
        trim_history_to_token_budget,
    ):
        return _load_isolated_function(
            _INIT_PATH,
            "_append_history",
            {
                "config": SimpleNamespace(
                    catty_session_context_enabled=True,
                    catty_session_context_target_tokens=256_000,
                    catty_session_context_trim_to_tokens=192_000,
                    catty_session_context_headroom_tokens=32_000,
                    catty_openai_model="test",
                ),
                "_get_session_cache": lambda: cache,
                "logger": _NoopLogger(),
                "time": SimpleNamespace(time=lambda: 123.0),
                "_get_time_bucket_context_store": lambda: SimpleNamespace(
                    record_turn=lambda *args, **kwargs: None
                ),
                "activity_feed": SimpleNamespace(
                    record_assistant_reply=lambda *args, **kwargs: None
                ),
                "count_history_tokens": count_history_tokens,
                "trim_history_to_token_budget": trim_history_to_token_budget,
                "get_current_model_override": lambda: None,
                "get_session_token_estimator_multiplier": lambda model: 1.0,
            },
            strip_relative_imports=True,
        )

    def test_high_watermark_is_strict_and_single_trim_metadata_persists(self) -> None:
        initial_history = [
            {"role": "user", "content": "old user"},
            {"role": "assistant", "content": "old assistant"},
        ]
        exact_cache = _FakeHistoryCache(
            initial_history,
            {"trim_epoch": 4, "trim_count": 9, "preserved": "yes"},
        )
        exact_trim_calls: list[int] = []
        append_exact = self._append_history(
            exact_cache,
            count_history_tokens=lambda history: 224_000,
            trim_history_to_token_budget=lambda history, budget: exact_trim_calls.append(budget),
        )
        append_exact("private:1", "new user", "new assistant")
        self.assertEqual(exact_trim_calls, [])
        self.assertEqual(exact_cache.metadata["history_tokens_estimate"], 224_000)
        self.assertEqual(exact_cache.metadata["trim_epoch"], 4)
        self.assertEqual(exact_cache.metadata["trim_count"], 9)
        self.assertEqual(exact_cache.metadata["preserved"], "yes")

        trigger_cache = _FakeHistoryCache(
            initial_history,
            {"trim_epoch": 4, "trim_count": 9, "preserved": "yes"},
        )
        count_values = iter((224_001, 192_000))
        count_calls: list[int] = []
        trim_calls: list[tuple[int, int]] = []

        def count_history_tokens(history) -> int:
            count_calls.append(len(history))
            return next(count_values)

        def trim_history_to_token_budget(history, budget):
            trim_calls.append((len(history), budget))
            return list(history[-2:])

        append_trigger = self._append_history(
            trigger_cache,
            count_history_tokens=count_history_tokens,
            trim_history_to_token_budget=trim_history_to_token_budget,
        )
        append_trigger("private:1", "new user", "new assistant")

        self.assertEqual(count_calls, [4, 2])
        self.assertEqual(trim_calls, [(4, 192_000)])
        self.assertEqual(
            trigger_cache.history,
            [
                {"role": "user", "content": "new user"},
                {"role": "assistant", "content": "new assistant"},
            ],
        )
        self.assertEqual(trigger_cache.metadata["history_tokens_estimate"], 192_000)
        self.assertEqual(trigger_cache.metadata["trim_epoch"], 5)
        self.assertEqual(trigger_cache.metadata["trim_count"], 10)
        self.assertEqual(trigger_cache.metadata["preserved"], "yes")
        self.assertEqual(
            trigger_cache.metadata_writes,
            [
                {
                    "history_tokens_estimate": 192_000,
                    "trim_epoch": 5,
                    "trim_count": 10,
                    "context_updated_at": 123.0,
                }
            ],
        )



if __name__ == "__main__":
    unittest.main()
