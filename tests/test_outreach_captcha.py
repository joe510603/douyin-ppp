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
# 区分：验证码 vs 限流 vs 登录失效
# ============================================================

def test_三种停手分类正确():
    for msg in ("跳转到安全验证页", "出现验证码控件", "页面提示：请完成安全验证"):
        assert OutreachWorker._risk_kind(msg) == "verify", msg

    for msg in ("页面提示：操作频繁", "页面提示：账号存在异常",
                "页面提示：环境异常", "页面提示：访问频繁"):
        assert OutreachWorker._risk_kind(msg) == "limit", msg


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


def _worker_with_show(tmp_path, **cfg_over):
    """带「叫窗口」信号的 worker"""
    cfg = get_config()
    cfg.outreach.captcha_wait_minutes = 0.2
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    ev = threading.Event()
    show = threading.Event()
    w = OutreachWorker(cfg, str(tmp_path / "t.db"), log=lambda m: None,
                       resume_event=ev, show_window_event=show)
    return w, ev, show


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
    assert w._notify_risk("verify", "出现验证码控件") is False
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
    assert w._notify_risk("verify", "出现验证码控件") is False
    assert sent == [], "通知开关关着就不该发"


# ============================================================
# 三种停手：都要通知，而且指引不一样
# ============================================================

def test_三种停手都能分类():
    assert OutreachWorker._risk_kind("出现验证码控件") == "verify"
    assert OutreachWorker._risk_kind("跳转到安全验证页") == "verify"
    assert OutreachWorker._risk_kind("没有登录态（cookie 里没有 sessionid）") == "login"
    assert OutreachWorker._risk_kind("登录失效") == "login"
    # 软风控那段话里没有「验证码」「登录」，应该归到 limit
    assert OutreachWorker._risk_kind(
        "连续 2 条评论发出去但页面看不到，判定账号被限流（软风控），已停手。"
    ) == "limit"
    assert OutreachWorker._risk_kind("页面提示：操作频繁") == "limit"
    assert OutreachWorker._risk_kind("页面提示：环境异常") == "limit"


def _capture_notify(tmp_path, monkeypatch, kind, reason):
    """跑一次通知，把发出去的文本抓回来"""
    w, ev = _worker(tmp_path)
    w.cfg.notify.lark_im_enabled = True
    sent = []
    monkeypatch.setattr(
        "src.notify.lark_im.get_im_notifier",
        lambda *a, **k: type("N", (), {"is_configured": True,
                                       "send_text": lambda self, t: sent.append(t) or (True, "")})(),
    )
    w._notify_risk(kind, reason, {"display_id": "123456"})
    return sent[0] if sent else ""


def test_限流的通知要说清扫码没用(tmp_path, monkeypatch):
    """用户最容易误会的一点：限流不是登录问题，扫码没用"""
    text = _capture_notify(tmp_path, monkeypatch, "limit", "账号被限流（软风控）")
    assert "限流" in text
    assert "扫码" in text and "没用" in text, "得说清扫码没用（用户最容易误会成登录问题）"
    assert "抖音 App" in text, "得告诉用户怎么自己判断是不是真被限流"
    assert "冷却" in text


def test_登录失效的通知要提示扫码(tmp_path, monkeypatch):
    text = _capture_notify(tmp_path, monkeypatch, "login", "没有登录态")
    assert "扫码" in text
    assert "登录" in text


def test_验证码的通知要说清怎么验(tmp_path, monkeypatch):
    text = _capture_notify(tmp_path, monkeypatch, "verify", "出现验证码控件")
    assert "验证码" in text
    assert "打开浏览器窗口" in text, "得告诉他窗口平时是藏着的"


def test_三种通知内容互不相同(tmp_path, monkeypatch):
    a = _capture_notify(tmp_path, monkeypatch, "limit", "x")
    b = _capture_notify(tmp_path, monkeypatch, "login", "x")
    c = _capture_notify(tmp_path, monkeypatch, "verify", "x")
    assert len({a, b, c}) == 3, "三种情况的指引不能一样"


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


# ============================================================
# 「打开浏览器窗口」按钮
# ============================================================

def test_点了打开窗口按钮会把窗口叫回来(tmp_path, monkeypatch):
    """网页上点按钮 → worker 线程把窗口挪到屏幕中间"""
    w, ev, show = _worker_with_show(tmp_path)
    called = []
    monkeypatch.setattr(w, "_bring_window_back", lambda: called.append(1) or True)

    result = {}

    def _waiter():
        result["ok"] = w._wait_for_human("出现验证码控件")

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.3)

    show.set()                      # ← 相当于网页上点了按钮
    time.sleep(0.5)
    ev.set()                        # 验证完了
    t.join(timeout=5)

    assert called, "应该调过 _bring_window_back"
    assert result["ok"] is True


def test_没点按钮不会乱动窗口(tmp_path, monkeypatch):
    """不该没事就把用户窗口叫到屏幕中间（抢焦点）"""
    w, ev, show = _worker_with_show(tmp_path)
    called = []
    monkeypatch.setattr(w, "_bring_window_back", lambda: called.append(1) or True)

    t = threading.Thread(target=lambda: w._wait_for_human("出现验证码控件"))
    t.start()
    time.sleep(0.8)
    ev.set()
    t.join(timeout=5)
    assert called == [], "没点按钮就不该动窗口"


# ============================================================
# 验证完了自动接着跑
# ============================================================

class _FakePage:
    def __init__(self, url="https://www.douyin.com/video/123"):
        self.url = url


class _FakeDy:
    """假装是 Douyin 对象"""

    def __init__(self, blocked: bool):
        self.blocked = blocked
        self.calls = 0

    def check_risk(self):
        self.calls += 1
        if self.blocked:
            from src.outreach.douyin_api import RiskControl

            raise RiskControl("出现验证码控件")


def test_验证码还在就继续等(tmp_path, monkeypatch):
    w, ev, show = _worker_with_show(tmp_path)
    dy = _FakeDy(blocked=True)
    w._dy = dy
    w._page = _FakePage()
    monkeypatch.setattr(w, "_captcha_cleared", lambda: False)

    t0 = time.time()
    w._wait_for_human("出现验证码控件")
    assert time.time() - t0 >= 10, "验证码还在就该一直等"


def test_验证码过了自动继续(tmp_path, monkeypatch):
    """验证完不用再点「继续」，自动接着跑"""
    w, ev, show = _worker_with_show(tmp_path)
    w._dy = _FakeDy(blocked=False)
    w._page = _FakePage()

    t0 = time.time()
    ok = w._wait_for_human("出现验证码控件")
    assert ok is True, "验证码过了应该自动继续"
    assert time.time() - t0 < 10, "应该很快就检测到，不用等满超时"


def test_验证码判定看页面状态(tmp_path):
    """URL 还在 verify/captcha 上 → 不算过了"""
    w, ev, show = _worker_with_show(tmp_path)
    w._dy = _FakeDy(blocked=False)
    w._page = _FakePage(url="https://www.douyin.com/verify?x=1")
    assert w._captcha_cleared() is False

    w._page = _FakePage(url="https://www.douyin.com/video/123")
    assert w._captcha_cleared() is True


def test_没有页面时不瞎判(tmp_path):
    w, ev, show = _worker_with_show(tmp_path)
    w._dy = None
    w._page = None
    assert w._captcha_cleared() is False


# ============================================================
# runner：叫窗口的接口
# ============================================================

def test_runner_没在等验证时不能叫窗口():
    from src.outreach.runner import OutreachRunner

    r = OutreachRunner()
    assert r.waiting_human is False
    assert r.show_browser_window() is False


def test_runner_在等验证时能叫窗口():
    from src.outreach.runner import OutreachRunner

    r = OutreachRunner()
    r._mark_waiting_human("出现验证码控件")
    assert r.waiting_human is True
    assert r.show_browser_window() is True
    assert r._show_window.is_set(), "应该给 worker 发了信号"
    # 叫完窗口不等于恢复，还要人点「继续」或自动检测
    assert r.resume() is True
    assert r._resume.is_set()


