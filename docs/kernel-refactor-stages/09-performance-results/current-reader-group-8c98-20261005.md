# 当前读取组：私有overlay与往来checkpoint基准（8c98，2026-10-05）

本组完成两处current读取修改、定向36项验证和主120五页正常池工作量对照。宽24模块首轮实际为336通过、2失败，共338项，退出1；两失败是新增测试的预期错误，修正仅涉及测试，关联36项随后全部通过。两组重叠，不相加，不宣称一次338项全通过。新8c98纯浏览器计时尚未开始；旧525f主450的3次超线仍保留，当前严格500ms未通过。

固定源码目录为私有`stage9-build-source-reader-group-converged-20261005`，777文件，inventory SHA `8c98a8e870f6be0dde8fdfc9590e4145a0e1181c4ffc8c03fc0faacb3da58e38`，manifest SHA `f52f6138b2f2e83d2dae3b57bc5a5fcc9cf09f6de5bbf4609fc140ae71b67146`。与old525相比恰好两src（`close_storage`、`report_projection`）及两关联test改变；API、DDL、前端、33个v1文件及4个合同字节保持。snapshot工具首个`-I`直执行缺少`stage9_source`，发生在目标创建前；校准受信scripts路径和runpy后实际exit0，成功快照日志为唯一完成证据。这是一次工具调用错误，没有产品修复。

| 当前调用／问题 | 修改与守卫 | 实际依据 |
| --- | --- | --- |
| 冻结accounting读取的私有写入overlay | 仅展平标准ChainMap为原优先序的叶map，保留空staging map；非标准mapping保留原语义。新写入仍只进fresh，失败不污染共享缓存 | 单元断言实际查找次数减少，加入1024个无关缓存项不增长；业务全等、作用域及失败原子性保留 |
| current报表跨cutoff／source共用已认证checkpoint | 在同一owned读取快照、相同connection内，按精确checkpoint期间保存已经认证的基准；各次运算复制dict后再应用后续delta。完整结果仍按cutoff／source独立，未知／失败保持详细回退 | 单元断言同基准只遍历一次、基准增长全部义务保留、失败后不污染；无owned快照或另一个快照不复用 |
| 资产完整owner与adopted-only | 两组119个slice具有不同subjects／凭证保护，保留两种读取语义 | 旧单次行跨度owner113.91ms、prime93.10ms、slice10.02ms；merge／update／return<0.1ms。并不证明旧99ms self已完全定位，见[原诊断](current-main120-api-cost-525f-20261005.md) |
| 正常五页池／HTTP／完整核验／frozen v1 | 五页通过实际正常池调用；本次不执行HTTP、cProfile、新资格或实际preview。完整核验及v1不借用普通展示结论 | 回归执行原有期初、冻结、v1、核验、repair、backup断言；API／DDL／前端／v1合同未改 |

五页对照实际退出0，终态`diagnostic_complete`、cleanup为空、五个business_equal均为真。登录通过core `SecurityService`对已有合成owner进行密码认证，未执行native登录窗口或HTTP。完整数据库主文件、空WAL、结构、三项身份、state／history、FK、既有session、登出、pool和源码守卫成立；旧原件未改变。复用old525资格`f79923f6313b3be6fd8038810e750caa7328df439eab44d5af0a465683a50ef5`，明确fresh content／qualification／actual preview均false，新源没有在本诊断中完成新资格。

| 页 | SQL | 返回行 | 值字节 | VM旧→新（100条采样） |
| --- | --- | --- | --- | --- |
| 简报 | 190 | 14219 | 8245723 | 820500→815500 |
| 资金 | 166 | 10158 | 3606570 | 1245500→1243700 |
| 员工 | 97 | 5259 | 5189473 | 1175700→1175500 |
| 资产 | 380 | 14232 | 11950624 | 1104100→1101500 |
| 季度报表 | 254 | 18592 | 11598641 | 983000→980800 |

SQL、返回行／值字节、结果JSON、typed事实与采用slice等工作计数均与旧v3一致；VM小幅波动不归因本组收益。逐字段差异仅各页`/read_context/read_version`、简报`/data/generated_at`和报表`/checked_at`，未用宽泛时间归一化。旧v3工作量直接复用，没有重跑。仪表工作与宽回归并行，wall时间不用于收益判断，也不代替浏览器500ms；本组已证明的是查找层数及checkpoint重遍历减少。

首轮新增测试2失败、5.40s和宽组2失败／336通过、1992.96s均保留。unknown在同快照保持稳定未知，repair后使用新snapshot；full accounting的`.subjects`为描述union，而adopted-only为逐月描述，修正这两处预期后关联36项32.83s全通过，生产两src未因此再改。四个变动文件Ruff通过。宽组120月bank witness大样本构造是本轮长耗时，不因文档收口机械重建。

证据见[manifest](current-reader-group-8c98-20261005-evidence/manifest.json)：13个确定性redacted gzip、1项helper私有SHA引用，保留run／execution log／五份raw、首失败／修后定向／宽组日志、snapshot成功日志／忠实保存的首次tool stderr和源码manifest；未重复发布旧helper或旧v3原件。manifest SHA为`b3e0779387f0579d78f0073ddc6cf8e0a3331370cabaf1940752cdecc68ce7a4`；run原SHA为`45a1269b69940f11eabdd0e31494f62dd54176cc0831b7a4ff51498c00b0e2a0`，helper原SHA为`48480fbeb95c3d5f849f04542b4d42830c226c2b4e4ca0b4eea627e7fd2099e9`。私有guard用canonical SHA，路径、授权和内存地址脱敏，不归档数据库或凭据。

四分布、主规模按需及资源／前台影响继续待验。保持draft／0、现有5173、资料和身份；正式冻结／发布／入口切换延期。新源码尚无浏览器性能结论，阶段不宣称完成。
