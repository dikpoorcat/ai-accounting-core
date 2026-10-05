<!-- @format -->

# r70：当前内容核验、边界与承接预检

固定源SHA `402024802800938685bfb59983e6d717ae32fa55c555bad0a3ced3f3d8a8e404`。主12、主48和独立12三个同结构合成根已分别由当前r70源码的注册checker完整核验：status=complete，sources/historical_adoption/projections/read_indexes四coverage均verified，limitations=[]，各自真实开放月preview成功并保存当前source、身份、state/epochs和digest。这些新回执取代“该三个根的r70注册核验运行中”状态；原native诊断里的not_run仍是诊断时的真实记录，不回写旧档。

本轮核验与12月proof、dry-run及归档有并行工作，不能将耗时解释为独占性能。`integrity_ms`仅计注册内容核验段，不是进程总elapsed，也不含随后开放月preview总时间。三个根通过不证明120月、压力、浏览器或正式包通过。仓库仍draft/0；隔离r70 released1不是正式仓库冻结。

## 当前源码注册核验

| 样本 | 实际根 | facts / calculations / vouchers / closes / evidence | 原始integrity_ms | current preview |
| --- | --- | --- | ---: | --- |
| main12 | stage9-owner-main12-r43 | 30,225 / 12,413 / 12,309 / 11 / 85 | 98823.928 | 2016-12，当前预览已实际生成 |
| main48 | stage9-owner-main48-r63 | 121,593 / 50,519 / 49,245 / 47 / 337 | 1059252.082 | 2019-12，当前预览已实际生成 |
| independent12 | stage9-owner-independent12-r63 | 30,114 / 12,025 / 12,000 / 11 / 37 | 89709.757 | 2016-12，当前预览已实际生成 |

主12／主48的current preview digest与construction digest不同，分别保存，未改旧构造依据；独立12两值相同也不省去真实preview。报告验证的实际source均为`.tmp/stage9-build-source-owner-r70-release`。stdout均返回complete，主48进程session84789 exit0；不借prior r63 proof替代当前来源。本次整理没有再次执行checker或preview。

## 固定来源的full边界回执

`.tmp/stage9-owner-r70-boundaries/report.json`记录source=fixed r70、mode=full；主线程session80386 exit0，stdout指向该报告。保留报告中实际采样与计时，但它是功能边界回执及包含harness的RSS采样，不是纯浏览器、独占CPU或整个服务树峰值验收。

| 边界 | 实际接受 | 超界实际拒绝与写入保护 |
| --- | --- | --- |
| 单原件 | 20MiB=20,971,520B，保存digest | 20,971,521B拒绝；固定脚本在异常后执行_assert_unchanged，全物理计数不增。报告未单列证据拒绝前后数字，不能补造。 |
| 银行原行 | 100,000行；CSV7,389,036B；物理entries100,000；typed save delta fact_revision1、entries100000、request1、audit1 | 100,001行公开inspect拒绝，registration_attempted=false；全物理计数不增，after为evidence3/fact_revision1/entries100000/request5/audit5。 |
| 原子批量事实 | 5,000项；physical_fact_delta5000/request_delta1/audit_delta1 | 5,001项拒绝；计数不增，after=evidence3/fact_revision5001/entries100000/request8/audit8。 |

最后物理计数与批量拒绝后的计数相同：evidence3、fact_revision5001、bank entries100000、request8、audit8。各accepted/rejected在自身操作前后核计数，不能将较晚总计数与前一步相比称为拒绝写入。此full边界不是240/1536MiB多原件verify/backup/restore流程，也不是200员工/5000业务的12月压力验收。

## 大样本承接只是dry-run

三个`stage9-owner-{main120,independent48,independent120}-r70-rebase-dry.log`均为exact_difference_verified、synthetic_raw_rows_owner_candidate_not_migration。它们证明已选原报告/checkpoint/固定源与精确结构delta、原身份/state及冻结digest预检匹配；没有创建/复制/发布目标，也没有目标r70注册完整核验与current preview。

主120和独立48来源仍built_not_verified；独立120来源complete只证明其旧r30回执。不能把三个dry日志标成当前120/48月full targetverify，也不能当正式迁移。wrapper绑定r70 SHA，pinned r49 executor SHA=`8711ee4a99043ee573dceec995d6f311ca97714ee9bae1019a09c9f9c4a603e4`，原精确CASE、合同delta、raw-row证明与当前注册checker保持。未知变化拒绝、失败staging保全、目标已存在拒绝；不重放业务或改冻结历史。

## 归档范围与下一步

[独立manifest](owner-r70-content-and-boundaries-manifest.json)保存三个JSON/stdout、边界report/stdout及固定脚本、r70wrapper/r49executor、三个dry日志，以及三份只读准备文档entrypoints/coverage/pressure。准备文档中的建议命令是未执行方案，不是执行回执；旧“运行中”判断保留为写作当时状态，不用于当前验收。每条记录raw与gzip的SHA、bytes、mtime，排他创建并解压逐字节确认，旧档不覆盖。

浏览器session59943仍在准备，ready/start gate由主线程管理，本组未复制任何运行中browser中间结果。原生六入口低于500ms仍不是浏览器通过。大样本承接、当前120/独立48/压力核验、正式v1三合同、最终运行包/实际AI MCP和原件规模仍未完成。本次仅证据整理，没有测试、测量、SQL、服务或生产编辑；完成后停止工具以等待纯计时。
