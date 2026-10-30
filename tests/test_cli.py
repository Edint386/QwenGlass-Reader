"""命令行：books / open / 配置。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from glassreader.cli import main
from glassreader.config import ConfigError, load_settings

from .conftest import KEY


def test_books(books_dir: Path, state_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["books", "--books", str(books_dir), "--state", str(state_file)]) == 0
    out = capsys.readouterr().out
    assert "2 本" in out and "测试长篇.txt  未读" in out


def test_open_prints_datasource(
    books_dir: Path, state_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("READER_KEY", KEY)
    rc = main(["open", "雾港", "--books", str(books_dir), "--state", str(state_file), "--port", "8799"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["t"] == "雾港来信"
    assert data["u"].startswith(f"http://127.0.0.1:8799/r/{KEY}/")
    # open 会记一次最近阅读
    assert json.loads(state_file.read_text(encoding="utf-8"))


def test_open_unknown_book(books_dir: Path, state_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["open", "三体", "--books", str(books_dir), "--state", str(state_file)]) == 1
    assert "书架上没有找到《三体》" in capsys.readouterr().err


def test_bad_env_is_usage_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("READER_PORT", "99999")
    assert main(["books"]) == 2
    assert "READER_PORT" in capsys.readouterr().err


def test_load_settings() -> None:
    s = load_settings({"public_url": "https://cast.example.com/reader/", "port": 8790},
                      {"READER_SWAP_SWIPE": "1", "READER_ALLOWED_HOSTS": "a.example.com"})
    assert s.public_url == "https://cast.example.com/reader" and s.port == 8790 and s.swap_swipe
    assert s.token_generated and s.key_generated and s.key.replace("-", "").replace("_", "").isalnum()
    assert s.mcp_url == "https://cast.example.com/reader/mcp"
    assert "cast.example.com" in s.mcp_allowed_hosts() and "a.example.com:*" in s.mcp_allowed_hosts()
    local = load_settings({}, {})
    assert local.base_url == "http://127.0.0.1:8780" and local.warnings()
    with pytest.raises(ConfigError):
        load_settings({"key": "带/斜杠"}, {})
    with pytest.raises(ConfigError):
        load_settings({}, {"READER_PUBLIC_URL": "ftp://x"})
    with pytest.raises(TypeError):
        load_settings({"nope": 1}, {})
