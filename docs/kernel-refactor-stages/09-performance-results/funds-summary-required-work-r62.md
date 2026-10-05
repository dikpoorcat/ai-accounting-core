<!-- @format -->

# r62 资金汇总的必要读取范围

本组只处理已证明的资金汇总职责和同快照已完成的凭证认证。固定 r61 ResidentReadPool 诊断显示，简报的资金读取约 117.7 毫秒，其中 account_summary 约 89.7 毫秒；这不是纯浏览器验收。当前候选尚未经过最终独占浏览器计时。

| 范围／调用方 | 实际消费与证据 | 决定 |
| --- | --- | --- |
| FundsRead.movements → account_summary，简报／资金 | 汇总不消费 local_index、page_key；r12 的原始实验只替换这个 row_number，在当时路径下未见稳定收益 | 保留 r12 的负面结果。老板看板精简后的当前路径尚未重新验证，不能据此永久排除；本轮保留当前 SQL，不重试窗口排序。r62 同一诊断在确认历史原件之前已经完成，保留原件，不能登记为修复 |
| FundsRead.bank_summary → 简报／资金摘要；完整 statements 页 | 银行摘要只消费金额、状态、计数、日期，不消费 source_row_count、source_rows_total_fen 两个窗口。r12 原件没有修改这两个窗口 | 原匹配、来源、事实、冻结父关系和状态判断保持不变；无分页摘要使用相同银行源但省去两窗口。bank_source 和完整 statements 页仍保留批次字段及窗口 |
| Journal.account_amounts → FundsRead.events(current=True) | 必要全月金额认证已加载实际完整凭证行和头。events 原来仍在来源发现、冻结引用、最终 effects 三次执行同一月选择器 | 同一 owned active 事务、当前读取器和精确整月成功证明存在时，按已认证 actual voucher_lines 的账户与已认证头的主体过滤，复用头做来源发现和精确引用定位。最终 effects SQL、verify_sql_outcomes、资金来源及独立冻结采用检查保留 |
| 无成功证明、其他月份、失败、子范围、历史事件及固定 v1 | 局部头、先前事务或单纯正文摘要不能代表完整整月采用和实际行 | 不新建证明标记或跨请求缓存，保留原选择／核验路径。公共 CLI／MCP 的 dashboard 映射使用相同规则 |
| settlement historical／current scope | _tail_rows 虽调用两次，真正的当月尾段 SELECT 只有一次、1,246 行；第二次只检查截止日之后的事实 | 保留，不能按调用次数认定重复或删去当前知识边界 |
| 账户余额、零账户、独立期初、退休身份 | 历史余额只读最近冻结根、相关桶和开放尾；历史银行身份仍影响 missing／coverage；退休账户判定需要真实 opening／closing／movement | 保留，不把全账户字段未展示解释成可以删去身份、发生额或未知传播 |
| 员工、资产、季度、核心关账／完整核验／修复／备份 | 员工／资产选择头的窗口确定权威版本，分页窗口支撑计数，均有实际消费者。季度无本组 FundsRead 明细窗口；核心和固定存储核验不以老板汇总代替来源证明 | 不修改这些路径。Journal 的 1,006 个 source_lines 检查各一次，未认定为重复；owner_tasks 的完整事实和资料认证也保留 |

## 私有固定 r61 对照

原始 SQL、EXPLAIN QUERY PLAN 和精确 VM 步数保存在 `.tmp/stage9-funds-summary-r62-ab.py/.json/.log`。银行纯摘要逐字段相等，共一行：126,649 → 85,629 VM。该对照不代表端到端时延收益。money 窗口的已完成诊断为 244,604 → 216,813 VM，共两行，未采用；只证明该次诊断的 VM 减少，未证明整页收益。它不改写 r12 当时的结果，也不能证明精简后的路径没有收益。

已有金额证明复用的第一稿使用普通 JSON JOIN 定位冻结引用，SQLite 选择了错误驱动：响应相等，但 1,572,500 → 29,392,200 VM，未采用。`.tmp/stage9-funds-proof-r62-ab-first.py`、第一稿 `.json/.log` 独立保留。第二稿沿已有 read_indexes 的精确输入驱动规则，使用 CROSS JOIN 和 close_reference_lookup；完整简报除 generated_at 外相等：

| 实际工作量 | 原路径 | 既有证明复用 |
| --- | ---: | ---: |
| SQL | 312 | 311 |
| 返回行 | 19,723 | 19,219 |
| 返回值字节 | 8,008,093 | 7,974,829 |
| VM | 1,572,500 | 1,467,800 |
| 结果正文传输／解码 | 1,058／1,057 | 1,058／1,057 |
| typed facts／采用子片段读取 | 121／24 | 121／24 |

第二稿保存在 `.tmp/stage9-funds-proof-r62-ab.py`、`-ab-r2.json/.log`。它只复用必要金额核验已经加载的头和行，未新增加整月 header materialization；与历史为了 SQL 汇总额外返回约 644 KB 的负实验不同。正文读取和解码没有下降，不能宣称来源内容装载减少。

## 定向验证

生产只改 dashboard_funds.py，新测试为 test_funds_required_summary_work.py；没有修改 Journal、QueryReads、SQL 结构或任何固定 v1 文件。新测试覆盖银行原完整明细源与窄摘要逐字段／VM 对照，实际账户与主体过滤、内部转款、已证明头复用工作量、不同范围／新快照／固定 v1 fallback，以及默认 20 条外实际行和结果损坏。最终组同时覆盖原资金汇总分页、实际冲正、独立原月锚、无影响复核、无凭证零金额来源、银行覆盖及独立期初。

第一集中组 **32 passed in 52.69s**，日志和 JUnit 为 `.tmp/stage9-funds-required-work-r62.log/.xml`。该组中的原无影响复核／冲正用例走普通资金页，不能当作新复用入口的直接覆盖，随后补三个真实分支：先执行完整 Journal 金额认证，再读资金事件。开放冲正与原件正文、自带摘要共同损坏后的独立原月及完整核验拒绝 **2 passed**；无影响复核首轮因测试误断言闭期 basis 必须是新 head 而失败，原 `-branches-r62.log/.xml` 的 **1 failed, 2 passed in 8.91s** 保留。正确语义是冻结凭证继续使用原 owner／basis，独立 close adopted head 为新 review；按这两项准确等式、完整 effects 相等和旧 owner 的输入身份认证改写后，该失败用例单独 **1 passed in 6.20s**，记录为 `-noimpact-r62.log/.xml`。共 **35 个不同用例通过**，不把补测合写为一轮无失败记录，也未重复此前 32 项。

最终 Ruff 通过。生产与测试停止修改。没有运行全量、构包或纯浏览器计时，净时延收益和阶段是否达标仍由根线程在候选固定后独占验收。
