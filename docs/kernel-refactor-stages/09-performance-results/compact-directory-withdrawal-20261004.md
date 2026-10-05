# 稀疏accounting目录候选撤回与原生环境校准（2026-10-04）

稀疏descriptor候选已撤回。候选减少了完整JSON描述记录的实体化，但daemon环境对齐后的配对原生资产中位耗时392.25435→442.5354ms，没有性能收益；不能因分配对象少宣布优化成功。生产恢复8473的全部原字节，目录key查找与冻结余额唯一key哈希复用保留。旧引用测试修正首源98f的71通过／1失败完整保留；最终9ac的4模块统一回归72项通过，未作新全域回归。当前只有原生、profile、源码和测试证据，新鲜完整资格及浏览器验收未完成，第9阶段仍实施中。

## 原生环境与真实浏览器边界

此前目录key、GC尾部及对象图／Lark导入诊断的原生helper没有调用真实daemon的`_prepare_static_runtime`；实际浏览器服务已经调用。该准备包含既有静态模型预备和冻结对象图，不能把未对齐原生进程的长期对象扫描成本当作真实浏览器根因。对象图初次helper失败和r2结果、静态准备有／无freeze的插桩对照保留原件，均为合成诊断，不是整页验收。本轮未改生产GC策略或optional imports；未关闭垃圾回收。

新增配对纯原生helper先调用实际daemon准备，再进行各页3次暖机和30次成功样本。bfb与8473各150成功／0错误、业务摘要及资料／身份／状态／源码守卫不变，全部慢样本保留。该对照没有HTTP、渲染或准备好的负责人待办，不能覆盖c3c3浏览器的资产19个≥500ms样本；未重获当前来源完整资格，顺序窗口及未测OS干扰也限制因果结论。

| 入口 | bfb median／p95／max ms | 8473 median／p95／max ms | ≥500ms |
| --- | --- | --- | --- |
| assets | 433.45615／520.7147／526.6862 | 405.0756／441.3209／656.8034 | 3→1 |
| brief | 293.4933／305.5383／309.8181 | 297.32835／316.2238／318.1855 | 0→0 |
| quarterly_report | 366.0898／398.7232／400.0739 | 350.07005／370.0303／407.1591 | 0→0 |
| funds | 240.8713／262.6415／278.3790 | 239.73735／281.0547／294.1963 | 0→0 |
| employees | 272.6701／293.6246／302.3278 | 243.93765／261.0037／265.5371 | 0→0 |

原件为`.tmp/stage9-daemon-aligned-directory-native-{baseline,current}-20261004.{py,json,log}`。8473资产max656.8034ms仍失败；对齐不构成浏览器达标或500ms标准放宽。

## 候选源码、完整核验与分配观察

候选固定源为 `731ae1c1ffa569cfe670fb84996498631eeea5c23f5da3d54cd01868d0a0bae5`，771文件，源码manifest SHA `74cede15352cb2bcdbfea036775835b7f8039f95decb747bcf6ccab4ecd8d49b`。相对8473只改`close_storage.py`并增加专用测试；前端、两库合同及fixed-v1字节不变。候选先对完整accounting原文做SHA认证并遍历全部canonical描述记录，仅为请求的桶从原文偏移实体化hash／count，非canonical内容回原完整JSON路径；不跳过语义过滤、物理桶或引用权威比较。

`.tmp/stage9-compact-directory-profile-20261004.{py,json,log}`是cProfile／SQL／VM／解码观察器诊断。资产119根全部认证4725674原文字节、遍历59847条描述记录，实体化712条，两字段各356；原文字节与完整walk保留。五页业务摘要与8473相同，SQL／返回行和值字节／采样VM工作量原样。资产仍为SQL381、14232行、11950624值字节及1241100采样VM，245结果／253244字节；观察到JSON输入计数字节4325391，不能当成磁盘读取减少或整页收益。分配降低属于诊断结论，插桩耗时不能作为纯原生收益。

## 配对纯原生结果与撤回

在另一个独立daemon环境对齐窗口，优化前8473与候选731各150成功／0错误，各页保留全部30次样本，业务摘要与守卫一致：

| 入口 | 8473 median／p95／max ms | 731 median／p95／max ms | ≥500ms |
| --- | --- | --- | --- |
| assets | 392.25435／427.3807／439.2765 | 442.5354／459.6894／481.9531 | 0→0 |
| brief | 288.01975／305.4287／321.5304 | 289.5403／303.2174／350.8546 | 0→0 |
| quarterly_report | 336.54745／351.4075／354.2846 | 339.51875／358.2001／363.2125 | 0→0 |
| funds | 227.2664／241.0349／249.9560 | 225.1334／229.8865／231.5805 | 0→0 |
| employees | 230.24945／240.7994／243.9917 | 230.33855／240.8316／250.0649 | 0→0 |

两次8473窗口不可混并或择最快作统一基线。原件为`.tmp/stage9-daemon-aligned-compact-native-{baseline,current}-20261004.{py,json,log}`。资产中位变慢50.28105ms是本窗口观察，regex walk额外成本可能抵消JSON分配节省仅是解释假设；插桩与纯窗不足以把全部50ms确定归因于regex、GC或某种导入。

`.tmp/stage9-compact-directory-withdrawal-20261004.json`保留撤回事实及原helper的原因表述，公开结论按上述因果限制解释。生产`close_storage.py`恢复后SHA `2bcba941ee202a169d9606e4bc64b5e811e7e5dec4d2c7ec99c28421181ac205`与8473完全相同。后续扩展专项移至忽略的`.tmp/stage9-compact-directory-unrun-extended-tests-20261004.py`，SHA `379f502166da6b6bc317e286ecada1fb3859b35b2c4248b3294483c9db29d1f4`；未运行，不写成通过，也不随撤回恢复到生产测试目录。

## 首次失败、旧测试修正与当前状态

候选首次专项90通过／2fixture失败，失败发生在trailing_comma及broken_filter差分fixture；修正后的35项边界通过与首次专项重叠，不能相加。初版只读审查发现cached／negative scope早退与filter非canonical fallback两处问题，已在候选中修正，复审原件保留；这不是对撤回候选的一般资格认定。

731组回归收集即1 ERROR：旧`test_close_reference_selection_work.py`仍导入已退出的`_selected_accounting_references`，业务测试未执行；JUnit的tests=1仅代表collection错误，不作业务通过数。源码manifest、source diff、runner、首次日志与XML全部保留。

生产还原8473后，仅修旧引用选择测试的首个固定源为 `98f460b90ee5a69f8658fac35b21e12a5bb455c91ccdf83a78427cc82473d64d`，770文件，源码manifest SHA `37cbb1c9c0e94cd548e42c2cc57a9bdc48c8713ce99afb87ff82f2092020fc65`。唯一源码差异是`test_close_reference_selection_work.py`，生产、前端、合同及fixed-v1字节均不变；4模块回归71通过／1失败，失败为Journal测试仍观察旧`verify_close_references`，当前hydrate走`verify_selected_voucher_adoptions`。该首轮结果完整保留。

修正实际观察点并保留业务及新快照重核验断言后的最终固定源为 `9ac1ddcc2ee3b4d5d4758aff091d5b33c12e1f54a482c4f6ab83ecd6853bcd2d`，770文件，源码manifest SHA `5398ce88ec9b8bc1878cc425046f1d9764d42383720e46706a8aca4dfeea2a1b`。相对8473仍只改变旧引用选择测试，该文件SHA `45332efced785a5af218cd19c2c5673263a695d2bccc30334180b7ec0f30aeb1`；生产全部字节恢复。`.tmp/stage9-directory-test-repair-converged-regression-20261004.{json,log,xml}`记录4模块统一72通过，0失败／错误／跳过，固定清单不变。实际只运行`test_close_reference_selection_work`、`test_frozen_voucher_header_binding`、`test_accounting_candidate_integrity`及`test_close_reference_physical_batch`；raw helper沿用的scope字符串过宽，不代表新完整五页／v1／repair／backup回归。先前356项仍绑定8473，不向9ac转移计数。另有代理报告单模块7通过但未保存raw log，不伪造原件或加入统一组计数。

[证据manifest](compact-directory-withdrawal-20261004-evidence/manifest.json)独立保存59件完整原生/profile/script/log、五个源码manifest、候选与修正测试源文件、两轮审查、验证映射、首轮失败及撤回receipt，manifest SHA `4232b78c830b8e937c54b1806161e93cb258e01da93d060673a328bd29ed7a8c`。每项含私有原文件SHA、公开脱敏SHA、gzip SHA及解压一致性；workspace根路径、私有守卫和read_context已最小化，gzip mtime为0。不复制业务数据库、凭据或进行中的浏览器runner。已封42件的目录key归档保持原字节。

阶段仍未完成。最新成功真实浏览器结果继续只绑定c3c3；新源码完整资格、实际开放预览和浏览器门槛尚未完成。目录库／公司库draft／0、现有5173、资料根及身份原样，正式冻结、交付及运行入口切换延期。
