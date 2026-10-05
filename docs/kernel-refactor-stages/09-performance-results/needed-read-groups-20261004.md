# 第9阶段：必要读取与局部循环收口（2026-10-04）

第9阶段仍未完成。最新封存源为 `.tmp/stage9-build-source-lookup-read-group-20261004`，source SHA `f105b8822823be855cec132845a743e1926a8d65c996c5da9ab384ee49081023`，manifest SHA `c449feb8e31ee6824c0bfb5ebb86cbbd767889fb09fd599afd3b54e16bcb97f0`。本组在1836的简报／gross复用和结算密集表示基础上，只保留资产同snapshot余额核验复用、carrying keys集合、filter显式循环及group内目录描述符一次索引。不改schema、wire、固定v1、正式版本、现有5173、资料根或身份。目录与公司保持draft／0，正式v1、正式交付及切版延期。

1836的主12／48各150成功且全部<500ms、主120的150成功／0错误但FAIL36／150（资产30、简报1、报表5）仍只绑定[原两组记录](two-read-groups-20261004.md)。543回归、新45消费者、160实际CLI开发包及独立38 MCP只读均保持原源码与范围，不能改成f105新验收。旧失败、首次错误及全部慢样本原样保留。

## 根因、同类范围与处理

1836主120ResourceTiming显示资产API median563.25ms、render tail median33.25ms，简报API354.95ms／tail34.20ms、报表API433.25ms／tail30.80ms。context与页面请求重叠，不累加；这些说明等待主要在API，不构成独立CPU归因。未据此删页面职责或有效owner待办。

| 根因或候选 | 同类排查与实际问题 | 保留修复／结论 |
| --- | --- | --- |
| 资产重复余额核验 | 同snapshot的balance_totals已登记准确asset／month成功scope，_assets再次直接验证同范围；写入后的engine核验、无owned reads的fallback及完整verifier职责不同。 | 进入已有QueryReads.verify_balance_periods；成功scope精确复用、失败不缓存，不新增跨请求缓存。 |
| 资产集合反复遍历 | acquisition effects逐项执行key in asset_keys.values()；全源码同类membership检索的另两处仅为三／四项常量集合。 | carrying_keys集合只构造一次，用于原余额输入和相同membership谓词；不批量改正常values循环。 |
| filter生成器开销 | 两个prime group已认证filter并复用exact snapshot选择；476次调用不表示完整filter重复算476次。当前close、duplicate、classification消费者及fixed-v1分派均排查。 | 两处all(generator)改显式for／False／True；positions算法、cache、认证与未知语义不变，不增加bigint或持久缓存。 |
| group目录二次线性查找 | _buckets_rows／_buckets_rows_many先枚举present再逐桶从目录开头next；完整256桶也走该路径。单桶management／direct reference、family decode及完整field读取一并排查。 | 调用内bucket→exact descriptor索引一次建立，setdefault保留首次命中；None缺桶与Ellipsis默认direct查找分别保留。目录／块SHA、承诺、顺序、计数、多重集及长度核验全部保留，失败不发布前缀。 |
| owner kind提前缩窄 | frozen adopted叶和publication不独立绑定kind；mutable calculation／subject kind一致不能证明冻结分类。 | 拒绝运行时提前过滤。完全认证238个owner还需6,618,896字节保存结果及事实证明，不是假定免费；稀疏scope仍为未验证候选。 |
| adopted-head／无voucher短路 | 选择含独立冻结来源、撤去、缺current与未发布lane；只按当前候选／voucher会漏消失原source、沿旧voucher和新采用basis。 | 未做SQL改写，不删missing-source lane，不把UNION ALL单独当等价；完整发现与精确核验保留。 |

## 简报、报表及解码的负面结果

简报account_amounts实际消费完整outcome lines，首屏20条不能替代月度金额核验。无独立anchor路径即使raw SHA相等也需重复字段检查，raw-hash／JSON1抽lines或extra-ignore validator没有等价证明，本轮未实施专用窄行解码。

报表年初与期末party nets是两个cutoff，已有owned-snapshot缓存；12次classification读取各411个，4932个ID全部不同，没有跨月header重复。开放月RECLASS与CASH／PROFIT的union仍为2067／2436行、203253／238421返回字节、1004／1005凭证；独立缩party会使现金利润再读共同source，不能从938条RECLASS推算大幅减少贡献正文。保留全月选择证明、原结果字节认证及不同消费者范围。

directory JSON微测覆盖119个不同合成正文、约4.63MB，普通from_json候选约4.9ms局部收益不代表profile的85ms均可省；escaped surrogate、深度及错误类型／消息存在差异。未替换parser，未触碰unique stored JSON或fixed-v1；request memo的跨月重复收益也未证实。

报表贡献native解码四候选经过335项有限接受／拒绝及JSON值矩阵，无观察差异，但1000次、11轮轮换微测全组未见稳定收益。实际2行输入原8.5964ms，四候选分别12.6857／12.9585／10.8999／9.7813ms；40行原73.6340ms，最佳候选仍75.3844ms。拒绝该组候选，生产_decode未改；不从微测推算整页时延。selected_open_line等局部k×n循环已排查，当前实际贡献小，不为2行引入索引缓存；合法大批量仅列未来按实际工作量判断。

## 工作量与验证证据

f105五页实际插桩与1836 baseline对照完成，资产少5 SQL、9行、37,002返回字节及14,400 VM，其余四页全部work counters相同。资产结果仍485行／527,255字节／485 decoded，普通JSON仍2904次／9,362,144输入字节；没有减少必要结果解码。五页strict business SHA在baseline native、current native及f105插桩之间逐页相同，before／after source、DB／WAL、format／身份／state／history守卫为true，read pools已关闭。SQL返回字节不是磁盘IO，VM为采样步骤，Python分配peak不是RSS；资产instrumented1370.38ms不作为纯时或500ms承诺。

c4e8是descriptor索引加入前的中间源，SHA `c4e83cabb49e1bbd4b8d92a79a21111edf3a7938d567384d3056165cb9613e7a`。早期paired插桩观察到相同资产counter差值，作为历史原件保留，不将该receipt改标f105。f105与1836的源差异回执独立绑定四个文件：三份生产文件仅允许本组函数变化，其他AST相同；brief测试仅格式换行AST相同，另754文件字节相同。新增descriptor测试在f105封存之后，当前工作树不冒充完整758文件与封存源相同；三份生产文件仍逐字节等于f105。

纯内存filter实验包含empty／positive／negative／owner-month-mix及实际false-positive形状，240 keys×119月、9轮交替旧新顺序，72对比较均新循环较快，boolean／hit及完整positions cache相同。cached位置四类new／old median比例0.380370／0.680893／0.498051／0.443215，public路径0.856731／0.919898／0.868492／0.871680。局部稳定收益支持保留循环，不承诺页面500ms。

已认证合成256桶实验，former present枚举＋逐桶next共33,152次描述符访问，current group及cross-group各256次，完整field含初始枚举512次。叶、位置一致，空scope／缺桶／重复请求及冲突descriptor正反排列保留首次命中成功／拒绝语义；这是访问次数证据，不推算毫秒。新增 `tests/kernel/test_close_descriptor_work.py` 使用32／128／256实际认证目录，断言访问≤3N，完整leaf／position、direct／group／public read_section一致及empty／missing／duplicate／tamper，3 PASS；三份生产文件和新test Ruff通过。来源为root工具stdout，未编造XML或持久测试log。

| 验证组 | 实际范围与结果 | 边界 |
| --- | --- | --- |
| 资产／余额 | 13 PASS，31.39秒；完整／fallback金额、旧member损坏、closed correction、batch／opening、准确成功／失败scope及分页冻结名。 | 独立本组，不相加成完整回归。 |
| filter消费者 | 31 PASS，18.28秒；exact absence／false positive、损坏／遗漏filter、失败无前缀发布、state-only adoption及fixed-v1。 | fixed-v1源未改，不等于当前完整历史全套。 |
| descriptor group | 25 PASS／14 deselected；cross-period、owned scope、失败group、validator失败、current eligibility、reference与v1 fallback。 | 没有main、preview、服务或完整verifier。 |
| descriptor增长回归 | 新增3 PASS，32／128／256实际认证目录访问≤3N及完整值／位置一致，包含缺桶、重复及损坏拒绝。 | 新test不在f105封存inventory；不替代旧25行为组或完整回归。 |
| 被拒绝解码候选相关组 | 40 PASS，61.62秒；贡献bytes／index／v1、repair version、stored JSON boundary及party summary。 | 生产未改，有限335矩阵与微测单列，不累计为69／109全套。 |

## f105纯native顺序窗口

同一隔离主120来源分别运行1836 baseline与f105 current；每入口3 warmups＋30成功样本，无profiler、SQL／VM观察器、HTTP、认证或渲染。五页business SHA逐页相同，source inventory、files／身份／state／history守卫为true，两次read pool均关闭。当前owner_review_request absent，未覆盖prepared owner TODO；历史1836完整资格及preview仅为来源，不称f105重新执行。全部warmups、样本和慢样本保留。

| 入口 | 1836 median ms | f105 median ms |
| --- | ---: | ---: |
| assets | 509.07455 | 502.4137 |
| brief | 343.136 | 345.1095 |
| reports | 396.06525 | 384.2977 |
| funds | 237.3393 | 238.10505 |
| employees | 256.5187 | 260.12815 |

资产max由537.2504升为569.134ms，其他页面亦未全面下降。顺序窗口有限观察不证明稳定因果收益；native与浏览器数字不能直接相减。当前没有f105 prepared浏览器500ms结果，主120性能问题仍未关闭。

## 归档与剩余范围

[本组19件诊断证据清单](needed-read-groups-20261004-evidence/manifest.json)保留公开审计文本、两次native全样本、f105插桩／比较／源差异回执、c4e8 paired插桩与源差异、纯内存微测及资产／filter XML，gzip mtime=0，原path／SHA／长度和压缩SHA绑定，解压回环相同；清单SHA `17ea3df6726dbc419a74dc6e787725ca34f2792da3a04ac17fd0761c7cc84243`。未发布数据库、ZIP、helper、完整source inventory、私有CPU／环境快照、凭据或PID；不将私有大日志装成raw。原two-read归档不改。

f105仍需按明确范围完成新的prepared实际preview／浏览器验收；不能用native、源差异或历史Q生成新的签名intent。当前没有重跑f105完整Q／actual preview、fresh消费者或开发包，历史1836资格、543／45／160／38仅按原绑定引用。独立12／48／120、压力12、按需详情及公司切换仍未验。240／1536MiB旧24b9结果及此前改动风险桥接保持原范围，不扩成f105新规模通过；fixed-v1、AI写入／真人批准、正式包、真实升级也不新增通过结论。阶段9、46项及编号外仍分别保留完成、排除、延期和未验，不提交、不增加批准流程。
