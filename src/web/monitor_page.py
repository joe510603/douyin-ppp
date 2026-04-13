"""监控管理页面 — 账号增删改查 + 启停控制 + 标签管理"""

from __future__ import annotations

from nicegui import ui

from ..config import get_config, save_config, reload_config
from ..models.comment import MonitorAccount
from .layout import create_confirm_dialog


def create_monitor_page(on_add_account, on_delete_account, on_toggle_account, on_manual_detect=None, on_update_account=None):
    """
    创建监控管理页面。
    
    Args:
        on_add_account: 添加账号回调 (name, sec_user_id) -> None
        on_delete_account: 删除账号回调 (sec_user_id) -> None
        on_toggle_account: 启停切换回调 (sec_user_id, enabled) -> None
        on_manual_detect: 手动检测回调 () -> None
        on_update_account: 编辑账号回调 (sec_user_id, new_name, new_tags) -> None，用于同步运行时状态
    """

    # 顶部操作栏
    with ui.element("div").classes("app-card w-full q-mb-md q-pa-md page-enter"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("监控操作").classes("section-title")
            if on_manual_detect:
                ui.button("立即检测直播状态", on_click=on_manual_detect).props(
                    "unelevated icon=refresh"
                ).classes("btn-primary")
    
    # 添加账号表单
    with ui.element("div").classes("app-card w-full q-pa-lg page-enter-delay-1"):
        ui.label("添加监控账号").classes("section-title q-mb-md")
        
        with ui.row().classes("w-full items-end q-col-gutter-sm"):
            name_input = ui.input("账号名称（自定义）").props("dense outlined").classes("col-12 col-sm-4 app-input")
            
            sec_id_input = ui.input("抖音 sec_user_id").props("dense outlined").classes("col-12 col-sm-4 app-input")
            sec_id_input.set_visibility(False)
        
        # 输入方式切换
        with ui.row().classes("q-mt-sm"):
            ui.button("从 URL 提取", on_click=lambda: [url_input.set_visibility(True), sec_id_input.set_visibility(False)]).classes("btn-ghost").props("flat dense")
            ui.button("直接输入 ID", on_click=lambda: [url_input.set_visibility(False), sec_id_input.set_visibility(True)]).classes("btn-ghost").props("flat dense")
        
        url_input = ui.input("抖音主页链接").props('placeholder="https://v.douyin.com/xxx 或 https://www.douyin.com/user/xxx" dense outlined').classes("w-full q-mt-sm app-input")
        
        async def extract_and_fill():
            url = url_input.value.strip()
            if not url:
                ui.notify("请输入链接", type="warning")
                return
            
            from urllib.parse import parse_qs, urlparse
            import httpx
            
            # 处理短链接
            if "v.douyin.com" in url or "vm.douyin.com" in url:
                ui.notify("正在解析短链接...", type="info")
                try:
                    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
                        resp = await client.get(url)
                        url = str(resp.url)
                        ui.notify(f"已解析: {url[:50]}...", type="info")
                except Exception as e:
                    ui.notify(f"短链接解析失败: {e}", type="warning")
            
            # 提取 sec_user_id
            sid = ""
            if "sec_user_id" in url:
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                sid = qs.get("sec_user_id", [""])[0]
            
            if not sid and "/user/" in url:
                parts = url.split("/user/")
                if len(parts) > 1:
                    sid = parts[1].split("?")[0].split("/")[0]
            
            if sid:
                sec_id_input.value = sid
                sec_id_input.set_visibility(True)
                url_input.set_visibility(False)
                ui.notify(f"已提取 ID: {sid[:20]}...", type="positive")
            else:
                ui.notify("未找到 sec_user_id，请检查链接格式", type="negative")
        
        with ui.row().classes("q-mt-md gap-sm"):
            ui.button("提取并添加", on_click=extract_and_fill).classes("btn-primary").props("unelevated")
        
        def do_add():
            name = name_input.value.strip()
            sid = sec_id_input.value.strip()
            if not name:
                ui.notify("请输入账号名称", type="warning")
                return
            if not sid:
                ui.notify("请输入 sec_user_id", type="warning")
                return
            try:
                on_add_account(name, sid)
                name_input.value = ""
                sec_id_input.value = ""
                url_input.value = ""
                sec_id_input.set_visibility(False)
                url_input.set_visibility(True)
                ui.notify(f"已添加: {name}", type="positive")
                ui.navigate.to("/monitor")
            except Exception as e:
                ui.notify(f"添加失败: {e}", type="negative")
        
        ui.button("确认添加", on_click=do_add).classes("btn-primary q-mt-sm").props("unelevated")
    
    # 账号列表（动态渲染）
    @ui.refreshable
    def account_list_ui():
        config = get_config()
        accounts = config.monitors
        
        if not accounts:
            with ui.element("div").classes("app-card w-full q-pa-xl page-enter-delay-2"):
                ui.label("暂无监控账号，请在上方添加").classes("text-center").style("color: var(--color-text-tertiary);")
            return
        
        with ui.element("div").classes("w-full page-enter-delay-2"):
            ui.label(f"共 {len(accounts)} 个监控账号").classes("section-title q-mb-md")
            
            for acc in accounts:
                status_class = "account-card-live" if acc.enabled else "account-card-idle"
                # 检查是否有 error 状态
                from .. import config as cfg_module
                # 简化：用 enabled 判断
                if not acc.enabled:
                    status_class = "account-card-idle"
                
                with ui.element("div").classes(f"account-card {status_class} w-full q-mb-sm"):
                    with ui.row().classes("items-center justify-between w-full"):
                        with ui.column().classes("col"):
                            ui.label(acc.name).classes("text-weight-bold").style("font-size: 15px; color: var(--color-text-primary);")
                            ui.label(f"ID: {acc.sec_user_id[:24]}{'...' if len(acc.sec_user_id) > 24 else ''}").style("font-size: 12px; color: var(--color-text-tertiary);")
                            # 标签显示
                            if acc.tags:
                                with ui.row().classes("q-mt-xs gap-sm"):
                                    for tag in acc.tags:
                                        ui.label(tag).classes("app-chip")
                            else:
                                ui.label("无标签").style("font-size: 12px; color: var(--color-text-tertiary);")
                        
                        with ui.row().classes("col-auto items-center gap-sm"):
                            # 启/停按钮
                            if acc.enabled:
                                ui.button("已启用", on_click=lambda a=acc: toggle_acc(a)).props("flat dense").classes("btn-ghost").style("color: var(--color-success);")
                            else:
                                ui.button("已暂停", on_click=lambda a=acc: toggle_acc(a)).props("flat dense").classes("btn-ghost").style("color: var(--color-text-tertiary);")
                            
                            # 编辑按钮
                            ui.button("编辑", on_click=lambda a=acc: edit_acc(a)).props("flat dense color=info").classes("btn-ghost")
                            
                            # 删除按钮
                            ui.button("删除", on_click=lambda a=acc: confirm_delete(a)).props("flat dense").classes("btn-ghost").style("color: var(--color-danger);")
    
    def toggle_acc(account: MonitorAccount):
        on_toggle_account(account.sec_user_id, not account.enabled)
        account_list_ui.refresh()
    
    def confirm_delete(account: MonitorAccount):
        create_confirm_dialog(
            title="确认删除",
            message=f"确定要删除监控账号「{account.name}」吗？此操作不可撤销。",
            on_confirm=lambda: delete_acc(account),
        )
    
    def delete_acc(account: MonitorAccount):
        on_delete_account(account.sec_user_id)
        account_list_ui.refresh()
        ui.notify(f"已删除: {account.name}", type="positive")
    
    def edit_acc(account: MonitorAccount):
        """编辑账号弹窗"""
        with ui.dialog().classes("confirm-dialog") as dialog, ui.card().classes("q-pa-lg"):
            ui.label("编辑账号").classes("confirm-dialog-title q-mb-md")
            
            edit_name = ui.input("账号名称", value=account.name).props("dense outlined").classes("w-full app-input")
            
            # 标签管理
            edit_tags = list(account.tags) if account.tags else []
            tags_container = ui.column().classes("w-full q-mb-md")
            
            def render_tags():
                tags_container.clear()
                with tags_container:
                    ui.label("标签").style("font-size: 13px; font-weight: 500; color: var(--color-text-secondary); margin-bottom: 4px;")
                    with ui.row().classes("wrap gap-sm"):
                        for i, tag in enumerate(edit_tags):
                            with ui.element("div").classes("app-chip"):
                                ui.label(tag).style("font-size: 12px;")
                                ui.icon("close", size="14px").classes("app-chip-remove").on("click", lambda idx=i: remove_tag(idx))
                    
                    with ui.row().classes("items-center gap-sm q-mt-sm"):
                        new_tag_input = ui.input(placeholder="输入新标签").props("dense outlined").classes("app-input").style("max-width: 200px;")
                        ui.button("添加", on_click=lambda: add_tag(new_tag_input)).classes("btn-ghost").props("flat dense")
            
            def remove_tag(idx):
                edit_tags.pop(idx)
                render_tags()
            
            def add_tag(input_el):
                val = input_el.value.strip()
                if val and val not in edit_tags:
                    edit_tags.append(val)
                    input_el.value = ""
                    render_tags()
            
            render_tags()
            
            with ui.row().classes("w-full justify-end gap-sm q-mt-md"):
                ui.button("取消", on_click=dialog.close).classes("btn-ghost").props("flat")
                ui.button("保存", on_click=lambda: save_edit(account, edit_name.value, edit_tags, dialog)).classes("btn-primary").props("unelevated")
        
        dialog.open()
    
    def save_edit(account: MonitorAccount, new_name: str, new_tags: list, dialog):
        config = get_config()
        for acc in config.monitors:
            if acc.sec_user_id == account.sec_user_id:
                acc.name = new_name.strip() or acc.name
                acc.tags = new_tags
                break
        save_config(config)
        reload_config()
        # 同步运行时状态
        if on_update_account:
            on_update_account(account.sec_user_id, new_name.strip() or account.name, new_tags)
        dialog.close()
        account_list_ui.refresh()
        ui.notify("账号信息已更新", type="positive")
    
    account_list_ui()
