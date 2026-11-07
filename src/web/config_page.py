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
                options={
                    "openai": "OpenAI",
                    "deepseek": "DeepSeek",
                    "minimax": "MiniMax（Coding Plan·国内）",
                    "minimax_intl": "MiniMax（Coding Plan·国际）",
                    "local": "本地模型（Ollama/vLLM）",
                },
                value=config.llm.provider,
                label="提供商",
            ).props("dense outlined").classes("col-6 col-sm-3 app-input")
            
            llm_model = ui.input(
                "模型名称",
                value=config.llm.model,
                placeholder="gpt-4o-mini / deepseek-chat / MiniMax-M3",
            ).props("dense outlined").classes("col-6 col-sm-3 app-input")

        with ui.row().classes("w-full items-end q-col-gutter-sm q-mt-sm"):
            llm_api_key = ui.input(
                "API Key",
                value=config.llm.api_key,
                placeholder="sk-... / MiniMax API Key",
                password=True,
            ).props("dense outlined").classes("col-12 col-sm-4 app-input")
            
            llm_base_url = ui.input(
                "API Base URL（可选）",
                value=config.llm.base_url,
                placeholder="留空则按提供商自动填：minimax → https://api.minimaxi.com/v1",
            ).props("dense outlined").classes("col-12 col-sm-4 app-input")

        ui.label(
            "提示：选 MiniMax 后 Base URL 留空即可自动使用 https://api.minimaxi.com/v1；"
            "模型填 MiniMax-M3。抖音号筛选与留言生成都走这个模型。"
        ).style("font-size: 12px; color: var(--color-text-tertiary); margin-top: 8px; line-height: 1.6;")

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

    # ========== 购车意向线索筛选 ==========
    with ui.element("div").classes("app-card w-full q-mt-lg q-pa-lg page-enter-delay-4"):
        ui.label("购车意向线索").classes("section-title q-mb-md")
        ui.label(
            "实时规则筛查：弹幕进来即用关键词+权重判定购车意向，"
            "**不需要 LLM**，零成本零延迟，短命直播间也不漏。"
            "LLM（MiniMax / 本地 Ollama）只作为可选的第二层精排。"
        ).style("font-size: 13px; color: var(--color-text-tertiary); margin-bottom: 16px; line-height: 1.6;")

        lead_enabled_switch = ui.switch("启用购车意向分析", value=config.lead.enabled).classes("q-mb-md")
        lead_realtime_switch = ui.switch(
            "实时筛查（弹幕到达即判定）", value=config.lead.realtime_enabled
        ).classes("q-mb-md")
        lead_realtime_push = ui.switch(
            "命中后实时推送飞书群", value=config.lead.realtime_push
        ).classes("q-mb-md")

        with ui.row().classes("w-full items-end q-col-gutter-sm"):
            lead_min_level = ui.select(
                options={
                    "high": "仅高意向",
                    "medium": "中意向及以上",
                    "low": "低意向及以上（推荐）",
                },
                value=config.lead.min_intent_level,
                label="最低意向等级",
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            lead_cooldown = ui.number(
                "同用户推送间隔（分钟）",
                value=config.lead.push_cooldown_minutes,
                min=0, max=1440, step=5,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            lead_lookback = ui.number(
                "AI 扫描回溯（小时）",
                value=config.lead.lookback_hours,
                min=1, max=168, step=1,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            lead_batch = ui.number(
                "每批分析用户数",
                value=config.lead.batch_size,
                min=5, max=100, step=5,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

        def save_lead_config():
            config.lead.enabled = lead_enabled_switch.value
            config.lead.realtime_enabled = lead_realtime_switch.value
            config.lead.realtime_push = lead_realtime_push.value
            config.lead.min_intent_level = lead_min_level.value or "low"
            config.lead.push_cooldown_minutes = int(lead_cooldown.value or 30)
            config.lead.lookback_hours = int(lead_lookback.value or 24)
            config.lead.batch_size = int(lead_batch.value or 20)
            try:
                save_config(config)
                reload_config()
                ui.notify("线索筛选配置已保存", type="positive")
            except Exception as e:
                ui.notify(f"保存失败: {e}", type="negative")

        ui.button("保存线索配置", on_click=save_lead_config).classes("btn-primary q-mt-md").props("unelevated icon=save")

    # ========== 飞书通知 ==========
    with ui.element("div").classes("app-card w-full q-mt-lg q-pa-lg page-enter-delay-4"):
        ui.label("飞书群通知").classes("section-title q-mb-md")
        ui.label(
            "只需群机器人的 Webhook 地址，不需要企业自建应用。"
            "获取方式：飞书群 → 设置 → 群机器人 → 添加机器人 → 复制 Webhook 地址。"
            "若开启了「签名校验」，把密钥填进下方 Secret。"
        ).style("font-size: 13px; color: var(--color-text-tertiary); margin-bottom: 16px; line-height: 1.6;")

        notify_enabled_switch = ui.switch("启用飞书通知", value=config.notify.enabled).classes("q-mb-md")

        with ui.row().classes("w-full items-end q-col-gutter-sm"):
            notify_webhook = ui.input(
                "Webhook 地址",
                value=config.notify.webhook_url,
                placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
            ).props("dense outlined").classes("col-12 col-sm-7 app-input")

            notify_secret = ui.input(
                "签名密钥（可选）",
                value=config.notify.secret,
                placeholder="开启签名校验时填写",
                password=True,
            ).props("dense outlined").classes("col-12 col-sm-5 app-input")

        with ui.row().classes("w-full items-end q-col-gutter-sm q-mt-sm"):
            notify_levels = ui.select(
                options={
                    "high": "仅高意向",
                    "high,medium": "高 + 中意向",
                    "high,medium,low": "全部等级",
                },
                value=",".join(config.notify.push_levels),
                label="推送等级",
            ).props("dense outlined").classes("col-6 col-sm-3 app-input")

            notify_contact_only = ui.switch(
                "仅推送有联系方式的线索", value=config.notify.contact_only
            ).classes("col-6 col-sm-3")

            notify_max_per = ui.number(
                "单条消息最多线索数",
                value=config.notify.max_per_message,
                min=1, max=20, step=1,
            ).props("dense outlined").classes("col-6 col-sm-3 app-input")

        def save_notify_config():
            config.notify.enabled = notify_enabled_switch.value
            config.notify.webhook_url = (notify_webhook.value or "").strip()
            config.notify.secret = (notify_secret.value or "").strip()
            config.notify.push_levels = [
                x for x in (notify_levels.value or "high").split(",") if x
            ]
            config.notify.contact_only = notify_contact_only.value
            config.notify.max_per_message = int(notify_max_per.value or 5)
            try:
                save_config(config)
                reload_config()
                ui.notify("飞书通知配置已保存", type="positive")
            except Exception as e:
                ui.notify(f"保存失败: {e}", type="negative")

        ui.button("保存飞书配置", on_click=save_notify_config).classes("btn-primary q-mt-md").props("unelevated icon=save")

        # ---- 多维表格（线索表）----
        with ui.expansion("多维表格（线索清单）", value=False).props("dense").classes("w-full q-mt-md"):
            ui.label(
                "把筛出的线索沉淀成一张多维表格，销售在飞书里直接跟进、分配、标记状态。"
                "表格的 App Token 和数据表 ID 从表格链接里取。"
            ).style("font-size: 12px; color: var(--color-text-tertiary); margin-bottom: 12px; line-height: 1.6;")

            bitable_enabled_switch = ui.switch(
                "启用多维表格同步", value=config.notify.bitable_enabled
            ).classes("q-mb-md")

            with ui.row().classes("w-full items-end q-col-gutter-sm q-mb-md"):
                bitable_backend = ui.select(
                    options={
                        "auto": "自动（有 App 凭据走直连，否则走 lark-cli）",
                        "http": "直连开放平台（App 凭据 / 用户 Token）",
                        "lark_cli": "lark-cli（认证与续期由 lark-cli 负责）",
                    },
                    value=config.notify.bitable_backend or "auto",
                    label="写入后端",
                ).props("dense outlined").classes("col-12 col-sm-5 app-input")

                bitable_levels = ui.select(
                    options={
                        "high": "高意向",
                        "medium": "中意向",
                        "low": "低意向",
                    },
                    value=config.notify.bitable_levels or ["high", "medium", "low"],
                    label="自动写入的意向等级",
                    multiple=True,
                ).props("dense outlined").classes("col-12 col-sm-4 app-input")

                bitable_cooldown = ui.number(
                    "写表冷却（分钟）", value=config.notify.bitable_cooldown_minutes,
                    min=0, max=1440, step=1,
                ).props("dense outlined").classes("col-12 col-sm-3 app-input")

            with ui.row().classes("w-full items-end q-col-gutter-sm"):
                bitable_app_id = ui.input(
                    "App ID", value=config.notify.bitable_app_id, placeholder="cli_xxxxxxxx"
                ).props("dense outlined").classes("col-12 col-sm-4 app-input")

                bitable_app_secret = ui.input(
                    "App Secret", value=config.notify.bitable_app_secret, password=True
                ).props("dense outlined").classes("col-12 col-sm-4 app-input")

                bitable_app_token = ui.input(
                    "App Token", value=config.notify.bitable_app_token, placeholder="bascnXXXX 或 wiki 解析出的 obj_token"
                ).props("dense outlined").classes("col-12 col-sm-4 app-input")

            with ui.row().classes("w-full items-end q-col-gutter-sm q-mt-sm"):
                bitable_table_id = ui.input(
                    "数据表 ID", value=config.notify.bitable_table_id, placeholder="tblXXXX"
                ).props("dense outlined").classes("col-12 col-sm-4 app-input")

                bitable_user_token = ui.input(
                    "用户 Token（备选）", value=config.notify.bitable_user_token,
                    placeholder="留空则用上面的 App ID/Secret"
                ).props("dense outlined").classes("col-12 col-sm-4 app-input")

                bitable_lark_cli_identity = ui.select(
                    options={"user": "用户身份", "bot": "绑定应用"},
                    value=config.notify.bitable_lark_cli_identity or "user",
                    label="lark-cli 身份",
                ).props("dense outlined").classes("col-12 col-sm-4 app-input")

            with ui.row().classes("w-full items-end q-col-gutter-sm q-mt-sm"):
                bitable_lark_cli_path = ui.input(
                    "lark-cli 路径", value=config.notify.bitable_lark_cli_path,
                    placeholder="留空自动探测；launchd 环境建议显式填写绝对路径"
                ).props("dense outlined").classes("col-12 col-sm-12 app-input")

            ui.label(
                "「直连」需要企业自建应用并开通多维表格读写权限；"
                "「lark-cli」复用已授权的飞书身份，token 过期由 lark-cli 自己续期，适合后台自跑。"
            ).classes("text-caption text-grey-7 q-mb-md")

            def save_bitable_config():
                config.notify.bitable_enabled = bitable_enabled_switch.value
                config.notify.bitable_backend = (bitable_backend.value or "auto").strip()
                config.notify.bitable_levels = list(bitable_levels.value or [])
                config.notify.bitable_cooldown_minutes = int(bitable_cooldown.value or 0)
                config.notify.bitable_app_id = (bitable_app_id.value or "").strip()
                config.notify.bitable_app_secret = (bitable_app_secret.value or "").strip()
                config.notify.bitable_app_token = (bitable_app_token.value or "").strip()
                config.notify.bitable_table_id = (bitable_table_id.value or "").strip()
                config.notify.bitable_user_token = (bitable_user_token.value or "").strip()
                config.notify.bitable_lark_cli_identity = (
                    bitable_lark_cli_identity.value or "user"
                ).strip()
                config.notify.bitable_lark_cli_path = (bitable_lark_cli_path.value or "").strip()
                try:
                    save_config(config)
                    reload_config()
                    ui.notify("多维表格配置已保存", type="positive")
                except Exception as e:
                    ui.notify(f"保存失败: {e}", type="negative")

            ui.button("保存多维表格配置", on_click=save_bitable_config).classes("btn-primary q-mt-md").props("unelevated icon=save")

    # ========== 抖音触达 ==========
    with ui.element("div").classes("app-card w-full q-mt-lg q-pa-lg page-enter-delay-4"):
        ui.label("抖音触达（搜人 + 作品留言）").classes("section-title q-mb-md")
        ui.label(
            "在抖音真实界面搜索线索的抖音号，定位其主页并给作品留言。"
            "⚠️ 自动化操作有触发平台风控的可能，请务必保持限速与每日上限，勿调高。"
        ).style("font-size: 13px; color: var(--color-text-tertiary); margin-bottom: 8px; line-height: 1.6;")

        ui.label(
            "🔑 前置条件：需要**已登录**的抖音 Cookie（含 sessionid）。"
            "请到上方「抖音 Cookie / 视频评论 Cookie」里点「自动获取 Cookie」扫码登录，"
            "否则搜索页会提示「登录后即可搜索」，触达无法执行。"
        ).style("font-size: 12px; color: var(--color-text-tertiary); margin-bottom: 16px; line-height: 1.6;")

        outreach_enabled_switch = ui.switch("启用抖音触达", value=config.outreach.enabled).classes("q-mb-md")

        with ui.row().classes("w-full items-end q-col-gutter-sm"):
            outreach_daily = ui.number(
                "每日留言上限",
                value=config.outreach.daily_limit,
                min=1, max=200, step=1,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            outreach_min_delay = ui.number(
                "最小间隔（秒）",
                value=config.outreach.min_delay,
                min=10, max=600, step=5,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            outreach_max_delay = ui.number(
                "最大间隔（秒）",
                value=config.outreach.max_delay,
                min=10, max=900, step=5,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            outreach_max_run = ui.number(
                "单轮最多处理",
                value=config.outreach.max_per_run,
                min=1, max=50, step=1,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

        with ui.row().classes("w-full items-end q-col-gutter-sm q-mt-sm"):
            outreach_fail_stop = ui.number(
                "连续失败几次停止",
                value=config.outreach.fail_stop_threshold,
                min=1, max=10, step=1,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            outreach_per_user = ui.number(
                "每人留言作品数",
                value=config.outreach.comments_per_user,
                min=1, max=5, step=1,
            ).props("dense outlined").classes("col-6 col-sm-2 app-input")

            outreach_template = ui.input(
                "留言模板（留空则由 AI 生成）",
                value=config.outreach.message_template,
                placeholder="看到你在{live_room}聊到{model}，方便留个联系方式细聊吗？",
            ).props("dense outlined").classes("col-12 col-sm-8 app-input")

        def save_outreach_config():
            config.outreach.enabled = outreach_enabled_switch.value
            config.outreach.daily_limit = int(outreach_daily.value or 30)
            config.outreach.min_delay = float(outreach_min_delay.value or 45)
            config.outreach.max_delay = float(outreach_max_delay.value or 120)
            config.outreach.max_per_run = int(outreach_max_run.value or 10)
            config.outreach.fail_stop_threshold = int(outreach_fail_stop.value or 3)
            config.outreach.comments_per_user = int(outreach_per_user.value or 1)
            config.outreach.message_template = outreach_template.value or ""
            try:
                save_config(config)
                reload_config()
                ui.notify("触达配置已保存", type="positive")
            except Exception as e:
                ui.notify(f"保存失败: {e}", type="negative")

        ui.button("保存触达配置", on_click=save_outreach_config).classes("btn-primary q-mt-md").props("unelevated icon=save")

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
