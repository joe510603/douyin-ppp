"""抖音的「要本人验证」不能被误判成「账号被限流」

真实经过（2026-11-11）
----------------------
用户反馈：「我用手机能留言，用另一台电脑也能留言，
所以你这边发消息是不是有问题」——**用户的直觉是对的**。

抓下来的真相：
- 发评论时抖音弹「接收短信验证码」对话框
  （「为确保是本人操作抖音账号，请输入当前手机号 xxx 收到的短信验证码」）
- 平台自己的裁决接口 ``passport/safe/query_decision/`` 返回：
      account_flow = "verify"      ← 要验证，**不是**限流
      verify_ways = [mobile_sms_verify, qr_code_verify]
- 而我们的代码：
  1. publish 响应的匹配串写成 ``/comment/publish/``（带斜杠），
     真实 URL 是 ``/comment/publish``（不带）→ **响应体从来没被读过**
  2. ``check_risk`` 不认识「接收短信验证码」这个弹窗 → 不知道要停下叫人
  3. 于是流程一路走下去，回查评论区当然找不到
     → 误判成「账号被限流（软风控）」→ 停手 + 冷却 120 分钟
     → 还通知用户「扫码没用」

账号从头到尾**没有被限流**。
"""
from __future__ import annotations

import json

from src.outreach.douyin_api import (
    LIMIT_TEXTS, RISK_TEXTS, VERIFY_TEXTS, Douyin, RiskControl,
)


# ============================================================
# 关键词分类
# ============================================================

def test_短信验证码弹窗算要验证():
    for msg in ("接收短信验证码", "为确保是本人操作抖音账号",
                "请输入验证码", "获取验证码", "短信验证码"):
        assert any(k in msg for k in VERIFY_TEXTS), msg


def test_限流关键词不算要验证():
    for msg in LIMIT_TEXTS:
        assert not any(k in msg for k in VERIFY_TEXTS), msg


def test_两组关键词不重叠():
    assert not (set(VERIFY_TEXTS) & set(LIMIT_TEXTS))


# ============================================================
# 裁决接口：account_flow=verify 是「要验证」，不是限流
# ============================================================

class _FakeResponse:
    def __init__(self, url: str, body: str):
        self.url = url
        self._body = body

    def text(self) -> str:
        return self._body


class _FakePage:
    def __init__(self):
        self.handlers = []

    def on(self, event, handler):
        if event == "response":
            self.handlers.append(handler)

    def fire(self, url: str, body: str) -> None:
        for h in list(self.handlers):
            h(_FakeResponse(url, body))


DECISION_BODY = json.dumps({
    "data": {
        "account_flow": "verify",
        "verify_scene": "comment",
        "verify_ways": [
            {"act_type": "22", "mobile": "187******52",
             "verify_way": "mobile_sms_verify"},
            {"nickname": "江枫唱晚", "verify_way": "qr_code_verify"},
        ],
    }
})


def test_裁决接口说verify就归为要验证():
    page = _FakePage()
    dy = Douyin(page)
    page.fire("https://www.douyin.com/passport/safe/query_decision/",
              DECISION_BODY)
    assert dy.verify_flow == "verify"
    assert "qr_code_verify" in dy.verify_ways
    assert "mobile_sms_verify" in dy.verify_ways


def test_裁决接口坏掉不影响():
    page = _FakePage()
    dy = Douyin(page)
    page.fire("https://www.douyin.com/passport/safe/query_decision/", "not json")
    assert dy.verify_flow == ""


def test_异常自带类型不用再猜():
    """RiskControl.kind 是唯一判据——调用方不再重新匹配关键词"""
    e = RiskControl("抖音要求验证本人操作", kind="verify")
    assert e.kind == "verify"
    assert RiskControl("普通风控").kind == "limit"


# ============================================================
# publish 响应的 URL 匹配
# ============================================================

def test_publish_url匹配不带斜杠():
    """真实 URL 是 /comment/publish（不带尾斜杠）——
    之前写成 /comment/publish/，**永远匹配不上**，响应体从来没被读过"""
    real = "https://www.douyin.com/aweme/v1/web/comment/publish?app_name=aweme"
    assert "/comment/publish" in real
    assert "/comment/publish/" not in real, "带斜杠的匹配串对不上真实 URL"


def test_comment_list_url匹配带斜杠():
    """comment/list 真实 URL 是带斜杠的，别搞反了"""
    real = "https://www-hj.douyin.com/aweme/v1/web/comment/list/?aweme_id=1"
    assert "comment/list" in real


# ============================================================
# 人工验证之后：先回查，没发才补发（重复评论是限流风险）
# ============================================================

class _FakeDy:
    """假装是 Douyin：第一次发撞验证，验证后按设定回话"""

    def __init__(self, found_after_verify: bool):
        self.found_after_verify = found_after_verify
        self.posts = 0
        self.checks = 0

    def post_comment(self, aweme_id, text):
        self.posts += 1
        if self.posts == 1:
            raise RiskControl("抖音要求验证本人操作", kind="verify")
        return {"ok": True, "status_code": 0, "comment_id": "cid-1"}

    def check_comment(self, aweme_id, text, nickname=None, scrolls=6):
        self.checks += 1
        return {"found": self.found_after_verify, "total": 3}


def _worker(tmp_path):
    from src.config import get_config
    from src.outreach.worker import OutreachWorker

    cfg = get_config()
    cfg.outreach.captcha_wait_minutes = 0.05
    return OutreachWorker(cfg, str(tmp_path / "t.db"), log=lambda m: None)


def test_验证后评论已发出去就不再补发(tmp_path):
    """已经发出去了 → 补发会变成重复评论（抖音的限流/封号风险）"""
    w = _worker(tmp_path)
    dy = _FakeDy(found_after_verify=True)
    w._bring_window_back = lambda: True
    w._notify_risk = lambda *a, **k: True
    w._wait_for_human = lambda reason: True

    res = w._post_with_human_help(dy, "123", "你好", {"id": 1})
    assert res["ok"] is True
    assert dy.posts == 1, "已经发出去就不该再发一次"
    assert dy.checks == 1, "应该回查过一次"


def test_验证后没发出去就补发一次(tmp_path):
    w = _worker(tmp_path)
    dy = _FakeDy(found_after_verify=False)
    w._bring_window_back = lambda: True
    w._notify_risk = lambda *a, **k: True
    w._wait_for_human = lambda reason: True

    res = w._post_with_human_help(dy, "123", "你好", {"id": 1})
    assert res["ok"] is True
    assert dy.posts == 2, "没发出去应该补发一次"


def test_限流类异常不当作要验证处理(tmp_path):
    """limit 类型必须往上抛，不能停下来等一个不会来的人"""
    w = _worker(tmp_path)
    called = []

    class _LimitDy:
        def post_comment(self, *a, **k):
            raise RiskControl("页面提示：操作频繁", kind="limit")

    try:
        w._post_with_human_help(_LimitDy(), "123", "你好", {"id": 1})
        assert False, "应该抛出去"
    except RiskControl as e:
        assert e.kind == "limit"
    assert called == []


# ============================================================
# 验证状态不能「粘」住（2026-11-11 实测踩到）
# ============================================================

def test_验证通过后要清掉状态():
    """不清的话，下一条线索刚开跑就被自己的旧状态拦死

    实测（2026-11-11 run #10）：第 1 条验证完发出去了，
    第 2 条在**搜索阶段**就被 verify_flow=verify 拦停，整轮提前结束。
    """
    from src.outreach.douyin_api import Douyin

    dy = Douyin(_FakePage())
    dy.verify_flow = "verify"
    dy.verify_flow_at = 9999999999.0
    dy.verify_ways = ["qr_code_verify"]
    dy.clear_verify()
    assert dy.verify_flow == ""
    assert dy.verify_ways == []
    assert dy._verify_pending() is False


def test_过期的裁决不再拦人():
    """超过 60 秒的裁决是过期状态，不该继续拦后面的动作"""
    import time as _t

    from src.outreach.douyin_api import Douyin

    dy = Douyin(_FakePage())
    dy.verify_flow = "verify"
    dy.verify_flow_at = _t.time() - 120
    assert dy._verify_pending() is False

    dy.verify_flow_at = _t.time()
    assert dy._verify_pending() is True


def test_人工验证后worker会清状态(tmp_path):
    """_post_with_human_help 在验证通过后必须调 clear_verify"""
    w = _worker(tmp_path)
    cleared = []

    class _Dy(_FakeDy):
        def clear_verify(self):
            cleared.append(1)

    dy = _Dy(found_after_verify=True)
    w._bring_window_back = lambda: True
    w._notify_risk = lambda *a, **k: True
    w._wait_for_human = lambda reason: True

    w._post_with_human_help(dy, "123", "你好", {"id": 1})
    assert cleared, "验证通过后必须清掉「要验证」状态"


# ============================================================
# kind 词汇统一
# ============================================================

def test_kind词汇统一():
    from src.outreach.douyin_api import (
        KIND_LIMIT, KIND_LOGIN, KIND_VERIFY, normalize_kind,
    )

    assert normalize_kind("captcha") == KIND_VERIFY
    assert normalize_kind("verify") == KIND_VERIFY
    assert normalize_kind("验证码") == KIND_VERIFY
    assert normalize_kind("login") == KIND_LOGIN
    assert normalize_kind("limit") == KIND_LIMIT
    assert normalize_kind("") == KIND_LIMIT
    assert normalize_kind("瞎写的") == KIND_LIMIT


# ============================================================
# 间隔可以当场调
# ============================================================

def test_本轮临时间隔覆盖配置(tmp_path):
    """网页上填的间隔要能覆盖配置里的"""
    w = _worker(tmp_path)
    w.cfg.outreach.min_delay = 45
    w.cfg.outreach.max_delay = 120
    w._delay_min = 10
    w._delay_max = 20
    assert w._delay_min == 10 and w._delay_max == 20

    # 没给覆盖时用配置里的
    w._delay_min = None
    w._delay_max = None
    assert w._delay_min is None and w._delay_max is None

