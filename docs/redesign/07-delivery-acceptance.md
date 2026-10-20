<!-- @format -->

# 开发任务包与验收

本文件把设计转成可以分别承接、独立验证并真实集成的交付任务。负责人统一开工依据、公共交接、持续集成和最终验收；各任务的编码与验证可以分别委托。同一交付 owner 表示唯一维护责任，不要求由负责人亲自编写全部代码。真实资料整理、换库和服务切换按实际指定范围另行执行。

开工依据是本目录完整设计、唯一 Schema、DDL 和预先写好的合成预期。实现者不能临场补定业务规则、接口或表结构，也不能以测试替身已经通过代替真实模块接通。

## 1. 全部任务先定完，再按依赖并行

编码前，由集成负责人核对 [产品需求](01-product-requirements.md)、[架构](02-architecture.md)、[接口合同](03-contracts.md)、[数据设计](04-data-model.md)、[业务规则](05-business-rules.md)、[看板适配](06-dashboard-adapter.md)及下列共同样例。每种支持业务必须有严格事实类型、完整结果含义、必要依据、正式表归属和独立预期；数据设计与 DDL 相符，Schema 引用可解析。缺少其中一项，就修正相关设计后再开始受影响任务，不让承接人猜。

设计编制中的文件不能当作已经实现的能力。全部任务包在开工前一并确定；依赖就绪后可以并行，不按业务类型反复拆出一百多个小任务，也不先写一部分代码再补决定其含义的设计。

共同验收资料如下：

| 资料 | 用途 |
| --- | --- |
| [公共类型](contracts/core.schema.json)、[业务事实](contracts/business-facts.schema.json)及其明确引用的领域 Schema | 输入输出、整数金额、事实、结果和引用类型的唯一真源。 |
| [目录库 DDL](schema/catalog.sql)、[公司库 DDL](schema/company.sql) | 空合成库结构、表归属、约束及索引。结构变更先修改所属合同，不能各自加同名表。 |
| [共同核心算例](examples/core-acceptance.json) | 本文件规定的跨模块金额、并发、失败接续和模块替换预期；不是新业务 API。 |
| 业务规则中的逐 kind 算例和合作投资算例 | 各家庭的公式、处理边界、凭证及特殊情况；共同核心算例不替代业务目录的逐项覆盖。 |
| [旧能力对照](reference/legacy-coverage.md) | 查漏与复用索引，不继承旧内部流程、审计或数据库格式。 |

任务包名称是交付责任，不要求同一个人完成，也不形成新的运行时模块。每包有一个交付 owner；领域包内各家庭另有独立 owner。公共合同及共享装配各由唯一 owner 修改，避免并行人员各改一份。

## 2. 六个任务包

### F：公共合同、存储与文件任务基础

**owner：基础设施负责人。** 交付 `src/ai_accounting_modular/contracts/`、`storage/`、独立 `files/`，生成/校验工具及 `tests/modular/contracts/`、`storage/`、`files/`。维护纯类型、错误、金额和日期校验、UnitOfWork/ReadContext、结构识别、空库创建及一致数据库快照；同体系变更遵守明确的前向合同，不建设没有实际需求的万能迁移引擎。files只管当前任务、运行接续和文件验证，不实现工资、报表口径或文档内容。

- 输入为唯一 Schema、两个 DDL、架构的单公司事务和备份边界；输出为严格类型、`storage.public` 的公开工厂及list_companies/company_metadata只读能力。业务模块使用注入的工作单元，不拿任意 SQL 入口。
- storage拥有目录库的 `catalog_meta/owner_profile/company_location`，公司库的 `system_meta/company_profile/module_schema`；files独立拥有 `file_task`。其他模块提交自己的结构合同和仓储；存储负责装配执行，不获得其业务写权限。FileTaskPort按共同合同提供ensure_current/get/update(expected_state,head)/verified_download，host注入各类producer；files不导入其业务实现。
- 允许改本包源码、所属测试、生成工具和公共合同；涉及业务字段时先同步唯一合同和对应预期，不能新增“什么都能放”的公共 JSON。禁止通过兼容旧格式接受旧库。
- 复用索引为旧 `kernel/types.py` 的金额/期间边界、`kernel/backup.py` 的校验思路；搬入前去除旧系统标识和总服务依赖。现有纯函数能否复用由新预期判断。
- 无产品模块先决依赖。独立验收覆盖金额溢出、bool/float/非法日期拒绝、SQLite 外键和严格表、事务全回滚、单公司隔离、只读快照、旧格式拒绝、新目标不覆盖、备份包损坏拒绝。
- 首个真实集成节点是 F+L+A：在临时公司库执行一次完整原子提交。此后与 A 的文档集合一起验收备份，不能只证明 SQLite 文件能复制。

### L：正式账务与结算

**owner：账务负责人。** 交付 `ledger/`，含期初与统一报表取数，及 `tests/modular/ledger/`。输入是领域程序产生的完整 `PostingPlan`、公开引用和同一工作单元；输出为 `ledger.public` 的正式采用、凭证、义务、清偿、成本和财务快照。所属期初/报表接续事实也实现describe_change/evaluate_slot/apply_current/export_current/query_current，不能由application代写。AI 不可直接提交计划或自由分录。

- 私有表为 `opening_fact/report_basis_fact/opening_member`、`adopted_result/result_slot_current/result_source`、`voucher/voucher_line/voucher_sequence`、`obligation/settlement/obligation_reclassification/cost_lot/cost_consumption/period_close`、`result_fund_movement/result_tax_sale/result_report_contribution`。资金/税销售/报表贡献与同一正式result头保存和冻结，供所有消费者公开查询，不各存一份可编辑正式金额。期初与接续事实仍走共同办理入口，由本 owner 的公开端口保存。
- 允许改本包及所属测试、结构合同；不写领域事实、真实银行行、资料待办或看板表。全平衡凭证、结果完整槽集合和稳定业务引用在此核验。
- 复用索引为旧 `kernel/domains/opening.py`、`kernel/report_projection.py`、`report_semantics.py`及对应合成测试；复用共享取数含义，不导入旧审批或开放期版本链。
- F 的类型及真实工作单元就绪后，先用手写确定计划独立验证，再接真实经营与工资领域。独立样例覆盖借贷不平衡拒绝、重复清偿/成本超耗拒绝、无凭证结果、保号替换、冻结不可改、闭期更正头替换及全范围汇总。
- 必须与 D+A 实跑核心 C01–C05；与读取包对照正式财务快照及完整分页。只测计划替身不算领域已接通。

### D：业务领域并行交付

**owner：各家庭负责人。** 本包按下表六个边界并行，每个 owner 只维护自己的模块和事实表。每家都实现 `describe_change/evaluate_slot/apply_current/export_current/query_current`：先声明责任、完整结果槽与机器依赖，应用按结果槽拓扑逐槽求值得到PostingPlan，完整ChangePlan才交owner保存；不提前按整业务算完全部槽。使用对应严格 Fact、采用依据和目标类型，不自行提交事务或更新别家私表。identity和records仍用其明确的plan_change，不套领域核算协议。

| 独立边界 | 私有表 | 输入、输出与重点独立预期 | 复用索引 |
| --- | --- | --- | --- |
| `domains/operations` | `op_fact` | 收入费用、预收预付、往来及追收等明确事实→完整计划；履约、退款、代付不得重复收入成本，改低已付款义务须明确追收关系。 | 旧 `kernel/domains/transactions.py`、`adjustments.py`、`managed_reserve.py`。 |
| `domains/people` | `people_fact` | 用工、工资奖金、劳务、缴费依据→成本及各项公开义务；有据个税、跨月付款、资本化、不补造日期。 | `payroll/contributions.py`、旧 `kernel/domains/payroll.py`、`labor_assets.py`。 |
| `domains/assets` | `asset_fact` | 购置/项目/启用/消耗/处置→卡片、成本和正式计划；整批来源、不重复资本化及寿命边界。 | `fixed_assets.py`、`intangible_assets.py`、旧 `kernel/domains/assets.py`。 |
| `domains/taxes` | `tax_fact` | 明确政策/税点/实际税额及已支持计算→税费计划和表单取数；期间、生效、累计及舍入按业务规则。 | 旧 `kernel/domains/taxes.py`、`income_tax.py`、`tax_point_basis.py`。 |
| `domains/financing` | `financing_fact/investment_fact` | 股东资金、贷款及支持的投资生命周期→本息/成本/收益/风险承担结果；合作项目作为投资理财的一种方式，按实际合作合同记出资、收益、损失和退出，不借用货币基金或固定息贷款公式。 | `borrowings.py`、旧 `kernel/domains/investments.py`、`assets.py`相关贷款函数。 |
| `money` | `money_fact/money_occurrence/bank_statement_row/platform_activity_row/money_reconciliation/money_reconciliation_member` | 真实收付/划转/原行→资金发生和清偿计划；occurrence只登记稳定实付身份，正式金额及分类由ledger按结果头采用。流水核对不重复记付款，原1000元实付不因应付改800元被抹成800元。 | 旧 `kernel/domains/banking.py`、`cash.py`、`platforms.py`及支付/抵销函数。 |

表中简写路径以 `src/ai_accounting/` 为根，具体入口见旧能力对照。旧代码只能作为复用候选，逐 kind 的规则和先写好的预期优先。

F 合同完成后，各家庭可用公开账务/身份读取替身先实现纯计算；不必等待其他家庭写完。真实集成依照业务依赖进行：经营/人员/资产/融资→实际收付→期间消耗→税费→共享报表。每家提交有效、缺项、冲突、边界和闭期影响算例及完整结果，不用大批“只检查成功状态”的用例代替金额核对。

### A：AI 办理协调与标准档案

**owner：业务接入负责人。** 交付 `application/identity/agent/records/` 的各自边界与 `tests/modular/application/identity/agent/records/`。它是一次完整办理和资料接续任务，内部仍依架构拆模块；不把领域公式放进应用协调器。identity为独立身份模块，公开plan_change/apply_current/get_batch/find，records、reading和领域只读使用，不反向依赖application。

- 输入为 ApplyRequest、对象/来源/当前待办/说明及各 owner 的计划端口；输出为 application.public、MCP/CLI 一次办理结果、当前文档集合及可接续状态。notes可独立办理，沿当前说明序号比较，不为修改说明伪造业务凭证。agent 引导老板并解释实际结果，不让 AI 编排采用、归档和缓存修复。
- identity私有表为 `identity_object`；application私有表为 `business_head/current_dependency`；records私有表为 `source/source_use/current_issue/current_issue_scope/company_note/document_shard_head/document_set`。事实表、对象、正式结果及读取目标通过各 owner 的端口加入共同事务，应用层不能代写。来源边统一用source_use承接业务、对象、待办及说明，不复制多份可独立修改的引用。应用层负责备份快照plan，交给files接续，不拥有文件任务表。
- 允许改本包、AI 操作说明、模板和所属测试；不改老板看板布局，不设置审批、开放期请求流水或全部旧答复。文件任务只保存当前/有效交付及有界失败原因。
- 复用旧 `kernel/materials.py` 的来源位置核对、`replay_document.py` 的解析经验及现有中文说明；退出旧固定步骤、批准、全部版本和依赖旧运行身份的重放。最终文档从实际保存的事实生成，Markdown 与 JSON 不分别编辑。
- F 就绪后用明确计划替身验证原子协调和防重；records 可用公开事实导出替身独立验证跨月分片、清单切换、租约与失败接续。独立样例覆盖已有 create A+新 B、丢回复重试、scope 冲突、撤去墓碑、来源固定内容、晚到任务不覆盖新目标。
- F+L+至少一个真实领域必须尽早接通；C01–C10 在真实模块下执行。备份再与 F 联合绑定数据库 head、已发布文档 set_id 和来源租约；新空库重建再接全部支持领域，不从旧库补隐藏状态。

### R：读取模型与看板适配

**owner：读取交付负责人。** 交付 `reading/dashboard/`、必要前端 API/生成类型接续、`tests/modular/reading/dashboard/`及对应前端测试。reading 维护派生模型和计划，dashboard 只适配既有 HTTP；二者不重算账务。

- 输入为ledger、领域、money、records、identity公开结果、storage公司元数据及统一ReadContext；输出为reading.public、冻结的五页响应、报表三接口及独立只读来源查阅。请求、字段、版本、分页、取消、差异与预算全部依 [看板规范](06-dashboard-adapter.md)。
- 私有表仅 `read_scope_head/read_component/read_item`。文件任务仓储归F包的独立files模块；reading拥有报表plan与口径，通过共同FileTaskPort接续，不直接写 `file_task` 或反向导入application。
- 允许改本包和所属测试；前端接续遵守根目录 FRONTEND.md。主要显示内容、UI 样式和既有交互保持，密码核对的一句最小文案按看板规范说明。合作投资只接通既有财务汇总、真实资金和完整凭证，不新增专页、板块、接口或项目管理展示；通用进展无适用明细时明确说明，不阻断记账。顾问从AI提供的/review/materials深链进入第11节独立来源页，依严格ReviewMaterialsQuery/Response和SourceContentQuery打开已登记原件/老板补充。不修改主导航、旧业务generic逻辑或原五页默认请求。
- 复用现有 Vue 组件、API 消费方式、取消/缓存与递增追加行为；旧 `dashboard_sort.py`、分区和组明细测试只作为行为索引。新后台不得调用旧总服务、旧事实表或跨模块私表。
- F 及公开读取合同就绪后可用确定的语义结果替身独立开发；真实集成必须接同一公司事务中的真实领域/账务结果，并验证跨月余额、冻结读取、旧分页409、当前模型及导出所见一致。报表partial的有据金额正常显示、未知null且禁完整导出；不得无限preparing。来源页验收引用范围/分页、冻结旧依据、开放旧代409、跨公司404、原件校验失败不交付及移动读取租约，依[来源样例](examples/review-materials-cases.json)。
- C09–C14 和看板规范全部功能、工作量与性能矩阵通过，才算交付。旧500ms成绩、纯 HTTP 耗时或只读第一页不计作新验收。

### H：装配与完整交付

**owner：集成负责人。** 交付 `host/`、运行入口/依赖注入、后台接续、合成环境工厂、`tests/modular/integration/architecture/acceptance/`及最终运行说明。只装配公开工厂，不以 host 私有工具替代模块合同。

- 输入为前五包的公开实现及全部共同样例；输出为一套实际可运行的单体程序、MCP/CLI/HTTP、既有会话体验、文件任务接续和完整验证报告。
- 无独立业务私表。启动和关闭负责资源生命周期；后台执行使用各 owner 的公开能力，不能靠直接改 pending 状态或扫描所有业务 JSON 修复。
- 允许修改本包、项目运行配置及集成测试。禁止自动切换现有服务、搬移真实旧库、清理真实资料或提交 Git；这些不因设计交付完成获得授权。
- 复用现有启动、同源会话与前端构建流程的有效经验，路由切到新公开模块；不引用旧内核作为运行代理。
- F 的工厂和合同就绪即可搭建合成装配；后续每包真实实现到达就替换对应替身。对每个集成节点输出“真实模块/仍是替身”清单，不把混合装配称为完整集成。
- 最终运行全部核心样例、逐 kind 与合作投资样例、架构边界、前后端构建、包/恢复/重建和性能矩阵。未通过项要指出真实影响，不改预期遮掩缺失能力。

## 3. 依赖与真实集成节点

| 节点 | 可以并行做什么 | 必须接通的真实实现和退出条件 |
| --- | --- | --- |
| G0：设计合同一致 | 全包审读自身输入输出及共同样例；补正唯一真源。 | Schema/DDL/规则/页面/样例无互相冲突和悬空引用，所有任务包范围一次明确；这是一项设计完整性检查，没有业务审批。 |
| G1：基础可用 | F 实现；其余包先准备独立测试和公开替身。 | 新合成目录/公司库能创建；严格类型、事务、ReadContext及表归属可运行。 |
| G2：第一条真实办理链 | L、六个 D 边界、A、R 按公开合同并行实现；H持续装配。 | 真实经营费用→真实采用→标准文档→真实查询；保号改错和丢回复防重通过。不能等所有模块完工才第一次集成。 |
| G3：跨模块与闭期 | 继续各家庭规则和页面；A/F 联调备份，R联调真实模型。 | 工资+真实付款+追收、资产成本消耗、税费共享口径、冻结更正、scope竞争和文件中断均由真实模块通过。 |
| G4：完整运行 | 合成大规模性能、故障恢复、边界替换可并行验证，计时不与建库/插桩抢资源。 | 无业务替身；五页、MCP/CLI、文档、备份/恢复/简化重建及核心/逐 kind 样例全部通过。 |

每个包的独立测试可以使用严格替身，但替身不得实现一份消费方自己的工资、税额或余额公式。预期先从本设计给出的确定数字取得；真实集成对同一预期核对，不能让两端各自生成结果再彼此证明正确。

## 4. 核心样例如何执行和核对

[core-acceptance.json](examples/core-acceptance.json) 是测试数据及 oracle，不是运行时接口。每个 case 在独立空临时公司库/资料根执行，不继承上一 case、真实目录或真实业务。固定 ID、月份、金额及来源内容属于合成资料；请求中的金额全部为整数分字符串。通过测试时钟固定Asia/Shanghai当前时间为JSON中的clock.now，不把它做成业务API参数，也不据此补造事实的实际日期。

JSON 中 `requests` 是完整 ApplyRequest 模板；`objects/sources` 是可复用夹具，事实已写在完整请求中。测试适配层按 `given` 请求名依次办理并等文档ready，按 steps 顺序执行；存在variants时，每个分支重新执行given。`capture` 只记录运行时生成的不透明头、游标、任务和文件集合身份，后一步用`{"$capture":"步骤.字段"}`原样引用；不能用它推算期望金额。`$sandbox`只替换为本case独有临时根，不能落入真实资料根。`expect` 的金额和数量是事先固定值，不由被测实现计算。

oracle 使用以下固定含义：

- `business` 核对 accounting_get 的当前有效事实/seq/state；`voucher` 核对 ledger 公开凭证及分录，金额汇总以完整匹配行计算，不能使用页面已读局部页。
- `oracle_scope`限定该case的金额、凭证角色及条数统计；未限定时按明确请求范围读取。单笔费用更正的凭证数不包含公司结转或其他业务凭证，不能因漏写必需结转而凑数量。空期初也是有效事实，整公司业务身份清单须包含它。
- `obligation` 核对公开业务槽的原额、真实清偿、剩余和追收；真实收付另向 money 查询。来源、文档与读取不得用账务查询的同一段内部 SQL 伪装独立核验。
- `same_as` 比较上一步捕获的公开结果、相关头/seq、正式金额及当前文档集合；失败不允许一部分变了。`unchanged` 明确列出应原子保持的范围。
- `absent_current_payloads` 检查当前事实、未冻结旧采用、开放期旧凭证/义务/关系、当前文档及可下载交付中不再存在被替代正文；不要求擦除 Git、合成测试输入或 SQLite 空闲页，不删除仍有真实采用依据的原件或冻结结果。开放头原result_id保持、generation前进，旧generation正文不保存；旧voucher_version_id令牌失效，不能仅比较result_id误判仍可读。
- `fail_at` 是仅测试可注入的故障点，不能成为 AI/API 的生产参数。必须使用真实文件及事务，在指定边界中断并重启合成实例；仅抛异常后继续同一函数不覆盖进程中断。
- `logical_equal` 比较明示的业务和财务语义，不比较重建后新生成的运行身份、result_id、旧凭证号、旧月度纠错过程或 SQLite 文件字节。

幂等、部分已存在、整单冲突和回复丢失在合成环境中可执行完整原请求；不建立专为测试的“绕过校验保存”入口。白盒检查限 owner 自己的私表和结构保护；跨模块金额及业务验收必须走公开入口。

JSON中的invoke由H包测试适配层按下表执行，不增加生产API，也不允许在适配层补一套核算：

| 样例动作 | 实际调用与观察方式 |
| --- | --- |
| `accounting_apply/documents/period/backup` | 调用03规定的同名应用入口；backup仅传company_id/expected_database_id，输出位置在本case预先配置为$sandbox/configured-backups。不能向生产入口加target字段。 |
| `accounting_restore/rebuild` | 在独立离线目标host同步调用IsolatedTargetRequest，以显式action/mode/source_path/new_target_directory办理。source_path来自前面已捕获的已验证ZIP或完整标准清单manifest_path；测试核对原字节摘要与离线返回source_digest，另核对清单set_id，不用集合身份冒充文件摘要，不从源任务或源DB反查路径。目标只允许本case临时空新目录，源实例可已关闭；不在同一目录注册两个相同身份。 |
| `observe_business/observe_frozen_voucher/observe_atomic_scope` | 经accounting_get及ledger/money公开查询取完整当前或精确冻结结果；捕获result_id、generation、凭证编号、实际资金身份。source_head及旧定位令牌从同范围dashboard响应取得。atomic_scope规范比较当前事实/seq、采用、凭证、义务、真实资金、文档集合及相关目标，排除查询时间与诊断ID。 |
| `observe_issue_scope/read_published_documents` | 前者调用records.current_issues；后者从records当前清单读取全部引用片并校验摘要，列出每个business_id所属片及出现次数，不从DB伪造文件已经发布。 |
| `dashboard_brief_ready/brief_vouchers/exact_voucher/all_vouchers_same_new_head` | 分别调用06的brief首读交付、section=vouchers、精确voucher_version_id定位及同一新snapshot_version下的完整分页；ready首读按返回间隔等待，失败不得当ready。全部分页用首次20、后续500，核对尾页和去重数量。 |
| `prepare_all_selected_models/capture_full_public_business_and_ui` | 通过真实读取入口使指定范围就绪，再捕获完整公开投影、组件/范围头及请求工作量。页面为实际Vue合成环境；模板/样式摘要基于本次既有页面文件，排除明确约定的一句密码文字修正。不能用静态截图或固定响应替代实际查询和DOM。 |
| `accounting_backup_wait_verified/wait_current_file_task` | 创建后只查询该task_id，经files真实worker执行到succeeded/failed；成功后核验ZIP、回执摘要及字节数，capture已验证包的独立路径verified_package_path及package_sha256。pending/running不能提前交付。 |
| `start_backup_and_pause_after_snapshot_lease/resume_backup` | 调用真实备份协调，在一致DB快照及文档/来源租约已取得处暂停，然后继续真实复制和校验。比较包内DB、文档及来源；并发提交发生在协调锁已释放之后。 |
| `restart_synthetic_host` | 结束合成子进程后用同一case的目录、公司库与资料根重启。仅修改build_label时，核算语义与结构合同不变；不能删除数据、任务或缓存来“恢复”。 |
| `stop_synthetic_source_host` | 所有源归档/备份已ready后结束源合成实例。离线恢复/重建只读已经捕获的包或清单，不连接源目录/业务库；原件按清单受控根读取。manifest_sha256是独立读取manifest_path原字节所得；不能用set_id代替文件摘要。 |
| `install_contract_preserving_ledger_internal_implementation/rerun_core_and_five_page_acceptance` | 注入或部署另一种真实ledger私有实现，公开合同与核算语义不变；用它重新运行C01–C13和06完整矩阵，不递归运行C14自身。替换前后都使用真实事务、存储与计算。 |

`expect`里的列表按明确业务排序比较；身份集合按集合比较且不能有重复；金额按规范分字符串比较，不能先转浮点。文档/包摘要用读取的真实字节独立计算；这些摘要可以是运行时值，金额oracle不可以。

## 5. 架构与模块替换验收

集成负责人交付可自动执行的边界检查：跨模块仅导入 public/共同合同；领域不得导入 host/reading/dashboard；消费方不得引用私有表；模块结构合同的每张表恰有一个 owner；公开合同不依赖 SQLite 连接或 Vue 类型。ReadContext 的事务句柄仅为进程内基础能力，不出现在 JSON 线协议。

在完整真实合成实例上，用保持合同的另一种账务内部查询/存储实现替换 ledger 的私有实现，或完成一次仅影响内部实现的真实重构；重跑核心金额、旧前端 Schema/交互及性能矩阵。前端主模板和其余领域无需跟着改内部路径或表名，不发生全公司缓存失效，结果与预算仍满足，才证明模块化有效。只替换一个永远返回固定样例的 mock 不计。

同类越界发现后检查全部相同入口，不能只修首个导入。确认外部合同确需变化时，先修唯一 Schema、共同预期及受影响说明，再同批接续消费者；不保留两套长期接口或在适配器暗中吞字段。

## 6. 环境、命令和交付证据

开发使用仓库 `.tmp-kernel-venv` 的 Python，遵守 pyproject.toml 的 Python 3.12 范围和项目依赖；SQLite 必须实测支持 DDL 的 STRICT、JSON及外键要求。前端按现有 package-lock.json 安装依赖。性能报告记录实际 Windows/CPU/内存/存储、Python/SQLite/Node/浏览器及构建来源；冷启动单列。

实现按以下测试目录组织，并提供可复现命令；设计检查不能代替这些产品验证，实际支持范围与运行方式以本目录入口说明为准：

```powershell
& '.tmp-kernel-venv/Scripts/python.exe' -m pytest tests/modular/contracts tests/modular/storage tests/modular/files
& '.tmp-kernel-venv/Scripts/python.exe' -m pytest tests/modular/ledger tests/modular/domains tests/modular/money
& '.tmp-kernel-venv/Scripts/python.exe' -m pytest tests/modular/application tests/modular/identity tests/modular/agent tests/modular/records
& '.tmp-kernel-venv/Scripts/python.exe' -m pytest tests/modular/reading tests/modular/dashboard
& '.tmp-kernel-venv/Scripts/python.exe' -m pytest tests/modular/architecture tests/modular/integration tests/modular/acceptance
npm --prefix frontend test
npm --prefix frontend run build
```

测试运行器由 H 包维护，包含 `tests/modular/acceptance/test_core_examples.py`，读取本目录 JSON并报告每个 case/step 的输入、预期和实际差异。测试输出只含合成内容。各 owner 同时交付事实/结果 Schema 校验、所属模块测试、一次完整自读后的设计复核及删除无用层次后的再验证；不能以“写完且测试绿”直接结束。

最终报告逐项列出真实集成范围、执行命令和环境、未通过项目、读取及归档预算、备份内 DB/document set 一致性、恢复与简化重建的差别、仍有来源的具体展示缺口。只有完整真实实现达到共同样例和明确预算才称为产品交付完成；设计文本、历史测试数和旧性能记录不作为通过证据。
