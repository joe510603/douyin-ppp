"""线索流水线的两个修正（2026-11-13 用户反馈）

一、同一个人说的内容要**全部展示**
------------------------------------------------
用户反馈：「你在抓取弹幕这边好像只保留了一句，能不能把同一个人说的内容都展示出来？」

根因：``upsert_lead`` 更新时 ``evidence = ?`` 直接整个覆盖，
而写入方每次只带**当前这一条**弹幕。
于是 ``evidence_count`` 在累加（表里显示「5 条证据」），
``evidence`` 内容却**只剩最后一句**。

二、飞书表里标「已留资」的人，别再给他留言
------------------------------------------------
用户流程：在飞书表里维护线索，拿到电话/微信时把状态改成「已留资」。

问题：映射表里**根本没有「已留资」** → 回拉直接忽略它
→ 本地状态还是 pending → 系统照样给已留资的人留言。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from src.models.lead import Lead
from src.storage.db_storage import DBStorage


# ============================================================
# 一、弹幕证据累加
# ============================================================

def _lead(key="d1", content="现在落地多少？"):
    return Lead(
        lead_key=key,
        user_id="u1",
        display_id="d1",
        nickname="小明",
        room_id="r1",
        monitor_name="小米汽车｜世茂广场店",
        intent_level="high",
        intent_score=80,
        intent_reason="问了价格",
        car_model="SU7",
        evidence=json.dumps([content], ensure_ascii=False),
        evidence_count=1,
    )


def _run(coro_fn):
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            store = DBStorage(db_path=str(Path(tmp) / "test.db"))
            await store.init_db()
            try:
                return await coro_fn(store)
            finally:
                await store.close()
    return asyncio.run(main())


def test_同一个人说的多句都要留着():
    async def t(db):
        await db.upsert_lead(_lead(content="现在落地多少？"))
        await db.upsert_lead(_lead(content="首付要多少？"))
        await db.upsert_lead(_lead(content="能分期吗？"))
        rows, total = await db.query_leads()
        assert total == 1, "同一个人还是一条线索"
        ev = json.loads(rows[0]["evidence"])
        assert len(ev) == 3, f"三句话都该留着，实际：{ev}"
        assert "首付要多少？" in ev, ev
        assert rows[0]["evidence_count"] == 3, rows[0]["evidence_count"]

    _run(t)


def test_重复的弹幕不重复记():
    async def t(db):
        await db.upsert_lead(_lead(content="在吗？"))
        await db.upsert_lead(_lead(content="在吗？"))
        rows, _ = await db.query_leads()
        ev = json.loads(rows[0]["evidence"])
        assert len(ev) == 1, f"重复内容不该记两遍：{ev}"

    _run(t)


def test_证据条数有上限():
    """不让一条线索的证据无限膨胀（表里那一列有长度限制）"""
    from src.storage.db_storage import _merge_evidence

    old = json.dumps([f"第{i}句" for i in range(20)], ensure_ascii=False)
    new = json.dumps(["最新一句"], ensure_ascii=False)
    out = json.loads(_merge_evidence(old, new, cap=10))
    assert len(out) == 10, len(out)
    assert out[-1] == "最新一句", out[-1]


# ============================================================
# 二、「已留资」
# ============================================================

def test_飞书的已留资要能被认出来():
    from src.intelligence.lead_service import (
        _FEISHU_OUTREACH_TO_LOCAL, _FEISHU_STATUS_TO_LOCAL,
    )
    assert _FEISHU_STATUS_TO_LOCAL.get("已留资") == "converted"
    assert _FEISHU_OUTREACH_TO_LOCAL.get("已留资") == "converted"


def test_已留资写回飞书用同一个词():
    from src.notify.bitable import OUTREACH_LABELS, STATUS_LABELS
    assert OUTREACH_LABELS.get("converted") == "已留资"
    assert STATUS_LABELS.get("converted") == "已留资"


def test_搜不到的人标成可重试状态而不是终态():
    """以前标 skipped（终态）→ 这条线索永远不会再被触达"""
    from src.outreach.store import STATUS_NOT_FOUND, STATUS_SKIPPED
    assert STATUS_NOT_FOUND != STATUS_SKIPPED
    from src.notify.bitable import OUTREACH_LABELS
    assert OUTREACH_LABELS.get("not_found") == "搜不到"


def test_已留资的人不在触达队列里(tmp_path):
    import sqlite3

    from src.outreach.store import STATUS_CONVERTED, OutreachStore

    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_key TEXT DEFAULT '', display_id TEXT DEFAULT '',
            nickname TEXT DEFAULT '', car_model TEXT DEFAULT '',
            monitor_name TEXT DEFAULT '', store TEXT DEFAULT '',
            intent_level TEXT DEFAULT 'none', intent_score INTEGER DEFAULT 0,
            intent_reason TEXT DEFAULT '', evidence TEXT DEFAULT '[]',
            last_hit_at TEXT DEFAULT '', status TEXT DEFAULT 'new',
            outreach_status TEXT DEFAULT 'pending',
            outreach_at TEXT DEFAULT '', outreach_note TEXT DEFAULT '',
            source TEXT DEFAULT 'rule', bitable_record_id TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute(
        "INSERT INTO leads (id, display_id, nickname, outreach_status) "
        "VALUES (1, 'u1', 'n1', 'pending')"
    )
    conn.commit()
    conn.close()

    st = OutreachStore(path)
    try:
        st.set_outreach_status(1, STATUS_CONVERTED, "")
        rows = st.pending_leads()
    finally:
        st.close()

    assert not any(r["id"] == 1 for r in rows)
