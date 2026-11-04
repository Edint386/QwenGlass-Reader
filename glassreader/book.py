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
#: 不规范但常见的写法：第、数字、章之间有空白，章字后面直接接标题，前面带“正文”，大写 CHAPTER，第 N 话。
#: CHAPTER_RE 认得的它都认得。正文里的句子也可能长这样（“第三章说的是”），用不用它看数量，见 :func:`_headings`
_LOOSE_CHAPTER_RE = re.compile(
    r"^[ \t　]*(?:正文[ \t　:：]*)?("
    r"第[ \t　]*[0-9０-９零〇一二两三四五六七八九十百千万]{1,9}[ \t　]*[章节回卷集部篇话]"
    r"(?:[ \t　:：].{0,30}|.{0,29}[^。！？…”’」』，、；：,.!?;:\s])?"
    r"|(?:序章|序言|序|楔子|引子|尾声|后记|番外)(?:[ \t　:：].{0,30})?"
    r"|(?i:chapter)[ \t]*\d{1,5}(?:[ \t:：.].{0,30})?"
    r")[ \t　]*$",
    re.M,
)
#: 一章最多这么多字，再长就按段落切开（:func:`_split`）
MAX_CHAPTER_CHARS = 50_000
#: 判断编码时看出错处前后各多少字节
_SNIFF_BYTES = 65536
_SPACES = " \t　﻿​‌‍"
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_NUMBER_IN_TITLE = re.compile(r"第 ?([0-9０-９零〇一二两三四五六七八九十百千万]{1,9}) ?[章节回话]")


def decode_text(raw: bytes) -> str:
    """按 BOM → UTF-8 → GB18030 的顺序解码；国内 TXT 小说大多是 GBK/GB18030。

    UTF-8 里个别坏字节（下载截断）不该把整本书变成乱码：只看出错处附近，坏字符占非 ASCII 字符的比例很低就还是 UTF-8。
    """
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if raw.startswith(bom):
            return raw[len(bom) :].decode(enc, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        window = raw[max(exc.start - _SNIFF_BYTES, 0) : exc.start + _SNIFF_BYTES].decode("utf-8", errors="replace")
        wide = len(window) - len(window.encode("ascii", "ignore"))
        if window.count("\ufffd") * 50 < wide:
            return raw.decode("utf-8", errors="replace")
        return raw.decode("gb18030", errors="replace")


def _headings(text: str) -> list[re.Match[str]]:
    """标题行。宽松写法比 CHAPTER_RE 多出一倍才算数，说明这本书的标题就是这么写的；零星一两行多半是正文，
    一个规范标题都没有时至少要 3 行。

    宽松写法只用来多分几章：只有它认的行留在正文坐标里（:meth:`Book.parse`），偏移一个字都不变。
    页图版（main）只认 CHAPTER_RE，标题不占偏移，多吃掉一行，两边同一份进度就落在不同的地方。
    """
    heads = list(CHAPTER_RE.finditer(text))
    loose = list(_LOOSE_CHAPTER_RE.finditer(text))
    return loose if len(loose) > max(2 * len(heads) + 1, 2) else heads


def parse_cn_number(text: str) -> int | None:
    """``"12"`` / ``"１２"`` / ``"十二"`` / ``"一百零三"`` / ``"两千"`` / ``"二三四五"`` → 整数；不是数字返回 None。"""
    text = text.strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if all(ch in _CN_DIGITS for ch in text):  # 没有十百千：一位一位读。语音识别会把 2222 写成“二二二二”，标题也有“第一〇五章”
        return int("".join(str(_CN_DIGITS[ch]) for ch in text))
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
    #: 听书时念的章节名，空的不念。切开的章节 title 带排版用的序号，这里没有
    spoken: str = ""
    #: 不带切分序号的标题，念给用户听的句子用这个
    base_title: str = ""

    def __post_init__(self) -> None:
        self.base_title = self.base_title or self.title

    @property
    def number(self) -> int | None:
        """标题里的“第 N 章”的 N（“第十二回 …” → 12）；没有就是 None。"""
        m = _NUMBER_IN_TITLE.search(self.title)
        return parse_cn_number(m.group(1)) if m else None

    @property
    def unit(self) -> str:
        """标题里“第 N 章”的“章”；没有就是空串。"""
        m = _NUMBER_IN_TITLE.search(self.title)
        return m.group(0)[-1] if m else ""


def _split(chapter: Chapter) -> list[Chapter]:
    """太长的章节按段落切开，标题加序号：排版和缓存按章算，没认出标题的书全书是一章。偏移不变。"""
    if chapter.end - chapter.start <= MAX_CHAPTER_CHARS:
        return [chapter]
    parts: list[Chapter] = []
    for offset, para in chapter.paragraphs:
        if not parts or offset - parts[-1].start >= MAX_CHAPTER_CHARS:
            parts.append(Chapter(chapter.title, offset, offset, spoken="" if parts else chapter.spoken))
        parts[-1].paragraphs.append((offset, para))
        parts[-1].end = offset + len(para)
    if len(parts) > 1:
        for i, part in enumerate(parts, 1):
            part.title = f"{chapter.title} {i}"
    return parts


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
        heads = _headings(text)
        spans: list[tuple[str, str, str]] = []  # (标题, 正文, 听书念的标题)
        if not heads:
            spans.append((title, text, title))
        else:
            if text[: heads[0].start()].strip(_SPACES + "\n"):
                spans.append(("前言", text[: heads[0].start()], "前言"))
            for i, m in enumerate(heads):
                end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
                name = re.sub(r"[ \t　]+", " ", m.group(1).strip())
                if CHAPTER_RE.fullmatch(text, m.start(), m.end()):
                    spans.append((name, text[m.end() : end], name))
                else:  # 这一行留在正文里，算偏移，听书时也已经读到它了，不用再念一遍标题
                    spans.append((name, text[m.start() : end], ""))

        chapters: list[Chapter] = []
        offset = 0
        for ch_title, body, spoken in spans:
            ch = Chapter(ch_title, offset, offset, spoken=spoken)
            for line in body.split("\n"):
                line = line.strip(_SPACES)
                if line:
                    ch.paragraphs.append((offset, line))
                    offset += len(line)
            ch.end = offset
            chapters.extend(_split(ch))
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
        m = re.fullmatch(r"第?\s*([0-9０-９零〇一二两三四五六七八九十百千万]{1,9})\s*[章节回话]?", query)
        if m and (n := parse_cn_number(m.group(1))) is not None:
            matches = [i for i, ch in enumerate(self.chapters) if ch.number == n]
            if matches:  # 宽松分章时“第3节课老师走进来”也有编号：先看量词，再看是不是规范标题
                return min(matches, key=lambda i: (self.chapters[i].unit != query[-1], not self.chapters[i].spoken))
            if any(ch.number is not None for ch in self.chapters):
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
