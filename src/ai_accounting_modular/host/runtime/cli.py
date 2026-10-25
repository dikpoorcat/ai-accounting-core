"""finance-modular: explicit catalog, one resident and whole typed business commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_accounting_modular.contracts.public import ContractError, loads_json

from ._state import MAX_REQUEST_BYTES


def _input(path):
    if path:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
    else:
        raw = b"" if sys.stdin.isatty() else sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ContractError("batch_too_large", "请求文件超过入口限制。")
    value = loads_json(raw) if raw.strip() else {}
    if not isinstance(value, dict):
        raise ContractError("invalid_input", "命令内容须为 JSON 对象。")
    return value


def main():
    parser = argparse.ArgumentParser(
        description="新体系本机会计入口，使用明确目录，独立于 finance-local。"
    )
    parser.add_argument("--catalog", type=Path)
    commands = parser.add_subparsers(dest="mode", required=True)
    identifiers = commands.add_parser("ids", help="生成标准UUID，不连接目录或创建业务")
    identifiers.add_argument("--count", type=int, default=1, help="生成数量，1至1001，默认1")
    commands.add_parser("init", help="用本机密码窗口新建目录和负责人，不覆盖已有文件")
    call = commands.add_parser("call", help="一次调用完整 accounting_* 命令")
    call.add_argument("command")
    call.add_argument("--input", type=Path)
    company = commands.add_parser("company-create", help="在明确的新资料根建立独立公司空库")
    company.add_argument("--input", type=Path)
    register = commands.add_parser("company-register", help="核验并登记明确的新体系独立目标")
    register.add_argument("--input", type=Path)
    daemon = commands.add_parser("daemon", help="前台运行新体系居民后台")
    daemon.add_argument("--port", type=int, default=0)
    daemon.add_argument("--static-directory", type=Path)
    commands.add_parser("serve", help="打开同源看板；密码只在本机窗口输入")
    commands.add_parser("mcp", help="指定目录时连接同一后台；省略目录时只提供离线恢复和重建")
    commands.add_parser("service-info", help="核验本机服务，不输出连接能力")
    commands.add_parser(
        "dev-proxy-info", help="仅向本机开发代理输出已核验连接及变化探针，不启动后台"
    )
    commands.add_parser("stop", help="安全停止明确目录的新体系后台")
    security = commands.add_parser("security", help="请求或查询本机身份窗口")
    security.add_argument(
        "operation",
        choices=("login", "change_password", "recover", "replace_recovery_code", "status"),
    )
    for action in ("restore", "rebuild"):
        offline = commands.add_parser(action, help="独立同步处理显式离线包/清单，不启动源服务")
        offline.add_argument("--input", type=Path)
    upgrade = commands.add_parser(
        "database-upgrade", help="服务停止后，以已验证备份升级明确公司结构"
    )
    upgrade.add_argument("--company-directory", type=Path, required=True)
    upgrade.add_argument("--backup", type=Path, required=True)
    upgrade.add_argument("--expected-structure-fingerprint", required=True)
    args = parser.parse_args()
    runtime = None
    try:
        if args.mode == "ids":
            from ai_accounting_modular.agent.public import generate_ids

            result = {"ids": generate_ids(args.count)}
        elif args.mode == "database-upgrade":
            from ._upgrade import run

            result = run(args.company_directory, args.backup, args.expected_structure_fingerprint)
        elif args.mode in {"restore", "rebuild"}:
            from ai_accounting_modular.host.public import create_offline_application

            request = _input(args.input)
            if request.get("action") != args.mode:
                raise ContractError("invalid_input", "离线命令与请求 action 必须一致。")
            result = create_offline_application().accounting_isolated(request)
        elif args.mode == "mcp" and args.catalog is None:
            from ._mcp import create_offline_mcp

            create_offline_mcp().run(transport="stdio")
            return
        else:
            if args.catalog is None or not args.catalog.is_absolute():
                raise ContractError("invalid_path", "请用 --catalog 明确目录库的绝对路径。")
            if args.mode == "init":
                from ._native import initialize

                result = initialize(args.catalog)
            elif args.mode == "daemon":
                from .public import create_runtime

                runtime = create_runtime(
                    args.catalog, port=args.port, static_directory=args.static_directory
                )
                runtime.start()
                # Background owns current durable tasks; the main thread waits only for shutdown.
                runtime.thread.join()
                return
            elif args.mode == "dev-proxy-info":
                from ._client import development_proxy_info

                result = development_proxy_info(args.catalog)
            else:
                from ._client import Client

                client = Client(args.catalog, autostart=args.mode not in {"service-info", "stop"})
                if args.mode == "mcp":
                    from ._mcp import create_mcp

                    create_mcp(client).run(transport="stdio")
                    return
                if args.mode == "service-info":
                    result = client.info()
                elif args.mode == "stop":
                    result = client.stop()
                elif args.mode == "security":
                    result = (
                        client.security("session_status", {})
                        if args.operation == "status"
                        else client.security("request", {"kind": args.operation})
                    )
                elif args.mode == "serve":
                    import webbrowser

                    webbrowser.open(client.browser_url()["url"])
                    result = {"status": "opened", "message": "新体系看板已打开。"}
                elif args.mode == "company-create":
                    result = client.create_company(_input(args.input))
                elif args.mode == "company-register":
                    result = client.register_company(_input(args.input))
                else:
                    command = args.command.replace("-", "_")
                    result = client.dispatch(command, _input(args.input))
    except ContractError as error:
        result = error.to_response()
    except KeyboardInterrupt:
        return
    except Exception as error:
        from .public import Runtime

        result = Runtime.failure(error).to_response()
    finally:
        if runtime is not None:
            runtime.close()
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(output)
    else:
        stream.write(output.encode("utf-8"))
    if isinstance(result, dict) and (
        result.get("status") == "rejected" or result.get("state") == "failed"
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
