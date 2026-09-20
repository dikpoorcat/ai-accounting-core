# 统一业务查询接入契约

本契约提供两个只读公共查询：`business_status` 按稳定业务身份说明当前事实、正式发布、
截止月份的核算与清偿；`period_readiness` 分开说明闭期冻结结论、当前关账业务条件和
当前后续事项。五页接入直接消费这些合同，不在前端或 Dashboard 适配层重建核算、
清偿、关账或文件关联规则。

## 一致快照与时间口径

公共查询各自在一个只读 SQLite 事务中完成。Dashboard 已持有连接时调用
`BusinessQueries._period_readiness(connection, period, as_of=...)`；Workflow 组合调用
`Workflow._query(connection, period, as_of=..., period_readiness=...)`。同一响应不跨连接
拼接准备状态。

`period` 是账面还原截止月份。`latest_fact` 与 `current_business_result` 表示查询时当前知识，
不按月份或 `as_of` 回放旧 current；事实所属月、实际 `posting_period` 和正式发布时间分别
保留。`as_of` 只用于中国自然日下的外部期限和完成可证明性，不改变账面事件、清偿截止或
当前正式发布头。

`business_status` 固定分开三个核算口径：

- `as_posted` 按实际入账月还原截至所选月末的凭证事件和无凭证状态结果。
- `current_business_result` 是当前正式发布链的有效结果；未发布事实和待重算状态不能替代它。
- `frozen_adoption` 只在所选月有独立关账并明确采用该业务时返回，保存正式发布、计算摘要、
  采用角色和证明；后续版本不能替换它。

`closure` 区分 `exact_close`、`covered_by_later_close` 与 `open`。后续关账覆盖只说明连续边界，
不能冒充本月独立批准，也不能生成 `frozen_adoption`。明确请求不存在的当月冻结内容时，
服务返回 `frozen_snapshot_unavailable`。

冲正事件保留冲正凭证自身版本及其 `reverses_voucher_version_id`；闭期更正按正式发布链在
指定开放入账月记录新旧结果差额。无影响复核保留原入账月和凭证拥有者，同时把新的采用
计算保存为当前业务依据。无凭证结果只按正式发布或直接冻结采用进入 `state_results`；依赖
计算仅用于来源追溯，不能因为出现在依赖图中就冒充直接采用。

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
- `covered_by_later_close`：
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
分别表达，不进入冻结结论或关账门禁。`current_followups.external` 只包含事项起止期间覆盖
所选月份的已登记义务；其他月份尚未完成的事项不计入所选月待办。页面应把冻结卡片和当前待办分区展示，不能把当前问题
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

适配层只做标签、排序和页面分组。已有五页响应字段需兼容保留时，可以从共享结果投影；
不得通过投影改变 `result_digest`、凭证方向、义务稳定键、完成状态或任务关联级别。

定向接入验证至少覆盖：闭期后冲正与替换、冲正后无分录结果、无影响复核不重复、无本月事件、
缺工资事实、两名同额工资的精确收款人、闭期未完成申报、季度报表期间范围、pending/failed
文件，以及普通查询不读文件或运行任务。
