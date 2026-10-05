# 四类必要读取根因与合组验证

本轮保持draft／0，不正式冻结、交付正式包或切换现有5173、资料根及身份。四组修改已进入一次统一受影响回归：18整文件、29精确节点，runner为 `.tmp/stage9-four-read-groups-regression-20261003.py`。**统一回归289通过／1失败，579.21秒；单项断言修复后1通过，4.73秒，不写290项整组全通过。** 当前固定fa5c主48已fresh四项资格通过，完整150成功／0错误／58次≥500ms，整体失败；旧4d完整150／FAIL3保留，见[4d独立记录](owner-authority-read-groups-20261003.md)。开发包产品自检通过，独立AI接续未完成；不同源结果不作源码因果判断，不与以前176／148或376／1及单项修复相加。

## 四类根因与最窄实现

| 根因 | 实际改变 | 必要保护及未覆盖收益 |
| --- | --- | --- |
| 必要摘要格式检查逐字符进入Python | `types.is_sha256_hex`集中严格小写ASCII、恰好64字符的C正则判断；current settlement目录、close preview／derived roots、balance bucket、asset owner membership采用同一窄规则。 | 保留原isinstance／type区别、全部必要目录条目、顺序／范围／重复／数量、父认证原文SHA、身份及实际来源。大写、空白、换行、Unicode替代、坏字符拒绝；不改存储／DDL／响应。已有正则及32字符身份路径不改，v1独立实现不改。 |
| 已知资产开放期下界被nullable OR隐藏 | `_asset_open_batch_owners`在after已知时使用明确 `posting_period>? AND posting_period<=?`；无close只用上界。primary及baseline两支UNION保留。 | 仅候选owner locator，仍调用原asset_members_many认证。按采用posting_period而非owner原期定位，replacement、withdrawal、baseline-only及未来排除保留。不新增索引、lookup、token或缓存；不影响brief其他消费链。 |
| 主体清偿先hydrate全尾再筛选 | `_tail_periods`先完整认证全部真实期间／late review／source／seal；仅frozen_subject_summary显式选择 `_subject_scope`，按subject或精确source obligation keys两支UNION装载真实payload，构建目标override／period amount。 | 空key direct、跨主体付款、负贡献／撤去、current后来更正及unresolved保留。窄scope／tail不写通用maps；已有完整scope可作superset。root／selected block仅复用同连接活动snapshot已有完整成功缓存。未命中无关冻结state坏正文不再单独阻断主体读，full／close／repair／backup仍拒；全尾source／seal坏仍普通拒。 |
| first资金首屏同一money source构建两次 | 支持的owned current first先保留原完整state／opening／unknown身份准备，既有_sql_summary_page同次生成九字段全账户summary和首账户page；summary活动keys与零活动keys合并排序后选择。 | 全money／transfer在筛选前构建，公司汇总不截断；原checked／None／negative／来源／cursor保留。unowned、v1、identity_correction及不支持keys保留旧流程；all／explicit account／accounts section／brief amounts不进入first guard。bank／investment详情仍独立，full不消费页面summary为authority。 |

四份scope为 `.tmp/stage9-sha256-format-validation-scope-20261003.md`、`stage9-owner-4d-native-work-and-open-owner-range-decision-20261003.md`、`stage9-settlement-subject-tail-scope-20261003.md`、`stage9-owner-funds-first-shared-source-implementation-20261003.md`；资金原始消费审查另保留 `stage9-owner-funds-first-account-repeated-movements-scope-20261003.md`。行号与静态建议限定各scope时点，以最终实现／实际回执收口。

## 旧4d实际工作与范围探针

诊断绑定固定4d SHA `4d4c2f9bed660d5738ab9a5aea4d704525cb09207578a63679e70793163d11b5`、qualified SHA `80b6f8d722cf3afcb9d86ce9e535bbb4cfa0c9f47e466dbe1177ba9851d75973`及同一owned main48 root／身份／state。6入口各warm一次、measure_work一次，funds／assets各profile一次，共14次只读native调用；instrumentation／profile响应hash与各自warm相同。此段只记录旧4d诊断，不充当fa5c资格或页面结果。

| 4d入口 | SQL | VM（100步采样） | 返回行 | 值字节 |
| --- | ---: | ---: | ---: | ---: |
| context | 4 | 10,900 | 96 | 850 |
| brief | 193 | 869,700 | 16,338 | 7,507,561 |
| funds | 171 | 1,438,300 | 11,372 | 3,432,830 |
| employees | 98 | 782,300 | 6,975 | 4,227,716 |
| assets | 258 | 920,600 | 9,663 | 6,563,498 |
| quarterly_report | 255 | 948,100 | 24,869 | 10,684,375 |

旧4d相对148d的brief／assets各少2条镜像query、1458行、219006值字节，decode不变；funds／reports上述工作量不变。profile中目录三次分别认证all／open／subject，不是三次重复root；146965次字符generator及65.643ms包含profile开销，不作可节省页面耗时。尾段self含SQLite cursor step，不是纯PythonCPU；资金loads数不能代替主要成本归因。

同库同参数资产range探针的owner集合完全相同：nullable下界298600VM、明确下界5500VM、posting_periods＋JSON IN14400VM；原baseline plan只有上界，明确下界能走双边界。采用明确下界，不采用新增月份locator。它是原4d同库范围对照，不是新整页收益。独立必要源／余额／seal的posting_periods定位继续保留。

## 首次与定向修复原件

| 独立回执 | 实际结果与失败 |
| --- | --- |
| SHA format directed first | 39通过，1.89秒；格式等价、合法／损坏目录及Python调用工作量边界。 |
| asset open owner range first | 6通过，7.03秒；无close／已知close／4000无关publication增长及3个真实engine成员／源故障边界。 |
| subject tail first | 5通过，31.66秒；真实三方完整输出对照、current／historical及counts组合、无关坏source／seal／缺row、selected／unselected state及repair。小fixture尾payload／override各6→1只是此fixture额外装载。 |
| subject boundaries first | 4通过／1失败，23.01秒；错误amount_fen测试字段。首次fixture修复仍失败，因为真实撤去返回空义务而非零金额；宽scope／独立SQL也为空。两份失败均保留。fix2仅该1项通过，6.85秒，保留发布24000→更正26000→撤去为空的双counts完整断言。 |
| subject root／block risk | before复现失败：additional_root_reads1、selected_block_reads2、unique1。沿现有完整root／block缓存修正后仅该1项通过，9.78秒：0／1／1。金额700000和窄maps不污染断言保留。 |
| funds first shared source | 首轮11通过／2失败，16.64秒；Unicode ID被正式ASCII合同拒绝、Registry不是dataclass，均为新增fixture准备失败。只改合法排序case及scoped registry设置，未放宽生产。补测7通过，10.85秒，覆盖两个修正、四个新非first分支及旧首账户单项；不改称首轮全过。 |

资金真实合成case完整response等于旧两阶段法，money source SQL调用2→1；48条公司movement／首账户24条／后续22条末页保留，body加载不增加。负退款／transfer／冲正／unknown、其他账户坏源和SQLite overflow保留。实际first必要state proof前移，多个独立非法条件同时存在时异常优先级可能不同；每个独立非法源仍拒，不能声称全部错误顺序不变。

定向分轮不相加为完整套件。旧统计decode_close wrapper已接收并透传 `_verified_material_versions`，旧断言保留；新private keyword／scope必须在合组时验证真实wrapper透传，不能只看无TypeError。

## 统一回归、归档与待验

统一18整文件＋29精确节点已闭合：289通过／1失败／2个record_property警告，579.21秒，runner580.96197秒、files_unchanged=true。两个警告是JUnit记录property的xunit格式提示，保留原文，不计业务失败。范围覆盖普通五页、资产成员、清偿／员工、资金all／explicit account／section／first、HTTP／CLI／MCP、full／close／repair／backup及真实非空v1历史。没有机械重跑未改的material ledger、copy checkpoint、包工具或旧规模组。

唯一失败为 `test_asset_batch_reads::test_batch_voucher_and_asset_cards_share_exact_amounts_without_double_counting` 仍要求brief collections没有vouchers，这是用户已要求恢复凭证后的过时断言。仅修测试为正向凭证显示、分页计数不重复、消费凭证金额30000／借贷各30000、两资产与diagnostic原文一致；全部原业务断言保留，生产未为此失败修改。单项真实复验1通过、4.73秒，只有 `.tmp/stage9-restored-owner-voucher-assertion-fix-20261003.xml`，exec直接stdout未保存log，未补造log。首轮统一失败保留，不写290整组通过。统一XML SHA `c012f91bd6c7737916dea9a89c3158fd86c36f4c3cf46b416a0cd33103965ddd`；单项XML SHA `d7af796d778482505a0ce95dc959db5c39fa2d161369b2e3d1573e7ee5eb7462`。

同类negative voucher静态搜索仅此当前collection断言；generated前端对top data.vouchers不存在的断言服务于退出重复payload，正确保留。旧 `frontend/tests/browser-t5-integration.cjs`、`browser-t6-interactions.cjs` 及 `scripts/verify_t5_browser.py`、`verify_t6_browser.py` 仍能手动调用，结构过时，但当前npm／CI／tests／主文档没有主动入口。本轮未执行、未删除，也没有证据声明它们完全被Stage9覆盖；此项是静态未验证边界，不扩大本轮门禁。`verify_t5_read_costs.py` 同属旧工具，未据文件名视为当前计时证据。

[新独立归档39份公开合成原件](owner-four-read-groups-20261003-raw/manifest.json)：四scope及资金原审查、全部定向失败／修复、旧4d native和range探针、闭合统一runner／JSON／XML／log及单项fix XML。原文3012747 B、gzip277036 B，总压缩预算512 KiB，额外原文上限4 MiB；秘密标量检查、mtime0、roundtrip逐字节相同及每份原件SHA前后核对通过。manifest SHA `172cf148d2507324a1c53b1993f3a5a0c329171ddd80d23a063f2ff8e0cbebbd`。pstats保留.tmp不纳，DB／WAL／ZIP、巨大source inventory、秘密及私有渠道禁止纳入；所有旧manifest／失败原件保持。

## fa5c当前主48与开发包

固定源 `.tmp/stage9-build-source-owner-four-read-groups-fixed-20261003`，SHA `fa5c7074031755dd8dcf7682198d735c6e6070f704d8c4699a34b5229e489877`，752文件。fresh qualification `stage9-owner-four-read-groups-main48-fix1-20261003-qualified.json` SHA `040587c6265728ac9c2c5220baa6f3c8a090b44b7ad6c54b1b9eafb443288356`：sources／historical_adoption／projections／read_indexes均verified，limitations=[]；integrity920243.91ms、preview780401.39ms、总1702789.61ms。绑定同一main48 root、公司／数据库身份和state `[1,2065,1164,152,48238,0]`，没有向新源继承旧资格。

browser SHA `d4d01399622d20395f22f164843a4b09352fdb5c15aa084cc46757f140f691fa`：五页各30次完整成功、0错误，**FAIL58／150**。纯窗111.9877秒，12个owned合成target CPU计数精确不变、全部resume0，无并行tests／build；runner2054.9402秒是资格及浏览器总过程，不是纯窗。

| 页 | median／p95／max ms | ≥500ms |
| --- | --- | ---: |
| brief | 517.10／575.80／633.30 | 28 |
| funds | 415.90／476.50／494.40 | 0 |
| employees | 354.70／395.90／412.40 | 0 |
| assets | 415.20／474.60／474.80 | 0 |
| reports | 576.20／675.70／694.70 | 30 |

冷态另列：brief672.20、funds920.20、employees884.00、assets1413.00、reports973.70ms；首次打开1778.70ms，browser launch到首次render3866.51ms。切公司unavailable（单个eligible company），不算通过。旧4d完整150／FAIL3（funds1、assets2）与本次完整150／FAIL58分记，不能拼接，也不能仅据两次统计推断源码因果或稳定收益。

native原件 `stage9-owner-four-read-groups-work-fa5c-20261003.json` SHA `8208c2a4dd266c272dca6c7331efd989c12da26155212173f224e7a57824f5bf`：7次warm＋7次work＋brief／report两次profile，共16次只读调用，before／after完全相同，source／book／checkpoint守卫保留，各instrumented响应等于其warm。brief主要SQL193／VM869700／行16338／值字节7507561和report255／948100／24869／10684375与旧4d完全相同。assets为251／569400／8408／6020686；funds_first为171／1227000／11370／3432819，这些诊断工作减少不等于整页时延／CPU／RSS收益。

**原件memo把无filters的funds称为actual browser default-all，这是错误的参数归属。** 实际热刷新使用explicit selected account；first仅首次请求。ResourceTiming有意仅存company／period，未保存完整参数，不能由其query反推all。native funds(no filters)及funds_first均只按各自参数记诊断，原件不改写；未执行HTTP／auth／serialize或active owner_review_request callback测量。

开发包产品只自检一次，搬移包验证PASS：159实际CLI调用、33个历史v1模块、4212软件文件，draft／0；独立GPT AI resume尚未完成。final只读receipt `.tmp/stage9-development-delivery-four-read-groups-fa5c-20261003-fix1-receipt-fix2.json` SHA `e4ca488b1e28555a7cedf20e0bd18cd0cc9f734df44d7d5aa4cc986b1e8f6f63`。前置原件 `.tmp/stage9-development-delivery-four-read-groups-fa5c-20261003-preflight-failure.json` SHA `ea7de2db8821a9f381fcd5a070a5ff33a11f725a2a231e5e40dbcadd624ecbe0`：131个pycache被wrapper额外inventory门禁误拒，未进入产品构建；随后产品自检成功，wrapper旧固定160调用断言失败；只读receipt审查再因过时顶层schema_contracts/draft.json路径失败，最终按actual contract paths及实际159收口，未重跑产品。失败原件及wrapper保留，不将工具假设失败改称产品失败。

[本次独立18份公开原件](owner-four-read-groups-fa5c-main48-package-20261003-raw/manifest.json)：资格、完整browser、pure control／runner／log、native script／JSON／gate／log，以及必要包verification／exit／build输出和wrapper。原文3361974 B，gzip278101 B；压缩500 KiB／解压5 MiB预算内，秘密标量检查、gzip mtime0、解压逐字节及原件SHA前后核对通过。manifest SHA `e3befcea9c3a550b1ed7a7b7d725510c92338957e47dc2f6d49a7340b1d13d0d`。含大源码inventory的preflight及final receipt只保留.tmp原件与SHA引用，不抽字段伪造原件；DB／WAL／ZIP／source大inventory／pstats／私有渠道不归档，旧39份manifest保持。

统一289／1与fix1分记；其余六规模样本、当前证据规模、当前独立AI接续及最终开发交付门禁尚未完成。正式冻结／正式包／入口切换延期。
