"""**新线索即时触达** —— 新进线索一进来就发，积压的旧线索一条都不碰

用户要求（2026-11-18）
----------------------
「新加一个功能，新进线索自动触达，开启后，有新的符合要求的抖音号进来，
就立刻触达，其他的抖音号不管，这样既可以提高回复的概率，
也可以不用集中去触达。」

要盯住的
--------
1. **只处理刚进来的那一条** —— ``run(limit=1, only=[抖音号])``。
   积压的旧线索一条都不能被卷进来（这是用户说的「其他的抖音号不管」）。
2. **开关关着 → 一条都不发**
3. **最小间隔必须自己加** —— worker 的间隔是「两条之间」才 sleep 的，
   一轮只跑一条就不睡 → 不自己控速就会连着发、撞风控。
4. **跑不了的时候线索不丢** —— 冷却中/暂停中/正在跑批量轮询，
   都要留在队列里等条件好了再来。
5. **撞验证要停** —— 走和批量轮询同一套暂停逻辑，不能硬撞下一条。
6. **每小时封顶** —— 防弹幕潮把每日额度一次打光。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.config import get_config
from src.outreach.instant_trigger import InstantTrigger


class _FakeRunner:
    """假装是个 runner，只记录被调用了几次、传了什么参数"""

    def __init__(self, cooldown: int = 0, running: bool = False, sent_today: int = 0):
        self._cooldown = cooldown
        self._running = running
        self._sent_today = sent_today
        self.calls: list[dict] = []
        self.result = {"ok": True, "sent": 1, "dry": 0, "failed": 0,
                       "not_found": 0, "stopped": ""}

    @property
    def is_running(self) -> bool:
        return self._running

    def risk_cooldown_left(self) -> int:
        return self._cooldown

    def verify_cooldown_left(self) -> int:
        return 0

    def sent_today(self) -> int:
        return self._sent_today

    async def run(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return dict(self.result)


class _FakeScheduler:
    """假装是个调度器：只提供 blockers() 和 note_round_result()"""

    def __init__(self, blockers: list[str] | None = None):
        self._blockers = blockers or []
        self.noted: list[dict] = []

    def blockers(self, ignore_auto_mode: bool = False) -> list[str]:
        return list(self._blockers)

    def note_round_result(self, result: dict) -> None:
        self.noted.append(dict(result or {}))


@pytest.fixture
def cfg_instant(monkeypatch):
    """即时触达配置：开关开、干跑、最小间隔 0（测试不真等）、每小时上限放开"""
    cfg = get_config()
    o = cfg.outreach
    o.enabled = True
    o.auto_mode = False          # 即时触达**不依赖** auto_mode
    o.auto_instant = True
    o.auto_instant_dry_run = True
    o.auto_instant_gap_seconds = 0
    o.auto_instant_max_per_hour = 0   # 0 = 不封顶
    o.daily_limit = 100
    yield o
    o.auto_instant = False
    o.auto_instant_dry_run = True
    o.auto_instant_gap_seconds = 30
    o.auto_instant_max_per_hour = 20
    o.enabled = False


def _patch(monkeypatch, runner, scheduler):
    """把 trigger 依赖的 runner / scheduler 换成假的

    ⚠ 要 patch 的是 **instant_trigger 模块里绑定的那个名字**，
    不是 auto_scheduler 模块里的属性 ——
    instant_trigger 是在模块导入时 ``from .auto_scheduler import get_auto_scheduler``
    的，函数已经绑死在它自己的命名空间里，
    patch 源模块的属性对它**不生效**（patch 不到就等于调真的 runner → 真开浏览器 → 测试卡死）。
    """
    import src.outreach.instant_trigger as it_mod

    monkeypatch.setattr(it_mod, "get_outreach_runner", lambda: runner)
    monkeypatch.setattr(it_mod, "get_auto_scheduler", lambda: scheduler)


# ================================================================
# 入队
# ================================================================

def test_开关关着就不入队(cfg_instant):
    cfg_instant.auto_instant = False
    t = InstantTrigger()
    assert t.notify({"display_id": "abc123"}) is False
    assert t._queue.qsize() == 0


def test_没有抖音号就不入队(cfg_instant):
    """没抖音号就搜不到人，触达不了"""
    t = InstantTrigger()
    assert t.notify({"display_id": ""}) is False
    assert t.notify({}) is False
    assert t._queue.qsize() == 0


def test_入队成功(cfg_instant):
    t = InstantTrigger()
    assert t.notify({"display_id": "abc123"}) is True
    assert t._queue.qsize() == 1


def test_队列满了丢最老的保新的(cfg_instant, monkeypatch):
    """弹幕潮来的时候队列会满 —— 丢最老的（新的价值更高）"""
    import src.outreach.instant_trigger as it_mod

    monkeypatch.setattr(it_mod, "_QUEUE_MAX", 3)
    t = InstantTrigger()
    for i in range(5):
        t.notify({"display_id": f"user{i}"})
    assert t._queue.qsize() == 3
    # 剩下的是**最新**的三条
    left = []
    while not t._queue.empty():
        left.append(t._queue.get_nowait())
    assert left == ["user2", "user3", "user4"], f"丢的应该是最老的，实际剩 {left}"


def test_notify_new_lead_不抛异常(monkeypatch):
    """通知失败绝不能影响实时采集链路"""
    import src.outreach.instant_trigger as it_mod

    def boom(lead):
        raise RuntimeError("炸了")

    monkeypatch.setattr(it_mod, "get_instant_trigger", boom)
    assert it_mod.notify_new_lead({"display_id": "x"}) is False


# ================================================================
# 只触达新线索，不碰积压
# ================================================================

def test_只触达这一条(cfg_instant, monkeypatch):
    """核心：run(limit=1, only=[抖音号]) —— 积压的旧线索一条都不碰"""
    runner = _FakeRunner()
    sched = _FakeScheduler()
    _patch(monkeypatch, runner, sched)

    t = InstantTrigger()
    t.notify({"display_id": "newbie001"})
    asyncio.run(t._process("newbie001"))

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["limit"] == 1, "必须只跑一条"
    assert call["only"] == ["newbie001"], f"必须只处理这一个号，实际 {call.get('only')}"
    assert call["dry_run"] is True, "默认干跑"


def test_干跑开关生效(cfg_instant, monkeypatch):
    cfg_instant.auto_instant_dry_run = False
    runner = _FakeRunner()
    _patch(monkeypatch, runner, _FakeScheduler())
    t = InstantTrigger()
    asyncio.run(t._process("real001"))
    assert runner.calls[0]["dry_run"] is False


# ================================================================
# 限速
# ================================================================

def test_最小间隔自己加(cfg_instant):
    """worker 的间隔是两条之间才 sleep 的，单条触达不睡 → 必须自己控速"""
    cfg_instant.auto_instant_gap_seconds = 30
    t = InstantTrigger()
    assert t._gap_left() == 0.0          # 还没发过 → 不等

    t._last_sent_at = time.time()
    assert 25 <= t._gap_left() <= 30

    # 30 秒前发的 → 不用等了
    t._last_sent_at = time.time() - 31
    assert t._gap_left() == 0.0


def test_每小时封顶(cfg_instant):
    cfg_instant.auto_instant_max_per_hour = 3
    t = InstantTrigger()
    now = time.time()
    t._sent_times = [now - 10, now - 20, now - 30]
    assert t._hour_cap_reached() is True

    # 超出一小时的不算
    t._sent_times = [now - 3700, now - 3800]
    assert t._hour_cap_reached() is False


def test_零值不能被当成没设置(cfg_instant):
    """⚠ ``0 or 30`` 是 **30** —— 0 是合法取值，不能被默认值吃掉

    2026-11-18 实测踩到：
      ``float(o.auto_instant_gap_seconds or 30)``  把用户设的「0 秒」
      悄悄变成 30 秒，测试直接卡了 2 分半（每轮都在等 30 秒）。
    这条测试钉住：间隔 0 = 不限速；上限 0 = 不封顶。
    """
    cfg_instant.auto_instant_gap_seconds = 0
    cfg_instant.auto_instant_max_per_hour = 0

    t = InstantTrigger()
    t._last_sent_at = time.time()
    assert t._gap_left() == 0.0, "间隔设成 0 时不该再等"

    t._sent_times = [time.time()] * 10
    assert t._hour_cap_reached() is False, "上限设成 0 时不该封顶"

    # status() 也要如实显示 0，不能显示成 30 / 20
    st = t.status()
    assert st["gap_seconds"] == 0
    assert st["max_per_hour"] == 0


def test_封顶之后不再发(cfg_instant, monkeypatch):
    """每小时打满之后要停下，而不是继续打"""
    cfg_instant.auto_instant_max_per_hour = 2
    runner = _FakeRunner()
    _patch(monkeypatch, runner, _FakeScheduler())

    t = InstantTrigger()
    t._sent_times = [time.time(), time.time()]
    assert t._hour_cap_reached() is True


# ================================================================
# 跑不了的时候：等，不丢
# ================================================================

def test_冷却中就等着不丢线索(cfg_instant, monkeypatch):
    """限流冷却中 → 不能发，但线索要留在队列里"""
    runner = _FakeRunner(cooldown=30)
    sched = _FakeScheduler(blockers=["限流冷却中，还剩 30 分钟"])
    _patch(monkeypatch, runner, sched)

    t = InstantTrigger()
    t.notify({"display_id": "waiting001"})

    async def scenario():
        # 跑一小会儿就停（真实实现会一直等到条件满足）
        task = asyncio.create_task(t._process("waiting001"))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert runner.calls == [], "冷却中不该发"
    assert t.stats["waiting"] >= 1, "应该记下「在等」"


def test_每日上限到了就停(cfg_instant, monkeypatch):
    runner = _FakeRunner(sent_today=100)
    _patch(monkeypatch, runner, _FakeScheduler())

    t = InstantTrigger()

    async def scenario():
        task = asyncio.create_task(t._process("overlimit"))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert runner.calls == [], "发满了不该再发"


# ================================================================
# 撞验证 → 停
# ================================================================

def test_撞验证要交给调度器处理(cfg_instant, monkeypatch):
    """即时触达撞了验证，必须和批量轮询一样停下来等人，不能硬撞下一条"""
    runner = _FakeRunner()
    runner.result = {"ok": False, "sent": 0, "dry": 0, "failed": 0,
                     "stopped": "撞上抖音验证了", "stop_kind": "verify"}
    sched = _FakeScheduler()
    _patch(monkeypatch, runner, sched)

    t = InstantTrigger()
    asyncio.run(t._process("hitverify"))
    assert len(sched.noted) == 1, "撞验证的结果没交给调度器 → 不会暂停"


def test_正常完成也记账(cfg_instant, monkeypatch):
    runner = _FakeRunner()
    sched = _FakeScheduler()
    _patch(monkeypatch, runner, sched)

    t = InstantTrigger()
    asyncio.run(t._process("ok001"))
    assert t.stats["sent"] == 1
    assert t.last_lead == "ok001"


# ================================================================
# 没跑成 → 放回队列，别丢
# ================================================================

def test_没跑成要放回队列(cfg_instant, monkeypatch):
    """竞态：查「有没有在跑」和真跑之间隔着 await，批量轮询可能刚好插进来

    这时候 run() 返回 ok=False、这条根本没被处理 ——
    不放回去的话，新线索就此消失。
    """
    runner = _FakeRunner()
    runner.result = {"ok": False, "sent": 0, "dry": 0, "failed": 0,
                     "not_found": 0, "stopped": "", "error": "已有触达任务在跑"}
    _patch(monkeypatch, runner, _FakeScheduler())

    t = InstantTrigger()
    asyncio.run(t._process("race001"))
    assert t._queue.qsize() == 1, "没跑成的线索要放回队列"
    assert t._queue.get_nowait() == "race001"


def test_重试有上限(cfg_instant, monkeypatch):
    """一条卡死的线索不能把队列堵死

    ⚠ 放弃之后重试计数**不能**清零 ——
    清零的话下一轮又从 1 开始数，这条就会无限重试、队列永远清不掉。
    """
    runner = _FakeRunner()
    runner.result = {"ok": False, "sent": 0, "dry": 0, "failed": 0,
                     "not_found": 0, "stopped": "", "error": "一直不行"}
    _patch(monkeypatch, runner, _FakeScheduler())

    t = InstantTrigger()
    for _ in range(8):
        asyncio.run(t._process("stuck001"))
    assert t._queue.qsize() <= 3, f"重试次数没封顶，队列涨到 {t._queue.qsize()}"
    assert t._retries.get("stuck001", 0) > 3, "放弃后要把重试计数留住（否则无限重试）"


# ================================================================
# 和 auto_mode 的关系
# ================================================================

def test_即时触达不依赖_auto_mode(cfg_instant, monkeypatch):
    """auto_mode 关着（即时触达独立可用）"""
    cfg_instant.auto_mode = False
    runner = _FakeRunner()
    _patch(monkeypatch, runner, _FakeScheduler())

    t = InstantTrigger()
    asyncio.run(t._process("independent"))
    assert len(runner.calls) == 1, "auto_mode 关着时即时触达也该能跑"


def test_blockers_忽略_auto_mode(cfg_instant):
    """AutoScheduler.blockers(ignore_auto_mode=True) 不看 auto_mode 开关"""
    from src.outreach.auto_scheduler import AutoScheduler

    cfg_instant.auto_mode = False
    r = _FakeRunner()
    s = AutoScheduler(r)
    assert s.blockers() != [], "普通检查应该被 auto_mode 挡住"
    assert s.blockers(ignore_auto_mode=True) == [], "忽略 auto_mode 时不该被挡"
