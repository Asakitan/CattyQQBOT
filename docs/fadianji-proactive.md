# 机机独立主动群聊

## 状态与边界

已部署到 `D:/CattyQQAI`，28个交付文件双端 SHA-256 一致；推送后 03:28:38 插件加载、03:28:41 OneBot 重连。只同步机机普通群聊触发和主动例外提示，核心身份、NSFW及其它人格保留；未改真实 `config.json`、历史/记忆数据，不启用旧笨猫冒泡链路。

新规则只适用于解析后 `persona.name == "fadianji"` 的群。普通回复可由机机关键词、真实 @、引用或已有续聊触发；主动冒泡是另外的独立调度机会，不为群友强加新续聊窗口。`proactive.enabled=false` 不阻断 `fdj_proactive.enabled=true`，旧 `proactive` 配置段和旧人格行为保持各自作用域。

主回复/签到积分/复读让位使用同一唤醒判定。单独发「机机」「机机：」保留真实原文，不再被空前缀丢弃；`message_utils.preserve_bare_prefix` 只由机机群适配显式开启，默认行为不变。普通跟进消耗两条预算，新显式唤醒重置；关闭旧filter不截断已存在的文本续聊，广播、权限、自介及旧图片守卫仍检查。本体避让等其它既有准入规则没有撤销。

## 使用与旋钮

缺少 `fdj_proactive` 段时使用下表默认值，不必为启用默认行为改写真实配置。需要关闭时，将配置短节 `fdj_proactive.enabled` 设为 `false`；也可使用对应环境变量 `CATTY_FDJ_PROACTIVE_ENABLED=false`。JSON 中显式配置按原 loader 规则覆盖同名环境变量，缺省字段保留环境变量/模型默认。

所有运行时字段均为 `catty_fdj_proactive_<短字段>`；`.env`/环境变量名为其全大写形式，例如 `CATTY_FDJ_PROACTIVE_DAILY_MIN`。模板 `config.example.json` 与 loader 默认段具有相同的十二个字段。

| 短字段 | 默认 | 含义与验证 |
|---|---:|---|
| `enabled` | `true` | 机机独立开关，不借用 `proactive.enabled` |
| `dir` | `data/fdj_proactive` | 每群独立 JSON 状态目录；非空且不含 NUL，JSON 相对路径以配置目录为基准 |
| `daily_min` | `5` | 每群每天随机机会数下限，整数范围 5–10 |
| `daily_max` | `10` | 每群每天随机机会数上限，整数范围 5–10 |
| `min_interval_minutes` | `60` | 两次机会的最小间隔分钟数，有限正数 |
| `check_interval_seconds` | `60` | 调度检查间隔秒数，有限正数 |
| `active_window_minutes` | `15` | 活跃判断的近期窗口，有限正数 |
| `active_min_messages` | `5` | 活跃窗口内真人消息门槛，正整数 |
| `warm_window_minutes` | `360` | 少数起话题机会仍要求此窗口内有群友活动，有限正数 |
| `night_chance` | `0.2` | 一天有夜间机会的概率，有限数且范围 0–1 |
| `timeout_seconds` | `30` | 生成与单次发送各自的墙钟上限，不是共享总预算；有限正数 |
| `failure_backoff_minutes` | `60` | 失败退避分钟数，有限正数 |

`daily_max < daily_min` 或 `warm_window_minutes < active_window_minutes` 时 `Config` 验证拒绝，不交换、不钳位、不静默校正；环境变量字符串经正常配置解析后遵循同一验证。原有技能配置、技能按需披露及联网搜索去冷却不受此切片影响。

## 调度合同：机会不是发送保证

- 固定 UTC+8 日界线；每天 08:00–22:00 随机分散机会，不在午夜集中派发。
- 默认 20% 的日期允许至多一个 22:00–24:00 夜间机会；00:00–08:00 不冒泡。
- 至少 75% 的机会必须满足近 15 分钟至少 5 条真人消息；少数 `topic` 起话题机会仍须 6 小时内有群友活动。窗口和门槛按上表配置取值。
- 每活跃群各自随机 5–10 个目标/机会，不是必须发满 5–10 条。冷群、错过窗口、无合适话题或失败可以少发或不发，不临近午夜补数。
- 生成文本、取得机会、状态落盘、调用发送接口、已确认送达是不同状态。`NO_REPLY` 不是发送；超时/异常或缺少成功回执不等于发送成功。发送结果未知时不能声称已送达，也不能仅因没拿到回执就认定未发送并盲目重发。

调度状态存放在新的 `data/fdj_proactive` 每群JSON中，不复用旧笨猫配额。目录已被Git忽略，runtime通过线程调用全部持久化方法。

## 分层与纯提示 API

- 统一入口适配层：沿用现有事件、当前群人格解析、已有续聊和群上下文；不在入口堆另一套人格/历史系统。
- 独立 runtime：协调当前群快照、生成超时、发送回执及失败退避，不把工具执行伪装成闲聊。
- scheduler：负责每群日期、机会、活跃度、时段与持久化状态；不承担人格文本拼装。
- `fdj_proactive_prompt.build_fdj_proactive_messages`：纯消息构造，不扫描磁盘、不初始化 NoneBot、不访问配置或网络、不调度或发送。

调用参数为 `persona` 及关键字 `scope`、`mode`、`recent_messages`、`summary=''`、`now=None`、`no_reply_marker=NO_REPLY_MARKER`。仅接受机机和非空 `group:` scope；`mode` 仅为 `join` 或 `topic`。`now` 缺省不读取时钟；传入无时区时间按 UTC+8 解释，带时区时间转换为 UTC+8。

人格段使用 `persona.core_persona`、`persona.conversation_prompt_for(scope)`，并按既有规则渲染 `chat_rhythm` 的 `{split_marker}`；不附加笨猫主动模板，也不删改机机原有猫相关引用或NSFW文案。复用 `request_intent.append_context_request_hint(messages, ())` 的统一上下文/来源/真实回执规则，不暴露工具。其后用本轮主动短聊任务限定背景，避免把旧请求当成新执行任务。

调用方只传当前群按时间升序的快照。输入记录包含普通字符串 `speaker`、`text` 和布尔值 `is_bot`；异常对象、空名字/文本、非布尔标注均丢弃。可选 `scope` 不匹配当前群时丢弃。只检查最后 20 个位置，最新消息优先；含摘要、名称、转义及 JSON 结构的完整背景不超过 4000 字符。截断不修改原输入，名字最多 80 个 JSON 字符、摘要最多 800 个 JSON 字符。此预算不截断可信人格核心。

输出每项只有 `role/content`。人物名、消息与摘要仅在独立 `user` JSON 背景中，不提升为系统指令；这种结构化分离是上下文卫生措施，不宣称能绝对消除模型受注入影响的风险。`join` 接当前话题、不擅自回答历史任务；`topic` 起一个群友容易接的话题、不编造实时资讯或自身真实经历。本轮仅一条一行普通短聊、总字数 ≤50，不用工具、不声称联网或后台动作、不用固定开场池；不合时宜或没话可说输出 `NO_REPLY`。空背景合法，不虚构场景补数。

## 验证记录

`BubbleHost` 显式注入配置、人格、bot、recent消息、busy检查、生成、发送、清洗和确认后记账。确认群缓存5分钟，失败不回退到未经确认的群，不调用会删除群记忆的旧候选路径。生成前忙碌仅延后，生成后出现主回复则跳过；正常新真人发言允许插嘴，新增bot回复或原上下文全部消失时放弃。

生成和发送分别限时；实际发送前再次检查slot/UTC+8日期，避免claim线程跨午夜后发出。claim取消时排空同一线程并结束为failed；发送已开始的异常/取消/超时或畸形ACK记unknown，绝不重试。有效ACK才写recent，finish线程也在取消时收尾。文件I/O收尾不受生成/发送超时强行截断。

离线用例：`tests/test_fdj_proactive_config_prompt.py`，使用独立包壳直接导入模块，覆盖十二项默认/环境映射/模板同构、旧开关关闭时新默认独立、范围拒绝、真实 Persona 接口、模式和输出限制、统一上下文提示、无工具 schema、有界截断、异常对象、跨 scope 隔离、注入资料分离、无 I/O 和空背景。

新增104项本地全通过（调度40、配置/提示20、集成44），含真实scheduler/runtime/临时状态文件往返和重建去重。完整58模块989项仅1项既有浮点deadline失败、9跳过、0错误；消息抽取另26项通过。远端Shell工具本次不可用，远端只做逐文件 SHA-256 校验，未跑远端回归；未运行真实模型或QQ实发。见 [会话08](session-log/session-08.md)。