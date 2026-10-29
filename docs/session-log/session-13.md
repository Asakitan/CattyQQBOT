# 会话 13 · 模型、端点与兜底 key 收口到 config

## 目标与范围

- 盘点运行时代码里写死的模型名、厂商端点和兜底 key，把可运营的取值移到 `config.json` / `config.example.json` / `Config` 字段。
- 覆盖：主 AI 之外的 evolution judge、CPU L4 stylize、文本/向量模型、RAG embedding、NovelAI 生成与 Director、Pixelbunny 普通绘图、Ollama 启动参数与 derived model。
- 不改变各线路的默认值：只把默认值集中到配置层，删除业务模块里的字面量兜底。

## 已完成

- `config.py` 新增字段：`catty_rag_local_bge_model`、`catty_embedding_model`、`catty_imagegen_pixelbunny_model`、`catty_imagegen_nai_endpoint`、`catty_imagegen_nai_augment_endpoint`、`catty_cpu_engine_l4_ollama_base_url/api_key/model`、`catty_evolution_judge_base_url`。
- `catty_config_loader.py` 增加 `rag.local_bge_model`、`rag.embedding_model`、`imagegen.pixelbunny_model`、`imagegen.nai.endpoint/augment_endpoint`、`cpu_engine.l4_ollama_*`、`evolution.judge_base_url` 的 env 映射；`ai_fallback` 默认补 `auto_build_derived_model/derived_model_name/base_model`。
- `cpu_engine/evolution_judge.py`：移除 `https://api.deepseek.com/v1` 与 `deepseek-v4.1-flash` 字面量兜底；空配置明确告警并返回 `None`。
- `cpu_engine/ollama_stylize.py`：改用 `cpu_engine.l4_ollama_*`，不再借用 `ai_fallback`（避免误打云端 DeepSeek），空配置跳过。
- `cpu_engine/corpus_txtai.py` / `router.py`：`TxtaiCorpus` 的 `model_name` 改为必填；router 从 `catty_text2vec_model_name` 读取，空值禁用 L3 corpus。
- `nlu/text2vec_engine.py` / `nlu/prototypes.py`：移除 `BAAI/bge-small-zh-v1.5` 字面量兜底；模型名为空时加载器直接跳过，prototype 缓存 hash 使用配置值。
- `catty_rag.py` / `rag_embedding_backend.py` / `rag_embedding_worker.py`：本地 BGE 模型 id 由 `rag.local_bge_model` 提供，经 `CATTY_RAG_MODEL` 传给隔离 worker；OpenAI embedding 用 `rag.embedding_model`，空值跳过。
- `tools.py` / `catty_nsfw_imagegen.py`：NovelAI 生成/加工端点从 `imagegen.nai.endpoint/augment_endpoint` 读取；Pixelbunny 普通绘图用 `imagegen.pixelbunny_model`；移除 `seedream-4.5/5-pro`、`gpt-image-2`、`nai-diffusion-4-5-full` 的运行时兜底。
- `catty_integrations.py`：`ollama.api_url` 与 `ai_fallback.derived_model_name/base_model` 只读 config；缺失时给明确提示/错误。
- `config.json` 与 `config.example.json` 已补键；`config.json` 中 11 个敏感字段逐项比对未变。

## 验证证据

- `py_compile`：15 个改动/测试文件全部通过。
- 定向测试：`test_config` 17、`test_config_loader` 17、`test_cache_cpu` 29、`test_rag_async_prompt` 25、`test_shared_persona_nsfw_imagegen` 8、`test_tool_dispatcher` 28、`test_imagegen_force_tool` 5、`test_integrations` 17，全部 OK。
- 离线清单 `scripts/run_offline_regressions.py`：1462 项，0 failures、42 errors、9 skipped。42 个 error 全部来自既有 `test_fdj_proactive_integration` / `test_shared_persona_integration` 的 `_extract_incoming_with_voice` 未注入，源文件与本轮改动无关（`git status` 干净）。
- loader 实测：新 env 键均按 `config.json` 导出；`CATTY_EVOLUTION_JUDGE_BASE_URL` 为空表示回退 audit/main 路线。
- 未验证：真实 DeepSeek/Ollama/NovelAI/Pixelbunny 请求；远端配置与部署。

## 部署状态与剩余事项

- 本会话结束时未部署；后续已由 [会话 16](session-16.md) 部署并按字段合并远端配置。
- 远端若要覆盖默认值，需要补同名配置键；不补时 pydantic 默认值与改动前行为一致。
- 本地 BGE 若换成非 512 维模型，worker/backend 的 512 维协议常量需要同步调整（本轮未改维度协议）。
