"""视频评论抓取页面 — 任务创建表单、进度展示、任务列表管理"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional

from nicegui import ui

from ..config import get_config
from ..storage.db_storage import get_db
from ..utils.logger import get_logger

log = get_logger("video_scrape_page")


def create_video_scrape_page(task_manager):
    """
    创建视频评论抓取页面。

    Args:
        task_manager: TaskManager 实例
    """

    # ========== 任务创建 ==========
    with ui.element("div").classes("app-card w-full q-pa-lg page-enter"):
        ui.label("创建抓取任务").classes("section-title q-mb-md")

        with ui.row().classes("w-full items-end q-col-gutter-sm"):
            # 平台选择
            source_select = ui.select(
                options=["douyin", "xiaohongshu"],
                value="douyin",
                label="平台",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            # 模式选择
            mode_select = ui.select(
                options={"keyword": "关键词搜索", "account": "账号抓取", "video": "视频/笔记链接"},
                value="keyword",
                label="抓取模式",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

        # 参数输入区 — 根据模式动态显示
        params_container = ui.column().classes("w-full q-mt-sm")

        keyword_row = ui.row().classes("w-full items-end q-col-gutter-sm")
        with keyword_row:
            keyword_input = ui.input(
                "搜索关键词（多个用逗号分隔）",
                placeholder="例如: 美食,旅游,穿搭",
            ).props("dense outlined").classes("col-12 col-sm-8 app-input")

        account_row = ui.row().classes("w-full items-end q-col-gutter-sm")
        with account_row:
            account_input = ui.input(
                "账号 ID",
                placeholder="抖音 sec_user_id 或小红书用户 ID",
            ).props("dense outlined").classes("col-12 col-sm-8 app-input")

        video_row = ui.row().classes("w-full items-end q-col-gutter-sm")
        with video_row:
            video_input = ui.input(
                "视频/笔记链接",
                placeholder="https://www.douyin.com/video/xxx 或 https://www.xiaohongshu.com/explore/xxx",
            ).props("dense outlined").classes("col-12 col-sm-8 app-input")

        # 高级选项
        with ui.expansion("高级选项", icon="tune").classes("w-full q-mt-sm"):
            with ui.column().classes("gap-sm"):
                with ui.row().classes("items-center gap-md"):
                    max_videos_input = ui.number(
                        "最大视频/笔记数",
                        value=20,
                        min=1,
                        max=100,
                    ).props("dense outlined").classes("app-input")
                    
                    max_comments_input = ui.number(
                        "每视频最大评论数",
                        value=100,
                        min=10,
                        max=500,
                    ).props("dense outlined").classes("app-input")
                
                with ui.row().classes("items-center gap-md"):
                    comment_order_select = ui.select(
                        options={"all": "全部评论", "hot": "高赞优先", "latest": "最新优先"},
                        value="all",
                        label="评论排序",
                    ).props("dense outlined").classes("app-input")
                    
                    min_likes_input = ui.number(
                        "最低点赞数",
                        value=0,
                        min=0,
                        max=1000,
                    ).props("dense outlined").classes("app-input")
                
                include_reply_switch = ui.switch(
                    "包含回复",
                    value=False,
                )
                
                # 监控账号关联（仅账号模式和视频模式）
                monitor_name_select = ui.select(
                    options=["不关联"] + [m.name for m in get_config().monitors],
                    value="不关联",
                    label="关联监控账号",
                ).props("dense outlined").classes("app-input")

        # 根据模式切换显示
        def _update_visibility():
            mode = mode_select.value
            keyword_row.set_visibility(mode == "keyword")
            account_row.set_visibility(mode == "account")
            video_row.set_visibility(mode == "video")

        mode_select.on("update:model-value", _update_visibility)
        _update_visibility()

        # 提交按钮
        async def _create_task():
            source = source_select.value
            mode = mode_select.value
            
            # 获取监控账号名称
            monitor_name = None
            if monitor_name_select.value != "不关联":
                monitor_name = monitor_name_select.value

            # 构建参数
            if mode == "keyword":
                raw = keyword_input.value.strip()
                if not raw:
                    ui.notify("请输入搜索关键词", type="warning")
                    return
                keywords = [k.strip() for k in raw.split(",") if k.strip()]
                params = json.dumps({
                    "keywords": keywords,
                    "max_videos": int(max_videos_input.value),
                    "max_comments": int(max_comments_input.value),
                    "comment_order": comment_order_select.value,
                    "min_likes": int(min_likes_input.value),
                    "include_reply": include_reply_switch.value,
                    "monitor_name": monitor_name,
                })
            elif mode == "account":
                account_id = account_input.value.strip()
                if not account_id:
                    ui.notify("请输入账号 ID", type="warning")
                    return
                params = json.dumps({
                    "sec_user_id": account_id,
                    "max_videos": int(max_videos_input.value),
                    "max_comments": int(max_comments_input.value),
                    "comment_order": comment_order_select.value,
                    "min_likes": int(min_likes_input.value),
                    "include_reply": include_reply_switch.value,
                    "monitor_name": monitor_name,
                })
            elif mode == "video":
                video_url = video_input.value.strip()
                if not video_url:
                    ui.notify("请输入视频/笔记链接", type="warning")
                    return
                params = json.dumps({
                    "video_url": video_url,
                    "max_comments": int(max_comments_input.value),
                    "comment_order": comment_order_select.value,
                    "min_likes": int(min_likes_input.value),
                    "include_reply": include_reply_switch.value,
                    "monitor_name": monitor_name,
                })
            else:
                return

            # 导入采集器并创建任务
            try:
                if source == "douyin":
                    from ..collector.video_comment_collector import DouyinVideoCollector
                    config = get_config()
                    # 优先使用视频评论专用 cookie，回退到直播监控 cookie
                    cookie = config.douyin_video.cookie or config.douyin.cookie
                    collector = DouyinVideoCollector(cookie=cookie)

                    async def run_douyin(tid, src, p, tm):
                        try:
                            p_dict = json.loads(p)
                            if src == "douyin" and "keywords" in p_dict:
                                return await collector.scrape_by_keyword(
                                    tid, p_dict["keywords"], p_dict.get("max_videos", 20), tm
                                )
                            elif "sec_user_id" in p_dict:
                                return await collector.scrape_by_account(
                                    tid, p_dict["sec_user_id"], p_dict.get("max_videos", 30), tm
                                )
                            elif "video_url" in p_dict:
                                return await collector.scrape_by_video_url(
                                    tid, p_dict["video_url"], tm
                                )
                            return 0
                        finally:
                            await collector.close()

                    task_id = await task_manager.create_task(source, mode, params, run_douyin)
                else:
                    from ..collector.xhs_comment_collector import XhsCommentCollector
                    config = get_config()
                    # 小红书使用专门的 xhs cookie（包含 a1 等关键字段）
                    cookie = config.xhs.cookie
                    collector = XhsCommentCollector(cookie=cookie)

                    async def run_xhs(tid, src, p, tm):
                        try:
                            p_dict = json.loads(p)
                            if "keywords" in p_dict:
                                return await collector.scrape_by_keyword(
                                    tid, p_dict["keywords"], p_dict.get("max_videos", 20), tm
                                )
                            elif "sec_user_id" in p_dict:
                                return await collector.scrape_by_account(
                                    tid, p_dict["sec_user_id"], p_dict.get("max_videos", 30), tm
                                )
                            elif "video_url" in p_dict:
                                return await collector.scrape_by_note_url(
                                    tid, p_dict["video_url"], tm
                                )
                            return 0
                        finally:
                            await collector.close()

                    task_id = await task_manager.create_task(source, mode, params, run_xhs)

                ui.notify(f"任务已创建: {task_id}", type="positive")
                task_list_ui.refresh()

            except Exception as e:
                log.error(f"创建任务失败: {e}", exc_info=True)
                ui.notify(f"创建失败: {e}", type="negative")

        with ui.row().classes("q-mt-md gap-sm"):
            ui.button("开始抓取", on_click=_create_task).classes("btn-primary").props("unelevated icon=play_arrow")

    # ========== 任务列表 ==========
    @ui.refreshable
    def task_list_ui():
        with ui.element("div").classes("app-card w-full q-pa-lg q-mt-md page-enter-delay-1"):
            ui.label("抓取任务列表").classes("section-title q-mb-md")

            async def load_tasks():
                tasks, total = await task_manager.list_tasks(limit=50)
                return tasks, total

            # 使用 ui.table 显示任务
            columns = [
                {"name": "id", "label": "任务 ID", "field": "id", "align": "center", "sortable": True},
                {"name": "source", "label": "平台", "field": "source", "align": "center"},
                {"name": "mode", "label": "模式", "field": "mode", "align": "center"},
                {"name": "status", "label": "状态", "field": "status", "align": "center", "sortable": True},
                {"name": "progress", "label": "进度", "field": "progress", "align": "center"},
                {"name": "result_count", "label": "评论数", "field": "result_count", "align": "center", "sortable": True},
                {"name": "error", "label": "错误信息", "field": "error", "align": "left"},
                {"name": "created_at", "label": "创建时间", "field": "created_at", "align": "center", "sortable": True},
            ]

            # 格式化显示
            async def get_rows():
                tasks, _ = await task_manager.list_tasks(limit=50)
                rows = []
                mode_labels = {"keyword": "关键词", "account": "账号", "video": "链接"}
                source_labels = {"douyin": "抖音", "xiaohongshu": "小红书"}
                status_colors = {
                    "pending": "grey",
                    "running": "blue",
                    "completed": "green",
                    "failed": "red",
                    "cancelled": "orange",
                }

                for t in tasks:
                    # 解析进度
                    try:
                        prog = json.loads(t.get("progress", "{}"))
                        progress_str = f"{prog.get('scraped', 0)}/{prog.get('total', '?')}"
                    except Exception:
                        progress_str = "-"

                    status = t.get("status", "pending")
                    status_label = {
                        "pending": "等待中",
                        "running": "运行中",
                        "completed": "已完成",
                        "failed": "失败",
                        "cancelled": "已取消",
                    }.get(status, status)

                    rows.append({
                        "id": t.get("id", "")[:8],
                        "source": source_labels.get(t.get("source", ""), t.get("source", "")),
                        "mode": mode_labels.get(t.get("mode", ""), t.get("mode", "")),
                        "status": status_label,
                        "progress": progress_str,
                        "result_count": t.get("result_count", 0),
                        "error": t.get("error", "") or "",
                        "created_at": (t.get("created_at", "") or "")[:19],
                        "_status_color": status_colors.get(status, "grey"),
                        "_raw_id": t.get("id", ""),
                        "_raw_status": status,
                    })
                return rows

            # 占位表格
            table = ui.table(
                columns=columns,
                rows=[],
                row_key="id",
                pagination={"rowsPerPage": 15},
            ).classes("w-full app-table")

            # 操作按钮行
            with ui.row().classes("q-mt-sm gap-sm"):
                async def refresh_list():
                    rows = await get_rows()
                    table.rows = rows
                    table.update()
                    # 检查是否有 Cookie 相关失败
                    for row in rows:
                        if row.get("_raw_status") == "failed" and "Cookie" in row.get("error", ""):
                            ui.notify(
                                "⚠️ 检测到 Cookie 过期失败，请前往「配置管理 → 抖音视频评论」刷新 Cookie",
                                type="warning",
                                timeout=0,
                            )
                            break

                ui.button("刷新列表", on_click=refresh_list).classes("btn-ghost").props("flat icon=refresh")

                ui.button(
                    "去刷新 Cookie →",
                    on_click=lambda: ui.navigate.to("/config"),
                ).classes("btn-secondary").props("unelevated icon=warning")

                async def cancel_selected():
                    ui.notify("选中任务行后点击取消", type="info")

                # 初始化加载
                async def _initial_load():
                    rows = await get_rows()
                    table.rows = rows
                    table.update()

                ui.timer(3.0, _initial_load)
                _initial_load()

    task_list_ui()
