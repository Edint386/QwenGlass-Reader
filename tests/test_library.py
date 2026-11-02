"""书库、按计数差值翻页、跳转、进度文件。"""

from __future__ import annotations

import errno
import gc
import json
import os
import threading
import time
import weakref
from pathlib import Path
from typing import Any

import pytest

from glassreader import library as library_mod
from glassreader.book import book_id_for
from glassreader.library import (
    BRIGHTNESS_LEVELS,
    DEFAULT_OPTICS,
    DEFAULT_PACE,
    PACES,
    Display,
    Library,
    View,
    new_session_id,
    normalize_title,
    parse_counters,
)
from glassreader.pager import DEFAULT_SPACING, DEFAULT_WEIGHT, FONT_SIZES, LINE_SPACINGS, WEIGHTS

from .conftest import make_novel

ZERO = (0,) * 9


def novel(lib: Library):
    return lib.book(lib.find("测试长篇")[0].id)


def where(v: View) -> tuple[int, int]:
    return v.chapter, v.page


@pytest.mark.parametrize(
    ("raw", "counters"),
    [("1-2-3-4-5-20-3-60-155", (1, 2, 3, 4, 5, 20, 3, 60, 155)), ("1-x-3-4-5-0-0-0-0", (1, 0, 3, 4, 5, 0, 0, 0, 0)),
     # 还开着的旧页面：a-b-j-k-f-g-z…，后面的计数不能当成设置值
     ("3-1-2-0-1-0", (3, 1, 2, 0, 0, 0, 0, 0, 0)), ("3-1-2-0-1-0-6", (3, 1, 2, 0, 6, 0, 0, 0, 0)),
     ("3-1-2-0-1-0-6-" + "9-9-9-9-9-9", (3, 1, 2, 0, 6, 0, 0, 0, 0)), ("5", (5, 0, 0, 0, 0, 0, 0, 0, 0)),
     ("9999999999", ZERO)],
)
def test_parse_counters(raw: str, counters: tuple[int, ...]) -> None:
    assert parse_counters(raw) == counters


def test_session_ids_are_unique_and_path_safe() -> None:
    ids = {new_session_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.isalnum() and len(i) <= 40 for i in ids)


def test_scan_nested_and_hidden(books_dir: Path, state_file: Path) -> None:
    (books_dir / "子目录").mkdir()
    (books_dir / "子目录" / "第二本.txt").write_text("正文\n", encoding="utf-8")
    (books_dir / ".隐藏").mkdir()
    (books_dir / ".隐藏" / "不要.txt").write_text("x\n", encoding="utf-8")
    (books_dir / "不是书.md").write_text("x\n", encoding="utf-8")
    lib = Library(books_dir, state_file)
    names = sorted(e.name for e in lib.entries())
    assert names == ["子目录/第二本.txt", "测试长篇.txt", "雾港来信.txt"]
    assert {e.id for e in lib.entries()} == {book_id_for(n) for n in names}


def test_scan_ignores_extension_case(books_dir: Path, state_file: Path) -> None:
    (books_dir / "大写.TXT").write_text("正文\n", encoding="utf-8")
    (books_dir / "混写.Txt").write_text("正文\n", encoding="utf-8")
    lib = Library(books_dir, state_file)
    assert {"大写", "混写"} <= {e.title for e in lib.entries()}
    assert lib.book(lib.find("大写")[0].id).title == "大写"


def test_missing_books_dir(tmp_path: Path) -> None:
    lib = Library(tmp_path / "没有", tmp_path / "p.json")
    assert lib.entries() == [] and lib.recent() is None and lib.find("x") == []


def test_find_ranking(books_dir: Path, state_file: Path) -> None:
    for name in ("雾港", "雾港来信续集", "新雾港来信"):
        (books_dir / f"{name}.txt").write_text("正文\n", encoding="utf-8")
    lib = Library(books_dir, state_file)
    assert [e.title for e in lib.find("《雾港来信》")] == ["雾港来信", "雾港来信续集", "新雾港来信"]
    assert [e.title for e in lib.find("雾港")][0] == "雾港"
    assert lib.find("不存在") == []
    assert normalize_title(" 《Abc 来信》") == "abc来信"


@pytest.mark.parametrize("query", ["你好，旧时光", "你好旧时光", "你好 旧时光！", "你好"])
def test_find_ignores_punctuation_on_both_sides(library: Library, books_dir: Path, query: str) -> None:
    (books_dir / "你好，旧时光.txt").write_text(make_novel(), encoding="utf-8")
    library.scan()
    assert [e.title for e in library.find(query)] == ["你好，旧时光"]


def test_find_prefers_the_title_as_written(books_dir: Path, state_file: Path) -> None:
    for name in ("三体", "三体！"):
        (books_dir / f"{name}.txt").write_text("正文\n", encoding="utf-8")
    lib = Library(books_dir, state_file)
    plain, marked = lib.find("三体")[0], lib.find("三体！")[0]
    assert (plain.title, marked.title) == ("三体", "三体！")
    for later in (plain, marked):  # 哪本最近读过都不影响
        lib.save(lib.view(lib.book(later.id)))
        assert [e.title for e in lib.find("《三体！》")] == ["三体！", "三体"]
        assert [e.title for e in lib.find(" 三体 ")] == ["三体", "三体！"]
    assert [e.title for e in lib.find("三体。")] == ["三体！", "三体"]  # 都不一字不差：标点去掉后并列，按最近阅读


@pytest.mark.parametrize("query", ["。", "？", "...", "!", "，", "《。》", "《》"])
def test_find_punctuation_alone_is_not_an_empty_query(library: Library, query: str) -> None:
    assert library.find(query) == [] and library.find(query, rescan=False) == []


def test_find_a_title_of_only_punctuation(library: Library, books_dir: Path) -> None:
    (books_dir / "？？？.txt").write_text("正文\n", encoding="utf-8")
    library.scan()
    assert [e.title for e in library.find("？？？")] == ["？？？"]
    assert library.find("。") == []


def test_find_rescans_for_new_books(library: Library, books_dir: Path) -> None:
    (books_dir / "后来放进来的.txt").write_text("正文\n", encoding="utf-8")
    assert [e.title for e in library.find("后来")] == ["后来放进来的"]


def test_recent_book_follows_last_save(library: Library) -> None:
    assert library.recent() is None
    a = novel(library)
    b = library.book(library.find("雾港来信")[0].id)
    library.save(library.view(a))
    time.sleep(0.01)
    library.save(library.view(b))
    assert library.recent().title == "雾港来信"
    # 同档的书按最近阅读排在前面
    assert [e.title for e in library.find("")][0] == "雾港来信"


def test_turn_baseline_repeat_and_delta(library: Library) -> None:
    book = novel(library)
    v, note = library.turn(book, "s1", ZERO)
    assert note == "baseline" and where(v) == (0, 0)
    v, note = library.turn(book, "s1", ZERO)
    assert note == "repeat"
    v1, _ = library.turn(book, "s1", (1, 0, 0, 0, 0, 0, 0, 0, 0))
    v2, note = library.turn(book, "s1", (1, 0, 0, 0, 0, 0, 0, 0, 0))  # 内核重复请求同一地址
    assert note == "repeat" and where(v2) == where(v1)
    assert library.progress[book.id]["offset"] == v1.offset


def test_turn_lost_request_is_counted(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    v, _ = library.turn(book, "s", (3, 0, 0, 0, 0, 0, 0, 0, 0))  # 1、2 两个地址没发出来
    expected = library.move_pages(library.view(book, 0), 3)
    assert where(v) == where(expected)


def test_turn_stale_request_does_not_go_back(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    v4, _ = library.turn(book, "s", (4, 0, 0, 0, 0, 0, 0, 0, 0))
    v, note = library.turn(book, "s", (2, 0, 0, 0, 0, 0, 0, 0, 0))  # 晚到的旧地址
    assert note == "repeat" and where(v) == where(v4)


def test_new_session_baseline_keeps_position(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s1", ZERO)
    v, _ = library.turn(book, "s1", (5, 1, 0, 0, 0, 0, 0, 0, 0))
    # 页面经 on-new-intent 换了新会话，计数器没归零：只记基线，不能当成 5 次翻页
    v2, note = library.turn(book, "s2", (5, 1, 0, 0, 0, 0, 0, 0, 0))
    assert note == "baseline" and where(v2) == where(v)
    v3, _ = library.turn(book, "s2", (6, 1, 0, 0, 0, 0, 0, 0, 0))
    assert where(v3) == where(library.move_pages(v, 1))


def test_sessions_are_per_book(library: Library) -> None:
    a = novel(library)
    b = library.book(library.find("雾港来信")[0].id)
    library.turn(a, "s", ZERO)
    v, note = library.turn(b, "s", (2, 0, 0, 0, 0, 0, 0, 0, 0))
    assert note == "baseline" and v.book is b


def test_session_memory_is_bounded(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library_mod, "MAX_SESSIONS", 3)
    book = novel(library)
    for i in range(10):
        library.turn(book, f"s{i}", ZERO)
    assert len(library._sessions) == 3


def test_move_pages_across_chapters_and_ends(library: Library) -> None:
    book = novel(library)
    start = library.view(book, 0)
    pages0 = len(start.pages.pages)  # 前言
    v = library.move_pages(start, pages0)
    assert where(v) == (1, 0)  # 翻过前言最后一页进入第一章第一页
    back = library.move_pages(v, -1)
    assert where(back) == (0, pages0 - 1)  # 从章首往前翻到上一章最后一页
    assert where(library.move_pages(start, -5)) == (0, 0)  # 书头停住
    end = library.move_pages(start, 10_000)
    assert end.chapter == len(book.chapters) - 1 and end.page == len(end.pages.pages) - 1
    assert where(library.move_pages(end, 3)) == where(end)  # 书尾停住


def test_moves_skip_empty_chapters(books_dir: Path, state_file: Path) -> None:
    (books_dir / "有空章.txt").write_text(make_novel(chapters=4, empty=(2,)), encoding="utf-8")
    lib = Library(books_dir, state_file)
    book = lib.book(lib.find("有空章")[0].id)
    ch1 = lib.view(book, book.chapters[1].start)
    last_of_ch1 = lib.move_pages(ch1, len(ch1.pages.pages) - 1)
    assert lib.move_pages(last_of_ch1, 1).chapter == 3  # 跳过只有标题的第二章
    assert lib.move_chapters(ch1, 1).chapter == 3
    assert lib.move_chapters(lib.view(book, book.chapters[3].start), -1).chapter == 1


def test_move_chapters_clamps(library: Library) -> None:
    book = novel(library)
    v = library.view(book, 0)
    assert where(library.move_chapters(v, -3)) == (0, 0)
    last = library.move_chapters(v, 99)
    assert last.chapter == len(book.chapters) - 1 and last.page == 0
    mid = library.move_pages(library.view(book, book.chapters[2].start), 2)
    assert where(library.move_chapters(mid, 0)) == (2, 0)  # 0 = 回到本章开头


def test_next_chapter_in_the_last_chapter_stays_put(library: Library) -> None:
    book = novel(library)
    v = library.move_pages(library.view(book, book.chapters[-1].start), 1)
    assert v.page == 1
    assert where(library.move_chapters(v, 1)) == where(library.move_chapters(v, 99)) == where(v)
    assert where(library.move_chapters(v, 0)) == (v.chapter, 0)  # 0 和往前还是照常
    assert library.move_chapters(v, -1).chapter == v.chapter - 1
    library.save(v)
    assert where(library.jump(book, chapter="下一章")) == where(v)
    assert library.position(book)[0] == v.offset


def test_next_chapter_before_a_trailing_empty_chapter_stays_put(books_dir: Path, state_file: Path) -> None:
    (books_dir / "空结尾.txt").write_text(make_novel(chapters=3, empty=(3,)), encoding="utf-8")
    lib = Library(books_dir, state_file)
    book = lib.book(lib.find("空结尾")[0].id)
    v = lib.move_pages(lib.view(book, book.chapters[2].start), 1)
    assert where(lib.move_chapters(v, 1)) == where(v)


@pytest.mark.parametrize("empty", [(3,), (3, 4)])
def test_previous_chapter_from_a_trailing_empty_chapter(books_dir: Path, state_file: Path, empty: tuple[int, ...]) -> None:
    (books_dir / "空结尾.txt").write_text(make_novel(chapters=max(empty), empty=empty), encoding="utf-8")
    lib = Library(books_dir, state_file)
    book = lib.book(lib.find("空结尾")[0].id)
    end = lib.view(book, book.length)
    assert end.chapter == len(book.chapters) - 1 and end.chapter not in book.readable()
    assert lib.move_chapters(end, -1).chapter == 2  # 第二章，最后一个有正文的章
    assert lib.move_chapters(end, -2).chapter == 1
    assert lib.move_chapters(end, -9).chapter == book.readable()[0]
    assert where(lib.move_chapters(end, 1)) == where(end)
    lib.save(end)
    lib.turn(book, "s", ZERO)
    v, _ = lib.turn(book, "s", (0, 0, 0, 1, 0, 0, 0, 0, 0))  # 页面上的 k 走同一条路
    assert v.chapter == 2


def test_previous_chapter_in_a_book_of_only_titles(books_dir: Path, state_file: Path) -> None:
    (books_dir / "全是标题.txt").write_text("第一章 甲\n第二章 乙\n第三章 丙\n", encoding="utf-8")
    lib = Library(books_dir, state_file)
    book = lib.book(lib.find("全是标题")[0].id)
    v = lib.view(book)
    assert where(lib.move_chapters(v, -1)) == where(v) and where(lib.move_chapters(v, 1)) == where(v)


def test_resize_keeps_text_position(library: Library) -> None:
    book = novel(library)
    v = library.move_pages(library.view(book, book.chapters[3].start), 3)
    for steps in (1, 2, -1, -2, 9, -9):
        r = library.resize(v, steps)
        assert r.font_size in FONT_SIZES
        page = r.pages.pages[r.page]
        last = page.lines[-1]
        assert page.offset <= v.offset < last.offset + len(last.text)  # 新的一页仍包含原来那页的第一个字
    assert library.resize(v, 9).font_size == FONT_SIZES[-1]
    assert library.resize(v, -9).font_size == FONT_SIZES[0]


def test_auto_turn_moves_one_page(library: Library) -> None:
    book = novel(library)
    start, _ = library.turn(book, "s", ZERO)
    v, note = library.turn(book, "s", (0, 0, 0, 0, 1, 0, 0, 0, 0))
    assert note.startswith("+1") and v.offset > start.offset


def test_auto_turn_backlog_counts_as_one_page(library: Library) -> None:
    book = novel(library)
    start, _ = library.turn(book, "s", ZERO)
    v, _ = library.turn(book, "s", (0, 0, 0, 0, 5, 0, 0, 0, 0))  # 断线时走过的几圈
    assert where(v) == where(library.move_pages(start, 1))


def test_next_right_after_auto_turn_is_the_same_turn(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    book = novel(library)
    clock = [100.0]
    monkeypatch.setattr(library_mod.time, "monotonic", lambda: clock[0])
    library.turn(book, "s", ZERO)
    auto, _ = library.turn(book, "s", (0, 0, 0, 0, 1, 0, 0, 0, 0))
    clock[0] += 0.5
    merged, _ = library.turn(book, "s", (1, 0, 0, 0, 1, 0, 0, 0, 0))  # 新图还没显示就滑了一下
    assert where(merged) == where(auto)
    clock[0] += library_mod.TURN_MERGE_SECONDS
    moved, _ = library.turn(book, "s", (2, 0, 0, 0, 1, 0, 0, 0, 0))
    assert moved.offset > auto.offset


def test_auto_turn_right_after_next_is_the_same_turn(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    book = novel(library)
    clock = [100.0]
    monkeypatch.setattr(library_mod.time, "monotonic", lambda: clock[0])
    library.turn(book, "s", ZERO)
    manual, _ = library.turn(book, "s", (1, 0, 0, 0, 0, 0, 0, 0, 0))
    clock[0] += 0.5
    merged, note = library.turn(book, "s", (1, 0, 0, 0, 1, 0, 0, 0, 0))
    assert note == "merged" and where(merged) == where(manual)


def test_quick_manual_turns_are_not_merged(library: Library) -> None:
    book = novel(library)
    start, _ = library.turn(book, "s", ZERO)
    library.turn(book, "s", (1, 0, 0, 0, 0, 0, 0, 0, 0))
    v, _ = library.turn(book, "s", (2, 0, 0, 0, 0, 0, 0, 0, 0))
    assert where(v) == where(library.move_pages(library.move_pages(start, 1), 1))


def test_turn_applies_font_then_chapter_then_pages(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    v, _ = library.turn(book, "s", (1, 0, 2, 0, 0, 28, 0, 0, 0))
    assert v.font_size == 28
    assert where(v) == (2, 1)  # 跳两章到第二章，再翻一页


def test_swap_swipe(books_dir: Path, state_file: Path) -> None:
    lib = Library(books_dir, state_file, swap_swipe=True)
    book = lib.book(lib.find("测试长篇")[0].id)
    lib.turn(book, "s", ZERO)
    v, _ = lib.turn(book, "s", (0, 1, 0, 0, 0, 0, 0, 0, 0))  # “上一页”计数加一，对调后是下一页
    assert where(v) == where(lib.move_pages(lib.view(book, 0), 1)) != (0, 0)


def test_jump(library: Library) -> None:
    book = novel(library)
    assert library.jump(book, chapter="第三章").chapter == 3
    assert library.jump(book, chapter=4).chapter == 4
    assert library.jump(book, chapter="下一章").chapter == 5
    assert library.jump(book, chapter="上一章").chapter == 4
    assert library.jump(book, chapter="标题2").chapter == 2
    assert library.jump(book, chapter="第九十九章") is None
    assert library.progress[book.id]["chapter_index"] == 2  # 找不到不改进度
    assert where(library.jump(book, percent=0)) == (0, 0)
    v = library.jump(book, percent=50)
    assert 45 <= book.percent(v.offset) <= 55
    assert where(library.jump(book, percent=150)) == where(library.view(book, book.length))
    before = library.view(book)
    assert where(library.jump(book, pages=2)) == where(library.move_pages(before, 2))
    assert where(library.jump(book, chapter="第一章", pages=1)) == (1, 1)


def test_progress_survives_restart(books_dir: Path, state_file: Path) -> None:
    lib = Library(books_dir, state_file)
    book = novel(lib)
    lib.turn(book, "s", ZERO)
    v, _ = lib.turn(book, "s", (7, 0, 0, 0, 0, 28, 0, 0, 0))
    saved = json.loads(state_file.read_text(encoding="utf-8"))[book.id]
    assert saved["offset"] == v.offset and saved["font_size"] == 28 and saved["file"] == "测试长篇.txt"
    assert saved["chapter"] == book.chapters[v.chapter].title and saved["page"] == v.page + 1
    lib2 = Library(books_dir, state_file)
    v2 = lib2.view(lib2.book(book.id))
    assert (v2.offset, v2.font_size, where(v2)) == (v.offset, 28, where(v))
    assert lib2.recent().id == book.id
    assert not list(state_file.parent.glob("*.tmp"))


def test_corrupt_progress_file_is_left_alone(books_dir: Path, state_file: Path) -> None:
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{不是 JSON", encoding="utf-8")
    lib = Library(books_dir, state_file)
    assert lib.progress == {}
    assert state_file.read_text(encoding="utf-8") == "{不是 JSON"


def test_save_returns_the_time_it_wrote(library: Library) -> None:
    book = novel(library)
    assert library.save(library.view(book)) == library.last_read(book.id)


def test_saving_keeps_a_copy_of_a_corrupt_file(books_dir: Path, state_file: Path) -> None:
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{不是 JSON", encoding="utf-8")
    lib = Library(books_dir, state_file)
    book = novel(lib)
    lib.save(lib.view(book, 0))
    kept = list(state_file.parent.glob("progress.json.corrupt-*"))
    assert [p.read_text(encoding="utf-8") for p in kept] == ["{不是 JSON"]
    assert book.id in json.loads(state_file.read_text(encoding="utf-8"))


@pytest.mark.parametrize("broken", ["{不是 JSON", "", "[1, 2]"])
def test_file_going_bad_keeps_other_books_and_display(library: Library, state_file: Path, broken: str) -> None:
    a, b = (library.book(e.id) for e in library.entries()[:2])
    library.save(library.move_pages(library.view(a), 1))
    library.set_display(weight=3, brightness=40, spacing=155)
    offset = library.position(a)[0]
    state_file.write_text(broken, encoding="utf-8")
    assert library.position(a)[0] == offset and library.display.brightness == 40  # 读到坏文件也不清空
    library.save(library.view(b))
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved[a.id]["offset"] == offset and saved["_display"]["brightness"] == 40 and b.id in saved


def test_bad_file_does_not_bring_back_a_stale_instances_memory(books_dir: Path, state_file: Path) -> None:
    image, text = Library(books_dir, state_file), Library(books_dir, state_file)
    a, b = (image.book(e.id) for e in image.entries()[:2])
    text.save(text.view(a, 100))
    image.save(image.view(b, 100))
    image.set_display(brightness=60, pace=40)
    assert text.display.pace == image.display.pace == 40
    text.save(text.view(a, 300))
    text.set_display(brightness=30, pace=50)  # 页图版闲着，没读过这些
    offset = text.position(a)[0]
    state_file.write_text("{不是 JSON", encoding="utf-8")
    image.save(image.view(b, 200))
    assert text.position(a)[0] == offset and (text.display.brightness, text.display.pace) == (30, 50)


def test_restarted_instance_finds_the_progress_in_the_backup(books_dir: Path, state_file: Path) -> None:
    image, text = Library(books_dir, state_file), Library(books_dir, state_file)
    a, b = (image.book(e.id) for e in image.entries()[:2])
    image.save(image.view(a, 1000, 28))
    image.set_display(weight=1, brightness=60, pace=40)
    where_a = image.position(a)
    state_file.write_bytes(state_file.read_bytes()[:40])
    restarted = Library(books_dir, state_file)
    assert restarted.position(a) == where_a and restarted.display.pace == 40  # 翻页之前就要读对，不然第一次保存盖掉真实位置
    restarted.save(restarted.view(b))
    assert text.position(a) == where_a and text.display == Display(1, 60, DEFAULT_SPACING, 40)


def test_backup_is_not_a_link_to_the_progress_file(library: Library, state_file: Path) -> None:
    library.save(library.view(novel(library), 100))
    backup = state_file.with_name("progress.json.bak")
    assert backup.read_text(encoding="utf-8") == state_file.read_text(encoding="utf-8")
    assert backup.stat().st_ino != state_file.stat().st_ino


def test_newer_memory_beats_the_backup(library: Library, state_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = novel(library)
    library.save(library.view(book, 100))
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device")))
    v = library.view(book, 5000)
    library.save(v)  # 写不进去，只在内存里
    monkeypatch.undo()
    state_file.write_text("{不是 JSON", encoding="utf-8")
    assert library.position(book)[0] == v.offset
    library.save(library.view(library.book(library.entries()[1].id)))
    assert Library(library.books_dir, state_file).position(book)[0] == v.offset


@pytest.mark.parametrize("backup", [None, "{也坏了", "[1]"])
def test_bad_file_without_a_good_backup_uses_memory(library: Library, state_file: Path, backup: str | None) -> None:
    book = novel(library)
    library.save(library.view(book, 100))
    offset = library.position(book)[0]
    bak = state_file.with_name("progress.json.bak")
    bak.unlink()
    if backup:
        bak.write_text(backup, encoding="utf-8")
    state_file.write_text("{不是 JSON", encoding="utf-8")
    assert library.position(book)[0] == offset
    library.save(library.view(book, 200))
    assert json.loads(state_file.read_text(encoding="utf-8"))[book.id]["offset"] > 0


def test_a_bad_timestamp_in_the_backup_does_not_stop_recovery(library: Library, state_file: Path) -> None:
    book = novel(library)
    library.save(library.view(book, 100))
    offset = library.position(book)[0]
    bak = state_file.with_name("progress.json.bak")
    data = json.loads(bak.read_text(encoding="utf-8"))
    data[book.id]["updated_ts"] = "坏"
    bak.write_text(json.dumps(data), encoding="utf-8")
    state_file.write_text("{不是 JSON", encoding="utf-8")
    assert library.position(book)[0] == offset


def test_failing_to_write_over_a_bad_file_keeps_the_progress(library: Library, state_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = (library.book(e.id) for e in library.entries()[:2])
    library.save(library.move_pages(library.view(a), 1))
    library.save(library.view(b, 200))
    offsets = library.position(a)[0], library.position(b)[0]
    state_file.write_text("{不是 JSON", encoding="utf-8")
    write_text = Path.write_text

    def full(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.suffix == ".tmp":
            raise OSError(errno.ENOSPC, "No space left on device")
        return write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", full)
    library.save(library.view(a, offsets[0]))  # 坏文件已经抄了一份，新文件没写成
    assert (library.position(a)[0], library.position(b)[0]) == offsets
    monkeypatch.undo()
    library.save(library.view(a, offsets[0]))  # 磁盘腾出来了，内存里的都写回去
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert (saved[a.id]["offset"], saved[b.id]["offset"]) == offsets


def test_failing_writes_keep_only_one_copy_of_the_bad_file(library: Library, state_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = novel(library)
    library.save(library.view(book))
    state_file.write_text("{不是 JSON", encoding="utf-8")
    write_text = Path.write_text

    def full(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.suffix == ".tmp":
            raise OSError(errno.ENOSPC, "No space left on device")
        return write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", full)
    for _ in range(3):
        library.save(library.view(book))
    assert len(list(state_file.parent.glob("progress.json.corrupt-*"))) == 1


def test_a_reader_while_a_bad_file_is_replaced_keeps_the_progress(library: Library, state_file: Path) -> None:
    book = novel(library)
    library.save(library.move_pages(library.view(book), 5))
    library.set_display(brightness=40, pace=44)
    where = library.position(book)
    other = Library(library.books_dir, state_file)
    assert other.position(book) == where
    state_file.write_text("{不是 JSON", encoding="utf-8")
    with library._updating():  # 坏文件抄好了，新文件还没写
        assert other.position(book) == where and other.display.brightness == 40
        following = other.move_pages(other.view(book), 1)  # 这一下不能从书头算起
    other.save(following)
    assert other.position(book)[0] > where[0]


def test_dying_while_a_bad_file_is_replaced_keeps_the_progress(library: Library, state_file: Path) -> None:
    a, b = (library.book(e.id) for e in library.entries()[:2])
    library.save(library.move_pages(library.view(a), 5))
    library.set_display(brightness=40, pace=44)
    where = library.position(a)
    state_file.write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(KeyboardInterrupt), library._updating():  # 进程死在写回之前
        raise KeyboardInterrupt
    restarted = Library(library.books_dir, state_file)
    restarted.save(restarted.view(b))
    assert restarted.position(a) == where
    assert (restarted.display.brightness, restarted.display.pace) == (40, 44)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved[a.id]["offset"] == where[0] and saved["_display"]["brightness"] == 40


def test_bad_files_in_the_same_second_are_both_kept(library: Library, state_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = novel(library)
    monkeypatch.setattr(library_mod.time, "strftime", lambda *_: "20261101-000000")
    for text in ('{"第一份', '{"第二份'):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(text, encoding="utf-8")
        library.save(library.view(book))
    kept = sorted(p.read_text(encoding="utf-8") for p in state_file.parent.glob("progress.json.corrupt-*"))
    assert kept == ['{"第一份', '{"第二份']


def test_deleting_the_file_still_starts_over(library: Library, state_file: Path) -> None:
    a, b = (library.book(e.id) for e in library.entries()[:2])
    library.save(library.move_pages(library.view(a), 1))
    state_file.unlink()
    library.save(library.view(b))
    assert set(json.loads(state_file.read_text(encoding="utf-8"))) == {b.id}


def test_read_error_keeps_the_file_and_retries(library: Library, state_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = novel(library)
    v = library.move_pages(library.view(book), 1)
    library.save(v)
    read_text = Path.read_text

    def flaky(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == state_file:
            raise OSError(errno.EIO, "读错误")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    library._stamp = None  # 另一个实例刚写过
    assert library.position(book)[0] == v.offset and library._stamp is None
    moved = library.move_pages(v, 1)
    library.save(moved)  # 读不了就不写：只存在内存里，翻页不断
    assert library.position(book)[0] == moved.offset
    assert json.loads(state_file.read_bytes())[book.id]["offset"] == v.offset
    monkeypatch.undo()
    assert library.position(book)[0] == v.offset and library._stamp is not None


def test_bad_saved_values_are_clamped(books_dir: Path, state_file: Path) -> None:
    lib = Library(books_dir, state_file)
    book = novel(lib)
    lib.progress[book.id] = {"offset": 10**9, "font_size": 99}
    offset, size = lib.position(book)
    assert offset == book.length and size == 24


@pytest.mark.parametrize("bad", ["abc", None, [1], {"a": 1}, float("inf"), float("nan")])
def test_a_bad_offset_counts_as_not_read(library: Library, bad: Any) -> None:
    book = novel(library)
    library.progress[book.id] = {"offset": bad, "font_size": 24}
    assert library.position(book) == (0, 24)


@pytest.mark.parametrize("bad", ["昨天", None, [1], {"a": 1}, float("nan")])
def test_a_bad_timestamp_blocks_neither_finding_nor_the_latest_book(library: Library, bad: Any) -> None:
    a, b = (library.book(e.id) for e in library.entries()[:2])
    library.save(library.view(a))
    library.save(library.view(b))
    library.progress[a.id]["updated_ts"] = bad
    assert library.last_read(a.id) == 0.0
    assert library.recent().id == b.id
    assert {e.id for e in library.find("")} >= {a.id, b.id}


def test_a_number_in_a_string_still_counts(library: Library) -> None:
    book = novel(library)
    library.progress[book.id] = {"offset": "120"}
    assert library.position(book)[0] == 120


def test_edited_file_is_reloaded(library: Library, books_dir: Path) -> None:
    book = novel(library)
    path = books_dir / "测试长篇.txt"
    path.write_text(make_novel(chapters=2), encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    again = library.book(book.id)
    assert again is not book and len(again.chapters) == 3


def test_deleted_file(library: Library, books_dir: Path) -> None:
    book_id = library.find("测试长篇")[0].id
    (books_dir / "测试长篇.txt").unlink()
    assert library.book(book_id) is None
    library.scan()
    assert book_id not in {e.id for e in library.entries()}


def test_unreadable_file(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_: Any, **__: Any) -> None:
        raise PermissionError(errno.EACCES, "没有权限")

    monkeypatch.setattr(library_mod.Book, "load", denied)
    assert library.book(library.find("测试长篇")[0].id) is None


def test_failed_write_does_not_stop_turning(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    book = novel(library)
    start, _ = library.turn(book, "s", ZERO)

    def full(*_: Any) -> None:
        raise OSError(errno.ENOSPC, "磁盘满了")

    monkeypatch.setattr(library_mod.os, "replace", full)
    v, _ = library.turn(book, "s", (1, 0, 0, 0, 0, 0, 0, 0, 0))
    assert v.offset > start.offset and library.position(book)[0] == v.offset
    library.set_display(brightness=40)
    assert library.display.brightness == 40


def test_only_the_two_latest_books_stay_parsed(books_dir: Path, state_file: Path) -> None:
    (books_dir / "第三本.txt").write_text("正文\n", encoding="utf-8")
    lib = Library(books_dir, state_file)
    a, b, c = (e.id for e in lib.entries())
    calls: list[int] = []
    lib.on_evict.append(lambda: calls.append(1))
    first = lib.book(a)
    lib.book(b)
    assert lib.book(a) is first and not calls  # 刚用过的排到后面
    lib.book(c)
    assert lib.book(a) is first and len(calls) == 1
    assert list(lib._books) == [c, a]


def test_edited_and_removed_books_are_evicted(library: Library, books_dir: Path) -> None:
    calls: list[int] = []
    library.on_evict.append(lambda: calls.append(1))
    book = novel(library)
    path = books_dir / "测试长篇.txt"
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert library.book(book.id) is not book and len(calls) == 1
    path.unlink()
    library.scan()
    assert len(calls) == 2


def test_evicted_book_is_released_from_the_page_caches(books_dir: Path, state_file: Path) -> None:
    (books_dir / "第三本.txt").write_text("正文\n", encoding="utf-8")
    lib = Library(books_dir, state_file)
    a, b, c = (e.id for e in lib.entries())
    book = lib.book(a)
    lib.page_image(lib.view(book))
    gone = weakref.ref(book)
    del book
    lib.book(b)
    lib.book(c)
    gc.collect()
    assert gone() is None


def test_page_image_cache_and_formats(library: Library) -> None:
    v = library.view(novel(library))
    png = library.page_image(v)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and library.page_image(v) is png  # 缓存
    assert library.page_image(v, "jpg")[:3] == b"\xff\xd8\xff"


def test_long_book_opens_fast(tmp_path: Path) -> None:
    """几 MB 的长篇：解析 + 分一章 + 渲染一页要远小于平台 3 s 的工具超时（research §1 约束 4）。"""
    d = tmp_path / "books"
    d.mkdir()
    (d / "长篇.txt").write_text(make_novel(chapters=600, paragraphs=40), encoding="utf-8")
    assert (d / "长篇.txt").stat().st_size > 2_000_000
    lib = Library(d, tmp_path / "p.json")
    t0 = time.perf_counter()
    book = lib.book(lib.find("长篇")[0].id)
    v = lib.jump(book, percent=60)
    lib.page_image(v)
    assert time.perf_counter() - t0 < 1.5


def saved(font: int = 0, weight: int = 0, brightness: int = 0, spacing: int = 0, *, nxt: int = 0) -> tuple[int, ...]:
    return (nxt, 0, 0, 0, 0, font, weight, brightness, spacing)


def test_display_defaults(library: Library) -> None:
    assert library.display == Display(DEFAULT_WEIGHT, 100, DEFAULT_SPACING)


def test_page_settings_are_what_the_page_shows(library: Library) -> None:
    book = novel(library)
    assert library.page_settings(book) == {"f": 24, "w": DEFAULT_WEIGHT + 1, "b": 100, "l": DEFAULT_SPACING, "o": 0}
    library.set_display(weight=0, brightness=10, spacing=125)
    assert library.page_settings(book) == {"f": 24, "w": 1, "b": 10, "l": 125, "o": 225}


@pytest.mark.parametrize("brightness", BRIGHTNESS_LEVELS)
def test_mask_opacity_moves_25_per_step(library: Library, brightness: int) -> None:
    library.set_display(brightness=brightness)
    assert library.page_settings(novel(library))["o"] == (100 - brightness) // 10 * 25  # 页面上每档加减 25


def test_saving_each_setting(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    v, _ = library.turn(book, "s", saved(font=20))
    assert v.font_size == 20
    library.turn(book, "s", saved(font=20, weight=5))
    assert library.display.weight == 4
    library.turn(book, "s", saved(font=20, weight=5, brightness=40))
    assert library.display.brightness == 40
    library.turn(book, "s", saved(font=20, weight=5, brightness=40, spacing=170))
    assert library.display == Display(4, 40, 170)


@pytest.fixture
def two(library: Library) -> tuple[Library, Library]:
    """页图版开着页面，文字版在另一个进程里改设置。"""
    return library, Library(library.books_dir, library.state_file)


def test_leaving_the_brightness_panel_untouched_keeps_the_other_instances_value(two: tuple[Library, Library]) -> None:
    image, text = two
    book = novel(image)
    image.turn(book, "s", (0, 0, 0, 0, 0, 0, 0, 0, 0))  # 页面打开时拿到的亮度是 100
    text.set_display(brightness=50)
    image.turn(book, "s", (1, 0, 0, 0, 0, 0, 0, 0, 0))
    v, note = image.turn(book, "s", (1, 0, 0, 0, 0, 0, 0, 100, 0))  # 进亮度面板没调，单击退出也写 ub
    assert note == "repeat" and text.display.brightness == 50
    image.turn(book, "s", (2, 0, 0, 0, 0, 0, 0, 100, 0))
    assert text.display.brightness == 50  # 地址里一直带着这个值


def test_a_brightness_changed_in_the_panel_is_saved_even_back_to_the_start(two: tuple[Library, Library]) -> None:
    image, text = two
    book = novel(image)
    image.turn(book, "s", (0, 0, 0, 0, 0, 0, 0, 0, 0))
    image.turn(book, "s", (0, 0, 0, 0, 0, 0, 0, 80, 0))
    assert text.display.brightness == 80
    image.turn(book, "s", (0, 0, 0, 0, 0, 0, 0, 100, 0))  # 调回打开时的值也是调过
    assert text.display.brightness == 100


def test_the_brightness_a_page_opened_with_is_per_session(two: tuple[Library, Library]) -> None:
    image, text = two
    book = novel(image)
    image.turn(book, "s", (0, 0, 0, 0, 0, 0, 0, 0, 0))
    text.set_display(brightness=50)
    image.turn(book, "new", (0, 0, 0, 0, 0, 0, 0, 0, 0))  # 重新打开，这次拿到的是 50
    image.turn(book, "new", (0, 0, 0, 0, 0, 0, 0, 100, 0))
    assert text.display.brightness == 100  # 从 50 调到 100


def test_an_untouched_brightness_panel_does_not_hold_back_a_new_size(two: tuple[Library, Library]) -> None:
    image, text = two
    book = novel(image)
    image.turn(book, "s", (0, 0, 0, 0, 0, 0, 0, 0, 0))
    text.set_display(brightness=50)
    v, _ = image.turn(book, "s", (0, 0, 0, 0, 0, 30, 0, 100, 0))
    assert v.font_size == 30 and text.display.brightness == 50


def test_saved_value_repeating_in_every_address_is_not_an_action(library: Library) -> None:
    book = novel(library)
    start, _ = library.turn(book, "s", ZERO)
    library.turn(book, "s", saved(weight=5))
    _, note = library.turn(book, "s", saved(weight=5))
    assert note == "repeat"
    v, _ = library.turn(book, "s", saved(weight=5, nxt=1))
    assert v.offset > start.offset and library.display.weight == 4


def test_first_request_of_a_session_only_records_the_settings(library: Library) -> None:
    book = novel(library)
    v, note = library.turn(book, "s", saved(font=20, weight=5, brightness=40, spacing=170))  # 页面经 on-new-intent 换了会话
    assert note == "baseline" and v.font_size == 24 and library.display == Display()


def test_old_settings_in_the_address_do_not_undo_the_other_instance(books_dir: Path, state_file: Path) -> None:
    image, text = Library(books_dir, state_file), Library(books_dir, state_file)
    book = novel(image)
    old = saved(font=20, brightness=80)
    image.turn(book, "s1", ZERO)
    image.turn(book, "s1", old)
    assert image.display.brightness == 80 and image.position(book)[1] == 20
    text.set_display(brightness=50)
    text.save(text.view(book, None, 28))
    for session, address in (("s2", old), ("s1", old), ("s1", saved(font=20, brightness=80, nxt=1))):
        v, _ = image.turn(book, session, address)  # 新会话的第一次请求、老会话的重复地址、翻页
        assert image.display.brightness == 50 and v.font_size == 28
    assert image.position(book)[1] == 28


def test_adjusting_one_setting_leaves_the_stale_ones_alone(books_dir: Path, state_file: Path) -> None:
    image, text = Library(books_dir, state_file), Library(books_dir, state_file)
    book = novel(image)
    image.turn(book, "s", ZERO)
    image.turn(book, "s", saved(font=20, brightness=80))
    text.set_display(brightness=50)
    v, _ = image.turn(book, "s", saved(font=22, brightness=80))  # 只调了字号
    assert v.font_size == 22 and image.display.brightness == 50
    image.turn(book, "s", saved(font=22, brightness=60))
    assert image.display.brightness == 60


def test_setting_back_to_an_earlier_value_counts(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", saved(brightness=80))
    library.turn(book, "s", saved(brightness=60))
    library.turn(book, "s", saved(brightness=80))
    assert library.display.brightness == 80


@pytest.mark.parametrize("bad", [saved(font=17), saved(font=99), saved(weight=7), saved(brightness=55), saved(spacing=142)])
def test_unknown_saved_values_change_nothing(library: Library, bad: tuple[int, ...]) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    v, _ = library.turn(book, "s", bad)
    assert library.display == Display() and v.font_size == 24


def test_spacing_repaginates_at_the_same_text(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    before, _ = library.turn(book, "s", saved(nxt=3))
    after, _ = library.turn(book, "s", saved(spacing=170, nxt=3))
    assert library.display.spacing == 170
    start, end = after.offset, after.pages.pages[after.page].lines[-1].offset
    assert len(after.pages.pages) > len(before.pages.pages) and start <= before.offset <= end


def test_display_is_global_and_survives_restart(library: Library) -> None:
    library.set_display(weight=3, brightness=40, spacing=155)
    fresh = Library(library.books_dir, library.state_file)
    assert fresh.display == library.display
    assert fresh.recent() is None  # 显示设置不算阅读记录


def test_pace_survives_restart(library: Library) -> None:
    assert library.display.pace == DEFAULT_PACE
    library.set_display(pace=30)
    assert Library(library.books_dir, library.state_file).display.pace == 30


def test_real_brightness_survives_restart(library: Library) -> None:
    assert library.display.optics == DEFAULT_OPTICS
    library.set_display(optics=30)
    assert Library(library.books_dir, library.state_file).display.optics == 30


def test_a_saved_real_brightness_outside_the_levels_falls_back(library: Library) -> None:
    library.set_display(pace=30, optics=35)
    assert Library(library.books_dir, library.state_file).display == Display(pace=30)


def test_an_old_image_version_replacing_the_display_keeps_the_pace(library: Library) -> None:
    library.set_display(pace=34)
    data = json.loads(library.state_file.read_text(encoding="utf-8"))
    data["_display"] = {"weight": 2, "brightness": 50, "spacing": 140}  # 升级前的页图版保存时整项替换 _display
    library.state_file.write_text(json.dumps(data), encoding="utf-8")
    assert library.display == Display(2, 50, 140, 34)


def test_a_pace_saved_in_the_old_place_still_counts(library: Library) -> None:
    library.set_display(brightness=100, pace=DEFAULT_PACE)
    data = json.loads(library.state_file.read_text(encoding="utf-8"))
    del data["_text"]
    data["_display"]["pace"] = 34
    library.state_file.write_text(json.dumps(data), encoding="utf-8")
    assert library.display.pace == 34
    library.set_display(pace=library.display.pace)
    saved = json.loads(library.state_file.read_text(encoding="utf-8"))
    assert saved["_text"] == {"pace": 34} and Library(library.books_dir, library.state_file).display.pace == 34


def test_saving_display_keeps_keys_it_does_not_know(library: Library) -> None:
    library.set_display(brightness=100)
    data = json.loads(library.state_file.read_text(encoding="utf-8"))
    data["_display"]["newer"] = 7  # 另一个实例比这边新
    library.state_file.write_text(json.dumps(data), encoding="utf-8")
    library.set_display(brightness=40)
    saved = json.loads(library.state_file.read_text(encoding="utf-8"))["_display"]
    assert saved["newer"] == 7 and saved["brightness"] == 40


def test_failed_save_keeps_keys_it_does_not_know(library: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    library.set_display(pace=30)
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    library.set_display(brightness=40, pace=30)
    assert library.progress["_text"]["pace"] == 30 and library.display.brightness == 40


def test_image_page_settings_leave_the_pace_alone(library: Library) -> None:
    book = novel(library)
    library.set_display(pace=30)
    library.turn(book, "s1", (0, 0, 0, 0, 0))
    assert library.turn(book, "s1", (0, 0, 0, 0, 0, 0, 0, 100, 0))[1] == "repeat"  # 设置没变，不算操作
    library.turn(book, "s1", (0, 0, 0, 0, 0, 0, 0, 40, 0))
    assert library.display == Display(brightness=40, pace=30)


def test_bad_display_values_fall_back(library: Library) -> None:
    library.set_display(weight=3)
    data = json.loads(library.state_file.read_text(encoding="utf-8"))
    data["_display"] = {"weight": 99, "brightness": "x", "spacing": "x", "pace": 21}
    library.state_file.write_text(json.dumps(data), encoding="utf-8")
    assert Library(library.books_dir, library.state_file).display == Display()


@pytest.mark.parametrize(("old", "new"), [(65, 60), (22, 20), (15, 20), (5, 10)])
def test_old_brightness_levels_snap_to_tens(library: Library, old: int, new: int) -> None:
    library.set_display(brightness=100)
    data = json.loads(library.state_file.read_text(encoding="utf-8"))
    data["_display"]["brightness"] = old
    library.state_file.write_text(json.dumps(data), encoding="utf-8")
    assert Library(library.books_dir, library.state_file).display.brightness == new


def test_images_do_not_depend_on_brightness(library: Library) -> None:
    book = novel(library)
    v = library.view(book)
    page, title = library.page_image(v), library.title_image(book)
    library.set_display(brightness=30)
    assert library.page_image(v) == page and library.title_image(book) == title  # 亮度由页面上的遮罩决定
    library.set_display(weight=5, brightness=30)
    assert library.page_image(v) != page


def test_two_instances_share_progress_and_display(books_dir: Path, state_file: Path) -> None:
    image, text = Library(books_dir, state_file), Library(books_dir, state_file)
    a, b = (image.book(e.id) for e in image.entries()[:2])
    assert a is not None and b is not None
    image.save(image.move_pages(image.view(a), 1))
    assert text.position(a) == image.position(a)
    text.save(text.view(b))  # 另一个实例存别的书，不能盖掉刚才那本
    assert a.id in text.progress and b.id in image.progress
    assert text.recent() == image.recent()
    text.set_display(weight=4, brightness=40, spacing=170)
    assert image.display == Display(4, 40, 170) and image.position(a)[0] > 0


def test_saving_a_setting_keeps_what_the_other_instance_just_saved(books_dir: Path, state_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image, text = Library(books_dir, state_file), Library(books_dir, state_file)
    book = novel(image)
    text.set_display(weight=1, pace=20)
    image.turn(book, "s", ZERO)
    store = image.set_display

    def racing(**changes: int) -> None:  # 页图版已经读过显示设置，这时文字版存了别的
        text.set_display(weight=2, pace=34)
        store(**changes)

    monkeypatch.setattr(image, "set_display", racing)
    image.turn(book, "s", saved(brightness=70))
    assert text.display == Display(2, 70, DEFAULT_SPACING, 34)


def test_saving_only_touches_the_given_settings(library: Library) -> None:
    library.set_display(weight=1, brightness=40, spacing=170, pace=30)
    library.set_display(brightness=60)
    assert library.display == Display(1, 60, 170, 30)
    library.set_display(pace=34)
    assert library.display == Display(1, 60, 170, 34)
    library.set_display()
    assert library.display == Display(1, 60, 170, 34)


def test_only_a_pace_leaves_the_image_settings_alone(library: Library) -> None:
    library.set_display(pace=30)
    assert "_display" not in json.loads(library.state_file.read_text(encoding="utf-8"))


def test_concurrent_display_writers_lose_nothing(books_dir: Path, state_file: Path) -> None:
    def work(library: Library, name: str, values: list[int]) -> None:
        for value in values:
            library.set_display(**{name: value})

    jobs = [("brightness", [*BRIGHTNESS_LEVELS * 3, 70]), ("pace", [*list(PACES[:10]) * 3, 34])]
    threads = [threading.Thread(target=work, args=(Library(books_dir, state_file), *job)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert Library(books_dir, state_file).display == Display(brightness=70, pace=34)


def test_concurrent_writers_lose_nothing(books_dir: Path, state_file: Path) -> None:
    libraries = [Library(books_dir, state_file) for _ in range(2)]
    books = [libraries[0].book(e.id) for e in libraries[0].entries()[:2]]

    def work(library: Library, book: Any) -> None:
        for _ in range(30):
            library.save(library.view(book))

    threads = [threading.Thread(target=work, args=pair) for pair in zip(libraries, books, strict=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert {b.id for b in books} <= set(saved)
