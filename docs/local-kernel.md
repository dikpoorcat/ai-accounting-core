# 本地 SQLite 会计内核

本实现位于 `src/ai_accounting/kernel/`，以确认事实、正式计算和统一事务发布为边界。
默认运行入口使用新的公司 SQLite 文件和常驻服务；旧账依原件、事实确认和回放补充说明重建。
新内核可从空公司运行，也可恢复自身生成的便携公司包，不直接导入旧凭证、余额或计算结果。

## 运行

在仓库 PowerShell 中准备受控运行时：

```powershell
.\scripts\kernel-runtime.ps1
.\.tmp-kernel-venv\Scripts\python.exe -m ai_accounting.kernel.cli --root .\data\local-kernel call schema
```

脚本使用仓库内 uv 0.12.3 创建独立虚拟环境，固定 CPython 3.12.13，安装锁文件中的依赖。
本机验收使用 SQLite 3.53.1；连接工厂拒绝低于 3.51.3 的 SQLite。
原 `.venv` 与系统 Python 不被替换。可生成包含运行时、依赖、页面和空白报表模板的独立软件包：

```powershell
.\scripts\package-local-kernel.ps1
```

默认在 `.tmp-local-distribution/<时间戳>` 生成 ZIP、文件摘要清单及验证结果。
打包后实际移位解压验证，Python 使用包内相对搜索路径并忽略主机 Python 环境变量；
验证包括类型化入账、幂等重试、备份恢复、页面访问和 STDIO MCP。
软件包不含公司数据、工作资料、`.env`、原 PostgreSQL 服务或开发工具。
运行包的生成和使用见 [独立运行包说明](local-kernel-packaging.md)。

将以下 JSON 保存为 `create-company.json`，换成待新建公司的已确认身份后执行：

```json
{"taxpayer_id":"91310000999999999X","name":"架构验收演示公司"}
```

```powershell
.\.tmp-kernel-venv\Scripts\python.exe -m ai_accounting.kernel.cli --root .\data\local-kernel call create_company --input .\create-company.json
```

返回的 `id` 是后续命令的 `company_id`。每次调用都从目录重新绑定公司和数据库实例身份。
目录结构是 `catalog.sqlite` 与 `<统一社会信用代码>/company.sqlite`。
活动文件必须放在本地磁盘。已知 UNC、映射网络驱动器和 OneDrive 目录会拒绝；其他同步软件也不能同步活动文件，应只同步生成后的备份包。

日常启动使用 [本地服务与身份验证说明](local-kernel-startup.md) 中的启动器。
负责人密码由原生窗口验证，CLI、MCP、页面和后台任务共享同一资料根目录的常驻服务。
开发环境可以先构建页面，再启动服务：

```powershell
npm --prefix frontend run build:release
.\.tmp-kernel-venv\Scripts\python.exe -m ai_accounting.kernel.cli --root .\data serve
```

通过启动器打开本机页面。服务只监听 `127.0.0.1`，校验本地调用身份、负责人会话及 Host / Origin。
页面支持公司、月份、科目和现金流汇总、待处理事项、凭证分页及逐层结果追溯。
金额通过十进制整数字符串传给浏览器，以 BigInt 格式化。

STDIO MCP 使用同一服务：

```powershell
.\.tmp-kernel-venv\Scripts\python.exe -m ai_accounting.kernel.cli --root .\data mcp
```

此入口公开 `finance_local_schema`、`finance_local_command` 和 `finance_local_security`，
作为常驻服务的薄适配。业务调用需要有效负责人会话，密码不进入命令参数或 MCP 业务载荷。

## 一个业务如何入账

1. 用 `evidence` 登记实际采用的原件内容：`content_base64`、`media_type`、`name`、`request_id`。
2. 用 `save_fact` 或 `save_facts` 确认类型化事实，并引用证据摘要。
3. 用 `preview` 传入稳定业务身份 `subjects`，查看确定性结果与事实问题。
4. 用 `confirm` 提交相同 `subjects`、`preview_digest`、`epochs` 和幂等 `request_id`。

`save_fact` 载荷示例：

```json
{
  "company_id":"创建公司返回的id",
  "kind":"expense",
  "subject_id":"supplier-invoice-2026-09-001",
  "data":{
    "period":"2026-09",
    "counterparty_id":"supplier-001",
    "amount_fen":126000,
    "expense_class":"administration",
    "creditor_kind":"supplier"
  },
  "evidence":["evidence命令返回的SHA-256十六进制摘要"],
  "expected_revision":0,
  "request_id":"confirm-invoice-2026-09-001"
}
```

`save_facts` 的 `facts` 数组使用相同记录结构，但每条去掉 `company_id`、`request_id`；
公司和请求编号放在批次外层。每批 1 至 5000 条，同一业务身份不能重复出现。
类型校验在写事务前完成，任一记录版本冲突或写入失败会撤销整个批次。
批量登记只确认事实，不隐式发布全部账务。

事实缺失返回 `needs_information` 和结构化 `fact_issues`。
对外没有接收科目、借贷方向或任意分录行的发布接口；预览里的分录是计算输出。

## 模块边界

| 代码 | 职责 |
|---|---|
| `types.py`、`contracts.py` | 整数分、月份、实际日期、声明式读取、类型化事实和纯计算结果 |
| `domains/` | 交易结算、薪酬、税费、资产融资、银行、现金与垫付的事实及计算器 |
| `storage.py`、`schema.py` | 版本、证据、类型化字段与重复子记录的持久化和通用数据库保护 |
| `engine.py` | 一致快照、依赖图、预览、幂等、统一发布、开放期替代和闭期补偿 |
| `accounting.py`、`domains/accounting.py` | 核算等价及旧结果兼容；完整结果摘要和精确依据独立保留 |
| `query_reads.py`、`query_semantics.py`、`business_queries.py` | 请求内精确读取、冻结采用证明及共同财务位置、清偿和期间准备 |
| `read_indexes.py` | 与源事务同步的原始引用目录、命中核验和显式完整性验证 |
| `dashboard*.py`、`display.py`、`provenance.py` | 五页只读投影、内容采用及历史补充资料的精确来源 |
| `periods.py` | 资料覆盖、业务模块声明的月末义务、关账清单和冻结查询 |
| `workflow.py` | 已确认外部义务、实际完成依据和跨月待办 |
| `payroll_tax_declarations.py` | 实际工资申报税额及明确采用的代发口径，保留未代发差额 |
| `reports.py`、`exports.py` | 财务三表、分类核对、银行代发与提交后可重试文件任务 |
| `runtime.py`、`backup.py`、`catalog.py` | 受控 SQLite 连接、完整公司备份与目录身份绑定 |
| `service.py`、`cli.py`、`mcp.py`、`http.py` | 本地调用适配；业务计算不放在适配层 |

业务模块声明事实模型、读取范围、计算器及可选的期间检查，通过 `Registry` 注册。
跨模块输入由 Context 提供，计算器不读数据库、文件、当前时间或随机源。
真实收付、核销和税点可以在一个依赖图中计算，最后由同一个事务发布。

## 保存什么，如何更正

稳定 `subject` 对应追加的 `fact_revision`；公共修订元数据与领域类型字段分开保存。
日期和金额不是任意 JSON 字段：月份落库为年月序号，分为 64 位整数，证据摘要为 32 字节 BLOB。
分配行、银行流水行等重复类型记录使用独立 STRICT 子表。
JSON 保存复合政策明细、正式计算解释和冻结清单。

`fact_current` 表示最新确认事实，`calculation_current` 表示当前有效的会计计算。
初次未发布和来源变化都会进入 `pending`；保存新事实不会偷偷替换已发布凭证。
具体版本依赖与类型化范围依赖同时记录，空范围也会记录。
计算器可用明确的版本引用读取已封存事实或计算，用于已申报依据等历史比较；
这些版本按主键加载，不会被同批次较新的计算覆盖，也不扫描全部历史版本。
已经退出有效结果图的旧计算保留审计，不继续参与当前更正传播。

新依据复核后，若核算意义与核算月份未变，保存新的计算依据及 `review_no_impact` 处置，
沿用已封存凭证，不增加凭证或冲正；以后真正发生金额等变化时，仍能定位并更正原有效凭证。
核算意义包含分录、义务身份、余额及后续计算状态，不仅比较金额。原完整 outcome、result_digest
及精确引用保持不变；核算签名不能替代内容有效性、提交并发版本或真实办理依据。细则见
[核算等价合同](accounting-equivalence.md)和[历史来源与内容版本](history-content-versions.md)。

开放期通过新结果版本替代，保留凭证身份和编号。关闭截止线以内的账务保持不变，
更正必须指定开放 `correction_period`，直接反向原凭证，再发布必要的新处理。
原凭证、原事实、实际付款都可以追溯。工资减少导致的已付差额必须有明确追偿等处理依据。
付款分配可通过专门事实版本修订，已发生的金额、日期、账户和收付主体不能被工资重算改变。

`preview_delete` / `delete` 只撤去允许撤销且没有当前下游依赖的开放业务，保留历史和编号。
实际行为的录入错误使用 `amend_fact`，明确 `recording_error_confirmed=true` 并引用纠错依据；
它追加修正版，原记录不改写。撤去误录的实际行为还须向删除预览及确认传入同一 `recording_error_evidence`。
新发生的付款或重新申报使用新业务身份，不能伪装成旧记录纠错。
管理说明、归集月份、收款姓名账号单独追加版本；普通核算不因管理说明改变而过期。

## 提交与封存

读事务加载一致输入后结束，在锁外计算并复核预览。
写入使用 `BEGIN IMMEDIATE`；检查幂等与相关状态版本后，一次提交事实、计算、凭证、依赖、审计和汇总。
相同请求重试返回原结果，同键不同载荷拒绝；包括真实 COMMIT 失败在内的异常都显式回滚。

分录先写，版本头后写。延迟外键禁止提交孤立分录；版本头聚合验证正数、至少两行和借贷平衡，
版本头存在后禁止修改或追加分录。事实和计算也有封存关系保护，不能事后补挂依据。
数据库不再重复计算工资、税额或折旧。

核算、资料、管理分别维护版本。普通核算核对核算版本，关账核对核算与资料版本，完整代发核对三者。
计算版本保存事实和规则版本、输入范围、结果与程序内容摘要；历史查询直接读取已存结果。

## 查询、关账与恢复

科目发生额、现金流和往来余额在发布事务内同步维护。
更正减去旧影响、加上新影响；余额为零的投影行统一省略。
`rebuild` 可从有效凭证和计算重建，验收逐分比较重建前后结果。
共同查询以稳定业务身份关联事实、计算、凭证、清偿与外部办理；看板在同一公司库读事务内
返回版本、完整汇总及分页明细。目录库公司列表不承诺与公司库跨库原子读取。
历史财务位置采用精确冻结 trial balance 及必要的同步增量，不以当前无期间余额代替。
完整 CLI/MCP `business_status`、`period_readiness` 与页面投影复用共同含义；页面准备投影
不能替代正式关账检查。详见[统一业务查询](unified-business-queries.md)。

增长集合先选本页实体或来源再展开；游标绑定公司、期间、筛选、实体及快照版本，文件任务另
绑定工作器状态版本。客户端不得把已加载页当作全部数据或拼接不同版本。
无分录冻结采用须有完整相关证明；未证明状态继续为 `unestablished`，未知金额保持 `null`。
有限期初包采用证明不推广到普通依赖，不用当前头或相同净额推定历史采用。

关账检查资料覆盖和未处理事实，并调用业务模块声明的资产折旧、借款计息、银行对账等期间义务。
已有业务的较早月份必须先关闭。冻结清单保存有效版本、来源、分类、资料依据、负责人确认及上一关账摘要。
闭期核算沿精确冻结引用；历史页面可按明确身份补充现有管理资料，并逐字段标注实际来源与冲突，
不把后来资料冒充当时采用的核算或经营说明。历史清偿截至所选月末，当前跟进仅纳入这些业务
精确相关的后来事项；核算已冻结、外部办理完成和文件任务成功彼此独立。

连续历史关账先用 `preview_close_range` 核对完整起止范围。多家公司可通过原生
`approve_close_batches` 窗口一次密码确认各自的具体预览，再逐公司调用 `close_range`。
每家公司全范围原子提交；跨公司的成功结果分别记录。响应丢失或部分成功时复用原请求、
批准和幂等键恢复，不逐月重复输入密码，也不关闭批准截止月之后的月份。

备份先用 Online Backup API 生成一致 SQLite 快照，再验证完整性、外键、公司身份、证据摘要和关账清单。
每家公司独立封装为 `<统一社会信用代码>.finance-company.zip`。
`restore_company` 校验便携包后导入没有同一身份、没有目标数据库的目录，禁止合并或覆盖既有公司。
常驻服务启动时恢复后台任务；失败有明确状态并最多自动尝试三次，必要时可显式重试。
账务提交不随文件生成失败撤销。
`jobs` 可跨会话查询待运行、失败或完成任务及产物位置；关账成功与便携包生成成功是独立状态。
季度三表须在首次相关关账前确认并启用报表口径；每月分类及季末内部所得税口径先检查，外部实际申报在关账后办理。
具体闭环见 [季度三表与冻结导出](local-kernel-reports.md)。

## 外部义务与流程状态

`workflow` 按 `period` 和明确的查询日 `as_of` 返回月度步骤及跨月义务；
`as_of_semantics=current_knowledge` 表示按当前事实、正式复核及当前关账状态判断过去业务日，
不是还原当时系统知识。当前已关账状态不按 `as_of` 截断，完成列表也不是全部历史修订。
`obligation_basis` 按 `obligation_id` 返回可用于确认外部完成的当前正式计算集合。
生成文件、资料齐全、工资计提和实际申报是独立事实，不能彼此代替完成状态。

`external_obligation` 使用 `obligation_kind`、`start_period`、`end_period` 确定事项身份范围。
`applicability_confirmed=true` 必须有证据；`applicability` 为 `required` 或显式 `not_applicable`。
未登记事项仍属于适用范围未明确，工资资料清单的 `no_business` 不能替代不适用确认。
`due_date` 是可空的管理期限：有来源时保存实际截止日，未知时不推定到期，也不编造日期。

真实外部提交使用 `external_completion`，保存 `obligation_id`、当时的 `obligation_fact_id`，
以及明确接受的 `accepted_calculations`（每个业务身份对应一个确切计算版本）。
`completion_status` 区分 `submitted` 与 `confirmed_complete`，均须引用真实完成凭据。
`date_status=known` 时提供 `completion_date`；历史完成日期未建立时使用 `not_established` 并保留空日期。
`period` 是记录所属月，不能把它的月末当作实际完成日。日期未知时，流程使用该确切事实版本的
不可变确认审计时间作为已获知完成的保守上界，按固定 UTC+08 自然日与 `as_of` 比较；
查询确认日及之后可以显示已完成，查询更早日期则不能提前显示。没有对应确认审计时保持未建立。
已知 `completion_date` 仍按实际业务日判断，不以较晚的录入时间替代。
返回的 `recorded_completions` 分别提供 `basis_current`（依据是否仍有效）、`known_as_of`
（按当前知识判断指定业务日是否已能证明完成）和 `confirmation_recorded_at`（精确事实的系统确认时间）。
有实际完成日的记录也返回确认时间；`completion_time_basis` 指明采用实际日、确认时间或无法建立。
该确认时间不代表负责人最早知悉日；无可信或唯一审计关联时为空。
`completion_date` 始终保留原值；幂等重放及后续重算不会改写最初确认时间。
已有完成记录但依据变化时，记录继续可见，义务的 `basis_review_required` 标明需要复核；
查询时间元数据不进入纯业务计算结果，也不替代真实申报凭据。

工资、奖金、劳务等来源必须全部形成对应的正式结果，才能取得完整申报依据。
有效工资档案还要求覆盖月份有明确工资事实和正式处理；零工资可以显式确认，
离开工资核算范围使用档案有效期，不从缺记录推断无薪酬。
同一员工月份检查也用于关账，防止工资整月漏建。空计算集合另需
`no_reportable_activity_confirmed=true`，该确认不能绕过已知缺失或未发布工资。

重算保留原提交接受的版本和实际日期，只重新判断 `basis_current`。
核算等价判断遵循 [核算等价合同](accounting-equivalence.md)，原完整结果摘要和真实已接受版本仍保留；
只调整 `due_date` 不使原提交失效。
结果等价的新计算版本经正式复核后，原提交仍有效：`accepted_calculations` 保留真实采用的旧版本，
`reviewed_calculations` 单独记录此次复核的当前版本；未发布的复核不能消除待办或绕过关账检查。
新的实际提交使用新业务身份，录入错误才使用有证据的 `amend_fact`，实际付款不随工资或流程重算改写。
季度税费及报表完成须先关闭覆盖月份，完成事实记录在覆盖期之后，不能反向阻止这些月份关账。
它明确接受当次当前正式计算，并不等同于旧关账清单；闭期更正后旧关账快照保持不变，
原提交依据可以因此过期。第 2—4 步依据实际核算和外部完成情况显示状态，不因材料齐全自动完成。
季度核算集合排除外部提交及其流程复核，避免流程自己的状态变更制造重新申报义务。

## 实际工资申报税额与代发口径

`payroll_tax_declaration_actual` 独立保存每人的工资税期、实际申报税额和不可变原件。
`declared_tax_fen` 不得以计算值或未知的零补齐；`declaration_date` 未知时保持空。
该事实不改变工资计算、累计税额、税款应付或现金，也不证明实际代扣已经发生。
同一人员税期存在多个当前申报明细时明确报冲突，不按金额相同或保存顺序选择。

公司明确决定按实际申报额代发时，保存并发布 `payroll_disbursement_basis`，
引用工资稳定身份和确切 `declaration_fact_id`。纯计算从原工资净薪中减去
“实际申报税额 − 原计算税额”，生成有依据的代发目标；不重新计算税法或更换工资基数。
目标为负、超过原净薪应付或已核销金额超过目标时，需要明确差额处置。
例如税差为 600 元，代发少付 600 元，该差额仍是未结清的工资应付，不能声称已经代扣。

代发预览的 `payroll_disbursements` 保留原工资计算、申报事实、采用口径计算、已核销额和
未代发额，`total_held_fen` 汇总保留差额。确认及后台文件任务冻结这些版本与相关状态版本。
真实付款继续使用原工资净薪义务部分核销；生成文件不产生资金、税款或虚假的结清。
完整导出不会静默略去冲突人员，指定 `source_ids` 只检查被选工资对应的申报及口径；
无关人员的待处理代发计划、未申报劳务或外部完成状态不会新增全局代发门禁。
原有资料完整性和真实会计更正检查继续有效。纯代发管理计划也不进入季度会计申报依据，
不使已闭期账务改变。

## 验证入口与读取边界

按改动风险选择必要检查，以下是可用入口，不是每次修改的固定门禁：

```powershell
.\.tmp-kernel-venv\Scripts\python.exe -m pytest
.\.tmp-kernel-venv\Scripts\python.exe -m ruff check src/ai_accounting/kernel tests/kernel scripts
npm --prefix frontend test
npm --prefix frontend run build:release
```

测试使用隔离公司文件，覆盖空范围来源后补、工资与付款联动、开放期保号、闭期冲正、
数据库封存与真实提交失败、状态版本竞态、零余额汇总重建、现金与银行、税点及退抵、文件恢复。

银行代发文件的生成和恢复见 [银行代发生成与恢复](kernel-payment-exports.md)。

公司库当前 v11、目录库 v3；目录 v1—v3 和公司 v1—v11 合同均已冻结，后续结构变化追加前向
事务迁移。v10 的 close/job/audit 目录只保存原始精确引用，不保存采用或完成结论；v11 仅对精确
识别的历史 v10 结构补齐读取索引，不重建引用目录。普通读取核验命中的源引用，不能发现任意
删除后不再命中的遗漏；迁移及显式完整性验证比较完整引用多重集合。不得在每次连接时全库扫描
或自动回填。

有界读取限制无关历史装载和页外对象展开，不承诺总成本恒定。完整相关分类校验、历史现金
汇总、相关清偿和完整冻结图保留真实输入成本；细则见[有界看板查询](bounded-dashboard-queries.md)。
