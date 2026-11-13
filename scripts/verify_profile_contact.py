"""端到端验证：主页简介 → 联系方式 → 写库 → 飞书（生产代码路径）

跟网页上点「触达」走的是**同一段代码**：
  ``Douyin.resolve_by_sec_uid`` → ``OutreachWorker._harvest_profile_contact``

不会发评论、不改触达状态，只补联系方式。

用法：
    .venv/bin/python scripts/verify_profile_contact.py [抖音号 ...]
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.outreach.browser import Browser  # noqa: E402
from src.outreach.douyin_api import Douyin  # noqa: E402
from src.outreach.worker import OutreachWorker  # noqa: E402

DB = "data/db/douyin_ppp.db"


def _读(lead_id: int):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT display_id, nickname, contact_type, contact_value, bitable_record_id "
        "FROM leads WHERE id = ?", [lead_id]).fetchone()
    conn.close()
    return row


def main() -> int:
    cfg = get_config()
    targets = sys.argv[1:] or ["xxy22244"]
    profile = str(pathlib.Path(cfg.outreach.profile_dir).absolute())
    print(f"profile: {profile}\n")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    leads = []
    for t in targets:
        r = conn.execute(
            "SELECT * FROM leads WHERE display_id = ?", [t]).fetchone()
        if r:
            leads.append(dict(r))
    conn.close()
    if not leads:
        print("没找到这些线索")
        return 1

    with Browser(profile, headless=False, slow_mo=0, window_mode="background") as (_ctx, page):
        dy = Douyin(page, log=lambda m: print("   ", m))
        w = OutreachWorker(cfg, DB, log=lambda m: print("   ", m))
        for lead in leads:
            lead_id = lead["id"]
            before = _读(lead_id)
            print(f"═════ {lead['display_id']}　{lead.get('nickname')} ═════")
            print(f"   之前: contact_type={before[2]!r} contact_value={before[3]!r}")

            sec = str(lead.get("sec_uid") or "").strip()
            if sec:
                info = dy.resolve_by_sec_uid(sec)
            else:
                info = dy.resolve(lead["display_id"], nickname=lead.get("nickname"))
            if not info:
                print("   ❌ 没定位到人")
                continue
            sig = str(info.get("signature") or "")
            print(f"   简介（{len(sig)} 字）= {sig[:160]!r}")

            w._harvest_profile_contact(info, lead)
            # 等飞书后台线程写完
            t0 = time.time()
            while time.time() - t0 < 30:
                if w._feishu.flush(timeout=5):
                    break
            after = _读(lead_id)
            print(f"   之后: contact_type={after[2]!r} contact_value={after[3]!r}")
            print()
    w._feishu.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
