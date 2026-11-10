"""触达撞验证码的处理 —— 叫人 + 停下等 + 恢复

背景
----
抖音会弹验证码（短信/拖拽图形），这东西**只能人工过**。
原来撞上验证码只是记条日志然后结束本轮，
人不在电脑前就等于这轮白跑，而且完全不知道发生了什么。

现在是：
1. 飞书私聊通知你（手机也有推送）
2. 浏览器窗口从屏幕外挪回屏幕中间（不然你看不见验证码）
3. 触达**暂停**等你在网页上点「我验证好了，继续」
4. 超时（默认 15 分钟）没人管就结束本轮

另一个容易混的点：**验证码 ≠ 账号被限流**。
限流（操作频繁/环境异常）是平台不给过了，硬刚只会把号搞废，必须直接停；
验证码是平台要你证明你是人，是可以等人来处理的。
"""

import threading
import time

import pytest

from src.config import get_config
from src.outreach.worker import OutreachWorker


# ============================================================
# 区分：验证码 vs 限流
# ============================================================

def test_验证码被识别出来():
    for msg in ("跳转到安全验证页", "出现验证码控件", "页面提示：请完成安全验证"):
        assert OutreachWorker._is_captcha(msg), msg


def test_限流不算验证码():
    """限流要直接停，等人来处理没有意义"""
    for msg in ("页面提示：操作频繁", "页面提示：账号存在异常",
                "页面提示：环境异常", "页面提示：访问频繁"):
        assert not OutreachWorker._is_captcha(msg), msg


# ============================================================
# 暂停 / 恢复
# ============================================================

def _worker(tmp_path, **cfg_over):
    cfg = get_config()
    # 测试里等 0.2 分钟（12 秒）足够
    cfg.outreach.captcha_wait_minutes = 0.2
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    ev = threading.Event()
    w = OutreachWorker(cfg, str(tmp_path / "t.db"), log=lambda m: None,
                       resume_event=ev)
    return w, ev


def test_等待人工验证时收到等待事件(tmp_path):
    w, ev = _worker(tmp_path)
    events = []
    w.on_event = lambda e: events.append(e)

    # 后台线程里等，主线程模拟用户点「继续」
    result = {}

    def _waiter():
        result["ok"] = w._wait_for_human("出现验证码控件")

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.3)
    assert any(e.get("type") == "captcha_wait" for e in events), events
    assert w.waiting_human is True
    ev.set()
    t.join(timeout=5)

    assert result["ok"] is True, "点了继续应该返回 True（可以接着跑）"
    assert w.waiting_human is False


def test_没人管就超时(tmp_path):
    w, ev = _worker(tmp_path)
    events = []
    w.on_event = lambda e: events.append(e)

    t0 = time.time()
    ok = w._wait_for_human("出现验证码控件")
    assert ok is False, "没人管不能返回 True"
    assert time.time() - t0 >= 10, "应该等满了超时时间才返回"
    assert any(e.get("type") == "captcha_timeout" for e in events), events


def test_没给resume_event就不等(tmp_path):
    """没接上 Event（理论上不会发生）时不能干等"""
    cfg = get_config()
    cfg.outreach.captcha_wait_minutes = 0.2
    w = OutreachWorker(cfg, str(tmp_path / "t.db"), log=lambda m: None)
    assert w._wait_for_human("出现验证码控件") is False


def test_等待时间为0时不等(tmp_path):
    w, ev = _worker(tmp_path, captcha_wait_minutes=0)
    assert w._wait_for_human("出现验证码控件") is False


# ============================================================
# 通知
# ============================================================

def test_通知失败不影响流程(tmp_path, monkeypatch):
    """飞书没配好也不能把触达搞挂"""
    w, ev = _worker(tmp_path)
    cfg = w.cfg
    cfg.notify.lark_im_enabled = False
    assert w._notify_captcha("出现验证码控件") is False
    # 没开通知时也要正常往下走
    assert w._wait_for_human("出现验证码控件") in (True, False)


def test_通知开关关着就不发(tmp_path, monkeypatch):
    """避免测试真的发消息出去"""
    w, ev = _worker(tmp_path)
    w.cfg.notify.lark_im_enabled = False
    sent = []
    monkeypatch.setattr(
        "src.notify.lark_im.get_im_notifier",
        lambda *a, **k: type("N", (), {"is_configured": True,
                                       "send_text": lambda self, t: sent.append(t)})(),
    )
    assert w._notify_captcha("出现验证码控件") is False
    assert sent == [], "通知开关关着就不该发"


# ============================================================
# 窗口：藏起来 / 叫回来
# ============================================================

class _FakeSession:
    """假装自己是 CDP session"""

    def __init__(self):
        self.sent = []

    def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        return {}

    def detach(self):
        pass


class _FakeCtx:
    def __init__(self):
        self.sessions = []

    def new_cdp_session(self, page):
        s = _FakeSession()
        self.sessions.append(s)
        return s


def test_窗口挪动用的是CDP而不是启动参数():
    """--window-position 在 macOS 上会被持久化 profile 覆盖，必须走 CDP"""
    from src.outreach.browser import bring_to_front, move_offscreen

    ctx = _FakeCtx()
    move_offscreen(ctx, object())
    methods = [m for s in ctx.sessions for m, _ in s.sent]
    assert "Browser.setWindowBounds" in methods

    ctx2 = _FakeCtx()
    bring_to_front(ctx2, object(), "Chrome for Testing")
    calls = [(m, p) for s in ctx2.sessions for m, p in s.sent]
    bounds_calls = [p for m, p in calls if m == "Browser.setWindowBounds"]
    assert bounds_calls, "应该调 setWindowBounds"
    # 叫回来必须是屏幕内的坐标，且窗口状态是 normal（最小化的窗口点不动）
    b = bounds_calls[0]["bounds"]
    assert b["left"] >= 0 and b["top"] >= 0, b
    assert b["windowState"] == "normal", b
    # 还要把标签页激活，不然窗口可能压在别的窗口底下
    assert any(m == "Page.bringToFront" for m, _ in calls), calls


def test_窗口藏起来时坐标在屏幕外():
    from src.outreach.browser import move_offscreen

    ctx = _FakeCtx()
    move_offscreen(ctx, object())
    for s in ctx.sessions:
        for m, p in s.sent:
            if m == "Browser.setWindowBounds":
                assert p["bounds"]["left"] < 0, p


def test_激活应用失败不影响流程():
    """osascript 挂了就挂了，不能把触达搞崩"""
    from src.outreach import browser

    calls = []

    def _boom(name):
        calls.append(name)
        return False

    orig = browser.activate_app
    browser.activate_app = _boom
    try:
        from src.outreach.browser import bring_to_front

        ctx = _FakeCtx()
        assert bring_to_front(ctx, object(), "Chrome for Testing") is True
        assert calls, "应该尝试过激活"
    finally:
        browser.activate_app = orig

