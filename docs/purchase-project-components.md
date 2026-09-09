# 供应商预付款与项目成本

业务库执行至前向迁移 `0003_essential_accounting`；目录库保持独立的 v2 基线。
所有新增组件使用 `finance_preview_event` / `finance_record_event`，金额为整数分，
正式写入仍只有统一提交器。缺少关键事实返回 `needs_information`。

## 交付前预付款

- `supplier_advance`：明确 `purchase_purpose`、`amount_fen`、证据和稳定组件键；
  `funds` 提供实际付款日期与支出。对象、项目号、合同号和用途文本放入可选 `metadata`。
- `supplier_advance_application`：`advances` 引用预付款，`allocations` 引用真实供应商应付，
  两侧合计相等。来源用 `open_item_id` 或同笔 `source_component_key`，允许部分及多次冲抵。
- `supplier_advance_refund`：以 `advances` 引用可退余额，资金项提供实际退款到账日期；
  退款说明及编号可选。

来源必须属于同公司，账户、方向和业务来源有效；不比较对象及项目标签。
科目继承原来源，整笔累计消耗不能超过来源余额。标签变化本身不构成债务转移；
实际不同主体之间权利义务变化必须通过对应业务组件表达。
冲抵不提供资金项，不再次归属现金流；退款沿用原预付款的现金流分类。
预付款不能通过普通应收核销或核销未来应付绕过来源规则。

例如 2022-09-21 预付 800,000 分，2022-11-30 资产成本确认 1,600,000 分：

1. 9 月记录 `supplier_advance`，实际付款 800,000 分。
2. 11 月 `intangible_asset_acquisition` 使用 `settlement_method=payable`，确认资产及应付。
3. 同笔 `supplier_advance_application` 冲抵 800,000 分，`payable_settlement` 支付尾款
   800,000 分；两者的应付来源均可用资产组件键。资金仅分配给尾款支付组件。

预付款不能推断为已完成阶段验收；11 月的应付不能用于核销 9 月付款。

## 已完成阶段的成本与债务

`project_cost` 同时确认阶段成本及供应商应付。付款继续使用 `payable_settlement`，也可组合
预付款冲抵。必须明确：

- 已确认阶段成果的业务日期、金额、`rights_controlled` 和证据；
- `project_nature`：外购成果 `purchased_intangible`，或自行开发 `internal_development`；
- `cost_element`：购价 `purchase_price`、不可抵扣税费 `noncreditable_tax`、直接归属成本
  `directly_attributable_cost`。需要多个构成时用多个组件，不能把税额重复加入两项成本。

外购成果成本使用 `intangible_project_cost`。自行开发使用 `development_expenditure`，还须
填写 `development_conditions`：技术可行、完成并使用的意图、经济利益、资源、可靠计量以及
条件满足日期。条件未满足或发生日期早于条件满足日期时不得资本化。自行开发的成本应按
直接归属成本及不可抵扣税费归集，不能虚构购入价格。现有组件不自动计算或申报进项抵扣。

## 结转为无形资产或费用

达到预定可用状态后，`intangible_asset_acquisition` 使用 `settlement_method=project_cost`。
组件顶层 `cost_sources` 列出 `component_id` 或同笔 `component_key`，以及本次结转金额。
总成本与 `cost_components` 由明确来源及本次分配金额生成，无需重复填写；
若同时提供总成本或分解项，则必须与来源相符。无来源直接购置使用明确 `cost_fen`，
成本分解选填，提供时合计必须相等；不强制无关分项填零。

来源可以跨期及明细账户，不依赖管理项目标签，累计消耗不得超额。此时只结转已有
成本，不再新建应付或现金流。资产卡片、摊销、报废复用原机制，来源证据独立保留。
阶段验收编号、付款义务编号、合同号、普通应付到期日、资本化说明均为可选管理信息，
阶段成果是否实际确认及资产确认条件仍是核算事实。

`finance_get_event` 对阶段成本返回 `project_cost_balance`：确认金额、已消耗金额和当前可用
金额。项目取消或部分成果废弃时，以 `project_cost_expense` 明确来源和费用分类，原因说明可选。
已转费用的组件不能重新作为资产来源。

未关账修改或删除通过整笔生命周期处理；有效下游依赖会阻止改写来源。已关账通过关联
冲正处理。冲正或删除消费者后恢复来源可用余额，不修改历史凭证。

## 科目与报表

默认预付账户 `1123`（`prepayments`）归入资产负债表预付账款；外购项目成本 `189901`
（`intangible_project_cost`）归入其他非流动资产；资本化研发支出 `4301`
（`development_expenditure`）归入开发支出。同类明细账户仍用 `finance_configure_account`。
现金流仅按实际支付、退款及其业务用途记录；资产结转和预付款冲抵不重复统计。

依据：财政部 [科目和主要账务处理](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf)，
以及 [《小企业会计准则》第三十九、四十条](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf)。
