"""触达窗口模式 —— background / offscreen / visible

用户需求：「chrome 的网页可以让我能看到，可以不用隐藏，
只是让它不在所有窗口最前面也能运行，并且不要一直弹到最前面」

三个要点：
1. **看得见**：background 模式窗口在屏幕内（不是负坐标）
2. **不抢焦点**：启动后把前台还给用户，跑完也不占着前台
3. **不一直弹到最前面**：真实触达路径（搜索/开主页/开作品/找评论框）
   不把 Chrome 弹到前台；只有撞验证码时才主动叫到前面，
   处理完之后再把前台还回去
"""
from __future__ import annotations

from src.outreach import browser
from src.outreach.browser import (
    WINDOW_MODES, Browser, background_bounds, screen_bounds,
)


# ============================================================
# 模式解析
# ============================================================

def test_三种模式都在():
    assert set(WINDOW_MODES) == {"background", "offscreen", "visible"}


def test_默认是background(tmp_path):
    b = Browser(tmp_path / "profile", window_mode="")
    assert b.window_mode == "background", "默认要看得见，不能藏起来"


def test_老的offscreen参数还能用(tmp_path):
    """兼容：以前是 hide_window / offscreen 布尔"""
    b = Browser(tmp_path, offscreen=True)
    assert b.window_mode == "offscreen"
    assert b.offscreen is True


def test_非法模式退回background(tmp_path):
    b = Browser(tmp_path, window_mode="瞎写的")
    assert b.window_mode == "background"


def test_window_mode优先于offscreen(tmp_path):
    b = Browser(tmp_path, offscreen=True, window_mode="background")
    assert b.window_mode == "background"


# ============================================================
# 背景位置：看得见，但不挡事
# ============================================================

def test_背景位置在屏幕内():
    b = background_bounds()
    l, t, w, h = screen_bounds()
    assert b["left"] >= l, "不能跑到屏幕左边外面去"
    assert b["top"] >= t
    assert b["left"] + b["width"] <= l + w + 40, "别超出屏幕右边缘太多"
    assert b["width"] > 400 and b["height"] > 300, "太小就看不清了"


def test_背景位置在右半边():
    """摆在右边，左边留给用户干活"""
    b = background_bounds()
    l, t, w, h = screen_bounds()
    assert b["left"] >= l + w * 0.4, "应该在屏幕右半边"


# ============================================================
# 焦点管理
# ============================================================

def test_记住用户的前台应用():
    browser.set_user_app("Microsoft Edge")
    assert browser._user_app == "Microsoft Edge"
    browser.set_user_app("")


def test_还焦点时没记住就不动():
    browser.set_user_app("")
    assert browser.release_focus() is False


def test_还焦点会激活用户的应用(monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "activate_app", lambda n: calls.append(n) or True)
    browser.set_user_app("Microsoft Edge")
    assert browser.release_focus() is True
    assert calls == ["Microsoft Edge"]
    browser.set_user_app("")


def test_验证码处理完会把前台还给用户(tmp_path):
    """撞验证码时我们把浏览器叫到前面，处理完必须还回去，
    不然浏览器就一直占着前台，用户打的字进错了窗口"""
    from src.outreach.worker import OutreachWorker
    import threading

    cfg_outreach = None
    from src.config import get_config

    cfg = get_config()
    cfg.outreach.captcha_wait_minutes = 0.2
    ev = threading.Event()
    w = OutreachWorker(cfg, str(tmp_path / "t.db"), log=lambda m: None,
                       resume_event=ev)

    released = []
    w._release_focus = lambda: released.append(1)

    t = threading.Thread(target=lambda: w._wait_for_human("出现验证码控件"))
    t.start()
    time_sleep = 0.3
    import time as _t
    _t.sleep(time_sleep)
    ev.set()
    t.join(timeout=5)
    assert released, "验证完了应该把前台还给用户"
