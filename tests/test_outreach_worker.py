"""触达 worker 测试 — 干跑不能污染线索状态

背景
----
干跑只是验证「能不能找到人、能不能挑到作品、话术长什么样」，
**不应该改动任何线索状态**。

之前干跑会把状态写成 searching/found，踩了两个坑：
1. 这条线索就卡在队列外了——回拉也不会把它改回 pending
   （本地相对上次同步的值变了，走「跳过」分支），
   下一轮真跑时这些线索再也不会被处理
2. 多机共用一张飞书表时更危险：干跑会把另一台机器写的真实触达状态盖掉
"""

import sqlite3

from src.config import get_config
from src.outreach.worker import OutreachWorker

LEADS_DDL = """
CREATE TABLE leads (
    id INTEGER PRIMARY KEY,
    display_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    outreach_status TEXT DEFAULT 'pending',
    outreach_note TEXT DEFAULT '',
    outreach_at TEXT DEFAULT '',
    intent_level TEXT DEFAULT 'none',
    intent_score INTEGER DEFAULT 0,
    evidence TEXT DEFAULT '[]',
    car_model TEXT DEFAULT '',
    store TEXT DEFAULT '',
    monitor_name TEXT DEFAULT '',
    bitable_record_id TEXT DEFAULT ''
)
"""


OUTREACH_RECORDS_DDL = """
CREATE TABLE outreach_records (
    id TEXT PRIMARY KEY,
    lead_id INTEGER DEFAULT NULL,
    display_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    message TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    record_id TEXT DEFAULT '',
    sec_uid TEXT DEFAULT '',
    aweme_id TEXT DEFAULT '',
    aweme_desc TEXT DEFAULT '',
    template_id TEXT DEFAULT '',
    channel TEXT DEFAULT 'comment',
    verified INTEGER DEFAULT 0
)
"""


def _make_db(tmp_path):
    db = tmp_path / "worker_test.db"
    conn = sqlite3.connect(db)
    conn.execute(LEADS_DDL)
    conn.execute(OUTREACH_RECORDS_DDL)
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, outreach_status) "
        "VALUES (1, 'dy001', '小明', 'pending')"
    )
    conn.commit()
    conn.close()
    return str(db)


def _status_of(db_path: str, lead_id: int = 1) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT outreach_status FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_dry_run_does_not_change_local_status(tmp_path):
    """干跑时 _set_status 不许动本地状态"""
    db = _make_db(tmp_path)
    w = OutreachWorker(get_config(), db, log=lambda m: None)
    w._dry = True

    w._set_status({"id": 1, "display_id": "dy001"}, "searching")
    w._set_status({"id": 1, "display_id": "dy001"}, "found")

    assert _status_of(db) == "pending"


def test_dry_run_does_not_change_note_or_time(tmp_path):
    """干跑也不许写 outreach_note / outreach_at"""
    db = _make_db(tmp_path)
    w = OutreachWorker(get_config(), db, log=lambda m: None)
    w._dry = True

    w._set_status({"id": 1, "display_id": "dy001"}, "failed", "没搜到这个人")

    conn = sqlite3.connect(db)
    try:
        note, at = conn.execute(
            "SELECT outreach_note, outreach_at FROM leads WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert note == ""
    assert at == ""


def test_real_run_changes_local_status(tmp_path):
    """真跑（_dry=False）必须照常写状态，否则队列不会推进"""
    db = _make_db(tmp_path)
    w = OutreachWorker(get_config(), db, log=lambda m: None)
    w._dry = False

    w._set_status({"id": 1, "display_id": "dy001"}, "found")

    assert _status_of(db) == "found"


def test_dry_run_record_does_not_block_real_send(tmp_path):
    """干跑记录不能算「已触达」——否则干跑验证完的人永远真发不出去

    去重的目的是「不重复打扰客户」，干跑没打扰过客户。
    """
    from src.outreach.store import OutreachStore

    db = _make_db(tmp_path)
    st = OutreachStore(db)
    try:
        st.add_touch(lead_id=1, display_id="dy001", nickname="小明",
                     status="dry_run")
        # 干跑过了，但还没真发过 → 不该被去重挡住
        assert st.recent_touch_days("dy001", 30) is False
    finally:
        st.close()


def test_sent_record_blocks_resend(tmp_path):
    """真发过的要挡住，避免重复打扰"""
    from src.outreach.store import OutreachStore

    db = _make_db(tmp_path)
    st = OutreachStore(db)
    try:
        st.add_touch(lead_id=1, display_id="dy001", nickname="小明",
                     status="sent")
        assert st.recent_touch_days("dy001", 30) is True
    finally:
        st.close()


def test_failed_record_does_not_block_retry(tmp_path):
    """失败的不算发出去过，可以重试"""
    from src.outreach.store import OutreachStore

    db = _make_db(tmp_path)
    st = OutreachStore(db)
    try:
        st.add_touch(lead_id=1, display_id="dy001", nickname="小明",
                     status="failed", error="没搜到人")
        assert st.recent_touch_days("dy001", 30) is False
    finally:
        st.close()


def test_risk_cooldown_blocks_start(tmp_path, monkeypatch):
    """风控冷却期内不允许再启动触达

    抖音软风控（评论发出去但页面看不到）触发后，继续发只会把号搞废。
    """
    from src.outreach.runner import OutreachRunner

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outreach_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, dry_run INTEGER DEFAULT 0, "
        "total INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, "
        "skipped INTEGER DEFAULT 0, note TEXT DEFAULT '', "
        "started_at TEXT DEFAULT (datetime('now','localtime')), "
        "finished_at TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO outreach_runs (note, started_at) VALUES (?, datetime('now','localtime'))",
        ("风控：评论发出去但页面看不到（软风控），已手动停止",),
    )
    conn.commit()
    conn.close()

    r = OutreachRunner()
    monkeypatch.setattr(r, "_db_path", lambda: db)

    left = r.risk_cooldown_left()
    assert left > 0


def test_no_cooldown_when_recent_run_is_normal(tmp_path, monkeypatch):
    """最近一次运行正常 → 没有冷却"""
    from src.outreach.runner import OutreachRunner

    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outreach_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, dry_run INTEGER DEFAULT 0, "
        "total INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, "
        "skipped INTEGER DEFAULT 0, note TEXT DEFAULT '', "
        "started_at TEXT DEFAULT (datetime('now','localtime')), "
        "finished_at TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO outreach_runs (note, started_at) VALUES ('干跑', datetime('now','localtime'))"
    )
    conn.commit()
    conn.close()

    r = OutreachRunner()
    monkeypatch.setattr(r, "_db_path", lambda: db)

    assert r.risk_cooldown_left() == 0
