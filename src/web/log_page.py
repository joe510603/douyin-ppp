"""日志查看页面"""

from __future__ import annotations

from pathlib import Path
from nicegui import ui

from ..config import get_config


def create_log_page():
    """创建日志查看页面"""

    config = get_config()
    log_path = Path(config.logging.file_path)

    with ui.element("div").classes("app-card w-full q-pa-md page-enter"):
        ui.label("运行日志").classes("section-title q-mb-sm")

        if log_path.exists():
            ui.label(f"日志文件: {log_path}").style("font-size: 12px; color: var(--color-text-tertiary); margin-bottom: 12px;")

            # 日志级别筛选
            with ui.row().classes("items-center gap-sm q-mb-md"):
                level_filter = ui.select(
                    options=["ALL", "DEBUG", "INFO", "WARNING", "ERROR"],
                    value="ALL",
                    label="日志级别",
                ).props("dense outlined").classes("app-input")

                ui.button("刷新", on_click=lambda: refresh_logs()).classes("btn-ghost").props("flat dense icon=refresh")

            # 日志内容区域
            log_display = ui.code("").classes("w-full").style(
                "height: 500px; overflow-y: auto;"
            )
            log_display.classes("log-display")

            @ui.refreshable
            def refresh_logs():
                try:
                    lines = log_path.read_text(encoding="utf-8", errors="replace").strip().split("\n")
                    level = level_filter.value

                    if level != "ALL":
                        filtered = [l for l in lines if f"| {level}" in l or f"| {level.upper()}" in l]
                    else:
                        filtered = lines[-200:]  # 最近200行

                    log_display.content = "\n".join(filtered[::-1]) if filtered else "无匹配日志"
                except Exception as e:
                    log_display.content = f"读取日志失败: {e}"

            refresh_logs()

        else:
            ui.label(f"日志文件不存在: {log_path}").style("color: var(--color-warning); padding: 16px 0;")
            ui.label("请检查 logging.file_path 配置或等待首次日志生成").style("font-size: 12px; color: var(--color-text-tertiary);")
