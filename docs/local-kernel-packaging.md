# 本地内核独立运行包

当前源码为公司 v11、目录 v3，结构及读取边界见[有界看板查询](bounded-dashboard-queries.md)。T6 正式更新使用主仓受控运行环境；既有 T5 候选包仍以其 v10 验收记录为准，尚未生成包含 T6 与 v11 的新软件包。
T5 软件候选包、实际构建文件和验证状态见 [T5 结果](t5-implementation-result.md)；候选验证不等于部署或整体架构验收。

## 历史 v8 交付及验证

此前已交付 [dashboard-owner-complete-20260912.zip](../.tmp-local-distribution/dashboard-owner-complete-20260912.zip)，[公开验收记录](local-kernel-dashboard-owner-package-verification.json)不含凭据或企业资料。
以下至“构建和使用”之前均为该 v8 包的历史核验，保留其原版本、数量和摘要，不能当作当前源码验证。

| 项目 | 已核验值 |
| --- | --- |
| 受控运行时 | Python 3.12.13、SQLite 3.53.1 |
| 数据库合同 | 公司 v8、目录 v3 |
| 注册事实 | 105 种 |
| 软件文件 | 4,112 个，ZIP 另含清单 |
| 软件依赖 / 应用模块 | 38 个分发包 / 71 个 Python 模块 |
| ZIP 大小 | 33,934,720 字节 |
| ZIP SHA-256 | `3eb52e4a930fb4998a2f1a81af2e1505796462661616c80e0bdcc4aa79da9c74` |
| 计算构建 | `local-kernel-2:705c6b72e0a4394b0f422872065154009ab484d38706027163a78de2a0e91fd3` |

本包保留五页 Vue 看板，默认先展示经营情况。短长摘要、按来源确定的分录对象、原文件名、全月账户筛选、跨月及期初工资、连续更正链与实际文件进度均使用内核查询。公司库维持 v8、目录库维持 v3，没有结构迁移；本轮经授权追加的真实企业展示资料只在私有公司库和便携备份中，不进入软件包。具体口径见[老板视角优化](dashboard-owner-experience.md)。

打包只读取 `npm run build:release` 生成的正式页面。包内 19 份页面资源已与当前发布文件逐字节核对，`/`、`index.html`、`local.html` 进入同一看板，五页路径支持直接访问和刷新。

## 实际运行验证

本次使用默认完整验证，没有跳过移目录验收。包已解压到另一目录，完成 13 次 CLI 调用、一张合成凭证发布及幂等重试、后台自动公司备份和另一目录恢复。下列能力均实际执行通过：

- 所有文件摘要及包内模块路径，隔离外部 Python 环境变量。
- 原生 Tcl/Tk 表单登录、`pythonw` 窗口及远程取消、Windows 凭据与私有元数据 ACL。
- CLI、两个相对路径启动器、MCP 握手与查询、页面及鉴权 API。
- 五页路由、兼容 HTML 入口及六个看板查询；浏览器金额保持整数分字符串，CLI/MCP 保持原有整数合同。
- 整数金额、凭证借贷平衡、公司身份、完整备份及恢复。

同一发布页面另用实际同源服务完成桌面和手机检查、季度报表导出及任务面板下载。合成导出文件损坏后明确提示不能下载，重新生成取得新的有效任务及文件；原凭证、冲正和替换凭证分别定位正确依据。公司切换旧响应、分页版本变化、107 张凭证和 102 条银行流水也已在浏览器核对。仓库启动器在 Windows PowerShell 5.1 和 PowerShell 7 中分别以最终源码和移目录运行包启动四个新的合成空目录，均通过并正常停止；已修复默认 Windows shell 对脚本编码及 Python 参数引号的兼容问题。

合成会话已撤销，对应 Windows 凭据已删除。验证未访问真实公司库或真实负责人凭据。运行包采用明确软件白名单，不含公司数据库、重建资料、`.env`、旧 ORM、PostgreSQL 适配或已退役的一次性身份接续工具。

原始报告位于同名 `-verification.json`，实际移目录和合成数据分别位于 `-relocated`、`-relocated-validation`；合成数据不进入软件 ZIP。[首轮对接验收](local-kernel-dashboard-alignment-package-verification.json)、[恢复看板时的 v8 验收](local-kernel-dashboard-v8-package-verification.json)、[上一版 v7 验收](local-kernel-actual-withholding-v7-package-verification.json)及[更早 v6 验收](local-kernel-reconstruction-v6-package-verification.json)保留各自原构建标识，不能与本次测试合并。本轮 `owner-release`、`owner-final` 是收尾修复前的中间构建，正式交付以 `owner-complete` 为准。

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
