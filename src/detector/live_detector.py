"""开播状态检测器 — 调用抖音 API 检测账号是否正在直播"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from ..config import get_config
from ..models.comment import MonitorAccount, MessageType
from src.utils.logger import get_logger
from src.utils.cookie_manager import get_cookie_status

log = get_logger("detector")


@dataclass
class LiveRoomInfo:
    """直播间信息"""
    room_id: str = ""
    status: bool = False              # 是否在播
    title: str = ""                   # 直播标题
    user_count: int = 0               # 在线人数
    total_user: int = 0               # 累计观看数
    anchor_nickname: str = ""         # 主播昵称
    anchor_sec_id: str = ""           # 主播 sec_user_id
    stream_url: str = ""              # 推流地址
    raw_data: dict = field(default_factory=dict)
    user_unique_id: str = ""          # 主播 user_unique_id（数值ID，WebSocket 连接需要）


class LiveDetector:
    """
    抖音直播间开播状态检测器。
    
    通过调用抖音 Web API 检测指定用户是否正在直播。
    支持定时轮询和单次检测。
    """
    
    # 抖音用户信息 API - PC Web 端（已失效，改用移动端 API）
    USER_INFO_API = "https://api.amemv.com/aweme/v1/user/"
    USER_INFO_API_WEB = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
    # 直播间状态 API（webcast.douyin.com 已废弃，改用 live.douyin.com）
    LIVE_ROOM_API = "https://live.douyin.com/webcast/room/web/enter/"

    # 移动端 User-Agent
    MOBILE_UA = "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.91 Mobile Safari/537.36"
    # PC Web User-Agent
    WEB_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    def _get_effective_cookie(self) -> str:
        """获取有效的 Cookie，优先使用有 ttwid 的"""
        config = get_config()
        douyin_cookie = config.douyin.cookie or ""
        video_cookie = config.douyin_video.cookie or ""

        # 检查 douyin.cookie 是否有 ttwid
        if "ttwid" in douyin_cookie:
            return douyin_cookie
        # 回退到 douyin_video.cookie
        if "ttwid" in video_cookie:
            return video_cookie
        # 都没有，返回 douyin_cookie（可能格式不同）
        return douyin_cookie or video_cookie

    async def _get_client(self, mobile: bool = False) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            cookie = self._get_effective_cookie()
            headers = {
                "User-Agent": self.MOBILE_UA if mobile else self.WEB_UA,
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json",
            }
            if cookie:
                headers["Cookie"] = cookie

            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=15,
                follow_redirects=True,
            )
        return self._client
    
    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
    
    async def detect_by_sec_user_id(self, sec_user_id: str) -> LiveRoomInfo:
        """
        通过 sec_user_id 检测是否在播。

        Args:
            sec_user_id: 用户 sec_user_id

        Returns:
            LiveRoomInfo 直播间信息
        """
        info = LiveRoomInfo(anchor_sec_id=sec_user_id)

        # 检查 Cookie 状态
        cookie = self._get_effective_cookie()
        cookie_status = get_cookie_status(cookie)
        if cookie_status == "expired":
            log.warning(f"Cookie 已失效，检测结果可能不准确")
        elif cookie_status == "expiring_soon":
            log.warning(f"Cookie 即将在 24h 内过期，检测结果可能不准确")

        # 记录 API 是否返回了有效数据
        api_has_nickname = False
        api_has_room_id = False

        # 尝试移动端 API
        await self._try_mobile_api(sec_user_id, info)
        if info.anchor_nickname:
            api_has_nickname = True
        if info.room_id:
            api_has_room_id = True

        # 如果移动端也没数据，尝试 PC Web API
        if not info.anchor_nickname:
            await self._try_web_api(sec_user_id, info)
            if info.anchor_nickname:
                api_has_nickname = True
            if info.room_id:
                api_has_room_id = True

        # 【核心修复】强制浏览器检测的条件：
        # 1. API 没拿到 nickname，或者
        # 2. 检测到直播但没有 room_id（说明 API 只检测到状态，没拿到 room_id）
        # 注意：不再依赖 cookie 的 stale live_debug_info
        need_browser = (not api_has_nickname) or (info.status and not api_has_room_id)
        if need_browser:
            log.info(f"[{sec_user_id[:15]}...] {'API无昵称' if not api_has_nickname else 'API无room_id'}，强制浏览器检测")
            await self._try_browser_detection(sec_user_id, info)

        # 如果上述都没拿到 room_id，最后手段：尝试从 cookie live_debug_info 提取
        # 仅当 room_id 仍然为空时才使用（这是最不可靠的回退）
        if info.status and not info.room_id:
            import re
            import urllib.parse
            config = get_config()
            douyin_cookie = config.douyin.cookie or ""
            video_cookie = config.douyin_video.cookie or ""
            for cookie_name, cookie_val in [("douyin", douyin_cookie), ("video", video_cookie)]:
                if not cookie_val or "live_debug_info" not in cookie_val:
                    continue
                try:
                    live_debug_match = re.search(r'live_debug_info=([^;]+)', cookie_val)
                    if live_debug_match:
                        live_debug_decoded = urllib.parse.unquote(urllib.parse.unquote(live_debug_match.group(1)))
                        room_id_match = re.search(r'"roomId"\s*:\s*"?(\d{18,20})"?', live_debug_decoded)
                        if room_id_match:
                            info.room_id = room_id_match.group(1)
                            log.info(f"[Cookie-Fallback] 从 {cookie_name}.cookie 提取 room_id={info.room_id}（⚠️可能为历史数据）")
                            break
                        else:
                            href_match = re.search(r'live\.douyin\.com/(\d{10,15})', live_debug_decoded)
                            if href_match:
                                info.room_id = href_match.group(1)
                                log.info(f"[Cookie-Fallback] 从 {cookie_name}.cookie 提取 href room_id={info.room_id}（⚠️可能为历史数据）")
                                break
                except Exception as e:
                    log.debug(f"[Cookie] 提取 room_id 失败: {e}")

        return info

    async def _try_mobile_api(self, sec_user_id: str, info: LiveRoomInfo) -> None:
        """尝试移动端 API 获取直播状态"""
        try:
            client = await self._get_client(mobile=True)

            params = {
                "sec_user_id": sec_user_id,
                "device_platform": "android",
                "aid": "6383",
            }

            resp = await client.get(self.USER_INFO_API, params=params)

            if resp.status_code == 200 and resp.text:
                data = resp.json()
                log.debug(f"[{sec_user_id[:15]}...] 移动端 API 响应: {str(data)[:200]}")

                if data.get("status_code") == 0:
                    user_data = data.get("user_info") or data.get("user") or {}
                    if user_data:
                        info.anchor_nickname = user_data.get("nickname", "")

                        # 移动端直播状态检测
                        if user_data.get("is_living") == 1:
                            info.status = True
                            info.room_id = str(user_data.get("room_id", ""))
                            info.title = user_data.get("live_title", "")
                            info.user_count = user_data.get("online_user_count", 0)
                            log.info(f"[移动端] 检测到开播 [{info.anchor_nickname}]: {info.title}")

                        # 备用：检查 room_id 字段
                        if not info.status and user_data.get("room_id"):
                            room_id = user_data.get("room_id")
                            if str(room_id).isdigit() and int(room_id) > 0:
                                info.status = True
                                info.room_id = str(room_id)
                                log.info(f"[移动端] 检测到开播 [{info.anchor_nickname}]: room_id={room_id}")
        except Exception as e:
            log.debug(f"移动端 API 检测失败: {e}")

    async def _try_web_api(self, sec_user_id: str, info: LiveRoomInfo) -> None:
        """尝试 PC Web API 和直播间 API"""
        try:
            client = await self._get_client(mobile=False)

            # 优先尝试直播间 API（更可靠）
            try:
                params = {
                    "aid": "6383",
                    "app_name": "douyin_web",
                    "live_id": "1",
                    "sec_user_id": sec_user_id,
                }
                
                resp = await client.get(self.LIVE_ROOM_API, params=params)
                
                if resp.status_code == 200 and resp.text:
                    data = resp.json()
                    log.debug(f"[{sec_user_id[:15]}...] 直播间 API 响应: {str(data)[:300]}")
                    
                    # 检查直播间数据
                    room_data_list = data.get("data", [])
                    if room_data_list and len(room_data_list) > 0:
                        room = room_data_list[0]
                        room_status = room.get("status", 0)
                        
                        if room_status == 2:  # 2 = 直播中
                            info.status = True
                            info.room_id = str(room.get("room_id", ""))
                            info.title = room.get("title", "")
                            info.user_count = room.get("user_count", 0)
                            info.total_user = room.get("total_user", 0)
                            info.raw_data = room
                            
                            # 尝试获取主播昵称
                            owner = room.get("owner", {})
                            info.anchor_nickname = owner.get("nickname", "")
                            
                            log.info(f"[直播间API] 检测到开播 [{info.anchor_nickname}]: {info.title} (在线 {info.user_count} 人)")
                            return  # 成功获取，直接返回
            except Exception as e:
                log.debug(f"直播间 API 调用失败: {e}")

            # 回退：尝试用户信息 API
            params = {
                "sec_user_id": sec_user_id,
                "device_platform": "webapp",
                "aid": "6383",
                "cookie_enabled": "true",
            }

            resp = await client.get(self.USER_INFO_API_WEB, params=params)

            if resp.status_code == 200 and resp.text:
                data = resp.json()

                if data.get("status_code") == 0 and data.get("user"):
                    user_data = data["user"]
                    info.anchor_nickname = user_data.get("nickname", "")

                    # 检查 room_data
                    room_data_str = user_data.get("room_data")
                    if room_data_str:
                        try:
                            room_data = json.loads(room_data_str) if isinstance(room_data_str, str) else room_data_str
                        except json.JSONDecodeError:
                            room_data = None
                    else:
                        room_data = None
                        for key in ["stream_room", "live_room", "room"]:
                            if key in user_data and user_data[key]:
                                room_data = user_data[key]
                                break

                    if room_data and isinstance(room_data, dict):
                        room_status = room_data.get("status", 0)
                        if room_status == 2:
                            info.status = True
                            info.room_id = str(room_data.get("id_str", room_data.get("room_id", "")))
                            info.title = room_data.get("title", "")
                            info.user_count = room_data.get("user_count", 0)
                            info.raw_data = room_data
                            log.info(f"[Web端] 检测到开播 [{info.anchor_nickname}]: {info.title}")
        except Exception as e:
            log.debug(f"Web API 检测失败: {e}")

    async def _try_browser_detection(self, sec_user_id: str, info: LiveRoomInfo) -> None:
        """通过 Playwright 打开用户主页，提取直播状态和 room_id"""
        try:
            from playwright.sync_api import sync_playwright
            import re
            import json as json_mod

            log.debug(f"[{sec_user_id[:15]}...] 使用浏览器检测直播状态")

            def _fetch_sync():
                nonlocal info
                browser = None  # 确保变量已定义
                with sync_playwright() as p:
                    # 使用持久化上下文（包含登录状态）
                    try:
                        context = p.chromium.launch_persistent_context(
                            user_data_dir=str(Path("data/browser_data")),
                            headless=True,
                            viewport={"width": 1400, "height": 900},
                            user_agent=self.WEB_UA,
                            args=["--no-sandbox", "--disable-setuid-sandbox",
                                  "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                    except Exception:
                        # 回退：使用独立 browser
                        browser = p.chromium.launch(
                            headless=True,
                            args=["--no-sandbox", "--disable-setuid-sandbox",
                                  "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                        )
                        context = browser.new_context(
                            viewport={"width": 1400, "height": 900},
                            user_agent=self.WEB_UA,
                        )
                        # 注入 Cookie
                        cookie = self._get_effective_cookie()
                        if cookie:
                            from urllib.parse import unquote
                            cookies = []
                            for part in cookie.split(";"):
                                part = part.strip()
                                if "=" in part:
                                    key, value = part.split("=", 1)
                                    cookies.append({
                                        'name': key.strip(),
                                        'value': unquote(value.strip()),
                                        'domain': '.douyin.com',
                                        'path': '/',
                                    })
                            if cookies:
                                try:
                                    context.add_cookies(cookies)
                                except Exception as e:
                                    log.debug(f"设置 cookies 失败: {e}")
                        page = context.new_page()
                        browser = None

                    # 打开用户主页
                    profile_url = f"https://www.douyin.com/user/{sec_user_id}"
                    try:
                        page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(5000)
                    except Exception as e:
                        log.debug(f"打开用户主页失败: {e}")
                        context.close()
                        if browser:
                            browser.close()
                        return

                    # 获取页面内容
                    content = page.content()

                    # 提取昵称 - 多种方式
                    if not info.anchor_nickname:
                        # 方式1: 从 HTML 正则
                        nickname_match = re.search(r'"nickname"\s*:\s*"([^"]+)"', content)
                        if nickname_match:
                            info.anchor_nickname = nickname_match.group(1)
                            log.debug(f"提取到昵称: {info.anchor_nickname}")

                    if not info.anchor_nickname:
                        # 方式2: 从 DOM 获取
                        try:
                            nickname = page.locator('h1, [class*="nickname"], [class*="name"]').first.inner_text(timeout=3000)
                            if nickname and len(nickname) < 50:
                                info.anchor_nickname = nickname
                                log.debug(f"从DOM提取到昵称: {info.anchor_nickname}")
                        except:
                            pass

                    if not info.anchor_nickname:
                        # 方式3: 从 JS 变量获取
                        try:
                            nickname = page.evaluate('''() => {
                                return document.querySelector('h1')?.textContent?.trim() ||
                                       document.querySelector('[class*="nick"]')?.textContent?.trim();
                            }''')
                            if nickname and len(nickname) < 50:
                                info.anchor_nickname = nickname
                                log.debug(f"从JS提取到昵称: {info.anchor_nickname}")
                        except:
                            pass

                    # ========== 核心：从页面 JS 数据中提取 room_id ==========

                    # 方式1: 从 __RENDER_DATA__ 或 SSR 数据中提取
                    room_id = None
                    web_rid = None

                    # 尝试从 RENDER_DATA 提取
                    try:
                        render_data = page.evaluate('''() => {
                            try {
                                if (window.__RENDER_DATA__) return JSON.stringify(window.__RENDER_DATA__);
                            } catch(e) {}
                            try {
                                // 从 script 标签提取
                                const scripts = document.querySelectorAll('script');
                                for (const s of scripts) {
                                    if (s.textContent && s.textContent.includes('room_id')) {
                                        return s.textContent.substring(0, 50000);
                                    }
                                }
                            } catch(e) {}
                            return null;
                        }''')
                        if render_data:
                            # 搜索 room_id（18-19位长数字）
                            room_match = re.search(r'"room_id"\s*:\s*"?(\d{18,})"?', render_data)
                            if room_match:
                                room_id = room_match.group(1)
                            # 搜索 web_rid (直播间页面短ID)
                            web_rid_match = re.search(r'"web_rid"\s*:\s*"?(\d+)"?', render_data)
                            if web_rid_match:
                                web_rid = web_rid_match.group(1)
                            # 搜索 idStr (room_id 的字符串形式)
                            if not room_id:
                                idstr_match = re.search(r'"idStr"\s*:\s*"(\d{18,})"', render_data)
                                if idstr_match:
                                    room_id = idstr_match.group(1)
                            # 检查直播状态
                            status_match = re.search(r'"status"\s*:\s*(\d+)', render_data)
                            if status_match and int(status_match.group(1)) == 2:
                                info.status = True
                    except Exception as e:
                        log.debug(f"从 RENDER_DATA 提取失败: {e}")

                    # 方式2: 从页面 HTML 正则提取 room_id
                    if not room_id:
                        room_id_match = re.search(r'"room_id"\s*:\s*"(\d{18,})"', content)
                        if room_id_match:
                            room_id = room_id_match.group(1)

                    # 方式3: 查找 idStr
                    if not room_id:
                        idstr_match = re.search(r'"idStr"\s*:\s*"(\d{18,})"', content)
                        if idstr_match:
                            room_id = idstr_match.group(1)

                    # 方式4: 尝试从直播卡片链接提取 web_rid
                    if not room_id and not web_rid:
                        try:
                            # 直播卡片通常有链接 live.douyin.com/xxxxxx
                            live_link = page.evaluate('''() => {
                                const links = document.querySelectorAll('a[href*="live.douyin.com"]');
                                for (const a of links) {
                                    const m = a.href.match(/live\\.douyin\\.com\\/(\\d+)/);
                                    if (m) return m[1];
                                }
                                return null;
                            }''')
                            if live_link:
                                web_rid = live_link
                        except:
                            pass

                    # 方式5: 检测直播入口元素（确定在播但可能拿不到 room_id）
                    if not info.status:
                        try:
                            live_banner = page.locator('[class*="live"], [class*="直播"]').first
                            if live_banner.is_visible(timeout=2000):
                                info.status = True
                                log.debug(f"[浏览器] 检测到直播入口元素")
                        except:
                            pass

                    # 方式6: 查找"直播中"文字
                    if not info.status:
                        try:
                            page.wait_for_selector("text=直播中", timeout=3000)
                            info.status = True
                            log.debug(f"[浏览器] 页面包含「直播中」文字")
                        except:
                            pass

                    # 设置 room_id（优先使用真实 room_id，其次 web_rid）
                    # web_rid 是 12 位短 ID，真实 room_id 是 18-19 位长 ID
                    # WebSocket 连接需要真实 room_id 才能正确接收弹幕
                    if room_id and len(room_id) >= 18:
                        info.room_id = room_id
                        info.status = True
                        log.info(f"[浏览器] 提取到真实 room_id={room_id}")
                    elif web_rid:
                        info.status = True
                        # web_rid 需要进一步获取真实 room_id
                        log.info(f"[浏览器] 提取到 web_rid={web_rid}，正在获取真实 room_id...")
                        real_room_id = None
                        
                        # 方式A: 使用 httpx 直接请求（快速，但可能被验证码拦截）
                        try:
                            import httpx as _httpx
                            _headers = {
                                "User-Agent": self.WEB_UA,
                                "Accept": "text/html,application/xhtml+xml",
                                "Accept-Encoding": "gzip, deflate",
                                "Referer": "https://live.douyin.com/",
                            }
                            # 注意：带 Cookie 请求 live.douyin.com 会返回空页面(6KB)，
                            # 不带 Cookie 可能能获取到完整HTML(1MB+)
                            with _httpx.Client(timeout=15, follow_redirects=True) as _client:
                                _resp = _client.get(f"https://live.douyin.com/{web_rid}", headers=_headers)
                                _html = _resp.text
                                if len(_html) > 50000:  # 有效页面通常 > 500KB
                                    # 策略1: web_rid 附近的 roomId
                                    _nearby = re.findall(r'roomId.{0,50}?web_rid.{0,30}?' + re.escape(web_rid), _html)
                                    if _nearby:
                                        _cm = re.search(r'roomId[^\d]*(\d{18,20})', _nearby[0])
                                        if _cm:
                                            real_room_id = _cm.group(1)
                                    # 策略2: 转义格式
                                    if not real_room_id:
                                        _esc = re.findall(r'roomId\\\\\":\\\\\"(\d{18,20})\\\\\"', _html)
                                        if _esc:
                                            real_room_id = _esc[0]
                                    # 策略3: HTML实体格式
                                    if not real_room_id:
                                        _ent = re.findall(r'roomId&quot;:&quot;(\d{18,20})&quot;', _html)
                                        if _ent:
                                            real_room_id = _ent[0]
                                    # 策略4: 回退
                                    if not real_room_id:
                                        _all = re.findall(r'roomId.*?(\d{18,20})', _html)
                                        if _all:
                                            real_room_id = _all[0]
                                    if real_room_id:
                                        log.info(f"[浏览器] httpx 获取到真实 room_id={real_room_id}")
                                else:
                                    log.debug(f"[浏览器] httpx 返回页面太短({len(_html)}B)，可能被验证码拦截")
                        except Exception as e:
                            log.debug(f"[浏览器] httpx 获取失败({e})")
                        
                        # 方式B: httpx 失败时，用 Playwright 直接导航到直播页面
                        if not real_room_id:
                            try:
                                log.info(f"[浏览器] httpx 失败，使用 Playwright 访问直播页面获取 room_id...")
                                live_url = f"https://live.douyin.com/{web_rid}"
                                page.goto(live_url, wait_until='domcontentloaded', timeout=15000)
                                page.wait_for_timeout(3000)  # 等待页面渲染
                                
                                # 从页面 JS 变量提取 room_id
                                live_page_data = page.evaluate('''() => {
                                    try {
                                        // 从 RENDER_DATA 提取
                                        const renderEl = document.getElementById('RENDER_DATA');
                                        if (renderEl) {
                                            const text = decodeURIComponent(renderEl.textContent);
                                            const m = text.match(/"room_id"\\s*:\\s*"?(\d{18,})"?/);
                                            if (m) return m[1];
                                        }
                                        // 从 __NEXT_DATA__ 提取
                                        const nextEl = document.getElementById('__NEXT_DATA__');
                                        if (nextEl) {
                                            const data = JSON.parse(nextEl.textContent);
                                            const rid = data?.props?.pageProps?.room?.room_id || 
                                                       data?.props?.pageProps?.roomInfo?.room?.id_str;
                                            if (rid) return String(rid);
                                        }
                                        // 从 window 变量提取
                                        if (window.__RENDER_DATA__) {
                                            const rd = window.__RENDER_DATA__;
                                            const m2 = JSON.stringify(rd).match(/"room_id"\\s*:\\s*"?(\d{18,})"?/);
                                            if (m2) return m2[1];
                                        }
                                    } catch(e) {}
                                    return null;
                                }''')
                                if live_page_data and len(str(live_page_data)) >= 18:
                                    real_room_id = str(live_page_data)
                                    log.info(f"[浏览器] Playwright 从直播页面获取到真实 room_id={real_room_id}")
                                else:
                                    # 回退：从 HTML 内容提取
                                    live_html = page.content()
                                    _all = re.findall(r'"room_id"\s*:\s*"(\d{18,})"', live_html)
                                    if not _all:
                                        _all = re.findall(r'roomId.*?(\d{18,20})', live_html)
                                    if _all:
                                        real_room_id = _all[0]
                                        log.info(f"[浏览器] Playwright 从直播页面HTML获取到 room_id={real_room_id}")
                                    else:
                                        log.warning(f"[浏览器] Playwright 也未能获取真实 room_id, html_len={len(live_html)}")
                            except Exception as e:
                                log.warning(f"[浏览器] Playwright 访问直播页面失败: {e}")
                        
                        if real_room_id:
                            info.room_id = real_room_id
                        else:
                            info.room_id = web_rid
                            log.warning(f"[浏览器] 未能获取真实 room_id，使用 web_rid={web_rid}")
                    elif room_id:
                        info.room_id = room_id
                        info.status = True
                        log.info(f"[浏览器] 提取到 room_id={room_id}")

                    # 如果检测到直播但没有 room_id，尝试从直播链接获取
                    if info.status and not info.room_id:
                        log.info(f"[浏览器] 检测到直播但无 room_id，尝试从直播链接获取...")
                        found_web_rid = None
                        
                        # 方式1: 从 a 标签获取
                        try:
                            live_link_el = page.locator('a[href*="live.douyin.com"]').first
                            if live_link_el.is_visible(timeout=2000):
                                href = live_link_el.get_attribute('href')
                                if href and 'live.douyin.com' in href:
                                    rid_match = re.search(r'live\.douyin\.com/(\d+)', href)
                                    if rid_match:
                                        found_web_rid = rid_match.group(1)
                                        log.debug(f"[浏览器] 从 a 标签获取到 live_id={found_web_rid}")
                        except:
                            pass

                        # 方式2: 从 JS 获取直播间链接
                        if not found_web_rid:
                            try:
                                found_web_rid = page.evaluate('''() => {
                                    // 查找所有链接
                                    const links = document.querySelectorAll('a[href*="live.douyin.com"]');
                                    for (const a of links) {
                                        const m = a.href.match(/live\\.douyin\\.com\\/(\\d+)/);
                                        if (m) return m[1];
                                    }
                                    return null;
                                }''')
                                if found_web_rid:
                                    log.debug(f"[浏览器] 从 JS 获取到 web_rid={found_web_rid}")
                            except:
                                pass

                        # 方式3: 从页面 HTML 提取所有 live.douyin.com 链接
                        if not found_web_rid:
                            try:
                                live_links = re.findall(r'live\.douyin\.com/(\d{5,})', content)
                                if live_links:
                                    found_web_rid = live_links[0]
                                    log.debug(f"[浏览器] 从 HTML 获取到 web_rid={found_web_rid}")
                            except:
                                pass

                        # 如果获取到了 web_rid，获取真实 room_id
                        # 注意：即使提取到的ID是18-19位，也不能直接当作真实room_id使用
                        # 因为页面上的链接可能是推荐直播间的ID，而非当前用户的
                        if found_web_rid:
                            real_room_id_2 = None
                            
                            # 方式A: httpx 请求（带 Cookie 和签名）
                            try:
                                import httpx as _httpx2
                                from urllib.parse import urlencode
                                
                                # 读取 Cookie
                                config = get_config()
                                cookie = config.douyin.cookie if config and config.douyin else ""
                                
                                _headers2 = {
                                    "User-Agent": self.WEB_UA,
                                    "Accept": "text/html,application/xhtml+xml",
                                    "Accept-Encoding": "gzip, deflate",
                                    "Referer": "https://live.douyin.com/",
                                }
                                
                                # 添加 Cookie 到请求头
                                if cookie:
                                    _headers2["Cookie"] = cookie
                                
                                # 构建基础 URL
                                base_url = f"https://live.douyin.com/{found_web_rid}"
                                
                                # 尝试带签名的请求
                                try:
                                    from ..utils.signer import generate_a_bogus
                                    params_str = f"web_rid={found_web_rid}"
                                    x_bogus = generate_a_bogus(params_str, self.WEB_UA)
                                    if x_bogus:
                                        base_url = f"{base_url}?web_rid={found_web_rid}&X-Bogus={x_bogus}"
                                        log.debug(f"[浏览器] 生成 X-Bogus 签名: {x_bogus[:20]}...")
                                except Exception as sig_e:
                                    log.debug(f"[浏览器] 签名生成失败: {sig_e}")
                                
                                with _httpx2.Client(timeout=15, follow_redirects=True) as _client2:
                                    _resp2 = _client2.get(base_url, headers=_headers2)
                                    _html2 = _resp2.text
                                    if len(_html2) > 50000:
                                        # 从完整HTML提取 roomId
                                        _nearby2 = re.findall(r'roomId.{0,50}?web_rid.{0,30}?' + re.escape(found_web_rid), _html2)
                                        if _nearby2:
                                            _cm = re.search(r'roomId[^\d]*(\d{18,20})', _nearby2[0])
                                            if _cm:
                                                real_room_id_2 = _cm.group(1)
                                        if not real_room_id_2:
                                            _esc2 = re.findall(r'roomId\\\\\":\\\\\"(\d{18,20})\\\\\"', _html2)
                                            if _esc2:
                                                real_room_id_2 = _esc2[0]
                                        if not real_room_id_2:
                                            _all2 = re.findall(r'roomId.*?(\d{18,20})', _html2)
                                            if _all2:
                                                real_room_id_2 = _all2[0]
                                        if real_room_id_2:
                                            log.info(f"[浏览器] httpx 从直播链接获取到真实 room_id={real_room_id_2}")
                                    else:
                                        log.debug(f"[浏览器] httpx 直播链接页面太短({len(_html2)}B)")
                            except Exception as e:
                                log.debug(f"[浏览器] httpx 获取直播链接失败({e})")
                            
                            # 方式B: Playwright 回退
                            if not real_room_id_2:
                                try:
                                    live_url2 = f"https://live.douyin.com/{found_web_rid}"
                                    page.goto(live_url2, wait_until='domcontentloaded', timeout=15000)
                                    page.wait_for_timeout(3000)
                                    live_page_data2 = page.evaluate('''() => {
                                        try {
                                            const renderEl = document.getElementById('RENDER_DATA');
                                            if (renderEl) {
                                                const text = decodeURIComponent(renderEl.textContent);
                                                const m = text.match(/"room_id"\\s*:\\s*"?(\d{18,})"?/);
                                                if (m) return m[1];
                                            }
                                        } catch(e) {}
                                        return null;
                                    }''')
                                    if live_page_data2 and len(str(live_page_data2)) >= 18:
                                        real_room_id_2 = str(live_page_data2)
                                        log.info(f"[浏览器] Playwright 从直播链接页面获取到真实 room_id={real_room_id_2}")
                                except Exception as e:
                                    log.warning(f"[浏览器] Playwright 访问直播链接页面失败: {e}")
                            
                            if real_room_id_2:
                                info.room_id = real_room_id_2
                            else:
                                info.room_id = found_web_rid
                                log.warning(f"[浏览器] 未能获取真实room_id，使用原始ID={found_web_rid}")
                            log.info(f"[浏览器] 从直播链接提取 room_id={info.room_id}")

                    if info.status:
                        log.info(f"[浏览器] 最终结果: status=True, room_id={info.room_id}, nickname={info.anchor_nickname}")
                    else:
                        log.debug(f"[浏览器] 未检测到直播")

                    context.close()
                    if browser:
                        browser.close()

            await asyncio.to_thread(_fetch_sync)

        except ImportError:
            log.debug("未安装 playwright，跳过浏览器检测")
        except Exception as e:
            log.debug(f"浏览器检测失败: {e}")

    async def detect(self, account: MonitorAccount) -> LiveRoomInfo:
        """
        检测单个监控账号的开播状态。
        
        Args:
            account: 监控账号配置
            
        Returns:
            LiveRoomInfo
        """
        return await self.detect_by_sec_user_id(account.sec_user_id)
    
    async def batch_detect(
        self, 
        accounts: list[MonitorAccount],
    ) -> dict[str, LiveRoomInfo]:
        """
        批量检测多个账号。
        
        Args:
            accounts: 监控账号列表
            
        Returns:
            {sec_user_id: LiveRoomInfo}
        """
        tasks = [self.detect(acc) for acc in accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        result_map = {}
        for acc, res in zip(accounts, results):
            if isinstance(res, Exception):
                log.error(f"批量检测异常 [{acc.name}]: {res}")
                result_map[acc.sec_user_id] = LiveRoomInfo(status=False)
            else:
                result_map[acc.sec_user_id] = res
        
        return result_map


# 全局检测器实例
_detector_instance: Optional[LiveDetector] = None


def get_detector() -> LiveDetector:
    """获取全局检测器实例"""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = LiveDetector()
    return _detector_instance


async def close_detector():
    """关闭全局检测器"""
    global _detector_instance
    if _detector_instance:
        await _detector_instance.close()
        _detector_instance = None
