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


def _make_db(tmp_path):
    db = tmp_path / "worker_test.db"
    conn = sqlite3.connect(db)
    conn.execute(LEADS_DDL)
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
