# 月份发现组（2026-10-03）

本组固定源为draft／0，SHA22ef17f6aa6b40025eaf9fe939a209f15f7d4c73605e982925859fe42a1cfff3，740文件；公司／目录SQL合同为c9／b484。定位helper与三个caller已收敛，集中8文件51passed；主12／48fresh资格完成，但纯浏览器分别1／8超线，总体均over_target。季度report原生前后净延迟尚未测，不从work-only或跨源旧对照推净收益。正式版延期，现有5173、真实资料及身份原样。

## 根因、范围与修复

余额与清偿的普通tail／summary为发现实际posting月份，原三表UNION会枚举同月大量重复业务行；后续仍需按每个真实月份核验余额、清偿投影和封签。新增posting_period_reads.posting_periods只负责定位：固定五表可信名单，利用现有posting_period前导索引递归min寻找下一月，再合并各表月份。每表独立发现，孤立projection／seal仍入候选；没有新DDL、缓存或成功证明。

| 实际接通位置 | 调用与保留边界 |
| --- | --- |
| period_balances._totals | Dashboard._brief_amounts→balance_totals／balance_movements；normal tail改定位，verify_balance_periods、冻结余额、金额与来源核验保留。 |
| settlement_freeze._tail_rows | _scope→_tail_rows；保留late reviews、严格minimum下界／maximum上界及verify_settlement_periods。定位不代表完成清偿核验。 |
| settlement_projection._summary_scope | settlement_position_rows／settlement_summary／settlement_followup_summary；current保持无上限，historical保留through，subject筛选不裁独立月份coverage。 |

五页及CLI／MCP经这些公共读取时消费同一定位逻辑，未逐传输或按需入口新验收。静态同类排查共10处：写入同步的bounded subject枚举、显式期间与full-integrity混合选择、Engine发布检查、BusinessQueries._selected_accounting精确subject采用月份、report完整compare、Worklist候选身份发现继续原职责。固定report_projection_v1未改；没有因为代码相似替换其它SQL。详见归档scope的精确函数／行／caller表；未改位置的性能尚未测，不能称已排除优化。

空集合、after严格下界、through包含上界和current无上限保持原含义。helper返回月份集合不是完整性proof，正常caller仍执行原核验；坏封签／失效来源不得当空月跳过，冻结历史、真实金额、晚复核及失败拒绝保护不减。

## 分轮测试与定位工作量

| 原件 | 实际结果与接受范围 |
| --- | --- |
| stage9-posting-period-discovery-directed-first.log | 初定向8passed／18.42s；新增helper范围与原caller业务保护。 |
| stage9-posting-period-discovery-loaded-guard.log | 追加装载护栏后仅复验1case，1passed／4.17s，不写为新版8项全量重跑。 |
| stage9-posting-period-discovery-group.log／.xml | 集中8文件51passed／113.04s：test_posting_period_reads、test_period_balance_freeze、test_period_balance_index_scope、test_settlement_freeze、test_settlement_freeze_v1、test_settlement_late_reviews、test_settlement_period_scopes、test_settlement_candidate_selection。XML为实际名单，静态scope推荐列表不是执行记录；分轮不相加。 |

定位专用合成case：12／48／120月、每月1000重复行，seek VM2340／8820／21780，对照UNION368639／1486475／3722147；装载月份行60／240／600，同月重复行从1到1000不增长。这是小case定位工作量，不是主规模native、服务器CPU或500ms页面收益；源正文与必要业务proof装载本身不减少。

## 主48实际work-only对照

stage9-draft-period-main48-work-first.json／.log已实际完成exit0。以同一draft主48构造输入a73ded22…，在22ef模块／factory下仅三处函数对照d198，六入口完整响应除自然generated_at／报表checked_at时钟一致；source、身份／epochs／checkpoint前后保护保留。current_source_full_verification=false，native_samples_ms={}，没有当前资格证明或native／浏览器时测，不作500ms通过。

| 入口 | SQL before→after | VM before→after | 返回rows／bytes before→after | 必要结果load／JSON decode／贡献decode（均不变） |
| --- | ---: | ---: | --- | --- |
| context | 4→4 | 10900→10900 | 96／850不变 | 0／0／0 |
| brief | 233→241 | 1136300→1061100 | 19904→19910／10165621→10165669 | 1016／1015／0 |
| funds | 158→162 | 1491700→1437200 | 11458→11462／5260673→5260705 | 542／542／0 |
| employees | 98→100 | 825900→805500 | 8431→8433／4446706→4446722 | 101／101／0 |
| assets | 268→272 | 834900→785200 | 12517→12521／8123871→8123903 | 197／197／0 |
| quarterly_report | 270→270 | 1022000→1023400 | 24896／10688456不变 | 1007／3／1004 |

前四页减少的是月份定位扫描，SQL数反而略增，返回量也有少量月份行增长；金额／完整响应、结果装载及解码不减。typed fact decode按六入口0／121／35／0／40／417全部保留。context计数不变；报表VM略增，其SQL、行／bytes、完整结果装载及贡献解码全不变，不称报表收益。仪器墙钟不作净延迟证据，亦不替代正在单独调度的资格／纯浏览器验收。

## Draft复制、固定源与尚未验证

主12／48隔离draft复制已完成，247表的原逐表attestation仅schema_meta／schema_history不同，其余逐表digest相同；构造身份、业务、凭证及冻结历史保留，旧released资格不继承。复制目标采用d198 draft源；后续22ef仅是新读取候选，不能混为复制时源码。

| 合成目标／copy-book SHA | 原件counts（fact／calc／voucher／close／evidence） | 当前证明 |
| --- | --- | --- |
| stage9-draft-fixture-main48-20261003／a73ded22688073e7aecb7a79d56a6e31d9d6be202e1083738b091d42d3671d93 | 121593／50519／49245／47／337 | copied_content_unverified；复制保全，不是内容资格。 |
| stage9-draft-fixture-main12-20261003／f9526f9cbaa283125c39353b0106abb4beb38db6fc91adf35ca9788f1513627f | 30225／12413／12309／11／85 | copied_content_unverified；复制保全，不是内容资格。 |

原copy-book仍保留copied_content_unverified输入状态；随后独立fresh资格已完成，不改写旧构造报告：main12 qualified-r1四coverage verified／limitations=[]，169696.69ms；main48 qualified-r2同样四verified／[]，1938124.15ms。核验counts与上表实际复制counts一致，资格耗时不是页面耗时。首启动嵌套book-report路径安全guard拒绝exit2，无DB或资格动作；原失败保留，平铺原字节输入后完成r2。

## 两规模纯浏览器：均未达标

两轮均5页各30次，150success／0error，warmups3；主12 over_target共1超，主48 over_target共8超。以下只列本组实际JSON，不复制历史统计。

| 页面 | main12 median／p95／max ms；超500 | main48 median／p95／max ms；超500 |
| --- | --- | --- |
| brief | 416.4／476.4／495.0；0 | 476.5／517.0／517.2；5 |
| funds | 297.0／317.1／336.8；0 | 356.3／395.6／396.8；0 |
| employees | 236.4／256.5／256.9；0 | 296.4／355.4／375.8；0 |
| assets | 237.0／295.8／355.3；0 | 436.0／476.8／596.5；1 |
| reports | 436.2／495.7／536.7；1 | 476.4／512.1／515.4；2 |

stage9-main12-draft-pure-control-r1.json与main48对应r2均timing_finished：owned targets暂停期间kernel／user CPU原值不变，逐项cpu_unchanged=true、resume_status=0。资格／copy工作在纯计时窗口暂停，不能将整轮其它阶段并行误写成计时并行。控制成功独立于性能失败；旧1c37／6688的source／format／root条件不同，不是受控AB净收益。cold仅单次、switch为single_eligible_company不可用，导航尚未完整验收。

保存ResourceTiming的主12报表唯一超线样本，较长部分位于page等待responseStart，render尾没有增长；context与page请求并行。等待包含HTTP排队／调度／service／security／catalog／engine／读取／响应等，不能作server CPU或某函数根因。r1归因JSON缺source元数据、r2仅补来源，均保留；当前季度cProfile脚本只准备，未执行，不把准备记录当原生测量。

## 开发package工具缺陷与未验证

22ef package的mandatory relocated selfcheck实际失败：verify_local_package.assert_owner_read对dashboard_period_preparation误访问selected_period.key，而真实schema4返回period: Month。这是工具响应形状断言错误，不是业务缺项；原失败包／stdout／stderr保留，不能靠外置补检写成功。修正工具48项定向通过／34.19s、Ruff通过，不是实际包通过。

工具fix1固定源SHA97b9c0041b481b5eb53f6036567d46865c063295e62ff460458eb72014a9368f，741文件；source receipt确认全部src／合同／模板／前端及业务build与22ef逐字节相同，仅工具／对应测试差异。fix1完整mandatory包自检尚待真正成功，不改写原失败。main120复制session97740仍运行，其它规模、导航／按需、包／实际AI及两档evidence未验；不反复扩写运行中过程。

## 原始记录保全

18件明确已完成合成原件新独占归档：[manifest](owner-posting-period-discovery-group-20261003-manifest.json)，SHA44ff832aca250cf93215cd14818147f8150778606af19f50635ece0ffe0d1c0c；gzip在本目录owner-posting-period-discovery-group-20261003-raw／，mtime=0，逐件原字节／SHA往返一致。包含分轮log与实际group XML、最终scope、两份copy-book及attestation／完成日志／平铺输入、初路径失败、固定源manifest及窄helper／测试原文。没有DB、凭据、完整checkpoint或运行中资格／浏览器／main120日志；原件和旧manifest不改。本次仅文档与压缩，未运行测试、DB或测量。

本次work-only另5件独占保全：[小manifest](owner-posting-period-main48-work-20261003-manifest.json)，SHA2c027c27b8168f4bbae420c37748fa99b6170d5ddb5c4f315ada175eab1e31d5，gzip在同目录owner-posting-period-main48-work-20261003-raw／。保存helper最终原字节（SHA与实际执行JSON记录相同）、准备plan、work JSON／log及贡献读取静态scope；后者明确只取旧r3证据，不当本组新测量。mtime=0，原字节／SHA往返一致；旧18件manifest与全部原件不改，不重复归档DB／checkpoint或已存原件。

两规模本轮另20件：[浏览器原件manifest](owner-posting-period-two-scale-browser-20261003-manifest.json)，SHAfacb63cffaa351e1652d3d6f9d4f359bc33d309fdf707232e6da01477bf2ffdd，gzip在同目录owner-posting-period-two-scale-browser-20261003-raw／。仅已完成qual／browser／控制script-JSON-log／ready-start，以及当前ResourceTiming归因记录和未执行cProfile脚本；mtime=0、SHA和原字节往返一致，无DB／checkpoint、运行中包日志或重复旧归档。本次更新后等待新证据，未执行新测试／DB／测量。
