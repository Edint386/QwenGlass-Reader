"""断行、分页、渲染。"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from glassreader.book import Book
from glassreader.pager import (
    FONT_SIZES,
    NO_LINE_END,
    NO_LINE_START,
    Layout,
    _widths,
    layout_for,
    paginate_chapter,
    render_page,
    wrap,
)

from .conftest import SAMPLES, make_novel

PARA = "邮差是个年轻人，戴着一顶被雨水泡软了的帽子。他把箱子放在门槛上，退后两步，像是怕它会突然打开似的。“没有寄件人，”他说，“邮局在仓库里放了二十多年。”"


@pytest.mark.parametrize("size", FONT_SIZES)
def test_wrap_keeps_text_and_rules(size: int) -> None:
    layout = layout_for(size)
    widths = _widths(layout.font, size)
    indent = widths["　"] * 2
    lines = wrap(PARA * 3, 100, layout.text_width, widths, indent)
    assert "".join(line.text for line in lines) == PARA * 3
    assert lines[0].indent and not any(line.indent for line in lines[1:])
    assert lines[-1].last and not any(line.last for line in lines[:-1])
    pos = 100
    for i, line in enumerate(lines):
        assert line.offset == pos
        pos += len(line.text)
        assert line.text, "空行"
        if i:
            assert line.text[0] not in NO_LINE_START, f"行首禁则：{line.text!r}"
        if i < len(lines) - 1:
            assert line.text[-1] not in NO_LINE_END, f"行尾禁则：{line.text!r}"
        # 只有悬挂的标点可以伸出右边距
        room = layout.text_width - (indent if line.indent else 0)
        body = line.text.rstrip("".join(NO_LINE_START))
        assert sum(widths[c] for c in body) <= room + 0.5


def test_wrap_short_paragraph_is_one_last_line() -> None:
    widths = _widths(Layout().font, 24)
    (line,) = wrap("短句。", 0, 400, widths, 48)
    assert line.text == "短句。" and line.indent and line.last


@pytest.mark.parametrize("size", [20, 24, 32])
def test_paginate_chapter_covers_text(size: int) -> None:
    book = Book.parse(make_novel(chapters=3, paragraphs=30), title="t")
    layout = layout_for(size)
    cp = paginate_chapter(book, 1, layout)
    assert len(cp.pages) > 2
    assert all(0 < len(p.lines) <= layout.body_lines for p in cp.pages)
    text = "".join(line.text for p in cp.pages for line in p.lines)
    assert text == "".join(para for _, para in book.chapters[1].paragraphs)
    starts = [p.offset for p in cp.pages]
    assert starts == sorted(starts) and starts[0] == book.chapters[1].start
    # page_at：每页范围内的偏移都落在这一页
    for i, p in enumerate(cp.pages):
        assert cp.page_at(p.offset) == i
        assert cp.page_at(p.lines[-1].offset + len(p.lines[-1].text) - 1) == i


def test_empty_chapter_has_one_blank_page() -> None:
    book = Book.parse(make_novel(chapters=3, empty=(2,)), title="t")
    cp = paginate_chapter(book, 2, Layout())
    assert len(cp.pages) == 1 and cp.pages[0].lines == []
    assert cp.pages[0].offset == book.chapters[2].start


def test_bigger_font_means_more_pages() -> None:
    book = Book.load(SAMPLES / "雾港来信.txt")
    counts = [len(paginate_chapter(book, 3, layout_for(s)).pages) for s in FONT_SIZES]
    assert counts == sorted(counts) and counts[0] < counts[-1]


def test_render_page_png() -> None:
    book = Book.load(SAMPLES / "雾港来信.txt")
    cp = paginate_chapter(book, 3, Layout())
    data = render_page(book, cp, 1, Layout())
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG" and img.size == (478, 290)
    assert len(img.getcolors()) <= 4  # 4 级灰度
    # 真机远程图预算：解码后 ≤ 742400 B（glasscast research §2.3）；文件越小翻页越快（research §1 约束 5、6）
    assert 478 * 290 * 4 <= 742400
    assert len(data) < 14 * 1024
    # 黑底：四角都是黑的（不发光）
    gray = img.convert("L")
    assert all(gray.getpixel(xy) == 0 for xy in [(0, 0), (477, 0), (0, 289), (477, 289)])


def test_render_unquantized_is_bigger() -> None:
    book = Book.load(SAMPLES / "雾港来信.txt")
    cp = paginate_chapter(book, 3, Layout())
    assert len(render_page(book, cp, 0, Layout(), levels=0)) > len(render_page(book, cp, 0, Layout()))


def test_render_long_chapter_title_is_truncated() -> None:
    book = Book.parse("第一章 " + "很长的章节名" * 4 + "\n正文。\n", title="t")
    assert len(book.chapters[0].title) > 20
    data = render_page(book, paginate_chapter(book, 0, Layout()), 0, Layout())
    assert Image.open(io.BytesIO(data)).size == (478, 290)
