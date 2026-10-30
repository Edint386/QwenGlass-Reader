"""MCP 工具：``list_books`` / ``open_book`` / ``jump`` / ``reading_status``（docs/research.md §4.6）。

只管“打开哪本、跳到哪”这类偶发操作；翻页在眼镜页面里完成，不经过大模型（research §0 第 1 条）。

- ``open_book`` / ``jump`` 返回页面要的两个字段 ``u``（页图地址前缀，带新会话号）和 ``t``（书名），
  由大模型照抄进 ``UI页面渲染(templateName="reader", dataSource={"u", "t"})``（skill/SKILL.md）。
  每次都换新会话号：内核按完整 URL 缓存页图，电脑端按会话记计数基线（research §4.2）。
- 平台把 MCP 出参只当 ``content[0].text`` 交给模型（glasscast research §9.5），所以返回体是扁平的 JSON 对象；
  ``message`` 是可以直接念给用户的一句中文，技术细节放 ``error``。
- 工具必须 3 s 内返回（research §1 约束 4）：按章分页、按需解析，几 MB 的书第一次打开也在 1 s 内。
- 工具描述会交给大模型选工具、平台上改不了，所以用中文写清触发场景。

逻辑在 :class:`ReaderTools`（不依赖 SDK，直接单测），:func:`build_mcp_server` 再注册成 SDK 2.x 的工具。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Required, TypedDict

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

from . import __version__
from .book import Book
from .config import Settings
from .library import Entry, Library, View, new_session_id

__all__ = [
    "SERVER_INSTRUCTIONS",
    "TOOL_NAMES",
    "BookItem",
    "BookResult",
    "ListBooksResult",
    "ReaderTools",
    "StatusResult",
    "build_mcp_server",
]

log = logging.getLogger(__name__)

TOOL_NAMES: tuple[str, ...] = ("list_books", "open_book", "jump", "reading_status")
#: list_books 最多列几本（念给用户听的 message 只念前 5 本）
MAX_LIST = 20

SERVER_INSTRUCTIONS = (
    "在千问 AI 眼镜上看电脑里的 TXT 小说。open_book / jump 成功后，必须用平台的 UI 页面渲染工具打开页面："
    'templateName="reader"，dataSource={"u": 返回的 u, "t": 返回的 t}，两个字段原样照抄。'
    "翻页、开菜单都在眼镜上用镜腿完成，不要为了翻页调用工具。"
)


class BookItem(TypedDict, total=False):
    title: Required[str]
    #: 读到的章节名；没读过为空串
    chapter: str
    #: 全书百分比；没读过为 0
    percent: int
    #: 最后阅读时间 "YYYY-MM-DD HH:MM:SS"；没读过为空串
    last_read: str


class ListBooksResult(TypedDict, total=False):
    success: Required[bool]
    message: Required[str]
    #: 书架上（符合 query 的）总本数
    count: int
    #: 最近读的在前，最多 20 本
    books: list[BookItem]
    error: str | None


class BookResult(TypedDict, total=False):
    """open_book / jump 的返回体。成功时 u、t 齐全；失败时 success=false、message（可念）、candidates（可能的书名）。"""

    success: Required[bool]
    message: Required[str]
    #: 页面 dataSource 的 u：页图地址前缀（含新会话号）。原样照抄，不要念出来
    u: str
    #: 页面 dataSource 的 t：书名
    t: str
    title: str
    chapter: str
    #: 当前章节是第几个章节（1 起，含前言 / 序章）
    chapter_index: int
    chapters: int
    #: 本章第几页 / 本章页数（按当前字号）
    page: int
    pages: int
    percent: int
    font_size: int
    #: 没找到书或章节时，可能想要的书名 / 章节名
    candidates: list[str]
    error: str | None


class StatusResult(TypedDict, total=False):
    success: Required[bool]
    message: Required[str]
    title: str
    chapter: str
    chapter_index: int
    chapters: int
    page: int
    pages: int
    percent: int
    font_size: int
    last_read: str
    error: str | None


def _quote(title: str) -> str:
    return f"《{title}》"


class ReaderTools:
    def __init__(self, settings: Settings, library: Library):
        self.settings = settings
        self.library = library

    # ------------------------------------------------------------------ 选书

    def _pick(self, title: str) -> tuple[Entry | None, BookResult | None]:
        """按书名选书；title 为空 = 最近读的那本。找不到时第二项是可以直接返回的失败结果。"""
        lib = self.library
        if title.strip():
            found = lib.find(title)
            if found:
                return found[0], None
            names = [e.title for e in lib.find("")][:5]
            msg = f"书架上没有找到{_quote(title.strip('《》 '))}"
            if names:
                msg += "，有：" + "、".join(_quote(n) for n in names)
            return None, {"success": False, "message": msg, "candidates": names, "error": "book not found"}
        recent = lib.recent()
        if recent:
            return recent, None
        entries = lib.find("")
        if len(entries) == 1:
            return entries[0], None
        if not entries:
            return None, {
                "success": False,
                "message": "书架是空的，请把 TXT 小说放进电脑上的书库文件夹",
                "candidates": [],
                "error": f"no .txt in {self.settings.books_dir}",
            }
        names = [e.title for e in entries][:5]
        return None, {
            "success": False,
            "message": "还没有阅读记录，想看哪一本？书架上有：" + "、".join(_quote(n) for n in names),
            "candidates": names,
            "error": "no recent book",
        }

    def _book_result(self, v: View, message: str) -> BookResult:
        book = v.book
        return {
            "success": True,
            "message": message,
            "u": self.settings.page_prefix(book.id, new_session_id()),
            "t": book.title,
            "title": book.title,
            "chapter": book.chapters[v.chapter].title,
            "chapter_index": v.chapter + 1,
            "chapters": len(book.chapters),
            "page": v.page + 1,
            "pages": len(v.pages.pages),
            "percent": book.percent(v.offset),
            "font_size": v.font_size,
        }

    def _load(self, entry: Entry) -> Book | BookResult:
        book = self.library.book(entry.id)
        if book is None:
            msg = f"{_quote(entry.title)}的文件打不开了"
            return {"success": False, "message": msg, "error": f"cannot read {entry.path}"}
        return book

    # ------------------------------------------------------------------ 工具

    def list_books(self, query: Any = "") -> ListBooksResult:
        query = "" if query is None else str(query)
        lib = self.library
        lib.scan()
        entries = lib.find(query, rescan=False)
        items: list[BookItem] = []
        for e in entries[:MAX_LIST]:
            p = lib.progress.get(e.id, {})
            items.append({
                "title": e.title,
                "chapter": str(p.get("chapter", "")),
                "percent": int(p.get("percent", 0)),
                "last_read": str(p.get("updated", "")),
            })
        if not entries:
            empty = "书架是空的，请把 TXT 小说放进电脑上的书库文件夹"
            msg = f"书架上没有找到{_quote(query)}" if query.strip() else empty
        else:
            spoken = "、".join(
                _quote(i["title"]) + (f"读到{i['percent']}%" if i["last_read"] else "") for i in items[:5]
            )
            msg = f"书架上有{len(entries)}本：{spoken}" + ("等" if len(entries) > 5 else "")
        return {"success": True, "message": msg, "count": len(entries), "books": items}

    def open_book(self, title: Any = "") -> BookResult:
        entry, failure = self._pick("" if title is None else str(title))
        if failure:
            return failure
        assert entry is not None
        book = self._load(entry)
        if isinstance(book, dict):
            return book
        fresh = entry.id not in self.library.progress
        v = self.library.view(book)
        self.library.save(v)  # 记一次“最近阅读”，下次说“继续看”就是这本
        where = "从头开始" if fresh else book.chapters[v.chapter].title
        return self._book_result(v, f"已打开{_quote(book.title)}，{where}")

    def jump(self, title: Any = "", chapter: Any = "", percent: Any = None, pages: Any = None) -> BookResult:
        entry, failure = self._pick("" if title is None else str(title))
        if failure:
            return failure
        assert entry is not None
        book = self._load(entry)
        if isinstance(book, dict):
            return book
        chapter_text = "" if chapter is None else str(chapter).strip()
        try:
            pct = None if percent in (None, "") else float(str(percent).rstrip("%"))
            delta = None if pages in (None, "") else int(float(str(pages)))
        except ValueError:
            error = f"bad percent/pages: {percent!r} {pages!r}"
            return {"success": False, "message": "没听清要跳到哪里", "error": error}
        if not chapter_text and pct is None and not delta:
            return {"success": False, "message": "要跳到第几章，还是百分之多少？", "error": "nothing to jump to"}
        v = self.library.jump(book, chapter=chapter_text or None, percent=pct, pages=delta)
        if v is None:
            names = [c.title for c in book.chapters][:5]
            return {
                "success": False,
                "message": f"{_quote(book.title)}里没有找到{chapter_text}",
                "candidates": names,
                "error": "chapter not found",
            }
        ch_title = book.chapters[v.chapter].title
        if chapter_text:
            msg = f"已跳到{ch_title}"
        elif pct is not None:
            msg = f"已跳到{book.percent(v.offset)}%，{ch_title}"
        else:
            msg = f"已往{'后' if delta > 0 else '前'}翻{abs(delta)}页"
        return self._book_result(v, msg)

    def reading_status(self, title: Any = "") -> StatusResult:
        entry, failure = self._pick("" if title is None else str(title))
        if failure:
            return {k: v for k, v in failure.items() if k in ("success", "message", "error")}  # type: ignore[return-value]
        assert entry is not None
        p = self.library.progress.get(entry.id)
        if not p:
            return {"success": True, "message": f"{_quote(entry.title)}还没开始读", "title": entry.title, "percent": 0}
        book = self._load(entry)
        if isinstance(book, dict):
            return {k: v for k, v in book.items() if k in ("success", "message", "error")}  # type: ignore[return-value]
        v = self.library.view(book)
        ch_title = book.chapters[v.chapter].title
        return {
            "success": True,
            "message": f"{_quote(book.title)}读到{ch_title}，全书{book.percent(v.offset)}%",
            "title": book.title,
            "chapter": ch_title,
            "chapter_index": v.chapter + 1,
            "chapters": len(book.chapters),
            "page": v.page + 1,
            "pages": len(v.pages.pages),
            "percent": book.percent(v.offset),
            "font_size": v.font_size,
            "last_read": str(p.get("updated", "")),
        }


LIST_BOOKS_DESCRIPTION = (
    "列出电脑书库里的 TXT 小说和每本读到哪里（最近读的在前）。用户问“书架上有什么书”“有哪些小说”“我在看哪些书”时调用。"
    "query 可选，按书名片段筛选。"
)
OPEN_BOOK_DESCRIPTION = (
    "打开一本小说，从上次读到的地方继续。用户说“看小说”“继续看”“接着读”“打开《某书》”“换一本看《某书》”时调用。"
    "title 是书名或书名片段，不说书名（继续看）时留空，打开最近读的那本。"
    '成功后必须用 UI 页面渲染工具打开页面：templateName="reader"，dataSource={"u": 返回的 u, "t": 返回的 t}。'
)
JUMP_DESCRIPTION = (
    "跳到某一章、某个百分比，或往前 / 往后翻几页。用户说“跳到第三章”“看下一章”“回到上一章”“从夜渡那章开始”"
    "“跳到一半”“回到开头”“往后翻五页”时调用。title 留空 = 当前在读的书。"
    "chapter：章节号或标题片段，如“第三章”“3”“夜渡”，也可以是“下一章”“上一章”；"
    "percent：0～100（“开头”= 0，“一半”= 50）；pages：往后翻为正、往前翻为负。三者给一个。"
    '成功后同样要用 UI 页面渲染工具重新打开页面：templateName="reader"，dataSource={"u": 返回的 u, "t": 返回的 t}。'
)
READING_STATUS_DESCRIPTION = (
    "查询读到哪里了：书名、章节、全书百分比、最后阅读时间。用户问“我看到哪了”“这本书读了多少”“上次读到哪”时调用。"
    "title 留空 = 最近读的那本。不会打开页面。"
)


# 参数按 Any 收（大模型常把章节号传成整数、把百分比传成 "50%"），schema 里照常声明类型，ReaderTools 再宽松解析
TitleParam = Annotated[Any, WithJsonSchema({"type": "string", "description": "书名或书名片段；留空 = 最近读的那本"})]
QueryParam = Annotated[Any, WithJsonSchema({"type": "string", "description": "书名片段，留空列出全部"})]
ChapterParam = Annotated[Any, WithJsonSchema(
    {"type": ["string", "integer"], "description": "章节号或标题片段，如“第三章”“3”“夜渡”“下一章”“上一章”"})]
PercentParam = Annotated[Any, WithJsonSchema(
    {"type": "number", "minimum": 0, "maximum": 100, "description": "全书百分比"})]
PagesParam = Annotated[Any, WithJsonSchema({"type": "integer", "description": "往后翻为正，往前翻为负"})]


def build_mcp_server(settings: Settings, library: Library, *, tools: ReaderTools | None = None) -> MCPServer:
    """创建注册好 4 个工具的 MCPServer。每个应用新建一个（SDK 的 session manager 只能 run 一次）。"""
    ctl = tools or ReaderTools(settings, library)
    server = MCPServer(
        "glassreader",
        title="眼镜看小说",
        description="在千问 AI 眼镜上看电脑里的 TXT 小说：书架、打开、跳章、查进度",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )

    def failure(tool: str, exc: BaseException) -> dict[str, Any]:
        log.exception("工具 %s 执行出错", tool)
        message = "电脑上的阅读程序出错了，请查看它的日志"
        return {"success": False, "message": message, "error": f"{type(exc).__name__}: {exc}"}

    def annotations(title: str, read_only: bool) -> ToolAnnotations:
        return ToolAnnotations(title=title, readOnlyHint=read_only, destructiveHint=False, idempotentHint=read_only,
                               openWorldHint=False)

    @server.tool(name="list_books", title="书架", description=LIST_BOOKS_DESCRIPTION,
                 annotations=annotations("书架", True))
    async def list_books(query: QueryParam = "") -> ListBooksResult:
        try:
            return await anyio.to_thread.run_sync(ctl.list_books, query)
        except Exception as exc:  # noqa: BLE001
            return failure("list_books", exc)  # type: ignore[return-value]

    @server.tool(name="open_book", title="打开小说", description=OPEN_BOOK_DESCRIPTION,
                 annotations=annotations("打开小说", False))
    async def open_book(title: TitleParam = "") -> BookResult:
        try:
            return await anyio.to_thread.run_sync(ctl.open_book, title)
        except Exception as exc:  # noqa: BLE001
            return failure("open_book", exc)  # type: ignore[return-value]

    @server.tool(name="jump", title="跳转", description=JUMP_DESCRIPTION, annotations=annotations("跳转", False))
    async def jump(
        title: TitleParam = "", chapter: ChapterParam = "", percent: PercentParam = None, pages: PagesParam = None
    ) -> BookResult:
        try:
            return await anyio.to_thread.run_sync(ctl.jump, title, chapter, percent, pages)
        except Exception as exc:  # noqa: BLE001
            return failure("jump", exc)  # type: ignore[return-value]

    @server.tool(name="reading_status", title="阅读进度", description=READING_STATUS_DESCRIPTION,
                 annotations=annotations("阅读进度", True))
    async def reading_status(title: TitleParam = "") -> StatusResult:
        try:
            return await anyio.to_thread.run_sync(ctl.reading_status, title)
        except Exception as exc:  # noqa: BLE001
            return failure("reading_status", exc)  # type: ignore[return-value]

    return server
