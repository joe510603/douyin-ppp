"""触达任务编排器 —— asyncio 侧入口

线程模型（重要）
----------------
触达要用 Playwright 的 **sync API**（抖音网页操作成熟稳定的那套），
而主进程是 asyncio 架构。两者的关键约束：

- Playwright sync API **不能跨线程调用** —— 对象必须在创建它的线程里用
- 所以整个触达流程跑在**一个专用线程**里，主进程只负责：
  - 起这个线程、把结果收回来
  - 展示进度（回调）
  - 不阻塞自己的事件循环（NiceGUI 页面照常响应）

这样既不用把 1000 多行的抖音操作逻辑重写成 async，
也不会让「跑一轮要几分钟」的任务卡死 Web 服务。

安全阀
------
所有「会不会把号跑废」的判断都在 worker 里：
干跑默认开、每日上限、随机间隔、长休息、N 天去重、连续失败自停、撞风控立即停。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime
from typing import Callable, Optional

from ..config import get_config
from ..storage.db_storage import get_db
from ..utils.logger import get_logger
from .store import OutreachStore

log = get_logger("outreach_runner")


class OutreachRunner:
    """触达任务执行器（asyncio 侧）"""

    def __init__(self):
        self.config = get_config()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_result: dict = {}
        self._logs: list[str] = []
        # 结构化事件流（网页控制台显示每条线索的详情）
        self._events: list[dict] = []
        # 本轮进度：总数 / 已处理 / 当前在处理第几条
        self._progress: dict = {}
        self._lock = threading.Lock()
        # 撞验证码时 worker 会停下等人工验证。
        # 网页上点「我验证好了，继续」→ resume() → worker 那边醒过来接着跑。
        self._resume = threading.Event()
        self._resume.set()          # 初始状态 = 不需要等
        # 网页上点「打开浏览器窗口」→ 这里 set → worker 把窗口叫到屏幕中间
        # （Playwright 的 sync API 不能跨线程用，浏览器归 worker 线程管，
        #   所以主线程不能直接去挪窗口，只能发个信号过去）
        self._show_window = threading.Event()
        self._waiting_human = False
        self._captcha_reason = ""

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_result(self) -> dict:
        return dict(self._last_result)

    # ================================================================
    # 撞验证码：等人工验证
    # ================================================================

    @property
    def waiting_human(self) -> bool:
        """当前是不是在等人工验证"""
        return self._waiting_human

    def show_browser_window(self) -> bool:
        """把浏览器窗口叫到屏幕中间，让用户自己去操作

        网页上那个「打开浏览器窗口（我自己验证）」按钮调它。

        为什么要绕一圈发信号：浏览器是 worker 线程里的 Playwright 对象，
        sync API 不允许跨线程调用，所以主线程不能直接去挪窗口。
        """
        if not self._waiting_human:
            return False
        self._append_log("🪟 正在把浏览器窗口叫到屏幕中间…")
        self._show_window.set()
        return True

    def resume(self) -> bool:
        """人工验证完了，继续跑

        网页上那个「我验证好了，继续」按钮调它。
        """
        if not self._waiting_human:
            return False
        self._append_log("▶ 收到人工验证完成信号，继续触达")
        self._waiting_human = False
        self._captcha_reason = ""
        self._resume.set()
        return True

    def _mark_waiting_human(self, reason: str) -> None:
        self._waiting_human = True
        self._captcha_reason = reason
        self._resume.clear()

    def recent_logs(self, limit: int = 200) -> list[str]:
        with self._lock:
            return list(self._logs[-limit:])

    def recent_events(self, limit: int = 200) -> list[dict]:
        """本轮每条线索的处理详情（给网页控制台画表）"""
        with self._lock:
            return list(self._events[-limit:])

    def progress(self) -> dict:
        """控制台顶部要显示的进度信息"""
        with self._lock:
            p = dict(self._progress)
        p["running"] = self._running
        p["cooldown"] = self.risk_cooldown_left()
        p["waiting_human"] = self._waiting_human
        p["captcha_reason"] = self._captcha_reason
        return p

    def _append_log(self, msg: str) -> None:
        """记一条触达日志

        除了存内存（给网页看），还实时打到控制台 + 服务日志。
        触达是个长流程（每条要几十秒），不实时输出的话
        干等的人根本不知道跑到哪一步了。
        """
        with self._lock:
            self._logs.append(msg)
            if len(self._logs) > 1000:
                del self._logs[:500]
        try:
            print(msg, flush=True)
        except Exception:
            pass
        try:
            log.info("[触达] %s", msg)
        except Exception:
            pass

    def risk_cooldown_left(self) -> int:
        """风控冷却还剩多少分钟（0 = 可以跑）

        抖音软风控（评论发出去但页面看不到）一旦触发，
        继续发只会把号搞废——冷却期内直接拒绝启动。
        """
        try:
            st = OutreachStore(self._db_path())
            try:
                runs = st.recent_runs(limit=20)
            finally:
                st.close()
        except Exception:
            return 0

        minutes = int(getattr(self.config.outreach, "risk_cooldown_minutes", 120) or 0)
        if minutes <= 0:
            return 0
        for r in runs:
            note = str(r.get("note") or "")
            if "风控" not in note and "限流" not in note:
                continue
            started = str(r.get("started_at") or "")
            try:
                t = datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            left = minutes - (datetime.now() - t).total_seconds() / 60
            if left > 0:
                return int(left)
        return 0

    def _db_path(self) -> str:
        db = get_db()
        path = getattr(db, "db_path", None)
        if path:
            return str(path)
        return "data/db/douyin_ppp.db"

    async def run(
        self,
        limit: Optional[int] = None,
        dry_run: Optional[bool] = None,
        only: Optional[list[str]] = None,
    ) -> dict:
        """跑一轮触达任务

        Args:
            limit: 本轮最多处理几条
            dry_run: 干跑（不发送、不回写状态）
            only: 只处理指定的抖音号

        Returns:
            {"ok", "total", "sent", "failed", "skipped", "dry", "stopped"}
        """
        if self._running:
            return {"ok": False, "error": "已有触达任务在跑", "total": 0,
                    "sent": 0, "failed": 0, "skipped": 0}

        o = self.config.outreach
        if not o.enabled and dry_run is not True:
            return {"ok": False, "error": "触达功能未启用（配置页 → 抖音触达）",
                    "total": 0, "sent": 0, "failed": 0, "skipped": 0}

        # 风控冷却：账号刚被判限流就别再撞了。
        # 干跑也算——干跑虽然不发评论，但照样发搜索请求，
        # 账号被限流的时候连搜索都可能加重风控。
        left = self.risk_cooldown_left()
        if left > 0:
            msg = (f"账号疑似被限流（软风控），冷却中，还剩 {left} 分钟。"
                   f"先别跑，等风控过去再说")
            self._append_log(f"🛑 {msg}")
            return {"ok": False, "error": msg, "total": 0,
                    "sent": 0, "failed": 0, "skipped": 0}

        self._running = True
        self._logs = []
        started = datetime.now().strftime("%H:%M:%S")
        self._append_log(f"▶ [{started}] 触达任务开始"
                          f"{'（干跑）' if (o.dry_run if dry_run is None else dry_run) else ''}")

        loop = asyncio.get_event_loop()
        try:
            # 在线程里跑同步的 worker，主事件循环不受阻
            result = await loop.run_in_executor(
                None,
                self._run_in_thread,
                limit, dry_run, only,
            )
        except Exception as e:
            log.error(f"触达任务异常: {e}", exc_info=True)
            result = {"ok": False, "error": str(e), "total": 0,
                      "sent": 0, "failed": 0, "skipped": 0}
        finally:
            self._running = False
            self._waiting_human = False
            self._captcha_reason = ""
            self._resume.set()
            self._show_window.clear()

        self._last_result = result
        summary = (
            f"发 {result.get('sent', 0)}　干跑 {result.get('dry', 0)}　"
            f"失败 {result.get('failed', 0)}　跳过 {result.get('skipped', 0)}"
        )
        self._append_log(f"■ 触达任务结束：{summary}")
        log.info(f"[触达] 任务结束：{summary}")
        return result

    def _run_in_thread(self, limit, dry_run, only) -> dict:
        """在线程里跑 worker（同步）"""
        from .worker import OutreachWorker

        config = get_config()
        worker = OutreachWorker(
            config,
            self._db_path(),
            log=self._append_log,
            on_event=self._on_event,
            resume_event=self._resume,
            show_window_event=self._show_window,
        )
        try:
            return worker.run(limit=limit, dry_run=dry_run, only=only)
        finally:
            try:
                worker.st.close()
            except Exception:
                pass

    def _on_event(self, event: dict) -> None:
        """worker 的事件回调（在线程里触发）"""
        t = event.get("type")
        # 结构化事件流：网页控制台靠它显示「每条线索的处理详情」
        lead = event.get("lead") or {}
        item = {
            "type": t,
            "time": datetime.now().strftime("%H:%M:%S"),
            "display_id": lead.get("display_id") or "",
            "nickname": lead.get("nickname") or "",
            "car_model": lead.get("car_model") or "",
            "intent_level": lead.get("intent_level") or "",
            "text": (event.get("text") or "")[:120],
            "error": (event.get("error") or "")[:160],
            "aweme_id": (event.get("work") or {}).get("aweme_id") or "",
            "aweme_desc": ((event.get("work") or {}).get("desc") or "")[:40],
        }
        with self._lock:
            self._events.append(item)
            if len(self._events) > 500:
                del self._events[:250]
            # 进度：start 给总数，current 给序号，处理完一个 done +1
            if t == "start":
                self._progress = {
                    "total": int(event.get("total") or 0),
                    "done": 0,
                    "index": 0,
                    "current": "",
                    "mode": "干跑" if event.get("dry_run") else "真发",
                    "sent": 0, "failed": 0, "skipped": 0, "dry": 0,
                }
            elif t == "current":
                self._progress["index"] = int(event.get("index") or 0)
                self._progress["current"] = (
                    f"{lead.get('display_id') or ''} {lead.get('nickname') or ''}".strip()
                )
            elif t in ("sent", "failed", "skipped", "dry", "unshown"):
                self._progress["done"] = int(self._progress.get("done") or 0) + 1
                key = {"sent": "sent", "failed": "failed", "skipped": "skipped",
                       "dry": "dry", "unshown": "sent"}[t]
                self._progress[key] = int(self._progress.get(key) or 0) + 1
                self._progress["current"] = ""
        if t == "need_login":
            self._append_log("⚠️ 需要登录：请先扫码登录触达专用浏览器")
        elif t == "risk":
            self._append_log(f"🛑 风控：{event.get('error')}")
        elif t == "sent":
            self._append_log(f"✅ 已留言：{event.get('text', '')[:40]}")
        elif t == "captcha_wait":
            self._mark_waiting_human(str(event.get("error") or ""))
            self._append_log(
                "⏸ 撞上验证码，已暂停等你人工验证"
                f"（最多 {event.get('minutes') or 15} 分钟）"
            )
        elif t == "captcha_resumed":
            self._waiting_human = False
            self._captcha_reason = ""
        elif t == "captcha_timeout":
            self._waiting_human = False
            self._captcha_reason = ""
            self._append_log("⏸ 等验证码超时，结束本轮")

    # ================================================================
    # 扫码登录
    # ================================================================

    async def login(self, timeout: int = 300) -> dict:
        """打开触达专用浏览器，等用户扫码登录

        抖音的搜索和发评论都要求登录态（cookie 里有 sessionid）。
        这里用专用 profile 扫码一次，长期复用——
        把别处的 cookie 搬过来很容易被判「环境异常」。
        """
        from .login import login as _login

        o = self.config.outreach
        profile = self._profile_dir()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: _login(profile, log=self._append_log, timeout=timeout,
                           headless=False),
        )

    def _profile_dir(self) -> str:
        """触达专用浏览器 profile 目录"""
        import pathlib

        p = pathlib.Path(self.config.outreach.profile_dir or "data/outreach_profile")
        if not p.is_absolute():
            p = pathlib.Path.cwd() / p
        return str(p)

    # ================================================================
    # 查询接口（给页面用）
    # ================================================================

    def pending_count(self) -> int:
        """还有多少条待触达"""
        try:
            st = OutreachStore(self._db_path())
            try:
                return len(st.pending_leads(
                    intent_levels=list(self.config.outreach.intent_levels or []),
                    stores=list(self.config.outreach.stores or []),
                ))
            finally:
                st.close()
        except Exception:
            return 0

    def recent_runs(self, limit: int = 10) -> list[dict]:
        try:
            st = OutreachStore(self._db_path())
            try:
                return st.recent_runs(limit)
            finally:
                st.close()
        except Exception:
            return []

    def recent_touches(self, limit: int = 50) -> list[dict]:
        try:
            st = OutreachStore(self._db_path())
            try:
                return st.recent_touches(limit)
            finally:
                st.close()
        except Exception:
            return []

    def sent_today(self) -> int:
        try:
            st = OutreachStore(self._db_path())
            try:
                return st.sent_today()
            finally:
                st.close()
        except Exception:
            return 0

    # ================================================================
    # 话术预览（不发任何东西）
    # ================================================================

    def preview(self, limit: int = 10) -> list[dict]:
        """给页面预览「将要发出去的话术」

        只生成不发送，也不动任何状态。
        """
        from .copywriter import Copywriter
        from .worker import lead_to_copywriter_input

        o = self.config.outreach
        cfg_dict = {
            "comment": {
                "max_chars": o.max_chars,
                "salutation": o.salutation,
                "salutation_pool": list(o.salutation_pool or []),
                "templates_file": o.templates_file,
                "allow_contact_words": o.allow_contact_words,
            },
            "dm": {"max_chars": 120},
        }
        cw = Copywriter(cfg_dict, log=lambda m: None)
        st = OutreachStore(self._db_path())
        try:
            leads = st.pending_leads(
                intent_levels=list(o.intent_levels or []),
                stores=list(o.stores or []),
            )
        finally:
            st.close()

        out = []
        for lead in leads[:limit]:
            item = {
                "display_id": lead.get("display_id") or "",
                "nickname": lead.get("nickname") or "",
                "car_model": lead.get("car_model") or "",
                "store": lead.get("store") or "",
                "intent_level": lead.get("intent_level") or "",
                "intent_score": lead.get("intent_score") or 0,
                "intent_reason": lead.get("intent_reason") or "",
            }
            c = cw.generate(lead_to_copywriter_input(lead))
            item["text"] = c.get("text", "")
            item["template_id"] = c.get("template_id", "")
            item["ok"] = c.get("ok", True)
            item["reason"] = c.get("reason", "")
            out.append(item)
        return out


# ============================================================
# 全局单例
# ============================================================

_runner: Optional[OutreachRunner] = None


def get_outreach_runner() -> OutreachRunner:
    """获取全局触达执行器"""
    global _runner
    if _runner is None:
        _runner = OutreachRunner()
    return _runner
