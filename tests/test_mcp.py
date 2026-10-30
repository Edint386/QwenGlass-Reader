"""MCP 工具。

- 端到端：mcp 2.x 的 streamable HTTP 客户端经 ``httpx2.ASGITransport`` 打到 :func:`create_app`，走一遍
  initialize → tools/list → tools/call（与平台调用路径一致，同 glasscast 的测试）。ASGITransport 不跑 lifespan，
  手动进入 ``app.router.lifespan_context``。
- 单元：:class:`ReaderTools` 的消息分支和失败形状。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from glassreader.config import Settings
from glassreader.library import Library
from glassreader.mcp_tools import TOOL_NAMES, ReaderTools, build_mcp_server
from glassreader.server import create_app

from .conftest import KEY, PUBLIC, TOKEN, make_settings

T = TypeVar("T")
#: 平台对工具统一 3000 ms 超时；这里要求 1 s 内返回
TOOL_BUDGET_S = 1.0


def run(coro_factory: Callable[[], Awaitable[T]], timeout: float = 30.0) -> T:
    async def wrapper() -> T:
        return await asyncio.wait_for(coro_factory(), timeout)

    return asyncio.run(wrapper())


@contextlib.asynccontextmanager
async def mcp_session(app: Starlette) -> AsyncIterator[tuple[ClientSession, httpx2.AsyncClient]]:
    base = PUBLIC.split("/reader")[0]  # 反代剥掉 /reader 前缀后转发到这里
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), base_url=base, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10
        ) as http:
            async with streamable_http_client(f"{base}/mcp", http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session, http


async def call(session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """调用工具并检查：1 s 内返回、不是 isError、structuredContent 与文本 JSON 一致。"""
    t0 = time.monotonic()
    result = await session.call_tool(name, arguments or {})
    assert time.monotonic() - t0 < TOOL_BUDGET_S
    assert result.is_error is False
    text = json.loads(result.content[0].text)
    assert result.structured_content == text
    return text


def local_path(u: str) -> str:
    """把 u（公网地址，带 /reader 前缀）换成本应用里的路径。"""
    assert u.startswith(PUBLIC + "/r/")
    return u[len(PUBLIC) :]


def test_full_flow(settings: Settings, library: Library) -> None:
    app = create_app(settings, library)

    async def flow() -> None:
        async with mcp_session(app) as (s, http):
            tools = await s.list_tools()
            assert [t.name for t in tools.tools] == list(TOOL_NAMES)
            assert all(t.description and t.output_schema for t in tools.tools)

            shelf = await call(s, "list_books")
            assert shelf["success"] and shelf["count"] == 2
            assert "书架上有2本" in shelf["message"]

            opened = await call(s, "open_book", {"title": "《测试长篇》"})
            assert opened["success"] and opened["t"] == opened["title"] == "测试长篇"
            assert opened["message"] == "已打开《测试长篇》，从头开始"
            path = local_path(opened["u"])
            assert path.startswith(f"/r/{KEY}/{library.find('测试长篇')[0].id}/")

            # 眼镜按 u 拉页图并翻页
            r = await http.get(path + "/0-0-0-0-0-0.png")
            assert r.status_code == 200 and r.headers["content-type"] == "image/png"
            r = await http.get(path + "/1-0-0-0-0-0.png")
            assert r.headers["x-reader-chapter"] == "2"

            again = await call(s, "open_book", {})  # 继续看 = 最近读的那本，位置是刚翻到的
            assert again["title"] == "测试长篇" and again["chapter"] == "第一章 标题1"
            assert again["u"] != opened["u"]  # 每次新会话号

            jumped = await call(s, "jump", {"chapter": "第三章"})
            assert jumped["success"] and jumped["message"] == "已跳到第三章 标题3"
            assert jumped["chapter_index"] == 4 and jumped["page"] == 1

            status = await call(s, "reading_status")
            assert status["message"] == "《测试长篇》读到第三章 标题3，全书" + str(status["percent"]) + "%"
            assert status["last_read"]

            other = await call(s, "open_book", {"title": "雾港"})
            assert other["title"] == "雾港来信"
            assert (await call(s, "reading_status"))["title"] == "雾港来信"
            assert (await call(s, "reading_status", {"title": "测试长篇"}))["chapter"] == "第三章 标题3"

    run(flow)


def test_jump_accepts_loose_arguments(settings: Settings, library: Library) -> None:
    app = create_app(settings, library)

    async def flow() -> None:
        async with mcp_session(app) as (s, _):
            await call(s, "open_book", {"title": "测试长篇"})
            assert (await call(s, "jump", {"chapter": 2}))["chapter"] == "第二章 标题2"
            half = await call(s, "jump", {"percent": "50%"})
            assert 40 <= half["percent"] <= 55 and half["message"].startswith("已跳到")
            back = await call(s, "jump", {"pages": "-2"})
            assert back["message"] == "已往前翻2页"
            assert (await call(s, "jump", {"chapter": "下一章"}))["success"]

    run(flow)


def test_failures(settings: Settings, library: Library) -> None:
    app = create_app(settings, library)

    async def flow() -> None:
        async with mcp_session(app) as (s, _):
            missing = await call(s, "open_book", {"title": "三体"})
            assert not missing["success"] and missing["message"].startswith("书架上没有找到《三体》，有：")
            assert set(missing["candidates"]) == {"测试长篇", "雾港来信"}
            # 还没读过、书架上有两本：不知道打开哪本
            first = await call(s, "open_book")
            assert not first["success"] and "想看哪一本" in first["message"]
            await call(s, "open_book", {"title": "测试长篇"})
            nothing = await call(s, "jump")
            assert not nothing["success"] and nothing["message"] == "要跳到第几章，还是百分之多少？"
            bad = await call(s, "jump", {"chapter": "第九十九章"})
            assert not bad["success"] and bad["message"] == "《测试长篇》里没有找到第九十九章"
            garbled = await call(s, "jump", {"percent": "一半"})
            assert not garbled["success"] and garbled["message"] == "没听清要跳到哪里"

    run(flow)


def test_tool_exception_becomes_failure_object(settings: Settings, library: Library) -> None:
    class Boom(ReaderTools):
        def list_books(self, query: Any = "") -> Any:
            raise RuntimeError("磁盘坏了")

    app = create_app(settings, library, mcp_server=build_mcp_server(settings, library, tools=Boom(settings, library)))

    async def flow() -> None:
        async with mcp_session(app) as (s, _):
            out = await call(s, "list_books")
            assert out == {"success": False, "message": "电脑上的阅读程序出错了，请查看它的日志",
                           "error": "RuntimeError: 磁盘坏了"}

    run(flow)


# --------------------------------------------------------------------------- ReaderTools 单元


def tools_for(books_dir: Path, state_file: Path) -> ReaderTools:
    settings = make_settings(books_dir, state_file)
    return ReaderTools(settings, Library(settings.books_dir, settings.state_file))


def test_empty_shelf(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    t = tools_for(empty, tmp_path / "p.json")
    assert t.list_books()["message"] == "书架是空的，请把 TXT 小说放进电脑上的书库文件夹"
    out = t.open_book()
    assert not out["success"] and out["message"] == "书架是空的，请把 TXT 小说放进电脑上的书库文件夹"
    assert t.reading_status() == {"success": False, "message": out["message"], "error": out["error"]}


def test_single_book_opens_without_title(tmp_path: Path, books_dir: Path) -> None:
    (books_dir / "测试长篇.txt").unlink()
    t = tools_for(books_dir, tmp_path / "p.json")
    out = t.open_book("")
    assert out["success"] and out["title"] == "雾港来信"


def test_status_of_unread_book(books_dir: Path, state_file: Path) -> None:
    t = tools_for(books_dir, state_file)
    out = t.reading_status("雾港来信")
    assert out == {"success": True, "message": "《雾港来信》还没开始读", "title": "雾港来信", "percent": 0}


def test_list_books_message_and_order(books_dir: Path, state_file: Path) -> None:
    for i in range(6):
        (books_dir / f"书{i}.txt").write_text("正文\n", encoding="utf-8")
    t = tools_for(books_dir, state_file)
    t.open_book("雾港来信")
    out = t.list_books()
    assert out["count"] == 8 and out["books"][0]["title"] == "雾港来信"
    assert out["message"].startswith("书架上有8本：《雾港来信》读到0%、") and out["message"].endswith("等")
    assert t.list_books("书3")["books"] == [{"title": "书3", "chapter": "", "percent": 0, "last_read": ""}]


def test_page_prefix_uses_public_url(books_dir: Path, state_file: Path) -> None:
    t = tools_for(books_dir, state_file)
    u = t.open_book("雾港来信")["u"]
    prefix = f"{PUBLIC}/r/{KEY}/{t.library.find('雾港来信')[0].id}/"
    assert u.startswith(prefix) and u[len(prefix) :].isalnum()
