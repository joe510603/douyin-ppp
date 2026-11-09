"""浏览器层：一个专用的 Chrome profile，扫码登录一次，之后长期复用。

为什么不用无头 + cookie 注入：
- 抖音的登录态和风控指纹绑得紧，直接搬 cookie 很容易被判「环境异常」；
- 用 persist context 保持一个真实的、有历史的浏览器目录，最接近真人。

注意：这个 profile 目录里存着登录态，别提交到 git，也别拷给别人。
"""
from __future__ import annotations

import pathlib
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright

# 抹掉最明显的自动化特征。不是「反检测黑科技」，只是别一进门就自报家门。
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
    "--lang=zh-CN",
    # 抖音首页是自动播放的视频流，在自动化窗口里又卡又费 CPU；
    # 我们的活儿（搜索 / 拉作品 / 发评论）根本不需要播视频。
    "--autoplay-policy=user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    # 窗口被移到屏幕外时，别让 Chrome 认为「被遮挡了」而降频
    "--disable-features=CalculateNativeWinOcclusion",
]

# 自动化用的窗口直接摆到屏幕外面：有头模式（抖音私信必须）但不挡你干活。
_OFFSCREEN = ["--window-position=-3200,-3200", "--window-size=1440,900"]


class Browser:
    """with Browser(profile_dir, headless=False) as (ctx, page): ..."""

    def __init__(self, profile_dir: str | pathlib.Path, headless: bool = False,
                 slow_mo: int = 0, block_media: bool = False, offscreen: bool = False):
        self.profile_dir = pathlib.Path(profile_dir).expanduser()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo = slow_mo
        self.block_media = block_media
        self.offscreen = offscreen
        self._pw: Any = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None

    def __enter__(self) -> tuple[BrowserContext, Page]:
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=_ARGS + (_OFFSCREEN if self.offscreen else []),
            ignore_default_args=["--enable-automation", "--no-sandbox"],
            user_agent=_UA,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            accept_downloads=False,
        )
        self.ctx.add_init_script(_STEALTH)
        if self.block_media:
            # 拦掉视频/音频请求：页面秒开，也不会有声音吵到人
            def _route(route):
                try:
                    if route.request.resource_type == "media":
                        route.abort()
                    else:
                        route.continue_()
                except Exception:
                    try:
                        route.continue_()
                    except Exception:
                        pass
            self.ctx.route("**/*", _route)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(20000)
        return self.ctx, self.page

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


class Session:
    """进程内常驻的浏览器：跑完一轮不关，下一轮直接复用。

    为什么需要：每次都 launch_persistent_context 要重开窗口、重新加载抖音首页，
    既慢又会在你屏幕上闪一下。看板里连续点「干跑 / 真发」时尤其难受。
    这里把它做成「开一次、一直用」，窗口默认摆到屏幕外（offscreen=True），
    不抢焦点、不挡视线，需要人工处理时再单独 `python -m dtouch open`。
    """

    _browser: "Browser | None" = None
    _ctx = None
    _page = None
    _alive = False
    page = None  # 供外部直接取用（pipeline 的 _page_ctx）

    @classmethod
    def get(cls, profile_dir, headless: bool = False, slow_mo: int = 120,
            block_media: bool = False, offscreen: bool = True):
        """返回 (ctx, page)；已经开着就直接复用。"""
        if cls._browser is not None:
            try:
                if cls._alive and cls._ctx is not None and cls._page is not None:
                    return cls._ctx, cls._page
            except Exception:
                pass
            cls.close()
        b = Browser(profile_dir, headless=headless, slow_mo=slow_mo,
                    block_media=block_media, offscreen=offscreen)
        ctx, page = b.__enter__()
        cls._browser, cls._ctx, cls._page = b, ctx, page
        cls.page = page
        cls._alive = True
        return ctx, page

    @classmethod
    def alive(cls) -> bool:
        """只看标志位：page 对象不能跨线程调，会被 Playwright 拒绝。"""
        return bool(cls._alive and cls._page is not None)

    @classmethod
    def close(cls) -> None:
        b, cls._browser, cls._ctx, cls._page = cls._browser, None, None, None
        cls.page = None
        cls._alive = False
        if b is not None:
            try:
                b.__exit__(None, None, None)
            except Exception:
                pass
        cls._alive = False
