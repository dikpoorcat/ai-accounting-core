# r24 员工历史 eligible 读取范围：静态调用链审查

本审查只读生产代码和已保存的 `.tmp/stage9-grouped-main{12,48}-r24-all-pages.json`。未运行新的查询、测试或性能测量；48 月报告在原诊断时仍为 `measuring`。这些数据是带 SQL 插桩的工作量，不是 500ms 页面验收。

## 员工页的真实消费者

`Dashboard.employees` → `_employees` → `payroll_head_metadata` → `adopted_head_metadata`。后者核对截止月所有 close header 的 publication highwater，从权威 `calculation_publication` 取每个业务主体最后一个已采用的工资/期初工资结果头；SQL 返回 `subject_id, calculation_id, posting_period, fact_id, kind, period, line_count`。12→48 月，这条 eligible 查询返回 600→2,400 行、约 129,600→1,126,800 VM。它不是报告或所有五页共享调用：`Dashboard.brief` 的 `_brief_workforce_cost` 只要求 `posting_period=snap.period`，本月财务发生额另由 `metric_rows` 计算；资金、资产、报告页不调用这个工资头函数。

全部已采用工资头有明确用途。`_employees` 用每头的 `fact_id` 找员工身份；按员工选最新 `(posting_period,id)` 作 `representative_heads` 并核对应采用的计算；由所有身份构成 `known`/`registered_count`、员工状态、筛选与分页。若只取当前月工资，过去有工资但本月无工资且仍应显示在员工名单的人会消失。`kind/period/line_count` 分别用于身份/来源一致性、本月无凭证状态结果定位；`subject_id/id` 还用于精确打开某人的历史工资、付款摘要来源绑定。身份数量随业务月数增加不必等同人员数增加：同一人每月有独立工资主体，SQL 每主体一头，50 人 × 48 月可有约 2,400 头。因此不能直接把这 2,400 行判为多余。

但“需要每个头的身份”不等于“名单需要二次解码每个头的完整类型化工资标量”。开放月 `_employees` 在分页之前执行 `wage_scalars = scalar_facts(snap,wage_heads)`，按 kind 把全部历史头对应的 typed fact 表行 `SELECT f.*` 读入并 `decode_fields`；在普通名单阶段，`wage_identity` 只从这些行取 `employee_id`。同函数随后 `current_role_matches(fact_ids, 'employee')` 已对全部命中事实运行 `verify_hits`：从实际 typed 原文独立重建引用、核 digest，比较 `entity_reference_current` 后返回 `fact_id→employee_id`。`wage_identity` 仍以 scalar 的 `employee_id` 作为角色缺失时的 fallback，因此现在不能不经缺角色/歧义反例就删标量读取。已保存 48 月员工 SQL 显示两批 `fact_payroll` 宽行各约 2,450 行/462KB，另有 2,450 行 `entity_reference_current`；与上述双路径一致，但未通过逐函数动态归因证明每个解码字节都是重复。

真正完整工资结果及累计依赖只应给明确打开的 `employee_id`：`detail_subjects`、`wage_calculations`、`history_ids`、`picked_sources` 都在名单分页后使用；默认列表不组装每人的 `payroll_sources` 历史卡。付款摘要只对本页 `selected_ids` 从 `frozen_payroll_period_payments`/窄清偿 summary 取金额，再对实际命中工资主体核精确采用；个人源详情继续需要相应 fact 的 `period/payroll_period/component`、税申报与清偿字段。这里不是允许跳过来源核验，而是区分列表身份列与已打开的详情字段。

## 入口与历史规则

五页：员工页直接调用全历史 `payroll_head_metadata`；简报只调本月工资头；资金/资产/季度报告无该函数调用。`Dashboard.business_status` 针对传入的单个 `subject_id` 查当前 fact、当前 publication、期间会计采用及所请求明细，不借全体工资头枚举。员工页 `settlement_events` 明细也须传 `employee_id`，主体集合从该员工命中工资来源构成。

CLI、MCP 与 HTTP 均经 `service.py` 的 `dashboard_employees` 分发到同一个 `Dashboard.employees`，`command_schema.py` 也引用同一签名；未见 CLI/MCP 专属员工头选择器。关账、完整核验、修复、备份不调用 `_employees` 或 `adopted_head_metadata`；完整性使用 `verify_entity_references` 对权威所有事实重建并核记录，修复使用 `rebuild_entity_references`，其全域职责不能按页面分页缩小。历史 `content_v1` 没有独立 `_employees_v1` 页面算法；共用页面的 close header/section 经 `QueryReads` 路由固定历史 reader，正式历史完整重建另由 content-history 规则处理。若未来对员工普通页身份选择做改动，必须保持固定 v1 的关账高水位与真实来源检查；不应改写 v1 full comparer 来配合页面优化。

## 相似但不能混作同一缺陷的路径

- 资产页/简报的 `_asset_card_sources` 在分页前按所有资产获取、启用、处置及项目成本已选计算，`verified_scalar_facts` 核并读标量，才能构造资产身份、启停状态、active/pending 总数；资产页之后才按卡分页读取更详细的 source/members。它与员工“身份先于详情”相似，但资产所需 `asset_id/type` 与实际源核验范围不同。48 月资产页工作量中 close_reference 小结果大扫描由另一组负责，不能归咎于这条员工 eligible SQL。
- 资金页的 `FundsRead.__init__` 按期初/银行对账等 kinds 选截止月历史状态结果头，并取 metadata；这些历史来源可能影响旧未处理状态与当前余额。此前按本月银行 statement/reconciliation 缩范围有反例，静态审查不能认定为多余。资金事件 SQL 用 `json_each(outcome)` 取命中资金效应，不是直接复用工资头路径。
- `BusinessQueries._business_status`/`Dashboard.business_status` 按显式业务主体及期间取得最新 fact、publication、精确采用和选中集合；没有全公司工资头查询。若调用方指定单一工资主体，仍须保留其正确历史 adopted 来源和清偿详情，但不能据此推论整页名单可只取当前月份。

## 当前判断与未验证范围

已由代码确认：每个 eligible 头作为过去工资主体的身份枚举、采用身份与付款来源连接具有明确用途；默认列表不需要为每个人展开完整工资结果树。已由代码发现可进一步验证的窄重复：开放月 `current_role_matches` 已从真实 typed 内容核身份，但 `scalar_facts` 又预先读/解码全部工资头标量，普通名单多数仅消费其中 `employee_id`。这仍只是候选，不是可直接删除的生产变更：需要验证缺角色、身份纠错、无凭证结果、期初工资、闭期连续更正、同一人多批、损坏原文和显式个人详情对照；还需量名单列与详情列的实际解码分布。12/48 工作量已保存，48 完整核验及纯浏览器时延结果当时未完，不把增长定性为违反 500ms。
