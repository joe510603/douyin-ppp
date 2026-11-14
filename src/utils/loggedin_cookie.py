"""登录态 cookie 的「源头」与续命。

问题
----
检测器靠抖音 Web 端用户接口（``/aweme/v1/web/user/profile/other/``）判开播，
而那个接口**必须登录态**（2026-11-14 实测）：

    未登录 cookie → HTTP 200，但 body **0 字节**（什么都拿不到）
    登录态 cookie → 25KB，含 ``room_data.status=2``

登录态是会过期的。一过期，检测器对所有账号一律「未检测到直播」→
采集器永不启动 → **一整天的弹幕全丢**，而日志上一片正常
（因为 ``get_cookie_status()`` 只查 ttwid 那个匿名令牌，跟登录态毫无关系，
所以它照样打印「✅ 有效」）。

2026-11-14 就是这么丢了一整天：14:07 之后一条弹幕都没有，
而四家门店其实一直在播。

办法
----
登录态真正的源头是**触达那个真浏览器的 profile** ——
用户在 Edge 里正常用抖音，sessionid 由抖音自动续期。
这里定期把 profile 里的**明文** cookie 读出来（Edge 用钥匙串里的密钥解密），
交给检测器用；变了就写回 config，让重启后依然有效。

为什么不用「手动复制 cookie」的老办法
    每次都要重新扫码；抄一次就固定一份，过几天照样过期。

为什么用「副本」而不是直接开原 profile
    触达跑的时候原 profile 被 Edge 占着，第二个浏览器打不开它；
    副本只拷必要文件（~1MB），Edge 能解开它的 cookie（同一把钥匙串密钥）。
"""

from __future__ import annotations

import pathlib
import threading
import time
from typing import Optional

from .logger import get_logger

log = get_logger("loggedin_cookie")

# 这些 cookie 是**房间相关**的，不是身份
# （live_debug_info 里存着上次看的直播间的 roomId）
# 留着它会让检测器把「别的直播间」的 room_id 塞给当前账号
_EXCLUDE_NAMES = {"live_debug_info"}


class LoggedInCookieProvider:
    """从真浏览器 profile 里读出登录态 cookie，并按 TTL 自动续期。"""

    def __init__(self, profile_dir: str = "data/outreach_profile",
                 base_dir: str = "data/detect_profiles",
                 ttl_minutes: int = 15):
        self.profile_dir = pathlib.Path(profile_dir).expanduser()
        self.base_dir = pathlib.Path(base_dir).expanduser()
        self.ttl_seconds = max(60, int(ttl_minutes) * 60)
        self._cookie: str = ""
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()
        self._refreshing = False

    # ---------- 对外 ----------

    def get(self) -> str:
        """拿当前缓存的登录态 cookie（**不阻塞**）。没有就返回空串。"""
        with self._lock:
            return self._cookie

    def is_fresh(self) -> bool:
        with self._lock:
            return bool(self._cookie) and (time.time() - self._fetched_at) < self.ttl_seconds

    def ensure_fresh(self) -> bool:
        """过期了就起个后台线程去刷新（**立即返回**，不阻塞调用方）。

        为什么要有这个：检测每 60 秒一轮，不能为了拿 cookie 把每一轮都拖慢；
        但也不能一直用一份会过期的 cookie —— 所以后台悄悄续，续好了自动生效。
        """
        if self.is_fresh():
            return True
        with self._lock:
            if self._refreshing:
                return bool(self._cookie)
            self._refreshing = True

        def _worker():
            try:
                self.refresh(force=True)
            except Exception as e:
                log.warning(f"登录态 cookie 后台刷新失败: {e}")
            finally:
                with self._lock:
                    self._refreshing = False

        threading.Thread(target=_worker, name="login-cookie-refresh", daemon=True).start()
        return bool(self.get())

    def refresh(self, force: bool = False) -> str:
        """立刻重新读一次 cookie，返回新的 cookie 字符串。"""
        if not force and self.is_fresh():
            return self.get()
        cookie = self._harvest()
        # 没有 sessionid = 未登录 —— 这种 cookie 抖音 Web 端用户接口会返回 0 字节，
        # 用了反而让检测器「看不见」直播间。用它不如用上一份。
        if cookie and "sessionid" not in cookie:
            log.warning("读到的 cookie 里没有 sessionid（未登录），不采用")
            cookie = ""
        with self._lock:
            changed = bool(cookie) and cookie != self._cookie
            if cookie:
                self._cookie = cookie
                self._fetched_at = time.time()
            elif self._cookie:
                # 这次没读到，但之前有 —— 保留旧的，别把能用的也丢了
                log.warning("这次没读到登录态 cookie，继续用上一次读到的")
            else:
                self._fetched_at = 0.0
        if changed:
            log.info(f"登录态 cookie 已更新（{len(cookie.split(';'))} 个字段）")
        return self.get()

    # ---------- 内部 ----------

    def _harvest(self) -> str:
        """从 profile 副本里读出明文 cookie（只读，不碰原 profile）。"""
        try:
            from playwright.sync_api import sync_playwright

            from . import browser_identity
            from .profile_seed import seed_profile
        except Exception as e:
            log.warning(f"准备 cookie 采集环境失败: {e}")
            return ""

        work = self.base_dir / "harvest"
        try:
            if not seed_profile(self.profile_dir, work):
                return ""
        except Exception as e:
            log.warning(f"复制 profile 失败: {e}")
            return ""

        channel, _app = browser_identity.pick()
        kwargs = {
            "user_data_dir": str(work),
            "headless": True,
            "viewport": {"width": 1400, "height": 900},
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if channel:
            kwargs["channel"] = channel

        try:
            with sync_playwright() as p:
                try:
                    ctx = p.chromium.launch_persistent_context(**kwargs)
                except Exception as e:
                    log.warning(f"用 {channel or '自带Chromium'} 打开 profile 副本失败: {e}")
                    return ""
                try:
                    cookies = ctx.cookies()
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"读 cookie 失败: {e}")
            return ""

        parts: list[str] = []
        for c in cookies:
            name = c.get("name") or ""
            dom = (c.get("domain") or "").lstrip(".")
            if name in _EXCLUDE_NAMES:
                continue
            if not (dom.endswith("douyin.com") or dom.endswith("douyinstatic.com")):
                continue
            parts.append(f"{name}={c.get('value') or ''}")

        cookie = "; ".join(parts)
        if "sessionid" not in cookie:
            log.warning("读到的 cookie 里没有 sessionid（未登录？），不采用")
            return ""
        log.info(f"从 {self.profile_dir} 读到登录态 cookie（{len(parts)} 个字段）")
        return cookie


_provider: Optional[LoggedInCookieProvider] = None
_provider_lock = threading.Lock()


def get_provider() -> LoggedInCookieProvider:
    """全局单例（第一次调用时按配置构造）。"""
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                try:
                    from ..config import get_config
                    cfg = get_config()
                    det = getattr(cfg, "live_detection", None)
                    src = (getattr(det, "profile_source", "") or "").strip() \
                        or (getattr(cfg.outreach, "profile_dir", "") or "data/outreach_profile")
                    ttl = int(getattr(det, "cookie_refresh_minutes", 15) or 15)
                except Exception:
                    src = "data/outreach_profile"
                    ttl = 15
                _provider = LoggedInCookieProvider(src, "data/detect_profiles", ttl)
    return _provider


def apply_to_config(cookie: str) -> bool:
    """把 cookie 写回 config（重启后依然有效）。返回是否真的改了。"""
    if not cookie:
        return False
    try:
        from ..config import get_config, save_config
        cfg = get_config()
        if (cfg.douyin.cookie or "") == cookie:
            return False
        cfg.douyin.cookie = cookie
        cfg.douyin_video.cookie = cookie
        save_config(cfg)
        log.info("登录态 cookie 已写回 config")
        return True
    except Exception as e:
        log.warning(f"写回 config 失败: {e}")
        return False
