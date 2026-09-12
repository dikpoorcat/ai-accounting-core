# 统一业务查询接入契约

T3，任务 ID `01a094b8-eea3-72b0-bfb3-a1aea4a77bdb`；架构任务 ID
`01a09416-a4e3-76b2-bf46-6387d8d47cfc`。本契约采用执行计划 v2，包含计划 v1 经架构审核
批准后的闭期冻结、当前后续事项、冲正及文件范围补充约束。

T3 提供两个只读公共查询：`business_status` 按稳定业务身份说明当前事实、正式发布、
截止月份的核算与清偿；`period_readiness` 分开说明闭期冻结结论、当前关账业务条件和
当前后续事项。T4 的五页接入直接消费这些合同，不在前端或 Dashboard 适配层重建核算、
清偿、关账或文件关联规则。

## 一致快照与时间口径

公共查询各自在一个只读 SQLite 事务中完成。Dashboard 已持有连接时调用
`BusinessQueries._period_readiness(connection, period, as_of=...)`；Workflow 组合调用
`Workflow._query(connection, period, as_of=..., period_readiness=...)`。同一响应不跨连接
拼接准备状态。

`period` 是核算截止月份。`latest_fact` 和 `current_publication` 始终表示查询时当前知识，
不按月份或 `as_of` 回放旧 current；两者自己的事实期间、核算期间和 `posting_period`
分别保留。`as_of` 只用于中国自然日下的外部期限和完成可证明性，不改变凭证选择、
清偿截止或当前头。

`selected_accounting.period_events` 只含 `posting_period` 等于所选月份的正式事件；
`through_period` 含截至该月的有效凭证影响和无分录状态结果。已有当月 close manifest 时
只取其精确冻结引用，否则取当前有效正式发布。无本月事件时保留空数组；截至期没有任何
正式结果时才使用 `not_established`。

冲正事件保留冲正凭证自身版本，同时以 `reverses_voucher_version_id` 指向的原凭证计算
作为反向核算依据；替换事件采用更正计算。无影响复核在开放期只出现一次凭证影响，
凭证版本保持原精确版本而计算采用当前正式复核；闭期仍取 manifest 原计算，当前复核仅在
`current_publication` 出现。可按当前发布或冻结采用证明选取的无分录结果进入 `state_results`，
不标为未发布。
依赖计算只作为来源追溯，不自行成为入账事件。

旧 manifest 的 `calculations` 可能同时包含无分录发布结果和其历史依赖，不能仅凭出现于清单
就认作独立有效状态。若冻结材料不能唯一证明采用版本，查询保留候选和精确追溯目标，
局部标记不可建立；这些候选不参与金额累计，也不用当前头补写历史结论。

冻结采用证明按每份 manifest 分别建立，只读取其完整计算集合和不可变依赖：

- 集合内未被其他计算依赖、且计算期间等于关账期间的图顶层计算，证明为
  `manifest_lineage_root`。
- manifest 精确凭证条目明确引用的计算，证明为 `manifest_voucher_root`；无分录更正
  产生的冲正凭证也可保留这一采用证明。
- 期初包可按 `manifest_opening_member_adoption`（合同版本 1）建立领域采用证明：先在
  完整 manifest 图中独立证明一个 `opening.MODELS` 明细根，核对它的直接计算依赖、
  精确成员事实及原样复制的结果值。明细必须符合 `calculate_detail` 的固定结果外形，
  无普通分录、余额影响或其他额外输出；不能用包反证明细形成循环。
  包的声明成员、冻结成员和依赖中的全部期初事实必须精确双射，按实际事实种类重计的
  分类数量与两份 counts 相符，全部成员 opening_lines 拼接等于包 opening_lines。
  首封存月份必须等于建账期且无更早封存或凭证；仅以该 manifest 的精确凭证版本借贷
  总额加包 opening_lines，按科目核对冻结 trial_balance（纯零空行归一化）。金额一致
  只是必要核验，不能替代身份锚点，不能据此证明交易方或余额关系，也不豁免原有完整性
  与关系核对。证明保留 close digest、包及明细的精确计算／事实版本和 result_digest。
  同一完整范围内的全部获证版本仍先收集再判唯一；多个获证包或同身份已采用明细冲突
  保留未知。缺少上述条件的旧包仅保留追溯，任意承接依赖不自动获得此证明。
- 即使只有一个候选，只要它被其他计算作为依赖引用且没有明确的根证明，也返回
  `unestablished`、`reason=manifest_state_adoption_not_proven` 和精确追溯候选，不累计。

可证明为根的正常无分录及期初状态仍正常采用。某结果同时是根和依赖时，旧 manifest
可能没有足够信息证明根角色，查询保留局部未知；不借查询时 current、处置记录先后或候选
数量反推历史采用，也不修改原 manifest。当前跟进仍独立采用当前正式头。

## 独立状态

页面必须独立展示以下状态，不能用其中一项代替另一项：

| 状态 | 权威字段 | 含义 |
| --- | --- | --- |
| 资料齐全 | `period_readiness.*.materials` | 当前清单、材料逐项处置和覆盖检查 |
| 核算完成 | `period_readiness.*.accounting` | 应建事实存在，且相关事实已正式发布、无 pending |
| 关账业务条件 | `period_readiness.*.close_requirements` | 快照、银行及领域关账检查；不包含付款、全部申报或文件任务 |
| 实际付款与其他清偿 | `business_status.settlements` | 截止月份按义务稳定键累计的来源、付款、非现金及其他清偿 |
| 申报及其他外部事项 | `business_status.external`、`period_readiness.current_followups.external` | 按真实完成依据得到的 `completion_status` |
| 文件生成 | `file_jobs` | 冻结任务计划、执行状态和执行时校验结论；不表示付款或申报完成 |

`Workflow.status/steps` 是兼容字段，闭期可继续显示 `closed`。新页面判断实际完成只使用
义务的 `completion_status`；`accounting_closed` 只说明核算边界。缺少当前完成依据时即使
月份已关账也仍显示待办。

有效工资档案覆盖月份但没有工资事实时，`accounting.issues` 和关账检查来源中均保留
`missing_payroll`，聚合问题只出现一次。可调用工资准备入口不表示事实已建立或已发布，
查询也不会自动准备工资。

前期未关闭只阻断关账顺序，不清空当前月份的业务检查。Workflow 的资料、工资和交易步骤
仍返回实际缺项，关闭步骤保留顺序阻断；正式关账入口的异常顺序不变。

## 闭期与当前后续事项

`period_readiness` 的顶层固定分为 `closure`、`frozen_readiness`、`readiness` 和
`current_followups`：

- `exact_close`：`closure={state,digest}`。冻结结论只投影当月 manifest 实际保存的内容；
  `frozen_readiness={status:"ready",source:"exact_period_manifest",...}`，其中 `readiness`、
  `inventories`、`material_coverage`、`previous_close_digest` 每项均包装为
  `{status:"recorded",value}` 或 `{status:"not_recorded"}`；`readiness` 为 null。
- `sealed_by_later_close`：
  `closure={state,sealing_boundary,sealing_digest}`，记录最早更晚闭期边界；当月没有
  manifest，因此 `frozen_readiness={status:"unavailable",reason:"no_exact_period_manifest"}`，
  `readiness` 为 null，不借后来 manifest 补造。
- `open`：`closure={state:"open"}`；没有当月或更晚 close，`frozen_readiness` 为 null，
  `readiness` 返回当前关账业务条件。

三种情况都返回 `current_followups`，其知识口径为当前，且
`affects_frozen_readiness=false`。后补材料、未发布或 pending、缺工资事实、清偿、实际申报
和文件任务不会改写历史冻结结论。`current_followups.settlements` 只表示查询时当前知识下
所选月份涉及业务的清偿跟进，纳入这些业务后来已正式发布的付款或更正，并单列当前清偿
范围与截止期间；不混入无关的后来业务。它与 `business_status.settlements` 的历史月末余额
分别表达，不进入冻结结论或关账门禁。页面应把冻结卡片和当前待办分区展示，不能把当前问题
渲染成原关账失败，也不能因 legacy `closed` 清空当前外部事项。

## 来源与文件任务

`trace_targets` 只给可直接传入现有 `trace(calculation_id, voucher_version_id=...)` 的精确
目标。历史凭证追溯不使用最新计算替换；管理档案后补字段与冻结字段并列，并保留每个字段的
`field_sources`、`recorded_at` 和采用口径。

`file_jobs` 只按冻结计划中已声明的结构关联：

- `payment_export` 使用 `plan.rows[*].sources[*]` 的稳定业务、计算和义务引用；
- `tax_import` 只解析顶层 `plan.source_versions`，并只在事实与计算版本表中核对；
- `report_export` 只用 `plan.report_fact_ids` 建立直接业务关联，季度 `plan.period` 与
  `plan.source_closes` 只能建立期间范围关联。

查询不递归扫描任意 JSON，不把 `excluded_sources` 当采用来源，不关联公司备份，不读取或
重验外部文件。`succeeded` 只表示任务执行时完成校验；当前下载可用性继续调用既有专用
校验入口。

任务计划的集合或关联字段损坏时按任务隔离：保留能够证明的业务或期间关联并附局部问题；
无法证明关联则跳过或标记未知。损坏的无关任务不能中断普通业务查询。

## 五页接入

纯财务位置分类由 `query_semantics.classify_financial_position` 提供，已由 Dashboard 与
Reports 两个窄消费者共同调用。分类缺少精确交易方或科目映射时，仅把受影响行和派生合计
设为 null，`equation_valid` 也可为 null，同时返回 `complete=false` 和结构化 `issues`；
已证明的其他科目继续返回，消费者不得把 null 当零或退回整科目净额。共享关系解析器同样
已由这两个窄消费者复用；经营简报期间准备已改为读取 `BusinessQueries` 的同连接结果。
报表默认行筛选保留任何金额列为 null 的行，以“—”表示未知；不能因已知列恰为零而隐藏缺项。

经营简报以 `period_readiness` 显示资料、核算、关账和当前待办，以 `business_status` 的
事件、清偿与来源展开具体业务。资金页和员工页使用同一 `settlements`，不得再按同额、姓名
或同月猜付款归属；人员收款人取冻结 allocation 的精确身份。资产页按稳定业务和精确来源
展示取得、启用、摊销及清偿，不用凭证金额反推生命周期。财务报表页复用同一期间准备结论，
但报表自身勾稽、接续资料和导出预览仍由 Reports 合同判断。

T4 适配层只做标签、排序和页面分组。已有五页响应字段需兼容保留时，可以从共享结果投影；
不得通过投影改变 `result_digest`、凭证方向、义务稳定键、完成状态或任务关联级别。

定向接入验证至少覆盖：闭期后冲正与替换、冲正后无分录结果、无影响复核不重复、无本月事件、
缺工资事实、两名同额工资的精确收款人、闭期未完成申报、季度报表期间范围、pending/failed
文件，以及普通查询不读文件或运行任务。
