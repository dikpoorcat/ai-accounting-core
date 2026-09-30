# 已保存 JSON 的 SQL 筛选解释

同一保存原文若含重复键，SQLite JSON1 使用先出现的值，而 Python 解码使用后出现的值。先按 JSON 路径做负向筛选、再对命中行核验正文，会漏掉本该命中的来源。固定 r30 源码在新建合成库的真实全局清偿列表中，接受了首个 `values.obligations=[]`、末个为非空且修改行摘要的结果；当前入口拒绝 `content_integrity_failed`。原始复现为仓库 `.tmp/stage9-g-fixed-r30-negative.json`，源码来源由该文件记录。

`QueryReads.settlement_subjects` 的守卫限定为原负向筛选依赖的 `values` 与 `values.obligations` 路径：在原索引候选查询之前检查这两个键的唯一性与容器类型，包括 JSON 转义后的同名键。它不认证其余 outcome 字段、不填充完整结果核验缓存，原索引候选、后续解析和摘要证明保持原职责。没有命中的无关字段损坏仍由完整核验发现。`BusinessQueries._settlement_collection`、资金结果、实体 profile、工资已采用头行数及重复检查 manifest 另按各自实际 SQL 筛选范围核验，固定 v1 重复检查解码单独验证。

新合成真实入口回归在 `tests/kernel/test_stored_json_selection.py`：普通与转义的重复根键/嵌套键、未改与重写行摘要、全局与指定期间清偿、工资头、员工 profile、重复检查的 current/v1、首次期初确认、合法非规范 JSON 等共 17 项通过。旧版复现源见 `.tmp/stage9-g-fixed-r30-negative.py`，完整定向命令为 `.tmp-kernel-venv/Scripts/python.exe -m pytest tests/kernel/test_stored_json_selection.py -q`。

只读 12/48 月合成库的实际 SQL 工作量记录在 `.tmp/stage9-g-settlement-actual-r31.json`，测量脚本 `.tmp/stage9-g-settlement-actual-r31.py`。原选择分别约 0.54M/2.14M VM，路径守卫额外约 0.64M/2.63M VM；这是明确成本，不能把其 CPU 时间当作纯浏览器验收。守卫没有加载全部 outcome 正文到 Python；如未来需要缩小候选范围，必须继续覆盖完整原负向选择语义与跨期采用。
