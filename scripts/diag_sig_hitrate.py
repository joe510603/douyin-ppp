"""诊断：简介里到底有多少人能提取出联系方式（命中率）

已确认（2026-11-13 实测）：
  ``https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid=...&aid=1128``
  不需要 a_bogus 签名，直接返回**完整的** signature（主页 DOM 那个是被截断的）。

这个脚本拿一批真实线索跑一遍，看：
  1. 多少人的简介非空
  2. 多少人的简介能提取出电话/微信/QQ
  3. 提取出来的长什么样（人工核对）

用法：
    .venv/bin/python scripts/diag_sig_hitrate.py [条数]
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.intelligence.contact_extractor import extract_contacts  # noqa: E402
from src.outreach.browser import Browser  # noqa: E402

OLD_API = ("https://www.iesdouyin.com/web/api/v2/user/info/"
           "?sec_uid={sec}&aid=1128&_signature=_02B4Z6wo00f01")


def main() -> int:
    cfg = get_config()
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25

    import sqlite3
    db = sqlite3.connect("data/db/douyin_ppp.db")
    rows = db.execute(
        "SELECT display_id, nickname, sec_uid FROM leads "
        "WHERE sec_uid != '' AND display_id != '' ORDER BY id LIMIT ?", (n,)
    ).fetchall()
    db.close()
    print(f"profile: {profile}  样本 {len(rows)} 条\n")

    with Browser(profile, headless=False, slow_mo=0, window_mode="background") as (_ctx, page):
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)

        has_sig = 0
        has_contact = 0
        fails = 0
        for display_id, nick, sec in rows:
            try:
                import json
                resp = page.request.get(OLD_API.format(sec=sec), timeout=20000)
                j = json.loads(resp.text())
                u = j.get("user_info") or j.get("user") or {}
                sig = (u.get("signature") or "").strip()
                if not sig:
                    print(f"  · {display_id:<14} {nick[:12]:<14} 简介为空")
                    continue
                has_sig += 1
                c = extract_contacts([sig])
                mark = ""
                if c.has_contact:
                    has_contact += 1
                    mark = f"★ {c.contact_type} → {c.value}"
                print(f"  · {display_id:<14} {nick[:12]:<14} 简介={sig[:70]!r} {mark}")
            except Exception as e:
                fails += 1
                print(f"  · {display_id:<14} ❌ {type(e).__name__}: {str(e)[:80]}")

        print(f"\n样本 {len(rows)}：接口失败 {fails}，简介非空 {has_sig}，"
              f"能提取到联系方式 {has_contact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
