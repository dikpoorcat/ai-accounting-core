<!-- @format -->

# 未发布副本与迁移保留摘要：2026-10-05

本组消除独占新副本在 SQLite backup 前预置 WAL 所造成的额外整库 WAL，并复用同一写事务内首段重复的保留摘要。固定源码为 `a5d4e1ac7d41814bb7a74d30410e0dbe45e6a691c85bd69f71deff10f57bd1d1`，source manifest SHA256 为 `a95fce65d9fc6d5381f043d591caf2ab6562f1b08bba1ce24090c833e3639a0b`，记录 776 文件。归档核对 manifest 字节及其中相关 7 个源码/测试文件，未重新核对全部源码、数据库或服务。

生产范围是 `backup.copy_to_unpublished_database` 与 `offline_development_upgrade` 的新 catalog 备份目标。新目标先切 DELETE，分批复制完成后恢复原模式；原 WAL 目标必须完成 TRUNCATE `(0,0,0)`。调用者仍负责独占、新建、结构与身份核验、发布或失败保留。失败不发布，可保留 DELETE 模式的部分副本供诊断。活动库 WAL 与来源不变。正式公司 ZIP 备份原先就使用原生 sqlite3 新暂存文件、timeout/progress 与最终 DELETE，本组未更改该路径；restore 的解包核验与发布前 checkpoint 也未替换为此函数。

历史合成构造器范围是 `prepare_stage9_draft_book._copy_pages`。它复用同一函数，但仍只接受完整相同的 c9/b484 历史 SQL；原始字节、身份、新目标空置、metadata 更新、最终 checkpoint、attestation 和注册/发布门禁保留。测试从完整归档 c9 对象实际建库，正常安装 metadata 并验证结构。三项完整 prepare 故障场景显式采用测试专用历史 SQL 生成器，仍经过真实对象及指纹比较；这些结果不表示当前 525 生产生成器接受 c9。当前 525 目标被历史 page 构造器拒绝的测试通过。

迁移摘要范围是 `upgrade_company`：取得写锁后，首段 pre-hash 与 `original_rows` 是同一来源、同一事务且中间无写入，因而复用该摘要。第二段仍重新绑定第一段实际生成的表和历史回执；各段 post-hash、结构核验、身份绑定、历史保全和失败回滚均保留。定向 XML 记录 c9 单段原始 366 行、累计扫描 732 行；923 两段原始 366 行、累计扫描 1466 行。代码消除一次首段全表扫描，不据此宣称端到端时间收益。

| 已完成记录 | 实际结果与限制 |
| --- | --- |
| 首次复制回归 | Windows access violation，栈位于 backup 的 progress 回调；测试在复制中对目标执行 PRAGMA，属于不安全重入观测。保留 fatal log，没有完成 XML；之后只观察文件 WAL 大小。 |
| r2 六模块统一组 | 87 PASS / 9 FAIL / 16 ERROR，84.69s。历史 fixture 使用当前 525 合同，而构造器固定 c9，存在合同选择与建库不匹配。不是通过组。 |
| r3 | 8 PASS / 5 FAIL / 27 ERROR，6.61s。只改 bundle 仍由当前 DDL 建库，严格结构核验拒绝；未绕过核验。 |
| r4 | 38 PASS / 3 FAIL，23.16s。归档 DDL 建库成功；三个完整 prepare 故障测试被当前生成器与 c9 的 SQL 门禁提前拒绝。 |
| r5 | 38 PASS / 3 ERROR，21.04s。测试专用历史 SQL 按对象名字顺序还原，使索引先于依赖表；随后改为表、索引、触发器顺序。 |
| r6 历史两模块 | 41 PASS，24.89s。只覆盖 `test_stage9_copy_checkpoint` 与 `test_stage9_draft_fixture_copy`；包含失败不注册/不发布、来源与 raw BLOB 保全及当前合同拒绝。 |
| root 保留摘要定向组 | 5 PASS / 15 deselected，11.37s。两项记录扫描工作量；三项在各段 DDL 后真实修改 state，要求摘要检出、数据与结构回滚、外键恢复。XML 的两条 record_property 格式警告原样保留。 |
| a5d4 最终 17 模块统一组 | 279 PASS / 0 FAIL / 0 ERROR / 0 SKIP，506.61s，exit 0。固定源、manifest、776 文件前后相等；runner 要求 isolated、dont_write_bytecode、UTF-8，记录的五个实际 kernel/package import 全部位于 a5d4 snapshot 的 src。cache 和 fixture 使用源目录外的专用位置。 |

上述运行独立保留，不相加。通用文件回归包含约 8 MB 合成 BLOB，分批观察目标 WAL 最大值 0、源 main/WAL SHA 不变、源数据不变、中断保护及只复制 main 到新名后的 readonly/integrity 自足读取；目录备份测试也核对独立 main 的结构和登记身份。r2–r6 及 root 五项定向测试为此前工作区运行及各自 provenance；最终 279 则单独绑定 a5d4 固定源。相关模块回归不替代完整 qualification。

临时 v2 clone 是另一条私人合成诊断路径，仍绑定 ed96 来源，不是本组 a5d4 生产运行。回执记录公司 main `14359863296` bytes、目录 main `147456` bytes、来源守卫相等，并明确 `copied_content_unverified`。root 报告公司 WAL 为 0；归档者未独立打开副本或重测。初次 runtime-WAL clone 导致整库 WAL、部分 main 与磁盘耗尽的事实来自 root 消息记录；现存首次 copy log 仅含进度，没有完整失败 traceback，不能补造。v2 没有完成完整业务核验、preview、服务或性能验收，后续 ce54 main120 迁移/内容核验仍在执行。

ce54 包首次尝试的真实 receipt 为 failed，自检调用次数 0，失败在构建前源 inventory 的 extra 文件断言。root 报告四个测试 cache；receipt 本身未列这些 cache 的名称，归档仅保存其真实失败与 SHA，不宣称构建或自检通过。新 [a5d4 开发包默认自检](development-package-a5d4-20261005.md) 已独立记录 PASS。`stage9-copy-retention-consumer-20261005` 的最终统一组也已完成，原件 log/XML/JSON 单独封存；279 不与此前 41、5 或其他历史组相加。

最终统一组的实际模块是 `test_report_flow`、`test_report_flow_integrity`、`test_report_projection`、`test_reports_bounded_reads`、`test_dashboard_reports_adapter`、`test_dashboard_projection`、`test_dashboard_funds_alignment`、`test_dashboard_workforce_assets`、`test_unpublished_database_copy`、`test_stage9_copy_checkpoint`、`test_stage9_draft_fixture_copy`、`test_direct_adoption_development_upgrade`、`test_development_upgrade`、`test_runtime_backup`、`test_development_backup_rollover`、`test_direct_adoption_index`、`test_direct_adoption_v1_isolation`。这覆盖相关读消费者、备份恢复、迁移、索引与 fixed-v1 隔离，不表示全仓或完整 GUI 验收。大库 content1 仍在执行；独立 MCP 隔离 host 已准备好，AI 业务验收未开始。

本组没有新的实际浏览器、完整 qualification、500ms 或纯计时通过结论。真实磁盘满故障注入、journal_mode 切换失败、目录外部写者并发、大库峰值与全生命周期资源尚未由本组小型回归证明。未操作真实资料、数据库、身份或现有服务；不表示第 9 阶段完成或正式交付。

独立 [证据 manifest](unpublished-copy-and-retention-20261005-evidence/manifest.json) SHA256：`8924ea0e03dbb21f364df6e936e2da08a9aca66794c01b7cb7792b9c2ec73ff0`。封存 23 件脱敏 gzip、7 件私有 SHA 引用。保留失败原件、日志、XML、provenance、source manifest、clone 回执和 ce54 失败回执；helper、私人合成输入仅索引 SHA，不复制数据库、ZIP 或凭据。路径、内部标识、进程/线程标识及地址脱敏，private guard 以规范 JSON SHA 表示；每件记录原件 SHA、脱敏 SHA、gzip SHA，并验证解压字节相等。归档是证据整理，不是独立复验。

最终统一组使用另一个 [consumer 证据 manifest](unpublished-copy-and-retention-20261005-consumer-evidence/manifest.json)，SHA256 为 `1abcdc76f61592e39abd8e75ea6f68605e9a97260b0931256fc365b1602d823e`。3 件完整脱敏 gzip 保存 log/XML/JSON，runner 仅私有 SHA 引用，逐件解压回验；前述初始封存 manifest 字节不变。初始 manifest 中“排除进行中证据”是当时的状态，不作为最终统一组或新包的状态。
