# 看板规模与压力测试准备

准备入口为 `scripts/prepare_stage9_suite.py`。它构造可复用的合成样本，分别保存构造、复制、独立完整核验及浏览器烟雾检查结果；不运行正式30次热刷新，不作500ms达标结论。验收方法与完成边界以[看板性能优化规范](performance-optimization.md)为准。

## 样本矩阵

| 样本 | 人员或登记对象 | 每月业务 | 月数 | 用途 |
| --- | ---: | ---: | --- | --- |
| 累计依赖与独立业务混合 | 50名员工 | 1,000笔 | 12／48／120 | 主规模验收 |
| 当月独立业务配对 | 50个登记对象 | 1,000笔 | 12／48／120 | 独立分布对照 |
| 累计依赖压力 | 200名员工 | 5,000笔 | 12 | 压力诊断 |

混合样本通过内核登记工资、税务、资金、资产、借款、跨月清偿、开放期替换与闭期更正，保全原件、逐行资料处置、月度清单、采用及冻结历史。独立对照也通过实际登记、发布、银行核对和资料处置构造。每库只含一家合成公司，最后一个月开放、此前月份关闭。

12→48→120个月递增构造，在12与48个月边界复制独立样本；最终120个月本体保留，避免再保存两份相同的大库。复制只改变目标目录与checkpoint中的公司数据库位置，逐表核对原始行和实际SQL；不重算业务，不修改源，不继承核验资格。200人压力样本独立构造。所有根、报告和日志都是仓库 `.tmp` 的 `stage9-` 直属路径，与当前资料库及5173服务隔离。

## 准备命令

使用仓库 `.tmp-kernel-venv/Scripts/python.exe`。Node和Playwright路径使用本机已安装或工作区依赖提供的实际路径，不安装新依赖。下面的源码快照与run-id必须是本轮新的名字；已有产物不能覆盖。

```powershell
# 正式静态构建包含合同检查和类型检查。
npm --prefix frontend run build:release

# 固定当前源码、合成构造器、测量工具、生成合同和正式静态资源。
$testRun = 'preparation-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$testSource = '.tmp/stage9-' + $testRun + '-source'
& '.tmp-kernel-venv/Scripts/python.exe' 'scripts/snapshot_stage9_source.py' `
  --target $testSource

# 替换为已验证的本机实际路径。
$testNode = 'C:/Users/MD01/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
$testPlaywright = 'C:/Users/MD01/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'
& '.tmp-kernel-venv/Scripts/python.exe' 'scripts/prepare_stage9_suite.py' `
  --source $testSource --run-id $testRun `
  --node $testNode --playwright-module $testPlaywright --phase all
```

`all`先对两个2月小库执行构造、复制、独立完整核验和五页默认刷新烟雾检查，再构造七个完整样本，最后逐库运行注册完整核验与真实开放月预览。烟雾每页预热3次、诊断刷新1次，保留逐次记录及超500ms结果；这不作性能达标结论，已声明的`over_target`退出1只算技术完成，其他失败或不完整五页不能通过。移动布局、展开交互与正式30次计时分别执行。也可顺序使用 `plan`、`smoke`、`build`、`verify` 分开执行。准备报告保存每个子进程的日志路径、实际退出码、耗时和失败原因。构造使用测试专用的历史核验延后机制；其输出始终为 `built_not_verified`，只有后续独立进程的完整核验及预览成功才标为 `verified`。双分布烟雾与七库核验全部成功才为 `ready`；仅七库核验完成为`samples_verified`。

成功步骤可以在同一固定源码下继续使用。失败的输出与日志保留；不自动删除、覆盖或修复部分月份。若中途失败，先检查保存的报告、stderr与完整月份checkpoint，再明确恢复范围或选择新的run-id。源码变化时重新保存完整快照，不回写原快照或旧资格。

## 后续测量与复用

准备报告的 `samples.<样本>.book_report` 指向已完成独立核验的报告，`source`指向该次固定实现。正式浏览器入口为 `scripts/benchmark_stage9_browser.py`，默认使用release静态资源并绑定随机端口隔离常驻服务；不会重启当前服务。对每个样本传入其 `--book-report`、`--source`、新 `--output`、实际Node与Playwright路径，使用 `--warmups 3 --repeats 30`。

建库、核验、构建和插桩结束后才进入纯计时窗口。默认五页主数字、20条明细及默认附属内容连续两帧稳定才算热刷新完成；已选模块、展开详情、筛选、直跳、续页、冷启动和公司切换分别记录。逐次成功、失败、超时、慢样本及实际退出保留，P50/P95采用nearest-rank。压力档超500ms保留为诊断，不能混入主规模承诺。

构造期间对照固定源码与正在开发的代码，关注实际数据库结构、业务事实和结果合同、冻结历史解码、检查点与恢复格式。发现新变化使既有样本无法读取或核验时，立即停止构造、保留现场并报告具体差异，不改写指纹或旧记录。仅页面响应版本变化时，检查新前后端是否配套，并验证旧业务库可读；不因此重建样本。

业务或测量实现变化时，旧 `ready` 只证明准备时的源码与样本。采用新固定源码测量已有样本，应使用浏览器入口的 `--qualify-output` 产生新的独立核验报告；不得仅修改旧报告的source、状态或指纹。需要构造新分布时用新根，不向既有资格样本写入。保留源码、样本与原始结果供后续对照，清理只按实际授权处理。
