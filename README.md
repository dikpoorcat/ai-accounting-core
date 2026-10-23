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

当前使用重构后的 3.0 模块化体系，正式运行资料统一放在 `data/accounting`。项目 MCP、记账 Skill 和看板共用 `data/accounting/catalog.sqlite3`，每家公司有独立的业务库、原件、当前标准业务文档及备份。

打开当前看板：

```powershell
& '.tmp-kernel-venv/Scripts/python.exe' -I -B -X utf8 -m ai_accounting_modular.host.runtime.cli --catalog 'D:/GitHub/ai-accounting-core/data/accounting/catalog.sqlite3' serve
```

安装、公司接入、CLI、MCP 及离线恢复／重建统一见[安装、运行与会计 AI 接入](docs/redesign/09-runtime-and-ai.md)，当前账套与验证结果见[交付结果](docs/redesign/10-delivery-status.md)。普通启动只打开现有环境，不自行换库或重放。

开发看板需要实时看到前端修改时，在上述 V3 后台运行后执行：

```powershell
npm --prefix frontend run dev
```

打开 `http://127.0.0.1:5173/`。开发页面默认连接同一个 `data/accounting/catalog.sqlite3` 后台，复用负责人身份及公司库；修改 Vue、TypeScript 或样式后由 Vite 热更新。需要指定其他 V3 目录库时，在启动 Vite 前设置 `FINANCE_MODULAR_CATALOG` 为目录库文件路径。开发代理不启动、迁移或替换数据库；后端源码变化仍需单独重启 V3 后台。

## 旧版资料

`finance-local` 及 `data/kernel-released` 等属于旧体系 `ai-accounting-kernel/2`，与当前模块化体系的数据和备份格式不同。旧版内核、账本及保全资料仍独立存放，不能用旧入口办理当前账套。5173 开发页面现已连接 V3。旧版说明见[运行入口](docs/local-kernel-startup.md)、[会计操作](docs/local-kernel.md)和[独立打包](docs/local-kernel-packaging.md)。

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
