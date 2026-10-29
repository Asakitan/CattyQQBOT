# 独立修复模型与 worker

## 范围与状态

- `src/catty_self_repair/agent.py`：非流式 Chat Completions / Responses 工具循环、逐请求预算、真实候选验证与发布准备。
- `src/catty_self_repair/worker.py`：配置热读、全局进程锁、日志/手动入口、脱敏状态、`ready.json` 暂存。
- `scripts/catty_self_repair.py`：独立入口，不导入或启动 `catty_qq_ai.__init__` / NoneBot。
- worker 只准备候选；真正应用、健康观察、确认和回滚属于父守护 `supervisor.py`。
- 定向测试使用临时配置/源码、模拟预算/工作区接口、模拟日志监测器与 `httpx.MockTransport`；进程验收另外使用本机 HTTP 模型夹具与真实子进程。生产主 AI 也用合成内容验证了两轮工具协议，未执行工具或发送真实日志；见 [会话 17](session-log/session-17.md)。

## 模型配置

运行时读取根目录 `config.json`。`self_repair.model` 缺省、为空或 `TARGET` 时，启用后使用主 `ai` 段的模型、端点、密钥和 HTTP/SOCKS 代理；未启用不解析主路由。显式修复模型使用自己的路由，不与主 AI 混用；请求失败不自动切模型。额度、日志和验证仍独立。下面是独立模型字段示例；实际默认值以 `RepairConfig` 为准。

```json
{
  "self_repair": {
    "enabled": false,
    "auto_apply": true,
    "base_url": "https://HOST/v1",
    "api_key": "TOKEN",
    "model": "TARGET",
    "http_proxy": "",
    "weekly_token_budget": 100000000,
    "max_input_tokens": 65536,
    "max_output_tokens": 8192,
    "max_rounds": 16,
    "request_timeout_seconds": 120,
    "run_timeout_seconds": 900
  }
}
```

- 支持 `/v1`、完整 `/chat/completions` 与 `/responses`；只有主机而没有路径时补 `/v1/chat/completions`。模型端点不接受 URL 查询串、内嵌凭据或重定向。
- `enabled=false` 不轮询日志、不发送模型请求；主 AI 和独立模型均未配置完整时保持等待。空 `api_key` 仅适用于不要求认证的端点。
- 使用安装好的 `httpx`，显式传入 HTTP/SOCKS 代理；`trust_env=False`、`follow_redirects=False`、传输重试为零。HTTP 失败不会切模型、切端点或自动重试。
- Responses 使用非流式 HTTP 和 `store=false`；每轮返回的加密推理项、函数调用及工具回执原序加入下一轮输入。返回推理项却缺少加密内容、未完成响应或回放含待脱敏内容时停止；没有推理项的正常响应可继续。该流程不使用主回复的 WebSocket lane 或缓存。
- 成功响应中的 `error: null` 按正常用量结算；非空 API 错误、HTTP 错误及不可信用量仍按原有保守规则处理。
- `opencode.ai` 及其子域在同一修复 run 内始终使用 `x-opencode-session=catty-repair-<run_id>`；固定发送 `User-Agent: Catty-Self-Repair/1.0`。
- `extra_body` 仅开放受限 `temperature`、`top_p`、`reasoning_effort`、`thinking`；`thinking` 仅允许 `type` / `enabled` / `budget_tokens`，预算不能超过输出上限。不能注入额外提示、模型、工具、流式开关或 token 限制。
- `extra_headers` 仅允许信息类 `HTTP-Referer`、`X-Title`、`X-Request-ID`、`X-Correlation-ID`；不允许改授权、Host、路由、会话标识、User-Agent 或传输/计费限制。

## 请求与预算

预算数据库为 `data/self_repair/budget.sqlite3`，调用共享 `WeeklyBudget`。UTC+8 周一周界、未决预留跨重启/跨周保留、100M 硬上限与降额持久化由预算模块负责。

每次实际请求的顺序固定：

1. 重读配置并核对指纹；补充新轮换的已知秘密值，并保留旧秘密值用于后续脱敏。
2. 将全部 messages、工具 schema、工具回执和允许的 extra_body 序列化，按完整 UTF-8 字节数加消息/schema/protocol 余量进行输入包络检查。不是按字符除以四，也不宣称是供应商 tokenizer 的精确值；超限直接停止，不偷偷摘要或再发压缩请求。
3. `reserve(max_input_tokens + max_output_tokens)`，持久化 `runs/<run_id>/requests.json`，再发一次非流式、固定输出上限的请求；Chat Completions 显式 `n=1`。
4. 收到有效 `usage` 后立即 `settle`，并持久化结算回执，之后才允许执行任何工具。
5. 标准 prompt/completion 总量包含缓存输入与推理输出，不减去 cached/reasoning；独立 cache-read/cache-write 字段按保守上界计入，互相矛盾的总量取更大值。
6. 没有完整有效 usage、网络异常、取消、超时、无效/超大 JSON 都保留原预留；错误响应即使报告较小 usage 也不退款。超预留或单项超限的 usage 先交预算记账，再停止工具执行。

没有额外隐藏的总结/审计/重试模型请求。正常工具循环每一轮都有独立预留与回执。

HTTP 单响应最多 2 MiB；虽然用 HTTP 分块读取控制内存，API 请求本身仍为非流式 JSON。拒绝压缩响应，避免隐式解压突破上限。单响应最多 8 次工具调用、单工具参数最多 512 KiB、单工具回执最多 24 KiB、源码哈希读取最多 8 MiB；read 文本视图最多 400 行并有更小字节截断。JSON 解析拒绝重复键和非有限数字。

## 六种工具

| 工具 | 参数/约束 |
| --- | --- |
| `list` | `offset`、`limit`，最多 200 个路径/页，仅 `Workspace.list_files()` 返回的模型可读源码 |
| `read` | `path`、`start`、`end`，返回整文件 `sha256` 与有界行视图；读前后字节必须一致 |
| `search` | `query`、`limit`，查询最多 1024 UTF-8 字节、结果最多 50 条 |
| `replace_text` | `path`、`expected_sha256`、`old`、`new`；必须先读，同 SHA 的实际源码中 old 恰好匹配一次，修改后旧读凭据失效 |
| `write_new` | `path`、`content`，通过 `Workspace.write_file` 新建允许范围内 Python 源码，不覆盖已有文件 |
| `finish` | `outcome=changed/no_changes`、`summary`；只能是本响应最后且唯一的结束工具 |

没有 shell/执行代码/联网工具。日志、源码、工具输出和模型历史均作为不可信数据，不能转成新权限或直接执行其中的指令。模型不能编辑测试、控制器、配置、人格或凭据；链接/路径/修改量/源与测试完整性由 Workspace 再次检查。源码改写涉及被脱敏的凭据时拒绝操作，不把隐藏值猜回去。

CLI 生成的 `source=manual`、`level=MANUAL` 事件单独进入 `manual_upgrade_requests`，作为用户升级目标；其余留在 `untrusted_incidents`，不能借故障内容增加权限。唯一替换按 `find == rfind` 检查，重叠的多个匹配也拒绝。源码可以引用认证变量，但不能写入已知凭据、密钥形态的字面量或 `[REDACTED]` 占位标记。

所有已知密钥（含项目其它配置段的秘密值）先脱敏，再进行 JSON 转义和截断；模型文本、有效工具 JSON 参数、日志证据、源码视图、错误回执与状态使用同一脱敏路径。无效工具 JSON 不原样回显。HTTP 错误不记录响应正文、URL 或请求头，异常只报告类型。

## 验证与发布

- 网络/供应商/配置等外因允许 `no_changes`；此状态不声称已经执行验证。声明没有修改但候选实际变更时停止，不发布。
- 真正有修改时调用 `Workspace.validate(deadline=<同一整轮 monotonic 截止>)`，仅其实际返回 `ok is True` 才调用 `prepare_release`。模型写“测试通过”不能改变结果。
- 基线/候选详细失败报告保留在工作区验证产物及脱敏 `runs/<run_id>/run.json`；`status.json` 只保存小摘要和报告路径，不把原有基线失败伪造成全绿。
- 候选目录和测试子进程不是操作系统安全沙箱。无 shell 工具、环境清洗、不可变测试和哈希检查不等于阻止恶意 Python 的全部 OS 行为；不要把这些机制描述成 OS 隔离。
- `auto_apply=false` 仍完成验证与 release manifest 准备，但只报告 `candidate`，不写 `ready.json`。
- 自动发布前再次 `load_config`：必须仍 enabled、configured，且完整指纹一致；取消或配置变化只留下候选。
- `ready.json` 仅包含 `run_id`、根相对 `release_path=data/self_repair/runs/<run_id>/release.json` 和 `config_fingerprint`。拒绝任意外部路径。worker 从不调用 `apply_release`。
- 已有 `ready.json` 或 `active_release.json` 时暂停生成新候选，防止重叠升级；父守护消费 ready 时仍须再校验配置。

## 常驻、取消与 CLI

默认常驻循环每 tick 热读配置。`FileLock(data/self_repair/worker.lock)` 覆盖整个常驻进程；单次 tick 使用同一锁。另一个进程持锁时手动请求返回 `busy`，不会静默排队或宣称已消费。

支持 `--root ROOT`、`--once`、`--status`、`--request TEXT`；`--request` 总是只跑一次且走同一预算、验证与发布门控，不绕过 disabled。超长手动请求拒绝而不静默截断。`--status` 不实例化监测器、不请求模型、不预留预算，只输出白名单脱敏状态。退出码：成功/等待 0、一般失败 1、锁忙 2、预算不足 3、取消 130。

SIGINT/SIGTERM/Windows SIGBREAK 置停止事件；活动运行抛 `RepairCancelled(BaseException)` 解开验证清理栈，后续信号不重复打断清理。HTTP 与工具边界检查整轮截止时间；验证把同一期限传给受控子进程 runner。父守护仍保留限时强制回收自有进程树的最后兜底；取消不释放未知费用。

## 离线验证

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_self_repair_agent.py
E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_self_repair_worker.py
```

覆盖：逐轮 reserve/settle/工具顺序、缓存/推理计费、未知费用、零隐式重试、独立路由与头策略、UTF-8 包络、响应/回执上限、秘密脱敏、先读/SHA/唯一替换、外因无修改、真实验证门控、基线失败、配置变化、预算不足、整轮期限、取消清理、worker 锁、ready/active 暂停、候选模式、父级发布交接与 CLI 状态。
