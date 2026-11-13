"""线索数据模型 — 用户级购车意向线索 + 抖音触达任务

设计要点：
- 一条 Lead 聚合同一用户（lead_key）在多个直播间的多条弹幕，避免重复线索
- intent_* 字段由 LLM 判定，contact_* 字段由规则提取，两者独立可覆盖
- outreach_* 字段描述第二步「搜人 + 留言」的执行状态，与线索本身解耦
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class IntentLevel(str, Enum):
    """购车意向等级"""

    HIGH = "high"      # 明确购车/到店/问价
    MEDIUM = "medium"  # 有兴趣但未明确
    LOW = "low"        # 泛泛询问/比较/观望
    NONE = "none"      # 无购车意向


class LeadStatus(str, Enum):
    """线索处理状态"""

    NEW = "new"            # 待处理
    PUSHED = "pushed"      # 已推送飞书
    CONTACTED = "contacted"  # 已跟进
    IGNORED = "ignored"    # 已忽略
    CONVERTED = "converted"  # 已留资（销售在飞书表里标的终态）


class OutreachStatus(str, Enum):
    """抖音触达执行状态"""

    PENDING = "pending"    # 排队中
    SEARCHING = "searching"  # 搜索用户中
    FOUND = "found"        # 已定位到用户主页
    COMMENTED = "commented"  # 已留言
    FAILED = "failed"      # 失败
    SKIPPED = "skipped"    # 已跳过（超上限/无抖音号等）
    NOT_FOUND = "not_found"  # 这次没搜到这个人（**可重试**，不是终态）
    CONVERTED = "converted"  # 已留资（终态，不再留言）


def fmt_dt(dt) -> str:
    """时间统一成 "YYYY-MM-DD HH:MM:SS"

    为什么必须统一：leads.created_at / last_hit_at 都用这个格式，
    而排序是**字符串比较**——
    live_comments.create_time 是 ISO 格式（"2026-11-11T16:35:25"），
    里面的 "T"(0x54) 比空格(0x20) 大，混在一起排序结果会完全错乱。
    """
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(dt, str) and dt:
        try:
            return datetime.fromisoformat(dt.replace("T", " ")).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            return dt[:19]
    return ""


# 意向等级排序（数值越大越优先）
INTENT_LEVEL_ORDER: dict[str, int] = {
    IntentLevel.NONE.value: 0,
    IntentLevel.LOW.value: 1,
    IntentLevel.MEDIUM.value: 2,
    IntentLevel.HIGH.value: 3,
}


class Lead(BaseModel):
    """购车意向线索（用户级）"""

    id: Optional[int] = None
    # 唯一键：优先用 display_id，其次 user_id，最后 room_id+nickname 兜底
    lead_key: str = Field(default="", description="线索去重键")

    # 用户身份
    user_id: str = Field(default="", description="抖音 uid")
    display_id: str = Field(default="", description="抖音号（平台分配，稳定不变）")
    nickname: str = Field(default="", description="用户昵称")
    avatar: str = Field(default="", description="头像 URL")

    # 来源直播间
    room_id: str = Field(default="", description="直播间 ID")
    monitor_name: str = Field(default="", description="监控账号名称")
    live_room: str = Field(default="", description="直播间标题")

    # 归一化后的门店短名（monitor_name 去掉「小米汽车」前缀和各种分隔符）
    store: str = Field(default="", description="归一化后的门店名")

    # AI 判定结果
    intent_level: str = Field(default=IntentLevel.NONE.value, description="意向等级 high/medium/low/none")
    intent_score: int = Field(default=0, description="意向分 0-100")
    intent_reason: str = Field(default="", description="判定依据")
    car_model: str = Field(default="", description="关注的车型")

    # 联系方式（规则提取 + LLM 补充）
    contact_type: str = Field(default="", description="phone / wechat / both")
    contact_value: str = Field(default="", description="联系方式内容")

    # 证据弹幕
    evidence: str = Field(default="", description="触发线索的弹幕 JSON 数组")
    evidence_count: int = Field(default=0, description="触发弹幕条数")
    comment_count: int = Field(default=0, description="该用户累计弹幕数")

    # 最后一次被抓到（命中）的时间，格式 "YYYY-MM-DD HH:MM:SS"
    #
    # 为什么不能直接用 created_at：created_at 是**首次**抓到这条线索的时间。
    # 同一个人隔几天又在直播间问了一次，created_at 还停在老时间，
    # 但他其实是最"热乎"的（可能人就在直播间里等着回复）。
    # 触达排序按这个字段倒序 → 最新的线索先留言。
    last_hit_at: str = Field(default="", description="最后一次抓到该线索的时间")

    # 处理状态
    status: str = Field(default=LeadStatus.NEW.value, description="new/pushed/contacted/ignored")
    pushed_at: str = Field(default="", description="推送飞书时间")

    # 判定来源：rule=规则实时判定 / llm=AI 批量判定
    source: str = Field(default="rule", description="判定来源 rule / llm")

    # 飞书多维表格的记录 ID（用于后续更新而不是重复创建）
    bitable_record_id: str = Field(default="", description="飞书多维表格 record_id")

    # 上次同步进飞书时的状态标记（格式 "处理状态|触达状态"）
    # 用来判断本地状态是否真的变了——没变就不能覆盖销售在飞书里改过的值
    bitable_synced_status: str = Field(default="", description="上次同步进飞书的状态标记")

    # 第二步：抖音触达状态
    outreach_status: str = Field(default=OutreachStatus.PENDING.value, description="触达执行状态")
    outreach_at: str = Field(default="", description="触达时间")
    outreach_note: str = Field(default="", description="触达备注（目标链接/错误信息）")

    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    class Config:
        use_enum_values = True

    @property
    def priority(self) -> int:
        """排序优先级：意向等级 + 有联系方式加权"""
        base = INTENT_LEVEL_ORDER.get(self.intent_level, 0)
        if self.contact_value:
            base += 1
        return base

    @property
    def is_actionable(self) -> bool:
        """是否值得触达：有抖音号且意向不低于 low"""
        return bool(self.display_id) and INTENT_LEVEL_ORDER.get(self.intent_level, 0) >= INTENT_LEVEL_ORDER[
            IntentLevel.LOW.value
        ]


class OutreachRecord(BaseModel):
    """抖音触达任务记录（第二步）"""

    id: str = Field(default="", description="任务 ID")
    lead_id: Optional[int] = Field(default=None, description="关联线索 ID")
    display_id: str = Field(default="", description="目标抖音号")
    nickname: str = Field(default="", description="目标昵称")

    target_url: str = Field(default="", description="定位到的用户主页 URL")
    message: str = Field(default="", description="实际发送的留言内容")
    status: str = Field(default=OutreachStatus.PENDING.value, description="执行状态")
    error: str = Field(default="", description="失败原因")

    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    class Config:
        use_enum_values = True
