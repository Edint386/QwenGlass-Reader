"""HTTP 路由：页图（key 鉴权、计数翻页、通用头）、MCP 鉴权、访问日志打码。"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from glassreader.config import Settings
from glassreader.library import Library
from glassreader.server import create_app, redact_path

from .conftest import KEY, PUBLIC, TOKEN, make_settings


@pytest.fixture
def client(settings: Settings, library: Library) -> Iterator[TestClient]:
    with TestClient(create_app(settings, library), base_url=PUBLIC.split("/reader")[0]) as c:
        yield c


@pytest.fixture
def book_id(library: Library) -> str:
    return library.find("测试长篇")[0].id


def page_url(book_id: str, counters: str = "0-0-0-0-0-0", ext: str = "png", key: str = KEY, sess: str = "s1") -> str:
    return f"/r/{key}/{book_id}/{sess}/{counters}.{ext}"


def assert_common_headers(r) -> None:  # noqa: ANN001
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["cross-origin-resource-policy"] == "cross-origin"


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"
    assert_common_headers(r)


def test_page_png(client: TestClient, book_id: str) -> None:
    r = client.get(page_url(book_id))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert_common_headers(r)
    assert Image.open(io.BytesIO(r.content)).size == (478, 290)
    assert r.headers["x-reader-chapter"] == "1" and r.headers["x-reader-page"].startswith("1/")
    assert r.headers["x-reader-font"] == "24"


def test_page_jpg(client: TestClient, book_id: str) -> None:
    r = client.get(page_url(book_id, ext="jpg"))
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:3] == b"\xff\xd8\xff"


def test_title_image(client: TestClient, book_id: str) -> None:
    r = client.get(f"/r/{KEY}/{book_id}/s1/title.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert_common_headers(r)
    assert client.get(f"/r/WrongKey/{book_id}/s1/title.png").status_code == 404
    assert client.get(f"/r/{KEY}/0000000000/s1/title.png").status_code == 404


def test_title_does_not_move_the_position(client: TestClient, book_id: str) -> None:
    first = client.get(page_url(book_id, "0-0-0-0-0-0-0"))
    client.get(f"/r/{KEY}/{book_id}/s1/title.png")
    assert client.get(page_url(book_id, "0-0-0-0-0-0-0")).headers["X-Reader-Page"] == first.headers["X-Reader-Page"]


def test_saved_settings_in_the_address(client: TestClient, book_id: str) -> None:
    assert client.get(page_url(book_id, "0-0-0-0-0-0-0-0-0")).status_code == 200
    r = client.get(page_url(book_id, "0-0-0-0-0-0-5-0-0"))
    assert r.status_code == 200 and client.app.state.library.display.weight == 4


def test_turning_pages(client: TestClient, book_id: str) -> None:
    pages = [client.get(page_url(book_id, f"{n}-0-0-0-0-0")).headers["x-reader-page"] for n in (0, 0, 1, 1, 2)]
    # 首次只记基线；重复地址不动；每加一翻一页
    assert pages[0] == pages[1] and pages[2] == pages[3] != pages[0]
    r = client.get(page_url(book_id, "2-0-1-0-0-0"))
    assert r.headers["x-reader-chapter"] != "1" and r.headers["x-reader-page"].startswith("1/")
    r = client.get(page_url(book_id, "2-0-1-0-0-28-0-0-0"))
    assert r.headers["x-reader-font"] == "28"


@pytest.mark.parametrize(
    "path",
    [
        "/r/WrongKey/{book}/s1/0-0-0-0-0-0.png",  # key 不对
        "/r/{key}/0000000000/s1/0-0-0-0-0-0.png",  # 书不存在
        "/r/{key}/NOT-A-BOOK/s1/0-0-0-0-0-0.png",  # 书 id 格式不对
        "/r/{key}/{book}/s%201/0-0-0-0-0-0.png",  # 会话号带空格
        "/r/{key}/{book}/s1/0-0-0-0-0-0.gif",  # 后缀不对
        "/r/{key}/{book}/s1/a-b.png",  # 计数不是数字
        "/r/{key}/{book}/s1",
        "/s/{key}/screen.jpg",
    ],
)
def test_bad_page_paths_are_404(client: TestClient, book_id: str, path: str) -> None:
    r = client.get(path.format(key=KEY, book=book_id))
    assert r.status_code == 404
    assert KEY not in r.text
    assert_common_headers(r)


def test_deleted_book_is_404(client: TestClient, book_id: str, books_dir: Path) -> None:
    assert client.get(page_url(book_id)).status_code == 200
    (books_dir / "测试长篇.txt").unlink()
    assert client.get(page_url(book_id, "1-0-0-0-0-0")).status_code == 404


def test_mcp_requires_token(client: TestClient) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream"}
    r = client.post("/mcp", json=body, headers=headers)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert_common_headers(r)
    assert client.post("/mcp", json=body, headers={**headers, "Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 405


@pytest.mark.parametrize("auth", [f"Bearer {TOKEN}", TOKEN, f"Bearer Bearer {TOKEN}"])
def test_mcp_token_forms(client: TestClient, auth: str) -> None:
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Authorization": auth, "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": "2025-06-18"},
    )
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"list_books", "open_book", "jump", "reading_status"}


def test_mcp_rejects_foreign_host(settings: Settings, library: Library) -> None:
    with TestClient(create_app(settings, library), base_url="http://evil.example.com") as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                   headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"})
    assert r.status_code == 421


def test_access_log_redacts_key(client: TestClient, book_id: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        client.get(page_url(book_id))
        client.get(page_url(book_id, key="WrongKey"))
    # 只看服务端自己的日志（测试客户端 httpx2 会打出它请求的完整地址）
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("glassreader"))
    assert "/r/…/" in text and KEY not in text and "WrongKey" not in text


def test_redact_path() -> None:
    assert redact_path(f"/r/{KEY}/abc/s/1.png") == "/r/…/abc/s/1.png"
    assert redact_path("/mcp") == "/mcp"


def test_create_app_needs_secrets(books_dir: Path, state_file: Path, library: Library) -> None:
    settings = make_settings(books_dir, state_file)
    from dataclasses import replace

    with pytest.raises(ValueError):
        create_app(replace(settings, key=""), library)
    with pytest.raises(ValueError):
        create_app(replace(settings, token=""), library)
