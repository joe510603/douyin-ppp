"""回填 leads.last_hit_at —— 从 live_comments 关联出「最后一次抓到」的时间

为什么需要回填
--------------
``last_hit_at`` 是 v2.0.25 新加的列，用来排「最近抓到优先」。
新线索在写入时就会填好，但**历史线索**是空的——
只靠迁移时那句 ``UPDATE ... SET last_hit_at = created_at``，
拿到的是「首次抓到的时间」，不是「最后一次」。

差别很大：
- 某人 11-07 问了价，11-11 又问了一次 → created_at 是 11-07，
  但他其实是最热乎的那条线索，应该排最前面

怎么关联
--------
live_comments.user_display_id（抖音号）或 user_id 就是 leads.lead_key。
取该用户**最后一条**弹幕的时间即可。

live_comments.create_time 是 ISO 格式（带 T），
统一转成 "YYYY-MM-DD HH:MM:SS"（跟 created_at 一样）——
排序是字符串比较，格式不统一结果会完全错乱。

用法
----
    .venv/bin/python scripts/backfill_lead_last_hit.py            # 预览
    .venv/bin/python scripts/backfill_lead_last_hit.py --apply    # 真写
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import sqlite3  # noqa: E402

from src.config import get_config  # noqa: E402
from src.models.lead import fmt_dt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（不加只预览）")
    args = ap.parse_args()

    cfg = get_config()
    db_path = pathlib.Path(cfg.storage.db_path)
    if not db_path.exists():
        print(f"❌ 找不到库：{db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
        if "last_hit_at" not in cols:
            print("❌ leads 表还没有 last_hit_at 列，先重启一次服务（会自动迁移）")
            return 1

        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        print(f"leads 共 {total} 条")

        # 全量算：迁移时已经把 last_hit_at 填成 created_at 了，
        # 这次要用 live_comments 关联出的**真实最后弹幕时间**覆盖掉。
        # 关联不到的老线索保留原值（created_at 兜底，不算错）。
        rows = conn.execute(
            """SELECT l.id, l.lead_key, l.created_at, l.last_hit_at,
                      (SELECT MAX(c.create_time) FROM live_comments c
                        WHERE COALESCE(NULLIF(c.user_display_id, ''), c.user_id) = l.lead_key
                      ) AS last_at
                 FROM leads l"""
        ).fetchall()

        filled = 0
        changed = 0
        unmatched = 0
        for r in rows:
            raw = fmt_dt(r["last_at"])
            if not raw:
                # 关联不到弹幕（老数据可能已被清理）→ 保留 created_at
                unmatched += 1
                continue
            filled += 1
            if raw != (r["last_hit_at"] or ""):
                changed += 1
            if args.apply:
                conn.execute(
                    "UPDATE leads SET last_hit_at = ? WHERE id = ?", [raw, r["id"]]
                )

        if args.apply:
            conn.commit()
        print(f"{'已写入' if args.apply else '将写入'} {filled} 条"
              f"（关联到弹幕的）")
        print(f"其中 {changed} 条跟原值不同（这些才是真正「最近抓到」的）")
        print(f"关联不到弹幕、保留原值：{unmatched} 条")

        # 抽样看看排出来的前几条
        print("\n--- 按 last_hit_at 倒序，前 8 条 ---")
        for r in conn.execute(
            """SELECT display_id, nickname, intent_score, created_at, last_hit_at
                 FROM leads WHERE outreach_status='pending' AND display_id != ''
                 ORDER BY COALESCE(NULLIF(last_hit_at,''), created_at) DESC,
                          intent_score DESC, id DESC LIMIT 8"""
        ):
            mark = "  ← 比首次抓到新" if r["last_hit_at"] != r["created_at"] else ""
            print(f"  {r['display_id']:<14} 分={r['intent_score']:<4} "
                  f"创建={r['created_at']} 最后={r['last_hit_at']}{mark}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
