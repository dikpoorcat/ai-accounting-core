# r71 展示专用读取：资金摘要与资产卡片

银行相关84项已通过且包含原新增9项；资产／前向护栏旧103、完整源旧55、普通页205失败批及随后79分类定向组各保留不同实现／执行身份，不相加。205原组仍200 passed／5 failed；生产current／fixed-v1窄公开守卫恢复既有分类后，79项实际通过，没有重跑205。r71已构造固定隔离候选，主48 native诊断报表仍有528.7ms样本，未达500ms门槛；当前source full integrity为not_run、没有本轮30次browser，正式仓库仍draft／0，最终正式release和包未完成。

## 同类问题、调用范围与处理

| 类型／位置与调用方 | 必要读取与修复 | 本批证明／缺口 |
| --- | --- | --- |
| 简报只消费金额，却构造银行对账展示：dashboard.brief → _funds(summary_only=True) → dashboard_funds.funds | FundsRead初始化、account_summary及完整资金来源／期初／零账户身份先成功，再早返回八个金额字段。跳过其后bank_summary、展示计数／风险／覆盖、空collections与未消费产品摘要。 | 相对固定r70生产diff只增加此早返回及注释；不修改普通资金分支、分类／来源scope／历史分派、模型／SQL合同／版本或缓存。 |
| 银行原行与已入账资金职责不同：普通资金页、FundsRead.bank_summary及核心匹配 | 普通资金页仍保留对账展示；原行999与实际资金1000分别表达，pending保留；核心匹配仍拒绝bank_match_difference。 | 新组显式核对这条边界，不能因简报退出展示而关闭真实匹配门禁。 |
| 汇总只看已显示账户，漏掉省略及零账户：FundsRead.account_summary | 摘要汇总account_rows与omitted_account_rows；余额／变动、当前及冻结资金事件、独立期初及不确定选择、退休账户归属继续核对。 | 普通资金页八金额及显式业务预期等价；未知closing_fen传播None，不把缺项补零。 |
| 资产金额与卡片展示混用同一历史装载：dashboard._assets → _selected_asset_member_heads | 全范围金额、对象枚举和筛选保留；最近计提月份及状态仅在分页后为命中卡片读取。冻结批次通过独立small采用列表、精确owner身份及完整成员目录摘要定位，不解码未消费的历史owner正文；本页最新成员及开放owner仍完整核验。 | 31卡的20条默认页、11条续页、页外精确直跳及全量金额单例通过；1／3个月真实冻结对照metadata装载正文0次，旧完整路径8／20次。该单例与29项窄组不是固定相关整组或主规模时延证明。 |
| 来源缺失被JOIN和空候选隐藏：QueryReads、Journal、BusinessQueries、Reports／report_projection、integrity | 以必要发布前向核current/version，不只验证幸存selector；合法clear lines=[]、no-impact旧版本、未来／闭期边界及固定v1独立规则保留。 | 五文件103项通过实际范围（含v1护栏32），原失败保留。原逆向pointer＋version＋直属lines同时缺失仍source-only verified且repair改变投影的风险已证实；随后完整源护栏55项验证source-only及repair前置拒绝；普通页接入后205原组保留5项分类断言失败；生产窄公开守卫恢复既有分类后79定向通过，未重跑205，不称全类最终完成。 |
| 其他同名summary_only | 工资／应收付／资料的同名选项不属于此次资金早返回；资产有自己的金额与卡片职责。 | 不按参数同名扩大修改，不以本银行84项证明其他消费者完成。 |

当前八字段为total_fen、bank_fen、cash_fen、payment_platform_fen、inflow_fen、outflow_fen、net_change_fen、internal_transfer_fen。summary_only生产消费者为简报；普通资金页走完整分支。CLI／MCP／core／关账／fullverify／repair／backup没有直接消费此简报资金摘要，不据此修改它们的证明边界。

## 实际回执与工作量

实际一次新增窄组：9 passed、2 warnings、stdout16.31s；JUnit9项、0失败／错误／跳过，suite时间15.935s。两条warning是record_property与xunit2兼容提示；XML实际包含两条工作量属性，已读取核对。

实际命令：

    ./.tmp-kernel-venv/Scripts/python.exe -X utf8 -m pytest tests/kernel/test_owner_funds_summary_scope.py -q -ra --junitxml=.tmp/stage9-r71-owner-funds-summary.xml

author记录指定production／new-test两文件Ruff通过，只有tool stdout回执，没有独立ruff log，不补造。已有test_funds_bounded_reads两参数摘要断言更新为八金额，普通资金页及bank_summary未匹配断言保留；原9项命令没有测试这份文件，随后84项受影响组已包含它。

银行受影响组session59274 exit0：84 passed、4 record_property／xunit2 warnings、stdout480.21s；JUnit84项、0失败／错误／跳过，suite时间479.776s。七个文件／node分别为新增资金摘要9、资金有界读取6、银行身份见证21、历史账户身份20、老板简报金额25、期初位置2及简报默认有界单例1。84已包含新增9，不与首批或任何旧组相加。范围仅银行／资金及这些简报边界，资产和正式v1大组不在其中。

实际命令：

    .tmp-kernel-venv/Scripts/python.exe -X utf8 -m pytest tests/kernel/test_owner_funds_summary_scope.py tests/kernel/test_funds_bounded_reads.py tests/kernel/test_bank_identity_witness_scope.py tests/kernel/test_funds_historical_account_identities.py tests/kernel/test_owner_brief_amounts.py tests/kernel/test_dashboard_opening_position_scope.py tests/kernel/test_owner_brief_slim.py::test_brief_default_is_bounded_and_does_not_load_technical_payload -q -ra --junitxml=.tmp/stage9-r71-brief-funds-related.xml

原新增9项的独立16.31s回执和首批源码／工作量证据仍保留历史身份；84项是随后受影响组，不称当前全部修改或五页验收。


| 实际银行原行 | 旧 → 窄VM | SQL | 返回行 | 返回字节 | 必要结果加载／解码 |
| ---: | ---: | ---: | ---: | ---: | --- |
| 9 | 9,100 → 7,800 | 71 → 65 | 127 → 124 | 18,133 → 17,926 | 7 → 7／1377B不变 |
| 209 | 41,300 → 17,200 | 71 → 65 | 127 → 124 | 36,933 → 36,726 | 7 → 7／1381B不变 |

209原行的VM实际降低24,100，SQL少6次；返回只少3行／207B，不声称返回量显著下降。银行原行相关SQL3→2，窄路径两项必要来源读取仍在。七次计算结果加载与解码及1377／1381B保持严格相同，typed_fact_json_decodes均0；收益来自不再聚合未消费对账明细，不是弱化source／header／body身份证明。此为真实合成工作量对照，没有页面时延结论。

## 资产metadata与前向凭证护栏的实际组

资产初fixture、合法空owner／March成员构造和metadata/history/page缺指针真实失败分别保留，不被后续通过覆盖。作者范围及独立静态审查按原写作时间归档，旧“未实施／未验证”不当当前结论。1／3月metadata正文装载与31卡20＋11分页证据保持原范围，不外推主48性能。

| 独立运行 | 原始结果 | 解释及有效范围 |
| --- | --- | --- |
| forward-voucher-guard-initial | 15 passed／38 deselected、1 record_property warning、24.16s；XML23.989s | 首pointer窄组，不是53项完整重跑。 |
| forward-voucher-positive | no tests ran、0.39s；XML0项 | test_publications.py路径不存在，编排失败，不是业务失败或通过。 |
| forward-voucher-positive-corrected | 35 passed、84.09s；XML83.908s | report_projection19／publication_periods12／payroll_corrections4。 |
| open-voucher-work-initial | 1 failed、10.35s；XML10.128s | 缺省sqlite_vm_steps被直接索引而KeyError，不是实际VM超线。 |
| asset-forward-voucher-related | 103 passed、191.75s；XML191.486s | owner_asset_metadata_scope62／publication_v1_voucher_heads32／asset_owner_outcome_reads7／owner_asset_card_page_scope1／open_voucher_scope_work1。 |

工作量修后在103中通过：VM计数使用get(...,0)，保持“大样本≤小样本＋400”阈值。1／80冻结业务均SQL9、返回7行／1322B、结果加载与解码0。省略VM键不等于有精确VM=0回执，counter字典原样归档；这里只证明该开放来源读取不因80个无关冻结业务增加正文或返回集合，不证明所有历史／页面恒定。

固定v1的32项独立护栏覆盖它实际列出的正常／缺指针／旧指针／clear/no-impact／隔离等状态；文件明确不独立覆盖“reverse version不存在”或多来源同时删除。它不是最终正式工厂v1完整大组。

### 完整源风险与随后实际55项护栏

独立历史probe（.tmp/stage9-r71-reversal-source-repair-probe.py/json/log/-command.txt）在合成闭期更正中删除reverse pointer、version及直属lines：source-only仍verified，vouchers由3变2；Maintenance.rebuild_projections返回rebuilt、changed=true、read_repair_revision=1，projections verified。source-only的projections／read_indexes为not_checked，不能说它完成四coverage完整核验。三阶段完整dump保留原漏检身份，修复后的通过不回写原件。

随后current publication与固定publication_v1各自增加verify_open_correction_heads，完整源核验编排在严格核验calculation／publication／voucher映射后，依据真实发布分段重建必要冲正来源集合。缺失来源不能因幸存映射缩小而消失；合法零基线／撤去／无影响复核、开放替换、跨月与连续闭期更正保留。固定v1使用自己的规则与编码；本组不引入正式合同冻结或新业务推断。

实际一次相关组55 passed、无警告、stdout113.48s；JUnit55项、0失败／错误／跳过，suite113.032s。新增完整源20项包含current／fixed-v1合法分段14项、缺reverse version组合4项、source-only与repair前置拒绝且state／投影／来源不变1项、固定v1不调用当前编码／证明1项。另35项为payroll_corrections4、publication_periods12、report_projection19，已在同一次运行中重新执行；不与旧35、103、84或其他窄组相加。

实际保存命令（保留原样，执行命令没有-X utf8；本次证据整理使用仓库Python -X utf8）：

    .tmp-kernel-venv/Scripts/python.exe -m pytest -q tests/kernel/test_open_correction_source_heads.py tests/kernel/test_payroll_corrections.py tests/kernel/test_publication_periods.py tests/kernel/test_report_projection.py --junitxml=.tmp/stage9-r71-correction-complete-source-related.xml

55证明这次完整源／repair边界及列明阳性范围，普通页面pointer／version／lines三者全删覆盖仍在核对；不称整类最终关闭。原103为新增完整源护栏接入前的实现范围，不能替代55，也不证明正式v1大组或页面500ms。四份新API／test在2026-10-02T13:16:08.834472+00:00运行期间只读观察的SHA／bytes／mtime，与本次归档时相同；这是两时点观察，并非执行时导入锁定或完整固定source inventory。其他阳性test仅有归档时间身份。

归档production/source是逐文件读取时的当前SHA及mtime，root仍在修订，没有103执行时锁定完整source inventory，不能声称archive-time快照逐字等于group执行实现或统一固定release。当前test副本也按此时间身份保存，实际组成员由XML核对。

## 普通页独立前向来源与实际最终相关组失败

普通页在已严格核验的当前发布行上，调用current／fixed-v1各自的verify_open_correction_publication_heads，按真实发布分段定位必需闭期基线并严格解码必要结果，重建精确reverse义务；QueryReads共享护栏接入，删除只靠幸存reverse版本发现义务的旧SQL。完整源API继续用于source-only核验及repair前置，不把两种调用范围互相替代。

普通页probe原初版因fixture缺fact_report_classification导致健康quarterly_report拒绝；repair1因冻结分类目录与来源不一致、repair2因尝试原地改稳定事实而immutable_fact拒绝，都没有完成损坏后业务对照。repair3改用production_bundle真实cash_funding闭期更正与amend_fact：健康accounting／funds／quarterly_report／brief均成功；删reverse pointer＋version＋直属lines后，前三者仍成功，brief因monthly_account拒绝。它保留普通页新护栏接入前的真实风险身份，complete=true不表示修复成功。

随后新普通API测试加入真实cash_funding四consumer健康／损坏前后，期望损坏后全部精确拒绝correction_voucher_source_missing；另含current／fixed-v1合法clear／review／replace／move、owned／unowned读取、必要基线严格解码和固定v1隔离，以及真实批量查询与必要正文装载边界。实际10文件一次组为**200 passed／5 failed，stdout371.84s**；JUnit205项、5失败、0错误／跳过，suite371.619s。五失败均为test_publication_v1_voucher_heads的缺reverse head四模式与移动后旧reverse version单例：仍拒绝，实际reason为correction_current_reversal_missing，旧断言要求current_reversal_head_mismatch。不能将200通过写为205通过、整类最终关闭或仅补5后原整组成功。

runner保存实际命令、完整stderr/stdout、XML和selected-source前后628／629文件清单。before digest为48d385b95ba992f6e598e50ab420098513311152450c5310e230ff7170b4837f，after为2c2d4aa0067a574d1a637bae08648dbbe9b2400472e49ea1844de29c01a8d315，source_unchanged=false。变化仅新增frontend/tests/local-api-proxy.test.mjs及修改frontend/vite.config.ts；列明后端与测试前后SHA相同。这仍不是全候选固定验收。归档逐文件另比对当前hash与两清单，若归档时已修订则保留不同身份，不冒充执行源码。

银行84、资产旧103、完整源旧55及本轮205是不同实现／执行范围；205重新执行其中部分源码与用例，不能相加。原103为完整源guard前，55为普通页新API前；205原失败保持不变，随后79分类定向通过；正式v1／当前固定源码完整核验与浏览器500ms／最终包均未完成。

## 分类修复79项与固定r71诊断

两窄公开守卫仅将缺当前reverse head／旧reverse head的reason恢复为既有current_reversal_head_mismatch；完整源守卫自身correction_voucher_source_missing分类不改，也不改旧测试断言。实际test_open_correction_publication_heads47＋test_publication_v1_voucher_heads32一次79 passed、stdout92.89s，JUnit79项／0失败／错误／跳过、suite92.601s。runner selected-source前后digest均08d4bcb08b8326f10fc4c54f7b85e294d64d385bb36a40cc73ea29a866578dd8、source_unchanged=true。它是生产分类修复定向回执，不回写此前205原200／5失败为成功，不与55／103／84相加。

隔离candidate构造日志、provenance及snapshot清单实际保存r71固定源SHA11a1461acf5d0ca8f8902a1a521edac8464c382b38a2586ea0851d50c11669ba，631个selected文件。candidate内合同released／1属于隔离来源，不是正式仓库冻结。归档使用这份固定snapshot的生产／test副本，并逐文件核对snapshot清单及classification执行清单；后续checkout继续同类排查不改变该身份。

主48固定r71 native每路3次，另存真实work和6profiles，context约11ms。五页实际样本（毫秒）如下：

| 页面 | native三样本 |
| --- | --- |
| 简报 | 427.891／490.029／473.095 |
| 资金 | 428.400／362.464／368.303 |
| 员工 | 260.097／218.811／232.601 |
| 资产 | 461.793／475.806／483.967 |
| 报表 | 528.704／495.180／431.701 |

报表有≥500ms，不能称native全部通过，更不是五页browser通过。profile/work为插桩诊断，与native样本不是同一计时；不相加。JSON明确current_source_full_integrity=not_run，prior verification来源r63，不能把prior fullverify变成r71 current证明。本轮无30次browser／冷开／切换验收。

公共前向守卫出现重复源header与publication关系证明：root同类定位为五页约额外5049或10100返回行、约1.4或2.8MB，原SQL／逐次计数随JSON保留。合法更正必须读取必要基线及发布关系；扩大扫描与重复证明的量需要继续收敛，当前正在全局五页及core／v1同类排查。该量是诊断定位，不能把全部页面耗时归给它，也不以删除必要来源证明作为修复。

## 业务边界与未验证

新组覆盖bank／cash／platform合法转款与显式金额：total1700、bank900、cash200、platform600、internal100；普通资金页八金额相同。资金outcome重复键、fact seal、publication、零账户独立state损坏，在先前已读及重新读取情形均拒绝。

未知期初用真实严格source结果核验后的下游uncertainty替身检查None传播；它不是实际损坏fixture或工作量测量。银行相关受影响组已按上述84项实际通过；历史／固定正式v1完整矩阵、当前固定源码完整核验与性能收敛、后续最终合同及当前固定源码浏览器性能仍由root统一收口，本组不作它们的通过声明。r70原主48浏览器37／150失败不能转为r71通过。

原新增9项说明、stdout／XML、实现／新测试／更新断言及固定r70前版本、只读生成diff见[首批manifest](owner-display-read-scope-r71-manifest.json)。随后84项原stdout／XML、当前作者范围说明及本集中记录修订见[银行相关组manifest](owner-display-read-scope-r71-related-manifest.json)，引用首批而不覆盖旧证据。资产metadata／前向护栏回执及当前来源快照见[资产护栏manifest](owner-display-read-scope-r71-assets-manifest.json)，引用先前清单而不覆盖旧证据。完整源55项及原三阶段probe见[完整源护栏manifest](owner-display-read-scope-r71-correction-manifest.json)，不覆盖此前各批。普通页失败205项、前后inventory及四轮probe见[普通页最终组原失败manifest](owner-display-read-scope-r71-forward-final-failed-manifest.json)，旧完整源probe只引用上一清单、不重复归档。79分类回执／候选身份／固定native诊断见[分类与native manifest](owner-display-read-scope-r71-classification-native-manifest.json)，旧失败组不覆盖。各批保存raw／gzip双SHA、bytes和mtime；生成diff无原文件mtime，明确来源双SHA。先前document_sha256保留当时版本，本批保存当前修订。全部exclusive gzip解压逐字节一致。本次仅归档，无SQL、测试、service、browser、build或生产编辑。
