# r25 尚未达 500 ms 的读取根因分类（只读）

依据：固定 r24 的 12/48 月浏览器 30 次原始记录、48 月六入口 SQL/VM 仪器、48 月真实 HTTP 单次与三 worker 全程 cProfile，以及当前调用链。48 月已独立完整核验；cProfile、HTTP 单次和 SQL 仪器有插桩／后台负载，只用于定位工作量，不能与纯浏览器时间相加或当作优化收益。本轮未查询数据库、运行测试或改生产文件。

## 五页与公共入口

| 入口 | r24 48 月纯浏览器 median / 超 500 | 当前主要链和根因分类 | 尚不能省掉的证明／窄测假设 |
| --- | ---: | --- | --- |
| 简报 complete | 957.8 ms / 30 次 | 父进程仍做 `_funds`、`_long_term_assets`、默认 100 凭证与本地 `period_readiness`；三 worker 中报告先 `check_report_readiness` 再 `_position`，材料和查重各独立完整范围。worker 有各自只读事务，不能跨 worker 复用来源证明。48 月 cProfile 报告 worker 759.7 ms 检查＋252.7 ms 位置，另等父 obligations 71 ms；父本地准备、账户与默认明细和 worker 并行，数字不可相加。 | 材料本月新原件与旧月变化、查重命中来源、报告账面归属均须核验；先量关键路径上实际等待、父本地准备与报告 worker 的 CPU 重叠，不能仅按单函数调用数判断可省。 |
| 资金 | 448.2 ms / 4 次 | `FundsRead` 历史银行／期初状态身份与已闭 accounting 桶，账户汇总与本页资金来源；48 月 HTTP 单次业务体约 351 ms，`close_accounting` 47 次约 103 ms（含其他层，非可加项）。旧银行资料可能决定本月零额账户和未办状态。 | 简单只取当月 statement/reconciliation 已有反例，不能重试。量同快照相同 close/subject 集的成功桶实际重复字节，再判定是否有纯重复；expected publication/voucher 两 SQL 必须保留完整主体范围。 |
| 员工 | 536.6 ms / 23 次 | `payroll_head_metadata` 枚举 2,400 个已采用工资主体，eligible SQL 约 1.127M VM；开放 `current_role_matches` 对 2,450 个事实做 typed 原文和实体引用重建，r24 诊断约 140.6 ms CPU。原先同页再 `scalar_facts` 水合 2,400 头约 31.25 ms CPU，已在当前工作树按身份／详情分层消除，尚未进入 r24 正式计时。付款及一人详情仍按实际命中。 | 不能只取本月工资头：旧工资员工仍须在名单中。closed `payroll_head_identities` 的 recorded 引用是否覆盖所有 no-impact 头的实际内容，release_review 正复现／修复，不把目录摘要互比当原文核验。进一步只能量各角色类型／闭开路每事实所需证明，不能删完整历史身份范围。 |
| 资产 | 556.2 ms / 25 次 | `asset_member_events` 约 521k VM；另两条小结果 `close_reference` 范围查询共约 1.044M VM，当前由 brief_perf 组审查。HTTP 单次六次 selected-accounting 约 345 ms、卡来源约 234 ms、141 次 close accounting 约 222 ms（嵌套包含）。卡身份、启停／处置与历史成员会增长。 | 身份与金额已分层；active 卡与 fallback 卡不能混。量每张默认卡真实采用成员、命中桶和 SQL 返回字节，才判断哪些是 N+1／大扫描；不以余额替代对象身份。 |
| 季报 | 817.1 ms / 30 次 | 完整报表需要输出所有 `report_fact_ids`，48 月一条分类集合查询确实返 15,208 ID／487 KB、约 494k VM；季度三个 `period_readiness` 仍各自检查。HTTP 单次 `party_balance_rows` 两个 cutoff 约 480 ms（inclusive），`_party_delta` 约 185 ms、两次开放贡献读约 159 ms；这些时间存在嵌套。 | 完整 ID 字段不可用 issues-only 缩掉。确认两 cutoff 的月段与成功缓存命中；分类在 party 与全文报表有不同实际 source voucher 集，只有同精确 ID 已核内容可共享，不以调用两次直接认定重复。 |

`context` 不是五个业务页之一：48 月 47 个 close 认证使 SQL 约 93k VM／523 行／0.90 MB，真实 HTTP 单次约 67 ms；当前不是主要 500 ms 问题。准备检查、close-review、按需详情经同一 `LocalService.dispatch` 内核路径供 HTTP／CLI／MCP 使用；正式 `preview_close`、`close`、完整核验、修复和备份仍从权威源全量独立核验，不使用普通页快路。固定 v1 reader/comparer 是独立规则，不能把当前页面诊断当成 v1 性能或安全证明。

## 按根因分组的已证工作与待测假设

| 根因 | 可复核证据与当前判断 | 下一次仅需测量的窄假设 |
| --- | --- | --- |
| 必要完整校验 | 报告 worker `party_balance_rows` 调用 3 次、仅 2 次 `_party_balance_rows_uncached`，分别服务期初与期末 cutoff；旧月走已认证 checkpoint／flow，开放月 `_party_delta` 实际核本月约 1,004 个正式贡献的 publication 行、anchor、来源 bytes、关系、后补分类和问题。48 月 profile 中 `_party_balance_rows_uncached` 约 581 ms、`_party_delta` 约 303 ms、`read_open_contributions` 两次总约 168 ms、其中 `_decode` 1,004 次约 37 ms；均为 inclusive，不能相加。员工开放角色 2,450 事实的 `verify_hits` 也属本页身份核验。 | 对每个 cutoff 记录 `(checkpoint,closed month,open month)`、唯一贡献／关系 source 数、实际来源字节、`_report_snapshot_cache` 命中与 fallback；若本月贡献确实全部被消费，确认校验范围而不是再用 settlement 金额推 party。员工按 kind/recorded/current 分解 `_expected_rows` 行数与解码量，验证在保留全体身份拒绝下是否有仅元数据的子路径。 |
| 同请求重复 | 报告同快照成功结果已有 `party_balance_rows`、`report_party_month`、`_party_delta` 和分类逐 ID proof；`read_open_contributions` 的两次调用不能单凭次数算两次 1,004 正文。材料 worker 的 `versions` 5 次、`current_facts` 6 次和查重 949 check records 也可能服务不同范围／独立竞品集合；其 worker 不共享 snapshot，父进程不得搬用 proof。开放员工原 2,400 `scalar_facts` 与角色真实 typed 重建才是已确证的重复水合，已窄修。 | 记录同一 `QueryReads` 下各次调用的精确输入 ID 交集、已成功 proof、真正物理返回字节与失败传播；只对完全相同内容、同事务成功证明考虑去重。不要重复此前开放 party 新行表、SQL 直接聚合、额外 worker 搬移等负面方案。 |
| 无界装载／晚筛 | 员工 600→2,400 eligible 头确用于列全体历史员工及代表头，不等于多余；旧 typed 标量水合已减，尚未减事实体验证。完整报告 15,208 分类 ID 是公开输出。简报默认 100 凭证需真实 card／来源，但 r24 还未证明祖先载入随 120 月扩大。 | 员工只比较新窄改进入固定候选后的实际默认页与单人详情返回字节；完整 report ID 保持不变。对默认 100 凭证量实际祖先计算数量与 response 消费字段，不能凭 `ancestors=True` 字样宣称过载。 |
| 精确条件但 SQL 大扫描／循环读取 | r24 六入口：员工 eligible 1.127M VM／2,400 行；简报全局缺 typed 反查 488.7k VM／0 行，属于损坏拒绝，不可缩范围；资产小 `close_reference` 查询 1.044M VM；报告完整分类查询 494k VM／15,208 行，三个准备检查含 408.5k VM 的候选查询。48 月总 VM 为 brief 5.55M、funds 4.05M、employees 4.11M、assets 4.61M、reports 6.79M；这些是不同页，不能相加到一页延迟。 | 用相同 SQL/参数的 `EXPLAIN` 与每条实际行／字节定位扫描驱动；业务候选、缺 typed 和历史引用缺行规则分别保持。当前 `_position` 本月开放分类查询 r24 约 22.5k VM／411 行，不是 48 月主要增长；大量同月无关业务另有约每 1,000 条＋9k VM 的已知风险。 |
| 缓存查找遍历 | 当前 `raw_calculations` 已改按请求 ID membership；`close_storage.read_accounting` 已用 `ChainMap` overlay，避免每次复制已验桶。`QueryReads._report_snapshot_cache` 是同快照字典键，r24 profile 未呈现新的 O(cache-size) self-time 证据。`verify_selected_content` 仍有 `identifiers - self._verified_source_contents.keys()`，但目前未见它在主要五页 profile 中占显著 self-time；不因语法单独加缓存。 | 如后续分段显示缓存查找热点，记录请求 ID 数、cache 大小、该行 CPU 与真正 SQL/核验，证明 O(cache-size) 才窄改；不能把字典命中当跨请求业务证明。 |

安全边界：普通页可复用同一只读事务内成功核验的精确来源；命中源正文、封签、分类冲突、unknown/零额及实际依赖必须继续拒绝损坏。未命中的旧源体按现有投影边界由完整核验／正式关账／备份独立发现。以上是根因分类和待测假设，并未给 r25 产出新的性能数字或声称五页已达标。
