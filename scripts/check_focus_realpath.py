"""实测：跑真实触达路径（除了发评论），Chrome 会不会弹到前台

用户需求：「让它不在所有窗口最前面也能运行，并且不要一直弹到最前面」

只发评论有风险（账号在冷却），但**搜索、打开主页、打开作品、找评论框**
这些是只读动作——正好是触达里做得最多的部分。

所以这里模拟真实路径，只看焦点行为，不发任何评论。

    .venv/bin/python scripts/check_focus_realpath.py
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.outreach.browser import Browser, frontmost_app  # noqa: E402
from src.outreach.douyin_api import Douyin  # noqa: E402

# 触达历史里真实发过评论的那个号
TARGET = "999999104"
DOUYIN = "https://www.douyin.com"


def _snap(dy, tag):
    f = frontmost_app()
    print(f"   {tag:22s} 前台={f}")
    return f


def main() -> int:
    profile = pathlib.Path("data/outreach_profile")
    print(f"▶ 起始前台: {frontmost_app()}\n")

    steals = []
    with Browser(profile, headless=False, window_mode="background",
                 block_media=True) as (ctx, page):
        dy = Douyin(page, shot_dir=pathlib.Path("data/outreach_shots"))
        _snap(dy, "启动后")

        page.goto(f"{DOUYIN}/search/{TARGET}?type=user",
                  wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        f = _snap(dy, "搜索页")
        if f == "Google Chrome for Testing":
            steals.append("搜索页")

        page.goto(f"{DOUYIN}/user/self", wait_until="domcontentloaded",
                  timeout=45000)
        page.wait_for_timeout(2500)
        f = _snap(dy, "打开主页")
        if f == "Google Chrome for Testing":
            steals.append("打开主页")

        try:
            page.goto(f"{DOUYIN}/video/7705634241514447225",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
        except Exception as e:
            # 抖网络抽风是常事，不该因此判定失败——重点是焦点行为
            print(f"   （打开作品页失败：{str(e).splitlines()[0][:60]}）")
        f = _snap(dy, "打开作品页")
        if f == "Google Chrome for Testing":
            steals.append("打开作品页")

        # 找评论框（触达里最常做的动作）
        try:
            page.mouse.wheel(0, 800)
            page.wait_for_timeout(1500)
            page.mouse.move(600, 400)
            page.wait_for_timeout(600)
        except Exception as e:
            print(f"   （交互异常：{e}）")
        f = _snap(dy, "滚动/找评论框")
        if f == "Google Chrome for Testing":
            steals.append("滚动")

        from src.outreach.browser import _get_window_bounds
        b = _get_window_bounds(ctx, page)
        print(f"\n窗口坐标: {b}")

    print(f"\n结束时前台: {frontmost_app()}")
    print()
    print(f"抢前台的动作: {steals or '无 ✅'}")
    return 0 if not steals else 1


if __name__ == "__main__":
    raise SystemExit(main())
