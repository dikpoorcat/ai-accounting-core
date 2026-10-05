<!-- @format -->

# r64 默认读取随历史增长审计与每月主体范围收敛

本组先只读固定 r63 源码及既有诊断原件，形成下列审计；随后按父任务授权，在工作区实现完整 accounting 的每月主体范围收敛并运行定向测试。固定源码 `.tmp/stage9-build-source-owner-r63-release` 未修改；审计章节的源码位置均相对该目录的 `src/ai_accounting/kernel`。实现范围、验证与未解部分单列在文末。没有运行大库 profile、浏览器、纯计时或完整回归。

父任务提供的主 12 月五页各 30 次纯浏览器样本均小于 500ms；主 48 月 `.tmp/stage9-owner-main48-browser-r63a.json` 则为简报 median 735.8 / max 777.0ms、30 次超线，员工 1037.5 / 1096.9ms、30 次超线，报表 676.8 / 717.2ms、30 次超线，资产 436.8 / 576.6ms、2 次超线，资金 336.5 / 449.6ms、0 次超线。两根均完整注册核验通过且有实际有效预览；这些是合成公司，不是当前真实库迁移。

## 证据及可比较范围

读取原件为 `.tmp/stage9-resident-r63-profile.py/.json/.log` 与 `.tmp/stage9-resident-r63-main48-profile.py/.json/.log`；profile 明细取 JSON 已导出的函数计数，原始五份 `.prof` 各自保留，本文未重新运行或处理 profiler。两 helper 明确加载相同固定 r63 源、使用实际 `ResidentReadPool`、核对注册 verified 回执，主 12 根为 `.tmp/stage9-owner-main12-r43`，主 48 根为 `.tmp/stage9-owner-main48-r63`。分别读取 2016-12 / 2019-12 的默认 20 条，以及相应第四季度 deferred 报表；每月 50 人、1,000 笔业务。

JSON 内 `scope` 分别为 `fixed_r63_resident_profile_and_work_diagnostic_not_browser_acceptance` 和 `fixed_r63_main48_resident_profile_and_work_diagnostic_not_browser_acceptance`。这些计数是实际工作量；profile 时间只定位嵌套职责，不相加、不作净收益预测或纯浏览器验收。不同年份的数据会增加实际对象、历史业务和正文大小，不能称为除了关闭月数之外每条业务完全相同的 A/B。没有 EXPLAIN 新测量，不能由 SQL 文本猜实际执行计划。

| 页面 | SQL 次数 12→48 | VM 12→48 | 返回行 12→48 | 返回字节 12→48 | 保存结果传输行 / 解码次 12→48 |
| --- | --- | --- | --- | --- | --- |
| 简报 | 310→594 | 1,389,800→1,443,800 | 17,737→19,994 | 7,619,195→10,397,391 | 1,058 / 1,057→1,202 / 1,201 |
| 资金 | 227→515 | 1,027,700→1,062,800 | 7,194→8,268 | 2,675,559→4,761,613 | 584 / 584→728 / 728 |
| 员工 | 149→366 | 604,700→1,568,000 | 11,299→35,750 | 5,055,183→13,844,770 | 100 / 100→100 / 100 |
| 资产 | 401→1,164 | 325,500→599,300 | 4,585→10,094 | 2,886,105→9,274,520 | 109 / 109→433 / 433 |
| 报表 | 367→634 | 924,900→956,100 | 24,424→25,254 | 9,618,791→10,842,778 | 1,006 / 2→1,006 / 2 |

所有页面 `full_close_manifest_decodes`、`adoption_section_reads`、`material_original_parses` 均为 0。这只排除对应完整 manifest／原件解析，不排除每个旧月的 header、subroot、采用 bucket 或 readiness block 展开。简报/资金的 VM 仅增 54,000 / 35,100，而传输分别增约 2.78 / 2.09MB；不能把 SQL 变多直接解释为全表扫描。员工正文仍 553,340B、typed fact 解码仍 0；历史成本已经转移到头、事实角色和采用叶。报表 contribution 解码仍 1,004、保存结果解码仍 2，stdlib JSON loads 却为 685→4,441；二者不是相同计数口径。

## 共同冻结采用路径：全历史主体与逐月 scope

`dashboard_funds.py:81 FundsRead.__init__ → BusinessQueries._selected_accounting:406 → QueryReads.close_accounting_many:1570 / _close_accounting_many:1688 → close_storage._read_accounting_many:960 / _read_accounting:743` 是简报和资金的主要共同历史路径。资产批次 owner 也进入这一链。`_selected_accounting` 先按 kind 取得该类**全部主体**，通过正式发布发现请求 cutoff 内的真正 posting periods，然后把同一主体全集交给每个关闭月。其范围精确到对象类型和 cutoff，但主体与月份的对应关系尚未缩到每月实际发布主体。

| 实际证据 | 必要工作与处理决定 |
| --- | --- |
| 简报采用切片 24→96、采用行 47→191；资金 23→95、46→190。`_may_contain_with_positions` 简报 299→4,655，资金 298→4,654；比关闭月数的约四倍增加更快。 | 源码的全主体 × 多关闭月明确解释了 membership probes 的放大。优先核查**精确每月主体 scope**，不要先删掉历史采用证明。现有 `subjects_by_period` 仅在 adopted-only 分支允许；不能向 include-vouchers 分支直接塞参数后认为等价。保留独立正式 publication authority、冻结 highwater、current、原/冲正凭证及目录损坏发现。 |
| 简报、资金、员工、资产的 accounting family subroot SQL 均 11→47 行、436,593→1,868,298B；该 SELECT 的 VM 仅约 200→800。 | 已确认逐月 family 正文传输/解码增长。family 已按 `(period, storage_digest, family)` 缓存，四页各请求内均只读一次每月 family；不是每个切片重传整个 subroot。压缩 SQL 次数不能消除这份必要结构。更窄 scope 能否避免某些月份/家族，须先证明那些月份不影响采用、期初和零金额身份。 |
| `FundsRead.account_summary:402` 对历史 statement/reconciliation 再调用 `frozen_bank_account_identities`，沿 adopted-only 切片认证精确 bank identity。完整 accounting slice 与 adopted-only slice分别缓存。 | 两路径实际重叠历史叶，但承担完整 state 枚举和后续银行事实身份职责，不能凭切片数认定所有第二次证明重复。可审查同 owned 快照内，从成功完整 slice 取得精确子集，省重复叶物化；仍须实际 scope、独立银行事实/account ID、record digest/seals 一致，不能以“已经验过”布尔跳过银行核验。此方案未实现、未验证收益。 |
| 金额类 `_selected_accounting` 默认 `include_vouchers=False` 仍读取 accounting slices 的 vouchers，并执行 actual voucher selector用于 represented/no-line state 区分。 | 此身份区分有明确职责；不能仅因调用参数 false 换成 adopted-only并删 vouchers。需要逐类证明无凭证 state 与 journal_basis的完整关系，通用公共 reader不默认放松。 |

这组比上轮 42 行 `monthly_account` 重读或 open-tail 发现的两个单行结果更能解释历史增长。r63 的 money 私有头复用仍仅开放精确月、实际 SQLite 头、owned active transaction、current reader及非 fixed v1；这次不能扩大为全部历史头复用。默认 Journal 的 1,005 个当月头、完整凭证行、事实和来源 anchor 仍是必要金额认证。

## 员工：不消费旧正文，但消费全部工资身份

`dashboard.py:3447 _employees → payroll_head_metadata / adopted_head_metadata:121 → current_role_matches → verified_payroll_heads:373 / verified_adopted_head_identities:235` 不传 posting_period，故枚举所有旧工资主体。每月工资有独立 subject，`row_number partition by subject_id` 不会把同一人的 48 份工资压成一份。工资头 SELECT 实际 600→2,400 行、81,480→325,920B，VM 83,300→335,900；后续身份/封签 SQL同为600→2,400行，VM27,600→110,400。冻结采用叶550→2,350，切片11→47；采用 bucket bytes为4,429,791B（48），并非旧工资 outcome。

默认人员范围、当前身份纠错、工资无行结果、同月金额、负责人确认、已离职状态及历史清偿均有既定业务含义。当前 `representative_heads` 已选每个人的最新代表，但**在此前**已验证所有历史 head/role，并在后面再次用所有head构成人员范围。只留下 representative 会丢掉历史引用被身份纠错合并/拆分、无行工资、未知状态及当前角色冲突；不判定全部旧头是重复。

`current_role_matches → verify_hits/_expected_rows` 证明 fact 的 recorded/current角色，`verified_adopted_head_identities` 证明发布/冻结采用、主体/fact身份及seal；职责不同，不按函数看起来相似删除其中一个。可继续审查一个已认证的员工 membership/代表目录能否覆盖同等完整范围，或同一次枚举的既有精确角色结果能否在后续真实消费中复用，但这是新的证明边界，尚未批准或实现。本轮不重试审查第24项、r12 MoneyWindowA或644KB全月头方案。

## 资产：金额快路仍先展开所有批次 owner 的完整成员

`dashboard.py:4222–4330` 已对未处置且 cost/非charge/冻结余额一致的卡片使用 carrying 快路；fallback资产保留完整历史。快路卡片仍调用 `BusinessQueries._selected_asset_member_heads:827`，该函数用 `complete_owners=True` 选择全部 activation/consumption owners，读取所有 owner 正文及全部成员，逐 member 验身份、dependency、foreign owner、次序、line_start/count、summary，并构建 directory；**完整验证之后**才按 `asset_ids` 留下实际卡片事件。因此SQL返回对象范围比调用所需快路卡片广，但完整 directory 与 owner membership digest 比较正是当前来源证明，不能只加 `m.asset_id IN (...)`隐藏非命中成员。

实际成员 SELECT `m.*,c.kind,...` 为78→1,176行、65,904→1,015,512B、VM5,900→86,600。owner专用读取 `SELECT id,kind,period,outcome,digest` 为96行 /1,111,505B（48）；`verify_sql_outcomes` 的另一个 owner/body查询总145行/1,132,004B。后者是裸JSON/摘要选择守卫，前者随后解析相同owner并做完整成员目录证明；两职责不能取消，但相同owner正文在同快照再次传输和解码是值得审查的实际重叠。当前缓存只保留成功语法/摘要ID，没保留该次解析结果；不得为消除重读另加第二份全历史正文缓存。

资产正文总109→433行、197,173→2,382,031B、采用切片33→141。不能把全部新增正文归因于默认卡片身份；它还含快路owner严格membership和真实fallback历史。`_asset_card_sources:4051` 的普通 acquisition/lifecycle/project头仍走r63身份路径，金额、成员和实际处置正文仍必须严格读取。优先决定是否可利用同调用已有完整owner内容，及是否存在有独立锚的精确member proof；未证明前保留整批次目录认证。不得删除activation反向撤回、再采用和closed correction的独立原月来源。

## 清偿与资金余额：最新根也有历史目录正文

`settlement_freeze._scope:938` 用最近close为baseline，再核开放tail，不是逐月重放所有已闭清偿。`_read_root:311` 每次请求对最新root读取一次，严格比较close-derived root、正文digest、all/open目录的物理完整行，再展开groups/period_amounts/change counts。该root本身含累计历史目录，177,786→679,077B；目录ref SQL两次总344→1,458行、51,559→219,006B。简报、员工、资产都消费这条共同链；资金页不调用此清偿root。

开放tail SQL仍1,246行 /521,477B，`_add_group`仍753次。简报两次 `_scope` 对historical/currentcutoff，有精确 `(period,current)`cache和counterpart复用；root只读一次、tail主体只传一次。员工 `_tail_rows` 的profile own7.66→96.95ms，简报/资产对应own基本不增，`_add_group`简报反而80.45→4.82ms，这证明不能把单个profile时间归为增长定律。当前tail工作量稳定；已确认增长为baseline正文和目录，不是重复tail或全部旧state展开。

`period_balances._totals:354` 按最近冻结balance root + precise open periods处理ending/movement。`verify_balance_periods`使用精确key；余额total/movement各自发现同tail的两次UNION仍合计2行/16B/约53,200VM，没有随历史显著增长。`verify_balance_scope` 的 `any` 遍历只在无close的fallback，当前有close路径不靠它。保留三方候选（publication、balance、seal），不能只信一张可重建表。累计balance根按业务key增长属于必要金额范围，不判定所有历史余额读取都应删去。

## 报表：全历史readiness来源与有界余额职责不同

`Dashboard.quarterly_report:2244 → Reports._report:1045(_issues_only=True) → _closed_report_fact_sources:352` 仍先取 `period_close <= quarter_end` 的全部旧月，逐月读取financial_reports readiness，把完整fact ID列表展开，再在Python按本次候选 `retain_ids`过滤。实际readiness block SQL11→47次、159,497→681,449B，`close_readiness_check`为47次；full manifest计数为0。需要旧报表profile及建账接续依据，并须防止可维修close_reference遗漏隐藏来源；不能只缩成当前季度或用mutable目录替代来源。

`_issues_only` 已按候选kind/年月取得需用ID，但旧月readiness全文仍先解码。这是明确“少量元数据消费却展开历史来源列表”候选，可考虑以现有冻结来源结构内的精确membership/absence证明回答这些candidate，但需要证明所有applicable profile冲突、carryforward/税务依据以及缺失目录负例。未经证明不能把retain_ids视作读取授权范围的完整替代。

`party_balance_rows:786 / _party_balance_rows_uncached:813` 则从最近usable checkpoint读取截至cutoff的有界月份；12和48的`read_report_flow`调用为22→23，真实flow SQL为11→12，不是旧47个月余额全重放。两次party_balance分别用于年初/期末，cache按cutoff/source精确，stored month同请求已复用；不判同范围重复。open `_party_delta` 所需contribution解码仍1,004，fact decode417不变，独立anchor/parents/source binding校验必须保留。报表公共`verify_current_voucher_publications`仍缺Journal实际完整头，r63私有复用条件不适用。

## lookup、默认页、其他调用方及决定

| 范围 | 代码及证据 | 决定 |
| --- | --- | --- |
| 缓存lookup | QueryReads metadata/fact/voucher/lines/source以ID membership找missing；close slices以exact(period,frozenset(subjects))查找；close_storage已用ChainMap私有staging避免全cache复制，positions按(key,bit_count)字典查找。`authoritative_close_rows:1808` 两次dict union会物化当前close map，规模为已访问关闭月。 | 未发现主要缓存lookup扫全部正文集合；不能为猜测问题再加缓存。dict union是小的重复集合复制，只有47项，无当前主要瓶颈证据。scope笛卡尔探测及bucket过滤不同于cache lookup。 |
| 默认明细 | brief `month_journal.page(limit20)`、funds SQL summary/page、employees/assets page_keys保持20；全汇总另按完整范围认证。 | 不以页面limit缩金额/人员完整性，不默认取消明细；本轮profile直接调用这些默认值，增长发生在其上游范围。 |
| context | `Dashboard.context:1654 / _periods:1587` 按真实indexed periods递归枚举，构造月份/季度目录；`_snapshot`确认选中期间；`_read_context`取identity、epochs、repair revision。 | context不是金额/历史正文读取。强制刷新需要新的可选期间目录；不能删请求。此次五份main profile未包含context HTTP，不给其48新成本或并发净耗时结论。 |
| owner review | `CloseReview.read:1490` 开放期读有效活动preview、核版本，闭期读exact owner_review section；不重建close或五页。r62已去掉同源/公开响应同模型重复。 | 保留独立saved review与当前经营金额语义。此次未profile48 context/review；不称为已证实增长热点。旧r62网络证据仅支持稳定选择热刷3/2/2/2/2请求，不推广到48所有交错。 |
| 按需详情 | business_status / employee settlement_events / asset source_history及settlement_events均有精确ID、游标和版本。 | 不是默认展开全部详情；本轮未测每条按需分支。精确ID可缩目录，但命中正文仍严格验证完整采用、源、seal及独立原月。 |
| HTTP/CLI/MCP | Service动作表:759–766映射相同Dashboard读入口；validate_command/response是独立边界。普通低层reader可能非owned，不能复用另一个事务证明。 | 后续任何共用reader修订须同步保持public ID入口和非owned fallback。本文没有CLI/MCP新增验收，也不改变响应合同/完成条件。 |
| close/fullverify | `Periods`正式关账、`integrity.verify_integrity:1203`及`verify_close_integrity:1538`核完整source/dependency/evidence/frozen adoption；完整报表contribution重建包括未命中来源。 | 默认看板身份proof不等于完整content/source proof，不用于替代关账完整认证；不得按默认金额快路缩其核验范围。 |
| repair/backup | `Maintenance.repair_read_indexes:24`先核不可变来源、重建投影/目录，再完整核验并推进repair revision；`backup._verify_connection:149`使用版本bundle内容verifier，portable verify保留完整历史。 | 默认读取scope收敛不授权隐藏缺失anchor、源或冻历史；repair只能维修可重建层。备份/恢复保持完整来源核验和 retained history。 |
| fixed v1 | content_history_context路由独立close/report/settlement/balance/publication等reader；QueryReads._close_accounting_many遇非current close_storage逐月走原历史路径。 | 当前proof package与新快路不得送入v1；历史decode、错误/版本化整数与采用语义保留。共享叶/tuple修改仍需检查v1消费者。 |

处理顺序建议先评审共同的“全历史主体×每月scope”与bank重复叶范围，再评审员工全头身份、资产完整owner member walk、报表readiness候选membership，最后处理已确认但量级较小的重复集合/尾期发现。每组必须基于真实consumer保留独立authority与完整性负例，再以返回行/字节、VM、正文/采用/解码计数证明范围收敛。本文不证明这些设计已可等价实现，也不承诺净延迟收益。

审查第24项已回退，跨请求缓存不采用；这两项属于明确边界。r12 MoneyWindowA 与全月头47KB→644KB保留当时路径的负面结果，当前精简路径收益未验证，本轮不重试，不能外推成永久排除。上述为固定r63的只读审计与本轮范围，固定目录不修改。

## 本组获授权的实现决定

父任务随后授权只改工作区的 `business_queries.py`、`close_storage.py`、`query_reads.py`、本组新增测试及本文，继续使用 GPT-6.1 / high；不改其他生产文件、DDL或合同，不跑大回归、大库profile或纯计时。

选择收敛点是完整 accounting reader 的每月主体范围，不改变业务结果或默认显示。`_selected_accounting` 从实际正式 publication 的 posting_period 建保守主体集合，再以实际 current voucher 的物理 period 与 owner subject 扩展；不以可维修目录的存在与否推导采用。旧原凭证、冲正或采用头的月份即使不同，也必须由独立实际凭证集合纳入，不仅凭新月publication过滤。

`subjects_by_period` 扩展到 full accounting，完整 publication/voucher authority继续按原全subjects独立枚举，逐月保留highwater与terminal/successor条件。每月窄scope只决定读取哪些冻结subject buckets，最终actual adopted/voucher集合仍与该月全authority比较；窄scope漏主体须报损坏，不能变成静默漏结果。空scope须先证明该月authority为空。采用叶、主体/fact身份、seal、current、冻结digest、实际line内容、原/冲正独立来源及state/voucher区分保持原职责。

低层adopted-only的精确子集合同保持；当前版本非同reader和fixed-v1不使用新scope，沿原全subjects逐月proof。现有完整slice缓存的窄scope键改为exact `(period, per_period_subjects, authority_subjects)`，原未窄化调用仍用 `(period, subjects)`；完整absence证明的authority范围必须在键中，避免较小主体全集的成功结果遮住后续较大全集中应发现的发布/凭证。没有增加第二层缓存或跨请求证明，成功完整slice到adopted-only子集复用不作为本组必做项。新增测试先证明与旧全subjects路径结果相同，以及缺目录、错scope、错authority、开放替换/月份纠错/跨月更正/撤去与失败不发布缓存，再记录合成规模的真实工作量。尚未完成实现或验证时不把上述决定写成已交付行为。

父任务提供的固定r63只读计划原件 `.tmp/stage9-owner-plans-r64.json` 随后补齐。原完整authority `d54cd4…1680` 使用 `publication_subject(subject_id=?)`与独立previous-publication唯一索引；原proof-period发现 `a593b1…4749b` 使用calculation_subject与publication calculation唯一索引，并作DISTINCT临时B-tree。采用身份per-period authority `9acb5d…0186`已能seek subject_id+posting_period。它们支持“当前瓶颈包括Python保守主体范围”，不证明全库扫描。资产成员 `5282eb…c22e`按owner索引seek，仍因全部owner集合返回全部members；清偿ref `f89081…38a`按period/kind索引seek，root本身的累计目录大小才是增长。helper额外EXPLAIN会贡献VM，该JSON的`diagnostic_with_explain`绝不替代前文原计数、收益或计时基线。本组只读取既有计划，没有运行EXPLAIN。

## 实现、调用范围与定向验证

工作区已经完成上述范围收敛。`BusinessQueries._selected_accounting` 用一次 publication 与实际 current voucher 的 UNION 建每月主体集合；未按 highwater 提前排除 publication，因此旧发布、withdraw、后续替换均可保守命中。完整 reader 仍以全部请求 subjects 建独立 publication/voucher authority，保留原 highwater、terminal/successor 条件。`close_storage.read_accounting_many` 和 `QueryReads.close_accounting_many` 接受完整路径的 `subjects_by_period`，只缩每月 membership 与 bucket 选择。空 scope 仍核该月全 authority；任何遗漏采用或独立凭证都拒绝。整批成功后才发布 headers、parts、positions 和 slices，失败不留下已成功前缀。

完整窄 scope 的缓存键包含全 authority subjects；每月 scope 恰好等于全集时继续用原 `(period, subjects)` 键，可与原单月/完整批读取复用。adopted-only 的既有键、精确 scope 和契约不变；本组未加入完整 slice 到 adopted-only 子集的新复用。current 非 owned 连接执行同等完整证明且不发布请求缓存；非 current reader 与 fixed-v1 不接收新 authority package，继续全 subjects 逐月路径。生产修改限定于三份授权文件的上述段；`test_close_accounting_filter.py` 的 serial hook 仅增加参数接收，仍忽略窄 map、逐月读全部 subjects，以保留旧基线语义。父任务另行实施的 asset owner 局部正文携带与 metadata 解码不纳入本组结果。

| 调用方 | 本组影响及边界 |
| --- | --- |
| `FundsRead.__init__`、Reports 的 `_opening_rows`、Calculations 的 `selected`、BusinessQueries snapshot/opening selection | 经过 `_selected_accounting` 的指定 kind/subject 读取使用每月保守 scope；金额与 selected state/voucher 结果仍完整。简报、资金、报表及相应 HTTP/CLI/MCP Service 读入口沿原公共链受影响，未改响应合同。 |
| `_selected_asset_owner_events`、完整 asset members、member heads | owner 的 shared accounting selection 使用窄 scope；整批 member directory 与 dependency 完整证明继续由原 consumer 负责，未把 scope 等同精确 member proof。 |
| `_settlements`、`settlements`、`_current_settlement_selection`、`_current_settlement_followups`、`_business_status`、`business_collection` | 指定主体的共同 accounting selection 使用窄 scope；后续当前状态、原/冲正凭证、清偿冻结根及按需详情校验继续执行。 |
| Journal 的 `account_amounts` 原冲正读取、FundsRead 的 `_verify_event_sources` 等直接 full accounting 批读取 | 不传 map 的调用保持全主体每月路径；新增 API 参数没有自动缩其证明。没有将独立完整 content/source 校验替换为身份快路。 |
| 员工工资身份、银行 adopted-only 身份 | 原 per-period adopted-only 路径不变；全历史工资头身份尚未缩。完整成功 slice 子集复用仍未实现。 |
| close/fullverify、repair、backup、fixed-v1 | 未改其完整来源核验、重建或便携包合同。共享低层 full reader 只有显式 map 才窄化；fixed-v1 保留旧路径。 |

新增 `tests/kernel/test_full_accounting_period_scopes.py` 验证结果与旧全 subjects 批读取相同，覆盖三/六个月不同主体、开放期替换与来源月份迁移、无凭证 state、旧凭证与新无影响采用、跨月闭期更正、withdraw/空关闭月、physical voucher 和 publication 月份独立发现、漏主体/漏凭证、缺目录/缺 block/篡改 block/删 publication、失败无前缀缓存、扩大 authority 不误用较小 universe 成功缓存、全集 map 复用旧缓存、非 owned 与 fixed-v1 边界及错误 map。

| 定向运行原件 | 实际结果及处理 |
| --- | --- |
| `.tmp/stage9-full-accounting-scopes-r64-first.log/.xml` | 13 passed / 1 failed；新增测试错误地断言 selected 返回含 subject_id，按真实 calculation_id 结构修正。不是生产证明失败。 |
| `.tmp/stage9-full-accounting-scopes-r64-second.log/.xml` | 36 passed / 1 failed；原 serial hook 不接受新增关键字。按父任务授权修 hook，保留逐月全部主体的结果及工作量基线。 |
| `.tmp/stage9-full-accounting-scopes-r64-final.log/.xml` | 新专用文件、原 period-scopes、filter、dashboard business-queries 四文件 67 passed / 2 warnings，83.27s。 |
| `.tmp/stage9-full-accounting-scopes-r64-cache-final.log/.xml` | 缓存键保留全集复用及新增对应测试后，专用文件完整复验 17 passed / 2 warnings，35.04s。不同 suite 数量不累计；当前专用文件为 17 项。 |
| Ruff | 三生产文件、新专用测试、原 hook 测试五文件检查通过。两次 warning 均为 record_property 与 xunit2 兼容提示；XML 已保留计数属性。 |

计数测试只测 synthetic close batch 的 scope 成本，不包含 publication/voucher UNION 发现成本，也不是页面计时。每月四个不同主体的三月样本 membership probes 从 36 降到 12，六个月从 144 降到 24，验证全部主体 × 每月的 Python 探测已变为实际每月主体之和。完整采用/voucher 结果逐项相同。三月两路径同为 SQL 30、VM 1,500、返回 91 行/26,167B、stdlib JSON loads 54/input 20,796B、adoption slices 3/rows 12、body rows 0；六个月两路径同为 SQL 54、VM 3,300、181 行/52,407B、stdlib JSON loads 108/input 41,678B、adoption slices 6/rows 24、body rows 0。没有通过减少独立 authority、内容校验或正文来制造工作量下降。

本组尚未证明 12/48 月 native 工作量净收益和五页纯浏览器达标；新 UNION 的实际查询成本须由父任务后续诊断验证。即使 membership 收敛，每个命中月的 accounting family、必要 publication/voucher authority、累计清偿目录仍会增长。员工全历史身份、资产完整 member 范围、报表 readiness 全历史来源及完整 slice 的银行子集复用均未在本组解决。资产重复 owner body 已交父任务单独收敛；此处不把其收益与本组探测变化混合。统一回归、大库诊断和验收由父任务在修改组收敛后固定源码执行。
