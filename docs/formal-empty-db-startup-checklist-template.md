<!-- @format -->

# 正式空库启动检查单模板

仅适用于单企业内部正式使用的全新空数据库；当前有效约束以仓库根目录 [`AGENTS.md`](../AGENTS.md) 为准。

- [ ] 已确认目标数据库没有未知历史业务数据，也不是已删除的旧 Alembic revision；否则停止并等待用户决定处理方式。
- [ ] 已确认使用隔离的本地 PostgreSQL 17 实例和正式使用所需的存储目录；不复用共享环境。
- [ ] 已完成依赖安装，并执行 `alembic upgrade head`；数据库 revision 已到当前仓库 head。
- [ ] Docker Desktop 已启动，当前用户可访问 `desktop-linux` 上下文；执行下列空库演练并确认通过：

  ```powershell
  .\.venv\Scripts\pytest.exe -q tests\test_private_pilot_simulation_postgres.py::test_private_pilot_fictional_five_month_rehearsal_on_ephemeral_postgresql17
  ```

  该测试仅创建并自动清理临时 PostgreSQL 17 容器，不连接默认 Compose 数据库或真实资料。

- [ ] 已使用 `finance-bootstrap` 创建唯一的正式企业，并妥善保存输出的 `org_id`。
- [ ] 在录入任何正式业务前，已从开始记账月份连续生成所需的开放会计期间。
- [ ] 已创建唯一的本地负责人账号，妥善保存一次性恢复码，并完成 `finance-login` 登录。
- [ ] 已在同一 Codex 会话中确认登录后的下一次企业数据工具调用使用已认证的负责人会话，无需重启 Codex。
- [ ] 已由用户明确授权开始正式使用；未获授权前不导入真实业务资料或部署为共享服务。

真实企业资料、处理进度、证据指纹、数据库标识和凭证编号只保存在已排除 Git 跟踪的本地资料目录中，不写入公共仓库。
