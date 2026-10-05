# 同一快照重复读取发布关系

r73同类统一289项已通过，固定候选六路native诊断成功；原r72的287项失败批保留。当前完整核验进行中，未完成browser30次或500ms正式验收。

## 根因和实际证据

开放期金额读取必须独立检查发布来源及其当前凭证，避免缺失指针或冲正内容从查询中消失。r71 加入这项检查后，后续金额、报表和业务详情又读取相同的发布关系；范围复用仅覆盖完全相同的截止月份，直接调用公共函数的路径也未复用成功结果。

48 个月主样本的固定 r71 原生诊断：简报 428／490／473ms，资金 428／362／368ms，员工 260／219／233ms，资产 462／476／484ms，报表 529／495／432ms。与 r70 相比，简报、员工和报表各多返回约 5,049 行及 1.4MiB，资金与资产约翻倍。这是诊断，不是浏览器 30 次验收。原件为 `.tmp/stage9-resident-r71-main48-profile.json`；当前来源完整核验仍为 `not_run`。

## 排查范围与处理决定

| 路径 | 必要检查 | 本组处理 |
| --- | --- | --- |
| 五页金额、默认明细、账户筛选与按需详情 → Journal、BusinessQueries | 实际发布来源、当前头、旧凭证与新复核依据、必需冲正均保留。 | 在同一受控只读快照内复用已成功检查的精确版本关系；选取范围与金额检查分别保留。 |
| 报表 → Reports、report_projection | 开放期前向检查及冻结采用独立存在。 | 公共函数与持有 QueryReads 的调用使用同一成功范围，较大完整范围覆盖较小范围。 |
| 上下文、准备检查、核对摘要 | 不因函数名称相似新增全账检查。 | 核对真实调用链；没有消费本组关系的路径不改。 |
| CLI／MCP、关账、完整核验、修复、备份恢复 | 写事务及完整源核验独立执行。 | 未受控连接不复用；不能将普通页成功范围当成完整源或正文核验。 |
| 固定 v1 历史读取 | 使用其固定规则、解码与来源核验。 | 不套用当前格式的复用；保留独立实现及隔离验证。 |

所有复用在退出快照时清空，失败不发布成功标记，不增加跨请求缓存。确实相同的月度与开放范围须由实际关账边界证明，不能只凭请求月份相同推定。每个版本只保存其实际采用关系，避免将整批映射复制到每个版本导致平方级内存。

## r72原失败身份

r72一次17文件统一组实际287项：283 passed／4 failed、5 record_property／xunit2 warnings，stdout442.29s；JUnit287项、4失败、0错误／跳过，suite442.064s。runner保存原始命令、完整stdout/stderr、XML及selected-source前后清单，digest均62f4cd7ce77db7b40cbe24ff7900c74ac91d1c990756d49b60040e5ebd1dffdb，source_unchanged=true。原r72没有性能回执；r71诊断保留旧source身份，当前r73结果见下列实际范围。

| 问题类型 | 实际失败与根因 | 当前状态 |
| --- | --- | --- |
| 公共ID查找旧次数断言 | test_required_money_header_reuse::test_filtered_amount_reader_keeps_public_id_lookup要求1次，实际相同精确ID集合调用2次。原件保留实际调用；不称业务金额错误，也不先认定重复调用已消除。 | r73相关组已通过；原次数断言失败保留历史身份。 |
| 失败后留下摘要核验记录 | test_report_selector_header_reuse的original_missing／head_period／original_voucher三例均正确抛content_integrity_failed，但关系核验失败后_verified_publication_ids仍保留本次新增摘要记录；摘要成功先登记，失败没有恢复新增状态。 | r73失败原子性相关用例已通过；原例正确拒绝但残留记录的事实保留。 |

摘要核验、前向发布关系、凭证正文和冻结采用是不同证明。只有完整关系核验成功后才能发布本批复用成功；失败不能留下新增摘要记录供后续读取。原4失败保留，不把283通过或后续补跑改为287通过，不与旧r71或资产局部9项相加。

原4回执、runner、完整前后inventory、必要源与17份test副本见[统一组失败manifest](owner-open-publication-related-r72-failed-manifest.json)。源副本记录归档时SHA／bytes／mtime并比较执行前后清单；修复期间已变的源明确标记归档身份，不冒充原失败执行源。raw／gzip双SHA与解压逐字节一致保留，旧r71证据不覆盖。本次仅证据整理，无测试、SQL、service、browser、build或计时。

## r73实际成组验证与原生诊断

生产修复将关系核验失败时本批新增摘要记录撤回，检查点按实际输入限制；先前合法成功证明保留，不能把整个缓存清空代替失败原子性。金额读取的旧次数断言按实际精确ID公共调用范围核对；r72原287项仍283 passed／4 failed，不回写。r73同类统一17文件实际289 passed、5 record_property／xunit2 warnings，stdout431.82s；JUnit289项、0失败／错误／跳过，suite431.601s。新增失败状态隔离等实际用例随XML保存，不与原287或r71各组相加。

runner前后selected-source digest均6a9086e066bd09df5834781fe49da6d4fb7e3333ba6b5569241193a33925f75a，source_unchanged=true。固定隔离候选snapshot SHA420ceb395fc5aec17ddaedf8914c7fc8de640da9f08cb35aded8a3a99a4eb7f4、633文件，候选released合同不代表正式仓库已冻结。归档生产及test优先用固定snapshot逐文件核实际执行before／after，当前checkout若继续变化不冒充该轮源。

| 页面 | r73 native三样本ms | 返回行r71→r73 | 返回字节r71→r73 | outcome装载行／字节r71→r73 |
| --- | --- | --- | --- | --- |
| 上下文 | 11.043／10.027／13.303 | 96→96 | 850→850 | 0/0→0/0 |
| 简报 | 414.179／455.725／416.764 | 24087→22080 | 10932439→10643054 | 1016/994988→1016/994988 |
| 资金 | 277.994／283.806／282.969 | 18674→13626 | 7165849→5731411 | 543/388423→542/387755 |
| 员工 | 211.012／213.626／211.662 | 10580→10581 | 4914091→4914099 | 101/554008→101/554008 |
| 资产 | 394.215／417.633／377.542 | 20512→15312 | 10286522→8786014 | 293/277382→292/276714 |
| 报表 | 445.347／441.788／421.650 | 28098→26094 | 11835731→11546370 | 1007/991627→1007/991627 |

六条native各3样本均小于500ms，是已完成的原生诊断范围；work及profiles另行插桩，不能与native相加。这不是browser30次／冷开／切换或完整规模验收，不能写500ms门槛已通过。员工返回量实际略增1行／8B、SQL239→240，报表SQL645→648；并非所有指标下降。简报／员工／报表必要outcome装载未减少，资金与资产仅各少1行／668B，完整源／正文／冻结采用证明仍保留。

诊断JSON明确current_source_full_integrity=not_run、prior来源r63。主48r73完整核验由主线程进行中，不提前归档中间输出或宣称完成；主／独立其他完整规模与浏览器仍待对应真实回执。后续只读审查发现period digest集合遍历1处、close map全构造2处候选，当前未修改／未计时，是未验证假说，不能写成已排除或已获收益。

本批回执、来源及原生/插桩/profile原件见[r73统一组manifest](owner-open-publication-related-r73-manifest.json)，引用旧失败和执行源补档且不覆盖。所有raw／gzip双SHA、bytes／mtime及解压逐字节一致保存。本次只整理证据，没有生产编辑、测试、SQL、服务、build或计时。

## 首次公共物理头重复搬运与必要业务范围

固定r73主48 actual 2019-12窄audit已证实公共开放范围首次成功路径内部重复物理头读取；后续同owned scope缓存仅剩2／4小行重叠，不能把它当大根因。1006 terminal（984 initial／21 open_replace／1 review_no_impact）是必要全开放前向范围，不能按资金科目或默认20条缩小。简报全月1005凭证金额及非映射金额风险核验必要；资金先命中科目实际504物理候选，全页542结果含独立state／来源，不称542同一凭证集合。报表1004 contribution没有再次完整body解析证据，正文／byteproof／冻结采用证明继续区分。

实际首两查询1006＋1006行／818348B／90800VM；诊断合并物理LEFT h/v字段为1006行／656978B／77700VM，字段精确相同，理论少1006行／161370B／13100VM。这只验证物理字段等价，没有执行合并后的完整证明或净native收益。后续normal版本／current／original关系头仍有重复搬运，但review旧主体和reverse独立义务不能省。whole-month1005 selector与normal1004不等是合法冲正，negative equality结果保留。

r74首次物理头传递已实现并在三个独立受影响回执验证：early2 passed／1 warning、6.04s（当时source与test更早版本）；related57 passed／2 warnings、35.99s（XML35.769）；之后新增真实missing-publication失败后重读work负例1 passed／1 warning、4.15s（XML3.880）。57与1、早期2各自执行范围，不写成同一次60项或58整组。query_reads源SHA305924fa53da1b7696433efa8c2654bbe50baf69114ad87e390c9c0e8b0727ac；test早期／57／新增负例三版本SHA由原inventory分别记录。

私有单模块ABBA修正版两scope分别每侧warm3／实测6、每次新owned snapshot；1006 terminal、1005 relation与1007摘要记录逐字段等价，退出reset。month／through均少1SQL、2009行、462646B，VM少39300／39400，必要结果仍1行／668B／1decode。month wall median68.600→60.201ms，through66.361→65.245ms，尾部明显波动，未证明稳定大时延收益／没有稳定负优化证据。原两个编排失败measured0保留。它仅overlay当前query_reads，其他source仍fixedr73；不是已固定r75 candidate、current完整核验或五页／browser500ms验收。新r75 snapshot尚未创建，等待其他修改统一收敛后固定。

preview活动摘要intent隔离相关组39 passed／91.07s，执行前后7修改文件SHA相同；早期4不叠加。完整preview返回、固定冻结合同与最终manifest事务复验职责不变。bounded旧r73观察没有复现全面变慢，净持有与性能收益未验，见[第二批回执manifest](owner-r75-grouped-read-and-render-intent-bounded-manifest.json)。前端恢复后153通过与DOM恢复前诊断分别记录于[公共HTTP与render](owner-http-request-and-readiness-r73.md)。本批全部原件见[读取与render归并manifest](owner-r75-grouped-read-and-render-manifest.json)，引用旧r73／r74档案，不重复复制旧47份或整源码；此前失败、慢样本始终保留。当前仍draft／0、main12r73 5／150慢、main48r73 79／150慢。
