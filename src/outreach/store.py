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
from datetime import datetime, timedelta
from typing import Any, Optional

from ..intelligence.contact_extractor import merge_contact

# 触达状态 → leads.outreach_status 的取值
STATUS_PENDING = "pending"
STATUS_SEARCHING = "searching"
STATUS_FOUND = "found"
STATUS_COMMENTED = "commented"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
# 搜不到这个人 —— **可重试**，不是终态
#
# 为什么单独一个状态（2026-11-13）：
#   抖音搜索一次只给 6~10 个人，目标常常在第 2、3 页；
#   而且搜索结果是概率性的（同一个号 18:36 搜不到、18:50 搜得到）。
#   所以「这一次没搜到」≠「这个人没了」。
#   以前直接标 skipped（终态）→ 这条线索就永远不会再被触达，
#   而用户抽查发现这些人**全都搜得到**（实测 4 个抽查 3 个能搜到）。
#
#   现在它留在队列里、排在一堆正常线索**后面**：
#   正常线索先做，等队列见底了再回头重试这些。
STATUS_NOT_FOUND = "not_found"
# 已留资 —— 销售在飞书表里手动标的终态
#
# 拿到电话/微信的人就别再留言了（用户 2026-11-13 明确要求）。
# 这是一个**终态**：不在待触达队列里，回拉认它、也不再把它改回 pending。
STATUS_CONVERTED = "converted"


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

# 触达执行明细（每条留言一行，成功失败都记，用于去重和复盘）
#
# 跟 outreach_runs 一样放在 store.py：这个表是**触达线程**写的，
# 表归它管才合理。放在 db_storage.py 的话，
# 触达线程必须等主进程 init_db 跑完才能用（"no such table"）。
CREATE_TABLE_OUTREACH_RECORDS_SQL = """

CREATE TABLE IF NOT EXISTS outreach_records (
    id TEXT PRIMARY KEY,
    lead_id INTEGER DEFAULT NULL,
    display_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    target_url TEXT DEFAULT '',
    message TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    -- ---- 触达执行明细（v2.0.11 接入自动触达时补）----
    record_id TEXT DEFAULT '',     -- 飞书多维表格的 record_id（回写触达状态用）
    sec_uid TEXT DEFAULT '',       -- 抖音定位到的用户 ID
    aweme_id TEXT DEFAULT '',      -- 留言的作品 ID
    aweme_desc TEXT DEFAULT '',    -- 作品文案（复盘用）
    template_id TEXT DEFAULT '',   -- 用的哪条话术模板
    channel TEXT DEFAULT 'comment',-- comment / dm
    verified INTEGER DEFAULT 0     -- 1=回查确认评论真的在页面上
);
"""

CREATE_OUTREACH_RECORDS_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_records(status);",
    "CREATE INDEX IF NOT EXISTS idx_outreach_lead ON outreach_records(lead_id);",
    "CREATE INDEX IF NOT EXISTS idx_outreach_created ON outreach_records(created_at);",
    "CREATE INDEX IF NOT EXISTS idx_outreach_display ON outreach_records(display_id);",
]

# 自适应间隔的状态（只有一行，id=1）
#
# 为什么单独一张表而不是塞进 outreach_runs：
#   outreach_runs.kind 是「这一轮因为什么停手」，有它自己的语义
#   （限流冷却就靠它判断），往里塞「慢档」会跟那个判断串味。
#   速度状态是另一回事：跨轮次持续存在，到期自己消失。
CREATE_TABLE_OUTREACH_SPEED_SQL = """
CREATE TABLE IF NOT EXISTS outreach_speed (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    slow_until TEXT DEFAULT '',      -- 慢档生效到什么时候（空 = 快档）
    reason TEXT DEFAULT '',          -- 为什么慢（给人看）
    updated_at TEXT DEFAULT ''
);
"""


class OutreachStore:
    """触达任务的本地存储"""

    def __init__(self, db_path: str | pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # ⚠ check_same_thread=False：这个 store 会被**飞书推送的后台线程**碰
        #   （写成功后的 on_ok 回调要把「上次同步进飞书的状态」标记落库，
        #   见 worker._push_feishu_status）。SQLite 默认的 C 库是
        #   SERIALIZED 模式，同一连接上的访问由 C 层自己串行化，
        #   所以跨线程用是安全的（再配 busy_timeout 兜住写锁竞争）。
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=15.0, check_same_thread=False
        )
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
            # 明细表也自己建——触达线程不该依赖主进程先 init_db
            self._conn.executescript(CREATE_TABLE_OUTREACH_RECORDS_SQL)
            for sql in CREATE_OUTREACH_RECORDS_INDEXES_SQL:
                self._conn.execute(sql)
            # 自适应间隔的状态表（单行）
            self._conn.executescript(CREATE_TABLE_OUTREACH_SPEED_SQL)
            cols = {r[1] for r in
                    self._conn.execute("PRAGMA table_info(outreach_runs)")}
            if "kind" not in cols:
                self._conn.execute(
                    "ALTER TABLE outreach_runs ADD COLUMN kind TEXT DEFAULT ''"
                )
            # 明细表的补列（v2.0.11 之后陆续加的）
            rec_cols = {r[1] for r in
                        self._conn.execute("PRAGMA table_info(outreach_records)")}
            for col, ddl2 in (
                ("record_id", "TEXT DEFAULT ''"),
                ("sec_uid", "TEXT DEFAULT ''"),
                ("aweme_id", "TEXT DEFAULT ''"),
                ("aweme_desc", "TEXT DEFAULT ''"),
                ("template_id", "TEXT DEFAULT ''"),
                ("channel", "TEXT DEFAULT 'comment'"),
                ("verified", "INTEGER DEFAULT 0"),
            ):
                if col not in rec_cols:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE outreach_records ADD COLUMN {col} {ddl2}"
                        )
                    except sqlite3.OperationalError:
                        pass
            self._conn.commit()
        except sqlite3.OperationalError:
            # 表被别的进程锁着 / 已经有别的结构 → 交给上层报错，别在这里崩
            pass

        # leads 表归 db_storage 管，但触达线程不能干等主进程迁移完才敢用：
        # ``leads.sec_uid`` 是「已知抖音 sec_uid，跳过搜索」的依据，
        # ``leads.contact_type`` / ``contact_value`` 是「联系方式」的落点，
        # 列没有的话 SELECT 直接报 no such column，整轮触达直接瘫。
        # 所以这里自己补一次（ALTER ADD COLUMN 是幂等的）。
        try:
            lead_cols = {r[1] for r in
                         self._conn.execute("PRAGMA table_info(leads)")}
            if lead_cols:
                for col, ddl2 in (
                    ("sec_uid", "TEXT DEFAULT ''"),
                    ("contact_type", "TEXT DEFAULT ''"),
                    ("contact_value", "TEXT DEFAULT ''"),
                    # 「搜不到」累计次数——用来判断是不是该放弃了（用户 2026-11-13 指示）
                    ("not_found_count", "INTEGER DEFAULT 0"),
                    # 处理状态（销售在飞书里标的「已跟进/已留资」）
                    # pending_leads 要读它来生成「上次同步进飞书的状态」标记
                    ("status", "TEXT DEFAULT ''"),
                ):
                    if col not in lead_cols:
                        self._conn.execute(
                            f"ALTER TABLE leads ADD COLUMN {col} {ddl2}"
                        )
                self._conn.commit()
        except sqlite3.OperationalError:
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
        - outreach_status 是 pending **或** not_found（搜不到 → 可重试，排后面）
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

        ⚠️ not_found 排在一堆 pending **后面**：
          「这次没搜到」的线索要等正常线索做完了才回头重试，
          否则它们会把每一轮的名额都占满。
        """
        sql = [
            "SELECT id, lead_key, display_id, nickname, car_model, store, ",
            "intent_level, intent_score, intent_reason, evidence, monitor_name, ",
            "bitable_record_id, outreach_status, created_at, last_hit_at, ",
            "contact_type, contact_value, sec_uid, status, not_found_count ",
            "FROM leads WHERE outreach_status IN (?, ?) AND display_id != ''"
        ]
        params: list[Any] = [STATUS_PENDING, STATUS_NOT_FOUND]

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
            #
            # ⚠ 前面的 CASE：pending 排前面，not_found（这次没搜到、
            #   等下轮重试的）排后面 —— 别让它们把每轮名额占满。
            sql.append(
                " ORDER BY CASE outreach_status WHEN 'pending' THEN 0 ELSE 1 END, "
                "COALESCE(NULLIF(last_hit_at, ''), created_at) DESC, "
                "intent_score DESC, id DESC"
            )
        try:
            rows = self._conn.execute("".join(sql), params).fetchall()
        except sqlite3.OperationalError as e:
            # 补不上的列（老库 + ALTER 失败）别让整轮瘫掉：
            # 退回不带这些列的查询，少用一次「跳过搜索」而已。
            if not any(c in str(e) for c in ("sec_uid", "status", "not_found_count")):
                raise
            fallback = sql
            for col in (", sec_uid ", ", status ", ", not_found_count "):
                fallback = [s.replace(col, " ") for s in fallback]
            rows = self._conn.execute("".join(fallback), params).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d.setdefault("sec_uid", "")
                d.setdefault("status", "")
                d.setdefault("not_found_count", 0)
                out.append(d)
            return out
        out = []
        for r in rows:
            d = dict(r)
            d.setdefault("sec_uid", "")
            d.setdefault("status", "")
            d.setdefault("not_found_count", 0)
            out.append(d)
        return out

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

    def set_synced_status(self, lead_id: int, synced: str) -> None:
        """更新「上次同步进飞书的状态」标记

        这个标记是回拉判断「本地这次有没有自己改过」的**唯一依据**：
          · 标记 == 本地当前值 → 本地没改过 → 回拉采纳飞书的值（销售改的）
          · 标记 != 本地当前值 → 本地改过   → 回拉不采纳（本地说了算）

        ⚠ 只能在**飞书真的写成功之后**调它（见 worker._push_feishu_status）：
        早对齐 → 飞书端还写着旧值 → 回拉会把这个旧值当成「销售改的」采纳 →
        把刚跳过的线索又放回队列（实测 2026-11-13 发生过）。
        """
        self._conn.execute(
            "UPDATE leads SET bitable_synced_status = ? WHERE id = ?",
            [synced, lead_id],
        )
        self._conn.commit()

    def bump_not_found(self, lead_id: int) -> int:
        """记一次「这次没搜到这个人」，返回累计次数

        为什么要有次数：一次搜不到**不能**说明这个人没了（抖音搜索是概率性的、
        一次只给 6~10 个人）。但搜了 N 次还搜不到，就多半是真的搜不到了
        （改名 / 注销 / 被隐藏）——这时候继续吊在队列末尾纯属浪费名额。
        用户 2026-11-13 明确要求：多次搜不到就跳过，后面人工验证。
        """
        try:
            self._conn.execute(
                "UPDATE leads SET not_found_count = "
                "COALESCE(not_found_count, 0) + 1 WHERE id = ?",
                [lead_id],
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT not_found_count FROM leads WHERE id = ?", [lead_id]
            ).fetchone()
            return int((row[0] if row else 0) or 0)
        except sqlite3.OperationalError:
            # 老库补不上列时不能拖垮整轮触达，退化成「按 0 算」
            return 0

    def release_stuck_leads(self) -> int:
        """把卡在「搜索中 / 已定位」的线索退回队列

        为什么需要
        ----------
        这两个状态都不是终态，而待触达队列只挑 ``pending`` / ``not_found``，
        所以卡在这里的线索就**永远没人管了**（实测卡了 3 条出不来）。

        正常跑完的轮次不受影响 —— 结束时手上那条已经是
        已留言 / 已跳过 / 失败 这些终态，不会被误改。
        进程被杀掉 / 崩了、来不及归还的那条，靠这个在下一轮开头兜底。
        """
        cur = self._conn.execute(
            "UPDATE leads SET outreach_status = 'pending', "
            "outreach_note = '上一轮中断，重新排队' "
            "WHERE outreach_status IN ('searching', 'found')"
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def reset_not_found_count(self, lead_id: int) -> None:
        """把「搜不到」计数清零（人工把线索放回队列时用）"""
        try:
            self._conn.execute(
                "UPDATE leads SET not_found_count = 0 WHERE id = ?", [lead_id]
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def set_sec_uid(self, lead_id: int, sec_uid: str, nickname: str = "") -> None:
        """记下定位到的抖音 sec_uid（下次直接开主页，省一次搜索）

        写**独立列**（``leads.sec_uid``），不覆盖 ``outreach_note``——
        outreach_note 存的是「失败原因」，把它冲掉等于把故障记录弄没了。

        为什么要缓存（2026-11-12）：
        抖音 web 端搜索接口会静默限流（不报错，就是给你一个空的 user_list），
        而主页 ``/user/{sec_uid}`` 完全不受影响。
        记住 sec_uid = 少一次搜索 = 少一次被限流的机会，
        而且搜索被限流时这条线索照样能触达。
        """
        sec_uid = str(sec_uid or "").strip()
        if not sec_uid:
            return
        self._conn.execute(
            "UPDATE leads SET sec_uid = ? "
            "WHERE id = ? AND (sec_uid IS NULL OR sec_uid = '')",
            [sec_uid, lead_id],
        )
        self._conn.commit()

    def set_contact(
        self,
        lead_id: int,
        contact_type: str,
        contact_value: str,
        prefer_new_same_type: bool = False,
    ) -> tuple[bool, str, str]:
        """把联系方式写回线索（**按类型合并**，不覆盖已有的同类）

        返回 (是否真的改了, 合并后的 contact_type, 合并后的 contact_value)。
        「是否真的改了」是给调用方决定要不要同步飞书用的 ——
        没改还去写飞书就是白跑一次 lark-cli 子进程（实测 1.4 秒/次）。

        ``prefer_new_same_type`` 默认 False（简介链路用）：
        弹幕里说出来的联系方式是客户**当场**说的，比简介里那个更贴近这次问价，
        所以简介只负责**补**没有的类型，不去覆盖弹幕已经有的。
        """
        contact_value = str(contact_value or "").strip()
        if not contact_value:
            return False, "", ""
        row = self._conn.execute(
            "SELECT contact_type, contact_value FROM leads WHERE id = ?", [lead_id]
        ).fetchone()
        if not row:
            return False, "", ""
        old_type = row["contact_type"] or ""
        old_value = row["contact_value"] or ""
        new_type, new_value = merge_contact(
            old_type, old_value, contact_type, contact_value,
            prefer_new_same_type=prefer_new_same_type,
        )
        if new_type == old_type and new_value == old_value:
            return False, old_type, old_value
        self._conn.execute(
            "UPDATE leads SET contact_type = ?, contact_value = ? WHERE id = ?",
            [new_type, new_value, lead_id],
        )
        self._conn.commit()
        return True, new_type, new_value

    def cache_sec_uid_map(self, mapping: dict[str, str]) -> int:
        """批量缓存 sec_uid：一次搜索返回一页用户，顺手把它们都存下来

        抖音搜索一次会返回一页（约 10 个）用户，每个都带 sec_uid。
        只存目标那一个太浪费——顺手把这一页里**我们线索池里也有**的号都存上，
        后面那些线索就不用再搜索了（搜索次数直接少一截）。

        返回实际更新了多少条。
        """
        if not mapping:
            return 0
        n = 0
        for display_id, sec_uid in mapping.items():
            display_id = str(display_id or "").strip()
            sec_uid = str(sec_uid or "").strip()
            if not display_id or not sec_uid:
                continue
            cur = self._conn.execute(
                "UPDATE leads SET sec_uid = ? "
                "WHERE display_id = ? AND (sec_uid IS NULL OR sec_uid = '')",
                [sec_uid, display_id],
            )
            n += cur.rowcount or 0
        if n:
            self._conn.commit()
        return n

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

    # ============================================================
    # 自适应间隔的状态
    # ============================================================

    def speed_state(self) -> dict:
        """当前的间隔状态：慢档还是快档

        慢档到期自动变回快档 —— 判断放在读取的时候，
        这样就算服务一直没重启，状态也会自然过期。
        """
        try:
            row = self._conn.execute(
                "SELECT * FROM outreach_speed WHERE id = 1"
            ).fetchone()
        except Exception:
            return {"slow": False, "until": "", "reason": ""}
        if not row:
            return {"slow": False, "until": "", "reason": ""}
        until = str(row["slow_until"] or "")
        if not until:
            return {"slow": False, "until": "", "reason": str(row["reason"] or "")}
        try:
            t = datetime.strptime(until, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return {"slow": False, "until": "", "reason": ""}
        slow = t > datetime.now()
        return {"slow": slow, "until": until if slow else "", "reason": str(row["reason"] or "")}

    def set_slow_delay(self, minutes: int, reason: str = "") -> str:
        """切到慢档（记到什么时候为止），返回截止时间

        已经在慢档里再撞一次风控 → 顺延（重新从现在开始算）。
        """
        until = datetime.now() + timedelta(minutes=max(1, int(minutes or 1)))
        ts = until.strftime("%Y-%m-%d %H:%M:%S")
        self._conn.execute(
            "INSERT INTO outreach_speed (id, slow_until, reason, updated_at) "
            "VALUES (1, ?, ?, datetime('now','localtime')) "
            "ON CONFLICT(id) DO UPDATE SET slow_until=excluded.slow_until, "
            "reason=excluded.reason, updated_at=excluded.updated_at",
            [ts, (reason or "")[:120]],
        )
        self._conn.commit()
        return ts

    def clear_slow_delay(self) -> None:
        """撤掉慢档，回到快档"""
        try:
            self._conn.execute(
                "UPDATE outreach_speed SET slow_until='', reason='', "
                "updated_at=datetime('now','localtime') WHERE id=1"
            )
            self._conn.commit()
        except Exception:
            pass
