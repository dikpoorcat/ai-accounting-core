# 全内核必要事实审查

本轮按“是否改变科目、金额、期间、税务、现金流、折摊计息或实际权利义务”审查。
管理资料可以后补，不能被服务、数据库或运行契约重新升级为记账门禁。
以下是逐规则决策清单；测试名是对应验证位置，运行结果见文末交付记录。

后续“把日期错误误读为管理资料必填”的复审与预防机制见
[核算事实与追问契约](fact-requirements.md)：统一字段语义、支持精度、结构化诊断和先复用来源再追问的顺序。

## 日期精度与必要事实补修（0004）

以下补修取代上轮仍要求个人实际垫付日、单组件单资金日期及外部申报日期的结论。
日期只有影响确认、税务、计算或来源先后关系时才是核算事实；管理信息不能再次进入派生哈希。

|编号|发现的限制／风险|最终处理与理由|贯通位置及验证|
|---|---|---|---|
|F01|只确定月份仍必须补某日|新增recognition_period；月末是确认截止，不是实际发生日|组件协议、提交器、查询、前端、0004 SQL；test_fact_precision、test_fact_precision_postgres|
|F02|个人费用和债务转换强制付款日|取消；保留垫付人、债务性质、确认日期／月份、金额和证据|component_service、metadata、核算摘要及运行契约；六月确认七月报销测试|
|F03|个人垫付押金也要求付款日|取消；非现金同时形成押金债权和员工债务|组件、分录及往来；monthly_personal_deposit_and_debt_transfer|
|F04|月末确认被误用为月中已存在的债务|禁止；核销资金日期和非现金确认截止不能早于来源成立截止|统一资金验证、ledger、延期数据库保护；early cutoff测试及直接SQL绕过测试|
|F05|多资金日期一律要求拆组件|移除通用限制；逐资金记录真实日期和来源分配，不推断单个付款日|prepare_request、compile_funds、settlement_schedule；不同日期代收和多批次工资测试|
|F06|工资扣缴生成往来重复接收未使用的付款日|删除内部重复参数；扣缴金额来自明确工资来源，日期由资金计划记录|salary_components、service；双批次双日期工资及稳定来源幂等测试|
|F07|首次工资扣除、社保实际数登记必须有申报日|日期选填并排除请求摘要；税年、月份、处理性质、金额和证据保留|schemas、service、ORM、0004空值；payroll_registration_dates测试|
|F08|所得税结果确认日强制等于外部申报日|按结果确认日期或月份核算，保留所属期、前次结果和来源顺序；申报日期只作管理|CIT预览、组件、保存、查询、哈希、0004；CIT生命周期及幂等回归|
|F09|工资所得适用性被表示为申报状态|改为wage_tax_scope：wage_income／contributions_only；不推断外部完成状态|协议、计算、ORM、PG保护函数、看板与回放；PG正式工资和原工资回归|
|F10|劳务管理资料从派生行重新进入哈希|从计算输入及派生核算投影完整移除；历史存储不回写|labor_remuneration_service；管理状态、编号和分解变化核算输入不变测试|
|F11|劳务必须分别填写固定报酬及佣金，包括零|明确gross_remuneration_fen，分解一起选填；完整已知分解可唯一计算总额；未知分项保存NULL|schema、计算、查询、ORM、0004合计约束；总报酬用例和修改删除回归|
|F12|运行指令要求先完成外部申报才能关账|删除该硬门禁文字和误导性的required投影；保留独立流程提示|agent_contract、accounting_period_service；关账、MCP及看板回归|
|F13|公司初始化、资料变更、备份配置强制说明文本|说明可省；公司身份、实际政策、生效日期、授权及备份路径继续验证；公司预览哈希排除说明|company_schemas/service、组织资料约束、0004；多公司PG及schema回归|
|F14|借款完整结算必须当天记账|允许记账日在实际付款日之后；实际计息和合同判断仍使用付款日|domain_components、BorrowingPayment、0004日期约束；借款生命周期回归|
|F15|借款分期清偿、单项劳务分次取得会改变现有计算机制|继续明确拒绝未实现机制，不取最早／最晚日期代替事实；不限制不同已支持业务组件的组合|BORROWING_INSTALLMENT_SETTLEMENT_NOT_SUPPORTED、LABOR_INSTALLMENT_INCOME_ATTRIBUTION_NOT_SUPPORTED|
|F16|资产投用、资本化、借款计息、税务时点被误当管理日期|保留；分别决定折摊起点、可归集成本、利息金额或税务归属；从明确来源能取得的日期直接复用|domain_action_schemas、purchase_components、资产／借款／税务既有计算和必要事实回归|
|F17|修改重录包破坏已完成操作或重新推断真实事实|只修订新交付包未完成部分；准备输入和158项前缀哈希不变，原状态文件逐字节不变|replay binding检查、私有reentry-verification.json；原始确认第6项支持六月员工债务|

存量凭证、事件JSON、计算哈希和关账快照不回写；0004仅对工资行的规范化适用性列作语义重命名。
管理资料后补继续使用独立版本记录，不通过修改实际债权人或计税条件实现。

本轮私有新包：`outputs/fact-precision/composition-replay-v3-fact-precision/`。
原包、原证据及`outputs/.composition-replay-essential-live-20260909.state.json`均保留。
负责人原确认记录（SHA256 `30f09a72035b17bdbc4cca74225474cfbc13219238cbe751cfe9ed05191319a4`）
第6项明确六月成本及应付员工报销款，故两笔佣金可以按月确认；没有补造垫付日期。
资料核验不等于真实业务续录。现有真实回放仍停留于158项，本轮不启动密码窗口。

## 本轮验证记录（0004）

仅使用隔离 SQLite／PostgreSQL 17 测试库；没有迁移生产库或当前回放数据库。
下列分组存在重叠，不累加为总数。

|范围|最终验证结果|记录|
|---|---|---|
|全套非PostgreSQL回归|815通过、1跳过；唯一失败为MCP旧工作流版本号断言，修正后该项重测通过，覆盖816个通过用例|outputs/fact-precision-all-nonpg-final.log、outputs/fact-precision-mcp-failure.log|
|组件、工资多批次多日期、劳务修改删除、所得税幂等与回放回归|79通过|outputs/fact-precision-regressions.log|
|日期精度、必要事实、借款延后记账与迁移模型|25通过|outputs/fact-precision-final-unit.log|
|PostgreSQL月度来源直接写入保护、工资适用性、组件保护、更正生命周期、回放、管理资料及多公司|22通过|outputs/fact-precision-pg-final.log|
|PostgreSQL空库初始化、前向迁移和Alembic模型一致性|1通过|outputs/fact-precision-pg-baseline.log|
|月度确认业务导出并回放至独立空库，保留月份及稳定来源并核对终态|1通过|outputs/fact-precision-pg-monthly-replay.log|
|所得税月度结果省略申报日期、管理资料不影响预览哈希与确认重试|1通过；亦纳入全套非PostgreSQL回归|outputs/fact-precision-cit-month.log|
|前端类型检查与生产构建|通过|outputs/fact-precision-frontend.log|
|Ruff及Git差异格式|通过；保留原有回放修复的格式和内容|outputs/fact-precision-lint-final.log|
|两家公司新重录包离线核验|414条有效业务资料、90份证据、126个文件通过；准备输入与158项前缀哈希一致|outputs/fact-precision/reentry-verification.json|

资料整理结果：仅新包未完成操作采用新字段；两笔佣金依据原始确认按六月员工债务整理，
没有补造外部垫付日期。原恢复状态文件SHA256保持为
`6e0850b3e6273c55a7a82460e0393ee40f17d2e119ea6df4b50249cfae2ae2ae`。

隔离回放结果：合成业务的新协议导出、空库执行、来源重映射、管理资料和终态核对通过。
真实恢复状态只在内存副本上验证新包绑定，没有执行真实公司第159项及后续业务。
单来源借款分期清偿和单项劳务分次取得仍属于F15明确拒绝的未实现机制。

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
|K17|多个资金日期自动取最早日期|取消推断与通用拆分限制；逐资金记录实际日期，保留独立确认事实，详见F05–F06|test_component_cannot_infer_date_from_different_fund_dates及多日期工资回归|
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
|P02|所有代收必须提供个人垫付链|取消；只有明确个人垫付债务才需垫付人、债务确认日期或月份和依据；实际垫付日选填|pass_through与person_advance/debt_transfer分离|
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
|W09|实际工资与计税工资不同必须文字解释|说明可选且不进入试算、事实及哈希；明确两类金额、所得适用范围和差异证据仍必需|PayrollEmployeeItem / payroll计算、service、ck_payroll_line_gross_salary；test_essential_payroll|
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
|C03|经营结论被归为可选管理功能，关账成功却可无结论|2026-09-10 修复：结论及上下文哈希是 AI 必须完成的关账交付，不要求负责人补写；漏交返回 AI 可自行处理的缺项，保存结论与关账原子完成；其他管理资料仍可省略|accounting_period_schemas/service、agent_contract；缺项、上下文过期、幂等与看板读取回归|
|C04|外部申报办理进度及人员打卡阻止关账|管理提示与账务阻断分离|owner_workflow_close_gates不计入blockers；PG对应规则移除|
|C05|逐项重复复核声明强制|取消；本次快照的负责人授权承担完整性确认|ConfirmAccountingPeriodCloseRequest及关账PG测试|
|C06|任意试算草稿被当成未计提实际业务|取消；草稿仅提示，已知应计事项仍检查|工资／劳务草稿与实际应计检查|
|C07|关账授权、备份、银行对账、应计和账表一致性|保留；直接保护闭期完整性|原关账、备份及直接SQL保护回归|
|C08|生成自然月份还需说明和重复证据|只需公司、月份和幂等键；生成日历本身不生成会计分录|GenerateAccountingPeriodRequest|
|C09|对象内连接导致匿名往来丢出报表|改外连接，稳定业务引用显示；金额分类取业务来源|dashboard_brief / replay_cli / 报表回归|
|C10|回放丢失管理资料或把它混回会计事实|回放v3分别重建会计事实与追加式管理版本|_metadata_operations及PG空库回放|
|C11|基线与迁移策略|2026-09-09 按负责人要求归并为业务 v4 空库基线；目录库 v2 保持独立；旧库只读导出后回放|test_baseline_migration：空库、command.check及非空保护|
|C12|以资料包验证代替真实重录完成|分别报告资料验证、隔离回放、真实业务重录|下述交付记录|
|C13|凭证摘要、分录说明及更正原因影响关账计算哈希|从计算投影排除；保留当前会计事实哈希、金额及来源，原文字仍存展示快照|test_close_hash_uses_accounting_facts_and_ignores_management_and_command_audit|

## 上轮资料交付（0003 历史记录）

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

## 上轮验证记录（0003 历史记录）

以下为必要事实解耦阶段的历史验收记录，不代表当前回放状态。2026-09-09 整理项目时，
表中旧 `outputs/` 日志和旧资料包已移入回收站；当前本机资料及验证结果以 `outputs/README.md`
为索引。

该阶段仅在隔离 SQLite／PostgreSQL 17 数据库验证，未对试用公司的旧数据库执行迁移。
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
