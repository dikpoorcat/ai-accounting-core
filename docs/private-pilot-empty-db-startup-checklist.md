<!-- @format -->

# 空库私有试用启动检查单

仅适用于单企业私有试用的全新空数据库；当前有效约束以仓库根目录 [`AGENTS.md`](../AGENTS.md) 为准。

- [x] 已确认目标数据库没有用户或历史业务数据，也不是旧 Alembic revision；否则停止并等待用户决定处理方式。
- [x] 已确认使用隔离的本地 PostgreSQL 17 实例和试用所需的存储目录；不复用共享或生产环境。
- [x] 已完成依赖安装，并执行 `alembic upgrade head`；数据库 revision 为唯一的 `0001_baseline`。
- [x] Docker Desktop 已启动，当前用户可访问 `desktop-linux` 上下文；执行下列空库演练并确认通过：

  ```powershell
  .\.venv\Scripts\pytest.exe -q tests\test_private_pilot_simulation_postgres.py::test_private_pilot_fictional_five_month_rehearsal_on_ephemeral_postgresql17
  ```

  该测试仅创建并自动清理临时 PostgreSQL 17 容器，不连接默认 Compose 数据库或真实资料。

- [x] 已使用 `finance-bootstrap` 创建唯一的试用企业，并妥善保存输出的 `org_id`。
- [x] 在录入任何正式业务前，已从开始记账月份连续生成所需的开放会计期间。
- [x] 已创建唯一的本地负责人账号，妥善保存一次性恢复码，并完成 `finance-login` 登录。
- [x] 已在登录后重启 Codex，并确认 MCP 使用已认证的负责人会话。
- [x] 已由用户明确授权开始试用；未获授权前不导入真实业务资料或部署为生产服务。
