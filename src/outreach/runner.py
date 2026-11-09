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
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_result(self) -> dict:
        return dict(self._last_result)

    def recent_logs(self, limit: int = 200) -> list[str]:
        with self._lock:
            return list(self._logs[-limit:])

    def _append_log(self, msg: str) -> None:
        with self._lock:
            self._logs.append(msg)
            if len(self._logs) > 1000:
                del self._logs[:500]

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
        if t == "need_login":
            self._append_log("⚠️ 需要登录：请先扫码登录触达专用浏览器")
        elif t == "risk":
            self._append_log(f"🛑 风控：{event.get('error')}")
        elif t == "sent":
            self._append_log(f"✅ 已留言：{event.get('text', '')[:40]}")

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
