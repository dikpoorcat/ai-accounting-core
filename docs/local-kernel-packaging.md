# 本地内核独立运行包

`scripts/package-local-kernel.ps1` 从仓库已准备好的受控环境生成 Windows x64 软件包。打包过程不下载软件，不复制公司数据库、`.env`、仓库工作资料或整个开发虚拟环境。

2026-09-11 已通过完整移目录验收的构建为 `local-kernel-1:8840d89e6e5f3696c43518715d67a4a85bed2d155ac909dee8c0846c7a1ea1b5`。本地软件包是 [20260911-020906.zip](../.tmp-local-distribution/20260911-020906.zip)，压缩后 30,850,187 字节，软件文件共 82,855,652 字节。[可分享的验收记录](local-kernel-package-verification.json) 保存包摘要和检查结果，不含本机路径或鉴权信息。

在仓库根目录运行：

```powershell
# 前端有改动时先在 frontend 目录执行 npm run build。
.\scripts\package-local-kernel.ps1

# 也可以指定一个尚不存在的输出目录。
.\scripts\package-local-kernel.ps1 -OutputDirectory .tmp-local-distribution\release-example
```

构建使用 `.tmp-kernel-venv/Scripts/python.exe`，要求 Python 3.12.13 和 SQLite 3.53.1。包内保留标准库、实际安装的运行依赖及其声明的额外依赖、内核与共用纯计算模块、空白财务报表模板和已构建页面。`manifest.json` 记录运行时版本、计算程序内容版本、依赖版本和每个文件的摘要。

默认产物包括软件目录、同名 ZIP、另一个实际解压目录，以及 `-verification.json` 验证报告。已有目标不会被覆盖。验证使用的合成公司和备份单独保存在 `-relocated-validation` 目录，不进入软件包。`-SkipValidation` 只用于检查打包中间结果；未经默认验证的产物不能作为已验收运行包交付。

软件包可解压到另一个本地目录，通过相对路径启动器运行：

```powershell
.\finance-local.ps1 --root D:\会计资料 serve
.\finance-local.ps1 --root D:\会计资料 mcp
.\finance-local.ps1 --help
```

也可以使用 `finance-local.cmd`。运行包不需要另行安装 Python、SQLite、PostgreSQL 或 Node.js。解释器的 `python312._pth` 将模块搜索限制在包内；启动器启用隔离模式，不读取用户站点包或外部 `PYTHONPATH`、`PYTHONHOME`。页面启动命令只在本机监听，鉴权令牌由当前进程生成。

默认验收实际污染外部 Python 环境变量，再使用解压后的解释器检查文件摘要和所有已加载模块的路径。随后通过 CLI 完成建公司、类型 Schema、登记证据、发布凭证、幂等重试、完整公司备份和另一目录恢复，并检查两个启动器、MCP 握手与查询、页面和鉴权查询。公司身份、恢复结果和整数金额必须一致。

这是软件运行包；公司便携备份仍通过内核的 `backup` 命令生成。不要复制正在使用的 SQLite 主文件代替正式备份。源代码改动后需要重新打包，让计算程序版本、软件摘要和验收报告对应同一份交付物。
