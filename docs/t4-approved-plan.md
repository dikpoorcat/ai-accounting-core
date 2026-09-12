# T4 已批准实施计划

任务 ID：`01a09521-a902-7ca2-9fbb-2d9ffa74f194`。
架构任务：`01a09416-a4e3-76b2-bf46-6387d8d47cfc`。
架构已明确批准 v1＋v2，以下 v2 限定优先。

# T4「有界读取与看板接入」详细计划 v1

任务 ID：`01a09521-a902-7ca2-9fbb-2d9ffa74f194`
架构审核任务：`01a09416-a4e3-76b2-bf46-6387d8d47cfc`
状态：仅规划，待架构审核；尚未修改文件、运行测试或操作业务数据库。

## 1. 目标、基线与关键决策

使现有五页真正使用 T3 业务状态、清偿和期间准备合同，消除每页构造全部历史快照、报表全载历史分录及共享查询逐项读取。保留既有布局，只调整必要的状态分区、明细分页、追溯入口及上下文交互。

已读共同协作约定、AGENTS、FRONTEND 及 T1—T3 契约；T3 r2 记录的 22 个文件哈希与当前源码完全一致。首次实现、r1、r2 的证据按各自代码状态和覆盖范围复用，不合并通过数量。

本计划明确提出两项需架构放行的接口／结构调整：

- 公司库前向升级到 v10，目录库仍 v3；增加最小的精确引用索引，解决 manifest、jobs、audit 数组引用无法反查的问题。不改写任何 v1—v9 结构合同。
- 五页 Dashboard 响应采用 v2，并同步更新内置前端；保留现有字段名和金额含义，明确分页集合及准备状态投影。CLI/MCP 的完整 `business_status`、`period_readiness` 合同保持不变；不保留另一套无界页面实现。

“有界”按实际需求定义：汇总扫描必要的月度汇总行；完整业务状态按相关业务和精确依赖计成本；分页只展开本页实体、来源及必要依赖。必须完整检查的冻结集合、真实相关历史及公开来源清单不能截断，不承诺这些输出的常数成本。

## 2. 共享读取与 v10 引用索引

**统一取数，继续使用已有规则。**

在现有 BusinessQueries 周边建立一个同连接、请求级读取上下文，不新增框架或跨请求缓存。先选择期间、稳定业务身份和页面键，再按 kind 批量读取事实及子表、计算、凭证行、依赖边、档案和来源时间。公共业务查询提供同连接内部入口；Dashboard、Reports、Workflow 组合响应复用该上下文，不能循环调用会各自开连接的公共查询。

共享关系解析器保留现有规则，改为批量提供精确来源、共享依赖图及已解析节点，消除重复 SQL 和重复祖先遍历。候选筛选同时覆盖既有明确来源声明，不能因缺依赖边漏掉应返回的 unresolved 项。汇总与明细使用同一选取器和 reducer；汇总模式不构造最后会丢弃的 movements、facts 或整份业务详情。

**引用索引只存原始引用，不存业务结论。**

追加三类窄表及其正反索引：

- 冻结引用：保存每份 close 的完整 calculation 成员、精确 voucher version 及原声明 calculation_id，并索引 Reports／展示实际使用的固定路径事实引用。保存原路径、出现位置、所属 close 和来源摘要；字段“引用角色”仅表示原 JSON 路径，不表示根或已采用。
- 文件任务候选引用：仅提取 T3 明确合同中的 subject、fact、calculation、义务及期间范围。仍由原文件关联解析器核验是否直接关联、期间关联及局部错误；不索引 excluded_sources、不递归猜 JSON、不关联公司备份。
- 审计来源出现记录：复用 provenance 的受支持 action／结果解析，索引 source_type、精确 source_id、audit_id 和出现位置。同一批次重复不能去重；唯一性、时间格式与未知管理来源仍按 T2 判断，时间取原 audit。

每份支持的源记录保存已处理标记与原摘要，包括零引用或局部损坏的记录。索引生成器由迁移、正常写入和完整性校验共用，不能另写一份来源规则。索引损坏或缺失不能悄悄按“没有业务”返回，也不在普通读取中自动回填或退回全历史扫描；使用现有完整性错误路径定位，修复属于显式维护。

v9→v10 在同一前向事务内建立、回填并核验索引，再记录结构版本；冻结正文、摘要、事实、凭证、T2 内容依据原样保留。运行时在关账（含批量关账）、任务入队及受支持审计原事务内同步写索引；失败整笔回滚，幂等重放不增加引用。原源和索引之间的留存约束随 v10 冻结；便携包完整性验证覆盖引用目录与原源的一致性。既有 v1—v9→当前的升级链仍可使用。

**冻结和当前选取不混用。**

- 先通过精确引用定位相关 close；对每份参与证明的 manifest，读取其完整 calculation 集合及相关不可变依赖边，再按 T3 的 manifest_lineage_root／manifest_voucher_root 规则判断。分页、单主体过滤和缓存均不能改变证明集合。
- 旧材料不能证明采用时，保留 unestablished、原 reason、全部精确候选及 trace_targets；不累计、不看当前头、不按候选数量或依赖深度推断。
- 历史 settlements 截止所选月；current_followups 从 T3 规定的截至期相关业务集合出发，纳入精确关联的后来付款、更正及当前正式发布，排除无关后来业务。两种结果分别计算、分别标注。
- 资料、核算、关账条件、付款、外部完成和文件任务仍由各自权威字段决定。unknown、null、局部问题及无本月事件保留，不把没有返回某页明细当作已完成。

## 3. 看板与报表的实际读取路径

**核算汇总。**

普通科目及现金流汇总复用 monthly_account、monthly_cashflow、opening_account；准确闭期采用该 manifest 的 trial_balance，开放区间按既有封存边界接续必要月度增量。只有证明属于该区间的期初才可进入汇总。无当月 manifest 时保留 sealed_by_later_close／open 的真实含义，不制造冻结结论，不使用无期间维度的当前 balance 代替历史。

往来重分类、税款退抵、内部转账及现金分类仍走已有共享规则。只读取这些分类需要的精确来源行，批量／流式归集；不能先按科目净额抵销再找交易方，无法归属的零净额行也必须保留 unknown 和 issues。

**Reports。**

普通账户余额用上述汇总；本季／本年利润和现金流只读取本年所需分类明细，历史期初、往来与显式接续资料只读取其必要来源。完整 result_digest、calculation_hash、source_closes、report_fact_ids 和导出预览依据不删减、不改用核算签名。Reports 的 opening 选取接入 T3 冻结采用证明，禁止把清单中的依赖自动当作有效期初。

季度页面在一个 BEGIN 读事务内生成 open report、closed export preview、来源详情和各相关月份的 period_readiness。抽取 Reports 自己使用的“适用建账口径＋逐月闭期覆盖”检查供 context 复用：context 不再逐季度生成三表，quarters.complete 保持原 period／closed_periods 语义，不能简化为季末 closed。

**五页按需取数。**

- 简报只取财务、资金、人员成本和资产的必要汇总，不再调用资金、员工、资产的完整页面构造器。业务列表可展示无分录结果与不可证明候选，凭证列表保持精确历史版本。
- 资金保留全公司汇总与账户筛选明细两种范围。先在选定的正式 effect／凭证集合筛账户、选页面键，再展开摘要与来源；银行流水按已采用 fact ID 和规范化 entry 子表分页，不通过完整 Store.fact 先加载整份流水。
- 员工、资产先选择实体键，整域统计后取本页详情；工资、奖金、劳务、项目及资产生命周期来源由共同选取和关系结果投影。工资 allocation 采用精确收款人，整批或项目付款不分摊到单卡。
- 所有入口同一响应只用一个只读事务，包括 epochs、封存边界、汇总、准备状态及来源时间；普通读取不运行任务、不读取证据正文、不验证外部文件。

## 4. 页面契约、分页及上下文修正

Dashboard v2 保留现有金额和汇总字段，金额仍为整数分字符串，允许未知的字段明确为 null。新增共用 TypeScript 契约放在现有 api 目录，覆盖 T3 状态、稳定业务／义务身份、局部 issues、field_sources、trace_targets 和后补口径。

页面 period_preparation 是显式的 T3 投影：保留 closure、冻结 recorded/not_recorded 状态、当前业务条件、当前跟进状态及完整计数／金额，明细另分页；不声称它是完整公共 period_readiness。旧清偿展示字段从同一结果映射。需要完整单业务内容时，增加只读 Dashboard business-status 入口，接受 company_id、period、subject_id 和 expected_version，返回同连接的现有业务合同；其成本按该业务实际输出和完整依赖计算。

分页默认 100、最大 500，延续现有 brief/funds 限制。brief 保留 after_number；funds 保留三种游标和账户筛选。员工、资产及嵌套来源／当前跟进集合增加独立游标与 page 元信息（total_count、returned_count、has_more、next_cursor），由现有页面接口通过 section、entity_id 选择集合。完整总额、人数和问题计数独立返回；员工／资产现有筛选下推至后端，排序保留现有顺序并以稳定 ID 打破并列。每次只展开 limit+1 个页面键，不能先生成全部详情再切片。

游标绑定公司、数据库、期间／季度、as_of、集合、实体、全部筛选及 snapshot_version；版本保持现有 epochs 并发语义。文件任务页另绑定相关任务状态和结果摘要的 collection_version，防止工作器更新未推进 epochs 时拼接旧页；它不替代 snapshot_version。上下文或版本变化返回现有 dashboard_snapshot_changed，前端清空旧页并重载，不拼不同版本。对 route.query.voucher 增加同期间精确凭证定位，沿用现有摘要投影和 trace 入口；停止 while(loadMore) 搜索所有前页。

五页在现有栏目内分别展示“所选月末核算”和“当前后续事项”。exact_close 与 sealed_by_later_close 分别表达，后补待办不称为原关账失败；旧欠款后来已付应在当前跟进反映。报表准备与导出仍由 Reports 决定，文件成功不等于付款或申报完成。未知卡片和金额行不隐藏，null 参与派生展示时继续传播 null，不经 fen(null) 变成零。

所有页及共享 context 使用单调请求世代并捕获公司、期间／季度和筛选。每次选择变化先使旧请求失效、abort、清理旧数据和游标，再判断是否复用响应；成功、失败、finally、刷新后的选择和路由回填均核验世代。员工 A→B→A 的旧 A 缓存不能绕过 B 请求失效化，忽略 abort 的迟到响应也不能覆盖 A。卸载和切公司同样使世代失效；不引入新状态管理依赖。

## 5. 验证、实施顺序与交付

批准后顺序：保存自身开工差异／哈希边界 → v10 引用目录与同连接批量读取 → BQ／Reports／Dashboard 有界取数 → 五页接口和交互 → 定向验证及结果回传。保留其他任务已有改动，仅本任务写入批准范围；不提交无关改动，不操作真实资料／业务库，不部署。

只执行直接覆盖变更风险的验证，使用仓库 .tmp-kernel-venv：

1. **语义定向回归**：复用 T3 r2 冻结采用、r1 相关后来清偿、同额员工精确收款人、局部任务损坏及 null 证据作为用例基础。因选取器／resolver 被改动，重跑相应测试；另覆盖 Reports 期初采用与看板投影，不重跑 T3 首次整批。
2. **迁移与同步**：合成 v9→v10，核对原正文／摘要／凭证／T2 依据不变；补测现有旧版本升级入口、回填中断回滚、空引用、重复 audit occurrence、损坏候选隔离、普通和批量关账、三类任务入队、幂等重放以及便携验证。仅覆盖新增索引的写入接缝，不重测整个核算内核。
3. **历史与报表**：闭期后无影响复核、冲正／替换／无分录、根兼依赖、无本月事件、后来付款、未知往来抵销为零；优化前后在有效基线上比较金额、null、issues、精确追溯和报表预览。错误旧口径以 T1—T3 合同为准，不能用旧输出作为错误金标准。
4. **成本证据**：固定目标月、页面和相关业务，增加 10 倍无关历史凭证、任务及审计；冷请求记录 SQL 次数、SQLite VM 工作量、解码对象数和峰值内存。要求不展开无关 outcome／fact／audit/job 载荷、不出现逐实体 SQL，页面详情展开受页长与必要依赖约束；引用定位使用索引。另加长真实相关链及多个共享祖先，证明依赖完整且不重复遍历。必要完整 manifest 集合、月度汇总行、来源输出与当前业务规模单独计量，不能用缓存或总 SQL 条数掩盖。
5. **分页／并发／前端**：跨页筛选、汇总不随页长变化、旧游标失效、精确凭证直达不扫前页；同响应读到一半时另连接提交，证明 context／报表／来源不混版本。使用现有 node:test 与 Vue harness 测 A→B→A、忽略 abort、切公司、卸载和刷新期间再选择；合成 fixture 一次覆盖五页历史／当前分区及未知值实际渲染。运行受影响前端测试和 npm run build（已含 type-check；仓库无 lint 命令），Python 仅变更文件静态检查。
6. **交付**：提交 T4 查询边界及 v2 衔接文档、变更清单、源码哈希和定向验证记录；每批记录代码状态与适用性，复用证据单独引用。主动回传本架构任务审核，处理修订后再交接 T5；不自行宣布全项目验收。

规划阶段到此为止。请架构任务审核 v1，尤其确认 v10 引用索引与 Dashboard v2 的批准范围；明确放行且离开计划模式前不实施。

---

T4 计划 v2 补充（只限定 v1 以下边界，其余沿用；未实施、未预跑测试）

任务 ID：01a09521-a902-7ca2-9fbb-2d9ffa74f194
架构任务：01a09416-a4e3-76b2-bf46-6387d8d47cfc

1、v10 固定范围
公司 v10／目录 v3，仅三类可重建原始引用目录及必要普通／表达式索引：
- close：用于“稳定业务／精确计算→相关 close”和“close→完整成员”查询。提取 calculations[*]、vouchers[*].id 与同条目原声明 calculation_id；另限现有消费者实际使用的 readiness.financial_reports.facts[*]、management_snapshot.typed_facts[*].id、profiles[*].id、management[*].id、payees[*].id。后四类均位于 management_snapshot，保留原路径／出现位置和精确身份；单值字段直接读所选 close，不泛化索引。完整成员供 T3 根证明，不保存 adopted/root 等结论。
- audit：用于任意精确来源跨期确认时间反查。复用 provenance._result/_references，范围仅 confirm_fact／recording_correction 的 fact_id、confirm_facts.results[*].fact_id、save_display_profile 的 id、payee 的 payee_revision_id。保持现有结构验证、actor 包装和重复出现；management 不补推引用。
- jobs：用于跨期单业务直接来源和期间范围查询。仅 payment_export 的 plan.rows[*].sources[*]、plan.period；tax_import 的顶层 plan.source_versions[*]、plan.period；report_export 的 plan.report_fact_ids[*]、plan.source_closes[*].period 和 plan.period 的季度范围。引用类型／义务及来源身份只做候选键。业务直接关联不能以月份替代；不读 excluded_sources、备份或任意 JSON 路径。原解析器仍决定关联、局部错误和状态。

2、完整性责任修正
processed marker 只记录提取曾完成，不能证明反向索引没有遗漏。
- 合法写路径：同一事务由同一提取器同步源和目录；来源关联、出现位置唯一性及写保护约束防止孤立／非法追加或改写，失败回滚。同步接缝按实际源 INSERT 覆盖：Engine._write 的受支持 audit，Display.save_display_profile 和 Exports.save_payee 的实际审计路径，Periods 普通/批量 close 及内部直接 INSERT，Exports／TaxImport／Reports 的任务入队；不只在一个通用出口假定覆盖。
- 迁移及显式便携完整校验：逐原源重提取，比较完整多重集合（含重复及零引用记录），发现遗漏、多余、错绑和摘要不符。原记录局部坏字段按 T3 原合同保留可证明关联、问题或跳过，不能因此一律拒绝升级。
- 普通查询：只核对命中的引用、原固定路径与精确目标，报告已发现的索引不一致；不在 Store.connection、verify_schema 或冷页面重算全历史。绕过写保护删去索引行但保留 marker，导致来源根本未命中时，局部查询不能保证发现，须由显式完整校验发现。此限制明确写入交付文档。结构缺表／缺索引／缺触发器和目录损坏不转成业务 unestablished；来源无法建立仍按业务合同返回。

3、端点版本及分页集合（沿用 /api/dashboard/，无 /v2 路由或双栈）
| 端点 | 版本 | 有限 section／分页边界 |
| --- | --- | --- |
| context | 保持 2 | 月份／季度元数据保持原形 |
| brief | 1→2 | vouchers、businesses、open_items、settlement_events、external_followups、file_jobs；准备状态改为明确摘要投影 |
| funds | 1→2 | accounts、movements、statements、investment_products、investment_events；已有三游标兼容，新增持续增长实体页和共同清偿字段 |
| employees | 1→2 | employees、payroll_sources、labor_sources、settlement_events；筛选统计完整 |
| assets | 1→2 | assets、projects、source_history、settlement_events |
| quarterly-report | 保持 1 | 固定三表行、既有勾稽／接续／导出结构完整；同连接附加期间准备摘要，不把固定行分页 |
| business-status（新增只读） | 1 | 单业务状态、少量义务、单项字段来源证明包完整；events、settlement_events、source_history、file_jobs 等增长历史按页投影；CLI/MCP 完整合同不变 |

v1 所述新增 business-status 返回方式按此限定：网页历史可分页，不能暗中把分页结果冒充完整公共 business_status。
section 在端点内固定枚举，不开放任意集合。employees 使用 employee_id，assets 使用 asset_id，项目使用 project_id，业务详情使用 subject_id；服务器按精确关系映射，不能互换。
实际前端 API 消费者运行时校验端点 schema_version 和必需 page 元信息（不只改 TS 类型），不匹配明确要求刷新；页上分别显示完整总计、筛选总计和已加载条数。固定报表行、单笔少量义务、每项精确追溯／字段来源包不拆页。公司／数据库／期间／as_of／筛选／epochs 及 jobs collection_version 游标约束沿用 v1。

4、读取口径补充
本年分类明细按凭证 posting_period 限期，跨年更正仍回读往年精确原计算和必要依赖。历史损益余额、年初／季初现金分别接续并避免期初重复；往来按“账户＋稳定交易方”保留历史净额，科目净额为零不排除其正负或未知分项。monthly_cashflow 只复用足够表达的已有汇总，不能替代依赖工资／费用／出资性质的原现金分类。
context 轻量检查仅复用它原来消费的 period／closed_periods 结论；Reports/readiness 正式金额守恒、税额、勾稽和现有后补比较数豁免不变。
单事务承诺限于绑定的公司库核算响应；跨公司目录元数据由目录读取，不能宣称与公司库属于同一 SQLite 快照。返回绑定 company_id/database_id 并处理选择失效，不建设跨库事务框架。

5、实施／验证收敛
同一 T4 按“索引与共享读取→消费者与分页”顺序推进，每阶段冻结有效代码哈希／差异及已有证据。每次只跑实际受影响边界的定向集，不每阶段跑全部 T4、不重跑 T3 整批。
迁移、回滚、重复出现、遗漏检测和同步写路径挑代表性接缝复用夹具；所有成本指标集中在一套合成样本测量，保留冷请求和无关历史／真实相关依赖两种增长证据，不逐页重建大型数据。T5 继续负责跨任务统一验证。

请审核此 v2 补充；当前仍等待明确放行，不修改代码、迁移或测试文件。
