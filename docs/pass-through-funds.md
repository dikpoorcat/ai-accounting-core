# 组件式代收代付

运行协议 `business-components-v3`，业务库使用空库基线 `0001_business_baseline_v4`。
代收与应收核销、预收和其他业务采用相同 `finance_record_event` 组件协议，银行或现金收付
统一放在 `funds`。代收确认代收代付负债，不形成收入或预收款；不必先登记受益人或债权人。

## 一笔收款包含多个业务用途

以下金额均为整数分；示例编号须替换为当前公司真实编号。

```json
{
  "org_id": "<公司UUID>",
  "idempotency_key": "receipt-components-1",
  "posting_date": "2026-08-09",
  "description": "佣金回款及代收款",
  "evidence_references": ["<业务证据UUID>"],
  "components": [
    {
      "key": "commission",
      "kind": "receivable_settlement",
      "business_date": "2026-08-09",
      "allocations": [{"open_item_id": "<原佣金应收UUID>", "amount_fen": 9657350}]
    },
    {
      "key": "collection-1",
      "kind": "pass_through",
      "amount_fen": 2342650
    }
  ],
  "funds": [{
    "key": "bank-receipt",
    "account_code": "100201",
    "direction": "receipt",
    "payment_date": "2026-08-09",
    "amount_fen": 12000000,
    "allocations": [
      {"component_key": "commission", "amount_fen": 9657350},
      {"component_key": "collection-1", "amount_fen": 2342650}
    ],
    "bank_transaction_references": [{"id": "<整笔120000元流水UUID>"}]
  }]
}
```

不同代收业务使用独立稳定组件键；明确预收款另有 `customer_advance` 组件。
每个组件键唯一，资金分配总和必须精确等于真实收款。未说明的余款返回 `needs_information`。
不能把未知余款默认成预收，也不能通过调整收入、税额或往来余额凑平。

用途、受益人、经办人和管理日期可以放入 `metadata`，也可以关账后通过
`finance_update_business_metadata` 后补。缺这些资料不追问、不阻断。
只有明确“个人先行垫付并形成公司对个人的债务”时，才用个人垫付或债务转换业务，
提供实际垫付人、债务确认日期或月份及依据；具体垫付日仅为可选管理资料。
不能仅因管理信息里出现人名就推断发生债务转换。

## 付款和债务转移

直接支付使用 `payable_settlement` 组件，以 `allocations` 精确引用代收应付款。
可用原入账幂等键 `source_event_key`、`source_component_key`、`source_open_item_key`
精确引用，也可使用已有开放项UUID。普通核销允许多来源、多对象，与费用、其他应付结算
共同分配到实际 `funds`；检查公司、来源类型和剩余余额，不要求重填来源对象。

收款后员工或股东代付，使用 `debt_transfer` 提交实际 `payer`、债务确认日期或月份、证据及已偿债务
`allocations`，由原债权人转为对代垫人的应付。归还时按新应付来源使用
`payable_settlement`。明确同笔依赖可通过 `source_component_key` 表达，不能虚构现金流。

## 修改、删除与冲正

`finance_get_event` 读取当前完整事实及 `facts_hash`，`finance_amend_event` 提交完整
`replacement`、预期哈希、新幂等键和原因。保留未改变组件的稳定键，整笔重算保留原凭证
编号及审计历史，同时处理全部往来和银行匹配。已有后续依赖时先处理依赖，禁止级联删除。
开放月删除付款会恢复核销余额和对应匹配；已关账通过关联冲正更正。

相同幂等键和事实返回原结果，不同事实拒绝。任一组件缺事实、超额或失败时整笔回滚，
不能留下部分正式凭证或部分匹配。代码重构不自动执行试用公司的业务更正。
