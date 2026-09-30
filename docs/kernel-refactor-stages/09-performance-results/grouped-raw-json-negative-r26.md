# r25 员工对象引用原文：SQL JSON / raw SHA 私有原型

固定 `.tmp/stage9-build-source-r25-final-release`、已 `complete/verified` 主48月合成库，只读真实 HTTP 员工默认与单人详情；原型仅 `.tmp/stage9-r25-payroll-head-scope.py` 的 `raw_fact_fast` 分支。生产源、固定源码、库和数据格式未改。

## 安全边界

现行 `_expected_rows` 从 `fact_revision` 取权威 digest，`Store.fact_data_many` 按类型表/子表还原每字段，`digest(data)` 规范序列化并比较，再 `references_from_data` 枚举全部引用及 `_current_bindings` 处理身份纠错。私有分支保留元数据、引用、当前绑定的相同规则；仅对 `_scalar_fact_json_sql` 支持的类型尝试 SQL 构造完整 JSON、SHA256 与权威 fact.digest 相等后 `json.loads`，不等/缺行/不支持退回原逐字段读取及 `digest(data)`。固定 v1 `content_version=1` 直接原路径。没有跨请求缓存。

这不能简单复制现有 `_scalar_fact_hashes` 的安全声明：`QueryReads.verify_selected_content` 在它之前还独立检查所有命中源的 seal、物理期间和 `verify_fact_child_order`；普通 `_expected_rows` 并没有同等前置检查。`_scalar_fact_json_sql` 排除 composite JSON codec 和 v1，子表按 item_no 聚合；缺 parent 进入 fallback，额外 child 改变 raw 摘要（摘要不符时 fallback）。若子表仅 item_no 有不连续但同顺序内容，原 `fact_data_many` 也只按 item_no 排序并不检查连续性；完整核验的独立子行检查仍不可省。

固定样本本次工资 2,400 事实及 profile 50 事实均受 SQL fast path 支持；`stage9-r25-raw-fact-json-safety.json` 显示这 2,450 条 SQL JSON 与 `canonical(json.loads(raw))` 全同，另有中文、组合字符、emoji、U+2028、换行、引号、反斜杠样本同形。**这不是所有可能损坏编码的证明。**在同一内存 payroll 表将合法年月整数改为 120000，SQL `value_sql` 的无效月份分支产生 JSON `"period":0`，`json.loads` 后仍是规范 JSON；原 `decode_fields → YearMonth.from_ordinal` 则抛 `ValueError`。若攻击者同时把 `fact_revision.digest` 改成该 raw JSON 的 SHA，仅 raw SHA 会接受原实现拒绝的损坏。要保留原拒绝，至少必须在快路径逐月字段严查 SQL 值/物理值及其他 codec 对应的异常，而这会增加复杂度与成本。v1 必须永远留原规则。

## 实际工作量与同义性

默认页 ABBA 原始文件 `stage9-r25-final-main48-payroll-head-scope-{A1,A2}.json` 和 `...-raw_fact_fast-{B1,B2}.json`；全部 HTTP 200、完整响应 SHA256 相同 `df5248ba...b8db04`。原 `_expected_rows` CPU 两次 171.9/187.5ms；快路 125.0/140.6ms，子段稳定少约 47ms。原完整页进程 CPU 828.1/796.9ms，快路 765.6/843.8ms；均值仅约 812.5→804.7ms，差约 8ms，低于本仪器化/并行环境波动。快路新增 SQL 返回 2,450 行/1,133,770 字节、90,600 VM，代替原 `fact_data_many` 类型/子表读取约 4,900 行/555,420 字节、75,700 VM；原 raw JSON 约 1,055,370 字节。单人详情固定哈希种子下完整响应 SHA 相同 `a287e62a...24451ddb5f2`，单轮 CPU 1156→1031ms，但无稳定净收益结论。

非规范 raw 编码故意在一条 SQL JSON 前加空格，SHA 不等时仅该条退回原类型读取、整页响应仍完全相同（`...raw_encoding_mismatch-F1b.json`，2449 hit / 1 fallback）。在 fallback 原始事实正文注入一处改动，两路径都返回同一个 500 `content_integrity_failed / component=fact / record_id`（`...raw_fact_payload-F2.json`）。这是私有读时注入，不替代物理损坏/摘要联动的正式回归。

决定：**不落生产。**普通员工默认页未得到足够净收益，SQL 原文字节明显上升；更有月份 codec 反例表明仅 raw SHA 会削弱损坏拒绝。该负结果与此前“只展开 entity 声明约省 8ms”不是同一优化，均保留。不以新的通用原文缓存或全局 SQL JSON 框架继续试探。
