# r73–r77公共HTTP请求与历史构造准备

当前r77主12月完整核验四项verified、limitations=[]，真实开放月预览完成；150次纯浏览器刷新成功，但简报3次、报表13次≥500ms，性能失败。源码SHA`ab5e42034690c34a0015822265c465bed414b807067310879400de396b8b030c`。按用户要求完成本轮后停止，不启动其他规模、探针或正式交付。具体结果与未验证范围以[本轮记录](owner-r77-main12-round.md)及[原件清单](owner-r77-main12-performance-manifest.json)为准，下列各轮诊断按其历史源码和条件保留。


此前固定r76主12四coverage完整核验verified、limitations=[]，真实开放月预览完成；纯浏览器150次全部响应成功，但简报4次、报表3次≥500ms，性能失败。简报median／p95／max为457.0／575.8／576.6ms，资金315.8／395.0／396.9，员工255.8／311.4／335.5，资产275.8／393.3／403.4，报表450.6／515.7／516.9。源码SHA`30338c4e2eebb2713f3a9167d64f38ed2b4e725ad7f84fee3f2fbdcba3128ccd`；完整核验阶段94.034s，不是整个准备runner耗时。原样本及核验见[r76清单](owner-r76-main12-performance-manifest.json)。

同候选另作插桩诊断，每页warm3／10刷新，50次成功对应110个请求，无重复主请求，所有主API最后完成。HTTP启动差最大1.717ms，handler入口至dispatch最大0.115ms，dispatch结束至handler结束最大3.346ms；没有此段明显排队证据。简报／报表主dispatch中位428.899／402.736ms，browser减HTTP并行包络余量中位34.492／35.669ms，该余量混合前后未覆盖阶段，不是净render成本。既有harness没有保存逐样本ResourceTiming和共享时钟，旧7个纯计时超线不能用新诊断反推原因。诊断未复现超线不替代纯30失败；不据此调GC、线程、优先级或网络。同请求金额结果重复搬运随后在r77修复，主48及更大样本没有r77验收。

主48固定r73纯浏览器实际150次成功响应中79次≥500ms，性能失败；主12先前5／150失败与r70先前37／150失败各保留原件，内容完整核验通过不替代性能通过。固定source SHA420ceb395fc5aec17ddaedf8914c7fc8de640da9f08cb35aded8a3a99a4eb7f4，browser backend build及静态hash与保存的snapshot manifest绑定；本次不重新扫描全树。

| 页面 | 慢样本／30 | median／p95／max ms |
| --- | --- | --- |
| 简报 | 30 | 636.6／697.0／735.3 |
| 资金 | 4 | 436.8／535.6／536.3 |
| 员工 | 0 | 316.7／471.9／473.2 |
| 资产 | 17 | 515.9／596.3／715.4 |
| 报表 | 28 | 556.8／615.1／616.0 |

冷开与首次加载另存原JSON，不能并入热刷新成功结论；切公司为single_eligible_company unavailable。六路native各3样本小于500ms没有证明本轮browser通过。

## 请求范围与必要安全检查

| 问题类型 | 已保存证据与结论 | 未验证边界 |
| --- | --- | --- |
| 五页重复请求／请求串行 | attempt3请求计数和HTTP/dispatch时间表确认正常五页热刷新没有重复主请求，相关请求启动间距小于2ms，已并行。setup、warmup和冷开请求仍在完整记录中，不能按总计数声称重复。 | 该诊断受主48设置活动影响，不是独占纯浏览器计时，不能用3次样本替代30次失败。 |
| 并发cProfile归因 | browser attempt3保存真实仪表化结果；Python3.12并发profile可能污染调用图，不能按其中函数图断言某个请求独占耗时或精确调用归属。 | 不据污染图归因GIL／network或关闭安全复验。 |
| 串行请求边界 | sequential attempt2成功串行context／brief／close-review；各endpoint实际4次metadata、4次schema、2次_check_catalog、1次authorize、1次response validate、1次dump，SQLite实际open通常2次，首次context3次。 | schema自身own时间<1ms，不等于包含连接开销的inclusive耗时<1ms；摘要检查／序列化小于1ms，authorize约12.8ms是brief样本，首次context与其他路径独立，不外推所有请求。安全职责仍须对应真实复验。 |
| 初失败与尝试身份 | browser初次warmups>=1断言失败；attempt2 close-review prepared等待超时／ERR_EMPTY_RESPONSE；sequential初次failed保留。attempt3 over_target、sequential attempt2 passed各为独立原件。 | 没有独立保存的请求runner stdout/log不补造；当前存在的JSON、runners及profile summaries完整归档。 |

## 同进程HTTP配对与实际渲染边界

同fixed r73 source、同服务／进程的main48 direct与dispatch正反配对，warm3、实际3对语义相同；精确剔除generated_at／checked_at展示时间。direct wall median407.013ms、dispatch446.657ms，公共dispatch路径差额约39.644ms。它包括公共授权／目录／响应处理等职责，不是网络传输测量；此前浏览器约190ms差额尚未归因，不能把39.64ms推成全部190ms原因。原attempt1在samples=[]时AssertionError失败保留；attempt2 complete与session撤销／app关闭分别保存。

RSS／peak、GC及SQL调用量只是同进程观察线索；不能仅凭RSS差异宣布缓存、内存压力或GIL根因，也不能据此关闭安全检查复验。同owned快照后续head scope只有brief2／assets4／reports0小重叠，已确认不是此前的大返回根因；未新增缓存。

真实release主12五页DOM基线attempt3各1次，加载时旧root disconnected、旧节点全断开，完成后same_root=false，确认刷新导致五页DOM卸载重建。response_end到两个requestAnimationFrame为40.3–57.2ms，是等待两个帧的诊断边界，不等于纯Vue渲染CPU耗时或全部可省收益。刷新总时延、旧节点／mutations原样保存。原两次failed/returncode1不抹去；优化实施与收益尚未验证，不称页面500ms通过。

公共金额来源audit另证实首次批次物理头重复搬运，见[发布证明与必要读取](owner-publication-proof-reuse-r72.md)及[本轮原件manifest](owner-r74-root-cause-audit-manifest.json)。r74当轮仅保存已完成的窄diagnostic；主12r73的5／150慢和主48r73的79／150慢作为历史结果保留。渲染修复随后在r75完成；RSS、未解释差额及具体净时延归因仍未验证。正式仓库draft／0、阶段未完成。

## 技术预览持有、组件重建与当前验证边界

真实preview生命周期attempt1因KeyError在第一响应核对时失败，reads=[]，未保留已完成before测量；attempt2被动记录24reads，第0各作预热，无collect／阈值调整。去预热wall median简报369.930→501.649ms、资产196.847→237.074、报表363.983→409.170；GC暂停median分别7.429→34.443、4.577→5.228、16.422→31.071。各样本扣实际暂停后的median仍增104.535／33.652／35.712ms，剩余未归因。观察器自身长期保留13,911条GC记录及读取事件片段，inventory未做referrer分解；不能把新增24,718 tracked或RSS241.9→672.9MB全归活动preview。5,278,127B为返回preview编码含locator，不是缓存heap或各section大小。

同旧fixedr73 main12的bounded观察不保留per-GC事件列表，也未collect或改阈值；preview实际65.738s，简报385.716→379.471ms、资产217.295→209.642、报表378.422→399.826。旧attempt2全面变慢未复现，不能作为活动缓存导致延迟的证据。RSS240.8→522.4MB、tracked1934→82860，但当时仍有QueryReads和typed对象，后续首次预热GC已收集56,233对象；这是未作referrer／净存活分解的风险线索，不能全归长期活动preview。其他小任务可能并行，两个观察均不作纯时延验收。

r75活动intent已完成隔离相关组39 passed（stdout91.07s／XML90.894s，0fail／0error／0skip），7修改文件运行前后SHA相同；早期4项不叠加。仅从同份已核完整预览保留PublicOwnerReview精确规范bytes、身份／版本／confirmation及digest定位，并以每服务短期HMAC覆盖完整绑定，返回新解码对象；同月替换淘汰、service.close清除持有。CLI／MCP完整preview及冻结全合同保留，密码批准／close仍事务重建全manifest并比较digest。负例保留摘要及普通SHA共同损坏、身份／月份／digest／epochs／repair／confirmation损坏、跨公司旧intent、caller alias、失效与批准回滚；39项实际范围和源码SHA以封存回执为准。实际Tk交互、净retained heap／GC、规模完整核验及浏览器性能均未验，不能称500ms达标。

前端同选择刷新保留隐藏挂载，必要公司／月份／筛选与失效恢复仍清空。修复前完整suite148 passed／2 failed和early release build原件保存；恢复修正后一次统一全前端153／153通过，contracts:check／vue-tsc／vite release通过，验收source hash前后相同。早期28及恢复定向40全部已包含153，不相加。dashboard_snapshot_changed、显式续页version mismatch与详情changed走清空主投影／旧版本／分页后双门控重新读取，不能保留已知失效金额。

r74真实DOM及合成fixture final确认挂载隐藏、错误／切换边界，但使用恢复修复前Vue，不能当当前后修复源码浏览器验收；旧失败attempts完整保留。恢复修正当轮只有前端统一检查，不以旧DOM诊断证明修复后的收益。fixedr73旧慢样本保持：主12 5／150、主48 79／150。四组收敛后r76主12完整核验通过，但纯浏览器7／150超线；r77后续结果见本文开头，不将内容核验写成500ms通过，正式仓库draft／0未冻结。

当前证据及分时source绑定见[读取与render首批manifest](owner-r75-grouped-read-and-render-manifest.json)及[bounded与intent第二批manifest](owner-r75-grouped-read-and-render-intent-bounded-manifest.json)，raw／gzip双SHA、bytes／mtime与解压逐字节一致。未实际核对的根因和收益继续为未验证。

活动摘要持有退出后，同类全局审查又确认owner review临时position视图的循环引用未及时断开：current及固定v1成功／计算异常用finally释放；current构造中已建立反向helper后源读取失败也清理，保持原SQLite异常及caller事务。14项修正组43.46s、构造6项13.79s（含既有4）各保留范围，不相加；原9通过／1旧UI技术字段断言失败保留，该来源绑定移到core并保持老板金额与技术键退出。固定v1构造在末句journal反向引用之前完成所有源读取，没有同类后置失败点，未强行修改其构造。未量化RSS／净持有或延迟收益。

资产三scope为2个full与1个adopted-only；141个slice中_family47，职责并不相同，不能强行合并或因此声称所有问题已排除。此处采用同类审查最终边界，不新增SQL／计时验证。

上一固定r75 source SHA`71e1515219cb16f8462bd6273de5244c5a11f495380068b3c99ddf1577f93e98`六路native／work／profile已完成；五页相对r73均少2009行、462646B、39200VM，实际耗时未证明稳健大收益。其main12四coverage verified、limitations=[]及真实preview已完成，integrity阶段90.835s不等于整个runner耗时。该上一源码内容核验不替代r76；本批已封原件与source manifest／provenance见[新清单](owner-r76-lifecycle-and-prior-r75-manifest.json)，没有捕获正在写的r76验证回执。

## 历史manifest与构造准备

历史source inventory曾被当前required-file集合解释，因历史没有frontend/local-api-proxy.ts被错误阻断。工具守卫改为按各旧固定source原manifest声明核完整路径／字节／汇总SHA；当前r73仍按当前完整source guard执行。轻量guard实际验证三种旧source及current_r73，负例覆盖缺文件、改字节、错汇总、路径逃逸、未知CASE拒绝；健康小fixture通过。它没有读取SQLite、构造、内容核验或preview。

readiness现为static_ready_runtime_guards_mandatory，保留initial_preflight_result原失败与实际后续静态结果。main120／independent48仍built_not_verified；independent120旧verified保留旧source身份。main120的186,767,161字节checkpoint本批不读取、不复制、不压缩；原SHA在准备报告中引用，JSON完整source guard仍留实际构造前执行。源SQLite身份／FK／冻结row digest与目标注册完整核验及真实开放preview尚未由readiness证明；磁盘预算只是保守规划估算。不能把准备、源码负例或语法通过写为构造通过。

原件集中见[本批manifest](owner-r73-main48-browser-request-readiness-manifest.json)，保存raw／gzip双SHA、bytes／mtime并解压逐字节核对。全部未实际检查的事项保持未验证。本次仅档案与文档整理，无SQL、tests、服务、构造或计时。
