import ast
import asyncio
import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from unittest import mock


_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "catty_rag_async_test"
_PACKAGE = types.ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_ROOT / "src" / "catty_qq_ai")]
sys.modules[_PACKAGE_NAME] = _PACKAGE


def _load_module(name):
    path = _ROOT / "src" / "catty_qq_ai" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE_NAME}.{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prompt_manager = _load_module("prompt_manager")
catty_rag = _load_module("catty_rag")


def _manager(recall):
    manager = prompt_manager.PromptManager()
    manager.register_static("first", "first", order=100)
    manager.register("catty_rag_recall", recall, order=458)
    manager.register_static("last", "last", order=800)
    return manager


def _rag_prompt(query):
    store = catty_rag.CattyRAGStore.__new__(catty_rag.CattyRAGStore)
    store._enabled = True
    store._get_collection = lambda _scope: types.SimpleNamespace(query=query)
    return lambda: catty_rag.build_rag_recall_prompt(store, "group:1", "message", persona="catty")


def _query_result(text):
    return {"documents": [[text]], "metadatas": [[{"role": "user", "ts": 0}]], "distances": [[0.1]]}


class AsyncRagPromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [
            mock.patch.object(prompt_manager, "_TIKTOKEN_TRIED", True),
            mock.patch.object(prompt_manager, "_TIKTOKEN_ENC", None),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def test_recall_runs_off_loop_and_keeps_scope_persona_and_order(self):
        main_thread = threading.get_ident()
        calls = []

        class Store:
            enabled = True

            def query(self, scope, text, *, top_k, persona):
                calls.append((scope, text, top_k, persona, threading.get_ident()))
                return [(0.9, "same-persona memory", {"role": "assistant", "ts": 0})]

        store = Store()
        manager = _manager(lambda: catty_rag.build_rag_recall_prompt(
            store, "group:1", "current message", top_k=3, persona="fadianji",
        ))
        output = await manager.build_messages_async()
        self.assertEqual(calls[0][:4], ("group:1", "current message", 3, "fadianji"))
        self.assertNotEqual(calls[0][4], main_thread)
        self.assertEqual([item["content"] for item in output[::2]], ["first", "last"])
        self.assertIn("same-persona memory", output[1]["content"])
        self.assertEqual(len(calls), 1)

    async def test_budget_and_sync_build_remain_equivalent(self):
        sync_manager = _manager(lambda: "recalled")
        async_manager = _manager(lambda: "recalled")
        expected = sync_manager.build_messages(max_tokens=3, extra_protected=frozenset({"first"}))
        actual = await async_manager.build_messages_async(
            max_tokens=3, extra_protected=frozenset({"first"}),
        )
        self.assertEqual(actual, expected)
        self.assertEqual(async_manager.last_trim_report, sync_manager.last_trim_report)

    async def test_disabled_recall_is_never_invoked(self):
        recall = mock.Mock(return_value="not used")
        manager = _manager(recall)
        manager.apply_config(disabled=["catty_rag_recall"])
        self.assertEqual([m["content"] for m in await manager.build_messages_async()], ["first", "last"])
        recall.assert_not_called()

    async def test_timeout_keeps_loop_live_and_limits_late_workers(self):
        entered = threading.Event()
        release = threading.Event()
        semaphore = threading.BoundedSemaphore(1)
        calls = []

        def blocked(**_kwargs):
            calls.append("blocked")
            entered.set()
            release.wait(2)
            return _query_result("late")

        skipped = mock.Mock(return_value=_query_result("must not run"))
        with mock.patch.object(catty_rag, "_RAG_QUERY_SLOTS", semaphore), \
             mock.patch.object(prompt_manager, "_RAG_RECALL_TIMEOUT_SECONDS", 0.08):
            try:
                first = asyncio.create_task(_manager(_rag_prompt(blocked)).build_messages_async())
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                heartbeat = asyncio.Event()
                asyncio.get_running_loop().call_soon(heartbeat.set)
                await asyncio.wait_for(heartbeat.wait(), 0.5)
                self.assertFalse(first.done())
                output = await asyncio.wait_for(first, 1)
                self.assertEqual([m["content"] for m in output], ["first", "last"])
                output = await asyncio.wait_for(_manager(_rag_prompt(skipped)).build_messages_async(), 1)
                self.assertEqual([m["content"] for m in output], ["first", "last"])
                with self.assertRaises(catty_rag.RagQueryBusyError):
                    await asyncio.to_thread(_rag_prompt(skipped))
                skipped.assert_not_called()
                self.assertEqual(calls, ["blocked"])
            finally:
                release.set()
                acquired = await asyncio.to_thread(semaphore.acquire, True, 1)
                self.assertTrue(acquired)
                if acquired:
                    semaphore.release()
            output = await _manager(_rag_prompt(lambda **_: _query_result("recovered"))).build_messages_async()
            self.assertEqual([m["content"] for m in output[::2]], ["first", "last"])
            self.assertIn("recovered", output[1]["content"])

    async def test_cancellation_propagates_without_releasing_busy_slot_early(self):
        entered = threading.Event()
        release = threading.Event()
        semaphore = threading.BoundedSemaphore(1)

        def blocked(**_kwargs):
            entered.set()
            release.wait(2)
            return _query_result("late")

        with mock.patch.object(catty_rag, "_RAG_QUERY_SLOTS", semaphore):
            try:
                task = asyncio.create_task(_manager(_rag_prompt(blocked)).build_messages_async())
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(semaphore.acquire(blocking=False))
            finally:
                release.set()
                acquired = await asyncio.to_thread(semaphore.acquire, True, 1)
                self.assertTrue(acquired)
                if acquired:
                    semaphore.release()

    async def test_recall_failure_leaves_other_segments(self):
        def failed():
            raise RuntimeError("synthetic embedding failure")

        output = await _manager(failed).build_messages_async()
        self.assertEqual([m["content"] for m in output], ["first", "last"])

    async def test_busy_rag_is_reported_as_evidence_error(self):
        harness = importlib.import_module(f"{_PACKAGE_NAME}.fadianji_harness")
        semaphore = threading.BoundedSemaphore(1)
        store = catty_rag.CattyRAGStore.__new__(catty_rag.CattyRAGStore)
        store._enabled = True
        store._get_collection = mock.Mock()
        with mock.patch.object(catty_rag, "_RAG_QUERY_SLOTS", semaphore):
            self.assertTrue(semaphore.acquire(blocking=False))
            try:
                packet = await asyncio.to_thread(
                    harness.build_fadianji_evidence_packet,
                    "fixture", persona="catty", scope_key="group:1", group_id="1", user_id="7",
                    memory_store=None, rag_store=store, book_k=0, scene_k=0,
                    corpus_profile_enabled=False, feed_max_items=0,
                )
            finally:
                semaphore.release()
        self.assertEqual(packet["source_status"]["rag"]["state"], "error")
        self.assertIn({"source": "rag", "error_type": "RagQueryBusyError"}, packet["diagnostics"])
        store._get_collection.assert_not_called()

    async def test_cognitive_stage_times_out_and_drops_late_result(self):
        path = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        builder = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_build_messages")
        stage = next(
            n for n in ast.walk(builder) if isinstance(n, ast.Try)
            and any(isinstance(x, ast.Name) and x.id == "_build_cognitive_turn_context_sync" for x in ast.walk(n))
        )
        function = ast.parse(
            "async def stage():\n"
            "    _cognitive_system_context = ''\n"
            "    _adaptive_user_prompt = ''\n"
            "    return _cognitive_system_context, _adaptive_user_prompt\n"
        ).body[0]
        function.body.insert(-1, copy.deepcopy(stage))
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        logger = mock.Mock()

        def blocked(**_kwargs):
            entered.set()
            release.wait(2)
            finished.set()
            return "late evidence", "late adaptive prompt"

        async def short_wait(coroutine, timeout):
            self.assertEqual(timeout, 8.0)
            return await asyncio.wait_for(coroutine, 0.08)

        namespace = {
            "asyncio": types.SimpleNamespace(to_thread=asyncio.to_thread, wait_for=short_wait, TimeoutError=asyncio.TimeoutError),
            "_build_cognitive_turn_context_sync": blocked, "incoming": types.SimpleNamespace(text="message"),
            "_persona": object(), "key": "group:1", "_is_private_event": False,
            "event": types.SimpleNamespace(user_id=7, group_id=1), "_display_name": lambda _: "user",
            "quoted_text": "", "logger": logger,
        }
        exec(compile(module, str(path), "exec"), namespace)
        try:
            task = asyncio.create_task(namespace["stage"]())
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertEqual(await asyncio.wait_for(task, 1), ("", ""))
            logger.warning.assert_called_once()
        finally:
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))


class RagEmbeddingClientTests(unittest.TestCase):
    def test_openai_embedding_client_has_bounded_timeout_and_no_retries(self):
        old_client = mock.Mock()
        new_client = object()
        old_client.with_options.return_value = new_client
        embedding = types.SimpleNamespace(client=old_client)
        factory = mock.Mock(return_value=embedding)
        chromadb = types.ModuleType("chromadb")
        utils = types.ModuleType("chromadb.utils")
        utils.embedding_functions = types.SimpleNamespace(OpenAIEmbeddingFunction=factory)
        chromadb.utils = utils
        store = catty_rag.CattyRAGStore.__new__(catty_rag.CattyRAGStore)
        store._config = types.SimpleNamespace(
            catty_openai_api_key="TOKEN",
            catty_openai_base_url="https://example.test/v1/chat/completions",
            catty_embedding_model="example-embedding",
        )
        with mock.patch.dict(sys.modules, {"chromadb": chromadb, "chromadb.utils": utils}):
            result = store._try_openai_embedding()
        self.assertIs(result, embedding)
        self.assertIs(result.client, new_client)
        old_client.with_options.assert_called_once_with(timeout=5.0, max_retries=0)
        factory.assert_called_once_with(
            api_key="TOKEN", api_base="https://example.test/v1", model_name="example-embedding",
        )

    def test_legacy_embedding_client_remains_usable(self):
        embedding = types.SimpleNamespace(client=object())
        chromadb = types.ModuleType("chromadb")
        utils = types.ModuleType("chromadb.utils")
        utils.embedding_functions = types.SimpleNamespace(OpenAIEmbeddingFunction=lambda **_: embedding)
        chromadb.utils = utils
        store = catty_rag.CattyRAGStore.__new__(catty_rag.CattyRAGStore)
        store._config = types.SimpleNamespace(
            catty_openai_api_key="TOKEN",
            catty_openai_base_url="https://example.test/v1",
            catty_embedding_model="example-embedding",
        )
        with mock.patch.dict(sys.modules, {"chromadb": chromadb, "chromadb.utils": utils}):
            self.assertIs(store._try_openai_embedding(), embedding)

    def test_real_chat_builder_awaits_async_prompt_manager(self):
        path = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        builder = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_messages")
        calls = [
            node for node in ast.walk(builder)
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "_st_manager"
            and node.value.func.attr == "build_messages_async"
        ]
        self.assertEqual(len(calls), 1)

    @unittest.skipIf(importlib.util.find_spec("nonebot") is None, "nonebot not installed")
    def test_real_build_messages_with_blocked_recall(self):
        script = textwrap.dedent('''
            import asyncio, os, sys, threading
            root = sys.argv[1]
            sys.path[:0] = [root, os.path.join(root, 'src')]
            sys.modules['chromadb'] = None
            import nonebot
            nonebot.init(log_level='ERROR')
            assert nonebot.load_plugin('catty_qq_ai') is not None
            import catty_qq_ai as app
            import catty_qq_ai.prompt_manager as pm
            from nonebot.adapters.onebot.v11 import PrivateMessageEvent, Message
            from nonebot.adapters.onebot.v11.event import Sender
            pm._TIKTOKEN_TRIED, pm._TIKTOKEN_ENC = True, None
            pm._RAG_RECALL_TIMEOUT_SECONDS = 0.1
            entered, release = threading.Event(), threading.Event()
            class Rag:
                enabled = True
                def add(self, *args, **kwargs): pass
                def query(self, *args, **kwargs):
                    entered.set()
                    release.wait(3)
                    return []
            app.catty_rag_store = Rag()
            app._build_cognitive_turn_context_sync = lambda **kwargs: ('', '')
            text = 'fixture question'
            msg = Message(text)
            event = PrivateMessageEvent(time=0, self_id=99, post_type='message', sub_type='friend', user_id=7, message_type='private', message_id=1, message=msg, original_message=msg, raw_message=text, font=0, sender=Sender(user_id=7, nickname='user'))
            incoming = app.ExtractedMessage(text=text, history_content=text, mentioned=False, replied_to_self=False, used_prefix=False, image_urls=[], image_keys=[], has_image=False, directed=True, directed_strength='direct', directly_requested=True, needs_filter=False)
            async def check():
                task = asyncio.create_task(app._build_messages(event, 'private:7', incoming))
                try:
                    assert await asyncio.to_thread(entered.wait, 2), 'RAG query not reached'
                    heartbeat = asyncio.Event()
                    asyncio.get_running_loop().call_soon(heartbeat.set)
                    await asyncio.wait_for(heartbeat.wait(), 0.5)
                    messages, prefer_spark = await asyncio.wait_for(task, 5)
                    assert messages and isinstance(prefer_spark, bool)
                    print('REAL_BUILD_MESSAGES_MOCK_OK')
                finally:
                    release.set()
                    if not task.done(): task.cancel()
            asyncio.run(check())
        ''')
        allowed_env = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA")
        env = {key: os.environ[key] for key in allowed_env if key in os.environ}
        env["PYTHONUTF8"] = "1"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-c", script, str(_ROOT)],
                cwd=directory, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REAL_BUILD_MESSAGES_MOCK_OK", result.stdout)


class RagFailureTests(unittest.TestCase):
    def _store(self, collection):
        store = catty_rag.CattyRAGStore.__new__(catty_rag.CattyRAGStore)
        store._enabled = True
        store._get_collection = mock.Mock(return_value=collection)
        return store

    def test_query_failure_is_not_retried_without_persona_filter(self):
        collection = types.SimpleNamespace(query=mock.Mock(side_effect=TimeoutError("synthetic")))
        store = self._store(collection)
        with self.assertRaises(catty_rag.RagQueryError) as raised:
            store.query("group:1", "fixture", persona="fadianji")
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        self.assertEqual(collection.query.call_count, 1)

    def test_where_unsupported_falls_back_and_still_filters_persona(self):
        response = {
            "documents": [["shared", "other-persona"]],
            "metadatas": [[{"role": "user"}, {"role": "assistant", "persona": "catty"}]],
            "distances": [[0.1, 0.1]],
        }
        query = mock.Mock(side_effect=[TypeError("unexpected keyword argument 'where'"), response])
        result = self._store(types.SimpleNamespace(query=query)).query(
            "group:1", "fixture", persona="fadianji", boost_recency=False,
        )
        self.assertEqual([item[1] for item in result], ["shared"])
        self.assertEqual(query.call_count, 2)

    def test_embedding_error_is_reported_as_evidence_error(self):
        harness = importlib.import_module(f"{_PACKAGE_NAME}.fadianji_harness")
        collection = types.SimpleNamespace(query=mock.Mock(side_effect=RuntimeError("fixture")))
        packet = harness.build_fadianji_evidence_packet(
            "fixture", persona="catty", scope_key="group:1", group_id="1", user_id="7",
            memory_store=None, rag_store=self._store(collection), book_k=0, scene_k=0,
            corpus_profile_enabled=False, feed_max_items=0,
        )
        self.assertEqual(packet["source_status"]["rag"]["state"], "error")
        self.assertIn({"source": "rag", "error_type": "RagQueryError"}, packet["diagnostics"])

    def test_backfill_counts_only_writes_and_skips_unchanged_documents(self):
        class Collection:
            def __init__(self):
                self.rows = {}
                self.writes = 0
                self.fail = False

            def get(self, *, ids, include):
                row = self.rows.get(ids[0])
                return {"documents": [row[0]], "metadatas": [row[1]]} if row else {
                    "documents": [], "metadatas": [],
                }

            def upsert(self, *, ids, documents, metadatas):
                if self.fail:
                    raise RuntimeError("fixture")
                self.rows[ids[0]] = (documents[0], metadatas[0])
                self.writes += 1

        collection = Collection()
        store = self._store(collection)
        memory = types.SimpleNamespace(_data={
            "groups": {"1": {"summary": "first", "last_summary_at": 10}}, "users": {},
        })
        self.assertEqual(store.backfill_memory(memory), 1)
        self.assertEqual(store.backfill_memory(memory), 0)
        self.assertEqual(collection.writes, 1)
        memory._data["groups"]["1"]["summary"] = "changed"
        self.assertEqual(store.backfill_memory(memory), 1)
        collection.fail = True
        memory._data["groups"]["1"]["summary"] = "not written"
        self.assertEqual(store.backfill_memory(memory), 0)
        self.assertEqual(collection.writes, 2)


class LocalBGEProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = importlib.import_module(f"{_PACKAGE_NAME}.rag_embedding_backend")

    def _wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        waiter = threading.Event()
        while time.monotonic() < deadline:
            if predicate():
                return True
            waiter.wait(0.02)
        return bool(predicate())

    def test_start_does_not_wait_for_process_creation(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_spawn(*args, **kwargs):
            entered.set()
            release.wait(2)
            raise OSError("fixture")

        backend = self.backend.LocalBGEEmbeddingFunction(restart_delay=1, model_name="fixture-model")
        try:
            with mock.patch.object(self.backend.subprocess, "Popen", side_effect=blocked_spawn):
                before = time.monotonic()
                backend.start()
                self.assertLess(time.monotonic() - before, 0.5)
                self.assertTrue(entered.wait(1))
                with self.assertRaises(self.backend.LocalBGENotReadyError):
                    backend(["fixture"])
                backend.close()
                release.set()
                self.assertTrue(self._wait(lambda: backend._supervisor is None))
        finally:
            release.set()
            backend.close()

    def test_local_model_selection_is_independent_of_nlu_switch(self):
        store = catty_rag.CattyRAGStore.__new__(catty_rag.CattyRAGStore)
        store._config = types.SimpleNamespace(
            catty_rag_local_bge_enabled=True,
            catty_use_text2vec=False,
            catty_rag_local_bge_model="fixture-model",
        )
        worker = store._try_local_bge_embedding()
        try:
            self.assertIsInstance(worker, self.backend.LocalBGEEmbeddingFunction)
            self.assertFalse(worker.ready)
            self.assertIsNone(worker._supervisor)
        finally:
            worker.close()
        store._config.catty_rag_local_bge_enabled = False
        self.assertIsNone(store._try_local_bge_embedding())

    def test_real_worker_protocol_with_synthetic_dependencies(self):
        worker = _ROOT / "src" / "catty_qq_ai" / "rag_embedding_worker.py"
        script = textwrap.dedent('''
            import runpy, sys, types
            torch = types.ModuleType('torch')
            torch.set_num_threads = lambda n: None
            hub = types.ModuleType('huggingface_hub')
            def snapshot_download(*, repo_id, local_files_only):
                assert repo_id == 'BAAI/bge-small-zh-v1.5' and local_files_only
                return 'synthetic-snapshot'
            hub.snapshot_download = snapshot_download
            text2vec = types.ModuleType('text2vec')
            class Model:
                def __init__(self, snapshot, device):
                    assert snapshot == 'synthetic-snapshot' and device == 'cpu'
                def encode(self, texts, normalize_embeddings):
                    assert normalize_embeddings and all(len(text) <= 256 for text in texts)
                    return [[float(len(text))] + [0.0] * 511 for text in texts]
            text2vec.SentenceModel = Model
            sys.modules.update(torch=torch, huggingface_hub=hub, text2vec=text2vec)
            runpy.run_path(WORKER, run_name='__main__')
        ''').replace("WORKER", repr(str(worker)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture_worker.py"
            path.write_text(script, encoding="utf-8")
            backend = self.backend.LocalBGEEmbeddingFunction(
                worker_path=path,
                restart_delay=0.1,
                model_name="BAAI/bge-small-zh-v1.5",
            )
            try:
                backend.start()
                self.assertTrue(self._wait(lambda: backend.ready), backend._last_error_type)
                proc = backend._proc
                self.assertEqual(backend(["x" * 300])[0][0], 256.0)
                self.assertEqual(len(backend.embed_query(["query"])[0]), 512)
            finally:
                backend.close()
            self.assertIsNotNone(proc.poll())

    def test_inference_timeout_kills_and_restarts_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            flag = Path(directory) / "first"
            path = Path(directory) / "hung_worker.py"
            script = textwrap.dedent('''
                import json, pathlib, sys, time
                flag = pathlib.Path(FLAG)
                first = not flag.exists()
                flag.write_text('started', encoding='utf-8')
                print('CATTY_RAG_V1=' + json.dumps({'event': 'ready'}), flush=True)
                for line in sys.stdin:
                    request = json.loads(line)
                    if first:
                        time.sleep(10)
                    print('CATTY_RAG_V1=' + json.dumps({'event': 'result', 'id': request['id'],
                        'embeddings': [[0.5] * 512 for text in request['input']]}), flush=True)
            ''').replace("FLAG", repr(str(flag)))
            path.write_text(script, encoding="utf-8")
            backend = self.backend.LocalBGEEmbeddingFunction(
                worker_path=path, startup_timeout=2, request_timeout=0.2,
                restart_delay=0.05, model_name="fixture-model",
            )
            try:
                backend.start()
                self.assertTrue(self._wait(lambda: backend.ready))
                first = backend._proc
                with self.assertRaises(self.backend.LocalBGETimeoutError):
                    backend(["fixture"])
                self.assertTrue(self._wait(lambda: first.poll() is not None))
                self.assertTrue(self._wait(lambda: backend.ready and backend._proc is not first))
                self.assertEqual(backend(["fixture"])[0][0], 0.5)
            finally:
                backend.close()

    def test_model_startup_timeout_terminates_hung_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hung_startup.py"
            path.write_text("import threading\nthreading.Event().wait(30)\n", encoding="utf-8")
            backend = self.backend.LocalBGEEmbeddingFunction(
                worker_path=path, startup_timeout=0.2, restart_delay=10,
                model_name="fixture-model",
            )
            try:
                backend.start()
                self.assertTrue(self._wait(lambda: backend._proc is not None))
                proc = backend._proc
                self.assertTrue(self._wait(lambda: proc.poll() is not None))
                self.assertFalse(backend.ready)
                self.assertEqual(backend._last_error_type, "TimeoutError")
            finally:
                backend.close()

    @unittest.skipUnless(os.name == "nt", "Windows parent-process handle test")
    def test_worker_exits_if_parent_dies_during_model_load(self):
        worker = _ROOT / "src" / "catty_qq_ai" / "rag_embedding_worker.py"
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "loading"
            path = Path(directory) / "stalled_model.py"
            script = textwrap.dedent('''
                import pathlib, runpy, sys, threading, types
                torch = types.ModuleType('torch')
                torch.set_num_threads = lambda n: None
                hub = types.ModuleType('huggingface_hub')
                hub.snapshot_download = lambda **kw: 'synthetic-snapshot'
                text2vec = types.ModuleType('text2vec')
                class Model:
                    def __init__(self, *args, **kwargs):
                        pathlib.Path(MARKER).write_text('loading', encoding='utf-8')
                        threading.Event().wait(30)
                text2vec.SentenceModel = Model
                sys.modules.update(torch=torch, huggingface_hub=hub, text2vec=text2vec)
                runpy.run_path(WORKER, run_name='__main__')
            ''').replace("MARKER", repr(str(marker))).replace("WORKER", repr(str(worker)))
            path.write_text(script, encoding="utf-8")
            target = subprocess.Popen(
                [getattr(sys, "_base_executable", sys.executable), "-I", "-c", "import threading; threading.Event().wait(30)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            env = self.backend._child_environment("fixture-model")
            env["CATTY_RAG_PARENT_PID"] = str(target.pid)
            child = subprocess.Popen(
                [sys.executable, "-I", "-B", "-u", "-X", "utf8", str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            )
            try:
                self.assertTrue(self._wait(marker.exists), "worker did not enter model load")
                target.terminate()
                target.wait(timeout=3)
                child.wait(timeout=5)
            finally:
                if target.poll() is None:
                    target.kill()
                    target.wait(timeout=3)
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
                if child.stdin:
                    child.stdin.close()
                if child.stdout:
                    child.stdout.close()

    def test_secret_environment_is_not_inherited(self):
        with mock.patch.dict(os.environ, {
            "CATTY_OPENAI_API_KEY": "TOKEN", "API_KEY": "TOKEN", "USERNAME": "fixture-user",
        }):
            env = self.backend._child_environment()
        self.assertNotIn("CATTY_OPENAI_API_KEY", env)
        self.assertNotIn("API_KEY", env)
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        self.assertEqual(env["OMP_NUM_THREADS"], "1")
        self.assertEqual(env["USERNAME"], "fixture-user")
        self.assertEqual(env["CATTY_RAG_PARENT_PID"], str(os.getpid()))

    def test_frozen_application_cannot_spawn_itself(self):
        backend = self.backend.LocalBGEEmbeddingFunction()
        try:
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(self.backend.subprocess, "Popen") as popen:
                with self.assertRaises(self.backend.LocalBGENotReadyError):
                    backend.start()
                popen.assert_not_called()
        finally:
            backend.close()


class RepeatDeliveryTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(importlib.util.find_spec("nonebot") is None, "nonebot not installed")
    async def test_only_successful_repeat_is_added_to_bot_history(self):
        from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError

        path = _ROOT / "src" / "catty_qq_ai" / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = copy.deepcopy(next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_expression_repeat"
        ))
        function.decorator_list = []
        function.returns = None
        for argument in function.args.args:
            argument.annotation = None
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        remember = mock.Mock()
        namespace = {
            "_locks": {"scope": asyncio.Lock()}, "_conversation_queue_key": lambda _event: "scope",
            "_remember_bot_repeat_for_event": remember, "OnebotActionFailed": ActionFailed,
            "OnebotNetworkError": NetworkError, "logger": mock.Mock(),
        }
        exec(compile(module, str(path), "exec"), namespace)
        run = namespace["handle_expression_repeat"]
        event = object()
        failure = ActionFailed(status="failed", retcode=1200, message='{"result":120,"errMsg":""}')
        for side_effect in (failure, NetworkError("fixture"), None):
            matcher = types.SimpleNamespace(send=mock.AsyncMock(side_effect=side_effect), finish=mock.AsyncMock())
            await run(matcher, event, {"catty_repeat_message": "fixture"})
            matcher.finish.assert_awaited_once()
        remember.assert_called_once_with(event, "fixture")


if __name__ == "__main__":
    unittest.main()
