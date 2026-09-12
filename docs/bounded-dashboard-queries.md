# 有界读取与看板合同（T4）

任务 `01a09521-a902-7ca2-9fbb-2d9ffa74f194`。实施依据为已批准的 [T4 v1＋v2](t4-approved-plan.md)，以及架构批准的期初领域采用证明补充。前置合同仍为 [核算等价](accounting-equivalence.md)、[历史内容版本](history-content-versions.md) 和 [共同业务查询](unified-business-queries.md)。

## 读取边界

`QueryReads` 在调用者的公司 SQLite 读事务内批量缓存精确事实、计算元数据、凭证行、依赖和来源。缓存不跨请求。BusinessQueries、Reports、Dashboard 和组合 Workflow 使用同一连接；HTTP 看板在同一响应内读取版本、汇总、明细和期间准备。目录库提供公司列表，独立于公司库业务快照，不宣称跨数据库原子读取。

普通历史科目汇总采用精确冻结 trial balance，并接续之后必要的 `monthly_account`、`opening_account` 增量。当前无期间的 `balance` 不作为历史往来依据。往来重分类继续由共同财务分类器按科目与稳定交易方处理；未知及正负来源不得因总净额为零而先行抵销。

报表按凭证入账期 `posting_period` 选择本年利润、现金流分类明细及相关历史期初／往来来源。跨年更正保留精确原计算。财务报表和导出仍使用完整结果摘要、报表事实、冻结来源清单及原现金分类规则。`context.quarters.complete` 仅复用 Reports 的建账期间与逐月闭期覆盖检查，不再为每个季度生成完整三表。

银行对账单明细从选中的规范化 entry 子表分页，不先装载完整银行流水事实；账面资金变动从精确选中计算的资金余额 effects 分页。员工与资产先计算整个领域的必要汇总、筛选稳定实体键，再展开本页的来源。月度指标只提取需要的标量事实字段和冻结结果值。档案、收款人、管理说明和税务身份按明确实体读取，历史补充仍保留 T2 的逐字段来源与冲突。

## 公司 v10 的引用目录

目录库保持 v3；公司库通过前向事务由 v9 升级到 v10。v1—v9 合同不回写。新增三个可重建的原始引用目录：

- `close_reference`：完整 calculation 成员、精确 voucher 及其原 calculation 声明，以及现有消费者使用的固定事实／管理来源路径。
- `job_reference`：T3 支持的有限任务声明中的业务、事实、计算、义务与期间候选。
- `audit_reference`：受支持审计动作的精确来源出现记录，保留重复出现位置。

它们只定位候选，不保存 adopted、root、settled 或 completed。目录与源记录在原写事务内同步，包含普通／批量关账及相关文件任务。普通读取核验命中的精确叶与来源标记，不在打开连接时全库回填或扫描。迁移和显式完整校验比较原始提取结果的完整多重集合；便携包验证包含该检查。

凭证选取从所选主体／业务类型的计算及精确凭证身份驱动，再应用原冻结／当前选择规则；原凭证到冲正凭证的反查使用 v10 普通索引。它保留相关冲正，不为验证主体条件先扫描全部历史凭证。来源历史页的发布追溯和封存反查同样从页内主体、精确引用与期间驱动既有索引。

命中核验不能发现一个被任意删除、因而完全不再被反向查询命中的来源；此类完整性问题由显式全量验证发现。已处理标记不构成任意遗漏的证明。旧来源的局部非法字段沿既有合同保留问题或跳过不支持的声明，不扩展为猜测性 JSON 引用。

## 冻结采用与当前后续事项

通用无分录采用证明仍使用完整相关 manifest 图，保留 `manifest_lineage_root` 和 `manifest_voucher_root`。筛选和分页只缩小候选范围，不截断参与证明的成员或依赖。无法证明的候选继续是 `unestablished`，保留全部精确候选与追溯，不作为已采用事件累计。

期初包的有限补充 `manifest_opening_member_adoption` 详见共同业务查询合同：独立已采用的白名单明细、精确包／成员事实与固定结果形状、实际分类计数、完整 opening lines 和首封存逐科目的借贷发生额（分别核对 gross debit／credit）必须全部一致。它不推广到任意依赖，不用当前头、唯一候选或相同净额证明采用，不豁免正式 readiness 检查。

历史清偿截至所选月末。当前跟进从该历史范围的业务身份出发，纳入精确相关的后来付款和当前正式发布，排除无关后来业务。两者在页面分别标明 cutoff。义务金额、部分未知、未建立采用及文件／外部办理问题不转成零或完成。

未知员工／资产实体行只说明精确候选中出现的身份，金额为 `null`，保留 `candidate_selections` 和逐候选追溯。这些占位不创建事实、不证明采用。实体属性不能确立时继续为空；有已确立卡片时保留其独立依据，不用它覆盖未知候选。

## 页面版本与集合

`brief`、`funds`、`employees`、`assets` 使用 `schema_version: 2`；`context` 保持 2，`quarterly-report` 保持 1。新增只读 `business-status` 使用 1。公开 CLI/MCP `business_status`、`period_readiness` 的完整查询继续存在；页面 summary 明确标识为投影。

每页保留原汇总字段。增长集合位于 `data.collections[section]`：

| 入口 | section |
| --- | --- |
| brief | vouchers、businesses、open_items、settlement_events、external_followups、file_jobs |
| funds | accounts、movements、statements、investment_products、investment_events |
| employees | employees、payroll_sources、labor_sources、settlement_events |
| assets | assets、projects、source_history、settlement_events |
| business-status | events、settlement_events、source_history、file_jobs |

集合包含 `items` 和 `page`。`page.total_count` 是全范围数量，`filtered_count` 是筛选后数量，`returned_count` 是本响应数量；另含 `has_more`、`next_cursor`。默认 100、最多 500。未请求的明细集合可不展开，页面展开时独立请求。原资金三个游标及简报 `after_number` 保留衔接；旧资金 page 的 total_count 保持筛选后含义，新 collections 明确两种总数。

游标绑定公司、数据库、期间、as_of、入口、集合、实体、全部筛选和 snapshot_version。文件任务集合另绑定相关任务状态／结果的 collection_version；工作器更新没有推进业务 epochs 时也不能拼页。版本或范围不匹配使用 `dashboard_snapshot_changed`。

员工实体使用 `employee_id`，资产和项目使用 `asset_id`／`project_id`，业务详情另用 `subject_id`。员工筛选支持 all、in_period、payroll、no_payroll、unknown、ended；资产支持 all、active、fixed、intangible、pending、exited。员工卡片的 payroll_source_page 独立绑定该 employee_id。

员工、劳务、资产和项目来源中的历史 `movements` 同样在展开前分页，附 `movements_page` 与精确 `subject_id`，沿用父页 limit。完整义务金额与计数来自共享 summary；已返回的明细不是完整历史。其游标可通过 `business-status` 的 `settlement_events` 继续读取。该入口的 `settlement_view` 仅允许 historical／current，默认 current，游标绑定该选择；历史详情与当前跟进分别标明范围。

上述来源卡片的历史明细沿用共同查询的“该业务相关清偿”范围，包括该业务承接的其他来源，保留每条 `source_business`；`movements_scope` 明示该范围。义务汇总仍按本来源义务展示，不能把相关明细自行加总为本来源付款。适配器不在共享分页之后另行删行，否则会令数量与游标失真。

简报深链使用 `voucher_version_id` 或旧链接的 `voucher_number` 精确定位，二者互斥，结果放在 `data.focused_voucher`。不会循环读取此前各页，也不把焦点凭证追加进当前分页集合。

`period_preparation` 是带标签的期间准备投影，保留 closure、冻结 recorded/not_recorded、当前业务条件、当前跟进计数／金额及问题。季度页面在同一事务内给出三个相关月份。完整单业务的固定状态、义务和采用证明保留在 business-status；来源历史、事件和文件任务另行分页。

## 客户端与成本说明

五页及共享 context 使用单调请求世代。切公司、期间、筛选、刷新和卸载都使旧请求失效；成功、失败、finally、路由回填和下一页合并均检查原上下文。员工 A→B→A 不会因缓存提前返回而留下 B 请求。API 客户端实际检查 schema 版本和分页数量，而非只声明 TypeScript 类型。金额字符串使用 BigInt；null 继续传播。

有界不表示所有业务都是常数成本：资金账户期初及投资成本仍在 SQL 中聚合相关历史资金／1101 凭证 effects，其成本随相关历史资金业务增长；员工全域统计仍需相应员工管理元数据。整个领域的准确汇总需要相应标量输入；共同清偿器必须读取相关源业务与精确关系；单个大批次的不可变事实／结果及完整采用证明不能截断。分页限制返回与展开的实体／来源键，完整公开报表和显式完整性检查仍按其真实输出范围计成本。验证按代码状态记录，不把不同批次的通过数量相加；最终证据见 [T4 实施验证记录](t4-implementation-verification.json)。

## v11 前向兼容

公司库当前版本追加为 v11，目录库仍为 v3；以上 T4/T5 历史口径与验收记录不回写。标准 v10 保留原结构并追加版本历史。另以独立的 `recorded_business_v10_pre_account_indexes.json` 精确识别已记录历史 v10 形态，在前向事务中只补齐 `calculation_obligations`、`dependency_scope_any_kind`、`voucher_line_account`、`voucher_version_reverses` 四个读取索引；保留原 `schema_history` 第 10 行并追加第 11 行，不重建 close/job/audit 引用目录，不改变业务表、凭证或来源内容。缺失触发器、额外缺项、局部修补及其他相似形态继续按结构不匹配拒绝，不能用忽略差异或回写冻结 v10 合同来兼容。

备份验证沿用已知旧版本识别及 v10 以上完整引用核验，目录绑定和恢复沿用验证后前向升级；历史包本身不因读取或恢复而改写。迁移资源继续由现有 `*.json` 打包规则纳入，历史形态文件不参与 `v*_business.json` 版本合同枚举。
