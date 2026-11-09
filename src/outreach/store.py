"""触达执行的存储层（同步 sqlite3）

为什么是同步的：
触达任务跑在**专用线程**里（Playwright 的 sync API 不能跨线程使用，
而 douyin-ppp 主进程是 asyncio 架构）。线程里用同步 sqlite3 最简单，
不用为了一个后台任务把整个存储层 async 化。

和主进程的关系：
主进程用 aiosqlite 写同一个库文件，靠 SQLite 的 WAL 模式 + busy_timeout 排队。
连接各自独立，不共享游标。

职责划分：
- ``leads`` 表：线索主表（和主进程共用），这里只读 + 更新 outreach_status
- ``outreach_records`` 表：每一条发出去的留言（成功失败都记，用于去重和复盘）
- ``outreach_runs`` 表：每轮任务一行摘要
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
import uuid
from typing import Any, Optional

# 触达状态 → leads.outreach_status 的取值
STATUS_PENDING = "pending"
STATUS_SEARCHING = "searching"
STATUS_FOUND = "found"
STATUS_COMMENTED = "commented"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


class OutreachStore:
    """触达任务的本地存储"""

    def __init__(self, db_path: str | pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        # 写锁最多等 15 秒再报 "database is locked"——主进程可能正在写线索
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ================================================================
    # 名单：读 leads 表
    # ================================================================

    def pending_leads(
        self,
        intent_levels: Optional[list[str]] = None,
        stores: Optional[list[str]] = None,
        min_score: float = 0.0,
    ) -> list[dict]:
        """取待触达的线索

        条件：
        - outreach_status = pending
        - 有抖音号（display_id 不为空）——没号没法搜人
        - 意向等级 / 门店 / 意向分下限可过滤

        排序：高意向 → 中意向 → 低意向，同级按意向分倒序。
        """
        sql = [
            "SELECT id, lead_key, display_id, nickname, car_model, store, ",
            "intent_level, intent_score, intent_reason, evidence, monitor_name, ",
            "bitable_record_id, outreach_status ",
            "FROM leads WHERE outreach_status = ? AND display_id != ''"
        ]
        params: list[Any] = [STATUS_PENDING]

        if intent_levels:
            ph = ",".join("?" for _ in intent_levels)
            sql.append(f" AND intent_level IN ({ph})")
            params.extend(intent_levels)
        if stores:
            ph = ",".join("?" for _ in stores)
            sql.append(f" AND store IN ({ph})")
            params.extend(stores)
        if min_score:
            sql.append(" AND intent_score >= ?")
            params.append(float(min_score))

        sql.append(" ORDER BY intent_score DESC, id DESC")
        rows = self._conn.execute("".join(sql), params).fetchall()
        return [dict(r) for r in rows]

    def get_lead(self, lead_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM leads WHERE id = ?", [lead_id]
        ).fetchone()
        return dict(row) if row else None

    def set_outreach_status(
        self,
        lead_id: int,
        status: str,
        error: str = "",
        sec_uid: str = "",
    ) -> None:
        """更新线索的触达状态（同时清掉旧错误信息）"""
        self._conn.execute(
            "UPDATE leads SET outreach_status = ?, "
            "outreach_note = ?, outreach_at = datetime('now','localtime') "
            "WHERE id = ?",
            [status, (error or "")[:300], lead_id],
        )
        self._conn.commit()

    def set_sec_uid(self, lead_id: int, sec_uid: str, nickname: str = "") -> None:
        """记下定位到的抖音 sec_uid（下次同类任务能省一次搜索）"""
        self._conn.execute(
            "UPDATE leads SET outreach_note = ? WHERE id = ?",
            [f"sec_uid={sec_uid}" if sec_uid else "", lead_id],
        )
        self._conn.commit()

    # ================================================================
    # 留言记录
    # ================================================================

    def add_touch(
        self,
        lead_id: Optional[int] = None,
        record_id: str = "",
        display_id: str = "",
        nickname: str = "",
        sec_uid: str = "",
        aweme_id: str = "",
        aweme_desc: str = "",
        comment_text: str = "",
        template_id: str = "",
        channel: str = "comment",
        status: str = "sent",
        error: str = "",
    ) -> str:
        """记一条触达记录（成功的失败的都记）"""
        rid = uuid.uuid4().hex
        self._conn.execute(
            """INSERT INTO outreach_records (
                id, lead_id, display_id, nickname, message, status, error,
                record_id, sec_uid, aweme_id, aweme_desc, template_id, channel
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                rid, lead_id, display_id, nickname, comment_text, status, (error or "")[:300],
                record_id, sec_uid, aweme_id, aweme_desc, template_id, channel,
            ],
        )
        self._conn.commit()
        return rid

    def mark_verified(self, record_id: str, ok: bool, note: str = "") -> None:
        """标记回查结果（评论是否真的出现在页面上）"""
        self._conn.execute(
            "UPDATE outreach_records SET verified = ?, error = ? WHERE id = ?",
            [1 if ok else 0, (note or "")[:300], record_id],
        )
        self._conn.commit()

    def recent_touch_days(self, display_id: str, days: int) -> bool:
        """这个抖音号最近 N 天内**真的发出去过**没有（防止重复打扰）

        只认 status='sent'（真的发出去了）。
        干跑记录（dry_run）不算——干跑没打扰过客户，
        要是把干跑也算进去，干跑验证完的人就永远真发不出去了，
        「先干跑看看再真发」这个流程直接走不通。
        失败记录（failed）也不算，那是没发出去，可以重试。
        """
        if not display_id or days <= 0:
            return False
        since = time.time() - days * 86400
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM outreach_records "
            "WHERE display_id = ? AND status = 'sent' "
            "AND created_at >= ?",
            [display_id, since],
        ).fetchone()
        return bool(row and row["n"])

    def sent_today(self) -> int:
        """今天真实发出去了几条（干跑不算）"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM outreach_records "
            "WHERE status = 'sent' AND date(created_at) = date('now','localtime')"
        ).fetchone()
        return int(row["n"] if row else 0)

    def recent_touches(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM outreach_records ORDER BY created_at DESC LIMIT ?", [limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def comments_to_verify(self, limit: int = 30) -> list[dict]:
        """已发出但还没回查确认的留言"""
        rows = self._conn.execute(
            "SELECT * FROM outreach_records "
            "WHERE status = 'sent' AND verified = 0 AND aweme_id != '' "
            "ORDER BY created_at DESC LIMIT ?",
            [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    # ================================================================
    # 运行记录
    # ================================================================

    def start_run(self, dry_run: bool) -> int:
        cur = self._conn.execute(
            "INSERT INTO outreach_runs (dry_run) VALUES (?)", [1 if dry_run else 0]
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, sent: int = 0, failed: int = 0, skipped: int = 0, note: str = ""
    ) -> None:
        self._conn.execute(
            "UPDATE outreach_runs SET sent = ?, failed = ?, skipped = ?, note = ?, "
            "finished_at = datetime('now','localtime') WHERE id = ?",
            [sent, failed, skipped, (note or "")[:300], run_id],
        )
        self._conn.commit()

    def recent_runs(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM outreach_runs ORDER BY id DESC LIMIT ?", [limit]
        ).fetchall()
        return [dict(r) for r in rows]
