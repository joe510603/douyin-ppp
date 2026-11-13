"""抖音主页「个人简介」能不能拿到、拿到的是不是原文（2026-11-13）

用户要求：搜主页时顺便看简介，有些客户把联系方式写在简介里。

实测踩过的坑（都写进代码注释了，这里钉住行为）：
  · 主页 DOM 里那份简介被平台**截断 + 掩码**
    （"…171专业000施工..."，末尾省略号）→ 号码读不出来
  · 作品接口 ``aweme/post`` 的 ``author`` 里**没有** signature 字段
    （signature_extra / profile_signature_components 都是 null）
  · ``RENDER_DATA`` 是**静态缓存**（每次都是同一个人），不能用
  · 新接口 ``/aweme/v1/web/user/profile/other/`` 要 a_bogus 签名，
    没签直接回 ``{"status_code":0,"status_msg":"blocked"}``
  · ✅ 老接口 ``https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid=…``
    **不要签名**，返回**完整原文**（实测 25 条连着调 0 失败）
"""
from __future__ import annotations

import json

from src.outreach.douyin_api import Douyin


# ============================================================
# 假浏览器
# ============================================================

class _FakeResp:
    def __init__(self, body: str):
        self._body = body

    def text(self) -> str:
        return self._body


class _FakeRequest:
    def __init__(self, body: str = "", exc: Exception | None = None):
        self._body = body
        self._exc = exc
        self.calls: list[str] = []

    def get(self, url: str, timeout: int | None = None):
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._body)


class _FakePage:
    def __init__(self, body: str = "", exc: Exception | None = None,
                 dom: str = ""):
        self.request = _FakeRequest(body, exc)
        self._dom = dom

    def on(self, *a, **k):
        pass

    def evaluate(self, script: str):
        return self._dom


def _douyin(page, works=None) -> Douyin:
    dy = Douyin(page, log=lambda m: None)

    def _fake_fetch(sec_uid, after_open=None):
        if after_open:
            after_open()
        return list(works or [])

    dy._fetch_works_page = _fake_fetch  # type: ignore[assignment]
    dy.ensure_login = lambda: None  # type: ignore[assignment]
    return dy


def _用户信息(**kw) -> str:
    user = {
        "sec_uid": "MS4wLjABAAAAxxxx",
        "nickname": "永华：",
        "unique_id": "yonghua38816",
        "signature": "隆广顺大量招收学员，加盟电话13281975716",
        "aweme_count": 3,
    }
    user.update(kw)
    return json.dumps({"status_code": 0, "user_info": user})


# ------------------------------------------------------------ fetch_profile_info

def test_老接口能拿到完整简介():
    dy = _douyin(_FakePage(body=_用户信息()))
    got = dy.fetch_profile_info("MS4wLjABAAAAxxxx")
    assert got and got["signature"].endswith("加盟电话13281975716")
    assert got["nickname"] == "永华："


def test_老接口不需要签名参数():
    """请求里只能有 sec_uid，别去碰那些要 a_bogus 的新接口"""
    page = _FakePage(body=_用户信息())
    dy = _douyin(page)
    dy.fetch_profile_info("MS4wLjABAAAAxxxx")
    assert page.request.calls
    assert "sec_uid=MS4wLjABAAAAxxxx" in page.request.calls[0]
    assert "a_bogus" not in page.request.calls[0]


def test_接口挂了不能抛异常():
    """简介只是补充信息，拿不到不该让整条线索的触达失败"""
    dy = _douyin(_FakePage(exc=RuntimeError("network down")))
    assert dy.fetch_profile_info("MS4wLjABAAAAxxxx") is None


def test_返回空user时是None():
    dy = _douyin(_FakePage(body=json.dumps({"status_code": 2, "user_info": {}})))
    assert dy.fetch_profile_info("MS4wLjABAAAAxxxx") is None


def test_空sec_uid直接返回None():
    dy = _douyin(_FakePage(body=_用户信息()))
    assert dy.fetch_profile_info("") is None


# ------------------------------------------------------------ resolve_by_sec_uid

def test_已知sec_uid的快路径也能拿到简介():
    """这条快路径**不搜索**，所以简介得单独去取"""
    page = _FakePage(body=_用户信息())
    dy = _douyin(page, works=[{"aweme_id": "a1", "author_nickname": "永华："}])
    got = dy.resolve_by_sec_uid("MS4wLjABAAAAxxxx")
    assert got and got["signature"].endswith("加盟电话13281975716")
    assert got["nickname"] == "永华："


def test_老接口挂了退到主页DOM():
    page = _FakePage(exc=RuntimeError("boom"), dom="全国二手钢结构…171专业000施工")
    dy = _douyin(page, works=[{"aweme_id": "a1", "author_nickname": "老董"}])
    got = dy.resolve_by_sec_uid("MS4wLjABAAAAxxxx")
    assert got and "全国二手钢结构" in got["signature"]
    # DOM 里那份末尾有个「更多」按钮的文案，要去掉
    assert "更多" not in got["signature"]


def test_两处都没有简介时是空字符串():
    page = _FakePage(body=json.dumps({"user_info": {}}), dom="")
    dy = _douyin(page, works=[])
    got = dy.resolve_by_sec_uid("MS4wLjABAAAAxxxx")
    assert got and got["signature"] == ""
