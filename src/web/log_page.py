"""日志查看页面"""

from __future__ import annotations

from pathlib import Path
from nicegui import ui

from ..config import get_config


def create_log_page():
    """创建日志查看页面"""

    config = get_config()
    log_path = Path(config.logging.file_path)

    with ui.card().classes("w-full"):
        ui.label("📝 运行日志").classes("text-h6 q-mb-sm")

        if log_path.exists():
            ui.label(f"日志文件: {log_path}").classes("text-caption text-grey-6")

            # 日志级别筛选
            level_filter = ui.select(
                options=["ALL", "DEBUG", "INFO", "WARNING", "ERROR"],
                value="ALL",
                label="日志级别",
            ).props("dense inline").classes("q-mr-md")

            # 日志内容区域
            log_display = ui.code("").classes("w-full font-mono text-small").style(
                "height: 400px; overflow-y: auto; background: #1e1e1e; color: #d4d4d4;"
            )

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

            # 刷新按钮（在 refresh_logs 定义后）
            ui.button("🔄 刷新", on_click=refresh_logs).props("flat dense").classes("q-mr-md")

            refresh_logs()

        else:
            ui.label(f"日志文件不存在: {log_path}").classes("text-warning q-pa-md")
            ui.label("请检查 logging.file_path 配置或等待首次日志生成").classes("text-caption text-grey-6")
