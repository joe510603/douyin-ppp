"""直播间弹幕采集器 — 基于 WebSocket + Protobuf 的实时弹幕抓取"""

from __future__ import annotations

import asyncio
import gzip
import json
import random
import re
import struct
import time as import_time
from datetime import datetime
from typing import Optional

import websockets

from ..config import get_config
from ..models.comment import LiveComment, MessageType, LiveRoomInfo, MonitorAccount
from ..utils.logger import get_logger
from ..utils.retry import ReconnectManager, RetryPolicy

log = get_logger("collector")


# ============================================================
# 协议常量（参考 DySpider / DouyinLiveWebFetcher）
# ============================================================

# WebSocket 服务器地址池
WS_SERVERS = [
    "wss://webcast5-ws-web-lf.douyin.com",
    "wss://webcast5-ws-web-lq.douyin.com",
]

# Protobuf 消息类型码 → 方法名映射
MESSAGE_TYPE_MAP = {
    1: "WebcastChatMessage",             # 弹幕/评论
    2: "WebcastGiftMessage",             # 礼物消息
    3: "WebcastMemberMessage",           # 成员进入
    4: "WebcastSocialMessage",           # 社交消息(分享/关注)
    5: "WebcastLikeMessage",             # 点赞
    6: "WebcastUpdateFansGroupMessage",  # 粉丝团更新
    8: "WebcastRoomUserSeqMessage",      # 在线人数
}

# PushFrame 头部格式
PUSH_FRAME_HEADER_FORMAT = ">IHIHHII"
PUSH_FRAME_HEADER_SIZE = struct.calcsize(PUSH_FRAME_HEADER_FORMAT)


def _parse_push_frame(data: bytes) -> list[tuple[int, bytes]]:
    """
    解析抖音 PushFrame 二进制数据包。
    
    PushFrame 结构:
    ├── header_len (4B uint32 LE)  头部长度
    ├── header (变长)               JSON 头部
    │   ├── ver (int)               协议版本
    │   ├── op (int)                操作码
    │   └── seq (int)               序列号
    ├── payload_len (4B uint32 LE)  载荷长度
    └── payload (变长)              实际数据（可能被 gzip 压缩）
    
    Returns:
        [(message_type_code, payload_bytes), ...]
    """
    messages = []
    offset = 0
    length = len(data)
    
    while offset < length:
        if offset + PUSH_FRAME_HEADER_SIZE > length:
            break
        
        # 解析头部
        header_len, header_ver, op, seq_id, msg_type, _, payload_len = struct.unpack_from(
            PUSH_FRAME_HEADER_FORMAT, data, offset
        )
        
        # 验证头部长度合理性
        if header_len > 1024 or header_len < 18:
            break
        
        payload_offset = offset + header_len
        if payload_offset + payload_len > length:
            break
        
        payload = data[payload_offset:payload_offset + payload_len]
        
        # 尝试解压 gzip
        if len(payload) >= 2 and payload[:2] == b'\x1f\x8b':
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        
        messages.append((msg_type, payload))
        offset = payload_offset + payload_len
    
    return messages


def _parse_chat_message(payload: bytes) -> Optional[LiveComment]:
    """
    尝试解析聊天/弹幕消息。
    
    注意：这里使用简化的解析方式。完整的解析需要编译 .proto 文件。
    对于核心字段，我们直接从二进制中提取可读字符串。
    """
    comment = None
    try:
        # 尝试作为 protobuf 解析
        # 如果有编译好的 proto 模块，优先使用
        from ...proto import parse_message as proto_parse
        parsed = proto_parse("WebcastChatMessage", payload)
        if parsed:
            comment = LiveComment(**parsed)
            comment.message_type = MessageType.CHAT
            return comment
    except ImportError:
        pass
    except Exception:
        pass
    
    # 回退：提取可读文本
    try:
        content = _extract_string_from_payload(payload)
        if content:
            comment = LiveComment(
                content=content[:500],  # 限制长度
                message_type=MessageType.CHAT,
                raw_data=payload.hex()[:200] if get_config().websocket.log_unknown_messages else None,
            )
    except Exception:
        pass
    
    return comment


def _extract_string_from_payload(data: bytes) -> str:
    """从 protobuf 二进制载荷中尝试提取 UTF-8 字符串"""
    strings = []
    current = b""
    
    for byte in data:
        if 32 <= byte < 127 or byte in (0xe2, 0xe3):  # 可打印字符或中文起始字节
            current += bytes([byte])
        else:
            if len(current) >= 2:
                try:
                    s = current.decode("utf-8")
                    strings.append(s)
                except UnicodeDecodeError:
                    pass
            current = b""
    
    if len(current) >= 2:
        try:
            strings.append(current.decode("utf-8"))
        except UnicodeDecodeError:
            pass
    
    # 返回最长的字符串（通常是评论内容）
    if strings:
        return max(strings, key=len)
    return ""


class LiveCollector:
    """
    直播间弹幕采集器。
    
    通过 WebSocket 连接抖音直播间服务器，
    实时接收并解析弹幕/礼物/进场等消息。
    """
    
    def __init__(
        self, 
        on_comment: Optional[callable] = None,
        on_status_change: Optional[callable] = None,
        monitor_name: str = "",
    ):
        """
        Args:
            on_comment: 新评论回调函数 signature: (comment: LiveComment) -> None
            on_status_change: 连接状态变化回调 signature: (connected: bool, msg: str) -> None
            monitor_name: 监控账号名称（用于标识数据来源）
        """
        self.on_comment = on_comment
        self.on_status_change = on_status_change
        self.monitor_name = monitor_name
        self._ws = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._reconnect_mgr = ReconnectManager(RetryPolicy(
            max_attempts=get_config().websocket.max_reconnect_attempts,
            initial_delay=get_config().websocket.reconnect_initial_delay,
            max_delay=get_config().websocket.reconnect_max_delay,
        ))
        self._stats = {
            "messages_total": 0,
            "chat_count": 0,
            "gift_count": 0,
            "member_count": 0,
            "connect_time": None,
            "last_message_time": None,
        }
    
    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.open
    
    @property
    def stats(self) -> dict:
        return dict(self._stats)
    
    async def connect(self, room_id: str, cookie: str = "", ws_url_override: str = None) -> bool:
        """
        连接到指定直播间。

        Args:
            room_id: 直播间 room_id
            cookie: 抖音 Cookie（用于鉴权）
            ws_url_override: 手动指定的 WebSocket URL（带签名）

        Returns:
            是否连接成功
        """
        if self._running:
            log.warning("采集器已在运行中")
            return True

        self._running = True
        self._task = asyncio.create_task(
            self._run(room_id, cookie, ws_url_override),
            name=f"live-collector-{room_id}",
        )
        return True
    
    async def disconnect(self):
        """断开连接"""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        self._reconnect_mgr.reset()
        self._notify_status(False, "已断开连接")
    
    async def _run(self, room_id: str, cookie: str, ws_url_override: str = None):
        """WebSocket 主循环"""
        config = get_config()

        # 确定 WebSocket URL
        ws_url = None
        current_ws_url_override = ws_url_override
        use_signer_first = True  # 标志：是否优先使用签名服务
        ttwid = None  # 存储 ttwid
        
        # 尝试从 cookie 中提取 ttwid
        if cookie:
            import re
            ttwid_match = re.search(r'ttwid=([^;]+)', cookie)
            if ttwid_match:
                ttwid = ttwid_match.group(1)
                log.debug(f"从配置中提取到 ttwid: {ttwid[:20]}...")

        while self._running:
            # 1. 首次尝试：使用账号指定的 URL
            if not ws_url and current_ws_url_override:
                ws_url = current_ws_url_override
                log.info(f"使用账号指定的 WebSocket URL")

            # 2. 优先使用 Node.js 签名服务（高性能）
            if not ws_url and use_signer_first:
                log.info("正在使用 Node.js 签名服务获取 WebSocket URL...")
                
                # 如果没有 ttwid，先从直播间页面获取
                if not ttwid:
                    log.info("未找到 ttwid，正在从直播间页面获取...")
                    # 构建直播间 URL（假设 room_id 是直播间的 web_rid）
                    live_url = f"https://live.douyin.com/{room_id}"
                    room_info = await self._fetch_room_info_from_page(live_url)
                    if room_info:
                        ttwid = room_info.get('ttwid')
                        real_room_id = room_info.get('room_id')
                        if real_room_id and real_room_id != room_id:
                            log.info(f"使用真实的 room_id: {real_room_id}")
                            room_id = real_room_id
                
                ws_url = await self._fetch_signed_ws_url_from_signer(room_id)
                if ws_url:
                    log.info("✅ Node.js 签名服务获取成功")
                else:
                    use_signer_first = False  # 签名服务失败，标记不再使用

            # 3. 回退到 Playwright 方式（兼容性好但性能低）
            if not ws_url:
                log.info("正在使用 Playwright 获取 WebSocket URL...")
                ws_url = await self._fetch_signed_ws_url(room_id)
                if ws_url:
                    log.info("✅ Playwright 获取成功")
                else:
                    # 4. 最后手段：使用备用方式构建
                    log.warning("Playwright 失败，使用备用方式构建 WebSocket URL")
                    ws_url = self._build_ws_url(room_id, cookie)

            try:
                log.info(f"正在连接直播间 {room_id}...")
                self._notify_status(True, f"连接中... {ws_url[:60]}...")

                # 构建请求头（参考 douyin-parse-danmu）
                headers = None
                if ttwid or cookie:
                    from websockets.http import Headers
                    headers = Headers()
                    # 使用 ttwid 构建 Cookie
                    cookie_header = f"ttwid={ttwid}" if ttwid else cookie
                    headers["Cookie"] = cookie_header
                    headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
                    log.debug(f"WebSocket 连接 Headers: Cookie={cookie_header[:30]}...")

                # websockets 12.0+ 使用 additional_headers
                ws_kwargs = {
                    "ping_interval": config.websocket.heartbeat_interval,
                    "ping_timeout": 15,
                    "close_timeout": 5,
                    "max_size": 10 * 1024 * 1024,  # 10MB max message
                }
                if headers:
                    ws_kwargs["additional_headers"] = headers

                async with websockets.connect(ws_url, **ws_kwargs) as ws:
                    self._ws = ws
                    self._reconnect_mgr.record_success()
                    self._stats["connect_time"] = datetime.now().isoformat()
                    
                    log.info(f"✅ 直播间 {room_id} 已连接")
                    self._notify_status(True, f"已连接 - 直播间 {room_id}")
                    
                    # 注意：参考 douyin-parse-danmu 项目，不需要发送进入直播间请求
                    # WebSocket 连接建立后，服务器会自动推送消息
                    
                    # 接收消息循环
                    message_count = 0
                    async for raw_msg in ws:
                        if not self._running:
                            break

                        message_count += 1
                        try:
                            if isinstance(raw_msg, bytes):
                                log.debug(f"收到二进制消息 #{message_count}: {len(raw_msg)} bytes")
                                await self._handle_raw_message(raw_msg, room_id, ws)  # 传递 ws 对象
                            elif isinstance(raw_msg, str):
                                log.debug(f"收到文本消息 #{message_count}: {raw_msg[:200]}")
                                # frontier 协议可能发送文本消息
                                self._stats["messages_total"] += 1
                        except Exception as e:
                            log.error(f"处理消息异常: {e}")
                            
            except websockets.exceptions.ConnectionClosed as e:
                log.warning(f"WebSocket 连接关闭: {e.code} {e.reason}")
                self._notify_status(False, f"连接断开: {e.reason}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"WebSocket 异常: {e}")
                self._notify_status(False, f"连接错误: {e}")

                # 如果是签名无效错误，清除 URL 并禁用签名服务以便使用 Playwright
                if "HTTP 200" in str(e) or "rejected" in str(e).lower():
                    if use_signer_first:
                        log.warning("签名服务失败，将使用 Playwright 重试")
                        use_signer_first = False
                        ws_url = None
                        continue  # 立即重试，不等待
                    else:
                        log.warning("签名可能已失效，下次重连时将获取新 URL")
                        ws_url = None
                        current_ws_url_override = None

            self._ws = None

            # 判断是否重连
            if not self._running:
                break

            self._reconnect_mgr.record_failure()

            if self._reconnect_mgr.should_give_up:
                log.error(f"达到最大重连次数 ({self._reconnect_mgr.policy.max_attempts}), 停止重连")
                self._notify_status(False, "重连次数耗尽，已停止")
                break

            delay = self._reconnect_mgr.next_delay()
            log.info(f"将在 {delay:.1f}s 后重新连接... (第 {self._reconnect_mgr.attempts + 1} 次)")
            self._notify_status(False, f"等待重连 ({delay:.0f}s)...")
            await asyncio.sleep(delay)
    
    def _build_ws_url(self, room_id: str, cookie: str = "") -> str:
        """构建 WebSocket 连接 URL"""
        import random

        url = WS_SERVERS[random.randint(0, len(WS_SERVERS) - 1)]
        url += "/webcast/im/push/v2/?"
        url += "&".join([
            f"app_name=douyin_web",
            f"version_code=19300000",
            f"live_platform=web",
            f"room_id={room_id}",
            f"aid=6383",
            f"user_unique_id={random.randint(100000000000, 999999999999)}",
            f"device_platform=web",
            f"tz_difference=-480",
        ])

        # 尝试签名（如果 signer 可用）
        try:
            from ..utils.signer import generate_signature
            sig = generate_signature(url)
            if sig:
                url += f"&{sig}"
        except Exception:
            pass

        return url

    async def _fetch_room_info_from_page(self, live_url: str) -> dict | None:
        """
        从直播间页面获取 ttwid 和 room_id
        
        Args:
            live_url: 直播间 URL (https://live.douyin.com/xxx)
        
        Returns:
            dict: {'ttwid': str, 'room_id': str} 或 None
        """
        try:
            import aiohttp
            import re
            
            headers = {
                'authority': 'live.douyin.com',
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
                'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'cache-control': 'max-age=0',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(live_url, headers=headers, ssl=False) as response:
                    if response.status != 200:
                        log.error(f"获取直播间页面失败: HTTP {response.status}")
                        return None
                    
                    # 1. 从 Set-Cookie 中提取 ttwid
                    ttwid = None
                    for header in response.headers.getall('Set-Cookie', []):
                        if 'ttwid=' in header:
                            ttwid_match = re.search(r'ttwid=([^;]+)', header)
                            if ttwid_match:
                                ttwid = ttwid_match.group(1)
                                break
                    
                    # 2. 从页面内容中提取 room_id
                    html = await response.text()
                    room_id_match = re.search(r'roomId\\\\":\\\\"(\d+)\\\\"', html)
                    if not room_id_match:
                        # 尝试另一种格式
                        room_id_match = re.search(r'"roomId":"(\d+)"', html)
                    
                    if not room_id_match:
                        log.error("无法从页面中提取 room_id")
                        return None
                    
                    room_id = room_id_match.group(1)
                    
                    log.info(f"从页面获取到 ttwid={ttwid[:20] if ttwid else 'None'}..., room_id={room_id}")
                    
                    return {
                        'ttwid': ttwid,
                        'room_id': room_id
                    }
                    
        except Exception as e:
            log.error(f"获取直播间信息失败: {e}", exc_info=True)
            return None

    async def _fetch_signed_ws_url_from_signer(self, room_id: str) -> str | None:
        """
        使用 Node.js 签名服务获取 WebSocket URL（高性能方式）
        
        参考自 douyin-parse-danmu 项目的实现
        """
        try:
            from ..utils.signer_client import get_signer_client
            import hashlib
            import random
            import urllib.parse
            
            user_unique_id = str(random.randint(7300000000000000000, 7999999999999999999))
            version_code = "180800"
            webcast_sdk_version = "1.0.14-beta.0"
            
            # 1. 签名参数（严格按照参考项目的顺序）
            sig_params = {
                "live_id": "1",
                "aid": "6383",
                "version_code": version_code,
                "webcast_sdk_version": webcast_sdk_version,
                "room_id": str(room_id),
                "sub_room_id": "",
                "sub_channel_id": "",
                "did_rule": "3",
                "user_unique_id": user_unique_id,
                "device_platform": "web",
                "device_type": "",
                "ac": "",
                "identity": "audience"
            }
            
            # 2. 计算 X-Ms-Stub（将参数拼接成字符串后取 MD5）
            sig_params_str = ",".join([f"{k}={v}" for k, v in sig_params.items()])
            x_ms_stub = hashlib.md5(sig_params_str.encode('utf-8')).hexdigest()
            
            # 3. 调用签名服务
            signer = get_signer_client()
            signature = await signer.get_signature(x_ms_stub)
            
            if not signature:
                log.warning("签名服务返回空结果")
                return None
            
            # 4. 构建 WebSocket URL（使用 webcast5 服务器）
            webcast_params = {
                "room_id": str(room_id),
                "compress": "gzip",
                "version_code": version_code,
                "webcast_sdk_version": webcast_sdk_version,
                "live_id": "1",
                "did_rule": "3",
                "user_unique_id": user_unique_id,
                "identity": "audience",
                "signature": signature,
            }
            
            # 5. 构建基础 WebSocket URL
            base_url = "wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/?"
            query_string = "&".join([f"{k}={v}" for k, v in webcast_params.items()])
            ws_url = base_url + query_string
            
            # 6. 添加浏览器参数（参考项目的 buildRequestUrl 方法）
            parsed_url = urllib.parse.urlparse(ws_url)
            params = dict(urllib.parse.parse_qsl(parsed_url.query))
            
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
            
            params.update({
                "aid": "6383",
                "device_platform": "web",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": user_agent.split()[0],
                "browser_version": user_agent.split('/')[-1],
            })
            
            # 重新构建 URL
            new_query = urllib.parse.urlencode(params)
            ws_url = f"{parsed_url.scheme}://{parsed_url.hostname}{parsed_url.path}?{new_query}"
            
            log.debug(f"签名服务构建的 WebSocket URL: {ws_url[:100]}...")
            return ws_url
                
        except Exception as e:
            log.error(f"使用签名服务失败: {e}", exc_info=True)
            return None

    async def _fetch_signed_ws_url(self, room_id: str) -> str | None:
        """
        使用 Playwright 通过浏览器获取抖音直播间的 WebSocket 签名 URL。
        这是获取有效签名的最可靠方式。
        """
        config = get_config()
        cookie_str = config.douyin.cookie

        # 同步版本在异步函数中运行
        def _fetch_with_playwright():
            import re
            from urllib.parse import unquote

            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=True,
                        args=[
                            '--no-sandbox',
                            '--disable-setuid-sandbox',
                            '--disable-dev-shm-usage',
                            '--disable-blink-features=AutomationControlled',
                        ]
                    )

                    # 创建上下文
                    context = browser.new_context(
                        viewport={'width': 1680, 'height': 1050},
                        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    )

                    # 首先访问抖音主页以设置必要的 cookies
                    try:
                        main_page = context.new_page()
                        main_page.goto('https://www.douyin.com/', wait_until='domcontentloaded', timeout=15000)
                        main_page.wait_for_timeout(2000)

                        # 解析并添加 cookies
                        if cookie_str:
                            cookies = []
                            for part in cookie_str.split(";"):
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
                                    log.warning(f"设置 cookies 失败: {e}")
                        main_page.close()
                    except Exception as e:
                        log.warning(f"访问抖音主页失败: {e}")

                    # 创建页面
                    page = context.new_page()
                    ws_url = None
                    ws_urls_found = []  # 收集所有找到的 WebSocket URL

                    # 监听 WebSocket 连接创建
                    def on_websocket(websocket):
                        nonlocal ws_url
                        url = websocket.url
                        ws_urls_found.append(url)
                        log.debug(f"WebSocket 创建: {url[:100]}...")
                        # 抖音使用 frontier 或 webcast5-ws
                        if ('wss://webcast' in url and 'push/v2' in url) or \
                           ('wss://frontier' in url and 'ws/v2' in url):
                            if not ws_url:
                                ws_url = url
                                log.info(f"✅ 通过 Playwright WebSocket 事件获取到 URL: {url[:100]}...")

                    page.on('websocket', on_websocket)

                    # 同时监听请求（作为备用）
                    def on_request(request):
                        nonlocal ws_url
                        url = request.url
                        if ('wss://webcast' in url and 'push/v2' in url) or \
                           ('wss://frontier' in url and 'ws/v2' in url):
                            if not ws_url:
                                ws_url = url
                                log.info(f"✅ 通过 Request 监听获取到 WebSocket URL: {url[:100]}...")

                    page.on('request', on_request)

                    try:
                        # 访问直播间页面
                        live_url = f"https://live.douyin.com/{room_id}"
                        log.info(f"使用 Playwright 访问: {live_url}")

                        # 拦截所有响应，寻找包含 webcast 的数据
                        api_responses = {}

                        def on_response(response):
                            url = response.url
                            if 'webcast' in url or 'im/push' in url:
                                api_responses[url] = True
                                log.debug(f"捕获到 API 响应: {url[:100]}...")

                        page.on('response', on_response)

                        response = page.goto(live_url, wait_until='domcontentloaded', timeout=30000)

                        if response and response.ok:
                            # 等待页面加载完成
                            try:
                                page.wait_for_load_state('networkidle', timeout=15000)
                            except:
                                pass  # networkidle 可能超时，继续
                            
                            # 等待关键元素加载
                            try:
                                page.wait_for_selector('[class*="live"]', timeout=5000)
                            except:
                                pass
                            
                            # 额外等待让 JS 执行和 WebSocket 创建
                            log.debug(f"页面加载完成，等待 WebSocket 创建...")
                            
                            # 分段等待，每次检查是否已获取到 URL
                            for i in range(30):  # 最多等30秒
                                page.wait_for_timeout(1000)
                                if ws_url:
                                    log.info(f"在第 {i+1} 秒捕获到 WebSocket URL")
                                    break
                                
                                # 每5秒打印一次调试信息
                                if i > 0 and i % 5 == 0:
                                    log.debug(f"已等待 {i} 秒，已捕获 {len(ws_urls_found)} 个 WebSocket 连接")

                            # 如果没捕获到，记录调试信息
                            if not ws_url:
                                content = page.content()
                                log.debug(f"页面内容长度: {len(content)}")
                                log.debug(f"捕获到的所有 WebSocket URL: {ws_urls_found}")
                                log.debug(f"捕获到的 API 响应数: {len(api_responses)}")

                            # 尝试通过 JS 获取 WebSocket 信息
                            if not ws_url:
                                try:
                                    ws_info = page.evaluate('''() => {
                                        // 1. 尝试从 window 对象获取
                                        for (let key in window) {
                                            try {
                                                let val = window[key];
                                                if (val && typeof val === 'string' && val.includes('wss://webcast')) {
                                                    return {source: key, url: val};
                                                }
                                            } catch(e) {}
                                        }
                                        // 2. 尝试从 SSR_HYDRATED_DATA 获取
                                        if (window._SSR_HYDRATED_DATA) {
                                            try {
                                                const data = JSON.stringify(window._SSR_HYDRATED_DATA);
                                                const match = data.match(/wss:\/\/webcast[^\\s"'<>]+/);
                                                if (match) return {source: '_SSR_HYDRATED_DATA', url: match[0]};
                                            } catch(e) {}
                                        }
                                        // 3. 尝试从 __INITIAL_STATE__ 获取
                                        if (window.__INITIAL_STATE__) {
                                            try {
                                                const data = JSON.stringify(window.__INITIAL_STATE__);
                                                const match = data.match(/wss:\/\/webcast[^\\s"'<>]+/);
                                                if (match) return {source: '__INITIAL_STATE__', url: match[0]};
                                            } catch(e) {}
                                        }
                                        return null;
                                    }''')
                                    if ws_info and ws_info.get('url'):
                                        ws_url = ws_info['url']
                                        log.info(f"通过 JS 获取到 WebSocket URL (来源: {ws_info.get('source')}): {ws_url[:100]}...")
                                except Exception as e:
                                    log.debug(f"通过 JS 获取 WebSocket URL 失败: {e}")

                            # 尝试从所有网络响应中查找 WebSocket URL
                            if not ws_url and api_responses:
                                for url in api_responses.keys():
                                    if 'wss://' in url and 'webcast' in url:
                                        ws_url = url
                                        log.info(f"从 API 响应获取到 WebSocket URL: {ws_url[:100]}...")
                                        break

                                # 搜索各种可能的 WebSocket URL 格式
                                patterns = [
                                    (r'wss://webcast5-ws-web[^\s"\'<>]+push/v2[^\s"\'<>]*', 'webcast5 格式'),
                                    (r'wss://webcast[^\s"\'<>]+/webcast/im/push/v2[^\s"\'<>]*', '标准格式'),
                                    (r'["\']?(wss://webcast[^\s"\'<>]+)["\']?', '通用格式'),
                                ]
                                for pattern, desc in patterns:
                                    ws_match = re.search(pattern, content)
                                    if ws_match:
                                        ws_url = ws_match.group(0)
                                        ws_url = unquote(ws_url)
                                        # 清理 URL
                                        ws_url = re.sub(r'["\'<>\\]', '', ws_url)
                                        log.info(f"从页面内容提取到 WebSocket URL ({desc}): {ws_url[:100]}...")
                                        break

                    except Exception as e:
                        log.error(f"Playwright 访问页面失败: {e}")
                    finally:
                        page.close()
                        context.close()
                        browser.close()

                    return ws_url

            except ImportError as e:
                log.warning(f"Playwright 未安装: {e}")
                return None
            except Exception as e:
                log.error(f"Playwright 执行失败: {e}")
                return None

        # 在线程中运行同步的 Playwright 代码
        try:
            import asyncio
            return await asyncio.to_thread(_fetch_with_playwright)
        except Exception as e:
            log.error(f"在线程中运行 Playwright 失败: {e}")
            return None

    async def _send_enter_room(self, ws, room_id: str):
        """发送进入直播间握手包"""
        enter_payload = json.dumps({
            "type": "msgtype_enter",
            "data": {"room_id": int(room_id)},
        }).encode()
        
        header = json.dumps({
            "ver": 1,
            "op": 7,
            "seq": 1,
        }).encode()
        
        frame = (
            struct.pack(">I", len(header)) +
            header +
            struct.pack(">I", len(enter_payload)) +
            enter_payload
        )
        
        await ws.send(frame)
        log.debug(f"已发送进入直播间请求: room_id={room_id}")
    
    async def _handle_raw_message(self, raw_data: bytes, room_id: str, ws=None):
        """
        处理原始二进制消息（参考 douyin-parse-danmu 的实现）
        
        流程：
        1. 解析 PushFrame -> 获取 logId 和 payload
        2. gzdecode 解压 payload
        3. 解析 Response -> 检查 needAck
        4. 如果 needAck，发送 ACK 响应
        5. 遍历 messagesList 处理每条消息
        """
        try:
            import gzip
            from proto import dy_pb2
            
            # 1. 解析 PushFrame
            push_frame = dy_pb2.PushFrame()
            push_frame.ParseFromString(raw_data)
            log_id = push_frame.logId
            payload = push_frame.payload
            
            # 2. 解压 payload
            try:
                decompressed = gzip.decompress(payload)
            except Exception:
                # 如果解压失败，可能未压缩
                decompressed = payload
            
            # 3. 解析 Response
            response = dy_pb2.Response()
            response.ParseFromString(decompressed)
            
            internal_ext = response.internalExt
            
            # 4. 如果需要 ACK，发送响应
            if response.needAck and ws and log_id:
                await self._send_ack(ws, log_id, internal_ext)
            
            # 5. 处理每条消息
            for message in response.messagesList:
                self._stats["messages_total"] += 1
                self._stats["last_message_time"] = datetime.now().isoformat()
                
                method = message.method
                payload_data = message.payload
                
                # 根据方法名处理不同类型的消息
                await self._process_message(method, payload_data, room_id)
                
        except Exception as e:
            log.error(f"处理消息失败: {e}", exc_info=True)
            # 回退到旧的解析方式
            messages = _parse_push_frame(raw_data)
            for msg_type_code, payload in messages:
                self._stats["messages_total"] += 1
                self._stats["last_message_time"] = datetime.now().isoformat()
                await self._process_message_by_type(msg_type_code, payload, room_id)
    
    async def _send_ack(self, ws, log_id: int, internal_ext: str):
        """发送 ACK 响应"""
        try:
            from proto import dy_pb2
            
            # 构建 ACK PushFrame
            ack_frame = dy_pb2.PushFrame()
            ack_frame.payloadType = 'ack'
            ack_frame.logId = log_id
            ack_frame.payloadType = internal_ext
            
            ack_data = ack_frame.SerializeToString()
            await ws.send(ack_data)
            log.debug(f"已发送 ACK: log_id={log_id}")
            
        except Exception as e:
            log.error(f"发送 ACK 失败: {e}")
    
    async def _process_message(self, method: str, payload: bytes, room_id: str):
        """根据方法名处理消息"""
        from proto import dy_pb2
        
        comment = None
        
        try:
            if method == 'WebcastChatMessage':
                # 弹幕消息
                chat_msg = dy_pb2.ChatMessage()
                chat_msg.ParseFromString(payload)
                
                user_name = getattr(chat_msg.user, 'nickName', '未知用户') if chat_msg.HasField('user') else "未知用户"
                content = chat_msg.content
                
                comment = LiveComment(
                    content=content,
                    message_type=MessageType.CHAT,
                    room_id=room_id,
                    user_nickname=user_name,
                    monitor_name=self.monitor_name,
                    raw_data=chat_msg.SerializeToString().hex()[:200],  # 转换为 hex 字符串
                )
                self._stats["chat_count"] += 1
                log.info(f"💬 [{user_name}]: {content}")
                
            elif method == 'WebcastGiftMessage':
                # 礼物消息
                gift_msg = dy_pb2.GiftMessage()
                gift_msg.ParseFromString(payload)
                
                user_name = getattr(gift_msg.user, 'nickName', '未知用户') if gift_msg.HasField('user') else "未知用户"
                gift_name = "礼物"
                
                comment = LiveComment(
                    content=f"[礼物] {user_name} 送出 {gift_name}",
                    message_type=MessageType.GIFT,
                    room_id=room_id,
                    user_nickname=user_name,
                    monitor_name=self.monitor_name,
                    gift_name=gift_name,
                    raw_data=None,
                )
                self._stats["gift_count"] += 1
                log.info(f"🎁 [{user_name}] 送出礼物")
                
            elif method == 'WebcastMemberMessage':
                # 进场消息
                member_msg = dy_pb2.MemberMessage()
                member_msg.ParseFromString(payload)
                
                user_name = getattr(member_msg.user, 'nickName', '未知用户') if member_msg.HasField('user') else "未知用户"
                
                comment = LiveComment(
                    content=f"[进场] {user_name}",
                    message_type=MessageType.MEMBER,
                    room_id=room_id,
                    user_nickname=user_name,
                    monitor_name=self.monitor_name,
                    raw_data=None,
                )
                self._stats["member_count"] += 1
                log.debug(f"🚪 [{user_name}] 进入直播间")
                
            elif method == 'WebcastLikeMessage':
                # 点赞消息
                like_msg = dy_pb2.LikeMessage()
                like_msg.ParseFromString(payload)
                
                user_name = getattr(like_msg.user, 'nickName', '未知用户') if like_msg.HasField('user') else "未知用户"
                
                comment = LiveComment(
                    content=f"[点赞] {user_name}",
                    message_type=MessageType.LIKE,
                    room_id=room_id,
                    user_nickname=user_name,
                    monitor_name=self.monitor_name,
                    raw_data=None,
                )
                log.debug(f"👍 [{user_name}] 点赞")
                
            else:
                # 其他消息类型
                log.debug(f"收到消息: {method}")
                
            # 触发回调
            if comment and self.on_comment:
                try:
                    import asyncio
                    if asyncio.iscoroutinefunction(self.on_comment):
                        # 异步回调
                        asyncio.create_task(self.on_comment(comment))
                    else:
                        # 同步回调
                        self.on_comment(comment)
                except Exception as e:
                    log.error(f"评论回调异常: {e}")
                    
        except Exception as e:
            log.error(f"处理消息失败 ({method}): {e}")
    
    async def _process_message_by_type(self, msg_type_code: int, payload: bytes, room_id: str):
        """处理原始二进制消息"""
        messages = _parse_push_frame(raw_data)
        
        for msg_type_code, payload in messages:
            self._stats["messages_total"] += 1
            self._stats["last_message_time"] = datetime.now().isoformat()
            
            method_name = MESSAGE_TYPE_MAP.get(msg_type_code, "Unknown")
            
            # 解析具体消息类型
            comment = None
            
            if msg_type_code == 1:
                # 弹幕/评论
                comment = _parse_chat_message(payload)
                if comment:
                    comment.message_type = MessageType.CHAT
                    comment.room_id = room_id
                    self._stats["chat_count"] += 1
                    
            elif msg_type_code == 2:
                # 礼物
                gift_name = _extract_string_from_payload(payload)[:50]
                comment = LiveComment(
                    content=f"[礼物] {gift_name}",
                    message_type=MessageType.GIFT,
                    room_id=room_id,
                    gift_name=gift_name,
                    raw_data=None,
                )
                self._stats["gift_count"] += 1
                
            elif msg_type_code == 3:
                # 用户进场
                nickname = _extract_string_from_payload(payload)[:30]
                comment = LiveComment(
                    content=f"[进场] {nickname}",
                    message_type=MessageType.MEMBER,
                    room_id=room_id,
                    user_nickname=nickname,
                    raw_data=None,
                )
                self._stats["member_count"] += 1
            
            elif msg_type_code == 8:
                # 在线人数更新
                try:
                    count = _extract_number_from_payload(payload)
                    if count is not None:
                        log.debug(f"在线人数更新: {count}")
                except Exception:
                    pass
                continue
            
            else:
                # 其他/未知类型
                config = get_config()
                if config.websocket.log_unknown_messages:
                    log.debug(f"未知消息 type={msg_type_code} ({method_name}), len={len(payload)}")
                continue
            
            # 触发回调
            if comment and self.on_comment:
                try:
                    import asyncio
                    if asyncio.iscoroutinefunction(self.on_comment):
                        # 异步回调
                        asyncio.create_task(self.on_comment(comment))
                    else:
                        # 同步回调
                        self.on_comment(comment)
                except Exception as e:
                    log.error(f"评论回调异常: {e}")
    
    def _notify_status(self, connected: bool, msg: str):
        """通知连接状态变化"""
        if self.on_status_change:
            try:
                self.on_status_change(connected, msg)
            except Exception:
                pass


def _extract_number_from_payload(data: bytes) -> Optional[int]:
    """尝试从 protobuf 载荷中提取数字（用于在线人数等）"""
    # 简化实现：搜索连续数字字节
    for i in range(len(data) - 3):
        val = struct.unpack(">I", data[i:i+4])[0]
        if 1 <= val <= 10000000:  # 合理的在线人数范围
            return val
    return None
