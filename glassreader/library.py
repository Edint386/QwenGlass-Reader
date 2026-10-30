"""书库、阅读位置和进度（docs/research.md §4.2、§4.5）。

- 书库是一个目录，递归收集 ``*.txt``；书按需解析，文件改了（mtime 变了）下次用到时重新解析。
- 阅读位置 = 全书字符偏移 + 字号，存在一个 JSON 文件里（原子替换）。换字号重新分页后仍落在同一段文字。
- 眼镜页面每个操作只给六个累计计数器中的一个加一（a 下一页、b 上一页、j 下一章、k 上一章、f 字号加、g 字号减），
  页图地址里带着全部六个值。这里记住每个会话上次看到的计数，按差值移动位置：
  内核重复请求同一地址时差值为 0；中间某个地址没发出来，下一个地址的计数已经包含了它。
  会话的第一次请求只记基线：页面经 on-new-intent 换了新会话时，计数器不会归零。
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from .book import Book, book_id_for
from .pager import DEFAULT_FONT_SIZE, FONT_SIZES, ChapterPages, layout_for, paginate_chapter, render_page

__all__ = ["COUNTER_NAMES", "Entry", "Library", "View", "new_session_id", "normalize_title", "parse_counters"]

log = logging.getLogger(__name__)

#: 页图地址里六个计数器的顺序（与 pages/reader/template.xml 的 src 一致）
COUNTER_NAMES = ("next", "prev", "next_chapter", "prev_chapter", "font_up", "font_down")
#: 记住多少个会话的计数基线（每次打开页面一个会话）
MAX_SESSIONS = 512
_NEXT_CHAPTER = {"下一章", "下章", "后一章", "next"}
_PREV_CHAPTER = {"上一章", "上章", "前一章", "prev", "previous"}


def parse_counters(raw: str) -> tuple[int, ...]:
    """``"3-1-0-0-0-0"`` → 六元组；缺的补 0，非数字当 0（页面不会发这种，宽松处理不报错）。"""
    nums = [int(x) if x.isdigit() and len(x) <= 9 else 0 for x in raw.split("-")[:6]]
    return tuple(nums + [0] * (6 - len(nums)))


def new_session_id() -> str:
    """会话号：时间 + 随机，只含字母数字（地址路径里用，内核按完整 URL 缓存，每次打开都要新的）。"""
    return time.strftime("%m%d%H%M%S") + secrets.token_hex(3)


def normalize_title(text: str) -> str:
    """书名比较用：去掉书名号、引号、空白，英文小写。"""
    return re.sub(r"[《》<>〈〉「」『』“”\"'\s　]", "", text).lower()


@dataclass(frozen=True)
class Entry:
    """书库里的一本书（未解析）。"""

    id: str
    title: str
    path: Path
    #: 书库内相对路径（posix），书 id 由它算出
    name: str


@dataclass(frozen=True)
class View:
    """当前要显示的一页。"""

    book: Book
    font_size: int
    chapter: int
    page: int
    pages: ChapterPages
    offset: int


class Library:
    def __init__(self, books_dir: Path, state_file: Path, *, swap_swipe: bool = False):
        self.books_dir = books_dir.expanduser()
        self.state_file = state_file.expanduser()
        self.swap_swipe = swap_swipe
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._books: dict[str, Book] = {}
        self._sessions: OrderedDict[tuple[str, str], tuple[int, ...]] = OrderedDict()
        self.progress: dict[str, dict[str, Any]] = self._load_progress()
        self.scan()

    # ------------------------------------------------------------------ 书库

    def scan(self) -> list[Entry]:
        """重新扫描书库目录（很快：只列文件，不解析）。"""
        entries: dict[str, Entry] = {}
        if self.books_dir.is_dir():
            for path in sorted(self.books_dir.rglob("*.txt")):
                rel = path.relative_to(self.books_dir)
                if any(part.startswith(".") for part in rel.parts) or not path.is_file():
                    continue
                name = rel.as_posix()
                entry = Entry(book_id_for(name), path.stem, path, name)
                entries[entry.id] = entry
        with self._lock:
            self._entries = entries
            for book_id in list(self._books):
                if book_id not in entries:
                    del self._books[book_id]
        return list(entries.values())

    def entries(self) -> list[Entry]:
        with self._lock:
            return list(self._entries.values())

    def book(self, book_id: str) -> Book | None:
        """解析好的书；文件改过就重新解析。书库里没有（或文件没了）返回 None。"""
        with self._lock:
            entry = self._entries.get(book_id)
            if entry is None:
                return None
            try:
                mtime = entry.path.stat().st_mtime
            except OSError:
                return None
            cached = self._books.get(book_id)
            if cached is None or cached.mtime != mtime:
                cached = Book.load(entry.path, book_id=book_id)
                self._books[book_id] = cached
            return cached

    def find(self, query: str, *, rescan: bool = True) -> list[Entry]:
        """按书名找书，最匹配的在前：完全相同 > 开头相同 > 包含；同档按最近阅读排。空查询返回全部（最近读的在前）。

        一本都没找到时重新扫描一次书库再找（书是启动之后才放进去的）。
        """
        q = normalize_title(query)
        ranked: list[tuple[int, float, str, Entry]] = []
        for e in self.entries():
            t = normalize_title(e.title)
            if not q:
                rank = 0
            elif t == q:
                rank = 0
            elif t.startswith(q):
                rank = 1
            elif q in t:
                rank = 2
            else:
                continue
            ranked.append((rank, -self.last_read(e.id), e.title, e))
        if not ranked and rescan:
            self.scan()
            return self.find(query, rescan=False)
        return [r[3] for r in sorted(ranked, key=lambda r: r[:3])]

    def recent(self) -> Entry | None:
        """最近读过的书（书库里还在的）；一本都没读过就返回 None。"""
        read = (e for e in self.entries() if e.id in self.progress)
        return max(read, key=lambda e: self.last_read(e.id), default=None)

    def last_read(self, book_id: str) -> float:
        return float(self.progress.get(book_id, {}).get("updated_ts", 0.0))

    # ------------------------------------------------------------------ 位置

    def position(self, book: Book) -> tuple[int, int]:
        """(字符偏移, 字号)。没读过从头开始、默认字号。"""
        p = self.progress.get(book.id, {})
        size = p.get("font_size", DEFAULT_FONT_SIZE)
        return min(max(int(p.get("offset", 0)), 0), book.length), size if size in FONT_SIZES else DEFAULT_FONT_SIZE

    def view(self, book: Book, offset: int | None = None, font_size: int | None = None) -> View:
        """偏移所在的那一页（默认当前进度）。"""
        cur_offset, cur_size = self.position(book)
        offset = cur_offset if offset is None else offset
        size = cur_size if font_size is None else font_size
        ch = book.chapter_at(offset)
        cp = self.chapter_pages(book, ch, size)
        page = cp.page_at(offset)
        return View(book, size, ch, page, cp, cp.pages[page].offset)

    def chapter_pages(self, book: Book, chapter: int, font_size: int) -> ChapterPages:
        return _paginate(book, book.mtime, chapter, font_size)

    def move_pages(self, v: View, delta: int) -> View:
        """前后翻 delta 页，跨章节；到书头 / 书尾停住。空章节跳过。"""
        book, ch, page, cp = v.book, v.chapter, v.page, v.pages
        readable = book.readable()
        while delta > 0:
            if page + 1 < len(cp.pages):
                page += 1
            else:
                later = [i for i in readable if i > ch]
                if not later:
                    break
                ch, page = later[0], 0
                cp = self.chapter_pages(book, ch, v.font_size)
            delta -= 1
        while delta < 0:
            if page > 0:
                page -= 1
            else:
                earlier = [i for i in readable if i < ch]
                if not earlier:
                    break
                ch = earlier[-1]
                cp = self.chapter_pages(book, ch, v.font_size)
                page = len(cp.pages) - 1
            delta += 1
        return View(book, v.font_size, ch, page, cp, cp.pages[page].offset)

    def move_chapters(self, v: View, delta: int) -> View:
        """跳到前后第 delta 个有正文的章节的第一页；到头停住。"""
        readable = v.book.readable()
        here = max((i for i, c in enumerate(readable) if c <= v.chapter), default=0)
        target = readable[min(max(here + delta, 0), len(readable) - 1)]
        return self.view(v.book, v.book.chapters[target].start, v.font_size)

    def resize(self, v: View, steps: int) -> View:
        """字号在 :data:`FONT_SIZES` 里走 steps 档，位置不变。"""
        i = FONT_SIZES.index(v.font_size) if v.font_size in FONT_SIZES else FONT_SIZES.index(DEFAULT_FONT_SIZE)
        size = FONT_SIZES[min(max(i + steps, 0), len(FONT_SIZES) - 1)]
        return self.view(v.book, v.offset, size)

    def jump(
        self,
        book: Book,
        *,
        chapter: str | int | None = None,
        percent: float | None = None,
        pages: int | None = None,
    ) -> View | None:
        """语音跳转：章节（“第 3 章”“夜渡”“下一章”）/ 百分比 / 前后翻页数。章节找不到返回 None。"""
        v = self.view(book)
        if chapter is not None and str(chapter).strip():
            text = str(chapter).strip()
            if text in _NEXT_CHAPTER:
                v = self.move_chapters(v, 1)
            elif text in _PREV_CHAPTER:
                v = self.move_chapters(v, -1)
            else:
                index = book.find_chapter(text)
                if index is None:
                    return None
                v = self.view(book, book.chapters[index].start, v.font_size)
        elif percent is not None:
            pct = min(max(float(percent), 0.0), 100.0)
            v = self.view(book, int(book.length * pct / 100), v.font_size)
        if pages:
            v = self.move_pages(v, int(pages))
        self.save(v)
        return v

    def turn(self, book: Book, session: str, counters: tuple[int, ...]) -> tuple[View, str]:
        """页图请求：按计数差值移动位置，返回 (当前页, 日志说明)。"""
        with self._lock:
            key = (book.id, session)
            last = self._sessions.get(key)
            self._sessions[key] = tuple(map(max, counters, last)) if last else counters
            self._sessions.move_to_end(key)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
            v = self.view(book)
            if last is None:
                return v, "baseline"
            nxt, prv, nch, pch, big, small = (max(0, c - old) for c, old in zip(counters, last, strict=True))
            if self.swap_swipe:
                nxt, prv = prv, nxt
            if not (nxt or prv or nch or pch or big or small):
                return v, "repeat"
            if big or small:
                v = self.resize(v, big - small)
            if nch or pch:
                v = self.move_chapters(v, nch - pch)
            if nxt or prv:
                v = self.move_pages(v, nxt - prv)
            self.save(v)
            return v, f"+{nxt} -{prv} 章+{nch} 章-{pch} 字号{v.font_size}"

    # ------------------------------------------------------------------ 进度文件

    def save(self, v: View) -> None:
        book = v.book
        entry = self._entries.get(book.id)
        with self._lock:
            self.progress[book.id] = {
                "title": book.title,
                "file": entry.name if entry else book.path.name,
                "offset": v.offset,
                "font_size": v.font_size,
                "chapter": book.chapters[v.chapter].title,
                "chapter_index": v.chapter,
                "page": v.page + 1,
                "pages": len(v.pages.pages),
                "percent": book.percent(v.offset),
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_ts": time.time(),
            }
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(json.dumps(self.progress, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_file)

    def _load_progress(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("进度文件 %s 读不了（%s），从空进度开始；原文件保留不动", self.state_file, exc)
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 页图

    def page_image(self, v: View, ext: str = "png") -> bytes:
        return _page_image(v.book, v.book.mtime, v.font_size, v.chapter, v.page, ext)


@lru_cache(maxsize=256)
def _paginate(book: Book, mtime: float, chapter: int, font_size: int) -> ChapterPages:
    return paginate_chapter(book, chapter, layout_for(font_size))


@lru_cache(maxsize=256)
def _page_image(book: Book, mtime: float, font_size: int, chapter: int, page: int, ext: str) -> bytes:
    cp = _paginate(book, mtime, chapter, font_size)
    layout = layout_for(font_size)
    if ext == "png":
        return render_page(book, cp, page, layout)
    # JPEG 只是退路（research §7 P1）：文字页 JPEG 比 4 级灰度 PNG 大一倍多；从未量化的图压，硬边缘会更大
    png = render_page(book, cp, page, layout, levels=0)
    buf = io.BytesIO()
    Image.open(io.BytesIO(png)).save(buf, "JPEG", quality=40, optimize=True)
    return buf.getvalue()
