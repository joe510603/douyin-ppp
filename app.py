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
from src.web.video_scrape_page import create_video_scrape_page
from src.web.wordcloud_page import create_wordcloud_page
from src.web.layout import create_app_header
from src.web.service_page import create_service_page
from src.task import TaskManager, get_task_manager, close_task_manager
from src.utils.signer_manager import ensure_signer_running

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

        # collector room_id → sec_user_id 列表的反向映射
        # 解决 detector 更新 room_id 后旧 collector 无法通过 room_id 找到 sec_id 的问题
        self._collector_room_to_sec_ids: dict[str, list[str]] = {}
        
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
        self.task_manager: TaskManager | None = None
        
        # 调度任务
        self._scheduler_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None


state = AppState()


# ============================================================
# 核心业务逻辑
# ============================================================

def _get_effective_cookie() -> str:
    """获取包含 ttwid 的有效 cookie"""
    config = get_config()
    douyin_cookie = config.douyin.cookie or ""
    video_cookie = config.douyin_video.cookie or ""
    if "ttwid" in douyin_cookie:
        return douyin_cookie
    if "ttwid" in video_cookie:
        return video_cookie
    return douyin_cookie or video_cookie

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
    log.info(f"   版本: 2.0.1")
    log.info(f"   配置: {config.app.name}")
    log.info(f"   端口: {config.app.port}")
    
    # 启动时检查并启动签名服务
    log.info("🔧 检查签名服务状态...")
    signer_ok = await ensure_signer_running()
    if not signer_ok:
        log.warning("⚠️ 签名服务启动失败，将使用 Playwright 回退方案（性能较低）")

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
    """从配置文件同步监控账号到运行时状态（新增+更新）"""
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
                "viewer_count": 0,  # 初始化在线人数
            }
        else:
            # 已存在的账号：同步 name 和 enabled（改名/启停后实时生效）
            state.monitor_states[sid]["name"] = acc.name
            state.monitor_states[sid]["enabled"] = acc.enabled


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
    # 通过反向映射通知所有相关的 sec_id（解决 detector 更新 room_id 后旧 collector 找不到的问题）
    _notify_all_sec_ids_for_room(room_id, connected, msg)
    # 同时用原来的方式通知（保持兼容性）
    sid = _find_sec_user_id_by_room(room_id)
    if sid and sid in state.monitor_states:
        error_keywords = ["错误", "失败", "异常", "超时", "鉴权"]
        is_error = any(kw in msg for kw in error_keywords)
        state.monitor_states[sid]["status"] = "live" if connected else ("error" if is_error else "idle")
        state.monitor_states[sid]["error_message"] = msg if not connected and is_error else None
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
            # 区分正常下播和异常：消息中包含"错误"/"失败"/"异常"等才标记为error
            error_keywords = ["错误", "失败", "异常", "超时", "鉴权"]
            is_error = any(kw in msg for kw in error_keywords)
            new_status = "error" if is_error else "idle"
            # 更新当前 sec_id
            state.monitor_states[sec_id]["status"] = new_status
            state.monitor_states[sec_id]["error_message"] = msg if is_error else None
            # 也更新所有共用同一 collector room_id 的其他 sec_id
            # 注意：collector.disconnect() 会清理 _collector_room_to_sec_ids，
            # 所以这里用传入的 room_id 查找（如果还有映射的话）
            collector_room = None
            for rid, sids in state._collector_room_to_sec_ids.items():
                if sec_id in sids:
                    collector_room = rid
                    break
            if collector_room:
                for other_sid in state._collector_room_to_sec_ids.get(collector_room, []):
                    if other_sid != sec_id and other_sid in state.monitor_states:
                        state.monitor_states[other_sid]["status"] = new_status
                        state.monitor_states[other_sid]["error_message"] = msg if is_error else None
                        state.monitor_states[other_sid]["viewer_count"] = 0
        # 更新在线直播间数
        state.stats["online_rooms"] = sum(1 for v in state.monitor_states.values() if v.get("status") == "live")
    
    # 在线人数更新回调
    def on_viewer_count_update(count: int, room_id: str = None):
        log.info(f"📊 在线人数回调触发: {monitor_name} -> {count}")
        # 【关键修复】只取更大的值，避免 RoomStatsMessage 的小数值覆盖 RoomUserSeqMessage 的真实人数
        current = state.monitor_states[sec_id].get("viewer_count", 0)
        if count >= current:
            state.monitor_states[sec_id]["viewer_count"] = count
            log.info(f"✅ 已更新 state.monitor_states[{sec_id}]['viewer_count'] = {count}")
            # 同时更新共用同一 collector room_id 的其他账号
            if room_id and room_id in state._collector_room_to_sec_ids:
                for other_sid in state._collector_room_to_sec_ids[room_id]:
                    if other_sid != sec_id and other_sid in state.monitor_states:
                        state.monitor_states[other_sid]["viewer_count"] = count
        else:
            log.debug(f"⏭️ 忽略较小的人数 {count}（当前 {current}）")
    
    collector = LiveCollector(
        on_comment=on_new_comment,
        on_status_change=lambda c, m: on_status_change(c, m),
        on_viewer_count_update=lambda c, rid=None: on_viewer_count_update(c, rid),
        on_stop_requested=lambda: stop_collection(room_id),
        monitor_name=monitor_name,
        anchor_name=anchor_name,
        anchor_id=sec_id,
    )

    success = await collector.connect(room_id, _get_effective_cookie(), ws_url_override=ws_url)
    if success:
        state.collectors[room_id] = collector
        # 注册 collector room_id → sec_id 的反向映射
        if room_id not in state._collector_room_to_sec_ids:
            state._collector_room_to_sec_ids[room_id] = []
        if sec_id not in state._collector_room_to_sec_ids[room_id]:
            state._collector_room_to_sec_ids[room_id].append(sec_id)
        # 【修复】同步 room_id 到 monitor_states，否则首页表格不显示房间号
        if sec_id in state.monitor_states:
            state.monitor_states[sec_id]["room_id"] = room_id
            state.monitor_states[sec_id]["status"] = "live"
            state.monitor_states[sec_id]["error_message"] = None
        log.info(f"✅ 启动采集: 直播间 {room_id} ({anchor_name})")


async def stop_collection(room_id: str):
    """停止指定直播间的采集"""
    collector = state.collectors.pop(room_id, None)
    # 清理反向映射
    if room_id in state._collector_room_to_sec_ids:
        del state._collector_room_to_sec_ids[room_id]
    if collector:
        await collector.disconnect()
        log.info(f"⏹️ 停止采集: 直播间 {room_id}")


def _find_sec_user_id_by_room(room_id: str) -> str | None:
    """通过 room_id 找到对应的 sec_user_id（优先从 collector 反向映射查找）"""
    # 优先从 collector 反向映射查找（解决 detector 更新 room_id 后旧 collector 找不到 sec_id 的问题）
    if room_id in state._collector_room_to_sec_ids:
        sids = state._collector_room_to_sec_ids[room_id]
        if sids:
            return sids[0]
    # 回退：从 monitor_states 查找
    for sid, st in state.monitor_states.items():
        if st.get("room_id") == room_id:
            return sid
    return None


def _notify_all_sec_ids_for_room(room_id: str, connected: bool, msg: str):
    """通知所有使用该 collector room_id 的 sec_id"""
    sids = state._collector_room_to_sec_ids.get(room_id, [])
    for sid in sids:
        if sid in state.monitor_states:
            error_keywords = ["错误", "失败", "异常", "超时", "鉴权"]
            is_error = any(kw in msg for kw in error_keywords)
            state.monitor_states[sid]["status"] = "live" if connected else ("error" if is_error else "idle")
            state.monitor_states[sid]["error_message"] = msg if not connected and is_error else None
    # 更新在线直播间数
    online_count = sum(1 for v in state.monitor_states.values() if v.get("status") == "live")
    state.stats["online_rooms"] = online_count


# ============================================================
# Web UI 页面路由
# ============================================================

@ui.page("/")
def index_page():
    """首页 / 仪表盘"""
    create_app_header(active_page="dashboard")
    create_dashboard(state.monitor_states, state.stats)


@ui.page("/monitor")
def monitor_page():
    """监控管理页面"""
    create_app_header(active_page="monitor")
    
    from src.config import MonitorItem
    
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
    
    def on_update(sec_id, new_name, new_tags):
        """编辑账号后同步运行时状态"""
        if sec_id in state.monitor_states:
            state.monitor_states[sec_id]["name"] = new_name
    
    async def on_manual_detect():
        """手动触发一次检测"""
        ui.notify("开始检测直播状态...", type="info")
        try:
            await run_single_detection()
            ui.notify("检测完成", type="positive")
        except Exception as e:
            log.error(f"手动检测失败: {e}")
            ui.notify(f"检测失败: {e}", type="negative")
    
    create_monitor_page(on_add, on_delete, on_toggle, on_manual_detect, on_update)


@ui.page("/danmaku")
def danmaku_page():
    """实时弹幕大屏"""
    create_app_header(active_page="danmaku", immersive=True)
    
    rooms = list(set(st.get("room_id", "") for st in state.monitor_states.values() if st.get("room_id")))
    create_danmaku_page(rooms)


@ui.page("/video_scrape")
def video_scrape_page():
    """视频评论抓取页面"""
    create_app_header(active_page="video_scrape")
    create_video_scrape_page(state.task_manager)


@ui.page("/wordcloud")
def wordcloud_page():
    """词云分析页面"""
    create_app_header(active_page="wordcloud")
    create_wordcloud_page()


@ui.page("/data")
def data_page():
    """数据浏览与导出"""
    create_app_header(active_page="data")
    
    def on_export(filepath):
        pass  # 可扩展：通知用户下载
    
    create_data_page(on_export)


@ui.page("/config")
def config_page():
    """配置管理页面"""
    create_app_header(active_page="config")
    
    create_config_page()


@ui.page("/logs")
def logs_page():
    """日志查看页面"""
    create_app_header(active_page="logs")
    create_log_page()


@ui.page("/service")
def service_page():
    """服务管理页面"""
    create_app_header(active_page="service")
    create_service_page()


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
        dark=False,  # 默认亮色，由主题切换按钮控制
        viewport="width=device-width, initial-scale=1",
        storage_secret="douyin-ppp-secret-2024",
    )


async def on_startup():
    """NiceGUI 启动后回调 — 初始化业务组件并启动调度"""
    await initialize()

    # 初始化 TaskManager
    state.task_manager = get_task_manager()

    # 启动调度任务
    loop = asyncio.get_event_loop()
    state._flush_task = loop.create_task(flush_loop(), name="flush-loop")

    # 【核心修复】检测循环在独立线程中运行
    # 避免 Playwright 的 sync_playwright() 阻塞 uvicorn 事件循环导致 NiceGUI WebSocket 断连
    import threading
    import queue as _queue

    # 检测线程 → 主线程的检测结果队列
    _detection_result_queue: _queue.Queue = _queue.Queue()
    state._detection_result_queue = _detection_result_queue

    def _run_detection_thread(q: _queue.Queue):
        """独立线程：运行检测循环，结果放入队列"""
        import asyncio
        import sys
        sys.path.insert(0, str(Path(__file__).parent.resolve()))

        from src.detector.live_detector import LiveDetector
        from src.config import get_config
        from src.models.comment import MonitorAccount

        async def _worker():
            config = get_config()
            # 【核心修复】在线程内创建独立的 LiveDetector，不共享主进程的 httpx.AsyncClient
            # get_detector() 返回的是主进程单例，跨 event loop 访问会阻塞主事件循环
            detector = LiveDetector()

            async def _detect_once():
                accounts = [MonitorAccount(**acc.model_dump()) for acc in config.monitors if acc.enabled]
                if not accounts:
                    return {}
                return await detector.batch_detect(accounts)

            while True:
                try:
                    results = await _detect_once()
                    if results:
                        q.put(("detection_results", results))
                    config = get_config()
                    await asyncio.sleep(config.live_detection.interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error(f"[Detection-Thread] 异常: {e}")
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        break

        asyncio.run(_worker())

    t = threading.Thread(target=_run_detection_thread, args=(_detection_result_queue,), daemon=True)
    t.start()
    state._detection_thread = t

    # 主线程监听检测结果，驱动采集
    async def _listen_detection_results():
        q = state._detection_result_queue
        while True:
            try:
                msg_type, data = q.get_nowait()
                if msg_type == "detection_results":
                        # 【诊断日志】
                    live_sids = [sid for sid, info in data.items() if info.status and info.room_id]
                    log.debug(f"[Detection-Listener] 检测结果: {len(live_sids)}个在线")

                    for sid, info in data.items():
                        if info.status and info.room_id:
                            room_id = info.room_id
                            web_rid = getattr(info, 'web_rid', '') or ''
                            if room_id not in state.collectors:
                                await start_collection(room_id, sid, info.anchor_nickname)
                            else:
                                if sid in state.monitor_states:
                                    state.monitor_states[sid]["room_id"] = room_id
                                    state.monitor_states[sid]["web_rid"] = web_rid
                                    state.monitor_states[sid]["status"] = "live"
                                    state.monitor_states[sid]["error_message"] = None
                        else:
                            # 【修复】账号不在线时也要更新状态！否则 monitor_states 保留旧状态，UI 一直显示"直播中"
                            # 但如果 collector 仍在运行（采集器证明直播间实际还在播），保留 room_id 和 live 状态
                            if sid in state.monitor_states:
                                old_room_id = state.monitor_states[sid].get("room_id", "")
                                has_collector = old_room_id and old_room_id in state.collectors
                                if has_collector:
                                    # collector 还在跑，说明直播间实际还在播，保留 room_id 和 live 状态
                                    state.monitor_states[sid]["viewer_count"] = 0
                                else:
                                    # 确实下播了，清空所有状态
                                    state.monitor_states[sid]["status"] = "idle"
                                    state.monitor_states[sid]["room_id"] = ""
                                    state.monitor_states[sid]["viewer_count"] = 0
                                    state.monitor_states[sid]["error_message"] = None
                    # 检查下播：停止没有检测到的直播间
                    for rid in list(state.collectors.keys()):
                        collector = state.collectors[rid]
                        # 如果 collector 的任务已完成（可能中途失败退出），从状态中移除
                        if collector._task is not None and collector._task.done():
                            log.warning(f"[Detection-Listener] 采集任务 {rid} 已结束但未清理，主动移除")
                            del state.collectors[rid]
                            if rid in state._collector_room_to_sec_ids:
                                for sid in state._collector_room_to_sec_ids[rid]:
                                    if sid in state.monitor_states:
                                        state.monitor_states[sid]["status"] = "idle"
                                        state.monitor_states[sid]["viewer_count"] = 0
                                        state.monitor_states[sid]["error_message"] = "采集中断"
                            del state._collector_room_to_sec_ids[rid]
                            continue
                        sids = state._collector_room_to_sec_ids.get(rid, [])
                        still_live = any(
                            data.get(sid) and data[sid].status and data[sid].room_id == rid
                            for sid in sids
                        )
                        if not still_live:
                            log.info(f"[Detection-Listener] 检测不到直播间 {rid}，停止采集")
                            await stop_collection(rid)
            except _queue.Empty:
                pass
            except Exception as e:
                log.error(f"[Detection-Listener] 异常: {e}")
            # yield 让出事件循环，让 NiceGUI 处理 WebSocket
            await asyncio.sleep(0.5)

    state._scheduler_task = loop.create_task(_listen_detection_results(), name="detection-listener")
    log.info(f"调度任务已启动（检测线程 TID={t.ident}，独立线程）✅")


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

    # 停止检测线程（daemon 线程，主进程退出时自动终止）
    if hasattr(state, '_detection_thread') and state._detection_thread:
        log.info("检测线程已关闭")

    # 断开所有采集器
    for room_id in list(state.collectors.keys()):
        await stop_collection(room_id)

    # 刷写剩余数据
    if state.db:
        await state.db.flush_buffer()

    # 关闭 TaskManager
    await close_task_manager()

    # 关闭资源
    await close_database()
    await close_detector()

    # 关闭签名服务
    from src.utils.signer_manager import get_signer_manager
    signer_manager = get_signer_manager()
    await signer_manager.stop()

    log.info("已安全关闭 👋")


if __name__ == "__main__":
    main()
