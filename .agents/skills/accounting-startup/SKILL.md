---
name: accounting-startup
description: Prepare the installed local SQLite accounting service and native owner login when the user says 启动, 启动记账环境, or 做好记账准备. Open the accounting page and verify the actual MCP connection. Changes to startup implementation are repository development.
---

# 记账环境准备

“启动”授权准备已有环境并打开页面。执行 `deploy/windows/start_accounting.ps1`，使用已安装包或仓库受控运行时，复用每个资料根目录唯一的本地服务。脚本默认打开页面；无需 Docker 或 PostgreSQL。参数和安装说明见 [本地运行入口](../../../docs/local-kernel-startup.md)。

1. 通过宿主工具发现 `ai_accounting`，调用 `finance_local_schema`，遵守返回的 `agent_operating_protocol`。调用 `finance_local_command(command="companies", payload={})` 验证实际 MCP 连接和负责人会话。仅页面可访问、进程存在或工具名称可见，都不算准备完成。
2. 需要登录时，调用 `finance_local_security(action="request", payload={"kind":"login"})`，再用 `action="status"` 和返回的 `request_id` 检查安全窗口。仅 `waiting_for_user` 表示窗口已显示。负责人输入原独立密码后，重试公司列表，以实际成功响应确认登录有效。密码、恢复码、会话令牌不进入聊天、业务载荷、命令行或临时文件。
3. 新安装尚无负责人时，按明确安装授权请求 `kind="bootstrap_owner"`；已有目录身份异常不能通过重建负责人或数据库绕过。窗口取消时结束该登录尝试，不循环弹窗。
   启动只复用或恢复登录。历史关账批准在业务资料和整个批次预览准备好后一次请求，不在启动时提前弹出关账密码窗口；已有安全请求先查询持久状态，避免中断后重复要求密码。
4. 页面通过启动器的一次性票据打开，票据兑换后移出地址栏。不要构造令牌 URL；需要再开页面可执行 `finance-local --root <资料根目录> serve`。已有页面可复用。
5. 单独“启动”不选公司、不读取账务、不创建企业。成功后简短说明环境和页面已就绪。若用户同时要求开始记账或提供业务资料，继续使用 `$accounting-operator`。

MCP 由宿主管理，配置使用同一资料根目录和 `finance-local ... mcp`。若当前会话未加载新工具，报告服务状态与 MCP 尚未连接的区别，使用宿主已有重连能力；无法重连时说明需重新打开项目会话。不得另起后台 stdio 进程冒充当前会话已连接。

日常启动不包含重新安装、清库、导入旧账、回放或正式账务写入。结构指纹、运行时或公司身份不匹配时报告实际错误，不能重新建表掩盖问题。已知数据库的前向升级和持久任务恢复由服务启动完成。
