# r62 五页刷新、请求与响应校验路径审查

## 范围与证据边界

本次沿五页刷新、共享上下文、月度核对、业务详情和只读 HTTP 响应路径检查重复工作。请求审查阶段仅阅读源码、固定静态包、已有测试及 root 后续提供的实际请求诊断。随后根据确证的月度核对同模型重复，获授权窄改内部投影／公开响应验证职责并运行相关定向测试；没有修改前端、改变默认显示／完成条件或开展新的纯计时。

当前性能依据为 [r61 纯浏览器回执](../../../.tmp/stage9-owner-main12-browser-r61.json)：12 月合成企业、12,000 项业务、五页共 150 个成功样本。简报 median 456.3ms、max 540.1ms，2/30 次达到或超过 500ms；其他四页最大值分别为资金 277.6ms、员工 317.3ms、资产 217.2ms、报表 473.2ms。该结果仍未达到五页全部样本小于 500ms 的目标。

固定源码目录为 `.tmp/stage9-build-source-owner-r61-release`，其 [source-manifest.json](../../../.tmp/stage9-build-source-owner-r61-release/source-manifest.json) SHA 为 `6d9951d3660ae4d0c81bc7da6241085a69a03ab8d8640a0d984ef263eba637b7`。此次浏览器实际使用其 `src/ai_accounting/static/dashboard`，不是同目录中另存的 `frontend/dist`。实际服务的静态 chunk 已核对到并行请求及 API 路径：

| 页面 | 实际服务的 chunk |
| --- | --- |
| 简报 | `BriefView-BdUmfROR.js` |
| 资金 | `FundsView-CwBN2vbY.js` |
| 员工 | `EmployeesView-D4oW0gQF.js` |
| 资产 | `AssetsView-Cwlr_MKz.js` |
| 报表 | `ReportsView-DJOuiyI-.js` |
| 公共请求／上下文 | `main-D4r8ChYH.js` |
| 共享业务详情 | `BusinessStatusDetails-DPyfY9Vy.js` |

可读源码位置使用当前 `frontend/src`，并与上述固定 served chunk 的请求路径、`Promise.all([main, contextGate])` 及预取消费行为对应核对。不能将当前源码本身视为另一次固定构建或浏览器验收。

## 五页稳定选择下的热刷新

以下调用数先由源码预计，后在固定 r61 服务的 r62 诊断中得到一次热刷新及一次 warmup 的实际支持，前提为同一公司、有效且不变的月份／季度，没有并发点击或资料版本变化。它们不是 r61 纯计时回执中的实测请求次数，也不推广到全部选择交错。

| 页面 | 预计看板请求数／时序 | 源码位置与处理决定 |
| --- | --- | --- |
| 简报 | 3：context、brief、close-review 并行 | `BriefView.vue:loadData/refresh`；月度核对通过 `prefetchCloseReview` 提前请求。主页面等 main 与 contextGate，核对组件消费已有 promise。保留核对结论及上下文完成条件。 |
| 资金 | 2：context、funds 并行 | `FundsView.vue:refresh/loadFunds`；context 对象变化 watcher 在同期间且已有 activeRequest 时不另起请求。账户／银行筛选变化是新的查询范围。未确认同范围热刷新重复加载。 |
| 员工 | 2：context、employees 并行 | `EmployeesView.vue:refresh/loadPeriod`；watch 关注公司 ID、期间与筛选，而非每次 context 对象替换。工资来源展开、继续查看才另发精确请求。 |
| 资产 | 2：context、assets 并行 | `AssetsView.vue:refresh/loadAssets/synchronizePeriod`；同公司 ID、期间与筛选不变时 context 更新不触发另一轮加载。资产／项目直跳及分页保留独立范围。 |
| 报表 | 2：context、quarterly-report 并行 | `ReportsView.vue:refresh/preview`；同季度主请求不先等待 context；提交显示前校验期间仍有效。生成、任务查询、下载不属于默认刷新请求。 |

共享 [useDashboardContext.ts](../../../frontend/src/composables/useDashboardContext.ts) 的非强制读取复用已加载 context 或 pending promise；明确刷新使用 force，取消旧请求并取得新月份列表。`App.vue:watch(context)` 在原位刷新时复用返回 context，不再发起 context fetch。每页仍检查请求代次、选择范围及取消状态，避免旧公司或旧月份结果覆盖当前页面。

强制连续刷新可以取消并替换前一轮请求；公司切换、无效期间纠正、保存公司恢复可能产生不同选择的后续请求。这些路径不能仅以请求数大于上表判定为重复。当前审查没有取得这些路径的真实网络轨迹，也未证明全部 watcher 交错都无重复。

r62 实际请求诊断已按原始顺序核对并归档：[manifest](owner-r62-refresh-requests-manifest.json)、[原始 JSON gzip](owner-r62-refresh-requests.json.gz)、[原始日志 gzip](owner-r62-refresh-requests.log.gz)。其 `status=passed`、`sample_scope=diagnostic`、`warmups=1`、`sample_count=1`；这些插桩样本不是性能验收，不能用其中单次小于 500ms 的数字改写 r61 超标结果。

原始文件共有 105 条 HTTP 记录与 37 条 dispatch。限定 browser 阶段后，看板 HTTP 与 dispatch 均恰好 36 条，全部 HTTP 200：context 16、brief 4、close-review 4、funds 3、employees 3、assets 3、quarterly-report 3。37 条 dispatch 中额外的一条是 setup 阶段 `preview_close`，不能算页面重复；其他 HTTP 包括静态资源及登录过程，也不能全部计为看板请求。

按 dispatch 实际起点排序，初始 context 在 67661.001–68213.772ms，随后 brief/review 在 68224.563/68225.452ms 开始。之后简报还有该页 navigation、warmup 和一次热刷新，所以总共 4 组；其他四页分别 navigation、warmup、一次热刷新，共各 3 组。navigation 先完成公司 context 初始化才启动 main；它不属于同一热刷新中的串行重复。

各页最后一次热刷新的 context 与 main 起点差分别为简报 0.767ms、资金 0.873ms、员工 0.941ms、资产 0.828ms、报表 0.724ms；简报 review 与 context 起点差 1.525ms。各组执行区间重叠，证实本次热刷新没有先等 context 才执行主读取；也没有 Panel 再发第二个 close-review。此次覆盖的稳定选择刷新中，实际请求数符合上表的 3／2／2／2／2，没有额外同范围看板请求。

## 月度核对、详情与跨 endpoint 内容

- `api/closeReview.ts:prefetchCloseReview` 启动一个请求，并将失败转换为可消费的 settled promise；`CloseReviewPanel.vue:loadSummary` 复用公司／期间一致的预取结果。预取失败直接显示错误，不补发相同请求；不匹配的旧公司预取才被忽略。组件没有固定轮询或技术详情读取。
- 固定后端 `close_review.py:CloseReview.read` 的闭期路径读取已认证 header 的 `owner_review` section；开放期读取活动预览中已保存 review，并比较当前业务／修复版本。它不重新生成整份关账准备或五页投影。核对金额可能与简报金额相同，但分别代表已保存核对结论与当前经营投影，不能不经版本判断互相替代。
- `BusinessStatusDetails.vue:toggle/load` 首次打开时才请求一个精确业务；已经成功加载且选择未变化时再打开不会重复请求。分页有独立游标／版本；选择改变立即清空，资料版本失效要求刷新。`api/businessStatus.ts` 的动态 validator import 是首次详情的模块加载，不是重复 JSON 响应请求或重复 Ajv 调用。
- context 是公司和可查看月份／季度目录，没有金额或整月来源正文。固定 `dashboard.py:context/_periods` 按真实索引期间枚举，主页面 `_snapshot` 再确认所选期间存在；两者是不同事务的目录和精确选择检查。刷新要求更新月份列表，不能省掉 context 请求，也不能把旧目录结果当本页完成依据。
- 员工工资来源、资金筛选明细、资产／项目定位和详情各有额外请求，但当前默认 20 条与精确直跳没有通过先拉全页寻找对象实现。此次未验证全部展开／分页／跨月详情分支的真实请求数。

## 响应校验与序列化

固定 r61 后端 Service／HTTP 外层链为：`http.py` 看板 GET → `LocalService.dispatch(response_format="http_json")` → `validate_command` → 授权和领域读取 → `response_contracts.validate_response` → 同一 TypeAdapter 的 `dump_json` → `reply` 直接发送 bytes。此前审查只检查这一外层，误将 Service 的一次调用推广为月度核对整条内部链也只有一次；下表记录已确认的 r61 内部重复，不能将后来修正倒写成 r61 原实现。

看板 GET 没有经过 `contract_reply/http_response`，所以没有该辅助入口带来的另一轮 HTTP 校验；后者仍用于其他独立边界，必须保留。`dump_json` 是经过校验结果的序列化，不是再执行一次 `validate_python`。这两点不能排除领域返回 helper 在 Service 前已经校验同一响应。

| 实际对象链 | 同模型重复与必要不同合同 |
| --- | --- |
| prepared：`CloseReview.read:1570 require_owner_review(manifest.owner_review)` → `_response:1479 public_owner_review(review)` → `public_owner_review:233 require_owner_review(review)` | 同一个存储 OwnerReview 合同验证两次。第一次输出是验证后的新 dict；第二次输入只是把该未改内容传给投影函数，再生成一份验证结果，完整 collections 仍被重复遍历。 |
| closed：`CloseReview.read:1519 require_owner_review(read_section(..., owner_review))` → 同一投影链 | 独立冻结 header／section 认证与源 OwnerReview 合同校验必要；后续投影里的第二次同合同校验是重复。不得将 section 内容认证或首次源合同校验一起删除。 |
| `CloseReview._response:1468 DASHBOARD_CLOSE_REVIEW_ADAPTER.validate_python(public_dict)` → `Service.dispatch:470 validate_response` | `RESPONSE_ADAPTERS[dashboard_close_review]` 在 `response_contracts.py:2548` 正是该 adapter；后者再次验证同 shape、同内容的公开响应，属于相同公开合同重复。unprepared／covered／stale 的 null-review 响应也有这两次。 |
| 完整 OwnerReview → PublicOwnerReview 金额／业务／状态投影 | 源存储合同与公开老板合同形状不同，各自一次是必要职责；上述问题不是因为两个合同同时存在，而是每个合同各自又验证一次。 |
| 五页／context／readiness／workflow／业务详情／报表响应 | 当前相应响应生产模块没有额外 `validate_python/validate_response/model_validate` helper 调用，尚未发现相同整份公开响应在返回 Service 前再验一次。内部 Fact 模型、事实内容证明、日期金额类型核对不属于相同响应合同重复。 |

`security/window.py:_owner_review_text` 是 `public_owner_review` 的另一个独立调用方；它在本机密码窗口展示源核对内容，不能仅为看板优化取消其独立输入校验。关账构建的子引用／目录合同与最后完整 OwnerReview 合同、完整核验重建的新 review 与原已保存 review，是不同对象或层级；目前没有把这些核心构建／核验路径定为默认读取重复缺陷。固定 v1 独立实现保持原规则。

浏览器链为 `requestLocalJson` 一次 `response.json()` → `requestGeneratedJson` 一次生成 Ajv validator → `matchesRequest`。最后一步核最终 URL 的公司、期间、业务、筛选、快照与集合分页关系；`validDashboardCollections` 还检查条数等于 items 长度、总量／筛选量／返回量关系、has_more 与游标关系。这些语义检查不能用 JSON schema 的字段类型检查替代。服务端合同校验与浏览器 Ajv 是各自边界，当前没有发现同一客户端响应反复调用同一 validator 的路径。

root 对固定 r61、真实合成根 2016-11 已闭期 review 的 [profile](../../../.tmp/stage9-close-validation-r62-profile.json) 显示：五次 native 读取加公开响应验证为 11.9–14.6ms，插桩一次 18.1ms；四次 TypeAdapter 验证 inclusive 合计约 0.355ms，其中源 `require_owner_review` 两次约 0.292ms。这不是纯浏览器时间，也说明重复校验不是当前简报主要慢因。

当前工作区已窄改 `close_review.py`：独立 `public_owner_review` 仍严格验证一次源合同；私有 `_public_owner_review` 只投影当前内部 `require_owner_review` 的实际成功结果。prepared／closed 源合同各保留一次，stale 检查顺序未改。`CloseReview._response` 不再提前执行公开 adapter，由 Service 的统一 `validate_response` 验证公开响应一次，native／HTTP／http_json 保持同一边界。独立 `http_response` 仍严格验证；没有客户端“已校验”开关、额外业务缓存或跨请求证明复用。

该修正也解决内部 builder 的错误公开 payload 先由局部 Pydantic 抛出、绕过 `response_contract_mismatch` 安全归类的问题；统一边界以 HTTP 500 和声明字段路径反馈，不能泄露错误金额值或未知私有键名。存储源损坏仍按 `content_integrity_failed` 拒绝，不能混同为合法业务待补资料。

定向 `test_close_review_validation_boundaries.py` 新增 11 项已通过 31.27s：prepared／closed／covered／unprepared／stale 的实际源／公开合同调用数，源损坏先于 stale 判断的拒绝，公开 builder 失败安全路径，独立 HTTP／本机窗口校验，以及 None／超过 JavaScript 安全整数／int64 上限的同版金额传输。极值金额是明确的公开序列化合同探针，不宣称真实保存的空业务 review 自然产生该金额。修正前 prepared／unprepared 两项计数失败回执保留为原重复证据。最终一次相关小组包含新 11 项、现有月度核对 transport 8 项、本机批准 1 项、页面／本机窗口同版投影 1 项及公开 close-review HTTP 合同 1 项：[22 passed，83.14s](../../../.tmp/stage9-close-validation-r62-final.log)，[XML](../../../.tmp/stage9-close-validation-r62-final.xml)。该 22 项包含已先行通过的新 11 项，不能相加为 33 项；生产与新增测试 Ruff／格式检查通过。

## 等待成本与已有验证

默认五页读取、context、月度核对及业务详情没有固定 `setTimeout` 等待。发现的定时器分别用于选择提示 6 秒后消失、定位高亮 2.2 秒后消失、安全窗口 pending 状态 1.2 秒轮询，以及报表导出后台任务 1.2 秒轮询；后两者只在对应办理流程发生，不是普通热刷新固定成本。后台任务面板按公司变化或用户刷新读取，没有持续默认轮询。

固定 `frontend/tests/browser-stage9-hot-refresh.cjs:measure` 在页面记录点击起点；需本次 context、主响应及简报核对响应结束，并且完整内容可见、主 header 不 busy，连续两个 animation frame 成立后记录页面时间。Playwright 后续断言／RPC等待不计入这个时间。两帧是现有完整渲染测量条件；本次没有通过删条件或减少等待帧数改变成功标准。

固定包已有测试源码提供以下证据，本次未重跑：

- `frontend/tests/dashboard-context-v2.test.mjs` 的 hot refresh case 覆盖简报、员工、资产、报表：main 在 context 完成前启动，context 替换同选择时 main calls 保持 1，context 检查失败不会显示已完成主响应。该特定 case 排除了资金页，不能说五页都由它直接证明。
- `frontend/tests/brief-close-review-prefetch.test.mjs` 验证核对组件复用预取后额外请求为 0、预取失败不重复请求、公司变化不接受旧预取。
- r61 纯浏览器回执保存 150 次完成结果，但 `http_requests` 和 `dispatch_calls` 均为空；harness 只要求每个必要 endpoint 至少存在一个本次响应，不能据此证明每轮精确请求数或排除额外已取消请求。r62 后续实际插桩补证了上述稳定选择的一次刷新／warmup／navigation 顺序，尚未为原 150 次纯样本逐次记录请求轨迹。

## 处理决定与未验证项

没有足够证据支持修改五页刷新编排、删除 context／close-review 内容或削弱 schema／语义校验。月度核对同源／同公开合同重复已按内部职责窄修，成本小、验证边界更准确；该修正尚未形成新的整页纯浏览器验收，不能作为 500ms 达标依据。其他页面当前未发现同类整份响应重复，不能由此宣称排除所有内部路径。

稳定选择下的五页默认刷新已由 r62 实际请求轨迹确认，没有支持删除请求或校验的重复成本证据。资金 context watcher 的这一稳定刷新分支已包含在该次诊断中；并发刷新、初次保存公司恢复、跨公司切换、失效期间纠正、详情展开／分页等其他交错仍未取得对应轨迹。诊断已独立归档，没有覆盖 r61 原回执；r61 简报超标状态保持不变。后续只按新的具体证据继续定位，不能由本次调用数扩大宣称所有路径均已排除重复。
