"""HTTP 服务：眼镜拉页图，加上挂在 ``/mcp`` 的 MCP 工具（docs/research.md §4.2、§4.6）。

==============================================  ==============================================================
``GET /healthz``                                ``200 ok``
``GET /r/{key}/{book}/{session}/{n}.png``       当前页的 4 级灰度 PNG。``n`` 是页面的累计计数（``a-b-j-k-…``），
                                                按差值移动阅读位置（:meth:`~glassreader.library.Library.turn`）
``GET /r/{key}/{book}/{session}/title.png``     左栏竖排书名图
``GET /r/{key}/{book}/{session}/{n}.jpg``       同上，JPEG（真机不显示 PNG 时的退路，research §7 P1）
``/mcp``                                        MCP streamable HTTP（stateless + JSON 响应），Bearer 鉴权
``WS /agent``                                   Agent 自定义接入（文字传输版，glassreader/agent.py），Bearer 鉴权
==============================================  ==============================================================

- 眼镜发不了请求头，页图只能靠路径里的随机 key 鉴权；key 不对、书不存在、地址不合法一律同样的 404，
  访问日志里 key 打码（uvicorn 自己的访问日志会打出完整路径，CLI 里关掉了它）。
- 页图地址必须以 ``.png`` / ``.jpg`` 结尾：手机 App 按后缀认图片类型（glasscast research §9.6）。
- 所有响应 ``Cache-Control: no-store``，并带 CORS / CORP 头：本地 WASM 模拟器是 cross-origin isolated 页面，
  内核用 XHR 下载，缺这两个头就加载不了（glasscast research §9.2 E8）。
- 必须在 ASGI lifespan 里运行（uvicorn 默认会；测试用 ``app.router.lifespan_context``），否则 MCP 的
  session manager 没启动，``/mcp`` 返回 500。
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import re
import threading
import time
from collections.abc import AsyncIterator

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket

from .agent import AGENT_PATH, AgentHub, serve
from .config import Settings
from .library import Library, parse_counters
from .mcp_tools import ReaderTools, build_mcp_server

__all__ = ["COMMON_HEADERS", "MCP_PATH", "create_app", "redact_path"]

log = logging.getLogger(__name__)
access_log = logging.getLogger("glassreader.access")

MCP_PATH = "/mcp"
MCP_MAX_BODY_BYTES = 256 * 1024
COMMON_HEADERS: tuple[tuple[str, str], ...] = (
    ("cache-control", "no-store"),
    ("access-control-allow-origin", "*"),
    ("cross-origin-resource-policy", "cross-origin"),
)
_PAGE_FILE = re.compile(r"^([0-9]{1,9}(?:-[0-9]{1,9}){0,12})\.(png|jpg)$")
_TITLE_FILE = re.compile(r"^title(?:-[0-9]{1,9})*\.png$")
_BOOK_ID = re.compile(r"^[0-9a-f]{10}$")
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_KEY_IN_PATH = re.compile(r"^/r/[^/]+")


def redact_path(path: str) -> str:
    """``/r/<key>/…`` → ``/r/…/…``。"""
    return _KEY_IN_PATH.sub("/r/…", path)


class CommonHeadersMiddleware:
    """给每个 HTTP 响应补上 :data:`COMMON_HEADERS`（已有的同名头不覆盖）。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in COMMON_HEADERS]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw}
                raw.extend((k, v) for k, v in self.headers if k not in present)
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class AccessLogMiddleware:
    """一行一请求的访问日志（logger ``glassreader.access``），路径里的 key 已打码。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        t0 = time.perf_counter()
        status, nbytes = 0, 0

        async def counting(message: Message) -> None:
            nonlocal status, nbytes
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body":
                nbytes += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, counting)
        finally:
            headers = Headers(scope=scope)
            client = headers.get("x-real-ip") or (scope.get("client") or ("-",))[0]
            access_log.info('%s %s %s %dB %.1fms %s "%s"', scope.get("method", "-"), redact_path(scope.get("path", "")),
                            status or "-", nbytes, (time.perf_counter() - t0) * 1000, client,
                            headers.get("user-agent", "-")[:80])


class McpGuardMiddleware:
    """``/mcp`` 及其子路径：校验 Bearer Token（常量时间比较），stateless 模式下拒绝 GET（405）。

    平台插件选 Bearer Token 时每个请求自动带 ``Authorization: Bearer <token>``；常见误填是在 Token 栏里也写了
    ``Bearer ``，这里宽松处理（也接受裸 token），判断依据始终是 token 本身。
    """

    def __init__(self, app: ASGIApp, *, token: str, path: str = MCP_PATH) -> None:
        if not token:
            raise ValueError("MCP token 不能为空")
        self.app = app
        self.token = token.encode("utf-8")
        self.path = path
        self._lock = threading.Lock()
        self._last_reject_log = 0.0

    @staticmethod
    def extract_token(header: str | None) -> str:
        value = (header or "").strip()
        for _ in range(2):  # “Bearer Bearer <token>”
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                value = parts[1].strip()
        return value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == self.path or path.startswith(self.path + "/")):
            await self.app(scope, receive, send)
            return
        token = self.extract_token(Headers(scope=scope).get("authorization"))
        if not token or not hmac.compare_digest(token.encode("utf-8"), self.token):
            self._log_reject()
            response: Response = JSONResponse(
                {"error": "unauthorized", "message": "缺少或错误的 Bearer Token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="glassreader"'},
            )
        elif scope.get("method") == "GET":
            response = PlainTextResponse("Method Not Allowed: stateless MCP endpoint, use POST", status_code=405,
                                         headers={"Allow": "POST"})
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)

    def _log_reject(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_reject_log < 10.0:
                return
            self._last_reject_log = now
        log.warning("拒绝 MCP 请求（401）：Bearer Token 缺失或不匹配。检查平台插件里的 Token 是否等于 READER_TOKEN")


def create_app(
    settings: Settings,
    library: Library,
    *,
    mcp_server: MCPServer | None = None,
    access_log: bool = True,
) -> Starlette:
    if not settings.token or not settings.key:
        raise ValueError("create_app 需要非空的 token 和 key（用 load_settings 生成）")
    server = mcp_server or build_mcp_server(settings, library)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.dns_rebinding_protection,
        allowed_hosts=settings.mcp_allowed_hosts(),
        allowed_origins=settings.mcp_allowed_origins(),
    )
    # stateless + JSON 响应：平台每次调用都是独立的 POST（插件选「无状态协议」，glasscast research §9.9）
    mcp_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=security,
        host=settings.host,
        max_request_body_size=MCP_MAX_BODY_BYTES,
    )
    session_manager = server.session_manager
    key = settings.key.encode("utf-8")

    def not_found() -> Response:
        return PlainTextResponse("Not Found", status_code=404)

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def page(request: Request) -> Response:
        p = request.path_params
        m = _PAGE_FILE.match(p["file"])
        title = _TITLE_FILE.match(p["file"])
        if (
            not hmac.compare_digest(str(p["key"]).encode("utf-8"), key)
            or not (m or title)
            or not _BOOK_ID.match(p["book"])
            or not _SESSION.match(p["session"])
        ):
            return not_found()
        if title:
            def render_book_title() -> bytes | None:
                book = library.book(p["book"])
                return None if book is None else library.title_image(book)

            png = await anyio.to_thread.run_sync(render_book_title)
            return not_found() if png is None else Response(png, media_type="image/png")
        counters, ext = parse_counters(m.group(1)), m.group(2)

        def render() -> tuple[bytes, dict[str, str]] | None:
            book = library.book(p["book"])
            if book is None:
                return None
            v, note = library.turn(book, p["session"], counters)
            log.info("《%s》 会话 %s %s → %s %s 第 %d/%d 页", book.title, p["session"], m.group(1), note,
                     book.chapters[v.chapter].title, v.page + 1, len(v.pages.pages))
            headers = {
                "X-Reader-Chapter": str(v.chapter + 1),
                "X-Reader-Page": f"{v.page + 1}/{len(v.pages.pages)}",
                "X-Reader-Font": str(v.font_size),
                "X-Reader-Percent": str(book.percent(v.offset)),
            }
            return library.page_image(v, ext), headers

        result = await anyio.to_thread.run_sync(render)
        if result is None:
            return not_found()
        data, headers = result
        return Response(data, media_type="image/png" if ext == "png" else "image/jpeg", headers=headers)

    hub = AgentHub(ReaderTools(settings, library), library)

    async def agent(ws: WebSocket) -> None:
        await serve(ws, hub, settings.token, McpGuardMiddleware.extract_token)

    async def internal_error(request: Request, exc: Exception) -> Response:
        # 500 由 ServerErrorMiddleware 直接发出、绕过上面的中间件，通用头要写在这里
        # （缺 CORP 的响应，模拟器连状态码都看不到）
        log.error("处理 %s %s 时出错：%s: %s", request.method, redact_path(request.url.path), type(exc).__name__, exc)
        return PlainTextResponse("Internal Server Error", status_code=500, headers=dict(COMMON_HEADERS))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    middleware = [Middleware(CommonHeadersMiddleware)]
    if access_log:
        middleware.append(Middleware(AccessLogMiddleware))
    middleware.append(Middleware(McpGuardMiddleware, token=settings.token, path=MCP_PATH))
    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/r/{key}/{book}/{session}/{file}", page, methods=["GET"]),
            WebSocketRoute(AGENT_PATH, agent),
            *mcp_app.routes,
        ],
        middleware=middleware,
        lifespan=lifespan,
        exception_handlers={Exception: internal_error},
    )
    app.state.settings = settings
    app.state.library = library
    app.state.mcp_server = server
    app.state.agent_hub = hub
    return app
