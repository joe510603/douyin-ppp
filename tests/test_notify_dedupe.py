"""持续性故障的告警去重 —— 别把同一个故障刷成 N 张卡

背景
----
2026-11-15 实测踩到：检测器「全盲」是**持续状态**。
旧逻辑是「连续 2 轮看不见 → 发一张红卡」，而盲态每 2 轮就又满足一次
→ 实测在 15:06 / 15:10 / 15:15 连发 3 张完全一样的红卡片。

刷屏的真正代价：
1. 真正的新故障被淹没（用户被红卡训练成「又是这个，忽略」）；
2. 程序会被当成「乱报」，以后真出事也没人信。

要钉住的：
1. 第一次 → 该发；
2. 冷却期内同一类故障 → 不发；
3. 超过冷却期 → 又该发（故障可能一直没修好，得提醒）；
4. 故障**恢复** → 冷却解除（下次再坏要能立刻报）；
5. 换了故障类型 → 算新故障，可以报（别把两件事混成一件）。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.utils.notify_dedupe import NotifyDedupe


class FakeClock:
    """可控的假时钟（别在测试里真的 sleep）"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_第一次该发():
    clock = FakeClock()
    d = NotifyDedupe(cooldown_seconds=1800, clock=clock)
    assert d.should_notify("blind") is True


def test_发过一次之后冷却期内不再发():
    clock = FakeClock()
    d = NotifyDedupe(cooldown_seconds=1800, clock=clock)
    d.mark_sent("blind")
    assert d.should_notify("blind") is False


def test_冷却期过了又能发():
    """故障一直没修好 → 得再提醒一次，不能一次报完就永远沉默。"""
    clock = FakeClock()
    d = NotifyDedupe(cooldown_seconds=1800, clock=clock)
    d.mark_sent("blind")
    clock.advance(1800)
    assert d.should_notify("blind") is True


def test_冷却期内差一点点也不发():
    """1799 秒时还是不该发（边界别写错）。"""
    clock = FakeClock()
    d = NotifyDedupe(cooldown_seconds=1800, clock=clock)
    d.mark_sent("blind")
    clock.advance(1799)
    assert d.should_notify("blind") is False
    clock.advance(1)
    assert d.should_notify("blind") is True


def test_恢复了就解除冷却():
    """故障恢复 → 万一马上又坏了，要能立刻再报，不能还卡在冷却里。"""
    clock = FakeClock()
    d = NotifyDedupe(cooldown_seconds=1800, clock=clock)
    d.mark_sent("blind")
    assert d.should_notify("blind") is False
    d.on_recovered()
    assert d.should_notify("blind") is True


def test_不同故障类型互不影响():
    """「全盲」和「拿不到 room_id」是两件事，别把一件的冷却套到另一件上。"""
    clock = FakeClock()
    d = NotifyDedupe(cooldown_seconds=1800, clock=clock)
    d.mark_sent("blind")
    assert d.should_notify("blind") is False
    assert d.should_notify("no_room") is True, "新故障类型应该能立刻报"


def test_seconds_since_没发过返回_none():
    d = NotifyDedupe(cooldown_seconds=1800, clock=FakeClock())
    assert d.seconds_since("blind") is None
