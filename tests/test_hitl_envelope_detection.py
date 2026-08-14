"""检测阻塞式 HITL 工具结果的唯一正确方式:先解析,再查键。

四处生产代码曾各自写 ``text.strip().startswith('{"__hitl__":')``。它今天能用,
靠的是三个生产者恰好都把 ``__hitl__`` 写在第一个键、且序列化时不带缩进 ——
都是巧合,不是契约。其中两个生产者返回的是 dict,序列化发生在别处。

一旦失配,审批卡片就不会出现,而用户看到的是一个悬在那里的 approval token。
没有任何东西会报错。所以这些用例按「同一份载荷的多种合法序列化」来测,而不是
只测生产代码今天恰好产出的那一种。
"""
import json

import pytest

from packages.core.constants.hitl_envelope import (
    HitlEnvelopeKey,
    is_hitl_envelope,
    parse_hitl_envelope,
)

PAYLOAD = {
    "__hitl__": True,
    "error": "approval_required",
    "approval_token": "hitl-calendar-delete",
    "hitl": {"id": "hitl-calendar-delete", "prompt": "Delete it?"},
}


def test_envelope_keys_are_the_frontend_contract():
    assert HitlEnvelopeKey.values() == [
        "__hitl__", "error", "approval_token", "hitl", "operation",
    ]
    # 会被当作 dict 下标和 f-string 用,必须渲染成 wire key。
    assert str(HitlEnvelopeKey.MARKER) == "__hitl__"
    assert f"{HitlEnvelopeKey.MARKER}" == "__hitl__"


@pytest.mark.parametrize(
    "label,serialized",
    [
        ("紧凑", json.dumps(PAYLOAD)),
        # 旧的 startswith 判断在这三种下全部失配或依赖巧合:
        ("缩进2", json.dumps(PAYLOAD, indent=2)),
        ("缩进4", json.dumps(PAYLOAD, indent=4)),
        ("宽分隔符", json.dumps(PAYLOAD, separators=(", ", ": "))),
        ("sort_keys", json.dumps(PAYLOAD, sort_keys=True)),
        ("前后空白", "  \n" + json.dumps(PAYLOAD) + "\n  "),
        ("非 ASCII", json.dumps({**PAYLOAD, "note": "删除吗?"}, ensure_ascii=False)),
    ],
)
def test_every_legal_serialization_is_detected(label, serialized):
    parsed = parse_hitl_envelope(serialized)
    assert parsed is not None, f"{label} 形式的 HITL 载荷没被识别 —— 审批卡片会静默消失"
    assert parsed["approval_token"] == "hitl-calendar-delete"


def test_key_order_does_not_matter():
    """两个生产者返回 dict 由别处序列化 —— 键序不该是检测的前提。"""
    reordered = {
        "approval_token": "x", "error": "approval_required",
        "hitl": {}, "__hitl__": True,
    }
    assert parse_hitl_envelope(json.dumps(reordered)) is not None


def test_already_parsed_dict_is_accepted():
    assert parse_hitl_envelope(PAYLOAD) is PAYLOAD


@pytest.mark.parametrize("value", [
    None, "", "   ", "not json", "[]", "42",
    '{"ok": true}',                       # 普通工具结果
    '{"__hitl__": false}',                # 显式否
    '{"__hitl__": "yes"}',                # 真值但不是 True
    '{"__hitl__": null}',
])
def test_non_envelopes_return_none(value):
    assert parse_hitl_envelope(value) is None


def test_marker_must_be_true_not_merely_truthy():
    """``{"__hitl__": "no"}`` 不是在要人 —— 用 is True,不用真值判断。"""
    assert is_hitl_envelope({"__hitl__": True}) is True
    assert is_hitl_envelope({"__hitl__": "no"}) is False
    assert is_hitl_envelope({"__hitl__": 1}) is False


def test_no_production_code_detects_hitl_by_string_prefix():
    """守一类,不守一处。

    这个前缀判断原本在四个地方各写了一遍(streams.py 两处、chat_service.py
    两处),PR #475 又照抄了第五、第六处。修掉六处不解决问题 —— 下一个人
    照样会写。按仓库扫。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in (*root.glob("packages/**/*.py"), *root.glob("apps/api/**/*.py")):
        if path.name == "hitl_envelope.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*#", line):
                continue
            if "startswith" in line and "__hitl__" in line:
                offenders.append(f"{path.relative_to(root)}:{lineno}")
    assert not offenders, (
        "这些地方在用字符串前缀判断 HITL 载荷,改用 parse_hitl_envelope():\n  "
        + "\n  ".join(offenders)
    )
