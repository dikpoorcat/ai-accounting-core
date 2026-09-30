# 材料结果与期间 SQL guard：同一只读快照内复用

固定 r32 合成主 12 月已完整核验。私有 AB 使用该只读库与固定来源，未修改库、源码快照或业务输入。准备摘要在同一 `QueryReads.snapshot` 内先执行 `Periods.collect_current_readiness` 的材料检查：`read_completeness_summary → check_completeness_many → _material_result` 对命中的结果原文逐条拒绝重复 JSON 键并核对摘要；随后期间检查取得当期全部正式结果 ID，调用 `QueryReads.verify_sql_outcomes`，才让 SQLite JSON1 读取银行字段。不是 guard 先于材料检查。材料检查不承担完整事实、依赖、正式采用或来源封签核验，后续这些证明仍须执行。

实现只对受控 `QueryReads.snapshot` 且连接对象相同的材料检查建立暂存批。暂存批亲自调用原 `verify_outcome_bytes` 严格核验行原文与摘要，整次 `check_completeness_many` 正常返回后才把精确 ID 登入该快照原有的 `_verified_sql_outcomes` 集合。这里的“正常返回”只指语法／摘要校验完成，结果仍可带业务 `issues`，不会缓存材料完成结论。任一后续行、资料解析或结果构造抛错均不提交成功前缀；快照结束清空集合，旧暂存批因快照令牌变更不能提交。未托管连接、别的连接与写入路径继续各自核验，没有对外布尔免验入口，也不保留解析后的 999 个对象。

| 材料入口与调用方 | 此次决定 |
| --- | --- |
| `read_completeness_summary`：期间准备摘要；常驻简报 materials worker | 经 `check_completeness_many`，仅在同一受控快照内成功批次可供后续 SQL guard 复用；worker 的证明留在该 worker，不能跨进程给父请求使用。 |
| `check_completeness`：`Periods.check_readiness`、直接材料页／关账核对 | 同一批量实现；只有调用方实际提供同连接受控 `QueryReads` 时登记。普通独立调用保持原严格核验。 |
| `check_completeness_many`：代发、工作清单以及独立批量调用 | 没有受控 `QueryReads.snapshot` 的调用不登记；结果及覆盖依据不变。 |
| 完整核验、修复、备份、固定 v1 历史读取 | 原权威来源与独立语义核验不使用这份 SQL 语法／摘要集合。 |

私有 AB 的完整准备响应 SHA-256 同为 `139543a3d5946a59a1208d28ccbb9bca03bbcb3d04d6f16d316192d5197a3846`。材料命中 999 个 ID，原 SQL guard 需读取／严格解析 1,016 个，本方案仍检查其中 17 个：`verify_outcome_bytes` 总调用 2,015→1,016 次，实际 SQL 返回行 70,068→69,069、返回值字节 19,180,539→18,139,229、VM 2,053,800→2,044,800。其余约 2.04M VM 不由此方案消除。诊断 CPU 1437.5→1203.1 ms 有并行负载与插桩，只作方向证据。

另分别补测串行原生简报的两种准备口径。`preparation=deferred` 不运行材料检查，原 AB 中材料命中为零、SQL guard 工作量不变；这**不能**推广为默认完整简报无收益。`preparation=complete` 的新 AB 在同一进程有 999 个材料核验，但其中 650 个结果此前已有 guard 成功证明，材料之后真正重复的 guard 结果仅 349 个；严格检查 2,075→1,726 次，返回行 75,115→74,766、返回值字节 21,474,878→21,202,672、VM 3,134,300→3,131,200。两份完整响应逐路径比较也只差动态 `generated_at`。r33 已实现的单向材料→guard 复用尚未处理这 650 个相反顺序命中；[双向原型与调用矩阵](r33-material-proof-two-way.md)单列该遗漏。这些均为单进程串行诊断，不是浏览器或 500 ms 验收。**常驻完整简报**将 materials 放在独立 worker 的只读事务，父进程的 SQL guard 属另一事务；这份证明不能跨进程／事务传给父请求，因此上述串行 complete 收益不能视为常驻简报收益。

[原始 AB 无损 gzip 与双 SHA-256 清单](r32-material-proof-reuse-archive.json)保存两份准备摘要、deferred 简报与 complete 简报各两份原始记录。真实材料来源、合法结果、重复键损坏、失败批次前缀、新快照及未托管调用定向为 3 passed；新源尚待下一固定候选成组回归。`verify_selected_content` 属更强的事实／依赖证明；现有样本没有证实其成功后又触发同范围 SQL guard 的独立收益，暂不扩大复用。
