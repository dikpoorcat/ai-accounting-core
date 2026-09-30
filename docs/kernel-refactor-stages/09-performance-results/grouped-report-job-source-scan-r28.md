# 报表任务列表遗漏的全来源逐项解码

这条 A／B 类漏点在固定 r28 源上只读确认；r29 查询组正在另行验证，本文不作为 r29 验收。正常主 48 月合成库已有 r28 独立完整核验，2019 年第 3 季度 `Reports.preview_export` 返回 `closed/ready` 冻结计划，含 18,512 个 `report_fact_ids`、无接续报表来源。私有探针仅生成只读计划并查询，不创建任务、文件或修改数据库。[原始计数](grouped-report-job-source-scan-r28.json)与 `.tmp/stage9-r29-report-job-scan.py` 保留；原始 JSON SHA-256 为 `0e914b072ac578462c9cfa99149b4b3d7852459af33f7314219c99da8856684a`。后台仍有其他任务，墙钟是诊断；SQL 次数、VM 下界和实际源集合是主要证据。

`reports.py:1996–2055` 的 `browser_job_results` 对**每个** `report_export` 任务、不论 pending、running、failed、succeeded，先读取冻结计划，再逐个 `Store.fact` 解码整个 `report_fact_ids`，只比较 `fact.kind == report_carry_forward`。主 48 月一次正常计划的该循环为 **111,024 次 SELECT**（每事实六次）、约 **1.736M SQLite VM 下界**，进程 CPU 三次约 2.52–2.70 秒，墙钟约 2.53–3.48 秒。结果只有 `report_source.carry_forward_fact_id=None`；非接续的 18,512 份正文、子表和证据列表没有进入标题或下载决定。`Store.fact` 会做当前选定模型的 typed 解码，却不核对 `fact_revision.digest`、封签、原件内容或冻结计划摘要，因而这条循环也不是完整来源证明。单条头查询在正常库约 2.4–2.7 毫秒，**但它未覆盖原循环对无关 typed 资料缺失／格式损坏的偶然拒绝，不能直接视为业务等价**。

| 调用链 | 真正消费和必要检查 | 本轮判断 |
| --- | --- | --- |
| `frontend/App.vue` 的文件进度面板 → `/api/local/jobs` → `Engine.jobs` → `Reports.browser_job_results` | 面板仅打开时请求最近 20 项；每项显示期间、状态、接续来源和下载可用性。pending／running／failed 也触发同一全来源循环 | 默认五页未展开时没有该成本；面板含多个旧报表任务时按任务重复增长 |
| `ReportsView.vue` 导出后轮询单一 `job_id` | 每约 1.2 秒轮询一次，pending、failed 可重试、succeeded 均先全量解码；成功后才下载 | 真正影响用户等待报表生成和取得文件，不能用默认五页计时解释 |
| `LocalService.dispatch('jobs')`、CLI/MCP | 直接返回 `Engine.jobs` 的任务状态、错误码和结果；不调用浏览器装饰器 | 没有这条 `Store.fact` 扫描；也没有浏览器文件链接声明 |
| `run_report_jobs`、`download_browser_report` | 生成前核对冻结计划摘要、公司／库身份、模板，再渲染并发布；浏览器下载独立核对目标边界、计划摘要、路径、manifest 和**实际文件 SHA／字节** | 文件生成／下载职责不能借任务列表缩小；下载的实际字节校验不得省略 |
| `browser_report_details`、`BusinessQueries._file_jobs`、`read_indexes.job_rows` | 前者用 SQL 对完整计划 ID 计数，接续选项仅验证候选；文件任务详情批量取计划 ID 的身份供主体关联，索引核对任务引用多重集 | 不是这里的逐 ID `Store.fact`；完整计划引用或各自返回语义另有职责，本轮未量非空文件任务详情净成本 |
| 完整核验 `integrity._check_job_sources` | 核对任务计划摘要、公司／库身份和每份冻结引用确有已核源；全局来源核验另查 typed、digest、封签、原件 | 是无关来源损坏／缺失的完整拒绝边界，不由任务标题的 rare-kind 选择替代 |

`reports.py:_report_tax_and_carry_refs` 与 `_report_profiles` 已使用“稀少类型的权威头 → 与已认证计划引用集合精确相交 → 仅命中事实做来源及 typed 核验”的同型选择；本次漏查了**文件任务消费者**。仓库范围检索到的其他 `Store.fact` 调用并没有同规模、仅为 kind 的报表循环：`Engine.trace` 返回依赖事实正文，发布／撤销与身份纠错按具体业务字段计算或对照，`duplicate_freeze` 则需要 typed 内容；不把它们机械归为可删读取。

本组修复限于 `browser_job_results` 的接续来源选择：同一次列表请求在**一个只读事务**批量取得其中全部报表任务计划，逐任务校验摘要、公司／库身份与 `report_fact_ids` 的类型和唯一性，再对去重的全部 ID 一次核对存在。原先只从 `subject.kind=report_carry_forward` 找稀少候选；真实合成损坏证实，已采用 carry 的 kind 被改成其他类型后，固定 r29 方法拒绝，新方法却把接续来源隐藏为 `None` 并返回 pending。这是展示来源的实际退化，不能归入无关来源的完整核验边界。[三种损坏对照原始摘要](grouped-report-job-carry-corruption-r30.json)保留 r29 方法源码与诊断日志 SHA-256。

现用 **subject kind 与 `fact_report_carry_forward` typed 头两种独立候选的并集**，与各冻结计划的精确 ID 集合相交；任一命中必须在两种头里同时存在，再执行 typed 与同事务 `_verify_report_fact_sources`。`plan` 与 job payload 均没有可复用的显式 carry ID，因此不从页面参数猜测。三种真实损坏——carry kind 改写、typed 头缺失、非 carry 伪装 carry——均在列表入口拒绝；持久测试文件 8 项通过，Ruff 通过。成功证明只在本次快照复用；下载仍在单独请求内重新核对实际文件。无关非接续来源的正文和 typed 全量损坏仍由完整核验负责，列表不宣称全库证明。另一条保留全部正文验证的有界 256-ID 批量原型在正常库虽为 111,024→235 次 SELECT、约 2.52→1.16 秒 CPU，但 VM 下界 1.736M→2.281M，仍全量解码，故不作为本次实现。

本组受影响回归还需结合现有浏览器任务、适配器和报表下载测试核对 0／1／多接续来源、四种任务状态、20 项分页／单 job 轮询及外部目录交付；当前 8 项定向通过不等于统一 r30 组已完成。工作量回归捕获**真实生产 `browser_job_results`** 的 SQL、typed 解码次数和响应。生成与下载的原有原件字节核验断言保留。
