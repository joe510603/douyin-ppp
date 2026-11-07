"""实时线索服务测试 — 热路径打分 + 旁路落库"""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.models.comment import LiveComment, MessageType
from src.models.lead import IntentLevel
from src.storage.db_storage import DBStorage


def _comment(content, nickname="测试用户", display_id="test_dy_001", room_id="r1"):
    return LiveComment(
        message_type=MessageType.CHAT,
        content=content,
        user_id="u_" + (display_id or "x"),
        user_display_id=display_id,
        user_nickname=nickname,
        room_id=room_id,
        monitor_name="测试直播间",
        create_time=datetime.now(),
    )


def _run_with_db(coro_fn):
    """在临时库上跑一个 async 测试体，并让 LeadService 用上这个库"""
    import src.intelligence.lead_service as ls_mod

    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            db = DBStorage(db_path=str(Path(tmp) / "t.db"))
            await db.init_db()
            original = ls_mod.get_db
            ls_mod.get_db = lambda: db
            try:
                return await coro_fn(db, ls_mod)
            finally:
                ls_mod.get_db = original
                await db.close()
    return asyncio.run(main())


# ============================================================
# 热路径打分
# ============================================================

def test_高意向弹幕被打分并入队():
    async def t(db, ls_mod):
        svc = ls_mod.LeadService()
        await svc.start()
        try:
            r = svc.submit(_comment("落地多少钱？什么时候能提车 店在哪里"))
            assert r is not None, "高意向弹幕未命中"
            assert r.level in (IntentLevel.HIGH.value, IntentLevel.MEDIUM.value)
            assert svc.stats["hits"] == 1
            assert not svc._queue.empty()
        finally:
            await svc.stop()
    _run_with_db(t)


def test_噪音弹幕不入队():
    async def t(db, ls_mod):
        svc = ls_mod.LeadService()
        await svc.start()
        try:
            assert svc.submit(_comment("哈哈哈")) is None
            assert svc.submit(_comment("666")) is None
            assert svc._queue.empty()
            assert svc.stats["hits"] == 0
        finally:
            await svc.stop()
    _run_with_db(t)


def test_自家账号不入队():
    async def t(db, ls_mod):
        svc = ls_mod.LeadService()
        svc._self_names = {"小米汽车成都龙湖滨江体验店"}
        await svc.start()
        try:
            r = svc.submit(_comment("落地多少钱", nickname="小米汽车 | 成都龙湖滨江体验店"))
            assert r is None
            assert svc.stats["excluded"] == 1
        finally:
            await svc.stop()
    _run_with_db(t)


def test_非弹幕类型不参与打分():
    async def t(db, ls_mod):
        svc = ls_mod.LeadService()
        await svc.start()
        try:
            c = _comment("落地多少钱")
            c.message_type = MessageType.MEMBER
            assert svc.submit(c) is None
        finally:
            await svc.stop()
    _run_with_db(t)


# ============================================================
# 旁路落库
# ============================================================

def test_命中后异步落库():
    async def t(db, ls_mod):
        svc = ls_mod.LeadService()
        svc.config.lead.realtime_push = False   # 不测飞书，只测落库
        await svc.start()
        try:
            svc.submit(_comment("落地多少钱？什么时候提车", display_id="dy_e2e_001"))
            await svc._queue.join()   # 等队列排空
            rows, total = await db.query_leads()
            assert total == 1
            row = rows[0]
            assert row["display_id"] == "dy_e2e_001"
            assert row["intent_level"] in (IntentLevel.HIGH.value, IntentLevel.MEDIUM.value)
            assert row["source"] == "rule"
            assert "命中信号" in row["intent_reason"]
        finally:
            await svc.stop()
    _run_with_db(t)


def test_同一用户多次发言累积证据():
    async def t(db, ls_mod):
        svc = ls_mod.LeadService()
        svc.config.lead.realtime_push = False
        await svc.start()
        try:
            svc.submit(_comment("落地多少钱", display_id="dy_e2e_002"))
            await svc._queue.join()
            svc.submit(_comment("什么时候提车", display_id="dy_e2e_002"))
            await svc._queue.join()
            rows, total = await db.query_leads()
            assert total == 1, "同一用户应只有一条线索"
            assert rows[0]["evidence_count"] == 2
        finally:
            await svc.stop()
    _run_with_db(t)


def test_联系方式被提取():
    async def t(db, ls_mod):
        svc = ls_mod.LeadService()
        svc.config.lead.realtime_push = False
        await svc.start()
        try:
            svc.submit(_comment("落地多少钱？电话13812345678", display_id="dy_e2e_003"))
            await svc._queue.join()
            rows, _ = await db.query_leads()
            assert rows[0]["contact_value"] == "电话 13812345678"
            assert "phone" in rows[0]["contact_type"]
        finally:
            await svc.stop()
    _run_with_db(t)


def test_推送冷却期内不重复推():
    async def t(db, ls_mod):
        svc = ls_mod.LeadService()
        svc.config.lead.push_cooldown_minutes = 30
        svc._pushed_at["dy_e2e_004"] = datetime.now()
        assert svc._in_cooldown("dy_e2e_004") is True
        assert svc._in_cooldown("从未推过的用户") is False

        svc.config.lead.push_cooldown_minutes = 0
        assert svc._in_cooldown("dy_e2e_004") is False
    _run_with_db(t)
