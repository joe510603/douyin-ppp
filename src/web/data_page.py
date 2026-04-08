"""数据浏览与导出页面 — 表格查看评论、筛选搜索、Excel导出"""

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
    
    # ========== 定义回调函数（必须在按钮之前）==========
    
    async def do_query():
        """执行查询并刷新表格"""
        ui.notify("开始查询...", type="info")
        
        db = get_db()
        
        # 确保数据库已初始化
        if db._db is None:
            try:
                await db.init_db()
            except Exception as e:
                log.error(f"数据库初始化失败: {e}")
                ui.notify(f"数据库初始化失败: {e}", type="negative")
                return
        
        kw = keyword_input.value.strip() or None
        rid = room_filter.value.strip() or None
        
        type_map = {
            "全部类型": None,
            "💬 弹幕/评论": MessageType.CHAT,
            "🎁 礼物": MessageType.GIFT,
            "👋 进场": MessageType.MEMBER,
            "👍 点赞": MessageType.LIKE,
        }
        mt = type_map.get(type_select.value)
        
        dt_start = None
        dt_end = None
        if date_from.value:
            try:
                dt_start = datetime.fromisoformat(date_from.value)
            except ValueError:
                pass
        if date_to.value:
            try:
                dt_end = datetime.fromisoformat(date_to.value) + timedelta(days=1)
            except ValueError:
                pass
        
        try:
            rows, total = await db.query_comments(
                room_id=rid,
                message_type=mt,
                keyword=kw,
                start_time=dt_start,
                end_time=dt_end,
                limit=10000,
            )
            
            formatted = []
            for r in rows:
                r["create_time"] = (r.get("create_time") or "")[:19]
                r["content"] = (str(r.get("content")) or "")[:100]
                formatted.append(r)
            
            table.rows = formatted
            table.update()
            result_info.text = f"查询结果: 共 {total} 条记录 (当前显示 {len(formatted)} 条)"
            ui.notify(f"查询完成，共 {total} 条记录", type="positive")
            
        except Exception as e:
            log.error(f"数据查询失败: {e}", exc_info=True)
            result_info.text = f"查询失败: {e}"
            ui.notify(f"查询失败: {e}", type="negative")
    
    async def do_export():
        """执行导出"""
        ui.notify("正在导出，请稍候...", type="info")
        
        try:
            db = get_db()
            
            # 确保数据库已初始化
            if db._db is None:
                await db.init_db()
            
            # 解析时间筛选
            dt_start = None
            dt_end = None
            if date_from.value:
                try:
                    dt_start = datetime.fromisoformat(date_from.value)
                except ValueError:
                    pass
            if date_to.value:
                try:
                    dt_end = datetime.fromisoformat(date_to.value) + timedelta(days=1)
                except ValueError:
                    pass
            
            # 解析消息类型
            type_map = {
                "全部类型": None,
                "💬 弹幕/评论": MessageType.CHAT,
                "🎁 礼物": MessageType.GIFT,
                "👋 进场": MessageType.MEMBER,
                "👍 点赞": MessageType.LIKE,
            }
            mt = type_map.get(type_select.value)
            
            exporter = ExcelStorage(db)
            filepath = await exporter.export_to_excel(
                keyword=keyword_input.value.strip() or None,
                room_id=room_filter.value.strip() or None,
                message_type=mt,
                start_time=dt_start,
                end_time=dt_end,
            )
            
            if not filepath:
                ui.notify("没有数据可导出", type="warning")
                return
            
            # 生成下载链接
            file_path = Path(filepath)
            if file_path.exists():
                ui.notify(f"导出成功！文件: {file_path.name}", type="positive", timeout=5000)
                
                # 提供下载链接
                config = get_config()
                export_dir = Path(config.storage.export_dir)
                relative_path = file_path.relative_to(export_dir)
                
                with ui.dialog() as dialog, ui.card():
                    ui.label("📥 导出完成").classes("text-h6")
                    ui.label(f"文件名: {file_path.name}").classes("q-mb-sm")
                    ui.label(f"路径: {filepath}").classes("text-caption text-grey q-mb-md")
                    
                    with ui.row():
                        ui.button("关闭", on_click=dialog.close).props("flat")
                        ui.button("打开文件夹", on_click=lambda: _open_folder(file_path.parent)).props("unelevated color=secondary")
                
                dialog.open()
            else:
                ui.notify("导出失败: 文件不存在", type="negative")
                
        except Exception as e:
            log.error(f"导出失败: {e}", exc_info=True)
            ui.notify(f"导出失败: {e}", type="negative", timeout=5000)
    
    def do_reset():
        keyword_input.value = ""
        room_filter.value = ""
        type_select.value = "全部类型"
        date_from.value = ""
        date_to.value = ""
        ui.notify("已重置筛选条件", type="info")
    
    # ========== 页面布局 ==========
    
    # 筛选条件
    with ui.card().classes("w-full"):
        ui.label("🔍 数据筛选").classes("text-h6 q-mb-sm")
        
        with ui.row().classes("wrap items-end"):
            keyword_input = ui.input("关键词搜索", placeholder="搜索评论内容...").props("dense clearable").classes("col-12 col-sm-4 q-pr-sm")
            
            room_filter = ui.input("直播间ID", placeholder="留空=全部").props("dense").classes("col-6 col-sm-3 q-pr-sm")
            
            type_select = ui.select(
                options=["全部类型", "💬 弹幕/评论", "🎁 礼物", "👋 进场", "👍 点赞"],
                label="消息类型",
                value="全部类型"
            ).props("dense").classes("col-6 col-sm-3 q-pr-sm")
            
            date_from = ui.input("开始日期", placeholder="YYYY-MM-DD").props("dense").classes("col-6 col-sm-3 q-pr-sm")
            date_to = ui.input("结束日期", placeholder="YYYY-MM-DD").props("dense").classes("col-6 col-sm-3")
        
        # 操作按钮 - 放在筛选条件下方
        with ui.row().classes("q-mt-md"):
            ui.button("🔍 查询", on_click=do_query).props("unelevated color=primary").classes("q-mr-sm")
            ui.button("📥 导出 Excel", on_click=do_export).props("unelevated color=secondary").classes("q-mr-sm")
            ui.button("🔄 重置", on_click=do_reset).props("flat")
    
    # 数据表格
    with ui.card().classes("w-full q-mt-md"):
        result_info = ui.label("点击「查询」加载数据").classes("text-caption text-grey-6 q-mb-sm")
        
        columns = [
            {"name": "id", "label": "序号", "field": "id", "align": "center", "sortable": True},
            {"name": "create_time", "label": "时间", "field": "create_time", "align": "center", "sortable": True},
            {"name": "message_type", "label": "类型", "field": "message_type", "align": "center"},
            {"name": "content", "label": "内容", "field": "content", "align": "left", "sortable": True},
            {"name": "user_nickname", "label": "用户", "field": "user_nickname", "align": "left"},
            {"name": "room_id", "label": "直播间", "field": "room_id", "align": "center"},
            {"name": "monitor_name", "label": "监控账号", "field": "monitor_name", "align": "left"},
        ]
        
        table = ui.table(columns=columns, rows=[], row_key="id", pagination={"rowsPerPage": 50}).classes("w-full")