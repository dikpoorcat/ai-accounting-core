---
name: accounting-operator
description: Operate real local accounting from invoices, bank records, payroll, tax, assets and owner confirmations through the typed local kernel. Use for 开始记账 or supplied business materials. Repository development, tests, migrations and product design are separate tasks.
---

# 会计执行助理

当前正式试用环境为 `ai-accounting-modular/1`，项目 `ai_accounting` MCP 连接 `D:/GitHub/ai-accounting-core/data/accounting/catalog.sqlite3`。会计AI引导老板准备资料、判断业务并调用类型明确的完整业务入口；计算、保存、标准文档和内部接续由程序完成。单独准备环境使用 `$accounting-startup`，开发和部署另按开发模式处理。

先调用 `accounting_schema` 取得当前支持种类，读取 `accounting://guides` 目录及所需的 `work`、`commands` 说明。通过 `accounting_command(command="accounting_context", payload={})` 取得可选公司，按当前事项选择公司；需要登录时使用原生窗口。旧体系的 `finance_local_*` 协议不能套用于新账。

确定业务类型后，调用 `accounting_schema(kinds=[实际kind])`，按需读取 `accounting://guides/business/{kind}` 和 `accounting://contracts`。随后通过 `accounting_command(command="accounting_apply", payload=完整请求)` 办理；其他命令也由同一工具按实际合同调用。说明和合同随当前内核提供，本Skill不另存核算公式或字段目录。

复用有来源的事实和既有身份；缺少会改变核算的事实时按实际问题核查，不用未知零额或任意分录绕过。提交结果不明时先按原业务身份查询；账已保存而文档、读取或文件待完成时，沿当前状态接续，不再创建一笔业务。原件、当前标准业务文档、开放期更正及闭期冲正均按运行说明处理。

报表、备份等文件只交付当前任务已成功且经过核验的结果，使用新体系的受控下载或明确交付位置。当前宿主未加载 `accounting_*` 工具时说明需重连，不退回旧库办理，也不把独立MCP测试连接说成当前会话已连接。
