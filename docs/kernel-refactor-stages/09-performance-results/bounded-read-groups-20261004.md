# 第9阶段：精确head lookup与报表classification聚合（2026-10-04）

本轮固定候选源为 `.tmp/stage9-build-source-bounded-read-groups-20261004`，source SHA `2e474b71d7871f27a8146286508756c033e823017db02a1bf8ab8fc69e986c91`，manifest SHA `48437917589a2b7453cc5b644a8bc8c9c2444d318774e9cefd62350ee84aa274`，762文件。相对第一组69838，仅生产`dashboard_reads.py`与`report_flow.py`变化，已有header aggregate测试调整插桩以识别新query，新增head scope／report classification两个tests，另757文件相同。fixed-v1、frontend、static与schema字节相同；旧业务cases与60N VM门槛保留。源码差异回执为 `.tmp/stage9-bounded-read-groups-source-diff-20261004.json`。

第二组精确head lookup与第三组report classification聚合已收敛，当前pure顺序窗口完成，五页strict业务SHA与69838一致；保留流式report candidate，依据是严格decision等价、实际返回工作量与有限顺序观察。该观察不证明稳定因果39ms收益或浏览器500ms通过，也不称阶段最终源码或交付。第一组69838的166测试、pure与归档保持[原固定来源](asset-owner-frozen-scope-20261004.md)，不改标为2e474。目录与公司仍draft／0、现有5173、资料根及身份原样，正式冻结、交付和切版延期。

## 第二组：精确直接采用节点

`_verify_head_close_locators`在owned current snapshot且head ID非空str、posting_period严格int、`0 <= posting_period <= snapshot.month`时，只读取 `(head ID, posting_period)`直接采用节点。原双ID候选发现及mutable缺源／redirect lane不变；selected type／position／related／source／path见证、无效输入／unowned／fixed-v1 fallback与完整verifier跨月multiset职责保留。专项31 PASS及同node强化growth、22项员工损坏边界、2项非owned／fixed-v1桥接与首次fixture失败见[head locator组](head-close-locator-scope-20261004.md)。

root集中head消费者6文件123 PASS／263.36秒，日志／XML为 `.tmp/stage9-head-locator-consumers-20261004.log`与同名`.xml`。该组部分重叠第一组166及31专项，不相加成独立总数；head query固定，但同process的report为初版，123结果不转移成最终report candidate验收。

## 第三组：必要classification header在SQL内核验

报表12次classification header读取共4,932个ID且全部不同，核验是必要工作；本组减少逐行送入Python的header结果，没有删分类来源、跳过核验或缩report范围。SQL用流式CASE decision聚合，requested和voucher relation只物化一次，仍按原错误优先级处理source-null／missing／digest／order／scope；非writer ID／period类型保留原行比较fallback，fixed-v1代码未改。Outcome正文、冻结root和完整verifier职责不变。

第一版materialized candidate为429,136 VM，超过保留的`4,932 × 60 = 295,920`门槛，且numeric ID／string period与SQLite affinity有语义差异，已撤去；原插桩未识别新query及Ruff B905失败一并保留。最终流式candidate合成范围为251,568 VM，相比原187,437仍增加约34%，但返回降为1行／16字节，原为4,932行／379,764字节；12k无关对象及120月增长不增加其VM。header aggregate测试仅适配query识别，没有降低旧cases或VM门槛。

agent最终focused47 PASS／7.85秒与Ruff只绑定最终candidate的两个classification测试文件；首版51 PASS／63.13秒仍只绑定旧candidate。最终封存源的report消费者30 PASS／61.56秒，日志／XML为 `.tmp/stage9-bounded-report-consumers-20261004-fix1.log`与同名`.xml`。root首次误用不存在的`test_report_party_summary_reads.py`，command exit1／no tests ran／0.20秒，原log／XML保留；修正为实际`test_report_party_summary.py`后才得到30 PASS，不隐藏首次路径错误。详细候选结果为 `.tmp/stage9-report-classification-scope-result-20261004.json`，该文件对69838的fixture读取范围与对最终candidate的synthetic work分别记录，不改标为2e474整页pure。

## 2e474五页profile诊断

新profile `.tmp/stage9-bounded-read-group-profile-20261004.json`五页strict business SHA逐页与69838相同，before／after guards、source inventory与read pool关闭为true。这里读取已保存诊断回执，不新增实际库或fileguard扫描。

| 页面 | 相对69838的实际work变化 | 保留工作 |
| --- | --- | --- |
| assets | 采样VM -33,600；SQL／行／字节及其他counter不变 | 485 result rows／527,255字节／485 decoded与普通JSON不变。 |
| employees | 采样VM +600；其余counter不变 | 读取范围、正文与公开响应一致。 |
| brief、funds | 全部counter不变 | 原必要读取范围保留。 |
| reports | SQL仍255；返回行-4,920（classification 4,932→12），返回字节-783,996；采样VM +64,300 | outcome仍1,007行／1,058,063字节；普通JSON仍513次／4,342,872输入字节。 |

instrumented毫秒不证明稳定因果或pure性能；减少返回字节不等于减少磁盘IO，VM增加与行传输下降分别保留。以下pure与profile分开执行，保留各自来源与范围。

## 69838／2e474 pure顺序窗口

同一隔离主120来源的69838 baseline与2e474 current，每入口各3 warmups＋30成功样本，原JSON为 `.tmp/stage9-bounded-read-native-baseline-20261004.json`与current同名文件。两次before／after guards、source inventory与read pool关闭为true；company／catalog及非manifest files跨source相同，manifest分别绑定自身源码，不要求两份manifest路径或hash相同。五页strict业务SHA与profile及原69838一致，完整warmups／samples与慢样本均保存。

| 入口 | 69838 median／p95／max ms | 2e474 median／p95／max ms |
| --- | --- | --- |
| assets | 465.5659／502.2612／502.4402 | 450.7420／506.8031／509.7234 |
| brief | 336.60005／376.9850／415.1938 | 337.5314／360.2181／363.2969 |
| reports | 435.59755／556.1759／611.5078 | 396.2116／470.7567／567.5849 |
| funds | 254.0371／312.1755／336.8240 | 235.9455／291.6073／296.8922 |
| employees | 264.4720／294.5259／298.7232 | 259.40445／301.6898／305.0836 |

report median与返回行下降支持保留严格等价候选，但有限顺序窗口不证明稳定因果改善。brief／funds／employees未因本组修改业务读取，其延迟波动不归因本次修复；员工+600采样VM如实保留。资产尾部仍超过500ms，report max567.5849ms；这些没有HTTP／render／prepared owner TODO的native结果不能当成browser门槛通过，也不关闭主120原失败。

## 证据准备与剩余边界

[本组18件独立证据清单](bounded-read-groups-20261004-evidence/manifest.json) SHA `531909ec56a09ae844adffcbc0c1342dcc0add30e2bc464257d28fecc79d4bf6`，包括source-diff、head／最终report日志与XML、首次路径失败log／XML、head31与同growth node复验XML、profile诊断／日志、report候选结果、主120精确lookup诊断及pure两次全部样本／日志。含根路径或guard内容的JSON明确标redacted analysis，guard／read_context内容仅保存SHA，完整warmups／samples及work counters保留；私有原件path／SHA／长度与公开representation SHA／长度分别绑定，不冒充raw。summary分别记录pytest与XML时间、重叠范围、first path failure、五页counter差值与各项业务守卫结论。gzip mtime=0，压缩SHA及解压回环均核对相同。未复制数据库、ZIP、脚本、凭据或完整source inventory，不更改第一组归档。

2e474只是这两组已测固定来源，历史profile／pure与18件归档不覆盖。后续c3c3已修private helper分类事实证明缺口，保留公开assets原有拒绝边界，新增报表同月聚合与目录缓存命中工作，见[本轮记录](owner-proof-read-work-20261004.md)。新来源工作量／原生窗口、开发包及限定MCP完成，随后c3c3主120新鲜资格／实际预览完成且真实热刷新FAIL19／150，见[实际记录](owner-proof-main120-browser-c3c3-20261004.md)；不能继承本组通过数或旧资格。原主120FAIL36／150未关闭，阶段9仍实施中，正式发布延期。
