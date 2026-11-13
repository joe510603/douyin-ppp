"""把重建后的「弹幕原文」推回飞书多维表格

背景（2026-11-13）
------------------
用户反馈：「同一个人说的内容只保留了一句」。
根因在 ``upsert_lead``（已修：改成累加），但**历史数据**里
每条线索存的都还是「最后那一句」。

``scripts/backfill_lead_evidence.py`` 负责从 ``live_comments`` 重建本地证据，
这个脚本负责把重建结果**推回飞书表**——
不然销售在飞书里看到的还是那句话。

只推「弹幕原文」这一列（patch_fields），
不动销售改过的「处理状态 / 触达状态」。

用法：
    .venv/bin/python scripts/push_evidence_to_bitable.py --dry-run
    .venv/bin/python scripts/push_evidence_to_bitable.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.notify.bitable import get_bitable_writer  # noqa: E402

DB_PATH = "data/db/douyin_ppp.db"


def _load() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, display_id, nickname, evidence, bitable_record_id "
        "FROM leads WHERE display_id != '' AND evidence IS NOT NULL AND evidence != ''"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        ev = r["evidence"] or ""
        if ev.startswith("["):
            try:
                items = json.loads(ev)
                ev = " / ".join(str(i) for i in items)
            except (ValueError, TypeError):
                pass
        out.append({
            "id": r["id"],
            "record_id": r["bitable_record_id"] or "",
            "text": ev[:1000],
        })
    return out


async def _push(rows: list[dict]) -> tuple[int, int, str]:
    """用**批量更新**推（record-batch-update，一次 200 条）

    为什么不用逐条 patch_fields：lark-cli 每次调用都要解密一次 token
    （实测 1.4~3 秒一条），403 条要跑十几分钟；
    批量接口 403 条只要 3 次调用。
    """
    writer = get_bitable_writer(get_config())
    if not writer.is_configured:
        return 0, 0, "飞书写入器没配置"

    ok = 0
    skip = 0
    err = ""
    chunk: list[tuple[str, dict]] = []
    for r in rows:
        if not r["record_id"]:
            skip += 1
            continue
        chunk.append((r["record_id"], {"弹幕原文": r["text"]}))
        if len(chunk) >= 200:
            ok += await _push_chunk(writer, chunk)
            chunk = []
    if chunk:
        ok += await _push_chunk(writer, chunk)
    return ok, skip, err


async def _push_chunk(writer, chunk: list[tuple[str, dict]]) -> int:
    payload = {"update_records": {rid: fields for rid, fields in chunk}}
    try:
        await writer._run(
            ["base", "+record-batch-update", "--base-token", writer.app_token,
             "--table-id", writer.table_id],
            payload,
        )
        print(f"  ✅ 一批 {len(chunk)} 条推完")
        return len(chunk)
    except Exception as e:
        print(f"  ❌ 这一批失败：{e}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = _load()
    with_record = [r for r in rows if r["record_id"]]
    print(f"要推 {len(rows)} 条，其中 {len(with_record)} 条有 record_id")

    if args.dry_run:
        for r in with_record[:5]:
            print(f"  {r['record_id']} → {r['text'][:90]}")
        print("（dry-run，没推）")
        return 0

    if not with_record:
        print("没有可推的记录（线索都没关联 record_id）")
        return 0

    ok, skip, err = asyncio.run(_push(with_record))
    print(f"\n✅ 推送完成：成功 {ok} 条，跳过（无 record_id）{skip} 条")
    if err:
        print(f"（最后一次错误：{err}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
