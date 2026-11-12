"""自动触达调度器

用户要求：「我想要能自动取做触达，不用我在手动跑了，有可以设置自动」

要盯住的：
1. 开关关着 → 绝不自动跑
2. 触达功能没启用 → 不跑
3. 正在跑 → 不重复触发
4. 限流冷却中 → 不跑（硬撞就是找死）
5. 间隔没到 → 不跑
6. 自动模式默认**干跑**（安全阀）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from src.config import get_config
from src.outreach.auto_scheduler import AutoScheduler


class _FakeRunner:
    """假装是个 runner，只记录被调用了几次、传了什么参数"""

    def __init__(self, cooldown: int = 0, running: bool = False):
        self._cooldown = cooldown
        self._running = running
        self.calls: list[dict] = []
        self.result = {"ok": True, "sent": 2, "dry": 0, "failed": 0}

    @property
    def is_running(self) -> bool:
        return self._running

    def risk_cooldown_left(self) -> int:
        return self._cooldown

    async def run(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return dict(self.result)


@pytest.fixture
def cfg_outreach(monkeypatch):
    """把 outreach 配置改成可控状态

    默认走**老的定时模式**（auto_continuous=False）——
    连续模式的行为单独一组用例测，两种模式的间隔语义完全不同，
    混在一起测会互相掩盖。
    """
    cfg = get_config()
    o = cfg.outreach
    o.enabled = True
    o.auto_mode = True
    o.auto_dry_run = True
    o.auto_interval_minutes = 60
    o.auto_max_per_run = 5
    o.max_per_run = 10
    o.auto_continuous = False
    o.auto_round_gap_seconds = 60
    o.auto_idle_poll_seconds = 30
    yield o
    o.enabled = False
    o.auto_mode = False


@pytest.fixture
def cfg_continuous(cfg_outreach):
    """连续模式：轮完接下一轮，新线索马上跟上"""
    cfg_outreach.auto_continuous = True
    return cfg_outreach


# ============================================================
# 什么时候该跑 / 不该跑
# ============================================================

def test_开关关着就不跑(cfg_outreach):
    cfg_outreach.auto_mode = False
    r = _FakeRunner()
    s = AutoScheduler(r)
    assert asyncio.run(s.maybe_run()) is False
    assert r.calls == []


def test_触达没启用就不跑(cfg_outreach):
    cfg_outreach.enabled = False
    r = _FakeRunner()
    s = AutoScheduler(r)
    assert asyncio.run(s.maybe_run()) is False
    assert r.calls == []


def test_正在跑就不重复触发(cfg_outreach):
    r = _FakeRunner(running=True)
    s = AutoScheduler(r)
    assert asyncio.run(s.maybe_run()) is False
    assert r.calls == [], "上一轮还在跑，不能再起一轮"


def test_限流冷却中就不跑(cfg_outreach):
    r = _FakeRunner(cooldown=25)
    s = AutoScheduler(r)
    assert asyncio.run(s.maybe_run()) is False
    assert r.calls == []
    assert "冷却" in s.last_error


def test_间隔没到就不跑(cfg_outreach):
    cfg_outreach.auto_interval_minutes = 60
    r = _FakeRunner()
    s = AutoScheduler(r)
    # 先跑一轮
    assert asyncio.run(s.maybe_run()) is True
    assert len(r.calls) == 1
    # 立刻再跑 → 间隔没到
    assert asyncio.run(s.maybe_run()) is False
    assert len(r.calls) == 1


def test_间隔到了才跑(cfg_outreach):
    cfg_outreach.auto_interval_minutes = 60
    r = _FakeRunner()
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    # 假装上次是 61 分钟前
    s.last_run_at = datetime.now() - timedelta(minutes=61)
    assert asyncio.run(s.maybe_run()) is True
    assert len(r.calls) == 2


def test_手动触发可以跳过间隔(cfg_outreach):
    """刚打开开关就该马上跑一轮，不能让人干等一个周期"""
    cfg_outreach.auto_interval_minutes = 60
    r = _FakeRunner()
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert len(r.calls) == 1
    # 手动触发
    s.trigger_now()
    assert asyncio.run(s.maybe_run()) is True
    assert len(r.calls) == 2


# ============================================================
# 安全阀
# ============================================================

def test_自动模式默认是干跑(cfg_outreach):
    """无人值守时默认不能真发"""
    cfg_outreach.auto_dry_run = True
    r = _FakeRunner()
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert r.calls[0]["dry_run"] is True


def test_可以显式开成真发(cfg_outreach):
    cfg_outreach.auto_dry_run = False
    r = _FakeRunner()
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert r.calls[0]["dry_run"] is False


def test_每轮条数用的是自动模式的设置(cfg_outreach):
    cfg_outreach.auto_max_per_run = 3
    cfg_outreach.max_per_run = 10
    r = _FakeRunner()
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert r.calls[0]["limit"] == 3


def test_冷却优先于手动触发(cfg_outreach):
    """手动触发也不能绕过冷却——真被限流了还硬撞就是找死"""
    r = _FakeRunner(cooldown=30)
    s = AutoScheduler(r)
    s.trigger_now()
    assert asyncio.run(s.maybe_run()) is False
    assert r.calls == []


def test_跑的时候抛异常不会把调度器搞挂(cfg_outreach):
    class _Boom(_FakeRunner):
        async def run(self, **kwargs):
            raise RuntimeError("炸了")

    s = AutoScheduler(_Boom())
    assert asyncio.run(s.maybe_run()) is False
    assert "炸了" in s.last_error


# ============================================================
# 状态
# ============================================================

def test_状态能反映开关(cfg_outreach):
    cfg_outreach.auto_mode = True
    cfg_outreach.auto_dry_run = True
    s = AutoScheduler(_FakeRunner())
    st = s.status()
    assert st["enabled"] is True
    assert st["dry_run"] is True
    assert st["interval_minutes"] == 60


def test_没开启时状态是未开启(cfg_outreach):
    cfg_outreach.auto_mode = False
    s = AutoScheduler(_FakeRunner())
    st = s.status()
    assert st["configured"] is False
    assert st["enabled"] is False


# ============================================================
# 连续模式：打开开关就一直自己跑
# ============================================================

def test_连续模式不等固定间隔(cfg_continuous):
    """连续模式的核心：轮完接下一轮，不等那个固定周期

    用户要求「打开自动触达，就会一直自己去触达了」。
    老逻辑要等 auto_interval_minutes（这里 60 分钟），
    连续模式只等换手间隙（60 秒）。
    """
    cfg_continuous.auto_interval_minutes = 60
    cfg_continuous.auto_round_gap_seconds = 60
    r = _FakeRunner()
    s = AutoScheduler(r)
    assert asyncio.run(s.maybe_run()) is True
    # 假装上一轮是 90 秒前（换手间隙已过，但离 60 分钟还早得很）
    s.last_run_at = datetime.now() - timedelta(seconds=90)
    assert asyncio.run(s.maybe_run()) is True
    assert len(r.calls) == 2


def test_连续模式换手间隙没过就不跑(cfg_continuous):
    """刚跑完立刻又来 → 换手间隙没过，不该马上再开一轮"""
    cfg_continuous.auto_round_gap_seconds = 60
    r = _FakeRunner()
    s = AutoScheduler(r)
    assert asyncio.run(s.maybe_run()) is True
    assert asyncio.run(s.maybe_run()) is False
    assert len(r.calls) == 1


def test_连续模式队列空了就早点看新线索(cfg_continuous):
    """队列空的时候要勤看——新进的线索才能马上跟上"""
    cfg_continuous.auto_round_gap_seconds = 600   # 有活干时等很久
    cfg_continuous.auto_idle_poll_seconds = 30    # 闲着时勤看
    r = _FakeRunner()
    r.result = {"ok": True, "sent": 0, "dry": 0, "failed": 0, "total": 0}
    s = AutoScheduler(r)
    assert asyncio.run(s.maybe_run()) is True
    # 空跑之后，40 秒后应该又去看一眼（不必等 600 秒）
    s.last_run_at = datetime.now() - timedelta(seconds=40)
    assert asyncio.run(s.maybe_run()) is True
    assert len(r.calls) == 2


def test_连续模式状态里有连续标记(cfg_continuous):
    s = AutoScheduler(_FakeRunner())
    st = s.status()
    assert st["continuous"] is True
    assert st["paused"] is False


# ============================================================
# 撞验证：停下来叫人，不硬撞
# ============================================================

def test_撞验证就暂停不再自动跑(cfg_continuous):
    """撞了「本人操作验证」必须停下来等人，不能自己又撞一次"""
    r = _FakeRunner()
    r.result = {
        "ok": True, "sent": 0, "dry": 0, "failed": 1, "total": 1,
        "stopped": "触发风控：抖音要求验证本人操作才能发评论",
        "stop_kind": "verify",
    }
    s = AutoScheduler(r)
    assert asyncio.run(s.maybe_run()) is True
    assert s.paused is True
    assert "验证" in s.last_result.get("stopped", "") or s.paused
    # 暂停后不该再自动跑
    s.last_run_at = datetime.now() - timedelta(minutes=10)
    assert asyncio.run(s.maybe_run()) is False
    assert len(r.calls) == 1, "撞验证后不该自己又跑一轮"


def test_撞验证后手动解除能恢复(cfg_continuous):
    r = _FakeRunner()
    r.result = {
        "ok": True, "sent": 0, "dry": 0, "failed": 1, "total": 1,
        "stopped": "触发风控：抖音要求验证本人操作",
        "stop_kind": "verify",
    }
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert s.paused is True
    assert s.clear_pause("网页上点了解除") is True
    assert s.paused is False
    # 解除后马上就能跑
    assert asyncio.run(s.maybe_run()) is True
    assert len(r.calls) == 2


def test_撞验证后手动触发会解除暂停(cfg_continuous):
    """手动触发 = 人已经在了 → 顺手把暂停解掉"""
    r = _FakeRunner()
    r.result = {
        "ok": True, "sent": 0, "dry": 0, "failed": 1, "total": 1,
        "stopped": "触发风控：抖音要求验证本人操作",
        "stop_kind": "verify",
    }
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert s.paused is True
    s.trigger_now()
    assert s.paused is False
    assert asyncio.run(s.maybe_run()) is True
    assert len(r.calls) == 2


def test_登录失效也会暂停(cfg_continuous):
    r = _FakeRunner()
    r.result = {
        "ok": True, "sent": 0, "dry": 0, "failed": 1, "total": 1,
        "stopped": "登录失效：没有 sessionid",
        "stop_kind": "login",
    }
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert s.paused is True


def test_今日已发满会暂停到明天(cfg_continuous):
    """发满了就别每分钟空跑一轮，暂停到明天自动恢复"""
    r = _FakeRunner()
    r.result = {
        "ok": True, "sent": 0, "dry": 0, "failed": 0, "total": 0,
        "stopped": "今天已发 100 条，达到每日上限 100，收工",
        "daily_limit_reached": True,
    }
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert s.paused is True
    st = s.status()
    assert st["paused"] is True
    assert "明天" in st["paused_reason"]


def test_限流不进暂停状态(cfg_continuous):
    """限流由冷却计时管，冷却完了自动接着跑，不该永久暂停"""
    r = _FakeRunner()
    r.result = {
        "ok": True, "sent": 0, "dry": 0, "failed": 1, "total": 1,
        "stopped": "触发风控：页面提示：操作频繁",
        "stop_kind": "limit",
    }
    s = AutoScheduler(r)
    asyncio.run(s.maybe_run())
    assert s.paused is False
