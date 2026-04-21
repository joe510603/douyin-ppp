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
from websocket import WebSocketApp

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
        on_viewer_count_update: Optional[callable] = None,
        on_stop_requested: Optional[callable] = None,  # 房间离线时通知停止采集
        monitor_name: str = "",
        anchor_name: str = "",
        anchor_id: str = "",
    ):
        """
        Args:
            on_comment: 新评论回调函数 signature: (comment: LiveComment) -> None
            on_status_change: 连接状态变化回调 signature: (connected: bool, msg: str) -> None
            on_viewer_count_update: 在线人数更新回调 signature: (count: int, room_id: str) -> None
            on_stop_requested: 房间离线时的停止请求回调 signature: () -> None
            monitor_name: 监控账号名称（用于标识数据来源）
            anchor_name: 主播名称
            anchor_id: 主播 sec_user_id
        """
        self.on_comment = on_comment
        self.on_status_change = on_status_change
        self.on_viewer_count_update = on_viewer_count_update
        self.on_stop_requested = on_stop_requested
        self.monitor_name = monitor_name
        self.anchor_name = anchor_name
        self.anchor_id = anchor_id
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

    def _get_effective_cookie(self) -> str:
        """获取包含 ttwid 的有效 cookie"""
        config = get_config()
        douyin_cookie = config.douyin.cookie or ""
        video_cookie = config.douyin_video.cookie or ""
        if "ttwid" in douyin_cookie:
            return douyin_cookie
        if "ttwid" in video_cookie:
            return video_cookie
        return douyin_cookie or video_cookie

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
        
        # 尝试从 cookie 中提取 ttwid，如果没有则使用 effective cookie
        import re
        effective_cookie = self._get_effective_cookie()
        cookie_to_use = cookie if "ttwid" in cookie else effective_cookie

        if cookie_to_use:
            ttwid_match = re.search(r'ttwid=([^;]+)', cookie_to_use)
            if ttwid_match:
                ttwid = ttwid_match.group(1)
                log.debug(f"从 cookie 中提取到 ttwid: {ttwid[:20]}...")
            else:
                log.warning("提供的 cookie 中没有 ttwid，弹幕功能可能受限")
                # 仍然使用 cookie（可能没有 ttwid）作为备用
                cookie_to_use = cookie_to_use or cookie

        while self._running:
            # 1. 首次尝试：使用账号指定的 URL
            if not ws_url and current_ws_url_override:
                ws_url = current_ws_url_override
                log.info(f"使用账号指定的 WebSocket URL")

            # 2. 优先使用 Node.js 签名服务（高性能）
            cursor = ""
            internal_ext = ""
            heartbeat_duration = 0
            if not ws_url and use_signer_first:
                log.info("正在使用 Node.js 签名服务获取 WebSocket URL...")

                # 如果 room_id 位数不够（是 web_rid 而非真实 room_id），必须先获取真实 room_id
                # 或者如果没有 ttwid，也需要从页面获取
                need_fetch_page = (len(str(room_id)) < 18) or (not ttwid)
                if need_fetch_page:
                    reason = "room_id 位数不足(可能是 web_rid)" if len(str(room_id)) < 18 else "未找到 ttwid"
                    log.info(f"需要从直播间页面获取信息: {reason}")
                    # 构建直播间 URL（假设 room_id 是直播间的 web_rid）
                    live_url = f"https://live.douyin.com/{room_id}"
                    room_info = await self._fetch_room_info_from_page(live_url)
                    if room_info:
                        ttwid = room_info.get('ttwid')
                        real_room_id = room_info.get('room_id')
                        if real_room_id and real_room_id != room_id:
                            log.info(f"✅ 获取到真实 room_id: {real_room_id} (原 room_id={room_id})")
                            room_id = real_room_id
                        elif len(str(room_id)) < 18:
                            log.warning(f"⚠️ 未能获取真实 room_id，当前 room_id={room_id} 可能不正确，弹幕功能可能异常")

                # 【关键】先获取 cursor 和 internal_ext（参考 DouYin_Spider）
                webcast_info = await self._fetch_webcast_detail(room_id, cookie_to_use)
                if webcast_info:
                    cursor = webcast_info.get("cursor", "")
                    internal_ext = webcast_info.get("internal_ext", "")
                    heartbeat_duration = webcast_info.get("heartbeat_duration", 0)
                    log.info(f"✅ 获取 webcast detail: cursor={cursor[:30] if cursor else 'N/A'}..., internal_ext={len(internal_ext) if internal_ext else 0}B")
                    # 如果 cursor 为空，说明房间已下播（或 room_id 陈旧），尝试从直播间页面获取真实 room_id
                    if not cursor and not internal_ext:
                        log.warning(f"⚠️ 直播间 {room_id} 暂无直播内容，尝试从直播间页面获取真实 room_id...")
                        # 尝试直接请求直播间页面获取 room_id
                        real_room_id = await self._fetch_room_id_from_live_page(room_id, cookie_to_use)
                        if real_room_id and real_room_id != room_id:
                            log.info(f"✅ 找到真实 room_id: {real_room_id}，重新获取 webcast detail...")
                            room_id = real_room_id
                            # 用新 room_id 重新获取 webcast detail
                            webcast_info2 = await self._fetch_webcast_detail(room_id, cookie_to_use)
                            if webcast_info2:
                                cursor = webcast_info2.get("cursor", "")
                                internal_ext = webcast_info2.get("internal_ext", "")
                                heartbeat_duration = webcast_info2.get("heartbeat_duration", 0)
                                if not cursor and not internal_ext:
                                    log.warning(f"⚠️ 直播间 {room_id} 确实暂无直播内容，跳过采集")
                                    self._notify_status(False, "房间暂无直播内容")
                                    return
                        else:
                            log.warning(f"⚠️ 直播间 {room_id} 暂无直播内容（webcast detail 返回空），跳过采集")
                            self._notify_status(False, "房间暂无直播内容")
                            return

                ws_url = await self._fetch_signed_ws_url_from_signer(room_id, cursor=cursor, internal_ext=internal_ext)
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
                    ws_url = self._build_ws_url(room_id, cookie_to_use)

            try:
                log.info(f"正在连接直播间 {room_id}...")
                self._notify_status(True, f"连接中... {ws_url[:60]}...")

                # 构建请求头（参考 DouYin_Spider/dy_live/server.py）
                cookie_str = f"ttwid={ttwid}" if ttwid else cookie_to_use
                log.debug(f"WebSocket 连接 Cookie: {cookie_str[:30]}...")

                # ------------------------------------------------------------
                # 【核心修复】使用同步 websocket-client 在后台线程运行
                # 原因：websockets 的 async for 迭代器在无消息时阻塞事件循环，
                # 导致 Monitor 任务的 asyncio.sleep() 永远得不到执行。
                # 参考 DouYin_Spider/dy_live/server.py 的架构：
                #   - WebSocketApp 运行在独立线程
                #   - 消息通过 Queue 传递到主事件循环
                #   - 心跳在独立线程中发送
                # ------------------------------------------------------------
                import threading
                from queue import Queue, Empty
                from proto import dy_pb2

                msg_queue: Queue = Queue()
                shutdown_event = threading.Event()
                ws_thread_exception: Exception | None = None

                # 用于 ACK 的 WebSocket 实例（在子线程中持有）
                _sync_ws_for_ack: list = []

                def _on_ws_open(ws: WebSocketApp):
                    log.info(f"[WS-THREAD] WebSocket 已连接 room={room_id}")
                    _sync_ws_for_ack.append(ws)

                def _on_ws_message(ws: WebSocketApp, raw_msg):
                    """WebSocketApp 回调，在子线程中执行 — 只负责入队，不处理业务"""
                    if shutdown_event.is_set():
                        return
                    try:
                        msg_queue.put_nowait(raw_msg)
                    except Exception:
                        pass  # 队列满时丢弃（极端情况）

                def _on_ws_error(ws: WebSocketApp, error):
                    nonlocal ws_thread_exception
                    log.warning(f"[WS-THREAD] WebSocket 错误: {error}")
                    ws_thread_exception = error

                def _on_ws_close(ws: WebSocketApp, close_status_code, close_msg):
                    log.warning(f"[WS-THREAD] WebSocket 关闭: code={close_status_code}, msg={close_msg}")
                    shutdown_event.set()

                def _run_ws_thread():
                    """在子线程中运行 WebSocket 客户端（同步 API）"""
                    try:
                        # 构建 header
                        header_list = [
                            "Pragma: no-cache",
                            "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
                            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
                            "Upgrade: websocket",
                            "Cache-Control: no-cache",
                            "Connection: Upgrade",
                            "Origin: https://live.douyin.com",
                        ]
                        # 使用 ttwid 构建 Cookie
                        cookie_str = f"ttwid={ttwid}" if ttwid else cookie_to_use

                        app = WebSocketApp(
                            url=ws_url,
                            header=header_list,
                            cookie=cookie_str,
                            on_open=_on_ws_open,
                            on_message=_on_ws_message,
                            on_error=_on_ws_error,
                            on_close=_on_ws_close,
                        )

                        # 使用 run_forever 的内置 ping（标准 WebSocket ping 帧）
                        # 不要发送自定义 Protobuf hb 帧 — 抖音服务器可能不认识导致断开
                        app.run_forever(ping_interval=20, ping_timeout=10)
                    except Exception as e:
                        nonlocal ws_thread_exception
                        log.error(f"[WS-THREAD] 运行异常: {e}")
                        ws_thread_exception = e
                    finally:
                        shutdown_event.set()

                # 在后台线程中启动 WebSocket
                ws_thread = threading.Thread(target=_run_ws_thread, daemon=True)
                ws_thread.start()

                self._reconnect_mgr.record_success()
                self._stats["connect_time"] = datetime.now().isoformat()
                log.info(f"✅ 直播间 {room_id} 已连接（后台线程模式）")
                self._notify_status(True, f"已连接 - 直播间 {room_id}")

                # ------------------------------------------------------------
                # 主事件循环：消费消息队列 + 运行 Monitor 任务
                # 由于 WebSocket 在子线程运行，事件循环始终保持响应
                # ------------------------------------------------------------
                message_count = 0
                last_msg_time = import_time.time()
                no_message_start: float | None = None
                NO_MESSAGE_TIMEOUT = 120
                NO_MESSAGE_WARN = 60
                connection_start = import_time.time()
                ws_closed_expected = False  # 是否是主动关闭

                async def _monitor_timeout():
                    nonlocal no_message_start, message_count
                    last_warn_logged = False
                    last_timeout_logged = False
                    try:
                        for i in range(200):  # 最多运行 200 * 5 = 1000 秒
                            if not self._running:
                                return
                            await asyncio.sleep(5)
                            elapsed_connect = import_time.time() - connection_start
                            elapsed_no_msg = (import_time.time() - no_message_start
                                if no_message_start is not None and message_count > 0
                                else elapsed_connect)
                            if elapsed_no_msg >= NO_MESSAGE_TIMEOUT and not last_timeout_logged:
                                log.warning(f"⏰ 直播间 {room_id} 超过{NO_MESSAGE_TIMEOUT}秒无消息，标记为无数据状态")
                                self._notify_status(False, f"无数据({int(elapsed_no_msg)}s无消息)")
                                last_timeout_logged = True
                                last_warn_logged = True
                            elif elapsed_no_msg >= NO_MESSAGE_WARN and not last_warn_logged:
                                log.warning(f"⚠️ 直播间 {room_id} 超过{NO_MESSAGE_WARN}秒无消息（已连接{int(elapsed_connect)}s）")
                                last_warn_logged = True
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        log.error(f"[Monitor] 异常: {e}")

                timeout_task = asyncio.create_task(_monitor_timeout())
                log.info(f"[Monitor] 启动超时监控任务 for room={room_id}")

                # 获取事件循环引用，用于从子线程上下文发送 ACK
                loop = asyncio.get_running_loop()

                async def _send_ack_from_thread(log_id: int, internal_ext: str):
                    """从子线程调用：在主事件循环中发送 ACK"""
                    sync_ws = _sync_ws_for_ack[0] if _sync_ws_for_ack else None
                    if not sync_ws:
                        return
                    ack_frame = dy_pb2.PushFrame()
                    ack_frame.payloadType = "ack"
                    ack_frame.logId = log_id
                    ack_frame.payload = internal_ext.encode('utf-8')
                    sync_ws.send(ack_frame.SerializeToString(), opcode=0x02)

                # 消费消息队列（关键：用 asyncio.to_thread 避免阻塞事件循环）
                # 如果直接用 msg_queue.get(timeout=1)，会同步阻塞整个 asyncio 事件循环，
                # 导致 uvicorn/NiceGUI 无法处理 HTTP 请求（curl 超时）
                running = True
                while running and self._running:
                    try:
                        # 在线程池中执行同步的 Queue.get()，让事件循环保持响应
                        raw_msg = await asyncio.to_thread(msg_queue.get, timeout=1)
                    except Empty:
                        # 检查子线程是否异常退出
                        if shutdown_event.is_set():
                            if ws_thread_exception:
                                raise ws_thread_exception
                            break
                        continue

                    now = import_time.time()
                    if message_count == 0:
                        no_message_start = now
                    message_count += 1
                    last_msg_time = now

                    try:
                        await self._handle_raw_message_async(
                            raw_msg, room_id, _send_ack_from_thread
                        )
                    except Exception as e:
                        log.error(f"处理消息异常: {e}", exc_info=True)

                    # 让出事件循环，确保 Monitor 任务有机会运行
                    await asyncio.sleep(0)

                    elapsed_no_msg = now - no_message_start if no_message_start else 0
                    if elapsed_no_msg >= NO_MESSAGE_TIMEOUT:
                        log.warning(f"⏰ 直播间 {room_id} 超过{NO_MESSAGE_TIMEOUT}秒无消息")
                        self._notify_status(False, f"无数据({int(elapsed_no_msg)}s无消息)")
                        no_message_start = None
                    elif elapsed_no_msg >= NO_MESSAGE_WARN and message_count > 1:
                        log.warning(f"⚠️ 直播间 {room_id} 超过{NO_MESSAGE_WARN}秒无消息")

                    # web_rid 重新检测
                    if len(str(room_id)) <= 15 and elapsed_no_msg >= NO_MESSAGE_WARN:
                        try:
                            live_url = f"https://live.douyin.com/{room_id}"
                            room_info = await self._fetch_room_info_from_page(live_url)
                            if room_info and room_info.get("room_id") and room_info["room_id"] != room_id:
                                real_room_id = room_info["room_id"]
                                log.info(f"🔄 成功获取真实 room_id={real_room_id}，将重新连接...")
                                # 通知子线程关闭
                                shutdown_event.set()
                                room_id = real_room_id
                                ws_url = None
                                continue
                        except Exception as e:
                            log.warning(f"重新获取 room_id 失败: {e}")

                    # 检查子线程状态
                    if shutdown_event.is_set():
                        break

                # 清理
                timeout_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(timeout_task), timeout=2)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                self._ws = None
                _sync_ws_for_ack.clear()
                msg_queue.queue.clear()

                if not self._running:
                    break

                # 检查子线程是否有异常
                if ws_thread_exception:
                    raise ws_thread_exception

                self._reconnect_mgr.record_failure()

            except websockets.ConnectionClosed as e:
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
        从直播间页面获取 ttwid 和 room_id（使用 httpx 同步请求，在线程中运行）
        
        Args:
            live_url: 直播间 URL (https://live.douyin.com/xxx)
        
        Returns:
            dict: {'ttwid': str, 'room_id': str} 或 None
        """
        def _fetch_sync():
            import re
            try:
                import httpx as _httpx
                
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Accept-Encoding': 'gzip, deflate',
                    'Referer': 'https://live.douyin.com/',
                }
                # 注意：带 Cookie 请求 live.douyin.com 会返回空页面(6KB)，
                # 不带 Cookie 反而能获取到完整HTML(1MB+)，其中包含 roomId
                
                with _httpx.Client(timeout=15, follow_redirects=True) as client:
                    resp = client.get(live_url, headers=headers)
                    if resp.status_code != 200:
                        log.error(f"获取直播间页面失败: HTTP {resp.status_code}")
                        return None
                    
                    html = resp.text
                    if len(html) < 50000:
                        log.warning(f"httpx 返回页面太短({len(html)}B)，可能被验证码拦截，尝试 Playwright")
                        return _fetch_with_playwright()
                    
                    # 1. 从 Set-Cookie 中提取 ttwid
                    ttwid = None
                    for header_value in resp.headers.get_list('set-cookie'):
                        if 'ttwid=' in header_value:
                            ttwid_match = re.search(r'ttwid=([^;]+)', header_value)
                            if ttwid_match:
                                ttwid = ttwid_match.group(1)
                                break
                    
                    # 2. 从页面内容中提取真实 room_id
                    room_id = _extract_room_id_from_html(html, live_url)
                    
                    if not room_id:
                        log.error(f"无法从页面中提取 room_id, html_len={len(html)}")
                        return None
                    
                    log.info(f"从页面获取到 ttwid={ttwid[:20] if ttwid else 'None'}..., room_id={room_id}")
                    
                    return {
                        'ttwid': ttwid,
                        'room_id': room_id
                    }
                    
            except Exception as e:
                log.error(f"httpx 获取直播间信息失败: {e}")
                return _fetch_with_playwright()
        
        def _fetch_with_playwright():
            """使用 Playwright 获取 room_id（httpx 失败时的回退方案）"""
            import re
            try:
                from playwright.sync_api import sync_playwright
                from pathlib import Path

                browser_data_dir = Path("data/browser_data")
                browser_data_dir.mkdir(parents=True, exist_ok=True)

                with sync_playwright() as p:
                    # 优先使用持久化上下文（包含登录状态）
                    try:
                        context = p.chromium.launch_persistent_context(
                            user_data_dir=str(browser_data_dir),
                            headless=True,
                            viewport={'width': 1680, 'height': 1050},
                            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
                            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                        browser = None
                    except Exception:
                        # 回退：使用独立 browser
                        browser = p.chromium.launch(
                            headless=True,
                            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
                        )
                        context = browser.new_context(
                            viewport={'width': 1680, 'height': 1050},
                            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
                        )
                        page = context.new_page()

                    try:
                        page.goto(live_url, wait_until='domcontentloaded', timeout=15000)
                        page.wait_for_timeout(3000)

                        # 从 JS 变量提取 room_id
                        live_page_data = page.evaluate('''() => {
                            try {
                                const renderEl = document.getElementById('RENDER_DATA');
                                if (renderEl) {
                                    const text = decodeURIComponent(renderEl.textContent);
                                    const m = text.match(/"room_id"\\s*:\\s*"?(\d{18,})"?/);
                                    if (m) return {room_id: m[1], ttwid: null};
                                }
                            } catch(e) {}
                            // 回退：尝试其他方式
                            try {
                                const nextData = document.getElementById('__NEXT_DATA__');
                                if (nextData) {
                                    const data = JSON.parse(nextData.textContent);
                                    const rid = data?.props?.pageProps?.room?.room_id;
                                    if (rid) return {room_id: String(rid), ttwid: null};
                                }
                            } catch(e) {}
                            return null;
                        }''')

                        ttwid = None
                        room_id = None
                        if live_page_data and live_page_data.get('room_id'):
                            room_id = str(live_page_data['room_id'])
                            log.info(f"Playwright 获取到真实 room_id={room_id}")
                        else:
                            # 从 HTML 提取
                            html = page.content()
                            room_id = _extract_room_id_from_html(html, live_url)

                        # 提取 ttwid
                        for cookie in context.cookies():
                            if cookie['name'] == 'ttwid':
                                ttwid = cookie['value']
                                break

                        if room_id:
                            log.info(f"Playwright 从页面获取到 room_id={room_id}")
                            return {'ttwid': ttwid, 'room_id': room_id}
                        else:
                            log.error(f"Playwright 也无法提取 room_id")
                            return None
                    finally:
                        page.close()
                        context.close()
                        if browser:
                            browser.close()
            except Exception as e:
                log.error(f"Playwright 获取直播间信息失败: {e}")
                return None
        
        def _extract_room_id_from_html(html, url):
            """从 HTML 内容提取真实 room_id"""
            room_id = None
            web_rid_match = re.search(r'live\.douyin\.com/(\d+)', url)
            web_rid = web_rid_match.group(1) if web_rid_match else None
            
            # 策略1: 找 web_rid 附近的 roomId
            if web_rid:
                nearby = re.findall(r'roomId.{0,50}?web_rid.{0,30}?' + re.escape(web_rid), html)
                if nearby:
                    context_match = re.search(r'roomId[^\d]*(\d{18,20})', nearby[0])
                    if context_match:
                        room_id = context_match.group(1)
            # 策略2: 转义格式
            if not room_id:
                escaped_match = re.findall(r'roomId\\\\\":\\\\\"(\d{18,20})\\\\\"', html)
                if escaped_match:
                    room_id = escaped_match[0]
            # 策略3: HTML 实体格式
            if not room_id:
                entity_match = re.findall(r'roomId&quot;:&quot;(\d{18,20})&quot;', html)
                if entity_match:
                    room_id = entity_match[0]
            # 策略4: 回退
            if not room_id:
                all_matches = re.findall(r'roomId.*?(\d{18,20})', html)
                if all_matches:
                    room_id = all_matches[0]
            return room_id
        
        try:
            import asyncio
            return await asyncio.to_thread(_fetch_sync)
        except Exception as e:
            log.error(f"在线程中运行页面获取失败: {e}")
            return None

    async def _fetch_signed_ws_url_from_signer(self, room_id: str, cursor: str = "", internal_ext: str = "") -> str | None:
        """
        使用 Node.js 签名服务获取 WebSocket URL（高性能方式）

        参考 DouYin_Spider/dy_live/server.py 的完整参数列表
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
            # 【关键参数】参考 DouYin_Spider 的完整参数列表
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
                # 以下是参考项目添加的关键参数
                "aid": "6383",
                "device_platform": "web",
                "cookie_enabled": "true",
                "screen_width": "2560",
                "screen_height": "1440",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": "Mozilla",
                "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
                "browser_online": "true",
                "tz_name": "Asia/Shanghai",
                "update_version_code": version_code,
                "app_name": "douyin_web",
                "endpoint": "live_pc",
                "support_wrds": "1",
                # cursor 和 internal_ext 来自 get_webcast_detail API
                "cursor": cursor,
                "internal_ext": internal_ext,
                "heartbeatDuration": "0",
                "live_reason": "",
                "im_path": "/webcast/im/fetch/",
                "need_persist_msg_count": "15",
                "insert_task_id": "",
                "sub_room_id": "",
                "sub_channel_id": "",
                "device_type": "",
                "ac": "",
                "host": "https://live.douyin.com",
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

    async def _fetch_webcast_detail(self, room_id: str, cookie: str = "", user_unique_id: str = "") -> dict | None:
        """
        获取直播间的 cursor 和 internal_ext（参考 DouYin_Spider/dy_apis/douyin_api.py）

        调用 https://live.douyin.com/webcast/im/fetch/ 返回 Protobuf Response，
        其中包含 cursor、internalExt、heartbeatDuration 等关键参数。
        这些参数必须传给 WebSocket 连接，服务器才知道从哪个位置开始推送消息。

        关键修复：需要 a_bogus 签名 + msToken，否则返回 Content-Length: 0。
        """
        try:
            import httpx
            from proto import dy_pb2
            import urllib.parse

            # 从 cookie 提取 msToken
            ms_token = ""
            if cookie:
                for part in cookie.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k.strip().lower() == "mstoken":
                            ms_token = urllib.parse.unquote(v.strip())
                            break

            # user_unique_id：优先使用传入的值（来自浏览器检测），否则生成随机
            if not user_unique_id:
                user_unique_id = str(random.randint(7300000000000000000, 7999999999999999999))

            # 构建请求参数（参考 DouYin_Spider 的 get_webcast_detail）
            base_params = {
                "resp_content_type": "protobuf",
                "did_rule": "3",
                "device_id": "",
                "app_name": "douyin_web",
                "endpoint": "live_pc",
                "support_wrds": "1",
                "user_unique_id": str(user_unique_id),
                "identity": "audience",
                "need_persist_msg_count": "15",
                "insert_task_id": "",
                "live_reason": "",
                "room_id": str(room_id),
                "version_code": "180800",
                "last_rtt": "0",
                "live_id": "1",
                "aid": "6383",
                "fetch_rule": "1",
                "cursor": "",
                "internal_ext": "",
                "device_platform": "web",
                "cookie_enabled": "true",
                "screen_width": "2560",
                "screen_height": "1440",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": "Mozilla",
                "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
                "browser_online": "true",
                "tz_name": "Asia/Shanghai",
            }

            # 生成 a_bogus 签名（关键！无签名服务器返回空）
            a_bogus = ""
            try:
                from ..utils.signer import generate_a_bogus
                params_str = "&".join(f"{k}={v}" for k, v in base_params.items())
                a_bogus = generate_a_bogus(params_str, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")
            except Exception as e:
                log.debug(f"生成 a_bogus 失败: {e}")

            if a_bogus:
                base_params["a_bogus"] = a_bogus
            if ms_token:
                base_params["msToken"] = ms_token

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Origin": "https://live.douyin.com",
                "Referer": f"https://live.douyin.com/{room_id}",
            }

            # 将 cookie 字符串转换为 httpx 可接受的格式
            cookie_dict = {}
            if cookie:
                for part in cookie.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookie_dict[k.strip()] = v.strip()

            # 同步请求在异步函数中运行
            def _fetch_sync():
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(
                        "https://live.douyin.com/webcast/im/fetch/",
                        params=base_params,
                        headers=headers,
                        cookies=cookie_dict,
                    )
                    return resp.content

            raw_bytes = await asyncio.get_running_loop().run_in_executor(None, _fetch_sync)

            if not raw_bytes or len(raw_bytes) == 0:
                log.warning(f"[webcast_detail] 直播间 {room_id} 返回空（Content-Length: 0）")
                return None

            response = dy_pb2.Response()
            try:
                response.ParseFromString(raw_bytes)
            except Exception:
                # 抖音可能返回 (1) gzip 压缩的 protobuf (2) JSON 响应
                try:
                    decompressed = gzip.decompress(raw_bytes)
                    response.ParseFromString(decompressed)
                    log.debug(f"[webcast_detail] gzip 解压成功，直播间 {room_id}")
                except Exception:
                    # 尝试 JSON 格式（抖音某些版本返回 JSON 而非 protobuf）
                    try:
                        json_text = raw_bytes.decode('utf-8')
                        if json_text.startswith('{'):
                            json_data = json.loads(json_text)
                            # JSON 响应通常是 {"data": [], "status_code": 0} 表示无数据
                            # 这不是错误，只是直播间暂无推送数据
                            log.debug(f"[webcast_detail] 服务器返回 JSON（非protobuf，直播间 {room_id} 可能无数据）: {json_text[:100]}")
                            return None
                    except Exception:
                        pass
                    log.debug(f"[webcast_detail] 原始响应前32字节 hex: {raw_bytes[:32].hex()}")
                    raise

            log.info(f"[webcast_detail] cursor={response.cursor[:50] if response.cursor else 'N/A'}, internalExt_len={len(response.internalExt)}, heartbeatDuration={response.heartbeatDuration}, needAck={response.needAck}")

            return {
                "cursor": str(response.cursor),
                "internal_ext": response.internalExt,
                "heartbeat_duration": response.heartbeatDuration,
                "need_ack": response.needAck,
            }

        except Exception as e:
            log.error(f"获取 webcast detail 失败: {e}", exc_info=True)
            return None

    async def _fetch_room_id_from_live_page(self, room_id_or_web_rid: str, cookie: str = "") -> str | None:
        """
        从直播间页面获取真实 room_id（18位长ID）。

        当 detector 返回的 room_id 是陈旧的 web_rid 或已下播房间时，
        此方法通过请求直播间页面来获取当前真实的 room_id。
        """
        try:
            import httpx, re

            def _fetch_sync():
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Referer": "https://live.douyin.com/",
                }
                cookies = {}
                if cookie:
                    for part in cookie.split(";"):
                        part = part.strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            cookies[k.strip()] = v.strip()

                with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                    resp = client.get(
                        f"https://live.douyin.com/{room_id_or_web_rid}",
                        headers=headers,
                        cookies=cookies,
                    )
                    return resp.text

            html = await asyncio.get_running_loop().run_in_executor(None, _fetch_sync)

            # 策略1: 从 script 标签的 JSON 中提取 roomId（参考 DouYin_Spider）
            room_id_match = re.search(r'"roomId"\s*:\s*"?(\d{18,20})"?', html)
            if room_id_match:
                return room_id_match.group(1)

            # 策略2: 从 script 标签中提取 room_id（标准格式）
            room_id_match2 = re.search(r'"room_id"\s*:\s*"?(\d{18,20})"?', html)
            if room_id_match2:
                return room_id_match2.group(1)

            # 策略3: 查找 room_id 和 web_rid 关联（roomId ... web_rid XXX 格式）
            room_id_match3 = re.search(r'roomId[^"]*?"(\d{18,20})"[^}]*?web_rid[^"]*?"' + re.escape(room_id_or_web_rid), html)
            if room_id_match3:
                return room_id_match3.group(1)

            # 策略4: 从 HTML 中查找 18+ 位数字的房间 ID
            long_ids = re.findall(r'\b(\d{18,20})\b', html)
            for rid in long_ids[:5]:  # 取前5个
                if rid == room_id_or_web_rid:
                    continue
                # 验证这个 ID 对应的房间是否有效
                detail = self._check_room_by_webcast_detail(rid)
                if detail:
                    return rid

            return None

        except Exception as e:
            log.debug(f"从直播间页面获取 room_id 失败: {e}")
            return None

    def _check_room_by_webcast_detail(self, room_id: str) -> bool:
        """同步检查房间是否有效（通过 webcast detail）"""
        try:
            import httpx, gzip, re
            from proto import dy_pb2

            params = {
                "room_id": room_id, "aid": "6383", "app_name": "douyin_web",
                "version_code": "180800", "live_id": "1", "did_rule": "3",
                "device_platform": "web", "endpoint": "live_pc",
                "support_wrds": "1", "user_unique_id": "7500000000000000001",
                "identity": "audience", "need_persist_msg_count": "15",
                "fetch_rule": "1", "cursor": "", "internal_ext": "",
                "resp_content_type": "protobuf",
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://live.douyin.com",
                "Referer": f"https://live.douyin.com/{room_id}",
            }
            with httpx.Client(timeout=5.0) as client:
                resp = client.get("https://live.douyin.com/webcast/im/fetch/",
                                  params=params, headers=headers)
                if len(resp.content) > 0:
                    r = dy_pb2.Response()
                    r.ParseFromString(resp.content)
                    return bool(r.messagesList or r.cursor)
            return False
        except Exception:
            return False
            return None

    async def _fetch_signed_ws_url(self, room_id: str) -> str | None:
        """
        使用 Playwright 通过浏览器获取抖音直播间的 WebSocket 签名 URL。
        这是获取有效签名的最可靠方式。
        """
        config = get_config()
        # 优先使用有 ttwid 的 Cookie
        douyin_cookie = config.douyin.cookie or ""
        video_cookie = config.douyin_video.cookie or ""
        cookie_str = video_cookie if "ttwid" in video_cookie else (douyin_cookie if "ttwid" in douyin_cookie else douyin_cookie)

        # 同步版本在异步函数中运行
        def _fetch_with_playwright():
            import re
            from urllib.parse import unquote
            from pathlib import Path

            try:
                from playwright.sync_api import sync_playwright

                browser_data_dir = Path("data/browser_data")
                browser_data_dir.mkdir(parents=True, exist_ok=True)

                with sync_playwright() as p:
                    # 优先使用持久化上下文（包含登录状态）
                    try:
                        context = p.chromium.launch_persistent_context(
                            user_data_dir=str(browser_data_dir),
                            headless=True,
                            viewport={'width': 1680, 'height': 1050},
                            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                            args=[
                                '--no-sandbox',
                                '--disable-setuid-sandbox',
                                '--disable-dev-shm-usage',
                                '--disable-blink-features=AutomationControlled',
                            ],
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                        browser = None
                    except Exception:
                        # 回退：使用独立 browser + new_context
                        browser = p.chromium.launch(
                            headless=True,
                            args=[
                                '--no-sandbox',
                                '--disable-setuid-sandbox',
                                '--disable-dev-shm-usage',
                                '--disable-blink-features=AutomationControlled',
                            ]
                        )
                        context = browser.new_context(
                            viewport={'width': 1680, 'height': 1050},
                            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        )
                        page = context.new_page()

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

                    # 创建页面
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
                        if browser:
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
    
    async def _handle_raw_message_async(self, raw_data: bytes, room_id: str, ack_callback=None):
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
            if response.needAck and ack_callback and log_id:
                log.info(f"[ACK] needAck=True, log_id={log_id}, internalExt_len={len(internal_ext)}")
                await ack_callback(log_id, internal_ext)
            
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
        """发送 ACK 响应（参考 DouYin_Spider 的实现）"""
        try:
            from proto import dy_pb2

            # 构建 ACK PushFrame（参考 dy_live/server.py）
            ack_frame = dy_pb2.PushFrame()
            ack_frame.payloadType = "ack"
            ack_frame.logId = log_id
            ack_frame.payload = internal_ext.encode('utf-8')

            ack_data = ack_frame.SerializeToString()
            await ws.send(ack_data)
            log.debug(f"已发送 ACK: log_id={log_id}")
            
        except Exception as e:
            log.error(f"发送 ACK 失败: {e}")
    
    def _extract_user_id(self, user) -> tuple[str, str]:
        """
        从 User proto 中提取用户 ID。

        Returns:
            tuple[short_id, display_id]:
            - short_id: 抖音号（用户设置的，仅当有效时才返回）
            - display_id: displayId 或其他备用 ID
        """
        if not user:
            return "", ""

        short_id = ""
        display_id = ""

        try:
            # 1. 优先提取 shortId（抖音号）
            if hasattr(user, 'shortId') and user.shortId:
                short_id = str(user.shortId)

            # 2. 提取 displayId 作为备用
            if hasattr(user, 'displayId') and user.displayId:
                display_id = str(user.displayId)
            # 3. 备用：尝试 idStr
            elif hasattr(user, 'idStr') and user.idStr:
                display_id = str(user.idStr)
            # 4. 备用：尝试 id（内部数值）
            elif hasattr(user, 'id') and user.id:
                display_id = str(user.id)

        except Exception:
            pass

        return short_id, display_id

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
                short_id, display_id = self._extract_user_id(chat_msg.user) if chat_msg.HasField('user') else ("", "")
                content = chat_msg.content

                comment = LiveComment(
                    content=content,
                    message_type=MessageType.CHAT,
                    room_id=room_id,
                    user_id=short_id or display_id,  # 优先用抖音号，没有则用 displayId
                    user_short_id=short_id,  # 真正的抖音号
                    user_nickname=user_name,
                    user_avatar=getattr(chat_msg.user, 'avatarUrl', '') if chat_msg.HasField('user') else '',
                    monitor_name=self.monitor_name,
                    anchor_name=self.anchor_name,
                    anchor_id=self.anchor_id,
                    raw_data=chat_msg.SerializeToString().hex()[:200],
                )
                self._stats["chat_count"] += 1
                log.info(f"💬 [{short_id or display_id}/{user_name}]: {content}")

            elif method == 'WebcastGiftMessage':
                # 礼物消息
                gift_msg = dy_pb2.GiftMessage()
                gift_msg.ParseFromString(payload)

                user_name = getattr(gift_msg.user, 'nickName', '未知用户') if gift_msg.HasField('user') else "未知用户"
                short_id, display_id = self._extract_user_id(gift_msg.user) if gift_msg.HasField('user') else ("", "")
                gift_name = "礼物"

                comment = LiveComment(
                    content=f"[礼物] {user_name} 送出 {gift_name}",
                    message_type=MessageType.GIFT,
                    room_id=room_id,
                    user_id=short_id or display_id,
                    user_short_id=short_id,
                    user_nickname=user_name,
                    user_avatar=getattr(gift_msg.user, 'avatarUrl', '') if gift_msg.HasField('user') else '',
                    monitor_name=self.monitor_name,
                    anchor_name=self.anchor_name,
                    anchor_id=self.anchor_id,
                    gift_name=gift_name,
                    raw_data=None,
                )
                self._stats["gift_count"] += 1
                log.info(f"🎁 [{short_id or display_id}/{user_name}] 送出礼物")

            elif method == 'WebcastMemberMessage':
                # 进场消息
                member_msg = dy_pb2.MemberMessage()
                member_msg.ParseFromString(payload)

                user_name = getattr(member_msg.user, 'nickName', '未知用户') if member_msg.HasField('user') else "未知用户"
                short_id, display_id = self._extract_user_id(member_msg.user) if member_msg.HasField('user') else ("", "")

                comment = LiveComment(
                    content=f"[进场] {user_name}",
                    message_type=MessageType.MEMBER,
                    room_id=room_id,
                    user_id=short_id or display_id,
                    user_short_id=short_id,
                    user_nickname=user_name,
                    user_avatar=getattr(member_msg.user, 'avatarUrl', '') if member_msg.HasField('user') else '',
                    monitor_name=self.monitor_name,
                    anchor_name=self.anchor_name,
                    anchor_id=self.anchor_id,
                    raw_data=None,
                )
                self._stats["member_count"] += 1
                log.debug(f"🚪 [{short_id or display_id}/{user_name}] 进入直播间")

            elif method == 'WebcastLikeMessage':
                # 点赞消息
                like_msg = dy_pb2.LikeMessage()
                like_msg.ParseFromString(payload)

                user_name = getattr(like_msg.user, 'nickName', '未知用户') if like_msg.HasField('user') else "未知用户"
                short_id, display_id = self._extract_user_id(like_msg.user) if like_msg.HasField('user') else ("", "")

                comment = LiveComment(
                    content=f"[点赞] {user_name}",
                    message_type=MessageType.LIKE,
                    room_id=room_id,
                    user_id=short_id or display_id,
                    user_short_id=short_id,
                    user_nickname=user_name,
                    user_avatar=getattr(like_msg.user, 'avatarUrl', '') if like_msg.HasField('user') else '',
                    monitor_name=self.monitor_name,
                    anchor_name=self.anchor_name,
                    anchor_id=self.anchor_id,
                    raw_data=None,
                )
                log.debug(f"👍 [{short_id or display_id}/{user_name}] 点赞")
                
            elif method == 'WebcastRoomStatsMessage':
                # 在线人数更新（RoomStatsMessage）
                try:
                    stats_msg = dy_pb2.RoomStatsMessage()
                    stats_msg.ParseFromString(payload)

                    # 提取在线人数：优先 numeric 字段，其次 string 字段
                    count = None
                    if stats_msg.total > 0:
                        count = stats_msg.total
                    elif stats_msg.displayValue > 0:
                        count = stats_msg.displayValue
                    elif stats_msg.displayLong:
                        try:
                            count = int(stats_msg.displayLong.replace(",", ""))
                        except (ValueError, AttributeError):
                            pass
                    elif stats_msg.displayMiddle:
                        try:
                            count = int(stats_msg.displayMiddle.replace(",", ""))
                        except (ValueError, AttributeError):
                            pass

                    if count is not None and count > 0:
                        log.info(f"📱 [RoomStats] 人数={count} (displayValue={stats_msg.displayValue}, total={stats_msg.total})")
                        if self.on_viewer_count_update:
                            try:
                                import asyncio
                                if asyncio.iscoroutinefunction(self.on_viewer_count_update):
                                    asyncio.create_task(self.on_viewer_count_update(count, room_id))
                                else:
                                    self.on_viewer_count_update(count, room_id)
                            except Exception as e:
                                log.error(f"在线人数更新回调异常: {e}")
                except Exception as e:
                    log.error(f"处理在线人数消息异常: {e}")

            elif method == 'WebcastRoomUserSeqMessage':
                # 在线人数更新（RoomUserSeqMessage — 抖音使用此消息传真实人数）
                try:
                    seq_msg = dy_pb2.RoomUserSeqMessage()
                    seq_msg.ParseFromString(payload)

                    count = None
                    # 字段优先级：total(实时在线) > popularity(展示热度) > totalStr
                    # 注意：totalUser 是累计入场人数，不反映当前在线，不要用
                    if seq_msg.total > 0:
                        count = seq_msg.total
                    elif seq_msg.popularity > 0:
                        count = seq_msg.popularity
                    elif seq_msg.totalStr:
                        try:
                            count = int(seq_msg.totalStr.replace(",", ""))
                        except (ValueError, AttributeError):
                            pass

                    if count is not None and count > 0:
                        log.info(f"📊 [RoomUserSeq] 人数={count} (popularity={seq_msg.popularity}, total={seq_msg.total}, totalUser={seq_msg.totalUser}, totalStr={seq_msg.totalStr})")
                        if self.on_viewer_count_update:
                            try:
                                import asyncio
                                if asyncio.iscoroutinefunction(self.on_viewer_count_update):
                                    asyncio.create_task(self.on_viewer_count_update(count, room_id))
                                else:
                                    self.on_viewer_count_update(count, room_id)
                            except Exception as e:
                                log.error(f"在线人数更新回调异常: {e}")
                    else:
                        log.debug(f"📊 [RoomUserSeq] 人数为0 (popularity={seq_msg.popularity}, total={seq_msg.total}, totalUser={seq_msg.totalUser})")
                except Exception as e:
                    log.error(f"处理RoomUserSeq消息异常: {e}")

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
        """处理原始二进制消息（回退路径，无法提取 user_id）"""
        messages = _parse_push_frame(payload)

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
                    monitor_name=self.monitor_name,
                    anchor_name=self.anchor_name,
                    anchor_id=self.anchor_id,
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
                    monitor_name=self.monitor_name,
                    anchor_name=self.anchor_name,
                    anchor_id=self.anchor_id,
                    raw_data=None,
                )
                self._stats["member_count"] += 1
            
            elif msg_type_code == 8:
                # 在线人数更新
                try:
                    count = _extract_number_from_payload(payload)
                    log.info(f"📱 收到在线人数消息，payload长度={len(payload)}, 提取结果={count}")
                    if count is not None:
                        log.info(f"✅ 在线人数更新: {count}")
                        # 触发在线人数更新回调
                        if self.on_viewer_count_update:
                            try:
                                import asyncio
                                if asyncio.iscoroutinefunction(self.on_viewer_count_update):
                                    asyncio.create_task(self.on_viewer_count_update(count, room_id))
                                else:
                                    self.on_viewer_count_update(count, room_id)
                            except Exception as e:
                                log.error(f"在线人数更新回调异常: {e}")
                    else:
                        log.warning(f"⚠️ 无法从payload提取在线人数")
                except Exception as e:
                    log.error(f"处理在线人数消息异常: {e}")
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
        # 房间离线时：通知 app 停止采集（避免重复启动）
        # 注意：on_stop_requested 可能是 async 函数，需要用 create_task
        if not connected and "暂无直播内容" in msg:
            if self.on_stop_requested:
                try:
                    cb = self.on_stop_requested
                    if asyncio.iscoroutinefunction(cb):
                        asyncio.create_task(cb())
                    else:
                        cb()
                except Exception:
                    pass


def _extract_number_from_payload(data: bytes) -> Optional[int]:
    """尝试从 protobuf 载荷中提取数字（用于在线人数等）"""
    # 改进实现：搜索连续数字字节，优先返回较小的合理值
    # 抖音在线人数通常在 1-100000 之间
    candidates = []
    for i in range(len(data) - 3):
        try:
            val = struct.unpack(">I", data[i:i+4])[0]
            if 1 <= val <= 100000:  # 合理的在线人数范围
                candidates.append(val)
        except:
            continue
    
    # 如果找到多个候选值，选择最常见的一个（排除明显异常的大值）
    if candidates:
        from collections import Counter
        # 过滤掉异常大的值（超过10万的可能是其他字段）
        reasonable = [c for c in candidates if c <= 50000]
        if reasonable:
            counter = Counter(reasonable)
            most_common = counter.most_common(1)[0][0]
            log.debug(f"提取到在线人数候选: {candidates[:5]}, 选择: {most_common}")
            return most_common
        else:
            return candidates[0]
    
    return None
