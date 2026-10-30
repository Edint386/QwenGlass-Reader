"""局域网 HTTPS 转发：让同一 Wi-Fi 里的手机直接从这台 Mac 取图，不绕道新加坡（research.md §9.14）。

路由器把 ``cast.example.com`` 解析到本机局域网地址后，手机会用 https 连本机 443 端口。本脚本用和公网相同的
证书（从新加坡服务器复制来的 Let's Encrypt 证书）在 443 上做 TLS，解密后转给本机的服务。只用标准库。

两种模式：

- 不带 ``--route``（原来的行为，一个字节都不改）：解密后把字节流原样转给 ``--upstream-port``
  （默认 glasscast serve 的 127.0.0.1:8766），不解析 HTTP。
- 带 ``--route 前缀=主机:端口``（可重复）：逐个请求解析 HTTP/1.1，路径以某个前缀开头的转给对应上游并剥掉前缀
  （``/reader/r/…`` → ``/r/…``，与公网 OpenResty 的 ``proxy_pass http://…/r/`` 一致），其余照旧转给
  ``--upstream-port``。必须逐请求而不是逐连接分流：手机（CFNetwork）会在一条连接上先后请求两个服务。
  草稿说明见 reader/deploy/README.md；这个文件审过之后替换 glasscast/tools/lan_https_proxy.py。

用法（在能截屏的终端里另开一个标签页）::

    uv run python tools/lan_https_proxy.py                                     # 只有 glasscast
    uv run python tools/lan_https_proxy.py --route /reader/=127.0.0.1:8780     # glasscast + glassreader

证书默认在 ``~/Library/Application Support/glasscast/tls/{fullchain,privkey}.pem``（0600）。证书续期后要重新复制。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TLS_DIR = Path.home() / "Library" / "Application Support" / "glasscast" / "tls"
#: 请求头 / 响应头上限（StreamReader 默认 limit 也是 64 KiB）
MAX_HEAD = 64 * 1024
#: 分流模式下请求体整个读进内存再转发（为了换连接重发）；手机走局域网只有 GET 取图，MCP 走公网
MAX_BODY = 1024 * 1024
#: 分流模式下等下一个请求的空闲上限（秒）；手机会长时间复用连接，到点关掉，手机会自己重连
DEFAULT_IDLE_TIMEOUT = 300.0
#: 等上游响应头的上限（秒）
DEFAULT_UPSTREAM_TIMEOUT = 60.0

log = logging.getLogger("lan-https")

_KEY_IN_PATH = re.compile(r"(/[sr]/)[^/?]+")


def redact(target: str) -> str:
    """``/s/<key>/…``、``/r/<key>/…`` 里的 key 打码（日志用）。只打第一段：后面的 ``/s/`` 可能是页图路径里的会话号。"""
    return _KEY_IN_PATH.sub(r"\1…", target, count=1)


# --------------------------------------------------------------------------- 原样转发（不带 --route）


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    total = 0
    try:
        while data := await reader.read(65536):
            total += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - 对端已断开时关闭也可能报错，忽略
            pass
    return total


def make_handler(upstream_host: str, upstream_port: int):
    async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        peer = client_writer.get_extra_info("peername")
        started = time.monotonic()
        try:
            up_reader, up_writer = await asyncio.open_connection(upstream_host, upstream_port)
        except OSError as exc:
            log.warning("连不上 glasscast %s:%s（%s），断开 %s", upstream_host, upstream_port, exc, peer)
            client_writer.close()
            return
        sent, received = await asyncio.gather(pipe(client_reader, up_writer), pipe(up_reader, client_writer))
        log.info("%s 连接结束：上行 %d B，下行 %d B，%.0f ms", peer[0] if peer else "?", sent, received, (time.monotonic() - started) * 1000)

    return handle


# --------------------------------------------------------------------------- 按路径分流（带 --route）


@dataclass(frozen=True)
class Route:
    #: 以 / 开头和结尾，如 ``/reader/``
    prefix: str
    host: str
    port: int

    @classmethod
    def parse(cls, text: str) -> Route:
        """``/reader/=127.0.0.1:8780`` → Route。"""
        prefix, sep, target = text.partition("=")
        host, colon, port = target.rpartition(":")
        if not sep or not colon or not host or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"--route 应写成 /前缀/=主机:端口：{text!r}")
        if not (prefix.startswith("/") and prefix.endswith("/") and len(prefix) > 1):
            raise ValueError(f"--route 的前缀要以 / 开头和结尾，如 /reader/：{text!r}")
        return cls(prefix, host, int(port))

    def match(self, target: str) -> str | None:
        """命中时返回剥掉前缀后的请求目标（保留开头的 /），否则 None。``/reader`` 本身也算命中（→ ``/``）。"""
        bare = self.prefix[:-1]
        path = target.split("?", 1)[0]
        if path != bare and not path.startswith(self.prefix):
            return None
        rest = target[len(bare) :]
        return rest if rest.startswith("/") else "/" + rest


class HttpError(Exception):
    def __init__(self, status: int, reason: str, message: str):
        super().__init__(message)
        self.status, self.reason, self.message = status, reason, message


@dataclass
class Head:
    """一个请求头或响应头：起始行 + 头字段（保留原顺序和大小写）。"""

    start: str
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: bytes) -> Head:
        lines = raw.decode("latin-1").split("\r\n")
        head = cls(lines[0])
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep or not name or name != name.strip():
                raise HttpError(400, "Bad Request", f"头字段不合法：{line[:80]!r}")
            head.headers.append((name, value.strip()))
        return head

    def get(self, name: str) -> str | None:
        values = [v for k, v in self.headers if k.lower() == name]
        return ", ".join(values) if values else None

    def tokens(self, name: str) -> list[str]:
        return [t.strip().lower() for t in (self.get(name) or "").split(",") if t.strip()]

    def without(self, name: str) -> Head:
        return Head(self.start, [(k, v) for k, v in self.headers if k.lower() != name])

    def encode(self) -> bytes:
        return (self.start + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in self.headers) + "\r\n").encode("latin-1")

    @property
    def chunked(self) -> bool:
        te = self.tokens("transfer-encoding")
        return bool(te) and te[-1] == "chunked"

    def content_length(self) -> int | None:
        value = self.get("content-length")
        if value is None:
            return None
        values = {v.strip() for v in value.split(",")}
        if len(values) != 1 or not next(iter(values)).isdigit():
            raise HttpError(400, "Bad Request", f"Content-Length 不合法：{value!r}")
        return int(next(iter(values)))


async def read_head(reader: asyncio.StreamReader) -> bytes | None:
    """读到空行为止；对端干净地关闭（一个字节都没有）返回 None。"""
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial.strip():
            return None
        raise HttpError(400, "Bad Request", "头不完整") from None
    except asyncio.LimitOverrunError:
        raise HttpError(431, "Request Header Fields Too Large", "头太大") from None


async def read_body(reader: asyncio.StreamReader, head: Head) -> bytes:
    """把请求体整个读出来，chunked 解成定长（转发时改写成 Content-Length）。"""
    if head.chunked:
        parts, total = [], 0
        while True:
            size_line = await reader.readuntil(b"\r\n")
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise HttpError(400, "Bad Request", "chunk 长度不合法") from None
            if size == 0:
                while (await reader.readuntil(b"\r\n")) != b"\r\n":  # 丢掉 trailer
                    pass
                return b"".join(parts)
            total += size
            if total > MAX_BODY:
                raise HttpError(413, "Payload Too Large", "请求体太大")
            parts.append(await reader.readexactly(size))
            await reader.readexactly(2)
    length = head.content_length() or 0
    if length > MAX_BODY:
        raise HttpError(413, "Payload Too Large", "请求体太大")
    return await reader.readexactly(length) if length else b""


async def relay_body(
    src: asyncio.StreamReader, dst: asyncio.StreamWriter, head: Head, status: int, method: str
) -> tuple[int, bool]:
    """按响应头的定界方式转发响应体，返回 (字节数, 是否读到上游关闭为止)。"""
    if method == "HEAD" or 100 <= status < 200 or status in (204, 304):
        return 0, False
    total = 0
    if head.chunked:
        while True:
            size_line = await src.readuntil(b"\r\n")
            dst.write(size_line)
            size = int(size_line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                while True:
                    line = await src.readuntil(b"\r\n")
                    dst.write(line)
                    if line == b"\r\n":
                        await dst.drain()
                        return total, False
            remaining = size + 2
            while remaining:
                data = await src.read(min(65536, remaining))
                if not data:
                    raise ConnectionError("上游在 chunk 中途断开")
                dst.write(data)
                remaining -= len(data)
                await dst.drain()
            total += size
    length = head.content_length()
    if length is not None:
        remaining = length
        while remaining:
            data = await src.read(min(65536, remaining))
            if not data:
                raise ConnectionError("上游在响应体中途断开")
            dst.write(data)
            remaining -= len(data)
            await dst.drain()
        return length, False
    while data := await src.read(65536):  # 没有长度：读到上游关闭（之后客户端连接也要关）
        dst.write(data)
        total += len(data)
        await dst.drain()
    return total, True


def error_response(status: int, reason: str, message: str) -> bytes:
    body = (message + "\n").encode("utf-8")
    return (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode("latin-1") + body


class Upstreams:
    """一条客户端连接用到的上游连接（每个上游最多一条，复用）。"""

    def __init__(self) -> None:
        self.conns: dict[tuple[str, int], tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}

    async def get(self, key: tuple[str, int]) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bool]:
        """(reader, writer, 是否复用的旧连接)。"""
        if key in self.conns:
            reader, writer = self.conns[key]
            if not writer.is_closing() and not reader.at_eof():
                return reader, writer, True
            self.drop(key)
        reader, writer = await asyncio.open_connection(*key)
        self.conns[key] = (reader, writer)
        return reader, writer, False

    def drop(self, key: tuple[str, int]) -> None:
        conn = self.conns.pop(key, None)
        if conn:
            conn[1].close()

    def close(self) -> None:
        for key in list(self.conns):
            self.drop(key)


def make_http_handler(
    default: tuple[str, int],
    routes: list[Route],
    *,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    upstream_timeout: float = DEFAULT_UPSTREAM_TIMEOUT,
):
    routes = sorted(routes, key=lambda r: len(r.prefix), reverse=True)  # 最长前缀优先

    def pick(target: str) -> tuple[tuple[str, int], str]:
        for r in routes:
            rest = r.match(target)
            if rest is not None:
                return (r.host, r.port), rest
        return default, target

    async def exchange(
        ups: Upstreams, key: tuple[str, int], request: bytes, method: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, Head, bytes]:
        """发请求、读到第一个响应头（1xx 中间响应由调用方接着处理）。

        复用的旧连接已被上游关掉时，GET / HEAD 换新连接重发一次。
        """
        for attempt in (1, 2):
            reader, writer, reused = await ups.get(key)
            try:
                writer.write(request)
                await writer.drain()
                raw = await asyncio.wait_for(read_head(reader), upstream_timeout)
                if raw is None:
                    raise ConnectionError("上游关闭了连接")
                return reader, writer, Head.parse(raw), raw
            except (ConnectionError, HttpError):
                ups.drop(key)
                if reused and attempt == 1 and method in ("GET", "HEAD"):
                    continue
                raise
        raise AssertionError("unreachable")

    async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        peer = client_writer.get_extra_info("peername")
        who = peer[0] if peer else "?"
        started = time.monotonic()
        ups = Upstreams()
        counts: dict[str, int] = {}
        down = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(read_head(client_reader), idle_timeout)
                except TimeoutError:
                    break
                if raw is None:
                    break
                t0 = time.monotonic()
                req = Head.parse(raw)
                parts = req.start.split(" ")
                if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
                    raise HttpError(400, "Bad Request", f"请求行不合法：{req.start[:80]!r}")
                method, target, version = parts
                key, new_target = pick(target)
                if "100-continue" in req.tokens("expect"):
                    client_writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await client_writer.drain()
                body = await read_body(client_reader, req)
                fwd = req.without("expect").without("transfer-encoding").without("content-length")
                fwd.start = f"{method} {new_target} {version}"
                if body or req.chunked or req.content_length() is not None:
                    fwd.headers.append(("Content-Length", str(len(body))))
                try:
                    up_reader, up_writer, resp, resp_raw = await exchange(ups, key, fwd.encode() + body, method)
                    status = int(resp.start.split(" ")[1])
                    while 100 <= status < 200 and status != 101:  # 中间响应（如 100 Continue）原样转发，接着读最终响应
                        client_writer.write(resp_raw)
                        raw2 = await asyncio.wait_for(read_head(up_reader), upstream_timeout)
                        if raw2 is None:
                            raise ConnectionError("上游关闭了连接")
                        resp, resp_raw = Head.parse(raw2), raw2
                        status = int(resp.start.split(" ")[1])
                except (OSError, TimeoutError, HttpError, ValueError, IndexError) as exc:
                    log.warning("%s %s %s → %s:%s 失败：%s: %s", who, method, redact(target), *key,
                                type(exc).__name__, exc)
                    client_writer.write(error_response(502, "Bad Gateway", "上游服务没有响应"))
                    await client_writer.drain()
                    break
                client_writer.write(resp_raw)
                if status == 101:  # 协议升级（WebSocket 等）：之后双向原样转发
                    ups.conns.pop(key, None)
                    await client_writer.drain()
                    await asyncio.gather(pipe(client_reader, up_writer), pipe(up_reader, client_writer))
                    return
                n, until_eof = await relay_body(up_reader, client_writer, resp, status, method)
                await client_writer.drain()
                down += n
                label = f"{key[0]}:{key[1]}"
                counts[label] = counts.get(label, 0) + 1
                log.debug("%s %s %s → %s%s %d %d B %.0f ms", who, method, redact(target), label, redact(new_target),
                          status, n, (time.monotonic() - t0) * 1000)
                if until_eof or "close" in resp.tokens("connection"):
                    ups.drop(key)
                    break
                conn = req.tokens("connection")
                if "close" in conn or (version == "HTTP/1.0" and "keep-alive" not in conn):
                    break
        except HttpError as exc:
            client_writer.write(error_response(exc.status, exc.reason, exc.message))
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ssl.SSLError, ValueError):
            pass  # 任一端中途断开、上游 chunk 长度不合法：直接关连接
        finally:
            ups.close()
            try:
                await client_writer.drain()
                client_writer.close()
            except Exception:  # noqa: BLE001 - 对端已断开时关闭也可能报错，忽略
                pass
            if counts:
                summary = "，".join(f"{k} {v} 个" for k, v in counts.items())
                log.info("%s 连接结束：%s 请求，下行 %d B，%.0f ms", who, summary, down,
                         (time.monotonic() - started) * 1000)

    return handle


# --------------------------------------------------------------------------- 入口


async def main_async(args: argparse.Namespace) -> None:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(args.tls_dir / "fullchain.pem", args.tls_dir / "privkey.pem")
    ctx.set_alpn_protocols(["http/1.1"])
    if args.route:
        default = (args.upstream_host, args.upstream_port)
        handler = make_http_handler(default, args.route, idle_timeout=args.idle_timeout)
        routes = [f"{r.prefix} → {r.host}:{r.port}" for r in args.route]
        target = "，".join([*routes, f"其余 → {default[0]}:{default[1]}"])
    else:
        handler = make_handler(args.upstream_host, args.upstream_port)
        target = f"{args.upstream_host}:{args.upstream_port}"
    server = await asyncio.start_server(handler, args.host, args.port, ssl=ctx, limit=MAX_HEAD)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("局域网 HTTPS 转发已启动：%s → %s", addrs, target)
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认所有网卡）")
    ap.add_argument("--port", type=int, default=443, help="监听端口（默认 443）")
    ap.add_argument("--upstream-host", default="127.0.0.1")
    ap.add_argument("--upstream-port", type=int, default=8766)
    ap.add_argument("--route", action="append", type=Route.parse, default=[], metavar="/前缀/=主机:端口",
                    help="按路径前缀分流并剥掉前缀（可重复），如 /reader/=127.0.0.1:8780；不给就原样转发字节流")
    ap.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT, help="分流模式下空闲连接多久关掉（秒）")
    ap.add_argument("--tls-dir", type=Path, default=DEFAULT_TLS_DIR, help="含 fullchain.pem 和 privkey.pem 的目录")
    ap.add_argument("-v", "--verbose", action="store_true", help="分流模式下逐个请求打日志（key 已打码）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
