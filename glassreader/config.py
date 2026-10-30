"""glassreader 的运行配置。

来源优先级：命令行参数 > 环境变量 ``READER_<字段名大写>`` > 默认值（与 glasscast 相同）。

- ``public_url``：眼镜（经手机）下载页图、平台调用 MCP 用的公网根地址，可以带路径前缀（反代剥掉前缀后转发，
  例如 ``https://cast.example.com/reader``）。为空时用本机地址，只够本地模拟器联调。
- ``token``：MCP 的 Bearer Token。``key``：页图地址里的随机段 ``/r/<key>/…``——眼镜发不了请求头，
  页图只能靠路径鉴权（同 glasscast）。两者为空时启动时自动生成；平台插件要填固定的 token，所以正式用要写进 .env。
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_PORT",
    "ENV_PREFIX",
    "ENV_VARS",
    "ConfigError",
    "Settings",
    "generate_key",
    "generate_token",
    "load_settings",
    "normalize_public_url",
]

ENV_PREFIX = "READER_"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780
DEFAULT_BOOKS_DIR = Path("~/Books")
DEFAULT_STATE_FILE = Path("~/Library/Application Support/glassreader/progress.json")


class ConfigError(ValueError):
    """配置不合法（消息是给人看的中文，可能多行）。"""


def normalize_public_url(url: str) -> str:
    """校验并规范化对外根地址：去掉末尾 ``/``；只允许 http/https；不能带 query、fragment、用户名密码。"""
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"public_url 必须是 http(s)://主机[:端口][/路径] 形式: {url!r}")
    if parts.query or parts.fragment:
        raise ValueError(f"public_url 不能带 ? 查询参数或 # 片段: {url!r}")
    if parts.username or parts.password:
        raise ValueError("public_url 里不要放用户名/密码")
    try:
        parts.port  # noqa: B018 - 触发端口合法性检查
    except ValueError:
        raise ValueError(f"public_url 的端口不合法: {url!r}") from None
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def generate_token() -> str:
    """随机 MCP Bearer Token（43 个 URL 安全字符，256 bit）。"""
    return secrets.token_urlsafe(32)


def generate_key() -> str:
    """随机页图路径 key：12 字节 → 16 个 URL 安全字符。"""
    return secrets.token_urlsafe(12)


def _parse_bool(text: str) -> bool:
    value = text.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"应为 1/0、true/false: {text!r}")


def _parse_port(text: str | int) -> int:
    port = int(text)
    if not 0 < port < 65536:
        raise ValueError(f"端口应在 1～65535: {text!r}")
    return port


def _parse_key(text: str) -> str:
    value = text.strip()
    if not value or not all(c.isalnum() or c in "-_" for c in value):
        raise ValueError("key 只能用字母、数字、- 和 _（它是网址路径的一段）")
    return value


_PARSERS: dict[str, Any] = {
    "public_url": normalize_public_url,
    "host": str.strip,
    "port": _parse_port,
    "token": str.strip,
    "key": _parse_key,
    "books_dir": lambda t: Path(str(t).strip()).expanduser(),
    "state_file": lambda t: Path(str(t).strip()).expanduser(),
    "swap_swipe": _parse_bool,
    "allowed_hosts": lambda t: tuple(h.strip().lower() for h in str(t).split(",") if h.strip()),
    "dns_rebinding_protection": _parse_bool,
}
#: 字段 → 环境变量
ENV_VARS: dict[str, str] = {name: ENV_PREFIX + name.upper() for name in _PARSERS}


@dataclass(frozen=True)
class Settings:
    public_url: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str = field(default="", repr=False)
    key: str = field(default="", repr=False)
    books_dir: Path = DEFAULT_BOOKS_DIR.expanduser()
    state_file: Path = DEFAULT_STATE_FILE.expanduser()
    #: 真机前滑 / 后滑方向和预期相反时对调（research §7 P2），不用改页面
    swap_swipe: bool = False
    #: 除 public_url 的主机和本机回环外，MCP 额外放行的 Host 头（DNS 重绑定保护）
    allowed_hosts: tuple[str, ...] = ()
    dns_rebinding_protection: bool = True
    token_generated: bool = False
    key_generated: bool = False

    @property
    def local_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if host in ("0.0.0.0", "[::]"):
            host = "127.0.0.1"
        return f"http://{host}:{self.port}"

    @property
    def base_url(self) -> str:
        """页图和 MCP 的对外根地址：public_url，没配就是本机地址。"""
        return self.public_url or self.local_url

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    def page_prefix(self, book_id: str, session: str) -> str:
        """页面 dataSource 里的 ``u``：页面在后面拼 ``/<六个计数>.png``。"""
        return f"{self.base_url}/r/{self.key}/{book_id}/{session}"

    def mcp_allowed_hosts(self) -> list[str]:
        """MCP DNS 重绑定保护放行的 Host：public_url 的主机（带不带端口都行）、本机回环、额外配置的主机。"""
        hosts: list[str] = []

        def add(host: str) -> None:
            host = host.strip().lower()
            for candidate in ([host] if ":" in host.split("]")[-1] else [host, f"{host}:*"]):
                if candidate and candidate not in hosts:
                    hosts.append(candidate)

        if self.public_url:
            parts = urlsplit(self.public_url)
            add(parts.netloc.lower())
            if parts.port is not None:
                add(parts.hostname or "")
        for host in ("127.0.0.1", "localhost", "[::1]", *self.allowed_hosts):
            add(host)
        return hosts

    def mcp_allowed_origins(self) -> list[str]:
        origins = []
        for host in self.mcp_allowed_hosts():
            for scheme in ("http", "https"):
                origin = f"{scheme}://{host}"
                if origin not in origins:
                    origins.append(origin)
        return origins

    def warnings(self) -> list[str]:
        out = []
        if not self.public_url:
            out.append(f"没有设置 public_url（{ENV_VARS['public_url']}）：页图地址是本机地址，只能在本机模拟器里用")
        elif self.public_url.startswith("http://"):
            out.append("public_url 不是 https：平台调用 MCP、手机下载页图都要公网 HTTPS")
        if not self.books_dir.is_dir():
            out.append(f"书库目录不存在：{self.books_dir}（{ENV_VARS['books_dir']} / --books）")
        return out


def load_settings(
    cli: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    generate_secrets: bool = True,
) -> Settings:
    """按“命令行 > 环境变量 > 默认值”合并。``cli`` 里值为 None 表示没给。所有错误汇总成一个 :class:`ConfigError`。"""
    cli = dict(cli or {})
    env = env or {}
    unknown = sorted(set(cli) - set(_PARSERS))
    if unknown:
        raise TypeError(f"未知配置项: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    errors: list[str] = []
    for name, var in ENV_VARS.items():
        raw = env.get(var)
        if raw is not None and raw.strip():
            try:
                values[name] = _PARSERS[name](raw)
            except ValueError as exc:
                errors.append(f"环境变量 {var}: {exc}")
    for name, value in cli.items():
        if value is None:
            continue
        try:
            if isinstance(value, str) or name in ("port", "books_dir", "state_file"):
                value = _PARSERS[name](value if not isinstance(value, Path) else str(value))
            elif name == "allowed_hosts":
                value = tuple(dict.fromkeys((*values.get("allowed_hosts", ()), *(str(h).lower() for h in value))))
        except ValueError as exc:
            errors.append(f"参数 {name}: {exc}")
            continue
        values[name] = value
    if errors:
        raise ConfigError("\n".join(errors))
    settings = Settings(**values)
    if generate_secrets:
        if not settings.token:
            settings = replace(settings, token=generate_token(), token_generated=True)
        if not settings.key:
            settings = replace(settings, key=generate_key(), key_generated=True)
    return settings


def settings_field_names() -> list[str]:
    return [f.name for f in fields(Settings)]
