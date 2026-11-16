"""检测器**不许先访问抖音首页** —— 那个动作本身会触发验证页

背景（2026-11-16 实测，同一时段、同一批账号、同一套 profile 副本）
----------------------------------------------------------------
    先开首页 + 注入 cookie + 再开用户页 → 验证页 5/5
    直接开用户页（不碰首页）           → 验证页 0/5

访问首页这个动作会让抖音给会话弹「验证码中间页」，
一半账号直接瞎掉 → 检测器以为「没开播」→ 采集器不起 → 弹幕全丢。

⚠ 以前代码里的注释说「必须先访问 domain 一次才能 add_cookies」——
  这个前提**根本不成立**：
  · profile 副本里本来就有登录态 cookie（seed_profile 复制的就是真浏览器的 cookie 库）
  · Playwright 的 add_cookies 指定了 domain 也不需要先导航

要钉住的：
1. 浏览器检测路径**不能** goto 抖音首页
2. 不再调 add_cookies 覆盖 profile 自带的 cookie
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

DETECTOR = pathlib.Path(__file__).resolve().parent.parent / "src" / "detector" / "live_detector.py"


def _source() -> str:
    return DETECTOR.read_text(encoding="utf-8")


def test_浏览器路径不访问抖音首页():
    """检测器不能 goto 抖音首页（那一步会触发验证页）。"""
    src = _source()
    # 去掉注释行再看：注释里会提到这段历史
    code = "\n".join(
        line for line in src.split("\n")
        if not line.strip().startswith("#")
    )
    hits = re.findall(r'goto\(\s*["\']https://www\.douyin\.com/["\']', code)
    assert not hits, f"检测器不该访问抖音首页（实测会触发验证页），found {hits}"


def test_不再覆盖注入cookie():
    """主页检测路径不调 add_cookies 覆盖 —— profile 自带的登录态 cookie 才是对的。

    注意：``_try_browser_detection`` 函数体里**还有一处** add_cookies
    （约 809 行），那是「备用取 room_id」路径：它用 ``browser.new_context()``
    开的是**临时 context**（没有 profile，也就没有登录态），
    所以那里必须手动注入 ttwid。那处不在本次修复范围内。

    这里只查「持久化上下文」那段 —— 也就是 profile 检测路径。
    """
    src = _source()
    start = src.index("async def _try_browser_detection")
    end = src.index("async def detect(", start)
    body = src[start:end]

    # 持久化上下文那段：从 launch_persistent_context 到打开用户主页
    p_start = body.index("launch_persistent_context")
    p_end = body.index("打开用户主页")
    segment = body[p_start:p_end]

    code = "\n".join(
        line for line in segment.split("\n")
        if not line.strip().startswith("#")
    )
    assert "add_cookies(" not in code, \
        "持久化上下文路径不该覆盖注入 cookie（profile 里本来就是真浏览器的登录态）"


def test_浏览器路径仍然打开用户主页():
    """别把修复做过头：主页还是要打开的。"""
    src = _source()
    code = "\n".join(
        line for line in src.split("\n")
        if not line.strip().startswith("#")
    )
    assert "/user/" in code, "还是要打开用户主页的"
