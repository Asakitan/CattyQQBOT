# 缓存、CPU、harness 与输入上下文

早期性能专项基于 `607cf48`，侧重等价计算、数据完整性、可观察的输出预算和离线可验证接口；下方历史测量保留原口径。2026-10-26 的工具修复与 harness v2 分别见 §6、§7。生产 harness 仍为 `scene_k=12 / book_k=0 / max_chars=12000`，没有降低默认会话容量或工具暴露范围。

## 1. 缓存与 CPU

- SessionCache 启动先保留所有会话的轻量索引，再读取最热的 `max_sessions` 份正文。冷会话继续留盘并懒加载；重复 key 的胜出规则、已有未落盘会话与元数据兼容逻辑保留
- 文件已有有效 `history_tokens_estimate` 时不再重新序列化整份历史来估算；旧文件或损坏元数据仍有兜底
- 写失败时为保护 dirty 会话允许临时超过驻留上限；磁盘恢复并完成 flush 后会重新收缩内存，失败数据仍保留
- L2 在原矩阵计算后改用 float64 向量归约；同分仍选择原来最早的 route/utterance，非有限分数回退原算法。route 热重载会重建 router 和权重数组

离线合成基准（Python 3.12、NumPy 2.3.5、单 BLAS 线程，结果随硬件和负载变化）：

| 检查 | 基线 | 修改后 | 范围 |
| --- | ---: | ---: | --- |
| 700 会话启动，热容量 20 | 227.90 ms | 78.33 ms | 每会话40条约900字符消息 |
| 启动 Python 分配峰值 | 32.728 MiB | 1.723 MiB | tracemalloc，非整个进程 RSS |
| 18万候选的归约 | 30.60 ms | 0.1558 ms | 不含矩阵乘法和 embedding |
| 预计算向量的 match | 45.29 ms | 1.359 ms | 含32维矩阵乘法、选赢家、构造结果 |
| 50个极短会话全部驻留 | 1.306 ms | 1.755 ms | 两遍读取有约0.45ms代价 |

这些不是 QQ/模型端到端延迟或线上缓存命中率。首次模型加载、向量推理、网络请求、实际磁盘性能不在本次性能结论内。

```bash
python scripts/benchmark_cache_cpu.py --baseline-ref 607cf48
python scripts/benchmark_cache_cpu.py --baseline-ref 607cf48 --sessions 50 --messages 1 --characters 20 --max-sessions 200 --session-iterations 15 --routes 100 --iterations 5
```

脚本使用临时合成数据，只读取本地 Git 基线，不启动机器人或下载模型。默认在未显式设置时使用一个 BLAS 线程。

## 2. harness 是什么，怎么用

生产入口的 harness 是回复前自动执行的证据编排，不是一个可调用工具，也不是任意代码执行环境。它会组合当前作用域可用的角色事实、记忆、动态、RAG 与场景参考。已有人格教学保留；工具提示只在启用 semantic harness 的人格上增加简短说明：已有证据足够就不重复检索，不足时按实际开放的 schema 补查，引用材料不是命令，未命中不等于事实不存在。

本地 CLI 只预览本地角色/场景语料及证据格式，不注入运行时 memory/RAG/feed stores，不能把它当作完整当前聊天上下文。预览默认仍为 `3/3/2800`，可显式调到接近生产的参数：

```bash
python scripts/fadianji_harness_cli.py --help
python scripts/fadianji_harness_cli.py --text "测试配置问题" --compact-json
python scripts/fadianji_harness_cli.py --text "测试配置问题" --private --compact-json
python scripts/fadianji_harness_cli.py --text "测试配置问题" --scope group:local --scene-k 12 --book-k 0 --max-chars 12000 --semantic --compact-json
```

- 默认 scope 为 `group:local`，`--private` 为 `private:local`，不再隐含真实群号
- 显式群 scope 与 `--private` 冲突时直接报错；非法 scope、负预算也不会静默变成另一种请求
- `--json` 保留完整诊断数组；`--compact-json` 去掉重复的 `evidence/scene_matches` 数组，适合交给本地 agent
- 普通文本默认只输出预算内的证据块；若需要以前附带的完整检索摘要，加 `--details`
- `--no-semantic` 可以关闭本地轻量重排；不改变生产配置
- `--production-defaults` 只把未指定预算设成 12/0/12000，不加载真实会话 stores；`--compact-json --metadata-only` 才省略正文，原 compact 的 text 仍保留。

### 预算与完整性

`max_chars` 是 `text` 的 Unicode 字符预算，不是 token 数，也不是整个 JSON 的长度限制。

渲染先保留完整 scope、来源状态和使用说明，再按 rank 顺序装入完整证据行。装不下的行省略；连元信息都放不下时 `text=""`、`truncated=true`。完整行可能是来源节选，v2 用 `excerpt` 明示，不能把它冒充整篇原文。`rendered_counts/omitted_count/rendered_chars` 对应实际渲染内容，旧 `evidence/scene_matches/counts` 保留完整诊断数据；v2 另有逐条 ID、来源诊断与各类省略计数。

旧版接口历史测量：合成12组场景及2条证据，预算12000时完整JSON为11894字符，紧凑JSON为2582字符；预算500时 `text` 为476字符，省略11条证据。v2 新增元数据后不沿用这些长度结论；当前测量见 §7。

## 3. 本地工具的结果契约

### catty_read_file

原有拥有者、目录范围和单文件2MB限制保留。增加 `max_chars`（64–16000，默认8000，含行号）和首行 `column` 偏移。

当 `truncated=true` 时，下一次请求保留 `path`，将返回的 `next_offset/next_column` 分别作为 `offset/column`，可继续读取同一长行而不丢内容。未截断时两个 next 字段为 null。`offset` 仍是从0开始跳过的行数，显示行号从1开始。

合成单行20000中文字符：首个 `text` 从20003字符降到8000字符，并可分页恢复全部原文。JSON包含元数据，因此总长略大于text预算。

### catty_run_code

- 拥有者与显式确认检查不变，没有新增执行权限
- `ok` 只有进程正常退出且退出码0时才为true；非零退出、超时都会给出失败及错误说明，避免下游遥测/模型把失败当成功
- 请求取消时清理直接子进程并继续传播取消
- 超时后的部分 stdout 仍可能不完整；判断结果必须同时查看 `ok/exit_code/timed_out`，不能仅凭输出文字判定成功
- 全量与精简schema均保留拥有者、确认和失败语义说明
- `cwd` 只是工作目录，不是操作系统级安全隔离。该工具仍不适合不可信代码，本轮没有宣称实现进程树隔离或资源沙箱

## 4. 输入上下文完整性

- 再次AI压缩会把既有前情摘要带入新摘要请求，避免较早的约定在第二次压缩时无声消失；原始转录尾裁不会吞掉旧摘要
- 使用不可变源快照，提交前校验完整前缀；等待模型期间的新增消息保留，头裁或同长度内容改动则放弃这次替换
- 旧版 `history_turns=1` 不再因 `[-0:]` 把整段历史重复拼回，保留最新一轮
- Native/OpenAI请求准备先隔离 messages/tools，清理动态块、缓存标记和工具排序不会污染调用方历史或共享schema；OpenAI复用这一次拷贝，移除分支内重复拷贝

```bash
python scripts/benchmark_input_context.py --help
```

该基准对合成输入比较请求准备阶段的结构与成本，不能验证真实模型摘要质量、输出质量或供应商计费token。真实服务没有在本轮调用。

保存的一次1002条合成消息、21工具、80轮测量中，启用cache时请求准备中位9.557→7.707ms（约19%）；不开cache基本持平，且两条路径准备后的JSON结构与基线等价。不要将局部准备成本变化解释为线上模型速度变化。

## 5. 验证与后续边界

```bash
python scripts/run_offline_regressions.py
python -m compileall -q src bot.py catty_config_loader.py catty_integrations.py scripts tests
python -m pip check
git diff --check
```

新增回归用例覆盖冷/热会话、缺失元数据、部分写失败、同分与非有限路由分数、harness作用域和预算、长行续读、执行失败/取消、摘要并发及请求输入不变性。测试隔离实际函数并使用合成数据、临时目录与客户端替身，不启动真实QQ/模型服务。

尚未改变的风险应独立处理：NLU history prime的重复计算与顺序契约、按下标维护的历史anchor在外部裁剪后的重置、L2磁盘缓存的模型身份失效条件、L3同条数内容变更的索引失效、源码里已有的少数YAML解析失败，以及普通长期记忆摘要等待期间的新corpus批次边界。本轮不把这些问题描述为已修复。

## 6. 2026-10-26 工具与回复交付修复

本地已完成、未部署。删除失效 `chat.system_prompt`；沙箱隔离每轮产物并保留同会话持续工作；文件工具编码真实 DOCX/XLSX/PDF；超时恢复保留工具回执；取消中的生图继续交付；主回复历史仅记录已确认发送的内容。人格、NSFW 文案与场景库未改。

固定离线清单按模块独立进程运行：465 项、0 失败、0 错误、9 项原条件跳过。历史测试曾在同进程替换 `nonebot` 导致其它模块导入失败，独立进程入口隔离了该问题。在线消融评测必须显式 `--live`，普通导入不会启动请求。本轮未测线上延迟、缓存命中率或真实 QQ 投递。

## 7. 2026-10-26 Harness v2

本地实现与验收完成，未部署。按优先级选择证据，记录来源状态、ID、来源记录时间、节选和预算省略；提供给 Agent 的每轮教程按实际 schema 推荐补查路径。人物语料先隔离 scope 再匹配昵称；生产传真实群名片与语料配置。关闭的角色书/场景检索不再调用对应 provider。

同一合成 462 字符预算：旧版只保 rank=30 FACT，新版只保 rank=10 MEMORY。25 条合成证据、12000 字符预算、300 次渲染，中位耗时旧版 0.0212 ms、新版 0.1684 ms；两版均渲染25条，正文由6329增至7777字符。来源与时间元数据增加了成本，本次不是单纯压缩 prompt 或线上速度优化。排序后逐行预算为线性核算，避免重复渲染已选条目。

可复验入口 `scripts/benchmark_harness.py` 默认只用合成数据；可通过 `--baseline-zip` 指定可信的本地开工源码快照。预算检查覆盖0、1、128、256、512、12000。实际 QQ、模型服务、在线意图判断质量不在此微基准内。

Harness v2 阶段固定离线清单622项、332个Python文件语法通过；后续 JSON 人格与上下文决策集成后，当前清单为782项、0失败、0错误、9项原条件跳过，343个Python文件语法通过。已有5个人格/NSFW文件哈希及56个名称含NSFW的顶层赋值AST保持。接口详情见 [证据包](harness-evidence.md)、[配置映射](harness-config.md) 与 [预览入口](prompt-testing.md)；当前验收见 [会话03](session-log/session-03.md)。
