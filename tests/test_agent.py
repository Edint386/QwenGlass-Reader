"""Agent 文字传输版：协议、环形缓冲、语音命令和页面模板。"""

from __future__ import annotations

import json
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
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
from glassreader.config import Settings
from glassreader.library import Display, Library
from glassreader.mcp_tools import ReaderTools
from glassreader.server import create_app

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
        assert all(sum(widths[c] for c in line) <= BODY_WIDTH + size * 2 for line in lines)  # 悬挂标点可以略超
    joined = "".join(p.text for p in pages).replace("\n", "").replace("　", "")
    assert joined == "".join(para for _, para in book.chapters[chapter].paragraphs).replace("　", "")


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
    ("下一页", ("pages", "1")),
    ("上一页。", ("pages", "-1")),
    ("往后翻五页", ("pages", "五")),
    ("往前翻3页", ("pages", "-3")),
    ("下一章", ("chapter", "下一章")),
    ("上一章", ("chapter", "上一章")),
    ("跳到第三章", ("chapter", "第三章")),
    ("跳到第12章", ("chapter", "第12章")),
    ("跳到百分之五十", ("percent", "五十")),
    ("跳到50%", ("percent", "50")),
    ("回到开头", ("percent", "0")),
    ("跳到一半", ("percent", "50")),
    ("我看到哪了", ("status", "")),
    ("书架上有什么书", ("shelf", "")),
    ("字大一点", ("font", "1")),
    ("字号缩小", ("font", "-1")),
    ("打开《雾港来信》", ("open", "雾港来信")),
    ("换一本雾港来信", ("open", "雾港来信")),
    ("看小说", ("open", "")),
    ("继续看", ("open", "")),
    ("今天天气怎么样", ("help", "")),
    ("", ("help", "")),
    ("开始听书", ("listen", "")),
    ("继续听", ("listen", "")),
    ("读给我听", ("listen", "")),
    ("暂停", ("quiet", "")),
    ("别读了", ("quiet", "")),
    ("我不想听书", ("help", "")),
    ("从头开始", ("percent", "0")),
    ("跳到下一章", ("chapter", "下一章")),
    ("记录一下当前开头的这个字符", ("help", "")),  # 2026-11-01 真机：对别人说的话把进度带回了书头
    ("这个开头写得好", ("help", "")),
    ("一半一半吧", ("help", "")),
    ("第三章写得不错", ("help", "")),
    ("百分之五十的人", ("help", "")),
    ("等下一章再说", ("help", "")),
])
def test_parse_command(text: str, expected: tuple[str, str]) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize(("text", "value"), [("12", 12), ("三", 3), ("十", 10), ("二十", 20), ("十五", 15),
                                             ("三十五", 35), ("两", 2), ("一百二十", None)])
def test_number(text: str, value: int | None) -> None:
    assert _number(text) == value


# ---------------------------------------------------------------------- 会话


def test_opening_shows_the_page_and_speaks(session: AgentSession) -> None:
    out = session.start(init()["payload"])
    (show,) = actions(out, "SHOW_UI")
    assert show["display"] == {"mode": "OPEN"} and show["view"]["props"]["templateId"] == "tpl_1"
    data = show["view"]["props"]["data"]
    assert data["t"] == session.book.title and data["fs"] in TEXT_SIZES
    assert {f"s{i}" for i in range(SLOTS)} <= set(data) and {f"h{i}" for i in range(SLOTS)} <= set(data)
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


def test_turn_sends_only_the_slots_that_changed(opened: AgentSession) -> None:
    before = opened._current().offset
    (update,) = shown(turn(opened, 1))
    assert opened._current().offset > before
    assert set(update) <= {"bar"} | {f"{k}{i}" for k in "shrf" for i in range(SLOTS)}  # 没有重发书名和字号
    assert 0 < len(update) <= 4 * SLOTS + 1 and opened.sent["s1"] == opened._current().text


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


@pytest.mark.parametrize("slot", ["1", 8, -1, None])
def test_bad_slot_is_ignored(opened: AgentSession, slot: Any) -> None:
    assert turn(opened, slot) == []


def test_book_end_stops_and_says_so(opened: AgentSession) -> None:
    say(opened, "跳到百分之一百")
    last = opened._current()
    assert "全书完" in opened.sent.values()
    turn(opened, (opened.slot + 1) % SLOTS)
    assert opened._current() == last  # 走到头了，槽号往前但页不动
    assert opened.sent[f"s{opened.slot}"] == last.text


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


def test_voice_pages(opened: AgentSession) -> None:
    start = opened._current().offset
    say(opened, "往后翻三页")
    assert opened._walk(-3) is not None and opened._current().offset > start


def test_menu_actions_change_chapter_and_font(opened: AgentSession) -> None:
    def act(name: str) -> list[dict[str, Any]]:
        action = {"pageUuid": opened.page_uuid, "action": {"name": name, "params": {"slot": opened.slot}}}
        return opened.handle({"type": "UI_ACTION", "payload": action})

    chapter = opened.chapter
    act("next_chapter")
    assert opened.chapter > chapter


def display(s: AgentSession, **params: int) -> list[dict[str, Any]]:
    action = {"pageUuid": s.page_uuid, "action": {"name": "display", "params": params}}
    return s.handle({"type": "UI_ACTION", "payload": action})


def test_display_saves_the_settings_and_repaginates(opened: AgentSession) -> None:
    update = {k: v for part in shown(display(opened, f=18, w=700, b=40)) for k, v in part.items()}
    assert (opened.size, opened.bold) == (18, True) and (update["fs"], update["fw"]) == (18, 700)
    assert update["s"]["b"] == 40 and update["s"]["o"] == 150 and opened.library.display.brightness == 40
    assert all(len(update[f"s{i}"].split("\n")) <= lines_per_page(18) for i in range(SLOTS) if f"s{i}" in update)


def test_voice_turn_sends_the_visible_page_first(opened: AgentSession) -> None:
    turn(opened, 2)
    first, rest = shown(say(opened, "下一页"))
    assert first[f"s{opened.slot}"] == opened._current().text and "v" in first
    assert {k[1:] for k in first if agent.SLOT_FIELD.fullmatch(k)} == {str(opened.slot)}
    assert len(rest) > 8 and all(agent.SLOT_FIELD.fullmatch(k) and k[1:] != str(opened.slot) for k in rest)


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


def test_exit_speaks_and_closes(opened: AgentSession) -> None:
    assert spoken(say(opened, "退出")) and opened.closing


def test_confirmed_double_click_closes_the_session_silently(opened: AgentSession) -> None:
    action = {"pageUuid": opened.page_uuid, "action": {"name": "exit", "params": {"slot": opened.slot}}}
    assert opened.handle({"type": "UI_ACTION", "payload": action}) == [] and opened.closing


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


def test_commands_while_listening(opened: AgentSession, clock: list[float]) -> None:
    say(opened, "开始听书")
    assert say(opened, "今天天气怎么样") == [] and say(opened, "能说什么") == [] and opened.listening
    assert say(opened, "继续听书") == []
    out = say(opened, "下一章")
    assert actions(out, "STOP_OUTPUT") and "已跳到" in spoken(out) and opened.listening  # 接着读新的一章
    out = say(opened, "我看到哪了")
    assert actions(out, "STOP_OUTPUT") and "读到" in spoken(out) and not opened.listening


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


def test_keepalive_resends_a_field_until_idle_for_too_long(opened: AgentSession) -> None:
    (update,) = shown(opened.keepalive())
    assert update == {"t": opened.book.title}
    opened.last_action -= agent.IDLE_LIMIT_SECONDS + 1
    assert opened.keepalive() == []


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


def test_bad_first_message_closes(client: TestClient, settings: Settings) -> None:
    with client.websocket_connect(AGENT_PATH, headers=auth(settings)) as ws:
        ws.send_json({"type": "UTTERANCE"})
        closed = ws.receive()
        assert closed["type"] == "websocket.close" and closed["code"] == 1002


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
    assert all(el.get("font-weight") == "{{fw}}" for el in bodies)
    slots = [el.get(f"{A}if") for el in page.iter("view") if el.get(f"{A}if", "").startswith("{{d:i ==")]
    assert slots == [f"{{{{d:i == {i}}}}}" for i in range(SLOTS)]


def test_only_the_shared_bar_has_a_layout_binding(page: ET.Element) -> None:
    # width 绑定每次写数据都重新设置，菜单移动也不例外
    assert [el.get("width") for el in page.iter("view") if "{{" in el.get("width", "")] == ["{{bar}}"]


def test_ring_wraps_in_the_page(page: ET.Element) -> None:
    actions_ = {}
    for e in page.iter("event"):
        actions_.setdefault(e.get("match"), []).append(e.get("action"))
    assert actions_["event://next?"][:2] == ["data://update?i=0&c={{d:p}}", "data://update?i={{d:i + 1}}&c={{d:p}}"]
    assert actions_["event://prev?"][:2] == [f"data://update?i={SLOTS - 1}&c={{{{d:p}}}}", "data://update?i={{d:i - 1}}&c={{d:p}}"]
    report = "svc://ui-action?mode=mcp&name=turn&params={{json:turn}}"
    assert actions_["event://next?"][2] == report and actions_["event://prev?"][2] == report


def test_page_action_names_are_known_to_the_server(page: ET.Element) -> None:
    rows = [*page.iter("event"), *page.iter("key-event")]
    uris = [v for e in rows for v in e.attrib.values() if v.startswith("svc://")]
    names = {u.split("name=")[1].split("&")[0] for u in uris}
    assert names == {"turn", "next_chapter", "prev_chapter", "display", "exit", "listen"}


def test_double_click_twice_leaves_and_ends_the_session(page: ET.Element) -> None:
    meta = page.find("page-meta")
    assert meta.get("confirm-back-text") and meta.get("forbidden") is None  # 写了 forbidden 双击就完全没反应
    assert page.find("prompt-config").get("text") == ""  # 提示显示在这里，平时不占地方
    rows = [e for e in page.iter("key-event") if e.get("on-key-back")]  # 其他分支缺这个键，都落到最后一支
    assert len(rows) == 1 and rows[0].get(f"{A}else") == ""
    assert rows[0].get("on-key-back").startswith("svc://ui-action?mode=mcp&name=exit")


def test_menus_wrap_around(page: ET.Element) -> None:
    rows = {e.get(f"{A}if", e.get(f"{A}elif")): e for e in page.iter("key-event")}
    assert rows["{{d:m == 1}}"].get("on-key-prev") == "data://update?m=6"
    assert rows[None].get("on-key-next") == "data://update?m=6"  # a:else 是第 5 项
    assert rows["{{d:m == 6}}"].get("on-key-next") == "data://update?m=1"
    assert rows["{{d:m == 11}}"].get("on-key-prev") == "data://update?m=14"
    assert rows["{{d:m == 14}}"].get("on-key-next") == "data://update?m=11"
    assert not any("stay" in v for e in page.iter("key-event") for v in e.attrib.values())


def test_display_is_reported_only_on_save(page: ET.Element) -> None:
    rows = [e.get("match") for e in page.iter("event") if "name=display" in e.get("action", "")]
    assert rows == ["event://save?"]


def test_font_panel_steps_through_the_native_sizes(page: ET.Element) -> None:
    rows = [e for e in page.iter("event") if e.get("match") == "event://up21?"]
    assert [e.get("action") for e in rows[:2]] == ["dataSource://update?fs=28", "dataSource://update?fs=32"]
    assert rows[2].get(f"{A}elif") == f"{{{{fs != {TEXT_SIZES[-1]}}}}}"


def test_page_data_matches_the_fields_the_server_sends(session: AgentSession) -> None:
    session.start(init()["payload"])
    assert set(json.loads(DATA.read_text(encoding="utf-8"))) == set(session.sent)


def test_settings_the_text_page_cannot_show_are_kept(opened: AgentSession) -> None:
    library = opened.library
    assert opened.book is not None
    library.save(library.view(opened.book, font_size=26))
    library.set_display(Display(5, 100, 170))
    reopened = AgentSession("s2", opened.tools, library)
    reopened.start(init(session="s2")["payload"])
    assert reopened.size in (24, 28) and reopened.bold
    action = {"pageUuid": reopened.page_uuid, "action": {"name": "turn", "params": {"slot": 1}}}
    reopened.handle({"type": "UI_ACTION", "payload": action})
    assert library.position(reopened.book)[1] == 26
    display(reopened, b=60, w=700)
    assert library.display == Display(5, 60, 170)  # 没切换粗细就不动六档


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
    assert (rows[0].get(f"{A}if"), rows[0].get("action")) == ("{{d:vs != v}}", "data://update?vs={{v}}&c={{d:p}}")
