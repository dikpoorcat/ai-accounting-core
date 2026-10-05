# 第9阶段：证据引用边界与已验证金额合计

2026-10-04。本轮两组代码已收敛，受影响统一回归一次316 PASS／266.71秒，runner269.1086秒、exit0。12个完整测试文件及9个节点在同次执行中覆盖输入格式58例与非负合计83例，全部kernel Python及相关测试的运行前后SHA不变；不与旧166项相加。当前主仓库源码已不同于dd86；dd86资格、FAIL50／150五页、开发包自检和实际MCP部分验收仅属于旧固定源，不构成本轮代码验收。没有新性能测量、正式冻结、正式包交付或运行入口切换，draft／0与现有5173、资料和身份保留。

## 证据引用输入边界

此前独立实际MCP提交一字符evidence_digest，返回internal_error且无写入，属于已发现缺陷。本组16个生产文件及新增`test_evidence_reference_boundary.py`统一公开引用格式边界：共享`EvidenceDigest`保留原JSON pattern、严格字符串及fullmatch，不trim／小写化／生成来源；格式错误归入invalid_command，合法未知hash保留各入口原有查询错误码。与此无关的ValueError仍分类internal_error。

检查17组字段声明及嵌套来源入口，公开wire基线覆盖29个字段；`prepare_fact_registration.source_locations`改为类型化SourceLocation序列，关闭原Mapping union绕过digest约束的逃逸。内部冻结／原始来源读取器、material私有读取器、exports内部worker converter保留。格式拒绝发生在排序、查询和写入之前；不补造核算事实，不改变金额精度、期间、冻结或内容查找政策。

生成公司DDL `c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb`、目录DDL `b484d315272bb64de3856b458f04d2d4881da05db7552250864260916c69a4ee`与前值及draft／0包合同一致；所有已注册fact JSON schema相同。没有合同重生成或数据库调整。

| 已执行范围 | 原始结果 | 边界 |
| --- | --- | --- |
| 新模块首轮 | 54 PASS／3 FAIL，26.84s | 失败为测试基线：material treatment字段、missing-period路径、JSON list与native tuple位置；首次失败保留 |
| 修正失败项 | 3 PASS／54 deselected，6.87s | 只补跑这3项，不改写首轮 |
| 后发现nested Mapping逃逸 | 1 PASS，6.23s | 此项随后新增 |
| 静态检查 | 修改生产文件及新增测试Ruff通过，scope diff检查通过 | 源结果记录；不替代业务回归 |

58个当前测试案例最初分轮覆盖；本轮随后在316项统一回归中一次执行最终完整模块并通过。包含18种格式／类型错误、后项batch原子拒绝、合法未知、needs_information、幂等／来源历史、嵌套模型与合同。CLI／MCP只调用真实transport wrapper并将ServiceClient替换为隔离合成LocalService dispatch；不是live daemon、真实stdio MCP或网络修复验收。此前真实MCP错误没有在新代码下重放。统一回归覆盖所选入口、核验、备份、冻结历史及关账保护，未执行每个合法业务流程。

## 同次已验证非负金额的重复检查

`integrity._lines`先逐行证明exact int、非负、单边非零、每金额int64，并保存新tuple；合计阶段两侧由逐输入／前缀重复checked改为`source_checked(sum(...))`。非负前缀不大于最终额，Python整数加法不机器溢出。保留所有逐行核验、空／单行短路、借先贷后求值、最终int64及异常文本；不提前累计总额，避免合计溢出遮盖后续坏行错误。

另外两处仅复用同次已经完成的证明：`_check_sources`的actual紧邻来自`_lines`，voucher total比较仍checked最终借方和；`_check_closes`的trial来自`_totals`，其逐行／每账户合计与duplicate_trial_account前置错误保留，跨账户两侧仍checked最终和。移除integrity内部不再使用的source_sum_fen import；全局sum_fen、v1模块及领域合计未修改，不批量推广到允许正负值或没有前置证明的位置。

| _lines合格行数 | 原checked次数 | 当前次数 |
| --- | ---: | ---: |
| 2 | 12 | 6 |
| 10 | 60 | 22 |
| 100 | 600 | 202 |

current与historical_content(1)均覆盖。新增`test_verified_line_totals_work.py`一次83 PASS／5.09s，覆盖opening、类型与形状、MAX_FEN、两侧溢出与后续坏行错误优先级、工作次数、general signed prefix overflow不变、公开核验wrapper、current/v1来源及冻结关账、完整核验与损坏voucher total拒绝。83／5.09s来自保存的专项审查结果记录，未单独归档原始pytest stdout；不伪造log。1688次局部等价probe与CPU时钟量化限制也保留在该审查原文，probe不替代正式测试。没有CPU或五页复测，不宣称500ms达标或端到端收益。

## dd86 HTTP管线诊断：分析摘要，不是原件

诊断始终使用已固定dd86，未测上述新代码。首份input guard误判namespace import，status=diagnostic_failed、0请求。fix1实际17个HTTP请求全部200，两个worker profiles；最终guard仍failed，因为比较了含登录timestamp的owner整行。两次failed原件保留，未改写成通过。独立只读analysis从当前owner行仅替换last_authenticated_at重建原行SHA，精确匹配before；密码hash、credential version、状态、限流等不变，753选定source、book／checkpoint、company文件／WAL／state／repair、身份与合同均不变。正常login／logout、服务退出有据。analysis本身requests_added=0、services_started=0、database_writes=0。

本诊断无active close preview，brief owner_review_request回调保留但返回无请求，与浏览器实际小型active intent分支不同；不是pure timing、资格或浏览器验收。业务guarded read约占brief／report线程cycles的95.1%／96.4%，认证约8–20ms，预热后的响应校验及JSON输出通常亚毫秒。少数首轮response validation超过1ms，不能概括成所有请求输出都小于1ms。inclusive阶段／profile有嵌套不可求和，cycles不等于CPU毫秒，client−worker差值含调度／profile打包，未记录SQLite VM／返回行，不能据profile execute次数冒充SQL工作量。

brief `Journal.account_amounts`、report party_scope及root source proof只读审查已完成：brief 1006个唯一正文各解码一次，后续资金504个来源missing0；report两次贡献读取合计1004次decode已有成功复用，没有已证实的大批可删重复。贡献行点查1995次、self8.548ms，首次resolution生成13.509ms已有复用；集合大小与索引净收益未知。唯一native probe误用unowned `Reports.report`，未覆盖贡献分支，0统计不能解释集合大小或收益。后续现有行贡献汇总复用仍在审查，未实施或承诺收益；不新增跨请求缓存，不减少必要检查。HTTP认证／输出未有足够证据成为主要优化组，不能以此削弱不同事务／来源／冻结边界。

## 证据与状态

[7份公开小型原件manifest](evidence-reference-and-verified-totals-20261004-raw/manifest.json)包含输入组结果、首次失败／修正／nested／Ruff日志、合同检查和金额专项审查；gzip mtime=0，原字节SHA与解压回环核验。拒绝已有目标、不删除原件；原始总量不超过5MiB、gzip不超过512KiB。三份HTTP诊断JSON因含profile／进程／身份数据不归档，仅保存原路径及SHA；上文是明示分析摘要，不能作为原字节回执。私有MCP、credential／PID、DB／WAL、ZIP、raw profile大清单与真实资料均排除，旧归档未改。

[本轮316项原始回归与只读审查／probe的独立7份归档](evidence-reference-and-verified-totals-316-20261004-raw/manifest.json)保存实际py／json／xml／log和审查原字节，逐件来源SHA、gzip mtime0及解压回环均已核对，总量受5MiB／512KiB限制；未覆盖旧归档。源码SHA清单见回归JSON，当前production未freeze。probe仅诊断且贡献分支未覆盖，不是性能验收。

已完成：两组实现、小型／分轮检查及同次316项统一回归、上述只读审查。未验：新固定源完整资格／五页／开发包及实际MCP修复接续、其余六规模和当前证据完整边界。正式冻结／交付／切版延期，第9阶段不标完成，不提交Git。
