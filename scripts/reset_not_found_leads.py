"""重置「因为搜索 bug 被误标」的线索状态

背景（2026-11-11 ~ 11-12）
--------------------------
抖音搜索默认落在「综合」tab，用户列表要手动点「用户」tab 才加载
（2026-11-11 23:05 已修复）。在那之前跑的每一轮，
「搜不到人」都是**假的**——不是那些人失效了，是搜索没加载用户列表。

那些线索被标成了 failed/skipped，就不会再被触达了。
这个脚本把它们重置回「待触达」，让它们重新排队。

重置条件（三条都要满足）
------------------------
1. 本地 ``outreach_status`` 是 failed / skipped
2. ``outreach_note`` 里含「没搜到这个人」——就是被搜索 bug 误伤的那批
3. 该抖音号在 ``outreach_records`` 里**没有** ``status='sent'`` 的记录
   （有的话说明确实留言成功了，别重复发）

⚠️ 多机共用同一张飞书表时，另一台机器可能也在处理这些线索。
本地重置后，下一次状态同步会把「待触达」推回飞书表。

用法::

    .venv/bin/python scripts/reset_not_found_leads.py          # 只看
    .venv/bin/python scripts/reset_not_found_leads.py --apply  # 真改
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（不加只预览）")
    args = ap.parse_args()

    cfg = get_config()
    db_path = pathlib.Path(cfg.storage.db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT l.id, l.lead_key, l.display_id, l.nickname, l.store,
                      l.outreach_status, l.outreach_at, l.outreach_note,
                      (SELECT COUNT(*) FROM outreach_records r
                        WHERE r.display_id = l.display_id AND r.status = 'sent'
                      ) AS sent_cnt
                 FROM leads l
                WHERE l.outreach_status IN ('failed', 'skipped')
                  AND l.outreach_note LIKE '%没搜到这个人%'
                ORDER BY l.outreach_at DESC"""
        ).fetchall()

        targets = [r for r in rows if not r["sent_cnt"]]
        already = [r for r in rows if r["sent_cnt"]]

        print(f"误标「没搜到这个人」的线索共 {len(rows)} 条")
        print(f"  → 里面已经成功留言过的 {len(already)} 条（不动，避免重复发）")
        print(f"  → 可以重置回「待触达」的 {len(targets)} 条\n")

        for r in targets:
            print(f"  id={r['id']:<5} {r['display_id']:<14} {(r['nickname'] or '')[:12]:<14}"
                  f" {r['outreach_status']:<8} {r['outreach_at']}")

        if not targets:
            print("\n没有要重置的")
            return 0

        if args.apply:
            conn.execute(
                """UPDATE leads SET outreach_status = 'pending',
                          outreach_note = '搜索故障误标，已重置待触达',
                          outreach_at = ''
                    WHERE id IN (%s)"""
                % ",".join(str(r["id"]) for r in targets)
            )
            conn.commit()
            print(f"\n✅ 已重置 {len(targets)} 条回「待触达」")
            print("   （下次状态同步会把「待触达」推回飞书表）")
        else:
            print("\n（预览模式，没写库。要真改加 --apply）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
