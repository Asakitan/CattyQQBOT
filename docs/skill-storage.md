# 技能存储合同

实现：`src/catty_qq_ai/skill_store.py`。本层只有标准库依赖，不导入 NoneBot、人格注册表、网络客户端或 LLM。

## 目录与 API

`SkillStore(root: str | Path, *, max_body_chars: int = 6000, max_bytes: int = 65536)`

- `root` 是技能目录本身；应用层通常传 `data/skills`，不会再自动追加一层 `skills`。
- 构造只保存配置、取得同根目录的 `RLock`，不创建目录、不扫描、不加载正文。
- 不存在的库可以检索为空、读取为 `not_found`，这两种操作不创建目录；首次保存创建目录。
- 每条技能是 `<root>/<id>.json` 一个完整 UTF-8 JSON 文件，不拆成索引和正文两个文件。
- 这是项目内部JSON合同，不是直接兼容Agent Skills的 `SKILL.md` 目录格式；采用按需披露思路，并按要求将元数据也推迟到检索后。默认 `data/skills/` 已被Git忽略。
- `.skills.lock` 是跨进程锁文件，不是技能、索引或数据快照，释放锁时不删除。

### `search(query: str, *, limit: int = 3) -> dict`

成功返回 `{"ok": true, "candidates": [metadata]}`。

每个 metadata **只有** `id/title/description/keywords/sha256/body_chars/updated_at/sources`，不含正文、路径或内部评分。

- 查询必须是非空白字符串，最多 1000 字符；`limit` 必须是正整数，超过 5 截为 5，不接受布尔值。
- 检索只匹配 `id/title/description/keywords` 的 ASCII 单词及中文 bigram，忽略英文大小写。独立单字中文可匹配独立单字；不对正文、来源 URL、来源标题做检索。
- 分数为交集 token 数，零相关项不返回；同分按 ID 排序，不为凑满数量补结果。
- 每次从磁盘刷新以看到其他实例/进程的提交；JSON 的正文只在读取、校验和计算字符数时短暂存在，实例缓存中只有 metadata。
- 返回 metadata 是深拷贝，调用者修改返回值不修改缓存或磁盘。
- 目录最多枚举 4096 项（含非 JSON 项），每次扫描预算 16 MiB（含单文件溢出探测字节）；单文件限制为 `max_bytes`。超过整体预算返回 `scan_limit`，不把截断后的结果伪装成完整检索。
- 坏 JSON、重复键、非法编码/字段、超大项、文件名与 ID 不符或不安全文件只隔离该项，不保留它的旧缓存；其他合法记录仍可检索。隔离是忽略，不自动删除或改名原文件。

### `load(skill_id: str, *, expected_sha256: str) -> dict`

成功返回 `{"ok": true, "skill": {...}, "sha256": "..."}`。

- 只接受安全 ID，不接受文件路径。
- `skill` 包含六个必填定义字段，以及 `visibility/schema_version/created_at/updated_at`。
- 必须提供上次检索或保存得到的 SHA；比较磁盘当前完整文件字节的 SHA-256，缺失返回 `not_found`，版本变化返回 `sha_conflict`，内容结构损坏返回 `invalid_record`。
- SHA 输入接受大小写十六进制，输出为小写。SHA 在返回信封或 metadata 中，不写入被哈希的 JSON 自身。
- 不把加载出的正文放进实例缓存，不进行自动重新检索或自动加载不同版本。

### `save_verified(definition: dict, *, overwrite: bool = False, expected_sha256: str | None = None) -> dict`

成功返回 `{"ok": true, "status": "created" | "updated", "id": "...", "sha256": "..."}`。

这是**可信执行层内部 API**。上层负责核实实际抓取回执、生成可共享技能、排除原始聊天/私有数据；本层的字段校验不能证明来源真实，也不能替代上层生成器。不得直接暴露成允许工具提交任意正文的接口。本层不调用新 LLM、不下载 URL。

定义必填字段：

| 字段 | 合同 |
|---|---|
| `id` | 1–64 字符，小写 ASCII 字母/数字组成，以单个连字符分组；不接受大小写自动转换、下划线、路径、`..`、首尾/连续连字符、Windows 设备名 |
| `title` | 非空白字符串，最多 100 字符 |
| `description` | 非空白字符串，最多 300 字符 |
| `keywords` | 列表，0–8 个非空白字符串，每项最多 80 字符 |
| `body` | 非空白字符串，最多 `max_body_chars`，默认 6000 字符；保留正文空白/换行，不接受 NUL 或不能编码为 UTF-8 的字符串 |
| `sources` | 1–3 个来源回执，每项恰为下表四个字段 |

来源字段：

| 字段 | 合同 |
|---|---|
| `url` | 非空 HTTP/HTTPS URL，最多 2048 字符，有主机名，不含凭据、控制符或空白；本层不联网验证目标 |
| `title` | 非空白来源标题，最多 300 字符 |
| `fetched_at` | 带时区的 ISO 时间，保存为 UTC；仅表示**抓取时刻，不是发布日** |
| `content_sha256` | 可信执行层抓取内容的 64 位十六进制 SHA-256，保存为小写 |

- 持久化 `visibility` 始终为 `global`，`schema_version` 始终为整数 1。
- 兼容输入中的 `visibility/visible/schema_version/created_at/updated_at`，但不采用它们控制范围、版本和服务器时间；其余未声明字段拒绝，不持久化顶层自 SHA。
- 服务器生成 UTC `created_at/updated_at`；更新保留原 `created_at`，使 `updated_at` 比前版至少晚 1 微秒。调用者提供的时间不能伪造创建/更新时间。
- `overwrite=false` 只允许新建；存在返回 `already_exists`，传入期望 SHA 返回 `unexpected_sha256`。
- `overwrite=true` 必须提供 `expected_sha256` 且文件已经存在；以磁盘真实版本比对，不要求同实例事先检索或加载。不匹配返回 `sha_conflict`，不自动覆盖。
- 字符限制与 UTF-8 JSON 总字节限制独立；序列化后超过 `max_bytes` 返回 `record_too_large`。

## 并发、原子事务与文件边界

- 同根目录共享进程内 `RLock`；存储操作另持 `.skills.lock` 的 OS 文件锁（Windows `msvcrt.locking`，POSIX `flock`），协调多个会话、实例和进程。
- 修改在锁内比较期望 SHA，并在替换前再次检查目标版本；不依赖 metadata 缓存判断冲突。
- 新文件使用同目录唯一 `mkstemp`：完整写入 → `flush` → `fsync` → 版本复核 → `os.replace`。仅替换成功后返回提交回执；校验、写入、`fsync` 或替换失败不会先删除旧文件。
- 临时文件失败时尽力清理。替换是事务提交点，不在提交后执行可能把已提交操作报告为失败的额外持久化步骤。这里不承诺目录项在所有文件系统/硬件突然断电时均已持久化。
- 根路径及其祖先、锁文件、记录读取都检查 symlink 和 Windows reparse-point（含 junction）；记录必须为普通单链接文件，拒绝目录、硬链接和越界 ID。读取前后比对打开对象与文件身份，支持平台使用 `O_NOFOLLOW`。
- 锁协调遵循本协议的进程，不是对任意本机进程的访问控制。运行账号必须控制技能目录与祖先的修改权限；不要把它作为隔离有权并发替换目录树的恶意本机进程的沙箱。
- 所有公共操作失败返回稳定 `{"ok": false, "error": "code"}`，不输出异常消息、正文、凭据或绝对路径。构造参数类型/范围错误则为固定文本 `ValueError`。

主要错误码：`invalid_id`、`invalid_definition`、`invalid_title`、`invalid_description`、`invalid_keywords`、`invalid_body`、`invalid_sources`、`invalid_query`、`invalid_limit`、`invalid_sha256`、`invalid_overwrite`、`expected_sha256_required`、`unexpected_sha256`、`already_exists`、`not_found`、`sha_conflict`、`invalid_record`、`record_too_large`、`unsafe_path`、`scan_limit`、`lock_unavailable`、`io_error`。

## 验证入口与未验证范围

`tests/test_skill_store.py` 通过独立包壳直接加载模块，不触发 NoneBot。标准库测试入口为 `python -B -X utf8 -m unittest discover -s tests -p test_skill_store.py -v`。

2026-10-27本地28项全部通过，覆盖metadata/无正文缓存、相关性、跨实例读取更新、SHA冲突、失败保留旧文件、同步顺序、路径/symlink/reparse/hardlink、扫描上限、坏记录、线程与独立解释器并发。真实symlink/hardlink不可用的平台用例会显式skip；本次该模块无skip，reparse属性模拟仍不等于真实Windows junction实测。上层共享内容质量、生产部署和断电恢复不属于本存储合同验证。