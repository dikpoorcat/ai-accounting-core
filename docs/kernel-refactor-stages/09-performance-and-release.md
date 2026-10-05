# 第 9 阶段：老板看板与开发库性能收口

## 当前状态

**本轮开发范围完成，代码、测试与文档随本阶段提交：规定主规模及独立分布热刷新已达标，按需功能、新开发包、资源诊断与独立只读补证已完成。** 本轮保持目录／公司draft／0，现有5173、资料根及身份原样；正式合同冻结、正式包交付和运行入口切换延期，完成范围及未验边界见下文，本次提交不表示正式发行。

[c063三主规模](09-performance-results/current-three-scale-browser-c063-20261005.md)12／48／120月共450次有效热刷新全部<500ms，最大456.2ms，实际退出0／0／0；[四分布](09-performance-results/current-four-distribution-browser-c063-20261005.md)独立三档450次全部<500ms、最大396.9ms，压力150次读取成功／120次超线、最大1695.0ms，仅诊断不承诺500ms。资格fresh=false，有限复用525f完整Q，当前精确正常边界与实际preview分别成立；不将旧完整Q改成当前新Q。a5、525f、8c98各自旧窗口、冷态、慢样本和首次失败保留，不跨窗口归因单个改法。

[主120按需与复制权限修正](09-performance-results/on-demand-and-copy-permissions-c063-1de-20261005.md)分别绑定自身来源：c063 Python v5／CJS v7实际37项操作、75响应、159 HTTP全部完成，根exit0，前后守卫及正常撤销成立；v1–v4和v5首次遗漏执行参数的失败保留。生产copy WAL／SHM权限修正收敛为1de七模块147passed／2平台skip、实际exit0；pre-fix和中间工作区未冻结来源明确标null，不假冒c063或1de固定源码验收，也不将c063450计时改名1de。

[1de新开发包](09-performance-results/development-package-copy-sidecars-1de-20261005.md)默认构建／重定位自检实际exit0，161 CLI／4213文件，全部源码、历史v1和合同守卫成立；新包runtime非空恢复桥19 MCP实际exit0，执行fresh完整核验、备份恢复及完整冻结payload比较，正常关闭。首次误用仓库Python的前置断言失败保留，不推测161与旧159调用差异原因。c063旧159 CLI／19 MCP包和另10 MCP正常同根接续、a5两公司280 MCP整案各按自身来源保留，不合并计数或补写旧整案未办理业务。

240／1536MiB现有合成原件均完成准备及三次当前完整核验。240四API和45次诊断前台刷新已完成，只读postcert v2实际exit0；原v4整体验证exit1因把合法新增backup三记录纳入整库历史不变比较，原失败保留。1536四API与独立第四前台补测完成，只读postcert v1实际exit0；原marker及独立补测末尾JSON序列化失败的exit1保留。[资源组](09-performance-results/resource-foreground-copy-permissions-1de-20261005.md)分别记录原窗口、独立补测与只读补证，不改写原失败终态。c063 ACL、1de旧owner凭据已退役导致认证失败、v3错误要求已提交WAL为空等失败链保留；不重新设置旧身份或以准备代替测量。

定稿五页、会计凭证、税务局模板与局部刷新保留，已退出专用装载不恢复。真人密码窗口、强制崩溃、详情自身inflight、整轮AI GUI及Windows完整外部子树不因正常补验或包自检而显示通过。没有新改动、失败或风险的已验证组不机械重跑。

## 既有结果与归档

各行仅对自身源码、合同、分布和执行窗口负责；不同来源、重叠套件和局部补跑不合并数字。原生、TEMP影子、插桩或并行诊断不代替浏览器500ms。完整SHA、分段样本、首次失败及私有原件索引留在独立档案。

| 来源／组 | 已知结论 | 边界／回退 | 归档 |
| --- | --- | --- | --- |
| a5d4三主规模实际浏览器 | 12／48／120月各150成功，450次全部<500ms，最大456.7ms；完整资格、实际预览及正常退出通过 | 冷态超线保留，单公司切换unavailable；不替代525f结果、剩余分布或阶段完成 | [三规模结果及原件](09-performance-results/current-three-scale-browser-a5d4-20261005.md) |
| c063三主／四分布与当前开发组 | 三主450有效全部<500ms、最大456.2ms；独立三档450全部<500ms、最大396.9ms；压力150读取成功／120超线、报表最大1695.0ms、资产454.9ms。321回归／五页work、默认159CLI／19MCP桥及正常同根10MCP分别通过 | 浏览器有限复用525f完整Q、fresh=false，当前正常边界与实际preview成立；压力诊断worker声明over_target退出1／根0，owned正常退出范围保留；旧失败及跨窗对照不回写。按需功能已另组通过，资源诊断与只读补证已完成，正式延期 | [三主](09-performance-results/current-three-scale-browser-c063-20261005.md)、[四分布及失败链](09-performance-results/current-four-distribution-browser-c063-20261005.md)、[修正](09-performance-results/current-summary-lifecycle-c063-20261005.md)、[开发包](09-performance-results/development-package-summary-lifecycle-c063-20261005.md)、[正常接续](09-performance-results/current-package-normal-restart-c063-20261005.md) |
| c063按需／1de复制权限 | 按需37操作／75响应／159 HTTP及父守卫实际exit0；固定1de七模块147passed／2平台skip、实际exit0 | v1–v4、v5遗漏执行参数、pre-fix与中间来源null、validator锁冲突及snapshot导入失败保留；功能不套500ms，不将c063计时改名1de | [按需与权限](09-performance-results/on-demand-and-copy-permissions-c063-1de-20261005.md) |
| 1de最终开发包／非空MCP | 默认161 CLI／4213文件、新runtime19 MCP完整核验、备份恢复及冻结payload比较实际exit0；源码与正常关闭守卫成立 | 首次误用仓库Python前置失败保留；调用计数不跨包相加。真人窗口、强kill／inflight与正式发行未由本组证明 | [开发包](09-performance-results/development-package-copy-sidecars-1de-20261005.md) |
| 1de240／1536MiB资源 | 两档准备及三次当前完整Q已验；240四API／45诊断刷新与只读补证完成；1536四API与独立第四前台补测完成，两档只读补证实际exit0 | 240原历史摘要错误比较、1536第四marker及原ACL／认证／WAL前提失败均保留；有限RSS不作绝对峰，诊断不套500ms，原runtime exit1与只读补证exit0分别保留 | [资源组](09-performance-results/resource-foreground-copy-permissions-1de-20261005.md) |
| 8c98读取组及三主纯计时 | 两src／两test固定，定向36项及实际五页业务／来源守卫通过；450有效、448次<500ms，48报表516.3ms／120资产637.6ms各一次超线，无技术失败，实际工具退出0／1／1 | 复用525f完整核验、fresh=false，新实际preview与精确边界守卫通过；宽首轮336／2预期错误及修正保留。严格未通过；后续c063两修正已统一321项／五页work验证；c063新三主450全部通过，不向8c98回写通过，不单点归因或解释GC | [读取组](09-performance-results/current-reader-group-8c98-20261005.md)、[真实计时及原件](09-performance-results/current-three-scale-browser-8c98-20261005.md) |
| 525f主120接口／资产行跨度 | 五页HTTP pair／profile／池工作量完成，API和单次资产均diagnostic_complete、业务守卫成立，实际exit0；owner113.91ms、prime93.10ms | 新host／首次／插桩不是500ms或旧host GC归因；两旧helper失败保留，238slice不同作用域；后续8c98读取组及其450次纯计时另记，不将新host诊断解释为GC根因 | [诊断及有限原件](09-performance-results/current-main120-api-cost-525f-20261005.md) |
| 525f三主规模实际浏览器／计数恢复 | 450有效样本，447次<500ms、3次超线；120月简报554.5ms、资产516.0／517.1ms，无技术失败；三组完整资格、实际预览、退出及来源／业务守卫成立 | 该525f来源严格500ms未通过；a5独立结果不替代。共享voucher_version映射已修，恢复不重Q，原失败、冷态及慢样本保留；后续补充门禁与主严格结果分别记录 | [真实结果及恢复链](09-performance-results/current-three-scale-browser-525f-count-recovery-20261005.md) |
| a5d4复制／摘要与开发包 | 未发布目标先避免全量WAL，首步复用同事务保留摘要；279相关回归、159CLI包自检通过 | 活动库WAL不改，第二步及后核验保留；access violation、合同fixture失败、ce54构建前cache拒绝均保留；包不是正式发行 | [复制与摘要](09-performance-results/unpublished-copy-and-retention-20261005.md)、[开发包](09-performance-results/development-package-a5d4-20261005.md)、[合成存储清理](09-performance-results/test-storage-cleanup-20261005.md) |
| 三主规模实际迁移／独立MCP | 12／48的a5迁移、120的ce54迁移均保留身份、原表、state、历史、FK与严格checkpoint；两公司实际MCP及备份恢复完成 | 各来源分别记录，迁移不是新Q或性能；280含失败和读取，正常stop/resume不是强杀，工资付款及甲10月完整关账未验 | [12／48迁移](09-performance-results/synthetic-index-transition-a5d4-20261005.md)、[120迁移](09-performance-results/synthetic-index-transition-main120-ce54-20261005.md)、[独立MCP](09-performance-results/independent-mcp-a5d4-20261005.md) |
| ed96→a5实际读取池工作量 | 五页业务全等、双方七守卫通过；资产／报表VM下降，SQL／行／字节／解码未降 | 首次私有observer失败保留，插桩不是500ms；238资产slice分属完整owner与adopted-only作用域，不能当成全部重复 | [读取池对照及原件](09-performance-results/five-page-pool-work-v2-20261005.md) |
| 独立12月复制／525f调整 | 源守卫、三次四覆盖内容核验及247原表摘要、身份和历史保留通过 | 50个对象、0员工的独立分布不替代主人员规模；首次导入失败保留，后续c063实际preview及150次浏览器全部达标，有限复用资格另记 | [实际执行及原件](09-performance-results/independent12-copy-upgrade-525f-20261005.md) |
| 525f完整核验快照组 | 六文件修正；15模块首轮248通过／1失败，修正旧测试hook后仅迁移模块28通过；外部提交隔离、回滚、历史decoder及非空工作量反例通过。新包默认161次CLI和19次真实MCP核验／备份恢复桥接通过；三主规模各自完整资格及实际预览完成 | 新37项是首轮子集，不累计；五页热路径未进入新增wrapper；首次私有桥脚本失败保留，不拼调用数。525f浏览器450次另组已执行且有3次超线，a5不继承修正 | [完整修正](09-performance-results/verification-snapshot-boundaries-20261005.md)、[新包与桥接](09-performance-results/development-package-verification-snapshot-525f-20261005.md) |
| ce54／ed96索引、报表与稳定guard | 精确采用索引、JSON对象编码及SQL准备程序复用有定向／工作量证据 | 业务证明不跨快照缓存；六个PRAGMA仍编译；原生窗口和TEMP影子不能证明整页改善 | [索引与报表组](09-performance-results/adopted-index-and-flow-groups-20261005.md)、[稳定guard](09-performance-results/stable-owned-read-guard-20261005.md) |
| 9ac主120正式浏览器 | 150成功、0断言错误、17超线；简报／资金／员工／资产／报表最大518.2／394.7／416.7／618／677.4ms | 全样本、ResourceTiming及冷态保留；约5.89GiB仅部分资格窗口观察，不是全生命周期峰 | [浏览器及原件](09-performance-results/current-main120-browser-9ac1-20261004.md) |
| 9ac短HTTP／GUI／资源桥 | 真实daemon准备后的短HTTP诊断；61布局与9切换分支；240MiB／1.5GiB核验、备份、恢复、再核验完成 | 新host不是原长准备host；GC最长约22ms不能解释全部超时；两员工GUI、无关账大BLOB轻样本不覆盖主规模、前台或全树RSS | [生命周期](09-performance-results/current-http-lifetime-diagnostic-9ac1-20261004.md)、[GUI](09-performance-results/current-dashboard-navigation-9ac1-20261004.md)、[资源桥](09-performance-results/current-evidence-risk-bridge-9ac1-20261004.md) |
| 9ac／c3旧包与浏览器 | 9ac包及限定46次成功MCP有独立实绩；c3主120仍有资产19次超线 | 旧包/MCP不借给a5；时间归一化helper过宽，原始仅四处动态时间差异；同harness不同窗口不证明稳定因果 | [9ac包/MCP](09-performance-results/development-package-mcp-9ac1-20261004.md)、[c3浏览器](09-performance-results/owner-proof-main120-browser-c3c3-20261004.md)、[c3包](09-performance-results/development-package-mcp-c3c3-20261004.md) |
| 807／bfb／8473成员、头与目录 | 必要成员/头保护及精确key复用保留；描述遍历119186→59847、目标哈希12119→239 | 旧native未执行daemon静态准备，旧GC／Lark对象图不是actual browser根因；daemon对齐原生也不等于整页达标 | [member](09-performance-results/frozen-member-current-guard-20261004.md)、[头绑定](09-performance-results/voucher-header-binding-20261004.md)、[目录key](09-performance-results/directory-key-lookup-20261004.md) |
| 731稀疏目录负实验 | 实体化减少但资产原生中位反而392.25→442.54ms，已撤回 | regex walk仅推测；扩展专项未验证；不采用descriptor两原型，不关GC、不加跨请求业务缓存 | [撤回及环境校准](09-performance-results/compact-directory-withdrawal-20261004.md) |
| 24b9两主规模与资格 | 主12／48各150成功但分别10／28超线 | 资格／preview耗时不是500；切公司unavailable、sidecar/SHM及外部fix2限制保留；其余五规模未验 | [整案](09-performance-results/open-scope-reverse-head-group-20261004.md)、[两规模原件](09-performance-results/open-scope-two-browser-final-20261004-raw/manifest.json)、[完整资格Q/R](09-performance-results/open-scope-main48-qualification-20261004-raw/manifest.json)、[首次CLI路径拒绝](09-performance-results/open-scope-browser-cli-preflight-20261004-raw/manifest.json) |
| 24b9硬边界／资源／MCP | 20MiB、10万行、5000事实边界及两档大原件流程、限定MCP有实证 | 100001仅inspect拒绝、5000未preview/publish；轻样本非主性能；GUI未覆盖、乙工资/申报/关账未完成，恢复ZIP内running快照不改成功；私有失败helper保留 | [范围及硬边界](09-performance-results/open-scope-reverse-head-group-20261004.md)、[两档原件](09-performance-results/open-scope-evidence-resource-final-20261004-raw/manifest.json)、[MCP原件](09-performance-results/open-scope-mcp-final-20261004-raw/manifest.json) |
| fa5c／dd86及更早消费组 | 旧主48各150成功但58／50超线；冷态、首次与慢样本保留 | 资金first误等同浏览器explicit账户已纠正；MCP partial及撤回候选不继承，资格wall、native工作下降不证明CPU/RSS/整页收益 | [四类保护](09-performance-results/owner-four-read-groups-20261003.md)、[fa5c原件](09-performance-results/owner-four-read-groups-fa5c-main48-package-20261003-raw/manifest.json)、[保留消费](09-performance-results/surviving-read-consumers-20261003.md)、[输入与金额](09-performance-results/evidence-reference-and-verified-totals-20261004.md)、[journal撤回](09-performance-results/journal-contribution-withdrawal-20261004.md) |
| 页面定稿、金额与期间 | 专用装载／详情／缓存退出，全量金额、真实来源及精确期间保留 | 已定业务不恢复；拥有者、固定身份与真实撤去保护保留 | [money/identity](09-performance-results/owner-money-identity-group-20261003.md)、[金额摘要](09-performance-results/owner-money-summary-group-20261003.md)、[定稿](09-performance-results/owner-final-ui-baseline-20261003.md)、[月份发现](09-performance-results/owner-posting-period-discovery-group-20261003.md)、[精确月](09-performance-results/owner-month-selection-group-20261003.md)、[r68](09-performance-results/owner-required-groups-r68.md)、[r69](09-performance-results/owner-required-groups-r69.md) |
| 展示／权威来源与同次消费 | 展示与完整证明分工、范围/manifest/权限守卫去重、批量装载已有组证据 | 派生目录不证明采用；ordinary/full各自保留来源、正文、未知、冻结采用与金额 | [r71展示](09-performance-results/owner-display-read-scope-r71.md)、[r72发布](09-performance-results/owner-publication-proof-reuse-r72.md)、[资产](09-performance-results/owner-asset-repeated-reads-r72.md)、[148d消费](09-performance-results/owner-current-consumption-group-20261003.md)、[176范围](09-performance-results/owner-integrity-repeated-ranges-20261003.md)、[authority](09-performance-results/owner-authority-read-groups-20261003.md)、[C4/C5](09-performance-results/owner-close-physical-batch-group-20261003.md) |
| 旧公共读取、包接续及资格修复 | 按原合同与源保留实绩 | 不合并计数、不向当前继承，正式历史能力隔离验证不代表正式发行 | [资格/浏览器](09-performance-results/owner-current-consumption-main48-harness-repair-20261003-raw/manifest.json)、[e0e读取](09-performance-results/owner-draft-runtime-query-group-20261003.md)、[旧包](09-performance-results/owner-draft-package-runtime-group-20261003.md)、[未来正式能力](09-performance-results/release-remaining-gates.md)、[r73历史](09-performance-results/owner-r73-content-upgrade-history-manifest.json) |
| 旧局部无收益与生命周期实验 | 请求、准备、对象释放及按需路径已有独立排查 | 无收益只适用于当时调用/计划/分布；不碰运气重复，不将理论候选写成已排除 | [请求/准备](09-performance-results/owner-http-request-and-readiness-r73.md)、[r74](09-performance-results/owner-r74-root-cause-audit-manifest.json)、[r76](09-performance-results/owner-r76-lifecycle-and-prior-r75-manifest.json)、[r77](09-performance-results/owner-r77-main12-round.md)、[r78](09-performance-results/owner-r78-review-on-demand.md) |

## 验收口径


主样本为单公司 50 人、每月 1,000 笔业务，分别覆盖 12、48、120 个月；另保留独立业务与累计依赖分布，以及 200 人、每月 5,000 笔压力样本。工资累计、普通收付、跨月清偿、备用金、资产、税务、修订、闭期更正、正常资料处置和明确报表依据均须保留。管理事实、资料事实、修订和自动派生结果另计，不充当业务笔数。

样本须有开放月、冻结月及有效关账预览。构造完成、raw-copy、逐表日志或结构元数据更新均不等于内容 verified；浏览器计时前由构造器之外的注册核验入口完成完整核验，覆盖 sources、historical_adoption、projections、read_indexes，记录实际限制。资格核验可以在浏览器服务进程内先完成，随后才预热和计时；不能复用构造器自证，也不能让核验与纯计时并行。生产业务门禁不能使用生成器的延后核验选项。

三主规模525f已经分别完成新的完整注册核验和实际预览，不改其流程。四个后续隔离副本的迁移也使用同版注册核验，分别检查写锁前来源、锁内来源和目标。其测速准备允许复用真实目标证明，避免第四次相同业务扫描；必须先在提交后的正常Store快照内核对目录绑定、完整结构、身份、状态和历史，以及SQLite integrity／外键，并确认完整主文件摘要、大小和空WAL／journal仍与成功迁移回执一致。父进程持有该副本的服务独占锁，实际登录、预览和计时前后守卫保持；记录明确区分原目标核验耗时与本次提交后检查，不伪造新完整核验。8c98三主规模也已在相同完整主文件SHA、空sidecar及精确DDL／身份／状态／历史／FK边界成立后有限复用525f完整核验，fresh=false，并重新执行当前实际preview；不冒充新完整核验。这些复用只用于明确绑定的有限测试副本，不是生产或页面跨请求缓存。有限复用模式已完成无数据库导入检查和独立只读审查，v3接入修正后的共享计数门禁；c063四分布当前正常边界与实际preview均成立，独立三档450次全部达标，压力150次成功／120超线仅诊断；两次启动／release失败及正常owned退出链见[四分布档案](09-performance-results/current-four-distribution-browser-c063-20261005.md)。这些当前检查仍是fresh=false，不改写为新完整Q。

| 度量 | 固定口径 |
| --- | --- |
| 五页热态 | 本机正式前端构建及隔离常驻服务；每页预热后记录 30 次，从点击刷新到上下文、主数字、默认 **20 条**明细及全部可见附属内容渲染完成。全部成功样本须 **<500ms**，保存中位、p95、最大及逐次样本。 |
| 默认业务范围 | 保留全量业务汇总、必要业务结果与明确老板待办；独立月度核对模块及其专用请求／缓存已退出，不作为页面默认内容或按需模块恢复。AI／MCP同版核对合同、原生批准窗口摘要、完整准备、关账和冻结信任链保留。来源证明与技术诊断不进入老板默认读取。 |
| 单列范围 | 冷启动、首次打开、切公司、业务详情、事件及文件任务分别记录；未测或 unavailable 不显示通过。 |
| 隔离与失败 | 纯计时不与建库、回归、完整核验或插桩并行；HTTP／dispatch 插桩须显式启用并标诊断。错误响应不算完成，慢样本和首次失败不删除。 |
| 身份与环境 | 保存固定源码清单、构造／核验来源、合同、静态产物及摘要。当前环境为 Windows、Ryzen 7 3700X、Node 24.19.0、仓库 Python 3.12.13／SQLite 3.53.1；最终报告以实际环境为准。 |
| 工作量 | 分别记录 SQL、SQLite VM、返回行和值字节、事实／结果解码、采用证明及内存。返回字节不等于磁盘读取，Python 分配峰值不等于 RSS，服务树峰值必须同时间采样。 |

小样本、原生调用、并行诊断与局部最快结果仅用于定位。旧默认 100 条及完整准备的报告保留原口径，不能冒充新看板验收。浏览器默认只用 `src/ai_accounting/static/dashboard` 正式构建，缺失即失败；`frontend/dist` 只供显式诊断。

一次问题发现触发一次同类排查；一组相关修改收敛后固定源码成组验证。部分复跑不改写首轮整组，重叠套件不相加；没有新改动、失败或风险的已验证组不机械重跑。

旧性能实验只说明当时的结果。引用前先核对页面职责、响应内容、默认读取范围、数据分布、源码及执行计划；这些条件明显改变后，原结论不能直接变成“已排除”或“不再重试”。重新试验须有当前调用链与工作量依据，作范围明确的新旧对照，同时保留原负面结果；条件没变则不为碰运气重复测。资金汇总窗口排序此前在旧路径未见稳定收益，老板看板精简后的当前路径尚未重新验证，本轮不重试。明确的产品排除、审查第24项回退及信任链边界不因此放开。

## 当前收口与延期事项

| 问题类型 | 当前判断与处理 | 收口状态 |
| --- | --- | --- |
| 测试副本堆积与磁盘耗尽 | 根据构造和复制记录退役已确认旧合成数据库；新独占副本避免全量WAL并严格checkpoint，活动库不改。生产复制路径进一步保护正常打开后WAL／SHM权限 | [既有清理](09-performance-results/test-storage-cleanup-20261005.md)释放196.062 GiB，a5统一279项保留原范围；[1de权限修正](09-performance-results/on-demand-and-copy-permissions-c063-1de-20261005.md)七模块147passed／2平台skip。真实磁盘满、模式切换失败和外部写者并发未验证 |
| 同一升级事务重复扫描全表保留摘要 | 仅首步复用已有基线；每步后核验、第二步新回执／新表及最终内容保持；正式升级、恢复的不同边界不合并 | 实际扫描、三处数据损坏回滚及统一279项通过；大库ce54仍保留原执行器，不把减少一遍扫描写成已有大库耗时收益 |
| 完整核验缺少同一读取快照 | 全局检查注册核验、直接完整核验、备份／恢复和两类迁移；无调用者事务时自建正常读事务，已有读写事务只借用；预验、写锁内复验、目标核验分别保留 | 已知失败修正并定向通过，外部并发提交不会混入同次核验；非空工作量反例及五页不进入新wrapper验证通过。525f开发包、小型MCP内容桥及三主规模各自完整资格／实际预览通过；525f旧窗450次有3次超线；当前c063三主450次及独立三档450次全部达标，压力另列诊断，不单点归因 |
| 精确SQL定位仍命中累计无关引用 | 两处候选定位使用直接采用部分索引，原身份、类型、路径、发布和来源核验保持；旧path-first两个拓扑负面结果保留 | 三主规模真实迁移通过；120月实际五页业务全等，资产VM下降11.5%，行及解码量不变。包含本组的a5浏览器450次全部达标；不单独归因索引，525f旧窗450次有3次超线；当前c063三主与独立三档各450次达标，不单独归因索引。首次统计脚本失败保留 |
| 汇总只需判定却重复解析JSON字段 | 正常唯一字符串来源使用对象key/value，重复、特殊身份及编码保留原退路；完整核验和固定v1仍独立 | 本组87项定向及固定ce54八模块146项通过；套件有重叠不相加；包含该改动的当前c063三主与独立三档各450次达标，压力诊断保留，不单独归因 |
| 连接已复用但SQL反复编译 | owned快照固定安装事务保护，私有BEGIN／ROLLBACK不进入公开语句缓存；通用及fixed-v1路径保持，成功数据及proof不跨快照 | 91项组及后续22项模块通过，编译工作下降；包含本组的a5三规模整页通过，525f旧窗450次有3次超线；当前c063三主与独立三档各450次达标，不单独归因编译复用 |
| 同快照内遍历目录找key、重复目标哈希 | 精确目录索引及唯一key哈希复用已保留；119186→59847描述遍历、12119→239目标哈希，必要正文与摘要不减 | 本组工作量已有证据，包含本组的a5三规模整页通过；525f旧窗450次有3次超线；当前c063三主与独立三档各450次达标，压力仍以真实超线诊断记录，按需功能已通过，资源诊断与只读补证已完成 |
| 稀疏解码分配 | 731候选未见实际性能收益，已撤回；完整walk与失败样本保留 | 不继续采用此候选，不把结论扩大为所有解码方案已排除 |
| 长准备后的对象生命周期 | 本轮静态确认_private_overlay递归closure自引用环持有prior叶map；改显式stack，weakref验证立即释放，统一321项回归通过。五页、QueryReads、snapshot、pool及相关proof链同类审查保留必要存活，不冻结业务或关闭GC | 已确认的是closure延迟释放；8c98慢样本的GC原因仍未证实，新host观察不证明原host生命周期或耗时收益。c063三主严格450全部通过，跨窗口仍不证明单项wall收益，见[当前修正](09-performance-results/current-summary-lifecycle-c063-20261005.md) |
| 实际接口耗时及等待 | 525f五页HTTP／profile／池工作量和资产跨度已归档，owner prime93.10ms，merge／update／return<0.1ms；8c98 overlay展平及checkpoint复用已有定向／工作量守卫 | 8c98真实450次有2次超线；后续c063汇总及临时closure已通过321项／五页work，新三主450全通过，保留跨窗口限制，不推定单项收益，不将GC视为637.6ms已证实根因。见[525f诊断](09-performance-results/current-main120-api-cost-525f-20261005.md)、[8c98计时](09-performance-results/current-three-scale-browser-8c98-20261005.md) |
| 身份枚举与重复源头读取 | 不直接删除资产generic activation lane：它仍检查current member重定向、孤立未采用member及owner缺失。238个slice分别为119次完整owner和119次adopted-only头，已在同快照共享部分核验；不同作用域不能直接合并 | 当前单次行跨度业务全等且守卫成立；两组完整读取不属全重复。常量for_close重复检查不产生SQL／解码；窄身份头另有批量读取候选，约12,300 VM、47,124字节，不是主要成本。逐ID交集未记录，未实现或计量收益，保护保留 |

完整全范围到显式subject子集的证明复用已再次核对当前调用链：页面只传fact_ids时子集为空，现已在SQL前返回；非空子集核验要求119999截止，当前页面月份证明不覆盖。没有实际重复读取收益证据，本轮不改，不把理论可能性写成已经排除。其余未验证候选保持未验证。

当前已收敛c063主／独立热刷新、主120按需功能，以及1de复制权限修正和最终开发包／MCP恢复桥；实际数字与来源见上表，不累计重叠套件，不回写历史失败。240诊断与只读补证已完成，1536独立第四前台及只读补证已完成；两档只读补证实际exit0，原资源和独立补测runtime exit1分别保留。旧硬界限与原整案未办理业务保留原范围，正常接续不代表强制崩溃或真人窗口。

46项1–45及10b、编号外事项保持各自完成、保留、回退、排除、延期或未验去处：18／19不新增入口分类或近期变更列表，24负面回退不重试，27已排除；定期备份、跨公司配对及正式发行继续延期。本轮开发范围完成，代码、测试与文档随本阶段提交，draft／0和真实5173、资料、身份保持。

## 本轮完成范围与交付边界

开发库代码、受影响业务保护、合同生成／页面构建、规定规模性能、按需功能、复制权限、最终开发包及两档资源诊断／只读补证已完成；代码、测试与文档随本次提交交付，必要内容及未知状态保留。正式冻结／正式包／入口切换本轮延期。逐项核对46项和编号外，分别保留完成、保留、排除、延期及未验状态，不宣称全部完成；旧候选或局部补跑不能替代当前证据。本次提交的准确Git身份由实际提交记录给出，不预写哈希。
