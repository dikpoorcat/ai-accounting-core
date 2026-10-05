# 已核资金效果的重复组装（2026-10-03）

资金摘要及报告源分读已进入固定候选 `fe3a0ba43267bb05f3159bf38aeca13bf90f8cdc8c9b774882a6a560f27bc8f6`，主12资格重新通过，150次纯浏览器全部成功，但简报3次、报表2次超500ms，整体over_target，尚未性能达标。资金130项通过；报告新专项12项通过，关联组134项通过／1项旧消息匹配失败分别保留，不相加。后续生产新修改另行固源和验证，不能回写为本次fe3内容。

资金修复的触发条件是只需要完整摘要，且同一只读快照已经核验本月资金效果；原实现仍编码JSON、由SQLite解析并生成未使用的分页序号再聚合。主48旧原生诊断中该摘要SQL为153400 VM步、2行，这是组装工作，不是核验多余，也不能将简报10175661返回字节全部归为正文。旧46报告源分读原型与新生产测试分开记录。

## 排查范围与决定

| 位置及调用方 | 实际职责 | 决定 |
| --- | --- | --- |
| `FundsRead.account_summary(page_request=None)`；简报 `summary_only`、资金汇总及未请求流水集合的读取 | 摘要不消费流水分页序号；部分请求已有完整核验的效果 | 在已有资格内直接汇总；仍先执行 `events(current=True)` 的来源、采用、冲正原件和引用核验。 |
| `FundsRead.movements`、资金默认及账户筛选分页、流水详情 | 稳定分页、完整范围汇总和精确账户筛选 | 保留原 SQL 路径；不先拉全部明细，也不改变首账户或分页。 |
| `Journal.account_amounts`、期间余额、清偿汇总 | 凭证行一致、实际入账和清偿含义分别核验；Journal→Funds 已同快照复用 | 保留必要读取；不能用余额投影替代完整凭证行证明。 |
| 员工、资产、季度报表 | 领域汇总与资金摘要不同；资产身份优化已单独收口 | 不强行套用资金汇总。46固定源分类头4932refs／4932unique、月间交集0，为必要读取，不实施头缓存；报告源分读原型见下节，不能冒充新生产验证。 |
| CLI/MCP 看板读取 | 经过相同服务及看板内部读取 | 接入同一窄改动，不新增入口或响应字段。 |
| 完整核验、关账、修复、备份恢复、固定v1历史读取 | 独立核对权威来源与冻结内容 | 不接入新快路。闭期、固定v1、非当前自有快照及正文不齐保留原 SQL。 |

## 修改边界

新汇总只消费 `_verified_money_effects()` 已经允许的精确内容，不为获得资格补装载结果。完整事件核验成功后才消费，不增加跨请求缓存。后续期初、零额账户、未知金额、归属调整及账户档案处理保持原样。

保守检查全体效果的绝对金额合计不超过int64正上限；可能发生 SQLite SUM 中间溢出、INT64_MIN 或异常字符串编码时回原 SQL，保留原异常。冲正按原 `sign` 撤回收付，不能显示成再次收付款。内部转款仍按同一事件的不同账户、原金额合计为零及已有三种转款类型判断。日期最大值忽略null，空集合仍为空摘要。

定向及关联测试比较新路径与强制原SQL的完整金额、账户和日期，保留三种渠道、内转、反冲、零／未知账户、溢出回退、晚核验失败及固定历史拒绝。显式page_request保留原分页，首账户选择先取得完整摘要再走原流水分页，不以摘要代替明细。

## 已完成测试与测量工具

| 独立回执 | 实际结果 | 原件 |
| --- | --- | --- |
| 资金摘要首轮定向 | 6 passed，14.25s | `.tmp/stage9-money-summary-first.log/.xml` |
| 补分页绕行、真实SQL溢出及未知期初后 | 新摘要文件6项＋原owner_funds_summary_scope未知期初单例1项，共7 passed，11.25s | `.tmp/stage9-money-summary-directed.log/.xml` |
| 资金／汇总／来源等8模块组 | 130 passed，216.09s，3 warnings；包含同一修正摘要文件6项及ownerScope全文件中的未知期初场景 | `.tmp/stage9-money-summary-group.log/.xml` |
| 浏览器资源logger工具 | 6 passed，119.2133ms；只验证资源时间戳与归属记录，不证明页面变快 | `.tmp/stage9-browser-resource-directed.log` |

3个warning均为pytest `record_property` 与JUnit xunit2格式兼容提示，未作为业务失败。Ruff与diff check通过。新文件始终为6项（large含2个参数），首轮与后轮的区别是补入分页及真实溢出断言；后7项和130组包含相同修正代码及未知期初场景。各轮不相加；130组运行于当时主仓库draft代码，未绑定最终released固定源，report_projection生产改动在该组完全结束后才应用，不能把130组移作新报告实现通过。工具用时不是页面用时。

报告生产专项 `.tmp/stage9-report-split-directed.log/.xml` 为12passed、18.27s、4warnings；关联组 `.tmp/stage9-report-source-group.log/.xml` 为134passed／1failed、248.78s、7warnings。失败为 `test_open_report_rejects_missing_no_impact_review_publication`：损坏仍拒绝，旧regex期待“当前凭证缺少正式发布采用”，实际为“正式发布缺少精确当前核算来源”。原失败保留；旧46已实际复现同错，当前消息及拒绝守卫另11passed；不能把组写成全通过，也不把后修测试视为封存fe3原内容。

## fe3主12资格、纯浏览器及ResourceTiming

新资格四项coverage均verified、limitations=[]，真实开放月预览通过；30,225事实、12,413计算、12,309凭证、11关账和85证据保留。正式前端、默认20条、每页3次预热及30次刷新，150次全部成功、慢样本全保留：

| 页面 | 中位ms | p95 ms | 最大ms | 超500ms／30 |
| --- | ---: | ---: | ---: | ---: |
| 简报 | 417.3 | 516.3 | 737.0 | 3 |
| 资金 | 296.8 | 336.6 | 336.6 | 0 |
| 员工 | 236.1 | 276.3 | 276.9 | 0 |
| 资产 | 255.4 | 276.5 | 277.0 | 0 |
| 报表 | 417.4 | 516.9 | 592.4 | 2 |

控制器实际记录copy PID14312计时前后creation、kernel和user CPU计数相同，`copy_cpu_unchanged=true`；计时结束状态over_target，resume_status=0，copy恢复成功。计时期间没有copy CPU工作，不能据此推导每项网络延迟由何种服务器工作导致。

从原始browser JSON的每页30份ResourceTiming派生下表；duration为资源start至responseEnd，尾部为两个关联响应中最后responseEnd至原renderAt。统计中位为中间两项平均，p95为nearest rank；不是服务器CPU测量：

| 页面 | context中位／p95／最大ms | main中位／p95／最大ms | 响应后尾部中位／p95／最大ms |
| --- | ---: | ---: | ---: |
| 简报 | 54.3／65.6／102.2 | 389.5／492.6／695.4 | 30.0／41.3／41.4 |
| 资金 | 52.5／72.3／76.3 | 272.5／298.6／303.1 | 28.1／39.4／40.5 |
| 员工 | 52.8／64.3／70.7 | 199.8／245.7／249.9 | 29.1／40.9／40.9 |
| 资产 | 53.3／64.8／84.2 | 215.0／241.4／245.3 | 27.8／40.4／40.6 |
| 报表 | 53.8／61.8／65.9 | 395.0／479.4／557.0 | 30.4／39.9／40.6 |

全部超限样本（各页从1编号，不含预热）如下；context与main并行，不能把两者duration相加：

| 页面／样本 | 整页ms | context／main duration ms | main request至responseStart ms | 点击至最后响应ms | 响应后尾部ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 简报4 | 516.3 | 65.6／492.6 | 490.5 | 493.3 | 23.0 |
| 简报5 | 737.0 | 102.2／695.4 | 693.4 | 696.1 | 40.9 |
| 简报12 | 515.5 | 49.9／477.8 | 475.8 | 478.6 | 36.9 |
| 报表1 | 516.9 | 65.9／479.4 | 476.8 | 480.1 | 36.8 |
| 报表2 | 592.4 | 55.8／557.0 | 554.1 | 557.8 | 34.6 |

请求到首字节包含排队、传输、请求处理等，尾部包含响应处理、JSON、Vue DOM和现有两次RAF完成检查；不能称serverCPU、纯绘制或孤立paint时长。派生脚本及JSON只读取已结束报告，未重跑浏览器。当前fe3主48、主120及独立／压力规模仍未取得本组资格和性能回执，旧46主48结果不继承为fe3。

主120原隔离raw copy现已exit0，状态仍为copied_content_unverified，原复制报告SHA `b177f5ae3c08e052bda684aa79f34afbb6cbb079eb9762b5d3fd924f41a2e548`。新独占helper绑定fe3候选，同时保留19ae实际复制来源；只检查来源清单、结构／内容合同兼容、身份、epochs及冻结metadata，生成 `.tmp/stage9-main120-summary-groups-construction.json`（SHA `12bf325c25b0f004f7f0cb8e99dae7e55bd52f9e13a129b1fdca1a794076982b`）。该输入status=unverified，四coverage均unverified，measurements={}；原month_stats.seconds仅属历史构造统计，未重新复制、完整核验、预览或浏览器。初执行因旧helper漏传load_bundle必填版本参数失败；仅新helper补显式released/1后成功，初失败日志保留，旧helper及checkpoint未改。

旧46固定源实际复现同一无影响复核发表缺失错误；当前测试消息及拒绝／缓存守卫定向11passed（10.54s）另记，不抹掉原关联组134passed／1failed。fe3新native与正常授权dispatch均已完成仪器诊断，前者记录实际工作量，后者包含授权边界；instrumented耗时不替代页面耗时，也不构成主120资格或整组性能通过。

## 报表读取调查与分读原型

两项原型固定来源为 `46f84508afd54165f23af5a83434b1881002ab45490f4358f0c1755c743748be`，只使用明确合成主48。分类头跨2018-12至2019-11每月411条，共4932refs、4932unique、重复0，月间交集0。实际头读取VM187600、返回784188字节，与已有native对应；没有重复头可供缓存收益，因此不实施分类头缓存。这是必要读取判断，不是性能通过。

开放来源分读修正原型实际比较两个真实case：2019-12非空2292条与2020-01空集合0条，完整有序tuple及发表证明ID均完全相等，保留原full proof，不改变闭期来源核验。工作量为：

| case／指标 | 原完整路径 | 分读原型 |
| --- | ---: | ---: |
| 非空输出 | 2292 | 2292 |
| SQL | 19 | 20 |
| 返回行 | 5335 | 6340 |
| 返回字节 | 1997799 | 1607450 |
| SQLite VM steps | 413800 | 352600 |
| 结果正文读取／字节 | 1／668 | 1／668 |
| 空case：SQL／行／字节 | 8／3／64 | 8／3／64 |

SQL及头／返回行反增，不能只列VM与字节下降或称净提速；空case没有SQL进度VM计数，日志0→0只表示未计到进度步，不能称所有SQLite工作为0。初版已取得非空case数值，但打印空case时缺 `sqlite_vm_steps` key触发KeyError，整轮失败；失败.py/.json/.log保留，修正fix1另存，不改写为通过。原型小型非空fixture未执行；新生产专项另有12passed，关联组旧消息失败及后续修正仍按独立回执处理，不由原型自动验收。

## 原件保全与待验范围

上述16份明确合成脚本、JSON、日志及XML独立归档于 [owner-money-summary-group-20261003-manifest.json](owner-money-summary-group-20261003-manifest.json)，清单SHA256为 `17e8b78920569578684a91d3eec45ac981f988f8232030856b1f7f769be30c7b`；每件新gzip独占创建、mtime=0、raw bytes解压一致。只包含本组明确文件，不含DB、凭据或原checkpoint，不覆盖此前归档。manifest区分主仓库未固源测试、固定46原型、初失败／后修正和logger工具，并记录原字节及gzip SHA。

新fe3主12、测试及来源封存metadata共20份原件另存于 [owner-summary-groups-fe3-main12-20261003-manifest.json](owner-summary-groups-fe3-main12-20261003-manifest.json)，清单SHA256为 `97aacd15affd8692602ea5766f1cdb7da2a933f119b210b41eba2c7a90001041`。同样独占创建gzip、mtime=0并逐件验证原字节解压一致；保留关联组原失败，区分draft测试与固定fe3资格／浏览器，派生ResourceTiming统计不是新测量，不覆盖上述16件或此前归档。

上述旧46复现、11guard／当前test-only原文、fe3 native及授权dispatch另11份原件独立归档于 [owner-summary-groups-diagnostics-20261003-manifest.json](owner-summary-groups-diagnostics-20261003-manifest.json)，SHA `db4c09a72ced06bdedb5e37c8a72f2fc2bc75336ab96777bb8323c197e189675`；gzip独占、mtime=0并核对原字节往返，当前测试原文不是fe3封存源。没有覆盖旧归档，也未收DB、凭据或原checkpoint。

本轮整理未运行测试、qualification、preview、浏览器或新测量；只执行主120构造metadata helper对明确合成复制库的只读检查。固定fe3主12资格及整页证据已经取得，但5次超线仍未通过性能目标；本组其他规模及后续生产改动须另取证。完整关账、维修、备份恢复、fixed-v1最终范围保留各自验收，不以本组窄改代替。
