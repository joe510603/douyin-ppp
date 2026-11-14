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
from src.utils import browser_identity
from src.utils.logger import get_logger
from src.utils.cookie_manager import get_cookie_status
from src.utils.profile_seed import SeededProfiles

log = get_logger("detector")


@dataclass
class LiveRoomInfo:
    """直播间信息"""
    room_id: str = ""
    web_rid: str = ""                 # 直播间短ID（用于直播间URL）
    status: bool = False              # 是否在播
    title: str = ""                   # 直播标题
    user_count: int = 0               # 在线人数
    total_user: int = 0               # 累计观看数
    anchor_nickname: str = ""         # 主播昵称
    anchor_sec_id: str = ""           # 主播 sec_user_id
    stream_url: str = ""              # 推流地址
    raw_data: dict = field(default_factory=dict)
    user_unique_id: str = ""          # 主播 user_unique_id（数值ID，WebSocket 连接需要）
    # 页面**真的把这个账号渲染出来了吗**（2026-11-14 加）
    #
    # 为什么需要这个字段：未登录/环境异常时，抖音会把主页降级成「通用壳页」，
    # 壳页里既没有直播间入口、也没有这个账号的信息 —— 这时候「没检测到直播」
    # 和「我们看不见这个账号」长得一模一样，整场直播的弹幕就全丢了，
    # 而日志上一片正常。
    #
    # 所以必须把「看不见」和「没开播」分成两件事：
    #   rendered=False → 检测器自己瞎了（该告警、该修登录态）
    #   rendered=True  + status=False → 这个账号确实没开播（正常）
    rendered: bool = False
    # 接口路径（Web 端用户接口）拿到数据了吗？
    # 接口活着 → 「在播/没开播」这个判断是**权威**的；
    # 接口死了就只剩浏览器在看页面 —— 那是降级状态，认错的概率更高。
    api_ok: bool = False


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
    # 备用：用户主页 API（方案B，用于获取 room_id）
    USER_PROFILE_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"

    # 移动端 User-Agent
    MOBILE_UA = "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.91 Mobile Safari/537.36"
    # PC Web User-Agent
    WEB_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        # 浏览器数据目录（在 __init__ 里解析一次，避免 nested function 作用域问题）
        self._browser_data_dir = str(Path("data/browser_data"))
        # 「登录态副本」管理器（2026-11-14 加）
        #
        # 背景：检测器的 cookie 是**未登录态**，抖音会把主页降级成通用壳页，
        # 壳页里没有直播间入口 → 检测器永远报「未检测到直播」→
        # 采集器永远不启动 → 一整天的弹幕全丢，而日志看不出异常。
        #
        # 修法：把触达那个**真浏览器**的登录态复制一份给检测器用
        # （见 src/utils/profile_seed.py 的实测数据）。
        self._seeded = None
        try:
            from ..config import get_config
            cfg = get_config()
            det_cfg = getattr(cfg, "live_detection", None)
            src = getattr(det_cfg, "profile_source", "") or getattr(cfg.outreach, "profile_dir", "") \
                or "data/outreach_profile"
            slots = int(getattr(det_cfg, "profile_slots", 4) or 4)
            refresh = int(getattr(det_cfg, "seed_refresh_minutes", 60) or 60)
            self._seeded = SeededProfiles(src, "data/detect_profiles", slots, refresh)
        except Exception as e:
            log.warning(f"初始化登录态 profile 管理器失败（将退回旧 profile）: {e}")

    def _get_effective_cookie(self) -> str:
        """获取有效的 Cookie，优先用带 ttwid 的

        【2026-11-14】优先用「从真浏览器 profile 续命来的登录态 cookie」。
        为什么：检测器判开播靠抖音 Web 端用户接口，那个接口**必须登录态** ——
        未登录 cookie 会返回 0 字节的 body，检测器对所有账号一律「未检测到直播」，
        采集器永不启动，一整天的弹幕全丢，而日志上一片正常。
        登录态会过期，所以由 src/utils/loggedin_cookie.py 定期从 Edge profile 续命。
        """
        try:
            from ..utils.loggedin_cookie import get_provider
            fresh = get_provider().get()
            if fresh and "sessionid" in fresh:
                return fresh
        except Exception as e:
            log.debug(f"取登录态 cookie 失败，改用 config 里的: {e}")

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
            # 关闭旧的 client 避免资源泄漏
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
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
        if self._client:
            await self._client.aclose()
            self._client = None
    
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

        # 【2026-11-14】删掉了「Cookie-Fallback」那段兜底
        #
        # 它做的是：从 cookie 里的 live_debug_info 抠出「上次看的直播间的 roomId」
        # 塞给当前账号。这个 hack 有两个要命的问题：
        #
        # 1. **串号**：live_debug_info 记的是**你上次看的那场直播**，
        #    跟当前账号没关系。实测没开播的「成都锦江」拿到了
        #    「成都万象城」的 room_id —— 采集器会连上去，
        #    把**别人直播间的弹幕**记到这个账号名下。
        # 2. **来源不一致**：它读的是 config 里的 cookie，
        #    而真正发请求用的是「登录态 cookie」（见 _get_effective_cookie），
        #    两份 cookie 已经不是同一份了，这个兜底读到的多半是过期数据。
        #
        # 现在 room_id 只有一条来路：**浏览器从直播链接的 query 里直接取**
        # （live.douyin.com/<web_rid>?room_id=<18位>）。
        # 取不到就不启动采集 —— 宁可漏一场，也不能把别人的弹幕算成线索。
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
                            # 【核心修复】移动端合播校验：owner_sec_uid 与查询账号不一致时跳过
                            owner_sec_uid = user_data.get("owner_sec_uid", "")
                            if owner_sec_uid and owner_sec_uid != sec_user_id:
                                log.info(f"[移动端] 检测到团播 room_id={user_data.get('room_id')}, owner={owner_sec_uid[:15]}... != 查询账号，跳过")
                                info.status = False
                                info.room_id = ""
                                return

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
                            owner = room.get("owner", {})
                            owner_sec_uid = owner.get("sec_uid", "") or room.get("owner_sec_uid", "")

                            # 【核心修复】身份校验：合播/连屏时，多个账号的 API 可能返回同一个 room_id
                            # 必须验证 room 的 owner 是否真的属于当前查询的账号
                            if owner_sec_uid and owner_sec_uid != sec_user_id:
                                log.info(f"[直播间API] 检测到团播 room_id={room.get('room_id')}，owner_sec_uid={owner_sec_uid[:15]}... != 查询账号，跳过")
                                return  # 合播房间，不属于自己的直播，跳过

                            info.status = True
                            info.room_id = str(room.get("room_id", ""))
                            info.title = room.get("title", "")
                            info.user_count = room.get("user_count", 0)
                            info.total_user = room.get("total_user", 0)
                            info.raw_data = room

                            # 尝试获取主播昵称
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
                    # API 能拿到这个账号的数据 = 我们「看得见」它（自检依据）
                    info.rendered = True
                    # 接口路径活着 → 「在播/没开播」的判断是权威的
                    info.api_ok = True

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

        # 【方案B备用】如果上述 API 都失败（无 room_id），尝试带 a_bogus 签名的用户主页 API
        # 适用于：API 能获取到用户信息但获取不到 room_id 的情况
        if not info.room_id and info.anchor_nickname:
            try:
                from ..utils.signer import generate_a_bogus

                client = await self._get_client(mobile=False)
                params = {
                    "sec_user_id": sec_user_id,
                    "device_platform": "webapp",
                    "aid": "6383",
                    "cookie_enabled": "true",
                }

                # 生成 a_bogus 签名
                params_str = "&".join(f"{k}={v}" for k, v in params.items())
                a_bogus = generate_a_bogus(params_str, self.WEB_UA)
                if a_bogus:
                    params["a_bogus"] = a_bogus

                resp = await client.get(self.USER_PROFILE_API, params=params)
                if resp.status_code == 200 and resp.text:
                    data = resp.json()
                    if data.get("status_code") == 0 and data.get("user"):
                        user_data = data["user"]
                        # 尝试从 user 字段获取 room_id
                        room_data = user_data.get("room_data") or user_data.get("room")
                        if room_data and isinstance(room_data, dict):
                            if room_data.get("status") == 2:
                                info.status = True
                                info.room_id = str(room_data.get("id_str", room_data.get("room_id", "")))
                                info.title = room_data.get("title", "")
                                log.info(f"[方案B] a_bogus 签名获取到 room_id={info.room_id}")
            except Exception as e:
                log.debug(f"[方案B] a_bogus 签名备用 API 失败: {e}")

    async def _try_browser_detection(self, sec_user_id: str, info: LiveRoomInfo) -> None:
        """通过 Playwright 打开用户主页，提取直播状态和 room_id"""
        try:
            from playwright.sync_api import sync_playwright
            import re
            import json as json_mod

            log.debug(f"[{sec_user_id[:15]}...] 使用浏览器检测直播状态")

            def _fetch_sync():
                nonlocal info
                browser = None
                context = None
                page = None
                with sync_playwright() as p:
                    # 【2026-10-13 fix】必须先注入 config 里的最新 cookie 覆盖 profile，
                    # 否则用 data/browser_data 里失效的 ttwid 打开用户主页会被重定向到首页。
                    # 策略：先尝试持久化上下文（保留浏览器其他状态），启动后立即 add_cookies
                    # 用 config 里的新 cookie 覆盖；持久化失败才回退到独立 browser（手动注入）。
                    effective_cookie = self._get_effective_cookie()
                    inject_cookies = []
                    if effective_cookie:
                        from urllib.parse import unquote
                        for part in effective_cookie.split(";"):
                            part = part.strip()
                            if "=" in part:
                                k, v = part.split("=", 1)
                                inject_cookies.append({
                                    'name': k.strip(),
                                    'value': unquote(v.strip()),
                                    'domain': '.douyin.com',
                                    'path': '/',
                                })

                    # ---- 用哪一份 profile ----
                    #
                    # 优先用「登录态副本」（每个并发一份，互不抢 profile 锁）。
                    # 拿不到就退回旧的 data/browser_data（那个是未登录态，
                    # 页面会降级成壳页 → 看不见直播间，只能当降级方案）。
                    slot_dir = self._seeded.next_dir() if self._seeded else None
                    user_data_dir = str(slot_dir or self._browser_data_dir)

                    # ---- 用哪个浏览器 ----
                    #
                    # 必须用**系统里的 Edge**：profile 里的 cookie 是用
                    # 「Microsoft Edge Safe Storage」（macOS 钥匙串）加密的，
                    # 拿 Playwright 自带的 Chromium 打开只会看到一堆解不开的密文。
                    # 而且指纹要和触达那边一致 —— 同一个账号一会儿 Chromium
                    # 一会儿 Edge，在抖音眼里就是两个人。
                    channel, _app_name = browser_identity.pick()

                    launch_kwargs = {
                        "user_data_dir": user_data_dir,
                        "headless": True,
                        "viewport": {"width": 1400, "height": 900},
                        "args": ["--no-sandbox", "--disable-setuid-sandbox",
                                 "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                    }
                    if channel:
                        launch_kwargs["channel"] = channel

                    # ⚠ 故意**不覆写 User-Agent**。
                    #   以前这里写死 "Chrome/125"，而真正的引擎是 Chromium 153 ——
                    #   Playwright 的 user_agent= 参数只改字符串、不改 Client Hints，
                    #   抖音读一下 userAgentData.brands 就知道你编了
                    #   （理由同 outreach/browser.py 顶部那段注释）。
                    try:
                        context = p.chromium.launch_persistent_context(**launch_kwargs)
                    except Exception as e:
                        if channel:
                            log.warning(f"[浏览器] 用 {channel} 启动失败，退回自带 Chromium: {e}")
                            launch_kwargs.pop("channel", None)
                            context = p.chromium.launch_persistent_context(**launch_kwargs)
                        else:
                            raise
                    log.info(f"[浏览器] 持久化上下文启动成功（{channel or '自带Chromium'}，profile={user_data_dir}），"
                             f"注入 {len(inject_cookies)} 条 cookie 覆盖 profile...")

                    # 必须先访问 domain 一次才能 add_cookies
                    #
                    # ⚠ 只等「响应头」（commit），不等整页资源（2026-11-13）：
                    #   这里唯一的目的就是让 origin 建立起来好注入 cookie，
                    #   等 domcontentloaded 要 15 秒，而检测是 14 个账号并发
                    #   （每个都启一个 Chromium）→ 实测整个检测器吃掉 216% CPU，
                    #   把触达的浏览器挤得搜索从 20 秒变成 60+ 秒。
                    #   改成 commit 后这一步 15 秒 → 1~2 秒。
                    first_page = context.pages[0] if context.pages else context.new_page()
                    try:
                        first_page.goto("https://www.douyin.com/",
                                       wait_until="commit", timeout=15000)
                    except Exception:
                        pass  # 即使失败也继续
                    if inject_cookies:
                        added = False
                        try:
                            context.add_cookies(inject_cookies)
                            added = True
                        except Exception as e:
                            log.warning(f"设置 cookies 失败（等页面加载完再试一次）: {e}")
                        if not added:
                            # 保险起见：等页面真的加载完再试一次
                            # （cookie 注入失败 = 用 profile 里的旧 cookie
                            #   → 可能被重定向到首页 → 误判「没开播」→ 漏掉直播间）
                            try:
                                first_page.wait_for_load_state(
                                    "domcontentloaded", timeout=10000)
                                context.add_cookies(inject_cookies)
                                added = True
                            except Exception as e:
                                log.warning(f"设置 cookies 仍然失败: {e}")
                        if added:
                            log.info(f"[浏览器] ✅ 已注入 {len(inject_cookies)} 条 cookie 覆盖 profile")
                    page = context.pages[0] if context.pages else context.new_page()

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

                    # 【2026-11-14】判断页面**真的把这个账号渲染出来了吗**
                    #
                    # 未登录 / 环境异常时，抖音把主页降级成「通用壳页」——
                    # 壳页里没有这个锚点的任何信息（连他的 sec_uid 都没有）。
                    # 这时候「没检测到直播」和「我们看不见这个账号」是同一件事，
                    # 整场直播的弹幕就全丢了，而日志上一片正常。
                    #
                    # 锚点自己的 sec_uid 会出现在**渲染出来的主页 HTML** 里，
                    # 壳页里没有 —— 这是区分两者的唯一可靠证据。
                    info.rendered = bool(sec_user_id and sec_user_id in content)
                    if not info.rendered:
                        log.info(f"[浏览器] 页面没渲染出该账号（疑似降级/未登录），"
                                 f"不作为「没开播」依据: {sec_user_id[:15]}...")

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

                    # 方式4: 尝试从直播卡片链接提取 web_rid / room_id
                    #
                    # 【2026-11-14 改进】链接的 query 里**直接带 room_id**：
                    #   live.douyin.com/163468593199?...&room_id=7707947305089501961&...
                    # 以前只取短 ID（web_rid），还得再发一次 HTTP 去直播页里刨 room_id；
                    # 现在一次就能拿到真正的 room_id（18-19 位），
                    # 少一次请求、少 3~5 秒，也少一个失败点。
                    if not room_id and not web_rid:
                        try:
                            live_links = page.evaluate('''() => {
                                const out = [];
                                const links = document.querySelectorAll('a[href*="live.douyin.com"]');
                                for (const a of links) {
                                    const m = a.href.match(/live\\.douyin\\.com\\/(\\d+)/);
                                    if (m) out.push({web_rid: m[1], href: a.href});
                                }
                                return out;
                            }''') or []
                            if live_links:
                                web_rid = live_links[0].get("web_rid") or ""
                                rm = re.search(r"room_id=(\d{18,})", live_links[0].get("href", ""))
                                if rm:
                                    room_id = rm.group(1)
                                    log.info(f"[浏览器] 从直播链接直接取到 room_id={room_id}")
                        except Exception as e:
                            log.debug(f"从直播链接提取失败: {e}")

                    # 方式5+6（已删除 - 2026-10-13）：
                    # 之前 detector 用 'page.wait_for_selector("text=直播中")' 当开播判定，
                    # 但抖音用户主页里"直播中"3 个字到处都是（"主播正在直播"按钮、"相关直播"tab 等），
                    # 导致 status=True 误报严重，明明没开播的账号也会被当工人进。
                    # 现在只信 RENDER_DATA 里的 status=2（line 516-521 提取出来的）。
                    # 如果 RENDER_DATA 都没拿到，status 保持 False，宁可漏报不误报。

                    # 设置 room_id（优先使用真实 room_id，其次 web_rid）
                    # web_rid 是 12 位短 ID，真实 room_id 是 18-19 位长 ID
                    # WebSocket 连接需要真实 room_id 才能正确接收弹幕
                    if room_id and len(room_id) >= 18:
                        info.room_id = room_id
                        info.web_rid = web_rid or ""  # 保存 web_rid 用于直播间链接
                        info.status = True
                        log.info(f"[浏览器] 提取到真实 room_id={room_id}")
                    elif web_rid:
                        info.status = True
                        info.web_rid = web_rid  # 先保存 web_rid
                        # web_rid 需要进一步获取真实 room_id
                        log.info(f"[浏览器] 提取到 web_rid={web_rid}，正在获取真实 room_id...")
                        log.info(f"[浏览器] 已保存 info.web_rid={info.web_rid}")
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

                                # 【核心修复A】增强 Playwright 上下文：设置 Cookie 模拟真实用户
                                config = get_config()
                                douyin_cookie = config.douyin.cookie if config else ""

                                # 构建 context 参数
                                context_options = {
                                    'viewport': {'width': 1680, 'height': 1050},
                                    'user_agent': self.WEB_UA,
                                    'ignore_https_errors': True,
                                }

                                # 如果有有效 Cookie，设置到 context（避免被识别为机器人）
                                cookies_to_set = []
                                if douyin_cookie and "ttwid" in douyin_cookie:
                                    import urllib.parse
                                    for part in douyin_cookie.split(";"):
                                        part = part.strip()
                                        if "=" in part:
                                            k, v = part.split("=", 1)
                                            k = k.strip()
                                            if k == "ttwid":
                                                cookies_to_set.append({
                                                    'name': 'ttwid',
                                                    'value': urllib.parse.unquote(v.strip()),
                                                    'domain': '.douyin.com',
                                                    'path': '/',
                                                })
                                                break

                                # 尝试使用持久化 context（包含登录状态），失败则回退到临时 context
                                browser = None
                                context = None
                                try:
                                    from pathlib import Path
                                    browser_data_dir = Path("data/browser_data")
                                    browser_data_dir.mkdir(parents=True, exist_ok=True)
                                    context = p.chromium.launch_persistent_context(
                                        user_data_dir=str(browser_data_dir),
                                        headless=True,
                                        **context_options,
                                    )
                                    browser = None
                                except Exception:
                                    # 回退：使用临时 context
                                    browser = p.chromium.launch(
                                        headless=True,
                                        args=['--no-sandbox', '--disable-setuid-sandbox',
                                              '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'],
                                    )
                                    context = browser.new_context(**context_options)
                                    # 设置 cookies
                                    if cookies_to_set:
                                        try:
                                            context.add_cookies(cookies_to_set)
                                        except Exception as e:
                                            log.debug(f"[浏览器] 设置 Cookie 失败: {e}")

                                page = context.pages[0] if context.pages else context.new_page()

                                # 【核心修复B】增加等待策略：等待网络空闲 + 关键元素出现
                                page.goto(live_url, wait_until='domcontentloaded', timeout=20000)
                                # 先等待关键元素出现（最多等 8 秒）
                                try:
                                    page.wait_for_selector('[class*="live"], [class*="Living"], [class*="直播"]',
                                                           timeout=8000)
                                except Exception:
                                    pass  # 超时继续，让 JS 有更多时间渲染

                                # 【核心修复C】额外等待让 React/Vue 完全渲染
                                page.wait_for_timeout(5000)  # 从 3s 增加到 5s

                                # 【核心修复D】多重提取策略：从多个数据源提取 room_id
                                extracted_ids = []

                                # 策略1: 从 RENDER_DATA 提取（decodeURIComponent 防止转义）
                                try:
                                    render_data = page.evaluate('''() => {
                                        try {
                                            const el = document.getElementById('RENDER_DATA');
                                            if (el && el.textContent) {
                                                const text = decodeURIComponent(el.textContent);
                                                // 查找 room_id
                                                const match = text.match(/"room_id"\\s*:\\s*"?(\\d{18,})"?/);
                                                if (match) return {source: 'RENDER_DATA', id: match[1]};
                                                // 查找 roomId（驼峰格式）
                                                const match2 = text.match(/"roomId"\\s*:\\s*"?(\\d{18,})"?/);
                                                if (match2) return {source: 'roomId', id: match2[1]};
                                            }
                                        } catch(e) {}
                                        return null;
                                    }''')
                                    if render_data and render_data.get('id'):
                                        extracted_ids.append(('RENDER_DATA', render_data['id']))
                                except Exception as e:
                                    log.debug(f"[浏览器] RENDER_DATA 提取失败: {e}")

                                # 策略2: 从 __NEXT_DATA__ 提取
                                try:
                                    next_data = page.evaluate('''() => {
                                        try {
                                            const el = document.getElementById('__NEXT_DATA__');
                                            if (el && el.textContent) {
                                                const data = JSON.parse(el.textContent);
                                                // 尝试多个可能的路径
                                                const paths = [
                                                    'props.pageProps.room.room_id',
                                                    'props.pageProps.roomInfo.room.id_str',
                                                    'props.pageProps.roomInfo.room.id',
                                                    'props.pageProps.liveRoomInfo.roomId',
                                                ];
                                                for (const p of paths) {
                                                    const val = p.split('.').reduce((o, k) => o && o[k], data);
                                                    if (val) return {source: '__NEXT_DATA__', id: String(val)};
                                                }
                                            }
                                        } catch(e) {}
                                        return null;
                                    }''')
                                    if next_data and next_data.get('id'):
                                        extracted_ids.append(('__NEXT_DATA__', next_data['id']))
                                except Exception as e:
                                    log.debug(f"[浏览器] __NEXT_DATA__ 提取失败: {e}")

                                # 策略3: 从 window.__RENDER_DATA__ 提取
                                try:
                                    window_render = page.evaluate('''() => {
                                        try {
                                            if (window.__RENDER_DATA__) {
                                                const text = JSON.stringify(window.__RENDER_DATA__);
                                                const match = text.match(/"room_id"\\s*:\\s*"?(\\d{18,})"?/);
                                                if (match) return {source: 'window.__RENDER_DATA__', id: match[1]};
                                            }
                                        } catch(e) {}
                                        return null;
                                    }''')
                                    if window_render and window_render.get('id'):
                                        extracted_ids.append(('window.__RENDER_DATA__', window_render['id']))
                                except Exception as e:
                                    log.debug(f"[浏览器] window.__RENDER_DATA__ 提取失败: {e}")

                                # 策略4: 从 window.__INITIAL_STATE__ 提取
                                try:
                                    initial_state = page.evaluate('''() => {
                                        try {
                                            if (window.__INITIAL_STATE__) {
                                                const text = JSON.stringify(window.__INITIAL_STATE__);
                                                const match = text.match(/"room_id"\\s*:\\s*"?(\\d{18,})"?/);
                                                if (match) return {source: '__INITIAL_STATE__', id: match[1]};
                                            }
                                        } catch(e) {}
                                        return null;
                                    }''')
                                    if initial_state and initial_state.get('id'):
                                        extracted_ids.append(('__INITIAL_STATE__', initial_state['id']))
                                except Exception as e:
                                    log.debug(f"[浏览器] __INITIAL_STATE__ 提取失败: {e}")

                                # 策略5: 从 SSR_HYDRATED_DATA 提取
                                try:
                                    ssr_hydrated = page.evaluate('''() => {
                                        try {
                                            if (window._SSR_HYDRATED_DATA) {
                                                const text = JSON.stringify(window._SSR_HYDRATED_DATA);
                                                const match = text.match(/"room_id"\\s*:\\s*"?(\\d{18,})"?/);
                                                if (match) return {source: '_SSR_HYDRATED_DATA', id: match[1]};
                                            }
                                        } catch(e) {}
                                        return null;
                                    }''')
                                    if ssr_hydrated and ssr_hydrated.get('id'):
                                        extracted_ids.append(('_SSR_HYDRATED_DATA', ssr_hydrated['id']))
                                except Exception as e:
                                    log.debug(f"[浏览器] _SSR_HYDRATED_DATA 提取失败: {e}")

                                # 策略6: 回退到 HTML 内容提取
                                if not extracted_ids:
                                    live_html = page.content()
                                    _all = re.findall(r'"room_id"\s*:\s*"(\d{18,})"', live_html)
                                    if _all:
                                        extracted_ids.append(('HTML-content', _all[0]))
                                    else:
                                        # 尝试 roomId 格式
                                        _all2 = re.findall(r'roomId.*?(\d{18,20})', live_html)
                                        if _all2:
                                            extracted_ids.append(('HTML-roomId', _all2[0]))

                                # 选择第一个有效的 ID
                                for source, rid in extracted_ids:
                                    if rid and len(rid) >= 18:
                                        real_room_id = rid
                                        log.info(f"[浏览器] Playwright 从 {source} 获取到真实 room_id={real_room_id}")
                                        break

                                # 如果还是没拿到，检查是否被验证码拦截
                                if not real_room_id:
                                    page_title = page.title() if page else ""
                                    page_url = page.url if page else ""
                                    log.warning(f"[浏览器] Playwright 未获取到 room_id，页面标题={page_title}，URL={page_url}")

                                # 清理资源
                                page.close()
                                context.close()
                                if browser:
                                    browser.close()

                            except Exception as e:
                                log.warning(f"[浏览器] Playwright 访问直播页面失败: {e}")
                        
                        if real_room_id:
                            info.room_id = real_room_id
                            log.info(f"[浏览器] 获取到 room_id={real_room_id}, info.web_rid={info.web_rid}")
                        else:
                            info.room_id = web_rid
                            info.web_rid = web_rid
                            log.warning(f"[浏览器] 未能获取真实 room_id，使用 web_rid={web_rid}")
                    elif room_id:
                        info.room_id = room_id
                        info.web_rid = web_rid or ""
                        info.status = True
                        log.info(f"[浏览器] 提取到 room_id={room_id}")

                    # 如果检测到直播但没有 room_id，尝试从直播链接获取
                    if info.status and not info.room_id:
                        log.info(f"[浏览器] 检测到直播但无 room_id，尝试从直播链接获取...")
                        found_web_rid = None

                        # 方式0: 滚动页面后再查找直播链接（直播卡片可能需要滚动才显示）
                        try:
                            page.evaluate('window.scrollTo(0, 0)')
                            page.wait_for_timeout(500)
                            # 尝试点击任何可见的"直播中"或直播相关元素
                            live_elements = page.locator('[class*="live"], [class*="直播"], [class*="Living"]').all()
                            for elem in live_elements[:5]:  # 最多尝试5个
                                try:
                                    if elem.is_visible(timeout=1000):
                                        # 尝试获取元素的 href 属性
                                        href = elem.get_attribute('href')
                                        if href and 'live.douyin.com' in href:
                                            rid_match = re.search(r'live\.douyin\.com/(\d+)', href)
                                            if rid_match:
                                                found_web_rid = rid_match.group(1)
                                                log.debug(f"[浏览器] 从滚动后的元素获取到 live_id={found_web_rid}")
                                                break
                                        # 尝试点击元素（可能打开弹窗或导航）
                                        elem.click(timeout=2000)
                                        page.wait_for_timeout(2000)
                                        # 点击后检查 URL 是否变化
                                        current_url = page.url
                                        if 'live.douyin.com' in current_url:
                                            rid_match = re.search(r'live\.douyin\.com/(\d+)', current_url)
                                            if rid_match:
                                                found_web_rid = rid_match.group(1)
                                                log.debug(f"[浏览器] 点击后从URL获取到 live_id={found_web_rid}")
                                                break
                                except Exception as e:
                                    log.debug(f"[浏览器] 尝试元素失败: {e}")
                                    continue
                        except Exception as e:
                            log.debug(f"[浏览器] 滚动和点击尝试失败: {e}")

                        # 方式1: 从 a 标签获取（重新查找，因为滚动后可能显示了）
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
                                info.web_rid = found_web_rid  # 保存 web_rid
                            else:
                                info.room_id = found_web_rid
                                info.web_rid = found_web_rid
                                log.warning(f"[浏览器] 未能获取真实room_id，使用原始ID={found_web_rid}")
                            log.info(f"[浏览器] 从直播链接提取 room_id={info.room_id}")

                    if info.status:
                        log.info(f"[浏览器] 最终结果: status=True, room_id={info.room_id}, web_rid={info.web_rid}, nickname={info.anchor_nickname}")
                    else:
                        # 【2026-10-13 提升为 INFO】之前 debug 级别看不到 "未开播" 状态，
                        # 导致用户以为 detector 卡住。其实它是正常工作的"没开播"信号。
                        log.info(f"[浏览器] 未检测到直播 (room_id='', web_rid='', nickname='')")

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
        stagger_delay: float = 4.0,
        max_concurrency: int = 0,
    ) -> dict[str, LiveRoomInfo]:
        """
        批量检测多个账号（并发）。

        【2026-10-13 改动】原来是顺序扫描（9 个账号 × 15s = 2 分钟多），期间直播间上下播
        可能错过窗口。现在用 asyncio.gather 并发，所有账号一起检测，~15s 完成一轮。

        ⚠ 但并发**不是越多越好**（2026-11-13 实测）：
          每个账号都要**各启一个 Chromium**（Playwright 的 sync API 不能跨线程共用，
          所以并发是靠线程数堆出来的），14 个账号并发 = 14 个浏览器同时起来，
          实测吃掉 **290% CPU**（10 核机器 load average 17~27）。
          而触达那边每分钟都在真发消息，它的浏览器被挤得搜索从 20 秒变成 46 秒。

          所以这里加一个**并发上限**（默认 4 个）：
          CPU 降到 ~85%，代价是一轮检测从 ~15 秒变成 ~55 秒
          —— 开播后平均多等 40 秒才被发现。取舍得业务自己定，
          配置项是 live_detection.max_concurrency。

        Args:
            accounts: 监控账号列表
            stagger_delay: 已废弃（保留参数兼容）。原本用于错峰避免同时请求，
                           现在浏览器实例天然隔离，并发不再有封禁风险。
            max_concurrency: 同时最多检测几个账号（0 = 用配置里的值）

        Returns:
            {sec_user_id: LiveRoomInfo}
        """
        if not accounts:
            return {}

        if not max_concurrency:
            try:
                from ..config import get_config
                max_concurrency = int(get_config().live_detection.max_concurrency or 4)
            except Exception:
                max_concurrency = 4
        max_concurrency = max(1, int(max_concurrency))

        # 信号量：把「同时起几个浏览器」限制住
        sem = asyncio.Semaphore(max_concurrency)

        async def _detect_one(acc: MonitorAccount) -> tuple[str, LiveRoomInfo]:
            async with sem:
                try:
                    return (acc.sec_user_id, await self.detect(acc))
                except Exception as e:
                    log.error(f"批量检测异常 [{acc.name}]: {e}")
                    return (acc.sec_user_id, LiveRoomInfo(status=False))

        # 并发跑所有账号，每个账号独立 LiveDetector（避免共享 httpx 跨 event loop）
        results = await asyncio.gather(
            *[_detect_one(acc) for acc in accounts],
            return_exceptions=False,
        )
        return dict(results)


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
