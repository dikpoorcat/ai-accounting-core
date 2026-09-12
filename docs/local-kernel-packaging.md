# 本地内核独立运行包

当前源码为公司库 v11、目录库 v3，结构及读取边界见[有界看板查询](bounded-dashboard-queries.md)。仓库环境的软件更新不等于独立运行包已更新；交付运行包时应从当前源码重新打包并验证。

## 构建和使用

仓库使用 `.tmp-kernel-venv/Scripts/python.exe`，不得使用本机默认 Python。前端改动后先执行 `npm run build:release`，再从仓库根目录打包。打包复制现成的发布页面，不再次构建；普通 `build` 的 `frontend/dist` 不能证明服务优先读取的静态目录已更新。打包前应确认输出目录、同名 ZIP、`-relocated`、`-relocated-validation` 和 `-verification.json` 五项均不存在，保留旧产物：

```powershell
.\scripts\package-local-kernel.ps1 -OutputDirectory .tmp-local-distribution\release-example
```

默认产物是软件目录、同名 ZIP、实际解压目录和验证报告。`-SkipValidation` 只用于检查中间产物，不能将其作为已验收运行包交付。`manifest.json` 记录受控运行时、计算构建、依赖版本和逐文件摘要。

解压后使用相对路径启动器：

```powershell
.\finance-local.ps1 --root D:\会计资料 serve
.\finance-local.ps1 --root D:\会计资料 mcp
.\finance-local.ps1 --help
```

也可使用 `finance-local.cmd`。包自带 Python、SQLite、Tcl/Tk、Argon2、PDF/Excel 读取及导出依赖、空白报表模板和发布页面，无需 PostgreSQL、Docker、Node.js 或旧虚拟环境。解释器启用隔离模式，模块路径限制在包内。

每个资料根目录使用唯一常驻服务。负责人只在原生安全窗口输入独立密码；Windows 凭据管理器保存会话。当前用户及 SYSTEM 以外的用户不能读取服务私有连接元数据；原生窗口的连接能力通过匿名标准输入管道传入，不放进命令行或环境变量。

这是软件包。公司备份仍由业务内核的 `backup` 命令生成，必须等后台任务成功并验证 ZIP。不能复制正在使用的 SQLite 主文件代替正式备份。修改运行源码后须重新打包，使计算构建、文件摘要和验收报告对应同一份交付物。
