# 第9阶段：开放范围普通凭证反向关系复用

2026-10-04。本组已实现，独立静态审查无阻断发现，一次定向91 PASS／67.51秒及Ruff通过，随后固定24b9受影响统一回归一次345 PASS／768.53秒，runner774.225662秒、exit0，所选源码／测试SHA不变；这些耗时为测试耗时。仅修改`query_reads.py`与新增定向测试，未改变DDL、固定v1、前端、服务、数据库或正式发布状态。旧316项成功回归属于修改前源码，不继承或与本组345项累计；独立审查未自行重跑实施组测试。

owned current开放范围SQL已实际返回publication、current calculation、fact及physical voucher头，并通过末端NOT EXISTS证明无successor。普通owner=head此前在完整source与forward关系检查后，又独立读取同一calculation_current与publication头；本组只复用已经成立的同快照普通反向关系。review、冲正、撤去、清零、相反方向missing-source查询、冻结采用与完整结果正文保留原检查。公开／unowned、显式subject子集、固定v1、完整核验、关账、repair与备份恢复仍按各自独立入口运行。

普通receipt只含原`id + CONTENT_FIELDS + has_successor=0`，不暴露scope辅助字段。普通signature冲突先拒绝；普通pending全部组装后执行独立review／reversal整批，成功后才登记普通receipt，失败不发布本组普通成功且保留之前独立成功。没有生成saved input、source content或adoption完整成功证明，没有跨请求缓存。

## 一次只读工作量对照

before源`68964762c4266280c01c47756ac935cf28dfc992e9bf0032fb5bc6d123492c61`与candidate源`24b9b8b65723cee8c6982b1f41235dce380e62788db33f100176f92c714571fc`各756文件，文件清单仅`query_reads.py`不同，新测试在两个snapshot相同。before manifest的reconstruction明确记录从candidate复制后，用保存的316回归前组`query_reads.py`精确原字节替换；它是单变量对照源，不是再次运行316的回执。对同一主48合成样本六入口各执行一次严格只读插桩，无预热；context工作量相同。五页结果如下：

| 入口 | SQL次数 | VM before→after | 返回行 before→after | 返回值字节 before→after |
| --- | ---: | ---: | ---: | ---: |
| brief | 193→193 | 868000→836900 | 16338→14332 | 7507561→7219039 |
| funds_first | 171→171 | 1227300→1196200 | 11370→9364 | 3432819→3144297 |
| employees | 98→98 | 782600→751600 | 6975→4969 | 4227716→3939194 |
| assets | 251→251 | 569300→538100 | 8408→6402 | 6020686→5732164 |
| quarterly_report | 255→255 | 948200→917100 | 24869→22863 | 10684375→10395853 |

每页少2006返回行、288522返回值字节与31000～31200 VM指令，VM按100指令采样。SQL次数未减少，因为独立review／reversal仍需要原查询；完整结果load、结果正文字节与所有decode计数保持不变。返回值字节不是磁盘读取量；插桩耗时、Python分配峰值均不作为纯延迟／RSS或500ms验收。

六入口`business_response_sha256`相同：仅归一化原响应clock（top generated_at／checked_at及data.generated_at）和**精确top-level `read_context.read_version`**，后者绑定源码build；其余所有业务字段，包括金额、业务版本、分页、IDs与状态原样比较。context原source-bound SHA也相同，五主响应原SHA不同，不能写全文hash相同或不带边界的完整响应等价。该明确归一化只属于本组新helper，不回写此前贡献候选的完整等价未验结论。

两份诊断均完成，前后文件SHA、身份、state、schema_history、登记／repair守卫与source inventory全部不变。旧qualification只用于确认既有同一合成样本；本组不是资格、pure timing、HTTP、浏览器或active preview验收，funds_first不等于实际selected-account热刷新。

## 验证与原件

一次91项覆盖新关系复用、既有physical head、publication snapshot与v1 voucher head；定向新例保留普通／独立receipt相同、缺来源反向检查、review／replacement／clear／withdraw／reversal、当前／发布／subject损坏重复失败、混合独立失败不留普通pending、早先成功保留与signature冲突先拒绝。91 PASS／67.51秒及Ruff来自实施组工具输出与保存审计：测试session70220最终tool stdout为91 passed in 67.51s，Ruff工具stdout为All checks passed!；没有本91持久stdout／XML／receipt，不构造日志或为造日志重复运行。91项定向本身不提供全仓回归、资格、浏览器、开发包或实际MCP验收；随后345受影响回归与同源开发包另列，不与旧316、166或撤回候选79项相加。

[独立原字节归档manifest](open-scope-reverse-head-group-20261004-raw/manifest.json)保留本组prepare／work脚本、before／after／comparison JSON、审计及独立review、包含定向结果记录的实施审计及两源manifest，不打包全源码。逐件审查实际内容，合成标识／路径与source-bound read version保留，身份guard仅SHA；SQL模板无绑定值与结果正文，未包含PID、凭据、认证令牌或真实私有资料。gzip mtime0，原字节SHA和解压回环核对，总量≤5MiB／512KiB，不覆盖旧原件。

## 固定24b9受影响回归与开发包

受影响统一回归一次345 PASS／768.53秒，runner774.225662秒、exit0、files_unchanged=true，固定源SHA仍为24b9。同次选择18个完整文件＋9个节点，覆盖看板／资金／员工资产／老板展示／contract／bounded与deferred读取、业务查询、身份／凭证／季度报表、reports／projection、QueryReads／snapshot／integrity及本组新关系测试；9节点覆盖来源损坏／repair边界、实际备份恢复与损坏拒绝、非空v1冻结／身份／期初累计、关账review transport与密码契约。运行前后178个kernel生产／相关测试文件SHA不变。仅证明本次实际受影响范围，不是所有业务流程或全仓回归，不与旧316、91、166累计。

固定同源draft开发包唯一一次默认build／ZIP搬移自检passed，未跳过默认validation，实际160个CLI inputs与160次调用一致，4212个软件文件、109个fact kinds、33个保留v1模块与3个合同核对通过。selected756文件、source manifest及完整inventory前后均相同，完整inventory含113个派生pycache；本次是实际全清单一致，不能推广到旧包。包build为`local-kernel-2:d9d5ed53a4d7f7cded8930f462801d556717c08903e7a3e40a94b862314c5b28`，目录／公司保持draft／0，`packaged_offline_upgrade=null`。

包自带隔离自检覆盖备份恢复、后台备份、原生批准关账、闭期更正／冻结历史、备用金、页面及认证API、relative launcher、stdio MCP握手／查询、native私有传输与合成登录清理，不能替代独立AI实际业务MCP、真实当前库升级或正式发布。本轮不重复345或包测试。当前主48完整qualification已通过；当前主12／主48五页分别FAIL10／150、FAIL28／150，独立AI MCP限定整案及最终新备份／两次恢复／cleanup已完成，实际GUI窗口及业务边界另列；旧dd86 FAIL50及旧MCP partial不转移。

[受影响回归／开发包／剩余SQL审计独立原件manifest](open-scope-validation-package-20261004-raw/manifest.json)保存新回归py／log／xml／json、开发包审查md／receipt及剩余SQL审计md七件；原字节SHA、gzip mtime0与回环核验，包receipt实际内容只有隔离合成自检结果、格式／build、路径、清单SHA及布尔记录，没有凭据或PID。既有work九件、native六件与历史原件保留。

## 当前合同、前端与fresh消费者验证

Python数据库合同与response model export `--check`各通过，`contract-and-frontend-scope`的status=passed仅表示这两项exports；其`frontend_scope_same_as_prior_passed_validation=false`，旧前端结果不能转移。因此随后实际执行当前前端一轮contracts／samples／tests／release-build：Node24.19.0，45份既有fixture验证、184测试／0失败、type-check与正式静态build通过。frontend/src、正式static/dashboard及frontend/dist前后完整清单均相同，fixture与source manifest SHA不变；重建结果与当前源及已验开发包相同，没有重新打包或重新封源。前端184不与后端345相加。

另从当前24b9后端实际合成HTTP路径新生成45份响应，并由当前generated consumers验证通过；fresh样本SHA与生产／消费者相关文件SHA前后不变，没有覆盖产品或fixture文件。它与既有fixture45项分别记录，不能用旧fixture代替当前后端响应；合成HTTP合同／消费者通过不等于实际浏览器或性能资格。

两份helper首次预检失败保留：contract/frontend helper误把未在source manifest的生成脚本当成必在清单，任何validation command尚未启动即失败；main48 helper误查不存在的read_repair_state表，真实repair字段在state，首轮guard失败时尚未启动app／登录／qualification。各original与fix1另存，首次失败不改写为通过。main48 fix1随后真正qualification已完成通过；首次失败仍原样保留，完成Q／R见下一节独立归档。

[当前合同／前端／fresh消费者与首次预检失败独立归档](open-scope-contract-frontend-20261004-raw/manifest.json)保存实际helper、无私有数据的log／receipt／45份合成响应及两次preflight failure；原始frontend summary包含PID，原件保持不变，只保存路径／SHA，并另存明确去PID的public analysis，不冒充原字节summary回执。main48 original／fix1 helper实际含合成密码常量，也仅保存私有路径／SHA，不归档源码；两份preflight failure JSON没有密码，按原字节归档。其余选定原件逐件SHA、gzip mtime0及回环核对，源码中的PID变量和测试标题token只是代码／名称，不包含运行PID或访问凭据；合成分页cursor及source-bound版本不当作认证令牌。旧归档保持原样；完成Q／R另独立归档。

## 当前24b9主48完整资格

main48 fix1实际完整qualification完成，Q status=complete、R status=passed；当前registered完整核验四coverage（sources／historical_adoption／projections／read_indexes）均verified、limitations=[]。实际121593事实、50519计算、49245凭证、47关账、337证据；公司`c73e4f4d144340c98ade475b8dd8c8b9`、database`6cc5f6cbe6cd402ebdfd072195398780`，公司合同draft／0。

当前24b9源重新执行fresh open preview，期间2019-12，digest `5b93cc1477b74a296008302bd654c452848b7b91677d06f095c8da788abcccf6`，state `[1,2065,1164,152,48238,0]`。原构造preview digest保留，不代替fresh结果。runner总1597.24762988秒，integrity847589.0567ms、preview739360.6761ms、资格过程1589502.0071ms均属准备正确性耗时，不作刷新性能；Q measurements={}，R qualification_only=true、browser_or_pure_timing_started=false，没有500ms结论。

Q SHA `1a822f7f45934e9eeb52647292f1e10bd9c4bfe601953fd03ecddbf9eb545494`，R SHA `74bfe49d17422fba0d91d5de85c12b6f464d6267f7b346d4abd93af1a91347b8`，source manifest SHA `9d81e82d7e2637637cc052cd42ff80946c6a0274264473d53f1d22a6f48fcef5`。业务文件、公司／目录身份、state、schema_history与read_repair_revision、构造input／checkpoint及source inventory前后相同；catalog owner核对排除正常native登录timestamp，并单列该合法认证变化。synthetic_credentials_revoked=true、cleanup_errors=[]。

[完成资格Q／R独立原字节清单](open-scope-main48-qualification-20261004-raw/manifest.json)仅保存完整安全Q／R两件，30609原字节／7646 gzip字节，gzip mtime0、原字节SHA与解压回环核对。不归档含合成密码的original／fix1 helper，私有路径／SHA及首次read_repair_state预检失败沿用[已有清单](open-scope-contract-frontend-20261004-raw/manifest.json)，该helper预检错误不记作产品失败。未重跑资格或测试；旧归档未改写。随后当前主12完整资格通过；主12／主48纯浏览器均未达标，剩五规模未验，详见后文。独立MCP限定整案另见后文。

## 当前24b9硬边界正确性

固定源原boundary harness仅执行一次full，exit0、runner59.5029秒；与主48语义资格并行，elapsed及RSS仅为诊断，不算纯性能。单份20971520字节（20MiB）实际保存、20971521字节拒绝且物理计数不变；100000实际原银行行完成公共inspect、全解析及typed bank_statement保存，CSV7389036字节、整数分合计12300000，实际100000子行；100001原行合法登记后仅公共inspect超界拒绝，未尝试typed超界保存。5000 typed事实save成功，5001拒绝，均核对无半写入。

最终实际evidence3／fact_revision5001／bank children100000／request8／audit8；独立只读inspection再次与原report相符。合成目录owner／session均0，目录与公司draft／0、identity／schema_history／合同及source／inventory守卫通过；inspection前后数据库字节SHA不变。原report SHA为`037ad84be28775997968fb6b13a077cff143450b8c80b5d680e423e2dae7802e`。没有HTTP／server／browser、真实root／5173或既有进程操作。

post-run只读inspector首轮遗漏`database_format(bundle=...)`必要参数而TypeError，原helper／失败记录保留；fix1只读核对通过，未重跑harness。该辅助错误不冒充业务边界失败或首轮通过。没有5000事实preview／发布、100000行整页浏览器或typed银行100001直接拒绝实证。

[硬边界独立原字节及明确分析归档](open-scope-hard-boundaries-20261004-raw/manifest.json)保留结果md、report、run log、inspection、首次TypeError及original／fix1 inspector；runner原件包含child PID，保持私有原路径／SHA，另存明确去child_pid的public analysis，不冒充原字节runner。逐件gzip mtime0、SHA及回环核对，无业务原件或数据库归档。240／1536MiB证据流程及独立MCP限定整案已完成，实际范围和公开分析清单见下一节。

## 当前双档证据与独立MCP整案

240及1536MiB各唯一一次、顺序完成原harness完整exit0，实际登记原件、完整verify_file、portable backup、restore及restored verify_file通过。source756、manifest／额外inventory、workspace合同守卫通过；恢复原件count／distinct digest／BLOB字节一致。全部draft／0、身份／历史／结构指纹保留；正常合成凭据轮换及全部session撤销、公司DB字节SHA不变，清理在测量窗外。

| 档位 | 实际原件／BLOB字节 | ZIP字节 | sampled resident RSS峰值字节 |
| --- | ---: | ---: | ---: |
| 240MiB mixed | 12／251658240 | 126954163 | 254685184 |
| 1536MiB mixed | 77／1610612736 | 801691921 | 258252800 |

前台实际浏览器默认20条／deferred简报争用共331样本，≥500ms均0；仅2人／40业务开放月。RSS每10ms采样resident及同进程后台操作，不含Node／Edge，不是严格上界。游戏／同步背景不受控，无窗口内连续OS CPU监测；这些观察不等于主48五页500ms验收，也不证明稳定因果性能。

[双档独立清单](open-scope-evidence-resource-final-20261004-raw/manifest.json)逐件核对原27件path／SHA／长度，保留安全原manifest与明确去PID的结果／两档report／cleanup公开分析。原report SHA240为`8750e8ff17c7f22be8d2d4a0d82c9c6bfd857db3a26baf9404f7e2a0b2bbe61b`，1536为`339912c958c3c73949eca3d6b2ebfcc0f587c1b6ca38dbec6512bbf9eb90a577`。分析不冒充原字节回执，原PID／helpers及环境原件保留私有路径／SHA，没有公开数据库／ZIP或凭据。

独立AI通过当前开发包的生产stdio MCP对隔离HTTP／kernel实际操作，acceptance status=passed。AI生产调用124（源113＋首次restore6＋final5）；harness只读preflight10另计，不计initialize／list_tools。root owner原生批准request envelope1，native_execute／native_update各1；AI private native calls0。三host正常退出、六个owned进程已退出，三处合成会话凭据均absent／error1168；source756及package4212完整清单前后不变。

| 实际业务 | 本轮结果／边界 |
| --- | --- |
| 乙工资 | gross60000、员工缴费4800、单位缴费9600、税0、net55200整数分；published，未付款、未申报、未关账。 |
| 甲单月关账 | root合成owner经同版私有native批准，2026-09关账；冻结全文SHA`9098c46037835b91b99e333890754a45d23246c57a95cb247f53fb1da6ca70ba`保留。实际GUI密码窗口未出现，不声称真人窗口覆盖。 |
| 外部办理与复核 | 实际记录completed／reviewed；原对象与最终恢复对象全文相同，冻结内容未受后续外部操作改变。 |
| 新备份及两次恢复 | 两个新恢复catalog实际integrity verified、四coverage verified／limitations=[]，身份／冻结全文保留。final ZIP181582字节、SHA`b4e1ef0c66dd08a0d5dde2074179f4c43b23647fad8c4e897c07563cb822681c`；源最终backup succeeded／verified。 |

final ZIP自身backup job保留snapshot时running，恢复后没有重跑该job去写源路径，不伪造restored-job成功。操作中PLACEHOLDER摘要拒绝、首次restore两个unknown alias拒绝、helper及native查找首次预检失败均保留原结果；修正不改写首轮通过。旧dd86的丢响应、stale preview、重试／接续等场景本次未重跑，不由本轮整案推广覆盖。

私有acceptance原件SHA`9d70127055d4dcc87c4f7b36b7c0f7dd83a71f22d8422d71e1c9d82cadfccb35`；[MCP独立清单](open-scope-mcp-final-20261004-raw/manifest.json)逐hash／长度核对其实际artifact链，仅归档明确去owned PID的完整公开analysis。原receipt、tools raw、含密码helpers、private native交换及ZIP保持私有path／SHA，不公开认证token／capability／private URL或运行PID；去PID分析不是原字节receipt。

## 当前主12／主48纯浏览器：两规模均未达标

当前24b9主12fix1及主48原窗口实际完整结束，各5页×30有限样本、150成功／0错误，status=over_target、runner exit1；源码／业务文件／身份／state／历史／repair守卫不变，synthetic_credentials_revoked=true。每规模ResourceTiming实际300响应全部200，各resource diagnostics=[]，非诊断HTTP数组http_requests／dispatch_calls均空；原top没有errors字段，不补造该字段。

| 页面 | 主12 median／p95／max ms | 主12 ≥500ms | 主48 median／p95／max ms | 主48 ≥500ms |
| --- | --- | ---: | --- | ---: |
| brief | 376.4／455.5／455.9 | 0 | 456.3／576.2／596.5 | 5 |
| funds | 354.7／511.9／534.8 | 2 | 376／436.6／496.4 | 0 |
| employees | 294.1／332.9／335.8 | 0 | 316.2／336.4／355.6 | 0 |
| assets | 235.4／255／256.2 | 0 | 356.5／436.3／512.8 | 1 |
| reports | 476.1／575／575.4 | 8 | 516.1／593.4／595.2 | 22 |

主12 **FAIL10／150**，主48 **FAIL28／150**。render_tail按每页原30条汇总如下；它含响应处理／JSON／Vue DOM及已有两次RAF完成检查，不是独立paint或源码CPU归因。

| 页面 | 主12 tail median／p95／max ms | 主48 tail median／p95／max ms |
| --- | --- | --- |
| brief | 31.3／41／41 | 28.55／41.1／42.1 |
| funds | 28.05／40.5／40.7 | 33.95／38.5／40.6 |
| employees | 31.1／40.9／41.4 | 30.6／41／41.2 |
| assets | 28.95／37.9／40.4 | 28.25／37.3／40.7 |
| reports | 29.55／40.3／40.6 | 32.6／41.1／41.2 |

主12冷态五页566.6／605.2／512.7／493.4／872.7ms，首次1632.5ms；主48冷态636.9／882.6／852.4／1186.4／1513.5ms，首次2900ms。两规模切公司均unavailable／single_eligible_company，不能记切公司通过。主12纯窗87.969秒、主48纯窗101.578秒；总runner含准备／资格过程不作纯窗耗时。

主12controller实际暂停正在ready等待的main48真实worker，GetProcessTimes kernel／user前后原值相等，窗口后恢复一次成功（resume_ntstatus=0）；主48targets=[]／resumes=[]，没有暂停对象。只控制本次测试owned进程，没有停止游戏／同步或真实服务；release前实际环境snapshot保留，游戏／cloud背景不受控、纯窗没有OS连续CPU监测。因此两次失败是当前样本真实未达标结果，不能归因某一源码或宣称稳定改善／回归，也不拼接旧样本。

主12current完整资格Q为complete、四coverage verified／limitations=[]，事实30225／计算12413／凭证12309／关账11／证据85，fresh preview digest`045e8801f1c2808a948a77e3ba739a8885957cee85c86d07cd2a40b4ac3f5f06`。Q SHA`5199e57e6edfc844cac4add609eddf548fd81f715427858b8c2deaeb70a20a3c`；主48先前完整Q及本次restart实际fresh preview证据保留。资格正确性不能替代500ms门槛。

[两规模独立清单](open-scope-two-browser-final-20261004-raw/manifest.json)安全原字节保存browser完整samples／ResourceTiming及主12Q，另存明确去PID的两runner／controller分析、只读样本摘要与测试sidecar记录。主12browser SHA`9d99cd3e3bfe313cde90c0e4a0d6aa3178c223c09fd546bc00352faa42904103`，主48 SHA`311c1ef035ae5837711cc88cb96b2a6ae68ac3e215ce6959e69ea35902d19cc0`。全部gzip mtime0／SHA／回环核对，不公开运行PID／凭据／private URL，分析不冒充原字节receipt；所有慢样本保留。

主12／120首轮CLI路径拒绝exit2发生在任何数据库操作／qualification前，业务／历史／source守卫相同，旧log／receipt与外部fix1 SHA见[首次路径拒绝清单](open-scope-browser-cli-preflight-20261004-raw/manifest.json)。随后测试helper还发现独立checkpoint与private sidecar权限问题；剩五样本10库／30个DB-WAL-SHM已完成权限修复和当前private ACL核对，DB字节SHA／身份／state／history及WAL SHA不变，不声明SHM字节不变。原before-ACL capture为空数组，旧changed_paths=[]不能证明未改；原CLI／checkpoint／ACL收集失败记录原样保留，raw owner SID／SDDL仅私有路径／SHA。外部fix2 SHA`45cb6869e43bbb6ad6b8595dce45ca8b1d6d6c59991b77b82ab605f1d6faab12`仅compile，未跑剩五规模资格／性能。因当前两规模未达标，暂不继续main120，下一步先诊断当前根因；不重复345／前端／双档／MCP，不宣称第9阶段完成。


## 剩余SQL组只读审计

当前审计提出三个具体候选，均**未实施、收益未验证**，不写已排除：资金完整summary与bounded detail窗口分工，必须保留完整账户合计／内部转账／bank partition及count／cursor；季度frozen flow在已认证月根之间批量读取精确classification headers，仍逐月核身份／membership／digest，现有汇总不证明ID重复；员工roster先按requested kind定位再选exact heads，保留开放／撤去／更正／missing-source与完整frozen零行发现。bank summary-only窗口拆分在dd86已存在，不计新优化；23次read_report_flow调用含11缓存命中与12实际月读，不计23次SQL。跨请求范围重复不授权persistent成功缓存，完整核验／关账／repair／backup／restore和固定v1保持独立来源边界。

## 顺序native耗时诊断：没有稳定收益结论

同一before／candidate固定源随后分别顺序运行六入口，每入口先预热3次，再记录30次；每源180次计时调用及18次预热，全部成功，六个归一化business SHA跨源相同，所有文件／身份／state／历史／repair守卫与source inventory均不变。计时调用未启用SQL、profile或分配插桩，窗口内没有回归、建库或资格核验。

| 入口 | before median／p95／max ms | after median／p95／max ms | ≥500ms before→after |
| --- | --- | --- | ---: |
| context | 10.373／12.157／14.749 | 12.047／14.317／15.935 | 0→0 |
| brief | 327.587／406.512／409.885 | 451.275／560.312／620.524 | 0→6 |
| funds_first | 252.096／321.587／327.343 | 310.340／469.706／491.385 | 0→0 |
| employees | 204.799／275.643／280.120 | 264.722／406.027／434.763 | 0→0 |
| assets | 258.035／327.430／342.757 | 370.167／592.554／615.323 | 0→3 |
| quarterly_report | 697.588／805.039／851.978 | 568.491／811.290／842.298 | 25→20 |

report中位下降但p95上升，其余入口after中位均更慢；全部30原始样本及慢样本保留。两源顺序执行，非交替对照，没有窗口OS CPU监测，因此不能据此证明稳定收益或因果回归。事后一次GetCim工具stdout看到游戏、云同步与杀毒CPU较高，仅是窗口后的单次观察，没有持久CPU原件，不补造，也不据此归因任何单个慢样本；没有停止、暂停或改变游戏、同步及真实服务。

[独立native原字节归档manifest](open-scope-reverse-head-native-20261004-raw/manifest.json)另存prepare／native／comparison脚本与before／after／comparison JSON六件，保存所有原始samples，mtime0、SHA及解压回环核验；不修改上述9件work归档或旧原件。native仅包含内核调用，缺HTTP授权、context／main并行与浏览器渲染，不能继承为五页500ms通过。正常关系省返回行／字节已由独立工作量对照证明；纯native计时环境未受控，没有稳定延迟结论。

第9阶段仍实施中。当前24b9受影响回归、合同／前端／消费者、开发包搬移自检、主48完整资格及硬边界正确性分别通过；当前两档证据流程及独立实际MCP限定整案完成；当前主12／主48五页分别FAIL10／150、FAIL28／150；剩五规模及按需详情未验，暂不继续主120，先诊断根因。GUI窗口及业务限制单列。dd86旧五页FAIL50／150保留；draft／0及正式冻结／正式交付／运行切换延期保持不变。
