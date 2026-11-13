"""触达吞吐的两个决定（2026-11-13 用户拍板）

用户反馈：「是不是转牛角尖了，一直在搜索搜不到的人，然后一直触发风控？
……搜不到应该就是真的搜不到了，继续去触达呀，今天才只给20多个发了小秀」

两个决定：
  1. **砍掉搜不到时的第 3 次搜索**（抖音号 → 昵称 → ~~抖音号再搜~~）
     实测当天第 3 次触发 2 次、0 次救回过人，每次赔上 20~40 秒。
  2. **直播检测并发限到 4 个**
     14 个账号并发各启一个 Chromium = 290% CPU，把触达搜索从 20 秒拖到 46 秒。

两个都要用测试钉住，否则改一次退一次。
"""

from __future__ import annotations

import asyncio

from src.detector.live_detector import LiveDetector
from src.outreach.douyin_api import Douyin


# ============================================================
# 1. 搜不到时不做第 3 次搜索
# ============================================================

def _douyin() -> Douyin:
    return Douyin(page=None, log=lambda m: None)


def test_搜不到只搜两次():
    """抖音号 → 昵称，两条路都失败就收工"""
    d = _douyin()
    calls: list[tuple] = []

    def _resolve_once(unique_id, by_nickname=False, want_dy=""):
        calls.append((unique_id, by_nickname))
        return None

    d._resolve_once = _resolve_once
    d._clean_nickname = lambda n: "昵称X"

    assert d.resolve("12345678", nickname="昵称X") is None
    assert len(calls) == 2, f"不该搜第三次：{calls}"
    assert calls[0] == ("12345678", False)
    assert calls[1] == ("昵称X", True)


def test_搜到人就不搜昵称():
    """第一条路成功 → 只搜一次"""
    d = _douyin()
    calls: list = []

    def _resolve_once(unique_id, by_nickname=False, want_dy=""):
        calls.append(unique_id)
        return {"sec_uid": "s1", "nickname": "n", "unique_id": unique_id}

    d._resolve_once = _resolve_once
    info = d.resolve("12345678", nickname="昵称X")
    assert info and info["unique_id"] == "12345678"
    assert len(calls) == 1, calls


def test_搜不到就没有昵称只搜一次():
    """没有昵称 → 只有一次机会"""
    d = _douyin()
    calls: list = []

    def _resolve_once(unique_id, by_nickname=False, want_dy=""):
        calls.append(unique_id)
        return None

    d._resolve_once = _resolve_once
    d._clean_nickname = lambda n: ""
    assert d.resolve("12345678", nickname=None) is None
    assert len(calls) == 1, calls


def test_重试开关还能打开():
    """默认关（用户拍板），但配置里还能打开 —— 换一次概率性失手的保险"""
    from src.config import get_config

    assert get_config().outreach.search_retry is False

    d = _douyin()
    calls: list = []
    d._resolve_once = lambda uid, by_nickname=False, want_dy="": (
        calls.append(uid) or None
    )
    d._clean_nickname = lambda n: "昵称X"
    d._search_retry_enabled = lambda: True

    d.resolve("12345678", nickname="昵称X")
    assert len(calls) == 3, calls


# ============================================================
# 2. 直播检测的并发上限
# ============================================================

class _假账号:
    def __init__(self, name):
        self.name = name
        self.sec_user_id = f"sec_{name}"


def test_检测并发不超过上限():
    """14 个账号各启一个浏览器 → 290% CPU，把触达搜索拖慢 2 倍多"""
    det = LiveDetector()
    live = {"now": 0, "peak": 0}

    async def _detect(acc):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        from src.detector.live_detector import LiveRoomInfo
        return acc.sec_user_id, LiveRoomInfo(status=False)

    det.detect = _detect
    accounts = [_假账号(f"a{i}") for i in range(14)]

    asyncio.run(det.batch_detect(accounts, max_concurrency=4))

    assert live["peak"] <= 4, f"并发峰值 {live['peak']}，超过上限了"


def test_检测默认并发也是限住的():
    """不传 max_concurrency → 走配置里的默认值（4）"""
    det = LiveDetector()
    live = {"now": 0, "peak": 0}

    async def _detect(acc):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        from src.detector.live_detector import LiveRoomInfo
        return acc.sec_user_id, LiveRoomInfo(status=False)

    det.detect = _detect
    accounts = [_假账号(f"a{i}") for i in range(14)]

    asyncio.run(det.batch_detect(accounts))

    assert live["peak"] <= 4, f"并发峰值 {live['peak']}，超过上限了"


def test_检测结果还是全的():
    """限并发不能少检测账号"""
    det = LiveDetector()

    async def _detect(acc):
        from src.detector.live_detector import LiveRoomInfo
        return LiveRoomInfo(status=True)

    det.detect = _detect
    accounts = [_假账号(f"a{i}") for i in range(14)]

    result = asyncio.run(det.batch_detect(accounts, max_concurrency=4))

    assert len(result) == 14, result
    assert all(v.status for v in result.values())
