# 必要金额读取与正式交付边界

本组仅封存已完成原件、归并不同问题类型，不运行测试、SQL、服务、性能或建库，不读取仍在写的r77金额实验。r76主12纯浏览器150次成功中7次≥500ms（简报4、报表3），性能仍失败；其完整核验与仪表化诊断各自身份见[既有性能清单](owner-r76-main12-performance-manifest.json)，本组不重复归档。正式仓库仍draft／0、未正式冻结或交付最终包。

| 类型 | 实际问题与保留判断 | 未验证边界 |
| --- | --- | --- |
| 同快照事实正文复验 | 固定r76、已核主12每页一次native探针：brief1009、funds507、employees50、assets12均无body-helper跨批同绑定重复；reports1006检查／1005唯一fact，仅1次同kind／period／digest重复。重复是loan_interest的1行期间读取（40 value bytes）及缓存原文SHA，已有raw cache避免scalar正文重读；无child-order SQL。聚合profile不能推出上千事实重复，不据此盲目新增正文成功缓存。 | verify_sources与selected的同ID交集未取得完整binding／scope证明；main48实际交集及净收益未测。单次instrumented native非纯时延验收。 |
| 只物化lines的JSON捷径 | 结果raw SHA与同记录digest一起改动时，摘要相等并非独立原文锚；publication/input/version/seal不额外绑定这份结果摘要。忽略非lines字段会漏掉全层重复key，strict字段类型不能替代loads_unique，因此拒绝直接落地lines-only。 | 重复key触发是静态规则论证，此审计没有执行负例；保留原完整／固定v1 decoder，不造parser或扩大proof权限。 |
| 用贡献证明正常完整行 | contribution只可作为current owned、精确开放月正常owner==basis凭证的候选。仍须两向完整version／line集合、唯一连续line_no、整数单边／平衡／total及完整账户对monthly_account对照；review、冻结adoption、reversal、clear与withdrawn保留各自来源核验和fallback。 | 既有r73报表1004贡献约3,059,132B为历史代价，不能当r76新测量；减少Outcome解码同时增加贡献／parent／证明工作，净收益未验。逐条幸存line匹配或SUM不能当完整行集证明。 |
| 资产采用scope | 实际为2个full与1个adopted-only；141个_read_accounting slice中_family47，既有共享未搬三份family。A/B静态候选有包含关系，但分页／空页／fallback消费不同，C另含opening／disposal等身份。 | SQL记录无各次subject参数；缺独立physical voucher→subject分区，不能按联合adopted IDs强行派生子scope。联合读取收益未验，不宣称全部问题已排除。 |
| 正式v1／upgrade／最终包 | 已有包内production bundle、离线CLI、exclusive freeze、全根只读预查、同resident锁内重验、公司先目录后及partial retry、事务source/target和历史保留核验、固定v1历史解码与实际包自检入口。只读审计未确认新的立即生产修复缺陷。 | r73隔离六文件50项／109.62s是原源码回归，非最终源码／最终ZIP通过；synthetic v2不是正式已发布升级支持。最终合同／fixed-v1 descriptor与当前candidate匹配、copied CLI及正式包解释器／launcher／native窗口等仍按真实执行门槛验收。 |

CLI升级不启动服务，正式bundle拒draft和混版，原未完成operation须先由原安装版处理；这类职责不为省预查成本退出。开发skip flag有明确不可交付限制，本身不是已证实缺陷。最终包须全新不存在目标、无SkipValidation、validation非null且passed、目录／公司released／1、packaged_offline_upgrade非null，并保存实际命令和来源摘要；尚未取得这些最终回执。

旧只读fact审计提出的成功正文proof只是候选；后取得的实际同绑定交集仅1，当前决定以实测范围为准。未来若扩大，仍须先确认精确binding／snapshot交集、失败原子撤回、冲突绑定拒绝以及current／unowned／fixed-v1隔离，不把正文proof升级为来源图、publication、冻结采用或完整结果权限。

原件与分时source绑定见[本组manifest](owner-r77-money-and-release-audit-manifest.json)。探针固定r76 source、已核输入与loaded-module SHA来自原JSON；资产profile属于r75，金额贡献工作量引用r73，release审计为当前静态代码，四者不得混为一个已验候选。所有原文件保留，gzip逐字节还原；旧失败、慢样本及既有archives不改。
