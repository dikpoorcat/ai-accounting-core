<!-- @format -->

# 看板性能测量样本准备

本文记录可复用的合成规模样本、构造来源和后续浏览器测量方式。样本用于隔离性能观察，不代表真实企业账簿，也不构成正式性能验收结果。

## 本轮准备结果

本轮构造 ledger 为 `.tmp/stage9-preparation-20261009-recovery2-preparation.json`，状态为 `ready`，七个样本均为 `verified`。实际 Python 进程退出回执为 `.tmp/stage9-preparation-20261009-recovery2-process-exit.json`，退出码为 0。各样本的核验报告按 `.tmp/stage9-preparation-20261009-recovery2-{key}-verified.json` 命名。

固定源码快照位于 `.tmp/stage9-preparation-source-20261008-ready`，共 807 个文件，完整源码指纹为 `6cb3baa880427973d5d8e90f533cd9ab401158acfeb3ef51b9637e3b53800afb`。本轮最终交付审计报告为 `.tmp/stage9-recovery2-delivery-evidence-20261009-r2.json`，真实进程退出回执为 `.tmp/stage9-recovery2-delivery-evidence-20261009-r2.json.actual-exit.json`；审计退出码为 0。该审计核对已保存的报告、来源、退出回执、源码及兼容性链，没有重跑完整核验，报告字段 `qualification_database_rows_rescanned` 为 `false`。

准备格式 watcher 基线为 revision 15，215 个文件，guard 为 0。该状态只记录格式检查，不代替样本核验或交付审计。最后一处多分项说明前缀变化仅做精确静态差异桥，没有重复动态读取；各批次的测试范围在交付证据中分别保留。

| Sample key | 样本根目录 | 核验报告 | 规模与用途 |
|---|---|---|---|
| `main12` | `.tmp/stage9-preparation-20261008-ready-main12` | `.tmp/stage9-preparation-20261009-recovery2-main12-verified.json` | 50 人、每月 1,000 笔；主库规模样本 |
| `main48` | `.tmp/stage9-preparation-20261008-ready-main48` | `.tmp/stage9-preparation-20261009-recovery2-main48-verified.json` | 50 人、每月 1,000 笔；主库规模样本 |
| `main120` | `.tmp/stage9-preparation-20261008-ready-main120` | `.tmp/stage9-preparation-20261009-recovery2-main120-verified.json` | 50 人、每月 1,000 笔；主库规模样本 |
| `independent12` | `.tmp/stage9-preparation-20261008-ready-independent12` | `.tmp/stage9-preparation-20261009-recovery2-independent12-verified.json` | 50 个登记对象、每月 1,000 笔；独立业务对照 |
| `independent48` | `.tmp/stage9-preparation-20261008-ready-independent48` | `.tmp/stage9-preparation-20261009-recovery2-independent48-verified.json` | 50 个登记对象、每月 1,000 笔；独立业务对照 |
| `independent120` | `.tmp/stage9-preparation-20261009-recovery2-independent120` | `.tmp/stage9-preparation-20261009-recovery2-independent120-verified.json` | 50 个登记对象、每月 1,000 笔；独立业务对照 |
| `pressure12` | `.tmp/stage9-preparation-20261009-recovery2-pressure12` | `.tmp/stage9-preparation-20261009-recovery2-pressure12-verified.json` | 200 人、每月 5,000 笔；压力诊断样本 |

上述样本根目录和核验报告路径均来自 ready ledger。旧的 101 月和 62 月中断现场保留作历史排查，不属于完整样本，不纳入本矩阵。

准备入口为 `scripts/prepare_stage9_suite.py`，构造、复制、独立核验和浏览器烟雾分别保存报告及日志。主库和独立业务库按 12→48→120 月递增，在完整月份检查点保存边界副本，随后分别独立完整核验。压力样本单独构造。每库只含一家合成公司，最后一个月开放、此前月份关闭。复制时只调整目标位置及 checkpoint 中的数据库位置，并逐表比对原始行和 SQL；不重算业务，也不继承核验资格。续建副本先通过标准连接初始化 WAL，再持有无事务的只读保活连接；核验进程继续只读。样本根、报告和日志位于仓库 `.tmp` 下，与当前资料库及 5173 服务隔离。

准备入口的 `all` 阶段先对两个两月小库进行构造、复制、独立完整核验和五页默认刷新烟雾，再准备七个完整样本并完成各库核验及开放月预览。烟雾使用 3 次 warmup 和 1 次诊断，不作为正式性能达标结论。`plan`、`smoke`、`build`、`verify` 可分阶段运行。成功阶段可在同一固定源码快照下续做；失败产物和日志保留，不自动删除、覆盖或修补部分月份。源码变化时应另存完整快照，不回写旧快照或旧资格报告。验收口径见[看板性能优化规范](performance-optimization.md)。

## 已有浏览器烟雾与适用范围

双小库两个月、五页面烟雾测量已技术完成，使用 3 次 warmup 和 1 次诊断；报告分别为 `.tmp/stage9-preparation-20261008-ready-smoke-main-browser.json` 与 `.tmp/stage9-preparation-20261008-ready-smoke-independent-browser.json`。已声明的 `over_target` 退出1仅表示诊断超500ms，五页完整且无技术失败才算烟雾完成；不能把它称为性能通过。七个完整规模样本均由本轮独立新进程完成注册完整核验及开放月预览。

正式 30 次刷新测量和 500 ms 验收尚未执行。当前工作区兼容性证据覆盖215个相关文件的精确审查、30项持久依赖原字节对照、双小库预览和指定分组读取及费用标题检查；批量付款、单凭证多事项、期初工资付款等分支未动态覆盖，不能据此声称最新源码已完成七库完整核验或所有入口验证。测量最新代码时，先保存新的 release/source，再用已有 `--book-report` 指向这些库，配合新 `--source` 和新 `--qualify-output`，让测量入口先核验、后计时。不得改写旧资格报告。

## 新一轮样本准备

需要新的业务分布或重新构造整组样本时，使用新的 run-id 和源码目录，不覆盖现有产物。本机依赖路径沿用下方已验证的 Node 与 Playwright 路径。

```powershell
npm --prefix frontend run build:release
if ($LASTEXITCODE -ne 0) { throw 'release 构建失败' }
$testRun = 'preparation-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff')
$testSource = '.tmp/stage9-' + $testRun + '-source'
& '.tmp-kernel-venv/Scripts/python.exe' 'scripts/snapshot_stage9_source.py' --target $testSource
if ($LASTEXITCODE -ne 0) { throw '源码快照失败' }
& '.tmp-kernel-venv/Scripts/python.exe' 'scripts/prepare_stage9_suite.py' `
  --source $testSource --run-id $testRun --phase all `
  --node 'C:/Users/MD01/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe' `
  --playwright-module 'C:/Users/MD01/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'
```

## 后续单样本 30 次测量

下面命令只供后续复制执行。本次准备没有运行它。将 `$sampleKey` 设为表中的一个键；命令从 ledger 读取对应的核验报告和固定源码快照，并为输出、标准输出、标准错误及真实进程退出码生成新的时间戳文件。

```powershell
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$workspace = 'D:/GitHub/ai-accounting-core'
$ledgerPath = Join-Path $workspace '.tmp/stage9-preparation-20261009-recovery2-preparation.json'
$ledger = Get-Content -Raw $ledgerPath | ConvertFrom-Json
$sampleKey = 'main12' # 改为 main48、main120、independent12、independent48、independent120 或 pressure12
$sample = $ledger.samples.$sampleKey
if ($null -eq $sample -or $sample.status -ne 'verified') { throw "样本不可用或未核验：$sampleKey" }

$source = [string]$ledger.source
$bookReport = [string]$sample.book_report
$python = Join-Path $workspace '.tmp-kernel-venv/Scripts/python.exe'
$browserScript = Join-Path $source 'scripts/benchmark_stage9_browser.py'
$node = 'C:/Users/MD01/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
$playwright = 'C:/Users/MD01/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'

$stamp = Get-Date -Format 'yyyyMMdd-HHmmssfff'
$stem = ".tmp/stage9-browser-$sampleKey-$stamp"
$output = Join-Path $workspace "$stem.json"
$stdout = Join-Path $workspace "$stem.stdout.txt"
$stderr = Join-Path $workspace "$stem.stderr.txt"
$receipt = Join-Path $workspace "$stem-process-exit.json"
foreach ($path in @($output, $stdout, $stderr, $receipt)) {
  if (Test-Path -LiteralPath $path) { throw "输出已存在：$path" }
}

& $python $browserScript `
  --workspace $workspace `
  --source $source `
  --book-report $bookReport `
  --output $output `
  --node $node `
  --playwright-module $playwright `
  --static-build release `
  --warmups 3 `
  --repeats 30 `
  1> $stdout 2> $stderr
$actualExitCode = $LASTEXITCODE
[ordered]@{
  sample_key = $sampleKey
  output = $output
  stdout = $stdout
  stderr = $stderr
  actual_process_exit_code = $actualExitCode
} | ConvertTo-Json | Set-Content -Encoding utf8 $receipt
if ($actualExitCode -ne 0) { throw "浏览器测量进程退出码为 $actualExitCode；回执：$receipt" }
```

该命令使用本轮固定 source 和对应核验样本，结果只说明该快照的测量。若目标改为最新代码，使用新 release/source 内的浏览器脚本及已有样本的 `--book-report`，并加入 `--qualify-output <新的资格结果路径>`；不能把旧快照结果标记为最新源码的核验结果。
