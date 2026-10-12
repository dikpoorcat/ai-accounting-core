"""Standalone local runtime: finance-local --root DIR call COMMAND --input request.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="本地 SQLite 确定性会计内核")
    parser.add_argument("--root", type=Path)
    commands = parser.add_subparsers(dest="mode", required=True)
    call = commands.add_parser(
        "call", help="执行类型化业务命令或事项工作稿命令；先call schema发现合同"
    )
    call.add_argument("command")
    call.add_argument("--input", type=Path)
    commands.add_parser("mcp", help="通过本地服务提供 stdio MCP")
    commands.add_parser("stop", help="安全停止此资料目录的本地服务")
    commands.add_parser("service-info", help="只读核验本资料目录的服务连接信息")
    commands.add_parser("upgrade", help="离线升级已登记的正式版本数据库")
    development = commands.add_parser(
        "upgrade-development", help="备份并显式升级已声明来源的开发库，保留身份和历史"
    )
    development.add_argument(
        "--fund-account-input",
        type=Path,
        help="有来源的受管资金账户身份初始化JSON；不接收分录或科目编码",
    )
    daemon = commands.add_parser("daemon", help="运行每个资料根目录唯一的本地服务")
    daemon.add_argument("--port", type=int, default=0)
    daemon.add_argument(
        "--replay-scope", type=Path, help="显式加载私有 JSON 重放范围；需先停止现有服务"
    )
    web = commands.add_parser("serve", help="启动本地服务并打开会计界面")
    web.add_argument("--port", type=int, default=0)
    security = commands.add_parser("security", help="请求本机安全窗口；密码只在窗口输入")
    security.add_argument(
        "operation", choices=("login", "change_password", "recover", "setup", "close", "status")
    )
    security.add_argument("--input", type=Path)
    args = parser.parse_args()
    from .diagnostics import error_response

    try:
        if args.root is None:
            import os

            configured_root = os.environ.get("FINANCE_DATA_ROOT")
            if not configured_root:
                from .contracts import KernelError

                raise KernelError(
                    "data_root_required", "请使用启动器，或明确指定 --root / FINANCE_DATA_ROOT"
                )
            args.root = Path(configured_root)
        if args.mode == "daemon":
            from .runtime_snapshot import bootstrap_runtime

            bootstrap_runtime(args.root)
        from .daemon import ServiceClient, default_root

        if args.root is None:
            args.root = default_root()
    except Exception as exc:
        print(json.dumps(error_response(exc), ensure_ascii=False))
        raise SystemExit(1) from None
    if args.mode == "service-info":
        from .daemon import _check_health, _metadata_for_root, _request

        try:
            metadata = _metadata_for_root(args.root)
            health = _request(metadata, "/api/health")
            _check_health(metadata, health)
            print(
                json.dumps(
                    {
                        key: value
                        for key, value in {
                            **metadata,
                            "execution_mode": health["execution_mode"],
                            "replay_scope_digest": health["replay_scope_digest"],
                        }.items()
                        if key != "capability"
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as exc:
            print(json.dumps(error_response(exc), ensure_ascii=False))
            raise SystemExit(1) from None
        return
    if args.mode in {"upgrade", "upgrade-development"}:
        if args.mode == "upgrade-development":
            from .offline_development_upgrade import upgrade_root
        else:
            from .offline_upgrade import upgrade_root

        try:
            options = {}
            if args.mode == "upgrade-development" and args.fund_account_input is not None:
                value = json.loads(args.fund_account_input.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError(
                        "fund account input must map company ids to typed account identities"
                    )
                options["fund_account_inputs"] = value
            print(json.dumps(upgrade_root(args.root, **options), ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps(error_response(exc), ensure_ascii=False))
            raise SystemExit(1) from None
        return
    if args.mode == "mcp":
        from .mcp import serve

        serve(args.root)
        return
    if args.mode == "stop":
        from .daemon import stop_service

        try:
            print(json.dumps(stop_service(args.root), ensure_ascii=False))
        except Exception as exc:
            print(json.dumps(error_response(exc), ensure_ascii=False))
            raise SystemExit(1) from None
        return
    if args.mode == "daemon":
        from .daemon import run

        try:
            run(args.root, port=args.port, replay_scope_file=args.replay_scope)
        except Exception as exc:
            print(json.dumps(error_response(exc), ensure_ascii=False))
            raise SystemExit(1) from None
        return
    try:
        payload = (
            json.loads(args.input.read_text(encoding="utf-8"))
            if getattr(args, "input", None)
            else {}
        )
        service = ServiceClient(args.root)
        if args.mode == "security":
            kind = {"setup": "bootstrap_owner", "close": "approve_period_close"}.get(
                args.operation, args.operation
            )
            response = (
                service.security("session_status", {})
                if args.operation == "status"
                else service.security("request", {"kind": kind, **payload})
            )
        elif args.mode == "serve":
            import webbrowser

            result = service.browser_url()
            if "url" in result:
                webbrowser.open(result["url"])
                response = {"status": "opened", "message": "会计界面已打开"}
            else:
                response = service.security("request", {"kind": "login"})
        else:
            response = service.dispatch(args.command, payload)
    except Exception as exc:
        response = error_response(exc)
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    if isinstance(response, dict) and response.get("status") == "rejected":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
