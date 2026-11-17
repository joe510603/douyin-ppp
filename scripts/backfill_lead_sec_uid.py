#!/usr/bin/env python3
"""回填 leads.sec_uid —— 从 live_comments 拿弹幕里本来就有的 secUid。

为什么需要（2026-11-17）
----------------------
采集器拿到弹幕的第一条就知道这个人的 secUid（排除自家账号就是靠它判的），
但建线索时这个值被丢掉了。于是每条新线索在触达时都要**重新搜人**：

  · 抖音 web 端搜索接口一次要 ~20 秒（实测）
  · 而且它最脆弱：静默限流（返回空 user_list、不报错），
    搜不到就整条线索出局

存上 sec_uid → 触达时直接开主页、**完全跳过搜索**（worker.py 的 resolve_by_sec_uid）。
每条省 17-20 秒，还少一次被限流的机会。

用法：
    python scripts/backfill_lead_sec_uid.py --dry-run
    python scripts/backfill_lead_sec_uid.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "db", "douyin_ppp.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看会改什么，不写库")
    ap.add_argument("--limit", type=int, default=0, help="最多回填多少条（0=不限）")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"❌ 找不到数据库：{DB}")
        return 1

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    has_sec = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE sec_uid IS NOT NULL AND sec_uid != ''"
    ).fetchone()[0]
    need = total - has_sec
    print(f"线索总数 {total}，已有 sec_uid {has_sec}，待回填 {need}")

    # 弹幕表里同一个人的 secUid —— 取该人**最新一条**弹幕带的那一个。
    #
    # ⚠ 两个坑：
    # 1. display_id 为空时不能 join —— 空串会互相匹配，
    #    把 A 人的线索配上 B 人的 secUid。用 l.display_id != '' 挡掉。
    # 2. 不用 GROUP BY + 裸列（SQLite 会从组里随机挑一行），
    #    改用相关子查询，结果是确定的。
    sql = """
        SELECT l.id            AS lead_id,
               l.nickname      AS nickname,
               l.display_id    AS display_id,
               (SELECT c.user_sec_id FROM live_comments c
                 WHERE c.user_display_id = l.display_id
                   AND c.user_sec_id IS NOT NULL AND c.user_sec_id != ''
                 ORDER BY c.collected_at DESC LIMIT 1) AS sec_uid,
               (SELECT MAX(c.collected_at) FROM live_comments c
                 WHERE c.user_display_id = l.display_id
                   AND c.user_sec_id IS NOT NULL AND c.user_sec_id != '') AS src_at
        FROM leads l
        WHERE (l.sec_uid IS NULL OR l.sec_uid = '')
          AND l.display_id IS NOT NULL AND l.display_id != ''
          AND EXISTS (SELECT 1 FROM live_comments c
                       WHERE c.user_display_id = l.display_id
                         AND c.user_sec_id IS NOT NULL AND c.user_sec_id != '')
        ORDER BY src_at DESC
    """
    rows = conn.execute(sql).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    print(f"可回填 {len(rows)} 条：")
    for r in rows:
        print(f"  #{r['lead_id']:<5} {str(r['nickname'])[:20]:<22} "
              f"{str(r['display_id'])[:16]:<18} → {r['sec_uid'][:26]}…")

    if not rows:
        print("✅ 没有可回填的")
        return 0

    if args.dry_run:
        print("\n（dry-run，未写库）")
        return 0

    conn.close()
    wconn = sqlite3.connect(DB)
    n = 0
    for r in rows:
        cur = wconn.execute(
            "UPDATE leads SET sec_uid = ? "
            "WHERE id = ? AND (sec_uid IS NULL OR sec_uid = '')",
            [r["sec_uid"], r["lead_id"]],
        )
        n += cur.rowcount or 0
    wconn.commit()
    wconn.close()
    print(f"\n✅ 已回填 {n} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
