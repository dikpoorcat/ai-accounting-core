# 全内核必要事实审查

本轮按“是否改变科目、金额、期间、税务、现金流、折摊计息或实际权利义务”审查。
管理资料可以后补，不能被服务、数据库或运行契约重新升级为记账门禁。
以下是逐规则决策清单；测试名是对应验证位置，运行结果见文末交付记录。

## 协议、来源和提交

|编号|原限制或风险|决定与保留理由|实现及验证|
|---|---|---|---|
|K01|普通往来必须有对象|取消；以原事件键、组件键、往来项键挂账，不造占位对象|component_schemas / component_service / OpenItem；test_essential_accounting|
|K02|客户／供应商档案标签决定普通业务可否记账|取消；分类来自业务组件与账户规则|普通收入、费用、预收预付编译；test_business_components|
|K03|核销重复填写对象与科目|取消；由明确来源继承|obligation / obligation_account / ledger；test_business_components|
|K04|来源只能用旧数据库UUID或同笔键|扩展；跨事件原幂等键＋组件键＋开放项键统一解析|prepare_request；匿名部分付款及重试测试|
|K05|修改后原业务引用失效|保留原事件幂等键与稳定组件身份|event_amendments；test_component_lifecycle、test_purchase_components|
|K06|同类组件、多个对象、多个资金账户组合受限|取消事件组合名单；各来源及资金分配独立校验|test_business_components、test_essential_payroll|
|K07|重复索要已由资金项明确的收付款日期|复用资金日期；仍校验与明确核算日期的关系|prepare_request / compile_funds；匿名代收测试|
|K08|经营说明、用途等进入计算确认|组件管理字段进入metadata，核算事实与哈希排除metadata|accounting_request及各领域哈希；必要事实测试|
|K09|缺少可选资料自动触发追问|取消；needs_information只列缺失的核算事实|组件need和各领域missing_information；最低事实用例|
|K10|公司隔离、证据、整数分|保留；防止串账、无依据和非确定金额|schema / evidence检查 / PG归属约束|
|K11|借贷平衡、资金守恒、幂等|保留；整笔计划一次原子提交|ledger / compile_funds；test_component_ledger、test_component_guards_postgres|
|K12|未来、撤销、错误方向、来源余额不足|保留；对应真实来源有效性|来源解析后仍进入原校验；test_purchase_components、test_pass_through_postgres|
|K13|循环、重复核销和并发余额争抢|保留；明确依赖和整笔最终状态校验|组件依赖图、来源锁和余额检查；PG组件测试|
|K14|实际债务转移可被改名替代|拒绝；管理资料不改权利义务|debt_transfer/person_advance与metadata分离|
|K15|可自由提交借贷行或执行脚本|继续禁止；同类科目只继承受控业务分类|finance_configure_account；test_business_components|
|K16|同一来源不同引用方式、管理文本变化导致记账幂等冲突|统一解析日期和来源后仅摘要核算事实；管理变更走后补入口|test_accounting_idempotency_reuses_dates_and_equivalent_sources|
|K17|多个资金日期自动取最早日期|取消推断；按实际日期拆分组件后组合，保留独立履约日|test_component_cannot_infer_date_from_different_fund_dates|
|K18|修改、删除、冲正必须文字原因|说明可省；原记录、操作者、命令、版本与前后状态继续审计|event_amendment_schemas / ReverseEventRequest；组件生命周期与PG回归|

## 管理资料后补

|编号|原限制或风险|决定与保留理由|实现及验证|
|---|---|---|---|
|M01|管理资料只能随会计事实一起修改|新增独立finance_update_business_metadata|business_metadata；test_essential_accounting、test_essential_postgres|
|M02|关账后不能补对象与说明|允许；不触碰凭证、会计事实或关账快照|独立版本表，无期间写入检查；PG关账后更新测试|
|M03|并发更新互相覆盖|预期版本＋事件锁＋唯一版本约束|METADATA_VERSION_CONFLICT及并发测试|
|M04|重试重复增加版本|公司内幂等键及请求哈希|同键同请求复用；不同请求拒绝|
|M05|管理历史被原地覆盖或删除|禁止；ORM和PG均追加写历史|before_flush / metadata triggers|
|M06|外部公司对象引用进入当前公司|拒绝；只校验类型、公司归属及执行权限|validate_metadata及PG插入保护|
|M07|修改凭证重建组件导致资料丢失|按event_id＋稳定组件键关联，不FK到可重建组件行|整笔修改／删除保留管理历史；事件查询单列完整历史|
|M08|管理历史被误认为下游会计依赖|从账务依赖扫描排除，仍保留历史|event_amendments；预付退款修改删除测试|
|M09|当前管理名称被当成历史会计事实|查询分开facts和management；金额报表不依赖当前资料|MCP、dashboard_brief、BriefActivityWorkbench|

## 普通业务、代收与项目成本

|编号|原限制或风险|决定与保留理由|实现及验证|
|---|---|---|---|
|P01|代收必须外部受益人、当前债权人和用途|取消；金额、收款日期、稳定键及证据足以确定资金和代收负债|compile_pass_through；匿名收款及部分付款测试|
|P02|所有代收必须提供个人垫付链|取消；只有明确个人垫付债务才需垫付人、日期和依据|pass_through与person_advance/debt_transfer分离|
|P03|代付必须重填外部受益人|取消；引用原代收来源按金额核销|普通payable_settlement；余额上限回归|
|P04|普通费用、其他收入、押金等必须对象或说明|对象／说明改可选；个人垫付人的身份仍影响真实债务|普通编译器；组件混合及退款回归|
|P05|预付款需合同号和项目号|取消；purchase_purpose决定现金流，编号只用于管理|purchase_components；test_purchase_components|
|P06|冲抵必须同供应商标签、同项目标签|取消重复标签比较；保留来源科目、方向、同公司和可用金额|预付冲抵和不同标签退款成功用例|
|P07|阶段确认必须验收单号、付款义务编号、到期日、说明|移到metadata；阶段事实、权利控制、成本性质仍必需|project_cost；缺rights_controlled/project_nature/cost_element失败|
|P08|付款时直接核销未来确认的资产应付|继续拒绝；先形成预付或真实阶段债务|9月预付11月冲抵；阶段债务支付后资产归集用例|
|P09|研发资本化只靠说明或标签|不能；条件事实及满足日期仍必需|development_conditions及成本分类验证|
|P10|归集成本必须重复填项目和完整分解|取消；从明确来源按金额生成总成本和分解，重复提供时精确核对|compile_intangible_asset_acquisition及多成本来源用例|
|P11|费用结转必须原因文本|原因可选；来源与费用分类必需|project_cost_expense；余额恢复、超额及错误来源回归|

## 资产、借款、工资与税务

|编号|原限制或风险|决定与保留理由|实现及验证|
|D01|资产编号、名称、供应商名称阻止购置确认|改可选；系统提供稳定内部身份|asset服务、模型、前向迁移；test_essential_domains|
|D02|资产分解必须每项填数，包括无关零额|明确cost_fen，分解选填；给出分解时校验合计|fixed_assets/intangible_assets计算器及领域测试|
|D03|普通资产应付到期日强制|取消；不影响当前负债确认|资产schema/service/DB settlement约束|
|D04|资产投用状态、投用日、寿命、残值、费用归属|保留；直接决定折旧摊销与确认时点|领域必要事实及生命周期测试|
|D05|资产管理名称和系统码进入预览来源证明|移除管理等值校验；保留资产身份、金额、投用和计算事实|本地启用与首月折旧PG回放|
|D06|无形资产权利类型及可辨认性|保留必要类型化事实；一般权利说明、命名、寿命解释不强制|intangible_asset_schemas及最小事实测试|
|D07|借款名称、编号、用途必填|取消；内部稳定身份独立于管理标签|borrowing_schemas/service；test_essential_domains|
|D08|放款前必须完整未来利息付款日历|取消；支付安排与计息区间分离|borrowing_service；无日历放款及连续计息测试|
|D09|提前还款、展期、罚息、融资费用条款尚未发生也阻断|未发生条款不阻断当前支持的核算；实际进入未实现机制仍明确拒绝|借款条款和实际付款路径验证|
|D10|本金、利率、计息基础、期限与连续区间|保留；决定利息及负债类别|calculate_simple_interest / borrowing lifecycle|
|D11|月末只按付款日历判断借款应计|改为连续覆盖放款日至min(月末,到期日)|accounting_period_service及PG关账函数|
|W01|劳务人员管理编号必填|取消；保留人员识别、所得性质和税务身份|labor_remuneration schemas/service；test_essential_payroll|
|W02|曾在员工档案出现即永久排斥劳务|取消永久排斥；当期所得性质与实际雇佣事实仍校验|劳务身份回归|
|W03|外部申报状态、申报编号阻断劳务计提|办理状态和编号不阻断；确定税额的实际申报结果仍保留|labor schema/service/DB；无管理信息计提测试|
|W04|法定缴款必须机构代码、名称和普通到期日|取消；险种、金额、所属期和来源仍必要；不创建虚构机构|工资应付OpenItem、ledger与PG约束|
|W05|一项工资结算只能一个批次|按每个来源逐项计算，可组合多个批次和人员|salary_components；test_essential_payroll|
|W06|补缴必须核定单号、解释、唯一旧工资批次|管理信息可省；明确补缴金额、员工、所属期、险种和个人承担方式必需|payroll_supplement_components及必要事实测试|
|W07|工资确认文本和管理备注进入哈希|排除；批次、人员、金额、税务计算哈希和证据保留|service/labor/tax组件哈希与确认回归|
|W08|补缴按相同金额或可选核定编号判定重复|取消；同员工同期间同金额可为独立业务，依赖稳定幂等键保护重试|test_repeated_supplements_local_payment_and_reversal使用完全相同金额|
|W09|实际工资与申报工资不同必须文字解释|说明可选且不进入试算、事实及哈希；明确两类金额、申报状态和差异证据仍必需|PayrollEmployeeItem / payroll计算、service、ck_payroll_line_gross_salary；test_essential_payroll|
|W10|本地工资税款来源默认只有单个批次，唯一索引及终态校验按单来源计数|改为完整来源批次集合；索引包含批次、逐关系校验覆盖，仍拒绝同批次重复链接|compile_payable_settlement、PayrollEventLink、0003；test_postgres_local_salary_tax_payment_links_every_source_payroll_batch|
|T01|税种、所属期、税源、计税规则、来源分配|保留；防止税额和现金流错误|tax_confirmation_components / enterprise_income_tax|
|T02|企业所得税申报编号和确认说明强制|改可选并从计算哈希排除；明确申报税额和归属仍必要|EIT direct与组件路径回归|
|T03|申报导出专用个人资料扩散到普通记账|只在导出功能验证；工资核算保留确有计算意义的身份和扣缴事实|payroll tax import与普通工资测试分开|
|T04|任意税率、未版本化规则、浮点金额|继续禁止；税率和税额用Decimal，记录生效期和官方来源|原税务计算与来源完整性回归|
|T05|报表分类、期初余额与季度所得税确认必须文字说明|改可选并排除请求摘要；保留分类、期初及税额事实、证据、版本和审计|financial_statement_schemas/service/models、0003；报表与PG回归|

## 银行、关账、查询和交付

|编号|原限制或风险|决定与保留理由|实现及验证|
|C01|银行入账先完成全套对账范围确认|取消；有效本公司资金账户即可记账|_validate_posting_bank_account、各领域入口|
|C02|已提供流水的账户、日期、币种、金额、方向及唯一匹配|保留；防止同笔资金重复使用|统一资金提交；银行匹配PG回归|
|C03|经营解读及其哈希为关账必填|改可选管理功能；主动提交时验证上下文|accounting_period_schemas/service；关账测试|
|C04|外部申报办理进度及人员打卡阻止关账|管理提示与账务阻断分离|owner_workflow_close_gates不计入blockers；PG对应规则移除|
|C05|逐项重复复核声明强制|取消；本次快照的负责人授权承担完整性确认|ConfirmAccountingPeriodCloseRequest及关账PG测试|
|C06|任意试算草稿被当成未计提实际业务|取消；草稿仅提示，已知应计事项仍检查|工资／劳务草稿与实际应计检查|
|C07|关账授权、备份、银行对账、应计和账表一致性|保留；直接保护闭期完整性|原关账、备份及直接SQL保护回归|
|C08|生成自然月份还需说明和重复证据|只需公司、月份和幂等键；生成日历本身不生成会计分录|GenerateAccountingPeriodRequest|
|C09|对象内连接导致匿名往来丢出报表|改外连接，稳定业务引用显示；金额分类取业务来源|dashboard_brief / replay_cli / 报表回归|
|C10|回放丢失管理资料或把它混回会计事实|回放v3分别重建会计事实与追加式管理版本|_metadata_operations及PG空库回放|
|C11|改写两个正式空库基线|禁止；新增0003_essential_accounting前向迁移|test_baseline_migration：空库、升级、command.check及保护|
|C12|以资料包验证代替真实重录完成|分别报告资料验证、隔离回放、真实业务重录|下述交付记录|
|C13|凭证摘要、分录说明及更正原因影响关账计算哈希|从计算投影排除；保留当前会计事实哈希、金额及来源，原文字仍存展示快照|test_close_hash_uses_accounting_facts_and_ignores_management_and_command_audit|

## 本次资料交付

2026-09-09 通过 PostgreSQL 只读事务重新核对两家公司。当前生产库仍在
`0006_pass_through`，未迁移或写入。业务事件、凭证、分录、往来、核销、银行流水和证据表
与9月8日只读快照一致；两家公司分别有330、90条原始事件。全部90份证据通过哈希和大小核验。

新私有包为 `outputs/essential-accounting/composition-replay-v3-essential/`，来源为9月9日
已修订的v5资料包，保留414条有效业务资料及原证据。`reentry-verification.json` 记录完整清单
和文件哈希验证；`source-comparison.json` 记录本轮最新库核对。逐公司操作顺序、来源引用、
整数金额和核对点在 `essential-reentry-checklist.md`，原始包原地保留。

资料缺口：UI项目9月是否存在已确认的独立阶段成果仍无依据。本包继续采用已确认的
9月预付款、11月确认资产并冲抵路径；“阶段确认债务、付款、最终归集资产”的能力用隔离
测试事实验证，不将该假设写进真实重录材料。

资料已整理并通过离线验证。隔离测试库的执行结果单独记录；真实业务尚未重录，未启动密码窗口。

## 验证记录

本次仅在隔离 SQLite／PostgreSQL 17 数据库验证，未对试用公司的旧数据库执行迁移。
下表按实际命令分组；各组可能重复覆盖同一测试，不累加为总数。

|范围|结果|记录位置|
|---|---|---|
|全套非 PostgreSQL 测试（`pytest -m 'not postgres'`）|793 passed，1 skipped；跳过项为非 Windows 专用测试|outputs/essential-final-nonpg.log|
|管理资料、看板投影、MCP 最新补充回归|29 passed|outputs/essential-final-management.log|
|组合验收、管理流程提示、MCP 更正契约及关账服务|47 passed|outputs/essential-final-acceptance.log|
|PostgreSQL 前向迁移、空库初始化、Alembic 模型一致性及管理资料保护|11 passed|test_baseline_migration、test_essential_postgres|
|PostgreSQL 组件保护、修改删除、代收、采购来源|22 passed|component_guards、component_lifecycle、pass_through、purchase_components 的 PostgreSQL 测试|
|新协议回放、报表、代收与采购终态核对|40 passed|outputs/essential-final-replay-pg.log|
|PostgreSQL 本地资产完整生命周期|3 passed|test_local_asset_lifecycle_postgres|
|PostgreSQL 无管理说明的财务报表确认|1 passed|test_financial_statements_postgres|
|PostgreSQL 两个工资批次结算并在同笔缴纳聚合个税|1 passed；最后索引修正后基线及模型一致性再验证4 passed|test_repeated_payroll_components 中的 PostgreSQL 用例；test_baseline_migration|
|前端类型检查与生产构建|通过|outputs/essential-frontend-final.log|
|Python 编译、Ruff、Git 差异格式检查|通过|outputs/essential-changed-lint.log|
|两家公司新协议资料包离线核验|2 公司、414 条有效业务资料、90 份证据、122 个文件通过|outputs/essential-package-final.log|

隔离库回放验证覆盖新协议记录、来源重映射、预览确认、管理资料版本回放、幂等重试和终态
科目／往来／工资税务／资产核对。真实公司资料目前仅完成只读核对、整理与离线验证，
不把这些结果记为真实公司已重录或已完成真实空库回放。
