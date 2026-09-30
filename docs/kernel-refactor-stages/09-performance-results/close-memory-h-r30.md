# H 组：完整核验的关账正文驻留

独立 120 月合成书在 r29 完整核验运行约 26 分钟后，RSS 达 9.43 GB、峰值 9.52 GB，进程被明确停止；该次核验**没有通过**。只读结构检查显示，119 个关账的 `material_coverage.coverage` 原块累计约 1.984 GB，`fact_ids` 与 `resolution_versions` 各约 308 MB、306 MB。原 `integrity._check_closes` 在开始验证第一月前就保留全部已解码 manifest，并继续把它们传给投影与索引比较器；关账预览也先保留全期解码对象。这是已确认的无界驻留，源事实/计算的保留是另一项，不能把 9.52 GB 全数归到关账正文。

修正保留原来的逐月完整存储解码、封签、身份链、发布边界、正式采用、资料覆盖、试算平衡和 owner review。每月通过核验后只在本次读取事务的内存中保留规范 JSON 的压缩像；完整核验成功返回的私有 archive 由原 `verified_source_lease` 绑定。消费者索取某月时再次检查同一连接、同一 lease、权威关账行全集及该月行内容，然后仅展开该月。提前取得 lazy view 后结束事务、重新开始事务或进入另一 lease，同样不能再读取。压缩像不落库、不作业务权威，也不用于证实自身完整。

| 入口/消费者 | 具体接线 | 保留的独立核验 |
| --- | --- | --- |
| 完整核验、备份验证、恢复验证、修复前后验证 | `integrity.verify_integrity` → `_check_closes` → `VerifiedCloseArchive` | 源、关账、各派生根与读取索引全部按原规则比较；`include_projections=False` 不返回 archive，仍核关账 |
| 关账预览及正式关账的历史检查 | `integrity.verify_close_integrity` | 逐月临时压缩，当前要返回的预览 manifest 仍是完整原响应；事务末不留证明 |
| 报表来源 | `report_projection.py`、`report_projection_v1.py` | 逐月根与来源行重建 |
| 报表语义 | `report_semantics.py`、`report_semantics_v1.py` | 逐月语义重建 |
| 报表 flow | `report_flow.py`、`report_flow_v1.py` | 逐月 flow 重建及原异常/冲突检查 |
| 分类目录 | `report_classification_directory.py`、`report_classification_directory_v1.py` | 逐月目录从已认证来源重建，节点/根仍全比 |
| 资料冻结观察 | `material_watch.py`、`material_watch_v1.py` | 历史 heads 与每月根、桶、目录全比 |
| 读取索引 | `read_indexes.py` | 每个关账来源的完整 occurrence 多重集仍逐项比对；Archive 仅按单月提供已验正文 |

固定 v1 的持久内容解码、规则、金额及根仍由其独立模块解释；通用 archive 只搬运已核验的 Python 值。v1 owner review 对**当前月 manifest 对象身份**的检查原样保留，本月检查通过后才把 prefix 中这一项缩为后续月份确实使用的 opening ID 与 vouchers。所有改动的固定 v1 reader 源摘要由既有 `content_v1` 描述符覆盖，最终冻结仍须据本次源码重算描述符。

独立 120 月 `--verify-only` 的入口并未递归跑两次完整核验：`stage9_independent_book.py` 恢复合成书并释放 `book` 后，`verify_book_open_preview` 在第一笔只读事务运行一次注册 verifier，再在另一笔事务运行 `Periods.preview_close`。后者的 `_manifest→verify_close_integrity` 会重新逐闭月核 journal、权威源、关账采用及派生结果，然后检查开放月准备和预览摘要；这是与首次完整核验有大量历史交集的**另一项验收**，不能未经同事务与公开预览语义证明就省掉。固定 v1 由注册 verifier 选择其独立 reader。备份验源/复制品/解包成员、恢复与修复前后也各有独立核验边界，均不是此次独立 verify-only 进程暗中启动的第二次完整核验。

重复展开的确切位置是 `_check_closes`：完整核验先逐月从存储解码并压缩，第二遍正式采用检查再次解压；预览先逐月解码并压缩，进入 `_check_closes` 后两遍分别解压。随后资料观察、报表投影、语义、flow、分类目录分别从 archive 按月索取已验正文，完整核验的读取索引比较器也如此。`_CloseLookup.__getitem__` 每次均解压并 `json.loads`，所以这些**不同派生比较器**确有跨比较器重复解压，但各自规则重建与完整集合比较仍需保留。把所有月份展开缓存会恢复 r29 已证实的驻留问题，旧增量桶负面方案也不据此重试。`_check_sources` 留存已解码的 calculations/facts 给后续检查，main48 单阶段已有约 922 MB 增量；120 月该阶段和各比较器实际 CPU/内存占比尚无 phase 计量。

已执行的定向验证：`test_single_month_close.py`、`test_report_flow_integrity.py`、`test_material_watch.py`、`test_read_indexes.py` 共 28 passed；`test_verified_close_archive.py`、`test_v1_verified_close_prefix.py`、`test_report_classification_v1.py`、`test_integrity_content.py`、`test_opening_adoption.py` 共 52 passed；`test_offline_upgrade.py` 的非空 v1 冻结采用与 v1 decoder 内容合同各 1 passed。两项 repair 断言另行复测通过，与前述 52 项有重叠，不重复计数。非空 v1 升级测试首次在实现中间态失败，暴露当前对象身份要求，修正后目标复测通过。Ruff check 通过。随后固定 r30 组结果为 511 通过、1 个快照夹具失败，修正夹具后相关 2 项通过，原失败不覆盖；主 12／48 月注册完整核验和真实预览通过，进程峰值工作集约 695.4 MB／2.58 GB。原始结果见[组记录](group-r30-manifest.json)。独立 120 月仍在运行，不能据此宣告 H 组完成。

| 已独立核验的合成书 | 关账数 | 原存储 decode CPU | 规范化及压缩 CPU | 一轮解压与 JSON CPU | 压缩暂存总字节 |
| --- | ---: | ---: | ---: | ---: | ---: |
| main12 r29 | 11 | 3.28 s | 0.63 s | 0.34 s | 7,950,967 |
| main48 r29 | 47 | 52.94 s | 7.47 s | 4.89 s | 78,608,453 |

以上为单独只读诊断，不是完整核验或页面时延。多个完整比较器各自逐月展开，会增加有界的 JSON CPU；不能只报压缩后的字节而把解压成本当零。原始逐月结果见 [12 月](h-close-archive-main12-cost.json)、[48 月](h-close-archive-main48-cost.json)。独立 120 月最后单月无 tracemalloc 的存储解码 RSS 增量约 155.9 MB，其规范逻辑正文约 41.73 MB、压缩后约 5.64 MB，见 [单月记录](h-close-last120-no-trace.json)。有 tracemalloc 的 RSS 增量不可作生产内存外推。`_check_sources` 单阶段无 tracemalloc 在 main48 另增约 922 MB，本组未改变该保留范围；120 月完整核验峰值和总 CPU 必须由后续固定候选实测。

独立 120 月 r30 运行中，内存观察器在 06:38 写诊断文件遇到 `IOException`，错误地把自身停止写成 `process_exited`。核验进程 39312 仍在运行，原观察文件和脚本保留。06:40 以新的 `stage9-independent120-r30b-memory.json` 接续观察，并记录 `stage9-independent120-r30-observation-restart.json`；这一时间段的采样缺口不补造。共享观察脚本改为有界重试和原子替换，观察器失败与被观察进程退出分别表达。已观测进程峰值约 6.33 GB，仍不是完成结果；最终核验状态只看核验器自己的回执。

该 verify-only 入口只在整个 `verify_book_open_preview` 返回后写最终 stdout；目前没有中途 phase。RSS 先降后升不足以判断当前正在完整核验还是预览，更不能推断死循环。下次独立测量宜在注册 verifier 与预览前后，以及完整 verifier 的 `_check_sources`、`_check_closes`、各比较器前后单独 flush JSONL 阶段记录，计 wall/进程 CPU/RSS、按调用者解压次数；阶段文件不替代完整核验回执。本次未注入或停止进程，也未据运行时间判断失败。
