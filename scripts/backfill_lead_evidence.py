#!/usr/bin/env python3
"""回填 leads.evidence（弹幕原文）

背景
----
早期版本的「意向批量扫描」路径在构造 Lead 时没有把原始弹幕带进
``evidence`` 字段，导致落库是 ``"[]"``。飞书多维表格的「弹幕原文」
列因此全是空的——销售看到的只有分数和「命中信号：价格询问…」，
看不到客户到底说了什么。

这里从 ``live_comments`` 重新捞回原始弹幕，用**同一套规则引擎**
重新打分，挑出最能说明购车意向的几条回填，保证：
- 判定依据与弹幕原文一致（都是规则口径）
- 不依赖当初是哪条代码路径写的这条线索

用法::

    .venv/bin/python scripts/backfill_lead_evidence.py            # 全部空证据的线索
    .venv/bin/python scripts/backfill_lead_evidence.py --dry-run  # 只看会改什么
    .venv/bin/python scripts/backfill_lead_evidence.py --force    # 覆盖已有证据
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.intelligence.rule_scorer import RuleScorer  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "db" / "douyin_ppp.db"


def _load_leads(conn: sqlite3.Connection, force: bool) -> list[sqlite3.Row]:
    if force:
        sql = "SELECT id, lead_key, nickname, intent_level FROM leads ORDER BY intent_score DESC"
    else:
        sql = (
            "SELECT id, lead_key, nickname, intent_level FROM leads "
            "WHERE evidence IS NULL OR evidence = '' OR evidence = '[]' "
            "ORDER BY intent_score DESC"
        )
    conn.row_factory = sqlite3.Row
    return conn.execute(sql).fetchall()


def _load_messages(conn: sqlite3.Connection, keys: list[str]) -> dict[str, list[str]]:
    """一次性把候选用户的弹幕捞回来（避免逐条查 15 万行表）"""
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    sql = f"""
        SELECT COALESCE(NULLIF(user_display_id, ''), user_id) AS ukey,
               content
        FROM live_comments
        WHERE message_type = 'WebcastChatMessage'
          AND content IS NOT NULL AND content != ''
          AND (user_display_id IN ({placeholders}) OR user_id IN ({placeholders}))
    """
    rows = conn.execute(sql, keys + keys).fetchall()
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["ukey"], []).append(r["content"])
    return grouped


def main() -> int:
    ap = argparse.ArgumentParser(description="回填线索的弹幕原文")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    ap.add_argument("--top", type=int, default=3, help="每条线索保留几条弹幕")
    ap.add_argument("--force", action="store_true", help="覆盖已有证据")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=15)
    conn.execute("PRAGMA busy_timeout = 5000")

    leads = _load_leads(conn, args.force)
    if not leads:
        print("没有需要回填的线索")
        return 0

    keys = [r["lead_key"] for r in leads if r["lead_key"]]
    messages = _load_messages(conn, keys)
    print(f"待回填线索 {len(leads)} 条，命中弹幕用户 {len(messages)} 个")

    scorer = RuleScorer(min_level="low")
    updates: list[tuple[str, int, int]] = []
    missing = 0

    for lead in leads:
        key = lead["lead_key"]
        msgs = messages.get(key) or []
        if not msgs:
            missing += 1
            continue

        # 只保留非排除、且确实命中信号的弹幕，按分数取前 N 条
        scored = []
        for m in msgs:
            r = scorer.score(m, lead["nickname"] or "")
            if r.excluded or not r.signals:
                continue
            scored.append((r.score, m[:200]))
        if not scored:
            # 兜底：至少留一条原始弹幕，别让销售看到空原文
            scored = [(0, m[:200]) for m in msgs[: args.top]]

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[: args.top]
        evidence = json.dumps([t[1] for t in top], ensure_ascii=False)
        updates.append((evidence, len(top), lead["id"]))

    print(f"可回填 {len(updates)} 条，找不到弹幕 {missing} 条")

    if args.dry_run:
        for evidence, _cnt, lid in updates[:5]:
            print(f"  #{lid} -> {evidence}")
        print("(dry-run，未写库)")
        return 0

    conn.executemany(
        "UPDATE leads SET evidence = ?, evidence_count = ?, updated_at = datetime('now','localtime') WHERE id = ?",
        updates,
    )
    conn.commit()
    print(f"✅ 已回填 {len(updates)} 条线索的弹幕原文")

    # 校验
    row = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE evidence IS NULL OR evidence = '' OR evidence = '[]'"
    ).fetchone()
    print(f"剩余空证据线索：{row[0]}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
