#!/usr/bin/env python3
"""飞书多维表格引导脚本 —— 建字段 + 灌线索 + 回写 record_id

用途
----
本项目在**运行时**用「企业自建应用 app_id + app_secret」换
tenant_access_token 写多维表格（见 src/notify/bitable.py）。但建表阶段
需要「能建字段、能写记录」的应用权限，这一步走 lark-cli 的**用户身份**
完成——用户自己的身份天然有这张表的编辑权。

本脚本负责把「挖掘线索自动上表」整理成生产形态：
1. 把飞书默认表结构（文本/单选/日期/附件）改造成线索表结构（12 字段）
2. 清掉建表时留下的空行
3. 把本地线索池灌进多维表格
4. 把生成的 record_id 回写到 leads.bitable_record_id，供后续增量更新

**幂等**：按「抖音号」判重，已存在的线索走 update，不会产生重复行。

用法::

    .venv/bin/python scripts/bootstrap_bitable.py --dry-run
    .venv/bin/python scripts/bootstrap_bitable.py
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

from src.notify.bitable import LEAD_TABLE_FIELDS_V3 as FIELDS  # noqa: E402

DEFAULT_DB = ROOT / "data" / "db" / "douyin_ppp.db"

# 多维表格标识（app_token / table_id）**不进版本库**：
# 本仓库是公开的，表里存着真实客户线索，表标识没必要公开。
# 读取优先级：命令行参数 > 环境变量 > 本地 config.yaml
ENV_APP_TOKEN = "DOUYIN_PPP_BITABLE_APP_TOKEN"
ENV_TABLE_ID = "DOUYIN_PPP_BITABLE_TABLE_ID"

FIELD_NAMES = [f["name"] for f in FIELDS]

# 说明：不再复用飞书默认表的「文本/单选/日期」字段。
# 飞书的主字段恒在最左列且 API 不允许改主字段，要让「发现时间」排第一，
# 必须在建表时就把发现时间作为第一个字段——见 scripts/migrate_bitable_layout.py。

LEVEL_MAP = {"high": "高意向", "medium": "中意向", "low": "低意向"}
STATUS_MAP = {"new": "待处理", "pushed": "待处理", "contacted": "已跟进", "ignored": "已忽略"}
OUTREACH_MAP = {
    "pending": "待触达", "searching": "搜索中", "found": "已定位",
    "commented": "已留言", "failed": "失败", "skipped": "已跳过",
}


# ============================================================
# lark-cli 封装
# ============================================================

def cli(args: list[str], timeout: int = 180) -> dict:
    """调用 lark-cli（用户身份），返回解析后的 JSON"""
    cmd = ["lark-cli", *args, "--as", "user", "--format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError(f"lark-cli 无输出: {' '.join(cmd)}\nstderr: {proc.stderr[:500]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"lark-cli 返回非 JSON:\n{out[:800]}\nstderr: {proc.stderr[:500]}")
    if not data.get("ok", True):
        raise RuntimeError(f"lark-cli 失败: {json.dumps(data, ensure_ascii=False)[:800]}")
    return data


def cli_write_json_file(args: list[str], payload: dict) -> dict:
    """把 JSON payload 写临时文件后用 --json @file 传（避免命令行转义地狱）

    注意：--json @file 只接受 cwd、/tmp、~/files 下的路径
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8", dir="/tmp") as f:
        json.dump(payload, f, ensure_ascii=False)
        path = f.name
    try:
        return cli([*args, "--json", f"@{path}"])
    finally:
        Path(path).unlink(missing_ok=True)


# ============================================================
# 步骤 1：字段结构
# ============================================================

def reconcile_fields(base: str, table: str, dry_run: bool) -> dict:
    """把表结构对齐到目标字段集，并固定视图里的字段顺序。

    注意：**飞书的主字段恒在最左列，且 API 不允许改「哪个字段是主字段」**
    （`+field-update` 传 primary 会被拒）。所以「发现时间」要排第一，
    只能建表时就把它放在第一个字段位——见 scripts/migrate_bitable_layout.py。

    这里只负责：
    1. 补建缺失的字段
    2. 用视图的 visible_fields 固定显示顺序（主字段会被后端强制顶到第一位）
    """
    data = cli(["base", "+field-list", "--base-token", base, "--table-id", table])
    existing = {f["name"]: f for f in (data.get("data") or {}).get("fields") or []}
    print(f"当前字段：{list(existing)}")

    created: list[str] = []

    # 1. 补建缺失字段
    for spec in FIELDS:
        if spec["name"] in existing:
            continue
        if dry_run:
            print(f"  [dry-run] 新建字段 {spec['name']}")
            created.append(spec["name"])
            continue
        cli_write_json_file(
            ["base", "+field-create", "--base-token", base, "--table-id", table],
            spec,
        )
        print(f"  ✅ 新建字段 {spec['name']}")
        created.append(spec["name"])

    # 2. 固定视图字段顺序
    order = [f["name"] for f in FIELDS]
    views = cli(["base", "+view-list", "--base-token", base, "--table-id", table])
    for view in (views.get("data") or {}).get("views") or []:
        vid = view.get("id")
        if not vid:
            continue
        if dry_run:
            print(f"  [dry-run] 视图「{view.get('name')}」字段顺序 → {order}")
            continue
        cli_write_json_file(
            ["base", "+view-set-visible-fields", "--base-token", base,
             "--table-id", table, "--view-id", vid],
            {"visible_fields": order},
        )
        print(f"  ✅ 视图「{view.get('name')}」字段顺序已固定")

    if not dry_run:
        data = cli(["base", "+field-list", "--base-token", base, "--table-id", table])
        now = [f["name"] for f in (data.get("data") or {}).get("fields") or []]
        print(f"字段就绪：{now}")

    return {"created": created, "renamed": [], "dropped": []}


# ============================================================
# 步骤 2：读取线索
# ============================================================

def load_leads(db_path: str, limit: int = 0) -> list[dict]:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM leads WHERE intent_level IN ('high','medium','low') ORDER BY intent_score DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()
    return rows


def build_row(lead: dict) -> list:
    """把一条线索映射成多维表格的一行（顺序同 FIELD_NAMES）"""
    evidence = lead.get("evidence") or ""
    if evidence.startswith("["):
        try:
            items = json.loads(evidence)
            evidence = " / ".join(str(i) for i in items)
        except (ValueError, TypeError):
            pass

    def txt(v):
        v = (v or "").strip()
        return v if v else None

    # 按**字段名**返回字典（不是位置数组）——字段顺序一变位置数组就会静默写错列
    return {
        "发现时间": lead.get("created_at") or None,
        "抖音号": txt(lead.get("display_id")) or txt(lead.get("lead_key")),
        "昵称": txt(lead.get("nickname")),
        "意向等级": LEVEL_MAP.get(lead.get("intent_level") or ""),
        "意向分": int(lead.get("intent_score") or 0),
        "车型": txt(lead.get("car_model")),
        "弹幕原文": txt(evidence),
        "判定依据": txt(lead.get("intent_reason")),
        "来源直播间": txt(lead.get("monitor_name")) or txt(lead.get("live_room")),
        "联系方式": txt(lead.get("contact_value")),
        "处理状态": STATUS_MAP.get(lead.get("status") or "", "待处理"),
        "触达状态": OUTREACH_MAP.get(lead.get("outreach_status") or "", "待触达"),
    }


# ============================================================
# 步骤 3：记录
# ============================================================

def list_records(base: str, table: str) -> tuple[list[str], dict, list[str]]:
    """返回 (字段名, {抖音号: record_id}, 空行 record_id 列表)"""
    data = cli(["base", "+record-list", "--base-token", base, "--table-id", table, "--limit", "200"])
    d = data.get("data") or {}
    names = d.get("fields") or []
    ids = d.get("record_id_list") or []
    rows = d.get("data") or []

    try:
        dy = names.index("抖音号")
    except ValueError:
        dy = None

    by_dy: dict[str, str] = {}
    blanks: list[str] = []
    for rid, row in zip(ids, rows):
        val = ""
        if dy is not None and dy < len(row):
            raw = row[dy]
            if isinstance(raw, list):
                val = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in raw)
            elif raw is not None:
                val = str(raw)
        if val.strip():
            by_dy[val.strip()] = rid
        else:
            blanks.append(rid)
    return names, by_dy, blanks


def upsert_leads(base: str, table: str, leads: list[dict], by_dy: dict[str, str]) -> tuple[int, int, int]:
    """写入线索。已存在走 update，新的走 batch_create。返回 (新建, 更新, 失败)"""
    created = updated = failed = 0
    todo: list[dict] = []

    for lead in leads:
        row = build_row(lead)
        dy = row.get("抖音号")
        if not dy:
            failed += 1
            continue
        rid = by_dy.get(dy)
        if rid:
            payload = {k: v for k, v in row.items() if v is not None}
            try:
                cli_write_json_file(
                    ["base", "+record-upsert", "--base-token", base, "--table-id", table,
                     "--record-id", rid],
                    payload,
                )
                updated += 1
            except RuntimeError as e:
                print(f"  ⚠️  更新失败 {dy}: {e}")
                failed += 1
        else:
            todo.append(row)

    # 批量新建，每批最多 200 行
    # 注意：CLI 实际接受的格式是 {"create_records":[{字段映射}, ...]}，
    # 不是旧文档里的 {"fields":[...], "rows":[[...]]}
    for i in range(0, len(todo), 200):
        chunk = todo[i:i + 200]
        records = [{k: v for k, v in row.items() if v is not None} for row in chunk]
        try:
            cli_write_json_file(
                ["base", "+record-batch-create", "--base-token", base, "--table-id", table],
                {"create_records": records},
            )
            created += len(chunk)
            print(f"  ✅ 批量新建 {len(chunk)} 行")
        except RuntimeError as e:
            print(f"  ⚠️  批量新建失败: {e}")
            failed += len(chunk)

    return created, updated, failed


def delete_blanks(base: str, table: str, blanks: list[str]) -> int:
    """删除空行。--record-id 可重复传，一次删一批"""
    n = 0
    for i in range(0, len(blanks), 50):
        chunk = blanks[i:i + 50]
        args = ["base", "+record-delete", "--base-token", base, "--table-id", table]
        for rid in chunk:
            args += ["--record-id", rid]
        try:
            cli([*args, "--yes"])
            n += len(chunk)
        except RuntimeError as e:
            print(f"  ⚠️  删除空行失败: {e}")
    return n


def write_back_record_ids(db_path: str, by_dy: dict[str, str]) -> int:
    """把 record_id 回写到 leads.bitable_record_id"""
    conn = sqlite3.connect(db_path, timeout=15)
    conn.execute("PRAGMA busy_timeout = 5000")
    rows = [(rid, dy) for dy, rid in by_dy.items()]
    conn.executemany(
        "UPDATE leads SET bitable_record_id = ? WHERE display_id = ? OR lead_key = ?",
        [(rid, dy, dy) for rid, dy in rows],
    )
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) FROM leads WHERE bitable_record_id != ''"
    ).fetchone()[0]
    conn.close()
    return n


# ============================================================
# main
# ============================================================

def resolve_table_ids(args) -> tuple[str, str]:
    """决定用哪张表：命令行 > 环境变量 > 本地 config.yaml"""
    import os

    base = (args.base_token or "").strip()
    table = (args.table_id or "").strip()

    if not base:
        base = (os.environ.get(ENV_APP_TOKEN) or "").strip()
    if not table:
        table = (os.environ.get(ENV_TABLE_ID) or "").strip()

    if not base or not table:
        # 退回本地 config.yaml 的 notify 段
        cfg_path = ROOT / "config.yaml"
        if cfg_path.exists():
            import yaml

            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            notify = data.get("notify") or {}
            base = base or (notify.get("bitable_app_token") or "").strip()
            table = table or (notify.get("bitable_table_id") or "").strip()

    if not base or not table:
        raise SystemExit(
            "缺少多维表格标识。任选一种方式提供：\n"
            f"  1) 命令行：--base-token <app_token> --table-id <tbl...>\n"
            f"  2) 环境变量：{ENV_APP_TOKEN} / {ENV_TABLE_ID}\n"
            "  3) 本地 config.yaml 的 notify.bitable_app_token / notify.bitable_table_id\n"
            "（本仓库公开，标识刻意不写进代码）"
        )
    return base, table


def main() -> int:
    ap = argparse.ArgumentParser(description="飞书多维表格引导（建字段 + 灌线索）")
    ap.add_argument("--base-token", default="", help="多维表格 App Token")
    ap.add_argument("--table-id", default="", help="数据表 ID（tbl...）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--limit", type=int, default=0, help="最多灌多少条线索，0=全部")
    ap.add_argument("--dry-run", action="store_true", help="只看字段变更，不写库不灌数据")
    args = ap.parse_args()

    base_token, table_id = resolve_table_ids(args)
    print(f"目标表：{table_id}（app_token {base_token[:6]}…）")

    print("=== 1. 字段结构 ===")
    reconcile_fields(base_token, table_id, args.dry_run)
    if args.dry_run:
        print("(dry-run 结束)")
        return 0

    print("\n=== 2. 读取线索 ===")
    leads = load_leads(args.db, args.limit)
    print(f"待写入线索 {len(leads)} 条")

    print("\n=== 3. 写入记录 ===")
    _names, by_dy, blanks = list_records(base_token, table_id)
    print(f"表内已有 {len(by_dy)} 条有抖音号的记录，{len(blanks)} 条空行")
    created, updated, failed = upsert_leads(base_token, table_id, leads, by_dy)
    print(f"新建 {created} / 更新 {updated} / 失败 {failed}")

    print("\n=== 4. 清理空行 ===")
    _n2, by_dy2, blanks2 = list_records(base_token, table_id)
    if blanks2:
        n = delete_blanks(base_token, table_id, blanks2)
        print(f"✅ 删除 {n} 条空行")
    else:
        print("无空行")

    print("\n=== 5. 回写 record_id ===")
    _n3, by_dy3, _b3 = list_records(base_token, table_id)
    total = write_back_record_ids(args.db, by_dy3)
    print(f"✅ 已回写 {total} 条线索的 record_id")

    print("\n=== 完成 ===")
    print(f"多维表格最终记录数：{len(by_dy3)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
