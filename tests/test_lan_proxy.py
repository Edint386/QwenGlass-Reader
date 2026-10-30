"""deploy/lan_https_proxy.py（局域网 HTTPS 转发草稿）的本机测试：自签证书 + 两个假上游，不碰真实的 443 和服务器。

重点是“按路径分流”模式：同一条 TLS 连接上交替请求两个服务（手机就是这样复用连接的）、剥掉前缀、
请求体 / chunked / HEAD / 304 / Connection: close / 上游断开 / 旧连接失效重发；以及不带 --route 时仍是原样转发。
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import importlib.util
import json
import shutil
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

PROXY_FILE = Path(__file__).resolve().parent.parent / "deploy" / "lan_https_proxy.py"

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl 生成自签证书")


@pytest.fixture(scope="module")
def proxy_mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lan_https_proxy", PROXY_FILE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass 在类定义时要从 sys.modules 找本模块
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tls_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("tls")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
         "-keyout", str(d / "privkey.pem"), "-out", str(d / "fullchain.pem")],
        check=True, capture_output=True,
    )
    return d


class Upstream(BaseHTTPRequestHandler):
    """假上游：把收到的方法、路径、请求体回显成 JSON；几个特殊路径测各种响应定界方式。"""

    protocol_version = "HTTP/1.1"
    name = "?"

    def log_message(self, *args: object) -> None:
        pass

    def _echo(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        return json.dumps({"server": self.name, "method": self.command, "path": self.path, "body": body,
                           "te": self.headers.get("Transfer-Encoding")}).encode()

    def do_GET(self) -> None:  # noqa: N802
        payload = self._echo()
        if self.path.endswith("/chunked"):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (payload[:5], payload[5:]):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path.endswith("/304"):
            self.send_response(304)
            self.end_headers()
            return
        if self.path.endswith("/eof"):  # 没有长度，读到关闭为止
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        if self.path.endswith("/once"):  # 回完就悄悄关掉连接（不带 Connection: close），模拟上游重启 / 空闲超时
            self.close_connection = True

    do_HEAD = do_GET
    do_POST = do_GET


@contextlib.contextmanager
def upstream(name: str) -> Iterator[int]:
    handler = type(f"Upstream{name}", (Upstream,), {"name": name})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def proxy(mod: ModuleType, tls_dir: Path, handler_factory) -> Iterator[int]:  # noqa: ANN001
    loop = asyncio.new_event_loop()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(tls_dir / "fullchain.pem", tls_dir / "privkey.pem")
    ctx.set_alpn_protocols(["http/1.1"])
    server = loop.run_until_complete(
        asyncio.start_server(handler_factory(), "127.0.0.1", 0, ssl=ctx, limit=mod.MAX_HEAD)
    )
    port = server.sockets[0].getsockname()[1]
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    async def shutdown() -> None:
        # Python 3.12 的 server.close() 不关已有连接：把还在等下一个请求的处理协程取消掉，让它们在循环里收尾
        server.close()
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    try:
        yield port
    finally:
        asyncio.run_coroutine_threadsafe(shutdown(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)
        loop.close()


def client(port: int) -> http.client.HTTPSConnection:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)


def get(conn: http.client.HTTPSConnection, method: str, path: str, **kw) -> tuple[int, dict, http.client.HTTPResponse]:  # noqa: ANN003
    conn.request(method, path, **kw)
    r = conn.getresponse()
    data = r.read()
    return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") or
                      (data and data.startswith(b"{")) else {}), r


@pytest.fixture
def routed(proxy_mod: ModuleType, tls_dir: Path) -> Iterator[tuple[int, int, int]]:
    with upstream("cast") as a, upstream("reader") as b:
        route = proxy_mod.Route.parse(f"/reader/=127.0.0.1:{b}")
        with proxy(proxy_mod, tls_dir, lambda: proxy_mod.make_http_handler(("127.0.0.1", a), [route])) as port:
            yield port, a, b


@pytest.mark.parametrize(
    ("text", "target", "expected"),
    [
        ("/reader/=127.0.0.1:8780", "/reader/r/k/b/s/1-0.png", "/r/k/b/s/1-0.png"),
        ("/reader/=127.0.0.1:8780", "/reader", "/"),
        ("/reader/=127.0.0.1:8780", "/reader?x=1", "/?x=1"),
        ("/reader/=127.0.0.1:8780", "/reader/healthz?a=b", "/healthz?a=b"),
        ("/reader/=127.0.0.1:8780", "/readerx/a", None),
        ("/reader/=127.0.0.1:8780", "/s/key/screen.jpg/1_2.jpg", None),
    ],
)
def test_route_match(proxy_mod: ModuleType, text: str, target: str, expected: str | None) -> None:
    assert proxy_mod.Route.parse(text).match(target) == expected


@pytest.mark.parametrize("bad", ["reader/=127.0.0.1:8780", "/reader=127.0.0.1:8780", "/reader/=127.0.0.1", "/reader/=:80",
                                 "/reader/127.0.0.1:8780", "/reader/=h:99999"])
def test_route_parse_rejects(proxy_mod: ModuleType, bad: str) -> None:
    with pytest.raises(ValueError):
        proxy_mod.Route.parse(bad)


def test_redact(proxy_mod: ModuleType) -> None:
    assert proxy_mod.redact("/s/KEY/screen.jpg/1.jpg") == "/s/…/screen.jpg/1.jpg"
    assert proxy_mod.redact("/reader/r/KEY/b/s/1.png") == "/reader/r/…/b/s/1.png"


def test_one_connection_alternates_between_services(routed: tuple[int, int, int]) -> None:
    port, _, _ = routed
    conn = client(port)
    seen = []
    for path in ["/s/k/screen.jpg/1_1.jpg", "/reader/r/k/b/s/0-0-0-0-0-0.png", "/healthz", "/reader/healthz",
                 "/reader", "/readerx", "/reader/r/k/b/s/1-0-0-0-0-0.png?x=1"]:
        status, echo, _ = get(conn, "GET", path)
        assert status == 200
        seen.append((echo["server"], echo["path"]))
    conn.close()
    assert seen == [
        ("cast", "/s/k/screen.jpg/1_1.jpg"),
        ("reader", "/r/k/b/s/0-0-0-0-0-0.png"),
        ("cast", "/healthz"),
        ("reader", "/healthz"),
        ("reader", "/"),
        ("cast", "/readerx"),
        ("reader", "/r/k/b/s/1-0-0-0-0-0.png?x=1"),
    ]


def test_bodies_and_framing(routed: tuple[int, int, int]) -> None:
    port, _, _ = routed
    conn = client(port)
    status, echo, _ = get(conn, "POST", "/reader/mcp", body=b'{"jsonrpc":"2.0"}', headers={"Content-Type": "application/json"})
    assert status == 200 and echo == {"server": "reader", "method": "POST", "path": "/mcp", "body": '{"jsonrpc":"2.0"}', "te": None}
    # chunked 请求体：转发时改成定长
    status, echo, _ = get(conn, "POST", "/reader/mcp", body=iter([b"ab", b"cd"]), encode_chunked=True,
                          headers={"Transfer-Encoding": "chunked"})
    assert status == 200 and echo["body"] == "abcd" and echo["te"] is None
    # Expect: 100-continue 由转发器自己回 100
    status, echo, _ = get(conn, "POST", "/reader/mcp", body=b"xyz", headers={"Expect": "100-continue"})
    assert status == 200 and echo["body"] == "xyz"
    # chunked 响应原样转发
    status, echo, r = get(conn, "GET", "/reader/chunked")
    assert status == 200 and echo["path"] == "/chunked" and r.getheader("Transfer-Encoding") == "chunked"
    # HEAD 和 304 没有响应体，连接还能继续用
    status, _, r = get(conn, "HEAD", "/reader/x")
    assert status == 200 and int(r.getheader("Content-Length")) > 0
    status, _, _ = get(conn, "GET", "/reader/304")
    assert status == 304
    status, echo, _ = get(conn, "GET", "/s/after")
    assert status == 200 and echo["server"] == "cast"
    conn.close()


def test_read_until_close(routed: tuple[int, int, int]) -> None:
    port, _, _ = routed
    conn = client(port)
    status, echo, r = get(conn, "GET", "/reader/eof")
    assert status == 200 and echo["path"] == "/eof" and r.getheader("Connection") == "close"
    status, echo, _ = get(conn, "GET", "/reader/again")  # http.client 会自动重连
    assert status == 200 and echo["path"] == "/again"
    conn.close()


def test_stale_upstream_connection_is_retried(routed: tuple[int, int, int]) -> None:
    port, _, _ = routed
    conn = client(port)
    assert get(conn, "GET", "/reader/once")[0] == 200  # 上游回完悄悄关掉连接
    status, echo, _ = get(conn, "GET", "/reader/next")  # 转发器复用旧连接失败，换新连接重发
    assert status == 200 and echo["path"] == "/next"
    conn.close()


def test_upstream_down_is_502(proxy_mod: ModuleType, tls_dir: Path) -> None:
    with upstream("cast") as a:
        dead = proxy_mod.Route.parse("/reader/=127.0.0.1:9")  # discard 端口，没人监听
        with proxy(proxy_mod, tls_dir, lambda: proxy_mod.make_http_handler(("127.0.0.1", a), [dead])) as port:
            conn = client(port)
            conn.request("GET", "/reader/r/k/b/s/0.png")
            r = conn.getresponse()
            assert r.status == 502 and r.getheader("Connection") == "close"
            assert "上游服务没有响应" in r.read().decode()
            conn.close()
            ok = client(port)
            assert get(ok, "GET", "/s/x")[0] == 200  # 另一个上游不受影响
            ok.close()


def test_bad_request_is_400(routed: tuple[int, int, int]) -> None:
    port, _, _ = routed
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, ctx.wrap_socket(raw) as s:
        s.sendall(b"NONSENSE\r\n\r\n")
        data = s.recv(4096)
    assert data.startswith(b"HTTP/1.1 400 Bad Request")


def test_without_routes_is_a_byte_pipe(proxy_mod: ModuleType, tls_dir: Path) -> None:
    with upstream("cast") as a:
        with proxy(proxy_mod, tls_dir, lambda: proxy_mod.make_handler("127.0.0.1", a)) as port:
            conn = client(port)
            status, echo, _ = get(conn, "GET", "/reader/r/k/b/s/0.png")
            assert status == 200 and echo == {"server": "cast", "method": "GET", "path": "/reader/r/k/b/s/0.png",
                                              "body": "", "te": None}
            conn.close()
