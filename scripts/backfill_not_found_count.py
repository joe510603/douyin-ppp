#!/usr/bin/env python3
"""把历史「搜不到」次数回填进 leads.not_found_count

为什么需要
----------
「多次搜不到就跳过」（用户 2026-11-13 指示）是**按累计次数**判的。
但 not_found_count 是新加的列，老数据的次数记在 outreach_records 里
（每次搜不到都记了一条 status='not_found' 的触达记录）。

不回填的话：那几条已经被反复搜了 10 多轮的线索，计数从 0 开始，
还要再搜 3 次才放弃 —— 白白再占 3 轮名额。

幂等
----
重复跑结果一样；而且只往大里补（MAX），不会把已经累加的次数改小。

用法
----
    .venv/bin/python scripts/backfill_not_found_count.py
    .venv/bin/python scripts/backfill_not_found_count.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = "data/db/douyin_ppp.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH, help="数据库路径")
    ap.add_argument("--dry-run", action="store_true", help="只看会改什么，不落库")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"找不到数据库：{args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 补列（老库可能没有）
    cols = {r[1] for r in cur.execute("PRAGMA table_info(leads)")}
    if "not_found_count" not in cols:
        cur.execute("ALTER TABLE leads ADD COLUMN not_found_count INTEGER DEFAULT 0")
        conn.commit()
        print("已补 leads.not_found_count 列")

    rows = cur.execute(
        "SELECT lead_id, COUNT(*) n FROM outreach_records "
        "WHERE status = 'not_found' AND lead_id IS NOT NULL "
        "GROUP BY lead_id"
    ).fetchall()

    changed = 0
    for r in rows:
        lead_id = r["lead_id"]
        n = int(r["n"])
        cur.execute(
            "SELECT not_found_count FROM leads WHERE id = ?", [lead_id]
        ).fetchone()
        old_row = cur.execute(
            "SELECT not_found_count, outreach_status, display_id FROM leads WHERE id = ?",
            [lead_id],
        ).fetchone()
        if not old_row:
            continue
        old = int(old_row["not_found_count"] or 0)
        if n <= old:
            continue
        changed += 1
        print(
            f"  lead {lead_id}（{old_row['display_id']}）"
            f" 搜不到次数 {old} → {n}  [{old_row['outreach_status']}]"
        )
        if not args.dry_run:
            cur.execute(
                "UPDATE leads SET not_found_count = ? WHERE id = ?", [n, lead_id]
            )

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"\n共 {len(rows)} 条线索有历史搜不到记录，需要补 {changed} 条"
          + ("（dry-run，未落库）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
