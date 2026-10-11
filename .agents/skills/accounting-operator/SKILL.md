---
name: accounting-operator
description: Operate real local accounting from invoices, bank records, payroll, tax, assets and owner confirmations through the typed local kernel. Use for 开始记账 or supplied business materials. Repository development, tests, migrations and product design are separate tasks.
---

# 会计执行助理

处理真实企业资料，通过 `ai_accounting` MCP 调用本地确定性内核。单独“启动”或准备环境使用 `$accounting-startup`；代码、测试、迁移、部署及产品设计走开发模式。

先调用 `finance_local_schema` 无参概要，读取 `agent_operating_protocol` 通用规则及专项目录。按当前事项选择接口合同，遵守随响应提供的 `agent_operating_rules`；目录中需要的其他规则可用 `view="selected", protocol_topics=[主题名]` 读取。

详细执行要求以当前内核运行协议为唯一来源，本 Skill 不重复维护。通过 `finance_local_command(command=..., payload=...)` 执行业务；接口选择、来源核对、负责人提问、工作稿、发布、恢复及关账均按已读取的规则执行。

需要交付已核验的支付文件到桌面时，可使用 [复制脚本](scripts/copy-export-to-desktop.ps1)，具体条件遵守 `deliveries` 专项规则。
