"""读 TXT、切章节。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from glassreader import book as book_module
from glassreader.book import CHAPTER_RE, MAX_CHAPTER_CHARS, Book, book_id_for, decode_text, parse_cn_number

from .conftest import SAMPLES, make_novel


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("第一章 皮箱".encode(), "第一章 皮箱"),
        (b"\xef\xbb\xbf" + "带 BOM".encode(), "带 BOM"),
        ("国标编码的小说".encode("gb18030"), "国标编码的小说"),
    ],
)
def test_decode_text(raw: bytes, expected: str) -> None:
    assert decode_text(raw) == expected


@pytest.mark.parametrize(
    "damage",
    [lambda raw: raw[:-2], lambda raw: raw[:29] + b"\xff" + raw[29:], lambda raw: raw[:999] + b"\xff" + raw[999:]],
    ids=["tail-cut", "near-start", "middle"],
)
def test_bad_byte_in_utf8_keeps_the_book(damage: Any) -> None:
    text = make_novel()
    book = Book.parse(decode_text(damage(text.encode())), title="t")
    assert [c.title for c in book.chapters] == [c.title for c in Book.parse(text, title="t").chapters]


@pytest.mark.parametrize(
    "text",
    [make_novel(), "plain ascii prelude\n" * 5000 + "国标编码的小说", "mostly English text " * 500 + "只有几个中文"],
    ids=["all-chinese", "ascii-prelude", "mostly-english"],
)
def test_gbk_is_still_gbk(text: str) -> None:
    assert decode_text(text.encode("gb18030")) == text


def test_decode_utf16_with_bom() -> None:
    assert decode_text(b"\xff\xfe" + "雾港".encode("utf-16-le")) == "雾港"


@pytest.mark.parametrize(
    ("text", "value"),
    [("12", 12), ("１２", 12), ("十", 10), ("十二", 12), ("二十", 20), ("一百零三", 103), ("两千", 2000),
     ("三万五千", 35000), ("零", 0), ("", None), ("十a", None)],
)
def test_parse_cn_number(text: str, value: int | None) -> None:
    assert parse_cn_number(text) == value


def paragraphs(book: Book) -> list[tuple[int, str]]:
    return [p for c in book.chapters for p in c.paragraphs]


def image_book(text: str, monkeypatch: pytest.MonkeyPatch) -> Book:
    """页图版（main）眼里的这本书：只认 CHAPTER_RE，也不切超长章节。"""
    with monkeypatch.context() as m:
        m.setattr(book_module, "_LOOSE_CHAPTER_RE", CHAPTER_RE)
        m.setattr(book_module, "MAX_CHAPTER_CHARS", len(text) + 1)
        return Book.parse(text, title="t")


def test_sample_chapters() -> None:
    book = Book.load(SAMPLES / "雾港来信.txt")
    assert [c.title for c in book.chapters] == ["前言", "序章 灯塔", "第一章 皮箱", "第二章 第一封信", "第三章 夜渡"]
    assert [c.number for c in book.chapters] == [None, None, 1, 2, 3]
    assert book.id == book_id_for("雾港来信.txt")
    # 偏移首尾相接、覆盖全书
    assert book.chapters[0].start == 0
    for a, b in zip(book.chapters, book.chapters[1:], strict=False):
        assert a.end == b.start
    assert book.chapters[-1].end == book.length
    # 段落去掉了全角空格缩进
    assert all(not p.startswith("　") for c in book.chapters for _, p in c.paragraphs)


def test_heading_variants() -> None:
    text = "\n".join([
        "第1章 数字", "正文一", "第十二回：章回体", "正文二", "Chapter 3 English", "正文三",
        "番外 后日谈", "正文四", "第三章说的是", "第三章　这一行不是标题，因为太长了太长了太长了太长了太长了太长了太长了", "正文五",
    ])
    titles = [c.title for c in Book.parse(text, title="t").chapters]
    assert titles == ["第1章 数字", "第十二回：章回体", "Chapter 3 English", "番外 后日谈"]


@pytest.mark.parametrize(
    ("first", "second", "titles", "numbers"),
    [
        ("第一章风起云涌", "第二章夜渡", ["第一章风起云涌", "第二章夜渡"], [1, 2]),
        ("第1章风起", "第2章夜渡", ["第1章风起", "第2章夜渡"], [1, 2]),
        ("第一章、风起", "第二章、夜渡", ["第一章、风起", "第二章、夜渡"], [1, 2]),
        ("第一章，风起", "第二章，夜渡", ["第一章，风起", "第二章，夜渡"], [1, 2]),
        ("第一章.风起", "第二章.夜渡", ["第一章.风起", "第二章.夜渡"], [1, 2]),
        ("第一章（上）", "第二章（下）", ["第一章（上）", "第二章（下）"], [1, 2]),
        ("第 1 章 风起", "第　2　章　夜渡", ["第 1 章 风起", "第 2 章 夜渡"], [1, 2]),
        ("正文 第一章 风起", "正文：第二章 夜渡", ["第一章 风起", "第二章 夜渡"], [1, 2]),
        ("第1话 风起", "第2话 夜渡", ["第1话 风起", "第2话 夜渡"], [1, 2]),
        ("CHAPTER 1", "CHAPTER 2", ["CHAPTER 1", "CHAPTER 2"], [None, None]),
    ],
)
def test_loose_headings(first: str, second: str, titles: list[str], numbers: list[int | None]) -> None:
    book = Book.parse(f"{first}\n正文一\n{second}\n正文二\n第三章归来\n正文三\n", title="t")
    assert [c.title for c in book.chapters] == [*titles, "第三章归来"]
    assert [c.number for c in book.chapters] == [*numbers, 3]
    assert book.chapter_at(book.length - 1) == 2


PARA = "这是一段正文，内容不重要。"


def body(*heads: str) -> str:
    return "".join(f"{head}\n　　{PARA}\n{PARA}\n" for head in heads)


@pytest.mark.parametrize(
    ("text", "chapters"),
    [
        (body(*[f"第{i}章风起云涌" for i in range(1, 31)]), 30),
        (body(*[f"第{i}话 风起" for i in range(1, 31)]), 30),
        (body(*[f"正文 第{i}章 风起" for i in range(1, 31)]), 30),
        (body(*[f"第 {i} 章 风起" for i in range(1, 31)]), 30),
        (body(*[f"CHAPTER {i} Rise" for i in range(1, 31)]), 30),
        (body(*[f"第{i}章（上）" for i in range(1, 31)]), 30),
        (body("第一卷 启程", *[f"第{i}章风起" for i in range(1, 11)], "第二卷 归来", *[f"第{i}章风起" for i in range(11, 21)]), 22),
        (body(*[f"第{i}章 风起" if i % 4 == 0 else f"第{i}章风起" for i in range(1, 41)]), 40),
        ("简介写在这里\n" + body(*[f"第{i}章风起" for i in range(1, 11)]), 11),
        (body("楔子", *[f"第{i}章风起" for i in range(1, 11)], "后记"), 12),
        (make_novel(), 7),
    ],
)
def test_offsets_match_the_image_version(text: str, chapters: int, monkeypatch: pytest.MonkeyPatch) -> None:
    book, image = Book.parse(text, title="t"), image_book(text, monkeypatch)
    assert len(book.chapters) == chapters
    assert paragraphs(book) == paragraphs(image) and book.length == image.length  # 两个实例共用进度文件


def test_a_split_book_still_matches_the_image_version(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "\n".join(f"第{i}章风起\n{PARA * 5000}\n{PARA * 5000}" for i in range(1, 5))
    book, image = Book.parse(text, title="t"), image_book(text, monkeypatch)
    assert len(book.chapters) > len(image.chapters) and paragraphs(book) == paragraphs(image)


def test_a_loose_heading_stays_in_the_text_and_is_not_spoken_twice() -> None:
    book = Book.parse(body("第一章风起", "第二章 夜渡", "第三章归来", "第四章散场"), title="t")
    loose, strict = book.chapters[0], book.chapters[1]
    assert loose.paragraphs[0] == (0, "第一章风起") and loose.spoken == ""
    assert strict.spoken == "第二章 夜渡" and strict.paragraphs[0][1] == PARA  # 这一行 CHAPTER_RE 认，不占偏移


@pytest.mark.parametrize("lines", [("第一节课开始了", "第二节课也开始了"), ("第一回合他赢了", "第二回合他输了")])
def test_two_stray_lines_do_not_split_a_book_without_headings(lines: tuple[str, ...]) -> None:
    assert len(Book.parse("\n".join(["第一段", lines[0], "第二段", lines[1], "第三段"]), title="t").chapters) == 1


def test_lines_that_look_like_headings_only_move_chapter_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [f"第{n}回写的是那件事" for n in "一二三四五"]
    text = body("第一章 风起", "第二章 夜渡", "第三章 归来") + "\n".join(lines)
    book, image = Book.parse(text, title="t"), image_book(text, monkeypatch)
    assert len(book.chapters) > len(image.chapters) and paragraphs(book) == paragraphs(image)


def test_spaced_heading_jumps_by_number() -> None:
    book = Book.parse("第 1 章 风起\n一\n第 2 章 夜渡\n二\n第 十二 章 归来\n三\n", title="t")
    assert book.find_chapter("第二章") == 1 and book.find_chapter("第 2 章") == 1 and book.find_chapter("2") == 1
    assert book.find_chapter("第十二章") == 2
    assert book.find_chapter("第九章") is None


def test_loose_chapters_beside_a_regular_prologue() -> None:
    book = Book.parse("楔子\n一\n第一章风起\n二\n第二章夜渡\n三\n第三章归来\n四\n", title="t")
    assert [c.title for c in book.chapters] == ["楔子", "第一章风起", "第二章夜渡", "第三章归来"]
    assert book.find_chapter("第三章") == 3


@pytest.mark.parametrize("line", ["第三章说的是", "第一部分的内容", "第三回头看了一眼"])
def test_stray_lines_do_not_change_a_regular_book(line: str) -> None:
    plain = make_novel()
    book = Book.parse(plain.replace("第二章　标题2\n", f"第二章　标题2\n{line}\n"), title="t")
    assert [c.title for c in book.chapters] == [c.title for c in Book.parse(plain, title="t").chapters]
    assert any(p == line for _, p in book.chapters[2].paragraphs)  # 还是正文，偏移不变


@pytest.mark.parametrize("line", ["第三章说的是", "第一部分的内容"])
def test_one_stray_line_does_not_split_a_book_without_headings(line: str) -> None:
    assert len(Book.parse(f"第一段\n{line}\n第二段\n", title="t").chapters) == 1


@pytest.mark.parametrize("ending", ["。", "！", "？", "……", "”", "，", "：", ".", "!"])
def test_sentences_are_not_loose_headings(ending: str) -> None:
    lines = [f"第{n}章他说了这句话{ending}" for n in "一二三四"]
    assert [c.title for c in Book.parse("\n".join(lines), title="散文").chapters] == ["散文"]


def test_no_headings_is_one_chapter() -> None:
    book = Book.parse("第一行\n\n第二行\n", title="散文")
    assert [c.title for c in book.chapters] == ["散文"]
    assert book.length == len("第一行第二行")


def test_find_chapter_prefers_real_headings_over_lines_that_look_like_them() -> None:
    lines = [f"第{n}章 真标题{n}\n" + "".join(f"第{n}节课老师走进来\n正文。\n第{n}回合开始了\n正文。\n" for _ in range(3)) for n in (1, 2, 3)]
    book = Book.parse("".join(lines), title="形似标题")
    assert len(book.chapters) == 21  # 宽松分章
    for query in ("第3章", "第三章", "3", "第 3 章"):
        assert book.chapters[book.find_chapter(query)].title == "第3章 真标题3"
    assert book.chapters[book.find_chapter("第2节")].title == "第2节课老师走进来"
    assert book.chapters[book.find_chapter("第2回")].title == "第2回合开始了"
    assert book.find_chapter("第四章") is None


def test_find_chapter_without_a_unit_prefers_the_real_heading() -> None:
    lookalikes = "".join(f"第{n}节课老师走进来\n正文。\n第{n}回合开始了\n正文。\n" for n in (3, 2, 3, 2))
    book = Book.parse(f"第1章 甲\n正文。\n{lookalikes}第2章 乙\n正文。\n第3章 丙\n正文。\n", title="形似标题")
    assert book.chapters[book.find_chapter("3")].title == "第3章 丙"  # 形似的行排在前面
    assert book.chapters[book.find_chapter("二")].title == "第2章 乙"


def test_find_chapter_goes_by_unit_among_real_headings() -> None:
    book = Book.parse("第一章 甲\n第1节 子\n正文。\n第2节 丑\n正文。\n第二章 乙\n第1节 寅\n正文。\n", title="t")
    assert book.chapters[book.find_chapter("第二章")].title == "第二章 乙"
    assert book.chapters[book.find_chapter("第2节")].title == "第2节 丑"
    assert book.chapters[book.find_chapter("第1节")].title == "第1节 子"


def test_find_chapter_among_loose_headings_keeps_the_written_unit() -> None:
    book = Book.parse("".join(f"第{n}节课老师走进来\n正文。\n第{n}章夜渡\n正文。\n" for n in (1, 2, 3)), title="t")
    assert book.chapters[book.find_chapter("第2章")].title == "第2章夜渡"


def test_long_book_without_headings_splits_at_paragraphs() -> None:
    para = "风从海上吹来。" * 100
    book = Book.parse("\n".join([para] * 200), title="散文")
    assert [c.title for c in book.chapters] == ["散文 1", "散文 2", "散文 3"]
    assert [p for c in book.chapters for p in c.paragraphs] == [(i * len(para), para) for i in range(200)]  # 偏移不变
    assert book.chapters[0].start == 0 and book.chapters[-1].end == book.length == 200 * len(para)
    for a, b in zip(book.chapters, book.chapters[1:], strict=False):
        assert a.end == b.start and book.chapter_at(b.start) == book.chapters.index(b)
    assert all(c.end - c.start < MAX_CHAPTER_CHARS + len(para) for c in book.chapters)
    assert book.find_chapter("第二章") == 1  # 没有编号的书按序号算


def test_long_numbered_chapter_keeps_its_number() -> None:
    para = "风从海上吹来。" * 100
    book = Book.parse("\n".join(["第一章 风起", *[para] * 100, "第二章 夜渡", "正文"]), title="t")
    assert [c.title for c in book.chapters] == ["第一章 风起 1", "第一章 风起 2", "第二章 夜渡"]
    assert [c.spoken for c in book.chapters] == ["第一章 风起", "", "第二章 夜渡"]  # 序号只用来排版
    assert [c.base_title for c in book.chapters] == ["第一章 风起", "第一章 风起", "第二章 夜渡"]
    assert [c.number for c in book.chapters] == [1, 1, 2]
    assert book.find_chapter("第一章") == 0 and book.find_chapter("第二章") == 2


def test_single_long_paragraph_stays_whole() -> None:
    book = Book.parse("字" * (MAX_CHAPTER_CHARS + 1), title="散文")
    assert [c.title for c in book.chapters] == ["散文"]


def test_front_matter_only_when_not_blank() -> None:
    assert Book.parse("\n　\n第一章 开始\n正文\n", title="t").chapters[0].title == "第一章 开始"


def test_find_chapter() -> None:
    book = Book.parse(make_novel(chapters=12), title="t")
    assert book.chapters[book.find_chapter("第三章")].title == "第三章 标题3"
    assert book.chapters[book.find_chapter("3")].title == "第三章 标题3"
    assert book.chapters[book.find_chapter("三")].title == "第三章 标题3"
    assert book.chapters[book.find_chapter("第十章")].title == "第十章 标题10"
    assert book.chapters[book.find_chapter("标题7")].title == "第七章 标题7"
    assert book.find_chapter("第九十章") is None  # 有编号的书不按序号乱猜
    assert book.find_chapter("不存在") is None
    assert book.find_chapter("") is None


def test_find_chapter_without_numbers_uses_order() -> None:
    book = Book.parse("序章 起\n一\n楔子 承\n二\n尾声 合\n三\n", title="t")
    assert book.find_chapter("第二章") == 1
    assert book.find_chapter("第四章") is None


def test_chapter_at_and_readable() -> None:
    book = Book.parse(make_novel(chapters=4, empty=(2,)), title="t")
    ch2, ch3 = book.chapters[2], book.chapters[3]  # 0 前言 1 第一章 2 第二章（空）3 第三章
    assert ch2.start == ch2.end == ch3.start
    assert book.chapter_at(ch2.start) == 3  # 空章节和下一章同一起点：正文属于下一章
    assert book.readable() == [0, 1, 3, 4]
    assert book.chapter_at(-5) == 0
    assert book.chapter_at(book.length + 100) == len(book.chapters) - 1


def test_percent() -> None:
    book = Book.parse(make_novel(), title="t")
    assert book.percent(0) == 0
    assert book.percent(book.length) == 100
    assert book.percent(book.length * 10) == 100


def test_load_reads_gbk_file(tmp_path: Path) -> None:
    path = tmp_path / "旧小说.txt"
    path.write_bytes(make_novel(chapters=2).encode("gb18030"))
    book = Book.load(path)
    assert book.title == "旧小说"
    assert book.chapters[1].title == "第一章 标题1"
    assert book.mtime > 0
