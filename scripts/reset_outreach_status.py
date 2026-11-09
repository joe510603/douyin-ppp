#!/usr/bin/env python3
"""批量重置线索的「触达状态」（飞书表 + 本地库同步）

背景
----
2026-11-09 发现飞书表里有 38 条线索的「触达状态」是已留言/已跳过/失败/已定位，
但本地 ``outreach_records`` 一条执行记录都没有、抖音也从未登录过——
这些状态是调试双向同步时用 lark-cli 手动写进去的**测试残留**。

后果很实在：
- 22 个真实客户在飞书表里显示「已留言」，销售以为联系过了，不再跟进
- 这些线索同时被排除在「待触达」队列之外，自动触达也永远不会碰它们

所以状态必须**两边一起**改回「待触达」，否则下一轮回拉同步又会把旧值带回来。

坑：``+record-batch-update`` 用**中文字段名**会静默失败
----
返回 ok、rev 也涨，但值根本没变、变更历史里也不留痕迹。
必须用字段 ID（``fldXXXX``）。本脚本用 ``+field-list`` 动态查，不硬编码。

用法::

    # 看看哪些线索会被改（默认只打印，不写）
    .venv/bin/python scripts/reset_outreach_status.py

    # 只重置指定抖音号
    .venv/bin/python scripts/reset_outreach_status.py --display-id 601939522 --apply

    # 真的执行（重置所有非 pending 的线索）
    .venv/bin/python scripts/reset_outreach_status.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "db" / "douyin_ppp.db"

ENV_APP_TOKEN = "DOUYIN_PPP_BITABLE_APP_TOKEN"
ENV_TABLE_ID = "DOUYIN_PPP_BITABLE_TABLE_ID"

LARK_CLI = os.environ.get("LARK_CLI_BIN", "/Users/jack/.npm-global/bin/lark-cli")

TARGET_FIELD = "触达状态"
RESET_LABEL = "待触达"


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
    data = json.loads(out)
    if not data.get("ok", True):
        raise RuntimeError(str(data.get("error") or data)[:400])
    return data


def cli_write_json_file(args: list[str], payload: dict) -> dict:
    """lark-cli 的 --json @file 只接受 cwd / /tmp / ~/files 下的路径"""
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
    app_token = args.app_token or os.environ.get(ENV_APP_TOKEN, "")
    table_id = args.table_id or os.environ.get(ENV_TABLE_ID, "")

    if not app_token or not table_id:
        cfg_path = ROOT / "config.yaml"
        if cfg_path.exists():
            import yaml

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


def field_id_of(base: str, table: str, field_name: str) -> str:
    """查字段 ID —— batch_update 只认 field_id，中文名会静默失败"""
    data = cli(["base", "+field-list", "--base-token", base, "--table-id", table])
    for f in (data.get("data") or {}).get("fields") or []:
        if f.get("name") == field_name:
            return f.get("id") or ""
    raise SystemExit(f"表里没有「{field_name}」这一列")


def load_targets(db_path: str, display_ids: list[str]) -> list[dict]:
    """要重置的线索：本地有 record_id（对得上飞书记录）且触达状态不是 pending"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT id, display_id, nickname, outreach_status, "
            "bitable_record_id, bitable_synced_status FROM leads "
            "WHERE outreach_status != 'pending' AND bitable_record_id != ''"
        )
        params: list = []
        if display_ids:
            sql += f" AND display_id IN ({','.join('?' * len(display_ids))})"
            params = list(display_ids)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="批量重置触达状态（飞书 + 本地）")
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--app-token", default="")
    ap.add_argument("--table-id", default="")
    ap.add_argument(
        "--display-id", action="append", default=[],
        help="只重置指定抖音号，可重复；不给则重置所有非 pending 的线索",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="真的写飞书和本地库；不加这个只打印计划",
    )
    args = ap.parse_args()

    app_token, table_id = resolve_table_ids(args)
    targets = load_targets(args.db_path, args.display_id)

    if not targets:
        print("没有需要重置的线索（触达状态全是 pending，或没有对应的飞书记录）")
        return 0

    print(f"待重置 {len(targets)} 条：")
    for t in targets:
        print(f"  {t['display_id']:<18} {t['nickname'][:12]:<14} {t['outreach_status']}")

    if not args.apply:
        print("\n（只预览。确认无误后加 --apply 执行）")
        return 0

    # --- 飞书侧：用字段 ID 批量改，PATCH 语义不碰「处理状态」 ---
    fid = field_id_of(app_token, table_id, TARGET_FIELD)
    payload = {
        "update_records": {
            t["bitable_record_id"]: {fid: [RESET_LABEL]} for t in targets
        }
    }
    res = cli_write_json_file(
        ["base", "+record-batch-update", "--base-token", app_token, "--table-id", table_id],
        payload,
    )
    print(f"\n飞书已更新 {len(res.get('data', {}).get('record_id_list') or [])} 条 → 「{RESET_LABEL}」")

    # --- 本地侧：状态回 pending，标记同步成「待处理|待触达」 ---
    conn = sqlite3.connect(args.db_path)
    try:
        ids = [t["id"] for t in targets]
        conn.execute(
            f"UPDATE leads SET outreach_status='pending', outreach_note='', "
            f"outreach_at='', bitable_synced_status='待处理|{RESET_LABEL}' "
            f"WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        conn.commit()
    finally:
        conn.close()
    print(f"本地已更新 {len(ids)} 条 → pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
