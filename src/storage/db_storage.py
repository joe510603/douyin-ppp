"""SQLite 存储引擎 — 评论数据的持久化存储与查询"""

from __future__ import annotations

import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import get_config
from ..models.comment import LiveComment, VideoComment, ScrapeTask, MessageType
from src.utils.logger import get_logger


log = get_logger("db_storage")

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


class DBStorage:
    """异步 SQLite 数据库存储引擎"""
    
    def __init__(self, db_path: Optional[str] = None):
        config = get_config()
        self.db_path = db_path or config.storage.db_path
        self.db_path = Path(self.db_path).resolve()
        self._db: Optional[aiosqlite.Connection] = None
        self._pending_buffer: list[LiveComment] = []
    
    async def init_db(self):
        """初始化数据库（建表+索引）"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        
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
        
        await self._db.commit()
        
        log.info(f"数据库初始化完成: {self.db_path}")
    
    async def _create_analysis_table(self):
        """创建分析结果表"""
        await self._db.executescript(CREATE_TABLE_ANALYSIS_SQL)
        for sql in CREATE_ANALYSIS_INDEXES_SQL:
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
                gift_name, gift_count, like_count, raw_data, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            )
            for c in comments
        ]
        
        await self._db.executemany("""
            INSERT INTO live_comments (
                message_type, content, user_id, user_nickname, user_avatar,
                room_id, anchor_id, anchor_name, monitor_name, create_time,
                gift_name, gift_count, like_count, raw_data, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        await self._db.commit()
        return len(comments)
    
    def buffer_comment(self, comment: LiveComment):
        """将评论加入内存缓冲区（高频写入优化）"""
        self._pending_buffer.append(comment)
    
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
