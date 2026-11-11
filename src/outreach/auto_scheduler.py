"""自动触达调度器 — 定时自动跑触达，不用人守着点按钮

用户要求：「我想要能自动取做触达，不用我在手动跑了，有可以设置自动」

几个关键的安全决定
------------------
1. **自动模式默认干跑**（``auto_dry_run=True``）。
   自动 = 无人值守。无人值守时真发评论，一旦撞风控，
   人不在电脑前就白跑一轮，还可能把号搞废。
   要「自动真发」，必须在页面上**显式**把干跑关掉——
   没人值守的时候，默认就该是安全的那个。
2. **不改变已有的安全阀**：冷却中不跑、每日上限照旧、撞风控照旧停手。
   自动只负责「什么时候跑一轮」，不碰「怎么跑」的风险控制。
3. **不重复触发**：上一轮还在跑就跳过这一轮。

另外要提醒用户的坑（页面上也写了）：
**多机共用同一张飞书表时，两台机器都开自动触达会重复留言**——
去重靠的是本机 ``outreach_records``，跨机看不见对方的记录。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from ..config import get_config
from ..utils.logger import get_logger

log = get_logger("outreach_auto")


class AutoScheduler:
    """自动触达调度器

    跟 OutreachRunner 的关系：**只调用，不改逻辑**。
    所有风控判断、停手、通知都在 runner/worker 里，
    这里只决定「什么时候该跑一轮」。
    """

    def __init__(self, runner):
        self.runner = runner
        # 上次跑完的时间（None = 还没跑过）
        self.last_run_at: Optional[datetime] = None
        self.last_result: dict = {}
        self.last_error: str = ""
        # 手动触发：跳过「间隔没到」的限制，立刻跑一轮
        self._force_next = False

    # ================================================================
    # 状态（给页面显示）
    # ================================================================

    def status(self) -> dict:
        """当前自动触达状态"""
        cfg = get_config()
        o = cfg.outreach
        enabled = bool(o.auto_mode) and bool(o.enabled)
        interval = max(1, int(o.auto_interval_minutes or 60))

        next_at = ""
        if enabled and self.last_run_at:
            next_at = (self.last_run_at + timedelta(minutes=interval)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        return {
            "enabled": enabled,
            "configured": bool(o.auto_mode),
            "dry_run": bool(o.auto_dry_run),
            "interval_minutes": interval,
            "max_per_run": int(o.auto_max_per_run or o.max_per_run or 10),
            "last_run_at": self.last_run_at.strftime("%Y-%m-%d %H:%M:%S")
            if self.last_run_at else "",
            "next_run_at": next_at,
            "last_result": dict(self.last_result or {}),
            "last_error": self.last_error,
            "running": bool(self.runner.is_running),
            "cooldown": int(self.runner.risk_cooldown_left() or 0),
        }

    def trigger_now(self) -> None:
        """立刻跑一轮（跳过间隔限制）

        用户在页面上刚打开自动开关时调用——
        让人当场看到效果，而不是干等一个周期。
        """
        self._force_next = True
        log.info("⏩ 手动触发一轮自动触达")

    # ================================================================
    # 判断 + 执行
    # ================================================================

    async def maybe_run(self) -> bool:
        """检查该不该跑一轮；该跑就跑。

        Returns: True = 真的跑了；False = 没跑（附原因在 last_error）
        """
        cfg = get_config()
        o = cfg.outreach

        if not o.auto_mode:
            return False
        if not o.enabled:
            self.last_error = "触达功能没启用"
            return False
        if self.runner.is_running:
            return False

        # 冷却中不跑（真被限流了还要硬撞就是找死）
        left = int(self.runner.risk_cooldown_left() or 0)
        if left > 0:
            self.last_error = f"限流冷却中，还剩 {left} 分钟"
            return False

        # 间隔没到不跑
        interval = max(1, int(o.auto_interval_minutes or 60))
        if not self._force_next and self.last_run_at:
            elapsed = (datetime.now() - self.last_run_at).total_seconds() / 60
            if elapsed < interval:
                self.last_error = f"距上轮才过 {int(elapsed)} 分钟，间隔 {interval} 分钟"
                return False

        self._force_next = False
        dry = bool(o.auto_dry_run)
        limit = int(o.auto_max_per_run or o.max_per_run or 10)
        mode = "干跑" if dry else "真发"
        log.info(f"🤖 自动触达开始（{mode}，本轮最多 {limit} 条）")

        try:
            result = await self.runner.run(limit=limit, dry_run=dry)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.last_result = {}
            log.error(f"🤖 自动触达异常: {e}", exc_info=True)
            return False

        self.last_run_at = datetime.now()
        self.last_result = dict(result or {})
        stopped = str((result or {}).get("stopped") or "")
        self.last_error = stopped
        log.info(
            f"🤖 自动触达结束：发 {(result or {}).get('sent', 0)}　"
            f"干跑 {(result or {}).get('dry', 0)}　"
            f"失败 {(result or {}).get('failed', 0)}"
            + (f"　停止原因：{stopped}" if stopped else "")
        )
        return True

    # ================================================================
    # 常驻循环
    # ================================================================

    async def loop(self) -> None:
        """常驻循环：每 30 秒检查一次

        用 30 秒的检查粒度而不是等一个整周期，
        是为了让「手动触发」「冷却结束」这类状态变化能及时生效。
        """
        log.info("🤖 自动触达调度器已启动")
        while True:
            try:
                await asyncio.sleep(30)
                await self.maybe_run()
            except asyncio.CancelledError:
                log.info("🤖 自动触达调度器已停止")
                raise
            except Exception as e:
                log.error(f"自动触达调度异常: {e}", exc_info=True)
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    raise


# ================================================================
# 单例
# ================================================================

_auto: Optional[AutoScheduler] = None


def get_auto_scheduler() -> AutoScheduler:
    """获取全局自动触达调度器

    跟 OutreachRunner 用同一个单例 —— 两者必须指向同一个 runner，
    否则调度器判断「正在跑」会看错对象，可能重复触发。
    """
    global _auto
    if _auto is None:
        from .runner import get_outreach_runner
        _auto = AutoScheduler(get_outreach_runner())
    return _auto
