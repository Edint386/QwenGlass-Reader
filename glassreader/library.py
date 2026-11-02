"""书库、阅读位置和进度（docs/research.md §4.2、§4.5）。

- 书库是一个目录，递归收集 ``*.txt``（不分大小写）；书按需解析，文件改了（mtime 变了）下次用到时重新解析。
- 阅读位置 = 全书字符偏移 + 字号，存在一个 JSON 文件里（原子替换，旁边留一份 ``.bak``）。换字号重新分页后仍落在同一段文字。
  粗细、亮度、行距是全局设置，换一本书不用重新调，存在同一个文件的 ``_display`` 键下。
  智能翻页的速度只有文字版用，存在 ``_text`` 键下。两个实例保存设置时都只写改动的几项，在文件锁里和最新内容合并。
- 眼镜页面每个翻页操作只给一个累计计数器加一（a 下一页、b 上一页、j 下一章、k 上一章、z 自动翻页），
  页图地址里带着全部计数。这里记住每个会话上次看到的计数，按差值移动位置：
  内核重复请求同一地址时差值为 0；中间某个地址没发出来，下一个地址的计数已经包含了它。
  会话的第一次请求只记基线：页面经 on-new-intent 换了新会话时，计数器不会归零。
- 地址最后四段是显示设置保存后的值（字号、粗细、亮度、行距），0 = 这次打开还没保存过。这几个是绝对值不是计数：
  页面打开时由 dataSource 的 ``s`` 拿到当前值，在设置里调好后才写进地址。之后每个地址都带着它，页面重开也不清零，
  这期间文字版可能改过同一项，所以也按会话记住上次的值，某一段变了才算用户刚调的；会话的第一次请求只记基线。
  亮度段还有一条：退出亮度面板时没调也会写，所以从 0 变成页面打开时拿到的值不算改动。
"""

from __future__ import annotations

import fcntl
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from .book import Book, book_id_for
from .pager import (
    DEFAULT_FONT_SIZE,
    DEFAULT_SPACING,
    DEFAULT_WEIGHT,
    FONT_SIZES,
    LINE_SPACINGS,
    WEIGHTS,
    ChapterPages,
    layout_for,
    paginate_chapter,
    render_page,
    render_title,
)

__all__ = [
    "BRIGHTNESS_LEVELS",
    "COUNTER_NAMES",
    "DEFAULT_OPTICS",
    "DEFAULT_PACE",
    "OPTICS_LEVELS",
    "PACES",
    "SETTING_NAMES",
    "Display",
    "Entry",
    "Library",
    "View",
    "new_session_id",
    "normalize_title",
    "parse_counters",
]

log = logging.getLogger(__name__)

#: 页图地址的顺序（与 pages/reader/template.xml 的 src 一致）：先是累计计数，后是显示设置保存后的值
COUNTER_NAMES = ("next", "prev", "next_chapter", "prev_chapter", "auto")
SETTING_NAMES = ("font", "weight", "brightness", "spacing")
#: 亮度档（%），每档差 10，页面上直接加减
BRIGHTNESS_LEVELS = tuple(range(10, 101, 10))
#: 进度文件里放显示设置的键：不是书 id，书库里的书不会撞上
DISPLAY_KEY = "_display"
#: 放文字版自己的设置的键。页图版读进 Display 也用不上，保存设置时不写它
TEXT_KEY = "_text"
#: 智能翻页每秒多少字（文字版用），页面上每次加减 2
PACES = range(4, 61, 2)
DEFAULT_PACE = 18
#: 真机亮度（文字版用），平台的工具收 0～100。读不到眼镜的真实值，这里只记用户上次设的数
OPTICS_LEVELS = range(0, 101, 10)
DEFAULT_OPTICS = 50
#: 内存里留几本解析好的书：近 30 MB 的书每本占 30～50 MB，容器只有 256 MB
MAX_BOOKS = 2
#: 记住多少个会话的计数基线（每次打开页面一个会话）
MAX_SESSIONS = 512
#: 自动翻页和手动“下一页”隔这么久以内算同一次翻页：新图加载约 1 秒，这期间眼镜上还是旧页，用户看到的不是刚翻到的那页
TURN_MERGE_SECONDS = 1.5
_NEXT_CHAPTER = {"下一章", "下章", "后一章", "next"}
_PREV_CHAPTER = {"上一章", "上章", "前一章", "prev", "previous"}


def parse_counters(raw: str) -> tuple[int, ...]:
    """``"3-1-0-0-2-20-0-0-0"`` → 九元组，非数字当 0（页面不会发这种，宽松处理不报错）。

    段数不是九的是还开着的旧页面，顺序是 ``a-b-j-k-f-g-z…``：只取翻页用的几段，别把后面的计数当成设置值。
    """
    nums = [int(x) if x.isdigit() and len(x) <= 9 else 0 for x in raw.split("-")]
    size = len(COUNTER_NAMES) + len(SETTING_NAMES)
    if len(nums) == size:
        return tuple(nums)
    nums += [0] * (7 - len(nums))
    return (*nums[:4], nums[6], *[0] * len(SETTING_NAMES))


def _saved_number(value: Any, name: str) -> float:
    """进度文件里的数字。手改坏了按没读过算，和坏的显示设置一样不报错：一条坏记录不能挡住打开书。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number):
        log.warning("进度文件里的 %s 不是数字：%r，按 0 算", name, value)
        return 0.0
    return number


def _read_progress(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def new_session_id() -> str:
    """会话号：时间 + 随机，只含字母数字（地址路径里用，内核按完整 URL 缓存，每次打开都要新的）。"""
    return time.strftime("%m%d%H%M%S") + secrets.token_hex(3)


def _bare_title(text: str) -> str:
    """去掉书名号和空白，标点还在：《三体！》和《三体》是两本书，按前者的全名要能找到它。"""
    return re.sub(r"[《》<>〈〉「」『』\s　]", "", text)


def normalize_title(text: str) -> str:
    """书名比较用：去掉书名号、引号、空白、标点，英文小写。语音命令去掉了标点，这里不去的话带标点的书名对不上。"""
    return re.sub(r"[“”\"'，。！？、,.!?]", "", _bare_title(text)).lower()


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


@dataclass(frozen=True)
class Display:
    """全局显示设置：粗细档位（0 起）、亮度百分比、行距百分比、智能翻页每秒字数、真机亮度。"""

    weight: int = DEFAULT_WEIGHT
    brightness: int = 100
    spacing: int = DEFAULT_SPACING
    pace: int = DEFAULT_PACE
    optics: int = DEFAULT_OPTICS


class Library:
    def __init__(self, books_dir: Path, state_file: Path, *, swap_swipe: bool = False):
        self.books_dir = books_dir.expanduser()
        self.state_file = state_file.expanduser()
        self.backup_file = self.state_file.with_name(self.state_file.name + ".bak")
        self.swap_swipe = swap_swipe
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._books: OrderedDict[str, Book] = OrderedDict()
        #: 有书离开 _books 时调用。排版缓存的键是 Book 对象，不清的话旧书一直留在内存里；别的模块的缓存也注册到这里
        self.on_evict: list[Callable[[], None]] = [_paginate.cache_clear, _page_image.cache_clear]
        self._sessions: OrderedDict[tuple[str, str], tuple[int, ...]] = OrderedDict()
        self._last_turn: dict[tuple[str, str], tuple[float, bool]] = {}  # 会话 → (时间, 是否自动翻页)
        self._stamp: tuple[int, int] | None = None
        self._kept_bad: tuple[int, int] | None = None  # 已经抄过一份的坏文件：写不进去时它一直在，不能每次保存都抄
        self._progress: dict[str, dict[str, Any]] = {}
        self._display = Display()
        self.scan()

    # ------------------------------------------------------------------ 书库

    def scan(self) -> list[Entry]:
        """重新扫描书库目录（很快：只列文件，不解析）。"""
        entries: dict[str, Entry] = {}
        if self.books_dir.is_dir():
            for path in sorted(self.books_dir.rglob("*.txt", case_sensitive=False)):
                rel = path.relative_to(self.books_dir)
                if any(part.startswith(".") for part in rel.parts) or not path.is_file():
                    continue
                name = rel.as_posix()
                entry = Entry(book_id_for(name), path.stem, path, name)
                entries[entry.id] = entry
        with self._lock:
            self._entries = entries
            gone = [book_id for book_id in self._books if book_id not in entries]
            for book_id in gone:
                del self._books[book_id]
            if gone:
                self._evicted()
        return list(entries.values())

    def _evicted(self) -> None:
        for callback in self.on_evict:
            callback()

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
                try:
                    cached = Book.load(entry.path, book_id=book_id)
                except OSError as exc:
                    log.warning("读不了 %s：%s", entry.path, exc)
                    return None
                evict = book_id in self._books  # 文件改过，旧版本要放掉
                self._books[book_id] = cached
                if len(self._books) > MAX_BOOKS:
                    self._books.popitem(last=False)
                    evict = True
                if evict:
                    self._evicted()
            self._books.move_to_end(book_id)
            return cached

    def find(self, query: str, *, rescan: bool = True) -> list[Entry]:
        """按书名找书，最匹配的在前：完全相同 > 去掉标点后相同 > 开头相同 > 包含；同档按最近阅读排。
        空查询返回全部（最近读的在前）；只有标点不算空查询，除了书名就是这几个标点的书，什么也不匹配。

        一本都没找到时重新扫描一次书库再找（书是启动之后才放进去的）。
        """
        bare, q = _bare_title(query), normalize_title(query)
        ranked: list[tuple[int, float, str, Entry]] = []
        for e in self.entries():
            t = normalize_title(e.title)
            if not query.strip() or _bare_title(e.title) == bare:
                rank = 0
            elif not q:
                continue
            elif t == q:
                rank = 1
            elif t.startswith(q):
                rank = 2
            elif q in t:
                rank = 3
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
        return _saved_number(self.progress.get(book_id, {}).get("updated_ts", 0.0), "updated_ts")

    def last_percent(self, book_id: str) -> int:
        return int(_saved_number(self.progress.get(book_id, {}).get("percent", 0), "percent"))

    # ------------------------------------------------------------------ 位置

    def position(self, book: Book) -> tuple[int, int]:
        """(字符偏移, 字号)。没读过从头开始、默认字号。"""
        p = self.progress.get(book.id, {})
        size = p.get("font_size", DEFAULT_FONT_SIZE)
        offset = int(_saved_number(p.get("offset", 0), "offset"))
        return min(max(offset, 0), book.length), size if size in FONT_SIZES else DEFAULT_FONT_SIZE

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
        return _paginate(book, book.mtime, chapter, font_size, self.display.spacing)

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
        """跳到前后第 delta 个有正文的章节的第一页；到头停住。已经是最后一章时往后不动，不退回本章开头。"""
        readable = v.book.readable()
        here = max((i for i, c in enumerate(readable) if c <= v.chapter), default=0)
        if delta > 0 and here == len(readable) - 1:
            return v
        if delta < 0 and v.chapter not in readable:  # 停在尾部空章：往回第一步落在最后一个有正文的章，不是它的前一章
            here += 1
        target = readable[min(max(here + delta, 0), len(readable) - 1)]
        return self.view(v.book, v.book.chapters[target].start, v.font_size)

    def resize(self, v: View, steps: int) -> View:
        """字号在 :data:`FONT_SIZES` 里走 steps 档，位置不变。"""
        i = FONT_SIZES.index(v.font_size) if v.font_size in FONT_SIZES else FONT_SIZES.index(DEFAULT_FONT_SIZE)
        size = FONT_SIZES[min(max(i + steps, 0), len(FONT_SIZES) - 1)]
        return self.view(v.book, v.offset, size)

    def set_display(
        self,
        *,
        weight: int | None = None,
        brightness: int | None = None,
        spacing: int | None = None,
        pace: int | None = None,
        optics: int | None = None,
    ) -> None:
        """只存给出的几项，别的留给文件里最新的值：整组写回会盖掉另一个实例刚存的设置。"""
        shown = {"weight": weight, "brightness": brightness, "spacing": spacing}
        text = {"pace": pace, "optics": optics}
        changes = {key: {k: v for k, v in part.items() if v is not None} for key, part in ((DISPLAY_KEY, shown), (TEXT_KEY, text))}
        self._store({key: value for key, value in changes.items() if value})

    def page_settings(self, book: Book) -> dict[str, int]:
        """页面 dataSource 的 ``s``：当前显示设置，设置面板要显示这些值。o 是遮罩透明度，亮度每档差 25。"""
        d = self.display
        return {
            "f": self.position(book)[1],
            "w": d.weight + 1,
            "b": d.brightness,
            "l": d.spacing,
            "o": (100 - d.brightness) * 5 // 2,
        }

    def _apply_saved(self, book: Book, v: View, saved: tuple[int, ...]) -> tuple[View, bool]:
        """把地址里保存的设置值用上。0 和不认识的值当没改；同一个值重复来不算改动。"""
        font, weight, brightness, spacing = saved
        d = self.display
        changes: dict[str, int] = {}
        if 1 <= weight <= len(WEIGHTS) and weight - 1 != d.weight:
            changes["weight"] = weight - 1
        if brightness in BRIGHTNESS_LEVELS and brightness != d.brightness:
            changes["brightness"] = brightness
        if spacing in LINE_SPACINGS and spacing != d.spacing:
            changes["spacing"] = spacing
        size = font if font in FONT_SIZES else v.font_size
        if not changes and size == v.font_size:
            return v, False
        if changes:
            self.set_display(**changes)
        v = self.view(book, v.offset, size)
        self.save(v)
        return v, True

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
        size = len(COUNTER_NAMES)
        counters = (*counters, *[0] * (size + len(SETTING_NAMES) - len(counters)))
        counters, saved = counters[:size], counters[size:]
        with self._lock:
            key = (book.id, session)
            last = self._sessions.get(key)
            opened = last[-1] if last else self.display.brightness  # 第一次请求紧跟在打开之后，这就是页面拿到的 s.b
            self._sessions[key] = (*(map(max, counters, last) if last else counters), *saved, opened)
            self._sessions.move_to_end(key)
            while len(self._sessions) > MAX_SESSIONS:
                self._last_turn.pop(self._sessions.popitem(last=False)[0], None)
            if last is None:
                return self.view(book), "baseline"
            font, weight, brightness, spacing = (new if new != old else 0 for new, old in zip(saved, last[size:-1], strict=True))
            if brightness == opened and not last[size + SETTING_NAMES.index("brightness")]:
                brightness = 0  # 亮度面板只能单击退出，退出就写地址，没调也写：页面拿到的旧值不能盖掉文字版后来改的
            v, adjusted = self._apply_saved(book, self.view(book), (font, weight, brightness, spacing))
            nxt, prv, nch, pch, auto = (max(0, c - old) for c, old in zip(counters, last[:size], strict=True))
            if self.swap_swipe:
                nxt, prv = prv, nxt
            manual = bool(nxt or prv or nch or pch or adjusted)
            if not (manual or auto):
                return v, "repeat"
            now = time.monotonic()
            last_at, last_auto = self._last_turn.get(key, (float("-inf"), False))
            recent = now - last_at < TURN_MERGE_SECONDS
            if not manual:
                if recent and not last_auto:
                    return v, "merged"
                nxt = 1  # 断线时积压的几次也只翻一页
            elif recent and last_auto and nxt:
                nxt -= 1
            self._last_turn[key] = (now, not manual)
            if nch or pch:
                v = self.move_chapters(v, nch - pch)
            if nxt or prv:
                v = self.move_pages(v, nxt - prv)
            self.save(v)
            return v, f"+{nxt} -{prv} 章+{nch} 章-{pch} 字号{v.font_size}"

    # ------------------------------------------------------------------ 进度文件
    #
    # 页图版和文字版是两个进程，共用一个进度文件。读的时候发现文件换过就重新读；写的时候加文件锁，
    # 先读最新内容，只改自己这一项，谁也不会盖掉对方刚写的东西。

    @property
    def progress(self) -> dict[str, dict[str, Any]]:
        self._refresh()
        return self._progress

    @property
    def display(self) -> Display:
        self._refresh()
        return self._display

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.state_file)
        except OSError:
            return None
        return st.st_ino, st.st_mtime_ns

    def _refresh(self) -> None:
        with self._lock:
            stamp = self._file_stamp()
            if stamp == self._stamp:
                return
            try:
                self._progress = self._load_progress()
            except OSError as exc:  # 留着内存里这份，stamp 不动，下次访问再读
                log.warning("进度文件 %s 读不了：%s", self.state_file, exc)
                return
            self._stamp = stamp
            self._display = self._load_display()

    @contextmanager
    def _updating(self) -> Iterator[dict[str, dict[str, Any]]]:
        """拿到文件里最新的进度，改完写回。文件锁在关闭文件时释放。"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.state_file.with_name(self.state_file.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._progress = self._load_progress(quarantine=True)
            yield self._progress
            text = json.dumps(self._progress, ensure_ascii=False, indent=2)
            _write_atomic(self.state_file, text)
            self._stamp = self._file_stamp()
            self._display = self._load_display()
            _write_atomic(self.backup_file, text)  # 不是硬链接：外面把文件改坏时备份不能跟着坏

    def _store(self, changes: dict[str, dict[str, Any]]) -> None:
        """改进度文件里的几项，这几项里不认识的键留着：另一个实例可能比这边新。

        写不进去（磁盘满、没权限）只记日志，这个进程里照样用新值：翻页不能因此断掉。
        """
        def merge(progress: dict[str, dict[str, Any]]) -> None:
            for key, value in changes.items():
                progress[key] = {**progress.get(key, {}), **value}

        try:
            with self._updating() as progress:
                merge(progress)
        except OSError as exc:
            log.error("进度写不进 %s：%s", self.state_file, exc)
            with self._lock:
                merge(self._progress)
                self._display = self._load_display()

    def save(self, v: View) -> float:
        """存这本书的位置，返回写进文件的时间。写完再读文件，读到的可能已经是另一个实例的了。"""
        book = v.book
        entry = self._entries.get(book.id)
        now = time.time()
        self._store({book.id: {
            "title": book.title,
            "file": entry.name if entry else book.path.name,
            "offset": v.offset,
            "font_size": v.font_size,
            "chapter": book.chapters[v.chapter].base_title,
            "chapter_index": v.chapter,
            "page": v.page + 1,
            "pages": len(v.pages.pages),
            "percent": book.percent(v.offset),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_ts": now,
        }})
        return now

    def _load_progress(self, *, quarantine: bool = False) -> dict[str, dict[str, Any]]:
        """读进度文件。文件坏了就用 :meth:`_recovered`，要写回时（quarantine）先把坏文件抄一份，别的书的进度还能手工救回来。

        只抄不挪，等新文件原子替换它：挪走之后到写好之前没有文件，另一个实例这时读到的是“从头开始”，
        翻一页就把位置写回书头；写入方死在这时，下次启动也用不上备份。

        OSError 往上抛：不知道文件里是什么，不能写回去。
        """
        try:
            return _read_progress(self.state_file)
        except FileNotFoundError:  # 一直没有文件时，内存里的是还没写进去的进度
            return self._progress if self._stamp is None else {}
        except ValueError as exc:
            bad = self._file_stamp()
            if quarantine and bad != self._kept_bad:
                stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"  # 同一秒坏两次也不能互相盖掉
                kept = self.state_file.with_name(f"{self.state_file.name}.corrupt-{stamp}")
                shutil.copyfile(self.state_file, kept)
                self._kept_bad = bad
                log.error("进度文件坏了（%s），留了一份在 %s，用备份和内存里的进度接着写", exc, kept)
            else:
                log.warning("进度文件 %s 坏了（%s），先用备份和内存里的进度，下次保存时替换", self.state_file, exc)
            return self._recovered()

    def _recovered(self) -> dict[str, dict[str, Any]]:
        """文件坏了时最新的进度。内存可能是旧的（另一个实例写过，这边没读过）或空的（刚重启），备份是上一次写入的全部内容；
        内存也可能更新（备份写失败了）。书取 updated_ts 新的那份，显示设置没有时间，取备份。
        """
        try:
            backup = _read_progress(self.backup_file)
        except (OSError, ValueError):
            return self._progress

        def written_at(record: dict[str, Any]) -> float:
            return _saved_number(record.get("updated_ts", 0.0), "updated_ts")

        merged = dict(self._progress)
        for key, record in backup.items():
            if written_at(record) >= written_at(merged.get(key, {})):
                merged[key] = record
        return merged

    def _load_display(self) -> Display:
        """进度文件里的显示设置；不认识的值（手改过、档位变了）退回默认。"""
        p = self._progress.get(DISPLAY_KEY, {})
        weight, brightness, spacing = p.get("weight"), p.get("brightness"), p.get("spacing")
        text = self._progress.get(TEXT_KEY, {})
        pace, optics = text.get("pace", p.get("pace")), text.get("optics")  # 速度以前存在 _display 里
        if isinstance(brightness, int):  # 亮度以前的档位不是整十
            brightness = min(max(round(brightness / 10) * 10, 10), 100)
        return Display(
            weight if weight in range(len(WEIGHTS)) else DEFAULT_WEIGHT,
            brightness if brightness in BRIGHTNESS_LEVELS else 100,
            spacing if spacing in LINE_SPACINGS else DEFAULT_SPACING,
            pace if pace in PACES else DEFAULT_PACE,
            optics if optics in OPTICS_LEVELS else DEFAULT_OPTICS,
        )

    # ------------------------------------------------------------------ 页图

    def page_image(self, v: View, ext: str = "png") -> bytes:
        return _page_image(v.book, v.book.mtime, v.font_size, v.chapter, v.page, ext, self.display)

    def title_image(self, book: Book) -> bytes:
        return _title_image(book.title)


@lru_cache(maxsize=256)
def _paginate(book: Book, mtime: float, chapter: int, font_size: int, spacing: int) -> ChapterPages:
    return paginate_chapter(book, chapter, layout_for(font_size, spacing))


@lru_cache(maxsize=16)
def _title_image(title: str) -> bytes:
    return render_title(title)


@lru_cache(maxsize=256)
def _page_image(book: Book, mtime: float, font_size: int, chapter: int, page: int, ext: str, display: Display) -> bytes:
    cp = _paginate(book, mtime, chapter, font_size, display.spacing)
    layout = layout_for(font_size, display.spacing)
    if ext == "png":
        return render_page(book, cp, page, layout, weight=display.weight)
    # JPEG 只是退路（research §7 P1）：文字页 JPEG 比 4 级灰度 PNG 大一倍多；从未量化的图压，硬边缘会更大
    png = render_page(book, cp, page, layout, levels=0, weight=display.weight)
    buf = io.BytesIO()
    Image.open(io.BytesIO(png)).save(buf, "JPEG", quality=40, optimize=True)
    return buf.getvalue()
