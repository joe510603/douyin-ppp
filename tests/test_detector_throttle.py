"""检测请求必须**限速** —— 并发上限管不住总速率

背景（2026-11-15 实测）
----------------------
之前检测完全没节流：18 个账号 ÷ 60 秒 = 每 3.3 秒一个请求。
实测这个密度会触发抖音的**全局**风控 —— 连**全新 profile** 都放行不了
（页面全被降级成壳页：正文 533 字、含「用户不存在」、没有主播资料）。

⚠ 关键教训：**并发上限 ≠ 限速**
并发上限控制的是「同时开几个浏览器」，但一轮检测的**总请求量**没变。
限流看的是速率，所以光限并发没用 —— 必须有一个「两次请求之间至少隔 N 秒」
的全局闸门。

要钉住的：
1. 配置项存在且默认值合理（默认必须 > 0，否则等于没修）
2. 闸门真的会限速：N 个账号耗时至少 (N-1) × min_interval
3. min_interval = 0 时不节流（保持可配置，但别默认开）
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config


def test_默认就有节流间隔():
    """默认必须 > 0 —— 默认 0 等于没修。"""
    d = get_config().live_detection
    assert d.min_request_interval > 0, "默认必须限速，否则一上线就会被限流"


def test_闸门真的限速():
    """N 个账号并发跑，相邻请求间隔至少 min_interval。"""
    from src.detector.live_detector import LiveDetector
    from src.models.comment import MonitorAccount
    from src.detector.live_detector import LiveRoomInfo

    min_interval = 0.5
    n = 4
    det = LiveDetector()

    calls: list[float] = []

    async def fake_detect(account):
        calls.append(time.monotonic())
        return LiveRoomInfo(status=False)

    det.detect = fake_detect  # type: ignore[assignment]
    accounts = [
        MonitorAccount(name=f"a{i}", sec_user_id=f"MS4wLjABAAAA{i}",
                       room_id="", websocket_url="")
        for i in range(n)
    ]
    asyncio.run(det.batch_detect(accounts, min_request_interval=min_interval))

    assert len(calls) == n
    gaps = [calls[i + 1] - calls[i] for i in range(len(calls) - 1)]
    for g in gaps:
        assert g >= min_interval * 0.8, f"两次请求间隔 {g:.2f}s 小于 {min_interval}s"


def test_节流关闭时不等待():
    """min_request_interval = 0 → 不节流（保持可配置性）。"""
    from src.detector.live_detector import LiveDetector
    from src.models.comment import MonitorAccount
    from src.detector.live_detector import LiveRoomInfo

    det = LiveDetector()
    calls: list[float] = []

    async def fake_detect(account):
        calls.append(time.monotonic())
        return LiveRoomInfo(status=False)

    det.detect = fake_detect  # type: ignore[assignment]
    accounts = [
        MonitorAccount(name=f"a{i}", sec_user_id=f"MS4wLjABAAAA{i}",
                       room_id="", websocket_url="")
        for i in range(5)
    ]
    asyncio.run(det.batch_detect(accounts, min_request_interval=0.0))

    gaps = [calls[i + 1] - calls[i] for i in range(len(calls) - 1)]
    assert gaps, "应该记录到请求时刻"
    assert max(gaps) < 0.3, f"关闭节流时不该有间隔（实测最大间隔 {max(gaps):.2f}s）"
