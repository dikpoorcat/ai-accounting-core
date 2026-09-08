# Windows 本地运行速查

## 开机后启动

在 Codex 本仓库会话中说“启动”。AI 会启动 Docker Desktop、等待已有 PostgreSQL 容器健康、
复用或启动只读财务看板，验证 MCP 连接和负责人登录，并打开看板。若登录过期，在弹出的
本机登录窗口输入密码即可。准备完成后说“开始记账”或提供业务资料；也可以一次说
“启动并开始记账”。单独“启动”不会处理账务。

手工启动本机服务（不包含 Codex MCP 连接和登录验证）：

```powershell
.\deploy\windows\start_accounting.ps1 -OpenBrowser
```

重复运行会复用已有看板，不重启正常进程。脚本从自身位置定位仓库，不要求当前目录。
默认看板地址为 `http://127.0.0.1:8765/`，可用 `-Port` 指定端口；
`-TimeoutSeconds` 设置 Docker Engine 和容器各自的就绪等待时间（默认 120 秒）。
日常启动只启动已有容器，不安装依赖、初始化或迁移数据库，也不重建容器或数据卷。
缺少已有容器时按首次安装或恢复流程处理，不用新空库掩盖故障。

MCP 使用仓库 `.codex/config.toml` 中的 STDIO 配置，由 Codex 管理。当前会话无法调用工具时，
使用宿主可用的重连功能；若仍不可用，重新打开本项目会话后再说“启动”。不要另开后台
`finance-mcp` 进程。看板首页响应成功仅证明页面可用，不能代替 MCP 和登录验证。

## 负责人安全窗口

首次设置、登录、关账授权、改密、恢复账号和更换恢复码使用同一套原生 Windows 表单。
原有 `finance-login setup/login/recover/change-password/replace-recovery-code/approve-close`
命令现在只请求窗口，不在终端读取密码或显示恢复码；成功请求不等于操作完成。
统一入口为 `finance-login security-window --kind <操作类型>`，通过
`finance-login security-window-status --request-id <返回的编号>` 查询当前目标的状态。

MCP 对应 `finance_request_owner_security_window` 与 `finance_get_owner_security_window_status`。
操作类型为 `bootstrap_owner`、`login`、`approve_period_close`、`change_password`、`recover`、
`replace_recovery_code`。工具和命令均不接受秘密字段或任意脚本。
`starting` 表示启动中，`waiting_for_user` 表示原生表单已显示，`succeeded` 表示窗口操作完成；
之后仍须重试原业务工具。关账还必须查询精确匹配的未消费授权。

运行依赖仓库 Python 的 Tkinter 和同目录 `pythonw.exe`；缺少时返回窗口不可用，不能改走终端。
窗口跨进程去重，相同请求复用，不同请求返回 `OWNER_SECURITY_WINDOW_BUSY`。
关闭或崩溃不视为成功；身份操作已提交而后续失败时，应按结果恢复登录，不重复建号或改密。
恢复码仅在本地表单一次性显示，请点击“我已保存恢复码”后继续。

会话按真实数据库身份和端点分别存于 Windows 凭据管理器。升级后每个实例首次重新登录一次；
旧全局令牌不复制，原密码保持有效。窗口状态存于当前 Windows 用户受限目录，绝不作为授权凭据。

若返回 `operation_committed=true`，身份操作已经提交；`login_completed=false` 表示尚未完成登录。
若 `operation_committed=null`，进程中断导致提交结果不确定，必须先核验账号状态，不能自动重做。
已建号但未登录时使用登录窗口；改密或恢复完成后用新密码重新登录。未确认保存恢复码便关闭
窗口时，先登录再显式请求更换恢复码；没有可用密码或恢复码时停止并人工处理，不重建负责人。
本地表单不自动复制或保存恢复码，也不是抵御同一 Windows 用户任意代码执行的安全隔离区。

## 前端改动后更新看板

```powershell
Set-Location .\frontend
npm run build:release
Set-Location ..
.\deploy\windows\restart_dashboard.ps1 -OpenBrowser
```
