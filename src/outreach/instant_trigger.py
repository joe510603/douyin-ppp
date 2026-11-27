"""新线索即时触达 —— 新进线索一进来就发，不等批量轮询

用户要求（2026-11-18）
----------------------
「新加一个功能，新进线索自动触达，开启后，有新的符合要求的抖音号进来，
就立刻触达，其他的抖音号不管，这样既可以提高回复的概率，
也可以不用集中去触达。」

和 ``auto_mode`` 的分工
----------------------
``auto_mode`` 是**批量轮询**：一轮一批（配置 30 条），轮与轮之间还有换手间隙。
新线索要等当前轮跑到它，或者等下一轮 —— 转化窗口就过了。

本模块是**事件驱动**：线索一落库就入队，后台消费，一轮到就发。
两者共用同一套风控机制，但**互不依赖** —— ``auto_mode`` 关着也能用本功能。

「其他的抖音号不管」
------------------
只处理**刚进来的那一条**：``runner.run(limit=1, only=[display_id])``。
``only`` 是按抖音号过滤的，所以积压的旧线索一条都不会被卷进来。

⚠ 单条触达必须自己控速（这是本模块最容易踩的坑）
-------------------------------------------------
``worker`` 的自适应间隔是**两条之间**才 sleep 的
（源码里是 ``if idx < len(picked)`` —— 一轮只有一条就不睡）。
即时触达每次只跑一条 → 间隔**不生效** → 新线索密集时会连着发，
等于拿账号硬撞风控。

所以这里自己加最小间隔（``auto_instant_gap_seconds``），
再加一道每小时封顶（``auto_instant_max_per_hour``）。

风控复用
--------
冷却、验证冷却、暂停、每日上限、30 天去重 —— 全部走现有的机制，
一个都不绕过：

* 能不能跑 → ``AutoScheduler.blockers()``（两边同一份判断）
* 撞了验证怎么办 → ``AutoScheduler.note_round_result()``
  （自动暂停 + 飞书红卡，和批量轮询完全一样）
* 每日上限、去重 → ``worker`` 内部本来就判

排队而不是丢
------------
跑不了的时候（正在跑批量轮询 / 冷却中 / 暂停中），
线索**留在队列里等**，不丢。等条件好了再来。
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..config import get_config
from ..utils.logger import get_logger
from .auto_scheduler import get_auto_scheduler
from .runner import get_outreach_runner

log = get_logger("outreach_instant")

# 队列上限：弹幕潮来的时候不至于把内存撑爆
_QUEUE_MAX = 100
# 同一条线索最多重试几次（免得一条卡死的线索把队列堵死）
_MAX_RETRY = 3


class InstantTrigger:
    """新线索即时触达触发器

    职责：
    1. ``notify()`` —— 线索落库后调用，**非阻塞**入队
    2. ``loop()``   —— 后台消费队列，一条一条发
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._task: Optional[asyncio.Task] = None
        # 上次真正发出（开始跑）的时间戳 → 算最小间隔
        self._last_sent_at: float = 0.0
        # 最近一小时的发出时间戳 → 算每小时封顶
        self._sent_times: list[float] = []
        # 每条线索的重试次数（没跑成 → 放回队列）
        self._retries: dict[str, int] = {}
        # 干跑模式下也算「发出」吗？**算** —— 干跑照样发搜索请求，
        # 照样消耗风控额度，所以节流必须一样。
        self.stats: dict = {
            "queued": 0, "sent": 0, "dry": 0, "failed": 0,
            "dropped": 0, "not_found": 0, "waiting": 0,
        }
        self.last_error: str = ""
        self.last_lead: str = ""

    # ================================================================
    # 开关 / 状态
    # ================================================================

    @property
    def enabled(self) -> bool:
        o = get_config().outreach
        return bool(o.auto_instant)

    def status(self) -> dict:
        """给页面显示的状态"""
        o = get_config().outreach
        # ⚠ 0 是合法取值（间隔 0 = 不限速；上限 0 = 不封顶），
        #    所以不能用 ``or 默认值`` —— ``0 or 30`` 会把 0 变成 30。
        raw_gap = o.auto_instant_gap_seconds
        gap = 30 if raw_gap is None else int(raw_gap)
        raw_cap = o.auto_instant_max_per_hour
        cap = 20 if raw_cap is None else int(raw_cap)
        since_last = 0.0
        if self._last_sent_at:
            since_last = max(0.0, time.time() - self._last_sent_at)
        return {
            "enabled": self.enabled,
            "configured": bool(o.auto_instant),
            "dry_run": bool(o.auto_instant_dry_run),
            "gap_seconds": gap,
            "max_per_hour": cap,
            "queued": self._queue.qsize(),
            "waiting": self.stats.get("waiting", 0),
            "sent": self.stats.get("sent", 0),
            "dry": self.stats.get("dry", 0),
            "failed": self.stats.get("failed", 0),
            "not_found": self.stats.get("not_found", 0),
            "dropped": self.stats.get("dropped", 0),
            "last_lead": self.last_lead,
            "since_last_seconds": int(since_last),
            "running": bool(self._task and not self._task.done()),
            "last_error": self.last_error,
        }

    # ================================================================
    # 入队
    # ================================================================

    def notify(self, lead: dict) -> bool:
        """有新线索进来了 → 排进即时触达队列（非阻塞）

        Args:
            lead: 线索 dict（至少要有 ``display_id``）

        Returns:
            True = 入队成功；False = 没入队（开关没开 / 没有抖音号 / 队列满了）
        """
        if not self.enabled:
            return False
        did = str((lead or {}).get("display_id") or "").strip()
        if not did:
            # 没抖音号就搜不到人，触达不了
            return False
        try:
            self._queue.put_nowait(did)
        except asyncio.QueueFull:
            # 队列满了 → 丢最老的一条，保新的
            # （新线索的价值更高，而且老的那条还在待触达队列里，没丢）
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self.stats["dropped"] = self.stats.get("dropped", 0) + 1
            except Exception:
                pass
            try:
                self._queue.put_nowait(did)
            except Exception:
                self.last_error = "即时触达队列已满，这条没能入队"
                log.warning(f"⚠️ {self.last_error}")
                return False
        self.stats["queued"] = self.stats.get("queued", 0) + 1
        log.info(f"⚡ 新线索入队（即时触达）：{did}")
        return True

    # ================================================================
    # 生命周期
    # ================================================================

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.loop(), name="instant-outreach")
        log.info("⚡ 即时触达通道已启动")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        log.info("⚡ 即时触达通道已停止")

    # ================================================================
    # 消费
    # ================================================================

    async def loop(self) -> None:
        """后台消费队列

        一次一条，串行 —— 触达本来就不该并发（同一个浏览器、同一套风控）。
        """
        while True:
            try:
                did = await self._queue.get()
            except asyncio.CancelledError:
                log.info("⚡ 即时触达通道已停止")
                raise
            try:
                await self._process(did)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.error(f"⚡ 即时触达异常：{e}", exc_info=True)
            finally:
                try:
                    self._queue.task_done()
                except Exception:
                    pass

    async def _process(self, did: str) -> None:
        """处理一条新线索：等条件满足 → 发 → 记账"""
        scheduler = get_auto_scheduler()
        runner = get_outreach_runner()
        o = get_config().outreach

        # ---- 1. 等到能跑 ----
        while True:
            if not self.enabled:
                # 开关被关了 → 这条不发了（它还在待触达队列里，不会丢）
                self.last_error = "即时触达开关已关闭"
                return

            blockers = scheduler.blockers(ignore_auto_mode=True)
            if blockers:
                # 跑不了 → 等一等再看，别丢
                self.last_error = blockers[0]
                self.stats["waiting"] = self._queue.qsize() + 1
                await asyncio.sleep(self._wait_seconds(blockers))
                continue

            # 每日上限（worker 内部也会判，这里先挡一层，别白跑）
            try:
                sent_today = int(runner.sent_today() or 0)
            except Exception:
                sent_today = 0
            # ⚠️ daily_limit = 0 表示**不限**（2026-11-27 用户要求）
            #   原来写的是 `int(o.daily_limit or 0)`，daily_limit=0 时算出 0，
            #   而 `sent_today >= 0` 永远成立 → 即时触达会**一条都发不出去**。
            daily_limit = int(o.daily_limit if o.daily_limit is not None else 0)
            if daily_limit > 0 and sent_today >= daily_limit:
                self.last_error = f"今日已发满 {sent_today}/{daily_limit}"
                await asyncio.sleep(60)
                continue

            # 每小时封顶
            if self._hour_cap_reached():
                self.last_error = (
                    f"本小时即时触达已达上限 "
                    f"{len(self._sent_times)}/{o.auto_instant_max_per_hour}"
                )
                await asyncio.sleep(60)
                continue

            # 最小间隔（单条触达没有轮内间隔，必须自己控速）
            gap_left = self._gap_left()
            if gap_left > 0:
                await asyncio.sleep(gap_left)
                continue

            break

        # ---- 2. 真跑 ----
        dry = bool(o.auto_instant_dry_run)
        self.last_lead = did

        log.info(f"⚡ 即时触达：{did}（{'干跑' if dry else '真发'}）")
        try:
            result = await runner.run(limit=1, only=[did], dry_run=dry)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.stats["failed"] = self.stats.get("failed", 0) + 1
            log.error(f"⚡ 即时触达失败：{e}", exc_info=True)
            self._requeue(did, reason=str(e))
            return
        finally:
            # 间隔从**这一轮结束**开始算（和 worker 轮内的语义一致）。
            #
            # 为什么不是从开始算：一轮触达本身要十几秒（搜人+挑作品+发），
            # 从开始计时的话，实际两条之间的间隔会被压缩成
            # 「间隔 - 本轮耗时」—— 轮跑得越久，间隔越短，越容易撞风控。
            self._mark_attempt()

        result = result or {}

        # 撞验证 / 发满 → 和批量轮询一样：暂停 + 飞书红卡，不硬撞
        #
        # ⚠ 必须在下面「没跑成要放回队列」的判断**之前** ——
        #   撞验证时 result["ok"] 也是 False（压根没发出去），
        #   如果先 return，就永远走不到这里 →
        #   撞了抖音验证不会暂停 → 接着撞下一条 → 越撞越凶。
        scheduler.note_round_result(result)

        # 没跑成（例：批量轮询刚好也在跑）→ 放回队列，别把这条丢了。
        #
        # 竞态是真的：``blockers()`` 查「有没有在跑」和真正 ``runner.run()``
        # 之间隔着 await（间隔等待），这中间批量轮询可能刚好启动。
        # 这时候 run() 会直接返回 ok=False、这条根本没被处理 ——
        # 不放回去的话，新线索就此消失（它还在待触达队列里，但不再享受即时触达）。
        if result.get("ok") is False and not result.get("sent") and not result.get("dry"):
            self._requeue(
                did,
                reason=str(result.get("error") or result.get("stopped") or "没跑成"),
            )
            return
        self.stats["sent"] = self.stats.get("sent", 0) + int(result.get("sent") or 0)
        self.stats["dry"] = self.stats.get("dry", 0) + int(result.get("dry") or 0)
        self.stats["failed"] = self.stats.get("failed", 0) + int(result.get("failed") or 0)
        self.stats["not_found"] = (
            self.stats.get("not_found", 0) + int(result.get("not_found") or 0)
        )
        self.last_error = str(result.get("stopped") or "")

        log.info(
            f"⚡ 即时触达完成：发 {result.get('sent', 0)}　"
            f"干跑 {result.get('dry', 0)}　失败 {result.get('failed', 0)}"
            + (f"　停止原因：{result.get('stopped')}" if result.get("stopped") else "")
        )

    def _requeue(self, did: str, reason: str = "") -> None:
        """没跑成 → 放回队列，等下一轮再试

        重试次数有上限（``_MAX_RETRY``），免得一条卡死的线索把队列堵死。

        ⚠ 放弃之后**不要**把计数 pop 掉 ——
        pop 掉的话下一次又从 1 开始数，这条线索就会无限重试
        （实测踩过：队列每轮 +1，永远清不掉）。
        保留一个「已超限」的计数，让它从此不再入队。
        """
        retry = self._retries.get(did, 0) + 1
        self._retries[did] = retry
        if retry > _MAX_RETRY:
            self.last_error = f"{did} 重试 {_MAX_RETRY} 次仍没跑成，放弃（即时触达）"
            log.warning(f"⚡ {self.last_error}：{reason}")
            return
        try:
            self._queue.put_nowait(did)
        except asyncio.QueueFull:
            pass
        self.last_error = f"{did} 没跑成，稍后重试（第 {retry} 次）：{reason}"
        log.info(f"⚡ {self.last_error}")

    # ================================================================
    # 限速辅助
    # ================================================================

    def _mark_attempt(self) -> None:
        """记一次尝试（不管成功失败）—— 用于算间隔和每小时封顶"""
        self._last_sent_at = time.time()
        self._sent_times.append(self._last_sent_at)
        self._trim_sent_times()

    def _gap_left(self) -> float:
        """距离上次发出还差几秒

        ⚠ 别写成 ``o.auto_instant_gap_seconds or 30`` ——
        ``0 or 30`` 是 **30**！0 是合法取值（= 不限速），
        那个写法会把用户设的「0 秒」悄悄变成 30 秒。
        """
        if not self._last_sent_at:
            return 0.0
        o = get_config().outreach
        raw = o.auto_instant_gap_seconds
        gap = 30.0 if raw is None else float(raw)
        return max(0.0, gap - (time.time() - self._last_sent_at))

    def _hour_cap_reached(self) -> bool:
        """这一小时是否已经到上限（0 = 不封顶）"""
        o = get_config().outreach
        raw = o.auto_instant_max_per_hour
        cap = 20 if raw is None else int(raw)
        if cap <= 0:
            return False
        self._trim_sent_times()
        return len(self._sent_times) >= cap

    def _trim_sent_times(self) -> None:
        """只保留最近一小时的记录"""
        cutoff = time.time() - 3600
        self._sent_times = [t for t in self._sent_times if t >= cutoff]

    @staticmethod
    def _wait_seconds(blockers: list[str]) -> float:
        """被挡住时等多久再看一次

        「已有触达任务在跑」→ 轮次可能要跑好几分钟，等久一点；
        其它（冷却/暂停）→ 短一点，恢复了就赶紧发。
        """
        if any("在跑" in b for b in blockers):
            return 15.0
        if any("冷却" in b for b in blockers):
            return 30.0
        return 10.0


# ================================================================
# 单例
# ================================================================

_trigger: Optional[InstantTrigger] = None


def get_instant_trigger() -> InstantTrigger:
    global _trigger
    if _trigger is None:
        _trigger = InstantTrigger()
    return _trigger


def notify_new_lead(lead: dict) -> bool:
    """有新线索进来了 —— 通知即时触达通道（给 lead_service 调）

    刻意做成**不抛异常、不阻塞**：实时采集链路不能因为触达通道出问题就卡住。
    触达通道自己会记日志。
    """
    try:
        return get_instant_trigger().notify(lead)
    except Exception as e:
        log.warning(f"⚡ 即时触达通知失败（不影响采集）：{e}")
        return False
