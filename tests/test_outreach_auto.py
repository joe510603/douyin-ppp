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
    """把 outreach 配置改成可控状态"""
    cfg = get_config()
    o = cfg.outreach
    o.enabled = True
    o.auto_mode = True
    o.auto_dry_run = True
    o.auto_interval_minutes = 60
    o.auto_max_per_run = 5
    o.max_per_run = 10
    yield o
    o.enabled = False
    o.auto_mode = False


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
