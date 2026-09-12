# 核算等价与无影响复核

比较合同 `accounting-v1`。本契约用于正式发布及外部办理依据的核算等价判断，
不替代来源审阅、并发版本或导出文件依据。

## 两种摘要

- `result_digest` 始终是完整原 `outcome` 的摘要。核算比较不修改 outcome，
  不写入派生信封，也不改变历史摘要。仅程序构建变化、事实和 outcome 未变时，
  `calculation_id` 可以变化，`result_digest` 不变。
- `accounting` 包含比较合同和派生摘要，仅存在于比较上下文、发布预览、确认和
  既有发布审计结果中。基线包括稳定业务身份、类型、期间、完整冻结事实 payload
  和原 outcome，因此涵盖分录、现金流、期初、余额、完整义务及后续计算状态。
  资产寿命、残值等即使未进入当次 outcome，也通过冻结事实参与比较。

事实外层的版本 ID、revision 和附加证据是审计版本；类型化 payload 中的身份、
精确版本引用和业务字段继续参与比较。未知新增字段保守保留，缺失与 null 不归一化，
不将未知金额补零，不按净额归并分录或义务。

## 来源豁免

领域策略通过 Registry 注册；通用发布器不识别工资字段。仅以下路径规范化：

| 路径 | 机器消费者及保留的核算含义 |
| --- | --- |
| 工资 `actual_withholding.fact_id/evidence` 及 `actual_withholding_adopted` 解释中的同两项 | 以精确冻结实际扣税事实的稳定身份和完整 payload 摘要替代版本指针，仅排除附加证据；人员、期间、实际税额、确认状态和原申报累计扣除全部保留。代发使用的已记扣税状态、工资累计、估算差额、义务及未知边界继续参与比较。 |
| 工资 `source_versions` 中对应实际扣税事实的唯一元素 | 仅替换这一元素。准备检查使用的档案版本、其他实际来源、首次工资处理、规则版本及列表顺序保持严格。 |
| 四类实际付款 `settlements[*].source_calculation` 中对应工资来源的指针 | 在核对 allocation、人员、义务、方向和清偿许可后，以精确来源工资的稳定身份、期间和完整核算签名替代指针。工资义务限制或累计状态变化也使付款不等价。报表按原指针读取的科目、方向、金额和交易方含义仍保留。 |

四类付款为 `payment`、`cash_payment`、`platform_payment`、
`payroll_reserve_payment`；工资来源仅包括 `payroll`、`payroll_bounded`。
其他来源的付款指针、tax_transfers、期初成员、退抵税和备用金承接来源不豁免。
工资规则版本、险种解释分项、未知扣除项版本也不豁免。

所有原指针和证据仍在 outcome 中保存。规范化只能使用记录的精确依赖，不能读取当前
事实冒充原来源。稳定身份与同额不是充分条件，来源核算状态和限制必须完整进入比较。

## 发布、兼容与失败恢复

`impact` 为 `initial`、`review_no_impact`、`accounting_changed` 或
`compatibility_required`。预览中的比较结论、旧 current ID 和结果进入完整预览签名；
确认重算并继续核验 epochs、精确来源和旧 current，正式分类与发布动作保持一致。

无影响复核仍追加计算、依赖、publication 和处置记录，正式发布后才推进 current
并清除 pending；保留原凭证编号、版本、记账期间和关账结果。之后发生真实核算变化，
继续按既有规则重算或关联冲正。本轮付款签名包含完整来源工资签名，因此真实工资累计
或义务变化可能使同额付款仍进入更正；本契约只豁免已证明的来源变化。

旧记录没有 accounting 字段是正常形态。比较时从原 outcome、其 fact_id 指向的冻结
事实和必要精确依赖只读派生，不调用当前 evaluator 重算历史、不修改旧记录。支持
现行常规结果及工资无实际扣税、有实际扣税、未启动扣缴、精确累计和有界累计分支。
普通结果解码、`#id` 和原始追溯不强制执行适配。

必要结构或冻结来源不可恢复、或请求了不支持的比较合同时，相关项返回
`accounting_compatibility_required`，不转换为负责人补充事实的问题。
同一 confirm 的任一必需比较失败都会使整笔请求原子失败；无关查询和独立业务不受
此比较错误影响。补充受控只读适配后重新预览原业务请求即可恢复，不回写历史。

## 外部办理与后续边界

workflow 以派生签名判断真实已接受依据与当前核算是否等价，但原
`accepted_calculations`、完成事实、证据和实际日期不变。新来源仍需正式复核，
`reviewed_calculations` 保留精确的最新计算 ID 和完整结果摘要。

`PayrollDisbursementBasis.declaration_fact_id`、外部完成事实中的精确引用、
材料逐行处置、`TaxImportDetails.payroll_result_digest` 及导出预览/manifest
保持原精确版本约束。补证后相关导出或材料仍可能需要复核；内容有效性和历史后补
显示遵循[历史来源与内容版本](history-content-versions.md)，统一业务查询遵循
[统一业务查询接入契约](unified-business-queries.md)。
