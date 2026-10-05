<!-- @format -->

# r63 简报必要金额读取只读审查

本组先只读审查当前源码、既有阶段记录和固定 r61 诊断原件，随后按 root 授权，将两处“同快照已认证头重复传输”收敛为一组窄改，并运行新增定向文件。未运行建库、profile、规模回归、构包或浏览器计时。r62 已采用的资金 B/C 和月度核对职责调整，以各自既有回执为准。员工／资产身份读取与本组已完成集中受影响回归；后续父任务固定 r63 候选的主 12 月完整注册核验和纯浏览器刷新已通过，范围另列于文末。

root 提供的 r62 主 12 月、50 人、每月 1,000 笔、每页 30 次纯浏览器结果为简报 median 436.8ms、p95 476.7ms、max 596.2ms，1 次达到或超过 500ms；其他四页全部小于 500ms。这仍未达到全部样本小于 500ms 的要求。下述工作量来自 `.tmp/stage9-resident-r62-profile.json/.log`，其中 `scope` 明确是 **fixed_r61_resident_profile_and_work_diagnostic_not_browser_acceptance**，不能因文件名 r62 将其当作 r62 生产计时。

## 金额认证的必要边界

`Dashboard.brief → _brief_amounts → month_journal.account_amounts` 先认证整月正式金额，再读取有限活动页、经营分类、资金、待收待付及负责人待办。现有月份读取使用精确 selector、冻结引用或当前发布，不把默认 20 条当完整月份。

固定 r61 简报共有 312 次 SQL、19,723 返回行、8,008,093 返回值字节、约 1.573M VM；结果正文传输／解码 1,058／1,057。`_brief_amounts` 的 profile inclusive 为 256.5ms，`account_amounts` 为 252.3ms；资金为 117.7ms、待收待付为 63.8ms、活动分类为 35.0ms、待办为 27.8ms。这些时间有 profile 开销且包含嵌套，不相加，也不作为净收益预测。

诊断实际 selector 返回 **1,005 个当月凭证头**；`_lines` 对 **1,006 个唯一保存结果**各检查一次。两种计数来自不同对象，不能泛称为 1,006 个重复凭证。当前 `expected_lines` 已按 calculation ID 去重，2,010 次 `result_lines` 调用不会使严格保存行检查运行两遍。

| 读取／证明 | 实际职责 | 审查结论 |
| --- | --- | --- |
| 整月 selector 与实际完整 `voucher_line` | 精确采用版本、原凭证／冲正、行次序、完整账户／借贷／cashflow tuple、金额整数类型、凭证总额 | 必要。不能只读利润账户、首屏凭证、余额投影或摘要。 |
| `verify_selected_content` | 保存结果严格 JSON、摘要、来源身份、计算和事实双封签、实际事实正文和物理期间、子行顺序 | 必要。不能以修改后的 body 与 digest 自洽代替独立采用／输入证明。 |
| 当前发布与无影响复核 | 原发布和 current reviewed head 都须存在，主体／期间／voucher identity 相符，current 无 successor | 必要；重复的头传输可以窄化，但这些关系不能略去。 |
| 冻结采用、冲正的独立原月 | 当前月和原月采用结果、凭证头与原始完整行分别比较 | 必要。不能将 correction 的新 head 当作原凭证保存 owner 或原月 adopted head。 |
| `_brief_amounts` 与 `monthly_account` 比较 | 将实际认证金额与可重建投影完整比较，损坏直接报错 | 必要；同请求内该 21 行投影重复传输不是第二次业务证明，见下表的小项。 |
| 活动付款来源的 `verify_published_source_bindings` | 付款明确引用的来源与各自 publication、不可变 anchor、贡献正文及 fact/result binding 独立比较 | 必要。即使结果已经解码，也不能凭 body+digest 自洽省去 anchor。 |
| `verify_saved_input_identity` | 缺少可重建贡献正文等真实 fallback 时，核保存 ID 的事实、依赖版本和读取作用域 | 与 anchor、事实内容及采用不同，不因方法调用相似认定重复。 |

## 已有证据支持的改动边界

第一、二项已窄改，第三项及投影小项明确保留；本组未证明端到端净收益。

| 优先级／位置 | 已确认的重复及原始工作量 | 可执行边界 |
| --- | --- | --- |
| 1：`Journal.account_amounts:746 → verify_current_voucher_publications:88` | Journal 已取得实际整月 selector 头，随后该函数按 ID 再 JOIN 同一 voucher version、calculation、voucher current，返回 1,005 行／320,548B／34,200VM。后者的 `id/voucher_id/calculation_id/reverses_id/period/subject_id` 与前者已有物理头重合；其新增值为 current 命中和 `max(period_close)`。 | 保留独立 ID 入口原查询；为 Journal 的实际 selector 结果窄拆私有行输入路径，绑定同连接、owned active 只读事务及真实选择器。仅复用实际头字段；保留独立 `closed_through`、current/head、publication successor、record digest、来源／事实／冻结检查。不得提供公开“已核验”参数，不新物化全月头，不复用上一请求。 |
| 2：`_Snapshot.activity_classification:1180` 的 `missing_kinds` | 成功整月 `verified_rows` 已有精确 `basis_calculation_id/basis_kind`，但分类只查 `_metadata`，遗漏这份既有头，再返回 478 行／35,094B／3,800VM 的 `SELECT c.id,c.kind`。 | 先从同一次成功整月头按精确 basis calculation ID 取得 kind，再查真正缺失 ID；来源 anchor 和 narrow missing-source SQL 保留。不要把 voucher owner ID、subject ID 或 current head 猜成 basis；冲正、冻结无影响复核各保留原实际 ID。只增加一个实际被消费的小映射，无需新证明缓存。 |
| 3：`FundsRead.account_summary → balance_totals / balance_movements → period_balances._totals` | 相同 period/category/keys 的两调用都发现最近 close 和精确 open tail；tail 三表 UNION 实际运行两次，共 2 行／16B／53,200VM，约 2.42ms SQL 诊断时间。第二次金额 seal 检查已由 `verify_balance_periods` 精确缓存命中；不是再次完整认证。 | 如前两项后仍需缩小工作，可在同 owned 快照复用**精确 tail 发现结果**，键包含 cutoff、真实 close 身份与适用读取范围。不缓存完整余额、不合并 closing 与 movement 语义、不删 publication／balance／seal 任一候选来源；非托管或固定 v1 继续原路。该项量级小于整月头传输。 |
| 小项：`_Snapshot.__init__:286` 与 `_brief_amounts:2499` | `SELECT account,debit,credit FROM monthly_account WHERE period=?` 实际两次，共 42 行／872B／300VM，约 0.43ms SQL 诊断时间；前者立即折成净额，后者需要 debit/credit 完整比较。 | 如顺带收敛，同快照保留原始 21 行及其 debit/credit，让两消费者复用；不能用净额相等替代借／贷发生额完整相等。不值得独立扩展接口或添加缓存层。 |

第一项不能简单将 `close_period is None` 视为全部 current 条件。原函数允许一个 still-current voucher 同时被更晚冻结月引用，并只核 `period > max(period_close)` 的开放当前凭证；selector 对 `close_period is not None` 的分支不强制 current。实现须准确区分开放候选与冻结候选，仍核 actual current 命中，或仅在已证明的 `no_close_references` 精确月快路径复用，并让其他范围沿原 ID 入口。这比直接接受任意字典作为证明更窄，也便于验证。

第二项只有成功 `verified_rows` 才具备完整依据。无成功证明、子范围、不同月、固定 v1 及分类本身独立读取继续 fallback。同一个 basis ID 多次出现时应得到同一 actual kind；不要为了凑命中，将未认证 header、修订事实或当前人员／资产类型填进去。

## 同类排查与不采用项

| 范围 | 实际代码／诊断证据 | 决定 |
| --- | --- | --- |
| `_lines` 里的 Python 物化 | 1,006 次必要严格检查；四键生成器在 profile 显示 11,110 次 resume、73.0ms own，整段 `_lines` inclusive 101.2ms。每条实际行另建 tuple 用于严格保存／实际比较，这是完整证明。 | 不删 `_lines`、不缩 tuple、不取消 bool/float、范围、形状、balance 和 cashflow 检查。可将固定四字段生成器赋值改为直接四次索引以减少临时生成器，但 profile 对大量短调用的开销很重，不能据该 73ms 承诺 native 收益；该共享函数也进入历史核验，不能替换其版本化 `checked/sum_fen`。 |
| scalar fact physical period + hash | `_verify_selected_fact_bodies` 先读取 typed 表 ID/period，再 `_scalar_fact_hashes` 返回同 ID 的原始 canonical JSON。expense 的 physical 读取 404 行／16,160B，payment 495 行／19,800B；正常 hash 路径随后再次命中同 physical rows。 | 有同一行二次 seek，但职责不同。若未来合并，scalar 专用 helper 必须同时返回**实际物理 period**并与 fact header 比较，保留缺失集合、child order、seal 和 canonical fallback；raw-cache 命中和不支持事实仍需物理检查。共享 `_scalar_fact_json_sql` 也用于正式 typed fact loader，不能直接改变其两列输出。这组范围更大、收益未测，暂不优先。 |
| 来源 header 的再次读取 | `_verify_anchored_source_bytes` 即使已有 decoded contents，仍读取结果／事实身份与摘要，比较独立 anchor 的精确四元绑定。r61 实际 495 行／121,835B，但没有再次返回全部 outcome。 | 不是可删除的重复正文；不要将其与 `verify_selected_content` 的 body 摘要证明混为一项。 |
| 付款来源贡献正文 | `verify_published_source_bindings` 495 行／1,557,339B，是 activity 来源的独立 anchor/checksum/绑定，后续只消费部分字段。 | 未证明可以在不改变不可变锚核验的情况下少传这些正文。不能仅因未显示全文改用 mutable JSON 字段或只比较 digest。 |
| `voucher_lines`／result caches | `QueryReads.voucher_lines` 用 ID 集合与 dict membership 找缺项；result_lines 用 dict 按 ID 查找；成功 source contents 按 ID 命中。 | 在审查路径未发现按全缓存线性查找。不能新加第二份缓存。 |
| balance scope cache | `verify_balance_scope` 有 `any` 历史 scope 搜索，但该 profile 的关闭期资金使用 `verify_balance_periods` 精确 key；后者两次调用仅一次真实 seal 核验。 | 没有当前瓶颈证据，保留。 |
| ledger endings／历史余额 | `snap.accounts` 只读最近冻结 trial balance 与 open increments；`balance_totals` 只读最近冻结余额根、相关 buckets 和 open tail；`balance_movements` 在闭月消费冻结 activity，在开月消费当月 activity。 | 未发现为了简报结束余额遍历全部未用旧 result。不能删闭月 baseline、独立期初或 opening 未建立的传播。 |
| 清偿 historical/current | `frozen_dashboard_open/_scope` 调用两次，`_tail_rows` 实际开放期 1,246 行只返回一次；另一调用检查当前 cutoff 之后的真实期间。 | 已有 r62 证据表明范围不同。仍保留历史／当前差异、清偿 seal、未处理资料和未知传播，不按函数次数删第二次。 |

已阅读 [r62 资金必要读取](funds-summary-required-work-r62.md)、[r62 请求／响应职责审查](owner-r62-refresh-path-audit.md)、[r31 凭证汇总分页负实验](journal-summary-page-negative-r31.md)、[JSON 选择守卫](grouped-stored-json-selection-r31.md)、[protected callers](stored-json-protected-callers-r31.md)、[r32 accounting 批读](accounting-authority-batch-r32.md)、[r34 证明边界](r34-read-proof-review.md)，并核对阶段主文档的“未采用”记录。r12 money window、全月 header 从约 47KB 到 644KB、report flow 第二缓存及 r31 MATERIALIZED 汇总／页末页 VM 增加约 11.6%–11.7%，均保留各自当时路径的负面结果；当前路径收益未验证，本轮不重试。不能把少一次 SQL 当收益，也不能把旧性能结果外推成永久排除。GC／schema skip、跨请求缓存和审查第 24 项仍按明确边界不采用。

第一、二项利用必要金额读取已经付出的实际头，不重新执行全量 selector 然后建立共享目录，也不增加第二份存储数据。它们与上述 644KB 负实验的输入／传输边界不同，但仍须经过完整响应和真实工作量验证后才可认定修复有效。

## 五页与其他入口的共同边界

| 入口 | 本次建议如何适用 | 必须保留的职责 |
| --- | --- | --- |
| 简报及对应 CLI/MCP dashboard 映射 | 第一、二项直接作用于已确定的默认完整金额链；第三项作用于资金摘要共同 reader | 默认 20 条、整月金额／分类、funds/open items/owner tasks 及成功响应完成条件全部不变。 |
| 资金页 | 默认没有先执行 Journal 完整金额证明，不能凭简报的另一次请求复用头；period discovery 候选是资金共同读取的同事务小项 | 原 events 来源、effects、银行历史身份、对账、内部互转及摘要／页范围。r62 bank windows 与成功整月头复用以原回执为准。 |
| 员工／资产 | 不能凭代码类似要求引入第一项；它们按自己类型／主体选择，且没有默认完整整月 Journal proof | 权威类型、cohort、批次 owner/member、完整清偿、原月采用及当前身份纠错。身份 agent 的变更单独验证。 |
| 财务报表／`report_projection._selected_rows` | 此函数也是 `verify_current_voucher_publications` 的真实 caller，但只得到逐行报告 tuple，缺少完整 actual voucher owner 字段，不能复用 Journal 的新私有输入承诺 | ID 入口原查询；独立报表行、分类、金额和来源 proof。完整季度没有本项全月头可用。 |
| 公共全局清偿、业务详情及 CLI/MCP business queries | 未确认有 Journal 同样的实际完整头已加载；不扩大复用 | JSON1 负向选择守卫、完整采用和来源核验；重复键不能隐藏候选。 |
| 发布、真实关账、完整核验、修复、便携备份／恢复 | 不调用老板简报证明替代其完整 source/dependency/evidence 核验；公共 ID checker 如果拆 helper，原入口行为继续完整 | 原事务原子性、封签／审计、投影与目录重建、不可变冻结和备份交付验证。 |
| 固定 v1 | 保留 V1Reads、history 内容 codec、独立 close/report/settlement/balance reader；不将 active decoder／新快路径直接引入 | 历史保存解释、源 tuple、错误和原始采用依据；共享 `_lines` 如仅改直接索引也须保持历史语义相同。 |

## 本组实现与定向结果

只改 `query_reads.py` 的当前凭证采用内部职责、`dashboard_reads.py` 的 `Journal.account_amounts` 调用，以及 `dashboard.py` 的活动分类类型查找。私有行输入只有 current QueryReads、owned active 事务、实际 SQLite Row、相同精确月、已检查 `open_voucher_period` 无冻结引用、current close／publication reader 及非固定 v1 registry 才使用。Journal 的 kind/account/subject 子范围、闭期及其他条件沿公共 ID 入口；公共入口仍执行原物理头查询和共用的全部发布关系核验。无新 proof flag、HTTP 参数或跨请求缓存，完整金额成功后才发布原整月 `verified_rows`。

历史上下文 `historical_content(1)` 即使持有 current registry 也回退；活动类型复用同样受 owned active、current close reader、非 v1 registry 约束。原来源 anchor、实际物理 fact、source identity/seals 和严格完整 tuple 规则未改；`_lines` microoptimization、余额 tail 两次发现和 42 行 monthly_account 小项本轮均未实施。

新增 `tests/kernel/test_required_money_header_reuse.py`，真实合成月直接验证必要金额和公共 ID 入口等式、开放无影响复核、冻结 fallback、闭期连续更正的独立原月、source kind／seal 损坏、坏 current 指针、原／review 发布缺失、新请求损坏及失败不发布整月证明。边界覆盖 disabled/unowned、不同月、字典行、fixed-v1 registry 和 historical v1 上下文。活动防退化改为增加 24 个前月无关对象及正式发布，检查业务分类、返回行和字节、VM、结果装载与解码均不增长，不依赖匹配 SQL 文本。独立 anchor 损坏由共同组的 `test_brief_activity_reuse` 实际负例验证。

| 记录 | 实际结果 |
| --- | --- |
| `.tmp/stage9-money-headers-r63-first.log/.xml` | 首轮 **12 passed, 1 failed in 12.97s**。坏指针夹具把两个 current subject 指向同一 UNIQUE calculation，未成功写入损坏，属于夹具错误；原件保留。 |
| `.tmp/stage9-money-headers-r63-corrected.log/.xml` | 先删除第二个 current pointer，再将第一个指向第二个真实已发布 calculation，构成可保存的坏主体指针；修正后及新增 historical context 用例 **14 passed in 13.39s**。 |
| `.tmp/stage9-money-headers-r63-final.log/.xml` | 加入 disabled/unowned、filtered 和缺失无影响 review 分支后，最终文件 **18 passed in 13.56s**。Ruff 检查三个生产文件和新增测试通过。 |
| `.tmp/stage9-money-activity-growth-r63.log/.xml` | root 将活动的 SQL 文本断言替换为实际无关历史增长检查；修订单例 **1 passed in 7.77s**。它取代原文件中的同一用例，不增加不同用例总数，该修正已纳入后续集中受影响组。 |

最终 24 笔真实合成月以独立 snapshot 对照当前路径与原公共 ID 头查询路径，金额均为 300 分费用及应付：返回行 **245→222**、返回值字节 **43,528→36,356**、VM **7,600→6,800**；保存结果传输和解码数量相同。该记录位于最终 JUnit properties，不代表 12 月主样本或端到端收益。

## 主 12 月实际常驻工作量

固定 r63 对照实际固定 r61 的[常驻读取工作量](owner-resident-work-r63.md)已完成现成原件归档；旧 helper 的 r62 文件名不表示固定 r62。简报 SQL **312 → 310**、返回行 **19,723 → 17,737**、返回值字节 **8,008,093 → 7,619,195**、VM **1,573,500 → 1,389,800**；结果正文仍 **1,058 行 / 971,720B**，结果解码仍 **1,057 次 / 971,069B**，typed fact、采用切片与原件解析计数均未变。该比较包含 r62 已实施的范围收敛，不能把全部差额归于本组两处头复用；实际 SQL 变化分别列在工作量档案。必要完整金额、来源 anchor 和实际事实认证保留；42 行 monthly_account 小项仍未实施。资金和报表主体 SQL／返回值／正文／解码不变，VM 仅分别 +100／-200 的采样粒度差异。

此为实际 ResidentReadPool 的主 12 月五页工作量，与主 48 月完整核验并行。所有 native/profile 时间仅用于定位，不作净收益、速度 A/B 或 500ms 验收。独立纯浏览器 passed 另见原始回执；不能外推 48／120 月或正式包。两版共 16 份 helper／JSON／log／prof 逐份 gzip、解压比对和 SHA256 均已核对，归档未运行新测试或测量。

## 集中受影响回归与验证边界

两组收敛后的集中受影响回归已完成：**212 passed、7 warnings**，pytest 为 **460.48 秒**，外层实际进程为 **462.0434458999953 秒**，exit_code=0。源码清单前后 SHA-256 均为 `16cfe1fd5d755ce8c1add56bc15f3578f73ea6efe9fd426b9191ec53411f25cc`，source_unchanged=true、changed_files=[]。[原始回执清单](owner-required-group-r63-manifest.json)保存 JSON、log 和 JUnit XML 的原文 gzip、原件时间、字节数及双重 SHA-256；解压后逐份与原件相同。7 条警告为 `record_property` 与 xunit2 的兼容性警告。集中组后的 3 处测试 import 排序和空行整理不改变逻辑，但不属于本组源码未变声明。

本轮实际目标包括新增必要金额文件、原 Journal source header／current adoption／publication proof／原月 scope／kind authority、activity reuse、brief amounts、summary proof、funds work 和 posted line reuse，并与员工／资产身份组及两项响应合同合并验证。实际目标逐份记录在清单，不将局部 18 项或重叠定向结果累加为新总数；首轮坏指针失败和修复历史保持原样。

这是两组集中受影响回归，不是最终全部回归。现有微型工作量不代表主样本端到端收益。后续父任务固定 r63 snapshot `3372ca217fecc7d45d4068c57d0650fb78605e5dcb5b62dd8070054dc11a827b`，主 12 月实际完整注册核验 90235.8733ms、四项 coverage verified、limitations=[]，实际开放月预览完成；纯浏览器五页各 30 次成功且全部小于 500ms，简报／资金／员工／资产／报表最大为 496.2／277.2／316.8／237.3／477.2ms。[候选与全部逐次原文](owner-candidate-pure-r63-manifest.json)独立保存，中位／p95 及冷页边界见[阶段记录](../09-performance-and-release.md)。只认定当前主 12 月候选通过；仓库仍 draft/0，主 48 月及独立 12 月新合成目标已完成完整注册核验，四项 verified、limitations=[]；业务行、rowid 与冻结原文保留，fresh-target 元数据不是迁移历史。主 48 月实际纯浏览器 150 次均成功，但 92 次达到或超过 500ms、status=over_target，性能失败；[规模与全部失败原文](owner-main48-independent12-r63-manifest.json)独立归档，精确时间及逐页统计见阶段记录。主 120 月、独立纯计时、压力、正式库和最终交付未验收。下一组 r64 仅只读排查历史增长，暂无生产修改。r62 简报 1／30 次超线和全部原始失败仍保留。
