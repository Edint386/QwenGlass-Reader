"""眼镜页面模板和 SKILL.md 的静态检查。

模拟器 / 真机测出来的内核限制（docs/research.md §1、§6）在这里变成规则，改模板时不至于踩回去：
每次 ``data://update`` 只写一个键；属性里不能有 ``&``（内核不解码实体，裸写又不是良构 XML）；
表达式只能有一个算术运算符，且左边不能是常量；根节点直接子元素 ≤ 64；页图地址里六个计数的顺序与服务端一致。
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from glassreader.library import COUNTER_NAMES
from glassreader.mcp_tools import JUMP_DESCRIPTION, OPEN_BOOK_DESCRIPTION, TOOL_NAMES

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "pages" / "reader" / "template.xml"
DATA = ROOT / "pages" / "reader" / "data.json"
SKILL = ROOT / "skill" / "SKILL.md"
UI_SCHEMA = ROOT / "skill" / "ui_schema.json"
A = "{waft://dsl}"
#: 页面计数变量 → 服务端计数器（顺序即页图地址里的顺序）
PAGE_COUNTERS = dict(zip("abjkfg", COUNTER_NAMES, strict=True))


@pytest.fixture(scope="module")
def root() -> ET.Element:
    return ET.parse(TEMPLATE).getroot()


def all_attrs(root: ET.Element) -> list[tuple[ET.Element, str, str]]:
    return [(el, k, v) for el in root.iter() for k, v in el.attrib.items()]


def test_root_shape(root: ET.Element) -> None:
    assert root.tag == "view" and root.get("width") == "100%" and root.get("height") == "100%"
    assert root.get("flex-flow") == "row" and root.get("column-gap") == "0"  # 横排默认有 16 px 列间距
    assert len(list(root)) <= 64
    meta = root.find("page-meta")
    assert meta is not None and meta.get("screen-awake") == "keep"
    assert root.find("tick") is None  # 不用 tick：真机上每次刷新都会让没加载完的图重新加载


def test_no_ampersand_or_less_than_in_attributes(root: ET.Element) -> None:
    for el, k, v in all_attrs(root):
        assert "&" not in v and "<" not in v, f"<{el.tag} {k}={v!r}>"


def test_data_updates_write_one_key(root: ET.Element) -> None:
    updates = [v for _, _, v in all_attrs(root) if v.startswith("data://")]
    assert updates
    for v in updates:
        assert re.fullmatch(r"data://update\?[a-z]+=[^&=]+", v), v


def expressions(root: ET.Element) -> list[str]:
    return [m.group(1).strip() for _, _, v in all_attrs(root) for m in re.finditer(r"\{\{(.*?)\}\}", v)]


def test_expressions_have_at_most_one_arithmetic_operator(root: ET.Element) -> None:
    exprs = expressions(root)
    assert exprs
    for e in exprs:
        if e.startswith("f:"):
            continue
        ops = re.findall(r"(?<=\s)[-+*/](?=\s)", e)
        assert len(ops) <= 1, f"只算第一个运算符：{e!r}"
        if ops:
            assert not re.match(r"^-?\d", e), f"常量在左时算术结果为 0：{e!r}"


def test_initial_data(root: ET.Element) -> None:
    data = root.find("data")
    assert data is not None
    for name in (*PAGE_COUNTERS, "m"):
        assert data.get(name) == "0"


def test_image_src_matches_server_counter_order(root: ET.Element) -> None:
    (image,) = root.iter("image")
    counters = "-".join(f"{{{{d:{n}}}}}" for n in PAGE_COUNTERS)
    assert image.get("src") == f"{{{{u}}}}/{counters}.png"
    assert (image.get("width"), image.get("height")) == ("478", "290")


def test_each_counter_is_incremented_by_exactly_one_handler(root: ET.Element) -> None:
    for name in PAGE_COUNTERS:
        hits = [v for _, _, v in all_attrs(root) if v == f"data://update?{name}={{{{d:{name} + 1}}}}"]
        assert len(hits) == 1, name


def test_key_events(root: ET.Element) -> None:
    allowed = {"on-key-enter", "on-key-next", "on-key-prev", f"{A}if", f"{A}elif", f"{A}else"}
    events = list(root.iter("key-event"))
    assert events
    for ev in events:
        assert set(ev.attrib) <= allowed, ev.attrib
        assert len([k for k in ev.attrib if k.startswith("on-key-")]) == 1  # 每条链只管一种按键
    # a:elif / a:else 必须紧跟同一种按键的 a:if / a:elif
    prev = None
    for el in root:
        if f"{A}elif" in el.attrib or f"{A}else" in el.attrib:
            assert prev is not None and prev.tag == el.tag
            assert f"{A}if" in prev.attrib or f"{A}elif" in prev.attrib
            if el.tag == "key-event":
                assert [k for k in el.attrib if k.startswith("on-")] == [k for k in prev.attrib if k.startswith("on-")]
        prev = el


def test_menu_highlight_boxes(root: ET.Element) -> None:
    boxes = [el for el in root.iter("view") if el.get("floating") == "true" and el.get("border-color") == "#FFFFFF"]
    assert [b.get(f"{A}if") for b in boxes] == [f"{{{{d:m == {i}}}}}" for i in range(1, 6)]
    assert [b.get("top") for b in boxes] == [str(36 * i) for i in range(5)]


def test_mock_data() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    assert set(data) == {"u", "t"}


def test_skill_md() -> None:
    text = SKILL.read_text(encoding="utf-8")
    front = re.match(r"^---\nname: (.+)\ndescription: (.+)\n---\n", text)
    assert front and front.group(1) == "眼镜看小说"
    assert "Use when" in front.group(2) and "Do NOT use" in front.group(2)
    assert '^^tQ8fCF.ui_page_render^^(templateName="reader", dataSource={"u": "<返回的 u>", "t": "<返回的 t>"})' in text
    used = set(re.findall(r"\^\^READER\.(\w+)\^\^", text))
    assert used == set(TOOL_NAMES)
    assert "templateName=" not in text.replace('templateName="reader"', "")  # 只用 reader 一个页面
    assert json.loads(UI_SCHEMA.read_text(encoding="utf-8")) == []


def test_tool_descriptions_say_how_to_open_the_page() -> None:
    for desc in (OPEN_BOOK_DESCRIPTION, JUMP_DESCRIPTION):
        assert 'templateName="reader"' in desc and '"u": 返回的 u' in desc
