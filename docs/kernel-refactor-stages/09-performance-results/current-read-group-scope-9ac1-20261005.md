<!-- @format -->

# 9ac1 当前读取组范围诊断（2026-10-05）

本次固定来源为 `9ac1ddcc2ee3b4d5d4758aff091d5b33c12e1f54a482c4f6ab83ecd6853bcd2d`，引用当前资格 SHA `f90b99f83d7b86fb57262209bb9f03876824a0d200a875645833973ec9c90174`。诊断使用新隔离原生服务 host，不是原资格 host；没有重新完整核验、`preview_close` 或业务写入。cProfile、SQLite VM 采样及 observer 均有插桩开销，不是纯计时、HTTP/浏览器结果或 500ms 验收。

首次 observer 在调用原 identity 方法前消耗了 generator，造成工具 `KeyError`；该失败状态和错误原件保留，不能归类为生产业务失败。首次成功的 brief、funds 保留在失败记录中，修复只涉及工具参数保留，后续仅补 employees、assets、reports。五页结果来自两份原件，不拼成单次五页通过，也不累加重跑次数。

资产读取包括 120 个取得结果、当前开放月份的 119 个消费成员，以及 119 个闭月的所有者采用；每个闭月实际读取两个所有者的范围。119 个完整子根约 4.7MB，仍承担独立采用核验。header 重复 238/238，额外约 47KB、SQL 约 2.66ms；当前证据不支持把它列为高收益的安全删除项。报表 937 个重叠来源全部命中缓存，仅新增 66 个，1004 次解码为必要处理，分类引用为 4932 个互异项。返回字节是 Python 值大小，不是磁盘 IO；VM 每 1000 条指令采样。

两份 receipt 均声明业务/身份/状态/历史守卫相等、源码未变、原会话不变、新会话正常 logout、read pool 与 catalog observer 已关闭。合成认证写入单独检查，不宣称目录库物理完整不变。本次只归档并核对原件 SHA，没有独立重跑业务核验或重新哈希数据库，没有读取真实资料、运行数据库/服务/浏览器/测试，也没有修改生产、主阶段、路线图或已有封存档案。

独立 [manifest.json](current-read-group-scope-9ac1-20261005-evidence/manifest.json) SHA 为 `4bb4fea80453437a359a34d6b90f183d1ce032019cd6dff53918bde6352a02f6`。两件 gzip 保存完整分析版观察、scope、计数、SQL、profile 和首次失败状态；内部 ID、私有守卫、资料根、PID、内存地址及秘密经脱敏或哈希表示。清单记录原件/脱敏/gzip SHA、字节数，gzip `mtime=0`，均解压回验相等。helper 不发布内容，只保留私有路径/SHA：当前修复版 SHA 已与完成 receipt 匹配；首次 helper 历史 SHA 来自失败 receipt，当前同路径已为修复版，不能宣称首次 helper 原字节已再次核验。

| 原件 | 原始 SHA256 |
| --- | --- |
| completed（employees/assets/reports） | `e6748b7718f3f82ea4ef52052dc638340c5f5e960ac598d3d82af24b28f3241a` |
| 首次失败（保留 brief/funds） | `8586d7ebff4a4c35ecd7586a514263c681ce3d49e15197afe2bdfcd7977144be` |
