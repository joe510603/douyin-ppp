#!/usr/bin/env python3
"""把多维表格重建为「发现时间排最前、只显示日期」的布局

背景
----
飞书多维表格的**主字段恒在最左列**，而开放平台 API 不允许修改
「哪个字段是主字段」（`+field-update` 传 primary 会被拒：
`Unrecognized key(s) in object: 'primary'`）。

所以想让「发现时间」真正排到第一列，只能**重建一张表**：
新建表时第一个字段就是主字段。

做法：
1. 用本地线索池（leads 表，source of truth）作为数据源
2. 新建表，第一个字段是「发现时间」，显示格式 yyyy/MM/dd（不带时分秒）
3. 批量灌入全部线索
4. 打印新的 table_id，由人工确认后再切换 config / 删除旧表

用法::

    .venv/bin/python scripts/migrate_bitable_layout.py --dry-run
    .venv/bin/python scripts/migrate_bitable_layout.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.notify.bitable import (  # noqa: E402
    LEAD_TABLE_FIELDS_V3,
    _lead_to_fields,
)

DEFAULT_DB = ROOT / "data" / "db" / "douyin_ppp.db"
DEFAULT_BASE_TOKEN = "Ca1gbNpl1aCcaSsfwxtcPJmcnFh"
TABLE_NAME = "挖掘线索自动上表"

# 新表的字段顺序：发现时间（主字段）→ 抖音号 → 昵称 → …
# 「昵称」从原来的最后一位挪到第 3 位——销售看表时先看是谁。
NEW_FIELD_ORDER = [
    "发现时间", "抖音号", "昵称", "意向等级", "意向分", "车型",
    "弹幕原文", "判定依据", "来源直播间", "联系方式", "处理状态", "触达状态",
]


def cli(args: list[str], payload: dict | None = None) -> dict:
    cmd = ["lark-cli", *args, "--as", "user", "--format", "json"]
    tmp = None
    try:
        if payload is not None:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8", dir="/tmp"
            ) as f:
                json.dump(payload, f, ensure_ascii=False)
                tmp = f.name
            cmd += ["--json", f"@{tmp}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        out = (proc.stdout or "").strip()
        if not out:
            raise RuntimeError(f"lark-cli 无输出: {(proc.stderr or '')[:400]}")
        data = json.loads(out)
        if not data.get("ok", True):
            raise RuntimeError(json.dumps(data.get("error") or data, ensure_ascii=False)[:500])
        return data
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def new_spec() -> list[dict]:
    """按新顺序重排字段定义，并把发现时间改成只显示日期"""
    by_name = {f["name"]: f for f in LEAD_TABLE_FIELDS_V3}
    out = []
    for name in NEW_FIELD_ORDER:
        spec = json.loads(json.dumps(by_name[name]))   # 深拷贝
        if name == "发现时间":
            spec["style"] = {"format": "yyyy/MM/dd"}
        out.append(spec)
    return out


def load_leads(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM leads WHERE intent_level IN ('high','medium','low') "
        "ORDER BY intent_score DESC, id DESC"
    ).fetchall()]
    conn.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="重建多维表格：发现时间排最前、只显示日期")
    ap.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--table-id", default="", help="复用已建好的表（只灌数据不建表）")
    ap.add_argument("--name", default=TABLE_NAME, help="新表名")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    spec = new_spec()
    print("目标字段顺序：")
    for i, f in enumerate(spec):
        mark = " ← 主字段（第一列）" if i == 0 else ""
        fmt = f.get("style", {}).get("format", "")
        print(f"  {i+1:2d}. {f['name']:<8} [{f['type']}] {fmt}{mark}")

    leads = load_leads(args.db)
    print(f"\n本地线索 {len(leads)} 条")

    if args.dry_run:
        print("(dry-run，未建表)")
        return 0

    # ---- 1. 建新表（第一个字段即主字段）----
    if args.table_id:
        new_table_id = args.table_id
        print(f"\n↻ 复用已有表：{new_table_id}")
    else:
        data = cli([
            "base", "+table-create",
            "--base-token", args.base_token,
            "--name", args.name,
            "--fields", json.dumps(spec, ensure_ascii=False),
        ])
        table = data.get("data", {}).get("table") or {}
        new_table_id = table.get("table_id") or table.get("id")
        print(f"\n✅ 新表已建：{new_table_id}")

    # ---- 2. 灌数据 ----
    fields = _lead_to_fields
    records = []
    for lead in leads:
        row = fields(lead, lead.get("created_at") or "")
        # 只留新表有的字段，值统一成"日期"语义（时分秒照给，显示格式负责裁掉）
        row = {k: v for k, v in row.items() if k in NEW_FIELD_ORDER}
        if row.get("发现时间"):
            row["发现时间"] = str(row["发现时间"])[:19] or None
        records.append({"fields": row})

    created = 0
    for i in range(0, len(records), 200):
        chunk = records[i:i + 200]
        cli([
            "base", "+record-batch-create",
            "--base-token", args.base_token,
            "--table-id", new_table_id,
        ], {"create_records": chunk})
        created += len(chunk)
        print(f"  灌入 {created}/{len(records)}")

    # ---- 3. 校验 ----
    check = cli([
        "base", "+record-list",
        "--base-token", args.base_token,
        "--table-id", new_table_id,
        "--limit", "200",
    ])
    d = check.get("data") or {}
    names = d.get("fields") or []
    print(f"\n新表字段顺序：{names}")
    print(f"新表记录数：{len(d.get('record_id_list') or [])}")

    print("\n=== 下一步（人工确认后执行）===")
    print(f"1. 把 config.yaml 的 notify.bitable_table_id 改成：{new_table_id}")
    print(f"2. 确认新表数据无误后删除旧表：tblPFNAGsfBrBnAI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
