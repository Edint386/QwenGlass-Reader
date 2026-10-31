"""眼镜页面模板和 SKILL.md 的静态检查。

模拟器 / 真机测出来的内核限制（docs/research.md §1、§6）在这里变成规则，改模板时不至于踩回去：
``data://update`` 一次写多个键时用 ``&amp;`` 分隔，属性里不能有 ``<``；
表达式只能有一个算术运算符，且左边不能是常量；根节点直接子元素 ≤ 64；页图地址里计数的顺序与服务端一致。
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from glassreader.library import BRIGHTNESS_LEVELS, COUNTER_NAMES
from glassreader.mcp_tools import JUMP_DESCRIPTION, OPEN_BOOK_DESCRIPTION, TOOL_NAMES
from glassreader.pager import FONT_SIZES, LINE_SPACINGS, TITLE_SIZE, WEIGHTS

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "pages" / "reader" / "template.xml"
DATA = ROOT / "pages" / "reader" / "data.json"
SKILL = ROOT / "skill" / "SKILL.md"
UI_SCHEMA = ROOT / "skill" / "ui_schema.json"
A = "{waft://dsl}"
#: 页面计数变量 → 服务端计数器（顺序即页图地址里的顺序）
PAGE_COUNTERS = dict(zip("abjkz", COUNTER_NAMES, strict=True))
#: 显示设置保存后写进图片地址的变量
PAGE_SAVED = ("uf", "uw", "ub", "us")


@pytest.fixture(scope="module")
def root() -> ET.Element:
    return ET.parse(TEMPLATE).getroot()


def all_attrs(root: ET.Element) -> list[tuple[ET.Element, str, str]]:
    return [(el, k, v) for el in root.iter() for k, v in el.attrib.items()]


def test_root_shape(root: ET.Element) -> None:
    assert root.tag == "view" and root.get("width") == "100%" and root.get("height") == "100%"
    assert root.get("flex-flow") == "row" and root.get("column-gap") == "0"  # 横排默认有 16 px 列间距
    assert len(list(root)) <= 64
    (body,) = root.findall("view")  # 可见元素都在这一层里，根节点要留给事件
    assert body.get("column-gap") == "0"
    meta = root.find("page-meta")
    assert meta is not None and meta.get("screen-awake") == "keep"
    assert root.find("tick") is None  # 不用 tick：真机上每次刷新都会让没加载完的图重新加载


def test_no_less_than_and_ampersand_only_between_data_keys(root: ET.Element) -> None:
    for el, k, v in all_attrs(root):
        assert "<" not in v, f"<{el.tag} {k}={v!r}>"
        assert "&" not in v or v.startswith("data://update?"), f"<{el.tag} {k}={v!r}>"


def writes(update: str) -> dict[str, str]:
    """一条 ``data://update`` 写的键和值。"""
    return dict(pair.split("=", 1) for pair in update.removeprefix("data://update?").split("&"))


def test_data_updates_are_well_formed(root: ET.Element) -> None:
    updates = [v for _, _, v in all_attrs(root) if v.startswith("data://")]
    assert updates
    for v in updates:
        assert re.fullmatch(r"data://update\?[a-z]+=[^&=]+(&[a-z]+=[^&=]+)*", v), v
        assert len(writes(v)) == v.count("&") + 1, f"同一个键写了两次：{v}"


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
    title, page = root.iter("image")
    counters = "-".join(f"{{{{d:{n}}}}}" for n in (*PAGE_COUNTERS, *PAGE_SAVED))
    assert page.get("src") == f"{{{{u}}}}/{counters}.png"
    assert (page.get("width"), page.get("height")) == ("478", "290")
    assert title.get("src") == "{{u}}/title.png"
    assert (int(title.get("width", 0)), int(title.get("height", 0))) == TITLE_SIZE


def test_each_counter_is_incremented_by_exactly_one_handler(root: ET.Element) -> None:
    for name in PAGE_COUNTERS:
        updates = [v for _, _, v in all_attrs(root) if v.startswith("data://")]
        hits = [v for v in updates if writes(v).get(name) == f"{{{{d:{name} + 1}}}}"]
        assert len(hits) == 1, name


def test_key_events(root: ET.Element) -> None:
    keys = {"on-key-enter", "on-key-next", "on-key-prev"}
    events = list(root.iter("key-event"))
    assert events
    for ev in events:
        assert set(ev.attrib) <= keys | {f"{A}if", f"{A}elif", f"{A}else"}, ev.attrib
        # 链是按键分开算的：缺了哪个键，这个键就跳过这一支落到下一支，所以每支都写全
        assert keys <= set(ev.attrib), ev.attrib
    # 一条链按状态分支，一个元素管三个键，省根节点名额
    assert f"{A}if" in events[0].attrib and f"{A}else" in events[-1].attrib
    assert all(f"{A}elif" in ev.attrib for ev in events[1:-1])
    # a:elif / a:else 必须紧跟同类元素的 a:if / a:elif
    prev = None
    for el in root:
        if f"{A}elif" in el.attrib or f"{A}else" in el.attrib:
            assert prev is not None and prev.tag == el.tag
            assert f"{A}if" in prev.attrib or f"{A}elif" in prev.attrib
        prev = el


def test_menu_highlight_boxes(root: ET.Element) -> None:
    boxes = [el for el in root.iter("view") if el.get("floating") == "true" and el.get("border-color") == "#FFFFFF"]
    rows = [*range(11, 16), *range(1, 6)]  # 显示设置菜单在前，主菜单在后
    assert [b.get(f"{A}if") for b in boxes] == [f"{{{{d:m == {i}}}}}" for i in rows]
    assert [b.get("top") for b in boxes] == [str(36 * i) for i in range(5)] * 2


def test_auto_turn_timer(root: ET.Element) -> None:
    events = list(root.iter("event"))
    assert len({e.get("match") for e in events}) <= 32  # 内核每页最多 32 个 match 条目
    # 前缀匹配：n 从 1 开头的数倒数到 100 或 1000 都是 d1，再减 1 变成 9 开头，只有 d9 匹配，就是一圈结束
    assert {e.get("match") for e in events} >= {"event://d1", "event://d9"}
    assert not {e.get("match") for e in events} & {f"event://d{digit}" for digit in range(2, 9)}
    laps = {v for _, _, v in all_attrs(root) if re.fullmatch(r"emit://event/d\d+\?n=\d+", v)}
    assert laps == {"emit://event/d183?n=183"}  # 183 到 100 正好 84 步，约一秒
    data = root.find("data")
    assert data is not None and data.get("s") == "7"  # 默认 7 秒一页
    by_match = {e.get("match"): e for e in events}
    assert by_match["event://up25?"].get(f"{A}if") == "{{d:s != 30}}"
    assert by_match["event://down25?"].get(f"{A}if") == "{{d:s != 0}}"  # 0 = 关闭


def test_menu_entry_stops_auto_turn_or_opens_settings(root: ET.Element) -> None:
    # 同一条链按 p 定去向：分开写的话先清 p，正在翻的时候也会进设置
    events = [e for e in root.iter("event") if e.get("match") == "event://auto?"]
    assert [(e.get(f"{A}if"), e.get("action")) for e in events] == [
        ("{{d:p > 0}}", "data://update?p=0&m=0"),
        (None, "data://update?m=25"),
    ]
    assert f"{A}else" in events[1].attrib


def test_menu_order(root: ET.Element) -> None:
    menu = next(el for el in root.iter("view") if el.get(f"{A}elif") == "{{d:m > 0}}")
    labels = [el for el in menu if el.tag == "label"]
    assert [el.get("text") for el in labels] == ["返回阅读", "停止自动翻页", "自动翻页", "下一章", "上一章", "显示设置"]
    sub = next(el for el in root.iter("view") if el.get(f"{A}elif") == "{{d:m > 10}}")
    assert [el.get("text") for el in sub if el.tag == "label"] == [
        "字号  {{d:fv}}", "粗细  {{d:wv}}/6", "亮度  {{d:bv}}%", "行距  {{d:sv}}%", "返回"]
    assert labels[1].get(f"{A}if") == "{{d:p > 0}}" and f"{A}else" in labels[2].attrib


def test_side_labels_use_native_font_sizes(root: ET.Element) -> None:
    # 文档 §34：原生字号只有这些档位，其余退回 14，排版会和预想的不一样
    sizes = {el.get("font-size") for el in root.iter("label") if el.get("font-size")}
    assert sizes <= {"10", "14", "16", "18", "20", "22", "24", "28", "32", "36", "40", "44", "58"}


def test_manual_turn_restarts_the_countdown_in_the_same_write(root: ET.Element) -> None:
    reading = next(e for e in root.iter("key-event") if e.get(f"{A}if") == "{{d:m == 0}}")
    assert reading.get("on-key-enter") == "data://update?m=1"
    assert reading.get("on-key-next") == "data://update?c={{d:p}}&a={{d:a + 1}}"
    assert reading.get("on-key-prev") == "data://update?c={{d:p}}&b={{d:b + 1}}"


def test_auto_turn_restarts_the_countdown_in_the_same_write(root: ET.Element) -> None:
    actions = [e.get("action") for e in root.iter("event") if e.get("match") == "event://turn?"]
    assert actions == ["data://update?c={{d:p}}&z={{d:z + 1}}"]


def test_brightness_mask_is_on_top_of_everything(root: ET.Element) -> None:
    (body,) = root.findall("view")
    mask = list(body)[-1]
    assert mask.get("bg-opa") == "{{d:op}}" and mask.get("bg-color") == "#000000"
    assert (mask.get("left"), mask.get("top"), mask.get("width"), mask.get("height")) == ("0", "0", "640", "290")
    assert mask.get(f"{A}if") is None
    assert all("animate-alpha" not in el.attrib for el in root.iter())


def test_no_idle_loop_or_side_dimming_left(root: ET.Element) -> None:
    data = root.find("data")
    assert data is not None and data.get("l") is None and data.get("y") is None
    assert not {"event://idle?", "event://wake?"} & {e.get("match") for e in root.iter("event")}


def test_settings_load_from_data_source_only_when_it_is_there(root: ET.Element) -> None:
    lifecycle = root.find("lifecycle-event")
    assert lifecycle is not None and lifecycle.get("on-load") == "emit://event/init?"
    (init,) = [e for e in root.iter("event") if e.get("match") == "event://init?"]
    assert writes(init.get("action", "")) == {
        "fv": "{{s.f}}", "wv": "{{s.w}}", "bv": "{{s.b}}", "sv": "{{s.l}}", "op": "{{s.o}}"}
    assert init.get(f"{A}if") == "{{s.f > 0}}"  # 大模型漏传 s 时留默认值


def test_setting_ranges_match_the_server(root: ET.Element) -> None:
    conditions = {e.get("match"): e.get(f"{A}if") for e in root.iter("event")}
    assert conditions["event://up21?"] == f"{{{{d:fv != {FONT_SIZES[-1]}}}}}"
    assert conditions["event://down21?"] == f"{{{{d:fv != {FONT_SIZES[0]}}}}}"
    assert conditions["event://up22?"] == f"{{{{d:wv != {len(WEIGHTS)}}}}}"
    assert conditions["event://down22?"] == "{{d:wv != 1}}"
    assert conditions["event://up23?"] == f"{{{{d:bv != {BRIGHTNESS_LEVELS[-1]}}}}}"
    assert conditions["event://down23?"] == f"{{{{d:bv != {BRIGHTNESS_LEVELS[0]}}}}}"
    assert conditions["event://up24?"] == f"{{{{d:sv != {LINE_SPACINGS[-1]}}}}}"
    assert conditions["event://down24?"] == f"{{{{d:sv != {LINE_SPACINGS[0]}}}}}"
    steps = {a for a in (b - c for b, c in zip(FONT_SIZES[1:], FONT_SIZES))}
    assert steps == {2}


def test_swiping_changes_the_value_and_the_image_address_together(root: ET.Element) -> None:
    actions = {e.get("match"): e.get("action", "") for e in root.iter("event")}
    for row, value, saved, step in ((21, "fv", "uf", 2), (22, "wv", "uw", 1), (24, "sv", "us", 15)):
        for event, sign in (("up", "+"), ("down", "-")):
            new = f"{{{{d:{value} {sign} {step}}}}}"
            assert writes(actions[f"event://{event}{row}?"]) == {value: new, saved: new}
    # 亮度只看遮罩，不用换页图
    assert writes(actions["event://up23?"]) == {"op": "{{d:op - 25}}", "bv": "{{d:bv + 10}}"}
    assert writes(actions["event://down23?"]) == {"op": "{{d:op + 25}}", "bv": "{{d:bv - 10}}"}


def key_branches(root: ET.Element) -> dict[str | None, ET.Element]:
    """按键链的分支：条件 → 元素，a:else 的条件是 None。"""
    return {e.get(f"{A}if") or e.get(f"{A}elif"): e for e in root.iter("key-event")}


def test_saving_brightness_and_returning_to_the_submenu(root: ET.Element) -> None:
    branches = key_branches(root)
    enter = {cond: e.get("on-key-enter") for cond, e in branches.items()}
    assert enter["{{d:m == 23}}"] == "data://update?ub={{d:bv}}&m=13"  # 只有亮度要在保存时写地址
    assert enter[None] == "data://update?m=11"  # 主菜单进二级菜单
    assert enter["{{d:m == 15}}"] == "data://update?m=5"  # 返回回到主菜单
    assert enter["{{d:m > 20}}"] == "data://update?m={{d:m - 10}}"  # 保存回到二级菜单同一行
    assert enter["{{d:m > 10}}"] == "data://update?m={{d:m + 10}}"  # 选中一项进去调
    order = list(branches)
    assert order.index("{{d:m == 23}}") < order.index("{{d:m > 20}}") < order.index("{{d:m > 10}}")
    assert order.index("{{d:m == 15}}") < order.index("{{d:m > 10}}")


def test_menus_wrap_around(root: ET.Element) -> None:
    branches = key_branches(root)
    assert branches["{{d:m == 1}}"].get("on-key-prev") == "data://update?m=5"
    assert branches[None].get("on-key-next") == "data://update?m=1"
    assert branches["{{d:m == 11}}"].get("on-key-prev") == "data://update?m=15"
    assert branches["{{d:m == 15}}"].get("on-key-next") == "data://update?m=11"


def test_adjust_panel_shows_the_value_and_how_to_save(root: ET.Element) -> None:
    panel = next(el for el in root.iter("view") if el.get(f"{A}elif") == "{{d:m > 20}}" and el.get("width") == "80")
    texts = [el.get("text") for el in panel]
    assert texts == ["字号", "粗细", "亮度", "行距", "{{d:fv}}", "{{d:wv}}/6", "{{d:bv}}%", "{{d:sv}}%", "单击保存"]
    assert not {"前滑", "后滑"} & set(texts)


def test_mock_data() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    assert set(data) == {"u", "t", "s"} and set(data["s"]) == {"f", "w", "b", "l", "o"}


def test_skill_md() -> None:
    text = SKILL.read_text(encoding="utf-8")
    front = re.match(r"^---\nname: (.+)\ndescription: (.+)\n---\n", text)
    assert front and front.group(1) == "眼镜看小说"
    assert "Use when" in front.group(2) and "Do NOT use" in front.group(2)
    assert '^^tQ8fCF.ui_page_render^^(templateName="reader", dataSource={"u": "<返回的 u>", "t": "<返回的 t>", "s": <返回的 s>})' in text
    assert '"s": 返回的 s' in text and "两个字段" not in text  # s 漏传了亮度和设置面板就不对
    used = set(re.findall(r"\^\^READER\.(\w+)\^\^", text))
    assert used == set(TOOL_NAMES)
    assert "templateName=" not in text.replace('templateName="reader"', "")  # 只用 reader 一个页面
    assert json.loads(UI_SCHEMA.read_text(encoding="utf-8")) == []


def test_tool_descriptions_say_how_to_open_the_page() -> None:
    for desc in (OPEN_BOOK_DESCRIPTION, JUMP_DESCRIPTION):
        assert 'templateName="reader"' in desc and '"u": 返回的 u' in desc and '"s": 返回的 s' in desc
