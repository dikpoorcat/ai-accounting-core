<!-- @format -->

# 老板看板读取组：2026-10-03

本轮收敛了关账 header 支撑读取批取、资产启用身份与摘要读取两组修复。固定候选 `46f84508afd54165f23af5a83434b1881002ab45490f4358f0c1755c743748be` 的主48月资格核验与纯浏览器、原生工作量测量均已完成：150次刷新全部成功，28次超过500ms，状态为 `over_target`。header 与资产读取减少工作量，未取得整体净提速或性能通过。发表缓存 tuple 未实施。各轮测试分别记录，不相加；插桩耗时不作为页面耗时。

## 关账 header 支撑读取

同一个当前托管只读快照需要多个已关账月份时，原来每个 header 各取 storage root、close marker 和公司／数据库身份。必要的是逐月核对原始 root SHA、逻辑摘要、marker、身份、编码、源版本／highwater、preview 和完整 root 形状；重复的是逐项执行这些支撑 SQL。

`close_storage.verified_headers` 按精确请求月份批取 root／marker，并读取一次实际身份，随后复用原逐条校验。`read_indexes` 权威关账行和 `QueryReads.close_accounting_many` 的缺失 header 接入；支撑数据仅在调用内存在。权威行必须全批 header 成功后才登记，accounting 必须原完整 family／采用／凭证批次成功后才发布 header、slice、位置等缓存。没有批取 family SQL，没有改变完整源算法或历史合同。

五页及详情的 `Dashboard._snapshot` 共享 QueryReads 托管快照，Reports 也有独立托管入口。BusinessQueries、CLI、MCP 是否走新路径取决于实际调用是否拥有同连接的 active QueryReads snapshot，而非协议名称。非托管 BEGIN、无事务、另一连接、固定 v1 reader／registry 保持原独立路径。完整核验、关账核验、维修、备份和恢复仍用各自独立连接及原核验器；本轮仅检查调用链，不能写作这些完整流程已回归通过。

生产专项请求两个实际闭月，另预置 12／48／120 个无关缓存月份，并禁止整 cache 遍历。authority 和 accounting 两路 header 支撑 SQL 均由独立路径 6 条降为 2 条，同快照再次请求为 0。末行 root／marker／family 真损坏、重复失败及恢复重试、重封根后的错误身份或 bool highwater 均拒绝，失败不发布前缀；连接、事务切换及固定 v1 窄 fallback 也有专项保护。这些数字是缓存月份数量，不是完整 12／48／120 月主样本。

旧固定源 SQL 原型使用 `8672404dd3fcb198f80d3583782b98590b8c8358e2c2e354fa3822b71fefa36b`。主 12／48 中 11／47 个 closes 的 header SQL 33／141 → 2，精确 VM 363／1551 → 326／1298，返回字节 1408／6016 → 768／3072。family 原型虽 SQL 11／47 → 1，VM 却 176／752 → 314／1286，返回字节 436593／1868298 不变，因此未实施。初轮代理行形状错误 `KeyError 0` 没有有效样本，失败原件与修正 r2 分别保留。

| 验证轮次 | 实际结果与处置 |
| --- | --- |
| accounting／完整期间范围专项 | 31 passed；归档日志为原 tool session 31897 最终输出的明确转录 |
| QueryReads／read-index／新 header 首轮 | 65 passed、1 failed；精确事实删除仍拒绝，但旧测试预期 `unknown_calculation`，实际为来源身份不一致的 `content_integrity_failed` |
| 固定旧 P3 源单例 | 同样失败，证明旧断言问题，保留日志及 bootstrap 脚本 |
| 修正单例＋header 专项 | 14 passed；保留拒绝并加强错误消息与缓存断言 |
| root 最终 owned／unowned 双范围单例 | 2 passed，41 deselected；独立记录 |

相关命令为仓库虚拟环境 Python 的 `-m pytest`：先 `test_close_accounting_filter.py test_full_accounting_period_scopes.py -q`，再 `test_query_reads.py test_read_indexes.py test_close_header_batch.py -q`；收敛复验为 `test_query_reads.py::test_batch_calculation_still_requires_exact_fact_version test_close_header_batch.py -q`。本归档任务未执行这些命令。

## 资产启用身份与摘要

资产卡片需要启用成员的身份、采用期间和状态，原 unfiltered activation 定位先读取全部成员 outcome，后续金额／卡片详情又做自己的完整读取。冻结采用保存的完整 membership digest 可以证明完整身份目录，但不能证明成员金额或任意 result 内容。

当前生产在卡片列表定位中选择已认证 kind 的 activation owner：闭期完整成员目录先核对 membership digest、身份、seal、依赖和外来归属等，再构造身份事件；开放 owner 仍完整读取成员及 outcome。整个选中 owner 成功后才发布同快照选择缓存，后续金额及最近事件读取仍作完整来源核验。缺少未展示成员也必须拒绝，默认 20、分页、精确卡片和筛选不能缩窄 owner 的完整性边界。

此修复限资产卡片身份消费者；简报／资金／员工／报表没有据此记收益。资产项目 section、summary-only、普通公共 BusinessQueries／CLI／MCP、unowned 和 fixed-v1 保留各自原完整路径。完整核验、维修、备份、恢复与 v1 全流程不由这个身份缓存替代，也未在本组重跑。

当前生产专项使用 12／48 张卡片、两闭月一开放月及相同内容的对象 profile 历史增长，完整返回相等；默认页、类型筛选、精确末卡、下一页及完整目录损坏拒绝另有断言。其工作量为：

| 当前生产 fixture | 12 卡：旧／新 | 48 卡：旧／新 |
| --- | ---: | ---: |
| SQL | 200／186 | 256／242 |
| 返回行 | 1095／1070 | 2957／2896 |
| 返回字节 | 277524／267134 | 908762／874000 |
| result 读取与解码次数 | 52／39 | 196／147 |
| result 读取字节 | 57584／45254 | 227216／178670 |
| accounting slice 读取 | 5／3 | 5／3 |
| typed fact 解码 | 24／24 | 40／40 |
| SQLite VM steps | 43900／43900 | 146200／148000 |

48 卡单批 VM 增加约 1.2%，不隐去或解释为 VM 优化；更少传输和重复解码不等于每种形状的更低 VM 或整页延迟。初次生产 fixture 两条因 strict ReimbursedAsset 的 creditors 要求 tuple、fixture 传入 list 而失败，首轮结果为 75 passed、2 failed；修正实际 fixture 后窄复验曾为 1 passed、1 failed，暴露 VM 增加。保留完整响应、金额和字节断言，改为如实记录 VM 而不要求未经证实的全面下降，最终 2 passed、5 deselected。75 和后续 2 不能相加为一次全组通过。

同源旧固定 snapshot 原型仅是机制诊断，不是当前生产整页测量。初次主 12 原型未复用 owner selection，默认资产 result 读取 76 → 77、字节 69543 → 79927，总返回字节 3506818 → 3550665，出现反增；后续 selection 复用与项目守卫修正分别留存新报告，不覆盖初次结果。合成 fixture 的首次月份选择错误也保留失败。最新成员重复 outcome、金额失败不缓存、闭期更正／历史冻结、其他成员损坏等原型负例与当前生产专项各自保留，不能把旧原型的全部边界冒充新生产已跑的完整验收。

旧固定源复用原型默认资产主 12／48 的 SQL 为 424／1215 → 355／966，VM 为 503800／874700 → 497400／840300，返回字节为 3506818／8321408 → 3469192／8171930，完整 result 解码为 76／292 → 53／197。项目独立 section 却反增：主 12 返回字节 1877159 → 1951617、VM 284600 → 293300；主 48 返回字节 4328550 → 4636845、VM 453900 → 501600。最终项目 guard 恢复原路径，主 48 SQL 同为 920、返回字节同为 4328550、解码同为 242；两轮插桩 VM 453600／458500 仍如实保留，不写所有计数完全相同。

小型两对象 fixture 中，完整正文本来已必读的形状还出现 +79／+582 字节开销，不能声称所有业务形状工作量下降。公共业务 status 共享读取有窄 response／work 对照，真实 CLI／MCP transport、身份认证端到端未执行。旧主样本原型绑定固定源逐文件清单及合成公司，运行前后身份、state、catalog、checkpoint 不变；与 root 同时进行的主 120 copy 不是纯计时背景。

首轮命令为 `.tmp-kernel-venv/Scripts/python.exe -m pytest -q tests/kernel/test_owner_asset_activation_identity.py tests/kernel/test_owner_asset_metadata_scope.py tests/kernel/test_owner_asset_card_page_scope.py tests/kernel/test_asset_owner_outcome_reads.py`；两次窄复验为 `.tmp-kernel-venv/Scripts/python.exe -u -m pytest -q -s tests/kernel/test_owner_asset_activation_identity.py -k complete_card_response`。本归档任务不重跑。

## 发表缓存 tuple：此次不实施

`_verify_current_voucher_publication_rows` 空输入或全部关系缓存命中时提前返回；首次缺失才先遍历请求月份对应的 `_verified_publications` 全 tuple 制造 ID 集合，再逐实际 publication 查询 `_verified_publication_ids` 的精确 posting_period。增长轴是同月缓存发表行数，不是无关月份 cache 数量。

真实登记／发布的小 fixture 先 prime 完整月份证明，再请求一个凭证。同月 12／48／120 条发表行原访问 12／48／120，临时 ID＋精确期间原型为 0，membership／期间查询各 1，返回一致；再次请求均被关系缓存早退。完整月份 tuple 唯一自然 producer 整批成功后同时填 ID→posting_period，ID cache 还有独立成功 producer，但不能反推完整月覆盖。人为清除 ID cache 留 tuple、注入 digest 失败时，原路径跳过而原型重新核验拒绝；这是私有缓存篡改诊断，不能称为真实数据库损坏或任意篡改完全等价。

实际两月 MixedBook 每月 26 业务的调用 trace：简报、资产和报表首次 tuple 均为空；资金／员工各请求 31 个 current voucher，先已有 34 条 period tuple，因此各遍历 34。已有主 12／48 native 原件能证明五页调用 helper，却无独立 tuple 访问计数。默认 20 条可见列表不意味着必要当前凭证核验只取 20。本轮没有四页真实小集合收益证据，故不实施，不记报表收益。CLI／MCP 同样须依据 owned snapshot、已有 period proof 和关系未命中三个条件；unowned、历史 v1／registry v1 小 fixture 保持原语义。

scope 初次错误查询不存在的列、pages 初次违反 builder must-new 的失败均单独保存；修正报告不覆盖原件。没有修改完整期间证明、历史 reader 或 typed 合同。

## 原件与待验范围

当前46候选主48月重新通过注册完整核验，sources、historical_adoption、projections、read_indexes四项均verified，limitations=[]，并经常驻服务真实开放月预览。121,593事实、50,519计算、49,245凭证、47关账和337证据保留；资格通过不替代性能通过。优化前同功能867240候选与当前46的正式前端、默认20条、每页预热3次／测30次对照如下；纯计时期间未运行native插桩。慢样本全部保留：

| 页面 | 前／后中位ms | 前／后p95 ms | 前／后最大ms | 前／后超过500ms／30 |
| --- | ---: | ---: | ---: | ---: |
| 经营简报 | 497.0／515.7 | 575.7／656.2 | 638.1／954.8 | 14／17 |
| 资金 | 356.6／375.2 | 417.0／435.6 | 535.2／460.5 | 1／0 |
| 员工 | 296.0／295.9 | 317.0／316.9 | 317.2／316.9 | 0／0 |
| 资产 | 476.4／437.1 | 575.6／517.0 | 575.7／555.7 | 6／2 |
| 财务报表 | 477.0／496.7 | 537.1／556.0 | 597.4／556.8 | 6／9 |

资产中位与超线次数下降，资金尾部改善，但简报和报表中位上升，总超线27→28；不能归因或宣称整体稳定净提速。当前首次加载4,041.3ms、冷打开635.9／542.9／494.7／716.3／734.6ms另列，切公司因只有一家合格公司未验证。

native在纯计时结束后独立插桩，身份、state、checkpoint与源码前后恒定。下表是工作量，不是页面耗时；报表贡献解码仍为1,004份，上下文仍4条SQL／96行／850字节／0完整结果解码：

| 默认路径 | SQL前／后 | 返回字节前／后 | VM前／后 | 完整结果解码前／后 | accounting slice前／后 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 简报 | 426／285 | 10180408／10175661 | 1435100／1434100 | 1015／1015 | 50／50 |
| 资金 | 347／206 | 5265452／5260705 | 1493200／1492200 | 542／542 | 49／49 |
| 员工 | 239／98 | 4451453／4446706 | 826800／825800 | 101／101 | 0／0 |
| 资产 | 1215／825 | 8321408／8167183 | 875800／839500 | 292／197 | 141／94 |
| 季度报表 | 647／638 | 11083724／11083421 | 1084000／1084000 | 3／3 | 0／0 |

旧main48 brief profile中的538次 `asdict`／约82ms只作为后备线索；当前account_amounts自身没有asdict，尚未证明当前瓶颈，不实施序列化修改。当前46候选主12月未测；此前主12通过属于旧P3源19ae，不能继承为46通过。主120仅进行隔离raw copy，不能记内容核验、预览或性能通过；独立业务、压力、切公司及最终运行包继续待验。

原读取组清单 [owner-read-groups-20261003-manifest.json](owner-read-groups-20261003-manifest.json) 登记原件路径、原字节及独占gzip SHA、长度与类别，逐件解压核对；不含数据库、fixture目录或真实资料。它区分旧final-ui固定源原型、旧P3复现、当时未冻结生产专项和只读运行时原型，不能冒充后来固定的46候选；当前46资格和工作量另用integrated清单。旧baseline、P3、刷新清单及gzip不覆盖。

本清单包含 42 份原件，SHA256 为 `76f74628755e52ec95f9687b5d4c815c02cd65a48343dbe50fdd7277b4dd05b0`。归档脚本为 `.tmp/stage9-archive-owner-read-groups.py`；首次归档操作仅运行字节压缩／解压核对，没有执行 CPU 测试、诊断 probe 或数据库读取。下述新固定源限定验证是后续独立任务，原清单不回写。

本轮新完成原件独立保存在 [integrated清单](owner-read-groups-integrated-20261003-manifest.json)：43份，SHA256为 `5650599c5659e7d88f5228797247adc1a70b4405df4eb07e79e192d1d59178c4`，全部独占gzip、mtime=0、原字节roundtrip通过。包含主48新资格／浏览器／native、source seal、14文件消费者和员工fixture各轮失败／通过，以及明确未执行的copy输入准备和只读审查。资格日志合并在browser.log；消费者实际脚本为 `stage9-owner-read-groups-tests.py`，只有.py／.log／.xml，没有JSON报告。旧42／27／7份清单不回写。完整关账／维修／备份恢复和fixed-v1最终验收仍按实际范围保留，不能以本组窄专项或14文件测试替代。

## 固定源公共消费者与员工测试修正

两组生产修改固定为 `46f84508afd54165f23af5a83434b1881002ab45490f4358f0c1755c743748be`。14文件回归覆盖五页、业务详情、投影修复、内容核验、备份和固定历史的实际测试场景，首轮186通过、1失败；不能据此声称最终大规模关账、恢复和CLI／MCP运输全部验收完成。已通过模块没有重复全套。

失败分类及处理：员工列表已改用 `payroll_list_head_metadata`，旧spy仍挂 `payroll_head_metadata`，观察结果为空。测试改为对准实际列表入口；完整reference分支明确禁用列表捷径，调用原完整 `adopted_head_metadata`，保留开放／关闭月份完整业务response相等和历史line_count范围断言。旧P3内核和原测试实际复现同样失败。随后检查同类测试，两个手写SQLite结构还缺当前选择器必需的身份表、publication字段、完整冻结采用定位和索引；补齐并逐项镜像实际schema，不改生产旁路。

fixture新增的核验stub曾全表读取无关对象，被工作量计数包含；修正为只按本次请求calculation ID、精确 `(close_period,position)` 批取成对来源，保留事实和期间一致性核验。其局部绝对预算仍未通过。repo、旧P3及新固定源的 `dashboard_reads.py` SHA均为 `37d4039d6071d8e1eef647144c6d92f63305ef672e26949d845e2378afba51d8`，并非本轮生产算法退化；补全必要冻结定位后，旧局部schema的绝对VM预算已不适用。本测试退出过期的600000／2500000预算，保留全部业务、身份、修订排除、行数解码，以及 `json_extract <= 3×months`、`48 < 4×12`、`120 < 4×48` 的实际增长守卫。**原600000上限从未通过；五页500ms目标不变。**

| 各轮独立回执 | 结果 | 原件（均在 `.tmp`，不覆盖） |
| --- | --- | --- |
| 固定源14文件首轮 | 186 passed、1 failed，317.99s | `stage9-owner-read-groups-consumers.log/.xml` |
| 旧P3原员工单例 | 1 failed，8.44s，原spy失效 | `stage9-employee-line-scope-old-p3-original.log/.xml/.json` |
| 新内核＋修正单例及两同类文件 | 25 passed、2 failed，101.55s，手写结构缺项 | `stage9-employee-line-scope-new-fixed-directed.log/.xml/.json` |
| 当前员工列表修正单例 | 1 passed、35 deselected，8.03s；原独立日志未记录完整固源来源，不冒充固定源整组 | `stage9-employee-line-scope-corrected.log` |
| 补齐fixture首轮 | 1 passed、1 failed，1.74s，48月VM1148000超原预算 | `stage9-payroll-line-fixture-corrected-first.log/.xml/.json` |
| 窄stub及真实索引 | 1 passed、1 failed，1.65s，原预算仍失败 | `stage9-payroll-line-fixture-corrected-second.log/.xml/.json` |
| 退出过期局部预算 | 本文件3案例通过，1.59s | `stage9-payroll-line-fixture-corrected-final.log/.xml/.json` |
| Ruff等价换行后绑定最终测试SHA | 本文件3案例通过，1.61s；Ruff和diff check通过 | `stage9-payroll-line-fixture-corrected-final-format.log/.xml/.json` |

最终局部工作量与原失败相同，如实保留，不称VM提速：

| 实际selector＋窄fixture核验 | 12月 | 48月 | 120月 |
| --- | ---: | ---: | ---: |
| 每月工资／无关对账业务 | 50／50 | 50／50 | 50／50 |
| 选中head数 | 602 | 2402 | 6002 |
| close-limit解析次数 | 24 | 96 | 240 |
| VM steps | 289300 | 1152800 | 2879800 |

旧P3源为 `19ae7d824077e8e988ffe254120f7d8ee7ed42708c5fdf8fb6eff2ffb3e49941`；新限定验证的生产模块来自46f845，修正测试来自仓库，实际模块路径／SHA前后校验并单独保存，不能称封存源已包含后来修正的测试。没有修改生产或封存manifest。各轮结果不加总，失败原件均保留，新原件不回写此前42份归档清单。

`test_employee_history_pruning_work.py` 只读对照后未重复执行：它检查加入无关已发表费用后，当前／精确员工页的本月付款、月末未付及完整结果解码集合不增长，不能替代本组列表spy或完整head对照。限定组结束后才开始主48纯计时，native随后执行；主48资格已完成但浏览器仍over_target。当前46的主12、主120及独立／压力样本、最终运行包和实际AI验收仍待各自回执。
