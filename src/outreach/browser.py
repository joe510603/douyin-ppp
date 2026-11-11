"""浏览器层：一个专用的 Chrome profile，扫码登录一次，之后长期复用。

为什么不用无头 + cookie 注入：
- 抖音的登录态和风控指纹绑得紧，直接搬 cookie 很容易被判「环境异常」；
- 用 persist context 保持一个真实的、有历史的浏览器目录，最接近真人。

注意：这个 profile 目录里存着登录态，别提交到 git，也别拷给别人。
"""
from __future__ import annotations

import pathlib
import subprocess
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright

# 抹掉最明显的自动化特征。不是「反检测黑科技」，只是别一进门就自报家门。
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

# 强制静音 —— 打开作品时抖音会直接放视频带声音，吵人还暴露「有人在看」
#
# 为什么用 JS 兜底而不是只靠拦网络请求：
# - 只拦网络请求：万一页面逻辑等视频加载，评论区可能不渲染（为了安静把功能搞坏不值）
# - 只靠 JS 静音：MediaSource / blob / 缓存里的音频还能出声
# 所以两层都要：JS 静音是**保证没声音**，网络拦截是可选项（省流量省 CPU）。
_MUTE_AUDIO = """
(() => {
  const mute = (el) => { try { el.muted = true; el.volume = 0; } catch (e) {} };
  // 1. play() 之前先静音（覆盖绝大多数自动播放）
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    mute(this);
    return origPlay.apply(this, arguments);
  };
  // 2. 页面上已经存在的媒体元素
  const muteAll = () => {
    try { document.querySelectorAll('video,audio').forEach(mute); } catch (e) {}
  };
  // 3. 后续新插入的媒体元素也静音（抖音是无限流，元素一直在换）
  const start = () => {
    muteAll();
    try {
      new MutationObserver(muteAll).observe(document.documentElement,
        {childList: true, subtree: true});
    } catch (e) {}
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
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

# 撞验证码时把窗口挪回屏幕中间的落点（留点边距，别贴着 Dock / 菜单栏）
_CAPTCHA_WINDOW = {"left": 120, "top": 80, "width": 1200, "height": 880}

# 藏起来的窗口停在哪：整个挪到屏幕左边外面。
# Chrome 会把 left 调整到「右边缘刚好贴着屏幕左边缘外」的位置，
# 实测这样窗口完全不可见，但浏览器照常跑（不是 headless）。
_OFFSCREEN_BOUNDS = {"left": -2000, "top": 0, "width": 1200, "height": 800}

# 窗口模式（outreach.window_mode）
# - background：窗口**看得见**，摆在屏幕右侧，但**不抢焦点、不置顶**（默认）
# - offscreen：完全藏到屏幕外
# - visible：正常显示并置前（扫码登录用）
WINDOW_MODES = ("background", "offscreen", "visible")


def _set_window_bounds(ctx: Any, page: Any, bounds: dict) -> bool:
    """用 CDP 移动/改变浏览器窗口"""
    session = None
    try:
        session = ctx.new_cdp_session(page)
        target = session.send("Browser.getWindowForTarget")
        window_id = target.get("windowId")
        if window_id is None:
            return False
        session.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": dict(bounds)})
        return True
    except Exception:
        return False
    finally:
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass


# ================================================================
# 前台焦点管理
# ================================================================
# 坑（实测）：浏览器一启动，macOS 就把前台应用换成它了，
# 哪怕窗口已经被挪到屏幕外、什么都看不见。
#
# 后果比「窗口挡住屏幕」更阴：用户切回自己的窗口继续干活，
# 看起来一切正常，但**键盘输入全进了那个看不见的浏览器**——
# 在文档里打的字一个字都存不下来，只在 Chrome 里冒出来。
#
# 所以：启动前记住前台是谁，藏好窗口后把焦点还回去。

def frontmost_app() -> str:
    """当前最前面的应用名（拿不到就返回空串）"""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process'
             ' whose frontmost is true'],
            capture_output=True, text=True, timeout=8)
        name = (out.stdout or "").strip()
        return name if out.returncode == 0 else ""
    except Exception:
        return ""


def activate_app(name: str) -> bool:
    """把某个应用叫到前台"""
    if not name:
        return False
    try:
        out = subprocess.run(
            ["osascript", "-e", f'tell application "{name}" to activate'],
            capture_output=True, text=True, timeout=8)
        return out.returncode == 0
    except Exception:
        return False


# 触达期间「用户自己的前台应用」。
# 浏览器启动时会抢前台，我们要记住原来是谁，把它还给用户；
# 撞验证码时我们主动把浏览器叫到前台，处理完之后再还回去——
# 不然浏览器就一直占着前台了。
_user_app = ""


def set_user_app(name: str) -> None:
    global _user_app
    _user_app = (name or "").strip()


def release_focus() -> bool:
    """把前台还给用户（撞验证码处理完之后用）"""
    if not _user_app:
        return False
    return activate_app(_user_app)


def _chrome_app_name(pw: Any) -> str:
    """从 Playwright 的浏览器可执行文件推出 macOS 上的 app 名

    例：.../chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing
    → "Google Chrome for Testing"
    """
    try:
        exe = pathlib.Path(pw.chromium.executable_path)
        for part in exe.parts:
            if part.endswith(".app"):
                return part[:-4]
    except Exception:
        pass
    return "Google Chrome"


def _get_window_bounds(ctx: Any, page: Any) -> dict:
    """读窗口当前坐标"""
    session = None
    try:
        session = ctx.new_cdp_session(page)
        wid = session.send("Browser.getWindowForTarget").get("windowId")
        if wid is None:
            return {}
        return session.send("Browser.getWindowBounds", {"windowId": wid}).get("bounds") or {}
    except Exception:
        return {}
    finally:
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass


def screen_bounds() -> tuple[int, int, int, int]:
    """主屏幕可用区域 (left, top, width, height)

    用 Finder 的 desktop 窗口 bounds——它就是主显示器的工作区。
    拿不到就按 1440x900 估。
    """
    try:
        out = subprocess.run(
            ["osascript", "-e",
             "tell application \"Finder\" to get bounds of window of desktop"],
            capture_output=True, text=True, timeout=8)
        nums = [int(float(x)) for x in (out.stdout or "").strip().split(",")]
        if len(nums) == 4:
            return nums[0], nums[1], nums[2] - nums[0], nums[3] - nums[1]
    except Exception:
        pass
    return 0, 0, 1440, 900


def background_bounds() -> dict:
    """「看得见但不挡事」的窗口位置：摆在主屏幕右侧

    用户需求：「chrome 的网页可以让我能看到，不用隐藏，
    只是让它不在所有窗口最前面也能运行」

    所以不是藏起来，也不是铺满屏幕，而是**贴在右半边**，
    像一个副面板：左边照常干活，它在后面自己跑。
    """
    l, t, w, h = screen_bounds()
    # 宽度取屏幕的 42%，最多 1100；高度留出菜单栏和 Dock
    bw = max(760, min(1100, int(w * 0.42)))
    bh = max(560, min(860, h - 120))
    return {"left": l + w - bw - 16, "top": t + 40,
            "width": bw, "height": bh}


def place_in_background(ctx: Any, page: Any) -> bool:
    """把窗口摆到屏幕右侧（可见，但在后面跑，不抢焦点）

    实测坑：Chrome 偶尔只接受「移动」不接「改尺寸」，
    所以这里设完要**读回来核对**，不对就再来一次。
    """
    target = background_bounds()
    ok = _set_window_bounds(ctx, page, target)
    for _ in range(2):
        cur = _get_window_bounds(ctx, page)
        if not cur:
            break
        # 尺寸差得多就是没生效，再设一次
        if abs(int(cur.get("width", 0)) - int(target["width"])) > 40:
            ok = _set_window_bounds(ctx, page, target) or ok
        else:
            break
    return ok


def move_offscreen(ctx: Any, page: Any) -> bool:
    """把窗口挪到屏幕外（看不见，但浏览器照常跑）

    为什么不能只靠启动参数 ``--window-position=-3200,-3200``：
    macOS 上持久化 profile 会记住上次的窗口位置，把这个参数覆盖掉——
    实测「屏幕外」和「不屏幕外」的窗口位置只差 22 像素，
    也就是说触达跑的时候那个窗口一直实实在在挡在屏幕左上角。

    CDP 的 ``Browser.setWindowBounds`` 是实测可靠的。
    """
    return _set_window_bounds(ctx, page, _OFFSCREEN_BOUNDS)


def bring_to_front(ctx: Any, page: Any, app_name: str = "") -> bool:
    """把浏览器窗口挪到屏幕中间并置前

    撞验证码时必须这么干：窗口平时藏在屏幕外，
    你根本看不见那个验证码长什么样。

    「置前」分两层，缺一不可：
    1. ``Browser.setWindowBounds`` 把窗口挪回屏幕中间；
    2. ``Page.bringToFront`` + osascript activate 把 app 叫到前台——
       只挪位置的话，窗口可能压在别的窗口底下，用户还是会以为没叫回来。

    Playwright 的 page API 管不到窗口位置，但 Chromium 的调试协议可以。
    """
    bounds = dict(_CAPTCHA_WINDOW)
    bounds["windowState"] = "normal"
    ok = _set_window_bounds(ctx, page, bounds)

    # 把标签页也激活一下
    session = None
    try:
        session = ctx.new_cdp_session(page)
        session.send("Page.bringToFront")
    except Exception:
        pass
    finally:
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass

    # 再把整个 app 叫到前台（macOS 上窗口在前台 app 里才是真的「置前」）
    for name in (app_name, "Google Chrome for Testing", "Google Chrome", "Chromium"):
        if not name:
            continue
        if activate_app(name):
            break
    return ok


class Browser:
    """with Browser(profile_dir, headless=False) as (ctx, page): ..."""

    def __init__(self, profile_dir: str | pathlib.Path, headless: bool = False,
                 slow_mo: int = 0, block_media: bool = False,
                 offscreen: bool = False, window_mode: str = ""):
        self.profile_dir = pathlib.Path(profile_dir).expanduser()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo = slow_mo
        self.block_media = block_media
        # window_mode 是新接口；offscreen 是老参数，保留兼容。
        # 两者都给了的话 window_mode 优先。
        mode = (window_mode or "").strip()
        if mode not in WINDOW_MODES:
            mode = "offscreen" if offscreen else "background"
        self.window_mode = mode
        self.offscreen = (mode == "offscreen")
        self._pw: Any = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        # macOS 上这个浏览器的 app 名（撞验证码时要用它把窗口叫到前台）
        self.app_name: str = ""

    def __enter__(self) -> tuple[BrowserContext, Page]:
        # 启动浏览器会把前台抢走（实测：Edge → Google Chrome for Testing，
        # 而且会一直占着前台）。先把「谁在前台」记下来，
        # 等窗口摆好之后原样还回去——不然用户切回自己的窗口，
        # 打的字会全进了那个浏览器里。
        #
        # visible 模式例外：扫码登录时用户**就是要**看到并操作那个窗口。
        prev_front = frontmost_app() if self.window_mode != "visible" else ""

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
        self.app_name = _chrome_app_name(self._pw)
        self.ctx.add_init_script(_STEALTH)
        # 静音是默认行为：触达要打开作品看评论区，抖音会直接放视频带声音。
        self.ctx.add_init_script(_MUTE_AUDIO)
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

        # ---- 摆窗口 ----
        if self.window_mode == "offscreen":
            # 启动参数 --window-position 在 macOS 上经常不生效
            # （持久化 profile 会记住上次窗口位置），
            # 所以启动完再主动用 CDP 挪一次——这个是实测可靠的。
            move_offscreen(self.ctx, self.page)
        elif self.window_mode == "background":
            # 摆在屏幕右侧：看得见，但在后面自己跑
            place_in_background(self.ctx, self.page)

        # ---- 把前台还给用户 ----
        # 窗口藏起来或者摆在旁边都还不够：焦点不还回去的话，
        # 用户切回自己的窗口继续干活，打的字会全进了这个浏览器里。
        if prev_front and prev_front != self.app_name:
            set_user_app(prev_front)
            activate_app(prev_front)
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
    def app_name(cls) -> str:
        """这个浏览器的 macOS app 名（把窗口叫回前台时要用）"""
        if cls._browser is not None:
            return getattr(cls._browser, "app_name", "")
        return ""

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
