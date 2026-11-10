#!/usr/bin/env python3
"""按新规则重打分线索（只改规则变更真正影响到的那些）

背景
----
v2.0.14 给规则引擎加了「售后语境排除」——
问「修车/剐蹭/事故要多少钱」不再算购车「价格询问」。
v2.0.15 又修了「裸 max/pro/ultra 被当成版本询问」——
「展车有pro吗」「su7max现在什么价位了」是点名车型，不是问选哪个版本。

之前用旧规则打过分、已经落库的线索，判定还是错的，
话术会回「看到你问SU7多少钱，可以给你报个落地价」，答非所问。

**只更新开关前后结果不同的线索**
----
库里有些线索的新旧分数不一致，是因为「实时链路」和「回溯扫描」的判定口径
本来就不一样（单条弹幕打分 vs 多条取最高），不是因为规则改了。
所以这里用两个打分器对比：
- ``RuleScorer(exclude_aftersale=False, trim_as_version=True)``  —— 旧口径
- ``RuleScorer(exclude_aftersale=True, trim_as_version=False)``  —— 新口径
只有两者结果不同的线索才是规则变更真正影响到的，才动它。

用法::

    .venv/bin/python scripts/rescore_leads.py --dry-run   # 只看会改什么
    .venv/bin/python scripts/rescore_leads.py             # 真的改
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.intelligence.rule_scorer import RuleScorer  # noqa: E402

DEFAULT_DB = ROOT / "data" / "db" / "douyin_ppp.db"


def _evidence_items(raw: str) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                return [str(x) for x in items if x]
        except (ValueError, TypeError):
            pass
        return [raw]
    return [raw]


def _best(scorer: RuleScorer, texts: list[str]):
    best = None
    for t in texts:
        r = scorer.score(t)
        if best is None or r.score > best.score:
            best = r
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description="按新规则重打分线索")
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true", help="只看会改什么，不写库")
    ap.add_argument(
        "--recompute",
        action="store_true",
        help="按**当前证据 + 当前规则**重算并回写（证据回填后用这个）",
    )
    args = ap.parse_args()

    old_scorer = RuleScorer(exclude_aftersale=False, trim_as_version=True)
    new_scorer = RuleScorer(exclude_aftersale=True, trim_as_version=False)

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, display_id, nickname, intent_level, intent_score, intent_reason, "
        "evidence FROM leads"
    ).fetchall()

    changed = []
    for r in rows:
        texts = _evidence_items(r["evidence"] or "")
        if not texts:
            continue
        new = _best(new_scorer, texts)
        if new is None:
            continue
        if args.recompute:
            # 重算模式：拿「当前规则在当前证据上的结果」和「库里存的值」比。
            # 证据回填之后，存量分数可能来自另一条弹幕（或更早的规则版本），
            # 这里统一成同一套口径。
            if new.level == r["intent_level"] and new.score == r["intent_score"]:
                continue
        else:
            old = _best(old_scorer, texts)
            if old is None:
                continue
            if old.level == new.level and old.score == new.score:
                continue
        changed.append({
            "id": r["id"],
            "display_id": r["display_id"],
            "nickname": r["nickname"],
            "old_level": r["intent_level"],
            "old_score": r["intent_score"],
            "new_level": new.level,
            "new_score": new.score,
            "reason": new.reason,
        })

    title = "按当前证据重算后变化的线索" if args.recompute else "规则变更影响到的线索"
    print(f"{title}：{len(changed)} 条\n")
    for c in changed:
        print(f"  {c['display_id']:<20}{c['nickname'][:12]:<14}"
              f"{c['old_level']}/{c['old_score']} → {c['new_level']}/{c['new_score']}"
              f"　{c['reason']}")

    if not changed:
        print("没有受规则变更影响的线索。")
        return 0

    if args.dry_run:
        print("\n（dry-run，未写库）")
        return 0

    for c in changed:
        conn.execute(
            "UPDATE leads SET intent_level = ?, intent_score = ?, intent_reason = ? "
            "WHERE id = ?",
            (c["new_level"], c["new_score"], c["reason"], c["id"]),
        )
    conn.commit()
    conn.close()
    print(f"\n已更新 {len(changed)} 条。")
    print("下一步：到线索中心点「同步到多维表格」把新判定同步到飞书表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
