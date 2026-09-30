# 材料与 SQL guard 双向顺序：r33 私有 AB

r33 已实现**材料检查先成功、SQL guard 后读取**的同一受控快照复用，但默认简报的串行诊断另有相反顺序：部分结果先经过 `QueryReads.verify_sql_outcomes`，后进入 `_MaterialSqlOutcomeBatch.verify`，仍重复严格解析。两者核对的是同一 `calculation.id` 的存储 JSON 重复键／格式与摘要；这份证明仅供 SQLite JSON1 消费前的语法和摘要门禁，不是事实、依赖、正式采用、凭证或来源封签证明。r33 一开始只量了材料→guard，漏列 guard→材料，不能把材料 ID 数直接当作实际节省数。

私有 AB 只在 `_MaterialSqlOutcomeBatch.verify` 内，先核同一个 `QueryReads.snapshot` 的连接对象和快照令牌，再按精确 ID 查已有成功 guard 集及本批局部成功集。命中者跳过再次 `verify_outcome_bytes`，但 `_material_result` 的字段、期间、SQL 金额投影及材料检查继续执行。未命中者仍用原严格验证，整批正常结束才提交本批成功 ID；失败批次不提交前缀。AB 原型未修改固定源码或合成库。其后已把这项**窄双向复用**实施在当前 `query_reads.py`：生产批对象在每次 `verify` 和 `commit` 前检查活跃连接、快照令牌及未提交状态；离开快照后持有的批对象明确拒绝，不能借过期 ID 免验。跨连接和未托管调用仍走原严格验证。

| 真实入口 | r33 已有单向复用 | 双向私有原型 | 变化 |
| --- | ---: | ---: | --- |
| 串行原生 `brief(preparation='complete')`，默认 100 明细 | 材料核验 999；guard 已证 727；`verify_outcome_bytes` 1,726 次／唯一 ID 1,076 | guard→材料命中 650；严格核验 1,076 次／唯一 ID 1,076 | `loads_unique` 的其它 stored JSON 次数 1,228→578、对应原文字节 1,317,177→611,773。SQL VM 3,131,200、返回行 74,766、返回值 21,202,672 B **全部不变**。完整响应只差 `generated_at`。 |
| `period_preparation` | 材料先核 999，后续 guard 核其余 17；严格核验 1,016 次／唯一 ID 1,016 | 反向命中 0；严格核验仍 1,016 | SQL VM 2,044,800、返回行 69,069、返回值 18,139,229 B 及完整响应全部相同。 |

资料检查处于 QueryReads 受控快照时，先前 guard 已成功的精确 ID 可作为语法／摘要前置证明；同事务只读且 authorizer 阻止事务重开，任何后续同 ID 读取看到相同存储行。批内第二次同 ID 亦可按局部成功集跳过；局部集未提交前不供其它调用方使用。反之，未托管读取、其它连接／进程／事务、写入准备和失败后的重读均必须执行原核验。默认**常驻服务**完整简报将 materials 放到 worker、父进程另做 guard，两个证明不跨进程复用；本表是串行原生路径，不能当线上浏览器延迟改善。新增真实定向覆盖两种顺序、已证 ID 加失败后新前缀不缓存、批内重复、持有批离开快照、新快照及 8 个无关正式历史结果，4 passed；统一固定候选／大组仍待主线程验证。

同类调用边界：`stored_json.verify_sql_outcomes` 由 `QueryReads` 的状态元数据、资金／员工／资产 JSON1 读、期间银行检查调用，受控快照成功集已在这些调用间复用；`Periods` 独立读取与 `Engine` 期初写入直接调用存储 verifier，不承接该快照集。`business_queries` 的资产 owner／导出依据、`close_review` 的工资确认、`identity_corrections` 的写入准备直接 `verify_outcome_bytes` 后**消费完整 outcome 对象**，仅有 ID 证明不能替代该对象的解码，写入准备还处于不同事务。导出其它正文通过选中来源核验与严格 `load_outcome`，也不因材料／SQL guard 集免验。`QueryReads.verify_selected_content`、anchored bytes 与完整核验／维修／备份的源和采用证明职责更强，未在本 AB 量到可安全减去的同顺序工作；固定 v1 独立 decoder 和历史比较器不使用此当前快照集。这些路径不应因双向语法缓存改变。

证据为固定 r33 **读取候选**访问 r32 已独立完整核验的同一合成库；r32 与 r33 的 `schema.py`、catalog/company/content-v1 正式合同及 `content_v1.py` 来源 SHA 一致。尚未把本次读取称为 r33 独立完整核验。背景有组回归，CPU／wall 仅诊断，不是 500 ms 验收。四份原始 JSON 的无损 gzip 及各自原文／压缩 SHA-256 见[归档清单](r33-material-proof-two-way-archive.json)。
