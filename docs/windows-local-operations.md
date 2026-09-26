# Windows 本地运行速查

本机使用 `finance-local` SQLite 服务。一个资料根目录对应一个常驻服务；目录库索引多家公司，每家公司有独立业务库。当前开发库属于 `ai-accounting-kernel/2` 的 `draft / 0` 合同，连接时核对类别、版本和完整结构指纹。旧 PostgreSQL、Alembic 及旧 SQLite 库不是本系统的启动或升级来源。

## 启动与登录

在本仓库会话说“启动”，会按 `$accounting-startup` 只准备本地服务、验证 MCP 与负责人登录并打开页面。开始处理真实资料需另说“开始记账”或明确交办业务。

手工启动仓库环境：

```powershell
.\deploy\windows\start_accounting.ps1 -DataRoot D:\会计资料
```

未传 `-DataRoot` 时先使用 `FINANCE_DATA_ROOT`，否则使用仓库 `data/kernel-draft`。`-NoBrowser` 只连接服务并返回身份状态；默认打开页面。服务在本机回环地址自动选端口，重复启动复用同一资料根的现有服务。独立运行包可在包目录执行 `finance-local.ps1 --root D:\会计资料 serve`。入口细节见[本地会计工作台启动](local-kernel-startup.md)。

负责人在页面发起设置或登录，密码和恢复码只在本机安全窗口输入。MCP 使用 `finance_local_security` 请求、查询或取消窗口；成功发起窗口不等于完成登录。取消后结束该次尝试。不要把秘密放进业务命令、聊天或终端参数。

页面可打开只表示服务可用。操作前仍要确认 `finance_local_schema` 与 `finance_local_command` 可连接，以及负责人已登录。开发工作区的 Python 命令使用仓库 `.tmp-kernel-venv`，不使用系统 Python。

## 工作与请求恢复

泛化“开始记账”先调用 `workflow(company_id, as_of)`；指定月份时传 `period`。清单展示银行、工资、普通业务、税务、资产、融资的资料与核算状态，以及关账、实际办理和文件交付。空公司没有可靠月份时不补造起点。已到期且条件齐的事项优先；缺资料时继续其他独立事项。

每条写命令使用稳定 `request_id`。响应丢失后，能恢复原载荷就用原键重试；只有编号时调用 `request_result(company_id, submitted_request_id)`。`committed` 返回原动作和保存的结果，`unknown` 只说明当前公司库没有该请求与审计记录，不能据此断定业务失败或另记一笔。改变请求内容或预览失效时重新核对，并使用新键。

`needs_information` 的 `fact_issues` 指向缺少的字段、精度和可复用来源；只在明确下一步时使用 `resolution`。先查已有资料，再决定是否需要负责人补充。技术故障和内容损坏不转成业务追问。

## 数据库结构与故障

普通连接会核对目录库及公司库的系统标识、结构合同和实际 SQLite SQL 原文。当前阶段按全新开发库运行，不自动升级、删除、重建或补标识旧文件；已有文件即使没有业务行也不是可覆盖的新建目标。遇到 `schema_fingerprint_mismatch` 或不支持的库，保留原文件并按诊断处理。

`verify_integrity` 只读检查权威内容、发布链和投影。确认为派生数据差异后，`rebuild` 修复金额及期间投影，`repair_read_indexes` 修复引用目录；两者先核对权威来源，不能补造事实或改写冻结历史。不要用 SQL 手工改账或绕过合同检查。

后台备份和文件任务通过 `jobs(job_id=...)` 查询。失败返回稳定 `error_code` 和安全说明，处理明确原因后可对失败任务调用 `retry_job`；自动尝试有上限。`last_error` 是本机诊断内容，不作为公开结果或负责人追问。目录中的公司创建与恢复由 `operations` 查询。

## 页面与安全窗口

页面保持经营简报、资金、员工、资产、财务报表五页。关账先预览，再由负责人在同版核对内容上通过原生密码窗口批准，最后执行 `close`；页面浏览与窗口批准都不代替正式提交。报表、申报或付款的文件生成也不代表已对外办理。

页面开发构建和本机运行步骤见[启动说明](local-kernel-startup.md)。旧 `restart_dashboard.ps1`、Docker 容器和 `company_cli check-schema` 不是当前入口。
