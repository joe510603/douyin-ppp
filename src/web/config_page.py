"""配置管理页面 — Cookie/Token 配置、参数设置"""

from __future__ import annotations

import asyncio

from nicegui import ui, run

from ..config import get_config, save_config, reload_config
from ..utils.cookie_manager import get_cookie_status, fetch_cookie_by_browser
from src.utils.logger import get_logger

log = get_logger("config_page")


def _mask_cookie(cookie: str, show_len: int = 30) -> str:
    """隐藏 Cookie 内容，仅显示前后部分"""
    if not cookie or len(cookie) <= show_len:
        return "****" if cookie else ""
    return f"{cookie[:15]}...{cookie[-15:]} (已隐藏，点击眼睛图标显示)"


def _render_cookie_status_label(cookie: str) -> ui.label:
    """根据 Cookie 状态返回对应颜色的标签"""
    status = get_cookie_status(cookie)
    if status == "valid":
        label = ui.label("有效").classes("app-tag app-tag-success")
    elif status == "expiring_soon":
        label = ui.label("即将在 24h 内过期").classes("app-tag app-tag-warning")
    else:
        label = ui.label("已失效").classes("app-tag app-tag-danger")
    return label


def create_config_page():
    """创建配置管理页面"""

    config = get_config()

    # ========== 抖音 Cookie 配置 ==========
    with ui.element("div").classes("app-card w-full q-pa-lg page-enter"):
        ui.label("抖音登录凭证").classes("section-title q-mb-sm")
        ui.label(
            "用于开播检测和 WebSocket 鉴权。最关键的是 ttwid 字段。\n"
            "获取方式：浏览器登录 douyin.com → F12 → Application → Cookies → 复制全部或仅 ttwid"
        ).style("font-size: 13px; color: var(--color-text-tertiary); margin-bottom: 16px; line-height: 1.6;")

        # Cookie 状态行
        with ui.row().classes("items-center gap-sm q-mb-md"):
            ui.label("状态：").style("font-size: 14px; color: var(--color-text-secondary);")
            dy_status_label = _render_cookie_status_label(config.douyin.cookie)

        # Cookie 隐藏显示状态
        dy_show_cookie = [False]  # 用列表存储可变状态

        dy_cookie_textarea = ui.textarea(
            label="Cookie",
            value=_mask_cookie(config.douyin.cookie) if config.douyin.cookie else "",
            placeholder="粘贴完整的 Cookie 字符串或仅 ttwid=xxx...",
        ).props("rows=4 autogrow outlined input-style='font-family: monospace;'").classes("w-full app-input")
        dy_cookie_textarea._real_value = config.douyin.cookie  # 存储真实值

        def _toggle_dy_cookie():
            """切换显示/隐藏"""
            real_value = getattr(dy_cookie_textarea, '_real_value', '')
            if dy_show_cookie[0]:
                dy_cookie_textarea.set_value(real_value)
                dy_toggle_btn.props('icon=visibility_off')
            else:
                dy_cookie_textarea.set_value(_mask_cookie(real_value))
                dy_toggle_btn.props('icon=visibility')
            dy_show_cookie[0] = not dy_show_cookie[0]

        def _on_dy_input(e):
            """输入时更新真实值"""
            dy_cookie_textarea._real_value = e.value if not dy_show_cookie[0] else e.value

        dy_cookie_textarea.on('input', _on_dy_input)

        def save_dy_cookie():
            real_value = getattr(dy_cookie_textarea, '_real_value', dy_cookie_textarea.value)
            config.douyin.cookie = real_value or ""
            try:
                save_config(config)
                reload_config()
                _update_status_label(dy_status_label, config.douyin.cookie)
                ui.notify("抖音 Cookie 已保存", type="positive")
            except Exception as e:
                ui.notify(f"保存失败: {e}", type="negative")

        def _update_status_label(label_ui, cookie: str):
            status = get_cookie_status(cookie)
            # 先移除所有状态相关的类，再添加新的类
            label_ui.classes(remove="app-tag app-tag-success app-tag-warning app-tag-danger")
            if status == "valid":
                label_ui.set_text("有效")
                label_ui.classes("app-tag app-tag-success")
            elif status == "expiring_soon":
                label_ui.set_text("即将在 24h 内过期")
                label_ui.classes("app-tag app-tag-warning")
            else:
                label_ui.set_text("已失效")
                label_ui.classes("app-tag app-tag-danger")

        async def _on_auto_fetch_dy():
            """自动获取抖音 Cookie"""
            btn_fetch_dy.props("loading")
            ui.notify("正在启动浏览器，请在弹出的窗口中登录抖音...", type="info", timeout=0)

            try:
                cookie = await run.cpu_bound(fetch_cookie_by_browser)

                if cookie:
                    dy_cookie_textarea._real_value = cookie
                    dy_cookie_textarea.set_value(_mask_cookie(cookie))
                    save_dy_cookie()
                    _update_status_label(dy_status_label, get_config().douyin.cookie)
                    ui.notify("抖音 Cookie 获取成功", type="positive")
                else:
                    ui.notify("未能获取到有效 Cookie", type="negative")

            except ImportError as e:
                ui.notify(f"Playwright 未安装，请先运行: pip install playwright && playwright install chromium", type="negative", timeout=8000)
                log.error(f"Playwright 未安装: {e}")
            except Exception as e:
                ui.notify(f"获取失败: {e}", type="negative", timeout=5000)
                log.error(f"自动获取 Cookie 失败: {e}", exc_info=True)
            finally:
                btn_fetch_dy.props(remove="loading")

        with ui.row().classes("gap-sm q-mt-md"):
            dy_toggle_btn = ui.button("", on_click=_toggle_dy_cookie).props("icon=visibility flat round dense color=grey").classes("btn-icon")
            btn_fetch_dy = ui.button("自动获取 Cookie", on_click=_on_auto_fetch_dy).classes("btn-secondary").props("unelevated color=accent icon=language")
            ui.button("保存 Cookie", on_click=save_dy_cookie).classes("btn-primary").props("unelevated icon=save")

    # ========== 抖音视频评论 Cookie 配置 ==========
    with ui.element("div").classes("app-card w-full q-mt-lg q-pa-lg page-enter-delay-1"):
        ui.label("抖音视频评论 Cookie").classes("section-title q-mb-sm")
        ui.label(
            "用于视频评论抓取，需要包含 sessionid 等完整登录信息。\n"
            "获取方式：浏览器登录 douyin.com → F12 → Application → Cookies → 复制全部 Cookie\n"
            "注意：与直播监控 Cookie 分开配置，视频抓取需要完整登录状态。"
        ).style("font-size: 13px; color: var(--color-text-tertiary); margin-bottom: 16px; line-height: 1.6;")

        # Cookie 状态行
        with ui.row().classes("items-center gap-sm q-mb-md"):
            ui.label("状态：").style("font-size: 14px; color: var(--color-text-secondary);")
            dy_video_status_label = _render_cookie_status_label(config.douyin_video.cookie)

        dy_video_show_cookie = [False]
        dy_video_cookie_textarea = ui.textarea(
            label="Cookie",
            value=_mask_cookie(config.douyin_video.cookie) if config.douyin_video.cookie else "",
            placeholder="粘贴完整的 Cookie 字符串（包含 sessionid、passport_csrf_token 等）...",
        ).props("rows=4 autogrow outlined input-style='font-family: monospace;'").classes("w-full app-input")
        dy_video_cookie_textarea._real_value = config.douyin_video.cookie

        def _toggle_dy_video_cookie():
            real_value = getattr(dy_video_cookie_textarea, '_real_value', '')
            if dy_video_show_cookie[0]:
                dy_video_cookie_textarea.set_value(real_value)
                dy_video_toggle_btn.props('icon=visibility_off')
            else:
                dy_video_cookie_textarea.set_value(_mask_cookie(real_value))
                dy_video_toggle_btn.props('icon=visibility')
            dy_video_show_cookie[0] = not dy_video_show_cookie[0]

        def _on_dy_video_input(e):
            dy_video_cookie_textarea._real_value = e.value if not dy_video_show_cookie[0] else e.value

        dy_video_cookie_textarea.on('input', _on_dy_video_input)

        def save_dy_video_cookie():
            real_value = getattr(dy_video_cookie_textarea, '_real_value', dy_video_cookie_textarea.value)
            config.douyin_video.cookie = real_value or ""
            try:
                save_config(config)
                reload_config()
                _update_status_label(dy_video_status_label, config.douyin_video.cookie)
                ui.notify("抖音视频评论 Cookie 已保存", type="positive")
            except Exception as e:
                ui.notify(f"保存失败: {e}", type="negative")

        async def _on_auto_fetch_dy_video():
            """自动获取抖音视频评论 Cookie"""
            btn_fetch_dy_video.props("loading")
            ui.notify("正在启动浏览器，请在弹出的窗口中登录抖音...", type="info", timeout=0)

            try:
                cookie = await run.cpu_bound(fetch_cookie_by_browser)

                if cookie:
                    dy_video_cookie_textarea._real_value = cookie
                    dy_video_cookie_textarea.set_value(_mask_cookie(cookie))
                    save_dy_video_cookie()
                    _update_status_label(dy_video_status_label, get_config().douyin_video.cookie)
                    ui.notify("抖音视频评论 Cookie 获取成功", type="positive")
                else:
                    ui.notify("未能获取到有效 Cookie", type="negative")

            except ImportError as e:
                ui.notify(f"Playwright 未安装，请先运行: pip install playwright && playwright install chromium", type="negative", timeout=8000)
                log.error(f"Playwright 未安装: {e}")
            except Exception as e:
                ui.notify(f"获取失败: {e}", type="negative", timeout=5000)
                log.error(f"自动获取 Cookie 失败: {e}", exc_info=True)
            finally:
                btn_fetch_dy_video.props(remove="loading")

        with ui.row().classes("gap-sm q-mt-md"):
            dy_video_toggle_btn = ui.button("", on_click=_toggle_dy_video_cookie).props("icon=visibility flat round dense color=grey").classes("btn-icon")
            btn_fetch_dy_video = ui.button("自动获取 Cookie", on_click=_on_auto_fetch_dy_video).classes("btn-secondary").props("unelevated color=accent icon=language")
            ui.button("保存 Cookie", on_click=save_dy_video_cookie).classes("btn-primary").props("unelevated icon=save")

    # ========== 小红书 Cookie 配置 ==========
    with ui.element("div").classes("app-card w-full q-mt-lg q-pa-lg page-enter-delay-2"):
        ui.label("小红书登录凭证").classes("section-title q-mb-sm")
        ui.label(
            "用于小红书视频评论抓取。\n"
            "获取方式：浏览器登录 xiaohongshu.com → F12 → Application → Cookies → 复制全部 Cookie"
        ).style("font-size: 13px; color: var(--color-text-tertiary); margin-bottom: 16px; line-height: 1.6;")

        # Cookie 状态行
        with ui.row().classes("items-center gap-sm q-mb-md"):
            ui.label("状态：").style("font-size: 14px; color: var(--color-text-secondary);")
            xhs_status_label = _render_cookie_status_label(config.xhs.cookie)

        xhs_show_cookie = [False]
        xhs_cookie_textarea = ui.textarea(
            label="Cookie",
            value=_mask_cookie(config.xhs.cookie) if config.xhs.cookie else "",
            placeholder="粘贴完整的小红书 Cookie...",
        ).props("rows=4 autogrow outlined input-style='font-family: monospace;'").classes("w-full app-input")
        xhs_cookie_textarea._real_value = config.xhs.cookie

        def _toggle_xhs_cookie():
            real_value = getattr(xhs_cookie_textarea, '_real_value', '')
            if xhs_show_cookie[0]:
                xhs_cookie_textarea.set_value(real_value)
                xhs_toggle_btn.props('icon=visibility_off')
            else:
                xhs_cookie_textarea.set_value(_mask_cookie(real_value))
                xhs_toggle_btn.props('icon=visibility')
            xhs_show_cookie[0] = not xhs_show_cookie[0]

        def _on_xhs_input(e):
            xhs_cookie_textarea._real_value = e.value if not xhs_show_cookie[0] else e.value

        xhs_cookie_textarea.on('input', _on_xhs_input)

        def save_xhs_cookie():
            real_value = getattr(xhs_cookie_textarea, '_real_value', xhs_cookie_textarea.value)
            config.xhs.cookie = real_value or ""
            try:
                save_config(config)
                reload_config()
                _update_status_label(xhs_status_label, config.xhs.cookie)
                ui.notify("小红书 Cookie 已保存", type="positive")
            except Exception as e:
                ui.notify(f"保存失败: {e}", type="negative")

        with ui.row().classes("gap-sm q-mt-md"):
            xhs_toggle_btn = ui.button("", on_click=_toggle_xhs_cookie).props("icon=visibility flat round dense color=grey").classes("btn-icon")
            ui.button("保存 Cookie", on_click=save_xhs_cookie).classes("btn-primary").props("unelevated icon=save")

    # ========== LLM 配置 ==========
    with ui.element("div").classes("app-card w-full q-mt-lg q-pa-lg page-enter-delay-3"):
        ui.label("智能分析配置（LLM）").classes("section-title q-mb-sm")
        ui.label(
            "用于词云分析中的智能功能（情感分析、意图分类、竞品对比等）。\n"
            "不配置则仅能使用基础功能（高频词、问题挖掘、时间趋势）。"
        ).style("font-size: 13px; color: var(--color-text-tertiary); margin-bottom: 16px; line-height: 1.6;")

        llm_enabled_switch = ui.switch("启用 LLM 智能分析", value=config.llm.enabled).classes("q-mb-md")

        with ui.row().classes("w-full items-end q-col-gutter-sm"):
            llm_provider = ui.select(
                options=["openai", "deepseek"],
                value=config.llm.provider,
                label="提供商",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")
            
            llm_model = ui.input(
                "模型名称",
                value=config.llm.model,
                placeholder="gpt-4o-mini / deepseek-chat",
            ).props("dense outlined").classes("col-6 col-sm-3 app-input")

        with ui.row().classes("w-full items-end q-col-gutter-sm q-mt-sm"):
            llm_api_key = ui.input(
                "API Key",
                value=config.llm.api_key,
                placeholder="sk-...",
                password=True,
            ).props("dense outlined").classes("col-12 col-sm-4 app-input")
            
            llm_base_url = ui.input(
                "API Base URL（可选）",
                value=config.llm.base_url,
                placeholder="https://api.openai.com/v1",
            ).props("dense outlined").classes("col-12 col-sm-4 app-input")

        # 功能开关
        ui.label("分析功能开关").classes("text-subtitle2 q-mt-md q-mb-sm").style("color: var(--color-text-secondary);")
        with ui.row().classes("gap-md"):
            sentiment_switch = ui.checkbox("情感分析", value=config.llm.features.sentiment)
            intent_switch = ui.checkbox("意图分类", value=config.llm.features.intent)
            competitor_switch = ui.checkbox("竞品对比", value=config.llm.features.competitor)
            profile_switch = ui.checkbox("用户画像", value=config.llm.features.user_profile)

        def save_llm_config():
            config.llm.enabled = llm_enabled_switch.value
            config.llm.provider = llm_provider.value
            config.llm.model = llm_model.value or "gpt-4o-mini"
            config.llm.api_key = llm_api_key.value
            config.llm.base_url = llm_base_url.value
            config.llm.features.sentiment = sentiment_switch.value
            config.llm.features.intent = intent_switch.value
            config.llm.features.competitor = competitor_switch.value
            config.llm.features.user_profile = profile_switch.value
            try:
                save_config(config)
                reload_config()
                ui.notify("LLM 配置已保存", type="positive")
            except Exception as e:
                ui.notify(f"保存失败: {e}", type="negative")

        ui.button("保存 LLM 配置", on_click=save_llm_config).classes("btn-primary q-mt-md").props("unelevated icon=save")

    # ========== 采集参数配置 ==========
    with ui.element("div").classes("app-card w-full q-mt-lg q-pa-lg page-enter-delay-4"):
        ui.label("采集参数").classes("section-title q-mb-md")

        with ui.expansion("高级参数").props("dense"):

            detect_interval = ui.number(
                "检测间隔（秒）",
                value=config.live_detection.interval,
                min=10, max=600, step=10,
            ).props("dense outlined").classes("app-input")

            ws_heartbeat = ui.number(
                "心跳间隔（秒）",
                value=config.websocket.heartbeat_interval,
                min=1, max=30, step=1,
            ).props("dense outlined").classes("app-input")

            flush_int = ui.number(
                "数据库写入间隔（秒）",
                value=config.storage.flush_interval,
                min=1, max=60, step=1,
            ).props("dense outlined").classes("app-input")

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
                    ui.notify("参数已保存", type="positive")
                except Exception as e:
                    ui.notify(f"保存失败: {e}", type="negative")

            ui.button("保存参数", on_click=save_params).classes("btn-primary q-mt-md").props("unelevated icon=save")
