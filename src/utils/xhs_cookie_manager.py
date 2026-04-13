"""小红书 Cookie 管理器 — 通过 Playwright 持久化浏览器获取"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import get_config, reload_config, save_config
from ..utils.logger import get_logger

log = get_logger("xhs_cookie_manager")


def _get_cookies_from_context(context) -> str:
    """从 Playwright context 取所有 XHS cookie"""
    try:
        cookies = context.cookies(["https://www.xiaohongshu.com/"])
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    except Exception as e:
        log.warning(f"取 XHS cookie 失败: {e}")
        return ""


def save_xhs_cookie(cookie: str) -> None:
    """将 XHS Cookie 写入 config.yaml"""
    config = get_config()
    config.xhs.cookie = cookie
    save_config(config)
    reload_config()
    log.info("XHS Cookie 已保存到 config.yaml")


def fetch_xhs_cookie_by_browser() -> str:
    """
    通过 Playwright headful 浏览器获取 XHS Cookie。
    优先使用已登录状态（browser_data 持久化），否则等待扫码登录。
    """
    from playwright.sync_api import sync_playwright

    user_data_dir = Path("data/xhs_browser_data")
    user_data_dir.mkdir(parents=True, exist_ok=True)

    cookie_str = ""

    def _fetch():
        nonlocal cookie_str
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,
                viewport={"width": 1400, "height": 900},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )

            page = context.pages[0] if context.pages else context.new_page()

            log.info("打开小红书首页...")
            page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            initial_cookie = _get_cookies_from_context(context)
            has_session = "web_session" in initial_cookie
            has_a1 = "a1" in initial_cookie
            log.info(f"初始 Cookie: web_session={'有' if has_session else '无'}, a1={'有' if has_a1 else '无'}")

            if has_session and has_a1:
                # 已有登录，直接获取最新 cookie
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                cookie_str = _get_cookies_from_context(context)
                log.info(f"刷新后 Cookie 长度: {len(cookie_str)}")
            else:
                # 等待扫码登录
                log.info("=" * 60)
                log.info("请在浏览器中扫码登录小红书")
                log.info("登录后程序自动继续...")
                for i in range(120):
                    page.wait_for_timeout(1000)
                    c = _get_cookies_from_context(context)
                    if "web_session" in c and "a1" in c:
                        log.info(f"✅ 第 {i+1} 秒检测到登录，Cookie 长度: {len(c)}")
                        page.wait_for_timeout(5000)
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=15000)
                            page.wait_for_timeout(3000)
                            cookie_str = _get_cookies_from_context(context)
                        except Exception:
                            cookie_str = c
                        break
                    if i == 119:
                        log.error("登录超时（120秒）")
                        raise RuntimeError("小红书登录超时")

            # 打印关键字段
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" in part:
                    name = part.split("=")[0].strip()
                    if name in ["web_session", "a1", "webId", "unread"]:
                        val = part.split("=", 1)[1]
                        log.info(f"  ✓ {name}: {val[:30]}...")

            context.close()

    _fetch()

    if not cookie_str or "web_session" not in cookie_str or "a1" not in cookie_str:
        raise RuntimeError("未获取到有效小红书 Cookie（缺少 web_session 或 a1）")

    return cookie_str
