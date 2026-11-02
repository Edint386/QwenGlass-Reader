"""Agent 文字传输版：协议、环形缓冲、语音命令和页面模板。"""

from __future__ import annotations

import gc
import json
import re
import threading
import time
import weakref
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import websockets
from starlette.testclient import TestClient, WebSocketDenialResponse

from glassreader import agent
from glassreader.agent import (
    AGENT_PATH,
    AHEAD,
    BAR_WIDTH,
    BEHIND,
    BODY_WIDTH,
    LABEL_WIDTH,
    LINE_PITCH,
    SLOTS,
    TEXT_SIZES,
    AgentHub,
    AgentSession,
    _NativeWidths,
    _number,
    lines_per_page,
    parse_command,
    text_pages,
)
from glassreader.book import Book
from glassreader.config import Settings
from glassreader.library import BRIGHTNESS_LEVELS, DEFAULT_PACE, MAX_BOOKS, OPTICS_LEVELS, PACES, Display, Library
from glassreader.mcp_tools import ReaderTools
from glassreader.pager import NO_LINE_START
from glassreader.server import create_app

from .conftest import make_novel

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "pages" / "reader-text" / "template.xml"
DATA = ROOT / "pages" / "reader-text" / "data.json"
A = "{waft://dsl}"
PAGES = [{"templateId": "tpl_1", "description": "reader-text 看小说的文字页", "dataSchema": {}}]


def init(session: str = "ses_1", epoch: int = 1, **payload: Any) -> dict[str, Any]:
    entry = {"source": "VOICE_SKILL", "rawUtterance": "打开《测试长篇》"}
    body = {"connectionEpoch": epoch, "inputMode": "ASR_TEXT", "pages": PAGES, "entryContext": entry, **payload}
    return {"specVersion": "1.0", "sessionId": session, "type": "SESSION_INIT", "payload": body}


def actions(messages: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [a["payload"] for m in messages if m["type"] == "OUTPUT" for a in m["payload"]["actions"] if a["type"] == kind]


def spoken(messages: list[dict[str, Any]]) -> str:
    return "".join(a["text"] for a in actions(messages, "SPEAK"))


def shown(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a["view"]["props"]["data"] for a in actions(messages, "SHOW_UI")]


@pytest.fixture
def session(settings: Settings, library: Library) -> AgentSession:
    s = AgentSession("ses_1", ReaderTools(settings, library), library)
    s.epoch = 1
    return s


@pytest.fixture
def opened(session: AgentSession) -> AgentSession:
    session.start(init()["payload"])
    return session


def turn(s: AgentSession, slot: int) -> list[dict[str, Any]]:
    action = {"pageUuid": s.page_uuid, "action": {"name": "turn", "params": {"slot": slot}}}
    return s.handle({"type": "UI_ACTION", "payload": action})


def say(s: AgentSession, text: str) -> list[dict[str, Any]]:
    return s.handle({"type": "UTTERANCE", "payload": {"text": text}})


# ---------------------------------------------------------------------- 排版


@pytest.mark.parametrize("size", TEXT_SIZES)
def test_text_pages_fit_and_keep_the_text(library: Library, size: int) -> None:
    book = library.book(library.find("测试长篇")[0].id)
    assert book is not None
    chapter = next(i for i, c in enumerate(book.chapters) if c.paragraphs)
    pages = text_pages(book, book.mtime, chapter, size)
    widths = _NativeWidths(size)
    for page in pages:
        lines = page.text.split("\n")
        assert len(lines) <= lines_per_page(size)
        assert all(sum(widths[c] for c in line) <= LABEL_WIDTH for line in lines)  # 超出 label 的字会被切掉
    joined = "".join(p.text for p in pages).replace("\n", "").replace("　", "")
    assert joined == "".join(para for _, para in book.chapters[chapter].paragraphs).replace("　", "")


@pytest.mark.parametrize("size", TEXT_SIZES)
@pytest.mark.parametrize("marks", ["。”", "！”", "……", "——", "？"])
def test_hanging_punctuation_stays_inside_the_label(size: int, marks: str) -> None:
    text = "\n".join("　　" + "字" * n + marks + "后面的话。" for n in range(1, 60))
    book = Book.parse(text, title="标点")
    widths = _NativeWidths(size)
    pages = text_pages(book, book.mtime, 0, size)
    lines = [line for page in pages for line in page.text.split("\n")]
    assert max(sum(widths[c] for c in line) for line in lines) <= LABEL_WIDTH
    assert "".join(lines).replace("　", "") == "".join(para for _, para in book.chapters[0].paragraphs).replace("　", "")
    assert not any(line[0] in NO_LINE_START for line in lines)


@pytest.mark.parametrize(("size", "lines"), [(16, 10), (18, 9), (20, 8), (22, 7), (24, 7), (28, 6), (32, 5)])
def test_every_line_fits_above_the_progress_bar(size: int, lines: int) -> None:
    assert lines_per_page(size) == lines  # 模拟器里这些行数全部放得下，再多一行就被切掉


def test_every_size_has_a_measured_line_pitch() -> None:
    assert tuple(LINE_PITCH) == TEXT_SIZES


def test_narrow_characters_are_estimated_wider_than_half() -> None:
    widths = _NativeWidths(20)
    assert widths["字"] == 20 and widths["a"] > 10


@pytest.mark.parametrize(("text", "expected"), [
    ("退出", ("exit", "")),
    ("不看了", ("exit", "")),
    ("退出吧", ("exit", "")),
    ("退出阅读", ("exit", "")),
    ("结束阅读", ("exit", "")),
    ("下一页", ("pages", "1")),
    ("上一页。", ("pages", "-1")),
    ("往后翻五页", ("pages", "五")),
    ("往前翻3页", ("pages", "-3")),
    ("往前翻页", ("pages", "-1")),  # “翻页”不能抢在“往前”前面
    ("向前翻页", ("pages", "-1")),
    ("向前翻", ("pages", "-1")),
    ("向后翻", ("pages", "1")),
    ("翻页", ("pages", "1")),
    ("帮我翻到上一页吧", ("pages", "-1")),
    ("下一章", ("chapter", "下一章")),
    ("上一章", ("chapter", "上一章")),
    ("跳到第三章", ("chapter", "第三章")),
    ("跳到第12章", ("chapter", "第12章")),
    ("跳到第十二回", ("chapter", "第十二回")),
    ("跳到第三节", ("chapter", "第三节")),
    ("看第三章", ("chapter", "第三章")),
    ("读第三章吧", ("chapter", "第三章")),
    ("打开第三章", ("chapter", "第三章")),
    ("继续看第三章", ("chapter", "第三章")),
    ("看下一章", ("chapter", "下一章")),
    ("继续看下一章", ("chapter", "下一章")),
    ("读上一章", ("chapter", "上一章")),
    ("跳到百分之五十", ("percent", "五十")),
    ("跳到50%", ("percent", "50")),
    ("跳到33.3%", ("percent", "33")),  # 小数点不能丢，33.3 不是 333
    ("跳到百分之33.5", ("percent", "33")),
    ("跳到50％。", ("percent", "50")),
    ("回到开头", ("percent", "0")),
    ("跳到一半", ("percent", "50")),
    ("我看到哪了", ("status", "")),
    ("读到哪了", ("status", "")),
    ("我上次看到哪里了", ("status", "")),
    ("进度", ("status", "")),
    ("现在进度怎么样", ("status", "")),
    ("书架上有什么书", ("shelf", "")),
    ("书架", ("shelf", "")),
    ("有哪些书", ("shelf", "")),
    ("有什么小说", ("shelf", "")),
    ("字大一点", ("font", "1")),
    ("字号缩小", ("font", "-1")),
    ("把字放大一点", ("font", "1")),
    ("字小点吧", ("font", "-1")),
    ("打开《雾港来信》", ("open", "雾港来信")),
    ("换一本雾港来信", ("open", "雾港来信")),
    ("看小说", ("open", "")),
    ("继续看", ("open", "")),
    *[(text, ("shelf", "")) for text in ("换本书", "换一本书", "换一本", "换本", "换本书看", "换一本书看", "换一本小说", "换本书吧")],
    ("换本杂志", ("open", "杂志")),  # 这种要看书架上有没有，_command 再看
    ("打开文字阅读器", ("open", "")),
    ("我要看小说", ("open", "")),
    ("看《雾港来信》", ("open", "雾港来信")),
    ("继续读《雾港来信》吧", ("open", "雾港来信")),
    ("《雾港来信》", ("open", "雾港来信")),
    ("打开雾港来信", ("open", "雾港来信")),
    ("我想看雾港来信", ("open", "雾港来信")),
    *[(text, ("chapter", "第三章")) for text in ("我想看第三章", "我要看第三章", "继续读第三章", "接着看第三章", "看看第三章", "念第三章")],
    *[(text, ("chapter", "下一章")) for text in (
        "继续读下一章", "我想看下一章", "我要看下一章", "继续下一章", "接着看下一章", "看看下一章", "念下一章",
    )],
    *[(text, ("pages", "1")) for text in ("看下一页", "读下一页", "继续下一页", "继续看下一页", "看下一页吧")],
    *[(text, ("pages", "-1")) for text in ("看上一页", "读上一页", "继续看上一页")],
    *[(text, ("shelf", "")) for text in ("打开书架", "我的书架", "看书架", "我的书架上有什么书")],
    *[(text, ("font", "1")) for text in ("放大字体", "放大字号", "字体放大", "帮我放大字体", "字大一些", "字大些", "字体大一点", "把字体放大吧")],
    *[(text, ("font", "-1")) for text in ("缩小字体", "缩小字号", "字体缩小", "帮我缩小字体", "字小一些", "字小些")],
    *[(text, ("status", "")) for text in (
        "现在读到哪了", "现在看到哪了", "我现在看到哪了", "当前进度", "进度是多少", "我的进度", "当前进度是多少", "当前读到哪里了",
    )],
    ("跳到百分之三十三点三", ("percent", "三十三")),  # 只取整数部分，和 33.3% 一样
    ("翻到百分之五十点五吧", ("percent", "五十")),
    *[(text, ("help", "")) for text in (  # 这轮加的说法周围的日常话
        "看电影", "看看电影", "念书", "继续开会", "看下一站", "读下一句", "继续下一步",
        "看书架子", "我的书包", "字太小了", "放大一点", "缩小一点", "声音大一点", "字体大小", "现在几点", "当前位置",
        "我的电脑", "你现在看到哪了", "百分之三十三点三", "三十三点三",
    )],
    ("今天天气怎么样", ("help", "")),
    ("", ("help", "")),
    ("开始听书", ("listen", "")),
    ("继续听", ("listen", "")),
    ("读给我听", ("listen", "")),
    ("暂停", ("quiet", "")),
    ("别读了", ("quiet", "")),
    ("暂停一下", ("quiet", "")),
    ("暂停吧", ("quiet", "")),
    ("先暂停", ("quiet", "")),
    ("暂停朗读", ("quiet", "")),
    ("别说了", ("quiet", "")),
    ("不读了", ("quiet", "")),
    ("不听了", ("quiet", "")),
    ("我不想听书", ("help", "")),
    ("智能翻页", ("smart", "")),
    ("开始智能翻页", ("smart", "")),
    ("打开智能翻页", ("smart", "")),
    ("停止智能翻页", ("quiet", "")),
    ("关闭智能翻页", ("quiet", "")),
    *[(text, ("quiet", "")) for text in (  # 听书和智能翻页都这样停
        "暂停智能翻页", "关掉智能翻页", "退出智能翻页", "智能翻页暂停", "智能翻页关掉", "智能翻页停一下", "智能翻页暂停一下",
        "关闭听书", "关闭朗读", "关掉听书", "退出听书", "听书暂停", "暂停听书吧", "停止朗读", "停下", "停下来", "停下吧", "停一下吧",
        "别翻了", "别听了", "别念了", "不要读了", "不要翻了",
    )],
    *[(text, ("smart", "")) for text in ("开启智能翻页", "开智能翻页", "继续智能翻页", "接着智能翻页", "恢复智能翻页", "智能翻页吧")],
    *[(text, ("listen", "")) for text in ("继续听吧", "接着听吧", "继续听书吧", "恢复听书", "继续播放", "接着播放吧")],
    *[(text, ("open", "")) for text in ("继续吧", "继续看吧", "继续读吧", "接着看吧", "接着读吧")],
    *[(text, ("help", "")) for text in (  # 和停止、继续沾边的日常话
        "停", "停车", "停一下车", "暂停营业", "暂停键", "别翻", "别翻书", "别听他的", "关掉", "关闭智能手机", "退出智能",
        "继续走", "继续说", "接着说", "继续努力", "继续开会", "继续播放视频", "开智能手机",
    )],
    ("这个智能翻页不错", ("help", "")),
    ("从头开始看", ("percent", "0")),
    ("从头读", ("percent", "0")),
    ("跳到下一章", ("chapter", "下一章")),
    ("上一张", ("chapter", "上一章")),
    ("到下一章", ("chapter", "下一章")),
    ("回到一半", ("percent", "50")),
    ("记录一下当前开头的这个字符", ("help", "")),  # 2026-11-01 真机：对别人说的话把进度带回了书头
    ("这个开头写得好", ("help", "")),
    ("一半一半吧", ("help", "")),
    ("第三章写得不错", ("help", "")),
    ("百分之五十的人", ("help", "")),
    ("等下一章再说", ("help", "")),
    *[(text, ("help", "")) for text in (  # 跳得远的命令动词不能省
        "一半", "一半吧", "到一半", "开头", "开头吧", "最前面", "从头", "从头吧", "从头开始吧", "从头看吧",
        "第三章", "第五章吧", "到第一章", "去第一章吧", "百分之二十吧", "到百分之三十吧", "看到下一章", "33.3%",
    )],
    *[(text, ("help", "")) for text in (  # 问进度和书架要整句
        "项目进度怎么样", "进度条", "看到哪算哪", "你看到哪了", "买个书架", "书架太高了", "这有什么小说可看的",
    )],
    *[(text, ("help", "")) for text in (  # 这些日常短句不能退出
        "结束", "结束了", "结束吧", "关闭", "关闭了", "关闭吧", "退出了", "别看了",
    )],
    *[(text, ("help", "")) for text in (  # 单独的看、读不算打开，单字不算书名，书名号要在命令里
        "看这个", "看我", "看一下", "看看", "看来", "看书", "读书", "读完了", "看他那样子", "读到这里吧", "看着远方",
        "看电视", "看那边", "打开灯", "换一本吧", "你看过《鬼吹灯》吗", "我喜欢《鬼吹灯》",
    )],
    *[(text, ("help", "")) for text in (  # 翻页要整句
        "你往后翻三十页看看", "他往前翻二十页找到的", "我往后翻五页就看到了", "记下页码", "下页再说", "线上页面",
        "网上页面打不开", "翻页笔",
    )],
    *[(text, ("help", "")) for text in (  # 字号要整句，单说放大也不算
        "放大", "缩小", "把声音放大", "图片放大", "地图缩小一下", "差距缩小了", "名字大", "数字小一点", "文字大小",
        "字号大小", "年纪大了字小看不清",
    )],
    *[(text, ("help", "")) for text in (  # 听书、暂停、智能翻页也要整句
        "听书软件", "你在听书吗", "读给他听", "暂停营业", "暂停键", "停一下车", "智能翻页笔",
    )],
])
def test_parse_command(text: str, expected: tuple[str, str]) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize(("text", "value"), [("12", 12), ("三", 3), ("十", 10), ("二十", 20), ("十五", 15),
                                             ("三十五", 35), ("两", 2), ("一百二十", None), ("三四十", None)])
def test_number(text: str, value: int | None) -> None:
    assert _number(text) == value


# ---------------------------------------------------------------------- 会话


def test_opening_shows_the_page_and_speaks(session: AgentSession) -> None:
    out = session.start(init()["payload"])
    (show,) = actions(out, "SHOW_UI")
    assert show["display"] == {"mode": "OPEN"} and show["view"]["props"]["templateId"] == "tpl_1"
    data = show["view"]["props"]["data"]
    assert data["t"] == session.book.title and data["fs"] in TEXT_SIZES
    assert {f"s{i}" for i in range(SLOTS)} <= set(data) and {f"f{i}" for i in range(SLOTS)} <= set(data) and data["hd"]
    assert data["s0"]  # 当前页在 0 号槽
    assert "已打开" in spoken(out)


def test_no_page_published_is_spoken_not_crashed(session: AgentSession) -> None:
    out = session.start(init(pages=[])["payload"])
    assert "还没发布" in spoken(out) and not actions(out, "SHOW_UI")


def test_first_utterance_can_name_the_book(session: AgentSession) -> None:
    out = session.start(init()["payload"])
    assert session.book.title == "测试长篇" and "测试长篇" in spoken(out)


def test_no_book_named_and_nothing_read_yet_asks_which(session: AgentSession) -> None:
    out = session.start(init(entryContext={"rawUtterance": "看小说"})["payload"])
    assert session.book is None and "想看哪一本" in spoken(out) and not actions(out, "SHOW_UI")
    out = say(session, "打开《测试长篇》")
    assert session.book.title == "测试长篇" and actions(out, "SHOW_UI")


@pytest.mark.parametrize("text", ["下一页", "下一章", "跳到第三章", "跳到百分之五十", "字大一点", "我看到哪了", "开始听书", "智能翻页"])
def test_commands_before_a_book_is_open_ask_for_one(session: AgentSession, text: str) -> None:
    session.start(init(entryContext={"rawUtterance": "看小说"})["payload"])
    assert session.book is None and spoken(say(session, text)).startswith("还没打开书")


@pytest.mark.parametrize("text", ["用文字阅读器打开《雾港来信》", "文字阅读器，我要看《雾港来信》。"])
def test_first_utterance_can_name_the_book_inside_a_sentence(session: AgentSession, text: str) -> None:
    session.start(init(entryContext={"rawUtterance": text})["payload"])
    assert session.book.title == "雾港来信"


@pytest.mark.parametrize("text", ["打开文字阅读器", "文字阅读器", "用文字版看小说"])
def test_entry_phrases_open_the_latest_book(opened: AgentSession, text: str) -> None:
    reopened = AgentSession("s2", opened.tools, opened.library)
    reopened.start(init(session="s2", entryContext={"rawUtterance": text})["payload"])
    assert reopened.book.title == opened.book.title


@pytest.mark.parametrize("text", ["打开文字阅读器吧", "打开我的文字阅读器", "打开那个文字阅读器", "打开文字阅读器看小说"])
def test_entry_phrases_that_vary_still_open_the_page(opened: AgentSession, text: str) -> None:
    reopened = AgentSession("s2", opened.tools, opened.library)
    out = reopened.start(init(session="s2", entryContext={"rawUtterance": text})["payload"])
    assert reopened.book.title == opened.book.title and actions(out, "SHOW_UI") and "没有找到" not in spoken(out)


def test_entry_phrase_that_varies_with_nothing_read_yet_asks_which(session: AgentSession) -> None:
    out = session.start(init(entryContext={"rawUtterance": "打开那个文字阅读器"})["payload"])
    assert session.book is None and "想看哪一本" in spoken(out)


def test_a_book_named_at_entry_that_is_missing_is_answered(opened: AgentSession) -> None:
    reopened = AgentSession("s2", opened.tools, opened.library)
    out = reopened.start(init(session="s2", entryContext={"rawUtterance": "打开《不存在的书》"})["payload"])
    assert "没有找到" in spoken(out) and reopened.book is None


def test_turn_sends_only_the_slots_that_changed(opened: AgentSession) -> None:
    before = opened._current().offset
    (update,) = shown(turn(opened, 1))
    assert opened._current().offset > before
    assert set(update) <= {"bar", "pc", "hi", "lo", "hd", "s"} | {f"{k}{i}" for k in "sf" for i in range(SLOTS)}  # 没有重发书名和字号
    assert 0 < len(update) <= 2 * SLOTS + 6 and opened.sent["s1"] == opened._current().text


def test_progress_bar_follows_the_current_page(opened: AgentSession) -> None:
    assert opened.book is not None
    (update, *_) = shown(opened._jump(percent=50))
    assert update["bar"] == BAR_WIDTH * opened.book.percent(opened._current().offset) // 100 > 0


def test_ring_wraps_around_and_goes_back(opened: AgentSession) -> None:
    for slot in [*range(1, SLOTS), 0]:  # 走满一圈
        turn(opened, slot)
    far = opened._current()
    turn(opened, SLOTS - 1)  # 后滑一页：0 → 7
    assert opened._current().offset < far.offset
    turn(opened, 0)
    assert opened._current() == far


def test_ring_neighbours_match_the_book(opened: AgentSession) -> None:
    turn(opened, 1)
    expected = {(1 + k) % SLOTS: opened._walk(k) for k in range(-BEHIND, AHEAD + 1)}
    for slot, position in expected.items():
        if position is not None:
            assert opened.sent[f"s{slot}"] == opened._chapter_pages(position[0])[position[1]].text


def test_lost_turn_is_caught_up_by_the_next_slot(opened: AgentSession) -> None:
    start = opened._current()
    turn(opened, 3)  # 1、2 两条上报丢了
    assert opened._walk(-3) == (opened.book.chapter_at(start.offset), 0)


def test_turning_saves_progress(opened: AgentSession) -> None:
    turn(opened, 1)
    turn(opened, 2)
    assert opened.library.position(opened.book)[0] > 0


def test_old_page_action_is_ignored(opened: AgentSession) -> None:
    before = opened.page, opened.chapter
    action = {"pageUuid": "old", "action": {"name": "turn", "params": {"slot": 1}}}
    assert opened.handle({"type": "UI_ACTION", "payload": action}) == []
    assert (opened.page, opened.chapter) == before


@pytest.mark.parametrize("message", [{"type": "ERROR", "payload": {"code": "X"}}, {"type": "TOOL_RESULT"}, {"type": "以后才有"}, {}])
def test_other_platform_messages_get_no_answer(opened: AgentSession, message: dict[str, Any]) -> None:
    assert opened.handle(message) == []


@pytest.mark.parametrize("slot", ["1", 8, -1, None])
def test_bad_slot_is_ignored(opened: AgentSession, slot: Any) -> None:
    assert turn(opened, slot) == []


def test_a_swipe_sends_about_one_slot(opened: AgentSession, books_dir: Path) -> None:
    (books_dir / "长章.txt").write_text("第一章 甲\n" + "\n".join(["风从海上吹来。" * 100] * 200), encoding="utf-8")
    say(opened, "打开《长章》")
    turn(opened, 1)
    (update,) = shown(turn(opened, 2))
    assert sorted(k for k in update if agent.SLOT_FIELD.fullmatch(k)) == ["f6", "s6"]  # 只换环上最远的一格
    assert "hd" not in update and not set(update) & set(agent.PAGE_TABLES)  # 章节没变，表只在打开时发


def test_the_chapter_name_goes_out_when_the_chapter_changes(opened: AgentSession) -> None:
    title = opened.sent["hd"]
    while opened.sent["hd"] == title:
        (update,) = shown(turn(opened, (opened.slot + 1) % SLOTS))
    assert update["hd"] == opened.book.chapters[opened.chapter].title


def test_book_end_stops_and_says_so(opened: AgentSession) -> None:
    say(opened, "跳到百分之一百")
    last = opened._current()
    assert "全书完" in opened.sent.values()
    turn(opened, (opened.slot + 1) % SLOTS)
    assert opened._current() == last  # 走到头了，槽号往前但页不动
    assert opened.sent[f"s{opened.slot}"] == last.text


def test_a_title_only_last_chapter_has_no_page_to_land_on(opened: AgentSession, books_dir: Path, clock: list[float]) -> None:
    (books_dir / "书尾空章.txt").write_text(make_novel(chapters=3, empty=(3,)), encoding="utf-8")
    say(opened, "打开《书尾空章》")
    assert opened.book is not None
    assert spoken(say(opened, "跳到百分之一百")).startswith("已跳到第二章")
    assert opened.chapter == opened.book.readable()[-1] and opened._current().text and opened._walk(1) is None
    assert "全书读完了" not in spoken(say(opened, "开始听书"))


def test_voice_commands(opened: AgentSession) -> None:
    chapter = opened.chapter
    assert "已跳到" in spoken(say(opened, "下一章")) and opened.chapter > chapter
    assert opened.page == 0
    assert "已跳到" in spoken(say(opened, "上一章")) and opened.chapter == chapter
    assert "读到" in spoken(say(opened, "我看到哪了"))
    assert "书架" in spoken(say(opened, "书架上有什么书"))
    assert say(opened, "今天天气怎么样") == []  # 麦克风一直开着，别的话不接
    assert "下一页" in spoken(say(opened, "能说什么"))
    assert "没有找到" in spoken(say(opened, "跳到第九百九十九章"))


def test_next_chapter_in_the_last_chapter_stays_put(opened: AgentSession) -> None:
    assert opened.book is not None
    say(opened, "跳到百分之九十九")
    offset = opened._current().offset
    assert opened.page > 0 and spoken(say(opened, "下一章")) == "已经是最后一章"
    assert opened._current().offset == offset and opened.library.position(opened.book)[0] == offset


def test_prev_chapter_in_the_first_chapter_stays_put(opened: AgentSession) -> None:
    say(opened, "回到开头")
    assert spoken(say(opened, "上一章")) == "已经是第一章" and opened._current().offset == 0


@pytest.mark.parametrize("text", ["开头", "一半吧", "从头开始吧", "第一章吧", "百分之二十吧"])
def test_short_talk_does_not_move_the_reader(opened: AgentSession, text: str) -> None:
    opened._jump(percent=30)
    assert opened.book is not None
    saved = opened.library.position(opened.book)
    assert say(opened, text) == [] and opened.library.position(opened.book) == saved


@pytest.mark.parametrize("text", ["跳到33.3%", "跳到百分之33.5"])
def test_decimal_percent_stays_where_it_says(opened: AgentSession, text: str) -> None:
    assert opened.book is not None
    say(opened, text)
    assert opened.book.percent(opened._current().offset) <= 33


@pytest.mark.parametrize("text", ["跳到150%", "跳到百分之一百五十", "往后翻一百二十页", "往后翻三四十页"])
def test_numbers_out_of_range_only_get_the_help(opened: AgentSession, text: str) -> None:
    assert opened.book is not None
    saved = opened.library.position(opened.book)
    assert "可以说" in spoken(say(opened, text)) and opened.library.position(opened.book) == saved


@pytest.mark.parametrize("text", ["看第二章", "打开第二章", "读第2回", "我想看第二章", "继续读第二章"])
def test_chapter_with_a_reading_verb_jumps(opened: AgentSession, text: str) -> None:
    assert opened.book is not None
    assert "已跳到" in spoken(say(opened, text))
    assert opened.book.chapters[opened.chapter].title.startswith("第二章")


@pytest.mark.parametrize("text", ["帮助", "怎么用", "能说什么？", "能说什么。"])
def test_asking_for_help_is_answered(opened: AgentSession, text: str) -> None:
    assert "下一页" in spoken(say(opened, text))


@pytest.mark.parametrize("text", ["我帮助他做完了作业", "这东西到底怎么用", "你不知道他当时到底能说什么才能把这件事圆过去"])
def test_sentences_with_help_words_get_no_answer(opened: AgentSession, text: str) -> None:
    assert say(opened, text) == []


def test_percent_with_a_spoken_decimal_jumps_like_the_digits(opened: AgentSession) -> None:
    assert opened.book is not None
    say(opened, "跳到百分之三十三点三")
    offset = opened._current().offset
    say(opened, "跳到33.3%")
    assert opened._current().offset == offset and 0 < opened.book.percent(offset) <= 33


def test_voice_pages(opened: AgentSession) -> None:
    start = opened._current().offset
    say(opened, "往后翻三页")
    assert opened._walk(-3) is not None and opened._current().offset > start


@pytest.fixture
def shelf(opened: AgentSession, books_dir: Path) -> AgentSession:
    """书架上有名字容易碰上的书。"""
    for title in ("鬼吹灯", "这个杀手不太冷", "我的团长我的团"):
        (books_dir / f"{title}.txt").write_text(make_novel(), encoding="utf-8")
    opened.library.scan()
    return opened


CHATTER = ["打开灯", "打开空调", "打开窗户", "换成微信", "看这个", "看我", "看来", "看一下", "读完了",
           "我想看电影", "我要看比赛", "继续看电影", "打开书包",
           "你看过《鬼吹灯》吗", "我喜欢《鬼吹灯》", "打开吹灯"]


@pytest.mark.parametrize("text", CHATTER)
def test_talk_does_not_open_a_book_or_answer(shelf: AgentSession, text: str) -> None:
    latest = shelf.library.recent()
    assert say(shelf, text) == []
    assert shelf.book.title == "测试长篇" and shelf.library.recent() == latest


@pytest.mark.parametrize("text", CHATTER)
def test_talk_does_not_stop_the_reading(shelf: AgentSession, clock: list[float], text: str) -> None:
    say(shelf, "开始听书")
    assert say(shelf, text) == [] and shelf.listening


@pytest.mark.parametrize(("text", "title"), [
    ("打开鬼吹灯", "鬼吹灯"),
    ("打开《鬼吹灯》", "鬼吹灯"),
    ("打开鬼吹", "鬼吹灯"),  # 书名开头够了
    ("换成我的团长", "我的团长我的团"),
    ("换一本杀手", "这个杀手不太冷"),  # 说了换一本，书名中间的字也算
])
def test_a_book_on_the_shelf_opens_without_quotes(shelf: AgentSession, text: str, title: str) -> None:
    assert "已打开" in spoken(say(shelf, text)) and shelf.book.title == title


@pytest.mark.parametrize("text", ["打开《你好，旧时光》", "打开你好旧时光", "打开你好，旧时光", "打开《你好旧时光》", "换一本你好"])
def test_a_title_with_punctuation_opens_in_the_session(opened: AgentSession, books_dir: Path, text: str) -> None:
    (books_dir / "你好，旧时光.txt").write_text(make_novel(), encoding="utf-8")
    opened.library.scan()
    assert "已打开" in spoken(say(opened, text)) and opened.book.title == "你好，旧时光"


@pytest.mark.parametrize("text", ["换本书", "换一本书", "换一本", "换本书看"])
@pytest.mark.parametrize("before", ["暂停", ""])
def test_changing_book_without_a_title_lists_the_shelf(opened: AgentSession, clock: list[float], text: str, before: str) -> None:
    if before:
        say(opened, "开始听书")
        say(opened, before)
    offset = opened._current().offset
    out = say(opened, text)
    assert spoken(out).startswith("书架上有") and not opened.listening
    assert opened.book.title == "测试长篇" and opened._current().offset == offset


def test_changing_book_without_a_title_stops_the_voice_to_answer(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    out = say(opened, "换本书")
    assert actions(out, "STOP_OUTPUT") and spoken(out).startswith("书架上有") and not opened.listening


def test_a_book_added_during_the_session_opens(opened: AgentSession, books_dir: Path) -> None:
    (books_dir / "新到的书.txt").write_text(make_novel(), encoding="utf-8")
    say(opened, "打开新到的书")
    assert opened.book.title == "新到的书"


def test_a_book_added_during_the_session_opens_past_a_title_with_it_inside(opened: AgentSession, books_dir: Path) -> None:
    (books_dir / "地球三体往事.txt").write_text(make_novel(), encoding="utf-8")
    say(opened, "打开《地球三体往事》")
    (books_dir / "三体.txt").write_text(make_novel(), encoding="utf-8")
    say(opened, "打开三体")
    assert opened.book.title == "三体"


@pytest.mark.parametrize("text", ["打开《不存在的书》", "换一本不存在的书", "换本不存在的书"])
def test_asking_for_a_missing_book_is_answered(opened: AgentSession, text: str) -> None:
    assert "没有找到" in spoken(say(opened, text)) and opened.book.title == "测试长篇"


@pytest.mark.parametrize(("field", "bad"), [("offset", "abc"), ("offset", None), ("updated_ts", "昨天")])
def test_a_bad_record_does_not_stop_the_book_opening(opened: AgentSession, field: str, bad: Any) -> None:
    library = opened.library
    assert opened.book is not None
    data = json.loads(library.state_file.read_text(encoding="utf-8"))
    data[opened.book.id][field] = bad
    library.state_file.write_text(json.dumps(data), encoding="utf-8")
    for text in ("打开《测试长篇》", "打开文字阅读器"):
        reopened = AgentSession("s2", opened.tools, library)
        assert "已打开" in spoken(reopened.start(init(session="s2", entryContext={"rawUtterance": text})["payload"]))


def test_a_book_that_cannot_be_read_is_answered(opened: AgentSession, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_: Any, **__: Any) -> None:
        raise PermissionError(13, "没有权限")

    monkeypatch.setattr(Book, "load", denied)
    assert spoken(say(opened, "打开《雾港来信》")) == "《雾港来信》的文件打不开了"
    assert opened.book.title == "测试长篇"


@pytest.mark.parametrize("content", ["", "\n\n  \n", "第一章 开始\n第二章 结束\n"])
def test_a_book_with_no_text_opens_and_ends_at_once(opened: AgentSession, books_dir: Path, content: str) -> None:
    (books_dir / "空书.txt").write_text(content, encoding="utf-8")
    assert "已打开" in spoken(say(opened, "打开《空书》"))
    for text in ("下一页", "上一页", "下一章", "跳到百分之五十", "字大一点", "我看到哪了"):
        say(opened, text)  # 不崩就行
    assert spoken(say(opened, "开始听书")) == "全书读完了" and not opened.listening
    assert spoken(say(opened, "智能翻页")) == "全书读完了" and not opened.listening
    assert "已打开" in spoken(say(opened, "打开《测试长篇》"))


def test_zero_width_characters_never_reach_the_glasses(opened: AgentSession, books_dir: Path) -> None:
    (books_dir / "零宽.txt").write_text("第一章 开始\n　　前面\u200b后面\ufeff结尾。\n", encoding="utf-8")
    say(opened, "打开《零宽》")
    assert opened._current().text == "　　前面后面结尾。"  # 原生字体里是方块，也不能占排版的宽度
    assert "前面后面结尾。" in actions(say(opened, "开始听书"), "SPEAK")[0]["text"]


def test_turning_back_by_voice_goes_back(opened: AgentSession) -> None:
    say(opened, "往后翻三页")
    offset = opened._current().offset
    say(opened, "往前翻页")
    assert opened._current().offset < offset


def test_talk_about_pages_and_sizes_changes_nothing(opened: AgentSession) -> None:
    offset, size = opened._current().offset, opened.size
    for text in ("你往后翻三十页看看", "记下页码", "线上页面", "把声音放大", "年纪大了字小看不清"):
        assert say(opened, text) == []
    assert (opened._current().offset, opened.size) == (offset, size)


def test_menu_actions_change_chapter_and_font(opened: AgentSession) -> None:
    def act(name: str) -> list[dict[str, Any]]:
        action = {"pageUuid": opened.page_uuid, "action": {"name": name, "params": {"slot": opened.slot}}}
        return opened.handle({"type": "UI_ACTION", "payload": action})

    chapter = opened.chapter
    act("next_chapter")
    assert opened.chapter > chapter


def display(s: AgentSession, **params: Any) -> list[dict[str, Any]]:
    """页面每次保存都带全这几个值，没改的也带（模板的 ``cfg``）。只发改了的，测不出覆盖别的设置。"""
    full = {"f": s.sent["fs"], "w": s.sent["fw"], "b": s.page_brightness, "z": s.sent["zs"]} | params
    action = {"pageUuid": s.page_uuid, "action": {"name": "display", "params": full}}
    return s.handle({"type": "UI_ACTION", "payload": action})


def test_display_saves_the_settings_and_repaginates(opened: AgentSession) -> None:
    update = {k: v for part in shown(display(opened, f=18, w=700, b=40)) for k, v in part.items()}
    assert (opened.size, opened.bold) == (18, True) and (update["fs"], update["fw"]) == (18, 700)
    assert update["s"]["b"] == 40 and update["s"]["o"] == 150 and opened.library.display.brightness == 40
    assert all(len(update[f"s{i}"].split("\n")) <= lines_per_page(18) for i in range(SLOTS) if f"s{i}" in update)


def test_voice_turn_sends_only_the_slot_that_comes_in(opened: AgentSession) -> None:
    turn(opened, 2)
    (update,) = shown(say(opened, "下一页"))
    assert opened.slot == 3 and update["i"] == 3 and "v" in update
    assert sorted(k for k in update if agent.SLOT_FIELD.fullmatch(k)) == ["f7", "s7"]  # 环上最远的一格


def test_a_voice_turn_of_several_pages_moves_the_slot_as_far(opened: AgentSession) -> None:
    say(opened, "往后翻五页")
    assert opened.slot == 5 and opened.sent["i"] == 5


def test_a_voice_turn_stops_the_slot_at_the_book_end(opened: AgentSession) -> None:
    say(opened, "跳到百分之一百")
    slot = opened.slot
    say(opened, "下一页")
    assert opened.slot == slot


def test_a_jump_sends_the_visible_page_first(opened: AgentSession) -> None:
    turn(opened, 2)
    first, rest = shown(say(opened, "下一章"))
    assert first[f"s{opened.slot}"] == opened._current().text and "v" in first and "i" not in first
    assert {k[1:] for k in first if agent.SLOT_FIELD.fullmatch(k)} == {str(opened.slot)}
    assert len(rest) == 2 * (SLOTS - 1) and all(agent.SLOT_FIELD.fullmatch(k) and k[1:] != str(opened.slot) for k in rest)


def test_voice_font_updates_the_page_in_place_so_auto_turn_keeps_running(opened: AgentSession) -> None:
    turn(opened, 3)
    uuid, size, offset = opened.page_uuid, opened.size, opened._current().offset
    (show, *_) = actions(say(opened, "字大一点"), "SHOW_UI")
    assert show["display"] == {"mode": "UPDATE"} and show["pageUuid"] == uuid
    assert opened.slot == 3 and show["view"]["props"]["data"]["fs"] == opened.size > size
    assert opened._current().offset <= offset


def test_opening_a_book_in_a_session_updates_the_page_in_place(opened: AgentSession) -> None:
    turn(opened, 2)
    assert opened.book is not None
    (show, *_) = actions(say(opened, f"打开《{opened.book.title}》"), "SHOW_UI")
    assert show["display"] == {"mode": "UPDATE"} and opened.slot == 2


def test_display_stores_only_what_changed(opened: AgentSession, monkeypatch: pytest.MonkeyPatch) -> None:
    stored: list[dict[str, int]] = []
    store = opened.library.set_display

    def record(**changes: int) -> None:
        stored.append(changes)
        store(**changes)

    monkeypatch.setattr(opened.library, "set_display", record)
    display(opened)
    display(opened, z=30)  # 速度只从智能翻页面板来，这里的 z 是面板上剩下的数字
    display(opened, w=700)
    display(opened, b=40)
    assert stored == [{"weight": 4}, {"brightness": 40}]


def test_unknown_display_values_are_ignored(opened: AgentSession) -> None:
    size = opened.size
    display(opened, f=26, w=9, b=33)
    assert (opened.size, opened.bold) == (size, False)
    assert opened.library.display.brightness == 100


def test_font_keeps_the_reading_position(opened: AgentSession) -> None:
    for slot in range(1, 4):
        turn(opened, slot)
    offset = opened._current().offset
    display(opened, f=18)
    page = opened._current()
    following = opened._walk(1)
    assert page.offset <= offset
    if following is not None:
        assert opened._chapter_pages(following[0])[following[1]].offset > offset


def test_font_change_keeps_the_first_character_on_top(opened: AgentSession) -> None:
    for slot in range(1, 4):
        turn(opened, slot)
    offset = opened._current().offset
    first = opened._current().text.replace("\n", "").lstrip("　")[:6]
    for size in (18, 28, 16):
        display(opened, f=size)
        assert opened._current().offset == offset
        assert opened._current().text.replace("\n", "").lstrip("　").startswith(first)


def test_anchor_breaks_a_paragraph_without_losing_text(library: Library) -> None:
    book = library.book(library.entries()[0].id)
    assert book is not None
    chapter = max(book.readable(), key=lambda i: len(text_pages(book, book.mtime, i, 24)))
    plain = text_pages(book, book.mtime, chapter, 24)
    offset, para = max(book.chapters[chapter].paragraphs, key=lambda p: len(p[1]))
    anchor = offset + len(para) // 2
    pages = text_pages(book, book.mtime, chapter, 18, anchor)
    assert anchor in [p.offset for p in pages]

    def squeeze(rows: tuple[agent.TextPage, ...]) -> str:
        return "".join(p.text for p in rows).replace("\n", "").replace("　", "")

    assert squeeze(pages) == squeeze(plain)
    assert all(len(p.text.split("\n")) <= lines_per_page(18) for p in pages)


def test_opening_resumes_at_the_saved_character(opened: AgentSession) -> None:
    library = opened.library
    assert opened.book is not None
    turn(opened, 2)
    offset = opened._current().offset + 7  # 页图版记下的位置不一定在文字版的页首
    library.save(library.view(opened.book, offset, opened.size))
    saved = library.position(opened.book)[0]
    reopened = AgentSession("s2", opened.tools, library)
    reopened.start(init(session="s2")["payload"])
    assert reopened._current().offset == saved


@pytest.mark.parametrize("size", [16, 18, 20, 28, 32])
def test_reopening_after_a_resize_stays_on_the_same_character(opened: AgentSession, size: int) -> None:
    library = opened.library
    assert opened.book is not None
    turn(opened, 2)
    display(opened, f=size)
    offset = opened._current().offset  # 换字号后这一页不再和页图版的页对齐
    assert library.position(opened.book)[0] == offset
    reopened = AgentSession("s2", opened.tools, library)
    reopened.start(init(session="s2")["payload"])
    assert reopened._current().offset == offset


def test_turning_saves_the_text_page_with_other_line_spacing(opened: AgentSession) -> None:
    library = opened.library
    assert opened.book is not None
    library.set_display(spacing=170)
    for slot in (1, 2, 3, 4):
        turn(opened, slot)
        assert library.position(opened.book)[0] == opened._current().offset


def test_exit_speaks_and_closes(opened: AgentSession) -> None:
    assert spoken(say(opened, "退出")) and opened.closing


@pytest.mark.parametrize("text", ["结束了", "关闭吧", "别看了"])
def test_everyday_phrases_do_not_exit(opened: AgentSession, text: str) -> None:
    assert say(opened, text) == [] and not opened.closing


def test_confirmed_double_click_closes_the_session_silently(opened: AgentSession) -> None:
    action = {"pageUuid": opened.page_uuid, "action": {"name": "exit", "params": {"slot": opened.slot}}}
    assert opened.handle({"type": "UI_ACTION", "payload": action}) == [] and opened.closing


def test_double_click_stops_the_voice(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    out = tap(opened, "exit")
    assert actions(out, "STOP_OUTPUT") and actions(out, "SPEAK") == [] and opened.closing and not opened.listening


def test_double_click_during_smart_turning_has_no_voice_to_stop(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    assert tap(opened, "exit") == [] and opened.closing and not opened.listening


def test_exit_from_an_old_page_is_ignored(opened: AgentSession) -> None:
    action = {"pageUuid": "old", "action": {"name": "exit", "params": {}}}
    opened.handle({"type": "UI_ACTION", "payload": action})
    assert not opened.closing


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(agent.time, "monotonic", lambda: now[0])
    return now


def run_clock(s: AgentSession, clock: list[float], seconds: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for _ in range(int(seconds * 4)):
        clock[0] += agent.LISTEN_TICK_SECONDS
        out += s.listen_tick()
    return out


def test_listening_starts_at_the_top_of_the_page(opened: AgentSession, clock: list[float]) -> None:
    opened._jump(percent=30)
    (part,) = actions(say(opened, "开始听书"), "SPEAK")
    assert opened.listening and (part["index"], part["hasNext"]) == (1, True)
    first_line = opened._current().text.split("\n")[0].strip("　")
    assert first_line[:6] in part["text"]


def test_next_passage_is_sent_just_before_the_last_one_ends(opened: AgentSession, clock: list[float]) -> None:
    opened._jump(percent=30)
    (first,) = actions(say(opened, "开始听书"), "SPEAK")
    assert opened.listen_tick() == []
    seconds = len(first["text"]) / agent.LISTEN_CHARS_PER_SECOND
    assert actions(run_clock(opened, clock, seconds - agent.LISTEN_LEAD_SECONDS - 0.5), "SPEAK") == []
    (second, *_) = actions(run_clock(opened, clock, 3 + agent.LISTEN_LATENCY_SECONDS), "SPEAK")
    assert second["speechId"] == first["speechId"] and second["index"] == 2


def test_each_paragraph_is_sent_on_its_own(opened: AgentSession) -> None:
    assert opened.book is not None
    chapter = next(c for c in opened.book.chapters if len(c.paragraphs) > 3)
    (start, first), (second, _) = chapter.paragraphs[1:3]
    assert opened._passage(start) == (first + "\n", second)
    assert opened._passage(start + 3) == (first[3:] + "\n", second)  # 暂停后从段中间接着读
    title = opened._passage(chapter.start)
    assert title is not None and title[0].startswith(chapter.title + "。\n")


def test_the_number_of_a_split_chapter_is_not_read_out(opened: AgentSession, books_dir: Path) -> None:
    (books_dir / "无章.txt").write_text("\n".join(["风从海上吹来。" * 100] * 200), encoding="utf-8")
    say(opened, "打开《无章》")
    assert opened.book is not None
    first, second = opened.book.chapters[:2]
    assert (first.title, second.title) == ("无章 1", "无章 2")
    assert opened._passage(first.start)[0].startswith("无章。\n风从海上")  # 书名照念，序号不念
    assert opened._passage(second.start)[0].startswith("风从海上")


def test_a_voice_jump_does_not_say_the_split_number(opened: AgentSession, books_dir: Path) -> None:
    (books_dir / "无章.txt").write_text("\n".join(["风从海上吹来。" * 100] * 200), encoding="utf-8")
    say(opened, "打开《无章》")
    out = say(opened, "下一章")
    assert spoken(out) == "已跳到无章"
    assert opened.sent["hd"] == "无章 2"  # 页眉还带序号，不然分不出是哪一段
    assert "无章 2" not in spoken(say(opened, "我看到哪了"))


def test_a_heading_that_stays_in_the_text_is_read_once(opened: AgentSession, books_dir: Path, clock: list[float]) -> None:
    (books_dir / "连写.txt").write_text("".join(f"第{i}章风起云涌\n　　正文第{i}章。\n" for i in range(1, 6)), encoding="utf-8")
    say(opened, "打开《连写》")
    (part,) = actions(say(opened, "开始听书"), "SPEAK")
    assert part["text"] == "第1章风起云涌\n"
    assert opened._passage(opened.listen_at)[0] == "正文第1章。\n"


def test_the_paragraph_being_read_gets_a_dot(opened: AgentSession, clock: list[float]) -> None:
    opened._jump(chapter="下一章")
    plain = opened._current().text
    assert plain.startswith("　　")
    out = say(opened, "开始听书")
    marked = shown(out)[0][f"s{opened.slot}"]
    assert marked == agent.LISTEN_MARK + plain[2:] and len(agent.LISTEN_MARK) == 2  # 宽度不变，不会多折行
    assert actions(out, "SPEAK")  # 点和朗读一起发


def test_the_dot_moves_with_the_paragraph_and_leaves_on_pause(opened: AgentSession, clock: list[float]) -> None:
    opened._jump(chapter="下一章")
    say(opened, "开始听书")
    first = opened.listen_para
    updates = shown(run_clock(opened, clock, 12))
    assert opened.listen_para > first and any("●" in v for u in updates for v in u.values() if isinstance(v, str))
    out = say(opened, "暂停")
    assert "●" not in "".join(v for v in opened.sent.values() if isinstance(v, str)) and shown(out)


def test_passages_cover_the_text_without_gaps(opened: AgentSession) -> None:
    assert opened.book is not None
    offset = opened._current().offset
    read = ""
    while (passage := opened._passage(offset)) is not None and offset < opened.book.length:
        read += passage[0]
        assert passage[1] > offset
        offset = passage[1]
    for chapter in opened.book.chapters:
        for _, para in chapter.paragraphs[:2]:
            assert para[:10] in read


def test_the_page_follows_the_voice_and_saves_progress(opened: AgentSession, clock: list[float]) -> None:
    assert opened.book is not None
    opened._jump(percent=30)
    start = opened._current().offset
    say(opened, "开始听书")
    following = opened._walk(1)
    assert following is not None
    next_page = opened._chapter_pages(following[0])[following[1]].offset
    seconds = (next_page - start) / agent.LISTEN_CHARS_PER_SECOND
    seconds += agent.LISTEN_LATENCY_SECONDS - agent.LISTEN_PAGE_EARLY_SECONDS  # 比声音早一点翻
    run_clock(opened, clock, seconds - 1)
    assert opened._current().offset == start  # 还没读到下一页
    updates = shown(run_clock(opened, clock, 20))  # 章节名和段落间隔也要时间
    assert opened._current().offset >= next_page and updates  # 章末的短页可能一起翻过去
    assert opened.library.position(opened.book)[0] == opened._current().offset


def test_pause_stops_the_voice_and_resumes_at_a_sentence(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 12)
    heard = opened._heard()
    out = say(opened, "暂停")
    assert actions(out, "STOP_OUTPUT") == [{"scope": "SPEAK", "reason": "USER_ACTION"}] and not opened.listening
    assert heard - agent.LISTEN_BACK_CHARS - 60 <= opened.listen_resume <= heard
    resume = opened.listen_resume
    (part,) = actions(say(opened, "继续听书"), "SPEAK")
    assert opened.listen_base <= resume and part["index"] == 1 and opened.listening


@pytest.mark.parametrize("text", ["继续听书", "继续"])
def test_pause_right_after_a_page_turn_still_steps_back(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "开始听书")
    page = opened._current().offset
    while opened._current().offset == page:
        run_clock(opened, clock, agent.LISTEN_TICK_SECONDS)
    say(opened, "暂停")
    resume = opened.listen_resume
    assert resume < opened._current().offset  # 退回去的句首在上一页
    (part,) = actions(say(opened, text), "SPEAK")
    assert opened.listening and part["text"] == opened._passage(resume)[0]


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_pausing_in_a_paragraph_without_full_stops_goes_back_only_so_far(opened: AgentSession, books_dir: Path, clock: list[float], mode: str) -> None:
    smart_book(opened, books_dir, ["风从海上吹来，" * 400])
    say(opened, mode)
    run_clock(opened, clock, 60)
    heard = opened._heard()
    assert heard > agent.LISTEN_BACK_LIMIT * 2
    say(opened, "暂停")
    assert opened.listen_resume == heard - agent.LISTEN_BACK_LIMIT


def test_switching_modes_in_a_paragraph_without_full_stops_stays_near(opened: AgentSession, books_dir: Path, clock: list[float]) -> None:
    smart_book(opened, books_dir, ["风从海上吹来，" * 400])
    say(opened, "智能翻页")
    run_clock(opened, clock, 60)
    heard = opened._heard()
    say(opened, "开始听书")
    assert opened.listen_para == heard - agent.LISTEN_BACK_LIMIT


def test_pausing_near_the_start_stays_inside_the_book(opened: AgentSession, books_dir: Path, clock: list[float]) -> None:
    smart_book(opened, books_dir, ["风从海上吹来，" * 400])
    say(opened, "开始听书")
    say(opened, "暂停")
    assert opened.listen_resume == 0


def test_listening_from_a_new_page_ignores_the_old_place(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 12)
    say(opened, "暂停")
    say(opened, "往后翻三页")
    offset = opened._current().offset
    say(opened, "继续听书")
    assert opened.listen_base <= offset < opened.listen_at


def test_swiping_restarts_on_the_new_page(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 5)
    out = turn(opened, opened.slot + 1)
    assert actions(out, "STOP_OUTPUT") and actions(out, "SPEAK")[0]["index"] == 1
    assert opened.listening and opened.listen_base <= opened._current().offset < opened.listen_at


def test_the_menu_item_starts_and_pauses_listening(opened: AgentSession, clock: list[float]) -> None:
    def tap() -> list[dict[str, Any]]:
        action = {"pageUuid": opened.page_uuid, "action": {"name": "listen", "params": {"slot": opened.slot}}}
        return opened.handle({"type": "UI_ACTION", "payload": action})

    out = tap()
    assert opened.listening and shown(out)[0]["ls"] == 1 and actions(out, "SPEAK")
    run_clock(opened, clock, 5)
    out = tap()
    assert not opened.listening and shown(out)[0]["ls"] == 0 and actions(out, "STOP_OUTPUT")


def tap(s: AgentSession, name: str, **params: Any) -> list[dict[str, Any]]:
    action = {"pageUuid": s.page_uuid, "action": {"name": name, "params": {"slot": s.slot, "v": s.voice_turns, **params}}}
    return s.handle({"type": "UI_ACTION", "payload": action})


def test_taking_the_glasses_off_pauses_listening(opened: AgentSession, clock: list[float]) -> None:
    assert tap(opened, "away") == []
    say(opened, "开始听书")
    run_clock(opened, clock, 5)
    out = tap(opened, "away")
    assert actions(out, "STOP_OUTPUT") and not opened.listening and shown(out)[0]["ls"] == 0
    (part,) = actions(say(opened, "继续"), "SPEAK")  # 戴上后接着读
    assert opened.listening and part["index"] == 1


def test_listening_stops_when_nobody_touches_it_for_too_long(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    clock[0] = opened.last_action + agent.LISTEN_IDLE_SECONDS - 1
    opened.listen_tick()
    assert opened.listening
    clock[0] += 2
    assert actions(opened.listen_tick(), "STOP_OUTPUT") and not opened.listening
    assert opened.listen_tick() == []


def test_commands_while_listening(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    assert say(opened, "今天天气怎么样") == [] and say(opened, "能说什么") == [] and opened.listening
    assert say(opened, "继续听书") == []
    out = say(opened, "下一章")
    assert actions(out, "STOP_OUTPUT") and "已跳到" in spoken(out) and opened.listening  # 接着读新的一章
    out = say(opened, "我看到哪了")
    assert actions(out, "STOP_OUTPUT") and "读到" in spoken(out) and not opened.listening


@pytest.mark.parametrize("name", ["next_chapter", "prev_chapter"])
def test_menu_chapter_while_listening_reads_from_the_new_chapter(opened: AgentSession, clock: list[float], name: str) -> None:
    assert opened.book is not None
    say(opened, "跳到第三章")
    say(opened, "开始听书")
    run_clock(opened, clock, 12)
    speech, chapter = opened._speech, opened.chapter
    out = tap(opened, name)
    target = opened.chapter
    assert target == chapter + (1 if name == "next_chapter" else -1) and opened.listening
    assert actions(out, "STOP_OUTPUT") and "已跳到" in spoken(out)
    first = actions(out, "SPEAK")[-1]
    assert first["index"] == 1 and first["speechId"] == f"speech_{opened._speech}" and opened._speech > speech
    assert opened.book.chapters[target].title in first["text"]
    run_clock(opened, clock, 3)
    assert opened.chapter == target  # 不会被旧的朗读位置拉回去


def test_next_chapter_in_the_last_chapter_keeps_reading(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "跳到百分之九十九")
    say(opened, "开始听书")
    offset = opened._current().offset
    out = tap(opened, "next_chapter")
    assert "已经是最后一章" in spoken(out) and opened.listening and opened._current().offset == offset


def to_the_last_page(s: AgentSession) -> None:
    say(s, "跳到第六章")
    while s._walk(1) is not None:
        say(s, "下一页")


@pytest.mark.parametrize("text", ["下一章", "跳到第九十九章", "跳到百分之一千"])
def test_a_jump_that_goes_nowhere_keeps_smart_turning_in_place(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "跳到第六章")
    say(opened, "智能翻页")
    run_clock(opened, clock, 2)
    heard, turns = opened._heard(), opened.voice_turns
    out = say(opened, text)
    assert spoken(out) and opened.listening and opened.silent and actions(out, "STOP_OUTPUT") == []
    assert opened._heard() == heard and opened.voice_turns == turns


@pytest.mark.parametrize("text", ["下一章", "跳到第九十九章", "跳到百分之一千"])
def test_a_jump_that_goes_nowhere_reads_on_from_where_it_stopped(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "跳到第六章")
    say(opened, "开始听书")
    run_clock(opened, clock, 20)
    heard = opened._heard()
    out = say(opened, text)
    part = actions(out, "SPEAK")[-1]
    assert actions(out, "STOP_OUTPUT") and opened.listening and part["index"] == 1
    assert part["text"] == opened._passage(opened.listen_resume)[0] and opened.listen_resume > opened._current().offset
    assert heard - agent.LISTEN_BACK_CHARS - 60 <= opened.listen_resume <= heard  # 不是退回页首


def test_a_rejected_jump_does_not_restart_the_countdown(opened: AgentSession) -> None:
    say(opened, "跳到第六章")
    turns = opened.voice_turns
    say(opened, "下一章")
    say(opened, "跳到第九十九章")
    assert opened.voice_turns == turns


@pytest.mark.parametrize(("mode", "stops"), [("智能翻页", False), ("开始听书", True)])
def test_voice_turns_past_the_book_end_keep_going(opened: AgentSession, clock: list[float], mode: str, stops: bool) -> None:
    to_the_last_page(opened)
    opened.pace = 4
    say(opened, mode)
    run_clock(opened, clock, 1)
    heard, turns = opened._heard(), opened.voice_turns
    out = say(opened, "往后翻五页")
    assert opened.listening and opened.voice_turns == turns and bool(actions(out, "STOP_OUTPUT")) is stops
    assert opened.listen_base >= heard - agent.LISTEN_BACK_CHARS - 60


@pytest.mark.parametrize("mode", ["智能翻页", "开始听书"])
def test_swiping_past_the_book_end_keeps_going(opened: AgentSession, clock: list[float], mode: str) -> None:
    to_the_last_page(opened)
    opened.pace = 4
    say(opened, mode)
    run_clock(opened, clock, 1)
    heard, offset = opened._heard(), opened._current().offset
    out = turn(opened, (opened.slot + 1) % SLOTS)
    assert opened.listening and opened._current().offset == offset and opened._heard() == heard
    assert actions(out, "STOP_OUTPUT") == [] and actions(out, "SPEAK") == []


def test_going_back_to_the_start_on_the_first_page_reads_from_the_start(opened: AgentSession, books_dir: Path, clock: list[float]) -> None:
    smart_book(opened, books_dir, ["短段落短段落。"] * 100)
    say(opened, "开始听书")
    run_clock(opened, clock, 5)
    assert opened._current().offset == 0 and opened.listen_para > 0
    say(opened, "回到开头")
    assert opened.listening and opened.listen_para == 0


def test_a_missing_book_stops_the_reading_and_can_go_on(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 12)
    out = say(opened, "打开《不存在的书》")
    assert out[0]["payload"]["actions"][0]["type"] == "STOP_OUTPUT"  # 先停，不能和“没有找到”交错
    assert "没有找到" in spoken(out) and not opened.listening and opened.book.title == "测试长篇"
    assert actions(say(opened, "继续"), "SPEAK") and opened.listening


@pytest.mark.parametrize("text", ["继续", "继续看", "接着看", "看小说", "继续吧", "继续听吧"])
def test_continue_does_not_interrupt_the_reading(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "开始听书")
    assert say(opened, text) == [] and opened.listening


@pytest.mark.parametrize("text", ["看书", "项目进度怎么样", "买个书架"])
def test_talk_about_reading_does_not_stop_it(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "开始听书")
    assert say(opened, text) == [] and opened.listening


@pytest.mark.parametrize("text", ["暂停一下", "暂停吧", "先暂停", "别说了", "不读了", "关闭听书", "关掉朗读", "停下", "停下来", "别听了", "不要读了"])
def test_ways_to_pause(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "开始听书")
    assert actions(say(opened, text), "STOP_OUTPUT") and not opened.listening


@pytest.mark.parametrize("text", ["继续", "继续看", "接着读", "继续吧", "继续看吧", "继续读吧", "继续听吧", "继续播放"])
def test_continue_after_a_pause_reads_on(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 12)
    say(opened, "暂停")
    resume = opened.listen_resume
    (part,) = actions(say(opened, text), "SPEAK")
    assert opened.listening and part["index"] == 1 and opened.listen_at > resume
    assert "已打开" not in part["text"]


def test_continue_on_another_page_does_not_start_reading(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 12)
    say(opened, "暂停")
    say(opened, "往后翻三页")
    assert "已打开" in spoken(say(opened, "继续")) and not opened.listening


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_reading_to_the_end_leaves_no_pause_behind(opened: AgentSession, clock: list[float], mode: str) -> None:
    say(opened, "跳到第六章")
    while opened._walk(2) is not None:
        say(opened, "下一页")
    opened.pace = 8
    say(opened, mode)
    run_clock(opened, clock, 2)
    turn(opened, (opened.slot + 1) % SLOTS)  # 重新计时借了一次暂停
    assert opened.listening and opened._walk(1) is None
    run_clock(opened, clock, 600)
    assert not opened.listening
    assert "已打开" in spoken(say(opened, "继续")) and not opened.listening
    say(opened, mode)
    assert opened.listen_para >= opened._current().offset


def test_a_failure_leaves_no_pause_behind(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 5)
    turn(opened, opened.slot + 1)
    opened.failed()
    assert "已打开" in spoken(say(opened, "继续")) and not opened.listening


def test_continue_without_a_pause_does_not_start_reading(opened: AgentSession) -> None:
    assert "已打开" in spoken(say(opened, "继续")) and not opened.listening


def test_a_pause_in_one_book_does_not_carry_into_another(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    say(opened, "暂停")
    say(opened, "打开《雾港来信》")
    assert "已打开" in spoken(say(opened, "继续")) and not opened.listening


def test_reopening_the_book_forgets_the_pause(opened: AgentSession, clock: list[float]) -> None:
    assert opened.book is not None
    say(opened, "开始听书")
    say(opened, "暂停")
    say(opened, f"打开《{opened.book.title}》")
    assert "已打开" in spoken(say(opened, "继续")) and not opened.listening


def smart_book(opened: AgentSession, books_dir: Path, paragraphs: list[str], name: str = "短段落") -> AgentSession:
    """换一本段落长度已知的书。"""
    (books_dir / f"{name}.txt").write_text("\n".join(["第一章 开始", *paragraphs]), encoding="utf-8")
    opened.library.scan()
    say(opened, f"打开《{name}》")
    return opened


def dots(update: dict[str, Any]) -> list[str]:
    return [k for k, v in update.items() if isinstance(v, str) and "●" in v]


def next_page_offset(s: AgentSession) -> int:
    following = s._walk(1)
    assert following is not None
    return s._chapter_pages(following[0])[following[1]].offset


@pytest.mark.parametrize("text", ["智能翻页", "开始智能翻页"])
def test_smart_turning_starts_without_a_voice(opened: AgentSession, clock: list[float], text: str) -> None:
    opened._jump(chapter="下一章")
    out = say(opened, text)
    assert opened.listening and opened.silent and shown(out)[0]["ls"] == 2
    assert actions(out, "SPEAK") == [] and actions(out, "STOP_OUTPUT") == []
    assert dots(shown(out)[0]) == [f"s{opened.slot}"]


def test_smart_turning_stays_on_a_page_for_as_long_as_its_characters_take(opened: AgentSession, clock: list[float]) -> None:
    opened._jump(chapter="下一章")
    start = opened._current().offset
    say(opened, "智能翻页")
    seconds = (next_page_offset(opened) - start) / opened.pace
    run_clock(opened, clock, seconds - 0.5)
    assert opened._current().offset == start  # 不提前，也没有发送延迟
    out = run_clock(opened, clock, 1)
    assert opened._current().offset > start and shown(out) and opened.library.position(opened.book)[0] == opened._current().offset
    assert actions(run_clock(opened, clock, 60), "SPEAK") == []  # 跨章也不念章节名、不算段落间隔


def test_smart_turning_says_nothing_and_stops_nothing_on_its_way(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    out = run_clock(opened, clock, 120)
    assert actions(out, "SPEAK") == [] and actions(out, "STOP_OUTPUT") == [] and opened.listening


def test_fast_smart_turning_is_not_held_back_by_short_paragraphs(opened: AgentSession, books_dir: Path, clock: list[float]) -> None:
    smart_book(opened, books_dir, ["短段落短段落。"] * 400)
    opened.pace = 60
    start = opened._current().offset
    say(opened, "智能翻页")
    run_clock(opened, clock, 5)
    assert opened._heard() - start == 300  # 不是一拍一段：一段 7 字，那样每秒只走 28 字
    assert opened._current().offset <= opened._heard() < next_page_offset(opened)


def test_the_dot_changes_at_most_every_two_seconds_but_pages_turn_at_once(opened: AgentSession, books_dir: Path, clock: list[float]) -> None:
    smart_book(opened, books_dir, ["短段落短段落。"] * 400)
    opened.pace = 8
    say(opened, "智能翻页")
    last, dot_only, turns = clock[0], 0, 0
    for _ in range(4 * 24):
        page = opened._current().offset
        clock[0] += agent.LISTEN_TICK_SECONDS
        if not shown(opened.listen_tick()):
            continue
        if opened._current().offset == page:
            assert clock[0] - last >= agent.SMART_MIN_SECONDS
            dot_only += 1
        else:
            turns += 1
        last = clock[0]
    assert dot_only >= 4 and turns >= 3
    page, due = opened._current().offset, next_page_offset(opened)  # 紧接着上一次更新翻页，不用等
    opened.dot_due = clock[0] + 100
    clock[0] += (due - opened._heard()) / opened.pace + agent.LISTEN_TICK_SECONDS
    assert shown(opened.listen_tick()) and opened._current().offset > page


def test_the_dot_lands_on_the_paragraph_being_read(opened: AgentSession, books_dir: Path, clock: list[float]) -> None:
    smart_book(opened, books_dir, ["短段落短段落。"] * 40)
    opened.pace = 8
    say(opened, "智能翻页")
    run_clock(opened, clock, 4.5)
    assert opened.listen_para <= opened._heard() < opened.listen_at + 16
    assert any("●" in opened.sent[f"s{slot}"] for slot in range(SLOTS))


def test_the_menu_item_switches_smart_turning_on_and_off(opened: AgentSession, clock: list[float]) -> None:
    out = tap(opened, "smart")
    assert shown(out)[0]["ls"] == 2 and actions(out, "SPEAK") == []
    run_clock(opened, clock, 5)
    out = tap(opened, "smart_off")
    assert shown(out)[0]["ls"] == 0 and not opened.listening and actions(out, "STOP_OUTPUT") == []


def test_listening_and_smart_turning_replace_each_other(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 5)
    out = tap(opened, "smart")
    assert actions(out, "STOP_OUTPUT") and opened.silent and opened.listening and opened.sent["ls"] == 2
    run_clock(opened, clock, 5)
    out = tap(opened, "listen")
    assert actions(out, "STOP_OUTPUT") == [] and actions(out, "SPEAK") and not opened.silent and opened.sent["ls"] == 1
    out = say(opened, "智能翻页")
    assert actions(out, "STOP_OUTPUT") and opened.silent
    assert say(opened, "开始智能翻页") == []  # 已经开着
    out = say(opened, "开始听书")
    assert actions(out, "SPEAK") and not opened.silent and opened.listening


@pytest.mark.parametrize("text", ["停止智能翻页", "关闭智能翻页", "暂停", "暂停一下", "别读了", "暂停智能翻页", "关掉智能翻页", "退出智能翻页",
                                  "智能翻页暂停", "智能翻页停一下", "智能翻页关掉", "停下", "停下来", "别翻了"])
def test_ways_to_stop_smart_turning(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "智能翻页")
    out = say(opened, text)
    assert not opened.listening and opened.sent["ls"] == 0 and actions(out, "STOP_OUTPUT") == []
    assert not any("●" in v for v in opened.sent.values() if isinstance(v, str))


@pytest.mark.parametrize("text", ["继续", "继续吧", "继续智能翻页", "接着智能翻页", "恢复智能翻页"])
def test_continue_picks_smart_turning_up_again(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "智能翻页")
    run_clock(opened, clock, 3)
    say(opened, "暂停")
    resume = opened.listen_resume
    out = say(opened, text)
    assert opened.listening and opened.silent and actions(out, "SPEAK") == [] and opened.listen_base == resume


def test_speaking_during_smart_turning_does_not_stop_it(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    for text, heard in (("我看到哪了", "读到"), ("书架上有什么书", "《"), ("能说什么", "可以说")):
        out = say(opened, text)
        assert heard in spoken(out) and actions(out, "STOP_OUTPUT") == [] and opened.listening and opened.silent
    assert say(opened, "今天天气怎么样") == [] and say(opened, "继续") == [] and opened.listening


@pytest.mark.parametrize("text", ["下一章", "往后翻三页", "跳到百分之五十", "跳到第三章"])
def test_smart_turning_carries_on_from_where_a_command_lands(opened: AgentSession, clock: list[float], text: str) -> None:
    say(opened, "智能翻页")
    run_clock(opened, clock, 3)
    before = opened._current().offset
    out = say(opened, text)
    assert opened._current().offset != before and actions(out, "STOP_OUTPUT") == []
    assert opened.listening and opened.silent and opened.sent["ls"] == 2
    assert opened.listen_base == opened._current().offset


def test_swiping_restarts_smart_turning_on_the_new_page(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    run_clock(opened, clock, 2)
    out = turn(opened, opened.slot + 1)
    start = opened._current().offset
    assert actions(out, "STOP_OUTPUT") == [] and opened.listening and opened.silent and opened.listen_base == start
    assert opened._heard() == start


def test_opening_another_book_stops_smart_turning(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    out = say(opened, "打开《雾港来信》")
    assert "已打开" in spoken(out) and not opened.listening and opened.sent["ls"] == 0


def test_smart_turning_stops_at_the_end_of_the_book(opened: AgentSession, clock: list[float]) -> None:
    opened._jump(percent=99)
    say(opened, "智能翻页")
    out = run_clock(opened, clock, 600)
    assert spoken(out) == "全书读完了" and not opened.listening and opened.sent["ls"] == 0
    assert opened._walk(1) is None and actions(out, "STOP_OUTPUT") == []  # 停在最后一页
    assert not any("●" in v for v in opened.sent.values() if isinstance(v, str))


def test_pace_comes_from_the_panel_and_is_saved(opened: AgentSession) -> None:
    out = tap(opened, "smart", z=30)
    assert opened.pace == 30 and opened.listening and shown(out)[0]["zs"] == 30
    assert opened.library.display.pace == 30
    reopened = AgentSession("s2", opened.tools, opened.library)
    reopened.start(init(session="s2")["payload"])
    assert reopened.pace == 30 and reopened.sent["zs"] == 30


@pytest.mark.parametrize("bad", [3, 2, 62, 21, "20", None])
def test_bad_paces_are_ignored(opened: AgentSession, bad: Any) -> None:
    tap(opened, "smart", z=bad)
    assert opened.pace == DEFAULT_PACE == 18 and opened.library.display.pace == DEFAULT_PACE and opened.listening


def test_changing_the_pace_keeps_the_place(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    run_clock(opened, clock, 10)
    assert opened._heard() - opened.listen_base == 10 * DEFAULT_PACE
    place = opened._heard()
    assert shown(tap(opened, "smart", z=40))[0]["zs"] == 40  # 语音开的，页面还没收到 ls，用户开了面板
    assert opened._heard() == place  # 新速度不能拿旧的计时算，不然一下子跳出去
    run_clock(opened, clock, 2)
    assert opened._heard() == place + 80


def test_taking_the_glasses_off_stops_smart_turning(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    out = tap(opened, "away")
    assert not opened.listening and opened.sent["ls"] == 0 and actions(out, "STOP_OUTPUT") == []


def test_the_screen_going_off_stops_smart_turning_only(opened: AgentSession, clock: list[float]) -> None:
    assert tap(opened, "hidden") == []
    say(opened, "开始听书")
    assert tap(opened, "hidden") == [] and opened.listening  # 听书不看屏幕
    say(opened, "智能翻页")
    run_clock(opened, clock, 3)
    out = tap(opened, "hidden")
    assert not opened.listening and opened.sent["ls"] == 0 and actions(out, "STOP_OUTPUT") == []
    saved = opened.library.position(opened.book)
    run_clock(opened, clock, 600)
    assert opened.library.position(opened.book) == saved
    say(opened, "继续")  # 亮屏后接着走
    assert opened.listening and opened.silent


def test_smart_turning_stops_when_nobody_touches_it_for_too_long(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    clock[0] = opened.last_action + agent.LISTEN_IDLE_SECONDS + 1
    assert actions(opened.listen_tick(), "STOP_OUTPUT") == [] and not opened.listening


def test_resuming_restarts_smart_turning_at_the_shown_paragraph(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    run_clock(opened, clock, 5)
    para, page = opened.listen_para, opened._current().offset
    clock[0] += 3600  # 断线期间没有 tick
    out = opened.resumed()
    assert actions(out, "STOP_OUTPUT") == [] and actions(out, "SPEAK") == []
    assert opened.listening and opened.listen_base == para and opened._current().offset == page
    assert {k: v for part in shown(out) for k, v in part.items()} == opened.sent  # 全量重发


def test_a_failure_during_smart_turning_has_no_voice_to_stop(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    out = opened.failed()
    assert [m["type"] for m in out] == ["OUTPUT", "ERROR"] and not opened.listening
    assert actions(out, "STOP_OUTPUT") == [] and shown(out) == [{"ls": 0}]


def test_a_failure_while_listening_clears_the_page_state(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    out = opened.failed()
    assert actions(out, "STOP_OUTPUT") and shown(out) == [{"ls": 0}] and opened.sent["ls"] == 0
    assert out[-1]["type"] == "ERROR"
    say(opened, "下一页")
    assert opened.sent["ls"] == 0
    assert [m["type"] for m in opened.failed()] == ["ERROR"]  # 没开着就不用再发


def test_a_failure_while_reconnecting_clears_the_page_state(opened: AgentSession, clock: list[float], monkeypatch: pytest.MonkeyPatch) -> None:
    say(opened, "开始听书")
    monkeypatch.setattr(opened, "_refresh", lambda: 1 / 0)
    monkeypatch.setattr(opened, "_restart", lambda offset: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        opened.resumed()
    out = opened.failed()
    assert not opened.listening and shown(out) == [{"ls": 0}] and out[-1]["type"] == "ERROR"


def test_a_failure_before_a_page_is_open_sends_only_the_error(session: AgentSession) -> None:
    assert [m["type"] for m in session.failed()] == ["ERROR"]


def test_end_of_book_closes_the_speech(opened: AgentSession, clock: list[float]) -> None:
    assert opened.book is not None
    opened._jump(percent=99)
    say(opened, "开始听书")
    out = run_clock(opened, clock, 600)
    assert actions(out, "SPEAK")[-1]["hasNext"] is False and not opened.listening


def test_listening_over_the_socket(client: TestClient, settings: Settings) -> None:
    def until(ws: Any, kind: str) -> list[dict[str, Any]]:
        for _ in range(8):  # 翻页和下一段可能排在前面
            if found := actions([ws.receive_json()], kind):
                return found
        pytest.fail(f"没有收到 {kind}")

    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json(init(pages=PAGES))
        for _ in range(3):
            ws.receive_json()  # 就绪、打开页面、播报书名
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "开始听书"}})
        assert until(ws, "SPEAK")[0]["hasNext"] is True
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "暂停"}})
        assert until(ws, "STOP_OUTPUT")


def test_resuming_resends_what_may_have_been_lost(opened: AgentSession) -> None:
    say(opened, "下一章")  # 这几条没送到
    update = {k: v for part in shown(opened.resumed()) for k, v in part.items()}
    assert update == {k: v for k, v in opened.sent.items() if k != "i"} and update[f"s{opened.slot}"] == opened._current().text


def test_resuming_does_not_pull_the_page_back_from_lost_swipes(opened: AgentSession) -> None:
    turn(opened, 1)
    assert all("i" not in part for part in shown(opened.resumed()))


def test_resuming_with_an_engine_running_puts_the_page_where_it_is(opened: AgentSession) -> None:
    say(opened, "智能翻页")
    assert any(part.get("i") == opened.slot for part in shown(opened.resumed()))


def test_resuming_without_a_page_sends_nothing(session: AgentSession) -> None:
    session.start(init(entryContext={"rawUtterance": "看小说"})["payload"])
    assert session.resumed() == []


def test_resuming_reads_the_paragraph_again_with_a_new_clock(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 12)
    para, speech = opened.listen_para, opened._speech
    clock[0] += 60  # 断线期间没有 tick
    out = opened.resumed()
    (part,) = actions(out, "SPEAK")
    assert actions(out, "STOP_OUTPUT") and opened.listening and opened.listen_para == para
    assert part["text"] == opened._passage(para)[0]
    assert part["index"] == 1 and part["speechId"] == f"speech_{speech + 1}"
    assert actions(run_clock(opened, clock, 2), "SPEAK") == []  # 不会连发几段追时间


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_resuming_inside_a_paragraph_longer_than_a_page_stays_on_the_page(opened: AgentSession, books_dir: Path, clock: list[float], mode: str) -> None:
    smart_book(opened, books_dir, ["风从海上吹来。" * 400])
    say(opened, mode)
    while opened._current().offset < 300:
        run_clock(opened, clock, agent.LISTEN_TICK_SECONDS)
    page = opened._current().offset
    opened.resumed()
    assert opened.listening and opened._current().offset == page and opened.listen_para == page


def test_keepalive_resends_a_field_until_idle_for_too_long(opened: AgentSession) -> None:
    (update,) = shown(opened.keepalive())
    assert update == {"t": opened.book.title}
    opened.last_action -= agent.IDLE_LIMIT_SECONDS + 1
    assert opened.keepalive() == []


@pytest.mark.parametrize(("text", "alive"), [("今天天气真好啊", False), ("打开空调", False), ("下一页", True), ("我看到哪了", True)])
def test_only_commands_count_as_activity(opened: AgentSession, clock: list[float], text: str, alive: bool) -> None:
    opened.last_action = clock[0]
    clock[0] += agent.IDLE_LIMIT_SECONDS - 100
    say(opened, text)
    clock[0] += 101
    assert bool(opened.keepalive()) is alive


def test_keepalive_needs_a_page(session: AgentSession) -> None:
    assert session.keepalive() == []


def test_hub_epochs(settings: Settings, library: Library) -> None:
    hub = AgentHub(ReaderTools(settings, library), library)
    session, mode = hub.attach("a", 1)
    assert mode == "CREATED" and session is not None
    assert hub.attach("a", 2) == (session, "RESUMED")
    assert hub.attach("a", 2) == (session, "STALE")
    assert hub.attach("a", 1) == (session, "STALE")
    assert hub.attach("b", 3) == (None, "UNKNOWN")


def test_a_book_pushed_out_of_the_library_is_released(settings: Settings, books_dir: Path, library: Library) -> None:
    for title in "甲乙丙丁戊":
        (books_dir / f"{title}.txt").write_text(make_novel(), encoding="utf-8")
    library.scan()
    hub = AgentHub(ReaderTools(settings, library), library)
    session, _ = hub.attach("a", 1)
    assert session is not None
    session.start(init(session="a")["payload"])
    alive = []
    for title in "甲乙丙丁戊":
        say(session, f"打开《{title}》")
        alive.append(weakref.ref(session.book))
    gc.collect()
    assert [ref() is None for ref in alive] == [True] * (5 - MAX_BOOKS) + [False] * MAX_BOOKS
    assert text_pages(session.book, session.book.mtime, session.chapter, session.size)  # 当前这本还能排版


def test_hub_forgets_the_oldest_session(settings: Settings, library: Library) -> None:
    hub = AgentHub(ReaderTools(settings, library), library)
    hub.attach("oldest", 1)
    hub.attach("kept", 1)
    hub.attach("oldest", 2)  # 重连过的不算最旧
    for i in range(agent.MAX_SESSIONS - 1):
        hub.attach(f"s{i}", 1)
    assert len(hub.sessions) == agent.MAX_SESSIONS and "oldest" in hub.sessions and "kept" not in hub.sessions
    assert hub.attach("kept", 2) == (None, "UNKNOWN")


# ---------------------------------------------------------------------- WebSocket


@pytest.fixture
def client(settings: Settings, library: Library) -> Iterator[TestClient]:
    with TestClient(create_app(settings, library, access_log=False)) as c:
        yield c


def auth(settings: Settings, request_id: str = "wsr_1") -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.token}", "X-Request-Id": request_id}


def test_handshake_and_session(client: TestClient, settings: Settings) -> None:
    with client.websocket_connect(AGENT_PATH, headers=auth(settings, "wsr_abc")) as ws:
        assert dict(ws.extra_headers)[b"x-request-id"] == b"wsr_abc"
        ws.send_json(init())
        assert ws.receive_json() == {
            "specVersion": "1.0", "sessionId": "ses_1", "type": "SESSION_READY",
            "payload": {"mode": "CREATED", "connectionEpoch": 1},
        }
        shown_page = ws.receive_json()
        assert shown_page["type"] == "OUTPUT"
        assert shown_page["payload"]["actions"][0]["type"] == "SHOW_UI"
        assert ws.receive_json()["payload"]["actions"][0]["type"] == "SPEAK"
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "退出"}})
        assert ws.receive_json()["payload"]["actions"][0]["type"] == "SPEAK"
        assert ws.receive()["type"] == "websocket.close"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_bad_token_is_401_with_request_id(client: TestClient, headers: dict[str, str]) -> None:
    with pytest.raises(WebSocketDenialResponse) as exc:
        with client.websocket_connect(AGENT_PATH, headers=headers | {"X-Request-Id": "wsr_9"}):
            pass
    assert exc.value.status_code == 401 and exc.value.headers["x-request-id"] == "wsr_9"


def test_reconnect_resumes_and_closes_the_old_connection(client: TestClient, settings: Settings) -> None:
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as first:
        first.send_json(init())
        first.receive_json(), first.receive_json(), first.receive_json()
        with client.websocket_connect(AGENT_PATH, headers=auth(settings, "wsr_2")) as second:
            second.send_json(init(epoch=2))
            assert second.receive_json()["payload"] == {"mode": "RESUMED", "connectionEpoch": 2}
            closed = first.receive()
            assert closed["type"] == "websocket.close" and closed["code"] == 4001
            assert shown([second.receive_json()])[0]["t"] == "测试长篇"  # 断线时丢的更新全部重发


def test_older_epoch_is_closed_with_4002(client: TestClient, settings: Settings) -> None:
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as first:
        first.send_json(init(epoch=1))
        first.receive_json(), first.receive_json(), first.receive_json()
        with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as stale:
            stale.send_json(init(epoch=1))
            closed = stale.receive()
            assert closed["type"] == "websocket.close" and closed["code"] == 4002


def test_unknown_session_gets_a_fatal_error(client: TestClient, settings: Settings) -> None:
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json(init(session="gone", epoch=5))
        error = ws.receive_json()
        assert error["type"] == "ERROR" and error["payload"]["fatal"] is True
        assert ws.receive()["type"] == "websocket.close"


@pytest.mark.parametrize("first", ['{"type": "UTTERANCE"}', "不是 JSON"])
def test_bad_first_message_closes(client: TestClient, settings: Settings, first: str) -> None:
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_text(first)
        closed = ws.receive()
        assert closed["type"] == "websocket.close" and closed["code"] == 1002


def test_bad_json_keeps_the_connection(client: TestClient, settings: Settings) -> None:
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json(init())
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        ws.send_text("不是 JSON")
        error = ws.receive_json()
        assert error["payload"]["code"] == "BAD_JSON" and error["payload"]["fatal"] is False
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "能说什么"}})
        assert "下一页" in spoken([ws.receive_json()])


@pytest.mark.parametrize(("code", "kept"), [(1000, False), (1001, True), (1006, True)])
def test_only_a_clean_close_forgets_the_session(client: TestClient, settings: Settings, code: int, kept: bool) -> None:
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json(init())
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        ws.close(code)
    assert ("ses_1" in client.app.state.agent_hub.sessions) is kept  # 异常断开要留着等重连


def test_keepalive_goes_out_over_the_socket(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "KEEPALIVE_SECONDS", 0.01)
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json(init())
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        assert shown([ws.receive_json()]) == [{"t": "测试长篇"}]  # 平台约 120 秒没有业务消息就结束会话


def test_failing_to_save_keeps_the_connection(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(*_: Any) -> None:
        raise OSError(28, "No space left on device")

    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json(init())
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        monkeypatch.setattr(Library, "save", full)
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "下一页"}})
        error = ws.receive_json()
        assert error["type"] == "ERROR" and error["payload"]["fatal"] is False
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "能说什么"}})
        assert "下一页" in spoken([ws.receive_json()])


def test_failing_to_open_at_entry_keeps_the_connection(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: Any) -> None:
        raise RuntimeError("读不了")

    monkeypatch.setattr(ReaderTools, "open_book", broken)
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json(init())
        assert ws.receive_json()["type"] == "SESSION_READY"
        error = ws.receive_json()
        assert error["type"] == "ERROR" and error["payload"]["fatal"] is False
        monkeypatch.undo()
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "打开《测试长篇》"}})
        assert actions([ws.receive_json()], "SHOW_UI")


def test_failing_while_listening_stops_the_voice_once(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def broken(_: AgentSession) -> None:
        nonlocal calls
        calls += 1
        raise OSError(28, "No space left on device")

    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json(init())
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "开始听书"}})
        while not actions([ws.receive_json()], "SPEAK"):
            pass
        monkeypatch.setattr(AgentSession, "listen_tick", broken)
        received = [ws.receive_json()]
        while received[-1]["type"] != "ERROR":
            received.append(ws.receive_json())
        assert actions(received, "STOP_OUTPUT") and received[-1]["payload"]["fatal"] is False
        assert not client.app.state.agent_hub.sessions["ses_1"].listening
        time.sleep(agent.LISTEN_TICK_SECONDS * 3)
        assert calls == 1
        ws.send_json({"specVersion": "1.0", "sessionId": "ses_1", "type": "UTTERANCE", "payload": {"text": "能说什么"}})
        assert "下一页" in spoken([ws.receive_json()])


def test_real_socket_handshake(settings: Settings, library: Library) -> None:
    """真的走 uvicorn：101 和 401 都要带 X-Request-Id，TestClient 替代不了这一层。"""
    app = create_app(settings, library, access_log=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", ws_ping_interval=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}{AGENT_PATH}"

    async def scenario() -> None:
        async with websockets.connect(url, additional_headers=auth(settings, "wsr_live")) as ws:
            assert ws.response.headers["X-Request-Id"] == "wsr_live"
            await ws.send(json.dumps(init()))
            assert json.loads(await ws.recv())["type"] == "SESSION_READY"
            await ws.ping()
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
            async with websockets.connect(url, additional_headers={"X-Request-Id": "wsr_bad"}):
                pass
        assert exc.value.response.status_code == 401
        assert exc.value.response.headers["X-Request-Id"] == "wsr_bad"

    try:
        import anyio
        anyio.run(scenario)
    finally:
        server.should_exit = True
        thread.join(5)


# ---------------------------------------------------------------------- 页面模板


@pytest.fixture(scope="module")
def page() -> ET.Element:
    return ET.parse(TEMPLATE).getroot()


def test_page_has_one_text_label_per_slot(page: ET.Element) -> None:
    assert len(list(page)) <= 64
    bodies = [el for el in page.iter("label") if el.get("font-size") == "{{fs}}"]
    assert [el.get("text") for el in bodies] == [f"{{{{s{i}}}}}" for i in range(SLOTS)]
    assert all(el.get("font-weight") == "{{fw}}" and el.get("width") == str(LABEL_WIDTH) for el in bodies)
    slots = [el for el in page.iter("view") if el.get(f"{A}if", "").startswith("{{i ==")]
    assert [el.get(f"{A}if") for el in slots] == [f"{{{{i == {i}}}}}" for i in range(SLOTS)]
    assert [[label.get("text") for label in el] for el in slots] == [[f"{{{{s{i}}}}}", f"{{{{f{i}}}}}"] for i in range(SLOTS)]


def test_the_chapter_name_is_one_label(page: ET.Element) -> None:
    assert [el.get("text") for el in page.iter("label")].count("{{hd}}") == 1


def test_the_page_only_looks_up_tables_the_server_sends(page: ET.Element, opened: AgentSession) -> None:
    used = {name for el in page.iter() for v in el.attrib.values() for name in re.findall(r"\{\{(\w+)\[", v)}
    assert used == set(agent.PAGE_TABLES) and used <= set(opened.sent)
    sample = json.loads((TEMPLATE.parent / "data.json").read_text(encoding="utf-8"))
    assert used <= set(sample) and all(sample[k] == v for k, v in agent.PAGE_TABLES.items())  # 模拟器和平台预览用的数据


def test_only_the_shared_bar_has_a_layout_binding(page: ET.Element) -> None:
    # width 绑定每次写数据都重新设置，菜单移动也不例外
    assert [el.get("width") for el in page.iter("view") if "{{" in el.get("width", "")] == ["{{bar}}"]


def test_turning_looks_up_the_next_slot_until_the_edge_of_the_ring(page: ET.Element) -> None:
    assert row_actions(page, "event://tn?") == [("{{i != hi}}", "dataSource://update?i={{nx[i].n}}")]
    assert row_actions(page, "event://tp?") == [("{{i != lo}}", "dataSource://update?i={{pv[i].n}}")]
    assert row_actions(page, "event://t") == [  # 前缀，两个方向都接得到，翻不动也重新倒计时，隔一轮再试
        ("{{d:p > 0}}", "data://update?c={{d:p}}"),
        (None, "svc://ui-action?mode=mcp&name=turn&params={{json:turn}}"),
    ]
    assert [row["n"] for row in agent.PAGE_TABLES["nx"]] == [*range(1, SLOTS), 0]
    assert [row["n"] for row in agent.PAGE_TABLES["pv"]] == [SLOTS - 1, *range(SLOTS - 1)]


def report_turn(s: AgentSession, slot: int, seen: int) -> list[dict[str, Any]]:
    action = {"pageUuid": s.page_uuid, "action": {"name": "turn", "params": {"slot": slot, "v": seen}}}
    return s.handle({"type": "UI_ACTION", "payload": action})


def engine_turn(s: AgentSession, clock: list[float]) -> dict[str, Any]:
    """等服务翻一页，返回这一下发给页面的字段。"""
    turns, data = s.voice_turns, {}
    while s.voice_turns == turns:
        clock[0] += 0.25
        data = {k: v for part in shown(s.listen_tick()) for k, v in part.items()}
    return data


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_an_engine_turn_moves_the_slot_and_fills_one_page(opened: AgentSession, clock: list[float], mode: str) -> None:
    say(opened, mode)
    update = engine_turn(opened, clock)
    assert opened.slot == update["i"] == 1 and opened.sent["hi"] == 1 + AHEAD
    assert [k for k in update if k.startswith("f") and agent.SLOT_FIELD.fullmatch(k)] == [f"f{1 + AHEAD}"]  # 只有一页是新的


def test_a_swipe_crossing_an_engine_turn_lands_on_the_same_page(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    seen = opened.voice_turns
    engine_turn(opened, clock)
    position = opened.chapter, opened.page
    report_turn(opened, opened.slot, seen)  # 用户在上一页滑的，页面自己也走到了这一格
    assert (opened.chapter, opened.page) == position


def test_swiping_back_across_an_engine_turn_sends_the_page_there(opened: AgentSession, clock: list[float]) -> None:
    turn(opened, 2)
    say(opened, "智能翻页")
    seen, before = opened.voice_turns, opened._current().offset
    engine_turn(opened, clock)
    update = shown(report_turn(opened, 1, seen))  # 翻页那条还会把页面带到 3 号槽，要拉回来
    assert opened.slot == 1 and opened._current().offset < before
    assert any(part.get("i") == 1 for part in update)


def test_a_swipe_that_saw_the_engine_turn_is_not_sent_back(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    engine_turn(opened, clock)
    update = shown(report_turn(opened, 2, opened.voice_turns))
    assert opened.slot == 2 and all("i" not in part for part in update)


def test_a_report_behind_an_engine_turn_does_not_step_back(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    seen = opened.voice_turns
    engine_turn(opened, clock)
    position = opened.chapter, opened.page
    tap(opened, "menu_open", slot=0, v=seen)
    assert (opened.chapter, opened.page) == position and opened.slot == 1


def test_page_action_names_are_known_to_the_server(page: ET.Element) -> None:
    rows = [*page.iter("event"), *page.iter("key-event"), *page.iter("wear-event"), *page.iter("lifecycle-event")]
    uris = [v for e in rows for v in e.attrib.values() if v.startswith(("svc://", "emit://"))]
    names = {u.split("name=")[1].split("&")[0] for u in uris if "name=" in u} - {"{{e:n}}"}
    names |= {n for u in uris if u.startswith("emit://event/move?") for n in re.findall(r"[?&]n=(\w+)", u)}
    assert names == {
        "turn", "next_chapter", "prev_chapter", "display", "exit", "listen", "smart", "smart_off", "away", "hidden",
        "menu_open", "menu_close", "auto", "optics",
    }


@pytest.mark.parametrize(
    "name",
    ["turn", "next_chapter", "prev_chapter", "display", "listen", "smart", "smart_off", "away", "hidden", "menu_open", "menu_close", "auto", "optics", "exit"],
)
def test_the_server_knows_every_page_action(opened: AgentSession, caplog: pytest.LogCaptureFixture, name: str) -> None:
    tap(opened, name, z=0)
    assert "不认识的页面动作" not in caplog.text


def test_taking_the_glasses_off_stops_auto_turning_and_tells_the_server(page: ET.Element) -> None:
    assert page.find("wear-event").get("on-wear-off") == "emit://event/move?m={{d:m}}&p=0&n=away"  # 调到一半还没保存，不能退回正文
    first, report = [e.get("action") for e in page.iter("event") if e.get("match") == "event://move?"]
    assert first == "data://update?m={{e:m}}&p={{e:p}}&c={{d:p}}" and "name={{e:n}}" in report


def test_screen_off_stops_auto_turning_and_tells_the_server(page: ET.Element) -> None:
    events = page.find("lifecycle-event")
    assert events.get("on-screen-off") == "emit://event/move?m={{d:m}}&p=0&n=hidden"
    assert events.get("on-hide") == "data://update?p=0"  # 播报字幕可能触发，只关自动翻页


def test_double_click_twice_leaves_and_ends_the_session(page: ET.Element) -> None:
    meta = page.find("page-meta")
    assert meta.get("confirm-back-text") and meta.get("forbidden") is None  # 写了 forbidden 双击就完全没反应
    assert page.find("prompt-config").get("text") == ""  # 提示显示在这里，平时不占地方
    rows = [e for e in page.iter("key-event") if e.get("on-key-back")]  # 其他分支缺这个键，都落到最后一支
    assert len(rows) == 1 and rows[0].get(f"{A}else") == ""
    assert rows[0].get("on-key-back").startswith("svc://ui-action?mode=mcp&name=exit")


def key_action(page: ET.Element, m: int, key: str) -> str:
    """m 时按 key 触发什么。链是按键分开算的：命中的这一支缺这个键，就落到下一支。"""
    def holds(condition: str) -> bool:
        left, op, right = condition.strip("{}").split()
        assert left == "d:m"
        return {"==": m == int(right), ">": m > int(right)}[op]

    for row in page.iter("key-event"):
        condition = row.get(f"{A}if", row.get(f"{A}elif"))
        if (condition is None or holds(condition)) and row.get(key):
            uri = row.get(key)
            for expression, value in (("{{d:m + 1}}", m + 1), ("{{d:m - 1}}", m - 1), ("{{d:m + 10}}", m + 10), ("{{d:m - 10}}", m - 10)):
                uri = uri.replace(expression, str(value))
            return re.sub(r"\{\{(\w+)\[d:m\]\.n\}\}", lambda found: str(agent.PAGE_TABLES[found[1]][m]["n"]), uri)
    return ""


def key_targets(page: ET.Element, m: int) -> dict[str, str]:
    return {k.removeprefix("on-key-"): key_action(page, m, k) for k in ("on-key-enter", "on-key-next", "on-key-prev")}


def test_menus_wrap_around(page: ET.Element) -> None:
    move = "data://update?m="
    for items in ([1, 2, 3, 4, 5, 6, 7], [11, 12, 13, 14, 15]):
        for i, m in enumerate(items):
            assert key_action(page, m, "on-key-next") == f"{move}{items[(i + 1) % len(items)]}"
            assert key_action(page, m, "on-key-prev") == f"{move}{items[i - 1]}"
    assert not any("stay" in v for e in page.iter("key-event") for v in e.attrib.values())


def test_every_menu_item_does_what_it_says(page: ET.Element) -> None:
    enter = {m: key_targets(page, m)["enter"] for m in (0, *range(1, 8), *range(11, 16), *range(21, 25), 25, 26)}
    assert enter == {
        0: "emit://event/move?m=1&p={{d:p}}&n=menu_open",
        1: "emit://event/move?m=0&p={{d:p}}&n=menu_close",
        2: "emit://event/e{{d:m}}?",
        3: "emit://event/e{{d:m}}?",
        4: "emit://event/e{{d:m}}?",
        5: "emit://event/move?m=0&p=0&n=listen",
        6: "emit://event/move?m=0&p={{d:p}}&n=next_chapter",
        7: "emit://event/move?m=0&p={{d:p}}&n=prev_chapter",
        11: "data://update?m=21",
        12: "data://update?m=22",
        13: "data://update?m=23",
        14: "data://update?m=24",
        15: "data://update?m=4",
        21: "emit://event/move?m=11&p={{d:p}}&n=display",
        22: "emit://event/move?m=12&p={{d:p}}&n=display",
        23: "emit://event/move?m=13&p={{d:p}}&n=display",
        24: "emit://event/move?m=14&p={{d:p}}&n=display",
        25: "emit://event/start{{d:m}}?",
        26: "emit://event/start{{d:m}}?",
    }
    assert all(key_targets(page, m)["next"] and key_targets(page, m)["prev"] for m in enter)
    assert row_actions(page, "event://e4?") == [(None, "data://update?m=11")]
    assert key_targets(page, 22)["next"] == "dataSource://update?fw=700" and key_targets(page, 22)["prev"] == "dataSource://update?fw=400"
    assert key_targets(page, 24)["next"] == "emit://event/s{{d:m}}a?"  # 落到 s24a、s24b
    assert key_targets(page, 24)["prev"] == "emit://event/s{{d:m}}b?"
    assert key_targets(page, 26)["next"] == "emit://event/more?"
    assert key_targets(page, 25)["prev"] == "emit://event/less?"


def row_actions(page: ET.Element, match: str) -> list[tuple[str | None, str]]:
    """这个事件的每一行：(条件, 动作)。a:else 的条件写成空串。"""
    rows = [e for e in page.iter("event") if e.get("match") == match]
    return [(e.get(f"{A}if", e.get(f"{A}elif", e.get(f"{A}else"))), e.get("action")) for e in rows]


def test_smart_turning_stops_with_one_click_and_otherwise_opens_its_panel(page: ET.Element) -> None:
    assert row_actions(page, "event://e2?") == [
        ("{{ls == 2}}", "emit://event/move?m=0&p=0&n=smart_off"),
        ("", f"data://update?nv={{{{zs}}}}&nl={PACES[0] - PACES.step}&nh={PACES[-1]}&ns={PACES.step}&m=26"),
    ]


def test_auto_turning_panel_starts_from_the_saved_seconds(page: ET.Element) -> None:
    assert row_actions(page, "event://e3?") == [
        ("{{d:p > 0}}", "emit://event/move?m=0&p=0&n=menu_close"),
        ("", "data://update?nv={{d:s}}&nl=0&nh=30&ns=1&m=25"),
    ]


def test_the_panels_share_their_number(page: ET.Element) -> None:
    assert row_actions(page, "event://more?") == [("{{d:nv != d:nh}}", "data://update?nv={{d:nv + d:ns}}")]
    assert row_actions(page, "event://less?") == [("{{d:nv != d:nl}}", "data://update?nv={{d:nv - d:ns}}")]
    assert '"z": {{d:nv}}' in page.find("json").get("cfg")


def test_the_lowest_step_of_the_smart_panel_goes_back_without_starting(page: ET.Element) -> None:
    assert row_actions(page, "event://start26?") == [
        ("{{d:nv == d:nl}}", "emit://event/move?m=0&p={{d:p}}&n=menu_close"),
        ("", "emit://event/move?m=0&p=0&n=smart"),
    ]
    assert PACES[0] - PACES.step not in PACES  # 2 不是合法速度，正好用来表示关闭


def test_starting_auto_turning_takes_over_from_smart_turning(page: ET.Element) -> None:
    rows = row_actions(page, "event://start25?")
    assert ("{{ls == 2}}", "dataSource://update?ls=0") in rows  # 不清的话 d9 比服务先到，自动翻页被关掉
    assert rows[-1] == (None, "emit://event/move?m=0&p={{d:p}}&n=auto")


def test_every_state_is_reachable_and_goes_somewhere(page: ET.Element) -> None:
    seen, waiting = set(), [0]
    while waiting:
        m = waiting.pop()
        if m not in seen:
            seen.add(m)
            uris = [*key_targets(page, m).values(), *(action for _, action in row_actions(page, f"event://e{m}?"))]
            waiting += [int(n) for v in uris for n in re.findall(r"[?&]m=(\d+)", v)]
    assert seen >= {0, *range(1, 8), *range(11, 16), *range(21, 25)}
    for m in (*range(21, 25), 25, 26):  # 这几个由事件进出
        assert key_targets(page, m)["enter"].startswith("emit://event/")


def panel(page: ET.Element, tag: str) -> ET.Element:
    return next(e for e in page.iter("view") if e.get(f"{A}elif") == tag)


def test_main_menu_has_seven_items_inside_the_text_mask(page: ET.Element) -> None:
    menu = panel(page, "{{d:m > 0}}")
    top, height = int(menu.get("top")), int(menu.get("height"))
    assert (top, height) == (28, 242) and top + height <= 272  # 正文遮罩的范围，全在里面才一起变暗
    (box,) = [e for e in menu if e.tag == "view"]
    assert box.get("top") == "{{tt[d:m].n}}" and box.get("height") == "32"
    assert [agent.PAGE_TABLES["tt"][m]["n"] for m in range(1, 8)] == list(range(0, 193, 32))
    labels = [e for e in menu if e.tag == "label"]
    assert {e.get("height") for e in labels} == {"32"} and {e.get("font-size") for e in labels} == {"22"}
    items = [e.get("text") for e in labels if f"{A}else" not in e.attrib]  # a:else 是同一行的另一种文字
    assert items == ["返回阅读", "停止智能翻页", "停止自动翻页", "显示设置", "停止听书", "下一章", "上一章"]
    assert labels[1].get(f"{A}if") == "{{ls == 2}}" and labels[3].get(f"{A}if") == "{{d:p > 0}}" and labels[6].get(f"{A}if") == "{{ls == 1}}"


def test_display_menu_has_five_items(page: ET.Element) -> None:
    menu = panel(page, "{{d:m > 10}}")
    assert (menu.get("top"), menu.get("height")) == ("44", "202") and 44 * 2 + 202 == 290  # 上下居中
    (box,) = [e for e in menu if e.tag == "view"]
    assert box.get("top") == "{{tt[d:m].n}}" and box.get("height") == "36"
    assert [agent.PAGE_TABLES["tt"][m]["n"] for m in range(11, 16)] == list(range(0, 145, 36))
    texts = [e.get("text") for e in menu if e.tag == "label"]
    assert texts == ["字号  {{fs}}", "粗细  粗体", "粗细  常规", "亮度  {{d:bv}}%", "真机亮度  {{d:ob}}%", "返回"]


def test_the_panel_says_which_of_the_two_it_is(page: ET.Element) -> None:
    box = next(e for e in page.iter("view") if e.get(f"{A}if") == "{{d:m > 24}}")
    texts = [e.get("text") for e in box.iter("label")]
    assert texts == ["自动翻页", "智能翻页", "{{d:nv}}", "秒", "字/秒", "关闭", "单击返回阅读", "每页停留时间", "每秒阅读字数"]


def test_the_left_column_shows_smart_turning_after_the_connection_warnings(page: ET.Element) -> None:
    (left, *_) = [e for e in page.iter("view") if e.get("width") == "80"]
    chain = [e.get(f"{A}if", e.get(f"{A}elif")) for e in left if e.get(f"{A}if") or e.get(f"{A}elif")]
    assert chain == ["{{d:net == 0}}", "{{d:bt == 0}}", "{{i == hi}}", "{{i == lo}}", "{{ls > 0}}", "{{d:p > 0}}"]
    edges = [e for e in left if e.get(f"{A}elif") in ("{{i == hi}}", "{{i == lo}}")]
    assert {e.get("text") for e in edges} == {"网络卡顿"}
    engine = next(e for e in left if e.get(f"{A}elif") == "{{ls > 0}}")
    assert [(e.get("text"), e.get(f"{A}if"), e.get(f"{A}elif")) for e in engine] == [
        ("智能翻页", "{{ls == 2}}", None),
        ("听书", None, "{{d:m > 0}}"),  # 读着的时候左栏不用写
        ("已暂停", "{{d:m > 0}}", None),
    ]


def test_the_dimming_masks_cover_the_screen_and_come_last(page: ET.Element) -> None:
    body = page.find("view")
    masks = [e for e in body if "lv *" in e.get("bg-opa", "")]
    assert list(body)[-len(masks):] == masks and len(masks) == 7  # 放在最后才盖得住菜单
    rects = [(int(e.get("left")), int(e.get("top")), int(e.get("width")), int(e.get("height"))) for e in masks]
    assert sum(w * h for _, _, w, h in rects) == 640 * 290  # 不重叠时面积相等就是铺满
    for i, (x, y, w, h) in enumerate(rects):
        assert x >= 0 and y >= 0 and x + w <= 640 and y + h <= 290
        assert not any(x < a + c and a < x + w and y < b + d and b < y + h for a, b, c, d in rects[i + 1:])


def test_the_text_mask_stops_just_above_the_page_number(page: ET.Element) -> None:
    masks = [e for e in page.find("view") if "lv *" in e.get("bg-opa", "")]
    text, footer = masks[0], masks[2]
    assert int(text.get("top")) + int(text.get("height")) == int(footer.get("top")) == 272  # 页码从 y=272 开始，不能再往下


def test_display_is_reported_only_on_save(page: ET.Element) -> None:
    rows = [*page.iter("event"), *page.iter("key-event")]
    assert [v for e in rows for v in e.attrib.values() if re.search(r"n(ame)?=display", v)] == ["emit://event/move?m={{d:m - 10}}&p={{d:p}}&n=display"]
    assert set(re.findall(r'"(\w+)":', page.find("json").get("cfg"))) == {"slot", "v", "f", "w", "b", "o", "z"}  # 和上面的 display() 一致


def test_brightness_limits_and_masks_match_the_server(page: ET.Element) -> None:
    rows = {e.get("match"): e for e in page.iter("event")}
    assert rows["event://s23a?"].get(f"{A}if") == f"{{{{d:bv != {BRIGHTNESS_LEVELS[-1]}}}}}"
    assert rows["event://s23b?"].get(f"{A}if") == f"{{{{d:bv != {BRIGHTNESS_LEVELS[0]}}}}}"  # 超出的档位服务端不认，保存时被丢掉
    (lifecycle,) = page.iter("lifecycle-event")
    assert lifecycle.get("on-load") == "data://update?bv={{s.b}}&lv={{s.lv}}&ob={{s.ob}}"
    darkest = (100 - BRIGHTNESS_LEVELS[0]) // 10
    masks = {int(n) for e in page.iter("view") for n in re.findall(r"d:lv \* (\d+)", e.get("bg-opa", ""))}
    assert masks and max(masks) * darkest <= 255  # 超出 255 回绕，最暗的一档反而变亮


def test_bindings_leave_room_under_the_kernel_limit(page: ET.Element) -> None:
    # 超过 128 个内核直接丢属性。事件行不算，json 算。模拟器日志的 bindings= 才是真实的数（现在 108），这样数只是估计，失败时去那里看
    events = {"event", "key-event", "lifecycle-event", "wear-event", "connection-event"}
    assert sum(v.count("{{") for el in page.iter() if el.tag not in events for v in el.attrib.values()) + 6 <= 128


def test_font_panel_steps_through_the_native_sizes(page: ET.Element) -> None:
    def walk(table: str, start: int) -> list[int]:
        sizes = [start]
        for _ in TEXT_SIZES:
            sizes.append(agent.PAGE_TABLES[table][sizes[-1]]["n"])
        return sizes

    assert row_actions(page, "event://s21a?") == [(None, "dataSource://update?fs={{up[fs].n}}")]
    assert row_actions(page, "event://s21b?") == [(None, "dataSource://update?fs={{dn[fs].n}}")]
    assert walk("up", TEXT_SIZES[0]) == [*TEXT_SIZES, TEXT_SIZES[-1]]
    assert walk("dn", TEXT_SIZES[-1]) == [*reversed(TEXT_SIZES), TEXT_SIZES[0]]


def test_page_conditions_have_no_less_than(page: ET.Element) -> None:
    # 转义后的小于号比较恒假（2026-11-01 模拟器：fs = 16 时 fs < 24 不成立），要写成常量在左的 >
    assert not any("<" in v or "&lt;" in v for e in page.iter() for v in e.attrib.values())


def test_page_data_matches_the_fields_the_server_sends(session: AgentSession) -> None:
    session.start(init()["payload"])
    assert set(json.loads(DATA.read_text(encoding="utf-8"))) == set(session.sent)


def test_settings_the_text_page_cannot_show_are_kept(opened: AgentSession) -> None:
    library = opened.library
    assert opened.book is not None
    library.save(library.view(opened.book, font_size=26))
    library.set_display(weight=5, brightness=100, spacing=170)
    reopened = AgentSession("s2", opened.tools, library)
    reopened.start(init(session="s2")["payload"])
    assert reopened.size in (24, 28) and reopened.bold
    action = {"pageUuid": reopened.page_uuid, "action": {"name": "turn", "params": {"slot": 1}}}
    reopened.handle({"type": "UI_ACTION", "payload": action})
    assert library.position(reopened.book)[1] == 26
    display(reopened, f=reopened.size, w=700, b=60)  # 页面总是三个值一起发
    assert library.display == Display(5, 60, 170)  # 没切换粗细就不动六档
    assert library.position(reopened.book)[1] == 26  # 没改字号就不动页图版的字号
    display(reopened, f=28, w=700, b=60)
    assert library.position(reopened.book)[1] == reopened.saved_size == 28


# ---------------------------------------------------------------------- 和另一个实例共用进度


@pytest.fixture
def other(opened: AgentSession) -> Library:
    """页图版：另一个进程，只通过进度文件和这边打交道。"""
    return Library(opened.library.books_dir, opened.library.state_file)


def read_elsewhere(other: Library, s: AgentSession, percent: float = 78) -> tuple[int, int]:
    """在另一个实例跳到别处、换字号，返回文件里的位置。"""
    assert s.book is not None
    book = other.book(s.book.id)
    assert book is not None
    v = other.jump(book, percent=percent)
    assert v is not None
    other.save(other.view(book, v.offset, 30))
    return other.position(book)


def on_page(out: list[dict[str, Any]]) -> dict[str, Any]:
    return {k: v for part in shown(out) for k, v in part.items()}


def test_reconnecting_takes_up_the_other_instances_place(opened: AgentSession, other: Library) -> None:
    turn(opened, 1)
    where = read_elsewhere(other, opened)
    voice_turns = opened.voice_turns
    page = on_page(opened.resumed())
    assert opened._current().offset == where[0] and opened.saved_size == 30 and opened.size == 28
    assert page[f"s{opened.slot}"] == opened._current().text and page["fs"] == 28
    assert page["v"] == voice_turns + 1  # 页面的自动翻页要重新倒计时
    assert opened.library.position(opened.book) == where  # 重连不写文件
    turn(opened, 2)
    offset, size = opened.library.position(opened.book)
    assert offset > where[0] and size == 30


def test_a_swipe_lands_on_the_other_instances_page(opened: AgentSession, other: Library) -> None:
    where = read_elsewhere(other, opened)
    page = on_page(turn(opened, 1))
    assert opened.library.position(opened.book) == where  # 不盖回旧值，也不多翻一页
    assert opened.slot == 1 and page["s1"] == opened._current().text and page["fs"] == 28
    voice_turns = opened.voice_turns
    turn(opened, 2)
    offset, size = opened.library.position(opened.book)
    assert offset > where[0] and size == 30 and opened.voice_turns == voice_turns


def test_a_size_the_text_page_cannot_show_is_kept_when_taken_up(opened: AgentSession, other: Library) -> None:
    book = other.book(opened.book.id)
    other.save(other.view(book, font_size=26))
    turn(opened, 1)
    assert opened.size == 24 and opened.saved_size == 26
    turn(opened, 2)
    assert other.position(book)[1] == 26


def test_turning_on_alone_never_jumps(opened: AgentSession, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [opened._walk(k) for k in range(1, 25)]
    expected = [opened._chapter_pages(c)[p].offset for c, p in pages]
    monkeypatch.setattr(opened, "_load_position", lambda: pytest.fail("自己写的进度不算别的实例写的"))
    offsets = []
    for k in range(1, 25):
        turn(opened, k % SLOTS)
        offsets.append(opened.library.position(opened.book)[0])
        assert opened.synced_ts == opened.library.last_read(opened.book.id)
    assert offsets == expected


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_an_engines_own_saves_are_not_the_other_instance(opened: AgentSession, clock: list[float], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(opened, "_load_position", lambda: pytest.fail("自己写的进度不算别的实例写的"))
    say(opened, mode)
    start = opened.library.position(opened.book)[0]
    run_clock(opened, clock, 120)
    assert opened.library.position(opened.book)[0] > start
    assert opened.synced_ts == opened.library.last_read(opened.book.id)
    following = opened._walk(1)
    turn(opened, (opened.slot + 1) % SLOTS)
    assert (opened.chapter, opened.page) == following


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_a_swipe_during_an_engine_goes_on_from_the_other_instances_place(opened: AgentSession, other: Library, clock: list[float], mode: str) -> None:
    say(opened, mode)
    run_clock(opened, clock, 5)
    where = read_elsewhere(other, opened)
    turn(opened, 1)
    assert opened.listening and opened.listen_para == where[0]
    assert opened._current().offset == where[0] and opened.library.position(opened.book) == where


def test_reconnecting_during_an_engine_goes_on_from_the_other_instances_place(opened: AgentSession, other: Library, clock: list[float]) -> None:
    say(opened, "开始听书")
    run_clock(opened, clock, 5)
    where = read_elsewhere(other, opened)
    out = opened.resumed()
    assert actions(out, "STOP_OUTPUT") and actions(out, "SPEAK")
    assert opened.listening and opened.listen_para == where[0] and opened.library.position(opened.book) == where


def test_reconnecting_during_an_engine_can_go_back_to_the_other_instances_place(opened: AgentSession, other: Library, clock: list[float]) -> None:
    say(opened, "跳到百分之六十")
    say(opened, "开始听书")
    run_clock(opened, clock, 5)
    where = read_elsewhere(other, opened, percent=10)
    opened.resumed()
    assert opened.listen_para == where[0]  # 不是重读刚才那段


@pytest.mark.parametrize(("text", "moves", "size"), [("下一页", True, 30), ("下一章", True, 30), ("字大一点", False, 32)])
def test_voice_goes_on_from_the_other_instances_place(opened: AgentSession, other: Library, text: str, moves: bool, size: int) -> None:
    where = read_elsewhere(other, opened)
    say(opened, text)
    offset, saved = opened.library.position(opened.book)
    assert (offset > where[0] if moves else offset == where[0]) and saved == size


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
@pytest.mark.parametrize("text", ["下一页", "下一章"])
def test_voice_during_an_engine_goes_on_from_the_other_instances_place(opened: AgentSession, other: Library, clock: list[float], mode: str, text: str) -> None:
    say(opened, mode)
    where = read_elsewhere(other, opened)
    page = on_page(say(opened, text))
    offset, saved = opened.library.position(opened.book)
    assert offset > where[0] and saved == 30 and opened.size == 28 and page["fs"] == 28
    assert opened.listening and opened.listen_para == opened._current().offset == offset


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_a_jump_nowhere_during_an_engine_goes_on_from_the_other_instances_place(opened: AgentSession, other: Library, clock: list[float], mode: str) -> None:
    say(opened, mode)
    where = read_elsewhere(other, opened)
    say(opened, "跳到第九十九章")
    assert opened.library.position(opened.book) == where
    assert opened.listening and opened.listen_para == where[0]


def test_starting_the_other_engine_does_not_take_up_the_place_midway(opened: AgentSession, other: Library, clock: list[float]) -> None:
    say(opened, "智能翻页")
    read_elsewhere(other, opened)
    before = opened._current().offset
    assert say(opened, "智能翻页") == []  # 已经开着：这句什么都不动，下一滑才核对
    assert opened._current().offset == before and opened.listening


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_font_during_an_engine_stays_with_the_engine(opened: AgentSession, other: Library, clock: list[float], mode: str) -> None:
    say(opened, mode)
    run_clock(opened, clock, 5)
    read_elsewhere(other, opened)
    before = opened._current().offset
    say(opened, "字大一点")
    assert opened._current().offset == before and opened.listening  # 引擎接着走，位置归它管


def test_a_percent_jump_keeps_the_other_instances_size(opened: AgentSession, other: Library) -> None:
    read_elsewhere(other, opened)
    say(opened, "跳到百分之二十")
    assert opened.library.position(opened.book)[1] == 30


def test_a_jump_elsewhere_is_not_undone_by_a_failed_chapter(opened: AgentSession, other: Library) -> None:
    where = read_elsewhere(other, opened)
    say(opened, "跳到第九百九十九章")
    assert opened.library.position(opened.book) == where


@pytest.mark.parametrize(
    ("percent", "act"),
    [
        (78, lambda s: say(s, "跳到第九十九章")),
        (78, lambda s: say(s, "跳到百分之一千")),
        (78, lambda s: say(s, "往后翻一千零一页")),
        (100, lambda s: say(s, "下一章")),
        (0, lambda s: say(s, "上一章")),
        (100, lambda s: tap(s, "next_chapter")),
        (0, lambda s: tap(s, "prev_chapter")),
    ],
)
def test_a_command_that_goes_nowhere_still_shows_the_other_instances_page(opened: AgentSession, other: Library, percent: float, act: Any) -> None:
    say(opened, "跳到百分之四十")
    where = read_elsewhere(other, opened, percent=percent)
    page = on_page(act(opened))
    assert opened.library.position(opened.book) == where and opened.saved_size == 30
    assert page[f"s{opened.slot}"] == opened._current().text and page["fs"] == 28 and page["v"] == opened.voice_turns
    assert opened.sent == opened._slot_data()  # 页面和服务端没有差别
    following = opened._walk(1)
    turn(opened, opened.slot + 1)
    if following is not None:  # 刚才看到的就是那一页，这一滑接着往后，不跳过它
        assert (opened.chapter, opened.page) == following


def test_a_command_that_goes_nowhere_sends_nothing_new_on_its_own(opened: AgentSession) -> None:
    assert shown(say(opened, "跳到第九十九章")) == []


@pytest.mark.parametrize("start", [lambda s: say(s, "开始听书"), lambda s: say(s, "智能翻页"), lambda s: tap(s, "listen"), lambda s: tap(s, "smart")])
def test_engines_start_from_the_other_instances_place(opened: AgentSession, other: Library, clock: list[float], start: Any) -> None:
    where = read_elsewhere(other, opened)
    start(opened)
    assert opened.listening and opened.listen_para == where[0] and opened.library.position(opened.book) == where


@pytest.mark.parametrize("text", ["今天吃什么", "我看到哪了"])
def test_words_that_leave_the_page_do_not_take_up_the_other_instances_place(opened: AgentSession, other: Library, text: str) -> None:
    read_elsewhere(other, opened)
    before = opened._current().offset
    say(opened, text)
    assert opened._current().offset == before  # 这句不更新页面，留给下一次操作，那一滑才看得到新位置


def test_saving_settings_keeps_the_other_instances_place(opened: AgentSession, other: Library) -> None:
    where = read_elsewhere(other, opened)
    display(opened, b=40)
    assert opened.library.position(opened.book) == where and other.display.brightness == 40
    assert opened._current().offset == where[0] and opened.sent["fs"] == 28


def test_saving_nothing_new_leaves_the_book_record_alone(opened: AgentSession) -> None:
    turn(opened, 1)
    read = json.loads(opened.library.state_file.read_text(encoding="utf-8"))[opened.book.id]
    display(opened)
    display(opened, z=30)
    assert json.loads(opened.library.state_file.read_text(encoding="utf-8"))[opened.book.id] == read


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_changing_the_pace_during_an_engine_keeps_the_other_instances_place(opened: AgentSession, other: Library, clock: list[float], mode: str) -> None:
    say(opened, mode)
    where = read_elsewhere(other, opened)
    tap(opened, "smart", z=30)
    assert opened.library.position(opened.book) == where and other.display.pace == 30


def test_changing_the_size_during_an_engine_is_saved(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    display(opened, f=18)
    assert opened.library.position(opened.book)[1] == 18 and opened.listening


def test_a_size_changed_on_the_page_beats_the_other_instance(opened: AgentSession, other: Library) -> None:
    where = read_elsewhere(other, opened)
    display(opened, f=18)
    assert opened.library.position(opened.book) == (where[0], 18) and opened.size == 18


def test_saving_settings_keeps_the_other_instances_brightness(opened: AgentSession, other: Library) -> None:
    other.set_display(brightness=40)
    display(opened, w=700, b=100)  # 页面拿着打开时的 100
    assert other.display.brightness == 40 and other.display.weight == 4


def test_a_brightness_sent_to_the_page_never_reaches_what_it_reports(opened: AgentSession, other: Library) -> None:
    display(opened, b=60)
    other.set_display(brightness=40)
    assert on_page(turn(opened, 1))["s"]["b"] == 40  # d:bv 只在打开时取 s.b，这一下页面上还是 60
    display(opened, z=30)
    assert other.display.brightness == 40


def test_a_brightness_changed_on_the_page_beats_the_other_instance(opened: AgentSession, other: Library) -> None:
    other.set_display(brightness=40)
    display(opened, b=70)
    assert other.display.brightness == 70
    other.set_display(brightness=40)
    display(opened, b=100)  # 打开时就是 100，也算改了
    assert other.display.brightness == 100


def test_a_save_elsewhere_at_the_same_place_does_not_disturb(opened: AgentSession, other: Library) -> None:
    turn(opened, 1)
    book = other.book(opened.book.id)
    following = opened._walk(1)
    other.save(replace(other.view(book), offset=other.position(book)[0]))  # open_book 就这样记“最近阅读”
    voice_turns = opened.voice_turns
    turn(opened, 2)
    assert (opened.chapter, opened.page) == following and opened.voice_turns == voice_turns


def size_elsewhere(other: Library, s: AgentSession, size: int = 30) -> None:
    """另一个实例只换字号，位置就是这边页首的这个字。"""
    book = other.book(s.book.id)
    other.save(replace(other.view(book, s._current().offset, size), offset=s._current().offset))


def test_a_swipe_after_a_size_changed_elsewhere_still_turns(opened: AgentSession, other: Library) -> None:
    turn(opened, 1)
    size_elsewhere(other, opened)
    offset, voice_turns = opened._current().offset, opened.voice_turns
    page = on_page(turn(opened, 2))
    assert opened.size == 28 and opened.saved_size == 30 and page["fs"] == 28
    assert opened._current().offset > offset and opened.voice_turns == voice_turns


@pytest.mark.parametrize("mode", ["开始听书", "智能翻页"])
def test_a_swipe_during_an_engine_after_a_size_changed_elsewhere_still_turns(opened: AgentSession, other: Library, clock: list[float], mode: str) -> None:
    say(opened, mode)
    size_elsewhere(other, opened)
    offset, voice_turns = opened._current().offset, opened.voice_turns
    turn(opened, 1)
    assert opened._current().offset > offset and opened.saved_size == 30 and opened.listen_para == opened._current().offset
    assert opened.voice_turns == voice_turns


def test_a_book_opened_elsewhere_at_its_very_end_does_not_eat_the_swipe(opened: AgentSession, other: Library, books_dir: Path) -> None:
    (books_dir / "尾章.txt").write_text(make_novel(chapters=3, empty=(3,)), encoding="utf-8")
    say(opened, "打开《尾章》")
    say(opened, "跳到百分之百")
    other.scan()
    book = other.book(opened.book.id)
    other.jump(book, percent=100)
    assert other.position(book)[0] == book.length > opened._current().offset  # 书尾的空章也算全书长度
    position = opened.chapter, opened.page
    turn(opened, SLOTS - 1)
    assert (opened.chapter, opened.page) == (position[0], position[1] - 1)


def test_a_write_elsewhere_right_after_ours_is_not_taken_for_ours(opened: AgentSession, other: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    store = opened.library._store
    where: list[tuple[int, int]] = []

    def then_elsewhere(changes: dict[str, dict[str, Any]]) -> None:
        store(changes)
        monkeypatch.setattr(opened.library, "_store", store)
        where.append(read_elsewhere(other, opened))

    monkeypatch.setattr(opened.library, "_store", then_elsewhere)
    turn(opened, 1)
    assert where and opened.library.last_read(opened.book.id) > opened.synced_ts  # 这边记的是自己写的时间
    turn(opened, 2)
    assert opened.library.position(opened.book) == where[0]


@pytest.mark.parametrize("name", ["last_read", "position"])
def test_a_write_elsewhere_while_taking_up_the_file_is_not_lost(opened: AgentSession, other: Library, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """取时间和读位置之间另一个实例又写了一次：这次没读到的，下一次核对要看得到，不能被下一滑盖掉。"""
    read_elsewhere(other, opened, percent=30)
    reader = getattr(opened.library, name)
    latest: list[tuple[int, int]] = []

    def then_elsewhere(*args: Any) -> Any:
        value = reader(*args)
        monkeypatch.setattr(opened.library, name, reader)
        latest.append(read_elsewhere(other, opened))
        return value

    monkeypatch.setattr(opened.library, name, then_elsewhere)
    opened._load_position()
    turn(opened, 1)
    assert latest and opened.library.position(opened.book)[0] >= latest[0][0]


def test_a_missing_progress_file_does_not_send_the_session_home(opened: AgentSession) -> None:
    turn(opened, 1)
    offset = opened._current().offset
    opened.library.state_file.unlink()
    turn(opened, 2)
    assert opened._current().offset > offset
    assert opened.library.position(opened.book)[0] == opened._current().offset


def test_long_sentences_are_not_commands(opened: AgentSession) -> None:
    before = opened._current().offset
    assert say(opened, "这本书的上一页下一页我都看过了挺好看的你也可以看看") == []
    assert opened._current().offset == before
    assert parse_command("帮我翻到下一页") == ("pages", "1")
    assert parse_command("打开《一个书名特别特别特别特别特别长的小说》") == ("open", "一个书名特别特别特别特别特别长的小说")
    assert parse_command("下一张") == ("chapter", "下一章")


def test_voice_turns_tell_the_page_to_restart_the_countdown(opened: AgentSession, page: ET.Element) -> None:
    before = opened.sent["v"]
    assert "v" not in shown(turn(opened, 1))[0]  # 镜腿翻页页面自己重置
    (update, *_) = shown(say(opened, "下一页"))
    assert update["v"] == before + 1
    rows = [e for e in page.iter("event") if e.get("match") == "event://second?"]
    assert (rows[1].get(f"{A}elif"), rows[1].get("action")) == ("{{d:vs != v}}", "data://update?vs={{v}}&c={{d:p}}")


def test_starting_auto_turning_lines_up_the_voice_turns_first(page: ET.Element) -> None:
    rows = [e for e in page.iter("event") if e.get("match") == "event://start25?"]
    assert rows[1].get("action") == "data://update?p={{d:nv}}&c={{d:nv}}&s={{d:nv}}&vs={{v}}&r=1"  # 不然第一秒只对齐 vs，这一页多停 1 秒
    assert rows[0].get(f"{A}if") == "{{d:r == 0}}"  # 在写 r 之前，emit 排在这一串后面才执行，计时循环只起一个


def test_the_page_knows_when_there_is_no_next_page(opened: AgentSession, page: ET.Element) -> None:
    assert opened.sent["end"] == 0
    say(opened, "跳到百分之百")
    assert opened.sent["end"] == 1
    say(opened, "上一页")
    assert opened.sent["end"] == 0
    rows = [e for e in page.iter("event") if e.get("match") == "event://second?"]
    assert (rows[0].get(f"{A}if"), rows[0].get("action")) == ("{{end == 1}}", "data://update?p=0")  # 不会再翻到空白槽


def test_listening_turns_auto_turning_off_whichever_came_first(page: ET.Element) -> None:
    rows = [e for e in page.iter("event") if e.get("match") == "event://d9"]
    assert [e.get(f"{A}if", e.get(f"{A}elif")) for e in rows[:3]] == ["{{d:p == 0}}", "{{ls > 0}}", "{{d:m == 0}}"]
    assert rows[1].get("action") == "data://update?p=0&r=0"  # r 留着 1 的话下次 start 不会重启计时


# ---------------------------------------------------------------------- 菜单和智能翻页


def test_the_default_pace_is_18(opened: AgentSession) -> None:
    assert opened.pace == 18 and opened.sent["zs"] == 18


def test_opening_the_menu_pauses_smart_turning(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    run_clock(opened, clock, 5)
    page, place = (opened.chapter, opened.page), opened._heard()
    assert tap(opened, "menu_open") == []
    assert run_clock(opened, clock, 300) == []  # 没有翻页，圆点也不换
    assert (opened.chapter, opened.page) == page and opened._heard() == place and opened.listening
    assert tap(opened, "menu_close") == []
    assert opened._heard() == place  # 菜单开着的时间不算
    run_clock(opened, clock, 2)
    assert opened._heard() == place + 2 * opened.pace


def test_the_dot_wait_does_not_run_while_the_menu_is_open(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    due = opened.dot_due
    tap(opened, "menu_open")
    clock[0] += 60
    tap(opened, "menu_close")
    assert opened.dot_due == due + 60


def test_opening_the_menu_twice_keeps_the_first_time(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 10
    tap(opened, "menu_open")  # 关菜单的上报丢了
    assert opened.held_at == clock[0] - 10


def test_a_menu_closed_twice_changes_nothing(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    start = opened.listen_start
    tap(opened, "menu_close")
    assert opened.listen_start == start


def test_the_menu_holds_nothing_when_nothing_runs(opened: AgentSession) -> None:
    assert tap(opened, "menu_open") == [] and opened.held_at is None and not opened.listen_held


def narrate(s: AgentSession, clock: list[float]) -> None:
    say(s, "开始听书")
    run_clock(s, clock, 20)


def test_opening_the_menu_stops_the_voice(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    out = tap(opened, "menu_open")
    assert actions(out, "STOP_OUTPUT") and not opened.listening
    assert opened.sent["ls"] == 1  # 菜单里还要能停掉它
    assert run_clock(opened, clock, 300) == []


def test_closing_the_menu_reads_on_from_the_sentence(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    resume = opened.listen_resume
    run_clock(opened, clock, 300)
    out = tap(opened, "menu_close")
    assert opened.listening and not opened.silent and not opened.menu_open and not opened.listen_held
    assert actions(out, "SPEAK") and opened.listen_base <= resume and opened.sent["ls"] == 1


def test_stopping_listening_from_the_menu_stays_stopped(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    out = tap(opened, "listen")
    assert not actions(out, "SPEAK") and not opened.listening and opened.sent["ls"] == 0
    assert tap(opened, "menu_close") == []
    assert actions(say(opened, "继续"), "SPEAK")  # 还能接着读


@pytest.mark.parametrize("name", ["display", "optics", "hidden"])
def test_the_voice_waits_while_the_menu_stays_open(opened: AgentSession, clock: list[float], name: str) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    assert not actions(tap(opened, name), "SPEAK") and opened.listen_held and opened.sent["ls"] == 1
    assert actions(tap(opened, "menu_close"), "SPEAK")


def test_a_chapter_from_the_menu_reads_from_there(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    chapter = opened.chapter
    tap(opened, "menu_open")
    assert actions(tap(opened, "next_chapter"), "SPEAK")
    assert opened.listening and opened.chapter != chapter and opened.listen_base <= opened._current().offset


def test_smart_turning_from_the_menu_takes_over_the_paused_voice(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    out = tap(opened, "smart", z=20)
    assert opened.listening and opened.silent and not opened.listen_held and opened.sent["ls"] == 2
    assert not spoken(out)


def test_taking_the_glasses_off_with_the_menu_open_stays_stopped(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    tap(opened, "away")
    assert opened.sent["ls"] == 0 and tap(opened, "menu_close") == []


def test_saying_stop_with_the_menu_open_stays_stopped(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    say(opened, "暂停")
    assert opened.sent["ls"] == 0 and tap(opened, "menu_close") == []


def test_saying_go_on_with_the_menu_open_reads_at_once(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    assert actions(say(opened, "继续"), "SPEAK") and opened.listening and not opened.listen_held
    assert not actions(tap(opened, "menu_close"), "SPEAK")


def test_a_reconnect_reads_on_when_the_menu_report_was_lost(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    assert actions(opened.resumed(), "SPEAK") and opened.listening and not opened.menu_open


def test_a_failure_forgets_the_paused_voice(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    opened.failed()
    assert opened.sent["ls"] == 0 and tap(opened, "menu_close") == []


def test_leaving_with_the_menu_open_does_not_read_on(opened: AgentSession, clock: list[float]) -> None:
    narrate(opened, clock)
    tap(opened, "menu_open")
    assert not actions(tap(opened, "exit"), "SPEAK") and opened.closing


def test_saving_display_settings_keeps_the_menu_paused(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 5
    display(opened, b=40)
    assert run_clock(opened, clock, 60) == [] and opened.held_at is not None


def test_a_swipe_means_the_menu_is_closed(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 60
    turn(opened, 1)  # 关菜单的上报丢了
    assert opened.held_at is None
    run_clock(opened, clock, 1)
    assert opened._heard() == opened.listen_base + opened.pace


def test_a_reconnect_does_not_stay_paused(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 60
    opened.resumed()
    assert opened.held_at is None and opened.listening
    run_clock(opened, clock, 1)
    assert opened._heard() == opened.listen_base + opened.pace


@pytest.mark.parametrize("name", ["next_chapter", "prev_chapter"])
def test_a_chapter_from_the_menu_carries_on_from_there(opened: AgentSession, clock: list[float], name: str) -> None:
    opened.handle({"type": "UTTERANCE", "payload": {"text": "下一章"}})
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 60
    tap(opened, name)
    assert opened.held_at is None and opened.listening and opened.listen_base == opened._current().offset
    run_clock(opened, clock, 1)
    assert opened._heard() == opened.listen_base + opened.pace


def test_stopping_from_the_menu_clears_the_pause(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 60
    out = tap(opened, "smart_off")
    assert shown(out)[0]["ls"] == 0 and not opened.listening and opened.held_at is None
    say(opened, "继续")
    assert opened.listening and run_clock(opened, clock, 1) != [] or opened._heard() > opened.listen_base


def test_a_voice_jump_with_the_menu_open_waits_for_the_menu(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 60
    say(opened, "下一章")
    assert opened.listen_base == opened._current().offset and opened.held_at == clock[0]
    assert run_clock(opened, clock, 30) == []
    tap(opened, "menu_close")
    run_clock(opened, clock, 1)
    assert opened._heard() == opened.listen_base + opened.pace


def test_voice_stopping_with_the_menu_open_clears_the_pause(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    say(opened, "暂停")
    assert not opened.listening and opened.held_at is None


def test_listening_from_the_menu_replaces_a_paused_smart_turn(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 60
    out = tap(opened, "listen")
    assert opened.listening and not opened.silent and opened.held_at is None and actions(out, "SPEAK")


def test_an_error_clears_the_pause(opened: AgentSession) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    opened.failed()
    assert opened.held_at is None and not opened.listening


def test_a_long_menu_still_stops_at_the_idle_limit(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += agent.LISTEN_IDLE_SECONDS + 1
    out = opened.listen_tick()
    assert shown(out)[0]["ls"] == 0 and not opened.listening and opened.held_at is None


def test_taking_the_glasses_off_stops_a_paused_smart_turn(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    tap(opened, "away")
    assert not opened.listening and opened.held_at is None


def test_the_screen_going_off_stops_a_paused_smart_turn(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    tap(opened, "hidden")
    assert not opened.listening and opened.held_at is None


def test_starting_auto_turning_stops_smart_turning(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    out = tap(opened, "auto", z=7)
    assert shown(out)[0]["ls"] == 0 and not opened.listening and opened.held_at is None


def test_leaving_the_auto_panel_at_off_keeps_smart_turning(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    clock[0] += 60
    assert tap(opened, "auto", z=0) == []
    assert opened.listening and opened.held_at is None


def test_starting_auto_turning_leaves_listening_alone(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    assert tap(opened, "auto", z=7) == [] and opened.listening  # 页面的 d9 约 1 秒内关掉自动翻页


def test_smart_off_leaves_listening_alone(opened: AgentSession) -> None:
    say(opened, "开始听书")
    assert tap(opened, "smart_off") == [] and opened.listening


def test_starting_smart_turning_twice_keeps_it_going(opened: AgentSession, clock: list[float]) -> None:
    tap(opened, "smart", z=30)
    run_clock(opened, clock, 2)
    place = opened._heard()
    tap(opened, "smart", z=30)
    assert opened.listening and opened._heard() == place


def test_the_ring_edges_are_sent_with_the_page(opened: AgentSession) -> None:
    assert (opened.sent["hi"], opened.sent["lo"]) == (AHEAD, SLOTS - BEHIND)


@pytest.mark.parametrize("slot", range(SLOTS))
def test_the_ring_edges_follow_the_current_slot(opened: AgentSession, slot: int) -> None:
    for step in range(slot):
        turn(opened, step + 1)
    assert opened.sent["hi"] == (slot + AHEAD) % SLOTS and opened.sent["lo"] == (slot - BEHIND) % SLOTS
    assert opened.sent["hi"] == (opened.sent["lo"] + SLOTS - 1) % SLOTS  # 八个槽刚好首尾相接


def test_a_swipe_crossing_an_engine_turn_does_not_skip_a_page(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    seen = opened.voice_turns
    run_clock(opened, clock, 3)
    while opened.voice_turns == seen:
        run_clock(opened, clock, 0.25)  # 服务翻了一页，这条 UPDATE 还在路上
    page = opened.chapter, opened.page
    action = {"pageUuid": opened.page_uuid, "action": {"name": "turn", "params": {"slot": 1, "v": seen}}}
    opened.handle({"type": "UI_ACTION", "payload": action})
    assert (opened.chapter, opened.page) == page and opened.listening


def test_a_swipe_after_the_page_caught_up_still_turns(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    page = opened._current().offset
    action = {"pageUuid": opened.page_uuid, "action": {"name": "turn", "params": {"slot": 1, "v": opened.voice_turns}}}
    opened.handle({"type": "UI_ACTION", "payload": action})
    assert opened._current().offset > page


def test_a_swipe_back_crossing_an_engine_turn_goes_back_from_what_was_seen(opened: AgentSession, clock: list[float]) -> None:
    turn(opened, 1)
    turn(opened, 2)
    say(opened, "智能翻页")
    seen = opened.voice_turns
    start = opened._current().offset
    while opened.voice_turns == seen:
        run_clock(opened, clock, 0.25)
    action = {"pageUuid": opened.page_uuid, "action": {"name": "turn", "params": {"slot": 1, "v": seen}}}
    opened.handle({"type": "UI_ACTION", "payload": action})
    assert opened._current().offset < start


def test_a_swipe_at_the_ring_edge_catches_the_server_up(opened: AgentSession) -> None:
    """卡顿时前面几次上报丢了，翻不动的这一滑还会上报，服务一次补上。"""
    pages = [opened.sent[f"s{k}"] for k in range(AHEAD + 1)]
    update = shown(turn(opened, AHEAD))
    assert opened.slot == AHEAD and opened.sent["hi"] == (2 * AHEAD) % SLOTS
    assert update and opened._current().text == pages[AHEAD]


def test_a_repeated_slot_resends_the_ring_without_turning(opened: AgentSession, clock: list[float]) -> None:
    """页面只有翻不动才报同一个槽：更新丢了的话页面的 hi 还是旧的，不全量重发就一直卡在那。"""
    turn(opened, 1)
    page = opened._current().offset
    update = {k: v for part in shown(turn(opened, 1)) for k, v in part.items()}
    assert opened._current().offset == page
    assert update["hi"] == (1 + AHEAD) % SLOTS and all(f"s{k}" in update for k in range(SLOTS))
    assert not {"ls", "fs", "fw"} & set(update)  # 页面自己也写这几个，可能比这边新
    assert not set(agent.PAGE_TABLES) & set(update)  # 卡顿时能少发就少发，这几张表打开时收到过


def test_repeated_slots_resend_at_most_every_few_seconds(opened: AgentSession, clock: list[float]) -> None:
    """卡顿时一串翻不动的上报一起到，只是晚了，不能一滑发一环。"""
    turn(opened, 1)
    assert turn(opened, 1) != []
    assert turn(opened, 1) == []
    clock[0] += agent.RESEND_SECONDS
    assert turn(opened, 1) != []


def test_the_page_gets_the_mask_level_directly(opened: AgentSession) -> None:
    assert opened.sent["s"]["lv"] == 0
    display(opened, b=40)
    assert opened._slot_data()["s"] == {"b": 40, "o": opened.library.page_settings(opened.book)["o"], "lv": 6, "ob": 50}


def test_the_tools_of_the_session_are_logged(session: AgentSession, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger=agent.__name__)
    session.start(init(tools=[{"type": "function", "name": "change_brightness"}])["payload"])
    assert "change_brightness" in caplog.text


def test_voice_starting_smart_turning_with_the_menu_open_waits_for_the_menu(opened: AgentSession, clock: list[float]) -> None:
    tap(opened, "menu_open")
    out = say(opened, "智能翻页")
    assert shown(out)[0]["ls"] == 2 and opened.held_at == clock[0]  # 菜单项要变成“停止智能翻页”
    place = opened._heard()
    assert run_clock(opened, clock, 60) == [] and opened._heard() == place
    tap(opened, "menu_close")
    run_clock(opened, clock, 1)
    assert opened._heard() == place + opened.pace


def test_pausing_and_continuing_by_voice_in_the_menu_stays_paused(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    say(opened, "暂停")
    say(opened, "继续")
    assert opened.listening and opened.held_at == clock[0]
    assert run_clock(opened, clock, 60) == []


@pytest.mark.parametrize("name", ["hidden", "away"])
def test_the_menu_stays_open_across_the_screen_going_off(opened: AgentSession, clock: list[float], name: str) -> None:
    say(opened, "智能翻页")
    tap(opened, "menu_open")
    tap(opened, name)
    assert not opened.listening
    say(opened, "继续")
    assert opened.listening and opened.held_at is not None  # 页面的 m 没动，菜单还开着


def test_a_held_voice_jump_back_survives_a_reconnect(opened: AgentSession, clock: list[float]) -> None:
    for slot in range(1, 4):
        turn(opened, slot)
    say(opened, "智能翻页")
    run_clock(opened, clock, 1)
    tap(opened, "menu_open")
    say(opened, "回到开头")
    place = opened._current().offset
    opened.resumed()
    assert opened._current().offset == place and opened.listen_base == place


def test_restarting_smart_turning_restates_it_to_the_page(opened: AgentSession) -> None:
    say(opened, "智能翻页")  # 页面选自动翻页时自己清了 ls，那条上报丢了
    assert shown(tap(opened, "smart"))[0]["ls"] == 2


def report(s: AgentSession, name: str, slot: int, **params: Any) -> list[dict[str, Any]]:
    action = {"pageUuid": s.page_uuid, "action": {"name": name, "params": {"slot": slot, "v": s.voice_turns, **params}}}
    return s.handle({"type": "UI_ACTION", "payload": action})


def test_every_report_catches_up_with_lost_swipes(opened: AgentSession) -> None:
    start = opened._current().offset
    report(opened, "menu_open", 3)
    assert opened.slot == 3 and opened._current().offset > start


def test_leaving_saves_the_page_on_screen_after_lost_swipes(opened: AgentSession) -> None:
    report(opened, "exit", 2)
    assert opened.library.position(opened.book)[0] == opened._current().offset > 0 and opened.closing


def test_a_report_on_the_current_slot_does_not_resend(opened: AgentSession) -> None:
    assert report(opened, "menu_open", opened.slot) == []


# ---------------------------------------------------------------------- 真机亮度


def brightness_tool() -> dict[str, Any]:
    return {"type": "function", "name": "change_brightness", "parameters": {"type": "object"}}


@pytest.fixture
def lit(session: AgentSession) -> AgentSession:
    session.start(init(tools=[brightness_tool()])["payload"])
    return session


def calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m["payload"] for m in messages if m["type"] == "TOOL_CALL"]


def test_opening_the_book_does_not_touch_the_glasses(session: AgentSession) -> None:
    out = session.start(init(tools=[brightness_tool()])["payload"])
    assert calls(out) == [] and session.sent["s"]["ob"] == 50


def test_changing_the_real_brightness_calls_the_tool_and_is_remembered(lit: AgentSession) -> None:
    (call,) = calls(tap(lit, "optics", o=30))
    assert call["name"] == "change_brightness" and call["arguments"] == {"operation": "SET", "value": 30}
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", call["callId"])
    assert lit.library.display.optics == 30
    reopened = AgentSession("s2", lit.tools, lit.library)
    out = reopened.start(init(session="s2", tools=[brightness_tool()])["payload"])
    assert reopened.sent["s"]["ob"] == 30 and calls(out) == []  # 只记住，重开不下发


def test_every_call_has_its_own_id(lit: AgentSession) -> None:
    ids = [calls(tap(lit, "optics", o=v))[0]["callId"] for v in (10, 20, 30)]
    assert len(set(ids)) == 3


def test_an_unchanged_real_brightness_is_not_sent(lit: AgentSession) -> None:
    assert tap(lit, "optics", o=50) == []


@pytest.mark.parametrize("bad", [-10, 110, 35, "30", None, True, False, 30.0])
def test_bad_real_brightness_is_ignored(lit: AgentSession, bad: Any) -> None:
    assert tap(lit, "optics", o=bad) == [] and lit.optics == 50 and lit.library.display.optics == 50


def test_saving_other_settings_does_not_touch_the_glasses(lit: AgentSession) -> None:
    assert calls(display(lit, b=40, o=70)) == [] and lit.optics == 50


def test_a_missing_tool_is_said_once(opened: AgentSession) -> None:
    (first,) = actions(tap(opened, "optics", o=30), "SPEAK")
    assert "亮度" in first["text"] and actions(tap(opened, "optics", o=40), "SPEAK") == []
    assert opened.library.display.optics == 40  # 用户设的数照样记住


@pytest.mark.parametrize("status", ["FAILED", "TIMEOUT", "CANCELED"])
def test_a_failed_change_is_said_once(lit: AgentSession, status: str) -> None:
    (call,) = calls(tap(lit, "optics", o=30))
    result = {"type": "TOOL_RESULT", "payload": {"callId": call["callId"], "status": status}}
    assert len(actions(lit.handle(result), "SPEAK")) == 1
    assert lit.handle(result) == []


def test_a_dispatched_change_says_nothing_and_rearms_the_warning(lit: AgentSession) -> None:
    (call,) = calls(tap(lit, "optics", o=30))
    failed = {"type": "TOOL_RESULT", "payload": {"callId": call["callId"], "status": "FAILED"}}
    done = {"type": "TOOL_RESULT", "payload": {"callId": call["callId"], "status": "DISPATCHED"}}
    lit.handle(failed)
    assert lit.handle(done) == []
    assert len(actions(lit.handle(failed), "SPEAK")) == 1


def test_a_failed_change_does_not_talk_over_listening(lit: AgentSession) -> None:
    say(lit, "开始听书")
    (call,) = calls(tap(lit, "optics", o=30))
    assert lit.handle({"type": "TOOL_RESULT", "payload": {"callId": call["callId"], "status": "FAILED"}}) == []


def test_the_result_is_logged_to_see_what_it_carries(lit: AgentSession, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger=agent.__name__)
    lit.handle({"type": "TOOL_RESULT", "payload": {"callId": "optics_1", "status": "DISPATCHED", "result": {"current": 40}}})
    assert "current" in caplog.text


def test_the_page_reports_every_real_brightness_swipe(page: ET.Element) -> None:
    assert row_actions(page, "event://s24a?") == [(f"{{{{d:ob != {OPTICS_LEVELS[-1]}}}}}", f"data://update?ob={{{{d:ob + {OPTICS_LEVELS.step}}}}}")]
    assert row_actions(page, "event://s24b?") == [(f"{{{{d:ob != {OPTICS_LEVELS[0]}}}}}", f"data://update?ob={{{{d:ob - {OPTICS_LEVELS.step}}}}}")]
    assert row_actions(page, "event://s24") == [(None, "svc://ui-action?mode=mcp&name=optics&params={{json:cfg}}")]


def test_the_real_brightness_panel_shows_the_number(page: ET.Element) -> None:
    box = panel(page, "{{d:m > 20}}")
    texts = [e.get("text") for e in box if e.tag == "label"]
    assert "真机" in texts and "{{d:ob}}%" in texts


def test_the_percent_is_shared_and_not_in_the_slots(opened: AgentSession, page: ET.Element) -> None:
    assert sum(e.get("text") == "{{pc}}" for e in page.iter("label")) == 1
    assert not any(f"r{i}" in opened.sent for i in range(SLOTS)) and opened.sent["pc"] == "0%"
