# r28 简报临界路径与五页传输边界

本组只读诊断使用固定 r28 源 `743fc9f65aa22e7f24d9709c426fcd0c4b77135538f633a333bee7b8bd8399fd`。主 12 月使用 `.tmp/stage9-release-main-12-verified-r28.json` 对应的已核合成库；主 48 月插桩当时使用 r26 已完整核验的同一合成库，v1 SQL 与内容合同文件 SHA 与 r28 相同。该库**随后**由 r28 源[独立完整核验](stage9-release-main-48-verified-r28.json.gz)通过：121,593 事实、50,519 计算、49,245 凭证、47 次关账、337 原件，coverage 均 verified、`limitations=[]`；约 803.9 秒与建库并行，只是完整性结果，不是纯计时。120 月和正式冻结仍未完成。所有测量都在合成资料上进行，且有并行建库，墙钟只用于定位。

## C．三个 worker 与父进程的实际临界路径

两次主 48 月 native 热态响应哈希一致。资料、查重、报表 worker 在提交后约 1 毫秒内开始；资料约 251／268 毫秒、查重约 290／278 毫秒，均先于父进程的准备检查完成。报表约 628／622 毫秒，其中报表准备检查 474／499 毫秒，最后位置计算 70／74 毫秒也在父调用 `position()` 前完成。父完成约 826／836 毫秒、CPU 均约 781 毫秒；提交后的顺序工作包括月度概况 43–56、资金 214–216、工资 34–41、资产 93–98、待收付 76–95、准备 193–223 毫秒。父取 worker 问题／位置包各约 0.01–0.08／0.02–0.04 毫秒，未见队列或 IPC 等待成为主因。

主 48 月准备检查约 171–172 毫秒，`collect_current_readiness` 154–157 毫秒，首次 `prime_select` 119–122 毫秒。1,987 个事实请求有 1,979 个同快照首次载入，主要是历史 `asset_consumption` 1,128 个和当月 `report_classification` 411 个；主 12 月为 673／665 个未命中，其中资产消耗 66 个。默认 100 凭证此前仅覆盖其中 8 个 typed 来源。历史资产消耗虽在金额连续性计算中只用少数标量，现有准备 trace 与来源损坏拒绝仍选择并核验完整 typed 来源；不能只为本页 summary 跳过。不同 worker 有各自事务，也不能把一方成功证明当父进程证明。保留三 worker 与现有核验，不重试先前失败的第四 worker、跨请求缓存或 raw JSON 捷径。

## D．响应转换与浏览器尾段

主 48 月实际 HTTP 默认路径中，简报完整准备响应 882,716 字节，服务 dispatch 749.5 毫秒，服务端 `validate_response` 7.3 毫秒、唯一一次 `dump_json` 5.7 毫秒；报表按需季度响应 20,944 字节，对应 545.1／0.3／0.2 毫秒。资金、员工、资产按需响应分别为 348,414／168,114／190,766 字节，服务端验证加序列化合计约 5.8／4.1／4.7 毫秒。HTTP 直接发送已生成 bytes，CLI/MCP 取原生已验证结果；浏览器 JSON 解析、生成 Ajv 校验、公司／期间／URL 匹配各有独立职责。另用 Python 解析同体量 JSON 得到简报约 10.6 毫秒，但**不是浏览器 V8 的解析时间**。

简报实际父／worker `ForkingPickler` 包为 302、172、5、540、5 字节，逐个额外序列化均小于 0.05 毫秒。cProfile 的 `Connection.recv` 含等 worker 完成的时间，不能当作小包复制成本。来源构建中的 `canonical`／JSON loads 累计时间并非 HTTP 尾部二次编码；没有可安全删除的重复摘要证据。

主 12 月真实浏览器单次热刷新中，context 与主页请求并发，简报另取一次 close-review；没有观察到重复主页请求或 complete→deferred 重试。最后一个主响应 `responseEnd` 至完整渲染并连续两帧完成的尾段为简报 43.0、资金 77.8、员工 50.8、资产 32.3、报表 27.1 毫秒。该尾段合并 JSON 解析、Ajv、URL 核对、Vue 与两帧，无法单独归给 Ajv 或深拷贝。按需详情和分页不属于默认请求。保留所有客户端合同和刷新完成标记；本轮没有已证的大于 50 毫秒且可删的传输重复工作。

浏览器原标准脚本先因合成目录已有第二家公司，在 `benchmark_stage9_browser.py:296` 的单公司数量断言失败，**未启动浏览器、未生成标准报告**。私有 harness `.tmp/stage9-r28-browser-transport-harness.py` 的 SHA-256 为 `cd680a9546d3e5fc3dd8ebf46eb60c94bbf2ca84109f273c8795313a8d8e4c3f`；它仅放宽该目录数量断言为一或两家，主公司身份、请求及响应断言不变。其首个 `--instrument --warmups 1 --repeats 1` 诊断原始报告为 `over_target`，简报单次约 547.6 毫秒；第二个加 ResourceTiming 的私有单次报告为 `passed`，但二者**都不是正式 30 次纯浏览器页样本**，不能覆盖原失败，也不能宣称达标。完整尾段若要分摊 Ajv 与 DOM，需固定 r28 前端构建对应的 V8 profile；未用可变工作树的校验器代码冒充固定版本。

## 原始证据

[清单](grouped-brief-transport-r28-manifest.json)对每份保留原 `.tmp` 路径、原始字节数与 SHA-256、gzip 字节数与 SHA-256；归档逐份解压后与原文逐字节一致。压缩件：主 48 月[三 worker 时间线](stage9-r28-main48-brief-timeline.json.gz)、[准备时间线](stage9-r28-main48-brief-readiness-timeline.json.gz)，主 12／48 月[准备范围](stage9-r28-main12-brief-readiness-scope.json.gz)、[准备范围](stage9-r28-main48-brief-readiness-scope.json.gz)，[五页 HTTP](stage9-r28-http-five-page-profile.json.gz)、[worker 传输](stage9-r28-http-worker-wire-r2.json.gz)、两份[首轮 over_target](stage9-r28-main12-browser-transport-diagnostic.json.gz)和[ResourceTiming passed](stage9-r28-main12-browser-resource-timeline.json.gz)私有浏览器诊断，以及[主 48 月 r28 核验](stage9-release-main-48-verified-r28.json.gz)。原始 `.tmp` 文件未删除。准备范围的额外事实种类插桩受并行负载影响，仅采用 ID 工作量；全部墙钟、单次浏览器与包大小都不是 500 毫秒验收证据。
