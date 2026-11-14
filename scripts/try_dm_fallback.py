#!/usr/bin/env python3
"""实测私信兜底：拿一条「没有作品」的线索跑真实生产路径

为什么需要这个脚本
------------------
私信是**不可撤回**的对外动作（陌生人私信在对方回复/关注前只有一次机会），
所以不能一上来就让自动触达发。

这个脚本走的是**跟网页上点「触达」完全一样的代码路径**：
    Douyin.resolve_by_sec_uid → OutreachWorker._fallback_to_dm → Douyin.send_dm

用法
----
    # 干跑：不发送、不回写状态，只看流程通不通、文案长什么样
    .venv/bin/python scripts/try_dm_fallback.py 2179490450 --dry

    # 真发一条（谨慎！不可撤回）
    .venv/bin/python scripts/try_dm_fallback.py 2179490450 --send
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.douyin_api import Douyin  # noqa: E402
from src.outreach.worker import OutreachWorker  # noqa: E402

DB = "data/db/douyin_ppp.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("display_id", help="要试的抖音号")
    ap.add_argument("--dry", action="store_true", help="干跑（不发送）")
    ap.add_argument("--send", action="store_true", help="真发（不可撤回！）")
    args = ap.parse_args()

    cfg = get_config()
    cfg.outreach.dm_fallback = True       # 这个脚本就是来试私信的
    profile = pathlib.Path(cfg.outreach.profile_dir).absolute()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM leads WHERE display_id = ?", (args.display_id,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"❌ 库里没有抖音号 {args.display_id}")
        return 1
    lead = dict(row)
    print(f"目标：#{lead['id']} {lead['display_id']} {lead['nickname']}")
    print(f"当前状态：{lead['outreach_status']}　{lead['outreach_note'] or ''}")
    print(f"profile：{profile}\n")

    logs: list[str] = []

    def _log(m: str) -> None:
        logs.append(str(m))
        print(str(m))

    worker = OutreachWorker(cfg, DB, log=_log)
    worker._dry = not args.send
    worker._last_channel = ""
    try:
        from src.outreach.browser import Browser

        with Browser(profile, headless=False, slow_mo=0,
                     window_mode="background") as (_ctx, page):
            dy = Douyin(page, log=_log)
            info = dy.resolve_by_sec_uid(lead["sec_uid"])
            if not info:
                print("❌ 主页打不开")
                return 1
            print(f"\n主页信息：昵称={info.get('nickname')} "
                  f"作品数={info.get('aweme_count')}")
            lead["nickname"] = info.get("nickname") or lead.get("nickname")
            lead["sec_uid"] = info.get("sec_uid") or lead["sec_uid"]

            summary = {"dry": 0}
            worker._fallback_to_dm(dy, lead, summary, why="实测：没有作品")
            print(f"\n结果：{summary}")
            if args.send:
                print("⚠️ 已真发，私信不可撤回")
            else:
                print("（干跑，没有发送）")
    finally:
        worker.st.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
