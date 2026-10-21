<!-- @format -->

# AI Accounting Core

本地会计系统，帮助不熟悉财务的老板借助会计 AI 完成日常会计工作。AI 负责引导准备资料、理解业务和办理账务；程序负责确定性计算与可靠保存；看板供老板核对，也供专业顾问只读审查。老板负责现实信息和经营决定，顾问不修改或批准账务。

每家公司使用独立 SQLite 数据库。金额按整数分保存，月份与实际日期分开，未知金额不补零。系统不是法定账簿或报税系统。

## 模块化体系

新实现位于 `src/ai_accounting_modular/`，体系标识为 `ai-accounting-modular/1`。完整需求、模块合同、数据库结构和验收预期见 [需求与开发设计](docs/redesign/README.md)。

- 业务模块分别承接日常收支、工资劳务、税费、资产、资金和投资理财。合作项目投资属于投资理财的一种记账方式。
- `ledger` 统一采用核算结果、生成凭证、维护余额和关账依据；业务模块提交类型明确的事实，不开放任意借贷分录。
- `application` 负责一次完整业务的协调；`records` 保存原件与当前标准业务文档；`files` 负责备份、报表和下载。
- `reading` 通过公开结果生成读取数据；`dashboard` 适配现有 Vue 五页。各模块声明实际读取依赖，内部实现变化不要求其他模块跟着修改。
- `storage` 提供事务与结构核验；`host` 装配本机服务、CLI、MCP 和后台任务。模块通过公开入口协作，不直接读取其他模块的私表。

开放月份的错误直接修改或撤去，已有凭证保持编号，不另存错误日记。闭月更正保留必要的冲正和冻结依据。原件、当前标准业务文档和正式数据库分别承担来源、最终事实和账本的作用：备份恢复保留原账务状态，标准文档重建重新核算最终事实。

经营简报、资金、员工、资产和财务报表保留现有主要内容、样式和交互。月度核对为只读检查，不是批准流程。来源查阅使用独立只读入口，不增加五页默认读取负担。

## 模块化体系运行

使用仓库虚拟环境，不使用系统默认 Python。先构建独立静态页面：

```powershell
npm --prefix frontend run build:modular
& '.tmp-kernel-venv/Scripts/python.exe' -m ai_accounting_modular.host.runtime.cli --help
```

安装项目后可用 `finance-modular` 代替上面的 Python 模块命令。全局 `--catalog` 参数放在子命令之前，目录库使用绝对路径：

```powershell
finance-modular --catalog D:\新会计资料\catalog.sqlite3 init
finance-modular --catalog D:\新会计资料\catalog.sqlite3 serve
finance-modular --catalog D:\新会计资料\catalog.sqlite3 mcp
```

`init` 在原生窗口建立老板身份；`serve` 启动并打开看板；`daemon` 在前台运行；`mcp` 让 AI 连接同一服务。老板密码和恢复码在原生窗口处理，不放入聊天或命令行。`company-create` 新建公司，`company-register` 登记已恢复或重建的公司；完整输入以命令帮助和公开 Schema 为准。`service-info` 和 `stop` 只访问指定服务，不自动启动服务。

不带目录参数的 `mcp` 只提供离线恢复与重建。恢复或重建总是交付明确的新目标，登记接入是另一个动作。新入口不自动搬移真实资料、替换数据库或接替现有服务。

## 现有运行入口

`finance-local`、仓库记账 Skill 和现有 5173 开发页面仍属于 `ai-accounting-kernel/2`。其目录库、公司库为 `draft / 0`，备份格式为 2；它与上述模块化体系使用不同协议和数据结构，不能互换数据库或备份。

现有页面的启动方式保留：

```powershell
$env:FINANCE_DATA_ROOT = Join-Path $PWD 'data/kernel-released'
.\deploy\windows\start_accounting.ps1 -DataRoot $env:FINANCE_DATA_ROOT -NoBrowser
npm run dev
```

页面地址为 `http://127.0.0.1:5173/`，已经运行时直接使用。现有体系的命令、记账流程、原生登录和 Windows 独立包分别见 [运行入口](docs/local-kernel-startup.md)、[会计操作](docs/local-kernel.md) 和 [独立打包](docs/local-kernel-packaging.md)。这些运行规则不替代模块化体系的合同。真实数据库、原件、负责人身份和备份继续保全。

## 开发与验证

模块开发先读 [整体架构](docs/redesign/02-architecture.md)、[公开合同](docs/redesign/03-contracts.md) 和 [开发与验收](docs/redesign/07-delivery-acceptance.md)。字段、业务含义和预期由设计合同统一确定，各模块可以在公开边界内分别实现。

```powershell
& '.tmp-kernel-venv/Scripts/python.exe' tools/generate_modular_contracts.py --check
& '.tmp-kernel-venv/Scripts/python.exe' tools/generate_modular_storage.py --check
& '.tmp-kernel-venv/Scripts/python.exe' tools/check_modular_boundaries.py
& '.tmp-kernel-venv/Scripts/python.exe' docs/redesign/verify_design.py
& '.tmp-kernel-venv/Scripts/python.exe' -m pytest tests/modular --ignore=tests/modular/acceptance/performance -q
npm --prefix frontend test
npm --prefix frontend run build:modular
```

固定业务场景、真实模块替换、故障恢复、浏览器和性能分别验收。性能工具位于 `tests/modular/acceptance/performance/`，构造样本、正确性核对和正式计时分开；预算和样本数量见 [看板性能要求](docs/redesign/06-dashboard-adapter.md#10-性能目标和验收)。合成验收不写入真实账套，不改变现有服务。
