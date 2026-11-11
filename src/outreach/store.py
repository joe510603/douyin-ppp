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


# ================================================================
# 建表 SQL（唯一真相来源）
# ================================================================
# 放在 store.py 而不是 db_storage.py：
# 这个表是**触达线程**写的（OutreachStore 用同步 sqlite3），
# 表归它管才合理；而且 store 自己建表 → 不依赖主进程先跑 init_db，
# 避免「worker 先起来、表还没建」的竞态。
CREATE_TABLE_OUTREACH_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS outreach_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dry_run INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    sent INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    note TEXT DEFAULT '',
    kind TEXT DEFAULT '',
    started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT DEFAULT ''
);
"""

CREATE_OUTREACH_RUNS_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_outreach_runs_started ON outreach_runs(started_at);",
]


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
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """自己保证表结构能用（建表 + 补列）

        两件事：
        1. 建表 —— 触达线程可能比主进程的 init_db 先跑
        2. 补列 —— 老库里没有 ``kind`` 列，直接写会报 no such column

        老库补列是必须的：``kind`` 是 v2.0.23 才加的，
        线上库已经有一堆历史记录，不补列的话 finish_run 直接炸。
        """
        try:
            self._conn.executescript(CREATE_TABLE_OUTREACH_RUNS_SQL)
            for sql in CREATE_OUTREACH_RUNS_INDEXES_SQL:
                self._conn.execute(sql)
            cols = {r[1] for r in
                    self._conn.execute("PRAGMA table_info(outreach_runs)")}
            if "kind" not in cols:
                self._conn.execute(
                    "ALTER TABLE outreach_runs ADD COLUMN kind TEXT DEFAULT ''"
                )
            self._conn.commit()
        except sqlite3.OperationalError:
            # 表被别的进程锁着 / 已经有别的结构 → 交给上层报错，别在这里崩
            pass

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
        sort_by: str = "recent",
    ) -> list[dict]:
        """取待触达的线索

        条件：
        - outreach_status = pending
        - 有抖音号（display_id 不为空）——没号没法搜人
        - 意向等级 / 门店 / 意向分下限可过滤

        排序：
        - ``recent``（默认）：**最近抓到的优先**（用户要求）。
          先按 ``last_hit_at`` 倒序（这条线索最后一次被看到的时间），
          同时间再按意向分倒序。
          为什么不按 created_at：created_at 是**首次**抓到的时间，
          一个人隔几天又在直播间问了一次，他其实是最"热乎"的，
          应该排在前面，不是被压到一堆老线索后面。
        - ``intent``：老的排序（高意向优先），留着方便对照/回退。
        """
        sql = [
            "SELECT id, lead_key, display_id, nickname, car_model, store, ",
            "intent_level, intent_score, intent_reason, evidence, monitor_name, ",
            "bitable_record_id, outreach_status, created_at, last_hit_at ",
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

        if sort_by == "intent":
            sql.append(" ORDER BY intent_score DESC, id DESC")
        else:
            # last_hit_at 为空的老数据（回填失败的那些）排到最后，
            # 用 created_at 兜底比较——格式统一过，字符串比较是安全的。
            sql.append(
                " ORDER BY COALESCE(NULLIF(last_hit_at, ''), created_at) DESC, "
                "intent_score DESC, id DESC"
            )
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

    def mark_verified(
        self, record_id: str, ok: bool, note: str = "", status: str = ""
    ) -> None:
        """标记回查结果（评论是否真的出现在页面上）

        回查失败时**必须同时改 status**：
        记录刚写入时 status='sent'（表示「点了发送」），
        但回查发现评论根本不在页面上——评论其实没发出去。
        status 留着 'sent' 的话，去重逻辑（recent_touch_days 只认 'sent'）
        会把它当成「已经打扰过这个人」，30 天内再也不发，
        结果就是「明明没发出去，却再也不会发第二次」。
        """
        sets = "verified = ?, error = ?"
        values: list = [1 if ok else 0, (note or "")[:300]]
        if status:
            sets += ", status = ?"
            values.append(status)
        values.append(record_id)
        self._conn.execute(
            f"UPDATE outreach_records SET {sets} WHERE id = ?", values
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
        self, run_id: int, sent: int = 0, failed: int = 0, skipped: int = 0,
        note: str = "", kind: str = ""
    ) -> None:
        """收尾一轮触达

        kind = 这一轮是因为什么停手的：verify / limit / login / 空
        （空 = 正常跑完，或者自己撞到连续异常主动停手）

        单独存一列而不是塞进 note：note 是给人看的文案，
        kind 是给机器读的判断依据，混在一起早晚对不上。
        """
        self._conn.execute(
            "UPDATE outreach_runs SET sent = ?, failed = ?, skipped = ?, note = ?, "
            "kind = ?, finished_at = datetime('now','localtime') WHERE id = ?",
            [sent, failed, skipped, (note or "")[:300], (kind or "")[:20], run_id],
        )
        self._conn.commit()

    def recent_runs(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM outreach_runs ORDER BY id DESC LIMIT ?", [limit]
        ).fetchall()
        return [dict(r) for r in rows]
