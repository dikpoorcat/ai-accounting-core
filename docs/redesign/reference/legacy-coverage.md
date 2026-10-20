<!-- @format -->

# 现有能力与新设计对照

本文件供需求、规则、数据库和验收查漏。以目标业务家庭为准，旧事实类型不决定新入口、表或文档数量。实际注册器现有114类：106类当前输入、8类仅历史读取，其中74类有直接计算入口。入口为 `src/ai_accounting/kernel/service.py:49`；登记输入不等于正式完成。

新模型采用按业务家庭划分的少量生命周期根记录，严格校验 JSON facts；正式结果、凭证、债权债务及关系另有明确关系记录。旧106种输入不是106张新表。本表说明承接方向，最终字段与接口以新设计合同为准。

## 保留和退出

- 保留业务含义、核算边界、核对效果、特殊情况及必要来源。纯函数复用仍须通过新合同和事先写好的独立预期。
- 旧准备、预览、确认、采用、固定八步和内部编号衔接，由程序承接或退出；不保留逐次批准、专业顾问签字和额外审批。
- 开放期仅存当前有效事实和结果，已有凭证保留编号；旧错误正文、开放期版本链、纠错审计日记退出。闭期必要冻结及关联冲正继续保留。
- 标准文档日常即归并最终事实。真实后续付款、退款、损失和清偿不是过程垃圾。最少请求防重和结果回读不得变成改名审计。
- 本文件核对源码和合成测试位置，未执行测试或真实业务。测试存在不代表全部场景已验证。旧政策必须按新规则目录核验官方来源、生效范围和适用条件，不能仅凭代码存在直接采用。

## 业务家庭与复用入口

以下简写路径以 `src/ai_accounting/` 为起点；下一节逐项给出完整源码位置。

| 家庭 | 核心范围和复用入口 |
| --- | --- |
| 经营 | 收入费用、预收预付、往来、报销、押金和项目成本：`kernel/domains/transactions.py`、`adjustments.py`、`labor_assets.py`、`managed_reserve.py`。 |
| 薪酬 | 用工薪酬档案、工资奖金劳务、社保公积金和有据个税额：`payroll/contributions.py:240`、`:417`；`kernel/domains/payroll.py:454`、`:513`、`:601`、`:685`。 |
| 税费 | 增值税附加、实际税额、有限季度所得税、税点和退抵：`kernel/domains/taxes.py:202`、`:638`、`:904`、`:1103`；`income_tax.py:240`。 |
| 资产 | 固定/无形资产及有限处置：`fixed_assets.py:129`、`:201`，`intangible_assets.py:91`；`kernel/domains/assets.py:113`、`:424`、`:535`、`:667`。 |
| 股东借款 | 股东借入本金/出资；持牌机构CNY固定利率贷款：`kernel/domains/transactions.py:1073`，`assets.py:777`、`:820`、`:930`，`borrowings.py:38`。 |
| 投资 | 目前仅货币基金成本申购/赎回：`kernel/domains/investments.py:73`、`:178`。 |
| 资金 | 银行现金平台收付、抵销、划转和对账：`kernel/domains/transactions.py:1326`、`:1927`，`banking.py:313`，`cash.py`、`platforms.py`。 |
| 期初 | 各业务期初余额和必要累计，不产生本期历史业务：`kernel/domains/opening.py:361`、`:479`、`:591`。 |
| 资料/接续/报表 | 来源位置、当前待办、外部已办及共同报表取数：`kernel/materials.py`、`workflow.py`、`reports.py`。 |

## 全量类型归属

“现”为当前注册输入，“史”为只读历史语法。无直接计算通常表示来源、政策或管理资料，不等于漏实现。表中的内部引用位置不是要求AI手填的接口字段。

| 原类型 | 状态 | 目标家庭 | 核心规则与特殊情形 | 承接方向 | 模型；计算入口 |
| --- | --- | --- | --- | --- | --- |
| `advance` | 现 | 经营 | 客户/供应商预收预付；权利义务已成立，预收税点独立 | 预收预付生命周期 | `src/ai_accounting/kernel/domains/transactions.py:609`；`src/ai_accounting/kernel/domains/transactions.py:654` |
| `advance_fulfillment` | 现 | 经营 | 分次履约消耗预收；累计净额与税额只确认一次 | 同业务履约明细 | `src/ai_accounting/kernel/domains/transactions.py:774`；`src/ai_accounting/kernel/domains/transactions.py:868` |
| `advance_refund` | 现 | 经营 | 预收退款确认；原税期、税额与实际退款分开 | 真实退款保留，纠错红冲归并 | `src/ai_accounting/kernel/domains/transactions.py:961`；`src/ai_accounting/kernel/domains/transactions.py:990` |
| `annual_bonus` | 现 | 薪酬 | 毛额减有据扣税额，形成实发和个税应付 | 奖金生命周期 | `src/ai_accounting/kernel/domains/payroll.py:570`；`src/ai_accounting/kernel/domains/payroll.py:601` |
| `annual_bonus_opening_usage` | 史 | 薪酬 | 历史年终奖优惠使用情况 | 仅历史，新输入退出 | `src/ai_accounting/kernel/payroll_history.py:515`；`无直接计算` |
| `annual_bonus_policy` | 史 | 薪酬 | 历史年终奖计算政策 | 仅历史，不代表当前计算能力 | `src/ai_accounting/kernel/payroll_history.py:494`；`无直接计算` |
| `asset` | 现 | 资产 | 固定/无形资产购入或项目完工，同一成本只资本化一次 | 资产生命周期 | `src/ai_accounting/kernel/domains/assets.py:67`；`src/ai_accounting/kernel/domains/assets.py:113` |
| `asset_activation` | 现 | 资产 | 启用日、寿命、残值、受益归属及舍入 | 保留事实，准备入口程序化 | `src/ai_accounting/kernel/domains/assets.py:394`；`src/ai_accounting/kernel/domains/assets.py:424` |
| `asset_activation_batch` | 现 | 资产 | 整批启用责任与卡片成员一致 | 保留原子批次，不重复凭证 | `src/ai_accounting/kernel/asset_batch_models.py:27`；`src/ai_accounting/kernel/asset_batch_models.py:63` |
| `asset_advance` | 现 | 资产 | 资产预付款及对应资产用途 | 资产主责，资金清偿 | `src/ai_accounting/kernel/domains/labor_assets.py:14`；`src/ai_accounting/kernel/domains/labor_assets.py:31` |
| `asset_consumption` | 现 | 资产 | 按卡片连续折旧摊销，可零额 | 程序衔接，不逐卡搬编号 | `src/ai_accounting/kernel/domains/assets.py:470`；`src/ai_accounting/kernel/domains/assets.py:535` |
| `asset_consumption_month` | 现 | 资产 | 月度批次与各卡结果 | 批次主责，成员不重复入账 | `src/ai_accounting/kernel/asset_batch_models.py:48`；`src/ai_accounting/kernel/asset_batch_models.py:63` |
| `asset_disposal` | 现 | 资产 | 固定资产出售/报废；无形资产限月末无收入报废 | 保留已支持边界 | `src/ai_accounting/kernel/domains/assets.py:621`；`src/ai_accounting/kernel/domains/assets.py:667` |
| `bank_income` | 现 | 经营 | 银行利息或属于公司的奖励，无返还义务 | 业务归属明确，实际款只记一次 | `src/ai_accounting/kernel/domains/transactions.py:2205`；`src/ai_accounting/kernel/domains/transactions.py:2230` |
| `bank_opening` | 现 | 资金 | 银行账面起点，不由流水期初代替 | 与期初公开交接 | `src/ai_accounting/kernel/domains/banking.py:126`；`src/ai_accounting/kernel/domains/banking.py:152` |
| `bank_platform_transfer` | 现 | 资金 | 银行和平台真实划转，无新损益 | 资金划转生命周期 | `src/ai_accounting/kernel/domains/platforms.py:390`；`src/ai_accounting/kernel/domains/platforms.py:460` |
| `bank_reconciliation` | 现 | 资金 | 原行与正式资金结果核对，支持组合匹配 | 保留核对，引用程序维护 | `src/ai_accounting/kernel/domains/banking.py:274`；`src/ai_accounting/kernel/domains/banking.py:313` |
| `bank_statement` | 现 | 资金 | 外部流水和期初期末连续，自身不再产生资金 | 当前有效来源，不留开放错版 | `src/ai_accounting/kernel/domains/banking.py:200`；`src/ai_accounting/kernel/domains/banking.py:232` |
| `cash_bank_transfer` | 现 | 资金 | 存现/取现只变资金载体 | 资金划转 | `src/ai_accounting/kernel/domains/cash.py:105`；`src/ai_accounting/kernel/domains/cash.py:130` |
| `cash_funding` | 现 | 股东借款 | 股东现金借入或出资 | 保留，非通用对外借贷 | `src/ai_accounting/kernel/domains/cash.py:76`；`src/ai_accounting/kernel/domains/transactions.py:1073` |
| `cash_payment` | 现 | 资金 | 现金真实收付并分摊债务，与银行共用核销容量 | 统一资金收付规则 | `src/ai_accounting/kernel/domains/cash.py:20`；`src/ai_accounting/kernel/domains/transactions.py:1326` |
| `company_workflow_scope_v2` | 现 | 接续 | 公司需要办理的工作范围 | 保留安排，退出固定流程框架 | `src/ai_accounting/kernel/workflow.py:120`；`无直接计算` |
| `continuation_report_profile` | 现 | 报表 | 年中接续报表起点和比较范围 | 不补造过去发生额 | `src/ai_accounting/kernel/reports.py:78`；`无直接计算` |
| `employee_advance` | 现 | 经营 | 个人实际先付款，不是公司现金付款 | 真实环节保留，纠错手续不搬 | `src/ai_accounting/kernel/domains/adjustments.py:35`；`src/ai_accounting/kernel/domains/adjustments.py:76` |
| `expense` | 现 | 经营 | 费用和供应商/员工/个人应付 | 保留分类、对象与债务 | `src/ai_accounting/kernel/domains/transactions.py:277`；`src/ai_accounting/kernel/domains/transactions.py:288` |
| `expense_recovery` | 现 | 经营 | 原费用退回权利，不超过原成本，现金独立 | 真实后续业务保留 | `src/ai_accounting/kernel/domains/transactions.py:314`；`src/ai_accounting/kernel/domains/transactions.py:337` |
| `external_basis_review` | 现 | 接续 | 已外部办理的结果与账务差异 | 保留当前差异，退出强制复核档案 | `src/ai_accounting/kernel/workflow.py:783`；`src/ai_accounting/kernel/workflow.py:832` |
| `external_completion` | 现 | 接续 | 老板实际已办理的事项和采用结果 | 真实状态，无批准环节 | `src/ai_accounting/kernel/workflow.py:296`；`src/ai_accounting/kernel/workflow.py:388` |
| `external_obligation` | 现 | 接续 | 应办事项、期限和覆盖期 | 当前待办，旧证明链退出 | `src/ai_accounting/kernel/workflow.py:236`；`无直接计算` |
| `filing_calendar_policy_v2` | 现 | 接续 | 办理日历、期限和来源 | 规则和当前提醒 | `src/ai_accounting/kernel/workflow.py:95`；`无直接计算` |
| `funding` | 现 | 股东借款 | 股东银行借入/出资，loan形成应付、capital形成资本 | 股东资金生命周期 | `src/ai_accounting/kernel/domains/transactions.py:1048`；`src/ai_accounting/kernel/domains/transactions.py:1073` |
| `funds_transfer` | 现 | 资金 | 两个自有银行账户真实划转 | 两边呈现，无新损益 | `src/ai_accounting/kernel/domains/transactions.py:2124`；`src/ai_accounting/kernel/domains/transactions.py:2193` |
| `income_tax_assessment` | 现 | 税费 | 有据累计定额或季度computed，扣除前次累计 | 保留两种依据路线 | `src/ai_accounting/kernel/domains/income_tax.py:133`；`src/ai_accounting/kernel/domains/taxes.py:904` |
| `income_tax_declaration_actual` | 现 | 税费 | 实际累计应纳、预缴和应补退，含义不混用 | 实际来源优先，缺项不算法补 | `src/ai_accounting/kernel/domains/taxes.py:419`；`无直接计算` |
| `income_tax_policy` | 现 | 税费 | 所得税计算政策、生效范围和官方来源 | 规则根需重新核验 | `src/ai_accounting/kernel/domains/income_tax.py:74`；`无直接计算` |
| `income_tax_prepayment_basis` | 现 | 税费 | 查账方式、小微适用、弥补亏损及特殊事项 | 保留核算事实，不转为批准 | `src/ai_accounting/kernel/domains/income_tax.py:80`；`无直接计算` |
| `labor` | 现 | 薪酬 | 有据最终个税额的劳务毛额、实发和税债 | 劳务生命周期 | `src/ai_accounting/kernel/domains/payroll.py:764`；`src/ai_accounting/kernel/domains/payroll.py:798` |
| `labor_accrual` | 现 | 薪酬 | 劳务已发生未扣缴先记毛额债务 | 后补税额只拆债务不重复成本 | `src/ai_accounting/kernel/domains/payroll.py:629`；`src/ai_accounting/kernel/domains/payroll.py:675` |
| `labor_income_tax_policy` | 史 | 薪酬 | 历史劳务个税计算政策 | 仅历史，新输入退出 | `src/ai_accounting/kernel/payroll_history.py:571`；`无直接计算` |
| `labor_project_cost` | 现 | 薪酬 | 劳务成本资本化进明确项目 | 项目/资产引用同一成本 | `src/ai_accounting/kernel/domains/labor_assets.py:68`；`src/ai_accounting/kernel/domains/labor_assets.py:93` |
| `loan_agreement` | 现 | 股东借款 | 持牌机构CNY固定利率、360/365、期限和到期日 | 有限机构贷款合同 | `src/ai_accounting/kernel/domains/assets.py:777`；`无直接计算` |
| `loan_drawdown` | 现 | 股东借款 | 机构贷款实际到账本金和银行账户 | 真实放款与本金应付 | `src/ai_accounting/kernel/domains/assets.py:795`；`src/ai_accounting/kernel/domains/assets.py:820` |
| `loan_interest` | 现 | 股东借款 | 未偿本金区间计息，途中还本分段、可零额 | 复用有限贷款算法 | `src/ai_accounting/kernel/domains/assets.py:856`；`src/ai_accounting/kernel/domains/assets.py:930` |
| `managed_reserve_expense` | 现 | 经营 | 公司真实款转入代管备用金并列管理费 | 保留特定业务效果，不另造备用金余额 | `src/ai_accounting/kernel/domains/managed_reserve.py:278`；`src/ai_accounting/kernel/domains/managed_reserve.py:331` |
| `managed_reserve_internal_movement` | 现 | 资金 | 代管备用金内部原行，不是公司资金或费用 | 无凭证处置 | `src/ai_accounting/kernel/domains/managed_reserve.py:36`；`src/ai_accounting/kernel/domains/managed_reserve.py:62` |
| `managed_reserve_refund` | 现 | 经营 | 备用金真实退回公司，冲减同类费用 | 真实退款，不等于撤错账 | `src/ai_accounting/kernel/domains/managed_reserve.py:284`；`src/ai_accounting/kernel/domains/managed_reserve.py:335` |
| `material_group_resolution` | 现 | 资料 | 一组来源对应业务或无需成账的原因 | 当前关联，旧追加处置链退出 | `src/ai_accounting/kernel/materials.py:524`；`无直接计算` |
| `material_period_allocation` | 现 | 资料 | 同一原件跨月核算范围 | 有据分配，不以文件月推定 | `src/ai_accounting/kernel/materials.py:234`；`无直接计算` |
| `material_resolution_v2` | 现 | 资料 | 原件位置到业务字段/有效结果对应 | 当前关联，不留错误旧版 | `src/ai_accounting/kernel/materials.py:447`；`无直接计算` |
| `material_source_v2` | 现 | 资料 | 原件、位置、金额方向和覆盖范围 | 保留有效来源，旧完整性证明重审 | `src/ai_accounting/kernel/materials.py:262`；`无直接计算` |
| `money_fund_redemption` | 现 | 投资 | 有据批次成本消耗；净款减成本确认损益 | 货币基金，非全部投资 | `src/ai_accounting/kernel/domains/investments.py:120`；`src/ai_accounting/kernel/domains/investments.py:178` |
| `money_fund_subscription` | 现 | 投资 | 申购价及费用组成成本，付款独立 | 货币基金生命周期 | `src/ai_accounting/kernel/domains/investments.py:49`；`src/ai_accounting/kernel/domains/investments.py:73` |
| `opening_asset` | 现 | 期初 | 原价、累计摊折、寿命、残值与已用月数 | 不补造过往月度凭证 | `src/ai_accounting/kernel/domains/opening.py:93`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `opening_bank` | 现 | 期初 | 建账银行余额 | 无本期现金流 | `src/ai_accounting/kernel/domains/opening.py:39`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `opening_basis_correction` | 现 | 期初 | 期初依据改正后的有效值 | 保留正确期初，旧纠错档案退出 | `src/ai_accounting/kernel/identity_corrections.py:204`；`src/ai_accounting/kernel/identity_corrections.py:224` |
| `opening_cash` | 现 | 期初 | 建账库存现金 | 不造历史提现或收入 | `src/ai_accounting/kernel/domains/opening.py:48`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `opening_equity` | 现 | 期初 | 实收资本、公积和未分配利润 | 亏损可负，非收入 | `src/ai_accounting/kernel/domains/opening.py:232`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `opening_identity_binding` | 现 | 期初 | 同一真实账户/对象/债务的有效身份对应 | 正确关联保留，绑定版本链退出 | `src/ai_accounting/kernel/identity_corrections.py:148`；`src/ai_accounting/kernel/identity_corrections.py:311` |
| `opening_loan` | 现 | 期初 | 未偿本金、利息和建账计息边界 | 复用机构贷款，不造放款日 | `src/ai_accounting/kernel/domains/opening.py:140`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `opening_money_fund` | 现 | 期初 | 基金批次成本及原批次身份 | 后续赎回消耗同一成本 | `src/ai_accounting/kernel/domains/opening.py:247`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `opening_obligation` | 现 | 期初 | 往来性质、方向、对象、原业务和未清金额 | 未来清偿，不补旧收入费用 | `src/ai_accounting/kernel/domains/opening.py:83`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `opening_package` | 现 | 期初 | 期初范围及资产负债权益平衡 | 保留平衡，强制零项证明重审 | `src/ai_accounting/kernel/domains/opening.py:332`；`src/ai_accounting/kernel/domains/opening.py:479` |
| `opening_payroll_payable` | 现 | 期初 | 既往工资实发、税、社保公积金未清组成 | 未来清偿，不重复计提 | `src/ai_accounting/kernel/domains/opening.py:174`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `opening_payroll_state` | 史 | 薪酬 | 历史累计个税起点 | 仅历史，不补造旧累计 | `src/ai_accounting/kernel/domains/opening.py:198`；`无直接计算` |
| `opening_tax` | 现 | 期初 | 既往税期应付或可退税额 | 计提与实际缴退独立 | `src/ai_accounting/kernel/domains/opening.py:159`；`src/ai_accounting/kernel/domains/opening.py:591` |
| `overpayment` | 现 | 经营 | 实付超过当前应付的追收权利 | 保留真实钱，不改实付凑账 | `src/ai_accounting/kernel/domains/transactions.py:2324`；`src/ai_accounting/kernel/domains/transactions.py:2354` |
| `pass_through` | 现 | 经营 | 代收债权和对真实最终权利人的转付债务 | 不认公司收入费用 | `src/ai_accounting/kernel/domains/transactions.py:550`；`src/ai_accounting/kernel/domains/transactions.py:578` |
| `pass_through_return` | 现 | 经营 | 代收未转部分的退款，不超实际余额 | 真实后续退款 | `src/ai_accounting/kernel/domains/adjustments.py:239`；`src/ai_accounting/kernel/domains/adjustments.py:259` |
| `payment` | 现 | 资金 | 银行收付，单笔/批量与多债务分摊 | 资金入口，程序衔接内部顺序 | `src/ai_accounting/kernel/domains/transactions.py:1138`；`src/ai_accounting/kernel/domains/transactions.py:1326` |
| `payroll` | 现 | 薪酬 | 普通工资、社保公积金、有据扣税额 | 主工资生命周期 | `src/ai_accounting/kernel/domains/payroll.py:214`；`src/ai_accounting/kernel/domains/payroll.py:512` |
| `payroll_bounded` | 现 | 薪酬 | 当前与payroll同字段同算法的稳定旧身份 | 不建第二工资模型或表 | `src/ai_accounting/kernel/domains/payroll.py:243`；`src/ai_accounting/kernel/domains/payroll.py:512` |
| `payroll_change_notice_v2` | 现 | 薪酬 | 用工和工资资料的现实变化 | 保留当前变化，非全变更档案 | `src/ai_accounting/kernel/payroll_preparation.py:67`；`无直接计算` |
| `payroll_contribution_actual` | 现 | 薪酬 | 逐缴费项个人/公司实际申报额及状态 | 实际值优先，明确零额 | `src/ai_accounting/kernel/domains/payroll.py:197`；`无直接计算` |
| `payroll_contribution_policy` | 现 | 薪酬 | 项目、基数上下限、费率、舍入、生效范围 | 纯函数复用，官方根核验 | `src/ai_accounting/kernel/domains/payroll.py:143`；`无直接计算` |
| `payroll_disbursement_basis` | 现 | 薪酬 | 多笔/跨月实发与人员/税期对应 | 真实归属，内部引用程序化 | `src/ai_accounting/kernel/payroll_tax_declarations.py:199`；`src/ai_accounting/kernel/payroll_tax_declarations.py:243` |
| `payroll_first_wage_treatment` | 史 | 薪酬 | 历史首薪个税处理 | 仅历史，当前明确税额不依赖 | `src/ai_accounting/kernel/payroll_history.py:255`；`无直接计算` |
| `payroll_income_tax_policy` | 史 | 薪酬 | 历史工资累计个税政策 | 仅历史，不恢复旧计算路线 | `src/ai_accounting/kernel/payroll_history.py:139`；`无直接计算` |
| `payroll_no_change_v2` | 现 | 薪酬 | 沿用既有工资资料的无变化确认 | 保留复用，退出逐月证明门槛 | `src/ai_accounting/kernel/payroll_preparation.py:108`；`无直接计算` |
| `payroll_opening_state` | 史 | 薪酬 | 历史工资个税累计状态 | 仅历史，不生成新开账输入 | `src/ai_accounting/kernel/payroll_history.py:225`；`无直接计算` |
| `payroll_plan_v2` | 现 | 薪酬 | 人员月份安排及采用档案/政策 | 工作安排保留，程序承接旧确认 | `src/ai_accounting/kernel/payroll_preparation.py:29`；`无直接计算` |
| `payroll_profile` | 现 | 薪酬 | 员工、适用月段、参与情况、基数和差额承担 | 用工薪酬现行档案 | `src/ai_accounting/kernel/domains/payroll.py:164`；`无直接计算` |
| `payroll_reserve_payment` | 现 | 资金 | 毛额代发=全组净薪清偿+实际返备用金费用 | 组合原子效果，不造个人返款矩阵 | `src/ai_accounting/kernel/domains/payroll_reserve_payment.py:30`；`src/ai_accounting/kernel/domains/payroll_reserve_payment.py:132` |
| `payroll_tax_declaration_actual` | 现 | 税费 | 实际薪酬申报人员、税期和明细 | 真实申报，不证明实付税或批准 | `src/ai_accounting/kernel/payroll_tax_declarations.py:172`；`无直接计算` |
| `payroll_withholding_actual` | 史 | 薪酬 | 历史累计预扣字段 | 仅历史，现行明确税额承接 | `src/ai_accounting/kernel/payroll_history.py:297`；`无直接计算` |
| `platform_expense_confirmation` | 现 | 经营 | 公司平台费用归属和原行消耗 | 避免与普通expense重复认成本 | `src/ai_accounting/kernel/domains/platforms.py:198`；`src/ai_accounting/kernel/domains/platforms.py:235` |
| `platform_funding` | 现 | 股东借款 | 股东平台借入/出资及原行 | 与银行现金同一资金业务规则 | `src/ai_accounting/kernel/domains/platforms.py:360`；`src/ai_accounting/kernel/domains/platforms.py:492` |
| `platform_movement` | 现 | 资金 | 平台外部原行、方向、金额和位置 | 只登记来源，不自动认业务 | `src/ai_accounting/kernel/domains/platforms.py:59`；`src/ai_accounting/kernel/domains/platforms.py:99` |
| `platform_payment` | 现 | 资金 | 平台真实收付核销和原行消耗 | 共用债务容量，不重复资金 | `src/ai_accounting/kernel/domains/platforms.py:301`；`src/ai_accounting/kernel/domains/platforms.py:487` |
| `project_cost` | 现 | 资产 | 外购无形或内部开发项目的有据资本化成本 | `assets.project_cost`，非对外合作投资 | `src/ai_accounting/kernel/domains/transactions.py:392`；`src/ai_accounting/kernel/domains/transactions.py:407` |
| `project_release` | 现 | 资产 | 原资本化项目成本转费用 | `assets.project_release`，原成本不重复释放 | `src/ai_accounting/kernel/domains/transactions.py:514`；`src/ai_accounting/kernel/domains/transactions.py:537` |
| `refundable_deposit` | 现 | 经营 | 可退押金权利及待付债务 | 付款、退款分别对应 | `src/ai_accounting/kernel/domains/transactions.py:2250`；`src/ai_accounting/kernel/domains/transactions.py:2257` |
| `reimbursed_asset` | 现 | 资产 | 公司接受个人垫购资产，取得月明确、日可未知 | 资产和报销债权分别对应 | `src/ai_accounting/kernel/domains/assets.py:251`；`src/ai_accounting/kernel/domains/assets.py:316` |
| `reimbursed_asset_batch` | 现 | 资产 | 资产合计=债权人合计，无需人乘资产矩阵 | 保留两个真实合计 | `src/ai_accounting/kernel/domains/assets.py:168`；`src/ai_accounting/kernel/domains/assets.py:208` |
| `reimbursed_deposit` | 现 | 经营 | 个人垫付押金形成押金债权与报销应付 | 报销不消灭对房东债权 | `src/ai_accounting/kernel/domains/transactions.py:2285`；`src/ai_accounting/kernel/domains/transactions.py:2296` |
| `reimbursement_acceptance` | 现 | 经营 | 个人替公司清偿原债，公司承接报销债务 | 不重复确认原成本 | `src/ai_accounting/kernel/domains/adjustments.py:85`；`src/ai_accounting/kernel/domains/adjustments.py:123` |
| `remuneration_tax_basis` | 现 | 薪酬 | 对应报酬、独立税期及有据最终个税额 | 明细合并，AI不手算税额 | `src/ai_accounting/kernel/remuneration_tax_basis.py:81`；`无直接计算` |
| `report_carry_forward` | 现 | 报表 | 年中接续累计和比较资料 | 保留必要来源，非历史调整档案 | `src/ai_accounting/kernel/reports.py:105`；`无直接计算` |
| `report_classification` | 现 | 报表 | 同一核算结果到报表项目映射 | 共同口径，不各自重算 | `src/ai_accounting/kernel/reports.py:166`；`无直接计算` |
| `report_income_tax_confirmation` | 现 | 报表 | 旧报表所得税采用依据 | 合并税费正式结果，不重复确认 | `src/ai_accounting/kernel/reports.py:185`；`无直接计算` |
| `report_profile` | 现 | 报表 | 报表适用规则和呈现范围 | 保留适用信息，不按墙钟猜 | `src/ai_accounting/kernel/reports.py:64`；`无直接计算` |
| `sale_return` | 现 | 经营 | 真实销售退回、红字税额及原税期 | 真实退回保留，纠错红冲归并 | `src/ai_accounting/kernel/domains/transactions.py:1994`；`src/ai_accounting/kernel/domains/transactions.py:2020` |
| `service_sale` | 现 | 经营 | 服务收入、应收、纳税月及待转税额 | 收入、税点、收款分别对应 | `src/ai_accounting/kernel/domains/transactions.py:114`；`src/ai_accounting/kernel/domains/transactions.py:143` |
| `service_tax_point` | 现 | 税费 | 服务/预收纳税范围，转出待转税不重复收入 | 依据明确事实程序接续 | `src/ai_accounting/kernel/domains/transactions.py:1662`；`src/ai_accounting/kernel/domains/transactions.py:1717` |
| `settlement` | 现 | 资金 | 同交易方反向债务抵销或预付款应用 | 无新现金，校验用途 | `src/ai_accounting/kernel/domains/transactions.py:1890`；`src/ai_accounting/kernel/domains/transactions.py:1927` |
| `surtax_policy` | 现 | 税费 | 城建、教育、地方附加及减征、生效范围 | 规则根版本化 | `src/ai_accounting/kernel/domains/taxes.py:336`；`无直接计算` |
| `tax_assessment` | 现 | 税费 | 完整税期增值税附加计提，实际来源或计算 | 统一核算，不逐一搬旧入口 | `src/ai_accounting/kernel/domains/taxes.py:582`；`src/ai_accounting/kernel/domains/taxes.py:638` |
| `tax_credit_confirmation` | 现 | 税费 | 税局实际退抵、原申报及退回来源 | 真实外部结果，非系统批准 | `src/ai_accounting/kernel/domains/taxes.py:1018`；`src/ai_accounting/kernel/domains/taxes.py:1103` |
| `tax_import_details_v2` | 现 | 税费 | 原税表字段和明细 | 解析及实际取数，非过程档案 | `src/ai_accounting/kernel/tax_import.py:84`；`无直接计算` |
| `tax_import_identity_v2` | 现 | 税费 | 原表所属公司、表式和期间 | 不能串公司 | `src/ai_accounting/kernel/tax_import.py:64`；`无直接计算` |
| `tax_import_mapping_v2` | 现 | 税费 | 原表位置到明确税务字段 | 资料解析映射，不改核算意义 | `src/ai_accounting/kernel/tax_import.py:132`；`无直接计算` |
| `tax_point_basis` | 现 | 税费 | 合同/收款/开票/服务的有源税点与金额范围 | 保留精度和覆盖，引用程序化 | `src/ai_accounting/kernel/tax_point_basis.py:93`；`无直接计算` |
| `used_asset_vat_policy` | 现 | 税费 | 旧资产出售计税基础率和实纳率 | 不同于普通服务VAT | `src/ai_accounting/kernel/domains/taxes.py:341`；`无直接计算` |
| `vat_declaration_actual` | 现 | 税费 | 实际VAT/附加应纳、预缴、应补退 | 实际优先，未知不取参考填补 | `src/ai_accounting/kernel/domains/taxes.py:359`；`无直接计算` |
| `vat_policy` | 现 | 税费 | 征收率、阈值及比较符、生效区间 | 规则根，核验官方依据 | `src/ai_accounting/kernel/domains/taxes.py:331`；`无直接计算` |

## 新合同如何合并旧办理步骤

新字段和算法见 [业务规则](../05-business-rules.md) 与 [严格事实Schema](../contracts/business-facts.schema.json)。新合同不再逐项保留旧注册动作：

| 旧输入组合 | 新承接 |
| --- | --- |
| payroll/payroll_bounded、profile、contribution policy/actual、税额依据 | `people.relationship`保存当前正确人员关系，`people.employment`仅管缴费参数，另由contribution_policy/contribution_actual/remuneration承接缴费及报酬；一人一工资月一个根，有据税额嵌入报酬，不另建工资审批。 |
| labor/labor_accrual/labor_project_cost | remuneration的劳务严格变体；费用/项目去向、未扣未报/有据税额分别明确。 |
| 工资计划/无变更/变化通知、disbursement_basis | 当前工作安排及已有资料复用；必要申报代发口径嵌入工资事实，留差额不重记税，不保旧确认链。 |
| 三种实际付款、平台原行、备用金组合 | `money.movement`按真实资金性质变体；`money.platform_activity`仅保存原行，`money.reserve_internal`只处置来源。 |
| 员工代垫、公司接受、付款后降价多付 | 严格经营事实产生统一内部义务转移/超额重分类；保持真实实付及已采用分配，形成正确追收。 |
| 资产单/批接受、资产取得、启用批次、逐卡/月度摊折 | acquisition的严格变体、activation批次及程序生成consumption；一个责任结果加逐卡明细，不能重复入账。 |
| 基金期初及申购/赎回 | opening.package的基金明细与investment货币基金严格类型，共用可消费成本。 |
| 报表profile/classification/所得税确认/carry-forward | 公司报表适用信息、业务事实细类、实际税费结果及reports.continuation；不让AI按凭证版本逐行补技术分类。 |
| 期初多明细、总包、身份绑定和依据改正 | 一个opening.package及严格成员；当前有效身份/金额原子更正，旧错版本与纠错档案退出。 |
| 原件/逐项处置/分配/分组处置 | 资料来源、当前关系与未解事项；保留跨月来源和已用事实，退出只为证明操作过程的追加版本。 |

银行账户在公司建账后新开，不再为“新增账户”造一套凭证。对象的 `opened_period + zero_at_opening:true` 可声明有据零起点；已有余额按期初/接续处理，不能从流水期初自动认账。

## 必须保住的特殊场景及取证位置

下表是新验收的候选来源；旧测试中的预览批准、旧版本链和审计断言不直接继承。

| 特殊场景 | 旧合成测试位置 |
| --- | --- |
| 有据税额工资；实际社保替代估算；缺税额不能猜零 | `tests/kernel/test_payroll.py:333`、`:345`、`:376`。 |
| 劳务先计毛额、资本化只一次、预付与来源人一致 | `tests/kernel/test_labor_assets.py:152`、`:271`、`:307`、`:331`。 |
| 薪酬源改小后保留实付，需明确超付追收；闭期连续更正 | `tests/kernel/test_payroll_corrections.py:295`、`:346`、`:393`。 |
| 实际税额优先，参考失败不阻挡；实际缺项不回退；预缴不混应纳 | `tests/kernel/test_actual_tax_priority.py:135`、`:188`、`:244`、`:437`。 |
| 所得税computed取共享利润、按季度累计、实际覆盖参考 | `tests/kernel/test_income_tax_provision.py:88`、`:122`、`:197`；`test_computed_income_tax_flow.py:56`、`:87`、`:139`。 |
| 普通/预收税点、分次实际收款、不重税、跨期退抵 | `tests/kernel/test_service_tax_points.py:590`、`:624`、`:748`、`:845`、`:1318`；`test_tax_credits.py:248`、`:296`。 |
| 月精度不补造实际日，月中政策确需日；原行不能重复税点 | `tests/kernel/test_tax_point_basis.py:122`、`:206`、`:333`、`:433`；`test_tax_point_receipt_identity.py:53`。 |
| 实际费用退回、不同款项载体、税/社保滞纳性质分别冲回 | `tests/kernel/test_expense_recovery.py:32`、`:106`、`:252`。 |
| 代垫押金债权保留，老板代垫不变员工，未来清偿不扰闭期 | `tests/kernel/test_cash.py:271`、`:450`、`:519`、`:569`。 |
| 毛额发薪含备用金不造二次银行款，不造个人返款矩阵，整体失败回滚 | `tests/kernel/test_payroll_reserve_payment.py:81`、`:235`、`:315`。 |
| 代管内部原行无公司资金/费用，无凭证完成 | `tests/kernel/test_reserve_internal_movement.py:54`、`:135`、`:171`。 |
| 无人乘资产分摊矩阵，双合计平衡，卡片不重复资产债权 | `tests/kernel/test_reimbursement_assets.py:336`、`:341`、`:410`。 |
| 批次一张责任凭证、卡片明细，启用更正保编号 | `tests/kernel/test_asset_batches.py:123`、`:222`、`:255`；`test_asset_batch_invariants_review.py:108`。 |
| 货币基金多批成本只耗一次、零回款全损、成本改正不改真实收款 | `tests/kernel/test_investments.py:480`、`:499`、`:540`、`:567`。 |
| 多笔完整资金对应一条原行、批量多收款人、现金平台不混银行 | `tests/kernel/test_bank_composite_matching.py:109`、`:164`；`test_banking.py:633`、`:719`。 |
| 期初不造凭证/收入/现金流；未来清偿；资产/利息接续；年中不造半年工资 | `tests/kernel/test_opening_continuation.py:172`、`:234`、`:298`、`:625`。 |
| 无影响来源补充不重复凭证；同金额换月份仍是真会计变化 | `tests/kernel/test_review_no_impact.py:13`、`:35`、`:73`；`test_tax_accounting_equivalence.py:281`、`:460`。 |

## 有限支持、新增与明确退出

- 机构贷款支持持牌机构人民币固定利率；股东借入本金与出资另有既有能力。通用对外借出、普通其他贷款计息不因此被宣称已实现。
- 货币基金支持有据成本及实际结算；合作项目为明确新增，金融理财、债券、一般股权等预设类别不等于本轮已有全部公式。
- 现有企业所得税已经有有限季度computed与有据定额两种路线；个税当前为有据金额，8种历史个税类型均不能冒充当前可提交模型。
- 无形资产目前只承接已有月末无收入报废；任意无形资产转让、全部税种和全部申报表式不在既有能力内。
- 旧表式/2023年税法测试不保证2026年适用。新VAT阈值、开票选择、基础率、实纳率和表式有效范围按官方规则重核。
- 旧报表4301列存货的映射不继承：满足资本化条件的内部开发应列开发支出26；外购待启用189901列其他非流动资产28。新三表明确映射、现金流正式分类和独立数字例见05b，不把旧标签和整包Reports当作新模块公开接口。
- 旧一般坏账/债权核销没有当前类型化入口。报表有坏账行不等于可办坏账；不为“未来也许需要”预留通用核销API。已有资产/基金损失照各自成本规则保留。
- 旧开放版本审计、批准、重复确认、全历史兼容、旧库升级/导入、按旧规则重放均不是新体系交付要求。保留源码经验不构成操作真实资料、换库或重放授权。
