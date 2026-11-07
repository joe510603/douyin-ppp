"""线索存储层测试 — 建表、幂等 upsert、查询、触达统计

沿用项目既有约定：同步测试函数 + asyncio.run()，
不引入 pytest-asyncio 依赖。
"""

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.models.comment import LiveComment, MessageType
from src.models.lead import Lead, OutreachRecord, OutreachStatus
from src.storage.db_storage import DBStorage


def _make_db(tmpdir):
    store = DBStorage(db_path=str(Path(tmpdir) / "test.db"))
    return store


def _lead(key="d1", level="high", score=80, contact="", evidence=None):
    ev = evidence if evidence is not None else ["这个多少钱", "有现车吗"]
    return Lead(
        lead_key=key,
        user_id="u1",
        display_id="d1",
        nickname="小明",
        room_id="r1",
        monitor_name="测试直播间",
        intent_level=level,
        intent_score=score,
        intent_reason="问了价格和提车时间",
        car_model="小米SU7",
        contact_type="phone" if contact else "",
        contact_value=contact,
        evidence=json.dumps(ev, ensure_ascii=False),
        evidence_count=len(ev),
        comment_count=2,
    )


def _run(coro_fn):
    """在临时库上跑一个 async 测试体"""
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            await db.init_db()
            try:
                return await coro_fn(db)
            finally:
                await db.close()
    return asyncio.run(main())


# ============================================================
# 线索表
# ============================================================

def test_upsert_新建():
    async def t(db):
        lid = await db.upsert_lead(_lead())
        assert lid and lid > 0
        rows, total = await db.query_leads()
        assert total == 1
        assert rows[0]["display_id"] == "d1"
        assert rows[0]["intent_level"] == "high"
    _run(t)


def test_upsert_幂等_不重复插入():
    async def t(db):
        await db.upsert_lead(_lead())
        await db.upsert_lead(_lead())
        _, total = await db.query_leads()
        assert total == 1
    _run(t)


def test_重复命中累积证据条数():
    async def t(db):
        await db.upsert_lead(_lead(evidence=["a"]))
        await db.upsert_lead(_lead(evidence=["b"]))
        rows, _ = await db.query_leads()
        assert rows[0]["evidence_count"] == 2
        assert rows[0]["comment_count"] == 4
    _run(t)


def test_意向等级取更高者():
    async def t(db):
        await db.upsert_lead(_lead(level="low", score=20))
        await db.upsert_lead(_lead(level="high", score=90))
        rows, _ = await db.query_leads()
        assert rows[0]["intent_level"] == "high"
        assert rows[0]["intent_score"] == 90
    _run(t)


def test_低意向不会覆盖高意向():
    async def t(db):
        await db.upsert_lead(_lead(level="high", score=90))
        await db.upsert_lead(_lead(level="low", score=20))
        rows, _ = await db.query_leads()
        assert rows[0]["intent_level"] == "high"
        assert rows[0]["intent_score"] == 90
    _run(t)


def test_联系方式空值不覆盖已有():
    async def t(db):
        await db.upsert_lead(_lead(contact="13812345678"))
        await db.upsert_lead(_lead(contact=""))
        rows, _ = await db.query_leads()
        assert rows[0]["contact_value"] == "13812345678"
    _run(t)


def test_联系方式新值覆盖旧值():
    async def t(db):
        await db.upsert_lead(_lead(contact=""))
        await db.upsert_lead(_lead(contact="13900001111"))
        rows, _ = await db.query_leads()
        assert rows[0]["contact_value"] == "13900001111"
    _run(t)


def test_空字段不覆盖已有昵称():
    async def t(db):
        await db.upsert_lead(_lead())
        await db.upsert_lead(_lead())
        rows, _ = await db.query_leads()
        assert rows[0]["nickname"] == "小明"
    _run(t)


# ============================================================
# 线索查询
# ============================================================

def test_按等级筛选():
    async def t(db):
        await db.upsert_lead(_lead(key="a", level="high"))
        await db.upsert_lead(_lead(key="b", level="low"))
        rows, total = await db.query_leads(intent_levels=["high"])
        assert total == 1
        assert rows[0]["lead_key"] == "a"
    _run(t)


def test_按联系方式筛选():
    async def t(db):
        await db.upsert_lead(_lead(key="a", contact="13812345678"))
        await db.upsert_lead(_lead(key="b"))
        rows, total = await db.query_leads(has_contact=True)
        assert total == 1
        assert rows[0]["lead_key"] == "a"
    _run(t)


def test_关键词搜索():
    async def t(db):
        await db.upsert_lead(_lead(key="a"))
        await db.upsert_lead(_lead(key="b"))
        _, total = await db.query_leads(keyword="小明")
        assert total == 2
        _, total = await db.query_leads(keyword="不存在的关键词")
        assert total == 0
    _run(t)


def test_按意向分降序():
    async def t(db):
        await db.upsert_lead(_lead(key="a", level="low", score=20))
        await db.upsert_lead(_lead(key="b", level="high", score=90))
        rows, _ = await db.query_leads()
        assert rows[0]["lead_key"] == "b"
    _run(t)


def test_已存在键查询():
    async def t(db):
        await db.upsert_lead(_lead(key="a"))
        await db.upsert_lead(_lead(key="b"))
        found = await db.get_existing_lead_keys(["a", "c"])
        assert found == {"a"}
    _run(t)


# ============================================================
# 线索更新
# ============================================================

def test_update_字段():
    async def t(db):
        lid = await db.upsert_lead(_lead())
        ok = await db.update_lead(lid, intent_level="medium", intent_score=55)
        assert ok
        row = await db.get_lead(lid)
        assert row["intent_level"] == "medium"
        assert row["intent_score"] == 55
    _run(t)


def test_update_白名单外字段被忽略():
    async def t(db):
        lid = await db.upsert_lead(_lead())
        ok = await db.update_lead(lid, nonexistent_field="x")
        assert not ok
    _run(t)


def test_mark_pushed():
    async def t(db):
        lid = await db.upsert_lead(_lead())
        await db.mark_leads_pushed([lid])
        row = await db.get_lead(lid)
        assert row["status"] == "pushed"
        assert row["pushed_at"]
    _run(t)


def test_删除():
    async def t(db):
        lid = await db.upsert_lead(_lead())
        assert await db.delete_leads([lid]) == 1
        _, total = await db.query_leads()
        assert total == 0
    _run(t)


# ============================================================
# 用户活跃度聚合
# ============================================================

def _chat(content, display_id="d1", user_id="u1", nickname="小明"):
    return LiveComment(
        message_type=MessageType.CHAT,
        content=content,
        user_id=user_id,
        user_display_id=display_id,
        user_nickname=nickname,
        room_id="r1",
        monitor_name="直播间A",
        create_time=datetime.now(),
    )


def test_按用户聚合():
    async def t(db):
        await db.insert_comment(_chat("多少钱"))
        await db.insert_comment(_chat("有现车吗"))
        groups = await db.query_user_activity(since=datetime.now() - timedelta(hours=1))
        assert len(groups) == 1
        assert groups[0]["count"] == 2
        assert groups[0]["display_id"] == "d1"
        assert set(groups[0]["messages"]) == {"多少钱", "有现车吗"}
    _run(t)


def test_时间窗外不返回():
    """回归：collected_at 是 ISO(T) 格式，阈值必须同格式比较，
    否则历史数据会全部命中"""
    async def t(db):
        old = _chat("旧消息")
        old.collected_at = datetime.now() - timedelta(hours=2)
        await db.insert_comment(old)
        groups = await db.query_user_activity(since=datetime.now() - timedelta(hours=1))
        assert len(groups) == 0
    _run(t)


def test_时间窗内能返回():
    async def t(db):
        await db.insert_comment(_chat("新消息"))
        groups = await db.query_user_activity(since=datetime.now() - timedelta(hours=1))
        assert len(groups) == 1
    _run(t)


def test_进场消息不计入():
    async def t(db):
        await db.insert_comment(LiveComment(
            message_type=MessageType.MEMBER,
            content="[进场] 某人",
            user_id="111111",
            create_time=datetime.now(),
        ))
        groups = await db.query_user_activity(since=datetime.now() - timedelta(hours=1))
        assert len(groups) == 0
    _run(t)


# ============================================================
# 触达记录
# ============================================================

def test_触达记录写入与查询():
    async def t(db):
        await db.insert_outreach_record(OutreachRecord(
            id="r1", lead_id=1, display_id="d1", nickname="小明",
            status=OutreachStatus.COMMENTED.value, message="你好",
        ))
        rows, total = await db.query_outreach_records()
        assert total == 1
        assert rows[0]["message"] == "你好"
    _run(t)


def test_触达记录更新状态():
    async def t(db):
        await db.insert_outreach_record(OutreachRecord(id="r1", display_id="d1"))
        ok = await db.update_outreach_record("r1", status="commented", error="")
        assert ok
        rows, _ = await db.query_outreach_records()
        assert rows[0]["status"] == "commented"
    _run(t)


def test_今日计数只算已执行():
    async def t(db):
        await db.insert_outreach_record(OutreachRecord(id="r1", status="commented"))
        await db.insert_outreach_record(OutreachRecord(id="r2", status="failed"))
        await db.insert_outreach_record(OutreachRecord(id="r3", status="pending"))
        assert await db.count_outreach_today() == 1
    _run(t)
