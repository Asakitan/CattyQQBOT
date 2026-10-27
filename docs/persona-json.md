# JSON 人格定义与事务存储

实现合同见 [persona-contract.md](persona-contract.md)。本页对应 `src/catty_qq_ai/persona_store.py`、`persona_override_store.py` 与离线测试 `tests/test_persona_store.py`；不包含部署或真实聊天验收。

可直接导入 [sky_guide.json](examples/personas/sky_guide.json)。默认目录为 `data/personas`；也可将示例放入其中的 `definitions/sky_guide.json`，再执行 `/人格 重载`。导入和重载均不自动切换当前会话。

## 文件布局与边界

`PersonaDefinitionStore(root)` 使用调用方提供的目录；默认运行时目录由配置层决定，不从当前工作目录猜测：

```text
root/
  definitions/<name>.json
  switch_commands.json
  .write.lock
```

- 构造函数自动读取磁盘，也可显式 `reload()`。读空目录不创建文件；首次写入才创建目录和进程互斥锁文件。
- 定义层只使用标准库，不导入活动插件或人格 registry，不执行 JSON 内的代码。
- 保存成功不切换当前人格；回执固定含 `activated: false`。定义物化、registry 发布和会话切换由调用方负责。
- `definitions()`、`get()`、`list()` 返回独立副本；外部修改返回值不会改动缓存。

## 平铺 schema

`PERSONA_SCHEMA` 是 JSON Schema 2020-12 对象，顶层及结构化对象均拒绝未知字段。`validate_persona_definition(value)` 接收字典并返回规范化普通 JSON 字典；失败抛出 `PersonaValidationError`，其 `field`、`code` 不包含输入正文。

必填字段：

| 字段 | 类型/规则 |
| --- | --- |
| `schema_version` | 整数 `1`，布尔值不算整数 |
| `name` | 去两侧空白、转小写；`[a-z0-9][a-z0-9_-]{0,63}`，拒绝 Windows 设备名 |
| `char_name` | 非空字符串，兼作隐式查找别名 |
| `core_persona` | 非空字符串，原文保持不变，不另设小篇幅上限 |
| `aliases` | 可选字符串数组；去两侧空白，大小写折叠后唯一，按确定顺序排列 |

完整定义的规范化 UTF-8 JSON（含末尾换行）不得超过 **2 MiB**；磁盘原始 JSON 同样按 2 MiB 限制读取。单个 `core_persona` 没有额外的小长度上限。缺少的可选字段保持缺少，不补 NSFW 禁用策略，也不改写任何人格正文。

其它字段与现有 `Persona` 对齐：

| 字段组 | 类型 |
| --- | --- |
| `group_silence`、`first_mes`、`mes_example`、`style_examples`、`disambiguation_examples`、`persona_reminder_text`、`reply_gate_style`、`chat_rhythm` | 字符串或 null |
| `emoji_root`、`emoji_manifest_path`、`emoji_download_dir`、`emoji_guidance`、`model_override` | 字符串或 null |
| `nsfw_gate_skeleton`、`nsfw_style_override`、`nsfw_prefill` | 字符串或 null，按原文保存 |
| `owner_concept`、`cache_full_character_book`、`semantic_harness_enabled`、`mention_only_trigger`、`nsfw_gate_private_only`、`nsfw_freeform`、`pregnancy_private_only` | 布尔值 |
| `emoji_allow_downloads` | 布尔值或 null |
| `followup_reply_budget` | 整数或 null |
| `disabled_prompt_segments`、`disabled_features` | 唯一字符串数组，规范化排序 |
| `nsfw_scopes` | `group` / `private` 的唯一数组，规范化排序；允许空数组，缺省不注入策略 |
| `trigger_prefixes`、`directed_keywords` | 字符串数组或 null，保留顺序 |
| `conversation_prompts`、`nsfw_lexicon` | `[[字符串, 字符串], ...]`，保留顺序 |
| `character_book` | 对象数组或 null；条目必填 `identifier/keys/content`，可选 `order/constant/case_sensitive` |
| `imagegen` | 对象或 null，使用 `PersonaImagegen` 字段；允许部分对象，由 registry 填中性缺省值 |
| `reply_catalog` | 对象或 null，使用 `PersonaReplyCatalog` 字段；允许部分对象，由 registry 填中性缺省值 |

Python 调用方可用 tuple 表达数组、set/frozenset 表达集合字段，返回值仍全部为普通 JSON 数组。数字拒绝 NaN/Infinity；JSON 文件拒绝重复键。`definition_revision` 是运行时计算字段，不能作为输入保存。

内置 `catty/fadianji`、`笨猫/猫猫/机机/小机/发电机/不稳定发电机` 不允许被自定义名称、显示名称或别名覆盖。所有已加载定义的名称、显示名称、别名共享同一大小写不敏感命名空间；同一角色的名称与显示名可以相同。文件名必须严格为规范化 `<name>.json`，拒绝路径穿越和定义目录/文件的符号链接。

## API 与回执

| 方法 | 返回 |
| --- | --- |
| `reload()` | `{ok,status,count,errors,activated:false}`；部分失败为 `status: partial` |
| `definitions()` | `name -> definition` 的深拷贝 |
| `get(name)` | `{name,definition,sha256,path}` 或 None；支持自定义别名，内置不在此存储中 |
| `list()` | 排序后的 `{name,char_name,aliases,path,sha256,builtin:false}` 数组 |
| `save(definition,overwrite=False,expected_sha256=None)` | 成功 `{ok:true,status:"saved",name,path,sha256,activated:false}` |
| `bind(command,name,overwrite=False)` | 成功 `{ok:true,status:"bound",command,name,activated:false}` |
| `unbind(command)` | 成功 `{ok:true,status:"unbound",command,removed,activated:false}`；不存在也成功 |
| `switch_commands()` | `完整规范化命令 -> canonical name` 的副本 |

操作失败回执为 `{ok:false,status,error,field,activated:false}`，`status` 为 `conflict` 或 `error`。错误码包括 `sha_conflict`、`already_exists`、`alias_collision`、`builtin_collision`、`command_collision`、`unknown_field`、`type`、`too_large` 或异常类型名。异常消息、JSON 正文及未知字段原文不写入诊断。

`sha256` 是实际文件字节的 SHA-256，不是格式无关的语义版本；外部编辑缩进也改变版本。新建不能覆盖已有文件。更新必须同时满足：

1. `overwrite=True`；
2. 给出此前读取的 `expected_sha256`；
3. 该 SHA 同时等于此实例的上次有效读取记录和当前磁盘字节 SHA。

另一个实例更新后，直接提供它的新 SHA 仍不够，必须先重载本实例。校验和落盘失败不更新缓存。写入流程为同目录唯一临时文件 → flush → fsync → 提交前再次核验版本 → replace → 发布内存。相同根目录的线程共用锁，协作进程通过 `.write.lock` 排他写入；外部编辑器不参与这把锁，应在编辑完成后重载。

重载对每个坏文件保留当前实例的上次有效定义，其它有效文件照常读取；别名冲突的更新也不会替换旧定义。删除的文件会移出快照。冷启动没有历史内存，因此无法恢复已损坏文件的旧内容，也不会擅自重写坏文件。指令映射作为单文件整体校验，坏映射保留上次有效版本，绑定/解绑不会覆盖尚未修复的坏文件。

## 完整命令绑定

`normalize_switch_command` 去除两侧空白、合并空格/制表符并做大小写折叠；拒绝空串、换行和控制字符。调用方必须拿**完整消息**匹配映射，不按前缀触发，例如 `/use aurora` 不匹配 `/use` 或 `/use aurora now`。

绑定目标允许内置 canonical name/别名或已保存的自定义名称/别名。已有完整命令需要 `overwrite=True` 才能改绑，没有强制确认或草稿阶段。

保留命令来自实际命令解析源码，而非禁止所有斜杠命令：`__init__.py` 的人格、状态、vibe、aff、lore、profile、note、meme、timeline、adaptive、evolution、上下文/缓存/记忆和表情收藏命令；`owner_forward.py` 的申请同意/拒绝/待办命令。保留头的参数形式也拒绝。`/custom`、`/status-themed` 等未占用命令仍可绑定。

## 已有覆盖表的事务修复

`PersonaOverrideStore` 的公开签名与正常返回值不变：`set(...) -> None`、`clear(...) -> bool`。候选数据先写唯一临时文件、flush/fsync/replace，成功后才替换内存；写入异常向调用方传播，不再吞掉 OSError 后假装已切换/清除。持久化位置与 JSON 格式仍是 `memory_path` 同目录的 `persona_overrides.json`。

本地安全修复已随 2026-10-27 并入部署：在独立 `persona_overrides.json.lock` 上使用 Windows `msvcrt.locking` 或 POSIX `flock`；持锁期间重读当前文件、合并单条操作并原子发布。读取按需刷新，读取失败保留已知快照；损坏文件禁止写入。等待锁最长 10 秒，运行时不要删除锁文件；只协调遵循该锁的进程，不保证其他写入者或网络文件系统。

## 离线验证范围

`tests/test_persona_store.py` 覆盖字段与现有 dataclass 的静态一致性、未知字段/类型、原文与策略保留、2 MiB、内置/别名/命令冲突、重启、深拷贝、原子失败、发布顺序、磁盘/上次读取 SHA 双校验、坏文件保留、双实例写入与覆盖表错误传播。测试只用标准库和临时目录，不导入活动插件、不联网。

存储模块44项离线测试全部通过；完整集成清单782项、0失败/错误、9跳过，结果见 [会话03](session-log/session-03.md)。