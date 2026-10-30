"""``glassreader`` 命令行入口。

- ``glassreader serve``：启动 HTTP（页图）+ MCP 服务。
- ``glassreader books``：列出书库和进度（不需要服务在跑）。
- ``glassreader open [书名]``：打印一份页面 dataSource（``{"u", "t"}``），给本地模拟器联调用；
  key 要和正在跑的 serve 一致（都从 ``READER_KEY`` 读）。

配置优先级：命令行 > 环境变量 ``READER_*`` > 默认值（:mod:`glassreader.config`）。正式使用时把 token 和 key 写进
``.env``（``set -a; . ./.env; set +a``），否则每次启动都会生成新的，平台插件和已打开的页面都会失效。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence

from . import __version__
from .config import ENV_VARS, ConfigError, Settings, load_settings
from .library import Library, new_session_id

__all__ = ["build_parser", "main"]

log = logging.getLogger("glassreader")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="glassreader", description="在千问 AI 眼镜上看电脑里的 TXT 小说")
    p.add_argument("--version", action="version", version=f"glassreader {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--books", dest="books_dir", help=f"TXT 书库目录（{ENV_VARS['books_dir']}，默认 ~/Books）")
        sp.add_argument("--state", dest="state_file", help=f"进度文件（{ENV_VARS['state_file']}）")
        sp.add_argument("--public-url", help=f"眼镜和平台访问的公网根地址（{ENV_VARS['public_url']}）")
        sp.add_argument("--host", help=f"监听地址（{ENV_VARS['host']}，默认 127.0.0.1）")
        sp.add_argument("--port", type=int, help=f"端口（{ENV_VARS['port']}，默认 8780）")
        sp.add_argument("--key", help=f"页图地址里的随机段（{ENV_VARS['key']}）")

    serve = sub.add_parser("serve", help="启动服务")
    common(serve)
    serve.add_argument("--token", help=f"MCP Bearer Token（{ENV_VARS['token']}）")
    serve.add_argument("--swap-swipe", action="store_const", const=True, default=None,
                       help=f"前滑 / 后滑方向对调（{ENV_VARS['swap_swipe']}）")
    serve.add_argument("--allowed-host", dest="allowed_hosts", action="append", metavar="HOST",
                       help="MCP 额外放行的 Host 头（可重复）")
    serve.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])

    books = sub.add_parser("books", help="列出书库和进度")
    common(books)

    open_ = sub.add_parser("open", help="打印一份页面 dataSource（本地模拟器联调用）")
    common(open_)
    open_.add_argument("title", nargs="?", default="", help="书名或片段；不给 = 最近读的那本")
    return p


def _settings(args: argparse.Namespace, names: Sequence[str]) -> Settings:
    cli = {name: getattr(args, name, None) for name in names}
    return load_settings(cli, os.environ)


_COMMON = ("books_dir", "state_file", "public_url", "host", "port", "key")


def cmd_books(args: argparse.Namespace) -> int:
    settings = _settings(args, _COMMON)
    lib = Library(settings.books_dir, settings.state_file)
    entries = lib.find("")
    print(f"书库 {settings.books_dir}：{len(entries)} 本；进度文件 {settings.state_file}")
    for e in entries:
        p = lib.progress.get(e.id)
        where = f"{p.get('chapter', '')} {p.get('percent', 0)}%  {p.get('updated', '')}" if p else "未读"
        print(f"  {e.id}  {e.name}  {where}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    from .mcp_tools import ReaderTools

    settings = _settings(args, _COMMON)
    if settings.key_generated:
        print(f"注意：没有设置 {ENV_VARS['key']}，这里随机生成的 key 和正在跑的 serve 对不上", file=sys.stderr)
    result = ReaderTools(settings, Library(settings.books_dir, settings.state_file)).open_book(args.title)
    if not result["success"]:
        print(result["message"], file=sys.stderr)
        return 1
    print(json.dumps({"u": result["u"], "t": result["t"]}, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    settings = _settings(args, (*_COMMON, "token", "swap_swipe", "allowed_hosts"))
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    library = Library(settings.books_dir, settings.state_file, swap_swipe=settings.swap_swipe)
    app = create_app(settings, library)

    print(f"glassreader {__version__}", file=sys.stderr)
    print(f"  书库     {settings.books_dir}（{len(library.entries())} 本）", file=sys.stderr)
    print(f"  进度     {settings.state_file}", file=sys.stderr)
    print(f"  MCP      {settings.mcp_url}（Bearer，无状态）", file=sys.stderr)
    print(f"  页图     {settings.base_url}/r/<key>/<书>/<会话>/<计数>.png", file=sys.stderr)
    if settings.token_generated:
        print(f"  token    {settings.token}  ← 自动生成，写进 .env 的 {ENV_VARS['token']} 才能长期用", file=sys.stderr)
    if settings.key_generated:
        print(f"  key      {settings.key}  ← 自动生成，写进 .env 的 {ENV_VARS['key']}", file=sys.stderr)
    if settings.swap_swipe:
        print("  前滑 / 后滑方向已对调", file=sys.stderr)
    for warning in settings.warnings():
        print(f"  ! {warning}", file=sys.stderr)
    if not settings.public_url:  # 本机联调：直接给一份能贴进模拟器的 dataSource（不记最近阅读）
        entry = library.recent() or next(iter(library.find("")), None)
        if entry is not None:
            u = settings.page_prefix(entry.id, new_session_id())
            print(f"  模拟器 dataSource：{json.dumps({'u': u, 't': entry.title}, ensure_ascii=False)}", file=sys.stderr)

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        log_level=args.log_level,
        access_log=False,  # uvicorn 的访问日志会打出含 key 的完整路径；用 AccessLogMiddleware 代替
        server_header=False,
        timeout_keep_alive=7200,  # 经 SSH 隧道 + OpenResty keepalive 池时保持热连接（同 glasscast）
        timeout_graceful_shutdown=3,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0 if server.started else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"serve": cmd_serve, "books": cmd_books, "open": cmd_open}[args.command](args)
    except ConfigError as exc:
        print(f"配置错误：\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
