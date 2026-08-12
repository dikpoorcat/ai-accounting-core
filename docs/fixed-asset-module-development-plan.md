# 历史：固定资产模块开发基线

> 文档性质：历史设计与实现记录，不是当前指令或自动生效规则。现行规则只见[当前保留规则](./current-rules.md)。
> 状态：第一期实现完成，独立验收通过
> 目标：完成外购、启用、按月直线折旧、出售/报废处置和关联冲正的确定性闭环
> 历史前置基线：工资模块第七轮验收在本阶段立项前已通过；该轮任务书现已归档，不构成当前指令
> 最终记录：[固定资产模块第一期最终验收记录](./fixed-asset-module-acceptance.md)

## 1. 定位与边界

本模块仍属于单企业私有模拟试用内核，不是法定资产卡片、企业所得税汇算清缴、增值税申报或税务意见系统。Agent 只能提交固定字段的业务事实，不能提交科目代码、借贷方向或自由分录。

本阶段只支持：

- 外购且需要在验收/调试后启用的单项非不动产固定资产；
- 银行现付或供应商挂账；
- 启用时冻结直线法、使用寿命月数、预计净残值和受益区域；
- 从启用月份次月开始逐资产、逐月计提折旧；
- 出售自己使用过的固定资产或零收入报废；
- 通过关联反向凭证按依赖逆序冲正。

下列事实继续返回 `needs_information` 或稳定的 `MODULE_NOT_ENABLED`，不得推断：

- 自建、改扩建、融资租赁、盘盈、捐赠、投资、非货币交换；
- 一笔总价购入多项但没有独立价格或评估分配依据；
- 房屋建筑物、土地、投资性房地产和生产性生物资产；
- 后续资本化支出、减值、加速折旧、税务折旧、递延所得税和纳税调整；
- 出售不动产、未收款处置费用、保险赔偿或残料入库等未冻结处理。

## 2. 官方依据与规则版本

会计处理依据：

- [财政部《小企业会计准则》](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf)，第二十七至三十四条：成本计量、直线法、次月起折旧、减少当月仍计提以及处置损益。
- [财政部《小企业会计准则——会计科目、主要账务处理和财务报表》](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf)，`1601`、`1602`、`1604` 和 `1606` 科目的主要账务处理。

出售增值税依据：

- [财政部、税务总局公告 2026 年第 10 号](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html)：自 2026-01-01 起，小规模纳税人（不含自然人）销售自己使用过的固定资产，按照简易计税方法依照 3% 征收率减按 2% 计算缴纳增值税；含税销售额按规定征收率换算。

企业所得税只记录边界依据，不进入本阶段计算：[《中华人民共和国企业所得税法实施条例》](https://www.chinatax.gov.cn/n810341/n810765/n812176/n812748/c1193046/content.html)。税法最低折旧年限不能覆盖用户明确的账面使用寿命，也不能被表述为本系统已经完成税前扣除判断。

规则版本固定为：

- 会计：`small_enterprise_fixed_asset_straight_line_2013.1`；
- 出售增值税：有效期自 `2026-01-01` 起的 `small_scale_used_fixed_asset_vat_2026.1`，官方来源必须随事件冻结。

## 3. 公共工具与事件边界

新增专用 MCP 工具：

- `finance_acquire_fixed_asset`
- `finance_activate_fixed_asset`
- `finance_preview_fixed_asset_depreciation`
- `finance_confirm_fixed_asset_depreciation`
- `finance_dispose_fixed_asset`
- `finance_get_fixed_asset`

旧的粗粒度 `fixed_asset` 不能进入 `finance_record_event`。事件目录应把固定资产模块报告为已启用的专用工作流，并把以下内部事件列为不可由通用入口直接提交：

- `fixed_asset_acquisition`
- `fixed_asset_activation`
- `fixed_asset_depreciation`
- `fixed_asset_disposal`

所有写工具使用严格类型、`additionalProperties: false` 和稳定错误码；金额只接受严格整数分，会改变处理的布尔事实只接受 JSON `true`/`false`。缺少业务事实返回 `needs_information`，非法状态、跨企业引用、过期哈希或不受支持的处理返回 `rejected`。

## 4. 购置契约

购置请求必须明确：企业、幂等键、资产编号、名称、有限类别、预计使用超过一年、购置日、入账日、成本组成、供应商、结算方式和至少一份证据。

有限类别只接受 `production_equipment`、`tools_furniture`、`transport`、`electronic` 和显式表示“其他可移动有形资产”的 `other_movable_tangible`。不提供含义模糊的 `other`，避免把房屋建筑物、土地等本阶段未启用资产混入。

成本固定为以下非负整数分之和，合计必须大于零：

```text
cost_fen
= purchase_price_fen
+ noncreditable_tax_fen
+ transport_and_handling_fen
+ installation_and_direct_cost_fen
```

本阶段企业均为小规模纳税人，进项税不抵扣并随相关成本资本化；若请求声明可抵扣进项税，返回模块未启用，不生成凭证。

结算：

- `bank`：付款日期必填；银行流水引用合计必须与成本一致。
- `payable`：供应商和到期日必填；生成同额普通应付开放项，后续使用现有 `supplier_payment` 核销。

固定模板：

```text
借：在建工程/待启用固定资产       cost_fen
  贷：银行存款或应付账款           cost_fen
```

## 5. 启用契约

启用请求必须显式提供资产、启用日、入账日、预计使用寿命月数、预计净残值、受益区域、幂等键和证据。使用寿命不得由类别默认且必须至少为 13 个月；预计净残值必须满足 `0 <= residual_value_fen < cost_fen`。为避免产生无法过账的零分折旧月份，还必须满足 `cost_fen - residual_value_fen >= useful_life_months`，否则稳定拒绝 `FIXED_ASSET_INVALID_DEPRECIATION_POLICY`。

本阶段只支持 `straight_line`，受益区域为：

- `management`
- `sales`
- `service_delivery`

固定模板：

```text
借：固定资产                     cost_fen
  贷：在建工程/待启用固定资产     cost_fen
```

启用事件冻结折旧方法、寿命、残值、受益区域、会计规则版本和官方来源。不得原地修改；需要更正时先逆序冲正下游，再冲正启用并新建事件。

## 6. 月折旧契约

折旧从启用月份的下一个自然月开始。同一资产有效折旧月份必须连续；不得跳月、重复或超过使用寿命。处置月份仍需完成当月折旧；已经提足折旧的资产不再产生零金额凭证。

整数分直线法：

```text
depreciable_fen = cost_fen - residual_value_fen
base_monthly_fen = depreciable_fen // useful_life_months

前 N-1 个月：base_monthly_fen
最后一个月：depreciable_fen - 已累计折旧
```

先调用 preview 读取资产不可变事实、有效历史和规则，返回金额、计算哈希与完整 trace；confirm 必须携带同一资产、月份、入账日、哈希和幂等键，提交时重新计算。入账日必须属于所声明的折旧月份，且不得早于购置和启用凭证；状态变化或哈希不同返回 `FIXED_ASSET_CALCULATION_STALE`。

固定模板：

```text
借：管理费用/销售费用/主营业务成本—折旧   当月折旧
  贷：累计折旧                            当月折旧
```

## 7. 处置契约

处置只支持：

- `sale`：非不动产资产出售，收款方式为银行或应收；
- `retirement`：无处置收入、无增值税的报废。

出售请求必须明确含税收入、发票类型、是否放弃起征点免税、结算方式、客户、税务义务日和证据。专项规则有效期以税务义务日选取，不以处置日或入账日替代。内核按有效的旧固定资产专项规则计算：

```text
tax_sales_fen = 含税收入 ÷ (1 + 3%)
vat_fen = tax_sales_fen × 2%
```

两步均使用 `Decimal` 和整分 `ROUND_HALF_UP`；不得把 2% 当作价税分离分母，也不得使用普通服务销售 1% 规则。处置费用只支持已由银行支付的明确金额。起征点减免仍由现有期间税务计算统一判断，处置事件只计提销项税。

处置前必须已经连续计提至处置月份，或资产此前已经提足折旧。内核读取原值和有效累计折旧，自动计算账面价值与处置净损益；请求不能提交这些派生金额。

一张凭证按固定资产清理步骤生成确定性分录：结转原值和累计折旧、确认银行/应收处置收入及增值税、记录清理费，并把固定资产清理净额唯一转入营业外收入或营业外支出。赊销按含税收入创建应收开放项。

## 8. 持久模型与不可变性

线性迁移 `0009` 新增：

- `fixed_assets`
- `fixed_asset_activations`
- `fixed_asset_depreciations`
- `fixed_asset_disposals`

关键事实使用规范列和组织复合外键，不只保存在 JSON。折旧与处置行必须通过 `activation_id` 绑定当时被冻结的启用事实，不能用后来重新启用的政策解释旧历史。资产不保存可直接修改的当前状态或累计折旧；状态从关联 `BusinessEvent.status` 推导。正式事实行不得更新或删除，被冲正行保留为审计历史。

至少保证：

- `(org_id, asset_code)` 永久唯一；资产编号在购置冲正后也不复用；
- 每个规范行的 `event_id` 唯一且与同企业事件绑定；
- 同一资产最多一个有效启用、一个有效处置和每月一个有效折旧；
- 有效折旧月份连续，累计折旧不超过原值减残值；
- 处置后不能折旧或再次处置；
- 正式事件必须有且仅有正确的规范事实与精确凭证模板；
- 资产写入、冲正与直接 SQL 都进入同一资产行锁域。

新增系统科目角色：

- `fixed_asset_pending`
- `fixed_asset_cost`
- `accumulated_depreciation`
- `management_depreciation_expense`
- `sales_depreciation_expense`
- `service_cost_depreciation`
- `fixed_asset_clearance`
- `fixed_asset_disposal_gain`
- `fixed_asset_disposal_loss`

迁移必须为既有企业回填，不得只更新新企业 seed。

## 9. 幂等、依赖和冲正

请求哈希包含专用命令名和规范化请求。相同幂等键、相同请求回放原结果；相同键、不同请求稳定返回 `FIXED_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH`。

冲正沿用 `finance_reverse_event`，原事实、原凭证和资产规范行保持不变，只创建关联反向事件和反向凭证。依赖顺序固定为：

1. 先冲正处置；
2. 折旧从最新有效月份向前冲正；
3. 全部折旧和处置已冲正后才能冲正启用；
4. 启用及其全部下游已冲正后才能冲正购置；
5. 购置应付已核销时，先冲正相应供应商付款。

## 10. 最低稳定错误契约

`needs_information` 至少覆盖：资产身份、成本组成、供应商/结算、银行流水、来源证据、启用日、寿命、残值、受益区域、缺失前序折旧、处置类型/收入/税务/清理费事实。

`rejected` 至少使用下列稳定错误码：

- `FIXED_ASSET_REQUIRES_SPECIALIZED_WORKFLOW`
- `FIXED_ASSET_NOT_FOUND`
- `FIXED_ASSET_NOT_ACTIVATABLE`
- `FIXED_ASSET_ALREADY_ACTIVATED`
- `FIXED_ASSET_CALCULATION_STALE`
- `FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE`
- `FIXED_ASSET_DEPRECIATION_ALREADY_POSTED`
- `FIXED_ASSET_ALREADY_DISPOSED`
- `FIXED_ASSET_DISPOSAL_WITH_UNPOSTED_DEPRECIATION`
- `FIXED_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH`
- `FIXED_ASSET_OPEN_DEPENDENCIES_EXIST`

MCP 外层继续只公开稳定错误码，不回显 SQL、连接串、文件路径或长输入。

## 11. 解释轨迹

每个正式事件至少记录：

- 已验证的业务事实和证据 ID；
- 会计/税务规则代码、版本、有效期和官方来源；
- 成本组成或原值、残值、寿命、累计折旧、账面价值；
- 整分除法、末月调整或出售增值税计算步骤；
- 固定模板使用的系统角色、逐行金额、借贷合计；
- 依赖事件、计算哈希、凭证和冲正链。

## 12. 后续修改与验证

本文件不规定实现工作包、共享文件负责人、独立验收角色或统一测试门禁。后续修改内容和验证范围由用户针对当前步骤决定；既有单元、PostgreSQL、迁移、STDIO 和覆盖率检查可按需要选用，不要求每个小改动全部执行。
