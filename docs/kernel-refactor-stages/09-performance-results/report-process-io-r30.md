# 阶段 9：r30 季报进程读取请求与 SQL 工作量

本记录只分析固定 r30 源的 `Dashboard.quarterly_report(..., preparation="deferred")`。固定源清单 SHA-256 为 `bbb18c584ee827224daade05fbb138d688b4249d35b850d3d02171dcff84da2b`；12 月和 48 月合成库对应的 r30 完整核验报告均为 `complete`。三份原始 JSON 已无损 gzip 归档，原文字节数和 SHA-256 见[归档清单](stage9-r30-report-process-io-archive.json)，且已逐字节解压回验。12 月完整响应 SHA-256 为 `67fcefb9792ad9531d21627d66c2a3fcd327123b0c047f32d70055a014947e01`，48 月为 `40a08f26a8a187f1f6d1e41d8c847575d97396102c5603a274f35d43c2ff370d`；各自未插桩与插桩调用相等，不表示两档公司的内容相同。

探针在同一进程两次热身后取一次完整原生响应，再在另一轮相同响应里逐 SQL 统计 VM、返回行／值字节、Windows `GetProcessIoCounters` 增量。存在并行构建或核验负载，墙钟及 CPU 均为**诊断**，不是浏览器 500 ms 验收。`ReadTransferCount` 是本进程发出的读取请求字节，不能直接解释为物理磁盘读取量，也不提供 SQLite 哪个页被重复请求的身份。

| 完整 deferred 季报 | 12 月 | 48 月 |
| --- | ---: | ---: |
| 原生进程读取请求 | 451 次／1,838,883 B | 11,514 次／47,151,648 B |
| 原生墙钟／进程 CPU，诊断 | 294.2／281.3 ms | 458.4／468.8 ms |
| SQL VM | 973,179 | 1,198,535 |
| SQL 返回行／值字节 | 26,131／9,729,792 B | 26,961／10,953,779 B |

48 月的逐 SQL 读取请求可定位至具体工作。下表的「读取」仍是进程请求量；同一语句 12／48 的 VM、返回量对照来自此前保存的[逐 SQL 计划与原文清单](stage9-r30-report-sql-family-archives.json)，**不是**12月逐语句 I/O 对照。清单中的 12／48 月压缩原文含实际绑定查询的 `EXPLAIN QUERY PLAN`。

| 48 月 SQL 族及调用链 | 48 月进程读取 | 12→48 月 VM；返回 | 查询计划与范围判断 |
| --- | ---: | --- | --- |
| `read_open_contributions` 从 calculation ID 取 publication／anchor／body | 9.19 MB | 24,129→24,129；1,004→1,004 行，3.282→3.322 MB | `json_each(ids)` 后依次以 calculation、publication ID 精确索引查找。938个往来源首次读取，随后报表只补66个未验证源；正文、不可变 anchor 和 digest 均为现有来源证明的一部分。返回量仅涨约40 KB，较多请求不等于扫出无关来源。 |
| `browser_report_details` 对计划事实 ID 计数 | 6.16 MB | 64,214→256,718；均1行／16 B | 计划 ID 从4,938增至19,746；每个 ID 以 `fact_revision(id)`、`subject(id)` 索引核对种类。这里 **VM 确实随真实历史身份数增长**，是已解释的工作量，不是固定范围的异常重复扫描。页面只消费两类计数，但目前没有已证明等价的上游权威计数来源；公开 ReportPlan 仍需完整 ID。 |
| `_verify_anchored_source_bytes` 等对源计算／事实身份、封签的读取 | 5.08 MB | 43,250→43,250；均1,005行，1.204→1.237 MB | 按精确 calculation ID 查 `calculation`、`fact_revision`、`subject`，再核 seal；无全史表扫描。该查询与贡献正文针对同一源集合，却验证不同权威对象，不可把两条 SELECT 直接当重复证明删去。 |
| `_authoritative_rows` 的本月凭证和行 | 4.95 MB | 228,224→232,976；2,220→2,292行，0.902→0.936 MB | `voucher_period(period=?)` 驱动并按 voucher ID、basis calculation、publication 查当前月权威行；报表后续复用这些行。没有从此证据看出误读全历史凭证。 |
| `dependency_calculation` 的精确上游边 | 3.59 MB | 16,585→16,981；1,140→1,176行，150→155 KB | 对所需 calculation ID 逐个按 `(calculation_id)` 覆盖索引点查；边集合用于贡献正文中保存的 parent 精确对照。返回小、读取请求大，值得关注页访问，但不是 SQL VM 随历史线性扫描。 |
| 冻结 flow／close 根和 classification | 约数 MB，分散于多语句 | 旧月根11→47；分类头4,522→4,932行 | 报表需认证覆盖期内冻结根与目录。47 月相对11月的首次根读取不能记成同一根的重复。开放期分类候选返回403行，未见随历史膨胀。 |

按 phase 合计，48 月 `report` 为19.96 MB、`open_contributions` 为21.01 MB、`browser_details` 为6.16 MB。贡献 body、源身份、依赖三条就占约17.86 MB；其 VM 基本不变，返回量合计仅增加约78 KB。SQL 计划是按精确 ID 做多次索引及正文 B-tree 访问，而非全表扫历史行。48 月库文件约3.56 GB，12 月约0.59 GB；较大工作集、连接页缓存或物理布局导致同样查询发出更多页读取请求是**可检验解释**，不是本探针已经证明的原因。此前 64／256 MiB SQLite cache 对照没有稳定收益，不能据此盲目提高缓存或启用 mmap。查询计划相同也不能证明页面访问成本相同。

现有 `rowid_order_feasibility` ABBA 仅将已查得的 body 结果改为不同 `ORDER BY`。两条计划均为 `ids→publication→body` 后 `USE TEMP B-TREE FOR ORDER BY`，四轮均约64次／260,413 B，VM／结果多重集相同。它只否定**输出重排**的收益，未改变实际正文获取次序，不能拿来否定「先精确定位 rowid，再按 rowid 装载正文」的两阶段办法。若以后评估两阶段读取，仍须完整保留 calculation→publication→anchor/body 的精确身份、缺行拒绝、digest／seal／source binding／parent 核验，并比较整页 CPU、VM、I/O 与完整响应；当前没有足够净收益证据实施。

这组进程 I/O 数据仅覆盖 deferred 季报。五页对应入口的静态分界如下，不能套用本表47 MB 数值：

| 页面 | 当前主要读取职责 | 本轮 I/O 能否归因 |
| --- | --- | --- |
| 季报 | `Dashboard.quarterly_report→Reports._report(open)→browser_report_details`，完整计划 ID、往来与报表来源、两类技术计数 | **可**；本探针正覆盖这一条。 |
| 简报 | `Dashboard.brief` 父事务的明细、资金／工资／资产概况，及并行准备／报告检查；`check_report_readiness` 调 `_report(_issues_only=True)` | **不可直接套用**。issues-only 不生成季报公开19,746个计划 ID，也不跑浏览器计数；报表 proof 家族虽部分相同，范围和事务边界不同。 |
| 资金 | `Dashboard.funds→_funds→FundsRead` 的账户、流水、对账及可选准备 | **未测**；其结果 JSON、银行来源和分页 SQL 与本季报贡献 body 查询不同。 |
| 员工 | `Dashboard.employees→_employees` 的采用头、工资、员工资料和可选详情 | **未测**；工资来源及身份候选有自身证明路径。 |
| 资产 | `Dashboard.assets→_assets` 的成员采用、消费和来源；可选详情 | **未测**；资产成员、历史身份和来源各有独立选择。 |

共享的 `QueryReads`、冻结根和分类读法提示可能出现类似点查成本，但各页精确 ID 范围及事务不同，尚无对应进程 I/O 数据。

## 公共入口与同型点查的静态边界

`command_schema.py` 和 `service.py` 同时把 dashboard 五页、`report`／导出、关账、完整核验和维修注册为 CLI／MCP／服务可达命令；入口不同不能假设共享一个 `QueryReads` 事务。下表列**位置和处理决定**，而非给未测入口套用本轮进程 I/O：

| 入口与调用位置 | 同类读取及决定 | 实测状态 |
| --- | --- | --- |
| 五页默认与按需详情：`Dashboard.brief/funds/employees/assets/quarterly_report`，`Dashboard._business_collection`，`BusinessQueries` | `QueryReads.verify_selected_content`、`verify_close_references`、`Store.fact_data_many` 按各页精确 ID／period 验计算、事实与冻结引用；详情再按所选对象读。`brief` 的资料／准备 worker 有独立事务，不能跨 worker 传未经本事务认证的 proof。维持目前源／根校验与默认明细数量。 | **仅季报 deferred** 有本轮 I/O；其余页及详情未验证同量增长。 |
| 页面 context／准备／核对：`dashboard_metadata`、`Periods._manifest`、`BusinessQueries._period_readiness`、`CloseReview.read` | 按当前月份或历史冻结范围检查，可能触发 `check_report_readiness→Reports._report(_issues_only=True)`、`QueryReads` 冻结根。它与公开完整报表计划不是相同消费范围；首次权威证明仍必要。 | 本轮未测；不能将季报浏览器 ID 计数归给准备／核对。 |
| 公开报表、导出、CLI／MCP：`Reports.report`、`preview_export`、`confirm_export` 与 `Reports._report` | 公开 `ReportPlan.fact_ids`、金额和 hash 仍需完整来源；`report_projection.party_balance_rows→read_open_contributions` 与 `_report→read_open_contributions` 在同个 snapshot 共享已成功来源。季报的浏览器技术计数是额外消费者。 | 完整 deferred 页面已测；独立 CLI／MCP／导出事务未测 I/O，不能缩公开 ID 合同。 |
| 关账：`Periods.preview_close/close→_manifest` 与 `check_report_readiness` | 完整准备、冻结 trace 和权威源证明不能由页面结果替代；可能调用相同 `Reports._report`，但为独立事务与不同 cutoff。 | 未测本轮进程 I/O；保留独立核验。 |
| 完整核验、维修：`integrity.verify_integrity→compare_open_contributions`，`Maintenance._repair→verify_integrity→repair_open_contributions→verify_integrity` | 从权威源重建并比较全部不可变 anchor、正文及投影；不走页面 `read_open_contributions` 的成功缓存。维修前后各自完整校验有不同职责。 | 未测本轮进程 I/O；不能把普通页的两阶段读取假设替换完整核验。 |
| 备份、恢复：`backup.create_portable/verify_portable` 最终进入完整内容核验 | 包成员、数据库、身份和源内容须独立检验；与用户页面不在同一事务。 | 未测本轮 I/O；不承接页面的读结果。 |
| 固定 v1：`report_open_contribution_v1`、`report_projection_v1`、`history_reads_v1` | 自有历史内容规则和选择器；current 的快照缓存或潜在正文排序实验不能渗入已发布 v1 规则。 | 未测本轮 I/O；不修改。 |

这一矩阵只说明同型点查在哪些职责内出现；没有逐入口的 page ID、I/O 或完整响应对照，不能把「代码调用相似」写成重复物理读取。闭月、修复、备份及 v1 的首次或再次独立权威验证都按原合同保留。

## 两阶段正文读取已证负面

私有 A/B 原型只替换 `read_open_contributions` 的候选正文 SELECT；后续 content 对不可变 anchor／body digest、精确 calculation ID、source bindings、parents、来源 header、seal 的原检查原样执行。完整 deferred 季报各档 A/B 响应 SHA 相等。四份原始结果以无损 gzip 保存；逐份原文字节数、原文 SHA-256、压缩 SHA-256 见[两轮 AB 证据清单](stage9-r30-rowid-two-stage-ab-archive.json)。固定样本未注入损坏，因此证明的是正常完整响应等价及原检查代码保留，不宣称损坏分支已动态回归。

第一轮的 A 阶段虽未取 `body.content`，却仍取 `d.content_digest`，致使 SQLite 访问正文表页；它不是覆盖索引定位，不能作为两阶段方案的最终判断。修正版 r2 的 A 阶段只取 publication、anchor 与 `d.rowid`，计划明确 `SEARCH d USING COVERING INDEX ... (publication_id=?)`；B 阶段以 rowid 主键装 `content,content_digest`，计划明确 `SEARCH report_open_contribution USING INTEGER PRIMARY KEY (rowid=?)`，没有输出排序 temp B-tree。已否定的旧 `ORDER BY rowid` 只在取正文**之后**重排结果，也不是 r2。

| r2 完整季报 A→B | 12 月 | 48 月 |
| --- | --- | --- |
| 原候选正文 SQL 进程读请求 | 219,520 B | 9,189,693 B |
| 新 header／rowid body SQL 进程读请求 | 0／219,520 B | 4,689,920／4,467,005 B，合计9,156,925 B |
| 全调用 SQL VM | 973,179→983,254（+10,075） | 1,198,535→1,208,610（+10,075） |
| 全调用返回行／值字节增量 | +1,004／+16,064 B | +1,004／+16,064 B |
| 正文解码 | 两版均1,004次、3,018,776原文字节 | 两版均1,004次、3,059,132原文字节 |
| 原生 A/B/B/A 整页进程读请求 | 四轮均1,838,883 B | A 47,286,816；B 47,118,880／47,397,408；A 47,286,816 B |
| 原生 A/B/B/A 进程 CPU，诊断 | A 265.6；B 281.2／281.2；A 265.6 ms | A 484.4；B 500.0／515.6；A 484.4 ms |

48 月候选 SQL 只省32,768 B 请求，整页 B 两轮一低一高，没有稳定净降；header 仍需约4.69 MB 的精确索引／publication／anchor 页，正文主键装载另需4.47 MB。两阶段反而多10,075 VM、1,004行、16,064 B 值传递，并增加 CPU。12 月进程读取量完全相同。因此**不实施两阶段正文读取**，也不为它扩大回归。这个负面只针对现有结构与本次 ID 集合，不能据此断言所有可能的数据布局无效。

本轮最终能证实的增长类型是：浏览器计数随完整计划事实数增加的真实 VM 工作，以及本月来源数近似固定时多个精确索引点查族的进程读取请求上升。尚未证实的是这些请求对应的物理盘 I/O、相同页重复读取、无关业务行被扫描，或某个可删的100 ms级重复证明。当前合同内没有经证据支持的生产改动；不重试已否定的 cache/mmap、强制 kind 索引、SQL party 聚合、输出重排及本次两阶段正文方案。
