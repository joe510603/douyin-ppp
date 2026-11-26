#!/usr/bin/env python3
"""修复「被误判成没作品」的线索（2026-11-26）

背景
----
抖音把「主页作品列表」接口静默降级时（HTTP 200 + 0 字节），程序把
「**抓不到**」当成了「**这个人没有作品**」→ 改发私信 + 永久标记 no_works。
结果：
    · 543 条线索被标记成「没作品」（抽样 14 条里 **9 条是误判**，
      这些人实际有 5~107 条作品）
    · 其中 23 条已经被私信消耗掉
    · no_works 标记让它们**再也不会去留言** —— 有视频的客户被私信顶掉了

这个脚本用**还正常的资料接口**（老 web API user/info）逐条核对：
    aweme_count > 0  → 确定是误判 → 清标记 + 退回 pending（重新去留言）
    aweme_count == 0 → 确实没作品 → 保持不动
    查不到（接口空）→ **不下结论**（抖音会静默限流，空响应不等于没有）→ 重试

用法
----
    .venv/bin/python scripts/repair_no_works_misjudge.py --dry-run
    .venv/bin/python scripts/repair_no_works_misjudge.py --apply

⚠️ 老接口会**静默限流**（HTTP 200 + 空 body），所以：
    · 每次请求之间有间隔
    · 空响应一律重试，重试仍失败就记成「查不到」，**绝不当成「没有作品」**
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DB = "data/db/douyin_ppp.db"
# 回 pending 的状态：这些从没成功触达过，退回队列没有副作用
REOPEN_STATUS = ("skipped", "failed", "messaged")


def fetch_aweme_count(sec_uid: str, retries: int = 3) -> tuple[int | None, str]:
    """查这个人的作品总数。

    返回 (作品数, 说明)。作品数为 None = **查不到**（接口空/报错），
    调用方必须把它跟「作品数 0」区分开 —— 这正是这次修复的核心。
    """
    url = (f"https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid={sec_uid}"
           f"&aid=1128")
    last = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
            body = urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            last = f"{type(e).__name__}"
            time.sleep(2 + attempt * 2)
            continue
        if not body:
            # 抖音静默限流：HTTP 200 但响应体是空的 → **不能当成「没有作品」**
            last = "空响应（接口限流）"
            time.sleep(3 + attempt * 3)
            continue
        try:
            j = json.loads(body)
        except Exception:
            last = "响应不是 JSON"
            time.sleep(2 + attempt * 2)
            continue
        ui = j.get("user_info") or {}
        n = ui.get("aweme_count")
        if n is None:
            last = "资料里没有 aweme_count"
            return None, last
        try:
            return int(n), "ok"
        except Exception:
            return None, f"aweme_count 不是数字：{n!r}"
    return None, last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正改库（默认只报告）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0 = 全部）")
    ap.add_argument("--gap", type=float, default=1.5, help="每次请求之间的间隔秒")
    ap.add_argument("--from-report", default="",
                    help="直接用已有报告落库（不再调接口）—— "
                         "默认 data/logs/repair_no_works_misjudge_dryrun.json")
    args = ap.parse_args()

    if args.from_report:
        return _apply_from_report(args.from_report)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, display_id, nickname, sec_uid, outreach_status, no_works
        FROM leads
        WHERE no_works = 1 AND sec_uid != ''
        ORDER BY updated_at DESC
    """).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"待核对：{len(rows)} 条被标记「没作品」的线索\n")

    misjudged: list[dict] = []
    real_none: list[dict] = []
    unknown: list[dict] = []

    for i, r in enumerate(rows, 1):
        n, why = fetch_aweme_count(r["sec_uid"])
        if n is None:
            unknown.append({"id": r["id"], "nickname": r["nickname"],
                            "display_id": r["display_id"], "reason": why})
            verdict = "查不到"
        elif n > 0:
            misjudged.append({"id": r["id"], "nickname": r["nickname"],
                              "display_id": r["display_id"],
                              "status": r["outreach_status"], "aweme_count": n})
            verdict = f"❌误判（有 {n} 条作品）"
        else:
            real_none.append({"id": r["id"], "nickname": r["nickname"],
                              "display_id": r["display_id"]})
            verdict = "真没作品"
        if i % 10 == 0 or verdict.startswith("❌"):
            print(f"  [{i}/{len(rows)}] #{r['id']} {r['nickname'][:12]:<14}"
                  f" {r['outreach_status']:<10} {verdict}")
        time.sleep(args.gap)

    print(f"\n核对完成：误判 {len(misjudged)} / 真没作品 {len(real_none)} / 查不到 {len(unknown)}")

    if not args.apply:
        print("\n（这是演练，没改库。加 --apply 真正修）")
        _report(misjudged, real_none, unknown)
        return 0

    # ---- 真正修 ----
    fixed = 0
    for m in misjudged:
        con.execute("UPDATE leads SET no_works = 0 WHERE id = ?", [m["id"]])
        if m["status"] in REOPEN_STATUS:
            con.execute(
                "UPDATE leads SET outreach_status = 'pending', "
                "outreach_note = '（修复）之前被误判成没作品，改私信消耗掉了；"
                "实际有作品，重新排回留言队列' WHERE id = ?",
                [m["id"]])
        # 这条之前是被私信消耗掉的 → 把那次私信记录删掉，
        # 否则「30 天内已触达」会挡住它去留言（那 23 条就是这个情况）。
        if m["status"] == "messaged":
            con.execute(
                "DELETE FROM outreach_records WHERE lead_id = ? AND channel = 'dm'",
                [m["id"]])
        fixed += 1
    con.commit()
    print(f"\n✅ 已修复 {fixed} 条：清掉 no_works 标记 + 退回 pending 队列")

    report = pathlib.Path("data/logs/repair_no_works_misjudge.json")
    report.write_text(json.dumps(
        {"misjudged": misjudged, "real_none": real_none, "unknown": unknown},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已存：{report}")
    return 0


def _apply_from_report(path: str) -> int:
    """直接用已有报告落库（不再调接口）

    为什么要留这条路：核对要一条条调接口（534 条 ≈ 35 分钟），
    而且老接口会静默限流，多跑一遍就多一轮被限流的风险。
    报告里的结论已经够用（aweme_count 是接口原样返回的），直接落库即可。
    """
    p = pathlib.Path(path)
    if not p.exists():
        p = pathlib.Path("data/logs/repair_no_works_misjudge_dryrun.json")
    if not p.exists():
        print("找不到报告文件，先跑一次不带 --apply 的核对")
        return 2
    data = json.loads(p.read_text(encoding="utf-8"))
    misjudged = data.get("misjudged") or []
    unknown = data.get("unknown") or []
    print(f"报告：{p}")
    print(f"误判 {len(misjudged)} / 真没作品 {len(data.get('real_none') or [])} / "
          f"查不到 {len(unknown)}")
    if unknown:
        print(f"⚠️ 有 {len(unknown)} 条「查不到」→ **不动**它们（查不到不等于没有作品）")

    # ⚠️ 顺序很关键：**先写飞书，再改本地**（跟 worker._push_feishu_status 一个路数）
    #
    # 为什么必须写飞书：飞书每 5 分钟回拉一次销售改过的状态。
    # 本地改了但飞书没改 → 回拉会把本地盖回飞书的旧值（实测发生过：
    # 323 条修好 → 5 分钟后 274 条被回拉盖回「已跳过」）。
    # 判据是 `bitable_synced_status`：它为空 = 「从没同步过」→ 回拉认为
    # 「本地还是默认值」→ 无条件采纳飞书的值。
    # 所以：写飞书成功 → 把同步标记对齐成当前值（本地==标记→回拉看到两边一样，不动）；
    # 写飞书失败 → 同步标记保持旧的飞书值（本地!=标记→回拉跳过，保护住修复）。
    import asyncio

    from src.config import get_config
    from src.notify.bitable import (
        OUTREACH_LABELS, STATUS_LABELS, get_bitable_writer,
    )

    cfg = get_config()
    writer = get_bitable_writer(cfg)
    if not writer.is_configured:
        print("⚠️ 飞书表没配置 → 只改本地（会被回拉盖回去，治标）")
        push_ok = False
    else:
        push_ok = True

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = {r["id"]: r for r in con.execute(
        "SELECT id, display_id, nickname, status, outreach_status, "
        "bitable_record_id, bitable_synced_status FROM leads WHERE id IN "
        f"({','.join(str(m['id']) for m in misjudged) or '0'})")}

    stats = {"pushed": 0, "push_failed": 0, "no_record": 0, "local": 0}
    marker_pending = "待触达"

    async def _push_all() -> None:
        for m in misjudged:
            r = rows.get(m["id"])
            if r is None:
                stats["local"] += 1
                continue
            rid = (r["bitable_record_id"] or "").strip()
            if not rid:
                stats["no_record"] += 1
                stats["local"] += 1
                continue
            ok, err = await writer.patch_fields(rid, {"触达状态": marker_pending})
            if not ok:
                stats["push_failed"] += 1
                stats["local"] += 1
                continue
            stats["pushed"] += 1
            # 写成功了 → 同步标记对齐成「本地现在的值」
            # （这样回拉看到本地==标记==飞书的值，不会改）
            local_st = STATUS_LABELS.get(r["status"] or "", "待处理")
            con.execute(
                "UPDATE leads SET outreach_status = 'pending', no_works = 0, "
                "outreach_note = '（修复）之前被误判成没作品，改私信消耗掉了；"
                "实际有作品，重新排回留言队列', bitable_synced_status = ? "
                "WHERE id = ?", [f"{local_st}|{marker_pending}", m["id"]])
            con.commit()

    if push_ok:
        asyncio.run(_push_all())
    else:
        for m in misjudged:
            stats["local"] += 1
            con.execute(
                "UPDATE leads SET outreach_status = 'pending', no_works = 0, "
                "outreach_note = '（修复）之前被误判成没作品，改私信消耗掉了；"
                "实际有作品，重新排回留言队列' WHERE id = ?", [m["id"]])
        con.commit()

    # 之前被私信消耗掉的 → 把那次私信记录删掉，
    # 否则「30 天内已触达」会挡住它去留言。
    dm_cleaned = 0
    for m in misjudged:
        if m.get("status") == "messaged":
            cur = con.execute(
                "DELETE FROM outreach_records WHERE lead_id = ? AND channel = 'dm'",
                [m["id"]])
            dm_cleaned += cur.rowcount or 0
    con.commit()

    print(f"\n✅ 修复完成：")
    print(f"   飞书写回「待触达」成功 {stats['pushed']} 条"
          f"（失败 {stats['push_failed']}，无记录 {stats['no_record']}）")
    print(f"   本地退回 pending 队列 {stats['local'] + stats['pushed']} 条")
    print(f"   删掉 {dm_cleaned} 条误发的私信记录（否则 30 天内不会再留言）")
    con.close()
    return 0


def _report(misjudged: list, real_none: list, unknown: list) -> None:
    report = pathlib.Path("data/logs/repair_no_works_misjudge_dryrun.json")
    report.write_text(json.dumps(
        {"misjudged": misjudged, "real_none": real_none, "unknown": unknown},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已存：{report}")


if __name__ == "__main__":
    raise SystemExit(main())
