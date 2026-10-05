# 已认证冻结采用与实际凭证头绑定组（2026-10-04）

本组从807凭证头缺陷复现，经e39统一回归619通过／17失败，继续集中修复并固定到bfb来源。bfb的57模块统一回归832项全部通过，插桩与成对原生诊断已完成；资产仍有≥500ms原生样本，没有稳定时延改善或整页达标结论。e39失败原件及各来源工作量保留，不倒签为旧来源通过；bfb未新做完整资格或真实浏览器验收，第9阶段未完成。

## 根因、触发与实际复现

冻结采用的身份、源、目录或结果内容已认证，并不自动证明实际voucher头仍与该冻结采用一致。消费已认证采用时，缺少将实际凭证头相关字段绑定回采用依据的检查，可能接受头已损坏的凭证。这一职责不能由只读body、member身份或source kind证明代替。

在三个新建隔离合成公司fixture中，仅损坏2026-03已关账资产消耗凭证的reverses_id、total或number之一，close_reference保持不变。原复现后、生产修复前的只读工具记录核对6个相关生产文件＋4个fixture helper共10文件与807逐一相同，精确SHA在清单metadata保存；不是当前工作区的重新检查，当前修复代码不能再称仍807。source807，source SHA `80735c1e2832458cacb1e13097b9efd3e357cd5bd09fe6a67ad56716eef08613`。实际入口结果：

| 损坏字段 | assets／资产详情／business_status | 与损坏前响应关系 | 完整core |
| --- | --- | --- | --- |
| reverses_id | 均accepted | 三者均改变 | 拒绝content_integrity_failed／voucher_lines_mismatch |
| total | 均accepted | 三者均相同 | 拒绝content_integrity_failed／voucher_total_mismatch |
| number | 均accepted | assets及详情相同，business_status改变 | 拒绝content_integrity_failed／manifest_voucher_content_mismatch |

“accepted”不能统一写成“响应未变”；金额或方向展示变化与对头损坏未拒绝是不同现象。三个case的完整core均已保护，不能称所有核验入口漏检。实际复现只覆盖上述fixture和入口，不能扩大为所有五页、所有voucher字段或真实资料已损坏。

## e39凭证头修复来源与职责

固定源码 SHA `e39c5611ad8ce3a73bea4fd9f19de6cca25ba1a3fe0d893f0de78779807384c8`，769文件；源清单 SHA `45afd26b6b2bd9fb911e4af41102bb5c6865bf7fa46b0b2b0cb9d971f67810aa`。相对807仅修改QueryReads、close_storage、BusinessQueries、asset_batches、dashboard_funds和dashboard_reads六个生产文件，新增三个专项；前端、结构合同及fixed-v1字节不变。源差异归档与专项分别保留来源，早期共享工作区结果不倒签为完整e39资格。

QueryReads.verify_selected_voucher_adoptions批量取得实际完整头，将id、voucher_id、calculation_id、number、total、reverses_id及period绑定到精确冻结voucher叶；采用basis calculation不能冒充原凭证calculation。close_storage提供认证叶读取，固定reader保留完整section解码回退。reference镜像、目录桶、头与采用成功证明均先暂存，整组成功后才发布到当前owned snapshot；晚项失败不发布前缀成功，unowned、新snapshot和不同cutoff不借用旧证明。资产采用、项目成本及资金消费者接入共同绑定，不取消原source、owner、publication、member、typed fact、kind／month／digest及body职责。

BusinessQueries普通与owner路径用独立冻结集合核对选中凭证，覆盖period、kind、subject或实际源缺失导致的集合遗漏。Journal单月无kind/accounts筛选路径核对root完整ID集合；明确subjects可用独立close_accounting选择核对。普通filtered/history范围只绑定命中的头，不据此宣称完整negative proof；mNone及其它筛选范围的缺席authority也不扩大为已解决。本组同时复用已完整认证的实际owner结果，省去再次读取owner body；成员、owner digest、显式body与完整core核验仍保留，缓存不替代它们。

## 同类审查与验证边界

源级同类审查已有强保护在Journal.account_amounts、完整integrity、Maintenance及public backup前后核验；这不代表本次三字段probe直接运行过它们。纯冻结report flow／_stored_month受不可变投影root认证，不是live-header消费者。verified_rows只是缓存访问，hydrate只取refs；report_projection._manifest_rows是构建／完整compare。报表fallback及普通filtered/history的命中头绑定与独立完整集合证明须分别评估，不能用当前修复覆盖所有筛选隐藏情形。

| 已完成专项 | 通过／失败 | pytest秒 | 来源边界 |
| --- | --- | --- | --- |
| root journal run1 | 11通过、18未选 | 9.86 | 早期专项 |
| root journal run2 | 33通过 | 27.39 | 收敛后专项，含交集 |
| agent header run1 | 15通过 | 42.98 | 初版专项 |
| agent owner/header run2 | 9通过 | 19.94 | 含复测 |
| source boundary run3 | 7通过 | 18.46 | 含复测 |
| fixed-v1及晚失败run4 | 4通过 | 13.03 | 含复测 |
| owner boundary run5 | 2通过 | 6.19 | 含复测 |
| owner reuse首次失败 | 5通过、2失败 | 14.18 | fixture／对照构造失败原件保留 |

两份新增header／owner专项共31个唯一节点；分批复测不累加为37，root33也不再与它们相加。首次owner失败分别涉及对照body工作量与带外键约束的seal损坏构造，不写成生产完整性失败。大组回归已结束，636项中619通过、17失败、6条warnings；插桩工作量已完成，且与回归并行，耗时不作纯时延；没有新的500ms、浏览器资格或阶段完成结论。

[修复专项、插桩及失败回归25件清单](voucher-header-binding-e39-20261004-evidence/manifest.json) SHA `d577c665347c3f852916af4d231c0243c15e10b36f8bed4f311f459359511769`：八组log/XML、源差异、两个控制脚本、e39 profile的py/json/log及统一回归log/xml/json。回归控制脚本与先前归档逐字节相同，复用原件、不重复造件。脚本只保存、不执行；不包含损坏SQLite、固定源码整仓或完整私有inventory。原件SHA与脱敏representation分开，gzip mtime=0并核对解压回环。

## 固定e39插桩工作量

基线实际文件为`.tmp/stage9-frozen-member-guard-profile-20261004.{py,json,log}`，绑定807；新文件为`.tmp/stage9-voucher-header-group-profile-20261004.{py,json,log}`，绑定e39。五页业务SHA逐页相同，新profile的业务守卫、源码inventory不变及read pool关闭均为true。原件source_files=766沿用runner元数据，原值保留；固定e39 source manifest实际769文件，不用该旧计数替代清单绑定。

| 页／计数 | 807 → e39 |
| --- | --- |
| assets SQL | 376 → 380 |
| assets返回rows／value bytes | 13755 → 14111／11991251 → 12055278 |
| assets结果body rows／decode／bytes | 247 → 246／247 → 246／364225 → 363229 |
| assets JSON loads／input bytes | 2239 → 2238／8907624 → 8906628 |
| assets采样VM | 1215500 → 1240000 |
| brief SQL／采样VM | 191 → 190／819400 → 815400 |
| funds SQL／value bytes／采样VM | 181 → 178／3576630 → 3609894／1321900 → 1246300 |
| employees SQL／采样VM | 98 → 98／1174400 → 1174500 |
| report SQL／采样VM | 255 → 255／1022600 → 1022600 |

实际公开assets只省一个activation owner body、996B；新增actual frozen-header必要认证带来4条SQL及约65KB返回标量，总读取量增加，不能写性能通过。其它页未列工作量计数保持相同，funds增加33264B返回标量也如实保留。采样VM与插桩耗时均不等于纯时延，未运行本来源500ms验收。

同类caller补查已源码确认dashboard快余额分支未传已解码消费owner，这是e39遗漏；后续bfb已修复，专项结果见下节。此前公开工作量测试把“只省一个body”归因于identity时序，未覆盖这个caller分支；当前纠正归因，不据此宣称完整消费guard不存在。直接两个owner guard的8→6 body证明仍属该直接专项范围，不能扩大为公开页已经省两份。后续修复源码不装入e39结果；本次42模块回归已失败，结果与分类见下表，不归档为通过。

## 统一回归失败与实际边界

固定e39的42模块回归共636项：619通过、17失败、0 errors、0 skipped，6条warnings；pytest 953.20秒，控制helper 956.3319778秒，exit1。源码inventory未变化；这些elapsed不是纯计时。本轮专项与回归有交集，不相加为不同项总数。

| 初步失败分类 | 项数 | 精确位置／当前判断 |
| --- | --- | --- |
| asset旧fake签名不接受新keyword | 1 | test_asset_batch_reads::test_selected_asset_members_batches_exact_metadata_and_rejects_missing；TypeError；后续更新测试替身以接受新keyword |
| funds历史identity没有预期拒绝 | 13 | test_funds_historical_account_identities::test_public_historical_identity_rejects_consumed_damage的12个参数＋test_open_historical_state_still_authenticates_original_rows；DID NOT RAISE；后续确认旧断言调用brief金额路径，该路径没有identity消费职责，优化前也如此；改到实际funds消费入口并保留brief边界 |
| funds公开body工作量对照 | 1 | test_funds_historical_outcome_reuse::test_public_reuse_reduces_actual_body_transfer_and_decode；预计省4、实际省2；后续修正测试对照覆盖，不能把未发生的body节省写进指标 |
| funds失败缓存patch | 1 | test_funds_verified_money_effects::test_effect_inputs_publish_only_after_events_proof_and_share_one_selection；后续将failure injection接到新的实际证明路径，保留整组原子发布断言 |
| missing reverse-index旧成功预期冲突 | 1 | test_query_reads::test_publication_period_finds_voucher_even_if_reverse_index_omits_it；后续明确缺镜像必须严格拒绝；通过维修恢复后才能成功，不删除精确头refs检验 |

17=1+13+1+1+1，完整失败原件保留。上述结论只描述e39当时状态：该来源修复未全部通过，619通过不是后续源码完整资格，早期专项不能掩盖其统一回归失败。后续修正以独立固定bfb验证，结果见下节；e39失败不改写为通过，e39当时也没有新纯计时或浏览器达标结论。

## 集中修复bfb来源与专项

固定源码 SHA `bfb70cb0bdcffe8a17bfa326364242386e2b9e497fc9f1da00dc91b300f77919`，770文件；manifest SHA `c0061c3d0277601ea97a1f706d9bd668d04bd688e575720ca3eeec5e378ad5f0`。归档源差异以807为基线：9个生产文件与7个既有测试修改，新增4个专项；其中dashboard消费owner caller、entity_references与integrity属于e39之后的集中修复。早期专项分别保留当时来源，当前统一回归已完成832项通过；root强化的VM工作量节点在本组内通过，64refs与2048重复引用＋4096无关对象均取同2个ID，没有另行复跑。

dashboard快余额分支现把同snapshot已认证decoded消费owner传入成员读取，补齐此前实际遗漏。承认早期公开caller覆盖不足：旧“省一个body来自identity时序”归因错误，不能以直接两owner guard专项代替公开页覆盖。新19项专项覆盖实际开放／关闭历史消费owner路径，公开资产返回rows与业务值不变、body读取／decode实际减少：开放例12→11、9441→7524B，闭期例19→18、16605→14687B；这些是合成fixture工作量，SQL同为166／198，不能称时延或大样本500ms改善。

资金旧13项失败后来确认是测试调用brief金额入口，无历史identity消费职责，优化前已如此；将拒绝断言移到实际funds身份消费入口并保留brief金额边界。fake keyword、失败注入路径和body-count对照同步校正；缺reverse镜像保留严格拒绝，维修恢复后再验证成功。集中follow-up首轮20通过／15失败涉及测试时间与API构造；run2为34通过／1失败，最后entity_type失败暴露独立真实缺口，不能将其归作fixture。

真实新缺口是raw entity账户类型bank→cash变更，完整core原先未按原始事实的显式entity引用声明检查类型。新增verify_fact_entity_types在原始事实字节、顺序与seal已认证后批量精确检查引用对象身份、kind及account_type，独立于可重建reference目录；只使用已声明约束，不将business引用视作entity，不从空kinds推断类型。此证明补到完整integrity；它不扩大为所有筛选缺席证明，也不替代voucher头认证。fixed-v1按其descriptor声明测试，不表示正式v1合同、正式运行包或交付已验证。

| 集中专项原件 | 结果 | pytest秒／边界 |
| --- | --- | --- |
| asset balance owner首次命令 | 0项、exit4 | 0.22；XML路径误当测试路径，保留log |
| asset balance owner修正运行 | 19通过 | 56.06；实际公开body工作量与保护边界 |
| root funds follow-up首次 | 20通过、15失败 | 106.37；测试时间／API fixture问题原件保留 |
| root funds follow-up run2 | 34通过、1失败 | 110.83；剩余失败是完整core真实entity-type缺口 |
| entity-type新增专项首轮 | 10通过、1失败 | 7.98；v1 descriptor fixture构造失败 |
| v1 descriptor单节点重跑 | 1通过 | 4.98；修正该fixture，不新增不同节点 |
| entity-type Ruff | 通过 | 原log保留，不是业务验收 |

entity专项包含新增10个节点和一个已有资金节点。首轮10通过与同一失败节点重跑1通过分别记录，不与root重复节点或asset19累加成新的全量通过数。[集中修复15件清单](voucher-header-binding-bfb-20261004-evidence/manifest.json) SHA `415ac4a98d8325b4538f15707b7a7c8a5f50a4008e718916027eaaf0dfc2a536`，包括上述成功／失败原件、Ruff、源差异与两个控制脚本；不复制完整source manifest、私有inventory或SQLite。gzip原件SHA、脱敏表示及回环分别核对。脚本只保存，不运行；本节早期专项来源与失败原件保持不变，bfb统一结果见下节。

## bfb统一结果与收口

固定bfb的57模块统一回归832通过、0失败、0 errors、0 skipped，6条warnings，exit0；pytest1199.24秒，helper1203.4743149秒，source inventory不变。原log SHA `5e93c5b1f65f583111a22cd1ba0beaa914f138162db8e9b0d60a3e9e6096fe3a`，原XML SHA `e8eac1951852a7cdebf504285c0b1cee91669f24c9eb90df457c9cfa7c735d87`。832与早期专项有交集，不累加成不同节点总数；该结果验证本组修复，不等于新完整资格或正式v1包。

新插桩metadata正确绑定770文件，五页业务SHA均同807；守卫、source inventory不变及pool关闭全部true。相对807工作量：

| 页 | SQL | 返回rows／value bytes | 结果body rows／bytes | JSON loads／input bytes | 采样VM |
| --- | --- | --- | --- | --- | --- |
| assets | 376→381 | 13755→14232／11991251→11950624 | 247→245／364225→253244 | 2239→2237／8907624→8796643 | 1215500→1241100 |
| brief | 191→191 | 14169→14219／8243473→8245723 | 1009／1059809不变 | 1403／2911079不变 | 819400→815800 |
| funds | 181→178 | 10178不变／3576630→3609894 | 542／387755不变 | 857／962894不变 | 1321900→1246300 |
| employees | 98不变 | 5259／5189473不变 | 101／554008不变 | 475／2790874不变 | 1174400→1174500 |
| report | 255不变 | 18592／11598641不变 | 1007／1058063不变 | 513／4342872不变 | 1022600不变 |

资产少读／解码两个owner body，节省110981B body；必要实际冻结头认证仍带来额外SQL、返回标量与VM成本。body减少不等于稳定时延收益；插桩elapsed不用作纯性能结论。

成对原生诊断以807为baseline、bfb为current，每页3暖机＋30正式样本，每个窗口150成功、0错误，全样本保留。两窗口五页业务SHA完全相等，各自before/after守卫、source inventory和pool关闭均true；company、catalog和非manifest文件跨窗口相等，manifest各自绑定，不能要求其不同来源路径／SHA相同。

| 页 | median ms：807→bfb | p95 ms：807→bfb | max ms：807→bfb | ≥500ms样本 |
| --- | --- | --- | --- | --- |
| assets | 450.697→463.1211 | 501.7037→481.6669 | 508.173→551.6532 | 2→1 |
| brief | 338.8551→335.81295 | 360.4423→357.1084 | 365.4009→362.9241 | 0→0 |
| report | 395.29165→396.71275 | 461.7207→454.156 | 518.7963→456.6266 | 1→0 |
| funds | 252.9024→242.77875 | 326.7718→308.4712 | 330.7861→320.8252 | 0→0 |
| employees | 262.5861→275.734 | 300.8778→317.7602 | 315.2194→326.4704 | 0→0 |

这是顺序窗口观察，资产median变慢、max仍551.6532ms，不能声称稳定改善或达标。纯窗口开始前本团队测试、profile和agent操作均结束；没有实测保存OS CPU干扰，不能称系统完全idle。原生不含HTTP、render或prepared owner TODO，不是500ms浏览器资格；旧c3真实浏览器19个慢样本及其它来源失败保持原事实。下一步只读排查少量身份／目录需求却解码整段历史accounting family的必要读取范围，目前未实施、未经性能证明，不覆盖bfb本组修复结果。

[收敛结果12件独立清单](voucher-header-binding-bfb-converged-20261004-evidence/manifest.json) SHA `8da9f7018acfcbef6e940f3e854cc221affd863dd17cc6f48b701aa833319775`：回归json/log/xml、profile py/json/log及baseline/current各py/json/log。原件SHA／字节与脱敏分析表示分别记录，所有暖机、正式与慢样本保留；私有根路径、守卫及read_context最小化为SHA引用，不冒充raw，不含凭据、SQLite或完整source inventory。gzip mtime0／回读／哈希已核对；此前15件及e39／807证据不修改。

## 复现原件

[三件独立清单](voucher-header-binding-20261004-evidence/manifest.json) SHA `eabe355091930e647749921ad04df4dae161715cdbefca30044f0c5ac37beffd`：`.tmp/stage9-voucher-header-defect-20261004-run1/probe.py`、`probe.log`、`receipt.json`。脚本是原复现材料，只保存、不执行；原件SHA／长度与公开representation分开，私有根路径及read_context按分析脱敏／SHA引用处理，不冒充raw。无SQLite、凭据或真实资料。gzip mtime=0、SHA与解压回环核对；此前失败与所有性能档案保持原来源，后续统一验证结果仍在同组记录，三件复现原档不变。
