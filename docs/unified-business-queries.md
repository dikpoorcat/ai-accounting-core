# 统一业务查询接入契约

本契约提供两个只读公共查询：`business_status` 按稳定业务身份说明当前事实、正式发布、
截止月份的核算与清偿；`period_readiness` 分开说明闭期冻结结论、当前关账业务条件和
当前后续事项。完整合同供 AI 会计通过 CLI/MCP 查询、核对与追溯，保留收付款、跨月清偿、
历史采用和文件关联能力。

老板看板使用独立的生成响应合同，从内核共享语义投影必要金额、业务状态和本人待办；
默认读取不组装完整期间准备、来源证明或技术诊断。五页及其按需业务详情不在前端重建
核算、清偿、关账或文件关联规则，展示取舍也不改变服务端的必要内容核验。

## 一致快照与时间口径

公共查询各自在一个只读 SQLite 事务中完成。明确需要完整期间准备的内部消费者通过
`BusinessQueries._period_readiness(connection, period, as_of=...)` 复用已有连接；`workflow`
通过 `Worklist.query` 复用同一期间读取，并组合公司级外部事项和任务。看板窄投影同样保持
一致快照，经营简报默认读取不调用完整期间准备。同一响应不跨连接拼接准备状态。

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

完整查询必须分开返回以下状态；看板只展示当前页面所需的投影，不能用其中一项代替另一项：

| 状态 | 权威字段 | 含义 |
| --- | --- | --- |
| 资料齐全 | `period_readiness.*.materials` | 当前清单、材料逐项处置和覆盖检查 |
| 核算完成 | `period_readiness.*.accounting` | 应建事实存在，且相关事实已正式发布、无 pending |
| 关账业务条件 | `period_readiness.*.close_requirements` | 快照、银行及领域关账检查；不包含付款、全部申报或文件任务 |
| 实际付款与其他清偿 | `business_status.settlements` | 截止月份按义务稳定键累计的来源、付款、非现金及其他清偿 |
| 申报及其他外部事项 | `business_status.external`、`period_readiness.current_followups.external` | 分别返回 `actual_completion_status` 与 `basis_review_status` |
| 文件生成 | `file_jobs` | 冻结任务计划、执行状态和执行时校验结论；不表示付款或申报完成 |

`workflow` 版本 1 的 `sections` 分为六类资料与核算、关账、实际办理、文件交付，没有旧步骤或兼容别名。实际申报已完成而账务未核对时，两种状态同时保留；核算关闭不能消除当前外部待办。省略月份时选最早已有来源的开放待处理月，已关闭月份的问题由开放月承接，公司级未办事项和活动／失败文件任务始终保留。

有效工资档案覆盖月份但没有工资事实时，`accounting.issues` 和关账检查来源中均保留
`missing_payroll`，聚合问题只出现一次。可调用工资准备入口不表示事实已建立或已发布，
查询也不会自动准备工资。

前期未关闭只阻断关账顺序，不清空当前月份的业务检查。工作清单六类业务
仍返回实际缺项，关账区保留顺序阻断；正式关账入口的异常顺序不变。

## 闭期与当前后续事项

`period_readiness` 的顶层固定分为 `closure`、`frozen_readiness`、`readiness` 和
`current_followups`：

- `exact_close`：`closure={state,digest}`。冻结结论只投影当月 manifest 实际保存的内容；
  `frozen_readiness={status:"ready",source:"exact_period_manifest",...}`，其中 `readiness`、
  `inventories`、`material_coverage`、`previous_close_digest` 每项均包装为
  `{status:"recorded",value}`；必需依据缺失属于内容错误。当前关账准备 `readiness` 为 null。
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
所选月份的已登记义务；其他月份尚未完成的事项不计入所选月待办。消费者应区分冻结结论和
当前待办，不能把当前问题解释成原关账失败，也不能因核算关闭清空当前外部事项。

完整 `period_readiness` 使用严格响应版本 1。`current_followups.settlements` 返回完整计数、未结数量及金额摘要，未知金额保持 null；业务详情通过明确的清偿查询取得。期间任务不携带原始结果袋，保留精确来源、执行时核验和局部问题。

## 来源与文件任务

`trace_targets` 只给可直接传入现有 `trace(calculation_id, voucher_version_id=...)` 的精确
目标。历史凭证追溯不使用最新计算替换；管理档案后补字段与冻结字段并列，并保留每个字段的
`field_sources`、`recorded_at` 和采用口径。

`file_jobs` 只按冻结计划中已声明的结构关联：

- `payment_export` 使用 `plan.rows[*].sources[*]` 的稳定业务、计算和义务引用；
- `tax_import` 只解析顶层 `plan.source_versions`，并只在事实与计算版本表中核对；
- `report_export` 只用 `plan.report_fact_ids` 建立直接业务关联，季度 `plan.period` 与
  `plan.source_closes` 只能建立期间范围关联。

业务详情不递归扫描任意 JSON，不把 `excluded_sources` 当采用来源，不关联公司备份，不读取或
重验外部文件。`succeeded` 只表示任务执行时完成校验；当前下载可用性继续调用既有专用
校验入口。

任务计划的集合或关联字段损坏时按任务隔离：保留能够证明的业务或期间关联并附局部问题；
无法证明关联则跳过或标记未知。损坏的无关任务不能中断普通业务查询。

公司工作清单另外列出全部活动和失败任务，以及各类最近的成功产物。关账备份保留真实月份，手工备份保持公司级。公开错误使用稳定 `error_code/error_message`，原始异常仅留本机诊断；任务成功不代表现有文件仍可下载。

## 五页接入

经营简报默认读取经营及资金摘要、待收待付、重要变化和本人待办，不读取完整
`period_readiness`。开放月份有有效核对请求时，点击本人待办才按精确预览读取月度核对。
独立凭证展示已恢复“按业务／按凭证”、分录展开及“分页／全部”：公开凭证号、科目、
往来对象和借贷金额可以展示，内部版本、来源证明和诊断图不进入页面。

资金页按实际收付与业务义务展示账面和流水，员工页保留人员清单、本月工资、劳务及截至
月末未付。员工页面不读取历月工资来源或逐笔工资付款历史，也不缓存这些已退出的详情；
劳务收付款事项仍可按需展开。服务端仍以精确义务和冻结 allocation 的人员身份核对收付，
历史义务继续参与截止月余额，不能按同额、姓名或同月猜付款归属。完整工资历史、清偿事件、
采用来源与技术依据由 AI 会计通过 CLI/MCP 读取和核验，页面退出不删除这些核心能力。

资产页按稳定业务和精确关联展示取得、启用、摊销及清偿，不用凭证金额反推生命周期。
财务报表页按 Reports 合同判断勾稽、接续资料和导出预览，已恢复三张报表的税务局模板格式
及生成下载；格式切换沿用同一份精确金额，预览和导出采用同一资料。财务位置分类由
`query_semantics.classify_financial_position` 提供；分类缺少精确交易方或科目映射时，受影响
行和派生合计保持 null，`equation_valid` 可为 null，并返回 `complete=false` 与结构化 `issues`；
已证明的其他科目继续返回。报表以“—”显示未知，默认行筛选保留
任何金额列为 null 的行，不能当零或退回整科目净额。完整财务位置留在报表及核心核验中。

五页消费各自的生成合同及有界分页集合，技术依据不由页面读取。适配层只做业务显示投影、
标签、排序和页面分组，不改变凭证方向、义务稳定键、完成状态或任务关联级别；摘要和快照
关联留作请求校验。同公司、同期间的模式和报表格式切换只更新本地显示；账户、员工和资产
筛选需要读取时只替换同快照的对应集合，保留汇总及无关区域。公司、期间或快照真正变化时
重新读取并取消旧请求，不能拼接不同版本的分页。

定向接入验证至少覆盖：闭期后冲正与替换、冲正后无分录结果、无影响复核不重复、无本月事件、
缺工资事实、两名同额工资的精确收款人、闭期未完成申报、季度报表期间范围、pending/failed
文件，以及普通查询不读文件或运行任务。
