"""浏览器身份：全项目统一「用哪个浏览器」。

为什么要有这个模块
------------------
抖音（字节）的风控看的是**指纹一致性**，不是某一个特征。
之前项目里各处硬编码了不同的 UA（125 / 126 / 131），而真正跑的引擎版本各不相同，
于是出现了两类一眼就能看出来的破绽：

1. **UA 字符串和 Client Hints 互相打脸**
   ``navigator.userAgent`` 说是 ``Chrome/131``，
   ``navigator.userAgentData.brands`` 却老实说 ``Chromium/153``。
   Playwright 的 ``user_agent=`` 参数**只改字符串、不改 brands**，
   所以两个版本号能在同一个页面里被当场对撞——不用猜，读一下就知道你在编。

2. **同一个账号在不同组件里是不同指纹**
   触达用 131、采集器用 125、直播用 126，来回切换像机器人。

所以这里只回答一个问题：**该用哪个浏览器**。
至于 UA 怎么报——**交给浏览器自己如实说**，一个字都别改。
"""

from __future__ import annotations

import pathlib
import sys

# (Playwright channel, macOS app 名, 常见安装路径)
_MAC_CANDIDATES = (
    ("chrome", "Google Chrome", (
        "/Applications/Google Chrome.app",
        "/Applications (x86_64)/Google Chrome.app",
    )),
    ("msedge", "Microsoft Edge", (
        "/Applications/Microsoft Edge.app",
        "/Applications (x86_64)/Microsoft Edge.app",
    )),
)

_LINUX_CANDIDATES = (
    ("chrome", "Google Chrome", (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    )),
    ("msedge", "Microsoft Edge", (
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    )),
)

# 自带 Chromium 的 app 名（兜底用的，指纹最弱）
BUNDLED_APP_NAME = "Google Chrome for Testing"


def installed_browsers() -> list[tuple[str, str]]:
    """系统里装好的真浏览器，按优先级：``[(channel, app 名), ...]``

    只列**系统真实安装**的浏览器——Playwright 自带的那个是
    "Google Chrome for Testing"（非官方构建），当兜底可以，当主力不合适。
    """
    if sys.platform == "darwin":
        candidates = _MAC_CANDIDATES
    elif sys.platform.startswith("linux"):
        candidates = _LINUX_CANDIDATES
    else:
        # Windows：让 Playwright 自己去注册表里找
        return [("chrome", "Google Chrome"), ("msedge", "Microsoft Edge")]

    found: list[tuple[str, str]] = []
    for channel, app_name, paths in candidates:
        for p in paths:
            if pathlib.Path(p).expanduser().exists():
                found.append((channel, app_name))
                break
    return found


def pick() -> tuple[str, str]:
    """挑一个浏览器来用。

    Returns:
        ``(channel, app 名)``。channel 为空串表示用 Playwright 自带的 Chromium
        （指纹最弱，只在没有系统浏览器时兜底）。
    """
    browsers = installed_browsers()
    if browsers:
        return browsers[0]
    return ("", BUNDLED_APP_NAME)


def app_name_for(channel: str) -> str:
    """channel → macOS 上的 app 名

    撞验证码时要用它把窗口叫到前台（osascript activate）。

    ⚠ 别用 Playwright 的 ``executable_path`` 推：用了 channel 之后，
    ``executable_path`` 指向的**仍然是自带的 Chromium**，推出来是错的。
    """
    return {
        "chrome": "Google Chrome",
        "msedge": "Microsoft Edge",
    }.get(channel, BUNDLED_APP_NAME)
