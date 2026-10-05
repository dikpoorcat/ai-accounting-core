<!-- @format -->

# adopted direct-reference 索引 TEMP shadow 诊断（2026-10-05）

原件状态为 `diagnostic_complete`，实际运行时间为 2026-10-04 17:49:28.987278–17:50:08.809372 UTC（本地 2026-10-05）。固定来源为 `ed96dfd296cd96d4279745eb0ac26a9422db450c10d3bb9f8e7def11e2f9d8c8`，771 文件，源码清单 SHA `58067bd14dcb1374b944cd891842c69ad4385ca020f37437464d39f7907a63cf`。本记录只封存已结束的诊断，不执行数据库、测试或性能工作。

## 设置与结果

helper 以 `mode=ro` 打开原合成 main120，公司 main 表不变；在 TEMP 完整复制 `close_reference` 的 8,933,123 行及原始表 DDL、已有索引，其他查询仍使用 main 的源表。原始、TEMP shadow baseline 使用相同 SQL/参数，indexed 仅将两处 `INDEXED BY close_reference_lookup` 改为新候选索引。五次原始调用的 semantic SHA 与此前保全主样本匹配，两种 TEMP 对照的返回 multiset 也与原始相同。

候选索引的 key 为 `(reference_type,reference_id,path,close_period,position)`，partial predicate 为 `path='adopted_results[*].fact_id' OR path='adopted_results[*].calculation_id'`。它针对直接采用引用的两个路径，不放松成员、publication、digest 或业务验证边界。

| 调用 | 返回行 | 原始 VM | shadow baseline VM | indexed VM |
| --- | ---: | ---: | ---: | ---: |
| assets | 240 | 239500 | 239500 | 96700 |
| employees（非空） | 100 | 28000 | 28000 | 28400 |
| employees（空） | 0 | 400 | 400 | 400 |
| funds（空） | 0 | 56300 | 56300 | 53200 |
| funds（非空） | 241 | 112100 | 112200 | 109000 |

VM 每 100 条指令采样，112100/112200 这类微差包含采样量化，不能写成确定的额外业务工作。以下为各调用单次插桩 execute/fetch，单位 ms；不是整页或客户端响应时间。

| 调用 | 原始 execute / fetch | shadow baseline execute / fetch | indexed execute / fetch |
| --- | ---: | ---: | ---: |
| assets | 152.749 / 2.732 | 250.517 / 2.811 | 30.024 / 2.924 |
| employees（非空） | 5.756 / 0.692 | 5.049 / 0.687 | 4.811 / 0.693 |
| employees（空） | 0.135 / 0.0005 | 0.126 / 0.0005 | 0.153 / 0.0004 |
| funds（空） | 27.312 / 0.0019 | 25.988 / 0.0015 | 19.241 / 0.0021 |
| funds（非空） | 42.873 / 1.240 | 40.316 / 1.171 | 34.049 / 1.190 |

资产 VM 工作量下降，其他样本收益有限或无收益，支持将这个 OR partial index 作为窄范围候选。当前观察不证明实际生产整页改善或 500ms 达标。后续实现、隔离迁移和生产验证属于另组，不由本 helper 证明。

## 限制与保护

原 main 与 TEMP 的记录布局、介质和表数据热态不同，执行顺序也改变页面温度，不能将单次原生耗时差异解释为稳定因果收益。TEMP 使用 FILE：复制表及原索引约 26.756s，新候选构建约 1.675s；末尾 TEMP 页数 493606、页大小 4096，总容量 `2021810176` 字节。约 2GB 覆盖整个 TEMP 表及全部索引，不是新索引大小，也不是生产迁移空间或耗时估计。

原件的源码、业务/身份/状态/历史守卫及 session 不变标志均为 true；没有创建新会话、服务或业务写入。helper 没有验证正式迁移、fixed-v1、备份、完整内容核验或新资格/close preview。本次归档依据 receipt 记录保护事实，未重新哈希数据库，也不是独立完整业务复验。生产、主阶段、路线图、AGENTS 与已有档案未修改。

独立 [manifest.json](adopted-index-shadow-20261005-evidence/manifest.json) SHA 为 `eb7e1021f3fd1b0739b844fc8a9425ecdce313f7c3e0f1211175488ce4010e67`。JSON 与日志保存为两件脱敏 gzip，完整保留五次调用的三个 SQL、plan、VM、execute/fetch 与结果；参数、内部 ID、guard 哈希化，私有路径/PID/内存地址/认证内容脱敏。helper 不发布内容，仅私有路径/SHA；已封存的 capture 和源码清单仅记录来源 SHA。清单含原件/脱敏/gzip SHA、字节数及解压回验，gzip `mtime=0`，回验相等。不复制数据库、ZIP、凭据或真实资料。

诊断 JSON 原始 SHA 为 `89f4e2fbc54898a3aaa27e114ddf74711d1503f7ede4d24b1039839f678ae0e2`，helper SHA 为 `c1c779e34cb70645fe43084b48703c2dc1361965e15b7e4d04ed3182d6b70fb1`，capture SHA 为 `b15f0942c2d2282f7ae18e2b7fbe5e5c7d0b390c7de40269e187cba4ad36a233`，均与实际原件匹配。
