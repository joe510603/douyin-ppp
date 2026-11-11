"""真发一条评论，全程抓网络 + 截图（用户要求：让我看看你怎么发的）

背景
----
用户反馈：「我用手机能留言，用另一台电脑也能留言，
所以你这边发消息是不是有问题，你真实发一条，让我看看你怎么发的」

这个质疑很可能是对的：如果手机/别的电脑能发，而触达判定「发不出去」，
那可能是**回查逻辑误判**，不是账号被限流。

所以这里用**完全相同**的作品和话术真发一条，并且：
1. 抓下所有跟 comment 相关的请求/响应（URL、状态码、响应体关键字段）
2. 发完立刻截图
3. 再打开作品页看评论到底在不在，截图
4. 顺便跑一遍生产用的回查函数 ``check_comment``，看它怎么说

这样「到底发没发出去」就不是推测，是看得见的。

    .venv/bin/python scripts/check_real_send.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.outreach.browser import Browser  # noqa: E402
from src.outreach.douyin_api import Douyin  # noqa: E402

# 上次被判「发不出去」的那条，原样参数
AWEME_ID = "7693153770800486054"
TEXT = "老板，你在问小米汽车多久能提车对吧？我这边能帮你查下现在的交付节奏～"
NICK = "张氏传媒"
SHOT_DIR = pathlib.Path("data/outreach_shots")


def main() -> int:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    netlog: list[tuple[str, int, str]] = []

    with Browser(pathlib.Path("data/outreach_profile"), headless=False,
                 window_mode="background", block_media=True) as (ctx, page):
        dy = Douyin(page, shot_dir=SHOT_DIR, log=lambda m: print(m, flush=True))

        def on_req(r):
            if "comment" in (r.url or "").lower():
                netlog.append(("REQ", 0, r.method + " " + r.url.split("?")[0][:110]))

        def on_resp(r):
            u = r.url or ""
            if "comment" in u.lower():
                netlog.append(("RESP", r.status, u.split("?")[0][:110]))

        page.on("request", on_req)
        page.on("response", on_resp)

        print(f"\n▶ 作品 {AWEME_ID}")
        print(f"▶ 话术：{TEXT}\n")
        print("=" * 60)

        # ---------- 1. 真发 ----------
        res = dy.post_comment(AWEME_ID, TEXT)
        print("=" * 60)
        print(f"\n▶ post_comment 返回: {json.dumps(res, ensure_ascii=False)}\n")

        page.wait_for_timeout(2500)
        page.screenshot(path=str(SHOT_DIR / "real_send_after.png"))
        print(f"▶ 发完截图: {SHOT_DIR / 'real_send_after.png'}")

        # 把网络日志打出来
        print("\n▶ 与 comment 相关的网络：")
        seen = set()
        for kind, code, info in netlog:
            k = (kind, info)
            if k in seen:
                continue
            seen.add(k)
            print(f"   {kind:5s} {code if code else '':>4} {info}")

        # ---------- 2. 重新打开，看评论在不在 ----------
        print("\n" + "=" * 60)
        print("\n▶ 重新打开作品页看评论区…")
        page.goto(f"https://www.douyin.com/video/{AWEME_ID}",
                  wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        dy._open_comment_panel()
        for _ in range(5):
            try:
                page.evaluate(
                    """() => {
                        const box = document.querySelector('[data-e2e="comment-list"], .comment-mainContent, div[class*="commentList"]');
                        if (box) box.scrollTop = box.scrollHeight;
                        else window.scrollBy(0, 1800);
                    }"""
                )
            except Exception:
                pass
            page.wait_for_timeout(1000)
        page.screenshot(path=str(SHOT_DIR / "real_send_recheck.png"))
        print(f"▶ 回查截图: {SHOT_DIR / 'real_send_recheck.png'}")

        # 页面文字里找我们的评论
        probe = TEXT.strip()[:14]
        try:
            body = page.evaluate("() => document.body.innerText") or ""
        except Exception:
            body = ""
        print(f"\n▶ 页面上找「{probe}」: {'找到了 ✅' if probe in body else '没找到 ❌'}")

        # ---------- 3. 跑生产用的回查函数 ----------
        print("\n▶ 生产用的 check_comment 回查：")
        chk = dy.check_comment(AWEME_ID, TEXT, nickname=None, scrolls=6)
        print(f"   {json.dumps(chk, ensure_ascii=False)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
