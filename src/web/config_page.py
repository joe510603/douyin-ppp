"""配置管理页面 — Cookie/Token 配置、参数设置"""

from __future__ import annotations

import asyncio

from nicegui import ui, run

from ..config import get_config, save_config, reload_config
from ..utils.cookie_manager import get_cookie_status, fetch_cookie_by_browser
from src.utils.logger import get_logger

log = get_logger("config_page")


def _render_cookie_status_label(cookie: str) -> ui.label:
    """根据 Cookie 状态返回对应颜色的标签"""
    status = get_cookie_status(cookie)
    if status == "valid":
        label = ui.label("✅ 有效")
        ui.add_css("""<style>.cookie-status-valid { color: #22c55e; font-weight: bold; }</style>""")
        label.classes("cookie-status-valid")
    elif status == "expiring_soon":
        label = ui.label("⚠️ 即将在 24h 内过期")
        ui.add_css("""<style>.cookie-status-expiring { color: #eab308; font-weight: bold; }</style>""")
        label.classes("cookie-status-expiring")
    else:
        label = ui.label("❌ 已失效")
        ui.add_css("""<style>.cookie-status-expired { color: #ef4444; font-weight: bold; }</style>""")
        label.classes("cookie-status-expired")
    return label


def create_config_page():
    """创建配置管理页面"""

    config = get_config()

    # Cookie 配置
    with ui.card().classes("w-full"):
        ui.label("🔐 抖音登录凭证").classes("text-h6 q-mb-sm")
        ui.label(
            "用于开播检测和 WebSocket 鉴权。最关键的是 ttwid 字段。\n"
            "获取方式：浏览器登录 douyin.com → F12 → Application → Cookies → 复制全部或仅 ttwid"
        ).classes("text-caption text-grey-6 q-mb-md")

        # Cookie 状态行
        with ui.row().classes("items-center gap-2 q-mb-md"):
            ui.label("状态：").classes("text-body2")
            status_label = _render_cookie_status_label(config.douyin.cookie)

        cookie_textarea = ui.textarea(
            label="Cookie",
            value=config.douyin.cookie,
            placeholder="粘贴完整的 Cookie 字符串或仅 ttwid=xxx...",
        ).props("rows=4 autogrow outlined").classes("w-full")

        def save_cookie():
            config.douyin.cookie = cookie_textarea.value or ""
            try:
                save_config(config)
                reload_config()
                # 刷新状态标签
                status_label.set_text(_get_status_text(config.douyin.cookie))
                _update_status_label(status_label, config.douyin.cookie)
                ui.notify("Cookie 已保存 ✅", type="positive")
            except Exception as e:
                ui.notify(f"保存失败: {e}", type="negative")

        def _get_status_text(cookie: str) -> str:
            status = get_cookie_status(cookie)
            if status == "valid":
                return "✅ 有效"
            elif status == "expiring_soon":
                return "⚠️ 即将在 24h 内过期"
            else:
                return "❌ 已失效"

        def _update_status_label(label_ui, cookie: str):
            status = get_cookie_status(cookie)
            # 移除旧状态 class
            label_ui.classes(clear=True)
            if status == "valid":
                label_ui.text("✅ 有效")
                label_ui.classes("cookie-status-valid")
            elif status == "expiring_soon":
                label_ui.text("⚠️ 即将在 24h 内过期")
                label_ui.classes("cookie-status-expiring")
            else:
                label_ui.text("❌ 已失效")
                label_ui.classes("cookie-status-expired")

        async def _on_auto_fetch():
            """自动获取 Cookie（Playwright headful 方式）"""
            btn_fetch.props("loading")  # 显示 loading 态
            ui.notify("正在启动浏览器，请在弹出的窗口中登录抖音...", type="info", timeout=0)
            
            try:
                # 使用 run.cpu_bound 在后台线程运行
                cookie = await run.cpu_bound(fetch_cookie_by_browser)
                
                if cookie:
                    cookie_textarea.set_value(cookie)
                    save_cookie()
                    _update_status_label(status_label, get_config().douyin.cookie)
                    ui.notify("✅ Cookie 获取成功！", type="positive")
                else:
                    ui.notify("❌ 未能获取到有效 Cookie", type="negative")
                    
            except ImportError as e:
                ui.notify(f"❌Playwright 未安装，请先运行: pip install playwright && playwright install chromium", type="negative", timeout=8000)
                log.error(f"Playwright 未安装: {e}")
            except Exception as e:
                ui.notify(f"❌ 获取失败: {e}", type="negative", timeout=5000)
                log.error(f"自动获取 Cookie 失败: {e}", exc_info=True)
            finally:
                btn_fetch.props(remove="loading")

        btn_fetch = ui.button("🌐 自动获取 Cookie", on_click=_on_auto_fetch).props(
            "unelevated color=accent"
        )

        ui.button("💾 保存 Cookie", on_click=save_cookie).props("unelevated color=primary")

    ui.separator()

    # 采集参数配置
    with ui.card().classes("w-full q-mt-md"):
        ui.label("⚙️ 采集参数").classes("text-h6 q-mb-sm")

        with ui.expansion("高级参数").props("dense"):

            detect_interval = ui.number(
                "检测间隔（秒）",
                value=config.live_detection.interval,
                min=10, max=600, step=10,
            ).props("dense")

            ws_heartbeat = ui.number(
                "心跳间隔（秒）",
                value=config.websocket.heartbeat_interval,
                min=1, max=30, step=1,
            ).props("dense")

            flush_int = ui.number(
                "数据库写入间隔（秒）",
                value=config.storage.flush_interval,
                min=1, max=60, step=1,
            ).props("dense")

            log_unknown = ui.switch(
                "记录未知消息原始数据",
                value=config.websocket.log_unknown_messages,
            )

            def save_params():
                config.live_detection.interval = int(detect_interval.value)
                config.websocket.heartbeat_interval = int(ws_heartbeat.value)
                config.storage.flush_interval = int(flush_int.value)
                config.websocket.log_unknown_messages = bool(log_unknown.value)
                try:
                    save_config(config)
                    reload_config()
                    ui.notify("参数已保存 ✅", type="positive")
                except Exception as e:
                    ui.notify(f"保存失败: {e}", type="negative")

            ui.button("保存参数", on_click=save_params).props("unelevated color=primary q-mt-md")
