# 1de两档大原件资源与前台诊断（测量与只读补证完成）

本文件保留最终结果。逐次原始记录、失败与慢样本已合并至[完整实验归档](../09-performance-history.zip)；链接备注标明归档内原路径，解压后可按原目录核对。

240／1536MiB两档普通服务的verify、backup、restore、restored_verify四个API均已完成。240原v4整轮实际exit1保留，其45次前台诊断及后续只读postcert v2实际exit0分别有证据；1536原v5整轮实际exit1保留，独立第四前台补测已完成，新runner末尾私有JSON记录失败实际exit1，1536只读postcert v1实际exit0。API／前台的completed及后续只读补证不代表原整轮exit0；本轮开发范围已完成，原失败终态和未验边界保留。完整有限文本证据已归档，具体范围与manifest见末段。

固定源 `1de644587eb0dddaee6a09739a825e78fa04f28cabd0c2af047c5378d3dc0b33`，777文件，source-manifest SHA `c2c3b858c0e3c28e338d2ada07d68e9a181516957c080f80f05154e5b2f58ddb`。两档准备分别完成当前三次fresh完整内容核验、原表／身份／业务／BLOB与来源守卫。240接续原合成副本并修正复制权限，1536按既有计划新建独占合成副本；不把准备记成服务资源phase或重建原件。读取汇总SHA `2e5bd93e0a22e301aa6551a8d811f023775e1206f90e26ae8eb4d600529f9f16`，具体数据仍绑定原worker／supervisor／actual终态及各自raw。

| 体积MiB | 普通服务API | 已完成耗时ms | 原窗口实际前台重叠 |
| --- | --- | ---: | --- |
| 240 | verify | 864.6 | 0／5 |
| 240 | backup | 8823.3 | 5／5 |
| 240 | restore | 4706.2 | 4／5，报表没有重叠 |
| 240 | restored_verify | 1051.4 | 0／5 |
| 1536 | verify | 4452.0 | 3／5，资产／报表没有重叠 |
| 1536 | backup | 51945.8 | 5／5 |
| 1536 | restore | 33001.2 | 5／5 |
| 1536 | 原restored_verify | 3718.2 | 原前台缺失；独立补测另列，不填0／5 |
| 1536 | 独立recovered_restored_verify | 4478.6 | 新窗口3／5，资产／报表没有重叠 |

耗时为与前台诊断同窗的HTTP操作wall，备份包括后台job等待。backup由pending回执经jobs确认succeeded且ZIP内容verified；restore及恢复后核验实际完成，四coverage verified、limitations为空。ZIP、业务、实际身份映射及原BLOB的守卫按各阶段回执保留，不能因API成功覆盖最后未成立的整轮守卫。

前台数字顺序统一为简报／资金／员工／资产／报表，单位ms。baseline各页5次，表中为最大；各资源phase原窗口各页1次。240共25次baseline及20次phase刷新，1536原窗口共25次baseline及15次phase刷新，独立新窗口另有五页各1次warm／1次实际刷新。独立补测执行的是新HTTP verify，与原3718.2ms第四API分别记录，不能补成原窗口并发覆盖。它们只诊断资源影响，不增加500ms门槛，不补作c063主450纯计时。

| 体积MiB | 前台窗口 | 五页最大ms |
| --- | --- | --- |
| 240 | baseline | 101.0／101.4／81.6／81.1／99.1 |
| 240 | verify | 100.9／100.2／80.6／81.2／80.0 |
| 240 | backup | 100.3／101.1／101.8／80.1／100.9 |
| 240 | restore | 120.9／121.0／100.4／101.0／81.1 |
| 240 | restored_verify | 101.1／100.9／80.7／80.7／81.2 |
| 1536 | baseline | 101.5／120.4／100.9／100.6／100.6 |
| 1536 | verify | 101.1／123.7／99.8／80.7／100.7 |
| 1536 | backup | 203.1／100.3／183.1／101.6／163.1 |
| 1536 | restore | 181.3／163.2／162.3／226.5／102.1 |
| 1536 | 原restored_verify | 原raw未生成，保留缺失；不是0ms |
| 1536 | 独立recovered_restored_verify | 121.0／144.0／101.9／80.2／98.9 |

0／5表示刷新实际区间未与API区间相交，不能称已观察到该phase并发影响；1536原最后一段缺少前台原件，与已观察但没有重叠不同。独立补测raw passed、五页齐全，新HTTP verify四coverage verified／limitations为空，实际重叠3／5；资产／报表没有重叠，不能写5／5。baseline和单次phase样本规模不同，前后数字不作稳定因果或分位承诺。所有ResourceTiming、逐次slow和失败原件保留。

RSS按同一采样批次分别汇总owned服务树与前台浏览器树，observer不计入服务。下表两个峰值分别取各树在有限观察窗中的最大批次和，不能相加成为同时峰；共享页也不代表唯一物理内存。各phase的原始采样、进程查询时间跨度及attach／exit记录保留。

| 原观察窗 | owned服务树观察峰MiB | 前台浏览器树观察峰MiB | RSS记录 | memberErrors | 最大采样间隔ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 240 v4 | 295.3 | 760.2 | 177 | 24 | 312 |
| 1536 v5 | 374.9 | 754.4 | 483 | 16 | 328 |
| 1536独立前台补测 | 280.9 | 694.8 | 121 | 3 | 282 |

名义RSS间隔250ms、成员发现间隔500ms，fatal sampler error均0；memberErrors及每行JSONL全部保留，不裁剪成无错误采样。1536 restored_verify窗口服务树观察峰374.9MiB，但原前台没有生成，不能把该窗口浏览器采样的0当作完成前台或0ms结果。有限采样存在发现、退出及查询间隔，以上不是全生命周期绝对峰，也不证明Windows全部外部进程树。

240只读postcert v2实际exit0，actual回执SHA `4779316f8bf73c53ff24cbb55e53520041d7b74d2a31bc7fbdb239fb978eb4a1`；原v4 exit1仍保留。原私有helper把合法新增backup request／job／audit三行纳入“整库历史不变”比较，导致最后断言失败。补证精确投影该三行、重新核对原六表守卫及保存引用列，并确认稳定历史、不可变BLOB、退休目录全物理与表摘要以及正常owned清理。补证没有数据库写入、重测API或重做fresh Q，没有把原失败回执改成成功。1536只读postcert v1实际exit0（session12464／chunkdcaf4e），actual回执SHA `0789917746829c63bd200b19feb733f53fecb73873d8de095e756a249e0b1339`。补证核对源与恢复后的immutable／BLOB、源六表及精确三行历史投影、生成列、退役目录／身份／旧件SHA全部成立，database_writes=false；原v5及独立补测exit1均保留，没有重复API或前台测量。

1536独立补测actual回执SHA `feee5cf6917d0e248aa97e942373c45263e3c19d4f27915aa00fb4a1b23ec288`，实际exit1。失败发生在finally的json.dump遇到restore_catalog_stable_before.schema_history.fingerprint的bytes，worker JSON残缺；新phase、foreground、HTTP完成记录、完整RSS JSONL及supervisor均有效，normal owned／forced／supervisor cleanup均为空。这不是新marker失败，不重新运行测量；残缺worker原字节以私有opaque文本保留，SHA `92a8350698fc126f4911ac4eb4350c2c0a9e011b1f7c814dcbbe86de5faaa831`，不修成看似成功JSON。其后的独立只读postcert实际exit0，不改写该runner终态。

| 失败／接续 | 保留的实际范围 |
| --- | --- |
| c063服务v1 | catalog WAL权限门禁失败、实际exit1，未进入资源测量 |
| 1de服务v2 | 旧owner认证失败、实际exit1，未进入资源测量；原凭据已按2026-10-04清理流程随机轮换并退役，不是数据库损坏 |
| 1de服务v3 | 错误要求正常已提交的原catalog WAL为空、实际exit1，发生于新session fixture／host／phase前 |
| 240服务v4 | 四API与45诊断刷新完成，整库历史错误比较导致实际exit1；原worker／supervisor／actual不改写 |
| 240 postcert v1／v2 | 首次只读补证断言失败保留；v2另有真实exit0及完整守卫，未重复测量 |
| 1536服务v5 | 四API完成，原restored_verify前台marker私有harness失败、实际exit1；该窗口缺失不回写 |
| 1536独立前台补测 | 五页与新HTTP verify完成，末尾bytes序列化使worker JSON残缺、实际exit1；有效独立记录与原残缺字节保留，最终独立只读postcert实际exit0，不改写该runner终态 |
| RO探针v1／v2 | v1 JSON尾部literal\\n非法，原件私有保留、排除合法JSON归档；v1 log／helper SHA及v2有效JSON／log保留 |

旧凭据退役记录按自身24b9来源保留，不标成1de业务操作。当前distinct小session catalog使用独立新测试owner映射已有大公司副本；旧目录／旧owner原样，不称旧身份登录接续或凭据重置。两档原大BLOB样本均0 closes，仅证明开放范围；冻结恢复另由[1de非空开发包MCP桥](development-package-copy-sidecars-1de-20261005.md)保存自身证据，不能借给本样本当大BLOB冻结验收。

资源测量、独立前台补测与两档只读补证已完成；240 postcert v2和1536 postcert v1实际exit0，原runtime和独立补测exit1保留，不把“measurements completed”改写成原整轮exit0。[有限文本证据manifest](../09-performance-history.zip "归档内：09-performance-results/resource-foreground-copy-permissions-1de-20261005-evidence/manifest.json")，SHA `57c6be9905fec9ef1a02e2a25ec326e403599a741980574dc49aa7d315435e4f`：117个gzip、18项私有helper SHA、11份实际终态，原失败exit全部保留；归档与独立回读／SHA／逐JSONL／已知秘密及绝对路径检查实际exit0。全部6份JSONL分别保留21／33／15／177／483／121条原记录，memberErrors未删。真实5173、资料与身份未操作，目录／公司draft／0保持；正式冻结、正式包交付及运行切换延期。真人密码窗口、强制崩溃、inflight中断和整轮AI GUI不由本资源诊断证明。
