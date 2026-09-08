<!-- @format -->

# 组合协议空库重录与终态核对

目录库基线为 `0001_catalog_baseline_v2`，公司业务库基线为
`0001_business_baseline_v3`，分别使用独立 PostgreSQL 17 数据库和迁移树。
旧业务基线及至 `0006_pass_through` 的旧场景迁移不支持原地升级。
此次重构只交付代码、隔离测试和重录资料，不自动重录、清空或更正试用公司。

## 1. 先保全最新事实

源数据库必须强制只读连接并在一致性事务中读取；不得仅凭旧回放包推断最新状态。
逐公司保存业务事实、原始证据、银行流水、领域资料和终态快照，校验证据字节数及 SHA-256。
不导出密码哈希、恢复码、会话令牌或数据库凭据。源 UUID 只供私有快照核验，执行清单使用
业务稳定引用，实际重录时解析为新编号。

本次最新只读快照和交付清单位于 Git 忽略的
`outputs/composition-reentry-20260908/`。`inventory.json` 记录源版本、数量、证据完整性和
与 9 月 3 日包的差异；原始包保持原地不动。一次性旧格式整理脚本保存在该私有目录，
不进入正式运行代码。旧数据的撤销、删除及冲正历史保留供核验，不机械重放已撤去的业务。

当前运行工具只接受 `ai-accounting-composition-replay-v2`，不接受旧 v1 请求或运行时转换。
今后从 v3 公司库刷新包使用：

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli export-system `
  --output .\outputs\composition-replay-YYYYMMDD
```

## 2. 审阅逐条重录清单并离线验证

逐公司清单按依赖排序，包括公司初始化、证据及人员资料、银行流水、业务入账、报表控制和
期间处理。每条业务注明来源、日期、整数分金额、组件键、资金分配和核对点。缺口必须单列，
不能用零额、推断税率、猜测债权人或默认现金用途补齐。

单项和组合业务均调用 `finance_record_event`，提交 `components` 与 `funds`。
工资、劳务、资产、借款和税务的必要试算、确认继续使用其专用流程；它们正式入账共用
内核提交器。资金项引用组件键，跨业务来源使用稳定业务引用，不能携带任意凭证行。
组合中需要确认的税务计算先调用 `finance_preview_event`，复核并提交返回的
`reviewed_request`。工资、劳务使用在新库重新预览的批次及哈希，不能沿用旧库编号。
同笔正常工资和合并计税奖金采用两级工资预览：先在新库预览正常工资，再用其新批次编号
预览奖金，奖金组件通过 `regular_payroll_component_keys` 保留对正常工资组件的稳定依赖。
随后统一调用 `finance_preview_event` 并只正式调用一次 `finance_record_event`。回放清单按依赖
执行预览，即使来源组件在 `components` 数组中位于奖金之后，也不得带入旧库的批次、工资行
或计算哈希。
同笔固定资产购置或启用与首个应计月份折旧，使用 `activation_component_key` 或
`activation_component_keys` 保留稳定依赖。新库回放时只提交这些组件键和业务事实，由
`finance_preview_event` 重新生成资产、启用来源证明及折旧哈希；不得沿用旧库的资产、启用
编号或计算哈希。来源组件即使在 `components` 数组中位于折旧之后，仍须先按依赖计算。

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli verify-package `
  --package .\outputs\composition-replay-YYYYMMDD
```

离线验证包括格式、完整清单、所有文件哈希、证据大小、操作次序与稳定引用、当前组件请求
结构。它只证明资料包一致，不能代替在目标库逐条执行后的余额和领域核验。清单存在未决
事实时必须先补充，不能称为已完成业务重录。

## 3. 明确选定空目标后初始化和登录

后续实际重录须由负责人另行发起。目标配置使用 `.env`，不得将真实密码写在命令行。
`DATABASE_URL` 指向独立目标目录库，公司运行、迁移和供应配置指向同一目标集群。
停止连接目标库的写进程，确认源库和目标身份不同。未知历史、已有表或非空目标立即停止，
不覆盖、不清理、不自动降级。

```powershell
$replayState = ".\outputs\.composition-replay.state.json"
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli prepare-empty `
  --package .\outputs\composition-replay-YYYYMMDD `
  --state-file $replayState
```

执行器先创建目录结构，再逐公司创建业务结构，核对独立 revision 和公司身份。使用返回的
`primary_org_id` 在本地无回显流程设置新负责人并登录；旧身份凭据、会话及审批不回放。

```powershell
$primaryOrgId = "prepare-empty 返回的 primary_org_id"
.\.venv\Scripts\python.exe -m ai_accounting.identity_cli setup `
  --org-id $primaryOrgId --login-name owner
.\.venv\Scripts\python.exe -m ai_accounting.identity_cli login --login-name owner
```

## 4. 逐条执行和期间处理

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli replay `
  --package .\outputs\composition-replay-YYYYMMDD `
  --state-file $replayState
```

先非默认公司、后默认公司；状态文件逐条记录成功结果和新编号，使用同一包及状态可断点续跑。
证据、人员、合同、卡片、应收应付来源先于引用它们的付款或核销。重复同类业务可在一笔中
出现，明确分配到同一笔实际收付；不同公司的事实不能组合。同笔组件依赖通过稳定键表达。

每个期间先补齐内核实际要求的工资、折旧摊销、税务和报表事实，再对账、复核并关账。
不能从缺少工资批次推断无工资。现有资料支持的历史控制按原依据重建，不伪造外部申报日期。
受控历史测试关账模式只在已明确指定的可丢弃测试库使用，成功和失败后均关闭；普通现账
关账继续使用密码复核和自动备份。任何失败保留状态和稳定错误码，不手工改库绕过。

## 5. 核对终态与正式启用

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli verify `
  --package .\outputs\composition-replay-YYYYMMDD `
  --state-file $replayState
```

核对科目期末余额、未结往来及债权人、银行流水和匹配、工资税务、资产借款、期间和报表，
并确认每张正式凭证借贷平衡、公司隔离、证据哈希完整。重组后不要求复现旧凭证编号、数量
或已冲正历史形成的累计借贷发生额。原账错误的差异必须另列原因、证据及确认结果，不能
照抄错误余额，也不能静默从检查点删除差异。

“资料已整理并离线验证”“隔离库测试通过”“业务已实际重录并核验”是不同状态，交付报告
必须分别说明。报告未通过不得启动正式服务。

重录通过后逐公司确认关账备份目录，调用 `finance_configure_close_backup` 和
`finance_get_close_backup_configuration` 核验。源机备份路径只作为参考，不自动照搬。
初始备份逐公司生成 `<统一社会信用代码>.finance-company.zip`，通过
`finance-backup verify-portable` 后交付。旧库、原始证据和旧包继续保留。
