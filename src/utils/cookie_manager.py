"""Cookie 管理器 — 检测状态、自动刷新、持久化"""

from __future__ import annotations

import re
import time
from pathlib import Path
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

    # 1. 检查 ttwid 是否存在（抖音必需）
    ttwid_match = re.search(r'ttwid=([^;]+)', cookie)
    if not ttwid_match:
        # 对于小红书，检查 web_session
        web_session_match = re.search(r'web_session=([^;]+)', cookie)
        if not web_session_match:
            log.warning("Cookie 中未找到 ttwid 或 web_session")
            return "expired"
        # 小红书：检查 a1 和 web_session 即可
        a1_match = re.search(r'a1=([^;]+)', cookie)
        if a1_match:
            log.info("小红书 Cookie 检测：包含关键字段 a1 和 web_session")
            return "valid"
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
                return "valid"
    except Exception as e:
        # 新版 ttwid 使用 base64 格式，不再是 "1|时间戳|签名" 格式，
        # 直接认为有效（不再打印 warning 以避免日志刷屏）
        return "valid"


def _convert_str_cookie(cookie_str: str) -> list[dict]:
    """将 cookie 字符串转换为 Playwright 格式"""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": ".douyin.com",
                "path": "/",
            })
    return cookies


def _capture_search_signature(context, keyword: str = "美食") -> dict:
    """
    用 Playwright 拦截 Douyin 搜索 API 请求，提取 uifid / msToken / a_bogus 等签名参数。
    这些值在同一个浏览器会话内有效，后续请求可以直接复用。
    """
    captured: dict = {}

    def on_request(request):
        url = request.url
        if '/aweme/v1/web/general/search/stream' in url:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            qs = parse_qs(parsed.query, keep_blank_values=True)
            captured['url'] = url
            captured['uifid'] = qs.get('uifid', [''])[0]
            captured['msToken'] = qs.get('msToken', [''])[0]
            captured['a_bogus'] = qs.get('a_bogus', [''])[0]
            captured['aid'] = qs.get('aid', ['6383'])[0]
            captured['params_str'] = parsed.query
            log.info(f"拦截到搜索签名: uifid={captured['uifid'][:20]}..., msToken={captured['msToken'][:20]}...")

    context.on('request', on_request)

    # 打开搜索页触发 API 请求
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(f"https://www.douyin.com/search/{keyword}?type=general",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)
    except Exception as e:
        log.warning(f"拦截签名时页面打开失败: {e}")

    context.remove_listener('request', on_request)
    return captured


def _save_search_signature(sig: dict) -> None:
    """保存搜索签名到缓存文件"""
    if not sig:
        return
    cache_path = Path("data/search_signature_cache.json")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import json
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(sig, f, ensure_ascii=False)
        log.info(f"搜索签名已缓存到 {cache_path}")
    except Exception as e:
        log.warning(f"保存搜索签名失败: {e}")


def _load_search_signature() -> dict:
    """从缓存加载搜索签名"""
    cache_path = Path("data/search_signature_cache.json")
    if not cache_path.exists():
        return {}
    try:
        import json
        with open(cache_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _get_cookies_from_context(page, context) -> str:
    """从 Playwright context 取所有域名为 .douyin.com 的 cookie，转成字符串"""
    try:
        cookies = context.cookies(["https://www.douyin.com/"])
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    except Exception as e:
        log.warning(f"context.cookies() 失败: {e}")
    # 降级：尝试 document.cookie
    try:
        return page.evaluate("document.cookie")
    except Exception:
        return ""


def save_cookie(cookie: str) -> None:
    """将 Cookie 写入 config.yaml 并重新加载配置。"""
    config = get_config()
    config.douyin.cookie = cookie
    save_config(config)
    reload_config()
    log.info("Cookie 已保存到 config.yaml 并重新加载配置")


def fetch_cookie_by_browser() -> str:
    """
    通过 Playwright headful 方式获取抖音 Cookie。

    启动 Chromium（headful=True），使用持久化的用户数据目录。
    如果已有登录状态则直接刷新获取；如果没有则等待用户扫码登录。

    Returns:
        完整 Cookie 字符串

    Raises:
        RuntimeError: Playwright 不可用或用户未完成登录
    """
    from playwright.sync_api import sync_playwright

    log.info("启动 Playwright headful 浏览器获取 Cookie...")

    try:
        import playwright
        _ = playwright.sync_api  # 验证可用
    except ImportError:
        raise RuntimeError("Playwright 未安装，请运行: pip install playwright && playwright install chromium")

    cookie_str = ""

    # 使用持久化的用户数据目录（保存在项目 data 目录下）
    user_data_dir = Path("data/browser_data")
    user_data_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"使用浏览器数据目录: {user_data_dir.absolute()}")

    def _fetch():
        nonlocal cookie_str

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            # 使用持久化上下文，这样登录状态会被保存，下次打开时不需要重新登录
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=False,  # headful，用户可见
                viewport={"width": 1400, "height": 900},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

            page = context.pages[0] if context.pages else context.new_page()

            log.info("打开抖音首页...")
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # 检查初始状态：是否已有登录
            initial_cookie = _get_cookies_from_context(page, context)
            has_ttwid = "ttwid" in initial_cookie
            log.info(f"初始 Cookie 检查: {'已登录（有 ttwid）' if has_ttwid else '未登录'}")

            if has_ttwid:
                # 已有登录状态，直接刷新获取最新 Cookie
                log.info("检测到已有登录状态，刷新页面获取最新 Cookie...")
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                cookie_str = _get_cookies_from_context(page, context)
                log.info(f"刷新后 Cookie 长度: {len(cookie_str)}")
            else:
                # 等待用户登录
                log.info("=" * 60)
                log.info("📱 请在浏览器中扫码登录抖音")
                log.info("   - 如出现验证码，请手动完成验证")
                log.info("   - 登录成功后，程序会自动继续")
                log.info("   - 最多等待 120 秒，超时将退出")
                log.info("=" * 60)

                # 轮询检测多种登录成功标志
                max_wait = 120
                login_detected = False
                for i in range(max_wait):
                    page.wait_for_timeout(1000)
                    try:
                        # 同时检查 context.cookies 和 localStorage
                        c = _get_cookies_from_context(page, context)
                        try:
                            ls = page.evaluate("() => localStorage.getItem('HasUserLogin')")
                        except Exception:
                            ls = None

                        if "ttwid" in c:
                            log.info(f"✅ 第 {i+1} 秒检测到 ttwid，登录成功！")
                            page.wait_for_timeout(3000)
                            cookie_str = _get_cookies_from_context(page, context)
                            login_detected = True
                            break
                        elif ls == "1":
                            log.info(f"✅ 第 {i+1} 秒检测到 localStorage HasUserLogin=1，登录成功！")
                            # HasUserLogin=1 出现时页面可能还在跳转/设置 Cookie
                            # 等 5 秒让页面稳定，再刷新一次强制触发 Cookie 写入
                            page.wait_for_timeout(5000)
                            try:
                                page.reload(wait_until="domcontentloaded", timeout=15000)
                                page.wait_for_timeout(5000)
                                cookie_str = _get_cookies_from_context(page, context)
                            except Exception as e:
                                log.warning(f"reload 后取 cookie 异常: {e}")
                                cookie_str = ""
                            if cookie_str and "ttwid" in cookie_str:
                                log.info("刷新后成功获取 Cookie")
                            else:
                                # 再等几秒尝试（有些 Cookie 写入较慢）
                                for retry_i in range(5):
                                    page.wait_for_timeout(2000)
                                    cookie_str = _get_cookies_from_context(page, context)
                                    if cookie_str and "ttwid" in cookie_str:
                                        log.info(f"第 {retry_i+1} 次重试后成功获取 Cookie")
                                        break
                            login_detected = True
                            break
                    except Exception:
                        # 页面可能在导航/重定向中，等一等再试
                        try:
                            page.wait_for_timeout(2000)
                            cookie_str = _get_cookies_from_context(page, context)
                            if "ttwid" in cookie_str:
                                login_detected = True
                                break
                            # 尝试等待页面稳定后刷新
                            page.reload(wait_until="domcontentloaded", timeout=15000)
                            page.wait_for_timeout(3000)
                            cookie_str = _get_cookies_from_context(page, context)
                            if "ttwid" in cookie_str:
                                login_detected = True
                                break
                        except Exception:
                            pass

                if not login_detected or not cookie_str or "ttwid" not in cookie_str:
                    context.close()
                    raise RuntimeError(
                        "登录超时（120秒内未检测到登录成功）。"
                        "请确保：1) 扫码成功 2) 页面完成跳转 3) 没有被验证码拦截"
                    )

            log.info(f"获取到 Cookie，长度: {len(cookie_str)}")

            # 拦截搜索 API 签名参数（uifid / a_bogus / msToken）
            log.info("拦截搜索 API 签名参数...")
            sig = _capture_search_signature(context, keyword="美食")
            if sig:
                _save_search_signature(sig)
                log.info("签名拦截完成")
            else:
                log.warning("未能拦截到签名参数，搜索功能可能受限")

            # 打印关键 Cookie
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" in part:
                    name = part.split("=")[0].strip()
                    if name in [
                        "sessionid", "ttwid", "__ac_nonce", "__ac_signature",
                        "sid_tt", "uid_tt", "sid_guard", "LOGIN_STATUS",
                        "s_v_web_id", "passport_csrf_token",
                    ]:
                        val = part.split("=", 1)[1]
                        log.info(f"  ✓ {name}: {val[:30]}...")

            page.close()
            context.close()

    _fetch()

    if not cookie_str or "ttwid" not in cookie_str:
        raise RuntimeError("未能获取到有效 Cookie（未包含 ttwid）")

    return cookie_str
