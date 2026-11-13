"""搜索要快：回首页不许等整页加载（2026-11-13 现场实测）

**现象**：一条线索的「找人」花了 130.6 秒：

    16:50:27  搜索：88593879790
    16:52:04  （这页的搜索框用不了：Timeout 3000ms exceeded.）
    16:52:33  （没拿到用户列表，重做一次搜索）
    16:52:37  ✓ 定位到 AAA   ⏱ 找人 130.6s

拆开看：`_type_in_search_box` 自己的超时只有 **3 秒**，
但它打日志的时刻晚了 97 秒 —— 那 97 秒全花在
``_goto(DOUYIN/)``（等 domcontentloaded）上。

抖音首页很重，而机器上同时跑着直播检测的十几个浏览器
（load average 12~20，10 核机器，62 个 Chromium 进程），
等整页资源就是 80+ 秒。

**修法**：
  1. 回首页只等「响应头」（``wait_until="commit"``），搜索框是首屏元素，
     commit 之后 1~2 秒就点得到 —— 这一步 80 秒 → 3~5 秒。
  2. 当前页输入成功但 URL 没跳时，**先在当前页再敲一次**，
     别急着回首页（回首页是最后手段）。
  3. 回首页不再重试 3 次（原来 45s × 3 = 135 秒最坏情况）。

这些行为必须用测试钉住，否则改一次退一次。
"""

from __future__ import annotations

from src.outreach.douyin_api import Douyin


class _假元素:
    def __init__(self):
        self.filled: list[str] = []
        self.pressed = 0

    def wait_for(self, state=None, timeout=None):
        return None

    def click(self):
        return None

    def fill(self, q):
        self.filled.append(q)

    def inner_text(self):
        return ""


class _假页面:
    """只记录关键动作的假页面"""

    def __init__(self, url="https://www.douyin.com/"):
        self.url = url
        self.gotos: list[dict] = []
        self.elements: list[_假元素] = []
        self.keyboard = self
        self._pressed = 0

    # --- 定位器 ---
    def locator(self, sel):
        el = _假元素()
        self.elements.append(el)
        return self

    @property
    def first(self):
        return self

    # --- 动作 ---
    def wait_for(self, state=None, timeout=None):
        return None

    def wait_for_timeout(self, ms):
        return None

    def click(self):
        return None

    def fill(self, q):
        self.elements[-1].fill(q)

    def press(self, key):
        self._pressed += 1

    def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append({"url": url, "wait_until": wait_until,
                           "timeout": timeout})
        self.url = url

    def on(self, *a, **k):
        pass

    def remove_listener(self, *a, **k):
        pass


def _douyin(page) -> Douyin:
    dy = Douyin(page, log=lambda m: None)
    dy.ensure_login = lambda: None
    dy.check_risk = lambda: None
    dy._body_text = lambda: ""

    def _capture(url_part, action, **k):
        # 真的执行页面动作（要测的就是「动作怎么做的」），
        # 只是不真的去抓响应
        action()
        return []
    dy._capture = _capture
    return dy


# ============================================================
# 1. 回首页只等响应头
# ============================================================

def test_回首页只等响应头不等整页资源():
    """domcontentloaded 实测要 80+ 秒，commit 只要几秒"""
    page = _假页面(url="https://www.douyin.com/video/123")
    dy = _douyin(page)

    # 当前页没有搜索框（locator 直接抛）→ 走回首页那条路
    def _boom(*a, **k):
        raise RuntimeError("这个页面没有搜索框")
    page.locator = _boom

    dy._search("88888888")

    assert page.gotos, "应该回过首页"
    goto = page.gotos[-1]
    assert goto["wait_until"] == "commit", (
        f"回首页不该等整页加载：{goto}"
    )
    assert goto["timeout"] and goto["timeout"] <= 20000, goto


def test_回首页不重试():
    """原来 45 秒 × 3 次 = 最坏 135 秒；回首页失败就该快速放弃"""
    page = _假页面(url="https://www.douyin.com/video/123")
    dy = _douyin(page)

    calls: list[str] = []

    def _fail(url, wait_until=None, timeout=None):
        calls.append(url)
        raise RuntimeError("net::ERR_CONNECTION_TIMED_OUT")
    page.goto = _fail

    import pytest

    with pytest.raises(RuntimeError):
        dy._goto("https://www.douyin.com/", wait="commit",
                 timeout=20000, retries=0)
    assert len(calls) == 1, f"回首页不该重试：{calls}"


def test_搜索最多两轮每轮只回一次首页():
    """搜索失败会重做一次（这是对的：抖音结果不稳定），
    但每一轮里回首页只能一次（原来 3 次 × 85 秒）"""
    page = _假页面(url="https://www.douyin.com/video/123")
    dy = _douyin(page)

    def _boom(*a, **k):
        raise RuntimeError("这个页面没有搜索框")
    page.locator = _boom

    dy._search("88888888")

    # 2 轮搜索 × 每轮 1 次回首页
    assert len(page.gotos) == 2, page.gotos


def test_回首页失败不会抛出去():
    """首页都打不开 → 记成「搜索没跑起来」，让上层按证据判断，
    而不是抛一个异常把整轮打断"""
    page = _假页面(url="https://www.douyin.com/video/123")
    dy = _douyin(page)

    def _boom(*a, **k):
        raise RuntimeError("没有搜索框")
    page.locator = _boom

    def _goto_fail(url, **k):
        raise RuntimeError("net::ERR_CONNECTION_TIMED_OUT")
    dy._goto = _goto_fail

    ev: dict = {}
    dy._search("88888888", evidence=ev)
    assert ev.get("search_ran") is False, ev


# ============================================================
# 2. 当前页能敲就先用当前页
# ============================================================

def test_当前页敲得动就不回首页():
    """当前页搜索框可用 → 直接敲，别回首页（快一个数量级）"""
    page = _假页面(url="https://www.douyin.com/jingxuan/search/abc")
    dy = _douyin(page)

    # URL 会跳到搜索页 → _wait_search_url 判定成功
    def _jump(url_before, q="", timeout=8.0):
        page.url = f"https://www.douyin.com/jingxuan/search/{q}"
        return True
    dy._wait_search_url = _jump

    dy._search("88888888")

    assert not page.gotos, f"当前页能用就不该回首页：{page.gotos}"
    assert page.elements and page.elements[0].filled == ["88888888"]
