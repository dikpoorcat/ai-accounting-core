# 本地 SQLite 会计内核

本实现位于 `src/ai_accounting/kernel/`，以确认事实、正式计算和统一事务发布为边界。当前使用新系统 `ai-accounting-kernel/2`，目录库和公司库均为 `draft / 0`，公司备份格式为 2。以下接口和模块说明描述当前实现。

[目标架构](kernel-refactor-architecture.md)按全新开发、全新库推进。通用运行层已拒绝旧系统数据库和备份，保留同系统正式版本的未来升级能力；开发库只接受当前精确指纹，不自动升级或重建。第 9 阶段才冻结正式首版，各业务阶段状态见[路线图](kernel-refactor-roadmap.md)。本轮不包含真实重录或换库。

## 运行

在仓库 PowerShell 中准备受控运行时：

```powershell
.\scripts\kernel-runtime.ps1
.\.tmp-kernel-venv\Scripts\python.exe -m ai_accounting.kernel.cli --root .\data\kernel-draft call schema
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
.\.tmp-kernel-venv\Scripts\python.exe -m ai_accounting.kernel.cli --root .\data\kernel-draft call create_company --input .\create-company.json
```

返回的 `id` 是后续命令的 `company_id`。每次调用都从目录重新绑定公司和数据库实例身份。
目录结构是 `catalog.sqlite` 与 `<统一社会信用代码>/company.sqlite`。
活动文件必须放在本地磁盘。已知 UNC、映射网络驱动器和 OneDrive 目录会拒绝；其他同步软件也不能同步活动文件，应只同步生成后的备份包。

应用自有目录、数据库、边车、日志和锁统一使用私有权限。Windows 收紧为当前用户及 SYSTEM，目录保留子项继承；POSIX 文件分支使用目录 0700、文件 0600。只处理已知自有对象，拒绝链接及不能落实的权限。外部备份父目录不改，只保护私有暂存空间和本公司的当前／上一份 ZIP；共享位置的恢复输入可以读取。同账户进程的文件控制能力仍是本机信任边界。

日常启动使用 [本地服务与身份验证说明](local-kernel-startup.md) 中的启动器。
负责人密码由原生窗口验证，CLI、MCP、页面和后台任务共享同一资料根目录的常驻服务。
开发环境可以先构建页面，再启动服务：

```powershell
npm --prefix frontend run build:release
.\.tmp-kernel-venv\Scripts\python.exe -m ai_accounting.kernel.cli --root .\data\kernel-draft serve
```

通过启动器打开本机页面。服务只监听 `127.0.0.1`，校验本地调用身份、负责人会话及 Host / Origin。
页面支持公司、月份、科目和现金流汇总、待处理事项及凭证分页。
金额通过十进制整数字符串传给浏览器，以 BigInt 格式化。

STDIO MCP 使用同一服务：

```powershell
.\.tmp-kernel-venv\Scripts\python.exe -m ai_accounting.kernel.cli --root .\data\kernel-draft mcp
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
| `dependencies.py` | 数据库、同批计算、实际读取记录和失效传播共用的选择语义 |
| `integrity.py`、`projections.py`、`period_balances.py`、`settlement_projection.py`、`maintenance.py`、`read_state.py` | 权威内容核验、当前余额四表、期间余额与清偿贡献投影、校验封印、受控维修和读取修复计数 |
| `dashboard*.py`、`display.py`、`provenance.py` | 五页只读投影、内容采用及历史补充资料的精确来源 |
| `periods.py` | 资料覆盖、业务模块声明的月末义务、关账清单和冻结查询 |
| `workflow.py` | 外部义务、真实办理、账务核对和精确依据 |
| `worklist.py` | 公司工作起点、自动选月及六类业务状态 |
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

`fact_current` 表示最新确认事实；`calculation_publication` 形成每个业务主体唯一、不可分叉的
正式发布链，并决定当前有效的会计结果。`calculation_current` 继续作为可重建的当前指针。
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

发布预览逐项返回业务 `source_period`、实际 `posting_period` 和动作 `mode`，确认事务内重新
核对同一决定。开放期通过新结果版本替代，保留凭证身份和编号；参数与所属月冲突时拒绝。
关闭截止线以内的账务保持不变，所属月已关账的迟到业务或更正必须指定开放
`posting_period`；内核保留冻结月份，并在指定月份记录新旧正式结果的差额。无影响复核
保留原入账月和凭证，同时在发布链记录新的采用依据。自动重算的开放依赖分别保留原入账月；
显式选择的根业务与指定月冲突时拒绝。
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

核算、资料、管理分别维护版本。普通核算核对实际依赖的版本，关账核对三类版本及读取修复版本，完整代发同样核对实际采用的全部版本。
计算版本保存事实和规则版本、输入范围、结果与程序内容摘要；历史查询直接读取已存结果。

## 查询、关账与恢复

科目发生额、现金流、期间余额贡献和清偿贡献在发布事务内同步维护。
更正按固定比较基准减去旧影响、加上新影响；余额为零的投影行统一省略。
`rebuild` 可从正式发布链和保存结果重建派生投影，验收逐分比较重建前后结果。
共同查询以稳定业务身份关联事实、计算、凭证、清偿与外部办理；看板在同一公司库读事务内
返回版本、完整汇总及分页明细。目录库公司列表不承诺与公司库跨库原子读取。
历史财务位置按实际入账月还原，独立期初不计入本期发生额；当时冻结只读取该次关账直接
保存的发布、计算、凭证拥有者与采用依据，不以当前结果替换。
完整 CLI/MCP `business_status`、`period_readiness` 与页面投影复用共同含义；页面准备投影
不能替代正式关账检查。详见[统一业务查询](unified-business-queries.md)。

增长集合先选本页实体或来源再展开；游标绑定公司、期间、筛选、实体及快照版本，文件任务另
绑定工作器状态版本。客户端不得把已加载页当作全部数据或拼接不同版本。
无分录结果、独立期初和资产批次都必须由关账合同直接声明采用关系；依赖中出现的结果不能
冒充采用。必需采用内容缺失属于内容错误，未知业务金额仍保持 `null`。

关账检查资料覆盖和未处理事实，并调用业务模块声明的资产折旧、借款计息、银行对账等期间义务。
已有业务的较早月份必须先关闭。冻结清单保存有效版本、来源、分类、资料依据、负责人确认及上一关账摘要。
闭期核算沿精确冻结引用；历史页面可按明确身份补充现有管理资料，并逐字段标注实际来源与冲突，
不把后来资料冒充当时采用的核算或经营说明。历史清偿截至所选月末，当前跟进仅纳入这些业务
精确相关的后来事项；核算已冻结、外部办理完成和文件任务成功彼此独立。

关账按公司逐月执行 `preview_close`，负责人在经营简报查看同版核对内容和实际采用依据，
再用原生 `approve_period_close` 密码窗口批准，由 AI 调用 `close`。窗口只签发批准，页面只读。
同公司同月的新预览替代旧预览；摘要、分页、批准和提交固定同一 `preview_digest`。
业务版本或读取修复版本变化会使旧预览失效。响应丢失先查原状态并复用幂等键。
冻结的 `owner_review` 从精确原版依据读取，被后续关账覆盖的空月不冒充独立批准。

备份先用 Online Backup API 生成一致 SQLite 快照，再核验结构、外键、公司身份、证据、全部保留事实／计算／凭证、正式发布链、关账直接采用和派生数据。返回 `verification`；必要采用依据缺失属于内容错误，备份和恢复都明确拒绝，不降级为有限核验。旧文件和旧 ZIP 不因核验而改写。
每家公司独立封装为 `<统一社会信用代码>.finance-company.zip`。
`restore_company` 校验便携包后导入没有同一身份、没有目标数据库的目录，禁止合并或覆盖既有公司。
常驻服务启动时恢复后台任务；失败有明确状态并最多自动尝试三次，必要时可显式重试。
账务提交不随文件生成失败撤销。
`jobs` 可跨会话查询待运行、失败或完成任务及产物位置；关账成功与便携包生成成功是独立状态。
季度三表须在首次相关关账前确认并启用报表口径；每月分类及季末内部所得税口径先检查，外部实际申报与账务核对分别登记，财报办理等待相关期间关闭。
具体闭环见 [季度三表与冻结导出](local-kernel-reports.md)。

## 工作清单、外部办理与恢复

`workflow(company_id, as_of, period?)` 是公司级工作起点，`period_readiness(company_id, period, as_of?)` 读取明确月份。两者和看板共用当前期间检查；清单不再是固定九步。银行、工资、普通业务、税务、资产、融资各有资料与核算状态，另列关账、实际办理和文件交付。详细原件及业务按需查询，汇总不依赖已加载页。

省略月份时，从已登记事实、资料、正式入账及已建事项选最早开放待处理月；未来月不自动选入。闭期真实问题由开放月承接，办理和文件任务在公司范围保留；没有开放任务时用最近处理月作背景。空公司没有可靠月份时返回 `period=null`，不能解释为全部完成。选月不根据成立日期补造空月，不逐月执行完整关账检查。

`as_of_semantics=current_knowledge` 表示按当前事实、正式核对及当前关账判断指定业务日，不还原当时的系统知识。当前关账不按查询日截断。普通未结款项不是到期付款指令，未知期限不会被推定逾期。

`external_obligation` 以 `obligation_kind/start_period/end_period` 声明精确范围。六类义务为社保申报、个税申报、季度税务、季度财报、年度所得税和年度工商报告。`applicability_confirmed` 及不适用结论须有证据；资料清单的无业务不能代替适用性。`due_date` 是可空管理期限，不补造日期。

`obligation_basis` 返回当前可核对的 `candidate_calculations` 和 `fact_issues`，不是已经采用的申报依据。`external_completion` 单独记录真实办理，绑定义务精确版本，保存 `source_facts`、实际采用的 `accepted_calculations` 或办理凭据 `adopted_evidence_digests`。可以先登记实际个税、社保资料，不要求先发布工资；空计算集合不能自己证明无业务。`completion_status` 区分 `submitted` 与 `confirmed_complete`。真实补报另建事实，以 `previous_completion_fact_id` 精确接续。

`date_status=known` 时记录真实 `completion_date`；未知时保留空日期。记录所属月不能代替实际日期。日期未知时，以该精确事实版本不可变确认审计的 UTC+08 自然日作为获知完成的保守上界；缺少可信唯一关联时保持未建立。`recorded_completions` 返回 `known_as_of`、`confirmation_recorded_at` 和 `completion_time_basis`。幂等重放、重算和后续核对不改写原确认时间。

`external_basis_review` 引用办理事实、义务及正式账务的精确版本，明确核对来源、原采用结果与当前核对结果，表达一致、差异或尚无法核对。它通过统一预览和发布产生无分录结果，不生成税额算法或伪造申报。实际个税及社保明细按保存的精确人员、月份、个人和单位金额核对；工资人员范围、未发布来源及无业务依据仍分别检查。

接口分别提供 `actual_completion_status` 和 `basis_review_status`：可以已申报而账务待核对。后续账务变化让核对过期，不抹去办理；新补报需要自己的核对，不能继承原办理的结论。等价比较遵守 [核算等价合同](accounting-equivalence.md)，正式引用保留精确版本。季度税务可先保存真实申报，季度财报须等待范围内核算和关账完成，空月可被后续关账覆盖，不补造批准。

实际办理、实际扣税、真实缴款和文件生成彼此独立。外部办理或核对未完成、个税文件映射问题不增加关账门槛，冻结内容不随新工作状态回写。

版本化 `agent_operating_protocol` 随 `schema` 发布。泛化开始展示完整清单，明确事项直接处理；先查资料再问，老板回答只适用于刚展示的公司、期间和事项。实际推进后更新进度，未推进不反复刷新；中断和公司切换重新读取上下文，不沿用另一公司的候选、预览或批准。

响应丢失时重放原载荷及原请求键；`request_result(company_id, submitted_request_id)` 核对现有请求与审计后返回 `committed` 和原回执，或 `unknown`。未知不代表失败，不能另记一笔。目录创建和恢复继续查询 `operations`。任务持久保存 `error_code`，公开直白安全说明，本地保留原异常；自动尝试耗尽后先处理原因，再显式重试原任务。技术故障、待重算和过期预览不能当作老板缺少业务事实。

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

目录库和公司库使用独立类别、`schema_meta`、`user_version` 和不可变 `schema_history`，业务身份不包含结构版本。`npm run db-contracts:check` 只比较生成 DDL 与包内开发合同；`npm run db-contracts:generate` 只更新开发合同文件，不修改数据库。首个正式版本冻结后使用父合同约束的增量结构合同及明确事务迁移，安装历史只记实际经过的步骤。

close 保存不可变的直接采用权威；job/audit 和读取目录保存精确源引用或索引，不推导采用或
完成结论。普通读取核验命中的引用，显式完整性验证核对完整引用；普通连接不扫描全部业务
或自动回填。`state.read_repair_revision` 在派生数据实际维修后原子增加，供页面、按需检查与
导出识别读取变化，不成为第四个业务 lane。

显式调用 `verify_integrity` 只读核验完整内容；`rebuild` 原子修复当前余额四表、期间余额、清偿贡献及其校验封印；`repair_read_indexes` 原子修复三张引用表及其来源标记。维修先检查权威源，不能修补历史事实或依赖；无差异不增加读取版本。普通发布核验本次来源和投影变化，新关账额外执行全科目独立核验。内容错误返回 `content_integrity_failed`，不能当成缺少业务事实向老板追问。

有界读取限制无关历史装载和页外对象展开，不承诺总成本恒定。完整相关分类校验、历史现金
汇总、相关清偿和完整冻结图保留真实输入成本；细则见[有界看板查询](bounded-dashboard-queries.md)。
