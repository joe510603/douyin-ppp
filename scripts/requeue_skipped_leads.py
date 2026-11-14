#!/usr/bin/env python3
"""把「搜不到而跳过」的线索放回待触达队列

背景
----
用户 2026-11-13 指示：「多次搜不到可以跳过，我后面来人工验证」。
所以搜不到 → 跳过（终态）是**用户拍板**的行为，不是 bug。

但有一批人是**在搜索逻辑修好之前**被跳过的：
今天 16:00-17:00 那段，抖音昵称是**截断**的（"候鸟…"），
拿它去搜永远搜不到 → 全部被判成「这个人失效了」→ 跳过。
修复之后（昵称去省略号 + 认人必须抖音号对得上），
这些人里面有一部分是**能搜到的**，就这么被埋掉了。

证据（2026-11-13 实测）
----------------------
今天被「搜不到」跳过的 36 条线索，**36 条**在 live_comments 表里
都能查到对应的抖音号 —— 说明这些人确实有号、在直播间留过言，
不是废号。

而且抖音的搜索结果是**概率性**的：
同一个号 18:36 搜不到、18:50 搜得到（当天实测）。
所以「上一轮搜不到」不等于「这个人没了」，跨轮再试是有意义的。

这个脚本做什么
--------------
把这批线索的 ``outreach_status`` 改回 ``pending``，
并把 ``not_found_count`` 清零 —— 他们就又能进下一轮的队列。

安全阀
------
1. **默认干跑**，加 ``--apply`` 才真正改。
2. 默认**只放回「搜不到」跳过的**（``outreach_note`` 里含「搜不到」），
   「没有公开作品」那批**不放回**（他们是真的没作品可留言）。
3. ``--limit`` 限制一次放回多少条（默认 50），
   免得一次性放太多、把一轮的名额全占了。
4. 放回之后如果又搜不到，**照样会被跳过**（跳过逻辑没变），
   所以不会变成无限重试。

用法
----
    .venv/bin/python scripts/requeue_skipped_leads.py              # 干跑，看会改什么
    .venv/bin/python scripts/requeue_skipped_leads.py --apply      # 真放回
    .venv/bin/python scripts/requeue_skipped_leads.py --apply --limit 20
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = "data/db/douyin_ppp.db"


def _connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


def pick_leads(conn: sqlite3.Connection, limit: int, reason: str) -> list[sqlite3.Row]:
    """挑出被跳过的线索

    ``reason``:
    - ``搜不到``：搜索没搜到人（可能是搜索逻辑修好之前被误跳过的）
    - ``作品``：搜到了但**没有可留言的作品** → 这些是私信兜底的候选

    排序：最近跳过的优先（last_hit_at 倒序）——
    最近在直播间冒过头的客户，成功率最高。
    """
    if reason == "作品":
        note_like = "%作品%"
    else:
        note_like = "%搜不到%"
    sql = """
        SELECT id, display_id, nickname, store, outreach_note,
               outreach_at, not_found_count, last_hit_at, created_at
          FROM leads
         WHERE outreach_status = 'skipped'
           AND outreach_note LIKE ?
           AND display_id != ''
         ORDER BY COALESCE(NULLIF(last_hit_at, ''), created_at) DESC
         LIMIT ?
    """
    return list(conn.execute(sql, [note_like, limit]).fetchall())


def has_douyin_id(conn: sqlite3.Connection, display_id: str) -> int:
    """弹幕原始表里有没有这个抖音号（= 这个号确实存在过）"""
    row = conn.execute(
        "SELECT COUNT(*) FROM live_comments WHERE user_display_id = ?",
        [display_id],
    ).fetchone()
    return int(row[0]) if row else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH, help="数据库路径")
    ap.add_argument("--apply", action="store_true", help="真正改（默认只干跑）")
    ap.add_argument("--limit", type=int, default=50, help="一次最多放回多少条")
    ap.add_argument("--reason", default="搜不到",
                    choices=["搜不到", "作品"],
                    help="放回哪一批：搜不到=搜索误跳过；作品=没作品（私信兜底的候选）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"❌ 找不到数据库：{args.db}")
        return 1

    conn = _connect(args.db)
    try:
        rows = pick_leads(conn, args.limit, args.reason)
        if not rows:
            print(f"✅ 没有「{args.reason}」跳过的线索要放回")
            return 0

        print(f"找到 {len(rows)} 条「{args.reason}」跳过的线索：\n")
        changed = 0
        for r in rows:
            did = str(r["display_id"] or "")
            known = has_douyin_id(conn, did)
            mark = "✓ 抖音号有记录" if known else "⚠️ 弹幕表里查不到这个号"
            print(f"  #{r['id']:<5} {did:<20} {(r['nickname'] or '')[:16]:<18} "
                  f"跳过于 {r['outreach_at'] or '?'}　{mark}")
            if args.apply:
                if args.reason == "作品":
                    # 标记「确认没作品」：下次进队列**直接转私信**，
                    # 不用再拉一遍作品列表确认（他们早就有 sec_uid，
                    # 也不会触发搜索）。
                    conn.execute(
                        "UPDATE leads SET outreach_status='pending', "
                        "not_found_count=0, outreach_note='', "
                        "no_works=1, outreach_at='', "
                        "updated_at=datetime('now','localtime') "
                        "WHERE id = ?",
                        [r["id"]],
                    )
                else:
                    conn.execute(
                        "UPDATE leads SET outreach_status='pending', "
                        "not_found_count=0, outreach_note='', "
                        "outreach_at='', updated_at=datetime('now','localtime') "
                        "WHERE id = ?",
                        [r["id"]],
                    )
                changed += 1
        if args.apply:
            conn.commit()
            tip = "下一轮会再搜一次他们"
            if args.reason == "作品":
                tip = ("下一轮会**直接开主页转私信**（有 sec_uid 的不搜索），"
                       "不用再拉作品列表确认")
            print(f"\n✅ 已放回 {changed} 条 → {tip}")
        else:
            print(f"\n（干跑，没有改任何东西。要真放回就加 --apply）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
