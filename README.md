<!-- @format -->

# AI Accounting Core

本地会计系统，帮助不熟悉财务的老板借助会计 AI 完成日常会计工作。AI 引导准备资料、理解业务并办理账务；程序负责确定性计算与可靠保存；看板供老板核对，也供专业顾问只读审查。老板负责现实信息和经营决定，顾问不修改或批准账务。

每家公司使用独立 SQLite 数据库。金额按整数分保存，核算月份与实际日期分开，未知金额不补零。系统不是法定账簿或报税系统。

## 当前 3.0 架构

实现位于 `src/ai_accounting_modular/`，运行标识为 `ai-accounting-modular/1`。完整需求、模块合同、数据库结构和验收预期见 [需求与开发设计](docs/design/README.md)。

- 业务模块分别承接日常收支、工资劳务、税费、资产、资金和投资理财。合作项目投资是投资理财的一种方式。
- `ledger` 统一采用核算结果、生成凭证、维护余额和关账依据；公共入口只接收类型明确的事实，不开放任意借贷分录。
- `application` 协调一次完整业务；`records` 保存原件与当前标准业务文档；`files` 负责备份、报表和文件交付。
- `reading` 从公开结果生成读取数据，`dashboard` 适配现有 Vue 五页。模块声明实际读取依赖，消费者不依赖别的模块的私表和内部实现。
- `storage` 提供事务与结构核验，`host` 装配本机服务、唯一 CLI `finance-modular`、MCP 和后台接续。

开放月份直接更正或按条件撤去误记，已有凭证保持编号，不另存错误日记。闭月更正保留必要的冲正和冻结依据。原件承担来源，标准业务文档表达最终事实，数据库保存正式账务：备份恢复保留保存时状态，文档重建重新核算最终事实。

经营简报、资金、员工、资产和财务报表保留现有主要内容、样式和交互。月度核对及顾问来源查阅均为只读，不是批准流程。

## 运行与资料

项目 MCP、记账 Skill 和看板使用 `data/accounting/catalog.sqlite3`。正式公司目录统一为 `data/accounting/companies/<信用代码>/`，其中 `runtime/` 保存账本，`originals/` 保存原件，`standard/` 保存当前标准业务文档，`backups/` 保存每家公司独立的完整备份。

在仓库中启动：

```powershell
& './tools/start-accounting.ps1'
```

开发看板需要实时看到 Vue、TypeScript 或样式修改时，在后台运行后执行：

```powershell
npm --prefix frontend run dev
```

打开 `http://127.0.0.1:5173/`。页面与 CLI、MCP 共用同一个后台和负责人身份；其他目录库通过 `FINANCE_MODULAR_CATALOG` 明确指定。后端源码变更仍需单独重启后台，普通启动不自行换库、创建公司或重放。

安装、CLI、MCP、离线恢复和重建见[安装、运行与会计 AI 接入](docs/design/09-runtime-and-ai.md)。当前能力和运行状态的查阅入口见[交付结果](docs/design/10-delivery-status.md)。

## 开发与验证

开发先读[整体架构](docs/design/02-architecture.md)、[公开合同](docs/design/03-contracts.md)和[开发与验收](docs/design/07-delivery-acceptance.md)。字段、业务含义、数据库结构及预期由唯一合同确定，各模块可以按公开边界独立开发和集成。

```powershell
& '.tmp-kernel-venv/Scripts/python.exe' tools/generate_modular_contracts.py --check
& '.tmp-kernel-venv/Scripts/python.exe' tools/generate_modular_storage.py --check
& '.tmp-kernel-venv/Scripts/python.exe' tools/check_modular_boundaries.py
& '.tmp-kernel-venv/Scripts/python.exe' tools/check_design.py
& '.tmp-kernel-venv/Scripts/python.exe' -m pytest tests/modular --ignore=tests/modular/acceptance/performance -q
npm --prefix frontend test
npm --prefix frontend run build
```

固定业务场景、真实模块替换、故障恢复、浏览器和性能分别验证。合成样本构建代码位于 `tests/modular/acceptance/performance/`，包含 120 个月样本；样本构建、正确性检查和计时分开。性能要求见[看板适配](docs/design/06-dashboard-adapter.md#10-性能目标和验收)与[优化方法](docs/performance-optimization.md)，未经老板确认的数字不作为硬门槛。合成验证不操作真实公司资料或现有服务。
