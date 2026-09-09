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
容器健康后，脚本会先只读检查目录库和已登记公司的数据库版本；检查失败时停止后续启动，
输出当前及所需版本，不打开登录窗口，也不自动升级。

MCP 使用仓库 `.codex/config.toml` 中的 STDIO 配置，由 Codex 管理。当前会话无法调用工具时，
使用宿主可用的重连功能；若仍不可用，重新打开本项目会话后再说“启动”。不要另开后台
`finance-mcp` 进程。看板首页响应成功仅证明页面可用，不能代替 MCP 和登录验证。

## 代码更新后的数据库版本检查

更新代码不等于已更新数据库。运行检查无需负责人密码，也不会修改业务：

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.company_cli check-schema
# 也可只检查目录及指定公司：
.\.venv\Scripts\python.exe -m ai_accounting.company_cli check-schema --org-id <公司UUID>
```

输出每个数据库的 `actual_revisions`、`required_revision`、`ready` 和 `upgrade_available`；
任一库未就绪时命令退出码为 1。业务库和目录库分别从各自正式迁移树取得 head。
MCP 和看板仍在每次路由时独立检查，不能以曾经启动成功代替本次检查。

- `DATABASE_SCHEMA_UPGRADE_REQUIRED`：已识别的当前迁移链上存在待执行前向迁移。
  例如业务 v4 → `0002_atomic_corrections`。按部署授权停止对应服务、保存并验证升级前备份，
  使用迁移账户对目录登记的每个目标业务库执行现有 Alembic 前向迁移；这不是空库回放。
  目录库仍使用 `0001_catalog_baseline_v2`，不能把业务迁移施加到目录库。
- `DATABASE_SCHEMA_UNSUPPORTED`：未初始化、退役链、未知 revision 或多 head。先核实部署，
  不能自动建库、stamp 或尝试把非空旧库升级到新基线。
- `DATABASE_SCHEMA_MISMATCH`：实际查询遇到缺表／缺列。核对上述检查及安全诊断编号；
  即使版本号看似正确也不能跳过审计查询或直接补列掩盖结构偏差。

迁移后重新运行检查、重启看板并重新连接 Codex 管理的 MCP，再验证已存在的
`finance_get_event` 可读取原事实、`facts_hash`、凭证及审计；最后才恢复预览—确认更正。
逐公司比对迁移前后的原业务事实、凭证编号、账务金额、审计和关账快照。
升级数据库结构不代表已经完成工资更正，也不授权自动撤销旧冲正或重录真实业务。

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
恢复码仅在本地表单一次性显示；可点击“复制恢复码”粘贴到安全位置，不要粘贴到聊天中。
复制不会自动确认已保存，保存妥当后仍需点击“我已保存恢复码”继续。

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
