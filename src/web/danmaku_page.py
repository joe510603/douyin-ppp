"""实时弹幕大屏 — 实时滚动展示直播弹幕流"""

from __future__ import annotations

from datetime import datetime
from collections import deque
from typing import Optional

from nicegui import ui

# 全局弹幕缓冲区（最多保留最近 500 条）
_danmaku_buffer: deque[dict] = deque(maxlen=500)

# 当前选中的直播间
_current_room: Optional[str] = None


def push_danmaku(data: dict):
    """外部调用：推送新弹幕到缓冲区"""
    data["_display_time"] = datetime.now().strftime("%H:%M:%S")
    _danmaku_buffer.appendleft(data)


def create_danmaku_page(available_rooms: list[str]):
    """
    创建实时弹幕大屏页面。
    
    Args:
        available_rooms: 可用的直播间 room_id 列表
    """
    global _current_room
    
    with ui.card().classes("w-full h-[70vh] overflow-hidden bg-dark"):
        with ui.row().classes("w-full items-center justify-between q-pa-sm bg-black"):
            ui.label("📡 实时弹幕流").classes("text-white text-h6")
            
            # 房间选择器
            room_select = ui.select(
                options=["全部"] + available_rooms,
                value="全部",
                label="筛选直播间",
            ).props("dark dense color=white standout").classes("q-mr-md")
        
        # 弹幕显示区域
        container = ui.column().classes("w-full h-full overflow-y-auto bg-grey-10 q-pa-md")
        container.style("max-height: calc(70vh - 60px); background-color: #121212;")
        
        @ui.refreshable
        def refresh_danmaku():
            selected = room_select.value or "全部"
            
            items = list(_danmaku_buffer)
            if selected != "全部":
                items = [d for d in items if d.get("room_id") == selected]
            
            if not items:
                with container:
                    ui.label("等待弹幕数据...").classes("text-grey-6 text-center q-pa-xl w-full")
                return
            
            with container:
                for item in items[:50]:  # 显示最近50条
                    msg_type = item.get("message_type", "")
                    content = item.get("content", "")
                    nickname = item.get("user_nickname", "")
                    time_str = item.get("_display_time", "")
                    room_id = item.get("room_id", "")[-6:]
                    
                    # 根据消息类型选择样式
                    if "Gift" in str(msg_type):
                        bg_color = "rgba(255,193,7,0.12)"
                        prefix = "🎁 "
                    elif "Member" in str(msg_type):
                        bg_color = "rgba(76,175,80,0.12)"
                        prefix = "👋 "
                    else:
                        bg_color = "rgba(255,255,255,0.05)"
                        prefix = ""
                    
                    with ui.row().classes("w-full q-my-xs q-py-xs q-px-sm rounded-borders").style(
                        f"background:{bg_color}"
                    ):
                        ui.label(time_str).classes("text-grey-5 text-caption q-mr-sm").style("min-width:55px;")
                        ui.label(prefix).classes("q-mr-none")
                        ui.label(nickname[:16] or "匿名").classes(
                            "text-blue-3 q-mr-sm text-body2"
                        ).style("font-weight:500; min-width:80px; max-width:120px; overflow:hidden;text-overflow:ellipsis; white-space:nowrap;")
                        ui.label(content[:80]).classes(
                            "text-white text-body2 flex-grow"
                        ).style("word-break:break-word;")
        
        refresh_danmaku()
