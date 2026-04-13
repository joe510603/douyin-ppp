"""实时弹幕大屏 — 增量刷新的实时弹幕流展示"""

from __future__ import annotations

import json
from datetime import datetime
from collections import deque
from typing import Optional

from nicegui import ui
from ..config import get_config

# 全局弹幕缓冲区（最多保留最近 500 条）
_danmaku_buffer: deque[dict] = deque(maxlen=500)

# 当前选中的直播间
_current_room: Optional[str] = None

# 上次渲染的弹幕 ID 集合（用于增量判断）
_last_rendered_ids: set[str] = set()

# 最大显示条数
_MAX_DISPLAY = 100


def push_danmaku(data: dict):
    """外部调用：推送新弹幕到缓冲区"""
    data["_display_time"] = datetime.now().strftime("%H:%M:%S")
    # 生成唯一 ID（用于增量更新判断）
    if "_id" not in data:
        data["_id"] = f"{data.get('room_id','')}_{data.get('_display_time','')}_{id(data)}"
    _danmaku_buffer.appendleft(data)


def _build_danmaku_html(item: dict) -> str:
    """构建单条弹幕的 HTML"""
    msg_type = item.get("message_type", "")
    content = item.get("content", "")
    nickname = item.get("user_nickname", "") or "匿名"
    time_str = item.get("_display_time", "")
    monitor_name = item.get("monitor_name", "")
    item_id = item.get("_id", "")

    if "Gift" in str(msg_type):
        item_class = "danmaku-item danmaku-item-gift"
        prefix = "🎁"
    elif "Member" in str(msg_type):
        item_class = "danmaku-item danmaku-item-member"
        prefix = "👋"
    else:
        item_class = "danmaku-item danmaku-item-chat"
        prefix = ""

    monitor_tag = ""
    if monitor_name:
        monitor_tag = f'<span style="color:#6366F1;font-size:11px;margin-left:auto;background:rgba(99,102,241,0.12);padding:1px 8px;border-radius:10px;white-space:nowrap;">{monitor_name[:10]}</span>'

    # HTML 转义
    safe_content = content[:100].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_nickname = nickname[:16].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return (
        f'<div class="w-full {item_class}" data-id="{item_id}" style="display:flex;align-items:center;">'
        f'<span style="color:#6B7280;font-size:12px;min-width:55px;margin-right:8px;">{time_str}</span>'
        f'<span style="margin-right:2px;">{prefix}</span>'
        f'<span style="color:#818CF8;font-size:13px;font-weight:500;min-width:80px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-right:8px;">{safe_nickname}</span>'
        f'<span style="color:#E5E7EB;font-size:13px;flex:1;word-break:break-word;">{safe_content}</span>'
        f'{monitor_tag}'
        f'</div>'
    )


def _get_filtered_items(selected_room: str, selected_type: str) -> list[dict]:
    """根据筛选条件获取弹幕列表"""
    items = list(_danmaku_buffer)

    if selected_room != "全部":
        items = [d for d in items if d.get("monitor_name") == selected_room or d.get("room_id") == selected_room]

    if selected_type != "全部":
        type_map = {"评论": ["Chat", "Comment"], "礼物": ["Gift"], "进场": ["Member"]}
        keywords = type_map.get(selected_type, [])
        items = [d for d in items if any(kw in str(d.get("message_type", "")) for kw in keywords)]

    return items[:_MAX_DISPLAY]


def create_danmaku_page(available_rooms: list[str]):
    """
    创建实时弹幕大屏页面。

    Args:
        available_rooms: 可用的直播间 room_id 列表
    """
    global _current_room

    config = get_config()
    # 构建监控名称映射 {room_id: monitor_name}
    monitor_name_map = {}
    for acc in config.monitors:
        if acc.room_id:
            monitor_name_map[acc.room_id] = acc.name

    # 可选的监控账号列表
    monitor_names = list(set(acc.name for acc in config.monitors))

    with ui.element("div").classes("danmaku-container w-full page-enter").style("height: calc(100vh - 72px); display: flex; flex-direction: column;"):
        # 顶部控制栏
        with ui.row().classes("danmaku-header w-full items-center justify-between q-pa-sm"):
            ui.label("实时弹幕流").style("color: #F9FAFB; font-size: 16px; font-weight: 600;")

            with ui.row().classes("items-center gap-sm"):
                # 消息类型筛选
                type_select = ui.select(
                    options=["全部", "评论", "礼物", "进场"],
                    value="评论",
                    label="消息类型",
                ).props("dark dense color=white standout").classes("q-mr-sm")

                # 账号筛选
                room_select = ui.select(
                    options=["全部"] + monitor_names + available_rooms,
                    value="全部",
                    label="筛选账号",
                ).props("dark dense color=white standout").classes("q-mr-md")

        # 弹幕显示区域 — 使用 ui.html 实现增量 DOM 操作
        danmaku_html = ui.html('<div id="danmaku-list" style="flex:1;overflow-y:auto;padding:12px;background:#0a0a0f;"></div>')

        # 增量更新逻辑
        _state = {"last_count": 0, "last_ids": set()}

        def _do_incremental_update():
            """增量更新弹幕列表：只追加新增的弹幕，移除超出的旧弹幕"""
            selected_room = room_select.value or "全部"
            selected_type = type_select.value or "全部"
            items = _get_filtered_items(selected_room, selected_type)

            if not items:
                return

            # 计算当前应显示的 ID 集合
            current_ids = {item.get("_id", "") for item in items}
            new_ids = current_ids - _state["last_ids"]
            removed_ids = _state["last_ids"] - current_ids

            # 构建新增弹幕 HTML（按时间顺序，新的在前）
            new_items = [item for item in items if item.get("_id", "") in new_ids]
            new_html_parts = [_build_danmaku_html(item) for item in new_items]

            # 构建 JavaScript 执行增量更新
            js_code = """
            (function() {
                const container = document.getElementById('danmaku-list');
                if (!container) return;

                // 移除已不在筛选结果中的弹幕
                const removedIds = %s;
                const existingEls = container.querySelectorAll('[data-id]');
                existingEls.forEach(el => {
                    if (removedIds.includes(el.getAttribute('data-id'))) {
                        el.remove();
                    }
                });

                // 移除超出的旧弹幕（保留最多 %d 条）
                const allItems = container.querySelectorAll('[data-id]');
                if (allItems.length > %d) {
                    for (let i = %d; i < allItems.length; i++) {
                        allItems[i].remove();
                    }
                }

                // 插入新弹幕到顶部
                const newHtml = %s;
                if (newHtml.length > 0) {
                    const wrapper = document.createElement('div');
                    wrapper.innerHTML = newHtml.join('');
                    const fragment = document.createDocumentFragment();
                    while (wrapper.firstChild) {
                        fragment.appendChild(wrapper.firstChild);
                    }
                    if (container.firstChild) {
                        container.insertBefore(fragment, container.firstChild);
                    } else {
                        container.appendChild(fragment);
                    }
                }

                // 自动滚动到顶部
                container.scrollTop = 0;
            })();
            """ % (
                json.dumps(list(removed_ids)),
                _MAX_DISPLAY,
                _MAX_DISPLAY,
                _MAX_DISPLAY,
                json.dumps(new_html_parts),
            )

            ui.run_javascript(js_code)
            _state["last_ids"] = current_ids
            _state["last_count"] = len(items)

        # 筛选变化时全量刷新
        def _on_filter_change():
            """筛选条件变化时重新渲染全部弹幕"""
            selected_room = room_select.value or "全部"
            selected_type = type_select.value or "全部"
            items = _get_filtered_items(selected_room, selected_type)

            if not items:
                all_html = '<div style="color:#6B7280;text-align:center;padding:48px 0;width:100%;">等待弹幕数据...</div>'
            else:
                all_html = "".join(_build_danmaku_html(item) for item in items)

            danmaku_html.set_content(f'<div id="danmaku-list" style="flex:1;overflow-y:auto;padding:12px;background:#0a0a0f;">{all_html}</div>')
            _state["last_ids"] = {item.get("_id", "") for item in items}
            _state["last_count"] = len(items)

        type_select.on("update:model-value", _on_filter_change)
        room_select.on("update:model-value", _on_filter_change)

        # 初始化渲染
        _on_filter_change()

        # 定时增量更新
        ui.timer(1.0, _do_incremental_update)
