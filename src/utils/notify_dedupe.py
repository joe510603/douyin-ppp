"""持续性故障的告警去重（同一个故障别反复刷屏）

问题
----
2026-11-15 实测踩到：检测器「全盲」是**持续状态**，每 2 轮就满足一次
「连续 2 轮看不见」的告警条件 → 实测在 15:06 / 15:10 / 15:15 连发 3 张红卡片。

刷屏的代价不只是吵：
1. 真正的新故障会被淹没（用户习惯了红卡 → 就不看了）；
2. 同一个故障反复喊，会让人以为「程序在乱报」→ 以后真的出事也不信了。

所以：**同一个持续故障，只在第一次报；之后进入冷却，冷却期内不再重复报。**
但有两种情况必须能立刻再报：
- 故障**恢复**了 → 冷却解除（万一下次又坏，要能立刻报出来）；
- 故障**变了**（换了一种故障类型）→ 算新故障，可以报。

用法
----
    d = NotifyDedupe(cooldown_seconds=1800)
    if d.should_notify(kind="blind"):        # True = 该发
        d.mark_sent(kind="blind")
    d.on_recovered()                          # 恢复 → 冷却解除
"""

from __future__ import annotations

import time
from typing import Optional


class NotifyDedupe:
    """按「故障类型」去重告警。

    Args:
        cooldown_seconds: 同一类故障两次告警之间至少隔多久（秒）。
        clock: 时间源（测试时注入假时钟）。
    """

    def __init__(self, cooldown_seconds: int = 1800, clock=time.time):
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self._clock = clock
        # kind -> 上次发出告警的时间戳
        self._last_sent: dict[str, float] = {}

    def should_notify(self, kind: str = "default") -> bool:
        """该不该发这条告警。

        同类故障在冷却期内 → False（别刷屏）；
        从没报过 / 已过冷却 / 换了故障类型 → True。
        """
        last = self._last_sent.get(kind)
        if last is None:
            return True
        return (self._clock() - last) >= self.cooldown_seconds

    def mark_sent(self, kind: str = "default") -> None:
        """记下「刚刚发过了」。"""
        self._last_sent[kind] = self._clock()

    def on_recovered(self) -> None:
        """故障恢复了 → 解除所有冷却。

        下次再坏的时候要能**立刻**报出来，不能还卡在冷却里。
        """
        self._last_sent.clear()

    def seconds_since(self, kind: str = "default") -> Optional[float]:
        """距离上次发过这条告警过了多少秒（没发过 = None）。查日志用。"""
        last = self._last_sent.get(kind)
        if last is None:
            return None
        return self._clock() - last
