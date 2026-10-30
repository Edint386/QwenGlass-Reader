"""读 TXT、切章节（docs/research.md §5）。

全书正文按段落拼成一条“字符偏移”坐标：阅读进度就是一个偏移量，与字号、版式无关。
"""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["CHAPTER_RE", "Book", "Chapter", "book_id_for", "decode_text", "parse_cn_number"]

#: 独占一行的章节标题：「第…章/节/回/卷/集/部/篇 标题」「序章 / 楔子 / 尾声 / 后记 / 番外…」「Chapter 12」
CHAPTER_RE = re.compile(
    r"^[ \t　]*("
    r"第[0-9０-９零〇一二两三四五六七八九十百千万]{1,9}[章节回卷集部篇](?:[ \t　:：].{0,30})?"
    r"|(?:序章|序言|序|楔子|引子|尾声|后记|番外)(?:[ \t　:：].{0,30})?"
    r"|[Cc]hapter[ \t]*\d{1,5}(?:[ \t:：.].{0,30})?"
    r")[ \t　]*$",
    re.M,
)
_SPACES = " \t　﻿​‌‍"
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_NUMBER_IN_TITLE = re.compile(r"第([0-9０-９零〇一二两三四五六七八九十百千万]{1,9})[章节回]")


def decode_text(raw: bytes) -> str:
    """按 BOM → UTF-8 → GB18030 的顺序解码；国内 TXT 小说大多是 GBK/GB18030。"""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if raw.startswith(bom):
            return raw[len(bom) :].decode(enc, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def parse_cn_number(text: str) -> int | None:
    """``"12"`` / ``"１２"`` / ``"十二"`` / ``"一百零三"`` / ``"两千"`` → 整数；不是数字返回 None。"""
    text = text.strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, section, digit = 0, 0, None
    for ch in text:
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                total += (section + (digit or 0)) * unit
                section = 0
            else:
                section += (1 if digit is None else digit) * unit  # “十二”的“十”前面没有数字
            digit = None
        else:
            return None
    return total + section + (digit or 0)


def book_id_for(name: str) -> str:
    """书 id：按书库里的相对路径取哈希前 10 位。改名会被当成新书（进度不跟随）。"""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


@dataclass
class Chapter:
    title: str
    #: 本章在全书偏移坐标里的起止（左闭右开）；只有标题没有正文的章节 start == end
    start: int
    end: int
    #: (偏移, 段落文字)
    paragraphs: list[tuple[int, str]] = field(default_factory=list)

    @property
    def number(self) -> int | None:
        """标题里的“第 N 章”的 N（“第十二回 …” → 12）；没有就是 None。"""
        m = _NUMBER_IN_TITLE.search(self.title)
        return parse_cn_number(m.group(1)) if m else None


@dataclass(eq=False)  # 按对象身份哈希，可作缓存键
class Book:
    id: str
    title: str
    path: Path
    chapters: list[Chapter]
    #: 正文总字数（各段落字数之和），百分比的分母
    length: int
    mtime: float = 0.0

    @classmethod
    def load(cls, path: Path, *, book_id: str | None = None) -> Book:
        stat = path.stat()
        return cls.parse(decode_text(path.read_bytes()), title=path.stem, path=path,
                         book_id=book_id or book_id_for(path.name), mtime=stat.st_mtime)

    @classmethod
    def parse(cls, text: str, *, title: str, path: Path = Path(), book_id: str = "", mtime: float = 0.0) -> Book:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        heads = list(CHAPTER_RE.finditer(text))
        spans: list[tuple[str, str]] = []
        if not heads:
            spans.append((title, text))
        else:
            if text[: heads[0].start()].strip(_SPACES + "\n"):
                spans.append(("前言", text[: heads[0].start()]))
            for i, m in enumerate(heads):
                end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
                spans.append((re.sub(r"[ \t　]+", " ", m.group(1).strip()), text[m.end() : end]))

        chapters: list[Chapter] = []
        offset = 0
        for ch_title, body in spans:
            ch = Chapter(ch_title, offset, offset)
            for line in body.split("\n"):
                line = line.strip(_SPACES)
                if line:
                    ch.paragraphs.append((offset, line))
                    offset += len(line)
            ch.end = offset
            chapters.append(ch)
        return cls(book_id or book_id_for(path.name or title), title, path, chapters, max(offset, 1), mtime)

    def chapter_at(self, offset: int) -> int:
        """偏移所在的章节序号（0 起）。空章节（只有标题）和下一章同一个起点，这时取下一章：正文属于它。"""
        starts = [c.start for c in self.chapters]
        i = bisect.bisect_right(starts, max(offset, 0)) - 1
        return min(max(i, 0), len(self.chapters) - 1)

    def readable(self) -> list[int]:
        """有正文的章节序号。按章跳转只在这些章节之间走，空章节（只有标题）跳过。"""
        return [i for i, ch in enumerate(self.chapters) if ch.end > ch.start] or [0]

    def percent(self, offset: int) -> int:
        return min(100, max(0, offset) * 100 // self.length)

    def find_chapter(self, query: str) -> int | None:
        """按“第 N 章”的 N 或标题片段找章节；找不到返回 None。"""
        query = query.strip()
        if not query:
            return None
        m = re.fullmatch(r"第?([0-9０-９零〇一二两三四五六七八九十百千万]{1,9})[章节回]?", query)
        if m and (n := parse_cn_number(m.group(1))) is not None:
            numbers = [ch.number for ch in self.chapters]
            if n in numbers:
                return numbers.index(n)
            if any(x is not None for x in numbers):
                return None  # 有编号的书里没有这一章，不要按序号乱猜
            # 标题里都没有编号的书：按“第 N 章”= 第 N 个章节理解（跳过自动加的「前言」）
            plain = [i for i, ch in enumerate(self.chapters) if ch.title != "前言"]
            return plain[n - 1] if 1 <= n <= len(plain) else None
        for i, ch in enumerate(self.chapters):
            if query == ch.title:
                return i
        for i, ch in enumerate(self.chapters):
            if query in ch.title:
                return i
        return None
