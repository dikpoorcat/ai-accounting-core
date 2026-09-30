# r25 合成备份 WinError 5：现场、定时释放与隔离对照

仅处理合成测试与 `.tmp` 诊断；生产备份、重试次数、权限、编辑器和真实服务均未修改或停止。

## 失败现场

`stage9-cd-group-r25/shard-2.log` 的 `test_portable_rollover_waits_for_short_windows_reader` 在 `backup._replace_archive(candidate, current)` 约 5 秒后仍收到 WinError 5。`.tmp/stage9-backup-lock-pid71960.jsonl` 中与 `test_portable_rollover_waits_f0` 对应的第 8 条记录（UTC 2026-09-27 16:41:56.383）显示：

| 对象 | Windows 属性 | Restart Manager 所列占用者 |
|---|---:|---|
| 候选 `package.zip`（source） | 32 / Archive | `Code.exe`，PID 73008 |
| 已有 current ZIP（destination） | 32 / Archive | 空列表 |

同一 JSONL 的第 9 条是**预期失败**的 timeout 测试：destination 列出测试 Python PID 71960 与 Code.exe，source 空。这表明诊断能观察到测试故意持有的 target 句柄。失败后现存 destination 的 `icacls` 只读结果为 `NT AUTHORITY\SYSTEM:(F)` 与 `MD01\MD01:(F)`；失败瞬间的旧插件 `icacls` 输出因 UTF-8 解码线程异常成为 `null`，不能把事后 ACL 冒充当时 ACL。该 `.tmp` 插件现已改为捕获原始 bytes、替换解码并保存 base64，未来失败不再丢失 ACL 或 RM 记录。

这些数据直接支持“末次失败时外部 Code.exe 占用候选 ZIP”，不证明它在之前所有重试时都持有同一句柄，也不证明测试 Timer 回调的运行历史。Restart Manager 空列表不是无内核句柄的完整证明。没有依据增加生产重试、变更 ACL 或停用编辑器。

## 定时释放机制的单次独立复现

`.tmp/stage9-backup-short-reader-probe-result.json` 在新建的专用合成 ZIP 上使用测试相同的 `CreateFileW` 共享方式和 100 毫秒 Timer。一次运行的事件：0.17ms/101.25ms 两次 WinError 5；102.06ms `CloseHandle` 返回成功、错误码 0；202.19ms `os.replace` 成功。原候选消失、current 内容为新值。它证明相同机制能成功释放测试句柄，但没有给失败那次 Timer 加事件日志；失败现场末次 destination 未列测试 PID 与 Timer 已释放的解释相符。

## 仓库外专用 OS TEMP 对照

原合成 r25 目录及 JSONL 均保留。新目标 `C:\Users\MD01\AppData\Local\Temp\ai-accounting-stage9-backup-r25-run2` 在运行前验证不存在；它只装新合成测试数据。`.tmp/stage9_backup_lock_diagnostic.py` 的白名单只增加这一精确根及首次尝试的精确根，不接收其它 TEMP 路径。

第一次启动器尝试在 `Tee-Object -LiteralPath ... -Append` 的 PowerShell 参数集错误后中止：上游短暂启动，只完成首个测试，留下 `.tmp/stage9-backup-isolated-r25-junit.xml` 内部错误与首次 OS TEMP 根。未删除这份现场，也未将它记为业务失败或通过。修正启动器后使用另一个预先不存在的 `-run2` 根，**一次完整运行**以下三文件，结果见 `.tmp/stage9-backup-isolated-r25-run2.log`、`...-junit.xml`：

```text
tests/kernel/test_runtime_backup.py
tests/kernel/test_foundation_backup.py
tests/kernel/test_stage9_backup_upgrade_history.py
79 passed in 62.27s
```

短读者等待测试通过（1.40s）。该运行的 `.tmp/stage9-backup-lock-pid33340.jsonl` 只有刻意超时用例的一条真实 WinError 5 终局记录，Restart Manager 的 destination 仅列测试 Python PID 33340，source 为空；没有 Code.exe 候选占用记录。其余 JSONL 条目是预期模拟 `PermissionError` 的故障测试，不属于真实 Windows 锁。

结论：这一轮对照把失败范围定位到仓库 `.tmp` 合成路径与外部文件观察者的交互，且备份原等待、失败保旧包、恢复和幂等断言在隔离根一次完整通过。单轮通过不保证今后仓库内或其它机器不再发生竞争；若再现，应保留当时的 source/destination RM 与 ACL 记录再判断，不能把 OS TEMP 作为绕过产品完整性检查的办法。
