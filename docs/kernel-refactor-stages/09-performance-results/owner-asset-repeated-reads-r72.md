# r72 资产同快照来源发现与当前页档案读取

## 根因与范围

只读检查固定 r70/r71 resident 报告和当前调用链。r71 资产页 native 为 461.8–484.0ms，1259 条 SQL、20512 行、10.29MB；r70 为 366.6–411.5ms，1217 条 SQL、11501 行、8.45MB。这些既有 native 数字不证明本次改动收益，也不是浏览器验收。r71 outcome 正文已由 385 行／1.33MB 降为 293 行／277KB；开放发布核验的重复头部传输由独立修复处理。

一次资产页先为全部启用卡片读取完整成员事件，随后为当前页卡片读取最后计提身份。两条路径以同一事务、同一 cutoff 调用 `_frozen_asset_owner_sources`；关账 section 已复用，但冻结声明格式、完整 owner 头部及发布记录仍再次读取和验证。r71 `calculation_identity_headers` 共 4 次、238 行、154782 字节。另有 21 次逐 ID 档案 SQL，返回 21 行／8710 字节；当前页 20 张卡逐一调用 `profile("asset", id)` 是其中的确定 N+1。

完整成员 metadata SQL 返回 1176 行／1322136 字节，其中只有 48 条启用成员与完整读取重叠；其余历史目录仍须核验。141 个 close accounting slices 分属卡片采用叶、启用完整核算及全部 owner 身份核算三个 authority universe，不作为可删除的重复。当前页以外成员的实际事实、成员来源身份、封印、精确依赖、异批采用和完整目录摘要均须保留。

## 处理

当前格式、当前 close/publication/asset membership reader、受控 `QueryReads` 事务允许按 cutoff 复用完整成功的冻结 owner 发现结果。结果仅属于该快照；全部声明、精确来源和发布核验成功后才保存。它不证明正文、直接采用或完整成员内容，不向这些成功集合写入标记。完整事件与 metadata 消费者仍执行自己的采用集合、凭证身份和成员检查。unowned、固定 v1 及其他 reader 保留独立发现路径；不共享跨请求结果，不恢复审查 24 的 close_rows 两段方案。

资产详情选定当前页后，通过现有 `prime_profiles("asset", selected)` 批量读取选中的档案，再按原 `profile` 规则组合冻结字段与当前补充字段。全量金额、计数和筛选继续先计算；默认 20 条、续页、精确定位及关闭期间名称均保持原语义。summary/project-only 请求不扩大资产档案读取范围。

CLI/MCP 业务状态、完整资产事件、engine trace、display、关账、integrity、repair 和 backup 保留各自完整正文／成员核验；冻结发现复用不能使这些调用继承 metadata 正文证明。固定 v1 保留其完整历史 reader。

## r72历史统一组与验证边界

资产新增小型9项回归此前在定向运行中分别通过，Ruff与语法检查是原局部回执；不是287统一组通过证明。随后r72一次17文件组实际283 passed／4 failed／5 record_property warnings，stdout442.29s；JUnit287项、4失败、0错误／跳过，suite442.064s。资产回归已纳入该组，不能抽出局部成功写成整组通过，也不与旧9项相加。

四失败来自公共发布证明链：金额读取旧public ID查找次数断言要求1次、实际2次；报表original_missing／head_period／original_voucher三例正确拒绝损坏来源，但失败后留下新增_verified_publication_ids摘要核验记录。资产冻结发现结果与此摘要集合不是同一种证明，各自保留成功发布、失败不新增及事务退出边界。随后r73统一289项通过其实际范围，原失败保留，见[公共发布证明记录](owner-publication-proof-reuse-r72.md)。

selected-source前后digest均62f4cd7ce77db7b40cbe24ff7900c74ac91d1c990756d49b60040e5ebd1dffdb，完整执行清单观察一致；归档时源码若已变化则另记当前SHA，不冒充执行副本。原始回执及必要源／tests见[统一组失败manifest](owner-open-publication-related-r72-failed-manifest.json)。旧r71记录保留原来源身份；原r72无native／browser结果；当前r73诊断见下列范围，不声称500ms正式达标。

原小样本当前页20张资产档案与无名称往来方两层回退合计至多3条档案SQL；同文本SQL的仪器记录按调用聚合，不把聚合记录误当单个档案范围。其金额、20条首屏／续页／精确定位、冻结和补充名称、cutoff／reader／snapshot边界，只按原局部回执范围陈述，不外推287通过或主规模收益。

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
