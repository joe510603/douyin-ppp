"""门店字段测试 — 归一化、落库、按门店筛选

门店名在配置里有多种写法：
    '小米汽车｜世茂广场店' / '小米汽车 | 成都龙湖滨江体验店' / '小米汽车 ｜ 机场路店'
归一化后要收敛成短名，销售才能按门店筛选线索。

沿用项目既有约定：同步测试函数 + asyncio.run()。
"""

import asyncio
import tempfile
from pathlib import Path

from src.models.lead import Lead
from src.storage.db_storage import DBStorage


def _lead(key="d1", display="d1", monitor="小米汽车｜世茂广场店", level="high"):
    return Lead(
        lead_key=key,
        user_id="u1",
        display_id=display,
        nickname="小明",
        room_id="r1",
        monitor_name=monitor,
        intent_level=level,
        intent_score=80,
        intent_reason="问了价格",
        car_model="SU7",
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


# ============================================================
# 落库时自动归一化
# ============================================================

def test_upsert_写入归一化门店():
    async def t(db):
        await db.upsert_lead(_lead(monitor="小米汽车｜世茂广场店"))
        rows, _ = await db.query_leads()
        assert rows[0]["store"] == "世茂广场店"


def test_upsert_半角竖线也归一化():
    async def t(db):
        await db.upsert_lead(_lead(monitor="小米汽车 | 成都龙湖滨江体验店"))
        rows, _ = await db.query_leads()
        assert rows[0]["store"] == "成都龙湖滨江体验店"


def test_upsert_顿号竖线也归一化():
    async def t(db):
        await db.upsert_lead(_lead(monitor="小米汽车丨成都万象城店"))
        rows, _ = await db.query_leads()
        assert rows[0]["store"] == "成都万象城店"


def test_upsert_空格竖线也归一化():
    async def t(db):
        await db.upsert_lead(_lead(monitor="小米汽车 ｜ 机场路店"))
        rows, _ = await db.query_leads()
        assert rows[0]["store"] == "机场路店"


def test_upsert_重复更新不冲掉门店():
    async def t(db):
        await db.upsert_lead(_lead(monitor="小米汽车｜世茂广场店"))
        await db.upsert_lead(_lead(monitor="小米汽车｜世茂广场店"))
        rows, total = await db.query_leads()
        assert total == 1
        assert rows[0]["store"] == "世茂广场店"


# ============================================================
# 按门店筛选
# ============================================================

def test_query_按门店筛选():
    async def t(db):
        await db.upsert_lead(_lead(key="a", display="a", monitor="小米汽车｜世茂广场店"))
        await db.upsert_lead(_lead(key="b", display="b", monitor="小米汽车｜德阳万达体验店"))
        await db.upsert_lead(_lead(key="c", display="c", monitor="小米汽车｜德阳万达体验店"))

        rows, total = await db.query_leads(store="德阳万达体验店")
        assert total == 2
        assert all(r["store"] == "德阳万达体验店" for r in rows)

        rows, total = await db.query_leads(store="世茂广场店")
        assert total == 1


def test_query_门店筛选为空时不过滤():
    async def t(db):
        await db.upsert_lead(_lead(key="a", display="a", monitor="小米汽车｜世茂广场店"))
        await db.upsert_lead(_lead(key="b", display="b", monitor="小米汽车｜德阳万达体验店"))
        rows, total = await db.query_leads(store=None)
        assert total == 2


def test_query_门店筛选无匹配():
    async def t(db):
        await db.upsert_lead(_lead(monitor="小米汽车｜世茂广场店"))
        rows, total = await db.query_leads(store="不存在的店")
        assert total == 0
        assert rows == []


def test_query_门店和等级组合筛选():
    async def t(db):
        await db.upsert_lead(
            _lead(key="a", display="a", monitor="小米汽车｜世茂广场店", level="high")
        )
        await db.upsert_lead(
            _lead(key="b", display="b", monitor="小米汽车｜世茂广场店", level="low")
        )
        rows, total = await db.query_leads(
            intent_levels=["high"], store="世茂广场店"
        )
        assert total == 1
        assert rows[0]["intent_level"] == "high"
