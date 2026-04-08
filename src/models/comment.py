"""评论数据模型 — 使用 Pydantic 定义数据结构"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """直播间消息类型"""
    CHAT = "WebcastChatMessage"           # 弹幕/评论
    GIFT = "WebcastGiftMessage"            # 礼物
    MEMBER = "WebcastMemberMessage"        # 用户进场
    LIKE = "WebcastLikeMessage"            # 点赞
    SOCIAL_MESSAGE = "WebcastSocialMessage"  # 关注/分享等社交
    UPDATE_FANS_GROUP = "WebcastUpdateFansGroupMessage"  # 粉丝团更新
    ROOM_USER_SEQ_MESSAGE = "WebcastRoomUserSeqMessage"  # 在线人数更新
    UNKNOWN = "Unknown"                    # 未知类型


class LiveComment(BaseModel):
    """直播弹幕/评论数据模型"""
    
    # 基础信息
    id: Optional[int] = None              # 数据库主键（写入后回填）
    message_type: MessageType = Field(default=MessageType.CHAT, description="消息类型")
    
    # 消息内容
    content: str = Field(default="", description="评论/弹幕内容")
    
    # 用户信息
    user_id: str = Field(default="", description="用户 ID")
    user_nickname: str = Field(default="", description="用户昵称")
    user_avatar: str = Field(default="", description="用户头像 URL")
    
    # 直播间信息
    room_id: str = Field(default="", description="直播间 room_id")
    anchor_id: str = Field(default="", description="主播用户 ID (sec_user_id)")
    anchor_name: str = Field(default="", description="主播昵称")
    monitor_name: str = Field(default="", description="监控账号名称")
    
    # 时间戳
    create_time: datetime = Field(default_factory=datetime.now, description="消息时间")
    
    # 扩展字段（用于礼物、点赞等）
    gift_name: Optional[str] = Field(default=None, description="礼物名称")
    gift_count: Optional[int] = Field(default=None, description="礼物数量")
    like_count: Optional[int] = Field(default=None, description="点赞数")
    
    # 元数据
    raw_data: Optional[str] = Field(default=None, description="原始数据（未知类型时保留）")
    collected_at: datetime = Field(default_factory=datetime.now, description="采集入库时间")

    class Config:
        use_enum_values = True

    @property
    def is_chat(self) -> bool:
        return self.message_type == MessageType.CHAT

    @property
    def is_gift(self) -> bool:
        return self.message_type == MessageType.GIFT

    @property
    def is_member_join(self) -> bool:
        return self.message_type == MessageType.MEMBER


class VideoComment(BaseModel):
    """视频评论数据模型"""
    
    id: Optional[int] = None
    
    # 视频信息
    video_id: str = Field(default="", description="视频 ID")
    video_url: str = Field(default="", description="视频链接")
    video_title: str = Field(default="", description="视频标题")
    video_author: str = Field(default="", description="视频作者")
    
    # 评论内容
    content: str = Field(default="", description="评论内容")
    
    # 用户信息
    user_id: str = Field(default="", description="评论用户 ID")
    user_nickname: str = Field(default="", description="评论用户昵称")
    
    # 时间与互动
    publish_time: datetime = Field(description="评论发布时间")
    like_count: int = Field(default=0, description="评论点赞数")
    reply_count: int = Field(default=0, description="回复数")
    
    # 来源
    source_keyword: Optional[str] = Field(default=None, description="搜索关键词（关键词模式）")
    collected_at: datetime = Field(default_factory=datetime.now)


class LiveRoomInfo(BaseModel):
    """直播间信息"""
    
    room_id: str = Field(..., description="直播间ID")
    status: bool = Field(..., description="是否直播中")
    anchor_nickname: str = Field(default="", description="主播昵称")
    title: str = Field(default="", description="直播间标题")
    viewer_count: int = Field(default=0, description="观看人数")
    
class MonitorAccount(BaseModel):
    """监控账号配置"""
    
    name: str = Field(..., description="账号显示名称")
    sec_user_id: str = Field(..., description="抖音 sec_user_id")
    room_id: str = Field(default="", description="直播间 room_id（自动检测）")
    enabled: bool = Field(default=True, description="是否启用监控")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    
    # 运行时状态（不持久化到配置文件）
    status: str = Field(default="idle", description="运行状态: idle/detecting/live/error")
    last_check_time: Optional[datetime] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
