---
name: accounting-startup
description: Prepare the installed local SQLite accounting service and native owner login when the user says 启动, 启动记账环境, or 做好记账准备. Open the accounting page and verify the actual MCP connection. Changes to startup implementation are repository development.
---

# 记账环境准备

当前正式试用环境是 `ai-accounting-modular/1`，目录库为 `D:/GitHub/ai-accounting-core/data/accounting/catalog.sqlite3`。项目MCP和看板共用该目录；`finance-local` 及其启动脚本属于旧体系，不用于这套账。

使用仓库受控运行时准备后台并打开看板：

```powershell
& 'D:/GitHub/ai-accounting-core/.tmp-kernel-venv/Scripts/python.exe' -I -B -X utf8 -m ai_accounting_modular.host.runtime.cli --catalog 'D:/GitHub/ai-accounting-core/data/accounting/catalog.sqlite3' serve
```

`serve` 复用该目录的后台，通过一次性票据打开页面，不自行拼接登录令牌。详细入口见[新体系运行说明](../../../docs/redesign/09-runtime-and-ai.md)。

通过宿主的 `ai_accounting` 连接调用 `accounting_schema`，以 `accounting_security(operation="session_status", payload={})` 查询负责人会话。需要登录时调用 `accounting_security(operation="request", payload={"kind":"login"})`，再用 `operation="status"` 和原 `request_id` 核对窗口状态。密码及恢复码只由老板在原生窗口输入；已有身份不重新建立，窗口取消后不循环弹窗。

登录后调用 `accounting_command(command="accounting_context", payload={})`，以实际成功返回核对连接和公司列表。页面能打开与当前聊天已连接MCP分别说明；宿主尚未加载新的 `accounting_*` 工具时，使用宿主重连能力，或请用户重新打开项目会话，不能把另起的stdio测试进程冒充本会话连接。

单独启动只准备现有环境，不创建公司、换库、重放或写账。用户继续提供业务资料时使用 `$accounting-operator`。
