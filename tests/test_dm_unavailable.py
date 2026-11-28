"""「私信停了，触达不要停」—— 私信通道的临时状态不能判死线索

用户 2026-11-27：「私信可以设置上限，但是私信停了，触达不要停」。

事故形态
--------
私信额度用完 / 私信通道被平台限流 / 私信输入框没渲染 ——
这些都是**我们这边**的临时状态，跟「这个人能不能被联系」毫无关系。

但代码以前一律抛 ``NoWorks`` → 线索被判成 **skipped 终态** →
**这个人从此再也不会被留言**（私信本来只是兜底通道，
没作品的人还有机会等他以后发作品；判死就是永久失去）。

更要命的是：私信一停，掉进私信兜底的那批线索每轮都被重搜一遍
（排序位置没变 → 永远排第一个）→ 整轮在原地打转，
**留言也跟着发不出去** —— 正好违背用户那句话。

盯住的：
1. 私信额度用完 / 通道暂停 / 发送失败(环境) / 被平台拦 → 退回 pending，**不判死**
2. 对方关了私信（对方侧、永久）→ 仍判跳过（这是唯一该判死的）
3. dm_daily_limit=0（私信被关掉）→ 判跳过（没渠道了），
   **不能**变成永远退不干净的队列垃圾
4. 退回队列的线索记一条 dm_wait 冷却标记 → 队列先晾它 30 分钟，
   别让私信一停就把整轮拖死
5. dm_wait 记录不占私信额度、也不算「已经打扰过这个人」
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import (  # noqa: E402
    DM_FAIL_BLOCKED, DM_FAIL_ENV, DM_FAIL_TARGET, DmUnavailable, NoWorks,
)
from src.outreach.worker import OutreachWorker  # noqa: E402


# ============================================================
# 假对象
# ============================================================

class _FakeDouyin:
    """假装是个 Douyin

    ``works_status='empty'`` = 接口可信地说这个人没有作品 → 走私信兜底。
    """

    def __init__(self, dm_ok: bool = True, msg: str = "", kind: str = "",
                 body: str = "", is_private: bool = False):
        self.dm_ok = dm_ok
        self.msg = msg
        self.kind = kind
        self.body = body
        self.is_private = is_private
        self.last_works_fetch = {"status": "empty", "detail": ""}
        self.sent_dm: list[tuple[str, str]] = []
        self.calls = 0

    def fetch_works(self, sec_uid: str, limit: int = 20) -> list:
        return []

    def resolve_by_sec_uid(self, sec_uid: str) -> dict:
        return {"sec_uid": sec_uid, "nickname": "昵称1", "aweme_count": 0,
                "signature": ""}

    def resolve(self, unique_id: str, nickname: str | None = None) -> dict:
        return self.resolve_by_sec_uid("MS4wLjABAAAAtest1")

    def send_dm(self, sec_uid: str, text: str) -> dict:
        self.calls += 1
        if self.dm_ok:
            self.sent_dm.append((sec_uid, text))
            return {"ok": True, "msg": "已发送", "is_private": self.is_private}
        return {"ok": False, "msg": self.msg or "私信失败", "kind": self.kind,
                "is_private": self.is_private}

    def detect_private_account(self) -> bool:
        return self.is_private

    def _body_text(self) -> str:
        return self.body

    def check_risk(self) -> None:
        return None

    def cookies_ok(self) -> bool:
        return True

    def set_control_account(self, sec_uid: str) -> None:
        """对照账号（2026-11-27）：真实 Douyin 用它分清「他没了」和「我们瞎了」。

        假对象不用真做对照，记下来就行（省得每个测试都去开一个对照页）。
        """
        self.control_sec_uid = sec_uid


def _建库(tmp_path, n: int = 1):
    """建测试库

    只建 leads 表；``outreach_records`` 交给 OutreachStore 自己建
    （用真实的表结构，才测得到真实的 dm_wait 冷却逻辑）。
    """
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE leads ("
        " id INTEGER PRIMARY KEY, lead_key TEXT, user_id TEXT, display_id TEXT, "
        " nickname TEXT, avatar TEXT, room_id TEXT, monitor_name TEXT, "
        " live_room TEXT, intent_level TEXT, intent_score INTEGER, "
        " intent_reason TEXT, car_model TEXT, contact_type TEXT, "
        " contact_value TEXT, evidence TEXT, evidence_count INTEGER, "
        " comment_count INTEGER, status TEXT DEFAULT 'new', pushed_at TEXT, "
        " outreach_status TEXT DEFAULT 'pending', outreach_at TEXT, "
        " outreach_note TEXT, created_at TEXT DEFAULT '', updated_at TEXT, "
        " source TEXT, bitable_record_id TEXT, bitable_synced_status TEXT, "
        " store TEXT, last_hit_at TEXT, sec_uid TEXT, "
        " not_found_count INTEGER DEFAULT 0, no_works INTEGER DEFAULT 0)"
    )
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO leads (id, display_id, nickname, sec_uid, store, "
            " outreach_status, status, no_works, last_hit_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 'new', 0, ?)",
            (i, f"dy_{i}", f"昵称{i}", f"MS4wLjABAAAAtest{i}", "测试门店",
             "2026-11-27 10:00:00"),
        )
    conn.commit()
    conn.close()
    return db


def _worker(tmp_path, n: int = 1, **cfg_over):
    return _worker_on(_建库(tmp_path, n), **cfg_over)


def _worker_on(db, **cfg_over):
    """在**已有的库**上开一个 worker

    跨轮的状态（私信暂停、被拦计数）都存在 DB 里，
    所以测「下一轮能不能读到」必须复用同一个库，不能重建。
    """
    cfg = get_config().model_copy(deep=True)
    cfg.outreach.dm_fallback = True
    cfg.outreach.dm_daily_limit = 10
    cfg.outreach.dm_fail_threshold = 2
    cfg.outreach.dm_pause_minutes = 60
    cfg.outreach.adaptive_delay = False
    cfg.outreach.min_delay = 1
    cfg.outreach.max_delay = 1
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    logs: list[str] = []
    w = OutreachWorker(cfg, str(db), log=logs.append)
    w._dry = False
    return w, logs


def _lead(**kw) -> dict:
    lead = {
        "id": 1, "display_id": "dy_1", "nickname": "昵称1",
        "sec_uid": "MS4wLjABAAAAtest1", "store": "测试门店",
        "car_model": "小米SU7", "contact_type": "", "contact_value": "",
        "evidence": "[]", "monitor_name": "测试直播间",
        "bitable_record_id": "", "intent_level": "high", "intent_score": 90,
        "no_works": 0,
    }
    lead.update(kw)
    return lead


# ============================================================
# 1. 私信额度用完 → 退回队列，不判死
# ============================================================

def test_私信额度用完是临时状态不是终态(tmp_path):
    """核心：额度用完是我们这边的状态，不能把线索判死"""
    w, _ = _worker(tmp_path, dm_daily_limit=1)
    try:
        w.st.add_touch(lead_id=1, display_id="dy_used", status="sent", channel="dm")
        assert w.st.dm_sent_today() == 1
        dy = _FakeDouyin()
        with pytest.raises(DmUnavailable) as ei:
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert "额度" in str(ei.value), str(ei.value)
        assert dy.calls == 0, "额度用完就不该再发私信"
        # ⚠ 关键：不能是 NoWorks（那是「跳过」终态）
        assert not isinstance(ei.value, NoWorks), "额度用完不能判死"
    finally:
        w.st.close()


def test_私信通道暂停中是临时状态不是终态(tmp_path):
    """平台把私信限了 → 冷却期间退回队列，不判死"""
    w, _ = _worker(tmp_path)
    try:
        w._dm_paused_until = time.time() + 3600
        dy = _FakeDouyin()
        with pytest.raises(DmUnavailable) as ei:
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert "暂停" in str(ei.value), str(ei.value)
        assert not isinstance(ei.value, NoWorks), "暂停中不能判死"
        assert dy.calls == 0, "暂停期间不该再撞私信"
    finally:
        w.st.close()


# ============================================================
# 2. 发送失败：分清是谁的问题
# ============================================================

def test_对方关了私信才判跳过(tmp_path):
    """对方侧、永久 → 跳过是对的（这条永远发不出私信）"""
    w, _ = _worker(tmp_path)
    try:
        dy = _FakeDouyin(dm_ok=False, msg="主页上没找到可见的「私信」按钮",
                         kind=DM_FAIL_TARGET)
        with pytest.raises(NoWorks):
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
    finally:
        w.st.close()


def test_我们环境的问题退回队列(tmp_path):
    """输入框没渲染 → 我们自己的问题，临时 → 退回队列"""
    w, _ = _worker(tmp_path)
    try:
        dy = _FakeDouyin(dm_ok=False, msg="私信输入框没渲染出来", kind=DM_FAIL_ENV)
        with pytest.raises(DmUnavailable):
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
    finally:
        w.st.close()


def test_平台拦了退回队列(tmp_path):
    """平台限流 → 临时 → 退回队列（不是判死）"""
    w, _ = _worker(tmp_path)
    try:
        dy = _FakeDouyin(dm_ok=False, msg="私信被拦：操作频繁", kind=DM_FAIL_BLOCKED)
        with pytest.raises(DmUnavailable):
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
    finally:
        w.st.close()


def test_私信失败不占去重额度(tmp_path):
    """失败的不是「已经打扰过这个人」→ 30 天内也该还能再试"""
    w, _ = _worker(tmp_path)
    try:
        dy = _FakeDouyin(dm_ok=False, msg="私信被拦：操作频繁", kind=DM_FAIL_BLOCKED)
        with pytest.raises(DmUnavailable):
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert not w.st.recent_touch_days("dy_1", 30), \
            "失败的私信不算打扰过这个人"
    finally:
        w.st.close()


# ============================================================
# 3. 私信被关掉（dm_daily_limit=0）→ 跳过，不是无限退回
# ============================================================

def test_私信关掉时判跳过而不是无限退回(tmp_path):
    """dm_daily_limit=0 = 不发私信

    这时「没作品 + 没私信渠道」= 真的没渠道了 → 跳过是对的。
    要是也退回 pending，这条线索就会永远在队列里打转
    （每轮都挑中它、每轮都退回来），把队列塞满。
    """
    w, _ = _worker(tmp_path, dm_daily_limit=0)
    try:
        dy = _FakeDouyin()
        with pytest.raises(NoWorks) as ei:
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert "私信已关闭" in str(ei.value), str(ei.value)
        assert dy.calls == 0
    finally:
        w.st.close()


# ============================================================
# 4. 主循环：DmUnavailable → 退回 pending + 记冷却标记
# ============================================================

def _跑一轮(w, dy, limit: int = 1, monkeypatch=None):
    @contextmanager
    def _fake_page_ctx(profile_dir, headless, session):
        yield object()

    if monkeypatch is not None:
        monkeypatch.setattr(w, "_page_ctx", _fake_page_ctx)
        monkeypatch.setattr("src.outreach.worker.Douyin",
                            lambda page, shot_dir=None, log=print: dy)
    return w.run(limit=limit)


def test_主循环里私信停了线索退回pending(tmp_path, monkeypatch):
    """最高层的回归护栏：私信停了，这条线索不能变成 skipped"""
    w, logs = _worker(tmp_path, dm_daily_limit=1)
    try:
        w.st.add_touch(lead_id=1, display_id="dy_used", status="sent", channel="dm")
        res = _跑一轮(w, _FakeDouyin(), limit=1, monkeypatch=monkeypatch)
        row = w.st.get_lead(1)
        assert row["outreach_status"] == "pending", \
            f"私信停了不该判死，实际 {row['outreach_status']}（res={res}）"
    finally:
        w.st.close()


def test_退回队列会记冷却标记(tmp_path, monkeypatch):
    """记一笔 dm_wait → 队列先晾它 30 分钟，别每轮都重搜"""
    w, logs = _worker(tmp_path, dm_daily_limit=1)
    try:
        w.st.add_touch(lead_id=1, display_id="dy_used", status="sent", channel="dm")
        _跑一轮(w, _FakeDouyin(), limit=1, monkeypatch=monkeypatch)
        rows = w.st._conn.execute(
            "SELECT status, channel FROM outreach_records WHERE status='dm_wait'"
        ).fetchall()
        assert len(rows) == 1, f"该记一条 dm_wait，实际 {len(rows)} 条"
        assert rows[0]["channel"] == "dm", rows[0]["channel"]
    finally:
        w.st.close()


def test_冷却期内这条不再被挑中(tmp_path, monkeypatch):
    """私信停着的时候，不能每轮都从队头挑这条、白搜一遍

    没有冷却标记的话，这条线索排序位置不变 → 每轮第一个被挑中 →
    20 秒搜索 × 每轮 → 整轮在原地打转，留言也发不出去。
    """
    w, logs = _worker(tmp_path, n=2, dm_daily_limit=1)
    try:
        w.st.add_touch(lead_id=1, display_id="dy_used", status="sent", channel="dm")
        _跑一轮(w, _FakeDouyin(), limit=1, monkeypatch=monkeypatch)

        # 谁被打了 dm_wait 标记，谁就该被晾起来
        marked = [r["lead_id"] for r in w.st._conn.execute(
            "SELECT lead_id FROM outreach_records WHERE status='dm_wait'")]
        assert len(marked) == 1, f"该记一条 dm_wait，实际 {marked}"
        deferred = marked[0]

        ids = [p["id"] for p in w.st.pending_leads()]
        assert deferred not in ids, \
            f"冷却期内的线索不该再被挑中，实际队列 {ids}"
        assert set(ids) == {1, 2} - {deferred}, \
            f"别的线索照常排队，实际队列 {ids}"
    finally:
        w.st.close()


def test_冷却标记不占私信额度(tmp_path, monkeypatch):
    """dm_wait 是「没发出去」，不能算额度"""
    w, logs = _worker(tmp_path, dm_daily_limit=10)
    try:
        w._dm_paused_until = time.time() + 3600     # 触发暂停分支
        _跑一轮(w, _FakeDouyin(), limit=1, monkeypatch=monkeypatch)
        assert w.st.dm_sent_today() == 0, "没发出去就不该占额度"
    finally:
        w.st.close()


def test_冷却过期后重新排队(tmp_path, monkeypatch):
    """晾够时间就该重新进队列（私信恢复后还能触达）"""
    w, logs = _worker(tmp_path, dm_daily_limit=1)
    try:
        w.st.add_touch(lead_id=1, display_id="dy_used", status="sent", channel="dm")
        _跑一轮(w, _FakeDouyin(), limit=1, monkeypatch=monkeypatch)
        # 把冷却标记改成 40 分钟前 → 过期
        w.st._conn.execute(
            "UPDATE outreach_records SET created_at = datetime('now','localtime','-40 minutes') "
            "WHERE status='dm_wait'")
        w.st._conn.commit()
        ids = [p["id"] for p in w.st.pending_leads()]
        assert 1 in ids, f"冷却过了就该重新排队，实际队列 {ids}"
    finally:
        w.st.close()


# ============================================================
# 5. 私信熔断必须跨轮有效
# ============================================================

def test_私信暂停跨轮有效(tmp_path):
    """「暂停私信 60 分钟」以前只管当前这一轮

    根因：worker **每轮都会新建**（runner._run_in_thread 每次 new 一个），
    暂停时间只存在实例内存里 → 下一轮又从 0 开始撞私信。
    """
    db = _建库(tmp_path)
    w, _ = _worker_on(db)
    try:
        w.st.set_dm_pause_until(time.time() + 3600)
        # 模拟下一轮：新 worker 从 DB 读暂停状态
        w2, _ = _worker_on(db)
        try:
            assert w2._dm_paused_until > time.time(), \
                "新 worker 必须读到 DB 里的私信暂停"
        finally:
            w2.st.close()
    finally:
        w.st.close()


def test_私信被拦计数跨轮累计(tmp_path):
    """连续被拦的计数也要跨轮累计，否则熔断永远触发不了"""
    db = _建库(tmp_path)
    w, _ = _worker_on(db)
    try:
        assert w.st.dm_fail_count() == 0
        assert w.st.bump_dm_fail() == 1
        assert w.st.bump_dm_fail() == 2
        # 新 worker 读到的是 2，不是 0
        w2, _ = _worker_on(db)
        try:
            assert w2._dm_failed == 2, w2._dm_failed
        finally:
            w2.st.close()
    finally:
        w.st.close()


def test_私信发成功后计数清零(tmp_path):
    """发出去一条 → 计数清零（通道是好的）"""
    w, _ = _worker(tmp_path)
    try:
        w.st.bump_dm_fail()
        w.st.bump_dm_fail()
        dy = _FakeDouyin(dm_ok=True)
        w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert w.st.dm_fail_count() == 0, "私信发得出去 → 旧账清掉"
    finally:
        w.st.close()
