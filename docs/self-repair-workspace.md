# 自动修复工作区、验证与发布

## 职责与边界

`src/catty_self_repair/workspace.py` 提供源码快照、模型候选工具和事务发布；`validation.py` 提供固定离线测试执行器。两个模块不导入 `catty_qq_ai.__init__`，不调用模型，不读取真实 `config.json`，不启动或停止 bot。父级监督器必须先停 bot，再调用发布、回滚或恢复接口；健康检查成功后才确认发布。

**Python 副本、只读属性、环境清理和审计钩子不是操作系统安全沙箱。** 它们用于防止普通误读、误写和验证串扰，不能可靠约束同用户权限的恶意 Python/native 代码，也不能证明没有运行时修改后恢复原状的行为。需要对抗恶意候选时，应在另行配置的低权限操作系统隔离环境执行；本模块没有宣称实现该隔离。

## 固定接口

- `create_snapshot(root, run_dir, config) -> Workspace`：`run_dir` 必须是 `data/self_repair/runs/<run_id>`，非空目录不覆盖。
- `Workspace(root, run_dir, config)`：重新打开已完成的元数据，拒绝配置指纹或根路径不一致。
- `baseline`、`candidate`：两个独立源码根，保留原项目相对路径布局。
- `list_files()`、`read_file(path, start=1, end=200)`、`search(query, limit=20)`：只暴露允许编辑的 Python 源码；读取为 1 起始行号，单次最多 1000 行、65536 字符；搜索为有界字面量搜索。
- `replace_text(path, old, new)`：`old` 必须非空且恰好命中一次，写入前校验候选当前哈希；保留原 UTF-8 BOM。
- `write_file(path, content)`：只创建新源码，不覆盖已有文件，不提供删除、shell 或自定义测试命令。
- `changes()`：返回 `path`、`before_sha256`、`after_sha256`，新文件的前像哈希为 `null`。
- `validate(*, deadline=None)`：返回包含 `ok`、全部测试明细、完整文件清单和完整性检查的字典，落盘为 `validation.json`。可选 `deadline` 是 `time.monotonic()` 的绝对截止时间，与验证总时限取较早者。
- `prepare_release(summary)`：仅接受绑定当前全部字节、配置指纹的成功验证；返回固定 `data/self_repair/runs/<run_id>/release.json`。

配置由调用者传入，本模块只计算其指纹，不从磁盘重读真实配置。父级消费 `ready.json` 时仍须重新加载配置并检查 `enabled`、`auto_apply` 与指纹。

## 快照白名单与不可编辑文件

快照的源码根固定为 `DEFAULT_ALLOWED_PATHS`：`src/catty_qq_ai`、`bot.py`、`catty_config_loader.py`、`catty_integrations.py`。`allowed_paths` 只能缩窄模型可编辑范围，不能扩展固定根。

为了让现有通过 `Path(__file__).parents[...]` 定位项目的测试实际运行，两个副本都包含冻结的：

- `tests/**/*.py`，包括未选中的 Python 测试辅助模块；只执行配置明确选择的测试模块。
- `scripts/**/*.py` 和 `src/catty_self_repair/**/*.py`，仅作只读导入/源码支持，不能被模型访问或修改。
- 存在时的 `config.example.json`、`pyproject.toml`、`setup.py`、`CattyQQAI.spec`。
- 人格正文及配置范围外的源码依赖，也仅作为只读支持文件。

不遍历真实数据、日志、缓存、模型、备份、虚拟环境或隐藏目录；不复制 `config.json`、`.env`、运行时 JSON、密钥文件或已知凭据源码文件。模板配置若出现非占位凭据则停止快照；空值、明确的模板值和本地 Ollama 的 `ollama` 占位值不视作真实密钥。不会读取真实配置后再“脱敏复制”。白名单源码仍被视为项目可信输入，不承诺发现任意源码中私自硬编码的秘密。

`personas/`、`characters/`、`prompts/` 下文件，`persona*`、`character*` 文件，以及已登记的人格正文/提示词文件禁止模型读写；`harness_prompt.py` 等正常 harness 源码不因通用 prompt 名称被全部封死。这是文件级范围约束，不是对任意新代码语义的证明。

基线全部只读，候选中的测试、控制器、脚本和支持文件只读；仍在每次验证及发布前复核实际字节，不把只读位当作可信证据。单文件上限 8 MiB，整个快照上限 128 MiB、8192 个文件。`max_files` 约束实际变更源码数量，`max_patch_bytes` 统计插入、删除、替换的**行字节数之和**；不是整份大文件的大小，因此大 `__init__.py` 的小修改可进入正常预算。

## 验证规则

1. LLM 开始编辑前就冻结测试。验证没有模型指定的命令、测试过滤器或测试替换入口。
2. 基线与候选对每个选定模块分别启动独立子进程：固定 `python -I -S -B -X utf8 -c <可信 runner>`，工作目录是对应副本，`tests/src/项目根` 指向该副本。
3. 不继承父进程 `PYTHONPATH`、代理、API key、`CATTY_*` 配置或用户 HOME；HOME/TEMP/缓存均指向该次子进程独有临时目录。只显式加入解释器依赖目录，不执行 `.pth` 或 editable-install 钩子。
4. 固定审计钩子限制普通 Python 文件访问、写入和额外子进程；禁止外部联网，允许事件循环所需的本机 loopback。它不是恶意代码安全边界。
5. 单模块使用 `test_timeout_seconds`，整轮使用 `validation_timeout_seconds` 及可选外层截止时间。Windows 在 stdin 放行前加入 kill-on-close Job；其他受支持平台使用独立进程组。正常退出、超时及 `BaseException` 取消都回收自有进程树。输出仅保留有界尾部，超量、缺少结果、加载失败、超时或不完整结果均失败。
6. 收集发现的 case ID、实际执行 ID、每个 outcome、subtest ID/outcome、skip 原因、异常类别/规范化消息摘要和诊断。不以测试总数相同代替无回归；同样总数下替换、丢失或新增 ID 也不能通过。
7. 新失败、新错误、新 skip、skip 原因变化、丢失 subtest、新的测试加载/fixture 错误均阻止发布。既有红项只有同 ID、同 outcome、同异常身份保持不变或转为成功才可接受；报告仍完整保留 `baseline_red`、`candidate_red`，不会说成“全绿”。
8. 无测试、零 case、全部跳过、执行数量与发现 ID 不一致，不能发布。既有读取根 `config.json` 的测试不会被过滤；由于没有复制真实配置，其缺文件错误按真实基线红项比较。
9. 每个子进程运行前后都复核两个副本的**全量**源码、测试、脚本、控制器和支持文件哈希；基线测试若影响候选，也会立即失败。成功报告通过独立摘要绑定到 `workspace.json`；随后任意候选变更都会使已有验证失效。

异常身份归一化同时处理运行根路径的原始、`repr` 和 JSON 转义形式（含中文目录）；只替换冻结根/临时根前缀，保留相对文件名。只将对象 repr 中的 `at 0x...>` 当作易变地址，不抹去 HRESULT 等十六进制错误码。真实源码验证曾发现 Windows `FileNotFoundError` 中双反斜杠未被替换，导致相同缺文件错误被当作新增回归；现有纯函数与子进程正反例覆盖该路径。

## 发布日志与恢复

固定活动日志：`data/self_repair/active_release.json`。四个接口共享非阻塞 OS 文件锁 `release.lock`：

| 接口 | 结果 |
|---|---|
| `apply_release(root, release_path, config)` | 成功 `status=applied`、`run_id`；普通写入失败自动逆序回滚 |
| `rollback_release(root, reason)` | 完整恢复 `status=rolled_back`；无活动日志为 `none` |
| `confirm_release(root)` | 完整后像仍匹配才 `status=confirmed`，持久记录完成后删除活动日志 |
| `recover_release(root)` | 未完成事务逆序回滚；已确认/已回滚但清理中断的事务只完成清理；无日志为 `none` |

路径或证据非法时抛出明确的 `WorkspaceError`/文件系统异常。人工改动无法安全恢复时返回 `status=conflict` 和 `conflicts`，保留活动日志及全部原件；父级必须保持停止状态，不能当成恢复成功启动 bot。

发布前重新检查**全量 live 前像**与**全量已验证候选后像**，包括未改源码、冻结测试及支持文件的新增、删除和变更；另检查发布 payload、验证报告与配置指纹。所有备份、暂存像和活动日志在第一次 live 写入前完成 flush/fsync。POSIX 同步父目录，Windows 重命名使用 write-through。

每份原文件先通过不可覆盖重命名移入 `transaction/held`，再验证移入的实际前像；新文件以不可覆盖重命名安装。若操作员在中途重新创建目标，新文件不会覆盖人工文件；若原文件在移动瞬间被人工修改，保留该原件，只在目标为空时移回。每个步骤都先持久记录意图，崩溃后依照前/后像及交易产物恢复，而不是仅信任步骤计数。

回滚逆序处理，只有 live 仍匹配已安装后像才移动回滚；第三种内容、异常链接或已应用后人工删除均保留并报冲突。回滚原件和备份也验证哈希。终态先写回活动日志，再写 `release_receipt.json`，最后删除活动日志，因此确认后清理崩溃不会被误回滚。备份、held 原件和 discarded 后像不自动删除，避免丢掉编辑器旧句柄后来写入的内容。

禁止路径穿越、绝对工具路径、反斜杠别名、ADS、设备名、尾随点/空格、大小写冲突、symlink/junction/reparse 和 hardlink；打开文件时比较 lstat/fstat，操作前反复检查祖先路径。这里解决正常并发编辑与可检测漂移，不声称提供对同权限攻击者的内核级文件系统事务。

## 验证入口

测试只创建临时合成仓库，不读取操作员的配置，不调真实模型，不部署或提交。覆盖路径/链接、预算、冻结支持、隔离环境、case/subtest 身份、旧红项、新失败/skip、完整性、超时/取消、发布冲突及多个崩溃窗口。

定向运行：

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location 'E:/VC/Catty'
& 'E:/Py/python.exe' -B -X utf8 -m unittest discover -s tests -p 'test_self_repair_workspace.py' -v
```

测试已经在本地执行；专项与完整回归回执见 [会话 14](session-log/session-14.md)。另有 `tests/test_self_repair_acceptance.py` 通过真实 worker/守护进程完成应用确认与崩溃回滚。
