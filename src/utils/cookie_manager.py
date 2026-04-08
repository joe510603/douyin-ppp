"""Cookie 管理器 — 检测状态、自动刷新、持久化"""

from __future__ import annotations

import re
import time
from typing import Optional

import httpx

from ..config import get_config, reload_config, save_config
from ..utils.logger import get_logger

log = get_logger("cookie_manager")

# 抖音用户信息 API（用于验证 Cookie 是否有效）
USER_INFO_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"


def get_cookie_status(cookie: str) -> str:
    """
    检测 Cookie 状态。

    Args:
        cookie: 完整的 Cookie 字符串

    Returns:
        状态字符串：valid / expiring_soon / expired
    """
    if not cookie:
        log.warning("Cookie 为空")
        return "expired"

    # 1. 检查 ttwid 是否存在
    ttwid_match = re.search(r'ttwid=([^;]+)', cookie)
    if not ttwid_match:
        log.warning("Cookie 中未找到 ttwid")
        return "expired"

    ttwid_value = ttwid_match.group(1)

    # 2. 解析 ttwid 中的过期时间（URL 解码后再解析）
    try:
        from urllib.parse import unquote
        decoded = unquote(ttwid_value)
        # ttwid 格式：1|时间戳|签名|版本
        parts = decoded.split('|')
        if len(parts) >= 2:
            expire_ts = int(parts[1])
            now_ts = int(time.time())
            remaining_hours = (expire_ts - now_ts) / 3600
            log.info(f"ttwid 过期时间剩余约 {remaining_hours:.1f} 小时")

            config = get_config()
            warn_hours = config.douyin.cookie_expire_warning_hours
            if remaining_hours <= 0:
                return "expired"
            elif remaining_hours <= warn_hours:
                return "expiring_soon"
            else:
                # 3. 调用 API 验证有效性
                if _verify_cookie_via_api(cookie):
                    return "valid"
                else:
                    log.warning("Cookie 已失效（API 验证失败）")
                    return "expired"
    except Exception as e:
        log.warning(f"解析 ttwid 过期时间失败: {e}，回退到 API 验证")

    # 回退：直接 API 验证
    if _verify_cookie_via_api(cookie):
        return "valid"
    else:
        return "expired"


def _verify_cookie_via_api(cookie: str) -> bool:
    """
    通过调用抖音 API 验证 Cookie 是否有效。

    Returns:
        True 表示有效，False 表示无效
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.douyin.com/",
            "Cookie": cookie,
        }

        params = {
            "sec_user_id": " ",  # 故意传空，API 会返回 401 而非数据
            "device_platform": "webapp",
            "aid": "6383",
        }

        resp = httpx.get(USER_INFO_API, headers=headers, params=params, timeout=10, follow_redirects=True)
        if resp.status_code == 401:
            log.warning("Cookie 验证失败：HTTP 401")
            return False

        data = resp.json()
        status_code = data.get("status_code", -1)
        if status_code == 0:
            return True
        else:
            log.warning(f"Cookie 验证失败：status_code={status_code}")
            return False

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            log.warning("Cookie 已失效：HTTP 401")
            return False
        log.error(f"Cookie 验证请求错误: {e}")
        return False
    except Exception as e:
        log.error(f"Cookie 验证异常: {e}")
        return False


def save_cookie(cookie: str) -> None:
    """
    将 Cookie 写入 config.yaml 并重新加载配置。

    Args:
        cookie: 完整的 Cookie 字符串
    """
    config = get_config()
    config.douyin.cookie = cookie
    save_config(config)
    reload_config()
    log.info("Cookie 已保存到 config.yaml 并重新加载配置")


def fetch_cookie_by_browser() -> str:
    """
    通过 Playwright headful 方式获取抖音 Cookie。

    启动 Chromium（headful=True），使用持久化的用户数据目录，
    这样登录状态会被保存，下次打开时不需要重新登录。
    
    打开抖音登录页，等待用户登录完成后从 document.cookie 中提取 ttwid，
    最后返回完整 Cookie 字符串并写入 config.yaml。

    Returns:
        完整 Cookie 字符串

    Raises:
        RuntimeError: Playwright 不可用或用户未完成登录
    """
    from pathlib import Path
    
    log.info("启动 Playwright headful 浏览器获取 Cookie...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright 未安装，请运行: pip install playwright && playwright install chromium")

    cookie_str = ""
    
    # 使用持久化的用户数据目录（保存在项目 data 目录下）
    user_data_dir = Path("data/browser_data")
    user_data_dir.mkdir(parents=True, exist_ok=True)
    
    log.info(f"使用浏览器数据目录: {user_data_dir.absolute()}")

    def _fetch():
        nonlocal cookie_str
        from urllib.parse import unquote

        with sync_playwright() as p:
            # 使用持久化上下文，这样登录状态会被保存
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,  # headful，用户可见
                viewport={'width': 1280, 'height': 800},
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                ]
            )

            page = context.pages[0] if context.pages else context.new_page()

            log.info("打开抖音登录页...")
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # 检查是否已经登录（ttwid 已存在）
            current_cookie = page.evaluate("document.cookie")
            log.info(f"初始 Cookie 检查: {'包含 ttwid' if 'ttwid' in current_cookie else '不包含 ttwid'}")
            
            if 'ttwid' in current_cookie:
                log.info("✅ 检测到已有登录状态，刷新页面以获取完整 Cookie...")
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                cookie_str = page.evaluate("document.cookie")
                log.info(f"刷新后 Cookie 长度: {len(cookie_str)}")
            else:
                # 等待用户登录
                log.info("⏳ 等待用户登录（检测 ttwid Cookie）...")
                log.info("请在浏览器中完成登录，登录成功后程序会自动继续")
                try:
                    # 轮询检测 ttwid，每秒检查一次，最多等2分钟
                    max_wait = 120
                    for i in range(max_wait):
                        page.wait_for_timeout(1000)
                        current_cookie = page.evaluate("document.cookie")
                        if 'ttwid' in current_cookie:
                            log.info(f"✅ 第 {i+1} 秒检测到 ttwid，用户已登录")
                            # 再等2秒确保其他 Cookie 也写入
                            page.wait_for_timeout(2000)
                            # 刷新页面获取完整 Cookie
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(2000)
                            cookie_str = page.evaluate("document.cookie")
                            break
                    else:
                        # 超时
                        current_cookie = page.evaluate("document.cookie")
                        if 'ttwid' not in current_cookie:
                            page.close()
                            context.close()
                            raise RuntimeError("登录超时（等待120秒），未检测到 ttwid，请重试")
                        cookie_str = current_cookie
                except Exception as e:
                    current_cookie = page.evaluate("document.cookie")
                    if 'ttwid' not in current_cookie:
                        page.close()
                        context.close()
                        raise RuntimeError(f"登录过程出错: {e}")
                    cookie_str = current_cookie

            log.info(f"获取到 Cookie，长度: {len(cookie_str)}")

            page.close()
            context.close()

    _fetch()

    if not cookie_str or 'ttwid' not in cookie_str:
        raise RuntimeError("未能获取到有效 Cookie")

    # 保存
    save_cookie(cookie_str)
    return cookie_str
