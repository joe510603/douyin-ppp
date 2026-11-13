"""把被「平台侧故障」误标成 failed 的线索救回来

背景（2026-11-13）
------------------
待触达队列只挑 ``outreach_status='pending'``。而触达过程中只要**平台侧**
出点状况（搜索被限流、页面超时、连接被掐断），旧代码就把这条线索标成
``failed`` —— 这一标就是**永久出局**，它再也不会被选中触达。

实测后果：``failed`` 141 条，``pending`` 只剩 50 条。
其中 107 条的失败原因全是「搜索接口被限流」——
而这些人手机上全都是能搜得到的（用户当场验证过）。

所以这个脚本把这类线索改回 pending，并把「待触达」同步回飞书表
（多机共用一张表，飞书那边也得是「待触达」，否则销售看到的还是「失败」）。

用法：
    .venv/bin/python scripts/repair_killed_leads.py --dry-run   # 只看会改哪些
    .venv/bin/python scripts/repair_killed_leads.py             # 真改
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config          # noqa: E402
from src.outreach.store import OutreachStore  # noqa: E402

DB_PATH = "data/db/douyin_ppp.db"

# 失败原因里出现这些词的，都是**平台侧故障**，不是「这个人没价值」
KILL_PATTERNS = (
    "%限流%",
    "%搜索接口%",
    "%搜索故障%",
    "%搜索返回空%",
    "%风控%",
    "%Timeout%",
    "%timeout%",
    "%net::ERR_%",
    "%code=-1%",
    "%验证本人操作%",
    # 上一轮修复留下的标记（被回拉又盖回 failed 的那些）
    "%平台侧故障误标失败%",
)

# 这些**不是**平台侧故障（登录失效要人扫码），别乱动
KEEP_FAILED = (
    "%没有登录态%",
    "%sessionid%",
)


def _pick(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """挑出该救回来的线索

    两类：
    1. 失败原因里写着「平台侧故障」的（限流/超时/断连）→ 明确误杀
    2. 失败原因**是空的**的 → 状态不明，也救回来让代码重新评一遍
       （这种躺着不动就等于永远排在队列外）
    """
    where = " OR ".join(f"outreach_note LIKE '{p}'" for p in KILL_PATTERNS)
    keep = " AND ".join(f"outreach_note NOT LIKE '{p}'" for p in KEEP_FAILED)
    sql = (
        "SELECT id, display_id, nickname, outreach_note, bitable_record_id "
        f"FROM leads WHERE display_id != '' AND outreach_status = 'failed' "
        f"AND ({where} OR outreach_note = '' OR outreach_note IS NULL) "
        f"AND {keep} ORDER BY id"
    )
    return conn.execute(sql).fetchall()


def _release_stuck(conn: sqlite3.Connection, minutes: int = 30) -> list[sqlite3.Row]:
    """把卡在「搜索中 / 已定位」的线索退回待触达

    为什么：轮次异常结束（撞验证超时、进程被杀）时，手上那条会停在
    「搜索中」或「已定位」——这俩都不是终态，而待触达队列只挑 pending，
    所以这条线索就永远没人管了（实测卡了 3 条出不来）。

    只处理**超过 minutes 没动过**的，避免把正在跑的那条误改掉。
    """
    sql = (
        "SELECT id, display_id, nickname, outreach_note, bitable_record_id "
        "FROM leads WHERE display_id != '' "
        "AND outreach_status IN ('searching', 'found') "
        "AND (outreach_at IS NULL OR outreach_at = '' OR "
        f"outreach_at < datetime('now','localtime','-{minutes} minutes')) "
        "ORDER BY id"
    )
    return conn.execute(sql).fetchall()


def _misjudged_not_found(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """把「误判搜不到」的线索从 skipped 改成 not_found（可重试）

    为什么（2026-11-13 实测）：
    抖音一次搜索只给 6~10 个人，目标常常在第 2、3 页；
    而且搜索结果是概率性的。
    以前搜不到就标 skipped（终态）→ 线索永远不再被触达。
    实测抽查 4 个「搜不到」的号，**3 个当场就搜到了**。

    现在 not_found 是可重试状态：留在队列里、排在一堆正常线索后面。
    """
    sql = (
        "SELECT id, display_id, nickname, outreach_note, bitable_record_id "
        "FROM leads WHERE display_id != '' AND outreach_status = 'skipped' "
        "AND outreach_note LIKE '%没搜到这个人%' ORDER BY id"
    )
    return conn.execute(sql).fetchall()


def _push_feishu(rows: list[sqlite3.Row], label: str = "待触达") -> tuple[int, str]:
    """把「触达状态」写回飞书表（只改这一个字段，不动销售改过的状态）"""
    from src.notify.bitable import get_bitable_writer

    cfg = get_config()
    writer = get_bitable_writer(cfg)
    if not writer.is_configured:
        return 0, "飞书写入器没配置"

    async def _run() -> tuple[int, str]:
        ok = 0
        err = ""
        for r in rows:
            rid = (r["bitable_record_id"] or "").strip()
            if not rid:
                continue
            try:
                good, msg = await writer.patch_fields(rid, {"触达状态": label})
            except Exception as e:
                good, msg = False, str(e)
            if good:
                ok += 1
            elif err:
                err = msg
        return ok, err

    return asyncio.run(_run())


def _push_feishu_label(rows: list[sqlite3.Row], label: str) -> tuple[int, str]:
    """同上，只是指定「触达状态」那一列要写什么"""
    return _push_feishu(rows, label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看会改哪些，不真改")
    ap.add_argument(
        "--stuck-minutes", type=int, default=30,
        help="卡在「搜索中/已定位」超过多少分钟算卡死（默认 30；"
             "刚被强杀的进程留下的可以调小）",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = _pick(conn)
    stuck = _release_stuck(conn, minutes=args.stuck_minutes)

    print(f"找到 {len(rows)} 条被平台侧故障误杀的线索")
    for r in rows[:20]:
        print(f"  {r['display_id']:<18} {(r['nickname'] or '')[:14]:<16} "
              f"{(r['outreach_note'] or '')[:52]}")
    if len(rows) > 20:
        print(f"  …… 还有 {len(rows) - 20} 条")

    print(f"\n找到 {len(stuck)} 条卡在「搜索中 / 已定位」的线索"
          f"（轮次中断留下的，永远没人管）")
    for r in stuck[:10]:
        print(f"  {r['display_id']:<18} {(r['nickname'] or '')[:14]:<16} "
              f"{(r['outreach_note'] or '')[:52]}")

    misjudged = _misjudged_not_found(conn)
    print(f"\n找到 {len(misjudged)} 条被误判成「搜不到」的线索"
          f"（实测大多数其实搜得到，要能重试）")

    if args.dry_run:
        conn.close()
        print("\n（dry-run，没改任何东西）")
        return 0

    if not rows and not stuck and not misjudged:
        conn.close()
        return 0

    # ---- 1. 本地改回 pending ----
    #
    # ⚠ bitable_synced_status 这一列很关键，别随便动它：
    #   飞书「回拉」靠它判断「本地这次有没有自己改过」。
    #   它是**空**的时候，回拉会认为「本地还是默认值」→ 直接采信飞书的值
    #   → 把你刚改回 pending 的线索又盖成 failed（实测踩过：改完 141 条，
    #   几分钟后又变回 115 条 failed）。
    #   所以给空的补一个「跟当前本地值不一样」的标记，本地改动就被保护住了。
    all_rows = rows + stuck
    ids = [r["id"] for r in all_rows]
    note = "平台侧故障误标失败，已重置待触达（2026-11-13）"
    conn.execute(
        "UPDATE leads SET outreach_status = 'pending', outreach_note = ?, "
        "outreach_at = datetime('now','localtime'), "
        "bitable_synced_status = CASE "
        "WHEN COALESCE(bitable_synced_status, '') = '' THEN '待处理|失败' "
        "ELSE bitable_synced_status END "
        "WHERE id IN " + f"({','.join('?' * len(ids))})",
        [note, *ids],
    )
    conn.commit()
    conn.close()
    print(f"\n✅ 本地已把 {len(ids)} 条改回 pending"
          f"（误杀 {len(rows)} 条 + 卡住 {len(stuck)} 条）")

    # ---- 1b. 「误判搜不到」的 → not_found（可重试，不是终态）----
    if misjudged:
        m_ids = [r["id"] for r in misjudged]
        conn2 = sqlite3.connect(DB_PATH)
        conn2.execute(
            "UPDATE leads SET outreach_status = 'not_found', "
            "outreach_at = datetime('now','localtime') WHERE id IN "
            + f"({','.join('?' * len(m_ids))})",
            m_ids,
        )
        conn2.commit()
        conn2.close()
        print(f"✅ 已把 {len(m_ids)} 条「误判搜不到」的改成 not_found（可重试）")

    # ---- 2. 飞书表同步（只改「触达状态」这一列）----
    ok, err = _push_feishu(all_rows)
    if ok:
        print(f"✅ 飞书表已同步 {ok} 条为「待触达」")
    if err:
        print(f"（飞书同步有失败：{err}）")

    if misjudged:
        ok2, err2 = _push_feishu_label(misjudged, "搜不到")
        if ok2:
            print(f"✅ 飞书表已同步 {ok2} 条为「搜不到」")
        if err2:
            print(f"（飞书同步有失败：{err2}）")

    # ---- 3. 复核 ----
    st = OutreachStore(DB_PATH)
    try:
        pend = len(st.pending_leads())
    finally:
        st.close()
    print(f"\n现在待触达队列里有 {pend} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
