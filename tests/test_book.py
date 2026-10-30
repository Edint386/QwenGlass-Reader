"""读 TXT、切章节。"""

from __future__ import annotations

from pathlib import Path

import pytest

from glassreader.book import Book, book_id_for, decode_text, parse_cn_number

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


def test_decode_utf16_with_bom() -> None:
    assert decode_text(b"\xff\xfe" + "雾港".encode("utf-16-le")) == "雾港"


@pytest.mark.parametrize(
    ("text", "value"),
    [("12", 12), ("１２", 12), ("十", 10), ("十二", 12), ("二十", 20), ("一百零三", 103), ("两千", 2000),
     ("三万五千", 35000), ("零", 0), ("", None), ("十a", None)],
)
def test_parse_cn_number(text: str, value: int | None) -> None:
    assert parse_cn_number(text) == value


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


def test_no_headings_is_one_chapter() -> None:
    book = Book.parse("第一行\n\n第二行\n", title="散文")
    assert [c.title for c in book.chapters] == ["散文"]
    assert book.length == len("第一行第二行")


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
