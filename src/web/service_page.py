"""服务状态管理页面"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime

from nicegui import ui

from ..utils.signer_manager import get_signer_manager

# 全局引用，供定时器使用
_signer_dot = None
_signer_label = None
_signer_badge = None
_signer_pid_label = None


def create_service_page():
    """创建服务状态管理页面"""
    global _signer_dot, _signer_label, _signer_badge, _signer_pid_label

    with ui.element("div").classes("app-card w-full q-pa-lg"):
        ui.label("服务管理").classes("section-title q-mb-lg")

        # 说明卡片
        with ui.element("div").classes("q-pa-md service-card q-mb-lg").style(
            "background:#f8fafc; border-left:4px solid #6366F1;"
        ):
            with ui.row().classes("items-center"):
                ui.html('<span style="font-size:1.2rem">ℹ️</span>')
                ui.label("本项目共 2 个服务").classes("q-ml-sm text-body2 font-weight-bold")
            ui.label(
                "① 签名服务（Node.js）— a_bogus 签名生成，默认端口 3010\n"
                "② 主服务（Python/NiceGUI）— Web UI + 采集检测，当前页面服务"
            ).classes("text-caption text-grey q-mt-sm").style("line-height:1.8;")

        # 服务状态网格
        with ui.grid(columns=2).classes("w-full q-col-gutter-md"):
            # --- 签名服务 ---
            with ui.element("div").classes("q-pa-md service-card"):
                with ui.row().classes("items-center q-mb-sm"):
                    ui.html('<span style="font-size:1.5rem">⚙️</span>')
                    ui.label("签名服务").classes("text-h6 q-ml-sm")
                    _signer_badge = ui.badge("检测中...", color="grey").classes("q-ml-auto")

                ui.label("Node.js a_bogus 签名服务 · 端口 3010").classes("text-caption text-grey q-mb-md")

                with ui.row().classes("items-center q-gutter-sm"):
                    _signer_dot = ui.html('<span style="color:#aaa">●</span>')
                    _signer_label = ui.label("检测中...")
                    _signer_pid_label = ui.label("").classes("text-caption text-grey q-ml-auto")

                with ui.row().classes("q-gutter-sm q-mt-md"):
                    ui.button("🔄 重启签名服务", on_click=_restart_signer).classes("restart-btn")
                    ui.button("🔍 立即检测", on_click=_check_signer).classes("check-btn")

                # 日志输出区
                _signer_log = ui.log().classes("w-full q-mt-sm").style(
                    "height:100px; font-size:0.7rem; background:#1e1e1e; color:#aaa; padding:8px; border-radius:8px;"
                )
                _signer_log.push(f"[{_ts()}] 正在检测服务状态...\n")

                # 自动检测
                async def _auto_check():
                    manager = get_signer_manager()
                    pid = manager._process.pid if manager._process else None
                    is_healthy = await manager.check_health()
                    if is_healthy:
                        _signer_dot.set_content('<span style="color:#4caf50">●</span>')
                        _signer_label.set_text(f"运行正常")
                        _signer_pid_label.set_text(f"PID {pid}")
                        _signer_badge.set_text("运行中")
                        _signer_badge.style = "background:#4caf50"
                    else:
                        _signer_dot.set_content('<span style="color:#f44336">●</span>')
                        _signer_label.set_text("未运行，请点击重启")
                        _signer_pid_label.set_text("")
                        _signer_badge.set_text("已停止")
                        _signer_badge.style = "background:#f44336"
                    _signer_log.push(f"[{_ts()}] {'✅ 健康' if is_healthy else '❌ 异常'}\n")

                ui.timer(10.0, _auto_check)

            # --- 主服务 ---
            with ui.element("div").classes("q-pa-md service-card"):
                with ui.row().classes("items-center q-mb-sm"):
                    ui.html('<span style="font-size:1.5rem">🖥️</span>')
                    ui.label("主服务").classes("text-h6 q-ml-sm")
                    ui.badge("运行中", color="green").classes("q-ml-auto")

                ui.label("NiceGUI Web UI + 采集 + 检测 · 端口 9527").classes("text-caption text-grey q-mb-md")

                with ui.row().classes("items-center q-gutter-sm"):
                    ui.html('<span style="color:#4caf50">●</span>')
                    ui.label("Web UI 运行中").classes("text-body2")
                    ui.label(f"PID: {_current_pid()}").classes("text-caption text-grey q-ml-auto")

                with ui.row().classes("q-gutter-sm q-mt-md"):
                    ui.button("📋 查看主日志", on_click=_show_main_logs).classes("restart-btn")
                    ui.button("🔄 重启主服务", on_click=_restart_main_tip).classes("check-btn")


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _current_pid() -> str:
    import os
    return str(os.getpid())


async def _restart_signer():
    """重启签名服务"""
    manager = get_signer_manager()
    ui.notify("正在重启签名服务...", type="info", timeout=2000)
    try:
        await manager.stop()
        await asyncio.sleep(1)
        success = await manager.start_if_needed()
        if success:
            ui.notify("✅ 签名服务重启成功", type="positive", timeout=3000)
        else:
            ui.notify("❌ 重启失败，请检查 Node.js 是否安装", type="negative", timeout=5000)
    except Exception as e:
        ui.notify(f"❌ 异常: {e}", type="negative", timeout=5000)


async def _check_signer():
    """手动检测"""
    manager = get_signer_manager()
    is_healthy = await manager.check_health()
    pid = manager._process.pid if manager._process else None
    ui.notify(
        "✅ 签名服务健康" if is_healthy else f"❌ 签名服务未运行 (PID: {pid})",
        type="positive" if is_healthy else "negative",
        timeout=3000,
    )


def _restart_main_tip():
    ui.notify("请在终端手动重启: python app.py", type="warning", timeout=4000)


def _show_main_logs():
    ui.notify("请在终端执行: tail -f logs/douyin_ppp.log", type="info", timeout=4000)
