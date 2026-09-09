<!-- @format -->

# 组合协议空库重录与终态核对

目录库基线为 `0001_catalog_baseline_v2`，公司业务库基线为
`0001_business_baseline_v4`，分别使用独立 PostgreSQL 17 数据库和迁移树。
旧业务基线及至 `0006_pass_through` 的旧场景迁移不支持原地升级。
2026-09-09 按负责人要求，将业务 v3 至 `0004_fact_precision` 的迁移归并为 v4。
预付项目成本、必要事实与管理资料解耦和日期精度已包含在新基线中。业务库后续迁移为 `0002_atomic_corrections`，增加联动更正和统一门禁。两棵迁移树分别执行至
`head`；旧业务链不能原地升级，末版 `0004_fact_precision` 仍可作为只读导出来源。

跨期预付款必须按真实日期先录预付、交付确认应付后再冲抵，不能把付款指向未来才形成的
应付。真实阶段验收则先录项目成本及债务，交付时按来源结转资产；字段及示例见
[供应商预付款与项目成本](purchase-project-components.md)。没有阶段验收证据时不改变为
阶段成本路径，也不从旧库错误核销结果推断业务事实。

本次修正版资料另存于 Git 忽略的 `outputs/`，保留原包及原始依据。修正版包先重新完成
离线验包，再由执行器按影响范围判断能否沿用既有回放状态：空库初始化所用的公司、基础资料或
科目配置发生变化，已完成操作的顺序或内容发生变化，新增引用需要状态中未保存的已完成结果，
以及已完成整包后又出现待执行操作时，必须创建独立空目标及新的状态文件；仅未执行操作、核验
资料或说明文件变化，且初始化投影与已完成操作前缀逐项一致时，可以更新包哈希绑定并断点续跑。
每次接受替换都在状态中记录旧、新清单哈希和判定时间。包哈希和协议校验只代表资料结构有效；
隔离测试数据回放成功也不代表试用公司已实际重录，仍须分别报告状态。

## 1. 先保全最新事实

源数据库必须强制只读连接并在一致性事务中读取；不得仅凭旧回放包推断最新状态。
逐公司保存业务事实、原始证据、银行流水、领域资料和终态快照，校验证据字节数及 SHA-256。
不导出密码哈希、恢复码、会话令牌或数据库凭据。源 UUID 只供私有快照核验，执行清单使用
业务稳定引用，实际重录时解析为新编号。

当前本机交付以 Git 忽略的 `outputs/README.md` 为索引；其中区分下一轮回放包、离线验证、
清理记录和清理前恢复存档。旧数据的撤销、删除及冲正历史保留在恢复存档供核验，
不机械重放已撤去的业务。保留原始来件；只有已经明确授权并保全恢复依据后，才清理旧资料。

当前运行工具只接受 `ai-accounting-composition-replay-v3`，不接受旧包协议或运行时转换。
业务库从正式空库基线 `0001_business_baseline_v4` 初始化；后续变更使用前向迁移。
从当前基线公司库或旧链末版 `0004_fact_precision` 只读刷新包使用：

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

执行器先创建目录结构，再逐公司创建业务结构，核对独立 revision 和公司身份，并保存状态。
随后自动请求原生“首次负责人设置”表单；`owner_security_window` 返回请求编号和状态。
在窗口输入两次新密码并确认已保存恢复码后自动登录；旧身份凭据、会话及审批不回放。
`starting` 仅代表正在启动，`waiting_for_user` 才表示表单已显示。
弹窗失败不撤销初始化，继续使用同一包和状态运行 `replay` 即可重新请求设置或登录。

```powershell
$primaryOrgId = "prepare-empty 返回的 primary_org_id"
.\.venv\Scripts\python.exe -m ai_accounting.identity_cli setup `
  --org-id $primaryOrgId --login-name owner
.\.venv\Scripts\python.exe -m ai_accounting.identity_cli security-window-status `
  --request-id "返回的 request_id"
```

## 4. 逐条执行和期间处理

```powershell
.\.venv\Scripts\python.exe -m ai_accounting.replay_cli replay `
  --package .\outputs\composition-replay-YYYYMMDD `
  --state-file $replayState
```

回放先检查实际目录和公司身份是否与状态文件一致。身份设置或登录尚未完成时，返回
`waiting_for_owner` 和当前目标库的窗口请求，不执行包内操作；失败返回 `blocked`。
窗口返回成功后再次执行同一回放命令，由内核重新验证本机会话。
每个目标实例独立保存会话；不要用仍连接现账库的 MCP 请求替代回放库窗口。
先非默认公司、后默认公司；状态文件逐条记录成功结果和新编号，使用同一包及状态可断点续跑。
执行时默认向 stderr 输出逐步 JSON 进度：当前公司、操作键、操作类型、已完成数量和耗时；
stdout 仍只输出最终 JSON 结果。需要安静执行时加 `--quiet`。状态中的 `last_run_timing`
和成功结果中的 `timing` 记录本次执行按操作类型汇总的耗时及检查点写入耗时，不包含启动、
验包和登录等待；它们是运行诊断，不参与会计计算或包哈希。

检查点采用紧凑 JSON，每项成功后写入临时文件、刷新并原子替换；替换失败保留上一份完整
状态，继续使用幂等键恢复。`replay` 和 `verify` 对同一状态文件互斥，重复启动立即返回
`REPLAY_STATE_ALREADY_IN_USE`。旁边的 `.lock` 文件可长期保留，操作系统会在进程退出时
释放锁，不能根据文件存在判断仍在运行，也不要在执行期间删除该文件。

历史银行匹配检查按业务和银行账户批量读取，合计仍包括同一业务跨月的所有有效匹配；
预览和确认分别读取最新快照。历史关账范围检查只批量读取确认时间，往来余额检查避免
额外加载整套凭证和证据。逐月顺序、余额校验、关账确认和每步持久化保持不变。
证据、人员、合同、卡片、应收应付来源先于引用它们的付款或核销。重复同类业务可在一笔中
出现，明确分配到同一笔实际收付；不同公司的事实不能组合。同笔组件依赖通过稳定键表达。

每个期间先补齐内核实际要求的工资、折旧摊销、税务和报表事实，再对账、复核并关账。
不能从缺少工资批次推断无工资。现有资料支持的历史控制按原依据重建，不伪造外部申报日期。
未确认试算草稿不直接证明业务发生；管理解读、外部办理进度和逐项打卡不作为关账门禁。
负责人针对本次快照确认完整性，授权、备份、已知应计、对账及账表一致性检查继续执行。
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

普通匿名往来按业务来源和稳定键核对，不要求先补对象。对象、用途、合同／项目标签和说明
放入组件 `metadata`；不得为满足旧门禁补造受益人、垫付日期或管理编号。
从新内核导出的资料包在会计业务及期间操作之后，通过 `finance_update_business_metadata`
逐版本恢复管理资料；明确清除值使用 `null`，不改写已形成的关账会计快照。

重新开始空库回放必须使用新状态文件，不能沿用上一轮已完成状态。新导出会保留负责人已
确认的工资输入，并以来源组件解析资产卡片及工资、劳务往来键中的新编号。未获确认的
业务事实仍须保持缺口，不能因初始化或资料整理补造日期、税务状态或项目验收事实。

“资料已整理并离线验证”“隔离库测试通过”“业务已实际重录并核验”是不同状态，交付报告
必须分别说明。报告未通过不得启动正式服务。

重录通过后逐公司确认关账备份目录，调用 `finance_configure_close_backup` 和
`finance_get_close_backup_configuration` 核验。源机备份路径只作为参考，不自动照搬。

当前本机正式回放完成后，使用负责人已确认的以下公司专属目录：

| 公司 | 关账备份目录 |
| --- | --- |
| 魂道（杭州）科技有限责任公司 | `D:\OneDrive\11、魂DAO\ai-accounting-company-backups` |
| 屋舍心声（杭州）房地产经纪有限责任公司 | `D:\OneDrive\12、屋舍心声\ai-accounting-company-backups` |

上述路径仅代表当前本机的负责人确认结果；切换主机、OneDrive 根目录或负责人另行变更位置时，
必须重新确认并逐公司追加配置版本，不得把该表作为跨主机自动默认值。

初始备份逐公司生成 `<统一社会信用代码>.finance-company.zip`，通过
`finance-backup verify-portable` 后交付。原始来件和最新恢复依据继续保留；旧库及旧包的
清理须明确指定范围，不作为回放命令的自动副作用。

## 原位更正与回放

来源事实更正使用 `finance_preview_correction`／`finance_confirm_correction` 的 `preview_confirm` 操作，
复用原业务引用，保留原凭证编号；具体协议和差额处理见[联动更正审查](atomic-corrections-review.md)。
管理资料、错误码或未完成试算不构成冲正依据。真实历史中的冲正审计保留为核对资料，
本次开发不会自动修复真实库、重置续跑状态或改写已完成操作。
