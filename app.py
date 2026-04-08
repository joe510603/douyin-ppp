"""
🎯 Douyin PPP — 抖音评论抓取工具
Web UI 入口 (NiceGUI + FastAPI)

用法:
    python3 app.py              # 启动 Web UI
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from nicegui import ui, app

from src.config import get_config, reload_config, save_config, Settings
from src.utils.logger import setup_logging, get_logger
from src.models.comment import MonitorAccount, LiveComment, MessageType
from src.storage.db_storage import DBStorage, init_database, close_database, get_db
from src.processor.comment_processor import CommentProcessor
from src.collector.live_collector import LiveCollector
from src.detector.live_detector import LiveDetector, get_detector, close_detector
from src.utils.cookie_manager import get_cookie_status
from src.web.dashboard import create_dashboard
from src.web.monitor_page import create_monitor_page
from src.web.danmaku_page import create_danmaku_page, push_danmaku
from src.web.data_page import create_data_page
from src.web.config_page import create_config_page
from src.web.log_page import create_log_page

log = get_logger("app")

# ============================================================
# 全局状态
# ============================================================

class AppState:
    """应用全局运行状态"""
    
    def __init__(self):
        # 监控账号运行时状态: {sec_user_id: {name, status, room_id, error_message, ...}}
        self.monitor_states: dict[str, dict] = {}
        
        # 各直播间的采集器实例: {room_id: LiveCollector}
        self.collectors: dict[str, LiveCollector] = {}
        
        # 全局统计
        self.stats = {
            "start_time": datetime.now(),
            "today_total": 0,
            "total_comments": 0,
            "online_rooms": 0,
        }
        
        # 组件
        self.db: DBStorage | None = None
        self.processor: CommentProcessor | None = None
        self.detector: LiveDetector | None = None
        
        # 调度任务
        self._scheduler_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None


state = AppState()


# ============================================================
# 核心业务逻辑
# ============================================================

async def initialize():
    """初始化所有组件"""
    config = get_config()
    
    setup_logging(
        level=config.logging.level,
        file_path=config.logging.file_path if config.logging.file_path else None,
        rotation=config.logging.rotation,
        retention=config.logging.retention,
    )
    
    log.info("=" * 50)
    log.info("🚀 Douyin PPP 启动中...")
    log.info(f"   版本: 1.0.0")
    log.info(f"   配置: {config.app.name}")
    log.info(f"   端口: {config.app.port}")

    # 启动时检查 Cookie 状态
    cookie = config.douyin.cookie
    cookie_status = get_cookie_status(cookie)
    if cookie_status == "valid":
        log.info("🔐 Cookie 状态：✅ 有效")
    elif cookie_status == "expiring_soon":
        log.warning("🔐 Cookie 状态：⚠️ 即将在 24h 内过期，请及时更新")
    else:
        log.warning("🔐 Cookie 状态：❌ 已失效，检测结果可能不准确，请到 配置管理 更新 Cookie")

    log.info("=" * 50)
    
    # 初始化数据库
    await init_database()
    state.db = get_db()
    
    # 初始化处理器
    state.processor = CommentProcessor()
    
    # 初始化检测器
    state.detector = LiveDetector()
    
    # 加载监控账号到运行时状态
    _sync_monitor_states()
    
    log.info("✅ 初始化完成")


def _sync_monitor_states():
    """从配置文件同步监控账号到运行时状态"""
    config = get_config()
    for acc in config.monitors:
        sid = acc.sec_user_id
        if sid not in state.monitor_states:
            state.monitor_states[sid] = {
                "name": acc.name,
                "sec_user_id": acc.sec_user_id,
                "room_id": acc.room_id or "",
                "enabled": acc.enabled,
                "status": "idle",
                "error_message": None,
                "last_check_time": None,
                "collector": None,
            }


async def on_new_comment(comment: LiveComment):
    """新评论回调（采集器触发）"""
    # 处理（清洗/去重）
    processed = state.processor.process(comment) if state.processor else comment
    
    if processed is not None:
        # 写入缓冲区
        if state.db:
            state.db.buffer_comment(processed)
        
        # 推送到弹幕大屏
        push_danmaku(processed.model_dump())
        
        # 更新统计
        state.stats["total_comments"] += 1
        state.stats["today_total"] += 1


async def on_connection_status(room_id: str, connected: bool, msg: str):
    """连接状态变化回调"""
    sid = _find_sec_user_id_by_room(room_id)
    if sid and sid in state.monitor_states:
        state.monitor_states[sid]["status"] = "live" if connected else ("error" if "错误" in msg else "idle")
        state.monitor_states[sid]["error_message"] = msg if not connected else None
    
    online_count = sum(1 for v in state.monitor_states.values() if v.get("status") == "live")
    state.stats["online_rooms"] = online_count


# ============================================================
# 调度循环
# ============================================================

async def run_single_detection():
    """执行一次直播检测"""
    config = get_config()
    detector = state.detector
    if not detector:
        return
    
    accounts = [MonitorAccount(**acc.model_dump()) for acc in config.monitors 
                if acc.enabled]
    
    if not accounts:
        return
    
    results = await detector.batch_detect(accounts)
    
    for acc in accounts:
        sid = acc.sec_user_id
        info = results.get(sid)
        
        if sid not in state.monitor_states:
            _sync_monitor_states()
        
        st = state.monitor_states.get(sid)
        if st:
            st["last_check_time"] = datetime.now().isoformat()
        
        if info and info.status:
            room_id = info.room_id
            if not room_id:
                continue
            
            # 更新 room_id 到状态和配置
            if st:
                st["room_id"] = room_id
            
            # 如果还没在采集，启动采集
            if room_id not in state.collectors:
                await start_collection(room_id, sid, info.anchor_nickname)
            
        elif info and not info.status:
            # 未开播，如果之前有采集则停止
            room_id = (st.get("room_id", "") if st else "")
            if room_id in state.collectors:
                await stop_collection(room_id)


async def detection_loop():
    """开播检测主循环"""
    config = get_config()
    detector = state.detector
    if not detector:
        return
    
    log.info(f"开播检测已启动 (间隔: {config.live_detection.interval}s)")
    
    while True:
        try:
            await run_single_detection()
            config = get_config()
            await asyncio.sleep(config.live_detection.interval)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"检测循环异常: {e}", exc_info=True)
            await asyncio.sleep(30)


async def flush_loop():
    """数据库缓冲区刷写循环"""
    config = get_config()
    while True:
        try:
            if state.db:
                count = await state.db.flush_buffer()
                if count > 0:
                    log.debug(f"刷写 {count} 条数据到数据库")
            await asyncio.sleep(config.storage.flush_interval)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"刷写循环异常: {e}")
            await asyncio.sleep(5)


async def start_collection(room_id: str, sec_id: str, anchor_name: str):
    """为指定直播间启动采集"""
    if room_id in state.collectors:
        return

    config = get_config()

    # 获取账号特定的 WebSocket URL 和监控名称
    ws_url = None
    monitor_name = ""
    for acc in config.monitors:
        if acc.sec_user_id == sec_id:
            ws_url = acc.websocket_url if acc.websocket_url else None
            monitor_name = acc.name
            break

    # 状态变化回调
    def on_status_change(connected: bool, msg: str):
        if connected:
            state.monitor_states[sec_id]["status"] = "live"
            state.monitor_states[sec_id]["error_message"] = None
        else:
            state.monitor_states[sec_id]["status"] = "error"
            state.monitor_states[sec_id]["error_message"] = msg
        # 更新在线直播间数
        state.stats["online_rooms"] = sum(1 for v in state.monitor_states.values() if v.get("status") == "live")
    
    collector = LiveCollector(
        on_comment=on_new_comment,
        on_status_change=lambda c, m: on_status_change(c, m),
        monitor_name=monitor_name
    )

    success = await collector.connect(room_id, config.douyin.cookie, ws_url_override=ws_url)
    if success:
        state.collectors[room_id] = collector
        log.info(f"✅ 启动采集: 直播间 {room_id} ({anchor_name})")


async def stop_collection(room_id: str):
    """停止指定直播间的采集"""
    collector = state.collectors.pop(room_id, None)
    if collector:
        await collector.disconnect()
        log.info(f"⏹️ 停止采集: 直播间 {room_id}")


def _find_sec_user_id_by_room(room_id: str) -> str | None:
    """通过 room_id 找到对应的 sec_user_id"""
    for sid, st in state.monitor_states.items():
        if st.get("room_id") == room_id:
            return sid
    return None


# ============================================================
# Web UI 页面路由
# ============================================================

@ui.page("/")
def index_page():
    """首页 / 仪表盘"""
    with ui.header(elevated=True).classes("bg-white"):
        with ui.row().classes("items-center w-full q-px-lg"):
            ui.label("🎯 Douyin PPP").classes("text-h6 text-primary q-mr-md")
            ui.space()
            # 导航菜单
            ui.button("🏠 首页", on_click=lambda: ui.navigate.to("/")).props("flat dense")
            ui.button("👥 监控管理", on_click=lambda: ui.navigate.to("/monitor")).props("flat dense")
            ui.button("📡 实时弹幕", on_click=lambda: ui.navigate.to("/danmaku")).props("flat dense")
            ui.button("📊 数据导出", on_click=lambda: ui.navigate.to("/data")).props("flat dense")
            ui.button("⚙️ 配置管理", on_click=lambda: ui.navigate.to("/config")).props("flat dense")
            ui.button("📝 日志", on_click=lambda: ui.navigate.to("/logs")).props("flat dense")
    
    create_dashboard(state.monitor_states, state.stats)


@ui.page("/monitor")
def monitor_page():
    """监控管理页面"""
    with ui.header(elevated=True).classes("bg-white"):
        with ui.row().classes("items-center w-full q-px-lg"):
            ui.button("🏠 首页", on_click=lambda: ui.navigate.to("/")).props("flat").classes("q-mr-sm")
            ui.label("监控管理").classes("text-h6 text-primary q-mr-auto")
    
    def on_add(name, sec_id):
        config = get_config()
        new_acc = MonitorItem(name=name, sec_user_id=sec_id, enabled=True)
        config.monitors.append(new_acc)
        save_config(config)
        reload_config()
        _sync_monitor_states()
    
    def on_delete(sec_id):
        config = get_config()
        config.monitors = [a for a in config.monitors if a.sec_user_id != sec_id]
        save_config(config)
        reload_config()
        state.monitor_states.pop(sec_id, None)
    
    def on_toggle(sec_id, enabled):
        config = get_config()
        for acc in config.monitors:
            if acc.sec_user_id == sec_id:
                acc.enabled = enabled
                break
        save_config(config)
        reload_config()
        if sec_id in state.monitor_states:
            state.monitor_states[sec_id]["enabled"] = enabled
    
    from src.config import MonitorItem as MonitorItem
    
    async def on_manual_detect():
        """手动触发一次检测"""
        ui.notify("开始检测直播状态...", type="info")
        try:
            await run_single_detection()
            ui.notify("检测完成", type="positive")
        except Exception as e:
            log.error(f"手动检测失败: {e}")
            ui.notify(f"检测失败: {e}", type="negative")
    
    create_monitor_page(on_add, on_delete, on_toggle, on_manual_detect)


@ui.page("/danmaku")
def danmaku_page():
    """实时弹幕大屏"""
    with ui.header(elevated=True).classes("bg-black"):
        with ui.row().classes("items-center w-full q-px-lg"):
            ui.button("🏠 首页", on_click=lambda: ui.navigate.to("/")).props("flat color=white").classes("q-mr-sm")
            ui.label("📡 实时弹幕大屏").classes("text-h6 text-white q-mr-auto")
    
    rooms = list(set(st.get("room_id", "") for st in state.monitor_states.values() if st.get("room_id")))
    create_danmaku_page(rooms)


@ui.page("/data")
def data_page():
    """数据浏览与导出"""
    with ui.header(elevated=True).classes("bg-white"):
        with ui.row().classes("items-center w-full q-px-lg"):
            ui.button("🏠 首页", on_click=lambda: ui.navigate.to("/")).props("flat").classes("q-mr-sm")
            ui.label("📊 数据浏览与导出").classes("text-h6 text-primary q-mr-auto")
    
    def on_export(filepath):
        pass  # 可扩展：通知用户下载
    
    create_data_page(on_export)


@ui.page("/config")
def config_page():
    """配置管理页面"""
    with ui.header(elevated=True).classes("bg-white"):
        with ui.row().classes("items-center w-full q-px-lg"):
            ui.button("🏠 首页", on_click=lambda: ui.navigate.to("/")).props("flat").classes("q-mr-sm")
            ui.label("⚙️ 配置管理").classes("text-h6 text-primary q-mr-auto")
    
    create_config_page()


@ui.page("/logs")
def logs_page():
    """日志查看页面"""
    with ui.header(elevated=True).classes("bg-white"):
        with ui.row().classes("items-center w-full q-px-lg"):
            ui.button("🏠 首页", on_click=lambda: ui.navigate.to("/")).props("flat").classes("q-mr-sm")
            ui.label("📝 日志查看").classes("text-h6 text-primary q-mr-auto")
    
    create_log_page()


# ============================================================
# 主入口
# ============================================================

def main():
    """应用主函数"""
    config = get_config()
    
    # 注册启动/关闭回调
    app.on_startup(on_startup)
    app.on_shutdown(on_shutdown)

    # NiceGUI 初始化
    ui.run(
        title="Douyin PPP - 抖音评论抓取工具",
        port=config.app.port,
        host=config.app.host,
        reload=False,
        show=True,
        favicon="🎯",
        dark=None,  # 自动检测系统主题
        viewport="width=device-width, initial-scale=1",
        storage_secret="douyin-ppp-secret-2024",
    )


async def on_startup():
    """NiceGUI 启动后回调 — 初始化业务组件并启动调度"""
    await initialize()
    
    # 启动调度任务
    loop = asyncio.get_event_loop()
    state._scheduler_task = loop.create_task(detection_loop(), name="detection-loop")
    state._flush_task = loop.create_task(flush_loop(), name="flush-loop")
    
    log.info("调度任务已启动 ✅")


async def on_shutdown():
    """NiceGUI 关闭前回调 — 清理资源"""
    log.info("正在关闭...")
    
    # 停止调度任务
    if state._scheduler_task and not state._scheduler_task.done():
        state._scheduler_task.cancel()
        try:
            await state._scheduler_task
        except asyncio.CancelledError:
            pass
    
    if state._flush_task and not state._flush_task.done():
        state._flush_task.cancel()
    
    # 断开所有采集器
    for room_id in list(state.collectors.keys()):
        await stop_collection(room_id)
    
    # 刷写剩余数据
    if state.db:
        await state.db.flush_buffer()
    
    # 关闭资源
    await close_database()
    await close_detector()
    
    log.info("已安全关闭 👋")


if __name__ == "__main__":
    main()
