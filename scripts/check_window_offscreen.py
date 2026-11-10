"""实测：触达窗口能不能藏起来、撞验证码时能不能叫回屏幕中央

为什么要有这个脚本
------------------
「窗口藏到屏幕外」听起来简单，但 macOS 上有三个坑：
1. Chrome 持久化 profile 会记住上次的窗口位置，``--window-position`` 启动参数直接被覆盖；
2. 浏览器一启动就把**前台焦点抢走**了——哪怕窗口在屏幕外、什么都看不见。
   用户切回自己的窗口继续干活，看起来一切正常，但打字全进了那个看不见的浏览器；
3. 窗口挪到屏幕外之后，撞验证码时你**根本看不见**验证码长什么样，
   所以「能不能叫回来」比「能不能藏起来」更重要。

这个脚本不开抖音、不碰账号，只开一个空白页看窗口坐标和前台应用：

    .venv/bin/python scripts/check_window_offscreen.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.outreach.browser import (  # noqa: E402
    Browser, bring_to_front, frontmost_app, move_offscreen,
)

CHROME_NAMES = ("Google Chrome for Testing", "Chromium", "Google Chrome")


def _bounds(page):
    """读窗口当前坐标"""
    session = page.context.new_cdp_session(page)
    try:
        wid = session.send("Browser.getWindowForTarget").get("windowId")
        return session.send("Browser.getWindowBounds", {"windowId": wid}).get("bounds")
    finally:
        session.detach()


def _is_chrome(name: str) -> bool:
    return any(n in (name or "") for n in CHROME_NAMES)


def main() -> int:
    profile = pathlib.Path("data/outreach_profile")
    print(f"▶ 用 profile：{profile}")
    print(f"▶ 开浏览器之前，前台是：{frontmost_app()}")

    with Browser(profile, headless=False, offscreen=True) as (ctx, page):
        page.goto("about:blank")

        b1 = _bounds(page)
        f1 = frontmost_app()
        print(f"① 启动后（窗口应在屏幕外、前台应还是你）：")
        print(f"   坐标 {b1}")
        print(f"   前台 {f1}")
        off_ok = (b1 or {}).get("left", 0) < 0
        focus_ok = not _is_chrome(f1)

        # 再挪一次，确认幂等
        move_offscreen(ctx, page)
        b2 = _bounds(page)
        print(f"② 再次 move_offscreen（应幂等）: {b2}")

        # 叫回屏幕中央
        ok = bring_to_front(ctx, page)
        b3 = _bounds(page)
        f3 = frontmost_app()
        print(f"③ bring_to_front（返回 {ok}）:")
        print(f"   坐标 {b3}")
        print(f"   前台 {f3}")
        back_ok = (b3 or {}).get("left", -1) >= 0 and (b3 or {}).get("top", -1) >= 0
        front_ok = _is_chrome(f3)

        # 叫回来之后还能不能操作
        page.goto("about:blank")
        page.set_content("<h1 id='t'>窗口可操作</h1>")
        click_ok = page.inner_text("#t") == "窗口可操作"

        # 再藏回去
        move_offscreen(ctx, page)
        b4 = _bounds(page)
        print(f"④ 再藏回屏幕外: {b4}")

    print()
    print(f"藏得起来（窗口在屏幕外）: {'✅' if off_ok else '❌'}  {b1}")
    print(f"不抢焦点（前台还是你的）: {'✅' if focus_ok else '❌'}  {f1}")
    print(f"叫得回来（回到屏幕中间）: {'✅' if back_ok else '❌'}  {b3}")
    print(f"叫得回来且置顶:           {'✅' if front_ok else '❌'}  {f3}")
    print(f"叫回来后还能操作:         {'✅' if click_ok else '❌'}")
    return 0 if all((off_ok, focus_ok, back_ok, front_ok, click_ok)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
