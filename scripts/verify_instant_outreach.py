#!/usr/bin/env python3
"""验证「新线索即时触达」链路真的能跑通（干跑，不发真留言）

用**真实的**触发器 + **真实的** runner + **真实的** worker，
只把「发不发光」设成干跑。走完的是生产同一条路径：

    notify → 队列 → 最小间隔 → runner.run(limit=1, only=[抖音号])
            → worker → 搜人 → 挑作品 → 生成话术 → 写触达记录

⚠ 跑这个脚本前**必须先停掉服务** ——
服务和脚本会抢同一个浏览器 profile（data/outreach_profile），
Playwright 不允许两个进程共用一个 profile。

用法：
    python scripts/verify_instant_outreach.py --lead 537
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.config import get_config  # noqa: E402
from src.outreach.instant_trigger import get_instant_trigger  # noqa: E402

DB = os.path.join(ROOT, "data", "db", "douyin_ppp.db")


def _lead_info(lead_id: int) -> dict:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, nickname, display_id, sec_uid, intent_level, intent_score, "
        "outreach_status, evidence FROM leads WHERE id = ?",
        [lead_id],
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit(f"❌ 找不到线索 #{lead_id}")
    return dict(row)


async def _main(lead_id: int, dry: bool) -> int:
    cfg = get_config()
    cfg.outreach.auto_instant = True
    cfg.outreach.auto_instant_dry_run = dry
    cfg.outreach.auto_instant_gap_seconds = 0
    cfg.outreach.enabled = True

    lead = _lead_info(lead_id)
    print(f"=== 验证目标：线索 #{lead_id} ===")
    print(f"  昵称   {lead['nickname']}")
    print(f"  抖音号 {lead['display_id']}")
    print(f"  secUid {lead['sec_uid'] or '（空 → 要走搜索）'}")
    print(f"  意向   {lead['intent_level']}/{lead['intent_score']}　状态 {lead['outreach_status']}")
    print(f"  证据   {lead['evidence']}")
    print(f"  模式   {'干跑（不发）' if dry else '⚠️ 真发'}")
    print()

    trig = get_instant_trigger()
    ok = trig.notify({
        "lead_id": lead["id"],
        "display_id": lead["display_id"],
        "nickname": lead["nickname"],
    })
    print(f"① notify 入队: {ok}（队列 {trig._queue.qsize()} 条）")
    if not ok:
        print("❌ 没入队")
        return 1

    print("② 开始消费队列…")
    t0 = time.time()
    await trig._process(lead["display_id"])
    print(f"③ 处理完成，用时 {time.time() - t0:.1f}s")
    print()

    st = trig.status()
    print("=== 触发器状态 ===")
    for k in ("queued", "sent", "dry", "failed", "not_found", "dropped", "last_error"):
        print(f"  {k:12} = {st.get(k)}")
    print()

    # 查触达记录：干跑会写记录但不回写线索状态
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT id, lead_id, nickname, status, channel, error, created_at, message "
        "FROM outreach_records WHERE lead_id = ? ORDER BY created_at DESC LIMIT 3",
        [lead_id],
    ))
    print(f"=== 触达记录（lead_id={lead_id}）===")
    for r in rows:
        d = dict(r)
        print(f"  {d['created_at']}  status={d['status']}  channel={d['channel']}")
        print(f"    error: {d['error']}")
        msg = str(d.get("message") or "")
        print(f"    文案: {msg[:160]}")
    if not rows:
        print("  （没有记录）")
    conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, required=True, help="线索 id")
    ap.add_argument("--real", action="store_true", help="真发（默认干跑）")
    args = ap.parse_args()
    return asyncio.run(_main(args.lead, not args.real))


if __name__ == "__main__":
    sys.exit(main())
