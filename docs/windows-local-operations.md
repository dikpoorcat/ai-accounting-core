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

## 前端改动后更新看板

```powershell
Set-Location .\frontend
npm run build:release
Set-Location ..
.\deploy\windows\restart_dashboard.ps1 -OpenBrowser
```
