# Windows 本地运行速查

## 开机后启动

先在 Docker Desktop 中启动 `ai-accounting-core`，再从仓库根目录启动只读财务看板：

```powershell
.\.venv\Scripts\finance-dashboard.exe
```

## 前端改动后更新看板

```powershell
Set-Location .\frontend
npm run build:release
Set-Location ..
.\deploy\windows\restart_dashboard.ps1 -OpenBrowser
```
