<!-- @format -->

# 9ac1 当前 HTTP 生命周期短诊断（2026-10-04）

本记录封存一个新建隔离 Python host 的插桩诊断。源码为 `9ac1ddcc2ee3b4d5d4758aff091d5b33c12e1f54a482c4f6ab83ecd6853bcd2d`，770 文件源码清单 SHA 为 `5398ce88ec9b8bc1878cc425046f1d9764d42383720e46706a8aca4dfeea2a1b`；引用的当前资格原件 SHA 为 `f90b99f83d7b86fb57262209bb9f03876824a0d200a875645833973ec9c90174`，与此前实际浏览器资格文件相同。原件状态为 `diagnostic_complete`。此次没有重新执行完整核验或实际 `preview_close`，也不是经历约 91 分钟资格过程的原 host；brief 没有当前活动的 `owner_review_request` 定位器。因此这些数据不能替代原 host、原生性能或浏览器 500ms 验收。

## 执行与分段口径

新 host 实际调用 daemon 的 `_prepare_static_runtime`，构造启用 read pool 的 `LocalService`，通过生产 HTTP 上下文及主接口并发读取。五页各进行两对预热、六对插桩读取，每对为 context 与主命令，共 80 次真实 HTTP 读取。随后每页进行一次单独的 cProfile：通过 `app.dispatch(..., response_format='http_json')` 调用，属于原生 dispatch，并未再走一次 HTTP 网络。原件统一记录 85 条请求，其中 context 40 条、五种主命令各 9 条，全部为 `succeeded`；`http_json` 表示响应格式，不能把末尾五次 profile 写成网络请求。

下表只取各页两次预热后的六次主命令，排除末尾独立 profile。`total_ms` 是包装器记录的主命令分段总耗时，不是客户端端到端时间；`_dispatch` 是其中的内部阶段。

| 页面 | total 中位数 / 最大 ms | 内部 _dispatch 中位数 / 最大 ms |
| --- | ---: | ---: |
| brief | 322.61 / 333.33 | 311.93 / 324.61 |
| funds | 258.17 / 289.95 | 246.14 / 281.60 |
| employees | 265.12 / 270.64 | 253.50 / 261.81 |
| assets | 443.36 / 607.61 | 431.85 / 595.65 |
| reports | 387.97 / 405.19 | 373.80 / 389.89 |

热样本各页授权阶段的中位数约 10–14ms，engine 中位数约 9–11ms，响应校验约 0.2–0.6ms、序列化约 0.1–0.3ms。engine 嵌在 `_dispatch` 中，不能把二者加总。插桩、并发 context 请求及运行上下文都会影响结果，不能据此解释原浏览器 host 的耗时差异或声明达标。

## GC、对象存活与阶段内存

归档完整保留 13 个阶段的新增 `sys.modules`、GC 计数/统计/freeze 数量、RSS 及 weakref 观察，4056 条 GC 事件，以及五页 cProfile 的原件所保存累计耗时和自身耗时条目。GC 事件包含预热与 profile，最长观察事件为 21.7976ms；该值不能解释原 host 的全部慢样本，也不建立 GC 与原浏览器超时的因果关系。未改变生产 GC 策略。

从注册 weakref 观察后的各阶段看，`QueryReads` 与 `_Snapshot` 活引用计数均为 0。没有强制 collect；弱引用观察不证明所有不可达对象都已回收，也不证明不存在其他保留对象。

当前 Python host 的阶段 RSS 从静态准备后的约 146.46MiB 增至 profile 后的约 784.17MiB。这只是阶段读数，不覆盖全进程树或全生命周期，不能称为绝对内存峰值。read pool 最多四个连接，每个 SQLite cache 配置 256MiB，是后续排查候选；配置上限不代表实际占用，当前证据未证明它是观察到的 RSS 来源。

## 保全与限制

原件声明 `stable_guards_equal`、`source_guard_unchanged`、`existing_sessions_unchanged`、`new_session_logged_out`、`server_stopped` 全部为 true。业务、身份、状态、历史守卫相等，原会话完整，新会话正常登出；合成目录库的授权/session 存在正常变化，不把它写成物理完整不变。不使用 OS 凭据或 5173 服务。生产实现未改变。

本次工作只归档证据并核对其字段及 SHA，没有重新哈希数据库，没有执行测试、数据库、服务或计时，也不是独立业务复验。上述保护事实依据原始 receipt；未重新计算 770 个源码文件的内容，源码不变事实依据 receipt 和固定源码清单。阶段 9、主阶段文档、路线图及已有封存档案均未改。

独立清单为 [manifest.json](current-http-lifetime-diagnostic-9ac1-20261004-evidence/manifest.json)，SHA 为 `722568208f5c5a7729ffaf1250b94761c2f34cb4212b0f3cc51d94d50a802647`。诊断原始 JSON SHA 为 `4aabbb188e46f91bb4fcfa3a8f851e174121d7e7c5fb5fe3e534a70eb09e0a49`，helper SHA 为 `85d63fbc4533ce2cfa935217add18750e5254baf9c601ab4a52558e928ce7801`，与 receipt 匹配。1 件诊断 JSON 保存为脱敏分析版 gzip，完整保留全部请求分段、阶段、GC 事件及五页 profile；私有根、PID、内存地址与秘密脱敏，before/after 私有守卫结构改为规范序列化 SHA。helper 含合成密码，仅保留私有路径/SHA，不发布内容；资格及源码清单仅保存来源引用 SHA。清单记录原件、脱敏数据和 gzip SHA、字节数及解压回验结果，gzip `mtime=0`，回验相等。不复制数据库、ZIP、凭据或真实资料。
