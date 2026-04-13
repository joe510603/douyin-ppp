"""数据浏览与导出页面 — 直播弹幕 + 视频评论，表格查看、筛选搜索、Excel导出"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from nicegui import ui, run

from ..storage.db_storage import get_db
from ..storage.excel_storage import ExcelStorage
from ..models.comment import MessageType
from ..config import get_config
from src.utils.logger import get_logger

log = get_logger("data_page")


def _open_folder(folder_path: Path):
    """打开文件夹（跨平台）"""
    import subprocess
    import platform
    
    system = platform.system()
    try:
        if system == "Darwin":  # macOS
            subprocess.run(["open", str(folder_path)])
        elif system == "Windows":
            subprocess.run(["explorer", str(folder_path)])
        elif system == "Linux":
            subprocess.run(["xdg-open", str(folder_path)])
    except Exception as e:
        log.warning(f"无法打开文件夹: {e}")


def create_data_page(on_export_complete=None):
    """创建数据浏览与导出页面"""

    # ========== 数据源切换 ==========
    current_source = {"value": "live"}  # "live" 或 "video"
    
    # 获取监控账号列表（用于用户筛选）
    config = get_config()
    monitor_names = ["全部账号"] + [m.name for m in config.monitors if m.enabled]
    all_tags = set()
    for m in config.monitors:
        all_tags.update(m.tags)
    tag_options = ["全部标签"] + sorted(all_tags)

    with ui.element("div").classes("app-card w-full q-pa-md page-enter"):
        ui.label("数据筛选").classes("section-title q-mb-md")

        # 数据源选择
        with ui.row().classes("w-full items-end q-col-gutter-sm q-mb-sm"):
            source_select = ui.select(
                options={"live": "直播弹幕", "video": "视频评论"},
                value="live",
                label="数据源",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

        # ===== 直播弹幕筛选条件 =====
        live_filter = ui.row().classes("w-full items-end q-col-gutter-sm")

        with live_filter:
            keyword_input = ui.input("关键词搜索", placeholder="搜索评论内容...").props("dense outlined clearable").classes("col-12 col-sm-3 app-input")
            monitor_select = ui.select(
                options=monitor_names,
                value="全部账号",
                label="监控账号",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")
            type_select = ui.select(
                options=["全部类型", "弹幕/评论", "礼物", "进场", "点赞"],
                label="消息类型",
                value="全部类型"
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")
            date_from = ui.date_input("开始日期").props("dense outlined").classes("col-6 col-sm-2 app-input")
            date_to = ui.date_input("结束日期").props("dense outlined").classes("col-6 col-sm-2 app-input")

        # ===== 视频评论筛选条件 =====
        video_filter = ui.row().classes("w-full items-end q-col-gutter-sm")
        with video_filter:
            v_keyword_input = ui.input("关键词搜索", placeholder="搜索评论内容...").props("dense outlined clearable").classes("col-12 col-sm-3 app-input")
            v_monitor_select = ui.select(
                options=monitor_names,
                value="全部账号",
                label="监控账号",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")
            v_source_select = ui.select(
                options=["全部", "抖音", "小红书"],
                value="全部",
                label="平台",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")
            v_date_from = ui.date_input("开始日期").props("dense outlined").classes("col-6 col-sm-2 app-input")
            v_date_to = ui.date_input("结束日期").props("dense outlined").classes("col-6 col-sm-2 app-input")

        video_filter.set_visibility(False)

        def _on_source_change():
            is_live = source_select.value == "live"
            live_filter.set_visibility(is_live)
            video_filter.set_visibility(not is_live)

        source_select.on("update:model-value", _on_source_change)

        # 操作按钮
        with ui.row().classes("q-mt-md gap-sm"):
            ui.button("查询", on_click=lambda: _do_query()).classes("btn-primary").props("unelevated icon=search")
            ui.button("导出 Excel", on_click=lambda: _do_export()).classes("btn-secondary").props("unelevated color=secondary icon=download")
            ui.button("重置", on_click=lambda: _do_reset()).classes("btn-ghost").props("flat icon=refresh")

    # ========== 查询/导出逻辑 ==========

    # ========== 数据表格 ==========
    with ui.element("div").classes("app-card w-full q-mt-md q-pa-md page-enter-delay-1"):
        result_info = ui.label("点击「查询」加载数据").style("font-size: 13px; color: var(--color-text-tertiary); margin-bottom: 8px;")

        columns = [
            {"name": "id", "label": "序号", "field": "id", "align": "center", "sortable": True},
            {"name": "create_time", "label": "时间", "field": "create_time", "align": "center", "sortable": True},
            {"name": "message_type", "label": "类型", "field": "message_type", "align": "center"},
            {"name": "content", "label": "内容", "field": "content", "align": "left", "sortable": True},
            {"name": "user_nickname", "label": "用户", "field": "user_nickname", "align": "left"},
            {"name": "room_id", "label": "直播间", "field": "room_id", "align": "center"},
            {"name": "monitor_name", "label": "监控账号", "field": "monitor_name", "align": "left"},
        ]

        table = ui.table(columns=columns, rows=[], row_key="id", pagination={"rowsPerPage": 50}).classes("w-full app-table")

    async def _do_query():
        """执行查询"""
        ui.notify("开始查询...", type="info")
        db = get_db()
        if db._db is None:
            try:
                await db.init_db()
            except Exception as e:
                ui.notify(f"数据库初始化失败: {e}", type="negative")
                return

        if source_select.value == "live":
            await _query_live(db)
        else:
            await _query_video(db)

    async def _query_live(db):
        """查询直播弹幕"""
        kw = keyword_input.value.strip() or None
        
        # 用户筛选
        monitor_name = None
        if monitor_select.value != "全部账号":
            monitor_name = monitor_select.value
        
        type_map = {
            "全部类型": None, "弹幕/评论": MessageType.CHAT, "礼物": MessageType.GIFT,
            "进场": MessageType.MEMBER, "点赞": MessageType.LIKE,
        }
        mt = type_map.get(type_select.value)
        dt_start, dt_end = _parse_dates(date_from, date_to)

        try:
            rows, total = await db.query_comments(
                keyword=kw, message_type=mt, monitor_name=monitor_name,
                start_time=dt_start, end_time=dt_end, limit=10000,
            )
            for r in rows:
                r["create_time"] = (r.get("create_time") or "")[:19]
                r["content"] = (str(r.get("content")) or "")[:100]

            live_columns = [
                {"name": "id", "label": "序号", "field": "id", "align": "center", "sortable": True},
                {"name": "create_time", "label": "时间", "field": "create_time", "align": "center", "sortable": True},
                {"name": "message_type", "label": "类型", "field": "message_type", "align": "center"},
                {"name": "content", "label": "内容", "field": "content", "align": "left", "sortable": True},
                {"name": "user_nickname", "label": "用户", "field": "user_nickname", "align": "left"},
                {"name": "monitor_name", "label": "监控账号", "field": "monitor_name", "align": "left"},
            ]
            table.columns = live_columns
            table.rows = rows
            table.update()
            result_info.text = f"查询结果: 共 {total} 条记录 (当前显示 {len(rows)} 条)"
            ui.notify(f"查询完成，共 {total} 条记录", type="positive")
        except Exception as e:
            log.error(f"数据查询失败: {e}", exc_info=True)
            ui.notify(f"查询失败: {e}", type="negative")

    async def _query_video(db):
        """查询视频评论"""
        kw = v_keyword_input.value.strip() or None
        
        # 用户筛选
        monitor_name = None
        if v_monitor_select.value != "全部账号":
            monitor_name = v_monitor_select.value
        
        source_map = {"全部": None, "抖音": "douyin", "小红书": "xiaohongshu"}
        source = source_map.get(v_source_select.value)
        dt_start, dt_end = _parse_dates(v_date_from, v_date_to)

        try:
            rows, total = await db.query_video_comments(
                source=source, keyword=kw, monitor_name=monitor_name,
                start_time=dt_start, end_time=dt_end, limit=10000,
            )
            for r in rows:
                r["collected_at"] = (r.get("collected_at") or "")[:19]
                r["content"] = (str(r.get("content")) or "")[:100]

            video_columns = [
                {"name": "id", "label": "序号", "field": "id", "align": "center", "sortable": True},
                {"name": "source", "label": "平台", "field": "source", "align": "center"},
                {"name": "monitor_name", "label": "监控账号", "field": "monitor_name", "align": "left"},
                {"name": "video_title", "label": "视频标题", "field": "video_title", "align": "left"},
                {"name": "content", "label": "评论内容", "field": "content", "align": "left", "sortable": True},
                {"name": "user_nickname", "label": "用户", "field": "user_nickname", "align": "left"},
                {"name": "like_count", "label": "点赞", "field": "like_count", "align": "center"},
                {"name": "source_keyword", "label": "搜索词", "field": "source_keyword", "align": "center"},
                {"name": "collected_at", "label": "采集时间", "field": "collected_at", "align": "center", "sortable": True},
            ]
            table.columns = video_columns
            table.rows = rows
            table.update()
            result_info.text = f"查询结果: 共 {total} 条记录 (当前显示 {len(rows)} 条)"
            ui.notify(f"查询完成，共 {total} 条记录", type="positive")
        except Exception as e:
            log.error(f"视频评论查询失败: {e}", exc_info=True)
            ui.notify(f"查询失败: {e}", type="negative")

    async def _do_export():
        """执行导出"""
        ui.notify("正在导出，请稍候...", type="info")
        try:
            db = get_db()
            if db._db is None:
                await db.init_db()

            exporter = ExcelStorage(db)

            if source_select.value == "live":
                type_map = {
                    "全部类型": None, "弹幕/评论": MessageType.CHAT, "礼物": MessageType.GIFT,
                    "进场": MessageType.MEMBER, "点赞": MessageType.LIKE,
                }
                mt = type_map.get(type_select.value)
                dt_start, dt_end = _parse_dates(date_from, date_to)
                filepath = await exporter.export_to_excel(
                    keyword=keyword_input.value.strip() or None,
                    message_type=mt,
                    start_time=dt_start,
                    end_time=dt_end,
                )
            else:
                source_map = {"全部": None, "抖音": "douyin", "小红书": "xiaohongshu"}
                source = source_map.get(v_source_select.value)
                dt_start, dt_end = _parse_dates(v_date_from, v_date_to)
                filepath = await exporter.export_video_comments(
                    source=source,
                    keyword=v_keyword_input.value.strip() or None,
                    start_time=dt_start,
                    end_time=dt_end,
                )

            if not filepath:
                ui.notify("没有数据可导出", type="warning")
                return

            file_path = Path(filepath)
            if file_path.exists():
                ui.notify(f"导出成功！文件: {file_path.name}", type="positive", timeout=5000)
                with ui.dialog() as dialog, ui.card().classes("q-pa-lg confirm-dialog"):
                    ui.label("导出完成").classes("confirm-dialog-title q-mb-sm")
                    ui.label(f"文件名: {file_path.name}").style("color: var(--color-text-secondary); margin-bottom: 4px;")
                    ui.label(f"路径: {filepath}").style("font-size: 12px; color: var(--color-text-tertiary); margin-bottom: 16px;")
                    with ui.row().classes("gap-sm"):
                        ui.button("关闭", on_click=dialog.close).classes("btn-ghost").props("flat")
                        ui.button("打开文件夹", on_click=lambda: _open_folder(file_path.parent)).classes("btn-primary").props("unelevated color=secondary")
                dialog.open()
            else:
                ui.notify("导出失败: 文件不存在", type="negative")
        except Exception as e:
            log.error(f"导出失败: {e}", exc_info=True)
            ui.notify(f"导出失败: {e}", type="negative", timeout=5000)

def _do_reset():
    keyword_input.value = ""
    type_select.value = "全部类型"
    date_from.value = ""
    date_to.value = ""
    v_keyword_input.value = ""
    v_source_select.value = "全部"
    v_date_from.value = ""
    v_date_to.value = ""
    ui.notify("已重置筛选条件", type="info")

def _parse_dates(from_input, to_input):
    dt_start = dt_end = None
    # ui.date_input 返回的是 YYYY-MM-DD 格式字符串
    if from_input.value:
        try:
            dt_start = datetime.strptime(from_input.value, "%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    if to_input.value:
        try:
            dt_end = datetime.strptime(to_input.value, "%Y-%m-%d") + timedelta(days=1)
        except (ValueError, TypeError):
            pass
    return dt_start, dt_end
