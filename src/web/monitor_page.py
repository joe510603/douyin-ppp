"""监控管理页面 — 账号增删改查 + 启停控制"""

from __future__ import annotations

from nicegui import ui
from ..config import get_config
from ..models.comment import MonitorAccount


def create_monitor_page(on_add_account, on_delete_account, on_toggle_account, on_manual_detect=None):
    """
    创建监控管理页面。
    
    Args:
        on_add_account: 添加账号回调 (name, sec_user_id) -> None
        on_delete_account: 删除账号回调 (sec_user_id) -> None
        on_toggle_account: 启停切换回调 (sec_user_id, enabled) -> None
        on_manual_detect: 手动检测回调 () -> None
    """
    
    # 顶部操作栏
    with ui.card().classes("w-full q-mb-md"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("🔍 监控操作").classes("text-subtitle2")
            if on_manual_detect:
                ui.button("立即检测直播状态", on_click=on_manual_detect).props(
                    "unelevated color=secondary icon=refresh"
                ).classes("q-ml-auto")
    
    # 添加账号表单
    with ui.card().classes("w-full"):
        ui.label("➕ 添加监控账号").classes("text-h6")
        
        name_input = ui.input("账号名称（自定义）").props("dense")
        sec_id_input = ui.input("抖音 sec_user_id").props("dense").classes("w-full")
        sec_id_input.set_visibility(False)
        
        # 输入方式切换
        def switch_to_url():
            url_input.set_visibility(True)
            sec_id_input.set_visibility(False)
        
        def switch_to_direct():
            url_input.set_visibility(False)
            sec_id_input.set_visibility(True)
        
        with ui.row():
            ui.button("从 URL 提取", on_click=switch_to_url).classes("q-mr-sm")
            ui.button("直接输入 ID", on_click=switch_to_direct)
        
        url_input = ui.input("抖音主页链接").props('placeholder="https://v.douyin.com/xxx 或 https://www.douyin.com/user/xxx" dense').classes("w-full")
        
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
        
        ui.button("提取并添加", on_click=extract_and_fill).props("unelevated color=primary").classes("q-mt-sm")
        
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
                ui.navigate.to("/monitor")  # 刷新页面
            except Exception as e:
                ui.notify(f"添加失败: {e}", type="negative")
        
        ui.button("确认添加", on_click=do_add).props("unelevated").classes("q-mt-sm")
    
    # 账号列表（动态渲染）
    ui.separator()
    
    @ui.refreshable
    def account_list_ui():
        config = get_config()
        accounts = config.monitors
        
        if not accounts:
            ui.label("暂无监控账号，请在上方添加").classes("text-grey q-pa-lg")
            return
        
        ui.label(f"共 {len(accounts)} 个监控账号").classes("text-subtitle2 q-mb-sm")
        
        for acc in accounts:
            with ui.card().classes("w-full q-mb-sm"):
                with ui.row().classes("items-center justify-between w-full"):
                    with ui.column().classes("col"):
                        ui.label(acc.name).classes("text-weight-bold text-body1")
                        ui.label(f"ID: {acc.sec_user_id}").classes("text-caption text-grey-6")
                        tags_str = ", ".join(acc.tags) if acc.tags else "无标签"
                        ui.label(tags_str).classes("text-caption text-grey-5")
                    
                    with ui.row().classes("col-auto items-center"):
                        # 启/停按钮
                        status_label = "✅ 已启用" if acc.enabled else "⏸️ 已暂停"
                        ui.button(
                            status_label,
                            on_click=lambda a=acc: toggle_acc(a),
                        ).props(f"flat {'color=primary' if acc.enabled else 'color=grey'} dense").classes("q-mr-xs")
                        
                        # 删除按钮
                        ui.button(
                            "删除",
                            on_click=lambda a=acc: delete_acc(a),
                        ).props("flat color=negative dense").classes("q-mr-xs")
                        
                        # 编辑按钮
                        ui.button("编辑").props("flat color=info dense")
    
    def toggle_acc(account: MonitorAccount):
        on_toggle_account(account.sec_user_id, not account.enabled)
        account_list_ui.refresh()
    
    def delete_acc(account: MonitorAccount):
        ui.notify(f"确认删除 [{account.name}]？", type="info")
        on_delete_account(account.sec_user_id)
        account_list_ui.refresh()
    
    account_list_ui()
