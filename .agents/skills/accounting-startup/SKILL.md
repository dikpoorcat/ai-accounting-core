---
name: accounting-startup
description: Prepare this repository's installed Windows accounting environment when the user says 启动, 启动记账环境, or 做好记账准备. Start Docker and the existing database, verify the Codex MCP connection and owner login, and open the dashboard. Requests to implement or change startup behavior are repository development instead.
---

# Accounting Startup

“启动”授权执行本机记账准备。直接完成可执行步骤，不再询问是否打开 Docker、启动已有服务或打开看板。

1. 从仓库根目录执行 `./deploy/windows/start_accounting.ps1`。脚本等待 Docker Engine 和已有 PostgreSQL 容器就绪，复用或启动本仓库的看板，并检查页面响应。长时间启动时持续报告实际进度。细节与手工入口见 [Windows 本地运行速查](../../../docs/windows-local-operations.md)。
2. 通过当前宿主的工具发现机制查找 `ai_accounting`，调用 `finance_get_event_schema` 并遵守其 `agent_operating_protocol`，然后调用 `finance_list_companies(include_archived=false)` 验证目录库连接和负责人登录。工具名称可见或看板返回 HTTP 200 都不等于 MCP 已连接、登录有效；必须以实际调用结果为准。
   - MCP 是由 Codex 按仓库 `.codex/config.toml` 管理的 STDIO 进程，不另起后台 `finance-mcp` 进程冒充连接。若宿主提供重连功能，重连后重试；若当前会话没有可调用的工具或连接仍失败，报告服务已就绪但 MCP 未连接，请用户重新打开本项目会话后再说“启动”。不改写 MCP 配置或审批设置。
   - 认证要求出现时，沿用内核启动的可见本机登录窗口，请负责人在窗口完成登录，随后重试被中断的只读调用。不得在聊天中索取、读取或代输密码，不在隐藏终端运行交互式登录。等待登录期间不能报告准备完成。
3. 成功后打开脚本返回的看板地址（默认 `http://127.0.0.1:8765/`）。优先用宿主的浏览器面板；没有面板工具时使用系统默认浏览器。已存在对应标签页则复用。
4. 单独的“启动”只报告准备结果和看板入口，例如：“记账环境已就绪，看板已打开。可以说‘开始记账’，或直接发送业务资料。”不选公司、不读取公司账务或展示月度待办。若没有可访问的公司，如实说明尚无可用公司，不自动创建。

若用户同时要求“启动并开始记账”或附带具体记账任务，准备成功后继续使用仓库 `$accounting-operator`，按其公司选择和业务流程执行，不要求重复发指令。

这是已有环境的日常启动，不包含安装依赖、数据库初始化、迁移、回放、清库、重建容器或卷、前端构建、Git 更新及正式账务写入。脚本报告缺少安装或现有容器、未知历史库、端口冲突等阻断时，不用初始化或覆盖绕过；说明未完成环节、实际原因和一个具体恢复动作。只报告实际验证过的状态，保留工具返回的稳定错误码。
