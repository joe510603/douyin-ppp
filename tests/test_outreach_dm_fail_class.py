"""私信失败要分清「是谁的问题」——别再因为对方关了私信就停掉整个触达

事故现场（2026-11-15）
-----------------------
飞书群弹了一张红卡片：

    「账号被限流（软风控），已停手。
      原因：连续 2 条私信发不出去，多半是私信功能被限了，已停手。冷却 60 分钟后再试
      评论发出去了、也没报错，但页面上看不到——抖音把它静默吞了。
      扫码没用，这不是登录问题。怎么办：…」

用户问：「确定不是误判？」——**是误判，而且错了两层**：

1. **触发条件错**：那 4 条私信失败的原因全是
   「主页上没找到可见的『私信』按钮（可能对方关闭了私信）」——
   是**对方**关了私信，跟我们的账号一点关系没有。
   代码却把「私信发不出去」一律当成「我们被限流」，
   连续 2 条就停掉**整个触达**。
   而账号好得很：停手之后 14:13 / 14:14 / 14:17 又连发 3 条评论，**全部成功**。
   停手纯属白停 —— 白白丢掉当轮所有评论触达机会。

2. **文案拼接错**：卡片标题和原因说的是「私信被限」，
   「怎么办」那段却是写给「评论被静默吞」场景的固定文案 →
   让人以为评论也被吞了。实际那 4 条评论全发出去了。

修法
----
- 私信失败分三类（``DM_FAIL_*``）：
  ``target_unavailable``（对方关了私信）/ ``env_issue``（我们环境问题）
  → 跳过这条，**不计数、不停手**；
  ``platform_blocked``（平台拦了）→ 只有这种才可能是账号被限。
- 私信熔断只**暂停私信通道**，评论照发
  （这正是「私信被限不该连累留言」那条原则）。
- 告警单独一种 kind（``KIND_DM``），只讲私信的事，不再跟评论限流混着说。
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import (  # noqa: E402
    DM_FAIL_BLOCKED, DM_FAIL_ENV, DM_FAIL_TARGET,
)
from src.outreach.worker import OutreachWorker  # noqa: E402


class _FakeDouyin:
    """假装是个 Douyin：私信按指定方式失败"""

    def __init__(self, ok: bool = True, msg: str = "", kind: str = "",
                 is_private: bool = False):
        self.ok = ok
        self.msg = msg
        self.kind = kind
        self.is_private = is_private
        self.calls = 0

    def send_dm(self, sec_uid: str, text: str) -> dict:
        self.calls += 1
        if self.ok:
            return {"ok": True, "msg": "已发送", "is_private": self.is_private}
        return {"ok": False, "msg": self.msg or "私信失败", "kind": self.kind,
                "is_private": self.is_private}

    def detect_private_account(self) -> bool:
        return self.is_private

    def _body_text(self) -> str:
        return ""

    def check_risk(self) -> None:
        return None


def _建库(tmp_path):
    db = tmp_path / "t.db"
    import sqlite3
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
        " outreach_note TEXT, created_at DEFAULT '', updated_at TEXT, "
        " source TEXT, bitable_record_id TEXT, bitable_synced_status TEXT, "
        " store TEXT, last_hit_at TEXT, sec_uid TEXT, not_found_count INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, sec_uid, store, "
        " outreach_status, status) VALUES (1, 'dy_x', '昵称A', "
        "'MS4wLjABAAAAtest', '测试门店', 'pending', 'new')"
    )
    conn.commit()
    conn.close()
    return db


def _worker(tmp_path, **cfg_over):
    cfg = get_config().model_copy(deep=True)
    cfg.outreach.dm_fallback = True
    cfg.outreach.dm_daily_limit = 10
    cfg.outreach.dm_fail_threshold = 2
    cfg.outreach.dm_pause_minutes = 60
    cfg.outreach.adaptive_delay = False
    cfg.outreach.min_delay = 20
    cfg.outreach.max_delay = 30
    for k, v in cfg_over.items():
        setattr(cfg.outreach, k, v)
    logs: list[str] = []
    w = OutreachWorker(cfg, str(_建库(tmp_path)), log=logs.append)
    w._dry = False
    return w, logs


def _lead(**kw) -> dict:
    lead = {
        "id": 1, "display_id": "dy_x", "nickname": "昵称A",
        "sec_uid": "MS4wLjABAAAAtest", "store": "测试门店",
        "car_model": "小米SU7", "contact_type": "", "contact_value": "",
        "evidence": "[]", "monitor_name": "测试直播间",
        "bitable_record_id": "", "intent_level": "high", "intent_score": 90,
    }
    lead.update(kw)
    return lead


# ============================================================
# 1. 对方关了私信 → 跳过，不计数，不停手
# ============================================================

def test_对方关了私信不计数不停手(tmp_path):
    """回归事故：连着 4 条「对方关了私信」也不该把整个触达停掉。"""
    w, logs = _worker(tmp_path, dm_fail_threshold=2)
    try:
        dy = _FakeDouyin(ok=False, msg="主页上没找到可见的「私信」按钮（可能对方关闭了私信）",
                         kind=DM_FAIL_TARGET)
        for _ in range(4):
            with pytest.raises(Exception) as ei:
                w._fallback_to_dm(dy, _lead(), {"dry": 0})
            # 跳过这条（终态），但**不是**停手
            assert "私信也没发出去" in str(ei.value), str(ei.value)

        assert w._dm_failed == 0, "对方关了私信不算我们被限流"
        assert w._dm_paused_until == 0, "不该暂停私信通道"
    finally:
        w.st.close()


def test_环境问题不计数不停手(tmp_path):
    """输入框没渲染这类我们自己的环境问题，也不该停手。"""
    w, _ = _worker(tmp_path, dm_fail_threshold=2)
    try:
        dy = _FakeDouyin(ok=False, msg="私信输入框没渲染出来", kind=DM_FAIL_ENV)
        for _ in range(3):
            with pytest.raises(Exception):
                w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert w._dm_failed == 0
        assert w._dm_paused_until == 0
    finally:
        w.st.close()


# ============================================================
# 2. 平台拦了 → 连续 N 次才暂停**私信通道**（触达不停）
# ============================================================

def test_平台拦了只暂停私信通道不停整个触达(tmp_path):
    """私信被平台拦 → 暂停私信，**评论照发**。

    这是用户定过的原则：私信被限不该连累留言。
    """
    w, logs = _worker(tmp_path, dm_fail_threshold=2, dm_pause_minutes=60)
    notified = []
    w._notify_risk = lambda kind, reason, lead=None: notified.append((kind, reason)) or True
    try:
        dy = _FakeDouyin(ok=False, msg="私信被拦：操作频繁", kind=DM_FAIL_BLOCKED)

        # 第 1 次：还没到阈值，只是这一条失败
        with pytest.raises(Exception):
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert w._dm_failed == 1
        assert w._dm_paused_until == 0, "还没到阈值不该暂停"

        # 第 2 次：到阈值 → 暂停私信通道，但**不抛停手异常**
        with pytest.raises(Exception) as ei:
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        # 异常是 NoWorks（跳过这条），不是 RiskControl（停整个触达）
        assert "RiskControl" not in type(ei.value).__name__
        assert w._dm_paused_until > time.time(), "私信通道该被暂停"
        assert notified, "该发通知告诉用户"

        kind, reason = notified[0]
        assert kind == "dm", kind
        assert "评论照发" in reason, reason
    finally:
        w.st.close()


def test_私信暂停期间直接退回队列不重复发(tmp_path):
    """暂停期间再遇到没作品的人 → 退回队列等私信恢复，别再去撞私信。

    ⚠ 2026-11-27：以前这里抛 NoWorks → 线索被判 skipped **终态** →
    这人就再也不会被留言。而私信被限是我们这边的临时状态，
    跟这个人能不能联系没关系 → 改成 DmUnavailable → 退回 pending。
    """
    w, _ = _worker(tmp_path, dm_fail_threshold=1, dm_pause_minutes=60)
    try:
        dy = _FakeDouyin(ok=False, msg="私信被拦：操作频繁", kind=DM_FAIL_BLOCKED)
        with pytest.raises(Exception):
            w._fallback_to_dm(dy, _lead(), {"dry": 0})
        assert w._dm_paused_until > time.time()

        dy2 = _FakeDouyin(ok=True)
        with pytest.raises(Exception) as ei:
            w._fallback_to_dm(dy2, _lead(), {"dry": 0})
        assert "暂停" in str(ei.value), str(ei.value)
        assert dy2.calls == 0, "暂停期间不该再发私信"
    finally:
        w.st.close()


# ============================================================
# 3. 分类常量本身
# ============================================================

def test_三种私信失败分类是分开的():
    assert DM_FAIL_TARGET != DM_FAIL_ENV != DM_FAIL_BLOCKED
    assert DM_FAIL_BLOCKED == "platform_blocked"
