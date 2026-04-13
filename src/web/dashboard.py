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

    # 统计卡片区域 — 只构建一次 DOM，用 set_text 局部更新避免闪烁
    with ui.grid(columns=4).classes("w-full q-col-gutter-md"):
        # 在线直播间
        with ui.element("div").classes("q-pa-sm"):
            with ui.element("div").classes("stat-card stat-card-blue page-enter"):
                online_label = ui.label("0").classes("stat-value").style("color: var(--color-primary);")
                ui.label("在线直播间").classes("stat-label")

        # 今日采集量
        with ui.element("div").classes("q-pa-sm"):
            with ui.element("div").classes("stat-card stat-card-green page-enter-delay-1"):
                today_label = ui.label("0").classes("stat-value").style("color: var(--color-success);")
                ui.label("今日采集").classes("stat-label")

        # 监控账号数
        with ui.element("div").classes("q-pa-sm"):
            with ui.element("div").classes("stat-card stat-card-purple page-enter-delay-2"):
                accounts_label = ui.label("0/0").classes("stat-value").style("color: #8B5CF6;")
                ui.label("监控账号").classes("stat-label")

        # 系统运行时间
        with ui.element("div").classes("q-pa-sm"):
            with ui.element("div").classes("stat-card stat-card-orange page-enter-delay-3"):
                uptime_label = ui.label("0:00:00").classes("stat-value").style("color: var(--color-warning);")
                ui.label("运行时间").classes("stat-label")

    def update_stats():
        """每秒局部更新统计数值（不重建 DOM）"""
        try:
            online_count = sum(1 for v in monitor_accounts_ref.values() if v.get("status") == "live")
            online_label.set_text(str(online_count))

            today_count = global_stats_ref.get("today_total", 0)
            today_label.set_text(f"{today_count:,}")

            total_accounts = len(monitor_accounts_ref)
            enabled = sum(1 for v in monitor_accounts_ref.values() if v.get("enabled", True))
            accounts_label.set_text(f"{enabled}/{total_accounts}")

            start_time = global_stats_ref.get("start_time", datetime.now())
            if isinstance(start_time, datetime):
                delta = datetime.now() - start_time
                hours, remainder = divmod(int(delta.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                uptime_label.set_text(f"{hours}:{minutes:02d}:{seconds:02d}")
        except Exception:
            pass  # 静默忽略，避免 timer 中断事件循环

    ui.timer(1.0, update_stats)

    # 监控账号状态表格
    with ui.element("div").classes("app-card w-full q-mt-lg q-pa-md page-enter-delay-2"):
        ui.label("监控状态").classes("section-title q-mb-md")

        @ui.refreshable
        def table_ui():
            columns = [
                {"name": "name", "label": "账号名称", "field": "name", "align": "left", "sortable": True},
                {"name": "sec_user_id", "label": "用户ID", "field": "sec_user_id", "align": "left"},
                {"name": "status", "label": "状态", "field": "status", "align": "center", "sortable": True},
                {"name": "viewer_count", "label": "在线人数", "field": "viewer_count", "align": "center", "sortable": True},
                {"name": "room_id", "label": "房间号", "field": "room_id", "align": "center"},
                {"name": "error_message", "label": "备注", "field": "error_message", "align": "left"},
            ]

            rows = []
            for sec_id, info in monitor_accounts_ref.items():
                status_val = info.get("status", "idle")
                room_id = info.get("room_id", "")
                viewer_count = info.get("viewer_count", 0)

                rows.append({
                    "name": info.get("name", sec_id[:12]),
                    "sec_user_id": sec_id[:20] + "...",
                    "status": _format_status(status_val),
                    "viewer_count": viewer_count if viewer_count > 0 else "-",
                    "room_id": room_id if room_id else "-",
                    "error_message": info.get("error_message", "") or "",
                })

            ui.table(columns=columns, rows=rows, row_key="sec_user_id", pagination=10).classes("w-full app-table")

        table_ui()

        def safe_table_refresh():
            """安全刷新表格，防止 slot 已删除时崩溃"""
            try:
                table_ui.refresh()
            except Exception:
                pass

        ui.timer(3.0, safe_table_refresh)


def _format_status(status: str) -> str:
    """格式化状态显示 — 使用文本emoji避免HTML转义"""
    status_map = {
        "live": "🟢 直播中",
        "detecting": "🔍 检测中",
        "idle": "⚪ 未开播",
        "error": "🔴 异常",
    }
    return status_map.get(status, status)
