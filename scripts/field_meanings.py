"""Reviewed exact field supplements; formal field metadata takes precedence.

These are business explanations, not a model/field manifest. Unknown names do
not receive another field's meaning. No schema, registry or Git is read here.
"""

from __future__ import annotations

SUPPLEMENTS: dict[str, str] = {}


def _add(model: str, **fields: str) -> None:
    for field, meaning in fields.items():
        SUPPLEMENTS[model + "." + field] = meaning


_add(
    "ai_accounting.kernel.asset_batch_models.ActivationMember",
    subject_id="批量启用选择的原单资产启用事实主题，计算时核验当前采用版本及未重复采用",
)

_add(
    "ai_accounting.kernel.asset_batch_models.AssetActivationBatch",
    members="本次批量启用采用的精确已登记单资产启用主题清单，不复制卡片参数",
)

_add(
    "ai_accounting.kernel.contracts.BalanceEffect",
    amount="该业务余额键的有符号整数分变化额，正负表示该余额增加或减少",
    category="该余额变化的业务归类，如应收、应付、资产或投资成本，供余额投影和清偿核验",
    key="业务义务或资产余额的精确语义键，用于按原计算结果归集和消耗额度",
)

_add(
    "ai_accounting.kernel.contracts.Calculation",
    fact_id="不可变事实版本标识，精确固定当时采用的正文；不是当前主题指针",
    id="不可变计算记录标识，由内核计算生成，不能作为负责人提交的业务显示编号",
    kind="计算对应的已注册业务事实类型，用于按所属计算器解释values及核验精确来源",
    result_digest="正式计算业务结果的完整性摘要，用于精确结果比对、采用及冻结核验",
    subject_id="内核登记的业务主题身份；与事实版本和计算版本标识分别保留",
    values="领域计算器返回的不可变业务结果字典，键和经济含义由实际注册计算器决定",
)

_add(
    "ai_accounting.kernel.contracts.Line",
    account="领域模板指定的核算科目代码，调用者不能通过业务入口自由选择科目",
    cashflow="本凭证行由领域模板声明的现金流语义类别，供报表归集，不自行改变资金方向",
    credit="本计算凭证行的贷方整数分金额，非负且与借方满足单行及凭证平衡约束",
    debit="本计算凭证行的借方整数分金额，非负且与贷方满足单行及凭证平衡约束",
)

_add(
    "ai_accounting.kernel.contracts.Outcome",
    balances="计算器声明的业务义务或资产余额变化，用于余额和清偿投影",
    explanation="领域计算器记录的政策、步骤和采用依据说明，随计算结果冻结",
    lines="领域计算器产生的封闭模板凭证行，正式发布时核验平衡；公共入口不接收自由分录",
    opening="标识本计算是否属于期初接续，决定期初包及正常业务处理边界",
    opening_lines="期初明细产生的期初专用凭证行，由期初包汇总平衡；不是历史交易补记",
    values="领域计算器返回的不可变业务结果字典，键和经济含义由实际注册计算器决定",
)

_add(
    "ai_accounting.kernel.display.BusinessProfile",
    display_name="当前业务记录明确登记的业务显示标题，与所关联对象名称独立",
    entity_id="该业务明确关联的公司目录对象身份，展示时复用对象档案，不在业务档案复制名称编号",
    kind="业务显示档案所属的已登记业务类型，供按实际业务类别核验和展示",
)

_add(
    "ai_accounting.kernel.domains.adjustments.EmployeeAdvance",
    payer_id="已代公司向原债权人付款的员工或负责人对象身份，不是原债实际收款人",
    payer_kind="实际代付人的员工或负责人身份性质，与payer_id对应核对",
    payment_on_behalf_confirmed="明确确认个人已代公司向原债权人付款，不把公司报销认定为个人垫付完成",
    sources="个人代付采用的原债业务义务、原收款人及代付金额分配",
)

_add(
    "ai_accounting.kernel.domains.adjustments.PassThroughReturn",
    amount_fen="本期明确退回原代收代付委托款的金额，整数分；受原来源剩余义务容量核验",
    refund_right_confirmed="明确确认该项退款或可退押金的权利及义务已成立，不以实际回款替代权利依据",
    source_id="本次退回采用的原pass_through主题引用，按原代收代付权利义务核对",
)

_add(
    "ai_accounting.kernel.domains.adjustments.ReimbursementAcceptance",
    payer_id="已代公司向原债权人付款的员工或负责人对象身份，不是原债实际收款人",
    payer_kind="实际代付人的员工或负责人身份性质，与payer_id对应核对",
)

_add(
    "ai_accounting.kernel.domains.assets.AcceptedAssetCost",
    asset_id="公司资产对象身份，用于衔接取得、启用、消耗、处置及期初卡片",
    asset_type="资产的核算类别，区分固定资产与无形资产的科目和消耗起算规则",
    cost_fen="整批公司验收中明确归属于这项资产的取得成本，整数分",
)

_add(
    "ai_accounting.kernel.domains.assets.AssetAcquisition",
    acquisition_basis="公司取得资产的明确基础，区分供应商采购与项目归集转入",
    asset_id="公司资产对象身份，用于衔接取得、启用、消耗、处置及期初卡片",
    asset_type="资产的核算类别，区分固定资产与无形资产的科目和消耗起算规则",
    cost_fen="本项有依据的取得或期初账面成本，以整数分保存，不以当前余额倒推",
    project_sources="本次项目结转或资产取得实际采用的项目成本来源及分配金额",
    supplier_id="承担该项采购或项目成本的实际供应商对象身份，用于形成供应商应付义务",
)

_add(
    "ai_accounting.kernel.domains.assets.AssetActivation",
    asset_id="公司资产对象身份，用于衔接取得、启用、消耗、处置及期初卡片",
    benefit_area="资产消耗受益的管理、销售或服务成本类别，用于封闭核算模板归类",
    residual_fen="明确预计残值，作为可消耗成本的扣减项；无形资产须满足零残值规则",
    rounding_policy="资产卡片的分摊舍入规则，区分向下取整尾月补余和逐卡四舍五入",
    useful_life_months="明确的预计使用或摊销总月数，为资产消耗卡片的计算参数",
)

_add(
    "ai_accounting.kernel.domains.assets.AssetConsumption",
    asset_id="公司资产对象身份，用于衔接取得、启用、消耗、处置及期初卡片",
)

_add(
    "ai_accounting.kernel.domains.assets.AssetDisposal",
    asset_id="公司资产对象身份，用于衔接取得、启用、消耗、处置及期初卡片",
    buyer_id="资产处置的实际购买方对象身份，用于处置收入及收款义务",
    disposal_kind="资产处置的明确性质，出售或报废等允许类别，决定核算模板及税务要求",
    exemption_eligible="明确确认该项业务符合所引用增值税政策的免税条件；不是按税额为零推定",
    gross_proceeds_fen="资产处置明确的含税对价总额，整数分；报废须符合零对价等模型约束",
    vat_policy_id="明确引用的增值税政策主题，计算时核对适用期及当前采用版本",
)

_add(
    "ai_accounting.kernel.domains.assets.LoanAgreement",
    annual_rate_percent="合同约定的精确十进制年利率百分数，不使用二进制浮点",
    currency="借款合同的明确币种；当前计算仅接受所声明支持的币种",
    day_count_basis="合同约定的年计息天数基础，用于按实际计息区间计算利息",
    lender_id="提供借款的实际出借人对象身份，用于本金、利息义务的归属",
    lender_is_licensed="明确出借人是否为持牌金融机构，决定相应借款核算要求",
    loan_term="借款的明确期限分类，区分短期与长期核算，不从到期日自动推定",
)

_add(
    "ai_accounting.kernel.domains.assets.LoanDrawdown",
    agreement_id="已登记借款合同的业务主题引用，用于复用出借人、利率及计息规则",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    principal_fen="逐笔借款的实际取得或期初未偿本金，以整数分保存；不是借款额度",
)

_add(
    "ai_accounting.kernel.domains.assets.LoanInterest",
    drawdown_id="本次计息采用的精确逐笔放款主题引用，由放款复用合同利率和未偿本金",
)

_add(
    "ai_accounting.kernel.domains.assets.ReimbursedAsset",
    asset_id="公司资产对象身份，用于衔接取得、启用、消耗、处置及期初卡片",
    asset_type="资产的核算类别，区分固定资产与无形资产的科目和消耗起算规则",
    company_acceptance_confirmed="明确确认公司已接受该项资产、押金或垫付义务，不把个人采购自动视为公司取得",
    cost_fen="本项有依据的取得或期初账面成本，以整数分保存，不以当前余额倒推",
    creditors="公司接受的个人代购垫付债权人及明确金额分摊，不能由总成本猜测个人份额",
)

_add(
    "ai_accounting.kernel.domains.assets.ReimbursedAssetBatch",
    acquisition_date="公司取得资产的实际日；允许未知时保留空，不能用个人采购日或月末替代",
    assets="整批公司接受的逐资产身份、资产类型和成本清单，不从债权人金额猜测单资产成本",
    company_acceptance_confirmed="明确确认公司已接受该项资产、押金或垫付义务，不把个人采购自动视为公司取得",
    cost_fen="公司整批接受个人代购资产的总成本，整数分；须与资产成本及债权人分摊分别相等",
    creditors="公司接受的个人代购垫付债权人及明确金额分摊，不能由总成本猜测个人份额",
)

_add(
    "ai_accounting.kernel.domains.assets.ReimbursementCreditor",
    amount_fen="公司接受的这名个人代购垫付债权金额，整数分；与整项资产成本分开核验",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
)

_add(
    "ai_accounting.kernel.domains.banking.BankEntry",
    description="原银行流水摘要文本，只作来源信息，不自动解释会计处理",
    reference="原银行流水的准确行参考标识，供勾稽同一行，不自动制造业务号",
    signed_fen="银行流水原始带符号金额，收入为正、支出为负，保留原方向",
)

_add(
    "ai_accounting.kernel.domains.banking.BankOpening",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    basis="明确银行起始余额属于新开账户还是首次纳入，决定允许的期初余额约束",
    opening_fen="该银行账户原始开户或流水期间起始金额，整数分",
)

_add(
    "ai_accounting.kernel.domains.banking.BankReconciliation",
    matches="每条原银行流水到正式收付或转账业务的明确勾稽关系，不凭同金额自动匹配",
    statement_id="已登记银行流水主题引用，账户和月份须与本次勾稽一致",
)

_add(
    "ai_accounting.kernel.domains.banking.BankStatement",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    closing_fen="原银行流水列明的期末余额，整数分，须与逐笔流水计算一致",
    entries="原银行流水逐行参考、实际日、带符号金额及原摘要，用于核对期初至期末勾稽",
    opening_fen="该银行账户原始开户或流水期间起始金额，整数分",
)

_add(
    "ai_accounting.kernel.domains.banking.Match",
    reference="原银行流水的准确行参考标识，供勾稽同一行，不自动制造业务号",
    source_kind="银行原行明确对应的正式收付款或互转类型",
)

_add(
    "ai_accounting.kernel.domains.cash.CashBankTransfer",
    amount_fen="本次公司现金与银行账户间实际转移的同额资金，整数分；direction明确银行侧方向",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    cash_account_id="明确登记的现金资金账户对象引用，与银行或平台账户分别核算",
    direction="取现withdrawal表示银行流出现金流入，存现deposit表示现金流出银行流入；不形成收入费用",
)

_add(
    "ai_accounting.kernel.domains.cash.CashFunding",
    amount_fen="出资人本次实际投入公司所选银行、现金或平台账户的资金金额，整数分；性质由funding_kind明确",
    cash_account_id="明确登记的现金资金账户对象引用，与银行或平台账户分别核算",
    funding_kind="资金投入的性质，区分资本投入与负责人借款，不由收款金额推断",
    owner_id="提供资金的出资人或公司负责人对象引用，结合funding_kind区分投入权益和代垫借款",
)

_add(
    "ai_accounting.kernel.domains.cash.CashPayment",
    allocations="本次实际收付对已采用业务义务的逐项分配，合计须满足该付款模型的总额约束",
    amount_fen="本次所选资金账户实际流入或流出的总额，整数分；须由逐项原义务分配完整解释",
    cash_account_id="明确登记的现金资金账户对象引用，与银行或平台账户分别核算",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    direction="实际资金相对所选公司账户的流入或流出方向，不接收任意借贷方向",
    payment_method="实际支付方式，区分单一具名交易方付款与有逐项收款人的银行汇总代付",
)

_add(
    "ai_accounting.kernel.domains.investments.MoneyFundRedemption",
    classification="明确确认该货币基金可随时赎回且持有意图不超过一年，不从产品名称推定",
    cost_basis="明确采用可复核的原始账面成本依据，不按收益率或账户余额反推",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    fund_id="所投资的具体基金产品对象身份；不是银行或支付平台账户身份",
)

_add(
    "ai_accounting.kernel.domains.investments.MoneyFundSubscription",
    acquisition_fees_fen="货币基金申购直接相关税费，整数分，零费用也须明确确认",
    classification="明确确认该货币基金可随时赎回且持有意图不超过一年，不从产品名称推定",
    cost_basis="明确采用可复核的原始账面成本依据，不按收益率或账户余额反推",
    excludes_declared_unpaid_distributions="明确申购成本不包含已宣告尚未发放收益，防止把应收收益计入投资成本",
    purchase_price_fen="货币基金申购明确支付价款，整数分，是账面取得成本的一部分",
)

_add(
    "ai_accounting.kernel.domains.investments.RedemptionCost",
    cost_fen="本次从该原基金批次消耗的账面成本，整数分；与本次净回款分别确认",
    source_id="本次赎回明确消耗的原申购或期初成本批次主题",
    source_kind="本次赎回消耗的原申购或期初货币基金成本类型",
)

_add(
    "ai_accounting.kernel.domains.labor_assets.AssetAdvance",
    amount_fen="已成立合同权利义务的资产采购预付金额，整数分，不提前确认为已取得固定或无形资产",
    asset_type="资产的核算类别，区分固定资产与无形资产的科目和消耗起算规则",
    contractual_obligation_established="明确确认预付或预收的合同权利义务已经成立",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
)

_add(
    "ai_accounting.kernel.domains.labor_assets.LaborProjectCost",
    capitalization_conditions_confirmed="明确确认项目成本满足资本化条件，不能仅凭项目名称或付款决定",
    gross_fee_fen="本期按权责发生确认的劳务税前费用，整数分；不是已实际支付净额",
    person_id="取得劳务报酬的公司目录人员身份，不意味着与公司存在工资任职关系",
    project_id="成本归集或结转的明确项目对象引用，用于核对同一项目的成本来源",
    tax_treatment="明确该项未付劳务成本尚未代扣、尚未申报；当前仅支持not_withheld_not_filed，不据此生成实际扣税或缴税",
)

_add(
    "ai_accounting.kernel.domains.managed_reserve.ManagedReserveInternalMovement",
    boundary="明确此组平台原行只属于备用金内部边界，不形成公司新增收付或费用",
    boundary_evidence_digest="确认备用金内部经济边界的保全原件摘要，必须在本事实采用证据中",
    movement_ids="本次处理实际消耗的平台原始变动主题引用，校验账户、日期、方向、金额与重复采用",
    platform_account_id="明确登记的支付平台账户对象引用；平台标识从正式账户身份链读取",
)

_add(
    "ai_accounting.kernel.domains.opening.Counts",
    assets="期初资产卡片明细的明确应有数量",
    bank="期初银行余额明细的明确应有数量",
    cash="期初现金余额明细的明确应有数量",
    counterparties="期初往来债权债务明细的明确应有数量",
    equity="期初权益明细的明确应有数量",
    loans="期初逐笔借款明细的明确应有数量",
    payroll_cumulative="期初人员累计个税状态明细的明确应有数量",
    payroll_payables="期初未清职工薪酬各组成明细的明确应有数量",
    taxes="期初税款明细的明确应有数量",
)

_add(
    "ai_accounting.kernel.domains.opening.Member",
    agreement_id="借款期初成员实际采用的借款合同引用，其余明细按模型允许保持空",
    kind="该期初清单成员的明确期初明细业务类型",
    subject_id="内核登记的业务主题身份；与事实版本和计算版本标识分别保留",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningAsset",
    accumulated_fen="期初卡片按原卡片规则已经消耗的累计折旧或摊销，整数分",
    asset_id="公司资产对象身份，用于衔接取得、启用、消耗、处置及期初卡片",
    asset_type="资产的核算类别，区分固定资产与无形资产的科目和消耗起算规则",
    benefit_area="资产消耗受益的管理、销售或服务成本类别，用于封闭核算模板归类",
    completed_months="建账前原资产已经完成消耗的月数，必须与启用日期和卡片规则一致",
    cost_fen="本项有依据的取得或期初账面成本，以整数分保存，不以当前余额倒推",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
    residual_fen="明确预计残值，作为可消耗成本的扣减项；无形资产须满足零残值规则",
    rounding_policy="资产卡片的分摊舍入规则，区分向下取整尾月补余和逐卡四舍五入",
    useful_life_months="明确的预计使用或摊销总月数，为资产消耗卡片的计算参数",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningBank",
    balance_fen="建账边界该正式资金账户明确的期初余额，整数分；不生成原历史收付",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningCash",
    balance_fen="建账边界该正式资金账户明确的期初余额，整数分；不生成原历史收付",
    cash_account_id="明确登记的现金资金账户对象引用，与银行或平台账户分别核算",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningEquity",
    balance_fen="建账边界该权益类别的明确有符号期初余额，整数分",
    equity_kind="期初权益明细的经济性质，资本、公积或未分配利润等允许类别",
    holder_or_basis_id="期初权益的明确持有人或依据标识，保留原权益归属而不虚构资金投入",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningLoan",
    accrued_interest_fen="建账前已经计提但尚未清偿的借款利息，整数分；不补造历史计息凭证",
    agreement_id="已登记借款合同的业务主题引用，用于复用出借人、利率及计息规则",
    lender_id="提供借款的实际出借人对象身份，用于本金、利息义务的归属",
    loan_term="借款的明确期限分类，区分短期与长期核算，不从到期日自动推定",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
    principal_fen="逐笔借款的实际取得或期初未偿本金，以整数分保存；不是借款额度",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningMoneyFund",
    classification="明确确认该货币基金可随时赎回且持有意图不超过一年，不从产品名称推定",
    cost_basis="明确采用可复核的原始账面成本依据，不按收益率或账户余额反推",
    cost_fen="本项有依据的取得或期初账面成本，以整数分保存，不以当前余额倒推",
    fund_id="所投资的具体基金产品对象身份；不是银行或支付平台账户身份",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningObligation",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    nature="期初往来义务的经济性质，决定应收应付科目、现金流类别和债权人归属",
    outstanding_fen="建账前业务在期初尚未清偿的明确债权或债务金额，整数分",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningPackage",
    completeness_confirmed="明确确认期初清单已完整列出所有该建账边界需要接续的明细",
    counts="负责人明确期初各明细类别应有数量，与精确成员清单逐类比对",
    members="期初包实际采用的逐业务类型和主题清单，按需要额外指定借款合同，不由库内现有行推定完整",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningPayrollPayable",
    component="期初未付薪酬的具体组成：净薪、扣税、个人或公司社保公积金等允许类别",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    outstanding_fen="建账前业务在期初尚未清偿的明确债权或债务金额，整数分",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
    recipient_id="所分配原业务义务的真实收款人对象身份；汇总代付中不能用批次标识替代人员",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningPayrollState",
    cumulative_employee_contributions_fen="截至through_period已采用的累计个人社保和公积金扣除，整数分",
    cumulative_income_fen="截至through_period该纳税年度已计入累计预扣的工资收入，整数分",
    cumulative_other_legal_deduction_fen="截至through_period已采用的累计其他依法扣除，整数分",
    cumulative_special_additional_deduction_fen="截至through_period已采用的累计专项附加扣除，整数分",
    cumulative_standard_deduction_fen="截至through_period已采用的累计基本减除费用，整数分",
    cumulative_tax_exempt_income_fen="截至through_period同年度已采用的累计免税收入，整数分",
    cumulative_tax_relief_fen="截至through_period已采用的累计税额减免，整数分",
    cumulative_withheld_tax_fen="截至through_period实际已扣缴的累计工资税额，整数分",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
    separate_method_already_used="明确本年度一次性奖金是否已使用单独计税机会，后续选择须遵守次数限制",
    through_period="期初累计工资状态已经包含到的截止月，后续累计从其后开始",
)

_add(
    "ai_accounting.kernel.domains.opening.OpeningTax",
    authority_id="期初税款债权或债务对应的征收机关对象身份",
    balance_kind="期初税款是应缴负债还是可退债权，不能仅凭金额正负推定",
    outstanding_fen="建账前业务在期初尚未清偿的明确债权或债务金额，整数分",
    package_id="明确所属的期初接续包主题引用，用于逐项清单、完整性和建账边界核验",
    tax_kind="期初未清税款的明确税费类别，按固定税种科目解释",
)

_add(
    "ai_accounting.kernel.domains.payroll.AnnualBonus",
    bonus_fen="本次一次性奖金的明确税前金额，整数分",
    bonus_policy_id="明确引用一次性奖金政策主题，按收入日校验生效及税率分档",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    expense_class="明确的成本费用性质，按内核封闭模板选择核算科目及报表归类",
    income_tax_policy_id="一次性奖金并入工资比较所引用的累计工资所得税政策主题",
    regular_payroll_id="奖金并入或比较计税所依据的同月正式工资业务主题引用",
    tax_method="一次性奖金明确选择的单独计税或并入工资方法；内核只按该选择核算",
)

_add(
    "ai_accounting.kernel.domains.payroll.AnnualBonusOpeningUsage",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    separate_method_already_used="明确本年度一次性奖金是否已使用单独计税机会，后续选择须遵守次数限制",
)

_add(
    "ai_accounting.kernel.domains.payroll.AnnualBonusPolicy",
    brackets="一次性奖金单独计税的有序税率档，按奖金除以12后的金额选择档位",
    effective_to="明确适用区间的终点，包含该日或月；允许空时表示未声明终点",
    primary_source_url="本政策的首要官方来源网址，用于版本来源核验",
    version="明确采用的政策版本标识，与内核事实修订序号相互独立",
)

_add(
    "ai_accounting.kernel.domains.payroll.ContributionActualItem",
    base_kind="缴费项目采用社保或公积金基数的类别",
    code="缴费政策或实际核定中的项目代码，用于逐项匹配缴费项目",
    employee_amount_fen="该缴费项目实际核定的个人承担金额，整数分",
    employer_amount_fen="该缴费项目实际核定的公司承担金额，整数分",
    state="该缴费项目实际是否已申报核定；not_declared时个人和公司金额必须为零",
)

_add(
    "ai_accounting.kernel.domains.payroll.ContributionRuleFact",
    base_kind="缴费项目采用社保或公积金基数的类别",
    code="缴费政策或实际核定中的项目代码，用于逐项匹配缴费项目",
    employee_rate="该缴费项目个人承担比例，精确十进制字符串，0至1",
    employer_rate="该缴费项目公司承担比例，精确十进制字符串，0至1",
    enabled="明确该缴费政策项目是否参与计算，不从费率为零推定停用",
    maximum_base_fen="该缴费项目适用的基数上限，整数分",
    minimum_base_fen="该缴费项目适用的基数下限，整数分",
    rounding="缴费项目金额按分计算的舍入方式，半入、向下或向上",
)

_add(
    "ai_accounting.kernel.domains.payroll.LaborAccrual",
    expense_class="明确的成本费用性质，按内核封闭模板选择核算科目及报表归类",
    person_id="取得劳务报酬的公司目录人员身份，不意味着与公司存在工资任职关系",
)

_add(
    "ai_accounting.kernel.domains.payroll.LaborIncomeTaxPolicy",
    brackets="政策明确的有序税率分档及速算扣除数；各档边界须满足政策模型约束",
    effective_to="明确适用区间的终点，包含该日或月；允许空时表示未声明终点",
    fixed_expense_deduction_fen="较小劳务收入按政策允许扣除的定额费用，整数分",
    large_payment_expense_rate="较大劳务收入按比例扣除费用的精确十进制比例",
    primary_source_url="本政策的首要官方来源网址，用于版本来源核验",
    small_payment_threshold_fen="劳务预扣政策中采用定额费用扣除的收入门槛，整数分",
    version="明确采用的政策版本标识，与内核事实修订序号相互独立",
)

_add(
    "ai_accounting.kernel.domains.payroll.LaborRemuneration",
    commission_base_fen="佣金计价所依据的明确业务基数金额，整数分",
    commission_rate_ppm="佣金率，以百万分之一整数表示；按约定基数确定税前报酬",
    expense_class="明确的成本费用性质，按内核封闭模板选择核算科目及报表归类",
    fixed_fee_fen="按固定费用计价明确约定的劳务税前金额，整数分",
    gross_payment_id="用于本次劳务计税核对的正式毛额付款主题引用",
    gross_payment_kind="用于本次劳务计税核对的实际毛额付款业务类型",
    person_id="取得劳务报酬的公司目录人员身份，不意味着与公司存在工资任职关系",
    policy_id="本次劳务所得税预扣采用的明确劳务税政策主题，按实际收入日核验生效",
    recipient_tax_status="明确劳务收款人为普通居民个人，当前仅支持ordinary_resident",
    remuneration_method="劳务报酬的明确计价方法，固定费用或按基数佣金，不以付款反推约定",
    withholding_method=(
        "明确本次按扣税后净额支付net_after_withholding，或已毛额支付未扣税gross_paid_without_withholding；后"
        "者须引用真实毛额付款，不推定税款已缴"
    ),
)

_add(
    "ai_accounting.kernel.domains.payroll.Payroll",
    accounting_gross_salary_fen="本月按权责发生确认的工资总额，作为工资成本和应付职工薪酬依据",
    contribution_basis="本月个人和公司缴费金额的依据选择：按政策计算或采用实际核定缴费",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    expense_class="明确的成本费用性质，按内核封闭模板选择核算科目及报表归类",
    other_legal_deduction_fen="本税期新增的其他依法扣除，与基本减除、个人缴费及专项附加分别计算",
    special_additional_deduction_fen="本税期新增专项附加扣除，按累计预扣算法接入年度累计",
    tax_exempt_income_fen="本税期工资中明确的免税收入，用于计算累计应纳税所得额",
    tax_relief_fen="本税期明确新增的税额减免，不是收入扣除或预估税额",
    tax_reported_salary_fen="本税期用于累计预扣的工资申报收入，与工资核算总额独立确认",
)

_add(
    "ai_accounting.kernel.domains.payroll.PayrollBounded",
    accounting_gross_salary_fen="本月按权责发生确认的工资总额，作为工资成本和应付职工薪酬依据",
    contribution_basis="本月个人和公司缴费金额的依据选择：按政策计算或采用实际核定缴费",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    expense_class="明确的成本费用性质，按内核封闭模板选择核算科目及报表归类",
    tax_reported_salary_fen="本税期用于累计预扣的工资申报收入，与工资核算总额独立确认",
)

_add(
    "ai_accounting.kernel.domains.payroll.PayrollContributionActual",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    items="本人员本月逐项实际社保公积金核定状态及个人、公司承担金额",
)

_add(
    "ai_accounting.kernel.domains.payroll.PayrollContributionPolicy",
    effective_to="明确适用区间的终点，包含该日或月；允许空时表示未声明终点",
    jurisdiction="该社保公积金政策的明确适用地区标识",
    primary_source_url="本政策的首要官方来源网址，用于版本来源核验",
    rules="逐项社保公积金政策规则，明确代码、基数类别、个人与公司费率、上下限、舍入和启用状态",
    version="明确采用的政策版本标识，与内核事实修订序号相互独立",
)

_add(
    "ai_accounting.kernel.domains.payroll.PayrollFirstWageTreatment",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
)

_add(
    "ai_accounting.kernel.domains.payroll.PayrollIncomeTaxPolicy",
    brackets="政策明确的有序税率分档及速算扣除数；各档边界须满足政策模型约束",
    effective_to="明确适用区间的终点，包含该日或月；允许空时表示未声明终点",
    legal_basis_source_url="累计预扣方法法律依据的首要官方来源网址，与具体政策来源分开保存",
    monthly_standard_deduction_fen="累计预扣政策规定的每月基本减除费用，整数分",
    primary_source_url="本政策的首要官方来源网址，用于版本来源核验",
    version="明确采用的政策版本标识，与内核事实修订序号相互独立",
)

_add(
    "ai_accounting.kernel.domains.payroll.PayrollOpeningState",
    cumulative_employee_contributions_fen="截至through_period已采用的累计个人社保和公积金扣除，整数分",
    cumulative_income_fen="截至through_period该纳税年度已计入累计预扣的工资收入，整数分",
    cumulative_other_legal_deduction_fen="截至through_period已采用的累计其他依法扣除，整数分",
    cumulative_special_additional_deduction_fen="截至through_period已采用的累计专项附加扣除，整数分",
    cumulative_standard_deduction_fen="截至through_period已采用的累计基本减除费用，整数分",
    cumulative_tax_exempt_income_fen="截至through_period同年度已采用的累计免税收入，整数分",
    cumulative_tax_relief_fen="截至through_period已采用的累计税额减免，整数分",
    cumulative_withheld_tax_fen="截至through_period实际已扣缴的累计工资税额，整数分",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    through_period="期初累计工资状态已经包含到的截止月，后续累计从其后开始",
)

_add(
    "ai_accounting.kernel.domains.payroll.PayrollProfile",
    contribution_shortfall="个人工资不足承担缴费时的明确处理：阻断或由公司承担，影响实际负担分配",
    effective_to="明确适用区间的终点，包含该日或月；允许空时表示未声明终点",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    housing_fund_base_fen="有依据的住房公积金缴费基数，与社保基数独立确认",
    housing_fund_participating="明确的公积金参与状态，决定是否需要基数和对应缴费项目",
    social_insurance_base_fen="有依据的社保缴费基数，供明确参保项目与政策上下限计算使用",
    social_insurance_participating="明确的社保参保状态，决定是否需要基数和适用社保缴费项目",
)

_add(
    "ai_accounting.kernel.domains.payroll.PayrollWithholdingActual",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    withholding_confirmed="明确确认该工资实际扣税记录成立，不把预计或拟申报税额当已扣",
)

_add(
    "ai_accounting.kernel.domains.payroll.TaxBracketFact",
    quick_deduction_fen="该税率档的速算扣除金额，整数分",
    rate="该税率档的精确十进制税率比例，0至1，不是百分数单位",
    upper_bound_fen="该税率档的上界，整数分；空表示无上界的最后一档",
)

_add(
    "ai_accounting.kernel.domains.payroll_reserve_payment.PayrollNetAllocation",
    amount_fen="本次实际收付款明确分配给这项原义务的整数分金额，不等于原义务全额",
    obligation="原业务计算产生的义务名称，用于确定本次清偿的具体债权或债务",
    recipient_id="所分配原业务义务的真实收款人对象身份；汇总代付中不能用批次标识替代人员",
    source_id="本次实际付款所清偿的原义务业务主题，与source_kind和obligation共同定位义务",
    source_kind="明确引用来源的业务类型，必须与source_id解析的业务一致",
)

_add(
    "ai_accounting.kernel.domains.payroll_reserve_payment.PayrollReservePayment",
    actual_return_date="有确切依据的实际返入备用金日期，须在付款之后并属于明确返款月",
    allocations="本次实际收付对已采用业务义务的逐项分配，合计须满足该付款模型的总额约束",
    amount_fen="完整工资组实际从银行流出的毛额，整数分；必须等于净薪分配加明确实际退入备用金金额",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    complete_group_confirmed="明确完整工资组已按银行毛额实际支付，不将个人净薪分配当逐人银行观察",
    counterparty_id="固定的工资完整组技术标识payroll-group，仅表示汇总代付批次，真实收款人由逐项recipient_id保存",
    direction="实际资金相对所选公司账户的流入或流出方向，不接收任意借贷方向",
    payment_method="实际支付方式，区分单一具名交易方付款与有逐项收款人的银行汇总代付",
    return_confirmed="明确确认备用金部分已实际返入，不把待返余额当已发生费用",
    return_period="明确实际返入备用金的月份，当前工资毛额处理要求与付款月一致",
)

_add(
    "ai_accounting.kernel.domains.platforms.BankPlatformTransfer",
    amount_fen="本次公司银行与平台账户间实际转移的同额资金，整数分；按direction核对平台原行",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    direction="bank_to_platform为银行划入平台，platform_to_bank为平台划回银行；平台原行方向须与之一致",
    movement_ids="本次处理实际消耗的平台原始变动主题引用，校验账户、日期、方向、金额与重复采用",
)

_add(
    "ai_accounting.kernel.domains.platforms.PlatformExpenseConfirmation",
    confirmed_amount_fen="有依据确认的平台费用净额，整数分，须与采用流出及退回原行相符",
    expense_class="明确的成本费用性质，按内核封闭模板选择核算科目及报表归类",
    outgoing_movement_ids="本次平台费用确认采用的原始流出变动主题清单",
    platform_account_id="明确登记的支付平台账户对象引用；平台标识从正式账户身份链读取",
    returned_movement_ids="同一费用退回采用的原始流入变动主题清单，须与对应费用处理核对",
    treatment_confirmed="明确确认这些原始平台变动属于所选择的费用处理，不从流水摘要推定",
)

_add(
    "ai_accounting.kernel.domains.platforms.PlatformFunding",
    amount_fen="出资人本次实际投入公司所选银行、现金或平台账户的资金金额，整数分；性质由funding_kind明确",
    funding_kind="资金投入的性质，区分资本投入与负责人借款，不由收款金额推断",
    movement_ids="本次处理实际消耗的平台原始变动主题引用，校验账户、日期、方向、金额与重复采用",
    owner_id="提供资金的出资人或公司负责人对象引用，结合funding_kind区分投入权益和代垫借款",
)

_add(
    "ai_accounting.kernel.domains.platforms.PlatformMovement",
    amount_fen="平台原始变动行明确的非负交易金额，整数分；实际流入或流出由direction保留",
    direction="实际资金相对所选公司账户的流入或流出方向，不接收任意借贷方向",
    platform_account_id="明确登记的支付平台账户对象引用；平台标识从正式账户身份链读取",
    source_evidence_digest="所引用原始交易资料的已登记内容摘要，不代替业务解释或处置确认",
    source_location="原始资料中定位该交易的行、单元格或片段位置",
    transaction_reference="原始交易记录明确提供的交易流水参考号，不制造临时交易编号",
)

_add(
    "ai_accounting.kernel.domains.platforms.PlatformPayment",
    allocations="本次实际收付对已采用业务义务的逐项分配，合计须满足该付款模型的总额约束",
    amount_fen="本次所选资金账户实际流入或流出的总额，整数分；须由逐项原义务分配完整解释",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    direction="实际资金相对所选公司账户的流入或流出方向，不接收任意借贷方向",
    movement_ids="本次处理实际消耗的平台原始变动主题引用，校验账户、日期、方向、金额与重复采用",
    payment_method="实际支付方式，区分单一具名交易方付款与有逐项收款人的银行汇总代付",
)

_add(
    "ai_accounting.kernel.domains.taxes.IncomeTaxAssessment",
    assessment_basis="企业所得税核定金额的明确来源或核定方式，不引入利润乘税率的推断",
    cumulative_assessed_fen="当年截至本期明确已核定的累计企业所得税额，整数分；不是以利润推算税额",
    year="本项企业所得税累计核定所属年度，与核算月份年份核对",
)

_add(
    "ai_accounting.kernel.domains.taxes.SurtaxPolicy",
    education_rate_percent="教育费附加适用的精确十进制百分数税率",
    effective_from="明确适用区间的起点，按该字段声明的实际日或月精度比较",
    effective_to="明确适用区间的终点，包含该日或月；允许空时表示未声明终点",
    local_education_rate_percent="地方教育附加适用的精确十进制百分数税率",
    payable_fraction="附加税费按政策实际应缴的精确比例，体现明确减征而非收入税率",
    source_url="税收政策的首要官方来源网址，与该政策的生效区间一同保存",
    urban_rate_percent="城市维护建设税适用的精确十进制百分数税率",
    version="明确采用的政策版本标识，与内核事实修订序号相互独立",
)

_add(
    "ai_accounting.kernel.domains.taxes.SurtaxPolicyFact",
    policy="对应税费类型的完整明确政策正文，包含版本、官方来源、生效区间和计算参数",
)

_add(
    "ai_accounting.kernel.domains.taxes.TaxAssessment",
    surtax_policy_id="税费核定采用的明确附加税费政策主题引用",
    tax_credit_ids="本次核定采用的税务退货抵减确认主题清单，校验期间及未重复消耗容量",
    vat_policy_id="明确引用的增值税政策主题，计算时核对适用期及当前采用版本",
)

_add(
    "ai_accounting.kernel.domains.taxes.TaxCreditAdvanceReturn",
    original_advance_id="该退款对应的原预收主题，必须与正式退款事实一致",
    return_id="本次税务抵减采用的正式预收退款主题引用",
)

_add(
    "ai_accounting.kernel.domains.taxes.TaxCreditConfirmation",
    authority_confirmed="明确确认征收机关已接受原申报与本次退货抵减处理，须有采用证据",
    confirmation_date="有明确依据的征收机关抵减确认实际日，不以记账或付款日代替",
    confirmation_evidence="已登记的征收机关抵减确认原件单个内容摘要，必须在本事实采用证据中",
    confirmation_reference="本次抵减确认或受理记录的明确参考标识",
    disposition="征收机关明确的退税refund、抵税offset或退抵refund_or_offset结果类型，不据此认定资金实际到账",
    exemption_reversal_fen="原免税业务退货导致本次需冲回的明确免税收益，整数分",
    original_assessment_id="退货抵减采用的原税费核定主题引用，用于确认原纳税范围和可抵减额度",
    original_filing_evidence="已登记的原税费申报原件单个内容摘要，必须在本事实采用证据中，固定原处理依据",
    original_filing_reference="原税费申报记录的明确参考标识，与原申报证据一同保存",
    returns="本次抵减采用的已确认退货及其原收入或原预收逐项引用，不自动猜测抵减来源",
    surtax_credit_fen="本次明确可抵减或可退的附加税费额，整数分，独立于增值税抵减",
    vat_credit_fen="本次明确可抵减或可退的增值税额，整数分，受原核定及退货容量校验",
)

_add(
    "ai_accounting.kernel.domains.taxes.TaxCreditReturn",
    original_sale_id="该退货对应的原服务收入主题，必须与正式退货事实一致",
    return_id="本次税务抵减采用的正式服务退货主题引用",
)

_add(
    "ai_accounting.kernel.domains.taxes.UsedAssetVatPolicy",
    effective_from="明确适用区间的起点，按该字段声明的实际日或月精度比较",
    effective_to="明确适用区间的终点，包含该日或月；允许空时表示未声明终点",
    payable_rate_percent="旧资产增值税实际应缴的精确百分数征收率，与含税价基数换算率独立",
    source_url="税收政策的首要官方来源网址，与该政策的生效区间一同保存",
    tax_base_rate_percent="旧资产增值税计算不含税基数时采用的精确百分数征收率",
    version="明确采用的政策版本标识，与内核事实修订序号相互独立",
)

_add(
    "ai_accounting.kernel.domains.taxes.UsedAssetVatPolicyFact",
    policy="对应税费类型的完整明确政策正文，包含版本、官方来源、生效区间和计算参数",
)

_add(
    "ai_accounting.kernel.domains.taxes.VatPolicy",
    effective_from="明确适用区间的起点，按该字段声明的实际日或月精度比较",
    effective_to="明确适用区间的终点，包含该日或月；允许空时表示未声明终点",
    rate_percent="增值税适用税率，以精确十进制百分数表示",
    source_url="税收政策的首要官方来源网址，与该政策的生效区间一同保存",
    threshold_fen="增值税免税门槛对应的金额，整数分",
    threshold_operator="免税门槛是严格小于还是小于等于，不能自行更改边界",
    version="明确采用的政策版本标识，与内核事实修订序号相互独立",
)

_add(
    "ai_accounting.kernel.domains.taxes.VatPolicyFact",
    policy="对应税费类型的完整明确政策正文，包含版本、官方来源、生效区间和计算参数",
)

_add(
    "ai_accounting.kernel.domains.transactions.Advance",
    amount_fen="本次已成立合同权利义务的预付或预收金额，整数分；按side分别形成资产或负债",
    contractual_obligation_established="明确确认预付或预收的合同权利义务已经成立",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    exemption_eligible="明确确认该项业务符合所引用增值税政策的免税条件；不是按税额为零推定",
    side="预付款或预收款的明确业务方向，决定形成资产或负债",
    tax_obligation_date="税点有实际日依据时保存的日期，须与明确的纳税义务所属月一致",
    tax_obligation_period="有依据的增值税纳税义务所属月，与收入确认月和付款归集月分别保留",
    vat_due_on_advance="明确本次预收是否已触发增值税纳税义务，与收入履约确认分别保存",
    vat_policy_id="明确引用的增值税政策主题，计算时核对适用期及当前采用版本",
)

_add(
    "ai_accounting.kernel.domains.transactions.AdvanceFulfillment",
    advance_id="原预付或预收业务主题引用，用于履约或退款的额度和权利核对",
    exemption_eligible="明确确认该项业务符合所引用增值税政策的免税条件；不是按税额为零推定",
    fulfilled_gross_fen="本次从原预收转为履约收入的含税金额，整数分",
    fulfillment_date="有依据的实际履约日；允许未知日时按明确核算月处理，不能补造实际日",
    tax_obligation_date="税点有实际日依据时保存的日期，须与明确的纳税义务所属月一致",
    tax_obligation_period="有依据的增值税纳税义务所属月，与收入确认月和付款归集月分别保留",
    vat_policy_id="明确引用的增值税政策主题，计算时核对适用期及当前采用版本",
)

_add(
    "ai_accounting.kernel.domains.transactions.AdvanceRefund",
    advance_id="原预付或预收业务主题引用，用于履约或退款的额度和权利核对",
    refund_right_confirmed="明确确认该项退款或可退押金的权利及义务已成立，不以实际回款替代权利依据",
    refunded_gross_fen="本次原预付或预收退款的含税金额，整数分",
)

_add(
    "ai_accounting.kernel.domains.transactions.Allocation",
    amount_fen="本次实际收付款明确分配给这项原义务的整数分金额，不等于原义务全额",
    obligation="原业务计算产生的义务名称，用于确定本次清偿的具体债权或债务",
    recipient_id="所分配原业务义务的真实收款人对象身份；汇总代付中不能用批次标识替代人员",
    source_id="本次实际付款所清偿的原义务业务主题，与source_kind和obligation共同定位义务",
    source_kind="明确引用来源的业务类型，必须与source_id解析的业务一致",
)

_add(
    "ai_accounting.kernel.domains.transactions.BankIncome",
    amount_fen="公司本期明确享有并已从银行收到的收入金额，整数分；与收入性质和权利确认共同核验",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    entitlement_confirmed="明确确认公司享有该项银行入款收入的权利，不把资金到账自动认定收入",
    income_kind="银行入款的明确收入性质，区分利息、补贴及其他内核允许的收入类别",
)

_add(
    "ai_accounting.kernel.domains.transactions.Expense",
    amount_fen="本期公司发生并确认应付的费用总额，整数分；实际付款另由收付事实确认",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    creditor_kind="费用债权人的明确性质，区分供应商、员工或负责人义务",
    expense_class="明确的成本费用性质，按内核封闭模板选择核算科目及报表归类",
)

_add(
    "ai_accounting.kernel.domains.transactions.ExpenseRecovery",
    amount_fen="公司本期明确具有追偿权的原费用收回金额，整数分；实际回款另行核销",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    recovery_right_confirmed="明确确认公司具有追偿或费用收回权，作为确认应收的依据",
    source_expense_id="本次收回所对应的原费用业务主题引用",
)

_add(
    "ai_accounting.kernel.domains.transactions.Funding",
    amount_fen="出资人本次实际投入公司所选银行、现金或平台账户的资金金额，整数分；性质由funding_kind明确",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    funding_kind="资金投入的性质，区分资本投入与负责人借款，不由收款金额推断",
    owner_id="提供资金的出资人或公司负责人对象引用，结合funding_kind区分投入权益和代垫借款",
)

_add(
    "ai_accounting.kernel.domains.transactions.FundsTransfer",
    amount_fen="本次公司两个银行账户间实际转移的同额资金，整数分；不形成收入或费用",
    destination_bank_account_id="银行互转中实际收款账户的已登记对象引用，与付款账户不同",
    source_bank_account_id="银行互转中实际付款账户的已登记对象引用",
)

_add(
    "ai_accounting.kernel.domains.transactions.Overpayment",
    amount_fen="原业务义务已经超额清偿且本次明确可追偿的金额，整数分；受正式付款与原义务容量核验",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    obligation_name="原业务计算产生的具体义务名称，用于识别超额清偿与追偿容量",
    recovery_right_confirmed="明确确认公司具有追偿或费用收回权，作为确认应收的依据",
    source_id="确认超付所关联的原业务主题，按source_kind及obligation_name核对已清偿容量",
    source_kind="明确引用来源的业务类型，必须与source_id解析的业务一致",
)

_add(
    "ai_accounting.kernel.domains.transactions.PassThrough",
    amount_fen="公司承担明确代收代付义务的委托款总额，整数分，不确认为公司收入",
    payer_id="公司受托代收代付的原付款方对象身份",
    rights_and_obligation_confirmed="明确确认公司对委托代收代付承担的权利义务及最终受益人，不按资金流猜测",
)

_add(
    "ai_accounting.kernel.domains.transactions.Payment",
    allocations="本次实际收付对已采用业务义务的逐项分配，合计须满足该付款模型的总额约束",
    amount_fen="本次所选资金账户实际流入或流出的总额，整数分；须由逐项原义务分配完整解释",
    bank_account_id="明确登记的银行资金账户对象引用；正式账号与身份依据从该账户定义和更正链读取",
    direction="实际资金相对所选公司账户的流入或流出方向，不接收任意借贷方向",
    payment_method="实际支付方式，区分单一具名交易方付款与有逐项收款人的银行汇总代付",
)

_add(
    "ai_accounting.kernel.domains.transactions.ProjectCost",
    amount_fen="本期该项目确认的供应商支出金额，整数分；按明确项目性质和资本化条件归集或费用化",
    capitalization_conditions_confirmed="明确确认项目成本满足资本化条件，不能仅凭项目名称或付款决定",
    project_id="成本归集或结转的明确项目对象引用，用于核对同一项目的成本来源",
    project_nature="项目支出的明确经济性质，区分资本性归集与当期费用，不从项目名称推断",
    supplier_id="承担该项采购或项目成本的实际供应商对象身份，用于形成供应商应付义务",
)

_add(
    "ai_accounting.kernel.domains.transactions.ProjectCostSource",
    amount_fen="本次从该原项目成本结转的整数分金额，受原来源未消耗成本容量限制",
    source_id="本次项目结转实际采用的原project_cost或labor_project_cost主题引用",
)

_add(
    "ai_accounting.kernel.domains.transactions.ProjectRelease",
    expense_class="明确的成本费用性质，按内核封闭模板选择核算科目及报表归类",
    project_id="成本归集或结转的明确项目对象引用，用于核对同一项目的成本来源",
    project_sources="本次项目结转或资产取得实际采用的项目成本来源及分配金额",
)

_add(
    "ai_accounting.kernel.domains.transactions.RefundableDeposit",
    amount_fen="本次公司明确享有返还权的可退押金金额，整数分，形成押金债权；实际支付另行登记",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    refund_right_confirmed="明确确认该项退款或可退押金的权利及义务已成立，不以实际回款替代权利依据",
)

_add(
    "ai_accounting.kernel.domains.transactions.ReimbursedDeposit",
    amount_fen="公司本期接受员工已垫付的可退押金金额，整数分，同时确认押金债权及对员工债务",
    company_acceptance_confirmed="明确确认公司已接受该项资产、押金或垫付义务，不把个人采购自动视为公司取得",
    counterparty_id="本项经济业务的明确交易方对象引用，用于义务归属及核销；不是资金账户",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    refund_right_confirmed="明确确认该项退款或可退押金的权利及义务已成立，不以实际回款替代权利依据",
)

_add(
    "ai_accounting.kernel.domains.transactions.SaleReturn",
    credit_note_vat_fen="明确红字或退货税务依据上的增值税额，整数分；不从退款金额猜测",
    customer_id="提供服务或发生退货的实际客户对象身份，用于确认及清偿应收义务",
    returned_gross_fen="本次服务退货或退款确认的原含税价款，整数分",
    sale_id="明确对应的原服务收入业务主题引用",
)

_add(
    "ai_accounting.kernel.domains.transactions.ServiceSale",
    customer_id="提供服务或发生退货的实际客户对象身份，用于确认及清偿应收义务",
    exemption_eligible="明确确认该项业务符合所引用增值税政策的免税条件；不是按税额为零推定",
    gross_fen="本次服务收入明确的含税价款总额，整数分",
    vat_policy_id="明确引用的增值税政策主题，计算时核对适用期及当前采用版本",
)

_add(
    "ai_accounting.kernel.domains.transactions.ServiceTaxPoint",
    declaration_confirmed="明确确认所记税点或税额已经形成申报事实，不把拟申报状态当完成",
    payment_id="税点触发所引用的实际收付款业务主题，只有对应触发模式使用",
    payment_kind="税点触发所引用的实际收付款类型，须与payment_id一致",
    sale_id="明确对应的原服务收入业务主题引用",
    trigger="本次服务纳税义务成立的明确触发事项，不从收付或履约日期自动推定",
)

_add(
    "ai_accounting.kernel.domains.transactions.Settlement",
    first="本次非资金抵销的第一项原业务义务及抵销金额分配",
    offset_right_confirmed="明确确认双方既有义务可依法或依约抵销的权利依据",
    second="本次非资金抵销的第二项原业务义务及同额抵销分配",
    settlement_kind="两项既有义务之间明确的清偿类型，如抵销或预收预付转结，不生成实际资金收付",
)

_add(
    "ai_accounting.kernel.entities.AssetProfile",
    category_label="明确登记的资产管理分类名称，供展示筛选；不改变asset_type核算类别",
    display_name="负责人或资料明确登记的对象显示名称，供查询展示；不取代正式身份",
    display_number="明确登记的管理显示编号，用于查找和展示；不是内核对象ID或自动顺序编号",
    external_identifiers="按明确标识种类保存的外部身份或编号，不与管理显示编号机械合并",
    note="该对象或业务的管理备注；不作为会改变核算处理的正式事实依据",
)

_add(
    "ai_accounting.kernel.entities.FundAccountProfile",
    active="明确登记的资金账户当前管理启用状态，不改变历史资金事实或核算结果",
    display_name="负责人或资料明确登记的对象显示名称，供查询展示；不取代正式身份",
    note="该对象或业务的管理备注；不作为会改变核算处理的正式事实依据",
)

_add(
    "ai_accounting.kernel.entities.NumberedEntityProfile",
    display_name="负责人或资料明确登记的对象显示名称，供查询展示；不取代正式身份",
    display_number="明确登记的管理显示编号，用于查找和展示；不是内核对象ID或自动顺序编号",
    external_identifiers="按明确标识种类保存的外部身份或编号，不与管理显示编号机械合并",
    note="该对象或业务的管理备注；不作为会改变核算处理的正式事实依据",
)

_add(
    "ai_accounting.kernel.entities.OrganizationProfile",
    display_name="负责人或资料明确登记的对象显示名称，供查询展示；不取代正式身份",
    display_number="明确登记的管理显示编号，用于查找和展示；不是内核对象ID或自动顺序编号",
    external_identifiers="按明确标识种类保存的外部身份或编号，不与管理显示编号机械合并",
    note="该对象或业务的管理备注；不作为会改变核算处理的正式事实依据",
)

_add(
    "ai_accounting.kernel.entities.PersonProfile",
    display_name="负责人或资料明确登记的对象显示名称，供查询展示；不取代正式身份",
    display_number="明确登记的管理显示编号，用于查找和展示；不是内核对象ID或自动顺序编号",
    employment_end="有任职依据的管理离职月或实际日；不自动推定工资处理截至日",
    employment_start="有任职依据的管理入职月或实际日；不是工资扣缴关系起点",
    employment_status="明确登记的管理在册状态；查询历史人员范围使用此值和管理任职日期，不从当前employment_state反推历史",
    external_identifiers="按明确标识种类保存的外部身份或编号，不与管理显示编号机械合并",
    note="该对象或业务的管理备注；不作为会改变核算处理的正式事实依据",
)

_add(
    "ai_accounting.kernel.fund_accounts.fund_profile_models.<locals>.BankAccountProfile",
    active="明确登记的资金账户当前管理启用状态，不改变历史资金事实或核算结果",
    display_name="负责人或资料明确登记的对象显示名称，供查询展示；不取代正式身份",
    note="该对象或业务的管理备注；不作为会改变核算处理的正式事实依据",
)

_add(
    "ai_accounting.kernel.fund_accounts.fund_profile_models.<locals>.CashAccountProfile",
    active="明确登记的资金账户当前管理启用状态，不改变历史资金事实或核算结果",
    note="该对象或业务的管理备注；不作为会改变核算处理的正式事实依据",
)

_add(
    "ai_accounting.kernel.fund_accounts.fund_profile_models.<locals>.PlatformAccountProfile",
    active="明确登记的资金账户当前管理启用状态，不改变历史资金事实或核算结果",
    display_name="负责人或资料明确登记的对象显示名称，供查询展示；不取代正式身份",
    note="该对象或业务的管理备注；不作为会改变核算处理的正式事实依据",
)

_add(
    "ai_accounting.kernel.identity_corrections.EntityAssignment",
    entity_id="明确登记的公司目录对象身份引用；不能用名称、编号或另一个业务主题替代",
    path="原事实允许更正的明确对象身份字段路径，不允许任意经济字段修改",
)

_add(
    "ai_accounting.kernel.identity_corrections.OpeningBasisCorrection",
    members="本次期初核算基础调整实际采用的精确身份绑定及包计算成员，用于配对扣回或恢复",
)

_add(
    "ai_accounting.kernel.identity_corrections.OpeningBasisMember",
    action="本次期初基础调整对原采用依据执行替代扣回或恢复的明确动作",
    binding_fact_id="本次期初基础调整采用的精确身份绑定事实版本",
    binding_subject_id="本次期初核算基础更正采用的身份绑定主题引用",
    package_calculation_id="期初身份更正精确采用的期初清单计算，固定原明细及采用范围",
)

_add(
    "ai_accounting.kernel.identity_corrections.OpeningIdentityBinding",
    adopted_scopes="身份更正后采用的精确查询作用域集合，用于定位更正后的期初义务",
    assignments="只对原期初事实明确身份路径登记的新对象引用，不更改冻结核算金额",
    operation="期初身份绑定的明确操作，重新指派身份或替代原来源，二者依据不同",
    original_scopes="原期初明细实际声明的查询作用域集合，保留更正前来源定位与历史依据",
    package_calculation_id="期初身份更正精确采用的期初清单计算，固定原明细及采用范围",
    replacement_package_calculation_id="替代操作实际采用的新期初清单精确计算版本",
    replacement_source_calculation_id="替代操作实际采用的新期初明细精确计算版本",
    replacement_source_fact_id="替代操作实际采用的新期初明细精确事实版本",
    source_calculation_id="期初身份更正精确采用的原明细计算版本",
    source_fact_id="被引用来源的不可变事实版本标识，防止当前来源修订替换原采用依据",
    source_kind="精确绑定原期初明细的业务类型，不是允许任意字段更正的模式",
    source_subject_id="期初身份更正绑定的原期初明细主题身份",
)

_add(
    "ai_accounting.kernel.materials.Column",
    column="原表中的明确列字母，用于解析或选定单元格范围",
    label="原列或管理展示的辅助说明，不替代列角色、金额方向或正式核算分类",
    role="原列的明确角色：金额、核算期间或背景上下文，不从列名自动猜测",
)

_add(
    "ai_accounting.kernel.materials.ControlTotal",
    location="资料中的准确原行、单元格、控制合计或片段位置，用于覆盖及来源核对",
    locations="该原控制合计明确覆盖的离散原行或片段位置清单",
    ranges="该原控制合计明确覆盖的表格单元格范围清单",
    scope="控制合计覆盖范围，明确指定原明细或该列全体明细；二者不得混用",
)

_add(
    "ai_accounting.kernel.materials.MaterialGroupMember",
    amount_fen="该原位置本次加入联合组的有符号整数分金额，受原行尚未覆盖容量限制",
    location="资料中的准确原行、单元格、控制合计或片段位置，用于覆盖及来源核对",
)

_add(
    "ai_accounting.kernel.materials.MaterialGroupResolution",
    basis_evidence_digest="已登记的期间分配或联合确认依据原件摘要",
    basis_location="分配或联合确认依据在原件中的准确位置",
    group_amount_fen="联合组明确覆盖的原资料金额合计，整数分；须与成员及分配总额一致",
    joint_basis_confirmed="明确这些原行可以联合采用为一个业务依据，不凭金额相等自动组并",
    links="本联合组实际采用的精确正式计算及金额字段分配，不复制计算对应主题、类型或期间",
    members="本联合组实际覆盖的原资料准确位置及每项分配金额，保持原行独立金额容量",
    reason="该资料处置或联合组确认的明确理由，随不可变处置版本保留",
    source_fact_id="被引用来源的不可变事实版本标识，防止当前来源修订替换原采用依据",
    source_id="已登记原资料的material_source_v2主题引用，与source_fact_id精确绑定当时解析依据",
)

_add(
    "ai_accounting.kernel.materials.MaterialLink",
    amount_fen="负责人本次明确分配给所选精确计算金额字段的有符号整数分金额；不是被引用业务总额的副本",
    amount_field="精确计算对应事实或结果中的明确金额路径，格式fact.*_fen或result.*_fen",
    calculation_id="不可变计算标识，精确固定采用事实与计算结果；不替换为当前计算",
)

_add(
    "ai_accounting.kernel.materials.MaterialPeriodAllocation",
    entries="对准确原行范围或片段逐项确认的核算期间及原始依据，不改变原件金额",
    source_fact_id="被引用来源的不可变事实版本标识，防止当前来源修订替换原采用依据",
    source_id="已登记原资料的material_source_v2主题引用，与source_fact_id精确绑定当时解析依据",
)

_add(
    "ai_accounting.kernel.materials.MaterialRange",
    column="原表中的明确列字母，用于解析或选定单元格范围",
    first_row="原表选定范围的起始行号，包含该行",
    last_row="原表选定范围的结束行号，包含该行且不得早于起始行",
    sheet="原表中的明确工作表名称，用于定位所选择的原行范围",
)

_add(
    "ai_accounting.kernel.materials.MaterialResolution",
    amount_fen="本次处置占用的原资料行金额，整数分；允许分次分配但须满足原行完整金额容量",
    duplicate_location="另一份资料中与本行重复的准确原位置",
    duplicate_source_id="认定重复的另一份已登记资料主题引用，不因摘要相似自动认定重复",
    location="资料中的准确原行、单元格、控制合计或片段位置，用于覆盖及来源核对",
    non_accounting_reason="明确原行无需核算的允许原因，不能以备注或文件已引用绕过处置",
    reason="该资料处置或联合组确认的明确理由，随不可变处置版本保留",
    recognition_period="原资料或明确处置确认的核算所属月；未知保持空，不以收件月替代",
    source_fact_id="被引用来源的不可变事实版本标识，防止当前来源修订替换原采用依据",
    source_id="已登记原资料的material_source_v2主题引用，与source_fact_id精确绑定当时解析依据",
    treatment="原行的明确处置：采用业务、非核算或重复等允许类别，不能用文件已引用代替处置",
)

_add(
    "ai_accounting.kernel.materials.MaterialSource",
    category="资料所属业务清单类别，用于关账及完整性检查，不能代替每一原行处置",
    evidence_digest="已保全原件的内容摘要引用，须指向实际登记的不可变证据",
    purpose="资料明确作为待逐行业务原件还是仅支持证据；仅支持用途必须另外说明",
    specification="原资料完整解析规格，明确列、表头、合计、片段及控制合计，不提供推断会计解释",
    supporting_purpose="资料仅为支持证据时的明确用途说明，不把文件引用当业务原行已处置",
)

_add(
    "ai_accounting.kernel.materials.Passage",
    amount_fen="该原始片段明确列示的有符号金额，整数分；未知金额保留空",
    excerpt="原始资料逐项引用的原文片段，不以摘要猜测业务事实",
    location="资料中的准确原行、单元格、控制合计或片段位置，用于覆盖及来源核对",
    page="原始资料准确页码，从1开始，定位片段证据",
    recognition_period="原资料或明确处置确认的核算所属月；未知保持空，不以收件月替代",
)

_add(
    "ai_accounting.kernel.materials.PeriodAllocationEntry",
    basis="原行所属期依据为原行明确期间、负责人有证据确认、未知或资料整体期间；unknown时期间必须为空",
    basis_evidence_digest="已登记的期间分配或联合确认依据原件摘要",
    basis_excerpt="有依据的期间确认原文片段，与摘要及位置共同保留",
    basis_location="分配或联合确认依据在原件中的准确位置",
    column="原表中的明确列字母，用于解析或选定单元格范围",
    first_row="原表选定范围的起始行号，包含该行",
    last_row="原表选定范围的结束行号，包含该行且不得早于起始行",
    location="资料中的准确原行、单元格、控制合计或片段位置，用于覆盖及来源核对",
    recognition_period="原资料或明确处置确认的核算所属月；未知保持空，不以收件月替代",
    sheet="原表中的明确工作表名称，用于定位所选择的原行范围",
)

_add(
    "ai_accounting.kernel.materials.Specification",
    all_pages_reviewed="明确确认整份资料各页已检查，不以登记少数片段代替完整检查",
    columns="原表全表通用的明确列角色规格，金额、期间及背景列分别声明",
    controls="原表控制合计及其覆盖原明细位置或范围，用于金额完整性校验",
    format="原始资料实际格式，决定使用CSV、Excel、PDF、图像或文本解析规格",
    header_rows="各工作表需要跳过的原表头行数，非负整数",
    passages="PDF、图像或文本中逐项明确的位置、原文、金额和期间片段",
    sheet_columns="原Excel各工作表独立的列角色规格，不用同名列自动共享解释",
    total_rows="各工作表明确的原合计行号；不把合计再次计为业务明细",
)

_add(
    "ai_accounting.kernel.payroll_preparation.PayrollChangeNotice",
    changed_fields="该人员本月需要变更确认的工资字段清单，不用通知替代新工资正文",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
)

_add(
    "ai_accounting.kernel.payroll_preparation.PayrollNoChange",
    employees="本月每名适用人员沿用的上期已采用工资精确版本清单；人员身份从原工资解析，不另复制",
)

_add(
    "ai_accounting.kernel.payroll_tax_declarations.PayrollDisbursementBasis",
    declaration_fact_id="本次代发采用的实际申报精确事实版本，防止当前申报改动覆盖原依据",
    declaration_id="本次代发依据采用的实际申报主题引用",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    payroll_id="采用申报税额确定代发净额的正式工资主题引用",
    payroll_kind="代发基础引用的明确工资事实类型，普通或有税收入实际日的工资",
    use_declared_tax_for_disbursement="明确允许采用实际申报税额确定代发净额；不改写工资成本或拟计算税额",
)

_add(
    "ai_accounting.kernel.payroll_tax_declarations.PayrollTaxDeclarationActual",
    declaration_confirmed="明确确认所记税点或税额已经形成申报事实，不把拟申报状态当完成",
    declared_tax_fen="有实际申报或受理依据的该税期申报税额，整数分；不等于自动计算的预扣额",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    income_category="已实际申报的所得类别，用于匹配对应工资或一次性奖金的扣税依据",
)

_add(
    "ai_accounting.kernel.reports.CashDetail",
    amount_fen="原凭证行归入该现金流量表类别的明确正整数分份额",
    category="现金流量表固定模板的允许行类别编号，与核算科目及原行方向分别核验",
    line_no="所引用不可变凭证版本中的原行序号，不能指向任意新增分录",
)

_add(
    "ai_accounting.kernel.reports.ContinuationReportProfile",
    accounting_standard="负责人明确采用的小企业会计准则标识，决定报表模板与处理范围",
    company_name="用于报表抬头的明确企业名称，不反推公司目录身份或纳税人编号",
    opening_package_id="已确认期初接续包引用；报表接续不能以当前业务推定期初",
)

_add(
    "ai_accounting.kernel.reports.CounterpartyDetail",
    counterparty_id="既有凭证行在报表债权债务汇总中明确归属的交易方对象，不改写原业务身份",
    line_no="所引用不可变凭证版本中的原行序号，不能指向任意新增分录",
)

_add(
    "ai_accounting.kernel.reports.PriorBalanceRow",
    beginning_fen="该资产负债表模板行有依据的年初余额，允许有符号整数分",
    line="固定报表模板中的行编号，须按对应资产负债、利润或现金流模板解释",
)

_add(
    "ai_accounting.kernel.reports.PriorFlowRow",
    line="固定报表模板中的行编号，须按对应资产负债、利润或现金流模板解释",
    quarter_to_date_fen="该利润或现金流报表行在建账前已发生的本季度累计金额，整数分",
    year_to_date_fen="该利润或现金流报表行在建账前已发生的本年度累计金额，整数分",
)

_add(
    "ai_accounting.kernel.reports.ProfitDetail",
    amount_fen="原凭证行归入这项利润表明细的正整数分份额，校验原行金额容量",
    detail_code="利润表允许的明细类别代码，如利息、管理费用或税金细分",
    line_no="所引用不可变凭证版本中的原行序号，不能指向任意新增分录",
)

_add(
    "ai_accounting.kernel.reports.ReportCarryForward",
    opening_package_id="已确认期初接续包引用；报表接续不能以当前业务推定期初",
    prior_cash="建账前已确认现金流量表各行的本季及本年累计接续值；不虚构历史收付",
    prior_profit="建账前已确认利润表各行的本季及本年累计接续值；不作为入账金额",
    year_beginning_balance="完整固定报表模板的年初余额行，来自建账前已确认报表；不生成分录",
)

_add(
    "ai_accounting.kernel.reports.ReportClassification",
    cash_details="原凭证行在现金流量表允许类别中的明确金额分配",
    counterparties="原凭证行用于报表债权债务归集的明确交易方引用",
    profit_details="原凭证行在利润表允许明细中的明确金额分配",
    voucher_version_id="既有不可变凭证版本引用；分类事实不能改变原科目或借贷金额",
)

_add(
    "ai_accounting.kernel.reports.ReportIncomeTaxConfirmation",
    calculation_id="assessed处理采用的精确已发布所得税核定计算，零额或不适用时必须为空",
    cumulative_assessed_fen="当年截至本期明确已核定的累计企业所得税额，整数分；不是以利润推算税额",
    explanation="负责人明确本季所得税报表处理依据的非空说明，随确认事实保存",
    treatment="明确本季所得税报表处理为不适用、已确认零额或采用正式核定金额，不以缺少核定事实推定为零",
)

_add(
    "ai_accounting.kernel.reports.ReportProfile",
    accounting_standard="负责人明确采用的小企业会计准则标识，决定报表模板与处理范围",
    company_name="用于报表抬头的明确企业名称，不反推公司目录身份或纳税人编号",
    newly_established_zero_opening_confirmed="明确企业新设且建账期初为零，不从缺少期初记录自动认定",
)

_add(
    "ai_accounting.kernel.tax_import.TaxImportDetails",
    cumulative_personal_pension_fen="税务导入所需个人养老金的年度累计扣除，整数分",
    cumulative_special_fen="税务模板各专项附加扣除列的明确年度累计金额字典，所有固定列包括零必须提供",
    current_other_fen="税务模板各其他扣除列的明确本期金额字典，所有固定列包括零必须提供",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    payroll_result_digest="这些税務导入明细精确绑定的正式工资结果摘要，工资重算后须重新核验",
    remark="税务导入模板的明确备注文本，不代替计算或申报依据",
    tax_relief_fen="税务导入文件本税期明确的税额减免字段，与协定减免合计须等于绑定正式工资的本期税额减免",
    treaty_relief_fen="有依据的税收协定减免税额，整数分；不是一般工资扣除",
)

_add(
    "ai_accounting.kernel.tax_import.TaxImportIdentity",
    document_number="税务申报所用证件号码原文，不猜测或由对象名称补造",
    document_type="税务客户端模板允许的明确证件类型，不能从号码格式推定",
    employee_code="税务客户端需要的明确人员编码，不等于本系统员工对象ID或显示编号",
    employee_id="公司对象目录中作为本项工资或垫付当事人的人员身份，不由姓名推定",
    name="用于税务导入文件的明确申报姓名，与对象管理显示名称独立核验",
)

_add(
    "ai_accounting.kernel.tax_import.TaxImportMapping",
    medical_code="本期税务模板医疗保险列对应的实际缴费项目代码，空表示明确不映射",
    pension_code="本期税务模板养老保险列对应的实际缴费项目代码，空表示明确不映射",
    unemployment_code="本期税务模板失业保险列对应的实际缴费项目代码，三个代码不得重复",
)

_add(
    "ai_accounting.kernel.work_draft_contract.DraftCandidate",
    command="该候选复用的已有类型化命令名；必须属于工作稿允许命令集合",
    id="本工作稿候选区段内唯一候选身份；不是正式业务编号",
    payload="候选的实际JSON输入；允许未补齐事实，但不接受凭据、原始字节或已登记事实快照",
)

_add(
    "ai_accounting.kernel.work_draft_contract.DraftPendingRequest",
    command="待核对回执的已有正式写入命令；受允许请求命令集合约束",
    payload="该待核对请求实际保存的JSON命令输入；不包含凭据或复制事实快照",
    request_id="该待核对命令的持久幂等请求身份；恢复时查原请求回执",
)

_add(
    "ai_accounting.kernel.work_draft_contract.DraftQuestion",
    answer="负责人实际提供的答案；缺少答案保留为空，不推断业务事实",
    id="本工作稿问题区段内唯一问题身份",
    question="向负责人明确提出的非空问题正文",
    source_refs="该答案或问题实际依据的明确来源引用",
)

_add(
    "ai_accounting.kernel.work_draft_contract.DraftResultRef",
    command="生成该结果的明确已有命令名",
    fact_id="该已登记结果的精确事实版本身份",
    fact_revision="该已登记结果的明确事实修订序号",
    job_id="该结果对应的持久后台任务身份；任务结果仍按真实输出核验",
    note="关于该结果引用的明确工作说明；不代替正式回执",
    request_id="该结果对应的持久幂等请求身份",
    subject_id="该已登记结果明确关联的业务身份",
)

_add(
    "ai_accounting.kernel.work_draft_contract.DraftSourceRef",
    evidence_digest="明确引用的来源原件内容摘要",
    fact_id="明确引用的精确事实版本身份",
    fact_revision="该引用明确指定的事实修订序号，不从当前版本补填",
    location="该来源中的具体原行、原页或资料位置",
    note="关于该引用的明确工作说明；不代替来源内容",
    source_id="明确引用的资料来源业务身份",
    subject_id="明确引用的业务对象身份",
)

_add(
    "ai_accounting.kernel.work_draft_contract.WorkDraft",
    candidates="尚未正式采用的类型化命令候选；复用已有命令合同，不建立另一套业务事实",
    next_step="工作稿记录的下一步操作说明；不自动执行或扩大业务授权",
    pending_requests="需要核对持久请求回执的明确命令、输入和幂等请求身份",
    questions="尚待负责人确认的问题及已提供答案；答案不自动成为正式事实",
    result_refs="工作稿明确保留的已登记结果引用；引用不等于再次采用或复制事实",
    resume_note="恢复工作时供操作者读取的明确交接说明",
    source_refs="工作稿明确保存的来源引用；不复制来源原件或已登记事实快照",
)

_add(
    "ai_accounting.kernel.workflow.AcceptedCalculation",
    calculation_id="不可变计算标识，精确固定采用事实与计算结果；不替换为当前计算",
    subject_id="内核登记的业务主题身份；与事实版本和计算版本标识分别保留",
)

_add(
    "ai_accounting.kernel.workflow.AdoptedSourceFact",
    fact_id="不可变事实版本标识，精确固定当时采用的正文；不是当前主题指针",
    subject_id="内核登记的业务主题身份；与事实版本和计算版本标识分别保留",
)

_add(
    "ai_accounting.kernel.workflow.CompanyWorkflowScope",
    applicability="每一外部办理类别明确的required或not_applicable，固定类别必须全部给出，不从活动推定",
    calendar_policy_id="明确采用的外部办理日历政策主题引用，复用办理周期及期限",
)

_add(
    "ai_accounting.kernel.workflow.ExternalBasisReview",
    completion_fact_id="本次复核针对的外部完成不可变事实版本，固定原完成依据",
    completion_id="本次依据复核所针对的外部完成业务主题引用",
    obligation_fact_id="所完成或复核的外部待办不可变事实版本标识，固定当时的期间和要求",
    obligation_id="所完成或复核的外部待办业务主题引用",
    obligation_kind="外部待办事项的明确类别，按固定定义决定覆盖期间和核验所需来源",
    review_result="明确本次复核与原依据匹配matched、识别差异difference_identified或未建立unestablished；不自动把来源变化认定无影响",
    reviewed_calculations="本次已实际核验的正式计算精确引用清单，与原采用清单分别保存",
)

_add(
    "ai_accounting.kernel.workflow.ExternalCompletion",
    completion_status="明确外部办理已提交submitted或已确认完成confirmed_complete，二者不是系统导出任务状态",
    date_status="外部办理实际日期已知或未知的明确状态，决定completion_date能否为空",
    no_reportable_activity_confirmed="明确确认本覆盖期没有需报告活动，不从缺少凭证或金额为零自动推定",
    obligation_fact_id="所完成或复核的外部待办不可变事实版本标识，固定当时的期间和要求",
    obligation_id="所完成或复核的外部待办业务主题引用",
    obligation_kind="外部待办事项的明确类别，按固定定义决定覆盖期间和核验所需来源",
    previous_completion_fact_id="上一次外部完成的精确事实版本引用，用于接续替换和审计",
)

_add(
    "ai_accounting.kernel.workflow.ExternalObligation",
    applicability="这一项外部办理义务明确是否需要办理，不从业务金额推定",
    applicability_confirmed="明确确认该外部待办是否适用，不把缺少业务自动当无需办理",
    obligation_kind="外部待办事项的明确类别，按固定定义决定覆盖期间和核验所需来源",
)

_add(
    "ai_accounting.kernel.workflow.FilingCalendarPolicy",
    deadlines="按外部义务类别和覆盖结束月明确登记的实际办理截止日清单，不计算推断截止日",
    primary_source_url="本政策的首要官方来源网址，用于版本来源核验",
    rules="各外部办理类别的明确周期规则，与明确截止日清单共同形成待办依据",
    version="明确采用的政策版本标识，与内核事实修订序号相互独立",
)

_add(
    "ai_accounting.kernel.workflow.FilingDeadline",
    obligation_kind="外部待办事项的明确类别，按固定定义决定覆盖期间和核验所需来源",
)

_add(
    "ai_accounting.kernel.workflow.FilingRule",
    cycle="该外部义务的明确办理周期，按月、季度或年度形成覆盖区间",
    obligation_kind="外部待办事项的明确类别，按固定定义决定覆盖期间和核验所需来源",
)
