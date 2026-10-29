# 会话 26 · 共享人格集成测试夹具修复

## 目标与范围

- 核对共享人格失败是否属于设计行为；只修复确认的缺陷，不改变人格、群聊判门或语音策略。

## 已完成

- 确认 `_rule` 已改为调用真实 `_extract_incoming_with_voice`，两个 AST 测试夹具仍只加载旧的同步抽取函数。
- 补齐 `test_shared_persona_integration` 与 `test_fdj_proactive_integration` 的函数加载清单；不替换为桩函数，不修改生产源码或测试断言。

## 验证证据

- 修复前定向运行两个模块：69 项、0 failures、42 errors、0 skipped；42 个错误均为 `NameError: name '_extract_incoming_with_voice' is not defined`。
- 修复后同一条定向命令：69 项、0 failures、0 errors、0 skipped。共享人格的唤醒词、真实 @、引用、续聊、广播/ACL 限制及原有主动调度断言全部保留并通过。
- 生产 Python 3.14.5 同组 69 项、0 failures、0 errors、0 skipped；两份测试文件及生产入口文件与本地 SHA-256 一致。
- 验证命令：`E:/Py/python.exe -B -X utf8 -c "import sys,unittest; sys.path.insert(0,'tests'); result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromNames(['test_shared_persona_integration','test_fdj_proactive_integration'])); raise SystemExit(not result.wasSuccessful())"`。
- 未重新运行完整离线清单，未调用真实语音转写、模型或 QQ。

## 部署状态与剩余事项

- 两份测试已通过 push_zip 推至 `D:/CattyQQAI/tests`，解压 2 文件、跳过 0；压缩包 SHA-256 为 `8954d301a3134062c835ec47b71bbbbd1a21c54ea0a88b4bfeab8bfb3f34bb45`。生产旧测试及相关文档备份在 `D:/CattyQQAI/tmp/shared-persona-fixture-20261030-44d2b8f6/backup`。
- 生产源码与配置保持原样；本次交付仅测试夹具及相关文档。