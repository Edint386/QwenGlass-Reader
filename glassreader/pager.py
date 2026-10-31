"""按眼镜画布分页、把一页渲染成图片（docs/research.md §5）。

- 分页和渲染用同一份字体度量，电脑端算出的页与眼镜上看到的完全一致。
- 按章分页、按需分页：几 MB 的长篇一次全书分页要好几秒，平台工具只给 3 秒（research §1 约束 4）。
  页脚的页码是“本章第几页”，全书位置用百分比表示。
- 眼镜是单色绿光机：黑 = 不发光（research §1 约束 12），所以黑底亮字。
"""

from __future__ import annotations

import bisect
import io
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .book import Book

__all__ = [
    "DEFAULT_SPACING",
    "DEFAULT_WEIGHT",
    "FALLBACK_FONTS",
    "FONT_SIZES",
    "HEITI",
    "LINE_SPACINGS",
    "NOTO_SANS",
    "NO_LINE_END",
    "NO_LINE_START",
    "SONGTI",
    "TITLE_SIZE",
    "WEIGHTS",
    "ChapterPages",
    "Layout",
    "Line",
    "Page",
    "layout_for",
    "paginate_chapter",
    "render_page",
    "render_title",
    "wrap",
]

# 行首禁则（不能出现在行首的标点，挤进上一行“悬挂”）与行尾禁则（不能留在行尾的开标点）
NO_LINE_START = frozenset("，。、；：？！）》」』】〉…—～·,.;:?!)]}%”’")
NO_LINE_END = frozenset("（《「『【〈([{“‘")
SONGTI = ("/System/Library/Fonts/Supplemental/Songti.ttc", 6)  # Songti SC Regular
HEITI = ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0)  # Hiragino Sans GB W3
NOTO_SANS = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2)  # Noto Sans CJK SC，Linux 的 fonts-noto-cjk
#: 主字体缺字时依次找的字体（不存在的跳过）。网文颜文字爱用泰文、彝文、坎纳达文和组合符号，中文字体都没有；
#: 前面的笔画和正文协调，Unifont 覆盖整个 BMP 兜底，但它是点阵风格，只该补别的字体都没有的字。最后一个给 Mac。
FALLBACK_FONTS = (
    *(f"/usr/share/fonts/truetype/noto/Noto{name}-Regular.ttf" for name in (
        "Sans", "SansThai", "SansYi", "SerifTibetan", "SansOriya", "SansKannada", "SansTelugu", "SansBengali",
        "SansGujarati", "SansGurmukhi", "SansDevanagari", "SansSinhala", "SansMalayalam", "SansCanadianAboriginal",
        "SansMath", "SansSymbols", "SansSymbols2",
    )),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/opentype/unifont/unifont.otf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)
#: 肯定没有字形的码位，画出来就是字体的缺字方块
NOT_A_CHAR = "\U0010fffd"
#: 不占位也不该显示的控制字符（零宽空格、方向标记、变体选择符…）。Unifont 给它们画了带字母的方框，不能交给字体。
INVISIBLE = frozenset(map(chr, (*range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x2070),
                                *range(0xFE00, 0xFE10), 0xFEFF)))
#: 可选字号，每档差 2，页面上直接加减。16 是下限：再小，笔画多的字就糊成一团
FONT_SIZES = tuple(range(16, 33, 2))
DEFAULT_FONT_SIZE = 24

# 正文灰度 / 辅助信息 / 线条
INK, DIM, FAINT = 235, 120, 60
#: 正文粗细档：(向外描粗的 px, 字边半透明像素的增益)。增益把字边往亮里归，量化后满亮像素从四成到六成。
#: 默认档之后 16、18 号的密字开始糊，是否接受由用户定。页眉页脚不受影响，它们本来就该暗
WEIGHTS = ((0.0, 1.0), (0.04, 1.15), (0.08, 1.3), (0.14, 1.35), (0.22, 1.4), (0.32, 1.4))
DEFAULT_WEIGHT = 2
#: 行距档（行高是字号的百分之几），每档差 15，页面上直接加减
LINE_SPACINGS = (125, 140, 155, 170, 185)
DEFAULT_SPACING = 140
#: 左栏书名图的大小，和 pages/reader 里的 image 一致
TITLE_SIZE = (72, 176)


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


def layout_for(font_size: int, spacing: int = DEFAULT_SPACING) -> Layout:
    return Layout(font_size=font_size, line_height=round(font_size * spacing / 100))


@lru_cache(maxsize=32)
def _font(spec: tuple[str, int], size: int) -> ImageFont.FreeTypeFont:
    for path, index in (spec, NOTO_SANS):  # 云端容器里没有 macOS 系统字体
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default(size)  # 都没有：退回 Pillow 内置字体，能跑但没有中文字形


@lru_cache(maxsize=32)
def _fallback_fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ...]:
    fonts = []
    for path in FALLBACK_FONTS:
        try:
            fonts.append(ImageFont.truetype(path, size))
        except OSError:
            continue
    return tuple(fonts)


def _bitmap(font: ImageFont.FreeTypeFont, ch: str) -> bytes:
    img = Image.new("L", (96, 96))
    ImageDraw.Draw(img).text((24, 64), ch, font=font, fill=255, anchor="ls")
    return img.tobytes()


@lru_cache(maxsize=128)
def _notdef(font: ImageFont.FreeTypeFont) -> bytes:
    return _bitmap(font, NOT_A_CHAR)


def _covering_font(font: ImageFont.FreeTypeFont, ch: str) -> ImageFont.FreeTypeFont:
    """能画出 ch 的字体：主字体优先，再按 FALLBACK_FONTS 找；都没有就用主字体。

    FreeType 经 Pillow 查不了“有没有这个字”，所以比位图：和缺字方块一样就是没有。
    """
    for candidate in (font, *_fallback_fonts(font.size)):
        if _bitmap(candidate, ch) != _notdef(candidate):
            return candidate
    return font


def _is_mark(ch: str) -> bool:
    """组合符号（含泰文元音）：叠在前一个字上，不占宽度。"""
    return unicodedata.category(ch) in ("Mn", "Me")


def _baseline(size: int) -> int:
    """文字框顶到基线的距离。不用字体自带的上沿：Noto 的上沿比冬青黑体高约 0.28 em，换字体文字会整体下移。"""
    return round(size * 0.9)


class _Widths(dict[str, float]):
    """逐字宽度缓存：FreeType 量一次字约几微秒，长篇分页要量几十万次。顺带记下主字体画不出的字该用哪个字体。"""

    def __init__(self, font: ImageFont.FreeTypeFont):
        super().__init__()
        self.font = font
        self.fonts: dict[str, ImageFont.FreeTypeFont] = {}

    def __missing__(self, ch: str) -> float:
        if ch in INVISIBLE:
            w = self[ch] = 0.0
            return w
        font = _covering_font(self.font, ch)
        if font is not self.font:
            self.fonts[ch] = font
        w = self[ch] = 0.0 if _is_mark(ch) else font.getlength(ch)
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


def _draw_chars(
    d: ImageDraw.ImageDraw, x: float, y: int, text: str, widths: _Widths, fill: int, gap: float = 0.0, stroke: float = 0.0
) -> None:
    """逐字画一行；gap 是两端对齐时加在字之间的余量，组合符号贴着前一个字，不算。"""
    for c in text:
        w = widths[c]
        if c not in INVISIBLE:
            font = widths.fonts.get(c, widths.font)
            d.text((x if w else x - gap, y), c, font=font, fill=fill, anchor="ls", stroke_width=stroke)
        if w:
            x += w + gap


def _encode(img: Image.Image, levels: int) -> bytes:
    """量化到 levels 级灰度（levels 为 0 不量化），编码成 PNG。"""
    if levels:
        step = 255 / (levels - 1)
        img = img.point(lambda v: int(round(v / step) * step)).quantize(colors=levels, dither=Image.Dither.NONE)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_page(
    book: Book,
    chapter: ChapterPages,
    index: int,
    layout: Layout,
    *,
    levels: int = 4,
    weight: int = DEFAULT_WEIGHT,
) -> bytes:
    """把本章第 index 页渲染成灰度 PNG。levels > 0 时量化到 levels 级灰度（体积约为不量化的 1/3，字边仍平滑）。

    weight 是 :data:`WEIGHTS` 的档位。亮度不在图里：页面整屏盖半透明黑色，正文和原生控件一起变暗。
    """
    page = chapter.pages[index]
    img = Image.new("L", (layout.width, layout.height), 0)
    d = ImageDraw.Draw(img)
    widths = _widths(layout.font, layout.font_size)
    small = _font(HEITI, 16)
    small_widths = _widths(HEITI, 16)
    pad, right = layout.pad_x, layout.width - layout.pad_x

    # 页眉：章节名（左，过长截断）+ 全书百分比（右）
    pct = f"{book.percent(page.offset)}%"
    title = book.chapters[chapter.chapter].title
    room = right - pad - small.getlength(pct) - 16
    while title and sum(small_widths[c] for c in title) > room:
        title = title[:-2] + "…"
    _draw_chars(d, pad, 8 + _baseline(16), title, small_widths, DIM)
    d.text((right, 8 + _baseline(16)), pct, font=small, fill=DIM, anchor="rs")
    d.line((pad, layout.header_h - 6, right, layout.header_h - 6), fill=FAINT, width=1)

    # 正文：非段末行逐字画、字间均分剩余宽度（两端对齐）；悬挂标点允许伸进右边距
    indent_px = widths["　"] * layout.indent
    stroke, gain = WEIGHTS[weight]
    y = layout.header_h + (layout.line_height - layout.font_size) // 2 - 2
    for line in page.lines:
        x = pad + (indent_px if line.indent else 0)
        ws = [w for w in map(widths.__getitem__, line.text) if w]
        spare = right - x - sum(ws)
        gap = spare / (len(ws) - 1) if not line.last and len(ws) > 1 and spare > 0 else 0.0
        _draw_chars(d, x, y + _baseline(layout.font_size), line.text, widths, INK, gap, stroke)
        y += layout.line_height
    body = (0, layout.header_h, layout.width, layout.height - layout.footer_h)
    img.paste(img.crop(body).point(lambda v: min(255, round(v * gain))), body)

    # 页脚：全书进度条 + 本章页码
    by = layout.height - layout.footer_h + 10
    x1 = right - 70
    d.line((pad, by, x1, by), fill=FAINT, width=2)
    done = (x1 - pad) * book.percent(page.offset) // 100
    if done:
        d.line((pad, by, pad + done, by), fill=DIM + 40, width=2)
    d.text((right, by), f"{index + 1} / {len(chapter.pages)}", font=small, fill=DIM, anchor="rm")

    return _encode(img, levels)


def _title_layout(title: str) -> tuple[int, list[str]]:
    """书名的 (字号, 各列文字)。短的一列大字；长的缩小排两列，“书名 - 作者”优先各占一列；实在放不下才省略。"""
    head, _, tail = title.partition(" - ")
    head, tail = "".join(head.split()), "".join(tail.split())
    whole = head + tail
    if not tail and len(head) <= TITLE_SIZE[1] // 28:
        return 24, [head]
    rows = 0
    for size in (20, 18, 16, 14):
        rows = TITLE_SIZE[1] // (size + 4)
        if tail and len(head) <= rows and len(tail) <= rows:
            return size, [head, tail]
        if len(whole) <= rows * 2:
            half = (len(whole) + 1) // 2
            return size, [whole[:half], whole[half:]]
    return 14, [whole[:rows], whole[rows:rows * 2 - 1] + "…"]


def render_title(title: str) -> bytes:
    """左栏竖排书名：原生控件算不出字数，也拆不了列，所以和正文一样由服务端排好。"""
    size, columns = _title_layout(title)
    img = Image.new("L", TITLE_SIZE, 0)
    d = ImageDraw.Draw(img)
    widths = _widths(HEITI, size)
    stroke = WEIGHTS[DEFAULT_WEIGHT][0]
    for i, column in enumerate(columns):
        cx = TITLE_SIZE[0] * (2 * i + 1) // (2 * len(columns))
        for row, c in enumerate(column):
            widths[c]  # 量一次才知道这个字该用哪个字体
            if c not in INVISIBLE:
                y = row * (size + 4) + _baseline(size)
                d.text((cx, y), c, font=widths.fonts.get(c, widths.font), fill=255, anchor="ms", stroke_width=stroke)
    return _encode(img, 4)
