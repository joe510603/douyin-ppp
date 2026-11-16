"""检测用的 profile 副本 —— 限流是**按目录**记的，用完必须重建

背景（2026-11-15 实测）
----------------------
检测器靠浏览器打开门店主页判断是否开播。但实测发现：

1. 同一 profile 目录，**第 1 个账号正常渲染，第 2 个开始就被降级**成
   533 字的壳页（正文含「用户不存在」、没有主播资料）——
   这个壳页里没有直播间入口 → 检测器报「未检测到直播」→ 采集器不起 →
   **整场直播的弹幕全丢**，而日志上一片正常。
2. **等 75 秒也不恢复** → 不是时间窗口问题。
3. **删掉目录重建 → 立刻恢复正常**。
4. 但「覆盖复制」（只把那 6 个种子文件复制过去）**不恢复** →
   Edge 运行时在 profile 里写下的痕迹必须一起清掉。

所以 slot 用完必须**整个删掉重建**。

要钉住的：
1. slot 用完会被重建（目录真的被删了再重建）
2. 重建后「占用中」标记要清掉（否则这个 slot 永远分配不出去）
3. 并发时不会把同一个 slot 分给两个线程（Chromium 会报 profile 被锁）
4. 没登录态时重建要失败并如实返回（不能假装成功）
"""

from __future__ import annotations

import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.utils import profile_seed as ps
from src.utils.profile_seed import SeededProfiles, seed_profile


class _FakeSeeded(SeededProfiles):
    """不碰真 profile 的假实现（只在内存里记账）。"""

    def __init__(self, slots=2):
        super().__init__(src="data/outreach_profile", base_dir="data/detect_profiles", slots=slots)
        self.rebuilt: list[int] = []
        self.ok = True

    def ensure(self, force: bool = False) -> bool:
        return self.ok

    def reset_slot(self, index: int) -> bool:
        self.rebuilt.append(index)
        self._used_at[index] = 0.0
        return self.ok


def test_取号会标记占用():
    s = _FakeSeeded(slots=2)
    idx = s.next_index()
    assert idx is not None
    assert idx in s._in_use, "取到的 slot 必须标记成占用中"


def test_并发取号不会给同一个slot():
    """两个线程同时取号，不能拿到同一个 slot（否则 Chromium 报 profile 被锁）。"""
    s = _FakeSeeded(slots=4)
    got: list[int] = []
    lock = threading.Lock()

    def worker():
        i = s.next_index()
        if i is not None:
            with lock:
                got.append(i)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 4, "4 个线程应该都拿到 slot"
    assert len(set(got)) == 4, f"slot 不能重复分配: {got}"


def test_slot全占满时返回None():
    """没有空闲 slot 时宁可返回 None，也不要硬塞给别人。"""
    s = _FakeSeeded(slots=1)
    assert s.next_index() == 0
    assert s.next_index() is None


def test_回收会重建并释放占用():
    """用完 recycle_slot → 目录重建 + 占用标记清掉。"""
    s = _FakeSeeded(slots=2)
    idx = s.next_index()
    assert idx in s._in_use
    s.recycle_slot(idx)
    assert idx in s.rebuilt, "用完必须重建（限流按目录记）"
    assert idx not in s._in_use, "重建完要释放占用，否则这个 slot 永远分配不出去"


def test_回收后同一个slot能再被取到():
    s = _FakeSeeded(slots=1)
    idx = s.next_index()
    s.recycle_slot(idx)
    assert s.next_index() is not None, "回收后应该能再取到"


def test_重建失败也要释放占用():
    """重建失败（比如源 profile 没了）也不能把 slot 永久卡死。"""
    s = _FakeSeeded(slots=1)
    idx = s.next_index()
    s.ok = False
    s.recycle_slot(idx)
    assert idx not in s._in_use, "重建失败也必须释放占用"


def test_release会释放占用():
    s = _FakeSeeded(slots=1)
    idx = s.next_index()
    s.release(idx)
    assert idx not in s._in_use


def test_seed_profile_源目录不存在时返回False():
    """源 profile 不在 → 如实返回 False，不能假装成功（否则以为有登录态）。"""
    assert seed_profile("data/__不存在的profile__", "data/detect_profiles/__test_out__") is False
