# Money／identity公共读取组（2026-10-03，收敛中）

本组尚未完成统一固源回归或当前性能验收。用户本轮明确只要开发库：primary保持draft／0，现有5173、资料根、身份原样；正式合同冻结、正式包交付和运行切换延期。6688主48fresh资格与150success／9超线仅属修改前候选，不能转移成当前新reader通过。本文集中记录已实施、静态边界、负面原型及分轮测试，不相加通过数。

## 问题与职责边界

| 类型 | 已证实范围／当前处理 | 保留与未验 |
| --- | --- | --- |
| 精确open publication signature重复构造 | 公共_query_reads relationship helper冷row入口已构造六字段signature，成功owned cache producer曾再次构造相同tuple；现保留该次局部signature，后续输入cache命中仍精确比对。 | 原reverse／forward关系、source/record/current身份及失败原子性不裁，unowned／v1仍原路。五页共享caller不等于五页性能改善；当前整组和净收益未验。 |
| 资产消费头带入启用历史目录 | 6688单次主48证明第二identity查询48activation＋48consumption owners、1176目录行，page20 emit20activation未被_assets消费。已仅在_selection非None的current-owned路径，按owner kind消费过滤局部events；cached selection不改，消费全部历史不裁。 | activation专用链继续全卡身份／损坏检查；None／unowned／v1原完整fallback。首20 latest同一owner不证明历史可裁，1128消费目录仍必要选择证据，latest替代未验证。 |
| 简报保存结果行与金额证明 | verify_selected_content只证明正文／摘要，不提供严格_lines；result_lines2010含memo hit，真正_lines1006。 | 首次全行严格类型、完整实际凭证、封签／采用／冲正、monthly_account借贷两列、页外未知及失败无journal前缀缓存保留。不得把源JSON proof偷换为金额proof。 |
| 窄own-line reader（已撤回） | r2／r3增加工作量；r4／r5减少解码与仪器峰值，但r5交替native对照没有净收益，终止此方向。dashboard.py、dashboard_reads.py、dashboard_funds.py、report_open_contribution.py已恢复精确6688原字节。 | 新18项prototype-only测试连同候选源码保存在独立.tmp目录后退出生产测试；不是删除原有业务断言。signature、资产消费过滤和开发package路径保留，当前整组与新性能仍待收敛。 |

固定v1、独立完整核验／关账、repair／backup／restore保留原证明与历史解码，不消费另一owned读事务的cache；core完整资产历史保持activation能力。CLI／MCP／HTTP由公共服务路由，未逐命令新验收，不加跨请求缓存、权威或DDL。

## 已执行原型：负面工作量与r5无净收益撤回

两个原型均在6688及已qualified主48合成样本上对照，完整响应除generation_time相等，state／身份前后一致；instrumented不作500ms／服务器CPU或稳定净延迟证据。

| 方案／原件prefix | SQL | VM | 返回行 | 返回bytes | 完整结果load／decode；贡献decode |
| --- | ---: | ---: | ---: | ---: | --- |
| 原6688简报 | 233 | 1136300 | 19904 | 10165621 | 1016／1015；0 |
| 全贡献r2 stage9-owner-month-anchored-lines-prototype-main48-r2 | 248 | 1592200 | 25522 | 14501254 | 1537／534；1003 |
| 不含四资金科目但仍完整report reader的r3 stage9-owner-month-anchored-lines-prototype-main48-r3 | 241 | 1481700 | 23004 | 11953279 | 1033／533；499 |
| 窄own-line reader r4 stage9-owner-month-anchored-lines-prototype-main48-r4 | 240 | 1480800 | 22324 | 11937428 | 1033／533；0（独立own-line读取不使用完整贡献decoder） |

r2／r3虽减少result JSON解码，却增加SQL／VM／正文返回，不能把decoder减少称整体省工或采用整份report reader。r4保留anchor／own-source bytes／whole-line原证明，完整响应仅排除data.generated_at后相等；其仪器峰值25741920→22140849B，instrumented6147.1518→6035.8811ms不作纯收益或浏览器通过。r5修窄classification读取后，完整响应仍相等，VM1136300→1156100、bytes10165621→11756554，result JSON解码1015→533、仪器峰值25743997→22019957B。15次交替无插桩backend对照old median／max483.7177／547.7141ms、new485.5747／557.0316ms，未见净收益，全部slow／raw样本保留；这也不是HTTP／浏览器500ms验收。root已用stage9-withdraw-own-lines-candidate.py撤回四个候选生产文件，新prototype-only测试保存在stage9-own-line-rejected-production-source-20261003。初原型完整响应比较失败（ValueError: Complete brief response changed）与r2至r5原件均保留；结论仅限定此方向，不永久否定未来完整verify内部局部复用。

## 已结束定向与当前失败

| 分组／原件 | 实际结果与范围 |
| --- | --- |
| signature stage9-money-signature-boundary-tests.log | test_open_publication_snapshot_work19passed／12.27s；冷每row一次signature、cache hit仍比对、末行缺字段／同ID不同voucher两次拒绝、无relationship/publication前缀及既有review/replace/v1/unowned范围。只此定向，未当前整组性能。 |
| 资产过滤初组 stage9-asset-consumption-scope-directed.log/.xml | 三文件16passed／2failed／97.27s。新增4case全部通过：24独立启用owners＋2消费月default／next／焦点完整响应等价、完整count、消费历史保留、rows／bytes下降且结果load不变；页外activation／consumption兄弟损坏retry无新prefix；None原参数。失败是私有消费头旧预期仍含首月未消费的fixed卡和仅activation冻结样本。 |
| 资产两node修正 stage9-asset-consumption-scope-correction.log/.xml | 2passed／6.78s，只修私有消费预期；core完整activation／consumption、IDs与采用月份断言保留，不称18项整组重跑。Ruff/diffcheck通过。 |
| 新anchored line首组 stage9-owner-month-anchored-lines-tests-first.log/.xml | 8passed／9failed／2warnings／42.73s，当前开发reader中途结果，原失败保留，不宣布业务完整性已收敛。 |
| line fix1 stage9-owner-month-anchored-lines-tests-fix1.log/.xml | 6passed／3failed／8deselected／30.20s，未完整重跑，root继续修正；当前最终测试／纯收益／浏览器未验。 |
| line＋开发package stage9-owner-lines-and-development-package-directed.log/.xml | 39passed／1failed／2warnings／90.96s。唯一失败是新增prototype混合fixture调用不存在的bank_funding；修正fixture后未重跑即撤回prototype，不能造修后全通过。原package既有通过记录不与本组相加。 |
| signature／assets／开发package stage9-signature-assets-development-package-group-corrected.log/.xml | 六文件114node，108passed／6failed／213.89s。六失败均在test_owner_asset_metadata_scope私有消费头与含activation的core完整事件比较，zero-owner失败发生在删除源前；closed_correction公共冻结items相等先通过。仅修私有测试口径，仍先完整core核验再投影消费事件；core完整身份与unowned／v1完整fallback断言保留。原失败回执不改，该组误含metadata文件，不是原计划三asset文件组。 |
| 资产metadata＋activation stage9-asset-metadata-consumption-correction.log/.xml | 后续两文件69passed／189.32s，覆盖已修私有helper及原计划test_owner_asset_activation_identity；不是114项完整重跑，不累计重复通过数。 |

## Signature／资产过滤的主48诊断

stage9-signature-assets-main48-diagnostic-r3.json为diagnostic_complete，六入口完整响应除generated_at／报表checked_at时钟字段外一致，state／身份前后一致；未新建或复用为当前资格，browser_acceptance=false。r1在context、r2在quarterly_report误比较时钟字段而失败，原.py/.json/.log保留；r3明确绑定r2报告SHA2959e0fe4249cb0158e8186b53e11b91f2a6caa06c1744bb3dd060d08a18c1a7并复用前五入口工作量，只补报表工作量与六入口native，不能称全部工作量重新测量。

| 入口 | baseline native median／max ms | current native median／max ms |
| --- | ---: | ---: |
| context | 11.2618／12.9425 | 11.0456／13.2356 |
| brief | 473.8998／484.6076 | 467.5541／489.4135 |
| funds | 334.5375／377.9584 | 297.6785／400.6494 |
| employees | 209.2216／241.8650 | 250.1837／304.7898 |
| assets | 430.6519／464.0349 | 390.1212／450.5887 |
| quarterly_report | 466.0448／554.9819 | 486.6272／502.8945 |

每入口各10次原生读取对照；员工与报表median变慢，资金及简报max上升，不能称整体稳定收益或500ms通过。这些不是HTTP／ResourceTiming／纯浏览器验收，不把仪器墙钟当页面时间。

| 入口 | SQL before→after | VM before→after | 返回rows／bytes before→after | 必要结果load／JSON decode／贡献decode（均保留） |
| --- | ---: | ---: | --- | --- |
| context | 4→4 | 10900→10900 | 96／850不变 | 0／0／0 |
| brief | 233→233 | 1136300→1136700 | 19904／10165621不变 | 1016／1015／0；outcome994988B |
| funds | 158→158 | 1492300→1490800 | 11458／5260673不变 | 542／542／0；outcome387755B |
| employees | 98→98 | 825900→827600 | 8431／4446706不变 | 101／101／0；outcome554008B |
| assets | 268→268 | 840300→834100 | 12565→12517／8164623→8123871 | 197／197／0；outcome211328B |
| quarterly_report | 270→270 | 1021200→1023500 | 24896／10688456不变 | 1007／3／1004；outcome991627B，贡献解码输入3059132B |

资产减少48返回行／40752B及48次stdlib JSON解析，activation专用身份链、197项完整结果解码和94次accounting slice读取仍保留；不是删历史余额／采用证明。其它五入口没有减少必要结果装载；typed fact decode按context／brief／funds／employees／assets／report为0／121／35／0／40／417，全部不变，brief／funds的accounting slice50／49亦不变。请求lifecycle静态审查未发现新的持久拥有边，现有release／reset仍在；未动态观察的reports／pool／异常／v1场景保持未验证，不实施GC／weakref方案。draft fixture工具已补WAL guard及真实凭证／冻结月保全，后续两文件32passed／21.25s、Ruff通过；首轮29passed／19.94s独立保留。工具低层测试不是大规模CLI复制／实际业务资格；大CLI、内容核验及浏览器未验。新封draft源d198、737文件；主48隔离复制尚运行，内容未核验。

## 原件保全

draft fixture工具及测试原文、首轮29pass与后续32pass日志另4件独占归档于[工具窄测清单](owner-draft-fixture-tools-20261003-manifest.json)，SHAf092569eaeab3c6e4f7df04643d85dd753afc13f040836ac2ac6e97672e52760；gzip目录owner-draft-fixture-tools-20261003-raw（本页同目录，原.tmp保留），mtime=0、原字节往返与SHA核对，未覆盖前一清单。固定draft source SHAd198eab959be1d3a1237744778b3329bbe4f01379d7917541bc4e017f31cf045，737文件；封源不等于样本已核验，主48新draft复制尚运行，不提前记录复制／资格／浏览器通过。

53件明确合成原件已新独占gzip归档：r1至r5原型与失败／slow数组、signature／assets r1/r2/r3及准备脚本、分轮测试回执、lifecycle静态范围、撤回helper及四候选源码／新增prototype-only测试快照。见[本组新清单](owner-money-identity-group-final-20261003-manifest.json)，SHA361070f60c6a4d073636532d1605bdeadaafe6545f1a0f47bfc22e732e59b876，gzip目录owner-money-identity-group-final-20261003-raw（本页同目录，原.tmp保留）；mtime=0，逐件SHA与原字节往返一致，原件／旧manifest未改，不含DB／凭据／checkpoint。

源6688 SHA6688df6c46f5416ca03917def0d29a701cb20e8ae45b962c59df4a1290f8da62，fresh主48资格SHA2ac7c29d02e920826d27bae8fdb2ff4deb740fa184e3a502aaa9e09521e804a3仅为诊断样本输入；新production与当前tests不是该快照，不转移资格coverage。不操作真实DB／服务，本次文档与压缩未执行测试／测量。

## 下一步与最小保护

r5无净收益后不继续该own-line方向，保留原金额／正文／whole-voucher证明。signature、资产消费过滤和开发package保留；114组原失败与后续两文件69通过分开，fixture工具窄测32通过，但当前draft源业务资格／整组／纯浏览器未验。当前12／48热刷新不能继承6688候选。保持46项完成／保留／排除／延期／未验状态，正式合同／包／切版本轮延期。

静态及调查来源：.tmp/stage9-next-read-group-scope.md、stage9-money-summary-boundary-test-scope.md、stage9-result-line-validation-group-scope.md、stage9-asset-owner-attribution-main48-6688.md；这些说明不替代动态验证，旧prototype/latest未验不当排除。此文只读原件并写记录，没有执行测试／DB／测量，未改生产。
