# 代收代付

迁移 `0006_pass_through`、运行契约 `accounting_execution_assistant_v33`。通过已有
`finance_record_event` 使用类型化事实，不增加自由分录接口。改动代码后应重新连接 MCP；
仅刷新数据库连接不会加载新业务类型。银行及已纳入对账范围的支付平台账户均可通过
`bank_account_code` 选择，流水总额须精确等于整笔收付。

## 收款与代付

`customer_receipt` 可同时包含佣金应收的 `allocations` 和明确的 `pass_through_items`。
总金额 = 应收核销 + 代收份额 + 明确的预收款。未说明余款用途时返回 `needs_information`。
代收份额只形成 `224105 / pass_through_payable` 其他应付款，不产生收入、费用或税额。
原应收业务已有的税务处理仍沿其应收核销链执行。

以下金额均为整数分，UUID 占位符必须替换为当前公司的真实编号。最小混合收款示例：

```json
{
  "org_id": "<公司UUID>",
  "idempotency_key": "mixed-receipt-1",
  "event_type": "customer_receipt",
  "business_dates": {
    "business_date": "2026-08-09",
    "payment_date": "2026-08-09",
    "posting_date": "2026-08-09"
  },
  "counterparty": {"id": "<付款客户UUID>"},
  "amounts": {"amount_fen": 12000000},
  "bank_account_code": "100201",
  "bank_transaction_references": [{"id": "<整笔120000元流水UUID>"}],
  "evidence_references": ["<业务证据UUID>"],
  "allocations": [{"open_item_id": "<原佣金应收UUID>", "amount_fen": 9657350}],
  "pass_through_items": [{
    "key": "beneficiary-1",
    "amount_fen": 2342650,
    "beneficiary": {"id": "<最终受益人UUID>"},
    "creditor": {"id": "<同一最终受益人UUID>"},
    "creditor_basis": "beneficiary",
    "purpose": "有证据确认的代收款用途"
  }],
  "description": "佣金回款及代收款"
}
```

有多个受益人或实际债权人时，按明确金额拆为多个条目，每个条目的 `key` 唯一。
同一条目修改时保留 `key`，以复用原往来编号。`beneficiary` 是最终受益人；
`creditor` 是当前公司应付款的债权人，二者不能混淆。

若收款前某人已经代垫给受益人，该条目应明确：

```json
{
  "key": "advanced-beneficiary-1",
  "amount_fen": 552685,
  "beneficiary": {"id": "<最终受益人UUID>"},
  "creditor": {"id": "<已确认代垫人UUID>"},
  "creditor_basis": "advance_reimbursement",
  "advance_payment_date": "2026-08-08",
  "advance_evidence_ids": ["<代垫证据UUID>"],
  "purpose": "归还已确认的受益人款项代垫"
}
```

代垫证据同时放入整笔事件的 `evidence_references`。代垫日期不得晚于收款日期。
如果是收款后员工或股东代付，先用 `employee_reimbursement` 的
`details.reimbursement_kind=existing_payable`、`paid_now=false`，提供真实代付日期、证据
和代收应付款 `allocations`，将相应余额转为对代垫人的应付；之后用
`employee_reimbursement_payment` 归还。不得为方便记账忽略真实代垫关系。

收款返回的 `data.created_open_items` 和 `finance_query_context` 提供往来编号、债权人、
`payable_category=pass_through`、`pass_through_key`、最终受益人及未结余额。
查询包含部分结算的往来。直接付给该债权人时：

```json
{
  "org_id": "<公司UUID>",
  "idempotency_key": "pass-through-payment-1",
  "event_type": "pass_through_payment",
  "business_dates": {
    "business_date": "2026-08-10",
    "payment_date": "2026-08-10",
    "posting_date": "2026-08-10"
  },
  "counterparty": {"id": "<该往来的实际债权人UUID>"},
  "amounts": {"amount_fen": 1789965},
  "bank_account_code": "100201",
  "bank_transaction_references": [{"id": "<17899.65元支出流水UUID>"}],
  "evidence_references": ["<付款证据UUID>"],
  "allocations": [{"open_item_id": "<代收应付款UUID>", "amount_fen": 1789965}],
  "description": "按确认的代收安排支付债权人"
}
```

同一付款只核销同一债权人的代收应付款，不能超额、串用普通应付款或跨公司核销。
同一幂等键相同事实返回原结果；不同事实拒绝。并发付款在事务内锁定和重新读取余额。

## 未关账错误收款的更正顺序

1. `finance_get_event` 读取原收款的当前事实和 `facts_hash`，查询原应收核销与银行匹配。
2. 先确认每个代收份额的最终受益人、实际债权人、金额及代垫事实。缺事实时停在补充事实，
   不把代收款填成 `unallocated_treatment=advance`，也不用 `customer_refund` 冒充代付。
3. 用 `finance_amend_event` 提交原收款编号、`expected_facts_hash`、新幂等键、原因及完整
   `replacement`。保持 `customer_receipt` 类型、原客户、佣金应收 `allocations`、整笔银行
   引用；增加已明确的代收条目，去掉错误的预收款处理。系统原子重算原凭证并保留编号及审计。
4. 更正成功后查询新往来，按实际债权人分别记付款，每笔支出匹配相应流水。

已有后续付款时，源收款修改或删除会返回依赖阻断。应按实际事实先处理后续付款，不能级联
删除。开放月删除付款会恢复其核销余额和银行匹配；冲正保留关联凭证并恢复相应余额。
已关账月份仍不能原地修改或删除。开发升级不自动执行真实公司的业务更正。
