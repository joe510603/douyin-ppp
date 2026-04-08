"""仪表盘首页 — 系统状态概览"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from nicegui import ui, app

from ..config import get_config
from ..models.comment import MonitorAccount, MessageType
from ..storage.db_storage import get_db
from src.utils.logger import get_logger

log = get_logger("dashboard")


def create_dashboard(monitor_accounts_ref, global_stats_ref):
    """创建仪表盘页面
    
    Args:
        monitor_accounts_ref: 监控账号状态字典的引用（state.monitor_states）
        global_stats_ref: 全局统计字典的引用（state.stats）
    """
    
    with ui.row().classes("w-full"):
        # 统计卡片
        with ui.card().classes("w-full"):
            ui.label("📊 系统状态概览").classes("text-h6")
            
            @ui.refreshable
            def stats_ui():
                # 实时计算在线直播间数
                online_count = sum(1 for v in monitor_accounts_ref.values() if v.get("status") == "live")
                
                # 实时计算运行时间
                start_time = global_stats_ref.get("start_time", datetime.now())
                if isinstance(start_time, datetime):
                    delta = datetime.now() - start_time
                    hours, remainder = divmod(int(delta.total_seconds()), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    uptime_str = f"{hours}:{minutes:02d}:{seconds:02d}"
                else:
                    uptime_str = "0:00:00"
                
                # 今日采集量
                today_count = global_stats_ref.get("today_total", 0)
                
                # 监控账号数
                total_accounts = len(monitor_accounts_ref)
                enabled = sum(1 for v in monitor_accounts_ref.values() if v.get("enabled", True))
                
                with ui.row().classes("w-full justify-between items-center q-mt-md"):
                    # 在线直播间
                    with ui.card().classes("q-pa-lg text-center").style("min-width: 140px"):
                        ui.label(str(online_count)).classes("text-h4 text-primary")
                        ui.label("在线直播间").classes("text-caption text-grey-7")
                    
                    # 今日采集量
                    with ui.card().classes("q-pa-lg text-center").style("min-width: 140px"):
                        ui.label(f"{today_count:,}").classes("text-h4 text-positive")
                        ui.label("今日采集").classes("text-caption text-grey-7")
                    
                    # 监控账号数
                    with ui.card().classes("q-pa-lg text-center").style("min-width: 140px"):
                        ui.label(f"{enabled}/{total_accounts}").classes("text-h4 text-info")
                        ui.label("监控账号").classes("text-caption text-grey-7")
                    
                    # 系统运行时间
                    with ui.card().classes("q-pa-lg text-center").style("min-width: 140px"):
                        ui.label(uptime_str).classes("text-h4 text-warning")
                        ui.label("运行时间").classes("text-caption text-grey-7")
            
            stats_ui()
            ui.timer(1.0, stats_ui.refresh)  # 每秒刷新
    
    # 监控账号状态表格
    with ui.card().classes("w-full q-mt-md"):
        ui.label("🎯 监控状态").classes("text-subtitle1 q-mb-sm")
        
        @ui.refreshable
        def table_ui():
            columns = [
                {"name": "name", "label": "账号名称", "field": "name", "align": "left", "sortable": True},
                {"name": "sec_user_id", "label": "用户ID", "field": "sec_user_id", "align": "left"},
                {"name": "status", "label": "状态", "field": "status", "align": "center", "sortable": True},
                {"name": "room_id", "label": "房间号", "field": "room_id", "align": "center"},
                {"name": "error_message", "label": "备注", "field": "error_message", "align": "left"},
            ]
            
            rows = []
            for sec_id, info in monitor_accounts_ref.items():
                status_val = info.get("status", "idle")
                room_id = info.get("room_id", "")
                
                rows.append({
                    "name": info.get("name", sec_id[:12]),
                    "sec_user_id": sec_id[:20] + "...",
                    "status": _format_status(status_val),
                    "room_id": room_id if room_id else "-",
                    "error_message": info.get("error_message", "") or "",
                })
            
            ui.table(columns=columns, rows=rows, row_key="sec_user_id", pagination=10).classes("w-full")
        
        table_ui()
        ui.timer(3.0, table_ui.refresh)  # 每3秒刷新


def _format_status(status: str) -> str:
    """格式化状态显示"""
    status_map = {
        "live": "🟢 直播中",
        "detecting": "🟡 检测中",
        "idle": "⚪ 未开播",
        "error": "❌ 异常",
    }
    return status_map.get(status, status)