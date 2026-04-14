"""布局组件 — 统一导航栏、主题切换"""

from __future__ import annotations

from nicegui import ui, app

from .styles import inject_global_styles


# 页面导航配置
NAV_ITEMS = [
    {"key": "dashboard", "label": "首页", "icon": "home", "path": "/"},
    {"key": "monitor", "label": "监控管理", "icon": "people", "path": "/monitor"},
    {"key": "danmaku", "label": "实时弹幕", "icon": "chat", "path": "/danmaku"},
    {"key": "video_scrape", "label": "评论抓取", "icon": "movie", "path": "/video_scrape"},
    {"key": "wordcloud", "label": "词云分析", "icon": "cloud", "path": "/wordcloud"},
    {"key": "data", "label": "数据导出", "icon": "analytics", "path": "/data"},
    {"key": "service", "label": "服务管理", "icon": "settings_applications", "path": "/service"},
    {"key": "config", "label": "配置管理", "icon": "settings", "path": "/config"},
    {"key": "logs", "label": "日志", "icon": "article", "path": "/logs"},
]


def create_theme_toggle():
    """创建主题切换按钮"""
    # 读取当前主题偏好
    is_dark = app.storage.general.get("dark", False)

    icon = "dark_mode" if not is_dark else "light_mode"

    toggle_btn = ui.button(icon=icon, on_click=lambda: _toggle_theme()).props(
        "flat round size=sm unelevated"
    ).classes("theme-toggle")

    # 页面加载时应用已保存的主题
    if is_dark:
        ui.run_javascript('document.body.classList.add("body--dark")')

    return toggle_btn


def _toggle_theme():
    """切换亮/暗色主题"""
    current = app.storage.general.get("dark", False)
    new_dark = not current
    app.storage.general["dark"] = new_dark

    # 通过 JS 切换 Quasar 暗色模式
    if new_dark:
        ui.run_javascript('document.body.classList.add("body--dark")')
    else:
        ui.run_javascript('document.body.classList.remove("body--dark")')

    # 刷新页面以更新图标
    ui.navigate.to(ui.navigate.current_path)


def create_app_header(active_page: str = "dashboard", immersive: bool = False):
    """
    创建统一的全局导航栏。

    Args:
        active_page: 当前激活的页面 key（对应 NAV_ITEMS 中的 key）
        immersive: 是否使用沉浸式暗色变体（弹幕页使用）
    """
    # 每个页面注入全局样式
    inject_global_styles()
    
    header_class = "app-header"
    if immersive:
        header_class = "app-header app-header-immersive"

    with ui.header().classes(f"{header_class} q-pa-none"):
        with ui.row().classes("items-center w-full q-px-lg").style("height: 56px;"):
            # 品牌标识
            brand_label = ui.label("Douyin PPP").classes("nav-brand q-mr-xl")
            if immersive:
                brand_label.classes(remove="nav-brand", add="nav-brand").style("color: #F9FAFB;")

            # 页面导航标签
            with ui.row().classes("items-center gap-sm"):
                for item in NAV_ITEMS:
                    is_active = item["key"] == active_page
                    tab_class = "nav-tab"
                    if is_active:
                        tab_class += " nav-tab-active"
                    if immersive and not is_active:
                        tab_class += " nav-tab-immersive"

                    tab = ui.label(item["label"]).classes(tab_class).on("click", lambda path=item["path"]: ui.navigate.to(path))

                    if immersive:
                        tab.style("color: #D1D5DB;" if not is_active else "color: #818CF8;")

            ui.space()

            # 主题切换
            if not immersive:
                create_theme_toggle()
            else:
                # 弹幕页使用简化的亮/暗切换
                create_theme_toggle()


def create_confirm_dialog(title: str, message: str, on_confirm, on_cancel=None):
    """
    创建确认弹窗。

    Args:
        title: 弹窗标题
        message: 提示信息
        on_confirm: 确认回调
        on_cancel: 取消回调（可选）
    """
    with ui.dialog().classes("confirm-dialog") as dialog, ui.card().classes("q-pa-lg"):
        ui.label(title).classes("confirm-dialog-title q-mb-sm")
        ui.label(message).classes("confirm-dialog-body q-mb-lg")

        with ui.row().classes("w-full justify-end gap-sm"):
            ui.button("取消", on_click=lambda: _cancel_dialog(dialog, on_cancel)).classes("btn-ghost").props("flat")
            ui.button("确认", on_click=lambda: _confirm_dialog(dialog, on_confirm)).classes("btn-primary").props("unelevated color=negative")

    dialog.open()
    return dialog


def _confirm_dialog(dialog, on_confirm):
    """确认操作"""
    dialog.close()
    if on_confirm:
        on_confirm()


def _cancel_dialog(dialog, on_cancel):
    """取消操作"""
    dialog.close()
    if on_cancel:
        on_cancel()
