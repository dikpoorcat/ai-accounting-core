# G 组：保存 JSON 的唯一解释与候选范围

SQLite JSON1 取对象重复键的首值，Python 普通解析取末值。r30 合成公司中，给已发布资金结果的 balances 前置另一同名字段、保留末尾原值和原逻辑摘要，原完整核验仍通过，而资金页和本月准备检查选到了错误银行账户；员工档案与查重 manifest 也有独立的负筛反例。原始复现分别在 .tmp/stage9-json-outcome-current-r30c.json、.tmp/stage9-json-profile-duplicate-r30.json 和 .tmp/stage9-g-duplicate-manifest-double-interpretation.py。这些是合成库损坏，不涉及真实资料。

本组用 current stored_json.loads_unique 和独立 fixed-v1 stored_json_v1.loads_unique 拒绝所有嵌套及转义后等价的重复对象键。相同记录行的 digest 可与正文一起受损，因此即使原文字节 SHA 恰好等于该行 digest，SQL JSON1 解释前也必须检查唯一键；已由不可变外部锚绑定的原文字节证明仍保持原有快路。合法非规范 JSON 的空白和字段顺序等价性不变。命中结果同时核原正文摘要，语法证明不冒充发布、依赖或业务采用证明。

| 范围 | 调用方与决定 | 验证边界 |
| --- | --- | --- |
| calculation.outcome 的 SQL JSON1 选取 | 资金、银行对账、员工/资产月度指标、资产消耗、资料结果、期间准备、期初唯一性、精确投资来源：在各自准确候选范围先严格核原文，再执行原 SQL；settlement_subjects 仅守卫决定 obligations 存在性的 JSON 路径，保持原候选 SQL，不给负选结果记完整 outcome proof | 选中凭证、正式发布、封签、事实和关系来源仍由原调用链核验；负选不能只验证 SQL 返回行。 |
| 已命中结果的 Python 直读 | QueryReads、资产批次 owner、身份纠错、期初绑定、业务依据、导出付款、file job 精确来源等使用严格解析；未变更原计划绑定及业务依据匹配 | 旧相同事实及结果来源的完整内容检查保留，损坏不转为 needs_information。 |
| entity_profile_revision.content 与事实复合 JSON | 员工候选在 Python 严格核所有最新 person 档案后筛选；Store.decode_fields 在事实 digest 检查前严格解析复合列，清偿 slots SQL 前仍核精确事实版本 | 历史档案修订不为当前名单逐条解码；已选历史事实原文仍按原范围验证。 |
| business_duplicate_check.manifest | 负筛前 SQL json_tree 拒绝重复键；同批登记的来源范围一次核验，纠错祖先按本次行向前定位精确 check；选中 check 的 current/v1 reader 严格解析，完整核验与修复仍独立检查 | SQL 扫描仍是实际成本，不能描述成零成本或固定次数；不跨写批缓存成功证明。 |
| fixed v1、完整核验、修复与备份 | 历史 source decoder 走固定 v1 模块与内容描述符；完整核验结果正文也拒重复键。维修/备份原件变更的真实合成反例均拒绝并保持源、投影、请求和审计不变 | 不使用未来 current model 重新解释已发布历史；本组不改变 DDL 或正常结果摘要格式。 |

同快照 QueryReads.verify_sql_outcomes 仅缓存整个成功批次的精确 ID；失败后不缓存部分前缀，新快照重新检验。真实生产函数工作量测试覆盖这一边界、员工 100 条无关历史修订只解码最新 1 条、批量候选中多个来源位置只执行一次 SQL manifest 守卫。测试在 test_stored_json_scope_work.py、test_duplicates.py。真实结果损坏测试在 test_stored_json_selection.py、test_exports.py；现有资产批次、业务任务、期初身份和维修边界定向继续通过。

r31 固定候选的 74 文件组结果是 **898 passed、51 failed、2 errors**，源码清单前后相同；不能记为已通过。[原始组回执与日志清单](group-r31-manifest.json)保留全部失败。主 12 月独立完整核验走到真实 preview_close 时，资料批量 linked_subjects 行缺少新校验器要求的结果正文和摘要列，抛 IndexError；同一漏列解释了组内 48 个资料相关失败。其余失败由各自负责组处理，不归因为资料漏列。修复只在 materials.py 将 id、fact_id、period、stored_outcome、result_digest、result_values 定义为两种 SELECT 共用的窄列合同；单对象和批量查询仍保持原来源范围及金额投影。test_materials.py 的真实来源→正式 expense→resolution link 测试先核批量 complete，再用前置重复 values、末值原样、同行 digest 改为原文字节 SHA 的损坏结果，批量入口拒绝为 content_integrity_failed。它与已有 split 和 read-summary 定向共 **3 passed**；r32 统一受影响组仍在运行，不能写成通过。

新增校验器的行字段已逐调用核对：BusinessQueries 的 asset owner 和 file job、CloseReview 的 basis inventory 和 payroll cards、IdentityCorrection 的 snapshot、stored_json 自己的精确查询均已选 id/outcome/digest；QueryReads、FundsRead、Periods、Dashboard 的守卫只传 ID，不要求调用方行增加字段。漏列只发生在 materials 的批量分支。固定 v1 不调用该资料 helper；完整的同型字段审查已并入本节，不再另立补丁说明。

读取成本不能沿用 G 修复前的资金/资料页字节统计。只读 main48 末月诊断抽取 1,073 份结果、1,026,690 原文字节，原文字节 SHA 与同行 digest 均匹配。相同样本各运行三遍，普通 JSON 解析墙钟约 8.0–8.2 ms，严格唯一键解析约 13.3–13.7 ms；这是独立 parser 诊断，**不是资料批量查询的总成本**，也不是完整页面或纯浏览器时延验收，Windows CPU 计时粒度不足。资料批量结果现在必须读取完整 outcome 原文作严格检查，原金额-only 字节数已失效；新增 SQL 守卫及实际整页字节/VM须待 r32 固定源码计量。原始 parser 数据归档在 [r31 组清单](group-r31-manifest.json)。

已核的非扩修边界见 [受保护调用方](stored-json-protected-callers-r31.md)：只由 Python 解释的生成中间 JSON，或调用前已有同事务完整来源证明的保存正文，不因文本出现 json.loads 而重复全历史验证。阶段整体状态和正式冻结门禁见[第 9 阶段主记录](../09-performance-and-release.md)；按需详情及 fixed-v1 来源的总体通过范围仍以新的固定组回执为准。
