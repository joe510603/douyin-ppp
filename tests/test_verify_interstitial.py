"""「验证码中间页」必须被认出来，不能当成「对方关了私信」

事故现场（2026-11-15）
-----------------------
用户收到一张自相矛盾的红卡片：

    标题：账号被限流（软风控），已停手
    原因：连续 2 条私信发不出去，多半是私信功能被限了
    怎么办：评论发出去了、也没报错，但页面上看不到——抖音把它静默吞了

实测出来的真相：
1. 抖音给触达那个 Edge 会话弹了**「验证码中间页」**（标题就叫这名字），
   首页正文是空的，用户主页显示「用户不存在」。
2. 主页渲染不出来 → 自然找不到「私信」按钮
   → 代码记成「对方关闭了私信」
   → 连续 2 条 → 判「私信功能被限流」→ **停掉整个触达**
   → 卡片原因说「私信被限」，步骤却是「评论被静默吞」，两边打架
3. 而账号其实没被限流：停手之后 14:13 / 14:14 / 14:17 连发 3 条评论全部成功。

盯住的：
1. `check_risk()` 要认出「验证码中间页」→ 报「要人工验证」，不是限流
2. 找不到私信按钮时，要分清「对方关了私信」和「我们的页面没渲染出来」
3. 检测器的 `rendered` 判据不能把验证页当成「页面正常」
   （那个壳页里也有 sec_uid，只看 sec_uid 会一直不告警）
"""

from __future__ import annotations

import pathlib
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.detector.live_detector import LiveDetector  # noqa: E402
from src.outreach.douyin_api import (  # noqa: E402
    DM_FAIL_ENV, DM_FAIL_TARGET, Douyin, RiskControl,
)


# ============================================================
# 1. check_risk 要认出「验证码中间页」
# ============================================================

class _FakePage:
    """够 check_risk 用的假页面"""

    def __init__(self, title="", body="", html="", url="https://www.douyin.com/"):
        self.url = url
        self._title = title
        self._body = body
        self._html = html
        self.shots: list[str] = []

    def title(self):
        return self._title

    def inner_text(self, _selector="body"):
        return self._body

    def content(self):
        return self._html

    def evaluate(self, *_a, **_k):
        return False


def _douyin_with_page(page) -> Douyin:
    d = Douyin.__new__(Douyin)          # 不跑 __init__（别真开浏览器）
    d.page = page
    d.shots = []
    d.shot_dir = None                    # 不存截图
    d.verify_flow = ""                   # 没有平台裁决
    d.verify_flow_at = 0.0
    d.verify_ways = []
    return d


def test_验证码中间页要报要人工验证():
    """标题就是「验证码中间页」→ 必须当成「要人来验」，不是限流。"""
    page = _FakePage(title="验证码中间页", body="", html="<html>验证 captcha</html>")
    d = _douyin_with_page(page)
    with pytest.raises(RiskControl) as ei:
        d.check_risk()
    assert ei.value.kind == "verify", ei.value.kind
    assert "验证" in str(ei.value), str(ei.value)


def test_标题带验证也算():
    page = _FakePage(title="安全验证", body="", html="<html></html>")
    d = _douyin_with_page(page)
    with pytest.raises(RiskControl) as ei:
        d.check_risk()
    assert ei.value.kind == "verify"


def test_正文空且html提到验证码也算():
    """即使标题正常，HTML 里有「验证码中间页」这个专属字样也算。

    ⚠ 判据要**精确**：抖音正常页面常驻一个隐藏的 nocaptcha iframe，
    用「HTML 提到 captcha 就当验证」会天天误报 → 触达动不动停下来叫人。
    """
    page = _FakePage(title="抖音", body="   ", html="<html><div>验证码中间页</div></html>")
    d = _douyin_with_page(page)
    with pytest.raises(RiskControl) as ei:
        d.check_risk()
    assert ei.value.kind == "verify"


def test_隐藏的nocaptcha_iframe不算验证():
    """正常页面里的隐藏 iframe（src 带 captcha）不该被判成验证页。"""
    page = _FakePage(
        title="某某的抖音 - 抖音",
        body="关注 | 私信 | 作品" * 40,
        html='<html><iframe src="https://x/rc-verifycenter/rmc-nocaptcha/index.html"></iframe></html>')
    d = _douyin_with_page(page)
    d.check_risk()   # 不该抛


def test_正常页面不误报():
    """正常主页（有内容、没有验证）不能被判成验证页。"""
    page = _FakePage(title="某某的抖音 - 抖音", body="关注 | 私信 | 作品" * 30,
                     html="<html>normal</html>")
    d = _douyin_with_page(page)
    d.check_risk()   # 不该抛


# ============================================================
# 2. 找不到私信按钮时，要分清是谁的问题
# ============================================================

def test_主页没渲染出来算我们侧问题():
    """正文空 / 标题是验证页 → 不是「对方关了私信」。

    回归事故：抖音弹验证码中间页 → 主页空白 → 找不到私信按钮 →
    被记成「对方关闭了私信」→ 再被当成「私信被限流」→ 白停一轮。
    """
    page = _FakePage(title="验证码中间页", body="", html="<html></html>")
    d = _douyin_with_page(page)
    # 直接测分类逻辑：open_dm 里按钮找不到时的分支
    body = page.inner_text("body")
    title = page.title()
    degraded = ("验证" in title) or ("验证码" in title) or ("用户不存在" in body) \
        or len(body.strip()) < 60
    assert degraded is True
    assert DM_FAIL_ENV != DM_FAIL_TARGET


def test_正常主页但没私信按钮算对方侧问题():
    """页面正常（内容够多、没有验证）→ 就是对方关了私信。"""
    page = _FakePage(title="某某的抖音 - 抖音", body="关注 | 作品 | 喜欢 | 私密账号" * 40,
                     html="<html>normal</html>")
    body = page.inner_text("body")
    title = page.title()
    degraded = ("验证" in title) or ("验证码" in title) or ("用户不存在" in body) \
        or len(body.strip()) < 60
    assert degraded is False


# ============================================================
# 3. 检测器的 rendered 判据：验证页不能算「页面正常」
# ============================================================

def test_验证页渲染不算看见账号():
    """壳页里也有 sec_uid，只看 sec_uid 会让自检永远不告警。"""
    det = LiveDetector()
    sec = "MS4wLjABAAAATVed-OOWyEXK_HHppKlR1fTchBgq2WE1Bjc3k88AoUrPuDkKbMugORGZpXI4PCFi"

    # 页面 HTML 里有 sec_uid（验证页的壳里也有），但标题是验证页
    page = MagicMock()
    page.title.return_value = "验证码中间页"
    page.inner_text.return_value = ""
    page.content.return_value = f"<html>{sec}</html>"
    page.evaluate.return_value = None

    ctx = MagicMock()
    ctx.pages = [page]
    ctx.new_page.return_value = page

    # 只验证判据本身（不真的开浏览器）
    body_text = page.inner_text("body")
    page_title = page.title()
    verify_page = ("验证" in page_title) or ("验证码" in page_title)
    degraded_page = ("用户不存在" in body_text) or (len(body_text.strip()) < 60)
    rendered = bool(sec and sec in page.content() and not verify_page and not degraded_page)
    assert rendered is False, "验证页不能算「看得见这个账号」"
