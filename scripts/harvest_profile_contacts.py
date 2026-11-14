#!/usr/bin/env python3
"""批量补「个人简介里的联系方式」

为什么需要这个脚本
------------------
触达流程里只有**被抽到去触达**的人才会去取简介（``_harvest_profile_contact``），
所以：
- 线索池里 400 多条，只有抽中的那部分有联系方式
- 想回答「这些人里到底多少人有电话/微信」，光看库是不够的

这个脚本把**所有已知 sec_uid 的线索**的简介都拉一遍，
统计命中率，并（可选）把提取到的联系方式写回 leads 表 + 同步飞书。

关键前提（2026-11-13 实测）
--------------------------
抖音的**老** web 接口拿得到**完整**的个人简介：

    https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid=...&aid=1128

- **不需要** a_bogus 签名
- **不需要** 登录态（脱开浏览器直接 HTTP 请求就能拿到，实测 200+ 条 0 失败）
- 每次约 0.2 秒

而主页 DOM 里那份简介是**截断 + 掩码**的（号码被吃掉），
新接口 ``/aweme/v1/web/user/profile/other/`` 要签名、没签回 blocked。

用法
----
    # 只统计（不改任何东西）
    .venv/bin/python scripts/harvest_profile_contacts.py --stats-only

    # 统计 + 把联系方式写回 leads 表
    .venv/bin/python scripts/harvest_profile_contacts.py --apply

    # 只看命中的人
    .venv/bin/python scripts/harvest_profile_contacts.py --stats-only --show-hits
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.intelligence.contact_extractor import extract_contacts  # noqa: E402

DB_PATH = "data/db/douyin_ppp.db"
OLD_API = ("https://www.iesdouyin.com/web/api/v2/user/info/"
           "?sec_uid={sec}&aid=1128&_signature=_02B4Z6wo00f01")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch_profile(sec_uid: str, timeout: float = 15.0) -> dict | None:
    """拉一个人的资料（不要签名、不要登录态）"""
    req = urllib.request.Request(
        OLD_API.format(sec=sec_uid),
        headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    return data.get("user_info") or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH, help="数据库路径")
    ap.add_argument("--limit", type=int, default=0, help="最多查多少条（0=不限）")
    ap.add_argument("--stats-only", action="store_true", help="只统计，不写库")
    ap.add_argument("--apply", action="store_true", help="把联系方式写回 leads 表")
    ap.add_argument("--show-hits", action="store_true", help="打印命中的人的简介原文")
    ap.add_argument("--delay", type=float, default=0.3, help="每条之间的间隔（秒）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"❌ 找不到数据库：{args.db}")
        return 1

    conn = sqlite3.connect(args.db, timeout=20.0)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT id, display_id, nickname, sec_uid, contact_type, contact_value "
               "FROM leads WHERE sec_uid != '' AND display_id != '' ORDER BY id")
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        rows = list(conn.execute(sql).fetchall())

        print(f"待查 {len(rows)} 条（有 sec_uid 的线索）\n")
        total = 0
        has_sig = 0
        has_contact = 0
        failed = 0
        hits = []
        new_contacts = []

        for i, r in enumerate(rows, 1):
            total += 1
            info = fetch_profile(r["sec_uid"])
            if not info:
                failed += 1
                time.sleep(args.delay)
                continue
            sig = str(info.get("signature") or "").strip()
            if sig:
                has_sig += 1
            c = extract_contacts([sig]) if sig else None
            if c is not None and c.has_contact:
                has_contact += 1
                hits.append((r, sig, c))
                if args.apply:
                    # 只在**原来是空**的时候写 —— 已有联系方式的不能被简介覆盖
                    # （弹幕里当场说的电话比简介里的更贴近这次问价）
                    conn.execute(
                        "UPDATE leads SET contact_type=?, contact_value=?, "
                        "updated_at=datetime('now','localtime') "
                        "WHERE id=? AND (contact_value IS NULL OR contact_value='')",
                        [c.contact_type, c.value, r["id"]],
                    )
                    new_contacts.append((r, c))
            if i % 50 == 0:
                print(f"  …已查 {i}/{len(rows)}（有简介 {has_sig}，有联系方式 {has_contact}）")
            time.sleep(args.delay)

        if args.apply:
            conn.commit()

        print(f"\n===== 统计 =====")
        print(f"  查了            {total} 条")
        print(f"  接口失败        {failed} 条")
        print(f"  简介非空        {has_sig} 条（{has_sig * 100 // max(total, 1)}%）")
        print(f"  **有联系方式**  {has_contact} 条"
              f"（{has_contact * 100 // max(total, 1)}%）")
        if args.apply and new_contacts:
            print(f"  新写入 leads 表 {len(new_contacts)} 条")
        elif args.apply:
            print("  没有新的联系方式")

        if args.show_hits and hits:
            print(f"\n===== 命中明细 =====")
            for r, sig, c in hits:
                print(f"  #{r['id']} {r['display_id']} {r['nickname'] or ''}")
                print(f"      {c.contact_type}: {c.value}")
                print(f"      简介: {sig[:100]!r}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
