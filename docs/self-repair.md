# Harness 自修复与升级

修复器是独立于 NoneBot 的进程：机器人崩溃时仍能读取日志、请求模型、生成代码候选。它修改的是本仓库的 harness 源码，不是模型权重，也不替代当前人格语气进化功能。

## 配置入口

`self_repair.enabled` 的模板默认值为 `false`，关闭时不会发送修复请求；当前生产及本地配置均已启用。启用后若 `self_repair.model` 为空、缺省或 `TARGET`，整条模型路由继承各自 `ai` 的 `base_url`、`api_key`、`model` 和 `http_proxy`；主模型或凭据变化会触发重新加载。仍可填写独立的 `model`、`base_url`、`api_key` 及可选 `http_proxy`，显式模型不会与主 AI 路由混用，也不会在失败后切线。本机已改为显式 opencode zen go 的 `deepseek-v4.1-flash`，继承关闭。

支持 OpenAI-compatible Chat Completions 和 `/responses` HTTP，模型需支持函数工具调用并返回完整输入/输出 `usage`。Responses 使用 `store=false`，逐轮回放带加密推理项的完整上下文；不借用主聊天 WebSocket 连接或续聊缓存。首次部署需让新版 `scripts/catty_hot_reload.py` 重启一次，此后 `self_repair` 配置由守护和 worker 热读。

| 配置 | 默认 | 用途 |
| --- | --- | --- |
| `model` / `base_url` / `api_key` | 空 | `model` 未指定时使用主 AI 路由；指定时使用独立模型 |
| `http_proxy` | 空 | 独立模型的 HTTP/SOCKS 代理；继承主 AI 时使用 `ai.http_proxy` |
| `weekly_token_budget` | `100000000` | 1E＝一亿；UTC+8 周一 00:00 开始的自然周，可下调 |
| `auto_apply` | `true` | 验证通过后交守护进程应用；`false` 只留候选和报告 |
| `max_input_tokens` / `max_output_tokens` | `65536` / `8192` | 每次请求的输入包络与输出上限，按两者之和预留 |
| `max_rounds` | `16` | 每个修复任务最多模型轮数 |
| `poll_seconds` / `debounce_seconds` | `15` / `30` | 日志轮询与故障合并等待 |
| `cooldown_seconds` | `21600` | 相同故障六小时内不重复发起模型任务 |
| `log_paths` | `bot_live.log`、`hot_reload.log`（均在 `logs/`） | WARNING / ERROR / CRITICAL 触发来源 |
| `max_files` / `max_patch_bytes` | `6` / `200000` | 一次源码修复的文件数和字节上限 |
| `test_modules` | 8 个固定离线模块，见示例配置 | 应用前冻结并执行的回归集合 |
| `health_grace_seconds` / `health_timeout_seconds` | `60` / `180` | 新版本的稳定观察窗口与启动验收期限 |

其它完整默认值见 `config.example.json`。这段配置由守护/修复进程直接读取，不添加插件的 `CATTY_*` 环境变量；修改配置不会因为文件监听而杀掉机器人。

## 执行流程

1. 增量读取日志，合并异常堆栈，替换已知凭据及常见认证字段；按故障指纹去重和冷却。
2. 独立周账本先预留，每个实际模型请求单独结算。超时、网络异常、缺少或不可信 usage 保留预留，不因重启或跨周清零。
3. 在 `data/self_repair/runs/` 创建源码候选。模型可以查看、搜索、精确替换及新增允许的 Python 文件，不能通过工具执行任意 shell 命令。
4. 运行原始副本与候选副本的固定测试。测试自身不能由模型修改；新的失败、错误、跳过或丢失测试阻止发布。
5. 守护进程停掉旧机器人后，复核磁盘哈希、保存备份、逐文件原子替换，只重启一次。验证新进程启动标记和持续心跳，失败则回滚；未完成的事务在守护下次启动前恢复。

默认源码范围是 `src/catty_qq_ai/`、`bot.py`、`catty_config_loader.py`、`catty_integrations.py`；`allowed_paths` 可进一步缩小。修复控制器、账本、测试、脚本、实际配置、用户记忆、运行数据及人格正文文件不向修改工具开放。候选不复制 `config.json` 或真实用户数据。

候选副本隔离的是代码版本，**不是操作系统安全沙箱**；校验会在本机子进程执行候选 Python。自动代码验证不能证明所有业务行为正确，也不证明真实 QQ 收发正常。

## 状态与操作

沿用 `start_catty.bat` → `scripts/catty_hot_reload.py` 常驻入口；独立 worker 由守护管理。直接运行 `bot.py` 或设置 `CATTY_NO_HOT_RELOAD=1` 不会启动源码自动应用链路。

`scripts/catty_self_repair.py` 提供状态查询、单次处理及显式升级任务入口；它只生成并验证候选，在线应用由守护完成。运行状态、周账本、故障游标、验证报告和回滚备份集中在 `data/self_repair/`，该目录已加入 Git 忽略。

| 参数 | 行为 |
| --- | --- |
| `--status` | 只读查看启用状态、待发布标记及最近结果，不发送模型请求；预算若存在于最近回执中则随回执展示 |
| `--once` | 处理一次已满足合并等待的日志事件，然后退出 |
| `--request "升级目标"` | 显式提出 harness 升级目标，使用相同的周预算、候选验证及发布流程 |
| `--root PATH` | 指定 Catty 根目录；不指定时使用脚本所在项目 |

手动处理和常驻 worker 共用进程锁；worker 正在运行时，手动处理返回 `busy`，不另开并发修复。手动模式可在停止守护后准备候选，再由重新启动的守护消费 `ready.json`。`--status` 不受此锁限制。`auto_apply=false` 只生成候选与报告，不写待发布标记。

主 AI 和独立模型均未配置时保持等待；关闭 `enabled` 后停止新请求和待应用候选。已经开始的发布仍完成健康验收或回滚，不将半发布状态留给机器人。

## 验证与部署

八个 `test_self_repair_*` 模块共 195 项通过，已纳入 `scripts/run_offline_regressions.py`。其中 4 项进程验收实际启动本机模拟 HTTP 模型、worker、守护和临时 bot，覆盖日志触发、工具修改源码、真实测试、重启确认，以及升级进程以 23 退出后的自动回滚。

本地相关回归 255 项、远端隔离包 195 项、部署后远端相关回归 160 项通过。生产源码 428 文件隔离快照的基线与候选各 191 项默认测试比对通过，保留未复制真实配置产生的两个缺文件基线错误。生产主 AI 经代理完成两轮合成工具协议探针；启用后真实日志触发两次处理，一次无改动、一次 `RemoteProtocolError`，均未发布，真实 QQ 会话及真实故障修复效果未验证。

生产配置已添加 `"self_repair": {"enabled": true}`，本地配置也已启用；两端保留各自的主 AI、代理与密钥，完整示例见 `config.example.json`。首次失败探针按保守规则计入 34816 token，加上两轮成功探针的 739 token，启用前周账本占用 35555 token。启用后一次采样占用 148600 token，含 1 笔连接中断后的未结算预留。

本地及远端配置合并测试各 5 项通过；原生产配置保存在仅管理员及 SYSTEM 可访问的备份目录。替换后目标配置的原访问主体和权限恢复为显式规则，继承状态由开启变为关闭；未复制或提交真实配置。

此前完整离线回归 1653 项、0 failures、42 errors、9 skipped；剩余错误均为既有两个群聊集成模块的 `_extract_incoming_with_voice` 未注入。完整证据见 [会话 17](session-log/session-17.md) 和 [会话 18](session-log/session-18.md)。

模块细节：[周预算](self-repair-budget.md)、[日志](self-repair-logs.md)、[模型与 worker](self-repair-agent.md)、[候选、验证与发布](self-repair-workspace.md)。