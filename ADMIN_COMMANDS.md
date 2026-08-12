# Catty / Fadianji 主人命令手册

所有管理命令只对 `config.json` 中配置的主人 QQ 生效。其他用户发送同样文本时，不会获得管理权限。

## 1. Scope、persona 与数据隔离

- **当前 scope**：命令所在的当前私聊或群聊会话；实际 key 由运行时会话配置决定。
- **当前 persona**：按「命令覆盖 → 群配置 → 默认人格」解析。当前可用人格为 `catty`（笨猫）与 `fadianji`（机机）。
- **跨人格共享**：preferred name、profile facts、user/group notes 使用现有 `MemoryStore`，不按 persona 分开。
- **按 scope + persona 隔离**：lore、scope meme、timeline、adaptive prompt 只修改当前 scope 的当前人格数据。
- **表情隔离**：Catty 使用自己的表情库；Fadianji 只使用 `fadianji/emoji/` 的精选只读表情，不收藏、不联网下载，也不回退到 Catty 表情库。

带删除参数的功能都使用持久化 ID。先执行对应的 `list`/`show` 命令取得 ID，再删除。

含正文的命令统一用半角竖线分隔参数与正文：

```text
左侧参数 | 右侧正文
```

---

## 2. 人格切换与状态

| 命令 | 作用 |
|---|---|
| `/人格` | 查看当前 scope、生效人格、命令覆盖和可用别名 |
| `/人格 笨猫` | 当前 scope 切到 `catty` |
| `/人格 机机` | 当前 scope 切到 `fadianji` |
| `/人格 默认` | 清除当前 scope 的命令覆盖，回到群配置或默认人格 |
| `/catty_status` | 查看当前 scope 的综合状态 |
| `/status`、`/笨猫状态`、`/猫猫状态` | `/catty_status` 的别名 |

人格别名：

- `笨猫`、`猫猫` → `catty`
- `机机`、`小机`、`发电机`、`不稳定发电机` → `fadianji`

切人格会清理不应跨人格继承的会话口吻状态，但不会删除共享的用户画像、preferred name 或 notes。

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
