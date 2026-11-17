"""**弹幕里就有的 secUid，别在建线索时丢掉** —— 触达时就不用再搜索

问题（2026-11-17 实测）
----------------------
采集器拿到弹幕的**第一条**就知道这个人的 secUid
（排除自家账号就是靠它判的 —— ``live_comments.user_sec_id``），
但 ``Lead`` 模型根本没有 ``sec_uid`` 字段、``upsert_lead`` 也不写它：

    线索总数 485，其中 315 条有 sec_uid —— 那 315 条是**触达时搜到后**
    写回去的（``OutreachStore.set_sec_uid``）；
    2026-11-17 当天新建的 19 条，**一条都没有**。

代价很实在
----------
抖音 web 端搜索接口是最脆弱的一环：

  · 一次要 ~20 秒（实测）
  · 会**静默限流**：不报错、不提示，就是给你一个空的 ``user_list``
  · 搜不到 → 整条线索出局（``LeadNotFound``）

而 ``/user/{sec_uid}`` 主页完全不受限流影响。
所以「建线索时就知道 secUid」= 每条省 17~20 秒 + 少一次被限流的机会。

要钉住的
--------
1. ``Lead`` 模型有 ``sec_uid`` 字段
2. 实时建线索（``lead_service._handle``）把它从弹幕带下来
3. 规则回扫（``rule_scanner``）、LLM 批量判定（``intent_analyzer``）也带
4. ``upsert_lead`` INSERT 时写入；UPDATE 时**只在空的时候填**，不覆盖已有的
5. 分组查询（回扫的数据来源）要能取到 sec_uid
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.models.lead import Lead

VIEWER_SEC = "MS4wLjABAAAAa7KPV8HdfjmNGnEiHBdnEPQqQ5ijyNd3QRu73H1mXunK8j6e6gvwTMFbP4aenZhf"
VIEWER_DISPLAY = "87184885725"


# ---------------------------------------------------------------- 模型

def test_lead_model_has_sec_uid():
    lead = Lead(lead_key="k1", display_id=VIEWER_DISPLAY)
    assert lead.sec_uid == ""          # 默认空，不影响老代码
    lead2 = Lead(lead_key="k2", sec_uid=VIEWER_SEC)
    assert lead2.sec_uid == VIEWER_SEC


# ---------------------------------------------------------------- 实时建线索

def test_lead_service_carries_sec_uid_from_comment():
    """实时建线索：弹幕里的 secUid 必须带到 Lead 上"""
    import asyncio

    from src.intelligence import lead_service as ls_mod
    from src.intelligence.rule_scorer import ScoreResult
    from src.models.comment import LiveComment, MessageType

    captured: dict = {}

    class _FakeDB:
        _db = object()

        async def upsert_lead(self, lead):
            captured["lead"] = lead
            return 1

        async def get_lead(self, lead_id):
            return {}

    svc = ls_mod.LeadService.__new__(ls_mod.LeadService)
    svc.config = type("C", (), {
        "lead": type("L", (), {"realtime_enabled": True, "realtime_push": False})(),
        "notify": type("N", (), {"push_levels": [], "contact_only": False})(),
    })()
    svc.stats = {"scanned": 0, "hits": 0, "excluded": 0, "pushed": 0,
                 "tabled": 0, "dropped": 0}
    svc._in_cooldown = lambda k: False
    svc._push = None

    async def _fake_sync(db, lead, level):
        return None

    svc._maybe_sync_bitable = _fake_sync

    comment = LiveComment(
        content="置换",
        message_type=MessageType.CHAT,
        user_id="u1",
        user_sec_id=VIEWER_SEC,
        user_display_id=VIEWER_DISPLAY,
        user_nickname="陈焱燚",
        room_id="r1",
        monitor_name="小米汽车｜合肥滨湖悦方体验店",
        anchor_id="MS4wLjABAAAA_主播",
    )
    comment.create_time = "2026-11-17 13:53:56"

    result = ScoreResult(score=32, level="medium", signals=["金融补贴"], car_model="")

    # lead_service 是 `from ..storage.db_storage import get_db` 直接引用的，
    # 得打在 lead_service 模块上，打在 db_storage 上没用
    orig_get_db = ls_mod.get_db
    ls_mod.get_db = lambda: _FakeDB()
    try:
        asyncio.new_event_loop().run_until_complete(
            svc._handle((comment, result))
        )
    finally:
        ls_mod.get_db = orig_get_db

    assert "lead" in captured, "线索根本没落库（_handle 提前返回了）"
    lead = captured["lead"]
    assert lead.sec_uid == VIEWER_SEC, "弹幕里的 secUid 没有带到线索上"
    assert lead.display_id == VIEWER_DISPLAY


# ---------------------------------------------------------------- 落库

class _AsyncCursor:
    """把 sqlite3 的同步 cursor 包成 aiosqlite 那样的可 await 对象"""

    def __init__(self, cur):
        self._cur = cur

    async def fetchone(self):
        return self._cur.fetchone()

    async def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount


class _AsyncDB:
    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql, params=None):
        return _AsyncCursor(self._conn.execute(sql, params or []))

    async def commit(self):
        self._conn.commit()


def _make_store(tmp_path):
    """建一个内存 sqlite，灌出 upsert_lead 需要的最小环境"""
    import sqlite3

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_key TEXT, user_id TEXT, display_id TEXT, nickname TEXT, avatar TEXT,
        room_id TEXT, monitor_name TEXT, live_room TEXT, store TEXT,
        intent_level TEXT, intent_score INTEGER, intent_reason TEXT, car_model TEXT,
        contact_type TEXT, contact_value TEXT,
        evidence TEXT, evidence_count INTEGER, comment_count INTEGER, last_hit_at TEXT,
        status TEXT, pushed_at TEXT, outreach_status TEXT, outreach_at TEXT,
        outreach_note TEXT, source TEXT, created_at TEXT, updated_at TEXT,
        sec_uid TEXT DEFAULT '', bitable_record_id TEXT DEFAULT '',
        bitable_synced_status TEXT DEFAULT '', not_found_count INTEGER DEFAULT 0,
        no_works INTEGER DEFAULT 0
    )""")
    return conn


def test_upsert_lead_inserts_sec_uid():
    """INSERT 时要把 sec_uid 写进去"""
    import asyncio

    from src.storage.db_storage import DBStorage

    conn = _make_store(None)
    store = DBStorage.__new__(DBStorage)
    store._db = _AsyncDB(conn)

    lead = Lead(lead_key="k1", display_id=VIEWER_DISPLAY, sec_uid=VIEWER_SEC,
                nickname="陈焱燚", intent_level="medium", intent_score=32,
                intent_reason="命中信号：金融补贴")

    asyncio.new_event_loop().run_until_complete(store.upsert_lead(lead))
    row = conn.execute("SELECT sec_uid FROM leads WHERE lead_key='k1'").fetchone()
    assert row is not None, "线索没落库"
    assert row["sec_uid"] == VIEWER_SEC


def test_upsert_lead_fills_empty_sec_uid_but_never_overwrites():
    """UPDATE 时：空的就填上，已有值的不能被覆盖"""
    import asyncio

    from src.storage.db_storage import DBStorage

    conn = _make_store(None)
    store = DBStorage.__new__(DBStorage)
    store._db = _AsyncDB(conn)

    loop = asyncio.new_event_loop()

    old = Lead(lead_key="k2", display_id="d2", sec_uid="OLD_SEC",
               nickname="老线索", intent_level="high", intent_score=80,
               intent_reason="r")
    loop.run_until_complete(store.upsert_lead(old))

    # 已经有值了 → 再来一条不能覆盖
    again = Lead(lead_key="k2", display_id="d2", sec_uid="NEW_SEC",
                 nickname="老线索", intent_level="medium", intent_score=32,
                 intent_reason="r2")
    loop.run_until_complete(store.upsert_lead(again))
    row = conn.execute("SELECT sec_uid FROM leads WHERE lead_key='k2'").fetchone()
    assert row["sec_uid"] == "OLD_SEC", "已有 sec_uid 被覆盖了"

    # 空值 → 补上
    empty = Lead(lead_key="k3", display_id="d3", sec_uid="",
                 nickname="空线索", intent_level="medium", intent_score=32,
                 intent_reason="r")
    loop.run_until_complete(store.upsert_lead(empty))
    fill = Lead(lead_key="k3", display_id="d3", sec_uid="FILLED_SEC",
                nickname="空线索", intent_level="medium", intent_score=32,
                intent_reason="r")
    loop.run_until_complete(store.upsert_lead(fill))
    row = conn.execute("SELECT sec_uid FROM leads WHERE lead_key='k3'").fetchone()
    assert row["sec_uid"] == "FILLED_SEC", "空的 sec_uid 没被补上"


# ---------------------------------------------------------------- 回扫路径

def test_rule_scanner_carries_sec_uid():
    """规则回扫建线索时也要带 sec_uid"""
    import inspect

    from src.intelligence import rule_scanner

    src = inspect.getsource(rule_scanner)
    assert 'sec_uid=g.get("sec_uid")' in src, "规则回扫没带 sec_uid"


def test_intent_analyzer_carries_sec_uid():
    """LLM 批量判定建线索时也要带 sec_uid"""
    import inspect

    from src.intelligence import intent_analyzer

    src = inspect.getsource(intent_analyzer)
    assert 'sec_uid=item.get("sec_uid")' in src, "LLM 判定没带 sec_uid"
    assert '"sec_uid": g.get("sec_uid")' in src, "LLM 候选里没带 sec_uid"


def test_grouped_users_query_exposes_sec_uid():
    """分组查询要能把 secUid 一起取出来（回扫的数据来源）"""
    import inspect

    from src.storage import db_storage

    src = inspect.getsource(db_storage)
    assert "MAX(NULLIF(user_sec_id, '')) AS sec_uid" in src, "分组查询没取 sec_uid"


# ---------------------------------------------------------------- 回归护栏

def test_upsert_lead_select_covers_every_row_reference():
    """**SELECT 的列必须覆盖 body 里所有 row[...] 引用**

    踩过的坑（2026-11-17 定位，生产真实炸过 23 次）
    ------------------------------------------------
    ``upsert_lead`` 的 SELECT 只取了 8 列，但合并分支读 ``row["car_model"]``
    → sqlite3.Row 读不到 → IndexError → 整个 upsert 抛掉
    → lead_service 只记一句 ERROR 就返回 → **线索更新被静默丢弃**。

    触发条件：老线索已在池里 + 新评论意向等级**更低** → 走 else 分支 → 炸。
    危害：越活跃的人越容易被丢更新（第一次分高 → 之后不再更新热度）。

    这个测试做静态审计：SELECT 里没有的列，只要 body 引用了，就报错。
    """
    import inspect
    import re

    from src.storage import db_storage

    body = inspect.getsource(db_storage.DBStorage.upsert_lead)
    m = re.search(r"SELECT (.*?) FROM leads WHERE lead_key", body, re.S)
    assert m, "没找到 upsert_lead 里的 SELECT"
    selected = set(re.findall(r"\b(\w+)\b", m.group(1)))
    used = set(re.findall(r'row\["(\w+)"\]', body))

    missing = used - selected
    assert not missing, (
        f"upsert_lead 的 SELECT 少取了列 {sorted(missing)} —— "
        f"body 里引用了 row[{sorted(missing)[0]!r}]，读到会抛 IndexError，"
        f"导致整条线索更新被静默丢弃"
    )


def test_upsert_lead_update_with_lower_intent_does_not_crash():
    """老线索 + 新评论意向等级更低 → 必须正常更新，不能抛 IndexError

    这是上面那个 bug 的行为级回归：
    更新失败时 evidence 不累加、last_hit_at 不刷新 → 线索显得很冷。
    """
    import asyncio

    from src.storage.db_storage import DBStorage

    conn = _make_store(None)
    store = DBStorage.__new__(DBStorage)
    store._db = _AsyncDB(conn)
    loop = asyncio.new_event_loop()

    # 先建一条 high 分的线索
    first = Lead(lead_key="k9", display_id="d9", nickname="老客户",
                 intent_level="high", intent_score=80, intent_reason="命中信号：价格询问",
                 car_model="SU7", evidence='["多少钱"]', evidence_count=1,
                 comment_count=1, last_hit_at="2026-11-16 10:00:00")
    loop.run_until_complete(store.upsert_lead(first))

    # 再来一条**更低**意向的评论（走 else 合并分支）
    second = Lead(lead_key="k9", display_id="d9", nickname="老客户",
                  intent_level="low", intent_score=10, intent_reason="命中信号：随便看看",
                  car_model="", evidence='["在吗"]', evidence_count=1,
                  comment_count=1, last_hit_at="2026-11-17 14:00:00")
    loop.run_until_complete(store.upsert_lead(second))   # 以前这里抛 IndexError

    row = conn.execute(
        "SELECT intent_level, intent_score, evidence_count, comment_count, "
        "last_hit_at, evidence FROM leads WHERE lead_key='k9'").fetchone()
    assert row["evidence_count"] == 2, "证据没累加 → 更新被丢了"
    assert row["comment_count"] == 2, "弹幕数没累加"
    assert row["last_hit_at"] == "2026-11-17 14:00:00", "最后命中时间没刷新"
    assert row["intent_level"] == "high", "意向等级应保留更高的那个"
