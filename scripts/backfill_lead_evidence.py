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

为什么需要「重新挑」
------------------
实时链路（``upsert_lead``）在同一个人再次命中时，
会把 ``evidence`` **替换成最新那一条弹幕**，而 ``evidence_count`` 却在累加。
结果就是：库里存的是「最后一条命中的弹幕」，不是「客户问得最重的那个」。
实测 262 条线索里有 82 条（31%）这样——
比如客户先问「全款多少钱提回家」又发了句「N90」，
存下来只剩「N90」，话术就会去回「看到你关注小米汽车」，完全没回应重点。

所以这里按**分档 + 分数**重排：
- 有「购买类」信号的弹幕优先（话术角度要用的是这个）
- 纯「联系方式」信号排后面（它权重最高但对话术角度没用）
- 同一句话去重（用户重复发同一句很常见）

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

        # 只保留非排除、且确实命中信号的弹幕
        scored = []
        seen: set[str] = set()
        hit_count = 0
        for m in msgs:
            r = scorer.score(m, lead["nickname"] or "")
            if r.excluded or not r.signals:
                continue
            # 去重：同一个用户把同一句话发两遍很常见，
            # 不去重的话「qq?」能占满 3 个名额，把真正问价格的那条挤掉。
            norm = "".join(m.split())
            if norm in seen:
                continue
            seen.add(norm)
            hit_count += 1
            # 分档：有「购买类」信号的优先，纯「联系方式」的排后面。
            # 联系方式权重 45 是最高的（「加个微信」确实算强意向），
            # 但它对**话术角度**毫无用处——它不映射任何模板，
            # 让它霸占首位会把「落地多少钱」这种真问题挤掉。
            has_purchase = any(s != "联系方式" for s in r.signals)
            tier = 2 if has_purchase else 1
            scored.append((tier, r.score, m[:200]))
        if not scored:
            # 兜底：至少留一条原始弹幕，别让销售看到空原文
            scored = [(0, 0, m[:200]) for m in msgs[: args.top]]
            hit_count = 0

        # 先按档位再按分数：同档内分数高的排前面
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        top = scored[: args.top]
        evidence = json.dumps([t[2] for t in top], ensure_ascii=False)
        # evidence_count = 命中信号的弹幕条数（去重后），
        # 不是 len(top)——top 只是存下来给话术/销售看的那几条，
        # 拿它当「触发条数」会把信息丢掉。
        updates.append((evidence, hit_count, lead["id"]))

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
