"""Command line entry point for isproxy."""

from __future__ import annotations

import argparse
import sys

from .js_rules import load_config
from .proxy import ProxyServer, Target, is_address_in_use

DEFAULT_TARGET = "https://www.runoob.com"


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="isproxy",
        description="把本地 127.0.0.1:<port> 反向代理到指定站点。",
        epilog=f"例如: isproxy {DEFAULT_TARGET} --port 8080",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=DEFAULT_TARGET,
        help=f"上游站点 URL（默认 {DEFAULT_TARGET}）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="本地监听地址（默认 127.0.0.1）")
    parser.add_argument("-p", "--port", type=_port, default=8080, help="本地监听端口（默认 8080，0 表示随机）")
    parser.add_argument("--timeout", type=float, default=30.0, help="连接上游的超时秒数（默认 30）")
    parser.add_argument("-k", "--insecure", action="store_true", help="不校验上游 HTTPS 证书")
    parser.add_argument("--js-rules", metavar="PATH", help="JS 修改规则 JSON 文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        target = Target.parse(args.target)
    except ValueError as exc:
        parser.error(str(exc))

    js_config = None
    if args.js_rules:
        try:
            js_config = load_config(args.js_rules)
        except (OSError, ValueError) as exc:
            parser.error(f"无法加载 JS 规则: {exc}")

    try:
        server = ProxyServer(
            (args.host, args.port),
            target,
            timeout=args.timeout,
            verify_tls=not args.insecure,
            js_config=js_config,
        )
    except OSError as exc:
        print(f"isproxy: 无法监听 {args.host}:{args.port}: {exc}", file=sys.stderr)
        if is_address_in_use(exc):
            print(
                f"isproxy: 端口可能被旧进程占用，可执行 "
                f"'netstat -ano | findstr :{args.port}' 查看 PID 并结束进程，"
                "或改用其他 --port。",
                file=sys.stderr,
            )
        return 1

    actual_host, actual_port = server.server_address[:2]
    display_host = "127.0.0.1" if actual_host in ("0.0.0.0", "::") else actual_host
    print(f"isproxy: http://{display_host}:{actual_port}  ->  {target.origin}", flush=True)
    if js_config and js_config.rules:
        print(f"isproxy: JS 修改规则 {len(js_config.rules)} 条", flush=True)
    print("isproxy: 按 Ctrl+C 停止", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nisproxy: 已停止", flush=True)
    finally:
        server.server_close()
    return 0
