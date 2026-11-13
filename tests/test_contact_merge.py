"""联系方式的「按类型合并」（2026-11-13 主页简介补联系方式时加的）

背景
----
联系方式现在有**两个来源**：
  1. 弹幕原文（客户在直播间自己说的）
  2. 抖音主页简介（有人习惯把电话写在简介里）

两个来源会先后写进同一条线索。以前弹幕链路是**整体覆盖**，
所以「简介里的微信」会被「弹幕里的电话」整条挤掉。

合并规则钉死在这里：
  · 一边空 → 原样返回另一边（不动老数据的存法）
  · 同类型 → 取新的（弹幕链路）／保留旧的（简介链路）
  · 不同类型 → 追加（两个都留着）
"""
from __future__ import annotations

from src.intelligence.contact_extractor import merge_contact, split_contact


def test_新的没值就保留旧的():
    assert merge_contact("phone", "13812345678", "", "") == ("phone", "13812345678")


def test_旧的是空就直接用新的():
    assert merge_contact("", "", "phone", "13812345678") == ("phone", "13812345678")


def test_空值不改动原样返回():
    """老数据存的是裸值，不能被改写成「电话 xxx」"""
    assert merge_contact("phone", "13812345678", "", "")[1] == "13812345678"
    assert merge_contact("", "", "phone", "13812345678")[1] == "13812345678"


def test_同类型取新的():
    t, v = merge_contact("phone", "13812345678", "phone", "13999998888")
    assert t == "phone"
    assert "13999998888" in v
    assert "13812345678" not in v


def test_同类型可以保留旧的():
    """简介链路用：弹幕是客户当场说的，比简介里那个更贴近这次问价"""
    t, v = merge_contact("phone", "13812345678", "phone", "13999998888",
                         prefer_new_same_type=False)
    assert t == "phone"
    assert "13812345678" in v
    assert "13999998888" not in v


def test_不同类型要追加():
    """弹幕给了电话、简介给了微信 —— 两个都要留着"""
    t, v = merge_contact("phone", "13812345678", "wechat", "abc123")
    assert t == "phone+wechat"
    assert "13812345678" in v
    assert "abc123" in v


def test_三种类型都在():
    t, v = merge_contact("phone+wechat", "电话 13812345678 / 微信 abc",
                         "qq", "12345")
    assert t == "phone+wechat+qq"
    assert "12345" in v


def test_合并后的排版固定():
    """类型顺序固定成 电话 / 微信 / QQ，跟 ContactInfo.value 一致"""
    _t, v = merge_contact("wechat", "abc123", "phone", "13812345678")
    assert v == "电话 13812345678 / 微信 abc123"


def test_认不出类型就整体取新的():
    """一边没有类型信息 → 没法治，按新的算"""
    assert merge_contact("", "", "phone", "138") == ("phone", "138")
    assert merge_contact("phone", "138", "", "") == ("phone", "138")


# ------------------------------------------------------------ split_contact

def test_split能认回带标签的值():
    got = split_contact("phone+wechat", "电话 13812345678 / 微信 abc")
    assert got == {"phone": "13812345678", "wechat": "abc"}


def test_split兼容裸值():
    assert split_contact("phone", "13812345678") == {"phone": "13812345678"}


def test_split空值返回空():
    assert split_contact("", "") == {}
