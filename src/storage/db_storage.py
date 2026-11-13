"""SQLite 存储引擎 — 评论数据的持久化存储与查询"""

from __future__ import annotations

import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import get_config
from ..intelligence.contact_extractor import merge_contact
from ..models.comment import LiveComment, VideoComment, ScrapeTask, MessageType
from ..models.lead import Lead, OutreachRecord, INTENT_LEVEL_ORDER
from ..outreach.store import (
    CREATE_OUTREACH_RECORDS_INDEXES_SQL,
    CREATE_TABLE_OUTREACH_RECORDS_SQL,
)
from src.utils.logger import get_logger


log = get_logger("db_storage")


def _intent_rank(level: str) -> int:
    """意向等级排序值，越大优先级越高"""
    return INTENT_LEVEL_ORDER.get(level, 0)


def _merge_evidence(old_json: str, new_json: str, cap: int = 10) -> str:
    """把老证据和新证据**合并**（去重、限长）

    为什么以前要改
    --------------
    以前 upsert 更新时 ``evidence = ?`` 直接整个覆盖，
    而写入方每次只带**当前这一条**弹幕
    （``json.dumps([comment.content[:200]])``）。
    结果就是：
      · ``evidence_count`` 在累加（表里显示「5 条证据」）
      · ``evidence`` 内容却**只剩最后一句**

    用户看到的现象就是「同一个人说的内容只保留了一句」。

    现在改成：老证据 + 新证据，去重后保留最近 ``cap`` 条。
    （``evidence_count`` 仍然是累计总数，不受这里的截断影响。）
    """
    def _load(s: str) -> list[str]:
        try:
            v = json.loads(s or "[]")
        except Exception:
            return []
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        # 老数据可能是裸字符串
        return [str(v)] if str(v or "").strip() else []

    out: list[str] = []
    for item in _load(old_json) + _load(new_json):
        item = item.strip()
        if not item or item in out:
            continue
        out.append(item)
    return json.dumps(out[-cap:], ensure_ascii=False)


# SQL 语句常量
CREATE_TABLE_LIVE_SQL = """
CREATE TABLE IF NOT EXISTS live_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_type TEXT NOT NULL DEFAULT 'WebcastChatMessage',
    content TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    user_nickname TEXT DEFAULT '',
    user_avatar TEXT DEFAULT '',
    room_id TEXT DEFAULT '',
    anchor_id TEXT DEFAULT '',
    anchor_name TEXT DEFAULT '',
    monitor_name TEXT DEFAULT '',
    create_time TEXT NOT NULL,
    gift_name TEXT,
    gift_count INTEGER,
    like_count INTEGER,
    raw_data TEXT,
    collected_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_live_room ON live_comments(room_id);",
    "CREATE INDEX IF NOT EXISTS idx_live_anchor ON live_comments(anchor_id);",
    "CREATE INDEX IF NOT EXISTS idx_live_type ON live_comments(message_type);",
    "CREATE INDEX IF NOT EXISTS idx_live_create_time ON live_comments(create_time);",
    "CREATE INDEX IF NOT EXISTS idx_live_collected_at ON live_comments(collected_at);",
]

CREATE_TABLE_VIDEO_SQL = """
CREATE TABLE IF NOT EXISTS video_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'douyin',
    task_id TEXT DEFAULT '',
    monitor_name TEXT DEFAULT '',
    video_id TEXT DEFAULT '',
    video_url TEXT DEFAULT '',
    video_title TEXT DEFAULT '',
    video_author TEXT DEFAULT '',
    content TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    user_nickname TEXT DEFAULT '',
    like_count INTEGER DEFAULT 0,
    reply_count INTEGER DEFAULT 0,
    publish_time TEXT DEFAULT '',
    source_keyword TEXT DEFAULT '',
    collected_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

CREATE_VIDEO_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_video_source ON video_comments(source);",
    "CREATE INDEX IF NOT EXISTS idx_video_task ON video_comments(task_id);",
    "CREATE INDEX IF NOT EXISTS idx_video_monitor ON video_comments(monitor_name);",
    "CREATE INDEX IF NOT EXISTS idx_video_video_id ON video_comments(video_id);",
    "CREATE INDEX IF NOT EXISTS idx_video_collected_at ON video_comments(collected_at);",
    "CREATE INDEX IF NOT EXISTS idx_video_keyword ON video_comments(source_keyword);",
]

CREATE_TABLE_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS scrape_tasks (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    mode TEXT NOT NULL,
    params TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    progress TEXT DEFAULT '{}',
    result_count INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CREATE_TASKS_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_task_source ON scrape_tasks(source);",
    "CREATE INDEX IF NOT EXISTS idx_task_status ON scrape_tasks(status);",
    "CREATE INDEX IF NOT EXISTS idx_task_created ON scrape_tasks(created_at);",
]

CREATE_TABLE_ANALYSIS_SQL = """
CREATE TABLE IF NOT EXISTS analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT DEFAULT '',
    analysis_type TEXT NOT NULL,
    source_type TEXT DEFAULT '',
    source_filter TEXT DEFAULT '',
    result_data TEXT NOT NULL,
    comment_count INTEGER DEFAULT 0,
    llm_provider TEXT DEFAULT '',
    llm_model TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

CREATE_ANALYSIS_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_analysis_type ON analysis_results(analysis_type);",
    "CREATE INDEX IF NOT EXISTS idx_analysis_created ON analysis_results(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_analysis_task ON analysis_results(task_id);",
]

CREATE_TABLE_LEADS_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_key TEXT NOT NULL DEFAULT '',
    user_id TEXT DEFAULT '',
    display_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    avatar TEXT DEFAULT '',
    room_id TEXT DEFAULT '',
    monitor_name TEXT DEFAULT '',
    live_room TEXT DEFAULT '',
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
    -- 抖音 sec_uid（定位到用户后记下来，下次直接开主页，不用再搜索）
    sec_uid TEXT DEFAULT '',
    source TEXT DEFAULT 'rule',
    bitable_record_id TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

CREATE_LEADS_INDEXES_SQL = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_key ON leads(lead_key);",
    "CREATE INDEX IF NOT EXISTS idx_leads_display ON leads(display_id);",
    "CREATE INDEX IF NOT EXISTS idx_leads_intent ON leads(intent_level);",
    "CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);",
    "CREATE INDEX IF NOT EXISTS idx_leads_outreach ON leads(outreach_status);",
    "CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(intent_score);",
]

# 每轮触达任务的摘要（看板展示 + 排障）
# ⚠️ 建表 SQL 的真相来源在 src/outreach/store.py ——
# 这个表是触达线程写的，表归它管；store 那边还会自己补列（老库迁移）。
from ..outreach.store import (  # noqa: E402
    CREATE_OUTREACH_RUNS_INDEXES_SQL,
    CREATE_TABLE_OUTREACH_RUNS_SQL,
)


class DBStorage:
    """异步 SQLite 数据库存储引擎"""

    # 最大缓冲区大小，防止内存溢出
    MAX_BUFFER_SIZE = 10000

    def __init__(self, db_path: Optional[str] = None):
        config = get_config()
        self.db_path = db_path or config.storage.db_path
        self.db_path = Path(self.db_path).resolve()
        self._db: Optional[aiosqlite.Connection] = None
        self._pending_buffer: list[LiveComment] = []
        self._flush_tasks: list[asyncio.Task] = []  # 跟踪刷写任务，避免静默丢失

    async def init_db(self):
        """初始化数据库（建表+索引）"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # 写锁最多等 5 秒再报 "database is locked"，避免无限期挂起
        await self._db.execute("PRAGMA busy_timeout=5000")
        
        await self._db.executescript(CREATE_TABLE_LIVE_SQL)
        for sql in CREATE_INDEXES_SQL:
            await self._db.execute(sql)
        await self._db.commit()
        
        # 数据库迁移：添加 monitor_name 字段
        try:
            await self._db.execute("ALTER TABLE live_comments ADD COLUMN monitor_name TEXT DEFAULT ''")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass  # 字段已存在
        
        # 创建 monitor_name 索引
        try:
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_live_monitor ON live_comments(monitor_name)")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 数据库迁移：添加 user_short_id 字段（抖音号）
        try:
            await self._db.execute("ALTER TABLE live_comments ADD COLUMN user_short_id TEXT DEFAULT ''")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass  # 字段已存在

        # 创建 user_short_id 索引（用于快速查找有抖音号的用户）
        try:
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_live_short_id ON live_comments(user_short_id)")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 数据库迁移：添加 user_display_id 字段（抖音 displayId，平台分配的不变 ID）
        try:
            await self._db.execute("ALTER TABLE live_comments ADD COLUMN user_display_id TEXT DEFAULT ''")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass  # 字段已存在

        # 创建 user_display_id 索引（用于反查 user profile 等场景）
        try:
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_live_display_id ON live_comments(user_display_id)")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 创建视频评论表
        await self._db.executescript(CREATE_TABLE_VIDEO_SQL)
        
        # 数据库迁移：为已存在的 video_comments 表添加 monitor_name 字段（先迁移，再创建索引）
        try:
            await self._db.execute("ALTER TABLE video_comments ADD COLUMN monitor_name TEXT DEFAULT ''")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass  # 字段已存在
        
        # 创建索引（此时字段一定存在）
        for sql in CREATE_VIDEO_INDEXES_SQL:
            await self._db.execute(sql)
        
        # 创建抓取任务表
        await self._db.executescript(CREATE_TABLE_TASKS_SQL)
        for sql in CREATE_TASKS_INDEXES_SQL:
            await self._db.execute(sql)
        
        # 创建分析结果表（LLM分析结果）
        await self._create_analysis_table()

        # 创建线索表（购车意向）与抖音触达记录表
        await self._create_leads_tables()

        await self._db.commit()
        
        log.info(f"数据库初始化完成: {self.db_path}")
    
    async def _create_analysis_table(self):
        """创建分析结果表"""
        await self._db.executescript(CREATE_TABLE_ANALYSIS_SQL)
        for sql in CREATE_ANALYSIS_INDEXES_SQL:
            await self._db.execute(sql)

    async def _create_leads_tables(self):
        """创建线索表与抖音触达记录表（幂等，可重复调用）"""
        await self._db.executescript(CREATE_TABLE_LEADS_SQL)
        for sql in CREATE_LEADS_INDEXES_SQL:
            await self._db.execute(sql)
        await self._db.executescript(CREATE_TABLE_OUTREACH_RECORDS_SQL)
        for sql in CREATE_OUTREACH_RECORDS_INDEXES_SQL:
            await self._db.execute(sql)
        # 迁移：老库补 source 字段（rule=规则判定 / llm=AI 判定）
        try:
            await self._db.execute("ALTER TABLE leads ADD COLUMN source TEXT DEFAULT 'rule'")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 迁移：老库补 bitable_record_id 字段（飞书多维表格的记录 ID，用于去重更新）
        try:
            await self._db.execute(
                "ALTER TABLE leads ADD COLUMN bitable_record_id TEXT DEFAULT ''"
            )
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 迁移：老库补 bitable_synced_status 字段
        # 记录「上次写入飞书时表里的处理状态」，用于判断本地状态是否真的变了。
        # 销售在飞书表里手动改的状态是权威值，不能被本地同步覆盖回去。
        try:
            await self._db.execute(
                "ALTER TABLE leads ADD COLUMN bitable_synced_status TEXT DEFAULT ''"
            )
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 迁移：老库补 store 字段（归一化后的门店短名）
        # 配置里同一个门店有多种写法（｜/丨/|/空格），归一化后才能按门店筛选。
        try:
            await self._db.execute("ALTER TABLE leads ADD COLUMN store TEXT DEFAULT ''")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 迁移：老库补 sec_uid（抖音 sec_uid）
        #
        # 有了它，下次触达同一个线索**不用再搜索**——
        # 搜索接口容易被抖音限流（静默返回空），
        # 能直接开主页就少一次被限流的机会。
        try:
            await self._db.execute("ALTER TABLE leads ADD COLUMN sec_uid TEXT DEFAULT ''")
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass
        try:
            await self._db.execute(
                "UPDATE leads SET sec_uid = ("
                "  SELECT r.sec_uid FROM outreach_records r"
                "  WHERE r.sec_uid != '' AND r.display_id = leads.display_id"
                "  ORDER BY r.created_at DESC LIMIT 1"
                ") WHERE (sec_uid IS NULL OR sec_uid = '')"
            )
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 迁移：老库补 last_hit_at（最后一次抓到该线索的时间）
        #
        # 触达排序要用它："最近抓到的线索先留言"（用户要求）。
        # 老数据先回填成 created_at —— 至少有值，
        # 精确回填（从 live_comments 关联）用 scripts/backfill_lead_last_hit.py。
        try:
            await self._db.execute(
                "ALTER TABLE leads ADD COLUMN last_hit_at TEXT DEFAULT ''"
            )
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass
        try:
            await self._db.execute(
                "UPDATE leads SET last_hit_at = created_at "
                "WHERE last_hit_at IS NULL OR last_hit_at = ''"
            )
            await self._db.commit()
        except aiosqlite.OperationalError:
            pass

        # 回填 store（老数据是空的）
        try:
            from ..notify.bitable import normalize_store

            cursor = await self._db.execute(
                "SELECT id, monitor_name FROM leads WHERE (store IS NULL OR store = '')"
                " AND monitor_name != ''"
            )
            rows = await cursor.fetchall()
            for row in rows:
                name = normalize_store(row["monitor_name"] or "")
                if name:
                    await self._db.execute(
                        "UPDATE leads SET store = ? WHERE id = ?", [name, row["id"]]
                    )
            if rows:
                await self._db.commit()
                log.info(f"回填门店字段：{len(rows)} 条")
        except Exception as e:
            log.warning(f"回填门店字段失败: {e}")

        # 迁移：触达记录补明细字段（v2.0.11 接入自动触达时加）
        for col, ddl in (
            ("record_id", "TEXT DEFAULT ''"),
            ("sec_uid", "TEXT DEFAULT ''"),
            ("aweme_id", "TEXT DEFAULT ''"),
            ("aweme_desc", "TEXT DEFAULT ''"),
            ("template_id", "TEXT DEFAULT ''"),
            ("channel", "TEXT DEFAULT 'comment'"),
            ("verified", "INTEGER DEFAULT 0"),
        ):
            try:
                await self._db.execute(
                    f"ALTER TABLE outreach_records ADD COLUMN {col} {ddl}"
                )
                await self._db.commit()
            except aiosqlite.OperationalError:
                pass

        # 触达运行记录（每轮一行摘要，用于看板和排障）
        await self._db.executescript(CREATE_TABLE_OUTREACH_RUNS_SQL)
        for sql in CREATE_OUTREACH_RUNS_INDEXES_SQL:
            await self._db.execute(sql)

    async def save_analysis_result(
        self,
        analysis_type: str,
        result_data: str,
        source_type: str = "",
        source_filter: str = "",
        comment_count: int = 0,
        llm_provider: str = "",
        llm_model: str = "",
        task_id: str = "",
    ) -> int:
        """保存分析结果"""
        cursor = await self._db.execute(
            """INSERT INTO analysis_results (
                task_id, analysis_type, source_type, source_filter, result_data,
                comment_count, llm_provider, llm_model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
            [task_id, analysis_type, source_type, source_filter, result_data,
             comment_count, llm_provider, llm_model],
        )
        await self._db.commit()
        return cursor.lastrowid

    # ============================================================
    # 线索（购车意向）
    # ============================================================

    async def upsert_lead(self, lead: Lead, commit: bool = True) -> int:
        """
        写入或更新一条线索（按 lead_key 幂等）。

        同一条线索再次命中时，累积证据条数、保留更高的意向等级，
        并用新出现的联系方式覆盖空值。

        Returns:
            lead 的数据库主键
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = await self._db.execute(
            "SELECT id, intent_level, intent_score, contact_type, contact_value, "
            "evidence_count, comment_count, evidence FROM leads WHERE lead_key = ?",
            [lead.lead_key],
        )
        row = await existing.fetchone()

        # 门店归一化：配置里同一个门店有多种写法，统一成短名
        store = ""
        if lead.monitor_name:
            from ..notify.bitable import normalize_store
            store = normalize_store(lead.monitor_name)

        if row is None:
            cursor = await self._db.execute(
                """INSERT INTO leads (
                    lead_key, user_id, display_id, nickname, avatar,
                    room_id, monitor_name, live_room, store,
                    intent_level, intent_score, intent_reason, car_model,
                    contact_type, contact_value,
                    evidence, evidence_count, comment_count, last_hit_at,
                    status, pushed_at, outreach_status, outreach_at, outreach_note,
                    source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    lead.lead_key, lead.user_id, lead.display_id, lead.nickname, lead.avatar,
                    lead.room_id, lead.monitor_name, lead.live_room, store,
                    lead.intent_level, lead.intent_score, lead.intent_reason, lead.car_model,
                    lead.contact_type, lead.contact_value,
                    lead.evidence, lead.evidence_count, lead.comment_count,
                    (lead.last_hit_at or now),
                    lead.status, lead.pushed_at, lead.outreach_status, lead.outreach_at, lead.outreach_note,
                    lead.source or "rule", now, now,
                ],
            )
            if commit:
                await self._db.commit()
            return cursor.lastrowid

        # 已存在 → 合并更新
        old_level = row["intent_level"]
        old_score = row["intent_score"]
        old_contact_type = row["contact_type"]
        old_contact_value = row["contact_value"]
        old_evidence_count = row["evidence_count"] or 0
        old_comment_count = row["comment_count"] or 0

        # 意向等级取更高者，分数取更高者
        if _intent_rank(lead.intent_level) >= _intent_rank(old_level):
            new_level, new_score = lead.intent_level, lead.intent_score
            new_reason, new_model = lead.intent_reason, lead.car_model
        else:
            new_level, new_score = old_level, old_score
            new_reason = lead.intent_reason or row["intent_reason"]
            new_model = lead.car_model or row["car_model"]

        # 联系方式：按**类型**合并
        #
        # 为什么不能整体覆盖（2026-11-13）：
        # 联系方式有两个来源 —— 弹幕原文、抖音主页简介。
        # 整体覆盖会让「简介里的微信」被「弹幕里的电话」整条挤掉，
        # 白白丢掉另一个渠道拿到的联系方式。
        # 同类型取新的（弹幕是实时说的，更准），不同类型追加（两个都留着）。
        new_contact_type, new_contact_value = merge_contact(
            old_contact_type, old_contact_value,
            lead.contact_type, lead.contact_value,
            prefer_new_same_type=True,
        )

        # 证据是**累加**的，不是覆盖：
        # 同一个人说了三句，表里就该看得到三句（用户 2026-11-13 要求）。
        # 以前这里是 `evidence = ?` 直接盖掉，而写入方每次只带当前这一条，
        # 于是 evidence_count 在涨、内容却只剩最后一句。
        new_evidence = _merge_evidence(row["evidence"] or "", lead.evidence or "")

        await self._db.execute(
            """UPDATE leads SET
                user_id = COALESCE(NULLIF(?, ''), user_id),
                display_id = COALESCE(NULLIF(?, ''), display_id),
                nickname = COALESCE(NULLIF(?, ''), nickname),
                avatar = COALESCE(NULLIF(?, ''), avatar),
                room_id = COALESCE(NULLIF(?, ''), room_id),
                monitor_name = COALESCE(NULLIF(?, ''), monitor_name),
                store = COALESCE(NULLIF(?, ''), store),
                live_room = COALESCE(NULLIF(?, ''), live_room),
                intent_level = ?, intent_score = ?, intent_reason = ?, car_model = ?,
                contact_type = ?, contact_value = ?,
                evidence = ?, evidence_count = ?, comment_count = ?,
                last_hit_at = COALESCE(NULLIF(?, ''), last_hit_at),
                updated_at = ?
            WHERE lead_key = ?""",
            [
                lead.user_id, lead.display_id, lead.nickname, lead.avatar,
                lead.room_id, lead.monitor_name, store, lead.live_room,
                new_level, new_score, new_reason, new_model,
                new_contact_type, new_contact_value,
                new_evidence, old_evidence_count + lead.evidence_count,
                old_comment_count + lead.comment_count,
                lead.last_hit_at, now, lead.lead_key,
            ],
        )
        if commit:
            await self._db.commit()
        return row["id"]

    async def upsert_leads_batch(self, leads: list[Lead]) -> int:
        """
        批量写入线索：整批只提交一次。

        逐条 commit 会在有其他进程/连接持有写锁时长时间阻塞
        （每条都要等 fsync + 锁竞争），整批单事务提交快一个数量级。

        Args:
            leads: 待写入的线索列表

        Returns:
            成功写入的条数
        """
        if not leads:
            return 0
        written = 0
        try:
            for lead in leads:
                lead_id = await self.upsert_lead(lead, commit=False)
                # 回填主键，调用方（如规则回扫）要靠它回写 record_id 等字段
                lead.id = lead_id
                written += 1
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        return written

    async def query_leads(
        self,
        intent_levels: Optional[list[str]] = None,
        status: Optional[str] = None,
        outreach_status: Optional[str] = None,
        has_contact: Optional[bool] = None,
        keyword: Optional[str] = None,
        store: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """查询线索列表，返回 (结果, 总条数)。按意向分降序。"""
        conditions = []
        params: list = []

        if intent_levels:
            placeholders = ",".join("?" for _ in intent_levels)
            conditions.append(f"intent_level IN ({placeholders})")
            params.extend(intent_levels)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if outreach_status:
            conditions.append("outreach_status = ?")
            params.append(outreach_status)
        if has_contact is True:
            conditions.append("contact_value != ''")
        elif has_contact is False:
            conditions.append("contact_value = ''")
        if store:
            conditions.append("store = ?")
            params.append(store)
        if keyword:
            conditions.append("(nickname LIKE ? OR display_id LIKE ? OR intent_reason LIKE ?)")
            params.extend([f"%{keyword}%"] * 3)

        where = " AND ".join(conditions) if conditions else "1=1"

        count_row = await self._db.execute(f"SELECT COUNT(*) FROM leads WHERE {where}", params)
        total = (await count_row.fetchone())[0]

        cursor = await self._db.execute(
            f"""SELECT * FROM leads WHERE {where}
                ORDER BY intent_score DESC, id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows], total

    async def get_lead(self, lead_id: int) -> Optional[dict]:
        """按主键取单条线索"""
        cursor = await self._db.execute("SELECT * FROM leads WHERE id = ?", [lead_id])
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_leads_by_ids(self, lead_ids: list[int]) -> list[dict]:
        """批量取线索"""
        if not lead_ids:
            return []
        placeholders = ",".join("?" for _ in lead_ids)
        cursor = await self._db.execute(f"SELECT * FROM leads WHERE id IN ({placeholders})", lead_ids)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_leads_by_keys(self, lead_keys: list[str]) -> list[dict]:
        """按 lead_key 批量取线索

        用于同步飞书多维表格前取回真实状态（record_id / 上次同步标记）。
        """
        if not lead_keys:
            return []
        keys = [k for k in lead_keys if k]
        if not keys:
            return []
        out: list[dict] = []
        # SQLite 的变量上限是 999，分批查
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = await self._db.execute(
                f"SELECT * FROM leads WHERE lead_key IN ({placeholders})", chunk
            )
            rows = await cursor.fetchall()
            out.extend(dict(r) for r in rows)
        return out

    async def update_lead(self, lead_id: int, **fields) -> bool:
        """更新线索字段（白名单内）。返回是否命中。"""
        allowed = {
            "intent_level", "intent_score", "intent_reason", "car_model",
            "contact_type", "contact_value", "status", "pushed_at",
            "outreach_status", "outreach_at", "outreach_note",
            "nickname", "display_id", "live_room", "bitable_record_id",
            "bitable_synced_status",
            "evidence", "evidence_count",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        values.append(lead_id)
        cursor = await self._db.execute(f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?", values)
        await self._db.commit()
        return cursor.rowcount > 0

    async def mark_leads_pushed(self, lead_ids: list[int]) -> int:
        """把线索标记为已推送飞书"""
        if not lead_ids:
            return 0
        placeholders = ",".join("?" for _ in lead_ids)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._db.execute(
            f"UPDATE leads SET status = 'pushed', pushed_at = ?, updated_at = ? WHERE id IN ({placeholders})",
            [now, now] + lead_ids,
        )
        await self._db.commit()
        return cursor.rowcount

    async def count_leads(self, intent_levels: Optional[list[str]] = None) -> int:
        """统计线索数量"""
        if intent_levels:
            placeholders = ",".join("?" for _ in intent_levels)
            cursor = await self._db.execute(
                f"SELECT COUNT(*) FROM leads WHERE intent_level IN ({placeholders})", intent_levels
            )
        else:
            cursor = await self._db.execute("SELECT COUNT(*) FROM leads")
        return (await cursor.fetchone())[0]

    async def delete_leads(self, lead_ids: list[int]) -> int:
        """删除线索"""
        if not lead_ids:
            return 0
        placeholders = ",".join("?" for _ in lead_ids)
        cursor = await self._db.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", lead_ids)
        await self._db.commit()
        return cursor.rowcount

    # ============================================================
    # 抖音触达记录
    # ============================================================

    async def insert_outreach_record(self, record: OutreachRecord) -> None:
        """写入一条触达记录"""
        await self._db.execute(
            """INSERT INTO outreach_records (
                id, lead_id, display_id, nickname, target_url, message, status, error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                record.id, record.lead_id, record.display_id, record.nickname,
                record.target_url, record.message, record.status, record.error,
                record.created_at, record.updated_at,
            ],
        )
        await self._db.commit()

    async def update_outreach_record(self, record_id: str, **fields) -> bool:
        """更新触达记录状态"""
        allowed = {"status", "error", "target_url", "message", "lead_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        values.append(record_id)
        cursor = await self._db.execute(f"UPDATE outreach_records SET {sets}, updated_at = ? WHERE id = ?", values)
        await self._db.commit()
        return cursor.rowcount > 0

    async def query_outreach_records(self, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """查询触达记录"""
        count_row = await self._db.execute("SELECT COUNT(*) FROM outreach_records")
        total = (await count_row.fetchone())[0]
        cursor = await self._db.execute(
            "SELECT * FROM outreach_records ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            [limit, offset],
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows], total

    async def count_outreach_today(self) -> int:
        """统计今日已执行的触达次数（用于每日上限控制）"""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM outreach_records WHERE date(created_at) = date('now', 'localtime') "
            "AND status IN ('searching', 'found', 'commented')"
        )
        return (await cursor.fetchone())[0]

    async def get_existing_lead_keys(self, keys: list[str]) -> set[str]:
        """查询哪些 lead_key 已经存在（用于跳过已分析用户）"""
        if not keys:
            return set()
        found: set[str] = set()
        # SQLite 参数数量上限约 999，分片查询
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            cursor = await self._db.execute(
                f"SELECT lead_key FROM leads WHERE lead_key IN ({placeholders})", chunk
            )
            rows = await cursor.fetchall()
            found.update(r["lead_key"] for r in rows)
        return found

    async def query_user_activity(
        self,
        since: datetime,
        limit_users: int = 300,
        min_messages: int = 1,
        message_type: str = "WebcastChatMessage",
    ) -> list[dict]:
        """
        按用户聚合指定时间窗内的弹幕。

        时间比较说明：collected_at 由 Python 以 ISO8601（带 T）格式写入，
        与 SQLite 的 datetime('now','localtime')（空格分隔）字符串比较会失真
        （'T' > ' ' 导致历史数据全部命中），因此阈值必须由调用方用 Python
        计算并以同样的 isoformat() 格式传入。

        Args:
            since: 起始时间（datetime），会用 isoformat() 转换
            limit_users: 最多返回多少个用户
            min_messages: 用户至少要有多少条弹幕才纳入
            message_type: 消息类型

        Returns:
            [{"ukey","user_id","display_id","nickname","room_id","monitor_name",
              "count","messages":[...]}]
        """
        cursor = await self._db.execute(
            """SELECT
                   COALESCE(NULLIF(user_display_id, ''), NULLIF(user_id, ''),
                            room_id || ':' || user_nickname) AS ukey,
                   MAX(user_id) AS user_id,
                   MAX(user_display_id) AS display_id,
                   MAX(user_nickname) AS nickname,
                   MAX(user_avatar) AS avatar,
                   MAX(room_id) AS room_id,
                   MAX(monitor_name) AS monitor_name,
                   COUNT(*) AS cnt,
                   MAX(create_time) AS last_at,
                   GROUP_CONCAT(content, '|||') AS msgs
               FROM live_comments
               WHERE message_type = ?
                 AND collected_at >= ?
                 AND content IS NOT NULL AND content != ''
                 AND COALESCE(user_id, '') NOT IN ('111111', '0')
               GROUP BY ukey
               HAVING cnt >= ?
               ORDER BY cnt DESC
               LIMIT ?""",
            [message_type, since.isoformat(), min_messages, limit_users],
        )
        rows = await cursor.fetchall()

        results = []
        for r in rows:
            messages = [m for m in (r["msgs"] or "").split("|||") if m]
            results.append({
                "ukey": r["ukey"] or "",
                "user_id": r["user_id"] or "",
                "display_id": r["display_id"] or "",
                "nickname": r["nickname"] or "",
                "avatar": r["avatar"] or "",
                "room_id": r["room_id"] or "",
                "monitor_name": r["monitor_name"] or "",
                "count": r["cnt"],
                "messages": messages,
                # 这个用户最后一次发言的时间（用来排「最近抓取优先」）
                "last_at": r["last_at"] or "",
            })
        return results

    async def get_lead_evidence(self, lead_key: str, limit: int = 5) -> list[str]:
        """取某个用户最近的弹幕原文（用于线索详情展示）"""
        cursor = await self._db.execute(
            """SELECT content FROM live_comments
               WHERE COALESCE(NULLIF(user_display_id, ''), user_id) = ?
                 AND message_type = 'WebcastChatMessage'
                 AND content != ''
               ORDER BY id DESC LIMIT ?""",
            [lead_key, limit],
        )
        rows = await cursor.fetchall()
        return [r["content"] for r in rows]

    async def close(self):
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None
    
    async def insert_comment(self, comment: LiveComment) -> int:
        """插入一条评论"""
        cursor = await self._db.execute("""
            INSERT INTO live_comments (
                message_type, content, user_id, user_nickname, user_avatar,
                room_id, anchor_id, anchor_name, monitor_name, create_time,
                gift_name, gift_count, like_count, raw_data, collected_at,
                user_short_id, user_display_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            comment.message_type.value if hasattr(comment.message_type, 'value') else str(comment.message_type),
            comment.content,
            comment.user_id,
            comment.user_nickname,
            comment.user_avatar,
            comment.room_id,
            comment.anchor_id,
            comment.anchor_name,
            comment.monitor_name,
            comment.create_time.isoformat(),
            comment.gift_name,
            comment.gift_count,
            comment.like_count,
            comment.raw_data,
            comment.collected_at.isoformat(),
            getattr(comment, 'user_short_id', ''),     # 抖音号（可被用户改/搜）
            getattr(comment, 'user_display_id', ''),   # 平台分配的 displayId（不变，后端 API 用）
        ))
        await self._db.commit()
        return cursor.lastrowid

    async def batch_insert(self, comments: list[LiveComment]) -> int:
        """批量插入评论"""
        if not comments:
            return 0

        rows = [
            (
                c.message_type.value if hasattr(c.message_type, 'value') else str(c.message_type),
                c.content, c.user_id, c.user_nickname, c.user_avatar,
                c.room_id, c.anchor_id, c.anchor_name, c.monitor_name,
                c.create_time.isoformat(),
                c.gift_name, c.gift_count, c.like_count,
                c.raw_data, c.collected_at.isoformat(),
                getattr(c, 'user_short_id', ''),     # 抖音号
                getattr(c, 'user_display_id', ''),   # displayId
            )
            for c in comments
        ]

        await self._db.executemany("""
            INSERT INTO live_comments (
                message_type, content, user_id, user_nickname, user_avatar,
                room_id, anchor_id, anchor_name, monitor_name, create_time,
                gift_name, gift_count, like_count, raw_data, collected_at,
                user_short_id, user_display_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        await self._db.commit()
        return len(comments)
    
    def buffer_comment(self, comment: LiveComment):
        """将评论加入内存缓冲区（高频写入优化）"""
        self._pending_buffer.append(comment)
        # 防止缓冲区无限增长
        if len(self._pending_buffer) > self.MAX_BUFFER_SIZE:
            # 刷写一半的缓冲区，保留另一半
            half = self.MAX_BUFFER_SIZE // 2
            overflow = self._pending_buffer[:half]
            self._pending_buffer = self._pending_buffer[half:]
            # 异步刷写溢出的数据
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self.batch_insert(overflow))
                # 【修复】跟踪刷写任务，避免静默丢失
                self._flush_tasks.append(task)
                # 清理已完成的任务（避免任务列表无限增长）
                self._flush_tasks = [t for t in self._flush_tasks if not t.done()]
            except RuntimeError:
                # 【修复】事件循环不存在时同步写入，避免数据丢失
                log.warning("flush_buffer: 事件循环不存在，改用同步刷写")
                try:
                    import threading
                    t = threading.Thread(target=lambda: asyncio.run(self.batch_insert(overflow)), daemon=True)
                    t.start()
                except Exception as e:
                    log.error(f"flush_buffer: 同步刷写也失败了，数据丢失 {len(overflow)} 条: {e}")

    async def flush_buffer(self) -> int:
        """将缓冲区的数据刷写到数据库"""
        if not self._pending_buffer:
            return 0
        count = await self.batch_insert(self._pending_buffer)
        self._pending_buffer.clear()
        return count

    async def query_comments(
        self,
        room_id: Optional[str] = None,
        anchor_id: Optional[str] = None,
        message_type: Optional[MessageType] = None,
        keyword: Optional[str] = None,
        monitor_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """
        查询评论。
        
        Returns:
            (结果列表, 总条数)
        """
        conditions = []
        params = []
        
        if room_id:
            conditions.append("room_id = ?")
            params.append(room_id)
        if anchor_id:
            conditions.append("anchor_id = ?")
            params.append(anchor_id)
        if message_type:
            mt_val = message_type.value if hasattr(message_type, 'value') else str(message_type)
            conditions.append("message_type = ?")
            params.append(mt_val)
        if monitor_name:
            conditions.append("monitor_name = ?")
            params.append(monitor_name)
        if keyword:
            conditions.append("content LIKE ?")
            params.append(f"%{keyword}%")
        if start_time:
            conditions.append("create_time >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("create_time <= ?")
            params.append(end_time.isoformat())
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        # 总条数
        count_row = await self._db.execute(
            f"SELECT COUNT(*) FROM live_comments WHERE {where_clause}", params
        )
        total = (await count_row.fetchone())[0]
        
        # 分页查询
        query = f"""
            SELECT * FROM live_comments 
            WHERE {where_clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        
        return result, total
    
    async def get_statistics(
        self, 
        room_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> dict:
        """获取统计数据"""
        conditions = []
        params = []
        
        if room_id:
            conditions.append("room_id = ?")
            params.append(room_id)
        if start_time:
            conditions.append("collected_at >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("collected_at <= ?")
            params.append(end_time.isoformat())
        
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        
        stats = {"total": 0, "chat": 0, "gift": 0, "member_join": 0, "like": 0}
        
        row = await self._db.execute(
            f"SELECT COUNT(*) as cnt, message_type FROM live_comments {where} GROUP BY message_type",
            params,
        )
        async for r in row:
            mt = r["message_type"]
            stats["total"] += r["cnt"]
            if "Chat" in mt:
                stats["chat"] = r["cnt"]
            elif "Gift" in mt:
                stats["gift"] = r["cnt"]
            elif "Member" in mt:
                stats["member_join"] = r["cnt"]
            elif "Like" in mt:
                stats["like"] = r["cnt"]
        
        return stats
    
    async def delete_comments(
        self, 
        before_time: Optional[datetime] = None,
        room_id: Optional[str] = None,
    ) -> int:
        """删除旧数据（清理用）"""
        conditions = []
        params = []
        
        if before_time:
            conditions.append("collected_at < ?")
            params.append(before_time.isoformat())
        if room_id:
            conditions.append("room_id = ?")
            params.append(room_id)
        
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        
        cur = await self._db.execute(f"DELETE FROM live_comments {where}", params)
        await self._db.commit()
        return cur.rowcount
    
    # ============================================================
    # 视频评论 CRUD
    # ============================================================
    
    async def insert_video_comment(self, comment: VideoComment) -> int:
        """插入一条视频评论"""
        cursor = await self._db.execute("""
            INSERT INTO video_comments (
                source, task_id, video_id, video_url, video_title, video_author,
                content, user_id, user_nickname, like_count, reply_count,
                publish_time, source_keyword, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            comment.source, comment.task_id, comment.video_id, comment.video_url,
            comment.video_title, comment.video_author, comment.content,
            comment.user_id, comment.user_nickname, comment.like_count,
            comment.reply_count, comment.publish_time, comment.source_keyword,
            comment.collected_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        await self._db.commit()
        return cursor.lastrowid
    
    async def batch_insert_video_comments(self, comments: list[VideoComment]) -> int:
        """批量插入视频评论"""
        if not comments:
            return 0

        if self._db is None:
            log.error(f"[DB] _db is None，数据库未初始化！跳过 {len(comments)} 条评论入库")
            return 0

        rows = [
            (
                c.source, c.task_id, c.video_id, c.video_url, c.video_title,
                c.video_author, c.content, c.user_id, c.user_nickname,
                c.like_count, c.reply_count, c.publish_time, c.source_keyword,
                c.collected_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            for c in comments
        ]

        try:
            await self._db.executemany("""
                INSERT INTO video_comments (
                    source, task_id, video_id, video_url, video_title, video_author,
                    content, user_id, user_nickname, like_count, reply_count,
                    publish_time, source_keyword, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            await self._db.commit()
            log.info(f"[DB] 成功入库 {len(comments)} 条评论到 {self.db_path}")
            return len(comments)
        except Exception as e:
            log.error(f"[DB] 入库失败: {e}")
            return 0
    
    async def query_video_comments(
        self,
        source: Optional[str] = None,
        task_id: Optional[str] = None,
        video_id: Optional[str] = None,
        keyword: Optional[str] = None,
        monitor_name: Optional[str] = None,
        source_keyword: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """查询视频评论"""
        conditions = []
        params = []
        
        if source:
            conditions.append("source = ?")
            params.append(source)
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if video_id:
            conditions.append("video_id = ?")
            params.append(video_id)
        if keyword:
            conditions.append("content LIKE ?")
            params.append(f"%{keyword}%")
        if monitor_name:
            conditions.append("monitor_name = ?")
            params.append(monitor_name)
        if source_keyword:
            conditions.append("source_keyword = ?")
            params.append(source_keyword)
        if start_time:
            conditions.append("collected_at >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("collected_at <= ?")
            params.append(end_time.isoformat())
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        count_row = await self._db.execute(
            f"SELECT COUNT(*) FROM video_comments WHERE {where_clause}", params
        )
        total = (await count_row.fetchone())[0]
        
        query = f"""
            SELECT * FROM video_comments
            WHERE {where_clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        
        return result, total
    
    async def get_video_comment_texts(
        self,
        source: Optional[str] = None,
        monitor_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        keyword: Optional[str] = None,
        limit: int = 100000,
    ) -> list[str]:
        """获取视频评论文本列表（用于词云生成）"""
        conditions = []
        params = []
        
        if source:
            conditions.append("source = ?")
            params.append(source)
        if monitor_name:
            conditions.append("monitor_name = ?")
            params.append(monitor_name)
        if start_time:
            conditions.append("collected_at >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("collected_at <= ?")
            params.append(end_time.isoformat())
        if keyword:
            conditions.append("content LIKE ?")
            params.append(f"%{keyword}%")
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        cursor = await self._db.execute(
            f"SELECT content FROM video_comments WHERE {where_clause} AND content != '' LIMIT ?",
            params + [limit],
        )
        rows = await cursor.fetchall()
        return [row["content"] for row in rows]
    
    async def get_live_comment_texts(
        self,
        monitor_name: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        keyword: Optional[str] = None,
        limit: int = 100000,
    ) -> list[str]:
        """获取直播弹幕文本列表（用于词云生成）"""
        conditions = []
        params = []
        
        if monitor_name:
            conditions.append("monitor_name = ?")
            params.append(monitor_name)
        if start_time:
            conditions.append("create_time >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("create_time < ?")
            params.append(end_time.isoformat())
        if keyword:
            conditions.append("content LIKE ?")
            params.append(f"%{keyword}%")
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        cursor = await self._db.execute(
            f"SELECT content FROM live_comments WHERE {where_clause} AND content != '' LIMIT ?",
            params + [limit],
        )
        rows = await cursor.fetchall()
        return [row["content"] for row in rows]
    
    async def delete_video_comments(
        self,
        before_time: Optional[datetime] = None,
        source: Optional[str] = None,
    ) -> int:
        """删除视频评论"""
        conditions = []
        params = []
        
        if before_time:
            conditions.append("collected_at < ?")
            params.append(before_time.isoformat())
        if source:
            conditions.append("source = ?")
            params.append(source)
        
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        
        cur = await self._db.execute(f"DELETE FROM video_comments {where}", params)
        await self._db.commit()
        return cur.rowcount
    
    # ============================================================
    # 抓取任务 CRUD
    # ============================================================
    
    async def insert_scrape_task(self, task: ScrapeTask) -> str:
        """创建抓取任务"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute("""
            INSERT INTO scrape_tasks (id, source, mode, params, status, progress, result_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.id, task.source, task.mode, task.params,
            task.status or "pending", task.progress or "{}",
            task.result_count, task.created_at or now, task.updated_at or now,
        ))
        await self._db.commit()
        return task.id
    
    async def update_scrape_task(
        self,
        task_id: str,
        status: Optional[str] = None,
        progress: Optional[str] = None,
        result_count: Optional[int] = None,
        error: Optional[str] = None,
    ):
        """更新抓取任务状态"""
        updates = []
        params = []

        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if progress is not None:
            updates.append("progress = ?")
            params.append(progress)
        if result_count is not None:
            updates.append("result_count = ?")
            params.append(result_count)
        if error is not None:
            updates.append("error = ?")
            params.append(error[:500])  # 限制长度

        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        params.append(task_id)

        await self._db.execute(
            f"UPDATE scrape_tasks SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await self._db.commit()
    
    async def get_scrape_task(self, task_id: str) -> Optional[dict]:
        """获取单个抓取任务"""
        cursor = await self._db.execute("SELECT * FROM scrape_tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    
    async def query_scrape_tasks(
        self,
        source: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """查询抓取任务列表"""
        conditions = []
        params = []
        
        if source:
            conditions.append("source = ?")
            params.append(source)
        if status:
            conditions.append("status = ?")
            params.append(status)
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        count_row = await self._db.execute(
            f"SELECT COUNT(*) FROM scrape_tasks WHERE {where_clause}", params
        )
        total = (await count_row.fetchone())[0]
        
        cursor = await self._db.execute(
            f"SELECT * FROM scrape_tasks WHERE {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        
        return result, total
    
    async def delete_scrape_task(self, task_id: str) -> bool:
        """删除抓取任务"""
        cur = await self._db.execute("DELETE FROM scrape_tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return cur.rowcount > 0


# 全局实例
_db_instance: Optional[DBStorage] = None


def get_db() -> DBStorage:
    """获取全局数据库实例"""
    global _db_instance
    if _db_instance is None:
        _db_instance = DBStorage()
    return _db_instance


async def init_database():
    """初始化全局数据库"""
    db = get_db()
    await db.init_db()


async def close_database():
    """关闭全局数据库"""
    global _db_instance
    if _db_instance:
        await _db_instance.close()
        _db_instance = None
