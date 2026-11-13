"""抖音搜索：认人只能靠抖音号 + 搜索结果不稳定要重试

背景（2026-11-12 实测，用户反馈「它说搜不到，我用手机试了试能搜到」）
--------------------------------------------------------------------
线索 `999999106 / 测试昵称C…` 在生产跑里被判成「这个人搜不到」，
但 18:50 用探针跑，**同一个号又搜到了**，sec_uid 一模一样。

三个问题叠在一起：

1. **昵称是抖音截断的**（"测试昵称C…"，3 个字符）
   → 拿它当搜索词等于搜「测试昵称C…」这个带省略号的词，**永远搜不到**。

2. **昵称不唯一**：抖音的截断昵称会被不同的人共用。
   实测搜「测试昵称C…」返回了一个 unique_id=999999112 的人，
   而我们要找的是 unique_id=999999106 —— **认错人了**。
   认错人 = 拿客户的号给陌生人发广告，比跳过还糟。

3. **抖音的搜索结果是概率性的**：同一个号，有时返回、有时给空。
   漏掉一条线索就是少一次转化机会。

修法：
- 搜之前把昵称末尾的省略号去掉（扩大搜索池）
- 昵称只用来「扩大搜索池」，**认人必须靠抖音号对得上**
- 两条路都没找到 → 用抖音号再搜一次（抖音的结果不稳定）
"""
from __future__ import annotations

from src.outreach.douyin_api import Douyin


def _douyin() -> Douyin:
    """只要用到不碰 page 的方法，page 传个占位符就行"""
    return Douyin(page=None, log=lambda m: None)


# ============================================================
# 昵称清洗：去掉抖音的截断省略号
# ============================================================

def test_昵称末尾的省略号要去掉():
    """抖音给的昵称是截断的（"测试昵称C…"）—— 带省略号搜不到任何人"""
    assert Douyin._clean_nickname("测试昵称C…") == "测试昵称C"
    assert Douyin._clean_nickname("测试昵称C...") == "测试昵称C"
    assert Douyin._clean_nickname("测试昵称C… ") == "测试昵称C"


def test_完整昵称不动():
    assert Douyin._clean_nickname("张伟") == "张伟"
    assert Douyin._clean_nickname("测试昵称D！不得了不得了……") == "测试昵称D！不得了不得了"
    assert Douyin._clean_nickname("") == ""
    assert Douyin._clean_nickname(None) == ""


def test_昵称中间的省略号不动():
    """只去**末尾**的——中间的省略号是昵称本来的一部分"""
    assert Douyin._clean_nickname("候…鸟") == "候…鸟"


# ============================================================
# 认人：昵称搜索的结果必须用抖音号确认
# ============================================================

def _假装搜索结果(dy: Douyin, users: list[dict]):
    """把 _search 打桩，让 _resolve_once 直接拿到这些候选"""
    dy._search = lambda q, **kw: [{"user_list": users}]
    dy.ensure_login = lambda: None
    dy.check_risk = lambda: None
    dy._probe_search_health = lambda *a, **k: True
    dy._body_text = lambda: ""


def test_昵称搜到的不是那个人就不认():
    """认错人 = 给陌生人发广告，比跳过还糟"""
    dy = _douyin()
    # 候选里有昵称一样、但抖音号是另一个人的
    _假装搜索结果(dy, [{
        "sec_uid": "MS4wLjABAAAA_others",
        "unique_id": "999999112",
        "nickname": "测试昵称C…",
    }])
    info = dy._resolve_once("测试昵称C", by_nickname=True, want_dy="999999106")
    assert info is None, "抖音号对不上就不能认"


def test_昵称搜到的抖音号对得上就认():
    """昵称只是搜索词，认人靠抖音号 —— 对得上就该认出来"""
    dy = _douyin()
    _假装搜索结果(dy, [
        {"sec_uid": "MS4wLjABAAAA_other", "unique_id": "999999112",
         "nickname": "测试昵称C…"},
        {"sec_uid": "MS4wLjABAAAA_right", "unique_id": "999999106",
         "nickname": "测试昵称C…"},
    ])
    info = dy._resolve_once("测试昵称C", by_nickname=True, want_dy="999999106")
    assert info is not None
    assert info["sec_uid"] == "MS4wLjABAAAA_right"
    assert info["unique_id"] == "999999106"


def test_短号也要能对上():
    """抖音有 unique_id（自定义号）和 short_id（自动号），两个都得认"""
    dy = _douyin()
    _假装搜索结果(dy, [{
        "sec_uid": "MS4wLjABAAAA_x",
        "short_id": "999999106",
        "nickname": "测试昵称C…",
    }])
    info = dy._resolve_once("测试昵称C", by_nickname=True, want_dy="999999106")
    assert info is not None
    assert info["sec_uid"] == "MS4wLjABAAAA_x"


def test_没有抖音号可对时才认昵称():
    """纯昵称线索（表里本来就没有抖音号）只能认昵称"""
    dy = _douyin()
    _假装搜索结果(dy, [{
        "sec_uid": "MS4wLjABAAAA_x", "unique_id": "111",
        "nickname": "测试昵称C…",
    }])
    info = dy._resolve_once("测试昵称C…", by_nickname=True)
    assert info is not None


# ============================================================
# 重试：抖音的搜索结果不稳定
# ============================================================

def test_抖音号搜索失败不再重试(monkeypatch):
    """砍掉第 3 次搜索（2026-11-13 用户拍板）

    第 3 次就是把第 1 次原样重做一遍，只为对付抖音结果的概率性。
    实测当天触发 2 次、0 次救回过人，每次赔上 20~40 秒。
    搜不到的人会被跳过（终态），真要再给机会，人工在飞书里放回队列。

    需要那个保险时：配置 search_retry=True（见 test_throughput.py）。
    """
    dy = _douyin()
    calls: list[str] = []

    def fake_resolve_once(unique_id, by_nickname=False, want_dy=""):
        calls.append(f"{unique_id}|{by_nickname}")
        return None

    dy._resolve_once = fake_resolve_once
    assert dy.resolve("999999106", nickname="测试昵称C…") is None
    assert calls.count("999999106|False") == 1, f"不该搜第三次：{calls}"
    assert any(c.endswith("|True") for c in calls), "中间要走一次昵称搜索"


def test_搜到了就不重试(monkeypatch):
    """一次就中就别折腾了"""
    dy = _douyin()
    calls: list[str] = []

    def fake_resolve_once(unique_id, by_nickname=False, want_dy=""):
        calls.append(unique_id)
        return {"sec_uid": "x", "unique_id": unique_id, "nickname": "n"}

    dy._resolve_once = fake_resolve_once
    dy.resolve("999999106", nickname="测试昵称C…")
    assert calls == ["999999106"]


def test_昵称是空的就不折腾():
    """没有昵称可退守，直接放弃"""
    dy = _douyin()
    calls: list[str] = []

    def fake_resolve_once(unique_id, by_nickname=False, want_dy=""):
        calls.append(unique_id)
        return None

    dy._resolve_once = fake_resolve_once
    assert dy.resolve("999999106", nickname="") is None
    assert calls == ["999999106"]
