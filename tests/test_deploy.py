"""部署草稿（reader/deploy/）的静态检查：本机没有 nginx，真正的 ``openresty -t`` 在服务器上做（deploy/README.md）。

这里保证几份文件之间对得上：端口、upstream 名字、glassreader 实际提供的路径、公网前缀；
以及不带 --route 时局域网转发器的代码和替换前的版本（glasscast/tools/lan_https_proxy.py.bak）逐字相同、
在用的 glasscast/tools/lan_https_proxy.py 就是本目录这份。
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from glassreader.config import DEFAULT_PORT
from glassreader.server import MCP_PATH
from glassreader.agent import AGENT_PATH

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
CLOUD = DEPLOY / "cloud"
UPSTREAM = DEPLOY / "1panel" / "zz-glassreader-upstream.conf"
SITE = DEPLOY / "1panel" / "reader.conf"
TUNNEL = DEPLOY / "tunnel.sh"
PROXY = DEPLOY / "lan_https_proxy.py"
CURRENT_PROXY = ROOT.parent / "glasscast" / "tools" / "lan_https_proxy.py"
OLD_PROXY = CURRENT_PROXY.with_name("lan_https_proxy.py.bak")  # 2026-10-30 替换前的版本
GLASSCAST_UPSTREAM = ROOT.parent / "glasscast" / "deploy" / "1panel" / "zz-glasscast-upstream.conf"
PUBLIC = "https://cast.example.com/reader"


def directives(path: Path) -> list[str]:
    """去掉注释后的语句（按 ; { } 切开）。"""
    text = "\n".join(line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())
    return [s.strip() for s in re.split(r"(?<=[;{}])", text) if s.strip()]


def locations(path: Path) -> dict[str, str]:
    """``location 修饰 路径 {…}`` → 块内文本（只有一层嵌套 limit_except）。"""
    text = "\n".join(line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())
    out = {}
    for m in re.finditer(r"location\s+(=|\^~)\s+(\S+)\s*\{((?:[^{}]|\{[^{}]*\})*)\}", text):
        out[f"{m.group(1)} {m.group(2)}"] = m.group(3)
    return out


@pytest.mark.parametrize("path", [UPSTREAM, SITE])
def test_nginx_syntax_basics(path: Path) -> None:
    stmts = directives(path)
    depth = 0
    for s in stmts:
        assert s.endswith((";", "{", "}")), f"语句没有以 ; {{ }} 结尾：{s!r}"
        depth += s.count("{") - s.count("}")
        assert depth >= 0
    assert depth == 0
    body = path.read_text(encoding="utf-8").lower()
    for banned in ("http3", "quic", "add_header", "listen ", "server_name"):
        assert banned not in "\n".join(directives(path)).lower(), banned
    assert "\t" not in body


@pytest.mark.skipif(not GLASSCAST_UPSTREAM.is_file(), reason="glasscast 不在旁边")
def test_upstream_matches_tunnel_and_glasscast_pattern() -> None:
    text = "\n".join(directives(UPSTREAM))
    assert re.findall(r"server 127\.0\.0\.1:(\d+) max_fails=0;", text) == ["18780", "18780"]
    assert "keepalive 16;" in text and "keepalive_timeout 3600s;" in text
    # 和 glasscast 的 upstream 同一套写法，只换名字和端口
    glasscast = "\n".join(directives(GLASSCAST_UPSTREAM))
    assert text.replace("glassreader_tunnel", "X").replace("18780", "P") == glasscast.replace(
        "glasscast_tunnel", "X").replace("18766", "P")
    tunnel = TUNNEL.read_text(encoding="utf-8")
    assert '"${READER_REMOTE_PORT:-18780}"' in tunnel
    assert f'"${{READER_PORT:-{DEFAULT_PORT}}}"' in tunnel
    assert f'"${{READER_PUBLIC_URL:-{PUBLIC}}}"' in tunnel


def test_site_routes_only_what_glassreader_serves() -> None:
    locs = locations(SITE)
    assert set(locs) == {"= /reader/healthz", "^~ /reader/r/", "= /reader/mcp", "^~ /reader/"}
    assert "proxy_pass http://glassreader_tunnel/healthz;" in locs["= /reader/healthz"]
    assert "proxy_pass http://glassreader_tunnel/r/;" in locs["^~ /reader/r/"]
    assert f"proxy_pass http://glassreader_tunnel{MCP_PATH};" in locs["= /reader/mcp"]
    assert "return 404;" in locs["^~ /reader/"]
    # 页图地址里有 key：不记访问日志；只允许 GET（含 HEAD）
    assert "access_log off;" in locs["^~ /reader/r/"]
    assert "limit_except GET" in locs["^~ /reader/r/"]
    assert "limit_except POST" in locs["= /reader/mcp"]
    for name in ("= /reader/healthz", "^~ /reader/r/", "= /reader/mcp"):
        body = locs[name]
        assert "proxy_http_version 1.1;" in body and 'proxy_set_header Connection "";' in body  # 走 keepalive 池
        assert "proxy_set_header Host $host;" in body  # MCP 的 Host 白名单按公网域名放行
    assert "non_idempotent" in locs["= /reader/mcp"] and "non_idempotent" not in locs["^~ /reader/r/"]


def test_tunnel_wrapper_points_at_glasscast_script() -> None:
    text = TUNNEL.read_text(encoding="utf-8")
    assert 'TUNNEL="${GLASSCAST_TUNNEL:-$HERE/../../glasscast/deploy/tunnel.sh}"' in text
    assert TUNNEL.stat().st_mode & 0o111
    sibling = (DEPLOY / ".." / ".." / "glasscast" / "deploy" / "tunnel.sh").resolve()
    if not sibling.is_file():
        pytest.skip("glasscast 不在旁边")


def _functions(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    src = path.read_text(encoding="utf-8")
    return {n.name: ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}


@pytest.mark.skipif(not OLD_PROXY.is_file(), reason="glasscast 不在旁边")
def test_byte_pipe_mode_is_unchanged() -> None:
    """不带 --route 时走的 pipe / make_handler 和替换前的版本逐字相同，替换文件不改变原有行为。"""
    new, old = _functions(PROXY), _functions(OLD_PROXY)
    for name in ("pipe", "make_handler"):
        assert new[name] == old[name], name
    assert "DEFAULT_TLS_DIR = Path.home() / \"Library\" / \"Application Support\" / \"glasscast\" / \"tls\"" in PROXY.read_text(
        encoding="utf-8")


@pytest.mark.skipif(not OLD_PROXY.is_file(), reason="glasscast 的转发器没换成本目录这份")
def test_deployed_proxy_is_this_copy() -> None:
    """换过之后（留有 .bak），在用的转发器就是本目录这份：改了一边忘了拷到另一边会失败。"""
    assert CURRENT_PROXY.read_text(encoding="utf-8") == PROXY.read_text(encoding="utf-8")


@pytest.mark.skipif(not OLD_PROXY.is_file() or shutil.which("patch") is None, reason="需要替换前的版本和 patch")
def test_review_diff_is_current(tmp_path: Path) -> None:
    """deploy/lan_https_proxy.diff 打到替换前的版本上正好得到本目录这份：改了它忘了重新生成 diff 会失败。"""
    target = tmp_path / "lan_https_proxy.py"
    shutil.copy(OLD_PROXY, target)
    subprocess.run(["patch", "-s", str(target), str(DEPLOY / "lan_https_proxy.diff")], check=True, capture_output=True)
    assert target.read_text(encoding="utf-8") == PROXY.read_text(encoding="utf-8")


def test_cloud_site_matches_compose_and_serves_only_glassreader_paths() -> None:
    compose = (CLOUD / "compose.yaml").read_text(encoding="utf-8")
    site = (CLOUD / "site.https.conf").read_text(encoding="utf-8")
    host_port, container_port = re.search(r"127\.0\.0\.1:(\d+):(\d+)", compose).groups()
    assert f"server 127.0.0.1:{host_port} " in site
    assert int(container_port) == DEFAULT_PORT
    assert re.findall(r"location\s+(?:=|\^~)?\s*(\S+)\s*\{", site) == ["/healthz", "/r/", MCP_PATH, "/text/healthz", "/text/r/", f"/text{AGENT_PATH}", "/"]


@pytest.mark.parametrize("path", ["/r/", "/text/r/"])
def test_cloud_key_paths_stay_out_of_the_logs(path: str) -> None:
    body = locations(CLOUD / "site.https.conf")[f"^~ {path}"]
    assert "access_log off;" in body
    assert re.search(r"error_log \S+ crit;", body)  # 上游连不上是 error 级别，日志行带完整请求，也就带着 key


def test_cloud_image_has_the_fallback_fonts() -> None:
    dockerfile = (CLOUD / "Dockerfile").read_text(encoding="utf-8")
    for package in ("fonts-noto-cjk", "fonts-noto-core", "fonts-dejavu-core", "fonts-unifont"):
        assert package in dockerfile


def code_lines(path: Path) -> list[str]:
    """脚本里的命令，去掉注释。"""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]


def test_cloud_image_keeps_dependencies_in_their_own_layer() -> None:
    lines = code_lines(CLOUD / "Dockerfile")
    order = ["ENV UV_COMPILE_BYTECODE=1", "COPY pyproject.toml uv.lock ./",
             "RUN uv sync --frozen --no-dev --no-install-project", "COPY glassreader ./glassreader",
             "RUN uv sync --frozen --no-dev --no-editable"]
    positions = [lines.index(line) for line in order]
    assert positions == sorted(positions)  # 代码在依赖前面的话，改一行就重装全部依赖


def test_push_clears_the_replaced_image_after_the_new_one_is_up() -> None:
    text = "\n".join(code_lines(CLOUD / "push.sh"))
    assert text.index("compose") < text.index("sudo docker image prune -f")


def test_push_tests_first_and_waits_for_a_healthy_container() -> None:
    text = "\n".join(code_lines(CLOUD / "push.sh"))
    assert text.index("uv run pytest -q") < text.index("rsync ")  # 测试不过不发
    assert "up -d --build --wait --wait-timeout 90" in text  # 只有 up -d 的话，容器起不来也返回成功
    assert "healthcheck:" in (CLOUD / "compose.yaml").read_text(encoding="utf-8")  # --wait 等的就是它


@pytest.mark.skipif(shutil.which("rsync") is None, reason="需要 rsync")
def test_book_sync_takes_txt_in_any_case(tmp_path: Path) -> None:
    filters = re.findall(r"--(?:in|ex)clude='[^']+'", "\n".join(code_lines(CLOUD / "sync_books.sh")))
    source, target = tmp_path / "source", tmp_path / "target"
    (source / "子目录").mkdir(parents=True)
    target.mkdir()
    for name in ("小写.txt", "大写.TXT", "混合.Txt", "子目录/书.tXt", "封面.jpg", "笔记.md"):
        (source / name).write_text("正文", encoding="utf-8")
    subprocess.run(["rsync", "-rt", *(f.replace("'", "") for f in filters), f"{source}/", f"{target}/"], check=True)
    assert sorted(p.name for p in target.rglob("*") if p.is_file()) == sorted(["小写.txt", "大写.TXT", "混合.Txt", "书.tXt"])


def test_book_sync_leaves_the_books_readable_by_the_container() -> None:
    lines = code_lines(CLOUD / "sync_books.sh")
    assert lines[-1] == 'ssh "$HOST" "sudo chmod -R a+rX /opt/glassreader/books"'  # rsync 之后，新文件沿用源文件权限


@pytest.mark.parametrize("script", ["push.sh", "sync_books.sh"])
def test_cloud_scripts_parse(script: str) -> None:
    subprocess.run(["bash", "-n", str(CLOUD / script)], check=True)
