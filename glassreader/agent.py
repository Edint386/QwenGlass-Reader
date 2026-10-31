"""Agent 自定义接入（文字传输版，docs/agent.md）。

平台用 WebSocket 连 ``/agent``，这里把正文当原生文字推给眼镜上的 ``reader-text`` 页面。不接大模型：
语音只认固定说法（:func:`parse_command`），不回答小说内容。

- 页面有 :data:`SLOTS` 个文字槽，排成一个环：翻页只是本地换显示哪个槽，立即响应，也不怕一两秒的断流。
  每翻一页上报当前槽号，这里记进度，并把环上离得远的槽换成新的邻页（只发变了的字段）。
- 云端单独跑一个实例，和页图版共用进度文件（:class:`~glassreader.library.Library` 负责不互相覆盖），两边接着读。
- 字号、粗细、亮度可调，页面上值变了就发 ``display``，这里重新分页再把环发回去。原生文字调不了行距。
- 听书用平台的文字播报：按句往下发，页面跟着翻。平台不告诉服务读到哪了，位置按语速估。
- 平台约 120 秒没有业务消息就结束会话，而看一页可能不止这么久，所以定时重发一个不变的字段保活；
  用户太久没操作就不再保活，让会话自然结束。
- 所有协议处理都在 :class:`AgentSession` 里，收一条消息、返回要发的消息，不碰网络，方便单测；
  :func:`serve` 只管收发和连接代次。
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import anyio
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from .book import Book
from .library import BRIGHTNESS_LEVELS, Display, Library
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
#: 环上的槽数，和 pages/reader-text/template.xml 一致；当前页前面留 AHEAD 页、后面留 BEHIND 页
SLOTS, AHEAD, BEHIND = 8, 4, 3
#: 原生字号只有固定档位：24 以下有常规字模，28、32 只有粗体
TEXT_SIZES = (16, 18, 20, 22, 24, 28, 32)
DEFAULT_TEXT_SIZE = 24
WEIGHT_NORMAL, WEIGHT_BOLD = 400, 700
#: 粗体用页图版的第几档粗细（0 起），读取时这一档以上算粗体
BOLD_WEIGHT = 4
#: 正文区大小（pages/reader-text），宽度和页图版一样
BODY_WIDTH, BODY_HEIGHT = layout_for(DEFAULT_TEXT_SIZE).text_width, 238
#: 原生字体的行距，模拟器里量的，字号 20 每页 9 行压到进度条的现象和它对得上
LINE_PITCH = {16: 23, 18: 26, 20: 29, 22: 32, 24: 35, 28: 41, 32: 46}
#: 进度条总宽（pages/reader-text）
BAR_WIDTH = 380
#: 不是全角的字按这个比例估宽：估窄了眼镜会多折一行，把最后一行挤出去
NARROW_RATIO = 0.62
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
#: 正在读的段落用它换掉两格缩进。● 在原生字体里正好一个字宽，后面的字不会动，也不会多折一行
LISTEN_MARK = "　●"
_SENTENCE = re.compile(r".+?(?:[。！？!?…]+[”’」』）)]*|$)", re.S)
#: 环上每个槽的字段：正文、章节名、百分比、页码
SLOT_FIELD = re.compile(r"[shrf]\d")
_CHINESE_DIGITS = "零一二三四五六七八九十百千两"
_HELP = "可以说下一页、下一章、跳到第几章、开始听书、换一本某某书，或者退出"
#: 会话里麦克风一直开着：比这长的话多半是在跟别人说话，里面带“下一页”也不算命令。书名号里的不算长度
MAX_COMMAND_CHARS = 14
#: 会跳走的命令要整句匹配，“记录一下当前开头的这个字符”不能把人带回书头
_GO = r"(?:帮我)?(?:跳到|跳转到|翻到|回到|转到|去|到)?"


@dataclass(frozen=True)
class TextPage:
    offset: int
    text: str
    #: 每一行开头的字在全书里的位置
    starts: tuple[int, ...] = ()


class _NativeWidths(dict[str, float]):
    """眼镜原生字体的字宽估算：拿不到字体文件，全角按字号，其余偏宽估，宁可少排一点。"""

    def __init__(self, size: int):
        super().__init__()
        self.size = size

    def __missing__(self, ch: str) -> float:
        if ch in INVISIBLE:
            w = 0.0
        else:
            w = self.size if unicodedata.east_asian_width(ch) in "WFA" else self.size * NARROW_RATIO
        self[ch] = w
        return w


def lines_per_page(size: int) -> int:
    """最后一行放得下字高就行。排多了最后一行被切掉，下一页又接着往后排，这一行就看不到了。"""
    return (BODY_HEIGHT - size) // LINE_PITCH[size] + 1


@lru_cache(maxsize=64)
def text_pages(book: Book, mtime: float, chapter: int, size: int, anchor: int = 0) -> tuple[TextPage, ...]:
    """把一章排成原生文字的页。自己断好行（用和页图版一样的禁则），换行符直接写在文字里，每页行数才是确定的。

    ``anchor`` 处强制换页：换字号后当前页的第一个字还在页首，代价是它前面那一页可能不满。
    """
    widths = _NativeWidths(size)
    per_page = lines_per_page(size)
    lines: list[tuple[int, str]] = []
    for offset, para in book.chapters[chapter].paragraphs:
        cut = anchor - offset
        parts = [(para[:cut], offset, size * 2), (para[cut:], anchor, 0)] if 0 < cut < len(para) else [(para, offset, size * 2)]
        for part, start, indent in parts:
            for line in wrap(part, start, BODY_WIDTH, widths, indent):
                text = "".join(c for c in line.text if c not in INVISIBLE)
                lines.append((line.offset, "　　" + text if line.indent else text))
    before = sum(1 for offset, _ in lines if offset < anchor)
    pages = [part[i:i + per_page] for part in (lines[:before], lines[before:]) for i in range(0, len(part), per_page)]
    if not pages:
        return (TextPage(book.chapters[chapter].start, ""),)
    return tuple(TextPage(p[0][0], "\n".join(text for _, text in p), tuple(o for o, _ in p)) for p in pages)


def parse_command(text: str) -> tuple[str, str]:
    """把一句话解析成 (命令, 参数)。只认固定说法，认不出来返回 ``("help", "")``，不猜。"""
    t = re.sub(r"[，。！？、,.!?\s]", "", text)
    if not t or len(re.sub(r"《.*?》", "", t)) > MAX_COMMAND_CHARS:
        return "help", ""
    if re.fullmatch(r"(退出|结束|关闭|不看了|别看了|结束阅读|退出阅读)(吧|了)?", t):
        return "exit", ""
    if re.fullmatch(r"(?:开始|继续|接着)?(?:听书|朗读)|继续听|接着听|(?:读|念)给我听", t):
        return "listen", ""
    if re.fullmatch(r"(?:暂停|停止)(?:听书|朗读)?|停一下|别(?:读|念)了|不听了", t):
        return "quiet", ""
    if m := re.search(rf"(?:往后|向后)翻([0-9{_CHINESE_DIGITS}]+)页", t):
        return "pages", m.group(1)
    if m := re.search(rf"(?:往前|向前)翻([0-9{_CHINESE_DIGITS}]+)页", t):
        return "pages", "-" + m.group(1)
    if re.search(r"下一页|下页|往后翻|翻页", t):
        return "pages", "1"
    if re.search(r"上一页|上页|往前翻", t):
        return "pages", "-1"
    if re.fullmatch(rf"{_GO}(?:下一[章张]|下章)吧?", t):
        return "chapter", "下一章"
    if re.fullmatch(rf"{_GO}(?:上一[章张]|上章)吧?", t):
        return "chapter", "上一章"
    if m := re.fullmatch(rf"{_GO}(第[0-9{_CHINESE_DIGITS}]+章)吧?", t):
        return "chapter", m.group(1)
    if m := re.fullmatch(rf"{_GO}([0-9]+)(?:%|％)吧?", t):
        return "percent", m.group(1)
    if m := re.fullmatch(rf"{_GO}百分之([0-9{_CHINESE_DIGITS}]+)吧?", t):
        return "percent", m.group(1)
    if re.fullmatch(rf"{_GO}(?:开头|最开头|最前面)(?:吧|去)?|从头(?:开始|看|读)?吧?", t):
        return "percent", "0"
    if re.fullmatch(rf"{_GO}一半吧?", t):
        return "percent", "50"
    if re.search(r"看到哪|读到哪|进度", t):
        return "status", ""
    if re.search(r"书架|有哪些书|有什么书|什么小说|哪些小说", t):
        return "shelf", ""
    if re.search(r"字号?(?:大|放大|大一点)|放大", t):
        return "font", "1"
    if re.search(r"字号?(?:小|缩小|小一点)|缩小", t):
        return "font", "-1"
    if m := re.search(r"《(.+?)》", t):
        return "open", m.group(1)
    if m := re.fullmatch(r"(?:换一本|换成|换本|打开|我想看|我要看|看|读|继续看|继续读)(.+)", t):
        title = m.group(1)
        return "open", "" if re.fullmatch(r"小说|书|小说吧|这本书|眼镜看小说|小说阅读器|文字阅读器|文字阅读|文字版|文字版小说", title) else title
    if re.fullmatch(r"看小说|继续|继续看|继续读|接着看|接着读", t):
        return "open", ""
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
        self.bold = False
        self.anchor = self.anchor_chapter = 0
        self.image_prefix = ""
        self.voice_turns = 0
        self.listening = False
        self.listen_at = self.listen_base = 0  # 下一段的开头；计时起点对应的字
        self.listen_para = -1  # 正在读的段落的开头
        self.listen_start = 0.0
        self.listen_resume = -1
        self.sent: dict[str, Any] = {}
        self.closing = False
        self.socket: Any = None
        self.last_action = time.monotonic()
        self._speech = 0
        self._part = 0
        self._pages = 0

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
        wanted = [p for p in pages if PAGE_KEYWORD in str(p.get("description", ""))]
        self.template_id = next((str(p.get("templateId", "")) for p in wanted or pages), "")
        utterance = str((payload.get("entryContext") or {}).get("rawUtterance") or "")
        command, argument = parse_command(utterance)
        return self._open(argument if command == "open" else "")

    def handle(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        kind, payload = message.get("type"), message.get("payload") or {}
        if kind == "UTTERANCE":
            self.last_action = time.monotonic()
            return self._command(str(payload.get("text", "")))
        if kind == "UI_ACTION":
            self.last_action = time.monotonic()
            return self._action(payload)
        if kind == "ERROR":
            log.warning("平台报错（会话 %s）：%s", self.id, payload)
        elif kind != "TOOL_RESULT":
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
        if name == "turn":
            return self._turn(params.get("slot"))
        if name in ("next_chapter", "prev_chapter"):
            return self._jump(chapter="下一章" if name == "next_chapter" else "上一章")
        if name == "display":
            return self._display(params)
        if name == "listen":
            return self._stop_listening() if self.listening else self._listen()
        if name == "exit":  # 双击两次：页面自己关了，会话要由服务结束，麦克风才会关
            self.closing = True
            return []
        log.info("不认识的页面动作 %r", name)
        return []

    def _turn(self, slot: Any) -> list[dict[str, Any]]:
        if not isinstance(slot, int) or not 0 <= slot < SLOTS:
            return []
        delta = (slot - self.slot) % SLOTS
        if delta > AHEAD:
            delta -= SLOTS
        self.slot = slot
        self.chapter, self.page = self._walk(delta, clamp=True) or (self.chapter, self.page)
        self._save()
        if not self.listening:
            return self._refresh()
        return [*self._stop_listening(), *self._start_listening(self._current().offset)]  # 翻走了就从新的一页读

    # ------------------------------------------------------------------ 语音

    def _command(self, text: str) -> list[dict[str, Any]]:
        command, argument = parse_command(text)
        if self.listening and command == "help":
            return []  # 读着的时候不接别的话，也不能和别的播报交错
        if not self.listening or command in ("font", "listen"):
            return self._run(command, argument, text)
        stopped = self._stop_listening()  # 不能和别的播报交错
        out = self._run(command, argument, text)
        again = command in ("pages", "chapter", "percent")
        return [*stopped, *out, *(self._start_listening(self._current().offset) if again else [])]

    def _run(self, command: str, argument: str, text: str) -> list[dict[str, Any]]:
        if command == "help":  # 听到别的话不接：播报会遮住页面，旁边有人说话时一直响
            return [self.speak(_HELP)] if re.search(r"帮助|怎么用|能说什么", text) else []
        if command == "exit":
            self.closing = True
            return [self.speak("好的，下次接着看")]
        if command == "shelf":
            return [self.speak(self.tools.list_books()["message"])]
        if command == "open":
            return self._open(argument)
        if self.book is None:
            return [self.speak("还没打开书，" + _HELP)]
        if command == "listen":
            return [] if self.listening else self._listen()
        if command == "quiet":
            return []  # _command 已经停了
        if command == "status":
            return [self.speak(self.tools.reading_status(self.book.title)["message"])]
        if command in ("chapter", "percent", "pages"):
            self.voice_turns += 1  # 页面每秒比对，变了就重新倒计时
        if command == "chapter":
            return self._jump(chapter=argument)
        if command == "percent":
            number = _number(argument)
            return self._jump(percent=float(number)) if number is not None else [self.speak(_HELP)]
        if command == "pages":
            sign = -1 if argument.startswith("-") else 1
            number = _number(argument.lstrip("-"))
            if number is None:
                return [self.speak(_HELP)]
            self.chapter, self.page = self._walk(sign * number, clamp=True) or (self.chapter, self.page)
            self._save()
            return self._refresh()
        if command == "font":
            return self._font(int(argument))
        return [self.speak(_HELP)]

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
        offset, size = self.library.position(book)
        self.size = size if size in TEXT_SIZES else min(TEXT_SIZES, key=lambda s: abs(s - size))
        self.saved_size = size  # 页图版的 26、30 这里没有，用户没改就原样存回去
        self.bold = self.library.display.weight >= BOLD_WEIGHT
        self.image_prefix = str(result["u"])
        self._place(offset)
        out = self._refresh(opening=not self.page_uuid)
        return [*out, self.speak(result["message"])]

    def _jump(self, *, chapter: str | None = None, percent: float | None = None) -> list[dict[str, Any]]:
        assert self.book is not None
        self._save()  # library.jump 从已保存的位置算下一章
        v = self.library.jump(self.book, chapter=chapter, percent=percent)
        if v is None:
            return [self.speak(f"没有找到{chapter}")]
        self._place(v.book.chapters[v.chapter].start if chapter else v.offset)
        self._save()
        title = self.book.chapters[self.chapter].title
        return [*self._refresh(), self.speak(f"已跳到{title}")]

    def _font(self, steps: int) -> list[dict[str, Any]]:
        assert self.book is not None
        offset = self._current().offset
        i = TEXT_SIZES.index(self.size)
        self.size = self.saved_size = TEXT_SIZES[min(max(i + steps, 0), len(TEXT_SIZES) - 1)]
        self._place(offset)
        self._save()
        return self._refresh()

    def _display(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """页面发来的显示设置，都是绝对值：连滑时后到的覆盖先到的，不认识的值不用。"""
        assert self.book is not None
        d = self.library.display
        offset, size = self._current().offset, self.size
        if params.get("f") in TEXT_SIZES:
            self.size = self.saved_size = params["f"]
        weight = d.weight
        if params.get("w") in (WEIGHT_NORMAL, WEIGHT_BOLD) and (params["w"] == WEIGHT_BOLD) != self.bold:
            self.bold = params["w"] == WEIGHT_BOLD
            weight = BOLD_WEIGHT if self.bold else DEFAULT_WEIGHT  # 没切换就留着页图版的六档粗细
        brightness = params.get("b") if params.get("b") in BRIGHTNESS_LEVELS else d.brightness
        self.library.set_display(Display(weight, brightness, d.spacing))
        if self.size != size:
            self._place(offset)
        self._save()
        return self._refresh()

    # ------------------------------------------------------------------ 听书

    def _listen(self) -> list[dict[str, Any]]:
        """暂停后还在这一页就接着读，翻走了就从这一页的开头读。"""
        assert self.book is not None
        following = self._walk(1)
        end = self._chapter_pages(following[0])[following[1]].offset if following else self.book.length
        begin = self._current().offset
        return self._start_listening(self.listen_resume if begin <= self.listen_resume < end else begin)

    def _start_listening(self, offset: int) -> list[dict[str, Any]]:
        self.listening = True
        self.listen_at = self.listen_base = offset
        self.listen_start = time.monotonic() + LISTEN_LATENCY_SECONDS
        self._speech += 1
        self._part = 0
        return self.listen_tick()

    def _stop_listening(self) -> list[dict[str, Any]]:
        self.listening = False
        self.listen_resume = self._sentence_start(self._heard() - LISTEN_BACK_CHARS)
        log.info("听书停在 %d，下次从 %d 读（会话 %s）", self._heard(), self.listen_resume, self.id)
        action = {"type": "STOP_OUTPUT", "payload": {"scope": "SPEAK", "reason": "USER_ACTION"}}
        return [self.envelope("OUTPUT", {"actions": [action]}), *self._refresh()]

    def _heard(self) -> int:
        """估计现在读到第几个字，不会超过已经发出去的。"""
        spent = max(time.monotonic() - self.listen_start, 0.0)
        return min(self.listen_base + int(spent * LISTEN_CHARS_PER_SECOND), self.listen_at)

    def listen_tick(self) -> list[dict[str, Any]]:
        """页面跟上估计的位置，快读完时发下一段。"""
        if not self.listening:
            return []
        heard = self._heard()
        out = self._follow(heard + int(LISTEN_PAGE_EARLY_SECONDS * LISTEN_CHARS_PER_SECOND))
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

    def _say_part(self, text: str, *, more: bool) -> dict[str, Any]:
        self._part += 1
        action = {"speechId": f"speech_{self._speech}", "index": self._part, "hasNext": more, "text": text}
        return self.envelope("OUTPUT", {"actions": [{"type": "SPEAK", "payload": action}]})

    def _follow(self, offset: int) -> list[dict[str, Any]]:
        """读到下一页的第一个字就翻过去。"""
        assert self.book is not None
        moved = False
        while (following := self._walk(1)) is not None:
            if self._chapter_pages(following[0])[following[1]].offset > offset:
                break
            self.chapter, self.page = following
            moved = True
        if not moved:
            return []
        self.voice_turns += 1
        self._save()
        return self._refresh()

    def _page_text(self, page: TextPage) -> str:
        """听书时正在读的段落前面有个点；从段落中间接着读时没有缩进，就不标了。"""
        if not self.listening or self.listen_para not in page.starts:
            return page.text
        lines = page.text.split("\n")
        i = page.starts.index(self.listen_para)
        if lines[i].startswith("　　"):
            lines[i] = LISTEN_MARK + lines[i][2:]
        return "\n".join(lines)

    def _passage(self, offset: int) -> tuple[str, int] | None:
        """从 offset 取一个段落（章首带上章节名），返回 (文字, 下一段的开头)，书读完了返回 None。"""
        assert self.book is not None
        for chapter in self.book.chapters[self.book.chapter_at(offset):]:
            if chapter.end <= offset:
                continue
            title = chapter.title + "。\n" if offset <= chapter.start else ""
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
        return text_pages(self.book, self.book.mtime, chapter, self.size, anchor)

    def _current(self) -> TextPage:
        return self._chapter_pages(self.chapter)[self.page]

    def _place(self, offset: int) -> None:
        """让 offset 这个字成为当前页的第一个字。"""
        assert self.book is not None
        self.anchor, self.chapter = offset, self.book.chapter_at(offset)
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
        settings = self.library.page_settings(self.book)
        data: dict[str, Any] = {
            "t": self.book.title,
            "u": self.image_prefix,
            "s": {"b": settings["b"], "o": settings["o"]},
            "fs": self.size,
            "fw": WEIGHT_BOLD if self.bold else WEIGHT_NORMAL,
            "ls": 1 if self.listening else 0,
            "v": self.voice_turns,
            "bar": BAR_WIDTH * self.book.percent(self._current().offset) // 100,
        }
        for k in range(-BEHIND, AHEAD + 1):
            slot = (self.slot + k) % SLOTS
            position = self._walk(k)
            if position is None:
                data[f"s{slot}"], data[f"h{slot}"] = "", "全书完" if k > 0 else ""
                data[f"r{slot}"], data[f"f{slot}"] = "", ""
                continue
            chapter, page = position
            pages = self._chapter_pages(chapter)
            data[f"s{slot}"] = self._page_text(pages[page])
            data[f"h{slot}"] = self.book.chapters[chapter].title
            data[f"r{slot}"] = f"{self.book.percent(pages[page].offset)}%"
            data[f"f{slot}"] = f"{page + 1} / {len(pages)}"
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
        self.library.save(self.library.view(self.book, self._current().offset, self.saved_size))


class AgentHub:
    """按 sessionId 记住会话和连接代次，断线重连时接着用。"""

    def __init__(self, tools: ReaderTools, library: Library):
        self.tools = tools
        self.library = library
        self.sessions: OrderedDict[str, AgentSession] = OrderedDict()

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
    if mode == "CREATED":
        await send(await anyio.to_thread.run_sync(session.start, payload))

    async def step(work: Any, *args: Any) -> None:
        async with busy:
            await send(await anyio.to_thread.run_sync(work, *args))

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
