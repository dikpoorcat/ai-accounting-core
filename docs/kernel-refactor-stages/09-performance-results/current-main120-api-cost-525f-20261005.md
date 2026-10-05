# 当前主120接口与资产行跨度诊断（525f，2026-10-05）

本组完成五页 HTTP pair、五份 cProfile 和五份正常读取池工作量，以及一次资产原生请求的行跨度观察。它们来自新 host、首次调用及显式插桩，不是浏览器500ms验收，不能重建旧host的缓存／GC状态，也不能解释旧整页慢样本的完整原因。当前525f主450仍有3次超线，阶段未完成。

固定源 SHA为`525f02d6718561bb51b22b559b03b3dc6f2aea968eb9a3a13484a532c0cc1d9f`，source manifest SHA为`23cf3745f887ec981bab163c6aa1594b6dda9b162bf1310c652677f8ac4db7c9`。复用既有资格回执`f79923f6313b3be6fd8038810e750caa7328df439eab44d5af0a465683a50ef5`，本诊断没有重做完整资格或实际开放月preview。静态产物与实际计时构建一致。

| 页 | HTTP pair wall ms | cProfile wall／线程CPU ms | 池内SQL／VM | 返回行／值字节 |
| --- | --- | --- | --- | --- |
| 简报 | 421.62 | 541.24／531.25 | 190／820500 | 14219／8245723 |
| 资金 | 275.25 | 364.48／359.375 | 166／1245500 | 10158／3606570 |
| 员工 | 789.97 | 342.25／328.125 | 97／1175700 | 5259／5189473 |
| 资产 | 1105.88 | 714.70／687.5 | 380／1104100 | 14232／11950624 |
| 季度报表 | 644.61 | 605.30／593.75 | 254／983000 | 18592／11598641 |

所有HTTP pair的context和page均为200。简报pair来自v2保留原件，v3显式记录不同host/session续接；不能把五组拼成同一host连续测量。嵌套耗时有重叠，不相加；VM按100条采样，返回值字节不是磁盘IO，Python分配峰不是RSS。cProfile业务差异仅简报`/data/generated_at`与报表`/checked_at`；其余为空。API v3终态`diagnostic_complete`，七守卫全部为真，HTTP进入／退出均10、active=0、池已关闭；实际执行进程90601退出0。v1 TypeError及v2 KeyError失败回执和日志原样留存，合同smoke首次失败与修正也保留，不计作成功调用。

资产行跨度仅一次原生业务请求，不跑HTTP、cProfile或公共工作量。owner完整作用域119个headers／240个subjects共113.91ms，prime跨度93.10ms，119个slice合计10.02ms；merge、update及返回释放各小于0.1ms。adopted-only的另一119个headers／120个subjects作用域为21.62ms。两组分别包含完整owner凭证保护和仅采用头保护，238次slice不能直接判为全部重复。行跨度包括被调用函数，不是独占self时间；线程CPU受Windows粒度影响。业务响应全等、差异为空、七守卫成立、池关闭，实际执行进程84722退出0。

当前候选组正在处理`close_storage`嵌套ChainMap展平与`report_projection`精确已认证checkpoint基准复用；尚未成组验证或测量收益，未改变API、DDL或frozen v1。本组证据支持继续核对prime和报表往来基准读取，不能预写成性能修复完成。main120按需、四分布和资源／前台影响仍待验。

原件目录为[manifest](current-main120-api-cost-525f-20261005-evidence/manifest.json)：25个确定性redacted gzip，9项私有SHA引用（4个helper、5个原始pstats）。JSON保留HTTP、profile top/callers、全部池内工作量、行跨度和失败状态；私有guard用canonical SHA，路径、授权和内存地址脱敏。helper不重复发布，原pstats保留原SHA及私有路径引用；没有归档数据库、凭据或源码快照。

| 原件 | SHA256 |
| --- | --- |
| API v3 | `18c9a2cbf839f8483283a5d7593a76357dc3ce98f8f8e9bf7f04656d2fdea0c6` |
| API v3 helper | `c81a33aa7a2d30d4708d3fd362e64c6543798a223514420b03753d595b7a1c66` |
| 资产行跨度 | `3a2dd0d8bd42cd66ff9e39a84628a796b9fe006ec4fbc167343c7e99084defb4` |
| 资产helper | `6288ef085e23b3c774ad3607b346bcd86c5bd7cb6abfaea3d104af610d363435` |
| 归档manifest | `9b4d38ac68dfc500a160a8a074117f381674f9ac0ec33614fb1a9179877cf272` |
