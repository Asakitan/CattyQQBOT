# Catty / Fadianji 主人命令手册

所有管理命令只对 `config.json` 中配置的主人 QQ 生效。其他用户发送同样文本时，不会获得管理权限。

`/meme_clear`、`/timeline_clear`、`/adaptive_clear` 不接受参数；带参数时显示用法，不清空数据。人格覆盖文件无法解析时保留原文件并禁止写入，需修复或恢复该文件后重试。

### HTTP 管理接口（本地候选，未部署）

`/dashboard`、`/dashboard/state`、`/dashboard/sse`、`/dashboard/_js_test`、`/dev/sim_chat` 与 `/dev/ambient_inject` 共享保护，不影响 OneBot 路由。未设置 `CATTY_ADMIN_TOKEN` 时只允许直接回环请求，并要求本机 Host 和同源 Origin；不要通过会剥除转发头的本机反向代理转发这些接口。

需要远程访问时，在服务进程环境中设置强随机 `CATTY_ADMIN_TOKEN`；所有客户端包括本机都须使用 HTTP Basic（用户名 `catty`、密码为该令牌）。远程请求须经正确配置的 HTTPS，浏览器登录后同源 SSE 使用相同认证；不要把令牌写入项目配置、日志或链接。令牌未配置时远程请求被拒绝。本地恶意进程及错误配置、剥除来源信息的代理不是回环模式的可信边界。

## 1. Scope、persona 与数据隔离

- **当前 scope**：命令所在的当前私聊或群聊会话；实际 key 由运行时会话配置决定。
- **当前 persona**：按「会话覆盖 → 群配置 → 默认人格」解析；直接命令和主人聊天工具修改的是同一份会话覆盖。内置 `catty`（笨猫）、`fadianji`（机机）保留，并可加载本地 JSON 自定义人格。
- **人格定义与切换指令**：属于机器人共享的人格库，不是每群各一份；保存定义、绑定指令不会自动改变任何会话的人格。执行切换只影响命令或聊天工具所在的当前 scope。
- **跨人格共享**：preferred name、profile facts、user/group notes 使用现有 `MemoryStore`，不按 persona 分开。
- **按 scope + persona 隔离**：lore、scope meme、timeline、adaptive prompt 只修改当前 scope 的当前人格数据。
- **表情隔离**：Catty 使用自己的表情库；Fadianji 只使用 `fadianji/emoji/` 的精选只读表情，不收藏、不联网下载，也不回退到 Catty 表情库。

带删除参数的功能都使用持久化 ID。先执行对应的 `list`/`show` 命令取得 ID，再删除。

含正文的命令统一用半角竖线分隔参数与正文：

```text
左侧参数 | 右侧正文
```

---

## 2. 人格定义、切换与状态

| 命令 | 作用 |
|---|---|
| `/人格` | 查看当前 scope、生效人格、命令覆盖和可用别名 |
| `/人格 列表` | 列出内置及已加载的自定义人格 |
| `/人格 笨猫` | 当前 scope 切到 `catty` |
| `/人格 机机` | 当前 scope 切到 `fadianji` |
| `/人格 <名字>`、`/人格 切换 <名字>` | 当前 scope 切到指定人格；可使用 id 或已注册别名 |
| `/人格 默认`、`/人格 清除`、`/人格 default`、`/人格 reset` | 清除当前 scope 的人格覆盖，回到群配置或默认人格；不删除人格定义 |
| `/人格 查看 <名字>` | 查看名字、版本、路径与核心设定节选；完整 JSON 见路径文件或聊天工具 show 回执 |
| `/人格 导入 \| <JSON>` | 校验并保存新的自定义人格；正文可为多行 JSON |
| `/人格 导入 覆盖 \| <JSON>` | 按当前已读版本更新同 id 定义；不覆盖内置人格 |
| `/人格 生成 <id> \| <设定>` | 用主模型把设定与当前会话上下文生成完整 JSON，校验后保存；**不自动切换** |
| `/人格 生成 <id> 覆盖 \| <设定>` | 结合已有定义与上下文生成更新，校验并通过版本检查后保存 |
| `/人格 指令 <名字> \| <完整指令>` | 给人格绑定一条完整消息切换指令；不会执行切换 |
| `/人格 解绑 \| <完整指令>` | 删除这条指令的绑定，不删除人格或改变当前人格 |
| `/人格 重载` | 重新读取本地人格定义与指令映射 |
| `/catty_status` | 按当前 scope 人格查看状态：笨猫看 daily_life/心情/好感度等，机机看状态机/语料库/本体避让等 |
| `/status`、`/笨猫状态`、`/猫猫状态`、`/机机状态`、`/发电机状态` | `/catty_status` 的别名（显示内容仍由当前人格决定） |

英文子命令也可用：`overview`、`list`、`use`/`switch`、`show`、`import`、`generate`、`bind`、`unbind`、`reload`；`overwrite` 等同于 `覆盖`。子命令的英文大小写不敏感，参数位置与中文相同。参数缺失或未知多参数子命令会返回用法，不会误执行写入；单独的未知名字由人格库返回“不认识的人格”。

`/status` 不写死笨猫：当前 scope 生效人格是 `fadianji` 时输出机机 dashboard（fadianji_state / 续聊预算 / fdj_corpus / 语料旧识 / callback_ledger / body_presence），是 `catty` 时输出笨猫 dashboard。

人格别名：

- `笨猫`、`猫猫` → `catty`
- `机机`、`小机`、`发电机`、`不稳定发电机` → `fadianji`

切人格会隔离不应跨人格继承的会话口吻状态，但不会删除共享的用户画像、preferred name 或 notes。直接命令等待已有回复结束后转场；主聊天中的工具切换先保存当前 scope 的选择，本轮仍用已捕获的人格完成回复，下轮再切换口吻上下文。

### 2.1 导入、生成与保存

最小定义是平铺 JSON，必填 `schema_version: 1`、`name`、`char_name`、`core_persona`；`aliases` 可选。生成命令的 id 用小写字母/数字开头，可包含小写字母、数字、下划线、连字符，长度至多 64；中文显示名用 `char_name` 或别名表达。其它字段须符合 `PERSONA_SCHEMA`，未知字段或错误类型会被拒绝，JSON 不执行代码。

```text
/人格 导入 | {
  "schema_version": 1,
  "name": "sky_guide",
  "char_name": "小岚",
  "core_persona": "你是小岚，温柔的天文爱好者；闲聊简短，解释观星问题时详细。",
  "aliases": ["小岚"]
}
/人格 查看 sky_guide
/人格 小岚
```

也可直接描述：

```text
/人格 生成 sky_guide | 按刚才讨论的观星伙伴设定，叫小岚；闲聊简短，讲星空知识时耐心解释。
/人格 生成 sky_guide 覆盖 | 保留原身份，把普通闲聊改得更简短。
```

- **保存 ≠ 切换**：导入或生成成功立即保存，可随后 `/人格 小岚`；没有强制草稿或确认步骤。
- 覆盖现有 id 必须明确给出 `覆盖`（聊天工具为 `overwrite=true`），运行时以先前读取的 `expected_sha256` 检查并发修改；版本冲突需重新读取，不会静默盖掉别人的更新。
- 生成命令使用无工具的主模型路线；默认总预算 60 秒、输出上限 4096 tokens。最多一次格式/字段修复，修复与初次生成共享同一截止时间。失败、超时或校验不通过不保存半成品、不切换人格。
- 生成保留用户设定意图，前文、引用、补充与纠正都可作为资料；不会因最后一句没有重复角色名就拒绝理解。
- 直接生成命令读取当前会话最近40条文本（最多24000字符）作为引用资料；普通聊天使用主回复已有上下文。可直接使用 [JSON 示例](docs/examples/personas/sky_guide.json)。

### 2.2 绑定切换指令

```text
/人格 指令 sky_guide | /小岚
/人格 指令 sky_guide | 进入观星模式
/小岚
/人格 解绑 | /小岚
```

绑定按**完整单行消息**匹配（首尾空白、连续空白与英文大小写按存储规则规范化），不是关键词或子串触发；“请进入观星模式聊聊”不会命中 `进入观星模式`。仍仅真实主人可用，普通用户知道指令也无管理权限。内置 `/人格` 语法优先；绑定冲突不会悄悄覆盖，改绑先解绑再绑定。

### 2.3 文件、权限与重载

- 默认数据根为 `data/personas`，可通过 `persona.dir`（运行时 `catty_persona_dir`）修改。
- 定义文件为 `definitions/<name>.json`；指令映射为 `switch_commands.json`。成功保存与绑定会持久化，进程重启后重新加载；当前 scope 的选择使用既有会话人格覆盖存储。
- 保存使用单文件原子发布；校验失败、写入失败和版本冲突不发布部分定义。重载时坏文件保留该文件上次可用定义，其它有效文件继续加载；首次加载坏文件不会成为可用人格。
- 手工编辑本地文件后用 `/人格 重载` 读取；重载不是切换指令，也不删除共享记忆。内置人格不能被同名 JSON 覆盖。
- 主人校验来自真实 QQ 事件，既用于直接命令，也在聊天工具执行时再次检查；提示词里自称“主人”不获得权限。
- 生成人格的配置键为 `persona.generation_timeout_seconds`（默认60）和 `persona.generation_max_tokens`（默认4096）。目录配置支持重载；数据文件本身在保存或 `/人格 重载` 后读取。

---

## 3. Preferred name 与 profile facts

这些数据**跨 Catty/Fadianji 共享**，目标由 `<qq>` 指定。

| 命令 | 作用 |
|---|---|
| `/profile_show <qq>` | 查看 preferred name 与 profile facts；同时显示 fact 的 `note_id` |
| `/profile_name_set <qq> <称呼>` | 设置 AI 对该用户优先使用的称呼 |
| `/profile_name_clear <qq>` | 清除 preferred name |
| `/profile_fact_add <qq> [category] \| <事实>` | 添加永久 profile fact；category 默认 `profile` |
| `/profile_fact_delete <qq> <note_id>` | 删除该用户的一条 profile fact |
| `/profile_fact_clear <qq>` | 只清空该用户的 profile facts，不影响其他 notes |

示例：

```text
/profile_name_set 123456789 阿明
/profile_fact_add 123456789 hobby | 喜欢机械键盘和音游
/profile_show 123456789
/profile_fact_delete 123456789 note_abcd1234
```

---

## 4. 长期 notes

Notes 也**跨 persona 共享**。`user` note 跟 QQ 走；`group` note 跟当前群走。

### 4.1 用户 notes

| 命令 | 作用 |
|---|---|
| `/note_list user <qq> [category]` | 列出用户 notes，可按 category 过滤 |
| `/note_add user <qq> [ttl_days] [category] \| <内容>` | 添加用户 note |
| `/note_delete user <qq> <note_id>` | 删除指定用户 note |
| `/note_clear user <qq> [category]` | 清空用户全部 notes，或只清某 category |

### 4.2 群 notes

群 note 命令只能在目标群里执行。

| 命令 | 作用 |
|---|---|
| `/note_list group [category]` | 列出当前群 notes |
| `/note_add group [ttl_days] [category] \| <内容>` | 添加当前群 note |
| `/note_delete group <note_id>` | 删除当前群 note |
| `/note_clear group [category]` | 清空当前群全部 notes，或只清某 category |

`ttl_days` 省略时使用默认 TTL；填 `0` 表示永久。数字后的下一个参数视为 category。

```text
/note_add user 123456789 30 schedule | 下个月准备搬家
/note_add group 0 rule | 周五晚上固定开黑
/note_list group rule
```

---

## 5. Scope lore

Lore 按**当前 scope + 当前 persona**隔离。切到另一个人格后看到的是另一份 lore。

| 命令 | 作用 |
|---|---|
| `/lore_show`、`/lore_list` | 列出当前 scope/persona 的 lore |
| `/lore_delete <identifier>`、`/lore_remove <identifier>` | 删除一条 lore，不会误删 meme |
| `/lore_clear` | 清空当前 scope/persona 的 lore，保留 meme |
| `/lore_summarize` | 用当前会话最近文本生成长期 lore |

`/lore_summarize` 会调用主模型；其余 lore 管理命令只操作本地存储。

---

## 6. Scope memes

Scope meme 同样按**当前 scope + 当前 persona**隔离。

| 命令 | 作用 |
|---|---|
| `/meme_list` | 列出当前 scope/persona 的群梗与 ID |
| `/meme_add <key1>#<key2> \| <说明>` | 添加 scope meme；关键词可用 `#`、空格、逗号等分隔 |
| `/meme_delete <id>` | 删除一条 meme，不会误删 lore |
| `/meme_clear` | 清空当前 scope/persona 的 memes，保留 lore |

```text
/meme_add 开庭#法官 | 群友说“开庭”时是在玩群内审判梗
```

---

## 7. Daily timeline

Timeline 是 AI 的真实日程/活动记录，按**当前 scope + 当前 persona**隔离；没有记录时 AI 应保持“不知道”，而不是编造活动。

| 命令 | 作用 |
|---|---|
| `/timeline_list [YYYY-MM-DD]` | 列出当前人格的全部 timeline，或指定日期 |
| `/timeline_add [YYYY-MM-DD] <标题> \| <详情>` | 新增计划；日期和详情均可省略 |
| `/timeline_update <id> <field> <value>` | 更新字段 |
| `/timeline_done <id>` | 将 item 标记为 `done` |
| `/timeline_delete <id>` | 删除 item |
| `/timeline_clear` | 清空当前 scope/persona 的 timeline |

`timeline_update` 支持字段：

- `title`
- `details`
- `day`（真实的 `YYYY-MM-DD`）
- `due_at`
- `kind`（`planned` / `observed`）
- `status`（`open` / `done` / `cancelled` / `overdue`）

```text
/timeline_add 2026-08-12 晚上看直播 | 看完后在群里聊感想
/timeline_list 2026-08-12
/timeline_update timeline_abcd1234 details 改成九点开始
/timeline_done timeline_abcd1234
```

---

## 8. Adaptive evolution prompt

Adaptive prompt 按**当前 scope + 当前 persona**隔离。它不是 system prompt，而是追加到当前用户消息中的 user-role 内容，并明确标记为 `自适应进化prompt`；同一条增强后的用户消息会按原字节进入历史，保证上下文和 prompt cache 连续。

| 命令 | 作用 |
|---|---|
| `/adaptive_list` | 列出当前 scope/persona 的自适应 prompt 与 ID |
| `/adaptive_set <name> [ttl_hours] \| <内容>` | 按 name 新增或更新 prompt |
| `/adaptive_delete <entry_id>` | 删除指定 prompt |
| `/adaptive_clear` | 清空当前 scope/persona 的 adaptive prompts |

```text
/adaptive_set short_reply 24 | 普通闲聊继续保持一句短回复，先接情绪再回答
/adaptive_list
/adaptive_delete adaptive_abcd1234
```

省略 `ttl_hours` 时使用存储默认规则；重复使用同一个 `name` 会更新原条目，而不是无限追加。

---

## 9. 签到、积分与好感度

| 命令 | 作用 |
|---|---|
| `/aff_show <qq>` | 查看积分、等级、经验和签到记录 |
| `/aff_reset_signin <qq>` | 清除今日签到标记，使其可再次签到 |
| `/aff_set_points <qq> <n>` | 将积分设为 `n` |
| `/aff_add_points <qq> <n>` | 增减积分；`n` 可为负数 |
| `/aff_set_exp <qq> <n>` | 设置好感经验并重算等级 |
| `/aff_reset <qq>` | 清空该用户整条 affection 记录 |
| `/aff_force_checkin <qq>` | 无视今日状态，强制记一次签到 |

主人本人仍使用无限积分与最高等级规则。

---

## 10. Vibe 画像

| 命令 | 作用 |
|---|---|
| `/vibe_show` | 查看主人自己的 vibe profile |
| `/vibe_show <qq>` | 查看指定用户的 vibe、话题、消息量和置信度 |
| `/vibe_reset <qq>` | 清空指定用户的 vibe profile；必须带 QQ |

Vibe 与 profile facts 是两套数据：`vibe_*` 管模型提取的交流画像，`profile_*` 管明确的 preferred name 和长期事实。

---

## 11. Evolution 管理

| 命令 | 作用 |
|---|---|
| `/evolve` 或 `#evolve` | 立即执行一次 evolution 流程 |
| `/rollback_evolution [1-30]` | 回滚最近若干天的 evolution 结果，默认 1 天 |

这些操作可能耗时，并可能调用模型或操作演化产物。

---

## 12. 表情收藏

Catty 可使用收藏命令。Fadianji 表情库为精选只读模式，因此在 Fadianji 人格下不会收藏或下载新表情。

图源优先级：本条消息附图 → 引用消息附图 → 最近 5 分钟群图。

支持前缀：

```text
收藏 / 收藏表情 / 收藏这个表情 / 收藏这个 / 收藏图
存表情 / 存这个表情 / 存这个 / 存图
加表情 / 加表情库 / 入库
/saveemoji / /emoji
```

示例：

```text
收藏
收藏 开心 喵呜
收藏#开心#无奈
```

---

## 13. 会话、上下文与缓存

以下命令在主聊天流中短路，仅主人可触发。

### 用聊天管理人格与承接上下文

主人可在正常聊天中让主模型调用 `catty_persona`（查询 schema/列表/定义、保存、切换、恢复默认、绑定/解绑、重载）。模型基于整段会话、引用消息、补充与纠正理解目标，不只看最后一句关键词；非主人无法通过同样说法写入或切换人格。

```text
把我们刚才讨论的小岚设定保存为 sky_guide，先不切换。
就换刚才那个。
给她绑定“进入观星模式”这条指令。
上一条说错了，是普通聊天简短，解释问题还是详细；更新刚才保存的定义。
恢复这个会话的默认人格。
```

保存与切换分别回执；已经成功的操作不因后续一句“好”重复执行。聊天保存直接由主模型填写完整定义，不另起生成规划模型；只有 `/人格 生成` 走专门的有时限生成器。讨论角色、举例或临时模仿口吻不等于永久保存；确实缺少会影响结果的关键参数才追问，不增加一律确认的流程。

### 清空当前会话上下文

```text
reset / /reset / clear / /clear
清空 / 清空上下文 / 重置 / 重置上下文 / 忘掉上文
```

### 列出会话（仅私聊）

```text
/sessions / /会话列表 / /会话
会话列表 / 列会话 / 查看会话 / 看看会话
ai会话列表 / ai列会话 / ai查看会话 / ai所有会话 / aisessions
```

### 清空当前记忆缓存

```text
clearcache / /clearcache
清空缓存 / 清除缓存 / 清理缓存
清空记忆缓存 / 清除记忆缓存
```

### 查看内部记忆

```text
memory / /memory / 记忆
查看记忆 / 看看记忆 / 显示记忆 / 读取记忆
查看人物信息 / 查看群友信息 / 查看人物画像 / 查看群友画像
你记得我什么
```

内部记忆输出可能包含路径和存储状态，只应由主人查看。

---

## 14. 配置开关

主人 QQ：

```json
{
  "owner_forward": {
    "owner_qq": "你的QQ号"
  }
}
```

四类自主写工具在 `config.json` 的 `tools` 段控制：

```json
{
  "tools": {
    "profile_memory_enabled": true,
    "scope_meme_enabled": true,
    "timeline_enabled": true,
    "adaptive_prompt_enabled": true
  }
}
```

这些开关同时影响工具 schema 暴露和执行时二次校验；owner 管理命令仍由 owner matcher 单独保护。
