# 精确月范围的重复工作（2026-10-03）

本组收敛两个窄范围：简报负责人待办在月驱动SQL中重复按五种kind查同一subject；简报凭证分页在本次快照已成功核验完整月头之后，再装配同一候选集。前者已改谓词，后者已接入成功月证明的排序／定位复用；同1c37固定候选主12、主48资格完成，主12本轮150success／0超线通过，主48150success／26超线仍失败。主120／独立／压力及最终包未验，不能由局部通过推导全部验收；新旧对照存在涨落，不称稳定整体收益。GC诊断没有支持生产修改，初版仪器污染和修正回执分开保全。

## 新固定源主12实际回执

新source为`1c37c576cb0026b0b39c199b74672a60890c43a7d9e2b944416179fe2bf398e9`，封存manifest SHA `845318d7e9857cbae422033a7e8b87a5fb7edd2896f1c5f0617709eabca1c979`，只属隔离released/1候选，正式仓库未切版。主12资格回执SHA `066dff5d70b1ae0a37d6f8f615c4242af2c0f6a6269c507f062a4a7bb739fa58`，status=complete，sources／historical_adoption／projections／read_indexes四coverage verified、limitations=[]，完整注册核验及真实开放月预览完成；qualification工具155975.9683ms不是页面耗时。

浏览器原JSON SHA `574ba3c30f0358ed176c6c8cac3a19e47f03370a4fc261ac985b18f2a3fdb1e7`，每页默认20条、30个成功样本，150success／0over500，status=passed：

| 页面 | 中位ms | p95 ms | 最大ms | 超500ms |
| --- | ---: | ---: | ---: | ---: |
| 简报 | 416.8 | 456.4 | 456.9 | 0 |
| 资金 | 297.0 | 336.9 | 356.3 | 0 |
| 员工 | 236.6 | 276.1 | 317.6 | 0 |
| 资产 | 256.2 | 277.0 | 277.3 | 0 |
| 报表 | 417.2 | 457.0 | 457.4 | 0 |

旧fe3同主12回执150success／5over500保留，简报／报表中位417.3／417.4ms对本轮416.8／417.2ms基本不变；本组未改报告生产代码，不能把两页最大值下降归因成已证实稳定收益。新native已完成，实际同完整范围的工作量见下表；旧46主48及本轮主48仍性能失败，主120尚未取得资格／浏览器通过。

初控制器额外要求全部demo daemon CPU为0；同一正常后台daemon10秒增加0.375CPU秒而拒绝，status=control_failed，未发start、未开始计时，原py／log／json完整保留。r2按用户约定纠正为计时期间无建库、回归、构造或插桩，原demo与基线相同持续运行且未操作服务；记录quiet interval0.34375CPU秒、timing2.921875CPU秒，不要求所有服务0CPU。r2 status=timing_finished／browser_status=passed；这两份控制器回执不混为一次失败浏览器结果。

新native绑定同1c37源及上述066d资格，status=diagnostic_complete、browser_acceptance=false；与旧fe3 native同完整scope的实际计数如下，箭头为旧→新，未变数值只列一次：

| 路径 | SQL | VM | 返回行 | 返回字节 | 结果装载／解码 | 响应字节 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| context | 4 | 2700 | 24 | 274 | 0／0 | 2965 |
| 简报 | 252→251 | 1196100→1052400 | 18259→18238 | 7840880→7831202 | 1016／1015 | 41887 |
| 资金 | 170 | 1427400 | 11008 | 3618556 | 542／542 | 20532 |
| 员工 | 97 | 598100 | 7180 | 3644895 | 101／101 | 19033 |
| 资产 | 322 | 497300 | 7428 | 3468081 | 53／53 | 12790 |
| 季度报表 | 377 | 993000 | 24218 | 9495754 | 1007／3 | 15542 |

简报VM约下降12%，返回少21头／9678B与重复默认分页头装配退出一致；仍完整装载1016个结果、解码1015个，金额／响应范围保留。context及其他四页全部counters和响应bytes均相同，report本组未改；必要来源读取没有退出，不能称五页工作全面下降。native插桩墙钟不作为浏览器验收，也不把这组计数直接归因成稳定净提速。主48本轮结果另列下节；主120仍只有construction-unverified准备，未提前填资格或性能通过。

## 同源主48实际资格、整页与增长

1c37主48资格JSON SHA `029e0eb04fdf4a51cde2d2f1ce235031a3086f33735eec92d70321d562f54656`，complete、四coverage verified／limitations=[]；121593事实、50519计算、49245凭证、47关账、337证据完整核验，真实开放月预览为2019-12，源码／合同保持原封。资格工具1980830.6014ms是完整准备／注册／预览成本，不是页面时长。

浏览器JSON SHA `d9797e4c310fbf57a45ed4193ea295db115c83d366eda6fb4e0e94cc9f350e19`：五页各30成功样本、共150success，26次超500ms，status=over_target：

| 页面 | 中位ms | p95 ms | 最大ms | 超500ms |
| --- | ---: | ---: | ---: | ---: |
| 简报 | 500.4 | 634.2 | 715.8 | 16 |
| 资金 | 374.9 | 416.9 | 435.6 | 0 |
| 员工 | 314.9 | 494.3 | 494.7 | 0 |
| 资产 | 457.0 | 576.4 | 635.7 | 5 |
| 报表 | 476.6 | 576.5 | 654.9 | 5 |

旧46主48超线17／0／0／2／9，本轮16／0／0／5／5；中位由515.7／375.2／295.9／437.1／496.7变为500.4／374.9／314.9／457.0／476.6ms，部分下降、部分反增，不能只看总28→26称整体稳定收益。简报、资产、报表三页仍未达标。controller计时区间暂停合成copy，copy CPU=0、resume_status=0且恢复活动；原demo继续正常运行、CPU3.953125秒，仅观察未修改，不作停止所有服务的门槛或serverCPU归因。

native48完成，source及上述029e资格SHA均实际绑定，原JSON SHA `03d96f748ad132b363000ae9da14bdccba633dc417cf46962fe3ab47bbde8e02`。同源主12→48完整scope增长如下；箭头是规模增长而不是优化前后：

| 路径 | SQL | VM | 返回字节 | 完整结果装载／解码 |
| --- | ---: | ---: | ---: | --- |
| context | 4→4 | 2700→10900 | 274→850 | 0／0不变 |
| 简报 | 251→283 | 1052400→1136100 | 7831202→10165781 | 1016／1015不变 |
| 资金 | 170→206 | 1427400→1492200 | 3618556→5260705 | 542／542不变 |
| 员工 | 97→98 | 598100→825800 | 3644895→4446706 | 101／101不变 |
| 资产 | 322→825 | 497300→839500 | 3468081→8167183 | 53／53→197／197 |
| 季度报表 | 377→639 | 993000→1022800 | 9495754→10693072 | 1007／3不变，贡献解码1004不变 |

资产装载随必要历史增长，报表完整结果解码3及贡献1004仍在；这些实际工作不能由返回20条或同月ID定位推断消失。插桩时长不作为500ms验收，主120、独立及压力未验仍保留。

## 资产成员目录的实际重叠调查

root实际probe只读执行一次默认主12资产页，未改原函数／证明，source1c37及066d主12资格绑定。`_selected_asset_activation_identities`选择11个activation owners／返回11成员，`_selected_asset_member_heads`选择24 owners（12 activation＋12 consumption）／返回78成员；同snapshot，owner交集11、member交集11，原完整selected目录proof均执行。前者无成功identity key，后者已有两个不同key，不把成功owner-selection缓存等同member目录缓存。

这证明11个activation目录输入重复，未证明可跳过完整proof或净收益；主要折旧／consumption成员仍须查，额外activation和不同证明职责不能仅凭交集删去。当前没有生产修复；fixed-v1、fallback、过滤／分页和主48／120未由该probe覆盖。原静态scope md标题仍为prepared-only，归档保留原文，与实际completed JSON分开，不倒写成执行记录。

## 必要读取与实际重复

| 调用位置与范围 | 实际证据 | 处理边界 |
| --- | --- | --- |
| `dashboard_owner.owner_tasks`，简报默认／部分响应及同一CLI/MCP看板入口 | 原计划已由`fact_period(period=?)`驱动，随后同一subject按五种kind重复seek；不是全历史扫描。 | 保留原joins，仅将kind membership包为`IS TRUE`，subject按id一次定位后判断kind；原来源、摘要、类型、身份、负责人条件及成功缓存发布保留。其他四页不调用该待办查询。 |
| `QueryReads.verify_open_voucher_scope`，本月发表正反向证明 | 当前发表头查询两种规模均85800 VM／1006行／675440B；反向缺失查询均38300 VM／0行。 | 两个方向均必要；0行是完成缺失保护的结果，不能叫0工作。不得先按账户或kind过滤再证明，不退出撤回、源头、封印、采用关系。 |
| `Journal.account_amounts`，完整精确月金额汇总 | 两种规模均114800 VM／1005头／454060B，完整月来源及实际凭证行核验后才发布`journal_verified_rows`。 | 金额汇总需要完整集合；失败不发布证明，分页不能制造证明或代替整月核验。 |
| `Journal.page`，同快照已有上述成功证明的无账户／kind／subject筛选月分页 | 原分页均81100 VM／21行／9678B，精确编号定位均33500 VM／1行／572B；候选仍是本月，但分页／聚焦再次装配头。 | 新路径复用成功精确月头，按(number,id)排序、bisect页定位、版本ID lookup，之后照旧hydrate。尚无新生产VM或整页净耗时结果，不能直接把原SQL成本当实际节省。 |
| `reports._rooted_classification_headers`，开放月份分类引用 | 主12／48原路径20500／22500 VM，411行／53430B不变；强制全部下游CROSS JOIN反增33000／35200 VM。 | 保留原计划。冻结报表此前4932不同引用、交集0的分类头是必要读取，不加推测性缓存。 |
| 其他kind选择：工资保留、补充工资、worklist、关账顺序／readiness、当前与fixed-v1覆盖；按指定kind驱动目录和业务查询 | 候选集及职责不同，本轮未逐项测量。 | 保留原路径，不按SQL相似性批量替换。完整核验、维修、备份恢复和fixed-v1保留独立完整来源职责。 |

## 月驱动单次定位的SQL证据

SQL读取源固定为`fe3a0ba43267bb05f3159bf38aeca13bf90f8cdc8c9b774882a6a560f27bc8f6`。主12本身已在fe3取得资格；主48使用旧46资格回执，在fe3下只核对schema、身份和state再只读探针，不能据此称主48取得fe3完整业务资格。两个探针绑定完整source inventory及实际模块路径，未调用服务、完整核验或预览，未写库。

| 月范围SQL | 主12 VM | 主48 VM | 返回结果 |
| --- | ---: | ---: | --- |
| 原owner membership | 100300 | 101700 | 两者100ID／3200B |
| 强制period-first | 100300 | 101700 | 同完整ID集合与字节 |
| 新boolean single seek | 37700 | 38200 | 同完整ID集合与字节 |
| 强制subject主键single seek | 37700 | 38200 | 同结果，但不依赖SQLite autoindex名 |
| CASE single seek | 42800 | 43400 | 同结果，实际VM较高 |
| kind-first | 12200 | 48200 | 同结果，随历史kind subjects增长 |

主12／48本月事实修订为2514／2550，总修订30225／121593；kind-first候选subjects随历史由1200增至4800，所以不选为默认方案。只实施`(s.kind IN(SELECT value FROM json_each(?))) IS TRUE`，不改索引、schema、查询集合或证明范围。实际VM按SQLite进度采样100步计数；返回tuple摘要、完整ID集合、行与字节分别核对，不能冒充正文JSON解码计数或页面计时。

生产专项实际文件为`.tmp/stage9-owner-kind-seek-tests.log`，不是任务中暂称的`stage9-owner-verified-fact-consumption-kind-seek.log`。命令为`.tmp-kernel-venv/Scripts/python.exe -B -m pytest -q tests/kernel/test_owner_verified_fact_consumption.py --basetemp=.tmp/stage9-owner-kind-seek-tests-pytest`，4passed／57.09s；保留类型化来源消费、有效模型但错误摘要拒绝，并覆盖12／48历史对象增长、16个无关同月profile、同待发工资与完整可见待办等价、行／字节不变和较低VM。Ruff回执为该改动两文件通过，当前draft测试不等于新固定源全套。

## Journal分页复用与静态审查边界

当前`dashboard_reads.py`的新路径仅在精确month、无kinds/accounts/subjects过滤、当前非fixed-v1的自有活动只读快照且已经存在成功`journal_verified_rows`时使用。未拥有连接、非事务／其他快照、无证明、不同范围、fixed-v1及筛选路径回原SQL；不额外预读或新增跨请求缓存。当前schema下闭期月只有原`account_amounts`完成冻结头、采用及实际行核验后才可能取得证明，不能跳过冻结内容验证。

排序键与原SQL`ORDER BY j.number,j.id`相同；普通游标使用严格number>after，读取limit+1保留has_more／next_cursor，精确版本ID优先于编号，精确编号使用同号区间。total／filtered_count及hydrate保持原调用。after_number和voucher_number超SQLite signed64范围、非法类型或limit时回原SQL，保留原绑定错误，不能在Python定位中静默变成空页。旧r31 MATERIALIZED方案不重试。

初版专项`.tmp/stage9-journal-verified-page-directed.log/.xml`为4passed／12.14s；后续审查收窄owned guard并新增int64两个参数场景，初4回执不能验收这些后改内容。静态重点仍是自有同快照／精确月缓存键、先全部证明成功才发布、空集合与游标边界、同号稳定排序、版本ID优先、闭期／固定v1拒绝和未托管分支。收窄后九模块关联组已经完成，`.tmp/stage9-journal-owner-scope-group.log/.xml`记录71passed、4warnings、127.05s，session36006 exit0；warnings均为record_property与xunit2不兼容提示。该结果包含后改guard场景，不与此前4项定向相加，也不等于新固定候选资格或500ms整页通过。本记录只核对完成回执，未运行或重复该组。

## GC诊断保留负结果

fe3主12授权dispatch GC初版guard用闭包保留Store的bound method，增加仪器自身的Store／闭包环；first脚本与JSON属于污染证据，不作为生产对象滞留或泄漏依据。r2改class method，不捕获实例。两轮完整原件分别保留，不以修正版覆盖初轮。

r2保持GC启用、thresholds700／10／10，简报和报表各3预热＋10记录请求。简报每请求累计GC6.743–10.713ms、773事件、单次最大2.9527ms；报表累计13.836–25.468ms、1177事件、单次最大13.4366ms；两者uncollectable=0。多次gen2 collect0是扫描live／transient分配，不证明闲置对象滞留。QueryReads finally及resident pool清缓存、token／authorizer／callback并回滚，静态未见设计上的跨请求业务缓存残留。

仪器自身仍增加分配与活对象，r2记录墙钟不是纯页面耗时；它没有复现或解释旧fe3浏览器简报695ms、报表557ms主请求异常。没有生产GC变更、collect／disable／freeze／阈值调整或通用缓存方案。新1c37本轮主12纯浏览器通过，仅证明这轮满足500ms目标，不证明GC根因或稳定净提速。

## 独立原件归档

13份明确合成原件保存于[owner-month-selection-group-20261003-manifest.json](owner-month-selection-group-20261003-manifest.json)，SHA256 `d688e893622afc863678f1882f5d2558cb753a58ebe7e4626b519cb2db682969`：GC first及r2各py／json，加scope md共5份；两个SQL探针各py／json／log共6份，加SQL审查md和4pass log共8份。各gzip独占创建、mtime=0，逐件原字节解压一致并检查源未变；未收DB、凭据、原checkpoint或真实资料，不覆盖此前任何清单。初版污染、修正版、固定fe3探针、旧46主48资格及当前draft测试分别标scope。

九模块71项完成回执另2份独立归档于[owner-month-selection-group-tests-20261003-manifest.json](owner-month-selection-group-tests-20261003-manifest.json)，SHA256 `4bd83183077d59dffd121af412871fb6cf4597d5653fc90103c58e2f389ce9ca`；新gzip同样独占、mtime=0并逐件核对raw bytes往返，旧13件清单保持原样。

新1c37 source metadata、主12资格／浏览器及初控制器失败／r2共12份已完成原件独立归档于[owner-month-reads-main12-20261003-manifest.json](owner-month-reads-main12-20261003-manifest.json)，SHA256 `6af4b1884181e22286289f9d7d7c984825da7aae27f29ae858232366b59628cb`；逐件gzip独占、mtime=0、原字节往返一致，不改旧清单，不含未结束native、DB、凭据或原checkpoint。

随后完成的1c37 native脚本／JSON／log另3份独立归档于[owner-month-reads-native-main12-20261003-manifest.json](owner-month-reads-native-main12-20261003-manifest.json)，SHA256 `0c5572e0e902d2414f527cec2e8a182219ec56a7a345329b1d9d175a3835c9f0`；绑定source及资格SHA，同样独占gzip、mtime=0、原字节往返一致，未覆盖上述12件清单。

主48资格／浏览器／controller／start／ready共8件，加root主12资产probe脚本／JSON／log／stdout／静态scope共5件，独立13件清单为[owner-month-reads-main48-20261003-manifest.json](owner-month-reads-main48-20261003-manifest.json)，SHA `2ce8b3856e5e8606ee3fdc709ac11815333725d4f318d8812889edd1e7f9397d`。其后完成native48另3件独立于[owner-month-reads-native-main48-20261003-manifest.json](owner-month-reads-native-main48-20261003-manifest.json)，SHA `70376b7be582ab23a6c1763eca48ec338e47e17770747a7270cde8f5563a587c`；两清单均gzip独占、mtime=0、raw bytes往返一致，不覆盖已落清单，不含DB／凭据／checkpoint。

本轮只读核对源码／既有回执、写本新文档及压缩原件，未运行测试、数据库、服务、CPU探针或浏览器；未改其他文档、生产代码、封存源或AGENTS。完整核验／关账／维修／备份恢复／fixed-v1以及独立／压力规模保留未验证范围，不能由这些窄定向或仪器探针推导全部验收。
