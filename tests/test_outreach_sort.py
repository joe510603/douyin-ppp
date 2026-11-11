"""触达排序：最近抓到的线索优先（用户要求）

用户原话：「能不能优先给日期最近的发消息？发布消息的排序要以最近抓取的优先」

为什么不能直接用 created_at
---------------------------
``created_at`` 是**首次**抓到这条线索的时间。同一个人隔几天又在直播间问了一次，
created_at 还停在老时间，但他其实是最"热乎"的（可能人就在直播间等着回复）。

所以加了 ``leads.last_hit_at``：这条线索**最后一次**被看到的时间。
新线索写入时就填好，历史线索用 scripts/backfill_lead_last_hit.py 回填。
"""

from __future__ import annotations

import sqlite3

from src.models.lead import fmt_dt
from src.outreach.store import OutreachStore


def _建库(tmp_path) -> str:
    """建一个带 leads 表的库（表结构照抄线上的）"""
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_key TEXT NOT NULL DEFAULT '',
            user_id TEXT DEFAULT '',
            display_id TEXT DEFAULT '',
            nickname TEXT DEFAULT '',
            room_id TEXT DEFAULT '',
            monitor_name TEXT DEFAULT '',
            store TEXT DEFAULT '',
            intent_level TEXT DEFAULT 'none',
            intent_score INTEGER DEFAULT 0,
            intent_reason TEXT DEFAULT '',
            car_model TEXT DEFAULT '',
            contact_type TEXT DEFAULT '',
            contact_value TEXT DEFAULT '',
            evidence TEXT DEFAULT '[]',
            evidence_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            last_hit_at TEXT DEFAULT '',
            status TEXT DEFAULT 'new',
            pushed_at TEXT DEFAULT '',
            outreach_status TEXT DEFAULT 'pending',
            outreach_at TEXT DEFAULT '',
            outreach_note TEXT DEFAULT '',
            source TEXT DEFAULT 'rule',
            bitable_record_id TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()
    conn.close()
    return str(path)


def _加线索(db_path: str, display_id: str, score: int, created_at: str,
            last_hit_at: str = "") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO leads (lead_key, display_id, nickname, intent_level, "
        "intent_score, outreach_status, created_at, updated_at, last_hit_at) "
        "VALUES (?, ?, ?, 'medium', ?, 'pending', ?, ?, ?)",
        [display_id, display_id, "某人", score, created_at, created_at,
         last_hit_at or created_at],
    )
    conn.commit()
    conn.close()


# ============================================================
# 排序
# ============================================================

def test_最近抓到的排前面(tmp_path):
    """高意向的老线索 vs 低意向的新线索 → 新的排前面"""
    db = _建库(tmp_path)
    # 老但是分高
    _加线索(db, "old_high", 90, "2026-11-01 10:00:00")
    # 新但是分低
    _加线索(db, "new_low", 20, "2026-11-11 16:00:00")

    st = OutreachStore(db)
    try:
        leads = st.pending_leads()
        assert leads[0]["display_id"] == "new_low", \
            f"应该先发最近抓到的，实际 {[l['display_id'] for l in leads]}"
    finally:
        st.close()


def test_可以切回高意向优先(tmp_path):
    """老排序留着，方便对照/回退"""
    db = _建库(tmp_path)
    _加线索(db, "old_high", 90, "2026-11-01 10:00:00")
    _加线索(db, "new_low", 20, "2026-11-11 16:00:00")

    st = OutreachStore(db)
    try:
        leads = st.pending_leads(sort_by="intent")
        assert leads[0]["display_id"] == "old_high"
    finally:
        st.close()


def test_同时间按意向分倒序(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, "same_low", 20, "2026-11-11 16:00:00")
    _加线索(db, "same_high", 80, "2026-11-11 16:00:00")

    st = OutreachStore(db)
    try:
        leads = st.pending_leads()
        assert leads[0]["display_id"] == "same_high"
    finally:
        st.close()


def test_最后互动时间比首次抓到时间更好使(tmp_path):
    """11-07 抓到、11-11 又聊了一次 → 应该排在 11-10 抓到的人前面"""
    db = _建库(tmp_path)
    _加线索(db, "active_today", 30, "2026-11-07 09:00:00",
            last_hit_at="2026-11-11 16:30:00")
    _加线索(db, "quiet_yesterday", 30, "2026-11-10 09:00:00",
            last_hit_at="2026-11-10 09:00:00")

    st = OutreachStore(db)
    try:
        leads = st.pending_leads()
        assert leads[0]["display_id"] == "active_today", \
            f"实际 {[l['display_id'] for l in leads]}"
    finally:
        st.close()


def test_没有last_hit_at的老数据用created_at兜底(tmp_path):
    """回填不到的老数据不能被排到最前面（也不能丢）"""
    db = _建库(tmp_path)
    _加线索(db, "no_lasthit", 50, "2026-11-09 09:00:00")
    # 手工清空，模拟回填失败的老数据
    conn = sqlite3.connect(db)
    conn.execute("UPDATE leads SET last_hit_at = ''")
    conn.commit()
    conn.close()

    st = OutreachStore(db)
    try:
        leads = st.pending_leads()
        assert len(leads) == 1
        assert leads[0]["display_id"] == "no_lasthit"
    finally:
        st.close()


# ============================================================
# 时间格式：必须统一，否则字符串排序会错乱
# ============================================================

def test_时间格式统一():
    """ISO 的 "T" 比空格大，混在一起排序结果会完全错乱"""
    assert fmt_dt("2026-11-11T16:35:25.405642") == "2026-11-11 16:35:25"
    assert fmt_dt("2026-11-11 16:35:25") == "2026-11-11 16:35:25"
    assert fmt_dt(None) == ""
    assert fmt_dt("") == ""
    # 同一时刻的不同写法，格式化后必须完全相等
    assert fmt_dt("2026-11-11T16:35:25") == fmt_dt("2026-11-11 16:35:25")


def test_格式化后可以直接字符串比较():
    a = fmt_dt("2026-11-11T09:00:00")
    b = fmt_dt("2026-11-11T16:00:00")
    assert a < b, "早的时间要排在前面"
