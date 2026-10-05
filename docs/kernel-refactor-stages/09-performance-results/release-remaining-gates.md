# 未来正式能力与既有隔离证据（2026-10-04）

用户本轮明确仅使用开发库draft／0，现有5173、资料根及身份原样；正式冻结、正式包交付和运行入口切换延期。本页保留未来能力与历史隔离证据，不维护第三份实施计划，不授权升级、冻结、换库或操作当前服务。当前工程状态以[第9阶段](../09-performance-and-release.md)和各group记录为准。

## 当前开发范围与待验

当前源码、验证结果和收口状态统一见[第9阶段](../09-performance-and-release.md)。[9ac主120浏览器](current-main120-browser-9ac1-20261004.md)、[旧开发包与限定MCP](development-package-mcp-9ac1-20261004.md)、[限定GUI正确性](current-dashboard-navigation-9ac1-20261004.md)及[旧证据体积风险桥接](current-evidence-risk-bridge-9ac1-20261004.md)分别保留当时来源的实际范围，不代表当前源码的结论，也不扩为完整GUI会计流程、强制中断、主规模切换性能或前台影响。本页只维护未来正式能力边界，不重复维护性能数字和执行计划。

c3c3当时的主120完整资格及实际预览、真实浏览器FAIL19／150见[原组实绩](owner-proof-main120-browser-c3c3-20261004.md)；当时开发包159次CLI及限定MCP48次见[原组证据](development-package-mcp-c3c3-20261004.md)。这些不是当前源码的资格或最终交付，非空资产、完整AI GUI及公司切换的限制仍按原组记录保留。所有旧失败和慢样本保留，测试不跨组累计；阶段状态以主文档为准。

旧24b9两档证据、硬边界、开发包及实际MCP限定整案保持[旧源记录](open-scope-reverse-head-group-20261004.md)的真实范围；此前数据保全／等价oracle／备份恢复风险桥接不扩成f105或69838两档耗时或RSS通过。证据路径、并发、lifetime变化或实际OOM／新峰值时按风险复测。旧FAIL、首次错误、慢样本、私有原件与明确redacted analysis均保留，不转移历史结果或冒充安全raw。完整46项仍分别保留完成、排除、延期及未验。

## 已有机制与历史隔离验证

| 能力 | 已实现与已测范围 | 不能推导的结论 |
| --- | --- | --- |
| 合同、冻结与版本拒绝 | 工厂按明确体系／类别／状态／版本／历史及SQLite保存SQL原文检查。candidate只读生成、freeze独占创建与失败清理、缺合同／DDL漂移／draft与released混版拒绝，在1c37固定源基础组有隔离证据。当前primary仍draft／0。 | 隔离released/1与合成v2夹具不代表当前库已正式发布，不把同SQL改标识当升级。已发布合同不可覆盖，正式选择延期。 |
| 固定content-v1 | stored models／reference declarations、descriptor和源码摘要核对；stored JSON漂移及warm-cache历史语义有回归。1c37主12非空registered released/1核验已完成：30225事实、12413计算、12309凭证、11关账、85证据，四coverage verified／limitations=[]。 | 不能写完全缺非空v1证明，也不能转移为当前源所有规模或最终包历史验收。冻结依据与历史内容继续独立，缺失即拒绝。 |
| 离线前向升级 | CLI直接调用offline_upgrade，不经LocalService；released根、身份及锁来源核对，公司逐库事务、目录最后迁移。合成1→2增量合同、fault／回滚／重试、历史及旧ZIP恢复已在1c37组验证。普通连接不自动升级。 | 不是跨库原子事务，不自动备份，不支持draft→released；合成v2不是生产v2合同或当前真实库迁移支持。 |
| 消费者／核验／维护历史 | 6688正式factory独立组111pass／2warnings／193.73s，完整source inventory前后相同，覆盖消费者、固定v1、production verifier、repair、backup／restore的实际测试范围。 | 不是逐命令全部场景通过；后续signature／资产过滤与新fixture修改不能借此proof宣称已验。 |
| 独立运行包机制 | packager依实际导入源码工厂构建；source／runtime build及manifest需一致，受控Python／SQLite、软件清单／SHA、既存输出拒绝、重定位自检与CLI／MCP辅助逻辑已实现。 | 辅助／mock测试及复制源码CLI不是完整实际包。未来正式包仍须实际入口、锁拒绝、同版upgrade、历史读取及完整自检证据，但不作为本轮正式交付任务。 |

1c37基础组原封12个test／fixture文件、9个实际生产module路径均绑定封存源，完整source inventory before==after；119passed／235.14s、exit0。source SHA1c37c576cb0026b0b39c199b74672a60890c43a7d9e2b944416179fe2bf398e9，manifest SHA845318d7e9857cbae422033a7e8b87a5fb7edd2896f1c5f0617709eabca1c979。原件保存在[基础回归清单](owner-month-reads-release-core-group-20261003-manifest.json)，SHA73347bdde2cd7ed214edfecf897b8115df2b4acf71a525d7c49c7fbeb3ea09ad；runner总墙钟236.2744s与pytest耗时分开。

6688组见[C4／C5记录](owner-close-physical-batch-group-20261003.md)及[111项原件清单](owner-close-reference-group-20261003-manifest.json)。旧候选及撤回见[money／identity记录](owner-money-identity-group-20261003.md)；最新dd86五页与开发包见[读取组结果](surviving-read-consumers-20261003.md)，随后316项及只读审查见[输入与金额核验结果](evidence-reference-and-verified-totals-20261004.md)。当前关系组见[普通开放反向关系组](open-scope-reverse-head-group-20261004.md)，撤回候选见[独立撤回记录](journal-contribution-withdrawal-20261004.md)。所有失败、局部复验、慢样本与旧manifest保持原样，不累计分组通过数。搬移自检和合成v2保留其真实标签，不冒充最终包、生产版本或当前源码验收。

## 未来正式边界与当前运行保护

`data/kernel-released`仅是路径名，当前库实际draft／0。未来若源码工厂切为released，新CLI／daemon的目录格式门禁及Vite重新发现将拒绝现有draft根；旧进程暂时继续不证明可重启兼容。本轮不切源码工厂、不改库指纹／身份、不引入自动迁移或替换根，因此没有需要用户再选择的A／B运行切版方案。

未来正式版需明确合同、固定内容、源及包的一致性，已发布合同不可回写；具体发布及入口保全另按届时用户授权和实际来源设计。保留schema_history、父合同约束、事务、业务／审计／冻结历史与失败回滚能力；不能用当前计算覆盖历史，也不能以已有隔离测试替代真实升级所需的具体结构迁移。

生产完整核验仍须从权威来源独立检查全部覆盖，不能继承旧核验结论。有限浏览器测试副本可明确复用已完成的四项完整资格：必须绑定原核验来源、完整文件SHA及大小，确认WAL为空或不存在，并通过当前源码正常入口的精确结构、身份、状态、历史和外键核对，再实际执行当前preview_close。此时明确标记fresh=false，不写成当前新鲜完整核验；任一来源变化或门禁失败均不能进入计时。资格、预览与计时输出分别记录，前后来源守卫保持。native／SQL工作量、插桩耗时或完成复制不能代替实际浏览器门槛。这是证据接受边界，不新增执行排程。
