"""按眼镜画布分页、把一页渲染成图片（docs/research.md §5）。

- 分页和渲染用同一份字体度量，电脑端算出的页与眼镜上看到的完全一致。
- 按章分页、按需分页：几 MB 的长篇一次全书分页要好几秒，平台工具只给 3 秒（research §1 约束 4）。
  页脚的页码是“本章第几页”，全书位置用百分比表示。
- 眼镜是单色绿光机：黑 = 不发光（research §1 约束 12），所以黑底亮字。
"""

from __future__ import annotations

import bisect
import io
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .book import Book

__all__ = [
    "FONT_SIZES",
    "HEITI",
    "NO_LINE_END",
    "NO_LINE_START",
    "SONGTI",
    "ChapterPages",
    "Layout",
    "Line",
    "Page",
    "layout_for",
    "paginate_chapter",
    "render_page",
    "wrap",
]

# 行首禁则（不能出现在行首的标点，挤进上一行“悬挂”）与行尾禁则（不能留在行尾的开标点）
NO_LINE_START = frozenset("，。、；：？！）》」』】〉…—～·,.;:?!)]}%”’")
NO_LINE_END = frozenset("（《「『【〈([{“‘")
SONGTI = ("/System/Library/Fonts/Supplemental/Songti.ttc", 6)  # Songti SC Regular
HEITI = ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0)  # Hiragino Sans GB W3
#: 可选字号（菜单“字号放大 / 缩小”在这几档之间走）
FONT_SIZES = (20, 22, 24, 28, 32)
DEFAULT_FONT_SIZE = 24

# 正文灰度 / 辅助信息 / 线条
INK, DIM, FAINT = 235, 120, 60


@dataclass(frozen=True)
class Layout:
    """正文页图的版式。默认 478×290：页面两侧各留 80 px 原生侧边栏 + 1 px 分隔线（pages/reader）。"""

    width: int = 478
    height: int = 290
    font: tuple[str, int] = HEITI  # 黑体笔画粗细均匀，在绿色光机上比宋体的细横更稳
    font_size: int = DEFAULT_FONT_SIZE
    line_height: int = 34
    pad_x: int = 14
    header_h: int = 30
    footer_h: int = 22
    indent: int = 2  # 段首缩进（全角字数）

    @property
    def body_lines(self) -> int:
        return max(1, (self.height - self.header_h - self.footer_h) // self.line_height)

    @property
    def text_width(self) -> int:
        return self.width - 2 * self.pad_x


def layout_for(font_size: int) -> Layout:
    return Layout(font_size=font_size, line_height=round(font_size * 1.42))


@lru_cache(maxsize=32)
def _font(spec: tuple[str, int], size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(spec[0], size, index=spec[1])
    except OSError:  # 不是 macOS（没有这两套系统字体）：退回 Pillow 内置字体，能跑但没有中文字形
        return ImageFont.load_default(size)


class _Widths(dict[str, float]):
    """逐字宽度缓存：FreeType 量一次字约几微秒，长篇分页要量几十万次。"""

    def __init__(self, font: ImageFont.FreeTypeFont):
        super().__init__()
        self.font = font

    def __missing__(self, ch: str) -> float:
        w = self[ch] = self.font.getlength(ch)
        return w


@lru_cache(maxsize=32)
def _widths(spec: tuple[str, int], size: int) -> _Widths:
    return _Widths(_font(spec, size))


@dataclass
class Line:
    text: str
    offset: int
    indent: bool = False  # 段首行（缩进）
    last: bool = False  # 段末行（不做两端对齐）


@dataclass
class Page:
    lines: list[Line] = field(default_factory=list)
    #: 本页第一个字的偏移；空章节的唯一一页取章节起点
    offset: int = 0


@dataclass
class ChapterPages:
    chapter: int
    pages: list[Page]

    def __post_init__(self) -> None:
        self._starts = [p.offset for p in self.pages]

    def page_at(self, offset: int) -> int:
        return max(0, bisect.bisect_right(self._starts, offset) - 1)


def wrap(text: str, offset: int, width: float, widths: dict[str, float], indent_px: float) -> list[Line]:
    """贪心断行 + 禁则：行首禁则字符悬挂到上一行行尾（最多两个连续标点，如“。”），行尾禁则字符推到下一行。"""
    lines: list[Line] = []
    start, cur, avail = 0, 0.0, width - indent_px
    i, n = 0, len(text)
    while i < n:
        w = widths[text[i]]
        if cur + w > avail and i > start:
            cut = i
            if text[i] in NO_LINE_START:
                cut = i + 1
                if cut < n and text[cut] in NO_LINE_START:
                    cut += 1
            elif text[cut - 1] in NO_LINE_END and cut - 1 > start:
                cut -= 1
            # 悬挂标点可能正好吃掉段尾：这一行就是段末行，不能两端对齐
            lines.append(Line(text[start:cut], offset + start, indent=not lines and indent_px > 0, last=cut >= n))
            start, i, cur, avail = cut, cut, 0.0, width
            continue
        cur += w
        i += 1
    if start < n:
        lines.append(Line(text[start:], offset + start, indent=not lines and indent_px > 0, last=True))
    return lines


def paginate_chapter(book: Book, chapter: int, layout: Layout) -> ChapterPages:
    widths = _widths(layout.font, layout.font_size)
    indent_px = widths["　"] * layout.indent
    pages: list[Page] = []
    page = Page()
    for offset, para in book.chapters[chapter].paragraphs:
        for line in wrap(para, offset, layout.text_width, widths, indent_px):
            if len(page.lines) == layout.body_lines:
                pages.append(page)
                page = Page()
            if not page.lines:
                page.offset = line.offset
            page.lines.append(line)
    if page.lines or not pages:
        if not page.lines:
            page.offset = book.chapters[chapter].start
        pages.append(page)
    return ChapterPages(chapter, pages)


def render_page(book: Book, chapter: ChapterPages, index: int, layout: Layout, *, levels: int = 4) -> bytes:
    """把本章第 index 页渲染成灰度 PNG。levels > 0 时量化到 levels 级灰度（体积约为不量化的 1/3，字边仍平滑）。"""
    page = chapter.pages[index]
    img = Image.new("L", (layout.width, layout.height), 0)
    d = ImageDraw.Draw(img)
    body = _font(layout.font, layout.font_size)
    widths = _widths(layout.font, layout.font_size)
    small = _font(HEITI, 16)
    pad, right = layout.pad_x, layout.width - layout.pad_x

    # 页眉：章节名（左，过长截断）+ 全书百分比（右）
    pct = f"{book.percent(page.offset)}%"
    title = book.chapters[chapter.chapter].title
    room = right - pad - small.getlength(pct) - 16
    while title and small.getlength(title) > room:
        title = title[:-2] + "…"
    d.text((pad, 8), title, font=small, fill=DIM)
    d.text((right, 8), pct, font=small, fill=DIM, anchor="ra")
    d.line((pad, layout.header_h - 6, right, layout.header_h - 6), fill=FAINT, width=1)

    # 正文：非段末行逐字画、字间均分剩余宽度（两端对齐）；悬挂标点允许伸进右边距
    indent_px = widths["　"] * layout.indent
    y = layout.header_h + (layout.line_height - layout.font_size) // 2 - 2
    for line in page.lines:
        x = pad + (indent_px if line.indent else 0)
        ws = [widths[c] for c in line.text]
        spare = right - x - sum(ws)
        gap = spare / (len(ws) - 1) if not line.last and len(ws) > 1 and spare > 0 else 0.0
        for c, w in zip(line.text, ws, strict=True):
            d.text((x, y), c, font=body, fill=INK)
            x += w + gap
        y += layout.line_height

    # 页脚：全书进度条 + 本章页码
    by = layout.height - layout.footer_h + 10
    x1 = right - 70
    d.line((pad, by, x1, by), fill=FAINT, width=2)
    done = (x1 - pad) * book.percent(page.offset) // 100
    if done:
        d.line((pad, by, pad + done, by), fill=DIM + 40, width=2)
    d.text((right, by), f"{index + 1} / {len(chapter.pages)}", font=small, fill=DIM, anchor="rm")

    if levels:
        step = 255 / (levels - 1)
        img = img.point(lambda v: int(round(v / step) * step)).quantize(colors=levels, dither=Image.Dither.NONE)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
