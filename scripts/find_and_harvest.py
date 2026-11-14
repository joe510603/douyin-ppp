#!/usr/bin/env python3
"""补抓「还没抓到过联系方式」的线索 —— **只抓联系方式，不留言**

为什么需要这个脚本
------------------
线索的联系方式有两个来源：
1. **弹幕原文**（客户当场说的）—— 采集时就提取了
2. **个人简介**（写在抖音主页上的）—— 要先**找到人**（拿到 sec_uid）才能取

第 2 条有个历史坑：**早期版本的触达没存 sec_uid**，
所以那批已经留言成功的线索（36 条），简介从来没被取过 →
联系方式也就从来没写进库。

现在库里 418 条线索里：
- 225 条有 sec_uid → 简介已全扫过（``harvest_profile_contacts.py``）
- 195 条没有 sec_uid → **取不到简介**，除非重新搜到人

这个脚本就是补这 195 条（以及以后新出现的）：

    搜人（复用触达同一段代码）→ 拿 sec_uid（存库，下次不用再搜）
    → 取简介 → 提取联系方式 → 写库 + 同步飞书

**只抓联系方式，不留言、不私信**：
- 已经留言过的人，再留言一次就是骚扰客户
- 私信额度是稀缺资源，不能拿来「探路」

安全阀
------
1. 默认**干跑**，加 ``--apply`` 才真跑
2. ``--limit`` 限制一次跑多少条（搜索要花 40~60 秒/条，还吃风控额度）
3. 搜索之间按配置里的间隔等（默认 20~30 秒）
4. 连续搜不到 N 次就停（搜索接口被限流的信号）
5. **已经留言过的人优先**（他们的线索质量最高，简介也最可能有联系方式）

用法
----
    .venv/bin/python scripts/find_and_harvest.py --limit 5            # 干跑
    .venv/bin/python scripts/find_and_harvest.py --limit 5 --apply    # 真跑 5 条
    .venv/bin/python scripts/find_and_harvest.py --apply --group all  # 连没留言过的也跑
"""
from __future__ import annotations

import argparse
import os
import pathlib
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = "data/db/douyin_ppp.db"


def pick_targets(conn: sqlite3.Connection, limit: int, group: str) -> list[sqlite3.Row]:
    """挑「还没抓到过联系方式」的人

    ``group``:
    - ``todo``（默认）：**留言成功过但没存 sec_uid** 的那些
      —— 质量最高，而且绝对不能再留言（会骚扰客户），只补联系方式
    - ``all``：所有没 sec_uid 的（包含还没轮到触达的人）
    """
    if group == "todo":
        sql = """
            SELECT id, display_id, nickname, sec_uid, outreach_status,
                   contact_type, contact_value
              FROM leads
             WHERE (sec_uid IS NULL OR sec_uid = '')
               AND display_id != ''
               AND outreach_status IN ('commented', 'messaged', 'converted')
             ORDER BY id
             LIMIT ?
        """
    else:
        sql = """
            SELECT id, display_id, nickname, sec_uid, outreach_status,
                   contact_type, contact_value
              FROM leads
             WHERE (sec_uid IS NULL OR sec_uid = '')
               AND display_id != ''
             ORDER BY CASE outreach_status
                        WHEN 'commented' THEN 0 WHEN 'messaged' THEN 1
                        WHEN 'pending' THEN 2 ELSE 3 END, id
             LIMIT ?
        """
    return list(conn.execute(sql, [limit]).fetchall())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH, help="数据库路径")
    ap.add_argument("--limit", type=int, default=5, help="最多处理多少条")
    ap.add_argument("--apply", action="store_true", help="真跑（默认干跑）")
    ap.add_argument("--group", default="todo", choices=["todo", "all"],
                    help="todo=只补留言成功过的；all=所有没 sec_uid 的")
    ap.add_argument("--delay", type=int, default=25, help="每次搜索之间的间隔（秒）")
    ap.add_argument("--max-miss", type=int, default=3,
                    help="连续几条搜不到就停手（多半是搜索接口被限流）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"❌ 找不到数据库：{args.db}")
        return 1

    from src.config import get_config
    from src.intelligence.contact_extractor import extract_contacts
    from src.outreach.browser import Browser
    from src.outreach.douyin_api import (
        Douyin, RiskControl, normalize_kind,
    )
    from src.outreach.store import OutreachStore

    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())

    conn = sqlite3.connect(args.db, timeout=20.0)
    conn.row_factory = sqlite3.Row
    targets = pick_targets(conn, args.limit, args.group)
    if not targets:
        print("✅ 没有需要补抓的线索")
        return 0
    print(f"待处理 {len(targets)} 条（分组 {args.group}）")
    print(f"profile: {profile}\n")
    if not args.apply:
        for r in targets:
            print(f"  #{r['id']:<5} {r['display_id']:<20} "
                  f"{(r['nickname'] or '')[:14]:<16} 状态={r['outreach_status']}")
        print("\n（干跑，没有搜索。加 --apply 才真跑）")
        conn.close()
        return 0

    store = OutreachStore(args.db)
    found = 0
    got_contact = 0
    miss_streak = 0
    try:
        with Browser(profile, headless=False, slow_mo=0,
                     window_mode="background") as (_ctx, page):
            dy = Douyin(page, log=lambda m: print("   " + str(m)))
            for i, r in enumerate(targets, 1):
                print(f"\n[{i}/{len(targets)}] #{r['id']} {r['display_id']} "
                      f"{r['nickname'] or ''}")
                try:
                    info = dy.resolve(r["display_id"], nickname=r["nickname"])
                except RiskControl as e:
                    # ⚠⚠ 撞上验证/限流 → **立刻停手**，不许继续撞。
                    #
                    # 为什么必须停：验证码和限流是**账号级**的风控信号。
                    # 继续撞只会把冷却期越拖越长，最后整个号都不能用 ——
                    # 而这个脚本的价值只是「多抓几个联系方式」，
                    # 拿账号去换不值。
                    kind = normalize_kind(getattr(e, "kind", "") or "verify")
                    print(f"\n🛑 撞上抖音风控（{kind}），立刻停手：{str(e)[:80]}")
                    print("   · 验证类：去网页「抖音触达」点「打开浏览器窗口」人工过拼图，"
                          "过完再跑这个脚本")
                    print("   · 限流类：等半小时到一小时，搜索接口自己会恢复")
                    print(f"   已处理 {i-1}/{len(targets)} 条，"
                          f"找到人 {found} 个，抓到联系方式 {got_contact} 条")
                    break
                except Exception as e:
                    print(f"   搜索异常：{str(e)[:70]}")
                    info = None
                if not info:
                    print("   没搜到这个人（抖音搜索是概率性的，下次再试）")
                    miss_streak += 1
                    # 连续搜不到 → 搜索接口多半被限流了。
                    # 抖音的搜索限流**不报错**，就是给你一个空的 user_list，
                    # 所以「搜不到」既可能是这个人真没了，也可能是接口被掐了。
                    # 分不清就别硬扫 —— 连着扫只会让限流窗口一直续着。
                    if miss_streak >= args.max_miss:
                        print(f"\n🛑 连续 {miss_streak} 条没搜到，多半是搜索接口被限流了，"
                              f"停手（等 30 分钟~1 小时它自己会恢复）")
                        print(f"   已处理 {i}/{len(targets)} 条，"
                              f"找到人 {found} 个，抓到联系方式 {got_contact} 条")
                        break
                    time.sleep(args.delay)
                    continue
                miss_streak = 0
                found += 1
                sec = info.get("sec_uid") or ""
                nick = info.get("nickname") or r["nickname"] or ""
                print(f"   ✓ 找到：{nick}")

                # sec_uid 存下来 → 下次触达直接开主页，省一次搜索
                if sec:
                    store.set_sec_uid(r["id"], sec, nick)

                # 取简介 → 提联系方式
                sig = str(info.get("signature") or "")
                if not sig:
                    pinfo = dy.fetch_profile_info(sec) or {}
                    sig = str(pinfo.get("signature") or "")
                c = extract_contacts([sig]) if sig else None
                if c is None or not c.has_contact:
                    print("   简介里没有联系方式")
                else:
                    changed, m_type, m_value = store.set_contact(
                        r["id"], c.contact_type, c.value, prefer_new_same_type=False)
                    got_contact += 1
                    print(f"   📞 {c.contact_type}: {c.value}"
                          + ("" if changed else "（已有同类联系方式，保留原来的）"))
                    print(f"      简介原文：{sig[:90]!r}")
                    # 同步飞书「联系方式」列
                    if changed:
                        _push_bitable(cfg, r["id"], m_value)
                time.sleep(args.delay)
    finally:
        store.close()
        conn.close()

    print(f"\n===== 结果 =====")
    print(f"  处理 {len(targets)} 条，找到人 {found} 条，抓到联系方式 {got_contact} 条")
    return 0


def _push_bitable(cfg, lead_id: int, contact: str) -> None:
    """把联系方式同步到飞书表的「联系方式」列

    用 ``patch_fields`` 只改这一个字段 ——
    **不能用整体替换**（会把销售改过的「处理状态」「触达状态」清掉）。

    失败不影响主流程（联系方式已经写进本地库了，
    飞书那边下一轮回拉也会对齐）。
    """
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    try:
        row = conn.execute(
            "SELECT bitable_record_id FROM leads WHERE id = ?", [lead_id]
        ).fetchone()
    finally:
        conn.close()
    record_id = (row[0] if row else "") or ""
    if not record_id:
        return
    try:
        import asyncio

        from src.notify.bitable import get_bitable_writer

        writer = get_bitable_writer(cfg)
        ok, err = asyncio.run(writer.patch_fields(record_id, {"联系方式": contact}))
        if ok:
            print("      ✓ 已同步飞书「联系方式」列")
        else:
            print(f"      （飞书同步失败：{err}）")
    except Exception as e:
        print(f"      （飞书同步失败：{str(e)[:60]}）")


if __name__ == "__main__":
    raise SystemExit(main())
