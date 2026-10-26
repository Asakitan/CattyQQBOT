# Python 沙箱：运行归属与文件交付

实现：`src/catty_qq_ai/code_sandbox.py`。本次仅修复运行目录、产物归属、输出收集和清理；不修改人格、NSFW、内容规则、包名单或安装策略。

## 调用合同

`async run_code(config, code, *, scope: str | None = None, input_files: Mapping[str, str | bytes] | None = None) -> dict`

- 旧的两个位置参数保持可用。`SandboxPaths` 的字段与 `sandbox_paths` / `ensure_sandbox` 入口保持不变。
- 不传 `scope`（或空字符串）时，每次运行完全独立，不自动读取全局 `work` 或前次运行的产物。
- 非空 `scope` 是**调用方可信会话身份**，不是模型可指定的任意工作路径。工具层应使用包含会话和用户的稳定值，例如 `f"{ctx.scope_key}:user:{ctx.user_id}"`。
- 同一进程、同一事件循环内，同 scope 的执行串行化；不同 scope 可并发。目录名使用身份的 SHA-256，不把身份当路径拼接。多个 bot 进程不可共用同一沙箱根目录；这里不是跨进程锁服务。
- `input_files` 是相对文件名到内容的映射；文本编码为 UTF-8，也支持 bytes。允许子目录，拒绝绝对路径、盘符和 `..`。它不读取调用方传来的宿主路径。

### 持续工作语义

1. 非空 scope 每次把该 scope **上次成功运行的工作文件副本**复制到新的工作目录。
2. 本次 `input_files` 在副本上覆盖同名输入；随后记录大小、纳秒 mtime 与内容 SHA-256 的输入基线。
3. 子进程在本次工作目录运行，仍能用普通相对路径读取、修改前轮文件，支持多轮代码迭代。
4. 成功运行会替换 scope 的工作副本，删除文件的结果也随之保存。失败、超时、超限不覆盖上次成功状态。
5. 未修改的继承文件或显式输入不算本次产物，不再次投递。新增或发生内容/mtime 变化的文件才进入产物候选。

旧的“先把文件写到 `ensure_sandbox(config).work`，随后运行时隐式读取”不再有效；应显式传 `input_files`，或在固定 scope 的前一轮生成文件。原 `test_read_inside_work_is_allowed` 已迁移到显式输入合同，并没有删除读取工作输入的能力。

## 目录与隔离

配置的沙箱根目录下：

| 路径 | 用途 |
|---|---|
| `runs/<run_id>/work/` | 本次独立工作目录 |
| `runs/<run_id>/home/` | 本次独立 HOME / USERPROFILE |
| `runs/<run_id>/tmp/` | 本次独立 TEMP / TMP |
| `runs/<run_id>/_catty_launcher.py` | 本次独立启动脚本，执行后移除，不占产物配额 |
| `scopes/<scope_hash>/work/` | 仅父进程维护的成功工作副本 |
| `exports/<run_id>/...` | 本次产物的独立字节副本，供延后投递 |
| `packages/` | 共享安装目录，仍由 `install_packages` 安装 wheel |
| `work/`、`home/`、`tmp/` | 保留旧 API 路径及历史文件清理兼容；不是新 run 的输入源 |

每次返回唯一 `run_id` 与 `work_dir`；`sandbox_root` 仍是配置根目录。运行代码只可访问自己的 run 目录、读取共享 packages 和既有 Python runtime 范围；不能读取其他 scope/run 或写入 exports。共享 packages 不能被普通运行改写，安装通道和原包名单不变。扫描不跟随符号链接、junction 或其他 reparse point。

这是既有的用户态 best-effort 沙箱，不是容器或操作系统安全边界；共享安装环境也不是每 scope 独立的包版本环境。

`ensure_sandbox` 的共享 launcher 模板在初始化锁内完成读取、比较和原子发布；并发初始化不会同时读/替换同一文件。实际子进程运行各自的私有 launcher，不打开共享模板，从根本上消除 Windows 首次并发运行的文件分享冲突，而不是捕获权限错误重试。

## 正常产物与失败产物

- 正常完成：`ok=true`，`files` 为本次可交付产物，`partial_files=[]`。
- 非零退出或超时：`ok=false`，`files=[]`；已生成的可收集文件只放入 `partial_files` 供诊断，**不得自动加入 QQ 投递队列**。
- 工作大小超限：清理本次 run，`files=[]`、`partial_files=[]`；不删除其他 run、此前 exports、scope 的成功状态或 packages。
- 单文件大小、返回文件个数仍使用已有上限。超过单文件或数量上限的文件不进入返回列表，不影响其他运行。
- 条目保持 `path` / `name` / `size`，增加 `run_id`。`path` 指向 `exports/<run_id>/...`，不是可继续编辑的工作文件。
- 导出是字节复制，不是硬链接；后轮同名覆盖、scope 更新和工作文件修改都不会改变待投递快照。这里的“不可变”是沙箱 API 与后续运行的合同，不是对本机管理员修改文件的禁止。
- 取消仍终止并等待子进程，取消 stdout reader 后等待其结束，再向上抛出取消；不会把取消当正常成功或投递。

工具层只应把 `ok=true` 的 `files` 放进 `pending_file_deliveries`。`partial_files` 可以给模型诊断，但不应转成成功交付回执。

## stdout / stderr 与线程

stderr 合并至 stdout。读取器持续 drain 管道，不落盘完整 stdout；内存只保留固定上限的头部与滚动尾部。字符截断也保留两端，中间使用 `... [stdout truncated] ...`，并返回 `truncated=true`，因此长日志后的 traceback / 最终异常仍可见。

准备工作目录、复制输入、计算内容基线、扫描大小、导出快照、保存 scope 副本都在后台线程执行，不在事件循环同步遍历大目录。文件系统线程完成前不释放本次运行的活动归属，避免取消时与清理并发删除。

## TTL 与容量

`cleanup_code_sandbox(config, *, now=None) -> int` 保持同步维护 API；异步循环必须通过 `asyncio.to_thread` 调用。它清理：

- 旧布局 `work/home/tmp` 的过期文件；
- 新布局 `runs`、`exports`、`scopes` 的过期完整目录；
- 中断删除留下的 `.expired-*` 回收目录。

新布局按父进程维护的运行完成/副本更新时间（目录 mtime）判断 TTL，而不是继承输入文件的旧 mtime；活动 run、export 和 scope 被跳过。父进程先在短锁内重命名待回收目录，再在锁外扫描和删除，避免大目录删除阻塞新运行的注册。`packages` 不参与 TTL。

快照在 TTL 内保持稳定；交付队列必须在 TTL 内消费。这里没有无限保留或“已投递确认”租约协议。容量上限作用于当前 run 的所有普通文件（含 work/home/tmp 及 run 根下文件），不是整个共享根目录；各 run/export/scope 的总磁盘占用由 TTL 回收控制，亦非操作系统级实时磁盘配额。

## 回归测试与验证边界

新增 `tests/test_sandbox_artifact_isolation.py`，只用临时目录和合成数据，覆盖：默认顺序运行仅收本次文件、同 scope 连续读改、两个 scope 顺序/并发隔离、同 scope 串行、显式输入、同名快照不变、失败/超时 partial、头尾输出、有界持续 drain、线程扫描、TTL 与超限不删其他 run、取消 kill/wait。

兼容测试在 `tests/test_code_sandbox.py`；其中 `SandboxRunTests.test_read_inside_work_is_allowed` 已改用显式输入。不要为了本次验证执行现有 `test_project_config_is_not_readable`（它引用真实项目配置）。

2026-10-26 本地验证：兼容沙箱测试 14 项、隔离与产物测试 22 项全部通过；真实配置读取负例未执行。首次并发测试发现的 Windows launcher 发布冲突已修复并复测通过。整体离线回归 465 项、0 失败、0 错误、9 项原条件跳过；未部署。