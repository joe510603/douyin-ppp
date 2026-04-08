"""SQLite 存储引擎 — 评论数据的持久化存储与查询"""

from __future__ import annotations

import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import get_config
from ..models.comment import LiveComment, VideoComment, MessageType
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
        
        log.info(f"数据库初始化完成: {self.db_path}")
    
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
