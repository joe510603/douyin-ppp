"""连续模式：新进来的线索插队

用户要求（2026-11-12）：「打开自动触达，就会一直自己去触达了，
包括有新进的也会自动触达」。

为什么需要插队
--------------
一轮开始时挑好的名单是个**快照**，而一轮要跑十几分钟。
期间新抓到的线索——尤其是刚在直播间问价的那种——
按快照排队的话要等到下一轮才被处理，转化窗口就过了。

所以每处理一条之前重新看一眼队列，把**比当前这条更新**的换到最前面。

要盯住的：
1. 更新的线索换到前面，且**名单长度不变**（不能超轮次上限）
2. 被换下来的那条排到队尾，这轮照样会处理到（不丢活）
3. 比当前这条更老的不插队（没意义）
4. 已经在名单里的不重复插
5. 30 天内真的发出去过的，不插队（不重复打扰）
"""

from __future__ import annotations

import sqlite3

from src.config import get_config
from src.outreach.worker import OutreachWorker

LEADS_DDL = """
CREATE TABLE leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_key TEXT NOT NULL DEFAULT '',
    display_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    intent_level TEXT DEFAULT 'medium',
    intent_score INTEGER DEFAULT 0,
    intent_reason TEXT DEFAULT '',
    evidence TEXT DEFAULT '[]',
    car_model TEXT DEFAULT '',
    store TEXT DEFAULT '',
    monitor_name TEXT DEFAULT '',
    outreach_status TEXT DEFAULT 'pending',
    outreach_note TEXT DEFAULT '',
    outreach_at TEXT DEFAULT '',
    bitable_record_id TEXT DEFAULT '',
    sec_uid TEXT DEFAULT '',
    last_hit_at TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
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


def _建库(tmp_path) -> str:
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    conn.execute(LEADS_DDL)
    conn.execute(OUTREACH_RECORDS_DDL)
    conn.commit()
    conn.close()
    return str(path)


def _加线索(db_path: str, display_id: str, last_hit_at: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO leads (lead_key, display_id, nickname, intent_level, "
        "intent_score, outreach_status, last_hit_at, created_at, updated_at) "
        "VALUES (?, ?, ?, 'medium', 50, 'pending', ?, ?, ?)",
        [display_id, display_id, f"某人{display_id}", last_hit_at,
         last_hit_at, last_hit_at],
    )
    conn.commit()
    conn.close()


def _已触达(db_path: str, display_id: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO outreach_records (id, display_id, status, created_at) "
        "VALUES (?, ?, 'sent', datetime('now', 'localtime'))",
        [f"rec-{display_id}", display_id],
    )
    conn.commit()
    conn.close()


def _worker(db_path: str):
    return OutreachWorker(get_config(), db_path, log=lambda m: None)


def _批次(w, ids: list[str]) -> list[dict]:
    """按 display_id 组一批待处理线索（模拟轮次开始时的快照）"""
    out = []
    for did in ids:
        row = w.st._conn.execute(
            "SELECT * FROM leads WHERE display_id = ?", (did,)
        ).fetchone()
        out.append(dict(row))
    return out


# ============================================================
# 插队
# ============================================================

def test_更新的线索会换到前面(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, "old", "2026-11-10 10:00:00")
    _加线索(db, "mid", "2026-11-11 10:00:00")
    _加线索(db, "new", "2026-11-12 10:00:00")
    w = _worker(db)

    picked = _批次(w, ["old", "mid"])
    assert w._promote_hot_leads(picked, 0) == 1
    assert picked[0]["display_id"] == "new"


def test_插队后名单长度不变(tmp_path):
    """换人不是加人头——轮次上限不能被插队撑破"""
    db = _建库(tmp_path)
    _加线索(db, "old", "2026-11-10 10:00:00")
    _加线索(db, "new", "2026-11-12 10:00:00")
    w = _worker(db)

    picked = _批次(w, ["old"])
    n = len(picked)
    w._promote_hot_leads(picked, 0)
    assert len(picked) == n
    assert picked[0]["display_id"] == "new"


def test_被挤下来的顺延到本轮后面(tmp_path):
    """插队的不是加人头——被换下来的那条本轮照样要做，只是排后面"""
    db = _建库(tmp_path)
    _加线索(db, "a", "2026-11-10 10:00:00")
    _加线索(db, "b", "2026-11-11 10:00:00")
    _加线索(db, "new", "2026-11-12 10:00:00")
    w = _worker(db)

    picked = _批次(w, ["a", "b"])
    w._promote_hot_leads(picked, 0)
    ids = [ld["display_id"] for ld in picked]
    assert len(ids) == 2, "总数不变"
    assert ids[0] == "new"
    assert "a" in ids, "被换下来的那条不能丢，要在本轮后面继续做"


def test_更老的不插队(tmp_path):
    """把老的换上来没意义——只提升比当前这条更新的"""
    db = _建库(tmp_path)
    _加线索(db, "cur", "2026-11-12 10:00:00")
    _加线索(db, "older", "2026-11-01 10:00:00")
    w = _worker(db)

    picked = _批次(w, ["cur"])
    assert w._promote_hot_leads(picked, 0) == 0
    assert picked[0]["display_id"] == "cur"


def test_已经在名单里的不重复插(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, "a", "2026-11-10 10:00:00")
    _加线索(db, "b", "2026-11-12 10:00:00")
    w = _worker(db)

    picked = _批次(w, ["a", "b"])
    # 当前这条是 a，但 b 已经在名单里（只是还没轮到）→ 不该重复插
    assert w._promote_hot_leads(picked, 0) == 0
    assert [ld["display_id"] for ld in picked] == ["a", "b"]


def test_近期触达过的不插队(tmp_path):
    """30 天内真的发出去过的人，不再插队打扰"""
    db = _建库(tmp_path)
    _加线索(db, "old", "2026-11-10 10:00:00")
    _加线索(db, "touched", "2026-11-12 10:00:00")
    _已触达(db, "touched")
    w = _worker(db)

    picked = _批次(w, ["old"])
    assert w._promote_hot_leads(picked, 0) == 0
    assert picked[0]["display_id"] == "old"


def test_没有新线索就不动(tmp_path):
    db = _建库(tmp_path)
    _加线索(db, "only", "2026-11-10 10:00:00")
    w = _worker(db)

    picked = _批次(w, ["only"])
    assert w._promote_hot_leads(picked, 0) == 0
    assert len(picked) == 1
