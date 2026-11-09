#!/usr/bin/env python3
"""扫飞书表全表的记录变更历史，找出「曾经留言过、但当前状态是待触达」的线索

背景
----
飞书表是多台机器共用的唯一真相源。但「重置触达状态」这个操作会把状态改回
「待触达」，把「已经联系过」这个事实**擦掉**——所有机器都以为没联系过，
于是重复评论同一个客户（抖音上重复评论是明确的限流/封号风险）。

真实案例：抖音号 42917865256 在 11-08 23:51 已经是「已留言」，
11-09 00:10 被重置回「待触达」，00:20 又重置一次。

飞书的记录变更历史里还留着这条事实。本脚本把全表扫一遍，
把「历史上有过已留言、但当前是待触达」的记录全找出来。

用法::

    .venv/bin/python scripts/find_reset_touches.py
    .venv/bin/python scripts/find_reset_touches.py --apply   # 补写回飞书
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENV_APP_TOKEN = "DOUYIN_PPP_BITABLE_APP_TOKEN"
ENV_TABLE_ID = "DOUYIN_PPP_BITABLE_TABLE_ID"
LARK_CLI = os.environ.get("LARK_CLI_BIN", "/Users/jack/.npm-global/bin/lark-cli")

# 只有「已留言」才代表真的评论发出去了。
# 「已定位」只是找到了人主页，没留言——不算触达过。
TOUCHED = ("已留言",)


def cli(args: list[str], timeout: int = 120) -> dict:
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


def resolve_table_ids(args) -> tuple[str, str]:
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
        raise SystemExit("缺少表标识（--app-token/--table-id / 环境变量 / config.yaml）")
    return app_token, table_id


def list_records(base: str, table: str) -> list[dict]:
    """拉全表，返回 [{record_id, 抖音号, 昵称, 触达状态, 处理状态}]"""
    data = cli(["base", "+record-list", "--base-token", base,
                "--table-id", table, "--limit", "200"])
    d = data.get("data") or {}
    names = d.get("fields") or []
    rows = d.get("data") or []
    ids = d.get("record_id_list") or []

    def col(name: str) -> int:
        return names.index(name) if name in names else -1

    i_dy, i_nick, i_os, i_st = (
        col("抖音号"), col("昵称"), col("触达状态"), col("处理状态")
    )
    out = []
    for idx, r in enumerate(rows):
        def val(i):
            if i < 0 or i >= len(r) or r[i] is None:
                return ""
            v = r[i]
            if isinstance(v, list):
                return v[0] if v else ""
            return str(v)

        out.append({
            "record_id": ids[idx] if idx < len(ids) else "",
            "douyin_id": val(i_dy),
            "nickname": val(i_nick),
            "outreach": val(i_os),
            "status": val(i_st),
        })
    return out


def history_states(base: str, table: str, record_id: str) -> list[str]:
    """这条记录历史上出现过的所有触达状态"""
    try:
        data = cli(["base", "+record-history-list", "--base-token", base,
                    "--table-id", table, "--record-id", record_id])
    except Exception:
        return []
    items = (data.get("data") or {}).get("items") or []
    states = []
    for it in items:
        for c in (it.get("field_changes") or []):
            if c.get("field_name") == "触达状态":
                states.append(str(c.get("after") or ""))
    return states


def main() -> int:
    ap = argparse.ArgumentParser(description="找出被重置掉的真实触达记录")
    ap.add_argument("--app-token", default="")
    ap.add_argument("--table-id", default="")
    ap.add_argument("--out", default="/tmp/reset_touches.csv")
    ap.add_argument("--apply", action="store_true",
                    help="把查出来的记录补写回飞书（状态改回已留言）")
    args = ap.parse_args()

    app_token, table_id = resolve_table_ids(args)
    records = list_records(app_token, table_id)
    print(f"全表 {len(records)} 条，开始查变更历史…")

    def check(r):
        if not r["record_id"]:
            return None
        states = history_states(app_token, table_id, r["record_id"])
        if not states:
            return None
        if r["outreach"] in TOUCHED:
            return None  # 当前状态本来就是「已触达」，没问题
        if any(s in TOUCHED for s in states):
            return {**r, "ever": ",".join(sorted(set(states)))}
        return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        hits = [h for h in ex.map(check, records) if h]

    print(f"\n被重置掉的真实触达：{len(hits)} 条")
    for h in hits:
        print(f"  {h['douyin_id']:<18} {h['nickname'][:14]:<16} 现={h['outreach']:<6} 历史上={h['ever']}")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(hits[0].keys()) if hits else ["record_id"])
        w.writeheader()
        w.writerows(hits)
    print(f"\n明细已存：{args.out}")

    if hits and args.apply:
        fid_data = cli(["base", "+field-list", "--base-token", app_token, "--table-id", table_id])
        fid = ""
        for fl in (fid_data.get("data") or {}).get("fields") or []:
            if fl.get("name") == "触达状态":
                fid = fl.get("id") or ""
        payload = {"update_records": {h["record_id"]: {fid: ["已留言"]} for h in hits}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8", dir="/tmp") as f:
            json.dump(payload, f, ensure_ascii=False)
            tmp = f.name
        try:
            cli(["base", "+record-batch-update", "--base-token", app_token,
                 "--table-id", table_id, "--json", f"@{tmp}"])
        finally:
            Path(tmp).unlink(missing_ok=True)
        print(f"已把 {len(hits)} 条补写回「已留言」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
