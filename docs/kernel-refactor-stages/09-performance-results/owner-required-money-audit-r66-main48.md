<!-- @format -->

# r66 主 48 月简报必要金额职责审查

本轮先只读核对固定 r66 源码、当前源码与 `.tmp/stage9-resident-r66-main48-profile.json`，随后按父任务授权，仅收敛公共 publication 期核验这一处已确认重复。父任务提供的 native 简报为 565–696ms；诊断文件明确声明 `fixed_r66_main48_resident_profile_and_work_diagnostic_not_browser_acceptance`，不是浏览器验收。当前 r67 银行缺 publication 修复及本地 outcome 复用已经收敛；员工定位和负责人待办的事实读取由父任务分别处理，不用旧诊断推算当前净收益。未运行建库、profile、构包或规模计时；本组测试状态另列于后文。

## 实际消费者与工作量

`Dashboard.brief → _brief_amounts → month_journal.account_amounts` 必须认证当月 1,005 个凭证头及实际完整行。之后调用 `_funds(summary_only=True)`、20 条活动、待收待付及负责人待办。`_position` 没有调用，其旧正文不属于这次简报路径。

诊断共 592 次 SQL、19,859 返回行、10,317,644 返回值字节、1,443,500 VM；保存结果传输／解码为 1,202／1,201，采用 accounting slice 96 次、191 条 adopted row。profile inclusive 为 `_brief_amounts` 247.96ms、`account_amounts` 244.83ms、资金 226.72ms、`FundsRead.__init__` 101.55ms、`account_summary` 119.67ms、待收待付约 100ms、活动约 61ms、负责人待办约 47ms。inclusive 包含嵌套及 profile 开销，不能相加或当作净节省。

## 已确认的一处相同 publication 内容重复

调用顺序为：

1. `month_journal.account_amounts → _selected_current_voucher_publications → _verify_current_voucher_publication_rows` 读取实际原／current 发布，逐条核 `verify_record`，成功后写入既有 `_verified_publication_ids[id] = posting_period`。独立 current/head、subject、voucher、successor 关系仍核验。
2. `_funds → FundsRead.account_summary → balance_totals → QueryReads.verify_balance_periods → period_balances.verify_selected_balances → QueryReads.verify_publication_periods → publication.verified_period_headers` 为当期覆盖读取全部 publication 的 canonical 八字段正文，再 hash。

第一段 publication SQL 实际返回 1,005 行、204,675B、23,100VM；第二段返回 1,028 行、370,636B、28,800VM，SQL 诊断 8.676ms。代码证明第一段的 1,005 个精确 ID 已成功认证，第二段只检查整期 `_verified_publications` 缓存，未消费已有 `_verified_publication_ids`，故这些八字段 canonical 内容的生成、传输和 hash 重复。全期 id／posting_period／sequence 枚举、全部 1,028 条顺序及覆盖仍必要；另外 23 条也必须首次严格核验，不能省整个查询或把 370,636B 全算作可删。

按授权已实现：在同连接 owned active 当前 reader 快照中，让全期 header 读取消费既有精确成功 ID→period，所有行仍返回实际 id／period／sequence；只有未核 ID 返回 canonical 内容。缓存命中的实际 period 必须比较，覆盖、排序、seal／projection 与本消费者的关系核验全部保留。无已有证明、非托管、写事务、固定 v1 与 historical reader context 继续原严格路径，不引入公开成功标志、新 root、跨请求缓存或第二套正文缓存。收益未知且量级有限，不能据此承诺简报达到 500ms。

生产仅修改 `publication.py` 的私有 `_verified_period_headers` 与 `query_reads.py:verify_publication_periods`。公共 `verified_period_headers(connection, periods)` 保持原签名及完整严格内容检查，不接受“已核验”参数。QueryReads 复用条件包含 `_active_fact_reads` 的实际 owner 身份、owned snapshot、当前事务、两个 current reader 与非 v1 registry；整期和新 ID 只在全部成功后进入已有缓存，不新增缓存。先枚举实际 requested period 的完整 id／period／sequence，按这些 ID 在既有字典 O(1) 查询；仅未核实际 ID 进入第二批完整 canonical 内容核验，第二批实际元数据和 ID 集合须与首次枚举严格相等。初稿曾将整个已核 ID 集序列化传 SQL，父任务静态审查发现会随无关月份证明增长，已在运行 pytest 前改成当前有界路径。没有扫描整个成功缓存、按缓存推导期覆盖或新增证明框架。输出 header 只证明 publication 内容身份与索引枚举，不是 calculation body、采用关系或余额金额证明。

## summary_only 是否还读了被丢弃的资金明细

`dashboard_funds.funds` 将 `sections` 置空，却仍构造 `FundsRead`、运行完整 `account_summary` 与 `bank_summary`。简报实际消费八项资金金额，以及 bank 的 `unmatched_count`、`needs_review_count`、`missing_account_count`、`coverage_state` 来生成业务风险。它没有消费账户数量、账户明细和投资产品汇总；投资汇总已经跳过，账户显示名称的 `account_item/profile` 也只在明细集合中运行。

`FundsRead.__init__` 的全历史 state selector 包含 opening package、bank opening、opening bank／cash、statement、reconciliation。`account_summary` 的历史银行身份专用读取把 94 个旧 statement／reconciliation 的实际 bank ID 加入账户集合。此集合在 `bank_summary` 里作为 `expected = 已认证 bank IDs | provided`，决定 missing／partial 风险。已开立、余额为零且本月无流水的账户没有 period-balance 金额行，也没有本月 statement；删除历史身份会将缺资料错误改成 complete 或 not_applicable。因而“只为账户名单读取”不等于“简报未消费”，不能从科目或投影金额猜银行账户，不能仅凭已有 period balance 认证跳过这些 no-voucher 身份。

| 实际读取／构造 | 简报的消费职责 | 决定 |
| --- | --- | --- |
| `balance_totals` 的冻结 baseline 与开放 tail、`balance_movements` 的当期 activity | 总额、分类余额、净变化与金额来源／覆盖 | 保留，两者语义不同。 |
| 当前 `movements → events → _verify_event_sources` | 外部流入／流出、内部转账以及实际保存 effects；来源独立 frozen adoption、source content 仍必要 | 保留；period balance 不包含外部／内部划转分类。 |
| 不确定 opening package 的 issues、候选余额身份与 saved input／outcome | 让未确认 opening 传播为未知金额，不能默认零 | 保留，即使没有当月金额行。 |
| 历史 bank state、frozen scalar identity 与实际采用 | 本月银行覆盖应有账户集合，包括零金额账户 | 保留；这是业务风险的必要来源，不是纯技术返回字段。 |
| 当前 statement／reconciliation adoption、fact、pending、parent 来源及 entries／matches | 本月收支用途核对计数与银行覆盖 | 保留，不能用旧完成标记替代。 |
| `fallback_code`、账户 `last_activity_date/movement_count`、差额／adjustment／negative flag 的展示构造、账户数量 | 没有作为简报字段消费；其中部分也供资金页或退役账户判断 | 可讨论职责拆分，但需保留退役账户对 expected 的影响。旧 profile 的 `account_summary` own 仅 0.492ms、init own 0.048ms，无证据这是 102ms 的主要来源，不开孤立优化。 |
| 仅 cash 零余额身份的名单展示 | 不参与银行 expected；简报不显示数量或现金账户明细 | 可能可窄化，但旧 profile 未证明存在大量此类读取或具体收益；标为未验证，不实施。 |

94 条历史 bank 的旧 SQL 含 publication、calculation、fact scalar、entity、两种 seal 及 outcome，共 66,803B、9,800VM、9.939ms；专用函数 profile inclusive 19.60ms。当前 r67 已从该 selector 严格解码结果局部复用 outcome，并在消费前再绑定独立 frozen leaf。这项已经修复的重复不能再计入候选收益。独立 pub、事实标量、entity 与冻结采用对照仍有职责。

较大的 101.55ms init 来源是 `_selected_accounting` 和采用证明：opening selection 与资金 selector 各自使用不同 kinds／subject universe，不能整份互换。虽然只请求 state，selector 仍须核实际 voucher identity，防止把已被凭证表示的 calculation 当成独立 no-line 状态。当前完整 scoped reader 还独立排除 mutable publication 目录遗漏，不能只依目录缩短历史月份。

固定诊断的 accounting family 物理读取为 47 次／47 行、1,868,298B；每个冻结月份只读一次。96 次 directory 和 block SQL 分别返回 192 行，来自不同 adopted_results／vouchers 字段与精确 subject bucket；`_verified_close_storage_parts` 已按 period、storage digest、field、bucket 复用，`ChainMap` 避免逐次复制整个成功缓存。没有证据同一个 family 或 bucket 大对象重复传输，不能将 96 次 slice 与 47 月机械相减当作可删量。

## 同类公共路径审查

| 范围 | 代码与诊断证据 | 决定 |
| --- | --- | --- |
| 五页和 CLI／MCP 共用选择、来源、金额证明 | brief／funds 通过共享 `QueryReads`；资产也调用 `balance_totals/verify_selected_balances`；清偿 `verify_settlement_periods` 调同一 publication period 证明。其他页面若没有前序精确 ID 证明，不产生本轮已确认的重复。 | publication 建议应落公共 helper；不把代码相似写成五页都有相同重复。所有页面接口保持原消费者核验。 |
| fullverify／close／repair／backup | `projections.require/repair` 的 period balance 重建；`require_period_balances` 先核完整 publication chain，`_period_seals` 无 QueryReads 时自行完整读取／verify；settlement standalone 分支也逐条 verify。 | 独立入口保留严格物理读取与全链／重建职责；不得依 UI 前序证明或改变 repair 的成功语义。备份及关账下游共用完整核验，不宣称本轮已运行。 |
| fixed v1／历史 reader context | 内容 reader 与 current registry 是不同边界，历史上下文即使持当前 registry 仍须自己的 reader。 | 建议若以后实施，须两个 reader context 的实际 fallback；单凭版本数字不够。 |
| 实际 voucher lines 与保存 tuple | 一次 2,294 行／224,929B 实际 line SQL；1,006 个唯一保存结果的 `_lines` 比较完整 tuple。 | 必要 fullmonth 工作；不做 `_lines` 微优化或字段裁剪。 |
| 保存结果、fact 与 source anchor | `verify_selected_content` 六调用只有两次 missing-body SQL，共 1,009 行／1,239,867B；精确成功 ID 已复用。活动 anchor 是独立不可变采用绑定。 | 不以调用数当 body 重复；结果 body+digest 自洽不能代替 anchor。待办 raw→typed 已由父任务修复。 |
| 历史／current 清偿页 | `_scope(period,current)` 是不同截止语义；root、tail、block、state 精确缓存已存在。两次目录是 all／open 不同职责。 | 不合并历史与当前金额。`_verified_page_sources` 两次 41 行小 header SQL 没有保存参数，无法证明实际 ID 重叠，标为未验证。 |
| `_totals` 与 monthly_account 小项 | 相同 tail 发现两次，54,000VM、诊断约 2.18ms；monthly_account 两次共 42 行／872B／300VM，前者净额、后者借贷完整比较。 | 仍是已知剩余小重复，本轮不扩成额外改动循环；不承诺该类重复完全消除。 |

## 本组验证状态

三文件 Ruff 已通过。新增 `tests/kernel/test_publication_period_reuse.py` 已准备真实 SQLite 的 1,005 已核／23 未核传输和 hash 工作量比较、增加 5,000 条无关月份已核 ID 后请求阶段 SQL 参数／返回行／字节／VM 不增长、八个未核字段损坏、nonowned／写事务／disabled／registry v1／historical v1 context、warm／新期／跨 snapshot、ID→period 冲突、末尾失败原子缓存与真实 current voucher→period seal 证明。生产源等待父任务与员工定位修复一起固定，再运行本组定向 pytest，避免测试期间修改共享生产源。当前未运行 pytest，也不将准备好的用例写成通过。

当前有证据的新增改动只有 publication 内容复用的小项。对资金 init 的更大职责删减，金额 proof 与银行覆盖名单之间存在真实业务消费，旧诊断没有证明可删的大块重复；暂不建议生产修改。若后续要改变“哪些已开账户必须提供本月流水”的覆盖语义，应作为明确产品决定及定向验证，不能以性能名义悄悄改变简报风险结论。
