"""把发评论时所有关键接口的响应体都抓出来

要弄清楚：抖音到底给了什么裁决？
- ``comment/publish``：发评论的结果（实测响应体是空的）
- ``passport/safe/query_decision/``：平台的风控裁决（pass / verify / block）
- ``second_verification`` 相关：二次验证

    .venv/bin/python scripts/check_publish_response.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.outreach.browser import Browser  # noqa: E402
from src.outreach.douyin_api import Douyin  # noqa: E402

AWEME_ID = "7693153770800486054"
TEXT = "老板，你在问小米汽车多久能提车对吧？我这边能帮你查下现在的交付节奏～"

WATCH = ("comment/publish", "query_decision", "second_verification", "verify")


def main() -> int:
    log: list[tuple[str, int, str, str]] = []

    with Browser(pathlib.Path("data/outreach_profile"), headless=False,
                 window_mode="background", block_media=True) as (ctx, page):
        dy = Douyin(page, shot_dir=pathlib.Path("data/outreach_shots"),
                    log=lambda m: print(m, flush=True))

        def on_resp(r):
            u = r.url or ""
            if any(w in u for w in WATCH):
                try:
                    body = r.text()
                except Exception as e:
                    body = f"<读不到: {e}>"
                log.append((u.split("?")[0][:90], r.status,
                            r.headers.get("content-type", ""), body))

        page.on("response", on_resp)

        print(f"\n▶ 作品 {AWEME_ID}")
        try:
            res = dy.post_comment(AWEME_ID, TEXT)
        except Exception as e:
            res = {"raised": type(e).__name__, "msg": str(e),
                   "kind": getattr(e, "kind", "")}
        page.wait_for_timeout(3000)
        page.screenshot(path=str(pathlib.Path("data/outreach_shots")
                                 / "publish_verdict.png"))

        print("\n" + "=" * 66)
        print("▶ post_comment:", json.dumps(res, ensure_ascii=False))
        print("=" * 66)
        for url, status, ctype, body in log:
            print(f"\n--- {url}")
            print(f"    status={status}  content-type={ctype}  len={len(body)}")
            if body:
                try:
                    print("    " + json.dumps(json.loads(body), ensure_ascii=False)[:1500])
                except Exception:
                    print("    " + body[:800])
            else:
                print("    <空响应体>")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
