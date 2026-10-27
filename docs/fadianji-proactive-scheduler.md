# 机机每日冒泡机会调度器

各人格共享本调度器（已合并并部署），同一群日共用计划；调用方按人格选择启用开关，日计划与节奏使用 `fdj_proactive` 配置。其余调度和回执合同不变。

## 范围与接口

实现位于 `src/catty_qq_ai/fdj_proactive.py`，测试位于 `tests/test_fdj_proactive.py`。它是机机专用的新调度与存储层，不启动 NoneBot，不访问网络，不调用模型，不发 QQ 消息，也不修改其他人格的主动行为。入口负责确认当前人格、维护真人活动时间、生成回复、调用发送接口。

固定公共接口：

- `BubblePolicy`：冻结 dataclass；字段与默认值见下表。
- `BubbleCandidate(group_id, day, slot_id, mode, night)`：冻结 dataclass；`mode` 为 `join` 或 `topic`。
- `BubbleScheduler(root: str | Path, policy: BubblePolicy)`：构造时无文件扫描或读写。
- `due(group_id, now, *, human_times) -> BubbleCandidate | None`
- `claim(candidate, now) -> str | None`
- `finish(group_id, attempt_id, outcome, now) -> bool`
- `skip(candidate, now, reason='unsuitable') -> bool`
- `status(group_id, now) -> dict`

所有公共方法同步且同进程线程安全；**包含 `due` / `status` 在内，每个方法都可能执行持久化 I/O，异步入口必须用 `asyncio.to_thread` 调用**。`now` 为调用方传入的有限、非负 Unix 时间；模块不使用 sleep 或真实时间等待。

| Policy 字段 | 默认值 | 语义 |
|---|---:|---|
| `daily_min` / `daily_max` | 5 / 10 | 每群每天抽取机会数，不是保证发送数 |
| `min_gap_seconds` | 3600 | 距最近 claim 与最近成功发送均需满足的滚动间隔 |
| `active_window_seconds` | 900 | 当前热闹判定窗口 |
| `active_min_messages` | 5 | 窗口内至少五条真人消息，不要求五个不同用户 |
| `warm_window_seconds` | 21600 | 自起话题也要在六小时内有真人活动 |
| `night_chance` | 0.2 | 每个群日安排一个夜间机会的概率，不是固定每五天一个 |
| `failure_backoff_seconds` | 3600 | failed / unknown 后从结果记录时刻起算的冷却 |
| `slot_grace_seconds` | 1800 | 机会开始后有效期，另受 22:00 / 24:00 边界限制 |

策略校验允许 `1 <= daily_min <= daily_max <= 64` 的测试/自定义额度；默认始终为 5–10。计数必须是整数，时间间隔必须为有限正数，warm 窗口不得小于 active 窗口。一天仅一个机会的自定义策略不安排夜间。

## 每日计划

- 日期永远为固定 **UTC+8**，不依赖宿主机时区或夏令时。
- SHA-256 输入为版本域、规范化群 ID、日期；用其整数结果初始化独立 `random.Random`。相同 group/day/policy 的计划可重现，不使用 Python 的随机进程 hash，不需要可变全局 RNG 或新增 seed 参数。
- 日间为 **08:00–22:00**，将 14 小时分层，每层中间 40%–60% 范围抖动取一个开始时间。默认日间开始时间至少相距一小时，不在整点统一开闸。
- 最多 `floor(quota / 4)` 个日间机会标为 `topic`，所以至少 75% 的计划机会必须热闹才能触发 `join`。topic 遇到热闹也返回 `join`；否则要求 warm 窗口内至少一条真人活动。
- 每群日按 `night_chance` 概率安排最多一个夜间机会，开始在 22:15–23:30，必须满足 active 门槛。日间机会不跨入 22:00，夜间机会不跨过午夜。
- **00:00–08:00 绝不发送**。午夜可以准备次日元数据，但不触发消息。错过有效期立即标记 `expired`，不追赶、不补发、不将剩余额度搬到下一天。有效期仍在时，群友变活跃后可自然触发。
- 日内读取已持久化的策略与计划；热配置/实例重建不重新抽额度。新策略从下一 UTC+8 日期的新计划开始生效。

真人时间由调用方提供；机器人消息、撤回事件、非消息事件应由入口过滤。本模块不存储这些时间，也不存聊天正文、用户 ID、昵称或调用方传入的 `reason`。未来时间、NaN、无穷和非数值活动会被忽略；窗口左边界包含在内。

## 调用顺序与幂等

1. 入口按群串行处理：用同一 scheduler 实例调用 `due`，无候选就结束该 tick。`due` 不认领额度，也不阻止另一调用方同时拿到相同候选，因此**模型生成阶段的群锁由入口负责**。
2. 有候选后生成文本；NO_REPLY、生成失败或不适宜直接 `skip`，消费本机会而不是下一 tick 再烧一次模型。`skip` 不记发送尝试，`reason` 为兼容参数，不落盘。重复跳过同一已 skipped 机会返回 True。
3. 有可发文本时调用 `claim`。只有非 None attempt ID 才是发送许可；先成功完成 fsync+replace 再返回许可。claim 会重查当日计划、slot 有效期、先前 due 的活动证据有效期、跨日滚动间隔、冷却、pending。
4. **仅发送一次**，然后 `finish(..., 'sent' | 'failed' | 'unknown', now)`。确认送达才记 sent；明确失败记 failed；超时、连接断开且无法确定是否送达记 unknown。任何不确定发送不自动重试。

活动证据是 `due` 在该实例内建立的短期许可；不能绕过 due 自行构造 candidate，也不能把 candidate 交给未调用 due 的新实例 claim。生成期间活动证据已过期时 claim 可能失败，这是安全丢弃而非补发。`skip` 可以消费生成前已经取得、生成后活动证据刚过期的机会；slot 自身已到期则已被 expired 消费。

重复 claim 返回 None，绝不再次返回可发送许可，但不重复修改状态；重复 finish 同一结果返回 True、不改计数或时间，冲突结果返回 False。此处幂等是**状态幂等、发送许可只返回一次**。不存在的 attempt / 不合法 outcome 返回 False。已 unknown 的发送不允许事后重新标成 sent。

## 持久化与恢复

- 每群一个 `<root>/<group_id>.json`，格式版本 1。安全 ID 仅允许开头字母数字、后续字母数字/下划线/短横线，最多 64 字符；拒绝路径、扩展名、Windows 设备名。ASCII 大小写统一为小写，避免 Windows 文件名别名绕过协调。
- 文件记录当前计划、slot 状态、最近 attempt/send 时间、失败冷却、最多 64 个近期发送尝试。历史尝试只保留幂等所需元数据；保留范围外的 finish 返回 False。
- 模块级 `RLock` 串行化同进程所有实例的读取—检查—写入，不使用实例内旧缓存覆盖其他实例写入。此方案要求**同一 root 只由一个 bot 进程拥有**，不是跨进程锁或分布式调度器。
- 每次写入使用唯一 sibling tmp，写入 UTF-8、flush、文件 fsync，再原子 replace；只有成功落盘才发布认领。临时文件尽力清理，不复用固定 `.tmp` 名称。
- 坏 JSON、NUL、结构缺失、计划被改、超大文件、非普通文件、已知文件丢失或 I/O 失败一律 fail closed。原坏文件不覆盖、不自动重置额度；同进程故障按规范化路径锁存，换 scheduler 实例也不能绕过。人工修复后重启进程再检查，不靠删除额度文件“修复”。
- 已知文件丢失检测依赖进程内已见路径；如果人为删除全部状态后再重启，单文件协议无法区分“首次使用”与“历史被删”，也不提供抵御外部旧快照回滚的独立账本。调用方必须保护并备份 root，不能把删文件当作恢复策略。
- 每次加载严格验证计划与尝试的一致性；不信任磁盘内的 sent 数字，所有每日计数由 slot 状态计算。
- pending attempt 保存进程随机 nonce。新 scheduler 实例仍在同一进程时继续看到 pending，不误判为重启；真正新进程遇到旧 nonce 的 pending 必须先持久化成 unknown，才继续调度。恢复写入失败则保持关闭，禁止发送。
- 跨日只换计划，不清 `last_attempt_at` / `last_send_at` / `backoff_until`，也不清活跃进程尚未 finish 的 pending。前一天的 pending 可以第二天结束，更新滚动时间但不消费新一天额度。
- 日期倒退返回 `clock_rollback`，不覆写新日计划。日内时钟回拨也不能重新打开已消费/过期 slot，最近 attempt/send 与冷却会继续挡住发送。
- 该协议保证发送许可 at-most-once，不承诺远端 exactly-once；“发送成功但写 finish 失败”恢复后记 unknown 是刻意的保守选择。文件 fsync+replace 不等于对所有文件系统/断电场景保证目录元数据持久化；调用方应使用正常本地磁盘并保留状态文件。

## `status` 实际字段

正常返回：

| 字段 | 类型 | 含义 |
|---|---|---|
| `ok` / `error` | bool / None | True / None |
| `day` / `quota` | str / int | 当前 UTC+8 日期 / 当前计划机会数 |
| `sent` / `unknown` / `failed` | int | **当前计划**对应终态 slot 数量 |
| `attempted` | int | 当前计划已经 claim 的 slot 数，包含 pending/sent/failed/unknown |
| `skipped` / `expired` | int | 当前计划跳过 / 到期未用 slot 数，不含在 attempted 中 |
| `pending` | str 或 None | 唯一未完成 attempt ID，可能来自前一天 |
| `last_attempt_at` / `last_send_at` | float 或 None | 跨日保留的最近 claim / 确认发送时刻 |
| `backoff_until` | float 或 None | 跨日保留的失败/不确定发送冷却截止 |
| `slots` | list[dict] | 下表所示的独立副本 |

每个 slot 字段：`slot_id`（日期:两位序号）、`at`、`expires_at`（Unix 时间，开始包含/截止不包含）、`mode`（**计划** join/topic，实际候选可从 topic 升为 join）、`night`（bool）、`state`、`attempt_id`（str 或 None）。`state` 是 `open / expired / skipped / pending / sent / failed / unknown` 之一。

存储或时钟异常返回 **`ok=False`、`error`、`day`、`quota=0`、`sent=0`、`unknown=0`、`attempted=0`、`slots=[]`**。这些零表示无法取得可信状态，**不表示有新额度或历史未发送**。错误码为 `storage_error / invalid_state / missing_state / clock_rollback`；错误中没有文件路径、聊天内容或原异常文本。非法 group ID / 非法 now 属于调用错误，会抛 ValueError 而非形成状态文件。

## 验证交接

测试直接用独立包壳加载本模块，仅使用临时数据和注入时间；40 项用例包含 400 个 group/day 计划的配额、75% 活动约束、夜间上限与分布性质，以及活跃/冷群、午夜、过期、跨日间隔、并发认领、重启恢复、幂等、路径、写入/读取失败和隐私元数据检查。

40项本地全通过，无跳过；集成另用真实scheduler/runtime和临时JSON验证发送一次、重复tick不重复发送及重建额度保持。项目回归使用 `scripts/run_offline_regressions.py`，不触发在线评测discover；当前58模块989项仅1项既有persona_admin浮点失败，见 [会话08](session-log/session-08.md)。本模块已随会话08部署，双端SHA-256一致；未做真实QQ发送或跨进程共用root验证。