# 无形资产与借款利息第一期开发基线

> 状态：已完成并通过独立验收（2026-08-11）；税务确定性前置门禁已于 2026-08-10 通过
> 阶段目标：完成外购无形资产的取得、摊销、零收入报废，以及金融机构人民币固定利率借款的放款、计息、付息、还本闭环
> 前置基线：[税务事实与期间结算确定性整改基线](./tax-determinism-remediation.md)
> 定位：单企业私有模拟试用内核，不是法定资产卡片、融资管理、税务申报、企业所得税汇算清缴或税务意见系统

## 1. 共同边界

两个模块都只接受固定字段的业务事实。Agent 不能提交科目代码、借贷方向、自由分录、账面价值、累计摊销、应计利息或借款余额等派生金额。正式凭证只从内核发布的有限模板生成。

金额均为严格整数分，输入和所有派生金额不得超过有符号 64 位整数上限；合计及利息计算在生成凭证前再次检查边界。利率和乘除计算只使用固定本地上下文的 `Decimal`，最终利息以 `ROUND_HALF_UP` 取整分，不读取进程全局 Decimal 精度。资料缺失返回 `needs_information`；不受支持但事实完整的业务返回稳定 `rejected` 错误，不得改走普通费用或其他通用事件。

## 2. 官方依据与规则版本

会计依据：

- [财政部《小企业会计准则》](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf)第三十八至四十二条：无形资产定义、外购成本、年限平均法、可供使用至停止使用/出售的摊销期和处置净额；第四十八、五十二条：短期及长期借款按本金和合同利率在应付利息日计提。
- [财政部《小企业会计准则——会计科目、主要账务处理和财务报表》](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf)：`1701`、`1702`、`2001`、`2501`、`2601` 及相关费用科目的主要账务处理。

企业所得税仅记录边界，不进入本阶段计算：[《中华人民共和国企业所得税法实施条例》](https://www.chinatax.gov.cn/n810341/n810765/n812176/n812748/c1193046/content.html)第三十七至三十九条、第六十七条。税法摊销期限和借款费用扣除/资本化条件不能覆盖账面政策，也不能被表述为本系统已完成纳税调整。

规则版本固定为：

- 无形资产会计：`small_enterprise_intangible_assets_2013.1`；
- 借款会计：`small_enterprise_borrowings_2013.1`。

正式事实冻结规则代码、版本、官方来源和适用边界。将来规则更正只能新增版本，不能覆盖历史。

## 3. 无形资产一期范围

只支持外购、已达到可供使用状态、可单独识别且不包含土地权利的单项无形资产：

- `software`
- `patent`
- `trademark`
- `copyright`
- `non_patented_technology`
- `other_identifiable_non_land`

`other_identifiable_non_land` 必须提交权利类型说明和可辨认依据，不能容纳土地使用权、商誉或无法单独识别的支出。

下列处理不在一期范围，不能被推断为普通外购资产：

- 研究支出、内部开发及研发资本化判断；
- 土地使用权、商誉、特许经营权中的不动产或资源税费判断；
- 接受投资、捐赠、非货币交换、企业合并或政府补助取得；
- 后续资本化支出、减值、税务摊销和税会差异；
- 出售、有收入处置、赔偿、残料或非月末停止使用。

## 4. 无形资产公共工具与事件

专用 MCP 工具：

- `finance_acquire_intangible_asset`
- `finance_preview_intangible_asset_amortization`
- `finance_confirm_intangible_asset_amortization`
- `finance_retire_intangible_asset`
- `finance_get_intangible_asset`

内部事件：

- `intangible_asset_acquisition`
- `intangible_asset_amortization`
- `intangible_asset_retirement`

旧的粗粒度 `intangible_asset` 继续不能进入 `finance_record_event`。事件目录应报告模块已启用并指向专用工作流。

## 5. 无形资产取得契约

请求必须明确：企业、幂等键、永久唯一资产编号、名称、有限类别、权利说明、供应商、取得日、可供使用日、入账日、成本组成、结算方式、使用寿命、寿命依据、受益区域和至少一份证据。

一期只接收取得时已经可供使用的资产；`acquisition_date` 和 `available_for_use_date` 必须位于同一自然月，入账日也必须位于该月且不早于取得日。需要实施或调试后才能使用的项目返回 `INTANGIBLE_ASSET_NOT_READY_WORKFLOW_NOT_ENABLED`。

成本：

```text
cost_fen
= purchase_price_fen
+ noncreditable_tax_fen
+ directly_attributable_cost_fen
```

各项为非负整数分且合计大于零。本阶段小规模纳税人进项税不抵扣；声明可抵扣进项税时稳定拒绝，不生成凭证。

结算只支持：

- `bank`：付款日和银行流水必填，流水合计必须等于成本；
- `payable`：供应商和到期日必填，生成同额受控应付开放项，后续由现有 `supplier_payment` 核销。

寿命事实：

- `life_basis=legal_or_contractual`：寿命月数必须有法律或合同证据；
- `life_basis=reliably_estimated`：寿命月数必须有明确估计依据；
- `life_basis=not_reliably_estimated`：用户仍必须明确提交不少于 120 个月，不由类别推断。

成本必须不少于寿命月数，避免产生零分正式摊销月。寿命最长为 119,988 个月，且从可供使用月份计算的最后一个摊销月不得晚于 `9999-12`；年份 `0000` 和任何不可表示的生命周期均在入账前稳定拒绝。残值固定为零，不向 Agent 开放残值字段。

固定模板：

```text
借：无形资产                         cost_fen
  贷：银行存款或应付账款             cost_fen
```

## 6. 无形资产月摊销契约

准则要求从可供使用时开始摊销。为形成确定的月度内核，一期采用并明确记录“可供使用当月记一个完整自然月摊销”的离散化政策；这不是固定资产的次月起折旧规则。

```text
base_monthly_fen = cost_fen // useful_life_months
前 N-1 个月       = base_monthly_fen
最后一个月        = cost_fen - 已累计摊销
```

同一资产有效摊销月份必须从可供使用月份开始连续，不得跳月、重复、超寿命或超过成本。最后一个月精确闭合到零账面价值；提足后不生成零分凭证。

preview 返回不可变事实、下一个月份、金额、完整 trace 和计算哈希；confirm 必须携带同一资产、月份、入账日、哈希和幂等键并在锁内重算。入账日必须属于声明月份。

受益区域固定为 `management`、`sales` 或 `service_delivery`：

```text
借：管理费用/销售费用/主营业务成本—无形资产摊销   amount_fen
  贷：累计摊销                                    amount_fen
```

## 7. 无形资产报废与冲正

一期仅支持在自然月最后一天停止使用且无任何收入、赔偿、税费或残料的报废。报废月份必须先完成当月摊销，除非资产此前已经摊足。

```text
借：累计摊销                         accumulated_fen
借：营业外支出                       book_value_fen
  贷：无形资产                       cost_fen
```

更正顺序固定：先冲正报废；再从最新月份向前冲正摊销；全部下游已冲正后才能冲正取得。资产编号在取得冲正后也不复用。原事实、凭证和规范行保留。

## 8. 借款一期范围

只支持中国持牌金融机构作为贷款人、人民币、单次全额放款、固定年利率、合同单利、一期只在到期日全额归还本金的经营周转借款。短期/长期科目由合同放款日与到期日的期限确定，不由 Agent 选择。

请求必须显式声明 `capitalization_applicable=false`。任何可能直接归属于固定资产、无形资产或超过 12 个月才可销售存货的借款费用，均返回 `BORROWING_CAPITALIZATION_NOT_ENABLED`，不得默认计入财务费用。

下列事项不支持：循环额度、多次提款、分期或提前还本、展期、浮动/重定价利率、复利、罚息、贴现、债券、手续费、溢折价、外币和汇兑、担保计量、委托贷款、股东/关联方或其他非金融企业借款、债务重组。

## 9. 借款公共工具与事件

专用 MCP 工具：

- `finance_draw_borrowing`
- `finance_preview_borrowing_interest`
- `finance_confirm_borrowing_interest`
- `finance_pay_borrowing_interest`
- `finance_repay_borrowing_principal`
- `finance_get_borrowing`

内部事件：

- `borrowing_drawdown`
- `borrowing_interest_accrual`
- `borrowing_interest_payment`
- `borrowing_principal_repayment`

旧的 `loan_interest` 不能进入通用事件入口。

## 10. 借款放款契约

请求必须明确：企业、幂等键、永久唯一借款编号、合同名称、贷款人、贷款人为持牌金融机构的严格布尔事实、币种 CNY、本金、放款日、到期日、入账日、固定年利率、日计数基础、资本化是否适用、用途说明、银行流水和合同证据。

合同还必须提交严格递增、无重复的应付息日清单；首个应付息日晚于放款日，最后一个应付息日必须等于到期日。计息预览和确认的 `period_end` 必须是清单中的下一个应付息日。循环提款、固定利率、单利、到期一次还本、提前还本、展期、罚息和融资手续费等支持边界事实使用严格布尔值逐项声明；缺失返回 `needs_information`，与一期边界不符才返回 `BORROWING_UNSUPPORTED_TERMS`。

日计数基础只接受 `actual_360` 或 `actual_365`。年利率使用 `Decimal`，必须大于零且不超过 100%，最多六位小数，并在正式事实、哈希和数据库中规范保存为六位小数字符串；超过六位不得静默舍入。放款银行流水必须精确等于本金。

期限不超过放款日后一个周年日使用短期借款角色，超过一个周年日使用长期借款角色：

```text
借：银行存款                 principal_fen
  贷：短期借款或长期借款     principal_fen
```

同时生成受控本金应付事实，不能超额归还。

## 11. 借款计息、付息和还本

计息区间使用左闭右开 `[period_start, period_end)`，实际天数由日期相减；首期开始日必须等于放款日，后续开始日必须等于上一有效计息期结束日，结束日不得超过到期日。

```text
interest_fen
= ROUND_HALF_UP(
    principal_fen
    × annual_rate_percent / 100
    × actual_days / day_count_denominator
  )
```

每期利息必须大于零。preview 返回本金、利率、天数、分母、未付利息、规则、trace 和哈希；confirm 在锁内重算。入账日固定为 `period_end`，并生成：

```text
借：财务费用—利息       interest_fen
  贷：应付利息           interest_fen
```

每笔付息请求只引用一个有效且未支付的计息事件，金额由内核读取，不由 Agent 分配；付款日不得早于该计息期末或晚于合同到期日，银行流水必须精确匹配：

```text
借：应付利息             interest_fen
  贷：银行存款           interest_fen
```

本金只能在合同到期日一次全额归还。必须已经连续计息至到期日且全部利息已支付；银行流水必须精确等于本金：

```text
借：短期借款或长期借款   principal_fen
  贷：银行存款           principal_fen
```

更正顺序：先冲正本金归还；再冲正相关付息；再按最新期间向前冲正计息；全部下游已冲正后才能冲正放款。借款编号不复用。

## 12. 持久模型与数据库提交点

线性迁移在税务整改迁移之后新增：

- `intangible_assets`
- `intangible_asset_amortizations`
- `intangible_asset_retirements`
- `borrowings`
- `borrowing_interest_accruals`
- `borrowing_payments`

所有规范行使用组织复合外键绑定业务事件、交易对手和来源事实。状态和累计金额从有效事件链推导，不保存可任意修改的“当前余额”。正式行不可更新或删除。

PostgreSQL 提交点至少阻止：跨企业引用、重复资产/借款编号、跳月或超额摊销、报废后摊销、计息缺口/重叠/越过到期日、重复付息、提前/部分/重复还本、超额开放项核销和越过下游直接冲正。直接 SQL 与并发写入必须进入同一资产或借款锁域。

新增系统角色：

- `intangible_asset_cost`
- `accumulated_amortization`
- `management_amortization_expense`
- `sales_amortization_expense`
- `service_cost_amortization`
- `intangible_asset_retirement_loss`
- `short_term_borrowing`
- `long_term_borrowing`
- `interest_payable`
- `borrowing_interest_expense`

迁移必须为既有企业安全回填科目并记录所有权；不能只更新新企业 seed。

## 13. 幂等、解释轨迹与稳定错误

所有写请求哈希包含专用命令名和规范化请求；同键同载荷回放，同键换载荷分别拒绝 `INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH` 或 `BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH`。

每个正式事件至少冻结：已验证事实、证据 ID、规则版本和官方来源、成本或合同参数、逐步整数分/Decimal 计算、系统角色与平衡合计、依赖事件、计算哈希、凭证和冲正链。

最低错误码：

- `INTANGIBLE_ASSET_REQUIRES_SPECIALIZED_WORKFLOW`
- `INTANGIBLE_ASSET_NOT_FOUND`
- `INTANGIBLE_ASSET_NOT_READY_WORKFLOW_NOT_ENABLED`
- `INTANGIBLE_ASSET_INVALID_AMORTIZATION_POLICY`
- `INTANGIBLE_ASSET_CALCULATION_STALE`
- `INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE`
- `INTANGIBLE_ASSET_ALREADY_RETIRED`
- `INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST`
- `INTANGIBLE_ASSET_COST_OUT_OF_RANGE`
- `INTANGIBLE_ASSET_AMORTIZATION_DATE_OUT_OF_RANGE`
- `INTANGIBLE_ASSET_COUNTERPARTY_IDENTITY_MISMATCH`
- `BORROWING_REQUIRES_SPECIALIZED_WORKFLOW`
- `BORROWING_NOT_FOUND`
- `BORROWING_UNSUPPORTED_TERMS`
- `BORROWING_CAPITALIZATION_NOT_ENABLED`
- `BORROWING_CALCULATION_STALE`
- `BORROWING_INTEREST_OUT_OF_SEQUENCE`
- `BORROWING_INTEREST_ALREADY_PAID`
- `BORROWING_PRINCIPAL_NOT_REPAYABLE`
- `BORROWING_OPEN_DEPENDENCIES_EXIST`
- `BORROWING_INVALID_RATE_PRECISION`
- `BORROWING_INTEREST_AMOUNT_OUT_OF_RANGE`
- `BORROWING_INTEREST_PAYMENT_DATE_INVALID`
- `BORROWING_LENDER_IDENTITY_MISMATCH`

## 14. 实现包与最终门禁

- A：无形资产专用 Schema、纯计算器、服务及单元/性质测试；避免修改通用 `service.py`。
- B：借款专用 Schema、纯计算器、服务及单元/性质测试；避免修改通用 `service.py`。
- C：单一负责人独占 `models.py`、线性迁移、科目回填、提交点函数及 PostgreSQL 迁移/负向/并发测试。
- D：单一集成负责人独占 `mcp_server.py`、事件目录、通用冲正路由、真实 STDIO、跨模块回归、README 和验收记录。

两个闭环都必须具备平衡、幂等、换载荷冲突、计算哈希、期间关闭、证据、解释轨迹和逆序冲正覆盖。最终执行：

```powershell
pytest -ra
pytest -m postgres -ra
pytest --cov=ai_accounting --cov-report=term-missing
ruff check .
python -m pip check
python -m compileall -q src alembic tests
alembic check
git diff --check
```

迁移和 PostgreSQL 验证只连接显式唯一命名的临时 PostgreSQL 17；默认 Compose `finance` 数据库保持只读。
