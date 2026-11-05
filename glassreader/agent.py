"""Agent 自定义接入（文字传输版，docs/agent.md）。

平台用 WebSocket 连 ``/agent``，这里把正文当原生文字推给眼镜上的 ``reader-text`` 页面。不接大模型：
语音只认固定说法（:func:`parse_command`），要整句匹配：会话里麦克风一直开着。不回答小说内容。

- 页面有 :data:`SLOTS` 个文字槽，排成一个环：翻页只是本地换显示哪个槽，立即响应，也不怕一两秒的断流。
  每翻一页上报当前槽号，这里记进度，并把环上离得远的槽换成新的邻页（只发变了的字段）。
- 云端单独跑一个实例，和页图版共用进度文件（:class:`~glassreader.library.Library` 负责不互相覆盖），两边接着读。
  会话可能开着 30 分钟，操作前要核对文件（:meth:`AgentSession._sync`），不然下一次存档用内存里的旧位置盖掉另一边的。
- 字号、粗细、亮度可调，单击保存才发 ``display``，这里重新分页再把环发回去。原生文字调不了行距。
- 真机亮度用平台的设备工具，只写不读：滑动改了才调用一次，打开页面不下发，记住的数只是上次设的值。
- 听书用平台的文字播报：按段落往下发，页面跟着翻。平台不告诉服务读到哪了，位置按语速估。
- 智能翻页用同一个引擎，不发声，按用户设的每秒字数走，每页停多久看这一页有多少字。开着菜单时停表。
- 平台约 120 秒没有业务消息就结束会话，而看一页可能不止这么久，所以定时重发一个不变的字段保活；
  用户太久没操作就不再保活，让会话自然结束。
- 所有协议处理都在 :class:`AgentSession` 里，收一条消息、返回要发的消息，不碰网络，方便单测；
  :func:`serve` 只管收发、定时和连接代次，处理出错不断连接（:meth:`AgentSession.failed`）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

import anyio
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import native_font
from .book import Book
from .library import BRIGHTNESS_LEVELS, DEFAULT_OPTICS, DEFAULT_PACE, MARKS, OPTICS_LEVELS, PACES, Library, normalize_title
from .mcp_tools import ReaderTools
from .pager import DEFAULT_WEIGHT, INVISIBLE, layout_for, wrap

__all__ = [
    "AGENT_PATH",
    "AHEAD",
    "BEHIND",
    "SLOTS",
    "SPEC_VERSION",
    "TEXT_SIZES",
    "AgentHub",
    "AgentSession",
    "TextPage",
    "parse_command",
    "serve",
    "text_pages",
]

log = logging.getLogger(__name__)

AGENT_PATH = "/agent"
SPEC_VERSION = "1.0"
#: 页面模板的名字：平台的 pages 列表里只有 templateId 和说明，按说明里的这个词认
PAGE_KEYWORD = "reader-text"
#: 亮度遮罩每档的透明度：四周和正文。屏幕灰度低于约 30 就不发光，所以四周的字本来就暗，遮得少一些
MASK_OUTER, MASK_BODY = 15, 22
#: 环上的槽数，和 skill/Pages/reader-text/template.xml 一致；当前页前面留 AHEAD 页、后面留 BEHIND 页
SLOTS, AHEAD, BEHIND = 8, 4, 3
#: 原生字号只有固定档位：24 以下有常规字模，28、32 只有粗体
TEXT_SIZES = (16, 18, 20, 22, 24, 28, 32)
DEFAULT_TEXT_SIZE = 24
#: 页面的主菜单是 m = 1～6，显示设置是 m = 11～16（16 是“返回”），每行高都是 32
MAIN_MENU, DISPLAY_MENU = range(1, 7), range(11, 17)
#: 显示设置从上到下的顺序：“返回”和主菜单的“返回阅读”一样排在最上面，进菜单先停在它上面。m 的编号不变，首尾相接的去向也不变
DISPLAY_ORDER = (DISPLAY_MENU[-1], *DISPLAY_MENU[:-1])
MAIN_ROW, DISPLAY_ROW = 32, 32
#: 显示设置里“段落标记”这一行进去的状态，别的行是 m + 10
MARK_ROW, MARK_PANEL = 15, 19
#: 自动翻页、智能翻页的数字面板
PANEL_AUTO, PANEL_SMART = 25, 26
WEIGHT_NORMAL, WEIGHT_BOLD = 400, 700
#: 粗体用页图版的第几档粗细（0 起），读取时这一档以上算粗体
BOLD_WEIGHT = 4
#: 正文区大小（skill/Pages/reader-text），宽度和页图版一样
BODY_WIDTH, BODY_HEIGHT = layout_for(DEFAULT_TEXT_SIZE).text_width, 238
#: 正文 label 的宽度：比正文区宽的部分留给行尾标点，再宽就被切掉（long-mode="clip" 不折行）
LABEL_WIDTH = 464
#: 正文 label 的左边和上边，段落标记按它和行距算位置
LABEL_LEFT, TEXT_TOP = 14, 30
#: 原生字体的行距，模拟器里量的，字号 20 每页 9 行压到进度条的现象和它对得上
LINE_PITCH = {16: 23, 18: 26, 20: 29, 22: 32, 24: 35, 28: 41, 32: 46}
#: 进度条总宽（skill/Pages/reader-text）
BAR_WIDTH = 380
#: 平台约 120 秒没有业务消息就结束会话
KEEPALIVE_SECONDS = 90
#: 这么久没操作就不再保活
IDLE_LIMIT_SECONDS = 1800
#: 记住多少个会话（断线重连要用）
MAX_SESSIONS = 64
#: 连接建立后等 SESSION_INIT 多久
INIT_TIMEOUT_SECONDS = 30
#: 听书：一个段落发一次，等待就落在段落之间。平台只收纯文字，声音和语速都改不了。
#: 平台的播报语速，标点也算字。2026-11-01 真机量到每秒约 6.9 字（59 字 8.59 秒）。平台没有播完回执，
#: 页面什么时候翻全靠它
LISTEN_CHARS_PER_SECOND = 6.6
#: 段落之间的停顿，折成字数。平台不支持插入静音，换行也不停，只能晚点发下一段；加上换行本身约 1 秒
LISTEN_PARAGRAPH_PAUSE_CHARS = 6
#: 发出第一段到听见声音大约隔多久
LISTEN_LATENCY_SECONDS = 1.0
#: 上一段还剩这么多秒就发下一段。0 = 估计读完才发：段落之间有空隙，声音每段都和估算对齐一次，不会越差越远
LISTEN_LEAD_SECONDS = 0.0
#: 页面比估算的位置早翻这么多秒：页面发到眼镜约 2.5 秒，声音发到眼镜也要时间，两边抵掉后差这么多
LISTEN_PAGE_EARLY_SECONDS = 0.5
#: 多久看一次要不要翻页、发下一段
LISTEN_TICK_SECONDS = 0.25
#: 暂停后往回退这么多字，再找到句首接着读
LISTEN_BACK_CHARS = 20
#: 最多退回这么多字：没有句号的长段落找不到近处的句首，不能一直退到段首，页面要等好久声音才追上来
LISTEN_BACK_LIMIT = 120
#: 这么久没操作就停下。断网时页面报不上来“摘下了”，不停的话进度会被一直往后推
LISTEN_IDLE_SECONDS = 7200
#: 智能翻页：只换圆点的 UPDATE 至少隔这么久。速度快时段落很短，每段发一次太频繁。翻页不受这个限制
SMART_MIN_SECONDS = 2.0
#: 段落标记的样式，下标是进度文件里存的数
MARK_NAMES = ("圆点", "竖线", "下划线", "圆角框")
MARK_DOT, MARK_BAR, MARK_UNDERLINE, MARK_BOX = MARKS
#: 标记没显示时的字段：这时不用动它们
MARK_IDLE = {"dy": TEXT_TOP, "dh": 0, "mx": 0, "mq": 0, "mw": 0, "mg": 0, "mc": "#000000", "mt": ""}
#: 圆点在各字号下的 (左, 上, 直径)，相对正文 label，模拟器里量的“　●”的墨迹，缩进第二格
MARK_DOTS = {16: (18, 7, 12), 18: (21, 8, 12), 20: (23, 9, 14), 22: (25, 10, 15), 24: (28, 12, 16), 28: (32, 13, 19), 32: (37, 14, 22)}
#: 竖线的位置和粗细，圆角框的上下留白
MARK_BAR_LEFT, MARK_BAR_WIDTH, MARK_PAD = 5, 3, 3
MARK_COLOR, DOT_COLOR = "#A0A0A0", "#E8E8E8"
_SENTENCE = re.compile(r".+?(?:[。！？!?…]+[”’」』）)]*|$)", re.S)
#: 环上每个槽的字段：正文
SLOT_FIELD = re.compile(r"s\d")
#: 翻不动时页面报同一个槽，可能是更新丢了，也可能只是还在路上。全量重发至少隔这么久：卡顿时不能一滑一环
RESEND_SECONDS = 5.0


def _table(values: dict[int, Any], size: int) -> list[dict[str, Any]]:
    """页面查表用的数组。元素写成 {n: 值}，和文档的 list[d:i].id 一样；下标没用到的填 0。
    数组只能整个发，所以只放不变的东西：每次翻页会变的东西放进来，每次都要重发整张。"""
    return [{"n": values.get(i, 0)} for i in range(size)]


def _ring_tables() -> dict[str, list[dict[str, int]]]:
    """环上的下一个、上一个槽。到环的尽头不翻由页面比 hi、lo，那两个数本来就随页发。"""
    slots = range(SLOTS)
    return {"nx": _table({i: (i + 1) % SLOTS for i in slots}, SLOTS), "pv": _table({i: (i - 1) % SLOTS for i in slots}, SLOTS)}


def _menu_tables() -> dict[str, list[dict[str, int]]]:
    """菜单首尾相接，高亮框的位置也查表：页面的表达式只能有一个运算符，算不了这些。"""
    following, previous, top = {}, {}, {}
    for menu, row in ((MAIN_MENU, MAIN_ROW), (DISPLAY_ORDER, DISPLAY_ROW)):
        for i, m in enumerate(menu):
            following[m], previous[m], top[m] = menu[(i + 1) % len(menu)], menu[i - 1], i * row
    size = DISPLAY_MENU[-1] + 1
    enter = {m: m + 10 for m in DISPLAY_MENU[:-1]} | {MARK_ROW: MARK_PANEL, DISPLAY_MENU[-1]: 4}  # 调字号这些是 m + 10，“返回”（16）回主菜单
    return {"mn": _table(following, size), "mp": _table(previous, size), "me": _table(enter, size), "tt": _table(top, size)}


def _size_tables() -> dict[str, list[dict[str, int]]]:
    """字号的下一档、上一档，到头是它自己。下标是字号。"""
    size = TEXT_SIZES[-1] + 1
    up = dict(zip(TEXT_SIZES, (*TEXT_SIZES[1:], TEXT_SIZES[-1]), strict=True))
    down = dict(zip(TEXT_SIZES, (TEXT_SIZES[0], *TEXT_SIZES[:-1]), strict=True))
    return {"up": _table(up, size), "dn": _table(down, size)}


def _mark_tables() -> dict[str, list[dict[str, Any]]]:
    """段落标记的下一种、上一种和名字，首尾相接。下标是样式。"""
    size = len(MARK_NAMES)
    names = [{"n": name} for name in MARK_NAMES]
    return {"ma": _table({i: (i + 1) % size for i in MARKS}, size), "mb": _table({i: (i - 1) % size for i in MARKS}, size), "ml": names}


def _text_tables() -> dict[str, list[dict[str, Any]]]:
    """同一个位置换文字。写成两个元素要多占一个绑定（a:else 也算），所以查表。nm pu pd 的下标是 m，mz mh 的下标是 ls。"""
    size = PANEL_SMART + 1
    names = {21: "字号", 22: "粗细", 23: "亮度", 24: "真机", PANEL_AUTO: "自动翻页", PANEL_SMART: "智能翻页"}
    return {
        "nm": _table(names, size),
        "pu": _table({PANEL_AUTO: "秒", PANEL_SMART: "字/秒"}, size),
        "pd": _table({PANEL_AUTO: "每页停留时间", PANEL_SMART: "每秒阅读字数"}, size),
        "mz": _table({0: "智能翻页", 1: "智能翻页", 2: "停止智能翻页"}, 3),
        "mh": _table({0: "听书", 1: "停止听书", 2: "听书"}, 3),
    }


def _mask_table() -> dict[str, list[dict[str, int]]]:
    """亮度遮罩分两层：全屏一层 lv * MASK_OUTER，正文和书名上再叠一层，叠完等于 lv * MASK_BODY。这是第二层的透明度，下标是 lv。"""
    def extra(lv: int) -> int:
        return round(255 * (1 - (255 - lv * MASK_BODY) / (255 - lv * MASK_OUTER)))

    return {"bo": _table({lv: extra(lv) for lv in range(len(BRIGHTNESS_LEVELS))}, len(BRIGHTNESS_LEVELS))}


#: 页面查的表里不变的那些，打开时发一次
PAGE_TABLES = _ring_tables() | _menu_tables() | _size_tables() | _mark_tables() | _mask_table() | _text_tables()
#: 这几个动作之后菜单还开着，别的动作都说明回到正文了
MENU_STAYS = ("display", "optics", "mark", "hidden", "away")
#: 页面自己也写的字段，全量重发时不带：它们可能比服务这边新
PAGE_OWNED = ("ls", "fs", "fw", "i", "ms", "bv", "lv", "ob")
#: 调眼镜真实亮度的工具，文档写的名字。可用的工具以 SESSION_INIT.tools 为准
OPTICS_TOOL = "change_brightness"
_CHINESE_DIGITS = "零一二三四五六七八九十百千两"
_HELP = "可以说下一页、下一章、跳到第几章、开始听书、智能翻页、换一本某某书，或者退出"
#: 会话里麦克风一直开着：比这长的话多半是在跟别人说话。书名号里的不算长度
MAX_COMMAND_CHARS = 14
#: 跳得远的命令动词不能省：“开头”“一半吧”“第三章吧”很可能是在跟别人说话，跳走了语音退不回来
_GO = r"(?:帮我)?(?:跳到|跳转到|翻到|回到|转到)"
#: 只动一步的命令动词可以省
_STEP = r"(?:帮我|请|再)?(?:跳到|跳转到|翻到|回到|转到|去|到)?"
#: 第几章、下一章还能这样说：“看第三章”“继续看下一章”
_READ = r"看|看看|读|念|打开|继续|继续看|继续读|接着看|我想看|我要看"
#: 下一页、上一页还能这样说：“看下一页”“继续看下一页”
_PAGE = rf"看|读|继续|继续看|{_STEP}"
#: 后面跟书名的说法。单独的“看”“读”不算：“看一下”“看这个”“读完了”太常见
_OPEN = r"(?:打开|换成|换一本|换本|我想看|我要看|继续看|继续读)"
#: 能说停就停的东西
_ENGINE = r"(?:听书|朗读|智能翻页)"


@dataclass(frozen=True)
class TextPage:
    offset: int
    text: str
    #: 每一行开头的字在全书里的位置
    starts: tuple[int, ...] = ()


class _NativeFont:
    """按码位查字宽。字体里没有的字眼镜上画成一格空白，不会回退到别的字体，宽度是 ``blank``。"""

    def __init__(self, ranges: tuple[tuple[int, int, int], ...], blank: int):
        self.ranges = ranges
        self.starts = [start for start, _, _ in ranges]
        self.blank = blank

    def advance(self, ch: str) -> int:
        code = ord(ch)
        i = bisect_right(self.starts, code) - 1
        return self.ranges[i][2] if i >= 0 and code <= self.ranges[i][1] else self.blank


_FONTS = {False: _NativeFont(native_font.REGULAR, native_font.REGULAR_BLANK), True: _NativeFont(native_font.BOLD, native_font.BOLD_BLANK)}


class _NativeWidths(dict[str, float]):
    """眼镜上每个字占多少像素。内核按整像素排，所以四舍五入。"""

    def __init__(self, size: int, bold: bool = False):
        super().__init__()
        self.size = size
        self.font = _FONTS[bold]

    def __missing__(self, ch: str) -> float:
        self[ch] = w = 0.0 if ch in INVISIBLE else float(round(self.font.advance(ch) * self.size / 1000))
        return w


#: 左栏书名占的地方，和页图版的书名图一样大
TITLE_BOX = (72, 176)
#: 书名一行一个标签，行距由页面定：原生文字调不了行距，一列一个标签的话 20 号要隔 29 像素一行，太松。
#: 字号固定 20，行距 24，和页图版书名图 20 号时一样，一列放 7 个字
TITLE_SIZE, TITLE_PITCH = 20, 24
TITLE_ROWS = TITLE_BOX[1] // TITLE_PITCH
_FULLWIDTH = str.maketrans({c: chr(ord(c) + 0xFEE0) for c in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"})


@lru_cache(maxsize=64)
def title_rows(title: str) -> tuple[str, ...]:
    """左栏竖排书名，从上到下每行的文字（TITLE_ROWS 行，没字的行是空串）。页面每行一个居中的标签，只会照着显示。

    规则照页图版的书名图（pager._title_layout）：一列放得下就一列；放不下排两列，“书名 - 作者”优先各占一列；
    实在放不下才省略。两列时一行是“左字、全角空格、右字”，哪边没字用全角空格占位，整行居中，两列的字就上下对齐。
    英文字母和数字比汉字窄，两列时换成全角的，不然这一行的两个字会往中间挤。
    """
    space = "\u3000"
    head, _, tail = title.partition(" - ")
    head, tail = ("".join(c for c in "".join(part.split()) if c not in INVISIBLE) for part in (head, tail))
    whole = head + tail
    n = TITLE_ROWS
    if not tail and len(head) <= n:
        rows = list(head)
    else:
        if tail and len(head) <= n and len(tail) <= n:
            left, right = head, tail
        elif len(whole) <= n * 2:
            half = (len(whole) + 1) // 2
            left, right = whole[:half], whole[half:]
        else:
            left, right = whole[:n], whole[n:n * 2 - 1] + "…"
        left, right = left.translate(_FULLWIDTH), right.translate(_FULLWIDTH)
        rows = [(left[i] if i < len(left) else space) + space + (right[i] if i < len(right) else space)
                for i in range(max(len(left), len(right)))]
    return tuple(rows + [""] * (n - len(rows)))


def lines_per_page(size: int) -> int:
    """最后一行放得下字高就行。排多了最后一行被切掉，下一页又接着往后排，这一行就看不到了。"""
    return (BODY_HEIGHT - size) // LINE_PITCH[size] + 1


@lru_cache(maxsize=64)
def text_pages(book: Book, mtime: float, chapter: int, size: int, anchor: int = 0, bold: bool = False) -> tuple[TextPage, ...]:
    """把一章排成原生文字的页。自己断好行（用和页图版一样的禁则），换行符直接写在文字里，每页行数才是确定的。

    ``anchor`` 处强制换页：换字号后当前页的第一个字还在页首，代价是它前面那一页可能不满。
    """
    widths = _NativeWidths(size, bold)
    per_page = lines_per_page(size)
    lines: list[tuple[int, str]] = []
    for offset, para in book.chapters[chapter].paragraphs:
        cut = anchor - offset
        parts = [(para[:cut], offset, size * 2), (para[cut:], anchor, 0)] if 0 < cut < len(para) else [(para, offset, size * 2)]
        for part, start, indent in parts:
            for line in wrap(part, start, BODY_WIDTH, widths, indent, LABEL_WIDTH):
                text = "".join(c for c in line.text if c not in INVISIBLE)
                lines.append((line.offset, "　　" + text if line.indent else text))
    before = sum(1 for offset, _ in lines if offset < anchor)
    pages = [part[i:i + per_page] for part in (lines[:before], lines[before:]) for i in range(0, len(part), per_page)]
    if not pages:
        return (TextPage(book.chapters[chapter].start, ""),)
    return tuple(TextPage(p[0][0], "\n".join(text for _, text in p), tuple(o for o, _ in p)) for p in pages)


def _squeeze(text: str) -> str:
    """去掉标点和空白。数字中间的点留着，“33.3%”不能变成 333。"""
    return re.sub(r"[，。！？、,!?\s]|(?<![0-9])\.|\.(?![0-9])", "", text)


def parse_command(text: str) -> tuple[str, str]:
    """把一句话解析成 (命令, 参数)。只认固定说法，要整句匹配，认不出来返回 ``("help", "")``，不猜。"""
    t = _squeeze(text)
    if not t or len(re.sub(r"《.*?》", "", t)) > MAX_COMMAND_CHARS:
        return "help", ""
    if re.fullmatch(r"退出(?:阅读|吧)?|结束阅读|不看了", t):
        return "exit", ""
    if re.fullmatch(r"(?:(?:开始|继续|接着|恢复)?(?:听书|朗读)|(?:继续|接着)(?:听|播放)|(?:读|念)给我听)吧?", t):
        return "listen", ""
    if re.fullmatch(r"(?:开始|打开|开启|开|继续|接着|恢复)?智能翻页吧?", t):
        return "smart", ""
    if re.fullmatch(
        rf"先?(?:暂停|停止)(?:一下|吧)?|先?(?:暂停|停止|关闭|关掉|退出){_ENGINE}(?:一下|吧)?"
        rf"|{_ENGINE}(?:(?:暂停|停止|关闭|关掉)(?:一下|吧)?|停一下)|停(?:一下|下来?)吧?"
        r"|(?:别|不要)(?:读|念|说|听|翻)了|不(?:听|读)了", t):
        return "quiet", ""
    if m := re.fullmatch(rf"(?:帮我|请|再)?(?:往后|向后)翻([0-9{_CHINESE_DIGITS}]+)页吧?", t):
        return "pages", m.group(1)
    if m := re.fullmatch(rf"(?:帮我|请|再)?(?:往前|向前)翻([0-9{_CHINESE_DIGITS}]+)页吧?", t):
        return "pages", "-" + m.group(1)
    if re.fullmatch(rf"(?:{_PAGE})(?:下一页|下页)吧?|(?:帮我|请|再)?(?:往后|向后)翻页?吧?|翻页吧?", t):
        return "pages", "1"
    if re.fullmatch(rf"(?:{_PAGE})(?:上一页|上页)吧?|(?:帮我|请|再)?(?:往前|向前)翻页?吧?", t):
        return "pages", "-1"
    if re.fullmatch(rf"(?:{_READ}|{_STEP})(?:下一[章张]|下章)吧?", t):
        return "chapter", "下一章"
    if re.fullmatch(rf"(?:{_READ}|{_STEP})(?:上一[章张]|上章)吧?", t):
        return "chapter", "上一章"
    if m := re.fullmatch(rf"(?:{_READ}|{_GO})(第[0-9{_CHINESE_DIGITS}]+[章节回])吧?", t):
        return "chapter", m.group(1)
    if m := re.fullmatch(rf"{_GO}([0-9]+)(?:\.[0-9]+)?[%％]吧?", t):
        return "percent", m.group(1)
    if m := re.fullmatch(rf"{_GO}百分之([0-9{_CHINESE_DIGITS}]+)(?:[.点][0-9{_CHINESE_DIGITS}]+)?吧?", t):
        return "percent", m.group(1)
    if re.fullmatch(rf"{_GO}(?:开头|最开头|最前面)(?:吧|去)?|从头(?:开始)?(?:看|读)", t):
        return "percent", "0"
    if re.fullmatch(rf"{_GO}一半吧?", t):
        return "percent", "50"
    if re.fullmatch(r"我?(?:上次|现在|当前)?(?:看|读)到哪(?:里|儿)?了?|(?:现在|当前|我的|阅读)?进度(?:是?多少|怎么样)?", t):
        return "status", ""
    if re.fullmatch(r"(?:打开|我的|看)?书架上?(?:有什么书|有哪些书|有什么|有哪些)?|有(?:哪些|什么)(?:书|小说)", t):
        return "shelf", ""
    if re.fullmatch(r"换一?本(?:(?:书|小说)(?:看|吧)?)?", t):  # 没说换哪本：报书架，用户接着说书名
        return "shelf", ""
    if re.fullmatch(r"(?:帮我)?(?:把?字[号体]?(?:放大(?:一?[点些])?|大一?[点些])|放大字[号体])吧?", t):
        return "font", "1"
    if re.fullmatch(r"(?:帮我)?(?:把?字[号体]?(?:缩小(?:一?[点些])?|小一?[点些])|缩小字[号体])吧?", t):
        return "font", "-1"
    if m := re.fullmatch(rf"(?:帮我|请)?(?:把|将|调)?亮度(?:调到|调成|调为|改成|改到|设为|设成|到)?(?:百分之)?([0-9{_CHINESE_DIGITS}]+)[%％]?吧?", t):
        return "brightness", m.group(1)
    if m := re.fullmatch(rf"(?:{_OPEN}|看|读)?《([^《》]+)》吧?", t):
        return "open", m.group(1)
    if re.fullmatch(rf"{_OPEN}(?:小说|书|小说吧|这本书|眼镜看小说|小说阅读器|文字阅读器|文字阅读|文字版|文字版小说)", t):
        return "open", ""
    if re.fullmatch(r"看小说|(?:继续|继续看|继续读|接着看|接着读)吧?", t):
        return "open", ""
    if m := re.fullmatch(rf"{_OPEN}(.{{2,}})", t):  # 书架上有没有这本，_command 再看
        return "open", m.group(1)
    return "help", ""


def _number(text: str) -> int | None:
    """``"12"`` / ``"三"`` / ``"二十"`` → 数字；太复杂的中文数字不认。"""
    if text.isdigit():
        return int(text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")} | {"两": 2}
    if len(text) == 1 and text in digits:
        return digits[text]
    if text in ("百", "一百"):
        return 100
    if "十" in text and "百" not in text and "千" not in text:
        tens, _, ones = text.partition("十")
        if (not tens or tens in digits) and (not ones or ones in digits):
            return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return None


class AgentSession:
    """一次 Agent 会话的状态。:meth:`handle` 收一条平台消息，返回要发回去的消息列表。"""

    def __init__(self, session_id: str, tools: ReaderTools, library: Library):
        self.id = session_id
        self.tools = tools
        self.library = library
        self.epoch = 0
        self.template_id = ""
        self.page_uuid = ""
        self.book: Book | None = None
        self.chapter = self.page = 0
        self.slot = 0
        self.size = self.saved_size = DEFAULT_TEXT_SIZE
        self.synced_ts = 0.0  # 这本书的进度上次写入或读到的时间，文件里比它新就是别人写的
        self.bold = False
        self.anchor = self.anchor_chapter = 0
        self.image_prefix = ""
        self.voice_turns = 0
        self.listening = False  # 引擎在跑：听书或智能翻页
        self.silent = False  # 引擎不发声，是智能翻页。停下后留着，说“继续”时要知道接着哪个
        self.pace = DEFAULT_PACE  # 智能翻页每秒多少字
        self.optics = DEFAULT_OPTICS  # 真机亮度：上次设的数，不是读到的
        self.mark = 0  # 段落标记的样式
        self.mark_preview: int | None = None  # 设置面板里正在看的样式，保存后清掉
        self.device_tools: set[str] = set()
        self.optics_warned = False  # 没调成只说一次，连滑时不能句句都播
        self.menu_open = False
        self.listen_held = False  # 听书被菜单停下了，回到正文接着读
        self.held_at: float | None = None  # 智能翻页停表的时刻：菜单开着
        self.resent_at = float("-inf")
        self.dot_due = 0.0  # 智能翻页：过了这个时刻才换圆点
        self.listen_at = self.listen_base = 0  # 下一段的开头；计时起点对应的字
        self.listen_para = -1  # 正在读的段落的开头
        self.listen_start = 0.0
        self.listen_resume = -1
        self.listen_page = ("", -1)  # 暂停在哪本书的哪一页
        self.sent: dict[str, Any] = {}
        self.closing = False
        self.socket: Any = None
        self.last_action = time.monotonic()
        self._speech = 0
        self._part = 0
        self._pages = 0
        self._calls = 0

    # ------------------------------------------------------------------ 协议

    def envelope(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"specVersion": SPEC_VERSION, "sessionId": self.id, "type": kind, "payload": payload}

    def ready(self, mode: str) -> dict[str, Any]:
        return self.envelope("SESSION_READY", {"mode": mode, "connectionEpoch": self.epoch})

    def error(self, code: str, message: str, *, fatal: bool = False) -> dict[str, Any]:
        return self.envelope("ERROR", {"scope": "SESSION", "code": code, "message": message, "fatal": fatal})

    def speak(self, text: str) -> dict[str, Any]:
        self._speech += 1
        action = {"speechId": f"speech_{self._speech}", "index": 1, "hasNext": False, "text": text}
        return self.envelope("OUTPUT", {"actions": [{"type": "SPEAK", "payload": action}]})

    def _show(self, mode: str, data: dict[str, Any]) -> dict[str, Any]:
        props: dict[str, Any] = {"data": data}
        if mode == "OPEN":
            props["templateId"] = self.template_id
        view = {"type": "TEMPLATE", "props": props}
        action = {"pageUuid": self.page_uuid, "display": {"mode": mode}, "view": view}
        return self.envelope("OUTPUT", {"actions": [{"type": "SHOW_UI", "payload": action}]})

    def start(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """第一次连接：记下页面模板，处理用户进入时说的那句话。"""
        pages = payload.get("pages") or []
        log.info("会话 %s 收到 %d 个页面：%s", self.id, len(pages),
                 [(p.get("templateId"), p.get("description"), bool(p.get("dataSchema"))) for p in pages])
        tools = payload.get("tools") or []
        log.info("会话 %s 可用工具：%s", self.id, tools)  # 文档只列了几个，实际下发什么要看这里
        self.device_tools = {str(t.get("name")) for t in tools if isinstance(t, dict)}
        wanted = [p for p in pages if PAGE_KEYWORD in str(p.get("description", ""))]
        self.template_id = next((str(p.get("templateId", "")) for p in wanted or pages), "")
        utterance = str((payload.get("entryContext") or {}).get("rawUtterance") or "")
        command, argument = parse_command(utterance)
        named = re.search(r"《(.+?)》", utterance)  # 进入时这句就是对眼镜说的，书名号在句中也算
        out = self._open(named.group(1) if named else argument if command == "open" else "")
        if self.book is None and not named and command == "open" and argument:
            return self._open("")  # “打开那个文字阅读器”：后面的词不是书名，不能进了会话没有页面
        return out

    def resumed(self) -> list[dict[str, Any]]:
        """断线重连：断线时丢的更新补不回来，全量重发；听书的计时断过了，从正在读的这段重读。
        段落比一页长时段首可能在前面几页，页面不回退，那就从本页页首读，不然声音要很久才追上页面。"""
        if self.book is None or not self.page_uuid:
            return []
        # 断线时滑过的话页面比这边新，不能把它拉回来。引擎走着时位置听服务的
        self.sent = {} if self.listening else {"i": self.sent.get("i", self.slot)}
        self._release()  # 断线时菜单可能关了，那条上报补不回来。宁可菜单后面翻页，也不能一直停着
        moved = self._sync()
        if self.listen_held:  # 关菜单的上报也可能丢了
            self.listen_held = False
            return self._listen()
        if not self.listening:
            return self._refresh()
        return self._restart(self._current().offset if moved else max(self.listen_para, self._current().offset))

    def handle(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        kind, payload = message.get("type"), message.get("payload") or {}
        if kind == "UTTERANCE":
            return self._command(str(payload.get("text", "")))
        if kind == "UI_ACTION":
            self.last_action = time.monotonic()
            return self._action(payload)
        if kind == "TOOL_RESULT":
            return self._tool_result(payload)
        if kind == "ERROR":
            log.warning("平台报错（会话 %s）：%s", self.id, payload)
        else:
            log.info("忽略消息 %s（会话 %s）", kind, self.id)
        return []

    def keepalive(self) -> list[dict[str, Any]]:
        """重发一个不变的字段，算一条业务消息；太久没操作就不发了。"""
        if not self.page_uuid or time.monotonic() - self.last_action > IDLE_LIMIT_SECONDS:
            return []
        return [self._show("UPDATE", {"t": self.sent.get("t", "")})]

    # ------------------------------------------------------------------ 眼镜上的操作

    def _action(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        action = payload.get("action") or {}
        name, params = action.get("name"), action.get("params") or {}
        if payload.get("pageUuid") != self.page_uuid or self.book is None:
            return []  # 旧页面发来的
        # 上报丢过（断线时不补发）的话页面已经翻走了，先跟上，不然退出、开听书都从旧的那一页算。
        # v 对不上时不算：服务刚翻的一页还在路上，槽号比这边旧是正常的
        behind: list[dict[str, Any]] = []
        if name != "turn" and params.get("slot") != self.slot and params.get("v") == self.voice_turns:
            behind = self._turn(params.get("slot"), params.get("v"))
        return [*behind, *self._act(name, params), *self._back_to_reading(name)]

    def _act(self, name: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
        if name == "menu_open":
            return self._hold()
        if name not in MENU_STAYS:
            self._release()
        if name == "turn":
            return self._turn(params.get("slot"), params.get("v"))
        if name == "chapter":  # z 是第几章。听书时要先停，再从新位置读
            return self._perform("number", str(params.get("z")), "")
        if name in ("display", "listen", "smart") and not self.listening:
            self._sync()
        if name == "display":
            return self._display(params)
        if name == "listen":
            if self.listen_held:  # 菜单已经让它停了，选“停止听书”就是回到正文也不接着读
                return self._drop_held()
            return self._stop_listening() if self.listening and not self.silent else self._engage(False)
        if name == "optics":
            self.sent["ob"] = params.get("o")  # 页面已经写了，发回去的话连滑时会把它拉回来
            return self._set_optics(params.get("o"))
        if name == "mark":
            return self._preview_mark(params.get("k"))
        if name == "smart":  # 面板上确定：已经走着就只换速度。ls 重发一次：页面可能清过，开自动翻页的上报又丢了
            self._set_pace(params.get("z"))
            self.sent.pop("ls", None)
            return self._engage(True) or self._refresh()
        if name == "away":  # 摘下眼镜
            return self._stop_listening() if self.listening else self._drop_held()
        # 灭屏：智能翻页没人看，存档不能一路往后推；听书不用看屏幕，接着读。选了自动翻页（z 是秒数，0 = 没开）：后选的算
        if name in ("hidden", "smart_off") or (name == "auto" and params.get("z")):
            return self._stop_listening() if self.listening and self.silent else []
        if name in ("auto", "menu_close"):
            return []
        if name == "exit":  # 双击两次：页面自己关了，会话要由服务结束，麦克风才会关
            out = [self._stop_output()] if self.listening and not self.silent else []  # 平台关会话时停不停播没验证过
            self.closing, self.listening = True, False
            return out
        log.info("不认识的页面动作 %r", name)
        return []

    def _turn(self, slot: Any, seen: Any = None) -> list[dict[str, Any]]:
        if not isinstance(slot, int) or not 0 <= slot < SLOTS:
            return []
        delta = (slot - self.slot) % SLOTS
        if delta > AHEAD:
            delta -= SLOTS
        if delta == 0 and time.monotonic() - self.resent_at >= RESEND_SECONDS:
            # 只有走到环的尽头翻不动才会报同一个槽：这边早到了，更新丢了，或者还在路上。丢了的话页面一直以为到头了
            self.resent_at = time.monotonic()
            self.sent = {k: v for k, v in self.sent.items() if k in PAGE_OWNED or k == "tb"}
        self.slot = slot
        if seen in (None, self.voice_turns):  # 页面自己翻过去的，发回去的话连滑时会把它拉回来。没看到服务刚翻的页就要发：那条会把页面带走
            self.sent["i"] = slot
            if "pn" in self.sent:  # 页面自己加减过了，到头就停
                self.sent["pn"] = min(max(self.sent["pn"] + delta, 1), self.sent["pt"])
        moved = self._sync()  # 换到了另一个实例留下的那一页，这一滑就算翻到它，再走一页要少看一页
        before = self.chapter, self.page
        if not moved:
            self.chapter, self.page = self._walk(delta, clamp=True) or before
            self._save()
        if not self.listening or (not moved and (self.chapter, self.page) == before):  # 到书头书尾滑不动，接着走
            return self._refresh()
        return self._restart(self._current().offset)  # 翻走了就从新的一页读

    # ------------------------------------------------------------------ 语音

    def _command(self, text: str) -> list[dict[str, Any]]:
        command, argument = parse_command(text)
        if command == "open" and argument and not re.search(r"《|换一?本", text) and not self._on_shelf(argument):
            command, argument = "help", ""  # “打开空调”。说了书名号或“换一本”才算在找书，要告诉用户没找到
        if command != "help":  # 旁边有人说话不算操作，不然保活一直停不下来
            self.last_action = time.monotonic()
        if command == "open" and not argument:
            if self.listening:
                return []  # 正读着，“继续”不用打断
            if self.book is not None and self._paused_here():
                command = "smart" if self.silent else "listen"  # 暂停后说“继续”是接着读
        if self.listening and not self.silent and command == "help":
            return []  # 读着的时候不接别的话，也不能和别的播报交错
        return self._perform(command, argument, text)

    def _perform(self, command: str, argument: str, text: str) -> list[dict[str, Any]]:
        if not self.listening or command in ("font", "brightness", "listen", "smart"):
            return self._run(command, argument, text)
        # 不能和别的播报交错。智能翻页没有语音流，只有换书和暂停才停
        stopped = self._stop_listening() if not self.silent or command in ("open", "quiet") else []
        turns = self.voice_turns
        out = self._run(command, argument, text)
        again: list[dict[str, Any]] = []
        if self.voice_turns != turns:  # 跳成了。不能比位置：在第一页说“回到开头”也要从头读
            again = self._start_listening(self._current().offset)
        elif command in ("pages", "chapter", "number", "percent") and not self.silent:  # 没跳成，刚才停了，接着读
            again = self._start_listening(self.listen_resume)
        return [*stopped, *out, *again]

    def _run(self, command: str, argument: str, text: str) -> list[dict[str, Any]]:
        if command == "help":  # 听到别的话不接：播报会遮住页面，旁边有人说话时一直响
            return [self.speak(_HELP)] if re.fullmatch(r"帮助|怎么用|能说什么", _squeeze(text)) else []
        if command == "exit":
            self.closing = True
            return [self.speak("好的，下次接着看")]
        if command == "shelf":
            return [self.speak(self.tools.list_books()["message"])]
        if command == "open":
            return [*(self._drop_held() if argument else []), *self._open(argument)]
        if self.book is None:
            return [self.speak("还没打开书，" + _HELP)]
        # 这些都从当前位置出发。翻页和跳转之后 _command 会让引擎从新位置起步，听书这时已经停了，智能翻页没停；
        # 别的命令引擎不重新起步，走着时不能换位置：页面上的位置和引擎估算的就对不上了
        if command in ("chapter", "number", "percent", "pages") or (command in ("listen", "smart", "font") and not self.listening):
            self._sync()
        if command in ("listen", "smart"):
            return self._engage(command == "smart")
        if command == "quiet":
            return self._drop_held()  # 走着的话 _command 已经停了
        if command == "status":
            return [self.speak(self.tools.reading_status(self.book.title)["message"])]
        if command == "chapter":
            return self._jump(chapter=argument)
        if command == "number":
            return self._jump_to_number(_number(argument))
        if command == "percent":
            number = _number(argument)
            return self._jump(percent=float(number)) if number is not None and number <= 100 else [*self._refresh(), self.speak(_HELP)]
        if command == "pages":
            sign = -1 if argument.startswith("-") else 1
            number = _number(argument.lstrip("-"))
            if number is None:
                return [*self._refresh(), self.speak(_HELP)]
            before = self.chapter, self.page
            self._go(sign * number)
            if (self.chapter, self.page) != before:
                self.voice_turns += 1  # 页面每秒比对，变了就重新倒计时
            self._save()
            return self._refresh()
        if command == "font":
            return self._font(int(argument))
        if command == "brightness":
            return self._voice_brightness(_number(argument))
        return [self.speak(_HELP)]

    def _on_shelf(self, title: str) -> bool:
        """是不是书架上某本书名的开头。书名中间的字不算，“打开灯”不能打开《鬼吹灯》。
        先扫描：find 只在一本都没找到时才扫，旧书名中间含这几个字时，新放进去的书就对不上了。"""
        query = normalize_title(title)
        if len(query) < 2:
            return False
        self.library.scan()
        return any(normalize_title(e.title).startswith(query) for e in self.library.find(query, rescan=False))

    def _open(self, title: str) -> list[dict[str, Any]]:
        result = self.tools.open_book(title)
        if not result["success"]:
            return [self.speak(result["message"])]
        entry = self.library.find(result["title"])[0]
        book = self.library.book(entry.id)
        if book is None:
            return [self.speak(f"《{entry.title}》的文件打不开了")]
        if not self.template_id:
            return [self.speak("阅读页面还没发布，没办法显示。")]
        self.book = book
        self.listen_page = ("", -1)
        self.bold = self.library.display.weight >= BOLD_WEIGHT
        self.pace = self.library.display.pace
        self.optics = self.library.display.optics
        self.mark = self.library.display.mark
        self._load_position()
        out = self._refresh(opening=not self.page_uuid)
        return [*out, self.speak(result["message"])]

    def _jump(self, *, chapter: str | None = None, percent: float | None = None) -> list[dict[str, Any]]:
        """没跳成也要 _refresh：_run 核对文件时可能换了位置，页面还不知道，下一滑就跳过了那一页。"""
        assert self.book is not None
        readable = self.book.readable()
        if chapter == "下一章" and self.chapter >= readable[-1]:  # 要在 library.jump 之前：它会夹到本章开头并存档
            return [*self._refresh(), self.speak("已经是最后一章")]
        if chapter == "上一章" and self.chapter <= readable[0]:
            return [*self._refresh(), self.speak("已经是第一章")]
        self._save()  # library.jump 从已保存的位置算下一章
        v = self.library.jump(self.book, chapter=chapter, percent=percent)
        if v is None:
            return [*self._refresh(), self.speak(f"没有找到{chapter}")]
        return self._land(v.book.chapters[v.chapter].start if chapter else v.offset)

    def _jump_to_number(self, number: int | None) -> list[dict[str, Any]]:
        """菜单里选的第几章，只数有正文的章。标题里的编号可能不连续，页面只会加减，比不了这些。"""
        assert self.book is not None
        readable = self.book.readable()
        if number is None or not 1 <= number <= len(readable):
            return self._refresh()
        return self._land(self.book.chapters[readable[number - 1]].start)

    def _land(self, offset: int) -> list[dict[str, Any]]:
        assert self.book is not None
        self._place(offset)
        self.voice_turns += 1
        self._save()
        title = self.book.chapters[self.chapter].base_title
        return [*self._refresh(), self.speak(f"已跳到{title}")]

    def _font(self, steps: int) -> list[dict[str, Any]]:
        assert self.book is not None
        offset = self._current().offset
        i = TEXT_SIZES.index(self.size)
        self.size = self.saved_size = TEXT_SIZES[min(max(i + steps, 0), len(TEXT_SIZES) - 1)]
        self._place(offset)
        self._save()
        return self._refresh()

    def _voice_brightness(self, level: int | None) -> list[dict[str, Any]]:
        """一个数同时定两个亮度，越大越亮。0～9 真机 0，软件 10%～100%；10～100 软件满，真机就是这个数，
        取整十：页面一步走 10，不是整十就走过了头。"""
        if level is None or level > 100:
            return []
        self.library.set_display(brightness=min(level + 1, 10) * 10)
        return [*self._set_optics(0 if level < 10 else (level + 5) // 10 * 10), *self._refresh()]

    def _display(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """页面发来的显示设置，都是绝对值：连滑时后到的覆盖先到的，不认识的值不用。
        只存变了的几项，别的留给文件里最新的值：另一个实例可能刚存过。"""
        assert self.book is not None
        offset, size, bold, saved = self._current().offset, self.size, self.bold, self.saved_size
        changes: dict[str, int] = {}
        if params.get("f") in TEXT_SIZES and params["f"] != self.sent.get("fs", self.size):  # 页面每次都带 f，没改就留着页图版的 26、30
            self.size = self.saved_size = params["f"]
        if params.get("w") in (WEIGHT_NORMAL, WEIGHT_BOLD) and (params["w"] == WEIGHT_BOLD) != self.bold:
            self.bold = params["w"] == WEIGHT_BOLD
            changes["weight"] = BOLD_WEIGHT if self.bold else DEFAULT_WEIGHT  # 没切换就留着页图版的六档粗细
        self.mark_preview = None
        if params.get("k") in MARKS and params["k"] != self.mark:
            self.mark = changes["mark"] = params["k"]
        if params.get("b") in BRIGHTNESS_LEVELS and params["b"] != self.sent.get("bv"):  # 页面每次都带 b，没改就留着另一边的
            changes["brightness"] = self.sent["bv"] = params["b"]
            self.sent["lv"] = (100 - params["b"]) // 10
        if changes:
            self.library.set_display(**changes)
        if (self.size, self.bold) != (size, bold):  # 两种粗细的字宽不一样，要重新分页
            self._place(offset)
        if (self.size, self.saved_size) != (size, saved):  # 没改字号就没有要存进这本书的：存一下会刷新最近阅读，引擎走着时还会盖掉另一边
            self._save()
        return self._refresh()

    # ------------------------------------------------------------------ 听书

    def _paused_here(self) -> bool:
        """还在暂停时的那一页。不看 listen_resume 落在哪：它退回句首后可能在页首之前。"""
        assert self.book is not None
        return self.listen_page == (self.book.id, self._current().offset)

    def _listen(self) -> list[dict[str, Any]]:
        """暂停后还在这一页就接着读，翻走了就从这一页的开头读。"""
        return self._start_listening(self.listen_resume if self._paused_here() else self._current().offset)

    def _engage(self, silent: bool) -> list[dict[str, Any]]:
        """开听书或智能翻页。两个互斥：另一个开着就先停，一样的开着不用动。"""
        if self.listening and self.silent == silent:
            return []
        stopped = self._stop_listening() if self.listening else []
        self.silent = silent
        return [*stopped, *self._listen()]

    def _set_pace(self, pace: Any) -> None:
        """智能翻页面板上确定的速度。已经走着时从估算的位置按新速度重新计时。"""
        if pace not in PACES or pace == self.pace:
            return
        if self.listening and self.silent:
            self.listen_base, self.listen_start = self._heard(), time.monotonic()
        self.pace = pace
        self.library.set_display(pace=pace)

    def _set_optics(self, value: Any) -> list[dict[str, Any]]:
        """用户改了真机亮度。工具没下发时页面上的数白改，说一声。"""
        if type(value) is not int or value not in OPTICS_LEVELS or value == self.optics:
            return []
        self.optics = value
        self.library.set_display(optics=value)
        if OPTICS_TOOL not in self.device_tools:
            return self._optics_warning("这次会话平台没有开放亮度调节")
        self._calls += 1
        call = {"callId": f"optics_{self._calls}", "name": OPTICS_TOOL, "arguments": {"operation": "SET", "value": value}}
        return [self.envelope("TOOL_CALL", call)]

    def _tool_result(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """文档说只回 DISPATCHED，整条记下来，看实际带不带当前亮度。"""
        log.info("工具结果（会话 %s）：%s", self.id, payload)
        if not str(payload.get("callId")).startswith("optics_"):
            return []
        if payload.get("status") in ("DISPATCHED", "SUCCESS"):
            self.optics_warned = False
            return []
        return self._optics_warning("真机亮度没调成")

    def _optics_warning(self, text: str) -> list[dict[str, Any]]:
        if self.optics_warned or (self.listening and not self.silent):  # 听书时不能和朗读交错
            return []
        self.optics_warned = True
        return [self.speak(text)]

    def _hold(self) -> list[dict[str, Any]]:
        """菜单打开：智能翻页停表，页面不翻，读到哪也不往后算。听书平台停不住计时，只能停播，回到正文从句首再读。"""
        self.menu_open = True
        if not self.listening:
            return []
        if not self.silent:
            self.listen_held = True  # 先记下：页面上的 ls 不变，菜单还要能停掉它
            return self._stop_listening()
        if self.held_at is None:
            self.held_at = time.monotonic()
        return []

    def _release(self) -> None:
        """回到正文：菜单开着的时间不算。"""
        self.menu_open = False
        if self.held_at is not None:
            spent = time.monotonic() - self.held_at
            self.listen_start += spent
            self.dot_due += spent
            self.held_at = None

    def _back_to_reading(self, name: Any) -> list[dict[str, Any]]:
        """被菜单停下的听书接着读。换到智能翻页时 _start_listening 已经清了标记。"""
        if not self.listen_held or name == "menu_open" or name in MENU_STAYS:
            return []
        self.listen_held = False
        return [] if self.closing else self._listen()

    def _drop_held(self) -> list[dict[str, Any]]:
        """不接着读了，页面上的 ls 要跟着清。"""
        if not self.listen_held:
            return []
        self.listen_held = False
        return self._refresh()

    def _restart(self, offset: int) -> list[dict[str, Any]]:
        """换个位置接着走，模式不变。智能翻页没有语音流，不用先停。"""
        return [*([] if self.silent else self._stop_listening()), *self._start_listening(offset)]

    def _start_listening(self, offset: int) -> list[dict[str, Any]]:
        self.listening, self.listen_held = True, False
        self.listen_page = ("", -1)  # 走起来就不算暂停了，不然读完全书后“继续听书”会回到旧位置
        self.listen_at = self.listen_base = offset
        self.listen_start = time.monotonic() + (0.0 if self.silent else LISTEN_LATENCY_SECONDS)
        self.dot_due = 0.0
        self._speech += 1
        self._part = 0
        if self.silent and self.menu_open:  # 菜单开着时用语音开始或跳走：从新位置起步，还是等菜单关了再走
            self.held_at, self.listen_para = self.listen_start, offset
            return self._refresh()
        return self.listen_tick()

    def _stop_listening(self) -> list[dict[str, Any]]:
        assert self.book is not None
        self.listening = False
        heard = self._heard()
        self.held_at = None
        self.listen_resume = max(self._sentence_start(heard - LISTEN_BACK_CHARS), heard - LISTEN_BACK_LIMIT)
        self.listen_page = (self.book.id, self._current().offset)
        log.info("听书停在 %d，下次从 %d 读（会话 %s）", heard, self.listen_resume, self.id)
        return [*([] if self.silent else [self._stop_output()]), *self._refresh()]

    def _stop_output(self) -> dict[str, Any]:
        action = {"type": "STOP_OUTPUT", "payload": {"scope": "SPEAK", "reason": "USER_ACTION"}}
        return self.envelope("OUTPUT", {"actions": [action]})

    def failed(self) -> list[dict[str, Any]]:
        """处理出错：听书停下，不然下一段还会撞上同一个错；连接留着，用户还能接着操作。
        页面上的 ls 要一起清，不然菜单还显示“停止听书”，选它反而开始读。不走 _refresh：出错的可能就是它。
        重连时 sent 刚清空，不知道页面上是什么，也要发。"""
        out = [self._stop_output()] if self.listening and not self.silent else []
        self.listening = self.listen_held = False
        self.held_at = None
        if self.page_uuid and self.sent.get("ls", 1):
            self.sent["ls"] = 0
            out.append(self._show("UPDATE", {"ls": 0}))
        return [*out, self.error("INTERNAL", "处理出错")]

    def _heard(self) -> int:
        """估计现在读到第几个字。听书不会超过已经发出去的，智能翻页没有发出去这回事，只看时间。"""
        spent = max((time.monotonic() if self.held_at is None else self.held_at) - self.listen_start, 0.0)
        if self.silent:
            return self.listen_base + int(spent * self.pace)
        return min(self.listen_base + int(spent * LISTEN_CHARS_PER_SECOND), self.listen_at)

    def listen_tick(self) -> list[dict[str, Any]]:
        """页面跟上估计的位置，快读完时发下一段。"""
        if not self.listening:
            return []
        if time.monotonic() - self.last_action > LISTEN_IDLE_SECONDS:
            return self._stop_listening()
        if self.held_at is not None:
            return []
        if self.silent:
            return self._smart_tick()
        heard = self._heard()
        out = self._refresh() if self._follow(heard + int(LISTEN_PAGE_EARLY_SECONDS * LISTEN_CHARS_PER_SECOND)) else []
        if heard + LISTEN_LEAD_SECONDS * LISTEN_CHARS_PER_SECOND < self.listen_at:
            return out
        passage = self._passage(self.listen_at)
        if passage is None:
            if heard < self.listen_at:
                return out
            self.listening = False
            return [*out, *self._refresh(), self._say_part("全书读完了", more=False)]
        text, following = passage
        self.listen_base -= len(text) - (following - self.listen_at) + LISTEN_PARAGRAPH_PAUSE_CHARS  # 章节名和停顿不占全书的字数
        log.info("听书第 %d 段：%d 字，估计读到 %d，发到 %d（会话 %s）", self._part + 1, len(text), heard, following, self.id)
        self.listen_para, self.listen_at = self.listen_at, following
        return [*out, *self._refresh(), self._say_part(text, more=True)]

    def _smart_tick(self) -> list[dict[str, Any]]:
        """智能翻页：不发声，页面按字数翻。章节名和段落间隔不算时间，翻页也不提前，
        发送延迟是常数，不影响每页停多久。圆点隔够 SMART_MIN_SECONDS 才换，翻页时顺带换。"""
        now, heard = time.monotonic(), self._heard()
        para, following = self.listen_para, self.listen_at
        while following <= heard:  # 速度快时一拍能走过好几段，不能一段一段地等
            passage = self._passage(following)
            if passage is None:
                self._follow(heard)
                self.listening = False
                return [*self._refresh(), self.speak("全书读完了")]
            para, following = following, passage[1]
        if not self._follow(heard) and now < self.dot_due:
            return []
        self.listen_para, self.listen_at, self.dot_due = para, following, now + SMART_MIN_SECONDS
        return self._refresh()

    def _say_part(self, text: str, *, more: bool) -> dict[str, Any]:
        self._part += 1
        action = {"speechId": f"speech_{self._speech}", "index": self._part, "hasNext": more, "text": text}
        return self.envelope("OUTPUT", {"actions": [{"type": "SPEAK", "payload": action}]})

    def _follow(self, offset: int) -> bool:
        """读到下一页的第一个字就翻过去，返回翻了没有。"""
        assert self.book is not None
        moved = False
        while (following := self._walk(1)) is not None:
            if self._chapter_pages(following[0])[following[1]].offset > offset:
                break
            self._go(1)
            moved = True
        if moved:
            self.voice_turns += 1
            self._save()
        return moved

    def _preview_mark(self, style: Any) -> list[dict[str, Any]]:
        """设置面板里看标记的样子，单击保存才存。ms 页面已经写了，发回去的话连滑时会把它拉回来。"""
        if style not in MARKS:
            return []
        self.mark_preview = self.sent["ms"] = style
        return self._refresh()

    def _marked_lines(self, slot: int, page: TextPage) -> tuple[list[int], bool]:
        """这一页要标哪几行，第一行是不是段首。没在读时预览：拿当前页的第一段示范。"""
        texts = page.text.split("\n")
        if self.listening:
            following = max(self.listen_at, self.listen_para + 1)  # 刚起步时还没算到下一段
            lines = [i for i, start in enumerate(page.starts) if self.listen_para <= start < following]
            return lines, bool(lines) and page.starts[lines[0]] == self.listen_para and texts[lines[0]].startswith("　　")
        if self.mark_preview is None or slot != self.slot:
            return [], False
        heads = [i for i, text in enumerate(texts) if text.startswith("　　")]
        first = heads[0] if heads else 0
        return list(range(first, next((i for i in heads if i > first), len(texts)))), bool(heads)

    def _mark_fields(self, slot: int, page: TextPage) -> dict[str, Any] | None:
        """这一页的段落标记。圆点和下划线的缩进都要段首；从段落中间接着读时没有缩进，圆点就不标了。"""
        lines, opening = self._marked_lines(slot, page)
        style = self.mark if self.mark_preview is None else self.mark_preview
        if not lines or (style == MARK_DOT and not opening):
            return None
        pitch, texts = LINE_PITCH[self.size], page.text.split("\n")
        fields = {**MARK_IDLE, "ds": slot, "dy": TEXT_TOP + lines[0] * pitch, "dh": len(lines) * pitch + MARK_PAD}
        if style == MARK_DOT:
            left, top, size = MARK_DOTS[self.size]
            return fields | {"mx": LABEL_LEFT + left, "mq": top, "mw": size, "mg": size, "mc": DOT_COLOR}
        if style == MARK_BAR:
            return fields | {"mx": MARK_BAR_LEFT, "mq": MARK_PAD, "mw": MARK_BAR_WIDTH, "mg": len(lines) * pitch - 2, "mc": MARK_COLOR}
        if style == MARK_UNDERLINE:
            fields["mt"] = "\n".join(self._blanks(texts[i]) for i in lines)
            if opening:  # 空格上也有下划线，缩进要盖住
                fields |= {"mx": LABEL_LEFT - 6, "mw": self.size * 2 + 6, "mg": pitch}
        return fields

    def _blanks(self, line: str) -> str:
        """和这一行一样长的空格：下划线跟着长度走，字不用画第二遍，画两遍会变粗。"""
        widths = _NativeWidths(self.size, self.bold)
        width = sum(widths[c] for c in line)
        return "　" * int(width // self.size) + " " * int(width % self.size // widths[" "])

    def _passage(self, offset: int) -> tuple[str, int] | None:
        """从 offset 取一个段落（章首带上章节名），返回 (文字, 下一段的开头)，书读完了返回 None。"""
        assert self.book is not None
        for chapter in self.book.chapters[self.book.chapter_at(offset):]:
            if chapter.end <= offset:
                continue
            title = chapter.spoken + "。\n" if chapter.spoken and offset <= chapter.start else ""
            for start, para in chapter.paragraphs:
                if offset < start + len(para):
                    text = title + para[max(offset - start, 0):] + "\n"
                    return "".join(c for c in text if c not in INVISIBLE), start + len(para)
        return None

    def _sentence_start(self, offset: int) -> int:
        """offset 所在句子的开头。"""
        assert self.book is not None
        for start, para in self.book.chapters[self.book.chapter_at(offset)].paragraphs:
            if start <= offset < start + len(para):
                begins = [start + m.start() for m in _SENTENCE.finditer(para)]
                return max((b for b in begins if b <= offset), default=start)
        return max(offset, 0)

    # ------------------------------------------------------------------ 位置和环

    def _chapter_pages(self, chapter: int) -> tuple[TextPage, ...]:
        assert self.book is not None
        anchor = self.anchor if chapter == self.anchor_chapter else 0
        return text_pages(self.book, self.book.mtime, chapter, self.size, anchor, self.bold)

    def _current(self) -> TextPage:
        return self._chapter_pages(self.chapter)[self.page]

    def _load_position(self) -> None:
        assert self.book is not None
        self.synced_ts = self.library.last_read(self.book.id)  # 先取时间：这之后另一边写的，下次 _sync 还看得到
        offset, size = self.library.position(self.book)
        self.size = size if size in TEXT_SIZES else min(TEXT_SIZES, key=lambda s: abs(s - size))
        self.saved_size = size  # 页图版的 26、30 这里没有，用户没改就原样存回去
        self._place(offset)

    def _sync(self) -> bool:
        """别的实例写过这本书：换到文件里的位置和字号，返回位置变了没有。内存里的是旧值，留着就盖掉人家的。

        自己写的 :meth:`_save` 都记了时间，听书、智能翻页走着也不会触发。只认更新的：文件没了或退回旧内容时，
        这边接着写就补上了，不能跟着跳回书头。比的是当前页的页首：只换了字号，或者文件里的偏移落在这一页中间
        （页图版跳到全书末尾时偏移是全书长度），用户看到的还是这一页，这一滑要照常翻。
        """
        assert self.book is not None
        written = self.library.last_read(self.book.id)
        if written <= self.synced_ts:
            return False
        self.synced_ts = written
        offset = self._current().offset
        if self.library.position(self.book) == (offset, self.saved_size):
            return False  # 打开书也算写，没有动过就别打断
        self._load_position()
        if self._current().offset == offset:
            return False
        self.voice_turns += 1  # 位置是服务端换的，页面的自动翻页要重新倒计时
        return True

    def _place(self, offset: int) -> None:
        """让 offset 这个字成为当前页的第一个字。"""
        assert self.book is not None
        readable = self.book.readable()
        chapter = self.book.chapter_at(offset)  # 书尾只有标题的章节没有页，停在它前面的章
        self.anchor, self.chapter = offset, max((i for i in readable if i <= chapter), default=readable[0])
        self.anchor_chapter = self.chapter
        pages = self._chapter_pages(self.chapter)
        self.page = max((i for i, p in enumerate(pages) if p.offset <= offset), default=0)

    def _step(self, chapter: int, page: int, delta: int) -> tuple[int, int] | None:
        """前后走一页，跨章，空章节跳过；到书头书尾返回 None。"""
        assert self.book is not None
        if 0 <= page + delta < len(self._chapter_pages(chapter)):
            return chapter, page + delta
        readable = self.book.readable()
        neighbours = [i for i in readable if i > chapter] if delta > 0 else [i for i in readable if i < chapter]
        if not neighbours:
            return None
        if delta > 0:
            return neighbours[0], 0
        return neighbours[-1], len(self._chapter_pages(neighbours[-1])) - 1

    def _go(self, delta: int) -> None:
        """服务这边往前后走几页，到书头书尾就停。槽号跟着走：页面切过去，环上只用补新进来的页。"""
        sign = 1 if delta > 0 else -1
        for _ in range(abs(delta)):
            position = self._step(self.chapter, self.page, sign)
            if position is None:
                return
            self.chapter, self.page = position
            self.slot = (self.slot + sign) % SLOTS

    def _walk(self, delta: int, *, clamp: bool = False) -> tuple[int, int] | None:
        """从当前页走 delta 页。走不到时 clamp 停在头尾，否则返回 None。"""
        position = (self.chapter, self.page)
        for _ in range(abs(delta)):
            step = self._step(*position, 1 if delta > 0 else -1)
            if step is None:
                return position if clamp else None
            position = step
        return position

    def _slot_data(self) -> dict[str, Any]:
        assert self.book is not None
        brightness = self.library.display.brightness
        readable = self.book.readable()
        ring: list[tuple[int, int, TextPage]] = []
        data: dict[str, Any] = {
            "tb": PAGE_TABLES,  # 放在一个键里：内核的数据键最多 64 个，见 research.md §11.22
            "t": self.book.title,
            "tr": [{"n": row} for row in title_rows(self.book.title)],  # 左栏竖排书名每行的字，换书才变；数组只能整个发
            "bv": brightness,
            "lv": (100 - brightness) // 10,
            "ob": self.optics,
            "fs": self.size,
            "fw": WEIGHT_BOLD if self.bold else WEIGHT_NORMAL,
            "i": self.slot,
            "ls": (2 if self.silent else 1) if self.listening or self.listen_held else 0,
            "zs": self.pace,
            "v": self.voice_turns,
            "end": 1 if self._walk(1) is None else 0,
            "hi": (self.slot + AHEAD) % SLOTS,
            "lo": (self.slot - BEHIND) % SLOTS,
            "hd": self.book.chapters[self.chapter].title,
            "bar": BAR_WIDTH * self.book.percent(self._current().offset) // 100,
            "pc": f"{self.book.percent(self._current().offset)}%",
            "cn": bisect_right(readable, self.chapter),
            "cc": len(readable),
            "pn": self.page + 1,
            "pt": len(self._chapter_pages(self.chapter)),
            "ms": self.mark if self.mark_preview is None else self.mark_preview,
            "ds": SLOTS,
            **{k: self.sent.get(k, v) for k, v in MARK_IDLE.items()},
        }
        for k in range(-BEHIND, AHEAD + 1):
            slot = (self.slot + k) % SLOTS
            position = self._walk(k)
            if position is None:
                data[f"s{slot}"] = "全书完" if k > 0 else ""
                continue
            chapter, page = position
            text_page = self._chapter_pages(chapter)[page]
            data[f"s{slot}"] = text_page.text
            ring.append((abs(k), slot, text_page))
        for _, slot, text_page in sorted(ring, key=lambda r: r[0]):  # 段落跨页时标在离当前页最近的一页
            if (fields := self._mark_fields(slot, text_page)) is not None:
                data |= fields
                break
        return data

    def _refresh(self, *, opening: bool = False) -> list[dict[str, Any]]:
        """把环上变了的字段发出去；第一次连同页面一起打开。"""
        data = self._slot_data()
        if opening:
            self._pages += 1
            self.page_uuid = f"reader-{self.id}-{self._pages}"[:128]
            self.sent = dict(data)
            return [self._show("OPEN", data)]
        changed = {k: v for k, v in data.items() if self.sent.get(k) != v}
        self.sent.update(changed)
        if f"s{self.slot}" not in changed:  # 滑动翻页：当前页早就在眼镜上了
            return [self._show("UPDATE", changed)] if changed else []
        # 语音翻页、跳章、换字号：八个槽都换了。先发看得见的这一个，其余的数据多、眼镜要逐个排版，放在后面
        hidden = {k: v for k, v in changed.items() if SLOT_FIELD.fullmatch(k) and int(k[1]) != self.slot}
        visible = {k: v for k, v in changed.items() if k not in hidden}
        return [self._show("UPDATE", part) for part in (visible, hidden) if part]

    def _save(self) -> None:
        assert self.book is not None
        offset = self._current().offset  # view 的偏移是页图版的页首，这里要存文字版这一页的第一个字
        self.synced_ts = self.library.save(replace(self.library.view(self.book, offset, self.saved_size), offset=offset))


class AgentHub:
    """按 sessionId 记住会话和连接代次，断线重连时接着用。"""

    def __init__(self, tools: ReaderTools, library: Library):
        self.tools = tools
        self.library = library
        self.sessions: OrderedDict[str, AgentSession] = OrderedDict()
        library.on_evict.append(text_pages.cache_clear)  # 缓存键是 Book 对象，不清的话挤出书库的书一直留在内存里

    def attach(self, session_id: str, epoch: int) -> tuple[AgentSession | None, str]:
        """返回 (会话, CREATED / RESUMED / STALE / UNKNOWN)。"""
        session = self.sessions.get(session_id)
        if session is None:
            if epoch > 1:
                return None, "UNKNOWN"
            session = AgentSession(session_id, self.tools, self.library)
            session.epoch = epoch
            self.sessions[session_id] = session
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)
            return session, "CREATED"
        if epoch <= session.epoch:
            return session, "STALE"
        session.epoch = epoch
        self.sessions.move_to_end(session_id)
        return session, "RESUMED"

    def drop(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


def _dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


async def _deny(ws: WebSocket, request_id: str) -> None:
    headers = {"X-Request-Id": request_id} if request_id else {}
    try:
        await ws.send_denial_response(JSONResponse({"error": "unauthorized"}, status_code=401, headers=headers))
    except RuntimeError:  # 服务器不支持拒绝时带响应体，退回普通的 403
        await ws.close(1008)


async def serve(ws: WebSocket, hub: AgentHub, token: str, extract_token: Any) -> None:
    """一条 WebSocket 连接的生命周期。``X-Request-Id`` 要原样写回，101 和 401 都要。"""
    request_id = ws.headers.get("x-request-id", "")
    if not token or extract_token(ws.headers.get("authorization")) != token:
        log.warning("拒绝 Agent 连接：Bearer Token 缺失或不匹配")
        await _deny(ws, request_id)
        return
    await ws.accept(headers=[(b"x-request-id", request_id.encode("latin-1"))] if request_id else None)
    lock = anyio.Lock()
    busy = anyio.Lock()  # 听书的计时和收到的消息都会改位置，要排队

    async def send(messages: list[dict[str, Any]]) -> None:
        async with lock:
            for message in messages:
                await ws.send_text(_dumps(message))

    try:
        with anyio.fail_after(INIT_TIMEOUT_SECONDS):
            first = json.loads(await ws.receive_text())
    except (TimeoutError, ValueError, WebSocketDisconnect):
        await _close(ws, 1002)
        return
    payload = first.get("payload") or {}
    session_id = str(first.get("sessionId", ""))
    if first.get("type") != "SESSION_INIT" or first.get("specVersion") != SPEC_VERSION or not session_id:
        await _close(ws, 1002)
        return
    session, mode = hub.attach(session_id, int(payload.get("connectionEpoch", 0)))
    if mode == "STALE":
        await _close(ws, 4002)
        return
    if session is None:
        stranger = AgentSession(session_id, hub.tools, hub.library)
        await send([stranger.error("UNKNOWN_SESSION", "这个会话已经不在了", fatal=True)])
        await _close(ws, 1011)
        return
    previous, session.socket = session.socket, ws
    await send([session.ready(mode)])
    if previous is not None:
        await _close(previous, 4001)

    async def step(work: Any, *args: Any) -> None:
        async with busy:
            try:
                messages = await anyio.to_thread.run_sync(work, *args)
            except Exception:  # 抛到任务组会断开连接，重连后撞上同一个错又断，一直循环
                log.exception("处理出错（会话 %s）", session.id)
                messages = session.failed()
            await send(messages)

    if mode == "CREATED":
        await step(session.start, payload)
    else:
        await step(session.resumed)

    async def beat() -> None:
        while True:
            await anyio.sleep(KEEPALIVE_SECONDS)
            await send(session.keepalive())

    async def narrate() -> None:
        while True:
            await anyio.sleep(LISTEN_TICK_SECONDS)
            if session.listening:
                await step(session.listen_tick)

    async with anyio.create_task_group() as tg:
        tg.start_soon(beat)
        tg.start_soon(narrate)
        try:
            while not session.closing:
                try:
                    message = json.loads(await ws.receive_text())
                except ValueError:
                    await send([session.error("BAD_JSON", "消息不是 JSON")])
                    continue
                await step(session.handle, message)
            await _close(ws, 1000)
            hub.drop(session_id)
        except WebSocketDisconnect as exc:
            if exc.code == 1000:  # 平台正常结束；异常断开要留着等重连
                hub.drop(session_id)
        finally:
            tg.cancel_scope.cancel()


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code)
    except (RuntimeError, WebSocketDisconnect):  # 对方已经断了
        pass
