"""开播状态检测器 — 调用抖音 API 检测账号是否正在直播"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
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


class LiveDetector:
    """
    抖音直播间开播状态检测器。
    
    通过调用抖音 Web API 检测指定用户是否正在直播。
    支持定时轮询和单次检测。
    """
    
    # 抖音用户信息 API
    USER_INFO_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
    # 直播间状态 API（备用）
    LIVE_ROOM_API = "https://webcast.douyin.com/webcast/room/web/enter/"
    
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            config = get_config()
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json",
            }
            if config.douyin.cookie:
                headers["Cookie"] = config.douyin.cookie
            
            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=10,
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
        config = get_config()
        cookie = config.douyin.cookie
        cookie_status = get_cookie_status(cookie)
        if cookie_status == "expired":
            log.warning(f"Cookie 已失效，检测结果可能不准确")
        elif cookie_status == "expiring_soon":
            log.warning(f"Cookie 即将在 24h 内过期，检测结果可能不准确")

        try:
            client = await self._get_client()
            
            # 方式1：通过用户主页获取直播间信息
            params = {
                "sec_user_id": sec_user_id,
                "device_platform": "webapp",
                "aid": "6383",
                "cookie_enabled": "true",
            }
            
            resp = await client.get(
                self.USER_INFO_API,
                params=params,
            )
            resp.raise_for_status()
            
            # 记录完整响应用于调试
            try:
                with open(f"/tmp/douyin_api_{sec_user_id[:10]}.json", "w") as f:
                    f.write(resp.text)
                log.debug(f"API 响应已保存到 /tmp/douyin_api_{sec_user_id[:10]}.json")
            except:
                pass
            
            try:
                data = resp.json()
            except Exception as json_err:
                log.error(f"JSON 解析失败 [{sec_user_id[:20]}...]: {json_err}")
                log.error(f"响应内容: {resp.text[:200]}")
                return info
            
            if data.get("status_code") == 0 and data.get("user"):
                user_data = data["user"]
                info.anchor_nickname = user_data.get("nickname", "")

                # 检查 room_data 字段（抖音 API 新版本将直播信息存在这里）
                room_data_str = user_data.get("room_data")
                if room_data_str:
                    try:
                        room_data = json.loads(room_data_str) if isinstance(room_data_str, str) else room_data_str
                    except json.JSONDecodeError:
                        room_data = None
                else:
                    # 兼容旧版本：检查 stream_room 等字段
                    room_data = None
                    possible_keys = ["stream_room", "live_room", "room"]
                    for key in possible_keys:
                        if key in user_data and user_data[key]:
                            room_data = user_data[key]
                            break

                if room_data and isinstance(room_data, dict):
                    log.debug(f"[{info.anchor_nickname}] 直播间数据: {str(room_data)[:300]}")

                    # 检查直播状态 (status=2 表示直播中)
                    room_status = room_data.get("status", 0)
                    log.debug(f"[{info.anchor_nickname}] 直播间状态: {room_status}")

                    if room_status == 2:
                        info.status = True
                        info.room_id = str(room_data.get("id_str", room_data.get("room_id", user_data.get("room_id_str", ""))))
                        # 获取直播标题
                        info.title = room_data.get("title", "")
                        if not info.title:
                            # 尝试从其他字段获取
                            info.title = user_data.get("room_id", "")
                        info.user_count = room_data.get("user_count", 0)
                        info.total_user = room_data.get("total_user", 0)
                        info.raw_data = room_data

                        log.info(f"检测到开播 [{info.anchor_nickname}]: {info.title} (房间 {info.room_id}, 在线 {info.user_count} 人)")
                    else:
                        log.debug(f"[{info.anchor_nickname}] 未开播 (status={room_status})")
                else:
                    log.debug(f"[{info.anchor_nickname}] 未找到直播间信息或数据为空")
            else:
                log.warning(f"API 返回异常状态: {data.get('status_code')}, 消息: {data.get('status_msg', 'unknown')}")
            
            info.raw_data = data
            
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP 错误检测 {sec_user_id}: {e.response.status_code}")
            log.error(f"响应内容: {e.response.text[:200]}")
        except Exception as e:
            log.error(f"检测失败 {sec_user_id}: {e}", exc_info=True)
        
        return info
    
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
