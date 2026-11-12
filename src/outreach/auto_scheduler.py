"""自动触达调度器 — 打开开关就一直在跑，不用人守着点按钮

用户要求（2026-11-12）
--------------------
「打开自动触达，就会一直自己去触达了，包括有新进的也会自动触达，
遇到需要验证的，提醒我。」

所以这里做三件事
---------------
1. **连续跑**（``auto_continuous``，默认开）
   老逻辑是「每隔 N 分钟跑一轮」——两轮之间要空等 N 分钟，
   新进的线索要等到下个周期才被处理，队列排空后更是干等一个周期。
   连续模式改成：轮完立刻接下一轮（只隔一个很短的换手间隙），
   队列空了不空等，每 ``auto_idle_poll_seconds`` 秒看一眼有没有新线索。

   ⚠ 连续跑**不等于没有节奏**：轮内每条之间的自适应间隔（20-30 秒）、
   每 5 条的长休息、每日上限、30 天去重全都照旧。
   连续模式只是去掉**轮与轮之间的那段空等**。

2. **撞验证就停下来叫人**（不是硬撞）
   撞了「本人操作验证」/ 登录失效 → 自动触达**暂停**，等人处理。
   15 分钟内人在网页上点了「我验证好了，继续」，这一轮会接着跑；
   没人管的话这一轮结束、自动触达停在暂停状态，
   等你在网页上点「解除暂停」或手动跑一轮。
   （暂停不解除就自动重试 = 拿账号硬撞验证，只会越撞越凶。）

3. **提醒走飞书群机器人**
   见 ``worker._notify_risk``：群机器人发红卡片 + @人，手机强提醒；
   私聊兜底（私聊会话不产生推送，实测过）。

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
from .douyin_api import KIND_LIMIT, KIND_LOGIN, KIND_SEARCH, KIND_VERIFY

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
        # ---- 暂停状态 ----
        #
        # 撞了验证码/登录失效 → 停下来等人，不硬撞。
        # ``_paused_until`` 有值 = 到点自动恢复（今天已发满会用到）；
        # 没值 = 必须人来解除（在网页上点一下）。
        self._paused = False
        self._paused_reason: str = ""
        self._paused_kind: str = ""
        self._paused_until: Optional[datetime] = None
        # 上一轮是不是「没东西可做」（队列是空的）→ 决定下次隔多久看一眼
        self._last_round_empty = False

    # ================================================================
    # 暂停 / 恢复
    # ================================================================

    def _pause(self, reason: str, kind: str = "",
               until: Optional[datetime] = None) -> None:
        """暂停自动触达，等人处理"""
        self._paused = True
        self._paused_reason = reason
        self._paused_kind = kind
        self._paused_until = until
        log.info(f"🤖 自动触达已暂停：{reason}")

    def clear_pause(self, reason: str = "手动解除暂停") -> bool:
        """解除暂停（人在网页上点「解除暂停」或手动跑一轮时调用）"""
        if not self._paused:
            return False
        self._paused = False
        self._paused_reason = ""
        self._paused_kind = ""
        self._paused_until = None
        # 解除后立刻跑一轮，不用等下一次轮询
        self._force_next = True
        log.info(f"🤖 自动触达恢复：{reason}")
        return True

    @property
    def paused(self) -> bool:
        return self._paused

    # ================================================================
    # 状态（给页面显示）
    # ================================================================

    def status(self) -> dict:
        """当前自动触达状态"""
        cfg = get_config()
        o = cfg.outreach
        enabled = bool(o.auto_mode) and bool(o.enabled)
        interval = max(1, int(o.auto_interval_minutes or 60))
        continuous = bool(getattr(o, "auto_continuous", True))
        gap = int(getattr(o, "auto_round_gap_seconds", 60) or 60)
        idle_gap = int(getattr(o, "auto_idle_poll_seconds", 30) or 30)

        next_at = ""
        if enabled and self.last_run_at:
            # 连续模式下「下次」不是固定周期，而是「隔多久再看」
            wait = idle_gap if self._last_round_empty else gap
            next_at = (self.last_run_at + timedelta(seconds=wait)).strftime(
                "%H:%M:%S"
            )

        # 状态词：给人一眼看明白现在在干嘛
        if not enabled:
            state = "已关闭"
        elif self._paused:
            state = "已暂停"
        elif self.runner.is_running:
            state = "正在跑"
        elif self._last_round_empty:
            state = "待命中"
        else:
            state = "待下一轮"

        return {
            "enabled": enabled,
            "configured": bool(o.auto_mode),
            "dry_run": bool(o.auto_dry_run),
            "interval_minutes": interval,
            "continuous": continuous,
            "round_gap_seconds": gap,
            "idle_poll_seconds": idle_gap,
            "max_per_run": int(o.auto_max_per_run or o.max_per_run or 10),
            "last_run_at": self.last_run_at.strftime("%Y-%m-%d %H:%M:%S")
            if self.last_run_at else "",
            "next_run_at": next_at,
            "last_result": dict(self.last_result or {}),
            "last_error": self.last_error,
            "running": bool(self.runner.is_running),
            "cooldown": int(self.runner.risk_cooldown_left() or 0),
            "paused": self._paused,
            "paused_reason": self._paused_reason,
            "paused_kind": self._paused_kind,
            "state": state,
        }

    def trigger_now(self) -> None:
        """立刻跑一轮（跳过间隔限制）

        用户在页面上刚打开自动开关时调用——
        让人当场看到效果，而不是干等一个周期。
        """
        self._force_next = True
        # 手动触发也算「人来处理了」→ 顺手把暂停解掉
        if self._paused:
            self.clear_pause("手动触发")
        log.info("⏩ 手动触发一轮自动触达")

    # ================================================================
    # 判断 + 执行
    # ================================================================

    def _gap_seconds(self, o) -> float:
        """这一轮跑完，下一轮隔多久再看

        连续模式：队列空了就早点再看（有新线索马上跑），
        有活干就按换手间隙来（给浏览器收尾的时间）。
        老逻辑：固定间隔。
        """
        if getattr(o, "auto_continuous", True):
            if self._last_round_empty:
                return float(getattr(o, "auto_idle_poll_seconds", 30) or 30)
            return float(getattr(o, "auto_round_gap_seconds", 60) or 60)
        return float(max(1, int(o.auto_interval_minutes or 60))) * 60

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

        # 暂停中：等人处理
        if self._paused:
            if self._paused_until and datetime.now() >= self._paused_until:
                # 到点了自动恢复（比如今天已发满，明天再跑）
                self.clear_pause("暂停时间到，自动恢复")
            elif not self._force_next:
                self.last_error = self._paused_reason or "已暂停，等人工处理"
                return False

        # 间隔没到不跑
        if not self._force_next and self.last_run_at:
            gap = self._gap_seconds(o)
            elapsed = (datetime.now() - self.last_run_at).total_seconds()
            if elapsed < gap:
                self.last_error = (
                    f"距上轮才过 {int(elapsed)} 秒，还要等 {int(gap - elapsed)} 秒"
                )
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
        # 队列是空的 → 下一轮早点来看有没有新线索
        self._last_round_empty = int((result or {}).get("total") or 0) == 0

        log.info(
            f"🤖 自动触达结束：发 {(result or {}).get('sent', 0)}　"
            f"干跑 {(result or {}).get('dry', 0)}　"
            f"失败 {(result or {}).get('failed', 0)}"
            + (f"　停止原因：{stopped}" if stopped else "")
        )

        self._after_round(result or {})
        return True

    def _after_round(self, result: dict) -> None:
        """一轮跑完之后：判断要不要暂停"""
        # 撞了验证码 / 登录失效 → 停下来等人，不硬撞。
        # 判据用结构化的 stop_kind，不靠解析文案（文案会漂）。
        kind = str(result.get("stop_kind") or "")
        if kind in (KIND_VERIFY, KIND_LOGIN):
            label = "抖音要求验证本人操作" if kind == KIND_VERIFY else "登录态失效"
            self._pause(
                f"{label}，触达已停下等你处理。"
                f"去网页「抖音触达」验证完点「解除暂停」就能接着跑",
                kind,
            )
            return
        if kind == KIND_LIMIT:
            # 限流：冷却由 runner 的 risk_cooldown_left 管，
            # 这里不暂停（冷却完了自动接着跑），但要说清楚在等什么。
            self.last_error = (
                "账号被限流，触达已停手，等冷却结束自动继续"
            )
            return
        if kind == KIND_SEARCH:
            self.last_error = "抖音网页版搜索被限流，等它自己恢复"
            return

        # 今天已发满 → 暂停到明天（否则调度器会每分钟空跑一轮）
        if result.get("daily_limit_reached"):
            until = (datetime.now() + timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0
            )
            self._pause(
                f"今天已发满 {self.last_result.get('sent', 0)} 条，"
                f"明天 {until.strftime('%H:%M')} 自动恢复",
                "daily_limit",
                until=until,
            )

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
