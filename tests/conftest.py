"""公共夹具：临时书库（生成的多章小说 + 仓库里的示例小说）、配置、书库对象。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from glassreader.config import Settings, load_settings
from glassreader.library import Library

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
PUBLIC = "https://cast.example.com/reader"
TOKEN = "reader-test-token-0123456789"
KEY = "ReaderTestKey_01"
CN = "零一二三四五六七八九十"


def make_novel(chapters: int = 6, paragraphs: int = 12, empty: tuple[int, ...] = ()) -> str:
    """生成一本多章小说：每章若干段，段里有逗号、句号、引号（覆盖禁则）。``empty`` 里的章节只有标题。"""
    out = ["测试长篇", "作者：生成", ""]
    for c in range(1, chapters + 1):
        name = CN[c] if c <= 10 else str(c)
        out.append(f"第{name}章　标题{c}")
        if c in empty:
            continue
        for p in range(1, paragraphs + 1):
            out.append(f"　　这是第{c}章的第{p}段，风从海上吹来，“灯塔亮了吗？”他问。远处的雾一点一点漫上来，船在水面上慢慢漂着。")
    return "\n".join(out) + "\n"


@pytest.fixture
def books_dir(tmp_path: Path) -> Path:
    d = tmp_path / "books"
    d.mkdir()
    (d / "测试长篇.txt").write_text(make_novel(), encoding="utf-8")
    shutil.copy(SAMPLES / "雾港来信.txt", d / "雾港来信.txt")
    return d


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / "progress.json"


@pytest.fixture
def library(books_dir: Path, state_file: Path) -> Library:
    return Library(books_dir, state_file)


def make_settings(books_dir: Path, state_file: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "public_url": PUBLIC,
        "token": TOKEN,
        "key": KEY,
        "books_dir": str(books_dir),
        "state_file": str(state_file),
    }
    values.update(overrides)
    return load_settings(values, {})


@pytest.fixture
def settings(books_dir: Path, state_file: Path) -> Settings:
    return make_settings(books_dir, state_file)
