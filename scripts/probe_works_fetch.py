#!/usr/bin/env python3
"""探针：为什么「有作品的人」被判成没作品、于是改发私信？

要回答的问题（2026-11-26 实测）：
    日志明明写着「✓ 定位到 XXX（作品 7 条）」，下一行却是「没有作品，改发私信」。
    说明「作品抓取」返回了空 —— 但**抓不到**和**真没有**是两回事。

这个探针做三件事，每件事都留下证据：
    1. 抓 /aweme/v1/web/aweme/post/ 的**原始响应**（状态码 / 字节数 / aweme_list 条数）
    2. 抓主页 DOM 的作品网格条数（接口空时的兜底）
    3. 抓页面标题和正文片段（用来认出「壳页」「验证码页」这种降级页）

用法：
    .venv/bin/python scripts/probe_works_fetch.py                # 默认实验组+对照组
    .venv/bin/python scripts/probe_works_fetch.py --sec-uid XXX   # 临时查一个

⚠️ 必须先停服务：data/outreach_profile 不能被两个进程同时用。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.outreach.browser import Browser  # noqa: E402
from src.outreach.douyin_api import DOUYIN  # noqa: E402

# 实验组：今天被判「没有可用的公开作品」→ 改发私信的人
EXPERIMENT = [
    ("实验组·秋花水凝👻", "81276160249",
     "MS4wLjABAAAAzKISH2laqYZFIRLjYzOb6Q1MGWYUDfZ8etT_CSZBgjnM44mAMOk-fMarxHLNkCOB"),
    ("实验组·N", "970221sh",
     "MS4wLjABAAAA0ol6JrEMKe1MRukQraDh8JEPbeFdqDN3ZWz2tMEF2zA"),
]
# 对照组：11-25 留言成功过的人（当时作品抓得好好的）
CONTROL = [
    ("对照组·钓鱼人", "7967chuan",
     "MS4wLjABAAAAdeD0yW2QVmBrZ6c8FJaefTmodBXTSlYMe4sEgAx3ZQ8"),
    ("对照组·🐟", "zxj9699",
     "MS4wLjABAAAAm9k6jxhte3JrvAGT34wGv0G2rozi_Q4_NvzKy6Y1IS8"),
]


def probe(page, label: str, sec_uid: str) -> dict:
    """开一次主页，把「作品到底抓没抓到」的证据全带回来"""
    hits: list[dict] = []

    def on_resp(r) -> None:
        try:
            if "/aweme/v1/web/aweme/post/" in r.url:
                try:
                    body = r.text()
                except Exception as e:
                    body = ""
                    hits.append({"status": r.status, "err": f"read body failed: {e}"})
                    return
                n = 0
                try:
                    j = json.loads(body)
                    n = len(j.get("aweme_list") or [])
                except Exception:
                    pass
                hits.append({
                    "status": r.status,
                    "bytes": len(body),
                    "aweme_list": n,
                    "url_tail": r.url.split("?")[0][-60:],
                })
        except Exception:
            pass

    page.on("response", on_resp)
    url = f"{DOUYIN}/user/{sec_uid}"
    t0 = time.time()
    err = ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(700)
        try:
            page.mouse.wheel(0, 1400)
        except Exception:
            pass
        page.wait_for_timeout(11000)      # 等作品接口
        try:
            page.mouse.wheel(0, 1800)
        except Exception:
            pass
        page.wait_for_timeout(5000)       # 再给一次机会
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    dom = page.evaluate(
        """() => {
          const box = document.querySelector('[data-e2e="user-post-list"]')
                  || document.querySelector('ul[scroll-list]');
          const inBox = box ? box.querySelectorAll('a[href*="/video/"]').length : -1;
          return {
            has_post_list: !!document.querySelector('[data-e2e="user-post-list"]'),
            in_box: inBox,
            all_video_links: document.querySelectorAll('a[href*="/video/"]').length,
            title: (document.title || '').trim().slice(0, 80),
            body: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 400),
          };
        }"""
    ) or {}
    try:
        page.off("response", on_resp)
    except Exception:
        pass

    out = {
        "label": label,
        "sec_uid": sec_uid,
        "url": url,
        "elapsed": round(time.time() - t0, 1),
        "nav_error": err,
        "post_api_hits": hits,
        "dom": dom,
    }
    print("\n" + "=" * 78)
    print(f"{label}  {sec_uid[:28]}…  用时 {out['elapsed']}s")
    if err:
        print(f"  导航出错：{err}")
    print(f"  作品接口命中 {len(hits)} 次：")
    for h in hits:
        if "err" in h:
            print(f"    - 读响应体失败：{h['err']}")
        else:
            print(f"    - HTTP {h['status']}  {h['bytes']} 字节  aweme_list={h['aweme_list']} 条")
    d = dom or {}
    print(f"  DOM：作品列表容器={d.get('has_post_list')}  容器内作品={d.get('in_box')}  "
          f"全页 /video/ 链接={d.get('all_video_links')}")
    print(f"  标题：{d.get('title')}")
    print(f"  正文：{(d.get('body') or '')[:200]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec-uid", default="", help="临时查一个人")
    ap.add_argument("--profile", default="data/outreach_profile")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    if args.sec_uid:
        targets = [("临时指定", "", args.sec_uid)]
    else:
        targets = EXPERIMENT + CONTROL

    profile = pathlib.Path(args.profile).resolve()
    if not profile.exists():
        print(f"profile 不存在：{profile}")
        return 2

    results = []
    with Browser(profile, headless=args.headless, slow_mo=0,
                 window_mode="offscreen", block_media=True) as (_ctx, page):
        for label, _did, sec in targets:
            if not sec:
                continue
            try:
                results.append(probe(page, label, sec))
            except Exception as e:
                print(f"{label} 探针异常：{type(e).__name__}: {e}")
            time.sleep(2)

    print("\n\n" + "#" * 78)
    print("汇总（接口条数 / DOM 条数）：")
    for r in results:
        api = sum(h.get("aweme_list") or 0 for h in r["post_api_hits"])
        dom = (r.get("dom") or {}).get("in_box")
        print(f"  {r['label']:<22} 接口 {api:>3} 条   DOM {dom} 条")

    out = pathlib.Path("data/logs/probe_works_fetch.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n证据已存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
