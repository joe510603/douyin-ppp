"""触达专用浏览器的扫码登录

为什么要有这一步：
抖音的搜索和发评论都要求登录态（cookie 里有 sessionid）。
把 cookie 从别处搬过来很容易被判「环境异常」——
抖音的登录态和浏览器指纹绑得紧。

所以用**专用 profile + 扫码登录**：扫一次，长期有效，
而且这个 profile 里带着真实的浏览历史，最接近真人。

流程：
    1. 启动有头浏览器（窗口摆在屏幕外，onScreen=False 时摆在正中）
    2. 打开抖音首页，等用户扫码
    3. 轮询 cookie，检测到 sessionid 就成功
    4. 超时没登录就放弃（不硬等）
"""

from __future__ import annotations

import pathlib
import time
from typing import Callable, Optional

from .browser import Browser

DOUYIN = "https://www.douyin.com/"

# 登录成功的标志：cookie 里有这两个之一
_LOGIN_COOKIE_NAMES = ("sessionid", "sessionid_ss")


def _has_login(ctx) -> bool:
    try:
        names = {c["name"] for c in ctx.cookies(f"{DOUYIN}")}
    except Exception:
        return False
    return any(n in names for n in _LOGIN_COOKIE_NAMES)


def login(
    profile_dir: str | pathlib.Path,
    log: Optional[Callable[[str], None]] = None,
    timeout: int = 300,
    headless: bool = False,
) -> dict:
    """打开浏览器等用户扫码登录

    Args:
        profile_dir: 浏览器 profile 目录（登录态存在这里）
        log: 日志回调
        timeout: 最多等多久（秒）
        headless: 是否无头（登录必须能看到窗口）

    Returns:
        {"ok": bool, "message": str}
    """
    log = log or (lambda m: print(m))

    profile = pathlib.Path(profile_dir).expanduser()
    profile.mkdir(parents=True, exist_ok=True)

    log(f"启动浏览器（登录态目录：{profile}）…")
    try:
        # 登录必须能看到窗口，所以 offscreen=False
        with Browser(profile, headless=headless, slow_mo=0, offscreen=False) as (ctx, page):
            try:
                page.goto(DOUYIN, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                log(f"打开抖音首页失败：{str(e).splitlines()[0][:60]}")

            if _has_login(ctx):
                log("✅ 已经是登录状态，无需扫码")
                return {"ok": True, "message": "已是登录状态"}

            log("")
            log("📱 请在弹出的抖音窗口里扫码登录（用抖音 App 扫二维码）")
            log("   登录成功会自动检测到，最长等 %d 秒…" % timeout)
            log("")

            deadline = time.time() + timeout
            checked_at = 0.0
            while time.time() < deadline:
                time.sleep(1.0)
                # 每 10 秒提示一次还在等
                if time.time() - checked_at > 10:
                    checked_at = time.time()
                    log("   …还在等扫码")
                if _has_login(ctx):
                    log("✅ 登录成功！登录态已保存到 profile，后续直接复用")
                    return {"ok": True, "message": "登录成功"}

            log("❌ 超时未登录")
            return {"ok": False, "message": f"超时（{timeout} 秒）未完成扫码登录"}
    except Exception as e:
        log(f"❌ 启动浏览器失败：{e}")
        return {"ok": False, "message": str(e)}
