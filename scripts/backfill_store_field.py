#!/usr/bin/env python3
"""回填飞书表的「门店」字段 + 初始化本地同步状态标记

背景
----
v2.0.10 给线索表加了「门店」列——14 个门店的线索混在一张表里，
销售没法按门店筛选、也没法把线索分给对应门店的人。

配置里的门店名写法很杂：
    '小米汽车｜世茂广场店' / '小米汽车 | 成都龙湖滨江体验店' / '小米汽车 ｜ 机场路店'
分号混用了全角竖线、半角竖线，还夹着空格。归一化成短名后
（「世茂广场店」「成都龙湖滨江体验店」）才能按同一套名字筛选。

同时做另一件事：初始化 ``leads.bitable_synced_status``。
这个字段记录「上次同步进飞书时的状态」，是状态双向同步的锚点。
没有它的话，第一次同步时无法判断本地状态到底变没变，
会把销售在飞书里改过的值覆盖回去。

**幂等**：按抖音号匹配，重复执行只更新不新增。

用法::

    .venv/bin/python scripts/backfill_store_field.py --dry-run
    .venv/bin/python scripts/backfill_store_field.py
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.notify.bitable import (  # noqa: E402
    STATUS_LABELS,
    OUTREACH_LABELS,
    normalize_store,
)

DEFAULT_DB = ROOT / "data" / "db" / "douyin_ppp.db"

ENV_APP_TOKEN = "DOUYIN_PPP_BITABLE_APP_TOKEN"
ENV_TABLE_ID = "DOUYIN_PPP_BITABLE_TABLE_ID"

LARK_CLI = "/Users/jack/.npm-global/bin/lark-cli"


def cli(args: list[str], timeout: int = 180) -> dict:
    """调 lark-cli，返回解析后的 JSON

    必须带 --as user --format json，否则输出不是 JSON。
    """
    proc = subprocess.run(
        [LARK_CLI, *args, "--as", "user", "--format", "json"],
        capture_output=True, text=True, timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError(f"lark-cli 无输出: {(proc.stderr or '')[:300]}")
    import json
    data = json.loads(out)
    if not data.get("ok", True):
        raise RuntimeError(str(data.get("error") or data)[:400])
    return data


def cli_write_json_file(args: list[str], payload: dict) -> dict:
    """lark-cli 的 --json @file 只接受 cwd / /tmp / ~/files 下的路径"""
    import json

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8", dir="/tmp"
    ) as f:
        json.dump(payload, f, ensure_ascii=False)
        tmp = f.name
    try:
        return cli([*args, "--json", f"@{tmp}"])
    finally:
        Path(tmp).unlink(missing_ok=True)


def resolve_table_ids(args) -> tuple[str, str]:
    """表标识：命令行 > 环境变量 > 本地 config.yaml"""
    import os
    import yaml

    app_token = args.app_token or os.environ.get(ENV_APP_TOKEN, "")
    table_id = args.table_id or os.environ.get(ENV_TABLE_ID, "")

    if not app_token or not table_id:
        cfg_path = ROOT / "config.yaml"
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            notify = cfg.get("notify") or {}
            app_token = app_token or notify.get("bitable_app_token", "")
            table_id = table_id or notify.get("bitable_table_id", "")

    if not app_token or not table_id:
        raise SystemExit(
            "缺少表标识。用 --app-token/--table-id 指定，"
            "或设环境变量，或在 config.yaml 里填 notify.bitable_app_token / bitable_table_id"
        )
    return app_token, table_id


def load_leads(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT lead_key, display_id, nickname, intent_level, intent_score, "
            "intent_reason, car_model, contact_value, monitor_name, live_room, "
            "evidence, status, outreach_status, bitable_record_id "
            "FROM leads ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_records(base: str, table: str) -> tuple[list[str], dict, list[str]]:
    """拉全表，返回 (record_id 列表, 字段名列表, 行数据)"""
    data = cli([
        "base", "+record-list",
        "--base-token", base,
        "--table-id", table,
        "--limit", "200",
    ])
    d = data.get("data") or {}
    return (
        [str(x) for x in (d.get("record_id_list") or [])],
        list(d.get("fields") or []),
        list(d.get("data") or []),
    )


def cell_text(value) -> str:
    """把单元格的多种形态统一成字符串"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    if isinstance(value, list):
        return "".join(cell_text(x) for x in value)
    return str(value)


def build_patch(lead: dict) -> dict:
    """只更新「门店」列——其他列不动，避免覆盖销售改过的状态"""
    store = normalize_store(lead.get("monitor_name") or "")
    if not store:
        return {}
    return {"门店": store}


def main() -> int:
    ap = argparse.ArgumentParser(description="回填飞书表门店字段 + 初始化同步标记")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--app-token", default="")
    ap.add_argument("--table-id", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    app_token, table_id = resolve_table_ids(args)

    leads = load_leads(args.db)
    print(f"本地线索 {len(leads)} 条")

    record_ids, fields, rows = list_records(app_token, table_id)
    print(f"飞书表 {len(record_ids)} 条记录，字段 {fields}")

    if "抖音号" not in fields:
        print("错误：表里没有「抖音号」列，无法匹配")
        return 1
    i_dy = fields.index("抖音号")

    # 抖音号 → record_id
    by_dy: dict[str, str] = {}
    for rid, row in zip(record_ids, rows):
        if i_dy >= len(row):
            continue
        dy = cell_text(row[i_dy])
        if dy:
            by_dy[dy] = rid
    print(f"匹配到 {len(by_dy)} 个抖音号")

    updates: dict[str, dict] = {}
    unmatched = 0
    for lead in leads:
        dy = (lead.get("display_id") or "").strip()
        rid = by_dy.get(dy)
        if not rid:
            unmatched += 1
            continue
        patch = build_patch(lead)
        if patch:
            updates[rid] = patch

    if unmatched:
        print(f"有 {unmatched} 条本地线索在飞书表里找不到对应记录（跳过）")

    if not updates:
        print("没有需要回填的内容")
    elif args.dry_run:
        print(f"[dry-run] 将更新 {len(updates)} 条记录")
        for rid, patch in list(updates.items())[:5]:
            print(f"  {rid}: {patch}")
    else:
        # 分批更新（单次上限 200）
        done = 0
        items = list(updates.items())
        for i in range(0, len(items), 200):
            chunk = dict(items[i:i + 200])
            cli_write_json_file(
                ["base", "+record-batch-update",
                 "--base-token", app_token, "--table-id", table_id],
                {"update_records": chunk},
            )
            done += len(chunk)
            print(f"  已更新 {done}/{len(items)}")
        print(f"✅ 回填完成：{done} 条记录")

    # 初始化本地同步状态标记
    # 不做这一步的话，第一次同步无法判断本地状态变没变，
    # 会把销售在飞书里改过的值覆盖回默认值。
    conn = sqlite3.connect(args.db)
    try:
        updated = 0
        for lead in leads:
            st = STATUS_LABELS.get(lead.get("status") or "", "待处理")
            os_ = OUTREACH_LABELS.get(lead.get("outreach_status") or "", "待触达")
            marker = f"{st}|{os_}"
            cur = conn.execute(
                "UPDATE leads SET bitable_synced_status = ? "
                "WHERE lead_key = ? AND (bitable_synced_status IS NULL OR bitable_synced_status = '')",
                (marker, lead.get("lead_key")),
            )
            updated += cur.rowcount
        conn.commit()
        print(f"✅ 初始化本地同步标记：{updated} 条")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
