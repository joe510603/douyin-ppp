#!/usr/bin/env python
"""清理「自家账号被当成线索」的脏数据

背景（2026-11-17）
------------------
门店官方号「小米汽车-成都金牛凯德体验店」自己在直播间发的口播文案
被规则打分器判成「价格询问、金融补贴」→ 当成高意向客户入池 →
程序给人家发了一条留言广告（08:09:16 实发）。

这个脚本把**所有 secUid 属于我们监控账号**的线索标成「已跳过」，
并把已发出的触达记录标注出来（记录留着，方便核对；不回滚已发的消息）。

用法：
    .venv/bin/python scripts/cleanup_own_account_leads.py --dry-run
    .venv/bin/python scripts/cleanup_own_account_leads.py
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config

DB = "data/db/douyin_ppp.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看不动")
    args = ap.parse_args()

    cfg = get_config()
    own = {
        (getattr(m, "sec_user_id", "") or "").strip()
        for m in getattr(cfg, "monitors", []) or []
        if (getattr(m, "sec_user_id", "") or "").strip()
    }
    if not own:
        print("没有监控账号，退出")
        return 1

    print(f"自家账号 {len(own)} 个")
    db = pathlib.Path(DB)
    if not db.exists():
        print(f"找不到库：{db}")
        return 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ph = ",".join("?" for _ in own)
    rows = conn.execute(
        f"SELECT id, display_id, nickname, sec_uid, outreach_status, intent_score, "
        f"monitor_name FROM leads WHERE sec_uid IN ({ph})",
        list(own),
    ).fetchall()

    if not rows:
        print("没有自家账号的线索，无需清理")
        return 0

    print(f"\n发现 {len(rows)} 条自家账号的线索：")
    for r in rows:
        print(f"  #{r['id']} {r['nickname']!r} 抖音号={r['display_id']} "
              f"状态={r['outreach_status']} 意向分={r['intent_score']}")

    # 已发出的触达记录（留着，只报告）
    touched = conn.execute(
        f"SELECT o.id, o.lead_id, o.nickname, o.status, o.channel, o.created_at "
        f"FROM outreach_records o JOIN leads l ON l.id = o.lead_id "
        f"WHERE l.sec_uid IN ({ph})",
        list(own),
    ).fetchall()
    if touched:
        print(f"\n⚠️ 其中已发出 {len(touched)} 条触达（撤不回，如实报告）：")
        for t in touched:
            print(f"  {t['created_at']} [{t['channel']}] {t['nickname']!r} "
                  f"状态={t['status']}")

    if args.dry_run:
        print("\n--dry-run：没有改动任何东西")
        return 0

    ids = [r["id"] for r in rows]
    idph = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE leads SET outreach_status='skipped', "
        f"outreach_note='自家账号，不是客户线索（脚本清理）' "
        f"WHERE id IN ({idph})",
        ids,
    )
    conn.commit()
    print(f"\n✓ 已把 {len(ids)} 条自家账号的线索标成「已跳过」")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
