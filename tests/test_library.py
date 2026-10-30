"""书库、按计数差值翻页、跳转、进度文件。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from glassreader import library as library_mod
from glassreader.book import book_id_for
from glassreader.library import Library, View, new_session_id, normalize_title, parse_counters
from glassreader.pager import FONT_SIZES

from .conftest import make_novel

ZERO = (0, 0, 0, 0, 0, 0)


def novel(lib: Library):
    return lib.book(lib.find("测试长篇")[0].id)


def where(v: View) -> tuple[int, int]:
    return v.chapter, v.page


@pytest.mark.parametrize(
    ("raw", "counters"),
    [("0-0-0-0-0-0", ZERO), ("3-1-2-0-1-0", (3, 1, 2, 0, 1, 0)), ("5", (5, 0, 0, 0, 0, 0)),
     ("1-x-2", (1, 0, 2, 0, 0, 0)), ("1-2-3-4-5-6-7", (1, 2, 3, 4, 5, 6)), ("9999999999", ZERO)],
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
    v1, _ = library.turn(book, "s1", (1, 0, 0, 0, 0, 0))
    v2, note = library.turn(book, "s1", (1, 0, 0, 0, 0, 0))  # 内核重复请求同一地址
    assert note == "repeat" and where(v2) == where(v1)
    assert library.progress[book.id]["offset"] == v1.offset


def test_turn_lost_request_is_counted(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    v, _ = library.turn(book, "s", (3, 0, 0, 0, 0, 0))  # 1、2 两个地址没发出来
    expected = library.move_pages(library.view(book, 0), 3)
    assert where(v) == where(expected)


def test_turn_stale_request_does_not_go_back(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    v4, _ = library.turn(book, "s", (4, 0, 0, 0, 0, 0))
    v, note = library.turn(book, "s", (2, 0, 0, 0, 0, 0))  # 晚到的旧地址
    assert note == "repeat" and where(v) == where(v4)


def test_new_session_baseline_keeps_position(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s1", ZERO)
    v, _ = library.turn(book, "s1", (5, 1, 0, 0, 0, 0))
    # 页面经 on-new-intent 换了新会话，计数器没归零：只记基线，不能当成 5 次翻页
    v2, note = library.turn(book, "s2", (5, 1, 0, 0, 0, 0))
    assert note == "baseline" and where(v2) == where(v)
    v3, _ = library.turn(book, "s2", (6, 1, 0, 0, 0, 0))
    assert where(v3) == where(library.move_pages(v, 1))


def test_sessions_are_per_book(library: Library) -> None:
    a = novel(library)
    b = library.book(library.find("雾港来信")[0].id)
    library.turn(a, "s", ZERO)
    v, note = library.turn(b, "s", (2, 0, 0, 0, 0, 0))
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


def test_turn_applies_font_then_chapter_then_pages(library: Library) -> None:
    book = novel(library)
    library.turn(book, "s", ZERO)
    v, _ = library.turn(book, "s", (1, 0, 2, 0, 1, 0))
    assert v.font_size == 28
    assert where(v) == (2, 1)  # 跳两章到第二章，再翻一页


def test_swap_swipe(books_dir: Path, state_file: Path) -> None:
    lib = Library(books_dir, state_file, swap_swipe=True)
    book = lib.book(lib.find("测试长篇")[0].id)
    lib.turn(book, "s", ZERO)
    v, _ = lib.turn(book, "s", (0, 1, 0, 0, 0, 0))  # “上一页”计数加一，对调后是下一页
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
    v, _ = lib.turn(book, "s", (7, 0, 0, 0, 1, 0))
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


def test_bad_saved_values_are_clamped(books_dir: Path, state_file: Path) -> None:
    lib = Library(books_dir, state_file)
    book = novel(lib)
    lib.progress[book.id] = {"offset": 10**9, "font_size": 99}
    offset, size = lib.position(book)
    assert offset == book.length and size == 24


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
