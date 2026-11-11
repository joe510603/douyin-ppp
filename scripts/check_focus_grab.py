"""实测：background 模式下，Chrome 会不会一直抢前台

用户需求：「chrome 的网页可以让我能看到，不用隐藏，
只是让它不在所有窗口最前面也能运行，并且不要一直弹到最前面」

要验三件事：
1. 窗口**看得见**（在屏幕内，不是负坐标）
2. 启动之后**焦点还给用户**，运行途中**不弹回前台**
3. 撞验证码时才把它叫到前台（bring_to_front）

做法：跑一遍真实触达会走的动作序列，每一步都查一次前台是谁。

    .venv/bin/python scripts/check_focus_grab.py
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.outreach.browser import Browser, frontmost_app  # noqa: E402

DOUYIN = "https://www.douyin.com/"
WORK = "https://www.douyin.com/video/7705634241514447225"


def main() -> int:
    profile = pathlib.Path("data/outreach_profile")
    print(f"▶ 起始前台: {frontmost_app()}\n")

    with Browser(profile, headless=False, window_mode="background") as (ctx, page):
        print(f"① 启动后前台: {frontmost_app()}")

        page.goto(DOUYIN, wait_until="domcontentloaded", timeout=45000)
        time.sleep(2)
        print(f"② goto 首页后: {frontmost_app()}")

        page.goto(WORK, wait_until="domcontentloaded", timeout=45000)
        time.sleep(2)
        print(f"③ goto 作品页后: {frontmost_app()}")

        new_page = ctx.new_page()
        new_page.goto("about:blank")
        time.sleep(1)
        print(f"④ 开新标签页后: {frontmost_app()}")
        new_page.close()
        time.sleep(1)

        try:
            page.mouse.wheel(0, 600)
            page.wait_for_timeout(800)
            page.mouse.move(400, 300)
            page.wait_for_timeout(500)
        except Exception as e:
            print(f"   （交互异常：{e}）")
        print(f"⑤ 滚动/移动鼠标后: {frontmost_app()}")

        from src.outreach.browser import _get_window_bounds
        b = _get_window_bounds(ctx, page)
        print(f"\n窗口坐标: {b}")

        visible = (b.get("left", -1) >= 0 and b.get("top", -1) >= 0
                   and b.get("width", 0) > 0)
        chrome_front = frontmost_app() == "Google Chrome for Testing"

    print(f"\n结束后前台: {frontmost_app()}")
    print()
    print(f"窗口在屏幕内（看得见）: {'✅' if visible else '❌'}  {b}")
    print(f"跑完 Chrome 没占着前台: {'✅' if not chrome_front else '❌'}")
    return 0 if (visible and not chrome_front) else 1


if __name__ == "__main__":
    raise SystemExit(main())
