# 会话 38 · 缓存前缀修复与 97–98% 目标

## 目标与范围

用户要求修复会话37发现的缓存问题，并尽量将实际命中推到97–98%。本会话只在本地实现和验证，后续由[会话39](session-39.md)部署；未发收费预热或模型探针，不靠增加无用前缀或改变分母抬高数字。保留NO_REPLY静默语义、既有人格、完整事实和工具权限。

## 已完成

- 官方受支持Responses新增`_ResponsesToolCatalog`：首轮顶层定义保持，后续新增函数以`additional_tools`追加；对应能力说明也在尾部更新，避免旧指南仍称load未开放。工具开关只追加变化、不重写已发状态；定义变化、已有顺序变化或权限撤销仍重建目录并触发正确重放。执行资格始终来自完整当轮允许集合。
- 普通路由保持原逐轮工具协议，回退剥除Responses专用项；恢复快照包含顶层目录、追加定义与已完成结果，限额`none`和超时收尾不重做工具。原脱敏和内容指纹校验保留。
- 技能schema不再写入候选、随机回执和已加载版本枚举，但load/save仍只在原资格满足后披露。执行器每次核对候选、来源、SHA和配置；保存的新建目标为null，更新必须为本轮已加载ID，旧缺省调用仍兼容。
- 按话题触发的星痕资料164→609，原文与条件不变。完整分条规则原文在164缓存并受budget保护，810保留短提醒；旧接口或关闭新段保留原完整提醒。固定工具结果说明只附一次，真实事实/结果不删减。
- 自动key开关启用时，新款官方Responses使用`catty:shared`；显式key不覆盖，旧模型/接口仍按scope，DeepSeek不注入key。只合并相同前缀的核算，不合并历史、人格、权限或WS链；12h策略未改。
- 追加工具定义纳入原token预算器，不能因没有content而漏算。TTL日志增加可用的response_id与实际显式断点累计指纹；缺失编号/期限保持unknown，不记录正文或密钥。

## 验证证据

14模块按最后一次结果汇总351项，342通过、9项既有跳过，0未解决失败/错误；15份本轮修改源码/测试无编辑器诊断。

| 模块 | 项数 | 结果 |
| --- | ---: | --- |
| test_prompt_audit_fixes | 10 | 通过 |
| test_skill_runtime | 28 | 通过 |
| test_skill_disclosure | 9 | 通过 |
| test_tool_switches | 7 | 通过 |
| test_cache_cohorts | 23 | 通过 |
| test_reply_split_reminder | 8 | 通过 |
| test_responses_api | 38 | 通过 |
| test_prompt_compressor | 37 | 通过 |
| test_openai_client | 71 | 62通过、9既有跳过 |
| test_responses_ws | 38 | 通过 |
| test_tool_history_recovery | 9 | 通过 |
| test_reply_deadline | 10 | 通过 |
| test_reply_message_composition | 48 | 通过 |
| test_skill_tools | 15 | 通过 |

完整定向入口如下；本轮按修改范围分批运行，失败修复后仅重跑相关模块，不重新执行全目录或在线评测：

```powershell
E:/Py/python.exe -B -X utf8 scripts/run_offline_regressions.py --module test_prompt_audit_fixes --module test_skill_runtime --module test_skill_disclosure --module test_tool_switches --module test_reply_split_reminder --module test_prompt_compressor --module test_openai_client --module test_responses_ws --module test_tool_history_recovery --module test_reply_deadline --module test_reply_message_composition --module test_skill_tools
E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_responses_api.py
E:/Py/python.exe -B -X utf8 -m unittest discover -s tests -p test_cache_cohorts.py
```

HTTP测试经过真实请求预处理和JSON转换，确认不同scope自动key一致、原input前缀未改、追加定义原位保留。WS真实工具循环用本地传输替身确认新增能力继续previous_response_id，而脱敏和同名定义变化仍全量重放。首次矩阵的一个旧断言要求新增工具必须重放；核对后补齐尾部能力说明并拆分该断言，WS38项与技能9项复验通过，没有削弱内容校验。

技能读写闭环验证真实临时SkillStore、只在选择后交付正文、公共抓页后保存、null新建、未知回执/未加载更新拒绝，以及完整结果不截短。相关纯预算测试证明追加schema被计入，分条原文与短提醒各有正负对照。

离线4条气泡配置下，真实生成器的完整尾部规则147字符，新短提醒35字符，每次少112个未缓存尾部字符，完整版保留在缓存前缀。这是字符量证据，不是token节省或服务端命中率实测。十份修改说明/交接的本地链接及351项表格计数已核对。

## 部署状态与剩余事项

本会话结束时未部署、未提交；后续已由[会话39](session-39.md)一起部署NO_REPLY和缓存修复。未清缓存、未发送收费模型探针，知识库、任务宿主和语音等无关并行修改保留。

97–98%尚无线上结果。上线切换key/前缀会有初次冷写；随后应按同模型全部实际input/cached_tokens累计，同时报告冷写、必要历史压缩及权限变化，不只挑高命中请求，也不用normalized指标代替真实读率。保留10月31日旧版140条请求92.20%作为历史样本，不作为修复后结论。

旧的同前缀归零与账户30m写入来源没有补齐原始回执，不能声称全部归因；新增编号与断点指纹用于后续自然流量排查。直接Images API不支持用本次聊天TTL日志推定其来源，不擅自改生图接口。

实现依据为OpenAI官方Prompt caching、Tool search（additional_tools）和Function calling（nullable schema）文档，本轮已在线核对；没有借此启用托管tool_search或扩大已有工具暴露。