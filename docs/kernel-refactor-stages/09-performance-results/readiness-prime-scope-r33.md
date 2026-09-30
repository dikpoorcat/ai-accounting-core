# r33 简报准备 prime_select：只读范围审查

依据固定 r33 父进程 `.tmp/stage9-brief-parent-profile-r33.log/.pstats` 和当前同组源码。无新数据库诊断、测试或生产修改。cProfile 为含调用累计时间，不能当每个读取的独占墙钟。

## 实际链路与可归因成本

`Dashboard.brief → BusinessQueries._period_readiness → Periods.check_readiness → collect_current_readiness` 在本次父进程调用一次，整个 collector 约 182 ms；它为五个 `registry.readiness` 取 `Read` 并集，一次 `QueryReads.prime_select(requested)` 约 130 ms，再以约 60 次 `reads.select` 从同一 `_selections` 取已选结果，逐规则形成 `Context.trace` 的事实/计算 ID。随后当前月 `verify_sql_outcomes`、material/duplicate/report 检查仍有独立工作。

此 profile 的 `prime_select` 总计 69 次、`Store.select_many` 70 次，但 collector 直接调用的只有一次大 `prime_select`，其余绝大多数为空或已缓存范围。`Store.select_many` 在整个父进程的所有调用合计只有 8 次 `runtime.execute`（约 24 ms 累计），因此不能把 60 个声明读解释为 60 条逐类型 SQL。`Store.select_many` 的计算结果对象构造 `Store.calculation` 为 1,428 次／约 53 ms；它严格解析整个 `outcome` 并构造 `Calculation`。事实加载 `QueryReads.fact_versions` 在该选择器下 2 次／约 13 ms，`Store.facts` 1 次／约 1 ms（父进程所有路径的 `Store.facts` 合计约 19 ms）。总选择时间还含行枚举、分组与调度，不能将这些累计子项简单相加。没有记录到每一条声明的唯一命中 ID、单个历史事实大小，不能以旧 r28 的 1,979 首次事实数代替 r33 的实测。

`QueryReads.prime_select` 只请求尚未在当前 snapshot `_selections` 的 `Read`；事实经 `fact_versions` 按 ID 成功缓存。`Store.select_many` 两种 source 各使用一个批量选择 SQL，而事实按 kind 统一取模型子表，计算结果从 SQL 行构造对象后分配给请求 slot。计算行如果跨不同 slot 重复，当前 `{id: self.calculation(row) for row in rows}` 会在覆盖 dict 项前重复解码；但本组五注册器的同形 `Read` 已去重，r33 profile 只显示 1,428 次构造，**没有 ID 重复计数证据**，不把这个潜在机制列成已证热点。

## 五条规则的真实消费

| 注册器 | 已选内容被规则实际怎样用 | 仅身份/元数据的部分与决定 |
| --- | --- | --- |
| `assets_and_financing` | current 已由 31 收至 27 项。资产卡片/批次/启用/处置及贷款事实字段决定生命周期、付款分配和计息区间；历史 `asset_consumption` **计算结果**的 `period`、`values.asset_id`、`values.completed_months` 决定每张卡最新连续月。 | 对消费计算只取若干标量，仍须保留每个历史候选、修订选择及原结果损坏拒绝。先前 31→27 已物理减少无用的消费**事实**加载 1,128 ID，但完整简报 CPU 未见稳定下降；既有“只水合标量”私有判断无充分收益，不重试。 |
| `bank_accounts` | 开户和身份更正事实决定账户集；本月 statement/reconciliation 事实决定有无；计算的 `fact_id` 证明相应事实正式发布，身份 binding 结果还用 `values`。 | 多数发布计算只消费 `fact_id`，但当前 `select_many` 会严格解析完整 outcome；尚无本规则分项耗时/重复 ID 证据，不能改成只信 metadata。 |
| `platform_movements` | 本月 movement 事实的 source ID、消费者事实的 `movement_ids` 确定唯一处置；计算行的 `fact_id` 证明两边均正式。 | 计算值本身未被规则使用；同样需证明完整候选、坏 outcome 的拒绝范围才可设计窄来源视图，未验证净收益。 |
| `payroll_presence` | 工资档案事实字段决定员工/月覆盖；本月工资事实和计算 `fact_id` 比对，检测漏算或未发布。 | 计算 outcome 仅作当前完整对象构造，规则未用其 `values`；profile 范围和当月工资事实仍必要。暂无按该项归因的 CPU/字节。 |
| `financial_reports` | `freeze_report_references` 有意对 profile、carry、当月 classification/tax 的每条声明 `Context.select`，将存在与缺席以及选中 ID 纳入 close readiness trace；另一个 snapshot checker 才做报表金额和来源核验。 | 该 evaluator 只消费选择身份，不消费类型化正文，但现有普通页若遇坏正文会拒绝。更改读器须维持完整权威源认证、空集及损坏合同，不能只从可修目录或未核 typed 表取 ID。 |

`Context.trace()` 记录每条被规则显式选择的全部 `FactVersion`/`Calculation` ID，而不是只记录最终 issue 命中的行。`Periods._manifest` 把当前 trace 写成新闭月不可变依据；`read_indexes`、`integrity`、report flow/projection 会按保存的精确引用验证或消费。上述所需的候选存在性不能因为页面仅显示 issue 数而跳过。既有闭月和固定 v1 读取保存的原 trace；current 声明变化不能回写它们。

## 共用入口和处理决定

默认简报、准备/核对摘要、工作清单、`BusinessQueries.period_readiness`（CLI/MCP 共用）均可到达 `_period_readiness`／`collect_current_readiness`；`Periods.preview_close/close` 在正式完整源核验后也调用 `check_readiness` 并生成当前 trace。完整核验、修复和备份恢复按保存的 close 及各自独立源验证执行，不能借普通页 `QueryReads` 成功缓存；固定 v1 有独立历史源/manifest 解释，普通 current 页面不改变已保存 trace。`close_review_integrity_v1` 的当前 followup 计算也调用 `_period_readiness`，但这不授权修改 v1 保存依据。

处理决定：已证父进程约 130 ms 的一次 bulk select，其中完整计算对象构造约 53 ms；**没有**已证同快照大量重复同 ID 解码、逐声明 SQL 或可删的剩余 `Read`。目前只可提出“按规则仅消费 `fact_id`/少量标量的计算结果，若仍完整核对原 outcome 字节、摘要、依赖和错误拒绝，尝试一个局部窄视图”的未验证候选。它要先量对象构造占哪项、是否实际减少 CPU，并比较坏源/更正/空集/trace 与普通页和正式关账同义；若只是重复做完整原文校验而净成本不降则不实施。此前资产标量化、跨 worker 移动及缩历史候选的负面不重试。
